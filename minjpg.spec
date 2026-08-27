# PyInstaller spec — builds a single-file minjpg executable.
#
#   pyinstaller minjpg.spec
#
# The bundled cjpeg for the *current* platform is added as a binary; at runtime
# encoder.py finds it under sys._MEIPASS/vendor/.

import platform
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

spec_dir = Path(SPECPATH)

_arch = {
    "x86_64": "x86_64", "amd64": "x86_64",
    "aarch64": "arm64", "arm64": "arm64",
}.get(platform.machine().lower(), platform.machine().lower())

if platform.system() == "Windows":
    _cjpeg = f"cjpeg-windows-{_arch}.exe"
else:
    _cjpeg = f"cjpeg-{platform.system().lower()}-{_arch}"

_cjpeg_path = spec_dir / "vendor" / _cjpeg
if not _cjpeg_path.is_file():
    raise SystemExit(
        f"Missing {_cjpeg_path}. Run: python tools/fetch_cjpeg.py --linux (or --windows)"
    )

# pillow-heif has no PyInstaller hook, so its libheif shared libraries have to
# be collected explicitly or HEIC support silently disappears from the binary.
_heif_binaries = []
_heif_hidden = []
try:
    import pillow_heif  # noqa: F401

    _heif_binaries = collect_dynamic_libs("pillow_heif")
    _heif_hidden = ["pillow_heif", "pillow_heif._pillow_heif", "_pillow_heif"]
except ImportError:
    print("minjpg.spec: pillow_heif not installed, building without HEIC support")

a = Analysis(
    ["main.py"],
    pathex=[str(spec_dir)],
    binaries=[(str(_cjpeg_path), "vendor")] + _heif_binaries,
    datas=[],
    hiddenimports=["PIL._tkinter_finder"] + _heif_hidden,
    excludes=["matplotlib", "pytest", "setuptools", "pandas", "scipy"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="minjpg",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
