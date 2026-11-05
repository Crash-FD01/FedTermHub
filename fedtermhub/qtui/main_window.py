# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Finestra principale dell'interfaccia Qt, con la disposizione della versione Linux.

Dall'alto: menu, barra dei comandi (pulsanti con tendina e cambio tema), poi la barra verticale a sinistra,
il pannello laterale (connessione rapida, Sessioni | File remoti) e le schede; in fondo la barra di stato.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCursor, QDesktopServices, QIcon, QKeySequence, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import model, termcore
from ..i18n import tr, trn
from . import connect, quick_tools, theme, updates
from .chrome import (
    KIND_ROLE, PATH_ROLE, FolderDialog, LogDialog, MultiExecDialog, SessionTree, ShortcutsDialog, add_heading, drop_target,
)
from .rdp_tab import RdpTab
from .session_dialog import AVAILABLE, SessionDialog
from .tunnels import TunnelManager, TunnelsDialog
from .net_tools import NetworkToolsDialog
from .vnc_tab import VncTab
from .settings_dialog import SettingsDialog
from .terminal_tab import TerminalPane, TerminalTab
from .files_panel import FilesPanel
from .sftp_client import SftpTarget
from .vault_dialogs import MasterPasswordDialog, VaultManagerDialog

ICONS = Path(__file__).resolve().parent.parent / "icons"
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
        self._icon_bindings: List[Tuple[Callable[[QIcon], None], str]] = []
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
        self._update_offered: set = set()
        self._update_button: Optional[QPushButton] = None
        self._pending_setup = None
        if self.settings.check_updates and updates.enabled():
            QTimer.singleShot(3000, self._check_updates)
            self._update_timer = QTimer(self)
            self._update_timer.setInterval(updates.RECHECK_MS)
            self._update_timer.timeout.connect(self._check_updates)
            self._update_timer.start()

    # ------------------------------------------------------------------ interfaccia
    def _action(self, text: str, slot, shortcut: str = "", icon_name: str = "", tip: str = "") -> QAction:  # noqa: ANN001
        act = QAction(text, self)
        if icon_name:
            self._bind_icon(act.setIcon, icon_name)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
        if tip:
            act.setToolTip(tip)
        act.triggered.connect(lambda *_: slot())
        return act

    def _bind_icon(self, setter: Callable[[QIcon], None], name: str) -> None:
        """Imposta un'icona e la ricorda: al cambio di tema si ridisegna nel colore giusto."""
        self._icon_bindings.append((setter, name))
        setter(icon(name))

    def _refresh_icons(self) -> None:
        for setter, name in list(self._icon_bindings):
            try:
                setter(icon(name))
            except RuntimeError:  # widget già distrutto
                self._icon_bindings.remove((setter, name))

    def _build_actions(self) -> None:
        self.act_new_session = self._action(tr("Nuova sessione"), self.new_session, "Ctrl+Shift+N", "fth-act-new")
        self.act_new_folder = self._action(tr("Nuova cartella"), self.new_folder, "", "fth-act-folder-new")
        self.act_local = self._action(tr("Nuovo terminale locale"), self.open_local_terminal, "Ctrl+Shift+T", "fth-tb-terminale")
        self.act_split = self._action(tr("Terminale diviso"), self.open_split_terminal, "", "fth-tb-terminale")
        self.act_multiexec = self._action(tr("MultiExec"), self.show_multiexec_dialog, "", "fth-tb-terminale")
        self.act_panes = [
            self._action(tr("Dividi in verticale (Ctrl+Shift+O)"), lambda: self.pane_action("split_v")),
            self._action(tr("Dividi in orizzontale (Ctrl+Shift+E)"), lambda: self.pane_action("split_h")),
            self._action(tr("Cambia layout (Ctrl+Shift+L)"), lambda: self.pane_action("cycle_layout")),
            self._action(tr("Broadcast input on/off (Ctrl+Shift+P)"), lambda: self.pane_action("toggle_broadcast")),
            self._action(tr("Chiudi riquadro (Ctrl+Shift+W)"), lambda: self.pane_action("close_pane")),
        ]
        self.act_quick = self._action(tr("Quick connect"), self.focus_quick_connect, "Ctrl+L", "fth-act-connect")
        self.act_edit = self._action(tr("Modifica selezionata"), self.edit_selected, "F2", "fth-act-edit")
        self.act_duplicate = self._action(tr("Duplica selezionata"), self.duplicate_selected, "Ctrl+D", "fth-act-duplicate")
        self.act_move = self._action(tr("Sposta selezionata"), self.move_selected, "", "fth-act-move")
        self.act_delete = self._action(tr("Elimina selezionata"), self.delete_selected, "Delete", "fth-act-delete")
        self.act_vault = self._action(tr("Password Manager"), self.open_vault_manager, "", "fth-tb-password")
        self.act_lock = self._action(tr("Blocca password"), self.lock_vault, "", "fth-rail-lock")
        self.act_settings = self._action(tr("Impostazioni"), self.open_settings, "Ctrl+,", "fth-tb-sistema")
        self.act_net_tools = self._action(tr("Strumenti di rete"), self.open_network_tools, "Ctrl+Shift+R", "fth-tb-rete")
        self.act_tunnels = self._action(tr("Tunneling SSH"), self.open_tunnels, "Ctrl+Shift+U", "fth-tb-rete")
        self.act_files = self._action(tr("File remoti"), self.toggle_remote_files, "", "fth-act-files",
                                      tr("Mostra o nasconde i file remoti della sessione SSH"))
        self.act_import = self._action(tr("Importa sessioni nella cartella selezionata"), self.import_sessions)
        self.act_export = self._action(tr("Esporta la selezione"), self.export_selected)
        self.act_export_all = self._action(tr("Esporta tutte le sessioni"), self.export_all)
        self.act_replace_all = self._action(tr("Sostituisci tutte le sessioni con un file"), self.replace_all_sessions)
        self.act_export_vault = self._action(tr("Esporta backup cifrato delle password"), self.export_vault)
        self.act_import_vault = self._action(tr("Importa backup cifrato delle password"), self.import_vault)
        self.act_close_tab = self._action(tr("Chiudi la scheda"), self.close_current_tab, "Ctrl+W")
        self.act_quit = self._action(tr("Esci"), self.close, "Ctrl+Q")
        self.act_about = self._action(tr("Informazioni"), self.about)
        self.act_updates = self._action(tr("Cerca aggiornamenti"), lambda: self._check_updates(manual=True))
        self.act_guide = self._action(tr("Guida"), lambda: QDesktopServices.openUrl(
            QUrl("https://github.com/Crash-FD01/FedTermHub/blob/main/docs/GUIDA.md")), "F1")
        self.act_shortcuts = self._action(tr("Scorciatoie da tastiera"), self.show_shortcuts)
        self.act_log = self._action(tr("Log applicazione"), self.open_log, "", "fth-rail-log")
        self.act_export_settings = self._action(tr("Esporta impostazioni"), self.export_settings)
        self.act_import_settings = self._action(tr("Importa impostazioni"), self.import_settings)
        self.act_expand = self._action(tr("Espandi cartelle"), lambda: self.tree.expandAll(), "", "fth-act-expand")
        self.act_collapse = self._action(tr("Comprimi cartelle"), self._collapse_folders, "", "fth-act-collapse")
        self.new_by_protocol = [
            self._action(tr("Nuova SSH"), lambda: self.new_session("ssh"), "", "fth-proto-ssh"),
            self._action(tr("Nuova RDP"), lambda: self.new_session("rdp"), "", "fth-proto-rdp"),
            self._action(tr("Nuova VNC"), lambda: self.new_session("vnc"), "", "fth-proto-vnc"),
            self._action(tr("Nuova Telnet"), lambda: self.new_session("telnet"), "", "fth-proto-telnet"),
            self._action(tr("Nuova seriale"), lambda: self.new_session("serial"), "", "fth-proto-serial"),
        ]
        # Mostra/nascondi le parti della finestra: (testo, impostazione)
        self.view_toggles = {}
        for key, text in (("show_menubar", tr("Mostra/nascondi menu")),
                          ("show_ribbon", tr("Mostra/nascondi barra comandi")),
                          ("show_sidebar", tr("Mostra/nascondi pannello sessioni")),
                          ("show_left_rail", tr("Mostra/nascondi barra verticale")),
                          ("show_statusbar", tr("Mostra/nascondi barra di stato"))):
            act = QAction(text, self)
            act.setCheckable(True)
            act.setChecked(bool(getattr(self.settings, key)))
            act.toggled.connect(lambda on, k=key: self.set_part_visible(k, on))
            self.view_toggles[key] = act
        self.view_toggles["show_sidebar"].setShortcut(QKeySequence("Ctrl+Shift+B"))
        # Tasti dei riquadri. Ctrl+Shift+E e Ctrl+Shift+P, come sulla versione Linux, nel terminale dividono e
        # attivano il broadcast, fuori dal terminale aprono i file remoti e le password: per questo le due
        # voci di menu non hanno una scorciatoia propria, e il tasto lo mostrano nel testo.
        self.pane_key_fallbacks = {"split_h": self.act_files, "toggle_broadcast": self.act_vault}
        self.pane_shortcuts = {}
        for action, keys in termcore.PANE_KEYS.items():
            shortcut = QShortcut(self)
            shortcut.setKeys([QKeySequence(key) for key in keys])
            shortcut.activated.connect(lambda a=action: self.pane_key(a))
            self.pane_shortcuts[action] = shortcut
        for act, key in ((self.act_files, "Ctrl+Shift+E"), (self.act_vault, "Ctrl+Shift+P")):
            act.setText(f"{act.text()}\t{key}")
        QApplication.instance().focusChanged.connect(self._focus_changed)
        self.network_pages = [
            (tr("Server locali"), "services", "fth-tb-rete"),
            (tr("Porte locali in ascolto"), "listeners", "fth-tb-rete"),
            ("Wake-on-LAN", "wol", "fth-tb-rete"),
            (tr("Network scanner"), "network-scan", "fth-act-search"),
            (tr("Port scanner"), "port-scan", "fth-act-search"),
        ]

    def _quick_tool_action(self, name: str) -> QAction:
        return self._action(tr(name), lambda n=name: self.run_quick_tool(n), "", "fth-tb-terminale")

    def _fill_menu(self, menu: QMenu, category: str, headings: bool) -> None:
        """Il contenuto delle tendine: lo stesso nei menu in alto e nei pulsanti della barra dei comandi."""
        def heading(text: str) -> None:
            if headings:
                add_heading(menu, text)

        if category == "terminal":
            heading(tr("Terminale"))
            menu.addAction(self.act_local)
            menu.addAction(self.act_split)
            menu.addAction(self.act_multiexec)
            panes = menu.addMenu(tr("Riquadri"))
            panes.addActions(self.act_panes)
            menu.addAction(self.act_quick)
            menu.addAction(self.act_close_tab)
            if not headings:
                menu.addSeparator()
                menu.addAction(self.act_quit)
        elif category == "sessions":
            heading(tr("Crea sessione"))
            menu.addAction(self.act_new_session)
            for act in self.new_by_protocol:
                menu.addAction(act)
            menu.addAction(self.act_new_folder)
            menu.addSeparator()
            heading(tr("Organizza"))
            for act in (self.act_edit, self.act_duplicate, self.act_move, self.act_delete):
                menu.addAction(act)
            if not headings:
                menu.addSeparator()
                for act in (self.act_export_all, self.act_export, self.act_import, self.act_replace_all):
                    menu.addAction(act)
        elif category == "network":
            heading(tr("Server e servizi"))
            for text, page, icon_name in self.network_pages[:2]:
                menu.addAction(self._action(text, lambda p=page: self.open_network_tools(p), "", icon_name))
            menu.addSeparator()
            heading(tr("Analisi rete"))
            for text, page, icon_name in self.network_pages[2:]:
                menu.addAction(self._action(text, lambda p=page: self.open_network_tools(p), "", icon_name))
            menu.addAction(self.act_net_tools)
            menu.addSeparator()
            heading(tr("Test rapidi"))
            for name in quick_tools.NETWORK:
                menu.addAction(self._quick_tool_action(name))
            menu.addAction(self._action(tr("Connessione Telnet"), self.quick_telnet, "", "fth-proto-telnet"))
        elif category == "tools":
            heading(tr("Strumenti"))
            menu.addAction(self.act_tunnels)
            if not headings:
                for act in (self.act_vault, self.act_export_vault, self.act_import_vault, self.act_lock):
                    menu.addAction(act)
                menu.addSeparator()
            for name in quick_tools.TOOLS:
                menu.addAction(self._quick_tool_action(name))
            if not headings:
                menu.addAction(self.act_log)
        elif category == "view":
            heading(tr("Vista"))
            for key in (("show_menubar", "show_ribbon") if not headings else ()) + (
                    "show_sidebar", "show_left_rail", "show_statusbar"):
                menu.addAction(self.view_toggles[key])
            menu.addAction(self.act_files)
            menu.addSeparator()
            menu.addAction(self.act_expand)
            menu.addAction(self.act_collapse)
        elif category == "system":
            heading(tr("Sicurezza"))
            menu.addAction(self.act_vault)
            menu.addAction(self.act_lock)
            menu.addSeparator()
            heading(tr("Sistema"))
            for act in (self.act_settings, self.act_log, self.act_guide, self.act_shortcuts):
                menu.addAction(act)
            if updates.enabled():
                menu.addAction(self.act_updates)
            menu.addAction(self.act_about)

    def _build_ui(self) -> None:
        # --- menu, con le stesse voci della versione Linux (quelle che ci sono anche su Windows)
        menubar = self.menuBar()
        menubar.setObjectName("menubar")
        for title, category in ((tr("Terminale"), "terminal"), (tr("Sessioni"), "sessions"), (tr("Rete"), "network"),
                                (tr("Strumenti"), "tools"), (tr("Vista"), "view")):
            self._fill_menu(menubar.addMenu(title), category, headings=False)
        settings_menu = menubar.addMenu(tr("Impostazioni"))
        settings_menu.addAction(self._action(tr("Apri impostazioni"), self.open_settings))
        settings_menu.addAction(self.act_export_settings)
        settings_menu.addAction(self.act_import_settings)
        help_menu = menubar.addMenu(tr("Aiuto"))
        for act in (self.act_guide, self.act_shortcuts):
            help_menu.addAction(act)
        if updates.enabled():
            help_menu.addAction(self.act_updates)
        help_menu.addAction(self.act_about)

        # --- barra dei comandi: pochi pulsanti, ognuno con la sua tendina, e il cambio tema a destra
        self.ribbon = QToolBar(tr("Barra comandi"))
        self.ribbon.setObjectName("ribbon")
        self.ribbon.setMovable(False)
        self.ribbon.setFloatable(False)
        self.ribbon.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        size = self.settings.toolbar_icon_size
        self.ribbon.setIconSize(QSize(size, size))
        self.ribbon.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.ribbon_buttons = {}
        for text, icon_name, category in ((tr("Terminale"), "fth-tb-terminale", "terminal"),
                                          (tr("Sessioni"), "fth-tb-sessioni", "sessions"),
                                          (tr("Rete"), "fth-tb-rete", "network"),
                                          (tr("Strumenti"), "fth-tb-strumenti", "tools"),
                                          (tr("Password"), "fth-tb-password", ""),
                                          (tr("Vista"), "fth-tb-vista", "view"),
                                          (tr("Sistema"), "fth-tb-sistema", "system")):
            button = QToolButton()
            button.setText(text)
            button.setToolTip(text)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            self._bind_icon(button.setIcon, icon_name)
            if category:
                menu = QMenu(button)
                self._fill_menu(menu, category, headings=True)
                button.setMenu(menu)
                button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            else:
                button.clicked.connect(self.open_vault_manager)
            self.ribbon.addWidget(button)
            self.ribbon_buttons[category or "password"] = button
        self.ribbon.addSeparator()
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.ribbon.addWidget(spacer)
        self.theme_button = QToolButton()
        self.theme_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.theme_button.clicked.connect(self.toggle_theme)
        self.ribbon.addWidget(self.theme_button)
        self._update_theme_button()
        self.addToolBar(self.ribbon)

        # --- corpo: barra verticale | pannello laterale | area delle schede
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        self.rail = QFrame()
        self.rail.setObjectName("rail")
        rail_layout = QVBoxLayout(self.rail)
        rail_layout.setContentsMargins(4, 6, 4, 6)
        rail_layout.setSpacing(6)
        for tip, icon_name, slot in ((tr("Pannello sessioni"), "fth-rail-sessions", self.show_sessions_panel),
                                     (tr("Nascondi/mostra pannello"), "fth-rail-sidebar",
                                      lambda: self.view_toggles["show_sidebar"].toggle()),
                                     (tr("Log applicazione"), "fth-rail-log", self.open_log),
                                     (None, None, None),
                                     (tr("Blocca password"), "fth-rail-lock", self.lock_vault)):
            if tip is None:
                rail_layout.addStretch(1)
                continue
            button = QToolButton()
            button.setToolTip(tip)
            button.setIconSize(QSize(20, 20))
            self._bind_icon(button.setIcon, icon_name)
            button.clicked.connect(lambda *_, s=slot: s())
            rail_layout.addWidget(button)
        body_layout.addWidget(self.rail)

        self.splitter = QSplitter()
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)
        self.sidebar = self._build_sidebar()
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(self._build_workspace())
        self.splitter.setSizes([max(190, self.settings.sidebar_width), 1000])
        self.splitter.setStretchFactor(1, 1)
        body_layout.addWidget(self.splitter, 1)
        self.setCentralWidget(body)
        self._update_welcome()

        # --- barra di stato: la scheda aperta a sinistra, versione a destra
        self.open_label = QLabel(tr("Pronto."))
        self.statusBar().addWidget(self.open_label, 1)
        self.tunnel_status = QLabel("")
        self.statusBar().addPermanentWidget(self.tunnel_status)
        platform = "Windows" if sys.platform == "win32" else "Qt"
        self.version_label = QLabel(f"{model.APP_NAME} {model.APP_VERSION} • {platform} workstation tools")
        self.version_label.setObjectName("versionLabel")
        self.statusBar().addPermanentWidget(self.version_label)
        self.statusBar().setSizeGripEnabled(False)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.show_sessions_panel)
        for key, act in self.view_toggles.items():
            self.set_part_visible(key, act.isChecked(), save=False)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(190)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        quick = QFrame()
        quick.setObjectName("quickBox")
        quick_layout = QHBoxLayout(quick)
        quick_layout.setContentsMargins(4, 4, 4, 4)
        quick_layout.setSpacing(4)
        self.quick_entry = QLineEdit()
        self.quick_entry.setPlaceholderText(tr("Quick connect…"))
        self.quick_entry.setToolTip(tr("utente@host[:porta] per SSH, rdp:host per il desktop remoto, telnet:host"))
        self.quick_entry.returnPressed.connect(self.quick_connect)
        quick_layout.addWidget(self.quick_entry, 1)
        go = QToolButton()
        go.setProperty("role", "flat")
        go.setToolTip(tr("Connetti"))
        self._bind_icon(go.setIcon, "fth-act-connect")
        go.clicked.connect(self.quick_connect)
        quick_layout.addWidget(go)
        layout.addWidget(quick)

        self.sidebar_tabs = QTabWidget()
        self.sidebar_tabs.setObjectName("sidebarPages")
        self.sidebar_tabs.setDocumentMode(True)

        sessions = QWidget()
        s_layout = QVBoxLayout(sessions)
        s_layout.setContentsMargins(4, 4, 4, 4)
        s_layout.setSpacing(4)
        titlebar = QFrame()
        titlebar.setObjectName("sideTitlebar")
        t_layout = QHBoxLayout(titlebar)
        t_layout.setContentsMargins(4, 2, 0, 2)
        title = QLabel(tr("Sessioni"))
        title.setObjectName("sideTitle")
        t_layout.addWidget(title, 1)
        hide = QToolButton()
        hide.setProperty("role", "flat")
        hide.setToolTip(tr("Nascondi pannello sessioni"))
        self._bind_icon(hide.setIcon, "fth-act-hide")
        hide.clicked.connect(lambda: self.view_toggles["show_sidebar"].setChecked(False))
        t_layout.addWidget(hide)
        s_layout.addWidget(titlebar)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Cerca sessioni…"))
        self.search.setClearButtonEnabled(True)
        search_icon = self.search.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        self._bind_icon(search_icon.setIcon, "fth-act-search")
        self.search.textChanged.connect(self.refresh_tree)
        s_layout.addWidget(self.search)
        toolbar = QFrame()
        toolbar.setObjectName("sessionToolbar")
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(0, 0, 0, 0)
        tb_layout.setSpacing(3)
        for act, text in ((self.act_new_session, tr("Nuova sessione")), (self.act_new_folder, tr("Nuova cartella")),
                          (self.act_edit, tr("Modifica")), (self.act_duplicate, tr("Duplica")),
                          (self.act_move, tr("Sposta")), (self.act_delete, tr("Elimina"))):
            button = QToolButton()
            button.setProperty("role", "mini")
            button.setDefaultAction(act)
            button.setToolTip(text)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            tb_layout.addWidget(button)
        s_layout.addWidget(toolbar)
        self.tree_model = QStandardItemModel(self)
        self.tree = SessionTree(self.drop_node)
        self.tree.setModel(self.tree_model)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        # Solo activated (doppio clic e Invio): con anche doubleClicked un doppio clic aprirebbe due schede.
        self.tree.activated.connect(lambda index: self.open_path(index.data(PATH_ROLE)))
        self.tree.pressed.connect(self.mark_activity)
        s_layout.addWidget(self.tree, 1)
        footer = QFrame()
        footer.setObjectName("sideFooter")
        f_layout = QHBoxLayout(footer)
        f_layout.setContentsMargins(0, 0, 0, 0)
        f_layout.setSpacing(4)
        for act in (self.act_expand, self.act_collapse):
            button = QToolButton()
            button.setProperty("role", "mini")
            button.setDefaultAction(act)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            f_layout.addWidget(button)
        s_layout.addWidget(footer)
        self.sidebar_tabs.addTab(sessions, tr("Sessioni"))

        # File remoti della scheda SSH aperta: ogni scheda ha il suo pannello, qui se ne mostra uno.
        self.files_stack = QStackedWidget()
        placeholder = QWidget()
        p_layout = QVBoxLayout(placeholder)
        p_layout.addStretch(1)
        p_label = QLabel(tr("I file remoti si vedono qui quando la scheda aperta è una sessione SSH."))
        p_label.setWordWrap(True)
        p_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        p_label.setProperty("role", "muted")
        p_layout.addWidget(p_label)
        p_button = QPushButton(tr("Mostra i file della sessione"))
        p_button.clicked.connect(self._ensure_current_files)
        p_layout.addWidget(p_button, 0, Qt.AlignmentFlag.AlignCenter)
        p_layout.addStretch(2)
        self.files_placeholder = placeholder
        self.files_stack.addWidget(placeholder)
        self.sidebar_tabs.addTab(self.files_stack, tr("File remoti"))
        self.sidebar_tabs.currentChanged.connect(self._sidebar_page_changed)
        layout.addWidget(self.sidebar_tabs, 1)
        self._sync_sidebar_files()
        return sidebar

    def _build_workspace(self) -> QWidget:
        workspace = QWidget()
        workspace.setObjectName("workspace")
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("workTabs")
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.setIconSize(QSize(10, 10))
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._current_tab_changed)
        self.welcome = self._welcome_page()
        layout.addWidget(self.welcome, 1)
        layout.addWidget(self.tabs, 1)
        self._tab_dots: dict = {}
        self.dot_timer = QTimer(self)
        self.dot_timer.setInterval(1000)
        self.dot_timer.timeout.connect(self._refresh_tab_dots)
        self.dot_timer.start()
        return workspace

    def _welcome_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(1)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(72, 72))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo)
        title = QLabel(tr("Benvenuto in {name}").format(name=model.APP_NAME))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 26px; font-weight: 600;")
        layout.addWidget(title)
        layout.addStretch(2)
        return page

    def _update_welcome(self) -> None:
        empty = self.tabs.count() == 0
        self.welcome.setVisible(empty)
        self.tabs.setVisible(not empty)
        if empty:
            if hasattr(self, "open_label"):
                self.open_label.setText(tr("Pronto."))
            self._sync_sidebar_files()

    # ------------------------------------------------------------------ parti della finestra, tema
    def set_part_visible(self, key: str, visible: bool, save: bool = True) -> None:
        widgets = {"show_menubar": lambda: self.menuBar(), "show_ribbon": lambda: self.ribbon,
                   "show_sidebar": lambda: self.sidebar, "show_left_rail": lambda: self.rail,
                   "show_statusbar": lambda: self.statusBar()}
        other = {"show_menubar": "show_ribbon", "show_ribbon": "show_menubar"}.get(key)
        if not visible and other and not getattr(self.settings, other):
            # Senza menu e senza barra dei comandi non si potrebbe più tornare indietro.
            self.view_toggles[key].setChecked(True)
            self.set_status(tr("Il menu e la barra dei comandi non si possono nascondere insieme."))
            return
        widgets[key]().setVisible(visible)
        setattr(self.settings, key, visible)
        if self.view_toggles[key].isChecked() != visible:
            self.view_toggles[key].setChecked(visible)
        if save:
            self._save_settings()

    def show_sessions_panel(self) -> None:
        self.view_toggles["show_sidebar"].setChecked(True)
        self.sidebar_tabs.setCurrentIndex(0)
        self.search.setFocus()
        self.search.selectAll()

    def focus_quick_connect(self) -> None:
        self.view_toggles["show_sidebar"].setChecked(True)
        self.quick_entry.setFocus()
        self.quick_entry.selectAll()

    def _collapse_folders(self) -> None:
        self.tree.collapseAll()
        if self.tree_model.rowCount():
            self.tree.expand(self.tree_model.index(0, 0))  # Home Session resta aperta

    def _update_theme_button(self) -> None:
        # Mostra il tema che si attiva con il clic, come nella versione Linux.
        dark = theme.is_dark()
        self.theme_button.setText(tr("Chiaro") if dark else tr("Scuro"))
        self.theme_button.setToolTip(tr("Passa al tema chiaro") if dark else tr("Passa al tema scuro"))
        self.theme_button.setIcon(icon("fth-theme-light" if dark else "fth-theme-dark"))

    def toggle_theme(self) -> None:
        self.settings.theme = "light" if theme.is_dark() else "dark"
        self._save_settings()
        self.apply_theme()

    def apply_theme(self) -> None:
        theme.apply(QApplication.instance(), self.settings.theme)
        self._refresh_icons()
        self._update_theme_button()
        self._tab_dots.clear()
        self._refresh_tab_dots()
        self.refresh_tree()
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            if isinstance(widget, TerminalTab):
                widget.send_theme()

    def _save_settings(self) -> None:
        try:
            self.settings.save(model.SETTINGS_FILE)
        except OSError:
            pass

    # ------------------------------------------------------------------ schede: pallino di stato e file remoti
    def _add_tab(self, widget: QWidget, title: str) -> int:
        """Una scheda nuova, con il pallino di stato a sinistra e la sua × per chiuderla."""
        index = self.tabs.addTab(widget, title)
        close = QToolButton()
        close.setProperty("role", "tabclose")
        close.setText("✕")
        close.setToolTip(tr("Chiudi la scheda"))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(lambda *_, w=widget: self.close_tab(self.tabs.indexOf(w)))
        self.tabs.tabBar().setTabButton(index, self.tabs.tabBar().ButtonPosition.RightSide, close)
        self.tabs.setCurrentIndex(index)
        self._refresh_tab_dots()
        return index

    def _tab_state(self, widget: QWidget) -> str:
        if isinstance(widget, TerminalTab):
            if widget.running:
                return "ok"
            return "off" if widget.exit_code in (0, None) else "error"
        if isinstance(widget, (RdpTab, VncTab)):
            return "ok" if widget.connected else "pending"
        if isinstance(widget, FilesPanel):
            return "ok" if widget.connected else "pending"
        return "off"

    def _refresh_tab_dots(self) -> None:
        colors = theme.colors()
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            state = self._tab_state(widget)
            if self._tab_dots.get(id(widget)) != state:
                self._tab_dots[id(widget)] = state
                self.tabs.setTabIcon(i, theme.dot_icon(colors[state]))

    def _current_tab_changed(self, _index: int) -> None:
        widget = self.tabs.currentWidget()
        if widget is not None:
            self.open_label.setText(tr("Aperto: {name}").format(name=self.tabs.tabText(self.tabs.currentIndex())))
        self._sync_sidebar_files()

    def _sync_sidebar_files(self) -> None:
        """La scheda «File remoti» del pannello laterale c'è solo quando la scheda aperta è una sessione SSH."""
        if not hasattr(self, "tabs"):
            return
        tab = self.tabs.currentWidget()
        ssh = isinstance(tab, TerminalTab) and tab.has_files
        self.sidebar_tabs.setTabVisible(1, ssh)
        self.sidebar_tabs.tabBar().setVisible(ssh)
        if not ssh:
            if self.sidebar_tabs.currentIndex() == 1:
                self.sidebar_tabs.setCurrentIndex(0)
            return
        panel = tab.files
        if panel is not None and self.files_stack.indexOf(panel) >= 0:
            self.files_stack.setCurrentWidget(panel)
        else:
            self.files_stack.setCurrentWidget(self.files_placeholder)

    def _sidebar_page_changed(self, index: int) -> None:
        if index == 1:
            self._ensure_current_files()

    def _ensure_current_files(self) -> None:
        tab = self.tabs.currentWidget()
        if isinstance(tab, TerminalTab):
            tab.ensure_files()
        self._sync_sidebar_files()

    def _files_created(self, tab: "TerminalTab", panel: FilesPanel) -> None:
        self.files_stack.addWidget(panel)
        if self.tabs.currentWidget() is tab:
            self._sync_sidebar_files()

    def _files_unavailable(self, tab: "TerminalTab") -> None:
        if tab.files is not None:
            self.files_stack.removeWidget(tab.files)
        self._sync_sidebar_files()

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
        home_icon = theme.home_icon()
        folder_cache: dict = {}

        def folder_icons(color: str) -> QIcon:
            if color not in folder_cache:
                folder_cache[color] = theme.folder_icon(color)
            return folder_cache[color]

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
                    home = path == self.store.home_path() and node.name == model.HOME_SESSION_NAME
                    item = QStandardItem(home_icon if home else folder_icons(node.color), node.name)
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    item.setData("folder", KIND_ROLE)
                    if home:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
                else:
                    detail = node.host or node.command or ""
                    item = QStandardItem(icon(PROTOCOL_ICONS.get(node.protocol, "")), node.name)
                    item.setToolTip(f"{node.protocol.upper()}  {detail}".strip())
                    item.setData("session", KIND_ROLE)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
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

    def new_session(self, protocol: str = "") -> None:
        if self.vault.exists() and not self.vault.unlocked:
            self.ensure_vault_unlocked(create_if_missing=False, quiet=True)
        preset = model.Session(name="", protocol=protocol) if protocol else None
        dialog = SessionDialog(self, preset, self.credential_labels(), not self.vault.unlocked, self.tunnel_labels())
        if preset is not None:
            dialog.setWindowTitle(tr("Nuova sessione"))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        sess = dialog.result_session()
        folder = self.target_folder()
        self.store.add_node(folder, sess)
        self.refresh_tree(select=self.store.find_node_path(sess))
        self.set_status(tr("Sessione creata: {name}").format(name=sess.name))

    def new_folder(self) -> None:
        dialog = FolderDialog(self, tr("Nuova cartella"))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, color = dialog.value()
        folder = model.FolderNode(name=name, color=color)
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
            dialog = FolderDialog(self, tr("Modifica cartella"), node.name, node.color)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                node.name, node.color = dialog.value()
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

    def drop_node(self, source: str, target: str, where: str) -> None:
        """Una voce trascinata nell'albero: dentro una cartella, oppure prima o dopo un'altra voce."""
        if self.store.is_home_path(source):
            return
        try:
            folder, index = drop_target(self.store, target, where)
            new_path = self.store.move_node(source, folder, index)
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

    def export_all(self) -> None:
        home = self.store.home_node()
        dest, _ = QFileDialog.getSaveFileName(self, tr("Esporta tutte le sessioni"),
                                              str(Path.home() / "fedtermhub-home-session.json"), "JSON (*.json)")
        if not dest:
            return
        try:
            model.atomic_write_text(Path(dest), json.dumps(model.session_package(home, "home-session"), indent=2,
                                                           ensure_ascii=False), mode=0o600)
            self.set_status(tr("Home Session esportata in {name}").format(name=Path(dest).name))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, model.APP_NAME, tr("Errore export Home Session: {exc}").format(exc=exc))

    def replace_all_sessions(self) -> None:
        src, _ = QFileDialog.getOpenFileName(self, tr("Sostituisci tutte le sessioni con un file"), str(Path.home()),
                                             "JSON (*.json)")
        if not src:
            return
        try:
            node = model.as_home_session(model.node_from_session_package(
                json.loads(Path(src).read_text(encoding="utf-8")), Path(src).stem))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, model.APP_NAME, tr("Errore import Home Session: {exc}").format(exc=exc))
            return
        answer = QMessageBox.warning(
            self, tr("Sostituire l'attuale Home Session?"),
            tr("Le sessioni attuali verranno sostituite dalla Home Session importata. Le password salvate non vengono modificate."),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Ok:
            return
        self.store.nodes = [node]
        self.store.save()
        self.refresh_tree()
        self.tree.expandAll()
        self.set_status(tr("Home Session importata da {name}").format(name=Path(src).name))

    def export_settings(self) -> None:
        dest, _ = QFileDialog.getSaveFileName(self, tr("Esporta impostazioni FedTermHub"),
                                              str(Path.home() / "fedtermhub-settings.json"), "JSON (*.json)")
        if not dest:
            return
        try:
            model.atomic_write_text(Path(dest), json.dumps(self.settings.to_dict(), indent=2, ensure_ascii=False), mode=0o600)
            self.set_status(tr("Impostazioni esportate in {dest}").format(dest=dest))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, model.APP_NAME, tr("Errore esportazione impostazioni: {exc}").format(exc=exc))

    def import_settings(self) -> None:
        src, _ = QFileDialog.getOpenFileName(self, tr("Importa impostazioni FedTermHub"), str(Path.home()), "JSON (*.json)")
        if not src:
            return
        try:
            raw = json.loads(Path(src).read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not ({"theme", "icon_theme", "terminal_font_family"} & set(raw.keys())):
                raise ValueError(tr("Il file non contiene impostazioni FedTermHub riconoscibili."))
            # Le stesse impostazioni della versione Linux: quelle che qui non servono restano nel file.
            imported = model.AppSettings.load(Path(src))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, model.APP_NAME, tr("Errore importazione impostazioni: {exc}").format(exc=exc))
            return
        for key, value in vars(imported).items():
            setattr(self.settings, key, value)
        self._save_settings()
        self.apply_theme()
        for key, act in self.view_toggles.items():
            self.set_part_visible(key, bool(getattr(self.settings, key)), save=False)
        self.set_status(tr("Impostazioni importate da {name}").format(name=Path(src).name))

    def open_log(self) -> None:
        LogDialog(self).exec()

    def show_shortcuts(self) -> None:
        actions = [self.act_new_session, self.act_quick, self.act_local, self.act_close_tab,
                   self.view_toggles["show_sidebar"], self.act_edit, self.act_duplicate, self.act_delete,
                   self.act_tunnels, self.act_net_tools, self.act_settings, self.act_guide, self.act_quit]
        rows = [(act.shortcut().toString(QKeySequence.SequenceFormat.NativeText), act.text().replace("&", ""))
                for act in actions if not act.shortcut().isEmpty()]
        rows.insert(1, ("Ctrl+F", tr("Cerca sessioni")))
        outside = tr("(con il fuoco fuori dal terminale)")
        rows += [("Ctrl+Shift+E", f"{tr('File remoti')} {outside}"), ("Ctrl+Shift+P", f"{tr('Password Manager')} {outside}")]
        ShortcutsDialog(self, rows).exec()

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
        if self.vault.exists() and not self.vault.unlocked:
            self.ensure_vault_unlocked(create_if_missing=False, quiet=True)
        dialog = TunnelsDialog(self, self.tunnels, self.credential_labels, self)
        dialog.exec()
        dialog.deleteLater()

    def sessions_using_tunnel(self, tunnel_id: str) -> List[model.Session]:
        return [sess for _path, sess, _folder in self.store.iter_sessions() if sess.required_tunnel_id == tunnel_id]

    def unlink_sessions(self, sessions: List[model.Session]) -> None:
        """Il tunnel non c'è più: le sessioni che lo usavano tornano a collegarsi direttamente."""
        for sess in sessions:
            sess.required_tunnel_id = ""
            if sess.routing_mode == "saved_tunnel":
                sess.routing_mode = "direct"
        self.store.save()
        self.refresh_tree()

    def create_linked_session(self, tunnel: model.TunnelSession, parent: QWidget) -> None:
        """Una sessione nuova che passa dal tunnel locale scelto (Crea sessione nel gestore dei tunnel)."""
        dialog = SessionDialog(parent, tunnel.linked_session(), self.credential_labels(), not self.vault.unlocked,
                               self.tunnel_labels())
        dialog.setWindowTitle(tr("Nuova sessione"))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            sess = dialog.result_session()
            self.store.add_node(self.store.home_path(), sess)
            self.refresh_tree(select=self.store.find_node_path(sess))
            self.set_status(tr("Sessione creata: {name}").format(name=sess.name))
        dialog.deleteLater()

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

    def open_split_terminal(self) -> TerminalTab:
        """Una scheda locale che nasce divisa in due riquadri affiancati."""
        launch = connect.local_shell(self.settings)
        launch.title = tr("Split")
        tab = self.add_terminal_tab(launch, protocol="local")
        tab.split_tab = True
        tab.split("tall")
        return tab

    def pane_action(self, action: str) -> None:
        """Una voce del menu Riquadri: agisce sulla scheda aperta."""
        tab = self.tabs.currentWidget()
        if isinstance(tab, TerminalTab):
            tab.dispatch(action)
        else:
            self.set_status(tr("I riquadri ci sono nelle schede terminale."))

    def pane_key(self, action: str) -> None:
        """Un tasto dei riquadri (termcore.PANE_KEYS)."""
        tab = self.tabs.currentWidget()
        fallback = self.pane_key_fallbacks.get(action)
        if isinstance(tab, TerminalTab) and (fallback is None or self._focused_pane() in tab.panes):
            tab.dispatch(action)
        elif fallback is not None:
            fallback.trigger()

    @staticmethod
    def _focused_pane() -> Optional[TerminalPane]:
        widget = QApplication.focusWidget()
        while widget is not None and not isinstance(widget, TerminalPane):
            widget = widget.parentWidget()
        return widget

    def _focus_changed(self, _old, _new) -> None:  # noqa: ANN001
        """Il riquadro attivo lo decide l'utente: un clic lo cambia, un passaggio di fuoco non chiesto no.

        Fuoco e riquadro attivo restano così sempre allineati: i tasti vanno dove l'utente si aspetta.
        """
        pane = self._focused_pane()
        if pane is None or pane.tab.relaying or pane is pane.tab.active_pane:
            return
        if QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            pane.tab.set_active(pane)
        else:
            QTimer.singleShot(0, pane.tab.focus_active)  # non dentro il cambio di fuoco ancora in corso

    def multiexec_targets(self) -> List[Tuple[object, str, str, bool]]:
        """Le schede terminale con un programma aperto: (scheda, titolo, tipo, già spuntata)."""
        targets = []
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            if isinstance(tab, TerminalTab) and tab.running:
                kind, default = termcore.multiexec_target(tab.protocol, tab.split_tab)
                targets.append((tab, self.tabs.tabText(index), kind, default))
        return termcore.multiexec_defaults(targets)

    def send_multiexec(self, command: str, tabs: List[TerminalTab]) -> int:
        """Manda il comando a tutti i riquadri delle schede scelte; quanti terminali l'hanno ricevuto."""
        line = termcore.multiexec_line(command, "\r")
        sent = sum(tab.feed_all(line) for tab in tabs)
        self.set_status(tr("MultiExec: comando inviato a {sent} terminali.").format(sent=sent))
        return sent

    def show_multiexec_dialog(self) -> None:
        targets = self.multiexec_targets()
        if not targets:
            self.set_status(tr("Nessuna scheda terminale aperta per MultiExec."))
            return
        dialog = MultiExecDialog(self, targets)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Mentre il dialogo era aperto una scheda può essersi chiusa.
            chosen = [tab for tab in dialog.selected_targets() if self.tabs.indexOf(tab) >= 0]
            if dialog.command_text() and chosen:
                self.send_multiexec(dialog.command_text(), chosen)
        dialog.deleteLater()

    def quick_connect(self, text: str = "") -> None:
        """Connessione rapida dal campo in cima al pannello: utente@host[:porta], rdp:host o telnet:host."""
        text = (text or self.quick_entry.text()).strip()
        if not text:
            self.focus_quick_connect()
            self.set_status(tr("Scrivi utente@host[:porta] per SSH, rdp:host per il desktop remoto o telnet:host."))
            return
        self.quick_entry.clear()
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
        self._add_tab(panel, sess.name or sess.host)
        self._update_welcome()
        panel.connect_server()

    def open_vnc(self, sess: model.Session) -> None:
        user, _domain, password = self.identity(sess)
        tab = VncTab(self.tabs, sess, user, password)
        tab.status.connect(self.set_status)
        self._add_tab(tab, sess.name or sess.host)
        self._update_welcome()

    def quick_telnet(self) -> None:
        text, ok = QInputDialog.getText(self, tr("Connessione Telnet"), tr("Host[:porta]"))
        if ok and text.strip():
            self.quick_connect("telnet:" + text.strip())

    def run_quick_tool(self, name: str) -> None:
        self.open_command_tab(tr(name), quick_tools.argv(name))

    def open_network_tools(self, page: str = "") -> None:
        if getattr(self, "_net_tools", None) is None:
            self._net_tools = NetworkToolsDialog(self, self.open_command_tab, self.create_session_for)
        if page:
            self._net_tools.show_page(page)
        self._net_tools.show()
        self._net_tools.raise_()
        self._net_tools.activateWindow()

    def open_command_tab(self, title: str, argv: List[str]) -> None:
        binary = connect.find_program(argv[0])
        if not binary:
            QMessageBox.warning(self, model.APP_NAME, tr("Programma non trovato: {name}").format(name=argv[0]))
            return
        self.add_terminal_tab(connect.Launch(title, [binary] + argv[1:], {}, close_on_success=False), protocol="tool")

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
        """File remoti della scheda SSH aperta, nel pannello laterale (e ritorno alle sessioni)."""
        tab = self.tabs.currentWidget()
        if not (isinstance(tab, TerminalTab) and tab.has_files):
            self.set_status(tr("Il pannello dei file remoti c'è nelle schede SSH."))
            return
        if self.sidebar.isVisible() and self.sidebar_tabs.currentIndex() == 1:
            self.sidebar_tabs.setCurrentIndex(0)
            return
        self.view_toggles["show_sidebar"].setChecked(True)
        self._sync_sidebar_files()
        self.sidebar_tabs.setCurrentIndex(1)

    def add_terminal_tab(self, launch: connect.Launch, protocol: str = "",
                         files_target: Optional[Callable[[], SftpTarget]] = None) -> TerminalTab:
        tab = TerminalTab(self.tabs, launch, self.settings, files_target=files_target, protocol=protocol)
        tab.status.connect(self.set_status)
        tab.files_created.connect(lambda panel, t=tab: self._files_created(t, panel))
        tab.files_unavailable.connect(lambda t=tab: self._files_unavailable(t))
        tab.activity.connect(self.mark_activity)
        tab.close_requested.connect(lambda t=tab: self.close_tab(self.tabs.indexOf(t)))
        self._add_tab(tab, launch.title)
        tab.finished.connect(lambda code, t=tab: self._terminal_finished(t, code))
        self._update_welcome()
        QTimer.singleShot(300, tab.focus_active)
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
        self._add_tab(tab, sess.name or sess.host)
        self._update_welcome()

    def close_tab(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is None:
            return
        if isinstance(widget, TerminalTab) and widget.files is not None:
            self.files_stack.removeWidget(widget.files)
        close = getattr(widget, "close_tab", None)
        if close:
            close()
        self.tabs.removeTab(index)
        self._tab_dots.pop(id(widget), None)
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
        if self.sidebar.isVisible():
            self.settings.sidebar_width = max(190, min(520, self.splitter.sizes()[0]))
        self._save_settings()
        self.vault.lock()
        event.accept()
        if self._pending_setup is not None:
            try:
                updates.launch_setup(self._pending_setup)
            except OSError as exc:
                QMessageBox.warning(None, tr("Aggiornamento non riuscito"), str(exc))

    # ------------------------------------------------------------------ impostazioni, aggiornamenti, informazioni
    def open_settings(self) -> None:
        dialog = SettingsDialog(self, self.settings)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.apply()
            self._save_settings()
            self.apply_theme()
            self.set_status(tr("Impostazioni applicate"))

    def _check_updates(self, manual: bool = False) -> None:
        def done(release: Optional[updates.Release]) -> None:
            if release is None:
                if manual:
                    QMessageBox.warning(self, tr("Aggiornamenti"), tr("Non riesco a raggiungere GitHub per controllare "
                                                                         "gli aggiornamenti. Controlla la connessione."))
                return
            if not updates.newer(release.version, model.APP_VERSION):
                if manual:
                    QMessageBox.information(self, tr("Aggiornamenti"), tr("Hai già l'ultima versione ({version}).").format(
                        version=model.APP_VERSION))
                return
            self._offer_update(release, ask=manual or release.version not in self._update_offered)

        updates.check_async(self, done)

    def _offer_update(self, release: "updates.Release", ask: bool) -> None:
        self._update_offered.add(release.version)
        if self._update_button is None:
            self._update_button = QPushButton()
            self._update_button.setProperty("role", "accent")
            self.statusBar().addPermanentWidget(self._update_button)
        try:
            self._update_button.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        self._update_button.setText(tr("Aggiorna a {version}").format(version=release.version))
        self._update_button.clicked.connect(lambda: self._ask_update(release))
        self._update_button.show()
        if ask:
            self._ask_update(release)

    def _ask_update(self, release: "updates.Release") -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(tr("Nuova versione di FedTermHub"))
        box.setText(tr("È disponibile FedTermHub {new} (questa è la {current}).").format(new=release.version,
                                                                                          current=model.APP_VERSION))
        box.setInformativeText(tr("Aggiorno adesso? Scarico il programma di installazione, chiudo FedTermHub e lo "
                                  "riapro alla fine. Sessioni, tunnel e password restano."))
        update = box.addButton(tr("Aggiorna ora"), QMessageBox.ButtonRole.AcceptRole)
        notes = box.addButton(tr("Novità"), QMessageBox.ButtonRole.HelpRole)
        box.addButton(tr("Più tardi"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(update)
        box.exec()
        if box.clickedButton() is notes:
            QDesktopServices.openUrl(QUrl(release.page))
        elif box.clickedButton() is update:
            if release.setup_url:
                self._download_update(release)
            else:
                QDesktopServices.openUrl(QUrl(release.page))

    def _download_update(self, release: "updates.Release") -> None:
        cancel = threading.Event()
        dialog = QProgressDialog(tr("Scarico FedTermHub {version}…").format(version=release.version), tr("Annulla"), 0, 1000, self)
        dialog.setWindowTitle(tr("Aggiornamento"))
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.canceled.connect(cancel.set)

        def progress(done: int, total: int) -> None:
            if total:
                dialog.setValue(int(done * 1000 / total))
                dialog.setLabelText(tr("Scarico FedTermHub {version}… {done} di {total} MB").format(
                    version=release.version, done=done // 1048576, total=total // 1048576))

        def finished(path) -> None:  # noqa: ANN001
            dialog.close()
            self._pending_setup = path
            if not self.close():  # chiusura annullata (sessioni aperte): l'installazione non parte
                self._pending_setup = None
                self.set_status(tr("Aggiornamento rimandato: chiudi FedTermHub e riprova da Aiuto › Cerca aggiornamenti."), 0)

        def failed(message: str) -> None:
            dialog.close()
            if not cancel.is_set():
                QMessageBox.warning(self, tr("Aggiornamento non riuscito"),
                                    tr("{error}\n\nPuoi scaricare il Setup.exe dalla pagina della release.").format(error=message))
                QDesktopServices.openUrl(QUrl(release.page))

        updates.download_async(self, release, progress, finished, failed, cancel)
        dialog.show()

    def about(self) -> None:
        QMessageBox.about(
            self, tr("Informazioni su {APP_NAME}").format(APP_NAME=model.APP_NAME),
            f"<b>{model.APP_NAME} {model.APP_VERSION}</b><br><br>"
            + tr("Gestore integrato di terminali e connessioni remote.") + "<br><br>"
            + tr("Configurazione locale: {path}").format(path=model.CONFIG_DIR) + "<br><br>"
            + "GPL-3.0-or-later · © 2026 Crash<br>"
            + '<a href="https://github.com/Crash-FD01/FedTermHub">github.com/Crash-FD01/FedTermHub</a>',
        )
