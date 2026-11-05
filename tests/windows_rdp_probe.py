# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sonda del componente RDP di Windows (solo CI Windows, per capire cosa vuole Connect()).

    python tests/windows_rdp_probe.py "host;DOMINIO\\utente;password"

Prova alcune varianti (cosa si imposta e quando si chiama Connect) e stampa gli eventi di ognuna.
"""
from __future__ import annotations

import sys
import time

from PySide6.QtAxContainer import QAxWidget
from PySide6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget

CLSID = "{8B918B82-7985-4C24-89DF-C33AD2BBFBCD}"


def pump(app: QApplication, seconds: float, until=None) -> None:  # noqa: ANN001
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if until and until():
            return
        time.sleep(0.02)


def main() -> int:
    host, user, password = (sys.argv[1].split(";", 2) + ["", "", ""])[:3]
    domain = ""
    if "\\" in user:
        domain, user = user.split("\\", 1)
    app = QApplication(sys.argv[:1])
    win = QMainWindow()
    central = QWidget()
    layout = QVBoxLayout(central)
    win.setCentralWidget(central)
    win.resize(1100, 800)
    win.show()
    pump(app, 1)

    def variant(name: str, show_first: bool, setup) -> None:  # noqa: ANN001
        events = []
        ax = QAxWidget()
        ok = ax.setControl(CLSID)
        ax.signal.connect(lambda n, argc, argv: events.append(str(n).split("(")[0]))
        ax.exception.connect(lambda code, src, desc, _h: events.append(f"exception {code & 0xFFFFFFFF:#x} {desc}"))
        layout.addWidget(ax)
        if show_first:
            ax.show()
            pump(app, 1)
        try:
            setup(ax)
            result = ax.dynamicCall("Connect()")
        except Exception as exc:  # noqa: BLE001
            result = f"eccezione Python {exc}"
        pump(app, 40, lambda: any(e in ("OnLoginComplete", "OnDisconnected") for e in events))
        print(f"[{name}] setControl={ok} Connect={result!r} hwnd={int(ax.winId())} eventi={events}", flush=True)
        if "OnLoginComplete" in events:
            ax.dynamicCall("Disconnect()")
            pump(app, 10, lambda: "OnDisconnected" in events)
        ax.hide()
        ax.deleteLater()
        pump(app, 2)

    def server_only(ax) -> None:  # noqa: ANN001
        print("  Server via setProperty:", ax.setProperty("Server", host), flush=True)
        ax.dynamicCall("Server", host)
        print("  Server letto:", ax.property("Server"), flush=True)

    def with_credentials(ax) -> None:  # noqa: ANN001
        server_only(ax)
        ax.dynamicCall("UserName", user)
        ax.dynamicCall("Domain", domain)
        adv = ax.querySubObject("AdvancedSettings9")
        print("  ClearTextPassword setProperty:", adv.setProperty("ClearTextPassword", password), flush=True)
        adv.setProperty("EnableCredSspSupport", True)
        adv.setProperty("AuthenticationLevel", 0)

    def with_size(ax) -> None:  # noqa: ANN001
        with_credentials(ax)
        ax.dynamicCall("DesktopWidth", 1024)
        ax.dynamicCall("DesktopHeight", 768)
        ax.dynamicCall("ColorDepth", 32)
        print("  DesktopWidth letto:", ax.property("DesktopWidth"), flush=True)

    variant("solo server, subito", False, server_only)
    variant("solo server, dopo show", True, server_only)
    variant("credenziali, dopo show", True, with_credentials)
    variant("credenziali e dimensioni, dopo show", True, with_size)
    variant("credenziali e dimensioni, subito", False, with_size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
