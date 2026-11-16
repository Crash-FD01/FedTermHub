# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Controllo del layout di una finestra aperta: quello che un utente vedrebbe tagliato o fuori posto.

Non giudica l'estetica: trova testi tagliati, campi schiacciati, pezzi fuori dal loro contenitore, intestazioni
di tabella troppo strette e finestre più grandi dello schermo. Lo usano il self-test e tools/audit_qt.py.
"""
from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractScrollArea,
    QAbstractSpinBox,
    QComboBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QTabBar,
    QToolButton,
    QWidget,
)

TOLERANCE = 2


def _name(widget: QWidget) -> str:
    text = ""
    for getter in ("text", "title", "placeholderText", "currentText"):
        value = getattr(widget, getter, None)
        if callable(value):
            try:
                text = str(value())
            except Exception:  # noqa: BLE001
                text = ""
            if text:
                break
    label = type(widget).__name__
    if widget.objectName():
        label += f"#{widget.objectName()}"
    return f"{label} «{text[:40]}»" if text else label


def _in_scroll_area(widget: QWidget, top: QWidget) -> bool:
    """Dentro un'area che scorre un widget può stare fuori dal riquadro visibile: non è un difetto."""
    parent = widget.parentWidget()
    while parent is not None and parent is not top:
        if isinstance(parent, QAbstractScrollArea):
            return True
        parent = parent.parentWidget()
    return False


def problems(top: QWidget) -> List[str]:
    """I difetti di layout della finestra `top`, già mostrata. Lista vuota: niente da segnalare."""
    found: List[str] = []
    screen = top.screen().availableGeometry() if top.isWindow() and top.screen() else None
    if screen is not None:
        if top.width() > screen.width() or top.height() > screen.height():
            found.append(f"finestra {top.width()}x{top.height()} più grande dello schermo {screen.width()}x{screen.height()}")
        minimum = top.minimumSize().expandedTo(top.minimumSizeHint())
        if minimum.width() > screen.width() or minimum.height() > screen.height():
            found.append(f"misura minima {minimum.width()}x{minimum.height()} più grande dello schermo "
                         f"{screen.width()}x{screen.height()}")
    for widget in top.findChildren(QWidget):
        if not widget.isVisibleTo(top) or widget.width() <= 0 or widget.height() <= 0:
            continue
        scrolled = _in_scroll_area(widget, top)
        if isinstance(widget, QHeaderView):
            found += _header_problems(widget)
            continue
        if isinstance(widget, QTabBar):
            if any(isinstance(c, QToolButton) and c.isVisible() and c.arrowType() != Qt.ArrowType.NoArrow
                   for c in widget.children()):
                found.append(f"{_name(widget)}: le schede non entrano tutte (compaiono le frecce)")
            continue
        if isinstance(widget.parentWidget(), (QTabBar, QAbstractSpinBox, QComboBox)):
            continue
        if isinstance(widget, QAbstractButton) and widget.text():
            hint = widget.sizeHint()
            if widget.width() < hint.width() - TOLERANCE or widget.height() < hint.height() - TOLERANCE:
                found.append(f"{_name(widget)}: testo tagliato ({widget.width()}x{widget.height()}, servono "
                             f"{hint.width()}x{hint.height()})")
        elif isinstance(widget, QLabel) and widget.text() and widget.pixmap().isNull():
            if widget.wordWrap():
                need = widget.heightForWidth(widget.width())
                if need > widget.height() + TOLERANCE:
                    found.append(f"{_name(widget)}: testo tagliato in altezza ({widget.height()}, servono {need})")
            elif widget.sizePolicy().horizontalPolicy() != QSizePolicy.Policy.Ignored:
                hint = widget.sizeHint()
                if widget.width() < hint.width() - TOLERANCE or widget.height() < hint.height() - TOLERANCE:
                    found.append(f"{_name(widget)}: testo tagliato ({widget.width()}x{widget.height()}, servono "
                                 f"{hint.width()}x{hint.height()})")
        elif isinstance(widget, (QLineEdit, QComboBox, QAbstractSpinBox)):
            hint = widget.minimumSizeHint()
            if widget.height() < hint.height() - TOLERANCE:
                found.append(f"{_name(widget)}: campo schiacciato (alto {widget.height()}, servono {hint.height()})")
            if isinstance(widget, (QComboBox, QAbstractSpinBox)) and widget.width() < hint.width() - TOLERANCE:
                found.append(f"{_name(widget)}: campo stretto (largo {widget.width()}, servono {hint.width()})")
            if isinstance(widget, QAbstractSpinBox) and widget.findChild(QLineEdit).textMargins().left() < 6:
                found.append(f"{_name(widget)}: testo attaccato al bordo (creare il campo con theme.spin_box)")
        parent = widget.parentWidget()
        if parent is not None and not scrolled and not widget.isWindow() and parent.layout() is not None:
            if not parent.rect().adjusted(-TOLERANCE, -TOLERANCE, TOLERANCE, TOLERANCE).contains(widget.geometry()):
                g = widget.geometry()
                found.append(f"{_name(widget)}: esce dal contenitore {_name(parent)} "
                             f"({g.x()},{g.y()} {g.width()}x{g.height()} in {parent.width()}x{parent.height()})")
    return sorted(set(found))


def _header_problems(header: QHeaderView) -> List[str]:
    found: List[str] = []
    model = header.model()
    if model is None or header.orientation() != Qt.Orientation.Horizontal:
        return found
    metrics = header.fontMetrics()
    for section in range(header.count()):
        if header.isSectionHidden(section):
            continue
        text = str(model.headerData(section, Qt.Orientation.Horizontal) or "")
        need = metrics.horizontalAdvance(text) + 14
        if text and header.sectionSize(section) < need:
            found.append(f"colonna «{text}»: intestazione tagliata (larga {header.sectionSize(section)}, servono {need})")
    return found
