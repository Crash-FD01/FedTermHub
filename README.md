# FedTermHub

**Versione 0.9.03 — BETA**

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

### Da RPM (consigliato)

# Sicurezza

Le password non compaiono mai nella riga di comando dei processi (passaggio
via file descriptor su memoria anonima sigillata). I file di configurazione
vengono scritti in modo atomico con permessi 0600. Il vault usa cifratura
autenticata e nessun dato lascia la macchina.

## Licenza e autore

Distribuito con licenza **MIT** (vedi `LICENSE`).

Autore: **Crash-FD01** — progetto indipendente.

Il componente nativo RDP si collega a FreeRDP (licenza Apache 2.0); la licenza
completa è inclusa in `LICENSES/Apache-2.0.txt`.
