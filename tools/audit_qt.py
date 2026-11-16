# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Controllo del layout della versione Windows (interfaccia Qt): apre ogni finestra con dati INVENTATI,
elenca i difetti trovati da qtui/layout_check.py e salva una schermata per finestra e per menu.

    xvfb-run -a -s "-screen 0 1280x720x24" python3 tools/audit_qt.py dark  /tmp/audit-scuro
    xvfb-run -a -s "-screen 0 1280x720x24" python3 tools/audit_qt.py light /tmp/audit-chiaro

1280x720 è uno schermo Full HD con Windows al 150%, 1536x864 al 125%: le misure che usano i portatili.
Su Windows funziona anche senza xvfb-run. Non tocca la configurazione dell'utente. Esce con 1 se trova difetti.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TEMA, USCITA = sys.argv[1], Path(sys.argv[2]).resolve()
REPO = Path(__file__).resolve().parents[1]
tmp = Path(tempfile.mkdtemp())
os.environ.update(XDG_CONFIG_HOME=str(tmp / "c"), XDG_CACHE_HOME=str(tmp / "k"), APPDATA=str(tmp / "c"),
                  LOCALAPPDATA=str(tmp / "k"))
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
sys.path.insert(0, str(REPO))
USCITA.mkdir(parents=True, exist_ok=True)

from fedtermhub import model  # noqa: E402

model.ensure_private_config_dir()
S, F = model.Session, model.FolderNode
store = model.SessionStore(model.SESSIONS_FILE)
store.nodes = [F(name=model.HOME_SESSION_NAME, color=model.HOME_SESSION_COLOR, children=[
    F(name="Clienti", color="#4aa3df", children=[
        F(name="Studio Rossi", color="#56c7d9", children=[
            S(name="fw-perimetrale", protocol="ssh", host="10.20.0.1", user="admin"),
            S(name="srv-dc01", protocol="rdp", host="10.20.0.10", user="Administrator"),
            S(name="nas-backup", protocol="sftp", host="10.20.0.30", user="backup"),
        ]),
        F(name="Officine Bianchi con un nome molto lungo", color="#f2c94c", children=[
            S(name="web01", protocol="ssh", host="web01.example.lan", user="admin"),
            S(name="terminal-server-di-produzione-01", protocol="rdp", host="ts.example.lan"),
        ]),
    ]),
    F(name="Infrastruttura", color="#9b8afb", children=[
        S(name="switch-core", protocol="serial", host="COM3", port="9600"),
        S(name="kvm-console", protocol="vnc", host="kvm.example.lan"),
        S(name="router-vecchio", protocol="telnet", host="10.20.0.254"),
    ]),
    S(name="Terminale locale", protocol="local"),
])]
store.save()

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fedtermhub.qtui import connect, layout_check, selftest_layout, theme  # noqa: E402
from fedtermhub.qtui.main_window import MainWindow  # noqa: E402

from fedtermhub.qtui.app import install_qt_translations  # noqa: E402

app = QApplication([])
install_qt_translations(app)
schermo = app.primaryScreen().availableGeometry()
model.SETTINGS_FILE.write_text(json.dumps({"theme": TEMA, "window_width": min(1360, schermo.width()),
                                           "window_height": min(840, schermo.height())}))
theme.apply(app, TEMA)
win = MainWindow()
win.show()


def wait(_app, condition, seconds):  # noqa: ANN001, ANN201
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        loop = QEventLoop()
        QTimer.singleShot(30, loop.quit)
        loop.exec()
        if condition():
            return True
    return False


wait(app, lambda: False, 1)
win.tree.expandAll()
win.add_terminal_tab(connect.Launch("web01", ["bash", "-c", "echo 'admin@web01:~$ '; exec sleep 600"], {},
                                    close_on_success=False), protocol="ssh")
win.add_terminal_tab(connect.Launch("Terminale locale", ["bash", "-c", "exec sleep 600"], {}), protocol="local")
wait(app, lambda: False, 2.5)
report = selftest_layout.run(app, win, wait, USCITA)

# Con AUDIT_SSH="host;porta;utente;password" anche i file remoti, nel pannello laterale (stretto) e larghi.
if os.environ.get("AUDIT_SSH"):
    from fedtermhub.qtui.files_panel import FilesPanel  # noqa: E402
    from fedtermhub.qtui.sftp_client import SftpTarget  # noqa: E402

    host, port, user, password = os.environ["AUDIT_SSH"].split(";", 3)
    for nome, compatto, larghezza in (("file-stretto", True, 240), ("file-largo", False, 760)):
        pannello = FilesPanel(SftpTarget(host=host, port=int(port), user=user, password=password, strict="accept-new"),
                              None, compact=compatto, ask_password=False)
        pannello.setWindowFlag(pannello.windowFlags().__class__.Window)
        pannello.setStyleSheet(f"FilesPanel {{ background: {theme.colors()['sidebar']}; }}")
        pannello.resize(larghezza, 420)
        pannello.show()
        pannello.connect_server()
        wait(app, lambda: pannello.connected and pannello.model.rowCount() > 0, 15)
        wait(app, lambda: False, 0.4)
        trovati = layout_check.problems(pannello)
        if trovati:
            report[nome] = trovati
        pannello.grab().save(str(USCITA / f"layout-{nome}.png"))
        pannello.shutdown()
        pannello.close()

# I menu: si fotografa lo schermo, perché sono finestre a parte.
for index, action in enumerate(win.menuBar().actions()):
    menu = action.menu()
    if menu is None:
        continue
    menu.popup(win.menuBar().mapToGlobal(win.menuBar().actionGeometry(action).bottomLeft()))
    wait(app, lambda: False, 0.3)
    app.primaryScreen().grabWindow(0).save(str(USCITA / f"menu-{index + 1}.png"))
    menu.hide()
for name, button in win.ribbon_buttons.items():
    button.showMenu() if button.menu() is None else button.menu().popup(button.mapToGlobal(button.rect().bottomLeft()))
    wait(app, lambda: False, 0.3)
    app.primaryScreen().grabWindow(0).save(str(USCITA / f"barra-{name}.png"))
    for popup in app.topLevelWidgets():
        if popup.inherits("QMenu"):
            popup.hide()

# La finestra stretta: la misura più piccola a cui si può portare.
win.showNormal()
win.resize(win.minimumSizeHint().expandedTo(win.minimumSize()))
wait(app, lambda: False, 0.5)
stretta = layout_check.problems(win)
if stretta:
    report["principale-stretta"] = stretta
win.grab().save(str(USCITA / "layout-principale-stretta.png"))
print(f"finestra stretta: {win.width()}x{win.height()}; schermo {schermo.width()}x{schermo.height()}")

for name, found in report.items():
    print(f"== {name}")
    for line in found:
        print("   ", line)
print(f"{sum(len(v) for v in report.values())} difetti in {len(report)} finestre; schermate in {USCITA}")
os._exit(1 if report else 0)
