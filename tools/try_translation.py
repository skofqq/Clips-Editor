"""Try the offline translation models and print the results.

    .venv\\Scripts\\python tools\\try_translation.py [ru-en|en-ru ...]

Downloads the models into the app's models folder on first use (~400 MB each),
then translates a few stream phrases and shows the device and the time taken.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAMPLES = {
    "ru-en": ["Привет, ребята, сегодня играем в новую игру.", "Это просто жесть, он его убил с одного выстрела!",
              "Чат, вы видели это? Я в шоке.", "Ахаха, ну ты клоун."],
    "en-ru": ["Hi guys, today we are playing a new game.", "That was insane, he killed him with one shot!",
              "Chat, did you see that? I'm shocked.", "Haha, you're such a clown."],
}


def main(argv: list[str]) -> int:
    from app.config import AppConfig
    from core.translation.engine import Translator
    from core.translation.models import download_model

    models_dir = AppConfig.load().data_dir / "models"
    for key in argv or list(SAMPLES):
        started = time.perf_counter()
        folder = download_model(models_dir, key, lambda f, m: print(f"  {m}", end="\r", flush=True))
        print(f"\n{key}: model in {folder} ({time.perf_counter() - started:.1f} s)")
        started = time.perf_counter()
        translator = Translator(folder)
        print(f"  loaded on {translator.device} in {time.perf_counter() - started:.1f} s")
        started = time.perf_counter()
        for line, out in zip(SAMPLES[key], translator.translate(SAMPLES[key])):
            print(f"  {line}\n    -> {out}")
        print(f"  translated in {time.perf_counter() - started:.2f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
