# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Password Manager nell'interfaccia Qt: sblocco, creazione, credenziali.

Stesso archivio cifrato della versione GTK (model.PasswordVault): un backup esportato su Linux si
importa qui e viceversa.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import model
from ..i18n import tr, trn

MIN_MASTER = 8


def _muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty("role", "muted")
    return label


class MasterPasswordDialog(QDialog):
    """Chiede la master password: per sbloccare, per creare l'archivio o per cambiarla.

    Con «check» la password viene provata subito: se è sbagliata la finestra resta aperta con l'errore.
    """

    TEXTS = {
        "unlock": (tr("Sblocca le password"), tr("Inserisci la master password per usare le credenziali salvate."), tr("Sblocca")),
        "create": (tr("Proteggi le tue password"),
                   tr("Scegli una master password: cifra tutte le credenziali salvate in FedTermHub. Non viene salvata "
                      "da nessuna parte e non si può recuperare: se la dimentichi, le credenziali sono perse."),
                   tr("Crea archivio")),
        "change": (tr("Cambia master password"),
                   tr("Le credenziali vengono cifrate di nuovo con la nuova password."), tr("Cambia password")),
        "export": (tr("Password del backup"),
                   tr("Il file di backup è cifrato con questa password: serve per importarlo di nuovo."), tr("Esporta")),
        "import": (tr("Password del backup"), tr("Inserisci la password scelta quando il backup è stato esportato."), tr("Importa")),
    }

    def __init__(self, parent: Optional[QWidget], mode: str = "unlock",
                 check: Optional[Callable[[str], Optional[str]]] = None) -> None:
        super().__init__(parent)
        self.mode = mode
        self.check = check
        heading, info, ok_label = self.TEXTS.get(mode, self.TEXTS["unlock"])
        self.setWindowTitle(heading)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        title = QLabel(heading)
        title.setProperty("role", "heading")
        layout.addWidget(title)
        layout.addWidget(_muted(info))
        form = QFormLayout()
        self.current = QLineEdit()
        self.current.setEchoMode(QLineEdit.EchoMode.Password)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        new = mode in {"create", "change", "export"}
        if mode == "change":
            form.addRow(tr("Password attuale"), self.current)
        form.addRow(tr("Nuova password") if mode == "change" else tr("Password"), self.password)
        if new:
            form.addRow(tr("Conferma"), self.confirm)
        layout.addLayout(form)
        self.show_box = QCheckBox(tr("Mostra la password"))
        self.show_box.toggled.connect(self._toggle_echo)
        layout.addWidget(self.show_box)
        self.error = QLabel("")
        self.error.setProperty("role", "danger")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = buttons.addButton(ok_label, QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok.setDefault(True)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        (self.current if mode == "change" else self.password).setFocus()

    def _toggle_echo(self, on: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
        for field in (self.current, self.password, self.confirm):
            field.setEchoMode(mode)

    def show_error(self, message: str) -> None:
        self.error.setText(message)
        self.error.show()

    def value(self) -> str:
        return self.password.text()

    def current_value(self) -> str:
        return self.current.text()

    def _accept(self) -> None:
        if self.mode == "change" and not self.current.text():
            self.show_error(tr("Inserisci la master password attuale."))
            return
        if not self.password.text():
            self.show_error(tr("Inserisci la password."))
            return
        if self.mode in {"create", "change"} and len(self.password.text()) < MIN_MASTER:
            self.show_error(tr("Usa almeno 8 caratteri: è l'unica protezione delle credenziali salvate."))
            return
        if self.mode in {"create", "change", "export"} and self.password.text() != self.confirm.text():
            self.show_error(tr("Le due password non coincidono."))
            return
        if self.check is not None:
            problem = self.check(self.password.text())
            if problem:
                self.show_error(problem)
                self.password.selectAll()
                self.password.setFocus()
                return
        self.accept()


class CredentialDialog(QDialog):
    def __init__(self, parent: Optional[QWidget], credential: Optional[model.Credential], groups: List[str]) -> None:
        super().__init__(parent)
        self.is_new = credential is None
        self.credential = credential or model.Credential()
        self.setWindowTitle(tr("Nuova credenziale") if self.is_new else tr("Modifica credenziale"))
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(self.credential.name if not self.is_new else "")
        self.name.setPlaceholderText(tr("Es. Administrator dominio Rossi"))
        self.group = QComboBox()
        self.group.setEditable(True)
        self.group.addItems([""] + groups)
        self.group.setCurrentText(self.credential.group)
        self.group.lineEdit().setPlaceholderText(tr("Es. il nome del cliente (facoltativo)"))
        self.username = QLineEdit(self.credential.username)
        self.domain = QLineEdit(self.credential.domain)
        self.domain.setPlaceholderText(tr("Solo per account Windows / Active Directory"))
        self.password = QLineEdit(self.credential.password)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        show = QPushButton(tr("Mostra"))
        show.setCheckable(True)
        show.toggled.connect(lambda on: self.password.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        generate = QPushButton(tr("Genera"))
        generate.setToolTip(tr("Crea una password casuale di 20 caratteri"))
        generate.clicked.connect(self._generate)
        pw_row = QHBoxLayout()
        pw_row.addWidget(self.password, 1)
        pw_row.addWidget(show)
        pw_row.addWidget(generate)
        self.strength = _muted("")
        self.password.textChanged.connect(self._update_strength)
        self.notes = QPlainTextEdit(self.credential.notes)
        self.notes.setMaximumHeight(90)
        form.addRow(tr("Nome"), self.name)
        form.addRow(tr("Gruppo"), self.group)
        form.addRow(tr("Utente"), self.username)
        form.addRow(tr("Dominio"), self.domain)
        form.addRow(tr("Password"), pw_row)
        form.addRow("", self.strength)
        form.addRow(tr("Note"), self.notes)
        layout.addLayout(form)
        layout.addWidget(_muted(tr("Le credenziali dello stesso gruppo compaiono insieme nell'elenco.")))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_strength()
        self.name.setFocus()

    def _generate(self) -> None:
        self.password.setText(model.generate_password())
        self.password.setEchoMode(QLineEdit.EchoMode.Normal)

    def _update_strength(self) -> None:
        if not self.password.text():
            self.strength.setText("")
            return
        _score, label = model.password_strength(self.password.text())
        self.strength.setText(tr("Robustezza: {label}").format(label=label))

    def _accept(self) -> None:
        if not self.name.text().strip():
            self.name.setFocus()
            return
        self.accept()

    def result_credential(self) -> model.Credential:
        cred = self.credential
        cred.name = self.name.text().strip() or tr("Senza nome")
        cred.group = self.group.currentText().strip()
        cred.username = self.username.text().strip()
        cred.domain = self.domain.text().strip()
        cred.password = self.password.text()
        cred.notes = self.notes.toPlainText().strip()
        return cred


class VaultManagerDialog(QDialog):
    """Le credenziali raggruppate per cliente a sinistra, i dettagli con i pulsanti di copia a destra."""

    def __init__(self, parent: Optional[QWidget], vault: model.PasswordVault, settings: model.AppSettings,
                 usage: Callable[[str], List[str]], on_changed: Callable[[], None]) -> None:
        super().__init__(parent)
        self.vault = vault
        self.settings = settings
        self.usage = usage
        self.on_changed = on_changed
        self.setWindowTitle(tr("Password"))
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        head = QHBoxLayout()
        title = QLabel(tr("Password Manager"))
        title.setProperty("role", "heading")
        head.addWidget(title)
        head.addStretch(1)
        self.lock_button = QPushButton(tr("Blocca"))
        self.lock_button.setToolTip(tr("Chiude la finestra e chiede la master password la prossima volta"))
        self.lock_button.clicked.connect(self._lock)
        head.addWidget(self.lock_button)
        layout.addLayout(head)

        splitter = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Cerca nome, utente, gruppo… (Ctrl+F)"))
        self.search.textChanged.connect(self.refresh)
        left_layout.addWidget(self.search)
        new_button = QPushButton(tr("Nuova credenziale"))
        new_button.clicked.connect(self._new)
        left_layout.addWidget(new_button)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.currentItemChanged.connect(lambda *_: self._show_selected())
        self.tree.itemDoubleClicked.connect(lambda *_: self._edit())
        left_layout.addWidget(self.tree, 1)
        self.count = _muted("")
        left_layout.addWidget(self.count)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.detail_title = QLabel(tr("Scegli una credenziale"))
        self.detail_title.setProperty("role", "heading")
        self.detail_group = _muted(tr("Selezionala nell'elenco per vedere, copiare o modificare i suoi dati."))
        right_layout.addWidget(self.detail_title)
        right_layout.addWidget(self.detail_group)
        self.detail_form = QFormLayout()
        self.user_label = QLabel()
        self.domain_label = QLabel()
        self.password_label = QLabel()
        self.notes_label = QLabel()
        self.notes_label.setWordWrap(True)
        self.used_label = QLabel()
        self.used_label.setWordWrap(True)
        for label in (self.user_label, self.domain_label, self.password_label):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.copy_user = QPushButton(tr("Copia"))
        self.copy_user.clicked.connect(lambda: self._copy("user"))
        self.copy_domain = QPushButton(tr("Copia"))
        self.copy_domain.clicked.connect(lambda: self._copy("domain"))
        self.copy_password = QPushButton(tr("Copia"))
        self.copy_password.clicked.connect(lambda: self._copy("password"))
        self.reveal = QPushButton(tr("Mostra"))
        self.reveal.setCheckable(True)
        self.reveal.toggled.connect(lambda *_: self._show_selected())
        self.detail_form.addRow(tr("Utente"), self._row(self.user_label, self.copy_user))
        self.detail_form.addRow(tr("Dominio"), self._row(self.domain_label, self.copy_domain))
        self.detail_form.addRow(tr("Password"), self._row(self.password_label, self.reveal, self.copy_password))
        self.detail_form.addRow(tr("Note"), self.notes_label)
        self.detail_form.addRow(tr("Usata da"), self.used_label)
        self.details = QWidget()
        self.details.setLayout(self.detail_form)
        right_layout.addWidget(self.details)
        right_layout.addStretch(1)
        actions = QHBoxLayout()
        self.edit_button = QPushButton(tr("Modifica"))
        self.edit_button.clicked.connect(self._edit)
        self.delete_button = QPushButton(tr("Elimina"))
        self.delete_button.clicked.connect(self._delete)
        actions.addWidget(self.edit_button)
        actions.addStretch(1)
        actions.addWidget(self.delete_button)
        right_layout.addLayout(actions)
        splitter.addWidget(right)
        splitter.setSizes([340, 560])
        layout.addWidget(splitter, 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.search.setFocus)
        QShortcut(QKeySequence("Ctrl+C"), self, activated=lambda: self._copy("password"))
        QShortcut(QKeySequence("Ctrl+B"), self, activated=lambda: self._copy("user"))
        self.refresh()

    @staticmethod
    def _row(*widgets: QWidget) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(widgets[0], 1)
        for widget in widgets[1:]:
            row.addWidget(widget)
        return box

    def selected(self) -> Optional[model.Credential]:
        item = self.tree.currentItem()
        if item is None:
            return None
        cid = item.data(0, Qt.ItemDataRole.UserRole)
        return self.vault.get(cid) if cid else None

    def refresh(self, select_id: str = "") -> None:
        query = self.search.text().strip().casefold()
        current = select_id or (self.selected().id if self.selected() else "")
        self.tree.clear()
        group_item: Optional[QTreeWidgetItem] = None
        last_group = None
        shown = 0
        to_select = None
        for cred in self.vault.sorted_credentials():
            haystack = " ".join((cred.name, cred.username, cred.domain, cred.group, cred.notes)).casefold()
            if query and query not in haystack:
                continue
            if cred.group != last_group:
                last_group = cred.group
                group_item = QTreeWidgetItem([(cred.group or tr("Senza gruppo")).upper()])
                group_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                font = group_item.font(0)
                font.setBold(True)
                font.setPointSizeF(font.pointSizeF() * 0.85)
                group_item.setFont(0, font)
                self.tree.addTopLevelItem(group_item)
            account = f"{cred.domain}\\{cred.username}" if cred.domain and cred.username else cred.username
            item = QTreeWidgetItem([f"{cred.name}\n{account}" if account else cred.name])
            item.setData(0, Qt.ItemDataRole.UserRole, cred.id)
            self.tree.addTopLevelItem(item)
            shown += 1
            if cred.id == current:
                to_select = item
        total = len(self.vault.credentials)
        if query:
            self.count.setText(tr("{visible} di {total} credenziali").format(visible=shown, total=total))
        else:
            self.count.setText(trn("1 credenziale", "{total} credenziali", total).format(total=total))
        if to_select is not None:
            self.tree.setCurrentItem(to_select)
        self._show_selected()

    def _show_selected(self) -> None:
        cred = self.selected()
        has = cred is not None
        for widget in (self.details, self.edit_button, self.delete_button):
            widget.setVisible(has)
        if not has:
            self.detail_title.setText(tr("Scegli una credenziale") if self.vault.credentials else tr("Nessuna password salvata"))
            self.detail_group.setText(
                tr("Selezionala nell'elenco per vedere, copiare o modificare i suoi dati.") if self.vault.credentials
                else tr("Salva qui utenti e password dei server: restano cifrati con la master password. Poi, nella "
                        "finestra di una sessione, scegli la credenziale da usare.")
            )
            return
        self.detail_title.setText(cred.name)
        self.detail_group.setText(cred.group or tr("Senza gruppo"))
        self.user_label.setText(cred.username or "—")
        self.domain_label.setText(cred.domain or "—")
        self.password_label.setText(cred.password if self.reveal.isChecked() else ("•" * 12 if cred.password else "—"))
        self.notes_label.setText(cred.notes or "—")
        users = self.usage(cred.id)
        self.used_label.setText(", ".join(users) if users else tr("Nessuna sessione la usa."))

    def _copy(self, what: str) -> None:
        cred = self.selected()
        if cred is None:
            return
        value = {"user": cred.username, "domain": cred.domain, "password": cred.password}[what]
        if not value:
            return
        clipboard = QGuiApplication.clipboard()
        clipboard.setText(value)
        seconds = self.settings.clear_clipboard_seconds
        if what == "password" and seconds > 0:
            # cancella gli appunti solo se contengono ancora la password copiata
            QTimer.singleShot(seconds * 1000, lambda v=value: clipboard.text() == v and clipboard.clear())

    def _new(self) -> None:
        dialog = CredentialDialog(self, None, self.vault.groups())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            cred = dialog.result_credential()
            self.vault.credentials.append(cred)
            self.vault.save()
            self.on_changed()
            self.refresh(cred.id)

    def _edit(self) -> None:
        cred = self.selected()
        if cred is None:
            return
        dialog = CredentialDialog(self, cred, self.vault.groups())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.result_credential()
            self.vault.save()
            self.on_changed()
            self.refresh(cred.id)

    def _delete(self) -> None:
        cred = self.selected()
        if cred is None:
            return
        users = self.usage(cred.id)
        detail = (trn("È usata da {n_usage} sessione ({names}): resterà salvata ma ti chiederà la password a ogni connessione.",
                      "È usata da {n_usage} sessioni ({names}): resteranno salvate ma ti chiederanno la password a ogni connessione.",
                      len(users)).format(n_usage=len(users), names=", ".join(users[:5]))
                  if users else tr("Nessuna sessione la usa. Eliminarla non si può annullare."))
        answer = QMessageBox.question(self, tr("Eliminare «{name}»?").format(name=cred.name), detail)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.vault.credentials = [c for c in self.vault.credentials if c.id != cred.id]
        self.vault.save()
        self.on_changed()
        self.refresh()

    def _lock(self) -> None:
        self.vault.lock()
        self.on_changed()
        self.reject()
