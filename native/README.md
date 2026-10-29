# fedtermrdp-native

Helper minimale che collega FedTermHub a `libfreerdp3` senza creare una finestra nativa.

## Canali IPC

- `--config-fd`: opzioni FreeRDP, una per riga, in un memfd anonimo;
- `--frame-fd`: header di stato da 1024 byte seguito dal framebuffer BGRA;
- `--command-fd`: messaggi binari fissi da 16 byte per input, controllo e cambio risoluzione.

Il helper legge la configurazione, la cancella dalla propria memoria, chiude il file descriptor e conserva le credenziali soltanto nelle strutture FreeRDP necessarie alla connessione.

## Build

```bash
sudo dnf install freerdp-devel libwinpr-devel gcc make pkgconf-pkg-config
make
```

Il codice usa le API client-common di FreeRDP 3 e un contesto basato su `rdpClientContext`.

## Ridimensionamento dinamico

Il comando `FTH_CMD_RESIZE` inoltra larghezza, altezza e scala al canale dinamico `Microsoft::Windows::RDS::DisplayControl`. Se il canale non è ancora collegato, l’ultima richiesta viene conservata e inviata appena disponibile.

## Rendering 3.16.2

Il helper mantiene un framebuffer cumulativo nella memoria condivisa e copia soltanto le regioni invalidate da FreeRDP. Il frontend GTK crea una superficie Cairo direttamente sopra la stessa area `mmap`: non vengono più prodotti snapshot completi, `bytearray` intermedi o code di frame in Python.

Il campo atomico `seq` protegge gli aggiornamenti. Se il producer cambia il framebuffer durante un draw, GTK accorpa una nuova richiesta di ridisegno senza bloccare input o rete. I campi `reserved[1..4]` continuano a descrivere il rettangolo dirty per `mark_dirty_rectangle`.

### Stabilizzazione 3.16.2

Il percorso di produzione torna a pubblicare fotogrammi completi e coerenti. Le ottimizzazioni dirty-rectangle e mmap diretto delle versioni precedenti sono state ritirate perché non hanno superato il collaudo reale.
