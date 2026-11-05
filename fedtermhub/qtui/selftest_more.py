# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prove automatiche di Telnet, seriale, VNC, strumenti di rete, SFTP e tunnel (parte di --self-test).

Telnet e VNC usano i server finti di selftest_servers; la seriale usa la porta virtuale loop:// di pyserial.
SSH, SFTP e tunnel partono solo con FEDTERMHUB_SELFTEST_SSH="host;porta;utente;password" (in CI: il server
OpenSSH del runner con un utente di prova).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Callable

from .. import model
from . import net_core, selftest_servers


def run_checks(app, window, check: Callable[[str, Callable[[], object]], None],  # noqa: ANN001, C901
               wait: Callable, report_path: Path) -> None:
    from .files_panel import FilesPanel
    from .main_window import TerminalTab
    from .vnc_tab import VncTab

    window.settings.ssh_strict_host_key = "accept-new"  # la prova non può rispondere alle domande di ssh

    def terminal_tab(sess: model.Session) -> TerminalTab:
        before = window.tabs.count()
        window.open_session(sess)
        assert window.tabs.count() == before + 1, "scheda non aperta"
        tab = window.tabs.currentWidget()
        assert isinstance(tab, TerminalTab), type(tab)
        return tab

    def collect(tab: TerminalTab) -> list:
        output: list = []
        tab.view.pty.output.connect(output.append)
        return output

    def close(tab) -> None:  # noqa: ANN001
        window.close_tab(window.tabs.indexOf(tab))

    # ------------------------------------------------------------------ Telnet
    def telnet() -> str:
        server = selftest_servers.start_telnet()
        tab = terminal_tab(model.Session(name="telnet-prova", protocol="telnet", host="127.0.0.1", port=str(server.port)))
        out = collect(tab)
        assert wait(app, lambda: "login:" in "".join(out), 15), "nessuna risposta dal server Telnet: " + "".join(out)[-200:]
        wait(app, lambda: tab.view.ready, 20)
        tab.view.on_input("prova\r")
        assert wait(app, lambda: "FTH_TELNET_OK" in "".join(out), 10), "risposta mancante: " + "".join(out)[-200:]
        line = [l for l in "".join(out).splitlines() if "FTH_TELNET_OK" in l][0].strip()
        size = line.split()[-1]
        assert size != "0x0", "dimensioni della finestra (NAWS) non inviate"
        close(tab)
        server.close()
        return f"{line}; opzioni: {', '.join(server.log[:4])}"

    check("Telnet integrato", telnet)

    # ------------------------------------------------------------------ seriale
    def serial() -> str:
        tab = terminal_tab(model.Session(name="seriale-prova", protocol="serial", host="loop://", port="9600"))
        out = collect(tab)
        assert wait(app, lambda: tab.running, 10), "porta non aperta: " + "".join(out)[-200:]
        tab.view.on_input("FTH_SERIALE")
        assert wait(app, lambda: "FTH_SERIALE" in "".join(out), 10), "nessuna eco dalla porta: " + "".join(out)[-200:]
        close(tab)
        return "loop:// 9600 8N1"

    check("Console seriale", serial)

    # ------------------------------------------------------------------ VNC
    def vnc() -> str:
        server = selftest_servers.start_vnc("Vnc-prova")
        tab = VncTab(window, model.Session(name="vnc-prova", protocol="vnc", host="127.0.0.1", port=str(server.port)),
                     "", "Vnc-prova")
        window.tabs.setCurrentIndex(window.tabs.addTab(tab, "vnc-prova"))
        window._update_welcome()
        assert wait(app, lambda: tab.connected or any(e.startswith("disconnected") for e in tab.events), 30), \
            "il client VNC non risponde: " + ", ".join(tab.events)
        assert tab.connected, "connessione VNC fallita: " + ", ".join(tab.events) + " " + tab.state_label.text()
        wait(app, lambda: "frame" in server.log, 10)
        wait(app, lambda: False, 1.5)
        pixels: list = []
        tab.view.page().runJavaScript("fthPixel(20, 20)", 0, pixels.append)
        tab.view.page().runJavaScript("fthPixel(300, 190)", 0, pixels.append)
        assert wait(app, lambda: len(pixels) == 2, 10), "schermo VNC non leggibile"
        top_left, bottom_right = (json.loads(p) for p in pixels)
        assert top_left[:3] == [255, 0, 0], f"colore in alto a sinistra {top_left[:3]} invece di rosso"
        assert bottom_right[:3] == [255, 255, 255], f"colore in basso a destra {bottom_right[:3]} invece di bianco"
        tab.page_bridge.sendKeys.emit("[65]")
        assert wait(app, lambda: 65 in server.keys, 5), "tasto non arrivato al server"
        window.grab().save(str(report_path.with_name("vnc-session.png")))
        tab.close_tab()
        close(tab)
        server.close()
        return f"schermo {top_left[3]}x{top_left[4]}, autenticazione e tastiera ok"

    check("VNC incorporato", vnc)

    # ------------------------------------------------------------------ strumenti di rete
    def tools() -> str:
        folder = Path(tempfile.mkdtemp(prefix="fth-tools-"))
        (folder / "config.txt").write_bytes(b"hostname switch-prova\n" * 50)
        logs: list = []
        tftp = net_core.TftpServer(folder, 0, logs.append, bind="127.0.0.1")
        data = net_core.tftp_get("127.0.0.1", tftp.port, "config.txt")
        tftp.stop()
        assert data == (folder / "config.txt").read_bytes(), "TFTP: file diverso"
        http = net_core.HttpFileServer(folder, 0, logs.append, bind="127.0.0.1")
        body = urllib.request.urlopen(f"http://127.0.0.1:{http.port}/config.txt", timeout=5).read()  # noqa: S310
        http.stop()
        assert body == data, "HTTP: file diverso"
        server = selftest_servers.start_telnet()
        import threading

        found = net_core.scan_ports("127.0.0.1", [server.port, selftest_servers.free_port()], lambda *_: None,
                                    lambda *_: None, threading.Event())
        assert [r.port for r in found] == [server.port], f"scansione porte: {found}"
        listening = net_core.listening_ports()
        assert any(item.port == server.port for item in listening), "porta in ascolto non trovata"
        rtt = net_core.ping("127.0.0.1")
        assert rtt is not None, "ping di 127.0.0.1 senza risposta"
        server.close()
        window.open_network_tools()
        wait(app, lambda: False, 0.5)
        window._net_tools.grab().save(str(report_path.with_name("network-tools.png")))
        window._net_tools.close()
        return f"TFTP, HTTP, scansione porte, {len(listening)} porte in ascolto, ping {rtt:.0f} ms"

    check("Strumenti di rete", tools)

    # ------------------------------------------------------------------ SSH, SFTP, tunnel (server vero)
    target = os.environ.get("FEDTERMHUB_SELFTEST_SSH", "")
    if not target:
        return
    host, port, user, password = (target.split(";", 3) + ["", "", "", ""])[:4]

    def ssh_session() -> str:
        sess = model.Session(name="ssh-prova", protocol="ssh", host=host, port=port, user=user)
        window.settings.show_remote_files = True
        window.identity = lambda s, _orig=window.identity: (user, "", password) if s.name == "ssh-prova" else _orig(s)
        tab = terminal_tab(sess)
        out = collect(tab)
        assert wait(app, lambda: tab.view.ready, 20), "terminale non pronto"
        assert wait(app, lambda: not tab.feeder.armed and tab.running, 30), \
            "password non scritta: " + "".join(out)[-300:]
        wait(app, lambda: False, 3)
        tab.view.on_input("echo FTH_SSH_$((6*7))_OK\r")
        ok = wait(app, lambda: "FTH_SSH_42_OK" in "".join(out) or "FTH_SSH_$((6*7))_OK" in "".join(out).replace("echo FTH", ""), 20)
        assert ok, "nessuna risposta dalla shell remota: " + "".join(out)[-300:]
        # Pannello dei file accanto al terminale
        assert wait(app, lambda: tab.files is not None and tab.files.connected, 30), \
            "pannello file non collegato: " + (tab.files.state.text() if tab.files else "non aperto")
        assert wait(app, lambda: tab.files.model.rowCount() > 0 or tab.files.cwd, 15), "cartella remota vuota"
        followed = ""
        if not re.match(r"^/?[A-Za-z]:", tab.files.home):
            # Server Unix: la shell dichiara la cartella (OSC 7) e il pannello la segue dopo un cd.
            assert wait(app, lambda: bool(tab.view.last_cwd), 15), "la shell non dichiara la cartella (OSC 7)"
            tab.view.on_input("cd /tmp\r")
            assert wait(app, lambda: tab.files.cwd == "/tmp", 15), f"il pannello non segue il cd: {tab.files.cwd}"
            followed = ", segue il cd"
        window.grab().save(str(report_path.with_name("ssh-session.png")))
        close(tab)
        return f"{user}@{host}:{port}, password scritta al prompt, pannello file in {tab.files.home}{followed}"

    check("SSH con password", ssh_session)

    def sftp() -> str:
        from .sftp_client import SftpTarget

        panel = FilesPanel(SftpTarget(host=host, port=int(port or 22), user=user, password=password, strict="accept-new"))
        panel.connect_server()
        assert wait(app, lambda: panel.connected and panel.cwd, 30), "SFTP non collegato: " + panel.state.text()
        local = Path(tempfile.mkdtemp(prefix="fth-sftp-")) / "fth-prova.bin"
        local.write_bytes(os.urandom(200_000))
        panel.upload([local])
        assert wait(app, lambda: "Caricamento completato" in panel.state.text() or "completat" in panel.state.text(), 30), \
            "caricamento: " + panel.state.text()
        assert wait(app, lambda: any(panel.model.item(r, 0).text() == local.name for r in range(panel.model.rowCount())), 10), \
            "file caricato non elencato"
        remote = panel.cwd.rstrip("/") + "/" + local.name
        back = local.with_name("ritorno.bin")
        done: list = []
        panel.worker.done.connect(lambda tag, _r: done.append(tag))
        panel.worker.run("download", lambda s: s.download(remote, back, lambda *_: None))
        assert wait(app, lambda: "download" in done, 30), "download non finito: " + panel.state.text()
        assert back.read_bytes() == local.read_bytes(), "il file scaricato è diverso"
        panel.worker.run("delete", lambda s: s.remove(remote))
        wait(app, lambda: "delete" in done, 15)
        panel.shutdown()
        return f"caricati e riscaricati {local.stat().st_size} byte in {panel.cwd}"

    check("SFTP", sftp)

    def tunnel() -> str:
        server = selftest_servers.start_telnet()
        local_port = selftest_servers.free_port()
        tunnel = model.TunnelSession(name="tunnel-prova", ssh_host=host, ssh_port=port or "22", ssh_user=user,
                                     local_port=str(local_port), remote_host="127.0.0.1", remote_port=str(server.port))
        window.tunnels.store.upsert(tunnel)
        window.tunnels.identity = lambda t: (user, password)
        result: list = []
        window.tunnels.when_ready(tunnel.id, lambda ok, log: result.append((ok, log)))
        assert wait(app, lambda: result, 40), "il tunnel non risponde"
        assert result[0][0], "tunnel non avviato: " + result[0][1]
        import socket

        with socket.create_connection(("127.0.0.1", local_port), timeout=5) as sock:
            sock.settimeout(5)
            data = b""
            deadline = time.monotonic() + 5
            while b"login:" not in data and time.monotonic() < deadline:
                data += sock.recv(1024)
        assert b"login:" in data, f"nessun dato attraverso il tunnel: {data!r}"
        window.tunnels.stop(tunnel.id)
        server.close()
        return f"127.0.0.1:{local_port} → {host} → 127.0.0.1:{server.port}"

    check("Tunnel SSH", tunnel)
