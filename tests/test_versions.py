#!/usr/bin/python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""La versione deve essere la stessa ovunque compaia.

Si scrive a mano in cinque punti: lo spec (Version e la prima voce del
%changelog), APP_VERSION nel programma (titolo della finestra e doctor),
install.sh e l'ultima <release> del metainfo (quella che mostrano GNOME
Software e Flathub). Il workflow di rilascio confrontava solo il tag con lo
spec: dimenticare uno degli altri pubblicava una versione che si presentava
con il numero vecchio.

    python3 tests/test_versions.py            controlla i file
    python3 tests/test_versions.py v0.9.07    controlla anche il tag

Non richiede GTK.
"""
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def one(pattern: str, path: str, flags: int = re.M) -> str:
    match = re.search(pattern, (ROOT / path).read_text(encoding="utf-8"), flags)
    if not match:
        raise SystemExit(f"{path}: non trovo {pattern!r}")
    return match.group(1)


def main() -> int:
    spec = one(r"^Version:\s*(\S+)", "fedtermhub.spec")
    found = {
        "fedtermhub.spec  Version": spec,
        "fedtermhub.spec  prima voce di %changelog": one(
            r"^%changelog\s*\n\*[^\n]*?-\s*(?:\d+:)?([0-9][^\s-]*)-\S+\s*$", "fedtermhub.spec"),
        "fedtermhub/fedtermhub.py  APP_VERSION": one(r'^APP_VERSION = "([^"]+)"', "fedtermhub/fedtermhub.py"),
        "install.sh  APP_VERSION": one(r'^APP_VERSION="([^"]+)"', "install.sh"),
    }
    metainfo = ROOT / "packaging/io.github.crash_fd01.fedtermhub.metainfo.xml"
    first_release = ET.parse(metainfo).getroot().find("releases/release")
    found["metainfo  ultima <release>"] = first_release.get("version") if first_release is not None else "(manca)"
    if len(sys.argv) > 1:
        found["tag"] = sys.argv[1].removeprefix("v")

    wrong = {where: value for where, value in found.items() if value != spec}
    for where, value in found.items():
        print(f"  {'OK ' if value == spec else 'ERR'} {value:<10} {where}")
    if wrong:
        print(f"\nVersioni diverse da quella dello spec ({spec}): allinea i file indicati con ERR.")
        return 1
    print(f"\nVersione {spec} coerente in tutti i file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
