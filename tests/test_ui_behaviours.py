#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Comportamenti dell'interfaccia introdotti nella 0.9.07, provati con GTK reale.

1. Una sessione nuova finisce in fondo al primo livello di Home Session, non
   nella cartella selezionata, e resta li' dopo il salvataggio e la
   riapertura (ensure_home_session_root non la deve spostare).
2. La sidebar dei file remoti nasconde i file che iniziano con un punto, li
   mostra con l'interruttore, filtra per nome senza nascondere le cartelle e
   dichiara quanti elementi sono esclusi.
3. Con il fuoco nel terminale, Ctrl+lettera e Ctrl+Shift+V arrivano al
   terminale invece di essere intercettati dalla finestra.

Uso:  xvfb-run -a python3 tests/test_ui_behaviours.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# I test controllano i testi originali in italiano, qualunque sia la lingua del sistema.
os.environ["FEDTERMHUB_LANGUAGE"] = "it"
sys.path.insert(0, str(PROJECT_ROOT / "fedtermhub"))


def pump_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def check_new_session_placement(app, window) -> None:
    store = window.store
    home = store.home_node()
    # Una cartella con dentro una sessione, selezionata nell'albero: prima
    # della 0.9.07 la sessione nuova finiva qui dentro.
    cartella = app.FolderNode(name="Clienti", children=[app.Session(name="esistente", protocol="ssh", host="a.example.test")])
    store.add_node(store.home_path(), cartella)
    window.refresh_sessions()
    percorso_cartella = store.find_node_path(cartella)
    window._select_store_path(percorso_cartella)
    pump_until(lambda: window.selected_path_and_node() is not None)
    assert window.selected_path_and_node()[0] == percorso_cartella

    nuova = app.Session(name="Nuova di prova", protocol="ssh", host="b.example.test", port="22")

    class FintoDialog:
        def __init__(self, *_a, **_k) -> None:
            pass

        def run(self) -> Gtk.ResponseType:
            return Gtk.ResponseType.OK

        def get_session(self):
            return nuova

        def should_offer_save_password(self) -> bool:
            return False

        def get_transient_password(self) -> str:
            return ""

        def destroy(self) -> None:
            pass

    originale = app.SessionDialog
    app.SessionDialog = FintoDialog
    try:
        window.on_add_session(None)
    finally:
        app.SessionDialog = originale

    assert nuova not in cartella.children, "la sessione e' finita nella cartella selezionata"
    assert home.children[-1] is nuova, "la sessione non e' in fondo al primo livello di Home Session"
    assert len(store.nodes) == 1, "la radice deve contenere solo Home Session"

    # Riapertura: nessuna normalizzazione deve spostarla.
    ricaricato = app.SessionStore(store.path)
    ricaricato.load()
    nomi = [n.name for n in ricaricato.home_node().children]
    assert nomi[-1] == "Nuova di prova", nomi
    print("  sessione nuova: in fondo a Home Session, anche dopo la riapertura")


class FintoClient:
    def __init__(self, _connection) -> None:
        pass

    def pwd(self) -> str:
        return "/home/utente"

    def listdir(self, path: str):
        from remote_files import RemoteEntry  # noqa: PLC0415

        if path == "/home/utente":
            return [
                RemoteEntry(".bashrc", "/home/utente/.bashrc", "file", "-rw-r--r--", 10),
                RemoteEntry(".ssh", "/home/utente/.ssh", "directory", "drwx------"),
                RemoteEntry("Documenti", "/home/utente/Documenti", "directory", "drwxr-xr-x"),
                RemoteEntry("nut.conf", "/home/utente/nut.conf", "file", "-rw-r--r--", 5),
                RemoteEntry("ups.conf", "/home/utente/ups.conf", "file", "-rw-r--r--", 5),
                RemoteEntry("leggimi.txt", "/home/utente/leggimi.txt", "file", "-rw-r--r--", 5),
            ]
        return []


def nomi_radice(sidebar) -> list[str]:
    root = sidebar.store.get_iter_first()
    if root is None:
        return []
    fuori = []
    child = sidebar.store.iter_children(root)
    while child is not None:
        fuori.append(sidebar.store[child][sidebar.COL_NAME])
        child = sidebar.store.iter_next(child)
    return fuori


def check_remote_filters(app, window) -> None:
    sidebar = window.remote_files_sidebar
    sidebar.client_factory = FintoClient
    sessione = app.Session(name="Server", protocol="ssh", host="linux.example.test", port="22", user="utente")
    page = Gtk.Box()
    page.fedtermhub_session = sessione
    page.fedtermhub_protocol = "ssh"
    window.notebook.append_page(page, Gtk.Label(label="SSH"))
    window._show_remote_files_for_page(page)

    attesi = ["Documenti", "nut.conf", "ups.conf", "leggimi.txt"]
    assert pump_until(lambda: nomi_radice(sidebar) == attesi), nomi_radice(sidebar)
    assert "2 non mostrati" in sidebar.status_label.get_text(), sidebar.status_label.get_text()

    sidebar.hidden_button.set_active(True)
    assert pump_until(lambda: ".bashrc" in nomi_radice(sidebar)), nomi_radice(sidebar)
    assert ".ssh" in nomi_radice(sidebar)

    sidebar.hidden_button.set_active(False)
    sidebar.filter_entry.set_text("conf")
    sidebar._on_filter_changed(sidebar.filter_entry)
    # le cartelle restano visibili anche se il nome non corrisponde
    assert pump_until(lambda: nomi_radice(sidebar) == ["Documenti", "nut.conf", "ups.conf"]), nomi_radice(sidebar)
    print("  file remoti: dotfile nascosti, interruttore e filtro per nome corretti")


def evento_tasto(window, keyval: int, ctrl: bool = True, shift: bool = False):
    ev = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    ev.key.keyval = keyval
    stato = 0
    if ctrl:
        stato |= Gdk.ModifierType.CONTROL_MASK
    if shift:
        stato |= Gdk.ModifierType.SHIFT_MASK
    ev.key.state = Gdk.ModifierType(stato)
    ev.key.window = window.get_window()
    # Si restituisce l'evento INTERO, non ev.key: ev.key e' una vista dentro
    # ev, e se ev viene liberato punta a memoria non piu' valida. La prima
    # stesura di questo test restituiva ev.key e passava per caso: il gestore
    # leggeva ctrl=False e un nome di tasto vuoto.
    return ev


def check_terminal_keys(app, window) -> None:
    """Con il fuoco nel terminale, Ctrl+lettera deve arrivare al terminale.

    In GTK3 il gestore della finestra viene chiamato prima del widget con il
    fuoco: Ctrl+W chiudeva la scheda invece di arrivare a bash o a nano, e
    Ctrl+K/L/T/B non arrivavano mai. Ctrl+Shift+V apriva il gestore password
    invece di incollare. Il gestore deve restituire False (non consumato).
    """
    window.add_terminal_tab("tasti", ["/bin/sleep", "60"])
    tab = window.terminal_tabs[-1]
    pump_until(lambda: bool(getattr(tab, "panes", [])))
    pagine = window.notebook.get_n_pages()
    tab.panes[0].terminal.grab_focus()
    pump_until(lambda: window._terminal_has_focus())
    assert window._terminal_has_focus(), "il terminale non ha preso il fuoco"

    for nome in ("w", "k", "l", "t", "b", "h"):
        consumato = window.on_global_key_press(window, evento_tasto(window, Gdk.keyval_from_name(nome)))
        assert not consumato, f"Ctrl+{nome.upper()} intercettato dalla finestra con il fuoco nel terminale"
    assert not window.on_global_key_press(window, evento_tasto(window, Gdk.KEY_V, shift=True)), \
        "Ctrl+Shift+V deve incollare nel terminale, non aprire il gestore password"
    assert window.notebook.get_n_pages() == pagine, "una scorciatoia ha chiuso o aperto una scheda"

    # Con il fuoco altrove le scorciatoie dell'applicazione funzionano ancora.
    window.session_tree.grab_focus()
    pump_until(lambda: not window._terminal_has_focus())
    visibile = window.sidebar.get_visible()
    assert window.on_global_key_press(window, evento_tasto(window, Gdk.KEY_b)), "Ctrl+B non gestito fuori dal terminale"
    pump_until(lambda: window.sidebar.get_visible() != visibile)
    assert window.sidebar.get_visible() != visibile, "Ctrl+B non ha cambiato la barra laterale"
    window.toggle_sidebar()
    print("  tasti: Ctrl+lettera arriva al terminale; fuori dal terminale le scorciatoie funzionano")


def main() -> int:
    if Gdk.Display.get_default() is None:
        print("UI_BEHAVIOURS_SKIP: nessun display GTK")
        return 0
    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["XDG_CONFIG_HOME"] = str(Path(temp_dir) / "config")
        os.environ["XDG_CACHE_HOME"] = str(Path(temp_dir) / "cache")
        config_dir = Path(os.environ["XDG_CONFIG_HOME"]) / "fedtermhub"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "settings.json").write_text(
            json.dumps({"startup_mode": "empty", "theme": "light", "show_error_popups": False}),
            encoding="utf-8",
        )
        import fedtermhub as app  # noqa: PLC0415

        application = Gtk.Application(application_id="io.github.crash_fd01.fedtermhub.UiBehavioursTest")
        assert application.register(None)
        window = app.AppWindow(application)
        window.show_all()
        check_new_session_placement(app, window)
        check_remote_filters(app, window)
        check_terminal_keys(app, window)
        window.destroy()
    print("UI_BEHAVIOURS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
