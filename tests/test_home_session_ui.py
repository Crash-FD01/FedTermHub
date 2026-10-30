#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""GTK smoke test della Home Session protetta e del menu contestuale dedicato."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# I test controllano i testi originali in italiano, qualunque sia la lingua del sistema.
os.environ["FEDTERMHUB_LANGUAGE"] = "it"
sys.path.insert(0, str(PROJECT_ROOT / "fedtermhub"))


def menu_labels(menu: Gtk.Menu) -> list[str]:
    labels: list[str] = []
    for child in menu.get_children():
        if isinstance(child, Gtk.SeparatorMenuItem):
            continue
        if isinstance(child, Gtk.MenuItem):
            label = child.get_label()
            if label:
                labels.append(label)
    return labels


def main() -> int:
    if Gdk.Display.get_default() is None:
        print("HOME_SESSION_UI_SKIP: nessun display GTK")
        return 0

    with tempfile.TemporaryDirectory() as temp_dir:
        config_root = Path(temp_dir) / "config"
        cache_root = Path(temp_dir) / "cache"
        os.environ["XDG_CONFIG_HOME"] = str(config_root)
        os.environ["XDG_CACHE_HOME"] = str(cache_root)
        app_config = config_root / "fedtermhub"
        app_config.mkdir(parents=True, exist_ok=True)
        (app_config / "settings.json").write_text(
            json.dumps({"startup_mode": "empty", "theme": "light", "show_error_popups": False}),
            encoding="utf-8",
        )

        import fedtermhub as app  # noqa: PLC0415

        application = Gtk.Application(application_id="io.github.crash_fd01.fedtermhub.HomeSessionTest")
        assert application.register(None)
        window = app.AppWindow(application)
        window.show_all()

        window._select_store_path(window.store.home_path())
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        selected = window.selected_path_and_node()
        assert selected is not None and selected[0] == window.store.home_path()
        window.on_session_selection_changed(window.session_tree.get_selection())

        for label in ("Modifica", "Duplica", "Sposta", "Elimina"):
            assert not window.session_action_buttons[label].get_sensitive(), label
        assert window.session_action_buttons["Nuova sessione"].get_sensitive()
        assert window.session_action_buttons["Nuova cartella"].get_sensitive()

        home_menu = window._build_session_context_menu(selected)
        home_labels = menu_labels(home_menu)
        expected_present = {
            "Espandi o comprimi", "Apri tutte le sessioni", "Nuova sessione", "Nuova cartella",
            "Esporta tutte le sessioni", "Importa sessioni qui",
            "Sostituisci tutte le sessioni con un file", "Dettagli",
        }
        assert expected_present.issubset(set(home_labels)), home_labels
        forbidden = {
            "Modifica", "Duplica", "Sposta", "Colore cartella", "Esporta la selezione", "Elimina",
        }
        assert forbidden.isdisjoint(set(home_labels)), home_labels

        # Una cartella normale conserva invece le proprie azioni e il colore.
        folder_path = "0.0"
        window._select_store_path(folder_path)
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        normal = window.selected_path_and_node()
        assert normal is not None and normal[0] == folder_path
        window.on_session_selection_changed(window.session_tree.get_selection())
        for label in ("Modifica", "Duplica", "Sposta", "Elimina"):
            assert window.session_action_buttons[label].get_sensitive(), label
        folder_labels = menu_labels(window._build_session_context_menu(normal))
        assert "Colore cartella" in folder_labels
        assert "Modifica" in folder_labels
        assert "Elimina" in folder_labels

        home_menu.destroy()
        window.destroy()

    print("HOME_SESSION_UI_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
