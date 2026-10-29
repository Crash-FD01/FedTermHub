#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Test senza rete esterna per la diagnostica di connessione."""
from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "fedtermhub"))

import connection_diagnostics as diag  # noqa: E402


def open_local_listener() -> tuple[socket.socket, int, threading.Thread]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])

    def serve_once() -> None:
        try:
            conn, _address = listener.accept()
            try:
                conn.sendall(b"SSH-2.0-FedTermHub-Test\r\n")
            finally:
                conn.close()
        except OSError:
            pass

    thread = threading.Thread(target=serve_once, daemon=True)
    thread.start()
    return listener, port, thread


def main() -> int:
    assert diag.normalize_port("rdp", "") == 3389
    assert diag.normalize_port("ssh", "2222") == 2222
    assert diag.normalize_port("ssh", "99999") == 0
    assert diag.make_target("IPv6", "ssh", "[::1]", 22).endpoint == "[::1]:22"

    listener, port, thread = open_local_listener()
    try:
        target = diag.make_target("SSH locale", "ssh", "127.0.0.1", port)
        report = diag.run_connection_diagnostics(target, timeout=1.2)
    finally:
        listener.close()
        thread.join(timeout=1.0)

    assert report.step("dns") is not None
    assert report.step("route") is not None
    assert report.step("vpn") is not None
    tcp = report.step("tcp")
    assert tcp is not None and tcp.status == "ok", report.to_text()
    assert str(port) in tcp.summary
    assert "SSH-2.0-FedTermHub-Test" in tcp.details
    assert report.outcome == "ok"
    text = report.to_text()
    assert "password" not in text.lower() or "non contiene password" in text.lower()
    assert "SSH locale" in text

    # Porta locale chiusa: deve essere distinta da un timeout.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    closed_port = int(probe.getsockname()[1])
    probe.close()
    closed = diag.run_connection_diagnostics(
        diag.make_target("Porta chiusa", "rdp", "127.0.0.1", closed_port), timeout=1.0
    )
    closed_tcp = closed.step("tcp")
    assert closed_tcp is not None and closed_tcp.status == "error"
    assert "rifiutata" in closed_tcp.summary.lower(), closed.to_text()
    assert "servizio" in closed.recommendation.lower()

    missing = diag.run_connection_diagnostics(diag.make_target("Vuota", "rdp", "", ""), timeout=1.0)
    assert missing.outcome == "error"
    assert missing.step("target") is not None

    original_resolver = diag._resolve_host
    try:
        diag._resolve_host = lambda _host, _timeout: ([], "Name or service not known")
        unresolved = diag.run_connection_diagnostics(
            diag.make_target("DNS interno", "rdp", "server.invalid", 3389), timeout=1.0
        )
    finally:
        diag._resolve_host = original_resolver
    assert unresolved.step("dns").status == "error"
    assert "dns" in unresolved.recommendation.lower()

    print("CONNECTION_DIAGNOSTICS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
