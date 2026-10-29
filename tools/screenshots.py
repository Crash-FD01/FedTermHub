# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Rigenera le schermate di docs/screenshots con dati dimostrativi INVENTATI.

Avvia la vera applicazione sotto Xvfb con una configurazione temporanea: le
cartelle, gli host e l'output del terminale sono di fantasia, il pannello dei
file remoti legge da un server simulato. Non tocca ~/.config/fedtermhub.

    xvfb-run -a -s "-screen 0 1400x880x24" python3 tools/screenshots.py . light docs/screenshots/principale-chiaro.png
    xvfb-run -a -s "-screen 0 1400x880x24" python3 tools/screenshots.py . dark  docs/screenshots/principale-scuro.png
    xvfb-run -a -s "-screen 0 1400x880x24" python3 tools/screenshots.py . light docs/screenshots/file-remoti-chiaro.png file
    xvfb-run -a -s "-screen 0 1400x880x24" python3 tools/screenshots.py . dark  docs/screenshots/file-remoti-scuro.png file
    xvfb-run -a -s "-screen 0 1400x880x24" python3 tools/screenshots.py . light docs/screenshots/password-chiaro.png password
    xvfb-run -a -s "-screen 0 1400x880x24" python3 tools/screenshots.py . dark  docs/screenshots/password-scuro.png password

Su Fedora servono xorg-x11-server-Xvfb e librsvg2 (per le icone SVG).
"""
import json, os, sys, tempfile, time
from pathlib import Path

REPO, TEMA, USCITA = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
VISTA = sys.argv[4] if len(sys.argv) > 4 else "sessioni"
tmp = Path(tempfile.mkdtemp())
os.environ.update(XDG_CONFIG_HOME=str(tmp / "c"), XDG_CACHE_HOME=str(tmp / "k"), XDG_DATA_HOME=str(tmp / "d"))
cfg = tmp / "c" / "fedtermhub"; cfg.mkdir(parents=True)
(cfg / "settings.json").write_text(json.dumps({"startup_mode": "empty", "theme": TEMA, "show_error_popups": False}))

import gi
gi.require_version("Gtk", "3.0"); gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk
sys.path.insert(0, str(REPO / "fedtermhub"))
import fedtermhub as app
from remote_files import RemoteEntry

S, F = app.Session, app.FolderNode
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
        S(name="switch-core", protocol="serial", host="/dev/ttyUSB0", port="9600"),
        S(name="kvm-console", protocol="vnc", host="kvm.example.lan"),
    ]),
    S(name="Terminale locale", protocol="local"),
]

class FintoClient:
    def __init__(self, _c): pass
    def pwd(self): return "/etc/nginx"
    def listdir(self, path):
        voci = [("conf.d", "directory"), ("sites-available", "directory"), ("sites-enabled", "directory"),
                ("snippets", "directory"), ("fastcgi_params", "file"), ("mime.types", "file"),
                ("nginx.conf", "file"), ("proxy_params", "file"), ("uwsgi_params", "file"), (".htpasswd", "file")]
        return [RemoteEntry(n, f"{path.rstrip('/')}/{n}", k, "drwxr-xr-x" if k == "directory" else "-rw-r--r--", 2048)
                for n, k in voci]

ESC = "\x1b"
def p(user, host, cwd): return f"{ESC}[1;32m{user}@{host}{ESC}[0m:{ESC}[1;34m{cwd}{ESC}[0m$ "
testo = (
    "Last login: Thu Oct 29 09:12:44 2026 from 10.0.0.15\r\n"
    + p("admin", "web01", "~") + "cd /etc/nginx\r\n"
    + p("admin", "web01", "/etc/nginx") + "sudo nginx -t\r\n"
    "nginx: the configuration file /etc/nginx/nginx.conf syntax is ok\r\n"
    "nginx: configuration file /etc/nginx/nginx.conf test is successful\r\n"
    + p("admin", "web01", "/etc/nginx") + "systemctl status nginx --no-pager | head -4\r\n"
    f"{ESC}[1;32m●{ESC}[0m nginx.service - A high performance web server and a reverse proxy server\r\n"
    "     Loaded: loaded (/lib/systemd/system/nginx.service; enabled; preset: enabled)\r\n"
    f"     Active: {ESC}[1;32mactive (running){ESC}[0m since Thu 2026-10-29 08:01:17 CET; 1h 11min ago\r\n"
    "   Main PID: 812 (nginx)\r\n"
    + p("admin", "web01", "/etc/nginx") + "df -h / /var\r\n"
    "Filesystem      Size  Used Avail Use% Mounted on\r\n"
    "/dev/vda1        40G   14G   24G  37% /\r\n"
    "/dev/vdb1       200G   61G  130G  32% /var\r\n"
    + p("admin", "web01", "/etc/nginx")
)

application = Gtk.Application(application_id=f"io.github.crash_fd01.fedtermhub.Demo{TEMA}")
application.register(None)
win = app.AppWindow(application)
home = win.store.home_node(); home.children = albero; win.store.save(); win.refresh_sessions()
win.session_tree.expand_all()
win.remote_files_sidebar.client_factory = FintoClient
web01 = albero[0].children[1].children[0]
win.add_terminal_tab("web01", ["/bin/sleep", "3600"], session=web01)
tab = win.terminal_tabs[-1]
win.resize(1360, 840); win.show_all()

finestra = {"w": win}
if VISTA == "password":
    C = app.Credential
    rossi = albero[0].children[0].children
    bianchi = albero[0].children[1].children
    credenziali = [
        C(name="Administrator dominio Rossi", username="Administrator", domain="STUDIOROSSI",
          password="Xr7!kq2Lm#90", notes="Account di dominio. Scade a marzo.", group="Studio Rossi"),
        C(name="Firewall perimetrale", username="admin", password="fw-Pass-2026", group="Studio Rossi"),
        C(name="Backup NAS", username="backup", password="nas-Backup-2026", group="Studio Rossi"),
        C(name="Web e database", username="admin", password="W3b!Bianchi#2026", group="Officine Bianchi"),
        C(name="Terminal server", username="m.verdi", domain="BIANCHI", password="TsB-77!aa", group="Officine Bianchi"),
        C(name="root Proxmox", username="root", password="pve-root-2026", group="Infrastruttura"),
    ]
    rossi[0].credential_id = credenziali[1].id
    rossi[1].credential_id = credenziali[0].id
    rossi[2].credential_id = credenziali[2].id
    bianchi[0].credential_id = bianchi[1].credential_id = credenziali[3].id
    bianchi[2].credential_id = credenziali[4].id
    albero[1].children[0].credential_id = credenziali[5].id
    win.store.save()
    app.KDF_ITERATIONS = 2_000
    win.vault.create("demo-solo-per-schermate")
    win.vault.credentials = credenziali
    win.vault.save()

def mostra():
    if VISTA == "password":
        dialogo = app.VaultManagerDialog(win, win.vault)
        dialogo.select_credential(win.vault.credentials[0].id)
        finestra["w"] = dialogo
        return False
    for pane in getattr(tab, "panes", []):
        pane.terminal.feed(testo.encode())
    win._show_remote_files_for_page(tab)
    if VISTA == "file":
        win.sidebar_notebook.set_current_page(win.remote_files_page_num)
    else:
        win.sidebar_notebook.set_current_page(0)
        win.session_tree.expand_all()
        win._select_store_path(win.store.find_node_path(web01))
    return False

def scatta():
    while Gtk.events_pending(): Gtk.main_iteration_do(False)
    gw = finestra["w"].get_window(); w, h = gw.get_width(), gw.get_height()
    Gdk.pixbuf_get_from_window(gw, 0, 0, w, h).savev(str(USCITA), "png", [], [])
    print(f"salvata {USCITA} {w}x{h}"); application.quit(); Gtk.main_quit(); return False

GLib.timeout_add(1500, mostra)
GLib.timeout_add(4500, scatta)
Gtk.main()
