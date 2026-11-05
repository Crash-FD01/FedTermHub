# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Schermate della versione Windows (interfaccia Qt) con gli stessi dati INVENTATI di tools/screenshots.py.

Serve a confrontare le due interfacce una accanto all'altra. Non tocca la configurazione dell'utente.

    xvfb-run -a -s "-screen 0 1400x880x24" python3 tools/screenshots_qt.py dark  qt-principale-scuro.png
    xvfb-run -a -s "-screen 0 1400x880x24" python3 tools/screenshots_qt.py light qt-principale-chiaro.png
    xvfb-run -a -s "-screen 0 1400x880x24" python3 tools/screenshots_qt.py dark  qt-menu-scuro.png menu

Su Windows funziona anche senza xvfb-run.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

TEMA, USCITA = sys.argv[1], Path(sys.argv[2]).resolve()
VISTA = sys.argv[3] if len(sys.argv) > 3 else "sessioni"
REPO = Path(__file__).resolve().parents[1]
tmp = Path(tempfile.mkdtemp())
os.environ.update(XDG_CONFIG_HOME=str(tmp / "c"), XDG_CACHE_HOME=str(tmp / "k"), APPDATA=str(tmp / "c"),
                  LOCALAPPDATA=str(tmp / "k"))
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
sys.path.insert(0, str(REPO))

from fedtermhub import model  # noqa: E402

model.ensure_private_config_dir()
model.SETTINGS_FILE.write_text(json.dumps({"theme": TEMA, "window_width": 1360, "window_height": 840}))
S, F = model.Session, model.FolderNode
albero = [
    F(name="Clienti", color="#4aa3df", children=[
        F(name="Studio Rossi", color="#56c7d9", children=[
            S(name="fw-perimetrale", protocol="ssh", host="10.20.0.1", user="admin"),
            S(name="srv-dc01", protocol="rdp", host="10.20.0.10", user="Administrator"),
            S(name="nas-backup", protocol="sftp", host="10.20.0.30", user="backup"),
        ]),
        F(name="Officine Bianchi", color="#f2c94c", children=[
            S(name="web01", protocol="ssh", host="web01.example.lan", user="admin"),
            S(name="db01", protocol="ssh", host="db01.example.lan", user="admin"),
            S(name="terminal-server", protocol="rdp", host="ts.example.lan"),
        ]),
    ]),
    F(name="Infrastruttura", color="#9b8afb", children=[
        S(name="proxmox-node1", protocol="ssh", host="pve1.example.lan", user="root"),
        S(name="switch-core", protocol="serial", host="COM3", port="9600"),
        S(name="kvm-console", protocol="vnc", host="kvm.example.lan"),
    ]),
    S(name="Terminale locale", protocol="local"),
]
store = model.SessionStore(model.SESSIONS_FILE)
store.nodes = [F(name=model.HOME_SESSION_NAME, color=model.HOME_SESSION_COLOR, children=albero)]
store.save()

# L'uscita finta del terminale: la stessa delle schermate della versione Linux.
G, B, N, W = "\\033[1;32m", "\\033[1;34m", "\\033[0m", "\\033[1;37m"
prompt = f"{G}admin@web01{N}:{B}%s{N}$ "
righe = [
    "Last login: Thu Oct 29 09:12:44 2026 from 10.0.0.15",
    prompt % "~" + "cd /etc/nginx",
    prompt % "/etc/nginx" + "sudo nginx -t",
    "nginx: the configuration file /etc/nginx/nginx.conf syntax is ok",
    "nginx: configuration file /etc/nginx/nginx.conf test is successful",
    prompt % "/etc/nginx" + "systemctl status nginx --no-pager | head -4",
    f"{G}●{N} nginx.service - A high performance web server and a reverse proxy server",
    "     Loaded: loaded (/lib/systemd/system/nginx.service; enabled; preset: enabled)",
    f"     Active: {G}active (running){N} since Thu 2026-10-29 08:01:17 CET; 1h 11min ago",
    "   Main PID: 812 (nginx)",
    prompt % "/etc/nginx" + "df -h / /var",
    "Filesystem      Size  Used Avail Use% Mounted on",
    "/dev/vda1        40G   14G   24G  37% /",
    "/dev/vdb1       200G   61G  130G  32% /var",
    prompt % "/etc/nginx",
]
script = "printf '%b' '" + "\\r\\n".join(r.replace("'", "'\\''") for r in righe) + "'; exec sleep 600"

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fedtermhub.qtui import connect, theme  # noqa: E402
from fedtermhub.qtui.main_window import MainWindow  # noqa: E402

app = QApplication([])
theme.apply(app, TEMA)
win = MainWindow()
win.show()


def prepara() -> None:
    win.refresh_tree()
    win.tree.expandAll()
    path = next(p for p, s, _f in win.store.iter_sessions() if s.name == "web01")
    win.refresh_tree(select=path)
    win.add_terminal_tab(connect.Launch("web01", ["bash", "-c", script], {}, close_on_success=False), protocol="ssh")
    if VISTA == "menu":
        QTimer.singleShot(2500, lambda: win.ribbon_buttons["sessions"].showMenu())
    QTimer.singleShot(5000, scatta)


def scatta() -> None:
    win.open_label.setText("Aperto: web01")
    screen = app.primaryScreen()
    pixmap = screen.grabWindow(0) if VISTA == "menu" else win.grab()
    if VISTA != "menu":
        pixmap = screen.grabWindow(0, win.geometry().x(), win.geometry().y(), win.width(), win.height())
    pixmap.save(str(USCITA))
    print(USCITA)
    os._exit(0)  # niente chiusura ordinata: chiederebbe conferma per la scheda aperta


QTimer.singleShot(800, prepara)
app.exec()
os._exit(0)
