#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Strumenti di rete provati dal vivo sotto Xvfb, sulla macchina di prova.

Copre i difetti trovati nella revisione della 0.9.10:
 - porte in ascolto: colonna Processo leggibile invece del testo grezzo di ss;
 - port scanner: niente destinazioni che nmap leggerebbe come opzioni, sessione creata con la
   porta trovata e solo per servizi apribili (prima tutto diventava SSH sulla 22);
 - cattura: dopo la fine non resta una callback idle che gira all'infinito (100% di CPU), il file
   successivo ha un nome nuovo;
 - server HTTP: una porta gia' occupata viene segnalata, non mostrata come server attivo;
 - operazioni da amministratore raggruppate in una sola richiesta di autorizzazione;
 - Wake-on-LAN: Magic Packet corretto.
Le parti che richiedono nmap o tcpdump (e i privilegi per catturare) si saltano se mancano.

Uso:  xvfb-run -a python3 tests/test_network_tools.py
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "fedtermhub"))


def pump(seconds: float) -> None:
    # Limitato nel tempo anche dentro: con una callback idle che si ripropone all'infinito la coda
    # degli eventi non si svuota mai, e il test deve fallire invece di restare bloccato.
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while Gtk.events_pending() and time.monotonic() < end:
            Gtk.main_iteration_do(False)
        time.sleep(0.02)


def wait_for(predicate, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pump(0.1)
        if predicate():
            return True
    return bool(predicate())


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Banner:
    """Servizio TCP di prova che si presenta come OpenSSH."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            # La scansione -sT chiude la connessione subito: un reset non deve fermare il servizio,
            # altrimenti il riconoscimento della versione che segue non trova piu' nessuno.
            try:
                conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
            except OSError:
                pass
            finally:
                conn.close()


def main() -> int:
    if Gdk.Display.get_default() is None:
        print("NETWORK_TOOLS_SKIP: nessun display GTK")
        return 0
    with tempfile.TemporaryDirectory() as temp_dir:
        tmp = Path(temp_dir)
        os.environ["XDG_CONFIG_HOME"] = str(tmp / "config")
        os.environ["XDG_CACHE_HOME"] = str(tmp / "cache")
        import network_tools as nt  # noqa: PLC0415

        messages = []
        nt.ToolPage.message = staticmethod(lambda _p, title, text, error=False: messages.append((title, text)))
        window = Gtk.Window()
        window.show()
        service = Banner()

        # --- logica pura
        assert nt._ss_process_text('users:(("sshd",pid=812,fd=3),("sshd",pid=900,fd=3))') == "sshd (pid 812, 900)"
        assert nt._ss_process_text("") == ""
        assert nt.session_protocol_for_service("2222", "ssh") == "ssh"
        assert nt.session_protocol_for_service("3389", "ms-wbt-server") == "rdp"
        assert nt.session_protocol_for_service("5901", "") == "vnc"
        assert nt.session_protocol_for_service("23", "telnet") == "telnet"
        assert nt.session_protocol_for_service("80", "http") == "", "HTTP non e' una sessione SSH"
        calls = []
        original_run = nt._run
        nt._run = lambda args, timeout=30: (calls.append(list(args)), original_run(["true"]))[1]
        try:
            nt._run_privileged_script(["echo uno", "echo due", "echo tre"])
        finally:
            nt._run = original_run
        assert len(calls) == 1 and "echo uno\necho due\necho tre" in calls[0][-1], calls
        print("  logica: processi di ss, servizi apribili, una sola autorizzazione")

        # --- porte in ascolto
        if shutil.which("ss"):
            page = nt.ListeningPortsPage(window)
            assert wait_for(lambda: "socket" in page.status.get_text() or "Errore" in page.status.get_text(), 15)
            rows = [tuple(row) for row in page.store if row[3] == str(service.port)]
            assert rows and "pid" in rows[0][4] and "users:" not in rows[0][4], rows
            print("  porte in ascolto:", rows[0][4])

        # --- Wake-on-LAN
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(3)
        wol = nt.WakeOnLanPage(window)
        wol.mac.set_text("aa:bb:cc:dd:ee:ff")
        wol.broadcast.get_child().set_text("127.0.0.1")
        wol.port.set_value(receiver.getsockname()[1])
        wol.repeat.set_value(1)
        wol.send(None)
        assert receiver.recv(200) == bytes.fromhex("FF" * 6 + "aabbccddeeff" * 16)
        print("  Wake-on-LAN: Magic Packet corretto")

        # --- port scanner
        created = []
        scanner = nt.PortScannerPage(window, lambda proto, host, port="": created.append((proto, host, port)))
        if shutil.which("nmap"):
            # Senza nmap la scansione si ferma prima, con "Nmap non installato".
            scanner.target.set_text("-oN/tmp/fedtermhub-x")
            scanner.scan(None)
            assert messages and messages[-1][0] == "Destinazione non valida", messages
            scanner.target.set_text("127.0.0.1")
            scanner.profile.set_active_id("custom")
            scanner.ports.set_text(str(service.port))
            scanner.services.set_active(True)
            scanner.scan(None)
            assert wait_for(lambda: "port" in scanner.status.get_text() or "Errore" in scanner.status.get_text()
                            or "Nessuna" in scanner.status.get_text(), 120), scanner.status.get_text()
            assert len(scanner.store) == 1, scanner.status.get_text()
            scanner.tree.get_selection().select_path(Gtk.TreePath.new_first())
            scanner.create_from_selected(None)
            assert created == [("ssh", "127.0.0.1", str(service.port))], (created, [tuple(r) for r in scanner.store], messages[-1:])
            print("  port scanner: servizio riconosciuto, sessione con la porta trovata")
        else:
            print("  port scanner: nmap assente, scansione saltata")

        # --- server HTTP
        services = nt.ServicesPage(window)
        started = time.monotonic()
        services.refresh_status()
        assert time.monotonic() - started < 0.2, "il controllo dei servizi non deve bloccare l'interfaccia"
        shared = tmp / "condivisa"
        shared.mkdir()
        (shared / "ciao.txt").write_text("ciao", encoding="utf-8")
        services.http_dir.set_filename(str(shared))
        pump(0.5)
        port = free_port()
        services.http_port.set_value(port)
        services.start_http(None)
        assert nt.RUNTIME.http_process is not None, messages[-1:]
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/ciao.txt", timeout=3).read() == b"ciao"
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        services.http_port.set_value(busy.getsockname()[1])
        services.start_http(None)
        assert nt.RUNTIME.http_process is None and "già usata" in messages[-1][1], messages[-1]
        services.stop_http(None)
        print("  server HTTP: avvio verificato, porta occupata segnalata")

        # --- cattura pacchetti (serve tcpdump e il permesso di catturare)
        if shutil.which("tcpdump") and os.geteuid() == 0:
            capture = nt.PacketCapturePage(window)
            capture.interface.set_active_id("lo")
            capture.count.set_value(0)
            capture.duration.set_value(0)
            capture.filter_entry.set_text(f"tcp port {service.port}")
            capture.start_capture(None)
            pump(2)
            for _ in range(3):
                with socket.create_connection(("127.0.0.1", service.port)) as conn:
                    conn.recv(40)
            pump(1)
            first = Path(capture.output.get_text())
            capture.stop_capture(None)
            assert wait_for(lambda: "Completata" in capture.status.get_text(), 15), capture.status.get_text()
            assert first.stat().st_size > 24, "file pcap vuoto"
            assert Path(capture.output.get_text()) != first, "la cattura successiva sovrascriverebbe la prima"
            # Con la callback idle che restituiva True il ciclo degli eventi non si svuotava piu'.
            deadline = time.monotonic() + 2
            while Gtk.events_pending() and time.monotonic() < deadline:
                Gtk.main_iteration_do(False)
            assert not Gtk.events_pending(), "una callback idle continua a girare dopo la cattura"
            print("  cattura: file scritto, arresto pulito, nessuna callback infinita")
        else:
            print("  cattura: tcpdump o privilegi assenti, saltata")
        GLib.idle_add(lambda: False)
        window.destroy()
    print("NETWORK_TOOLS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
