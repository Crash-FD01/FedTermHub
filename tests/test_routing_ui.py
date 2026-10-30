#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""GTK and command-building tests for user-friendly advanced routing."""
from __future__ import annotations

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


def combo_ids(combo: Gtk.ComboBoxText) -> list[str]:
    model = combo.get_model()
    values: list[str] = []
    if model is None:
        return values
    tree_iter = model.get_iter_first()
    while tree_iter is not None:
        # GtkComboBoxText stores id in column 1 when entries are added with append(id, text).
        values.append(str(model.get_value(tree_iter, 1)))
        tree_iter = model.iter_next(tree_iter)
    return values


def main() -> int:
    if Gdk.Display.get_default() is None:
        print("ROUTING_UI_SKIP: nessun display GTK")
        return 0

    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["XDG_CONFIG_HOME"] = str(Path(temp_dir) / "config")
        os.environ["XDG_CACHE_HOME"] = str(Path(temp_dir) / "cache")
        import fedtermhub as app  # noqa: PLC0415

        parent = Gtk.Window()

        ssh_session = app.Session(name="Linux", protocol="ssh", host="server.local")
        dialog = app.SessionDialog(parent, ssh_session, tunnels=[("tun-1", "Tunnel cliente")])
        assert not dialog.routing_expander.get_expanded()
        assert set(combo_ids(dialog.routing_mode)) == {"direct", "saved_tunnel", "proxy_jump"}
        dialog.routing_mode.set_active_id("proxy_jump")
        dialog.jump_host.set_text("bastion.local")
        dialog.jump_port.set_text("2222")
        dialog.jump_user.set_text("jumpuser")
        restored_ssh = dialog.get_session()
        assert restored_ssh.routing_mode == "proxy_jump"
        assert app.AppWindow.proxy_jump_spec(restored_ssh) == "jumpuser@bastion.local:2222"
        roundtrip_ssh = app.Session.from_dict(restored_ssh.to_dict())
        assert roundtrip_ssh.jump_host == "bastion.local"
        dialog.destroy()

        old_tunnel = app.Session.from_dict({
            "name": "Legacy tunnel", "protocol": "rdp", "host": "127.0.0.1",
            "required_tunnel_id": "tun-1",
        })
        assert old_tunnel.routing_mode == "saved_tunnel"

        rdp_session = app.Session(
            name="RDP gateway", protocol="rdp", host="pc.internal",
            routing_mode="rd_gateway", rdp_gateway_host="rdg.example.test",
            rdp_gateway_user="gateway-user", rdp_gateway_domain="EXAMPLE",
            rdp_gateway_use_same_credentials=False,
            rdp_gateway_credential_id="gw-cred",
        )
        dialog = app.SessionDialog(
            parent,
            rdp_session,
            credentials=[("gw-cred", "Gateway")],
            tunnels=[("tun-1", "Tunnel cliente")],
        )
        assert dialog.routing_expander.get_expanded()
        assert set(combo_ids(dialog.routing_mode)) == {"direct", "saved_tunnel", "rd_gateway"}
        assert dialog.routing_mode.get_active_id() == "rd_gateway"
        assert dialog.rdp_mode.get_active_id() == "native"
        assert dialog.rdp_gateway_credential.get_sensitive()
        saved_rdp = dialog.get_session()
        assert saved_rdp.rdp_gateway_host == "rdg.example.test"
        assert saved_rdp.rdp_gateway_credential_id == "gw-cred"
        dialog.destroy()

        class FakeWindow:
            @staticmethod
            def gateway_identity(session, target_user, target_domain, target_password):
                if session.rdp_gateway_use_same_credentials:
                    return target_user, target_domain, target_password
                return session.rdp_gateway_user, session.rdp_gateway_domain, "gateway-secret"

        fake = FakeWindow()
        options = app.AppWindow._build_native_rdp_options(
            fake, saved_rdp, "target-user", "TARGET", "target-secret"
        )
        gateway = next(item for item in options if item.startswith("/gateway:"))
        assert "g:rdg.example.test" in gateway
        assert "u:gateway-user" in gateway
        assert "d:EXAMPLE" in gateway
        assert "p:gateway-secret" in gateway
        assert "usage-method:direct" in gateway
        assert "type:auto" in gateway
        masked = app.AppWindow.mask_args([gateway])
        assert "gateway-secret" not in masked
        assert "p:********" in masked

        target = app.AppWindow.diagnostic_target_for_session(None, saved_rdp)
        assert target is not None
        assert target.host == "rdg.example.test"
        assert target.port == 443

        jump_target = app.AppWindow.diagnostic_target_for_session(None, restored_ssh)
        assert jump_target is not None
        assert jump_target.host == "bastion.local"
        assert jump_target.port == 2222

        parent.destroy()

    print("ROUTING_UI_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
