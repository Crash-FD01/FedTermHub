# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prove automatiche di Telnet, seriale, VNC, strumenti di rete, SFTP e tunnel (parte di --self-test).

Telnet e VNC usano i server finti di selftest_servers; la seriale usa la porta virtuale loop:// di pyserial.
SSH, SFTP e tunnel partono solo con FEDTERMHUB_SELFTEST_SSH="host;porta;utente;password" (in CI: il server
OpenSSH del runner con un utente di prova).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Callable

from .. import model
from .. import netcore as net_core
from . import selftest_servers


def run_checks(app, window, check: Callable[[str, Callable[[], object]], None],  # noqa: ANN001, C901
               wait: Callable, report_path: Path) -> None:
    from .files_panel import FilesPanel
    from .main_window import TerminalTab
    from .vnc_tab import VncTab

    window.settings.ssh_strict_host_key = "accept-new"  # la prova non può rispondere alle domande di ssh

    def terminal_tab(sess: model.Session) -> TerminalTab:
        before = window.tabs.count()
        window.open_session(sess)
        assert window.tabs.count() == before + 1, "scheda non aperta"
        tab = window.tabs.currentWidget()
        assert isinstance(tab, TerminalTab), type(tab)
        return tab

    def collect(tab: TerminalTab) -> list:
        output: list = []
        tab.view.pty.output.connect(output.append)
        return output

    def close(tab) -> None:  # noqa: ANN001
        window.close_tab(window.tabs.indexOf(tab))

    # ------------------------------------------------------------------ finestra come la versione Linux
    def layout() -> str:
        from . import theme

        menus = [a.text().replace("&", "") for a in window.menuBar().actions()]
        expected = ["Terminale", "Sessioni", "Rete", "Strumenti", "Vista", "Impostazioni", "Aiuto"]
        if os.environ.get("FEDTERMHUB_LANGUAGE", "it") == "it":
            assert menus == expected, menus
        assert set(window.ribbon_buttons) == {"terminal", "sessions", "network", "tools", "password", "view", "system"}
        for category in ("terminal", "sessions", "network", "tools", "view", "system"):
            assert window.ribbon_buttons[category].menu().actions(), f"tendina {category} vuota"
        assert window.rail.isVisible() and window.sidebar.isVisible()
        # cambio tema dal pulsante e ritorno
        before = theme.is_dark()
        window.toggle_theme()
        assert theme.is_dark() != before, "il pulsante del tema non cambia tema"
        window.toggle_theme()
        assert theme.is_dark() == before
        # Vista › mostra/nascondi pannello, e il menu non si nasconde insieme alla barra dei comandi
        window.view_toggles["show_sidebar"].setChecked(False)
        assert not window.sidebar.isVisible()
        window.view_toggles["show_sidebar"].setChecked(True)
        window.view_toggles["show_ribbon"].setChecked(False)
        window.view_toggles["show_menubar"].setChecked(False)
        assert window.menuBar().isVisible() or window.ribbon.isVisible(), "menu e barra dei comandi nascosti insieme"
        window.view_toggles["show_ribbon"].setChecked(True)
        window.view_toggles["show_menubar"].setChecked(True)
        # cartella colorata e trascinamento di una sessione dentro la cartella
        home = window.store.home_path()
        folder = model.FolderNode(name="cartella-prova", color="#53b175")
        sess = model.Session(name="trascinata", protocol="ssh", host="db.example.lan")
        window.store.add_node(home, folder)
        window.store.add_node(home, sess)
        window.refresh_tree()
        window.drop_node(window.store.find_node_path(sess), window.store.find_node_path(folder), "into")
        assert sess in folder.children, "trascinamento nella cartella non riuscito"
        window.store.remove_node(window.store.find_node_path(folder))
        window.refresh_tree()
        return f"menu {len(menus)}, barra dei comandi {len(window.ribbon_buttons)} pulsanti, tema, Vista, trascinamento"

    check("Finestra come la versione Linux", layout)

    # ------------------------------------------------------------------ doppio clic, benvenuto
    def double_click() -> str:
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QLabel, QPushButton

        from .chrome import PATH_ROLE

        for index in range(window.tabs.count() - 1, -1, -1):
            window.close_tab(index)
        assert window.welcome.isVisible(), "senza schede si vede il benvenuto"
        texts = " ".join(label.text() for label in window.welcome.findChildren(QLabel))
        assert model.APP_NAME in texts and not window.welcome.findChildren(QPushButton), texts
        home = window.store.home_path()
        sess = model.Session(name="doppio-clic", protocol="local")
        window.store.add_node(home, sess)
        window.refresh_tree(select=window.store.find_node_path(sess))
        window.tree.expandAll()
        app.processEvents()
        indexes = window.tree.selectionModel().selectedIndexes()
        assert indexes and indexes[0].data(PATH_ROLE) == window.store.find_node_path(sess), "sessione non selezionata"
        # Sulla finestra, non sul widget: è il percorso dei clic veri, e non lascia il tasto segnato come premuto.
        center: QPoint = window.tree.viewport().mapTo(window, window.tree.visualRect(indexes[0]).center())
        QTest.mouseClick(window.windowHandle(), Qt.MouseButton.LeftButton, pos=center)
        wait(app, lambda: False, 0.6)  # altrimenti i due clic contano come parte del doppio clic
        QTest.mouseDClick(window.windowHandle(), Qt.MouseButton.LeftButton, pos=center)
        wait(app, lambda: False, 0.3)
        opened = window.tabs.count()
        assert opened == 1, f"un doppio clic ha aperto {opened} schede"
        window.close_tab(0)
        window.store.remove_node(window.store.find_node_path(sess))
        window.refresh_tree()
        return "una scheda per doppio clic, benvenuto senza pulsanti"

    check("Doppio clic e benvenuto", double_click)

    # ------------------------------------------------------------------ finestre dei tunnel
    def tunnel_windows() -> str:
        opened: list = []
        try:
            return check_tunnel_windows(opened)
        finally:  # anche se la prova fallisce: una finestra rimasta aperta ruberebbe il fuoco alle prove dopo
            for dialog in opened:
                dialog.close()
                dialog.deleteLater()

    def check_tunnel_windows(opened: list) -> str:
        from .tunnels import TunnelEditDialog, TunnelsDialog

        store = window.tunnels.store
        before = list(store.tunnels)
        tunnel = model.TunnelSession(name="rdp-prova", ssh_host="bastion.example.lan", ssh_user="ops", local_port="13389",
                                     remote_host="10.20.0.5", remote_port="3389", server_alive_interval=45)
        store.upsert(tunnel)
        dialog = TunnelsDialog(window, window.tunnels, window.credential_labels, window)
        opened.append(dialog)
        dialog.resize(dialog.minimumSize())  # si parte dal minimo: lo schermo può essere piccolo (in CI 1024x768)
        dialog.show()
        wait(app, lambda: False, 0.2)
        assert dialog.table.columnCount() == 7 and dialog.selected() is not None
        row = [r for r in range(dialog.table.rowCount()) if dialog.table.item(r, dialog.C_NAME).text() == "rdp-prova"][0]
        dialog.table.selectRow(row)
        assert "13389" in dialog.detail_summary.text() and "45s" in dialog.detail_status.text(), dialog.detail_status.text()
        assert dialog.b_start.isEnabled() and not dialog.b_stop.isEnabled() and dialog.b_session.isEnabled()
        # allargando si allarga la tabella, non le barre né il riepilogo
        heights = (dialog.b_start.height(), dialog.detail_summary.parentWidget().height())
        table_size = (dialog.table.width(), dialog.table.height())
        dialog.resize(dialog.width() + 180, dialog.height() + 120)
        wait(app, lambda: False, 0.2)
        assert dialog.table.width() >= table_size[0] + 150 and dialog.table.height() >= table_size[1] + 100, \
            (table_size, dialog.table.size())
        # il riepilogo può solo accorciarsi: con più larghezza il testo va a capo meno
        assert dialog.b_start.height() == heights[0] and dialog.detail_summary.parentWidget().height() <= heights[1], \
            ("barre allargate", heights, dialog.b_start.height(), dialog.detail_summary.parentWidget().height())
        widths = [dialog.table.columnWidth(c) for c in range(7)]
        assert widths[dialog.C_AUTO] == 54 and widths[dialog.C_NAME] > 150, widths
        # log, cerca, duplica
        dialog.toggle_log()
        assert dialog.log_box.isVisible() and dialog.b_log.isEnabled()
        dialog.toggle_log()
        dialog.search.setText("nessun-tunnel-così")
        assert dialog.table.rowCount() == 0 and dialog.selected() is None
        dialog.search.clear()
        dialog.table.selectRow([r for r in range(dialog.table.rowCount())
                                if dialog.table.item(r, dialog.C_NAME).text() == "rdp-prova"][0])
        dialog.duplicate()
        clone = dialog.selected()
        assert clone is not None and clone.id != tunnel.id and not clone.auto_start
        store.remove(clone.id)
        dialog.refresh(tunnel.id)
        # Editor: tipi, percorso, anteprima e nessun campo perso nel salvataggio
        editor = TunnelEditDialog(dialog, tunnel, [], window.settings, window.tunnels.identity)
        opened.insert(0, editor)
        editor.resize(editor.minimumSize())
        editor.show()
        wait(app, lambda: False, 0.2)
        assert editor.current_type() == "local" and editor.flow_right.detail.text() == "10.20.0.5:3389"
        assert "-L" in editor.command_preview.toPlainText(), editor.command_preview.toPlainText()
        editor.radio_remote.setChecked(True)
        assert editor.arrow1.text() == "←" and editor.destination_stack.currentIndex() == 0
        editor.radio_dynamic.setChecked(True)
        assert editor.destination_stack.currentIndex() == 1 and "-D" in editor.command_preview.toPlainText()
        editor.radio_local.setChecked(True)
        saved = editor.result_tunnel()
        assert saved.to_dict() == tunnel.to_dict(), "l'editor cambia i campi del tunnel"
        name_width, name_height = editor.name.width(), editor.ssh_host.height()
        editor.resize(editor.width() + 180, editor.height() + 120)
        wait(app, lambda: False, 0.2)
        assert editor.name.width() >= name_width + 150 and editor.ssh_host.height() == name_height, "editor allargato male"
        editor.ssh_host.clear()
        editor._accept()
        assert editor.validation.isVisibleTo(editor) and editor.result() == 0, "salvato senza server SSH"
        store.remove(tunnel.id)
        assert [t.id for t in store.tunnels] == [t.id for t in before]
        return "elenco, stato, ridimensionamento, editor"

    check("Finestre dei tunnel", tunnel_windows)

    # ------------------------------------------------------------------ terminale diviso
    def seen(out: list, marker: str) -> bool:
        return wait(app, lambda: marker in "".join(out), 30)

    def press(key) -> bool:  # noqa: ANN001
        """Un tasto vero, che passa dalle scorciatoie della finestra. Falso se la finestra non è attiva."""
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        if app.activeWindow() is not window:
            return False
        QTest.keyClick(window.windowHandle(), key, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        return True

    def split() -> str:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QSplitter

        tab = window.open_split_terminal()
        assert len(tab.panes) == 2 and tab.pane_layout == "tall" and tab.split_tab, (len(tab.panes), tab.pane_layout)
        first, second = tab.panes
        assert wait(app, lambda: first.view.ready and second.view.ready, 30), "xterm.js non si è caricato nei riquadri"
        assert wait(app, lambda: first.running and second.running, 15), "un riquadro non è partito"
        assert first.isVisible() and second.isVisible(), "riquadri non visibili"
        wait(app, lambda: False, 1.5)  # le pagine appena pronte possono prendersi il fuoco un momento dopo
        assert tab.active_pane is second, "dopo la divisione il riquadro attivo non è quello nuovo"
        if app.activeWindow() is window:
            assert wait(app, lambda: window._focused_pane() is second, 5), "i tasti non andrebbero al riquadro nuovo"
        root = tab._root
        assert isinstance(root, QSplitter) and root.orientation() == Qt.Orientation.Horizontal, type(root)
        out1, out2 = [], []
        first.view.pty.output.connect(out1.append)
        second.view.pty.output.connect(out2.append)
        # il riquadro attivo cambia con i tasti, e ogni riquadro ha la sua shell
        assert tab.active_pane is second
        tab.dispatch("prev_pane")
        assert tab.active_pane is first
        tab.dispatch("next_pane")
        assert tab.active_pane is second
        first.view.on_input("echo FTH_UNO_$((6*7))\r")
        assert seen(out1, "FTH_UNO_42"), "riquadro 1: " + "".join(out1)[-200:]
        assert tab.active_pane is first, "scrivere in un riquadro lo rende attivo"
        assert "FTH_UNO" not in "".join(out2), "il riquadro 2 ha ricevuto i tasti del riquadro 1"
        # broadcast: il testo va ovunque, Ctrl+C e le risposte del terminale no
        tab.dispatch("toggle_broadcast")
        assert tab.broadcast and tab._toast.isVisible(), "broadcast senza avviso"
        first.view.on_input("echo FTH_TUTTI_$((6*7))")  # come dalla tastiera: il testo, poi Invio
        first.view.on_input("\r")
        assert seen(out1, "FTH_TUTTI_42") and seen(out2, "FTH_TUTTI_42"), "broadcast: " + "".join(out2)[-200:]
        before = len("".join(out2))
        tab.pane_input(first, "\x1b[12;5R")  # senza scriverla nella shell del riquadro 1
        wait(app, lambda: False, 0.5)
        assert "12;5R" not in "".join(out2)[before:], "una risposta del terminale è finita nell'altro riquadro"
        tab.dispatch("toggle_broadcast")
        first.view.on_input("echo FTH_SOLO_$((6*7))\r")
        assert seen(out1, "FTH_SOLO_42")
        assert "FTH_SOLO" not in "".join(out2), "broadcast spento: il testo è arrivato all'altro riquadro"
        # altre divisioni e disposizioni: i programmi restano vivi
        tab.dispatch("split_h")
        assert len(tab.panes) == 3 and tab.pane_layout == "fat" and tab._root.orientation() == Qt.Orientation.Vertical
        layouts = []
        for _ in range(4):
            tab.dispatch("cycle_layout")
            layouts.append(tab.pane_layout)
            wait(app, lambda: False, 0.2)
        assert layouts == ["grid", "stack", "tall", "fat"], layouts
        tab.dispatch("cycle_layout")
        tab.dispatch("cycle_layout")  # stack: si vede solo il riquadro attivo
        assert tab.pane_layout == "stack" and tab.active_pane.isVisible()
        assert sum(p.isVisible() for p in tab.panes) == 1, "stack: più di un riquadro visibile"
        tab.dispatch("next_pane")
        assert tab.active_pane.isVisible() and sum(p.isVisible() for p in tab.panes) == 1
        tab.dispatch("cycle_layout")
        assert all(p.isVisible() for p in tab.panes) and first.running and second.running
        first.view.on_input("echo FTH_DOPO_$((6*7))\r")
        assert seen(out1, "FTH_DOPO_42"), "il riquadro non risponde dopo i cambi di disposizione"
        # exit chiude solo quel riquadro; Ctrl+Shift+W chiude il riquadro attivo e l'ultimo chiude la scheda
        tab.panes[-1].view.on_input("exit\r")
        assert wait(app, lambda: len(tab.panes) == 2, 20), "exit in un riquadro non lo chiude"
        tab.active_pane = second
        tab.dispatch("close_pane")
        assert tab.panes == [first] and tab.active_pane is first
        assert window.tabs.indexOf(tab) >= 0
        tab.dispatch("close_pane")
        assert window.tabs.indexOf(tab) < 0, "l'ultimo riquadro non chiude la scheda"
        return "riquadri, disposizioni, broadcast, chiusura"

    check("Terminale diviso", split)

    def split_keys() -> str:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        window.show()
        window.raise_()
        window.activateWindow()
        tab = terminal_tab(model.Session(name="tasti", protocol="local"))
        assert wait(app, lambda: tab.view.ready, 30), "xterm.js non si è caricato"
        tab.focus_active()
        if not wait(app, lambda: app.activeWindow() is window and window._focused_pane() is tab.active_pane, 5):
            close(tab)
            return "finestra non attiva su questo sistema: tasti non provati, azioni provate sopra"
        out = collect(tab)
        files_before = window.sidebar_tabs.currentIndex()
        assert press(Qt.Key.Key_O) and len(tab.panes) == 2 and tab.pane_layout == "tall", "Ctrl+Shift+O"
        assert wait(app, lambda: tab.active_pane.view.ready and window._focused_pane() is tab.active_pane, 30), \
            "il fuoco non è passato al riquadro nuovo"
        assert press(Qt.Key.Key_E) and len(tab.panes) == 3 and tab.pane_layout == "fat", "Ctrl+Shift+E nel terminale"
        assert window.sidebar_tabs.currentIndex() == files_before, "Ctrl+Shift+E ha aperto i file remoti"
        assert wait(app, lambda: tab.active_pane.view.ready and window._focused_pane() is tab.active_pane, 30)
        assert press(Qt.Key.Key_P) and tab.broadcast, "Ctrl+Shift+P nel terminale"
        assert press(Qt.Key.Key_P) and not tab.broadcast
        assert press(Qt.Key.Key_L) and tab.pane_layout == "grid", "Ctrl+Shift+L"
        assert press(Qt.Key.Key_W) and len(tab.panes) == 2, "Ctrl+Shift+W"
        assert not any(ch in "".join(out) for ch in ("\x0f", "\x05")), "i tasti sono arrivati anche al programma"
        # un passaggio di fuoco non chiesto torna indietro; un clic del mouse cambia il riquadro attivo
        active = tab.active_pane
        other = tab.panes[0] if active is not tab.panes[0] else tab.panes[1]
        other.view.setFocus()
        assert wait(app, lambda: window._focused_pane() is active, 5) and tab.active_pane is active, \
            "il fuoco non chiesto ha cambiato riquadro"
        center = other.view.rect().center()
        target = other.view.mapTo(window, center)
        QTest.mouseClick(window.windowHandle(), Qt.MouseButton.LeftButton, pos=target)
        assert wait(app, lambda: tab.active_pane is other and window._focused_pane() is other, 5), \
            "il clic non cambia il riquadro attivo"
        # fuori dal terminale Ctrl+Shift+E resta File remoti (qui non c'è SSH: solo il messaggio) e non divide
        window.tree.setFocus()
        wait(app, lambda: False, 0.2)
        assert press(Qt.Key.Key_E) and len(tab.panes) == 2, "Ctrl+Shift+E fuori dal terminale ha diviso"
        close(tab)
        return "Ctrl+Shift+O, E, P, L, W con tasti veri; E fuori dal terminale"

    check("Tasti dei riquadri", split_keys)

    # ------------------------------------------------------------------ MultiExec
    def multiexec() -> str:
        from .chrome import MultiExecDialog
        from . import connect as launch_for

        for index in range(window.tabs.count() - 1, -1, -1):  # schede rimaste da una prova fallita
            window.close_tab(index)
        fake = launch_for.local_shell(window.settings)
        fake.title, fake.close_on_success = "web01", False
        ssh_tab = window.add_terminal_tab(fake, protocol="ssh")  # il comando è una shell: conta il tipo di scheda
        local = window.add_terminal_tab(launch_for.local_shell(window.settings), protocol="local")
        split_tab = window.open_split_terminal()
        tabs = (ssh_tab, local, split_tab)
        assert wait(app, lambda: all(p.view.ready and p.running for t in tabs for p in t.panes), 40), "terminali non pronti"
        targets = {t[0]: t for t in window.multiexec_targets()}
        assert targets[ssh_tab][3] and not targets[local][3] and not targets[split_tab][3], "spunte iniziali"
        assert "SSH" in targets[ssh_tab][2]
        dialog = MultiExecDialog(window, list(targets.values()))
        assert not dialog.send_button.isEnabled(), "Invia attivo senza comando"
        dialog.command.setText("echo FTH_MULTI_$((6*7))")
        assert dialog.send_button.isEnabled() and "1" in dialog.send_button.text(), dialog.send_button.text()
        assert dialog.selected_targets() == [ssh_tab]
        for check_box, target in dialog.checks:
            check_box.setChecked(target is not local)
        chosen = dialog.selected_targets()
        assert chosen == [ssh_tab, split_tab] and "2" in dialog.send_button.text(), dialog.send_button.text()
        command = dialog.command_text()
        dialog.deleteLater()
        outputs = {id(p): collect_pane(p) for t in tabs for p in t.panes}
        sent = window.send_multiexec(command, chosen)
        assert sent == 3, f"inviato a {sent} terminali"
        for pane in ssh_tab.panes + split_tab.panes:
            assert seen(outputs[id(pane)], "FTH_MULTI_42"), "".join(outputs[id(pane)])[-200:]
        assert "FTH_MULTI" not in "".join(outputs[id(local.panes[0])]), "la scheda non spuntata ha ricevuto il comando"
        for tab in tabs:
            window.close_tab(window.tabs.indexOf(tab))
        return f"{sent} terminali su 4, nessuna scheda: " + str(not window.multiexec_targets())

    def collect_pane(pane) -> list:  # noqa: ANN001
        output: list = []
        pane.view.pty.output.connect(output.append)
        return output

    check("MultiExec", multiexec)

    # ------------------------------------------------------------------ Telnet
    def telnet() -> str:
        server = selftest_servers.start_telnet()
        tab = terminal_tab(model.Session(name="telnet-prova", protocol="telnet", host="127.0.0.1", port=str(server.port)))
        out = collect(tab)
        assert wait(app, lambda: "login:" in "".join(out), 15), "nessuna risposta dal server Telnet: " + "".join(out)[-200:]
        wait(app, lambda: tab.view.ready, 20)
        tab.view.on_input("prova\r")
        assert wait(app, lambda: "FTH_TELNET_OK" in "".join(out), 10), "risposta mancante: " + "".join(out)[-200:]
        line = [l for l in "".join(out).splitlines() if "FTH_TELNET_OK" in l][0].strip()
        size = line.split()[-1]
        assert size != "0x0", "dimensioni della finestra (NAWS) non inviate"
        close(tab)
        server.close()
        return f"{line}; opzioni: {', '.join(server.log[:4])}"

    check("Telnet integrato", telnet)

    # ------------------------------------------------------------------ seriale
    def serial() -> str:
        tab = terminal_tab(model.Session(name="seriale-prova", protocol="serial", host="loop://", port="9600"))
        out = collect(tab)
        assert wait(app, lambda: tab.running, 10), "porta non aperta: " + "".join(out)[-200:]
        tab.view.on_input("FTH_SERIALE")
        assert wait(app, lambda: "FTH_SERIALE" in "".join(out), 10), "nessuna eco dalla porta: " + "".join(out)[-200:]
        close(tab)
        return "loop:// 9600 8N1"

    check("Console seriale", serial)

    # ------------------------------------------------------------------ VNC
    def vnc() -> str:
        server = selftest_servers.start_vnc("Vnc-prova")
        tab = VncTab(window, model.Session(name="vnc-prova", protocol="vnc", host="127.0.0.1", port=str(server.port)),
                     "", "Vnc-prova")
        window.tabs.setCurrentIndex(window.tabs.addTab(tab, "vnc-prova"))
        window._update_welcome()
        assert wait(app, lambda: tab.connected or any(e.startswith("disconnected") for e in tab.events), 30), \
            "il client VNC non risponde: " + ", ".join(tab.events)
        assert tab.connected, "connessione VNC fallita: " + ", ".join(tab.events) + " " + tab.state_label.text()
        wait(app, lambda: "frame" in server.log, 10)
        wait(app, lambda: False, 1.5)
        pixels: list = []
        tab.view.page().runJavaScript("fthPixel(20, 20)", 0, pixels.append)
        tab.view.page().runJavaScript("fthPixel(300, 190)", 0, pixels.append)
        assert wait(app, lambda: len(pixels) == 2, 10), "schermo VNC non leggibile"
        top_left, bottom_right = (json.loads(p) for p in pixels)
        assert top_left[:3] == [255, 0, 0], f"colore in alto a sinistra {top_left[:3]} invece di rosso"
        assert bottom_right[:3] == [255, 255, 255], f"colore in basso a destra {bottom_right[:3]} invece di bianco"
        tab.page_bridge.sendKeys.emit("[65]")
        assert wait(app, lambda: 65 in server.keys, 5), "tasto non arrivato al server"
        window.grab().save(str(report_path.with_name("vnc-session.png")))
        tab.close_tab()
        close(tab)
        server.close()
        return f"schermo {top_left[3]}x{top_left[4]}, autenticazione e tastiera ok"

    check("VNC incorporato", vnc)

    # ------------------------------------------------------------------ strumenti di rete
    def tools() -> str:
        folder = Path(tempfile.mkdtemp(prefix="fth-tools-"))
        (folder / "config.txt").write_bytes(b"hostname switch-prova\n" * 50)
        logs: list = []
        tftp = net_core.TftpServer(folder, 0, logs.append, bind="127.0.0.1")
        data = net_core.tftp_get("127.0.0.1", tftp.port, "config.txt")
        tftp.stop()
        assert data == (folder / "config.txt").read_bytes(), "TFTP: file diverso"
        http = net_core.HttpFileServer(folder, 0, logs.append, bind="127.0.0.1")
        body = urllib.request.urlopen(f"http://127.0.0.1:{http.port}/config.txt", timeout=5).read()  # noqa: S310
        http.stop()
        assert body == data, "HTTP: file diverso"
        server = selftest_servers.start_telnet()
        import threading

        found = net_core.scan_ports("127.0.0.1", [server.port, selftest_servers.free_port()], lambda *_: None,
                                    lambda *_: None, threading.Event())
        assert [r.port for r in found] == [server.port], f"scansione porte: {found}"
        listening = net_core.listening_ports()
        assert any(item.port == server.port for item in listening), "porta in ascolto non trovata"
        rtt = net_core.ping("127.0.0.1")
        assert rtt is not None, "ping di 127.0.0.1 senza risposta"
        server.close()
        window.open_network_tools()
        wait(app, lambda: False, 0.5)
        window._net_tools.grab().save(str(report_path.with_name("network-tools.png")))
        window._net_tools.close()
        return f"TFTP, HTTP, scansione porte, {len(listening)} porte in ascolto, ping {rtt:.0f} ms"

    check("Strumenti di rete", tools)

    # ------------------------------------------------------------------ SSH, SFTP, tunnel (server vero)
    target = os.environ.get("FEDTERMHUB_SELFTEST_SSH", "")
    if not target:
        return
    host, port, user, password = (target.split(";", 3) + ["", "", "", ""])[:4]

    def ssh_session() -> str:
        sess = model.Session(name="ssh-prova", protocol="ssh", host=host, port=port, user=user)
        window.settings.show_remote_files = True
        window.identity = lambda s, _orig=window.identity: (user, "", password) if s.name == "ssh-prova" else _orig(s)
        tab = terminal_tab(sess)
        out = collect(tab)
        assert wait(app, lambda: tab.view.ready, 20), "terminale non pronto"
        assert wait(app, lambda: not tab.feeder.armed and tab.running, 30), \
            "password non scritta: " + "".join(out)[-300:]
        # Il primo accesso di un utente nuovo su Windows crea il suo profilo: il prompt può tardare.
        mark = len("".join(out))
        prompt = re.compile(r"[>$#]\s*$")
        wait(app, lambda: bool(prompt.search(re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", "".join(out)[mark:]).rstrip("\r\n"))), 90)
        wait(app, lambda: False, 1)
        tab.view.on_input("echo FTH_SSH_$((6*7))_OK\r")
        ok = wait(app, lambda: "FTH_SSH_42_OK" in "".join(out) or "FTH_SSH_$((6*7))_OK" in "".join(out).replace("echo FTH", ""), 40)
        assert ok, "nessuna risposta dalla shell remota: " + "".join(out)[-300:]
        # Pannello dei file nella scheda «File remoti» del pannello laterale, come nella versione Linux
        assert wait(app, lambda: tab.files is not None and tab.files.connected, 30), \
            "pannello file non collegato: " + (tab.files.state.text() if tab.files else "non aperto")
        assert window.sidebar_tabs.isTabVisible(1), "scheda File remoti non mostrata per la sessione SSH"
        assert window.files_stack.indexOf(tab.files) >= 0, "pannello file fuori dal pannello laterale"
        window.toggle_remote_files()
        assert window.sidebar_tabs.currentIndex() == 1 and window.files_stack.currentWidget() is tab.files, \
            "File remoti (Ctrl+Maiusc+E) non mostra i file della scheda"
        assert wait(app, lambda: tab.files.model.rowCount() > 0 or tab.files.cwd, 15), "cartella remota vuota"
        followed = ""
        if not re.match(r"^/?[A-Za-z]:", tab.files.home):
            # Server Unix: la shell dichiara la cartella (OSC 7) e il pannello la segue dopo un cd.
            assert wait(app, lambda: bool(tab.view.last_cwd), 15), "la shell non dichiara la cartella (OSC 7)"
            tab.view.on_input("cd /tmp\r")
            assert wait(app, lambda: tab.files.cwd == "/tmp", 15), f"il pannello non segue il cd: {tab.files.cwd}"
            followed = ", segue il cd"
        wait(app, lambda: False, 1.5)
        # Lo schermo intero, non window.grab(): il terminale (WebEngine) su Windows non sempre compare nel grab.
        app.primaryScreen().grabWindow(0).save(str(report_path.with_name("ssh-session.png")))
        close(tab)
        assert not window.sidebar_tabs.isTabVisible(1), "scheda File remoti rimasta dopo la chiusura della sessione"
        return f"{user}@{host}:{port}, password scritta al prompt, pannello file in {tab.files.home}{followed}"

    check("SSH con password", ssh_session)

    def sftp() -> str:
        from .sftp_client import SftpTarget

        panel = FilesPanel(SftpTarget(host=host, port=int(port or 22), user=user, password=password, strict="accept-new"))
        panel.connect_server()
        assert wait(app, lambda: panel.connected and panel.cwd, 30), "SFTP non collegato: " + panel.state.text()
        local = Path(tempfile.mkdtemp(prefix="fth-sftp-")) / "fth-prova.bin"
        local.write_bytes(os.urandom(200_000))
        panel.upload([local])
        assert wait(app, lambda: "Caricamento completato" in panel.state.text() or "completat" in panel.state.text(), 30), \
            "caricamento: " + panel.state.text()
        assert wait(app, lambda: any(panel.model.item(r, 0).text() == local.name for r in range(panel.model.rowCount())), 10), \
            "file caricato non elencato"
        remote = panel.cwd.rstrip("/") + "/" + local.name
        back = local.with_name("ritorno.bin")
        done: list = []
        panel.worker.done.connect(lambda tag, _r: done.append(tag))
        panel.worker.run("download", lambda s: s.download(remote, back, lambda *_: None))
        assert wait(app, lambda: "download" in done, 30), "download non finito: " + panel.state.text()
        assert back.read_bytes() == local.read_bytes(), "il file scaricato è diverso"
        panel.worker.run("delete", lambda s: s.remove(remote))
        wait(app, lambda: "delete" in done, 15)
        panel.shutdown()
        return f"caricati e riscaricati {local.stat().st_size} byte in {panel.cwd}"

    check("SFTP", sftp)

    def tunnel() -> str:
        from .tunnels import TunnelProbe

        server = selftest_servers.start_telnet()
        local_port = selftest_servers.free_port()
        tunnel = model.TunnelSession(name="tunnel-prova", ssh_host=host, ssh_port=port or "22", ssh_user=user,
                                     local_port=str(local_port), remote_host="127.0.0.1", remote_port=str(server.port))
        window.tunnels.store.upsert(tunnel)
        window.tunnels.identity = lambda t: (user, password)
        probe_result: list = []
        probe = TunnelProbe(tunnel, window.settings, window.tunnels.identity)
        probe.finished.connect(lambda ok, message, details: probe_result.append((ok, message, details)))
        probe.start()
        assert wait(app, lambda: probe_result, 40), "Verifica SSH senza risposta"
        assert probe_result[0][0], f"Verifica SSH fallita: {probe_result[0]}"
        result: list = []
        window.tunnels.when_ready(tunnel.id, lambda ok, log: result.append((ok, log)))
        assert wait(app, lambda: result, 40), "il tunnel non risponde"
        assert result[0][0], "tunnel non avviato: " + result[0][1]
        import socket

        with socket.create_connection(("127.0.0.1", local_port), timeout=5) as sock:
            sock.settimeout(5)
            data = b""
            deadline = time.monotonic() + 5
            while b"login:" not in data and time.monotonic() < deadline:
                data += sock.recv(1024)
        assert b"login:" in data, f"nessun dato attraverso il tunnel: {data!r}"
        window.tunnels.stop(tunnel.id)
        server.close()
        return f"127.0.0.1:{local_port} → {host} → 127.0.0.1:{server.port}"

    check("Tunnel SSH", tunnel)
