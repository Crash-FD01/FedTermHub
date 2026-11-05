# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Riquadri del terminale e MultiExec: le regole comuni alle due interfacce, senza GTK e senza Qt.

Disposizioni, tasti, cosa replicare con il broadcast e come presentare le schede in MultiExec sono gli stessi
su Linux (terminal_panel.py, fedtermhub.py) e su Windows (qtui/terminal_tab.py).
"""
from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple, Union

try:
    from .i18n import tr
except ImportError:  # avvio come script (fedtermhub/fedtermhub.py) o dai test
    from i18n import tr  # type: ignore[no-redef]

# stack: un riquadro visibile alla volta; tall: il primo a sinistra, gli altri impilati a destra;
# fat: il primo in alto, gli altri affiancati sotto; grid: griglia il più quadrata possibile.
LAYOUTS = ("stack", "tall", "fat", "grid")

# Azione → tasti. Più tasti per la stessa azione: Invio del tastierino, e le parentesi quadre che con Maiusc
# diventano graffe su alcune tastiere.
PANE_KEYS: Dict[str, Tuple[str, ...]] = {
    "split_v": ("Ctrl+Shift+O",),
    "split_h": ("Ctrl+Shift+E",),
    "new_pane": ("Ctrl+Shift+Return", "Ctrl+Shift+Enter"),
    "close_pane": ("Ctrl+Shift+W",),
    "next_pane": ("Ctrl+Shift+]", "Ctrl+Shift+}"),
    "prev_pane": ("Ctrl+Shift+[", "Ctrl+Shift+{"),
    "cycle_layout": ("Ctrl+Shift+L",),
    "toggle_broadcast": ("Ctrl+Shift+P",),
}

# Una divisione esplicita sceglie la disposizione: Ctrl+Shift+O affianca, Ctrl+Shift+E mette sopra e sotto.
SPLIT_LAYOUTS = {"split_v": "tall", "split_h": "fat"}

Plan = Union[int, Tuple[str, List["Plan"]]]


def next_layout(current: str) -> str:
    index = LAYOUTS.index(current) if current in LAYOUTS else 0
    return LAYOUTS[(index + 1) % len(LAYOUTS)]


def layout_after_add(current: str, count: int) -> str:
    """La disposizione dopo aver aggiunto un riquadro: con «stack» non si vedrebbe, si passa a «tall»."""
    return "tall" if current == "stack" and count > 1 else current


def grid_shape(count: int) -> Tuple[int, int]:
    """Colonne e righe della griglia per `count` riquadri."""
    cols = int(math.ceil(math.sqrt(max(count, 1))))
    return cols, int(math.ceil(max(count, 1) / cols))


def layout_plan(layout: str, count: int, active: int = 0) -> Plan:
    """Come disporre `count` riquadri: l'indice di un riquadro, oppure ("h"|"v", parti) affiancate o impilate."""
    if layout == "stack" or count <= 1:
        return active if 0 <= active < count else 0
    rest = list(range(1, count))
    if layout == "tall":
        return ("h", [0, rest[0] if len(rest) == 1 else ("v", rest)])
    if layout == "fat":
        return ("v", [0, rest[0] if len(rest) == 1 else ("h", rest)])
    cols, _rows = grid_shape(count)
    rows: List[Plan] = []
    for start in range(0, count, cols):
        row = list(range(start, min(start + cols, count)))
        rows.append(row[0] if len(row) == 1 else ("h", row))
    return rows[0] if len(rows) == 1 else ("v", rows)


def broadcastable(data: str) -> bool:
    """Se l'input va replicato negli altri riquadri: testo, Invio, cancella, tab ed Esc.

    Il resto no: frecce e Ctrl+tasto restano nel riquadro attivo, come le risposte automatiche del terminale
    (posizione del cursore, mouse), che negli altri riquadri finirebbero scritte come testo.
    """
    if data in {"\r", "\x7f", "\t", "\x1b"}:
        return True
    return bool(data) and all(ch >= " " and ch != "\x7f" for ch in data)


def multiexec_target(protocol: str, split: bool = False) -> Tuple[str, bool]:
    """Come una scheda compare in MultiExec: (tipo, già spuntata). Spuntate sono le sessioni SSH.

    protocol: quello della sessione; "" o "local" per una shell locale, "tool" per uno strumento rapido.
    """
    if split:
        return tr("terminale diviso"), False
    if protocol in {"", "local"}:
        return tr("terminale locale"), False
    if protocol == "tool":
        return tr("strumento"), False
    return protocol.upper(), protocol.lower() == "ssh"


def multiexec_defaults(targets: Sequence[Tuple[object, str, str, bool]]) -> List[Tuple[object, str, str, bool]]:
    """Le schede (scheda, titolo, tipo, spuntata): se nessuna è spuntata, si spuntano tutte."""
    if any(default for *_rest, default in targets):
        return list(targets)
    return [(target, title, kind, True) for target, title, kind, _default in targets]


def multiexec_line(command: str, enter: str = "\n") -> str:
    """Il comando seguito da Invio: su Windows `enter` è "\\r", il tasto che PowerShell si aspetta da ConPTY."""
    return command + enter
