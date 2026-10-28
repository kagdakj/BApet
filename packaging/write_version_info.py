"""Generate build/version_info.txt (Windows executable metadata) from bapet.__version__."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
version = re.search(r'__version__ = "([^"]+)"', (ROOT / "bapet" / "__init__.py").read_text()).group(1)
parts = (version.split(".") + ["0", "0", "0"])[:4]
nums = ", ".join(str(int(re.sub(r"\D.*", "", p) or 0)) for p in parts)

TEMPLATE = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({nums}), prodvers=({nums}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Blue Archive Desktop Pet'),
      StringStruct('FileDescription', 'Blue Archive Desktop Pet'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'BlueArchiveDesktopPet'),
      StringStruct('LegalCopyright', 'MIT License (application code)'),
      StringStruct('OriginalFilename', 'BlueArchiveDesktopPet.exe'),
      StringStruct('ProductName', 'Blue Archive Desktop Pet'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""

out = ROOT / "build" / "version_info.txt"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(TEMPLATE, encoding="utf-8")
print(f"wrote {out} ({version})")
