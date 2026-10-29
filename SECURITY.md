<!-- SPDX-FileCopyrightText: 2026 Crash
     SPDX-License-Identifier: GPL-3.0-or-later -->
# Sicurezza / Security

## Segnalare una vulnerabilità

**Non aprire una issue pubblica.** Usa la segnalazione privata di GitHub:
[Security › Report a vulnerability](https://github.com/Crash-FD01/fedtermhub/security/advisories/new).

Indica la versione (`rpm -q fedtermhub`), come l'hai installato e i passi per
riprodurre il problema. Ricevi una prima risposta entro una settimana; la
correzione esce con una nuova versione e, quando serve, un avviso di sicurezza
che riconosce il tuo contributo (se lo desideri).

*Please report vulnerabilities privately through GitHub's "Report a
vulnerability" button, not in a public issue. Italian or English are both
fine.*

## Versioni supportate

Solo l'ultima versione pubblicata riceve correzioni di sicurezza. Il progetto è
in beta: aggiorna sempre prima di segnalare.

## Cosa rientra

FedTermHub maneggia credenziali e apre connessioni verso sistemi di terzi, quindi
sono particolarmente rilevanti:

- il **vault delle credenziali** (`~/.config/fedtermhub/vault.json`): cifratura,
  derivazione della chiave, gestione del blocco;
- l'**esposizione di password** nella riga di comando dei processi, nei log,
  nei file temporanei o negli appunti;
- l'**iniezione di opzioni** nei comandi lanciati (`ssh`, `xfreerdp`, `nmap`,
  `virt-install`) a partire da campi compilati dall'utente o da file importati;
- l'**helper nativo RDP** (`fedtermrdp-native`): memoria condivisa, protocollo
  della clipboard, gestione dei file trasferiti;
- l'**iniezione OSC 7** nelle shell remote via SSH;
- l'importazione di sessioni da file non fidati.

Non rientrano le vulnerabilità di FreeRDP, OpenSSH, VTE, libvirt o GTK in sé:
vanno segnalate ai rispettivi progetti. Se però FedTermHub le rende sfruttabili
in un modo che altrimenti non lo sarebbero, segnalalo qui.

## Scelte di progetto rilevanti

- Il vault usa AES-256-GCM con chiave derivata da PBKDF2-HMAC-SHA256 a 600.000
  iterazioni, salt e nonce nuovi a ogni salvataggio.
- Le password non compaiono mai nella riga di comando: si passano via variabile
  d'ambiente (`sshpass -e`) o via file descriptor su `memfd` sigillato.
- I file di configurazione si scrivono in modo atomico con permessi 0600.
- L'helper nativo viene compilato con i flag di hardening della distribuzione
  (`%set_build_flags`); la CI verifica che il binario abbia `BIND_NOW`.
