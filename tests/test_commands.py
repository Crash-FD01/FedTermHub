#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Nucleo comune: comandi ssh e tunnel (commands.py), strumenti di rete (netcore.py), aspetto (look.py) e
parità fra le due interfacce (docs/PARITA.md).

Gira senza GTK e senza Qt. Controlla anche che le due interfacce usino davvero il nucleo comune: se
qualcuno ricostruisce un comando ssh dentro fedtermhub.py o dentro qtui/, le due versioni potrebbero
tornare a comportarsi in modo diverso.
"""
from __future__ import annotations

import os
import re
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("FEDTERMHUB_LANGUAGE", "it")

from fedtermhub import commands, look, model, netcore, termcore  # noqa: E402


def settings(**kw) -> model.AppSettings:  # noqa: ANN003
    s = model.AppSettings()
    for key, value in kw.items():
        setattr(s, key, value)
    return s


def check_ssh() -> None:
    st = settings(connection_timeout_seconds=9, ssh_keepalive_interval=20, ssh_keepalive_count=4,
                  ssh_strict_host_key="accept-new", ssh_compression=True)
    common = commands.ssh_common_options(st)
    assert common == ["-o", "ConnectTimeout=9", "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=4",
                      "-o", "StrictHostKeyChecking=accept-new", "-C"], common
    assert "StrictHostKeyChecking" not in " ".join(commands.ssh_common_options(settings(ssh_strict_host_key="ask")))

    sess = model.Session(name="s", protocol="ssh", host="web01", port="2222", user="", command="uptime")
    argv = commands.ssh_session_argv("ssh", sess, "admin", ["-o", "X=1"])
    assert argv == ["ssh", "-o", "X=1", "-p", "2222", "--", "admin@web01", "uptime"], argv
    sftp = commands.ssh_session_argv("sftp", sess, "admin", [], port_flag="-P", with_command=False)
    assert sftp == ["sftp", "-P", "2222", "--", "admin@web01"], sftp

    evil = model.Session(name="x", protocol="ssh", host="-oProxyCommand=calc")
    assert commands.ssh_session_argv("ssh", evil, "", [])[-2:] == ["--", "-oProxyCommand=calc"]

    jump = model.Session(name="j", protocol="ssh", host="db", routing_mode="proxy_jump", jump_host="fe80::1",
                         jump_user="ops", jump_port="2200")
    assert commands.proxy_jump_spec(jump) == "ops@[fe80::1]:2200"
    assert commands.ssh_session_argv("ssh", jump, "", [])[1:3] == ["-J", "ops@[fe80::1]:2200"]
    incomplete = model.Session(name="j", protocol="ssh", host="db", routing_mode="proxy_jump")
    try:
        commands.ssh_session_argv("ssh", incomplete, "", [])
    except commands.CommandError as exc:
        assert "Jump host" in exc.title
    else:
        raise AssertionError("jump host vuoto accettato")


def check_tunnels() -> None:
    st = settings(connection_timeout_seconds=7, ssh_keepalive_interval=60)
    t = model.TunnelSession(name="t", ssh_host="bastion", ssh_port="22", ssh_user="ops", local_port="15432",
                            remote_host="db", remote_port="5432", server_alive_interval=15, server_alive_count=2)
    argv = commands.tunnel_argv("ssh", t, st, "ops", has_password=False)
    joined = " ".join(argv)
    assert argv[:3] == ["ssh", "-N", "-T"] and argv[-2:] == ["--", "ops@bastion"], argv
    assert "-L 127.0.0.1:15432:db:5432" in joined and "BatchMode=yes" in joined
    # ssh usa il primo valore: il keepalive del tunnel deve venire prima di quello generale
    assert joined.index("ServerAliveInterval=15") < joined.index("ServerAliveInterval=60"), joined
    assert "BatchMode" not in " ".join(commands.tunnel_argv("ssh", t, st, "ops", has_password=True))
    t.tunnel_type = "dynamic"
    assert "-D 127.0.0.1:15432" in " ".join(commands.tunnel_argv("ssh", t, st, "", False))
    t.tunnel_type = "remote"
    assert "-R 127.0.0.1:15432:db:5432" in " ".join(commands.tunnel_argv("ssh", t, st, "", False))
    probe = commands.tunnel_probe_argv("ssh", t, st, "ops", False)
    assert probe[-3:] == ["--", "ops@bastion", "echo FEDTERMHUB_TUNNEL_OK"], probe

    bad = model.TunnelSession(name="b", ssh_host="-oProxyCommand=x", ssh_port="99999", local_port="",
                              remote_host="", remote_port="x", options="'non chiusa", identity_file="/non/esiste")
    problems = commands.tunnel_problems(bad)
    assert len(problems) >= 6, problems
    try:
        commands.tunnel_argv("ssh", bad, st, "", False)
    except commands.CommandError:
        pass
    else:
        raise AssertionError("tunnel non valido accettato")

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        assert commands.bind_problem("127.0.0.1", port), "porta occupata non segnalata"
    assert commands.bind_problem("127.0.0.1", port) == ""


def check_netcore() -> None:
    assert netcore.parse_ports("22, 80-82 443") == [22, 80, 81, 82, 443]
    try:
        netcore.wake_on_lan("00:11:22:33:44", "127.0.0.1")
    except ValueError:
        pass
    else:
        raise AssertionError("MAC corto accettato")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(2)
        netcore.wake_on_lan("aa-bb-cc-dd-ee-ff", "127.0.0.1", receiver.getsockname()[1], repeat=2)
        packet = receiver.recv(200)
        assert packet == b"\xff" * 6 + bytes.fromhex("aabbccddeeff") * 16
    folder = Path(tempfile.mkdtemp())
    (folder / "a.cfg").write_bytes(b"x" * 1500)
    log: list = []
    server = netcore.TftpServer(folder, 0, log.append, bind="127.0.0.1")
    try:
        assert netcore.tftp_get("127.0.0.1", server.port, "a.cfg") == b"x" * 1500
        try:
            netcore.tftp_get("127.0.0.1", server.port, "../../etc/passwd")
        except OSError:
            pass
        else:
            raise AssertionError("TFTP fuori dalla cartella")
    finally:
        server.stop()
    assert netcore.local_networks(), "nessuna rete locale"


def check_both_interfaces_use_the_core() -> None:
    """Le due interfacce non devono ricostruire per conto loro i comandi che stanno nel nucleo comune."""
    gtk = (ROOT / "fedtermhub" / "fedtermhub.py").read_text(encoding="utf-8")
    qt = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "fedtermhub" / "qtui").glob("*.py"))
    for name, source in (("GTK", gtk), ("Qt", qt)):
        for function in ("commands.ssh_session_argv", "commands.tunnel_argv", "commands.proxy_jump_spec"):
            assert function in source, f"{name}: non usa {function}"
        assert not re.search(r'\[\s*"-L"\s*,', source), f"{name}: costruisce da sé un -L"
        assert not re.search(r'f"ConnectTimeout=\{', source.replace("commands.py", "")), f"{name}: opzioni ssh duplicate"
    gtk_tools = (ROOT / "fedtermhub" / "network_tools.py").read_text(encoding="utf-8")
    assert "netcore.wake_on_lan" in gtk_tools and "net_core.wake_on_lan" in qt


def check_look() -> None:
    """I colori della finestra Qt (look.py) sono quelli del foglio di stile GTK: le due finestre si somigliano."""
    gtk = (ROOT / "fedtermhub" / "fedtermhub.py").read_text(encoding="utf-8")
    dark = gtk[gtk.index("restyle 3.21: palette decisa, testo sempre chiaro"):]
    dark = dark[:dark.index('"""')]
    light = gtk[gtk.index("restyle 3.21: palette decisa, testo sempre scuro"):]
    light = light[:light.index('"""')]
    for name, css, colors in (("scuro", dark, look.WINDOW_DARK), ("chiaro", light, look.WINDOW_LIGHT)):
        for key in ("root", "bar", "rail", "sidebar", "workspace", "line", "text", "tree_text", "caption",
                    "status_text", "entry", "entry_line", "focus", "selected", "selected_text", "hover",
                    "tab_text", "tab_hover"):
            assert colors[key].lower() in css.lower(), f"tema {name}: {key} {colors[key]} non è nel foglio di stile GTK"
    for key, color in (("dark", "#79c7ff"), ("light", "#0878b8")):
        assert look.window_colors(key == "dark")["version"] == color and f".footer-version {{ color: {color}; }}" in gtk
    # icone e cartelle: la versione Linux le prende da look.py, non da una copia sua
    assert "look.colored_svg(" in gtk and "look.folder_svg(" in gtk and "look.HOME_SESSION_SVG" in gtk
    assert "proto_colors = {" not in gtk, "colori delle icone ricopiati in fedtermhub.py"
    panel = (ROOT / "fedtermhub" / "terminal_panel.py").read_text(encoding="utf-8")
    assert "look.TERMINAL_PALETTE" in panel and len(look.TERMINAL_PALETTE) == 16
    qt_theme = (ROOT / "fedtermhub" / "qtui" / "theme.py").read_text(encoding="utf-8")
    assert "look.TERMINAL_PALETTE" in qt_theme and "look.window_colors" in qt_theme
    assert look.icon_color("fth-proto-ssh", True) == "#22d3ee" and look.icon_color("sconosciuta", False) == "#3a4653"
    assert 'fill="#53b175"' in look.folder_svg("#53b175")


def _gtk_menu_entries() -> list:
    source = (ROOT / "fedtermhub" / "fedtermhub.py").read_text(encoding="utf-8")
    block = source[source.index('menubar.append(self.make_menu(tr("Terminale")'):]
    block = block[:block.index("root.pack_start(menubar")]
    entries = []
    for line in block.splitlines():
        if "make_menu(" in line:
            continue
        match = re.match(r'\s*\((?:tr\()?"([^"]+)"\)?,', line)
        if match and match.group(1) != "---":
            entries.append(match.group(1))
    return entries


def check_parity_doc() -> None:
    """docs/PARITA.md elenca tutte le voci dei menu Linux, e le voci dei menu segnate «sì» esistono davvero
    nell'interfaccia di quel sistema (le righe di «Altro» sono descrizioni e non si controllano)."""
    doc = (ROOT / "docs" / "PARITA.md").read_text(encoding="utf-8")
    rows = {}
    for line in doc.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 4 and cells[1] in {"sì", "no", "—"}:
            assert cells[2] in {"sì", "no", "—"}, line
            assert cells[0] not in rows, f"voce ripetuta: {cells[0]}"
            rows[cells[0]] = (cells[1], cells[2])
    entries = _gtk_menu_entries()
    assert len(entries) > 50, entries
    missing = [e for e in entries if e not in rows]
    assert not missing, f"voci dei menu Linux che mancano in docs/PARITA.md: {missing}"
    gtk = (ROOT / "fedtermhub" / "fedtermhub.py").read_text(encoding="utf-8")
    qt = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "fedtermhub" / "qtui").glob("*.py"))
    for name, (linux, windows) in rows.items():
        if linux == "sì" and name in entries:
            assert f'"{name}"' in gtk, f"{name}: segnata «sì» per Linux ma non c'è in fedtermhub.py"
        if windows == "sì" and name in entries:
            assert f'"{name}"' in qt or f"'{name}'" in qt, f"{name}: segnata «sì» per Windows ma non c'è in qtui/"


def check_panes() -> None:
    """Riquadri e MultiExec: stesse regole sulle due interfacce (termcore.py)."""
    assert termcore.next_layout("grid") == "stack" and termcore.next_layout("stack") == "tall"
    assert termcore.layout_after_add("stack", 2) == "tall" and termcore.layout_after_add("grid", 3) == "grid"
    assert [termcore.grid_shape(n) for n in (1, 2, 3, 4, 5, 9)] == [(1, 1), (2, 1), (2, 2), (2, 2), (3, 2), (3, 3)]
    assert termcore.layout_plan("tall", 1) == 0 and termcore.layout_plan("stack", 3, active=2) == 2
    assert termcore.layout_plan("tall", 2) == ("h", [0, 1])
    assert termcore.layout_plan("tall", 3) == ("h", [0, ("v", [1, 2])])
    assert termcore.layout_plan("fat", 3) == ("v", [0, ("h", [1, 2])])
    assert termcore.layout_plan("grid", 2) == ("h", [0, 1])
    assert termcore.layout_plan("grid", 3) == ("v", [("h", [0, 1]), 2])
    assert termcore.layout_plan("grid", 4) == ("v", [("h", [0, 1]), ("h", [2, 3])])
    # broadcast: testo e tasti semplici sì; frecce, Ctrl+tasto e risposte del terminale no
    for data in ("a", "uptime", "è", "\r", "\x7f", "\t", "\x1b"):
        assert termcore.broadcastable(data), repr(data)
    for data in ("", "\x03", "\x1b[A", "\x1b[12;5R", "\x1b[?1;2c", "\x1b[<0;3;4M", "a\x03"):
        assert not termcore.broadcastable(data), repr(data)
    # MultiExec: spuntate le SSH; se non ce n'è nessuna, tutte
    assert termcore.multiexec_target("ssh") == ("SSH", True)
    assert termcore.multiexec_target("telnet") == ("TELNET", False)
    assert termcore.multiexec_target("local") == ("terminale locale", False)
    assert termcore.multiexec_target("tool") == ("strumento", False)
    assert termcore.multiexec_target("local", split=True) == ("terminale diviso", False)
    mixed = [("a", "A", "SSH", True), ("b", "B", "TELNET", False)]
    assert termcore.multiexec_defaults(mixed) == mixed
    assert [t[3] for t in termcore.multiexec_defaults([("b", "B", "TELNET", False)])] == [True]
    assert termcore.multiexec_line("uptime") == "uptime\n" and termcore.multiexec_line("uptime", "\r") == "uptime\r"
    # le due interfacce usano il nucleo e i tasti della tabella sono quelli della versione Linux
    gtk = (ROOT / "fedtermhub" / "fedtermhub.py").read_text(encoding="utf-8")
    panel = (ROOT / "fedtermhub" / "terminal_panel.py").read_text(encoding="utf-8")
    qt = (ROOT / "fedtermhub" / "qtui" / "main_window.py").read_text(encoding="utf-8")
    qt_tab = (ROOT / "fedtermhub" / "qtui" / "terminal_tab.py").read_text(encoding="utf-8")
    for function in ("multiexec_target", "multiexec_defaults", "multiexec_line"):
        assert f"termcore.{function}" in gtk and f"termcore.{function}" in qt, function
    assert "termcore.next_layout" in panel and "termcore.next_layout" in qt_tab
    assert "termcore.PANE_KEYS" in qt and "termcore.broadcastable" in qt_tab
    for letter, action in (("o", "split_v"), ("e", "split_h"), ("w", "close_pane")):
        assert f'lower == "{letter}":\n            self._request("{action}")' in panel, action
        assert f"Ctrl+Shift+{letter.upper()}" in termcore.PANE_KEYS[action]
    for letter, action in (("l", "cycle_layout"), ("p", "toggle_broadcast")):
        assert f'lower == "{letter}":\n            self.dispatch("{action}", pane)' in panel, action
        assert f"Ctrl+Shift+{letter.upper()}" in termcore.PANE_KEYS[action]
    assert all(color in gtk for color in look.TERMINAL_TOAST.values()), "avviso del terminale diverso dal CSS GTK"


def check_tunnel_rules() -> None:
    """Stato, percorso, copia e sessione collegata dei tunnel: stesse regole sulle due interfacce."""
    assert commands.tunnel_state_display("running") == ("● Attivo", "#49d17d")
    assert commands.tunnel_state_display("waiting")[0] == commands.tunnel_state_display("restarting")[0]
    assert commands.tunnel_route("remote")["arrow"] == "←" and commands.tunnel_route("local")["arrow"] == "→"
    tunnel = model.TunnelSession(name="rdp", ssh_host="bastion", ssh_user="ops", local_port="13389",
                                 remote_host="10.20.0.5", remote_port="3389", auto_start=True)
    assert commands.tunnel_route_details(tunnel) == ("127.0.0.1:13389", "ops@bastion:22", "10.20.0.5:3389")
    clone = tunnel.duplicate()
    assert clone.id != tunnel.id and not clone.auto_start and clone.name == "rdp copia" and clone.local_port == "13389"
    draft = tunnel.linked_session()
    assert (draft.protocol, draft.host, draft.port, draft.required_tunnel_id) == ("rdp", "127.0.0.1", "13389", tunnel.id)
    for source in ("fedtermhub.py", "qtui/tunnels.py"):
        text = (ROOT / "fedtermhub" / source).read_text(encoding="utf-8")
        for function in ("commands.tunnel_route(", "commands.tunnel_route_details(", "commands.tunnel_state_display("):
            assert function in text, f"{source}: non usa {function}"
        assert ".duplicate()" in text


def main() -> int:
    for check in (check_tunnel_rules, check_ssh, check_tunnels, check_netcore, check_both_interfaces_use_the_core, check_look,
                  check_panes, check_parity_doc):
        check()
        print(f"OK {check.__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
