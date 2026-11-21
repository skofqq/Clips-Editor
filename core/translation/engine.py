"""Runs a translation model: SentencePiece in, CTranslate2 beam search, SentencePiece out."""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

BATCH = 16


class Translator:
    """Translates lines of text with a downloaded model folder (see :mod:`core.translation.models`)."""

    def __init__(self, folder: Path, device: str = "auto") -> None:
        import ctranslate2
        import sentencepiece as spm

        folder = Path(folder)
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

    def translate(self, lines: list[str], beam_size: int = 4, progress=None, cancel=None) -> list[str]:
        """One translation per line; empty lines stay empty."""
        result = [""] * len(lines)
        todo = [(i, line.strip()) for i, line in enumerate(lines) if line.strip()]
        for start in range(0, len(todo), BATCH):
            if cancel is not None and cancel.is_set():
                from core.translation.models import TranslationCancelled

                raise TranslationCancelled()
            chunk = todo[start:start + BATCH]
            tokens = [self.source.encode(text, out_type=str) for _i, text in chunk]
            outputs = self.model.translate_batch(tokens, beam_size=beam_size, max_decoding_length=256)
            for (i, _text), output in zip(chunk, outputs):
                result[i] = self.target.decode(output.hypotheses[0]).strip()
            if progress is not None:
                progress(min(1.0, (start + len(chunk)) / len(todo)), "")
        return result
