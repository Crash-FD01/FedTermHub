# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Avvio dell'interfaccia Qt (versione Windows di FedTermHub; su Linux per le prove).

    fedtermhub-qt                  apre il programma
    fedtermhub-qt --version        stampa la versione
    fedtermhub-qt --self-test F    prova il programma installato e scrive il risultato in F (JSON)

La prova automatica usa una configurazione temporanea (non tocca quella dell'utente) e controlla:
terminale con la shell locale, xterm.js, archivio delle password, import di un file di sessioni
esportato da Linux, comando SSH, e su Windows il componente RDP incorporato.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Callable, Dict, List

SELFTEST_TIMEOUT = 90


def _say(text: str) -> None:
    """print() che non fallisce nell'eseguibile senza console (PyInstaller --windowed: stdout è None)."""
    if sys.stdout is not None:
        print(text, flush=True)


def _isolate_config() -> Path:
    """Configurazione temporanea per la prova: va decisa prima di importare model e i18n."""
    root = Path(tempfile.mkdtemp(prefix="fedtermhub-selftest-"))
    for var, sub in (("APPDATA", "roaming"), ("LOCALAPPDATA", "local"), ("XDG_CONFIG_HOME", "config"),
                     ("XDG_CACHE_HOME", "cache")):
        os.environ[var] = str(root / sub)
    return root


def install_qt_translations(app) -> None:  # noqa: ANN001
    """Pulsanti standard di Qt (Salva, Annulla, Chiudi...) nella lingua dell'interfaccia."""
    from PySide6.QtCore import QLibraryInfo, QTranslator

    from .. import i18n

    if i18n.LANGUAGE == "en":
        return
    translator = QTranslator(app)
    if translator.load(f"qtbase_{i18n.LANGUAGE}", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(translator)


def _wait(app, condition: Callable[[], bool], seconds: float) -> bool:  # noqa: ANN001
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def self_test(report_path: Path) -> int:
    _isolate_config()
    os.environ.setdefault("FEDTERMHUB_LANGUAGE", "it")
    from PySide6.QtWidgets import QApplication

    from .. import model
    from . import connect, rdp_tab, theme
    from .main_window import MainWindow, TerminalTab

    model.KDF_ITERATIONS = 1000  # la prova non deve aspettare la derivazione vera della chiave
    app = QApplication.instance() or QApplication(sys.argv[:1])
    install_qt_translations(app)
    theme.apply(app, "dark")
    results: List[Dict[str, object]] = []

    def check(name: str, func: Callable[[], object]) -> None:
        start = time.monotonic()
        try:
            detail = func()
            results.append({"name": name, "ok": True, "detail": "" if detail is None else str(detail),
                            "seconds": round(time.monotonic() - start, 2)})
        except Exception as exc:  # noqa: BLE001
            results.append({"name": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}",
                            "trace": traceback.format_exc()[-2000:], "seconds": round(time.monotonic() - start, 2)})

    window = MainWindow()
    window.showMaximized()  # lo schermo del runner è piccolo (1024x768)

    def config_dir() -> str:
        assert "fedtermhub-selftest-" in str(model.CONFIG_DIR), model.CONFIG_DIR
        return str(model.CONFIG_DIR)

    check("configurazione temporanea", config_dir)

    def terminal() -> str:
        before = window.tabs.count()
        window.open_local_terminal()
        assert window.tabs.count() == before + 1, "scheda non aperta"
        tab = window.tabs.currentWidget()
        assert isinstance(tab, TerminalTab), type(tab)
        output: List[str] = []
        tab.view.pty.output.connect(output.append)
        assert _wait(app, lambda: tab.view.ready, 30), "xterm.js non si è caricato"
        assert _wait(app, lambda: tab.running, 10), f"la shell non è partita: {tab.start_error or 'nessun errore'}; {tab.launch.argv}"
        # $((6*7)) dà 42 sia in PowerShell (sottoespressione) sia nelle shell POSIX (aritmetica):
        # se arriva 42 la shell ha davvero eseguito il comando, non solo ripetuto i tasti.
        _wait(app, lambda: bool(output), 15)
        tab.view.on_input("echo FTH_SELFTEST_$((6*7))\r")
        assert _wait(app, lambda: "FTH_SELFTEST_42" in "".join(output), 30), \
            "risposta della shell non arrivata: " + "".join(output)[-300:]
        screen: List[str] = []
        _wait(app, lambda: False, 0.5)
        tab.view.screen_text(screen.append)
        assert _wait(app, lambda: bool(screen), 10), "testo del terminale non leggibile"
        assert "FTH_SELFTEST_42" in screen[0], "xterm.js non mostra l'output: " + screen[0][-300:]
        argv = tab.launch.argv
        tab.view.on_input("exit\r")
        assert _wait(app, lambda: not tab.running, 15), "la shell non si chiude"
        window.close_tab(window.tabs.indexOf(tab))
        return " ".join(argv)

    check("terminale locale", terminal)

    def vault() -> str:
        v = model.PasswordVault(model.VAULT_FILE)
        v.create("prova-Selftest-1")
        cred = model.Credential(name="Prova", username="utente", password="segreto-é€")
        v.credentials.append(cred)
        v.save()
        v.lock()
        again = model.PasswordVault(model.VAULT_FILE)
        try:
            again.unlock("sbagliata")
        except Exception:  # noqa: BLE001
            pass
        else:
            raise AssertionError("password principale sbagliata accettata")
        again.unlock("prova-Selftest-1")
        got = again.get(cred.id)
        assert got and got.password == "segreto-é€", "credenziale non ritrovata"
        return f"{len(again.credentials)} credenziale"

    check("archivio delle password", vault)

    def import_package() -> str:
        folder = model.FolderNode(name="Clienti")
        folder.children.append(model.Session(name="server-prova", protocol="ssh", host="198.51.100.7", user="admin"))
        folder.children.append(model.Session(name="desktop-prova", protocol="rdp", host="198.51.100.8",
                                             rdp_clipboard=True))
        package = model.CONFIG_DIR / "export-linux.json"
        package.write_text(json.dumps(model.session_package(folder, "folder"), indent=2), encoding="utf-8")
        window.import_session_file(package, window.store.home_path())
        reloaded = model.SessionStore(model.SESSIONS_FILE)
        sessions = [s.name for s in MainWindow._walk_sessions(reloaded.home_node())]
        assert {"server-prova", "desktop-prova"} <= set(sessions), sessions
        imported = [s for s in MainWindow._walk_sessions(reloaded.home_node()) if s.name == "desktop-prova"][0]
        assert imported.rdp_clipboard and imported.host == "198.51.100.8"
        return f"{len(sessions)} sessioni dopo l'import"

    check("import sessioni da Linux", import_package)

    def ssh_command() -> str:
        sess = model.Session(name="prova", protocol="ssh", host="198.51.100.7", port="2222", user="admin")
        launch = connect.ssh(sess, window.settings, "admin", "pw")
        assert launch.argv[-1] == "admin@198.51.100.7" and "-p" in launch.argv, launch.argv
        feeder = connect.PasswordFeeder("pw")
        assert feeder.feed("admin@198.51.100.7's password: ") == "pw\r"
        assert feeder.feed("password: ") is None, "la password va scritta una volta sola"
        return launch.argv[0]

    if sys.platform == "win32" or connect.ssh_binary():
        check("comando SSH", ssh_command)

    from .selftest_more import run_checks

    run_checks(app, window, check, _wait, report_path)

    if sys.platform == "win32" and app.platformName() != "offscreen":
        def rdp() -> str:
            """Il componente RDP di Windows nella scheda.

            Con FEDTERMHUB_SELFTEST_RDP="host;utente;password" (CI con Desktop remoto attivo) deve arrivare
            all'accesso vero; senza, basta che il componente si crei, accetti le impostazioni e risponda
            con un evento (di solito: connessione rifiutata).
            """
            assert rdp_tab.ax_available(), "comtypes o mstscax.dll non disponibili"
            target = os.environ.get("FEDTERMHUB_SELFTEST_RDP", "")
            host, user, password = (target.split(";", 2) + ["", "", ""])[:3] if target else ("127.0.0.2", "utente", "x")
            domain = ""
            if "\\" in user:
                domain, user = user.split("\\", 1)
            sess = model.Session(name="rdp-prova", protocol="rdp", host=host, rdp_cert_ignore=True)
            tab = rdp_tab.RdpTab(window, sess, user, domain, password)
            index = window.tabs.addTab(tab, "rdp-prova")
            window.tabs.setCurrentIndex(index)
            window._update_welcome()
            assert _wait(app, lambda: tab.ax is not None or tab.events, 15), "il componente RDP non si è creato"
            assert tab.ax is not None, "il componente RDP non si è creato: " + ", ".join(tab.events)
            assert str(tab.ax.Server) == host, f"Server = {tab.ax.Server!r}"
            assert tab.ax.AdvancedSettings9 is not None, "AdvancedSettings9 non disponibile"
            final = ("disconnected", "login", "fatal", "connect-error")
            _wait(app, lambda: any(e.startswith(final) for e in tab.events), 60)
            assert tab.events, f"nessun evento dal componente RDP; stato: {tab.state.text()}"
            if target:
                assert "login" in tab.events, ("accesso non riuscito: " + ", ".join(tab.events) + " — " + tab.state.text()
                                               + " — non impostate: " + ", ".join(tab.unset))
                _wait(app, lambda: False, 4)
                # QWidget.grab() non vede la finestra nativa del componente: si fotografa lo schermo.
                app.primaryScreen().grabWindow(0).save(str(report_path.with_name("rdp-session.png")))
            version = str(tab.ax.Version or "")
            tab.close_tab()
            _wait(app, lambda: any(e.startswith("disconnected") for e in tab.events), 15)
            window.tabs.removeTab(window.tabs.indexOf(tab))
            unset = f"; non accettate: {', '.join(tab.unset)}" if tab.unset else ""
            return f"versione {version}; eventi: {', '.join(tab.events)}; stato: {tab.state.text()}{unset}"

        check("RDP incorporato", rdp)

    window.grab().save(str(report_path.with_name("main-window.png")))
    window.close()
    ok = all(r["ok"] for r in results)
    report = {"version": model.APP_VERSION, "platform": sys.platform, "ok": ok, "checks": results}
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    for r in results:
        _say(("OK   " if r["ok"] else "FAIL ") + str(r["name"]) + (f" — {r['detail']}" if r["detail"] else ""))
    return 0 if ok else 1


def main(argv: List[str] = None) -> int:  # type: ignore[assignment]
    argv = list(sys.argv[1:] if argv is None else argv)
    # Nell'eseguibile senza console stdout e stderr non esistono: un print() farebbe cadere il programma.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if "--version" in argv:
        from .. import model

        _say(f"{model.APP_NAME} {model.APP_VERSION}")
        return 0
    if "--self-test" in argv:
        index = argv.index("--self-test")
        target = Path(argv[index + 1]) if index + 1 < len(argv) else Path("fedtermhub-selftest.json")
        try:
            return self_test(target.resolve())
        except Exception:  # noqa: BLE001
            target.write_text(json.dumps({"ok": False, "error": traceback.format_exc()}), encoding="utf-8")
            _say(traceback.format_exc())
            return 2

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    from .. import model
    from . import theme
    from .main_window import MainWindow

    if sys.platform == "win32":
        try:
            import ctypes

            # Icona giusta nella barra delle applicazioni invece di quella di Python.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FedTermHub.FedTermHub")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(model.APP_NAME)
    app.setApplicationVersion(model.APP_VERSION)
    app.setOrganizationName(model.APP_NAME)
    install_qt_translations(app)
    settings = model.AppSettings.load(model.SETTINGS_FILE)
    theme.apply(app, settings.theme)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
