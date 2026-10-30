#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# I test controllano i testi originali in italiano, qualunque sia la lingua del sistema.
os.environ["FEDTERMHUB_LANGUAGE"] = "it"
sys.path.insert(0, str(ROOT / "fedtermhub"))

import ephemeral_lab  # noqa: E402
from ephemeral_lab import (  # noqa: E402
    EphemeralLabDialog,
    LabConfig,
    LabSupport,
    build_virt_install_command,
    create_cloud_init_files,
    generate_password,
    safe_domain_name,
)


class FakeController:
    def list_instances(self):
        return []

    def start_async(self, *_args, **_kwargs):
        raise AssertionError("non deve essere avviato nel test UI")

    def open_console(self, _identifier):
        return None

    def destroy(self, _identifier):
        return None


def main() -> int:
    name = safe_domain_name("Test VM / cliente")
    assert name.startswith("fth-Test-VM-cliente-")
    assert len(name) < 64

    password = generate_password()
    assert len(password) >= 12
    assert any(c.islower() for c in password)
    assert any(c.isupper() for c in password)
    assert any(c.isdigit() for c in password)

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        image = root / "base.qcow2"
        image.write_bytes(b"placeholder")
        config = LabConfig(
            name="Linux test",
            base_image=image,
            memory_mb=2048,
            vcpus=2,
            network="isolated",
            username="fedlab",
            password="Password-123",
        )
        user_data, meta_data = create_cloud_init_files(root, config)
        assert "fedlab" in user_data.read_text()
        assert "Password-123" in user_data.read_text()
        assert os.stat(user_data).st_mode & 0o777 == 0o600
        assert os.stat(meta_data).st_mode & 0o777 == 0o600
        overlay = root / "overlay.qcow2"
        argv = build_virt_install_command(config, "fth-test", overlay, user_data, meta_data)
        joined = " ".join(argv)
        assert "--transient" in argv
        assert "qemu:///session" in argv
        assert "--network none" in joined
        assert "--cloud-init" in argv
        assert "path=" + str(overlay) in joined

        nat = LabConfig(
            name=config.name,
            base_image=config.base_image,
            memory_mb=config.memory_mb,
            vcpus=config.vcpus,
            network="user",
            username=config.username,
            password=config.password,
        )
        nat_argv = build_virt_install_command(nat, "fth-test2", overlay, user_data, meta_data)
        assert "--network user" in " ".join(nat_argv)

    ephemeral_lab.inspect_lab_support = lambda: LabSupport(True, (), (), "Supporto simulato disponibile.")
    from gi.repository import Gtk
    window = Gtk.Window()
    dialog = EphemeralLabDialog(window, FakeController())
    deadline = __import__("time").monotonic() + 2.0
    while __import__("time").monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if dialog.start_button.get_sensitive():
            break
        __import__("time").sleep(0.02)
    assert dialog.start_button.get_sensitive()
    assert dialog.preset_combo.get_active_id() == "standard"
    assert dialog.network_combo.get_active_id() == "isolated"
    dialog.destroy()
    window.destroy()

    print("test_ephemeral_lab: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
