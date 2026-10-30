#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prova dal vivo il motore RDP nativo installato: si collega a un server RDP e aspetta un frame vero.

    python3 tests/rdp_engine_probe.py /usr/libexec/fedtermhub/fedtermrdp-native 127.0.0.1

Lo usa la CI dopo aver installato il pacchetto su ogni distribuzione, con un server xrdp locale: con
credenziali sbagliate xrdp mostra comunque la sua schermata di accesso, quindi arrivano frame veri
senza bisogno di un utente. Dimostra che il motore parte, che trova la sua FreeRDP, che TLS funziona
con l'OpenSSL della distribuzione e che il framebuffer condiviso viene riempito.
Non è un test_*.py: serve un server RDP, che sotto i test normali non c'è.
"""
from __future__ import annotations

import mmap
import os
import struct
import subprocess
import sys
import time

HEADER_SIZE = 1024
CAPACITY = 1920 * 1200 * 4
MAGIC = 0x46544852  # come in native_rdp.py


def main() -> int:
    helper, host = sys.argv[1], sys.argv[2]
    frame_fd = os.memfd_create("probe-frame", 0)
    os.ftruncate(frame_fd, HEADER_SIZE + CAPACITY)
    frame = mmap.mmap(frame_fd, HEADER_SIZE + CAPACITY)
    options = [f"/v:{host}", "/u:nobody", "/p:wrong", "/d:", "/cert:ignore", "/size:1024x768",
               "/bpp:32", "/gdi:sw", "/gfx:progressive", "/sec:tls"]
    config_fd = os.memfd_create("probe-config", 0)
    os.write(config_fd, ("\n".join(options) + "\n").encode())
    os.lseek(config_fd, 0, os.SEEK_SET)
    command_read, _command_write = os.pipe()
    process = subprocess.Popen(
        [helper, "--config-fd", str(config_fd), "--frame-fd", str(frame_fd), "--command-fd", str(command_read)],
        pass_fds=(config_fd, frame_fd, command_read), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env={**os.environ, "LC_ALL": "C"},
    )
    ok = False
    deadline = time.time() + 40
    while time.time() < deadline and process.poll() is None:
        values = struct.unpack_from("<16I", frame, 0)
        seq, width, height, stride = values[2], values[4], values[5], values[6]
        if values[0] == MAGIC and seq and seq % 2 == 0 and width and height and stride >= width * 4:
            data = frame[HEADER_SIZE:HEADER_SIZE + stride * height]
            painted = sum(1 for i in range(0, len(data), 4096) if data[i:i + 3] != b"\0\0\0")
            if painted > 20:
                print(f"frame {width}x{height} ricevuto (sequenza {seq}, {painted} campioni non neri)")
                ok = True
                break
        time.sleep(0.2)
    process.terminate()
    try:
        output = process.communicate(timeout=5)[0]
    except subprocess.TimeoutExpired:
        process.kill()
        output = process.communicate()[0]
    if not ok:
        print("nessun frame dal server RDP; ultime righe del motore:")
        print("\n".join(output.splitlines()[-25:]))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
