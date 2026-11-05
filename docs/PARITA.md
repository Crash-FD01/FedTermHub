<!--
SPDX-FileCopyrightText: 2026 Crash
SPDX-License-Identifier: GPL-3.0-or-later
-->
# Linux e Windows: cosa c'è dove

FedTermHub ha due interfacce, ognuna fatta con quello che funziona meglio sul suo sistema:

- **Linux**: GTK 3, terminale VTE, desktop remoto con FreeRDP (`fedtermhub/fedtermhub.py`);
- **Windows**: Qt 6, terminale xterm.js con ConPTY, desktop remoto con il componente di Windows (`fedtermhub/qtui/`).

Sotto le due interfacce c'è un **nucleo comune**, uguale per entrambe: sessioni, cartelle, password cifrate,
impostazioni e formato di import/export (`model.py`), comandi ssh, ProxyJump e tunnel (`commands.py`),
strumenti di rete (`netcore.py`), riquadri del terminale e MultiExec (`termcore.py`), colori, icone e colori del
terminale (`look.py`). Le sessioni esportate da
una versione si importano nell'altra, e le due finestre hanno la stessa disposizione: menu, barra dei
comandi con le tendine, barra verticale, pannello Sessioni | File remoti, schede con il pallino di stato.

La tabella elenca le voci dei menu della versione Linux e le funzioni principali. **sì** = c'è, **no** = non
c'è ancora, **—** = non serve su quel sistema. `tests/test_commands.py` controlla che ogni voce dei menu
Linux sia nella tabella e che le voci segnate **sì** esistano davvero nell'interfaccia di quel sistema.

## Terminale

| Funzione | Linux | Windows | Note |
|---|---|---|---|
| Nuovo terminale locale | sì | sì | Windows: PowerShell (7 se c'è) o la shell scelta nelle Impostazioni |
| Terminale diviso | sì | sì | stessi tasti; Windows: anche in Terminale › Riquadri, perché il clic destro copia e incolla |
| MultiExec | sì | sì | |
| Quick connect | sì | sì | `utente@host[:porta]`, `rdp:host`, `telnet:host` |
| Esci | sì | sì | |

## Sessioni

| Funzione | Linux | Windows | Note |
|---|---|---|---|
| Nuova sessione | sì | sì | |
| Nuova SSH | sì | sì | password dal Password Manager scritta al prompt |
| Nuova RDP | sì | sì | Linux: FreeRDP incorporato; Windows: il componente Desktop remoto di Windows |
| Nuova VNC | sì | sì | Windows: noVNC dentro la scheda |
| Nuova Telnet | sì | sì | Windows: Telnet integrato, senza telnet.exe |
| Nuova seriale | sì | sì | Windows: porte COM |
| Nuova cartella | sì | sì | con il colore dell'icona |
| Modifica selezionata | sì | sì | |
| Duplica selezionata | sì | sì | |
| Sposta selezionata | sì | sì | anche trascinando nell'albero |
| Elimina selezionata | sì | sì | |
| Esporta tutte le sessioni | sì | sì | stesso formato sulle due versioni |
| Esporta la selezione | sì | sì | |
| Importa sessioni nella cartella selezionata | sì | sì | |
| Sostituisci tutte le sessioni con un file | sì | sì | |

## Rete

| Funzione | Linux | Windows | Note |
|---|---|---|---|
| Server locali | sì | sì | Windows: server HTTP e TFTP temporanei |
| FTP / SFTP share | sì | no | |
| Porte locali in ascolto | sì | sì | |
| Wake-on-LAN | sì | sì | |
| Network scanner | sì | sì | |
| Port scanner | sì | sì | |
| Cattura pacchetti | sì | — | usa tcpdump; su Windows serve Wireshark |
| Ping | sì | sì | |
| Traceroute | sì | sì | Windows: tracert |
| DNS lookup | sì | sì | Windows: Resolve-DnsName |
| Check porta TCP | sì | sì | |
| Connessione Telnet | sì | sì | |
| Route | sì | sì | Windows: route print |
| Socket aperti | sì | sì | Windows: netstat -ano |

## Strumenti

| Funzione | Linux | Windows | Note |
|---|---|---|---|
| Tunneling SSH | sì | sì | locale, remoto e dinamico (SOCKS), avvio automatico; stessa finestra sulle due versioni |
| Password Manager | sì | sì | stesso archivio cifrato (AES-GCM) |
| Esporta backup cifrato delle password | sì | sì | |
| Importa backup cifrato delle password | sì | sì | |
| Blocca password | sì | sì | anche da solo dopo i minuti di inattività scelti |
| Chiavi SSH | sì | sì | |
| Known hosts | sì | sì | |
| Journal | sì | — | il registro di systemd esiste solo su Linux |
| Info sistema | sì | sì | |
| Log applicazione | sì | sì | |
| Diagnostica compatibilità | sì | — | controlla desktop, librerie e programmi della distribuzione |

## Vista

| Funzione | Linux | Windows | Note |
|---|---|---|---|
| Mostra/nascondi menu | sì | sì | |
| Mostra/nascondi barra comandi | sì | sì | |
| Mostra/nascondi pannello sessioni | sì | sì | |
| Mostra/nascondi barra verticale | sì | sì | |
| Mostra/nascondi barra di stato | sì | sì | |
| Espandi cartelle | sì | sì | |
| Comprimi cartelle | sì | sì | |

## Impostazioni e Aiuto

| Funzione | Linux | Windows | Note |
|---|---|---|---|
| Apri impostazioni | sì | sì | |
| Esporta impostazioni | sì | sì | stesso file sulle due versioni |
| Importa impostazioni | sì | sì | |
| Guida | sì | sì | |
| Scorciatoie da tastiera | sì | sì | |
| Informazioni | sì | sì | |

## Altro

| Funzione | Linux | Windows | Note |
|---|---|---|---|
| File remoti | sì | sì | scheda File remoti del pannello laterale, segue la cartella della shell |
| Tema chiaro e scuro | sì | sì | pulsante a destra nella barra dei comandi |
| Lab | sì | — | macchine virtuali usa e getta con libvirt |
| Cerca aggiornamenti | — | sì | su Linux aggiorna il gestore dei pacchetti |
| Aggiornamento dal programma | — | sì | scarica il Setup.exe, controlla l'impronta SHA-256 e riapre FedTermHub |
