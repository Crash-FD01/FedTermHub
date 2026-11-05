#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Interfaccia Qt (versione Windows): parti senza finestre e prova completa se c'è PySide6.

 - PasswordFeeder scrive la password una volta sola, solo al prompt giusto;
 - i comandi ssh/telnet non trasformano un host che comincia con "-" in un'opzione;
 - confronto delle versioni per l'avviso degli aggiornamenti;
 - con PySide6 installato: fedtermhub-qt --self-test (terminale, password, import, ssh).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("FEDTERMHUB_LANGUAGE", "it")

HAVE_QT = importlib.util.find_spec("PySide6") is not None


def check_feeder() -> None:
    from fedtermhub.qtui.connect import PasswordFeeder

    f = PasswordFeeder("s3cret")
    assert f.feed("The authenticity of host 'x' can't be established.\r\nAre you sure (yes/no)? ") is None
    assert f.feed("Enter passphrase for key 'id_ed25519': ") is None, "la passphrase della chiave non è la password"
    assert f.feed("\x1b[0madmin@host's password: ") == "s3cret\r"
    assert f.feed("admin@host's password: ") is None, "una volta sola"
    f = PasswordFeeder("s3cret")
    assert f.feed("Permission denied (publickey).\r\n") is None
    assert f.feed("password: ") is None, "dopo Permission denied non scrive più"
    assert PasswordFeeder(None).feed("password: ") is None


def check_commands() -> None:
    from fedtermhub import model
    from fedtermhub.qtui import connect

    settings = model.AppSettings()
    if connect.ssh_binary():
        sess = model.Session(name="x", protocol="ssh", host="-oProxyCommand=calc", user="u")
        argv = connect.ssh(sess, settings, "u", None).argv
        assert argv[-2:] == ["--", "u@-oProxyCommand=calc"], argv
        jump = model.Session(name="x", protocol="ssh", host="h", routing_mode="proxy_jump", jump_host="j",
                             jump_user="ju", jump_port="2222")
        argv = connect.ssh(jump, settings, "", "pw").argv
        assert "-J" in argv and argv[argv.index("-J") + 1] == "ju@j:2222", argv
        assert "NumberOfPasswordPrompts=1" in argv
    try:
        connect.ssh(model.Session(name="x", protocol="ssh", host="h", routing_mode="proxy_jump"), settings, "", None)
    except connect.LaunchError:
        pass
    else:
        if connect.ssh_binary():
            raise AssertionError("jump host vuoto accettato")


def check_versions() -> None:
    from fedtermhub.qtui import updates

    assert updates.newer("0.9.14", "0.9.13")
    assert updates.newer("v0.10.0", "0.9.13")
    assert not updates.newer("0.9.13", "0.9.13")
    assert not updates.newer("0.9.2", "0.9.13")


def check_self_test() -> None:
    report = Path(tempfile.mkdtemp()) / "report.json"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    env.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
    try:
        result = subprocess.run([sys.executable, "-m", "fedtermhub.qtui", "--self-test", str(report)], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=400)
    except subprocess.TimeoutExpired as exc:
        partial = report.read_text(encoding="utf-8") if report.is_file() else "(nessun risultato)"
        raise AssertionError(f"prova bloccata; risultato parziale:\n{partial}\n{exc.stderr or ''}") from exc
    assert report.is_file(), result.stdout + result.stderr
    data = json.loads(report.read_text(encoding="utf-8"))
    failed = [c for c in data.get("checks", []) if not c["ok"]]
    assert data["ok"] and not failed, json.dumps(data, indent=2, ensure_ascii=False)


def main() -> int:
    # connect.py non usa Qt: si prova sempre. Il resto solo dove PySide6 è installato (CI Windows).
    checks = [check_feeder, check_commands]
    if HAVE_QT:
        checks += [check_versions, check_self_test]
    else:
        print("PySide6 non installato: salto la prova dell'interfaccia")
    for check in checks:
        check()
        print(f"OK {check.__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
