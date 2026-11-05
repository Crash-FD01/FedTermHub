# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Strumenti di rete senza programmi esterni, comuni alle due interfacce.

Ping (su Windows con IcmpSendEcho, senza aprire finestre di console), scansione di una rete e delle porte
TCP, porte in ascolto, Wake-on-LAN, server HTTP e TFTP temporanei. Nessuna funzione qui tocca
l'interfaccia (né GTK né Qt): girano nei thread e restituiscono dati.
"""
from __future__ import annotations

import concurrent.futures
import http.server
import ipaddress
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

try:
    from .i18n import tr
except ImportError:  # avvio come script (fedtermhub/fedtermhub.py) o dai test
    from i18n import tr  # type: ignore[no-redef]

IS_WINDOWS = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW: nessuna console che lampeggia

COMMON_PORTS = [21, 22, 23, 25, 53, 80, 110, 135, 139, 143, 161, 389, 443, 445, 465, 587, 636, 993, 995, 1433, 1521,
                2049, 3306, 3389, 5432, 5900, 5985, 5986, 6443, 8006, 8080, 8443, 9090, 9100, 10000]
TOP_PORTS = sorted(set(COMMON_PORTS + [
    20, 37, 49, 69, 79, 81, 88, 106, 111, 113, 119, 123, 137, 138, 144, 179, 199, 427, 444, 497, 513, 514, 515, 543,
    544, 548, 554, 631, 646, 873, 902, 990, 1025, 1026, 1027, 1028, 1029, 1110, 1720, 1723, 1755, 1900, 2000, 2001,
    2121, 2717, 3000, 3128, 3268, 3269, 3478, 4443, 4899, 5000, 5009, 5051, 5060, 5101, 5190, 5357, 5631, 5666, 5800,
    6000, 6001, 6646, 7070, 8000, 8008, 8009, 8081, 8088, 8181, 8888, 9000, 9100, 9200, 9999, 10443, 32768, 49152,
    49153, 49154, 49155, 49156, 49157]))
SERVICES = {
    20: "ftp-data", 21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 53: "dns", 69: "tftp", 80: "http", 88: "kerberos",
    110: "pop3", 111: "rpcbind", 123: "ntp", 135: "msrpc", 137: "netbios-ns", 139: "netbios-ssn", 143: "imap",
    161: "snmp", 389: "ldap", 443: "https", 445: "smb", 465: "smtps", 514: "syslog", 515: "printer", 548: "afp",
    554: "rtsp", 587: "submission", 631: "ipp", 636: "ldaps", 873: "rsync", 902: "vmware", 993: "imaps", 995: "pop3s",
    1433: "mssql", 1521: "oracle", 1723: "pptp", 1900: "upnp", 2049: "nfs", 3128: "proxy", 3268: "ldap-gc",
    3306: "mysql", 3389: "rdp", 4899: "radmin", 5060: "sip", 5432: "postgresql", 5900: "vnc", 5901: "vnc",
    5985: "winrm", 5986: "winrm-https", 6443: "kubernetes", 8006: "proxmox", 8080: "http-alt", 8443: "https-alt",
    9090: "cockpit", 9100: "jetdirect", 9200: "elasticsearch", 10000: "webmin",
}
SESSION_FOR_PORT = {22: "ssh", 23: "telnet", 3389: "rdp", 5900: "vnc", 5901: "vnc"}


def run_hidden(argv: List[str], timeout: float = 30) -> subprocess.CompletedProcess:
    # stdin esplicito: un programma senza console (il Setup.exe) può avere uno standard input non valido, e
    # subprocess cadrebbe nel duplicarlo («The handle is invalid»).
    return subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=timeout,  # noqa: S603
                          creationflags=NO_WINDOW,
                          encoding="utf-8" if not IS_WINDOWS else "oem", errors="replace")


# ------------------------------------------------------------------ ping
def ping(host: str, timeout_ms: int = 800) -> Optional[float]:
    """Millisecondi di risposta, oppure None. Su Windows con l'API ICMP (non serve essere amministratori)."""
    if IS_WINDOWS:
        return _icmp_windows(host, timeout_ms)
    try:
        proc = run_hidden(["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), "-n", host], timeout=timeout_ms / 1000 + 2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"time[=<]([\d.]+)", proc.stdout)
    return float(match.group(1)) if proc.returncode == 0 and match else None


def _icmp_windows(host: str, timeout_ms: int) -> Optional[float]:
    import ctypes
    from ctypes import wintypes

    try:
        address = socket.gethostbyname(host)
    except OSError:
        return None

    class IP_OPTION_INFORMATION(ctypes.Structure):
        _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                    ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]

    class ICMP_ECHO_REPLY(ctypes.Structure):
        _fields_ = [("Address", wintypes.ULONG), ("Status", wintypes.ULONG), ("RoundTripTime", wintypes.ULONG),
                    ("DataSize", wintypes.USHORT), ("Reserved", wintypes.USHORT), ("Data", ctypes.c_void_p),
                    ("Options", IP_OPTION_INFORMATION)]

    iphlpapi = ctypes.WinDLL("iphlpapi")
    iphlpapi.IcmpCreateFile.restype = wintypes.HANDLE
    iphlpapi.IcmpCloseHandle.argtypes = [wintypes.HANDLE]  # senza: un HANDLE a 64 bit non entra in un int C
    iphlpapi.IcmpSendEcho.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.WORD, ctypes.c_void_p,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    handle = iphlpapi.IcmpCreateFile()
    if not handle or handle == wintypes.HANDLE(-1).value:
        return None
    try:
        payload = b"FedTermHub"
        size = ctypes.sizeof(ICMP_ECHO_REPLY) + len(payload) + 8
        reply = ctypes.create_string_buffer(size)
        dest = struct.unpack("<L", socket.inet_aton(address))[0]
        count = iphlpapi.IcmpSendEcho(handle, dest, payload, len(payload), None, reply, size, timeout_ms)
        if count:
            echo = ICMP_ECHO_REPLY.from_buffer_copy(reply)
            if echo.Status == 0:
                return float(echo.RoundTripTime)
        return None
    finally:
        iphlpapi.IcmpCloseHandle(handle)


# ------------------------------------------------------------------ reti locali e ARP
def local_networks() -> List[str]:
    """Le reti IPv4 delle schede attive, es. ["192.168.1.0/24"] (le /16 o più grandi diventano /24)."""
    pairs: List[tuple] = []
    try:
        from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface

        for iface in QNetworkInterface.allInterfaces():
            flags = iface.flags()
            if not (flags & QNetworkInterface.InterfaceFlag.IsUp) or flags & QNetworkInterface.InterfaceFlag.IsLoopBack:
                continue
            for entry in iface.addressEntries():
                if entry.ip().protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol:
                    pairs.append((entry.ip().toString(), entry.prefixLength()))
    except ImportError:  # interfaccia GTK: senza Qt si legge «ip -4 addr»
        try:
            out = run_hidden(["ip", "-o", "-4", "addr", "show", "up"], 10).stdout
            pairs = [(ip, int(prefix)) for ip, prefix in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", out)]
        except (OSError, subprocess.TimeoutExpired):
            pairs = []
    networks: List[str] = []
    for ip, prefix in pairs:
        try:
            net = ipaddress.ip_network(f"{ip}/{max(prefix, 24)}", strict=False)
        except ValueError:
            continue
        if net.is_loopback or net.is_link_local:
            continue
        if str(net) not in networks:
            networks.append(str(net))
    return networks or ["192.168.1.0/24"]


def arp_table() -> Dict[str, str]:
    """{ip: mac} dalla cache ARP del sistema (arp -a su Windows, ip neigh su Linux)."""
    table: Dict[str, str] = {}
    try:
        if IS_WINDOWS:
            out = run_hidden(["arp", "-a"], 10).stdout
            for ip, mac in re.findall(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})", out):
                table[ip] = mac.replace("-", ":").lower()
        else:
            out = run_hidden(["ip", "neigh"], 10).stdout
            for ip, mac in re.findall(r"(\d+\.\d+\.\d+\.\d+) .*?lladdr ([0-9a-f:]{17})", out):
                table[ip] = mac
    except (OSError, subprocess.TimeoutExpired):
        pass
    return table


@dataclass
class HostResult:
    ip: str
    rtt: Optional[float]
    name: str = ""
    mac: str = ""
    open_ports: str = ""


def scan_network(cidr: str, resolve: bool, progress: Callable[[int, int], None], found: Callable[[HostResult], None],
                 stop: threading.Event, timeout_ms: int = 700) -> List[HostResult]:
    """Ping di tutti gli indirizzi della rete (massimo 4096), con TCP 445/22/80 per chi non risponde al ping."""
    net = ipaddress.ip_network(cidr.strip(), strict=False)
    hosts = [str(h) for h in (net.hosts() if net.num_addresses > 2 else [net.network_address])]
    if len(hosts) > 4096:
        raise ValueError(tr("rete troppo grande (massimo 4096 indirizzi): usa una /20 o più piccola"))
    results: List[HostResult] = []
    done = 0

    def probe(ip: str) -> Optional[HostResult]:
        if stop.is_set():
            return None
        rtt = ping(ip, timeout_ms)
        if rtt is None:
            for port in (445, 22, 80, 443, 3389):
                try:
                    start = time.monotonic()
                    with socket.create_connection((ip, port), timeout=timeout_ms / 1000):
                        rtt = (time.monotonic() - start) * 1000
                        break
                except ConnectionRefusedError:  # risponde con RST: l'host c'è
                    rtt = 0.0
                    break
                except OSError:
                    continue
        if rtt is None:
            return None
        result = HostResult(ip, rtt)
        if resolve:
            try:
                result.name = socket.gethostbyaddr(ip)[0]
            except OSError:
                pass
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        for result in pool.map(probe, hosts):
            done += 1
            progress(done, len(hosts))
            if result is not None:
                results.append(result)
                found(result)
    macs = arp_table()
    for result in results:
        result.mac = macs.get(result.ip, "")
    return results


# ------------------------------------------------------------------ porte TCP
def parse_ports(text: str) -> List[int]:
    ports: List[int] = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
            if start > end:
                start, end = end, start
            ports.extend(range(max(1, start), min(65535, end) + 1))
        else:
            ports.append(int(part))
    return sorted({p for p in ports if 0 < p < 65536})


@dataclass
class PortResult:
    port: int
    service: str
    banner: str = ""

    @property
    def session_protocol(self) -> str:
        return SESSION_FOR_PORT.get(self.port, "")


def _banner(sock: socket.socket, port: int) -> str:
    try:
        if port in (80, 8080, 8000, 8008, 8081, 8088, 9090, 10000):
            sock.sendall(b"HEAD / HTTP/1.0\r\n\r\n")
        sock.settimeout(1.2)
        data = sock.recv(256)
    except OSError:
        return ""
    text = data.decode("latin-1", "replace")
    if text.startswith("HTTP/"):
        server = re.search(r"(?im)^server:\s*(.+)$", text)
        return (text.split("\r\n")[0] + (f" — {server.group(1).strip()}" if server else ""))[:120]
    if port == 3389 or data[:1] == b"\x03":
        return ""
    return re.sub(r"[^\x20-\x7e]+", " ", text).strip()[:120]


def scan_ports(host: str, ports: Iterable[int], progress: Callable[[int, int], None],
               found: Callable[[PortResult], None], stop: threading.Event, timeout: float = 0.8) -> List[PortResult]:
    ports = list(ports)
    address = socket.gethostbyname(host)
    results: List[PortResult] = []
    done = 0

    def probe(port: int) -> Optional[PortResult]:
        if stop.is_set():
            return None
        try:
            with socket.create_connection((address, port), timeout=timeout) as sock:
                return PortResult(port, SERVICES.get(port, ""), _banner(sock, port))
        except OSError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as pool:
        for result in pool.map(probe, ports):
            done += 1
            if done % 16 == 0 or done == len(ports):
                progress(done, len(ports))
            if result is not None:
                results.append(result)
                found(result)
    return results


# ------------------------------------------------------------------ porte in ascolto
@dataclass
class Listener:
    proto: str
    address: str
    port: int
    pid: int
    process: str


def listening_ports() -> List[Listener]:
    listeners: List[Listener] = []
    if IS_WINDOWS:
        names: Dict[int, str] = {}
        try:
            for line in run_hidden(["tasklist", "/fo", "csv", "/nh"], 20).stdout.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 2 and parts[1].isdigit():
                    names[int(parts[1])] = parts[0].lstrip('"')
        except (OSError, subprocess.TimeoutExpired):
            pass
        out = run_hidden(["netstat", "-ano"], 20).stdout
        for line in out.splitlines():
            cols = line.split()
            if len(cols) >= 4 and cols[0] in {"TCP", "UDP"}:
                # In ascolto: indirizzo remoto 0.0.0.0:0 o [::]:0 (lo stato è scritto nella lingua di Windows)
                if cols[0] == "TCP" and (len(cols) < 5 or cols[2] not in {"0.0.0.0:0", "[::]:0"}):
                    continue
                local = cols[1]
                pid = int(cols[-1]) if cols[-1].isdigit() else 0
                address, _, port = local.rpartition(":")
                if port.isdigit():
                    listeners.append(Listener(cols[0], address.strip("[]"), int(port), pid, names.get(pid, "")))
    else:
        out = run_hidden(["ss", "-ltunpH"], 20).stdout
        for line in out.splitlines():
            cols = line.split()
            if len(cols) < 5:
                continue
            address, _, port = cols[4].rpartition(":")
            proc = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
            if port.isdigit():
                listeners.append(Listener(cols[0].upper(), address.strip("[]"), int(port),
                                          int(proc.group(2)) if proc else 0, proc.group(1) if proc else ""))
    unique = {(item.proto, item.address, item.port): item for item in listeners}
    return sorted(unique.values(), key=lambda item: (item.port, item.proto))


# ------------------------------------------------------------------ Wake-on-LAN
def wake_on_lan(mac: str, broadcast: str = "255.255.255.255", port: int = 9, repeat: int = 1) -> None:
    """Manda il «magic packet» (sei FF e il MAC ripetuto 16 volte), eventualmente più volte."""
    clean = re.sub(r"[:\-.\s]", "", mac.strip())
    if len(clean) != 12 or not all(ch in "0123456789abcdefABCDEF" for ch in clean):
        raise ValueError(tr("indirizzo MAC non valido"))
    packet = b"\xff" * 6 + bytes.fromhex(clean) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for index in range(max(1, repeat)):
            if index:
                time.sleep(0.05)
            sock.sendto(packet, (broadcast or "255.255.255.255", port))


# ------------------------------------------------------------------ server temporanei
class HttpFileServer:
    """Server HTTP di sola lettura su una cartella (per passare file a un apparato o a un altro PC)."""

    def __init__(self, folder: Path, port: int, log: Callable[[str], None], bind: str = "0.0.0.0") -> None:
        folder = Path(folder)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: ANN001, ANN002, N802
                log(f"{self.client_address[0]}  {fmt % args}")

        self.httpd = http.server.ThreadingHTTPServer((bind, port), partial(Handler, directory=str(folder)))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="fth-http", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class TftpServer:
    """Server TFTP (RFC 1350, con blksize e tsize): legge i file di una cartella e, se permesso, li scrive.

    Serve per i backup e gli aggiornamenti di switch, firewall, telefoni IP. I nomi non possono uscire dalla
    cartella.
    """

    def __init__(self, folder: Path, port: int, log: Callable[[str], None], allow_write: bool = False,
                 bind: str = "0.0.0.0") -> None:
        self.folder = Path(folder).resolve()
        self.log = log
        self.allow_write = allow_write
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind, port))
        self.port = self.sock.getsockname()[1]
        self.running = True
        self.thread = threading.Thread(target=self._loop, name="fth-tftp", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        self.sock.settimeout(0.5)
        while self.running:
            try:
                data, client = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._transfer, args=(data, client), daemon=True).start()

    def _path(self, name: str) -> Optional[Path]:
        name = name.replace("\\", "/").lstrip("/")
        target = (self.folder / name).resolve()
        if target != self.folder and self.folder not in target.parents:
            return None
        return target

    @staticmethod
    def _error(sock: socket.socket, client, code: int, text: str) -> None:  # noqa: ANN001
        sock.sendto(struct.pack(">HH", 5, code) + text.encode() + b"\0", client)

    def _transfer(self, data: bytes, client) -> None:  # noqa: ANN001
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.sock.getsockname()[0], 0))
        sock.settimeout(3)
        try:
            opcode = struct.unpack(">H", data[:2])[0]
            fields = data[2:].split(b"\0")
            name = fields[0].decode("latin-1")
            mode = fields[1].decode("latin-1").lower() if len(fields) > 1 else "octet"
            options = {fields[i].decode().lower(): fields[i + 1].decode() for i in range(2, len(fields) - 1, 2) if fields[i]}
            path = self._path(name)
            if path is None:
                self._error(sock, client, 2, "access violation")
                return
            blksize = max(8, min(int(options.get("blksize", 512)), 65464)) if "blksize" in options else 512
            if opcode == 1:
                self._send_file(sock, client, path, name, blksize, options, mode)
            elif opcode == 2:
                if not self.allow_write:
                    self._error(sock, client, 2, "write not allowed")
                    self.log(tr("{client}  scrittura rifiutata: {name}").format(client=client[0], name=name))
                    return
                self._receive_file(sock, client, path, name, blksize, options)
            else:
                self._error(sock, client, 4, "illegal operation")
        except Exception as exc:  # noqa: BLE001
            self.log(tr("{client}  errore: {error}").format(client=client[0], error=exc))
        finally:
            sock.close()

    def _oack(self, sock, client, options: Dict[str, str], blksize: int, tsize: Optional[int]) -> bool:  # noqa: ANN001
        reply = {}
        if "blksize" in options:
            reply["blksize"] = str(blksize)
        if "tsize" in options and tsize is not None:
            reply["tsize"] = str(tsize)
        if not reply:
            return False
        payload = b"".join(k.encode() + b"\0" + v.encode() + b"\0" for k, v in reply.items())
        sock.sendto(struct.pack(">H", 6) + payload, client)
        return True

    def _send_file(self, sock, client, path: Path, name: str, blksize: int, options, mode: str) -> None:  # noqa: ANN001
        if not path.is_file():
            self._error(sock, client, 1, "file not found")
            self.log(tr("{client}  non trovato: {name}").format(client=client[0], name=name))
            return
        content = path.read_bytes()
        if mode == "netascii":
            content = content.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        if self._oack(sock, client, options, blksize, len(content)):
            if not self._wait_ack(sock, client, 0):
                return
        block = 1
        offset = 0
        start = time.monotonic()
        while True:
            chunk = content[offset:offset + blksize]
            packet = struct.pack(">HH", 3, block & 0xFFFF) + chunk
            for _attempt in range(5):
                sock.sendto(packet, client)
                if self._wait_ack(sock, client, block & 0xFFFF):
                    break
            else:
                self.log(tr("{client}  interrotto: {name}").format(client=client[0], name=name))
                return
            offset += len(chunk)
            block += 1
            if len(chunk) < blksize:
                break
        self.log(tr("{client}  inviato {name} ({size} byte in {seconds} s)").format(
            client=client[0], name=name, size=len(content), seconds=f"{time.monotonic() - start:.1f}"))

    @staticmethod
    def _wait_ack(sock, client, block: int) -> bool:  # noqa: ANN001
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                return False
            if addr != client:
                continue
            opcode, number = struct.unpack(">HH", data[:4])
            if opcode == 4 and number == block:
                return True
            if opcode == 5:
                return False
        return False

    def _receive_file(self, sock, client, path: Path, name: str, blksize: int, options) -> None:  # noqa: ANN001
        path.parent.mkdir(parents=True, exist_ok=True)
        if not self._oack(sock, client, options, blksize, None):
            sock.sendto(struct.pack(">HH", 4, 0), client)
        expected = 1
        received = bytearray()
        while True:
            try:
                data, addr = sock.recvfrom(blksize + 4)
            except socket.timeout:
                self.log(tr("{client}  ricezione interrotta: {name}").format(client=client[0], name=name))
                return
            if addr != client:
                continue
            opcode, number = struct.unpack(">HH", data[:4])
            if opcode != 3:
                return
            if number == expected & 0xFFFF:
                received += data[4:]
                expected += 1
            sock.sendto(struct.pack(">HH", 4, number), client)
            if len(data) - 4 < blksize and number == (expected - 1) & 0xFFFF:
                break
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(bytes(received))
        os.replace(tmp, path)
        self.log(tr("{client}  ricevuto {name} ({size} byte)").format(client=client[0], name=name, size=len(received)))

    def stop(self) -> None:
        self.running = False
        self.sock.close()


def tftp_get(host: str, port: int, name: str, timeout: float = 3) -> bytes:
    """Client TFTP minimo (per le prove del server)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    sock.sendto(struct.pack(">H", 1) + name.encode() + b"\0octet\0", (host, port))
    data = bytearray()
    expected = 1
    try:
        while True:
            packet, addr = sock.recvfrom(1024)
            opcode, block = struct.unpack(">HH", packet[:4])
            if opcode == 5:
                raise OSError(packet[4:-1].decode())
            if opcode == 3 and block == expected:
                data += packet[4:]
                sock.sendto(struct.pack(">HH", 4, block), addr)
                expected += 1
                if len(packet) - 4 < 512:
                    return bytes(data)
    finally:
        sock.close()
