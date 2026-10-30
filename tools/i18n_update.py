#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Aggiorna i cataloghi delle traduzioni (fedtermhub/locale/*.po).

Raccoglie tutti i testi passati a tr() e trn() nei moduli di fedtermhub/ e riscrive ogni
catalogo .po: le traduzioni già presenti restano, i testi nuovi entrano con msgstr vuoto
(restano in italiano finché qualcuno non li traduce), quelli spariti dal codice vengono tolti.

    python3 tools/i18n_update.py            # aggiorna tutti i cataloghi
    python3 tools/i18n_update.py --check    # esce con 1 se un catalogo non è aggiornato (CI)

Un msgstr lasciato vuoto di proposito (comandi, nomi tecnici, marcatori di log) è normale.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "fedtermhub"
LOCALE_DIR = PACKAGE / "locale"
sys.path.insert(0, str(PACKAGE))

from i18n import parse_po  # noqa: E402

HEADER = """# Traduzione di FedTermHub.
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# File generato e aggiornato da tools/i18n_update.py: modifica solo le righe msgstr.
msgid ""
msgstr ""
"Project-Id-Version: FedTermHub\\n"
"Language: {language}\\n"
"MIME-Version: 1.0\\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Content-Transfer-Encoding: 8bit\\n"
"""


def extract() -> Dict[str, List[str]]:
    """{msgid: [file:riga, ...]} nell'ordine in cui compaiono nei moduli."""
    found: Dict[str, List[str]] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"tr", "trn"}:
                for arg in node.args[: 1 if node.func.id == "tr" else 2]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value:
                        found.setdefault(arg.value, []).append(f"{path.name}:{node.lineno}")
    return found


def po_msgids(path: Path) -> List[str]:
    """Tutti i msgid del catalogo, anche quelli ancora senza traduzione."""
    found = []
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("msgid "):
            current = json.loads(line[6:])
            continue
        if line.startswith('"') and current is not None:
            current += json.loads(line)
            continue
        if current:
            found.append(current)
        current = None
    if current:
        found.append(current)
    return found


def po_string(keyword: str, text: str) -> str:
    def quote(part: str) -> str:
        return json.dumps(part, ensure_ascii=False)

    if "\n" not in text.rstrip("\n"):
        return f"{keyword} {quote(text)}\n"
    lines = text.splitlines(keepends=True)
    return f'{keyword} ""\n' + "".join(quote(line) + "\n" for line in lines)


def render(language: str, messages: Dict[str, List[str]], catalog: Dict[str, str]) -> str:
    out = [HEADER.format(language=language)]
    for msgid, places in messages.items():
        out.append("\n")
        ordered = sorted(set(places), key=lambda place: (place.split(":")[0], int(place.split(":")[1])))
        out.append("#: " + " ".join(ordered[:4]) + "\n")
        out.append(po_string("msgid", msgid))
        out.append(po_string("msgstr", catalog.get(msgid, "")))
    return "".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="non scrive: segnala i cataloghi da aggiornare")
    parser.add_argument("--seed", type=Path, help="json {testo italiano: traduzione} da aggiungere (lingua unica)")
    args = parser.parse_args()

    messages = extract()
    stale = []
    for po in sorted(LOCALE_DIR.glob("*.po")):
        catalog = parse_po(po)
        if args.seed:
            seed = json.loads(args.seed.read_text(encoding="utf-8"))
            catalog.update({k: v for k, v in seed.items() if v})
        if args.check:
            # Solo l'elenco dei testi conta: i riferimenti file:riga cambiano a ogni modifica del codice.
            if set(po_msgids(po)) != set(messages):
                stale.append(po)
        else:
            po.write_text(render(po.stem, messages, catalog), encoding="utf-8")
        done = sum(1 for m in messages if catalog.get(m))
        print(f"{po.name}: {done}/{len(messages)} testi tradotti")
    if args.check and stale:
        print("Cataloghi da aggiornare con tools/i18n_update.py: " + ", ".join(p.name for p in stale))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
