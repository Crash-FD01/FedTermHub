#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""GTK smoke test for monitor-targeted native RDP fullscreen.

Run interactively or under Xvfb, for example:
    xvfb-run -a python3 tests/test_rdp_monitor_ui.py
"""
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


def main() -> int:
    if Gdk.Display.get_default() is None:
        print("RDP_MONITOR_UI_SKIP: nessun display GTK")
        return 0

    with tempfile.TemporaryDirectory() as temp_dir:
        config_root = Path(temp_dir) / "config"
        cache_root = Path(temp_dir) / "cache"
        os.environ["XDG_CONFIG_HOME"] = str(config_root)
        os.environ["XDG_CACHE_HOME"] = str(cache_root)
        app_config = config_root / "fedtermhub"
        app_config.mkdir(parents=True, exist_ok=True)
        (app_config / "settings.json").write_text(
            json.dumps({"startup_mode": "empty", "theme": "light", "rdp_preflight": False}),
            encoding="utf-8",
        )

        import fedtermhub as app  # noqa: PLC0415

        monitors = app.enumerate_monitors()
        assert monitors, "GDK non ha enumerato monitor"
        assert hasattr(Gtk.Window, "fullscreen_on_monitor")

        parent = Gtk.Window()
        session = app.Session(
            name="RDP monitor esterno",
            protocol="rdp",
            rdp_start_fullscreen=True,
            rdp_monitor=monitors[0].selector,
            rdp_window_mode="detached",
        )
        dialog = app.SessionDialog(parent, session=session)
        dialog.show_all()
        assert dialog.rdp_start_fullscreen.get_active()
        assert dialog.rdp_monitor.get_active_id() == monitors[0].selector
        assert dialog.rdp_detach_on_open.get_active()
        assert dialog.rdp_monitor.get_sensitive()
        restored = dialog.get_session()
        assert restored.rdp_start_fullscreen
        assert restored.rdp_monitor == monitors[0].selector
        assert restored.rdp_window_mode == "detached"
        dialog.rdp_mode.set_active_id("legacy")
        assert dialog.rdp_detach_on_open.get_sensitive()
        assert dialog.rdp_start_fullscreen.get_sensitive()
        assert dialog.rdp_monitor.get_sensitive()
        dialog.rdp_mode.set_active_id("native")
        assert dialog.rdp_detach_on_open.get_sensitive()
        assert dialog.rdp_start_fullscreen.get_sensitive()
        assert dialog.rdp_monitor.get_sensitive()
        dialog.destroy()
        parent.destroy()

        application = Gtk.Application(application_id="io.github.crash_fd01.fedtermhub.UiTest")
        assert application.register(None)
        window = app.AppWindow(application)
        window.show_all()
        # La modalità già sganciata deve restare nel wrapper GTK gestito
        # da FedTermHub, non nel client xfreerdp esterno.
        source = Path(app.__file__).read_text(encoding="utf-8")
        launch_block = source[source.index("def _launch_rdp_ready"):source.index("def _launch_rdp_ready") + 2200]
        assert "_launch_external_rdp(sess" not in launch_block
        tab_type = app.NativeRdpTab
        assert tab_type is not None
        tab = tab_type(
            "Fullscreen monitor test",
            ["/v:127.0.0.1"],
            None,
            autostart_on_map=False,
            preferred_monitor=monitors[0].selector,
        )
        window.add_widget_tab("Fullscreen monitor test", tab)
        window.set_native_rdp_fullscreen(tab, True, monitors[0].selector)
        assert tab.fullscreen_active
        assert tab.preferred_monitor == monitors[0].selector

        def finish() -> bool:
            window.set_native_rdp_fullscreen(tab, False, monitors[0].selector)
            assert not tab.fullscreen_active
            window.detach_native_rdp_tab(tab, monitors[0].selector, False)
            assert window.notebook.page_num(tab) == -1
            assert len(window.detached_rdp_windows) == 1
            detached = window.detached_rdp_windows[0]
            assert tab.get_toplevel() is detached
            detached.set_rdp_fullscreen(True)
            assert detached.fullscreen_active
            assert tab.fullscreen_active
            assert not window._fullscreen
            detached.set_rdp_fullscreen(False)
            assert not detached.fullscreen_active
            detached.close_session()
            assert not window.detached_rdp_windows
            window.destroy()
            Gtk.main_quit()
            return False

        GLib.timeout_add(450, finish)
        Gtk.main()

    print("RDP_MONITOR_UI_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
