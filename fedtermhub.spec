Name:           fedtermhub
# Epoch 1: le prime build private usavano la numerazione 3.x prima del
# passaggio alla serie 0.9.x. Senza Epoch, dnf considererebbe 0.9.x un
# downgrade e rifiuterebbe l'aggiornamento.
Epoch:          1
Version:        0.9.12
Release:        1%{?dist}
Summary:        Session manager for terminals, SSH, RDP, VNC and SFTP
License:        GPL-3.0-or-later
URL:            https://github.com/Crash-FD01/FedTermHub
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz

BuildRequires:  gcc
BuildRequires:  make
BuildRequires:  python3
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib
BuildRequires:  xorg-x11-server-Xvfb
BuildRequires:  python3-gobject
BuildRequires:  python3-cairo
BuildRequires:  gtk3
BuildRequires:  vte291
BuildRequires:  python3-cryptography
BuildRequires:  python3-defusedxml
BuildRequires:  pkgconfig(freerdp3)
BuildRequires:  pkgconfig(freerdp-client3)
BuildRequires:  pkgconfig(winpr3)
Requires:       python3-gobject
Requires:       python3-cairo
Requires:       gtk3
Requires:       vte291
Requires:       openssh-clients
Requires:       iproute
Recommends:     nmap
Recommends:     tcpdump
Requires:       freerdp
Recommends:     tigervnc
Recommends:     telnet
Requires:       python3-cryptography
Requires:       python3-defusedxml
Recommends:     python3-pyserial
Recommends:     python3-winrm
Recommends:     sshpass
Requires:       adwaita-icon-theme
Recommends:     papirus-icon-theme
# Necessari per incollare su Linux i FILE copiati dentro una sessione RDP:
# PyGObject non permette a GTK di diventare proprietario della selezione con
# dati serviti da callback, quindi la lista file viene pubblicata da wl-copy
# (Wayland) o xclip (X11).
Recommends:     wl-clipboard
Recommends:     xclip
Suggests:       openssh-server
Suggests:       vsftpd
Suggests:       tftp-server
Suggests:       telnet-server
Suggests:       nfs-utils
Suggests:       firewalld
Suggests:       tigervnc-server-common
Suggests:       tigervnc-x11-server
Suggests:       tigervnc-wayland-server
Recommends:     libvirt-daemon-kvm
Recommends:     libvirt-client
Recommends:     virt-install
Recommends:     virt-viewer
Recommends:     qemu-img

%description
FedTermHub is a session manager and remote desktop client for Linux. It
brings local terminals, SSH sessions, RDP and VNC desktops, SFTP file
transfer, serial consoles and SSH tunnels together in one GTK application,
with sessions organized in folders and credentials kept in an encrypted
vault protected by a master password. The native RDP backend is built
directly on FreeRDP 3 and renders sessions both in tabs and in detached
windows.

%prep
%autosetup

%build
# set_build_flags esporta CFLAGS/CXXFLAGS/LDFLAGS con %%{optflags} e
# %%{build_ldflags} della distribuzione: senza, l'helper C veniva compilato
# con i soli flag di ripiego del Makefile, quindi senza l'hardening Fedora.
%set_build_flags
%make_build -C native

%install
mkdir -p %{buildroot}%{_datadir}/fedtermhub
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_libexecdir}/fedtermhub
mkdir -p %{buildroot}%{_datadir}/applications
mkdir -p %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
cp -R fedtermhub %{buildroot}%{_datadir}/fedtermhub/
install -m 0755 native/fedtermrdp-native %{buildroot}%{_libexecdir}/fedtermhub/fedtermrdp-native
install -m 0644 fedtermhub.desktop %{buildroot}%{_datadir}/applications/io.github.crash_fd01.fedtermhub.desktop
mkdir -p %{buildroot}%{_metainfodir}
install -m 0644 packaging/io.github.crash_fd01.fedtermhub.metainfo.xml %{buildroot}%{_metainfodir}/io.github.crash_fd01.fedtermhub.metainfo.xml
cp -a share/icons/hicolor/* %{buildroot}%{_datadir}/icons/hicolor/
cat > %{buildroot}%{_bindir}/fedtermhub <<EOF
#!/usr/bin/bash
export FEDTERMHUB_RDP_NATIVE_HELPER="%{_libexecdir}/fedtermhub/fedtermrdp-native"
exec /usr/bin/python3 "%{_datadir}/fedtermhub/fedtermhub/fedtermhub.py" "\$@"
EOF
chmod 0755 %{buildroot}%{_bindir}/fedtermhub
# Solo l'entry point e' eseguibile. Gli altri sono moduli importati: renderli
# 0755 senza shebang fa scattare rpmlint (non-executable-script) e suggerisce
# a torto che siano avviabili da soli.
chmod 0755 %{buildroot}%{_datadir}/fedtermhub/fedtermhub/fedtermhub.py
find %{buildroot}%{_datadir}/fedtermhub/fedtermhub -name '*.py' \
    ! -name 'fedtermhub.py' -exec chmod 0644 {} +

%check
/usr/bin/python3 -m compileall -q fedtermhub
bash -n install.sh uninstall.sh
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.crash_fd01.fedtermhub.desktop
appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/io.github.crash_fd01.fedtermhub.metainfo.xml
xvfb-run -a /usr/bin/python3 tests/test_core.py
/usr/bin/python3 tests/test_theme_parity.py
/usr/bin/python3 tests/test_connection_diagnostics.py
xvfb-run -a /usr/bin/python3 tests/test_rdp_monitor_ui.py
xvfb-run -a /usr/bin/python3 tests/test_rdp_rendering.py
xvfb-run -a /usr/bin/python3 tests/test_connection_diagnostics_ui.py
xvfb-run -a /usr/bin/python3 tests/test_home_session_ui.py
xvfb-run -a /usr/bin/python3 tests/test_routing_ui.py
xvfb-run -a /usr/bin/python3 tests/test_remote_files.py
xvfb-run -a /usr/bin/python3 tests/test_ephemeral_lab.py
xvfb-run -a /usr/bin/python3 tests/test_external_rdp_cli.py
xvfb-run -a /usr/bin/python3 tests/test_ui_behaviours.py
xvfb-run -a /usr/bin/python3 tests/test_vault_ui.py
xvfb-run -a /usr/bin/python3 tests/test_revisione.py
xvfb-run -a /usr/bin/python3 tests/test_network_tools.py
xvfb-run -a /usr/bin/python3 tests/test_i18n.py
/usr/bin/python3 tests/test_distro.py

%files
%license LICENSE LICENSES/Apache-2.0.txt
%doc README.md docs/GUIDA.md TEST_REPORT.md
%{_bindir}/fedtermhub
%{_libexecdir}/fedtermhub/fedtermrdp-native
%{_datadir}/fedtermhub
%{_datadir}/applications/io.github.crash_fd01.fedtermhub.desktop
%{_metainfodir}/io.github.crash_fd01.fedtermhub.metainfo.xml
%{_datadir}/icons/hicolor/*/apps/io.github.crash_fd01.fedtermhub.png

%changelog
* Fri Oct 30 2026 Crash-FD01 - 1:0.9.12-1
- Altre distribuzioni: pacchetti e repository firmati per Debian 12+, Ubuntu 22.04+, Linux Mint 21+ e derivate (APT), RHEL/AlmaLinux/Rocky 9+ (dnf) e openSUSE Tumbleweed e Leap 16 (zypper), x86_64 e aarch64; Fedora resta con il suo RPM, ora anche per aarch64. setup.sh riconosce la distribuzione e usa apt, dnf, rpm-ostree o zypper
- Fuori da Fedora il motore RDP e' nel pacchetto fedtermhub-rdp con la sua copia di FreeRDP 3.32.2 (solo client, compilata da packaging/build-freerdp.sh con SHA-256 fissato) e di libfuse3, trovate via RPATH in /usr/libexec/fedtermhub/lib: desktop remoto uguale ovunque, anche dove FreeRDP 3 non c'e' (Ubuntu 22.04, Mint 21, RHEL 9). Costruito su AlmaLinux 9 (glibc 2.34)
- fedtermhub/distro.py: famiglia della distribuzione da /etc/os-release; i messaggi «Installa: …» mostrano il comando giusto (apt, dnf, zypper, pacman, rpm-ostree) e il nome del pacchetto di quella distribuzione
- Strumenti di rete: nomi dei servizi (ssh/sshd, tftpd-hpa…), cartella TFTP (/srv/tftp su Debian e Ubuntu), firewall con firewalld o ufw; il server Telnet e' disattivato dove non esiste come servizio. Senza python3-defusedxml (RHEL 9) l'output di nmap si legge comunque, rifiutando DTD ed entita'
- install.sh: dipendenze con dnf, apt, zypper o pacman (Arch e derivate si installano da qui); il motore RDP si compila contro la FreeRDP 3 del sistema o, se manca, contro una copia compilata al momento
- Test: tests/test_distro.py (riconoscimento, pacchetti, servizi, firewall). La CI costruisce i pacchetti universali e li installa su Debian 12/13, Ubuntu 22.04/24.04, AlmaLinux 9/10, Rocky 9, Tumbleweed e Leap 16 (x86_64 e aarch64): doctor, finestra sotto Xvfb, motore RDP collegato a un xrdp locale (tests/rdp_engine_probe.py) e alcuni test dei sorgenti con il GTK di ogni distribuzione

* Fri Oct 30 2026 Crash-FD01 - 1:0.9.11-1
- Lingua inglese: tutta l'interfaccia e' tradotta (finestre, menu, messaggi, diagnostica, console seriale, output del doctor). Con "Automatica" FedTermHub segue la lingua del sistema: italiano per it_*, inglese per il resto; si sceglie a mano in Impostazioni > Interfaccia > Lingua e vale dal riavvio. FEDTERMHUB_LANGUAGE=it|en la forza per una sola esecuzione
- Traduzioni in fedtermhub/locale/en.po (formato gettext, si modifica con Poedit); tools/i18n_update.py aggiorna il catalogo quando cambiano i testi
- Testi con singolare e plurale scritti per intero invece di attaccare le desinenze ("element{o,i}"), che non si potevano tradurre
- I pulsanti della barra delle sessioni sono riconosciuti da una chiave fissa e non dall'etichetta, che ora cambia con la lingua
- Il file .desktop ha nome e descrizione in inglese, con la versione italiana per i sistemi in italiano
- Test: tests/test_i18n.py controlla il catalogo (aggiornato, stessi segnaposto {nome} e stessi mnemonici dell'italiano), la scelta della lingua e l'avvio vero in inglese; tests/test_theme_parity.py cerca i mnemonici in conflitto anche nelle etichette inglesi; gli altri test fissano l'italiano qualunque sia la lingua del sistema

* Fri Oct 30 2026 Crash-FD01 - 1:0.9.10-1
- Password Manager: cliccare su una credenziale apriva subito la modifica (Gtk.ListBox attiva la riga con un clic singolo). Ora la seleziona e basta; si modifica con il pulsante Modifica
- Telnet: nuovo tipo di sessione (Nuova Telnet), connessione rapida telnet:host[:porta], voce Connessione Telnet nel menu Rete, diagnostica di connessione; usa il client telnet di Fedora (ora tra i Recommends) con "--" prima dell'host
- Cattura pacchetti: dopo ogni cattura FedTermHub restava al 100% di CPU (una callback idle che si riproponeva all'infinito). Fermare la cattura non chiede piu' di nuovo la password (tcpdump gira gia' come utente con -Z) e ogni cattura usa un file nuovo invece di sovrascrivere la precedente
- Server locali: lo stato dei servizi (rpm -q e systemctl per cinque servizi) veniva letto ogni 4 secondi nel thread dell'interfaccia, che si bloccava a ogni giro; ora in un thread separato
- Condivisioni FTP: applicarle falliva sempre per un utente normale (vsftpd.conf su Fedora e' leggibile solo da root) e chiedeva la password per ogni comando, una decina di volte con tre cartelle. Ora un unico script con una sola autorizzazione; lo stesso per NFS, firewall dei servizi e firewall VNC
- Server HTTP: una porta gia' occupata veniva mostrata come server attivo; ora l'avvio e' verificato. Propone la cartella Pubblici invece dell'intera home, che con "Rete locale" diventava visibile a tutta la rete
- Port scanner: "Crea sessione dal servizio" usava sempre la porta predefinita (un SSH sulla 2222 diventava una sessione sulla 22) e trasformava in SSH qualunque servizio sconosciuto, HTTP compreso; ora porta trovata e solo SSH, RDP, VNC o Telnet. Una destinazione che comincia con "-" non viene passata a nmap
- Porte in ascolto: la colonna Processo mostrava il testo grezzo di ss; ora per esempio "sshd (pid 812)"
- Wake-on-LAN: propone i broadcast delle reti collegate, oltre a 255.255.255.255
- Strumenti rapidi: girano con bash anche se la shell dell'utente e' zsh o fish (i comandi usano read -rp e /dev/tcp)
- Diagnostica di connessione: per VNC con display 1 provava la porta TCP 1 invece della 5901
- Test: tests/test_network_tools.py prova gli strumenti dal vivo sotto Xvfb (nmap e tcpdump quando presenti)

* Fri Oct 30 2026 Crash-FD01 - 1:0.9.09-1
- Sicurezza: un file di sessioni importato con un host o un utente che comincia con "-" (per esempio -oProxyCommand=...) faceva eseguire un comando locale alla connessione. Ora ssh, sftp, tunnel e file remoti ricevono "--" prima della destinazione e le sessioni con dati non validi vengono rifiutate prima di connettersi
- VNC: nel campo Display/porta il valore 5901 (suggerito dal campo stesso) veniva passato come display e TigerVNC si collegava alla porta 11801. Ora un numero da 100 in su e' una porta (host::porta), uno piu' piccolo un display
- Connessione rapida: IPv6 tra parentesi quadre ([fe80::1]:22) e rdp:host:porta / vnc:host:porta; prima la porta finiva nell'host. Dal menu, con il campo vuoto, porta al campo invece di non fare niente
- Terminale: una shell locale uscita con "exit" lasciava la scheda aperta e morta; gli strumenti rapidi dicevano "Invio per chiudere" senza chiudere. Ora la scheda si chiude quando il programma termina; SSH, SFTP e seriale si chiudono con un'uscita normale e restano aperte con il pulsante di riconnessione se la connessione cade; le sessioni Comando restano aperte per leggere l'output. Ctrl+Shift+W sull'ultimo riquadro chiude la scheda
- Schede: un solo percorso di chiusura (pulsante, Ctrl+W, chiusura automatica); prima erano quattro diversi e uno lasciava la scheda chiusa nell'elenco di MultiExec
- MultiExec: mostra le schede aperte e invia solo a quelle scelte (le sessioni SSH sono gia' spuntate); prima inviava a tutte, compresi strumenti, journal e console seriali
- Sessioni: l'albero ricorda le cartelle aperte; prima ogni modifica, aggiunta o eliminazione le richiudeva tutte tranne Home Session. Il duplicato compare sotto l'originale ed e' selezionato. Le tre strade per creare una sessione si comportano allo stesso modo
- Sessioni: la finestra non salva una sessione senza host, con porta non numerica o con un comando vuoto; resta aperta e dice cosa correggere
- Sessioni: un file sessioni illeggibile veniva sostituito dagli esempi senza avvisare e la copia di sicurezza poteva essere sovrascritta da quella successiva. Ora la copia ha la data nel nome e all'avvio compare un avviso con il percorso
- Sessioni: l'eliminazione di una cartella conta sessioni e sottocartelle di tutti i livelli; "Apri tutte le sessioni" chiede conferma sopra le cinque sessioni; nel menu contestuale "Apri" diventa "Connetti" per le sessioni ed "Espandi o comprimi" per le cartelle
- Menu Sessioni: quattro voci di import/export al posto di sei quasi doppie; l'importazione legge anche il formato vecchio
- Password Manager: la password salvata modificando una sessione prende il gruppo della sua cartella; "Blocca password" non dice piu' "bloccato" quando non c'era niente da bloccare; "vault" sostituito da "Password Manager" in tutti i testi
- File remoti: Aggiorna ricarica la cartella corrente (prima, con una cartella selezionata, ci entrava); dopo un caricamento la vista resta dove era
- Seriale: un campo "comando" arrivato da un file importato faceva uscire subito il ponte seriale
- Impostazioni aperte una sola volta; finestra Scorciatoie completa (mancavano Ctrl+K, Ctrl+H e il terminale); menu Aiuto con la Guida
- Test: tests/test_revisione.py copre tutte queste correzioni con GTK reale sotto Xvfb

* Thu Oct 29 2026 Crash-FD01 - 1:0.9.08-1
- Password Manager ridisegnato a due pannelli: elenco a sinistra con ricerca, dettaglio della credenziale a destra con i pulsanti Copia accanto a ogni dato, password nascosta finche' non la si chiede e di nuovo nascosta cambiando credenziale. Prima era una tabella con le azioni divise fra la barra in alto e quella in basso, e Esporta/Importa/Blocca avevano lo stesso peso di Copia password
- Password Manager: credenziali raggruppate (di solito per cliente) e ordinate per gruppo e nome, "Senza gruppo" in fondo; la ricerca trova anche il gruppo e accetta piu' parole. Per le credenziali senza gruppo propone il nome della cartella delle sessioni che le usano, con un clic. La password RDP salvata da una sessione prende il gruppo della sua cartella
- Password Manager: per ogni credenziale si vede quali sessioni e tunnel la usano; l'eliminazione avvisa quali sessioni torneranno a chiedere la password
- Password Manager: pagina di benvenuto quando l'archivio e' vuoto, che spiega a cosa serve e come collegare una credenziale a una sessione
- Password Manager: scorciatoie Ctrl+F (cerca), Ctrl+N (nuova), Ctrl+C (copia password), Ctrl+B (copia utente), Canc (elimina), Invio (modifica); nei campi di testo Ctrl+C copia il testo come sempre
- Password Manager: cambio della master password dal menu, con verifica della password attuale
- Credenziale: campi larghi, gruppo con suggerimenti, occhio per mostrare la password, generatore (20 caratteri senza caratteri ambigui, tutti i tipi richiesti da Active Directory), barra di robustezza, note su piu' righe, Salva disattivo senza nome, avviso di cambiare la password anche sul server quando la si modifica
- Master password: una password sbagliata non chiude piu' la finestra lasciando solo un messaggio nella barra di stato; l'errore compare nella finestra e si riprova. Minimo 8 caratteri alla creazione, con barra di robustezza. La spunta "non chiedere piu'" ora vale anche alla creazione (prima era mostrata ma ignorata)
- Backup cifrati: testi propri invece di quelli della master password, finestre aperte sopra il Password Manager e non dietro, password del backup sbagliata segnalata nella finestra con nuovo tentativo
- Le tendine Credenziale delle sessioni seguono lo stesso ordine e mostrano il gruppo accanto al nome
- Test: tests/test_vault_ui.py prova la finestra con GTK reale sotto Xvfb; tests/test_versions.py controlla che la versione sia la stessa in spec, programma, install.sh e metainfo
- Verifica installazione: un workflow installa ogni release dal sito su Fedora 43 e 44 puliti e ne controlla firme, avvio e rimozione

* Thu Oct 29 2026 Crash-FD01 - 1:0.9.07-1
- Tastiera: con il fuoco nel terminale Ctrl+lettera arriva al terminale. In GTK3 il gestore della finestra viene chiamato prima del widget con il fuoco: Ctrl+W chiudeva la scheda invece di cancellare una parola o cercare in nano, Ctrl+K/L/T/B (B e' il prefisso di tmux) non arrivavano mai al terminale e Ctrl+Shift+V apriva il gestore password invece di incollare. Verificato con tasti veri inviati con xdotool sotto Xvfb
- Interfaccia: pulsanti dei dialoghi in italiano al posto di Gtk.STOCK_* (deprecati, e in inglese su un sistema in inglese); azione principale evidenziata in tutti i dialoghi con contrasto sopra 4.5:1; stato premuto visibile per gli interruttori della barra dei file remoti
- File remoti: il numero di elementi esclusi dai filtri ora compare anche per la cartella principale, non solo per le sottocartelle
- Strumenti di rete: la finestra non si apriva senza il comando rpm (avvio dai sorgenti su una distribuzione non RPM)
- Test: nuovo tests/test_ui_behaviours.py sull'applicazione vera sotto Xvfb (tastiera nel terminale, posizione delle sessioni nuove, filtri dei file remoti); tools/screenshots.py rigenera le schermate con dati inventati
- Distribuzione: repository dnf firmato e sito su GitHub Pages pubblicati dal workflow Rilascio, installazione con un comando (setup.sh); README e guida completa in docs/GUIDA.md; modelli per issue e pull request; build su Fedora 43 e 44
- Interfaccia: il tema chiaro non stilizzava 13 selettori presenti solo nel tema scuro. Il caso piu' grave era il pallino di stato della connessione nella barra schede (.is-connected/.is-disconnected): senza colore definito prendeva quello del testo e smetteva di indicare lo stato. Aggiunte anche .tab-label, .tab-close, .tab-reconnect, .diagnostics-listbox, .session-scroll, .tunnel-explain, .tunnel-editor e gli stati :active di ribbon e rail
- Interfaccia: il tema scuro non stilizzava .session-error-tab, che ricadeva sul bianco di GTK dentro un'interfaccia scura
- Nuovo test tests/test_theme_parity.py: fa fallire la build quando una classe e' stilizzata in un tema solo, quando un colore scende sotto 4.5:1 di contrasto sullo sfondo reale, quando due mnemonici collidono nella stessa finestra o quando un pulsante solo-icona resta senza tooltip
- Sessioni: una sessione nuova non viene piu' creata dentro la cartella selezionata in quel momento, destinazione che cambiava a seconda di dove si trovava il cursore. Ora viene aggiunta in fondo all'elenco, fuori da ogni cartella, e viene selezionata e portata in vista perche' si veda dov'e'. "Nuova cartella" e "Importa qui" continuano a rispettare la selezione
- File remoti: i file nascosti sono esclusi per impostazione predefinita, con interruttore nella barra e scorciatoia Ctrl+H. Su una home reale i dotfile erano la maggioranza delle voci
- File remoti: nuovo campo di filtro per nome sulla cartella aperta; le cartelle restano visibili per poterci entrare
- File remoti: il conteggio nella barra di stato dichiara quanti elementi sono esclusi dai filtri, per non far sembrare vuota una cartella che non lo e'
- Accessibilita': 21 pulsanti convertiti a mnemonico (Alt+lettera); prima non ce n'era nessuno in tutta l'applicazione. Aggiunti i due tooltip mancanti sui pulsanti solo-icona
- Test: corrette due asserzioni obsolete in tests/test_core.py che verificavano ancora la versione 2 del protocollo clipboard, passato a 3 nella serie 0.9.05. La suite falliva da allora
- Test: tests/test_external_rdp_cli.py era presente ma non veniva mai eseguito in %%check
- Licenza: il progetto passa a GPL-3.0-or-later, come Helpix. Aggiunte le intestazioni SPDX a tutti i sorgenti e il file REUSE.toml; l'helper nativo, prima Apache-2.0, e' riallineato (FreeRDP resta Apache-2.0, compatibile con GPLv3)
- Packaging: rimosso debug_package %%{nil}; il pacchetto debuginfo e' richiesto per ogni binario ELF
- Packaging: il Makefile dell'helper nativo aggiunge -z relro -z now ai flag che riceve e tiene gli avvisi separati dai CFLAGS. Le build RPM ricevevano gia' i flag di hardening di Fedora, perche' da Fedora 36 %%set_build_flags viene eseguito in automatico in %%build; ne beneficia la compilazione manuale con install.sh, che non li aveva
- Packaging: aggiunti URL e Source0 completo; Epoch documentato
- Packaging: app ID io.github.crash_fd01.fedtermhub al posto di it.local.FedTermHub, che non era un dominio controllato; icone e desktop file rinominati di conseguenza
- Packaging: nuovo file AppStream, installato e validato in %%check
- Packaging: nmap, tcpdump, tigervnc, python3-winrm, python3-pyserial e sshpass passano da Requires a Recommends: sono funzioni opzionali che a runtime degradano con un messaggio esplicito
- Packaging: shebang /usr/bin/python3 solo sull'entry point e /usr/bin/bash nel launcher, niente /usr/bin/env; i moduli importati non sono piu' eseguibili. Rimossa l'installazione in pixmaps, percorso deprecato
- Packaging: Summary e %%description in inglese, come richiesto per i metadati dei pacchetti Fedora; rimossi i Requires espliciti su freerdp-libs e libwinpr, che rpm ricava dal binario; LICENSE non piu' duplicato in %%license
- Distribuzione: CI GitHub (sintassi, test, REUSE, build RPM in container Fedora con verifica di BIND_NOW), workflow di release sul tag, Makefile per Copr

* Fri Sep 11 2026 Crash-FD01 - 1:0.9.06-1
- File remoti: l'albero segue ora la directory corrente della shell. Il meccanismo e' OSC 7, la sequenza con cui la shell dichiara al terminale dove si trova: osservare i comandi digitati non funzionerebbe, perche' un cd puo' stare dentro script, alias, funzioni o pushd
- File remoti: sulle sessioni SSH il supporto OSC 7 viene iniettato nella shell remota, perche' /etc/profile.d/vte.sh non e' quasi mai presente sui server e VTE_VERSION non attraversa sshd. La riga preserva un PROMPT_COMMAND gia' impostato dall'utente, non si duplica alla riconnessione, inizia con uno spazio per restare fuori dalla cronologia (HISTCONTROL=ignorespace) e non produce errori su dash/busybox
- File remoti: l'aggiornamento e' accettato solo dal riquadro attivo del tab in primo piano, cosi' un comando in background o un altro tab non spostano l'albero sotto le mani dell'utente
- File remoti: nuovo pulsante nella barra della sidebar per disattivare l'inseguimento; la scelta viene salvata nelle impostazioni (follow_remote_directory)
- L'iniezione e' limitata al protocollo ssh: su seriale, RDP o comandi arbitrari finirebbe nel posto sbagliato

* Fri Sep 04 2026 Crash-FD01 - 1:0.9.05.8-1
- Terminale integrato: corretta una regressione introdotta nella 0.9.07. VTE_VERSION era stata inserita nella blocklist dell'ambiente insieme a LINES/COLUMNS, ma /etc/profile.d/vte.sh la usa come interruttore ([ ${VTE_VERSION:-0} -ge 3405 ]) per abilitare OSC 7, cioe' il meccanismo con cui la shell comunica al terminale la directory corrente. Ora viene impostata con la versione reale di VTE invece che rimossa

* Fri Aug 28 2026 Crash-FD01 - 1:0.9.05.7-1
- Terminale integrato: nano (e le applicazioni ncurses in genere) mostravano una schermata vuota. L'ambiente del processo figlio veniva ereditato integralmente: LINES e COLUMNS stantie sovrascrivono l'ioctl TIOCGWINSZ perche' ncurses le preferisce, e TERM era impostato con setdefault quindi un valore ereditato (tmux, altro terminale) vinceva. Ora TERM e' imposto e LINES/COLUMNS/TERMCAP/WINDOWID/VTE_VERSION sono rimosse
- Terminale integrato: la winsize del PTY viene riallineata alla griglia VTE dopo lo spawn e a ogni ridimensionamento del riquadro, con coalescenza e senza SIGWINCH inutili. Il processo figlio parte dal costruttore, prima che il widget sia allocato: senza riallineamento restava con la geometria di ripiego
- Terminale integrato: FEDTERMHUB_TERM permette di forzare un TERM diverso (utile sugli apparati di rete con database terminfo ridotto, dove xterm-256color non esiste)
- Terminale integrato: nuova voce di menu contestuale "Diagnostica terminale" che confronta griglia VTE, stty size e tput
- Terminale integrato: corretti i colori 0 e 8 della palette, illeggibili sullo sfondo (contrasto 1.19:1 e 3.93:1, ora 3.11:1 e 5.54:1)

* Fri Aug 28 2026 Crash-FD01 - 1:0.9.05.6-1
- RDP tastiera: risolta l'inversione di Bloc Num (e Bloc Maiusc) passando dalla sessione remota a quella locale. Il client dichiarava sempre "blocchi spenti" al server a ogni ingresso nella scheda: il campo toggleStates del Client Synchronize Event era fisso a 0. Ora viene letto lo stato reale della tastiera locale (Gdk.Keymap) e inviato come maschera KBD_SYNC_NUM_LOCK/CAPS_LOCK/SCROLL_LOCK
- RDP tastiera: lo stato dei blocchi viene ridichiarato anche dopo ogni riconnessione, perche' la sessione remota riparte con gli indicatori azzerati
- Lab temporaneo: la console grafica non si apriva ("Display can only be attached through libvirt with --attach"). Le VM del Lab usano --graphics spice,listen=none, cioe' un display senza alcun listener, raggiungibile solo via virDomainOpenGraphicsFD: virt-viewer lo supporta esclusivamente con --attach, che mancava. listen=none e' stato mantenuto perche' non espone nulla ne' in rete ne' su filesystem
- Lab temporaneo: quando la console non si apre, la VM non resta piu' attiva e invisibile. Prima virt-viewer restava vivo con il proprio dialogo di errore, il guardiano non tornava mai e ogni nuovo tentativo accumulava una macchina in piu'
- Lab temporaneo: il fallimento della console e' riconosciuto da un codice di uscita positivo con cleanup non ancora avviato, non dal tempo trascorso: una terminazione da segnale (pulsante "Termina ed elimina") non viene piu' scambiata per un errore
- Lab temporaneo: lo stderr di virt-viewer viene letto invece che scartato e il motivo esatto del fallimento viene mostrato all'utente
- install.sh: aggiunti wl-clipboard e xclip, necessari per incollare su Linux i file copiati in una sessione RDP (erano solo tra i Recommends dell'RPM)

* Mon Aug 03 2026 Crash-FD01 - 1:0.9.05.4-1
- RDP clipboard Windows -> Linux: abbandonato Gtk.Clipboard.set_with_data, che da PyGObject non funziona (le callback che servono i dati non attraversano l'introspezione, quindi il client non diventa mai proprietario della selezione e l'incolla restituisce il contenuto precedente degli appunti)
- RDP clipboard Windows -> Linux: la lista file viene ora pubblicata da wl-copy (Wayland) o xclip (X11), che si biforcano e restano proprietari della selezione servendo il tipo x-special/gnome-copied-files richiesto da Nautilus. Meccanismo verificato leggendo davvero il contenuto dagli appunti
- RDP clipboard Windows -> Linux: se nessuno dei due strumenti e' installato, il messaggio indica esattamente cosa installare (dnf install wl-clipboard) invece di un generico errore
- Aggiunti wl-clipboard e xclip tra i Recommends del pacchetto

* Mon Aug 03 2026 Crash-FD01 - 1:0.9.05.3-1
- RDP clipboard Windows -> Linux: le callback degli appunti "pigri" di GTK ora restano referenziate sull'istanza. Erano funzioni locali: una volta usciti dal metodo il garbage collector poteva liberarle e l'incolla restituiva il contenuto precedente degli appunti invece dei file
- RDP clipboard Windows -> Linux: l'esito di set_with_data viene finalmente controllato. Prima veniva ignorato e il log dichiarava il successo anche quando GTK rifiutava la proprieta' della selezione
- RDP clipboard Windows -> Linux: ripiego esplicito che mette i percorsi come testo e lo dichiara, invece di lasciar credere che i file siano negli appunti; il log riporta i percorsi effettivi per capire se provengono dal mount FUSE

* Mon Aug 03 2026 Crash-FD01 - 1:0.9.05.2-1
- RDP clipboard Windows -> Linux: corretto l'id di formato usato nella richiesta al server. FileGroupDescriptorW e' un formato a nome lungo registrato dinamicamente e l'id del server NON coincide con quello locale di winpr: veniva richiesto l'id locale, quindi il server non restituiva mai la lista file. Ora l'id annunciato dal server viene riconosciuto per nome e conservato
- RDP clipboard Windows -> Linux: aggiunto il passaggio ClipboardSetData mancante prima di ClipboardGetData. Senza depositare i byte grezzi di FileGroupDescriptorW nel clipboard locale, la conversione verso text/uri-list non aveva nulla da convertire e restituiva NULL
- RDP clipboard: diagnostica di connessione che indica sempre se la clipboard file e' attiva in entrambe le direzioni o solo verso Windows, e in quale punto della catena si ferma

* Mon Aug 03 2026 Crash-FD01 - 1:0.9.05.1-1
- RDP clipboard: NUOVA direzione Windows -> Linux per i file. Copiando file dentro la sessione remota, l'helper riconosce FileGroupDescriptorW, li mappa via CliprdrFileContext (FUSE) e Python li pubblica sugli appunti GTK con i target text/uri-list e x-special/gnome-copied-files, cosi' si incollano nel file manager di Linux
- RDP clipboard: protocollo mailbox portato a versione 3 con il campo remote_kind (testo o lista file); offset verificati tra helper C e lato Python
- RDP clipboard: diagnostica esplicita nel log quando FreeRDP e' privo di supporto FUSE (l'incolla file da Windows non e' possibile) o quando il server rifiuta il trasferimento file

* Mon Aug 03 2026 Crash-FD01 - 1:0.9.05-1
- RDP drag&drop: il bersaglio di rilascio sul canvas viene ora registrato SEMPRE (prima dipendeva dalla casella Clipboard: senza, il rilascio non produceva alcun effetto ne' alcun messaggio)
- RDP drag&drop: i file trascinati vengono copiati nella "Cartella condivisa" /drive: quando configurata - via immediata e affidabile, indipendente dalle capability cliprdr del server; niente sovrascritture (suffisso progressivo)
- RDP drag&drop: riscontro visibile a schermo dopo ogni rilascio (esito, destinazione o motivo del rifiuto) al posto del solo log nascosto
- RDP drag&drop: fallback clipboard cliprdr conservato, con istruzioni esplicite (Ctrl+V) e diagnostica quando il server rifiuta il trasferimento file
- RDP drag&drop: decodifica di riserva del payload text/uri-list grezzo quando get_uris() e' vuoto (alcuni setup Wayland)
- Drag&drop file SFTP: risolto il mancato rilascio dovuto a DestDefaults.ALL; ora la sequenza drag-motion/drag-drop/data-received e' gestita manualmente con evidenziazione della cartella di destinazione e fallback text/uri-list per Wayland
- Drag&drop sul canvas RDP: passato a DestDefaults.DROP|MOTION per eliminare il doppio drag_finish
- Barra RDP a schermo intero: trascinamento libero in qualunque punto (maniglia dedicata, doppio clic per riportarla ancorata in alto al centro) al posto delle sole tre posizioni fisse
- Network scanner: opzione ARP con privilegi via pkexec per popolare MAC e produttore e rilevare host che ignorano il ping
- Port scanner: retry automatico con -Pn quando l'host non risponde al ping e messaggi di stato piu' chiari

* Mon Aug 03 2026 Crash-FD01 - 1:0.9.04-1
- UI: le finestre indipendenti (RDP sganciata e pannello Lab temporaneo) hanno un Gtk.WindowGroup dedicato: i dialoghi modali della finestra principale (es. Nuova sessione) non le bloccano piu'
- RDP: campo "Cartella condivisa" con pulsante Sfoglia e validazione del percorso (deve esistere, niente virgole o fine riga); la cartella appare in Explorer come unita' di rete fedtermhub (/drive:)
- RDP: drag&drop di file dal file manager sul canvas della sessione: la lista viaggia sul canale cliprdr come FileGroupDescriptorW e i byte vengono serviti on-demand via CB_FILECONTENTS (CliprdrFileContext di FreeRDP 3); dopo il drop si incolla con Ctrl+V dentro Windows
- RDP: protocollo mailbox clipboard v2 (campo host_kind: testo o uri-list) e negoziazione capability file con il server

* Thu Jul 23 2026 Crash-FD01 - 1:0.9.03-1
- RPM: aggiunto Epoch 1 per consentire l'aggiornamento dalle vecchie versioni 3.x (prima dnf rifiutava il downgrade e i fix non venivano mai installati)
- RDP: la barra in schermo intero ora verifica la posizione fisica del puntatore prima di nascondersi (immune ai problemi degli eventi enter/leave su Wayland)
- RDP: i frame completamente neri pubblicati dal server durante il cambio risoluzione vengono saltati mantenendo l'ultimo frame valido
- Vault: la spunta "Non richiedere piu' finche' l'app resta aperta" ora e' direttamente nel prompt di sblocco all'avvio

* Thu Jul 23 2026 Crash-FD01 - 0.9.02-1
- RDP: eliminato il flash nero durante le riallocazioni del canvas (background pattern azzerato, blit SOURCE limitato al rettangolo del frame)
- RDP: la barra a scomparsa in schermo intero resta visibile finche' il mouse ci e' sopra (gestione corretta degli eventi di attraversamento sui pulsanti figli)

* Wed Jul 22 2026 Crash-FD01 - 0.9.01-1
- Prima release pubblica (beta)
