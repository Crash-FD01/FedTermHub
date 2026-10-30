# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Workspace terminale multi-riquadro di FedTermHub (basato su VTE).

Terminale a riquadri divisibili con layout multipli, ricerca nello scrollback,
apertura degli URL da tastiera (hints) e invio dell'input a più riquadri
contemporaneamente (broadcast).

Componenti pubblici (interfaccia storica preservata per il resto dell'app):

* :class:`TerminalPane` — un singolo riquadro ``Vte.Terminal`` con tema curato,
  palette ANSI a 16 colori, truecolor, ligature (se il font le supporta), URL
  cliccabili, OSC 52, OSC 8, ricerca nello scrollback, zoom del font.
* :class:`TerminalWorkspace` — contenitore di riquadri con **layout multipli**
  (stack, tall, fat, grid), navigazione, chiusura, zoom, schermo
  intero, **hints da tastiera** per aprire URL senza mouse e **broadcast input**
  su più riquadri. Espone ``.terminal``, ``.title``, ``.panes``,
  ``.fullscreen_active``, ``apply_preferences``, ``apply_terminal_theme``,
  ``feed_all``, ``reconnect``, ``is_connected`` e ``dispatch`` come la vecchia
  ``TerminalTab``.

Scorciatoie da tastiera:
    Ctrl+Shift+Invio   nuovo riquadro nel layout corrente
    Ctrl+Shift+O   dividi in verticale (affiancati)   → layout 'tall'
    Ctrl+Shift+E   dividi in orizzontale (sopra/sotto) → layout 'fat'
    Ctrl+Shift+W   chiudi il riquadro attivo
    Ctrl+Shift+]   riquadro successivo     Ctrl+Shift+[ precedente
    Ctrl+Shift+L   cicla i layout (stack → tall → fat → grid)
    Ctrl+Shift+P   broadcast: invio dell'input a tutti i riquadri on/off
    Ctrl+Shift+H   hints: apri un URL visibile scegliendolo da tastiera
    Ctrl+Shift+C   copia      Ctrl+Shift+V incolla
    Ctrl+Shift+F   cerca nello scrollback
    Ctrl +/-/0     zoom del font
    F11            schermo intero
"""

from __future__ import annotations

import math
import os
import re
import shutil
import sys
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
try:
    from .i18n import tr, trn
except ImportError:  # avvio come script (fedtermhub/fedtermhub.py) o dai test
    from i18n import tr, trn
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte  # noqa: E402


# Palette ANSI a 16 colori leggibile sul fondo scuro. Con una palette vuota VTE
# interpola i colori da foreground/background e ls, vim, htop diventano
# sfumature monocromatiche: la palette esplicita risolve il problema.
# Palette a 16 colori. Ogni voce e' verificata contro _BACKGROUND: il colore 0
# ("nero" ANSI) resta il piu' scuro per fedelta' al significato, ma a 3.1:1 e'
# leggibile, mentre il valore precedente (#1c2333) stava a 1.19:1 sullo sfondo
# ed era di fatto invisibile. Stesso motivo per il colore 8 (grigio), passato
# da 3.93:1 a 5.54:1. Tutti gli altri sono gia' sopra 4.5:1.
TERMINAL_PALETTE = (
    "#556380", "#ef4444", "#22c55e", "#eab308",
    "#3b82f6", "#d946ef", "#06b6d4", "#cbd5e1",
    "#7c8da6", "#f87171", "#4ade80", "#facc15",
    "#60a5fa", "#e879f9", "#22d3ee", "#f8fafc",
)

_FOREGROUND = "#dbeafe"
_BACKGROUND = "#0b1220"

# PCRE2 flags usati da Vte.Regex.
_PCRE2_MULTILINE = 0x00000400
_PCRE2_CASELESS = 0x00000008
_URL_PATTERN = r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"'\)\]]+"

# Alfabeto per le etichette "hints" (lettere della fila centrale della tastiera).
_HINT_ALPHABET = "asdfghjklqwertyuiopzxcvbnm"

# Layout disponibili, nell'ordine di ciclo con Ctrl+Shift+L.
LAYOUTS = ("stack", "tall", "fat", "grid")


def _default_shell(settings: object) -> str:
    configured = str(getattr(settings, "terminal_shell", "") or "").strip()
    if configured and Path(configured).exists():
        return configured
    return os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"


class TerminalPane(Gtk.Box):
    """Un singolo riquadro terminale, ri-generabile su divisione."""

    def __init__(
        self,
        argv: List[str],
        cwd: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        settings: Optional[object] = None,
        theme: str = "dark",
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.get_style_context().add_class("terminal-surface")
        self.get_style_context().add_class("terminal-pane")
        self.argv = list(argv)
        self.cwd = cwd
        self.extra_env = dict(extra_env or {})
        self.preferences = settings
        self.theme = theme
        self._font_scale = 1.0
        # Ultima winsize scritta nel PTY, per non inviare SIGWINCH inutili.
        self._last_pty_size: Tuple[int, int] = (0, 0)
        self._resize_pending = 0

        # Callback impostati dal workspace contenitore.
        self.on_focus: Optional[Callable[["TerminalPane"], None]] = None
        self.on_close: Optional[Callable[["TerminalPane"], None]] = None
        self.on_title: Optional[Callable[["TerminalPane", str], None]] = None
        self.on_state: Optional[Callable[["TerminalPane"], None]] = None
        # Notifica la directory corrente riportata dalla shell via OSC 7.
        # Firma: (pane, hostname, path). L'hostname serve al chiamante per
        # rifiutare aggiornamenti che arrivano da una macchina diversa.
        self.on_directory: Optional[Callable[["TerminalPane", str, str], None]] = None
        # Impostato dal chiamante per le sessioni SSH verso host Linux: fa
        # iniettare nella shell remota il PROMPT_COMMAND che emette OSC 7.
        self.osc7_bootstrap = False
        self._osc7_sent = False
        self._osc7_timer = 0
        self._last_directory = ""
        # Ritorna True se l'evento tastiera è stato consumato dal workspace
        # (scorciatoie globali / broadcast): impostato dal contenitore.
        self.on_key: Optional[Callable[["TerminalPane", "Gdk.EventKey"], bool]] = None
        self.connected = False
        self.exit_status: Optional[int] = None

        # Overlay per i marcatori "hints" (impostato dal workspace).
        self._hint_layer: Optional[Gtk.Widget] = None

        self.terminal = Vte.Terminal()
        self.terminal.set_scroll_on_keystroke(True)
        self.terminal.set_mouse_autohide(True)
        try:
            self.terminal.set_enable_bidi(False)
            self.terminal.set_allow_hyperlink(True)
        except Exception:
            pass
        # OSC 52: programmi remoti (vim, tmux, script via SSH) possono scrivere
        # sulla clipboard locale, Richiede VTE >= 0.76.
        try:
            self.terminal.set_enable_osc52(True)  # type: ignore[attr-defined]
        except Exception:
            try:
                self.terminal.set_property("enable-osc52", True)
            except Exception:
                pass
        try:
            # Doppio click "intelligente": seleziona interi IP, percorsi e URL
            # senza spezzarli su punti e slash.
            self.terminal.set_word_char_exceptions("-./_~:@?&=%+#")
        except Exception:
            pass

        self._url_match_tag = -1
        try:
            url_regex = Vte.Regex.new_for_match(_URL_PATTERN, -1, _PCRE2_MULTILINE)
            self._url_match_tag = self.terminal.match_add_regex(url_regex, 0)
            self.terminal.match_set_cursor_name(self._url_match_tag, "pointer")
        except Exception:
            self._url_match_tag = -1

        self.apply_preferences(settings)
        self.apply_terminal_theme(theme)

        self.terminal.connect("selection-changed", self._on_selection_changed)
        self.terminal.connect("button-press-event", self._on_button_press)
        self.terminal.connect("key-press-event", self._on_key_press)
        self.terminal.connect("focus-in-event", self._on_focus_in)
        # OSC 7: la shell dichiara la propria directory corrente. E' l'unico
        # modo corretto di seguirla — osservare i comandi digitati fallirebbe
        # con cd dentro script, alias, funzioni o pushd.
        try:
            self.terminal.connect(
                "current-directory-uri-changed", self._on_directory_uri_changed
            )
        except Exception:
            pass
        # VTE aggiorna il PTY in size_allocate, ma il figlio e' gia' partito
        # dal costruttore: questo garantisce il riallineamento al primo
        # allocate reale e a ogni ridimensionamento del riquadro.
        self.terminal.connect("size-allocate", self._on_size_allocate)
        self.terminal.connect("child-exited", self._on_child_exited)
        try:
            self.terminal.connect("window-title-changed", self._on_title_changed)
        except Exception:
            pass

        scroller = Gtk.ScrolledWindow()
        # Niente scroll orizzontale: VTE deve riadattare le colonne alla
        # larghezza reale e propagare il resize via TIOCSWINSZ, altrimenti il
        # processo remoto formatta il testo per una larghezza sbagliata.
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.terminal)

        # Barra di ricerca nello scrollback (Ctrl+Shift+F).
        self.search_revealer = Gtk.Revealer()
        self.search_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        search_box.get_style_context().add_class("terminal-search-bar")
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(tr("Cerca nello scrollback…"))
        self.search_entry.set_hexpand(True)
        prev_button = Gtk.Button.new_from_icon_name("go-up-symbolic", Gtk.IconSize.BUTTON)
        prev_button.set_tooltip_text(tr("Risultato precedente (Shift+Invio)"))
        next_button = Gtk.Button.new_from_icon_name("go-down-symbolic", Gtk.IconSize.BUTTON)
        next_button.set_tooltip_text(tr("Risultato successivo (Invio)"))
        close_button = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        close_button.set_tooltip_text(tr("Chiudi ricerca (Esc)"))
        search_box.pack_start(self.search_entry, True, True, 4)
        search_box.pack_start(prev_button, False, False, 0)
        search_box.pack_start(next_button, False, False, 0)
        search_box.pack_start(close_button, False, False, 4)
        self.search_revealer.add(search_box)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("key-press-event", self._on_search_key_press)
        self.search_entry.connect("activate", lambda *_: self._search_step(forward=True))
        prev_button.connect("clicked", lambda *_: self._search_step(forward=False))
        next_button.connect("clicked", lambda *_: self._search_step(forward=True))
        close_button.connect("clicked", lambda *_: self.hide_search())

        # Overlay che ospita il terminale + eventuale layer di hints.
        self._overlay = Gtk.Overlay()
        self._overlay.add(scroller)

        self.pack_start(self.search_revealer, False, False, 0)
        self.pack_start(self._overlay, True, True, 0)

        self.spawn()

    # Variabili che un processo in un PTY non deve MAI ereditare dal padre.
    # LINES/COLUMNS: ncurses le preferisce all'ioctl TIOCGWINSZ, quindi valori
    #   stantii fanno disegnare nano (e ogni applicazione ncurses) con una
    #   geometria diversa da quella reale del riquadro.
    # TERMCAP: descrizione del terminale precedente, incompatibile con VTE.
    # WINDOWID: si riferisce alla finestra da cui FedTermHub e' stato lanciato.
    # NOTA: VTE_VERSION NON va rimossa. Il valore ereditato si riferisce a
    # un'altra finestra, ma /etc/profile.d/vte.sh la usa come interruttore
    # ("[ ${VTE_VERSION:-0} -ge 3405 ]") per abilitare OSC 7, cioe' il modo in
    # cui la shell comunica al terminale la directory corrente. Va quindi
    # sovrascritta con la versione reale, non cancellata.
    _ENV_BLOCKLIST = ("LINES", "COLUMNS", "TERMCAP", "WINDOWID")

    @staticmethod
    def _vte_version_string() -> str:
        """Versione di VTE nel formato atteso da vte.sh: MAJOR*10000 + MINOR*100."""
        try:
            major = int(Vte.get_major_version())
            minor = int(Vte.get_minor_version())
            micro = int(Vte.get_micro_version())
            return str(major * 10000 + minor * 100 + micro)
        except Exception:
            # 3405 e' la soglia minima richiesta da vte.sh.
            return "6003"

    # ------------------------------------------------------------------ spawn
    def _build_env(self) -> List[str]:
        env = os.environ.copy()
        for name in self._ENV_BLOCKLIST:
            env.pop(name, None)
        # TERM va IMPOSTO, non suggerito: il widget e' un VTE, qualunque cosa
        # sia stata ereditata (tmux, un altro terminale, una sessione remota)
        # descrive un terminale diverso e manda in confusione terminfo.
        # FEDTERMHUB_TERM permette di forzarne un altro sugli apparati con
        # database terminfo ridotto, dove xterm-256color non esiste.
        env["TERM"] = os.environ.get("FEDTERMHUB_TERM") or "xterm-256color"
        # VTE gestisce il colore a 24 bit: dichiararlo abilita la gamma piena.
        env["COLORTERM"] = "truecolor"
        env["TERM_PROGRAM"] = "FedTermHub"
        env["VTE_VERSION"] = self._vte_version_string()
        if self.extra_env:
            # extra_env resta l'ultima parola: e' cosi' che una sessione puo'
            # imporre il proprio TERM.
            env.update(self.extra_env)
        return [f"{key}={value}" for key, value in env.items()]

    def _effective_cwd(self) -> str:
        if self.cwd and Path(self.cwd).is_dir():
            return self.cwd
        configured = str(getattr(self.preferences, "terminal_start_directory", "") or "").strip()
        if configured and Path(configured).is_dir():
            return configured
        return str(Path.home())

    def spawn(self) -> None:
        # Un nuovo PTY parte con la propria winsize di ripiego: invalidare
        # la cache costringe _sync_pty_size a riscriverla davvero.
        self._last_pty_size = (0, 0)
        self._osc7_sent = False
        self._last_directory = ""
        envv = self._build_env()
        cwd = self._effective_cwd()
        try:
            self.terminal.spawn_async(
                Vte.PtyFlags.DEFAULT,
                cwd,
                self.argv,
                envv,
                GLib.SpawnFlags.DEFAULT,
                None,
                None,
                -1,
                None,
                self._spawn_done,
                None,
            )
        except TypeError:
            # Firma più vecchia di Vte senza callback finale.
            self.terminal.spawn_async(
                Vte.PtyFlags.DEFAULT,
                cwd,
                self.argv,
                envv,
                GLib.SpawnFlags.DEFAULT,
                None,
                None,
                -1,
                None,
                None,
            )

    def _spawn_done(self, _terminal, _pid, error, _data=None) -> None:  # noqa: ANN001
        if error:
            self.connected = False
            self.terminal.feed(("\r\n" + tr("Errore avvio processo: {error}").format(error=error) + "\r\n").encode("utf-8"))
        else:
            self.connected = True
            # Il figlio e' partito dal costruttore, prima che il widget avesse
            # una dimensione reale: riallinea ora che il PTY esiste.
            self._schedule_pty_sync()
            self._schedule_osc7_bootstrap()
        if self.on_state is not None:
            self.on_state(self)

    def respawn(self) -> None:
        """Ri-esegue il comando nel riquadro (riconnessione in loco)."""
        try:
            self.terminal.reset(True, True)
        except Exception:
            pass
        self.exit_status = None
        self.terminal.feed(b"\r\n\x1b[2m[riconnessione in corso...]\x1b[0m\r\n")
        self.spawn()

    def clone_argv(self) -> "TerminalPane":
        """Crea un nuovo riquadro con lo stesso comando/cwd/env (per gli split)."""
        return TerminalPane(
            self.argv,
            cwd=self.cwd,
            extra_env=self.extra_env,
            settings=self.preferences,
            theme=self.theme,
        )

    # --------------------------------------------------------- preferenze UI
    def apply_preferences(self, settings: Optional[object]) -> None:
        self.preferences = settings
        pref = settings
        try:
            family = str(getattr(pref, "terminal_font", "") or "").strip()
            # Ligature: VTE le disegna se il font le fornisce (Fira Code,
            # JetBrains Mono, Cascadia Code…). Non serve alcuna API: basta
            # scegliere un font che le contenga nelle impostazioni.
            font = Pango.FontDescription(family) if family else Pango.FontDescription(tr("Monospace 11"))
            if self._font_scale != 1.0:
                size = font.get_size() or int(11 * Pango.SCALE)
                font.set_size(max(int(6 * Pango.SCALE), int(size * self._font_scale)))
            self.terminal.set_font(font)
        except Exception:
            pass
        try:
            self.terminal.set_scrollback_lines(int(getattr(pref, "terminal_scrollback_lines", 10000) or 10000))
            self.terminal.set_scroll_on_output(bool(getattr(pref, "terminal_scroll_on_output", False)))
            self.terminal.set_audible_bell(bool(getattr(pref, "terminal_audible_bell", False)))
        except Exception:
            pass
        try:
            self.terminal.set_cursor_shape(
                getattr(Vte.CursorShape, str(getattr(pref, "terminal_cursor_shape", "BLOCK")).upper(), Vte.CursorShape.BLOCK)
            )
            self.terminal.set_cursor_blink_mode(
                Vte.CursorBlinkMode.ON if bool(getattr(pref, "terminal_cursor_blink", True)) else Vte.CursorBlinkMode.OFF
            )
        except Exception:
            pass

    def apply_terminal_theme(self, theme: str) -> None:
        self.theme = theme
        fg = Gdk.RGBA()
        bg = Gdk.RGBA()
        fg.parse(_FOREGROUND)
        bg.parse(_BACKGROUND)
        palette = []
        for value in TERMINAL_PALETTE:
            color = Gdk.RGBA()
            color.parse(value)
            palette.append(color)
        self.terminal.set_colors(fg, bg, palette)
        try:
            self.terminal.set_bold_is_bright(True)
        except Exception:
            pass

    # ------------------------------------------------------------ operazioni
    def focus(self) -> None:
        try:
            self.terminal.grab_focus()
        except Exception:
            pass

    def shutdown(self) -> None:
        # Distruggere il riquadro chiude il PTY: VTE invia SIGHUP al processo
        # figlio (shell o ssh), come atteso chiudendo uno split.
        # Il timeout di resize va cancellato prima: scatterebbe su un widget
        # gia' distrutto.
        for attr in ("_resize_pending", "_osc7_timer"):
            if getattr(self, attr, 0):
                try:
                    GLib.source_remove(getattr(self, attr))
                except Exception:
                    pass
                setattr(self, attr, 0)
        try:
            self.destroy()
        except Exception:
            pass

    @staticmethod
    def _log_clipboard_error(action: str, exc: BaseException) -> None:
        print(tr("[FedTermHub] Clipboard: {action} fallita: {exc!r}").format(action=action, exc=exc), file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    def _copy(self) -> None:
        has_selection = False
        try:
            has_selection = bool(self.terminal.get_has_selection())
        except Exception:
            pass
        if not has_selection:
            print(tr("[FedTermHub] Clipboard: copia richiesta senza selezione attiva."), file=sys.stderr)
        try:
            self.terminal.copy_clipboard_format(Vte.Format.TEXT)
            return
        except Exception as exc:
            self._log_clipboard_error("copy_clipboard_format", exc)
        try:
            self.terminal.copy_clipboard()
        except Exception as exc:
            self._log_clipboard_error(tr("copy_clipboard (fallback)"), exc)

    def _paste(self) -> None:
        try:
            self.terminal.paste_clipboard()
        except Exception as exc:
            self._log_clipboard_error("paste_clipboard", exc)
            try:
                clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

                def _received(_cb: Gtk.Clipboard, text: Optional[str], _data=None) -> None:
                    if text:
                        try:
                            self.terminal.feed_child(text.encode("utf-8"))
                        except Exception as feed_exc:
                            self._log_clipboard_error(tr("feed_child fallback"), feed_exc)
                    else:
                        print(tr("[FedTermHub] Clipboard: nessun testo disponibile negli appunti."), file=sys.stderr)

                clipboard.request_text(_received, None)
            except Exception as fallback_exc:
                self._log_clipboard_error(tr("paste fallback via Gtk.Clipboard"), fallback_exc)

    def zoom(self, direction: int) -> None:
        if direction == 0:
            self._font_scale = 1.0
        else:
            step = 0.1 if direction > 0 else -0.1
            self._font_scale = max(0.5, min(3.0, self._font_scale + step))
        self.apply_preferences(self.preferences)

    # ------------------------------------------------------- ricerca scrollback
    def show_search(self) -> None:
        self.search_revealer.set_reveal_child(True)
        self.search_entry.grab_focus()
        try:
            if self.terminal.get_has_selection():
                self.terminal.copy_primary()
                primary = Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY)

                def received(_cb: Gtk.Clipboard, text: Optional[str], _data=None) -> None:
                    if text and "\n" not in text and len(text) < 200:
                        self.search_entry.set_text(text)
                        self.search_entry.select_region(0, -1)

                primary.request_text(received, None)
        except Exception:
            pass

    def hide_search(self) -> None:
        self.search_revealer.set_reveal_child(False)
        try:
            self.terminal.search_set_regex(None, 0)
        except Exception:
            pass
        self.terminal.grab_focus()

    def toggle_search(self) -> None:
        if self.search_revealer.get_reveal_child():
            self.hide_search()
        else:
            self.show_search()

    def _apply_search_pattern(self) -> bool:
        query = self.search_entry.get_text()
        if not query:
            try:
                self.terminal.search_set_regex(None, 0)
            except Exception:
                pass
            return False
        try:
            regex = Vte.Regex.new_for_search(
                re.escape(query), -1, _PCRE2_MULTILINE | _PCRE2_CASELESS
            )
            self.terminal.search_set_regex(regex, 0)
            self.terminal.search_set_wrap_around(True)
            return True
        except Exception as exc:
            self._log_clipboard_error("search_set_regex", exc)
            return False

    def _on_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        if self._apply_search_pattern():
            try:
                self.terminal.search_find_next()
            except Exception:
                pass

    def _search_step(self, forward: bool) -> None:
        if not self._apply_search_pattern():
            return
        try:
            if forward:
                self.terminal.search_find_next()
            else:
                self.terminal.search_find_previous()
        except Exception:
            pass

    def _on_search_key_press(self, _entry, event) -> bool:  # noqa: ANN001
        key = Gdk.keyval_name(event.keyval) or ""
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        if key == "Escape":
            self.hide_search()
            return True
        if key in {"Return", "KP_Enter"} and shift:
            self._search_step(forward=False)
            return True
        return False

    # ----------------------------------------------------------------- hints
    def collect_uris(self) -> List[str]:
        """URL attualmente visibili nel viewport, senza duplicati e nell'ordine
        di apparizione. Base per la modalità hints."""
        uris: List[str] = []
        seen = set()
        try:
            cols = self.terminal.get_column_count()
            rows = self.terminal.get_row_count()
            text = ""
            try:
                res = self.terminal.get_text_range(0, 0, rows, cols, None)
                text = res[0] if isinstance(res, tuple) else res
            except Exception:
                try:
                    res = self.terminal.get_text(None)
                    text = res[0] if isinstance(res, tuple) else res
                except Exception:
                    text = ""
            for match in re.finditer(_URL_PATTERN, text or ""):
                uri = match.group(0)
                if uri not in seen:
                    seen.add(uri)
                    uris.append(uri)
        except Exception:
            pass
        return uris

    def open_uri(self, uri: str) -> None:
        self._open_uri(uri)

    def show_diagnostics(self) -> None:
        """Stampa nel riquadro il confronto fra le tre misure che devono
        coincidere: la griglia di VTE, la winsize del PTY (``stty size``) e la
        geometria che ncurses usa davvero (``tput``). Se divergono, e' quella
        la causa di un'applicazione a schermo intero che disegna male."""
        try:
            rows = int(self.terminal.get_row_count())
            columns = int(self.terminal.get_column_count())
        except Exception:
            rows = columns = -1
        script = (
            tr("clear; printf '=== FedTermHub: diagnostica terminale ===\\n'; printf 'griglia VTE   : %s righe, %s colonne\\n' {rows} {columns}; printf 'PTY (stty)    : %s\\n' \"$(stty size 2>/dev/null || echo n/d)\"; printf 'ncurses (tput): %s righe, %s colonne\\n' \"$(tput lines 2>/dev/null)\" \"$(tput cols 2>/dev/null)\"; printf 'TERM          : %s (%s colori)\\n' \"$TERM\" \"$(tput colors 2>/dev/null)\"; printf 'LINES/COLUMNS : [%s] [%s]  (devono essere vuote)\\n' \"$LINES\" \"$COLUMNS\"; printf 'schermo alt.  : %s\\n' \"$(infocmp -1 \\\"$TERM\\\" 2>/dev/null | grep -c smcup)\"; printf 'nano          : %s\\n' \"$(nano --version 2>/dev/null | head -1)\"; printf 'Le tre geometrie devono coincidere.\\n'\n").format(rows=rows, columns=columns)
        )
        try:
            self.terminal.feed_child(script.encode("utf-8"))
        except Exception:
            pass

    # ------------------------------------------------------ OSC 7 / directory
    # Riga iniettata nella shell remota. Note sulle scelte:
    #  - lo spazio iniziale: con HISTCONTROL=ignorespace (predefinito su molte
    #    distribuzioni) la riga non finisce nella cronologia dell'utente;
    #  - il case su PROMPT_COMMAND evita di duplicare l'aggancio a ogni
    #    riconnessione e PRESERVA un PROMPT_COMMAND gia' impostato dall'utente
    #    invece di sovrascriverlo;
    #  - zsh non ha PROMPT_COMMAND: usa precmd_functions. La sintassi degli
    #    array e' pero' racchiusa in un eval, perche' dash/busybox analizzano
    #    l'intera riga prima di eseguirla e andrebbero in errore di sintassi
    #    su "precmd_functions+=(...)" anche senza mai entrare in quel ramo;
    #  - la chiamata finale emette subito l'OSC 7 senza attendere il prompt
    #    successivo, cosi' l'albero si allinea appena la sessione e' pronta.
    _OSC7_BOOTSTRAP = (
        " __fth_osc7() { printf '\\033]7;file://%s%s\\033\\\\' "
        "\"${HOSTNAME:-$(uname -n 2>/dev/null)}\" \"$PWD\"; }; "
        "if [ -n \"$ZSH_VERSION\" ]; then "
        "eval 'precmd_functions+=(__fth_osc7)'; "
        "elif [ -n \"$BASH_VERSION\" ]; then "
        "case \"$PROMPT_COMMAND\" in *__fth_osc7*) ;; "
        "*) PROMPT_COMMAND=\"__fth_osc7${PROMPT_COMMAND:+;$PROMPT_COMMAND}\";; esac; "
        "fi; __fth_osc7\n"
    )

    def _schedule_osc7_bootstrap(self) -> None:
        """Inietta il supporto OSC 7 nella shell remota, una volta sola.

        L'attesa non e' un ritardo arbitrario: serve a non scrivere la riga
        dentro un prompt che non e' quello della shell (conferma della chiave
        host, richiesta di secondo fattore, menu di un apparato). Non e' una
        garanzia — e' il motivo per cui l'iniezione e' disattivabile.
        """
        if not self.osc7_bootstrap or self._osc7_sent or self._osc7_timer:
            return
        self._osc7_timer = GLib.timeout_add(1800, self._send_osc7_bootstrap)

    def _send_osc7_bootstrap(self) -> bool:
        self._osc7_timer = 0
        if self._osc7_sent or not self.connected:
            return False
        # Se la shell ha gia' emesso OSC 7 da sola (host con vte-profile
        # installato), non serve iniettare nulla.
        if self._last_directory:
            self._osc7_sent = True
            return False
        try:
            self.terminal.feed_child(self._OSC7_BOOTSTRAP.encode("utf-8"))
            self._osc7_sent = True
        except Exception:
            pass
        return False

    def _on_directory_uri_changed(self, _terminal) -> None:  # noqa: ANN001
        try:
            uri = self.terminal.get_current_directory_uri()
        except Exception:
            return
        if not uri:
            return
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return
        path = unquote(parsed.path or "")
        if not path:
            return
        host = unquote(parsed.netloc or "")
        if path == self._last_directory:
            return
        self._last_directory = path
        if self.on_directory is not None:
            self.on_directory(self, host, path)

    @property
    def current_directory(self) -> str:
        return self._last_directory

    # ------------------------------------------------------- geometria del PTY
    def _sync_pty_size(self) -> bool:
        """Allinea la winsize del PTY alla griglia reale di VTE.

        VTE lo fa gia' da solo in size_allocate, ma la sincronizzazione qui
        copre due buchi concreti:
          - ``spawn()`` viene chiamato dal costruttore, quindi il figlio parte
            prima che il widget sia allocato: senza un riallineamento dopo il
            primo allocate, il processo resta con la geometria di ripiego;
          - se per qualunque motivo le due misure divergono, un'applicazione
            ncurses disegna su un'area piu' alta di quella visibile e tutto
            l'output scorre via lasciando solo l'ultima riga.

        Applica solo se i valori cambiano davvero: TIOCSWINSZ invia SIGWINCH a
        ogni modifica effettiva, e un SIGWINCH inutile fa ridisegnare l'intera
        schermata all'applicazione in primo piano.
        """
        self._resize_pending = 0
        try:
            pty = self.terminal.get_pty()
            if pty is None:
                return False
            rows = int(self.terminal.get_row_count())
            columns = int(self.terminal.get_column_count())
            if rows <= 0 or columns <= 0:
                return False
            if (rows, columns) == self._last_pty_size:
                return False
            pty.set_size(rows, columns)
            self._last_pty_size = (rows, columns)
        except Exception:
            return False
        return False

    def _schedule_pty_sync(self) -> None:
        """Coalesce le richieste: durante un trascinamento del divisore
        arrivano decine di allocate al secondo, e non serve toccare il PTY a
        ognuna."""
        if getattr(self, "_resize_pending", 0):
            return
        self._resize_pending = GLib.timeout_add(60, self._sync_pty_size)

    def _on_size_allocate(self, _widget, _allocation) -> None:  # noqa: ANN001
        self._schedule_pty_sync()

    # --------------------------------------------------------------- segnali
    def _on_focus_in(self, _widget, _event) -> bool:  # noqa: ANN001
        if self.on_focus is not None:
            self.on_focus(self)
        self._sync_pty_size()
        return False

    def _on_child_exited(self, _terminal, status) -> None:  # noqa: ANN001
        # Stato di uscita del processo (0 = uscita normale, per esempio "exit" in ssh): la scheda
        # decide se chiudersi da sola o restare aperta con il pulsante di riconnessione.
        self.exit_status = int(status)
        self.connected = False
        if self.on_state is not None:
            self.on_state(self)
        if self.on_close is not None:
            self.on_close(self)

    def _on_title_changed(self, _terminal) -> None:  # noqa: ANN001
        if self.on_title is not None:
            try:
                title = self.terminal.get_window_title() or ""
            except Exception:
                title = ""
            if title:
                self.on_title(self, title)

    def _on_selection_changed(self, _terminal) -> None:  # noqa: ANN001
        if bool(getattr(self.preferences, "terminal_copy_on_select", False)) and self.terminal.get_has_selection():
            self._copy()

    def _on_key_press(self, _terminal, event) -> bool:  # noqa: ANN001
        # Prima diamo al workspace la possibilità di gestire scorciatoie globali
        # (layout, hints, broadcast, nuovo riquadro). Se le consuma, stop.
        if self.on_key is not None:
            try:
                if self.on_key(self, event):
                    return True
            except Exception:
                pass

        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        key = Gdk.keyval_name(event.keyval) or ""
        lower = key.lower()
        if event.keyval == Gdk.KEY_F11:
            self._request("fullscreen")
            return True
        if ctrl and shift and lower == "c":
            self._copy()
            return True
        if ctrl and shift and lower == "v":
            self._paste()
            return True
        if ctrl and shift and lower == "o":
            self._request("split_v")
            return True
        if ctrl and shift and lower == "e":
            self._request("split_h")
            return True
        if ctrl and shift and lower == "w":
            self._request("close_pane")
            return True
        if ctrl and shift and lower == "r":
            self._request("reconnect")
            return True
        if ctrl and shift and lower == "f":
            self.toggle_search()
            return True
        if ctrl and shift and key in {"bracketright", "braceright"}:
            self._request("next_pane")
            return True
        if ctrl and shift and key in {"bracketleft", "braceleft"}:
            self._request("prev_pane")
            return True
        if ctrl and not shift and key in {"plus", "KP_Add", "equal"}:
            self.zoom(1)
            return True
        if ctrl and not shift and key in {"minus", "KP_Subtract"}:
            self.zoom(-1)
            return True
        if ctrl and not shift and key in {"0", "KP_0"}:
            self.zoom(0)
            return True
        return False

    def _request(self, action: str) -> None:
        workspace = self.get_workspace()
        if workspace is not None:
            workspace.dispatch(action, self)

    def get_workspace(self) -> Optional["TerminalWorkspace"]:
        widget = self.get_parent()
        while widget is not None:
            if isinstance(widget, TerminalWorkspace):
                return widget
            widget = widget.get_parent()
        return None

    def _on_button_press(self, _terminal, event) -> bool:  # noqa: ANN001
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        if event.button == 1 and ctrl:
            uri = self._match_uri(event)
            if uri:
                self._open_uri(uri)
                return True
        if event.button != 3:
            return False
        uri = self._match_uri(event)
        menu = Gtk.Menu()
        entries = [
            (tr("Copia"), self._copy, self.terminal.get_has_selection()),
            (tr("Incolla"), self._paste, True),
            (tr("Seleziona tutto"), self.terminal.select_all, True),
        ]
        if uri:
            entries.append((tr("Apri link: {v}").format(v=uri[:48]), lambda u=uri: self._open_uri(u), True))
        entries += [
            (tr("Riconnetti"), lambda: self._request("reconnect"), True),
            (tr("Cerca nello scrollback (Ctrl+Shift+F)"), self.show_search, True),
            (tr("Apri URL da tastiera — hints (Ctrl+Shift+H)"), lambda: self._request("hints"), True),
            (tr("Dividi in verticale (Ctrl+Shift+O)"), lambda: self._request("split_v"), True),
            (tr("Dividi in orizzontale (Ctrl+Shift+E)"), lambda: self._request("split_h"), True),
            (tr("Cambia layout (Ctrl+Shift+L)"), lambda: self._request("cycle_layout"), True),
            (tr("Broadcast input on/off (Ctrl+Shift+P)"), lambda: self._request("toggle_broadcast"), True),
            (tr("Chiudi riquadro (Ctrl+Shift+W)"), lambda: self._request("close_pane"), True),
            (tr("Aumenta testo"), lambda: self.zoom(1), True),
            (tr("Riduci testo"), lambda: self.zoom(-1), True),
            (tr("Testo predefinito"), lambda: self.zoom(0), True),
            (tr("Pulisci schermata"), lambda: self.terminal.feed_child(b"clear\n"), True),
            (tr("Diagnostica terminale"), self.show_diagnostics, True),
            (tr("Schermo intero (F11)"), lambda: self._request("fullscreen"), True),
        ]
        for label, callback, sensitive in entries:
            item = Gtk.MenuItem(label=label)
            item.set_sensitive(bool(sensitive))
            item.connect("activate", lambda _i, cb=callback: cb())
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _match_uri(self, event) -> Optional[str]:  # noqa: ANN001
        # Prima i link OSC 8 "veri" (ls --hyperlink, systemd, gcc…), poi il
        # fallback a regex per gli URL scritti come testo semplice.
        try:
            hyperlink = self.terminal.hyperlink_check_event(event)
            if hyperlink:
                return hyperlink
        except Exception:
            pass
        if self._url_match_tag < 0:
            return None
        try:
            match = self.terminal.match_check_event(event)
        except Exception:
            return None
        if not match:
            return None
        text, tag = match
        if text and tag == self._url_match_tag:
            return text
        return None

    def _open_uri(self, uri: str) -> None:
        try:
            Gtk.show_uri_on_window(self.get_toplevel(), uri, Gdk.CURRENT_TIME)
        except Exception:
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
            except Exception:
                pass


class TerminalWorkspace(Gtk.Box):
    """Contenitore di riquadri terminale con layout multipli.

    Mantiene l'interfaccia pubblica della vecchia ``TerminalTab`` così che il
    resto dell'applicazione (MultiExec, cambio tema, schermo intero, chiusura
    tab) continui a funzionare senza modifiche.
    """

    def __init__(
        self,
        title: str,
        argv: List[str],
        cwd: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        theme: str = "dark",
        settings: Optional[object] = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.get_style_context().add_class("terminal-workspace")
        self.title = title
        self.preferences = settings
        self.theme = theme
        self.fullscreen_active = False

        # Stato: layout corrente, broadcast input, modalità hints.
        self.layout = "stack"
        self.broadcast = False
        # Propagato a tutti i riquadri: seguire il cd della shell richiede
        # che la shell emetta OSC 7, che sugli host remoti va abilitato.
        self.osc7_bootstrap = False
        # Callback verso l'applicazione: (workspace, hostname, path).
        self.on_directory_change: Optional[Callable[["TerminalWorkspace", str, str], None]] = None
        self._hint_active = False
        self._hint_map: Dict[str, str] = {}
        self._hint_buffer = ""
        self._hint_pane: Optional[TerminalPane] = None
        self._toast_timeout_id: Optional[int] = None

        self.panes: List[TerminalPane] = []
        self._root_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Overlay per il "toast": un piccolo avviso temporaneo (in alto a
        # destra) che appare solo al cambio di layout o del broadcast e
        # scompare da solo. Nessuna barra fissa che ruba spazio.
        self._ws_overlay = Gtk.Overlay()
        self._ws_overlay.add(self._root_area)
        self._toast = Gtk.Label()
        self._toast.get_style_context().add_class("terminal-toast")
        self._toast.set_halign(Gtk.Align.END)
        self._toast.set_valign(Gtk.Align.START)
        self._toast.set_margin_top(10)
        self._toast.set_margin_end(12)
        self._toast.set_no_show_all(True)
        self._ws_overlay.add_overlay(self._toast)
        self.pack_start(self._ws_overlay, True, True, 0)

        first = self._new_pane(argv, cwd=cwd, extra_env=extra_env)
        self.panes.append(first)
        self.active_pane = first
        self._relayout()
        GLib.idle_add(lambda: (first.focus(), False)[1])

    # ---------------------------------------------------------- compat props
    @property
    def terminal(self) -> Vte.Terminal:
        """VTE del riquadro attivo (compatibilità con il codice esistente)."""
        pane = self.active_pane if self.active_pane in self.panes else (self.panes[0] if self.panes else None)
        if pane is None:
            return Vte.Terminal()
        return pane.terminal

    # -------------------------------------------------------------- gestione
    def _new_pane(
        self,
        argv: List[str],
        cwd: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> TerminalPane:
        pane = TerminalPane(
            argv,
            cwd=cwd,
            extra_env=extra_env,
            settings=self.preferences,
            theme=self.theme,
        )
        pane.osc7_bootstrap = self.osc7_bootstrap
        self._wire_pane(pane)
        return pane

    def _wire_pane(self, pane: TerminalPane) -> None:
        pane.on_focus = self._on_pane_focus
        pane.on_directory = self._on_pane_directory
        pane.on_close = self._on_pane_exit
        pane.on_title = self._on_pane_title
        pane.on_state = self._on_pane_state
        pane.on_key = self._on_pane_key

    def _on_pane_state(self, _pane: TerminalPane) -> None:
        callback = getattr(self, "on_state_change", None)
        if callable(callback):
            callback(self)

    @property
    def is_connected(self) -> bool:
        """Vero se almeno un riquadro ha un processo attivo."""
        return any(getattr(p, "connected", False) for p in self.panes)

    def reconnect(self) -> None:
        """Riavvia il comando nei riquadri che risultano caduti."""
        target = [p for p in self.panes if not getattr(p, "connected", False)] or list(self.panes)
        for pane in target:
            try:
                pane.respawn()
            except Exception:
                pass

    def _on_pane_directory(self, pane: TerminalPane, host: str, path: str) -> None:
        # Solo il riquadro attivo pilota la sidebar: con gli split aperti, un
        # comando che gira in secondo piano non deve spostare l'albero sotto
        # le mani dell'utente.
        if pane is not self.active_pane:
            return
        if self.on_directory_change is not None:
            self.on_directory_change(self, host, path)

    def _on_pane_focus(self, pane: TerminalPane) -> None:
        if pane in self.panes:
            self.active_pane = pane
            self._update_status()

    def _on_pane_title(self, pane: TerminalPane, title: str) -> None:
        if pane is self.active_pane and title:
            self.title = title

    def _on_pane_exit(self, pane: TerminalPane, explicit: bool = False) -> None:
        """Un riquadro e' finito (processo uscito) o l'utente ha chiesto di chiuderlo.

        Con piu' riquadri si chiude quello; con l'ultimo decide chi ospita il workspace
        (on_last_pane_close), perche' solo lui sa se la scheda va chiusa o tenuta per riconnettere.
        """
        if len(self.panes) > 1:
            self._close_pane(pane)
        else:
            closer = getattr(self, "on_last_pane_close", None)
            if callable(closer):
                closer(self, pane, explicit)

    # --------------------------------------------------- input broadcast/keys
    def _on_pane_key(self, pane: TerminalPane, event: "Gdk.EventKey") -> bool:
        """Intercetta scorciatoie globali del workspace e, se attivo, il
        broadcast dell'input. Ritorna True se l'evento è stato consumato."""
        # In modalità hints, i tasti scelgono l'etichetta.
        if self._hint_active:
            return self._hint_handle_key(event)

        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        key = Gdk.keyval_name(event.keyval) or ""
        lower = key.lower()

        if ctrl and shift and key in {"Return", "KP_Enter"}:
            self.dispatch("new_pane", pane)
            return True
        if ctrl and shift and lower == "l":
            self.dispatch("cycle_layout", pane)
            return True
        if ctrl and shift and lower == "p":
            self.dispatch("toggle_broadcast", pane)
            return True
        if ctrl and shift and lower == "h":
            self.dispatch("hints", pane)
            return True

        # Broadcast: replica i tasti "normali" su tutti i riquadri. Le
        # scorciatoie con Ctrl+Shift restano locali al riquadro attivo.
        if self.broadcast and not (ctrl and shift) and len(self.panes) > 1:
            unicode_val = Gdk.keyval_to_unicode(event.keyval)
            data: Optional[bytes] = None
            if key in {"Return", "KP_Enter"}:
                data = b"\r"
            elif key == "BackSpace":
                data = b"\x7f"
            elif key == "Tab":
                data = b"\t"
            elif key == "Escape":
                data = b"\x1b"
            elif unicode_val and not ctrl:
                try:
                    data = chr(unicode_val).encode("utf-8")
                except Exception:
                    data = None
            if data is not None:
                for p in list(self.panes):
                    try:
                        p.terminal.feed_child(data)
                    except Exception:
                        pass
                return True  # consumato: evita il doppio invio sul riquadro attivo
        return False

    def dispatch(self, action: str, pane: TerminalPane) -> None:
        if pane in self.panes:
            self.active_pane = pane
        if action == "split_v":
            self.split(Gtk.Orientation.HORIZONTAL)  # riquadri affiancati
        elif action == "split_h":
            self.split(Gtk.Orientation.VERTICAL)     # riquadri sopra/sotto
        elif action == "new_pane":
            self.add_pane()
        elif action == "close_pane":
            self._on_pane_exit(pane, explicit=True)
        elif action == "next_pane":
            self._cycle_pane(1)
        elif action == "prev_pane":
            self._cycle_pane(-1)
        elif action == "cycle_layout":
            self.cycle_layout()
        elif action == "toggle_broadcast":
            self.toggle_broadcast()
        elif action == "hints":
            self.start_hints()
        elif action == "fullscreen":
            self.toggle_fullscreen()
        elif action == "reconnect":
            self.reconnect()

    # --------------------------------------------------- creazione riquadri
    def add_pane(self) -> None:
        """Aggiunge un riquadro clonando il comando del riquadro attivo e
        ricalcola il layout corrente (nuovo riquadro)."""
        base = self.active_pane if self.active_pane in self.panes else (self.panes[0] if self.panes else None)
        if base is None:
            return
        new_pane = base.clone_argv()
        self._wire_pane(new_pane)
        self.panes.append(new_pane)
        self.active_pane = new_pane
        # Uno split esplicito ha senso solo con un layout a riquadri visibili;
        # se eravamo in 'stack', passiamo a 'tall' per mostrarli entrambi.
        if self.layout == "stack" and len(self.panes) > 1:
            self.layout = "tall"
        self._relayout()
        self._update_status()
        GLib.idle_add(lambda: (new_pane.focus(), False)[1])

    def split(self, orientation: Gtk.Orientation) -> None:
        """Split esplicito: imposta il layout su 'tall' o 'fat'
        secondo l'orientamento e aggiunge un riquadro."""
        self.layout = "tall" if orientation == Gtk.Orientation.HORIZONTAL else "fat"
        self.add_pane()

    # ---------------------------------------------------------------- layout
    def cycle_layout(self) -> None:
        idx = LAYOUTS.index(self.layout) if self.layout in LAYOUTS else 0
        self.layout = LAYOUTS[(idx + 1) % len(LAYOUTS)]
        self._relayout()
        self._toast_show(tr("Layout: {layout}").format(layout=self.layout))
        if self.active_pane is not None:
            GLib.idle_add(lambda: (self.active_pane.focus(), False)[1])

    def set_layout(self, name: str) -> None:
        if name in LAYOUTS:
            self.layout = name
            self._relayout()
            self._update_status()

    def _clear_root(self) -> None:
        for child in list(self._root_area.get_children()):
            self._root_area.remove(child)

    def _detach_pane_widgets(self) -> None:
        """Rimuove ogni riquadro dal proprio genitore attuale, così da poterlo
        ri-annidare nel nuovo albero di contenitori del layout."""
        for pane in self.panes:
            parent = pane.get_parent()
            if parent is not None:
                try:
                    parent.remove(pane)
                except Exception:
                    pass

    def _relayout(self) -> None:
        """(Ri)costruisce l'albero di ``Gtk.Paned`` in base a ``self.layout``.

        - stack: un solo riquadro visibile alla volta (gli altri restano vivi,
          navigabili con Ctrl+Shift+[ e ]).
        - tall:  primo riquadro grande a sinistra, gli altri impilati a destra.
        - fat:   primo riquadro grande in alto, gli altri affiancati sotto.
        - grid:  griglia il più quadrata possibile.
        """
        self._detach_pane_widgets()
        self._clear_root()
        panes = list(self.panes)
        if not panes:
            return

        if self.layout == "stack" or len(panes) == 1:
            active = self.active_pane if self.active_pane in panes else panes[0]
            self._root_area.pack_start(active, True, True, 0)
            self._root_area.show_all()
            return

        if self.layout == "tall":
            widget = self._build_tall(panes)
        elif self.layout == "fat":
            widget = self._build_fat(panes)
        else:  # grid
            widget = self._build_grid(panes)

        self._root_area.pack_start(widget, True, True, 0)
        self._root_area.show_all()

    def _build_tall(self, panes: List[TerminalPane]) -> Gtk.Widget:
        first, rest = panes[0], panes[1:]
        if not rest:
            return first
        right = self._stack_widgets_vertical(list(rest))
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.pack1(first, True, False)
        paned.pack2(right, True, False)
        GLib.idle_add(lambda: (paned.set_position(max(120, paned.get_allocated_width() // 2)), False)[1])
        return paned

    def _build_fat(self, panes: List[TerminalPane]) -> Gtk.Widget:
        first, rest = panes[0], panes[1:]
        if not rest:
            return first
        bottom = self._stack_widgets_horizontal(list(rest))
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        paned.pack1(first, True, False)
        paned.pack2(bottom, True, False)
        GLib.idle_add(lambda: (paned.set_position(max(80, paned.get_allocated_height() // 2)), False)[1])
        return paned

    def _build_grid(self, panes: List[TerminalPane]) -> Gtk.Widget:
        n = len(panes)
        cols = int(math.ceil(math.sqrt(n)))
        rows = int(math.ceil(n / cols))
        row_widgets: List[Gtk.Widget] = []
        idx = 0
        for _r in range(rows):
            row_panes = panes[idx:idx + cols]
            idx += cols
            if row_panes:
                row_widgets.append(self._stack_widgets_horizontal(list(row_panes)))
        return self._stack_widgets_vertical(row_widgets)

    def _stack_widgets_vertical(self, widgets: List[Gtk.Widget]) -> Gtk.Widget:
        widgets = [w for w in widgets if w is not None]
        if not widgets:
            return Gtk.Box()
        if len(widgets) == 1:
            return widgets[0]
        head, *tail = widgets
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        paned.pack1(head, True, False)
        paned.pack2(self._stack_widgets_vertical(tail), True, False)
        return paned

    def _stack_widgets_horizontal(self, widgets: List[Gtk.Widget]) -> Gtk.Widget:
        widgets = [w for w in widgets if w is not None]
        if not widgets:
            return Gtk.Box()
        if len(widgets) == 1:
            return widgets[0]
        head, *tail = widgets
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.pack1(head, True, False)
        paned.pack2(self._stack_widgets_horizontal(tail), True, False)
        return paned

    def _close_pane(self, pane: TerminalPane) -> None:
        if pane not in self.panes or len(self.panes) <= 1:
            return
        self.panes.remove(pane)
        pane.shutdown()
        if self.active_pane is pane or self.active_pane not in self.panes:
            self.active_pane = self.panes[0]
        self._relayout()
        self._update_status()
        GLib.idle_add(lambda: (self.active_pane.focus(), False)[1])

    def _cycle_pane(self, step: int) -> None:
        if not self.panes:
            return
        try:
            index = self.panes.index(self.active_pane)
        except ValueError:
            index = 0
        target = self.panes[(index + step) % len(self.panes)]
        self.active_pane = target
        # In layout 'stack' cambiare riquadro attivo richiede di ridisegnare.
        if self.layout == "stack":
            self._relayout()
        target.focus()
        self._update_status()

    # ------------------------------------------------------- broadcast input
    def toggle_broadcast(self) -> None:
        self.broadcast = not self.broadcast
        self._toast_show(tr("Broadcast attivo: l'input va a tutti i riquadri") if self.broadcast else tr("Broadcast disattivato"))

    # ----------------------------------------------------------------- hints
    def start_hints(self) -> None:
        """Modalità hints: etichetta ogni URL visibile nel riquadro attivo con
        una lettera; premendo la lettera si apre l'URL. Tutto da tastiera, senza mouse."""
        pane = self.active_pane if self.active_pane in self.panes else (self.panes[0] if self.panes else None)
        if pane is None:
            return
        uris = pane.collect_uris()
        if not uris:
            self._flash_status(tr("Nessun URL visibile per gli hints."))
            return
        labels = self._make_labels(len(uris))
        self._hint_map = {label: uri for label, uri in zip(labels, uris)}
        self._hint_buffer = ""
        self._hint_pane = pane
        self._hint_active = True
        self._render_hints(pane, self._hint_map)
        self._update_status(tr("Hints: premi la lettera per aprire ({n_uris} URL) · Esc annulla").format(n_uris=len(uris)))

    def _make_labels(self, count: int) -> List[str]:
        alpha = _HINT_ALPHABET
        if count <= len(alpha):
            return list(alpha[:count])
        labels = []
        i = 0
        while len(labels) < count:
            labels.append(alpha[i // len(alpha)] + alpha[i % len(alpha)])
            i += 1
        return labels

    def _render_hints(self, pane: TerminalPane, mapping: Dict[str, str]) -> None:
        # VTE non espone coordinate stabili per ogni match: invece di posizionare
        # un badge sopra ogni URL, mostriamo un elenco leggibile label→URL in
        # alto a sinistra, completamente pilotabile da tastiera (in forma di lista).
        overlay = pane._overlay
        if pane._hint_layer is not None:
            try:
                overlay.remove(pane._hint_layer)
            except Exception:
                pass
            pane._hint_layer = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.get_style_context().add_class("terminal-hints")
        box.set_halign(Gtk.Align.START)
        box.set_valign(Gtk.Align.START)
        box.set_margin_top(6)
        box.set_margin_start(6)
        for label, uri in mapping.items():
            row = Gtk.Label()
            row.set_xalign(0.0)
            short = uri if len(uri) <= 64 else uri[:61] + "…"
            row.set_markup(
                tr("<span background='#facc15' foreground='#111111'> {markup_escape_text} </span>  <span foreground='#dbeafe'>{markup_escape_text2}</span>").format(markup_escape_text=GLib.markup_escape_text(label), markup_escape_text2=GLib.markup_escape_text(short))
            )
            box.pack_start(row, False, False, 0)
        pane._hint_layer = box
        overlay.add_overlay(box)
        overlay.show_all()

    def _clear_hints(self) -> None:
        self._hint_active = False
        self._hint_buffer = ""
        pane = self._hint_pane
        self._hint_pane = None
        self._hint_map = {}
        if pane is not None and pane._hint_layer is not None:
            try:
                pane._overlay.remove(pane._hint_layer)
            except Exception:
                pass
            pane._hint_layer = None
            try:
                pane.focus()
            except Exception:
                pass
        self._update_status()

    def _hint_handle_key(self, event: "Gdk.EventKey") -> bool:
        key = Gdk.keyval_name(event.keyval) or ""
        if key == "Escape":
            self._clear_hints()
            return True
        if key == "BackSpace":
            self._hint_buffer = self._hint_buffer[:-1]
            return True
        ch = key.lower()
        if len(ch) == 1 and ch in _HINT_ALPHABET:
            self._hint_buffer += ch
            if self._hint_buffer in self._hint_map:
                uri = self._hint_map[self._hint_buffer]
                pane = self._hint_pane
                self._clear_hints()
                if pane is not None:
                    pane.open_uri(uri)
                return True
            if not any(lbl.startswith(self._hint_buffer) for lbl in self._hint_map):
                self._hint_buffer = ""
            return True
        return True  # in modalità hints assorbe ogni tasto

    # ------------------------------------------------------- avvisi transitori
    def _toast_show(self, text: str, ms: int = 1800) -> None:
        """Mostra un piccolo avviso temporaneo che scompare da solo."""
        self._toast.set_text(text)
        self._toast.show()
        if self._toast_timeout_id is not None:
            try:
                GLib.source_remove(self._toast_timeout_id)
            except Exception:
                pass
        def hide() -> bool:
            self._toast.hide()
            self._toast_timeout_id = None
            return False
        self._toast_timeout_id = GLib.timeout_add(ms, hide)

    def _update_status(self, override: Optional[str] = None) -> None:
        # Compat: chiamato dai vari punti del codice; ora mostra solo un toast
        # se c'è un messaggio esplicito, altrimenti non fa nulla (nessuna
        # barra fissa).
        if override:
            self._toast_show(override)

    def _flash_status(self, text: str) -> None:
        self._toast_show(text)

    # ----------------------------------------------------- reparent helpers
    def _first_pane_in(self, widget: Optional[Gtk.Widget]) -> Optional[TerminalPane]:
        if widget is None:
            return None
        if isinstance(widget, TerminalPane):
            return widget
        if isinstance(widget, Gtk.Paned):
            return self._first_pane_in(widget.get_child1()) or self._first_pane_in(widget.get_child2())
        return None

    # --------------------------------------------------------- preferenze UI
    def apply_preferences(self, settings: object) -> None:
        self.preferences = settings
        for pane in list(self.panes):
            try:
                pane.apply_preferences(settings)
            except Exception:
                pass

    def apply_terminal_theme(self, theme: str) -> None:
        self.theme = theme
        for pane in list(self.panes):
            try:
                pane.apply_terminal_theme(theme)
            except Exception:
                pass

    def stop(self) -> None:
        """Chiude tutti i riquadri: i processi ricevono SIGHUP, i timer vengono cancellati."""
        for pane in list(self.panes):
            pane.shutdown()
        self.panes = []

    def feed_all(self, data: bytes) -> int:
        """Invia ``data`` a tutti i riquadri (usato da MultiExec)."""
        sent = 0
        for pane in list(self.panes):
            try:
                pane.terminal.feed_child(data)
                sent += 1
            except Exception:
                pass
        return sent

    # ----------------------------------------------------------- fullscreen
    def toggle_fullscreen(self) -> None:
        self.fullscreen_active = not self.fullscreen_active
        top = self.get_toplevel()
        immersive = getattr(top, "set_terminal_fullscreen", None)
        if callable(immersive):
            immersive(self, self.fullscreen_active)
            return
        if isinstance(top, Gtk.Window):
            try:
                top.fullscreen() if self.fullscreen_active else top.unfullscreen()
            except Exception:
                pass
