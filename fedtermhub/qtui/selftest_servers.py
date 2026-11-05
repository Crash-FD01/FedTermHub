# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Piccoli server finti per la prova automatica (--self-test) e per i test: Telnet e VNC.

Girano in un thread su 127.0.0.1 e rispondono in modo prevedibile, così la prova del programma
installato controlla il client vero senza bisogno di apparati o di server installati sul PC.
"""
from __future__ import annotations

import socket
import struct
import threading
from typing import List, Tuple

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ECHO, SGA, NAWS = 1, 3, 31


class _Server:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.log: List[str] = []
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        raise NotImplementedError

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class TelnetTestServer(_Server):
    """Chiede NAWS, fa l'eco; alla riga «prova» risponde FTH_TELNET_OK con le dimensioni ricevute."""

    def _serve(self, conn: socket.socket) -> None:
        size: Tuple[int, int] = (0, 0)
        conn.sendall(bytes([IAC, WILL, ECHO, IAC, WILL, SGA, IAC, DO, NAWS]) + b"FedTermHub test\r\nlogin: ")
        line = bytearray()
        buf = b""
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
                i = 0
                while i < len(buf):
                    byte = buf[i]
                    if byte == IAC and i + 1 < len(buf):
                        command = buf[i + 1]
                        if command == SB:
                            end = buf.find(bytes([IAC, SE]), i)
                            if end < 0:
                                break
                            payload = buf[i + 2:end]
                            if payload[:1] == bytes([NAWS]) and len(payload) >= 5:
                                size = struct.unpack(">HH", payload[1:5])
                                self.log.append(f"naws {size[0]}x{size[1]}")
                            i = end + 2
                            continue
                        if command in (WILL, WONT, DO, DONT) and i + 2 < len(buf):
                            self.log.append(f"opt {command} {buf[i + 2]}")
                            i += 3
                            continue
                        i += 2
                        continue
                    if byte in (10, 0):
                        i += 1
                        continue
                    if byte == 13:
                        text = line.decode("utf-8", "replace")
                        self.log.append(f"line {text}")
                        reply = f"\r\nFTH_TELNET_OK {text} {size[0]}x{size[1]}\r\n$ " if text == "prova" else "\r\n$ "
                        conn.sendall(reply.encode())
                        line = bytearray()
                    else:
                        line.append(byte)
                        conn.sendall(bytes([byte]))  # eco
                    i += 1
                buf = buf[i:]
        except OSError:
            return
        finally:
            conn.close()


class VncTestServer(_Server):
    """Server RFB 3.8 minimo: autenticazione VNC (password), schermo 320x200 a quattro colori in Raw.

    Il quadrante in alto a sinistra è rosso, quello in alto a destra verde, in basso blu e bianco: il client
    di prova controlla i colori nella schermata. Registra i tasti ricevuti (KeyEvent).
    """

    WIDTH, HEIGHT = 320, 200

    def __init__(self, password: str = "") -> None:
        self.password = password
        self.keys: List[int] = []
        super().__init__()

    @staticmethod
    def _des_key(password: str) -> bytes:
        key = (password.encode("latin-1") + b"\0" * 8)[:8]
        # VNC usa la chiave DES con i bit di ogni byte invertiti
        return bytes(int(f"{b:08b}"[::-1], 2) for b in key)

    def _check(self, challenge: bytes, response: bytes) -> bool:
        try:
            from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
        except ImportError:  # cryptography meno recente
            from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES  # type: ignore[no-redef]
        from cryptography.hazmat.primitives.ciphers import Cipher, modes

        key = self._des_key(self.password)
        encryptor = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
        return encryptor.update(challenge) + encryptor.finalize() == response

    def _pixels(self, shifts: Tuple[int, int, int] = (16, 8, 0), big_endian: bool = False) -> bytes:
        """Lo schermo a 32 bit per pixel nel formato chiesto dal client (SetPixelFormat)."""
        colors = ((255, 0, 0), (0, 200, 0), (0, 0, 255), (255, 255, 255))
        order = ">I" if big_endian else "<I"
        packed = [struct.pack(order, (r << shifts[0]) | (g << shifts[1]) | (b << shifts[2])) for r, g, b in colors]
        half_w, half_h = self.WIDTH // 2, self.HEIGHT // 2
        top = packed[0] * half_w + packed[1] * (self.WIDTH - half_w)
        bottom = packed[2] * half_w + packed[3] * (self.WIDTH - half_w)
        return top * half_h + bottom * (self.HEIGHT - half_h)

    @staticmethod
    def _recv(conn: socket.socket, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise OSError("chiuso")
            data += chunk
        return data

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.sendall(b"RFB 003.008\n")
            self._recv(conn, 12)
            if self.password:
                conn.sendall(bytes([1, 2]))
                if self._recv(conn, 1)[0] != 2:
                    return
                challenge = bytes(range(16))
                conn.sendall(challenge)
                ok = self._check(challenge, self._recv(conn, 16))
                self.log.append("auth ok" if ok else "auth failed")
                if not ok:
                    reason = b"Password VNC sbagliata"
                    conn.sendall(struct.pack(">II", 1, len(reason)) + reason)
                    return
                conn.sendall(struct.pack(">I", 0))
            else:
                conn.sendall(bytes([1, 1]))
                self._recv(conn, 1)
                conn.sendall(struct.pack(">I", 0))
            self._recv(conn, 1)  # ClientInit (shared)
            name = b"FedTermHub test"
            pixel_format = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
            conn.sendall(struct.pack(">HH", self.WIDTH, self.HEIGHT) + pixel_format + struct.pack(">I", len(name)) + name)
            pixels = self._pixels()
            while True:
                kind = self._recv(conn, 1)[0]
                if kind == 0:  # SetPixelFormat
                    fmt = self._recv(conn, 19)[3:]
                    bpp, _depth, big, _true = fmt[0], fmt[1], fmt[2], fmt[3]
                    if bpp == 32:
                        pixels = self._pixels((fmt[10], fmt[11], fmt[12]), bool(big))
                elif kind == 2:  # SetEncodings
                    _pad, count = struct.unpack(">BH", self._recv(conn, 3))
                    self._recv(conn, 4 * count)
                elif kind == 3:  # FramebufferUpdateRequest
                    incremental = self._recv(conn, 9)[0]
                    if not incremental:
                        header = struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, self.WIDTH, self.HEIGHT, 0)
                        conn.sendall(header + pixels)
                        self.log.append("frame")
                elif kind == 4:  # KeyEvent
                    down, _pad, key = struct.unpack(">BHI", self._recv(conn, 7))
                    if down:
                        self.keys.append(key)
                elif kind == 5:  # PointerEvent
                    self._recv(conn, 5)
                elif kind == 6:  # ClientCutText
                    _pad, length = struct.unpack(">3sI", self._recv(conn, 7))
                    self._recv(conn, length)
                else:
                    return
        except OSError:
            return
        finally:
            conn.close()


def start_telnet() -> TelnetTestServer:
    return TelnetTestServer()


def start_vnc(password: str = "") -> VncTestServer:
    return VncTestServer(password)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
