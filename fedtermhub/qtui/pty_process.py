# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Un programma in un terminale vero (pseudo-terminale), letto e scritto da Qt.

Le sessioni SSH, Telnet e le shell locali dell'interfaccia Qt girano qui: hanno bisogno di un TTY con
dimensioni, colori e tasti come in un terminale normale. Linux: os.openpty. Windows: ConPTY tramite
pywinpty (WinPtyProcess, stessa interfaccia). Ripreso da Helpix (stesso autore, stessa licenza).
"""
from __future__ import annotations

import codecs
import errno
import os
import shutil
import signal
import struct
import subprocess
import sys
import threading

from PySide6.QtCore import QObject, QSocketNotifier, QTimer, Signal

if sys.platform != "win32":
    import fcntl
    import termios


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0))


def _child_setup() -> None:  # nel processo figlio, dopo setsid: il PTY diventa il terminale di controllo
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class _PosixPtyProcess(QObject):
    output = Signal(str)     # testo dal programma (già decodificato, in blocchi)
    finished = Signal(int)   # codice di uscita

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: subprocess.Popen | None = None
        self.fd: int | None = None
        self._notifier: QSocketNotifier | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf: list[str] = []
        self._flush = QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(12)  # raggruppa l'output: meno chiamate verso la pagina del terminale
        self._flush.timeout.connect(self._emit)
        self.cols, self.rows = 100, 30

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, argv: list[str], cwd: str, env: dict | None = None) -> None:
        self.stop()
        master, slave = os.openpty()
        _set_size(slave, self.cols, self.rows)
        full_env = dict(os.environ)
        full_env.update(env or {})
        full_env.update(TERM="xterm-256color", COLORTERM="truecolor")
        # Il programma deve avere il PTY come terminale di controllo (sudo, Ctrl+C, ridimensionamento).
        # `setsid --ctty` (util-linux) lo fa senza eseguire codice Python nel processo figlio, cosa
        # rischiosa con più thread attivi; preexec_fn resta solo come ripiego.
        setsid = shutil.which("setsid")
        try:
            if setsid:
                self.proc = subprocess.Popen([setsid, "--ctty", *argv], cwd=cwd, env=full_env, stdin=slave,
                                             stdout=slave, stderr=slave, close_fds=True)
            else:
                self.proc = subprocess.Popen(argv, cwd=cwd, env=full_env, stdin=slave, stdout=slave, stderr=slave,
                                             start_new_session=True, preexec_fn=_child_setup, close_fds=True)
        except OSError:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        os.set_blocking(master, False)
        self.fd = master
        self._decoder.reset()
        self._notifier = QSocketNotifier(master, QSocketNotifier.Type.Read, self)
        self._notifier.activated.connect(self._read)

    def _read(self, *_):
        if self.fd is None:
            return
        try:
            data = os.read(self.fd, 65536)
        except OSError as exc:
            if exc.errno == errno.EAGAIN:
                return
            data = b""  # EIO: il programma ha chiuso il terminale
        if not data:
            self._closed()
            return
        self._buf.append(self._decoder.decode(data))
        if not self._flush.isActive():
            self._flush.start()

    def _emit(self):
        if self._buf:
            text, self._buf = "".join(self._buf), []
            self.output.emit(text)

    def _closed(self):
        if self._notifier:
            self._notifier.setEnabled(False)
            self._notifier.deleteLater()
            self._notifier = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        self._emit()
        rc = 0
        if self.proc is not None:
            try:
                rc = self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                rc = self.proc.wait()
        self.proc = None
        self.finished.emit(rc)

    def write(self, text: str) -> None:
        self._write_bytes(text.encode("utf-8"))

    def _write_bytes(self, data: bytes) -> None:
        while data and self.fd is not None:
            try:
                n = os.write(self.fd, data)
            except BlockingIOError:  # il programma non sta leggendo: riprovo tra poco con il resto
                QTimer.singleShot(10, lambda rest=data: self._write_bytes(rest))
                return
            except OSError:
                return
            data = data[n:]

    def paste(self, text: str) -> None:
        """Incolla come in un terminale moderno (bracketed paste): il programma non lo esegue riga per riga."""
        self.write("\x1b[200~" + text.replace("\x1b", "") + "\x1b[201~")

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = max(cols, 2), max(rows, 2)
        if self.fd is not None:
            try:
                _set_size(self.fd, self.cols, self.rows)
            except OSError:
                pass
            if self.proc is not None:
                try:
                    os.killpg(self.proc.pid, signal.SIGWINCH)
                except OSError:
                    pass

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGHUP)
                self.proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    pass
        if self.fd is not None or self.proc is not None:
            self._closed()


class WinPtyProcess(QObject):
    """Come _PosixPtyProcess, su Windows: ConPTY (pseudo-console di Windows 10 1809+) tramite pywinpty.

    La lettura è bloccante, quindi avviene in un thread; il testo arriva al thread di Qt con un segnale."""
    output = Signal(str)
    finished = Signal(int)
    _chunk = Signal(str)
    _eof = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = None
        self.cols, self.rows = 100, 30
        self._buf: list[str] = []
        self._flush = QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(12)
        self._flush.timeout.connect(self._emit)
        self._chunk.connect(self._got)
        self._eof.connect(self._closed)
        self._reader: threading.Thread | None = None
        self._generation = 0

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.isalive()

    def start(self, argv: list[str], cwd: str, env: dict | None = None) -> None:
        from winpty import PtyProcess as _ConPty
        self.stop()
        full_env = dict(os.environ)
        full_env.update(env or {})
        full_env.update(TERM="xterm-256color", COLORTERM="truecolor")
        exe = shutil.which(argv[0], path=full_env.get("PATH")) or argv[0]
        cmdline = [exe, *argv[1:]]
        if exe.lower().endswith((".cmd", ".bat")):  # programmi installati con npm (gemini.cmd…): li apre cmd.exe
            cmdline = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", *cmdline]
        self.proc = _ConPty.spawn(cmdline, cwd=cwd, env=full_env, dimensions=(self.rows, self.cols))
        self._generation += 1
        gen, proc = self._generation, self.proc

        def reader():
            while True:
                try:
                    data = proc.read(65536)
                except (EOFError, OSError):
                    break
                if data and gen == self._generation:
                    self._chunk.emit(data)
                elif not proc.isalive():
                    break
            if gen == self._generation:
                self._eof.emit()

        self._reader = threading.Thread(target=reader, name="fedtermhub-conpty", daemon=True)
        self._reader.start()

    def _got(self, text: str):
        self._buf.append(text)
        if not self._flush.isActive():
            self._flush.start()

    def _emit(self):
        if self._buf:
            text, self._buf = "".join(self._buf), []
            self.output.emit(text)

    def _closed(self):
        self._emit()
        proc, self.proc = self.proc, None
        rc = 0
        if proc is not None:
            try:
                rc = int(proc.exitstatus or 0)
            except (TypeError, ValueError):
                rc = 0
            try:
                proc.close()
            except Exception:  # noqa: BLE001 - già chiuso
                pass
        self.finished.emit(rc)

    def write(self, text: str) -> None:
        if self.proc is not None:
            try:
                self.proc.write(text)
            except (EOFError, OSError):
                pass

    def paste(self, text: str) -> None:
        self.write("\x1b[200~" + text.replace("\x1b", "") + "\x1b[201~")

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = max(cols, 2), max(rows, 2)
        if self.proc is not None:
            try:
                self.proc.setwinsize(self.rows, self.cols)
            except (EOFError, OSError):
                pass

    def stop(self) -> None:
        if self.proc is None:
            return
        self._generation += 1  # il thread del processo vecchio non manda più nulla
        proc = self.proc
        try:
            if proc.isalive():
                proc.terminate(force=True)
        except Exception:  # noqa: BLE001
            pass
        self._closed()


PtyProcess = WinPtyProcess if sys.platform == "win32" else _PosixPtyProcess

