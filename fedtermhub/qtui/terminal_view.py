# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Terminale dell'interfaccia Qt: xterm.js in una pagina locale, collegato a un PtyProcess.

Ripreso dal terminale di Helpix. Il testo del programma arriva da self.pty, i tasti vanno a self.pty;
copia e incolla passano dagli appunti di Qt.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, List

from PySide6.QtCore import QObject, QUrl, QUrlQuery, Signal, Slot
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView

from . import theme
from .pty_process import PtyProcess

PAGE = Path(__file__).resolve().parent / "xterm" / "terminal.html"


class _Bridge(QObject):
    write = Signal(str)
    reset = Signal()
    themeChanged = Signal(str)  # noqa: N815 (nomi usati dalla pagina)
    fontChanged = Signal(int)  # noqa: N815
    focusTerminal = Signal()  # noqa: N815

    def __init__(self, view: "TerminalView"):
        super().__init__(view)
        self.view = view

    @Slot(str)
    def input(self, data: str) -> None:
        self.view.on_input(data)

    @Slot(int, int)
    def resized(self, cols: int, rows: int) -> None:
        self.view.pty.resize(cols, rows)

    @Slot(str)
    def copy(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)

    @Slot()
    def pasteClipboard(self) -> None:  # noqa: N802
        self.pasteText(QGuiApplication.clipboard().text())

    @Slot(str)
    def pasteText(self, text: str) -> None:  # noqa: N802
        if text:
            self.view.pty.paste(text)

    @Slot(str)
    def titleChanged(self, title: str) -> None:  # noqa: N802
        self.view.title_changed.emit(title)

    @Slot(str)
    def cwdChanged(self, uri: str) -> None:  # noqa: N802
        self.view._on_cwd(uri)

    @Slot()
    def ready(self) -> None:
        self.view._on_ready()


class TerminalView(QWebEngineView):
    """Mostra un PtyProcess. Segnali: output (testo dal programma), finished (codice di uscita)."""

    title_changed = Signal(str)
    cwd_changed = Signal(str)  # cartella corrente dichiarata dalla shell (OSC 7)

    def __init__(self, parent=None, font_size: int = 13, scrollback: int = 10000, blink: bool = True, process=None):
        super().__init__(parent)
        # Il programma dietro al terminale: un PtyProcess, oppure una connessione con la stessa interfaccia
        # (Telnet, porta seriale: stream_process.py).
        if process is not None:
            process.setParent(self)
        self.pty = process if process is not None else PtyProcess(self)
        self._ready = False
        self._pending: List[str] = []
        self.font_size = font_size
        self.input_hooks: List[Callable[[str], None]] = []
        self.last_cwd = ""
        page = QWebEnginePage(QWebEngineProfile.defaultProfile(), self)
        self.setPage(page)
        self.bridge = _Bridge(self)
        self.channel = QWebChannel(page)
        self.channel.registerObject("bridge", self.bridge)
        page.setWebChannel(self.channel)
        page.setBackgroundColor(QColor(theme.colors()["term"]))
        self.pty.output.connect(self._output)
        url = QUrl.fromLocalFile(str(PAGE))
        query = QUrlQuery()
        query.addQueryItem("size", str(font_size))
        query.addQueryItem("scrollback", str(scrollback))
        query.addQueryItem("blink", "1" if blink else "0")
        url.setQuery(query)
        self.setUrl(url)

    # -- pagina -------------------------------------------------------------------------------
    def _on_ready(self) -> None:
        self._ready = True
        self.send_theme()
        if self._pending:
            self.bridge.write.emit("".join(self._pending))
            self._pending.clear()

    @property
    def ready(self) -> bool:
        return self._ready

    def _on_cwd(self, uri: str) -> None:
        from urllib.parse import unquote, urlparse

        parsed = urlparse(uri)
        path = unquote(parsed.path) if parsed.scheme == "file" else ""
        if path:
            self.last_cwd = path
            self.cwd_changed.emit(path)

    def send_theme(self) -> None:
        self.page().setBackgroundColor(QColor(theme.colors()["term"]))
        if self._ready:
            self.bridge.themeChanged.emit(json.dumps(theme.terminal_theme()))

    def _output(self, text: str) -> None:
        if self._ready:
            self.bridge.write.emit(text)
        else:
            self._pending.append(text)

    def on_input(self, data: str) -> None:
        for hook in self.input_hooks:
            hook(data)
        self.pty.write(data)

    # -- azioni -------------------------------------------------------------------------------
    def write_local(self, text: str) -> None:
        """Scrive nel terminale un messaggio di FedTermHub (non va al programma)."""
        self._output(text.replace("\r\n", "\n").replace("\n", "\r\n"))

    def clear(self) -> None:
        if self._ready:
            self.bridge.reset.emit()
        self._pending.clear()

    def set_font_size(self, size: int) -> None:
        self.font_size = max(7, min(32, size))
        if self._ready:
            self.bridge.fontChanged.emit(self.font_size)

    def focus_terminal(self) -> None:
        self.setFocus()
        if self._ready:
            self.bridge.focusTerminal.emit()

    def screen_text(self, callback: Callable[[str], None], lines: int = 200) -> None:
        """Le ultime righe del terminale come testo semplice."""
        if not self._ready:
            callback("")
            return
        self.page().runJavaScript(f"window.fthScreen ? fthScreen({int(lines)}) : ''", lambda r: callback(r or ""))

    def find(self, text: str, callback: Callable[[bool], None]) -> None:
        if not self._ready:
            callback(False)
            return
        self.page().runJavaScript(f"window.fthFind ? fthFind({json.dumps(text)}) : false",
                                  lambda r: callback(bool(r)))
