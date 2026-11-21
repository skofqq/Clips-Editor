"""Runs a translation model: SentencePiece in, CTranslate2 beam search, SentencePiece out."""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

BATCH = 16
# The models translate one sentence at a time and may drop the rest of a longer line.
_SENTENCES = re.compile(r"(?<=[.!?…])\s+")


def split_sentences(line: str) -> list[str]:
    return [part for part in _SENTENCES.split(line.strip()) if part]


class Translator:
    """Translates lines of text with a downloaded model folder (see :mod:`core.translation.models`)."""

    def __init__(self, folder: Path, device: str = "auto") -> None:
        import ctranslate2
        import sentencepiece as spm

        from core.transcription.engine import add_cuda_dll_dirs

        add_cuda_dll_dirs()  # pip-installed cuBLAS (requirements-gpu.txt) must be findable on Windows
        folder = Path(folder)
        self.folder = folder
        self.source = spm.SentencePieceProcessor(model_file=str(folder / "src.spm.model"))
        self.target = spm.SentencePieceProcessor(model_file=str(folder / "tgt.spm.model"))
        if device == "auto":
            try:
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:  # noqa: BLE001 - no CUDA runtime
                device = "cpu"
        self.model = None
        if device == "cuda":
            try:
                self.model = ctranslate2.Translator(str(folder), device="cuda", compute_type="int8_float16")
            except Exception as exc:  # noqa: BLE001 - CUDA libraries missing: fall back to the CPU
                log.info("Translation on the GPU failed (%s); using the CPU", exc)
                device = "cpu"
        if self.model is None:
            self.model = ctranslate2.Translator(str(folder), device="cpu", compute_type="int8")
        self.device = device

    def _use_cpu(self) -> None:
        import ctranslate2

        self.model = ctranslate2.Translator(str(self.folder), device="cpu", compute_type="int8")
        self.device = "cpu"

    def translate(self, lines: list[str], beam_size: int = 4, progress=None, cancel=None) -> list[str]:
        """One translation per line; empty lines stay empty. Each sentence is translated on its own."""
        pieces = [(i, part) for i, line in enumerate(lines) for part in split_sentences(line)]
        done = self._translate_pieces([p for _i, p in pieces], beam_size, progress, cancel)
        result: list[list[str]] = [[] for _ in lines]
        for (i, _part), text in zip(pieces, done):
            if text:
                result[i].append(text)
        return [" ".join(parts) for parts in result]

    def _translate_pieces(self, todo: list[str], beam_size: int, progress, cancel) -> list[str]:
        result: list[str] = []
        for start in range(0, len(todo), BATCH):
            if cancel is not None and cancel.is_set():
                from core.translation.models import TranslationCancelled

                raise TranslationCancelled()
            chunk = todo[start:start + BATCH]
            tokens = [self.source.encode(text, out_type=str) for text in chunk]
            try:
                outputs = self.model.translate_batch(tokens, beam_size=beam_size, max_decoding_length=256)
            except RuntimeError as exc:
                if self.device != "cuda":
                    raise
                # The GPU loaded the model but a CUDA library is missing at run time: use the CPU.
                log.info("Translation on the GPU failed (%s); using the CPU", exc)
                self._use_cpu()
                outputs = self.model.translate_batch(tokens, beam_size=beam_size, max_decoding_length=256)
            result += [self.target.decode(output.hypotheses[0]).strip() for output in outputs]
            if progress is not None:
                progress(min(1.0, (start + len(chunk)) / len(todo)), "")
        return result
