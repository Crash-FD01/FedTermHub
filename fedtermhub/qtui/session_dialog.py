# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Finestra di una sessione (nuova o da modificare) nell'interfaccia Qt.

Modifica gli stessi campi della versione GTK (model.Session): i campi che questa interfaccia non
mostra restano come sono, così una sessione importata da Linux non perde nulla.
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
    ("vnc", "VNC", True),
    ("sftp", tr("SFTP (file remoti)"), True),
    ("telnet", "Telnet", True),
    ("serial", tr("Seriale"), True),
    ("local", tr("Terminale locale"), True),
    ("command", tr("Comando"), True),
]
AVAILABLE = {proto for proto, _label, ok in PROTOCOLS if ok}
DEFAULT_PORTS = {"ssh": "22", "rdp": "3389", "telnet": "23", "vnc": "5900", "sftp": "22"}
REMOTE = {"ssh", "rdp", "telnet", "vnc", "sftp"}
BAUD_RATES = ["9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"]


class SessionDialog(QDialog):
    def __init__(self, parent: Optional[QWidget], session: Optional[model.Session],
                 credentials: List[Tuple[str, str]], vault_locked: bool,
                 tunnels: Optional[List[Tuple[str, str]]] = None) -> None:
        super().__init__(parent)
        self.is_new = session is None
        self.session = copy.deepcopy(session) if session else model.Session(name="", protocol="ssh")
        self.setWindowTitle(tr("Nuova sessione") if self.is_new else tr("Modifica: {name}").format(name=self.session.name))
        self.setMinimumWidth(580)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.name = QLineEdit(self.session.name)
        self.name.setPlaceholderText(tr("Nome mostrato nell'elenco delle sessioni"))
        self.protocol = QComboBox()
        for proto, label, _ok in PROTOCOLS:
            self.protocol.addItem(label, proto)
        index = self.protocol.findData(self.session.protocol)
        self.protocol.setCurrentIndex(max(0, index))
        self.protocol.currentIndexChanged.connect(self._protocol_changed)
        self.host = QLineEdit(self.session.host if self.session.protocol != "serial" else "")
        self.host.setPlaceholderText(tr("es. 192.168.1.10 oppure server.dominio.local"))
        self.port = QLineEdit(self.session.port if self.session.protocol != "serial" else "")
        self.port.setMaximumWidth(90)
        self.host_row = QWidget()
        host_row = QHBoxLayout(self.host_row)
        host_row.setContentsMargins(0, 0, 0, 0)
        host_row.addWidget(self.host, 1)
        self.port_label = QLabel(tr("Porta"))
        host_row.addWidget(self.port_label)
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
        self.host_label = QLabel(tr("Host"))
        form.addRow(self.host_label, self.host_row)
        self.user_label = QLabel(tr("Utente"))
        form.addRow(self.user_label, self.user)
        self.domain_label = QLabel(tr("Dominio"))
        form.addRow(self.domain_label, self.domain)
        self.credential_label = QLabel(tr("Credenziale"))
        form.addRow(self.credential_label, self.credential)
        form.addRow("", self.use_password)
        self.command_label = QLabel(tr("Comando"))
        form.addRow(self.command_label, self.command)
        layout.addLayout(form)

        # --- seriale
        self.serial_box = QGroupBox(tr("Porta seriale"))
        serial_form = QFormLayout(self.serial_box)
        self.serial_port = QComboBox()
        self.serial_port.setEditable(True)
        refresh = QPushButton(tr("Aggiorna"))
        refresh.clicked.connect(self._refresh_serial_ports)
        serial_row = QHBoxLayout()
        serial_row.addWidget(self.serial_port, 1)
        serial_row.addWidget(refresh)
        self.serial_baud = QComboBox()
        self.serial_baud.setEditable(True)
        self.serial_baud.addItems(BAUD_RATES)
        self.serial_bits = QComboBox()
        self.serial_bits.addItems(["8", "7", "6", "5"])
        self.serial_parity = QComboBox()
        for code, label in (("N", tr("Nessuna")), ("E", tr("Pari")), ("O", tr("Dispari")), ("M", "Mark"), ("S", "Space")):
            self.serial_parity.addItem(label, code)
        self.serial_stop = QComboBox()
        self.serial_stop.addItems(["1", "1.5", "2"])
        serial_form.addRow(tr("Porta"), serial_row)
        serial_form.addRow(tr("Velocità (baud)"), self.serial_baud)
        serial_form.addRow(tr("Bit di dati"), self.serial_bits)
        serial_form.addRow(tr("Parità"), self.serial_parity)
        serial_form.addRow(tr("Bit di stop"), self.serial_stop)
        if self.session.protocol == "serial":
            self.serial_baud.setCurrentText(self.session.port or "115200")
        else:
            self.serial_baud.setCurrentText("115200")
        self.serial_bits.setCurrentText(self.session.serial_databits or "8")
        self.serial_parity.setCurrentIndex(max(0, self.serial_parity.findData(self.session.serial_parity or "N")))
        self.serial_stop.setCurrentText(self.session.serial_stopbits or "1")
        self._refresh_serial_ports()
        if self.session.protocol == "serial" and self.session.host:
            self.serial_port.setCurrentText(self.session.host)
        layout.addWidget(self.serial_box)

        # --- instradamento: jump host (SSH/SFTP) o tunnel salvato
        self.route_box = QGroupBox(tr("Instradamento"))
        route_form = QFormLayout(self.route_box)
        self.route = QComboBox()
        self.route.addItem(tr("Diretto"), "direct")
        self.route.addItem(tr("Passa da un jump host (ProxyJump)"), "proxy_jump")
        self.route.addItem(tr("Prima avvia un tunnel salvato"), "saved_tunnel")
        self.route.addItem(tr("Passa da un RD Gateway"), "rd_gateway")
        self.jump_host = QLineEdit(self.session.jump_host)
        self.jump_user = QLineEdit(self.session.jump_user)
        self.jump_port = QLineEdit(self.session.jump_port or "22")
        self.jump_port.setMaximumWidth(90)
        self.tunnel = QComboBox()
        for tid, label in tunnels or []:
            self.tunnel.addItem(label, tid)
        if self.session.required_tunnel_id and self.tunnel.findData(self.session.required_tunnel_id) < 0:
            self.tunnel.addItem(tr("Tunnel non trovato"), self.session.required_tunnel_id)
        self.tunnel.setCurrentIndex(max(0, self.tunnel.findData(self.session.required_tunnel_id)))
        self.gateway_host = QLineEdit(self.session.rdp_gateway_host)
        self.gateway_host.setPlaceholderText(tr("Server RD Gateway"))
        route_form.addRow(tr("Percorso"), self.route)
        self.jump_rows = [(QLabel(tr("Jump host")), self.jump_host), (QLabel(tr("Utente")), self.jump_user),
                          (QLabel(tr("Porta")), self.jump_port)]
        for label, widget in self.jump_rows:
            route_form.addRow(label, widget)
        self.tunnel_label = QLabel(tr("Tunnel"))
        route_form.addRow(self.tunnel_label, self.tunnel)
        self.gateway_label = QLabel(tr("Gateway"))
        route_form.addRow(self.gateway_label, self.gateway_host)
        self.route.setCurrentIndex(max(0, self.route.findData(self.session.routing_mode)))
        self.route.currentIndexChanged.connect(self._route_changed)
        layout.addWidget(self.route_box)

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
        for widget in (self.rdp_fullscreen, self.rdp_admin, self.rdp_clipboard, self.rdp_printer,
                       self.rdp_smartcard, self.rdp_sound):
            rdp_form.addRow(widget)
        rdp_form.addRow(tr("Cartella condivisa"), drive_row)
        layout.addWidget(self.rdp_box)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setProperty("role", "muted")
        layout.addWidget(self.hint)

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

    # ------------------------------------------------------------------ comportamento
    def _refresh_serial_ports(self) -> None:
        from .stream_process import list_serial_ports

        current = self.serial_port.currentText()
        self.serial_port.clear()
        for device, description in list_serial_ports():
            self.serial_port.addItem(f"{device} — {description}" if description else device, device)
        if current:
            self.serial_port.setCurrentText(current)
        elif self.serial_port.count() == 0:
            self.serial_port.setCurrentText("COM1" if IS_WINDOWS else "/dev/ttyUSB0")

    def _serial_device(self) -> str:
        text = self.serial_port.currentText().strip()
        index = self.serial_port.findText(text)
        if index >= 0 and self.serial_port.itemData(index):
            return str(self.serial_port.itemData(index))
        return text.split(" — ")[0].strip()

    def _browse_drive(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("Scegli la cartella da condividere nella sessione RDP"))
        if folder:
            self.rdp_drive.setText(folder)

    def _route_options(self, proto: str) -> List[str]:
        options = ["direct"]
        if proto in {"ssh", "sftp"}:
            options.append("proxy_jump")
        if proto in REMOTE:
            options.append("saved_tunnel")
        if proto == "rdp":
            options.append("rd_gateway")
        return options

    def _route_changed(self) -> None:
        mode = self.route.currentData()
        for label, widget in self.jump_rows:
            label.setVisible(mode == "proxy_jump")
            widget.setVisible(mode == "proxy_jump")
        self.tunnel_label.setVisible(mode == "saved_tunnel")
        self.tunnel.setVisible(mode == "saved_tunnel")
        self.gateway_label.setVisible(mode == "rd_gateway")
        self.gateway_host.setVisible(mode == "rd_gateway")
        if mode == "saved_tunnel" and self.tunnel.count() == 0:
            self.hint.setText(tr("Non ci sono tunnel salvati: creane uno in Strumenti › Tunnel SSH."))
            self.hint.show()

    def _protocol_changed(self) -> None:
        proto = self.protocol.currentData()
        remote = proto in REMOTE
        for widget in (self.host_label, self.host_row, self.user_label, self.user, self.credential_label,
                       self.credential, self.use_password):
            widget.setVisible(remote)
        self.domain.setVisible(proto == "rdp")
        self.domain_label.setVisible(proto == "rdp")
        self.command.setVisible(proto in {"ssh", "command"})
        self.command_label.setVisible(proto in {"ssh", "command"})
        self.command.setPlaceholderText(
            tr("es. ping 8.8.8.8 oppure Get-Service") if proto == "command" else tr("Comando remoto facoltativo")
        )
        self.port.setPlaceholderText(DEFAULT_PORTS.get(proto, ""))
        if proto == "vnc":
            self.port_label.setText(tr("Porta o display"))
            self.user_label.setText(tr("Utente (se il server lo chiede)"))
        else:
            self.port_label.setText(tr("Porta"))
            self.user_label.setText(tr("Utente"))
        self.serial_box.setVisible(proto == "serial")
        self.rdp_box.setVisible(proto == "rdp")
        # Percorsi possibili per il protocollo
        options = self._route_options(proto)
        current = self.route.currentData()
        view = self.route.view()
        for i in range(self.route.count()):
            view.setRowHidden(i, self.route.itemData(i) not in options)
        if current not in options:
            self.route.setCurrentIndex(self.route.findData("direct"))
        self.route_box.setVisible(len(options) > 1)
        self._route_changed()
        hints = {
            "sftp": tr("Si apre un pannello per sfogliare, scaricare e caricare file. Per le sessioni SSH lo stesso "
                       "pannello si apre accanto al terminale."),
            "vnc": tr("Il desktop VNC si apre nella scheda; la password VNC si prende dalla credenziale collegata."),
            "telnet": tr("Telnet non è cifrato: usalo solo per apparati che non hanno SSH."),
        }
        if proto in hints:
            self.hint.setText(hints[proto])
            self.hint.show()
        elif self.route.currentData() != "saved_tunnel":
            self.hint.hide()
        self.adjustSize()

    def _accept(self) -> None:
        candidate = self.result_session()
        problem = model.session_problem(candidate)
        if not candidate.name.strip():
            problem = tr("manca il nome della sessione")
        if candidate.routing_mode == "saved_tunnel" and not candidate.required_tunnel_id:
            problem = tr("scegli il tunnel da avviare prima della connessione")
        if problem:
            self.error.setText(tr("Correggi prima: ") + problem)
            self.error.show()
            return
        self.accept()

    def result_session(self) -> model.Session:
        sess = self.session
        sess.protocol = self.protocol.currentData()
        sess.notes = self.notes.toPlainText().strip()
        sess.command = self.command.text().strip()
        if sess.protocol == "serial":
            sess.host = self._serial_device()
            sess.port = self.serial_baud.currentText().strip()
            sess.serial_databits = self.serial_bits.currentText()
            sess.serial_parity = self.serial_parity.currentData()
            sess.serial_stopbits = self.serial_stop.currentText()
            sess.name = self.name.text().strip() or sess.host
            return sess
        sess.name = self.name.text().strip() or self.host.text().strip()
        if sess.protocol in REMOTE:
            sess.host = self.host.text().strip()
            sess.port = self.port.text().strip()
            sess.user = self.user.text().strip()
            sess.credential_id = self.credential.currentData() or ""
            sess.use_credential_password = self.use_password.isChecked()
        if sess.protocol == "rdp":
            sess.domain = self.domain.text().strip()
            sess.rdp_start_fullscreen = self.rdp_fullscreen.isChecked()
            sess.rdp_admin = self.rdp_admin.isChecked()
            sess.rdp_clipboard = self.rdp_clipboard.isChecked()
            sess.rdp_printer = self.rdp_printer.isChecked()
            sess.rdp_smartcard = self.rdp_smartcard.isChecked()
            sess.rdp_sound = self.rdp_sound.isChecked()
            sess.rdp_drive = self.rdp_drive.text().strip()
        mode = self.route.currentData() if self.route_box.isVisible() else "direct"
        sess.routing_mode = mode
        if mode == "proxy_jump":
            sess.jump_host = self.jump_host.text().strip()
            sess.jump_user = self.jump_user.text().strip()
            sess.jump_port = self.jump_port.text().strip() or "22"
        if mode == "saved_tunnel":
            sess.required_tunnel_id = self.tunnel.currentData() or ""
        if mode == "rd_gateway":
            sess.rdp_gateway_host = self.gateway_host.text().strip()
            if not sess.rdp_gateway_host:
                sess.routing_mode = "direct"
        return sess
