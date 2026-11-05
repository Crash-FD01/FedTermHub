#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Nucleo comune: comandi ssh e tunnel (commands.py), strumenti di rete (netcore.py) e parità fra interfacce.

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

from fedtermhub import commands, model, netcore  # noqa: E402


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
    assert probe[-3:] == ["--", "ops@bastion", "printf FEDTERMHUB_TUNNEL_OK"], probe

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


def main() -> int:
    for check in (check_ssh, check_tunnels, check_netcore, check_both_interfaces_use_the_core):
        check()
        print(f"OK {check.__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
