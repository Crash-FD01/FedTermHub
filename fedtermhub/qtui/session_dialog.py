# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Finestra di una sessione (nuova o da modificare) nell'interfaccia Qt.

Modifica gli stessi campi della versione GTK (model.Session): i campi che questa interfaccia non
mostra ancora restano come sono, così una sessione importata da Linux non perde nulla.
"""
from __future__ import annotations

import copy
import sys
from typing import List, Optional, Tuple

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import model
from ..i18n import tr

IS_WINDOWS = sys.platform == "win32"

# (protocollo, etichetta, disponibile in questa interfaccia)
PROTOCOLS: List[Tuple[str, str, bool]] = [
    ("ssh", "SSH", True),
    ("rdp", tr("Desktop remoto (RDP)"), True),
    ("telnet", "Telnet", True),
    ("local", tr("Terminale locale"), True),
    ("command", tr("Comando"), True),
    ("vnc", "VNC", False),
    ("sftp", "SFTP", False),
    ("serial", tr("Seriale"), False),
]
AVAILABLE = {proto for proto, _label, ok in PROTOCOLS if ok}
DEFAULT_PORTS = {"ssh": "22", "rdp": "3389", "telnet": "23", "vnc": "5900", "sftp": "22"}


class SessionDialog(QDialog):
    def __init__(self, parent: Optional[QWidget], session: Optional[model.Session],
                 credentials: List[Tuple[str, str]], vault_locked: bool) -> None:
        super().__init__(parent)
        self.is_new = session is None
        self.session = copy.deepcopy(session) if session else model.Session(name="", protocol="ssh")
        self.setWindowTitle(tr("Nuova sessione") if self.is_new else tr("Modifica: {name}").format(name=self.session.name))
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.name = QLineEdit(self.session.name)
        self.name.setPlaceholderText(tr("Nome mostrato nell'elenco delle sessioni"))
        self.protocol = QComboBox()
        for proto, label, ok in PROTOCOLS:
            self.protocol.addItem(label if ok else tr("{label} (non ancora su Windows)").format(label=label), proto)
        index = self.protocol.findData(self.session.protocol)
        self.protocol.setCurrentIndex(max(0, index))
        self.protocol.currentIndexChanged.connect(self._protocol_changed)
        self.host = QLineEdit(self.session.host)
        self.host.setPlaceholderText(tr("es. 192.168.1.10 oppure server.dominio.local"))
        self.port = QLineEdit(self.session.port)
        self.port.setMaximumWidth(90)
        host_row = QHBoxLayout()
        host_row.addWidget(self.host, 1)
        host_row.addWidget(QLabel(tr("Porta")))
        host_row.addWidget(self.port)
        self.user = QLineEdit(self.session.user)
        self.domain = QLineEdit(self.session.domain)
        self.domain.setPlaceholderText(tr("AZIENDA oppure vuoto"))
        self.credential = QComboBox()
        self.credential.addItem(tr("(nessuna credenziale collegata)"), "")
        for cid, label in credentials:
            self.credential.addItem(label, cid)
        if self.session.credential_id and self.credential.findData(self.session.credential_id) < 0:
            # archivio bloccato: si conserva il collegamento anche senza poterne mostrare il nome
            self.credential.addItem(tr("Credenziale salvata (sblocca le password per vederla)"), self.session.credential_id)
        self.credential.setCurrentIndex(max(0, self.credential.findData(self.session.credential_id)))
        if vault_locked:
            self.credential.setToolTip(tr("Sblocca il Password Manager per scegliere fra le credenziali salvate."))
        self.use_password = QCheckBox(tr("Usa la password salvata quando il protocollo lo permette"))
        self.use_password.setChecked(self.session.use_credential_password)
        self.command = QLineEdit(self.session.command)
        form.addRow(tr("Nome"), self.name)
        form.addRow(tr("Protocollo"), self.protocol)
        form.addRow(tr("Host"), host_row)
        form.addRow(tr("Utente"), self.user)
        self.domain_label = QLabel(tr("Dominio"))
        form.addRow(self.domain_label, self.domain)
        form.addRow(tr("Credenziale"), self.credential)
        form.addRow("", self.use_password)
        self.command_label = QLabel(tr("Comando"))
        form.addRow(self.command_label, self.command)
        self.host_widgets = [self.host, self.port, self.user, self.credential, self.use_password]
        layout.addLayout(form)

        # --- SSH: jump host
        self.ssh_box = QGroupBox(tr("Instradamento"))
        ssh_form = QFormLayout(self.ssh_box)
        self.use_jump = QCheckBox(tr("Passa da un jump host (ProxyJump)"))
        self.use_jump.setChecked(self.session.routing_mode == "proxy_jump")
        self.jump_host = QLineEdit(self.session.jump_host)
        self.jump_user = QLineEdit(self.session.jump_user)
        self.jump_port = QLineEdit(self.session.jump_port or "22")
        self.jump_port.setMaximumWidth(90)
        ssh_form.addRow(self.use_jump)
        ssh_form.addRow(tr("Jump host"), self.jump_host)
        ssh_form.addRow(tr("Utente"), self.jump_user)
        ssh_form.addRow(tr("Porta"), self.jump_port)
        layout.addWidget(self.ssh_box)

        # --- RDP
        self.rdp_box = QGroupBox(tr("Desktop remoto"))
        rdp_form = QFormLayout(self.rdp_box)
        self.rdp_fullscreen = QCheckBox(tr("Apri a schermo intero"))
        self.rdp_fullscreen.setChecked(self.session.rdp_start_fullscreen)
        self.rdp_admin = QCheckBox(tr("Sessione amministrativa (/admin)"))
        self.rdp_admin.setChecked(self.session.rdp_admin)
        self.rdp_clipboard = QCheckBox(tr("Appunti condivisi"))
        self.rdp_clipboard.setChecked(self.session.rdp_clipboard)
        self.rdp_printer = QCheckBox(tr("Stampanti"))
        self.rdp_printer.setChecked(self.session.rdp_printer)
        self.rdp_smartcard = QCheckBox(tr("Smart card"))
        self.rdp_smartcard.setChecked(self.session.rdp_smartcard)
        self.rdp_sound = QCheckBox(tr("Audio remoto sul PC locale"))
        self.rdp_sound.setChecked(self.session.rdp_sound)
        self.rdp_drive = QLineEdit(self.session.rdp_drive)
        self.rdp_drive.setPlaceholderText(tr("Nessuna cartella condivisa"))
        browse = QPushButton(tr("Sfoglia…"))
        browse.clicked.connect(self._browse_drive)
        drive_row = QHBoxLayout()
        drive_row.addWidget(self.rdp_drive, 1)
        drive_row.addWidget(browse)
        self.use_gateway = QCheckBox(tr("Passa da un RD Gateway"))
        self.use_gateway.setChecked(self.session.routing_mode == "rd_gateway")
        self.gateway_host = QLineEdit(self.session.rdp_gateway_host)
        self.gateway_host.setPlaceholderText(tr("Server RD Gateway"))
        for widget in (self.rdp_fullscreen, self.rdp_admin, self.rdp_clipboard, self.rdp_printer,
                       self.rdp_smartcard, self.rdp_sound):
            rdp_form.addRow(widget)
        rdp_form.addRow(tr("Cartella condivisa"), drive_row)
        rdp_form.addRow(self.use_gateway)
        rdp_form.addRow(tr("Gateway"), self.gateway_host)
        layout.addWidget(self.rdp_box)

        self.unavailable = QLabel("")
        self.unavailable.setWordWrap(True)
        self.unavailable.setProperty("role", "muted")
        layout.addWidget(self.unavailable)

        layout.addWidget(QLabel(tr("Note")))
        self.notes = QPlainTextEdit(self.session.notes)
        self.notes.setMaximumHeight(70)
        layout.addWidget(self.notes)

        self.error = QLabel("")
        self.error.setProperty("role", "danger")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._protocol_changed()
        self.name.setFocus()

    def _browse_drive(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("Scegli la cartella da condividere nella sessione RDP"))
        if folder:
            self.rdp_drive.setText(folder)

    def _protocol_changed(self) -> None:
        proto = self.protocol.currentData()
        remote = proto in {"ssh", "rdp", "telnet", "vnc", "sftp"}
        for widget in self.host_widgets:
            widget.setEnabled(remote or (proto == "serial" and widget in (self.host, self.port)))
        self.domain.setVisible(proto == "rdp")
        self.domain_label.setVisible(proto == "rdp")
        self.command.setVisible(proto in {"ssh", "command"})
        self.command_label.setVisible(proto in {"ssh", "command"})
        self.command.setPlaceholderText(
            tr("es. ping 8.8.8.8 oppure Get-Service") if proto == "command" else tr("Comando remoto facoltativo")
        )
        self.ssh_box.setVisible(proto == "ssh")
        self.rdp_box.setVisible(proto == "rdp")
        self.port.setPlaceholderText(DEFAULT_PORTS.get(proto, ""))
        if proto in AVAILABLE:
            self.unavailable.hide()
        else:
            self.unavailable.setText(tr("Questo tipo di sessione si apre per ora solo nella versione Linux di FedTermHub. "
                                        "Puoi comunque salvarla e modificarla qui."))
            self.unavailable.show()
        self.adjustSize()

    def _accept(self) -> None:
        candidate = self.result_session()
        problem = model.session_problem(candidate)
        if not candidate.name.strip():
            problem = tr("manca il nome della sessione")
        if problem:
            self.error.setText(tr("Correggi prima: ") + problem)
            self.error.show()
            return
        self.accept()

    def result_session(self) -> model.Session:
        sess = self.session
        sess.name = self.name.text().strip() or self.host.text().strip()
        sess.protocol = self.protocol.currentData()
        sess.host = self.host.text().strip()
        sess.port = self.port.text().strip()
        sess.user = self.user.text().strip()
        sess.domain = self.domain.text().strip()
        sess.credential_id = self.credential.currentData() or ""
        sess.use_credential_password = self.use_password.isChecked()
        sess.command = self.command.text().strip()
        sess.notes = self.notes.toPlainText().strip()
        if sess.protocol == "ssh":
            sess.routing_mode = "proxy_jump" if self.use_jump.isChecked() else (
                "direct" if sess.routing_mode in {"proxy_jump", "rd_gateway"} else sess.routing_mode)
            sess.jump_host = self.jump_host.text().strip()
            sess.jump_user = self.jump_user.text().strip()
            sess.jump_port = self.jump_port.text().strip() or "22"
        if sess.protocol == "rdp":
            sess.rdp_start_fullscreen = self.rdp_fullscreen.isChecked()
            sess.rdp_admin = self.rdp_admin.isChecked()
            sess.rdp_clipboard = self.rdp_clipboard.isChecked()
            sess.rdp_printer = self.rdp_printer.isChecked()
            sess.rdp_smartcard = self.rdp_smartcard.isChecked()
            sess.rdp_sound = self.rdp_sound.isChecked()
            sess.rdp_drive = self.rdp_drive.text().strip()
            sess.rdp_gateway_host = self.gateway_host.text().strip()
            sess.routing_mode = "rd_gateway" if self.use_gateway.isChecked() and sess.rdp_gateway_host else (
                "direct" if sess.routing_mode in {"proxy_jump", "rd_gateway"} else sess.routing_mode)
        return sess
