# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pannello dei file remoti (SFTP) dell'interfaccia Qt.

Si apre come scheda per le sessioni SFTP e accanto al terminale per le sessioni SSH; lì può seguire la
cartella della shell (OSC 7), come nella versione Linux. Si trascinano file e cartelle dal PC per caricarli;
doppio clic su un file lo scarica in una cartella temporanea e lo apre con il programma predefinito.
"""
from __future__ import annotations

import posixpath
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QStandardPaths, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStyle,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..i18n import tr
from . import theme
from .sftp_client import HostKeyQuestion, RemoteItem, SftpTarget, SftpWorker

ITEM_ROLE = Qt.ItemDataRole.UserRole + 10


def format_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(value)


class _DropTree(QTreeView):
    files_dropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)

    def dragEnterEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: ANN001, N802
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)
        else:
            super().dropEvent(event)


class FilesPanel(QWidget):
    """Sfoglia, scarica e carica file su un server SFTP. Segnali: status (testo per la barra di stato)."""

    status = Signal(str)
    connected_changed = Signal(bool)
    sftp_missing = Signal(str)  # il server non offre SFTP: chi ospita il pannello può chiuderlo

    def __init__(self, target: SftpTarget, parent: Optional[QWidget] = None, follow_terminal: bool = False,
                 compact: bool = False, ask_password: bool = True) -> None:
        super().__init__(parent)
        self.target = target
        # Pannello accanto al terminale: niente finestre a sorpresa, la password si chiede col pulsante «Accedi…».
        self.ask_password = ask_password
        self.cwd = ""
        self.home = ""
        self.connected = False
        self.follow = follow_terminal
        self.pending_follow: Optional[str] = None
        self.worker = SftpWorker(self)
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.worker.host_key.connect(self._host_key)
        self.worker.auth_needed.connect(self._auth_needed)
        self.worker.progress.connect(self._progress)
        self._transfer_started = 0.0
        self._open_after_download: Optional[Path] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4 if compact else 8, 4, 4 if compact else 8, 4)
        layout.setSpacing(4)
        bar = QHBoxLayout()
        bar.setSpacing(2)
        icons = Path(__file__).resolve().parent.parent / "icons"

        def tool(name: str, tip: str, slot, text: str = "") -> QToolButton:  # noqa: ANN001
            button = QToolButton()
            button.setIcon(theme.themed_icon(icons / f"{name}.svg"))
            button.setToolTip(tip)
            button.setAutoRaise(True)
            if text and not compact:
                button.setText(text)
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.clicked.connect(slot)
            bar.addWidget(button)
            return button

        tool("fth-act-up", tr("Cartella superiore"), self.go_up)
        tool("fth-act-home", tr("Cartella home"), lambda: self.navigate(self.home))
        tool("fth-act-refresh", tr("Aggiorna"), self.refresh)
        self.path = QLineEdit()
        self.path.setPlaceholderText(tr("Percorso remoto"))
        self.path.returnPressed.connect(lambda: self.navigate(self.path.text().strip()))
        if not compact:
            bar.addWidget(self.path, 1)
        tool("fth-act-folder-new", tr("Nuova cartella"), self.new_folder)
        tool("fth-act-upload", tr("Carica file…"), self.upload_dialog, tr("Carica"))
        tool("fth-act-download", tr("Scarica la selezione…"), self.download_selected, tr("Scarica"))
        if compact:
            bar.addStretch(1)
        self.follow_button = QToolButton()
        self.follow_button.setText(tr("Segui"))
        self.follow_button.setCheckable(True)
        self.follow_button.setChecked(follow_terminal)
        self.follow_button.setToolTip(tr("Segui la cartella del terminale (cd)"))
        self.follow_button.toggled.connect(self._follow_toggled)
        self.follow_button.setVisible(compact)
        bar.addWidget(self.follow_button)
        layout.addLayout(bar)
        if compact:
            layout.addWidget(self.path)

        self.model = QStandardItemModel(self)
        self.model.setHorizontalHeaderLabels([tr("Nome"), tr("Dimensione"), tr("Modificato"), tr("Permessi")])
        self.tree = _DropTree()
        self.tree.setModel(self.model)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSortingEnabled(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.doubleClicked.connect(self._activated)
        self.tree.files_dropped.connect(self.upload)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        if compact:
            self.tree.hideColumn(2)
            self.tree.hideColumn(3)
        layout.addWidget(self.tree, 1)

        foot = QHBoxLayout()
        self.state = QLabel(tr("Connessione a {target}…").format(target=target.label))
        self.state.setProperty("role", "muted")
        self.state.setWordWrap(True)
        foot.addWidget(self.state, 1)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(160)
        self.progress.hide()
        foot.addWidget(self.progress)
        self.cancel_button = QPushButton(tr("Annulla"))
        self.cancel_button.clicked.connect(self.worker.cancel.set)
        self.cancel_button.hide()
        foot.addWidget(self.cancel_button)
        self.reconnect_button = QPushButton(tr("Riconnetti"))
        self.reconnect_button.clicked.connect(self._reconnect_clicked)
        self._needs_password = False
        self.reconnect_button.hide()
        foot.addWidget(self.reconnect_button)
        layout.addLayout(foot)

    # ------------------------------------------------------------------ connessione
    def connect_server(self) -> None:
        self.reconnect_button.hide()
        self.state.setText(tr("Connessione a {target}…").format(target=self.target.label))
        self.worker.connect_to(self.target)

    def _host_key(self, tag: str, question: HostKeyQuestion) -> None:
        answer = QMessageBox.question(
            self, tr("Chiave dell'host sconosciuta"),
            tr("È la prima connessione a {host}.\n\nTipo di chiave: {kind}\nImpronta: {fp}\n\nSe l'impronta è quella del "
               "server, accettala: verrà salvata in known_hosts (la usa anche il terminale SSH).").format(
                host=question.host, kind=question.key_type, fp=question.fingerprint),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.target.approved_fingerprints.append(question.fingerprint)
            self.connect_server()
        else:
            self._failed(tag, tr("Chiave dell'host non accettata."))

    def _reconnect_clicked(self) -> None:
        if self._needs_password:
            self._ask_password()
        else:
            self.connect_server()

    def _auth_needed(self, _tag: str) -> None:
        if not self.ask_password:
            self._needs_password = True
            self.state.setText(tr("Il server chiede una password per i file remoti."))
            self.reconnect_button.setText(tr("Accedi…"))
            self.reconnect_button.show()
            return
        self._ask_password()

    def _ask_password(self) -> None:
        self._needs_password = False
        self.reconnect_button.setText(tr("Riconnetti"))
        password, ok = QInputDialog.getText(
            self, tr("Password SFTP"), tr("Password per {target}:").format(target=self.target.label),
            QLineEdit.EchoMode.Password)
        if ok and password:
            self.target.password = password
            self.connect_server()
        else:
            self._failed("connect", tr("Nessuna chiave accettata dal server e nessuna password."))

    # ------------------------------------------------------------------ risultati
    def _done(self, tag: str, result: object) -> None:
        if tag == "connect":
            self.connected = True
            self.home = str(result)
            self.connected_changed.emit(True)
            start = self.pending_follow or self.home
            self.pending_follow = None
            self.navigate(start)
            return
        if tag.startswith("list:"):
            path = tag[5:]
            self.cwd = path
            self.path.setText(path)
            self._fill(result)  # type: ignore[arg-type]
            self.state.setText(tr("{target} — {count} elementi").format(target=self.target.label, count=len(result)))
            return
        if tag in {"upload", "download", "open"}:
            self._end_transfer()
            if tag == "open" and self._open_after_download is not None:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._open_after_download)))
                self._open_after_download = None
            message = {"upload": tr("Caricamento completato"), "download": tr("Download completato"),
                       "open": tr("File aperto")}[tag]
            self.status.emit(message)
            self.state.setText(message)
            if tag == "upload":
                self.refresh()
            return
        self.refresh()

    def _failed(self, tag: str, message: str) -> None:
        if tag in {"upload", "download", "open"}:
            self._end_transfer()
        if tag == "nosftp":
            self.connected = False
            self.state.setText(message)
            self.sftp_missing.emit(message)
            return
        if tag == "connect":
            self.connected = False
            self.connected_changed.emit(False)
            self.reconnect_button.show()
            self.state.setText(tr("Non connesso: {error}").format(error=message))
            self.status.emit(tr("SFTP: {error}").format(error=message))
            return
        if tag.startswith("list:") and tag[5:] != self.home and self.connected:
            # Cartella sparita o non leggibile (anche quella del terminale): si torna alla home.
            self.status.emit(tr("SFTP: {error}").format(error=message))
            self.navigate(self.home)
            return
        if not (self.worker.session and self.worker.session.connected):
            self.connected = False
            self.reconnect_button.show()
        self.state.setText(message)
        self.status.emit(tr("SFTP: {error}").format(error=message))

    def _fill(self, items: List[RemoteItem]) -> None:
        self.model.removeRows(0, self.model.rowCount())
        style = self.style()
        folder = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        file_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        link_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileLinkIcon)
        for item in items:
            name = QStandardItem(folder if item.is_dir else (link_icon if item.is_link else file_icon), item.name)
            name.setData(item, ITEM_ROLE)
            size = QStandardItem("" if item.is_dir else format_size(item.size))
            size.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            modified = QStandardItem(time.strftime("%d/%m/%Y %H:%M", time.localtime(item.mtime)) if item.mtime else "")
            perms = QStandardItem(item.permissions)
            self.model.appendRow([name, size, modified, perms])

    # ------------------------------------------------------------------ navigazione
    def navigate(self, path: str) -> None:
        if not path:
            return
        if not self.connected:
            self.pending_follow = path
            return
        path = posixpath.normpath(path) if path != "/" else "/"
        self.worker.run(f"list:{path}", lambda s: s.listdir(path))

    def refresh(self) -> None:
        if not self.connected:
            self.connect_server()
            return
        self.navigate(self.cwd or self.home)

    def go_up(self) -> None:
        if self.cwd and self.cwd != "/":
            self.navigate(posixpath.dirname(self.cwd.rstrip("/")) or "/")

    def follow_directory(self, path: str) -> None:
        """La shell del terminale ha cambiato cartella (OSC 7)."""
        if not self.follow or not path or path == self.cwd:
            return
        self.navigate(path)

    def _follow_toggled(self, active: bool) -> None:
        self.follow = active

    def selected_items(self) -> List[RemoteItem]:
        rows = {index.row() for index in self.tree.selectionModel().selectedRows()}
        return [self.model.item(row, 0).data(ITEM_ROLE) for row in sorted(rows)]

    def _activated(self, index) -> None:  # noqa: ANN001
        item: RemoteItem = self.model.item(index.row(), 0).data(ITEM_ROLE)
        if item.is_dir:
            self.navigate(item.path)
        else:
            self.open_item(item)

    # ------------------------------------------------------------------ trasferimenti
    def _begin_transfer(self, text: str) -> None:
        self.worker.cancel.clear()
        self._transfer_started = time.monotonic()
        self.progress.setRange(0, 0)
        self.progress.show()
        self.cancel_button.show()
        self.state.setText(text)

    def _end_transfer(self) -> None:
        self.progress.hide()
        self.cancel_button.hide()

    def _progress(self, done: object, total: object) -> None:
        total_i, done_i = int(total or 0), int(done or 0)
        if total_i > 0:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(done_i * 1000 / total_i))
            elapsed = max(0.001, time.monotonic() - self._transfer_started)
            self.progress.setFormat(f"{format_size(done_i)} · {format_size(int(done_i / elapsed))}/s")

    def upload(self, paths: List[Path]) -> None:
        if not self.connected or not paths:
            return
        target_dir = self.cwd
        names = ", ".join(p.name for p in paths[:3]) + ("…" if len(paths) > 3 else "")
        self._begin_transfer(tr("Caricamento di {names} in {folder}…").format(names=names, folder=target_dir))
        callback = self.worker.progress_callback()

        def job(session) -> None:  # noqa: ANN001
            for path in paths:
                session.upload(path, target_dir, callback)

        self.worker.run("upload", job)

    def upload_dialog(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, tr("Carica file in {folder}").format(folder=self.cwd))
        if files:
            self.upload([Path(f) for f in files])

    def _download_dir(self) -> Path:
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation) or str(Path.home())
        return Path(base)

    def download_selected(self) -> None:
        items = self.selected_items()
        if not items:
            self.state.setText(tr("Seleziona prima i file da scaricare."))
            return
        folder = QFileDialog.getExistingDirectory(self, tr("Scarica in"), str(self._download_dir()))
        if not folder:
            return
        dest = Path(folder)
        self._begin_transfer(tr("Download di {count} elementi in {folder}…").format(count=len(items), folder=dest))
        callback = self.worker.progress_callback()

        def job(session) -> None:  # noqa: ANN001
            for item in items:
                session.download(item.path, dest / item.name, callback)

        self.worker.run("download", job)

    def open_item(self, item: RemoteItem) -> None:
        folder = Path(tempfile.gettempdir()) / "FedTermHub" / self.target.host.replace(":", "_")
        folder.mkdir(parents=True, exist_ok=True)
        local = folder / item.name
        self._open_after_download = local
        self._begin_transfer(tr("Apro {name}…").format(name=item.name))
        callback = self.worker.progress_callback()
        self.worker.run("open", lambda s: s.download(item.path, local, callback))

    # ------------------------------------------------------------------ altre azioni
    def new_folder(self) -> None:
        if not self.connected:
            return
        name, ok = QInputDialog.getText(self, tr("Nuova cartella"), tr("Nome della cartella in {folder}:").format(folder=self.cwd))
        name = name.strip().strip("/")
        if ok and name:
            path = posixpath.join(self.cwd, name)
            self.worker.run("mkdir", lambda s: s.mkdir(path))

    def rename_item(self, item: RemoteItem) -> None:
        name, ok = QInputDialog.getText(self, tr("Rinomina"), tr("Nuovo nome:"), text=item.name)
        name = name.strip().strip("/")
        if ok and name and name != item.name:
            new = posixpath.join(posixpath.dirname(item.path), name)
            self.worker.run("rename", lambda s: s.rename(item.path, new))

    def delete_items(self, items: List[RemoteItem]) -> None:
        if not items:
            return
        names = "\n".join(("📁 " if i.is_dir else "") + i.name for i in items[:10]) + ("\n…" if len(items) > 10 else "")
        answer = QMessageBox.warning(
            self, tr("Eliminare dal server?"),
            tr("Questi elementi verranno eliminati dal server, comprese le cartelle con tutto il loro contenuto. "
               "Non si può annullare.\n\n{names}").format(names=names),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            self.worker.run("delete", lambda s: [s.remove(i.path) for i in items])

    def _menu(self, pos) -> None:  # noqa: ANN001
        items = self.selected_items()
        menu = QMenu(self)
        if len(items) == 1 and not items[0].is_dir:
            menu.addAction(tr("Apri"), lambda: self.open_item(items[0]))
        if items:
            menu.addAction(tr("Scarica…"), self.download_selected)
        if len(items) == 1:
            menu.addAction(tr("Rinomina…"), lambda: self.rename_item(items[0]))
            menu.addAction(tr("Copia il percorso"), lambda: QGuiApplication.clipboard().setText(items[0].path))
        if items:
            menu.addAction(tr("Elimina…"), lambda: self.delete_items(items))
            menu.addSeparator()
        menu.addAction(tr("Carica file…"), self.upload_dialog)
        menu.addAction(tr("Nuova cartella…"), self.new_folder)
        menu.addAction(tr("Aggiorna"), self.refresh)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def shutdown(self) -> None:
        self.worker.shutdown()
