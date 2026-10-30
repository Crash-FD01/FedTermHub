# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Browser SFTP contestuale per la barra laterale di FedTermHub.

Il modulo usa il client OpenSSH ``sftp`` già richiesto dall'applicazione:
non introduce daemon, librerie di rete o credenziali aggiuntive. Le operazioni
sono eseguite fuori dal thread GTK e il contenuto delle directory viene caricato
solo quando l'utente le espande.
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple
try:
    from .i18n import tr, trn
except ImportError:  # avvio come script (fedtermhub/fedtermhub.py) o dai test
    from i18n import tr, trn

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402


@dataclass(frozen=True)
class SftpConnection:
    """Parametri non persistenti necessari al browser remoto."""

    label: str
    host: str
    port: str = "22"
    user: str = ""
    password: Optional[str] = None
    jump_spec: str = ""
    ssh_options: Sequence[str] = ()
    timeout_seconds: int = 12

    @property
    def identity(self) -> str:
        # La password non entra mai nell'identificatore o nei messaggi UI.
        return "\x1f".join(
            (
                self.host.strip(),
                self.port.strip() or "22",
                self.user.strip(),
                self.jump_spec.strip(),
                " ".join(self.ssh_options),
            )
        )


@dataclass(frozen=True)
class RemoteEntry:
    name: str
    path: str
    kind: str  # directory, file, link, other
    mode: str = ""
    size: int = 0
    detail: str = ""

    @property
    def is_directory(self) -> bool:
        return self.kind == "directory"


class SftpCommandError(RuntimeError):
    pass


def sftp_quote(value: str) -> str:
    """Quota un argomento per il linguaggio batch di OpenSSH sftp."""
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(tr("Il percorso remoto contiene caratteri non validi."))
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def remote_join(parent: str, name: str) -> str:
    parent = parent or "/"
    joined = posixpath.normpath(posixpath.join(parent, name))
    if not joined.startswith("/"):
        joined = "/" + joined
    return joined


def format_size(value: int) -> str:
    size = float(max(0, value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{value} B"


def friendly_sftp_error(error: str) -> str:
    text = (error or tr("Errore SFTP non specificato")).strip()
    lowered = text.casefold()
    if "permission denied" in lowered or "authentication" in lowered or "/dev/tty" in lowered:
        return (
            tr("Autenticazione SFTP non disponibile. Completa l'accesso nel terminale SSH "
            "oppure sblocca il Password Manager, quindi premi Aggiorna.")
        )
    if "host key verification failed" in lowered:
        return tr("Chiave host non accettata. Verifica la sessione SSH e riprova.")
    if "could not resolve hostname" in lowered:
        return tr("Nome host non risolto. Controlla DNS, VPN e configurazione della sessione.")
    if "connection refused" in lowered:
        return tr("Connessione SFTP rifiutata: controlla porta SSH e servizio sshd.")
    if "timeout" in lowered or "timed out" in lowered:
        return tr("Timeout SFTP: controlla rete, VPN, firewall e Jump Host.")
    return text[-700:]


def parse_sftp_long_listing(output: str, parent: str) -> List[RemoteEntry]:
    """Converte l'output di ``sftp ls -la`` in elementi ordinati.

    OpenSSH usa un formato simile a ``ls -l``. ``split(maxsplit=8)`` conserva
    correttamente i nomi contenenti spazi. Righe non riconosciute vengono
    ignorate invece di trasformarsi in nodi potenzialmente errati.
    """
    entries: List[RemoteEntry] = []
    for raw_line in output.splitlines():
        line = raw_line.strip("\r\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("sftp>"):
            continue
        if stripped.startswith((tr("Connected to "), tr("Changing to: "), tr("Remote working directory:"))):
            continue
        parts = stripped.split(maxsplit=8)
        if len(parts) < 9:
            continue
        mode, _links, _owner, _group, size_text, date_a, date_b, date_c, name = parts
        if not re.fullmatch(r"[bcdlps-][rwxStTs-]{9}[+@.]?", mode):
            continue
        if name in {".", ".."}:
            continue
        display_name = name
        if mode.startswith("l") and " -> " in display_name:
            display_name = display_name.split(" -> ", 1)[0]
        # Alcuni server SFTP restituiscono il percorso completo anche per ls -l.
        # Mostriamo solo il nome ma manteniamo il percorso assoluto corretto.
        absolute_name = display_name if display_name.startswith("/") else ""
        if absolute_name:
            display_name = posixpath.basename(absolute_name.rstrip("/")) or absolute_name
        # Secondo filtro per "." e "..": con i percorsi assoluti (es.
        # "/home/utente/.") il primo controllo non scatta perché il nome grezzo
        # non è letteralmente "." — dopo il basename lo è. Senza questo filtro
        # l'albero mostrava voci "." e ".." che, espanse, rilistavano la stessa
        # cartella all'infinito o risalivano di livello.
        if display_name in {".", ".."}:
            continue
        try:
            size = int(size_text)
        except ValueError:
            size = 0
        if mode.startswith("d"):
            kind = "directory"
        elif mode.startswith("l"):
            kind = "link"
        elif mode.startswith("-"):
            kind = "file"
        else:
            kind = "other"
        path = posixpath.normpath(absolute_name) if absolute_name else remote_join(parent, display_name)
        date_text = f"{date_a} {date_b} {date_c}"
        detail = mode
        if kind != "directory":
            detail += f"  •  {format_size(size)}"
        detail += f"  •  {date_text}"
        entries.append(RemoteEntry(display_name, path, kind, mode, size, detail))
    entries.sort(key=lambda item: (0 if item.is_directory else 1, item.name.casefold()))
    return entries


class SftpBatchClient:
    """Piccolo adapter sincrono attorno al client OpenSSH sftp."""

    def __init__(self, connection: SftpConnection) -> None:
        self.connection = connection

    def _argv_and_env(self) -> tuple[List[str], dict[str, str]]:
        sftp_binary = shutil.which("sftp")
        if not sftp_binary:
            raise SftpCommandError(tr("Client SFTP non trovato. Installa openssh-clients."))
        argv: List[str] = [sftp_binary, "-q"]
        argv.extend(str(item) for item in self.connection.ssh_options)
        if self.connection.jump_spec:
            argv.extend(["-J", self.connection.jump_spec])
        port = self.connection.port.strip() or "22"
        if port:
            argv.extend(["-P", port])
        target = self.connection.host.strip()
        if self.connection.user.strip():
            target = f"{self.connection.user.strip()}@{target}"
        # "--": un host che comincia con "-" non deve diventare un'opzione di sftp.
        argv += ["--", target]
        env = os.environ.copy()
        if self.connection.password:
            sshpass = shutil.which("sshpass")
            if not sshpass:
                raise SftpCommandError(
                    tr("La sessione usa una password salvata, ma sshpass non è installato.")
                )
            argv = [sshpass, "-e"] + argv
            env["SSHPASS"] = self.connection.password
        return argv, env

    def run(self, commands: Iterable[str], timeout: Optional[int] = None) -> str:
        argv, env = self._argv_and_env()
        payload = "\n".join(command.rstrip("\n") for command in commands) + "\nbye\n"
        try:
            completed = subprocess.run(
                argv,
                input=payload,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                timeout=float(timeout or max(8, self.connection.timeout_seconds + 5)),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SftpCommandError(tr("Timeout durante la connessione SFTP.")) from exc
        except OSError as exc:
            raise SftpCommandError(tr("Impossibile avviare il client SFTP: {exc}").format(exc=exc)) from exc
        combined = "\n".join(part for part in (completed.stderr, completed.stdout) if part).strip()
        failure_markers = (
            tr("Permission denied"), tr("No such file"), tr("Couldn't"), tr("Connection closed"),
            tr("Connection refused"), tr("Host key verification failed"), tr("Could not resolve hostname"),
            tr("subsystem request failed"),
        )
        if completed.returncode != 0 or any(marker in combined for marker in failure_markers):
            detail = combined or tr("Errore SFTP non specificato")
            # Evita finestre smisurate e non espone la riga di comando.
            raise SftpCommandError(detail[-1200:])
        return completed.stdout

    def pwd(self) -> str:
        output = self.run(["pwd"])
        for line in output.splitlines():
            match = re.search(r"Remote working directory:\s*(.+)$", line.strip())
            if match:
                path = match.group(1).strip()
                return path if path.startswith("/") else "/" + path
        return "/"

    def listdir(self, path: str) -> List[RemoteEntry]:
        normalized = posixpath.normpath(path or "/")
        output = self.run([tr("ls -la {sftp_quote}").format(sftp_quote=sftp_quote(normalized))])
        entries = parse_sftp_long_listing(output, normalized)
        if entries:
            return entries

        # Fallback per server SFTP che producono un long listing non POSIX
        # (alcuni appliance, NAS e OpenSSH Windows). Recuperiamo prima i nomi
        # e poi chiediamo i dettagli in un unico batch. In questo modo l'albero
        # non rimane vuoto soltanto perché owner/gruppo/data hanno un formato
        # differente da GNU ls.
        simple = self.run([tr("ls -1 {sftp_quote}").format(sftp_quote=sftp_quote(normalized))])
        names: List[str] = []
        for raw in simple.splitlines():
            item = raw.strip()
            if not item or item.startswith("sftp>"):
                continue
            if item.startswith((tr("Connected to "), tr("Changing to: "), tr("Remote working directory:"))):
                continue
            if item in {".", ".."}:
                continue
            if item.startswith(normalized.rstrip("/") + "/"):
                item = posixpath.basename(item.rstrip("/"))
            names.append(item)
        if not names:
            return []

        detail_commands = [
            tr("ls -ld {sftp_quote}").format(sftp_quote=sftp_quote(remote_join(normalized, name))) for name in names
        ]
        try:
            detailed = parse_sftp_long_listing(self.run(detail_commands), normalized)
        except SftpCommandError:
            detailed = []
        by_name = {entry.name: entry for entry in detailed}
        result: List[RemoteEntry] = []
        for name in names:
            entry = by_name.get(name)
            if entry is not None:
                result.append(entry)
            else:
                # Tipo non determinabile: viene mostrato come file generico.
                # L'utente può comunque scaricarlo; un refresh successivo può
                # arricchirlo se il server restituisce metadati migliori.
                result.append(RemoteEntry(name, remote_join(normalized, name), "file"))
        result.sort(key=lambda item: (0 if item.is_directory else 1, item.name.casefold()))
        return result

    def download(self, remote_path: str, local_path: Path, recursive: bool = False) -> None:
        option = "-r " if recursive else ""
        self.run([tr("get {option}-p {sftp_quote} {sftp_quote2}").format(option=option, sftp_quote=sftp_quote(remote_path), sftp_quote2=sftp_quote(str(local_path)))], timeout=3600)

    def upload(self, local_path: Path, remote_directory: str) -> None:
        remote_path = remote_join(remote_directory, local_path.name)
        option = "-r " if local_path.is_dir() else ""
        self.run([tr("put {option}-p {sftp_quote} {sftp_quote2}").format(option=option, sftp_quote=sftp_quote(str(local_path)), sftp_quote2=sftp_quote(remote_path))], timeout=3600)


class RemoteFilesSidebar(Gtk.Box):
    """Albero SFTP lazy-loaded da montare nella sidebar dell'applicazione."""

    COL_NAME = 0
    COL_PATH = 1
    COL_KIND = 2
    COL_DETAIL = 3
    COL_ICON = 4
    COL_LOADED = 5
    COL_LOADING = 6

    def __init__(
        self,
        parent_window: Gtk.Window,
        client_factory: Callable[[SftpConnection], SftpBatchClient] = SftpBatchClient,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.parent_window = parent_window
        self.client_factory = client_factory
        self.connection: Optional[SftpConnection] = None
        self.client: Optional[SftpBatchClient] = None
        self.connection_identity = ""
        self.home_path = "/"
        self._generation = 0
        self._busy_count = 0
        # Filtri della vista. Letti da entry_is_visible() a ogni inserimento,
        # quindi vanno impostati prima che si costruisca qualunque riga.
        self.show_hidden = False
        self.filter_text = ""
        # Posizione (x, y) del rilascio, valorizzata in on_drag_drop e usata da
        # on_drag_data_received (che riceve x/y non affidabili con DestDefaults.0).
        self._pending_drop_position: Optional[Tuple[int, int]] = None

        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        header.set_margin_start(6)
        header.set_margin_end(6)
        header.set_margin_top(6)
        header.set_margin_bottom(4)
        session_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        session_icon = Gtk.Image.new_from_icon_name("folder-remote-symbolic", Gtk.IconSize.MENU)
        session_row.pack_start(session_icon, False, False, 0)
        self.session_label = Gtk.Label(label=tr("Nessuna sessione SSH attiva"), xalign=0)
        self.session_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.session_label.get_style_context().add_class("remote-session-label")
        session_row.pack_start(self.session_label, True, True, 0)
        header.pack_start(session_row, False, False, 0)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.home_button = Gtk.Button.new_from_icon_name("go-home-symbolic", Gtk.IconSize.MENU)
        self.home_button.set_tooltip_text(tr("Torna alla cartella home remota"))
        self.home_button.connect("clicked", lambda *_: self.load_root(self.home_path))
        self.up_button = Gtk.Button.new_from_icon_name("go-up-symbolic", Gtk.IconSize.MENU)
        self.up_button.set_tooltip_text(tr("Sali alla cartella superiore"))
        self.up_button.connect("clicked", self.on_up_clicked)
        self.refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.MENU)
        self.refresh_button.set_tooltip_text(tr("Rileggi la cartella corrente"))
        self.refresh_button.connect("clicked", lambda *_: self.refresh())
        # Interruttore dell'inseguimento: se stai navigando l'albero con le
        # mani, un cd nel terminale non deve spostartelo sotto il cursore.
        self.follow_button = Gtk.ToggleButton()
        self.follow_button.set_image(
            Gtk.Image.new_from_icon_name("mail-forward-symbolic", Gtk.IconSize.MENU)
        )
        self.follow_button.set_tooltip_text(
            tr("Segui la cartella del terminale\n"
            "Quando è attivo, l'albero si sposta insieme ai cd della shell.")
        )
        self.follow_button.set_active(True)
        # Impostato dall'applicazione per persistere la scelta.
        self.on_follow_changed: Optional[Callable[[bool], None]] = None
        self.follow_button.connect("toggled", self._on_follow_toggled)
        # Su una home reale i dotfile sono la maggioranza delle voci (.bashrc,
        # .cache, .ssh, .viminfo…) e seppelliscono il contenuto vero: nascosti
        # per impostazione predefinita, come in qualunque file manager.
        self.hidden_button = Gtk.ToggleButton()
        self.hidden_button.set_image(
            Gtk.Image.new_from_icon_name("view-reveal-symbolic", Gtk.IconSize.MENU)
        )
        self.hidden_button.set_tooltip_text(
            tr("Mostra i file nascosti (Ctrl+H)\n"
            "I nomi che iniziano con un punto sono esclusi per impostazione predefinita.")
        )
        self.hidden_button.set_active(False)
        self.hidden_button.connect("toggled", self._on_hidden_toggled)
        for button in (self.home_button, self.up_button, self.refresh_button,
                       self.follow_button, self.hidden_button):
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.get_style_context().add_class("mini-action")
            toolbar.pack_start(button, False, False, 0)
        header.pack_start(toolbar, False, False, 0)

        # Filtro per nome sulla cartella aperta. Non e' una ricerca ricorsiva
        # sul server: filtra cio' che e' gia' stato letto, che e' quello che
        # serve quando una cartella ha centinaia di voci in una sidebar stretta.
        self.filter_entry = Gtk.SearchEntry()
        self.filter_entry.set_placeholder_text(tr("Filtra per nome…"))
        self.filter_entry.set_tooltip_text(
            tr("Filtra le voci della cartella corrente.\n"
            "Le cartelle restano visibili per poterci entrare.")
        )
        self.filter_entry.connect("search-changed", self._on_filter_changed)
        header.pack_start(self.filter_entry, False, False, 0)

        # Percorso corrente su riga propria a piena larghezza: con l'ellissi
        # centrale non viene mai tagliato, anche con la sidebar stretta. È la
        # riga "posizione" tipica dei file manager. Resta selezionabile per
        # copiare il percorso al volo.
        self.path_display = Gtk.Label(label="", xalign=0)
        self.path_display.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.path_display.set_selectable(True)
        self.path_display.set_max_width_chars(1)  # lascia decidere l'ellissi, non forza larghezza minima
        self.path_display.get_style_context().add_class("remote-path")
        header.pack_start(self.path_display, False, False, 0)

        transfer_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.download_button = Gtk.Button.new_with_mnemonic(tr("_Scarica"))
        self.download_button.set_tooltip_text(tr("Scarica il file o la cartella selezionata"))
        self.download_button.connect("clicked", self.on_download_clicked)
        self.download_button.get_style_context().add_class("secondary-action")
        self.upload_labeled_button = Gtk.Button.new_with_mnemonic(tr("_Carica qui"))
        self.upload_labeled_button.set_tooltip_text(tr("Carica file o cartelle nella cartella corrente"))
        self.upload_labeled_button.connect("clicked", self.on_upload_clicked)
        self.upload_labeled_button.get_style_context().add_class("secondary-action")
        transfer_bar.pack_start(self.download_button, True, True, 0)
        transfer_bar.pack_start(self.upload_labeled_button, True, True, 0)
        header.pack_start(transfer_bar, False, False, 0)
        self.pack_start(header, False, False, 0)
        # Alias di compatibilità: alcune parti del codice e i test cercano
        # ancora upload_button e path_label.
        self.upload_button = self.upload_labeled_button
        self.path_label = Gtk.Label()  # non più mostrata, ma usata come stato interno del percorso

        self.store = Gtk.TreeStore(str, str, str, str, str, bool, bool)
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.set_headers_visible(False)
        self.tree.set_level_indentation(10)
        self.tree.set_enable_tree_lines(False)
        self.tree.set_tooltip_column(self.COL_DETAIL)
        self.tree.connect("row-expanded", self.on_row_expanded)
        self.tree.connect("row-activated", self.on_row_activated)
        self.tree.connect("button-press-event", self.on_button_press)
        self.tree.get_selection().connect("changed", self.on_selection_changed)
        # NB: con DestDefaults.ALL GTK gestisce da sé motion/drop e ignora il
        # nostro drag-motion (che serve a mostrare la cartella di destinazione e
        # a rifiutare il drop quando non c'è connessione). Usiamo DestDefaults.0
        # e gestiamo esplicitamente motion, drop e la richiesta dei dati: è
        # l'unico modo affidabile perché il drag-data-received scatti davvero.
        self.tree.drag_dest_set(
            0,
            [Gtk.TargetEntry.new("text/uri-list", 0, 0)],
            Gdk.DragAction.COPY,
        )
        self.tree.connect("drag-motion", self.on_drag_motion)
        self.tree.connect("drag-drop", self.on_drag_drop)
        self.tree.connect("drag-leave", self.on_drag_leave)
        self.tree.connect("drag-data-received", self.on_drag_data_received)
        self.tree.set_tooltip_text(
            tr("Espandi le cartelle oppure trascina qui file e cartelle locali per caricarli.")
        )

        renderer_icon = Gtk.CellRendererPixbuf()
        renderer_icon.set_property("xpad", 4)
        renderer_text = Gtk.CellRendererText()
        renderer_text.set_property("ellipsize", Pango.EllipsizeMode.END)
        column = Gtk.TreeViewColumn(tr("File remoto"))
        column.pack_start(renderer_icon, False)
        column.add_attribute(renderer_icon, "icon-name", self.COL_ICON)
        column.pack_start(renderer_text, True)
        column.add_attribute(renderer_text, "text", self.COL_NAME)
        column.set_expand(True)
        self.tree.append_column(column)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.get_style_context().add_class("remote-files-scroll")
        scroll.add(self.tree)
        self.pack_start(scroll, True, True, 0)

        self.status_label = Gtk.Label(label=tr("Apri una sessione SSH per sfogliare i file remoti."), xalign=0)
        self.status_label.set_line_wrap(True)
        self.status_label.set_margin_start(8)
        self.status_label.set_margin_end(8)
        self.status_label.set_margin_top(5)
        self.status_label.set_margin_bottom(6)
        self.status_label.get_style_context().add_class("remote-status")
        self.pack_start(self.status_label, False, False, 0)
        self._set_controls_sensitive(False)

    def _on_hidden_toggled(self, button: Gtk.ToggleButton) -> None:
        self.show_hidden = bool(button.get_active())
        self.refresh()

    def _on_filter_changed(self, entry: Gtk.SearchEntry) -> None:
        testo = (entry.get_text() or "").strip().lower()
        if testo == self.filter_text:
            return
        self.filter_text = testo
        self.refresh()

    def toggle_hidden(self) -> None:
        """Scorciatoia Ctrl+H, come nei file manager."""
        self.hidden_button.set_active(not self.hidden_button.get_active())

    def focus_filter(self) -> None:
        self.filter_entry.grab_focus()

    def follows_terminal(self) -> bool:
        """True se l'albero deve seguire i cd della shell."""
        try:
            return bool(self.follow_button.get_active())
        except Exception:
            return False

    def _on_follow_toggled(self, button: Gtk.ToggleButton) -> None:
        if self.on_follow_changed is not None:
            self.on_follow_changed(bool(button.get_active()))

    def current_path(self) -> str:
        """Percorso alla radice dell'albero, o stringa vuota se scollegata."""
        return self.path_label.get_text() or ""

    def _set_current_path(self, path: str) -> None:
        """Aggiorna lo stato del percorso corrente e la riga posizione."""
        normalized = posixpath.normpath(path or "/") if path else ""
        self.path_label.set_text(normalized)
        if normalized:
            self.path_display.set_text(normalized)
            self.path_display.set_tooltip_text(normalized)
            self._update_up_button(normalized)
        else:
            self.path_display.set_text("")
            self.path_display.set_tooltip_text("")

    def _set_controls_sensitive(self, value: bool) -> None:
        for widget in (
            self.home_button, self.up_button, self.refresh_button, self.upload_button,
            self.upload_labeled_button, self.tree,
        ):
            widget.set_sensitive(value)
        self.download_button.set_sensitive(False)

    def clear_connection(self) -> None:
        self._generation += 1
        self.connection = None
        self.client = None
        self.connection_identity = ""
        self.home_path = "/"
        self.store.clear()
        self.session_label.set_text(tr("Nessuna sessione SSH attiva"))
        self._set_current_path("")
        self.status_label.set_text(tr("Apri una sessione SSH per sfogliare i file remoti."))
        self._set_controls_sensitive(False)

    def set_connection(self, connection: SftpConnection) -> None:
        if connection.identity == self.connection_identity and self.client is not None:
            return
        self._generation += 1
        generation = self._generation
        self.connection = connection
        self.client = self.client_factory(connection)
        client = self.client
        self.connection_identity = connection.identity
        self.store.clear()
        self.session_label.set_text(connection.label or connection.host)
        self._set_current_path("")
        self.status_label.set_text(tr("Connessione SFTP in corso…"))
        self._set_controls_sensitive(False)

        def worker() -> None:
            try:
                home = client.pwd()
                entries = client.listdir(home)
                GLib.idle_add(self._apply_root, generation, home, entries, "")
            except Exception as exc:  # UI: errore mostrato senza stack trace
                GLib.idle_add(self._apply_root, generation, "/", [], str(exc))

        threading.Thread(target=worker, name="fedtermhub-sftp-root", daemon=True).start()

    def _apply_root(self, generation: int, home: str, entries: List[RemoteEntry], error: str) -> bool:
        if generation != self._generation:
            return False
        if error:
            self.store.clear()
            self._set_current_path("")
            self.status_label.set_text(tr("SFTP non disponibile: {friendly_sftp_error}").format(friendly_sftp_error=friendly_sftp_error(error)))
            self._set_controls_sensitive(True)
            return False
        self.home_path = home or "/"
        self.load_root(self.home_path, prefetched=entries)
        self._set_controls_sensitive(True)
        return False

    def entry_is_visible(self, entry: RemoteEntry) -> bool:
        """Filtro applicato all'inserimento, non con un Gtk.TreeModelFilter.

        Il modulo usa iteratori dello store in una trentina di punti
        (espansione pigra, drop, menu contestuale): infilare un modello
        filtrante in mezzo costringerebbe a convertire ogni iteratore fra
        vista e modello, con molte occasioni di sbagliare. Filtrare
        all'inserimento e ricaricare quando il filtro cambia e' coerente con
        il caricamento pigro che il widget fa gia'.
        """
        # I segnaposto ("Caricamento…", "(vuota)", errori) non si filtrano mai:
        # sono stato dell'interfaccia, non contenuto remoto.
        if entry.kind == "placeholder":
            return True
        if not self.show_hidden and entry.name.startswith(".") and entry.name not in (".", ".."):
            return False
        if self.filter_text and self.filter_text not in entry.name.lower():
            # Le cartelle restano visibili anche se il nome non corrisponde:
            # altrimenti un risultato dentro una sottocartella diventerebbe
            # irraggiungibile.
            if not entry.is_directory:
                return False
        return True

    def _count_text(self, entries: List[RemoteEntry], posizione: str = "") -> str:
        """Testo della barra di stato dopo una lettura.

        Conta gli elementi VISIBILI, non quelli letti dal server, e dichiara
        quanti ne escludono i filtri: altrimenti una cartella di soli dotfile
        sembrerebbe vuota, o il numero non corrisponderebbe a cio' che si vede.
        Unica funzione per la cartella principale e per le sottocartelle, che
        prima avevano due messaggi separati e divergenti.
        """
        visibili = sum(1 for e in entries if self.entry_is_visible(e))
        esclusi = len(entries) - visibili
        testo = trn("{visibili} elemento", "{visibili} elementi", visibili).format(visibili=visibili)
        if posizione:
            testo += tr(" in {posizione}").format(posizione=posizione)
        if esclusi:
            testo += trn(" ({esclusi} non mostrato dai filtri)", " ({esclusi} non mostrati dai filtri)", esclusi).format(esclusi=esclusi)
        return testo

    def _append_entry(self, parent: Optional[Gtk.TreeIter], entry: RemoteEntry) -> Optional[Gtk.TreeIter]:
        if not self.entry_is_visible(entry):
            return None
        icon = {
            "directory": "folder-symbolic",
            "file": "text-x-generic-symbolic",
            "link": "emblem-symbolic-link-symbolic",
        }.get(entry.kind, "text-x-generic-symbolic")
        tree_iter = self.store.append(
            parent,
            [entry.name, entry.path, entry.kind, entry.detail, icon, not entry.is_directory, False],
        )
        if entry.is_directory:
            self.store.append(tree_iter, [tr("Caricamento…"), "", "placeholder", "", "content-loading-symbolic", True, False])
        return tree_iter

    def load_root(self, path: str, prefetched: Optional[List[RemoteEntry]] = None) -> None:
        if self.client is None:
            return
        normalized = posixpath.normpath(path or "/")
        generation = self._generation
        client = self.client
        if prefetched is not None:
            self._replace_root(generation, normalized, prefetched, "")
            return
        self.status_label.set_text(tr("Aggiornamento cartella remota…"))

        def worker() -> None:
            try:
                entries = client.listdir(normalized)
                GLib.idle_add(self._replace_root, generation, normalized, entries, "")
            except Exception as exc:
                GLib.idle_add(self._replace_root, generation, normalized, [], str(exc))

        threading.Thread(target=worker, name="fedtermhub-sftp-refresh", daemon=True).start()

    def _replace_root(self, generation: int, path: str, entries: List[RemoteEntry], error: str) -> bool:
        if generation != self._generation:
            return False
        if error:
            self.status_label.set_text(tr("Impossibile leggere {path}: {friendly_sftp_error}").format(path=path, friendly_sftp_error=friendly_sftp_error(error)))
            return False
        self.store.clear()
        root_name = path if path == "/" else posixpath.basename(path) or path
        root = self.store.append(
            None,
            [root_name, path, "directory", path, "folder-remote-symbolic", True, False],
        )
        for entry in entries:
            self._append_entry(root, entry)
        tree_path = self.store.get_path(root)
        self.tree.expand_row(tree_path, False)
        self.tree.get_selection().select_iter(root)
        self._set_current_path(path)
        self.status_label.set_text(self._count_text(entries))
        return False

    def refresh(self) -> None:
        # Aggiorna ricarica la cartella in cui sei. Prima, con una cartella selezionata, ci entrava:
        # "Aggiorna" cambiava posizione.
        self.load_root(self.current_path() or self.home_path)

    def selected_entry(self) -> Optional[RemoteEntry]:
        model, tree_iter = self.tree.get_selection().get_selected()
        if tree_iter is None:
            return None
        kind = model[tree_iter][self.COL_KIND]
        if kind == "placeholder":
            return None
        name = model[tree_iter][self.COL_NAME]
        path = model[tree_iter][self.COL_PATH]
        detail = model[tree_iter][self.COL_DETAIL]
        return RemoteEntry(name, path, kind, detail=detail)

    def selected_directory(self) -> str:
        selected = self.selected_entry()
        if selected is None:
            return self.path_label.get_text() or self.home_path
        if selected.kind == "directory":
            return selected.path
        return posixpath.dirname(selected.path) or "/"

    def on_selection_changed(self, _selection: Gtk.TreeSelection) -> None:
        entry = self.selected_entry()
        can_act = self.client is not None and entry is not None and entry.kind != "placeholder"
        self.download_button.set_sensitive(can_act)
        if entry is None:
            return
        if entry.kind == "directory":
            self._set_current_path(entry.path)
            self.status_label.set_text(tr("Cartella: {name}").format(name=entry.name))
        elif entry.kind == "placeholder":
            return
        else:
            container = posixpath.dirname(entry.path) or "/"
            self._set_current_path(container)
            # entry.detail contiene già modalità, dimensione e data del file.
            label = entry.detail or entry.name
            self.status_label.set_text(f"{entry.name}  —  {label}")

    def _update_up_button(self, path: str) -> None:
        # "Su" ha senso solo se non siamo già alla radice del filesystem.
        at_root = posixpath.normpath(path or "/") == "/"
        self.up_button.set_sensitive(self.client is not None and not at_root)

    def _destination_at_position(self, x: int, y: int) -> str:
        hit = self.tree.get_dest_row_at_pos(int(x), int(y))
        if hit is not None:
            tree_path, _position = hit
            try:
                tree_iter = self.store.get_iter(tree_path)
                kind = self.store[tree_iter][self.COL_KIND]
                path = self.store[tree_iter][self.COL_PATH]
                if kind == "directory" and path:
                    self.tree.get_selection().select_path(tree_path)
                    return path
                if path:
                    return posixpath.dirname(path) or "/"
            except Exception:
                pass
        return self.selected_directory()

    def _uri_list_target(self, context) -> Optional[Gdk.Atom]:  # noqa: ANN001
        """Trova il target text/uri-list tra quelli offerti dalla sorgente."""
        for atom in context.list_targets():
            if atom.name() == "text/uri-list":
                return atom
        return None

    def on_drag_motion(self, tree: Gtk.TreeView, context, x: int, y: int, time_value: int) -> bool:  # noqa: ANN001
        if self.client is None or self._uri_list_target(context) is None:
            Gdk.drag_status(context, 0, time_value)
            return False
        destination = self._destination_at_position(x, y)
        self.status_label.set_text(tr("Rilascia per caricare in {destination}").format(destination=destination))
        # Evidenzia la riga sotto il puntatore come bersaglio del drop.
        hit = tree.get_dest_row_at_pos(int(x), int(y))
        if hit is not None:
            tree.set_drag_dest_row(hit[0], Gtk.TreeViewDropPosition.INTO_OR_AFTER)
        Gdk.drag_status(context, Gdk.DragAction.COPY, time_value)
        return True

    def on_drag_leave(self, tree: Gtk.TreeView, _context, _time_value: int) -> None:  # noqa: ANN001
        tree.set_drag_dest_row(None, Gtk.TreeViewDropPosition.BEFORE)

    def on_drag_drop(self, tree: Gtk.TreeView, context, x: int, y: int, time_value: int) -> bool:  # noqa: ANN001
        """Con DestDefaults.0 il drop non è automatico: chiediamo noi i dati.

        Ricordiamo la posizione del rilascio perché drag-data-received arriva in
        un secondo momento (asincrono) e non riceve più x/y del punto di drop.
        """
        target = self._uri_list_target(context)
        if self.client is None or target is None:
            Gtk.drag_finish(context, False, False, time_value)
            return False
        self._pending_drop_position = (int(x), int(y))
        tree.drag_get_data(context, target, time_value)
        return True

    def on_drag_data_received(
        self,
        _tree: Gtk.TreeView,
        context,
        x: int,
        y: int,
        selection_data: Gtk.SelectionData,
        _info: int,
        time_value: int,
    ) -> None:  # noqa: ANN001
        self.tree.set_drag_dest_row(None, Gtk.TreeViewDropPosition.BEFORE)
        # get_uris() copre il caso Wayland/GNOME; se manca, prova a decodificare
        # il testo grezzo (alcune sorgenti inviano solo il payload text/uri-list).
        uris = list(selection_data.get_uris() or [])
        if not uris:
            raw = selection_data.get_data()
            if raw:
                try:
                    text = raw.decode("utf-8", errors="replace")
                except Exception:
                    text = ""
                uris = [
                    line.strip()
                    for line in text.splitlines()
                    if line.strip() and not line.startswith("#")
                ]
        paths: List[Path] = []
        for uri in uris:
            try:
                local_path = Gio.File.new_for_uri(uri).get_path()
            except Exception:
                local_path = None
            if local_path:
                candidate = Path(local_path)
                if candidate.exists():
                    paths.append(candidate)
        if not paths:
            self.status_label.set_text(tr("Trascinamento non valido: usa file o cartelle locali."))
            Gtk.drag_finish(context, False, False, time_value)
            self._pending_drop_position = None
            return
        drop_x, drop_y = self._pending_drop_position or (x, y)
        self._pending_drop_position = None
        destination = self._destination_at_position(drop_x, drop_y)
        self._upload_paths(paths, destination)
        Gtk.drag_finish(context, True, False, time_value)

    def on_up_clicked(self, *_args) -> None:
        current = self.path_label.get_text() or self.home_path
        parent = posixpath.dirname(current.rstrip("/")) or "/"
        self.load_root(parent)

    def on_row_activated(self, tree: Gtk.TreeView, path: Gtk.TreePath, _column: Gtk.TreeViewColumn) -> None:
        tree_iter = self.store.get_iter(path)
        kind = self.store[tree_iter][self.COL_KIND]
        if kind == "placeholder":
            return
        if kind != "directory":
            # Doppio click su un file: scarica.
            self.on_download_clicked()
            return
        # Doppio click su una cartella: apri se chiusa, chiudi se aperta.
        # La barra del percorso e il pulsante "Su" seguono sempre la cartella.
        if tree.row_expanded(path):
            tree.collapse_row(path)
        else:
            tree.expand_row(path, False)
        self._sync_location_to_iter(tree_iter)

    def expand_to_directory(self, tree_iter: Gtk.TreeIter) -> None:
        """Apre una cartella nell'albero senza distruggere il resto."""
        tree_path = self.store.get_path(tree_iter)
        if not self.tree.row_expanded(tree_path):
            self.tree.expand_row(tree_path, False)
        self.tree.get_selection().select_iter(tree_iter)
        self.tree.scroll_to_cell(tree_path, None, False, 0.0, 0.0)
        self._sync_location_to_iter(tree_iter)

    def _sync_location_to_iter(self, tree_iter: Gtk.TreeIter) -> None:
        kind = self.store[tree_iter][self.COL_KIND]
        path = self.store[tree_iter][self.COL_PATH]
        if kind == "directory" and path:
            self._set_current_path(path)

    def on_row_expanded(self, _tree: Gtk.TreeView, tree_iter: Gtk.TreeIter, _path: Gtk.TreePath) -> None:
        if self.store[tree_iter][self.COL_KIND] != "directory":
            return
        # Se la cartella è già stata caricata o è in corso, non rilanciare la
        # lettura: senza questo controllo un secondo expand (anche indiretto,
        # via expand_to_directory) riavviava un worker e duplicava i figli.
        if self.store[tree_iter][self.COL_LOADED] or self.store[tree_iter][self.COL_LOADING]:
            return
        remote_path = self.store[tree_iter][self.COL_PATH]
        if not remote_path:
            return
        self.store[tree_iter][self.COL_LOADING] = True
        generation = self._generation
        client = self.client
        if client is None:
            self.store[tree_iter][self.COL_LOADING] = False
            return
        row_reference = Gtk.TreeRowReference.new(self.store, self.store.get_path(tree_iter))

        def worker() -> None:
            try:
                entries = client.listdir(remote_path)
                GLib.idle_add(self._apply_children, generation, row_reference, entries, "")
            except Exception as exc:
                GLib.idle_add(self._apply_children, generation, row_reference, [], str(exc))

        threading.Thread(target=worker, name="fedtermhub-sftp-list", daemon=True).start()

    def _apply_children(
        self,
        generation: int,
        row_reference: Gtk.TreeRowReference,
        entries: List[RemoteEntry],
        error: str,
    ) -> bool:
        if generation != self._generation or not row_reference.valid():
            return False
        path = row_reference.get_path()
        tree_iter = self.store.get_iter(path)
        # Rimuove placeholder e figli precedenti in modo sicuro.
        child = self.store.iter_children(tree_iter)
        while child is not None:
            if not self.store.remove(child):
                child = None
            else:
                # store.remove sposta l'iteratore al fratello successivo e
                # ritorna True finché resta valido: non serve rileggerlo.
                child = child if self.store.iter_is_valid(child) else self.store.iter_children(tree_iter)
        if error:
            self.store.append(
                tree_iter,
                [tr("Errore di lettura"), "", "placeholder", friendly_sftp_error(error), "dialog-error-symbolic", True, False],
            )
            # Non segniamo la cartella come caricata: così "Aggiorna" può
            # ritentare la lettura invece di mostrare per sempre l'errore.
            self.store[tree_iter][self.COL_LOADED] = False
            self.store[tree_iter][self.COL_LOADING] = False
            self.status_label.set_text(friendly_sftp_error(error))
            return False
        if not entries:
            # Cartella vuota: un segnaposto esplicito evita il triangolo di
            # espansione "morto" e comunica lo stato.
            self.store.append(
                tree_iter,
                [tr("(vuota)"), "", "placeholder", tr("Nessun elemento"), "folder-symbolic", True, False],
            )
        for entry in entries:
            self._append_entry(tree_iter, entry)
        self.store[tree_iter][self.COL_LOADED] = True
        self.store[tree_iter][self.COL_LOADING] = False
        # len(entries) e' quanto ha letto il server; con un filtro attivo e'
        # diverso da quanto si vede, e dichiararlo e' l'unico modo perche'
        # l'utente capisca che l'elenco e' parziale invece di credere che una
        # cartella sia vuota.
        self.status_label.set_text(self._count_text(entries, self.store[tree_iter][self.COL_PATH]))
        return False

    def _popup_item(self, menu: Gtk.Menu, label: str, callback: Callable[[], None], sensitive: bool = True) -> None:
        item = Gtk.MenuItem(label=label)
        item.set_sensitive(sensitive)
        item.connect("activate", lambda *_: callback())
        menu.append(item)

    def on_button_press(self, tree: Gtk.TreeView, event) -> bool:  # noqa: ANN001
        if event.button != 3:
            return False
        hit = tree.get_path_at_pos(int(event.x), int(event.y))
        if hit is not None:
            path, _column, _cell_x, _cell_y = hit
            tree.get_selection().select_path(path)
        entry = self.selected_entry()
        model, tree_iter = self.tree.get_selection().get_selected()
        menu = Gtk.Menu()
        if entry is not None:
            if entry.kind == "directory":
                if tree_iter is not None:
                    self._popup_item(menu, tr("Apri"), lambda it=tree_iter: self.expand_to_directory(it))
                self._popup_item(menu, tr("Entra (mostra solo questa)"), lambda: self.load_root(entry.path))
                menu.append(Gtk.SeparatorMenuItem())
                self._popup_item(menu, tr("Scarica cartella…"), self.on_download_clicked)
                self._popup_item(menu, tr("Carica qui…"), self.on_upload_clicked)
            else:
                self._popup_item(menu, tr("Scarica file…"), self.on_download_clicked)
            self._popup_item(menu, tr("Copia percorso"), lambda: self.copy_path(entry.path))
            menu.append(Gtk.SeparatorMenuItem())
        self._popup_item(menu, tr("Aggiorna"), self.refresh, sensitive=self.client is not None)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def copy_path(self, path: str) -> None:
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(path, -1)
        self.status_label.set_text(tr("Percorso remoto copiato."))

    def on_download_clicked(self, *_args) -> None:
        entry = self.selected_entry()
        if entry is None or self.client is None:
            return
        is_directory = entry.kind == "directory"
        dialog = Gtk.FileChooserDialog(
            title=(tr("Scarica cartella remota") if is_directory else tr("Scarica file remoto")),
            transient_for=self.parent_window,
            action=(Gtk.FileChooserAction.SELECT_FOLDER if is_directory else Gtk.FileChooserAction.SAVE),
        )
        dialog.add_buttons(tr("Annulla"), Gtk.ResponseType.CANCEL, tr("Scarica"), Gtk.ResponseType.OK)
        if not is_directory:
            dialog.set_current_name(entry.name)
            dialog.set_do_overwrite_confirmation(True)
        try:
            if dialog.run() != Gtk.ResponseType.OK:
                return
            filename = dialog.get_filename()
        finally:
            dialog.destroy()
        if not filename:
            return
        destination = Path(filename)
        if is_directory:
            destination = destination / entry.name
            if destination.exists():
                self.status_label.set_text(tr("La destinazione esiste già: {destination}").format(destination=destination))
                return
        self.status_label.set_text(tr("Download di {name}…").format(name=entry.name))
        generation = self._generation
        client = self.client

        def worker() -> None:
            try:
                client.download(entry.path, destination, recursive=is_directory)
                GLib.idle_add(self._operation_done, generation, tr("Scaricato: {name}").format(name=destination.name), "")
            except Exception as exc:
                GLib.idle_add(self._operation_done, generation, "", str(exc))

        threading.Thread(target=worker, name="fedtermhub-sftp-download", daemon=True).start()

    def on_upload_clicked(self, *_args) -> None:
        if self.client is None:
            return
        destination = self.selected_directory()
        dialog = Gtk.FileChooserDialog(
            title=tr("Carica in {destination}").format(destination=destination),
            transient_for=self.parent_window,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(tr("Annulla"), Gtk.ResponseType.CANCEL, tr("Carica"), Gtk.ResponseType.OK)
        dialog.set_select_multiple(True)
        try:
            if dialog.run() != Gtk.ResponseType.OK:
                return
            filenames = dialog.get_filenames()
        finally:
            dialog.destroy()
        paths = [Path(item) for item in filenames if item]
        if paths:
            self._upload_paths(paths, destination)

    def _upload_paths(self, paths: Sequence[Path], destination: str) -> None:
        if self.client is None:
            return
        valid_paths = [Path(item) for item in paths if Path(item).exists()]
        if not valid_paths:
            self.status_label.set_text(tr("Nessun file locale valido da caricare."))
            return
        self.status_label.set_text(
            tr("Caricamento di {n_valid_paths} elemento/i in {destination}…").format(n_valid_paths=len(valid_paths), destination=destination)
        )
        generation = self._generation
        client = self.client

        def worker() -> None:
            try:
                for local_path in valid_paths:
                    client.upload(local_path, destination)
                GLib.idle_add(
                    self._operation_done,
                    generation,
                    tr("Caricamento completato: {n_valid_paths} elemento/i").format(n_valid_paths=len(valid_paths)),
                    "",
                    destination,
                )
            except Exception as exc:
                GLib.idle_add(self._operation_done, generation, "", str(exc))

        threading.Thread(target=worker, name="fedtermhub-sftp-upload", daemon=True).start()

    def _operation_done(
        self,
        generation: int,
        message: str,
        error: str,
        refresh_path: str = "",
    ) -> bool:
        if generation != self._generation:
            return False
        self.status_label.set_text(tr("Operazione SFTP fallita: {friendly_sftp_error}").format(friendly_sftp_error=friendly_sftp_error(error)) if error else message)
        if refresh_path and not error:
            # Dopo un caricamento si ricarica la vista corrente, senza saltare nella cartella di
            # destinazione (che con il trascinamento e' spesso una sottocartella).
            self.refresh()
        return False
