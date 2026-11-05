# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Desktop VNC nella scheda: noVNC in Qt WebEngine, collegato al server con il ponte locale (ws_bridge).

La password VNC arriva dalla credenziale collegata alla sessione (la versione Linux la fa chiedere a
TigerVNC); se manca o è sbagliata si chiede in una finestra. Gli appunti vanno nei due sensi.
"""
from __future__ import annotations

import json
from typing import List, Optional

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QColor, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import model
from ..i18n import tr
from .ws_bridge import VncBridge


class _Bridge(QObject):
    start = Signal(str)
    credentials = Signal(str)
    ctrlAltDel = Signal()  # noqa: N815
    setScale = Signal(bool)  # noqa: N815
    setViewOnly = Signal(bool)  # noqa: N815
    pasteText = Signal(str)  # noqa: N815
    sendKeys = Signal(str)  # noqa: N815
    stop = Signal()
    focusScreen = Signal()  # noqa: N815

    def __init__(self, tab: "VncTab") -> None:
        super().__init__(tab)
        self.tab = tab

    @Slot()
    def ready(self) -> None:
        self.tab._page_ready()

    @Slot(str, str)
    def state(self, state: str, detail: str) -> None:
        self.tab._state(state, detail)

    @Slot(str)
    def needCredentials(self, types: str) -> None:  # noqa: N802
        self.tab._need_credentials(json.loads(types))

    @Slot(str)
    def clipboard(self, text: str) -> None:
        if text:
            QGuiApplication.clipboard().setText(text)

    @Slot(str)
    def desktopName(self, name: str) -> None:  # noqa: N802
        self.tab.desktop_name = name
        self.tab.title_changed.emit(name)


class VncTab(QWidget):
    """Una scheda VNC. Segnali: status (testo per la barra di stato), title_changed (nome del desktop)."""

    status = Signal(str)
    title_changed = Signal(str)

    def __init__(self, parent: Optional[QWidget], sess: model.Session, user: str, password: Optional[str]) -> None:
        super().__init__(parent)
        self.sess = sess
        self.user = user
        self.password = password
        self.password_tried = False
        self.connected = False
        self.desktop_name = ""
        self.events: List[str] = []
        host, port = vnc_endpoint(sess.host, sess.port)
        self.host, self.port = host, port
        self.bridge_server = VncBridge(host, port, self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 4, 8, 4)
        self.state_label = QLabel(tr("Connessione a {host}:{port}…").format(host=host, port=port))
        self.state_label.setProperty("role", "muted")
        bar.addWidget(self.state_label, 1)
        self.scale = QCheckBox(tr("Adatta alla finestra"))
        self.scale.setChecked(True)
        self.scale.toggled.connect(lambda v: self.page_bridge.setScale.emit(v))
        self.view_only = QCheckBox(tr("Solo visualizzazione"))
        self.view_only.toggled.connect(lambda v: self.page_bridge.setViewOnly.emit(v))
        cad = QPushButton(tr("Ctrl+Alt+Canc"))
        cad.clicked.connect(lambda: self.page_bridge.ctrlAltDel.emit())
        paste = QPushButton(tr("Invia appunti"))
        paste.setToolTip(tr("Manda il testo degli appunti di questo PC agli appunti del desktop remoto"))
        paste.clicked.connect(lambda: self.page_bridge.pasteText.emit(QGuiApplication.clipboard().text()))
        self.fullscreen_button = QPushButton(tr("Schermo intero"))
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        self.reconnect_button = QPushButton(tr("Riconnetti"))
        self.reconnect_button.clicked.connect(self.reconnect)
        self.reconnect_button.hide()
        for widget in (self.scale, self.view_only, cad, paste, self.fullscreen_button, self.reconnect_button):
            bar.addWidget(widget)
        self.bar = QWidget()
        self.bar.setLayout(bar)
        layout.addWidget(self.bar)

        self.view = QWebEngineView(self)
        page = QWebEnginePage(QWebEngineProfile.defaultProfile(), self.view)
        page.setBackgroundColor(QColor("#1b1d22"))
        self.view.setPage(page)
        self.page_bridge = _Bridge(self)
        self.channel = QWebChannel(page)
        self.channel.registerObject("bridge", self.page_bridge)
        page.setWebChannel(self.channel)
        layout.addWidget(self.view, 1)
        self._fs_window: Optional[QWidget] = None
        self.view.load(self.bridge_server.base_url + "vnc.html")

    # ------------------------------------------------------------------ pagina
    def _page_ready(self) -> None:
        self.connect_vnc()

    def connect_vnc(self) -> None:
        self.reconnect_button.hide()
        self.page_bridge.start.emit(json.dumps({"scale": self.scale.isChecked(), "viewOnly": self.view_only.isChecked()}))

    def reconnect(self) -> None:
        self.password_tried = False
        self.connect_vnc()

    def _state(self, state: str, detail: str) -> None:
        self.events.append(f"{state}:{detail}" if detail else state)
        if state == "connecting":
            self.state_label.setText(tr("Connessione a {host}:{port}…").format(host=self.host, port=self.port))
        elif state == "connected":
            self.connected = True
            self.state_label.setText(tr("Connesso a {host}:{port}").format(host=self.host, port=self.port))
            self.status.emit(tr("Sessione VNC attiva"))
            self.page_bridge.focusScreen.emit()
        elif state == "securityfailure":
            self.state_label.setText(tr("Accesso VNC rifiutato: {reason}").format(reason=detail))
            self.password = None  # alla prossima prova si richiede
        elif state == "disconnected":
            self.connected = False
            if detail == "clean":
                text = tr("Sessione VNC chiusa")
            elif "securityfailure" in "".join(self.events[-2:]):
                text = self.state_label.text()
            else:
                text = tr("Disconnesso: il server VNC non risponde o ha chiuso la connessione ({host}:{port})").format(
                    host=self.host, port=self.port)
            self.state_label.setText(text)
            self.reconnect_button.show()
            self.status.emit(text)

    def _need_credentials(self, types: List[str]) -> None:
        creds = {}
        password = self.password if not self.password_tried else None
        user = self.user
        if "username" in types and not user:
            user, ok = QInputDialog.getText(self, tr("Accesso VNC"), tr("Utente per {host}:").format(host=self.host))
            if not ok:
                self.page_bridge.stop.emit()
                return
        if password is None and "password" in types:
            password, ok = QInputDialog.getText(self, tr("Password VNC"), tr("Password VNC per {host}:").format(host=self.host),
                                                QLineEdit.EchoMode.Password)
            if not ok:
                self.page_bridge.stop.emit()
                return
        self.password_tried = True
        if "username" in types:
            creds["username"] = user
        if "password" in types:
            creds["password"] = password or ""
        if "target" in types:
            creds["target"] = ""
        self.page_bridge.credentials.emit(json.dumps(creds))

    # ------------------------------------------------------------------ schermo intero
    def toggle_fullscreen(self) -> None:
        if self._fs_window is None:
            window = QWidget()
            window.setWindowTitle(f"{self.sess.name} — FedTermHub")
            fs_layout = QVBoxLayout(window)
            fs_layout.setContentsMargins(0, 0, 0, 0)
            fs_layout.addWidget(self.view)
            QShortcut(QKeySequence("Ctrl+Alt+Return"), window, activated=self.toggle_fullscreen)
            self._fs_window = window
            window.showFullScreen()
            self.status.emit(tr("Schermo intero: Ctrl+Alt+Invio per tornare alla scheda"))
        else:
            self.layout().addWidget(self.view, 1)
            self._fs_window.close()
            self._fs_window.deleteLater()
            self._fs_window = None
        self.page_bridge.focusScreen.emit()

    def close_tab(self) -> None:
        self.page_bridge.stop.emit()
        if self._fs_window is not None:
            self.toggle_fullscreen()
        self.bridge_server.close()


def vnc_endpoint(host: str, port: str):  # noqa: ANN201
    """Host e porta TCP del server VNC, con la regola di model.vnc_target: un numero sotto 100 è un display
    (porta 5900 + n), da 100 in su è la porta. L'host può contenere «:display» o «:porta»."""
    host, from_host = model.split_host_port(host or "")
    value = (port or "").strip().lstrip(":") or from_host
    if not value.isdigit():
        return host, 5900
    number = int(value)
    return host, (5900 + number if number < 100 else number)
