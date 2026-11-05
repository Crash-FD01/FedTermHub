# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""I «test rapidi» del menu Rete e degli Strumenti, con i comandi di Windows.

Sono gli stessi della versione Linux (Ping, Traceroute, DNS lookup, Check porta TCP, Route, Socket aperti,
Chiavi SSH, Known hosts, Info sistema), scritti per PowerShell: ognuno si apre in una scheda e chiede
quello che serve (l'host, la porta) dentro la scheda stessa. Fuori da Windows (le prove dei sorgenti)
usano bash come la versione Linux.
"""
from __future__ import annotations

import sys
from typing import Dict, List, Tuple

from ..i18n import tr


def _ps(text: str) -> str:
    """Un testo tradotto dentro apici singoli di PowerShell."""
    return text.replace("'", "''")


def _scripts() -> Dict[str, Tuple[str, str]]:
    """Nome (chiave italiana, come nella versione Linux) -> (script PowerShell, script bash)."""
    enter = tr("Invio per chiudere...")
    end_ps = f"; Write-Host ''; Read-Host '{_ps(enter)}' | Out-Null"
    end_sh = "; echo; read -rp '" + enter.replace("'", "'\\''") + "' _"
    ask_ps = "$h = Read-Host '{q}'; if (-not $h) {{ $h = '{d}' }}; "
    return {
        "Ping": (ask_ps.format(q=_ps(tr("Host da pingare [8.8.8.8]")), d="8.8.8.8") + "ping -n 5 $h" + end_ps,
                 "read -rp 'Host [8.8.8.8]: ' h; ping -c 5 \"${h:-8.8.8.8}\"" + end_sh),
        "Traceroute": (ask_ps.format(q=_ps(tr("Host traceroute [8.8.8.8]")), d="8.8.8.8") + "tracert $h" + end_ps,
                       "read -rp 'Host [8.8.8.8]: ' h; tracepath \"${h:-8.8.8.8}\"" + end_sh),
        "DNS lookup": (ask_ps.format(q=_ps(tr("Nome DNS [example.com]")), d="example.com")
                       + "Resolve-DnsName $h | Format-Table -AutoSize" + end_ps,
                       "read -rp 'Nome [example.com]: ' h; getent hosts \"${h:-example.com}\"" + end_sh),
        "Check porta TCP": (f"$h = Read-Host '{_ps(tr('Host'))}'; $p = Read-Host '{_ps(tr('Porta'))}'; "
                            "$c = New-Object Net.Sockets.TcpClient; "
                            "try { if ($c.ConnectAsync($h, [int]$p).Wait(4000) -and $c.Connected) "
                            f"{{ Write-Host '{_ps(tr('Porta aperta'))}' -ForegroundColor Green }} "
                            f"else {{ Write-Host '{_ps(tr('Porta chiusa/non raggiungibile'))}' -ForegroundColor Red }} }} "
                            f"catch {{ Write-Host '{_ps(tr('Porta chiusa/non raggiungibile'))}' -ForegroundColor Red }} "
                            "finally { $c.Dispose() }" + end_ps,
                            "read -rp 'Host: ' h; read -rp 'Porta: ' p; timeout 4 bash -c \"</dev/tcp/$h/$p\" && "
                            "echo aperta || echo chiusa" + end_sh),
        "IP locale": ("Get-NetIPAddress -AddressFamily IPv4 | Format-Table InterfaceAlias, IPAddress, PrefixLength -AutoSize"
                      + end_ps, "ip -br addr" + end_sh),
        "Route": ("route print" + end_ps, "ip route" + end_sh),
        "Socket aperti": ("netstat -ano" + end_ps, "ss -tulpen" + end_sh),
        "Chiavi SSH": ("Get-ChildItem -Force \"$env:USERPROFILE\\.ssh\" | Format-Table Mode, LastWriteTime, Length, Name -AutoSize; "
                       "ssh-add -l 2>$null" + end_ps, "ls -lah ~/.ssh; ssh-add -l 2>/dev/null" + end_sh),
        "Known hosts": ("notepad \"$env:USERPROFILE\\.ssh\\known_hosts\"", "${EDITOR:-nano} ~/.ssh/known_hosts"),
        "Info sistema": ("Get-ComputerInfo -Property CsName, OsName, OsVersion, OsArchitecture, CsTotalPhysicalMemory, "
                         "WindowsVersion | Format-List" + end_ps,
                         "hostnamectl; cat /etc/os-release" + end_sh),
    }


NETWORK = ("Ping", "Traceroute", "DNS lookup", "Check porta TCP", "Route", "Socket aperti")
TOOLS = ("Chiavi SSH", "Known hosts", "Info sistema")


def names() -> List[str]:
    return list(_scripts())


def argv(name: str) -> List[str]:
    """Il comando da aprire in una scheda per lo strumento ``name`` (chiave italiana)."""
    ps, sh = _scripts()[name]
    if sys.platform == "win32":
        return ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps]
    return ["bash", "-lc", sh]
