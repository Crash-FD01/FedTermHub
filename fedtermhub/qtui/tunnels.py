# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tunnel SSH nell'interfaccia Qt: elenco, modifica, avvio e arresto.

Ogni tunnel è un `ssh -N` (ssh.exe su Windows) in un pseudo-terminale nascosto: la password salvata si
scrive al prompt come nelle schede SSH (PasswordFeeder) e la conferma della chiave di un host nuovo si
chiede con una finestra. Stessi tunnel della versione Linux (model.TunnelStore, tunnels.json).
"""
from __future__ import annotations

import os
import re
import shlex
import socket
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import commands, model
from ..i18n import tr
from . import connect, theme
from .pty_process import PtyProcess

ICONS = Path(__file__).resolve().parent.parent / "icons"
_HOST_KEY = re.compile(r"(?i)are you sure you want to continue connecting.*\?\s*$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def ask_host_key(tunnel: model.TunnelSession, details: str) -> bool:
    """Chiede se accettare la chiave di un host nuovo: ssh aspetta la risposta nel terminale nascosto."""
    answer = QMessageBox.question(
        None, tr("Chiave dell'host sconosciuta"),
        tr("Tunnel «{name}»: è la prima connessione a {host}.\n\n{details}\n\nAccetti la chiave? Verrà salvata "
           "in known_hosts.").format(name=tunnel.name, host=tunnel.ssh_host, details=details),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    return answer == QMessageBox.StandardButton.Yes


def tunnel_command_text(tunnel: model.TunnelSession, settings: model.AppSettings) -> str:
    """Il comando da mostrare o copiare: la password non ci passa mai, si scrive al prompt."""
    binary = connect.ssh_binary() or "ssh"
    try:
        argv = commands.tunnel_argv(binary, tunnel, settings, tunnel.ssh_user, False)
    except commands.CommandError as exc:
        return tr("Configurazione incompleta: {exc}").format(exc=exc.title)
    return subprocess.list2cmdline(argv) if connect.IS_WINDOWS else shlex.join(argv)


def tunnel_argv(tunnel: model.TunnelSession, settings: model.AppSettings, user: str, password: Optional[str]) -> List[str]:
    """Il comando ssh del tunnel: commands.tunnel_argv, uguale alla versione Linux."""
    binary = connect.ssh_binary()
    if not binary:
        raise connect.LaunchError(tr("Client OpenSSH non trovato"))
    try:
        return commands.tunnel_argv(binary, tunnel, settings, user, bool(password))
    except commands.CommandError as exc:
        raise connect.LaunchError(exc.title) from exc


class TunnelRunner(QObject):
    """Un tunnel in esecuzione. Segnale changed() quando cambia lo stato o il log."""

    changed = Signal()

    def __init__(self, tunnel: model.TunnelSession, manager: "TunnelManager") -> None:
        super().__init__(manager)
        self.tunnel = tunnel
        self.manager = manager
        self.state = "stopped"
        self.log: List[str] = []
        self.process: Optional[PtyProcess] = None
        self.exit_code: Optional[int] = None
        self.feeder = connect.PasswordFeeder(None)
        self._tail = ""
        self._asking = False
        self._wanted = False
        self._check = QTimer(self)
        self._check.setInterval(400)
        self._check.timeout.connect(self._probe)
        self._deadline = 0
        self._elapsed = 0
        self._restart = QTimer(self)
        self._restart.setSingleShot(True)
        self._restart.timeout.connect(self.start)

    def _set(self, state: str) -> None:
        self.state = state
        self.changed.emit()

    def _append(self, text: str) -> None:
        clean = _ANSI.sub("", text).replace("\r", "")
        for line in clean.split("\n"):
            if line.strip():
                self.log.append(line.rstrip())
        self.log = self.log[-300:]
        self.changed.emit()

    def append_log(self, text: str) -> None:
        self._append(text)

    @property
    def pid(self) -> Optional[int]:
        proc = getattr(self.process, "proc", None)
        return getattr(proc, "pid", None)

    def start(self) -> None:
        if self.process is not None and self.process.running:
            return
        self._wanted = True
        self.state = "starting"  # prima dei messaggi nel log: chi aspetta il tunnel non deve vederlo «fermo»
        user, password = self.manager.identity(self.tunnel)
        try:
            argv = tunnel_argv(self.tunnel, self.manager.settings, user, password)
        except connect.LaunchError as exc:
            self._append(exc.title)
            self._set("error")
            return
        self.feeder = connect.PasswordFeeder(password)
        self.process = PtyProcess(self)
        self.process.output.connect(self._output)
        self.process.finished.connect(self._finished)
        self._append(tr("Avvio: {command}").format(command=" ".join(argv[1:])))
        try:
            self.process.start(argv, str(Path.home()), {})
        except Exception as exc:  # noqa: BLE001
            self._append(str(exc))
            self._set("error")
            return
        self._set("starting")
        self._deadline = max(8, self.manager.settings.connection_timeout_seconds + 4) * 1000
        self._elapsed = 0
        self._tail = ""
        self._check.start()

    def _output(self, text: str) -> None:
        answer = self.feeder.feed(text)
        if answer:
            self.process.write(answer)
            return
        self._append(text)
        self._tail = (self._tail + _ANSI.sub("", text))[-600:]
        last = self._tail.replace("\r", "\n").rstrip(" ").split("\n")[-1]
        if _HOST_KEY.search(last) and not self._asking:
            self._asking = True
            details = "\n".join(line for line in self._tail.replace("\r", "").split("\n")[-6:-1] if line.strip())
            accepted = ask_host_key(self.tunnel, details)
            self._asking = False
            self._tail = ""
            if self.process is not None:
                self.process.write("yes\r" if accepted else "no\r")

    def _probe(self) -> None:
        self._elapsed += self._check.interval()
        if self.process is None or not self.process.running:
            self._check.stop()
            return
        ready = False
        if self.tunnel.tunnel_type in {"local", "dynamic"}:
            host = self.tunnel.bind_address or "127.0.0.1"
            if host in {"0.0.0.0", "*", ""}:
                host = "127.0.0.1"
            try:
                with socket.create_connection((host, int(self.tunnel.local_port)), timeout=0.3):
                    ready = True
            except OSError:
                ready = False
        else:
            ready = self._elapsed >= 3000 and not self.feeder.armed
        if ready:
            self._check.stop()
            self._append(tr("Tunnel attivo."))
            self._set("running")
        elif self._elapsed >= self._deadline and not self._asking:
            self._check.stop()
            self._append(tr("Il tunnel non è diventato operativo entro il tempo previsto."))
            self.stop()
            self._set("error")

    def _finished(self, code: int) -> None:
        self._check.stop()
        self.process = None
        self.exit_code = code
        self._append(tr("ssh terminato (codice {code}).").format(code=code))
        if self._wanted and self.tunnel.auto_restart and self.state in {"running"}:
            self._set("waiting")
            self._restart.start(max(1, self.tunnel.restart_delay) * 1000)
        elif self._wanted and self.state != "stopped":
            self._set("error")
        else:
            self._set("stopped")

    def stop(self) -> None:
        self._wanted = False
        self._restart.stop()
        self._check.stop()
        if self.process is not None:
            process, self.process = self.process, None
            process.finished.disconnect(self._finished)
            process.stop()
            self._append(tr("Tunnel fermato."))
        self._set("stopped")


class TunnelManager(QObject):
    """Tutti i tunnel salvati e quelli in esecuzione."""

    changed = Signal()

    def __init__(self, settings: model.AppSettings, identity: Callable[[model.TunnelSession], tuple],
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.store = model.TunnelStore(model.TUNNELS_FILE)
        self.identity = identity
        self.runners: Dict[str, TunnelRunner] = {}

    def runner(self, tunnel_id: str) -> Optional[TunnelRunner]:
        tunnel = self.store.get(tunnel_id)
        if tunnel is None:
            return None
        runner = self.runners.get(tunnel_id)
        if runner is None:
            runner = TunnelRunner(tunnel, self)
            runner.changed.connect(self.changed)
            self.runners[tunnel_id] = runner
        runner.tunnel = tunnel
        return runner

    def state(self, tunnel_id: str) -> str:
        runner = self.runners.get(tunnel_id)
        return runner.state if runner else "stopped"

    def start(self, tunnel_id: str) -> None:
        runner = self.runner(tunnel_id)
        if runner is not None:
            runner.start()

    def stop(self, tunnel_id: str) -> None:
        runner = self.runners.get(tunnel_id)
        if runner is not None:
            runner.stop()

    def stop_all(self) -> None:
        for runner in list(self.runners.values()):
            runner.stop()

    def active(self) -> int:
        return sum(1 for r in self.runners.values() if r.state in {"running", "starting", "waiting"})

    def start_auto(self) -> None:
        for tunnel in self.store.tunnels:
            if tunnel.auto_start:
                self.start(tunnel.id)

    def when_ready(self, tunnel_id: str, callback: Callable[[bool, str], None]) -> None:
        """Avvia il tunnel (se serve) e chiama callback(pronto, log) quando è attivo o è fallito."""
        runner = self.runner(tunnel_id)
        if runner is None:
            callback(False, tr("Il tunnel collegato alla sessione non esiste più."))
            return
        if runner.state == "running":
            callback(True, "")
            return

        def check() -> None:
            if runner.state == "running":
                runner.changed.disconnect(check)
                callback(True, "")
            elif runner.state in {"error", "stopped"}:
                runner.changed.disconnect(check)
                callback(False, "\n".join(runner.log[-15:]))

        runner.changed.connect(check)
        runner.start()


class TunnelProbe(QObject):
    """Verifica SSH senza aprire porte: accesso e raggiungibilità (commands.tunnel_probe_argv), come su Linux.

    Gira in un pseudo-terminale nascosto perché su Windows non c'è sshpass: la password si scrive al prompt.
    """

    finished = Signal(bool, str, str)  # riuscita, messaggio, dettagli

    def __init__(self, tunnel: model.TunnelSession, settings: model.AppSettings,
                 identity: Callable[[model.TunnelSession], tuple], parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.tunnel = tunnel
        self.settings = settings
        self.identity = identity
        self.process: Optional[PtyProcess] = None
        self.output = ""
        self._done = False
        self._started = 0.0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._timeout)

    def start(self) -> None:
        binary = connect.ssh_binary()
        if not binary:
            self._finish(False, tr("Client OpenSSH non trovato"), "")
            return
        user, password = self.identity(self.tunnel)
        try:
            argv = commands.tunnel_probe_argv(binary, self.tunnel, self.settings, user, bool(password))
            self.feeder = connect.PasswordFeeder(password)
            self.process = PtyProcess(self)
            self.process.output.connect(self._output)
            self.process.finished.connect(self._finished)
            self._started = time.monotonic()
            self.process.start(argv, str(Path.home()), {})
        except Exception as exc:  # noqa: BLE001
            self._finish(False, tr("Impossibile preparare la verifica: {exc}").format(exc=exc), "")
            return
        self._timer.start(max(8, self.settings.connection_timeout_seconds + 5) * 1000)

    def _output(self, text: str) -> None:
        answer = self.feeder.feed(text)
        if answer:
            self.process.write(answer)
            return
        self.output += text
        last = _ANSI.sub("", self.output).replace("\r", "\n").rstrip(" ").split("\n")[-1]
        if _HOST_KEY.search(last):
            lines = _ANSI.sub("", self.output).replace("\r", "").split("\n")
            self.output = ""
            accepted = ask_host_key(self.tunnel, "\n".join(line for line in lines[-6:-1] if line.strip()))
            self.process.write("yes\r" if accepted else "no\r")

    def _clean_output(self) -> str:
        lines = (line.strip() for line in _ANSI.sub("", self.output).replace("\r", "\n").split("\n"))
        return "\n".join(line for line in lines if line and "FEDTERMHUB_TUNNEL_OK" not in line)

    def _finished(self, code: int) -> None:
        elapsed = int((time.monotonic() - self._started) * 1000)
        if code == 0 and "FEDTERMHUB_TUNNEL_OK" in self.output:
            self._finish(True, tr("Connessione SSH riuscita in {elapsed} ms.").format(elapsed=elapsed), self._clean_output())
        else:
            self._finish(False, tr("Verifica SSH fallita (codice {returncode}).").format(returncode=code), self._clean_output())

    def _timeout(self) -> None:
        if self.process is not None:
            self.process.finished.disconnect(self._finished)
            self.process.stop()
        self._finish(False, tr("Timeout durante la verifica SSH."), self._clean_output())

    def _finish(self, ok: bool, message: str, details: str) -> None:
        if not self._done:
            self._done = True
            self._timer.stop()
            self.finished.emit(ok, message, details)
            self.deleteLater()


def _heading(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet("font-size: 13px; font-weight: 600;")
    return label


def _card() -> Tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setProperty("role", "card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(11, 11, 11, 11)
    layout.setSpacing(8)
    return frame, layout


def _section(title: str) -> Tuple[QFrame, QGridLayout]:
    """Un riquadro con titolo e griglia di campi: la colonna dei campi prende la larghezza che avanza."""
    frame, layout = _card()
    layout.addWidget(_heading(title))
    grid = QGridLayout()
    grid.setHorizontalSpacing(9)
    grid.setVerticalSpacing(8)
    grid.setColumnStretch(1, 1)
    layout.addLayout(grid)
    layout.addStretch(1)
    return frame, grid


def _port_spin(value: int, minimum: int = 1, maximum: int = 65535) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(value)
    return spin


def _safe_port(value: object, default: int) -> int:
    try:
        return max(1, min(int(str(value)), 65535))
    except (TypeError, ValueError):
        return default


class _Expander(QWidget):
    """Una sezione che si apre e si chiude, come Gtk.Expander."""

    def __init__(self, title: str, content: QWidget) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.toggle = QToolButton()
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setStyleSheet("QToolButton { border: 0; background: transparent; font-weight: 600; }")
        self.toggle.toggled.connect(self._toggled)
        layout.addWidget(self.toggle)
        self.content = content
        content.hide()
        layout.addWidget(content)

    def _toggled(self, shown: bool) -> None:
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)
        self.content.setVisible(shown)


class _RouteNode(QFrame):
    """Un elemento del percorso del tunnel: icona, titolo e indirizzo."""

    def __init__(self, icon_name: str) -> None:
        super().__init__()
        self.setProperty("role", "card")
        self.setMinimumHeight(104)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        image = QLabel()
        image.setPixmap(theme.themed_icon(ICONS / f"{icon_name}.svg").pixmap(34, 34))
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title = QLabel()
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setStyleSheet("font-weight: 600;")
        self.detail = QLabel()
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail.setProperty("role", "muted")
        self.detail.setMinimumWidth(0)
        self.detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(image)
        layout.addWidget(self.title)
        layout.addWidget(self.detail)


class TunnelEditDialog(QDialog):
    """Nuovo o modifica tunnel, con la disposizione della versione Linux (TunnelDialog).

    Percorso, tre riquadri allineati ai suoi elementi, affidabilità, opzioni avanzate e anteprima del comando,
    tutto in un'unica area che scorre: allargando la finestra si allargano i campi, non gli spazi fra loro.
    """

    def __init__(self, parent: Optional[QWidget], tunnel: Optional[model.TunnelSession], credentials: List[tuple],
                 settings: Optional[model.AppSettings] = None,
                 identity: Optional[Callable[[model.TunnelSession], tuple]] = None) -> None:
        super().__init__(parent)
        self.tunnel = tunnel or model.TunnelSession()
        self.settings = settings or model.AppSettings()
        self.identity = identity or (lambda t: (t.ssh_user, None))
        t = self.tunnel
        self.setWindowTitle(tr("Nuovo tunnel SSH") if tunnel is None else tr("Modifica tunnel SSH"))
        self.resize(1020, 760)
        self.setMinimumSize(760, 480)
        shell = QVBoxLayout(self)
        shell.setSpacing(10)

        header = QHBoxLayout()
        image = QLabel()
        image.setPixmap(theme.themed_icon(ICONS / "fth-tb-rete.svg").pixmap(30, 30))
        header.addWidget(image)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        heading = QLabel(tr("Tunnel SSH"))
        heading.setProperty("role", "heading")
        subtitle = QLabel(tr("Crea un inoltro locale, remoto oppure un proxy SOCKS."))
        subtitle.setProperty("role", "muted")
        titles.addWidget(heading)
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        shell.addLayout(header)

        name_row = QHBoxLayout()
        name_label = QLabel(tr("Nome"))
        name_label.setMinimumWidth(90)
        name_row.addWidget(name_label)
        self.name = QLineEdit(t.name)
        self.name.setPlaceholderText(tr("es. RDP cliente tramite bastion"))
        name_row.addWidget(self.name, 1)
        shell.addLayout(name_row)

        modes = QHBoxLayout()
        self.radio_local = QRadioButton(tr("Locale  (-L)"))
        self.radio_remote = QRadioButton(tr("Remoto  (-R)"))
        self.radio_dynamic = QRadioButton(tr("SOCKS  (-D)"))
        self.type_group = QButtonGroup(self)
        for radio in (self.radio_local, self.radio_remote, self.radio_dynamic):
            self.type_group.addButton(radio)
            modes.addWidget(radio, 1)
        shell.addLayout(modes)

        body = QWidget()
        rows = QVBoxLayout(body)
        rows.setContentsMargins(4, 4, 4, 4)
        rows.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        shell.addWidget(scroll, 1)

        # Il percorso della connessione.
        route, route_layout = _card()
        route_layout.addWidget(_heading(tr("Percorso della connessione")))
        nodes = QHBoxLayout()
        nodes.setSpacing(12)
        self.flow_left = _RouteNode("fth-proto-local")
        self.flow_ssh = _RouteNode("fth-proto-ssh")
        self.flow_right = _RouteNode("fth-tb-rete")
        self.arrow1, self.arrow2 = QLabel("→"), QLabel("→")
        for arrow in (self.arrow1, self.arrow2):
            arrow.setStyleSheet("font-size: 18px;")
        nodes.addWidget(self.flow_left, 1)
        nodes.addWidget(self.arrow1)
        nodes.addWidget(self.flow_ssh, 1)
        nodes.addWidget(self.arrow2)
        nodes.addWidget(self.flow_right, 1)
        route_layout.addLayout(nodes)
        self.explain = QLabel()
        self.explain.setWordWrap(True)
        route_layout.addWidget(self.explain)
        rows.addWidget(route)

        # I tre riquadri corrispondono ai tre elementi del percorso.
        fields = QHBoxLayout()
        fields.setSpacing(10)
        listen_card, listen = _section(tr("Porta di ascolto"))
        ssh_card, ssh = _section(tr("Server SSH"))
        destination_card, destination = _section(tr("Destinazione"))
        fields.addWidget(listen_card, 3)
        fields.addWidget(ssh_card, 4)
        fields.addWidget(destination_card, 3)
        rows.addLayout(fields)

        self.bind_label = QLabel()
        self.bind_address = QLineEdit(t.bind_address or "127.0.0.1")
        self.bind_address.setPlaceholderText(tr("127.0.0.1, 0.0.0.0 oppure ::1"))
        self.port_label = QLabel()
        self.local_port = _port_spin(_safe_port(t.local_port, 1080 if t.tunnel_type == "dynamic" else 13389))
        listen.addWidget(self.bind_label, 0, 0)
        listen.addWidget(self.bind_address, 0, 1)
        listen.addWidget(self.port_label, 1, 0)
        listen.addWidget(self.local_port, 1, 1)

        self.ssh_host = QLineEdit(t.ssh_host)
        self.ssh_host.setPlaceholderText("bastion.example.local")
        self.ssh_port = _port_spin(_safe_port(t.ssh_port, 22))
        self.ssh_user = QLineEdit(t.ssh_user)
        self.ssh_user.setPlaceholderText(tr("utente SSH"))
        self.credential = QComboBox()
        self.credential.addItem(tr("Nessuna credenziale collegata"), "")
        for cid, label in credentials:
            self.credential.addItem(label, cid)
        if t.credential_id and self.credential.findData(t.credential_id) < 0:
            self.credential.addItem(tr("Credenziale {v}…").format(v=t.credential_id[:12]), t.credential_id)
        self.credential.setCurrentIndex(max(0, self.credential.findData(t.credential_id)))
        self.use_password = QCheckBox(tr("Usa la password salvata nel Password Manager"))
        self.use_password.setChecked(t.use_credential_password)
        self.identity_file = QLineEdit(t.identity_file)
        self.identity_file.setPlaceholderText("~/.ssh/id_ed25519")
        browse = QPushButton(tr("Sfoglia…"))
        browse.setAutoDefault(False)
        browse.clicked.connect(self._browse)
        identity_row = QHBoxLayout()
        identity_row.addWidget(self.identity_file, 1)
        identity_row.addWidget(browse)
        for row, (text, widget) in enumerate(((tr("Host"), self.ssh_host), (tr("Porta"), self.ssh_port),
                                              (tr("Utente"), self.ssh_user), (tr("Credenziale"), self.credential))):
            ssh.addWidget(QLabel(text), row, 0)
            ssh.addWidget(widget, row, 1)
        ssh.addWidget(self.use_password, 4, 1)
        ssh.addWidget(QLabel(tr("Chiave privata")), 5, 0)
        ssh.addLayout(identity_row, 5, 1)

        # Per SOCKS i campi lasciano il posto a una spiegazione.
        self.destination_stack = QStackedWidget()
        fields_page = QWidget()
        fields_grid = QGridLayout(fields_page)
        fields_grid.setContentsMargins(0, 0, 0, 0)
        fields_grid.setRowStretch(2, 1)
        fields_grid.setColumnStretch(1, 1)
        self.remote_host = QLineEdit(t.remote_host or "127.0.0.1")
        self.remote_host.setPlaceholderText(tr("Server o servizio finale"))
        self.remote_port = _port_spin(_safe_port(t.remote_port, 3389))
        fields_grid.addWidget(QLabel(tr("Host")), 0, 0)
        fields_grid.addWidget(self.remote_host, 0, 1)
        fields_grid.addWidget(QLabel(tr("Porta")), 1, 0)
        fields_grid.addWidget(self.remote_port, 1, 1)
        socks = QLabel(tr("La destinazione viene scelta dall'applicazione che usa il proxy SOCKS."))
        socks.setWordWrap(True)
        socks.setProperty("role", "muted")
        socks.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.destination_stack.addWidget(fields_page)
        self.destination_stack.addWidget(socks)
        destination.addWidget(self.destination_stack, 0, 0, 1, 2)

        # Affidabilità e avvio.
        reliability_card, reliability = _section(tr("Affidabilità e avvio"))
        reliability.setColumnStretch(3, 1)
        self.exit_on_failure = QCheckBox(tr("Arresta se l'inoltro non può essere aperto"))
        self.exit_on_failure.setChecked(t.exit_on_forward_failure)
        self.compression = QCheckBox(tr("Compressione SSH"))
        self.compression.setChecked(t.compression)
        self.auto_start = QCheckBox(tr("Avvia automaticamente con FedTermHub"))
        self.auto_start.setChecked(t.auto_start)
        self.auto_restart = QCheckBox(tr("Riconnetti automaticamente se il tunnel cade"))
        self.auto_restart.setChecked(t.auto_restart)
        self.keepalive = _port_spin(t.server_alive_interval, 0, 3600)
        self.keepalive.setSingleStep(5)
        self.keepalive_count = _port_spin(t.server_alive_count, 1, 100)
        self.restart_delay = _port_spin(t.restart_delay, 1, 300)
        reliability.addWidget(self.exit_on_failure, 0, 0, 1, 2)
        reliability.addWidget(self.compression, 0, 2, 1, 2)
        reliability.addWidget(self.auto_start, 1, 0, 1, 2)
        reliability.addWidget(self.auto_restart, 1, 2, 1, 2)
        reliability.addWidget(QLabel(tr("Keepalive (s)")), 2, 0)
        reliability.addWidget(self.keepalive, 2, 1)
        reliability.addWidget(QLabel(tr("Tentativi")), 2, 2)
        reliability.addWidget(self.keepalive_count, 2, 3)
        reliability.addWidget(QLabel(tr("Ritardo riavvio (s)")), 3, 0)
        reliability.addWidget(self.restart_delay, 3, 1)
        rows.addWidget(reliability_card)

        # Opzioni avanzate e anteprima del comando, chiuse all'inizio.
        advanced_page = QWidget()
        advanced = QGridLayout(advanced_page)
        advanced.setColumnStretch(1, 1)
        self.address_family = QComboBox()
        for label, key in ((tr("Automatica"), "auto"), (tr("Solo IPv4"), "ipv4"), (tr("Solo IPv6"), "ipv6")):
            self.address_family.addItem(label, key)
        self.address_family.setCurrentIndex(max(0, self.address_family.findData(t.address_family)))
        self.verbose_log = QCheckBox(tr("Log SSH dettagliato (-v)"))
        self.verbose_log.setChecked(t.verbose_log)
        self.options = QLineEdit(t.options)
        self.options.setPlaceholderText(tr("es. -o ProxyJump=jump.example"))
        advanced.addWidget(QLabel(tr("Famiglia IP")), 0, 0)
        advanced.addWidget(self.address_family, 0, 1)
        advanced.addWidget(self.verbose_log, 1, 1)
        advanced.addWidget(QLabel(tr("Opzioni extra")), 2, 0)
        advanced.addWidget(self.options, 2, 1)
        rows.addWidget(_Expander(tr("Opzioni avanzate"), advanced_page))
        self.command_preview = QPlainTextEdit()
        self.command_preview.setReadOnly(True)
        self.command_preview.setFixedHeight(84)
        self.command_preview.setStyleSheet("font-family: Consolas, 'DejaVu Sans Mono', monospace;")
        rows.addWidget(_Expander(tr("Anteprima del comando"), self.command_preview))
        rows.addStretch(1)

        self.validation = QLabel("")
        self.validation.setWordWrap(True)
        self.validation.hide()
        shell.addWidget(self.validation)

        buttons = QHBoxLayout()
        self.test_button = QPushButton(tr("Verifica connessione"))
        self.test_button.setAutoDefault(False)
        self.test_button.clicked.connect(self.test_connection)
        buttons.addWidget(self.test_button)
        buttons.addStretch(1)
        cancel = QPushButton(tr("Annulla"))
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save_button = QPushButton(tr("Salva tunnel"))
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._accept)
        buttons.addWidget(self.save_button)
        shell.addLayout(buttons)

        for edit in (self.name, self.ssh_host, self.ssh_user, self.bind_address, self.remote_host, self.identity_file,
                     self.options):
            edit.textChanged.connect(self.update_preview)
        for spin in (self.ssh_port, self.local_port, self.remote_port, self.keepalive, self.keepalive_count,
                     self.restart_delay):
            spin.valueChanged.connect(self.update_preview)
        for combo in (self.credential, self.address_family):
            combo.currentIndexChanged.connect(self.update_preview)
        for box in (self.use_password, self.exit_on_failure, self.compression, self.auto_start, self.auto_restart,
                    self.verbose_log):
            box.toggled.connect(self.update_preview)
        {"remote": self.radio_remote, "dynamic": self.radio_dynamic}.get(t.tunnel_type, self.radio_local).setChecked(True)
        self.type_group.buttonToggled.connect(lambda _button, on: on and self.update_type())
        self.update_type()

    def _browse(self) -> None:
        current = os.path.expanduser(self.identity_file.text().strip())
        start = current if current and Path(current).exists() else str(Path.home() / ".ssh")
        path, _ = QFileDialog.getOpenFileName(self, tr("Scegli chiave privata SSH"), start)
        if path:
            self.identity_file.setText(path)

    def current_type(self) -> str:
        if self.radio_dynamic.isChecked():
            return "dynamic"
        return "remote" if self.radio_remote.isChecked() else "local"

    def update_type(self) -> None:
        tunnel_type = self.current_type()
        route = commands.tunnel_route(tunnel_type)
        self.destination_stack.setCurrentIndex(1 if tunnel_type == "dynamic" else 0)
        self.bind_label.setText(route["bind"])
        self.port_label.setText(route["port"])
        self.flow_left.title.setText(route["left"])
        self.flow_ssh.title.setText(tr("Server SSH"))
        self.flow_right.title.setText(route["right"])
        self.arrow1.setText(route["arrow"])
        self.arrow2.setText(route["arrow"])
        self.explain.setText(route["explain"])
        self.update_preview()

    def result_tunnel(self) -> model.TunnelSession:
        return model.TunnelSession(
            id=self.tunnel.id,
            name=self.name.text().strip() or tr("Tunnel SSH"),
            tunnel_type=self.current_type(),
            ssh_host=self.ssh_host.text().strip(),
            ssh_port=str(self.ssh_port.value()),
            ssh_user=self.ssh_user.text().strip(),
            bind_address=self.bind_address.text().strip() or "127.0.0.1",
            local_port=str(self.local_port.value()),
            remote_host=self.remote_host.text().strip() or "127.0.0.1",
            remote_port=str(self.remote_port.value()),
            options=self.options.text().strip(),
            credential_id=self.credential.currentData() or "",
            use_credential_password=self.use_password.isChecked(),
            identity_file=self.identity_file.text().strip(),
            address_family=self.address_family.currentData() or "auto",
            exit_on_forward_failure=self.exit_on_failure.isChecked(),
            compression=self.compression.isChecked(),
            server_alive_interval=self.keepalive.value(),
            server_alive_count=self.keepalive_count.value(),
            auto_start=self.auto_start.isChecked(),
            auto_restart=self.auto_restart.isChecked(),
            restart_delay=self.restart_delay.value(),
            verbose_log=self.verbose_log.isChecked(),
        )

    def update_preview(self, *_args) -> None:  # noqa: ANN002
        tunnel = self.result_tunnel()
        left, ssh, right = commands.tunnel_route_details(tunnel)
        self.flow_left.detail.setText(left)
        self.flow_ssh.detail.setText(ssh)
        self.flow_right.detail.setText(right)
        for node in (self.flow_left, self.flow_ssh, self.flow_right):
            node.detail.setToolTip(node.detail.text())
        self.use_password.setEnabled(bool(tunnel.credential_id))
        self.command_preview.setPlainText(tunnel_command_text(tunnel, self.settings))

    def _show_validation(self, message: str, success: Optional[bool] = None) -> None:
        self.validation.setProperty("role", "danger" if success is False else "muted")
        self.validation.style().unpolish(self.validation)
        self.validation.style().polish(self.validation)
        self.validation.setText(message)
        self.validation.show()

    def test_connection(self) -> None:
        tunnel = self.result_tunnel()
        problems = commands.tunnel_problems(tunnel)
        if problems:
            self._show_validation(tr("Correggi prima: ") + " • ".join(problems), False)
            return
        self.test_button.setEnabled(False)
        self._show_validation(tr("Verifica del server SSH in corso…"))
        probe = TunnelProbe(tunnel, self.settings, self.identity, self)
        probe.finished.connect(self._test_result)
        probe.start()

    def _test_result(self, success: bool, message: str, details: str) -> None:
        self.test_button.setEnabled(True)
        self._show_validation(message if not details else f"{message}\n{details[-600:]}", success)

    def _accept(self) -> None:
        problems = commands.tunnel_problems(self.result_tunnel())
        if problems:
            self._show_validation(tr("Impossibile salvare: ") + " • ".join(problems), False)
            return
        self.accept()


class TunnelsDialog(QDialog):
    """Strumenti › Tunneling SSH, con la disposizione e le azioni della versione Linux (TunnelManagerDialog).

    Allargando la finestra si allarga solo la tabella; il riepilogo resta compatto e il log si apre sotto.
    """

    COLUMNS = 7
    C_STATE, C_NAME, C_TYPE, C_LISTEN, C_DEST, C_SSH, C_AUTO = range(COLUMNS)
    ACTIVE = {"running", "starting", "waiting"}

    def __init__(self, parent: Optional[QWidget], manager: TunnelManager, credentials: Callable[[], List[tuple]],
                 host: Optional[object] = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.credentials = credentials
        self.host = host  # la finestra: sessioni collegate ai tunnel e notifiche
        self._snapshot: object = None
        self.setWindowTitle(tr("Tunnel SSH"))
        self.resize(1120, 700)
        self.setMinimumSize(820, 560)
        root = QVBoxLayout(self)
        root.setSpacing(8)

        header = QHBoxLayout()
        image = QLabel()
        image.setPixmap(theme.themed_icon(ICONS / "fth-tb-rete.svg").pixmap(28, 28))
        header.addWidget(image)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        heading = QLabel(tr("Gestore tunnel SSH"))
        heading.setProperty("role", "heading")
        hint = QLabel(tr("Inoltri locali, remoti e proxy SOCKS in un'unica vista."))
        hint.setProperty("role", "muted")
        titles.addWidget(heading)
        titles.addWidget(hint)
        header.addLayout(titles, 1)
        self.count_label = QLabel(tr("0 tunnel"))
        self.count_label.setProperty("role", "muted")
        header.addWidget(self.count_label)
        root.addLayout(header)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(5)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Cerca tunnel…"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda *_: self.refresh())
        toolbar.addWidget(self.search, 1)
        self.b_new = self._button(tr("Nuovo"), self.new, toolbar)
        self.b_edit = self._button(tr("Modifica"), self.edit, toolbar)
        self.b_duplicate = self._button(tr("Duplica"), self.duplicate, toolbar)
        self.b_delete = self._button(tr("Elimina"), self.delete, toolbar)
        root.addLayout(toolbar)

        self.table = QTableWidget(0, self.COLUMNS)
        self.table.setHorizontalHeaderLabels([tr("Stato"), tr("Nome"), tr("Tipo"), tr("Porta"), tr("Destinazione"),
                                              tr("Server SSH"), tr("Auto")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setMinimumHeight(180)
        columns = self.table.horizontalHeader()
        columns.setStretchLastSection(False)
        # Le larghezze sono fisse tranne Nome, Destinazione e Server SSH, che si dividono lo spazio.
        for column, width in ((self.C_STATE, 150), (self.C_TYPE, 82), (self.C_LISTEN, 145), (self.C_AUTO, 54)):
            columns.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(column, width)
        for column in (self.C_NAME, self.C_DEST, self.C_SSH):
            columns.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.update_details)
        # Solo activated: un doppio clic emette anche doubleClicked, e l'azione partirebbe due volte.
        self.table.activated.connect(lambda *_: self.toggle_selected())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        root.addWidget(self.table, 1)

        actions = QHBoxLayout()
        actions.setSpacing(5)
        self.b_start = self._button(tr("Avvia"), self.start, actions)
        self.b_stop = self._button(tr("Ferma"), self.stop, actions)
        self.b_test = self._button(tr("Verifica SSH"), self.test_selected, actions)
        self.b_log = self._button(tr("Mostra log"), self.toggle_log, actions)
        self.b_copy = self._button(tr("Copia comando"), self.copy_command, actions)
        self.b_endpoint = self._button(tr("Copia endpoint"), self.copy_endpoint, actions)
        self.b_session = self._button(tr("Crea sessione"), self.create_linked_session, actions)
        actions.addStretch(1)
        root.addLayout(actions)

        detail, detail_layout = _card()
        detail.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.detail_title = _heading(tr("Seleziona un tunnel"))
        self.detail_summary = QLabel()
        self.detail_summary.setWordWrap(True)
        self.detail_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail_status = QLabel()
        self.detail_status.setWordWrap(True)
        self.detail_status.setProperty("role", "muted")
        detail_layout.addWidget(self.detail_title)
        detail_layout.addWidget(self.detail_summary)
        detail_layout.addWidget(self.detail_status)
        root.addWidget(detail)

        self.log_box = QWidget()
        log_layout = QVBoxLayout(self.log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel(tr("Log del tunnel")), 1)
        self._button(tr("Pulisci"), self.clear_log, log_header)
        log_layout.addLayout(log_header)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(400)
        self.log.setMinimumHeight(150)
        log_layout.addWidget(self.log, 1)
        self.log_box.hide()
        root.addWidget(self.log_box, 1)

        footer = QHBoxLayout()
        footer.setSpacing(6)
        self._button(tr("Avvia tutti"), self.start_all, footer)
        self._button(tr("Ferma tutti"), self.stop_all, footer)
        note = QLabel(tr("I tunnel restano attivi anche chiudendo questa finestra."))
        note.setProperty("role", "muted")
        note.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        footer.addWidget(note, 1)
        close = self._button(tr("Chiudi"), self.accept, footer)
        close.setDefault(True)
        root.addLayout(footer)

        manager.changed.connect(self._manager_changed)
        self.refresh()

    @staticmethod
    def _button(text: str, slot: Callable, layout: QHBoxLayout) -> QPushButton:  # noqa: ANN001
        button = QPushButton(text)
        button.setAutoDefault(False)
        button.clicked.connect(lambda *_: slot())
        layout.addWidget(button)
        return button

    def done(self, result: int) -> None:  # noqa: D102
        self.manager.changed.disconnect(self._manager_changed)
        super().done(result)

    # ------------------------------------------------------------------ elenco
    def selected(self) -> Optional[model.TunnelSession]:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.manager.store.get(self.table.item(rows[0].row(), self.C_NAME).data(Qt.ItemDataRole.UserRole))

    def _runtime_snapshot(self) -> tuple:
        return tuple((t.id, self.manager.state(t.id)) for t in self.manager.store.tunnels)

    def _manager_changed(self) -> None:
        # Il gestore avvisa a ogni riga di log: la tabella si rifà solo se è cambiato uno stato.
        if self._runtime_snapshot() != self._snapshot:
            self.refresh()
        else:
            self.update_details()

    def refresh(self, select_id: str = "") -> None:
        current = self.selected()
        select_id = select_id or (current.id if current else "")
        query = self.search.text().strip().lower()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        target_row = 0
        for tunnel in self.manager.store.tunnels:
            haystack = " ".join((tunnel.name, tunnel.type_label(), tunnel.listen_endpoint(),
                                 tunnel.destination_endpoint(), tunnel.ssh_endpoint())).lower()
            if query and query not in haystack:
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            label, color = commands.tunnel_state_display(self.manager.state(tunnel.id))
            values = (label, tunnel.name, tunnel.type_label(), tunnel.listen_endpoint(), tunnel.destination_endpoint(),
                      tunnel.ssh_endpoint(), tr("Sì") if tunnel.auto_start else "—")
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
            self.table.item(row, self.C_NAME).setData(Qt.ItemDataRole.UserRole, tunnel.id)
            state_item = self.table.item(row, self.C_STATE)
            state_item.setForeground(QColor(color))
            font = state_item.font()
            font.setBold(True)
            state_item.setFont(font)
            if tunnel.id == select_id:
                target_row = row
        shown = self.table.rowCount()
        if shown:
            self.table.selectRow(target_row)
        self.table.blockSignals(False)
        self.count_label.setText(tr("{shown} tunnel").format(shown=shown) if shown != 1 else tr("1 tunnel"))
        self._snapshot = self._runtime_snapshot()
        self.update_details()

    def update_details(self) -> None:
        tunnel = self.selected()
        buttons = (self.b_start, self.b_stop, self.b_test, self.b_log, self.b_copy, self.b_endpoint, self.b_session,
                   self.b_edit, self.b_duplicate, self.b_delete)
        for button in buttons:
            button.setEnabled(tunnel is not None)
        self.b_log.setEnabled(True)
        if tunnel is None:
            self.detail_title.setText(tr("Seleziona un tunnel"))
            self.detail_summary.setText(tr("Scegli una riga per visualizzare percorso, stato e azioni disponibili."))
            self.detail_status.setText("")
            self.update_log()
            return
        state = self.manager.state(tunnel.id)
        runner = self.manager.runners.get(tunnel.id)
        self.detail_title.setText(tunnel.name)
        self.detail_summary.setText(tunnel.summary())
        label = commands.tunnel_state_display(state)[0].replace("● ", "").replace("○ ", "")
        details = [tr("Stato: {replace}").format(replace=label),
                   tr("PID: {pid}").format(pid=runner.pid if runner and runner.pid else "—")]
        if runner is not None and runner.exit_code is not None:
            details.append(tr("Ultima uscita: {exit_code}").format(exit_code=runner.exit_code))
        details.append(tr("Keepalive: {server_alive_interval}s × {server_alive_count}").format(
            server_alive_interval=tunnel.server_alive_interval, server_alive_count=tunnel.server_alive_count))
        if tunnel.auto_restart:
            details.append(tr("Riconnessione: {restart_delay}s").format(restart_delay=tunnel.restart_delay))
        self.detail_status.setText("  •  ".join(details))
        self.b_start.setEnabled(state not in {"running", "starting"})
        self.b_stop.setEnabled(state in self.ACTIVE)
        self.b_session.setEnabled(tunnel.tunnel_type == "local")
        self.update_log()

    def update_log(self) -> None:
        if not self.log_box.isVisibleTo(self):
            return
        tunnel = self.selected()
        if tunnel is None:
            text = tr("Seleziona un tunnel per visualizzarne il log.")
        else:
            runner = self.manager.runners.get(tunnel.id)
            text = "\n".join(runner.log) if runner else ""
        if self.log.toPlainText() != text:
            self.log.setPlainText(text)
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _context_menu(self, pos) -> None:  # noqa: ANN001
        row = self.table.rowAt(pos.y())
        if row >= 0:
            self.table.selectRow(row)
        menu = QMenu(self)
        for text, slot in ((tr("Avvia / Ferma"), self.toggle_selected), (tr("Modifica"), self.edit),
                           (tr("Duplica"), self.duplicate), (tr("Verifica SSH"), self.test_selected),
                           (tr("Copia endpoint"), self.copy_endpoint), (tr("Copia comando"), self.copy_command),
                           (tr("Crea sessione collegata"), self.create_linked_session)):
            menu.addAction(text, slot)
        menu.addSeparator()
        menu.addAction(tr("Elimina"), self.delete)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ azioni
    def _status(self, text: str) -> None:
        setter = getattr(self.host, "set_status", None)
        if setter:
            setter(text)

    def _editor(self, tunnel: Optional[model.TunnelSession]) -> TunnelEditDialog:
        return TunnelEditDialog(self, tunnel, self.credentials(), self.manager.settings, self.manager.identity)

    def new(self) -> None:
        dialog = self._editor(None)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            tunnel = dialog.result_tunnel()
            self.manager.store.upsert(tunnel)
            self._status(tr("Tunnel salvato: {name}").format(name=tunnel.name))
            self.refresh(tunnel.id)
        dialog.deleteLater()

    def edit(self) -> None:
        tunnel = self.selected()
        if tunnel is None:
            return
        dialog = self._editor(tunnel)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            updated = dialog.result_tunnel()
            was_running = self.manager.state(tunnel.id) in {"running", "starting"}
            if was_running:
                self.manager.stop(tunnel.id)
            self.manager.store.upsert(updated)
            if was_running:
                self.manager.start(updated.id)
            self.refresh(updated.id)
        dialog.deleteLater()

    def duplicate(self) -> None:
        tunnel = self.selected()
        if tunnel is not None:
            clone = tunnel.duplicate()
            self.manager.store.upsert(clone)
            self.refresh(clone.id)

    def delete(self) -> None:
        tunnel = self.selected()
        if tunnel is None:
            return
        linked = list(self.host.sessions_using_tunnel(tunnel.id)) if self.host is not None else []
        text = tr("Il tunnel attivo verrà fermato e la configurazione rimossa.")
        if linked:
            text += tr("\n\nIl collegamento automatico verrà rimosso da {n_linked} sessioni.").format(n_linked=len(linked))
        answer = QMessageBox.question(self, tr("Eliminare il tunnel “{name}”?").format(name=tunnel.name), text)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.manager.stop(tunnel.id)
        self.manager.store.remove(tunnel.id)
        if linked:
            self.host.unlink_sessions(linked)
        self.refresh()

    def toggle_selected(self) -> None:
        tunnel = self.selected()
        if tunnel is None:
            return
        if self.manager.state(tunnel.id) in self.ACTIVE:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        tunnel = self.selected()
        if tunnel is not None:
            self.manager.start(tunnel.id)
            self.refresh(tunnel.id)

    def stop(self) -> None:
        tunnel = self.selected()
        if tunnel is not None:
            self.manager.stop(tunnel.id)
            self.refresh(tunnel.id)

    def start_all(self) -> None:
        for tunnel in self.manager.store.tunnels:
            if self.manager.state(tunnel.id) not in {"running", "starting"}:
                self.manager.start(tunnel.id)
        self.refresh()

    def stop_all(self) -> None:
        self.manager.stop_all()
        self._status(tr("Tutti i tunnel sono stati fermati."))
        self.refresh()

    def test_selected(self) -> None:
        tunnel = self.selected()
        if tunnel is None:
            return
        self.b_test.setEnabled(False)
        self.detail_status.setText(tr("Verifica del server SSH in corso…"))
        probe = TunnelProbe(tunnel, self.manager.settings, self.manager.identity, self)
        probe.finished.connect(lambda ok, message, details, t=tunnel: self._test_result(t, ok, message, details))
        probe.start()

    def _test_result(self, tunnel: model.TunnelSession, _ok: bool, message: str, details: str) -> None:
        self.b_test.setEnabled(True)
        self.manager.runner(tunnel.id).append_log(message + ("\n" + details if details else ""))
        self._status(message)
        self.update_details()
        self.detail_status.setText(message)

    def toggle_log(self) -> None:
        reveal = not self.log_box.isVisibleTo(self)
        self.log_box.setVisible(reveal)
        self.b_log.setText(tr("Nascondi log") if reveal else tr("Mostra log"))
        self.update_log()

    def clear_log(self) -> None:
        tunnel = self.selected()
        runner = self.manager.runners.get(tunnel.id) if tunnel else None
        if runner is not None:
            runner.log = []
            self.update_log()

    def copy_command(self) -> None:
        tunnel = self.selected()
        if tunnel is not None:
            QGuiApplication.clipboard().setText(tunnel_command_text(tunnel, self.manager.settings))
            self._status(tr("Comando tunnel copiato negli appunti."))

    def copy_endpoint(self) -> None:
        tunnel = self.selected()
        if tunnel is not None:
            QGuiApplication.clipboard().setText(tunnel.listen_endpoint())
            self._status(tr("Endpoint locale copiato negli appunti."))

    def create_linked_session(self) -> None:
        tunnel = self.selected()
        if tunnel is None or tunnel.tunnel_type != "local":
            self._status(tr("La creazione guidata è disponibile per i tunnel locali (-L)."))
            return
        if self.host is not None:
            self.host.create_linked_session(tunnel, self)
