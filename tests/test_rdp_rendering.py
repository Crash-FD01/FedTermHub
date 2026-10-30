#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Smoke test del renderer RDP stabile a frame completo."""
from __future__ import annotations
import mmap, os, struct, sys
from pathlib import Path
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk
PROJECT_ROOT = Path(__file__).resolve().parents[1]
# I test controllano i testi originali in italiano, qualunque sia la lingua del sistema.
os.environ["FEDTERMHUB_LANGUAGE"] = "it"
sys.path.insert(0, str(PROJECT_ROOT / "fedtermhub"))
import native_rdp

def main() -> int:
    if Gdk.Display.get_default() is None:
        print("RDP_RENDERING_SKIP: nessun display GTK")
        return 0
    width, height, stride = 8, 4, 32
    size = stride * height
    mapping = mmap.mmap(-1, native_rdp.HEADER_SIZE + size)
    tab = native_rdp.NativeRdpTab("Rendering test", ["/v:127.0.0.1"], None,
                                 autostart_on_map=False, max_width=1920, max_height=1080)
    tab.frame_map = mapping
    tab.frame_capacity = size
    frame = bytes(index % 251 for index in range(size))
    mapping[native_rdp.HEADER_SIZE:native_rdp.HEADER_SIZE + size] = frame
    struct.pack_into("<16I", mapping, 0, native_rdp.MAGIC, 1, 2,
                     native_rdp.STATE_CONNECTED, width, height, stride, size,
                     0, 0, 0, 0, 0, 0, 0, 0)
    assert tab._poll_frame()
    assert tab.frame_data == bytearray(frame)
    assert tab.surface is not None
    assert tab.last_seq == 2
    # Il frame mostrato è una copia coerente: modificare la mmap non altera il frame corrente.
    mapping[native_rdp.HEADER_SIZE:native_rdp.HEADER_SIZE + 4] = b"ZZZZ"
    assert tab.frame_data[:4] != b"ZZZZ"
    tab.frame_map = None
    mapping.close()
    tab.destroy()
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)
    print("RDP_RENDERING_STABLE_OK")
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
