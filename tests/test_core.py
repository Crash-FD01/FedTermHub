#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Core persistence and compatibility checks for FedTermHub."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def verify_ui_source_invariants() -> None:
    source = (PROJECT_ROOT / "fedtermhub" / "fedtermhub.py").read_text(encoding="utf-8")
    assert "def make_theme_toggle_button" in source
    assert "ribbon.pack_end(self.make_theme_toggle_button()" in source
    assert '("Tema scuro", lambda *_: self.set_theme_from_menu("dark"))' not in source
    assert '("Tema chiaro", lambda *_: self.set_theme_from_menu("light"))' not in source
    assert '_add_popup_item(menu, "Tema scuro"' not in source
    assert '_add_popup_item(menu, "Tema chiaro"' not in source
    assert "self.theme_combo" not in source
    assert 'window.set_icon_list(_APP_ICON_PIXBUFS)' in source
    assert 'window.set_icon_from_file(str(_APP_ICON_FILES[0]))' not in source
    assert 'self.get_style_context().add_class("session-error-tab")' in source
    assert '#workspace, #terminal-notebook, #terminal-notebook stack { background: #ffffff;' in source
    light = source.split('def light_css() -> str:', 1)[1].split('def on_session_filter_changed', 1)[0]
    assert '#workspace, #terminal-notebook, #terminal-notebook stack,\n        .terminal-surface' not in light
    assert 'col_name.add_attribute(renderer_name, "cell-background"' not in source
    assert 'def _apply_session_tree_cell_background' in source
    assert 'rdp_start_fullscreen: bool = False' in source
    assert 'rdp_monitor: str = "current"' in source
    assert 'def enumerate_monitors()' in source
    assert 'def resolve_monitor_selector(' in source
    assert 'fullscreen_on_monitor(self.get_screen(), descriptor.index)' in source
    assert 'rdp_window_mode: str = "embedded"' in source
    assert 'class RdpDetachedWindow' in source
    assert 'def detach_native_rdp_tab(' in source
    assert 'RDP aperto in una finestra indipendente' in source
    # 0.9.04: gruppo finestre dedicato — i dialoghi modali della finestra
    # principale non devono bloccare le finestre indipendenti (RDP sganciata
    # e pannello Lab temporaneo).
    detached_block = source.split("class RdpDetachedWindow", 1)[1].split("class AppWindow", 1)[0]
    assert "self._window_group = Gtk.WindowGroup()" in detached_block
    assert "self._window_group.add_window(self)" in detached_block
    lab_source = (PROJECT_ROOT / "fedtermhub" / "ephemeral_lab.py").read_text(encoding="utf-8")
    lab_dialog_block = lab_source.split("class EphemeralLabDialog", 1)[1]
    assert "self._window_group = Gtk.WindowGroup()" in lab_dialog_block
    assert "self._window_group.add_window(self)" in lab_dialog_block
    # 0.9.04: la cartella condivisa passa sempre dal validatore prima di
    # finire nella riga di comando FreeRDP (/drive:fedtermhub,percorso).
    assert "def normalize_rdp_drive_path(" in source
    assert source.count('/drive:fedtermhub,{normalize_rdp_drive_path(sess.rdp_drive)}') == 2
    assert '/drive:fedtermhub,{sess.rdp_drive}' not in source
    assert "class ConnectionDiagnosticsDialog" in source
    assert 'add_item("Diagnostica connessione", self.on_diagnose_selected)' in source
    assert 'diagnostic_session: Optional[Session] = None' in source
    assert 'self.routing_expander = Gtk.Expander(label="Instradamento avanzato")' in source
    assert 'routing_mode: str = "direct"' in source
    assert 'def proxy_jump_spec(sess: Session)' in source
    assert 'if routing_mode == "rd_gateway"' in source
    assert 'options.append("/gateway:" + ",".join(parts))' in source
    # 0.9.07: i pulsanti etichettati usano new_with_mnemonic per l'accesso
    # da tastiera (Alt+lettera), quindi l'etichetta contiene un underscore.
    assert 'Gtk.Button.new_with_mnemonic("_Diagnostica connessione")' in source
    assert "def _build_session_context_menu" in source
    assert "Home Session è una radice applicativa protetta" in source
    assert "self.store.is_home_path(path)" in source
    assert 'button.set_sensitive(has_selection and not home_selected)' in source
    diagnostic_source = (PROJECT_ROOT / "fedtermhub" / "connection_diagnostics.py").read_text(encoding="utf-8")
    assert "def run_connection_diagnostics" in diagnostic_source
    assert '"dns", "Risoluzione DNS"' in diagnostic_source
    assert '"route", "Instradamento"' in diagnostic_source
    assert '"vpn", "VPN"' in diagnostic_source
    assert '"tcp", "Porta TCP"' in diagnostic_source
    native_source = (PROJECT_ROOT / "fedtermhub" / "native_rdp.py").read_text(encoding="utf-8")
    assert 'autostart_on_map: bool = True' in native_source
    assert 'self.preferred_monitor = str(preferred_monitor or "current")' in native_source
    # 3.16.3: il renderer riusa staging e buffer di visualizzazione invece di
    # allocare un bytearray e una ImageSurface nuovi a ogni frame.
    assert 'self.frame_data = bytearray(data)' not in native_source
    assert 'self._staging' in native_source
    assert 'self.frame_data[0:size] = staging[0:size]' in native_source
    assert 'self.surface.mark_dirty()' in native_source
    assert 'GLib.timeout_add(16, self._poll_frame)' in native_source
    assert 'cairo.ImageSurface.create_for_data(' in native_source
    assert 'def _ensure_shared_surface(' not in native_source
    assert 'self.canvas.queue_draw()' in native_source
    assert 'def set_detach_callback(' in native_source
    native_c = (PROJECT_ROOT / "native" / "fedtermrdp_native.c").read_text(encoding="utf-8")
    assert 'reserved[1] = dirty_x' not in native_c
    assert 'static BOOL copy_frame(rdpContext* context)' in native_c
    # 0.9.04: drag&drop di file verso la sessione RDP via cliprdr.
    # Python: drop GTK sul canvas -> uri-list nella mailbox condivisa (kind=1).
    assert 'def write_host_uri_list(' in native_source
    assert 'CLIP_KIND_URILIST = 1' in native_source
    assert 'drag-data-received' in native_source
    assert 'CLIP_VERSION = 3' in native_source
    # C: annuncio FileGroupDescriptorW + byte serviti dal CliprdrFileContext
    # (CB_FILECONTENTS_REQUEST/RESPONSE del client common di FreeRDP 3).
    assert 'freerdp/client/client_cliprdr_file.h' in native_c
    assert 'FTH_CLIP_KIND_URILIST' in native_c
    assert 'cliprdr_file_context_init' in native_c
    assert 'cliprdr_serialize_file_list_ex' in native_c
    assert 'native_cliprdr_server_capabilities' in native_c
    assert '#define FTH_CLIP_VERSION 3u' in native_c
    # 3.16.3: rotellina negativa in complemento a due e input immediato.
    assert '0x100u - amount' in native_c
    assert 'CreateFileDescriptorEvent' in native_c
    # 3.16.3: terminale con palette ANSI reale, reflow delle colonne e F11.
    # 3.17: il terminale multi-riquadro vive nel modulo terminal_panel; qui resta
    # solo il workspace immersivo a livello di finestra.
    assert 'def set_terminal_fullscreen(' in source
    assert 'self.terminal.set_colors(fg, bg, [])' not in source
    terminal_source = (PROJECT_ROOT / "fedtermhub" / "terminal_panel.py").read_text(encoding="utf-8")
    assert 'TERMINAL_PALETTE' in terminal_source
    assert 'class TerminalPane' in terminal_source
    assert 'class TerminalWorkspace' in terminal_source
    assert 'scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)' in terminal_source
    assert 'self.terminal.set_colors(fg, bg, palette)' in terminal_source
    assert 'set_word_char_exceptions' in terminal_source
    assert 'match_add_regex' in terminal_source
    assert 'COLORTERM' in terminal_source
    assert 'def split(' in terminal_source
    assert 'def feed_all(' in terminal_source
    # 3.17.2: command palette (Ctrl+K), iteratore sessioni, SIGWINCH al focus
    assert 'class CommandPalette' in source
    assert 'def iter_sessions(' in source
    assert 'def open_command_palette(' in source
    assert 'pty.set_size(rows, columns)' in terminal_source
    # 3.18: riconnessione e indicatori di stato sulle tab
    assert 'def reconnect(self)' in terminal_source
    assert 'def respawn(self)' in terminal_source
    assert 'def is_connected' in terminal_source
    assert 'tab.on_state_change = refresh_status' in source
    assert 'tab-status' in source
    # 3.19: icone vettoriali di protocollo e azioni
    assert 'def register_app_icons(' in source
    assert 'fth-proto-' in source
    assert (PROJECT_ROOT / "fedtermhub" / "icons" / "fth-proto-ssh.svg").exists()
    assert (PROJECT_ROOT / "fedtermhub" / "icons" / "fth-proto-rdp.svg").exists()
    assert (PROJECT_ROOT / "fedtermhub" / "icons" / "fth-act-new.svg").exists()
    # 3.20: icone protocollo colorate per tema e refresh visivo
    assert 'proto_colors' in source
    assert 'restyle 3.21' in source
    assert 'fth-tb-terminale' in source
    assert (PROJECT_ROOT / 'fedtermhub' / 'icons' / 'fth-tb-terminale.svg').exists()
    assert '/gfx:progressive' in native_source
    assert 'options = [o for o in options if o not in {"/gfx", "+gfx"}]' not in native_source


def main() -> int:
    verify_ui_source_invariants()
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        os.environ["XDG_CONFIG_HOME"] = str(root / ".config")
        os.environ["XDG_CACHE_HOME"] = str(root / ".cache")
        sys.path.insert(0, str(PROJECT_ROOT / "fedtermhub"))

        import fedtermhub as app  # noqa: PLC0415
        import network_tools  # noqa: PLC0415

        config = Path(os.environ["XDG_CONFIG_HOME"]) / "fedtermhub"
        app.CONFIG_DIR = config
        app.SESSIONS_FILE = config / "sessions.json"
        app.TUNNELS_FILE = config / "tunnels.json"
        app.VAULT_FILE = config / "vault.json"
        app.SETTINGS_FILE = config / "settings.json"

        sessions = app.SessionStore(app.SESSIONS_FILE)
        assert sessions.home_node().name == app.HOME_SESSION_NAME
        assert mode(config) == 0o700
        assert mode(app.SESSIONS_FILE) == 0o600
        sessions.home_node().color = "#ffffff"
        sessions.save()
        normalized = app.SessionStore(app.SESSIONS_FILE)
        assert normalized.home_node().color == app.HOME_SESSION_COLOR
        assert normalized.home_node().name == app.HOME_SESSION_NAME

        app.SESSIONS_FILE.write_text(
            json.dumps([{"type": "session", "name": "Legacy", "protocol": "ssh"}]),
            encoding="utf-8",
        )
        migrated = app.SessionStore(app.SESSIONS_FILE)
        assert migrated.home_node().children[0].name == "Importate dalla 0.1"

        app.SESSIONS_FILE.write_text(
            json.dumps(
                {
                    "items": [
                        {"type": "folder", "name": app.HOME_SESSION_NAME, "children": []},
                        {"type": "session", "name": "Seconda", "protocol": "ssh"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        merged = app.SessionStore(app.SESSIONS_FILE)
        assert len(merged.nodes) == 1
        assert any(getattr(node, "name", "") == "Seconda" for node in merged.home_node().children)

        tunnels = app.TunnelStore(app.TUNNELS_FILE)
        tunnels.save()
        assert mode(app.TUNNELS_FILE) == 0o600
        assert len(app.TunnelStore(app.TUNNELS_FILE).tunnels) == len(tunnels.tunnels)

        settings = app.AppSettings()
        settings.save(app.SETTINGS_FILE)
        assert mode(app.SETTINGS_FILE) == 0o600
        assert app.AppSettings.load(app.SETTINGS_FILE).theme == settings.theme

        monitor_a = app.MonitorDescriptor(
            index=0, selector=app.make_monitor_selector(0, "BOE", "Panel", 1920, 1080, 1),
            label="Monitor 1", manufacturer="BOE", model="Panel", width=1920, height=1080,
            scale=1, primary=True,
        )
        monitor_b = app.MonitorDescriptor(
            index=1, selector=app.make_monitor_selector(1, "Dell", "U2723QE", 3840, 2160, 1),
            label="Monitor 2", manufacturer="Dell", model="U2723QE", width=3840, height=2160,
            scale=1, primary=False,
        )
        chosen, exact = app.resolve_monitor_selector(monitor_b.selector, [monitor_a, monitor_b], 0)
        assert chosen == monitor_b and exact
        moved_selector = app.make_monitor_selector(3, "Dell", "U2723QE", 3840, 2160, 1)
        chosen, exact = app.resolve_monitor_selector(moved_selector, [monitor_a, monitor_b], 0)
        assert chosen == monitor_b and exact
        missing_selector = app.make_monitor_selector(4, "LG", "Missing", 2560, 1440, 1)
        chosen, exact = app.resolve_monitor_selector(missing_selector, [monitor_a, monitor_b], 0)
        assert chosen == monitor_a and not exact
        chosen, exact = app.resolve_monitor_selector("current", [monitor_a, monitor_b], None)
        assert chosen == monitor_a and not exact
        chosen, exact = app.resolve_monitor_selector("primary", [monitor_a, monitor_b], None)
        assert chosen == monitor_a and exact

        # 0.9.04: cartella condivisa /drive — percorso validato e normalizzato.
        share_dir = root / "condivisa"
        share_dir.mkdir()
        assert app.normalize_rdp_drive_path(str(share_dir)) == str(share_dir)
        assert app.normalize_rdp_drive_path("") == ""
        share_file = root / "file.txt"
        share_file.write_text("x", encoding="utf-8")
        for bad in (
            str(share_dir) + ",altro",
            str(root / "mancante"),
            str(share_file),
            "percorso\ncon-newline",
        ):
            try:
                app.normalize_rdp_drive_path(bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"Percorso /drive non valido accettato: {bad!r}")

        rdp = app.Session(
            name="RDP esterno", protocol="rdp", rdp_start_fullscreen=True,
            rdp_monitor=monitor_b.selector, rdp_window_mode="detached",
        )
        restored_rdp = app.Session.from_dict(rdp.to_dict())
        assert restored_rdp.rdp_start_fullscreen is True
        assert restored_rdp.rdp_monitor == monitor_b.selector
        assert restored_rdp.rdp_window_mode == "detached"
        legacy_embedded = app.Session.from_dict({"name": "RDP legacy", "protocol": "rdp"})
        assert legacy_embedded.rdp_window_mode == "embedded"

        routed = app.Session(
            name="SSH via bastion", protocol="ssh", host="server.internal",
            routing_mode="proxy_jump", jump_host="bastion.local",
            jump_port="2222", jump_user="admin",
        )
        restored_routed = app.Session.from_dict(routed.to_dict())
        assert restored_routed.routing_mode == "proxy_jump"
        assert restored_routed.jump_host == "bastion.local"
        assert app.AppWindow.proxy_jump_spec(restored_routed) == "admin@bastion.local:2222"
        legacy_tunnel = app.Session.from_dict({
            "name": "Legacy", "protocol": "rdp", "required_tunnel_id": "old-tunnel"
        })
        assert legacy_tunnel.routing_mode == "saved_tunnel"

        vault = app.PasswordVault(app.VAULT_FILE)
        vault.create("correct horse battery staple")
        vault.credentials.append(app.Credential(name="test", username="u", password="p"))
        vault.save()
        assert mode(app.VAULT_FILE) == 0o600
        vault.lock()
        vault.unlock("correct horse battery staple")
        assert vault.credentials[0].password == "p"
        vault.lock()
        try:
            vault.unlock("wrong password")
        except Exception:
            pass
        else:
            raise AssertionError("Il vault ha accettato una password errata")

        payload = app.PasswordVault.encrypt_credentials_payload(
            [app.Credential(name="export", password="secret")], "export-pass"
        )
        restored = app.PasswordVault.decrypt_credentials_payload(payload, "export-pass")
        assert restored[0].password == "secret"

        network_tools.CONFIG_DIR = config
        network_tools.NETWORK_SHARES_FILE = config / "network_shares.json"
        network_tools._save_network_shares(
            {"ftp": [{"name": "share", "path": str(root), "mode": "rw"}], "sftp": []}
        )
        assert mode(network_tools.NETWORK_SHARES_FILE) == 0o600
        assert network_tools._load_network_shares()["ftp"][0]["name"] == "share"

    print("CORE_TEST_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
