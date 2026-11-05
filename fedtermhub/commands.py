# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Comandi OpenSSH di FedTermHub, uguali per le due interfacce (GTK su Linux, Qt su Windows).

Qui si decide quali opzioni riceve ssh, come si scrive la destinazione, come si costruisce un tunnel e
quando un tunnel non è valido. Le interfacce aggiungono solo quello che è proprio della piattaforma: sshpass
e il multiplexing (ControlMaster) su Linux, la password scritta al prompt su Windows.

Niente GTK, niente Qt: si prova con tests/test_commands.py.
"""
from __future__ import annotations

import os
import shlex
import socket
from pathlib import Path
from typing import List, Optional

try:
    from .i18n import tr
except ImportError:  # avvio come script (fedtermhub/fedtermhub.py) o dai test
    from i18n import tr  # type: ignore[no-redef]


class CommandError(ValueError):
    """Il comando non si può costruire: titolo per l'utente e suggerimento."""

    def __init__(self, title: str, hint: str = "") -> None:
        super().__init__(title)
        self.title = title
        self.hint = hint


def ssh_common_options(settings) -> List[str]:  # noqa: ANN001
    """Opzioni OpenSSH comuni a SSH, SFTP e tunnel: timeout, keepalive, chiave dell'host, compressione."""
    options: List[str] = ["-o", f"ConnectTimeout={settings.connection_timeout_seconds}"]
    if settings.ssh_keepalive_interval > 0:
        options += ["-o", f"ServerAliveInterval={settings.ssh_keepalive_interval}",
                    "-o", f"ServerAliveCountMax={settings.ssh_keepalive_count}"]
    if settings.ssh_strict_host_key in {"accept-new", "yes", "no"}:
        options += ["-o", f"StrictHostKeyChecking={settings.ssh_strict_host_key}"]
    if settings.ssh_compression:
        options.append("-C")
    return options


def proxy_jump_spec(sess) -> str:  # noqa: ANN001
    """Il valore di -J per una sessione con jump host: [utente@]host[:porta], IPv6 tra parentesi."""
    if getattr(sess, "routing_mode", "direct") != "proxy_jump":
        return ""
    host = str(getattr(sess, "jump_host", "") or "").strip()
    if not host:
        return ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    user = str(getattr(sess, "jump_user", "") or "").strip()
    port = str(getattr(sess, "jump_port", "22") or "22").strip()
    endpoint = f"{user}@{host}" if user else host
    if port and port != "22":
        endpoint += f":{port}"
    return endpoint


def ssh_target(user: str, host: str) -> str:
    return f"{user}@{host}" if user else host


def ssh_session_argv(binary: str, sess, user: str, options: List[str], *, port_flag: str = "-p",  # noqa: ANN001
                     with_command: bool = True) -> List[str]:
    """Il comando per una sessione SSH (o SFTP con port_flag="-P"): opzioni, jump host, porta, destinazione.

    "--" prima della destinazione: un host o un utente che comincia con "-" (da un file di sessioni importato)
    non deve diventare un'opzione come -oProxyCommand=...
    """
    jump = proxy_jump_spec(sess)
    if getattr(sess, "routing_mode", "direct") == "proxy_jump" and not jump:
        raise CommandError(tr("Jump host incompleto"),
                           tr("Compila il server jump host nella sezione Instradamento avanzato."))
    argv = [binary] + list(options)
    if jump:
        argv += ["-J", jump]
    if sess.port:
        argv += [port_flag, sess.port]
    argv += ["--", ssh_target(user, sess.host)]
    if with_command and getattr(sess, "command", ""):
        argv.append(sess.command)
    return argv


def valid_port(value: str) -> bool:
    text = str(value or "").strip()
    return text.isdigit() and 0 < int(text) < 65536


def tunnel_problems(tunnel, check_files: bool = True) -> List[str]:  # noqa: ANN001
    """Cosa impedisce di avviare il tunnel (vuoto se va bene)."""
    errors: List[str] = []
    if tunnel.tunnel_type not in {"local", "remote", "dynamic"}:
        errors.append(tr("tipo di tunnel non valido"))
    host = tunnel.ssh_host.strip()
    if not host:
        errors.append(tr("host SSH mancante"))
    elif host.startswith("-") or any(c.isspace() for c in host):
        errors.append(tr("host SSH «{host}» non valido").format(host=host))
    if tunnel.ssh_user.strip().startswith("-"):
        errors.append(tr("utente «{user}» non valido").format(user=tunnel.ssh_user))
    if not valid_port(tunnel.ssh_port):
        errors.append(tr("porta SSH non valida"))
    if not tunnel.bind_address.strip():
        errors.append(tr("indirizzo di bind mancante"))
    if not valid_port(tunnel.local_port):
        errors.append(tr("porta di inoltro non valida"))
    if tunnel.tunnel_type != "dynamic":
        if not tunnel.remote_host.strip() or tunnel.remote_host.strip().startswith("-"):
            errors.append(tr("host destinazione mancante"))
        if not valid_port(tunnel.remote_port):
            errors.append(tr("porta destinazione non valida"))
    if check_files and tunnel.identity_file:
        if not Path(os.path.expanduser(tunnel.identity_file)).is_file():
            errors.append(tr("chiave privata non trovata"))
    try:
        shlex.split(tunnel.options)
    except ValueError as exc:
        errors.append(tr("opzioni SSH non valide: {exc}").format(exc=exc))
    return errors


def bind_problem(address: str, port: int) -> str:
    """"" se la porta locale del tunnel è libera, altrimenti il motivo."""
    address = address.strip() or "127.0.0.1"
    try:
        infos = socket.getaddrinfo(address, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
                                   flags=socket.AI_PASSIVE)
    except OSError as exc:
        return tr("indirizzo di bind non valido: {exc}").format(exc=exc)
    last_error: Optional[BaseException] = None
    for family, socktype, proto, _canon, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            if os.name != "nt":  # su Windows SO_REUSEADDR permetterebbe di rubare una porta già in uso
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(sockaddr)
            return ""
        except OSError as exc:
            last_error = exc
        finally:
            sock.close()
    return tr("porta {address}:{port} non disponibile: {v}").format(address=address, port=port,
                                                                    v=last_error or tr("errore sconosciuto"))


def tunnel_argv(binary: str, tunnel, settings, user: str, has_password: bool) -> List[str]:  # noqa: ANN001
    """Il comando ssh -N del tunnel.

    Le opzioni del tunnel (keepalive, uscita se l'inoltro fallisce) vengono prima di quelle generali: ssh usa
    il primo valore di ogni opzione, e quelle del tunnel sono più specifiche.
    """
    problems = tunnel_problems(tunnel)
    if problems:
        raise CommandError("; ".join(problems))
    argv = [binary, "-N", "-T", "-o", "ControlMaster=no", "-o", "NumberOfPasswordPrompts=1"]
    if tunnel.exit_on_forward_failure:
        argv += ["-o", "ExitOnForwardFailure=yes"]
    if tunnel.server_alive_interval > 0:
        argv += ["-o", f"ServerAliveInterval={tunnel.server_alive_interval}",
                 "-o", f"ServerAliveCountMax={tunnel.server_alive_count}"]
    argv += ssh_common_options(settings)
    if tunnel.compression and "-C" not in argv:
        argv.append("-C")
    if tunnel.address_family == "ipv4":
        argv.append("-4")
    elif tunnel.address_family == "ipv6":
        argv.append("-6")
    if tunnel.verbose_log:
        argv.append("-v")
    argv += ["-p", tunnel.ssh_port or "22"]
    if tunnel.identity_file:
        argv += ["-i", os.path.expanduser(tunnel.identity_file)]
    listen = f"{tunnel.bind_address or '127.0.0.1'}:{tunnel.local_port}"
    if tunnel.tunnel_type == "dynamic":
        argv += ["-D", listen]
    elif tunnel.tunnel_type == "remote":
        argv += ["-R", f"{listen}:{tunnel.remote_host}:{tunnel.remote_port}"]
    else:
        argv += ["-L", f"{listen}:{tunnel.remote_host}:{tunnel.remote_port}"]
    if tunnel.options:
        argv += shlex.split(tunnel.options)
    if not has_password:
        argv += ["-o", "BatchMode=yes"]
    return argv + ["--", ssh_target(user, tunnel.ssh_host)]


def tunnel_probe_argv(binary: str, tunnel, settings, user: str, has_password: bool) -> List[str]:  # noqa: ANN001
    """ssh che si collega e stampa FEDTERMHUB_TUNNEL_OK, senza aprire porte: prova accesso e raggiungibilità."""
    problems = tunnel_problems(tunnel)
    if problems:
        raise CommandError("; ".join(problems))
    argv = [binary, "-T", "-o", "ControlMaster=no", "-o", "NumberOfPasswordPrompts=1",
            "-o", f"ConnectTimeout={max(3, settings.connection_timeout_seconds)}"]
    argv += ssh_common_options(settings)
    argv += ["-p", tunnel.ssh_port or "22"]
    if tunnel.address_family == "ipv4":
        argv.append("-4")
    elif tunnel.address_family == "ipv6":
        argv.append("-6")
    if tunnel.identity_file:
        argv += ["-i", os.path.expanduser(tunnel.identity_file)]
    if tunnel.verbose_log:
        argv.append("-v")
    if not has_password:
        argv += ["-o", "BatchMode=yes"]
    return argv + ["--", ssh_target(user, tunnel.ssh_host), "printf FEDTERMHUB_TUNNEL_OK"]
