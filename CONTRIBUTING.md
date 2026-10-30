<!-- SPDX-FileCopyrightText: 2026 Crash
     SPDX-License-Identifier: GPL-3.0-or-later -->
# Contribuire a FedTermHub

Grazie per l'interesse. FedTermHub è un progetto indipendente: ogni
segnalazione precisa e ogni pull request ben circoscritta fanno davvero la
differenza.

## Segnalare un problema

Apri una [issue](https://github.com/Crash-FD01/FedTermHub/issues) indicando:

- la versione (`rpm -q fedtermhub`, oppure quella mostrata nella barra di stato);
- la distribuzione e la versione (`fedtermhub --doctor` le stampa) e se usi Wayland o X11 (`echo $XDG_SESSION_TYPE`);
- cosa hai fatto, cosa ti aspettavi e cosa è successo invece;
- per i problemi del terminale, l'output di **Diagnostica terminale** (menu
  contestuale del terminale);
- per i problemi RDP, il log della scheda (pulsante nella barra della sessione).

Non incollare mai password, chiavi o indirizzi interni dei tuoi clienti: il log
RDP maschera già la password, ma controlla prima di pubblicarlo.

## Preparare l'ambiente

```bash
git clone https://github.com/Crash-FD01/FedTermHub
cd FedTermHub
./install.sh            # dipendenze con dnf/apt/zypper/pacman e motore RDP compilato
# poi, per provare le modifiche direttamente dai sorgenti:
FEDTERMHUB_RDP_NATIVE_HELPER="$HOME/.local/libexec/fedtermhub/fedtermrdp-native" \
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

### Testi e traduzioni

I testi dell'interfaccia si scrivono in italiano dentro `tr()` (o `trn()` per
singolare e plurale); i valori variabili vanno in segnaposto con nome, così chi
traduce può spostarli dove la frase lo richiede:

```python
tr("Sessione aggiunta: {name}").format(name=sess.name)
trn("{n} sessione", "{n} sessioni", n).format(n=n)
```

Dopo aver aggiunto o cambiato testi, aggiorna il catalogo e traduci le righe
`msgstr` rimaste vuote in `fedtermhub/locale/en.po`:

```bash
python3 tools/i18n_update.py
```

`tests/test_i18n.py` fallisce se il catalogo non è aggiornato, se una
traduzione perde un segnaposto o il mnemonico (`_Salva` → `_Save`), o se il
programma non parte in inglese. Per provare l'interfaccia in inglese:
`FEDTERMHUB_LANGUAGE=en python3 fedtermhub/fedtermhub.py`.

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
3. Unisci su `main` (con una pull request o con un push). Il workflow
   *Pubblicazione automatica* vede che per quella versione non c'è ancora il
   tag, lo crea e avvia *Rilascio*. Se la versione non è coerente in tutti i
   file si ferma con un errore e non crea niente. Unire modifiche senza
   cambiare versione non pubblica nulla.
   Il tag a mano funziona ancora: `git tag -a vX.Y.Z -m "FedTermHub X.Y.Z" && git push origin vX.Y.Z`.
4. *Rilascio* costruisce gli RPM per Fedora su Fedora 43 e, con
   *Pacchetti per le altre distribuzioni*, i `.deb` e gli `.rpm` universali su
   AlmaLinux 9 (la glibc più vecchia fra quelle supportate), con FreeRDP 3
   inclusa nel motore RDP; x86_64 e aarch64. Li firma, pubblica la release con
   `SHA256SUMS` firmato e aggiorna i repository (dnf per Fedora, dnf/zypper per
   RHEL e openSUSE, APT) e il sito. Chi ha installato con `setup.sh` riceve
   l'aggiornamento con `apt`, `dnf` o `zypper`.
5. Subito dopo, il workflow *Verifica installazione* installa la nuova versione
   dal sito su Fedora, Debian, Ubuntu, AlmaLinux e openSUSE puliti, con lo
   stesso comando del README, e controlla firme, versione, avvio, motore RDP
   (collegato a un xrdp locale) e rimozione. Gira anche ogni lunedì: se
   diventa rosso, il problema lo vedono gli utenti.

Le pull request che toccano programma o pacchetti costruiscono gli stessi
pacchetti universali e li installano su ogni distribuzione supportata, dove
girano anche alcuni test dei sorgenti con il GTK e il Python di quella
distribuzione (`packaging/ci-install-test.sh`).

**FreeRDP inclusa.** La versione sta in `packaging/build-freerdp.sh`, con il suo
SHA-256. Quando FreeRDP pubblica una correzione di sicurezza: aggiorna
versione e SHA-256 (quello del file `.sha256` accanto all'archivio sulla pagina
della release di FreeRDP) e pubblica una nuova versione di FedTermHub.

`.copr/Makefile` permette in alternativa di costruire su
[Copr](https://copr.fedorainfracloud.org) con il metodo "Make SRPM".
