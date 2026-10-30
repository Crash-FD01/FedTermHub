# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native libfreerdp framebuffer tab for FedTermHub 2.x.

The helper process links to libfreerdp3 and writes decoded BGRA frames into a
shared memfd. GTK draws that framebuffer directly, so there is no Gtk.Socket,
XEmbed, X11 reparenting or xfreerdp child window.
"""
from __future__ import annotations

import fcntl
import mmap
import os
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple
try:
    from .i18n import tr, trn
    from . import distro
except ImportError:  # avvio come script (fedtermhub/fedtermhub.py) o dai test
    from i18n import tr, trn
    import distro  # type: ignore[no-redef]

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

try:
    import cairo
except Exception:  # pragma: no cover
    cairo = None

MAGIC = 0x46544852
HEADER_SIZE = 1024
HEADER_WORDS = 16
STATE_IDLE = 0
STATE_CONNECTING = 1
STATE_CONNECTED = 2
STATE_DISCONNECTED = 3
STATE_ERROR = 4
STATE_STOPPING = 5

CMD_STOP = 1
CMD_MOUSE_MOVE = 2
CMD_MOUSE_BUTTON = 3
CMD_MOUSE_WHEEL = 4
CMD_KEY_XKB = 5
CMD_UNICODE = 6
CMD_FOCUS = 7
CMD_REFRESH = 8
CMD_RESIZE = 9

# Maschera toggleStates del Client Synchronize Event (MS-RDPBCGR 2.2.8.1.1.3.1.1.5),
# passata come primo argomento di CMD_FOCUS e inoltrata dall'helper a
# freerdp_input_send_focus_in_event(). Dichiara al server lo stato REALE dei
# tasti di blocco locali: mandare 0 (come faceva la 0.9.05.4) spegne Bloc Num e
# Bloc Maiusc nella sessione remota a ogni ingresso nella scheda, e da lì i due
# lati divergono a ogni pressione successiva.
KBD_SYNC_SCROLL_LOCK = 0x01
KBD_SYNC_NUM_LOCK = 0x02
KBD_SYNC_CAPS_LOCK = 0x04
KBD_SYNC_KANA_LOCK = 0x08


def native_helper_path() -> Path:
    env = os.environ.get("FEDTERMHUB_RDP_NATIVE_HELPER")
    if env:
        return Path(env).expanduser()
    package_dir = Path(__file__).resolve().parent
    candidates = [
        package_dir.parent / "native" / "fedtermrdp-native",
        package_dir / "native" / "fedtermrdp-native",
        Path.home() / ".local/libexec/fedtermhub/fedtermrdp-native",
        Path("/usr/libexec/fedtermhub/fedtermrdp-native"),
        Path("/usr/lib/fedtermhub/fedtermrdp-native"),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return candidates[0]


def native_backend_status() -> Tuple[bool, str]:
    helper = native_helper_path()
    if cairo is None:
        return False, tr("python3-cairo non disponibile")
    if not helper.is_file():
        return False, tr("helper non trovato: {helper}").format(helper=helper)
    if not os.access(helper, os.X_OK):
        return False, tr("helper non eseguibile: {helper}").format(helper=helper)
    if not hasattr(os, "memfd_create"):
        return False, tr("memfd_create non disponibile nel sistema")
    return True, str(helper)


# --- Clipboard bridge (bidirezionale host <-> sessione RDP) -----------------
# Mailbox in memoria condivisa separata dal frame buffer, letta/scritta sia da
# questo processo (clipboard GTK) sia dall'helper C (canale cliprdr). Layout
# concordato con FthClipHeader in native/fedtermrdp_native.c — se cambia uno
# dei due lati, va aggiornato anche l'altro:
#   offset  0 uint32 magic
#   offset  4 uint32 version
#   offset  8 uint32 host_seq       (Python scrive; seqlock dispari/pari)
#   offset 12 uint32 host_available
#   offset 16 uint32 host_len
#   offset 20 uint32 remote_seq     (helper C scrive; seqlock dispari/pari)
#   offset 24 uint32 remote_len
#   offset 28 uint32 capacity
#   offset 32 uint32 host_kind      (0 = testo UTF-8, 1 = text/uri-list di file)
#   offset 36 uint32 remote_kind    (0 = testo UTF-8, 1 = text/uri-list di file)
#   offset 40..63    riservato
#   [CLIP_CAPACITY byte]  host_data   Python -> helper -> server RDP
#   [CLIP_CAPACITY byte]  remote_data server RDP -> helper -> Python
CLIP_MAGIC = 0x46544843
CLIP_VERSION = 3
CLIP_CAPACITY = 256 * 1024
CLIP_HEADER_SIZE = 64
CLIP_MAILBOX_SIZE = CLIP_HEADER_SIZE + CLIP_CAPACITY * 2
CLIP_KIND_TEXT = 0
CLIP_KIND_URILIST = 1


class ClipboardBridge:
    """Sincronizza la clipboard GTK dell'host con il canale cliprdr gestito
    dall'helper C nativo, in entrambe le direzioni.

    Verso il server viaggiano testo (copia/incolla normale) e liste di file
    locali trascinate sul canvas (write_host_uri_list); dal server arriva
    solo testo. Le immagini restano fuori dal protocollo per ora.
    """

    def __init__(
        self,
        apply_remote_text: "Callable[[str], None]",
        apply_remote_files: "Optional[Callable[[List[str]], None]]" = None,
    ) -> None:
        self.apply_remote_text = apply_remote_text
        # Chiamata quando la sessione remota copia dei FILE e l'helper è
        # riuscito a mapparli su percorsi locali leggibili.
        self.apply_remote_files = apply_remote_files
        self.fd: Optional[int] = None
        self.map: Optional[mmap.mmap] = None
        self._last_remote_seq = 0
        self._owner_change_handler: Optional[int] = None
        # Ultimo testo che ABBIAMO impostato noi sulla clipboard host (ricevuto
        # dal server). Serve a distinguere una copia fatta dall'utente da un
        # rimbalzo del testo che abbiamo appena scritto noi: confrontare il
        # contenuto è robusto, mentre un flag "sopprimi il prossimo evento"
        # fallisce se set_text non genera owner-change (dipende dal
        # compositor) o se l'utente ricopia lo stesso testo. None = nessun
        # testo di provenienza remota in sospeso.
        self._last_pushed_to_host: Optional[str] = None

    def create_mailbox(self) -> Optional[int]:
        try:
            flags = getattr(os, "MFD_CLOEXEC", 0)
            fd = os.memfd_create("fedtermhub-rdp-clip", flags=flags)
            os.ftruncate(fd, CLIP_MAILBOX_SIZE)
            mapping = mmap.mmap(fd, CLIP_MAILBOX_SIZE, access=mmap.ACCESS_WRITE)
            struct.pack_into(
                "<16I", mapping, 0,
                CLIP_MAGIC, CLIP_VERSION,
                0, 0, 0,   # host_seq, host_available, host_len
                0, 0,      # remote_seq, remote_len
                CLIP_CAPACITY,
                0, 0, 0, 0, 0, 0, 0, 0,
            )
            self.fd = fd
            self.map = mapping
            return fd
        except Exception:
            self.close()
            return None

    def start(self) -> None:
        """Aggancia 'owner-change' della clipboard GTK e pubblica subito il
        contenuto corrente: una sessione appena connessa può quindi ricevere
        un incolla dall'host senza dover aspettare una nuova copia."""
        if self.map is None:
            return
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        try:
            self._owner_change_handler = clipboard.connect("owner-change", self._on_owner_change)
        except Exception:
            self._owner_change_handler = None
        self._refresh_host_text(clipboard)

    def _on_owner_change(self, clipboard: Gtk.Clipboard, _event) -> None:  # noqa: ANN001
        self._refresh_host_text(clipboard)

    def _refresh_host_text(self, clipboard: Gtk.Clipboard) -> None:
        def received(_cb: Gtk.Clipboard, text: Optional[str], _data=None) -> None:
            # Se questo è esattamente il testo che abbiamo appena ricevuto dal
            # server e messo noi sulla clipboard, non rimandarlo indietro:
            # sarebbe un'eco. Qualsiasi altro testo (inclusa una copia locale
            # dell'utente) va invece propagato verso la sessione RDP.
            if text is not None and text == self._last_pushed_to_host:
                return
            self._last_pushed_to_host = None
            self._write_host_text(text)

        try:
            clipboard.request_text(received, None)
        except Exception:
            pass

    def _write_host_text(self, text: Optional[str]) -> None:
        payload = (text or "").encode("utf-8")
        if len(payload) > CLIP_CAPACITY:
            # Tronca senza spezzare un carattere UTF-8 multibyte a metà:
            # decodifica ignorando i byte finali incompleti e ricodifica.
            payload = payload[:CLIP_CAPACITY].decode("utf-8", errors="ignore").encode("utf-8")
        self._write_host_payload(payload, 1 if text else 0, CLIP_KIND_TEXT)

    def write_host_uri_list(self, uris: "List[str]") -> bool:
        """Pubblica una text/uri-list di file locali (drag&drop sul canvas):
        l'helper la annuncia al server come FileGroupDescriptorW e serve i
        byte via CB_FILECONTENTS quando l'utente incolla in Explorer."""
        if self.map is None or not uris:
            return False
        payload = ("\r\n".join(uris) + "\r\n").encode("utf-8")
        if len(payload) > CLIP_CAPACITY:
            # Una uri-list troncata annuncerebbe file a metà: meglio rifiutare.
            return False
        self._write_host_payload(payload, 1, CLIP_KIND_URILIST)
        return True

    def _write_host_payload(self, payload: bytes, available: int, kind: int) -> None:
        if self.map is None:
            return
        current_seq = struct.unpack_from("<I", self.map, 8)[0]
        writing_seq = current_seq + 1  # dispari: chi legge sa di dover riprovare
        struct.pack_into("<I", self.map, 8, writing_seq)
        if payload:
            self.map[CLIP_HEADER_SIZE:CLIP_HEADER_SIZE + len(payload)] = payload
        struct.pack_into(tr("<II"), self.map, 12, available, len(payload))
        struct.pack_into("<I", self.map, 32, kind)
        struct.pack_into("<I", self.map, 8, writing_seq + 1)  # pari: dato valido

    def poll_remote(self) -> None:
        """Da chiamare a ogni tick del poll del frame: se il server ha
        pubblicato qualcosa di nuovo (copiato dentro la sessione remota) lo
        rende disponibile sulla clipboard GTK dell'host: testo semplice, o una
        lista di file quando l'helper è riuscito a mapparli localmente."""
        if self.map is None:
            return
        seq = struct.unpack_from("<I", self.map, 20)[0]
        if seq % 2 != 0 or seq == self._last_remote_seq:
            return
        length = min(struct.unpack_from("<I", self.map, 24)[0], CLIP_CAPACITY)
        kind = struct.unpack_from("<I", self.map, 36)[0]
        start = CLIP_HEADER_SIZE + CLIP_CAPACITY
        payload = bytes(self.map[start:start + length])
        seq_after = struct.unpack_from("<I", self.map, 20)[0]
        if seq_after != seq:
            return  # l'helper stava scrivendo in questo istante; si riprova al prossimo tick
        self._last_remote_seq = seq
        if not payload:
            return
        text = payload.decode("utf-8", errors="replace")
        if kind == CLIP_KIND_URILIST:
            uris = [line.strip() for line in text.splitlines() if line.strip()]
            if uris and self.apply_remote_files is not None:
                self.apply_remote_files(uris)
            return
        self._last_pushed_to_host = text
        self.apply_remote_text(text)

    def close(self) -> None:
        if self._owner_change_handler is not None:
            try:
                Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).disconnect(self._owner_change_handler)
            except Exception:
                pass
            self._owner_change_handler = None
        if self.map is not None:
            try:
                self.map.close()
            except Exception:
                pass
            self.map = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None


class NativeRdpTab(Gtk.Box):
    """RDP rendered by libfreerdp directly into a GTK DrawingArea."""

    def __init__(
        self,
        title: str,
        options: Sequence[str],
        password: Optional[str],
        theme: str = "dark",
        state_callback: Optional[Callable[[str], None]] = None,
        session_manager_callback: Optional[Callable[[], None]] = None,
        fullscreen_callback: Optional[Callable[[bool], None]] = None,
        close_callback: Optional[Callable[[], None]] = None,
        detach_callback: Optional[Callable[[bool], None]] = None,
        preferred_monitor: str = "current",
        autostart_on_map: bool = True,
        max_width: int = 4096,
        max_height: int = 2160,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("rdp-session-surface")
        self.title = title
        self.base_options = list(options)
        self.password = password
        self.theme = theme
        self.state_callback = state_callback
        self.session_manager_callback = session_manager_callback
        self.fullscreen_callback = fullscreen_callback
        self.close_callback = close_callback
        self.detach_callback = detach_callback
        self.preferred_monitor = str(preferred_monitor or "current")
        self.autostart_on_map = bool(autostart_on_map)
        self.max_width = max(1920, min(int(max_width), 7680))
        self.max_height = max(1080, min(int(max_height), 4320))

        self.process: Optional[subprocess.Popen[str]] = None
        self.frame_fd: Optional[int] = None
        self.config_fd: Optional[int] = None
        self.command_write_fd: Optional[int] = None
        self.frame_map: Optional[mmap.mmap] = None
        self.frame_capacity = self.max_width * self.max_height * 4
        self.poll_id: Optional[int] = None
        self.start_id: Optional[int] = None
        self.destroyed = False
        self.started = False
        self.last_seq = -1
        self.last_state = STATE_IDLE
        self.last_event_seq = 0
        self.last_dimensions = (0, 0, 0)
        self.pressed_keys = set()
        self.frame_data: Optional[bytearray] = None
        self._staging: Optional[bytearray] = None
        self.surface = None
        self.logs: List[str] = []
        self.reader_threads: List[threading.Thread] = []
        self.scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.remote_width = 0
        self.remote_height = 0
        self._last_motion = 0.0
        self.fullscreen_active = False
        self.auto_resize = any(o in {"/dynamic-resolution", "+dynamic-resolution"} for o in self.base_options)
        self.resize_id: Optional[int] = None
        self.fullscreen_bar_hide_id: Optional[int] = None
        self._pointer_on_bar = False
        # Posizione libera della barra fullscreen. None = ancorata in alto al
        # centro (comportamento predefinito); una coppia (x, y) in pixel
        # relativi all'overlay attiva il posizionamento manuale trascinato.
        self._fs_bar_pos: Optional[Tuple[float, float]] = None
        self._fs_bar_dragging = False
        self._fs_bar_drag_dx = 0.0
        self._fs_bar_drag_dy = 0.0
        self._fs_bar_moved = False
        self._drop_toast_hide_id: Optional[int] = None
        # Mantiene vive le callback che servono gli appunti "pigri" di GTK
        # finché siamo proprietari della selezione (vedi _apply_remote_files).
        self._clipboard_owner_refs: Optional[tuple] = None
        self.last_resize_request = (0, 0)
        self.last_resize_sent_at = 0.0
        self.detach_buttons: List[Gtk.Widget] = []

        # --- Clipboard bridge (bidirezionale host <-> sessione RDP) ---------
        # Attivo solo se /clipboard è tra le opzioni (checkbox "Clipboard"
        # nelle impostazioni della sessione). Vedi ClipboardBridge più sotto
        # per il protocollo dettagliato condiviso con l'helper C.
        self.clipboard_enabled = "/clipboard" in self.base_options
        self.clip_bridge: Optional[ClipboardBridge] = None

        self._build_ui()
        self.connect("destroy", self._on_destroy)
        self.connect("map", self._on_map)

    def _build_ui(self) -> None:
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.toolbar = toolbar
        toolbar.get_style_context().add_class("rdp-toolbar")
        self.state_dot = Gtk.Label(label="●")
        self.state_dot.get_style_context().add_class("status-dot-pending")
        toolbar.pack_start(self.state_dot, False, False, 0)
        self.spinner = Gtk.Spinner()
        toolbar.pack_start(self.spinner, False, False, 0)
        self.state_label = Gtk.Label(label=tr("Motore RDP nativo pronto"), xalign=0)
        self.state_label.set_ellipsize(Pango.EllipsizeMode.END)
        toolbar.pack_start(self.state_label, True, True, 0)

        actions = [
            (tr("Schermo intero"), "view-fullscreen-symbolic", self._on_fullscreen),
        ]
        if self.detach_callback is not None:
            actions.append((tr("Sgancia"), "window-new-symbolic", self._on_detach))
        actions.extend([
            (tr("Gestisci sessioni"), "system-users-symbolic", self._on_manage_sessions),
            (tr("Invia Ctrl+Alt+Canc"), "input-keyboard-symbolic", self._on_ctrl_alt_del),
            (tr("Aggiorna"), "view-refresh-symbolic", self._on_refresh),
            (tr("Riconnetti"), "system-reboot-symbolic", self._on_reconnect),
            (tr("Ferma"), "media-playback-stop-symbolic", self._on_stop),
            (tr("Diagnostica"), "dialog-information-symbolic", self._on_diagnostics),
        ])
        for label, icon, callback in actions:
            button = Gtk.Button()
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.set_tooltip_text(label)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            row.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU), False, False, 0)
            row.pack_start(Gtk.Label(label=label), False, False, 0)
            button.add(row)
            button.get_style_context().add_class("mini-action")
            button.connect("clicked", callback)
            toolbar.pack_start(button, False, False, 0)
            if callback == self._on_detach:
                self.detach_buttons.append(button)

        self.log_toggle = Gtk.ToggleButton()
        self.log_toggle.set_relief(Gtk.ReliefStyle.NONE)
        self.log_toggle.set_tooltip_text(tr("Mostra log nativo"))
        self.log_toggle.add(Gtk.Image.new_from_icon_name("text-x-generic-symbolic", Gtk.IconSize.MENU))
        self.log_toggle.connect("toggled", self._on_log_toggle)
        toolbar.pack_start(self.log_toggle, False, False, 0)
        self.pack_start(toolbar, False, False, 0)

        self.overlay = Gtk.Overlay()
        self.overlay.set_hexpand(True)
        self.overlay.set_vexpand(True)
        self.pack_start(self.overlay, True, True, 0)

        self.canvas = Gtk.DrawingArea()
        self.canvas.set_can_focus(True)
        self.canvas.get_style_context().add_class("native-rdp-canvas")
        self.canvas.set_hexpand(True)
        self.canvas.set_vexpand(True)
        self.canvas.add_events(
            Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
            | Gdk.EventMask.KEY_RELEASE_MASK
            | Gdk.EventMask.FOCUS_CHANGE_MASK
        )
        self.canvas.connect("draw", self._on_draw)
        self.canvas.connect("motion-notify-event", self._on_motion)
        # Anti-flash: di norma GTK3 "pulisce" la GdkWindow del canvas con il
        # colore di sfondo (nero) a ogni riallocazione — per esempio durante
        # l'animazione della barra a scomparsa — un istante prima che il
        # nostro _on_draw ridisegni il frame. Il risultato è un lampo nero.
        # Azzerare il background pattern elimina la pulizia implicita: lo
        # schermo mostra l'ultimo frame finché non arriva il successivo.
        self.canvas.connect("realize", self._on_canvas_realize)
        self.canvas.connect("button-press-event", self._on_button_press)
        self.canvas.connect("button-release-event", self._on_button_release)
        self.canvas.connect("scroll-event", self._on_scroll)
        self.canvas.connect("key-press-event", self._on_key_press)
        self.canvas.connect("key-release-event", self._on_key_release)
        self.canvas.connect("focus-in-event", self._on_focus_in)
        self.canvas.connect("focus-out-event", self._on_focus_out)
        self.canvas.connect("size-allocate", self._on_canvas_size_allocate)
        # Drag&drop di file dal file manager. Il target va registrato SEMPRE:
        # se dipendesse dalla clipboard, con la casella "Clipboard" disattivata
        # il canvas non sarebbe nemmeno un bersaglio di rilascio e l'utente non
        # riceverebbe alcun riscontro (il file "rimbalza" e sembra tutto rotto).
        # Registrandolo comunque possiamo spiegare cosa manca e, soprattutto,
        # usare la cartella condivisa /drive: che è molto più affidabile del
        # trasferimento file via cliprdr.
        self.canvas.drag_dest_set(
            Gtk.DestDefaults.DROP | Gtk.DestDefaults.MOTION,
            [Gtk.TargetEntry.new("text/uri-list", 0, 0)],
            Gdk.DragAction.COPY,
        )
        self.canvas.connect("drag-data-received", self._on_canvas_drag_data_received)
        self.overlay.add(self.canvas)

        # Barra immersiva a scomparsa. Rimane dentro la tab e quindi funziona
        # nello stesso modo su Wayland e X11, senza finestre ausiliarie.
        self.fullscreen_bar_revealer = Gtk.Revealer()
        self.fullscreen_bar_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.fullscreen_bar_revealer.set_transition_duration(180)
        self.fullscreen_bar_revealer.set_halign(Gtk.Align.CENTER)
        self.fullscreen_bar_revealer.set_valign(Gtk.Align.START)
        self.fullscreen_bar_revealer.set_margin_top(8)
        self.fullscreen_bar_revealer.set_no_show_all(True)

        fullscreen_bar = Gtk.EventBox()
        fullscreen_bar.set_visible_window(False)
        fullscreen_bar.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        fullscreen_bar.connect("enter-notify-event", self._on_fullscreen_bar_enter)
        fullscreen_bar.connect("leave-notify-event", self._on_fullscreen_bar_leave)
        # Barra compatta: solo icone con tooltip (niente etichette testuali) e
        # una maniglia a sinistra per trascinarla liberamente in qualunque
        # punto dello schermo, così non copre mai l'area di lavoro remota.
        bar_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        bar_row.get_style_context().add_class("rdp-fullscreen-bar")
        bar_row.get_style_context().add_class("rdp-fullscreen-bar-compact")

        def icon_button(icon: str, tooltip: str, callback, destructive: bool = False) -> Gtk.Button:
            button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.set_tooltip_text(tooltip)
            if destructive:
                button.get_style_context().add_class("destructive-action")
            button.connect("clicked", callback)
            bar_row.pack_start(button, False, False, 0)
            return button

        # Maniglia di trascinamento: un EventBox con l'icona "griglia" che
        # cattura pressione/movimento/rilascio del tasto sinistro e riposiziona
        # la barra sull'overlay. Doppio clic ripristina la posizione ancorata.
        drag_handle = Gtk.EventBox()
        drag_handle.set_visible_window(False)
        drag_handle.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        handle_icon = Gtk.Image.new_from_icon_name("view-more-symbolic", Gtk.IconSize.MENU)
        handle_icon.get_style_context().add_class("dim-label")
        drag_handle.add(handle_icon)
        drag_handle.set_tooltip_text(
            tr("Trascina per spostare la barra ovunque. Doppio clic per riportarla in alto al centro.")
        )
        try:
            drag_handle.get_window()  # noop safe; cursor set on realize below
        except Exception:
            pass
        drag_handle.connect("realize", self._on_fs_handle_realize)
        drag_handle.connect("button-press-event", self._on_fs_bar_drag_begin)
        drag_handle.connect("motion-notify-event", self._on_fs_bar_drag_motion)
        drag_handle.connect("button-release-event", self._on_fs_bar_drag_end)
        bar_row.pack_start(drag_handle, False, False, 2)

        icon_button("view-refresh-symbolic", tr("Riconnetti la sessione"), self._on_reconnect)
        icon_button("zoom-fit-best-symbolic", tr("Adatta la risoluzione remota allo schermo"), self._on_fit_resolution)
        if self.detach_callback is not None:
            detach_button = icon_button("window-new-symbolic", tr("Sgancia in una finestra indipendente"), self._on_detach)
            self.detach_buttons.append(detach_button)
        icon_button("window-minimize-symbolic", tr("Riduci a icona"), self._on_minimize_window)
        icon_button("view-restore-symbolic", tr("Torna alla finestra principale"), self._on_exit_fullscreen)
        icon_button("media-playback-stop-symbolic", tr("Disconnetti la sessione"), self._on_stop)
        icon_button("window-close-symbolic", tr("Chiudi la scheda"), self._on_close_session, destructive=True)

        fullscreen_bar.add(bar_row)
        self.fullscreen_bar_revealer.add(fullscreen_bar)
        self.overlay.add_overlay(self.fullscreen_bar_revealer)
        # Con una posizione manuale attiva, l'overlay chiede a noi dove mettere
        # la barra: rispondiamo con il rettangolo calcolato in _fs_bar_pos.
        self.overlay.connect("get-child-position", self._on_overlay_child_position)

        # Toast di riscontro per il drag&drop: senza un messaggio a schermo
        # l'utente non ha modo di sapere se il rilascio è andato a buon fine
        # (il log è nascosto per default).
        self.drop_toast_revealer = Gtk.Revealer()
        self.drop_toast_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.drop_toast_revealer.set_transition_duration(160)
        self.drop_toast_revealer.set_halign(Gtk.Align.CENTER)
        self.drop_toast_revealer.set_valign(Gtk.Align.END)
        self.drop_toast_revealer.set_margin_bottom(18)
        self.drop_toast_revealer.set_no_show_all(True)
        self.drop_toast_label = Gtk.Label(label="")
        self.drop_toast_label.set_line_wrap(True)
        self.drop_toast_label.set_max_width_chars(60)
        self.drop_toast_label.set_justify(Gtk.Justification.CENTER)
        toast_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toast_box.get_style_context().add_class("rdp-status-card")
        toast_box.pack_start(self.drop_toast_label, True, True, 0)
        self.drop_toast_revealer.add(toast_box)
        self.overlay.add_overlay(self.drop_toast_revealer)

        self.status_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.status_card.set_halign(Gtk.Align.CENTER)
        self.status_card.set_valign(Gtk.Align.CENTER)
        self.status_card.set_size_request(430, -1)
        self.status_card.get_style_context().add_class("rdp-status-card")
        self.status_title = Gtk.Label(label=tr("Desktop remoto"))
        self.status_title.get_style_context().add_class("panel-title")
        self.status_message = Gtk.Label(label=tr("La connessione partirà appena la scheda è visibile."))
        self.status_message.set_line_wrap(True)
        self.status_message.set_justify(Gtk.Justification.CENTER)
        self.status_card.pack_start(self.status_title, False, False, 0)
        self.status_card.pack_start(self.status_message, False, False, 0)
        # Pulsante Riconnetti ben visibile: quando la sessione cade (per
        # esempio dopo il blocco/sospensione del PC) basta un click, senza
        # andare a cercare l'azione nella toolbar.
        self.status_reconnect = Gtk.Button.new_with_mnemonic(tr("  _Riconnetti  "))
        self.status_reconnect.get_style_context().add_class("suggested-action")
        self.status_reconnect.set_halign(Gtk.Align.CENTER)
        self.status_reconnect.set_no_show_all(True)
        self.status_reconnect.connect("clicked", self._on_reconnect)
        self.status_card.pack_start(self.status_reconnect, False, False, 6)
        self.overlay.add_overlay(self.status_card)

        self.session_notice = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.session_notice.set_no_show_all(True)
        self.session_notice.set_halign(Gtk.Align.FILL)
        self.session_notice.set_valign(Gtk.Align.END)
        self.session_notice.set_margin_start(12)
        self.session_notice.set_margin_end(12)
        self.session_notice.set_margin_bottom(12)
        self.session_notice.get_style_context().add_class("notification-banner")
        self.session_notice.get_style_context().add_class("notice-warning")
        self.session_notice_label = Gtk.Label(label="", xalign=0)
        self.session_notice_label.set_line_wrap(True)
        self.session_notice.pack_start(self.session_notice_label, True, True, 0)
        manage = Gtk.Button.new_with_mnemonic(tr("_Gestisci sessioni"))
        manage.connect("clicked", self._on_manage_sessions)
        self.session_notice.pack_start(manage, False, False, 0)
        dismiss = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        dismiss.set_relief(Gtk.ReliefStyle.NONE)
        dismiss.set_tooltip_text(tr("Chiudi questo avviso"))
        dismiss.connect("clicked", self._dismiss_session_notice)
        self.session_notice.pack_start(dismiss, False, False, 0)
        self.overlay.add_overlay(self.session_notice)
        self.session_notice.hide()

        self.log_revealer = Gtk.Revealer()
        self.log_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.log_revealer.set_transition_duration(140)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_size_request(-1, 190)
        log_scroll.add(self.log_view)
        self.log_revealer.add(log_scroll)
        self.pack_start(self.log_revealer, False, True, 0)

    def request_start(self) -> bool:
        if self.destroyed or self.started:
            return False
        if not self.get_mapped():
            self.start_id = GLib.timeout_add(100, self.request_start)
            return False
        self._start()
        return False

    def _on_map(self, _widget: Gtk.Widget) -> None:
        if self.autostart_on_map and not self.started and self.start_id is None:
            self.start_id = GLib.idle_add(self.request_start)

    def _build_options(self) -> List[str]:
        options = [o for o in self.base_options if not o.startswith("/parent-window")]
        if not any(o.startswith("/size:") for o in options):
            alloc = self.canvas.get_allocation()
            width = max(1024, min(self.max_width, alloc.width if alloc.width > 100 else 1600))
            height = max(720, min(self.max_height, alloc.height if alloc.height > 100 else 900))
            options.append(f"/size:{width}x{height}")
        if not any(o.startswith("/bpp:") for o in options):
            options.append("/bpp:32")
        if not any(o in {"/gdi:sw", "/gdi:hw"} for o in options):
            options.append("/gdi:sw")
        # The native helper owns the display-control channel. Keep dynamic
        # resolution enabled so fullscreen/window transitions request the exact
        # canvas size; Cairo scaling remains the fallback for older servers.
        # La pipeline GFX (codec progressivo) riduce banda e scatti in modo
        # sostanziale rispetto agli aggiornamenti bitmap legacy. Il rendering
        # resta nel framebuffer GDI software: il client common di FreeRDP
        # inizializza gdi_graphics_pipeline quando SoftwareGdi è attivo, quindi
        # copy_frame continua a ricevere frame completi e coerenti. L'utente
        # può forzare un codec diverso aggiungendo /gfx:... nel campo comando.
        if not any(o.startswith("/gfx") or o in {"+gfx", "-gfx"} for o in options):
            options.append("/gfx:progressive")
        if self.password:
            options.append(f"/p:{self.password}")
        # La config viene consegnata all'helper come "una opzione per riga":
        # un newline dentro password, utente o host permetterebbe di iniettare
        # opzioni xfreerdp arbitrarie (es. /drive:). Meglio fallire subito.
        for option in options:
            if "\n" in option or "\r" in option:
                raise ValueError(
                    tr("Un parametro RDP contiene un carattere di fine riga non consentito.")
                )
        return options

    def _start(self) -> None:
        ok, detail = native_backend_status()
        if not ok:
            self._set_state(STATE_ERROR, tr("Backend RDP nativo non disponibile"), detail)
            return
        self.stop(silent=True)
        self.started = True
        self.last_seq = -1
        self.last_event_seq = 0
        self.frame_data = None
        self._staging = None
        self.last_dimensions = (0, 0, 0)
        self.surface = None
        self._set_state(STATE_CONNECTING, tr("Connessione RDP in corso"), tr("Avvio libfreerdp…"))

        try:
            helper = native_helper_path()
            self.frame_fd = os.memfd_create("fedtermhub-rdp-frame", flags=0)
            os.ftruncate(self.frame_fd, HEADER_SIZE + self.frame_capacity)
            self.frame_map = mmap.mmap(self.frame_fd, HEADER_SIZE + self.frame_capacity, access=mmap.ACCESS_WRITE)

            options = self._build_options()
            payload = ("\n".join(options) + "\n").encode("utf-8")
            memfd_flags = getattr(os, "MFD_CLOEXEC", 0) | getattr(os, "MFD_ALLOW_SEALING", 0)
            self.config_fd = os.memfd_create("fedtermhub-rdp-config", flags=memfd_flags)
            os.write(self.config_fd, payload)
            os.lseek(self.config_fd, 0, os.SEEK_SET)
            # The helper only needs to read the configuration. Sealing prevents
            # accidental or malicious modification after launch.
            try:
                seals = (
                    getattr(fcntl, "F_SEAL_WRITE", 0)
                    | getattr(fcntl, "F_SEAL_GROW", 0)
                    | getattr(fcntl, "F_SEAL_SHRINK", 0)
                    | getattr(fcntl, "F_SEAL_SEAL", 0)
                )
                if seals and hasattr(fcntl, "F_ADD_SEALS"):
                    fcntl.fcntl(self.config_fd, fcntl.F_ADD_SEALS, seals)
            except OSError:
                pass

            command_read, command_write = os.pipe()
            self.command_write_fd = command_write
            argv = [
                str(helper),
                "--config-fd", str(self.config_fd),
                "--frame-fd", str(self.frame_fd),
                "--command-fd", str(command_read),
            ]
            pass_fds = [self.config_fd, self.frame_fd, command_read]
            if self.clipboard_enabled:
                bridge = ClipboardBridge(self._apply_remote_clipboard_text, self._apply_remote_files)
                clip_fd = bridge.create_mailbox()
                if clip_fd is not None:
                    self.clip_bridge = bridge
                    argv += ["--clip-fd", str(clip_fd)]
                    pass_fds.append(clip_fd)
                else:
                    self._append_log(tr("[FedTermHub] Clipboard: impossibile creare la mailbox condivisa, disabilitata per questa sessione.\n"))
            try:
                self.process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    close_fds=True,
                    pass_fds=tuple(pass_fds),
                    start_new_session=True,
                    env={**os.environ, "LC_ALL": "C"},
                )
            finally:
                # Il lato read appartiene solo al processo figlio: va chiuso nel
                # padre anche quando Popen fallisce, altrimenti il descrittore
                # resta aperto per tutta la vita dell'applicazione.
                os.close(command_read)
            os.close(self.config_fd)
            self.config_fd = None
            self._start_log_threads()
            # 16 ms ≈ 60 Hz: dimezza la latenza di presentazione rispetto ai
            # 30 ms precedenti senza reintrodurre le code asincrone della 3.15.
            self.poll_id = GLib.timeout_add(16, self._poll_frame)
            self.canvas.grab_focus()
            if self.clip_bridge is not None:
                self.clip_bridge.start()
            self._append_log(tr("[FedTermHub] Backend nativo avviato; nessuna finestra X11 incorporata.\n"))
        except Exception as exc:
            self._append_log(tr("[FedTermHub] Errore avvio backend nativo: {exc}\n").format(exc=exc))
            self._set_state(STATE_ERROR, tr("Avvio RDP fallito"), str(exc))
            self.stop(silent=True)

    def _start_log_threads(self) -> None:
        if self.process is None:
            return
        for stream, prefix in ((self.process.stdout, "OUT"), (self.process.stderr, "ERR")):
            if stream is None:
                continue
            thread = threading.Thread(target=self._log_worker, args=(stream, prefix), daemon=True)
            self.reader_threads.append(thread)
            thread.start()

    def _log_worker(self, stream, prefix: str) -> None:  # noqa: ANN001
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                GLib.idle_add(self._append_log, f"[{prefix}] {line}")
        except Exception as exc:
            GLib.idle_add(self._append_log, tr("[LOG] {exc}\n").format(exc=exc))

    def _read_header(self) -> Optional[Tuple[int, ...]]:
        if self.frame_map is None:
            return None
        try:
            values = struct.unpack_from("<16I", self.frame_map, 0)
            if values[0] != MAGIC:
                return None
            return values
        except Exception:
            return None

    def _clipboard_status_text(self) -> str:
        if not self.clipboard_enabled:
            return tr("disattivata (checkbox Clipboard non spuntata)")
        if self.clip_bridge is None or self.clip_bridge.map is None:
            return tr("attivata ma mailbox non inizializzata")
        return tr("attiva (testo, bidirezionale)")

    def _apply_remote_clipboard_text(self, text: str) -> None:
        try:
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text(text, -1)
            clipboard.store()
        except Exception as exc:
            self._append_log(tr("[FedTermHub] Clipboard: impossibile impostare la clipboard host: {exc}\n").format(exc=exc))

    def _shared_drive_path(self) -> Optional[Path]:
        """Percorso della cartella condivisa /drive: della sessione, se c'è.

        FreeRDP la riceve come "/drive:fedtermhub,/percorso/locale": è la via
        più affidabile per portare file dentro la sessione, perché non dipende
        dalla negoziazione del trasferimento file su cliprdr (che molti server
        e molte policy disabilitano).
        """
        for option in self.base_options:
            if option.startswith("/drive:"):
                _, _, spec = option.partition(":")
                _, _, path = spec.partition(",")
                path = path.strip()
                if path:
                    candidate = Path(path).expanduser()
                    if candidate.is_dir():
                        return candidate
        return None

    def _show_drop_toast(self, message: str, seconds: int = 7) -> None:
        """Mostra un riscontro a schermo per qualche secondo."""
        try:
            self.drop_toast_label.set_text(message)
            self.drop_toast_revealer.set_no_show_all(False)
            self.drop_toast_revealer.show_all()
            self.drop_toast_revealer.set_reveal_child(True)
            if self._drop_toast_hide_id is not None:
                GLib.source_remove(self._drop_toast_hide_id)
            self._drop_toast_hide_id = GLib.timeout_add_seconds(
                max(2, seconds), self._hide_drop_toast
            )
        except Exception:
            pass

    def _hide_drop_toast(self) -> bool:
        self._drop_toast_hide_id = None
        try:
            self.drop_toast_revealer.set_reveal_child(False)
        except Exception:
            pass
        return False

    def _copy_files_to_shared_drive(self, paths: List[Path], target: Path) -> Tuple[int, List[str]]:
        """Copia i file rilasciati nella cartella condivisa, senza sovrascrivere.

        In caso di nome già presente aggiunge un suffisso progressivo: un drop
        non deve mai distruggere silenziosamente un file esistente.
        """
        copied = 0
        errors: List[str] = []
        for source in paths:
            try:
                destination = target / source.name
                stem, suffix = destination.stem, destination.suffix
                counter = 1
                while destination.exists():
                    destination = target / f"{stem} ({counter}){suffix}"
                    counter += 1
                if source.is_dir():
                    shutil.copytree(source, destination)
                else:
                    shutil.copy2(source, destination)
                copied += 1
            except Exception as exc:
                errors.append(f"{source.name}: {exc}")
        return copied, errors

    def _publish_uris_external(self, uris: List[str]) -> Optional[str]:
        """Mette una lista di file negli appunti tramite wl-copy o xclip.

        Serve perché Gtk.Clipboard.set_with_data non è utilizzabile da
        PyGObject: le callback che GTK userebbe per servire i dati non
        attraversano l'introspezione, quindi il client non riesce a diventare
        proprietario della selezione e l'incolla restituisce il contenuto
        precedente. wl-copy e xclip invece si biforcano e restano loro
        proprietari della selezione, servendo il tipo MIME richiesto.

        Restituisce il nome dello strumento usato, o None se nessuno è
        disponibile.
        """
        # x-special/gnome-copied-files è il tipo che Nautilus/Files richiede
        # per un incolla di file; la prima riga indica copia o taglio.
        payload = ("copy\n" + "\n".join(uris) + "\n").encode("utf-8")
        candidates: List[Tuple[str, List[str]]] = []
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
            candidates.append(("wl-copy", ["wl-copy", "--type", "x-special/gnome-copied-files"]))
        if os.environ.get("DISPLAY") and shutil.which("xclip"):
            candidates.append(
                ("xclip", ["xclip", "-selection", "clipboard", "-t", "x-special/gnome-copied-files"])
            )
        # Se la variabile di sessione non è quella attesa, prova comunque ciò
        # che è installato: meglio tentare che rinunciare.
        if not candidates:
            for tool, argv in (
                ("wl-copy", ["wl-copy", "--type", "x-special/gnome-copied-files"]),
                ("xclip", ["xclip", "-selection", "clipboard", "-t", "x-special/gnome-copied-files"]),
            ):
                if shutil.which(tool):
                    candidates.append((tool, argv))
        for tool, argv in candidates:
            try:
                # start_new_session: lo strumento deve sopravvivere e mantenere
                # la selezione anche dopo il ritorno di questa funzione.
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                if proc.stdin is not None:
                    proc.stdin.write(payload)
                    proc.stdin.close()
                return tool
            except Exception:
                continue
        return None

    def _apply_remote_files(self, uris: List[str]) -> None:
        """Mette i file copiati nella sessione remota sugli appunti dell'host."""
        paths = []
        for uri in uris:
            try:
                paths.append(GLib.filename_from_uri(uri)[0] if uri.startswith("file://") else uri)
            except Exception:
                paths.append(uri)
        plain = "\n".join(paths)
        names = ", ".join(Path(p).name for p in paths[:3])
        extra = tr(" e altri {v}").format(v=len(paths) - 3) if len(paths) > 3 else ""

        tool = self._publish_uris_external(uris)
        if tool:
            self._append_log(
                tr("[FedTermHub] {n_uris} file dalla sessione remota sono negli appunti tramite {tool} ({names}{extra}). Percorsi: {plain}\n").format(n_uris=len(uris), tool=tool, names=names, extra=extra, plain=plain)
            )
            self._show_drop_toast(
                tr("{n_uris} file dalla sessione remota sono negli appunti.\nIncollali nel file manager di Linux (Ctrl+V).").format(n_uris=len(uris))
            )
            return

        # Nessuno strumento disponibile: mettiamo i percorsi come testo e
        # spieghiamo cosa installare, invece di lasciare l'utente al buio.
        try:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(plain, -1)
        except Exception:
            pass
        self._append_log(
            tr("[FedTermHub] Per incollare i FILE su Linux serve wl-clipboard (Wayland) oppure xclip (X11): installane uno con '{command}'. Percorsi: {plain}\n").format(command=distro.install_command("wl-clipboard"), plain=plain)
        )
        self._show_drop_toast(
            tr("Per incollare i file serve wl-clipboard: installalo con\n"
            "{command}\n"
            "(intanto negli appunti trovi i percorsi come testo).").format(command=distro.install_command("wl-clipboard")),
            seconds=12,
        )

    def _on_canvas_drag_data_received(
        self, _widget, drag_context, _x, _y, data, _info, time_
    ) -> None:  # noqa: ANN001
        accepted = False
        try:
            uris = list(data.get_uris() or [])
        except Exception:
            uris = []
        # Alcune sorgenti (e diversi setup Wayland) non popolano get_uris():
        # in quel caso decodifichiamo il payload text/uri-list grezzo.
        if not uris:
            try:
                raw = data.get_data()
            except Exception:
                raw = None
            if raw:
                text = raw.decode("utf-8", errors="replace")
                uris = [
                    line.strip()
                    for line in text.splitlines()
                    if line.strip() and not line.startswith("#")
                ]
        # Solo file locali esistenti: URI remoti (http:, sftp:) o percorsi
        # spariti non possono essere né copiati né serviti via CB_FILECONTENTS.
        valid: List[str] = []
        local_paths: List[Path] = []
        for uri in uris:
            try:
                path, _host = GLib.filename_from_uri(uri)
            except Exception:
                continue
            if path and os.path.exists(path):
                valid.append(uri)
                local_paths.append(Path(path))

        if not valid:
            self._append_log(
                tr("[FedTermHub] Drop ignorato: nessun file locale valido tra gli elementi trascinati.\n")
            )
            self._show_drop_toast(tr("Nessun file locale valido tra gli elementi trascinati."))
            Gtk.drag_finish(drag_context, False, False, time_)
            return

        # --- Percorso 1 (preferito): cartella condivisa /drive: -------------
        # Immediato, visibile e indipendente dalle capability del server.
        shared = self._shared_drive_path()
        if shared is not None:
            copied, errors = self._copy_files_to_shared_drive(local_paths, shared)
            if copied:
                nomi = ", ".join(p.name for p in local_paths[:3])
                extra = tr(" e altri {v}").format(v=len(local_paths) - 3) if len(local_paths) > 3 else ""
                self._append_log(
                    tr("[FedTermHub] {copied} elementi copiati nella cartella condivisa {shared} ({nomi}{extra}). In Windows: Questo PC → fedtermhub.\n").format(copied=copied, shared=shared, nomi=nomi, extra=extra)
                )
                self._show_drop_toast(
                    tr("{copied} elementi copiati nella cartella condivisa.\nIn Windows aprili da «Questo PC → fedtermhub».").format(copied=copied)
                )
                accepted = True
            if errors:
                for line in errors:
                    self._append_log(tr("[FedTermHub] Copia fallita — {line}\n").format(line=line))
                if not copied:
                    self._show_drop_toast(tr("Copia nella cartella condivisa fallita: vedi il log."))
            Gtk.drag_finish(drag_context, accepted, False, time_)
            return

        # --- Percorso 2 (ripiego): clipboard file via cliprdr ---------------
        # Funziona solo se la clipboard è attiva E il server negozia il
        # trasferimento file; richiede comunque un Ctrl+V dentro la sessione.
        if self.clip_bridge is None:
            hint = (
                tr("Attiva «Clipboard» nelle impostazioni della sessione, "
                "oppure — molto più affidabile — imposta una «Cartella condivisa».")
            )
            self._append_log(tr("[FedTermHub] Drop ignorato: clipboard condivisa non attiva. {hint}\n").format(hint=hint))
            self._show_drop_toast(
                tr("Per trascinare file serve una «Cartella condivisa» nelle "
                "impostazioni della sessione (consigliata), oppure la clipboard attiva."),
                seconds=10,
            )
            Gtk.drag_finish(drag_context, False, False, time_)
            return

        accepted = self.clip_bridge.write_host_uri_list(valid)
        if accepted:
            nomi = ", ".join(p.name for p in local_paths[:3])
            extra = tr(" e altri {v}").format(v=len(local_paths) - 3) if len(local_paths) > 3 else ""
            self._append_log(
                tr("[FedTermHub] {n_valid} file pronti nella clipboard remota ({nomi}{extra}): incolla (Ctrl+V) dentro la sessione per copiarli. Richiede il supporto file del server.\n").format(n_valid=len(valid), nomi=nomi, extra=extra)
            )
            self._show_drop_toast(
                tr("{n_valid} file pronti: premi Ctrl+V dentro la sessione Windows.\nSe non succede nulla, il server non consente il trasferimento file via clipboard: usa una «Cartella condivisa».").format(n_valid=len(valid)),
                seconds=10,
            )
        else:
            self._append_log(
                tr("[FedTermHub] Drop rifiutato: lista file troppo grande per la clipboard condivisa.\n")
            )
            self._show_drop_toast(tr("Troppi file per la clipboard condivisa: usa una «Cartella condivisa»."))
        Gtk.drag_finish(drag_context, accepted, False, time_)

    def _poll_frame(self) -> bool:
        if self.destroyed:
            return False
        if self.clip_bridge is not None:
            self.clip_bridge.poll_remote()
        header = self._read_header()
        if header is not None:
            seq, state, width, height, stride = header[2], header[3], header[4], header[5], header[6]
            event_seq = header[11]
            if event_seq != self.last_event_seq:
                self.last_event_seq = event_seq
                event_message = self._header_message()
                if event_message:
                    self._append_log(tr("[Windows] {event_message}\n").format(event_message=event_message))
                    upper = event_message.upper()
                    if "BUMP" in upper or "SESSION" in upper or "LOGON" in upper:
                        self.session_notice_label.set_text(
                            tr("Windows segnala una sessione esistente o una richiesta di accesso. "
                            "Apri Gestisci sessioni per disconnettere, eseguire logoff o inviare un messaggio.")
                        )
                        self.session_notice.set_no_show_all(False)
                        self.session_notice.show_all()
                    self.state_label.set_text(event_message)
            if state != self.last_state:
                message = self._header_message()
                self._set_state(state, self._state_title(state), message)
                self.last_state = state
            if seq != self.last_seq and seq % 2 == 0 and width > 0 and height > 0 and stride >= width * 4:
                size = stride * height
                if size <= self.frame_capacity and self.frame_map is not None:
                    seq_before = seq
                    # Copia in un buffer di staging riusato: allocare 8 MB di
                    # bytearray e una nuova ImageSurface a ogni frame creava
                    # picchi del garbage collector percepiti come scatti.
                    staging = self._staging
                    if staging is None or len(staging) < size:
                        staging = bytearray(size)
                        self._staging = staging
                    mv = memoryview(self.frame_map)
                    staging[0:size] = mv[HEADER_SIZE:HEADER_SIZE + size]
                    header_after = self._read_header()
                    if header_after and header_after[2] == seq_before and seq_before % 2 == 0:
                        # Anti-flash: durante la rinegoziazione della
                        # risoluzione il server può pubblicare frame
                        # completamente neri (buffer azzerato prima del
                        # ridisegno). Se siamo entro 2s da una richiesta di
                        # resize e il frame campionato è tutto nero mentre il
                        # precedente non lo era, teniamo l'ultimo frame buono.
                        if (
                            self.surface is not None
                            and time.monotonic() - self.last_resize_sent_at < 2.0
                            and self._frame_sample_is_black(staging, size)
                        ):
                            return not self.destroyed
                        dims = (width, height, stride)
                        if (
                            self.frame_data is None
                            or self.surface is None
                            or dims != self.last_dimensions
                            or len(self.frame_data) < size
                        ):
                            self.frame_data = bytearray(size)
                            self.frame_data[0:size] = staging[0:size]
                            if cairo is not None:
                                self.surface = cairo.ImageSurface.create_for_data(
                                    self.frame_data, cairo.FORMAT_ARGB32, width, height, stride
                                )
                        else:
                            self.frame_data[0:size] = staging[0:size]
                            if self.surface is not None:
                                self.surface.mark_dirty()
                        self.remote_width, self.remote_height = width, height
                        self.last_dimensions = dims
                        self.last_seq = seq_before
                        self.canvas.queue_draw()
                        if state == STATE_CONNECTED:
                            self.status_card.hide()
            if state in {STATE_ERROR, STATE_DISCONNECTED} and self.process and self.process.poll() is not None:
                self.started = False
        if self.process is not None and self.process.poll() is not None and self.last_state not in {STATE_ERROR, STATE_DISCONNECTED}:
            self._set_state(STATE_ERROR, tr("Backend RDP terminato"), tr("Codice uscita: {returncode}").format(returncode=self.process.returncode))
            self.started = False
        return not self.destroyed

    def _header_message(self) -> str:
        if self.frame_map is None:
            return ""
        raw = self.frame_map[64:64 + 512]
        return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")

    @staticmethod
    def _state_title(state: int) -> str:
        return {
            STATE_IDLE: tr("RDP pronto"),
            STATE_CONNECTING: tr("Connessione RDP"),
            STATE_CONNECTED: tr("Connesso"),
            STATE_DISCONNECTED: tr("Sessione terminata"),
            STATE_ERROR: tr("Errore RDP"),
            STATE_STOPPING: tr("Chiusura RDP"),
        }.get(state, "RDP")

    def _set_state(self, state: int, title: str, message: str = "") -> None:
        self.state_label.set_text(message or title)
        classes = self.state_dot.get_style_context()
        for css in ("status-dot-pending", "status-dot-ok", "status-dot-error"):
            classes.remove_class(css)
        if state == STATE_CONNECTED:
            classes.add_class("status-dot-ok")
            self.spinner.stop()
            self.status_card.hide()
            self._schedule_resolution_update(120)
            # Dopo una (ri)connessione la sessione remota riparte con gli
            # indicatori di blocco azzerati: ridichiariamo quelli veri.
            GLib.timeout_add(400, lambda: (self.sync_lock_keys(), False)[1])
        elif state == STATE_ERROR:
            classes.add_class("status-dot-error")
            self.spinner.stop()
            self.status_card.show()
        else:
            classes.add_class("status-dot-pending")
            if state in {STATE_CONNECTING, STATE_STOPPING}:
                self.spinner.start()
            else:
                self.spinner.stop()
            self.status_card.show()
        # Riconnetti in evidenza quando la sessione è caduta (errore o
        # terminata, per esempio dopo blocco/sospensione del PC), nascosto
        # mentre si sta connettendo o è già connessa.
        if state in {STATE_ERROR, STATE_DISCONNECTED}:
            self.status_reconnect.show()
        else:
            self.status_reconnect.hide()
        self.status_title.set_text(title)
        self.status_message.set_text(message or title)
        if self.state_callback:
            self.state_callback(message or title)

    def _on_fs_handle_realize(self, widget: Gtk.Widget) -> None:
        """Mostra il cursore 'mano/spostamento' sopra la maniglia di trascinamento."""
        try:
            window = widget.get_window()
            if window is not None:
                display = widget.get_display()
                cursor = Gdk.Cursor.new_from_name(display, "grab")
                if cursor is None:
                    cursor = Gdk.Cursor.new_for_display(display, Gdk.CursorType.FLEUR)
                window.set_cursor(cursor)
        except Exception:
            pass

    def _on_overlay_child_position(self, _overlay: Gtk.Overlay, child: Gtk.Widget, allocation) -> bool:  # noqa: ANN001
        """Posiziona la barra fullscreen quando è in modalità 'trascinata'.

        Ritorna True (posizione gestita da noi) solo per la revealer della barra
        e solo quando l'utente l'ha spostata manualmente; in tutti gli altri
        casi lasciamo decidere l'overlay con l'allineamento predefinito.
        """
        if child is not self.fullscreen_bar_revealer or self._fs_bar_pos is None:
            return False
        try:
            nat_w = child.get_preferred_width()[1]
            nat_h = child.get_preferred_height()[1]
        except Exception:
            return False
        overlay_alloc = self.overlay.get_allocation()
        max_x = max(0, overlay_alloc.width - nat_w)
        max_y = max(0, overlay_alloc.height - nat_h)
        x, y = self._fs_bar_pos
        allocation.x = int(max(0, min(x, max_x)))
        allocation.y = int(max(0, min(y, max_y)))
        allocation.width = nat_w
        allocation.height = nat_h
        return True

    def _enter_manual_bar_mode(self) -> None:
        """Passa la barra da 'ancorata' a 'posizione libera' senza saltare.

        Alla prima presa, calcola la posizione attuale della barra rispetto
        all'overlay così che il trascinamento parta esattamente da dov'è.
        """
        if self._fs_bar_pos is not None:
            return
        coords = self.fullscreen_bar_revealer.translate_coordinates(self.overlay, 0, 0)
        if coords is not None:
            self._fs_bar_pos = (float(coords[0]), float(coords[1]))
        else:
            alloc = self.fullscreen_bar_revealer.get_allocation()
            self._fs_bar_pos = (float(alloc.x), float(alloc.y))
        # Da qui in poi il posizionamento è manuale: azzera gli allineamenti
        # così get-child-position ha pieno controllo.
        self.fullscreen_bar_revealer.set_halign(Gtk.Align.START)
        self.fullscreen_bar_revealer.set_valign(Gtk.Align.START)
        self.fullscreen_bar_revealer.set_margin_top(0)
        self.fullscreen_bar_revealer.set_margin_start(0)
        self.fullscreen_bar_revealer.set_margin_end(0)

    def _reset_bar_position(self) -> None:
        """Riporta la barra all'ancoraggio predefinito (alto, centrata)."""
        self._fs_bar_pos = None
        self.fullscreen_bar_revealer.set_halign(Gtk.Align.CENTER)
        self.fullscreen_bar_revealer.set_valign(Gtk.Align.START)
        self.fullscreen_bar_revealer.set_margin_top(8)
        self.fullscreen_bar_revealer.set_margin_start(0)
        self.fullscreen_bar_revealer.set_margin_end(0)
        self.overlay.queue_resize()

    def _on_fs_bar_drag_begin(self, widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if event.button != 1:
            return False
        # Doppio clic: ripristina la posizione ancorata.
        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS:
            self._reset_bar_position()
            return True
        self._enter_manual_bar_mode()
        self._fs_bar_dragging = True
        self._fs_bar_moved = False
        # Offset del puntatore rispetto all'angolo della barra, per un drag
        # senza scatti. event.x/y sono relativi alla maniglia: convertiamo in
        # coordinate overlay e sottraiamo l'origine corrente della barra.
        coords = widget.translate_coordinates(self.overlay, int(event.x), int(event.y))
        if coords is not None and self._fs_bar_pos is not None:
            self._fs_bar_drag_dx = coords[0] - self._fs_bar_pos[0]
            self._fs_bar_drag_dy = coords[1] - self._fs_bar_pos[1]
        else:
            self._fs_bar_drag_dx = 0.0
            self._fs_bar_drag_dy = 0.0
        # Mentre trascina, non far sparire la barra a timer.
        if self.fullscreen_bar_hide_id is not None:
            try:
                GLib.source_remove(self.fullscreen_bar_hide_id)
            except Exception:
                pass
            self.fullscreen_bar_hide_id = None
        try:
            window = widget.get_window()
            if window is not None:
                cursor = Gdk.Cursor.new_from_name(widget.get_display(), "grabbing")
                if cursor is not None:
                    window.set_cursor(cursor)
        except Exception:
            pass
        return True

    def _on_fs_bar_drag_motion(self, widget: Gtk.Widget, event: Gdk.EventMotion) -> bool:
        if not self._fs_bar_dragging:
            return False
        coords = widget.translate_coordinates(self.overlay, int(event.x), int(event.y))
        if coords is None:
            return False
        self._fs_bar_moved = True
        self._fs_bar_pos = (
            coords[0] - self._fs_bar_drag_dx,
            coords[1] - self._fs_bar_drag_dy,
        )
        self.overlay.queue_resize()
        return True

    def _on_fs_bar_drag_end(self, widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if event.button != 1 or not self._fs_bar_dragging:
            return False
        self._fs_bar_dragging = False
        try:
            window = widget.get_window()
            if window is not None:
                cursor = Gdk.Cursor.new_from_name(widget.get_display(), "grab")
                if cursor is not None:
                    window.set_cursor(cursor)
        except Exception:
            pass
        # Riprogramma la scomparsa solo se non è rimasto il mouse sopra.
        self._schedule_fullscreen_bar_hide(2600)
        return True

    def _frame_sample_is_black(self, buffer: bytearray, size: int) -> bool:
        """Rileva un frame completamente nero campionando ~64 punti sparsi.

        Costo trascurabile (nessuna scansione degli 8 MB): se anche un solo
        campione ha un canale RGB non nullo, il frame NON è nero e viene
        mostrato normalmente. Falsi positivi possibili solo con un desktop
        remoto realmente tutto nero, caso in cui saltare il frame è invisibile.
        """
        if size < 4:
            return False
        try:
            pixels = size // 4
            step = max(1, pixels // 64)
            for i in range(0, pixels, step):
                off = i * 4
                # Formato BGRX: basta un byte B, G o R diverso da zero.
                if buffer[off] or buffer[off + 1] or buffer[off + 2]:
                    return False
            return True
        except Exception:
            return False

    def _on_canvas_realize(self, widget: Gtk.Widget) -> None:
        try:
            window = widget.get_window()
            if window is not None:
                window.set_background_pattern(None)
        except Exception:
            pass

    def _on_draw(self, widget: Gtk.DrawingArea, cr) -> bool:  # noqa: ANN001
        alloc = widget.get_allocation()
        cr.set_source_rgb(0.015, 0.02, 0.027)
        cr.paint()
        if self.surface is None or self.remote_width <= 0 or self.remote_height <= 0:
            return False
        sx = alloc.width / self.remote_width
        sy = alloc.height / self.remote_height
        self.scale = min(sx, sy)
        draw_w = self.remote_width * self.scale
        draw_h = self.remote_height * self.scale
        self.offset_x = (alloc.width - draw_w) / 2.0
        self.offset_y = (alloc.height - draw_h) / 2.0
        cr.save()
        cr.translate(self.offset_x, self.offset_y)
        cr.scale(self.scale, self.scale)
        cr.set_source_surface(self.surface, 0, 0)
        try:
            # Il framebuffer RDP è opaco: SOURCE copia i pixel senza il costo
            # (e i possibili artefatti) della composizione alpha di OVER.
            cr.set_operator(cairo.OPERATOR_SOURCE)
        except Exception:
            pass
        pattern = cr.get_source()
        try:
            # A scala 1:1 il filtro bilineare costa e sfoca leggermente il
            # testo: NEAREST è pixel-perfect. Il bilineare resta per lo
            # scaling reale (fit in finestra o fullscreen su altra risoluzione).
            if abs(self.scale - 1.0) < 0.001:
                pattern.set_filter(cairo.FILTER_NEAREST)
            else:
                pattern.set_filter(cairo.FILTER_BILINEAR)
        except Exception:
            pass
        # Limita il disegno al rettangolo del frame remoto: con
        # OPERATOR_SOURCE un paint() sull'intero canvas scriverebbe
        # trasparenza nelle bande laterali (fuori dal frame), che il
        # compositor mostrerebbe come nero — un'altra fonte di flash.
        cr.rectangle(0, 0, self.remote_width, self.remote_height)
        cr.fill()
        cr.restore()
        return False

    def _remote_point(self, x: float, y: float) -> Tuple[int, int]:
        if self.scale <= 0 or self.remote_width <= 0 or self.remote_height <= 0:
            return 0, 0
        rx = int((x - self.offset_x) / self.scale)
        ry = int((y - self.offset_y) / self.scale)
        return max(0, min(self.remote_width - 1, rx)), max(0, min(self.remote_height - 1, ry))

    def _send(self, cmd_type: int, a: int = 0, b: int = 0, c: int = 0) -> None:
        if self.command_write_fd is None:
            return
        try:
            os.write(self.command_write_fd, struct.pack("<IIII", cmd_type, a & 0xFFFFFFFF, b & 0xFFFFFFFF, c & 0xFFFFFFFF))
        except (BrokenPipeError, OSError):
            pass

    def _on_motion(self, _widget: Gtk.Widget, event: Gdk.EventMotion) -> bool:
        if self.fullscreen_active and event.y <= 14:
            self._show_fullscreen_bar()
        now = time.monotonic()
        if now - self._last_motion < 0.008:
            return True
        self._last_motion = now
        x, y = self._remote_point(event.x, event.y)
        self._send(CMD_MOUSE_MOVE, x, y, 0)
        return True

    def _on_button_press(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        self.canvas.grab_focus()
        x, y = self._remote_point(event.x, event.y)
        packed = ((x & 0xFFFF) << 16) | (y & 0xFFFF)
        self._send(CMD_MOUSE_BUTTON, int(event.button), 1, packed)
        return True

    def _on_button_release(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        x, y = self._remote_point(event.x, event.y)
        packed = ((x & 0xFFFF) << 16) | (y & 0xFFFF)
        self._send(CMD_MOUSE_BUTTON, int(event.button), 0, packed)
        return True

    def _on_scroll(self, _widget: Gtk.Widget, event: Gdk.EventScroll) -> bool:
        x, y = self._remote_point(event.x, event.y)
        delta = 0
        if event.direction == Gdk.ScrollDirection.UP:
            delta = 120
        elif event.direction == Gdk.ScrollDirection.DOWN:
            delta = -120
        elif hasattr(event, "delta_y"):
            delta = int(-event.delta_y * 120)
        if delta:
            self._send(CMD_MOUSE_WHEEL, delta, x, y)
        return True

    def _on_key_press(self, _widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_F11:
            self._toggle_fullscreen()
            return True
        if self.fullscreen_active and event.keyval == Gdk.KEY_Escape:
            self._request_fullscreen(False)
            return True
        keycode = int(event.hardware_keycode)
        self.pressed_keys.add(keycode)
        self._send(CMD_KEY_XKB, keycode, 1, 0)
        return True

    def _on_key_release(self, _widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        keycode = int(event.hardware_keycode)
        self.pressed_keys.discard(keycode)
        self._send(CMD_KEY_XKB, keycode, 0, 0)
        return True

    def _lock_toggle_states(self) -> int:
        """Stato corrente dei tasti di blocco locali come maschera KBD_SYNC_*.

        Lo stato di blocco è mantenuto dal server X (o dal compositor Wayland)
        prima che l'evento raggiunga GTK: il nostro handler di tastiera lo
        consuma per la propagazione GTK ma non impedisce l'aggiornamento del
        lock locale. Il lato locale è quindi sempre autoritativo, e basta
        sincronizzare in una sola direzione quando la scheda prende il fuoco.
        """
        flags = 0
        try:
            keymap = Gdk.Keymap.get_for_display(self.canvas.get_display())
        except Exception:
            return 0
        for getter, flag in (
            ("get_scroll_lock_state", KBD_SYNC_SCROLL_LOCK),
            ("get_num_lock_state", KBD_SYNC_NUM_LOCK),
            ("get_caps_lock_state", KBD_SYNC_CAPS_LOCK),
        ):
            try:
                if getattr(keymap, getter)():
                    flags |= flag
            except Exception:
                # get_scroll_lock_state non è esposto da tutti i backend GDK:
                # meglio perdere un indicatore che rinunciare agli altri due.
                continue
        return flags

    def sync_lock_keys(self) -> None:
        """Ridichiara al server lo stato dei tasti di blocco.

        Utile anche fuori dal focus-in: dopo una riconnessione la sessione
        remota riparte con gli indicatori azzerati.
        """
        if self.started and not self.destroyed:
            self._send(CMD_FOCUS, self._lock_toggle_states(), 0, 0)

    def _on_focus_in(self, _widget: Gtk.Widget, _event: Gdk.EventFocus) -> bool:
        self._send(CMD_FOCUS, self._lock_toggle_states(), 0, 0)
        return False

    def _on_focus_out(self, _widget: Gtk.Widget, _event: Gdk.EventFocus) -> bool:
        # Release modifiers and any held key so changing tab cannot leave a key
        # logically pressed in the remote Windows session.
        for keycode in tuple(self.pressed_keys):
            self._send(CMD_KEY_XKB, keycode, 0, 0)
        self.pressed_keys.clear()
        return False

    def _on_canvas_size_allocate(self, _widget: Gtk.Widget, allocation: Gdk.Rectangle) -> None:
        if allocation.width > 0 and allocation.height > 0:
            self._schedule_resolution_update(260)

    def _schedule_resolution_update(self, delay_ms: int = 220) -> None:
        if not self.auto_resize or self.destroyed:
            return
        if self.resize_id is not None:
            try:
                GLib.source_remove(self.resize_id)
            except Exception:
                pass
        self.resize_id = GLib.timeout_add(delay_ms, self._send_current_resolution)

    def _send_current_resolution(self) -> bool:
        self.resize_id = None
        if self.destroyed or not self.started or self.last_state != STATE_CONNECTED:
            return False
        allocation = self.canvas.get_allocation()
        width = max(200, min(self.max_width, int(allocation.width)))
        height = max(200, min(self.max_height, int(allocation.height)))
        if width <= 0 or height <= 0:
            return False
        now = time.monotonic()
        if (width, height) == self.last_resize_request and now - self.last_resize_sent_at < 1.0:
            return False
        self.last_resize_request = (width, height)
        self.last_resize_sent_at = now
        self._send(CMD_RESIZE, width, height, 100)
        self._append_log(tr("[FedTermHub] Richiesta risoluzione remota {width}x{height}.\n").format(width=width, height=height))
        return False

    def _on_fit_resolution(self, _button: Optional[Gtk.Button] = None) -> None:
        self.auto_resize = True
        self.last_resize_request = (0, 0)
        self._schedule_resolution_update(40)
        self.canvas.grab_focus()
        self._schedule_fullscreen_bar_hide(1200)

    def _request_fullscreen(self, enabled: bool) -> None:
        if self.fullscreen_callback:
            self.fullscreen_callback(bool(enabled))
        else:
            top = self.get_toplevel()
            if isinstance(top, Gtk.Window):
                top.fullscreen() if enabled else top.unfullscreen()
            self.set_fullscreen_state(enabled)

    def _toggle_fullscreen(self) -> None:
        self._request_fullscreen(not self.fullscreen_active)

    def _on_fullscreen(self, _button: Gtk.Button) -> None:
        self._request_fullscreen(True)

    def _on_exit_fullscreen(self, _button: Optional[Gtk.Button] = None) -> None:
        self._request_fullscreen(False)

    def set_fullscreen_state(self, enabled: bool) -> None:
        self.fullscreen_active = bool(enabled)
        if self.fullscreen_active:
            self.toolbar.hide()
            self.fullscreen_bar_revealer.set_no_show_all(False)
            self.fullscreen_bar_revealer.show_all()
            self._show_fullscreen_bar()
        else:
            self.fullscreen_bar_revealer.set_reveal_child(False)
            self.fullscreen_bar_revealer.hide()
            self.fullscreen_bar_revealer.set_no_show_all(True)
            self.toolbar.show_all()
            if self.fullscreen_bar_hide_id is not None:
                try:
                    GLib.source_remove(self.fullscreen_bar_hide_id)
                except Exception:
                    pass
                self.fullscreen_bar_hide_id = None
        self._schedule_resolution_update(420)
        GLib.timeout_add(460, lambda: (self.canvas.grab_focus(), False)[1])

    def _show_fullscreen_bar(self) -> None:
        if not self.fullscreen_active:
            return
        self.fullscreen_bar_revealer.set_reveal_child(True)
        self._schedule_fullscreen_bar_hide(2200)

    def _schedule_fullscreen_bar_hide(self, delay_ms: int = 1800) -> None:
        if self.fullscreen_bar_hide_id is not None:
            try:
                GLib.source_remove(self.fullscreen_bar_hide_id)
            except Exception:
                pass
        self.fullscreen_bar_hide_id = GLib.timeout_add(delay_ms, self._hide_fullscreen_bar)

    def _pointer_inside_bar(self) -> bool:
        """Controlla la posizione FISICA del puntatore rispetto alla barra.

        Non si fida degli eventi enter/leave (che su alcuni setup, specie
        Wayland, possono non arrivare o arrivare con dettagli ambigui):
        interroga direttamente il device e confronta con l'allocazione reale
        della barra, con un margine di tolleranza.
        """
        try:
            if not self.fullscreen_bar_revealer.get_reveal_child():
                return False
            window = self.get_window()
            if window is None:
                return False
            display = self.get_display()
            seat = display.get_default_seat()
            pointer = seat.get_pointer()
            if pointer is None:
                return False
            _win, px, py, _mask = window.get_device_position(pointer)
            alloc = self.fullscreen_bar_revealer.get_allocation()
            # Coordinate della revealer relative al nostro widget.
            coords = self.fullscreen_bar_revealer.translate_coordinates(self, 0, 0)
            if coords is not None:
                bx, by = coords
            else:
                bx, by = alloc.x, alloc.y
            margin = 6
            return (bx - margin) <= px <= (bx + alloc.width + margin) and \
                   (by - margin) <= py <= (by + alloc.height + margin)
        except Exception:
            # In dubbio, considera il mouse sopra: meglio una barra che resta
            # un attimo in più di una che sparisce sotto il puntatore.
            return True

    def _hide_fullscreen_bar(self) -> bool:
        self.fullscreen_bar_hide_id = None
        # Mai nascondere la barra mentre il mouse ci è sopra: doppio controllo,
        # sia il flag da enter/leave sia la posizione fisica del puntatore.
        if getattr(self, "_pointer_on_bar", False) or self._pointer_inside_bar():
            # Riprova più tardi: quando il mouse se ne andrà davvero, sparirà.
            self._schedule_fullscreen_bar_hide(900)
            return False
        if self.fullscreen_active:
            self.fullscreen_bar_revealer.set_reveal_child(False)
            self.canvas.grab_focus()
        return False

    def _on_fullscreen_bar_enter(self, _widget: Gtk.Widget, _event) -> bool:
        self._pointer_on_bar = True
        if self.fullscreen_bar_hide_id is not None:
            try:
                GLib.source_remove(self.fullscreen_bar_hide_id)
            except Exception:
                pass
            self.fullscreen_bar_hide_id = None
        return False

    def _on_fullscreen_bar_leave(self, _widget: Gtk.Widget, event) -> bool:  # noqa: ANN001
        # GTK genera un leave-notify con detail=INFERIOR quando il puntatore
        # passa dallo sfondo della barra a un suo pulsante figlio: il mouse è
        # ancora DENTRO la barra, quindi va ignorato — era questo a farla
        # sparire mentre ci si stava sopra.
        try:
            if event.detail == Gdk.NotifyType.INFERIOR:
                return False
        except Exception:
            pass
        self._pointer_on_bar = False
        self._schedule_fullscreen_bar_hide(650)
        return False

    def _on_close_session(self, _button: Optional[Gtk.Button] = None) -> None:
        if self.close_callback:
            self.close_callback()
        else:
            self.stop()

    def set_detach_callback(self, callback: Optional[Callable[[bool], None]]) -> None:
        self.detach_callback = callback
        for button in self.detach_buttons:
            button.set_visible(callback is not None)
            button.set_sensitive(callback is not None)

    def _on_detach(self, _button: Optional[Gtk.Button] = None) -> None:
        if self.detach_callback is not None:
            self.detach_callback(bool(self.fullscreen_active))

    def _on_minimize_window(self, _button: Optional[Gtk.Button] = None) -> None:
        window = self.get_toplevel()
        if isinstance(window, Gtk.Window):
            try:
                window.iconify()
            except Exception:
                pass

    def _on_ctrl_alt_del(self, _button: Gtk.Button) -> None:
        # XKB keycodes for Ctrl, Alt and Delete on the standard evdev/XKB map.
        # The helper translates them through FreeRDP's keyboard map.
        for key in (37, 64, 119):
            self._send(CMD_KEY_XKB, key, 1, 0)
        for key in (119, 64, 37):
            self._send(CMD_KEY_XKB, key, 0, 0)

    def request_full_refresh(self) -> bool:
        if self.destroyed:
            return False
        self._send(CMD_REFRESH)
        self.canvas.queue_draw()
        return False

    def _on_refresh(self, _button: Gtk.Button) -> None:
        self.request_full_refresh()

    def _on_reconnect(self, _button: Gtk.Button) -> None:
        self.stop(silent=True)
        GLib.timeout_add(350, self.request_start)

    def _on_stop(self, _button: Gtk.Button) -> None:
        self.stop()

    def _on_manage_sessions(self, _button: Gtk.Button) -> None:
        if self.session_manager_callback:
            self.session_manager_callback()

    def _on_diagnostics(self, _button: Gtk.Button) -> None:
        header = self._read_header()
        details = [
            tr("Motore: libfreerdp3 nativo"),
            tr("Helper: {native_helper_path}").format(native_helper_path=native_helper_path()),
            tr("PID: {v}").format(v=self.process.pid if self.process else '-'),
            tr("Stato processo: {v}").format(v=self.process.poll() if self.process else '-'),
            tr("Framebuffer: {v}x{v2} stride={v3}").format(v=self.last_dimensions[0], v2=self.last_dimensions[1], v3=self.last_dimensions[2]),
            tr("Sequenza frame: {last_seq}").format(last_seq=self.last_seq),
            tr("Backend GTK: {v}").format(v=Gdk.Display.get_default().get_name() if Gdk.Display.get_default() else '-'),
            tr("XEmbed/Gtk.Socket: non utilizzati"),
            tr("Schermo intero immersivo: {v}").format(v='ON' if self.fullscreen_active else 'OFF'),
            tr("Auto-risoluzione: {v}").format(v='ON' if self.auto_resize else 'OFF'),
            tr("Ultima richiesta risoluzione: {v}x{v2}").format(v=self.last_resize_request[0], v2=self.last_resize_request[1]),
            tr("Clipboard bidirezionale: {clipboard_status_text}").format(clipboard_status_text=self._clipboard_status_text()),
        ]
        if header:
            details.extend([
                tr("Codice errore FreeRDP: 0x{v:08x}").format(v=header[8]),
                tr("Logon data/type: 0x{v:08x} / 0x{v2:08x}").format(v=header[9], v2=header[10]),
                tr("Messaggio: {header_message}").format(header_message=self._header_message()),
            ])
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel() if isinstance(self.get_toplevel(), Gtk.Window) else None,
            modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.CLOSE,
            text=tr("Diagnostica RDP nativa"),
        )
        dialog.format_secondary_text("\n".join(details))
        dialog.run()
        dialog.destroy()

    def _dismiss_session_notice(self, _button: Optional[Gtk.Button] = None) -> None:
        self.session_notice.hide()
        self.session_notice.set_no_show_all(True)

    def _on_log_toggle(self, button: Gtk.ToggleButton) -> None:
        self.log_revealer.set_reveal_child(button.get_active())

    def _append_log(self, text: str) -> bool:
        self.logs.append(text)
        if len(self.logs) > 4000:
            self.logs = self.logs[-3000:]
        buffer = self.log_view.get_buffer()
        end = buffer.get_end_iter()
        buffer.insert(end, text)
        mark = buffer.create_mark(None, buffer.get_end_iter(), False)
        self.log_view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
        return False

    def stop(self, silent: bool = False) -> None:
        # Rimuove il poller del frame precedente: senza questo passaggio ogni
        # riconnessione della stessa scheda accumulava un timeout a 33 Hz che
        # continuava a girare fino alla distruzione del widget.
        if self.poll_id is not None:
            try:
                GLib.source_remove(self.poll_id)
            except Exception:
                pass
            self.poll_id = None
        if self.command_write_fd is not None:
            self._send(CMD_STOP)
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=1.2)
            except subprocess.TimeoutExpired:
                try:
                    process.terminate()
                    process.wait(timeout=1.2)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
        self.process = None
        for fd_name in ("command_write_fd", "config_fd", "frame_fd"):
            fd = getattr(self, fd_name)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, fd_name, None)
        if self.frame_map is not None:
            try:
                self.frame_map.close()
            except Exception:
                pass
            self.frame_map = None
        if self.clip_bridge is not None:
            self.clip_bridge.close()
            self.clip_bridge = None
        self.started = False
        self.pressed_keys.clear()
        if hasattr(self, "session_notice"):
            self._dismiss_session_notice()
        if not silent and not self.destroyed:
            self._set_state(STATE_DISCONNECTED, tr("Sessione terminata"), tr("Connessione RDP chiusa"))

    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        if self.fullscreen_active and self.fullscreen_callback:
            try:
                self.fullscreen_callback(False)
            except Exception:
                pass
        self.destroyed = True
        # A reconnect needs the credential while the tab is alive. Drop the
        # final Python reference only when the tab is destroyed.
        self.password = None
        for source_name in ("resize_id", "fullscreen_bar_hide_id", "_drop_toast_hide_id"):
            source_id = getattr(self, source_name, None)
            if source_id is not None:
                try:
                    GLib.source_remove(source_id)
                except Exception:
                    pass
                setattr(self, source_name, None)
        if self.poll_id is not None:
            try:
                GLib.source_remove(self.poll_id)
            except Exception:
                pass
            self.poll_id = None
        self.stop(silent=True)
