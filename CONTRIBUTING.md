<!-- SPDX-FileCopyrightText: 2026 Crash
     SPDX-License-Identifier: GPL-3.0-or-later -->
# Contribuire a FedTermHub

Grazie per l'interesse. FedTermHub è un progetto indipendente: ogni
segnalazione precisa e ogni pull request ben circoscritta fanno davvero la
differenza.

## Segnalare un problema

Apri una [issue](https://github.com/Crash-FD01/FedTermHub/issues) indicando:

- la versione (`rpm -q fedtermhub`, oppure quella mostrata nella barra di stato);
- la versione di Fedora e se usi Wayland o X11 (`echo $XDG_SESSION_TYPE`);
- cosa hai fatto, cosa ti aspettavi e cosa è successo invece;
- per i problemi del terminale, l'output di **Diagnostica terminale** (menu
  contestuale del terminale);
- per i problemi RDP, il log della scheda (pulsante nella barra della sessione).

Non incollare mai password, chiavi o indirizzi interni dei tuoi clienti: il log
RDP maschera già la password, ma controlla prima di pubblicarlo.

## Preparare l'ambiente

```bash
sudo dnf install python3-gobject python3-cairo gtk3 vte291 \
    python3-cryptography python3-defusedxml \
    freerdp-devel libwinpr-devel gcc make pkgconf-pkg-config
git clone https://github.com/Crash-FD01/FedTermHub
cd FedTermHub
make -C native
FEDTERMHUB_RDP_NATIVE_HELPER="$PWD/native/fedtermrdp-native" \
    python3 fedtermhub/fedtermhub.py
```

## Prima di aprire una pull request

Esegui i controlli che fa anche la CI:

```bash
python3 -m compileall -q fedtermhub tests
python3 tests/test_core.py
python3 tests/test_theme_parity.py
xvfb-run -a python3 tests/test_remote_files.py   # e gli altri test GTK
```

Alcune regole che il progetto si è dato, quasi sempre dopo averle imparate a
proprie spese:

- **Una modifica allo stile va fatta in entrambi i temi.**
  `tests/test_theme_parity.py` fallisce se una classe è definita solo in
  `dark_css()` o solo in `light_css()`.
- **Mai la password sulla riga di comando di un processo.** Si passa per
  variabile d'ambiente (`sshpass -e`) o per file descriptor su memoria
  sigillata, come già fa il codice.
- **Niente `shell=True`, niente `except:` nudi, sempre un `timeout` sulle
  chiamate a `subprocess`.**
- **Variabili d'ambiente del terminale: non toglierne una perché sembra
  superflua.** `VTE_VERSION` sembrava superflua e serviva a
  `/etc/profile.d/vte.sh`.
- **Una correzione si appoggia a un riferimento verificabile** — il sorgente di
  FreeRDP, il comportamento documentato di virt-viewer — e non a un'ipotesi.
  Spiega nel messaggio di commit qual è la causa, non solo il sintomo.
- Ogni file nuovo porta l'intestazione SPDX:
  ```python
  # SPDX-FileCopyrightText: 2026 <Il tuo nome>
  # SPDX-License-Identifier: GPL-3.0-or-later
  ```

Contribuendo accetti che il tuo codice sia distribuito con licenza GPL-3.0 o
successiva, come il resto del progetto.

## Pubblicare una versione (manutentori)

1. Aggiorna `Version` e aggiungi la voce di `%changelog` in `fedtermhub.spec`.
2. Aggiorna `APP_VERSION` in `fedtermhub/fedtermhub.py`, la versione in
   `README.md` e `install.sh`, e il blocco `<releases>` del file
   `packaging/io.github.crash_fd01.fedtermhub.metainfo.xml`.
3. Commit, poi tag con la **stessa** versione dello spec:
   ```bash
   git tag v0.9.07
   git push origin main v0.9.07
   ```
4. Il workflow *Release* verifica che tag e spec coincidano, costruisce gli RPM
   e pubblica la release. Copr ricostruisce da solo grazie al webhook.
