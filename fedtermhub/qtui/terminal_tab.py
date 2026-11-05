# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Scheda terminale dell'interfaccia Qt: uno o più riquadri, come TerminalWorkspace della versione Linux.

Le regole (disposizioni, tasti, broadcast) stanno in termcore.py. Ogni riquadro ha il suo programma, avviato con
lo stesso comando del primo; `view` è sempre il terminale del riquadro attivo.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QLabel, QSplitter, QVBoxLayout, QWidget

from .. import look, model, termcore
from ..i18n import tr
from . import connect
from .files_panel import FilesPanel
from .sftp_client import SftpTarget
from .terminal_view import TerminalView


class TerminalPane(QWidget):
    """Un riquadro: un terminale con il suo programma."""

    def __init__(self, tab: "TerminalTab", launch: connect.Launch, settings: model.AppSettings) -> None:
        super().__init__(tab)
        self.tab = tab
        self.launch = launch
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        font_px = max(9, round(settings.terminal_font_size * 1.33))
        self.view = TerminalView(self, font_size=font_px, scrollback=settings.terminal_scrollback_lines,
                                 blink=settings.terminal_cursor_blink,
                                 process=launch.factory() if launch.factory else None)
        layout.addWidget(self.view)
        self.closing = False
        self.osc7_sent = False
        self.exit_code: Optional[int] = None
        self.start_error = ""
        self.feeder = connect.PasswordFeeder(launch.password)
        self.view.cwd_changed.connect(lambda path: tab.pane_cwd_changed(self, path))
        self.view.input_hooks.append(lambda data: tab.pane_input(self, data))
        self.view.pty.output.connect(self._output)
        self.view.pty.finished.connect(self._finished)
        self.start()

    def start(self) -> None:
        self.exit_code = None
        self.start_error = ""
        self.feeder = connect.PasswordFeeder(self.launch.password)
        try:
            self.view.pty.start(self.launch.argv, str(Path.home()), self.launch.env)
        except Exception as exc:  # noqa: BLE001
            self.start_error = f"{type(exc).__name__}: {exc}"
            self.view.write_local(tr("Errore avvio processo: {error}").format(error=exc) + "\n")
            self.exit_code = -1

    def _output(self, text: str) -> None:
        answer = self.feeder.feed(text)
        if answer:
            self.view.pty.write(answer)
        self.tab.pane_output()

    def _finished(self, code: int) -> None:
        self.exit_code = code
        self.feeder.disarm()
        if not self.closing:
            self.tab.pane_finished(self, code)

    def wait_for_restart(self, code: int) -> None:
        self.view.write_local("\n" + tr("[processo terminato con codice {code} — premi Invio per riaprire]").format(code=code) + "\n")
        self.view.input_hooks.append(self._restart_on_enter)

    def _restart_on_enter(self, data: str) -> None:
        if self.exit_code is not None and data in {"\r", "\n"}:
            self.view.input_hooks.remove(self._restart_on_enter)
            self.view.clear()
            self.start()

    @property
    def running(self) -> bool:
        return self.view.pty.running

    def stop(self) -> None:
        self.closing = True
        self.view.pty.stop()


class TerminalTab(QWidget):
    """Una scheda con un programma in un terminale (SSH, Telnet, PowerShell…), divisibile in riquadri.

    Per le sessioni SSH ha il suo pannello dei file remoti (SFTP), che la finestra mostra nella scheda
    «File remoti» del pannello laterale, come la versione Linux, e che segue la cartella della shell
    quando la shell la dichiara (OSC 7): con più riquadri, solo quella del riquadro attivo.
    """

    finished = Signal(int)
    status = Signal(str)
    files_created = Signal(object)
    files_unavailable = Signal()
    activity = Signal()  # l'utente ha scritto in un riquadro
    close_requested = Signal()  # Ctrl+Shift+W sull'ultimo riquadro

    def __init__(self, parent: QWidget, launch: connect.Launch, settings: model.AppSettings,
                 files_target: Optional[Callable[[], "SftpTarget"]] = None, protocol: str = "") -> None:
        super().__init__(parent)
        self.launch = launch
        self.settings = settings
        self.protocol = protocol
        self.split_tab = False  # nata divisa da Terminale › Terminale diviso
        self.pane_layout = "stack"
        self.broadcast = False
        self.files_target = files_target
        self.files: Optional[FilesPanel] = None
        self.files_missing = False
        self._first_output = False
        self._relaying = False
        self._root: Optional[QWidget] = None
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._toast = QLabel(self)
        self._toast.setStyleSheet(
            f"background: {look.TERMINAL_TOAST['background']}; color: {look.TERMINAL_TOAST['text']}; "
            f"border: 1px solid {look.TERMINAL_TOAST['line']}; border-radius: 8px; padding: 5px 12px; font-size: 12px;")
        self._toast.hide()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._toast.hide)
        first = TerminalPane(self, launch, settings)
        self.panes: List[TerminalPane] = [first]
        self.active_pane = first
        self._relayout()

    # ------------------------------------------------------------------ il riquadro attivo
    @property
    def view(self) -> TerminalView:
        return self.active_pane.view

    @property
    def exit_code(self) -> Optional[int]:
        return self.active_pane.exit_code

    @property
    def start_error(self) -> str:
        return self.active_pane.start_error

    @property
    def running(self) -> bool:
        return any(pane.running for pane in self.panes)

    def set_active(self, pane: TerminalPane) -> None:
        # Mentre i riquadri si spostano il fuoco passa da uno all'altro: non è una scelta dell'utente.
        if not self._relaying and pane in self.panes:
            self.active_pane = pane

    def focus_active(self) -> None:
        self.active_pane.view.focus_terminal()

    # ------------------------------------------------------------------ azioni dei tasti (termcore.PANE_KEYS)
    def dispatch(self, action: str) -> None:
        if action in termcore.SPLIT_LAYOUTS:
            self.split(termcore.SPLIT_LAYOUTS[action])
        elif action == "new_pane":
            self.add_pane()
        elif action == "close_pane":
            self.close_pane(self.active_pane)
        elif action == "next_pane":
            self.cycle_pane(1)
        elif action == "prev_pane":
            self.cycle_pane(-1)
        elif action == "cycle_layout":
            self.cycle_layout()
        elif action == "toggle_broadcast":
            self.toggle_broadcast()

    def split(self, layout: str) -> None:
        self.pane_layout = layout
        self.add_pane()

    def add_pane(self) -> TerminalPane:
        """Un riquadro nuovo con lo stesso comando (una sessione SSH si apre una seconda volta)."""
        pane = TerminalPane(self, self.launch, self.settings)
        self.panes.append(pane)
        self.active_pane = pane
        self.pane_layout = termcore.layout_after_add(self.pane_layout, len(self.panes))
        self._relayout()
        return pane

    def close_pane(self, pane: TerminalPane) -> None:
        """Chiude un riquadro; l'ultimo chiude la scheda."""
        if pane not in self.panes:
            return
        if len(self.panes) == 1:
            self.close_requested.emit()
            return
        self.panes.remove(pane)
        if self.active_pane is pane:
            self.active_pane = self.panes[0]
        self._discard(pane)
        self._relayout()

    def cycle_pane(self, step: int) -> None:
        index = self.panes.index(self.active_pane)
        self.active_pane = self.panes[(index + step) % len(self.panes)]
        if self.pane_layout == "stack":
            self._relayout()
        else:
            self.focus_active()

    def cycle_layout(self) -> None:
        self.pane_layout = termcore.next_layout(self.pane_layout)
        self._relayout()
        self._show_toast(tr("Layout: {layout}").format(layout=self.pane_layout))

    def toggle_broadcast(self) -> None:
        self.broadcast = not self.broadcast
        self._show_toast(tr("Broadcast attivo: l'input va a tutti i riquadri") if self.broadcast
                         else tr("Broadcast disattivato"))

    def feed_all(self, text: str) -> int:
        """Scrive `text` in tutti i riquadri aperti (MultiExec); quanti l'hanno ricevuto."""
        sent = 0
        for pane in self.panes:
            if pane.running:
                pane.view.pty.write(text)
                sent += 1
        return sent

    # ------------------------------------------------------------------ disposizione
    def _build(self, plan: termcore.Plan) -> QWidget:
        if isinstance(plan, int):
            return self.panes[plan]
        direction, parts = plan
        splitter = QSplitter(Qt.Orientation.Horizontal if direction == "h" else Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(3)
        for part in parts:
            splitter.addWidget(self._build(part))
        splitter.setSizes([100_000] * len(parts))
        return splitter

    def _relayout(self) -> None:
        self._relaying = True
        try:
            old = self._root
            if old is not None:
                self._outer.removeWidget(old)
            for pane in self.panes:
                pane.hide()
                pane.setParent(self)  # fuori dai divisori vecchi, ma con il programma vivo
            if isinstance(old, QSplitter):
                old.hide()
                old.deleteLater()
            active = self.panes.index(self.active_pane)
            plan = termcore.layout_plan(self.pane_layout, len(self.panes), active)
            self._root = self._build(plan)
            self._outer.addWidget(self._root)
            self._root.show()
            visible = [plan] if isinstance(plan, int) else range(len(self.panes))
            for index in visible:
                self.panes[index].show()
            self._toast.raise_()
        finally:
            self._relaying = False
        self.focus_active()

    def _discard(self, pane: TerminalPane) -> None:
        pane.stop()
        pane.hide()
        pane.setParent(None)
        pane.deleteLater()

    def _show_toast(self, text: str) -> None:
        """Un piccolo avviso in alto a destra che scompare da solo."""
        self._toast.setText(text)
        self._toast.adjustSize()
        self._toast.move(max(0, self.width() - self._toast.width() - 12), 10)
        self._toast.show()
        self._toast.raise_()
        self._toast_timer.start(1800)

    # ------------------------------------------------------------------ dai riquadri
    def pane_input(self, pane: TerminalPane, data: str) -> None:
        self.set_active(pane)
        self.activity.emit()
        if self.broadcast and len(self.panes) > 1 and termcore.broadcastable(data):
            for other in self.panes:
                if other is not pane and other.running:
                    other.view.pty.write(data)

    def pane_output(self) -> None:
        if not self._first_output and self.files_target is not None and self.settings.show_remote_files:
            # Il pannello dei file si collega poco dopo il terminale: se la chiave dell'host è nuova la
            # conferma prima ssh nel terminale e il pannello la trova già in known_hosts.
            self._first_output = True
            QTimer.singleShot(2500, self.ensure_files)

    def pane_finished(self, pane: TerminalPane, code: int) -> None:
        if len(self.panes) > 1:
            self.close_pane(pane)
            return
        pane.wait_for_restart(code)
        self.finished.emit(code)

    def pane_cwd_changed(self, pane: TerminalPane, path: str) -> None:
        # Solo il riquadro attivo guida i file: un comando in secondo piano non deve spostarli.
        if pane is self.active_pane and self.files is not None:
            self.files.follow_directory(path)

    # ------------------------------------------------------------------ file remoti
    @property
    def has_files(self) -> bool:
        return self.files_target is not None and not self.files_missing

    def ensure_files(self) -> Optional[FilesPanel]:
        """Crea e collega il pannello dei file remoti, se non c'è ancora (la finestra lo mette nel pannello laterale)."""
        if not self.has_files:
            return None
        if self.files is None:
            self.files = FilesPanel(self.files_target(), None, follow_terminal=self.settings.follow_remote_directory,
                                    compact=True, ask_password=False)
            self.files.status.connect(self.status)
            self.files.connected_changed.connect(self._files_connected)
            self.files.sftp_missing.connect(self._sftp_missing)
            self.files.follow_button.toggled.connect(self._follow_toggled)
            if self.view.last_cwd:
                self.files.pending_follow = self.view.last_cwd
            self.files_created.emit(self.files)
            self.files.connect_server()
        return self.files

    def _follow_toggled(self, active: bool) -> None:
        self.settings.follow_remote_directory = active
        if active:
            self._schedule_osc7()

    def _files_connected(self, ok: bool) -> None:
        if ok:
            self._schedule_osc7()

    def _sftp_missing(self, message: str) -> None:
        self.files_missing = True
        self.files_unavailable.emit()
        self.status.emit(tr("{message} Pannello dei file chiuso.").format(message=message))

    def _schedule_osc7(self) -> None:
        # Solo se il server ha SFTP (quindi una shell Unix, non un apparato) e la shell non dichiara già la
        # cartella da sola: la riga si scrive nella shell remota, come fa la versione Linux. Si scrive nel
        # primo riquadro, già connesso quando i file si collegano: in uno appena aperto potrebbe finire al
        # prompt della password.
        pane = self.panes[0]
        if pane.osc7_sent or pane.view.last_cwd or not self.settings.follow_remote_directory:
            return
        if self.files is not None and re.match(r"^/?[A-Za-z]:", self.files.home or ""):
            return  # server Windows (OpenSSH di Windows): la shell è cmd o PowerShell, niente riga per bash
        QTimer.singleShot(1800, self._send_osc7)

    def _send_osc7(self) -> None:
        pane = self.panes[0] if self.panes else None
        if pane is None or pane.osc7_sent or pane.view.last_cwd or not pane.running:
            return
        pane.osc7_sent = True
        pane.view.pty.write(connect.OSC7_BOOTSTRAP)

    # ------------------------------------------------------------------ fine
    def send_theme(self) -> None:
        for pane in self.panes:
            pane.view.send_theme()

    def close_tab(self) -> None:
        for pane in self.panes:
            pane.stop()
        if self.files is not None:
            self.files.shutdown()
            self.files.deleteLater()
