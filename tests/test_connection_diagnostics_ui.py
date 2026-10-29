#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Smoke test GTK della diagnostica contestuale, eseguibile sotto Xvfb."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "fedtermhub"))


def walk(widget):  # noqa: ANN001
    yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from walk(child)


def main() -> int:
    if Gdk.Display.get_default() is None:
        print("CONNECTION_DIAGNOSTICS_UI_SKIP: nessun display GTK")
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

        target = app.make_target("Server test", "rdp", "192.0.2.10", 3389)

        # Il modulo principale importa i simboli, non espone necessariamente il modulo.
        from connection_diagnostics import DiagnosticStep  # noqa: PLC0415

        def fake_diagnostics_fixed(fake_target, timeout=4.0):  # noqa: ANN001,ARG001
            return app.DiagnosticReport(
                target=fake_target,
                started_at="2026-07-11 12:00:00 CEST",
                elapsed_ms=42,
                steps=[
                    DiagnosticStep("dns", "Risoluzione DNS", "info", "IP diretto"),
                    DiagnosticStep("route", "Instradamento", "ok", "via eth0"),
                    DiagnosticStep("vpn", "VPN", "info", "Nessuna VPN attiva"),
                    DiagnosticStep("tcp", "Porta TCP", "error", "Timeout sulla porta 3389"),
                ],
                recommendation="Controlla VPN e firewall.",
                outcome="error",
            )

        app.run_connection_diagnostics = fake_diagnostics_fixed
        parent = Gtk.Window()
        dialog = app.ConnectionDiagnosticsDialog(parent, target, timeout=1.0)
        dialog.show_all()

        loop = GLib.MainLoop()

        def wait_report() -> bool:
            if dialog.report is not None:
                loop.quit()
                return False
            return True

        GLib.timeout_add(30, wait_report)
        GLib.timeout_add(2500, lambda: (loop.quit(), False)[1])
        loop.run()
        assert dialog.report is not None
        assert len(dialog.results.get_children()) == 4
        assert dialog.copy_button.get_sensitive()
        assert dialog.export_button.get_sensitive()
        assert dialog.summary_text.get_text() == "Controlla VPN e firewall."

        called = []
        error_tab = app.SessionErrorTab(
            "Host non raggiungibile",
            "Connessione fallita",
            "timed out",
            diagnostic_callback=lambda: called.append(True),
        )
        error_tab.show_all()
        diagnostic_buttons = [
            widget for widget in walk(error_tab)
            # L'etichetta porta il mnemonico ("_Diagnostica connessione"):
            # si confronta il testo visibile, senza il trattino basso.
            if isinstance(widget, Gtk.Button)
            and (widget.get_label() or "").replace("_", "") == "Diagnostica connessione"
        ]
        assert len(diagnostic_buttons) == 1
        diagnostic_buttons[0].clicked()
        assert called == [True]

        dialog.destroy()
        error_tab.destroy()
        parent.destroy()

    print("CONNECTION_DIAGNOSTICS_UI_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
