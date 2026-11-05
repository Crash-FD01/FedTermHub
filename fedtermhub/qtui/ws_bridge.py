# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ponte locale WebSocket → TCP per il client VNC (noVNC) nella scheda.

noVNC parla RFB su un WebSocket; i server VNC ascoltano su TCP. Questo piccolo server, solo su 127.0.0.1 e
con un codice casuale nell'indirizzo, serve la pagina di noVNC e gira i byte del WebSocket al server VNC
della sessione, e viceversa. Usa solo QtNetwork (nessun thread, nessuna libreria in più).
"""
from __future__ import annotations

import base64
import hashlib
import mimetypes
import secrets
import struct
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

STATIC = Path(__file__).resolve().parent / "novnc"
_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _Link(QObject):
    """Un WebSocket del browser collegato a un socket TCP verso il server VNC."""

    closed = Signal()

    def __init__(self, ws: QTcpSocket, host: str, port: int, parent: QObject) -> None:
        super().__init__(parent)
        self.ws = ws
        self.buffer = bytearray()
        self.pending = bytearray()
        self.tcp = QTcpSocket(self)
        self.tcp.connected.connect(self._tcp_connected)
        self.tcp.readyRead.connect(self._tcp_read)
        self.tcp.disconnected.connect(self.close)
        self.tcp.errorOccurred.connect(lambda *_: self.close())
        self.ws.readyRead.connect(self._ws_read)
        self.ws.disconnected.connect(self.close)
        self._closing = False
        self.tcp.connectToHost(host, port)

    def _tcp_connected(self) -> None:
        if self.pending:
            self.tcp.write(bytes(self.pending))
            self.pending.clear()

    def _tcp_read(self) -> None:
        data = bytes(self.tcp.readAll())
        if data:
            self._send_frame(2, data)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        length = len(payload)
        if length < 126:
            header = struct.pack(">BB", 0x80 | opcode, length)
        elif length < 65536:
            header = struct.pack(">BBH", 0x80 | opcode, 126, length)
        else:
            header = struct.pack(">BBQ", 0x80 | opcode, 127, length)
        self.ws.write(header + payload)

    def _ws_read(self) -> None:
        self.buffer += bytes(self.ws.readAll())
        while True:
            if len(self.buffer) < 2:
                return
            first, second = self.buffer[0], self.buffer[1]
            opcode = first & 0x0F
            masked = second & 0x80
            length = second & 0x7F
            offset = 2
            if length == 126:
                if len(self.buffer) < 4:
                    return
                length = struct.unpack(">H", self.buffer[2:4])[0]
                offset = 4
            elif length == 127:
                if len(self.buffer) < 10:
                    return
                length = struct.unpack(">Q", self.buffer[2:10])[0]
                offset = 10
            mask = b""
            if masked:
                if len(self.buffer) < offset + 4:
                    return
                mask = bytes(self.buffer[offset:offset + 4])
                offset += 4
            if len(self.buffer) < offset + length:
                return
            payload = bytes(self.buffer[offset:offset + length])
            del self.buffer[:offset + length]
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if length < 4096 else _unmask(payload, mask)
            if opcode in (0, 1, 2):
                if self.tcp.state() == QTcpSocket.SocketState.ConnectedState:
                    self.tcp.write(payload)
                else:
                    self.pending += payload
            elif opcode == 8:
                self.close()
                return
            elif opcode == 9:
                self._send_frame(10, payload)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            if self.ws.state() == QTcpSocket.SocketState.ConnectedState:
                self._send_frame(8, struct.pack(">H", 1000))
                self.ws.flush()
            self.ws.disconnectFromHost()
        except RuntimeError:
            pass
        try:
            self.tcp.abort()
        except RuntimeError:
            pass
        self.closed.emit()


def _unmask(payload: bytes, mask: bytes) -> bytes:
    """XOR veloce con la maschera del WebSocket (il pacchetto può essere grande)."""
    n = len(payload)
    key = int.from_bytes((mask * ((n + 3) // 4))[:n], "big")
    return (int.from_bytes(payload, "big") ^ key).to_bytes(n, "big")


class VncBridge(QObject):
    """Server locale: http://127.0.0.1:<porta>/<codice>/vnc.html e il WebSocket /<codice>/ws.

    Ogni ponte va verso un solo server VNC (host, porta), fissato alla creazione.
    """

    def __init__(self, host: str, port: int, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.host, self.port = host, port
        self.token = secrets.token_urlsafe(16)
        self.server = QTcpServer(self)
        self.server.newConnection.connect(self._new_connection)
        if not self.server.listen(QHostAddress.SpecialAddress.LocalHost, 0):
            raise OSError(self.server.errorString())
        self.links: list = []
        self._requests: Dict[QTcpSocket, bytearray] = {}

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.serverPort()}/{self.token}/"

    def _new_connection(self) -> None:
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            self._requests[sock] = bytearray()
            sock.readyRead.connect(lambda s=sock: self._read_request(s))
            sock.disconnected.connect(lambda s=sock: self._requests.pop(s, None))

    def _read_request(self, sock: QTcpSocket) -> None:
        if sock not in self._requests:
            return
        buf = self._requests[sock]
        buf += bytes(sock.readAll())
        if b"\r\n\r\n" not in buf:
            if len(buf) > 16384:
                sock.abort()
            return
        head, _, rest = bytes(buf).partition(b"\r\n\r\n")
        del self._requests[sock]
        sock.readyRead.disconnect()
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, path, _version = lines[0].split(" ", 2)
        except ValueError:
            sock.abort()
            return
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        prefix = f"/{self.token}/"
        if method != "GET" or not path.startswith(prefix):
            self._reply(sock, 404, b"not found")
            return
        name = path[len(prefix):].split("?", 1)[0]
        if headers.get("upgrade", "").lower() == "websocket" and name == "ws":
            self._upgrade(sock, headers, rest)
            return
        if name == "qwebchannel.js":
            # Lo script di QWebChannel è nelle risorse di Qt (qrc:), che una pagina http non può leggere.
            from PySide6.QtCore import QFile, QIODevice

            resource = QFile(":/qtwebchannel/qwebchannel.js")
            if resource.open(QIODevice.OpenModeFlag.ReadOnly):
                self._reply(sock, 200, bytes(resource.readAll()), "text/javascript")
                resource.close()
            else:
                self._reply(sock, 404, b"not found")
            return
        file = (STATIC / name).resolve()
        if not name or STATIC.resolve() not in file.parents or not file.is_file():
            self._reply(sock, 404, b"not found")
            return
        kind = "text/javascript" if file.suffix in {".js", ".mjs"} else (mimetypes.guess_type(file.name)[0] or "application/octet-stream")
        self._reply(sock, 200, file.read_bytes(), kind)

    @staticmethod
    def _reply(sock: QTcpSocket, code: int, body: bytes, kind: str = "text/plain") -> None:
        reason = {200: "OK", 404: "Not Found"}.get(code, "Error")
        head = (f"HTTP/1.1 {code} {reason}\r\nContent-Type: {kind}; charset=utf-8\r\nContent-Length: {len(body)}\r\n"
                "Cache-Control: no-store\r\nConnection: close\r\n\r\n").encode()
        sock.write(head + body)
        sock.disconnectFromHost()

    def _upgrade(self, sock: QTcpSocket, headers: Dict[str, str], rest: bytes) -> None:
        key = headers.get("sec-websocket-key", "").encode()
        accept = base64.b64encode(hashlib.sha1(key + _GUID).digest()).decode()  # noqa: S324 (lo chiede il protocollo)
        reply = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Accept: {accept}\r\n")
        protocols = [p.strip() for p in headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
        if protocols:
            reply += f"Sec-WebSocket-Protocol: {'binary' if 'binary' in protocols else protocols[0]}\r\n"
        sock.write((reply + "\r\n").encode())
        link = _Link(sock, self.host, self.port, self)
        self.links.append(link)
        link.closed.connect(lambda l=link: self.links.remove(l) if l in self.links else None)
        if rest:
            link.buffer += rest
            link._ws_read()

    def close(self) -> None:
        for link in list(self.links):
            link.close()
        self.server.close()
