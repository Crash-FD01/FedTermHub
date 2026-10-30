#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Lingua dell'interfaccia: scelta della lingua, catalogo inglese e avvio in inglese.

Controlla che:
 - la lingua segua la regola documentata (FEDTERMHUB_LANGUAGE, impostazione, sistema);
 - fedtermhub/locale/en.po contenga tutti i testi del codice (tools/i18n_update.py --check);
 - ogni traduzione mantenga gli stessi segnaposto {nome} del testo italiano, altrimenti
   .format() fallirebbe solo in inglese;
 - le etichette con mnemonico (_Salva) lo mantengano anche tradotte;
 - l'impostazione "language" venga letta e salvata correttamente;
 - il programma si avvii davvero in inglese (sotto Xvfb, se disponibile).
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = PROJECT_ROOT / "fedtermhub"
sys.path.insert(0, str(PACKAGE))

import i18n  # noqa: E402

PLACEHOLDER = re.compile(r"\{([A-Za-z_]\w*)(?:[:!][^{}]*)?\}")


def check_detection() -> None:
    saved = {k: os.environ.get(k) for k in ("FEDTERMHUB_LANGUAGE", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")}
    try:
        for key in saved:
            os.environ.pop(key, None)
        assert i18n.detect_language("auto") == "it", "senza lingua di sistema resta l'italiano"
        os.environ["LANG"] = "C.UTF-8"
        assert i18n.detect_language("auto") == "it", "C/POSIX non indica una lingua"
        os.environ["LANG"] = "it_IT.UTF-8"
        assert i18n.detect_language("auto") == "it"
        os.environ["LANG"] = "en_US.UTF-8"
        assert i18n.detect_language("auto") == "en"
        os.environ["LANG"] = "de_DE.UTF-8"
        assert i18n.detect_language("auto") == "en", "lingue senza catalogo: inglese"
        os.environ["LANGUAGE"] = "it:en"
        assert i18n.detect_language("auto") == "it", "LANGUAGE ha la precedenza su LANG"
        assert i18n.detect_language("en") == "en", "la scelta nelle Impostazioni vince sul sistema"
        assert i18n.detect_language("xx") == "it", "valore sconosciuto: si torna alla lingua del sistema"
        os.environ["FEDTERMHUB_LANGUAGE"] = "en"
        assert i18n.detect_language("it") == "en", "FEDTERMHUB_LANGUAGE vince su tutto"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def check_catalog() -> dict:
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "tools" / "i18n_update.py"), "--check"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, "catalogo non aggiornato: esegui tools/i18n_update.py\n" + result.stdout + result.stderr

    po = PACKAGE / "locale" / "en.po"
    catalog = i18n.parse_po(po)
    assert len(catalog) > 1500, f"catalogo inglese quasi vuoto: {len(catalog)} voci"

    errors = []
    for msgid, msgstr in catalog.items():
        if sorted(PLACEHOLDER.findall(msgid)) != sorted(PLACEHOLDER.findall(msgstr)):
            errors.append(f"segnaposto diversi: {msgid!r} -> {msgstr!r}")
        if msgid.endswith("\n") != msgstr.endswith("\n"):
            errors.append(f"a capo finale diverso: {msgid!r}")
    assert not errors, "\n".join(errors[:20])
    return catalog


def check_mnemonics(catalog: dict) -> None:
    """Un'etichetta con _ resta con il suo mnemonico anche in inglese."""
    missing = []
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in {"new_with_mnemonic", "add_button"}:
                continue
            for arg in node.args[:1]:
                if isinstance(arg, ast.Call) and getattr(arg.func, "id", "") == "tr" and isinstance(arg.args[0], ast.Constant):
                    text = arg.args[0].value
                    if re.search(r"_\w", text) and catalog.get(text) and not re.search(r"_\w", catalog[text]):
                        missing.append(f"{path.name}:{node.lineno} {text!r} -> {catalog[text]!r}")
    assert not missing, "mnemonico perso nella traduzione:\n" + "\n".join(missing)


def check_setting() -> None:
    with tempfile.TemporaryDirectory() as temp:
        env = dict(os.environ, XDG_CONFIG_HOME=temp, FEDTERMHUB_LANGUAGE="it")
        env.pop("DISPLAY", None)
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from pathlib import Path\n"
            "import fedtermhub as app\n"
            "p = Path(%r) / 'settings.json'\n"
            "s = app.AppSettings(); s.language = 'en'; s.save(p)\n"
            "assert app.AppSettings.load(p).language == 'en'\n"
            "p.write_text('{\"language\": \"klingon\"}')\n"
            "assert app.AppSettings.load(p).language == 'auto'\n"
            "print('ok')\n"
        ) % (str(PACKAGE), temp)
        result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False)
        assert result.returncode == 0 and "ok" in result.stdout, result.stdout + result.stderr

        # La lingua scelta nelle Impostazioni viene letta all'avvio, prima di creare l'interfaccia.
        (Path(temp) / "fedtermhub").mkdir(exist_ok=True)
        (Path(temp) / "fedtermhub" / "settings.json").write_text(json.dumps({"language": "en"}))
        env.pop("FEDTERMHUB_LANGUAGE")
        env["LANG"] = "it_IT.UTF-8"
        script = "import sys; sys.path.insert(0, %r)\nimport i18n\nprint(i18n.LANGUAGE, i18n.tr('Impostazioni'))\n" % str(PACKAGE)
        result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False)
        assert result.stdout.strip() == "en Settings", result.stdout + result.stderr


def check_english_start() -> None:
    """L'applicazione vera parte in inglese e mostra i menu tradotti."""
    if not os.environ.get("DISPLAY"):
        print("SALTATO — avvio in inglese: nessun DISPLAY (usa xvfb-run)")
        return
    with tempfile.TemporaryDirectory() as temp:
        env = dict(os.environ, XDG_CONFIG_HOME=str(Path(temp) / "config"),
                   XDG_CACHE_HOME=str(Path(temp) / "cache"), FEDTERMHUB_LANGUAGE="en")
        script = r'''
import sys
sys.path.insert(0, %r)
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
import fedtermhub as app
application = Gtk.Application(application_id="io.github.crash_fd01.fedtermhub.I18nTest")
assert application.register(None)
win = app.AppWindow(application)
win.show_all()
labels = [item.get_label() or "" for item in win.menubar.get_children()]
print("|".join(labels))
win.destroy()
''' % str(PACKAGE)
        result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True,
                                check=False, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        menus = result.stdout.strip().splitlines()[-1].split("|")
        for expected in ("Terminal", "Sessions", "Tools", "View", "Help"):
            assert expected in menus, f"menu inglese {expected!r} mancante: {menus}"
        for italian in ("Sessioni", "Strumenti", "Vista", "Aiuto"):
            assert italian not in menus, f"menu ancora in italiano: {italian}"


def main() -> int:
    check_detection()
    catalog = check_catalog()
    check_mnemonics(catalog)
    check_setting()
    check_english_start()
    print(f"OK — lingua: rilevamento, catalogo inglese ({len(catalog)} testi), segnaposto, mnemonici e avvio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
