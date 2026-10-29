#!/usr/bin/python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Parita' fra i due fogli di stile e leggibilita' dei colori.

I temi chiaro e scuro sono due blocchi gemelli di ~300 righe mantenuti a mano:
divergono per costruzione, e nella 0.9.06 nove classi risultavano stilizzate
solo in dark_css(). La piu' grave era il pallino di stato della connessione
(.is-connected / .is-disconnected), che in tema chiaro restava del colore del
testo e quindi non indicava piu' nulla.

Questo test non gira GTK: legge i due fogli dal sorgente e confronta gli
insiemi di classi. Serve a far fallire la build quando qualcuno aggiunge una
regola a un tema solo.

Uso:  python3 tests/test_theme_parity.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SORGENTE = Path(__file__).resolve().parent.parent / "fedtermhub" / "fedtermhub.py"

# Classi note come applicate dal codice ma volutamente prive di stile: sono
# agganci predisposti e mai riempiti. Elencarle qui le tiene fuori dal
# fallimento, ma visibili a chi legge.
SENZA_STILE_ACCETTATE = {
    "command-palette-row", "diagnostics-dialog", "diagnostics-row",
    "menu-separator", "session-editor", "theme-toggle-button",
}

# Coppie (colore, sfondo su cui compare) che devono restare leggibili.
# Lo sfondo e' quello REALE dell'elemento, non il bianco o il nero della
# finestra: il pallino di stato vive sull'header delle schede.
CONTRASTI = [
    ("scuro  .is-connected",     "#22c55e", "#0b0f16"),
    ("scuro  .is-disconnected",  "#ef4444", "#0b0f16"),
    ("chiaro .is-connected",     "#14672f", "#e9edf3"),
    ("chiaro .is-disconnected",  "#b91c1c", "#e9edf3"),
    ("chiaro .tab-close",        "#5b6676", "#e9edf3"),
    ("chiaro .tab-reconnect",    "#9a4708", "#e9edf3"),
    # testo bianco sull'azione principale (Salva, Applica, Avvia), entrambi i temi
    ("azione principale",        "#ffffff", "#1f6fa8"),
    ("azione principale :hover", "#ffffff", "#2479b6"),
    # finestra Password: testi secondari e segnali, sullo sfondo reale di ciascun pannello
    ("scuro  .vault-field-caption",   "#9aa6b4", "#151920"),
    ("scuro  .vault-row-subtitle",    "#a9b4c2", "#12161c"),
    ("scuro  subtitle selezionata",   "#dbeaf6", "#285f87"),
    ("scuro  .vault-group-header",    "#9cc3e4", "#12161c"),
    ("scuro  .vault-badge",           "#c9d3df", "#222a35"),
    ("scuro  .vault-usage-kind",      "#a9d3f5", "#1d3346"),
    ("scuro  .vault-delete",          "#ff9b9b", "#151920"),
    ("scuro  .vault-error",           "#ff9b9b", "#0f1115"),
    ("scuro  .vault-hint",            "#dbeaf6", "#17283a"),
    ("scuro  .vault-avatar",          "#cfe8fb", "#24415a"),
    ("chiaro .vault-field-caption",   "#5b6676", "#ffffff"),
    ("chiaro .vault-row-subtitle",    "#4b5a6b", "#ffffff"),
    ("chiaro subtitle selezionata",   "#24435b", "#c5e7fb"),
    ("chiaro .vault-group-header",    "#4a5d73", "#ffffff"),
    ("chiaro .vault-badge",           "#3a4a5c", "#e9eef4"),
    ("chiaro .vault-usage-kind",      "#17507d", "#e3f0fb"),
    ("chiaro .vault-delete",          "#b42318", "#ffffff"),
    ("chiaro .vault-error",           "#b91c1c", "#f5f7fa"),
    ("chiaro .vault-hint",            "#173a57", "#eaf4fd"),
    ("chiaro .vault-avatar",          "#1b4f78", "#dbe9f6"),
]
SOGLIA = 4.5


def _luminanza(hexcol: str) -> float:
    h = hexcol.lstrip("#")
    canali = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        canali.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = canali
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrasto(a: str, b: str) -> float:
    la, lb = _luminanza(a), _luminanza(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _senza_commenti(css: str) -> str:
    """Rimuove i commenti /* ... */.

    Necessario, non cosmetico: un commento che nomina una classe (per spiegare
    perche' esiste) la farebbe risultare presente anche dopo che la regola e'
    stata cancellata, e il controllo passerebbe su un tema rotto. E' successo
    davvero alla prima stesura di questo test.
    """
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.S)


def _fogli(src: str) -> tuple[str, str]:
    blocchi = [b for b in re.findall(r'"""(.*?)"""', src, re.S)
               if len(b.splitlines()) > 300]
    if len(blocchi) < 2:
        raise AssertionError(
            "Non trovo i due fogli di stile: la struttura di fedtermhub.py e' "
            "cambiata e questo test va aggiornato."
        )
    scuro, chiaro = _senza_commenti(blocchi[0]), _senza_commenti(blocchi[1])
    # Non dare per scontato l'ordine: se i due blocchi vengono scambiati nel
    # sorgente, il test deve continuare a dire la verita' su quale e' quale.
    if "#0f1115" in chiaro and "#0f1115" not in scuro:
        scuro, chiaro = chiaro, scuro
    return scuro, chiaro


def _errori_mnemonici(src: str) -> list[str]:
    """Due mnemonici uguali nella stessa finestra: uno dei due non risponde.

    Il confronto e' per classe contenitrice, non per file: finestre diverse
    possono benissimo riusare la stessa lettera.
    """
    import ast
    from collections import Counter, defaultdict

    classi = [(n.lineno, n.end_lineno, n.name)
              for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ClassDef)]
    per_classe: dict[str, list[str]] = defaultdict(list)
    for m in re.finditer(r'new_with_mnemonic\("([^"]+)"\)', src):
        riga = src[:m.start()].count("\n") + 1
        dentro = sorted((c for c in classi if c[0] <= riga <= c[1]),
                        key=lambda c: c[1] - c[0])
        lettera = re.search(r"_(\w)", m.group(1))
        if dentro and lettera:
            per_classe[dentro[0][2]].append(lettera.group(1).lower())

    fuori = []
    for cls, lettere in sorted(per_classe.items()):
        doppie = sorted(k for k, v in Counter(lettere).items() if v > 1)
        if doppie:
            fuori.append(f"Mnemonici in conflitto in {cls}: "
                         + ", ".join("Alt+" + d.upper() for d in doppie))
    return fuori


def _errori_tooltip() -> list[str]:
    """Un pulsante con la sola icona e senza tooltip non e' identificabile."""
    fuori = []
    for percorso in sorted(SORGENTE.parent.glob("*.py")):
        righe = percorso.read_text(encoding="utf-8").splitlines()
        for i, riga in enumerate(righe):
            if "new_from_icon_name" in riga and "Button" in riga:
                if "set_tooltip" not in "\n".join(righe[i:i + 5]):
                    fuori.append(f"Pulsante solo-icona senza tooltip: "
                                 f"{percorso.name}:{i + 1}")
    return fuori


def main() -> int:
    src = SORGENTE.read_text(encoding="utf-8")
    scuro, chiaro = _fogli(src)
    # Un nome che finisce con "-" e' il prefisso di una concatenazione
    # (add_class("diagnostic-" + step.status)): non e' una classe reale e
    # verrebbe segnalato a vuoto.
    applicate = {c for c in re.findall(r'add_class\(\s*["\']([a-z0-9-]+)["\']', src)
                 if not c.endswith("-")}
    errori: list[str] = []

    # Confronto per (classe, stato), non per sola classe: cercare ".tab-close"
    # come sottostringa lo trova anche quando resta solo ".tab-close:hover" e
    # la regola base e' sparita. Estrarre i token dai selettori gestisce da se'
    # i selettori raggruppati, perche' ogni classe del gruppo diventa un token.
    def token(css: str) -> set[tuple[str, str]]:
        fuori = set()
        for corpo in re.split(r"\{[^{}]*\}", css):
            for cls, stato in re.findall(r"\.([a-z0-9-]+)(:[a-z-]+)?", corpo):
                fuori.add((cls, stato))
        return fuori

    t_scuro, t_chiaro = token(scuro), token(chiaro)
    nei_due = {c for c, _ in t_scuro} | {c for c, _ in t_chiaro}

    def descrivi(coppie: set[tuple[str, str]]) -> list[str]:
        return sorted(f".{c}{s}" for c, s in coppie if c in applicate)

    solo_scuro = descrivi(t_scuro - t_chiaro)
    solo_chiaro = descrivi(t_chiaro - t_scuro)
    if solo_scuro:
        errori.append("Stilizzate solo nel tema SCURO: " + ", ".join(solo_scuro))
    if solo_chiaro:
        errori.append("Stilizzate solo nel tema CHIARO: " + ", ".join(solo_chiaro))

    mai = sorted(c for c in applicate if c not in nei_due)
    nuove = set(mai) - SENZA_STILE_ACCETTATE
    if nuove:
        errori.append(
            "Classi applicate dal codice e mai stilizzate in nessun tema: "
            + ", ".join("." + c for c in sorted(nuove))
            + " — definiscile in entrambi i fogli, oppure aggiungile a "
              "SENZA_STILE_ACCETTATE se sono agganci voluti."
        )

    for nome, colore, sfondo in CONTRASTI:
        r = contrasto(colore, sfondo)
        if r < SOGLIA:
            errori.append(f"Contrasto insufficiente {nome}: {colore} su "
                          f"{sfondo} = {r:.2f}:1 (minimo {SOGLIA})")

    errori.extend(_errori_mnemonici(src))
    errori.extend(_errori_tooltip())

    if errori:
        print("FALLITO — parita' dei temi\n")
        for e in errori:
            print("  *", e)
        return 1

    print(f"OK — {len(applicate)} classi applicate dal codice, "
          f"parita' fra i due temi rispettata")
    print(f"OK — {len(CONTRASTI)} coppie colore/sfondo sopra {SOGLIA}:1")
    print("OK — nessun mnemonico in conflitto, nessun pulsante solo-icona senza tooltip")
    return 0


if __name__ == "__main__":
    sys.exit(main())
