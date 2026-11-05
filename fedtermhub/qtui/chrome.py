# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pezzi della finestra principale che imitano la versione Linux: albero delle sessioni con trascinamento,
finestra della cartella con il colore, log dell'applicazione, scorciatoie da tastiera, titoli nei menu."""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTreeView,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from .. import model
from ..i18n import tr

PATH_ROLE = Qt.ItemDataRole.UserRole + 1
KIND_ROLE = Qt.ItemDataRole.UserRole + 2


def add_heading(menu: QMenu, text: str) -> None:
    """Un titolo di sezione nelle tendine (come «Crea sessione», «Organizza» nella versione Linux)."""
    label = QLabel(text)
    label.setProperty("role", "menuheading")
    action = QWidgetAction(menu)
    action.setDefaultWidget(label)
    action.setEnabled(False)
    menu.addAction(action)


class SessionTree(QTreeView):
    """L'albero delle sessioni: si trascina una voce sopra una cartella per spostarla, prima o dopo
    un'altra voce per riordinarla (come nella versione Linux)."""

    def __init__(self, on_drop: Callable[[str, str, str], None]) -> None:
        super().__init__()
        self.on_drop = on_drop
        self.setObjectName("sessionTree")
        self.setHeaderHidden(True)
        self.setIndentation(14)
        self.setAnimated(True)
        self.setUniformRowHeights(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setToolTip(tr("Doppio clic per aprire. Trascina una sessione sopra una cartella per spostarla; "
                           "trascinala prima o dopo un'altra voce per riordinarla."))

    def dropEvent(self, event) -> None:  # noqa: ANN001, N802
        source = self.currentIndex().data(PATH_ROLE)
        target_index = self.indexAt(event.position().toPoint())
        target = target_index.data(PATH_ROLE) if target_index.isValid() else ""
        where = {
            QAbstractItemView.DropIndicatorPosition.OnItem: "into",
            QAbstractItemView.DropIndicatorPosition.AboveItem: "before",
            QAbstractItemView.DropIndicatorPosition.BelowItem: "after",
        }.get(self.dropIndicatorPosition(), "into")
        # Il modello si ricostruisce dalle sessioni salvate: Qt non deve spostare le righe da solo.
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        if source is not None and source != target:
            self.on_drop(source, target or "", where)


class FolderDialog(QDialog):
    """Nome e colore dell'icona di una cartella."""

    def __init__(self, parent: QWidget, title: str, name: str = "", color: str = model.DEFAULT_FOLDER_COLOR) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.color = model.normalize_folder_color(color)
        form = QFormLayout(self)
        self.name = QLineEdit(name)
        form.addRow(tr("Nome cartella"), self.name)
        row = QHBoxLayout()
        self.presets: List[QPushButton] = []
        for label, value in model.FOLDER_COLOR_PRESETS:
            button = QPushButton()
            button.setFixedSize(24, 24)
            button.setToolTip(label)
            button.setCheckable(True)
            button.setProperty("color", value)
            button.clicked.connect(lambda _c=False, v=value: self.set_color(v))
            self.presets.append(button)
            row.addWidget(button)
        other = QPushButton(tr("Altro…"))
        other.clicked.connect(self._pick)
        row.addWidget(other)
        row.addStretch(1)
        form.addRow(tr("Colore icona"), row)
        hint = QLabel(tr("Il colore viene salvato insieme alla cartella."))
        hint.setProperty("role", "muted")
        form.addRow("", hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.set_color(self.color)

    def set_color(self, value: str) -> None:
        self.color = model.normalize_folder_color(value)
        for button in self.presets:
            chosen = button.property("color") == self.color
            button.setChecked(chosen)
            border = "#ffffff" if chosen else "transparent"
            button.setStyleSheet(f"background: {button.property('color')}; border: 2px solid {border}; border-radius: 12px;")

    def _pick(self) -> None:
        picked = QColorDialog.getColor(QColor(self.color), self, tr("Colore icona"))
        if picked.isValid():
            self.set_color(picked.name())

    def _accept(self) -> None:
        if self.name.text().strip():
            self.accept()
        else:
            self.name.setFocus()

    def value(self) -> Tuple[str, str]:
        return self.name.text().strip(), self.color


class LogDialog(QDialog):
    """Il log dell'applicazione (errori non gestiti, problemi all'avvio)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Log applicazione"))
        self.resize(820, 520)
        layout = QVBoxLayout(self)
        path = QLabel(str(model.LOG_FILE))
        path.setProperty("role", "muted")
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(path)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        clear = buttons.addButton(tr("Svuota"), QDialogButtonBox.ButtonRole.ResetRole)
        clear.clicked.connect(self._clear)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.load()

    def load(self) -> None:
        try:
            content = model.LOG_FILE.read_text(encoding="utf-8", errors="replace")[-200_000:]
        except OSError:
            content = ""
        self.text.setPlainText(content or tr("Nessun messaggio nel log."))
        self.text.moveCursor(self.text.textCursor().MoveOperation.End)

    def _clear(self) -> None:
        try:
            model.LOG_FILE.write_text("", encoding="utf-8")
        except OSError:
            pass
        self.load()


class ShortcutsDialog(QDialog):
    def __init__(self, parent: QWidget, actions: List[Tuple[str, str]]) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Scorciatoie da tastiera"))
        layout = QVBoxLayout(self)
        sections = [
            (tr("Applicazione"), actions),
            (tr("Terminale"), [
                (tr("Ctrl+Maiusc+C"), tr("Copia la selezione")),
                (tr("Ctrl+Maiusc+V"), tr("Incolla")),
                (tr("Clic destro"), tr("Copia la selezione, oppure incolla se non c'è selezione")),
                (tr("Ctrl+C"), tr("Va al programma nel terminale (interrompe)")),
                (tr("Ctrl+Maiusc+O / E"), tr("Dividi affiancato / sopra e sotto")),
                (tr("Ctrl+Maiusc+Invio"), tr("Nuovo riquadro")),
                (tr("Ctrl+Maiusc+W"), tr("Chiudi il riquadro (l'ultimo chiude la scheda)")),
                (tr("Ctrl+Maiusc+[ / ]"), tr("Riquadro precedente / successivo")),
                (tr("Ctrl+Maiusc+L"), tr("Cambia disposizione dei riquadri")),
                (tr("Ctrl+Maiusc+P"), tr("Scrivi in tutti i riquadri insieme")),
            ]),
        ]
        for title, rows in sections:
            heading = QLabel(title)
            heading.setProperty("role", "heading")
            layout.addWidget(heading)
            grid = QGridLayout()
            grid.setHorizontalSpacing(24)
            for row, (keys, what) in enumerate(rows):
                key = QLabel(f"<b>{keys}</b>")
                grid.addWidget(key, row, 0)
                grid.addWidget(QLabel(what), row, 1)
            grid.setColumnStretch(1, 1)
            layout.addLayout(grid)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MultiExecDialog(QDialog):
    """Invia un comando a più terminali, scelti uno per uno; le sessioni SSH sono già spuntate."""

    def __init__(self, parent: QWidget, targets: List[Tuple[object, str, str, bool]]) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("MultiExec"))
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        caption = QLabel(tr("&Comando"))
        layout.addWidget(caption)
        self.command = QLineEdit()
        self.command.setPlaceholderText(tr("Es. uptime  (viene inviato seguito da Invio)"))
        caption.setBuddy(self.command)
        layout.addWidget(self.command)
        layout.addWidget(QLabel(tr("Invia a")))
        box = QWidget()
        rows = QVBoxLayout(box)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(2)
        self.checks: List[Tuple[QCheckBox, object]] = []
        for target, title, kind, default in targets:
            check = QCheckBox(f"{title}   ({kind})")
            check.setChecked(default)
            check.toggled.connect(self._update)
            rows.addWidget(check)
            self.checks.append((check, target))
        rows.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(box)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMaximumHeight(260)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("Annulla"))
        self.send_button = buttons.addButton(tr("&Invia"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.send_button.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.command.textChanged.connect(self._update)
        self._update()
        self.command.setFocus()

    def _update(self, *_args) -> None:  # noqa: ANN002
        count = len(self.selected_targets())
        self.send_button.setEnabled(bool(self.command.text().strip()) and count > 0)
        self.send_button.setText(tr("&Invia") if count == 0 else tr("&Invia a {count}").format(count=count))

    def selected_targets(self) -> List[object]:
        return [target for check, target in self.checks if check.isChecked()]

    def command_text(self) -> str:
        return self.command.text()


def drop_target(store: model.SessionStore, target: str, where: str) -> Tuple[str, Optional[int]]:
    """Dove va una voce trascinata: (cartella di destinazione, posizione o None = in fondo)."""
    if not target:
        return store.home_path(), None
    node = store.get_node(target)
    if store.is_home_path(target) or (where == "into" and isinstance(node, model.FolderNode)):
        return target, None
    parent = store.parent_path(target)
    index = int(target.split(".")[-1])
    return parent, index + (1 if where != "before" else 0)
