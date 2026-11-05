# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tunnel SSH nell'interfaccia Qt: elenco, modifica, avvio e arresto.

Ogni tunnel è un `ssh -N` (ssh.exe su Windows) in un pseudo-terminale nascosto: la password salvata si
scrive al prompt come nelle schede SSH (PasswordFeeder) e la conferma della chiave di un host nuovo si
chiede con una finestra. Stessi tunnel della versione Linux (model.TunnelStore, tunnels.json).
"""
from __future__ import annotations

import re
import socket
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import commands, model
from ..i18n import tr
from . import connect
from .pty_process import PtyProcess

TYPES = [("local", tr("Locale (-L): porta di questo PC → destinazione")),
         ("remote", tr("Remoto (-R): porta del server → questo PC")),
         ("dynamic", tr("Dinamico (-D): proxy SOCKS"))]
STATE_LABELS = {"stopped": tr("Fermo"), "starting": tr("Avvio…"), "running": tr("Attivo"), "error": tr("Errore"),
                "waiting": tr("Riavvio fra poco…")}
_HOST_KEY = re.compile(r"(?i)are you sure you want to continue connecting.*\?\s*$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def tunnel_problems(tunnel: model.TunnelSession) -> List[str]:
    """Le stesse regole della versione Linux (commands.py), più il nome che qui è obbligatorio."""
    problems = [] if tunnel.name.strip() else [tr("manca il nome")]
    return problems + commands.tunnel_problems(tunnel)


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
            answer = QMessageBox.question(
                None, tr("Chiave dell'host sconosciuta"),
                tr("Tunnel «{name}»: è la prima connessione a {host}.\n\n{details}\n\nAccetti la chiave? Verrà salvata "
                   "in known_hosts.").format(name=self.tunnel.name, host=self.tunnel.ssh_host, details=details),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            self._asking = False
            self._tail = ""
            if self.process is not None:
                self.process.write("yes\r" if answer == QMessageBox.StandardButton.Yes else "no\r")

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


class TunnelEditDialog(QDialog):
    def __init__(self, parent: Optional[QWidget], tunnel: Optional[model.TunnelSession],
                 credentials: List[tuple]) -> None:
        super().__init__(parent)
        import copy

        self.tunnel = copy.deepcopy(tunnel) if tunnel else model.TunnelSession(local_port="")
        self.setWindowTitle(tr("Nuovo tunnel") if tunnel is None else tr("Modifica tunnel: {name}").format(name=self.tunnel.name))
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        t = self.tunnel
        self.name = QLineEdit(t.name if tunnel else "")
        self.name.setPlaceholderText(tr("es. Database del cliente"))
        self.kind = QComboBox()
        for key, label in TYPES:
            self.kind.addItem(label, key)
        self.kind.setCurrentIndex(max(0, self.kind.findData(t.tunnel_type)))
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.ssh_host = QLineEdit(t.ssh_host)
        self.ssh_host.setPlaceholderText(tr("Server SSH da cui passare"))
        self.ssh_port = QLineEdit(t.ssh_port or "22")
        self.ssh_port.setMaximumWidth(80)
        row = QHBoxLayout()
        row.addWidget(self.ssh_host, 1)
        row.addWidget(QLabel(tr("Porta")))
        row.addWidget(self.ssh_port)
        self.ssh_user = QLineEdit(t.ssh_user)
        self.credential = QComboBox()
        self.credential.addItem(tr("(nessuna: chiave SSH o agente)"), "")
        for cid, label in credentials:
            self.credential.addItem(label, cid)
        if t.credential_id and self.credential.findData(t.credential_id) < 0:
            self.credential.addItem(tr("Credenziale salvata (sblocca le password per vederla)"), t.credential_id)
        self.credential.setCurrentIndex(max(0, self.credential.findData(t.credential_id)))
        self.bind = QLineEdit(t.bind_address or "127.0.0.1")
        self.bind.setMaximumWidth(140)
        self.local_port = QLineEdit(t.local_port)
        self.local_port.setMaximumWidth(80)
        listen = QHBoxLayout()
        listen.addWidget(self.bind)
        listen.addWidget(QLabel(":"))
        listen.addWidget(self.local_port)
        listen.addStretch(1)
        self.remote_host = QLineEdit(t.remote_host or "127.0.0.1")
        self.remote_port = QLineEdit(t.remote_port)
        self.remote_port.setMaximumWidth(80)
        dest = QHBoxLayout()
        dest.addWidget(self.remote_host, 1)
        dest.addWidget(QLabel(":"))
        dest.addWidget(self.remote_port)
        self.identity = QLineEdit(t.identity_file)
        self.identity.setPlaceholderText(tr("Facoltativa: file della chiave privata"))
        browse = QPushButton(tr("Sfoglia…"))
        browse.clicked.connect(self._browse)
        identity_row = QHBoxLayout()
        identity_row.addWidget(self.identity, 1)
        identity_row.addWidget(browse)
        self.auto_start = QCheckBox(tr("Avvia all'apertura di FedTermHub"))
        self.auto_start.setChecked(t.auto_start)
        self.auto_restart = QCheckBox(tr("Riavvia da solo se la connessione cade"))
        self.auto_restart.setChecked(t.auto_restart)
        self.compression = QCheckBox(tr("Compressione"))
        self.compression.setChecked(t.compression)
        self.restart_delay = QSpinBox()
        self.restart_delay.setRange(1, 300)
        self.restart_delay.setSuffix(" s")
        self.restart_delay.setValue(t.restart_delay)
        self.options = QLineEdit(t.options)
        self.options.setPlaceholderText(tr("Opzioni ssh in più, es. -o HostKeyAlias=nome"))
        form.addRow(tr("Nome"), self.name)
        form.addRow(tr("Tipo"), self.kind)
        form.addRow(tr("Server SSH"), row)
        form.addRow(tr("Utente"), self.ssh_user)
        form.addRow(tr("Credenziale"), self.credential)
        self.listen_label = QLabel(tr("In ascolto su"))
        form.addRow(self.listen_label, listen)
        self.dest_label = QLabel(tr("Destinazione"))
        form.addRow(self.dest_label, dest)
        form.addRow(tr("Chiave privata"), identity_row)
        form.addRow("", self.auto_start)
        form.addRow("", self.auto_restart)
        form.addRow(tr("Attesa prima del riavvio"), self.restart_delay)
        form.addRow("", self.compression)
        form.addRow(tr("Opzioni"), self.options)
        layout.addLayout(form)
        self.example = QLabel("")
        self.example.setWordWrap(True)
        self.example.setProperty("role", "muted")
        layout.addWidget(self.example)
        self.error = QLabel("")
        self.error.setProperty("role", "danger")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._kind_changed()

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("Chiave privata"), str(Path.home() / ".ssh"))
        if path:
            self.identity.setText(path)

    def _kind_changed(self) -> None:
        kind = self.kind.currentData()
        self.dest_label.setVisible(kind != "dynamic")
        for widget in (self.remote_host, self.remote_port):
            widget.setVisible(kind != "dynamic")
        if kind == "local":
            self.listen_label.setText(tr("Porta su questo PC"))
            self.dest_label.setText(tr("Destinazione (vista dal server)"))
            self.example.setText(tr("Esempio: 127.0.0.1:13389 → 10.0.0.5:3389 porta il desktop remoto di 10.0.0.5 "
                                    "su questo PC; poi collegati a 127.0.0.1:13389."))
        elif kind == "remote":
            self.listen_label.setText(tr("Porta sul server"))
            self.dest_label.setText(tr("Destinazione (vista da questo PC)"))
            self.example.setText(tr("Esempio: il server apre la porta 8080 e la gira a 127.0.0.1:80 di questo PC."))
        else:
            self.listen_label.setText(tr("Proxy SOCKS su questo PC"))
            self.example.setText(tr("Esempio: 127.0.0.1:1080 diventa un proxy SOCKS5; il browser naviga dalla rete "
                                    "del server."))

    def result_tunnel(self) -> model.TunnelSession:
        t = self.tunnel
        t.name = self.name.text().strip()
        t.tunnel_type = self.kind.currentData()
        t.ssh_host = self.ssh_host.text().strip()
        t.ssh_port = self.ssh_port.text().strip() or "22"
        t.ssh_user = self.ssh_user.text().strip()
        t.credential_id = self.credential.currentData() or ""
        t.bind_address = self.bind.text().strip() or "127.0.0.1"
        t.local_port = self.local_port.text().strip()
        t.remote_host = self.remote_host.text().strip() or "127.0.0.1"
        t.remote_port = self.remote_port.text().strip()
        t.identity_file = self.identity.text().strip()
        t.auto_start = self.auto_start.isChecked()
        t.auto_restart = self.auto_restart.isChecked()
        t.restart_delay = self.restart_delay.value()
        t.compression = self.compression.isChecked()
        t.options = self.options.text().strip()
        return t

    def _accept(self) -> None:
        problems = tunnel_problems(self.result_tunnel())
        if problems:
            self.error.setText(tr("Correggi prima: ") + "; ".join(problems))
            self.error.show()
            return
        self.accept()


class TunnelsDialog(QDialog):
    """Strumenti › Tunnel SSH: elenco con stato, avvio, arresto, log."""

    def __init__(self, parent: Optional[QWidget], manager: TunnelManager, credentials: Callable[[], List[tuple]],
                 on_saved: Callable[[], None]) -> None:
        super().__init__(parent)
        self.manager = manager
        self.credentials = credentials
        self.on_saved = on_saved
        self.setWindowTitle(tr("Tunnel SSH"))
        self.resize(860, 520)
        layout = QVBoxLayout(self)
        intro = QLabel(tr("Un tunnel porta una porta di un server raggiungibile via SSH su questo PC (o il contrario). "
                          "Una sessione può avviare il suo tunnel da sola: Instradamento › Prima avvia un tunnel salvato."))
        intro.setWordWrap(True)
        intro.setProperty("role", "muted")
        layout.addWidget(intro)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([tr("Nome"), tr("Tipo"), tr("Su questo PC"), tr("Destinazione"), tr("Stato")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selection)
        self.table.doubleClicked.connect(lambda *_: self.edit())
        layout.addWidget(self.table, 2)
        buttons = QHBoxLayout()
        self.b_new = QPushButton(tr("Nuovo"))
        self.b_edit = QPushButton(tr("Modifica"))
        self.b_delete = QPushButton(tr("Elimina"))
        self.b_start = QPushButton(tr("Avvia"))
        self.b_stop = QPushButton(tr("Ferma"))
        for button, slot in ((self.b_new, self.new), (self.b_edit, self.edit), (self.b_delete, self.delete),
                             (self.b_start, self.start), (self.b_stop, self.stop)):
            button.setAutoDefault(False)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        close = QPushButton(tr("Chiudi"))
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        layout.addWidget(QLabel(tr("Log del tunnel selezionato")))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(400)
        layout.addWidget(self.log, 1)
        manager.changed.connect(self.refresh)
        self.refresh()

    def selected(self) -> Optional[model.TunnelSession]:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.manager.store.get(self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole))

    def refresh(self) -> None:
        current = self.selected()
        current_id = current.id if current else ""
        self.table.setRowCount(0)
        kinds = {"local": "-L", "remote": "-R", "dynamic": "SOCKS"}
        for tunnel in self.manager.store.tunnels:
            row = self.table.rowCount()
            self.table.insertRow(row)
            listen = f"{tunnel.bind_address}:{tunnel.local_port}"
            dest = "—" if tunnel.tunnel_type == "dynamic" else f"{tunnel.remote_host}:{tunnel.remote_port}"
            via = f"{tunnel.ssh_user + '@' if tunnel.ssh_user else ''}{tunnel.ssh_host}"
            values = [tunnel.name, kinds.get(tunnel.tunnel_type, ""), listen, f"{dest}  ({tr('via')} {via})",
                      STATE_LABELS.get(self.manager.state(tunnel.id), "")]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, tunnel.id)
                self.table.setItem(row, column, item)
            if tunnel.id == current_id:
                self.table.selectRow(row)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._selection()

    def _selection(self) -> None:
        tunnel = self.selected()
        state = self.manager.state(tunnel.id) if tunnel else "stopped"
        for button in (self.b_edit, self.b_delete):
            button.setEnabled(tunnel is not None)
        self.b_start.setEnabled(tunnel is not None and state in {"stopped", "error"})
        self.b_stop.setEnabled(tunnel is not None and state not in {"stopped", "error"})
        runner = self.manager.runners.get(tunnel.id) if tunnel else None
        text = "\n".join(runner.log) if runner else ""
        if self.log.toPlainText() != text:
            self.log.setPlainText(text)
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def new(self) -> None:
        dialog = TunnelEditDialog(self, None, self.credentials())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.manager.store.upsert(dialog.result_tunnel())
            self.manager.store.save()
            self.on_saved()
            self.refresh()

    def edit(self) -> None:
        tunnel = self.selected()
        if tunnel is None:
            return
        dialog = TunnelEditDialog(self, tunnel, self.credentials())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            was_running = self.manager.state(tunnel.id) == "running"
            self.manager.stop(tunnel.id)
            self.manager.store.upsert(dialog.result_tunnel())
            self.manager.store.save()
            self.on_saved()
            if was_running:
                self.manager.start(tunnel.id)
            self.refresh()

    def delete(self) -> None:
        tunnel = self.selected()
        if tunnel is None:
            return
        answer = QMessageBox.question(self, tr("Eliminare il tunnel?"), tr("Eliminare «{name}»?").format(name=tunnel.name))
        if answer == QMessageBox.StandardButton.Yes:
            self.manager.stop(tunnel.id)
            self.manager.store.remove(tunnel.id)
            self.manager.store.save()
            self.on_saved()
            self.refresh()

    def start(self) -> None:
        tunnel = self.selected()
        if tunnel is not None:
            self.manager.start(tunnel.id)

    def stop(self) -> None:
        tunnel = self.selected()
        if tunnel is not None:
            self.manager.stop(tunnel.id)
