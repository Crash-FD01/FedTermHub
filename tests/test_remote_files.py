#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Test parser e integrazione GTK del browser SFTP contestuale."""
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

from remote_files import RemoteEntry, parse_sftp_long_listing, remote_join, sftp_quote  # noqa: E402


class FakeClient:
    def __init__(self, _connection) -> None:
        self.downloads = []
        self.uploads = []

    def pwd(self) -> str:
        return "/home/utente"

    def listdir(self, path: str) -> list[RemoteEntry]:
        if path == "/home/utente":
            return [
                RemoteEntry("Documenti", "/home/utente/Documenti", "directory", "drwxr-xr-x"),
                RemoteEntry("note con spazi.txt", "/home/utente/note con spazi.txt", "file", "-rw-r--r--", 12),
            ]
        return [RemoteEntry("report.txt", f"{path}/report.txt", "file", "-rw-r--r--", 20)]

    def download(self, remote_path, local_path, recursive=False) -> None:
        self.downloads.append((remote_path, Path(local_path), recursive))

    def upload(self, local_path, remote_directory) -> None:
        self.uploads.append((Path(local_path), remote_directory))


def pump_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def main() -> int:
    listing = """drwxr-xr-x    4 utente users        4096 Jul 13 10:00 .
drwxr-xr-x    9 utente users        4096 Jul 13 10:00 ..
drwxr-xr-x    4 utente users        4096 Jul 13 10:00 /home/utente/.
drwxr-xr-x    9 utente users        4096 Jul 13 10:00 /home/utente/..
drwxr-xr-x    4 utente users        4096 Jul 13 10:00 Documenti
-rw-r--r--    1 utente users          12 Jul 13 09:30 note con spazi.txt
lrwxrwxrwx    1 utente users           8 Jul 12 18:00 ultimo -> release-1
"""
    parsed = parse_sftp_long_listing(listing, "/home/utente")
    assert [item.name for item in parsed] == ["Documenti", "note con spazi.txt", "ultimo"]
    assert all(item.name not in {".", ".."} for item in parsed)
    assert parsed[0].is_directory
    assert parsed[1].path == "/home/utente/note con spazi.txt"
    assert remote_join("/home/utente", "../shared") == "/home/shared"
    assert sftp_quote('a "b"') == '"a \\"b\\""'

    if Gdk.Display.get_default() is None:
        print("REMOTE_FILES_PARSER_OK / UI_SKIP")
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

        application = Gtk.Application(application_id="io.github.crash_fd01.fedtermhub.RemoteFilesTest")
        assert application.register(None)
        window = app.AppWindow(application)
        window.remote_files_sidebar.client_factory = FakeClient
        window.show_all()

        ssh_session = app.Session(
            name="Server Linux",
            protocol="ssh",
            host="linux.example.test",
            port="22",
            user="utente",
            routing_mode="proxy_jump",
            jump_host="bastion.example.test",
            jump_user="jump",
        )
        page = Gtk.Box()
        page.fedtermhub_session = ssh_session
        page.fedtermhub_protocol = "ssh"
        window.notebook.append_page(page, Gtk.Label(label="SSH"))
        window._show_remote_files_for_page(page)

        assert window.sidebar_notebook.get_show_tabs()
        assert window.remote_files_tab_label.get_visible()
        assert pump_until(lambda: len(window.remote_files_sidebar.store) == 1)
        root = window.remote_files_sidebar.store.get_iter_first()
        assert root is not None
        assert window.remote_files_sidebar.store[root][window.remote_files_sidebar.COL_PATH] == "/home/utente"
        assert window.remote_files_sidebar.store.iter_n_children(root) == 2
        documents = window.remote_files_sidebar.store.iter_children(root)
        assert documents is not None
        documents_path = window.remote_files_sidebar.store.get_path(documents)
        window.remote_files_sidebar.tree.expand_row(documents_path, False)
        assert pump_until(
            lambda: window.remote_files_sidebar.store.iter_n_children(documents) == 1
            and window.remote_files_sidebar.store[
                window.remote_files_sidebar.store.iter_children(documents)
            ][window.remote_files_sidebar.COL_NAME] == "report.txt"
        )

        local_file = Path(temp_dir) / "upload.txt"
        local_file.write_text("test", encoding="utf-8")
        client = window.remote_files_sidebar.client
        assert client is not None
        window.remote_files_sidebar._upload_paths([local_file], "/home/utente/Documenti")
        assert pump_until(lambda: len(client.uploads) == 1)
        assert client.uploads[0][1] == "/home/utente/Documenti"
        connection = window.remote_files_sidebar.connection
        assert connection is not None
        assert connection.jump_spec == "jump@bastion.example.test"

        local = Gtk.Box()
        window._show_remote_files_for_page(local)
        assert not window.sidebar_notebook.get_show_tabs()
        assert not window.remote_files_tab_label.get_visible()

        window.destroy()

    print("REMOTE_FILES_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
