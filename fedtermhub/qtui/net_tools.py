# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Strumenti › Strumenti di rete, nell'interfaccia Qt.

Scansione della rete e delle porte, porte in ascolto, Wake-on-LAN, server HTTP e TFTP temporanei, ping e
traceroute in un terminale. Le funzioni sono in net_core.py; qui solo finestre e thread.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, QSettings, QStandardPaths, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..i18n import tr
from . import net_core


class _Relay(QObject):
    progress = Signal(int, int)
    found = Signal(object)
    done = Signal(object)
    failed = Signal(str)
    log = Signal(str)


def _run(relay: _Relay, work: Callable[[], object]) -> None:
    def target() -> None:
        try:
            relay.done.emit(work())
        except Exception as exc:  # noqa: BLE001
            relay.failed.emit(str(exc))

    threading.Thread(target=target, daemon=True).start()


def _table(headers: List[str], stretch: int) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.verticalHeader().hide()
    table.setSortingEnabled(True)
    table.horizontalHeader().setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    return table


def _add_row(table: QTableWidget, values: List[object]) -> None:
    sorting = table.isSortingEnabled()
    table.setSortingEnabled(False)
    row = table.rowCount()
    table.insertRow(row)
    for column, value in enumerate(values):
        item = QTableWidgetItem()
        if isinstance(value, (int, float)):
            item.setData(Qt.ItemDataRole.DisplayRole, value)
        else:
            item.setText(str(value))
        table.setItem(row, column, item)
    table.setSortingEnabled(sorting)


def _copy_table(table: QTableWidget) -> None:
    lines = ["\t".join(table.horizontalHeaderItem(c).text() for c in range(table.columnCount()))]
    for row in range(table.rowCount()):
        lines.append("\t".join((table.item(row, c).text() if table.item(row, c) else "") for c in range(table.columnCount())))
    QGuiApplication.clipboard().setText("\n".join(lines))


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty("role", "muted")
    return label


class NetworkScanPage(QWidget):
    def __init__(self, tools: "NetworkToolsDialog") -> None:
        super().__init__()
        self.tools = tools
        layout = QVBoxLayout(self)
        layout.addWidget(_hint(tr("Trova gli host attivi in una rete: ping, poi alcune porte TCP per chi non risponde al ping. "
                                  "Il MAC arriva dalla cache ARP di Windows.")))
        bar = QHBoxLayout()
        self.target = QComboBox()
        self.target.setEditable(True)
        self.target.addItems(net_core.local_networks())
        self.resolve = QCheckBox(tr("Risolvi i nomi"))
        self.resolve.setChecked(True)
        self.start = QPushButton(tr("Scansiona"))
        self.start.clicked.connect(self.scan)
        self.stop_button = QPushButton(tr("Ferma"))
        self.stop_button.setEnabled(False)
        bar.addWidget(QLabel(tr("Rete")))
        bar.addWidget(self.target, 1)
        bar.addWidget(self.resolve)
        bar.addWidget(self.start)
        bar.addWidget(self.stop_button)
        layout.addLayout(bar)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        self.table = _table(["IP", tr("Nome"), "MAC", tr("Tempo (ms)")], 1)
        self.table.doubleClicked.connect(lambda *_: self.port_scan())
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        for text, slot in ((tr("Scansiona le porte"), self.port_scan), (tr("Crea sessione…"), self.create_session),
                           (tr("Copia l'elenco"), lambda: _copy_table(self.table))):
            button = QPushButton(text)
            button.clicked.connect(slot)
            actions.addWidget(button)
        actions.addStretch(1)
        self.status = QLabel("")
        actions.addWidget(self.status)
        layout.addLayout(actions)
        self.stop_event = threading.Event()
        self.stop_button.clicked.connect(self.stop_event.set)
        self.relay = _Relay(self)
        self.relay.progress.connect(lambda d, t: (self.progress.setRange(0, t), self.progress.setValue(d)))
        self.relay.found.connect(lambda r: _add_row(self.table, [r.ip, r.name, r.mac, round(r.rtt or 0, 1)]))
        self.relay.done.connect(self._done)
        self.relay.failed.connect(self._failed)

    def scan(self) -> None:
        self.table.setRowCount(0)
        self.stop_event.clear()
        self.start.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.progress.show()
        cidr, resolve = self.target.currentText().strip(), self.resolve.isChecked()
        self.status.setText(tr("Scansione di {cidr}…").format(cidr=cidr))
        _run(self.relay, lambda: net_core.scan_network(cidr, resolve, self.relay.progress.emit, self.relay.found.emit,
                                                       self.stop_event))

    def _done(self, results: object) -> None:
        self.start.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.progress.hide()
        macs = {r.ip: r.mac for r in results}  # type: ignore[union-attr]
        for row in range(self.table.rowCount()):
            ip = self.table.item(row, 0).text()
            if macs.get(ip):
                self.table.item(row, 2).setText(macs[ip])
        self.status.setText(tr("{count} host attivi").format(count=len(results)))  # type: ignore[arg-type]

    def _failed(self, message: str) -> None:
        self.start.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.progress.hide()
        self.status.setText(tr("Errore: {error}").format(error=message))

    def _selected_ip(self) -> str:
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).text() if rows else ""

    def port_scan(self) -> None:
        ip = self._selected_ip()
        if ip:
            self.tools.scan_ports_of(ip)

    def create_session(self) -> None:
        ip = self._selected_ip()
        if ip:
            self.tools.create_session_for(ip, "")


class PortScanPage(QWidget):
    def __init__(self, tools: "NetworkToolsDialog") -> None:
        super().__init__()
        self.tools = tools
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.host = QLineEdit()
        self.host.setPlaceholderText(tr("IP o nome host"))
        self.profile = QComboBox()
        self.profile.addItem(tr("Porte comuni ({n})").format(n=len(net_core.COMMON_PORTS)), "common")
        self.profile.addItem(tr("Le {n} porte più usate").format(n=len(net_core.TOP_PORTS)), "top")
        self.profile.addItem(tr("Intervallo personalizzato"), "custom")
        self.profile.addItem(tr("Tutte le porte TCP (1-65535)"), "all")
        self.ports = QLineEdit("22,80,443,3389,5900,8080")
        self.ports.setEnabled(False)
        self.profile.currentIndexChanged.connect(lambda *_: self.ports.setEnabled(self.profile.currentData() == "custom"))
        form.addRow(tr("Destinazione"), self.host)
        form.addRow(tr("Porte"), self.profile)
        form.addRow(tr("Elenco"), self.ports)
        layout.addLayout(form)
        bar = QHBoxLayout()
        self.start = QPushButton(tr("Avvia la scansione"))
        self.start.clicked.connect(self.scan)
        self.stop_button = QPushButton(tr("Ferma"))
        self.stop_button.setEnabled(False)
        bar.addWidget(self.start)
        bar.addWidget(self.stop_button)
        bar.addStretch(1)
        layout.addLayout(bar)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        self.table = _table([tr("Porta"), tr("Servizio"), tr("Risposta del servizio")], 2)
        self.table.doubleClicked.connect(lambda *_: self.create_session())
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        for text, slot in ((tr("Crea sessione dal servizio…"), self.create_session), (tr("Copia l'elenco"), lambda: _copy_table(self.table))):
            button = QPushButton(text)
            button.clicked.connect(slot)
            actions.addWidget(button)
        actions.addStretch(1)
        self.status = QLabel("")
        actions.addWidget(self.status)
        layout.addLayout(actions)
        self.stop_event = threading.Event()
        self.stop_button.clicked.connect(self.stop_event.set)
        self.relay = _Relay(self)
        self.relay.progress.connect(lambda d, t: (self.progress.setRange(0, t), self.progress.setValue(d)))
        self.relay.found.connect(lambda r: _add_row(self.table, [r.port, r.service, r.banner]))
        self.relay.done.connect(self._done)
        self.relay.failed.connect(self._failed)

    def scan(self) -> None:
        host = self.host.text().strip()
        if not host:
            self.status.setText(tr("Scrivi prima l'host."))
            return
        kind = self.profile.currentData()
        try:
            ports = {"common": net_core.COMMON_PORTS, "top": net_core.TOP_PORTS, "all": range(1, 65536)}.get(kind) or \
                net_core.parse_ports(self.ports.text())
        except ValueError:
            self.status.setText(tr("Elenco di porte non valido (es. 22,80,8000-8100)."))
            return
        self.table.setRowCount(0)
        self.stop_event.clear()
        self.start.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.progress.show()
        self.status.setText(tr("Scansione di {host}…").format(host=host))
        _run(self.relay, lambda: net_core.scan_ports(host, ports, self.relay.progress.emit, self.relay.found.emit,
                                                     self.stop_event))

    def _done(self, results: object) -> None:
        self.start.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.progress.hide()
        self.status.setText(tr("{count} porte aperte").format(count=len(results)))  # type: ignore[arg-type]

    def _failed(self, message: str) -> None:
        self._done([])
        self.status.setText(tr("Errore: {error}").format(error=message))

    def create_session(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        port = int(self.table.item(rows[0].row(), 0).text())
        self.tools.create_session_for(self.host.text().strip(), net_core.SESSION_FOR_PORT.get(port, ""), port)


class ListeningPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.addWidget(_hint(tr("Le porte aperte su questo PC e il programma che le usa.")))
        bar = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("Filtra per porta o programma"))
        self.filter.textChanged.connect(self._apply_filter)
        refresh = QPushButton(tr("Aggiorna"))
        refresh.clicked.connect(self.refresh)
        bar.addWidget(self.filter, 1)
        bar.addWidget(refresh)
        layout.addLayout(bar)
        self.table = _table([tr("Protocollo"), tr("Indirizzo"), tr("Porta"), "PID", tr("Programma")], 4)
        layout.addWidget(self.table, 1)
        self.relay = _Relay(self)
        self.relay.done.connect(self._fill)
        self.relay.failed.connect(lambda m: QMessageBox.warning(self, tr("Porte in ascolto"), m))
        self.loaded = False

    def refresh(self) -> None:
        self.loaded = True
        _run(self.relay, net_core.listening_ports)

    def _fill(self, items: object) -> None:
        self.table.setRowCount(0)
        for item in items:  # type: ignore[union-attr]
            _add_row(self.table, [item.proto, item.address, item.port, item.pid, item.process])
        self._apply_filter()

    def _apply_filter(self) -> None:
        text = self.filter.text().strip().lower()
        for row in range(self.table.rowCount()):
            hay = " ".join(self.table.item(row, c).text().lower() for c in range(self.table.columnCount()))
            self.table.setRowHidden(row, bool(text) and text not in hay)


class WakePage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.addWidget(_hint(tr("Accende un PC spento mandando il «magic packet» sulla rete (la scheda di rete del PC deve "
                                  "avere il Wake-on-LAN attivo).")))
        form = QFormLayout()
        self.mac = QLineEdit()
        self.mac.setPlaceholderText("00:11:22:33:44:55")
        self.broadcast = QLineEdit("255.255.255.255")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(9)
        form.addRow(tr("Indirizzo MAC"), self.mac)
        form.addRow(tr("Broadcast"), self.broadcast)
        form.addRow(tr("Porta UDP"), self.port)
        layout.addLayout(form)
        send = QPushButton(tr("Invia"))
        send.clicked.connect(self.send)
        layout.addWidget(send, 0, Qt.AlignmentFlag.AlignLeft)
        self.status = QLabel("")
        layout.addWidget(self.status)
        layout.addStretch(1)

    def send(self) -> None:
        try:
            net_core.wake_on_lan(self.mac.text(), self.broadcast.text().strip() or "255.255.255.255", self.port.value())
            self.status.setText(tr("Magic packet inviato a {mac}.").format(mac=self.mac.text().strip()))
        except (ValueError, OSError) as exc:
            self.status.setText(tr("Errore: {error}").format(error=exc))


class ServersPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.http: Optional[net_core.HttpFileServer] = None
        self.tftp: Optional[net_core.TftpServer] = None
        settings = QSettings("FedTermHub", "FedTermHub")
        default = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation) or str(Path.home())
        layout = QVBoxLayout(self)
        layout.addWidget(_hint(tr("Server da accendere per pochi minuti: per passare un firmware o una configurazione a uno "
                                  "switch, un firewall o un altro PC. La prima volta Windows chiede se permettere a "
                                  "FedTermHub di usare la rete: senza il permesso gli altri dispositivi non lo vedono.")))

        http_box = QGroupBox(tr("Server HTTP (sola lettura)"))
        http_form = QFormLayout(http_box)
        self.http_folder = QLineEdit(str(settings.value("tools/http_folder", default)))
        self.http_port = QSpinBox()
        self.http_port.setRange(1, 65535)
        self.http_port.setValue(int(settings.value("tools/http_port", 8080)))
        self.http_button = QPushButton(tr("Avvia"))
        self.http_button.clicked.connect(self.toggle_http)
        http_form.addRow(tr("Cartella"), self._folder_row(self.http_folder))
        http_form.addRow(tr("Porta"), self.http_port)
        http_form.addRow("", self.http_button)
        layout.addWidget(http_box)

        tftp_box = QGroupBox(tr("Server TFTP"))
        tftp_form = QFormLayout(tftp_box)
        self.tftp_folder = QLineEdit(str(settings.value("tools/tftp_folder", default)))
        self.tftp_port = QSpinBox()
        self.tftp_port.setRange(1, 65535)
        self.tftp_port.setValue(int(settings.value("tools/tftp_port", 69)))
        self.tftp_write = QCheckBox(tr("Permetti di scrivere file (backup delle configurazioni)"))
        self.tftp_button = QPushButton(tr("Avvia"))
        self.tftp_button.clicked.connect(self.toggle_tftp)
        tftp_form.addRow(tr("Cartella"), self._folder_row(self.tftp_folder))
        tftp_form.addRow(tr("Porta"), self.tftp_port)
        tftp_form.addRow("", self.tftp_write)
        tftp_form.addRow("", self.tftp_button)
        layout.addWidget(tftp_box)

        self.addresses = QLabel("")
        self.addresses.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.addresses.setWordWrap(True)
        layout.addWidget(self.addresses)
        layout.addWidget(QLabel(tr("Richieste ricevute")))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        layout.addWidget(self.log, 1)
        self.relay = _Relay(self)
        self.relay.log.connect(self.log.appendPlainText)

    def _folder_row(self, edit: QLineEdit) -> QWidget:
        row = QWidget()
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(edit, 1)
        browse = QPushButton(tr("Sfoglia…"))
        browse.clicked.connect(lambda: edit.setText(QFileDialog.getExistingDirectory(self, tr("Cartella"), edit.text()) or edit.text()))
        box.addWidget(browse)
        return row

    def _show_addresses(self) -> None:
        import socket

        try:
            ips = sorted({info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                          if not info[4][0].startswith("127.")})
        except OSError:
            ips = []
        lines = []
        if self.http:
            lines += [f"http://{ip}:{self.http.port}/" for ip in ips]
        if self.tftp:
            lines += [f"tftp://{ip}:{self.tftp.port}/" for ip in ips]
        self.addresses.setText(tr("Indirizzi di questo PC: ") + "   ".join(lines) if lines else "")

    def toggle_http(self) -> None:
        if self.http:
            self.http.stop()
            self.http = None
            self.http_button.setText(tr("Avvia"))
            self.relay.log.emit(tr("Server HTTP fermato."))
        else:
            folder = Path(self.http_folder.text())
            if not folder.is_dir():
                QMessageBox.warning(self, tr("Server HTTP"), tr("La cartella non esiste."))
                return
            try:
                self.http = net_core.HttpFileServer(folder, self.http_port.value(), self.relay.log.emit)
            except OSError as exc:
                QMessageBox.warning(self, tr("Server HTTP"), tr("Impossibile usare la porta {port}: {error}").format(
                    port=self.http_port.value(), error=exc))
                return
            settings = QSettings("FedTermHub", "FedTermHub")
            settings.setValue("tools/http_folder", str(folder))
            settings.setValue("tools/http_port", self.http_port.value())
            self.http_button.setText(tr("Ferma"))
            self.relay.log.emit(tr("Server HTTP attivo sulla porta {port}: {folder}").format(port=self.http.port, folder=folder))
        self._show_addresses()

    def toggle_tftp(self) -> None:
        if self.tftp:
            self.tftp.stop()
            self.tftp = None
            self.tftp_button.setText(tr("Avvia"))
            self.relay.log.emit(tr("Server TFTP fermato."))
        else:
            folder = Path(self.tftp_folder.text())
            if not folder.is_dir():
                QMessageBox.warning(self, tr("Server TFTP"), tr("La cartella non esiste."))
                return
            try:
                self.tftp = net_core.TftpServer(folder, self.tftp_port.value(), self.relay.log.emit, self.tftp_write.isChecked())
            except OSError as exc:
                QMessageBox.warning(self, tr("Server TFTP"), tr("Impossibile usare la porta {port}: {error}").format(
                    port=self.tftp_port.value(), error=exc))
                return
            settings = QSettings("FedTermHub", "FedTermHub")
            settings.setValue("tools/tftp_folder", str(folder))
            settings.setValue("tools/tftp_port", self.tftp_port.value())
            self.tftp_button.setText(tr("Ferma"))
            self.relay.log.emit(tr("Server TFTP attivo sulla porta {port}: {folder}").format(port=self.tftp.port, folder=folder))
        self._show_addresses()

    def stop_all(self) -> None:
        if self.http:
            self.toggle_http()
        if self.tftp:
            self.toggle_tftp()


class PingPage(QWidget):
    def __init__(self, tools: "NetworkToolsDialog") -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.addWidget(_hint(tr("Si aprono in una scheda terminale, così restano sotto gli occhi mentre lavori.")))
        form = QFormLayout()
        self.host = QLineEdit()
        self.host.setPlaceholderText(tr("IP o nome host"))
        form.addRow(tr("Destinazione"), self.host)
        layout.addLayout(form)
        bar = QHBoxLayout()
        for text, kind in ((tr("Ping continuo"), "ping"), (tr("Traceroute"), "trace"), (tr("Risoluzione DNS"), "dns")):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, k=kind: tools.run_command(k, self.host.text().strip()))
            bar.addWidget(button)
        bar.addStretch(1)
        layout.addLayout(bar)
        layout.addStretch(1)


class NetworkToolsDialog(QDialog):
    """La finestra degli strumenti di rete. Resta aperta mentre si lavora (non modale)."""

    def __init__(self, parent: QWidget, open_command: Callable[[str, List[str]], None],
                 create_session: Callable[[str, str, int], None]) -> None:
        super().__init__(parent)
        self.open_command = open_command
        self.create_session = create_session
        self.setWindowTitle(tr("Strumenti di rete"))
        self.resize(900, 620)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.network = NetworkScanPage(self)
        self.ports = PortScanPage(self)
        self.listening = ListeningPage()
        self.servers = ServersPage()
        self.tabs.addTab(self.network, tr("Scansione rete"))
        self.tabs.addTab(self.ports, tr("Scansione porte"))
        self.tabs.addTab(self.listening, tr("Porte in ascolto"))
        self.tabs.addTab(WakePage(), "Wake-on-LAN")
        self.tabs.addTab(self.servers, tr("Server temporanei"))
        self.tabs.addTab(PingPage(self), tr("Ping e traceroute"))
        self.tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(self.tabs)

    def _tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.listening and not self.listening.loaded:
            self.listening.refresh()

    def scan_ports_of(self, host: str) -> None:
        self.ports.host.setText(host)
        self.tabs.setCurrentWidget(self.ports)
        self.ports.scan()

    def create_session_for(self, host: str, protocol: str, port: int = 0) -> None:
        if not protocol:
            protocol, ok = QInputDialog.getItem(self, tr("Crea sessione"), tr("Protocollo per {host}:").format(host=host),
                                                ["ssh", "rdp", "vnc", "telnet", "sftp"], 0, False)
            if not ok:
                return
        self.create_session(host, protocol, port)

    def run_command(self, kind: str, host: str) -> None:
        if not host or host.startswith("-") or any(c.isspace() for c in host):
            QMessageBox.information(self, tr("Strumenti di rete"), tr("Scrivi un IP o un nome host valido."))
            return
        if net_core.IS_WINDOWS:
            argv = {"ping": ["ping", "-t", host], "trace": ["tracert", host], "dns": ["nslookup", host]}[kind]
        else:
            argv = {"ping": ["ping", host], "trace": ["tracepath", host], "dns": ["getent", "ahosts", host]}[kind]
        title = {"ping": tr("Ping {host}"), "trace": tr("Traceroute {host}"), "dns": tr("DNS {host}")}[kind].format(host=host)
        self.open_command(title, argv)

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802
        if self.servers.http or self.servers.tftp:
            answer = QMessageBox.question(self, tr("Strumenti di rete"),
                                          tr("I server temporanei verranno fermati. Chiudere?"))
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.servers.stop_all()
        self.network.stop_event.set()
        self.ports.stop_event.set()
        super().closeEvent(event)
