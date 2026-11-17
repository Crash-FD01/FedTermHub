# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Riquadri del terminale e MultiExec: le regole comuni alle due interfacce, senza GTK e senza Qt.

Disposizioni, tasti, cosa replicare con il broadcast e come presentare le schede in MultiExec sono gli stessi
su Linux (terminal_panel.py, fedtermhub.py) e su Windows (qtui/terminal_tab.py).
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Sequence, Tuple, Union

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


def is_terminal_reply(data: str) -> bool:
    """Se l'input è una risposta automatica del terminale (posizione del cursore, tipo di terminale, fuoco, mouse).

    Arrivano dallo stesso canale dei tasti, ma non le ha scritte l'utente: non devono cambiare il riquadro attivo.
    """
    if data.startswith(("\x1b]", "\x1bP", "\x1b_")):
        return True
    return data.startswith("\x1b[") and (data[2:3] in {"<", "M", "?", ">"} or data[-1:] in {"R", "c", "n", "t", "y", "I", "O"})


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
# La richiesta di password del client OpenSSH: «utente@host's password:» oppure «(utente@host) Password:».
# Le altre richieste (sudo, su, la passphrase di una chiave) hanno un'altra forma e non interessano.
_SSH_PROMPT = re.compile(r"(?:^|\n)\(?(?P<user>[^\s@()']+)@(?P<host>[^\s'()]+)(?:'s password|\) Password):[ \t]*$")
_LOGIN_FAILED = ("Permission denied", "Too many authentication failures", "Authentication failed", "Connection closed",
                 "Connection reset", "Connection timed out")


class PasswordCapture:
    """Riconosce la password che l'utente scrive al prompt di ssh, per proporre di salvarla.

    Riceve l'output del programma (feed_output) e i tasti dell'utente (feed_input). Quando l'accesso riesce,
    feed_output o settle restituiscono (utente, password); se il server la rifiuta, la password scritta si butta.
    Vale solo per l'host della sessione: la richiesta di un host di passaggio (ProxyJump) non conta. Dopo il
    primo accesso riuscito non guarda più niente.

    Nel dubbio rinuncia: una proposta mancata non fa danni, una password sbagliata salvata sì. Per questo, se
    mentre l'utente scrive arriva del testo che non è la richiesta ridisegnata (ConPTY ridisegna lo schermo
    quando la finestra cambia misura), smette di guardare fino al prossimo avvio del programma.
    """

    def __init__(self, host: str) -> None:
        self.host = host.strip().lower()
        self.user = ""
        self.state = "waiting"  # waiting → typing → sent → done
        self._tail = ""
        self._typed = ""

    def _reset(self) -> None:
        self.state, self._typed, self._tail = "waiting", "", ""

    def _give_up(self) -> None:
        self.state, self._typed, self._tail = "done", "", ""

    def _prompt(self) -> bool:
        match = _SSH_PROMPT.search(self._tail)
        if match is None or match.group("host").lower() != self.host:
            return False
        self._tail = ""
        if self.state != "typing":  # se sta già scrivendo è la stessa richiesta ridisegnata: quello che ha scritto resta
            self.user = match.group("user")
            self.state, self._typed = "typing", ""
        return True

    def _asking(self) -> bool:
        """L'ultima riga è un'altra domanda (codice di verifica, passphrase…): l'accesso non è ancora deciso."""
        return self._tail.rstrip(" \t").endswith(":")

    def _result(self) -> Tuple[str, str]:
        result = (self.user, self._typed)
        self.state, self._typed, self._tail = "done", "", ""
        return result

    def feed_output(self, text: str) -> Optional[Tuple[str, str]]:
        if self.state == "done":
            return None
        self._tail = (self._tail + _ANSI.sub("", text).replace("\r", "\n"))[-400:]
        if self._prompt():
            return None  # la prima richiesta, oppure di nuovo: la password di prima era sbagliata
        if self.state == "typing" and self._tail.strip():
            self._give_up()  # ssh ha scritto altro senza aspettare la password: non è più chiaro cosa si sta scrivendo
            return None
        if self.state != "sent":
            return None
        if any(marker in self._tail for marker in _LOGIN_FAILED):
            self._reset()
            return None
        shown = self._tail.strip()
        # Una riga intera o un po' di testo dopo l'Invio, senza rifiuti: il server ha accettato. Per i prompt
        # corti senza riga nuova («fw01> ») c'è settle().
        if shown and not self._asking() and (len(shown) >= 24 or self._tail.rstrip(" \t").endswith("\n")):
            return self._result()
        return None

    def settle(self) -> Optional[Tuple[str, str]]:
        """Dopo qualche secondo dall'Invio: se è arrivato del testo e nessun rifiuto, l'accesso è riuscito."""
        if self.state == "sent" and self._tail.strip() and not self._asking():
            return self._result()
        return None

    def feed_input(self, data: str) -> None:
        if self.state != "typing" or is_terminal_reply(data):
            return
        if data.startswith("\x1b[200~"):  # testo incollato
            data = data[6:].replace("\x1b[201~", "")
        for char in data:
            if char in "\r\n":
                self._tail = ""
                self.state = "sent" if self._typed else "waiting"
                return
            if char in "\x7f\b":
                self._typed = self._typed[:-1]
            elif char == "\x15":  # Ctrl+U: riga cancellata
                self._typed = ""
            elif char in "\x03\x04":  # Ctrl+C, Ctrl+D: rinuncia
                self._reset()
                return
            elif char == "\x1b":  # una freccia o un altro tasto speciale: non fa parte della password
                return
            elif char >= " ":
                self._typed += char


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
