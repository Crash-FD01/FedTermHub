#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regressioni della revisione generale della 0.9.09, provate con GTK reale.

Ogni blocco corrisponde a un difetto trovato e corretto:
 1. indirizzi: IPv6, host:porta nella connessione rapida, display/porta VNC;
 2. sicurezza: un host o un utente che comincia con "-" non diventa un'opzione di ssh/sftp;
 3. schede terminale: chiusura con "exit", Ctrl+Shift+W sull'ultimo riquadro, elenco MultiExec;
 4. albero sessioni: cartelle aperte conservate, duplicato accanto all'originale, file corrotto
    segnalato invece di essere azzerato in silenzio;
 5. finestra sessione: non salva una sessione con host mancante o porta non valida;
 6. MultiExec invia solo alle schede scelte; Impostazioni aperte una volta sola;
 7. file remoti: Aggiorna ricarica la cartella corrente invece di entrare in quella selezionata.

Uso:  xvfb-run -a python3 tests/test_revisione.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "fedtermhub"))


def pump(seconds: float = 0.3) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.01)


def wait_for(predicate, seconds: float = 4.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pump(0.05)
        if predicate():
            return True
    return bool(predicate())


def check_addresses(app) -> None:
    cases = {
        "web01": ("web01", "22"),
        "web01:2222": ("web01", "2222"),
        "[fe80::1]:2200": ("fe80::1", "2200"),
        "[::1]": ("::1", "22"),
        "fe80::1": ("fe80::1", "22"),
        "host:abc": ("host:abc", "22"),
    }
    for text, expected in cases.items():
        assert app.split_host_port(text, "22") == expected, (text, app.split_host_port(text, "22"))
    assert app.vnc_target("kvm", "") == "kvm"
    assert app.vnc_target("kvm", "1") == "kvm:1", "numero piccolo = display"
    assert app.vnc_target("kvm", "5901") == "kvm::5901", "5901 e' una porta, non il display 5901"
    assert app.vnc_target("fe80::1", "5900") == "[fe80::1]::5900"
    print("  indirizzi: IPv6, host:porta, display e porta VNC")


def check_session_problems(app) -> None:
    S = app.Session
    assert app.session_problem(S(name="ok", protocol="ssh", host="web01", port="22")) == ""
    assert "host" in app.session_problem(S(name="x", protocol="ssh", host=""))
    assert "non valido" in app.session_problem(S(name="x", protocol="ssh", host="-oProxyCommand=touch /tmp/x"))
    assert "non valido" in app.session_problem(S(name="x", protocol="sftp", host="h", user="-oX=1"))
    assert "porta" in app.session_problem(S(name="x", protocol="rdp", host="h", port="33a9"))
    assert "porta" in app.session_problem(S(name="x", protocol="ssh", host="h", port="70000"))
    assert app.session_problem(S(name="x", protocol="vnc", host="h", port="0")) == "", "display 0 e' valido"
    assert "comando" in app.session_problem(S(name="x", protocol="command", command=" "))
    assert "velocità" in app.session_problem(S(name="x", protocol="serial", host="/dev/ttyUSB0", port="fast"))
    assert app.session_problem(S(name="x", protocol="local")) == ""
    print("  validazione delle sessioni")


def check_option_injection(app, window) -> None:
    captured = {}
    original = window.add_terminal_tab
    window.add_terminal_tab = lambda title, argv, **kw: captured.update(argv=argv)
    try:
        window.open_ssh(app.Session(name="s", protocol="ssh", host="web01", user="admin", port="22"))
        argv = captured["argv"]
        assert argv[-2:] == ["--", "admin@web01"], argv
        window.launch_sftp(app.Session(name="f", protocol="sftp", host="web01", port="22"))
        assert captured["argv"][-2:] == ["--", "web01"], captured["argv"]
    finally:
        window.add_terminal_tab = original
    tunnel = app.TunnelSession(name="t", ssh_host="bastion", ssh_port="22", local_port="15432",
                               remote_host="db", remote_port="5432", bind_address="127.0.0.1")
    argv, _env = window.build_tunnel_command(tunnel, allow_unlock=False)
    assert argv[-2:] == ["--", "bastion"], argv
    if app.SftpConnection is not None:
        from remote_files import SftpBatchClient  # noqa: PLC0415

        client = SftpBatchClient(app.SftpConnection(label="x", host="h", port="22", user="u", password=None,
                                                    jump_spec="", ssh_options=(), timeout_seconds=5))
        try:
            argv, _env = client._argv_and_env()
            assert argv[-2:] == ["--", "u@h"], argv
        except Exception as exc:  # sftp assente nell'ambiente di prova
            assert "SFTP" in str(exc)

    reported = []
    original_report = window.report_session_error
    window.report_session_error = lambda title, *a, **k: reported.append(title)
    try:
        window._open_session_now(app.Session(name="importata", protocol="ssh", host="-oProxyCommand=touch /tmp/x"))
    finally:
        window.report_session_error = original_report
    assert reported == ["Sessione non valida"], reported

    opened = []
    original_open = window.open_session
    window.open_session = lambda sess: opened.append((sess.protocol, sess.host, sess.port, sess.user))
    try:
        for text in ("root@[fe80::1]:2200", "rdp:srv01:3390", "vnc:kvm:5901", "admin@web01", "telnet:sw-core", "telnet:10.0.0.1:2323"):
            window.quick.set_text(text)
            window.on_quick_connect()
        window.quick.set_text("-oProxyCommand=x")
        window.on_quick_connect()
    finally:
        window.open_session = original_open
    assert opened == [
        ("ssh", "fe80::1", "2200", "root"),
        ("rdp", "srv01", "3390", ""),
        ("vnc", "kvm", "5901", ""),
        ("ssh", "web01", "22", "admin"),
        ("telnet", "sw-core", "23", ""),
        ("telnet", "10.0.0.1", "2323", ""),
    ], opened

    # Telnet: "--" prima dell'host, scheda che si chiude con un'uscita normale.
    original_which = app.shutil.which
    app.shutil.which = lambda name: "/usr/bin/telnet" if name == "telnet" else original_which(name)
    window.add_terminal_tab = lambda title, argv, **kw: captured.update(argv=argv, kw=kw)
    try:
        window.open_telnet(app.Session(name="sw", protocol="telnet", host="sw-core", port="23"))
    finally:
        app.shutil.which = original_which
        window.add_terminal_tab = original
    assert captured["argv"] == ["/usr/bin/telnet", "--", "sw-core", "23"], captured["argv"]
    assert captured["kw"]["close_on_exit"] == "on_success"
    assert "host" in app.session_problem(app.Session(name="t", protocol="telnet", host=""))

    dialog = app.SessionDialog(window, app.Session(name="Nuova Telnet", protocol="telnet", port="23"))
    dialog.telnet_host.set_text("ups01")
    saved = dialog.get_session()
    dialog.destroy()
    assert (saved.protocol, saved.host, saved.port) == ("telnet", "ups01", "23")

    from connection_diagnostics import normalize_port  # noqa: PLC0415

    assert normalize_port("vnc", "1") == 5901, "display 1 = porta 5901, non la porta 1"
    assert normalize_port("vnc", "5901") == 5901 and normalize_port("telnet", "") == 23
    print("  ssh/sftp/tunnel con '--', sessioni importate non valide rifiutate, connessione rapida")


def terminal_pages(window) -> int:
    return window.notebook.get_n_pages()


def check_terminal_tabs(app, window) -> None:
    start = terminal_pages(window)
    window.add_terminal_tab("Locale", ["/bin/sh", "-c", "exit 0"], close_on_exit="always")
    assert wait_for(lambda: terminal_pages(window) == start), "la shell locale uscita lascia la scheda aperta"

    ssh = app.Session(name="ssh-finto", protocol="ssh", host="h")
    window.add_terminal_tab("ok", ["/bin/sh", "-c", "exit 0"], session=ssh, close_on_exit="on_success")
    assert wait_for(lambda: terminal_pages(window) == start), "'exit' in ssh deve chiudere la scheda"

    window.add_terminal_tab("caduta", ["/bin/sh", "-c", "exit 255"], session=ssh, close_on_exit="on_success")
    pump(1.2)
    assert terminal_pages(window) == start + 1, "una connessione caduta resta aperta per riconnettere"
    dropped = window.notebook.get_nth_page(start)
    assert not dropped.is_connected

    window.add_terminal_tab("comando", ["/bin/sh", "-c", "echo fatto"])
    pump(1.2)
    assert terminal_pages(window) == start + 2, "l'output di un comando non deve sparire"

    for page in list(window.terminal_tabs):
        window.close_specific_tab(page)
    assert terminal_pages(window) == start and not window.terminal_tabs, "chiusura: scheda ed elenco MultiExec"

    window.add_terminal_tab("vivo", ["/bin/sleep", "60"], session=ssh, close_on_exit="on_success")
    tab = window.terminal_tabs[-1]
    pump(0.5)
    tab.dispatch("close_pane", tab.panes[0])
    assert wait_for(lambda: terminal_pages(window) == start), "Ctrl+Shift+W sull'ultimo riquadro chiude la scheda"
    assert tab not in window.terminal_tabs
    print("  schede: exit, connessione caduta, comando, Ctrl+Shift+W, elenco MultiExec")


def check_multiexec(app, window) -> None:
    ssh = app.Session(name="web01", protocol="ssh", host="web01")
    window.add_terminal_tab("web01", ["/bin/cat"], session=ssh, close_on_exit="on_success")
    window.add_terminal_tab("Journal", ["/bin/cat"], close_on_exit="always")
    pump(0.5)
    remote, tool = window.terminal_tabs[-2], window.terminal_tabs[-1]
    received = {}
    for tab in (remote, tool):
        tab.feed_all = lambda data, t=tab: (received.setdefault(t, []).append(data), 1)[1]

    original = app.MultiExecDialog.run

    def scripted(dialog):
        kinds = {target: check.get_active() for check, target in dialog.checks}
        assert kinds[remote] and not kinds[tool], "di default solo le sessioni SSH"
        assert not dialog.send_button.get_sensitive(), "senza comando Invia e' disattivo"
        dialog.command.set_text("uptime")
        assert dialog.send_button.get_label() or True
        return Gtk.ResponseType.OK

    app.MultiExecDialog.run = scripted
    try:
        window.show_multiexec_dialog()
    finally:
        app.MultiExecDialog.run = original
    assert received == {remote: [b"uptime\n"]}, received
    for tab in (remote, tool):
        window.close_specific_tab(tab)
    print("  MultiExec: elenco delle schede, solo quelle scelte")


def tree_expanded_names(window) -> set:
    names = set()

    def walk(parent):
        it = window.tree_store.iter_children(parent)
        while it is not None:
            if window.session_tree.row_expanded(window.tree_store.get_path(it)):
                names.add(window.store.get_node(window.tree_store[it][window.COL_PATH]).name)
                walk(it)
            it = window.tree_store.iter_next(it)

    walk(None)
    return names


def check_tree(app, window, tmp: Path) -> None:
    S, F = app.Session, app.FolderNode
    rossi = F(name="Studio Rossi", children=[S(name="dc01", protocol="rdp", host="10.0.0.10")])
    clienti = F(name="Clienti", children=[rossi, F(name="Chiusa", children=[S(name="x", protocol="ssh", host="x")])])
    home = window.store.home_node()
    home.children = [clienti]
    window.store.save()
    window.refresh_sessions()
    window._select_store_path(window.store.find_node_path(rossi.children[0]))
    pump()
    assert {"Clienti", "Studio Rossi"} <= tree_expanded_names(window)
    window.refresh_sessions()
    pump()
    names = tree_expanded_names(window)
    assert {"Clienti", "Studio Rossi"} <= names and "Chiusa" not in names, names

    window._select_store_path(window.store.find_node_path(rossi.children[0]))
    pump()
    window.on_duplicate_selected(None)
    pump()
    assert [n.name for n in rossi.children] == ["dc01", "dc01 copia"]
    selected = window.selected_path_and_node()
    assert selected is not None and selected[1] is rossi.children[1], "la copia deve essere selezionata"

    broken = tmp / "rotto.json"
    broken.write_text("{non è json", encoding="utf-8")
    store = app.SessionStore(broken)
    assert store.load_warning and "copia" in store.load_warning
    copies = list(tmp.glob("rotto.illeggibile-*.json"))
    assert len(copies) == 1 and copies[0].read_text(encoding="utf-8") == "{non è json"
    print("  albero: cartelle aperte conservate, duplicato accanto, file corrotto segnalato")


def check_session_dialog(app, window) -> None:
    warnings = []
    original = Gtk.MessageDialog.run

    def fake_message(dialog):
        warnings.append(dialog.get_property("secondary-text"))
        return Gtk.ResponseType.OK

    Gtk.MessageDialog.run = fake_message
    try:
        dialog = app.SessionDialog(window, app.Session(name="Nuova SSH", protocol="ssh", port="22"))
        GLib.idle_add(lambda: (dialog.response(Gtk.ResponseType.OK), False)[1])
        GLib.timeout_add(300, lambda: (dialog.response(Gtk.ResponseType.CANCEL), False)[1])
        result = dialog.run()
        dialog.destroy()
    finally:
        Gtk.MessageDialog.run = original
    assert result == Gtk.ResponseType.CANCEL, "con l'host vuoto Salva non deve chiudere la finestra"
    assert warnings and "host" in warnings[0].lower(), warnings

    dialog = app.SessionDialog(window, app.Session(name="ok", protocol="ssh", host="web01", port="22"))
    GLib.idle_add(lambda: (dialog.response(Gtk.ResponseType.OK), False)[1])
    result = dialog.run()
    dialog.destroy()
    assert result == Gtk.ResponseType.OK
    print("  finestra sessione: niente salvataggio con dati non validi")


def check_settings_once(window) -> None:
    window.show_settings_dialog()
    first = window._settings_dialog
    window.show_settings_dialog()
    assert window._settings_dialog is first, "una sola finestra Impostazioni"
    first.response(Gtk.ResponseType.CANCEL)
    pump()
    assert window._settings_dialog is None
    print("  impostazioni: una finestra sola")


def check_remote_refresh(app, window) -> None:
    sidebar = window.remote_files_sidebar
    loaded = []
    original = sidebar.load_root
    sidebar.load_root = lambda path, prefetched=None: loaded.append(path)
    try:
        sidebar._set_current_path("/etc")
        sidebar.selected_entry = lambda: app_entry
        app_entry = __import__("remote_files").RemoteEntry("nginx", "/etc/nginx", "directory")
        sidebar.refresh()
    finally:
        sidebar.load_root = original
        del sidebar.selected_entry
    assert loaded == ["/etc"], loaded
    print("  file remoti: Aggiorna resta nella cartella corrente")


def main() -> int:
    if Gdk.Display.get_default() is None:
        print("REVISIONE_SKIP: nessun display GTK")
        return 0
    with tempfile.TemporaryDirectory() as temp_dir:
        tmp = Path(temp_dir)
        os.environ["XDG_CONFIG_HOME"] = str(tmp / "config")
        os.environ["XDG_CACHE_HOME"] = str(tmp / "cache")
        config_dir = tmp / "config" / "fedtermhub"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "settings.json").write_text(
            json.dumps({"startup_mode": "empty", "theme": "dark", "show_error_popups": False}), encoding="utf-8"
        )
        import fedtermhub as app  # noqa: PLC0415

        check_addresses(app)
        check_session_problems(app)
        application = Gtk.Application(application_id="io.github.crash_fd01.fedtermhub.RevisioneTest")
        assert application.register(None)
        window = app.AppWindow(application)
        window.show_all()
        pump()
        check_option_injection(app, window)
        check_terminal_tabs(app, window)
        check_multiexec(app, window)
        check_tree(app, window, tmp)
        check_session_dialog(app, window)
        check_settings_once(window)
        check_remote_refresh(app, window)
        window.destroy()
    print("REVISIONE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
