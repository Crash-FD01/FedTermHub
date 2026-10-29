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
python3 tests/test_theme_parity.py
python3 tests/test_versions.py
for t in tests/test_*.py; do xvfb-run -a python3 "$t" || echo "FALLITO: $t"; done
```

`tests/test_ui_behaviours.py` prova sull'applicazione vera i comportamenti che si
rompono facilmente: le combinazioni da tastiera dentro il terminale, la posizione
delle sessioni nuove, i filtri dei file remoti.

Se cambi l'interfaccia, rigenera le schermate (dati inventati, non tocca la tua
configurazione):

```bash
xvfb-run -a -s "-screen 0 1400x880x24" python3 tools/screenshots.py . light docs/screenshots/principale-chiaro.png
```

e le altre tre varianti indicate in `tools/screenshots.py`.

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

La prima volta, dal tuo computer, una sola volta: `packaging/github-setup.sh`.
Configura la pagina del repository e GitHub Pages, crea la chiave che firma i
pacchetti (la privata resta tua e nel secret di GitHub) e pubblica il primo tag.

Per ogni versione successiva:

1. Aggiorna `Version` e aggiungi la voce di `%changelog` in `fedtermhub.spec`.
2. Aggiorna `APP_VERSION` in `fedtermhub/fedtermhub.py`, `APP_VERSION` in
   `install.sh` e il blocco `<releases>` di
   `packaging/io.github.crash_fd01.fedtermhub.metainfo.xml`.
   `python3 tests/test_versions.py` controlla di non averne dimenticato nessuno.
3. Commit e push, poi il tag con la **stessa** versione dello spec:
   ```bash
   git tag -a v0.9.08 -m "FedTermHub 0.9.08"
   git push origin v0.9.08
   ```
4. Il workflow *Rilascio* verifica che il tag coincida con la versione in tutti
   quei file, costruisce gli RPM
   su Fedora 43, li firma, pubblica la release con `SHA256SUMS` firmato e
   aggiorna repository dnf e sito. Chi ha installato con `setup.sh` riceve
   l'aggiornamento con `sudo dnf upgrade`.
5. Subito dopo, il workflow *Verifica installazione* installa la nuova versione
   dal sito su Fedora 43 e 44 puliti, con lo stesso comando del README, e
   controlla firme, versione, avvio e rimozione. Gira anche ogni lunedì: se
   diventa rosso, il problema lo vedono gli utenti.

`.copr/Makefile` permette in alternativa di costruire su
[Copr](https://copr.fedorainfracloud.org) con il metodo "Make SRPM".
