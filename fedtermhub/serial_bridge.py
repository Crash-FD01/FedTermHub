# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bridge seriale interattivo per FedTermHub.

Esegue una sessione seriale dentro un terminale VTE. Uscita rapida: Ctrl+]
"""
from __future__ import annotations

import argparse
import os
import select
import sys
import termios
import threading
import tty
from typing import Optional

try:
    from .i18n import tr
except ImportError:  # avviato come script dal terminale della sessione seriale
    from i18n import tr

try:
    import serial
except ImportError:  # pragma: no cover - dipendenza runtime
    serial = None


def main() -> int:
    parser = argparse.ArgumentParser(description="Terminale seriale FedTermHub")
    parser.add_argument("--device", required=True, help="Device seriale, es. /dev/ttyUSB0")
    parser.add_argument("--baud", default="115200", help="Baud rate")
    parser.add_argument("--bytesize", default="8", choices=["5", "6", "7", "8"], help="Data bits")
    parser.add_argument("--parity", default="N", choices=["N", "E", "O", "M", "S"], help="Parità: N/E/O/M/S")
    parser.add_argument("--stopbits", default="1", choices=["1", "1.5", "2"], help="Stop bits")
    args = parser.parse_args()

    if serial is None:
        print(tr("python3-pyserial non installato. Su Fedora: sudo dnf install python3-pyserial"), file=sys.stderr)
        return 2

    baud = int(args.baud)
    bytesize = int(args.bytesize)
    parity_map = {
        "N": serial.PARITY_NONE,
        "E": serial.PARITY_EVEN,
        "O": serial.PARITY_ODD,
        "M": serial.PARITY_MARK,
        "S": serial.PARITY_SPACE,
    }
    stopbits_map = {
        "1": serial.STOPBITS_ONE,
        "1.5": serial.STOPBITS_ONE_POINT_FIVE,
        "2": serial.STOPBITS_TWO,
    }

    try:
        ser = serial.Serial(
            port=args.device,
            baudrate=baud,
            bytesize=bytesize,
            parity=parity_map[args.parity],
            stopbits=stopbits_map[args.stopbits],
            timeout=0.05,
            write_timeout=1,
        )
    except Exception as exc:  # noqa: BLE001
        print(tr("Errore apertura seriale {device}: {exc}").format(device=args.device, exc=exc), file=sys.stderr)
        print(tr("Suggerimento: controlla i permessi con: ls -l {device}").format(device=args.device), file=sys.stderr)
        return 1

    old_attrs: Optional[list] = None
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()

    if sys.stdin.isatty():
        old_attrs = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)

    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                data = ser.read(4096)
                if data:
                    os.write(stdout_fd, data)
            except Exception as exc:  # noqa: BLE001
                try:
                    os.write(stdout_fd, ("\r\n[" + tr("Errore lettura seriale: {exc}").format(exc=exc) + "]\r\n").encode())
                except OSError:
                    pass
                stop.set()
                break

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    try:
        os.write(
            stdout_fd,
            (
                tr("Connesso a {device} @ {baud} {framing}. Premi Ctrl+] per uscire.").format(
                    device=args.device, baud=baud, framing=f"{bytesize}{args.parity}{args.stopbits}"
                )
                + "\r\n"
            ).encode(),
        )
        while not stop.is_set():
            readable, _, _ = select.select([stdin_fd], [], [], 0.1)
            if not readable:
                continue
            data = os.read(stdin_fd, 1024)
            if not data:
                break
            escape_index = data.find(b"\x1d")  # Ctrl+]
            if escape_index >= 0:
                # Invia comunque i byte che precedono la sequenza di uscita,
                # così un incolla contenente 0x1d non perde dati.
                if escape_index > 0:
                    ser.write(data[:escape_index])
                break
            ser.write(data)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        try:
            ser.close()
        except Exception:
            pass
        if old_attrs is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        print("\r\n" + tr("Seriale chiusa."))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
