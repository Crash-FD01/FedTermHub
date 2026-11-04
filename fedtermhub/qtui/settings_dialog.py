# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Impostazioni dell'interfaccia Qt: solo quelle che questa versione usa davvero.

Le altre voci di model.AppSettings (layout GTK, file remoti...) restano nel file come sono.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import model
from ..i18n import tr
from . import updates


def _combo(options, current: str) -> QComboBox:  # noqa: ANN001
    box = QComboBox()
    for value, label in options:
        box.addItem(label, value)
    box.setCurrentIndex(max(0, box.findData(current)))
    return box


def _spin(value: int, minimum: int, maximum: int, suffix: str = "", special: str = "") -> QSpinBox:
    box = QSpinBox()
    box.setRange(minimum, maximum)
    box.setValue(value)
    if suffix:
        box.setSuffix(suffix)
    if special:
        box.setSpecialValueText(special)
    return box


class SettingsDialog(QDialog):
    def __init__(self, parent: Optional[QWidget], settings: model.AppSettings) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle(tr("Impostazioni"))
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)

        look = QGroupBox(tr("Aspetto"))
        form = QFormLayout(look)
        self.language = _combo([("auto", tr("Automatica (lingua del sistema)")), ("it", "Italiano"), ("en", "English")],
                               settings.language)
        self.theme = _combo([("dark", tr("Scuro")), ("light", tr("Chiaro"))], settings.theme)
        form.addRow(tr("Lingua"), self.language)
        form.addRow("", QLabel(tr("La lingua cambia al prossimo avvio.")))
        form.addRow(tr("Tema"), self.theme)
        layout.addWidget(look)

        term = QGroupBox(tr("Terminale"))
        form = QFormLayout(term)
        self.font_size = _spin(settings.terminal_font_size, 7, 28, " pt")
        self.scrollback = _spin(settings.terminal_scrollback_lines, 1000, 200000)
        self.scrollback.setSingleStep(1000)
        self.blink = QCheckBox(tr("Cursore lampeggiante"))
        self.blink.setChecked(settings.terminal_cursor_blink)
        form.addRow(tr("Dimensione del carattere"), self.font_size)
        form.addRow(tr("Righe nello storico"), self.scrollback)
        form.addRow("", self.blink)
        layout.addWidget(term)

        conn = QGroupBox(tr("Connessioni SSH"))
        form = QFormLayout(conn)
        self.timeout = _spin(settings.connection_timeout_seconds, 3, 120, " s")
        self.keepalive = _spin(settings.ssh_keepalive_interval, 0, 600, " s", tr("disattivato"))
        self.strict = _combo([
            ("ask", tr("Chiedi per gli host nuovi")),
            ("accept-new", tr("Accetta gli host nuovi, blocca le chiavi cambiate")),
            ("yes", tr("Solo host già conosciuti")),
            ("no", tr("Non controllare (sconsigliato)")),
        ], settings.ssh_strict_host_key)
        self.compression = QCheckBox(tr("Compressione (utile su linee lente)"))
        self.compression.setChecked(settings.ssh_compression)
        form.addRow(tr("Timeout di connessione"), self.timeout)
        form.addRow(tr("Keepalive"), self.keepalive)
        form.addRow(tr("Chiave dell'host"), self.strict)
        form.addRow("", self.compression)
        layout.addWidget(conn)

        safety = QGroupBox(tr("Sicurezza"))
        form = QFormLayout(safety)
        self.auto_lock = _spin(settings.vault_auto_lock_minutes, 0, 480, " min", tr("mai"))
        self.clipboard = _spin(settings.clear_clipboard_seconds, 0, 600, " s", tr("mai"))
        self.confirm_close = QCheckBox(tr("Chiedi conferma prima di chiudere con sessioni aperte"))
        self.confirm_close.setChecked(settings.confirm_close_active_sessions)
        form.addRow(tr("Blocca le password dopo"), self.auto_lock)
        form.addRow(tr("Svuota gli appunti dopo"), self.clipboard)
        form.addRow("", self.confirm_close)
        layout.addWidget(safety)

        self.check_updates = QCheckBox(tr("Avvisami quando esce una nuova versione"))
        self.check_updates.setChecked(settings.check_updates)
        self.check_updates.setVisible(updates.enabled())
        layout.addWidget(self.check_updates)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply(self) -> None:
        """Copia i valori scelti nelle impostazioni (il salvataggio lo fa chi chiama)."""
        s = self.settings
        s.language = self.language.currentData()
        s.theme = self.theme.currentData()
        s.terminal_font_size = self.font_size.value()
        s.terminal_scrollback_lines = self.scrollback.value()
        s.terminal_cursor_blink = self.blink.isChecked()
        s.connection_timeout_seconds = self.timeout.value()
        s.ssh_keepalive_interval = self.keepalive.value()
        s.ssh_strict_host_key = self.strict.currentData()
        s.ssh_compression = self.compression.isChecked()
        s.vault_auto_lock_minutes = self.auto_lock.value()
        s.clear_clipboard_seconds = self.clipboard.value()
        s.confirm_close_active_sessions = self.confirm_close.isChecked()
        s.check_updates = self.check_updates.isChecked()
