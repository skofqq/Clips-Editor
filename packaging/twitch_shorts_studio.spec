# PyInstaller build of Twitch Shorts Studio: one folder, everything next to the exe.
#   .venv\Scripts\pyinstaller --noconfirm packaging\twitch_shorts_studio.spec
# build_installer.bat runs this and then Inno Setup.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

root = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH is set by PyInstaller

datas = [
    (str(root / "ui" / "theme" / "icons"), "ui/theme/icons"),
    (str(root / "ui" / "theme" / "m3.qss"), "ui/theme"),
    (str(root / "docs"), "docs"),
]
datas += collect_data_files("faster_whisper")  # the Silero VAD model
datas += collect_data_files("onnx_asr")  # feature extractors for GigaAM / Parakeet
datas += copy_metadata("onnx-asr")  # onnx_asr reads its own version when imported
binaries = (collect_dynamic_libs("ctranslate2") + collect_dynamic_libs("onnxruntime")
            + collect_dynamic_libs("sentencepiece"))
ffmpeg = root / "tools" / "ffmpeg" / "bin"
for exe in ("ffmpeg.exe", "ffprobe.exe"):
    if (ffmpeg / exe).is_file():
        binaries.append((str(ffmpeg / exe), "tools/ffmpeg/bin"))

hiddenimports = (collect_submodules("app") + collect_submodules("core") + collect_submodules("ui")
                 + collect_submodules("keyring.backends") + collect_submodules("onnx_asr") + collect_submodules("sentencepiece")
                 + ["win32ctypes.core"])

a = Analysis(
    [str(root / "app" / "main.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "matplotlib", "IPython", "torch"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TwitchShortsStudio",
    icon=str(root / "packaging" / "app.ico"),
    console=False,
    upx=False,
    contents_directory=".",  # docs, theme and tools/ffmpeg sit next to the exe, as in the source tree
)
coll = COLLECT(exe, a.binaries, a.datas, name="TwitchShortsStudio", upx=False)  # noqa: F821
