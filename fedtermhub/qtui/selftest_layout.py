# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tutte le finestre del programma, aperte una alla volta con dati inventati, per controllarne il layout.

Lo usano il self-test (un controllo «Layout delle finestre») e tools/audit_qt.py (schermate di ogni finestra).
Nessuna finestra è modale e niente tocca le sessioni o le password dell'utente.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from PySide6.QtWidgets import QApplication, QTabWidget, QWidget

from .. import model
from . import layout_check
from .chrome import FolderDialog, LogDialog, MultiExecDialog, ShortcutsDialog
from .net_tools import NetworkToolsDialog
from .session_dialog import PROTOCOLS, SessionDialog
from .settings_dialog import SettingsDialog
from .tunnels import TunnelEditDialog, TunnelsDialog
from .vault_dialogs import CredentialDialog, MasterPasswordDialog, VaultManagerDialog

Scene = Tuple[str, Callable[[], QWidget]]

CREDENTIALS = [("c1", "Studio Rossi — admin"), ("c2", "Officine Bianchi — Administrator (dominio EXAMPLE)")]
TUNNELS = [("t1", "RDP Studio Rossi (127.0.0.1:13389)")]


def _demo_session(protocol: str) -> model.Session:
    sess = model.Session(name=f"prova-{protocol}", protocol=protocol, host="server01.example.lan", user="admin")
    if protocol == "serial":
        sess.host, sess.port = "COM3", "115200"
    if protocol in {"local", "command"}:
        sess.host = ""
    return sess


def _demo_vault() -> model.PasswordVault:
    vault = model.PasswordVault(Path(tempfile.mkdtemp()) / "vault.json")
    vault.create("prova-layout")
    vault.credentials += [
        model.Credential(name="Studio Rossi — admin", username="admin", password="x", group="Studio Rossi",
                         notes="Firewall e switch."),
        model.Credential(name="Officine Bianchi — Administrator", username="Administrator", domain="EXAMPLE",
                         password="x", group="Officine Bianchi"),
        model.Credential(name="Proxmox root", username="root", password="x"),
    ]
    return vault


def _demo_tunnels() -> List[model.TunnelSession]:
    return [
        model.TunnelSession(name="RDP Studio Rossi", ssh_host="bastion.example.lan", ssh_user="admin",
                            local_port="13389", remote_host="10.20.0.10", remote_port="3389"),
        model.TunnelSession(name="Proxy di manutenzione", tunnel_type="dynamic", ssh_host="bastion.example.lan",
                            ssh_user="admin", local_port="1080", auto_start=True),
    ]


def scenes(window) -> Iterator[Scene]:  # noqa: ANN001
    """(nome, funzione che crea la finestra). La finestra principale va per prima e non si chiude."""
    yield "principale", lambda: window
    for protocol, _label, _ok in PROTOCOLS:
        yield f"sessione-{protocol}", lambda p=protocol: SessionDialog(window, _demo_session(p), CREDENTIALS, False, TUNNELS)
    yield "sessione-nuova", lambda: SessionDialog(window, None, CREDENTIALS, True, TUNNELS)
    yield "impostazioni", lambda: SettingsDialog(window, window.settings)
    yield "cartella", lambda: FolderDialog(window, "Modifica cartella", "Studio Rossi", "#56c7d9")
    yield "log", lambda: LogDialog(window)
    yield "scorciatoie", lambda: ShortcutsDialog(window, window.shortcut_rows())
    yield "multiexec", lambda: MultiExecDialog(window, [
        (None, "web01", "SSH", True), (None, "db01", "SSH", True), (None, "Terminale locale", "terminale locale", False)])
    for mode in ("create", "unlock", "export", "import"):
        yield f"password-{mode}", lambda m=mode: MasterPasswordDialog(window, m)
    yield "credenziale", lambda: CredentialDialog(window, _demo_vault().credentials[0], ["Studio Rossi", "Officine Bianchi"])
    yield "password-manager", lambda: VaultManagerDialog(window, _demo_vault(), window.settings, lambda _cid: ["web01"],
                                                         lambda: None)
    yield "tunnel", lambda: _tunnels_dialog(window)
    yield "tunnel-editor", lambda: TunnelEditDialog(window, _demo_tunnels()[0], CREDENTIALS, window.settings,
                                                    window.tunnels.identity)
    yield "strumenti-di-rete", lambda: NetworkToolsDialog(window, lambda *_a: None, lambda *_a: None)


def _tunnels_dialog(window) -> TunnelsDialog:  # noqa: ANN001
    store = window.tunnels.store
    saved = list(store.tunnels)
    store.tunnels[:] = _demo_tunnels()
    dialog = TunnelsDialog(window, window.tunnels, lambda: CREDENTIALS, window)
    dialog._restore = lambda: store.tunnels.__setitem__(slice(None), saved)  # i tunnel veri tornano alla chiusura
    return dialog


def run(app: QApplication, window, wait: Callable, shots: Optional[Path] = None) -> Dict[str, List[str]]:  # noqa: ANN001
    """Apre ogni finestra, ne controlla il layout e, se `shots` è una cartella, la fotografa.

    Restituisce {nome della finestra: difetti}; solo le finestre con difetti.
    """
    report: Dict[str, List[str]] = {}

    def look(name: str, widget: QWidget) -> None:
        wait(app, lambda: False, 0.25)
        found = layout_check.problems(widget)
        if found:
            report[name] = found
        if shots is not None:
            widget.grab().save(str(shots / f"layout-{name}.png"))

    for name, create in scenes(window):
        widget = create()
        if widget is not window:
            widget.setModal(False)
            widget.show()
        tabs = widget.findChildren(QTabWidget) if widget is not window else []
        if tabs:
            for index in range(tabs[0].count()):
                tabs[0].setCurrentIndex(index)
                look(f"{name}-{index + 1}", widget)
        else:
            look(name, widget)
        if widget is not window:
            restore = getattr(widget, "_restore", None)
            widget.close()
            if restore:
                restore()
            widget.deleteLater()
            wait(app, lambda: False, 0.05)
    return report
