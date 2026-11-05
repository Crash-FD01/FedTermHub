# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Finestra principale dell'interfaccia Qt: sessioni in cartelle a sinistra, schede a destra."""
from __future__ import annotations

import json
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCursor, QDesktopServices, QIcon, QKeySequence, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QToolBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .. import model
from ..i18n import tr, trn
from . import connect, theme, updates
from .rdp_tab import RdpTab
from .session_dialog import AVAILABLE, SessionDialog
from .tunnels import TunnelManager, TunnelsDialog
from .net_tools import NetworkToolsDialog
from .vnc_tab import VncTab
from .settings_dialog import SettingsDialog
from .terminal_view import TerminalView
from .files_panel import FilesPanel
from .sftp_client import SftpTarget
from .vault_dialogs import MasterPasswordDialog, VaultManagerDialog

ICONS = Path(__file__).resolve().parent.parent / "icons"
PATH_ROLE = Qt.ItemDataRole.UserRole + 1
KIND_ROLE = Qt.ItemDataRole.UserRole + 2
PROTOCOL_ICONS = {
    "ssh": "fth-proto-ssh", "rdp": "fth-proto-rdp", "vnc": "fth-proto-vnc", "sftp": "fth-proto-sftp",
    "telnet": "fth-proto-telnet", "serial": "fth-proto-serial", "local": "fth-proto-local",
    "command": "fth-proto-command",
}


def icon(name: str) -> QIcon:
    svg = ICONS / f"{name}.svg"
    if svg.is_file():
        return theme.themed_icon(svg)
    png = ICONS / f"{name}.png"
    return QIcon(str(png)) if png.is_file() else QIcon()


def app_icon() -> QIcon:
    """L'icona del programma: accanto ai moduli nella versione installata, in share/ nei sorgenti."""
    name = "io.github.crash_fd01.fedtermhub.png"
    here = Path(__file__).resolve().parent
    for candidate in (here / name, here.parents[1] / "share" / "icons" / "hicolor" / "256x256" / "apps" / name):
        if candidate.is_file():
            return QIcon(str(candidate))
    return QIcon()


class TerminalTab(QWidget):
    """Una scheda con un programma in un terminale (SSH, Telnet, PowerShell…).

    Per le sessioni SSH può mostrare accanto il pannello dei file remoti (SFTP), che segue la cartella
    della shell quando la shell la dichiara (OSC 7).
    """

    finished = Signal(int)
    status = Signal(str)

    def __init__(self, parent: QWidget, launch: connect.Launch, settings: model.AppSettings,
                 files_target: Optional[Callable[[], "SftpTarget"]] = None) -> None:
        super().__init__(parent)
        self.launch = launch
        self.settings = settings
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        font_px = max(9, round(settings.terminal_font_size * 1.33))
        self.view = TerminalView(self, font_size=font_px, scrollback=settings.terminal_scrollback_lines,
                                 blink=settings.terminal_cursor_blink,
                                 process=launch.factory() if launch.factory else None)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.view)
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.splitter)
        self.files_target = files_target
        self.files: Optional[FilesPanel] = None
        self._osc7_sent = False
        self._first_output = False
        self.view.cwd_changed.connect(self._cwd_changed)
        self.feeder = connect.PasswordFeeder(launch.password)
        self.view.pty.output.connect(self._output)
        self.view.pty.finished.connect(self._finished)
        self.exit_code: Optional[int] = None
        self.start()

    # ------------------------------------------------------------------ file remoti
    @property
    def has_files(self) -> bool:
        return self.files_target is not None

    def toggle_files(self, show: Optional[bool] = None) -> None:
        if self.files_target is None:
            return
        if self.files is None:
            if show is False:
                return
            self.files = FilesPanel(self.files_target(), self, follow_terminal=self.settings.follow_remote_directory,
                                    compact=True, ask_password=False)
            self.files.status.connect(self.status)
            self.files.connected_changed.connect(self._files_connected)
            self.files.sftp_missing.connect(self._sftp_missing)
            self.files.follow_button.toggled.connect(self._follow_toggled)
            self.splitter.addWidget(self.files)
            width = max(600, self.width())
            self.splitter.setSizes([int(width * 0.68), int(width * 0.32)])
            if self.view.last_cwd:
                self.files.pending_follow = self.view.last_cwd
            self.files.connect_server()
            return
        self.files.setVisible(not self.files.isVisible() if show is None else show)

    def _follow_toggled(self, active: bool) -> None:
        self.settings.follow_remote_directory = active
        if active:
            self._schedule_osc7()

    def _files_connected(self, ok: bool) -> None:
        if ok:
            self._schedule_osc7()

    def _sftp_missing(self, message: str) -> None:
        if self.files is not None:
            self.files.hide()
        self.status.emit(tr("{message} Pannello dei file chiuso.").format(message=message))

    def _schedule_osc7(self) -> None:
        # Solo se il server ha SFTP (quindi una shell Unix, non un apparato) e la shell non dichiara già la
        # cartella da sola: la riga si scrive nella shell remota, come fa la versione Linux.
        if self._osc7_sent or self.view.last_cwd or not self.settings.follow_remote_directory:
            return
        if self.files is not None and re.match(r"^/?[A-Za-z]:", self.files.home or ""):
            return  # server Windows (OpenSSH di Windows): la shell è cmd o PowerShell, niente riga per bash
        QTimer.singleShot(1800, self._send_osc7)

    def _send_osc7(self) -> None:
        if self._osc7_sent or self.view.last_cwd or not self.running:
            return
        self._osc7_sent = True
        self.view.pty.write(connect.OSC7_BOOTSTRAP)

    def _cwd_changed(self, path: str) -> None:
        if self.files is not None:
            self.files.follow_directory(path)

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
        if not self._first_output and self.files_target is not None and self.settings.show_remote_files:
            # Il pannello dei file si collega poco dopo il terminale: se la chiave dell'host è nuova la
            # conferma prima ssh nel terminale e il pannello la trova già in known_hosts.
            self._first_output = True
            QTimer.singleShot(2500, lambda: self.toggle_files(True) if self.files is None else None)

    def _finished(self, code: int) -> None:
        self.exit_code = code
        self.feeder.disarm()
        self.view.write_local("\n" + tr("[processo terminato con codice {code} — premi Invio per riaprire]").format(code=code) + "\n")
        self.view.input_hooks.append(self._restart_on_enter)
        self.finished.emit(code)

    def _restart_on_enter(self, data: str) -> None:
        if self.exit_code is not None and data in {"\r", "\n"}:
            self.view.input_hooks.remove(self._restart_on_enter)
            self.view.clear()
            self.start()

    @property
    def running(self) -> bool:
        return self.view.pty.running

    def close_tab(self) -> None:
        self.view.pty.stop()
        if self.files is not None:
            self.files.shutdown()


def _system_idle_seconds() -> Optional[float]:
    """Da quanto tempo l'utente non tocca tastiera e mouse (Windows: GetLastInputInfo), None altrove."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):  # type: ignore[attr-defined]
            return None
        now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF  # type: ignore[attr-defined]
        return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:  # noqa: BLE001
        return None


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        model.ensure_private_config_dir()
        self.settings = model.AppSettings.load(model.SETTINGS_FILE)
        self.store = model.SessionStore(model.SESSIONS_FILE)
        self.vault = model.PasswordVault(model.VAULT_FILE)
        self.tunnels = TunnelManager(self.settings, self.tunnel_identity, self)
        self.tunnels.changed.connect(self._tunnels_changed)
        self.last_activity = time.monotonic()
        self.setWindowTitle(f"{model.APP_NAME} {model.APP_VERSION}")
        self.setWindowIcon(app_icon())
        self.resize(self.settings.window_width, self.settings.window_height)
        self._build_actions()
        self._build_ui()
        self.refresh_tree()
        # Niente filtro degli eventi su tutta l'applicazione: con QtWebEngine fa cadere PySide6.
        # L'attività si ricava dall'inattività del sistema (Windows) o dai tasti nei terminali e dal mouse.
        self._last_cursor = QCursor.pos()
        self.lock_timer = QTimer(self)
        self.lock_timer.setInterval(30_000)
        self.lock_timer.timeout.connect(self._auto_lock)
        self.lock_timer.start()
        if load_warning := getattr(self.store, "load_warning", ""):
            QTimer.singleShot(300, lambda: QMessageBox.warning(self, tr("Sessioni non leggibili"), load_warning))
        if any(t.auto_start for t in self.tunnels.store.tunnels):
            QTimer.singleShot(1500, self.tunnels.start_auto)
        if self.settings.check_updates and updates.enabled():
            QTimer.singleShot(4000, self._check_updates)

    # ------------------------------------------------------------------ interfaccia
    def _action(self, text: str, slot, shortcut: str = "", icon_name: str = "", tip: str = "") -> QAction:  # noqa: ANN001
        act = QAction(icon(icon_name) if icon_name else QIcon(), text, self)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
        if tip:
            act.setToolTip(tip)
        act.triggered.connect(lambda *_: slot())
        return act

    def _build_actions(self) -> None:
        self.act_new_session = self._action(tr("Nuova sessione"), self.new_session, "Ctrl+Shift+N", "fth-act-new")
        self.act_new_folder = self._action(tr("Nuova cartella"), self.new_folder, "", "fth-act-folder-new")
        self.act_local = self._action(tr("Terminale locale"), self.open_local_terminal, "Ctrl+Shift+T", "fth-tb-terminale")
        self.act_quick = self._action(tr("Connessione rapida"), self.quick_connect, "Ctrl+L", "fth-tb-rete")
        self.act_edit = self._action(tr("Modifica"), self.edit_selected, "F2", "fth-act-edit")
        self.act_duplicate = self._action(tr("Duplica"), self.duplicate_selected, "Ctrl+D", "fth-act-duplicate")
        self.act_move = self._action(tr("Sposta in cartella…"), self.move_selected)
        self.act_delete = self._action(tr("Elimina"), self.delete_selected, "Delete", "fth-act-delete")
        self.act_vault = self._action(tr("Password Manager"), self.open_vault_manager, "Ctrl+Shift+P", "fth-tb-password")
        self.act_lock = self._action(tr("Blocca password"), self.lock_vault)
        self.act_settings = self._action(tr("Impostazioni"), self.open_settings, "Ctrl+,", "fth-tb-sistema")
        self.act_net_tools = self._action(tr("Strumenti di rete"), self.open_network_tools, "Ctrl+Shift+R", "fth-tb-rete")
        self.act_tunnels = self._action(tr("Tunnel SSH"), self.open_tunnels, "Ctrl+Shift+U", "fth-tb-rete")
        self.act_files = self._action(tr("File remoti"), self.toggle_remote_files, "Ctrl+Shift+E", "fth-act-files",
                                      tr("Mostra o nasconde i file remoti accanto al terminale SSH"))
        self.act_import = self._action(tr("Importa sessioni…"), self.import_sessions)
        self.act_export = self._action(tr("Esporta selezione…"), self.export_selected)
        self.act_export_vault = self._action(tr("Esporta backup cifrato delle password"), self.export_vault)
        self.act_import_vault = self._action(tr("Importa backup cifrato delle password"), self.import_vault)
        self.act_close_tab = self._action(tr("Chiudi la scheda"), self.close_current_tab, "Ctrl+W")
        self.act_quit = self._action(tr("Esci"), self.close, "Ctrl+Q")
        self.act_about = self._action(tr("Informazioni"), self.about)
        self.act_guide = self._action(tr("Guida"), lambda: QDesktopServices.openUrl(
            QUrl("https://github.com/Crash-FD01/FedTermHub/blob/main/docs/GUIDA.md")))

    def _build_ui(self) -> None:
        menubar = self.menuBar()
        file_menu = menubar.addMenu(tr("File"))
        for act in (self.act_import, self.act_export, None, self.act_export_vault, self.act_import_vault, None, self.act_quit):
            file_menu.addSeparator() if act is None else file_menu.addAction(act)
        sessions_menu = menubar.addMenu(tr("Sessioni"))
        for act in (self.act_new_session, self.act_new_folder, self.act_quick, self.act_local, self.act_files, None, self.act_edit,
                    self.act_duplicate, self.act_move, self.act_delete, None, self.act_close_tab):
            sessions_menu.addSeparator() if act is None else sessions_menu.addAction(act)
        tools_menu = menubar.addMenu(tr("Strumenti"))
        for act in (self.act_net_tools, self.act_tunnels, None, self.act_vault, self.act_lock, None, self.act_settings):
            tools_menu.addSeparator() if act is None else tools_menu.addAction(act)
        help_menu = menubar.addMenu(tr("Aiuto"))
        help_menu.addAction(self.act_guide)
        help_menu.addAction(self.act_about)

        toolbar = QToolBar(tr("Comandi"))
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(20, 20))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        for act in (self.act_new_session, self.act_local, self.act_quick, self.act_files, self.act_vault, self.act_settings):
            toolbar.addAction(act)
        self.addToolBar(toolbar)

        splitter = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(6, 6, 0, 6)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Cerca sessioni…"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh_tree)
        left_layout.addWidget(self.search)
        self.tree_model = QStandardItemModel(self)
        self.tree = QTreeView()
        self.tree.setModel(self.tree_model)
        self.tree.setHeaderHidden(True)
        self.tree.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.doubleClicked.connect(lambda index: self.open_path(index.data(PATH_ROLE)))
        self.tree.activated.connect(lambda index: self.open_path(index.data(PATH_ROLE)))
        self.tree.pressed.connect(self.mark_activity)
        left_layout.addWidget(self.tree, 1)
        hint = QLabel(tr("Doppio clic per aprire una sessione"))
        hint.setProperty("role", "muted")
        left_layout.addWidget(hint)
        splitter.addWidget(left)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.welcome = self._welcome_page()
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.welcome)
        right_layout.addWidget(self.tabs)
        splitter.addWidget(right)
        splitter.setSizes([max(200, self.settings.sidebar_width), 1000])
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)
        self._update_welcome()
        self.tunnel_status = QLabel("")
        self.tunnel_status.setProperty("role", "muted")
        self.statusBar().addPermanentWidget(self.tunnel_status)
        self.statusBar().showMessage(tr("Pronto."))
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.search.setFocus)

    def _welcome_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(1)
        title = QLabel(f"{model.APP_NAME}")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 26px; font-weight: 600;")
        sub = QLabel(tr("Scegli una sessione a sinistra, oppure:"))
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setProperty("role", "muted")
        layout.addWidget(title)
        layout.addWidget(sub)
        row = QHBoxLayout()
        row.addStretch(1)
        for text, slot in ((tr("Nuova sessione"), self.new_session), (tr("Terminale locale"), self.open_local_terminal),
                           (tr("Connessione rapida"), self.quick_connect), (tr("Importa sessioni…"), self.import_sessions)):
            button = QPushButton(text)
            button.clicked.connect(lambda *_, s=slot: s())
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(2)
        return page

    def _update_welcome(self) -> None:
        empty = self.tabs.count() == 0
        self.welcome.setVisible(empty)
        self.tabs.setVisible(not empty)

    def set_status(self, text: str, ms: int = 6000) -> None:
        self.statusBar().showMessage(text, ms)

    # ------------------------------------------------------------------ albero delle sessioni
    def refresh_tree(self, *_args, select: str = "") -> None:
        query = self.search.text().strip().casefold() if hasattr(self, "search") else ""
        expanded = self._expanded_paths()
        current = select or (self.selected_path() or "")
        self.tree_model.clear()
        root_item = self.tree_model.invisibleRootItem()
        to_select = None

        def matches(node: model.Node) -> bool:
            if not query:
                return True
            if isinstance(node, model.FolderNode):
                return query in node.name.casefold() or any(matches(child) for child in node.children)
            return query in " ".join((node.name, node.host, node.user, node.notes, node.protocol)).casefold()

        def add(parent: QStandardItem, nodes: List[model.Node], prefix: str) -> None:
            nonlocal to_select
            for index, node in enumerate(nodes):
                path = f"{prefix}.{index}" if prefix else str(index)
                if not matches(node):
                    continue
                if isinstance(node, model.FolderNode):
                    item = QStandardItem(icon("fth-folder") if not icon("fth-folder").isNull() else
                                         self.style().standardIcon(self.style().StandardPixmap.SP_DirIcon), node.name)
                    item.setData("folder", KIND_ROLE)
                else:
                    detail = node.host or node.command or ""
                    item = QStandardItem(icon(PROTOCOL_ICONS.get(node.protocol, "")), node.name)
                    item.setToolTip(f"{node.protocol.upper()}  {detail}".strip())
                    item.setData("session", KIND_ROLE)
                item.setData(path, PATH_ROLE)
                parent.appendRow(item)
                if path == current:
                    to_select = item
                if isinstance(node, model.FolderNode):
                    add(item, node.children, path)

        add(root_item, self.store.nodes, "")
        # Home Session aperta sempre; le altre cartelle come erano (o tutte, se si sta cercando)
        for row in range(self.tree_model.rowCount()):
            self._restore_expansion(self.tree_model.index(row, 0), expanded, bool(query), depth=0)
        if to_select is not None:
            self.tree.setCurrentIndex(to_select.index())

    def _expanded_paths(self) -> set:
        paths = set()
        if not hasattr(self, "tree") or self.tree_model.rowCount() == 0:
            return {"0"}

        def walk(index) -> None:  # noqa: ANN001
            if self.tree.isExpanded(index):
                paths.add(index.data(PATH_ROLE))
            for row in range(self.tree_model.rowCount(index)):
                walk(self.tree_model.index(row, 0, index))

        for row in range(self.tree_model.rowCount()):
            walk(self.tree_model.index(row, 0))
        return paths

    def _restore_expansion(self, index, expanded: set, all_open: bool, depth: int) -> None:  # noqa: ANN001
        if all_open or depth == 0 or index.data(PATH_ROLE) in expanded:
            self.tree.setExpanded(index, True)
        for row in range(self.tree_model.rowCount(index)):
            self._restore_expansion(self.tree_model.index(row, 0, index), expanded, all_open, depth + 1)

    def selected_path(self) -> Optional[str]:
        if not hasattr(self, "tree"):
            return None
        index = self.tree.currentIndex()
        return index.data(PATH_ROLE) if index.isValid() else None

    def selected_node(self) -> Optional[Tuple[str, model.Node]]:
        path = self.selected_path()
        if path is None:
            return None
        try:
            return path, self.store.get_node(path)
        except Exception:  # noqa: BLE001
            return None

    def target_folder(self) -> str:
        """La cartella in cui mettere una sessione nuova: quella selezionata o quella della sessione selezionata."""
        selected = self.selected_node()
        if selected is None:
            return self.store.home_path()
        path, node = selected
        return path if isinstance(node, model.FolderNode) else self.store.parent_path(path)

    def _tree_menu(self, pos) -> None:  # noqa: ANN001
        index = self.tree.indexAt(pos)
        if index.isValid():
            self.tree.setCurrentIndex(index)
        menu = QMenu(self)
        selected = self.selected_node()
        if selected and isinstance(selected[1], model.Session):
            menu.addAction(tr("Apri"), lambda: self.open_path(selected[0]))
            menu.addSeparator()
        menu.addAction(self.act_new_session)
        menu.addAction(self.act_new_folder)
        if selected and not self.store.is_home_path(selected[0]):
            menu.addSeparator()
            for act in (self.act_edit, self.act_duplicate, self.act_move, self.act_delete):
                menu.addAction(act)
        menu.addSeparator()
        menu.addAction(self.act_export)
        menu.addAction(self.act_import)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ gestione sessioni
    def credential_labels(self) -> List[Tuple[str, str]]:
        return self.vault.labels() if self.vault.unlocked else []

    def new_session(self) -> None:
        if self.vault.exists() and not self.vault.unlocked:
            self.ensure_vault_unlocked(create_if_missing=False, quiet=True)
        dialog = SessionDialog(self, None, self.credential_labels(), not self.vault.unlocked, self.tunnel_labels())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        sess = dialog.result_session()
        folder = self.target_folder()
        self.store.add_node(folder, sess)
        self.refresh_tree(select=self.store.find_node_path(sess))
        self.set_status(tr("Sessione creata: {name}").format(name=sess.name))

    def new_folder(self) -> None:
        name, ok = QInputDialog.getText(self, tr("Nuova cartella"), tr("Nome cartella"))
        if not ok or not name.strip():
            return
        folder = model.FolderNode(name=name.strip())
        self.store.add_node(self.target_folder(), folder)
        self.refresh_tree(select=self.store.find_node_path(folder))

    def edit_selected(self) -> None:
        selected = self.selected_node()
        if selected is None:
            self.set_status(tr("Seleziona una sessione o cartella da modificare."))
            return
        path, node = selected
        if isinstance(node, model.FolderNode):
            if self.store.is_home_path(path):
                self.set_status(tr("Home Session è la cartella principale e non può essere rinominata."))
                return
            name, ok = QInputDialog.getText(self, tr("Modifica cartella"), tr("Nome cartella"), text=node.name)
            if ok and name.strip():
                node.name = name.strip()
                self.store.save()
                self.refresh_tree(select=path)
            return
        if node.credential_id and self.vault.exists() and not self.vault.unlocked:
            self.ensure_vault_unlocked(create_if_missing=False, quiet=True)
        dialog = SessionDialog(self, node, self.credential_labels(), not self.vault.unlocked, self.tunnel_labels())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.store.replace_node(path, dialog.result_session())
            self.refresh_tree(select=path)
            self.set_status(tr("Sessione aggiornata: {name}").format(name=node.name))

    def duplicate_selected(self) -> None:
        selected = self.selected_node()
        if selected is None or self.store.is_home_path(selected[0]):
            return
        path, node = selected
        clone = model.node_from_dict(model.node_to_dict(node))
        clone.name = tr("{name} copia").format(name=clone.name)
        container = self.store.get_container(self.store.parent_path(path))
        container.insert(container.index(node) + 1, clone)
        self.store.save()
        self.refresh_tree(select=self.store.find_node_path(clone))
        self.set_status(tr("Duplicato: {name}").format(name=clone.name))

    def move_selected(self) -> None:
        selected = self.selected_node()
        if selected is None or self.store.is_home_path(selected[0]):
            return
        path, node = selected
        folders = [(fpath, label) for fpath, _folder, label in self.store.iter_folders()
                   if not (fpath == path or fpath.startswith(path + "."))]
        labels = [label for _p, label in folders]
        choice, ok = QInputDialog.getItem(self, tr("Sposta in cartella"), tr("Sposta «{name}» in:").format(name=node.name),
                                          labels, 0, False)
        if not ok:
            return
        target = folders[labels.index(choice)][0]
        try:
            new_path = self.store.move_node(path, target)
        except Exception as exc:  # noqa: BLE001
            self.set_status(tr("Errore spostamento: {exc}").format(exc=exc))
            return
        self.refresh_tree(select=new_path)

    def delete_selected(self) -> None:
        selected = self.selected_node()
        if selected is None or self.store.is_home_path(selected[0]):
            return
        path, node = selected
        if isinstance(node, model.FolderNode):
            sessions = sum(1 for _ in self._walk_sessions(node))
            question = tr("Eliminare la cartella «{name}» con {join_parts}?").format(
                name=node.name, join_parts=trn("1 sessione", "{sessions} sessioni", sessions).format(sessions=sessions))
        else:
            question = tr("Eliminare «{name}»?").format(name=node.name)
        if QMessageBox.question(self, model.APP_NAME, question) != QMessageBox.StandardButton.Yes:
            return
        self.store.remove_node(path)
        self.refresh_tree()
        self.set_status(tr("Eliminato: {name}").format(name=node.name))

    @staticmethod
    def _walk_sessions(folder: model.FolderNode):  # noqa: ANN205
        for child in folder.children:
            if isinstance(child, model.FolderNode):
                yield from MainWindow._walk_sessions(child)
            else:
                yield child

    def session_names_using(self, credential_id: str) -> List[str]:
        return [sess.name for _path, sess, _folder in self.store.iter_sessions() if sess.credential_id == credential_id]

    # ------------------------------------------------------------------ import ed export
    def export_selected(self) -> None:
        selected = self.selected_node()
        node: model.Node = selected[1] if selected else self.store.home_node()
        scope = "home-session" if selected is None or self.store.is_home_path(selected[0]) else (
            "folder" if isinstance(node, model.FolderNode) else "session")
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", node.name).strip("-") or scope
        dest, _ = QFileDialog.getSaveFileName(self, tr("Esporta cartella o sessione"), str(Path.home() / f"fedtermhub-{safe}.json"),
                                              "JSON (*.json)")
        if not dest:
            return
        try:
            model.atomic_write_text(Path(dest), json.dumps(model.session_package(node, scope), indent=2, ensure_ascii=False), mode=0o600)
            self.set_status(tr("Esportato: {name} → {name2}").format(name=node.name, name2=Path(dest).name))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, model.APP_NAME, tr("Errore export selezione: {exc}").format(exc=exc))

    def import_sessions(self) -> None:
        src, _ = QFileDialog.getOpenFileName(self, tr("Importa in cartella selezionata"), str(Path.home()), "JSON (*.json)")
        if not src:
            return
        try:
            self.import_session_file(Path(src), self.target_folder())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, model.APP_NAME, tr("Errore import in cartella: {exc}").format(exc=exc))

    def import_session_file(self, src: Path, target: str) -> model.Node:
        """Aggiunge a una cartella le sessioni di un file esportato (anche dalla versione Linux)."""
        node = model.node_from_session_package(json.loads(src.read_text(encoding="utf-8")), src.stem)
        if isinstance(node, model.FolderNode) and node.name == model.HOME_SESSION_NAME:
            node = model.FolderNode(name=tr("Import {stem}").format(stem=src.stem), color=node.color, children=node.children)
        self.store.add_node(target, node)
        self.refresh_tree(select=self.store.find_node_path(node))
        self.set_status(tr("Importato '{name}' in {folder_label}").format(name=node.name, folder_label=target or model.HOME_SESSION_NAME))
        return node

    def export_vault(self) -> None:
        if not self.ensure_vault_unlocked(create_if_missing=False):
            return
        dialog = MasterPasswordDialog(self, "export")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        dest, _ = QFileDialog.getSaveFileName(self, tr("Esporta backup cifrato delle password"),
                                              str(Path.home() / "fedtermhub-password.json"), "JSON (*.json)")
        if not dest:
            return
        payload = model.PasswordVault.encrypt_credentials_payload(self.vault.credentials, dialog.value())
        model.atomic_write_text(Path(dest), json.dumps(payload, indent=2), mode=0o600)
        self.set_status(tr("Backup cifrato di {n_credentials} credenziali salvato in {name}").format(
            n_credentials=len(self.vault.credentials), name=Path(dest).name))

    def import_vault(self) -> None:
        src, _ = QFileDialog.getOpenFileName(self, tr("Importa backup cifrato delle password"), str(Path.home()), "JSON (*.json)")
        if not src:
            return
        try:
            raw = json.loads(Path(src).read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, model.APP_NAME, tr("Il file non è un backup valido: {exc}").format(exc=exc))
            return
        imported: List[model.Credential] = []

        def check(password: str) -> Optional[str]:
            try:
                imported[:] = model.PasswordVault.decrypt_credentials_payload(raw, password)
            except Exception:  # noqa: BLE001
                return tr("Password errata per questo backup. Riprova.")
            return None

        if MasterPasswordDialog(self, "import", check).exec() != QDialog.DialogCode.Accepted:
            return
        if not self.ensure_vault_unlocked(create_if_missing=True):
            return
        known = {cred.id for cred in self.vault.credentials}
        self.vault.credentials.extend(cred for cred in imported if cred.id not in known)
        self.vault.save()
        self.set_status(tr("Importate {n_imported} credenziali da {name}").format(n_imported=len(imported), name=Path(src).name))

    # ------------------------------------------------------------------ Password Manager
    def ensure_vault_unlocked(self, create_if_missing: bool = True, quiet: bool = False) -> bool:
        if self.vault.unlocked:
            return True
        if not model.CRYPTO_AVAILABLE:
            QMessageBox.warning(self, model.APP_NAME, tr("python3-cryptography non disponibile"))
            return False
        if not self.vault.exists():
            if not create_if_missing:
                return False
            dialog = MasterPasswordDialog(self, "create")
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            self.vault.create(dialog.value())
            self.set_status(tr("Password Manager creato e sbloccato."))
            return True

        def check(password: str) -> Optional[str]:
            try:
                self.vault.unlock(password)
            except Exception:  # noqa: BLE001
                return tr("Password errata. Riprova.")
            return None

        if MasterPasswordDialog(self, "unlock", check).exec() == QDialog.DialogCode.Accepted:
            self.last_activity = time.monotonic()
            if not quiet:
                self.set_status(tr("Password Manager sbloccato."))
            return True
        return False

    def open_vault_manager(self) -> None:
        if not self.ensure_vault_unlocked(create_if_missing=True):
            return
        VaultManagerDialog(self, self.vault, self.settings, self.session_names_using, lambda: None).exec()

    def lock_vault(self) -> None:
        self.vault.lock()
        self.set_status(tr("Password bloccate."))

    def mark_activity(self, *_args) -> None:
        self.last_activity = time.monotonic()

    def idle_seconds(self) -> float:
        system = _system_idle_seconds()
        if system is not None:
            return system
        cursor = QCursor.pos()
        if cursor != self._last_cursor:
            self._last_cursor = cursor
            self.mark_activity()
        return time.monotonic() - self.last_activity

    def _auto_lock(self) -> None:
        minutes = self.settings.vault_auto_lock_minutes
        if self.vault.unlocked and minutes > 0 and self.idle_seconds() > minutes * 60:
            self.vault.lock()
            self.set_status(tr("Password bloccate automaticamente per inattività"), 0)

    def identity(self, sess: model.Session) -> Tuple[str, str, Optional[str]]:
        """Utente, dominio e password per la sessione (dalla credenziale collegata, se c'è)."""
        user, domain, password = sess.user, sess.domain, None
        if sess.credential_id and model.CRYPTO_AVAILABLE and self.vault.exists():
            if self.vault.unlocked or self.ensure_vault_unlocked(create_if_missing=False):
                cred = self.vault.get(sess.credential_id)
                if cred is not None:
                    user = cred.username or user
                    domain = cred.domain or domain
                    password = cred.password if sess.use_credential_password else None
        return user, domain, password

    def tunnel_identity(self, tunnel: model.TunnelSession) -> Tuple[str, Optional[str]]:
        user, password = tunnel.ssh_user, None
        if tunnel.credential_id and model.CRYPTO_AVAILABLE and self.vault.exists():
            if self.vault.unlocked or self.ensure_vault_unlocked(create_if_missing=False):
                cred = self.vault.get(tunnel.credential_id)
                if cred is not None:
                    user = cred.username or user
                    password = cred.password if tunnel.use_credential_password else None
        return user, password

    def tunnel_labels(self) -> List[Tuple[str, str]]:
        return [(t.id, t.name) for t in self.tunnels.store.tunnels]

    def open_tunnels(self) -> None:
        TunnelsDialog(self, self.tunnels, self.credential_labels, lambda: None).exec()

    def _tunnels_changed(self) -> None:
        active = self.tunnels.active()
        self.tunnel_status.setText(trn("{n} tunnel attivo", "{n} tunnel attivi", active).format(n=active) if active else "")

    # ------------------------------------------------------------------ apertura delle sessioni
    def open_path(self, path: Optional[str]) -> None:
        if path is None:
            return
        try:
            node = self.store.get_node(path)
        except Exception:  # noqa: BLE001
            return
        if isinstance(node, model.Session):
            self.open_session(node)

    def open_session(self, sess: model.Session, _tunnel_ready: bool = False) -> None:
        problem = model.session_problem(sess)
        if problem:
            QMessageBox.warning(self, tr("Sessione non valida"),
                                tr("«{name}»: {problem}. Correggila con Modifica sessione.").format(name=sess.name, problem=problem))
            return
        if sess.protocol not in AVAILABLE:
            QMessageBox.information(self, model.APP_NAME, tr("Le sessioni {protocol} si aprono per ora solo nella versione Linux "
                                                             "di FedTermHub.").format(protocol=sess.protocol.upper()))
            return
        if sess.routing_mode == "saved_tunnel" and sess.required_tunnel_id and not _tunnel_ready:
            tunnel = self.tunnels.store.get(sess.required_tunnel_id)
            if tunnel is None:
                QMessageBox.warning(self, tr("Tunnel richiesto non trovato"),
                                    tr("La sessione «{name}» è collegata a un tunnel che non esiste più.").format(name=sess.name))
                return
            if self.tunnels.state(tunnel.id) != "running":
                self.set_status(tr("Avvio del tunnel «{name}»…").format(name=tunnel.name), 0)

                def ready(ok: bool, log: str, s=sess, t=tunnel) -> None:
                    if ok:
                        self.set_status(tr("Tunnel pronto: {name}").format(name=t.name))
                        self.open_session(s, _tunnel_ready=True)
                    else:
                        QMessageBox.warning(self, tr("Impossibile avviare il tunnel"),
                                            tr("La sessione «{name}» non è stata aperta perché il tunnel «{tunnel}» non è "
                                               "pronto.\n\n{log}").format(name=s.name, tunnel=t.name, log=log))

                self.tunnels.when_ready(tunnel.id, ready)
                return
        try:
            if sess.protocol == "rdp":
                self.open_rdp(sess)
                return
            if sess.protocol == "local":
                launch = connect.local_shell(self.settings)
                launch.title = sess.name or launch.title
            elif sess.protocol == "ssh":
                user, _domain, password = self.identity(sess)
                launch = connect.ssh(sess, self.settings, user, password)
                target = lambda s=sess, u=user, p=password: self.sftp_target(s, u, p)  # noqa: E731
                self.add_terminal_tab(launch, protocol="ssh", files_target=target)
                return
            elif sess.protocol == "sftp":
                self.open_sftp(sess)
                return
            elif sess.protocol == "vnc":
                self.open_vnc(sess)
                return
            elif sess.protocol == "telnet":
                launch = connect.telnet(sess, self.settings)
            elif sess.protocol == "serial":
                launch = connect.serial(sess)
            else:
                launch = connect.command(sess, self.settings)
            self.add_terminal_tab(launch, protocol=sess.protocol)
        except connect.LaunchError as exc:
            QMessageBox.warning(self, exc.title, exc.hint or exc.title)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("Errore apertura sessione"), f"{exc}\n\n{traceback.format_exc()}")

    def open_local_terminal(self) -> None:
        self.add_terminal_tab(connect.local_shell(self.settings), protocol="local")

    def quick_connect(self) -> None:
        text, ok = QInputDialog.getText(
            self, tr("Connessione rapida"),
            tr("Scrivi utente@host[:porta] per SSH, rdp:host per il desktop remoto o telnet:host."))
        text = text.strip()
        if not ok or not text:
            return
        proto = "ssh"
        lowered = text.lower()
        for prefix in ("rdp:", "telnet:", "ssh:"):
            if lowered.startswith(prefix):
                proto, text = prefix[:-1], text[len(prefix):].lstrip("/")
                break
        user = ""
        if "@" in text:
            user, text = text.rsplit("@", 1)
        host, port = model.split_host_port(text)
        sess = model.Session(name=text, protocol=proto, host=host, port=port, user=user)
        self.open_session(sess)

    def sftp_target(self, sess: model.Session, user: str, password: Optional[str]) -> SftpTarget:
        host, port = model.split_host_port(sess.host, sess.port or "22")
        jump = sess.routing_mode == "proxy_jump" and bool(sess.jump_host)
        return SftpTarget(
            host=host, port=int(port or 22), user=user, password=password,
            jump_host=sess.jump_host if jump else "", jump_port=int(sess.jump_port or 22) if jump else 22,
            jump_user=sess.jump_user if jump else "",
            timeout=float(max(3, self.settings.connection_timeout_seconds)),
            strict=self.settings.ssh_strict_host_key if self.settings.ssh_strict_host_key in {"ask", "accept-new", "yes", "no"} else "ask",
        )

    def open_sftp(self, sess: model.Session) -> None:
        user, _domain, password = self.identity(sess)
        panel = FilesPanel(self.sftp_target(sess, user, password), self.tabs)
        panel.status.connect(self.set_status)
        index = self.tabs.addTab(panel, icon("fth-proto-sftp"), sess.name or sess.host)
        self.tabs.setCurrentIndex(index)
        self._update_welcome()
        panel.connect_server()

    def open_vnc(self, sess: model.Session) -> None:
        user, _domain, password = self.identity(sess)
        tab = VncTab(self.tabs, sess, user, password)
        tab.status.connect(self.set_status)
        index = self.tabs.addTab(tab, icon("fth-proto-vnc"), sess.name or sess.host)
        self.tabs.setCurrentIndex(index)
        self._update_welcome()

    def open_network_tools(self) -> None:
        if getattr(self, "_net_tools", None) is None:
            self._net_tools = NetworkToolsDialog(self, self.open_command_tab, self.create_session_for)
        self._net_tools.show()
        self._net_tools.raise_()
        self._net_tools.activateWindow()

    def open_command_tab(self, title: str, argv: List[str]) -> None:
        binary = connect.find_program(argv[0])
        if not binary:
            QMessageBox.warning(self, model.APP_NAME, tr("Programma non trovato: {name}").format(name=argv[0]))
            return
        self.add_terminal_tab(connect.Launch(title, [binary] + argv[1:], {}, close_on_success=False), protocol="command")

    def create_session_for(self, host: str, protocol: str, port: int = 0) -> None:
        default = {"ssh": 22, "rdp": 3389, "vnc": 5900, "telnet": 23, "sftp": 22}.get(protocol, 0)
        sess = model.Session(name=host, protocol=protocol or "ssh", host=host,
                             port=str(port) if port and port != default else "")
        dialog = SessionDialog(self, sess, self.credential_labels(), not self.vault.unlocked, self.tunnel_labels())
        dialog.setWindowTitle(tr("Nuova sessione"))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            node = dialog.result_session()
            target = self.target_folder()
            self.store.add_node(target, node)
            self.refresh_tree(select=self.store.find_node_path(node))
            self.set_status(tr("Sessione creata: {name}").format(name=node.name))

    def toggle_remote_files(self) -> None:
        tab = self.tabs.currentWidget()
        if isinstance(tab, TerminalTab) and tab.has_files:
            tab.toggle_files()
        else:
            self.set_status(tr("Il pannello dei file remoti c'è nelle schede SSH."))

    def add_terminal_tab(self, launch: connect.Launch, protocol: str = "",
                         files_target: Optional[Callable[[], SftpTarget]] = None) -> TerminalTab:
        tab = TerminalTab(self.tabs, launch, self.settings, files_target=files_target)
        tab.status.connect(self.set_status)
        tab.view.input_hooks.append(self.mark_activity)
        index = self.tabs.addTab(tab, icon(PROTOCOL_ICONS.get(protocol, "")), launch.title)
        self.tabs.setCurrentIndex(index)
        tab.finished.connect(lambda code, t=tab: self._terminal_finished(t, code))
        self._update_welcome()
        QTimer.singleShot(300, tab.view.focus_terminal)
        return tab

    def _terminal_finished(self, tab: TerminalTab, code: int) -> None:
        if code == 0 and tab.launch.close_on_success:
            index = self.tabs.indexOf(tab)
            if index >= 0:
                self.close_tab(index)

    def open_rdp(self, sess: model.Session) -> None:
        user, domain, password = self.identity(sess)
        tab = RdpTab(self.tabs, sess, user, domain, password)
        tab.status.connect(self.set_status)
        index = self.tabs.addTab(tab, icon("fth-proto-rdp"), sess.name or sess.host)
        self.tabs.setCurrentIndex(index)
        self._update_welcome()

    def close_tab(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is None:
            return
        close = getattr(widget, "close_tab", None)
        if close:
            close()
        self.tabs.removeTab(index)
        widget.deleteLater()
        self._update_welcome()

    def close_current_tab(self) -> None:
        if self.tabs.count():
            self.close_tab(self.tabs.currentIndex())

    def active_sessions(self) -> int:
        count = 0
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            if isinstance(widget, TerminalTab) and widget.running:
                count += 1
            elif isinstance(widget, (RdpTab, VncTab)) and widget.connected:
                count += 1
        return count

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802
        active = self.active_sessions() + self.tunnels.active()
        if active and self.settings.confirm_close_active_sessions:
            answer = QMessageBox.question(
                self, tr("Chiudere FedTermHub?"),
                trn("C'è una sessione ancora aperta: verrà chiusa.", "Ci sono {n} sessioni ancora aperte: verranno chiuse.", active).format(n=active))
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        for i in range(self.tabs.count() - 1, -1, -1):
            self.close_tab(i)
        self.tunnels.stop_all()
        if not self.isMaximized():
            self.settings.window_width, self.settings.window_height = self.width(), self.height()
            try:
                self.settings.save(model.SETTINGS_FILE)
            except OSError:
                pass
        self.vault.lock()
        event.accept()

    # ------------------------------------------------------------------ impostazioni, aggiornamenti, informazioni
    def open_settings(self) -> None:
        dialog = SettingsDialog(self, self.settings)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.apply()
            self.settings.save(model.SETTINGS_FILE)
            theme.apply(QApplication.instance(), self.settings.theme)
            for i in range(self.tabs.count()):
                widget = self.tabs.widget(i)
                if isinstance(widget, TerminalTab):
                    widget.view.send_theme()
            self.set_status(tr("Impostazioni applicate"))

    def _check_updates(self) -> None:
        def done(latest: Optional[str]) -> None:
            if latest and updates.newer(latest, model.APP_VERSION):
                button = QPushButton(tr("Scarica FedTermHub {version}").format(version=latest))
                button.setFlat(True)
                button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(updates.RELEASES_PAGE)))
                self.statusBar().addPermanentWidget(button)
                self.set_status(tr("È disponibile FedTermHub {version}").format(version=latest), 0)

        updates.check_async(self, done)

    def about(self) -> None:
        QMessageBox.about(
            self, tr("Informazioni su {APP_NAME}").format(APP_NAME=model.APP_NAME),
            f"<b>{model.APP_NAME} {model.APP_VERSION}</b><br><br>"
            + tr("Gestore integrato di terminali e connessioni remote.") + "<br><br>"
            + tr("Configurazione locale: {path}").format(path=model.CONFIG_DIR) + "<br><br>"
            + "GPL-3.0-or-later · © 2026 Crash<br>"
            + '<a href="https://github.com/Crash-FD01/FedTermHub">github.com/Crash-FD01/FedTermHub</a>',
        )
