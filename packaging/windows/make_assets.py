# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Icona .ico e informazioni di versione dell'eseguibile per la build di Windows.

    python packaging/windows/make_assets.py <cartella di uscita>

Stampa la versione (da fedtermhub/model.py, come per i pacchetti Linux).
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ICON = os.path.join(ROOT, "share", "icons", "hicolor", "256x256", "apps", "io.github.crash_fd01.fedtermhub.png")


def version() -> str:
    text = open(os.path.join(ROOT, "fedtermhub", "model.py"), encoding="utf-8").read()
    return re.search(r'^APP_VERSION = "([^"]+)"', text, re.M).group(1)


def main() -> None:
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    from PIL import Image

    big = Image.open(ICON).convert("RGBA")
    big.save(os.path.join(out, "fedtermhub.ico"), sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    v = version()
    nums = (list(map(int, re.findall(r"\d+", v)[:3])) + [0, 0, 0])[:3] + [0]
    tup = ", ".join(map(str, nums))
    with open(os.path.join(out, "version.txt"), "w", encoding="utf-8") as f:
        f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({tup}), prodvers=({tup}), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Crash'),
      StringStruct('FileDescription', 'FedTermHub'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', 'FedTermHub'),
      StringStruct('LegalCopyright', 'Copyright (C) 2026 Crash - GPL-3.0-or-later'),
      StringStruct('OriginalFilename', 'FedTermHub.exe'),
      StringStruct('ProductName', 'FedTermHub'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")
    print(v)


if __name__ == "__main__":
    main()
