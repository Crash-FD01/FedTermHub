# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Aspetto comune alle due interfacce: colori della finestra, colori delle icone, icone delle cartelle.

La versione Linux (GTK, fedtermhub.py) e la versione Windows (Qt, qtui/) disegnano i widget ognuna a modo
suo, ma prendono da qui gli stessi colori, così le due finestre si somigliano. tests/test_commands.py
controlla che i colori della finestra siano gli stessi del foglio di stile GTK.

Niente GTK e niente Qt qui dentro.
"""
from __future__ import annotations

from typing import Dict

# Il terminale è scuro in entrambi i temi (come nella versione Linux): sfondo, testo e i 16 colori ANSI.
TERMINAL_BACKGROUND = "#0b1220"
TERMINAL_FOREGROUND = "#dbeafe"
TERMINAL_PALETTE = (
    "#556380", "#ef4444", "#22c55e", "#eab308",
    "#3b82f6", "#d946ef", "#06b6d4", "#cbd5e1",
    "#7c8da6", "#f87171", "#4ade80", "#facc15",
    "#60a5fa", "#e879f9", "#22d3ee", "#f8fafc",
)

# Colori delle parti della finestra principale, presi dal foglio di stile GTK («restyle 3.21»).
WINDOW_DARK: Dict[str, str] = {
    "root": "#0b0f16",          # fondo della finestra
    "bar": "#0f141c",           # menu, barra dei comandi, barra di stato
    "rail": "#0f151e",          # barra verticale a sinistra
    "sidebar": "#131a24",       # pannello delle sessioni
    "workspace": "#080b11",     # area delle schede
    "line": "#202b3a",          # bordi fra le parti
    "text": "#e6edf5",
    "tree_text": "#cdd6e2",
    "caption": "#cbd4df",       # scritte sotto le icone della barra dei comandi
    "status_text": "#aab4c2",
    "muted": "#a9b1be",
    "entry": "#0b111a",
    "entry_line": "#223047",
    "focus": "#3b82f6",
    "selected": "#21406b",
    "selected_text": "#ffffff",
    "hover": "#1d2836",
    "tab_text": "#8b96a5",
    "tab_hover": "#161d28",
    "button": "#232831",
    "button_line": "#3a424f",
    "button_hover": "#303641",
    "menu": "#191d24",
    "menu_line": "#343a46",
    "menu_hover": "#28303a",
    "version": "#79c7ff",
    "terminal": TERMINAL_BACKGROUND,
    "terminal_text": TERMINAL_FOREGROUND,
    "ok": "#49d17d",
    "pending": "#f3c969",
    "error": "#ff7185",
    "off": "#7d8796",
    "accent": "#1f6fa8",        # azione principale (Salva, Applica), testo bianco
    "accent_hover": "#2479b6",
    "danger": "#ff9b9b",
}
WINDOW_LIGHT: Dict[str, str] = {
    "root": "#e9edf3",
    "bar": "#f7f9fc",
    "rail": "#eef2f7",
    "sidebar": "#f7f9fc",
    "workspace": "#dfe4ec",
    "line": "#d5dce6",
    "text": "#1b2530",
    "tree_text": "#28323d",
    "caption": "#35414f",
    "status_text": "#5a6674",
    "muted": "#687587",
    "entry": "#ffffff",
    "entry_line": "#c8d2df",
    "focus": "#2f7fd6",
    "selected": "#cfe0f4",
    "selected_text": "#0f2438",
    "hover": "#e4ebf3",
    "tab_text": "#5a6674",
    "tab_hover": "#dde4ee",
    "button": "#ffffff",
    "button_line": "#c8d2df",
    "button_hover": "#eef3f8",
    "menu": "#ffffff",
    "menu_line": "#d5dce6",
    "menu_hover": "#e4ebf3",
    "version": "#0878b8",
    "terminal": TERMINAL_BACKGROUND,
    "terminal_text": TERMINAL_FOREGROUND,
    "ok": "#14672f",
    "pending": "#9a6700",
    "error": "#b91c1c",
    "off": "#5b6676",
    "accent": "#1f6fa8",
    "accent_hover": "#2479b6",
    "danger": "#b91c1c",
}

# Le icone di FedTermHub (fedtermhub/icons/*.svg) sono disegnate con «currentColor»: ogni interfaccia
# le colora così. Colori accesi sullo scuro, più saturi sul chiaro per restare leggibili sul bianco.
ICON_DEFAULT = ("#cbd5e1", "#3a4653")
ICON_COLORS: Dict[str, tuple] = {
    "fth-proto-ssh": ("#22d3ee", "#0e7490"),
    "fth-proto-rdp": ("#60a5fa", "#2563eb"),
    "fth-proto-vnc": ("#c084fc", "#7c3aed"),
    "fth-proto-sftp": ("#34d399", "#059669"),
    "fth-proto-serial": ("#fbbf24", "#b45309"),
    "fth-proto-local": ("#cbd5e1", "#475569"),
    "fth-proto-telnet": ("#f472b6", "#be185d"),
    "fth-proto-ftp": ("#fb923c", "#c2410c"),
    "fth-act-delete": ("#f87171", "#dc2626"),
    "fth-tb-terminale": ("#22d3ee", "#0e7490"),
    "fth-tb-sessioni": ("#60a5fa", "#2563eb"),
    "fth-tb-rete": ("#34d399", "#059669"),
    "fth-tb-strumenti": ("#c084fc", "#7c3aed"),
    "fth-tb-password": ("#fbbf24", "#b45309"),
    "fth-tb-vista": ("#f472b6", "#be185d"),
    "fth-tb-sistema": ("#9aa5b3", "#475569"),
    "fth-tb-lab": ("#5eead4", "#0d9488"),
}


def window_colors(dark: bool) -> Dict[str, str]:
    return WINDOW_DARK if dark else WINDOW_LIGHT


def icon_color(name: str, dark: bool) -> str:
    """Il colore del tratto di un'icona di FedTermHub (nome senza .svg) nel tema scuro o chiaro."""
    pair = ICON_COLORS.get(name, ICON_DEFAULT)
    return pair[0] if dark else pair[1]


def colored_svg(template: str, name: str, dark: bool) -> str:
    return template.replace("currentColor", icon_color(name, dark))


def folder_svg(color: str) -> str:
    """L'icona di una cartella nel colore scelto dall'utente (Modifica cartella › Colore)."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="17" viewBox="0 0 20 17">\n'
        f'<path d="M1.2 3.2c0-1 .8-1.8 1.8-1.8h4.7l1.7 2h7.6c1 0 1.8.8 1.8 1.8v8.7c0 1-.8 1.8-1.8 1.8H3c-1 0-1.8-.8-1.8-1.8z" fill="{color}"/>\n'
        f'<path d="M1.2 5.2h17.6v8.7c0 1-.8 1.8-1.8 1.8H3c-1 0-1.8-.8-1.8-1.8z" fill="{color}" opacity=".92"/>\n'
        '<path d="M2 5.2h16" stroke="#ffffff" stroke-opacity=".28"/>\n'
        '<path d="M1.2 3.2c0-1 .8-1.8 1.8-1.8h4.7l1.7 2h7.6c1 0 1.8.8 1.8 1.8v8.7c0 1-.8 1.8-1.8 1.8H3c-1 0-1.8-.8-1.8-1.8z" '
        'fill="none" stroke="#000000" stroke-opacity=".32"/>\n'
        '</svg>'
    )


# L'icona della cartella principale (Home Session).
HOME_SESSION_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="22" height="20" viewBox="0 0 22 20">
<path d="M2 8.2 11 1.4l9 6.8v9.2c0 .8-.7 1.5-1.5 1.5H3.5C2.7 18.9 2 18.2 2 17.4z" fill="#4aa3df"/>
<path d="M5.5 9.2v7.2h11V9.2L11 5.1z" fill="#1b75bb" opacity=".92"/>
<path d="M8.2 18.9v-5.4c0-.6.5-1.1 1.1-1.1h3.4c.6 0 1.1.5 1.1 1.1v5.4" fill="#0f1e2b" opacity=".65"/>
<path d="M2 8.2 11 1.4l9 6.8" fill="none" stroke="#dff4ff" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="17.1" cy="4.5" r="2.2" fill="#77e2ff" stroke="#0f1e2b" stroke-width=".8"/>
</svg>"""
