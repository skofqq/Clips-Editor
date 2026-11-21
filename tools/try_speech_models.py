"""Try the speech models on a real clip and print what each one heard.

    .venv\\Scripts\\python tools\\try_speech_models.py clip.mp4 [model ...]

Default models: small, gigaam-v3, parakeet-v3. Also runs the swear-word
double check for the first model and lists the swear words found.
Models download into the app's models folder on first use.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def main(argv: list[str]) -> int:

    from app.config import AppConfig
    from core.censor.dictionary import Dictionary
    from core.censor.review import detect
    from core.media.ffmpeg_locator import locate_ffmpeg
    from core.transcription.engine import TranscribeOptions, WhisperEngine, transcribe_file

    if not argv:
        print(__doc__)
        return 2
    clip = Path(argv[0])
    models = argv[1:] or ["small", "gigaam-v3", "parakeet-v3"]
    config = AppConfig.load()
    engine = WhisperEngine(config.data_dir / "models")
    ffmpeg = locate_ffmpeg().ffmpeg
    last = [""]

    def progress(_fraction: float, message: str) -> None:
        if message.split("…")[0] != last[0]:
            last[0] = message.split("…")[0]
            print("   ", message, flush=True)

    for n, model in enumerate(models):
        options = TranscribeOptions(model=model, language="auto", cross_check=(n == 0))
        started = time.monotonic()
        transcript = transcribe_file(engine, clip, ffmpeg, options, progress)
        took = time.monotonic() - started
        print(f"\n=== {model} ({transcript.language}, {took:.1f} s, device {engine.device_used}) ===")
        for segment in transcript.segments:
            print(f"[{segment.start_us / 1e6:7.2f}-{segment.end_us / 1e6:7.2f}] {segment.text}")
        found = detect(transcript, Dictionary()).suggestions
        print(f"--- swear words: {len(found)}")
        for s in found:
            mark = "  (only the 2nd model: " + transcript.check_model + ")" if s.source else ""
            print(f"    {s.word_start_us / 1e6:7.2f}-{s.word_end_us / 1e6:7.2f}  {s.text}{mark}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
