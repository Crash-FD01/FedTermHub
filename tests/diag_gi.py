# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Diagnostica temporanea: callback con user_data in PyGObject su openSUSE Leap 16."""
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

print("pygobject", gi.__version__, "gtk", Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version(), flush=True)
calls = []
store = Gtk.ListStore(str)
for t in ("a", "b"):
    store.append([t])
view = Gtk.TreeView(model=store)
col = Gtk.TreeViewColumn("x")
cell = Gtk.CellRendererText()
col.pack_start(cell, True)
col.set_cell_data_func(cell, lambda *a: calls.append(("cell", len(a))))
view.append_column(col)
lb = Gtk.ListBox()
lb.set_header_func(lambda *a: calls.append(("hdr-none", len(a))), None)
lb2 = Gtk.ListBox()
lb2.set_header_func(lambda *a: calls.append(("hdr", len(a))))
for box in (lb, lb2):
    for _ in range(2):
        box.add(Gtk.Label(label="r"))
win = Gtk.Window()
vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
for w in (view, lb, lb2):
    vb.add(w)
win.add(vb)
win.show_all()
GLib.idle_add(lambda: calls.append(("idle", 0)) or False)
GLib.timeout_add(500, Gtk.main_quit)
Gtk.main()
print("chiamate:", sorted(set(calls)), flush=True)
