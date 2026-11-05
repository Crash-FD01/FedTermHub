# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tema chiaro e scuro dell'interfaccia Qt, con gli stessi colori della versione GTK."""
from __future__ import annotations

from typing import Dict

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

DARK: Dict[str, str] = {
    "window": "#15181d", "panel": "#1b1f26", "base": "#0f1115", "alt": "#20252d", "text": "#e6e9ef",
    "muted": "#9aa5b2", "accent": "#4aa3df", "accentText": "#ffffff", "border": "#2c323c",
    "term": "#0f1115", "termText": "#e6e9ef", "selection": "#3a5a8c", "danger": "#ef6b6b", "ok": "#4cc38a",
}
LIGHT: Dict[str, str] = {
    "window": "#f5f7fa", "panel": "#ffffff", "base": "#ffffff", "alt": "#eef1f5", "text": "#1d232b",
    "muted": "#5d6773", "accent": "#1f6fb2", "accentText": "#ffffff", "border": "#d5dbe3",
    "term": "#ffffff", "termText": "#1d1d1f", "selection": "#bfd5f5", "danger": "#c4352b", "ok": "#1f7a36",
}

_current = "dark"


def colors() -> Dict[str, str]:
    return DARK if _current == "dark" else LIGHT


def is_dark() -> bool:
    return _current == "dark"


def apply(app: QApplication, name: str) -> None:
    """Applica il tema «dark» o «light» a tutta l'applicazione (stile Fusion, uguale su ogni sistema)."""
    global _current
    _current = "light" if name == "light" else "dark"
    c = colors()
    app.setStyle("Fusion")
    pal = QPalette()
    role = QPalette.ColorRole
    pal.setColor(role.Window, QColor(c["window"]))
    pal.setColor(role.WindowText, QColor(c["text"]))
    pal.setColor(role.Base, QColor(c["base"]))
    pal.setColor(role.AlternateBase, QColor(c["alt"]))
    pal.setColor(role.Text, QColor(c["text"]))
    pal.setColor(role.Button, QColor(c["panel"]))
    pal.setColor(role.ButtonText, QColor(c["text"]))
    pal.setColor(role.ToolTipBase, QColor(c["panel"]))
    pal.setColor(role.ToolTipText, QColor(c["text"]))
    pal.setColor(role.Highlight, QColor(c["accent"]))
    pal.setColor(role.HighlightedText, QColor(c["accentText"]))
    pal.setColor(role.PlaceholderText, QColor(c["muted"]))
    pal.setColor(role.Link, QColor(c["accent"]))
    for group in (QPalette.ColorGroup.Disabled,):
        pal.setColor(group, role.Text, QColor(c["muted"]))
        pal.setColor(group, role.ButtonText, QColor(c["muted"]))
        pal.setColor(group, role.WindowText, QColor(c["muted"]))
    app.setPalette(pal)
    app.setStyleSheet(f"""
        QToolBar {{ background: {c['panel']}; border: 0; border-bottom: 1px solid {c['border']}; spacing: 4px; padding: 3px; }}
        QToolButton {{ padding: 4px 8px; border-radius: 6px; }}
        QToolButton:hover {{ background: {c['alt']}; }}
        QTreeView {{ border: 0; background: {c['panel']}; }}
        QTreeView::item {{ padding: 3px 2px; }}
        QLineEdit, QComboBox, QPlainTextEdit {{ padding: 4px 6px; border: 1px solid {c['border']};
            border-radius: 6px; background: {c['base']}; }}
        QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {c['accent']}; }}
        QPushButton {{ padding: 5px 14px; border: 1px solid {c['border']}; border-radius: 6px; background: {c['panel']}; }}
        QPushButton:hover {{ background: {c['alt']}; }}
        QPushButton[role="accent"] {{ background: {c['accent']}; color: {c['accentText']}; border-color: {c['accent']}; padding: 2px 12px; }}
        QPushButton:default {{ background: {c['accent']}; color: {c['accentText']}; border-color: {c['accent']}; }}
        QTabWidget::pane {{ border: 0; }}
        QTabBar::tab {{ padding: 6px 12px; border: 0; border-bottom: 2px solid transparent; }}
        QTabBar::tab:selected {{ border-bottom-color: {c['accent']}; }}
        QStatusBar {{ background: {c['panel']}; color: {c['muted']}; border-top: 1px solid {c['border']}; }}
        QLabel[role="muted"] {{ color: {c['muted']}; }}
        QLabel[role="heading"] {{ font-size: 15px; font-weight: 600; }}
        QLabel[role="danger"] {{ color: {c['danger']}; }}
        QFrame[role="card"] {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 8px; }}
    """)


def terminal_theme() -> Dict[str, object]:
    """Colori per xterm.js: sfondo, testo, selezione e, nel tema chiaro, colori ANSI leggibili su bianco."""
    c = colors()
    theme: Dict[str, object] = {"dark": is_dark(), "background": c["term"], "foreground": c["termText"],
                                "cursor": c["termText"], "selectionBackground": c["selection"]}
    if not is_dark():
        theme.update(black="#1D1D1F", red="#C4352B", green="#1F7A36", yellow="#8A6100", blue="#0A5BC4",
                     magenta="#9A2FB0", cyan="#0B7A86", white="#6E6E73", brightBlack="#55555A",
                     brightRed="#D93F3F", brightGreen="#248A3D", brightYellow="#9C6F00", brightBlue="#0A66D8",
                     brightMagenta="#A83BC0", brightCyan="#0E8A96", brightWhite="#3A3A3C")
    return theme


def themed_icon(path):  # noqa: ANN001, ANN201
    """Un'icona SVG con il tratto «currentColor» disegnato nel colore del testo del tema (chiaro o scuro)."""
    from PySide6.QtCore import QByteArray
    from PySide6.QtGui import QIcon, QPixmap

    try:
        svg = open(path, encoding="utf-8").read()
    except OSError:
        return QIcon()
    if "currentColor" not in svg:
        return QIcon(str(path))
    data = QByteArray(svg.replace("currentColor", colors()["text"]).encode())
    icon = QIcon()
    for size in (16, 24, 32, 48):
        pixmap = QPixmap(size, size)
        pixmap.loadFromData(data, "SVG")
        icon.addPixmap(pixmap.scaled(size, size))
    return icon

