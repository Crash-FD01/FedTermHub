# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Connessioni che si mostrano in un terminale senza un programma esterno: Telnet e porta seriale.

Hanno la stessa interfaccia di PtyProcess (segnali output/finished, start, write, paste, resize, stop,
running), così TerminalView le usa allo stesso modo. Il client Telnet è scritto qui: telnetlib non c'è
più in Python 3.13 e il «Client Telnet» di Windows è una funzionalità da installare a parte.
"""
from __future__ import annotations

import codecs
import socket
import threading
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from ..i18n import tr

# Comandi e opzioni Telnet (RFC 854, 857, 858, 1073, 1091)
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
NOP, GA = 241, 249
ECHO, SGA, TTYPE, NAWS = 1, 3, 24, 31
TTYPE_IS, TTYPE_SEND = 0, 1


class _StreamProcess(QObject):
    """Base comune: un thread legge, il testo arriva nel thread dell'interfaccia a blocchi."""

    output = Signal(str)
    finished = Signal(int)
    _chunk = Signal(str)
    _eof = Signal(int)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf: list = []
        self._flush = QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(12)
        self._flush.timeout.connect(self._emit)
        self._chunk.connect(self._got)
        self._eof.connect(self._closed)
        self._thread: Optional[threading.Thread] = None
        self._alive = False
        self._opened = False
        self._pending_input = bytearray()  # tasti premuti mentre la connessione si sta ancora aprendo
        self._lock = threading.Lock()
        self.cols, self.rows = 100, 30

    @property
    def running(self) -> bool:
        return self._alive

    # -- da implementare -------------------------------------------------------------------------
    def _open(self) -> None:
        raise NotImplementedError

    def _read_once(self) -> bytes:
        raise NotImplementedError

    def _send_raw(self, data: bytes) -> None:
        raise NotImplementedError

    def _close_transport(self) -> None:
        raise NotImplementedError

    def _filter(self, data: bytes) -> bytes:
        return data

    # -- comune ----------------------------------------------------------------------------------
    def start(self, argv=None, cwd=None, env=None) -> None:  # noqa: ANN001 (stessa firma di PtyProcess)
        self.stop()
        self._decoder.reset()
        self._opened = False
        self._pending_input.clear()
        self._alive = True
        self._thread = threading.Thread(target=self._run, name=type(self).__name__, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        code = 0
        try:
            self._open()
            with self._lock:
                self._opened = True
                if self._pending_input:
                    self._send_raw(bytes(self._pending_input))
                    self._pending_input.clear()
            while self._alive:
                data = self._read_once()
                if data is None:
                    continue
                if not data:
                    break
                data = self._filter(data)
                if data:
                    self._chunk.emit(self._decoder.decode(data))
        except Exception as exc:  # noqa: BLE001
            if self._alive:
                self._chunk.emit("\r\n" + self._error_text(exc) + "\r\n")
                code = 1
        finally:
            try:
                self._close_transport()
            except Exception:  # noqa: BLE001
                pass
            self._eof.emit(code)

    def _error_text(self, exc: Exception) -> str:
        return str(exc)

    def _got(self, text: str) -> None:
        self._buf.append(text)
        if not self._flush.isActive():
            self._flush.start()

    def _emit(self) -> None:
        if self._buf:
            text, self._buf = "".join(self._buf), []
            self.output.emit(text)

    def _closed(self, code: int) -> None:
        self._flush.stop()
        self._emit()
        was_alive = self._alive
        self._alive = False
        if was_alive or code:
            self.finished.emit(code)

    def write(self, text: str) -> None:
        if not self._alive:
            return
        try:
            with self._lock:
                if self._opened:
                    self._send_raw(self._encode_input(text))
                else:
                    self._pending_input += self._encode_input(text)
        except OSError:
            pass

    def _encode_input(self, text: str) -> bytes:
        return text.encode("utf-8")

    def paste(self, text: str) -> None:
        # Gli apparati non conoscono il «bracketed paste»: le righe vanno come se fossero digitate.
        self.write(text.replace("\r\n", "\r").replace("\n", "\r"))

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = max(cols, 2), max(rows, 2)

    def stop(self) -> None:
        if not self._alive:
            return
        self._alive = False
        try:
            self._close_transport()
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None
        self._emit()
        self.finished.emit(0)


class TelnetProcess(_StreamProcess):
    """Client Telnet: eco e soppressione del go-ahead dal server, dimensioni della finestra (NAWS) e tipo
    di terminale (xterm-256color) quando il server li chiede; tutte le altre opzioni rifiutate."""

    def __init__(self, host: str, port: int = 23, timeout: float = 12.0, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.host, self.port, self.timeout = host, int(port or 23), timeout
        self.sock: Optional[socket.socket] = None
        self._state = 0  # 0 dati, 1 dopo IAC, 2 dopo comando (aspetta opzione), 3 in SB, 4 IAC in SB
        self._command = 0
        self._sb: bytearray = bytearray()
        self._naws = False
        self._remote_echo = False
        self._sent: set = set()  # (comando, opzione) già mandati: niente risposte ripetute (RFC 854)

    def _open(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(0.5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sent = {(DO, SGA), (WILL, NAWS), (WILL, TTYPE)}
        self._send_raw(bytes([IAC, DO, SGA, IAC, WILL, NAWS, IAC, WILL, TTYPE]))

    def _read_once(self) -> Optional[bytes]:
        try:
            return self.sock.recv(65536)
        except socket.timeout:
            return None

    def _send_raw(self, data: bytes) -> None:
        if self.sock is not None:
            self.sock.sendall(data)

    def _close_transport(self) -> None:
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    def _error_text(self, exc: Exception) -> str:
        if isinstance(exc, socket.timeout):
            return tr("Telnet: {host} non risponde (timeout).").format(host=self.host)
        if isinstance(exc, ConnectionRefusedError):
            return tr("Telnet: connessione rifiutata da {host}:{port}.").format(host=self.host, port=self.port)
        if isinstance(exc, socket.gaierror):
            return tr("Telnet: nome «{host}» non trovato.").format(host=self.host)
        return tr("Telnet: {error}").format(error=exc)

    def write(self, text: str) -> None:
        super().write(text)
        if self._alive and not self._remote_echo:
            # Il server non fa l'eco (non ha detto WILL ECHO): lo fa il terminale, come PuTTY.
            self._got(text.replace("\r", "\r\n").replace("\x7f", "\b \b"))

    def _encode_input(self, text: str) -> bytes:
        data = text.encode("utf-8").replace(bytes([IAC]), bytes([IAC, IAC]))
        # Invio: CR LF, come PuTTY («Telnet New Line»); un CR da solo va seguito da NUL (RFC 854).
        return data.replace(b"\r\n", b"\r").replace(b"\r", b"\r\n")

    def _send_naws(self) -> None:
        payload = bytes([self.cols >> 8 & 0xFF, self.cols & 0xFF, self.rows >> 8 & 0xFF, self.rows & 0xFF])
        payload = payload.replace(bytes([IAC]), bytes([IAC, IAC]))
        self._send_raw(bytes([IAC, SB, NAWS]) + payload + bytes([IAC, SE]))

    def resize(self, cols: int, rows: int) -> None:
        super().resize(cols, rows)
        if self._naws and self._alive:
            try:
                with self._lock:
                    self._send_naws()
            except OSError:
                pass

    def _negotiate(self, command: int, option: int) -> None:
        reply = None
        if command == WILL:
            if option == ECHO:
                self._remote_echo = True
            reply = DO if option in (ECHO, SGA) else DONT
        elif command == WONT:
            if option == ECHO:
                self._remote_echo = False
            reply = DONT
        elif command == DO:
            if option == NAWS:
                self._naws = True
                with self._lock:
                    if (WILL, NAWS) not in self._sent:
                        self._sent.add((WILL, NAWS))
                        self._send_raw(bytes([IAC, WILL, NAWS]))
                    self._send_naws()
                return
            reply = WILL if option in (TTYPE, SGA) else WONT
        elif command == DONT:
            if option == NAWS:
                self._naws = False
            reply = WONT
        if reply is not None and (reply, option) not in self._sent:
            self._sent.add((reply, option))
            self._sent.discard(({DO: DONT, DONT: DO, WILL: WONT, WONT: WILL}[reply], option))
            with self._lock:
                self._send_raw(bytes([IAC, reply, option]))

    def _subnegotiation(self, data: bytes) -> None:
        if len(data) >= 2 and data[0] == TTYPE and data[1] == TTYPE_SEND:
            with self._lock:
                self._send_raw(bytes([IAC, SB, TTYPE, TTYPE_IS]) + b"XTERM-256COLOR" + bytes([IAC, SE]))

    def _filter(self, data: bytes) -> bytes:
        out = bytearray()
        for byte in data:
            if self._state == 0:
                if byte == IAC:
                    self._state = 1
                else:
                    out.append(byte)
            elif self._state == 1:
                if byte == IAC:
                    out.append(IAC)
                    self._state = 0
                elif byte in (WILL, WONT, DO, DONT):
                    self._command = byte
                    self._state = 2
                elif byte == SB:
                    self._sb = bytearray()
                    self._state = 3
                else:  # NOP, GA, AYT...: niente da fare
                    self._state = 0
            elif self._state == 2:
                self._negotiate(self._command, byte)
                self._state = 0
            elif self._state == 3:
                if byte == IAC:
                    self._state = 4
                else:
                    self._sb.append(byte)
            elif self._state == 4:
                if byte == SE:
                    self._subnegotiation(bytes(self._sb))
                    self._state = 0
                else:
                    self._sb.append(byte)
                    self._state = 3
        return bytes(out)


class SerialProcess(_StreamProcess):
    """Porta seriale (COM3, /dev/ttyUSB0) con pyserial. Accetta anche gli URL di pyserial (loop://, rfc2217://)."""

    def __init__(self, port: str, baudrate: int = 115200, bytesize: str = "8", parity: str = "N",
                 stopbits: str = "1", parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.port_name = port
        self.baudrate = int(baudrate or 115200)
        self.bytesize = int(bytesize or 8)
        self.parity = (parity or "N").upper()[:1]
        self.stopbits = float(stopbits or 1)
        self.serial = None

    def _open(self) -> None:
        import serial  # pyserial

        self.serial = serial.serial_for_url(self.port_name, baudrate=self.baudrate, bytesize=self.bytesize,
                                            parity=self.parity, stopbits=self.stopbits, timeout=0.2)
        self._chunk.emit("\x1b[2m" + tr("[connesso a {port}, {baud} {bits}{parity}{stop} — scrivi pure]").format(
            port=self.port_name, baud=self.baudrate, bits=self.bytesize, parity=self.parity,
            stop=f"{self.stopbits:g}") + "\x1b[0m\r\n")

    def _read_once(self) -> Optional[bytes]:
        data = self.serial.read(max(1, self.serial.in_waiting or 1))
        return data if data else None

    def _send_raw(self, data: bytes) -> None:
        if self.serial is not None:
            self.serial.write(data)

    def _close_transport(self) -> None:
        if self.serial is not None:
            try:
                self.serial.close()
            finally:
                self.serial = None

    def _error_text(self, exc: Exception) -> str:
        text = str(exc)
        if "PermissionError" in text or "Access is denied" in text or "Accesso negato" in text:
            return tr("{port} è già usata da un altro programma (o manca il permesso).").format(port=self.port_name)
        if "could not open port" in text or "FileNotFoundError" in text:
            return tr("{port} non esiste: controlla il cavo e il numero della porta.").format(port=self.port_name)
        return tr("Porta seriale {port}: {error}").format(port=self.port_name, error=text)

    def send_break(self) -> None:
        if self.serial is not None:
            try:
                self.serial.send_break(0.25)
            except Exception:  # noqa: BLE001
                pass


def list_serial_ports() -> list:
    """[(porta, descrizione)] con pyserial: COM3 «USB Serial Port (COM3)», /dev/ttyUSB0 «FT232R…»."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    ports = []
    for info in sorted(list_ports.comports(), key=lambda p: p.device):
        description = info.description if info.description and info.description != "n/a" else ""
        ports.append((info.device, description))
    return ports
