# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Diagnostica temporanea: Vte.Terminal.spawn_async e user_data in PyGObject su openSUSE Leap 16."""
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import GLib, Gtk, Vte  # noqa: E402

print("pygobject", gi.__version__, "vte", Vte.get_major_version(), Vte.get_minor_version(), flush=True)
results = {}


def run(name, extra):  # noqa: ANN001
    term = Vte.Terminal()

    def done(*args):  # noqa: ANN002
        results[name] = f"callback con {len(args)} argomenti, pid={args[1] if len(args) > 1 else '?'}"

    print("--", name, flush=True)
    try:
        term.spawn_async(Vte.PtyFlags.DEFAULT, None, ["/bin/sh", "-c", "exit 0"], None, GLib.SpawnFlags.DEFAULT,
                         None, None, -1, None, done, *extra)
        results.setdefault(name, "chiamata accettata, callback non (ancora) arrivato")
    except TypeError as exc:
        results[name] = f"TypeError alla chiamata: {exc}"
    loop = GLib.MainLoop()
    GLib.timeout_add(1500, loop.quit)
    loop.run()


run("user_data None", [None])
run("senza user_data", [])
for k, v in results.items():
    print(f"{k}: {v}", flush=True)
