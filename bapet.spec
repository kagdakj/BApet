# PyInstaller spec for Blue Archive Desktop Pet (Linux + Windows).
#   pyinstaller --noconfirm bapet.spec
# Produces a single-file executable in dist/.
import os
import re
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(SPECPATH)
IS_WIN = sys.platform == "win32"
NAME = "BlueArchiveDesktopPet"

with open(os.path.join(ROOT, "bapet", "__init__.py"), encoding="utf-8") as fh:
    VERSION = re.search(r'__version__ = "([^"]+)"', fh.read()).group(1)

datas = [
    (os.path.join(ROOT, "bapet", "catalog.json"), "bapet"),
    (os.path.join(ROOT, "bapet", "assets"), os.path.join("bapet", "assets")),
]

# moderngl picks its GL loader (glcontext.x11 / egl / wgl) at runtime.
hiddenimports = collect_submodules("glcontext") + ["moderngl", "_moderngl"]
binaries = collect_dynamic_libs("glcontext")

excludes = [
    "tkinter", "unittest", "pydoc", "doctest", "pytest",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtPdf",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtXml", "PySide6.QtConcurrent",
    "PySide6.QtDBus", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtSvg",
    "PySide6.QtPrintSupport", "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools",
]

if IS_WIN:
    import glob

    import PySide6

    pyside_dir = os.path.dirname(PySide6.__file__)
    # Mesa software OpenGL fallback for machines without OpenGL 3.3 drivers.
    sw = os.path.join(pyside_dir, "opengl32sw.dll")
    if os.path.exists(sw):
        binaries.append((sw, "."))
    # Collect the Qt plugins explicitly. PyInstaller's PySide6 hook finds them by
    # importing QtCore at build time, which fails when cross-building under Wine
    # (no system ICU), silently producing an exe without a platform plugin.
    # The PySide6 runtime hook points QT_PLUGIN_PATH at <bundle>/PySide6/plugins.
    for sub, pattern in (
        ("platforms", "qwindows.dll"),
        ("styles", "*.dll"),
        ("imageformats", "q[gij]*.dll"),  # gif, ico, jpeg (png is built into QtGui)
    ):
        for dll in glob.glob(os.path.join(pyside_dir, "plugins", sub, pattern)):
            binaries.append((dll, os.path.join("PySide6", "plugins", sub)))

a = Analysis(
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
    optimize=1,
)

# Drop Qt translations and unused plugin families to keep the binary small.
def _keep(entry):
    dest = entry[0].replace("\\", "/")
    if "/translations/" in dest:
        return False
    for bad in ("/qml/", "plugins/sqldrivers", "plugins/multimedia", "plugins/designer",
                "plugins/printsupport", "plugins/qmltooling", "plugins/position"):
        if bad in dest:
            return False
    return True

a.datas = [d for d in a.datas if _keep(d)]
a.binaries = [b for b in a.binaries if _keep(b)]

# Refuse to produce an exe that cannot open a window.
_platform_plugin = "qwindows.dll" if IS_WIN else "libqxcb.so"
if not any(os.path.basename(b[0]) == _platform_plugin for b in a.binaries):
    raise SystemExit(f"Qt platform plugin {_platform_plugin} was not collected; aborting build.")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=NAME,
    debug=False,
    strip=not IS_WIN,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(ROOT, "bapet", "assets", "icon.ico"),
    version=os.path.join(ROOT, "build", "version_info.txt") if IS_WIN else None,
)
