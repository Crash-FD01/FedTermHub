# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sonda del componente RDP di Windows (solo CI Windows, per capire come pilotarlo da Python).

    python tests/windows_rdp_probe.py "host;DOMINIO\\utente;password"

Parte A: QAxWidget di PySide6 (le proprietà del componente si vedono? Connect() parte?).
Parte B: il componente ospitato con ATL (AtlAxWin) dentro un QWidget e pilotato con comtypes.
"""
from __future__ import annotations

import sys
import time
import traceback

from PySide6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget

CLSID = "{8B918B82-7985-4C24-89DF-C33AD2BBFBCD}"


def pump(app: QApplication, seconds: float, until=None) -> None:  # noqa: ANN001
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if until and until():
            return
        time.sleep(0.02)


def part_a(app, layout, host) -> None:  # noqa: ANN001
    from PySide6.QtAxContainer import QAxWidget

    ax = QAxWidget()
    print("A setControl:", ax.setControl(CLSID), flush=True)
    layout.addWidget(ax)
    pump(app, 1)
    doc = ax.generateDocumentation()
    print("A documentazione:", len(doc), "caratteri", flush=True)
    for line in doc.splitlines():
        if any(k in line for k in ("Server", "Connect(", "DesktopWidth", "ClearTextPassword")):
            print("   ", line.strip()[:200], flush=True)
    print("A metaObject:", ax.metaObject().className(), ax.metaObject().propertyCount(), flush=True)
    print("A dynamicCall set:", ax.dynamicCall("Server", host), flush=True)
    print("A dynamicCall get:", repr(ax.dynamicCall("Server")), flush=True)
    print("A dynamicCall setter:", repr(ax.dynamicCall("SetServer(QString)", host)), flush=True)
    try:
        bag = ax.propertyBag()
        print("A propertyBag Server:", repr(bag.get("Server")), "voci:", len(bag), flush=True)
        ax.setPropertyBag({"Server": host})
        print("A dopo setPropertyBag:", repr(ax.propertyBag().get("Server")), flush=True)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    ax.hide()
    ax.deleteLater()
    pump(app, 1)


def part_b(app, win, layout, host, user, domain, password) -> None:  # noqa: ANN001
    import ctypes
    from ctypes import wintypes

    import comtypes
    import comtypes.client

    holder = QWidget()
    holder.setMinimumSize(1024, 768)
    layout.addWidget(holder)
    pump(app, 1)
    atl = ctypes.WinDLL("atl")
    user32 = ctypes.WinDLL("user32")
    print("B AtlAxWinInit:", atl.AtlAxWinInit(), flush=True)
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    WS_CHILD, WS_VISIBLE, WS_CLIPCHILDREN = 0x40000000, 0x10000000, 0x02000000
    hwnd = user32.CreateWindowExW(0, "AtlAxWin", CLSID, WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN, 0, 0, 1024, 768,
                                  int(holder.winId()), None, None, None)
    print("B hwnd:", hwnd, flush=True)
    punk = ctypes.POINTER(comtypes.IUnknown)()
    atl.AtlAxGetControl.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.POINTER(comtypes.IUnknown))]
    print("B AtlAxGetControl:", atl.AtlAxGetControl(hwnd, ctypes.byref(punk)), bool(punk), flush=True)
    lib = comtypes.client.GetModule(r"C:\Windows\System32\mstscax.dll")
    names = [n for n in dir(lib) if n.startswith("IMsRdpClient") and n[12:].isdigit()]
    print("B interfacce:", sorted(names, key=lambda n: int(n[12:]))[-3:], flush=True)
    rdp = punk.QueryInterface(lib.IMsRdpClient9)
    rdp.Server = host
    rdp.UserName = user
    rdp.Domain = domain
    rdp.DesktopWidth = 1024
    rdp.DesktopHeight = 768
    rdp.ColorDepth = 32
    adv = rdp.AdvancedSettings9
    adv.EnableCredSspSupport = True
    adv.AuthenticationLevel = 0
    punk.QueryInterface(lib.IMsTscNonScriptable).ClearTextPassword = password
    print("B Server letto:", rdp.Server, flush=True)
    events = []

    class Sink:
        def OnConnecting(self, *a):  # noqa: N802, ANN002
            events.append("OnConnecting")

        def OnConnected(self, *a):  # noqa: N802, ANN002
            events.append("OnConnected")

        def OnLoginComplete(self, *a):  # noqa: N802, ANN002
            events.append("OnLoginComplete")

        def OnDisconnected(self, *a):  # noqa: N802, ANN002
            events.append(f"OnDisconnected {a[-1] if a else ''}")

        def OnFatalError(self, *a):  # noqa: N802, ANN002
            events.append(f"OnFatalError {a}")

        def OnLogonError(self, *a):  # noqa: N802, ANN002
            events.append(f"OnLogonError {a}")

    conn = comtypes.client.GetEvents(rdp, Sink(), interface=lib.IMsTscAxEvents)
    rdp.Connect()
    pump(app, 60, lambda: any(e.startswith(("OnLoginComplete", "OnDisconnected")) for e in events))
    print("B eventi:", events, "Connected:", rdp.Connected, flush=True)
    if "OnLoginComplete" in events:
        pump(app, 5)
        holder.grab().save("out/probe-rdp.png")
        rdp.Disconnect()
        pump(app, 10, lambda: any(e.startswith("OnDisconnected") for e in events))
        print("B dopo Disconnect:", events, flush=True)
    del conn


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
    win.resize(1100, 850)
    win.show()
    pump(app, 1)
    for part in (lambda: part_a(app, layout, host), lambda: part_b(app, win, layout, host, user, domain, password)):
        try:
            part()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
