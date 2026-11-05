<!--
SPDX-FileCopyrightText: 2026 Crash
SPDX-License-Identifier: GPL-3.0-or-later
-->
# FedTermHub — note per Claude

Gestore di terminali, SSH, RDP, VNC e file remoti per sistemisti. Autore: Crash (GitHub Crash-FD01).
Tutto in **italiano**: testi dell'interfaccia, commenti, messaggi di commit, PR, documentazione.

## Regole fisse
- **Mai dati veri dei clienti**: niente hostname, IP, utenti o nomi di aziende reali in codice, test, schermate,
  documentazione. Dati dimostrativi solo inventati (`example.lan`, «Studio Rossi», `10.20.0.x`), come in
  `tools/screenshots.py`. Niente nome di battesimo dell'autore nei dati di prova.
- La **chiave privata di firma** dei pacchetti non entra mai nel repository (`*PRIVATE*.asc` è in .gitignore).
- **Due interfacce, un nucleo comune** (decisione del 5/11/2026): Linux resta su GTK, Windows su Qt, ognuna con
  quello che funziona meglio sul suo sistema. La logica va nel nucleo comune, non dentro un'interfaccia.

## Mappa del codice
Nucleo comune (né GTK né Qt):
- `fedtermhub/model.py` — versione (`APP_VERSION`), percorsi, Session/FolderNode/SessionStore, vault cifrato,
  tunnel, AppSettings, import/export delle sessioni.
- `fedtermhub/commands.py` — comandi ssh, ProxyJump, tunnel (`ssh_session_argv`, `tunnel_argv`, controlli).
- `fedtermhub/netcore.py` — strumenti di rete (scansioni, porte in ascolto, WoL, server HTTP/TFTP, ping).
- `fedtermhub/look.py` — colori della finestra, colori delle icone, icone delle cartelle, colori del terminale.
- `fedtermhub/i18n.py` + `fedtermhub/locale/en.po` — testi: si scrive `tr("testo italiano")`.

Linux (GTK 3), pacchetti RPM/DEB:
- `fedtermhub/fedtermhub.py` — **13.000 righe: non leggerlo intero**, cerca per nome. Punti utili:
  `class AppWindow`, menu in `make_menu(tr("Terminale")`, tendine della barra in `def show_hud_menu`,
  fogli di stile in `def dark_css` / `def light_css` (le regole finali sono nei blocchi «restyle 3.21»),
  icone in `def register_app_icons`, albero in `def refresh_sessions`, dialoghi `class SessionDialog`,
  `TunnelManagerDialog`, `VaultManagerDialog`, `SettingsDialog`, `MultiExecDialog`, `SplitTerminalTab`.
- Altri moduli GTK: `terminal_panel.py` (VTE, riquadri), `network_tools.py`, `remote_files.py` (SFTP),
  `native_rdp.py` + `native/` (helper C su FreeRDP), `ephemeral_lab.py` (libvirt), `connection_diagnostics.py`,
  `windows_sessions.py`, `serial_bridge.py`, `distro.py`.

Windows (Qt 6 / PySide6), Setup.exe — `fedtermhub/qtui/`:
- `app.py` avvio e `--self-test`; `main_window.py` finestra (TerminalTab, MainWindow); `chrome.py` albero con
  trascinamento, dialoghi cartella/log/scorciatoie; `theme.py` tema da look.py; `quick_tools.py` test rapidi
  PowerShell; `terminal_view.py` + `xterm/` terminale; `pty_process.py` ConPTY; `connect.py` comandi di avvio;
  `session_dialog.py`, `settings_dialog.py`, `vault_dialogs.py`, `tunnels.py`, `net_tools.py`, `files_panel.py`
  + `sftp_client.py` (paramiko), `rdp_tab.py` (ActiveX via ATL+comtypes), `vnc_tab.py` + `ws_bridge.py` + `novnc/`,
  `stream_process.py` (Telnet, seriale), `updates.py`, `selftest_more.py` + `selftest_servers.py`.
- Pacchetto: `packaging/windows/` (build.ps1, fedtermhub.iss, requirements.txt).

Documentazione: `README.md`, `docs/GUIDA.md`, `docs/PARITA.md` (cosa c'è su Linux e su Windows),
`CONTRIBUTING.md`, `site/index.html`; changelog in `fedtermhub.spec` (%changelog).

## Quando si cambia qualcosa
- Voce nuova nei menu GTK → aggiungerla a `docs/PARITA.md` (lo controlla `tests/test_commands.py`).
  Funzione nuova su Windows → «sì» in PARITA.md e un controllo in `qtui/selftest_more.py`.
- Testi nuovi → `python3 tools/i18n_update.py`, poi riempire i `msgstr ""` nuovi in `en.po`
  (stessi segnaposto `{nome}`). La CI fallisce se il catalogo non è aggiornato.
- Colori o icone → `look.py`, che serve entrambe le interfacce.
- Nuovo file → intestazione SPDX (o coperto da `REUSE.toml`); `reuse lint` deve passare.

## Prove
- `tools/test_all.sh [fast|linux|qt]` — tutte le prove, una riga ciascuna, log solo per quelle fallite.
  GTK vuole un python con `gi` (lo script lo cerca; in sandbox di solito `python3.12`); Qt usa `.venv-qt/` o
  `FTH_QT_PYTHON`. Con `FEDTERMHUB_SELFTEST_SSH="host;porta;utente;password"` il self-test Qt prova anche
  SSH, SFTP e tunnel.
- Schermate per il confronto: `tools/screenshots.py` (GTK) e `tools/screenshots_qt.py` (Qt), sotto
  `xvfb-run -a -s "-screen 0 1400x880x24"`. Per Qt sotto xcb serve `libxcb-cursor0`.

## Versioni, PR e rilascio
- La versione sta in 4 posti: `model.py`, `fedtermhub.spec` (Version + voce in %changelog), `install.sh`,
  `packaging/io.github.crash_fd01.fedtermhub.metainfo.xml` (<release>). `tests/test_versions.py` li confronta.
- Un cambio di versione su `main` fa partire la **pubblicazione automatica** (tag, RPM/DEB, Setup.exe,
  SHA256SUMS firmato). Le PR solo di documentazione o strumenti non cambiano versione.
- GitHub **solo REST** (`gh api`), GraphQL non è disponibile: `gh pr create` non funziona.
  - aprire una PR: `gh api repos/Crash-FD01/FedTermHub/pulls -f base=main -f head=<ramo> -f title=… -f body=…`
  - pronta per la revisione: `gh api -X POST repos/Crash-FD01/FedTermHub/pulls/<n>/ccr/ready_for_review`
  - merge: `gh api -X PUT repos/Crash-FD01/FedTermHub/pulls/<n>/merge -f merge_method=squash`
  - dopo un push: `git update-ref refs/remotes/origin/<ramo> HEAD`
- CI: `tools/ci_wait.sh <n> [--windows [cartella]]` aspetta e stampa solo riassunto ed errori (lanciarlo in
  background: una sola notifica). Log di un job: `gh api repos/Crash-FD01/FedTermHub/actions/jobs/<id>/logs`.
  Gli artifact non si scaricano: le schermate del self-test di Windows sono in base64 nel log
  (`ci_wait.sh --windows DIR` le estrae).

## Lavorare risparmiando
- Cercare con grep e leggere solo l'intervallo che serve; mai file interi grandi.
- Immagini (schermate) solo per la verifica visiva finale, non a ogni ritocco.
- CI e attese lunghe: un solo comando in background che termina a lavoro finito, non un controllo per evento.

## Trappole già incontrate
- PySide6 + QtWebEngine: un eventFilter Python su tutta la QApplication fa cadere il programma (l'inattività si
  legge con GetLastInputInfo). Alla chiusura WebEngine/mstscax cadono: si esce con TerminateProcess.
- QAxWidget di PySide6 non funziona per l'RDP: si usa AtlAxWin + comtypes.
- Lo stile Qt su QSpinBox rompe le frecce: non stilizzarlo.
- In CI, `Start-Process -Wait` aspetta anche i processi figli (il programma riaperto dal Setup): usare WaitForExit.
- GTK su openSUSE Leap 16: collegare i segnali con `user_data=None` fallisce; i callback si collegano senza.
- Il self-test Qt non deve aprire finestre modali (`_no_dialogs` in app.py): bloccherebbero la CI.
