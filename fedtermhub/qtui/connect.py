# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Come si apre una sessione nell'interfaccia Qt: i comandi da lanciare, senza widget.

Su Windows SSH usa il client OpenSSH del sistema (ssh.exe, presente da Windows 10 1809); la shell
locale è PowerShell. Su Linux gli stessi comandi della versione GTK.

La password salvata non passa sulla riga di comando: PasswordFeeder la scrive nel terminale quando
ssh la chiede, come fa sshpass su Linux (che su Windows non esiste).
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .. import model
from ..i18n import tr

IS_WINDOWS = sys.platform == "win32"


@dataclass
class Launch:
    """Un programma da avviare in una scheda terminale."""

    title: str
    argv: List[str]
    env: Dict[str, str]
    password: Optional[str] = None  # da scrivere al prompt della password (PasswordFeeder)
    close_on_success: bool = True


class LaunchError(Exception):
    """La sessione non si può aprire: titolo per l'utente e suggerimento."""

    def __init__(self, title: str, hint: str = "") -> None:
        super().__init__(title)
        self.title = title
        self.hint = hint


def _windows_system_binary(name: str) -> Optional[str]:
    """Un programma di sistema di Windows, cercato anche dove un Python a 32 bit non guarderebbe."""
    found = shutil.which(name)
    if found:
        return found
    root = os.environ.get("SystemRoot", r"C:\Windows")
    for folder in ("System32", "Sysnative"):
        for sub in ("", "OpenSSH", r"WindowsPowerShell\v1.0"):
            candidate = Path(root) / folder / sub / f"{name}.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def local_shell(settings: model.AppSettings) -> Launch:
    """La shell locale: PowerShell (7 se c'è) su Windows, la shell dell'utente su Linux."""
    configured = (settings.terminal_shell or "").strip()
    if IS_WINDOWS:
        if configured and shutil.which(configured):
            return Launch(tr("Terminale locale"), [configured], {}, close_on_success=True)
        pwsh = shutil.which("pwsh")
        if pwsh:
            return Launch("PowerShell", [pwsh, "-NoLogo"], {})
        powershell = _windows_system_binary("powershell")
        if powershell:
            return Launch("PowerShell", [powershell, "-NoLogo"], {})
        return Launch(tr("Prompt dei comandi"), [os.environ.get("COMSPEC", "cmd.exe")], {})
    shell = configured if configured and Path(configured).exists() else (os.environ.get("SHELL") or "/bin/bash")
    return Launch(tr("Terminale locale"), [shell, "-l"], {})


def ssh_binary() -> Optional[str]:
    return _windows_system_binary("ssh") if IS_WINDOWS else shutil.which("ssh")


def proxy_jump_spec(sess: model.Session) -> str:
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


def ssh_options(settings: model.AppSettings, with_password: bool) -> List[str]:
    """Opzioni OpenSSH come nella versione GTK (timeout, keepalive, controllo della chiave dell'host)."""
    options = ["-o", f"ConnectTimeout={settings.connection_timeout_seconds}"]
    if settings.ssh_keepalive_interval > 0:
        options += ["-o", f"ServerAliveInterval={settings.ssh_keepalive_interval}",
                    "-o", f"ServerAliveCountMax={settings.ssh_keepalive_count}"]
    if settings.ssh_strict_host_key in {"accept-new", "yes", "no"}:
        options += ["-o", f"StrictHostKeyChecking={settings.ssh_strict_host_key}"]
    if settings.ssh_compression:
        options.append("-C")
    if with_password:
        # Una sola richiesta: se la password salvata è sbagliata ssh si ferma invece di riproporla
        # (PasswordFeeder la scrive una volta sola).
        options += ["-o", "NumberOfPasswordPrompts=1"]
    return options


def ssh(sess: model.Session, settings: model.AppSettings, user: str, password: Optional[str]) -> Launch:
    binary = ssh_binary()
    if not binary:
        if IS_WINDOWS:
            raise LaunchError(
                tr("Client OpenSSH di Windows non installato"),
                tr("Impostazioni › Sistema › Funzionalità facoltative › Aggiungi «Client OpenSSH», oppure da "
                   "PowerShell come amministratore: Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0"),
            )
        raise LaunchError(tr("Client SSH non trovato"), tr("Installa il pacchetto openssh-client(s)."))
    if not sess.host:
        raise LaunchError(tr("Host SSH mancante."))
    argv = [binary] + ssh_options(settings, bool(password))
    jump = proxy_jump_spec(sess)
    if getattr(sess, "routing_mode", "direct") == "proxy_jump" and not jump:
        raise LaunchError(tr("Jump host incompleto"), tr("Compila il server jump host nella sezione Instradamento avanzato."))
    if jump:
        argv += ["-J", jump]
    if sess.port:
        argv += ["-p", sess.port]
    target = f"{user}@{sess.host}" if user else sess.host
    # "--": un host o un utente che comincia con "-" non deve diventare un'opzione di ssh.
    argv += ["--", target]
    if sess.command:
        argv.append(sess.command)
    return Launch(sess.name or target, argv, {}, password=password)


def command(sess: model.Session, settings: model.AppSettings) -> Launch:
    if not sess.command:
        raise LaunchError(tr("Comando mancante."))
    if IS_WINDOWS:
        shell = local_shell(settings).argv[0]
        if shell.lower().endswith(("cmd.exe", "cmd")):
            return Launch(sess.name or tr("Comando"), [shell, "/d", "/k", sess.command], {})
        return Launch(sess.name or tr("Comando"), [shell, "-NoLogo", "-NoExit", "-Command", sess.command], {})
    shell = os.environ.get("SHELL") or "/bin/sh"
    return Launch(sess.name or tr("Comando"), [shell, "-lc", sess.command], {})


def telnet(sess: model.Session) -> Launch:
    binary = _windows_system_binary("telnet") if IS_WINDOWS else shutil.which("telnet")
    if not binary:
        if IS_WINDOWS:
            raise LaunchError(
                tr("Client Telnet non installato"),
                tr("Da PowerShell come amministratore: Enable-WindowsOptionalFeature -Online -FeatureName TelnetClient"),
            )
        raise LaunchError(tr("Client Telnet non installato"))
    # Il telnet di Windows non conosce "--": l'host è già controllato da session_problem().
    argv = [binary, sess.host, sess.port or "23"] if IS_WINDOWS else [binary, "--", sess.host, sess.port or "23"]
    return Launch(sess.name or tr("Telnet {host}").format(host=sess.host), argv, {})


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
# Solo la password del server: la passphrase di una chiave («Enter passphrase for key») è un'altra cosa.
_PROMPT = re.compile(r"(?i)(?:password|passwort|mot de passe|contraseña)[^\n]{0,60}:\s*$")


class PasswordFeeder:
    """Scrive la password salvata quando ssh la chiede, una volta sola (come sshpass).

    Guarda solo la fine dell'output: un «password:» alla fine della riga, entro il primo minuto
    dall'avvio. Dopo averla scritta (o se compare «Permission denied») non fa più nulla, così non può
    finire in un'altra richiesta. Le domande prima (per esempio la conferma della chiave dell'host) le
    risponde l'utente nel terminale.
    """

    def __init__(self, password: Optional[str], timeout: float = 60.0) -> None:
        self.password = password or ""
        self.armed = bool(self.password)
        self.deadline = time.monotonic() + timeout
        self._tail = ""

    def disarm(self) -> None:
        self.armed = False
        self.password = ""

    def feed(self, text: str) -> Optional[str]:
        """Il testo da mandare al programma (password + Invio) se l'output appena arrivato la chiede."""
        if not self.armed:
            return None
        if time.monotonic() > self.deadline:
            self.disarm()
            return None
        self._tail = (self._tail + _ANSI.sub("", text))[-400:]
        if "Permission denied" in self._tail or "Too many authentication failures" in self._tail:
            self.disarm()
            return None
        last_line = self._tail.replace("\r", "\n").split("\n")[-1]
        if _PROMPT.search(last_line):
            answer = self.password + "\r"
            self.disarm()
            return answer
        return None
