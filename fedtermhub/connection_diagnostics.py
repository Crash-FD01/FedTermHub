# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Diagnostica di connettività contestuale per FedTermHub.

Il modulo non dipende da GTK: i controlli possono essere eseguiti in un thread
worker, testati separatamente e mostrati dall'interfaccia principale soltanto
quando l'utente ne ha bisogno.
"""
from __future__ import annotations

import errno
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple


DEFAULT_PORTS: Dict[str, int] = {
    "rdp": 3389,
    "ssh": 22,
    "sftp": 22,
    "vnc": 5900,
    "telnet": 23,
}

STATUS_LABELS = {
    "ok": "OK",
    "warning": "ATTENZIONE",
    "error": "ERRORE",
    "info": "INFO",
}


@dataclass(frozen=True)
class DiagnosticTarget:
    """Destinazione priva di credenziali da sottoporre ai controlli."""

    name: str
    protocol: str
    host: str
    port: int

    @property
    def endpoint(self) -> str:
        port_label = str(self.port) if self.port > 0 else "porta non valida"
        if ":" in self.host and not self.host.startswith("["):
            return f"[{self.host}]:{port_label}"
        return f"{self.host}:{port_label}"


@dataclass
class DiagnosticStep:
    key: str
    title: str
    status: str
    summary: str
    details: str = ""


@dataclass
class DiagnosticReport:
    target: DiagnosticTarget
    started_at: str
    elapsed_ms: int
    steps: List[DiagnosticStep] = field(default_factory=list)
    resolved_addresses: List[str] = field(default_factory=list)
    recommendation: str = ""
    outcome: str = "info"

    def step(self, key: str) -> Optional[DiagnosticStep]:
        return next((item for item in self.steps if item.key == key), None)

    def to_text(self) -> str:
        lines = [
            "FedTermHub - Diagnostica connessione",
            "====================================",
            f"Sessione: {self.target.name or '-'}",
            f"Protocollo: {self.target.protocol.upper()}",
            f"Destinazione: {self.target.endpoint}",
            f"Eseguita: {self.started_at}",
            f"Durata: {self.elapsed_ms} ms",
            "",
        ]
        for item in self.steps:
            label = STATUS_LABELS.get(item.status, item.status.upper())
            lines.append(f"[{label}] {item.title}: {item.summary}")
            if item.details:
                for detail_line in item.details.strip().splitlines():
                    lines.append(f"    {detail_line}")
        if self.recommendation:
            lines += ["", "Suggerimento:", self.recommendation]
        lines += ["", "Il rapporto non contiene password o altri segreti."]
        return "\n".join(lines).rstrip() + "\n"


def normalize_port(protocol: str, value: object) -> int:
    text = str(value or "").strip()
    if text:
        if text.isdigit():
            number = int(text)
            # VNC: un numero piccolo e' il display (1 = porta 5901), come in vncviewer.
            if str(protocol or "").lower() == "vnc" and number < 100:
                return 5900 + number
            if 1 <= number <= 65535:
                return number
        return 0
    return DEFAULT_PORTS.get(str(protocol or "").lower(), 0)


def make_target(name: str, protocol: str, host: str, port: object = "") -> DiagnosticTarget:
    proto = str(protocol or "").strip().lower()
    clean_host = str(host or "").strip()
    if clean_host.startswith("[") and clean_host.endswith("]"):
        clean_host = clean_host[1:-1]
    return DiagnosticTarget(
        name=str(name or clean_host or "Sessione"),
        protocol=proto,
        host=clean_host,
        port=normalize_port(proto, port),
    )


def _run_command(argv: Sequence[str], timeout: float = 4.0) -> Tuple[int, str, str]:
    try:
        result = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(0.5, timeout),
            check=False,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return 124, stdout.strip(), (stderr.strip() or "comando scaduto")
    except OSError as exc:
        return 127, "", str(exc)


def _resolve_host(host: str, timeout: float) -> Tuple[List[str], Optional[str]]:
    try:
        literal = ipaddress.ip_address(host)
        return [str(literal)], None
    except ValueError:
        pass

    # Su Fedora getent interroga la stessa catena NSS usata dalle applicazioni
    # (DNS, /etc/hosts, mDNS quando configurato), ma può essere limitato con un
    # timeout senza lasciare bloccata la finestra GTK.
    getent = shutil.which("getent")
    if getent:
        rc, stdout, stderr = _run_command([getent, "ahosts", host], timeout)
        addresses: List[str] = []
        if rc == 0:
            for line in stdout.splitlines():
                fields = line.split()
                if not fields:
                    continue
                candidate = fields[0].split("%", 1)[0]
                try:
                    address = str(ipaddress.ip_address(candidate))
                except ValueError:
                    continue
                if address not in addresses:
                    addresses.append(address)
        if addresses:
            return addresses, None
        if rc == 124:
            return [], "timeout durante la risoluzione del nome"
        return [], stderr or stdout or "nome non trovato dalla configurazione NSS"

    # Fallback per ambienti minimali diversi da Fedora.
    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return [], str(exc)
    except OSError as exc:
        return [], str(exc)

    addresses = []
    for _family, _socktype, _proto, _canonname, sockaddr in records:
        address = str(sockaddr[0])
        if address not in addresses:
            addresses.append(address)
    return addresses, None if addresses else "nessun indirizzo restituito"


def _route_for(address: str, timeout: float) -> Tuple[DiagnosticStep, Dict[str, object]]:
    ip_binary = shutil.which("ip")
    if not ip_binary:
        return (
            DiagnosticStep(
                "route", "Instradamento", "warning",
                "Comando ip non disponibile",
                "Installa il pacchetto iproute per ottenere interfaccia, gateway e indirizzo sorgente.",
            ),
            {},
        )

    rc, stdout, stderr = _run_command([ip_binary, "-j", "route", "get", address], timeout)
    if rc == 0 and stdout:
        try:
            rows = json.loads(stdout)
            row = rows[0] if isinstance(rows, list) and rows else {}
            if isinstance(row, dict):
                dev = str(row.get("dev", "") or "")
                gateway = str(row.get("gateway", "") or "")
                source = str(row.get("prefsrc", row.get("src", "")) or "")
                route_type = str(row.get("type", "unicast") or "unicast")
                if route_type in {"unreachable", "blackhole", "prohibit", "throw"}:
                    return DiagnosticStep(
                        "route", "Instradamento", "error", f"Route {route_type}", stdout
                    ), row
                summary = f"via {dev or 'interfaccia non indicata'}"
                if gateway:
                    summary += f", gateway {gateway}"
                details = []
                if source:
                    details.append(f"Indirizzo sorgente: {source}")
                table = row.get("table")
                if table not in (None, "", 254):
                    details.append(f"Tabella di routing: {table}")
                return DiagnosticStep(
                    "route", "Instradamento", "ok", summary, "\n".join(details)
                ), row
        except (ValueError, TypeError, KeyError):
            pass

    # Compatibilità con versioni/iproute che non espongono JSON.
    rc, stdout, stderr = _run_command([ip_binary, "route", "get", address], timeout)
    low = f"{stdout}\n{stderr}".lower()
    if rc != 0 or any(word in low for word in ("unreachable", "prohibit", "blackhole", "network is unreachable")):
        return DiagnosticStep(
            "route", "Instradamento", "error", "Nessuna route utilizzabile",
            stderr or stdout or "ip route get non ha restituito un percorso.",
        ), {}
    tokens = stdout.split()
    dev = tokens[tokens.index("dev") + 1] if "dev" in tokens and tokens.index("dev") + 1 < len(tokens) else ""
    gateway = tokens[tokens.index("via") + 1] if "via" in tokens and tokens.index("via") + 1 < len(tokens) else ""
    source = tokens[tokens.index("src") + 1] if "src" in tokens and tokens.index("src") + 1 < len(tokens) else ""
    summary = f"via {dev or 'interfaccia non indicata'}"
    if gateway:
        summary += f", gateway {gateway}"
    return DiagnosticStep(
        "route", "Instradamento", "ok" if stdout else "warning", summary,
        f"Indirizzo sorgente: {source}" if source else stdout,
    ), {"dev": dev, "gateway": gateway, "prefsrc": source}


def _active_vpn(timeout: float) -> DiagnosticStep:
    nmcli = shutil.which("nmcli")
    active: List[str] = []
    details: List[str] = []
    if nmcli:
        rc, stdout, stderr = _run_command(
            [nmcli, "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"], timeout
        )
        if rc == 0:
            for line in stdout.splitlines():
                parts = line.rsplit(":", 2)
                if len(parts) != 3:
                    continue
                name, conn_type, device = parts
                conn_type_low = conn_type.lower()
                device_low = device.lower()
                if conn_type_low in {"vpn", "wireguard"} or device_low.startswith(("tun", "tap", "wg", "ppp")):
                    active.append(name or device or conn_type)
                    details.append(f"{name or '-'} — {conn_type or '-'} — {device or '-'}")
        elif stderr:
            details.append(stderr)

    if not active:
        ip_binary = shutil.which("ip")
        if ip_binary:
            rc, stdout, _stderr = _run_command([ip_binary, "-o", "link", "show", "up"], timeout)
            if rc == 0:
                for line in stdout.splitlines():
                    fields = line.split(":", 2)
                    if len(fields) < 2:
                        continue
                    device = fields[1].strip().split("@")[0]
                    if device.lower().startswith(("tun", "tap", "wg", "ppp")):
                        if device not in active:
                            active.append(device)
                            details.append(device)

    if active:
        return DiagnosticStep(
            "vpn", "VPN", "ok", f"Attiva: {', '.join(active)}", "\n".join(details)
        )
    return DiagnosticStep(
        "vpn", "VPN", "info", "Nessuna VPN attiva rilevata",
        "Non è necessariamente un errore: dipende dalla rete richiesta dalla sessione.",
    )


def _tcp_probe(addresses: Sequence[str], port: int, timeout: float, protocol: str) -> DiagnosticStep:
    if port <= 0:
        return DiagnosticStep(
            "tcp", "Porta TCP", "error", "Porta non valida",
            "Imposta una porta compresa tra 1 e 65535 nella sessione.",
        )

    attempts: List[str] = []
    best_failure: Optional[Tuple[str, str, str]] = None
    for address in addresses:
        started = time.monotonic()
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(max(0.5, timeout))
        try:
            sock.connect((address, port))
            elapsed_ms = int((time.monotonic() - started) * 1000)
            banner = ""
            if protocol in {"ssh", "sftp", "vnc"}:
                try:
                    sock.settimeout(min(1.5, max(0.5, timeout)))
                    banner = sock.recv(160).decode("utf-8", errors="replace").strip()
                except (socket.timeout, OSError):
                    banner = ""
            details = f"Indirizzo: {address}\nTempo apertura TCP: {elapsed_ms} ms"
            if banner:
                safe_banner = "".join(ch for ch in banner if ch.isprintable())[:140]
                details += f"\nBanner: {safe_banner}"
            return DiagnosticStep(
                "tcp", "Porta TCP", "ok", f"{port} raggiungibile in {elapsed_ms} ms", details
            )
        except ConnectionRefusedError as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            attempts.append(f"{address}: rifiutata dopo {elapsed_ms} ms")
            best_failure = ("refused", address, str(exc))
        except socket.timeout as exc:
            attempts.append(f"{address}: timeout dopo {int(timeout * 1000)} ms")
            if best_failure is None:
                best_failure = ("timeout", address, str(exc) or "timed out")
        except OSError as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            attempts.append(f"{address}: {exc} dopo {elapsed_ms} ms")
            kind = "unreachable" if exc.errno in {
                errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.EHOSTDOWN,
            } else "other"
            if best_failure is None or kind == "unreachable":
                best_failure = (kind, address, str(exc))
        finally:
            try:
                sock.close()
            except OSError:
                pass

    details = "\n".join(attempts) or "Nessun indirizzo provato."
    if not best_failure:
        return DiagnosticStep("tcp", "Porta TCP", "error", "Controllo non eseguito", details)
    kind, _address, error_text = best_failure
    if kind == "refused":
        return DiagnosticStep(
            "tcp", "Porta TCP", "error", f"Porta {port} rifiutata",
            details + "\nL'host risponde, ma il servizio non accetta connessioni sulla porta indicata.",
        )
    if kind == "timeout":
        return DiagnosticStep(
            "tcp", "Porta TCP", "error", f"Timeout sulla porta {port}",
            details + "\nPossibili cause: VPN assente, firewall DROP, NAT o host spento.",
        )
    if kind == "unreachable":
        return DiagnosticStep(
            "tcp", "Porta TCP", "error", "Rete o host non raggiungibile", details
        )
    return DiagnosticStep(
        "tcp", "Porta TCP", "error", f"Connessione non riuscita: {error_text}", details
    )


def _overall_recommendation(steps: Sequence[DiagnosticStep], target: DiagnosticTarget) -> Tuple[str, str]:
    by_key = {step.key: step for step in steps}
    dns = by_key.get("dns")
    route = by_key.get("route")
    tcp = by_key.get("tcp")
    vpn = by_key.get("vpn")

    if not target.host:
        return "Compila il nome host o l'indirizzo IP della sessione.", "error"
    if dns and dns.status == "error":
        return (
            "Verifica il nome host e i DNS configurati. Se il nome è interno, collega prima la VPN "
            "o usa il DNS aziendale corretto.",
            "error",
        )
    if tcp and tcp.status == "ok":
        return (
            "La connettività TCP è corretta. Se la sessione continua a fallire, verifica credenziali, dominio, "
            "certificato e autorizzazioni del servizio remoto.",
            "ok",
        )
    if route and route.status == "error":
        extra = ""
        if vpn and vpn.status != "ok":
            extra = " Non risulta attiva alcuna VPN: collegala se la destinazione è aziendale."
        return "Manca un percorso di rete verso la destinazione. Controlla routing e gateway." + extra, "error"
    if tcp and "rifiutata" in tcp.summary.lower():
        service = {
            "rdp": "Desktop remoto",
            "ssh": "SSH",
            "sftp": "SSH/SFTP",
            "vnc": "VNC",
            "telnet": "Telnet",
        }.get(target.protocol, "servizio remoto")
        return (
            f"Il percorso di rete funziona, ma {service} non ascolta sulla porta {target.port}. "
            "Controlla servizio, porta configurata e regole firewall REJECT.",
            "error",
        )
    if tcp and "timeout" in tcp.summary.lower():
        extra = ""
        if vpn and vpn.status != "ok":
            extra = " Se la rete è privata, collega prima la VPN."
        return (
            "Il tentativo TCP non riceve risposta. Controlla host acceso, VPN, NAT e firewall che potrebbe scartare i pacchetti."
            + extra,
            "error",
        )
    if tcp and tcp.status == "error":
        return "Controlla connettività, routing, firewall e stato del servizio remoto.", "error"
    return "Controlla i risultati evidenziati prima di riprovare la sessione.", "warning"


def run_connection_diagnostics(target: DiagnosticTarget, timeout: float = 4.0) -> DiagnosticReport:
    started_monotonic = time.monotonic()
    started_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    steps: List[DiagnosticStep] = []

    if not target.host:
        steps.append(DiagnosticStep("target", "Destinazione", "error", "Host mancante"))
        recommendation, outcome = _overall_recommendation(steps, target)
        return DiagnosticReport(
            target=target,
            started_at=started_at,
            elapsed_ms=int((time.monotonic() - started_monotonic) * 1000),
            steps=steps,
            recommendation=recommendation,
            outcome=outcome,
        )

    addresses, resolve_error = _resolve_host(target.host, timeout)
    if addresses:
        literal = False
        try:
            ipaddress.ip_address(target.host)
            literal = True
        except ValueError:
            pass
        summary = ", ".join(addresses[:4])
        if len(addresses) > 4:
            summary += f" (+{len(addresses) - 4})"
        steps.append(DiagnosticStep(
            "dns", "Risoluzione DNS", "info" if literal else "ok",
            "Indirizzo IP fornito direttamente" if literal else summary,
            "\n".join(addresses) if literal else "",
        ))
    else:
        steps.append(DiagnosticStep(
            "dns", "Risoluzione DNS", "error", "Nome non risolto", resolve_error or "Errore sconosciuto"
        ))

    route_data: Dict[str, object] = {}
    if addresses:
        route_step: Optional[DiagnosticStep] = None
        route_errors: List[str] = []
        for address in addresses:
            candidate_step, candidate_data = _route_for(address, timeout)
            if candidate_step.status != "error":
                route_step, route_data = candidate_step, candidate_data
                if address != addresses[0]:
                    route_step.details = (route_step.details + "\n" if route_step.details else "") + f"Indirizzo instradato: {address}"
                break
            route_errors.append(f"{address}: {candidate_step.summary}")
            route_step, route_data = candidate_step, candidate_data
        if route_step is not None and route_errors and route_step.status == "error":
            route_step.details = "\n".join(route_errors)
        if route_step is not None:
            steps.append(route_step)
    else:
        steps.append(DiagnosticStep(
            "route", "Instradamento", "warning", "Non verificabile senza un indirizzo risolto"
        ))

    vpn_step = _active_vpn(timeout)
    if route_data.get("dev") and str(route_data.get("dev", "")).lower().startswith(("tun", "tap", "wg", "ppp")):
        if vpn_step.status != "ok":
            vpn_step = DiagnosticStep(
                "vpn", "VPN", "ok", f"Traffico instradato su {route_data.get('dev')}",
                "Rilevata dal percorso di routing della destinazione.",
            )
    steps.append(vpn_step)

    if addresses:
        steps.append(_tcp_probe(addresses, target.port, timeout, target.protocol))
    else:
        steps.append(DiagnosticStep(
            "tcp", "Porta TCP", "warning", "Non verificabile senza risoluzione DNS"
        ))

    recommendation, outcome = _overall_recommendation(steps, target)
    return DiagnosticReport(
        target=target,
        started_at=started_at,
        elapsed_ms=int((time.monotonic() - started_monotonic) * 1000),
        steps=steps,
        resolved_addresses=addresses,
        recommendation=recommendation,
        outcome=outcome,
    )
