# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tema chiaro e scuro dell'interfaccia Qt, con i colori della versione Linux (fedtermhub/look.py)."""
from __future__ import annotations

from pathlib import Path
from typing import Dict

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .. import look

_current = "dark"


def _palette(dark: bool) -> Dict[str, str]:
    w = look.window_colors(dark)
    return {
        **w,
        # nomi usati dal resto dell'interfaccia Qt
        "window": w["root"], "panel": w["sidebar"], "base": w["entry"], "alt": w["hover"],
        "accentText": "#ffffff", "border": w["line"], "term": w["terminal"], "termText": w["terminal_text"],
        "selection": w["selected"], "danger": w["danger"],
    }


DARK: Dict[str, str] = _palette(True)
LIGHT: Dict[str, str] = _palette(False)


def colors() -> Dict[str, str]:
    return DARK if _current == "dark" else LIGHT


def is_dark() -> bool:
    return _current == "dark"


def stylesheet(c: Dict[str, str]) -> str:
    """Il foglio di stile Qt: stesse parti e stessi colori del foglio di stile GTK."""
    return f"""
    QMainWindow, QDialog {{ background: {c['root']}; }}
    QWidget {{ color: {c['text']}; }}

    QMenuBar#menubar {{ background: {c['bar']}; border-bottom: 1px solid {c['line']}; padding: 2px 7px; }}
    QMenuBar#menubar::item {{ padding: 5px 9px; border-radius: 7px; background: transparent; }}
    QMenuBar#menubar::item:selected {{ background: {c['menu_hover']}; }}
    QMenu {{ background: {c['menu']}; border: 1px solid {c['menu_line']}; border-radius: 10px; padding: 6px; }}
    QMenu::item {{ padding: 7px 24px 7px 10px; border-radius: 8px; background: transparent; }}
    QMenu::item:selected {{ background: {c['menu_hover']}; }}
    QMenu::item:disabled {{ color: {c['muted']}; }}
    QMenu::separator {{ height: 1px; background: {c['menu_line']}; margin: 5px 4px; }}
    QMenu::icon {{ padding-left: 6px; }}
    QLabel[role="menuheading"] {{ color: {c['muted']}; font-size: 10px; font-weight: 700; padding: 6px 10px 2px 10px;
        text-transform: uppercase; }}

    QToolBar#ribbon {{ background: {c['bar']}; border: 0; border-bottom: 1px solid {c['line']}; padding: 2px 5px; spacing: 2px; }}
    QToolBar#ribbon QToolButton {{ background: transparent; color: {c['caption']}; border: 1px solid transparent;
        border-radius: 6px; padding: 2px 5px; min-width: 46px; font-size: 10px; font-weight: 600; }}
    QToolBar#ribbon QToolButton:hover {{ background: {c['hover']}; border-color: {c['line']}; }}
    QToolBar#ribbon QToolButton:pressed, QToolBar#ribbon QToolButton:open {{ background: {c['selected']}; }}
    QToolBar#ribbon QToolButton::menu-indicator {{ image: none; width: 0; }}
    QToolBar#ribbon::separator {{ background: {c['line']}; width: 1px; margin: 6px 4px; }}

    QFrame#rail {{ background: {c['rail']}; border: 0; border-right: 1px solid {c['line']}; }}
    QFrame#rail QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 4px; }}
    QFrame#rail QToolButton:hover {{ background: {c['hover']}; border-color: {c['line']}; }}

    QWidget#sidebar {{ background: {c['sidebar']}; }}
    QFrame#quickBox {{ background: {c['sidebar']}; border: 0; border-bottom: 1px solid {c['line']}; }}
    QFrame#sideTitlebar {{ background: {c['sidebar']}; border: 0; }}
    QLabel#sideTitle {{ font-weight: 600; }}
    QFrame#sessionToolbar, QFrame#sideFooter {{ background: {c['sidebar']}; border: 0; }}
    QToolButton[role="mini"] {{ background: {c['button']}; border: 1px solid {c['button_line']}; border-radius: 8px;
        padding: 5px; min-width: 22px; }}
    QToolButton[role="mini"]:hover {{ background: {c['button_hover']}; }}
    QToolButton[role="flat"] {{ background: transparent; border: 0; border-radius: 6px; padding: 4px; }}
    QToolButton[role="flat"]:hover {{ background: {c['hover']}; }}
    QTreeView#sessionTree {{ background: transparent; border: 0; color: {c['tree_text']}; outline: 0; }}
    QTreeView#sessionTree::item {{ padding: 3px 2px; border-radius: 8px; }}
    QTreeView#sessionTree::item:hover {{ background: {c['hover']}; }}
    QTreeView#sessionTree::item:selected {{ background: {c['selected']}; color: {c['selected_text']}; }}

    QTabWidget::pane {{ border: 0; }}
    QTabBar {{ background: transparent; }}
    QTabBar::tab {{ background: transparent; border: 0; border-bottom: 2px solid transparent; padding: 7px 14px;
        margin: 0 1px; color: {c['tab_text']}; }}
    QTabBar::tab:selected {{ color: {c['text']}; border-bottom-color: {c['focus']}; }}
    QTabBar::tab:hover:!selected {{ background: {c['tab_hover']}; }}
    QTabWidget#sidebarPages QTabBar::tab {{ padding: 5px 10px; }}
    QToolButton[role="tabclose"] {{ background: transparent; border: 0; border-radius: 4px; padding: 0 3px;
        color: {c['tab_text']}; font-size: 11px; }}
    QToolButton[role="tabclose"]:hover {{ background: {c['tab_hover']}; color: {c['error']}; }}
    QWidget#workspace {{ background: {c['workspace']}; }}
    QTabWidget#workTabs > QTabBar {{ background: {c['root']}; }}
    QLabel#workspaceHint {{ background: {c['root']}; color: {c['muted']}; border-bottom: 1px solid {c['line']};
        padding: 4px 8px; }}

    QStatusBar {{ background: {c['bar']}; color: {c['status_text']}; border-top: 1px solid {c['line']}; }}
    QStatusBar QLabel {{ color: {c['status_text']}; padding: 0 4px; }}
    QStatusBar QLabel#versionLabel {{ color: {c['version']}; }}
    QStatusBar::item {{ border: 0; }}

    QLineEdit, QComboBox, QPlainTextEdit, QTextEdit {{ background: {c['entry']}; border: 1px solid {c['entry_line']};
        border-radius: 9px; padding: 4px 9px; selection-background-color: {c['focus']}; }}
    QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {c['focus']}; }}
    QPushButton {{ background: {c['button']}; border: 1px solid {c['button_line']}; border-radius: 9px; padding: 5px 12px; }}
    QPushButton:hover {{ background: {c['button_hover']}; }}
    QPushButton:disabled {{ color: {c['muted']}; }}
    QPushButton:default, QPushButton[role="accent"] {{ background: {c['accent']}; color: #ffffff; border-color: {c['accent']}; }}
    QPushButton:default:hover, QPushButton[role="accent"]:hover {{ background: {c['accent_hover']}; }}
    QPushButton[role="accent"] {{ padding: 2px 12px; }}
    QHeaderView::section {{ background: {c['bar']}; border: 0; border-bottom: 1px solid {c['line']};
        border-right: 1px solid {c['line']}; padding: 5px 8px; font-weight: 600; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
    QScrollBar::handle {{ background: {c['button_line']}; border-radius: 4px; min-height: 24px; min-width: 24px; margin: 2px; }}
    QScrollBar::handle:hover {{ background: {c['muted']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QToolTip {{ background: {c['menu']}; color: {c['text']}; border: 1px solid {c['menu_line']}; padding: 4px; }}

    QLabel[role="muted"] {{ color: {c['muted']}; }}
    QLabel[role="heading"] {{ font-size: 15px; font-weight: 600; }}
    QLabel[role="danger"] {{ color: {c['danger']}; }}
    QFrame[role="card"] {{ background: {c['sidebar']}; border: 1px solid {c['line']}; border-radius: 12px; }}
    """


def apply(app: QApplication, name: str) -> None:
    """Applica il tema «dark» o «light» a tutta l'applicazione (stile Fusion, uguale su ogni sistema)."""
    global _current
    _current = "light" if name == "light" else "dark"
    c = colors()
    app.setStyle("Fusion")
    pal = QPalette()
    role = QPalette.ColorRole
    pal.setColor(role.Window, QColor(c["root"]))
    pal.setColor(role.WindowText, QColor(c["text"]))
    pal.setColor(role.Base, QColor(c["sidebar"]))
    pal.setColor(role.AlternateBase, QColor(c["hover"]))
    pal.setColor(role.Text, QColor(c["text"]))
    pal.setColor(role.Button, QColor(c["button"]))
    pal.setColor(role.ButtonText, QColor(c["text"]))
    pal.setColor(role.ToolTipBase, QColor(c["menu"]))
    pal.setColor(role.ToolTipText, QColor(c["text"]))
    pal.setColor(role.Highlight, QColor(c["selected"]))
    pal.setColor(role.HighlightedText, QColor(c["selected_text"]))
    pal.setColor(role.PlaceholderText, QColor(c["muted"]))
    pal.setColor(role.Link, QColor(c["version"]))
    for group in (QPalette.ColorGroup.Disabled,):
        pal.setColor(group, role.Text, QColor(c["muted"]))
        pal.setColor(group, role.ButtonText, QColor(c["muted"]))
        pal.setColor(group, role.WindowText, QColor(c["muted"]))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet(c))


def terminal_theme() -> Dict[str, object]:
    """Colori per xterm.js: gli stessi del terminale della versione Linux, scuro anche nel tema chiaro."""
    names = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")
    theme: Dict[str, object] = {"dark": True, "background": look.TERMINAL_BACKGROUND,
                                "foreground": look.TERMINAL_FOREGROUND, "cursor": "#78c9ff",
                                "selectionBackground": "#2f78aa"}
    for index, color in enumerate(look.TERMINAL_PALETTE):
        name = names[index % 8]
        theme[name if index < 8 else "bright" + name.capitalize()] = color
    return theme


def _icon_from_svg(svg: str, sizes=(16, 20, 24, 32, 48)):  # noqa: ANN001, ANN202
    from PySide6.QtCore import QByteArray
    from PySide6.QtGui import QIcon, QPixmap

    import re

    box = re.search(r'viewBox="\s*[-\d.]+\s+[-\d.]+\s+([\d.]+)\s+([\d.]+)"', svg)
    vw, vh = (float(box.group(1)), float(box.group(2))) if box else (1.0, 1.0)
    icon = QIcon()
    for size in sizes:
        # Disegnata già alla misura giusta (non ingrandita da 24 px): nitida anche a 150-200 %.
        w, h = size * vw / max(vw, vh), size * vh / max(vw, vh)
        sized = re.sub(r'(<svg\b[^>]*?)\swidth="[^"]*"\s+height="[^"]*"', rf'\1 width="{w:g}" height="{h:g}"', svg, count=1)
        pixmap = QPixmap()
        if pixmap.loadFromData(QByteArray(sized.encode()), "SVG"):
            icon.addPixmap(pixmap)
    return icon


def themed_icon(path):  # noqa: ANN001, ANN201
    """Un'icona SVG di FedTermHub nel colore che ha nella versione Linux (look.py), per il tema attuale."""
    from PySide6.QtGui import QIcon

    try:
        svg = Path(path).read_text(encoding="utf-8")
    except OSError:
        return QIcon()
    if "currentColor" not in svg:
        return QIcon(str(path))
    return _icon_from_svg(look.colored_svg(svg, Path(path).stem, is_dark()))


def folder_icon(color: str):  # noqa: ANN201
    from .. import model

    return _icon_from_svg(look.folder_svg(model.normalize_folder_color(color)), sizes=(16, 20, 24))


def home_icon():  # noqa: ANN201
    return _icon_from_svg(look.HOME_SESSION_SVG, sizes=(16, 20, 24))


def dot_icon(color: str, size: int = 10):  # noqa: ANN201
    """Il pallino di stato delle schede (verde collegato, giallo in corso, grigio chiuso, rosso errore)."""
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 10 10">'
           f'<circle cx="5" cy="5" r="3.6" fill="{color}"/></svg>')
    return _icon_from_svg(svg, sizes=(size, size * 2))
