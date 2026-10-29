# FedTermHub 0.9.05.6 — Rapporto di correzione bug

## Obiettivo

Correggere tre malfunzionamenti segnalati sul campo (drag&drop file SFTP, barra
RDP a schermo intero limitata a tre posizioni, scanner di rete e porte poco
affidabili) senza regressioni su Sgancia, finestra indipendente, fullscreen e
selezione monitor introdotti nella serie 0.9.x.

## Modifiche verificate

- **Drag&drop SFTP** — il target di rilascio non usa piu' `DestDefaults.ALL`
  (che scavalcava i gestori manuali e impediva il drop): con `DestDefaults.0`
  la sequenza `drag-motion` → `drag-drop` (che richiede esplicitamente i dati)
  → `drag-data-received` scatta in modo affidabile. Aggiunti evidenziazione
  della riga di destinazione, memorizzazione del punto di rilascio e fallback
  di decodifica `text/uri-list` grezzo per i setup Wayland.
- **Drag&drop sul canvas RDP** — passato a `DestDefaults.DROP | MOTION` per
  eliminare il doppio `drag_finish` che si verificava con `DestDefaults.ALL`.
- **Barra RDP a schermo intero** — sostituito il ciclo sinistra/centro/destra
  con trascinamento libero: maniglia dedicata, handler `get-child-position`
  sull'overlay, clamp ai bordi, cursore grab/grabbing e doppio clic per
  riportarla ancorata in alto al centro. La barra non scompare mentre la si
  trascina.
- **Network scanner** — opzione "ARP con privilegi (MAC/vendor)" via `pkexec`
  per popolare MAC e produttore e rilevare host che ignorano il ping; gestione
  dei codici pkexec 126/127 e suggerimento contestuale quando manca il MAC.
- **Port scanner** — retry automatico con `-Pn` quando l'host non risponde al
  ping (host marcato "down"), con messaggi di stato che spiegano come procedere
  quando non emergono porte aperte.

## Verifiche automatiche

- Tutti i moduli Python compilano senza errori (`py_compile`).
- Logica di parsing XML nmap (host up/down, estrazione porte) e inserimento di
  `-Pn` verificati con test isolati.
- I comportamenti GTK interattivi (drag&drop, trascinamento della barra) vanno
  provati sul desktop Fedora reale: non sono coperti dai test headless.

## Limite dichiarato

Questa release e' focalizzata sulla correzione dei bug segnalati. La latenza
RDP residua resta da misurare sul collegamento reale, come gia' annotato nelle
release precedenti.

## Aggiunte 0.9.05.4 — clipboard file Windows -> Linux

Non si trattava di una regressione: la direzione remota -> host per i file non
era mai stata implementata. L'helper cercava nella lista formati del server
solo `CF_UNICODETEXT`/`CF_TEXT`, quindi un `FileGroupDescriptorW` (file copiato
in Windows) veniva scartato senza alcun effetto.

- l'helper riconosce ora `FileGroupDescriptorW` confrontando **anche il nome**
  del formato, perche' il suo id e' registrato dinamicamente e cambia a ogni
  sessione: un confronto sul solo id fallirebbe;
- la lista file viene passata a `cliprdr_file_context_update_server_data`, che
  la mappa su percorsi locali (mount FUSE); da li' si ricava una `text/uri-list`
  reale;
- il lato Python pubblica i file sugli appunti GTK con **due** target:
  `text/uri-list` e `x-special/gnome-copied-files`. Il secondo e' indispensabile:
  senza, Nautilus ignora il Ctrl+V anche con URI corretti;
- protocollo mailbox portato a versione 3 con il campo `remote_kind`.

### Verifiche eseguite

- **Compilazione e linking reali dell'helper C** con FreeRDP 3.30 (header
  installati nell'ambiente di sviluppo): tutti i simboli `cliprdr_file_context_*`
  risolvono. Nessun warning nuovo rispetto alla baseline; risolto anche un
  warning `-Wcomment` preesistente.
- **Offset della struct condivisa verificati numericamente** tra helper C e lato
  Python (`remote_seq` 20, `remote_len` 24, `host_kind` 32, `remote_kind` 36,
  header 64 byte): un disallineamento qui corromperebbe la clipboard in modo
  silenzioso.
- Tutti i moduli Python compilano; tarball riverificato dopo l'estrazione.

### Dipendenza dichiarata

L'incolla dei file da Windows a Linux richiede FreeRDP compilato con supporto
FUSE. Dove manca, la funzione e' tecnicamente impossibile lato client: in quel
caso il log riporta un messaggio esplicito e la via alternativa resta la
«Cartella condivisa», che funziona in entrambe le direzioni senza dipendenze.

## Aggiunte 0.9.05.6 — tastiera RDP e Lab temporaneo

### Bloc Num invertito passando da sessione remota a locale

**Causa.** `_on_focus_in` inviava `CMD_FOCUS` con il primo argomento fisso a `0`.
L'helper lo inoltra a `freerdp_input_send_focus_in_event(input, toggleStates)`,
dove quel campo è la maschera dello stato dei tasti di blocco dichiarata al
server (MS-RDPBCGR 2.2.8.1.1.3.1.1.5). Con `0`, a ogni ingresso nella scheda il
client dichiarava "Bloc Num e Bloc Maiusc spenti" indipendentemente dalla realtà,
e Windows li spegneva nella sessione remota.

La divergenza si accumulava perché lo stato di blocco locale viene aggiornato dal
server X (o dal compositor) **prima** che l'evento raggiunga GTK: il `return True`
dell'handler ferma la propagazione GTK ma non impedisce il toggle locale. Ogni
pressione di Bloc Num dentro la sessione cambiava quindi entrambi i lati, mentre
solo il remoto veniva riazzerato al focus successivo.

**Correzione.** `_lock_toggle_states()` legge lo stato reale da `Gdk.Keymap`
(`get_num_lock_state`, `get_caps_lock_state`, `get_scroll_lock_state`, ognuno
protetto singolarmente perché non tutti i backend GDK li espongono) e lo invia
come maschera `KBD_SYNC_*`. Poiché il lato locale è sempre autoritativo, la
sincronizzazione a senso unico sul focus-in è sufficiente. `sync_lock_keys()`
viene richiamata anche 400 ms dopo ogni passaggio a `STATE_CONNECTED`, perché una
sessione riconnessa riparte con gli indicatori azzerati.

**Non coperto.** Se è Windows a cambiare gli indicatori, invia una *Server Set
Keyboard Indicators PDU* che non viene ancora intercettata (`SetKeyboardIndicators`
di FreeRDP). Il caso pratico è coperto; manca la simmetria completa.

### Lab temporaneo: console che non si apre e VM orfane

**Causa.** Le VM sono create con `--graphics spice,listen=none`: un display privo
di qualunque listener, né TCP né socket Unix, raggiungibile solo tramite
`virDomainOpenGraphicsFD()` di libvirt. virt-viewer usa quella via
**esclusivamente in modalità `--attach`**, flag che mancava nell'invocazione:
cercava host e porta nell'XML, non li trovava e falliva con *"Display can only be
attached through libvirt with --attach"*.

`listen=none` è stato mantenuto: è la configurazione più stretta possibile e il
difetto era nel viewer, non nella VM. Il ripiego documentato, se su qualche
installazione il passaggio del descrittore desse problemi, è
`--graphics spice,listen=socket`.

**Effetto collaterale corretto.** virt-viewer restava vivo mostrando il proprio
dialogo di errore, quindi il `wait()` di `_watch_viewer` non tornava mai e la VM
non veniva distrutta: l'istanza restava `running` e ogni nuovo tentativo ne
accumulava una in più (due "Linux test" attivi nella segnalazione). Ora il
guardiano usa `communicate()` — che attende **e** svuota la pipe, evitando il blocco su buffer
pieno — e considera fallimento solo un'uscita con **codice positivo** avvenuta
mentre `cleanup_started` è falso. Le due condizioni servono entrambe: un codice
negativo è terminazione da segnale, cioè il nostro `terminate()`/`kill()` quando
l'utente preme "Termina ed elimina", e senza il controllo su `cleanup_started`
comparirebbe un dialogo di errore per una console che l'utente ha chiuso di sua
volontà. Il tempo trascorso, usato in una prima stesura, è stato scartato perché
non è un criterio affidabile: virt-viewer resta vivo finché l'utente non chiude
il proprio dialogo, quindi un fallimento immediato può manifestarsi minuti dopo.
Il motivo reale, letto da stderr invece che scartato, arriva all'utente tramite
il callback `on_viewer_error`, sganciato alla distruzione della finestra per non
toccare widget morti.

### Limiti dichiarati

Entrambe le correzioni richiedono hardware reale per la verifica finale: la prima
una sessione RDP verso Windows, la seconda libvirt con QEMU/KVM. In questo
ambiente sono stati verificati solo compilazione, coerenza del comando generato e
assenza di regressioni sui test esistenti.
