#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression test: generated external RDP options must parse in FreeRDP 3.x."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
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


def main() -> int:
    binary = shutil.which("xfreerdp3") or shutil.which("xfreerdp")
    if binary is None:
        print("EXTERNAL_RDP_CLI_SKIP: FreeRDP non installato")
        return 0
    if Gdk.Display.get_default() is None:
        print("EXTERNAL_RDP_CLI_SKIP: nessun display GTK")
        return 0
    if not hasattr(os, "memfd_create"):
        print("EXTERNAL_RDP_CLI_SKIP: memfd non disponibile")
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

        application = Gtk.Application(application_id="io.github.crash_fd01.fedtermhub.CliTest")
        assert application.register(None)
        window = app.AppWindow(application)
        session = app.Session(
            name="Parser CLI",
            protocol="rdp",
            host="127.0.0.1",
            port="1",
            user="parser-test",
            rdp_window_mode="detached",
            rdp_gfx=True,
            rdp_timeout="1000",
            rdp_start_fullscreen=False,
        )
        options = window._external_rdp_options(session, "parser-test", "", None)
        options.append("/p:test-only")

        forbidden = {"+async-input", "+bitmap-cache", "+offscreen-cache"}
        assert forbidden.isdisjoint(options), options
        assert "/cache:bitmap:on,offscreen:on" in options
        assert any(item.startswith("/t:") for item in options)
        assert not any(item.startswith("/title:") for item in options)

        fd = os.memfd_create("fedtermhub-rdp-cli-test", flags=0)
        try:
            os.write(fd, ("\n".join(options) + "\n").encode("utf-8"))
            os.lseek(fd, 0, os.SEEK_SET)
            proc = subprocess.run(
                [binary, f"/args-from:fd:{fd}"],
                pass_fds=(fd,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=8,
                env={**os.environ, "LC_ALL": "C"},
            )
        finally:
            os.close(fd)
            window.destroy()

    output = proc.stdout or ""
    assert proc.returncode != 23, output
    assert "Unexpected keyword" not in output, output
    assert "CommandLineParseArguments" not in output, output
    print("EXTERNAL_RDP_CLI_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
