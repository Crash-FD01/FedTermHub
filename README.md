# FedTermHub

[![CI](https://github.com/Crash-FD01/fedtermhub/actions/workflows/ci.yml/badge.svg)](https://github.com/Crash-FD01/fedtermhub/actions/workflows/ci.yml)
[![Copr](https://copr.fedorainfracloud.org/coprs/crash-fd01/fedtermhub/package/fedtermhub/status_image/last_build.png)](https://copr.fedorainfracloud.org/coprs/crash-fd01/fedtermhub/)
[![Licenza: GPL v3](https://img.shields.io/badge/licenza-GPLv3-blue.svg)](LICENSE)

**Versione 0.9.07 — BETA**

FedTermHub è un gestore di terminali e connessioni remote per Linux (Fedora),
sviluppato come progetto indipendente. Riunisce in un'unica applicazione
desktop GTK i terminali locali, le sessioni SSH, il desktop remoto RDP, il
trasferimento file e un vault cifrato per le credenziali.

> **Stato del progetto: BETA.** Il programma è funzionante e usato
> quotidianamente, ma è in sviluppo attivo: possono esserci bug, comportamenti
> incompleti e cambiamenti tra una versione e l'altra. Segnala i problemi
> aprendo una issue.

## Funzionalità principali

**Terminale multi-riquadro** — riquadri divisibili con layout multipli
(stack, tall, fat, grid) commutabili al volo, ricerca nello scrollback,
apertura degli URL da tastiera (hints), invio dell'input a più riquadri
contemporaneamente (broadcast), zoom del font, colori truecolor e supporto
alle ligature dei font da programmatore. Clipboard OSC 52 per copiare dalla
sessione SSH remota verso il sistema locale.

**Sessioni SSH** — rubrica di sessioni organizzabile in cartelle, connessione
rapida da command palette (Ctrl+K), riconnessione con un tasto, tunnel SSH
gestiti, MultiExec per lanciare comandi su più server insieme.

**Desktop remoto RDP** — motore nativo basato su libfreerdp con
ridimensionamento dinamico, clipboard bidirezionale (testo), barra di
controllo compatta e spostabile in schermo intero, riconnessione immediata
quando la sessione cade (per esempio dopo il blocco del PC), modalità legacy
xfreerdp come ripiego.

**File remoti** — sfoglia, carica e scarica file via SFTP e FTP con il client
OpenSSH di sistema, senza dipendenze aggiuntive.

**Vault credenziali** — password cifrate con AES-256-GCM e chiave derivata
con PBKDF2-HMAC-SHA256 (600.000 iterazioni). Blocco automatico configurabile
o sblocco persistente per l'intera sessione di lavoro.

**Strumenti di rete** — diagnostica connessioni, condivisioni di rete,
sessioni Windows remote, laboratori temporanei.

## Requisiti

- Fedora Linux (testato su Fedora 44) o distribuzione compatibile
- GTK 3, VTE (>= 0.76 per la clipboard OSC 52)
- Python 3 con PyGObject
- FreeRDP 3 (libfreerdp3) per il desktop remoto nativo
- Client OpenSSH per SSH/SFTP

## Installazione

### Da Copr (consigliato su Fedora)

```bash
sudo dnf copr enable crash-fd01/fedtermhub
sudo dnf install fedtermhub
```

Gli aggiornamenti arrivano poi con il normale `sudo dnf upgrade`.

### Dalle release GitHub

Ogni [release](https://github.com/Crash-FD01/fedtermhub/releases) contiene il
tarball dei sorgenti e gli RPM già costruiti per Fedora.

### Compilando dai sorgenti

```bash
tar -xzf fedtermhub-0.9.07.tar.gz
cd fedtermhub-0.9.07
bash COMPILA-RPM.sh
```

Lo script installa le dipendenze di build, compila il componente nativo RDP e
produce un RPM installabile. In alternativa: `bash install.sh` per
un'installazione diretta senza pacchetto.

### Disinstallazione

```bash
bash uninstall.sh
```

oppure `sudo dnf remove fedtermhub` se installato da RPM.

## Scorciatoie principali

| Tasti | Azione |
|---|---|
| `Ctrl+K` | Command palette: cerca e apri una sessione |
| `Ctrl+Shift+Invio` | Nuovo riquadro terminale |
| `Ctrl+Shift+O` / `Ctrl+Shift+E` | Dividi il riquadro (verticale / orizzontale) |
| `Ctrl+Shift+L` | Cambia layout dei riquadri |
| `Ctrl+Shift+[` / `Ctrl+Shift+]` | Riquadro precedente / successivo |
| `Ctrl+Shift+W` | Chiudi il riquadro |
| `Ctrl+Shift+P` | Broadcast input on/off |
| `Ctrl+Shift+H` | Hints: apri un URL visibile da tastiera |
| `Ctrl+Shift+F` | Cerca nello scrollback |
| `Ctrl+Shift+C` / `Ctrl+Shift+V` | Copia / Incolla |
| `Ctrl` `+`/`-`/`0` | Zoom del font |
| `F11` | Schermo intero |

## Sicurezza

Le password non compaiono mai nella riga di comando dei processi (passaggio
via file descriptor su memoria anonima sigillata). I file di configurazione
vengono scritti in modo atomico con permessi 0600. Il vault usa cifratura
autenticata e nessun dato lascia la macchina.

## Contribuire

Segnalazioni e pull request sono benvenute: vedi
[CONTRIBUTING.md](CONTRIBUTING.md). Per le vulnerabilità di sicurezza **non**
aprire una issue pubblica: vedi [SECURITY.md](SECURITY.md).

## Licenza e autore

FedTermHub è software libero, distribuito con licenza **GNU GPL versione 3 o
successiva** (vedi `LICENSE`). Puoi usarlo, studiarlo, modificarlo e
ridistribuirlo; le versioni modificate che distribuisci devono restare sotto la
stessa licenza e con i sorgenti disponibili.

Autore: **Crash** ([Crash-FD01](https://github.com/Crash-FD01)).

Il componente nativo RDP si collega a FreeRDP, distribuito con licenza
Apache 2.0, compatibile con la GPL versione 3; il testo è incluso in
`LICENSES/Apache-2.0.txt`. Il progetto segue la specifica
[REUSE](https://reuse.software): ogni file dichiara autore e licenza.
