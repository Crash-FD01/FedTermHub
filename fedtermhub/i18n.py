# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Lingua dell'interfaccia.

I testi sono scritti in italiano nel codice e passano da tr(); le traduzioni stanno in
fedtermhub/locale/<lingua>.po (formato gettext: si modificano con Poedit o un editor di testo).
Un testo senza traduzione resta in italiano: nessun errore, solo il testo originale.

La lingua si sceglie nelle Impostazioni (Automatica, Italiano, English) e vale dal riavvio
successivo: molti testi sono costanti create all'avvio. "Automatica" segue la lingua del sistema
(LANGUAGE, LC_ALL, LC_MESSAGES, LANG): italiano per it_*, inglese per tutto il resto.
FEDTERMHUB_LANGUAGE=it|en ha la precedenza su tutto (utile per i test e le schermate).

I testi composti usano segnaposto con nome, da riempire con .format():
    tr("Sessione aggiunta: {name}").format(name=sess.name)
cosi' chi traduce puo' spostarli dove la frase lo richiede.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, Optional

SUPPORTED = ("it", "en")
LOCALE_DIR = Path(__file__).resolve().parent / "locale"

LANGUAGE = "it"
_CATALOG: Dict[str, str] = {}
_REVERSE: Dict[str, str] = {}


def _configured_language() -> str:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    try:
        raw = json.loads((config_home / "fedtermhub" / "settings.json").read_text(encoding="utf-8"))
        return str(raw.get("language", "auto"))
    except Exception:
        return "auto"


def detect_language(setting: Optional[str] = None) -> str:
    """La lingua da usare: impostazione esplicita, altrimenti quella del sistema."""
    forced = os.environ.get("FEDTERMHUB_LANGUAGE", "").strip().lower()
    if forced in SUPPORTED:
        return forced
    choice = (setting if setting is not None else _configured_language()).strip().lower()
    if choice in SUPPORTED:
        return choice
    for variable in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(variable, "").strip()
        if not value:
            continue
        code = value.split(":")[0].split(".")[0].split("@")[0].split("_")[0].lower()
        if code in {"c", "posix"}:
            continue
        return "it" if code == "it" else "en"
    return "it"


_PO_STRING = re.compile(r'^(msgid|msgstr|msgctxt)\s+"(.*)"\s*$')
_PO_CONTINUATION = re.compile(r'^"(.*)"\s*$')


def _unescape(text: str) -> str:
    # Le stringhe .po usano gli escape del C: \\n \\t \\" \\\\
    return json.loads('"' + text.replace("\t", "\\t") + '"')


def parse_po(path: Path) -> Dict[str, str]:
    """Lettore minimo di file .po: msgid e msgstr su una o piu' righe, senza plurali.

    Le voci "fuzzy" (traduzione da ricontrollare) e quelle vuote vengono ignorate, come fa gettext.
    """
    catalog: Dict[str, str] = {}
    entry: Dict[str, str] = {}
    current = ""
    fuzzy = False

    def flush() -> None:
        nonlocal entry, fuzzy
        msgid, msgstr = entry.get("msgid"), entry.get("msgstr")
        if msgid and msgstr and not fuzzy:
            catalog[msgid] = msgstr
        entry, fuzzy = {}, False

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("#,") and "fuzzy" in line:
            fuzzy = True
            continue
        if line.startswith("#"):
            continue
        match = _PO_STRING.match(line)
        if match:
            if match.group(1) == "msgid" and "msgid" in entry:
                flush()
            current = match.group(1)
            entry[current] = _unescape(match.group(2))
            continue
        match = _PO_CONTINUATION.match(line)
        if match and current:
            entry[current] = entry.get(current, "") + _unescape(match.group(1))
    flush()
    return catalog


def setup(language: Optional[str] = None) -> str:
    """Carica il catalogo della lingua scelta e la restituisce."""
    global LANGUAGE, _CATALOG, _REVERSE
    LANGUAGE = detect_language(language)
    _CATALOG = {}
    if LANGUAGE != "it":
        po = LOCALE_DIR / f"{LANGUAGE}.po"
        if po.is_file():
            try:
                _CATALOG = parse_po(po)
            except Exception:
                _CATALOG = {}
    _REVERSE = {english: italian for italian, english in _CATALOG.items()}
    return LANGUAGE


def tr(text: str) -> str:
    """Il testo nella lingua dell'interfaccia (l'italiano originale se manca la traduzione)."""
    if not _CATALOG:
        return text
    return _CATALOG.get(text, text)


def original(text: str) -> str:
    """Il testo italiano da cui viene una traduzione (o il testo stesso).

    Serve dove il codice riconosce una voce dal suo testo, per esempio l'icona di una voce di menu:
    il riconoscimento resta sull'italiano qualunque sia la lingua.
    """
    return _REVERSE.get(text, text)


def trn(singular: str, plural: str, count: int) -> str:
    """Singolare o plurale secondo count (italiano e inglese hanno le stesse due forme)."""
    return tr(singular if count == 1 else plural)


setup()
