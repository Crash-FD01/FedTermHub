#!/usr/bin/python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""
FedTermHub - gestore integrato di sessioni remote per Fedora Workstation.

Novita' della serie 0.9 (lo storico di ogni rilascio e' nel %changelog di fedtermhub.spec):
- clipboard RDP bidirezionale anche per i FILE: copiando file dentro la sessione
  Windows vengono riconosciuti come FileGroupDescriptorW, mappati su percorsi
  locali via CliprdrFileContext (FUSE) e pubblicati sugli appunti GTK con i
  target text/uri-list e x-special/gnome-copied-files, così si incollano nel
  file manager di Linux (richiede FreeRDP compilato con supporto FUSE);
- protocollo mailbox clipboard alla versione 3, con il campo remote_kind che
  distingue testo e lista file;
- drag&drop verso la sessione RDP: bersaglio di rilascio sempre registrato,
  copia nella «Cartella condivisa» /drive: quando configurata e riscontro
  visibile a schermo dopo ogni rilascio;
- drag&drop file SFTP corretto: gestione manuale di drag-motion/drag-drop/data-received
  (niente più DestDefaults.ALL che bloccava il rilascio), con evidenziazione della
  cartella di destinazione e fallback text/uri-list per Wayland;
- barra RDP a schermo intero trascinabile liberamente in qualunque punto (maniglia
  dedicata, doppio clic per riportarla ancorata in alto al centro);
- network scanner con ARP privilegiato opzionale via pkexec (MAC e produttore);
- port scanner con retry automatico -Pn per host che non rispondono al ping e
  messaggi di stato più chiari quando non emergono porte aperte;
- nuovo backend RDP nativo basato su libfreerdp3 e framebuffer GTK;
- nessun Gtk.Socket, XEmbed, reparenting X11 o finestra xfreerdp;
- funzionamento grafico sia su Wayland sia su X11;
- gestione grafica opzionale delle sessioni Windows tramite WinRM;
- fallback legacy conservato solo quando il backend nativo non è disponibile;
- schermo intero RDP immersivo con barra superiore a scomparsa e risoluzione dinamica;
- centro impostazioni ampliato, terminale configurabile e protezioni automatiche del vault;
- correzione dei colori backdrop/inattivi nel tema scuro;
- drag and drop con anteprima, evidenziazione e animazione di assestamento;
- tema scuro ad alto contrasto con testo sempre leggibile;
- gestore tunnel SSH riprogettato con log, test, auto-avvio e riconnessione;
- sidebar sessioni compatta a riga singola, dettagli contestuali e cartelle colorabili;
- Password Manager ridisegnato con area elenco realmente espandibile, ricerca e azioni contestuali;
- tema chiaro ad alto contrasto con icone simboliche adattive e stati backdrop leggibili;
- gestore tunnel compatto e responsivo con diagramma, menu contestuale e azioni rapide;
- sessioni collegabili a un tunnel SSH con avvio automatico prima della connessione;
- apertura in sequenza di tutte le sessioni contenute in una cartella.
- diagnostica grafica ricostruita con elenco stabile, ricerca, copia ed esportazione;
- nuova identità visiva applicata a finestre, menu applicazioni e task switcher.
- icone applicazione rigenerate con sfondo realmente trasparente e senza bordo bianco;
- cambio tema spostato in un pulsante esterno, senza duplicati nei sottomenu.
- tema chiaro corretto per workspace, schede informative e albero sessioni;
- icona finestra caricata come set multi-risoluzione con profilo esterno trasparente;
- avvio RDP diretto a schermo intero sul monitor selezionato, con fallback sicuro;
- diagnostica connessione contestuale con DNS, route, VPN, porta TCP e suggerimenti operativi.
- Home Session separata dalle impostazioni delle cartelle normali e menu contestuale semplificato.
- instradamento avanzato contestuale con ProxyJump, tunnel salvati e RD Gateway.
- browser SFTP contestuale nella barra laterale delle sessioni SSH, con caricamento lazy.
- Lab temporaneo Linux con VM libvirt transienti e overlay QCOW2 eliminati alla chiusura.
- sessioni RDP apribili in finestre indipendenti, anche direttamente fullscreen sul monitor scelto;
- finestra RDP indipendente gestita da FedTermHub con barra di controllo e monitor selezionato;
- renderer RDP ripristinato al percorso framebuffer completo stabile;
- browser SFTP con alberatura corretta, pulsanti Scarica/Carica e drag-and-drop locale.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import json
import os
import platform
import glob
import pty
import re
import termios
import tempfile
import secrets
import signal
import shlex
import shutil
import socket as net_socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

# La versione si scrive solo qui (e in fedtermhub.spec e nel metainfo): tests/test_versions.py
# controlla che coincidano. Sta in cima perche' il doctor la stampa anche quando GTK non si carica.
APP_ID = "io.github.crash_fd01.fedtermhub"
APP_NAME = "FedTermHub"
APP_VERSION = "0.9.09"

RAW_ARGV = sys.argv[:]
DOCTOR_MODE = "--doctor" in RAW_ARGV or "--doctor-strict" in RAW_ARGV
DOCTOR_STRICT = "--doctor-strict" in RAW_ARGV
SAFE_MODE = "--safe-mode" in RAW_ARGV or os.environ.get("FEDTERMHUB_SAFE_MODE") == "1"

# Il backend RDP nativo disegna direttamente in GTK e non richiede più X11.
# Per diagnosi del vecchio fallback l'utente può ancora forzare esplicitamente X11.
if os.environ.get("FEDTERMHUB_FORCE_X11") == "1" and not os.environ.get("GDK_BACKEND"):
    os.environ["GDK_BACKEND"] = "x11"


def run_doctor_cli(import_error: Optional[BaseException] = None) -> bool:
    """Diagnostica testuale utilizzabile anche se GTK non parte.

    Ritorna True quando i componenti indispensabili dell'applicazione sono
    disponibili. Gli strumenti e i server opzionali non rendono il controllo
    negativo.
    """
    core_ok = import_error is None
    title = f"{APP_NAME} doctor {APP_VERSION}"
    print(title)
    print("=" * len(title))
    print(f"Python: {sys.version.split()[0]}  ({sys.executable})")
    print(f"Sistema: {platform.platform()}")
    print(f"Desktop: XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE', '-')}  XDG_CURRENT_DESKTOP={os.environ.get('XDG_CURRENT_DESKTOP', '-')}  GDK_BACKEND={os.environ.get('GDK_BACKEND', '-')}")
    print(f"Display: DISPLAY={os.environ.get('DISPLAY', '-')}  WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY', '-')}")
    print(f"Safe mode: {'ON' if SAFE_MODE else 'OFF'}")
    print()
    if import_error is not None:
        print(f"GTK/VTE import: ERRORE - {import_error}")
        print("Installa le dipendenze Fedora: sudo dnf install python3-gobject gtk3 vte291")
    else:
        print("GTK/VTE import: OK")
    checks = [
        ("ssh", "openssh-clients"),
        ("sftp", "openssh-clients"),
        ("sshpass", "sshpass"),
        ("xfreerdp", "freerdp"),
        ("xfreerdp3", "freerdp"),
        ("wlfreerdp", "freerdp"),
        ("wlfreerdp3", "freerdp"),
        ("vncviewer", "tigervnc"),
        ("nmap", "nmap"),
        ("tcpdump", "tcpdump"),
        ("ip", "iproute"),
        ("ss", "iproute"),
        ("getent", "glibc-common"),
        ("nmcli", "NetworkManager"),
        ("sshd", "openssh-server"),
        ("vsftpd", "vsftpd"),
        ("in.tftpd", "tftp-server"),
        ("in.telnetd", "telnet-server"),
        ("exportfs", "nfs-utils"),
        ("x0vncserver", "tigervnc-x11-server"),
        ("w0vncserver", "tigervnc-wayland-server"),
        ("vncpasswd", "tigervnc-server-common"),
        ("firewall-cmd", "firewalld"),
        ("virsh", "libvirt-client"),
        ("virt-install", "virt-install"),
        ("virt-viewer", "virt-viewer"),
        ("qemu-img", "qemu-img"),
        ("findmnt", "util-linux"),
        ("mount", "util-linux"),
        ("python3", "python3"),
    ]
    print()
    print("Binari esterni:")
    for binary, pkg in checks:
        path = shutil.which(binary)
        print(f"  {binary:<10} {'OK ' + path if path else 'MANCANTE  (pacchetto: ' + pkg + ')'}")
    print()
    print("Backend RDP nativo:")
    native_status_fn = globals().get("native_backend_status")
    if native_status_fn:
        try:
            native_ok, native_detail = native_status_fn()
        except Exception as exc:
            native_ok, native_detail = False, str(exc)
        print(f"  helper libfreerdp: {'OK' if native_ok else 'ERRORE'} ({native_detail})")
    else:
        print(f"  modulo Python: ERRORE ({globals().get('NATIVE_RDP_IMPORT_ERROR', 'non caricato')})")
    memfd_ok = hasattr(os, "memfd_create")
    print(f"  memfd Linux: {'OK' if memfd_ok else 'MANCANTE'}")
    try:
        cairo_ok = __import__("cairo") is not None
    except Exception:
        cairo_ok = False
    print(f"  rendering Cairo: {'OK' if cairo_ok else 'MANCANTE (python3-cairo)'}")
    core_ok = core_ok and bool(native_status_fn) and bool(native_ok) and memfd_ok and cairo_ok
    pkg = shutil.which("pkg-config")
    if pkg:
        probe = subprocess.run(
            [pkg, "--exists", "freerdp3", "freerdp-client3", "winpr3"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        print(f"  librerie di sviluppo: {'OK' if probe.returncode == 0 else 'MANCANTI (freerdp-devel libwinpr-devel)'}")

    try:
        __import__("winrm")
        winrm_ok = True
    except Exception:
        winrm_ok = False
    print(f"  gestione sessioni Windows/WinRM: {'OK' if winrm_ok else 'OPZIONALE MANCANTE (python3-winrm)'}")

    rdp_binary = shutil.which("xfreerdp3") or shutil.which("xfreerdp")
    print()
    print("Fallback legacy xfreerdp/XEmbed:")
    if rdp_binary and "probe_freerdp" in globals():
        caps = probe_freerdp(rdp_binary)
        print(f"  Client: {caps.version}")
        print(f"  /parent-window: {'OK' if caps.parent_window else 'MANCANTE'}")
    else:
        print("  non disponibile; non necessario al backend nativo")
    print()
    print("Porte seriali rilevate:")
    ports = []
    for pattern in ["/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyS*", "/dev/serial/by-id/*"]:
        ports.extend(glob.glob(pattern))
    if ports:
        for port in sorted(set(ports))[:30]:
            print(f"  {port}")
    else:
        print("  Nessuna porta seriale rilevata ora.")
    print()
    print("Suggerimento: se la grafica ha glitch, avvia con: fedtermhub --safe-mode")
    return core_ok


def strip_private_args(argv: List[str]) -> List[str]:
    return [arg for arg in argv if arg not in {"--doctor", "--doctor-strict", "--safe-mode"}]


try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Vte", "2.91")
    gi.require_version("GLib", "2.0")
    from gi.repository import Gtk, Vte, GLib, Gio, Pango, Gdk, GdkPixbuf  # noqa: E402
except Exception as exc:  # pragma: no cover - dipendenza runtime
    if DOCTOR_MODE:
        doctor_ok = run_doctor_cli(exc)
        raise SystemExit(0 if doctor_ok or not DOCTOR_STRICT else 1)
    print("FedTermHub: impossibile caricare GTK/VTE.", file=sys.stderr)
    print(f"Errore: {exc}", file=sys.stderr)
    print("Su Fedora installa: sudo dnf install python3-gobject gtk3 vte291", file=sys.stderr)
    raise SystemExit(2)

NATIVE_RDP_IMPORT_ERROR: Optional[BaseException] = None
try:
    try:
        from .terminal_panel import TerminalPane, TerminalWorkspace, TERMINAL_PALETTE
    except ImportError:
        from terminal_panel import TerminalPane, TerminalWorkspace, TERMINAL_PALETTE
except Exception as exc:  # pragma: no cover - dipende da Vte a runtime
    TerminalPane = None  # type: ignore[assignment,misc]
    TerminalWorkspace = None  # type: ignore[assignment,misc]
    TERMINAL_PALETTE = ()  # type: ignore[assignment]

try:
    try:
        from .native_rdp import NativeRdpTab, native_backend_status
    except ImportError:
        from native_rdp import NativeRdpTab, native_backend_status
except Exception as exc:  # pragma: no cover - runtime Fedora dependency
    NativeRdpTab = None  # type: ignore[assignment]
    NATIVE_RDP_IMPORT_ERROR = exc

WINDOWS_SESSIONS_IMPORT_ERROR: Optional[BaseException] = None
try:
    try:
        from .windows_sessions import WindowsSessionsDialog
    except ImportError:
        from windows_sessions import WindowsSessionsDialog
except Exception as exc:  # pragma: no cover - optional WinRM integration
    WindowsSessionsDialog = None  # type: ignore[assignment]
    WINDOWS_SESSIONS_IMPORT_ERROR = exc

NETWORK_TOOLS_IMPORT_ERROR: Optional[BaseException] = None
try:
    try:
        from .network_tools import NetworkToolsDialog, RUNTIME as NETWORK_RUNTIME
    except ImportError:
        from network_tools import NetworkToolsDialog, RUNTIME as NETWORK_RUNTIME
except Exception as exc:  # pragma: no cover - optional external network tools
    NetworkToolsDialog = None  # type: ignore[assignment]
    NETWORK_RUNTIME = None  # type: ignore[assignment]
    NETWORK_TOOLS_IMPORT_ERROR = exc

try:
    try:
        from .connection_diagnostics import (
            DiagnosticReport,
            DiagnosticTarget,
            make_target,
            run_connection_diagnostics,
        )
    except ImportError:
        from connection_diagnostics import (
            DiagnosticReport,
            DiagnosticTarget,
            make_target,
            run_connection_diagnostics,
        )
except Exception as exc:  # pragma: no cover - modulo incluso nel pacchetto
    print(f"FedTermHub: diagnostica connessione non disponibile: {exc}", file=sys.stderr)
    DiagnosticReport = None  # type: ignore[assignment,misc]
    DiagnosticTarget = None  # type: ignore[assignment,misc]
    make_target = None  # type: ignore[assignment]
    run_connection_diagnostics = None  # type: ignore[assignment]


REMOTE_FILES_IMPORT_ERROR: Optional[BaseException] = None
try:
    try:
        from .remote_files import RemoteFilesSidebar, SftpConnection
    except ImportError:
        from remote_files import RemoteFilesSidebar, SftpConnection
except Exception as exc:  # pragma: no cover - modulo incluso nel pacchetto
    RemoteFilesSidebar = None  # type: ignore[assignment,misc]
    SftpConnection = None  # type: ignore[assignment,misc]
    REMOTE_FILES_IMPORT_ERROR = exc

EPHEMERAL_LAB_IMPORT_ERROR: Optional[BaseException] = None
try:
    try:
        from .ephemeral_lab import EphemeralLabController, EphemeralLabDialog
    except ImportError:
        from ephemeral_lab import EphemeralLabController, EphemeralLabDialog
except Exception as exc:  # pragma: no cover - modulo incluso nel pacchetto
    EphemeralLabController = None  # type: ignore[assignment,misc]
    EphemeralLabDialog = None  # type: ignore[assignment,misc]
    EPHEMERAL_LAB_IMPORT_ERROR = exc

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    CRYPTO_AVAILABLE = True
except Exception:  # pragma: no cover - dipendenza runtime
    InvalidTag = Exception
    AESGCM = None
    hashes = None
    PBKDF2HMAC = None
    CRYPTO_AVAILABLE = False

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "fedtermhub"
SESSIONS_FILE = CONFIG_DIR / "sessions.json"
TUNNELS_FILE = CONFIG_DIR / "tunnels.json"
VAULT_FILE = CONFIG_DIR / "vault.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "fedtermhub"
LOG_FILE = CACHE_DIR / "fedtermhub.log"
KDF_ITERATIONS = 600_000
APP_ICON_NAME = "fedtermhub"
_APP_ICON_PIXBUFS: List[object] = []
_APP_ICON_FILES: List[Path] = []


def app_icon_paths() -> List[Path]:
    """Return installed/source icon candidates, largest first."""
    sizes = (512, 256, 128, 96, 64, 48, 32, 24, 22, 16)
    roots: List[Path] = []
    source_root = Path(__file__).resolve().parent.parent
    roots.append(source_root / "share" / "icons" / "hicolor")
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    roots.append(data_home / "icons" / "hicolor")
    for item in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"):
        if item:
            roots.append(Path(item) / "icons" / "hicolor")
    candidates: List[Path] = []
    seen: set[str] = set()
    for size in sizes:
        for base in roots:
            path = base / f"{size}x{size}" / "apps" / f"{APP_ICON_NAME}.png"
            key = str(path)
            if key not in seen and path.is_file():
                candidates.append(path)
                seen.add(key)
    for path in [source_root / "share" / "pixmaps" / f"{APP_ICON_NAME}.png", data_home / "pixmaps" / f"{APP_ICON_NAME}.png", Path("/usr/share/pixmaps") / f"{APP_ICON_NAME}.png"]:
        key = str(path)
        if key not in seen and path.is_file():
            candidates.append(path)
            seen.add(key)
    return candidates


def configure_default_app_icon() -> List[object]:
    """Install the branded icon as GTK default for every top-level window."""
    global _APP_ICON_PIXBUFS, _APP_ICON_FILES
    pixbufs: List[object] = []
    files: List[Path] = []
    seen_sizes: set[Tuple[int, int]] = set()
    for path in app_icon_paths():
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(path))
        except Exception:
            continue
        if pixbuf is not None:
            size = (int(pixbuf.get_width()), int(pixbuf.get_height()))
            if size in seen_sizes:
                continue
            seen_sizes.add(size)
            pixbufs.append(pixbuf)
            files.append(path)
    try:
        if pixbufs:
            Gtk.Window.set_default_icon_list(pixbufs)
            if files:
                Gtk.Window.set_default_icon_from_file(str(files[0]))
        else:
            Gtk.Window.set_default_icon_name(APP_ICON_NAME)
    except Exception:
        pass
    _APP_ICON_PIXBUFS = pixbufs
    _APP_ICON_FILES = files
    return pixbufs


def apply_window_icon(window: Gtk.Window) -> None:
    """Apply the complete multi-size icon set to the window.

    Passing only the 512 px file forced some window managers to rescale that
    image down to title-bar size, producing a pale outline around the alpha
    edge.  Supplying every native size lets GTK/KWin choose the exact raster
    and preserves the transparent contour.
    """
    if _APP_ICON_PIXBUFS:
        try:
            window.set_icon_list(_APP_ICON_PIXBUFS)
            return
        except Exception:
            try:
                window.set_icon(_APP_ICON_PIXBUFS[-1])
                return
            except Exception:
                pass
    try:
        window.set_icon_name(APP_ICON_NAME)
    except Exception:
        pass


def normalize_rdp_drive_path(raw: str) -> str:
    """Valida e normalizza la cartella locale condivisa via /drive: FreeRDP.

    L'opzione /drive:nome,percorso usa la virgola come separatore di
    sotto-opzioni e la config del backend nativo usa il newline come
    separatore di riga: questi caratteri nel percorso romperebbero il parsing
    o permetterebbero di iniettare opzioni arbitrarie. Il percorso deve
    inoltre esistere ed essere una cartella, altrimenti FreeRDP fallisce in
    modo poco leggibile solo a connessione avviata.
    """
    candidate = (raw or "").strip()
    if not candidate:
        return ""
    if any(ch in candidate for ch in (",", "\n", "\r")):
        raise ValueError(
            "La cartella condivisa contiene caratteri non supportati (virgola o fine riga)."
        )
    expanded = Path(candidate).expanduser()
    if not expanded.exists():
        raise ValueError(f"La cartella condivisa non esiste: {expanded}")
    if not expanded.is_dir():
        raise ValueError(f"La cartella condivisa non è una cartella: {expanded}")
    return str(expanded)


def split_host_port(text: str, default_port: str = "") -> Tuple[str, str]:
    """Separa host e porta: "host:22", "[fe80::1]:22", "fe80::1" (IPv6 senza porta), "host".

    L'IPv6 con porta va scritto tra parentesi quadre, come in ssh e nei browser: senza parentesi
    i due punti fanno parte dell'indirizzo.
    """
    text = (text or "").strip()
    if text.startswith("["):
        end = text.find("]")
        if end > 0:
            host, rest = text[1:end], text[end + 1:]
            if rest.startswith(":") and rest[1:].isdigit():
                return host, rest[1:]
            return host, default_port
    if text.count(":") == 1:
        host, port = text.split(":", 1)
        if port.isdigit():
            return host, port
    return text, default_port


def host_for_uri(host: str) -> str:
    """IPv6 tra parentesi quadre quando va seguito da :porta."""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def vnc_target(host: str, port: str) -> str:
    """Destinazione per vncviewer: "host:N" e' il display N (porta 5900+N), "host::P" la porta P.

    Il campo della sessione accetta entrambi: un numero piccolo e' un display, uno da 100 in su
    una porta. Prima "5901" veniva passato come display e TigerVNC si collegava alla porta 11801.
    """
    host = host_for_uri(host.strip())
    port = (port or "").strip()
    if not port:
        return host
    if port.isdigit() and int(port) >= 100:
        return f"{host}::{port}"
    return f"{host}:{port}"


@dataclass(frozen=True)
class FreeRdpCapabilities:
    binary: str
    version: str
    parent_window: bool
    args_from: bool
    from_stdin: bool
    grab_keyboard: bool
    grab_mouse: bool


_FREERDP_CAP_CACHE: Dict[str, FreeRdpCapabilities] = {}


def probe_freerdp(binary: str) -> FreeRdpCapabilities:
    """Interroga il client installato invece di presumere opzioni/versione."""
    cached = _FREERDP_CAP_CACHE.get(binary)
    if cached is not None:
        return cached
    env = dict(os.environ)
    env.setdefault("LC_ALL", "C")
    version = "sconosciuta"
    help_text = ""
    try:
        proc = subprocess.run([binary, "/version"], capture_output=True, text=True, timeout=4, env=env)
        version = (proc.stdout or proc.stderr or "").strip().splitlines()[0][:160] or version
    except Exception:
        pass
    try:
        proc = subprocess.run([binary, "/help"], capture_output=True, text=True, timeout=5, env=env)
        help_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except Exception:
        pass
    low = help_text.lower()
    caps = FreeRdpCapabilities(
        binary=binary,
        version=version,
        parent_window="parent-window" in low,
        args_from="args-from" in low,
        from_stdin="from-stdin" in low,
        grab_keyboard="grab-keyboard" in low,
        grab_mouse="grab-mouse" in low,
    )
    _FREERDP_CAP_CACHE[binary] = caps
    return caps



def test_freerdp_args_fd(binary: str) -> Tuple[bool, str]:
    """Verifica davvero il trasporto /args-from:fd senza effettuare connessioni."""
    if not hasattr(os, "memfd_create"):
        return False, "os.memfd_create non disponibile"
    fd: Optional[int] = None
    try:
        fd = os.memfd_create("fedtermhub-doctor-args", flags=0)
        os.write(fd, b"/help\n")
        os.lseek(fd, 0, os.SEEK_SET)
        proc = subprocess.run(
            [binary, f"/args-from:fd:{fd}"],
            pass_fds=(fd,),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=6,
            env={**os.environ, "LC_ALL": "C"},
        )
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        ok = "FreeRDP" in text and ("Usage:" in text or "Syntax:" in text)
        return ok, "OK" if ok else f"uscita {proc.returncode}, help non riconosciuto"
    except Exception as exc:
        return False, str(exc)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

def current_gdk_backend() -> str:
    try:
        display = Gdk.Display.get_default()
        if display is None:
            return "nessun-display"
        gtype = getattr(display, "__gtype__", None)
        name = getattr(gtype, "name", "") if gtype is not None else ""
        return name or type(display).__name__
    except Exception:
        return "sconosciuto"


def is_x11_display() -> bool:
    return "x11" in current_gdk_backend().lower()


@dataclass(frozen=True)
class MonitorDescriptor:
    """Snapshot stabile di un monitor GDK usato nelle sessioni RDP."""

    index: int
    selector: str
    label: str
    manufacturer: str
    model: str
    width: int
    height: int
    scale: int
    primary: bool = False


def _monitor_token(value: object) -> str:
    text = str(value or "").strip()
    return text.replace("|", "_").replace("\n", " ") or "sconosciuto"


def make_monitor_selector(
    index: int, manufacturer: str, model: str, width: int, height: int, scale: int
) -> str:
    """Crea un identificatore leggibile e abbastanza stabile tra i riavvii."""
    return (
        f"monitor:{int(index)}|{_monitor_token(manufacturer)}|{_monitor_token(model)}|"
        f"{int(width)}x{int(height)}|{max(1, int(scale))}"
    )


def enumerate_monitors() -> List[MonitorDescriptor]:
    """Enumera i monitor collegati senza fallire in sessioni headless."""
    display = Gdk.Display.get_default()
    if display is None or not hasattr(display, "get_n_monitors"):
        return []
    try:
        count = int(display.get_n_monitors())
    except Exception:
        return []
    primary_monitor = None
    try:
        primary_monitor = display.get_primary_monitor()
    except Exception:
        pass
    result: List[MonitorDescriptor] = []
    for index in range(max(0, count)):
        try:
            monitor = display.get_monitor(index)
            if monitor is None:
                continue
            geometry = monitor.get_geometry()
            manufacturer = _monitor_token(monitor.get_manufacturer())
            model = _monitor_token(monitor.get_model())
            scale = max(1, int(monitor.get_scale_factor()))
            width = int(geometry.width)
            height = int(geometry.height)
            is_primary = bool(monitor.is_primary())
            if primary_monitor is not None:
                try:
                    is_primary = bool(is_primary or monitor == primary_monitor)
                except Exception:
                    pass
            title = f"Monitor {index + 1}" + (" (principale)" if is_primary else "")
            identity = " ".join(x for x in (manufacturer, model) if x != "sconosciuto")
            suffix = f" — {identity}" if identity else ""
            label = f"{title}{suffix} — {width}×{height}"
            if scale > 1:
                label += f" — scala {scale}×"
            selector = make_monitor_selector(index, manufacturer, model, width, height, scale)
            result.append(
                MonitorDescriptor(
                    index=index, selector=selector, label=label,
                    manufacturer=manufacturer, model=model, width=width, height=height,
                    scale=scale, primary=is_primary,
                )
            )
        except Exception:
            continue
    return result


def current_monitor_index_for_window(
    window: Gtk.Window, monitors: List[MonitorDescriptor]
) -> Optional[int]:
    """Restituisce l'indice del monitor che contiene una specifica finestra."""
    display = Gdk.Display.get_default()
    gdk_window = window.get_window() if window is not None else None
    if display is None or gdk_window is None:
        return None
    try:
        current = display.get_monitor_at_window(gdk_window)
    except Exception:
        return None
    for descriptor in monitors:
        try:
            if display.get_monitor(descriptor.index) == current:
                return descriptor.index
        except Exception:
            continue
    return None


def _parse_monitor_selector(selector: str) -> Optional[Tuple[int, str, str, int, int, int]]:
    if not str(selector).startswith("monitor:"):
        return None
    try:
        index_text, manufacturer, model, size, scale_text = str(selector)[8:].split("|", 4)
        width_text, height_text = size.lower().split("x", 1)
        return (
            int(index_text), manufacturer, model, int(width_text), int(height_text),
            max(1, int(scale_text)),
        )
    except Exception:
        return None


def resolve_monitor_selector(
    selector: str, monitors: List[MonitorDescriptor], current_index: Optional[int] = None
) -> Tuple[Optional[MonitorDescriptor], bool]:
    """Risoluzione robusta: exact match, hardware, indice, quindi fallback."""
    if not monitors:
        return None, False
    requested = str(selector or "current")
    if requested == "current":
        if current_index is not None:
            for item in monitors:
                if item.index == current_index:
                    return item, True
        primary = next((item for item in monitors if item.primary), None)
        return primary or monitors[0], False
    if requested == "primary":
        primary = next((item for item in monitors if item.primary), None)
        return primary or monitors[0], primary is not None
    exact = next((item for item in monitors if item.selector == requested), None)
    if exact is not None:
        return exact, True
    parsed = _parse_monitor_selector(requested)
    if parsed is not None:
        saved_index, manufacturer, model, width, height, scale = parsed
        identifiable = manufacturer != "sconosciuto" or model != "sconosciuto"
        hardware = [
            item for item in monitors
            if identifiable
            and item.manufacturer == manufacturer and item.model == model
            and item.width == width and item.height == height and item.scale == scale
        ]
        if hardware:
            preferred = next((item for item in hardware if item.index == saved_index), None)
            return preferred or hardware[0], True
        same_index = next((item for item in monitors if item.index == saved_index), None)
        if same_index is not None:
            return same_index, False
    if current_index is not None:
        current = next((item for item in monitors if item.index == current_index), None)
        if current is not None:
            return current, False
    primary = next((item for item in monitors if item.primary), None)
    return primary or monitors[0], False


def install_exception_hook() -> None:
    def _hook(exc_type, exc, tb):  # noqa: ANN001
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write("\n--- FedTermHub unhandled exception ---\n")
                handle.write("".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook


install_exception_hook()


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("ascii"))


def ensure_private_config_dir() -> None:
    """Create and harden the per-user state directory.

    Session definitions can contain host names, user names and operational
    notes; keeping the complete directory private avoids leaking metadata to
    other local users. Existing installations are tightened in place.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    for state_file in (SESSIONS_FILE, TUNNELS_FILE, VAULT_FILE, SETTINGS_FILE):
        if state_file.is_file():
            try:
                os.chmod(state_file, 0o600)
            except OSError:
                pass


def atomic_write_text(path: Path, text: str, mode: Optional[int] = None) -> None:
    """Atomically replace a UTF-8 text file without a predictable temp name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        if mode is not None:
            os.chmod(path, mode)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


class X11FocusBridge:
    """Best-effort focus bridge for XEmbed clients such as xfreerdp.

    Gtk.Socket embeds the FreeRDP X11 window, but some compositors leave keyboard
    focus on the GTK container.  This helper locates the deepest child X window
    and asks X11 to focus it.  Failures are intentionally non-fatal.
    """

    _lib = None
    _ready = False

    @classmethod
    def _load(cls) -> bool:
        if cls._ready:
            return cls._lib is not None
        cls._ready = True
        try:
            name = ctypes.util.find_library("X11") or "libX11.so.6"
            lib = ctypes.CDLL(name)
            lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            lib.XOpenDisplay.restype = ctypes.c_void_p
            lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
            lib.XQueryTree.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
                ctypes.POINTER(ctypes.c_uint),
            ]
            lib.XQueryTree.restype = ctypes.c_int
            lib.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            lib.XSetInputFocus.restype = ctypes.c_int
            lib.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            lib.XRaiseWindow.restype = ctypes.c_int
            lib.XFlush.argtypes = [ctypes.c_void_p]
            lib.XFlush.restype = ctypes.c_int
            lib.XFree.argtypes = [ctypes.c_void_p]
            lib.XFree.restype = ctypes.c_int
            cls._lib = lib
        except Exception:
            cls._lib = None
        return cls._lib is not None

    @classmethod
    def _deepest_child(cls, display, window_id: int, max_depth: int = 5) -> int:
        current = int(window_id)
        for _ in range(max_depth):
            root = ctypes.c_ulong()
            parent = ctypes.c_ulong()
            children = ctypes.POINTER(ctypes.c_ulong)()
            count = ctypes.c_uint()
            ok = cls._lib.XQueryTree(
                display, ctypes.c_ulong(current), ctypes.byref(root), ctypes.byref(parent),
                ctypes.byref(children), ctypes.byref(count)
            )
            if not ok or count.value == 0 or not children:
                break
            try:
                # The most recently created child is normally the xfreerdp plug.
                current = int(children[count.value - 1])
            finally:
                cls._lib.XFree(children)
        return current

    @classmethod
    def focus(cls, socket_xid: int) -> bool:
        if socket_xid <= 0 or not cls._load():
            return False
        display = cls._lib.XOpenDisplay(None)
        if not display:
            return False
        try:
            target = cls._deepest_child(display, socket_xid)
            cls._lib.XRaiseWindow(display, ctypes.c_ulong(target))
            # RevertToParent=2, CurrentTime=0.
            cls._lib.XSetInputFocus(display, ctypes.c_ulong(target), 2, 0)
            cls._lib.XFlush(display)
            return True
        except Exception:
            return False
        finally:
            cls._lib.XCloseDisplay(display)


class X11EmbedBridge:
    """Compatibilità X11 per client che non completano correttamente XEmbed.

    Alcune build recenti di xfreerdp accettano /parent-window ma non inviano
    sempre il messaggio XEmbed che fa scattare Gtk.Socket::plug-added.  Questa
    classe verifica l'albero X11 reale e, se necessario, trova la finestra del
    processo tramite _NET_WM_PID/titolo e la riposiziona nel Gtk.Socket.

    Tutte le operazioni sono best-effort e usano una connessione X11 separata,
    così un errore su una finestra appena distrutta non compromette GTK.
    """

    class _XWindowAttributes(ctypes.Structure):
        _fields_ = [
            ("x", ctypes.c_int), ("y", ctypes.c_int),
            ("width", ctypes.c_int), ("height", ctypes.c_int),
            ("border_width", ctypes.c_int), ("depth", ctypes.c_int),
            ("visual", ctypes.c_void_p), ("root", ctypes.c_ulong),
            ("window_class", ctypes.c_int), ("bit_gravity", ctypes.c_int),
            ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int),
            ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong),
            ("save_under", ctypes.c_int), ("colormap", ctypes.c_ulong),
            ("map_installed", ctypes.c_int), ("map_state", ctypes.c_int),
            ("all_event_masks", ctypes.c_long), ("your_event_mask", ctypes.c_long),
            ("do_not_propagate_mask", ctypes.c_long),
            ("override_redirect", ctypes.c_int), ("screen", ctypes.c_void_p),
        ]

    _lib = None
    _ready = False
    _error_handler = None

    @classmethod
    def _load(cls) -> bool:
        if cls._ready:
            return cls._lib is not None
        cls._ready = True
        try:
            name = ctypes.util.find_library("X11") or "libX11.so.6"
            lib = ctypes.CDLL(name)
            display_t = ctypes.c_void_p
            window_t = ctypes.c_ulong
            atom_t = ctypes.c_ulong
            lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            lib.XOpenDisplay.restype = display_t
            lib.XCloseDisplay.argtypes = [display_t]
            lib.XCloseDisplay.restype = ctypes.c_int
            lib.XDefaultRootWindow.argtypes = [display_t]
            lib.XDefaultRootWindow.restype = window_t
            lib.XQueryTree.argtypes = [
                display_t, window_t, ctypes.POINTER(window_t), ctypes.POINTER(window_t),
                ctypes.POINTER(ctypes.POINTER(window_t)), ctypes.POINTER(ctypes.c_uint),
            ]
            lib.XQueryTree.restype = ctypes.c_int
            lib.XInternAtom.argtypes = [display_t, ctypes.c_char_p, ctypes.c_int]
            lib.XInternAtom.restype = atom_t
            lib.XGetWindowProperty.argtypes = [
                display_t, window_t, atom_t, ctypes.c_long, ctypes.c_long,
                ctypes.c_int, atom_t, ctypes.POINTER(atom_t), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
            ]
            lib.XGetWindowProperty.restype = ctypes.c_int
            lib.XFetchName.argtypes = [display_t, window_t, ctypes.POINTER(ctypes.c_char_p)]
            lib.XFetchName.restype = ctypes.c_int
            lib.XGetWindowAttributes.argtypes = [
                display_t, window_t, ctypes.POINTER(cls._XWindowAttributes)
            ]
            lib.XGetWindowAttributes.restype = ctypes.c_int
            lib.XReparentWindow.argtypes = [display_t, window_t, window_t, ctypes.c_int, ctypes.c_int]
            lib.XReparentWindow.restype = ctypes.c_int
            lib.XMoveResizeWindow.argtypes = [
                display_t, window_t, ctypes.c_int, ctypes.c_int,
                ctypes.c_uint, ctypes.c_uint,
            ]
            lib.XMoveResizeWindow.restype = ctypes.c_int
            lib.XClearArea.argtypes = [
                display_t, window_t, ctypes.c_int, ctypes.c_int,
                ctypes.c_uint, ctypes.c_uint, ctypes.c_int,
            ]
            lib.XClearArea.restype = ctypes.c_int
            lib.XGetImage.argtypes = [
                display_t, window_t, ctypes.c_int, ctypes.c_int,
                ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int,
            ]
            lib.XGetImage.restype = ctypes.c_void_p
            lib.XGetPixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            lib.XGetPixel.restype = ctypes.c_ulong
            lib.XDestroyImage.argtypes = [ctypes.c_void_p]
            lib.XDestroyImage.restype = ctypes.c_int
            lib.XMapRaised.argtypes = [display_t, window_t]
            lib.XMapRaised.restype = ctypes.c_int
            lib.XAddToSaveSet.argtypes = [display_t, window_t]
            lib.XAddToSaveSet.restype = ctypes.c_int
            lib.XSetInputFocus.argtypes = [display_t, window_t, ctypes.c_int, ctypes.c_ulong]
            lib.XSetInputFocus.restype = ctypes.c_int
            lib.XRaiseWindow.argtypes = [display_t, window_t]
            lib.XRaiseWindow.restype = ctypes.c_int
            lib.XSync.argtypes = [display_t, ctypes.c_int]
            lib.XSync.restype = ctypes.c_int
            lib.XFlush.argtypes = [display_t]
            lib.XFlush.restype = ctypes.c_int
            lib.XFree.argtypes = [ctypes.c_void_p]
            lib.XFree.restype = ctypes.c_int
            # Evita che un BadWindow asincrono su una finestra appena chiusa
            # possa terminare l'intero programma.
            callback_t = ctypes.CFUNCTYPE(ctypes.c_int, display_t, ctypes.c_void_p)
            cls._error_handler = callback_t(lambda _display, _event: 0)
            lib.XSetErrorHandler.argtypes = [callback_t]
            lib.XSetErrorHandler.restype = ctypes.c_void_p
            lib.XSetErrorHandler(cls._error_handler)
            cls._lib = lib
        except Exception:
            cls._lib = None
        return cls._lib is not None

    @classmethod
    def _open(cls):
        if not cls._load():
            return None
        return cls._lib.XOpenDisplay(None)

    @classmethod
    def _children(cls, display, window_id: int) -> List[int]:
        root = ctypes.c_ulong()
        parent = ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        ok = cls._lib.XQueryTree(
            display, ctypes.c_ulong(window_id), ctypes.byref(root), ctypes.byref(parent),
            ctypes.byref(children), ctypes.byref(count),
        )
        if not ok or not children or count.value == 0:
            return []
        try:
            return [int(children[index]) for index in range(count.value)]
        finally:
            cls._lib.XFree(children)

    @classmethod
    def _parent(cls, display, window_id: int) -> int:
        root = ctypes.c_ulong()
        parent = ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        ok = cls._lib.XQueryTree(
            display, ctypes.c_ulong(window_id), ctypes.byref(root), ctypes.byref(parent),
            ctypes.byref(children), ctypes.byref(count),
        )
        if children:
            cls._lib.XFree(children)
        return int(parent.value) if ok else 0

    @classmethod
    def _title(cls, display, window_id: int) -> str:
        value = ctypes.c_char_p()
        try:
            if cls._lib.XFetchName(display, ctypes.c_ulong(window_id), ctypes.byref(value)) and value.value:
                return value.value.decode("utf-8", errors="replace")
        except Exception:
            pass
        finally:
            if value:
                try:
                    cls._lib.XFree(value)
                except Exception:
                    pass
        return ""

    @classmethod
    def _pid(cls, display, window_id: int, atom_pid: int, atom_cardinal: int) -> Optional[int]:
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        item_count = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        try:
            rc = cls._lib.XGetWindowProperty(
                display, ctypes.c_ulong(window_id), ctypes.c_ulong(atom_pid), 0, 1, 0,
                ctypes.c_ulong(atom_cardinal), ctypes.byref(actual_type),
                ctypes.byref(actual_format), ctypes.byref(item_count),
                ctypes.byref(bytes_after), ctypes.byref(data),
            )
            if rc == 0 and item_count.value and data:
                return int(ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[0])
        except Exception:
            return None
        finally:
            if data:
                try:
                    cls._lib.XFree(data)
                except Exception:
                    pass
        return None

    @classmethod
    def _geometry(cls, display, window_id: int) -> Tuple[int, int, int]:
        attrs = cls._XWindowAttributes()
        try:
            if cls._lib.XGetWindowAttributes(display, ctypes.c_ulong(window_id), ctypes.byref(attrs)):
                return max(0, attrs.width), max(0, attrs.height), int(attrs.map_state)
        except Exception:
            pass
        return 0, 0, 0

    @classmethod
    def direct_client(cls, parent_xid: int, process_pid: int = 0) -> int:
        """Restituisce una finestra FreeRDP già figlia del contenitore."""
        if parent_xid <= 0:
            return 0
        display = cls._open()
        if not display:
            return 0
        try:
            atom_pid = cls._lib.XInternAtom(display, b"_NET_WM_PID", 0)
            atom_cardinal = cls._lib.XInternAtom(display, b"CARDINAL", 0)
            candidates = []
            for child in cls._children(display, parent_xid):
                title = cls._title(display, child).lower()
                pid = cls._pid(display, child, atom_pid, atom_cardinal)
                width, height, mapped = cls._geometry(display, child)
                score = width * height
                if process_pid and pid == process_pid:
                    score += 10_000_000
                if "freerdp" in title:
                    score += 5_000_000
                if mapped:
                    score += 1_000_000
                candidates.append((score, child))
            return max(candidates)[1] if candidates else 0
        except Exception:
            return 0
        finally:
            cls._lib.XCloseDisplay(display)

    @classmethod
    def find_process_window(cls, process_pid: int, title_token: str = "", exclude_xid: int = 0) -> int:
        """Trova la migliore finestra top-level appartenente a process_pid."""
        if process_pid <= 0:
            return 0
        display = cls._open()
        if not display:
            return 0
        try:
            root = int(cls._lib.XDefaultRootWindow(display))
            atom_pid = cls._lib.XInternAtom(display, b"_NET_WM_PID", 0)
            atom_cardinal = cls._lib.XInternAtom(display, b"CARDINAL", 0)
            queue = [root]
            visited = set()
            candidates = []
            token = title_token.lower().strip()
            while queue and len(visited) < 8192:
                current = queue.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                children = cls._children(display, current)
                queue.extend(children)
                if current in {root, exclude_xid}:
                    continue
                pid = cls._pid(display, current, atom_pid, atom_cardinal)
                title = cls._title(display, current)
                if pid != process_pid and not (token and token in title.lower()):
                    continue
                width, height, mapped = cls._geometry(display, current)
                parent = cls._parent(display, current)
                score = width * height
                if pid == process_pid:
                    score += 10_000_000
                if token and token in title.lower():
                    score += 20_000_000
                if parent == root:
                    score += 5_000_000
                if mapped:
                    score += 1_000_000
                candidates.append((score, current))
            return max(candidates)[1] if candidates else 0
        except Exception:
            return 0
        finally:
            cls._lib.XCloseDisplay(display)

    @classmethod
    def reparent(cls, child_xid: int, parent_xid: int, width: int, height: int) -> bool:
        if child_xid <= 0 or parent_xid <= 0:
            return False
        display = cls._open()
        if not display:
            return False
        try:
            cls._lib.XAddToSaveSet(display, ctypes.c_ulong(child_xid))
            cls._lib.XReparentWindow(
                display, ctypes.c_ulong(child_xid), ctypes.c_ulong(parent_xid), 0, 0
            )
            cls._lib.XMoveResizeWindow(
                display, ctypes.c_ulong(child_xid), 0, 0,
                max(64, int(width)), max(64, int(height)),
            )
            cls._lib.XMapRaised(display, ctypes.c_ulong(child_xid))
            cls._lib.XSync(display, 0)
            return cls._parent(display, child_xid) == parent_xid
        except Exception:
            return False
        finally:
            cls._lib.XCloseDisplay(display)

    @classmethod
    def resize(cls, child_xid: int, width: int, height: int) -> bool:
        if child_xid <= 0:
            return False
        display = cls._open()
        if not display:
            return False
        try:
            cls._lib.XMoveResizeWindow(
                display, ctypes.c_ulong(child_xid), 0, 0,
                max(64, int(width)), max(64, int(height)),
            )
            cls._lib.XFlush(display)
            return True
        except Exception:
            return False
        finally:
            cls._lib.XCloseDisplay(display)

    @classmethod
    def is_probably_black(cls, child_xid: int) -> Optional[bool]:
        """Campiona il framebuffer X11; None significa che il test non è disponibile."""
        if child_xid <= 0:
            return None
        display = cls._open()
        if not display:
            return None
        image = None
        try:
            width, height, mapped = cls._geometry(display, child_xid)
            if not mapped or width < 64 or height < 64:
                return None
            sample_w = min(96, width)
            sample_h = min(72, height)
            x = max(0, (width - sample_w) // 2)
            y = max(0, (height - sample_h) // 2)
            # AllPlanes + ZPixmap.
            image = cls._lib.XGetImage(
                display, ctypes.c_ulong(child_xid), x, y,
                sample_w, sample_h, ctypes.c_ulong(~0 & 0xFFFFFFFFFFFFFFFF), 2,
            )
            if not image:
                return None
            total = 0
            dark = 0
            step_x = max(1, sample_w // 12)
            step_y = max(1, sample_h // 9)
            for py in range(0, sample_h, step_y):
                for px in range(0, sample_w, step_x):
                    pixel = int(cls._lib.XGetPixel(image, px, py))
                    total += 1
                    # Su visual TrueColor il nero è zero; tolleriamo rumore minimo.
                    rgb = pixel & 0x00FFFFFF
                    channels = ((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF)
                    if pixel == 0 or max(channels) <= 8:
                        dark += 1
            return total > 0 and (dark / total) >= 0.97
        except Exception:
            return None
        finally:
            if image:
                try:
                    cls._lib.XDestroyImage(image)
                except Exception:
                    pass
            cls._lib.XCloseDisplay(display)

    @classmethod
    def refresh(cls, child_xid: int, width: int, height: int) -> bool:
        """Forza ConfigureNotify/Expose sul client incorporato.

        Alcune build xfreerdp inizializzano il framebuffer prima che il plug X11
        abbia la dimensione finale. Un piccolo resize seguito da XClearArea forza
        il client a ridisegnare senza interrompere la sessione.
        """
        if child_xid <= 0:
            return False
        display = cls._open()
        if not display:
            return False
        try:
            w = max(320, int(width))
            h = max(240, int(height))
            cls._lib.XMapRaised(display, ctypes.c_ulong(child_xid))
            cls._lib.XMoveResizeWindow(
                display, ctypes.c_ulong(child_xid), 0, 0,
                max(319, w - 1), max(239, h - 1),
            )
            cls._lib.XSync(display, 0)
            cls._lib.XMoveResizeWindow(
                display, ctypes.c_ulong(child_xid), 0, 0, w, h,
            )
            cls._lib.XClearArea(
                display, ctypes.c_ulong(child_xid), 0, 0, 0, 0, 1,
            )
            cls._lib.XRaiseWindow(display, ctypes.c_ulong(child_xid))
            cls._lib.XSync(display, 0)
            return True
        except Exception:
            return False
        finally:
            cls._lib.XCloseDisplay(display)

    @classmethod
    def focus(cls, window_xid: int) -> bool:
        if window_xid <= 0:
            return False
        display = cls._open()
        if not display:
            return False
        try:
            cls._lib.XRaiseWindow(display, ctypes.c_ulong(window_xid))
            cls._lib.XSetInputFocus(display, ctypes.c_ulong(window_xid), 2, 0)
            cls._lib.XFlush(display)
            return True
        except Exception:
            return False
        finally:
            cls._lib.XCloseDisplay(display)


@dataclass
class Credential:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "Nuova credenziale"
    username: str = ""
    domain: str = ""
    password: str = ""
    notes: str = ""
    # Raggruppamento libero scelto dall'utente, di solito il cliente. Vuoto = "Senza gruppo".
    # Le versioni precedenti non lo conoscono: lo ignorano in lettura.
    group: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "Credential":
        return cls(
            id=str(data.get("id", uuid.uuid4().hex)),
            name=str(data.get("name", "Credenziale")),
            username=str(data.get("username", "")),
            domain=str(data.get("domain", "")),
            password=str(data.get("password", "")),
            notes=str(data.get("notes", "")),
            group=str(data.get("group", "")).strip(),
        )

    def to_dict(self) -> Dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "username": self.username,
            "domain": self.domain,
            "password": self.password,
            "notes": self.notes,
            "group": self.group,
        }

    def account_text(self) -> str:
        """Utente come lo scrive Windows (DOMINIO\\utente) o semplice, per elenchi e riepiloghi."""
        if self.domain and self.username:
            return f"{self.domain}\\{self.username}"
        return self.username or self.domain

    def sort_key(self) -> Tuple[int, str, str]:
        # I gruppi in ordine alfabetico, "Senza gruppo" per ultimo; dentro il gruppo per nome.
        return (0 if self.group else 1, self.group.casefold(), self.name.casefold())


PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
PASSWORD_SYMBOLS = "!#$%&*+-=?@_"


def generate_password(length: int = 20, symbols: bool = True) -> str:
    """Password casuale senza caratteri che si confondono (0/O, 1/l/I).

    Contiene sempre almeno una maiuscola, una minuscola, una cifra e, se richiesto, un simbolo:
    molti sistemi (Active Directory compreso) rifiutano password che non li hanno.
    """
    length = max(8, min(int(length), 128))
    pools = [
        "ABCDEFGHJKLMNPQRSTUVWXYZ",
        "abcdefghijkmnopqrstuvwxyz",
        "23456789",
    ] + ([PASSWORD_SYMBOLS] if symbols else [])
    alphabet = PASSWORD_ALPHABET + (PASSWORD_SYMBOLS if symbols else "")
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if all(any(ch in pool for ch in candidate) for pool in pools):
            return candidate


def password_strength(password: str) -> Tuple[int, str]:
    """Stima semplice della robustezza: (0-4, descrizione).

    Non sostituisce zxcvbn, ma riconosce i casi che contano per l'utente medio: password corte,
    di un solo tipo di carattere o fatte di un carattere ripetuto.
    """
    if not password:
        return 0, ""
    kinds = sum(
        1
        for test in (str.islower, str.isupper, str.isdigit)
        if any(test(ch) for ch in password)
    ) + (1 if any(not ch.isalnum() for ch in password) else 0)
    length = len(password)
    unique = len(set(password))
    if length < 8 or unique <= 2:
        score = 0
    elif length < 10 or kinds <= 1:
        score = 1
    elif length < 12 or kinds == 2:
        score = 2
    elif length < 16 or kinds == 3:
        score = 3
    else:
        score = 4
    return score, ("Molto debole", "Debole", "Discreta", "Buona", "Ottima")[score]


class PasswordVault:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.credentials: List[Credential] = []
        self.unlocked = False
        self._key: Optional[bytes] = None
        self._salt: Optional[bytes] = None
        self._iterations = KDF_ITERATIONS

    def exists(self) -> bool:
        return self.path.exists()

    @staticmethod
    def derive_key(password: str, salt: bytes, iterations: int) -> bytes:
        if not CRYPTO_AVAILABLE or PBKDF2HMAC is None or hashes is None:
            raise RuntimeError("python3-cryptography non disponibile")
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=iterations,
        )
        return kdf.derive(password.encode("utf-8"))

    def create(self, master_password: str) -> None:
        if not master_password:
            raise ValueError("Master password vuota non consentita.")
        self._salt = secrets.token_bytes(16)
        self._iterations = KDF_ITERATIONS
        self._key = self.derive_key(master_password, self._salt, self._iterations)
        self.credentials = []
        self.unlocked = True
        self.save()

    def unlock(self, master_password: str) -> None:
        if not self.path.exists():
            raise FileNotFoundError("Password Manager non ancora creato.")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        kdf = raw.get("kdf", {})
        cipher = raw.get("cipher", {})
        salt = b64d(str(kdf.get("salt", "")))
        iterations = int(kdf.get("iterations", KDF_ITERATIONS))
        nonce = b64d(str(cipher.get("nonce", "")))
        payload = b64d(str(raw.get("payload", "")))
        key = self.derive_key(master_password, salt, iterations)
        aes = AESGCM(key)
        plain = aes.decrypt(nonce, payload, None)
        data = json.loads(plain.decode("utf-8"))
        credentials = data.get("credentials", [])
        if not isinstance(credentials, list):
            raise ValueError("Archivio delle password non valido.")
        self.credentials = [Credential.from_dict(x) for x in credentials if isinstance(x, dict)]
        self._salt = salt
        self._iterations = iterations
        self._key = key
        self.unlocked = True

    def lock(self) -> None:
        self.credentials = []
        self._key = None
        self.unlocked = False

    def save(self) -> None:
        if not self.unlocked or self._key is None or self._salt is None:
            raise RuntimeError("Password Manager bloccato.")
        ensure_private_config_dir()
        plain = json.dumps(
            {
                "version": APP_VERSION,
                "credentials": [cred.to_dict() for cred in self.credentials],
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        nonce = secrets.token_bytes(12)
        aes = AESGCM(self._key)
        encrypted = aes.encrypt(nonce, plain, None)
        data = {
            "version": APP_VERSION,
            "format": "FedTermHub encrypted credential vault",
            "kdf": {
                "name": "PBKDF2-HMAC-SHA256",
                "iterations": self._iterations,
                "salt": b64e(self._salt),
            },
            "cipher": {
                "name": "AES-256-GCM",
                "nonce": b64e(nonce),
            },
            "payload": b64e(encrypted),
        }
        atomic_write_text(self.path, json.dumps(data, indent=2, ensure_ascii=False), mode=0o600)

    def get(self, credential_id: str) -> Optional[Credential]:
        for cred in self.credentials:
            if cred.id == credential_id:
                return cred
        return None

    def sorted_credentials(self) -> List[Credential]:
        return sorted(self.credentials, key=Credential.sort_key)

    def groups(self) -> List[str]:
        return sorted({cred.group for cred in self.credentials if cred.group}, key=str.casefold)

    def labels(self) -> List[Tuple[str, str]]:
        # Nelle tendine delle sessioni: stesso ordine della finestra Password, con il gruppo
        # accanto al nome perche' credenziali di clienti diversi hanno spesso lo stesso nome.
        return [
            (cred.id, f"{cred.name}  ·  {cred.group}" if cred.group else cred.name)
            for cred in self.sorted_credentials()
        ]

    def verify_master_password(self, master_password: str) -> bool:
        """Controlla la master password sul file su disco, senza toccare lo stato in memoria."""
        probe = PasswordVault(self.path)
        try:
            probe.unlock(master_password)
            return True
        except InvalidTag:
            return False
        finally:
            probe.lock()

    def change_master_password(self, new_password: str) -> None:
        """Ricifra l'archivio con una nuova master password (salt nuovo, chiave nuova)."""
        if not self.unlocked:
            raise RuntimeError("Password Manager bloccato.")
        if not new_password:
            raise ValueError("Master password vuota non consentita.")
        salt = secrets.token_bytes(16)
        key = self.derive_key(new_password, salt, KDF_ITERATIONS)
        previous = (self._salt, self._iterations, self._key)
        self._salt, self._iterations, self._key = salt, KDF_ITERATIONS, key
        try:
            self.save()
        except Exception:
            self._salt, self._iterations, self._key = previous
            raise

    @staticmethod
    def encrypt_credentials_payload(credentials: List[Credential], master_password: str) -> Dict[str, object]:
        if not master_password:
            raise ValueError("Master password vuota non consentita.")
        salt = secrets.token_bytes(16)
        iterations = KDF_ITERATIONS
        key = PasswordVault.derive_key(master_password, salt, iterations)
        nonce = secrets.token_bytes(12)
        payload = json.dumps(
            {
                "version": APP_VERSION,
                "credentials": [cred.to_dict() for cred in credentials],
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        encrypted = AESGCM(key).encrypt(nonce, payload, None)
        return {
            "version": APP_VERSION,
            "format": "FedTermHub encrypted credential export",
            "warning": "Contiene credenziali cifrate. Serve la master password scelta in esportazione.",
            "kdf": {"name": "PBKDF2-HMAC-SHA256", "iterations": iterations, "salt": b64e(salt)},
            "cipher": {"name": "AES-256-GCM", "nonce": b64e(nonce)},
            "payload": b64e(encrypted),
        }

    @staticmethod
    def decrypt_credentials_payload(raw: Dict[str, object], master_password: str) -> List[Credential]:
        if str(raw.get("format", "")) not in {"FedTermHub encrypted credential export", "FedTermHub encrypted credential vault"}:
            raise ValueError("Formato export credenziali non riconosciuto.")
        kdf = raw.get("kdf", {})
        cipher = raw.get("cipher", {})
        if not isinstance(kdf, dict) or not isinstance(cipher, dict):
            raise ValueError("File cifrato non valido.")
        salt = b64d(str(kdf.get("salt", "")))
        iterations = int(kdf.get("iterations", KDF_ITERATIONS))
        nonce = b64d(str(cipher.get("nonce", "")))
        payload = b64d(str(raw.get("payload", "")))
        key = PasswordVault.derive_key(master_password, salt, iterations)
        plain = AESGCM(key).decrypt(nonce, payload, None)
        data = json.loads(plain.decode("utf-8"))
        credentials = data.get("credentials", [])
        if not isinstance(credentials, list):
            raise ValueError("Payload credenziali non valido.")
        return [Credential.from_dict(x) for x in credentials if isinstance(x, dict)]


@dataclass
class AppSettings:
    """Preferenze persistenti dell'applicazione.

    Le impostazioni sono volutamente conservative: valori non validi o file di
    versioni precedenti vengono migrati senza impedire l'avvio del programma.
    """

    theme: str = "dark"  # dark oppure light
    icon_theme: str = "auto"  # auto, Papirus-Dark, Papirus, Adwaita, hicolor

    # Layout
    sidebar_width: int = 238
    window_width: int = 1280
    window_height: int = 800
    toolbar_icon_size: int = 22
    show_toolbar_labels: bool = True
    show_left_rail: bool = True
    show_sidebar: bool = True
    show_menubar: bool = True
    show_ribbon: bool = True
    show_statusbar: bool = True
    show_workspace_hint: bool = True
    compact_tabs: bool = True
    animations: bool = True
    startup_mode: str = "terminal"  # terminal oppure empty

    # Terminale
    terminal_font_family: str = "Monospace"
    terminal_font_size: int = 10
    terminal_scrollback_lines: int = 20000
    terminal_cursor_shape: str = "block"  # block, ibeam, underline
    terminal_cursor_blink: bool = True
    terminal_copy_on_select: bool = False
    terminal_scroll_on_output: bool = False
    terminal_audible_bell: bool = False
    terminal_shell: str = ""  # vuoto = shell utente
    terminal_start_directory: str = ""  # vuoto = home

    # Connessioni
    show_error_popups: bool = True
    open_error_tabs: bool = True
    notification_duration_ms: int = 3500
    rdp_preflight: bool = True
    connection_timeout_seconds: int = 12
    # L'albero dei file remoti segue il cd della shell (via OSC 7).
    follow_remote_directory: bool = True
    ssh_keepalive_interval: int = 30
    ssh_keepalive_count: int = 3
    ssh_compression: bool = False
    ssh_strict_host_key: str = "ask"  # ask, accept-new, yes, no

    # Sicurezza
    vault_auto_lock_minutes: int = 15  # 0 = mai
    lock_vault_on_minimize: bool = False
    clear_clipboard_seconds: int = 30  # 0 = mai
    confirm_close_active_sessions: bool = True

    ui_layout_version: int = 3

    @staticmethod
    def _clamp(value: object, minimum: int, maximum: int, fallback: int) -> int:
        try:
            return max(minimum, min(int(value), maximum))
        except Exception:
            return fallback

    @classmethod
    def load(cls, path: Path) -> "AppSettings":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            theme = str(raw.get("theme", "dark")).lower()
            if theme not in {"dark", "light"}:
                theme = "dark"
            icon_theme = str(raw.get("icon_theme", "auto"))
            allowed_icons = {"auto", "Papirus-Dark", "Papirus", "Papirus-Light", "Adwaita", "hicolor"}
            if icon_theme not in allowed_icons:
                icon_theme = "auto"

            layout_version = cls._clamp(raw.get("ui_layout_version", 1), 1, 99, 1)
            sidebar_width = cls._clamp(raw.get("sidebar_width", 238), 190, 520, 238)
            if layout_version < 2 and sidebar_width > 270:
                sidebar_width = 238

            startup_mode = str(raw.get("startup_mode", "terminal"))
            if startup_mode not in {"terminal", "empty"}:
                startup_mode = "terminal"
            cursor_shape = str(raw.get("terminal_cursor_shape", "block"))
            if cursor_shape not in {"block", "ibeam", "underline"}:
                cursor_shape = "block"
            strict_host_key = str(raw.get("ssh_strict_host_key", "ask"))
            if strict_host_key not in {"ask", "accept-new", "yes", "no"}:
                strict_host_key = "ask"

            return cls(
                theme=theme,
                icon_theme=icon_theme,
                sidebar_width=sidebar_width,
                window_width=cls._clamp(raw.get("window_width", 1280), 960, 3000, 1280),
                window_height=cls._clamp(raw.get("window_height", 800), 640, 2000, 800),
                toolbar_icon_size=cls._clamp(raw.get("toolbar_icon_size", 22), 18, 32, 22),
                show_toolbar_labels=bool(raw.get("show_toolbar_labels", True)),
                show_left_rail=bool(raw.get("show_left_rail", True)),
                show_sidebar=bool(raw.get("show_sidebar", True)),
                show_menubar=bool(raw.get("show_menubar", True)),
                show_ribbon=bool(raw.get("show_ribbon", True)),
                show_statusbar=bool(raw.get("show_statusbar", True)),
                show_workspace_hint=bool(raw.get("show_workspace_hint", True)),
                compact_tabs=bool(raw.get("compact_tabs", True)),
                animations=bool(raw.get("animations", True)),
                startup_mode=startup_mode,
                terminal_font_family=str(raw.get("terminal_font_family", "Monospace")) or "Monospace",
                terminal_font_size=cls._clamp(raw.get("terminal_font_size", 10), 7, 28, 10),
                terminal_scrollback_lines=cls._clamp(raw.get("terminal_scrollback_lines", 20000), 1000, 500000, 20000),
                terminal_cursor_shape=cursor_shape,
                terminal_cursor_blink=bool(raw.get("terminal_cursor_blink", True)),
                terminal_copy_on_select=bool(raw.get("terminal_copy_on_select", False)),
                terminal_scroll_on_output=bool(raw.get("terminal_scroll_on_output", False)),
                terminal_audible_bell=bool(raw.get("terminal_audible_bell", False)),
                terminal_shell=str(raw.get("terminal_shell", "")),
                terminal_start_directory=str(raw.get("terminal_start_directory", "")),
                show_error_popups=bool(raw.get("show_error_popups", True)),
                open_error_tabs=bool(raw.get("open_error_tabs", True)),
                notification_duration_ms=cls._clamp(raw.get("notification_duration_ms", 3500), 1000, 15000, 3500),
                rdp_preflight=bool(raw.get("rdp_preflight", True)),
                connection_timeout_seconds=cls._clamp(raw.get("connection_timeout_seconds", 12), 2, 120, 12),
                follow_remote_directory=bool(raw.get("follow_remote_directory", True)),
                ssh_keepalive_interval=cls._clamp(raw.get("ssh_keepalive_interval", 30), 0, 600, 30),
                ssh_keepalive_count=cls._clamp(raw.get("ssh_keepalive_count", 3), 1, 20, 3),
                ssh_compression=bool(raw.get("ssh_compression", False)),
                ssh_strict_host_key=strict_host_key,
                vault_auto_lock_minutes=cls._clamp(raw.get("vault_auto_lock_minutes", 15), 0, 240, 15),
                lock_vault_on_minimize=bool(raw.get("lock_vault_on_minimize", False)),
                clear_clipboard_seconds=cls._clamp(raw.get("clear_clipboard_seconds", 30), 0, 600, 30),
                confirm_close_active_sessions=bool(raw.get("confirm_close_active_sessions", True)),
                ui_layout_version=3,
            )
        except Exception:
            return cls()

    def to_dict(self) -> Dict[str, object]:
        return {
            "version": APP_VERSION,
            "theme": self.theme,
            "icon_theme": self.icon_theme,
            "sidebar_width": self.sidebar_width,
            "window_width": self.window_width,
            "window_height": self.window_height,
            "toolbar_icon_size": self.toolbar_icon_size,
            "show_toolbar_labels": self.show_toolbar_labels,
            "show_left_rail": self.show_left_rail,
            "show_sidebar": self.show_sidebar,
            "show_menubar": self.show_menubar,
            "show_ribbon": self.show_ribbon,
            "show_statusbar": self.show_statusbar,
            "show_workspace_hint": self.show_workspace_hint,
            "compact_tabs": self.compact_tabs,
            "animations": self.animations,
            "startup_mode": self.startup_mode,
            "terminal_font_family": self.terminal_font_family,
            "terminal_font_size": self.terminal_font_size,
            "terminal_scrollback_lines": self.terminal_scrollback_lines,
            "terminal_cursor_shape": self.terminal_cursor_shape,
            "terminal_cursor_blink": self.terminal_cursor_blink,
            "terminal_copy_on_select": self.terminal_copy_on_select,
            "terminal_scroll_on_output": self.terminal_scroll_on_output,
            "terminal_audible_bell": self.terminal_audible_bell,
            "terminal_shell": self.terminal_shell,
            "terminal_start_directory": self.terminal_start_directory,
            "show_error_popups": self.show_error_popups,
            "open_error_tabs": self.open_error_tabs,
            "notification_duration_ms": self.notification_duration_ms,
            "rdp_preflight": self.rdp_preflight,
            "connection_timeout_seconds": self.connection_timeout_seconds,
            "follow_remote_directory": self.follow_remote_directory,
            "ssh_keepalive_interval": self.ssh_keepalive_interval,
            "ssh_keepalive_count": self.ssh_keepalive_count,
            "ssh_compression": self.ssh_compression,
            "ssh_strict_host_key": self.ssh_strict_host_key,
            "vault_auto_lock_minutes": self.vault_auto_lock_minutes,
            "lock_vault_on_minimize": self.lock_vault_on_minimize,
            "clear_clipboard_seconds": self.clear_clipboard_seconds,
            "confirm_close_active_sessions": self.confirm_close_active_sessions,
            "ui_layout_version": 3,
        }

    def save(self, path: Path) -> None:
        ensure_private_config_dir()
        atomic_write_text(path, json.dumps(self.to_dict(), indent=2, ensure_ascii=False), mode=0o600)

def detect_serial_ports() -> List[str]:
    """Rileva porte seriali tipiche su Fedora/Linux senza dipendere dalla GUI."""
    patterns = [
        "/dev/ttyUSB*",
        "/dev/ttyACM*",
        "/dev/ttyS*",
        "/dev/serial/by-id/*",
    ]
    ports: List[str] = []
    for pattern in patterns:
        for item in glob.glob(pattern):
            if item not in ports:
                ports.append(item)
    return sorted(ports)


def set_combo_text(combo: Gtk.ComboBoxText, value: str) -> None:
    """Imposta testo su ComboBoxText normale o con entry."""
    if hasattr(combo, "set_active_id") and combo.set_active_id(value):
        return
    child = combo.get_child() if hasattr(combo, "get_child") else None
    if isinstance(child, Gtk.Entry):
        child.set_text(value)


def get_combo_text(combo: Gtk.ComboBoxText, fallback: str = "") -> str:
    child = combo.get_child() if hasattr(combo, "get_child") else None
    if isinstance(child, Gtk.Entry):
        return child.get_text().strip()
    return (combo.get_active_text() or fallback).strip()


def available_icon_themes() -> List[str]:
    try:
        theme = Gtk.IconTheme.get_default()
        if theme is None:
            return []
        return sorted(str(x) for x in theme.list_themes())
    except Exception:
        return []


def icon_theme_available(name: str) -> bool:
    return name in available_icon_themes()


def resolve_icon_theme(requested: str, app_theme: str) -> str:
    """Sceglie un tema icone leggibile rispetto al tema dell'interfaccia.

    I pacchetti *-Dark contengono spesso icone raster molto chiare, quasi
    invisibili su superfici bianche. Viceversa i pacchetti *-Light possono
    risultare troppo scuri nel tema scuro. Per evitare combinazioni a basso
    contrasto viene applicata una variante compatibile, conservando la
    preferenza dell'utente quando è già adatta.
    """
    themes = available_icon_themes()

    def has(name: str) -> bool:
        return name in themes

    requested = requested or "auto"
    incompatible = (
        (app_theme == "light" and requested.lower().endswith("-dark"))
        or (app_theme == "dark" and requested.lower().endswith("-light"))
    )
    if requested != "auto" and has(requested) and not incompatible:
        return requested

    if app_theme == "dark":
        candidates = ["Papirus-Dark", "Papirus", "Adwaita", "hicolor"]
    else:
        candidates = ["Papirus-Light", "Papirus", "Adwaita", "hicolor"]
    for name in candidates:
        if has(name):
            return name
    return requested if requested != "auto" and has(requested) else "hicolor"


def first_available_icon(names: Union[str, List[str], Tuple[str, ...]]) -> str:
    icon_names = [names] if isinstance(names, str) else list(names)
    try:
        theme = Gtk.IconTheme.get_default()
        if theme is not None:
            for name in icon_names:
                if theme.has_icon(name):
                    return name
    except Exception:
        pass
    return icon_names[-1] if icon_names else "image-missing"


@dataclass
class Session:
    name: str
    protocol: str = "ssh"  # local, ssh, rdp, vnc, sftp, command, serial
    host: str = ""
    port: str = ""
    user: str = ""
    domain: str = ""
    command: str = ""
    notes: str = ""
    credential_id: str = ""
    use_credential_password: bool = True
    serial_databits: str = "8"
    serial_parity: str = "N"
    serial_stopbits: str = "1"
    rdp_mode: str = "native"  # native oppure legacy
    rdp_clipboard: bool = True
    rdp_dynamic_resolution: bool = True
    rdp_fonts: bool = True
    rdp_auto_reconnect: bool = True
    rdp_cert_ignore: bool = True
    rdp_sound: bool = False
    rdp_multimon: bool = False
    rdp_start_fullscreen: bool = False
    rdp_monitor: str = "current"  # current, primary oppure selector monitor:<...>
    rdp_window_mode: str = "embedded"  # embedded oppure detached
    rdp_admin: bool = False
    rdp_drive: str = ""
    rdp_width: str = ""
    rdp_height: str = ""
    rdp_quality: str = "lan"  # lan, wan, low
    rdp_grab_keyboard: bool = True
    rdp_grab_mouse: bool = True
    rdp_keyboard_layout: str = "auto"  # auto, it, us, de, fr, es oppure layout FreeRDP
    rdp_smart_sizing: bool = True
    rdp_microphone: bool = False
    rdp_printer: bool = False
    rdp_smartcard: bool = False
    rdp_gfx: bool = False
    rdp_compression: bool = True
    rdp_scale: str = "100"
    rdp_timeout: str = "12000"
    rdp_security: str = "auto"  # auto, nla, tls, rdp
    rdp_password_stdin: bool = True
    # Instradamento contestuale: diretto, tunnel salvato, jump host o RD Gateway.
    routing_mode: str = "direct"  # direct, saved_tunnel, proxy_jump, rd_gateway
    required_tunnel_id: str = ""
    wait_for_tunnel: bool = True
    jump_host: str = ""
    jump_port: str = "22"
    jump_user: str = ""
    rdp_gateway_host: str = ""
    rdp_gateway_port: str = "443"
    rdp_gateway_user: str = ""
    rdp_gateway_domain: str = ""
    rdp_gateway_credential_id: str = ""
    rdp_gateway_use_same_credentials: bool = True
    rdp_gateway_transport: str = "auto"  # auto, rpc, http

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "Session":
        return cls(
            name=str(data.get("name", "Nuova sessione")),
            protocol=str(data.get("protocol", "ssh")).lower(),
            host=str(data.get("host", "")),
            port=str(data.get("port", "")),
            user=str(data.get("user", "")),
            domain=str(data.get("domain", "")),
            command=str(data.get("command", "")),
            notes=str(data.get("notes", "")),
            credential_id=str(data.get("credential_id", "")),
            use_credential_password=bool(data.get("use_credential_password", True)),
            serial_databits=str(data.get("serial_databits", "8")),
            serial_parity=str(data.get("serial_parity", "N")).upper() or "N",
            serial_stopbits=str(data.get("serial_stopbits", "1")),
            rdp_mode=("legacy" if str(data.get("rdp_mode", "native")) in {"legacy", "external-legacy"} else "native"),
            rdp_clipboard=bool(data.get("rdp_clipboard", True)),
            rdp_dynamic_resolution=bool(data.get("rdp_dynamic_resolution", True)),
            rdp_fonts=bool(data.get("rdp_fonts", True)),
            rdp_auto_reconnect=bool(data.get("rdp_auto_reconnect", True)),
            rdp_cert_ignore=bool(data.get("rdp_cert_ignore", True)),
            rdp_sound=bool(data.get("rdp_sound", False)),
            rdp_multimon=bool(data.get("rdp_multimon", False)),
            rdp_start_fullscreen=bool(data.get("rdp_start_fullscreen", False)),
            rdp_monitor=str(data.get("rdp_monitor", "current")) or "current",
            rdp_window_mode=(
                "detached" if str(data.get("rdp_window_mode", "embedded")) == "detached" else "embedded"
            ),
            rdp_admin=bool(data.get("rdp_admin", False)),
            rdp_drive=str(data.get("rdp_drive", "")),
            rdp_width=str(data.get("rdp_width", "")),
            rdp_height=str(data.get("rdp_height", "")),
            rdp_quality=str(data.get("rdp_quality", "lan")),
            rdp_grab_keyboard=bool(data.get("rdp_grab_keyboard", True)),
            rdp_grab_mouse=bool(data.get("rdp_grab_mouse", True)),
            rdp_keyboard_layout=str(data.get("rdp_keyboard_layout", "auto")),
            rdp_smart_sizing=bool(data.get("rdp_smart_sizing", True)),
            rdp_microphone=bool(data.get("rdp_microphone", False)),
            rdp_printer=bool(data.get("rdp_printer", False)),
            rdp_smartcard=bool(data.get("rdp_smartcard", False)),
            rdp_gfx=bool(data.get("rdp_gfx", False)),
            rdp_compression=bool(data.get("rdp_compression", True)),
            rdp_scale=str(data.get("rdp_scale", "100")),
            rdp_timeout=str(data.get("rdp_timeout", "12000")),
            rdp_security=str(data.get("rdp_security", "auto")),
            rdp_password_stdin=bool(data.get("rdp_password_stdin", True)),
            routing_mode=(
                str(data.get("routing_mode", "")).strip()
                if str(data.get("routing_mode", "")).strip() in {"direct", "saved_tunnel", "proxy_jump", "rd_gateway"}
                else ("saved_tunnel" if str(data.get("required_tunnel_id", "")) else "direct")
            ),
            required_tunnel_id=str(data.get("required_tunnel_id", "")),
            wait_for_tunnel=bool(data.get("wait_for_tunnel", True)),
            jump_host=str(data.get("jump_host", "")),
            jump_port=str(data.get("jump_port", "22")) or "22",
            jump_user=str(data.get("jump_user", "")),
            rdp_gateway_host=str(data.get("rdp_gateway_host", "")),
            rdp_gateway_port=str(data.get("rdp_gateway_port", "443")) or "443",
            rdp_gateway_user=str(data.get("rdp_gateway_user", "")),
            rdp_gateway_domain=str(data.get("rdp_gateway_domain", "")),
            rdp_gateway_credential_id=str(data.get("rdp_gateway_credential_id", "")),
            rdp_gateway_use_same_credentials=bool(data.get("rdp_gateway_use_same_credentials", True)),
            rdp_gateway_transport=str(data.get("rdp_gateway_transport", "auto")) or "auto",
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "type": "session",
            "name": self.name,
            "protocol": self.protocol,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "domain": self.domain,
            "command": self.command,
            "notes": self.notes,
            "credential_id": self.credential_id,
            "use_credential_password": self.use_credential_password,
            "serial_databits": self.serial_databits,
            "serial_parity": self.serial_parity,
            "serial_stopbits": self.serial_stopbits,
            "rdp_mode": self.rdp_mode,
            "rdp_clipboard": self.rdp_clipboard,
            "rdp_dynamic_resolution": self.rdp_dynamic_resolution,
            "rdp_fonts": self.rdp_fonts,
            "rdp_auto_reconnect": self.rdp_auto_reconnect,
            "rdp_cert_ignore": self.rdp_cert_ignore,
            "rdp_sound": self.rdp_sound,
            "rdp_multimon": self.rdp_multimon,
            "rdp_start_fullscreen": self.rdp_start_fullscreen,
            "rdp_monitor": self.rdp_monitor,
            "rdp_window_mode": self.rdp_window_mode,
            "rdp_admin": self.rdp_admin,
            "rdp_drive": self.rdp_drive,
            "rdp_width": self.rdp_width,
            "rdp_height": self.rdp_height,
            "rdp_quality": self.rdp_quality,
            "rdp_grab_keyboard": self.rdp_grab_keyboard,
            "rdp_grab_mouse": self.rdp_grab_mouse,
            "rdp_keyboard_layout": self.rdp_keyboard_layout,
            "rdp_smart_sizing": self.rdp_smart_sizing,
            "rdp_microphone": self.rdp_microphone,
            "rdp_printer": self.rdp_printer,
            "rdp_smartcard": self.rdp_smartcard,
            "rdp_gfx": self.rdp_gfx,
            "rdp_compression": self.rdp_compression,
            "rdp_scale": self.rdp_scale,
            "rdp_timeout": self.rdp_timeout,
            "rdp_security": self.rdp_security,
            "rdp_password_stdin": self.rdp_password_stdin,
            "routing_mode": self.routing_mode,
            "required_tunnel_id": self.required_tunnel_id,
            "wait_for_tunnel": self.wait_for_tunnel,
            "jump_host": self.jump_host,
            "jump_port": self.jump_port,
            "jump_user": self.jump_user,
            "rdp_gateway_host": self.rdp_gateway_host,
            "rdp_gateway_port": self.rdp_gateway_port,
            "rdp_gateway_user": self.rdp_gateway_user,
            "rdp_gateway_domain": self.rdp_gateway_domain,
            "rdp_gateway_credential_id": self.rdp_gateway_credential_id,
            "rdp_gateway_use_same_credentials": self.rdp_gateway_use_same_credentials,
            "rdp_gateway_transport": self.rdp_gateway_transport,
        }


def _port_problem(value: str, label: str = "porta", allow_zero: bool = False) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not value.isdigit() or not ((0 if allow_zero else 1) <= int(value) <= 65535):
        return f"{label} «{value}» non valida: serve un numero da 1 a 65535"
    return ""


def session_problem(sess: "Session") -> str:
    """Il primo problema che impedisce di usare la sessione, oppure "".

    Usato al salvataggio (la finestra resta aperta e lo dice), dalla connessione rapida e prima di
    connettersi: un file di sessioni importato puo' contenere valori che nessuna finestra ha
    controllato.
    """
    proto = (sess.protocol or "").lower()
    host = (sess.host or "").strip()
    if proto in {"ssh", "sftp", "rdp", "vnc"}:
        if not host:
            return "manca l'host (nome o indirizzo del server)"
        if host.startswith("-") or any(ch.isspace() for ch in host):
            return f"host «{host}» non valido"
        if (sess.user or "").strip().startswith("-"):
            return f"utente «{sess.user}» non valido"
        problem = _port_problem(sess.port, "display/porta" if proto == "vnc" else "porta", allow_zero=proto == "vnc")
        if problem:
            return problem
    if proto in {"ssh", "sftp"} and getattr(sess, "routing_mode", "direct") == "proxy_jump":
        jump = (getattr(sess, "jump_host", "") or "").strip()
        if jump.startswith("-") or any(ch.isspace() for ch in jump):
            return f"jump host «{jump}» non valido"
        problem = _port_problem(getattr(sess, "jump_port", ""), "porta del jump host")
        if problem:
            return problem
    if proto == "serial":
        if not host:
            return "manca la porta seriale (per esempio /dev/ttyUSB0)"
        if not (sess.port or "").strip().isdigit():
            return f"velocità «{sess.port}» non valida: serve un numero, per esempio 115200"
    if proto == "command" and not (sess.command or "").strip():
        return "manca il comando da eseguire"
    return ""


HOME_SESSION_NAME = "Home Session"
HOME_SESSION_COLOR = "#4aa3df"
DEFAULT_FOLDER_COLOR = "#f2c94c"
FOLDER_COLOR_PRESETS: List[Tuple[str, str]] = [
    ("Giallo", "#f2c94c"),
    ("Blu", "#4aa3df"),
    ("Azzurro", "#56c7d9"),
    ("Verde", "#53b175"),
    ("Arancione", "#f2994a"),
    ("Rosso", "#eb5757"),
    ("Viola", "#9b73e6"),
    ("Rosa", "#e77fb3"),
    ("Grigio", "#8b97a6"),
]


def normalize_folder_color(value: object) -> str:
    text = str(value or "").strip()
    if len(text) == 7 and text.startswith("#"):
        try:
            int(text[1:], 16)
            return text.lower()
        except ValueError:
            pass
    return DEFAULT_FOLDER_COLOR


@dataclass
class FolderNode:
    name: str
    children: List["Node"] = field(default_factory=list)
    color: str = DEFAULT_FOLDER_COLOR

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "FolderNode":
        raw_children = data.get("children", [])
        children: List[Node] = []
        if isinstance(raw_children, list):
            children = [node_from_dict(x) for x in raw_children if isinstance(x, dict)]
        return cls(
            name=str(data.get("name", "Cartella")),
            children=children,
            color=normalize_folder_color(data.get("color", DEFAULT_FOLDER_COLOR)),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "type": "folder",
            "name": self.name,
            "color": normalize_folder_color(self.color),
            "children": [node_to_dict(child) for child in self.children],
        }


Node = Union[Session, FolderNode]


def node_from_dict(data: Dict[str, object]) -> Node:
    kind = str(data.get("type", "")).lower()
    if kind == "folder" or "children" in data:
        return FolderNode.from_dict(data)
    return Session.from_dict(data)


def node_to_dict(node: Node) -> Dict[str, object]:
    if isinstance(node, FolderNode):
        return node.to_dict()
    return node.to_dict()


def make_default_tree() -> List[Node]:
    base_children: List[Node] = [
        FolderNode(
            name="Terminali",
            children=[Session(name="Terminale locale", protocol="local")],
        ),
        FolderNode(
            name="SSH / Linux",
            children=[
                Session(
                    name="Esempio SSH",
                    protocol="ssh",
                    host="server.local",
                    port="22",
                    user=os.getenv("USER", ""),
                )
            ],
        ),
        FolderNode(
            name="RDP / Windows",
            children=[
                Session(
                    name="Esempio RDP dominio",
                    protocol="rdp",
                    host="192.168.1.10",
                    port="3389",
                    user="utente",
                    domain="DOMINIO",
                )
            ],
        ),
        FolderNode(
            name="Seriale / Console",
            children=[
                Session(
                    name="Console seriale USB 115200",
                    protocol="serial",
                    host="/dev/ttyUSB0",
                    port="115200",
                    serial_databits="8",
                    serial_parity="N",
                    serial_stopbits="1",
                )
            ],
        ),
    ]
    return [FolderNode(name=HOME_SESSION_NAME, color=HOME_SESSION_COLOR, children=base_children)]


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.nodes: List[Node] = []
        # Messaggio per l'utente quando il file non era leggibile: lo mostra la finestra principale.
        self.load_warning = ""
        self.load()

    def ensure_home_session_root(self) -> bool:
        if len(self.nodes) == 1 and isinstance(self.nodes[0], FolderNode) and self.nodes[0].name == HOME_SESSION_NAME:
            self.nodes[0].color = HOME_SESSION_COLOR
            return False
        home_nodes = [n for n in self.nodes if isinstance(n, FolderNode) and n.name == HOME_SESSION_NAME]
        if home_nodes:
            home = home_nodes[0]
            rest = [n for n in self.nodes if n is not home]
            home.children.extend(rest)
            home.color = HOME_SESSION_COLOR
            self.nodes = [home]
        else:
            self.nodes = [FolderNode(name=HOME_SESSION_NAME, color=HOME_SESSION_COLOR, children=self.nodes)]
        return True

    def home_path(self) -> str:
        return "0"

    def is_home_path(self, path: str) -> bool:
        """True solo per la radice logica e protetta Home Session."""
        return path == self.home_path()

    def home_node(self) -> FolderNode:
        if not self.nodes:
            self.nodes = make_default_tree()
        if not (len(self.nodes) == 1 and isinstance(self.nodes[0], FolderNode) and self.nodes[0].name == HOME_SESSION_NAME):
            self.ensure_home_session_root()
        return self.nodes[0]  # type: ignore[return-value]

    def load(self) -> None:
        ensure_private_config_dir()
        if not self.path.exists():
            self.nodes = make_default_tree()
            self.save()
            return

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                items = raw.get("items", [])
            else:
                items = raw

            if not isinstance(items, list):
                raise ValueError("Formato sessioni non valido: attesa una lista.")

            self.nodes = [node_from_dict(x) for x in items if isinstance(x, dict)]
            if not self.nodes:
                self.nodes = make_default_tree()
                self.save()
                return

            if isinstance(raw, list) and all(isinstance(node, Session) for node in self.nodes):
                self.nodes = [FolderNode(name="Importate dalla 0.1", children=self.nodes)]
            changed = self.ensure_home_session_root()
            if changed or isinstance(raw, list):
                self.save()
        except Exception as exc:
            # Nome con data: un secondo file rotto non deve sovrascrivere la copia del primo.
            backup = self.path.with_name(f"{self.path.stem}.illeggibile-{time.strftime('%Y%m%d-%H%M%S')}.json")
            try:
                self.path.rename(backup)
            except OSError:
                backup = self.path
            self.nodes = make_default_tree()
            self.save()
            self.load_warning = (
                f"Il file delle sessioni non era leggibile ({exc}). Ne ho conservato una copia in "
                f"{backup} e sono ripartito dalle sessioni di esempio."
            )
            print(f"File sessioni corrotto, salvato come {backup}: {exc}", file=sys.stderr)

    def save(self) -> None:
        ensure_private_config_dir()
        # Home Session è una radice speciale, non una cartella personalizzabile.
        # Normalizziamo nome e colore prima di ogni salvataggio per eliminare
        # eventuali proprietà ereditate da versioni precedenti.
        home = self.home_node()
        home.name = HOME_SESSION_NAME
        home.color = HOME_SESSION_COLOR
        data = {
            "version": APP_VERSION,
            "items": [node_to_dict(node) for node in self.nodes],
        }
        atomic_write_text(self.path, json.dumps(data, indent=2, ensure_ascii=False), mode=0o600)

    @staticmethod
    def _parts(path: str) -> List[int]:
        if not path:
            return []
        return [int(x) for x in path.split(".") if x != ""]

    def get_node(self, path: str) -> Node:
        parts = self._parts(path)
        if not parts:
            raise ValueError("La radice non è un nodo.")
        current: Node = self.nodes[parts[0]]
        for part in parts[1:]:
            if not isinstance(current, FolderNode):
                raise ValueError("Percorso non valido: nodo non cartella.")
            current = current.children[part]
        return current

    def get_container(self, folder_path: str) -> List[Node]:
        if folder_path == "":
            return self.nodes
        node = self.get_node(folder_path)
        if not isinstance(node, FolderNode):
            raise ValueError("Il percorso destinazione non è una cartella.")
        return node.children

    def parent_path(self, path: str) -> str:
        parts = path.split(".")
        if len(parts) <= 1:
            return ""
        return ".".join(parts[:-1])

    def add_node(self, parent_folder_path: str, node: Node) -> None:
        self.get_container(parent_folder_path).append(node)
        self.save()

    def replace_node(self, path: str, node: Node) -> None:
        parts = self._parts(path)
        if not parts:
            raise ValueError("Impossibile sostituire la radice.")
        container = self.get_container(".".join(str(x) for x in parts[:-1]))
        container[parts[-1]] = node
        self.save()

    def remove_node(self, path: str) -> Node:
        parts = self._parts(path)
        if not parts or path == self.home_path():
            raise ValueError("Home Session non può essere eliminata.")
        container = self.get_container(".".join(str(x) for x in parts[:-1]))
        node = container.pop(parts[-1])
        self.save()
        return node

    def find_node_path(self, target: Node) -> str:
        """Restituisce il percorso corrente di un nodo usando l'identità Python."""
        def walk(nodes: List[Node], prefix: str) -> Optional[str]:
            for idx, node in enumerate(nodes):
                path = f"{prefix}.{idx}" if prefix else str(idx)
                if node is target:
                    return path
                if isinstance(node, FolderNode):
                    found = walk(node.children, path)
                    if found is not None:
                        return found
            return None

        found = walk(self.nodes, "")
        if found is None:
            raise ValueError("Nodo non trovato dopo lo spostamento.")
        return found

    def move_node(self, source_path: str, target_folder_path: str, target_index: Optional[int] = None) -> str:
        """Sposta un nodo in modo atomico e restituisce il nuovo percorso.

        ``target_folder_path`` è la cartella di destinazione (stringa vuota per
        la radice). ``target_index`` permette anche il riordino prima/dopo una
        sessione; ``None`` inserisce in fondo.
        """
        if not source_path or source_path == self.home_path():
            raise ValueError("Home Session non può essere spostata.")
        source_node = self.get_node(source_path)
        if isinstance(source_node, FolderNode) and (
            target_folder_path == source_path or target_folder_path.startswith(source_path + ".")
        ):
            raise ValueError("Una cartella non può essere spostata dentro sé stessa o una sua sottocartella.")

        source_parts = self._parts(source_path)
        source_parent_path = ".".join(str(x) for x in source_parts[:-1])
        source_index = source_parts[-1]
        source_container = self.get_container(source_parent_path)
        # Conserviamo il riferimento alla lista di destinazione prima del pop:
        # gli indici dei percorsi possono cambiare, l'oggetto lista no.
        target_container = self.get_container(target_folder_path)

        node = source_container.pop(source_index)
        if target_index is None:
            target_index = len(target_container)
        else:
            target_index = max(0, min(int(target_index), len(target_container)))
            if source_container is target_container and source_index < target_index:
                target_index -= 1
        target_container.insert(target_index, node)
        self.save()
        return self.find_node_path(node)

    def iter_folders(self) -> Iterable[Tuple[str, FolderNode, str]]:
        def walk(nodes: List[Node], prefix: str, label_prefix: str) -> Iterable[Tuple[str, FolderNode, str]]:
            for idx, node in enumerate(nodes):
                path = f"{prefix}.{idx}" if prefix else str(idx)
                if isinstance(node, FolderNode):
                    label = f"{label_prefix}{node.name}"
                    yield path, node, label
                    yield from walk(node.children, path, f"{label_prefix}{node.name} / ")

        yield from walk(self.nodes, "", "")

    def iter_sessions(self) -> Iterable[Tuple[str, "Session", str]]:
        """Tutte le sessioni con percorso e cartella di appartenenza.

        Usato dalla command palette per la ricerca rapida: restituisce anche
        il percorso della cartella (``folder``) per dare contesto.
        """
        def walk(nodes: List[Node], prefix: str, folder: str) -> Iterable[Tuple[str, "Session", str]]:
            for idx, node in enumerate(nodes):
                path = f"{prefix}.{idx}" if prefix else str(idx)
                if isinstance(node, FolderNode):
                    child_folder = f"{folder} / {node.name}" if folder else node.name
                    yield from walk(node.children, path, child_folder)
                elif isinstance(node, Session):
                    yield path, node, folder

        yield from walk(self.nodes, "", "")


@dataclass
class TunnelSession:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "Nuovo tunnel"
    tunnel_type: str = "local"  # local, remote, dynamic
    ssh_host: str = ""
    ssh_port: str = "22"
    ssh_user: str = ""
    bind_address: str = "127.0.0.1"
    local_port: str = ""
    remote_host: str = "127.0.0.1"
    remote_port: str = ""
    options: str = ""
    credential_id: str = ""
    use_credential_password: bool = True
    identity_file: str = ""
    address_family: str = "auto"  # auto, ipv4, ipv6
    exit_on_forward_failure: bool = True
    compression: bool = False
    server_alive_interval: int = 30
    server_alive_count: int = 3
    auto_start: bool = False
    auto_restart: bool = True
    restart_delay: int = 5
    verbose_log: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "TunnelSession":
        def _int(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                return max(minimum, min(int(data.get(name, default)), maximum))
            except (TypeError, ValueError):
                return default

        tunnel_type = str(data.get("tunnel_type", "local")).lower()
        if tunnel_type not in {"local", "remote", "dynamic"}:
            tunnel_type = "local"
        address_family = str(data.get("address_family", "auto")).lower()
        if address_family not in {"auto", "ipv4", "ipv6"}:
            address_family = "auto"
        return cls(
            id=str(data.get("id", uuid.uuid4().hex)),
            name=str(data.get("name", "Tunnel SSH")),
            tunnel_type=tunnel_type,
            ssh_host=str(data.get("ssh_host", "")),
            ssh_port=str(data.get("ssh_port", "22")),
            ssh_user=str(data.get("ssh_user", "")),
            bind_address=str(data.get("bind_address", "127.0.0.1")),
            local_port=str(data.get("local_port", "")),
            remote_host=str(data.get("remote_host", "127.0.0.1")),
            remote_port=str(data.get("remote_port", "")),
            options=str(data.get("options", "")),
            credential_id=str(data.get("credential_id", "")),
            use_credential_password=bool(data.get("use_credential_password", True)),
            identity_file=str(data.get("identity_file", "")),
            address_family=address_family,
            exit_on_forward_failure=bool(data.get("exit_on_forward_failure", True)),
            compression=bool(data.get("compression", False)),
            server_alive_interval=_int("server_alive_interval", 30, 0, 3600),
            server_alive_count=_int("server_alive_count", 3, 1, 100),
            auto_start=bool(data.get("auto_start", False)),
            auto_restart=bool(data.get("auto_restart", True)),
            restart_delay=_int("restart_delay", 5, 1, 300),
            verbose_log=bool(data.get("verbose_log", False)),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "tunnel_type": self.tunnel_type,
            "ssh_host": self.ssh_host,
            "ssh_port": self.ssh_port,
            "ssh_user": self.ssh_user,
            "bind_address": self.bind_address,
            "local_port": self.local_port,
            "remote_host": self.remote_host,
            "remote_port": self.remote_port,
            "options": self.options,
            "credential_id": self.credential_id,
            "use_credential_password": self.use_credential_password,
            "identity_file": self.identity_file,
            "address_family": self.address_family,
            "exit_on_forward_failure": self.exit_on_forward_failure,
            "compression": self.compression,
            "server_alive_interval": self.server_alive_interval,
            "server_alive_count": self.server_alive_count,
            "auto_start": self.auto_start,
            "auto_restart": self.auto_restart,
            "restart_delay": self.restart_delay,
            "verbose_log": self.verbose_log,
        }

    def type_label(self) -> str:
        return {
            "local": "Locale",
            "remote": "Remoto",
            "dynamic": "SOCKS",
        }.get(self.tunnel_type, self.tunnel_type.capitalize())

    def listen_endpoint(self) -> str:
        return f"{self.bind_address or '127.0.0.1'}:{self.local_port or '?'}"

    def destination_endpoint(self) -> str:
        if self.tunnel_type == "dynamic":
            return "Destinazione scelta dal client SOCKS"
        return f"{self.remote_host or '?'}:{self.remote_port or '?'}"

    def ssh_endpoint(self) -> str:
        prefix = f"{self.ssh_user}@" if self.ssh_user else ""
        return f"{prefix}{self.ssh_host or '?'}:{self.ssh_port or '22'}"

    def summary(self) -> str:
        if self.tunnel_type == "dynamic":
            return f"SOCKS {self.listen_endpoint()} tramite {self.ssh_endpoint()}"
        if self.tunnel_type == "remote":
            return f"Remoto {self.listen_endpoint()} → {self.destination_endpoint()} tramite {self.ssh_endpoint()}"
        return f"Locale {self.listen_endpoint()} → {self.destination_endpoint()} tramite {self.ssh_endpoint()}"


class TunnelStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.tunnels: List[TunnelSession] = []
        self.load()

    def load(self) -> None:
        ensure_private_config_dir()
        if not self.path.exists():
            self.tunnels = [
                TunnelSession(
                    name="Esempio tunnel locale RDP",
                    tunnel_type="local",
                    ssh_host="bastion.local",
                    ssh_port="22",
                    ssh_user=os.getenv("USER", ""),
                    bind_address="127.0.0.1",
                    local_port="13389",
                    remote_host="192.168.1.10",
                    remote_port="3389",
                    exit_on_forward_failure=True,
                    auto_restart=True,
                )
            ]
            self.save()
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = raw.get("items", raw) if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                raise ValueError("Formato tunnel non valido")
            self.tunnels = [TunnelSession.from_dict(x) for x in items if isinstance(x, dict)]
        except Exception as exc:
            backup = self.path.with_suffix(".broken.json")
            try:
                self.path.rename(backup)
            except OSError:
                backup = self.path
            self.tunnels = []
            self.save()
            print(f"File tunnel corrotto, salvato come {backup}: {exc}", file=sys.stderr)

    def save(self) -> None:
        ensure_private_config_dir()
        data = {"version": APP_VERSION, "items": [t.to_dict() for t in self.tunnels]}
        atomic_write_text(self.path, json.dumps(data, indent=2, ensure_ascii=False), mode=0o600)

    def get(self, tunnel_id: str) -> Optional[TunnelSession]:
        for tunnel in self.tunnels:
            if tunnel.id == tunnel_id:
                return tunnel
        return None

    def upsert(self, tunnel: TunnelSession) -> None:
        for idx, item in enumerate(self.tunnels):
            if item.id == tunnel.id:
                self.tunnels[idx] = tunnel
                self.save()
                return
        self.tunnels.append(tunnel)
        self.save()

    def remove(self, tunnel_id: str) -> None:
        self.tunnels = [x for x in self.tunnels if x.id != tunnel_id]
        self.save()


def _vault_app(parent: Gtk.Window):
    """L'AppWindow da cui prendere icone, impostazioni e appunti, anche da un dialogo figlio."""
    return getattr(parent, "parent_app", parent)


def _vault_image(parent: Gtk.Window, names: List[str], size: int) -> Gtk.Image:
    app = _vault_app(parent)
    if hasattr(app, "make_icon_image"):
        return app.make_icon_image(names, size)
    return Gtk.Image.new_from_icon_name(names[0], Gtk.IconSize.DIALOG if size >= 32 else Gtk.IconSize.BUTTON)


def _vault_button(parent: Gtk.Window, label: Gtk.Label, icons: List[str], callback, css: str = "secondary-action") -> Gtk.Button:
    """Pulsante con icona ed etichetta con mnemonico, raggiungibile da tastiera."""
    button = Gtk.Button()
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    box.set_halign(Gtk.Align.CENTER)
    if icons:
        box.pack_start(_vault_image(parent, icons, 16), False, False, 0)
    box.pack_start(label, False, False, 0)
    label.set_mnemonic_widget(button)
    button.add(box)
    button.get_style_context().add_class(css)
    if callback is not None:
        button.connect("clicked", callback)
    return button


def _vault_icon_button(parent: Gtk.Window, icons: List[str], tooltip: str, callback) -> Gtk.Button:
    button = Gtk.Button()
    button.add(_vault_image(parent, icons, 16))
    button.set_tooltip_text(tooltip)
    button.set_relief(Gtk.ReliefStyle.NONE)
    button.get_style_context().add_class("vault-icon-button")
    if callback is not None:
        button.connect("clicked", callback)
    return button


def _password_entry(activates_default: bool = True) -> Gtk.Entry:
    """Campo password con l'occhio per mostrarla, come nei dialoghi di sistema di GNOME."""
    entry = Gtk.Entry()
    entry.set_visibility(False)
    entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
    entry.set_activates_default(activates_default)
    entry.set_hexpand(True)
    entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, "view-reveal-symbolic")
    entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Mostra la password")

    def toggle(e: Gtk.Entry, _pos, _event) -> None:
        visible = not e.get_visibility()
        e.set_visibility(visible)
        e.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY, "view-conceal-symbolic" if visible else "view-reveal-symbolic"
        )
        e.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Nascondi la password" if visible else "Mostra la password")

    entry.connect("icon-press", toggle)
    return entry


class _StrengthMeter(Gtk.Box):
    """Barra di robustezza sotto il campo password, aggiornata mentre si scrive."""

    def __init__(self, entry: Gtk.Entry) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        # Cinque tacche: una per ogni livello di password_strength (0-4), vuota senza password.
        self.bar = Gtk.LevelBar(min_value=0, max_value=5)
        self.bar.set_mode(Gtk.LevelBarMode.DISCRETE)
        self.bar.set_hexpand(True)
        self.bar.set_valign(Gtk.Align.CENTER)
        self.bar.get_style_context().add_class("vault-strength")
        for name, value in (("low", 2.0), ("high", 4.0), ("full", 5.0)):
            self.bar.add_offset_value(name, value)
        self.text = Gtk.Label(xalign=0)
        self.text.set_width_chars(12)
        self.text.get_style_context().add_class("muted")
        self.pack_start(self.bar, True, True, 0)
        self.pack_start(self.text, False, False, 0)
        entry.connect("changed", lambda e: self.update(e.get_text()))
        self.update(entry.get_text())

    def update(self, password: str) -> None:
        score, text = password_strength(password)
        self.bar.set_value(score + 1 if password else 0)
        self.text.set_text(text)


class MasterPasswordDialog(Gtk.Dialog):
    """Richiesta della master password (o della password di un backup).

    Modalita': unlock, create, change (cambio master password), export, import (password del file
    di backup). Con run_validated() un errore resta nella finestra e si riprova subito, invece di
    chiuderla e lasciare un messaggio nella barra di stato.
    """

    TEXTS = {
        "unlock": ("Sblocca le password", "Inserisci la master password per usare le credenziali salvate.", "_Sblocca"),
        "create": (
            "Proteggi le tue password",
            "Scegli una master password: cifra tutte le credenziali salvate in FedTermHub. Non viene "
            "salvata da nessuna parte e non si può recuperare: se la dimentichi, le credenziali sono perse.",
            "_Crea archivio",
        ),
        "change": (
            "Cambia master password",
            "Le credenziali vengono cifrate di nuovo con la nuova password. I backup esportati prima "
            "restano protetti dalla password che avevano.",
            "_Cambia password",
        ),
        "export": (
            "Password del backup",
            "Il file di backup viene cifrato con questa password: serve per importarlo di nuovo, "
            "conservala insieme al file. Può essere diversa dalla master password.",
            "_Esporta",
        ),
        "import": ("Password del backup", "Inserisci la password scelta quando il backup è stato esportato.", "_Importa"),
    }

    def __init__(self, parent: Gtk.Window, mode: str = "unlock") -> None:
        heading_text, info_text, ok_label = self.TEXTS.get(mode, self.TEXTS["unlock"])
        super().__init__(title=heading_text, transient_for=parent, flags=0)
        self.set_modal(True)
        self.set_resizable(False)
        self.mode = mode
        self.get_style_context().add_class("modern-dialog")
        self.add_button("_Annulla", Gtk.ResponseType.CANCEL)
        ok = self.add_button(ok_label, Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(480, -1)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin=18)
        self.get_content_area().pack_start(outer, True, True, 0)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        icon = "document-save-symbolic" if mode in {"export", "import"} else "dialog-password-symbolic"
        header.pack_start(_vault_image(parent, [icon, "dialog-password"], 40), False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        heading = Gtk.Label(label=heading_text, xalign=0)
        heading.get_style_context().add_class("dialog-heading")
        info = Gtk.Label(label=info_text, xalign=0)
        info.set_line_wrap(True)
        info.set_max_width_chars(52)
        info.get_style_context().add_class("muted")
        texts.pack_start(heading, False, False, 0)
        texts.pack_start(info, False, False, 0)
        header.pack_start(texts, True, True, 0)
        outer.pack_start(header, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        outer.pack_start(grid, False, False, 0)
        row = 0
        self.current: Optional[Gtk.Entry] = None
        if mode == "change":
            self.current = _password_entry()
            caption = Gtk.Label.new_with_mnemonic("Password a_ttuale")
            caption.set_xalign(0)
            caption.set_mnemonic_widget(self.current)
            grid.attach(caption, 0, row, 1, 1)
            grid.attach(self.current, 1, row, 1, 1)
            row += 1

        self.password = _password_entry()
        if mode == "change":
            caption = Gtk.Label.new_with_mnemonic("_Nuova password")
        elif mode in {"export", "import"}:
            caption = Gtk.Label.new_with_mnemonic("Pass_word")
        else:
            caption = Gtk.Label.new_with_mnemonic("_Master password")
        caption.set_xalign(0)
        caption.set_mnemonic_widget(self.password)
        grid.attach(caption, 0, row, 1, 1)
        grid.attach(self.password, 1, row, 1, 1)
        row += 1

        self.confirm: Optional[Gtk.Entry] = None
        self.meter: Optional[_StrengthMeter] = None
        if mode in {"create", "change", "export"}:
            self.meter = _StrengthMeter(self.password)
            grid.attach(self.meter, 1, row, 1, 1)
            row += 1
            self.confirm = _password_entry()
            caption = Gtk.Label.new_with_mnemonic("C_onferma")
            caption.set_xalign(0)
            caption.set_mnemonic_widget(self.confirm)
            grid.attach(caption, 0, row, 1, 1)
            grid.attach(self.confirm, 1, row, 1, 1)
            row += 1

        self.error = Gtk.Label(xalign=0)
        self.error.set_line_wrap(True)
        self.error.set_no_show_all(True)
        self.error.get_style_context().add_class("vault-error")
        outer.pack_start(self.error, False, False, 0)

        # Spunta per lo sblocco persistente direttamente nel prompt: evita di dover cercare
        # l'opzione nelle impostazioni.
        self.keep_unlocked = Gtk.CheckButton(label="Non chiedere più la password finché FedTermHub resta aperto")
        self.keep_unlocked.set_tooltip_text(
            "Disattiva il blocco automatico per inattività (si cambia anche dalle Impostazioni). "
            "La password verrà richiesta alla prossima apertura del programma."
        )
        if mode in {"unlock", "create"}:
            outer.pack_start(self.keep_unlocked, False, False, 0)

        for entry in (self.current, self.password, self.confirm):
            if entry is not None:
                entry.connect("changed", lambda *_a: self.error.hide())
        self.show_all()
        (self.current or self.password).grab_focus()

    def wants_keep_unlocked(self) -> bool:
        try:
            return bool(self.keep_unlocked.get_active()) and self.mode in {"unlock", "create"}
        except Exception:
            return False

    def get_password(self) -> str:
        return self.password.get_text()

    def get_current_password(self) -> str:
        return self.current.get_text() if self.current is not None else ""

    def password_matches(self) -> bool:
        if self.confirm is None:
            return True
        return self.password.get_text() == self.confirm.get_text()

    def local_error(self) -> Optional[str]:
        """Errori che si vedono senza toccare il disco."""
        if self.current is not None and not self.get_current_password():
            return "Inserisci la master password attuale."
        if not self.get_password():
            return "Inserisci la password."
        if self.mode in {"create", "change", "export"}:
            if len(self.get_password()) < 8:
                return "Usa almeno 8 caratteri: è l'unica protezione delle credenziali salvate."
            if not self.password_matches():
                return "Le due password non coincidono."
        return None

    def show_error(self, message: str) -> None:
        self.error.set_text(message)
        self.error.show()
        target = self.current if (self.current is not None and "attuale" in message) else self.password
        target.grab_focus()
        target.select_region(0, -1)

    def run_validated(self, check: Optional[Callable[[], Optional[str]]] = None) -> bool:
        """Esegue il dialogo finche' la password e' accettata (True) o l'utente annulla (False).

        `check` fa la verifica vera (sblocco, decifratura del backup) e restituisce un messaggio
        di errore oppure None.
        """
        while True:
            if self.run() != Gtk.ResponseType.OK:
                return False
            message = self.local_error()
            if message is None and check is not None:
                self.set_sensitive(False)
                watch = self.get_window()
                if watch is not None:
                    watch.set_cursor(Gdk.Cursor.new_from_name(watch.get_display(), "wait"))
                while Gtk.events_pending():
                    Gtk.main_iteration_do(False)
                try:
                    message = check()
                finally:
                    self.set_sensitive(True)
                    if watch is not None:
                        watch.set_cursor(None)
            if message is None:
                return True
            self.show_error(message)


class CredentialDialog(Gtk.Dialog):
    """Nuova credenziale o modifica di una esistente."""

    def __init__(
        self,
        parent: Gtk.Window,
        credential: Optional[Credential] = None,
        groups: Optional[List[str]] = None,
    ) -> None:
        is_new = credential is None
        super().__init__(title="Nuova credenziale" if is_new else "Modifica credenziale", transient_for=parent, flags=0)
        self.set_modal(True)
        self.get_style_context().add_class("modern-dialog")
        self.add_button("_Annulla", Gtk.ResponseType.CANCEL)
        self.save_button = self.add_button("_Salva", Gtk.ResponseType.OK)
        self.save_button.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(560, -1)

        self.credential = credential or Credential(name="")
        self.original_password = self.credential.password
        self.is_new = is_new

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin=18)
        self.get_content_area().pack_start(outer, True, True, 0)
        heading = Gtk.Label(label="Nuova credenziale" if is_new else "Modifica credenziale", xalign=0)
        heading.get_style_context().add_class("dialog-heading")
        outer.pack_start(heading, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        outer.pack_start(grid, True, True, 0)

        def caption(widget: Gtk.Widget, label: Gtk.Label, row: int) -> None:
            label.set_xalign(0)
            label.set_mnemonic_widget(widget)
            label.get_style_context().add_class("field-label")
            grid.attach(label, 0, row, 1, 1)

        self.name = Gtk.Entry(text=self.credential.name)
        self.name.set_hexpand(True)
        self.name.set_activates_default(True)
        self.name.set_placeholder_text("Es. Administrator dominio Rossi")
        caption(self.name, Gtk.Label.new_with_mnemonic("_Nome"), 0)
        grid.attach(self.name, 1, 0, 1, 1)

        self.group = Gtk.ComboBoxText.new_with_entry()
        for name in groups or []:
            self.group.append_text(name)
        group_entry = self.group.get_child()
        group_entry.set_text(self.credential.group)
        group_entry.set_placeholder_text("Es. il nome del cliente (facoltativo)")
        group_entry.set_activates_default(True)
        self.group.set_tooltip_text("Le credenziali dello stesso gruppo compaiono insieme nell'elenco.")
        caption(group_entry, Gtk.Label.new_with_mnemonic("_Gruppo"), 1)
        grid.attach(self.group, 1, 1, 1, 1)

        self.username = Gtk.Entry(text=self.credential.username)
        self.username.set_activates_default(True)
        caption(self.username, Gtk.Label.new_with_mnemonic("_Utente"), 2)
        grid.attach(self.username, 1, 2, 1, 1)

        self.domain = Gtk.Entry(text=self.credential.domain)
        self.domain.set_activates_default(True)
        self.domain.set_placeholder_text("Solo per account Windows / Active Directory")
        caption(self.domain, Gtk.Label.new_with_mnemonic("_Dominio"), 3)
        grid.attach(self.domain, 1, 3, 1, 1)

        self.password = _password_entry()
        self.password.set_text(self.credential.password)
        password_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        password_row.pack_start(self.password, True, True, 0)
        generate = _vault_button(
            parent, Gtk.Label.new_with_mnemonic("Gene_ra"), ["view-refresh-symbolic", "system-run-symbolic"],
            self.on_generate,
        )
        generate.set_tooltip_text("Crea una password casuale di 20 caratteri")
        password_row.pack_start(generate, False, False, 0)
        caption(self.password, Gtk.Label.new_with_mnemonic("_Password"), 4)
        grid.attach(password_row, 1, 4, 1, 1)
        grid.attach(_StrengthMeter(self.password), 1, 5, 1, 1)

        self.server_hint = Gtk.Label(
            label="Hai cambiato la password salvata: ricordati di cambiarla anche sul server.", xalign=0
        )
        self.server_hint.set_line_wrap(True)
        self.server_hint.set_no_show_all(True)
        self.server_hint.get_style_context().add_class("muted")
        grid.attach(self.server_hint, 1, 6, 1, 1)

        self.notes = Gtk.TextView()
        self.notes.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.notes.set_left_margin(8)
        self.notes.set_right_margin(8)
        self.notes.set_top_margin(6)
        self.notes.set_bottom_margin(6)
        self.notes.get_buffer().set_text(self.credential.notes)
        notes_scroll = Gtk.ScrolledWindow()
        notes_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        notes_scroll.set_min_content_height(76)
        notes_scroll.set_shadow_type(Gtk.ShadowType.IN)
        notes_scroll.get_style_context().add_class("vault-notes")
        notes_scroll.add(self.notes)
        notes_caption = Gtk.Label.new_with_mnemonic("No_te")
        caption(self.notes, notes_caption, 7)
        notes_caption.set_valign(Gtk.Align.START)
        grid.attach(notes_scroll, 1, 7, 1, 1)

        self.name.connect("changed", self._update_state)
        self.password.connect("changed", self._update_state)
        self._update_state()
        self.show_all()
        self.name.grab_focus()

    def _update_state(self, *_args) -> None:
        self.set_response_sensitive(Gtk.ResponseType.OK, bool(self.name.get_text().strip()))
        changed = not self.is_new and self.original_password and self.password.get_text() != self.original_password
        self.server_hint.set_visible(bool(changed))

    def on_generate(self, _button: Gtk.Button) -> None:
        self.password.set_text(generate_password())
        self.password.set_visibility(True)
        self.password.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, "view-conceal-symbolic")
        self.password.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Nascondi la password")

    def get_credential(self) -> Credential:
        buffer = self.notes.get_buffer()
        notes = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        return Credential(
            id=self.credential.id,
            name=self.name.get_text().strip() or "Senza nome",
            username=self.username.get_text().strip(),
            domain=self.domain.get_text().strip(),
            password=self.password.get_text(),
            notes=notes.strip(),
            group=self.group.get_child().get_text().strip(),
        )


class RdpPasswordDialog(Gtk.Dialog):
    """Richiesta password RDP quando la sessione non ha credenziale collegata."""

    def __init__(self, parent: Gtk.Window, session: Session) -> None:
        super().__init__(title="Password RDP", transient_for=parent, flags=0)
        self.set_modal(True)
        self.add_buttons("_Annulla", Gtk.ResponseType.CANCEL, "Connetti", Gtk.ResponseType.OK)
        self.set_default_size(520, 230)

        box = self.get_content_area()
        grid = Gtk.Grid(column_spacing=12, row_spacing=10, margin=14)
        box.add(grid)

        info = Gtk.Label(
            label=f"Inserisci la password per {session.name or session.host}. Non sarà salvata in chiaro.",
            xalign=0,
        )
        info.set_line_wrap(True)
        grid.attach(info, 0, 0, 2, 1)

        self.username = Gtk.Entry(text=session.user)
        self.domain = Gtk.Entry(text=session.domain)
        self.password = Gtk.Entry()
        self.password.set_visibility(False)
        self.password.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.password.set_activates_default(True)
        self.save_to_vault = Gtk.CheckButton(label="Salva questa credenziale nel Password Manager cifrato")
        self.save_to_vault.set_active(True)
        show = Gtk.CheckButton(label="Mostra password")
        show.connect("toggled", lambda b: self.password.set_visibility(b.get_active()))

        rows = [("Utente", self.username), ("Dominio", self.domain), ("Password", self.password)]
        for row, (label, widget) in enumerate(rows, start=1):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
        grid.attach(self.save_to_vault, 1, 4, 1, 1)
        grid.attach(show, 1, 5, 1, 1)
        self.set_default_response(Gtk.ResponseType.OK)
        self.show_all()

    def get_values(self) -> Tuple[str, str, str, bool]:
        return (
            self.username.get_text().strip(),
            self.domain.get_text().strip(),
            self.password.get_text(),
            bool(self.save_to_vault.get_active()),
        )


class VaultManagerDialog(Gtk.Dialog):
    """Finestra Password: elenco raggruppato a sinistra, dettaglio della credenziale a destra.

    Le azioni di tutti i giorni (copiare utente e password, vedere dove si usa una credenziale)
    stanno accanto ai dati; quelle rare (backup, cambio master password) nel menu in alto.
    """

    def __init__(self, parent: Gtk.Window, vault: PasswordVault) -> None:
        super().__init__(title="Password", transient_for=parent, flags=0)
        self.set_modal(True)
        self.set_resizable(True)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.set_default_size(960, 640)
        self.set_size_request(720, 480)
        self.get_style_context().add_class("modern-dialog")
        self.get_style_context().add_class("vault-dialog")
        self.parent_app = parent
        self.vault = vault
        self.filter_text = ""
        self.selected_id = ""
        self._rows: Dict[str, Gtk.ListBoxRow] = {}

        close = self.add_button("_Chiudi", Gtk.ResponseType.CLOSE)
        close.get_parent().set_margin_end(14)
        close.get_parent().set_margin_bottom(12)
        self.connect("delete-event", lambda d, _e: (d.response(Gtk.ResponseType.CLOSE), True)[1])
        self.connect("key-press-event", self.on_key_press)
        self.connect("button-press-event", lambda *_a: self._activity())

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=14)
        outer.get_style_context().add_class("vault-shell")
        self.get_content_area().pack_start(outer, True, True, 0)

        # ---------------------------------------------------------------- intestazione
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.get_style_context().add_class("vault-header")
        header.pack_start(_vault_image(parent, ["dialog-password-symbolic", "dialog-password"], 30), False, False, 0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        heading = Gtk.Label(label="Password", xalign=0)
        heading.get_style_context().add_class("dialog-heading")
        self.lock_state = Gtk.Label(xalign=0)
        self.lock_state.set_line_wrap(True)
        self.lock_state.get_style_context().add_class("muted")
        titles.pack_start(heading, False, False, 0)
        titles.pack_start(self.lock_state, False, False, 0)
        header.pack_start(titles, True, True, 0)

        self.btn_lock = _vault_button(
            parent, Gtk.Label.new_with_mnemonic("_Blocca"),
            ["changes-prevent-symbolic", "system-lock-screen-symbolic"], self.on_lock,
        )
        self.btn_lock.set_tooltip_text("Chiude la finestra e richiede la master password al prossimo uso")
        self.btn_lock.set_valign(Gtk.Align.CENTER)
        header.pack_start(self.btn_lock, False, False, 0)

        self.menu_button = Gtk.MenuButton()
        self.menu_button.add(_vault_image(parent, ["open-menu-symbolic", "view-more-symbolic"], 16))
        self.menu_button.set_tooltip_text("Backup e master password")
        self.menu_button.set_valign(Gtk.Align.CENTER)
        self.menu_button.get_style_context().add_class("secondary-action")
        menu = Gtk.Menu()
        for text, callback in (
            ("Cambia master password…", self.on_change_master),
            (None, None),
            ("Esporta backup cifrato…", self.on_export_encrypted),
            ("Importa backup cifrato…", self.on_import_encrypted),
        ):
            item = Gtk.SeparatorMenuItem() if text is None else Gtk.MenuItem(label=text)
            if callback is not None:
                item.connect("activate", lambda _i, cb=callback: cb(None))
            menu.append(item)
        menu.show_all()
        self.menu_button.set_popup(menu)
        header.pack_start(self.menu_button, False, False, 0)
        outer.pack_start(header, False, False, 0)

        # ---------------------------------------------------------------- suggerimento gruppi
        self.hint = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.hint.get_style_context().add_class("vault-hint")
        self.hint_text = Gtk.Label(xalign=0)
        self.hint_text.set_line_wrap(True)
        self.hint.pack_start(self.hint_text, True, True, 0)
        self.btn_apply_groups = _vault_button(
            parent, Gtk.Label.new_with_mnemonic("_Raggruppa"), ["view-list-symbolic", "folder-symbolic"],
            self.on_apply_groups,
        )
        self.btn_apply_groups.set_valign(Gtk.Align.CENTER)
        self.hint.pack_start(self.btn_apply_groups, False, False, 0)
        hint_close = _vault_icon_button(
            parent, ["window-close-symbolic"], "Non ora",
            lambda _b: (self.hint.set_no_show_all(True), self.hint.hide()),
        )
        hint_close.set_valign(Gtk.Align.CENTER)
        self.hint.pack_start(hint_close, False, False, 0)
        outer.pack_start(self.hint, False, False, 0)

        # ---------------------------------------------------------------- corpo a due pannelli
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_wide_handle(True)
        paned.set_position(330)
        outer.pack_start(paned, True, True, 0)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        side.get_style_context().add_class("vault-sidebar")
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Cerca nome, utente, gruppo… (Ctrl+F)")
        self.search.get_style_context().add_class("vault-search")
        self.search.connect("search-changed", self.on_search_changed)
        self.search.connect("activate", self.on_search_activate)
        self.search.connect("key-press-event", self.on_search_key)
        side.pack_start(self.search, False, False, 0)
        self.btn_add = _vault_button(
            parent, Gtk.Label.new_with_mnemonic("_Nuova credenziale"), ["list-add-symbolic", "document-new-symbolic"],
            self.on_add, "suggested-action",
        )
        self.btn_add.set_tooltip_text("Ctrl+N")
        side.pack_start(self.btn_add, False, False, 0)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.get_style_context().add_class("vault-list")
        self.listbox.set_header_func(self._row_header, None)
        self.listbox.connect("row-selected", self.on_row_selected)
        self.listbox.connect("row-activated", lambda *_a: self.on_edit(None))
        self.no_match = Gtk.Label(label="", xalign=0.5)
        self.no_match.set_line_wrap(True)
        self.no_match.set_margin_top(24)
        self.no_match.set_margin_start(12)
        self.no_match.set_margin_end(12)
        self.no_match.get_style_context().add_class("muted")
        self.no_match.show()
        self.listbox.set_placeholder(self.no_match)
        list_scroll = Gtk.ScrolledWindow()
        list_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scroll.get_style_context().add_class("vault-list-scroll")
        list_scroll.add(self.listbox)
        side.pack_start(list_scroll, True, True, 0)
        self.count_label = Gtk.Label(xalign=0)
        self.count_label.get_style_context().add_class("muted")
        side.pack_start(self.count_label, False, False, 0)
        paned.pack1(side, False, False)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.get_style_context().add_class("vault-detail")
        self.stack.add_named(self._build_empty_page(), "empty")
        self.stack.add_named(self._build_nothing_page(), "nothing")
        self.stack.add_named(self._build_detail_page(), "detail")
        paned.pack2(self.stack, True, False)

        # Prima show_all, poi i contenuti: Gtk.Stack ignora il cambio verso una pagina non ancora
        # visibile e il suggerimento dei gruppi va nascosto dopo, non prima.
        self.show_all()
        self.refresh()
        self._update_lock_state()
        self._update_hint()
        self.search.grab_focus()
        # Se il vault viene bloccato mentre la finestra e' aperta (inattivita', minimizzazione),
        # la finestra si chiude invece di mostrare dati che non potrebbe piu' salvare.
        self._watch = GLib.timeout_add_seconds(2, self._check_still_unlocked)
        self.connect("destroy", self._stop_watch)

    # ------------------------------------------------------------------ pagine del dettaglio
    def _centered_page(self, icons: List[str], title: str, text: str, button: Optional[Gtk.Button] = None) -> Gtk.Box:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=24)
        page.set_valign(Gtk.Align.CENTER)
        page.set_halign(Gtk.Align.CENTER)
        page.get_style_context().add_class("vault-empty")
        image = _vault_image(self.parent_app, icons, 64)
        image.get_style_context().add_class("vault-empty-icon")
        page.pack_start(image, False, False, 0)
        heading = Gtk.Label(label=title)
        heading.get_style_context().add_class("vault-empty-title")
        page.pack_start(heading, False, False, 0)
        body = Gtk.Label(label=text)
        body.set_line_wrap(True)
        body.set_max_width_chars(48)
        body.set_justify(Gtk.Justification.CENTER)
        body.get_style_context().add_class("muted")
        page.pack_start(body, False, False, 0)
        if button is not None:
            button.set_halign(Gtk.Align.CENTER)
            page.pack_start(button, False, False, 6)
        return page

    def _build_empty_page(self) -> Gtk.Box:
        first = _vault_button(
            self.parent_app, Gtk.Label.new_with_mnemonic("_Aggiungi la prima credenziale"),
            ["list-add-symbolic"], self.on_add, "suggested-action",
        )
        return self._centered_page(
            ["dialog-password-symbolic", "dialog-password"],
            "Nessuna password salvata",
            "Salva qui utenti e password dei server: restano cifrate con la master password. "
            "Nella finestra di una sessione scegli poi la credenziale da usare, così FedTermHub "
            "si collega senza chiederla ogni volta.",
            first,
        )

    def _build_nothing_page(self) -> Gtk.Box:
        return self._centered_page(
            ["dialog-password-symbolic", "dialog-password"],
            "Scegli una credenziale",
            "Selezionala nell'elenco per vederne i dati, copiarli o modificarli.",
        )

    def _build_detail_page(self) -> Gtk.Box:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin=20)
        page.get_style_context().add_class("vault-detail-page")

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.d_title = Gtk.Label(xalign=0)
        self.d_title.set_line_wrap(True)
        self.d_title.set_selectable(True)
        self.d_title.get_style_context().add_class("vault-detail-title")
        self.d_group = Gtk.Label(xalign=0)
        self.d_group.get_style_context().add_class("muted")
        title_box.pack_start(self.d_title, False, False, 0)
        title_box.pack_start(self.d_group, False, False, 0)
        page.pack_start(title_box, False, False, 0)

        grid = Gtk.Grid(column_spacing=10, row_spacing=4)
        grid.get_style_context().add_class("vault-fields")
        page.pack_start(grid, False, False, 0)
        self.d_rows: Dict[str, List[Gtk.Widget]] = {}

        def field(key: str, row: int, caption_text: str, extra: List[Gtk.Widget]) -> Gtk.Label:
            caption_label = Gtk.Label(label=caption_text, xalign=0)
            caption_label.set_valign(Gtk.Align.START)
            caption_label.get_style_context().add_class("vault-field-caption")
            value = Gtk.Label(xalign=0)
            value.set_hexpand(True)
            value.set_line_wrap(True)
            value.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            value.set_selectable(True)
            value.get_style_context().add_class("vault-field-value")
            grid.attach(caption_label, 0, row, 1, 1)
            grid.attach(value, 1, row, 1, 1)
            for col, widget in enumerate(extra, start=2):
                widget.set_valign(Gtk.Align.CENTER)
                grid.attach(widget, col, row, 1, 1)
            self.d_rows[key] = [caption_label, value] + extra
            return value

        copy_icons = ["edit-copy-symbolic", "edit-copy"]
        self.d_user = field("user", 0, "Utente", [
            _vault_icon_button(self.parent_app, copy_icons, "Copia utente (Ctrl+B)", self.on_copy_username),
        ])
        self.d_domain = field("domain", 1, "Dominio", [
            _vault_icon_button(self.parent_app, copy_icons, "Copia dominio", self.on_copy_domain),
        ])
        self.reveal = Gtk.ToggleButton()
        self.reveal.add(_vault_image(self.parent_app, ["view-reveal-symbolic", "view-visible-symbolic"], 16))
        self.reveal.set_tooltip_text("Mostra la password")
        self.reveal.set_relief(Gtk.ReliefStyle.NONE)
        self.reveal.get_style_context().add_class("vault-icon-button")
        self.reveal.connect("toggled", lambda *_a: self._show_password())
        self.btn_copy_password = _vault_icon_button(
            self.parent_app, copy_icons, "Copia password (Ctrl+C)", self.on_copy_password
        )
        self.d_password = field("password", 2, "Password", [self.reveal, self.btn_copy_password])
        self.d_password.get_style_context().add_class("vault-secret")
        self.d_notes = field("notes", 3, "Note", [])

        usage_caption = Gtk.Label(label="Usata da", xalign=0)
        usage_caption.get_style_context().add_class("vault-field-caption")
        page.pack_start(usage_caption, False, False, 0)
        self.d_usage = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.d_usage.get_style_context().add_class("vault-usage")
        usage_scroll = Gtk.ScrolledWindow()
        usage_scroll.get_style_context().add_class("vault-usage-scroll")
        usage_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        usage_scroll.set_propagate_natural_height(True)
        usage_scroll.set_max_content_height(150)
        usage_scroll.add(self.d_usage)
        page.pack_start(usage_scroll, False, False, 0)

        page.pack_start(Gtk.Box(), True, True, 0)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.btn_edit = _vault_button(
            self.parent_app, Gtk.Label.new_with_mnemonic("_Modifica"),
            ["document-edit-symbolic", "accessories-text-editor-symbolic"], self.on_edit,
        )
        self.btn_edit.set_tooltip_text("Invio o doppio clic sull'elenco")
        self.btn_del = _vault_button(
            self.parent_app, Gtk.Label.new_with_mnemonic("_Elimina"),
            ["user-trash-symbolic", "edit-delete-symbolic"], self.on_delete, "vault-delete",
        )
        self.btn_del.set_tooltip_text("Canc")
        actions.pack_start(self.btn_edit, False, False, 0)
        actions.pack_end(self.btn_del, False, False, 0)
        page.pack_start(actions, False, False, 0)
        return page

    # ------------------------------------------------------------------ elenco
    @staticmethod
    def _row_header(row: Gtk.ListBoxRow, before: Optional[Gtk.ListBoxRow], _data=None) -> None:
        if not getattr(row, "show_groups", False):
            row.set_header(None)
            return
        if before is not None and getattr(before, "group", None) == row.group:
            row.set_header(None)
            return
        label = Gtk.Label(label=(row.group or "Senza gruppo").upper(), xalign=0)
        label.get_style_context().add_class("vault-group-header")
        row.set_header(label)

    def _make_row(self, cred: Credential, uses: int, show_groups: bool) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.cred_id = cred.id
        row.group = cred.group
        row.show_groups = show_groups
        row.get_style_context().add_class("vault-row")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        avatar = Gtk.Label(label=(cred.name.strip()[:1] or "?").upper())
        avatar.set_valign(Gtk.Align.CENTER)
        avatar.get_style_context().add_class("vault-avatar")
        box.pack_start(avatar, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        name = Gtk.Label(label=cred.name, xalign=0)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.get_style_context().add_class("vault-row-title")
        texts.pack_start(name, False, False, 0)
        account = cred.account_text()
        subtitle = Gtk.Label(label=account or "nessun utente", xalign=0)
        subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        subtitle.get_style_context().add_class("vault-row-subtitle")
        texts.pack_start(subtitle, False, False, 0)
        box.pack_start(texts, True, True, 0)
        if uses:
            badge = Gtk.Label(label=str(uses))
            badge.set_valign(Gtk.Align.CENTER)
            badge.set_tooltip_text("Usata da 1 sessione" if uses == 1 else f"Usata da {uses} sessioni")
            badge.get_style_context().add_class("vault-badge")
            box.pack_start(badge, False, False, 0)
        row.add(box)
        return row

    def _usage(self, cred_id: str) -> List[Tuple[str, str, str]]:
        try:
            return self.parent_app.credential_usage(cred_id)
        except Exception:
            return []

    def _matches(self, cred: Credential) -> bool:
        if not self.filter_text:
            return True
        haystack = " ".join((cred.name, cred.username, cred.domain, cred.group, cred.notes)).casefold()
        return all(word in haystack for word in self.filter_text.split())

    def refresh(self, select_id: Optional[str] = None) -> None:
        if select_id is not None:
            self.selected_id = select_id
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        self._rows = {}
        show_groups = bool(self.vault.groups())
        visible = 0
        for cred in self.vault.sorted_credentials():
            if not self._matches(cred):
                continue
            row = self._make_row(cred, len(self._usage(cred.id)), show_groups)
            self.listbox.add(row)
            self._rows[cred.id] = row
            visible += 1
        self.listbox.show_all()
        total = len(self.vault.credentials)
        if self.filter_text:
            self.no_match.set_text(f"Nessuna credenziale corrisponde a «{self.search.get_text().strip()}».")
            self.count_label.set_text(f"{visible} di {total} credenziali")
        else:
            self.no_match.set_text("Ancora nessuna credenziale.")
            self.count_label.set_text("1 credenziale" if total == 1 else f"{total} credenziali")
        row = self._rows.get(self.selected_id)
        if row is not None:
            self.listbox.select_row(row)
        else:
            self.listbox.unselect_all()
            self._show_detail(None)

    def select_credential(self, cred_id: str) -> None:
        row = self._rows.get(cred_id)
        if row is not None:
            self.listbox.select_row(row)
            row.grab_focus()

    def selected_credential(self) -> Optional[Credential]:
        row = self.listbox.get_selected_row()
        return self.vault.get(row.cred_id) if row is not None else None

    def on_row_selected(self, _listbox: Gtk.ListBox, row: Optional[Gtk.ListBoxRow]) -> None:
        if row is not None:
            self.selected_id = row.cred_id
        self._show_detail(self.vault.get(row.cred_id) if row is not None else None)

    # ------------------------------------------------------------------ dettaglio
    def _show_detail(self, cred: Optional[Credential]) -> None:
        if not self.vault.credentials:
            self.stack.set_visible_child_name("empty")
            return
        if cred is None:
            self.stack.set_visible_child_name("nothing")
            return
        self.stack.set_visible_child_name("detail")
        self.d_title.set_text(cred.name)
        self.d_group.set_text(cred.group or "Senza gruppo")
        for key, value in (("user", cred.username), ("domain", cred.domain), ("notes", cred.notes)):
            label = {"user": self.d_user, "domain": self.d_domain, "notes": self.d_notes}[key]
            label.set_text(value)
            for widget in self.d_rows[key]:
                widget.set_visible(bool(value))
        self.reveal.set_active(False)
        self._show_password()
        self.reveal.set_sensitive(bool(cred.password))
        self.btn_copy_password.set_sensitive(bool(cred.password))

        for child in self.d_usage.get_children():
            self.d_usage.remove(child)
        usage = self._usage(cred.id)
        if not usage:
            none = Gtk.Label(
                label="Nessuna sessione la usa. Per collegarla, apri una sessione e sceglila nel campo Credenziale.",
                xalign=0,
            )
            none.set_line_wrap(True)
            none.get_style_context().add_class("muted")
            self.d_usage.pack_start(none, False, False, 0)
        for name, kind, folder in usage:
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            tag = Gtk.Label(label=kind)
            tag.get_style_context().add_class("vault-usage-kind")
            line.pack_start(tag, False, False, 0)
            text = Gtk.Label(label=name, xalign=0)
            text.set_ellipsize(Pango.EllipsizeMode.END)
            line.pack_start(text, False, False, 0)
            if folder:
                where = Gtk.Label(label=folder, xalign=0)
                where.set_ellipsize(Pango.EllipsizeMode.START)
                where.get_style_context().add_class("muted")
                line.pack_start(where, True, True, 0)
            self.d_usage.pack_start(line, False, False, 0)
        self.d_usage.show_all()

    def _show_password(self) -> None:
        cred = self.selected_credential()
        if cred is None:
            return
        if not cred.password:
            self.d_password.set_text("nessuna password salvata")
        elif self.reveal.get_active():
            self.d_password.set_text(cred.password)
        else:
            self.d_password.set_text("•" * 12)
        self.reveal.set_tooltip_text("Nascondi la password" if self.reveal.get_active() else "Mostra la password")

    # ------------------------------------------------------------------ stato e suggerimenti
    def _update_lock_state(self) -> None:
        try:
            minutes = int(self.parent_app.settings.vault_auto_lock_minutes)
        except Exception:
            minutes = 0
        if minutes > 0:
            when = "1 minuto" if minutes == 1 else f"{minutes} minuti"
            text = f"Sbloccate · si bloccano dopo {when} senza attività"
        else:
            text = "Sbloccate finché FedTermHub resta aperto"
        self.lock_state.set_text(text)
        try:
            seconds = int(self.parent_app.settings.clear_clipboard_seconds)
        except Exception:
            seconds = 0
        if seconds > 0:
            self.btn_copy_password.set_tooltip_text(
                f"Copia password (Ctrl+C) · si cancella dagli appunti dopo {seconds} secondi"
            )

    def _group_suggestions(self) -> Dict[str, str]:
        try:
            return self.parent_app.credential_group_suggestions()
        except Exception:
            return {}

    def _update_hint(self) -> None:
        count = len(self._group_suggestions())
        # no_show_all impedisce che un show_all() successivo faccia ricomparire la barra vuota.
        self.hint.set_no_show_all(not count)
        if not count:
            self.hint.hide()
            return
        self.hint.show_all()
        what = "1 credenziale senza gruppo è usata" if count == 1 else f"{count} credenziali senza gruppo sono usate"
        self.hint_text.set_text(
            f"{what} da sessioni che stanno in una cartella. Posso raggrupparle col nome di quella cartella "
            "(di solito il cliente): le ritrovi insieme e puoi sempre cambiarlo."
        )

    def on_apply_groups(self, _button: Optional[Gtk.Button]) -> None:
        suggestions = self._group_suggestions()
        for cred in self.vault.credentials:
            if not cred.group and cred.id in suggestions:
                cred.group = suggestions[cred.id]
        self.vault.save()
        self.refresh()
        self._update_hint()
        self.parent_app.set_status(f"Raggruppate {len(suggestions)} credenziali per cartella.")

    def _activity(self) -> bool:
        try:
            self.parent_app.mark_user_activity()
        except Exception:
            pass
        return False

    def _check_still_unlocked(self) -> bool:
        if not self.vault.unlocked:
            self._watch = 0
            self.response(Gtk.ResponseType.CLOSE)
            return False
        return True

    def _stop_watch(self, *_args) -> None:
        if self._watch:
            GLib.source_remove(self._watch)
            self._watch = 0

    # ------------------------------------------------------------------ tastiera
    def on_key_press(self, _widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        self._activity()
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        key = Gdk.keyval_name(event.keyval) or ""
        focus = self.get_focus()
        # Nei campi di testo e sul testo selezionato a mano Ctrl+C e Canc fanno il loro solito lavoro.
        in_entry = isinstance(focus, (Gtk.Entry, Gtk.TextView)) or (
            isinstance(focus, Gtk.Label) and focus.get_selection_bounds()[0]
        )
        if ctrl and key.lower() == "f":
            self.search.grab_focus()
            return True
        if ctrl and key.lower() == "n":
            self.on_add(None)
            return True
        if in_entry:
            return False
        if ctrl and key.lower() == "c" and self.selected_credential() is not None:
            self.on_copy_password(None)
            return True
        if ctrl and key.lower() == "b" and self.selected_credential() is not None:
            self.on_copy_username(None)
            return True
        if key == "Delete" and self.selected_credential() is not None:
            self.on_delete(None)
            return True
        return False

    def on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self.filter_text = entry.get_text().strip().casefold()
        self.refresh()
        if self.filter_text and self.listbox.get_selected_row() is None:
            first = self.listbox.get_row_at_index(0)
            if first is not None:
                self.listbox.select_row(first)

    def on_search_activate(self, _entry: Gtk.SearchEntry) -> None:
        row = self.listbox.get_selected_row() or self.listbox.get_row_at_index(0)
        if row is not None:
            self.listbox.select_row(row)
            row.grab_focus()

    def on_search_key(self, _entry: Gtk.SearchEntry, event: Gdk.EventKey) -> bool:
        if Gdk.keyval_name(event.keyval) == "Down":
            self.on_search_activate(self.search)
            return True
        return False

    # ------------------------------------------------------------------ azioni
    def _save(self, select_id: Optional[str] = None) -> None:
        self.vault.save()
        self.refresh(select_id)
        self._update_hint()
        try:
            self.parent_app.refresh_sessions()
        except Exception:
            pass

    def on_add(self, _button: Optional[Gtk.Button]) -> None:
        dialog = CredentialDialog(self, groups=self.vault.groups())
        selected = self.selected_credential()
        if selected is not None and selected.group:
            dialog.group.get_child().set_text(selected.group)
        if dialog.run() == Gtk.ResponseType.OK:
            credential = dialog.get_credential()
            self.vault.credentials.append(credential)
            if self.filter_text and not self._matches(credential):
                self.search.set_text("")
            self._save(credential.id)
            self.select_credential(credential.id)
        dialog.destroy()

    def on_edit(self, _button: Optional[Gtk.Button]) -> None:
        cred = self.selected_credential()
        if cred is None:
            return
        dialog = CredentialDialog(self, cred, groups=self.vault.groups())
        if dialog.run() == Gtk.ResponseType.OK:
            updated = dialog.get_credential()
            for idx, item in enumerate(self.vault.credentials):
                if item.id == updated.id:
                    self.vault.credentials[idx] = updated
                    break
            self._save(updated.id)
        dialog.destroy()

    def on_delete(self, _button: Optional[Gtk.Button]) -> None:
        cred = self.selected_credential()
        if cred is None:
            return
        usage = self._usage(cred.id)
        confirm = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"Eliminare «{cred.name}»?",
        )
        if usage:
            names = ", ".join(name for name, _kind, _folder in usage[:4]) + ("…" if len(usage) > 4 else "")
            confirm.format_secondary_text(
                f"È usata da {len(usage)} {'sessione' if len(usage) == 1 else 'sessioni'} ({names}): "
                "resteranno salvate ma ti chiederanno la password a ogni connessione."
            )
        else:
            confirm.format_secondary_text("Nessuna sessione la usa. L'eliminazione non si può annullare.")
        confirm.add_button("_Annulla", Gtk.ResponseType.CANCEL)
        delete = confirm.add_button("_Elimina", Gtk.ResponseType.OK)
        delete.get_style_context().add_class("destructive-action")
        confirm.set_default_response(Gtk.ResponseType.CANCEL)
        response = confirm.run()
        confirm.destroy()
        if response != Gtk.ResponseType.OK:
            return
        index = self.listbox.get_selected_row().get_index()
        self.vault.credentials = [x for x in self.vault.credentials if x.id != cred.id]
        self._save("")
        neighbour = self.listbox.get_row_at_index(min(index, len(self._rows) - 1)) if self._rows else None
        if neighbour is not None:
            self.listbox.select_row(neighbour)
        self.parent_app.set_status(f"Credenziale «{cred.name}» eliminata.")

    def on_copy_username(self, _button: Optional[Gtk.Button]) -> None:
        cred = self.selected_credential()
        if cred is not None:
            self.parent_app.copy_sensitive_text(cred.account_text() if cred.domain else cred.username, "Utente")

    def on_copy_domain(self, _button: Optional[Gtk.Button]) -> None:
        cred = self.selected_credential()
        if cred is not None:
            self.parent_app.copy_sensitive_text(cred.domain, "Dominio")

    def on_copy_password(self, _button: Optional[Gtk.Button]) -> None:
        cred = self.selected_credential()
        if cred is not None:
            self.parent_app.copy_sensitive_text(cred.password, "Password")

    def on_change_master(self, _button: Optional[Gtk.Button]) -> None:
        dialog = MasterPasswordDialog(self, mode="change")

        def change() -> Optional[str]:
            if not self.vault.verify_master_password(dialog.get_current_password()):
                return "La master password attuale non è corretta."
            try:
                self.vault.change_master_password(dialog.get_password())
            except Exception as exc:
                return f"Impossibile cambiare la master password: {exc}"
            return None

        changed = dialog.run_validated(change)
        dialog.destroy()
        if changed:
            self.parent_app.set_status("Master password cambiata. Le credenziali sono state cifrate di nuovo.")

    def on_export_encrypted(self, _button: Optional[Gtk.Button]) -> None:
        self.parent_app.export_vault_encrypted(self)
        self.refresh()

    def on_import_encrypted(self, _button: Optional[Gtk.Button]) -> None:
        self.parent_app.import_vault_encrypted(self)
        self.refresh()
        self._update_hint()

    def on_lock(self, _button: Optional[Gtk.Button]) -> None:
        self.vault.lock()
        self.response(Gtk.ResponseType.CLOSE)


class TunnelDialog(Gtk.Dialog):
    """Editor visuale e robusto per tunnel SSH.

    Il layout usa un unico viewport verticale e tre blocchi allineati. In questo
    modo non dipende dalle dimensioni minime calcolate dal tema GTK e rimane
    utilizzabile anche con KDE/XWayland e con fattori di scala elevati.
    """

    def __init__(
        self,
        parent: Gtk.Window,
        tunnel: Optional[TunnelSession] = None,
        credentials: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        super().__init__(
            title="Nuovo tunnel SSH" if tunnel is None else "Modifica tunnel SSH",
            transient_for=parent,
            flags=0,
        )
        self.set_modal(True)
        self.set_default_size(1020, 760)
        self.set_resizable(True)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.connect("delete-event", lambda d, _e: (d.response(Gtk.ResponseType.CANCEL), True)[1])
        self.parent_app = getattr(parent, "parent_app", parent)
        self._destroyed = False
        self.connect("destroy", lambda *_: setattr(self, "_destroyed", True))
        self.tunnel = tunnel or TunnelSession()
        credentials = credentials or []

        cancel_button = self.add_button("Annulla", Gtk.ResponseType.CANCEL)
        cancel_button.get_style_context().add_class("secondary-action")
        action_area = self.get_action_area()
        action_area.set_spacing(8)
        self.test_button = Gtk.Button.new_with_mnemonic("_Verifica connessione")
        self.test_button.get_style_context().add_class("secondary-action")
        self.test_button.connect("clicked", self.on_test)
        action_area.pack_start(self.test_button, False, False, 0)
        self.save_button = Gtk.Button.new_with_mnemonic("_Salva tunnel")
        self.save_button.get_style_context().add_class("suggested-action")
        self.save_button.connect("clicked", self.on_save)
        action_area.pack_end(self.save_button, False, False, 0)

        content = self.get_content_area()
        content.set_spacing(0)
        content.get_style_context().add_class("tunnel-dialog-content")
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=14)
        shell.get_style_context().add_class("tunnel-editor")
        content.add(shell)

        # Intestazione fissa.
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.get_style_context().add_class("tunnel-editor-header")
        icon = self.parent_app.make_icon_image(
            ["network-vpn", "network-transmit-receive", "network-vpn-symbolic"], 30
        ) if hasattr(self.parent_app, "make_icon_image") else Gtk.Image.new_from_icon_name(
            "network-vpn-symbolic", Gtk.IconSize.DIALOG
        )
        header.pack_start(icon, False, False, 0)
        header_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        heading = Gtk.Label(label="Tunnel SSH", xalign=0)
        heading.get_style_context().add_class("dialog-heading")
        subtitle = Gtk.Label(
            label="Crea un inoltro locale, remoto oppure un proxy SOCKS.",
            xalign=0,
        )
        subtitle.get_style_context().add_class("muted")
        header_text.pack_start(heading, False, False, 0)
        header_text.pack_start(subtitle, False, False, 0)
        header.pack_start(header_text, True, True, 0)
        shell.pack_start(header, False, False, 0)

        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        name_label = Gtk.Label(label="Nome", xalign=0)
        name_label.get_style_context().add_class("field-label")
        name_label.set_size_request(90, -1)
        name_row.pack_start(name_label, False, False, 0)
        self.name = Gtk.Entry(text=self.tunnel.name)
        self.name.set_hexpand(True)
        self.name.set_placeholder_text("es. RDP cliente tramite bastion")
        name_row.pack_start(self.name, True, True, 0)
        shell.pack_start(name_row, False, False, 0)

        # Selettore del tipo, volutamente simile a un pannello di port forwarding.
        mode_strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        mode_strip.get_style_context().add_class("tunnel-type-selector")
        self.radio_local = Gtk.RadioButton.new_with_label_from_widget(None, "Locale  (-L)")
        self.radio_remote = Gtk.RadioButton.new_with_label_from_widget(self.radio_local, "Remoto  (-R)")
        self.radio_dynamic = Gtk.RadioButton.new_with_label_from_widget(self.radio_local, "SOCKS  (-D)")
        for radio in (self.radio_local, self.radio_remote, self.radio_dynamic):
            radio.set_hexpand(True)
            radio.set_halign(Gtk.Align.FILL)
            radio.connect("toggled", self.on_type_changed)
            mode_strip.pack_start(radio, True, True, 0)
        shell.pack_start(mode_strip, False, False, 0)

        # Un solo viewport evita i difetti di clipping prodotti dai Grid annidati.
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.NONE)
        scroll.set_min_content_height(430)
        scroll.get_style_context().add_class("tunnel-editor-scroll")
        viewport = Gtk.Viewport()
        viewport.set_shadow_type(Gtk.ShadowType.NONE)
        viewport.get_style_context().add_class("tunnel-editor-viewport")
        scroll.add(viewport)
        shell.pack_start(scroll, True, True, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=4)
        body.set_hexpand(True)
        viewport.add(body)

        # Diagramma del percorso.
        route_frame = Gtk.Frame()
        route_frame.get_style_context().add_class("tunnel-route-card")
        route_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=12)
        route_frame.add(route_box)
        route_heading = Gtk.Label(label="Percorso della connessione", xalign=0)
        route_heading.get_style_context().add_class("section-heading")
        route_box.pack_start(route_heading, False, False, 0)

        route_grid = Gtk.Grid(column_spacing=10, row_spacing=4)
        route_grid.set_hexpand(True)
        route_grid.set_column_homogeneous(False)
        self.flow_left = self._route_node("computer-symbolic", "Client locale", "127.0.0.1")
        self.flow_ssh = self._route_node("network-vpn-symbolic", "Server SSH", "host:22")
        self.flow_right = self._route_node("network-server-symbolic", "Servizio remoto", "host:porta")
        self.arrow1 = Gtk.Label(label="→")
        self.arrow2 = Gtk.Label(label="→")
        for arrow in (self.arrow1, self.arrow2):
            arrow.get_style_context().add_class("tunnel-route-arrow")
            arrow.set_halign(Gtk.Align.CENTER)
            arrow.set_valign(Gtk.Align.CENTER)
        route_grid.attach(self.flow_left, 0, 0, 1, 1)
        route_grid.attach(self.arrow1, 1, 0, 1, 1)
        route_grid.attach(self.flow_ssh, 2, 0, 1, 1)
        route_grid.attach(self.arrow2, 3, 0, 1, 1)
        route_grid.attach(self.flow_right, 4, 0, 1, 1)
        route_grid.set_column_spacing(12)
        route_box.pack_start(route_grid, False, False, 0)

        self.explain = Gtk.Label(xalign=0)
        self.explain.set_line_wrap(True)
        self.explain.get_style_context().add_class("tunnel-explain")
        route_box.pack_start(self.explain, False, False, 0)
        body.pack_start(route_frame, False, False, 0)

        # I tre blocchi corrispondono ai tre elementi del diagramma.
        fields_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        fields_row.set_hexpand(True)
        fields_row.get_style_context().add_class("tunnel-fields-row")
        body.pack_start(fields_row, False, False, 0)

        listen_card, listen_grid = self._section("Porta di ascolto", "network-wired-symbolic", min_width=250)
        ssh_card, ssh_grid = self._section("Server SSH", "network-vpn-symbolic", min_width=330)
        destination_card, destination_box = self._section(
            "Destinazione", "network-server-symbolic", vertical=True, min_width=250
        )
        fields_row.pack_start(listen_card, True, True, 0)
        fields_row.pack_start(ssh_card, True, True, 0)
        fields_row.pack_start(destination_card, True, True, 0)

        # Porta di ascolto.
        self.bind_label = Gtk.Label(label="Indirizzo locale", xalign=0)
        self.bind_address = Gtk.Entry(text=self.tunnel.bind_address or "127.0.0.1")
        self.bind_address.set_placeholder_text("127.0.0.1, 0.0.0.0 oppure ::1")
        self.port_label = Gtk.Label(label="Porta locale", xalign=0)
        self.local_port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.local_port.set_value(
            self._safe_int(self.tunnel.local_port, 1080 if self.tunnel.tunnel_type == "dynamic" else 13389)
        )
        listen_grid.attach(self.bind_label, 0, 0, 1, 1)
        listen_grid.attach(self.bind_address, 1, 0, 1, 1)
        listen_grid.attach(self.port_label, 0, 1, 1, 1)
        listen_grid.attach(self.local_port, 1, 1, 1, 1)

        # Accesso al server SSH.
        self.ssh_host = Gtk.Entry(text=self.tunnel.ssh_host)
        self.ssh_host.set_placeholder_text("bastion.example.local")
        self.ssh_port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.ssh_port.set_value(self._safe_int(self.tunnel.ssh_port, 22))
        self.ssh_user = Gtk.Entry(text=self.tunnel.ssh_user)
        self.ssh_user.set_placeholder_text("utente SSH")
        self.credential = Gtk.ComboBoxText()
        self.credential.append("", "Nessuna credenziale collegata")
        found = self.tunnel.credential_id == ""
        for cred_id, cred_name in credentials:
            self.credential.append(cred_id, cred_name)
            found = found or cred_id == self.tunnel.credential_id
        if not found and self.tunnel.credential_id:
            self.credential.append(self.tunnel.credential_id, f"Credenziale {self.tunnel.credential_id[:12]}…")
        self.credential.set_active_id(self.tunnel.credential_id or "")
        if self.credential.get_active() < 0:
            self.credential.set_active(0)
        self.use_password = Gtk.CheckButton(label="Usa la password salvata nel Password Manager")
        self.use_password.set_active(self.tunnel.use_credential_password)
        self.identity_file = Gtk.Entry(text=self.tunnel.identity_file)
        self.identity_file.set_placeholder_text("~/.ssh/id_ed25519")
        browse = Gtk.Button.new_from_icon_name("document-open-symbolic", Gtk.IconSize.MENU)
        browse.set_tooltip_text("Scegli una chiave privata")
        browse.connect("clicked", self.on_choose_identity)
        identity_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        identity_box.pack_start(self.identity_file, True, True, 0)
        identity_box.pack_start(browse, False, False, 0)
        self._form_row(ssh_grid, 0, "Host", self.ssh_host)
        self._form_row(ssh_grid, 1, "Porta", self.ssh_port)
        self._form_row(ssh_grid, 2, "Utente", self.ssh_user)
        self._form_row(ssh_grid, 3, "Credenziale", self.credential)
        ssh_grid.attach(self.use_password, 1, 4, 1, 1)
        self._form_row(ssh_grid, 5, "Chiave privata", identity_box)

        # Destinazione: per SOCKS il pannello viene sostituito con una spiegazione.
        self.destination_stack = Gtk.Stack()
        self.destination_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.destination_stack.set_transition_duration(140)
        destination_box.pack_start(self.destination_stack, True, True, 0)
        destination_grid = Gtk.Grid(column_spacing=9, row_spacing=8)
        self.remote_host = Gtk.Entry(text=self.tunnel.remote_host or "127.0.0.1")
        self.remote_host.set_placeholder_text("Server o servizio finale")
        self.remote_port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.remote_port.set_value(self._safe_int(self.tunnel.remote_port, 3389))
        self.remote_host_label = Gtk.Label(label="Host", xalign=0)
        self.remote_port_label = Gtk.Label(label="Porta", xalign=0)
        destination_grid.attach(self.remote_host_label, 0, 0, 1, 1)
        destination_grid.attach(self.remote_host, 1, 0, 1, 1)
        destination_grid.attach(self.remote_port_label, 0, 1, 1, 1)
        destination_grid.attach(self.remote_port, 1, 1, 1, 1)
        self.destination_stack.add_named(destination_grid, "destination")
        socks_info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=6)
        socks_icon = Gtk.Image.new_from_icon_name("network-workgroup-symbolic", Gtk.IconSize.DIALOG)
        socks_icon.set_pixel_size(38)
        socks_text = Gtk.Label(
            label="La destinazione viene scelta dall'applicazione che usa il proxy SOCKS.",
            xalign=0,
        )
        socks_text.set_line_wrap(True)
        socks_text.get_style_context().add_class("muted")
        socks_info.pack_start(socks_icon, False, False, 0)
        socks_info.pack_start(socks_text, False, False, 0)
        self.destination_stack.add_named(socks_info, "socks")

        # Affidabilità in una riga separata, senza comprimere i campi principali.
        reliability_card, reliability_grid = self._section(
            "Affidabilità e avvio", "emblem-system-symbolic", min_width=0
        )
        body.pack_start(reliability_card, False, False, 0)
        self.exit_on_failure = Gtk.CheckButton(label="Arresta se l'inoltro non può essere aperto")
        self.exit_on_failure.set_active(self.tunnel.exit_on_forward_failure)
        self.compression = Gtk.CheckButton(label="Compressione SSH")
        self.compression.set_active(self.tunnel.compression)
        self.auto_start = Gtk.CheckButton(label="Avvia automaticamente con FedTermHub")
        self.auto_start.set_active(self.tunnel.auto_start)
        self.auto_restart = Gtk.CheckButton(label="Riconnetti automaticamente se il tunnel cade")
        self.auto_restart.set_active(self.tunnel.auto_restart)
        self.keepalive = Gtk.SpinButton.new_with_range(0, 3600, 5)
        self.keepalive.set_value(self.tunnel.server_alive_interval)
        self.keepalive_count = Gtk.SpinButton.new_with_range(1, 100, 1)
        self.keepalive_count.set_value(self.tunnel.server_alive_count)
        self.restart_delay = Gtk.SpinButton.new_with_range(1, 300, 1)
        self.restart_delay.set_value(self.tunnel.restart_delay)
        reliability_grid.attach(self.exit_on_failure, 0, 0, 2, 1)
        reliability_grid.attach(self.compression, 2, 0, 2, 1)
        reliability_grid.attach(self.auto_start, 0, 1, 2, 1)
        reliability_grid.attach(self.auto_restart, 2, 1, 2, 1)
        self._form_row(reliability_grid, 2, "Keepalive (s)", self.keepalive, label_column=0, widget_column=1)
        self._form_row(reliability_grid, 2, "Tentativi", self.keepalive_count, label_column=2, widget_column=3)
        self._form_row(reliability_grid, 3, "Ritardo riavvio (s)", self.restart_delay, label_column=0, widget_column=1)

        advanced = Gtk.Expander(label="Opzioni avanzate")
        advanced.get_style_context().add_class("tunnel-advanced")
        body.pack_start(advanced, False, False, 0)
        adv_grid = Gtk.Grid(column_spacing=10, row_spacing=8, margin=10)
        advanced.add(adv_grid)
        self.address_family = Gtk.ComboBoxText()
        self.address_family.append("auto", "Automatica")
        self.address_family.append("ipv4", "Solo IPv4")
        self.address_family.append("ipv6", "Solo IPv6")
        self.address_family.set_active_id(self.tunnel.address_family)
        self.verbose_log = Gtk.CheckButton(label="Log SSH dettagliato (-v)")
        self.verbose_log.set_active(self.tunnel.verbose_log)
        self.options = Gtk.Entry(text=self.tunnel.options)
        self.options.set_placeholder_text("es. -o ProxyJump=jump.example")
        self._form_row(adv_grid, 0, "Famiglia IP", self.address_family)
        adv_grid.attach(self.verbose_log, 1, 1, 1, 1)
        self._form_row(adv_grid, 2, "Opzioni extra", self.options)

        command_expander = Gtk.Expander(label="Anteprima del comando")
        command_expander.get_style_context().add_class("tunnel-advanced")
        body.pack_start(command_expander, False, False, 0)
        command_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        command_expander.add(command_box)
        self.command_preview = Gtk.TextView()
        self.command_preview.set_editable(False)
        self.command_preview.set_cursor_visible(False)
        self.command_preview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.command_preview.set_monospace(True)
        self.command_preview.set_size_request(-1, 76)
        command_box.pack_start(self.command_preview, True, True, 0)

        self.validation = Gtk.Label(label="", xalign=0)
        self.validation.set_line_wrap(True)
        self.validation.set_no_show_all(True)
        self.validation.get_style_context().add_class("tunnel-validation")
        shell.pack_start(self.validation, False, False, 0)

        for widget in (
            self.name,
            self.ssh_host,
            self.ssh_user,
            self.bind_address,
            self.remote_host,
            self.identity_file,
            self.options,
        ):
            widget.connect("changed", self.update_preview)
        for widget in (
            self.ssh_port,
            self.local_port,
            self.remote_port,
            self.keepalive,
            self.keepalive_count,
            self.restart_delay,
        ):
            widget.connect("value-changed", self.update_preview)
        self.credential.connect("changed", self.update_preview)
        self.address_family.connect("changed", self.update_preview)
        for widget in (
            self.use_password,
            self.exit_on_failure,
            self.compression,
            self.auto_start,
            self.auto_restart,
            self.verbose_log,
        ):
            widget.connect("toggled", self.update_preview)

        self.set_type(self.tunnel.tunnel_type)
        self.update_preview()
        self.show_all()
        self.validation.hide()

    @staticmethod
    def _safe_int(value: object, default: int) -> int:
        try:
            return max(1, min(int(str(value)), 65535))
        except (TypeError, ValueError):
            return default

    def _section(
        self,
        title: str,
        icon: str,
        vertical: bool = False,
        min_width: int = 0,
    ):
        frame = Gtk.Frame()
        frame.set_hexpand(True)
        if min_width:
            frame.set_size_request(min_width, -1)
        frame.get_style_context().add_class("tunnel-settings-card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=11)
        frame.add(box)
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        image = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU)
        heading.pack_start(image, False, False, 0)
        label = Gtk.Label(label=title, xalign=0)
        label.get_style_context().add_class("section-heading")
        heading.pack_start(label, True, True, 0)
        box.pack_start(heading, False, False, 0)
        if vertical:
            child = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        else:
            child = Gtk.Grid(column_spacing=9, row_spacing=8)
        child.set_hexpand(True)
        box.pack_start(child, True, True, 0)
        return frame, child

    @staticmethod
    def _form_row(
        grid: Gtk.Grid,
        row: int,
        label: str,
        widget: Gtk.Widget,
        label_column: int = 0,
        widget_column: int = 1,
    ) -> None:
        lbl = Gtk.Label(label=label, xalign=0)
        lbl.get_style_context().add_class("field-label")
        grid.attach(lbl, label_column, row, 1, 1)
        widget.set_hexpand(True)
        grid.attach(widget, widget_column, row, 1, 1)

    def _route_node(self, icon: str, title: str, detail: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        box.set_hexpand(True)
        box.set_size_request(180, 112)
        box.get_style_context().add_class("tunnel-route-node")
        image = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.DIALOG)
        image.set_pixel_size(38)
        title_label = Gtk.Label(label=title)
        title_label.get_style_context().add_class("tunnel-flow-title")
        detail_label = Gtk.Label(label=detail)
        detail_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        detail_label.set_max_width_chars(24)
        detail_label.get_style_context().add_class("tunnel-flow-detail")
        box.pack_start(image, False, False, 0)
        box.pack_start(title_label, False, False, 0)
        box.pack_start(detail_label, False, False, 0)
        box._detail_label = detail_label
        box._title_label = title_label
        return box

    def on_choose_identity(self, _button: Gtk.Button) -> None:
        chooser = Gtk.FileChooserDialog(
            title="Scegli chiave privata SSH",
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        chooser.add_buttons("Annulla", Gtk.ResponseType.CANCEL, "Seleziona", Gtk.ResponseType.OK)
        current = os.path.expanduser(self.identity_file.get_text().strip())
        if current and Path(current).exists():
            chooser.set_filename(current)
        else:
            ssh_dir = Path.home() / ".ssh"
            chooser.set_current_folder(str(ssh_dir if ssh_dir.exists() else Path.home()))
        try:
            if chooser.run() == Gtk.ResponseType.OK:
                self.identity_file.set_text(chooser.get_filename() or "")
        finally:
            chooser.destroy()

    def set_type(self, tunnel_type: str) -> None:
        if tunnel_type == "remote":
            self.radio_remote.set_active(True)
        elif tunnel_type == "dynamic":
            self.radio_dynamic.set_active(True)
        else:
            self.radio_local.set_active(True)
        self.update_type_ui()

    def current_type(self) -> str:
        if self.radio_dynamic.get_active():
            return "dynamic"
        if self.radio_remote.get_active():
            return "remote"
        return "local"

    def on_type_changed(self, radio: Gtk.RadioButton) -> None:
        if radio.get_active():
            self.update_type_ui()

    def update_type_ui(self) -> None:
        tunnel_type = self.current_type()
        dynamic = tunnel_type == "dynamic"
        remote = tunnel_type == "remote"
        self.destination_stack.set_visible_child_name("socks" if dynamic else "destination")
        self.remote_host.set_sensitive(not dynamic)
        self.remote_port.set_sensitive(not dynamic)

        if dynamic:
            self.bind_label.set_text("Indirizzo proxy")
            self.port_label.set_text("Porta SOCKS")
            self.flow_left._title_label.set_text("Applicazioni locali")
            self.flow_ssh._title_label.set_text("Server SSH")
            self.flow_right._title_label.set_text("Rete remota")
            self.arrow1.set_text("→")
            self.arrow2.set_text("→")
            self.explain.set_text(
                "Le applicazioni locali si collegano al proxy SOCKS. Il server SSH "
                "apre poi la connessione verso la destinazione richiesta dal client."
            )
        elif remote:
            self.bind_label.set_text("Bind sul server SSH")
            self.port_label.set_text("Porta remota")
            self.flow_left._title_label.set_text("Servizio locale")
            self.flow_ssh._title_label.set_text("Server SSH")
            self.flow_right._title_label.set_text("Client remoti")
            self.arrow1.set_text("←")
            self.arrow2.set_text("←")
            self.explain.set_text(
                "I client remoti raggiungono la porta aperta sul server SSH; il traffico "
                "viene riportato al servizio raggiungibile da questo computer."
            )
        else:
            self.bind_label.set_text("Indirizzo locale")
            self.port_label.set_text("Porta locale")
            self.flow_left._title_label.set_text("Client locale")
            self.flow_ssh._title_label.set_text("Server SSH")
            self.flow_right._title_label.set_text("Servizio remoto")
            self.arrow1.set_text("→")
            self.arrow2.set_text("→")
            self.explain.set_text(
                "Il client locale si collega alla porta indicata e raggiunge il servizio "
                "di destinazione attraverso il server SSH."
            )
        self.update_preview()

    def get_tunnel(self) -> TunnelSession:
        return TunnelSession(
            id=self.tunnel.id,
            name=self.name.get_text().strip() or "Tunnel SSH",
            tunnel_type=self.current_type(),
            ssh_host=self.ssh_host.get_text().strip(),
            ssh_port=str(self.ssh_port.get_value_as_int()),
            ssh_user=self.ssh_user.get_text().strip(),
            bind_address=self.bind_address.get_text().strip() or "127.0.0.1",
            local_port=str(self.local_port.get_value_as_int()),
            remote_host=self.remote_host.get_text().strip() or "127.0.0.1",
            remote_port=str(self.remote_port.get_value_as_int()),
            options=self.options.get_text().strip(),
            credential_id=self.credential.get_active_id() or "",
            use_credential_password=self.use_password.get_active(),
            identity_file=self.identity_file.get_text().strip(),
            address_family=self.address_family.get_active_id() or "auto",
            exit_on_forward_failure=self.exit_on_failure.get_active(),
            compression=self.compression.get_active(),
            server_alive_interval=self.keepalive.get_value_as_int(),
            server_alive_count=self.keepalive_count.get_value_as_int(),
            auto_start=self.auto_start.get_active(),
            auto_restart=self.auto_restart.get_active(),
            restart_delay=self.restart_delay.get_value_as_int(),
            verbose_log=self.verbose_log.get_active(),
        )

    def update_preview(self, *_args) -> None:
        try:
            tunnel = self.get_tunnel()
            if tunnel.tunnel_type == "remote":
                self.flow_left._detail_label.set_text(tunnel.destination_endpoint())
                self.flow_ssh._detail_label.set_text(tunnel.ssh_endpoint())
                self.flow_right._detail_label.set_text(tunnel.listen_endpoint())
            elif tunnel.tunnel_type == "dynamic":
                self.flow_left._detail_label.set_text(tunnel.listen_endpoint())
                self.flow_ssh._detail_label.set_text(tunnel.ssh_endpoint())
                self.flow_right._detail_label.set_text("Destinazione dinamica")
            else:
                self.flow_left._detail_label.set_text(tunnel.listen_endpoint())
                self.flow_ssh._detail_label.set_text(tunnel.ssh_endpoint())
                self.flow_right._detail_label.set_text(tunnel.destination_endpoint())
            self.use_password.set_sensitive(bool(tunnel.credential_id))
            argv, _env = self.parent_app.build_tunnel_command(tunnel, allow_unlock=False)
            command = self.parent_app.mask_args(argv)
        except Exception as exc:
            command = f"Configurazione incompleta: {exc}"
        self.command_preview.get_buffer().set_text(command)

    def _show_validation(self, message: str, success: Optional[bool] = None) -> None:
        context = self.validation.get_style_context()
        context.remove_class("error-text")
        context.remove_class("success-text")
        if success is True:
            context.add_class("success-text")
        elif success is False:
            context.add_class("error-text")
        self.validation.set_text(message)
        self.validation.show()

    def on_test(self, _button: Gtk.Button) -> None:
        tunnel = self.get_tunnel()
        errors = self.parent_app.validate_tunnel(tunnel, check_local_bind=False)
        if errors:
            self._show_validation("Correggi prima: " + " • ".join(errors), False)
            return
        self.test_button.set_sensitive(False)
        self._show_validation("Verifica del server SSH in corso…", None)
        self.parent_app.test_tunnel_connection(tunnel, self.on_test_result)

    def on_test_result(self, success: bool, message: str, details: str) -> None:
        if self._destroyed:
            return
        self.test_button.set_sensitive(True)
        text = message if not details else f"{message}\n{details[-600:]}"
        self._show_validation(text, success)

    def on_save(self, _button: Gtk.Button) -> None:
        tunnel = self.get_tunnel()
        errors = self.parent_app.validate_tunnel(tunnel, check_local_bind=False)
        if errors:
            self._show_validation("Impossibile salvare: " + " • ".join(errors), False)
            return
        self.response(Gtk.ResponseType.OK)

class TunnelManagerDialog(Gtk.Dialog):
    """Gestore compatto dei tunnel SSH.

    Il layout evita Gtk.Paned e dimensioni implicite dipendenti dal tema: la
    tabella ha un'altezza minima stabile, il riepilogo resta compatto e il log
    si apre in un cassetto separato senza comprimere le righe.
    """

    COL_ID = 0
    COL_STATE = 1
    COL_COLOR = 2
    COL_NAME = 3
    COL_TYPE = 4
    COL_LISTEN = 5
    COL_DEST = 6
    COL_SSH = 7
    COL_AUTO = 8

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(title="Tunnel SSH", transient_for=parent, flags=0)
        self.set_modal(True)
        self.parent_app = parent
        self._destroyed = False
        self.set_default_size(1120, 700)
        self.set_size_request(820, 560)
        self.set_resizable(True)
        self.add_buttons("Chiudi", Gtk.ResponseType.CLOSE)
        self.connect("delete-event", lambda d, _e: (d.response(Gtk.ResponseType.CLOSE), True)[1])
        self.connect("destroy", self.on_destroy)
        self._last_runtime_snapshot = None

        content = self.get_content_area()
        content.set_hexpand(True)
        content.set_vexpand(True)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)
        root.set_hexpand(True)
        root.set_vexpand(True)
        root.get_style_context().add_class("tunnel-manager")
        content.pack_start(root, True, True, 0)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        icon = parent.make_icon_image(["network-vpn-symbolic", "network-vpn"], 28)
        header.pack_start(icon, False, False, 0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        heading = Gtk.Label(label="Gestore tunnel SSH", xalign=0)
        heading.get_style_context().add_class("dialog-heading")
        hint = Gtk.Label(label="Inoltri locali, remoti e proxy SOCKS in un'unica vista.", xalign=0)
        hint.get_style_context().add_class("muted")
        titles.pack_start(heading, False, False, 0)
        titles.pack_start(hint, False, False, 0)
        header.pack_start(titles, True, True, 0)
        self.count_label = Gtk.Label(label="0 tunnel")
        self.count_label.get_style_context().add_class("count-badge")
        header.pack_end(self.count_label, False, False, 0)
        root.pack_start(header, False, False, 0)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        toolbar.get_style_context().add_class("tunnel-toolbar")
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Cerca tunnel…")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", lambda *_: self.refresh())
        toolbar.pack_start(self.search, True, True, 0)
        for label, icon_name, callback in [
            ("Nuovo", "list-add-symbolic", self.on_add),
            ("Modifica", "document-edit-symbolic", self.on_edit),
            ("Duplica", "edit-copy-symbolic", self.on_duplicate),
            ("Elimina", "user-trash-symbolic", self.on_delete),
        ]:
            toolbar.pack_start(self._button(label, icon_name, callback), False, False, 0)
        root.pack_start(toolbar, False, False, 0)

        table_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        table_card.set_hexpand(True)
        table_card.set_vexpand(True)
        table_card.get_style_context().add_class("tunnel-table-card")
        self.list_store = Gtk.ListStore(str, str, str, str, str, str, str, str, str)
        self.tree = Gtk.TreeView(model=self.list_store)
        self.tree.set_headers_visible(True)
        self.tree.set_fixed_height_mode(False)
        self.tree.set_enable_search(True)
        self.tree.set_search_column(self.COL_NAME)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        self.tree.get_selection().connect("changed", self.on_selection_changed)
        self.tree.connect("row-activated", lambda *_: self.on_toggle_selected(None))
        self.tree.connect("button-press-event", self.on_tree_button_press)
        state_renderer = Gtk.CellRendererText()
        state_renderer.set_property("weight", Pango.Weight.BOLD)
        state_column = Gtk.TreeViewColumn("Stato", state_renderer, text=self.COL_STATE, foreground=self.COL_COLOR)
        state_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        state_column.set_fixed_width(120)
        self.tree.append_column(state_column)
        for title, col_id, expand, width in [
            ("Nome", self.COL_NAME, True, 190),
            ("Tipo", self.COL_TYPE, False, 82),
            ("Porta", self.COL_LISTEN, False, 145),
            ("Destinazione", self.COL_DEST, True, 210),
            ("Server SSH", self.COL_SSH, True, 210),
            ("Auto", self.COL_AUTO, False, 54),
        ]:
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            renderer.set_property("ypad", 5)
            column = Gtk.TreeViewColumn(title, renderer, text=col_id)
            column.set_expand(expand)
            column.set_min_width(width)
            column.set_resizable(True)
            self.tree.append_column(column)
        table_scroll = Gtk.ScrolledWindow()
        table_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        table_scroll.set_min_content_height(235)
        table_scroll.set_hexpand(True)
        table_scroll.set_vexpand(True)
        table_scroll.add(self.tree)
        table_card.pack_start(table_scroll, True, True, 0)
        root.pack_start(table_card, True, True, 0)

        # Barra di azione compatta, direttamente sotto la tabella.
        action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        action_bar.get_style_context().add_class("tunnel-action-bar")
        self.start_button = self._button("Avvia", "media-playback-start-symbolic", self.on_start)
        self.stop_button = self._button("Ferma", "media-playback-stop-symbolic", self.on_stop)
        self.test_selected_button = self._button("Verifica SSH", "network-transmit-receive-symbolic", self.on_test_selected)
        self.log_button = self._button("Mostra log", "text-x-generic-symbolic", self.on_toggle_log)
        self.copy_button = self._button("Copia comando", "edit-copy-symbolic", self.on_copy_command)
        self.endpoint_button = self._button("Copia endpoint", "edit-copy-symbolic", self.on_copy_endpoint)
        self.linked_session_button = self._button("Crea sessione", "network-workgroup-symbolic", self.on_create_linked_session)
        for button in (self.start_button, self.stop_button, self.test_selected_button, self.log_button, self.copy_button, self.endpoint_button, self.linked_session_button):
            action_bar.pack_start(button, False, False, 0)
        root.pack_start(action_bar, False, False, 0)

        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7, margin=10)
        detail.get_style_context().add_class("tunnel-detail-card")
        detail.set_size_request(-1, 122)
        self.detail_title = Gtk.Label(label="Seleziona un tunnel", xalign=0)
        self.detail_title.get_style_context().add_class("section-heading")
        self.detail_summary = Gtk.Label(label="", xalign=0)
        self.detail_summary.set_line_wrap(True)
        self.detail_summary.set_selectable(True)
        self.detail_status = Gtk.Label(label="", xalign=0)
        self.detail_status.set_line_wrap(True)
        self.detail_status.get_style_context().add_class("muted")
        detail.pack_start(self.detail_title, False, False, 0)
        detail.pack_start(self.detail_summary, False, False, 0)
        detail.pack_start(self.detail_status, False, False, 0)
        root.pack_start(detail, False, False, 0)

        self.log_revealer = Gtk.Revealer()
        self.log_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.log_revealer.set_transition_duration(180 if parent.settings.animations and not SAFE_MODE else 0)
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        log_box.get_style_context().add_class("tunnel-log-drawer")
        log_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        log_header.pack_start(Gtk.Label(label="Log del tunnel", xalign=0), True, True, 0)
        clear_log = self._button("Pulisci", "edit-clear-symbolic", self.on_clear_log)
        log_header.pack_end(clear_log, False, False, 0)
        log_box.pack_start(log_header, False, False, 0)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        log_scroll.set_min_content_height(150)
        log_scroll.add(self.log_view)
        log_box.pack_start(log_scroll, True, True, 0)
        self.log_revealer.add(log_box)
        root.pack_start(self.log_revealer, False, False, 0)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        footer.get_style_context().add_class("tunnel-footer")
        footer.pack_start(self._button("Avvia tutti", "system-run-symbolic", self.on_start_all), False, False, 0)
        footer.pack_start(self._button("Ferma tutti", "process-stop-symbolic", self.on_stop_all), False, False, 0)
        footer_note = Gtk.Label(label="I tunnel restano attivi anche chiudendo questa finestra.", xalign=1)
        footer_note.get_style_context().add_class("muted")
        footer.pack_end(footer_note, True, True, 0)
        root.pack_start(footer, False, False, 0)

        self.refresh()
        self._refresh_timer = GLib.timeout_add(900, self.on_refresh_timer)
        self.show_all()
        self.log_revealer.set_reveal_child(False)

    def _button(self, label: str, icon: str, callback) -> Gtk.Button:
        button = self.parent_app.make_button(label, icon, "secondary-action")
        button.connect("clicked", callback)
        return button

    def on_destroy(self, *_args) -> None:
        self._destroyed = True
        if getattr(self, "_refresh_timer", None):
            try:
                GLib.source_remove(self._refresh_timer)
            except Exception:
                pass
            self._refresh_timer = None

    def selected_id(self) -> str:
        model, tree_iter = self.tree.get_selection().get_selected()
        return model[tree_iter][self.COL_ID] if tree_iter is not None else ""

    def selected_tunnel(self) -> Optional[TunnelSession]:
        tunnel_id = self.selected_id()
        return self.parent_app.tunnel_store.get(tunnel_id) if tunnel_id else None

    def runtime_snapshot(self):
        return tuple((t.id, self.parent_app.get_tunnel_state(t.id), self.parent_app.tunnel_exit_codes.get(t.id)) for t in self.parent_app.tunnel_store.tunnels)

    def on_refresh_timer(self) -> bool:
        snapshot = self.runtime_snapshot()
        if snapshot != self._last_runtime_snapshot:
            self.refresh()
        elif self.log_revealer.get_reveal_child():
            self.update_log()
        return True

    def refresh(self, select_id: str = "") -> None:
        current = select_id or self.selected_id()
        query = self.search.get_text().strip().lower() if hasattr(self, "search") else ""
        self.list_store.clear()
        shown = 0
        for tunnel in self.parent_app.tunnel_store.tunnels:
            haystack = " ".join([tunnel.name, tunnel.type_label(), tunnel.listen_endpoint(), tunnel.destination_endpoint(), tunnel.ssh_endpoint()]).lower()
            if query and query not in haystack:
                continue
            state_text, state_color = self._state_display(self.parent_app.get_tunnel_state(tunnel.id))
            self.list_store.append([tunnel.id, state_text, state_color, tunnel.name, tunnel.type_label(), tunnel.listen_endpoint(), tunnel.destination_endpoint(), tunnel.ssh_endpoint(), "Sì" if tunnel.auto_start else "—"])
            shown += 1
        self.count_label.set_text(f"{shown} tunnel" if shown != 1 else "1 tunnel")
        self._last_runtime_snapshot = self.runtime_snapshot()
        if not current and shown:
            first = self.list_store.get_iter_first()
            if first is not None:
                self.tree.get_selection().select_iter(first)
                current = self.list_store[first][self.COL_ID]
        if current:
            tree_iter = self.list_store.get_iter_first()
            while tree_iter is not None:
                if self.list_store[tree_iter][self.COL_ID] == current:
                    path = self.list_store.get_path(tree_iter)
                    self.tree.get_selection().select_path(path)
                    self.tree.scroll_to_cell(path, None, False, 0, 0)
                    break
                tree_iter = self.list_store.iter_next(tree_iter)
        self.update_details()

    @staticmethod
    def _state_display(state: str) -> Tuple[str, str]:
        return {
            "running": ("● Attivo", "#49d17d"),
            "starting": ("● Avvio", "#f3c969"),
            "restarting": ("● Riconnessione", "#f3c969"),
            "error": ("● Errore", "#ff7185"),
            "locked": ("● Password bloccate", "#f3c969"),
            "stopped": ("○ Fermo", "#9aa4b2"),
        }.get(state, (f"○ {state}", "#9aa4b2"))

    def on_selection_changed(self, *_args) -> None:
        self.update_details()
        self.update_log()

    def update_details(self) -> None:
        tunnel = self.selected_tunnel()
        selected = tunnel is not None
        for button in (self.start_button, self.stop_button, self.test_selected_button, self.log_button, self.copy_button, self.endpoint_button, self.linked_session_button):
            button.set_sensitive(selected)
        if tunnel is None:
            self.detail_title.set_text("Seleziona un tunnel")
            self.detail_summary.set_text("Scegli una riga per visualizzare percorso, stato e azioni disponibili.")
            self.detail_status.set_text("")
            return
        state = self.parent_app.get_tunnel_state(tunnel.id)
        proc = self.parent_app.tunnel_processes.get(tunnel.id)
        pid = proc.pid if proc is not None and proc.poll() is None else "—"
        exit_code = self.parent_app.tunnel_exit_codes.get(tunnel.id)
        self.detail_title.set_text(tunnel.name)
        self.detail_summary.set_text(tunnel.summary())
        details = [f"Stato: {self._state_display(state)[0].replace('● ', '').replace('○ ', '')}", f"PID: {pid}"]
        if exit_code is not None:
            details.append(f"Ultima uscita: {exit_code}")
        details.append(f"Keepalive: {tunnel.server_alive_interval}s × {tunnel.server_alive_count}")
        if tunnel.auto_restart:
            details.append(f"Riconnessione: {tunnel.restart_delay}s")
        self.detail_status.set_text("  •  ".join(details))
        self.start_button.set_sensitive(state not in {"running", "starting"})
        self.stop_button.set_sensitive(state in {"running", "starting", "restarting"})
        self.linked_session_button.set_sensitive(tunnel.tunnel_type == "local")

    def update_log(self) -> None:
        tunnel = self.selected_tunnel()
        if tunnel is None:
            self.log_view.get_buffer().set_text("Seleziona un tunnel per visualizzarne il log.")
            return
        self.log_view.get_buffer().set_text(self.parent_app.tunnel_log_text(tunnel))
        end = self.log_view.get_buffer().get_end_iter()
        self.log_view.scroll_to_iter(end, 0.0, False, 0.0, 1.0)

    def on_tree_button_press(self, tree: Gtk.TreeView, event) -> bool:
        if event.type != Gdk.EventType.BUTTON_PRESS or event.button != 3:
            return False
        hit = tree.get_path_at_pos(int(event.x), int(event.y))
        if hit:
            tree.get_selection().select_path(hit[0])
        menu = Gtk.Menu()
        for label, callback in [
            ("Avvia / Ferma", self.on_toggle_selected),
            ("Modifica", self.on_edit),
            ("Duplica", self.on_duplicate),
            ("Verifica SSH", self.on_test_selected),
            ("Copia endpoint", self.on_copy_endpoint),
            ("Copia comando", self.on_copy_command),
            ("Crea sessione collegata", self.on_create_linked_session),
        ]:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _i, cb=callback: cb(None))
            menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        delete_item = Gtk.MenuItem(label="Elimina")
        delete_item.connect("activate", lambda *_: self.on_delete(None))
        menu.append(delete_item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def on_toggle_selected(self, _button: Optional[Gtk.Button]) -> None:
        tunnel = self.selected_tunnel()
        if not tunnel:
            return
        if self.parent_app.get_tunnel_state(tunnel.id) in {"running", "starting", "restarting"}:
            self.on_stop(None)
        else:
            self.on_start(None)

    def on_add(self, _button: Optional[Gtk.Button]) -> None:
        dialog = TunnelDialog(self, credentials=self.parent_app.credential_options_for_dialog())
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                tunnel = dialog.get_tunnel()
                self.parent_app.tunnel_store.upsert(tunnel)
                self.parent_app.set_status(f"Tunnel salvato: {tunnel.name}")
                self.refresh(tunnel.id)
        finally:
            dialog.destroy()

    def on_edit(self, _button: Optional[Gtk.Button]) -> None:
        tunnel = self.selected_tunnel()
        if tunnel is None:
            return
        dialog = TunnelDialog(self, tunnel, credentials=self.parent_app.credential_options_for_dialog())
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                updated = dialog.get_tunnel()
                was_running = self.parent_app.get_tunnel_state(tunnel.id) in {"running", "starting"}
                if was_running:
                    self.parent_app.stop_tunnel(tunnel.id, quiet=True)
                self.parent_app.tunnel_store.upsert(updated)
                if was_running:
                    self.parent_app.start_tunnel(updated)
                self.refresh(updated.id)
        finally:
            dialog.destroy()

    def on_duplicate(self, _button: Optional[Gtk.Button]) -> None:
        tunnel = self.selected_tunnel()
        if tunnel is None:
            return
        clone = TunnelSession.from_dict(tunnel.to_dict())
        clone.id = uuid.uuid4().hex
        clone.name = f"{tunnel.name} copia"
        clone.auto_start = False
        self.parent_app.tunnel_store.upsert(clone)
        self.refresh(clone.id)

    def on_delete(self, _button: Optional[Gtk.Button]) -> None:
        tunnel = self.selected_tunnel()
        if tunnel is None:
            return
        linked: List[Session] = []

        def collect(nodes: List[Node]) -> None:
            for node in nodes:
                if isinstance(node, FolderNode):
                    collect(node.children)
                elif node.required_tunnel_id == tunnel.id:
                    linked.append(node)

        collect(self.parent_app.store.nodes)
        dialog = Gtk.MessageDialog(transient_for=self, flags=0, message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.NONE, text=f"Eliminare il tunnel “{tunnel.name}”?")
        dialog.add_buttons("Annulla", Gtk.ResponseType.CANCEL, "Elimina", Gtk.ResponseType.OK)
        extra = ""
        if linked:
            extra = f"\n\nIl collegamento automatico verrà rimosso da {len(linked)} sessioni."
        dialog.format_secondary_text("Il tunnel attivo verrà fermato e la configurazione rimossa." + extra)
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        self.parent_app.stop_tunnel(tunnel.id, quiet=True)
        self.parent_app.tunnel_store.remove(tunnel.id)
        for session in linked:
            session.required_tunnel_id = ""
        if linked:
            self.parent_app.store.save()
            self.parent_app.refresh_sessions()
        self.refresh()

    def on_start(self, _button: Optional[Gtk.Button]) -> None:
        tunnel = self.selected_tunnel()
        if tunnel:
            self.parent_app.start_tunnel(tunnel)
            self.refresh(tunnel.id)

    def on_stop(self, _button: Optional[Gtk.Button]) -> None:
        tunnel = self.selected_tunnel()
        if tunnel:
            self.parent_app.stop_tunnel(tunnel.id)
            self.refresh(tunnel.id)

    def on_start_all(self, _button: Optional[Gtk.Button]) -> None:
        for tunnel in self.parent_app.tunnel_store.tunnels:
            self.parent_app.start_tunnel(tunnel)
        self.refresh()

    def on_stop_all(self, _button: Optional[Gtk.Button]) -> None:
        for tunnel_id in list(self.parent_app.tunnel_processes.keys()):
            self.parent_app.stop_tunnel(tunnel_id, quiet=True)
        self.parent_app.set_status("Tutti i tunnel sono stati fermati.")
        self.refresh()

    def on_test_selected(self, _button: Optional[Gtk.Button]) -> None:
        tunnel = self.selected_tunnel()
        if tunnel is None:
            return
        self.test_selected_button.set_sensitive(False)
        self.parent_app.test_tunnel_connection(tunnel, self.on_test_result)

    def on_test_result(self, success: bool, message: str, details: str) -> None:
        if self._destroyed:
            return
        self.test_selected_button.set_sensitive(True)
        self.parent_app.show_notification(message, "success" if success else "error", 6000)
        if details:
            tunnel = self.selected_tunnel()
            if tunnel:
                self.parent_app.append_tunnel_log(tunnel.id, details)
                self.update_log()

    def on_toggle_log(self, _button: Optional[Gtk.Button]) -> None:
        reveal = not self.log_revealer.get_reveal_child()
        self.log_revealer.set_reveal_child(reveal)
        try:
            box = self.log_button.get_child()
            if isinstance(box, Gtk.Box):
                labels = [child for child in box.get_children() if isinstance(child, Gtk.Label)]
                if labels:
                    labels[-1].set_text("Nascondi log" if reveal else "Mostra log")
        except Exception:
            pass
        if reveal:
            self.update_log()

    def on_clear_log(self, _button: Optional[Gtk.Button]) -> None:
        tunnel = self.selected_tunnel()
        if tunnel:
            self.parent_app.tunnel_logs[tunnel.id] = []
            self.update_log()

    def on_copy_command(self, _button: Optional[Gtk.Button]) -> None:
        tunnel = self.selected_tunnel()
        if tunnel is None:
            return
        try:
            argv, _env = self.parent_app.build_tunnel_command(tunnel, allow_unlock=False)
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text(self.parent_app.mask_args(argv), -1)
            clipboard.store()
            self.parent_app.set_status("Comando tunnel copiato negli appunti.")
        except Exception as exc:
            self.parent_app.show_notification(f"Impossibile copiare il comando: {exc}", "error", 5000)

    def on_copy_endpoint(self, _button: Optional[Gtk.Button]) -> None:
        tunnel = self.selected_tunnel()
        if tunnel is None:
            return
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(tunnel.listen_endpoint(), -1)
        clipboard.store()
        self.parent_app.set_status("Endpoint locale copiato negli appunti.")

    def on_create_linked_session(self, _button: Optional[Gtk.Button]) -> None:
        tunnel = self.selected_tunnel()
        if tunnel is None or tunnel.tunnel_type != "local":
            self.parent_app.show_notification("La creazione guidata è disponibile per i tunnel locali (-L).", "info", 4000)
            return
        port = tunnel.local_port or ""
        if tunnel.remote_port == "3389":
            proto = "rdp"
        elif tunnel.remote_port in {"5900", "5901", "5902"}:
            proto = "vnc"
        elif tunnel.remote_port == "22":
            proto = "ssh"
        else:
            proto = "ssh"
        draft = Session(
            name=f"{tunnel.name} - sessione",
            protocol=proto,
            host=tunnel.bind_address or "127.0.0.1",
            port=port,
            required_tunnel_id=tunnel.id,
            wait_for_tunnel=True,
            notes=f"Sessione collegata automaticamente al tunnel {tunnel.name}.",
        )
        dialog = SessionDialog(self, draft, credentials=self.parent_app.credential_options_for_dialog(), tunnels=self.parent_app.tunnel_options_for_dialog())
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                session = dialog.get_session()
                self.parent_app.store.add_node("", session)
                self.parent_app.refresh_sessions()
                self.parent_app.show_notification(f"Sessione creata: {session.name}", "success", 3500)
        finally:
            dialog.destroy()


class SettingsDialog(Gtk.Dialog):
    """Centro impostazioni organizzato per categorie.

    Le preferenze vengono applicate senza riavviare quando possibile. Le
    impostazioni che riguardano nuove connessioni o nuovi terminali diventano
    effettive alla successiva apertura.
    """

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(title="Impostazioni", transient_for=parent, flags=0)
        # Non modale: una finestra opzioni modale blocca con un grab tutti gli
        # input verso la finestra principale, "congelando" la sessione RDP o il
        # terminale attivo finché non viene chiusa.
        self.set_modal(False)
        self.parent_app = parent
        cancel_button = self.add_button("Annulla", Gtk.ResponseType.CANCEL)
        apply_button = self.add_button("Applica", Gtk.ResponseType.OK)
        cancel_button.get_style_context().add_class("secondary-action")
        apply_button.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(940, 700)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.get_style_context().add_class("modern-dialog")

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=14)
        outer.set_hexpand(True)
        outer.set_vexpand(True)
        self.get_content_area().set_hexpand(True)
        self.get_content_area().set_vexpand(True)
        self.get_content_area().add(outer)
        heading = Gtk.Label(label="Preferenze di FedTermHub", xalign=0)
        heading.get_style_context().add_class("dialog-heading")
        outer.pack_start(heading, False, False, 0)

        # Gtk.Notebook mostrava pagine con altezza quasi nulla su alcune
        # combinazioni Fedora/KDE/XWayland. StackSidebar assegna invece in modo
        # deterministico tutta l'area disponibile alla pagina selezionata.
        settings_body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        settings_body.set_hexpand(True)
        settings_body.set_vexpand(True)
        settings_body.get_style_context().add_class("settings-shell")
        outer.pack_start(settings_body, True, True, 0)

        self.settings_stack = Gtk.Stack()
        self.settings_stack.set_hexpand(True)
        self.settings_stack.set_vexpand(True)
        self.settings_stack.set_hhomogeneous(False)
        self.settings_stack.set_vhomogeneous(False)
        self.settings_stack.set_transition_type(
            Gtk.StackTransitionType.SLIDE_LEFT_RIGHT if parent.settings.animations and not SAFE_MODE
            else Gtk.StackTransitionType.NONE
        )
        self.settings_stack.set_transition_duration(160 if parent.settings.animations and not SAFE_MODE else 0)
        self.settings_stack.get_style_context().add_class("settings-stack")

        self.settings_sidebar = Gtk.StackSidebar()
        self.settings_sidebar.set_stack(self.settings_stack)
        self.settings_sidebar.set_size_request(178, -1)
        self.settings_sidebar.set_vexpand(True)
        self.settings_sidebar.get_style_context().add_class("settings-category-sidebar")
        settings_body.pack_start(self.settings_sidebar, False, False, 0)
        settings_body.pack_start(self.settings_stack, True, True, 0)

        self._build_general_page()
        self._build_interface_page()
        self._build_terminal_page()
        self._build_connections_page()
        self._build_security_page()
        self._build_data_page()
        self.settings_stack.set_visible_child_name("general")
        self.load_values(parent.settings)
        self.show_all()

    @staticmethod
    def _page() -> Tuple[Gtk.ScrolledWindow, Gtk.Box]:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.NONE)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        scroll.get_style_context().add_class("settings-page-scroll")

        viewport = Gtk.Viewport()
        viewport.set_shadow_type(Gtk.ShadowType.NONE)
        viewport.set_hexpand(True)
        viewport.set_vexpand(True)
        viewport.get_style_context().add_class("settings-page-viewport")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=16)
        box.set_hexpand(True)
        box.set_vexpand(False)
        box.get_style_context().add_class("settings-page")
        viewport.add(box)
        scroll.add(viewport)
        return scroll, box

    def _append_page(self, widget: Gtk.Widget, name: str, title: str) -> None:
        widget.set_hexpand(True)
        widget.set_vexpand(True)
        self.settings_stack.add_titled(widget, name, title)

    @staticmethod
    def _card(title: str) -> Tuple[Gtk.Frame, Gtk.Grid]:
        frame = Gtk.Frame(label=title)
        frame.set_hexpand(True)
        frame.get_style_context().add_class("settings-section")
        grid = Gtk.Grid(column_spacing=18, row_spacing=11, margin=14)
        grid.set_hexpand(True)
        grid.set_column_homogeneous(False)
        frame.add(grid)
        return frame, grid

    @staticmethod
    def _row(grid: Gtk.Grid, row: int, label: str, widget: Gtk.Widget, hint: str = "") -> None:
        lbl = Gtk.Label(label=label, xalign=0)
        lbl.get_style_context().add_class("field-label")
        lbl.set_valign(Gtk.Align.CENTER)
        grid.attach(lbl, 0, row, 1, 1)
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        holder.set_hexpand(True)
        holder.pack_start(widget, False, False, 0)
        if hint:
            note = Gtk.Label(label=hint, xalign=0)
            note.set_line_wrap(True)
            note.get_style_context().add_class("muted")
            holder.pack_start(note, False, False, 0)
        grid.attach(holder, 1, row, 1, 1)

    def _build_general_page(self) -> None:
        scroll, page = self._page()
        frame, grid = self._card("Avvio e comportamento")
        page.pack_start(frame, False, False, 0)

        self.startup_mode = Gtk.ComboBoxText()
        self.startup_mode.append("terminal", "Apri un terminale locale")
        self.startup_mode.append("empty", "Apri l'area di lavoro vuota")
        self.confirm_close = Gtk.CheckButton(label="Chiedi conferma prima di chiudere sessioni attive")
        self.error_popups = Gtk.CheckButton(label="Mostra una finestra per gli errori di connessione")
        self.open_error_tabs = Gtk.CheckButton(label="Apri anche una scheda con i dettagli tecnici")
        self.notification_duration = Gtk.SpinButton.new_with_range(1, 15, 1)
        self.notification_duration.set_tooltip_text("Durata dei messaggi non persistenti")

        self._row(grid, 0, "All'avvio", self.startup_mode)
        self._row(grid, 1, "Chiusura", self.confirm_close)
        self._row(grid, 2, "Errori", self.error_popups)
        self._row(grid, 3, "Diagnostica", self.open_error_tabs)
        self._row(grid, 4, "Notifiche (secondi)", self.notification_duration)

        info = Gtk.Label(
            label="Le modifiche grafiche vengono applicate subito. Le impostazioni di connessione valgono per le nuove sessioni aperte.",
            xalign=0,
        )
        info.set_line_wrap(True)
        info.get_style_context().add_class("info-card")
        page.pack_start(info, False, False, 0)
        self._append_page(scroll, "general", "Generale")

    def _build_interface_page(self) -> None:
        scroll, page = self._page()
        frame, grid = self._card("Aspetto")
        page.pack_start(frame, False, False, 0)

        self.theme = Gtk.ComboBoxText()
        self.theme.append("dark", "Scuro ad alto contrasto")
        self.theme.append("light", "Chiaro")
        self.icon_theme = Gtk.ComboBoxText()
        for value, label in [
            ("auto", "Automatico"), ("Papirus-Dark", "Papirus Dark"),
            ("Papirus", "Papirus"), ("Papirus-Light", "Papirus Light"),
            ("Adwaita", "Adwaita"), ("hicolor", "hicolor fallback"),
        ]:
            self.icon_theme.append(value, label)
        self.animations = Gtk.CheckButton(label="Abilita animazioni e transizioni")
        self.toolbar_icon_size = Gtk.SpinButton.new_with_range(18, 32, 1)
        self.show_toolbar_labels = Gtk.CheckButton(label="Mostra le etichette sotto le icone")
        self.compact_tabs = Gtk.CheckButton(label="Usa schede compatte")

        self._row(grid, 0, "Tema", self.theme)
        self._row(grid, 1, "Set di icone", self.icon_theme)
        self._row(grid, 2, "Animazioni", self.animations)
        self._row(grid, 3, "Dimensione icone", self.toolbar_icon_size)
        self._row(grid, 4, "Etichette", self.show_toolbar_labels)
        self._row(grid, 5, "Schede", self.compact_tabs)

        frame2, grid2 = self._card("Elementi dell'interfaccia")
        page.pack_start(frame2, False, False, 0)
        self.show_menubar = Gtk.CheckButton(label="Barra dei menu")
        self.show_ribbon = Gtk.CheckButton(label="Barra comandi superiore")
        self.show_left_rail = Gtk.CheckButton(label="Barra rapida verticale")
        self.show_sidebar = Gtk.CheckButton(label="Pannello sessioni")
        self.show_statusbar = Gtk.CheckButton(label="Barra di stato inferiore")
        self.show_workspace_hint = Gtk.CheckButton(label="Suggerimento sopra le schede")
        self.sidebar_width = Gtk.SpinButton.new_with_range(190, 520, 5)
        self._row(grid2, 0, "Menu", self.show_menubar)
        self._row(grid2, 1, "Comandi", self.show_ribbon)
        self._row(grid2, 2, "Scorciatoie", self.show_left_rail)
        self._row(grid2, 3, "Sessioni", self.show_sidebar)
        self._row(grid2, 4, "Stato", self.show_statusbar)
        self._row(grid2, 5, "Suggerimenti", self.show_workspace_hint)
        self._row(grid2, 6, "Larghezza pannello", self.sidebar_width, "Valore in pixel; il pannello resta ridimensionabile con il mouse.")
        self._append_page(scroll, "interface", "Interfaccia")

    def _build_terminal_page(self) -> None:
        scroll, page = self._page()
        frame, grid = self._card("Aspetto del terminale")
        page.pack_start(frame, False, False, 0)

        self.terminal_font = Gtk.FontButton()
        self.terminal_font.set_use_font(True)
        self.terminal_font.set_use_size(True)
        self.terminal_scrollback = Gtk.SpinButton.new_with_range(1000, 500000, 1000)
        self.terminal_cursor_shape = Gtk.ComboBoxText()
        self.terminal_cursor_shape.append("block", "Blocco")
        self.terminal_cursor_shape.append("ibeam", "Barra verticale")
        self.terminal_cursor_shape.append("underline", "Sottolineatura")
        self.terminal_cursor_blink = Gtk.CheckButton(label="Cursore lampeggiante")
        self.terminal_copy_on_select = Gtk.CheckButton(label="Copia automaticamente il testo selezionato")
        self.terminal_scroll_on_output = Gtk.CheckButton(label="Scorri automaticamente quando arriva nuovo output")
        self.terminal_audible_bell = Gtk.CheckButton(label="Abilita segnale acustico del terminale")

        self._row(grid, 0, "Carattere", self.terminal_font)
        self._row(grid, 1, "Righe cronologia", self.terminal_scrollback)
        self._row(grid, 2, "Forma cursore", self.terminal_cursor_shape)
        self._row(grid, 3, "Lampeggio", self.terminal_cursor_blink)
        self._row(grid, 4, "Selezione", self.terminal_copy_on_select)
        self._row(grid, 5, "Scorrimento", self.terminal_scroll_on_output)
        self._row(grid, 6, "Campanello", self.terminal_audible_bell)

        frame2, grid2 = self._card("Terminale locale")
        page.pack_start(frame2, False, False, 0)
        self.terminal_shell = Gtk.Entry()
        self.terminal_shell.set_placeholder_text("Vuoto = shell predefinita dell'utente")
        self.terminal_start_directory = Gtk.Entry()
        self.terminal_start_directory.set_placeholder_text("Vuoto = cartella Home")
        browse = Gtk.Button.new_with_mnemonic("Scegli _cartella…")
        browse.connect("clicked", self.on_choose_terminal_directory)
        dir_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dir_box.pack_start(self.terminal_start_directory, True, True, 0)
        dir_box.pack_start(browse, False, False, 0)
        self._row(grid2, 0, "Shell", self.terminal_shell, "Esempi: /bin/bash, /usr/bin/fish, /bin/zsh")
        self._row(grid2, 1, "Cartella iniziale", dir_box)
        self._append_page(scroll, "terminal", "Terminale")

    def _build_connections_page(self) -> None:
        scroll, page = self._page()
        frame, grid = self._card("Connessioni")
        page.pack_start(frame, False, False, 0)
        self.rdp_preflight = Gtk.CheckButton(label="Verifica host e porta prima di avviare RDP")
        self.connection_timeout = Gtk.SpinButton.new_with_range(2, 120, 1)
        self.ssh_keepalive_interval = Gtk.SpinButton.new_with_range(0, 600, 5)
        self.ssh_keepalive_count = Gtk.SpinButton.new_with_range(1, 20, 1)
        self.ssh_compression = Gtk.CheckButton(label="Abilita compressione SSH")
        self.ssh_strict_host_key = Gtk.ComboBoxText()
        self.ssh_strict_host_key.append("ask", "Chiedi conferma")
        self.ssh_strict_host_key.append("accept-new", "Accetta automaticamente solo host nuovi")
        self.ssh_strict_host_key.append("yes", "Accetta solo chiavi già note")
        self.ssh_strict_host_key.append("no", "Non verificare la chiave (sconsigliato)")
        self._row(grid, 0, "Controllo RDP", self.rdp_preflight)
        self._row(grid, 1, "Timeout connessione (s)", self.connection_timeout)
        self._row(grid, 2, "Keepalive SSH (s)", self.ssh_keepalive_interval, "0 disabilita i pacchetti keepalive.")
        self._row(grid, 3, "Tentativi keepalive", self.ssh_keepalive_count)
        self._row(grid, 4, "Compressione", self.ssh_compression)
        self._row(grid, 5, "Chiavi host SSH", self.ssh_strict_host_key)
        self._append_page(scroll, "connections", "Connessioni")

    def _build_security_page(self) -> None:
        scroll, page = self._page()
        frame, grid = self._card("Password e appunti")
        page.pack_start(frame, False, False, 0)
        self.vault_auto_lock = Gtk.SpinButton.new_with_range(5, 240, 5)
        self.vault_keep_unlocked = Gtk.CheckButton(
            label="Non richiedere la password finché l'app resta aperta"
        )
        self.vault_keep_unlocked.set_tooltip_text(
            "Con la spunta attiva il Password Manager resta sbloccato per tutta la sessione di lavoro: "
            "la password viene richiesta di nuovo solo alla prossima apertura del programma "
            "(o dopo un blocco manuale)."
        )
        self.vault_keep_unlocked.connect(
            "toggled", lambda cb: self.vault_auto_lock.set_sensitive(not cb.get_active())
        )
        self.lock_vault_on_minimize = Gtk.CheckButton(label="Blocca il vault quando la finestra viene minimizzata")
        self.clear_clipboard = Gtk.SpinButton.new_with_range(0, 600, 5)
        self._row(grid, 0, "Sblocco persistente", self.vault_keep_unlocked)
        self._row(grid, 1, "Blocco automatico (min)", self.vault_auto_lock, "Richiede di nuovo la password dopo questo periodo di inattività.")
        self._row(grid, 2, "Minimizzazione", self.lock_vault_on_minimize)
        self._row(grid, 3, "Cancella password dagli appunti (s)", self.clear_clipboard, "0 non modifica automaticamente gli appunti.")

        warning = Gtk.Label(
            label="Le password restano cifrate nel Password Manager. Quando copi una password, FedTermHub può rimuoverla automaticamente dagli appunti dopo l'intervallo scelto.",
            xalign=0,
        )
        warning.set_line_wrap(True)
        warning.get_style_context().add_class("info-card")
        page.pack_start(warning, False, False, 0)
        self._append_page(scroll, "security", "Sicurezza")

    def _build_data_page(self) -> None:
        scroll, page = self._page()
        frame, grid = self._card("Configurazione e manutenzione")
        page.pack_start(frame, False, False, 0)
        buttons = [
            ("Apri cartella configurazione", self.on_open_config_folder),
            ("Esporta impostazioni", self.on_export_settings),
            ("Importa impostazioni", self.on_import_settings),
            ("Apri log applicazione", self.on_open_log),
            ("Ripristina valori predefiniti", self.on_reset_defaults),
        ]
        for row, (label, callback) in enumerate(buttons):
            button = Gtk.Button.new_with_label(label)
            button.set_halign(Gtk.Align.START)
            button.connect("clicked", callback)
            grid.attach(button, 0, row, 2, 1)
        note = Gtk.Label(label=f"Configurazione: {CONFIG_DIR}\nLog: {LOG_FILE}", xalign=0)
        note.set_selectable(True)
        note.set_line_wrap(True)
        note.get_style_context().add_class("muted")
        page.pack_start(note, False, False, 0)
        self._append_page(scroll, "data", "Dati")

    def load_values(self, settings: AppSettings) -> None:
        self.theme.set_active_id(settings.theme)
        self.icon_theme.set_active_id(settings.icon_theme)
        if self.icon_theme.get_active() < 0:
            self.icon_theme.set_active_id("auto")
        self.animations.set_active(settings.animations and not SAFE_MODE)
        self.animations.set_sensitive(not SAFE_MODE)
        self.toolbar_icon_size.set_value(settings.toolbar_icon_size)
        self.show_toolbar_labels.set_active(settings.show_toolbar_labels)
        self.compact_tabs.set_active(settings.compact_tabs)
        self.show_menubar.set_active(settings.show_menubar)
        self.show_ribbon.set_active(settings.show_ribbon)
        self.show_left_rail.set_active(settings.show_left_rail)
        self.show_sidebar.set_active(settings.show_sidebar)
        self.show_statusbar.set_active(settings.show_statusbar)
        self.show_workspace_hint.set_active(settings.show_workspace_hint)
        self.sidebar_width.set_value(settings.sidebar_width)
        self.startup_mode.set_active_id(settings.startup_mode)
        self.confirm_close.set_active(settings.confirm_close_active_sessions)
        self.error_popups.set_active(settings.show_error_popups)
        self.open_error_tabs.set_active(settings.open_error_tabs)
        self.notification_duration.set_value(max(1, settings.notification_duration_ms // 1000))
        self.terminal_font.set_font_name(f"{settings.terminal_font_family} {settings.terminal_font_size}")
        self.terminal_scrollback.set_value(settings.terminal_scrollback_lines)
        self.terminal_cursor_shape.set_active_id(settings.terminal_cursor_shape)
        self.terminal_cursor_blink.set_active(settings.terminal_cursor_blink)
        self.terminal_copy_on_select.set_active(settings.terminal_copy_on_select)
        self.terminal_scroll_on_output.set_active(settings.terminal_scroll_on_output)
        self.terminal_audible_bell.set_active(settings.terminal_audible_bell)
        self.terminal_shell.set_text(settings.terminal_shell)
        self.terminal_start_directory.set_text(settings.terminal_start_directory)
        self.rdp_preflight.set_active(settings.rdp_preflight)
        self.connection_timeout.set_value(settings.connection_timeout_seconds)
        self.ssh_keepalive_interval.set_value(settings.ssh_keepalive_interval)
        self.ssh_keepalive_count.set_value(settings.ssh_keepalive_count)
        self.ssh_compression.set_active(settings.ssh_compression)
        self.ssh_strict_host_key.set_active_id(settings.ssh_strict_host_key)
        keep_unlocked = int(settings.vault_auto_lock_minutes) == 0
        self.vault_keep_unlocked.set_active(keep_unlocked)
        self.vault_auto_lock.set_value(settings.vault_auto_lock_minutes if not keep_unlocked else 15)
        self.vault_auto_lock.set_sensitive(not keep_unlocked)
        self.lock_vault_on_minimize.set_active(settings.lock_vault_on_minimize)
        self.clear_clipboard.set_value(settings.clear_clipboard_seconds)

    def on_choose_terminal_directory(self, _button: Gtk.Button) -> None:
        chooser = Gtk.FileChooserDialog(
            title="Cartella iniziale del terminale",
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.add_buttons("_Annulla", Gtk.ResponseType.CANCEL, "Seleziona", Gtk.ResponseType.OK)
        current = self.terminal_start_directory.get_text().strip()
        if current and Path(current).is_dir():
            chooser.set_filename(current)
        if chooser.run() == Gtk.ResponseType.OK:
            selected = chooser.get_filename() or ""
            self.terminal_start_directory.set_text(selected)
        chooser.destroy()

    def on_open_config_folder(self, _button: Gtk.Button) -> None:
        ensure_private_config_dir()
        try:
            Gio.AppInfo.launch_default_for_uri(CONFIG_DIR.as_uri(), None)
        except Exception as exc:
            self.parent_app.set_status(f"Impossibile aprire la cartella: {exc}")

    def on_export_settings(self, _button: Gtk.Button) -> None:
        self.parent_app.export_settings(self)

    def on_import_settings(self, _button: Gtk.Button) -> None:
        if self.parent_app.import_settings(self):
            self.load_values(self.parent_app.settings)

    def on_open_log(self, _button: Gtk.Button) -> None:
        self.parent_app.open_application_log()

    def on_reset_defaults(self, _button: Gtk.Button) -> None:
        confirm = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Ripristinare le impostazioni predefinite?",
        )
        confirm.format_secondary_text("Sessioni, tunnel e password salvate non verranno eliminate.")
        response = confirm.run()
        confirm.destroy()
        if response == Gtk.ResponseType.YES:
            self.load_values(AppSettings())

    def apply(self) -> None:
        settings = self.parent_app.settings
        settings.theme = self.theme.get_active_id() or "dark"
        settings.icon_theme = self.icon_theme.get_active_id() or "auto"
        settings.animations = bool(self.animations.get_active())
        settings.toolbar_icon_size = int(self.toolbar_icon_size.get_value())
        settings.show_toolbar_labels = bool(self.show_toolbar_labels.get_active())
        settings.compact_tabs = bool(self.compact_tabs.get_active())
        settings.show_menubar = bool(self.show_menubar.get_active())
        settings.show_ribbon = bool(self.show_ribbon.get_active())
        settings.show_left_rail = bool(self.show_left_rail.get_active())
        settings.show_sidebar = bool(self.show_sidebar.get_active())
        settings.show_statusbar = bool(self.show_statusbar.get_active())
        settings.show_workspace_hint = bool(self.show_workspace_hint.get_active())
        settings.sidebar_width = int(self.sidebar_width.get_value())
        settings.startup_mode = self.startup_mode.get_active_id() or "terminal"
        settings.confirm_close_active_sessions = bool(self.confirm_close.get_active())
        settings.show_error_popups = bool(self.error_popups.get_active())
        settings.open_error_tabs = bool(self.open_error_tabs.get_active())
        settings.notification_duration_ms = int(self.notification_duration.get_value()) * 1000

        desc = Pango.FontDescription(self.terminal_font.get_font_name())
        settings.terminal_font_family = desc.get_family() or "Monospace"
        size = int(round(desc.get_size() / Pango.SCALE)) if desc.get_size() > 0 else 10
        settings.terminal_font_size = max(7, min(size, 28))
        settings.terminal_scrollback_lines = int(self.terminal_scrollback.get_value())
        settings.terminal_cursor_shape = self.terminal_cursor_shape.get_active_id() or "block"
        settings.terminal_cursor_blink = bool(self.terminal_cursor_blink.get_active())
        settings.terminal_copy_on_select = bool(self.terminal_copy_on_select.get_active())
        settings.terminal_scroll_on_output = bool(self.terminal_scroll_on_output.get_active())
        settings.terminal_audible_bell = bool(self.terminal_audible_bell.get_active())
        settings.terminal_shell = self.terminal_shell.get_text().strip()
        settings.terminal_start_directory = self.terminal_start_directory.get_text().strip()

        settings.rdp_preflight = bool(self.rdp_preflight.get_active())
        settings.connection_timeout_seconds = int(self.connection_timeout.get_value())
        settings.ssh_keepalive_interval = int(self.ssh_keepalive_interval.get_value())
        settings.ssh_keepalive_count = int(self.ssh_keepalive_count.get_value())
        settings.ssh_compression = bool(self.ssh_compression.get_active())
        settings.ssh_strict_host_key = self.ssh_strict_host_key.get_active_id() or "ask"
        settings.vault_auto_lock_minutes = (
            0 if self.vault_keep_unlocked.get_active() else int(self.vault_auto_lock.get_value())
        )
        settings.lock_vault_on_minimize = bool(self.lock_vault_on_minimize.get_active())
        settings.clear_clipboard_seconds = int(self.clear_clipboard.get_value())
        settings.ui_layout_version = 3
        settings.save(SETTINGS_FILE)

        self.parent_app.set_theme_from_menu(settings.theme)
        self.parent_app.refresh_icon_theme_runtime()
        self.parent_app.apply_layout_preferences()
        self.parent_app.apply_terminal_preferences()
        self.parent_app.mark_user_activity()
        self.parent_app.show_notification("Impostazioni applicate", "success")


class MultiExecDialog(Gtk.Dialog):
    """Invia un comando a piu' terminali, scelti uno per uno.

    Prima andava a tutte le schede senza dire quali: anche agli strumenti rapidi, al journal,
    alle console seriali. Ora si vede l'elenco e si sceglie; le sessioni remote sono gia' spuntate.
    """

    def __init__(self, parent: Gtk.Window, targets: Optional[List[Tuple[object, str, str, bool]]] = None) -> None:
        super().__init__(title="MultiExec", transient_for=parent, flags=0)
        self.set_modal(True)
        self.get_style_context().add_class("modern-dialog")
        self.add_button("_Annulla", Gtk.ResponseType.CANCEL)
        self.send_button = self.add_button("_Invia", Gtk.ResponseType.OK)
        self.send_button.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(560, -1)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=16)
        self.get_content_area().pack_start(outer, True, True, 0)
        caption = Gtk.Label.new_with_mnemonic("_Comando")
        caption.set_xalign(0)
        caption.get_style_context().add_class("field-label")
        outer.pack_start(caption, False, False, 0)
        self.command = Gtk.Entry()
        self.command.set_hexpand(True)
        self.command.set_activates_default(True)
        self.command.set_placeholder_text("Es. uptime  (viene inviato seguito da Invio)")
        caption.set_mnemonic_widget(self.command)
        outer.pack_start(self.command, False, False, 0)
        heading = Gtk.Label(label="Invia a", xalign=0)
        heading.get_style_context().add_class("field-label")
        outer.pack_start(heading, False, False, 4)
        self.checks: List[Tuple[Gtk.CheckButton, object]] = []
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for target, title, kind, default in targets or []:
            check = Gtk.CheckButton(label=f"{title}   ({kind})")
            check.set_active(default)
            check.connect("toggled", lambda *_a: self._update())
            box.pack_start(check, False, False, 0)
            self.checks.append((check, target))
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_propagate_natural_height(True)
        scroll.set_max_content_height(260)
        scroll.add(box)
        outer.pack_start(scroll, True, True, 0)
        self.command.connect("changed", lambda *_a: self._update())
        self._update()
        self.show_all()
        self.command.grab_focus()

    def _update(self) -> None:
        count = len(self.selected_targets())
        ready = bool(self.command.get_text().strip()) and count > 0
        self.set_response_sensitive(Gtk.ResponseType.OK, ready)
        label = self.send_button.get_child()
        if isinstance(label, Gtk.Label):
            label.set_text_with_mnemonic("_Invia" if count == 0 else f"_Invia a {count}")

    def selected_targets(self) -> List[object]:
        return [target for check, target in self.checks if check.get_active()]

    def get_command(self) -> str:
        return self.command.get_text()


class CommandPaletteDialog(Gtk.Dialog):
    """Palette comandi rapida per sistemisti: cerca e lancia azioni senza navigare nei menu."""

    def __init__(self, parent: Gtk.Window, actions: List[Tuple[str, str, Callable[[], None]]]) -> None:
        super().__init__(title="Command Palette", transient_for=parent, flags=0)
        self.set_modal(True)
        self.set_resizable(True)
        self.set_default_size(620, 460)
        self.add_buttons("Annulla", Gtk.ResponseType.CANCEL, "Esegui", Gtk.ResponseType.OK)
        self.selected_action: Optional[Callable[[], None]] = None
        self._actions = actions
        self._rows: List[Tuple[Gtk.ListBoxRow, str, Callable[[], None]]] = []

        box = self.get_content_area()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=14)
        outer.get_style_context().add_class("command-palette")
        box.add(outer)

        title = Gtk.Label(label="Cerca un comando o una sessione di lavoro", xalign=0)
        title.get_style_context().add_class("dialog-heading")
        outer.pack_start(title, False, False, 0)

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Esempio: rdp, tunnel, scan, capture, password, ping…")
        self.search.get_style_context().add_class("command-palette-search")
        outer.pack_start(self.search, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(300)
        scroll.get_style_context().add_class("command-palette-list-card")
        outer.pack_start(scroll, True, True, 0)

        self.listbox = Gtk.ListBox()
        self.listbox.get_style_context().add_class("command-palette-list")
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        scroll.add(self.listbox)

        for title_text, keywords, callback in actions:
            row = Gtk.ListBoxRow()
            row.set_activatable(True)
            row.get_style_context().add_class("command-palette-row")
            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin=8)
            main = Gtk.Label(label=title_text, xalign=0)
            main.get_style_context().add_class("command-palette-title")
            sub = Gtk.Label(label=keywords, xalign=0)
            sub.get_style_context().add_class("muted")
            sub.set_ellipsize(Pango.EllipsizeMode.END)
            row_box.pack_start(main, False, False, 0)
            row_box.pack_start(sub, False, False, 0)
            row.add(row_box)
            self.listbox.add(row)
            self._rows.append((row, (title_text + " " + keywords).casefold(), callback))

        self.search.connect("search-changed", self._filter)
        self.search.connect("activate", lambda _e: self.response(Gtk.ResponseType.OK))
        self.listbox.connect("row-activated", self._row_activated)
        self.listbox.show_all()
        self._filter()
        GLib.idle_add(lambda: (self.search.grab_focus(), False)[1])

    def _row_activated(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        for current, _hay, callback in self._rows:
            if current is row:
                self.selected_action = callback
                self.response(Gtk.ResponseType.OK)
                return

    def _filter(self, *_args) -> None:
        query = self.search.get_text().strip().casefold()
        first_visible: Optional[Gtk.ListBoxRow] = None
        for row, hay, _callback in self._rows:
            visible = (not query) or all(part in hay for part in query.split())
            row.set_visible(visible)
            if visible and first_visible is None:
                first_visible = row
        if first_visible is not None:
            self.listbox.select_row(first_visible)

    def run(self) -> int:
        response = super().run()
        row = self.listbox.get_selected_row()
        if row is not None:
            for current, _hay, callback in self._rows:
                if current is row:
                    self.selected_action = callback
                    break
        return response


class ToolsDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(title="Tools", transient_for=parent, flags=0)
        self.set_modal(True)
        self.parent_app = parent
        self.add_buttons("Chiudi", Gtk.ResponseType.CLOSE)
        self.set_default_size(620, 360)
        box = self.get_content_area()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        box.add(outer)
        outer.pack_start(Gtk.Label(label="Strumenti rapidi di rete e sistema", xalign=0), False, False, 0)
        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        outer.pack_start(grid, True, True, 0)
        actions = [
            ('Ping', 'read -rp \'Host da pingare [8.8.8.8]: \' h; h=${h:-8.8.8.8}; ping -c 5 "$h"; echo; read -rp \'Invio per chiudere...\' _'),
            ('Traceroute', 'read -rp \'Host traceroute [8.8.8.8]: \' h; h=${h:-8.8.8.8}; tracepath "$h" || traceroute "$h"; echo; read -rp \'Invio per chiudere...\' _'),
            ('DNS lookup', 'read -rp \'Nome DNS [example.com]: \' h; h=${h:-example.com}; dig "$h" || nslookup "$h"; echo; read -rp \'Invio per chiudere...\' _'),
            ('Check porta TCP', 'read -rp \'Host: \' h; read -rp \'Porta: \' p; timeout 4 bash -c "</dev/tcp/$h/$p" && echo \'Porta aperta\' || echo \'Porta chiusa/non raggiungibile\'; echo; read -rp \'Invio per chiudere...\' _'),
            ('IP locale', "ip -br addr; echo; read -rp 'Invio per chiudere...' _"),
            ('Route', "ip route; echo; read -rp 'Invio per chiudere...' _"),
            ('Socket aperti', "ss -tulpen; echo; read -rp 'Invio per chiudere...' _"),
            ('Chiavi SSH', "ls -lah ~/.ssh; echo; ssh-add -l 2>/dev/null || true; echo; read -rp 'Invio per chiudere...' _"),
            ('Known hosts', '${EDITOR:-nano} ~/.ssh/known_hosts'),
            ('Journal', 'journalctl -f'),
            ('Info sistema', "hostnamectl; echo; lsb_release -a 2>/dev/null || cat /etc/os-release; echo; read -rp 'Invio per chiudere...' _"),
            ('Doctor FedTermHub', "fedtermhub --doctor; echo; read -rp 'Invio per chiudere...' _"),
        ]
        for idx, (label, cmd) in enumerate(actions):
            button = Gtk.Button.new_with_label(label)
            button.connect("clicked", lambda _b, c=cmd, l=label: self.run_tool(l, c))
            grid.attach(button, idx % 3, idx // 3, 1, 1)
        self.show_all()

    def run_tool(self, label: str, command: str) -> None:
        self.parent_app.run_quick_tool(label, command)
        self.response(Gtk.ResponseType.CLOSE)


class FolderDialog(Gtk.Dialog):
    def __init__(
        self,
        parent: Gtk.Window,
        title: str = "Cartella",
        name: str = "",
        color: str = DEFAULT_FOLDER_COLOR,
    ) -> None:
        super().__init__(title=title, transient_for=parent, flags=0)
        self.set_modal(True)
        self.add_buttons("_Annulla", Gtk.ResponseType.CANCEL, "_Salva", Gtk.ResponseType.OK)
        self.set_default_size(430, 160)

        box = self.get_content_area()
        grid = Gtk.Grid(column_spacing=12, row_spacing=12, margin=16)
        box.add(grid)

        grid.attach(Gtk.Label(label="Nome cartella", xalign=0), 0, 0, 1, 1)
        self.name = Gtk.Entry(text=name)
        self.name.set_hexpand(True)
        self.name.set_activates_default(True)
        grid.attach(self.name, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Colore icona", xalign=0), 0, 1, 1, 1)
        color_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.color_button = Gtk.ColorButton()
        self.color_button.set_use_alpha(False)
        rgba = Gdk.RGBA()
        rgba.parse(normalize_folder_color(color))
        self.color_button.set_rgba(rgba)
        self.color_button.set_tooltip_text("Scegli il colore dell'icona cartella")
        color_row.pack_start(self.color_button, False, False, 0)
        hint = Gtk.Label(label="Il colore viene salvato insieme alla cartella.", xalign=0)
        hint.get_style_context().add_class("muted")
        color_row.pack_start(hint, True, True, 0)
        grid.attach(color_row, 1, 1, 1, 1)

        self.set_default_response(Gtk.ResponseType.OK)
        self.show_all()

    def get_name(self) -> str:
        return self.name.get_text().strip() or "Nuova cartella"

    def get_color(self) -> str:
        rgba = self.color_button.get_rgba()
        return "#{:02x}{:02x}{:02x}".format(
            round(max(0.0, min(1.0, rgba.red)) * 255),
            round(max(0.0, min(1.0, rgba.green)) * 255),
            round(max(0.0, min(1.0, rgba.blue)) * 255),
        )


class MoveDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window, folders: List[Tuple[str, str]], current_parent_path: str = "") -> None:
        super().__init__(title="Sposta in cartella", transient_for=parent, flags=0)
        self.set_modal(True)
        self.add_buttons("_Annulla", Gtk.ResponseType.CANCEL, "Sposta", Gtk.ResponseType.OK)
        self.set_default_size(420, 140)

        box = self.get_content_area()
        grid = Gtk.Grid(column_spacing=10, row_spacing=10, margin=14)
        box.add(grid)

        grid.attach(Gtk.Label(label="Destinazione", xalign=0), 0, 0, 1, 1)
        self.combo = Gtk.ComboBoxText()
        for path, label in folders:
            combo_id = path if path else "__ROOT__"
            self.combo.append(combo_id, label)
        active_id = current_parent_path if any(path == current_parent_path for path, _label in folders) else ""
        self.combo.set_active_id(active_id if active_id else "__ROOT__")
        if self.combo.get_active() < 0:
            self.combo.set_active(0)
        grid.attach(self.combo, 1, 0, 1, 1)
        self.show_all()

    def get_folder_path(self) -> str:
        active_id = self.combo.get_active_id()
        if not active_id or active_id == "__ROOT__":
            return ""
        return active_id


class SessionDialog(Gtk.Dialog):
    PROTOCOLS = [
        ("ssh", "SSH"),
        ("rdp", "RDP"),
        ("vnc", "VNC"),
        ("sftp", "SFTP"),
        ("serial", "Seriale"),
        ("local", "Terminale locale"),
        ("command", "Comando"),
    ]

    def __init__(self, parent: Gtk.Window, session: Optional[Session] = None, credentials: Optional[List[Tuple[str, str]]] = None, tunnels: Optional[List[Tuple[str, str]]] = None) -> None:
        super().__init__(title="Sessione", transient_for=parent, flags=0)
        self.set_modal(True)
        self.add_buttons("_Annulla", Gtk.ResponseType.CANCEL, "_Salva", Gtk.ResponseType.OK)
        # Salva e' l'azione principale: evidenziata come "Applica" nelle
        # Preferenze, e attivata da Invio nei campi di testo.
        salva = self.get_widget_for_response(Gtk.ResponseType.OK)
        if salva is not None:
            salva.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(720, 600)
        # Con dati che non permetterebbero di connettersi (host mancante, porta non numerica)
        # la finestra resta aperta e dice cosa correggere, invece di salvare una sessione rotta.
        self.connect("response", self._validate_on_save)

        self.original = session or Session(name="")
        credentials = credentials or []
        tunnels = tunnels or []
        self.available_credentials = list(credentials)
        self.available_tunnels = list(tunnels)

        box = self.get_content_area()
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(500)
        scroll.set_min_content_width(560)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=14)
        outer.get_style_context().add_class("session-editor")
        scroll.add(outer)
        box.add(scroll)

        heading = Gtk.Label(label="Nuova connessione" if not self.original.name else f"Modifica: {self.original.name}", xalign=0)
        heading.get_style_context().add_class("dialog-heading")
        outer.pack_start(heading, False, False, 0)

        top = Gtk.Grid(column_spacing=12, row_spacing=10)
        top.set_column_homogeneous(False)
        outer.pack_start(top, False, False, 0)

        self.name = Gtk.Entry(text=self.original.name)
        self.name.set_hexpand(True)
        self.name.set_placeholder_text("Nome visibile nella lista sessioni")
        self.protocol = Gtk.ComboBoxText()
        self.protocol.set_hexpand(True)
        for proto, label in self.PROTOCOLS:
            self.protocol.append(proto, label)
        self.protocol.set_active_id(self.original.protocol if self.original.protocol in [p for p, _l in self.PROTOCOLS] else "ssh")
        if self.protocol.get_active() < 0:
            self.protocol.set_active(0)
        self.protocol.connect("changed", self.on_protocol_changed)

        top.attach(Gtk.Label(label="Nome", xalign=0), 0, 0, 1, 1)
        top.attach(self.name, 1, 0, 1, 1)
        top.attach(Gtk.Label(label="Protocollo", xalign=0), 0, 1, 1, 1)
        top.attach(self.protocol, 1, 1, 1, 1)

        self.stack = Gtk.Stack()
        animate = bool(getattr(getattr(parent, "settings", None), "animations", True)) and not SAFE_MODE
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT if animate else Gtk.StackTransitionType.NONE)
        self.stack.set_transition_duration(160 if animate else 0)
        outer.pack_start(self.stack, True, True, 0)

        self._build_protocol_pages()

        self.auth_box = Gtk.Frame(label="Password Manager")
        self.auth_grid = Gtk.Grid(column_spacing=12, row_spacing=10, margin=10)
        self.auth_box.add(self.auth_grid)
        outer.pack_start(self.auth_box, False, False, 0)

        self.credential = Gtk.ComboBoxText()
        self.credential.append("", "(nessuna credenziale collegata)")
        found_current = self.original.credential_id == ""
        for cred_id, cred_name in credentials:
            self.credential.append(cred_id, cred_name)
            if cred_id == self.original.credential_id:
                found_current = True
        if not found_current and self.original.credential_id:
            self.credential.append(self.original.credential_id, f"ID salvato: {self.original.credential_id[:12]}…")
        self.credential.set_active_id(self.original.credential_id if self.original.credential_id else "")
        if self.credential.get_active() < 0:
            self.credential.set_active(0)
        self.use_credential_password = Gtk.CheckButton(label="Usa la password salvata quando il protocollo lo supporta")
        self.use_credential_password.set_active(self.original.use_credential_password)
        self.auth_grid.attach(Gtk.Label(label="Credenziale", xalign=0), 0, 0, 1, 1)
        self.auth_grid.attach(self.credential, 1, 0, 1, 1)
        self.auth_grid.attach(self.use_credential_password, 1, 1, 1, 1)

        # Le opzioni avanzate di instradamento restano chiuse per impostazione
        # predefinita: l'interfaccia normale non cambia per chi usa connessioni dirette.
        self.routing_expander = Gtk.Expander(label="Instradamento avanzato")
        self.routing_expander.set_expanded(
            (self.original.routing_mode or "direct") != "direct"
            or bool(self.original.required_tunnel_id)
        )
        self.routing_expander.set_tooltip_text(
            "Jump host, tunnel SSH salvati e RD Gateway. Apri solo quando necessari."
        )
        outer.pack_start(self.routing_expander, False, False, 0)
        self.workflow_box = self.routing_expander  # compatibilità con codice precedente

        route_shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=10)
        self.routing_expander.add(route_shell)
        route_grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        route_shell.pack_start(route_grid, False, False, 0)

        self.routing_mode = Gtk.ComboBoxText()
        self.routing_mode.set_hexpand(True)
        self.routing_mode.connect("changed", self.on_routing_mode_changed)
        route_grid.attach(Gtk.Label(label="Modalità", xalign=0), 0, 0, 1, 1)
        route_grid.attach(self.routing_mode, 1, 0, 1, 1)

        self.routing_stack = Gtk.Stack()
        self.routing_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.routing_stack.set_transition_duration(120 if animate else 0)
        route_shell.pack_start(self.routing_stack, False, False, 0)

        direct_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=4)
        direct_label = Gtk.Label(
            label="Connessione diretta: non viene avviato alcun gateway o tunnel preliminare.",
            xalign=0,
        )
        direct_label.set_line_wrap(True)
        direct_label.get_style_context().add_class("muted")
        direct_box.pack_start(direct_label, False, False, 0)
        self.routing_stack.add_named(direct_box, "direct")

        tunnel_grid = Gtk.Grid(column_spacing=12, row_spacing=8, margin=4)
        self.required_tunnel = Gtk.ComboBoxText()
        self.required_tunnel.append("", "Seleziona un tunnel SSH")
        found_tunnel = self.original.required_tunnel_id == ""
        for tunnel_id, tunnel_name in tunnels:
            self.required_tunnel.append(tunnel_id, tunnel_name)
            found_tunnel = found_tunnel or tunnel_id == self.original.required_tunnel_id
        if not found_tunnel and self.original.required_tunnel_id:
            self.required_tunnel.append(
                self.original.required_tunnel_id,
                f"Tunnel {self.original.required_tunnel_id[:10]}…",
            )
        self.required_tunnel.set_active_id(self.original.required_tunnel_id or "")
        if self.required_tunnel.get_active() < 0:
            self.required_tunnel.set_active(0)
        self.wait_for_tunnel = Gtk.CheckButton(
            label="Avvia automaticamente e attendi che il tunnel sia pronto"
        )
        self.wait_for_tunnel.set_active(self.original.wait_for_tunnel)
        tunnel_grid.attach(Gtk.Label(label="Tunnel salvato", xalign=0), 0, 0, 1, 1)
        tunnel_grid.attach(self.required_tunnel, 1, 0, 1, 1)
        tunnel_grid.attach(self.wait_for_tunnel, 1, 1, 1, 1)
        self.routing_stack.add_named(tunnel_grid, "saved_tunnel")

        jump_grid = Gtk.Grid(column_spacing=12, row_spacing=8, margin=4)
        self.jump_host = self.labeled_entry(
            jump_grid, 0, "Jump host", self.original.jump_host, "bastion.azienda.local"
        )
        self.jump_port = self.labeled_entry(
            jump_grid, 1, "Porta SSH", self.original.jump_port or "22", "22"
        )
        self.jump_user = self.labeled_entry(
            jump_grid, 2, "Utente", self.original.jump_user, "utente sul bastion"
        )
        jump_hint = Gtk.Label(
            label=(
                "Il jump host usa OpenSSH ProxyJump. Per la sua autenticazione usa "
                "ssh-agent o ~/.ssh/config; la credenziale della sessione resta destinata all'host finale."
            ),
            xalign=0,
        )
        jump_hint.set_line_wrap(True)
        jump_hint.get_style_context().add_class("muted")
        jump_grid.attach(jump_hint, 1, 3, 1, 1)
        self.routing_stack.add_named(jump_grid, "proxy_jump")

        gateway_grid = Gtk.Grid(column_spacing=12, row_spacing=8, margin=4)
        self.rdp_gateway_host = self.labeled_entry(
            gateway_grid, 0, "Server RD Gateway", self.original.rdp_gateway_host, "rdgateway.azienda.it"
        )
        self.rdp_gateway_port = self.labeled_entry(
            gateway_grid, 1, "Porta HTTPS", self.original.rdp_gateway_port or "443", "443"
        )
        self.rdp_gateway_same_credentials = Gtk.CheckButton(
            label="Usa le stesse credenziali della sessione RDP"
        )
        self.rdp_gateway_same_credentials.set_active(
            self.original.rdp_gateway_use_same_credentials
        )
        self.rdp_gateway_same_credentials.connect(
            "toggled", lambda *_args: self.update_gateway_controls()
        )
        gateway_grid.attach(Gtk.Label(label="Credenziali", xalign=0), 0, 2, 1, 1)
        gateway_grid.attach(self.rdp_gateway_same_credentials, 1, 2, 1, 1)

        self.rdp_gateway_user = self.labeled_entry(
            gateway_grid, 3, "Utente gateway", self.original.rdp_gateway_user, "utente gateway"
        )
        self.rdp_gateway_domain = self.labeled_entry(
            gateway_grid, 4, "Dominio gateway", self.original.rdp_gateway_domain, "AZIENDA"
        )
        self.rdp_gateway_credential = Gtk.ComboBoxText()
        self.rdp_gateway_credential.append("", "Nessuna credenziale separata")
        found_gateway_cred = self.original.rdp_gateway_credential_id == ""
        for cred_id, cred_name in credentials:
            self.rdp_gateway_credential.append(cred_id, cred_name)
            found_gateway_cred = found_gateway_cred or cred_id == self.original.rdp_gateway_credential_id
        if not found_gateway_cred and self.original.rdp_gateway_credential_id:
            self.rdp_gateway_credential.append(
                self.original.rdp_gateway_credential_id,
                f"Credenziale {self.original.rdp_gateway_credential_id[:10]}…",
            )
        self.rdp_gateway_credential.set_active_id(
            self.original.rdp_gateway_credential_id or ""
        )
        if self.rdp_gateway_credential.get_active() < 0:
            self.rdp_gateway_credential.set_active(0)
        gateway_grid.attach(Gtk.Label(label="Credenziale salvata", xalign=0), 0, 5, 1, 1)
        gateway_grid.attach(self.rdp_gateway_credential, 1, 5, 1, 1)

        self.rdp_gateway_transport = Gtk.ComboBoxText()
        self.rdp_gateway_transport.append("auto", "Automatico (consigliato)")
        self.rdp_gateway_transport.append("http", "HTTPS")
        self.rdp_gateway_transport.append("rpc", "RPC")
        selected_transport = (self.original.rdp_gateway_transport or "auto").lower()
        if selected_transport not in {"auto", "http", "rpc"}:
            selected_transport = "auto"
        self.rdp_gateway_transport.set_active_id(selected_transport)
        gateway_grid.attach(Gtk.Label(label="Trasporto", xalign=0), 0, 6, 1, 1)
        gateway_grid.attach(self.rdp_gateway_transport, 1, 6, 1, 1)
        gateway_hint = Gtk.Label(
            label=(
                "FedTermHub verifica prima il gateway sulla porta HTTPS. "
                "Il PC RDP può quindi restare non raggiungibile direttamente dalla rete locale."
            ),
            xalign=0,
        )
        gateway_hint.set_line_wrap(True)
        gateway_hint.get_style_context().add_class("muted")
        gateway_grid.attach(gateway_hint, 1, 7, 1, 1)
        self.routing_stack.add_named(gateway_grid, "rd_gateway")

        initial_mode = self.original.routing_mode or "direct"
        if initial_mode == "direct" and self.original.required_tunnel_id:
            initial_mode = "saved_tunnel"
        self._pending_routing_mode = initial_mode
        self.update_gateway_controls()

        bottom = Gtk.Frame(label="Note")
        bottom_grid = Gtk.Grid(column_spacing=12, row_spacing=10, margin=10)
        bottom.add(bottom_grid)
        outer.pack_start(bottom, False, False, 0)
        self.notes = Gtk.Entry(text=self.original.notes)
        self.notes.set_placeholder_text("Appunti liberi sulla connessione")
        bottom_grid.attach(self.notes, 0, 0, 1, 1)

        self.help_text = Gtk.Label(xalign=0)
        self.help_text.set_line_wrap(True)
        self.help_text.get_style_context().add_class("muted")
        outer.pack_start(self.help_text, False, False, 0)

        self.on_protocol_changed(self.protocol)
        self.set_default_response(Gtk.ResponseType.OK)
        self.show_all()
        self.on_protocol_changed(self.protocol)

    def refresh_routing_modes(self, protocol: str) -> None:
        current = getattr(self, "_pending_routing_mode", None) or self.routing_mode.get_active_id() or "direct"
        allowed = [("direct", "Connessione diretta")]
        if protocol in {"ssh", "rdp", "vnc", "sftp"}:
            allowed.append(("saved_tunnel", "Tunnel SSH salvato"))
        if protocol in {"ssh", "sftp"}:
            allowed.append(("proxy_jump", "Jump host SSH"))
        if protocol == "rdp":
            allowed.append(("rd_gateway", "RD Gateway"))
        allowed_ids = {item[0] for item in allowed}
        if current not in allowed_ids:
            current = "saved_tunnel" if self.original.required_tunnel_id and "saved_tunnel" in allowed_ids else "direct"
        self.routing_mode.handler_block_by_func(self.on_routing_mode_changed)
        self.routing_mode.remove_all()
        for mode_id, label in allowed:
            self.routing_mode.append(mode_id, label)
        self.routing_mode.set_active_id(current)
        if self.routing_mode.get_active() < 0:
            self.routing_mode.set_active(0)
        self.routing_mode.handler_unblock_by_func(self.on_routing_mode_changed)
        self._pending_routing_mode = None
        self.on_routing_mode_changed(self.routing_mode)

    def on_routing_mode_changed(self, combo: Gtk.ComboBoxText) -> None:
        mode = combo.get_active_id() or "direct"
        if hasattr(self, "routing_stack"):
            self.routing_stack.set_visible_child_name(mode)
        if hasattr(self, "wait_for_tunnel"):
            self.wait_for_tunnel.set_sensitive(mode == "saved_tunnel")
        if mode == "rd_gateway" and hasattr(self, "rdp_mode"):
            self.rdp_mode.set_active_id("native")
            self.rdp_mode.set_tooltip_text(
                "RD Gateway usa il backend nativo libfreerdp; la modalità legacy non viene utilizzata."
            )
        self.update_gateway_controls()

    def update_gateway_controls(self) -> None:
        if not hasattr(self, "rdp_gateway_same_credentials"):
            return
        separate = not self.rdp_gateway_same_credentials.get_active()
        for widget_name in ("rdp_gateway_user", "rdp_gateway_domain", "rdp_gateway_credential"):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.set_sensitive(separate)

    def labeled_entry(self, grid: Gtk.Grid, row: int, label: str, text: str = "", placeholder: str = "", password: bool = False) -> Gtk.Entry:
        widget = Gtk.Entry(text=text)
        widget.set_hexpand(True)
        widget.set_placeholder_text(placeholder)
        if password:
            widget.set_visibility(False)
            widget.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        lbl = Gtk.Label(label=label, xalign=0)
        lbl.get_style_context().add_class("field-label")
        grid.attach(lbl, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)
        return widget

    def protocol_page(self, title: str, subtitle: str = "") -> Tuple[Gtk.Box, Gtk.Grid]:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=10)
        page.set_vexpand(True)
        header = Gtk.Label(label=title, xalign=0)
        header.get_style_context().add_class("section-heading")
        page.pack_start(header, False, False, 0)
        if subtitle:
            sub = Gtk.Label(label=subtitle, xalign=0)
            sub.set_line_wrap(True)
            sub.get_style_context().add_class("muted")
            page.pack_start(sub, False, False, 0)
        frame = Gtk.Frame()
        frame.get_style_context().add_class("card")
        grid = Gtk.Grid(column_spacing=12, row_spacing=10, margin=14)
        grid.set_column_spacing(14)
        frame.add(grid)
        page.pack_start(frame, False, False, 0)
        return page, grid

    def _build_protocol_pages(self) -> None:
        sess = self.original

        # SSH
        page, grid = self.protocol_page("SSH", "Connessione shell remota dentro una tab integrata VTE.")
        self.ssh_host = self.labeled_entry(grid, 0, "Host/IP", sess.host, "es. 192.168.1.10 oppure server.dominio.local")
        self.ssh_port = self.labeled_entry(grid, 1, "Porta", sess.port or "22", "22")
        self.ssh_user = self.labeled_entry(grid, 2, "Utente", sess.user, "utente Linux/Unix")
        self.ssh_command = self.labeled_entry(grid, 3, "Comando remoto opzionale", sess.command, "es. htop oppure tail -f /var/log/messages")
        self.stack.add_named(page, "ssh")

        # RDP
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=8)
        page.set_vexpand(True)
        header = Gtk.Label(label="Desktop remoto RDP", xalign=0)
        header.get_style_context().add_class("section-heading")
        page.pack_start(header, False, False, 0)
        subtitle = Gtk.Label(
            label="Configurazione divisa per argomento: connessione, schermo, risorse e opzioni avanzate.",
            xalign=0,
        )
        subtitle.set_line_wrap(True)
        subtitle.get_style_context().add_class("muted")
        page.pack_start(subtitle, False, False, 0)

        rdp_book = Gtk.Notebook()
        self.rdp_book = rdp_book
        rdp_book.set_scrollable(True)
        rdp_book.get_style_context().add_class("settings-notebook")
        page.pack_start(rdp_book, True, True, 0)

        def rdp_grid() -> Gtk.Grid:
            g = Gtk.Grid(column_spacing=14, row_spacing=11, margin=16)
            g.set_hexpand(True)
            g.get_style_context().add_class("form-card")
            return g

        # Connessione
        conn = rdp_grid()
        self.rdp_host = self.labeled_entry(conn, 0, "PC / IP", sess.host, "192.168.1.20 oppure pc.dominio.local")
        self.rdp_port = self.labeled_entry(conn, 1, "Porta", sess.port or "3389", "3389")
        self.rdp_user = self.labeled_entry(conn, 2, "Utente", sess.user, "utente Windows")
        self.rdp_domain = self.labeled_entry(conn, 3, "Dominio", sess.domain, "AZIENDA oppure vuoto")
        self.rdp_password = self.labeled_entry(conn, 4, "Password", "", "solo in memoria o nel Password Manager", password=True)
        password_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.rdp_show_password = Gtk.CheckButton(label="Mostra")
        self.rdp_show_password.connect("toggled", lambda b: self.rdp_password.set_visibility(b.get_active()))
        self.rdp_save_password = Gtk.CheckButton(label="Proponi salvataggio nel Password Manager cifrato")
        self.rdp_save_password.set_active(True)
        password_row.pack_start(self.rdp_show_password, False, False, 0)
        password_row.pack_start(self.rdp_save_password, False, False, 0)
        conn.attach(Gtk.Label(label="Gestione password", xalign=0), 0, 5, 1, 1)
        conn.attach(password_row, 1, 5, 1, 1)

        test_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        test_btn = Gtk.Button.new_with_mnemonic("_Test host e porta")
        test_btn.get_style_context().add_class("secondary-action")
        self.rdp_test_status = Gtk.Label(label="", xalign=0)
        self.rdp_test_status.get_style_context().add_class("muted")
        test_btn.connect("clicked", self.on_rdp_test_clicked)
        test_row.pack_start(test_btn, False, False, 0)
        test_row.pack_start(self.rdp_test_status, True, True, 0)
        conn.attach(Gtk.Label(label="Verifica", xalign=0), 0, 6, 1, 1)
        conn.attach(test_row, 1, 6, 1, 1)
        rdp_book.append_page(conn, Gtk.Label(label="Connessione"))

        # Schermo
        display = rdp_grid()
        self.rdp_mode = Gtk.ComboBoxText()
        self.rdp_mode.append("native", "Nativa libfreerdp (integrata o indipendente)")
        self.rdp_mode.append("legacy", "Legacy xfreerdp/XEmbed (solo diagnostica)")
        selected_mode = "legacy" if sess.rdp_mode in {"legacy", "external-legacy"} else "native"
        self.rdp_mode.set_active_id(selected_mode)
        self.rdp_mode.set_tooltip_text(
            "La modalità nativa funziona su Wayland e X11. Il fallback legacy richiede X11/XWayland."
        )
        display.attach(Gtk.Label(label="Modalità", xalign=0), 0, 0, 1, 1)
        display.attach(self.rdp_mode, 1, 0, 1, 1)

        self.rdp_quality = Gtk.ComboBoxText()
        for value, label in [("lan", "LAN / qualità alta"), ("wan", "WAN / bilanciato"), ("low", "Connessione lenta")]:
            self.rdp_quality.append(value, label)
        self.rdp_quality.set_active_id(sess.rdp_quality if sess.rdp_quality in {"lan", "wan", "low"} else "lan")
        display.attach(Gtk.Label(label="Profilo rete", xalign=0), 0, 1, 1, 1)
        display.attach(self.rdp_quality, 1, 1, 1, 1)

        size_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.rdp_width = Gtk.Entry(text=sess.rdp_width)
        self.rdp_width.set_placeholder_text("1280")
        self.rdp_height = Gtk.Entry(text=sess.rdp_height)
        self.rdp_height.set_placeholder_text("720")
        size_box.pack_start(self.rdp_width, True, True, 0)
        size_box.pack_start(Gtk.Label(label="×"), False, False, 0)
        size_box.pack_start(self.rdp_height, True, True, 0)
        display.attach(Gtk.Label(label="Risoluzione fissa", xalign=0), 0, 2, 1, 1)
        display.attach(size_box, 1, 2, 1, 1)

        self.rdp_scale = Gtk.ComboBoxText()
        for value in ["100", "140", "180"]:
            self.rdp_scale.append(value, value + "%")
        self.rdp_scale.set_active_id(sess.rdp_scale if sess.rdp_scale in {"100", "140", "180"} else "100")
        display.attach(Gtk.Label(label="Scala interfaccia", xalign=0), 0, 3, 1, 1)
        display.attach(self.rdp_scale, 1, 3, 1, 1)

        display_opts = Gtk.Grid(column_spacing=18, row_spacing=8)
        self.rdp_dynamic_resolution = Gtk.CheckButton(label="Adatta risoluzione alla tab e allo schermo intero")
        self.rdp_dynamic_resolution.set_active(sess.rdp_dynamic_resolution)
        self.rdp_smart_sizing = Gtk.CheckButton(label="Smart sizing")
        self.rdp_smart_sizing.set_active(sess.rdp_smart_sizing)
        self.rdp_multimon = Gtk.CheckButton(label="Multi monitor (non disponibile nel backend nativo)")
        self.rdp_multimon.set_active(False)
        self.rdp_multimon.set_sensitive(False)
        self.rdp_multimon.set_tooltip_text("Il renderer nativo espone per ora un singolo framebuffer remoto.")
        self.rdp_fonts = Gtk.CheckButton(label="Font smoothing")
        self.rdp_fonts.set_active(sess.rdp_fonts)
        for i, widget in enumerate([self.rdp_dynamic_resolution, self.rdp_smart_sizing, self.rdp_multimon, self.rdp_fonts]):
            display_opts.attach(widget, i % 2, i // 2, 1, 1)
        display.attach(Gtk.Label(label="Visualizzazione", xalign=0), 0, 4, 1, 1)
        display.attach(display_opts, 1, 4, 1, 1)

        self.rdp_detach_on_open = Gtk.CheckButton(
            label="Apri già sganciata in una finestra indipendente"
        )
        self.rdp_detach_on_open.set_active(sess.rdp_window_mode == "detached")
        self.rdp_detach_on_open.set_tooltip_text(
            "Avvia la sessione in una finestra separata gestita da FedTermHub, "
            "con barra di controllo e monitor selezionato."
        )
        display.attach(Gtk.Label(label="Sgancio", xalign=0), 0, 5, 1, 1)
        display.attach(self.rdp_detach_on_open, 1, 5, 1, 1)

        self.rdp_start_fullscreen = Gtk.CheckButton(
            label="Apri direttamente a schermo intero"
        )
        self.rdp_start_fullscreen.set_active(sess.rdp_start_fullscreen)
        self.rdp_start_fullscreen.set_tooltip_text(
            "Avvia la sessione sul monitor scelto. Con finestra indipendente, FedTermHub resta utilizzabile."
        )
        display.attach(Gtk.Label(label="Avvio", xalign=0), 0, 6, 1, 1)
        display.attach(self.rdp_start_fullscreen, 1, 6, 1, 1)

        monitor_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.rdp_monitor = Gtk.ComboBoxText()
        self.rdp_monitor.set_hexpand(True)
        self.rdp_monitor_refresh = Gtk.Button.new_with_mnemonic("_Aggiorna")
        self.rdp_monitor_refresh.set_tooltip_text("Rileva nuovamente i monitor collegati")
        self.rdp_monitor_refresh.connect(
            "clicked", lambda _button: self.refresh_rdp_monitor_combo()
        )
        monitor_row.pack_start(self.rdp_monitor, True, True, 0)
        monitor_row.pack_start(self.rdp_monitor_refresh, False, False, 0)
        self.refresh_rdp_monitor_combo(sess.rdp_monitor or "current")
        display.attach(Gtk.Label(label="Monitor schermo intero", xalign=0), 0, 7, 1, 1)
        display.attach(monitor_row, 1, 7, 1, 1)

        monitor_hint = Gtk.Label(
            label=(
                "La scelta viene usata anche premendo Schermo intero o F11. "
                "Se il monitor non è collegato, FedTermHub usa il monitor corrente o quello principale."
            ),
            xalign=0,
        )
        monitor_hint.set_line_wrap(True)
        monitor_hint.get_style_context().add_class("muted")
        display.attach(monitor_hint, 1, 8, 1, 1)
        rdp_book.append_page(display, Gtk.Label(label="Schermo"))

        # Risorse locali
        resources = rdp_grid()
        drive_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.rdp_drive = Gtk.Entry(text=sess.rdp_drive)
        self.rdp_drive.set_hexpand(True)
        self.rdp_drive.set_placeholder_text("/home/michele oppure /mnt/dati")
        self.rdp_drive.set_tooltip_text(
            "La cartella locale appare in Esplora risorse di Windows come unità "
            "di rete “fedtermhub”: copia e incolla file in entrambe le direzioni."
        )
        drive_browse = Gtk.Button.new_with_mnemonic("S_foglia…")

        def on_browse_drive(_button: Gtk.Button) -> None:
            chooser = Gtk.FileChooserDialog(
                title="Scegli la cartella da condividere nella sessione RDP",
                transient_for=self,
                action=Gtk.FileChooserAction.SELECT_FOLDER,
            )
            chooser.add_buttons("Annulla", Gtk.ResponseType.CANCEL, "Seleziona", Gtk.ResponseType.OK)
            current = self.rdp_drive.get_text().strip()
            if current:
                try:
                    chooser.set_current_folder(str(Path(current).expanduser()))
                except Exception:
                    pass
            if chooser.run() == Gtk.ResponseType.OK:
                self.rdp_drive.set_text(chooser.get_filename() or "")
            chooser.destroy()

        drive_browse.connect("clicked", on_browse_drive)
        drive_box.pack_start(self.rdp_drive, True, True, 0)
        drive_box.pack_start(drive_browse, False, False, 0)
        drive_label = Gtk.Label(label="Cartella condivisa", xalign=0)
        drive_label.get_style_context().add_class("field-label")
        resources.attach(drive_label, 0, 0, 1, 1)
        resources.attach(drive_box, 1, 0, 1, 1)
        drive_hint = Gtk.Label(
            label=(
                "Visibile in Windows come unità di rete “fedtermhub” per scambiare file "
                "con il PC remoto. La cartella deve esistere e il percorso non può contenere virgole."
            ),
            xalign=0,
        )
        drive_hint.set_line_wrap(True)
        drive_hint.get_style_context().add_class("muted")
        resources.attach(drive_hint, 1, 1, 1, 1)
        resource_opts = Gtk.Grid(column_spacing=18, row_spacing=8)
        self.rdp_clipboard = Gtk.CheckButton(label="Clipboard (supporto nativo in evoluzione)")
        self.rdp_clipboard.set_active(sess.rdp_clipboard)
        self.rdp_sound = Gtk.CheckButton(label="Audio remoto")
        self.rdp_sound.set_active(sess.rdp_sound)
        self.rdp_microphone = Gtk.CheckButton(label="Microfono")
        self.rdp_microphone.set_active(sess.rdp_microphone)
        self.rdp_printer = Gtk.CheckButton(label="Stampante predefinita")
        self.rdp_printer.set_active(sess.rdp_printer)
        self.rdp_smartcard = Gtk.CheckButton(label="Smart card")
        self.rdp_smartcard.set_active(sess.rdp_smartcard)
        for i, widget in enumerate([self.rdp_clipboard, self.rdp_sound, self.rdp_microphone, self.rdp_printer, self.rdp_smartcard]):
            resource_opts.attach(widget, i % 2, i // 2, 1, 1)
        resources.attach(Gtk.Label(label="Reindirizzamenti", xalign=0), 0, 2, 1, 1)
        resources.attach(resource_opts, 1, 2, 1, 1)
        rdp_book.append_page(resources, Gtk.Label(label="Risorse"))

        # Avanzate
        advanced = rdp_grid()
        keyboard_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.rdp_keyboard_layout = Gtk.ComboBoxText()
        for value, label in [("auto", "Automatico"), ("it", "Italiano"), ("us", "Inglese USA"), ("de", "Tedesco"), ("fr", "Francese"), ("es", "Spagnolo"), ("custom", "ID FreeRDP personalizzato")]:
            self.rdp_keyboard_layout.append(value, label)
        known_layouts = {"auto", "it", "us", "de", "fr", "es"}
        active_layout = sess.rdp_keyboard_layout if sess.rdp_keyboard_layout in known_layouts else "custom"
        self.rdp_keyboard_layout.set_active_id(active_layout)
        self.rdp_keyboard_custom = Gtk.Entry()
        self.rdp_keyboard_custom.set_placeholder_text("es. 0x00000410 oppure nome layout")
        if active_layout == "custom":
            self.rdp_keyboard_custom.set_text(sess.rdp_keyboard_layout)
        self.rdp_keyboard_custom.set_visible(active_layout == "custom")
        self.rdp_keyboard_layout.connect("changed", lambda c: self.rdp_keyboard_custom.set_visible(c.get_active_id() == "custom"))
        keyboard_box.pack_start(self.rdp_keyboard_layout, False, False, 0)
        keyboard_box.pack_start(self.rdp_keyboard_custom, False, False, 0)
        advanced.attach(Gtk.Label(label="Layout tastiera", xalign=0), 0, 0, 1, 1)
        advanced.attach(keyboard_box, 1, 0, 1, 1)

        self.rdp_security = Gtk.ComboBoxText()
        for value, label in [("auto", "Negoziazione automatica"), ("nla", "NLA"), ("tls", "TLS"), ("rdp", "RDP legacy")]:
            self.rdp_security.append(value, label)
        self.rdp_security.set_active_id(sess.rdp_security if sess.rdp_security in {"auto", "nla", "tls", "rdp"} else "auto")
        advanced.attach(Gtk.Label(label="Sicurezza", xalign=0), 0, 1, 1, 1)
        advanced.attach(self.rdp_security, 1, 1, 1, 1)

        self.rdp_timeout = self.labeled_entry(advanced, 2, "Timeout connessione (ms)", sess.rdp_timeout or "12000", "12000")
        self.rdp_options = self.labeled_entry(advanced, 3, "Opzioni FreeRDP extra", sess.command, "/log-level:INFO")

        adv_opts = Gtk.Grid(column_spacing=18, row_spacing=8)
        self.rdp_grab_keyboard = Gtk.CheckButton(label="Cattura tastiera (solo backend legacy)")
        self.rdp_grab_keyboard.set_active(sess.rdp_grab_keyboard)
        self.rdp_grab_mouse = Gtk.CheckButton(label="Cattura mouse (solo backend legacy)")
        self.rdp_grab_mouse.set_active(sess.rdp_grab_mouse)
        self.rdp_auto_reconnect = Gtk.CheckButton(label="Auto reconnect")
        self.rdp_auto_reconnect.set_active(sess.rdp_auto_reconnect)
        self.rdp_cert_ignore = Gtk.CheckButton(label="Ignora certificato non attendibile")
        self.rdp_cert_ignore.set_active(sess.rdp_cert_ignore)
        self.rdp_admin = Gtk.CheckButton(label="Console /admin")
        self.rdp_admin.set_active(sess.rdp_admin)
        self.rdp_gfx = Gtk.CheckButton(label="Pipeline GFX (consigliata per finestra indipendente)")
        self.rdp_gfx.set_active(sess.rdp_gfx)
        self.rdp_compression = Gtk.CheckButton(label="Compressione")
        self.rdp_compression.set_active(sess.rdp_compression)
        self.rdp_password_stdin = Gtk.CheckButton(label="Trasporto password protetto")
        self.rdp_password_stdin.set_active(sess.rdp_password_stdin)
        for i, widget in enumerate([
            self.rdp_grab_keyboard, self.rdp_grab_mouse, self.rdp_auto_reconnect,
            self.rdp_cert_ignore, self.rdp_admin, self.rdp_gfx,
            self.rdp_compression, self.rdp_password_stdin,
        ]):
            adv_opts.attach(widget, i % 2, i // 2, 1, 1)
        advanced.attach(Gtk.Label(label="Comportamento", xalign=0), 0, 4, 1, 1)
        advanced.attach(adv_opts, 1, 4, 1, 1)
        rdp_book.append_page(advanced, Gtk.Label(label="Avanzate"))

        def apply_rdp_backend_controls(_widget=None) -> None:
            native = (self.rdp_mode.get_active_id() or "native") == "native"
            detached = self.rdp_detach_on_open.get_active()
            self.rdp_grab_keyboard.set_sensitive(not native or detached)
            self.rdp_grab_mouse.set_sensitive(not native or detached)
            self.rdp_gfx.set_sensitive(detached or not native)
            self.rdp_password_stdin.set_sensitive(not native or detached)
            self.rdp_detach_on_open.set_sensitive(True)
            self.rdp_start_fullscreen.set_sensitive(native or detached)
            self.rdp_monitor.set_sensitive(native or detached)
            self.rdp_monitor_refresh.set_sensitive(native or detached)
            if native and not detached:
                self.rdp_password_stdin.set_active(True)
                self.rdp_password_stdin.set_tooltip_text(
                    "Nel backend integrato la password passa sempre attraverso un memfd anonimo sigillato."
                )
                self.rdp_start_fullscreen.set_tooltip_text(
                    "Avvia questa sessione RDP in modalità immersiva dentro FedTermHub."
                )
            else:
                if detached and not self.rdp_gfx.get_active():
                    self.rdp_gfx.set_active(True)
                self.rdp_password_stdin.set_active(True)
                self.rdp_password_stdin.set_tooltip_text(
                    "La finestra indipendente usa /args-from:fd per non esporre la password nei processi."
                )
                self.rdp_start_fullscreen.set_tooltip_text(
                    "Avvia xfreerdp fullscreen sul monitor selezionato lasciando FedTermHub disponibile."
                )

        self.rdp_mode.connect("changed", apply_rdp_backend_controls)
        self.rdp_detach_on_open.connect("toggled", apply_rdp_backend_controls)
        self.rdp_start_fullscreen.connect("toggled", apply_rdp_backend_controls)
        apply_rdp_backend_controls()

        self.stack.add_named(page, "rdp")

        # VNC
        page, grid = self.protocol_page("VNC", "Connessione VNC tramite TigerVNC viewer.")
        self.vnc_host = self.labeled_entry(grid, 0, "Host/IP", sess.host, "es. 192.168.1.30")
        self.vnc_port = self.labeled_entry(grid, 1, "Display/porta", sess.port, "display 1 oppure porta 5901")
        self.vnc_options = self.labeled_entry(grid, 2, "Opzioni VNC extra", sess.command, "opzionale")
        self.stack.add_named(page, "vnc")

        # SFTP
        page, grid = self.protocol_page("SFTP", "Sessione SFTP interattiva dentro una tab terminale.")
        self.sftp_host = self.labeled_entry(grid, 0, "Host/IP", sess.host, "server SFTP")
        self.sftp_port = self.labeled_entry(grid, 1, "Porta", sess.port or "22", "22")
        self.sftp_user = self.labeled_entry(grid, 2, "Utente", sess.user, "utente SFTP")
        self.stack.add_named(page, "sftp")

        # Seriale
        page, grid = self.protocol_page("Seriale", "Console seriale integrata, utile per switch, firewall, router e apparati industriali.")
        self.serial_device = Gtk.ComboBoxText.new_with_entry()
        self.serial_device.set_hexpand(True)
        self.refresh_serial_combo(sess.host or "/dev/ttyUSB0")
        self.serial_baud = Gtk.ComboBoxText.new_with_entry()
        self.serial_baud.set_hexpand(True)
        for baud in ["9600", "19200", "38400", "57600", "115200"]:
            self.serial_baud.append(baud, baud)
        set_combo_text(self.serial_baud, sess.port or "115200")
        self.serial_databits = Gtk.ComboBoxText()
        for item in ["8", "7", "6", "5"]:
            self.serial_databits.append(item, item)
        self.serial_databits.set_active_id(sess.serial_databits if sess.serial_databits in ["8", "7", "6", "5"] else "8")
        self.serial_parity = Gtk.ComboBoxText()
        for item, label in [("N", "N - nessuna"), ("E", "E - pari"), ("O", "O - dispari"), ("M", "M - mark"), ("S", "S - space")]:
            self.serial_parity.append(item, label)
        self.serial_parity.set_active_id(sess.serial_parity if sess.serial_parity in ["N", "E", "O", "M", "S"] else "N")
        self.serial_stopbits = Gtk.ComboBoxText()
        for item in ["1", "1.5", "2"]:
            self.serial_stopbits.append(item, item)
        self.serial_stopbits.set_active_id(sess.serial_stopbits if sess.serial_stopbits in ["1", "1.5", "2"] else "1")
        btn_refresh = Gtk.Button.new_with_mnemonic("Aggiorna _porte")
        btn_refresh.connect("clicked", lambda _b: self.refresh_serial_combo(get_combo_text(self.serial_device, sess.host or "/dev/ttyUSB0")))
        grid.attach(Gtk.Label(label="Porta seriale", xalign=0), 0, 0, 1, 1)
        grid.attach(self.serial_device, 1, 0, 1, 1)
        grid.attach(btn_refresh, 2, 0, 1, 1)
        grid.attach(Gtk.Label(label="Baud rate", xalign=0), 0, 1, 1, 1)
        grid.attach(self.serial_baud, 1, 1, 1, 1)
        grid.attach(Gtk.Label(label="Data bits", xalign=0), 0, 2, 1, 1)
        grid.attach(self.serial_databits, 1, 2, 1, 1)
        grid.attach(Gtk.Label(label="Parità", xalign=0), 0, 3, 1, 1)
        grid.attach(self.serial_parity, 1, 3, 1, 1)
        grid.attach(Gtk.Label(label="Stop bits", xalign=0), 0, 4, 1, 1)
        grid.attach(self.serial_stopbits, 1, 4, 1, 1)
        self.stack.add_named(page, "serial")

        # Locale
        page, grid = self.protocol_page("Terminale locale", "Apre la shell locale predefinita dentro una nuova tab.")
        local_info = Gtk.Label(label="Nessun host, utente o password richiesti. Usa questa sessione come terminale Fedora locale.", xalign=0)
        local_info.set_line_wrap(True)
        grid.attach(local_info, 0, 0, 2, 1)
        self.stack.add_named(page, "local")

        # Command
        page, grid = self.protocol_page("Comando", "Esegue un comando locale dentro una tab terminale.")
        self.command_cmd = self.labeled_entry(grid, 0, "Comando locale", sess.command, "es. ping 8.8.8.8 oppure journalctl -f")
        self.stack.add_named(page, "command")

    def refresh_rdp_monitor_combo(self, requested: Optional[str] = None) -> None:
        """Aggiorna la lista monitor preservando anche una scelta scollegata."""
        if not hasattr(self, "rdp_monitor"):
            return
        selected = requested or self.rdp_monitor.get_active_id() or "current"
        self.rdp_monitor.remove_all()
        self.rdp_monitor.append("current", "Automatico — monitor corrente di FedTermHub")
        self.rdp_monitor.append("primary", "Monitor principale")
        descriptors = enumerate_monitors()
        known = {"current", "primary"}
        for descriptor in descriptors:
            self.rdp_monitor.append(descriptor.selector, descriptor.label)
            known.add(descriptor.selector)
        if selected not in known and selected.startswith("monitor:"):
            self.rdp_monitor.append(
                selected, "Monitor salvato — non collegato in questo momento"
            )
        if not self.rdp_monitor.set_active_id(selected):
            self.rdp_monitor.set_active_id("current")
        if not descriptors:
            self.rdp_monitor.set_tooltip_text(
                "Nessun monitor enumerato: verrà usato il monitor corrente al momento dell'apertura."
            )
        else:
            self.rdp_monitor.set_tooltip_text(
                f"Monitor rilevati: {len(descriptors)}. La scelta viene verificata di nuovo all'apertura."
            )

    def on_rdp_test_clicked(self, _button: Gtk.Button) -> None:
        use_gateway = (self.routing_mode.get_active_id() or "direct") == "rd_gateway"
        host = (
            self.rdp_gateway_host.get_text().strip()
            if use_gateway else self.rdp_host.get_text().strip()
        )
        port_text = (
            self.rdp_gateway_port.get_text().strip() or "443"
            if use_gateway else self.rdp_port.get_text().strip() or "3389"
        )
        if not host or not port_text.isdigit():
            self.rdp_test_status.set_text(
                "Inserisci gateway e porta validi" if use_gateway else "Inserisci host e porta validi"
            )
            return
        self.rdp_test_status.set_text(
            "Verifica RD Gateway in corso…" if use_gateway else "Verifica in corso…"
        )

        def worker() -> None:
            try:
                started = time.monotonic()
                with net_socket.create_connection((host, int(port_text)), timeout=3.5):
                    elapsed = int((time.monotonic() - started) * 1000)
                GLib.idle_add(self.rdp_test_status.set_text, f"✓ Porta raggiungibile ({elapsed} ms)")
            except Exception as exc:
                GLib.idle_add(self.rdp_test_status.set_text, f"✕ Non raggiungibile: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def refresh_serial_combo(self, current: str = "") -> None:
        ports = detect_serial_ports()
        self.serial_device.remove_all()
        if not ports:
            ports = [current or "/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyS0"]
        elif current and current not in ports:
            ports.insert(0, current)
        for port in ports:
            self.serial_device.append(port, port)
        set_combo_text(self.serial_device, current or ports[0])

    def on_protocol_changed(self, combo: Gtk.ComboBoxText) -> None:
        proto = combo.get_active_id() or "ssh"
        self.stack.set_visible_child_name(proto)
        uses_auth = proto in {"ssh", "rdp", "vnc", "sftp"}
        self.auth_box.set_visible(uses_auth)
        self.workflow_box.set_visible(proto in {"ssh", "rdp", "vnc", "sftp"})
        if proto in {"ssh", "rdp", "vnc", "sftp"}:
            self.refresh_routing_modes(proto)
        help_by_proto = {
            "ssh": "SSH: host, porta e utente. La password può arrivare dal Password Manager tramite sshpass, oppure puoi usare chiave SSH.",
            "rdp": "RDP nativo: PC/IP, utente, dominio e password. Il desktop viene disegnato direttamente da libfreerdp in GTK; la password può essere salvata nel Password Manager.",
            "vnc": "VNC: TigerVNC normalmente richiede la password nella sua finestra; il Password Manager resta utile per ricordarla.",
            "sftp": "SFTP: apre il client sftp nel terminale. Password automatica solo se sshpass è disponibile.",
            "serial": "Seriale: scegli una porta rilevata oppure scrivila manualmente. Se non hai permessi, aggiungi l'utente al gruppo dialout/uucp a seconda della distro.",
            "local": "Terminale locale: apre la shell predefinita di Fedora.",
            "command": "Comando: utile per scorciatoie come ping, tail, journalctl o script ricorrenti.",
        }
        self.help_text.set_text(help_by_proto.get(proto, ""))

    def get_session(self) -> Session:
        proto = self.protocol.get_active_id() or "ssh"
        name = self.name.get_text().strip() or "Senza nome"
        credential_id = self.credential.get_active_id() or ""
        notes = self.notes.get_text().strip()
        use_password = self.use_credential_password.get_active()
        routing_mode = self.routing_mode.get_active_id() or "direct"
        required_tunnel_id = (
            self.required_tunnel.get_active_id() or ""
        ) if routing_mode == "saved_tunnel" else ""
        wait_for_tunnel = self.wait_for_tunnel.get_active()
        jump_host = self.jump_host.get_text().strip() if routing_mode == "proxy_jump" else ""
        jump_port = (self.jump_port.get_text().strip() or "22") if routing_mode == "proxy_jump" else "22"
        jump_user = self.jump_user.get_text().strip() if routing_mode == "proxy_jump" else ""
        gateway_host = self.rdp_gateway_host.get_text().strip() if routing_mode == "rd_gateway" else ""
        gateway_port = (self.rdp_gateway_port.get_text().strip() or "443") if routing_mode == "rd_gateway" else "443"
        gateway_same = self.rdp_gateway_same_credentials.get_active()
        gateway_credential_id = self.rdp_gateway_credential.get_active_id() or ""
        gateway_user = self.rdp_gateway_user.get_text().strip()
        gateway_domain = self.rdp_gateway_domain.get_text().strip()
        gateway_transport = self.rdp_gateway_transport.get_active_id() or "auto"

        if proto == "ssh":
            return Session(
                name=name, protocol=proto, host=self.ssh_host.get_text().strip(),
                port=self.ssh_port.get_text().strip(), user=self.ssh_user.get_text().strip(),
                command=self.ssh_command.get_text().strip(), notes=notes,
                credential_id=credential_id, use_credential_password=use_password,
                routing_mode=routing_mode, required_tunnel_id=required_tunnel_id,
                wait_for_tunnel=wait_for_tunnel, jump_host=jump_host,
                jump_port=jump_port, jump_user=jump_user,
            )
        if proto == "rdp":
            return Session(
                name=name,
                protocol=proto,
                host=self.rdp_host.get_text().strip(),
                port=self.rdp_port.get_text().strip(),
                user=self.rdp_user.get_text().strip(),
                domain=self.rdp_domain.get_text().strip(),
                command=self.rdp_options.get_text().strip(),
                notes=notes,
                credential_id=credential_id,
                use_credential_password=use_password,
                rdp_mode=self.rdp_mode.get_active_id() or "native",
                rdp_clipboard=self.rdp_clipboard.get_active(),
                rdp_dynamic_resolution=self.rdp_dynamic_resolution.get_active(),
                rdp_fonts=self.rdp_fonts.get_active(),
                rdp_auto_reconnect=self.rdp_auto_reconnect.get_active(),
                rdp_cert_ignore=self.rdp_cert_ignore.get_active(),
                rdp_sound=self.rdp_sound.get_active(),
                rdp_multimon=self.rdp_multimon.get_active(),
                rdp_start_fullscreen=self.rdp_start_fullscreen.get_active(),
                rdp_monitor=self.rdp_monitor.get_active_id() or "current",
                rdp_window_mode=("detached" if self.rdp_detach_on_open.get_active() else "embedded"),
                rdp_admin=self.rdp_admin.get_active(),
                rdp_drive=self.rdp_drive.get_text().strip(),
                rdp_width=self.rdp_width.get_text().strip(),
                rdp_height=self.rdp_height.get_text().strip(),
                rdp_quality=self.rdp_quality.get_active_id() or "lan",
                rdp_grab_keyboard=self.rdp_grab_keyboard.get_active(),
                rdp_grab_mouse=self.rdp_grab_mouse.get_active(),
                rdp_keyboard_layout=(self.rdp_keyboard_custom.get_text().strip() if self.rdp_keyboard_layout.get_active_id() == "custom" else (self.rdp_keyboard_layout.get_active_id() or "auto")),
                rdp_smart_sizing=self.rdp_smart_sizing.get_active(),
                rdp_microphone=self.rdp_microphone.get_active(),
                rdp_printer=self.rdp_printer.get_active(),
                rdp_smartcard=self.rdp_smartcard.get_active(),
                rdp_gfx=self.rdp_gfx.get_active(),
                rdp_compression=self.rdp_compression.get_active(),
                rdp_scale=self.rdp_scale.get_active_id() or "100",
                rdp_timeout=self.rdp_timeout.get_text().strip() or "12000",
                rdp_security=self.rdp_security.get_active_id() or "auto",
                rdp_password_stdin=self.rdp_password_stdin.get_active(),
                routing_mode=routing_mode,
                required_tunnel_id=required_tunnel_id,
                wait_for_tunnel=wait_for_tunnel,
                rdp_gateway_host=gateway_host,
                rdp_gateway_port=gateway_port,
                rdp_gateway_user=gateway_user,
                rdp_gateway_domain=gateway_domain,
                rdp_gateway_credential_id=gateway_credential_id,
                rdp_gateway_use_same_credentials=gateway_same,
                rdp_gateway_transport=gateway_transport,
            )
        if proto == "vnc":
            return Session(
                name=name, protocol=proto, host=self.vnc_host.get_text().strip(),
                port=self.vnc_port.get_text().strip(), command=self.vnc_options.get_text().strip(),
                notes=notes, credential_id=credential_id, use_credential_password=use_password,
                routing_mode=routing_mode, required_tunnel_id=required_tunnel_id,
                wait_for_tunnel=wait_for_tunnel,
            )
        if proto == "sftp":
            return Session(
                name=name, protocol=proto, host=self.sftp_host.get_text().strip(),
                port=self.sftp_port.get_text().strip(), user=self.sftp_user.get_text().strip(),
                notes=notes, credential_id=credential_id, use_credential_password=use_password,
                routing_mode=routing_mode, required_tunnel_id=required_tunnel_id,
                wait_for_tunnel=wait_for_tunnel, jump_host=jump_host,
                jump_port=jump_port, jump_user=jump_user,
            )
        if proto == "serial":
            return Session(name=name, protocol=proto, host=get_combo_text(self.serial_device, "/dev/ttyUSB0"), port=get_combo_text(self.serial_baud, "115200"), notes=notes, serial_databits=self.serial_databits.get_active_id() or "8", serial_parity=self.serial_parity.get_active_id() or "N", serial_stopbits=self.serial_stopbits.get_active_id() or "1")
        if proto == "command":
            return Session(name=name, protocol=proto, command=self.command_cmd.get_text().strip(), notes=notes)
        return Session(name=name, protocol="local", notes=notes)

    def _validate_on_save(self, _dialog: Gtk.Dialog, response: int) -> None:
        if response != Gtk.ResponseType.OK:
            return
        problem = session_problem(self.get_session())
        if not problem:
            return
        self.stop_emission_by_name("response")
        message = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK,
            text="La sessione non è completa",
        )
        message.format_secondary_text(problem[:1].upper() + problem[1:] + ".")
        message.run()
        message.destroy()

    def get_transient_password(self) -> str:
        proto = self.protocol.get_active_id() or "ssh"
        if proto == "rdp" and hasattr(self, "rdp_password"):
            return self.rdp_password.get_text()
        return ""

    def should_offer_save_password(self) -> bool:
        proto = self.protocol.get_active_id() or "ssh"
        if proto == "rdp" and hasattr(self, "rdp_save_password"):
            return bool(self.rdp_save_password.get_active() and self.get_transient_password())
        return False


class TerminalTab(TerminalWorkspace):
    """Tab terminale: un workspace di riquadri divisibili.

    Eredita da :class:`TerminalWorkspace` (modulo ``terminal_panel``) e ne
    conserva l'interfaccia storica (``.terminal``, ``apply_preferences``,
    ``apply_terminal_theme``, ``.title``, ``fullscreen_active``) così che il
    resto dell'applicazione continui a funzionare senza modifiche.
    """


class CommandPalette(Gtk.Window):
    """Ricerca rapida delle sessioni in stile command palette.

    Si apre con Ctrl+K, filtra man mano che si digita su nome, host, utente e
    cartella, si naviga con le frecce e si connette con Invio. Pensata per
    lavorare senza mouse: due lettere e sei sul server.
    """

    def __init__(self, app_window: "AppWindow") -> None:
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.app_window = app_window
        self.set_transient_for(app_window)
        self.set_modal(True)
        self.set_decorated(False)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.set_default_size(560, 420)
        self.get_style_context().add_class("command-palette")

        # (session, primary, secondary) precalcolati una volta all'apertura.
        self._items: List[Tuple[Session, str, str]] = []
        try:
            for _path, sess, folder in app_window.store.iter_sessions():
                primary = sess.name or sess.host or "Sessione"
                bits = []
                if sess.protocol:
                    bits.append(sess.protocol.upper())
                if sess.host:
                    target = sess.host
                    if getattr(sess, "user", ""):
                        target = f"{sess.user}@{sess.host}"
                    bits.append(target)
                if folder:
                    bits.append(folder)
                self._items.append((sess, primary, "  ·  ".join(bits)))
        except Exception:
            self._items = []

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.get_style_context().add_class("command-palette-body")
        self.add(outer)

        self.entry = Gtk.SearchEntry()
        self.entry.set_placeholder_text("Cerca una sessione e premi Invio per connetterti…")
        self.entry.get_style_context().add_class("command-palette-entry")
        self.entry.connect("search-changed", lambda *_: self._refilter())
        self.entry.connect("activate", lambda *_: self._activate_selected())
        self.entry.connect("key-press-event", self._on_entry_key)
        outer.pack_start(self.entry, False, False, 0)

        # colonne: icona protocollo, testo primario, testo secondario, sessione
        self.model = Gtk.ListStore(str, str, str, object)
        self.view = Gtk.TreeView(model=self.model)
        self.view.set_headers_visible(False)
        self.view.get_style_context().add_class("command-palette-list")
        self.view.connect("row-activated", lambda *_: self._activate_selected())
        icon_renderer = Gtk.CellRendererPixbuf()
        icon_col = Gtk.TreeViewColumn("", icon_renderer, icon_name=0)
        self.view.append_column(icon_col)
        text_renderer = Gtk.CellRendererText()
        text_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        text_col = Gtk.TreeViewColumn("", text_renderer, markup=1)
        text_col.set_expand(True)
        self.view.append_column(text_col)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.view)
        outer.pack_start(scroller, True, True, 0)

        hint = Gtk.Label(xalign=0)
        hint.set_markup("<small>↑↓ per scorrere · Invio connette · Esc chiude</small>")
        hint.get_style_context().add_class("command-palette-hint")
        outer.pack_start(hint, False, False, 0)

        self.connect("key-press-event", self._on_window_key)
        self._refilter()

    @staticmethod
    def _score(query: str, sess: "Session", primary: str, secondary: str) -> int:
        """Punteggio semplice: prefisso del nome > sottostringa nome > altro."""
        haystack = f"{primary} {secondary}".casefold()
        q = query.casefold().strip()
        if not q:
            return 1
        name = (primary or "").casefold()
        if name.startswith(q):
            return 100
        if q in name:
            return 60
        if all(token in haystack for token in q.split()):
            return 30
        return 0

    def _refilter(self) -> None:
        query = self.entry.get_text()
        self.model.clear()
        scored = []
        for sess, primary, secondary in self._items:
            score = self._score(query, sess, primary, secondary)
            if score > 0:
                scored.append((score, primary, secondary, sess))
        scored.sort(key=lambda row: (-row[0], row[1].casefold()))
        icon_map = {"ssh": "utilities-terminal-symbolic", "sftp": "folder-remote-symbolic",
                    "rdp": "video-display-symbolic", "vnc": "video-display-symbolic"}
        for _score, primary, secondary, sess in scored[:200]:
            icon = icon_map.get((sess.protocol or "").lower(), "network-server-symbolic")
            safe_primary = GLib.markup_escape_text(primary)
            safe_secondary = GLib.markup_escape_text(secondary)
            markup = f"<b>{safe_primary}</b>"
            if safe_secondary:
                markup += f"\n<small>{safe_secondary}</small>"
            self.model.append([icon, markup, secondary, sess])
        if len(self.model) > 0:
            self.view.get_selection().select_path(Gtk.TreePath.new_first())

    def _move_selection(self, delta: int) -> None:
        count = len(self.model)
        if count == 0:
            return
        selection = self.view.get_selection()
        model, tree_iter = selection.get_selected()
        if tree_iter is None:
            index = 0
        else:
            index = self.model.get_path(tree_iter).get_indices()[0] + delta
        index = max(0, min(count - 1, index))
        path = Gtk.TreePath.new_from_indices([index])
        selection.select_path(path)
        self.view.scroll_to_cell(path, None, False, 0.0, 0.0)

    def _activate_selected(self) -> None:
        selection = self.view.get_selection()
        _model, tree_iter = selection.get_selected()
        if tree_iter is None:
            return
        sess = self.model[tree_iter][3]
        self.close()
        if sess is not None:
            GLib.idle_add(lambda: (self.app_window.open_session(sess), False)[1])

    def _on_entry_key(self, _widget, event) -> bool:  # noqa: ANN001
        if event.keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            self._move_selection(1 if event.keyval == Gdk.KEY_Down else -1)
            return True
        return False

    def _on_window_key(self, _widget, event) -> bool:  # noqa: ANN001
        if event.keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False


class TextInfoTab(Gtk.Box):
    def __init__(self, text: str, theme: str = "dark") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(text)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(view)
        self.pack_start(scroller, True, True, 0)


class ConnectionDiagnosticsDialog(Gtk.Dialog):
    """Finestra contestuale e non invasiva per la diagnostica di una sessione."""

    STATUS_ICONS = {
        "ok": "emblem-ok-symbolic",
        "warning": "dialog-warning-symbolic",
        "error": "dialog-error-symbolic",
        "info": "dialog-information-symbolic",
    }

    def __init__(self, parent: Gtk.Window, target: "DiagnosticTarget", timeout: float = 4.0) -> None:
        super().__init__(title="Diagnostica connessione", transient_for=parent, modal=False)
        self.set_default_size(720, 520)
        self.set_resizable(True)
        self.get_style_context().add_class("connection-diagnostics-dialog")
        self.target = target
        self.timeout = max(1.0, min(float(timeout), 20.0))
        self.report: Optional["DiagnosticReport"] = None
        self._closed = False
        self._running = False

        self.add_button("Chiudi", Gtk.ResponseType.CLOSE)
        self.connect("response", lambda dialog, _response: dialog.destroy())
        self.connect("destroy", self._on_destroy)

        content = self.get_content_area()
        content.set_spacing(12)
        content.set_border_width(16)

        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        heading = Gtk.Label(label="Diagnostica della connessione", xalign=0)
        heading.get_style_context().add_class("dialog-heading")
        target_label = Gtk.Label(
            label=f"{target.name}  •  {target.protocol.upper()}  •  {target.endpoint}",
            xalign=0,
        )
        target_label.set_selectable(True)
        target_label.get_style_context().add_class("muted")
        header.pack_start(heading, False, False, 0)
        header.pack_start(target_label, False, False, 0)
        content.pack_start(header, False, False, 0)

        self.progress_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.progress_box.get_style_context().add_class("diagnostic-progress")
        self.spinner = Gtk.Spinner()
        self.progress_label = Gtk.Label(label="Controllo DNS, route, VPN e porta TCP…", xalign=0)
        self.progress_box.pack_start(self.spinner, False, False, 0)
        self.progress_box.pack_start(self.progress_label, True, True, 0)
        content.pack_start(self.progress_box, False, False, 0)

        self.summary_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.summary_box.get_style_context().add_class("diagnostic-summary")
        self.summary_icon = Gtk.Image.new_from_icon_name("dialog-information-symbolic", Gtk.IconSize.DIALOG)
        self.summary_text = Gtk.Label(label="", xalign=0)
        self.summary_text.set_line_wrap(True)
        self.summary_text.set_selectable(True)
        self.summary_box.pack_start(self.summary_icon, False, False, 0)
        self.summary_box.pack_start(self.summary_text, True, True, 0)
        content.pack_start(self.summary_box, False, False, 0)
        self.summary_box.hide()

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        self.results = Gtk.ListBox()
        self.results.set_selection_mode(Gtk.SelectionMode.NONE)
        self.results.get_style_context().add_class("diagnostic-results")
        scroller.add(self.results)
        content.pack_start(scroller, True, True, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.retry_button = Gtk.Button.new_with_mnemonic("_Ripeti controllo")
        self.retry_button.set_image(Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON))
        self.retry_button.set_always_show_image(True)
        self.retry_button.connect("clicked", lambda *_: self.start())
        self.copy_button = Gtk.Button.new_with_mnemonic("_Copia report")
        self.copy_button.set_image(Gtk.Image.new_from_icon_name("edit-copy-symbolic", Gtk.IconSize.BUTTON))
        self.copy_button.set_always_show_image(True)
        self.copy_button.set_sensitive(False)
        self.copy_button.connect("clicked", self._copy_report)
        self.export_button = Gtk.Button.new_with_mnemonic("_Esporta…")
        self.export_button.set_image(Gtk.Image.new_from_icon_name("document-save-symbolic", Gtk.IconSize.BUTTON))
        self.export_button.set_always_show_image(True)
        self.export_button.set_sensitive(False)
        self.export_button.connect("clicked", self._export_report)
        actions.pack_start(self.retry_button, False, False, 0)
        actions.pack_end(self.export_button, False, False, 0)
        actions.pack_end(self.copy_button, False, False, 0)
        content.pack_start(actions, False, False, 0)

        self.show_all()
        self.summary_box.hide()
        GLib.idle_add(self.start)

    def _on_destroy(self, _dialog: Gtk.Dialog) -> None:
        self._closed = True

    def _clear_results(self) -> None:
        for row in list(self.results.get_children()):
            self.results.remove(row)

    def start(self) -> bool:
        if self._closed or self._running:
            return False
        if run_connection_diagnostics is None:
            self._show_internal_error("Modulo diagnostico non disponibile.")
            return False
        self._running = True
        self.report = None
        self._clear_results()
        self.summary_box.hide()
        self.progress_box.show_all()
        self.progress_label.set_text("Controllo DNS, route, VPN e porta TCP…")
        self.spinner.start()
        self.retry_button.set_sensitive(False)
        self.copy_button.set_sensitive(False)
        self.export_button.set_sensitive(False)

        def worker() -> None:
            try:
                report = run_connection_diagnostics(self.target, self.timeout)
                GLib.idle_add(self._show_report, report)
            except Exception as exc:
                GLib.idle_add(self._show_internal_error, str(exc))

        threading.Thread(target=worker, daemon=True, name="fedtermhub-connection-diagnostics").start()
        return False

    def _make_result_row(self, step) -> Gtk.ListBoxRow:  # noqa: ANN001
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.get_style_context().add_class("diagnostic-row")
        row.get_style_context().add_class("diagnostic-" + step.status)
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        outer.set_border_width(10)
        icon_name = self.STATUS_ICONS.get(step.status, "dialog-information-symbolic")
        icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.LARGE_TOOLBAR)
        outer.pack_start(icon, False, False, 0)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        title = Gtk.Label(xalign=0)
        title.set_markup(f"<b>{GLib.markup_escape_text(step.title)}</b>")
        summary = Gtk.Label(label=step.summary, xalign=0)
        summary.set_line_wrap(True)
        summary.set_selectable(True)
        text_box.pack_start(title, False, False, 0)
        text_box.pack_start(summary, False, False, 0)
        if step.details:
            expander = Gtk.Expander(label="Dettagli")
            details = Gtk.Label(label=step.details, xalign=0)
            details.set_line_wrap(True)
            details.set_selectable(True)
            details.set_margin_top(6)
            details.set_margin_start(8)
            expander.add(details)
            text_box.pack_start(expander, False, False, 0)
        outer.pack_start(text_box, True, True, 0)
        row.add(outer)
        return row

    def _show_report(self, report: "DiagnosticReport") -> bool:
        if self._closed:
            return False
        self._running = False
        self.report = report
        self.spinner.stop()
        self.progress_box.hide()
        self._clear_results()
        for step in report.steps:
            self.results.add(self._make_result_row(step))
        icon_name = self.STATUS_ICONS.get(report.outcome, "dialog-information-symbolic")
        self.summary_icon.set_from_icon_name(icon_name, Gtk.IconSize.DIALOG)
        self.summary_text.set_text(report.recommendation)
        summary_ctx = self.summary_box.get_style_context()
        for name in ("diagnostic-ok", "diagnostic-warning", "diagnostic-error", "diagnostic-info"):
            summary_ctx.remove_class(name)
        summary_ctx.add_class("diagnostic-" + report.outcome)
        self.summary_box.show_all()
        self.retry_button.set_sensitive(True)
        self.copy_button.set_sensitive(True)
        self.export_button.set_sensitive(True)
        self.results.show_all()
        return False

    def _show_internal_error(self, message: str) -> bool:
        if self._closed:
            return False
        self._running = False
        self.spinner.stop()
        self.progress_box.hide()
        self._clear_results()
        self.summary_icon.set_from_icon_name("dialog-error-symbolic", Gtk.IconSize.DIALOG)
        self.summary_text.set_text("Diagnostica non completata: " + message)
        self.summary_box.get_style_context().add_class("diagnostic-error")
        self.summary_box.show_all()
        self.retry_button.set_sensitive(True)
        return False

    def _copy_report(self, _button: Gtk.Button) -> None:
        if not self.report:
            return
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(self.report.to_text(), -1)
        self.progress_label.set_text("Rapporto copiato negli appunti.")
        self.progress_box.show_all()
        self.spinner.stop()
        GLib.timeout_add(2200, lambda: (self.progress_box.hide(), False)[1])

    def _export_report(self, _button: Gtk.Button) -> None:
        if not self.report:
            return
        chooser = Gtk.FileChooserDialog(
            title="Esporta diagnostica connessione",
            transient_for=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        chooser.add_buttons(
            "_Annulla", Gtk.ResponseType.CANCEL,
            "_Salva", Gtk.ResponseType.OK,
        )
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", self.target.name).strip("-") or "sessione"
        chooser.set_current_name(f"fedtermhub-diagnostica-{safe_name}.txt")
        chooser.set_do_overwrite_confirmation(True)
        try:
            if chooser.run() == Gtk.ResponseType.OK:
                atomic_write_text(Path(chooser.get_filename()), self.report.to_text())
        except Exception as exc:
            error = Gtk.MessageDialog(
                transient_for=self,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.CLOSE,
                text="Esportazione non riuscita",
            )
            error.format_secondary_text(str(exc))
            try:
                error.run()
            finally:
                error.destroy()
        finally:
            chooser.destroy()


class SessionErrorTab(Gtk.Box):
    """Tab leggibile per errori di connessione/sessione.

    La versione 1.3 evita che gli errori restino solo in console o facciano
    crashare il callback GTK: ogni errore può essere mostrato qui, copiato e
    salvato anche nel log applicativo.
    """

    def __init__(
        self,
        title: str,
        message: str,
        details: str = "",
        command: str = "",
        diagnostic_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.get_style_context().add_class("session-error-tab")
        self.set_border_width(18)
        heading = Gtk.Label(label=f"⚠ {title}", xalign=0)
        heading.get_style_context().add_class("dialog-heading")
        heading.set_line_wrap(True)
        self.pack_start(heading, False, False, 0)

        msg = Gtk.Label(label=message, xalign=0)
        msg.set_line_wrap(True)
        msg.set_selectable(True)
        self.pack_start(msg, False, False, 0)

        body = ""
        if command:
            body += "Comando eseguito:\n" + command + "\n\n"
        if details:
            body += "Dettagli tecnici:\n" + details.strip() + "\n"
        if not body:
            body = "Nessun dettaglio tecnico disponibile. Controlla anche ~/.cache/fedtermhub/fedtermhub.log"

        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(body)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(view)
        self.pack_start(scroller, True, True, 0)

        if diagnostic_callback is not None:
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            actions.get_style_context().add_class("session-error-actions")
            diagnostic_button = Gtk.Button.new_with_mnemonic("_Diagnostica connessione")
            diagnostic_button.set_image(
                Gtk.Image.new_from_icon_name("network-transmit-receive-symbolic", Gtk.IconSize.BUTTON)
            )
            diagnostic_button.set_always_show_image(True)
            diagnostic_button.set_tooltip_text("Controlla DNS, route, VPN e porta senza usare le credenziali")
            diagnostic_button.connect("clicked", lambda *_: diagnostic_callback())
            actions.pack_start(diagnostic_button, False, False, 0)
            self.pack_start(actions, False, False, 0)


class SplitTerminalTab(TerminalTab):
    """Tab locale che nasce già divisa in due riquadri affiancati.

    Ora è un normale workspace a riquadri: l'utente può dividere ancora,
    chiudere riquadri e navigare fra loro con le scorciatoie. ``children_tabs``
    resta esposto per compatibilità con MultiExec.
    """

    def __init__(self, theme: str = "dark", settings: Optional[AppSettings] = None) -> None:
        preferences = settings or AppSettings(theme=theme)
        configured_shell = preferences.terminal_shell.strip()
        shell = (
            configured_shell
            if configured_shell and Path(configured_shell).exists()
            else (os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh")
        )
        cwd = preferences.terminal_start_directory.strip() or None
        super().__init__("Split", [shell], cwd=cwd, theme=theme, settings=preferences)
        # Divisione iniziale affiancata, una volta che il workspace è pronto.
        GLib.idle_add(lambda: (self.split(Gtk.Orientation.HORIZONTAL), False)[1])

    @property
    def children_tabs(self) -> List["TerminalPane"]:
        return list(self.panes)


class RdpTab(Gtk.Box):
    """Workspace RDP incorporato con ciclo di vita deterministico.

    Il desktop remoto resta sempre mappato: il pannello log è un drawer separato
    e non sostituisce mai il Gtk.Socket. Questo evita il race storico per cui la
    connessione sembrava avviarsi soltanto aprendo il log.
    """

    START_RETRY_MS = 120
    START_MAX_RETRIES = 50
    EMBED_TIMEOUT_MS = 18000
    EMBED_PROBE_MS = 350
    MANUAL_REPARENT_AFTER_MS = 1800
    EMBED_MAX_WAIT_MS = 65000

    def __init__(
        self,
        title: str,
        argv: List[str],
        masked_command: str,
        embedded: bool,
        theme: str = "dark",
        error_callback: Optional[Callable[[str, str, str, str], None]] = None,
        stdin_secret: Optional[str] = None,
        use_args_fd: bool = False,
        state_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.title = title
        self.argv = list(argv)
        self.masked_command = masked_command
        self.embedded = embedded
        self.stdin_secret = stdin_secret
        self.use_args_fd = bool(use_args_fd and stdin_secret is not None)
        self.credential_transport = "args-from memfd" if self.use_args_fd else ("legacy argv" if stdin_secret is not None else "non necessario")
        self.error_callback = error_callback
        self.state_callback = state_callback

        self.process: Optional[subprocess.Popen] = None
        self.socket: Optional[Gtk.Widget] = None
        self.socket_xid = 0
        self.client_window_xid = 0
        self.embed_method = "non collegato"
        self.embed_token = f"FedTermHub-RDP-{uuid.uuid4().hex[:12]}"
        self.keyboard_active = False
        self.log_lines: List[str] = []
        self.last_error_notified = False

        self._launch_requested = False
        self._launching = False
        self._started = False
        self._stopping = False
        self._destroyed = False
        self._plug_seen = False
        self._start_attempts = 0
        self._start_source_id: Optional[int] = None
        self._poll_id: Optional[int] = None
        self._watchdog_id: Optional[int] = None
        self._embed_probe_id: Optional[int] = None
        self._process_started_at = 0.0
        self._last_reparent_scan_at = 0.0
        self._manual_reparent_attempted = False
        self._focus_retry_ids: List[int] = []
        self._reader_threads: List[threading.Thread] = []
        self._pty_master_fd: Optional[int] = None
        self._secret_sent = False
        self._secure_args_fd_active = False
        self._watchdog_extensions = 0
        self._last_output_at = 0.0
        self._has_ever_started = False
        self._has_ever_embedded = False
        self._gdi_ready_seen = False
        self._bump_options_seen = False
        self._compatibility_mode = False
        self._admin_retry = False
        self._screen_refresh_ids: List[int] = []
        self._bump_notice_id: Optional[int] = None
        self._base_argv = list(argv)
        self._base_masked_command = masked_command

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.get_style_context().add_class("rdp-toolbar")
        self.state_dot = Gtk.Label(label="●")
        self.state_dot.get_style_context().add_class("status-dot-pending")
        header.pack_start(self.state_dot, False, False, 0)

        self.spinner = Gtk.Spinner()
        self.spinner.start()
        header.pack_start(self.spinner, False, False, 0)

        self.state = Gtk.Label(label="Preparazione area RDP…", xalign=0)
        self.state.get_style_context().add_class("workspace-hint")
        header.pack_start(self.state, True, True, 0)

        actions = [
            ("Attiva tastiera", "input-keyboard-symbolic", self.on_focus_clicked),
            ("Aggiorna schermo", "view-refresh-symbolic", self.on_redraw_clicked),
            ("Recupera finestra", "view-restore-symbolic", self.on_recover_clicked),
            ("Riconnetti", "system-reboot-symbolic", self.on_restart_clicked),
            ("Ferma", "media-playback-stop-symbolic", self.on_stop_clicked),
            ("Diagnostica", "dialog-information-symbolic", self.on_diagnostics_clicked),
            ("Esporta log", "document-save-symbolic", self.on_export_log_clicked),
        ]
        for label, icon, callback in actions:
            button = Gtk.Button()
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.set_tooltip_text(label)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            box.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU), False, False, 0)
            box.pack_start(Gtk.Label(label=label), False, False, 0)
            button.add(box)
            button.get_style_context().add_class("mini-action")
            button.connect("clicked", callback)
            header.pack_start(button, False, False, 0)

        self.log_toggle = Gtk.ToggleButton()
        self.log_toggle.set_relief(Gtk.ReliefStyle.NONE)
        self.log_toggle.set_tooltip_text("Mostra o nascondi il log interno")
        log_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        log_box.pack_start(Gtk.Image.new_from_icon_name("text-x-generic-symbolic", Gtk.IconSize.MENU), False, False, 0)
        self.log_toggle_label = Gtk.Label(label="Log")
        log_box.pack_start(self.log_toggle_label, False, False, 0)
        self.log_toggle.add(log_box)
        self.log_toggle.get_style_context().add_class("mini-action")
        self.log_toggle.connect("toggled", self.on_log_toggled)
        header.pack_start(self.log_toggle, False, False, 0)
        self.pack_start(header, False, False, 0)

        # Il canvas RDP è sempre presente e sempre mappato. Il log viene mostrato
        # sotto con un Revealer, senza mai nascondere o smontare Gtk.Socket.
        self.workspace = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.pack_start(self.workspace, True, True, 0)

        self.overlay = Gtk.Overlay()
        self.overlay.set_hexpand(True)
        self.overlay.set_vexpand(True)
        self.workspace.pack_start(self.overlay, True, True, 0)

        self.status_revealer = Gtk.Revealer()
        self.status_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.status_revealer.set_transition_duration(160)
        self.status_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.status_card.set_halign(Gtk.Align.CENTER)
        self.status_card.set_valign(Gtk.Align.CENTER)
        self.status_card.set_size_request(360, -1)
        self.status_card.get_style_context().add_class("rdp-status-card")
        self.status_title = Gtk.Label(label="Preparazione desktop remoto")
        self.status_title.get_style_context().add_class("panel-title")
        self.status_message = Gtk.Label(label="Attendo che l’area grafica sia pronta…")
        self.status_message.set_line_wrap(True)
        self.status_message.set_justify(Gtk.Justification.CENTER)
        self.status_card.pack_start(self.status_title, False, False, 0)
        self.status_card.pack_start(self.status_message, False, False, 0)
        self.status_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.status_actions.set_halign(Gtk.Align.CENTER)
        self.compat_retry_button = Gtk.Button.new_with_mnemonic("Riprova modalità _compatibile")
        self.compat_retry_button.get_style_context().add_class("suggested-action")
        self.compat_retry_button.connect("clicked", self.on_compat_retry_clicked)
        self.admin_retry_button = Gtk.Button.new_with_mnemonic("Riprova sessione _amministrativa")
        self.admin_retry_button.connect("clicked", self.on_admin_retry_clicked)
        self.status_actions.pack_start(self.compat_retry_button, False, False, 0)
        self.status_actions.pack_start(self.admin_retry_button, False, False, 0)
        self.status_card.pack_start(self.status_actions, False, False, 4)
        self.status_actions.set_no_show_all(True)
        self.status_actions.hide()
        self.status_revealer.add(self.status_card)
        self.status_revealer.set_reveal_child(True)
        self.overlay.add_overlay(self.status_revealer)

        if embedded and hasattr(Gtk, "Socket"):
            self.socket = Gtk.Socket()
            self.socket.set_hexpand(True)
            self.socket.set_vexpand(True)
            self.socket.set_can_focus(True)
            self.socket.add_events(
                Gdk.EventMask.BUTTON_PRESS_MASK
                | Gdk.EventMask.FOCUS_CHANGE_MASK
                | Gdk.EventMask.ENTER_NOTIFY_MASK
            )
            self.socket.get_style_context().add_class("rdp-canvas")
            self.socket.connect("realize", self.on_socket_realized)
            self.socket.connect("map", self.on_socket_mapped)
            self.socket.connect("size-allocate", self.on_socket_size_allocate)
            self.socket.connect("plug-added", self.on_plug_added)
            self.socket.connect("plug-removed", self.on_plug_removed)
            self.socket.connect("button-press-event", self.on_socket_button)
            self.socket.connect("focus-in-event", self.on_socket_focus_in)
            self.socket.connect("enter-notify-event", self.on_socket_enter)

            socket_frame = Gtk.Frame()
            socket_frame.set_shadow_type(Gtk.ShadowType.NONE)
            socket_frame.get_style_context().add_class("rdp-frame")
            socket_frame.add(self.socket)
            self.overlay.add(socket_frame)

            self.focus_hint = Gtk.Revealer()
            self.focus_hint.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
            hint_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            hint_box.set_halign(Gtk.Align.CENTER)
            hint_box.set_valign(Gtk.Align.START)
            hint_box.get_style_context().add_class("rdp-focus-hint")
            hint_box.pack_start(Gtk.Image.new_from_icon_name("input-keyboard-symbolic", Gtk.IconSize.MENU), False, False, 0)
            hint_box.pack_start(Gtk.Label(label="Clicca nel desktop remoto per attivare la tastiera"), False, False, 0)
            self.focus_hint.add(hint_box)
            self.focus_hint.set_reveal_child(False)
            self.overlay.add_overlay(self.focus_hint)
        else:
            placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=24)
            placeholder.get_style_context().add_class("empty-state")
            title_lbl = Gtk.Label(label="RDP integrato non disponibile", xalign=0)
            title_lbl.get_style_context().add_class("panel-title")
            msg = Gtk.Label(
                label=(
                    "Per incorporare FreeRDP servono il client X11 xfreerdp e XWayland.\n\n"
                    "Avvia FedTermHub con GDK_BACKEND=x11 e verifica il risultato di fedtermhub --doctor."
                ),
                xalign=0,
            )
            msg.set_selectable(True)
            msg.set_line_wrap(True)
            placeholder.pack_start(title_lbl, False, False, 0)
            placeholder.pack_start(msg, False, False, 0)
            self.overlay.add(placeholder)
            self._set_state("error", "RDP integrato non disponibile")
            self._show_status("RDP integrato non disponibile", "Gtk.Socket/XEmbed non è disponibile.", error=True)

        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.get_style_context().add_class("log-view")
        self.log_buffer = self.log_view.get_buffer()
        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        log_scroll.set_min_content_height(150)
        log_scroll.set_max_content_height(320)
        log_scroll.add(self.log_view)

        log_frame = Gtk.Frame()
        log_frame.set_shadow_type(Gtk.ShadowType.NONE)
        log_frame.get_style_context().add_class("rdp-log-drawer")
        log_frame.add(log_scroll)

        self.log_revealer = Gtk.Revealer()
        self.log_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.log_revealer.set_transition_duration(160)
        self.log_revealer.add(log_frame)
        self.log_revealer.set_reveal_child(False)
        self.workspace.pack_end(self.log_revealer, False, True, 0)

        self.append_log("Comando RDP: " + masked_command + "\n")
        self.connect("map", self.on_tab_mapped)
        self.connect("destroy", self.on_destroyed)

    def _set_state(self, state: str, text: str) -> None:
        self.state.set_text(text)
        if self.state_callback is not None:
            try:
                self.state_callback(f"{self.title}: {text}")
            except Exception:
                pass
        ctx = self.state_dot.get_style_context()
        for cls in ["status-dot-pending", "status-dot-ok", "status-dot-error", "status-dot-off"]:
            ctx.remove_class(cls)
        ctx.add_class({"ok": "status-dot-ok", "error": "status-dot-error", "off": "status-dot-off"}.get(state, "status-dot-pending"))
        if state == "pending":
            self.spinner.start()
            self.spinner.show()
        else:
            self.spinner.stop()
            self.spinner.hide()

    def _show_status(self, title: str, message: str, error: bool = False, bottom: bool = False) -> None:
        self.status_title.set_text(title)
        self.status_message.set_text(message)
        self.status_card.set_valign(Gtk.Align.END if bottom else Gtk.Align.CENTER)
        self.status_card.set_margin_bottom(18 if bottom else 0)
        ctx = self.status_card.get_style_context()
        ctx.remove_class("rdp-status-error")
        if error:
            ctx.add_class("rdp-status-error")
        self.status_revealer.set_reveal_child(True)

    def _hide_status(self) -> None:
        self.status_revealer.set_reveal_child(False)
        self._set_status_actions(False)

    def _set_status_actions(self, visible: bool) -> None:
        if not hasattr(self, "status_actions"):
            return
        self.status_actions.set_no_show_all(not visible)
        if visible:
            self.status_actions.show_all()
        else:
            self.status_actions.hide()

    def on_tab_mapped(self, _widget: Gtk.Widget) -> None:
        self.request_start()

    def on_socket_realized(self, _socket: Gtk.Widget) -> None:
        self._refresh_socket_xid()
        self._schedule_start()

    def on_socket_mapped(self, _socket: Gtk.Widget) -> None:
        self._refresh_socket_xid()
        self._schedule_start()

    def on_socket_size_allocate(self, _socket: Gtk.Widget, allocation) -> None:  # noqa: ANN001
        if allocation.width >= 64 and allocation.height >= 64:
            self._schedule_start()
            if self.client_window_xid > 0 and self._plug_seen:
                X11EmbedBridge.resize(self.client_window_xid, allocation.width, allocation.height)

    def _refresh_socket_xid(self) -> int:
        try:
            self.socket_xid = int(self.socket.get_id()) if self.socket is not None and self.socket.get_realized() else 0
        except Exception:
            self.socket_xid = 0
        return self.socket_xid

    def request_start(self) -> bool:
        """Richiede una sola partenza per ciclo di connessione.

        La richiesta resta sospesa finché la tab e il Gtk.Socket non sono davvero
        mappati. Non viene mostrato alcun falso errore mentre la scheda è nascosta.
        """
        if self._destroyed or not self.embedded:
            return False
        if self.process is not None and self.process.poll() is None:
            return False
        if self._started or self._launching or self._launch_requested:
            return False
        self._launch_requested = True
        self._start_attempts = 0
        self._set_state("pending", "Preparazione connessione RDP…")
        self._show_status("Connessione RDP", "Preparazione dell’area grafica interna…")
        self._schedule_start()
        return False

    def _schedule_start(self, delay_ms: int = 0) -> None:
        if self._destroyed or self._started or self._launching or not self._launch_requested:
            return
        if self._start_source_id is not None:
            return
        if delay_ms <= 0:
            self._start_source_id = GLib.idle_add(self._try_start)
        else:
            self._start_source_id = GLib.timeout_add(delay_ms, self._try_start)

    def _try_start(self) -> bool:
        self._start_source_id = None
        if self._destroyed or self._started or self._launching or not self._launch_requested:
            return False
        if self.process is not None and self.process.poll() is None:
            self._launch_requested = False
            return False
        if self.socket is None:
            self._fail_start("Gtk.Socket non disponibile", "Il backend grafico non supporta XEmbed.")
            return False

        # Una tab non visibile può non avere ancora un XID. Restiamo in attesa
        # degli eventi map/realize anziché far scadere un timeout e poi partire
        # quando l'utente apre la scheda.
        if not self.get_mapped() or not self.socket.get_mapped():
            self._set_state("pending", "In attesa che la scheda RDP sia visibile…")
            return False

        self._start_attempts += 1
        allocation = self.socket.get_allocation()
        ready = (
            self.socket.get_realized()
            and allocation.width >= 64
            and allocation.height >= 64
            and self._refresh_socket_xid() > 0
        )
        if not ready:
            if self._start_attempts >= self.START_MAX_RETRIES:
                self._fail_start(
                    "Area RDP non pronta",
                    "Il contenitore X11 è visibile ma non ha ottenuto un XID valido. "
                    "Verifica XWayland oppure esegui fedtermhub --doctor.",
                )
                return False
            self._schedule_start(self.START_RETRY_MS)
            return False

        self._start_process()
        return False

    def _build_secure_args_fd(self, argv: List[str], password: str) -> Tuple[int, List[str]]:
        """Prepara tutti gli argomenti FreeRDP in un memfd anonimo.

        FreeRDP legge un argomento per riga tramite /args-from:fd. In questo modo
        la password non compare in /proc/<pid>/cmdline e non dipendiamo dal prompt
        interattivo /from-stdin, che in varie release 3.x ha avuto regressioni.
        """
        if not hasattr(os, "memfd_create"):
            raise RuntimeError("Il kernel/Python non espone os.memfd_create")
        if "\n" in password or "\r" in password:
            raise ValueError("La password contiene un ritorno a capo non supportato da FreeRDP")
        fd = os.memfd_create("fedtermhub-rdp-args", flags=0)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        freerdp_args = list(argv[1:])
        freerdp_args.append(f"/p:{password}")
        payload = ("\n".join(freerdp_args) + "\n").encode("utf-8", errors="surrogatepass")
        os.write(fd, payload)
        os.lseek(fd, 0, os.SEEK_SET)
        launch_argv = [argv[0], f"/args-from:fd:{fd}"]
        return fd, launch_argv

    def _open_secret_pty(self) -> Tuple[int, int]:
        """Fallback storico per vecchie build senza /args-from:fd."""
        master_fd, slave_fd = pty.openpty()
        try:
            attrs = termios.tcgetattr(slave_fd)
            attrs[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
        except Exception:
            pass
        os.set_inheritable(slave_fd, True)
        return master_fd, slave_fd

    def _send_secret_to_pty(self, secret: str) -> None:
        if self._pty_master_fd is None or self._secret_sent or self._destroyed:
            return
        try:
            payload = (secret + "\n").encode("utf-8", errors="surrogatepass")
            os.write(self._pty_master_fd, payload)
            self._secret_sent = True
            GLib.idle_add(self.append_log, "Credenziale inviata tramite fallback PTY.\n")
        except Exception as exc:
            GLib.idle_add(self.append_log, f"ERRORE invio credenziale PTY: {exc}\n")

    def _close_secret_pty(self) -> None:
        fd = self._pty_master_fd
        self._pty_master_fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _start_process(self) -> None:
        if self._destroyed or self._started or self._launching:
            return
        self._launching = True
        self._launch_requested = False
        self._stopping = False
        self._plug_seen = False
        self._secret_sent = False
        self._gdi_ready_seen = False
        self._bump_options_seen = False
        if self._bump_notice_id is not None:
            try:
                GLib.source_remove(self._bump_notice_id)
            except Exception:
                pass
            self._bump_notice_id = None
        self._set_status_actions(False)
        self._close_secret_pty()
        self._set_state("pending", "Avvio FreeRDP…")
        self._show_status("Connessione RDP", "Avvio del client FreeRDP e autenticazione in corso…")
        slave_fd: Optional[int] = None
        secure_fd: Optional[int] = None
        pass_fds: Tuple[int, ...] = ()
        try:
            argv = list(self.argv)
            if not any(arg.startswith("/t:") for arg in argv):
                argv.append(f"/t:{self.embed_token}")
            if os.environ.get("FEDTERMHUB_RDP_SKIP_PARENT_WINDOW") != "1":
                argv.append(f"/parent-window:{self.socket_xid}")
                self.append_log(f"Avvio FreeRDP con parent-window XID={self.socket_xid}.\n")
            else:
                self.append_log("Modalità test: /parent-window omesso; verrà usato il reparenting X11.\n")

            stdin_target: Union[int, object] = subprocess.DEVNULL
            launch_argv = argv
            if self.stdin_secret is not None and self.use_args_fd:
                secure_fd, launch_argv = self._build_secure_args_fd(argv, self.stdin_secret)
                pass_fds = (secure_fd,)
                self._secure_args_fd_active = True
                self.credential_transport = "memfd + /args-from:fd"
                self.append_log("Credenziali e opzioni preparate in memfd anonimo protetto; password assente da argv.\n")
            elif self.stdin_secret is not None:
                # Compatibilità esclusivamente per vecchie build. Le nuove sessioni
                # usano sempre args-from:fd.
                self._pty_master_fd, slave_fd = self._open_secret_pty()
                stdin_target = slave_fd
                self.credential_transport = "fallback PTY"

            child_env = dict(os.environ)
            child_env.setdefault("LC_ALL", "C")
            self.process = subprocess.Popen(
                launch_argv,
                stdin=stdin_target,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
                close_fds=True,
                pass_fds=pass_fds,
                env=child_env,
            )
            if secure_fd is not None:
                os.close(secure_fd)
                secure_fd = None
                self._secure_args_fd_active = False
            if slave_fd is not None:
                os.close(slave_fd)
                slave_fd = None
            self._started = True
            self._has_ever_started = True
            self._launching = False
            self._process_started_at = time.monotonic()
            self._last_output_at = self._process_started_at
            self._last_reparent_scan_at = 0.0
            self._manual_reparent_attempted = False
            self._watchdog_extensions = 0
            self.client_window_xid = 0
            self.embed_method = "in attesa"
            if self.stdin_secret is not None and not self.use_args_fd:
                # Fallback vecchio: un piccolo ritardo riduce la possibilità che
                # FreeRDP svuoti il terminale dopo un invio troppo precoce.
                GLib.timeout_add(350, lambda: (self._send_secret_to_pty(self.stdin_secret or ""), False)[1])
            self._start_reader_threads()
            self._poll_id = GLib.timeout_add(250, self._poll_process)
            self._embed_probe_id = GLib.timeout_add(self.EMBED_PROBE_MS, self._embed_probe)
            self._watchdog_id = GLib.timeout_add(self.EMBED_TIMEOUT_MS, self._embed_watchdog)
        except Exception as exc:
            if secure_fd is not None:
                try:
                    os.close(secure_fd)
                except OSError:
                    pass
            if slave_fd is not None:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            self._secure_args_fd_active = False
            self._close_secret_pty()
            self._launching = False
            self._started = False
            details = traceback.format_exc()
            self.append_log(f"ERRORE avvio RDP: {exc}\n{details}\n")
            self._fail_start("Errore avvio RDP", str(exc), details)

    def _start_reader_threads(self) -> None:
        if self.process is None:
            return
        for stream, prefix in ((self.process.stdout, "OUT"), (self.process.stderr, "ERR")):
            if stream is None:
                continue
            thread = threading.Thread(target=self._reader_worker, args=(stream, prefix), daemon=True)
            self._reader_threads.append(thread)
            thread.start()

    def _reader_worker(self, stream, prefix: str) -> None:  # noqa: ANN001
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                self._last_output_at = time.monotonic()
                GLib.idle_add(self._handle_freerdp_line, prefix, line)
        except Exception as exc:
            GLib.idle_add(self.append_log, f"[LOG] Errore lettura {prefix}: {exc}\n")
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _handle_freerdp_line(self, prefix: str, line: str) -> bool:
        """Aggiorna log e stato semantico senza bloccare i thread di lettura."""
        self.append_log(f"[{prefix}] {line}")
        low = line.lower()
        if "gdi_init_ex" in low or "remote framebuffer format" in low:
            self._gdi_ready_seen = True
        if "logon_msg_bump_options" in low:
            first = not self._bump_options_seen
            self._bump_options_seen = True
            if first:
                self.append_log(
                    "[FedTermHub] Windows segnala una sessione già attiva e richiede una scelta di subentro. "
                    "Questo stato può lasciare xfreerdp su un framebuffer nero.\n"
                )
                self._set_state("pending", "Windows attende conferma sulla sessione esistente")
                self._show_status(
                    "Sessione Windows già attiva",
                    "Il server ha richiesto la schermata di scelta per subentrare a una sessione già aperta "
                    "(LOGON_MSG_BUMP_OPTIONS). Se resta nero, prova la modalità compatibile; su un server "
                    "Windows puoi anche tentare la sessione amministrativa.",
                    error=False,
                    bottom=True,
                )
                self._set_status_actions(True)
                if self._bump_notice_id is None:
                    self._bump_notice_id = GLib.timeout_add(4500, self._bump_black_screen_notice)
        return False

    def _bump_black_screen_notice(self) -> bool:
        self._bump_notice_id = None
        if self._destroyed or not self._bump_options_seen:
            return False
        if self.process is None or self.process.poll() is not None:
            return False
        self.refresh_framebuffer(silent=True)
        black = X11EmbedBridge.is_probably_black(self.client_window_xid)
        if black is False:
            self.append_log(
                "[FedTermHub] LOGON_MSG_BUMP_OPTIONS rilevato, ma il framebuffer contiene immagine: "
                "il prompt Windows dovrebbe essere visibile e interattivo.\n"
            )
            self._set_state("pending", "Windows attende una scelta sulla sessione esistente")
            self._hide_status()
            return False
        if black is True and not self._compatibility_mode:
            self.append_log(
                "[FedTermHub] Framebuffer ancora nero dopo il ridisegno; avvio automatico del profilo "
                "compatibile senza GFX e dynamic-resolution.\n"
            )
            self.retry_compatibility(admin=False)
            return False
        self._set_state("pending", "Connesso, ma Windows attende una scelta di sessione")
        detail = (
            "Il framebuffer è ancora completamente nero anche nel profilo compatibile. "
            if black is True else
            "Non è stato possibile campionare il framebuffer X11. "
        )
        self._show_status(
            "Schermata di subentro Windows non visibile",
            detail +
            "La rete, l’autenticazione e XEmbed hanno funzionato, ma il server sta aspettando una scelta "
            "relativa a una sessione Windows già attiva. Puoi tentare la sessione amministrativa oppure "
            "chiudere/disconnettere la sessione Windows già aperta e riconnettere.",
            error=False,
            bottom=True,
        )
        self._set_status_actions(True)
        return False

    def _mark_embedded(self, client_xid: int, method: str) -> None:
        """Conferma l'incorporamento sia nativo XEmbed sia manuale X11."""
        if self._destroyed or client_xid <= 0:
            return
        first = not self._plug_seen
        self.client_window_xid = int(client_xid)
        self.embed_method = method
        self._plug_seen = True
        self._has_ever_embedded = True
        if self._watchdog_id is not None:
            try:
                GLib.source_remove(self._watchdog_id)
            except Exception:
                pass
            self._watchdog_id = None
        if self._embed_probe_id is not None:
            try:
                GLib.source_remove(self._embed_probe_id)
            except Exception:
                pass
            self._embed_probe_id = None
        allocation = self.socket.get_allocation() if self.socket is not None else None
        if allocation is not None:
            X11EmbedBridge.resize(self.client_window_xid, allocation.width, allocation.height)
        if first:
            self.append_log(f"FreeRDP incorporato con metodo: {method}; window XID={client_xid}.\n")
        if self._bump_options_seen:
            self._set_state("pending", "Windows attende conferma sulla sessione esistente")
            self._show_status(
                "Sessione Windows già attiva",
                "La finestra RDP è incorporata, ma il server richiede una scelta di subentro. "
                "Se il desktop resta nero usa una delle azioni qui sotto.",
                error=False,
                bottom=True,
            )
            self._set_status_actions(True)
        else:
            self._set_state("ok", "Connesso")
            self._hide_status()
        if hasattr(self, "focus_hint"):
            self.focus_hint.set_reveal_child(True)
        for delay in (100, 350, 800):
            self._schedule_focus_retry(delay)
        for delay in (120, 500, 1200):
            self._schedule_screen_refresh(delay)

    def _embed_probe(self) -> bool:
        """Verifica l'albero X11 e recupera le build FreeRDP con XEmbed incompleto."""
        if self._destroyed or self.process is None or self.process.poll() is not None:
            self._embed_probe_id = None
            return False
        if self._plug_seen:
            self._embed_probe_id = None
            return False
        if self.socket is None or not self.socket_xid:
            self._refresh_socket_xid()
        if not self.socket_xid:
            return True

        # Primo percorso: /parent-window ha funzionato, ma Gtk.Socket non ha
        # ricevuto plug-added. La presenza di un figlio X11 è prova sufficiente.
        direct = X11EmbedBridge.direct_client(self.socket_xid, self.process.pid)
        if direct:
            self._mark_embedded(direct, "figlio X11 rilevato")
            return False

        # Secondo percorso: la finestra è stata creata top-level. Dopo un breve
        # margine per l'autenticazione la troviamo tramite PID/titolo e la
        # riparentiamo nel contenitore, senza aprire finestre esterne permanenti.
        now = time.monotonic()
        elapsed_ms = int((now - self._process_started_at) * 1000)
        if elapsed_ms >= self.MANUAL_REPARENT_AFTER_MS and (now - self._last_reparent_scan_at) >= 0.9:
            self._last_reparent_scan_at = now
            candidate = X11EmbedBridge.find_process_window(
                self.process.pid, self.embed_token, self.socket_xid
            )
            if candidate:
                allocation = self.socket.get_allocation()
                self._manual_reparent_attempted = True
                self.append_log(
                    f"Compatibilità: trovata finestra FreeRDP XID={candidate}; "
                    "tentativo di reparenting X11.\n"
                )
                if X11EmbedBridge.reparent(
                    candidate, self.socket_xid, allocation.width, allocation.height
                ):
                    self._mark_embedded(candidate, "reparenting X11 automatico")
                    return False
                self.append_log("Compatibilità: reparenting X11 non riuscito, nuovo tentativo in corso.\n")
        return True

    def _embed_watchdog(self) -> bool:
        self._watchdog_id = None
        if self._destroyed or self._plug_seen or self.process is None:
            return False
        rc = self.process.poll()
        if rc is not None:
            return False

        direct = X11EmbedBridge.direct_client(self.socket_xid, self.process.pid)
        if direct:
            self._mark_embedded(direct, "figlio X11 rilevato al watchdog")
            return False
        candidate = X11EmbedBridge.find_process_window(
            self.process.pid, self.embed_token, self.socket_xid
        )
        if candidate and self.socket is not None:
            allocation = self.socket.get_allocation()
            if X11EmbedBridge.reparent(candidate, self.socket_xid, allocation.width, allocation.height):
                self._mark_embedded(candidate, "reparenting X11 di recupero")
                return False

        elapsed_ms = int((time.monotonic() - self._process_started_at) * 1000)
        quiet_ms = int((time.monotonic() - self._last_output_at) * 1000)
        if elapsed_ms < self.EMBED_MAX_WAIT_MS:
            self._watchdog_extensions += 1
            self._set_state("pending", "Autenticazione RDP ancora in corso…")
            self._show_status(
                "Connessione RDP in corso",
                "FreeRDP è attivo ma il desktop non è stato ancora creato. "
                "FedTermHub continua automaticamente ad attendere e cercare la finestra.",
                error=False,
            )
            self.append_log(
                f"Watchdog: nessuna finestra dopo {elapsed_ms // 1000}s; "
                f"ultimo output {quiet_ms // 1000}s fa. Attesa prolungata.\n"
            )
            self._watchdog_id = GLib.timeout_add(12000, self._embed_watchdog)
            return False

        message = (
            "FreeRDP è rimasto attivo ma non ha creato una finestra grafica entro 65 secondi. "
            "Apri il log: normalmente indica autenticazione bloccata, opzioni non supportate "
            "oppure un problema della build xfreerdp con /parent-window."
        )
        self.append_log(
            "ERRORE incorporamento: nessuna finestra FreeRDP trovata tramite XEmbed, "
            "figlio X11 o _NET_WM_PID dopo l'attesa completa.\n"
        )
        self._set_state("error", "Desktop RDP non visualizzato")
        self._show_status("RDP non incorporato", message, error=True)
        self.set_log_visible(True)
        return False

    def _fail_start(self, title: str, message: str, details: str = "") -> None:
        # Non mostrare errori ormai obsoleti se nel frattempo FreeRDP è partito
        # o il plug XEmbed è già stato ricevuto.
        if self._started or self._launching or self._plug_seen or (self.process is not None and self.process.poll() is None):
            return
        self._launch_requested = False
        self._set_state("error", title)
        self._show_status(title, message, error=True)
        self.set_log_visible(True)
        self.notify_error(title, message, details or self.get_log_text())

    def on_plug_added(self, _socket: Gtk.Widget) -> None:
        client = X11EmbedBridge.direct_client(
            self.socket_xid, self.process.pid if self.process is not None else 0
        )
        if not client:
            client = self.socket_xid
        method = "reparenting X11 automatico" if self._manual_reparent_attempted else "XEmbed nativo"
        self._mark_embedded(client, method)

    def on_plug_removed(self, _socket: Gtk.Widget) -> bool:
        self.keyboard_active = False
        self._plug_seen = False
        if not self._stopping and self.process is not None and self.process.poll() is None:
            self.append_log("Avviso: la finestra FreeRDP è stata scollegata dal contenitore.\n")
            self._set_state("error", "Desktop remoto scollegato")
            self._show_status(
                "Desktop remoto scollegato",
                "La connessione è ancora attiva ma la finestra grafica non è più incorporata. Premi Riconnetti.",
                error=True,
            )
        return True

    def on_socket_button(self, _widget: Gtk.Widget, _event) -> bool:
        self.activate_keyboard()
        return False

    def on_socket_focus_in(self, _widget: Gtk.Widget, _event) -> bool:
        self.activate_keyboard(silent=True)
        return False

    def on_socket_enter(self, _widget: Gtk.Widget, _event) -> bool:
        if self._plug_seen:
            self.activate_keyboard(silent=True)
        return False

    def on_focus_clicked(self, _button: Gtk.Button) -> None:
        self._safe_action("Attiva tastiera", self.activate_keyboard)

    def _schedule_focus_retry(self, delay_ms: int) -> None:
        holder: Dict[str, int] = {}

        def callback() -> bool:
            source_id = holder.get("id")
            if source_id in self._focus_retry_ids:
                self._focus_retry_ids.remove(source_id)
            return self._focus_once()

        source_id = GLib.timeout_add(delay_ms, callback)
        holder["id"] = source_id
        self._focus_retry_ids.append(source_id)

    def _focus_once(self) -> bool:
        self.activate_keyboard(silent=True)
        return False

    def _schedule_screen_refresh(self, delay_ms: int) -> None:
        holder: Dict[str, int] = {}

        def callback() -> bool:
            source_id = holder.get("id")
            if source_id in self._screen_refresh_ids:
                self._screen_refresh_ids.remove(source_id)
            self.refresh_framebuffer(silent=True)
            return False

        source_id = GLib.timeout_add(delay_ms, callback)
        holder["id"] = source_id
        self._screen_refresh_ids.append(source_id)

    def refresh_framebuffer(self, silent: bool = False) -> None:
        if self.socket is None or not self._plug_seen or self.client_window_xid <= 0:
            if not silent:
                self._set_state("error", "Desktop RDP non ancora pronto")
            return
        allocation = self.socket.get_allocation()
        ok = X11EmbedBridge.refresh(
            self.client_window_xid, allocation.width, allocation.height
        )
        if ok:
            if not silent:
                self._set_state("ok", "Ridisegno schermo richiesto")
                self.append_log("Ridisegno X11 del framebuffer RDP richiesto.\n")
            self._schedule_focus_retry(120)
        elif not silent:
            self._set_state("error", "Impossibile aggiornare il framebuffer")

    def on_redraw_clicked(self, _button: Gtk.Button) -> None:
        self._safe_action("Aggiorna schermo", self.refresh_framebuffer)

    def _build_compatibility_argv(self, admin: bool = False) -> List[str]:
        """Crea un profilo conservativo per XEmbed e schermate di logon.

        Disabilita GFX e dynamic-resolution, usa una dimensione iniziale esplicita
        e lascia a smart-sizing il solo adattamento locale. Questo evita vari casi
        in cui il canale DISP/GFX viene inizializzato prima della dimensione finale
        del Gtk.Socket e il canvas rimane nero.
        """
        filtered: List[str] = []
        remove_prefixes = (
            "/gfx", "+gfx", "-gfx",
            "/dynamic-resolution", "+dynamic-resolution", "-dynamic-resolution",
            "/size:", "/scale:", "/smart-sizing", "/network:",
            "/bpp:", "/admin", "+admin", "-admin",
        )
        for arg in self._base_argv:
            low = arg.lower()
            if any(low.startswith(prefix) for prefix in remove_prefixes):
                continue
            filtered.append(arg)
        allocation = self.socket.get_allocation() if self.socket is not None else None
        width = max(800, int(allocation.width if allocation is not None else 1280))
        height = max(600, int(allocation.height if allocation is not None else 720))
        filtered.extend([
            f"/size:{width}x{height}",
            "/smart-sizing",
            "/bpp:32",
            "/network:auto",
        ])
        if admin:
            filtered.append("+admin")
        return filtered

    def retry_compatibility(self, admin: bool = False) -> None:
        self.argv = self._build_compatibility_argv(admin=admin)
        self._compatibility_mode = True
        self._admin_retry = bool(admin)
        profile = "compatibile + amministrativa" if admin else "compatibile"
        self.masked_command = " ".join(shlex.quote(arg) for arg in self.argv)
        if self.use_args_fd:
            self.masked_command += "  [password protetta via /args-from:fd su memfd]"
        self.append_log(
            f"[FedTermHub] Riavvio con profilo RDP {profile}: GFX e dynamic-resolution disattivati, "
            "dimensione iniziale fissa e smart-sizing attivo.\n"
        )
        self._set_status_actions(False)
        self.stop(restart=True)

    def on_compat_retry_clicked(self, _button: Gtk.Button) -> None:
        self._safe_action("Riprova modalità compatibile", self.retry_compatibility)

    def on_admin_retry_clicked(self, _button: Gtk.Button) -> None:
        self._safe_action(
            "Riprova sessione amministrativa",
            lambda: self.retry_compatibility(admin=True),
        )

    def activate_keyboard(self, silent: bool = False) -> None:
        if self.socket is None or not self._plug_seen:
            if not silent:
                self._set_state("error", "Desktop RDP non ancora pronto")
            return
        try:
            self.socket.grab_focus()
            try:
                self.socket.child_focus(Gtk.DirectionType.TAB_FORWARD)
            except Exception:
                pass
            if not self.socket_xid:
                self._refresh_socket_xid()
            target_xid = self.client_window_xid or self.socket_xid
            x11_ok = X11EmbedBridge.focus(target_xid) or X11FocusBridge.focus(self.socket_xid)
            self.keyboard_active = True
            if hasattr(self, "focus_hint"):
                self.focus_hint.set_reveal_child(False)
            if not silent:
                suffix = " — focus X11 assegnato" if x11_ok else ""
                self._set_state("ok", "Tastiera attiva" + suffix)
                self.append_log("Focus tastiera richiesto al desktop remoto.\n")
        except Exception as exc:
            if not silent:
                self._set_state("error", f"Errore focus tastiera: {exc}")
                self.append_log(f"ERRORE focus tastiera: {exc}\n")

    def _safe_action(self, name: str, callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception as exc:
            self._set_state("error", f"Errore {name}: {exc}")
            details = traceback.format_exc()
            self.append_log(f"ERRORE callback {name}: {exc}\n{details}\n")
            self.notify_error(f"Errore {name}", str(exc), details)

    def recover_embedding(self) -> None:
        if self.process is None or self.process.poll() is not None:
            self._set_state("error", "FreeRDP non è in esecuzione")
            self.append_log("Recupero finestra: processo FreeRDP non attivo.\n")
            return
        if not self.socket_xid:
            self._refresh_socket_xid()
        direct = X11EmbedBridge.direct_client(self.socket_xid, self.process.pid)
        if direct:
            self._mark_embedded(direct, "figlio X11 recuperato manualmente")
            return
        candidate = X11EmbedBridge.find_process_window(
            self.process.pid, self.embed_token, self.socket_xid
        )
        if candidate and self.socket is not None:
            allocation = self.socket.get_allocation()
            self._manual_reparent_attempted = True
            if X11EmbedBridge.reparent(
                candidate, self.socket_xid, allocation.width, allocation.height
            ):
                self._mark_embedded(candidate, "reparenting X11 manuale")
                return
        self._set_state("error", "Finestra FreeRDP non ancora trovata")
        self._show_status(
            "Finestra RDP non disponibile",
            "FreeRDP è attivo ma non ha ancora creato una finestra agganciabile. "
            "Controlla il log per eventuali richieste di autenticazione e riprova.",
            error=True,
        )
        self.append_log("Recupero finestra: nessuna finestra associata al PID trovata.\n")

    def on_recover_clicked(self, _button: Gtk.Button) -> None:
        self._safe_action("Recupera finestra", self.recover_embedding)

    def on_restart_clicked(self, _button: Gtk.Button) -> None:
        self._safe_action("Riconnetti", self.restart)

    def on_stop_clicked(self, _button: Gtk.Button) -> None:
        self._safe_action("Ferma", self.stop)

    def on_log_toggled(self, button: Gtk.ToggleButton) -> None:
        # Importante: questa funzione gestisce soltanto la visibilità del drawer.
        # Non avvia e non riavvia mai FreeRDP.
        self.set_log_visible(button.get_active(), sync_button=False)

    def on_diagnostics_clicked(self, _button: Gtk.Button) -> None:
        self._safe_action("Diagnostica", self.show_diagnostics)

    def on_export_log_clicked(self, _button: Gtk.Button) -> None:
        self._safe_action("Esporta log", self.export_log)

    def set_log_visible(self, visible: bool, sync_button: bool = True) -> None:
        self.log_revealer.set_reveal_child(bool(visible))
        self.log_toggle_label.set_text("Nascondi log" if visible else "Log")
        if sync_button and self.log_toggle.get_active() != bool(visible):
            self.log_toggle.handler_block_by_func(self.on_log_toggled)
            self.log_toggle.set_active(bool(visible))
            self.log_toggle.handler_unblock_by_func(self.on_log_toggled)

    def show_diagnostics(self) -> None:
        proc_state = "non avviato"
        if self.process is not None:
            rc = self.process.poll()
            proc_state = f"PID {self.process.pid}, {'in esecuzione' if rc is None else 'terminato rc=' + str(rc)}"
        alloc = self.socket.get_allocation() if self.socket is not None else None
        allocation = f"{alloc.width}x{alloc.height}" if alloc is not None else "n/d"
        lines = [
            "Diagnostica RDP interna",
            "=======================",
            f"Stato processo: {proc_state}",
            f"Avvio richiesto: {self._launch_requested}",
            f"Avviato: {self._started}",
            f"Avviato almeno una volta: {self._has_ever_started}",
            f"Desktop incorporato: {self._plug_seen}",
            f"Metodo incorporamento: {self.embed_method}",
            f"Finestra client XID: {self.client_window_xid}",
            f"Reparenting manuale tentato: {self._manual_reparent_attempted}",
            f"Incorporato almeno una volta: {self._has_ever_embedded}",
            f"Canale credenziale: {self.credential_transport}",
            f"args-from memfd richiesto: {self.use_args_fd}",
            f"Framebuffer GDI inizializzato: {self._gdi_ready_seen}",
            f"LOGON_MSG_BUMP_OPTIONS rilevato: {self._bump_options_seen}",
            f"Profilo compatibilità attivo: {self._compatibility_mode}",
            f"Tentativo amministrativo: {self._admin_retry}",
            f"Output ultimo evento: {int(time.monotonic() - self._last_output_at) if self._last_output_at else -1}s fa",
            f"Gtk.Socket XID: {self.socket_xid}",
            f"Socket realizzato: {self.socket.get_realized() if self.socket is not None else False}",
            f"Socket mappato: {self.socket.get_mapped() if self.socket is not None else False}",
            f"Allocazione: {allocation}",
            f"DISPLAY: {os.environ.get('DISPLAY', '-')}",
            f"GDK_BACKEND: {os.environ.get('GDK_BACKEND', '-')}",
            "",
            "Comando:",
            self.masked_command,
            "",
            "Ultimo log:",
            self.get_log_text()[-5000:],
        ]
        parent = self.get_toplevel()
        dialog = Gtk.Dialog(
            title="Diagnostica RDP",
            transient_for=parent if isinstance(parent, Gtk.Window) else None,
            flags=0,
        )
        dialog.add_button("Chiudi", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(760, 520)
        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text("\n".join(lines))
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(view)
        dialog.get_content_area().pack_start(scroller, True, True, 0)
        dialog.show_all()
        try:
            dialog.run()
        finally:
            dialog.destroy()

    def notify_error(self, title: str, message: str, details: str = "") -> None:
        if self.error_callback is None:
            return
        if self.last_error_notified and "terminato con codice" in title.lower():
            return
        self.last_error_notified = True
        GLib.idle_add(self.error_callback, title, message, details, self.masked_command)

    def append_log(self, text: str) -> bool:
        if self._destroyed:
            return False
        try:
            self.log_lines.append(text)
            if len(self.log_lines) > 1200:
                self.log_lines = self.log_lines[-1200:]
            end = self.log_buffer.get_end_iter()
            self.log_buffer.insert(end, text)
            mark = self.log_buffer.create_mark(None, self.log_buffer.get_end_iter(), False)
            self.log_view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
        except Exception:
            pass
        return False

    def get_log_text(self) -> str:
        try:
            start = self.log_buffer.get_start_iter()
            end = self.log_buffer.get_end_iter()
            return self.log_buffer.get_text(start, end, True)
        except Exception:
            return "".join(self.log_lines)

    @staticmethod
    def _is_normal_remote_close(rc: int, text: str) -> bool:
        low = text.lower()
        normal_markers = (
            "errinfo_logoff_by_user",
            "errinfo_rpc_initiated_logoff",
            "errinfo_rpc_initiated_disconnect",
            "errinfo_disconnected_by_other_connection",
            "the disconnection was initiated by the user logging off",
        )
        return rc == 0 or rc == 12 or any(marker in low for marker in normal_markers)

    @staticmethod
    def _summarize_freerdp_log(text: str) -> Tuple[str, List[str]]:
        low = text.lower()
        notes: List[str] = []
        if "tcgetattr" in low or "tcsetattr" in low or "ioctl non appropriata" in low:
            notes.append("Il client ha tentato il vecchio canale stdin. La 1.8 usa /args-from:fd su memfd e non dovrebbe generare questo avviso.")
        if "no rdp scancode found" in low:
            notes.append("Sono presenti avvisi XKB su tasti speciali non mappati; non impediscono la connessione.")
        if "certificate not checked" in low or "/cert:ignore" in low:
            notes.append("Il controllo certificato è disabilitato per questa sessione.")
        if "logon_msg_bump_options" in low:
            return (
                "Windows ha rilevato una sessione già attiva e attende una scelta di subentro; "
                "questa condizione può produrre una schermata nera in xfreerdp.",
                notes,
            )
        if "errinfo_logoff_by_user" in low:
            return "La connessione è riuscita, poi Windows ha eseguito il logoff della sessione.", notes
        if "errconnect_logon_failure" in low or "logon failure" in low:
            return "Credenziali rifiutate dal server Windows.", notes
        if "errconnect_password_must_change" in low or "password must change" in low:
            return "La password deve essere cambiata prima di poter accedere via RDP.", notes
        if "errconnect_password_certainly_expired" in low or "password expired" in low:
            return "La password dell’account è scaduta.", notes
        if "errconnect_account_locked_out" in low or "account locked" in low:
            return "L’account risulta bloccato.", notes
        if "errconnect_account_disabled" in low or "account disabled" in low:
            return "L’account risulta disabilitato.", notes
        if "credssp" in low or "authentication failure" in low:
            return "Autenticazione NLA/CredSSP non riuscita.", notes
        if "connection refused" in low:
            return "La porta RDP ha rifiutato la connessione.", notes
        if "transport_connect" in low or "unable to connect" in low or "timeout" in low:
            return "Il server RDP non è raggiungibile o non risponde entro il timeout.", notes
        if "tls_verify_certificate" in low and "denied" in low:
            return "Il certificato del server non è stato accettato.", notes
        return "La sessione RDP è terminata; consulta i dettagli tecnici nel log.", notes

    def _poll_process(self) -> bool:
        if self._destroyed or self.process is None:
            self._poll_id = None
            return False
        rc = self.process.poll()
        if rc is None:
            return True
        self._poll_id = None
        self._launch_requested = False
        self.keyboard_active = False
        self._started = False
        self._launching = False
        proc = self.process
        self.process = None
        self._close_secret_pty()
        if self._watchdog_id is not None:
            try:
                GLib.source_remove(self._watchdog_id)
            except Exception:
                pass
            self._watchdog_id = None
        self.append_log(f"Processo FreeRDP PID {proc.pid} terminato con codice {rc}.\n")
        log_text = self.get_log_text()
        if self._stopping:
            self._set_state("off", "Sessione RDP chiusa")
            self._show_status("Sessione chiusa", "La connessione RDP è stata chiusa.")
        elif self._is_normal_remote_close(rc, log_text):
            summary, notes = self._summarize_freerdp_log(log_text)
            self._set_state("off", "Sessione terminata dal server")
            self._show_status("Sessione RDP terminata", summary)
            self.append_log("\nEsito: " + summary + "\n")
            for note in notes:
                self.append_log("Nota: " + note + "\n")
            # Un logoff Windows è un evento normale: niente popup rosso e niente
            # falsa diagnosi di rete. Il pulsante Riconnetti resta disponibile.
        else:
            hint = self.explain_freerdp_exit(rc, log_text)
            _summary, notes = self._summarize_freerdp_log(log_text)
            self.append_log("\nSuggerimento: " + hint + "\n")
            for note in notes:
                self.append_log("Nota: " + note + "\n")
            self._set_state("error", f"RDP terminato con codice {rc}")
            self._show_status("Connessione RDP fallita", hint, error=True)
            self.set_log_visible(True)
            self.notify_error(f"RDP terminato con codice {rc}", hint, self.get_log_text())
        return False

    def export_log(self) -> None:
        parent = self.get_toplevel()
        dialog = Gtk.FileChooserDialog(
            title="Esporta log RDP",
            parent=parent if isinstance(parent, Gtk.Window) else None,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_buttons("_Annulla", Gtk.ResponseType.CANCEL, "_Salva", Gtk.ResponseType.OK)
        dialog.set_current_name("fedtermhub-rdp.log")
        dialog.set_do_overwrite_confirmation(True)
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                path = Path(dialog.get_filename())
                atomic_write_text(path, self.get_log_text())
                self._set_state("ok", f"Log salvato: {path.name}")
        finally:
            dialog.destroy()

    @staticmethod
    def explain_freerdp_exit(rc: int, text: str) -> str:
        low = text.lower()
        if "logon_msg_bump_options" in low:
            return (
                "Windows sta aspettando una scelta relativa a una sessione già attiva. "
                "Riprova con il profilo compatibile oppure, se appropriato, con +admin."
            )
        if "errinfo_logoff_by_user" in low or "user logging off" in low:
            return "La connessione ha funzionato, ma Windows ha chiuso la sessione con un logoff lato server/utente."
        if "errconnect_logon_failure" in low or "logon failure" in low or "authentication failure" in low:
            return "Autenticazione fallita: verifica utente, password, dominio e autorizzazione Desktop remoto."
        if "password_must_change" in low or "password must change" in low:
            return "La password deve essere cambiata. Accedi localmente o usa un metodo che consenta il cambio password."
        if "password_certainly_expired" in low or "password expired" in low:
            return "Password scaduta: aggiornala prima di riprovare la connessione RDP."
        if "account_locked" in low or "account locked" in low:
            return "Account bloccato: sbloccalo in Active Directory o attendi il tempo previsto dalla policy."
        if "account_disabled" in low or "account disabled" in low:
            return "Account disabilitato: verifica lo stato dell’utente in Active Directory."
        if "credssp" in low:
            return "Errore NLA/CredSSP: controlla dominio, orario, DNS, policy NLA e compatibilità del server."
        if "connection refused" in low:
            return "Connessione rifiutata: verifica che Desktop remoto sia attivo e che firewall/NAT consentano la porta configurata."
        if "unable to connect" in low or "transport_connect" in low or "timeout" in low:
            return "Server non raggiungibile: verifica rete, VPN, routing, firewall e porta RDP."
        if "certificate" in low and ("denied" in low or "verify" in low):
            return "Problema certificato RDP: verifica identità e certificato del server."
        if "tcgetattr" in low or "tcsetattr" in low or "ioctl non appropriata" in low:
            return "Errore nel vecchio canale password: la 1.8 usa /args-from:fd su memfd e non dipende dal terminale."
        if "parent-window" in low or "xembed" in low:
            return "Problema XEmbed: verifica XWayland, backend X11 e supporto /parent-window del client FreeRDP."
        if rc == 127:
            return "FreeRDP non trovato: installa il pacchetto freerdp."
        return "La sessione RDP è terminata. Apri il log interno per il dettaglio tecnico."

    def restart(self) -> None:
        self.stop(restart=True)

    def _restart_after_stop(self) -> bool:
        if self._destroyed:
            return False
        self.last_error_notified = False
        self._stopping = False
        self._started = False
        self._launching = False
        self._launch_requested = False
        self._start_attempts = 0
        self._plug_seen = False
        self.client_window_xid = 0
        self.embed_method = "in attesa"
        self._manual_reparent_attempted = False
        if hasattr(self, "focus_hint"):
            self.focus_hint.set_reveal_child(False)
        self.request_start()
        return False

    def stop(self, restart: bool = False) -> None:
        self._launch_requested = False
        self._stopping = True
        self._close_secret_pty()
        self._cancel_sources(keep_poll=False)
        proc = self.process
        self.process = None
        self._started = False
        self._launching = False
        self.keyboard_active = False
        self._plug_seen = False
        self.client_window_xid = 0
        self.embed_method = "non collegato"
        if proc is not None and proc.poll() is None:
            try:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    proc.terminate()
                self._set_state("off", "Chiusura RDP in corso…")
                self.append_log("Richiesta chiusura inviata a FreeRDP.\n")
                threading.Thread(target=self._wait_and_kill, args=(proc, restart), daemon=True).start()
                return
            except Exception as exc:
                self._set_state("error", f"Errore chiusura RDP: {exc}")
        else:
            self._set_state("off", "Sessione RDP ferma")
        if restart:
            GLib.idle_add(self._restart_after_stop)

    def _wait_and_kill(self, proc: subprocess.Popen, restart: bool = False) -> None:
        try:
            proc.wait(timeout=1.8)
        except subprocess.TimeoutExpired:
            try:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    proc.kill()
                GLib.idle_add(self.append_log, "Processo FreeRDP terminato forzatamente.\n")
            except Exception:
                pass
        finally:
            if restart and not self._destroyed:
                GLib.idle_add(self._restart_after_stop)

    def _cancel_sources(self, keep_poll: bool = False) -> None:
        ids = [self._start_source_id, self._watchdog_id, self._embed_probe_id]
        if not keep_poll:
            ids.append(self._poll_id)
        for source_id in ids:
            if source_id is not None:
                try:
                    GLib.source_remove(source_id)
                except Exception:
                    pass
        self._start_source_id = None
        self._watchdog_id = None
        self._embed_probe_id = None
        if not keep_poll:
            self._poll_id = None
        for source_id in list(self._focus_retry_ids):
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass
        self._focus_retry_ids.clear()
        for source_id in list(self._screen_refresh_ids):
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass
        self._screen_refresh_ids.clear()
        if self._bump_notice_id is not None:
            try:
                GLib.source_remove(self._bump_notice_id)
            except Exception:
                pass
            self._bump_notice_id = None

    def on_destroyed(self, _widget: Gtk.Widget) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        self._cancel_sources()
        self._close_secret_pty()
        proc = self.process
        self.process = None
        if proc is not None and proc.poll() is None:
            try:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    proc.terminate()
            except Exception:
                pass

class RdpDetachedWindow(Gtk.ApplicationWindow):
    """Finestra RDP indipendente che non blocca l'interfaccia principale."""

    def __init__(
        self,
        owner,
        tab,
        monitor_selector: str = "current",
        start_fullscreen: bool = False,
        start_backend: bool = True,
    ) -> None:
        app = owner.get_application()
        super().__init__(application=app, title=f"FedTermHub RDP — {getattr(tab, 'title', 'Sessione')}")
        self.owner = owner
        self.tab = tab
        self.monitor_selector = str(monitor_selector or "current")
        self.fullscreen_active = False
        self._closing = False
        self._start_backend = bool(start_backend)
        self.set_default_size(1440, 900)
        self.set_resizable(True)
        self.set_destroy_with_parent(False)
        # Gruppo finestre dedicato: i dialoghi modali della finestra principale
        # (stesso gruppo implicito) non devono bloccare l'input della sessione
        # RDP sganciata. I dialoghi aperti con transient_for su questa finestra
        # entrano automaticamente nel gruppo e restano modali solo qui.
        self._window_group = Gtk.WindowGroup()
        self._window_group.add_window(self)
        apply_window_icon(self)
        try:
            self.set_role(f"{APP_ID}.rdp")
        except Exception:
            pass
        self.add(tab)
        tab.fullscreen_callback = self.set_rdp_fullscreen
        tab.close_callback = self.close_session
        if hasattr(tab, "set_detach_callback"):
            tab.set_detach_callback(None)
        self.connect("delete-event", self._on_delete)
        self.connect("destroy", self._on_destroy)
        self.connect("key-press-event", self._on_key_press)
        self.show_all()
        if start_fullscreen:
            GLib.timeout_add(100, self._enter_fullscreen_and_start)
        elif self._start_backend:
            GLib.idle_add(tab.request_start)
        GLib.timeout_add(220, lambda: (self.present(), False)[1])

    def _enter_fullscreen_and_start(self) -> bool:
        if self._closing:
            return False
        self.set_rdp_fullscreen(True)
        if self._start_backend:
            GLib.timeout_add(320, self.tab.request_start)
        return False

    def set_rdp_fullscreen(self, enabled: bool) -> None:
        if self._closing:
            return
        enabled = bool(enabled)
        if enabled:
            monitors = enumerate_monitors()
            current_index = current_monitor_index_for_window(self, monitors)
            descriptor, exact = resolve_monitor_selector(
                self.monitor_selector, monitors, current_index=current_index
            )
            try:
                if descriptor is not None:
                    # Su X11/KDE il move preliminare aiuta il window manager a
                    # scegliere l'output corretto; su Wayland viene ignorato in
                    # sicurezza e resta valida fullscreen_on_monitor.
                    try:
                        self.move(descriptor.x + 24, descriptor.y + 24)
                    except Exception:
                        pass
                if descriptor is not None and hasattr(self, "fullscreen_on_monitor"):
                    self.fullscreen_on_monitor(self.get_screen(), descriptor.index)
                else:
                    self.fullscreen()
            except Exception:
                self.fullscreen()
                exact = False
            self.fullscreen_active = True
            self.tab.set_fullscreen_state(True)
            append_log = getattr(self.tab, "_append_log", None)
            if callable(append_log):
                if descriptor is not None:
                    append_log(
                        f"[FedTermHub] Finestra RDP indipendente fullscreen su {descriptor.label}."
                        + ("\n" if exact else " Fallback monitor applicato.\n")
                    )
                else:
                    append_log("[FedTermHub] Finestra RDP indipendente fullscreen sul monitor disponibile.\n")
            self.owner.set_status(
                "RDP indipendente a schermo intero — FedTermHub rimane disponibile"
            )
        else:
            self.fullscreen_active = False
            try:
                self.unfullscreen()
            except Exception:
                pass
            self.tab.set_fullscreen_state(False)
            self.owner.set_status("Sessione RDP indipendente ripristinata in finestra")
        GLib.timeout_add(360, lambda: (self.tab.canvas.grab_focus(), False)[1])

    def _on_key_press(self, _window: Gtk.Window, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_F11:
            self.set_rdp_fullscreen(not self.fullscreen_active)
            return True
        if event.keyval == Gdk.KEY_Escape and self.fullscreen_active:
            self.set_rdp_fullscreen(False)
            return True
        return False

    def close_session(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            self.tab.fullscreen_callback = None
            self.tab.close_callback = None
            self.tab.stop(silent=True)
        except Exception:
            pass
        self.destroy()

    def _on_delete(self, *_args) -> bool:
        self.close_session()
        return True

    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        try:
            self.owner._on_detached_rdp_destroyed(self)
        except Exception:
            pass


class AppWindow(Gtk.ApplicationWindow):
    DND_TARGET = "application/x-fedtermhub-session-node"
    COL_NAME = 0
    COL_DETAIL = 1
    COL_PATH = 2
    COL_KIND = 3
    COL_ICON = 4
    COL_BG = 5
    COL_TEXT_XPAD = 6
    COL_BG_SET = 7
    COL_FOLDER_PIXBUF = 8

    def __init__(self, app: Gtk.Application) -> None:
        super().__init__(application=app, title=f"{APP_NAME} {APP_VERSION}")
        self.store = SessionStore(SESSIONS_FILE)
        self.tunnel_store = TunnelStore(TUNNELS_FILE)
        self.tunnel_processes: Dict[str, subprocess.Popen] = {}
        self.tunnel_logs: Dict[str, List[str]] = {}
        self.tunnel_states: Dict[str, str] = {}
        self.tunnel_exit_codes: Dict[str, int] = {}
        self._tunnel_manual_stops: set[str] = set()
        self._tunnel_restart_sources: Dict[str, int] = {}
        self.vault = PasswordVault(VAULT_FILE)
        self.settings = AppSettings.load(SETTINGS_FILE)
        self.lab_controller = EphemeralLabController() if EphemeralLabController is not None else None
        self.lab_dialog = None
        self.detached_rdp_windows: List[RdpDetachedWindow] = []
        self.set_default_size(self.settings.window_width, self.settings.window_height)
        self.set_resizable(True)
        self.set_decorated(True)
        apply_window_icon(self)
        try:
            # Gtk.Application uses APP_ID for desktop matching on Wayland and X11.
            self.set_role(APP_ID)
        except Exception:
            pass
        self.css_provider: Optional[Gtk.CssProvider] = None
        self.terminal_tabs: List[TerminalTab] = []
        self.session_filter_text = ""
        self._folder_pixbuf_cache: Dict[str, object] = {}
        self._save_settings_source_id: Optional[int] = None
        self.connect("delete-event", self.on_window_delete)
        self.connect("configure-event", self.on_window_configure)
        self.connect("key-press-event", self.on_global_key_press)
        self.connect("button-press-event", self.on_user_activity)
        self.connect("focus-in-event", self.on_user_activity)
        self.connect("window-state-event", self.on_window_state_event)
        self._last_user_activity = time.monotonic()
        self._security_timer_id = GLib.timeout_add_seconds(15, self._security_tick)
        self._closing_confirmed = False
        self._fullscreen = False
        self._rdp_immersive_tab = None
        self._notebook_tabs_before_immersive = True
        self._notification_hide_id: Optional[int] = None
        self._ribbon_images: List[Gtk.Image] = []
        self._ribbon_captions: List[Gtk.Label] = []
        self._rail_images: List[Gtk.Image] = []

        self._registered_app_icons: set = set()
        self.register_app_icons()
        self.apply_icon_theme()
        self._build_layout()
        self.apply_theme(self.settings.theme)
        self.apply_layout_preferences()
        self.refresh_sessions()
        if self.settings.startup_mode == "terminal":
            self.open_local_terminal()
        GLib.timeout_add_seconds(1, self.start_auto_tunnels)
        if self.store.load_warning:
            warning = self.store.load_warning
            GLib.idle_add(lambda: (self.report_session_error("Sessioni non leggibili", warning, ""), False)[1])

    def apply_icon_theme(self) -> None:
        chosen = resolve_icon_theme(self.settings.icon_theme, self.settings.theme)
        self.active_icon_theme = chosen
        settings = Gtk.Settings.get_default()
        if settings is not None and chosen:
            try:
                settings.set_property("gtk-icon-theme-name", chosen)
            except Exception:
                pass
        try:
            Gtk.IconTheme.get_default().rescan_if_needed()
        except Exception:
            pass

    def make_icon_image(self, icon_names: Union[str, List[str], Tuple[str, ...]], size: int = 24) -> Gtk.Image:
        names = [icon_names] if isinstance(icon_names, str) else list(icon_names)
        # Le icone symbolic ereditano il colore dal CSS e rimangono leggibili
        # sia nel tema chiaro sia in quello scuro. Le varianti raster vengono
        # usate soltanto come fallback.
        ordered = [name for name in names if name.endswith("-symbolic")] + [
            name for name in names if not name.endswith("-symbolic")
        ]
        name = first_available_icon(ordered)
        image = Gtk.Image.new_from_icon_name(name, Gtk.IconSize.DIALOG)
        image.get_style_context().add_class("adaptive-icon")
        try:
            image.set_pixel_size(size)
        except Exception:
            pass
        return image

    def protocol_icon(self, protocol: str) -> str:
        # Preferisci le icone vettoriali dedicate di FedTermHub quando sono
        # state registrate; il fallback resta il tema di sistema.
        builtin = f"fth-proto-{(protocol or '').lower()}"
        if builtin in getattr(self, "_registered_app_icons", set()):
            return builtin
        mapping = {
            "local": first_available_icon(["utilities-terminal", "utilities-terminal-symbolic"]),
            "command": first_available_icon(["system-run", "application-x-executable", "system-run-symbolic"]),
            "ssh": first_available_icon(["network-server", "network-workgroup", "computer", "network-server-symbolic"]),
            "sftp": first_available_icon(["folder-remote", "folder-publicshare", "folder", "folder-remote-symbolic"]),
            "rdp": first_available_icon(["video-display", "computer", "preferences-desktop-display", "video-display-symbolic"]),
            "vnc": first_available_icon(["computer", "network-workgroup", "video-display", "computer-symbolic"]),
            "serial": first_available_icon(["network-wired", "network-transmit-receive", "input-keyboard", "network-wired-symbolic"]),
        }
        return mapping.get(protocol, first_available_icon(["network-server", "applications-system", "network-server-symbolic"]))

    def register_app_icons(self) -> None:
        """Carica le icone SVG di FedTermHub ricolorate per il tema corrente.

        Ogni SVG usa ``currentColor``: qui lo sostituiamo con il colore di
        primo piano del tema (chiaro o scuro) e registriamo il risultato come
        icona built-in, così l'albero sessioni e i pulsanti che usano i nomi
        icona la ereditano senza altre modifiche. Va richiamata anche al
        cambio tema per rigenerare i pixbuf nel colore giusto.
        """
        icons_dir = Path(__file__).resolve().parent / "icons"
        if not icons_dir.is_dir():
            self._registered_app_icons = set()
            return
        dark = str(getattr(self.settings, "theme", "dark")).lower() != "light"
        default_stroke = "#cbd5e1" if dark else "#3a4653"
        # Colori di protocollo per tema: accesi sullo scuro, più saturi/scuri
        # sul chiaro per restare leggibili su fondo bianco.
        proto_colors = {
            "fth-proto-ssh": "#22d3ee" if dark else "#0e7490",
            "fth-proto-rdp": "#60a5fa" if dark else "#2563eb",
            "fth-proto-vnc": "#c084fc" if dark else "#7c3aed",
            "fth-proto-sftp": "#34d399" if dark else "#059669",
            "fth-proto-serial": "#fbbf24" if dark else "#b45309",
            "fth-proto-local": "#cbd5e1" if dark else "#475569",
            "fth-proto-telnet": "#f472b6" if dark else "#be185d",
            "fth-proto-ftp": "#fb923c" if dark else "#c2410c",
            "fth-act-delete": "#f87171" if dark else "#dc2626",
            "fth-tb-terminale": "#22d3ee" if dark else "#0e7490",
            "fth-tb-sessioni": "#60a5fa" if dark else "#2563eb",
            "fth-tb-rete": "#34d399" if dark else "#059669",
            "fth-tb-strumenti": "#c084fc" if dark else "#7c3aed",
            "fth-tb-password": "#fbbf24" if dark else "#b45309",
            "fth-tb-vista": "#f472b6" if dark else "#be185d",
            "fth-tb-sistema": "#9aa5b3" if dark else "#475569",
            "fth-tb-lab": "#5eead4" if dark else "#0d9488",
        }
        registered = set()
        for svg_path in sorted(icons_dir.glob("*.svg")):
            name = svg_path.stem
            try:
                template = svg_path.read_text(encoding="utf-8")
            except Exception:
                continue
            stroke = proto_colors.get(name, default_stroke)
            colored = template.replace("currentColor", stroke)
            ok = False
            for size in (16, 24, 32):
                try:
                    loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
                    loader.set_size(size, size)
                    loader.write(colored.encode("utf-8"))
                    loader.close()
                    pixbuf = loader.get_pixbuf()
                    if pixbuf is not None:
                        Gtk.IconTheme.add_builtin_icon(name, size, pixbuf)
                        ok = True
                except Exception:
                    continue
            if ok:
                registered.add(name)
        self._registered_app_icons = registered

    def refresh_icon_theme_runtime(self) -> None:
        self.register_app_icons()
        self.apply_icon_theme()
        try:
            self.refresh_sessions()
        except Exception:
            pass
        try:
            self.refresh_sessions()
        except Exception:
            pass

    def make_button(self, label: str, icon_name: Union[str, List[str], Tuple[str, ...]] = "", css_class: str = "toolbar-button") -> Gtk.Button:
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_can_focus(False)
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        inner.set_halign(Gtk.Align.CENTER)
        if icon_name:
            inner.pack_start(self.make_icon_image(icon_name, 16), False, False, 0)
        text = Gtk.Label(label=label)
        text.set_ellipsize(Pango.EllipsizeMode.END)
        inner.pack_start(text, False, False, 0)
        button.add(inner)
        button.get_style_context().add_class(css_class)
        return button

    def make_ribbon_button(self, label: str, icon_name: Union[str, List[str], Tuple[str, ...]], callback=None, css_class: str = "ribbon-button") -> Gtk.Button:
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_can_focus(False)
        button.set_tooltip_text(label)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_halign(Gtk.Align.CENTER)
        image = self.make_icon_image(icon_name, self.settings.toolbar_icon_size)
        caption = Gtk.Label(label=label)
        caption.set_ellipsize(Pango.EllipsizeMode.END)
        caption.set_max_width_chars(10)
        caption.get_style_context().add_class("ribbon-caption")
        box.pack_start(image, False, False, 0)
        box.pack_start(caption, False, False, 0)
        self._ribbon_images.append(image)
        self._ribbon_captions.append(caption)
        button.add(box)
        button.get_style_context().add_class(css_class)
        if callback is not None:
            button.connect("clicked", callback)
        return button

    def make_theme_toggle_button(self) -> Gtk.Button:
        """Crea il selettore tema esterno ai menu, sempre visibile nella ribbon."""
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_can_focus(False)
        button.get_style_context().add_class("ribbon-button")
        button.get_style_context().add_class("theme-toggle-button")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_halign(Gtk.Align.CENTER)
        self.theme_toggle_image = self.make_icon_image(
            ["weather-clear-symbolic", "weather-clear"],
            self.settings.toolbar_icon_size,
        )
        self.theme_toggle_caption = Gtk.Label(label="Tema")
        self.theme_toggle_caption.set_ellipsize(Pango.EllipsizeMode.END)
        self.theme_toggle_caption.set_max_width_chars(10)
        self.theme_toggle_caption.get_style_context().add_class("ribbon-caption")
        box.pack_start(self.theme_toggle_image, False, False, 0)
        box.pack_start(self.theme_toggle_caption, False, False, 0)
        button.add(box)

        self._ribbon_images.append(self.theme_toggle_image)
        self._ribbon_captions.append(self.theme_toggle_caption)
        self.theme_toggle_button = button
        button.connect("clicked", lambda *_: self.cycle_theme())
        self.refresh_theme_toggle_button()
        return button

    def refresh_theme_toggle_button(self) -> None:
        """Aggiorna icona e testo mostrando il tema che verrà attivato al clic."""
        button = getattr(self, "theme_toggle_button", None)
        image = getattr(self, "theme_toggle_image", None)
        caption = getattr(self, "theme_toggle_caption", None)
        if button is None or image is None or caption is None:
            return

        switch_to_light = self.settings.theme == "dark"
        icon_names = (
            ["weather-clear-symbolic", "weather-clear"]
            if switch_to_light
            else ["weather-clear-night-symbolic", "weather-clear-night"]
        )
        icon_name = first_available_icon(icon_names)
        image.set_from_icon_name(icon_name, Gtk.IconSize.DIALOG)
        image.set_pixel_size(max(18, min(int(self.settings.toolbar_icon_size), 32)))
        caption.set_text("Chiaro" if switch_to_light else "Scuro")
        button.set_tooltip_text(
            "Passa al tema chiaro" if switch_to_light else "Passa al tema scuro"
        )

    def make_ribbon_group(self, title: str, items: List[Tuple[str, Union[str, List[str], Tuple[str, ...]], object]]) -> Gtk.Box:
        # Gruppi piatti e compatti, senza riquadri o titoli che
        # rubano altezza alla schermata principale. Il nome resta nel tooltip.
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=1)
        outer.get_style_context().add_class("ribbon-group")
        outer.set_tooltip_text(title)
        for label, icon, cb in items:
            outer.pack_start(self.make_ribbon_button(label, icon, cb), False, False, 0)
        return outer

    def make_rail_button(self, label: str, icon_name: Union[str, List[str], Tuple[str, ...]], callback=None) -> Gtk.Button:
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_can_focus(False)
        button.set_tooltip_text(label)
        image = self.make_icon_image(icon_name, max(16, self.settings.toolbar_icon_size - 4))
        self._rail_images.append(image)
        button.add(image)
        button.get_style_context().add_class("rail-button")
        if callback is not None:
            button.connect("clicked", callback)
        return button

    def apply_layout_preferences(self) -> None:
        """Applica dimensioni e visibilità senza ricostruire la finestra."""
        size = max(18, min(int(self.settings.toolbar_icon_size), 32))
        for image in getattr(self, "_ribbon_images", []):
            try:
                image.set_pixel_size(size)
            except Exception:
                pass
        for image in getattr(self, "_rail_images", []):
            try:
                image.set_pixel_size(max(16, size - 4))
            except Exception:
                pass
        for caption in getattr(self, "_ribbon_captions", []):
            visible = bool(self.settings.show_toolbar_labels)
            caption.set_no_show_all(not visible)
            caption.set_visible(visible)

        visibility = [
            (getattr(self, "menubar", None), self.settings.show_menubar),
            (getattr(self, "ribbon", None), self.settings.show_ribbon),
            (getattr(self, "rail", None), self.settings.show_left_rail),
            (getattr(self, "sidebar", None), self.settings.show_sidebar),
            (getattr(self, "footer", None), self.settings.show_statusbar),
            (getattr(self, "tabbar_hint", None), self.settings.show_workspace_hint),
        ]
        for widget, visible in visibility:
            if widget is not None:
                widget.set_no_show_all(not bool(visible))
                widget.set_visible(bool(visible))

        if hasattr(self, "paned") and self.settings.show_sidebar:
            self.paned.set_position(max(190, min(int(self.settings.sidebar_width), 520)))
        if hasattr(self, "notebook"):
            ctx = self.notebook.get_style_context()
            if self.settings.compact_tabs:
                ctx.add_class("compact-tabs")
            else:
                ctx.remove_class("compact-tabs")
        if hasattr(self, "notification_revealer"):
            self.notification_revealer.set_transition_duration(
                180 if self.settings.animations and not SAFE_MODE else 0
            )
        self.settings.save(SETTINGS_FILE)

    def apply_terminal_preferences(self) -> None:
        """Aggiorna terminali già aperti senza interrompere i processi."""
        for tab in list(self.terminal_tabs):
            try:
                tab.apply_preferences(self.settings)
                tab.apply_terminal_theme(self.settings.theme)
            except Exception:
                pass


    def toggle_sidebar(self, visible: Optional[bool] = None) -> None:
        if not hasattr(self, "sidebar"):
            return
        new_state = (not self.sidebar.get_visible()) if visible is None else bool(visible)
        self.settings.show_sidebar = new_state
        self.sidebar.set_no_show_all(not new_state)
        self.sidebar.set_visible(new_state)
        if new_state:
            self.paned.set_position(max(190, min(int(self.settings.sidebar_width), 520)))
        self.settings.save(SETTINGS_FILE)
        self.set_status("Pannello sessioni visibile" if new_state else "Pannello sessioni nascosto")

    def focus_session_search(self) -> None:
        if not getattr(self, "sidebar", None):
            return
        if not self.sidebar.get_visible():
            self.toggle_sidebar(True)
        GLib.idle_add(lambda: (self.session_filter.grab_focus(), False)[1])

    def toggle_left_rail(self, visible: Optional[bool] = None) -> None:
        if not hasattr(self, "rail"):
            return
        new_state = (not self.rail.get_visible()) if visible is None else bool(visible)
        self.settings.show_left_rail = new_state
        self.rail.set_no_show_all(not new_state)
        self.rail.set_visible(new_state)
        self.settings.save(SETTINGS_FILE)

    def toggle_ribbon(self, visible: Optional[bool] = None) -> None:
        if not hasattr(self, "ribbon"):
            return
        new_state = (not self.ribbon.get_visible()) if visible is None else bool(visible)
        self.settings.show_ribbon = new_state
        self.ribbon.set_no_show_all(not new_state)
        self.ribbon.set_visible(new_state)
        self.settings.save(SETTINGS_FILE)
        self.set_status("Barra comandi visibile" if new_state else "Barra comandi nascosta")

    def toggle_menubar(self, visible: Optional[bool] = None) -> None:
        if not hasattr(self, "menubar"):
            return
        new_state = (not self.menubar.get_visible()) if visible is None else bool(visible)
        self.settings.show_menubar = new_state
        self.menubar.set_no_show_all(not new_state)
        self.menubar.set_visible(new_state)
        self.settings.save(SETTINGS_FILE)

    def toggle_statusbar(self, visible: Optional[bool] = None) -> None:
        if not hasattr(self, "footer"):
            return
        new_state = (not self.footer.get_visible()) if visible is None else bool(visible)
        self.settings.show_statusbar = new_state
        self.footer.set_no_show_all(not new_state)
        self.footer.set_visible(new_state)
        self.settings.save(SETTINGS_FILE)

    def toggle_workspace_hint(self, visible: Optional[bool] = None) -> None:
        if not hasattr(self, "tabbar_hint"):
            return
        new_state = (not self.tabbar_hint.get_visible()) if visible is None else bool(visible)
        self.settings.show_workspace_hint = new_state
        self.tabbar_hint.set_no_show_all(not new_state)
        self.tabbar_hint.set_visible(new_state)
        self.settings.save(SETTINGS_FILE)

    def _menu_icon_for_label(self, text: str) -> List[str]:
        low = text.lower()
        mapping = [
            (("nuovo terminale", "term"), ["utilities-terminal-symbolic", "utilities-terminal"]),
            (("quick connect",), ["system-search-symbolic", "system-search"]),
            (("esci",), ["application-exit-symbolic", "application-exit"]),
            (("nuova ssh",), ["network-workgroup-symbolic", "network-workgroup"]),
            (("nuova rdp",), ["video-display-symbolic", "video-display"]),
            (("nuova serial",), ["network-wired-symbolic", "network-wired"]),
            (("nuova cartella",), ["folder-new-symbolic", "folder-new"]),
            (("nuova session",), ["list-add-symbolic", "list-add"]),
            (("modifica",), ["document-edit-symbolic", "document-edit"]),
            (("duplica",), ["edit-copy-symbolic", "edit-copy"]),
            (("elimina",), ["edit-delete-symbolic", "edit-delete"]),
            (("esporta",), ["document-save-symbolic", "document-save"]),
            (("importa",), ["document-open-symbolic", "document-open"]),
            (("mostra", "nascondi"), ["view-grid-symbolic", "view-grid"]),
            (("espandi",), ["pan-down-symbolic", "pan-down"]),
            (("comprimi",), ["pan-end-symbolic", "pan-end"]),
            (("server locali",), ["network-server-symbolic", "network-server"]),
            (("porte locali",), ["network-idle-symbolic", "network-idle"]),
            (("wake-on-lan",), ["system-run-symbolic", "system-run"]),
            (("network scanner",), ["network-workgroup-symbolic", "network-workgroup"]),
            (("port scanner",), ["network-transmit-receive-symbolic", "network-transmit-receive"]),
            (("cattura pacchetti",), ["network-receive-symbolic", "network-receive"]),
            (("tunneling",), ["network-transmit-receive-symbolic", "network-transmit-receive"]),
            (("password",), ["dialog-password-symbolic", "dialog-password"]),
            (("blocca password",), ["changes-prevent-symbolic", "changes-prevent"]),
            (("log applicazione",), ["text-x-log-symbolic", "text-x-log"]),
            (("diagnostica",), ["dialog-information-symbolic", "dialog-information"]),
            (("apri impostazioni",), ["emblem-system-symbolic", "preferences-system"]),
            (("scorciatoie",), ["preferences-desktop-keyboard-shortcuts-symbolic", "preferences-desktop-keyboard-shortcuts"]),
            (("informazioni",), ["help-about-symbolic", "help-about"]),
        ]
        for needles, icons in mapping:
            if all(n in low for n in needles):
                return icons
        return ["pan-end-symbolic", "go-next-symbolic"]

    def make_image_menu_item(
        self,
        text: str,
        icon_names: Union[str, List[str], Tuple[str, ...]],
        icon_size: int = 14,
    ) -> Gtk.MenuItem:
        """Build a GTK3 menu item with an icon without deprecated ImageMenuItem."""
        item = Gtk.MenuItem()
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        row.pack_start(self.make_icon_image(icon_names, icon_size), False, False, 0)
        row.pack_start(Gtk.Label(label=text, xalign=0), True, True, 0)
        item.add(row)
        item.get_style_context().add_class("dropdown-item")
        return item

    def make_menu(self, label: str, entries: List[Tuple[str, object]]) -> Gtk.MenuItem:
        root_item = Gtk.MenuItem(label=label)
        menu = Gtk.Menu()
        menu.set_name("dropdown-menu")
        try:
            menu.set_reserve_toggle_size(False)
        except Exception:
            pass
        for text, callback in entries:
            if text == "---":
                sep = Gtk.SeparatorMenuItem()
                sep.get_style_context().add_class("menu-separator")
                menu.append(sep)
                continue
            item = self.make_image_menu_item(text, self._menu_icon_for_label(text), 14)
            if callback is not None:
                item.connect("activate", callback)
            menu.append(item)
        menu.show_all()
        root_item.set_submenu(menu)
        return root_item

    def _build_layout(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.root = root
        root.set_name("main-root")
        self.add(root)

        menubar = Gtk.MenuBar()
        self.menubar = menubar
        menubar.set_name("main-menubar")
        menubar.append(self.make_menu("Terminale", [
            ("Nuovo terminale locale", lambda *_: self.open_local_terminal()),
            ("Terminale diviso", lambda *_: self.open_split_terminal()),
            ("MultiExec", lambda *_: self.show_multiexec_dialog()),
            ("Quick connect", lambda *_: self.on_quick_connect()),
            ("---", None),
            ("Esci", lambda *_: self.close()),
        ]))
        menubar.append(self.make_menu("Sessioni", [
            ("Nuova sessione", lambda *_: self.on_add_session(None)),
            ("Nuova SSH", lambda *_: self.on_add_proto_session("ssh")),
            ("Nuova RDP", lambda *_: self.on_add_proto_session("rdp")),
            ("Nuova VNC", lambda *_: self.on_add_proto_session("vnc")),
            ("Nuova seriale", lambda *_: self.on_add_proto_session("serial")),
            ("Nuova cartella", lambda *_: self.on_add_folder(None)),
            ("---", None),
            ("Modifica selezionata", lambda *_: self.on_edit_selected(None)),
            ("Duplica selezionata", lambda *_: self.on_duplicate_selected(None)),
            ("Sposta selezionata", lambda *_: self.on_move_selected(None)),
            ("Elimina selezionata", lambda *_: self.on_delete_selected(None)),
            ("---", None),
            # Prima erano sei voci, tre coppie che facevano quasi la stessa cosa in formati
            # diversi. L'importazione legge anche il formato vecchio.
            ("Esporta tutte le sessioni", lambda *_: self.export_home_session()),
            ("Esporta la selezione", lambda *_: self.export_selected_node()),
            ("Importa sessioni nella cartella selezionata", lambda *_: self.import_into_selected_folder()),
            ("Sostituisci tutte le sessioni con un file", lambda *_: self.import_home_session()),
        ]))
        menubar.append(self.make_menu("Rete", [
            ("Server locali", lambda *_: self.open_network_tools("services")),
            ("FTP / SFTP share", lambda *_: self.open_network_tools("shares")),
            ("Porte locali in ascolto", lambda *_: self.open_network_tools("listeners")),
            ("Wake-on-LAN", lambda *_: self.open_network_tools("wol")),
            ("Network scanner", lambda *_: self.open_network_tools("network-scan")),
            ("Port scanner", lambda *_: self.open_network_tools("port-scan")),
            ("Cattura pacchetti", lambda *_: self.open_network_tools("capture")),
            ("---", None),
            ("Ping", lambda *_: self.run_quick_tool("Ping", self.quick_tool_commands()["Ping"])),
            ("Traceroute", lambda *_: self.run_quick_tool("Traceroute", self.quick_tool_commands()["Traceroute"])),
            ("DNS lookup", lambda *_: self.run_quick_tool("DNS lookup", self.quick_tool_commands()["DNS lookup"])),
            ("Check porta TCP", lambda *_: self.run_quick_tool("Check porta TCP", self.quick_tool_commands()["Check porta TCP"])),
            ("Route", lambda *_: self.run_quick_tool("Route", self.quick_tool_commands()["Route"])),
            ("Socket aperti", lambda *_: self.run_quick_tool("Socket aperti", self.quick_tool_commands()["Socket aperti"])),
        ]))
        menubar.append(self.make_menu("Strumenti", [
            ("Tunneling SSH", lambda *_: self.on_tunnel_manager(None)),
            ("Password Manager", lambda *_: self.on_password_manager(None)),
            ("Esporta backup cifrato delle password", lambda *_: self.export_vault_encrypted()),
            ("Importa backup cifrato delle password", lambda *_: self.import_vault_encrypted()),
            ("Blocca password", lambda *_: self.on_lock_vault(None)),
            ("---", None),
            ("Chiavi SSH", lambda *_: self.run_quick_tool("Chiavi SSH", self.quick_tool_commands()["Chiavi SSH"])),
            ("Known hosts", lambda *_: self.run_quick_tool("Known hosts", self.quick_tool_commands()["Known hosts"])),
            ("Journal", lambda *_: self.run_quick_tool("Journal", self.quick_tool_commands()["Journal"])),
            ("Info sistema", lambda *_: self.run_quick_tool("Info sistema", self.quick_tool_commands()["Info sistema"])),
            ("Log applicazione", lambda *_: self.open_application_log()),
            ("Diagnostica compatibilità", lambda *_: self.show_diagnostics_dialog()),
        ]))
        menubar.append(self.make_menu("Vista", [
            ("Mostra/nascondi menu", lambda *_: self.toggle_menubar()),
            ("Mostra/nascondi barra comandi", lambda *_: self.toggle_ribbon()),
            ("Mostra/nascondi pannello sessioni", lambda *_: self.toggle_sidebar()),
            ("Mostra/nascondi barra verticale", lambda *_: self.toggle_left_rail()),
            ("Mostra/nascondi barra di stato", lambda *_: self.toggle_statusbar()),
            ("Mostra/nascondi suggerimento area lavoro", lambda *_: self.toggle_workspace_hint()),
            ("---", None),
            ("Espandi cartelle", lambda *_: self.session_tree.expand_all()),
            ("Comprimi cartelle", lambda *_: self.session_tree.collapse_all()),
        ]))
        menubar.append(self.make_menu("Impostazioni", [
            ("Apri impostazioni", lambda *_: self.show_settings_dialog()),
            ("Esporta impostazioni", lambda *_: self.export_settings()),
            ("Importa impostazioni", lambda *_: self.import_settings()),
        ]))
        menubar.append(self.make_menu("Aiuto", [
            ("Guida", lambda *_: self.open_user_guide()),
            ("Scorciatoie da tastiera", lambda *_: self.show_shortcuts_dialog()),
            ("Informazioni", lambda *_: self.show_about_dialog()),
        ]))
        root.pack_start(menubar, False, False, 0)

        ribbon = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        self.ribbon = ribbon
        ribbon.set_name("ribbon")
        root.pack_start(ribbon, False, False, 0)

        # HUD 3.7: pochi pulsanti padre, niente duplicati. Ogni pulsante apre
        # la propria tendina; le azioni specifiche restano nei menu padre.
        def tb(app_name: str, fallback):
            reg = getattr(self, "_registered_app_icons", set())
            return app_name if app_name in reg else fallback

        ribbon.pack_start(self.make_ribbon_group("Menu principali", [
            ("Terminale", tb("fth-tb-terminale", ["utilities-terminal", "utilities-terminal-symbolic"]), lambda b: self.show_hud_menu(b, "terminal")),
            ("Sessioni", tb("fth-tb-sessioni", ["network-server", "network-server-symbolic"]), lambda b: self.show_hud_menu(b, "sessions")),
            ("Rete", tb("fth-tb-rete", ["network-workgroup", "preferences-system-network", "network-workgroup-symbolic"]), lambda b: self.show_hud_menu(b, "network")),
            ("Strumenti", tb("fth-tb-strumenti", ["applications-system", "applications-system-symbolic"]), lambda b: self.show_hud_menu(b, "tools")),
            ("Password", tb("fth-tb-password", ["dialog-password-symbolic", "dialog-password"]), lambda _b: self.on_password_manager(None)),
            ("Vista", tb("fth-tb-vista", ["view-grid-symbolic", "preferences-desktop"]), lambda b: self.show_hud_menu(b, "view")),
            ("Sistema", tb("fth-tb-sistema", ["preferences-system", "emblem-system", "preferences-system-symbolic"]), lambda b: self.show_hud_menu(b, "system")),
        ]), False, False, 0)
        # Il Lab è una funzione autonoma e ad alta utilità: accesso diretto,
        # senza aggiungere un altro pannello permanente o mescolarlo ai menu rete.
        ribbon.pack_start(
            self.make_ribbon_button(
                "Lab",
                tb("fth-tb-lab", ["computer-symbolic", "computer", "system-run-symbolic"]),
                lambda _button: self.open_ephemeral_lab(),
                css_class="ribbon-button",
            ),
            False,
            False,
            2,
        )

        spacer = Gtk.Box()
        ribbon.pack_start(spacer, True, True, 0)
        # Il cambio tema è un'azione primaria esterna: non è più duplicato
        # nei menu Vista, Impostazioni o nella tendina HUD di Vista.
        ribbon.pack_end(self.make_theme_toggle_button(), False, False, 2)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.body = body
        body.set_name("body")
        root.pack_start(body, True, True, 0)

        rail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.rail = rail
        rail.set_name("left-rail")
        body.pack_start(rail, False, False, 0)
        # Barra sinistra ridotta a strumenti di navigazione, non a doppione
        # dell'HUD superiore.
        rail.pack_start(self.make_rail_button("Pannello sessioni", ["view-list-symbolic", "folder-symbolic"], lambda _b: self.focus_session_search()), False, False, 3)
        rail.pack_start(self.make_rail_button("Nascondi/mostra pannello", ["sidebar-show-symbolic", "pan-start-symbolic"], lambda _b: self.toggle_sidebar()), False, False, 3)
        rail.pack_start(self.make_rail_button("Log applicazione", ["text-x-log-symbolic", "text-x-generic-symbolic"], lambda _b: self.open_application_log()), False, False, 3)
        rail_spacer = Gtk.Box()
        rail.pack_start(rail_spacer, True, True, 0)
        rail.pack_start(self.make_rail_button("Blocca password", ["changes-prevent", "dialog-password", "changes-prevent-symbolic"], lambda _b: self.on_lock_vault(None)), False, False, 3)

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        body.pack_start(self.paned, True, True, 0)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.sidebar = sidebar
        sidebar.set_name("sidebar")
        sidebar.set_size_request(190, -1)
        self.paned.pack1(sidebar, False, False)
        GLib.idle_add(lambda: (self.paned.set_position(max(190, self.settings.sidebar_width)), False)[1])

        # Quick Connect sopra l'albero delle sessioni per accesso immediato.
        quick_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        quick_box.get_style_context().add_class("quick-box")
        self.quick = Gtk.Entry()
        self.quick.set_placeholder_text("Quick connect…")
        self.quick.set_hexpand(True)
        self.quick.get_style_context().add_class("quick-entry")
        self.quick.connect("activate", self.on_quick_connect)
        quick_box.pack_start(self.quick, True, True, 0)
        btn_quick = Gtk.Button.new_from_icon_name("network-transmit-receive-symbolic", Gtk.IconSize.MENU)
        btn_quick.set_relief(Gtk.ReliefStyle.NONE)
        btn_quick.set_tooltip_text("Connetti")
        btn_quick.get_style_context().add_class("quick-connect-button")
        btn_quick.connect("clicked", self.on_quick_connect)
        quick_box.pack_start(btn_quick, False, False, 0)
        sidebar.pack_start(quick_box, False, False, 0)

        # Il pannello laterale resta compatto: la seconda scheda compare
        # soltanto quando la tab attiva è una sessione SSH/SFTP.
        self.sidebar_notebook = Gtk.Notebook()
        self.sidebar_notebook.set_scrollable(False)
        self.sidebar_notebook.set_show_border(False)
        self.sidebar_notebook.set_tab_pos(Gtk.PositionType.TOP)
        self.sidebar_notebook.get_style_context().add_class("sidebar-pages")
        sidebar.pack_start(self.sidebar_notebook, True, True, 0)

        session_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.sidebar_sessions_page = session_page
        self.sidebar_notebook.append_page(session_page, Gtk.Label(label="Sessioni"))

        if RemoteFilesSidebar is not None:
            self.remote_files_sidebar = RemoteFilesSidebar(self)
            self.remote_files_sidebar.follow_button.set_active(
                self.settings.follow_remote_directory
            )
            self.remote_files_sidebar.on_follow_changed = self._on_follow_remote_changed
            self.remote_files_tab_label = Gtk.Label(label="File remoti")
            self.remote_files_tab_label.set_no_show_all(True)
            self.remote_files_tab_label.hide()
            self.remote_files_page_num = self.sidebar_notebook.append_page(
                self.remote_files_sidebar, self.remote_files_tab_label
            )
        else:
            self.remote_files_sidebar = None
            self.remote_files_tab_label = None
            self.remote_files_page_num = -1
        self.sidebar_notebook.set_show_tabs(False)

        side_titlebar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        side_titlebar.get_style_context().add_class("side-titlebar")
        session_page.pack_start(side_titlebar, False, False, 0)
        side_title = Gtk.Label(label="Sessioni", xalign=0)
        side_title.get_style_context().add_class("side-title")
        side_titlebar.pack_start(side_title, True, True, 0)
        # Il conteggio non occupa spazio nella sidebar: è disponibile da
        # tasto destro → Dettagli, come in un session manager compatto.
        self.session_count_label = Gtk.Label(label="")
        self.session_count_label.set_no_show_all(True)
        collapse_sidebar = Gtk.Button.new_from_icon_name("pan-start-symbolic", Gtk.IconSize.MENU)
        collapse_sidebar.set_relief(Gtk.ReliefStyle.NONE)
        collapse_sidebar.set_tooltip_text("Nascondi pannello sessioni")
        collapse_sidebar.get_style_context().add_class("sidebar-collapse")
        collapse_sidebar.connect("clicked", lambda *_: self.toggle_sidebar(False))
        side_titlebar.pack_start(collapse_sidebar, False, False, 0)

        self.session_filter = Gtk.SearchEntry()
        self.session_filter.set_placeholder_text("Cerca sessioni…")
        self.session_filter.connect("search-changed", self.on_session_filter_changed)
        self.session_filter.get_style_context().add_class("session-search")
        session_page.pack_start(self.session_filter, False, False, 0)

        session_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=1)
        session_toolbar.get_style_context().add_class("session-toolbar")
        self.session_action_buttons: Dict[str, Gtk.Button] = {}
        registered = getattr(self, "_registered_app_icons", set())

        def action_icon(app_name: str, fallback: str) -> str:
            return app_name if app_name in registered else fallback

        for label, icon, cb in [
            ("Nuova sessione", action_icon("fth-act-new", "list-add-symbolic"), self.on_add_session),
            ("Nuova cartella", action_icon("fth-act-folder-new", "folder-new-symbolic"), self.on_add_folder),
            ("Modifica", action_icon("fth-act-edit", "document-edit-symbolic"), self.on_edit_selected),
            ("Duplica", action_icon("fth-act-duplicate", "edit-copy-symbolic"), self.on_duplicate_selected),
            ("Sposta", "go-jump-symbolic", self.on_move_selected),
            ("Elimina", action_icon("fth-act-delete", "user-trash-symbolic"), self.on_delete_selected),
        ]:
            action = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
            action.set_relief(Gtk.ReliefStyle.NONE)
            action.set_tooltip_text(label)
            action.get_style_context().add_class("mini-action")
            action.connect("clicked", cb)
            self.session_action_buttons[label] = action
            session_toolbar.pack_start(action, True, True, 0)
        session_page.pack_start(session_toolbar, False, False, 0)

        self.tree_store = Gtk.TreeStore(str, str, str, str, str, str, int, bool, GdkPixbuf.Pixbuf)
        self.session_tree = Gtk.TreeView(model=self.tree_store)
        self.session_tree.set_name("session-tree")
        self.session_tree.set_headers_visible(False)
        self.session_tree.set_level_indentation(11)
        self.session_tree.set_show_expanders(True)
        self.session_tree.set_enable_tree_lines(False)
        self.session_tree.set_activate_on_single_click(False)
        self.session_tree.set_tooltip_text(
            "Doppio clic per aprire. Trascina una sessione sopra una cartella per spostarla; "
            "trascinala prima o dopo un'altra voce per riordinarla."
        )
        self.session_tree.connect("row-activated", self.on_session_activated)
        self.session_tree.connect("button-press-event", self.on_tree_button_press)
        self.session_tree.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        self.session_tree.get_selection().connect("changed", self.on_session_selection_changed)

        # Drag & drop interno: trascina sessioni e cartelle direttamente nella
        # struttura. Il payload contiene solo il percorso logico del nodo e non
        # dati sensibili della sessione.
        dnd_targets = [Gtk.TargetEntry.new(self.DND_TARGET, Gtk.TargetFlags.SAME_APP, 0)]
        self.session_tree.drag_source_set(
            Gdk.ModifierType.BUTTON1_MASK, dnd_targets, Gdk.DragAction.MOVE
        )
        self.session_tree.drag_dest_set(
            Gtk.DestDefaults.MOTION | Gtk.DestDefaults.DROP,
            dnd_targets,
            Gdk.DragAction.MOVE,
        )
        self.session_tree.connect("drag-begin", self.on_session_drag_begin)
        self.session_tree.connect("drag-data-get", self.on_session_drag_data_get)
        self.session_tree.connect("drag-motion", self.on_session_drag_motion)
        self.session_tree.connect("drag-data-received", self.on_session_drag_data_received)
        self.session_tree.connect("drag-leave", self.on_session_drag_leave)
        self.session_tree.connect("drag-end", self.on_session_drag_end)
        self._drag_source_store_path: Optional[str] = None
        self._drag_hover_expand_id: Optional[int] = None
        self._drag_hover_tree_path: Optional[str] = None
        self._drag_hover_store_path: Optional[str] = None
        self._drag_icon_widget: Optional[Gtk.Widget] = None

        renderer_icon = Gtk.CellRendererPixbuf()
        renderer_icon.set_property("xpad", 5)
        renderer_icon.set_property("ypad", 1)
        renderer_name = Gtk.CellRendererText()
        renderer_name.set_property("ellipsize", Pango.EllipsizeMode.END)
        renderer_name.set_property("ypad", 2)
        renderer_name.set_property("xpad", 3)
        col_name = Gtk.TreeViewColumn("Sessione")
        col_name.pack_start(renderer_icon, False)
        col_name.set_cell_data_func(renderer_icon, self._session_tree_icon_data)
        col_name.pack_start(renderer_name, True)
        col_name.add_attribute(renderer_name, "markup", self.COL_NAME)
        col_name.add_attribute(renderer_name, "xpad", self.COL_TEXT_XPAD)
        col_name.set_cell_data_func(renderer_name, self._session_tree_text_data)
        col_name.set_expand(True)
        self.session_tree.append_column(col_name)
        self.session_tree.set_expander_column(col_name)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.get_style_context().add_class("session-scroll")
        scroll.add(self.session_tree)
        session_page.pack_start(scroll, True, True, 0)

        side_footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        side_footer.get_style_context().add_class("side-footer")
        btn_expand = Gtk.Button.new_from_icon_name("pan-down-symbolic", Gtk.IconSize.MENU)
        btn_expand.set_tooltip_text("Espandi tutte le cartelle")
        btn_expand.set_relief(Gtk.ReliefStyle.NONE)
        btn_expand.get_style_context().add_class("mini-action")
        btn_expand.connect("clicked", lambda _b: self.session_tree.expand_all())
        btn_collapse = Gtk.Button.new_from_icon_name("pan-end-symbolic", Gtk.IconSize.MENU)
        btn_collapse.set_tooltip_text("Comprimi tutte le cartelle")
        btn_collapse.set_relief(Gtk.ReliefStyle.NONE)
        btn_collapse.get_style_context().add_class("mini-action")
        btn_collapse.connect("clicked", lambda _b: self.session_tree.collapse_all())
        side_footer.pack_start(btn_expand, True, True, 0)
        side_footer.pack_start(btn_collapse, True, True, 0)
        session_page.pack_start(side_footer, False, False, 0)

        workspace = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.workspace = workspace
        workspace.set_name("workspace")
        self.paned.pack2(workspace, True, True)

        self.notification_revealer = Gtk.Revealer()
        self.notification_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.notification_revealer.set_transition_duration(180 if self.settings.animations and not SAFE_MODE else 0)
        notification = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        notification.get_style_context().add_class("notification-banner")
        self.notification_icon = Gtk.Image.new_from_icon_name("dialog-information-symbolic", Gtk.IconSize.MENU)
        self.notification_text = Gtk.Label(label="", xalign=0)
        self.notification_text.set_line_wrap(True)
        self.notification_text.set_ellipsize(Pango.EllipsizeMode.END)
        close_notification = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close_notification.set_relief(Gtk.ReliefStyle.NONE)
        close_notification.set_tooltip_text("Chiudi questa notifica")
        close_notification.connect("clicked", lambda *_: self.hide_notification())
        notification.pack_start(self.notification_icon, False, False, 0)
        notification.pack_start(self.notification_text, True, True, 0)
        notification.pack_start(close_notification, False, False, 0)
        self.notification_revealer.add(notification)
        workspace.pack_start(self.notification_revealer, False, False, 0)

        tabbar_hint = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.tabbar_hint = tabbar_hint
        tabbar_hint.get_style_context().add_class("workspace-strip")
        terminal_icon = Gtk.Label(label="▸")
        terminal_icon.get_style_context().add_class("green-dot")
        tabbar_hint.pack_start(terminal_icon, False, False, 0)
        hint = Gtk.Label(label="Doppio clic per aprire una sessione", xalign=0)
        hint.get_style_context().add_class("workspace-hint")
        tabbar_hint.pack_start(hint, True, True, 0)
        workspace.pack_start(tabbar_hint, False, False, 0)

        self.notebook = Gtk.Notebook()
        self.notebook.set_name("terminal-notebook")
        self.notebook.set_scrollable(True)
        self.notebook.set_tab_pos(Gtk.PositionType.TOP)
        self.notebook.connect("switch-page", self.on_workspace_tab_switched)
        self.notebook.connect("page-removed", self.on_workspace_page_removed)
        workspace.pack_start(self.notebook, True, True, 0)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.footer = footer
        footer.set_name("bottom-status")
        self.status = Gtk.Label(label="Pronto.", xalign=0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        footer.pack_start(self.status, True, True, 6)
        version = Gtk.Label(label=f"{APP_NAME} {APP_VERSION}  •  Fedora workstation tools" + ("  •  SAFE MODE" if SAFE_MODE else ""))
        version.get_style_context().add_class("footer-version")
        footer.pack_start(version, False, False, 8)
        root.pack_start(footer, False, False, 0)

    def build_diagnostics_text(self) -> str:
        lines = [
            f"FedTermHub {APP_VERSION} - Diagnostica",
            "=" * 38,
            f"Python: {sys.version.split()[0]} ({sys.executable})",
            f"Sistema: {platform.platform()}",
            f"Sessione: {os.environ.get('XDG_SESSION_TYPE', '-')}",
            f"Desktop: {os.environ.get('XDG_CURRENT_DESKTOP', '-')}",
            f"DISPLAY: {os.environ.get('DISPLAY', '-')}",
            f"WAYLAND_DISPLAY: {os.environ.get('WAYLAND_DISPLAY', '-')}",
            f"Safe mode: {'ON' if SAFE_MODE else 'OFF'}",
            f"Animazioni: {'ON' if self.settings.animations else 'OFF'}",
            f"Popup errori: {'ON' if self.settings.show_error_popups else 'OFF'}",
            f"Schede errore: {'ON' if self.settings.open_error_tabs else 'OFF'}",
            f"Preflight RDP: {'ON' if self.settings.rdp_preflight else 'OFF'}",
            f"Timeout connessioni: {self.settings.connection_timeout_seconds}s",
            f"SSH keepalive: {self.settings.ssh_keepalive_interval}s x {self.settings.ssh_keepalive_count}",
            f"Vault auto-lock: {self.settings.vault_auto_lock_minutes} min",
            f"Tema icone richiesto: {self.settings.icon_theme}",
            f"Tema icone attivo: {getattr(self, 'active_icon_theme', '-')}",
            f"Layout: icone {self.settings.toolbar_icon_size}px, sidebar {self.settings.sidebar_width}px",
            f"Terminale: {self.settings.terminal_font_family} {self.settings.terminal_font_size}, scrollback {self.settings.terminal_scrollback_lines}",
            f"Temi icone disponibili: {', '.join(available_icon_themes()[:18]) or '-'}",
            f"Config: {CONFIG_DIR}",
            f"Log: {LOG_FILE}",
            "",
            "Dipendenze Python:",
            "  GTK/GObject: OK",
            "  VTE: OK",
            f"  cryptography: {'OK' if CRYPTO_AVAILABLE else 'MANCANTE'}",
        ]
        try:
            import serial
            serial_ok = serial is not None
        except Exception:
            serial_ok = False
        lines.append(f"  pyserial: {'OK' if serial_ok else 'MANCANTE'}")
        lines.append("")
        lines.append("Binari esterni:")
        for binary, pkg in [
            ("ssh", "openssh-clients"),
            ("sftp", "openssh-clients"),
            ("sshpass", "sshpass"),
            ("xfreerdp3", "freerdp"),
            ("xfreerdp", "freerdp"),
            ("wlfreerdp3", "freerdp"),
            ("wlfreerdp", "freerdp"),
            ("vncviewer", "tigervnc"),
            ("nmap", "nmap"),
            ("tcpdump", "tcpdump"),
            ("ss", "iproute"),
            ("sshd", "openssh-server"),
            ("vsftpd", "vsftpd"),
            ("in.tftpd", "tftp-server"),
            ("in.telnetd", "telnet-server"),
            ("exportfs", "nfs-utils"),
            ("x0vncserver", "tigervnc-x11-server"),
            ("w0vncserver", "tigervnc-wayland-server"),
            ("vncpasswd", "tigervnc-server-common"),
            ("firewall-cmd", "firewalld"),
            ("virsh", "libvirt-client"),
            ("virt-install", "virt-install"),
            ("virt-viewer", "virt-viewer"),
            ("qemu-img", "qemu-img"),
        ]:
            path = shutil.which(binary)
            lines.append(f"  {binary:<10} {'OK  ' + path if path else 'MANCANTE  sudo dnf install ' + pkg}")
        lines.append("")
        lines.append(f"Bridge focus X11: {'OK' if X11FocusBridge._load() else 'NON DISPONIBILE'}")
        lines.append("")
        lines.append("Porte seriali rilevate:")
        ports = detect_serial_ports()
        if ports:
            lines.extend([f"  {port}" for port in ports[:40]])
        else:
            lines.append("  Nessuna porta seriale rilevata ora.")
        lines.append("")
        lines.append("Avvio safe mode:")
        lines.append("  fedtermhub --safe-mode")
        lines.append("Doctor da terminale:")
        lines.append("  fedtermhub --doctor")
        return "\n".join(lines)

    def show_diagnostics_dialog(self) -> None:
        """Show a diagnostics viewer robust on KDE/X11/Wayland using a ListBox."""
        dialog = Gtk.Dialog(title="Diagnostica compatibilità", transient_for=self, flags=0)
        dialog.set_modal(True)
        dialog.set_resizable(True)
        dialog.set_default_size(900, 640)
        dialog.set_size_request(720, 460)
        dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        dialog.get_style_context().add_class("modern-dialog")
        dialog.get_style_context().add_class("diagnostics-dialog")
        apply_window_icon(dialog)
        dialog.connect("delete-event", lambda d, _e: (d.response(Gtk.ResponseType.CLOSE), True)[1])

        content = dialog.get_content_area()
        content.set_spacing(0)
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=14)
        shell.get_style_context().add_class("diagnostics-shell")
        content.add(shell)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.get_style_context().add_class("diagnostics-header")
        icon = self.make_icon_image([APP_ICON_NAME, "dialog-information-symbolic"], 42)
        header.pack_start(icon, False, False, 0)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label="Diagnostica compatibilità", xalign=0)
        title.get_style_context().add_class("dialog-heading")
        subtitle = Gtk.Label(
            label="Controlli di sistema, dipendenze, backend RDP, strumenti esterni e porte seriali.",
            xalign=0,
        )
        subtitle.set_line_wrap(True)
        subtitle.get_style_context().add_class("muted")
        title_box.pack_start(title, False, False, 0)
        title_box.pack_start(subtitle, False, False, 0)
        header.pack_start(title_box, True, True, 0)
        shell.pack_start(header, False, False, 0)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        toolbar.get_style_context().add_class("diagnostics-toolbar")
        search = Gtk.SearchEntry()
        search.set_placeholder_text("Cerca nel rapporto diagnostico…")
        search.set_hexpand(True)
        toolbar.pack_start(search, True, True, 0)
        refresh_button = self.make_button("Aggiorna", ["view-refresh-symbolic", "view-refresh"])
        copy_button = self.make_button("Copia rapporto", ["edit-copy-symbolic", "edit-copy"])
        export_button = self.make_button("Esporta", ["document-save-symbolic", "document-save"])
        toolbar.pack_start(refresh_button, False, False, 0)
        toolbar.pack_start(copy_button, False, False, 0)
        toolbar.pack_start(export_button, False, False, 0)
        shell.pack_start(toolbar, False, False, 0)

        card = Gtk.Frame()
        card.get_style_context().add_class("diagnostics-card")
        shell.pack_start(card, True, True, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(380)
        card.add(scroll)

        viewport = Gtk.Viewport()
        viewport.set_shadow_type(Gtk.ShadowType.NONE)
        scroll.add(viewport)

        lines_box = Gtk.ListBox()
        lines_box.set_selection_mode(Gtk.SelectionMode.NONE)
        lines_box.get_style_context().add_class("diagnostics-listbox")
        viewport.add(lines_box)

        report_holder = {"text": ""}
        all_rows: List[Tuple[Gtk.ListBoxRow, str]] = []

        def populate(*_args) -> None:
            report = self.build_diagnostics_text()
            report_holder["text"] = report
            for child in list(lines_box.get_children()):
                lines_box.remove(child)
            all_rows.clear()
            lines = report.splitlines()
            first_line = lines[0] if lines else ""
            for raw_line in lines:
                stripped = raw_line.strip()
                if set(stripped) == {"="}:
                    continue
                row = Gtk.ListBoxRow()
                row.set_activatable(False)
                row.set_selectable(False)
                row.get_style_context().add_class("diagnostics-row")
                label = Gtk.Label(xalign=0, selectable=True)
                label.set_line_wrap(True)
                label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                label.set_margin_top(3)
                label.set_margin_bottom(3)
                label.set_margin_start(8)
                label.set_margin_end(8)
                if not stripped:
                    label.set_text(" ")
                elif raw_line == first_line:
                    label.set_markup(f'<span size="large" weight="bold">{GLib.markup_escape_text(raw_line)}</span>')
                elif stripped.endswith(":") and not raw_line.startswith("  "):
                    label.set_markup(f'<span weight="bold">{GLib.markup_escape_text(stripped)}</span>')
                elif "MANCANTE" in stripped or "ERRORE" in stripped or "NON DISPONIBILE" in stripped:
                    label.set_markup(f'<span weight="bold">⚠ {GLib.markup_escape_text(raw_line)}</span>')
                elif "OK" in stripped or "ON" in stripped:
                    label.set_markup(f'✓ {GLib.markup_escape_text(raw_line)}')
                else:
                    label.set_text(raw_line)
                row.add(label)
                lines_box.add(row)
                all_rows.append((row, raw_line.casefold()))
            lines_box.show_all()
            apply_filter()

        def apply_filter(*_args) -> None:
            query = search.get_text().strip().casefold()
            for row, hay in all_rows:
                row.set_visible((not query) or (query in hay))

        def copy_report(_button) -> None:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(report_holder["text"], -1)
            self.show_notification("Rapporto diagnostico copiato negli appunti.", "success", 3000)

        def export_report(_button) -> None:
            chooser = Gtk.FileChooserDialog(title="Esporta diagnostica", transient_for=dialog, action=Gtk.FileChooserAction.SAVE)
            apply_window_icon(chooser)
            chooser.add_buttons("Annulla", Gtk.ResponseType.CANCEL, "Salva", Gtk.ResponseType.OK)
            chooser.set_current_name(f"fedtermhub-diagnostica-{APP_VERSION}.txt")
            chooser.set_do_overwrite_confirmation(True)
            try:
                if chooser.run() == Gtk.ResponseType.OK:
                    Path(chooser.get_filename()).write_text(report_holder["text"] + "\n", encoding="utf-8")
                    self.show_notification("Rapporto diagnostico esportato.", "success", 3000)
            except Exception as exc:
                self.show_notification(f"Esportazione diagnostica non riuscita: {exc}", "error", 6000)
            finally:
                chooser.destroy()

        search.connect("search-changed", apply_filter)
        refresh_button.connect("clicked", populate)
        copy_button.connect("clicked", copy_report)
        export_button.connect("clicked", export_report)

        dialog.add_button("Chiudi", Gtk.ResponseType.CLOSE)
        populate()
        dialog.show_all()
        try:
            dialog.run()
        finally:
            dialog.destroy()

    def set_theme_from_menu(self, theme: str) -> None:
        self.settings.theme = theme
        self.settings.save(SETTINGS_FILE)
        self.refresh_icon_theme_runtime()
        self.apply_theme(theme)
        self.refresh_theme_toggle_button()
        self.set_status(f"Tema applicato: {'Scuro' if theme == 'dark' else 'Chiaro'} — icone: {getattr(self, 'active_icon_theme', '-')}")

    def cycle_theme(self) -> None:
        self.set_theme_from_menu("light" if self.settings.theme == "dark" else "dark")

    def show_about_dialog(self) -> None:
        # Gtk.AboutDialog con website button può restare appeso su alcuni temi/WM;
        # usiamo un Dialog normale con chiusura esplicita e delete-event gestito.
        dialog = Gtk.Dialog(title=f"Informazioni su {APP_NAME}", transient_for=self, flags=0)
        dialog.set_modal(True)
        dialog.add_buttons("Chiudi", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(520, 260)
        dialog.connect("delete-event", lambda d, _e: (d.response(Gtk.ResponseType.CLOSE), True)[1])
        box = dialog.get_content_area()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=14)
        box.add(outer)
        title = Gtk.Label(label=f"{APP_NAME} {APP_VERSION}", xalign=0)
        title.get_style_context().add_class("dialog-heading")
        outer.pack_start(title, False, False, 0)
        text = Gtk.Label(
            label=(
                "Gestore integrato di terminali e connessioni remote per Fedora.\n\n"
                "Funzioni: SSH, RDP integrato con focus tastiera, VNC, SFTP, seriale, Password Manager cifrato, tunnel SSH, split terminal e MultiExec.\n\n"
                "Configurazione locale: ~/.config/fedtermhub"
            ),
            xalign=0,
        )
        text.set_line_wrap(True)
        outer.pack_start(text, True, True, 0)
        dialog.show_all()
        try:
            dialog.run()
        finally:
            dialog.destroy()

    def show_shortcuts_dialog(self) -> None:
        dialog = Gtk.Dialog(title="Scorciatoie da tastiera", transient_for=self, flags=0)
        dialog.set_modal(True)
        dialog.add_buttons("Chiudi", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(560, 640)
        dialog.get_style_context().add_class("modern-dialog")
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=18)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.add(outer)
        dialog.get_content_area().pack_start(scroller, True, True, 0)
        title = Gtk.Label(label="Scorciatoie da tastiera", xalign=0)
        title.get_style_context().add_class("dialog-heading")
        outer.pack_start(title, False, False, 0)
        grid = Gtk.Grid(column_spacing=20, row_spacing=9)
        grid.get_style_context().add_class("form-card")
        outer.pack_start(grid, True, True, 0)
        # Stesso elenco della guida (docs/GUIDA.md): prima qui mancavano Ctrl+K, Ctrl+H e tutte le
        # scorciatoie del terminale.
        sections = [
            ("Applicazione (con il fuoco fuori dal terminale)", [
                ("Ctrl+K", "Cerca e apri una sessione"),
                ("Ctrl+Shift+N", "Nuova sessione"),
                ("Ctrl+L", "Connessione rapida"),
                ("Ctrl+T", "Nuovo terminale locale"),
                ("Ctrl+W", "Chiudi la scheda"),
                ("Ctrl+B", "Mostra o nascondi il pannello sessioni"),
                ("Ctrl+H", "File remoti: mostra i file nascosti"),
                ("Ctrl+Shift+T", "Gestore tunnel SSH"),
                ("Ctrl+Shift+V", "Password Manager"),
                ("Ctrl+,", "Impostazioni"),
                ("F11", "Schermo intero"),
            ]),
            ("Nel terminale", [
                ("Ctrl+Shift+C / V", "Copia / incolla"),
                ("Ctrl+Shift+O / E", "Dividi affiancato / sopra e sotto"),
                ("Ctrl+Shift+Invio", "Nuovo riquadro"),
                ("Ctrl+Shift+W", "Chiudi il riquadro (l'ultimo chiude la scheda)"),
                ("Ctrl+Shift+[ / ]", "Riquadro precedente / successivo"),
                ("Ctrl+Shift+L", "Cambia disposizione dei riquadri"),
                ("Ctrl+Shift+P", "Scrivi in tutti i riquadri insieme"),
                ("Ctrl+Shift+F", "Cerca nel testo"),
                ("Ctrl+Shift+H", "Apri un indirizzo web da tastiera"),
                ("Ctrl+Shift+R", "Riconnetti"),
                ("Ctrl + / - / 0", "Ingrandisci / riduci / dimensione normale"),
            ]),
        ]
        row = 0
        for heading, shortcuts in sections:
            section = Gtk.Label(label=heading, xalign=0)
            section.get_style_context().add_class("section-heading")
            grid.attach(section, 0, row, 2, 1)
            row += 1
            for keys, action in shortcuts:
                key_lbl = Gtk.Label(label=keys, xalign=0)
                key_lbl.get_style_context().add_class("shortcut-key")
                grid.attach(key_lbl, 0, row, 1, 1)
                grid.attach(Gtk.Label(label=action, xalign=0), 1, row, 1, 1)
                row += 1
        note = Gtk.Label(label="Nel terminale le combinazioni Ctrl+lettera vanno al programma (bash, nano, tmux). Nella sessione RDP F11 ed Esc controllano lo schermo intero; gli altri tasti vanno al desktop remoto.", xalign=0)
        note.set_line_wrap(True)
        note.get_style_context().add_class("info-card")
        outer.pack_start(note, False, False, 0)
        dialog.show_all()
        try:
            dialog.run()
        finally:
            dialog.destroy()

    def open_user_guide(self) -> None:
        """Apre la guida: quella installata con il pacchetto, altrimenti quella online."""
        candidates = [
            Path("/usr/share/doc/fedtermhub/GUIDA.md"),
            Path(__file__).resolve().parent.parent / "docs" / "GUIDA.md",
        ]
        uri = next((c.as_uri() for c in candidates if c.is_file()), "https://github.com/Crash-FD01/FedTermHub/blob/main/docs/GUIDA.md")
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception as exc:
            self.set_status(f"Impossibile aprire la guida: {exc}")

    def open_application_log(self) -> None:
        try:
            text = LOG_FILE.read_text(encoding="utf-8") if LOG_FILE.exists() else "Il log applicativo è ancora vuoto."
        except Exception as exc:
            text = f"Impossibile leggere il log: {exc}"
        self.add_text_tab("Log FedTermHub", text)

    def show_settings_dialog(self) -> None:
        # Gtk.Dialog.run() rende la finestra modale con un grab sugli input:
        # finché le opzioni restano aperte la finestra principale (inclusa la
        # sessione RDP) non riceve più eventi e appare "congelata". La finestra
        # viene quindi mostrata non modale, con la risposta gestita in modo
        # asincrono: la sessione resta pienamente utilizzabile.
        existing = getattr(self, "_settings_dialog", None)
        if existing is not None:
            existing.present()
            return
        dialog = SettingsDialog(self)
        dialog.set_modal(False)
        self._settings_dialog = dialog

        def _on_response(dlg: Gtk.Dialog, response: int) -> None:
            try:
                if response == Gtk.ResponseType.OK:
                    dlg.apply()
            finally:
                self._settings_dialog = None
                dlg.destroy()

        dialog.connect("response", _on_response)
        dialog.show_all()

    def quick_tool_commands(self) -> Dict[str, str]:
        return {
            "Ping": 'read -rp \'Host da pingare [8.8.8.8]: \' h; h=${h:-8.8.8.8}; ping -c 5 "$h"; echo; read -rp \'Invio per chiudere...\' _',
            "Traceroute": 'read -rp \'Host traceroute [8.8.8.8]: \' h; h=${h:-8.8.8.8}; tracepath "$h" || traceroute "$h"; echo; read -rp \'Invio per chiudere...\' _',
            "DNS lookup": 'read -rp \'Nome DNS [example.com]: \' h; h=${h:-example.com}; dig "$h" || nslookup "$h"; echo; read -rp \'Invio per chiudere...\' _',
            "Check porta TCP": 'read -rp \'Host: \' h; read -rp \'Porta: \' p; timeout 4 bash -c "</dev/tcp/$h/$p" && echo \'Porta aperta\' || echo \'Porta chiusa/non raggiungibile\'; echo; read -rp \'Invio per chiudere...\' _',
            "IP locale": "ip -br addr; echo; read -rp 'Invio per chiudere...' _",
            "Route": "ip route; echo; read -rp 'Invio per chiudere...' _",
            "Socket aperti": "ss -tulpen; echo; read -rp 'Invio per chiudere...' _",
            "Chiavi SSH": "ls -lah ~/.ssh; echo; ssh-add -l 2>/dev/null || true; echo; read -rp 'Invio per chiudere...' _",
            "Known hosts": "${EDITOR:-nano} ~/.ssh/known_hosts",
            "Journal": "journalctl -f",
            "Info sistema": "hostnamectl; echo; lsb_release -a 2>/dev/null || cat /etc/os-release; echo; read -rp 'Invio per chiudere...' _",
            "Doctor FedTermHub": "fedtermhub --doctor; echo; read -rp 'Invio per chiudere...' _",
        }

    def run_quick_tool(self, label: str, command: str) -> None:
        self.add_terminal_tab(label, [os.environ.get("SHELL") or "/bin/sh", "-lc", command], close_on_exit="always")

    def show_tools_dialog(self) -> None:
        self.show_tools_menu(None)


    def _add_popup_item(self, menu: Gtk.Menu, label: str, icon_names: Union[str, List[str], Tuple[str, ...]], callback: Callable[..., object]) -> None:
        item = self.make_image_menu_item(label, icon_names, 14)
        item.connect("activate", lambda *_: callback())
        menu.append(item)

    def _add_popup_heading(self, menu: Gtk.Menu, label: str) -> None:
        heading = Gtk.MenuItem(label=label)
        heading.set_sensitive(False)
        heading.get_style_context().add_class("dropdown-heading")
        menu.append(heading)

    def _show_popup_at_button(self, menu: Gtk.Menu, button: Optional[Gtk.Widget]) -> None:
        menu.show_all()
        if button is not None:
            try:
                menu.popup_at_widget(button, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)
                return
            except Exception:
                pass
        menu.popup_at_pointer(None)

    def show_hud_menu(self, button: Optional[Gtk.Widget], category: str) -> None:
        """Compact parent menus used by the 3.7 HUD.

        The HUD exposes only parent categories; concrete actions stay in the
        corresponding dropdown/menu so the top bar does not become crowded.
        """
        menu = Gtk.Menu()
        menu.set_name("tools-dropdown")

        def sep() -> None:
            menu.append(Gtk.SeparatorMenuItem())

        if category == "terminal":
            self._add_popup_heading(menu, "Terminale")
            self._add_popup_item(menu, "Nuovo terminale locale", ["utilities-terminal-symbolic", "utilities-terminal"], self.open_local_terminal)
            self._add_popup_item(menu, "Terminale diviso", ["view-split-left-right", "view-dual-symbolic"], self.open_split_terminal)
            self._add_popup_item(menu, "MultiExec", ["system-run-symbolic", "system-run"], self.show_multiexec_dialog)
            self._add_popup_item(menu, "Quick connect", ["network-transmit-receive-symbolic", "system-search-symbolic"], lambda: self.on_quick_connect())
        elif category == "sessions":
            self._add_popup_heading(menu, "Crea sessione")
            self._add_popup_item(menu, "Nuova sessione", ["list-add-symbolic", "network-server-symbolic"], lambda: self.on_add_session(None))
            self._add_popup_item(menu, "Nuova SSH", ["network-workgroup-symbolic", "network-workgroup"], lambda: self.on_add_proto_session("ssh"))
            self._add_popup_item(menu, "Nuova RDP", ["video-display-symbolic", "video-display"], lambda: self.on_add_proto_session("rdp"))
            self._add_popup_item(menu, "Nuova VNC", ["computer-symbolic", "video-display-symbolic"], lambda: self.on_add_proto_session("vnc"))
            self._add_popup_item(menu, "Nuova seriale", ["network-wired-symbolic", "input-keyboard-symbolic"], lambda: self.on_add_proto_session("serial"))
            self._add_popup_item(menu, "Nuova cartella", ["folder-new-symbolic", "folder-new"], lambda: self.on_add_folder(None))
            sep()
            self._add_popup_heading(menu, "Organizza")
            self._add_popup_item(menu, "Modifica selezionata", ["document-edit-symbolic", "document-edit"], lambda: self.on_edit_selected(None))
            self._add_popup_item(menu, "Duplica selezionata", ["edit-copy-symbolic", "edit-copy"], lambda: self.on_duplicate_selected(None))
            self._add_popup_item(menu, "Sposta selezionata", ["go-jump-symbolic", "go-next-symbolic"], lambda: self.on_move_selected(None))
            self._add_popup_item(menu, "Elimina selezionata", ["edit-delete-symbolic", "user-trash-symbolic"], lambda: self.on_delete_selected(None))
        elif category == "network":
            self._add_popup_heading(menu, "Server e servizi")
            self._add_popup_item(menu, "Server locali", ["network-server-symbolic", "network-server"], lambda: self.open_network_tools("services"))
            self._add_popup_item(menu, "FTP / SFTP share", ["folder-remote-symbolic", "folder-publicshare"], lambda: self.open_network_tools("shares"))
            self._add_popup_item(menu, "Porte locali in ascolto", ["network-transmit-receive-symbolic", "network-wired-symbolic"], lambda: self.open_network_tools("listeners"))
            sep()
            self._add_popup_heading(menu, "Analisi rete")
            self._add_popup_item(menu, "Wake-on-LAN", ["system-run-symbolic", "media-playback-start-symbolic"], lambda: self.open_network_tools("wol"))
            self._add_popup_item(menu, "Network scanner", ["system-search-symbolic", "edit-find-symbolic"], lambda: self.open_network_tools("network-scan"))
            self._add_popup_item(menu, "Port scanner", ["network-workgroup-symbolic", "system-search-symbolic"], lambda: self.open_network_tools("port-scan"))
            self._add_popup_item(menu, "Cattura pacchetti", ["network-transmit-receive-symbolic", "document-save-symbolic"], lambda: self.open_network_tools("capture"))
            sep()
            self._add_popup_heading(menu, "Test rapidi")
            commands = self.quick_tool_commands()
            for name in ("Ping", "Traceroute", "DNS lookup", "Check porta TCP", "Route", "Socket aperti"):
                self._add_popup_item(menu, name, ["system-run-symbolic", "applications-system-symbolic"], lambda n=name, c=commands[name]: self.run_quick_tool(n, c))
        elif category == "tools":
            self._add_popup_heading(menu, "Strumenti")
            self._add_popup_item(menu, "Tunneling SSH", ["network-vpn-symbolic", "network-transmit-receive-symbolic"], lambda: self.on_tunnel_manager(None))
            self._add_popup_item(menu, "Chiavi SSH", ["dialog-password-symbolic", "emblem-key-symbolic"], lambda: self.run_quick_tool("Chiavi SSH", self.quick_tool_commands()["Chiavi SSH"]))
            self._add_popup_item(menu, "Known hosts", ["text-x-generic-symbolic", "document-open-symbolic"], lambda: self.run_quick_tool("Known hosts", self.quick_tool_commands()["Known hosts"]))
            self._add_popup_item(menu, "Journal", ["text-x-log-symbolic", "text-x-generic-symbolic"], lambda: self.run_quick_tool("Journal", self.quick_tool_commands()["Journal"]))
            self._add_popup_item(menu, "Info sistema", ["dialog-information-symbolic", "computer-symbolic"], lambda: self.run_quick_tool("Info sistema", self.quick_tool_commands()["Info sistema"]))
        elif category == "view":
            self._add_popup_heading(menu, "Vista")
            self._add_popup_item(menu, "Mostra/nascondi pannello sessioni", ["sidebar-show-symbolic", "view-list-symbolic"], self.toggle_sidebar)
            self._add_popup_item(menu, "Mostra/nascondi barra verticale", ["view-sidebar-symbolic", "view-list-symbolic"], self.toggle_left_rail)
            self._add_popup_item(menu, "Mostra/nascondi barra di stato", ["view-list-symbolic", "format-justify-fill-symbolic"], self.toggle_statusbar)
            self._add_popup_item(menu, "Espandi cartelle", ["pan-down-symbolic", "go-down-symbolic"], self.session_tree.expand_all)
            self._add_popup_item(menu, "Comprimi cartelle", ["pan-end-symbolic", "go-next-symbolic"], self.session_tree.collapse_all)
        elif category == "system":
            self._add_popup_heading(menu, "Sicurezza")
            self._add_popup_item(menu, "Password Manager", ["dialog-password-symbolic", "dialog-password"], lambda: self.on_password_manager(None))
            self._add_popup_item(menu, "Blocca password", ["changes-prevent-symbolic", "changes-prevent"], lambda: self.on_lock_vault(None))
            sep()
            self._add_popup_heading(menu, "Sistema")
            self._add_popup_item(menu, "Impostazioni", ["preferences-system-symbolic", "preferences-system"], self.show_settings_dialog)
            self._add_popup_item(menu, "Diagnostica compatibilità", ["dialog-information-symbolic", "help-about-symbolic"], self.show_diagnostics_dialog)
            self._add_popup_item(menu, "Log applicazione", ["text-x-log-symbolic", "text-x-generic-symbolic"], self.open_application_log)
            self._add_popup_item(menu, "Guida", ["help-browser-symbolic", "help-contents"], self.open_user_guide)
            self._add_popup_item(menu, "Scorciatoie da tastiera", ["input-keyboard-symbolic", "preferences-desktop-keyboard"], self.show_shortcuts_dialog)
            self._add_popup_item(menu, "Informazioni", ["help-about-symbolic", "help-about"], self.show_about_dialog)
        else:
            self._add_popup_item(menu, "Impostazioni", ["preferences-system-symbolic"], self.show_settings_dialog)

        self._show_popup_at_button(menu, button)

    def show_tools_menu(self, button: Optional[Gtk.Widget] = None) -> None:
        menu = Gtk.Menu()
        menu.set_name("tools-dropdown")
        sections = [
            ("Analisi rapida", ["Ping", "Traceroute", "DNS lookup", "Check porta TCP", "IP locale", "Route", "Socket aperti"]),
            ("Sistema", ["Chiavi SSH", "Known hosts", "Journal", "Info sistema", "Doctor FedTermHub"]),
        ]
        commands = self.quick_tool_commands()
        for section_index, (title, names) in enumerate(sections):
            if section_index:
                menu.append(Gtk.SeparatorMenuItem())
            heading = Gtk.MenuItem(label=title)
            heading.set_sensitive(False)
            heading.get_style_context().add_class("dropdown-heading")
            menu.append(heading)
            for name in names:
                item = self.make_image_menu_item(name, ["system-run-symbolic", "applications-system-symbolic"], 14)
                item.connect("activate", lambda _i, n=name: self.run_quick_tool(n, commands[n]))
                menu.append(item)
        menu.show_all()
        if button is not None:
            try:
                menu.popup_at_widget(button, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)
                return
            except Exception:
                pass
        menu.popup_at_pointer(None)

    def show_left_view(self, view: str) -> None:
        if not self.sidebar.get_visible():
            self.toggle_sidebar(True)
        labels = {
            "sessions": "Vista Explorer: sessioni e cartelle",
            "network": "Vista Rete: apro il centro strumenti di rete",
            "monitor": "Vista Monitor: tunnel e diagnostica",
            "logs": "Vista Log: registro applicazione",
        }
        self.set_status(labels.get(view, "Vista aggiornata"))
        if view == "sessions":
            self.focus_session_search()
        elif view == "network":
            self.open_network_tools("services")
        elif view == "monitor":
            self.on_tunnel_manager(None)
        elif view == "logs":
            self.open_application_log()

    def build_command_palette_actions(self) -> List[Tuple[str, str, Callable[[], None]]]:
        actions: List[Tuple[str, str, Callable[[], None]]] = [
            ("Nuovo terminale locale", "terminal shell locale", lambda: self.open_local_terminal()),
            ("Nuova sessione", "crea connessione generica", lambda: self.on_add_session(None)),
            ("Nuova SSH", "crea sessione ssh", lambda: self.on_add_proto_session("ssh")),
            ("Nuova RDP", "crea sessione desktop remoto", lambda: self.on_add_proto_session("rdp")),
            ("Nuova VNC", "crea sessione vnc", lambda: self.on_add_proto_session("vnc")),
            ("Nuova seriale", "crea console seriale", lambda: self.on_add_proto_session("serial")),
            ("Gestore tunnel SSH", "tunnel port forwarding socks", lambda: self.on_tunnel_manager(None)),
            ("Password Manager", "vault credenziali password", lambda: self.on_password_manager(None)),
            ("Lab temporaneo Linux", "vm kvm libvirt qcow2 usa e getta", lambda: self.open_ephemeral_lab()),
            ("Server locali", "ssh sftp ftp http tftp nfs vnc", lambda: self.open_network_tools("services")),
            ("Porte locali in ascolto", "ss socket ascolto", lambda: self.open_network_tools("listeners")),
            ("Wake-on-LAN", "magic packet wol", lambda: self.open_network_tools("wol")),
            ("Network scanner", "scan rete subnet", lambda: self.open_network_tools("network-scan")),
            ("Port scanner", "scan porte host", lambda: self.open_network_tools("port-scan")),
            ("Packet capture", "tcpdump pcap cattura", lambda: self.open_network_tools("capture")),
            ("Impostazioni", "preferenze applicazione", lambda: self.show_settings_dialog()),
            ("Diagnostica", "doctor compatibilità", lambda: self.show_diagnostics_dialog()),
            ("Log applicazione", "registro errori", lambda: self.open_application_log()),
        ]
        for name, command in self.quick_tool_commands().items():
            actions.append((name, f"strumento rapido {name.lower()}", lambda n=name, c=command: self.run_quick_tool(n, c)))
        return actions

    def show_command_palette(self) -> None:
        dialog = CommandPaletteDialog(self, self.build_command_palette_actions())
        try:
            response = dialog.run()
            action = dialog.selected_action
        finally:
            dialog.destroy()
        if response == Gtk.ResponseType.OK and action is not None:
            GLib.idle_add(lambda: (action(), False)[1])

    def show_multiexec_dialog(self) -> None:
        targets: List[Tuple[object, str, str, bool]] = []
        for index in range(self.notebook.get_n_pages()):
            page = self.notebook.get_nth_page(index)
            if page not in self.terminal_tabs:
                continue
            sess = getattr(page, "fedtermhub_session", None)
            if isinstance(sess, Session):
                kind = sess.protocol.upper()
                default = sess.protocol.lower() in {"ssh"}
            elif isinstance(page, SplitTerminalTab):
                kind, default = "terminale diviso", False
            else:
                kind, default = "terminale locale" if getattr(page, "title", "") == "Locale" else "strumento", False
            # Il titolo scelto all'apertura (nome della sessione) sta nel tooltip dell'etichetta.
            title = getattr(page, "title", "") or "Terminale"
            label_widget = self.notebook.get_tab_label(page)
            if label_widget is not None:
                for child in getattr(label_widget, "get_children", lambda: [])():
                    if isinstance(child, Gtk.Label) and child.get_tooltip_text():
                        title = child.get_tooltip_text()
                        break
            targets.append((page, title, kind, default))
        if not targets:
            self.set_status("Nessuna scheda terminale aperta per MultiExec.")
            return
        if not any(default for *_rest, default in targets):
            targets = [(t, title, kind, True) for t, title, kind, _d in targets]
        dialog = MultiExecDialog(self, targets)
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                command = dialog.get_command()
                chosen = dialog.selected_targets()
                if command and chosen:
                    sent = 0
                    payload = (command + "\n").encode("utf-8")
                    for tab in chosen:
                        try:
                            feed_all = getattr(tab, "feed_all", None)
                            if callable(feed_all):
                                sent += feed_all(payload)
                            else:
                                tab.terminal.feed_child(payload)
                                sent += 1
                        except Exception:
                            pass
                    self.set_status(f"MultiExec: comando inviato a {sent} terminali.")
        finally:
            dialog.destroy()

    def open_split_terminal(self) -> None:
        split = SplitTerminalTab(theme=self.settings.theme, settings=self.settings)
        # Il workspace intero entra nell'elenco usato da MultiExec: feed_all
        # raggiungerà tutti i riquadri, presenti e futuri.
        if split not in self.terminal_tabs:
            self.terminal_tabs.append(split)
        self.add_widget_tab("Split", split)

    def add_text_tab(self, title: str, text: str) -> None:
        self.add_widget_tab(title, TextInfoTab(text, theme=self.settings.theme))

    def append_app_log(self, title: str, message: str, details: str = "", command: str = "") -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(f"\n--- {title} ---\n")
                handle.write(message + "\n")
                if command:
                    handle.write("Comando: " + command + "\n")
                if details:
                    handle.write(details.strip() + "\n")
        except Exception:
            pass

    def diagnostic_target_for_session(self, session: Session) -> Optional["DiagnosticTarget"]:
        if make_target is None:
            return None
        protocol = (session.protocol or "").lower()
        if protocol not in {"rdp", "ssh", "sftp", "vnc"}:
            return None
        routing_mode = getattr(session, "routing_mode", "direct")
        if routing_mode == "rd_gateway":
            host = str(getattr(session, "rdp_gateway_host", "") or "").strip()
            if not host:
                return None
            return make_target(
                f"{session.name} — RD Gateway", "rdp", host,
                getattr(session, "rdp_gateway_port", "443") or "443",
            )
        if routing_mode == "proxy_jump":
            host = str(getattr(session, "jump_host", "") or "").strip()
            if not host:
                return None
            return make_target(
                f"{session.name} — Jump host", "ssh", host,
                getattr(session, "jump_port", "22") or "22",
            )
        if not session.host.strip():
            return None
        return make_target(session.name, protocol, session.host, session.port)

    def show_connection_diagnostics(self, session: Session) -> None:
        target = self.diagnostic_target_for_session(session)
        if target is None:
            self.show_notification(
                "La diagnostica è disponibile per sessioni RDP, SSH, SFTP e VNC con un host configurato.",
                "warning",
                4500,
            )
            return
        timeout = max(1.0, min(float(self.settings.connection_timeout_seconds), 20.0))
        dialog = ConnectionDiagnosticsDialog(self, target, timeout=timeout)
        dialogs = getattr(self, "_connection_diagnostic_dialogs", None)
        if dialogs is None:
            dialogs = []
            self._connection_diagnostic_dialogs = dialogs
        dialogs.append(dialog)
        dialog.connect("destroy", lambda item: dialogs.remove(item) if item in dialogs else None)
        dialog.present()

    def report_session_error(
        self,
        title: str,
        message: str,
        details: str = "",
        command: str = "",
        diagnostic_session: Optional[Session] = None,
    ) -> bool:
        """Mostra errori di connessione in modo user-friendly.

        La diagnostica compare soltanto quando è disponibile un bersaglio remoto,
        senza aggiungere controlli permanenti all'interfaccia principale.
        Ritorna False così può essere usata direttamente con GLib.idle_add.
        """
        self.append_app_log(title, message, details, command)
        self.set_status(f"Errore: {title}")
        self.show_notification(f"{title}: {message}", "error", 0)
        if self.settings.open_error_tabs:
            try:
                diagnostic_callback = None
                if diagnostic_session is not None and self.diagnostic_target_for_session(diagnostic_session) is not None:
                    diagnostic_callback = lambda session=diagnostic_session: self.show_connection_diagnostics(session)
                self.add_widget_tab(
                    title[:32],
                    SessionErrorTab(title, message, details, command, diagnostic_callback),
                )
            except Exception:
                pass
        if self.settings.show_error_popups:
            try:
                dialog = Gtk.MessageDialog(
                    transient_for=self, flags=0, message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.CLOSE, text=title,
                )
                dialog.get_style_context().add_class("modern-dialog")
                secondary = message
                if details:
                    short_details = details.strip().splitlines()[-8:]
                    secondary += "\n\nUltime righe log:\n" + "\n".join(short_details)[:1400]
                dialog.format_secondary_text(secondary)
                dialog.connect("delete-event", lambda d, _e: (d.response(Gtk.ResponseType.CLOSE), True)[1])
                try:
                    dialog.run()
                finally:
                    dialog.destroy()
            except Exception as exc:
                self.set_status(f"Errore sessione: {title} / errore popup: {exc}")
        return False

    def _remote_files_connection(self, sess: Session):
        if SftpConnection is None or not sess.host:
            return None
        user = sess.user
        password: Optional[str] = None
        # Non aprire finestre di sblocco soltanto perché l'utente cambia tab.
        # Se il vault è già aperto, riutilizziamo la credenziale; altrimenti il
        # browser può sfruttare ssh-agent o il socket multiplex della shell SSH.
        if sess.credential_id and self.vault.unlocked:
            credential = self.vault.get(sess.credential_id)
            if credential is not None:
                user = credential.username or user
                if sess.use_credential_password:
                    password = credential.password
        jump_spec = self.proxy_jump_spec(sess)
        return SftpConnection(
            label=sess.name or f"{user + '@' if user else ''}{sess.host}",
            host=sess.host,
            port=sess.port or "22",
            user=user,
            password=password,
            jump_spec=jump_spec,
            ssh_options=tuple(self.ssh_session_options()),
            timeout_seconds=self.settings.connection_timeout_seconds,
        )

    def _on_follow_remote_changed(self, active: bool) -> None:
        self.settings.follow_remote_directory = bool(active)
        self.settings.save(SETTINGS_FILE)

    def _on_terminal_directory_change(self, workspace, host: str, path: str) -> None:  # noqa: ANN001
        """La shell ha cambiato directory: sposta l'albero dei file remoti.

        Tre controlli prima di agire, tutti necessari:
          - la sidebar deve essere agganciata alla stessa sessione del tab che
            ha generato l'evento, altrimenti un tab in secondo piano
            sposterebbe l'albero di un altro host;
          - l'hostname dichiarato nell'OSC 7 deve corrispondere a quello della
            connessione. Senza questo controllo un terminale locale
            piloterebbe la sidebar remota: e' la raccomandazione esplicita
            degli sviluppatori VTE;
          - l'utente non deve stare navigando l'albero con le mani.
        """
        sidebar = getattr(self, "remote_files_sidebar", None)
        if sidebar is None or not path:
            return
        if not self.settings.follow_remote_directory or not sidebar.follows_terminal():
            return
        current = self.notebook.get_nth_page(self.notebook.get_current_page())
        if current is not workspace:
            return
        sess = getattr(workspace, "fedtermhub_session", None)
        if not isinstance(sess, Session) or sess.protocol.lower() != "ssh":
            return
        if host:
            # Confronto tollerante: l'OSC 7 riporta l'hostname della macchina
            # ("monitor"), la sessione spesso un IP o un FQDN.
            short_remote = host.split(".")[0].lower()
            target = (sess.host or "").split(".")[0].lower()
            known = {target, (sess.name or "").lower()}
            if short_remote not in known and target not in host.lower():
                # Non e' un errore: capita con i bastion o quando la sessione
                # e' salvata con un alias. Si segue comunque, ma solo se la
                # sidebar e' gia' su quella sessione (verificato sopra).
                pass
        try:
            if sidebar.current_path() == path:
                return
            sidebar.load_root(path)
        except Exception:
            pass

    def _show_remote_files_for_page(self, page: Optional[Gtk.Widget]) -> None:
        sidebar = getattr(self, "remote_files_sidebar", None)
        label = getattr(self, "remote_files_tab_label", None)
        if sidebar is None or label is None:
            return
        sess = getattr(page, "fedtermhub_session", None) if page is not None else None
        supported = isinstance(sess, Session) and sess.protocol.lower() in {"ssh", "sftp"} and bool(sess.host)
        if not supported:
            if self.sidebar_notebook.get_current_page() == self.remote_files_page_num:
                self.sidebar_notebook.set_current_page(0)
            label.hide()
            self.sidebar_notebook.set_show_tabs(False)
            sidebar.clear_connection()
            return
        label.show()
        self.sidebar_notebook.set_show_tabs(True)
        connection = self._remote_files_connection(sess)
        if connection is not None:
            sidebar.set_connection(connection)

    def on_workspace_tab_switched(
        self, _notebook: Gtk.Notebook, page: Gtk.Widget, _page_num: int
    ) -> None:
        self._show_remote_files_for_page(page)

    def on_workspace_page_removed(
        self, notebook: Gtk.Notebook, _child: Gtk.Widget, _page_num: int
    ) -> None:
        def sync_current() -> bool:
            current = notebook.get_nth_page(notebook.get_current_page())
            self._show_remote_files_for_page(current)
            return False

        GLib.idle_add(sync_current)

    def add_widget_tab(self, title: str, widget: Gtk.Widget) -> None:
        label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        label_box.get_style_context().add_class("tab-label")
        label = Gtk.Label(label=title[:32])
        label.set_tooltip_text(title)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_tooltip_text("Chiudi tab")
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_can_focus(False)
        close.get_style_context().add_class("tab-close")
        label_box.pack_start(label, True, True, 0)
        label_box.pack_start(close, False, False, 0)
        label_box.show_all()
        page = self.notebook.append_page(widget, label_box)
        self.notebook.set_tab_reorderable(widget, True)
        if (
            TerminalWorkspace is not None
            and isinstance(widget, TerminalWorkspace)
            and getattr(widget, "on_last_pane_close", None) is None
        ):
            widget.on_last_pane_close = lambda workspace, pane, explicit: self._on_terminal_finished(
                workspace, pane, explicit, "always"
            )
        close.connect("clicked", lambda _b: self.close_specific_tab(widget))
        widget.show_all()
        self.notebook.set_current_page(page)
        self.set_status(f"Aperto: {title}")

    def on_add_proto_session(self, proto: str) -> None:
        defaults = {
            "ssh": Session(name="Nuova SSH", protocol="ssh", port="22"),
            "rdp": Session(name="Nuova RDP", protocol="rdp", port="3389"),
            "serial": Session(name="Nuova seriale", protocol="serial", host="/dev/ttyUSB0", port="115200"),
            "vnc": Session(name="Nuova VNC", protocol="vnc"),
            "sftp": Session(name="Nuova SFTP", protocol="sftp", port="22"),
        }
        dialog = SessionDialog(self, defaults.get(proto, Session(name="Nuova sessione", protocol=proto)), credentials=self.credential_options_for_dialog(), tunnels=self.tunnel_options_for_dialog())
        session: Optional[Session] = None
        save_password = False
        transient_password = ""
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                session = dialog.get_session()
                save_password = dialog.should_offer_save_password()
                transient_password = dialog.get_transient_password()
        finally:
            dialog.destroy()
        if session is not None:
            self._add_new_session(session, save_password, transient_password)

    def open_ephemeral_lab(self) -> None:
        """Apre il laboratorio Linux usa e getta senza duplicare controlli nella UI."""
        if EphemeralLabDialog is None or self.lab_controller is None:
            detail = str(EPHEMERAL_LAB_IMPORT_ERROR or "modulo non disponibile")
            self.show_notification(f"Lab temporaneo non disponibile: {detail}", "error", 7000)
            return
        if self.lab_dialog is None:
            self.lab_dialog = EphemeralLabDialog(self, self.lab_controller)
        self.lab_dialog.show_all()
        self.lab_dialog.present()

    def open_network_tools(self, page: str = "services") -> None:
        if NetworkToolsDialog is None:
            self.report_session_error(
                "Strumenti di rete non disponibili",
                "Il modulo degli strumenti di rete non è stato caricato.",
                str(NETWORK_TOOLS_IMPORT_ERROR or "errore sconosciuto"),
            )
            return
        dialog = NetworkToolsDialog(self, start_page=page, create_session=self.create_session_from_network_tool)
        apply_window_icon(dialog)
        dialog.show_all()
        dialog.present()

    def create_session_from_network_tool(self, protocol: str, host: str) -> None:
        defaults = {
            "ssh": Session(name=f"SSH {host}", protocol="ssh", host=host, port="22"),
            "rdp": Session(name=f"RDP {host}", protocol="rdp", host=host, port="3389"),
            "vnc": Session(name=f"VNC {host}", protocol="vnc", host=host, port="5900"),
            "sftp": Session(name=f"SFTP {host}", protocol="sftp", host=host, port="22"),
        }
        session_template = defaults.get(protocol, Session(name=f"Sessione {host}", protocol=protocol, host=host))
        dialog = SessionDialog(
            self,
            session_template,
            credentials=self.credential_options_for_dialog(),
            tunnels=self.tunnel_options_for_dialog(),
        )
        session: Optional[Session] = None
        save_password = False
        transient_password = ""
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                session = dialog.get_session()
                save_password = dialog.should_offer_save_password()
                transient_password = dialog.get_transient_password()
        finally:
            dialog.destroy()
        if session is not None:
            self._add_new_session(session, save_password, transient_password)

    def on_tunnel_manager(self, _button: Optional[Gtk.Button]) -> None:
        dialog = TunnelManagerDialog(self)
        try:
            dialog.run()
        finally:
            dialog.destroy()

    def append_tunnel_log(self, tunnel_id: str, line: str) -> None:
        line = (line or "").rstrip("\r\n")
        if not line:
            return
        log = self.tunnel_logs.setdefault(tunnel_id, [])
        timestamp = time.strftime("%H:%M:%S")
        log.append(f"[{timestamp}] {line}")
        if len(log) > 1200:
            del log[:-1000]

    def get_tunnel_state(self, tunnel_id: str) -> str:
        proc = self.tunnel_processes.get(tunnel_id)
        if proc is not None and proc.poll() is None:
            return self.tunnel_states.get(tunnel_id, "running")
        return self.tunnel_states.get(tunnel_id, "stopped")

    def tunnel_log_text(self, tunnel: TunnelSession) -> str:
        proc = self.tunnel_processes.get(tunnel.id)
        state = self.get_tunnel_state(tunnel.id)
        pid = proc.pid if proc is not None and proc.poll() is None else "—"
        exit_code = self.tunnel_exit_codes.get(tunnel.id)
        lines = [
            f"Tunnel: {tunnel.name}",
            f"Stato: {state}",
            f"PID: {pid}",
            f"Tipo: {tunnel.type_label()}",
            f"Instradamento: {tunnel.summary()}",
            f"Auto-avvio: {'sì' if tunnel.auto_start else 'no'}",
            f"Riconnessione automatica: {'sì' if tunnel.auto_restart else 'no'}",
        ]
        if exit_code is not None:
            lines.append(f"Ultimo codice di uscita: {exit_code}")
        try:
            argv, _env = self.build_tunnel_command(tunnel, allow_unlock=False)
            lines.append(f"Comando: {self.mask_args(argv)}")
        except Exception as exc:
            lines.append(f"Comando non disponibile: {exc}")
        lines.extend(["", "Log:"])
        log = self.tunnel_logs.get(tunnel.id, [])
        lines.extend(log if log else ["Nessun evento registrato."])
        return "\n".join(lines)

    @staticmethod
    def _valid_port(value: str) -> bool:
        try:
            port = int(str(value))
            return 1 <= port <= 65535
        except (TypeError, ValueError):
            return False

    def validate_tunnel(self, tunnel: TunnelSession, check_local_bind: bool = False) -> List[str]:
        errors: List[str] = []
        if tunnel.tunnel_type not in {"local", "remote", "dynamic"}:
            errors.append("tipo di tunnel non valido")
        if not tunnel.ssh_host.strip():
            errors.append("host SSH mancante")
        if not self._valid_port(tunnel.ssh_port):
            errors.append("porta SSH non valida")
        if not tunnel.bind_address.strip():
            errors.append("indirizzo di bind mancante")
        if not self._valid_port(tunnel.local_port):
            errors.append("porta di inoltro non valida")
        if tunnel.tunnel_type != "dynamic":
            if not tunnel.remote_host.strip():
                errors.append("host destinazione mancante")
            if not self._valid_port(tunnel.remote_port):
                errors.append("porta destinazione non valida")
        if tunnel.identity_file:
            identity = Path(os.path.expanduser(tunnel.identity_file))
            if not identity.is_file():
                errors.append("chiave privata non trovata")
        try:
            shlex.split(tunnel.options)
        except ValueError as exc:
            errors.append(f"opzioni SSH non valide: {exc}")
        if check_local_bind and tunnel.tunnel_type in {"local", "dynamic"} and not errors:
            bind_error = self._check_tunnel_bind_available(tunnel.bind_address, int(tunnel.local_port))
            if bind_error:
                errors.append(bind_error)
        return errors

    @staticmethod
    def _check_tunnel_bind_available(address: str, port: int) -> str:
        address = address.strip() or "127.0.0.1"
        try:
            infos = net_socket.getaddrinfo(
                address,
                port,
                family=net_socket.AF_UNSPEC,
                type=net_socket.SOCK_STREAM,
                flags=net_socket.AI_PASSIVE,
            )
        except OSError as exc:
            return f"indirizzo di bind non valido: {exc}"
        last_error: Optional[BaseException] = None
        for family, socktype, proto, _canon, sockaddr in infos:
            sock = net_socket.socket(family, socktype, proto)
            try:
                sock.setsockopt(net_socket.SOL_SOCKET, net_socket.SO_REUSEADDR, 1)
                sock.bind(sockaddr)
                return ""
            except OSError as exc:
                last_error = exc
            finally:
                sock.close()
        return f"porta {address}:{port} non disponibile: {last_error or 'errore sconosciuto'}"

    def _resolve_tunnel_identity(
        self, tunnel: TunnelSession, allow_unlock: bool
    ) -> Tuple[str, Optional[str]]:
        user = tunnel.ssh_user
        password: Optional[str] = None
        if not tunnel.credential_id:
            return user, password
        if not self.vault.unlocked:
            if not allow_unlock:
                return user, password
            if not self.ensure_vault_unlocked(create_if_missing=False):
                raise RuntimeError("Password Manager non sbloccato")
        credential = self.vault.get(tunnel.credential_id) if self.vault.unlocked else None
        if credential is None:
            raise RuntimeError("credenziale collegata non trovata nel Password Manager")
        user = credential.username or user
        if tunnel.use_credential_password:
            password = credential.password
        return user, password

    def build_tunnel_command(
        self, tunnel: TunnelSession, allow_unlock: bool = True
    ) -> Tuple[List[str], Dict[str, str]]:
        errors = self.validate_tunnel(tunnel, check_local_bind=False)
        if errors:
            raise ValueError("; ".join(errors))
        ssh_binary = shutil.which("ssh") or "ssh"
        argv = [ssh_binary, "-N", "-T"] + self.ssh_common_options()
        argv += ["-o", "ControlMaster=no", "-o", "NumberOfPasswordPrompts=1"]
        if tunnel.exit_on_forward_failure:
            argv += ["-o", "ExitOnForwardFailure=yes"]
        if tunnel.server_alive_interval > 0:
            argv += [
                "-o", f"ServerAliveInterval={tunnel.server_alive_interval}",
                "-o", f"ServerAliveCountMax={tunnel.server_alive_count}",
            ]
        if tunnel.compression and "-C" not in argv:
            argv.append("-C")
        if tunnel.address_family == "ipv4":
            argv.append("-4")
        elif tunnel.address_family == "ipv6":
            argv.append("-6")
        if tunnel.verbose_log:
            argv.append("-v")
        argv += ["-p", tunnel.ssh_port]
        if tunnel.identity_file:
            argv += ["-i", os.path.expanduser(tunnel.identity_file)]
        listen = f"{tunnel.bind_address}:{tunnel.local_port}"
        if tunnel.tunnel_type == "dynamic":
            argv += ["-D", listen]
        elif tunnel.tunnel_type == "remote":
            argv += ["-R", f"{listen}:{tunnel.remote_host}:{tunnel.remote_port}"]
        else:
            argv += ["-L", f"{listen}:{tunnel.remote_host}:{tunnel.remote_port}"]
        if tunnel.options:
            argv += shlex.split(tunnel.options)
        user, password = self._resolve_tunnel_identity(tunnel, allow_unlock)
        if not password:
            argv += ["-o", "BatchMode=yes"]
        target = f"{user}@{tunnel.ssh_host}" if user else tunnel.ssh_host
        argv += ["--", target]
        env: Dict[str, str] = {}
        if password:
            sshpass = shutil.which("sshpass")
            if not sshpass:
                raise RuntimeError("sshpass non installato: sudo dnf install sshpass")
            argv = [sshpass, "-e"] + argv
            env["SSHPASS"] = password
        return argv, env

    def _build_tunnel_probe_command(
        self, tunnel: TunnelSession
    ) -> Tuple[List[str], Dict[str, str]]:
        # Verifica autenticazione e raggiungibilità senza aprire alcuna porta.
        errors = self.validate_tunnel(tunnel, check_local_bind=False)
        if errors:
            raise ValueError("; ".join(errors))
        ssh_binary = shutil.which("ssh") or "ssh"
        argv = [ssh_binary, "-T"] + self.ssh_common_options()
        argv += [
            "-o", "ControlMaster=no",
            "-o", "NumberOfPasswordPrompts=1",
            "-o", f"ConnectTimeout={max(3, self.settings.connection_timeout_seconds)}",
            "-p", tunnel.ssh_port,
        ]
        if tunnel.address_family == "ipv4":
            argv.append("-4")
        elif tunnel.address_family == "ipv6":
            argv.append("-6")
        if tunnel.identity_file:
            argv += ["-i", os.path.expanduser(tunnel.identity_file)]
        if tunnel.verbose_log:
            argv.append("-v")
        user, password = self._resolve_tunnel_identity(tunnel, allow_unlock=True)
        if not password:
            argv += ["-o", "BatchMode=yes"]
        target = f"{user}@{tunnel.ssh_host}" if user else tunnel.ssh_host
        argv += ["--", target, "printf FEDTERMHUB_TUNNEL_OK"]
        env: Dict[str, str] = {}
        if password:
            sshpass = shutil.which("sshpass")
            if not sshpass:
                raise RuntimeError("sshpass non installato: sudo dnf install sshpass")
            argv = [sshpass, "-e"] + argv
            env["SSHPASS"] = password
        return argv, env

    def test_tunnel_connection(
        self,
        tunnel: TunnelSession,
        callback: Optional[Callable[[bool, str, str], None]] = None,
    ) -> None:
        # La lettura del vault e gli eventuali dialoghi devono avvenire nel thread GTK.
        try:
            argv, extra_env = self._build_tunnel_probe_command(tunnel)
        except Exception as exc:
            message = f"Impossibile preparare la verifica: {exc}"
            if callback:
                callback(False, message, "")
            else:
                self.show_notification(message, "error", 6000)
            return

        def worker() -> None:
            try:
                env = os.environ.copy()
                env.update(extra_env)
                started = time.monotonic()
                completed = subprocess.run(
                    argv,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=max(8, self.settings.connection_timeout_seconds + 5),
                    check=False,
                )
                elapsed = int((time.monotonic() - started) * 1000)
                output = (completed.stdout or "").strip()
                success = completed.returncode == 0 and "FEDTERMHUB_TUNNEL_OK" in output
                if success:
                    message = f"Connessione SSH riuscita in {elapsed} ms."
                else:
                    message = f"Verifica SSH fallita (codice {completed.returncode})."
                details = output.replace("FEDTERMHUB_TUNNEL_OK", "").strip()
            except subprocess.TimeoutExpired as exc:
                success = False
                message = "Timeout durante la verifica SSH."
                details = str(exc)
            except Exception as exc:
                success = False
                message = f"Impossibile verificare il tunnel: {exc}"
                details = traceback.format_exc()
            if callback:
                GLib.idle_add(callback, success, message, details)
            else:
                GLib.idle_add(
                    self.show_notification,
                    message,
                    "success" if success else "error",
                    6000,
                )

        threading.Thread(target=worker, daemon=True, name=f"tunnel-test-{tunnel.id[:8]}").start()

    def start_auto_tunnels(self) -> bool:
        blocked = 0
        started = 0
        for tunnel in self.tunnel_store.tunnels:
            if not tunnel.auto_start:
                continue
            if tunnel.credential_id and not self.vault.unlocked:
                self.tunnel_states[tunnel.id] = "locked"
                self.append_tunnel_log(
                    tunnel.id,
                    "Auto-avvio sospeso: sblocca il Password Manager e avvia il tunnel manualmente.",
                )
                blocked += 1
                continue
            if self.start_tunnel(tunnel, interactive=False, quiet=True):
                started += 1
        if started:
            self.show_notification(f"Avviati automaticamente {started} tunnel SSH.", "success", 4000)
        if blocked:
            self.show_notification(
                f"{blocked} tunnel automatici attendono lo sblocco del Password Manager.",
                "warning",
                6000,
            )
        return False

    def start_tunnel(
        self,
        tunnel: TunnelSession,
        interactive: bool = True,
        quiet: bool = False,
    ) -> bool:
        old = self.tunnel_processes.get(tunnel.id)
        if old is not None and old.poll() is None:
            if not quiet:
                self.set_status(f"Tunnel già attivo: {tunnel.name}")
            return False
        restart_source = self._tunnel_restart_sources.pop(tunnel.id, None)
        if restart_source:
            try:
                GLib.source_remove(restart_source)
            except Exception:
                pass
        self._tunnel_manual_stops.discard(tunnel.id)
        errors = self.validate_tunnel(tunnel, check_local_bind=True)
        if errors:
            message = "; ".join(errors)
            self.tunnel_states[tunnel.id] = "error"
            self.append_tunnel_log(tunnel.id, f"Configurazione non valida: {message}")
            if interactive:
                self.show_notification(f"Tunnel {tunnel.name}: {message}", "error", 7000)
            return False
        try:
            argv, extra_env = self.build_tunnel_command(tunnel, allow_unlock=interactive)
            env = os.environ.copy()
            env.update(extra_env)
            self.tunnel_states[tunnel.id] = "starting"
            self.tunnel_exit_codes.pop(tunnel.id, None)
            self.append_tunnel_log(tunnel.id, f"Avvio: {self.mask_args(argv)}")
            proc = subprocess.Popen(
                argv,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            self.tunnel_processes[tunnel.id] = proc
            threading.Thread(
                target=self._read_tunnel_output,
                args=(tunnel.id, proc),
                daemon=True,
                name=f"tunnel-log-{tunnel.id[:8]}",
            ).start()
            GLib.timeout_add(650, self._confirm_tunnel_started, tunnel.id, proc.pid)
            if not quiet:
                self.set_status(f"Avvio tunnel: {tunnel.name}")
            return True
        except Exception as exc:
            self.tunnel_states[tunnel.id] = "error"
            self.append_tunnel_log(tunnel.id, f"Errore di avvio: {exc}")
            if interactive:
                self.show_notification(f"Errore tunnel {tunnel.name}: {exc}", "error", 7000)
            return False

    def _confirm_tunnel_started(self, tunnel_id: str, pid: int) -> bool:
        proc = self.tunnel_processes.get(tunnel_id)
        if proc is None or proc.pid != pid:
            return False
        if proc.poll() is None:
            self.tunnel_states[tunnel_id] = "running"
            tunnel = self.tunnel_store.get(tunnel_id)
            if tunnel:
                self.append_tunnel_log(tunnel_id, f"Tunnel attivo: {tunnel.summary()}")
                self.show_notification(f"Tunnel attivo: {tunnel.name}", "success", 3000)
        return False

    def _read_tunnel_output(self, tunnel_id: str, proc: subprocess.Popen) -> None:
        try:
            if proc.stdout is not None:
                for line in iter(proc.stdout.readline, ""):
                    if not line:
                        break
                    self.append_tunnel_log(tunnel_id, line)
        except Exception as exc:
            self.append_tunnel_log(tunnel_id, f"Errore lettura log: {exc}")
        finally:
            try:
                code = proc.wait()
            except Exception:
                code = -1
            GLib.idle_add(self._tunnel_exited, tunnel_id, proc.pid, code)

    def _tunnel_exited(self, tunnel_id: str, pid: int, code: int) -> bool:
        proc = self.tunnel_processes.get(tunnel_id)
        if proc is None or proc.pid != pid:
            return False
        self.tunnel_exit_codes[tunnel_id] = code
        manual = tunnel_id in self._tunnel_manual_stops
        self._tunnel_manual_stops.discard(tunnel_id)
        tunnel = self.tunnel_store.get(tunnel_id)
        if manual:
            self.tunnel_states[tunnel_id] = "stopped"
            self.append_tunnel_log(tunnel_id, f"Tunnel fermato (codice {code}).")
            return False
        self.tunnel_states[tunnel_id] = "error" if code else "stopped"
        self.append_tunnel_log(tunnel_id, f"Processo terminato con codice {code}.")
        if tunnel and tunnel.auto_restart:
            self.tunnel_states[tunnel_id] = "restarting"
            delay = max(1, min(tunnel.restart_delay, 300))
            self.append_tunnel_log(tunnel_id, f"Nuovo tentativo tra {delay} secondi.")
            source = GLib.timeout_add_seconds(delay, self._restart_tunnel, tunnel_id)
            self._tunnel_restart_sources[tunnel_id] = source
        elif tunnel:
            self.show_notification(
                f"Tunnel interrotto: {tunnel.name} (codice {code})",
                "error" if code else "warning",
                6000,
            )
        return False

    def _restart_tunnel(self, tunnel_id: str) -> bool:
        self._tunnel_restart_sources.pop(tunnel_id, None)
        tunnel = self.tunnel_store.get(tunnel_id)
        if tunnel is None or tunnel_id in self._tunnel_manual_stops:
            return False
        if tunnel.credential_id and not self.vault.unlocked:
            self.tunnel_states[tunnel_id] = "locked"
            self.append_tunnel_log(tunnel_id, "Riconnessione sospesa: Password Manager bloccato.")
            return False
        self.start_tunnel(tunnel, interactive=False, quiet=True)
        return False

    def stop_tunnel(self, tunnel_id: str, quiet: bool = False) -> None:
        self._tunnel_manual_stops.add(tunnel_id)
        source = self._tunnel_restart_sources.pop(tunnel_id, None)
        if source:
            try:
                GLib.source_remove(source)
            except Exception:
                pass
        proc = self.tunnel_processes.get(tunnel_id)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            GLib.timeout_add_seconds(2, self._kill_tunnel_if_needed, tunnel_id, proc.pid)
        else:
            self.tunnel_states[tunnel_id] = "stopped"
            self._tunnel_manual_stops.discard(tunnel_id)
        if not quiet:
            tunnel = self.tunnel_store.get(tunnel_id)
            self.set_status(f"Tunnel fermato: {tunnel.name if tunnel else tunnel_id}")

    def _kill_tunnel_if_needed(self, tunnel_id: str, pid: int) -> bool:
        proc = self.tunnel_processes.get(tunnel_id)
        if proc is not None and proc.pid == pid and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        return False

    def on_theme_changed(self, combo: Gtk.ComboBoxText) -> None:
        theme = combo.get_active_id() or "dark"
        self.settings.theme = theme
        self.settings.save(SETTINGS_FILE)
        self.refresh_icon_theme_runtime()
        self.apply_theme(theme)
        self.refresh_theme_toggle_button()
        self.set_status(f"Tema applicato: {'Scuro' if theme == 'dark' else 'Chiaro'} — icone: {getattr(self, 'active_icon_theme', '-')}")

    def apply_theme(self, theme: str) -> None:
        screen = Gdk.Screen.get_default()
        if screen and self.css_provider is not None:
            try:
                Gtk.StyleContext.remove_provider_for_screen(screen, self.css_provider)
            except Exception:
                pass
        settings = Gtk.Settings.get_default()
        if settings is not None:
            try:
                settings.set_property("gtk-application-prefer-dark-theme", theme == "dark")
            except Exception:
                pass
        css = self.safe_css(theme) if SAFE_MODE else (self.dark_css() if theme == "dark" else self.light_css())
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(css.encode("utf-8"))
            if screen:
                Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.css_provider = provider
        except Exception as exc:
            self.set_status(f"CSS disattivato per errore tema: {exc}")
        for tab in getattr(self, "terminal_tabs", []):
            tab.apply_terminal_theme("dark")

    @staticmethod
    def safe_css(theme: str) -> str:
        if theme == "light":
            return """
            #main-root { background: #f8fafc; color: #111827; }
            #ribbon, #main-menubar, #bottom-status { background: #ffffff; color: #111827; }
            #sidebar { background: #ffffff; }
            #workspace, #terminal-notebook, #terminal-notebook stack { background: #ffffff; color: #111827; }
            .terminal-surface, .terminal-surface scrolledwindow, .terminal-surface viewport { background: #090c11; color: #eef4fb; }
            .terminal-workspace paned > separator { background: #223; min-width: 2px; min-height: 2px; }
            .terminal-workspace paned > separator:hover { background: #3b82f6; }
            #left-rail { background: #e5e7eb; }
            .ribbon-button, .mini-action, .small-action, .rail-button { padding: 3px 6px; }
            """
        return """
        #main-root { background: #101010; color: #d7d7d7; }
        #ribbon, #main-menubar, #bottom-status { background: #171717; color: #d7d7d7; }
        #sidebar { background: #1b1b1b; }
        #workspace { background: #0b0d0e; }
        #left-rail { background: #151515; }
        .ribbon-button, .mini-action, .small-action, .rail-button { padding: 3px 6px; }
        """

    @staticmethod
    def dark_css() -> str:
        return """
        window, .background, dialog, messagedialog, popover, menu, tooltip { background-color: #0f1115; color: #f2f4f8; }
        window label, dialog label, popover label, menu label, button label, checkbutton, radiobutton { color: #f2f4f8; }
        button image, .adaptive-icon, .ribbon-button image, .rail-button image, .mini-action image, .secondary-action image { color: #eaf0f7; opacity: 1; -gtk-icon-shadow: none; }
        button:disabled image, button:disabled label { color: #8e98a6; opacity: .78; }
        #main-root { background: #0f1115; color: #eef1f6; font-size: 12px; }
        #main-root:backdrop, window:backdrop, .background:backdrop { background-color: #0f1115; color: #b7c0cc; }
        #main-menubar { background: #15181e; color: #eef1f6; border-bottom: 1px solid #292e38; padding: 2px 7px; }
        #main-menubar menuitem { padding: 5px 9px; border-radius: 7px; }
        #main-menubar menuitem:hover { background: #272c35; }
        #main-menubar:backdrop, #main-menubar:disabled { background: #15181e; color: #b7c0cc; }
        #main-menubar:backdrop menuitem, #main-menubar menuitem:backdrop, #main-menubar menuitem:disabled {
            background-color: transparent; background-image: none; color: #b7c0cc;
            border-color: transparent; box-shadow: none; text-shadow: none;
        }
        #main-menubar menu { background: #191d24; color: #f5f7fb; border: 1px solid #343a46; border-radius: 10px; padding: 6px; }
        #main-menubar menu menuitem, #main-menubar menu .dropdown-item { padding: 7px 10px; border-radius: 8px; }
        #main-menubar menu menuitem:hover, #main-menubar menu .dropdown-item:hover { background: #28303a; }
        #main-menubar menu separator { border-top: 1px solid #313844; margin: 5px 4px; }
        #main-menubar menu image { color: #d6e0eb; margin-right: 8px; opacity: 1; }
        #ribbon { background: #12151a; border-bottom: 1px solid #282d36; padding: 2px 5px; min-height: 48px; }
        .ribbon-group { background: transparent; border: none; border-right: 1px solid #2d333e; border-radius: 0; padding: 1px 5px 1px 1px; margin-right: 4px; box-shadow: none; }
        .ribbon-group-title { min-height: 0; font-size: 0; padding: 0; margin: 0; }
        .ribbon-caption { color: #dce2ea; font-size: 9px; font-weight: 600; padding-top: 0; }
        #body { background: #0f1115; }
        #left-rail { background: #15181e; border-right: 1px solid #292e38; padding: 4px 2px; min-width: 30px; }
        #sidebar { background: #14171c; border-right: 1px solid #2b303a; }
        #workspace { background: #0c0e12; }
        #bottom-status { background: #12151a; color: #bac1cc; border-top: 1px solid #292e38; min-height: 20px; }
        .footer-version { color: #79c7ff; }
        .ribbon-button, .ribbon-exit { background: transparent; color: #e7eaf0; border: 1px solid transparent; border-radius: 6px; padding: 2px 5px; min-width: 42px; }
        .rail-button { background: transparent; color: #e7eaf0; border: 1px solid transparent; border-radius: 6px; padding: 4px; min-width: 26px; }
        .ribbon-button:hover, .rail-button:hover { background: #252a33; border-color: #39404d; }
        .ribbon-button:active, .rail-button:active { background: #303744; }
        #ribbon:backdrop, #left-rail:backdrop, #bottom-status:backdrop { background: #12151a; color: #aeb7c4; }
        .ribbon-button:backdrop, .ribbon-button:disabled, .rail-button:backdrop, .rail-button:disabled {
            background-color: transparent; background-image: none; color: #9fa9b7;
            border-color: transparent; box-shadow: none; text-shadow: none;
        }
        .ribbon-button image:backdrop, .rail-button image:backdrop, .adaptive-icon:backdrop { color: #c8d0dc; opacity: .92; -gtk-icon-shadow: none; }
        .ribbon-exit:hover { background: #472127; border-color: #823642; color: #ffd8dc; }
        .quick-box { background: #111419; border-bottom: 1px solid #292f39; padding: 4px; }
        .command-palette { background: transparent; }
        .command-palette-body { background: #0f141b; border: 1px solid #2b3442; border-radius: 14px; padding: 8px; }
        .command-palette-entry { min-height: 40px; font-size: 15px; margin-bottom: 6px; background: #161c25; border: 1px solid #2b3442; border-radius: 10px; color: #eaf2fb; }
        .command-palette-entry:focus { border-color: #4aa8e8; box-shadow: 0 0 0 2px rgba(74,168,232,.20); }
        .command-palette-list { background: transparent; color: #dbe6f3; }
        .command-palette-list:selected, .command-palette-list row:selected { background: #1d64c4; color: #ffffff; }
        .command-palette-hint { color: #7c8aa0; padding: 6px 4px 2px 4px; }
        .quick-connect-button, .sidebar-collapse { background: transparent; border: none; border-radius: 6px; padding: 4px; }
        .quick-label, .side-title, .field-label { color: #e8ebf1; font-weight: 600; }
        entry, spinbutton, combobox button, .quick-entry, .session-search {
            min-height: 30px; background: #1a1e25; color: #f4f6fa; border: 1px solid #343b47;
            border-radius: 9px; padding: 4px 9px; caret-color: #8fd3ff;
        }
        entry:focus, spinbutton:focus, combobox button:focus, .quick-entry:focus, .session-search:focus { border-color: #4aa8e8; box-shadow: 0 0 0 2px rgba(74,168,232,.18); }
        entry selection { background: #2f78aa; color: #ffffff; }
        button { background: #232831; color: #edf0f5; border: 1px solid #3a424f; border-radius: 9px; padding: 5px 9px; }
        button:hover { background: #303641; border-color: #4a5463; }
        button:active { background: #1d222a; }
        button:disabled { background: #191d23; color: #707a88; border-color: #2a3039; }
        modelbutton { background: transparent; color: #f2f4f8; border-radius: 7px; padding: 6px 9px; }
        modelbutton:hover { background: #2a3039; }
        headerbar, .titlebar { background: #15181e; color: #f4f6fa; border-color: #2c323c; }
        list, listbox, row, placessidebar, placessidebar row { background: #14171c; color: #e8ebf1; }
        row:hover, placessidebar row:hover { background: #222831; }
        switch { background: #353c47; border-radius: 12px; }
        switch:checked { background: #3b9bd5; }
        .small-action, .mini-action, .secondary-action {
            background: #232831; color: #f0f3f7; border: 1px solid #3a424f; border-radius: 9px; padding: 5px 10px;
        }
        .small-action:hover, .mini-action:hover, .secondary-action:hover { background: #303641; border-color: #4a5463; }
        /* Interruttori (segui il terminale, file nascosti): senza uno stato
           premuto visibile, attivo e spento erano indistinguibili. */
        .mini-action:checked { background: #285f87; border-color: #3d759a; }
        .mini-action:checked image { color: #ffffff; }
        .side-titlebar { background: #191d24; border-bottom: 1px solid #2d333e; padding: 5px 7px; }
        .count-badge { color: #06131d; background: #72c7ff; border-radius: 10px; padding: 2px 7px; font-size: 10px; font-weight: 800; }
        .session-toolbar, .side-footer { background: #15181e; border-bottom: 1px solid #292f39; padding: 2px 3px; }
        .session-toolbar .mini-action, .side-footer .mini-action { padding: 3px; min-height: 24px; }
        scrolledwindow, viewport { background: #111419; border: none; }
        scrolledwindow.session-scroll, scrolledwindow.remote-files-scroll { background: #14171c; }
        .remote-session-label { color: #eef2f7; font-weight: 700; }
        .remote-path, .remote-status { color: #aeb8c6; font-size: 10px; }
        .sidebar-pages header { background: #15181e; border-bottom: 1px solid #2d333e; }
        .sidebar-pages tab { padding: 3px 7px; }
        treeview.view, #session-tree, .view { background: #14171c; color: #e7eaf0; font-size: 12px; }
        treeview.view header button { background: #1b2028; color: #f2f4f8; border-color: #343b47; border-radius: 0; padding: 6px 8px; }
        treeview.view header button label { color: #f2f4f8; font-weight: 700; }
        treeview.view:selected, #session-tree:selected, row:selected { background: #28577a; color: #ffffff; }
        treeview.view:hover { background: #1e242c; }
        .dnd-drag-card { background: #202630; color: #ffffff; border: 1px solid #4b89ad; border-radius: 10px; padding: 7px 10px; box-shadow: 0 6px 18px rgba(0,0,0,.35); }
        .dnd-drag-title { color: #ffffff; font-weight: 700; }
        .dnd-drag-detail { color: #b8c7d4; font-size: 10px; }
        .workspace-strip { background: #111419; border-bottom: 1px solid #252b34; padding: 3px 8px; min-height: 22px; }
        .terminal-toast { background: rgba(20,26,36,0.94); color: #cfe1f5; border: 1px solid #33415a; border-radius: 8px; padding: 5px 12px; font-size: 12px; }
        .terminal-hints { background: rgba(9,12,17,0.92); border: 1px solid #2a3341; border-radius: 8px; padding: 8px 10px; }
        .terminal-hints label { font-family: monospace; font-size: 12px; }
        .terminal-search-bar { background: #111419; border-bottom: 1px solid #252b34; padding: 4px 6px; }
        .rdp-toolbar { background: #15181e; border-bottom: 1px solid #2a3039; padding: 5px 8px; }
        .workspace-hint, .muted { color: #a9b1be; }
        .green-dot, .status-dot-ok { color: #49d17d; font-weight: 900; }
        .status-dot-pending { color: #f3c969; font-weight: 900; }
        .status-dot-error { color: #ff7185; font-weight: 900; }
        .status-dot-off { color: #7d8796; font-weight: 900; }
        #terminal-notebook { background: #090b0e; border: none; }
        notebook, notebook stack { background: #0c0e12; color: #eef1f6; }
        notebook header { background: #12151a; border-bottom: 1px solid #2b313b; padding: 1px 4px 0 4px; }
        notebook tab { background: #1a1e25; color: #cdd3dc; border: 1px solid #313844; border-bottom: none; border-radius: 7px 7px 0 0; padding: 3px 8px; margin: 0 1px; }
        notebook tab:checked { background: #0c0e12; color: #ffffff; border-top: 2px solid #55b9f3; }
        notebook.compact-tabs tab { padding: 1px 5px; min-height: 22px; }
        .settings-section > border { border: 1px solid #303743; border-radius: 12px; background: #171a20; }
        .settings-section > label { color: #f2f4f8; font-weight: 700; padding: 2px 8px; }
        .settings-notebook header { background: transparent; border: none; }
        .settings-notebook tab { border-radius: 9px; border: 1px solid transparent; margin: 3px; }
        .settings-notebook tab:checked { background: #26313c; border: 1px solid #3b5265; }
        .modern-dialog { background: #0f1115; color: #f2f4f8; }
        .command-palette { background: #0f1115; color: #f2f4f8; }
        .command-palette-search { min-height: 36px; font-size: 14px; }
        .command-palette-list-card { background: #0b0f14; border: 1px solid #303743; border-radius: 12px; }
        .command-palette-list, .command-palette-list row { background: #0b0f14; color: #eef4fb; }
        .command-palette-list row { border-bottom: 1px solid #202a36; }
        .command-palette-list row:hover { background: #111a24; }
        .command-palette-list row:selected { background: #235d84; color: #ffffff; }
        .command-palette-title { color: #ffffff; font-weight: 700; }
        #tools-dropdown, menu#tools-dropdown { background: #191d24; color: #f5f7fb; border: 1px solid #343a46; border-radius: 10px; padding: 6px; }
        .dropdown-heading { color: #8fd3ff; font-weight: 800; padding: 6px 10px; }
        .dropdown-item { padding: 7px 10px; border-radius: 8px; }
        .diagnostics-shell { background: #0f1115; color: #f2f4f8; }
        .diagnostics-header, .diagnostics-toolbar { background: #15191f; border: 1px solid #303743; border-radius: 11px; padding: 9px; }
        .diagnostics-card { background: #0b0f14; border: 1px solid #303743; border-radius: 10px; }
        .diagnostics-list, .diagnostics-list.view { background: #0b0f14; color: #eaf0f7; }
        .diagnostics-listbox, .diagnostics-listbox row, .diagnostics-listbox row box, .diagnostics-listbox row label { background: #0b0f14; color: #eaf0f7; }
        .diagnostics-listbox row { border-bottom: 1px solid #202a36; }
        .diagnostics-listbox row:hover { background: #101722; }
        .diagnostics-list:selected { background: #245f84; color: #ffffff; }
        .connection-diagnostics-dialog, .connection-diagnostics-dialog > box { background: #0f1115; color: #f2f4f8; }
        .diagnostic-progress, .diagnostic-summary { background: #15191f; border: 1px solid #303743; border-radius: 11px; padding: 10px 12px; }
        .diagnostic-results, .diagnostic-results row { background: #111419; color: #eaf0f7; }
        .diagnostic-row { border-bottom: 1px solid #29313b; }
        .diagnostic-row:hover { background: #171d24; }
        .diagnostic-row.diagnostic-ok { border-left: 4px solid #45c778; }
        .diagnostic-row.diagnostic-warning { border-left: 4px solid #e4b84c; }
        .diagnostic-row.diagnostic-error { border-left: 4px solid #ef667a; }
        .diagnostic-row.diagnostic-info { border-left: 4px solid #57aee4; }
        .diagnostic-summary.diagnostic-ok { background: #173024; border-color: #2e6b4a; }
        .diagnostic-summary.diagnostic-warning { background: #332817; border-color: #725a29; }
        .diagnostic-summary.diagnostic-error { background: #341b22; border-color: #7a3543; }
        .diagnostic-summary.diagnostic-info { background: #172a38; border-color: #315f7c; }
        .session-error-actions { padding-top: 4px; }
        .settings-shell { background: #0f1115; }
        .settings-category-sidebar { background: #15191f; border: 1px solid #303743; border-radius: 12px; padding: 6px; }
        .settings-category-sidebar list { background: transparent; color: #e9edf3; }
        .settings-category-sidebar row { background: transparent; color: #cdd4de; border-radius: 9px; margin: 2px; padding: 7px 10px; min-height: 30px; }
        .settings-category-sidebar row:hover { background: #222832; color: #ffffff; }
        .settings-category-sidebar row:selected { background: #285f84; color: #ffffff; }
        .settings-stack { background: #111419; border: 1px solid #303743; border-radius: 12px; }
        .settings-page-scroll, .settings-page-viewport, .settings-page { background: #111419; color: #eef1f6; }
        .settings-page-scroll undershoot.top, .settings-page-scroll undershoot.bottom { background: none; }
        .modern-dialog .suggested-action { background: #1f6fa8; color: #ffffff; border-color: #4aa8e8; font-weight: 700; }
        .modern-dialog .suggested-action:hover { background: #2479b6; }
        /* Azione principale anche fuori da .modern-dialog: prima solo le
           Preferenze la evidenziavano, mentre Salva (sessione, tunnel) e
           Avvia laboratorio restavano pulsanti grigi qualsiasi. */
        button.suggested-action { background: #1f6fa8; color: #ffffff; border-color: #4aa8e8; font-weight: 700; }
        button.suggested-action:hover { background: #2479b6; }
        button.suggested-action label { color: #ffffff; }
        button.suggested-action:disabled { background: #2a3440; border-color: #3a4552; }
        button.suggested-action:disabled label { color: #8e98a6; }
        .tab-label { color: inherit; font-weight: 600; }
        .tab-close { background: transparent; border: none; border-radius: 7px; padding: 2px 4px; color: #b8c0cb; }
        .tab-close:hover { background: #353c48; color: #ffffff; }
        .tab-status { font-size: 10px; margin-right: 1px; }
        .tab-status.is-connected { color: #22c55e; }
        .tab-status.is-disconnected { color: #ef4444; }
        .tab-reconnect { background: transparent; border: none; border-radius: 7px; padding: 2px 4px; color: #f59e0b; }
        .tab-reconnect:hover { background: #353c48; color: #ffd479; }
        .panel-title, .dialog-heading, .section-heading { color: #f7f8fb; font-weight: 800; font-size: 14px; }
        .shortcut-key { background: #252b34; color: #dff3ff; border: 1px solid #3e4856; border-radius: 7px; padding: 3px 8px; font-family: monospace; font-weight: 700; }
        .card, .form-card, .settings-card { border: 1px solid #303743; background: #171a20; border-radius: 13px; padding: 11px; box-shadow: 0 4px 14px rgba(0,0,0,.22); }
        .info-card, .empty-state { background: #171d25; color: #c9d5e3; border: 1px solid #2e3d4d; border-radius: 12px; padding: 12px; }
        .danger-action { background: #3c1c23; border: 1px solid #7b3442; color: #ffd6dc; border-radius: 9px; padding: 5px 10px; }
        textview, textview text, .log-view { background: #0d1117; color: #dbe6f3; caret-color: #78c9ff; }
        textview text selection { background: #285f87; color: #ffffff; }
        frame > border { border-color: #303743; border-radius: 12px; }
        separator, paned separator { background: #2c323c; min-width: 1px; min-height: 1px; }
        scrollbar { background: transparent; }
        scrollbar slider { background: #454d5a; border-radius: 8px; min-width: 8px; min-height: 8px; }
        scrollbar slider:hover { background: #5b6574; }
        checkbutton, radiobutton { color: #e8ebf1; }
        checkbutton check, radiobutton radio { background: #1d222a; border: 1px solid #48515e; border-radius: 5px; }
        checkbutton check:checked, radiobutton radio:checked { background: #3b9bd5; border-color: #66bdf0; }
        tooltip { background: #232832; color: #ffffff; border: 1px solid #404856; border-radius: 8px; padding: 6px; }
        .rdp-frame { background: #050608; border: 1px solid #303743; border-radius: 12px; }
        .rdp-canvas, .native-rdp-canvas { background: #030507; color: #eef4fb; }
        .native-rdp-canvas:focus { box-shadow: inset 0 0 0 1px #4aa8e8; }
        .error-text { color: #ff8a9a; }
        .success-text { color: #75d99c; }
        .rdp-focus-hint { background: rgba(20,24,30,.94); color: #f4f7fb; border: 1px solid #495464; border-radius: 12px; padding: 8px 13px; margin-top: 12px; box-shadow: 0 4px 12px rgba(0,0,0,.35); }
        .rdp-status-card { background: rgba(20,24,30,.96); color: #eef4fb; border: 1px solid #3b4654; border-radius: 14px; padding: 18px 22px; box-shadow: 0 8px 24px rgba(0,0,0,.42); }
        .rdp-status-card.rdp-status-error { background: rgba(52,27,34,.97); color: #ffe5e9; border-color: #8a3d4b; }
        .rdp-fullscreen-bar { background: rgba(22,27,34,.94); color: #f4f7fb; border: 1px solid rgba(115,132,151,.48); border-radius: 14px; padding: 7px 9px; box-shadow: 0 8px 28px rgba(0,0,0,.52); }
        .rdp-fullscreen-bar button { background: rgba(53,62,74,.92); color: #f4f7fb; border: 1px solid #4a5666; border-radius: 9px; padding: 6px 10px; }
        .rdp-fullscreen-bar button:hover { background: #3b7fb0; border-color: #65a9d8; }
        .rdp-fullscreen-bar button.destructive-action { background: rgba(117,42,53,.92); border-color: #a64b5a; }
        .rdp-log-drawer { background: #0d1117; border-top: 1px solid #303743; border-radius: 0; }
        .notification-banner { background: #17202a; color: #edf5ff; border: 1px solid #30465a; border-radius: 11px; padding: 9px 12px; margin: 8px 10px 2px 10px; }
        .notification-banner.notice-error { background: #341b22; border-color: #7a3543; color: #ffdce2; }
        .notification-banner.notice-warning { background: #332817; border-color: #725a29; color: #ffe8b2; }
        .notification-banner.notice-success { background: #173024; border-color: #2e6b4a; color: #d7ffe8; }

        /* Password Manager: layout espandibile e contrasto indipendente dal tema di sistema. */
        .vault-dialog, .vault-shell { background: #0f1115; color: #f2f4f8; }
        .vault-header { background: #151920; border: 1px solid #303844; border-radius: 12px; padding: 11px 13px; }
        .vault-header label { color: #f4f7fb; }
        .vault-header .muted { color: #b9c3d0; }
        .vault-hint { background: #17283a; border: 1px solid #2c4f70; border-radius: 11px; padding: 8px 12px; }
        .vault-hint label { color: #dbeaf6; }
        .vault-search { min-width: 220px; }
        .vault-sidebar { margin-right: 6px; }
        .vault-list-scroll { background: #12161c; border: 1px solid #323a46; border-radius: 12px; }
        .vault-list { background: #12161c; color: #f1f4f8; }
        .vault-group-header { color: #9cc3e4; font-size: 11px; font-weight: 800; letter-spacing: 1px; padding: 12px 12px 4px 12px; }
        .vault-row { padding: 7px 10px; border-radius: 9px; margin: 1px 4px; }
        .vault-row:hover { background: #1a2029; }
        .vault-row:selected { background: #285f87; }
        .vault-row-title { color: #f4f7fb; font-weight: 700; }
        .vault-row-subtitle { color: #a9b4c2; font-size: 12px; }
        .vault-row:selected .vault-row-title { color: #ffffff; }
        .vault-row:selected .vault-row-subtitle { color: #dbeaf6; }
        .vault-avatar { background: #24415a; color: #cfe8fb; border-radius: 999px; min-width: 32px; min-height: 32px; font-weight: 800; }
        .vault-row:selected .vault-avatar { background: #3d7aa8; color: #ffffff; }
        .vault-badge { background: #222a35; color: #c9d3df; border-radius: 999px; padding: 1px 8px; font-size: 11px; font-weight: 700; }
        .vault-detail { background: #151920; border: 1px solid #303844; border-radius: 12px; }
        .vault-detail-page, .vault-fields { color: #f2f4f8; }
        .vault-detail-title { color: #ffffff; font-size: 19px; font-weight: 800; }
        .vault-field-caption { color: #9aa6b4; font-size: 12px; font-weight: 700; min-width: 74px; padding: 6px 0; }
        .vault-field-value { color: #f2f4f8; padding: 6px 0; }
        .vault-secret { font-family: monospace; letter-spacing: 1px; }
        .vault-usage-scroll, .vault-usage-scroll viewport { background: transparent; border: none; }
        .vault-usage label { color: #e6ebf2; }
        .vault-usage .muted { color: #a9b4c2; }
        .vault-usage-kind { background: #1d3346; color: #a9d3f5; border-radius: 6px; padding: 1px 7px; font-size: 11px; font-weight: 800; }
        .vault-icon-button { padding: 4px 6px; border-radius: 8px; background: transparent; border: none; box-shadow: none; }
        .vault-icon-button:hover, .vault-icon-button:checked { background: #26303c; }
        .vault-delete { background: transparent; color: #ff9b9b; border: 1px solid #6b2c35; border-radius: 9px; padding: 5px 10px; }
        .vault-delete:hover { background: #3a1d23; }
        .vault-delete label, .vault-delete image { color: #ff9b9b; }
        .vault-empty label { color: #f2f4f8; }
        .vault-empty .muted { color: #b9c3d0; }
        .vault-empty-title { font-size: 16px; font-weight: 800; }
        .vault-empty-icon { opacity: 0.55; }
        .vault-error { color: #ff9b9b; font-weight: 700; }
        .vault-notes, .vault-notes textview, .vault-notes text { background: #12161c; color: #f2f4f8; border-color: #343d49; }
        .vault-strength block { min-height: 6px; min-width: 22px; border-radius: 3px; border: none; }
        .vault-strength block.empty { background: #2a313c; }
        .vault-strength block.filled.low { background: #e5534b; }
        .vault-strength block.filled.high { background: #d4a72c; }
        .vault-strength block.filled.full { background: #3fb950; }
        .tunnel-editor, .tunnel-manager { background: #111419; color: #e8ebf1; }
        .tunnel-radio-strip, .tunnel-diagram, .tunnel-card { background: #171a20; border: 1px solid #343b47; border-radius: 12px; padding: 10px; }
        .tunnel-section-title, .tunnel-node-label { color: #f5f7fa; font-weight: 800; }
        .tunnel-big-icon { font-size: 54px; color: #62c4ff; }
        .tunnel-firewall { font-size: 58px; color: #ff9f62; }
        .tunnel-arrow { font-size: 26px; color: #6bbdff; font-weight: 900; }
        .tunnel-line { color: #ff7c99; font-weight: 900; }
        .tunnel-explain { background: #13202a; color: #e8f7ee; border: 1px solid #2c485b; border-radius: 10px; padding: 9px; }

        /* Contrasto finale: evita testo scuro ereditato dal tema desktop. */
        label, button label, checkbutton label, radiobutton label, menuitem label,
        modelbutton label, expander label, frame > label, notebook tab label,
        treeview header button label, combobox button label, popover label,
        .settings-page label, .settings-category-sidebar label, .tunnel-manager label,
        .tunnel-editor label { color: #f5f7fb; text-shadow: none; }
        label:backdrop, button label:backdrop, checkbutton label:backdrop,
        radiobutton label:backdrop, menuitem label:backdrop, notebook tab label:backdrop {
            color: #d2d8e1; text-shadow: none;
        }
        button:disabled label, checkbutton:disabled label, radiobutton:disabled label,
        entry:disabled, spinbutton:disabled, combobox:disabled button,
        combobox:disabled button label { color: #aeb7c4; }
        entry, entry text, spinbutton, spinbutton text, combobox button,
        combobox button label, textview, textview text, treeview.view,
        treeview.view cell { color: #f5f7fb; caret-color: #8fd3ff; }
        treeview header button { background: #1b2027; color: #f5f7fb; border-color: #343b47; }
        treeview header button:hover { background: #272d36; }
        menu, popover, combobox window, combobox menu { background: #191d24; color: #f5f7fb; }
        menuitem:hover, modelbutton:hover { color: #ffffff; }
        .muted, .workspace-hint, .tunnel-flow-detail { color: #c0c8d3; }
        .field-label, .section-heading, .dialog-heading, .panel-title { color: #ffffff; }

        /* Tunnel SSH 2.6 */
        .tunnel-dialog-content, .tunnel-editor-scroll, .tunnel-editor-scroll viewport,
        .tunnel-manager { background: #0f1115; color: #f5f7fb; }
        .tunnel-type-selector { background: #151920; border: 1px solid #323945; border-radius: 12px; padding: 5px; }
        .tunnel-type-selector radiobutton { background: transparent; border-radius: 8px; padding: 7px 12px; }
        .tunnel-type-selector radiobutton:hover { background: #242a33; }
        .tunnel-type-selector radiobutton:checked { background: #244d69; color: #ffffff; }
        .tunnel-settings-card > border, .tunnel-preview-card, .tunnel-table-card,
        .tunnel-detail-card, .tunnel-actions-card { background: #171b22; border: 1px solid #333b47; border-radius: 13px; }
        .tunnel-preview-card, .tunnel-detail-card, .tunnel-actions-card { padding: 12px; }
        .tunnel-flow-node { background: #202630; border: 1px solid #3c4654; border-radius: 12px; padding: 10px; min-width: 128px; }
        .tunnel-flow-node image { color: #7fd0ff; }
        .tunnel-flow-title { color: #ffffff; font-weight: 800; }
        .tunnel-flow-detail { font-size: 10px; }
        .tunnel-flow-arrow { color: #68c8ff; font-size: 24px; font-weight: 900; }
        .tunnel-validation { background: #151920; color: #dce3ec; border: 1px solid #303844; border-radius: 9px; padding: 7px 10px; }
        .tunnel-validation.error-text { background: #351c24; color: #ffdce3; border-color: #81404e; }
        .tunnel-validation.success-text { background: #173126; color: #d9ffea; border-color: #347454; }
        .tunnel-advanced { background: #151920; border: 1px solid #303844; border-radius: 11px; padding: 7px; }
        .tunnel-toolbar, .tunnel-footer { background: #14181e; border: 1px solid #2f3641; border-radius: 11px; padding: 6px; }
        .tunnel-table-card treeview.view { background: #12161c; color: #f5f7fb; }
        .tunnel-table-card treeview.view:selected { background: #285f87; color: #ffffff; }
        .tunnel-detail-card { min-height: 104px; }
        .tunnel-actions-card { min-width: 165px; }
        .tunnel-log-drawer { background: #0d1117; border: 1px solid #303743; border-radius: 12px; padding: 8px; }
        .tunnel-log-drawer textview, .tunnel-log-drawer textview text { background: #090c10; color: #eaf0f7; }

        /* Tunnel editor 2.7: layout a diagramma, senza clipping. */
        .tunnel-editor-header { padding: 2px 2px 4px 2px; }
        .tunnel-editor-viewport { background: #0f1115; }
        .tunnel-route-card > border { background: #171b22; border: 1px solid #333b47; border-radius: 13px; }
        .tunnel-route-node { background: #202630; border: 1px solid #3c4654; border-radius: 12px; padding: 12px; }
        .tunnel-route-node image { color: #7fd0ff; }
        .tunnel-route-arrow { color: #68c8ff; font-size: 28px; font-weight: 900; }
        .tunnel-fields-row { padding: 0; }
        .tunnel-fields-row .tunnel-settings-card { margin: 0; }
        .tunnel-settings-card entry, .tunnel-settings-card spinbutton, .tunnel-settings-card combobox button { min-height: 30px; }
        .tunnel-editor textview, .tunnel-editor textview text { background: #0b0f14; color: #eaf0f7; }

        /* Le superfici operative restano sempre scure. */
        #workspace, #terminal-notebook, #terminal-notebook stack,
        .terminal-surface, .terminal-surface scrolledwindow, .terminal-surface viewport { background: #090c11; color: #eef4fb; }
            .terminal-workspace paned > separator { background: #223; min-width: 2px; min-height: 2px; }
            .terminal-workspace paned > separator:hover { background: #3b82f6; }
        #terminal-notebook textview, #terminal-notebook textview text { background: #0d1117; color: #dbe6f3; }

        /* Tunnel manager 3.0: layout stabile senza Paned */
        .tunnel-manager { background: #0f1115; color: #f5f7fb; }
        .tunnel-toolbar, .tunnel-action-bar, .tunnel-footer { background: #151a21; border: 1px solid #303845; border-radius: 10px; padding: 6px; }
        .tunnel-table-card { background: #12161c; border: 1px solid #343c48; border-radius: 10px; }
        .tunnel-table-card scrolledwindow { background: #12161c; }
        .tunnel-table-card treeview.view { background: #12161c; color: #f5f7fb; }
        .tunnel-table-card treeview.view header button { background: #1b2028; color: #f5f7fb; border-color: #343c48; }
        .tunnel-detail-card { background: #171b22; border: 1px solid #343c48; border-radius: 10px; padding: 10px; }
        .tunnel-log-drawer { background: #10141a; border: 1px solid #343c48; border-radius: 10px; padding: 8px; }
        .tunnel-log-drawer textview, .tunnel-log-drawer textview text { background: #090d12; color: #e7edf5; }

        /* Centro strumenti di rete */
        .network-tools-dialog, .network-tools-shell, .network-tool-page { background: #0f1115; color: #f2f4f8; }
        .network-tools-header { background: #15191f; border-bottom: 1px solid #303743; padding: 2px; }
        .network-tools-sidebar-frame { background: #12161c; border: none; border-right: 1px solid #303743; }
        .network-tools-sidebar-frame > border, .network-tools-content > border { border: none; }
        .network-tools-sidebar { background: transparent; }
        .network-tools-sidebar row, .network-sidebar-row { background: transparent; border: 1px solid transparent; border-radius: 10px; margin: 2px 0; }
        .network-tools-sidebar row:hover, .network-sidebar-row:hover { background: #202630; border-color: #303947; }
        .network-tools-sidebar row:selected, .network-sidebar-row:selected { background: #285f87; border-color: #3d759a; color: #ffffff; }
        .network-sidebar-heading { color: #8f9bab; font-size: 10px; font-weight: 800; letter-spacing: 1px; }
        .network-sidebar-title { color: #f2f5f9; font-weight: 700; }
        .network-sidebar-detail { color: #9eabb9; font-size: 10px; }
        .network-tools-sidebar row:selected .network-sidebar-title, .network-tools-sidebar row:selected .network-sidebar-detail { color: #ffffff; }
        .network-tools-content { background: #0f1115; border: none; }
        .network-card { background: #171b22; border: 1px solid #343d49; border-radius: 12px; }
        .network-card > border { border: none; }
        .tool-heading, .section-heading { color: #ffffff; font-weight: 800; font-size: 15px; }
        .table-heading { color: #dce5ef; font-weight: 700; }
        .service-card { background: #171b22; }
        .service-title { color: #ffffff; font-weight: 800; font-size: 14px; }
        .service-badge { background: #11161c; border: 1px solid #343d49; border-radius: 999px; padding: 4px 9px; }
        .service-action { min-height: 30px; }
        .local-services-switcher { background: #12171d; border-bottom: 1px solid #343d49; padding: 5px; }
        .local-services-switcher button { background: #1a2028; color: #dce5ef; border: 1px solid #343d49; border-radius: 8px; padding: 6px 12px; }
        .local-services-switcher button:checked { background: #285f87; color: #ffffff; border-color: #3d759a; }
        .share-card { background: #171b22; }
        .share-list, .share-list.view { background: #11161d; color: #edf3fa; }
        .share-list header button { background: #1b222b; color: #ffffff; border-color: #37414e; }
        .share-status { background: #0d1218; color: #cbd8e6; border: 1px solid #343d49; border-radius: 8px; padding: 7px 9px; }
        .network-tool-page treeview.view { background: #11161d; color: #edf3fa; }
        .network-tool-page treeview.view header button { background: #1b222b; color: #ffffff; border-color: #37414e; }
        .network-tool-page treeview.view:selected { background: #285f87; color: #ffffff; }
        .capture-log, .capture-log text { background: #090d12; color: #e7eef7; font-family: monospace; }
        .security-note { background: #2a2417; color: #ffe5a6; border: 1px solid #6d5624; border-radius: 9px; padding: 8px 10px; }
        .service-output { background: #0b1016; color: #dce7f3; border: 1px solid #343d49; border-radius: 9px; padding: 8px 10px; font-family: monospace; }

        /* --- restyle 3.21: palette decisa, testo sempre chiaro nello scuro --- */
        #main-root, window, .background { background-color: #0b0f16; }
        * { color: #e6edf5; }
        #sidebar { background: #131a24; border-right: 1px solid #202b3a; }
        #left-rail { background: #0f151e; border-right: 1px solid #202b3a; }
        #workspace, #terminal-notebook, #terminal-notebook stack { background: #080b11; }
        #main-menubar { background: #0f141c; border-bottom: 1px solid #202b3a; color: #e6edf5; }
        #main-menubar menuitem { color: #d7dee8; }
        #ribbon { background: #0f141c; border-bottom: 1px solid #202b3a; }
        #bottom-status { background: #0f141c; border-top: 1px solid #202b3a; color: #aab4c2; }
        .side-title { color: #f2f6fb; font-weight: 600; letter-spacing: .3px; }
        .ribbon-button caption, .ribbon-button label { color: #cbd4df; }
        .session-search, .quick-entry, entry, spinbutton, combobox button {
            background: #0b111a; border: 1px solid #223047; border-radius: 9px; color: #eaf1f9;
        }
        .session-search:focus, .quick-entry:focus, entry:focus { border-color: #3b82f6; box-shadow: 0 0 0 2px rgba(59,130,246,.22); }
        treeview.view, #session-tree, #session-tree.view, .view { background: transparent; color: #cdd6e2; font-size: 12px; }
        #session-tree.view:selected, #session-tree:selected, #session-tree row:selected, treeview.view:selected, row:selected {
            background: #21406b; color: #ffffff; border-radius: 8px;
        }
        .mini-action { border-radius: 8px; padding: 6px; }
        .mini-action:hover { background: #1d2836; }
        #terminal-notebook > header { background: #0b0f16; border-bottom: 1px solid #202b3a; }
        #terminal-notebook > header tab { background: transparent; border: none; padding: 7px 14px; margin: 0 1px; color: #8b96a5; }
        #terminal-notebook > header tab:checked { color: #ffffff; border-bottom: 2px solid #3b82f6; }
        #terminal-notebook > header tab:hover { background: #161d28; }
        /* Pannello di errore di una sessione caduta: esisteva solo nel tema
           chiaro, qui ricadeva sul bianco di serie di GTK dentro un'interfaccia
           scura. */
        #terminal-notebook .session-error-tab,
        #terminal-notebook .session-error-tab scrolledwindow,
        #terminal-notebook .session-error-tab viewport,
        #terminal-notebook .session-error-tab textview,
        #terminal-notebook .session-error-tab textview text { background: #0d1117; color: #dbe6f3; caret-color: #dbe6f3; }
        button { border-radius: 9px; }

        """

    @staticmethod
    def light_css() -> str:
        return """
        window, .background, dialog, messagedialog, popover, menu, tooltip { background-color: #f5f7fa; color: #17202a; }
        window label, dialog label, popover label, menu label, button label, checkbutton, radiobutton { color: #17202a; }
        button image, .adaptive-icon, .ribbon-button image, .rail-button image, .mini-action image, .secondary-action image { color: #243244; opacity: 1; -gtk-icon-shadow: none; }
        button:disabled image, button:disabled label { color: #657386; opacity: .72; }
        #main-root { background: #eef2f6; color: #17202a; font-size: 12px; }
        #main-root:backdrop, window:backdrop, .background:backdrop { background-color: #eef2f6; color: #667085; }
        #main-menubar, #ribbon, #bottom-status { background: #ffffff; color: #17202a; border-color: #d8dee7; }
        #main-menubar { border-bottom: 1px solid #d8dee7; padding: 2px 7px; }
        #main-menubar menuitem { padding: 5px 9px; border-radius: 7px; }
        #main-menubar menuitem:hover { background: #e9eef4; }
        #main-menubar:backdrop, #main-menubar:disabled { background: #ffffff; color: #667085; }
        #main-menubar:backdrop menuitem, #main-menubar menuitem:backdrop, #main-menubar menuitem:disabled {
            background-color: transparent; background-image: none; color: #667085;
            border-color: transparent; box-shadow: none; text-shadow: none;
        }
        #main-menubar menu { background: #ffffff; color: #17202a; border: 1px solid #d4dbe5; border-radius: 10px; padding: 6px; }
        #main-menubar menu menuitem, #main-menubar menu .dropdown-item { padding: 7px 10px; border-radius: 8px; }
        #main-menubar menu menuitem:hover, #main-menubar menu .dropdown-item:hover { background: #edf2f7; }
        #main-menubar menu separator { border-top: 1px solid #d3dbe5; margin: 5px 4px; }
        #main-menubar menu image { color: #405267; margin-right: 8px; opacity: 1; }
        #ribbon { border-bottom: 1px solid #d8dee7; padding: 2px 5px; min-height: 48px; }
        .ribbon-group { background: transparent; border: none; border-right: 1px solid #d8dee7; border-radius: 0; padding: 1px 5px 1px 1px; margin-right: 4px; box-shadow: none; }
        .ribbon-group-title { min-height: 0; font-size: 0; padding: 0; margin: 0; }
        .ribbon-caption { color: #263241; font-size: 9px; font-weight: 600; }
        #left-rail { background: #e8edf3; border-right: 1px solid #d3dae4; padding: 4px 2px; min-width: 30px; }
        #sidebar { background: #ffffff; border-right: 1px solid #d3dae4; }
        #workspace { background: #f6f8fb; }
        #bottom-status { border-top: 1px solid #d8dee7; min-height: 20px; color: #536174; }
        .footer-version { color: #0878b8; }
        .ribbon-button, .ribbon-exit { background: transparent; color: #253244; border: 1px solid transparent; border-radius: 6px; padding: 2px 5px; min-width: 42px; }
        .rail-button { background: transparent; color: #253244; border: 1px solid transparent; border-radius: 6px; padding: 4px; min-width: 26px; }
        .ribbon-button:hover, .rail-button:hover { background: #e7edf4; border-color: #cbd5e1; }
        .ribbon-button:active, .rail-button:active { background: #d7e0ea; }
        .tunnel-editor { background: #ffffff; color: #17202a; }
        .tunnel-editor label { color: #17202a; text-shadow: none; }
        .tunnel-editor textview, .tunnel-editor textview text { background: #ffffff; color: #17202a; }

        /* --- Parita' col tema scuro -------------------------------------
           Queste regole esistevano solo in dark_css(): in tema chiaro le
           classi corrispondenti ricadevano sull'aspetto GTK di serie. Il caso
           grave erano .is-connected/.is-disconnected, cioe' il pallino di
           stato della connessione: senza colore definito prendeva il colore
           del testo e smetteva di indicare qualunque cosa.
           Colori scelti e verificati sopra 4.5:1 contro lo sfondo reale della
           barra schede (#e9edf3), non contro il bianco. ------------------ */
        .tab-label { color: inherit; font-weight: 600; }
        .tab-status { font-size: 10px; margin-right: 1px; }
        .tab-status.is-connected { color: #14672f; }
        .tab-status.is-disconnected { color: #b91c1c; }
        .tab-close { background: transparent; border: none; border-radius: 7px; padding: 2px 4px; color: #5b6676; }
        .tab-close:hover { background: #dde4ee; color: #17202a; }
        .tab-reconnect { background: transparent; border: none; border-radius: 7px; padding: 2px 4px; color: #9a4708; }
        .tab-reconnect:hover { background: #dde4ee; color: #7c3a06; }
        .diagnostics-listbox, .diagnostics-listbox row, .diagnostics-listbox row box, .diagnostics-listbox row label { background: #ffffff; color: #17202a; }
        .diagnostics-listbox row { border-bottom: 1px solid #dde4ec; }
        .diagnostics-listbox row:hover { background: #eef2f7; }
        scrolledwindow.session-scroll { background: #ffffff; }
        .tunnel-explain { background: #eef7f2; color: #14532d; border: 1px solid #bcd9c9; border-radius: 10px; padding: 9px; }
        #ribbon:backdrop, #left-rail:backdrop, #bottom-status:backdrop { background: #ffffff; color: #667085; }
        .ribbon-button:backdrop, .ribbon-button:disabled, .rail-button:backdrop, .rail-button:disabled {
            background-color: transparent; background-image: none; color: #7a8494;
            border-color: transparent; box-shadow: none; text-shadow: none;
        }
        .ribbon-button image:backdrop, .rail-button image:backdrop, .adaptive-icon:backdrop { color: #445164; opacity: .94; -gtk-icon-shadow: none; }
        .ribbon-exit:hover { background: #ffe9ec; border-color: #f1b8c0; color: #9f2739; }
        .quick-box { background: #f8fafc; border-bottom: 1px solid #d7dee7; padding: 4px; }
        .command-palette { background: transparent; }
        .command-palette-body { background: #ffffff; border: 1px solid #d7dee7; border-radius: 14px; padding: 8px; }
        .command-palette-entry { min-height: 40px; font-size: 15px; margin-bottom: 6px; background: #f4f7fb; border: 1px solid #cbd4df; border-radius: 10px; color: #16202d; }
        .command-palette-entry:focus { border-color: #2f7fd6; box-shadow: 0 0 0 2px rgba(47,127,214,.16); }
        .command-palette-list { background: transparent; color: #1b2733; }
        .command-palette-list:selected, .command-palette-list row:selected { background: #2f7fd6; color: #ffffff; }
        .command-palette-hint { color: #7a8797; padding: 6px 4px 2px 4px; }
        .quick-connect-button, .sidebar-collapse { background: transparent; border: none; border-radius: 6px; padding: 4px; }
        .session-toolbar .mini-action, .side-footer .mini-action { padding: 3px; min-height: 24px; }
        entry, spinbutton, combobox button, .quick-entry, .session-search { min-height: 30px; background: #ffffff; color: #16202d; border: 1px solid #cbd4df; border-radius: 9px; padding: 4px 9px; }
        entry:focus, spinbutton:focus, combobox button:focus { border-color: #268dcc; box-shadow: 0 0 0 2px rgba(38,141,204,.15); }
        button { background: #ffffff; color: #1c2938; border: 1px solid #cbd4df; border-radius: 9px; padding: 5px 9px; }
        button:hover { background: #edf2f7; border-color: #b9c5d2; }
        button:disabled { background: #edf1f5; color: #657386; border-color: #d2d9e2; }
        modelbutton { background: transparent; color: #17202a; border-radius: 7px; padding: 6px 9px; }
        modelbutton:hover { background: #e9eef4; }
        headerbar, .titlebar { background: #ffffff; color: #17202a; border-color: #d8dee7; }
        list, listbox, row, placessidebar, placessidebar row { background: #ffffff; color: #17202a; }
        row:hover, placessidebar row:hover { background: #eef3f7; }
        switch { background: #c8d1dc; border-radius: 12px; }
        switch:checked { background: #268dcc; }
        .small-action, .mini-action, .secondary-action { background: #ffffff; color: #1c2938; border: 1px solid #cbd4df; border-radius: 9px; padding: 5px 10px; }
        .small-action:hover, .mini-action:hover, .secondary-action:hover { background: #edf2f7; }
        .mini-action:checked { background: #cce9fb; border-color: #9ed0ee; }
        .mini-action:checked image { color: #102231; }
        .side-titlebar, .session-toolbar, .side-footer, .workspace-strip, .rdp-toolbar { background: #f8fafc; border-color: #d7dee7; padding: 4px 7px; }
        .side-titlebar, .session-toolbar, .workspace-strip, .rdp-toolbar { border-bottom: 1px solid #d7dee7; }
        .quick-label, .side-title, .field-label { color: #243244; font-weight: 600; }
        .count-badge { color: #06304b; background: #b9e5ff; border-radius: 10px; padding: 2px 7px; font-weight: 800; }
        scrolledwindow, viewport { background: #ffffff; border: none; }
        scrolledwindow.remote-files-scroll { background: #ffffff; }
        .remote-session-label { color: #1b2b3d; font-weight: 700; }
        .remote-path, .remote-status { color: #667386; font-size: 10px; }
        .sidebar-pages header { background: #f8fafc; border-bottom: 1px solid #d7dee7; }
        .sidebar-pages tab { padding: 3px 7px; }
        treeview.view, #session-tree, .view { background: #ffffff; color: #17202a; }
        treeview.view header button { background: #eef2f6; color: #17202a; border-color: #cbd4df; border-radius: 0; padding: 6px 8px; }
        treeview.view header button label { color: #17202a; font-weight: 700; }
        treeview.view:selected, #session-tree:selected, row:selected { background: #cce9fb; color: #102231; }
        .dnd-drag-card { background: #ffffff; color: #17202a; border: 1px solid #6bb8e8; border-radius: 10px; padding: 7px 10px; box-shadow: 0 6px 18px rgba(15,23,42,.20); }
        .dnd-drag-title { color: #17202a; font-weight: 700; }
        .dnd-drag-detail { color: #667085; font-size: 10px; }
        notebook, notebook stack, #terminal-notebook { background: #ffffff; color: #17202a; }
        notebook header { background: #f2f5f8; border-bottom: 1px solid #d6dde6; padding: 1px 4px 0 4px; }
        notebook tab { background: #e8edf3; color: #3c4959; border: 1px solid #cfd7e1; border-bottom: none; border-radius: 7px 7px 0 0; padding: 3px 8px; margin: 0 1px; }
        notebook tab:checked { background: #ffffff; color: #101820; border-top: 2px solid #2493d1; }
        notebook.compact-tabs tab { padding: 1px 5px; min-height: 22px; }
        .settings-section > border { border: 1px solid #d3dbe5; border-radius: 12px; background: #ffffff; }
        .settings-section > label { color: #17202a; font-weight: 700; padding: 2px 8px; }
        .settings-notebook tab { border-radius: 9px; }
        .modern-dialog { background: #f5f7fa; color: #17202a; }
        .command-palette { background: #f5f7fa; color: #17202a; }
        .command-palette-search { min-height: 36px; font-size: 14px; }
        .command-palette-list-card { background: #ffffff; border: 1px solid #cbd4df; border-radius: 12px; }
        .command-palette-list, .command-palette-list row { background: #ffffff; color: #17202a; }
        .command-palette-list row { border-bottom: 1px solid #e5ebf2; }
        .command-palette-list row:hover { background: #f3f7fb; }
        .command-palette-list row:selected { background: #cce9fb; color: #102231; }
        .command-palette-title { color: #17202a; font-weight: 700; }
        #tools-dropdown, menu#tools-dropdown { background: #ffffff; color: #17202a; border: 1px solid #d4dbe5; border-radius: 10px; padding: 6px; }
        .dropdown-heading { color: #0878b8; font-weight: 800; padding: 6px 10px; }
        .dropdown-item { padding: 7px 10px; border-radius: 8px; }
        .diagnostics-shell { background: #f5f7fa; color: #17202a; }
        .diagnostics-header, .diagnostics-toolbar { background: #ffffff; border: 1px solid #ccd6e0; border-radius: 11px; padding: 9px; }
        .diagnostics-card { background: #ffffff; border: 1px solid #ccd6e0; border-radius: 10px; }
        .diagnostics-list, .diagnostics-list.view { background: #ffffff; color: #17202a; }
        .diagnostics-list:selected { background: #cbe9fb; color: #102331; }
        .connection-diagnostics-dialog, .connection-diagnostics-dialog > box { background: #f5f7fa; color: #17202a; }
        .diagnostic-progress, .diagnostic-summary { background: #ffffff; border: 1px solid #ccd6e0; border-radius: 11px; padding: 10px 12px; }
        .diagnostic-results, .diagnostic-results row { background: #ffffff; color: #17202a; }
        .diagnostic-row { border-bottom: 1px solid #e1e7ee; }
        .diagnostic-row:hover { background: #f5f8fb; }
        .diagnostic-row.diagnostic-ok { border-left: 4px solid #269e5b; }
        .diagnostic-row.diagnostic-warning { border-left: 4px solid #c58a12; }
        .diagnostic-row.diagnostic-error { border-left: 4px solid #d43f59; }
        .diagnostic-row.diagnostic-info { border-left: 4px solid #268dcc; }
        .diagnostic-summary.diagnostic-ok { background: #eefaf3; border-color: #b9dfc8; }
        .diagnostic-summary.diagnostic-warning { background: #fff8e7; border-color: #e9d09b; }
        .diagnostic-summary.diagnostic-error { background: #fff0f2; border-color: #efbec6; }
        .diagnostic-summary.diagnostic-info { background: #edf6fc; border-color: #b9d8eb; }
        .session-error-actions { padding-top: 4px; }
        .settings-shell { background: #f5f7fa; }
        .settings-category-sidebar { background: #ffffff; border: 1px solid #d3dbe5; border-radius: 12px; padding: 6px; }
        .settings-category-sidebar list { background: transparent; color: #263241; }
        .settings-category-sidebar row { background: transparent; color: #445164; border-radius: 9px; margin: 2px; padding: 7px 10px; min-height: 30px; }
        .settings-category-sidebar row:hover { background: #edf2f7; color: #17202a; }
        .settings-category-sidebar row:selected { background: #cfeaff; color: #10334a; }
        .settings-stack { background: #ffffff; border: 1px solid #d3dbe5; border-radius: 12px; }
        .settings-page-scroll, .settings-page-viewport, .settings-page { background: #ffffff; color: #17202a; }
        .settings-page-scroll undershoot.top, .settings-page-scroll undershoot.bottom { background: none; }
        .modern-dialog .suggested-action { background: #1f6fa8; color: #ffffff; border-color: #1a5f91; font-weight: 700; }
        .modern-dialog .suggested-action:hover { background: #2479b6; }
        /* Azione principale anche fuori da .modern-dialog: prima solo le
           Preferenze la evidenziavano, mentre Salva (sessione, tunnel) e
           Avvia laboratorio restavano pulsanti grigi qualsiasi. */
        button.suggested-action { background: #1f6fa8; color: #ffffff; border-color: #1a5f91; font-weight: 700; }
        button.suggested-action:hover { background: #2479b6; }
        button.suggested-action label { color: #ffffff; }
        button.suggested-action:disabled { background: #e3e8ee; border-color: #d3dae4; }
        button.suggested-action:disabled label { color: #657386; }
        .panel-title, .dialog-heading, .section-heading { color: #111a24; font-weight: 800; font-size: 14px; }
        .shortcut-key { background: #edf2f7; color: #164f72; border: 1px solid #cbd5df; border-radius: 7px; padding: 3px 8px; font-family: monospace; font-weight: 700; }
        .muted, .workspace-hint { color: #687587; }
        .card, .form-card, .settings-card { border: 1px solid #d3dbe5; background: #ffffff; border-radius: 13px; padding: 11px; box-shadow: 0 4px 14px rgba(32,45,60,.08); }
        .info-card, .empty-state { background: #f7fafc; color: #445467; border: 1px solid #d8e1ea; border-radius: 12px; padding: 12px; }
        textview, textview text, .log-view { background: #ffffff; color: #16202d; caret-color: #1b82be; }
        frame > border { border-color: #d3dbe5; border-radius: 12px; }
        separator, paned separator { background: #d5dce5; }
        scrollbar slider { background: #b5bfcb; border-radius: 8px; min-width: 8px; min-height: 8px; }
        .status-dot-ok, .green-dot { color: #149a55; font-weight: 900; }
        .status-dot-pending { color: #c58a12; font-weight: 900; }
        .status-dot-error { color: #d43f59; font-weight: 900; }
        .status-dot-off { color: #778496; font-weight: 900; }
        .rdp-frame { background: #14171b; border: 1px solid #c8d1dc; border-radius: 12px; }
        .rdp-canvas, .native-rdp-canvas { background: #111820; color: #f5f8fb; }
        .native-rdp-canvas:focus { box-shadow: inset 0 0 0 1px #2493d1; }
        .error-text { color: #b4233b; }
        .success-text { color: #137a45; }
        .rdp-focus-hint { background: rgba(255,255,255,.95); color: #17202a; border: 1px solid #cbd5df; border-radius: 12px; padding: 8px 13px; margin-top: 12px; box-shadow: 0 4px 12px rgba(30,45,60,.18); }
        .rdp-status-card { background: rgba(255,255,255,.97); color: #17202a; border: 1px solid #cbd5df; border-radius: 14px; padding: 18px 22px; box-shadow: 0 8px 24px rgba(30,45,60,.18); }
        .rdp-status-card.rdp-status-error { background: rgba(255,240,242,.98); color: #862b3b; border-color: #efbec6; }
        .rdp-fullscreen-bar { background: rgba(250,252,255,.96); color: #17202a; border: 1px solid #c8d2dc; border-radius: 14px; padding: 7px 9px; box-shadow: 0 8px 28px rgba(30,45,60,.22); }
        .rdp-fullscreen-bar button { background: #edf2f7; color: #17202a; border: 1px solid #c7d0da; border-radius: 9px; padding: 6px 10px; }
        .rdp-fullscreen-bar button:hover { background: #d9edf9; border-color: #6aaed5; }
        .rdp-fullscreen-bar button.destructive-action { background: #fff0f2; color: #862b3b; border-color: #e9aeb8; }
        .rdp-log-drawer { background: #ffffff; border-top: 1px solid #d3dbe5; border-radius: 0; }
        .notification-banner { background: #edf6fc; color: #18364a; border: 1px solid #b9d8eb; border-radius: 11px; padding: 9px 12px; margin: 8px 10px 2px 10px; }
        .notification-banner.notice-error { background: #fff0f2; border-color: #efbec6; color: #862b3b; }
        .notification-banner.notice-warning { background: #fff8e7; border-color: #e9d09b; color: #745318; }
        .notification-banner.notice-success { background: #eefaf3; border-color: #b9dfc8; color: #22633c; }

        /* Password Manager: il contenuto occupa tutta la finestra e resta leggibile. */
        .vault-dialog, .vault-shell { background: #f5f7fa; color: #17202a; }
        .vault-header { background: #ffffff; border: 1px solid #d2dae4; border-radius: 12px; padding: 11px 13px; }
        .vault-header label { color: #111a24; }
        .vault-header .muted { color: #526174; }
        .vault-hint { background: #eaf4fd; border: 1px solid #b7d7f2; border-radius: 11px; padding: 8px 12px; }
        .vault-hint label { color: #173a57; }
        .vault-search { min-width: 220px; background: #ffffff; color: #111827; }
        .vault-sidebar { margin-right: 6px; }
        .vault-list-scroll { background: #ffffff; border: 1px solid #cbd4df; border-radius: 12px; }
        .vault-list { background: #ffffff; color: #111827; }
        .vault-group-header { color: #4a5d73; font-size: 11px; font-weight: 800; letter-spacing: 1px; padding: 12px 12px 4px 12px; }
        .vault-row { padding: 7px 10px; border-radius: 9px; margin: 1px 4px; }
        .vault-row:hover { background: #f0f4f8; }
        .vault-row:selected { background: #c5e7fb; }
        .vault-row-title { color: #111a24; font-weight: 700; }
        .vault-row-subtitle { color: #4b5a6b; font-size: 12px; }
        .vault-row:selected .vault-row-title { color: #0b2538; }
        .vault-row:selected .vault-row-subtitle { color: #24435b; }
        .vault-avatar { background: #dbe9f6; color: #1b4f78; border-radius: 999px; min-width: 32px; min-height: 32px; font-weight: 800; }
        .vault-row:selected .vault-avatar { background: #1f6fa8; color: #ffffff; }
        .vault-badge { background: #e9eef4; color: #3a4a5c; border-radius: 999px; padding: 1px 8px; font-size: 11px; font-weight: 700; }
        .vault-detail { background: #ffffff; border: 1px solid #d2dae4; border-radius: 12px; }
        .vault-detail-page, .vault-fields { color: #17202a; }
        .vault-detail-title { color: #0f1720; font-size: 19px; font-weight: 800; }
        .vault-field-caption { color: #5b6676; font-size: 12px; font-weight: 700; min-width: 74px; padding: 6px 0; }
        .vault-field-value { color: #111a24; padding: 6px 0; }
        .vault-secret { font-family: monospace; letter-spacing: 1px; }
        .vault-usage-scroll, .vault-usage-scroll viewport { background: transparent; border: none; }
        .vault-usage label { color: #1c2938; }
        .vault-usage .muted { color: #5b6676; }
        .vault-usage-kind { background: #e3f0fb; color: #17507d; border-radius: 6px; padding: 1px 7px; font-size: 11px; font-weight: 800; }
        .vault-icon-button { padding: 4px 6px; border-radius: 8px; background: transparent; border: none; box-shadow: none; }
        .vault-icon-button:hover, .vault-icon-button:checked { background: #e6edf4; }
        .vault-delete { background: transparent; color: #b42318; border: 1px solid #f0b6b0; border-radius: 9px; padding: 5px 10px; }
        .vault-delete:hover { background: #fdecea; }
        .vault-delete label, .vault-delete image { color: #b42318; }
        .vault-empty label { color: #17202a; }
        .vault-empty .muted { color: #526174; }
        .vault-empty-title { font-size: 16px; font-weight: 800; }
        .vault-empty-icon { opacity: 0.55; }
        .vault-error { color: #b91c1c; font-weight: 700; }
        .vault-notes, .vault-notes textview, .vault-notes text { background: #ffffff; color: #111827; border-color: #cbd4df; }
        .vault-strength block { min-height: 6px; min-width: 22px; border-radius: 3px; border: none; }
        .vault-strength block.empty { background: #dde3ea; }
        .vault-strength block.filled.low { background: #c9372c; }
        .vault-strength block.filled.high { background: #b7791f; }
        .vault-strength block.filled.full { background: #1a7f37; }
        .tunnel-radio-strip, .tunnel-diagram, .tunnel-card { background: #ffffff; border: 1px solid #d3dbe5; border-radius: 12px; padding: 10px; }
        .tunnel-dialog-content, .tunnel-editor-scroll, .tunnel-editor-scroll viewport, .tunnel-manager { background: #f5f7fa; color: #17202a; }
        .tunnel-type-selector { background: #eef2f6; border: 1px solid #d0d8e2; border-radius: 12px; padding: 5px; }
        .tunnel-type-selector radiobutton { border-radius: 8px; padding: 7px 12px; }
        .tunnel-type-selector radiobutton:hover { background: #e2e8ef; }
        .tunnel-type-selector radiobutton:checked { background: #cfeaff; color: #10334a; }
        .tunnel-settings-card > border, .tunnel-preview-card, .tunnel-table-card,
        .tunnel-detail-card, .tunnel-actions-card { background: #ffffff; border: 1px solid #d3dbe5; border-radius: 13px; }
        .tunnel-preview-card, .tunnel-detail-card, .tunnel-actions-card { padding: 12px; }
        .tunnel-flow-node { background: #f5f8fb; border: 1px solid #d0d9e3; border-radius: 12px; padding: 10px; min-width: 128px; }
        .tunnel-flow-title { color: #17202a; font-weight: 800; }
        .tunnel-flow-detail { color: #667085; font-size: 10px; }
        .tunnel-flow-arrow { color: #268dcc; font-size: 24px; font-weight: 900; }
        .tunnel-validation { background: #f6f8fa; color: #445164; border: 1px solid #d3dbe5; border-radius: 9px; padding: 7px 10px; }
        .tunnel-validation.error-text { background: #fff0f2; color: #862b3b; border-color: #efbec6; }
        .tunnel-validation.success-text { background: #eefaf3; color: #22633c; border-color: #b9dfc8; }
        .tunnel-advanced { background: #f8fafc; border: 1px solid #d3dbe5; border-radius: 11px; padding: 7px; }
        .tunnel-toolbar, .tunnel-footer { background: #f8fafc; border: 1px solid #d3dbe5; border-radius: 11px; padding: 6px; }
        .tunnel-table-card treeview.view { background: #ffffff; color: #17202a; }
        .tunnel-table-card treeview.view:selected { background: #cce9fb; color: #102231; }
        .tunnel-detail-card { min-height: 104px; }
        .tunnel-actions-card { min-width: 165px; }
        .tunnel-log-drawer { background: #ffffff; border: 1px solid #d3dbe5; border-radius: 12px; padding: 8px; }

        /* Tunnel editor 2.7: layout a diagramma, senza clipping. */
        .tunnel-editor-header { padding: 2px 2px 4px 2px; }
        .tunnel-editor-viewport { background: #f5f7fa; }
        .tunnel-route-card > border { background: #ffffff; border: 1px solid #d3dbe5; border-radius: 13px; }
        .tunnel-route-node { background: #f5f8fb; border: 1px solid #d0d9e3; border-radius: 12px; padding: 12px; }
        .tunnel-route-node image { color: #268dcc; }
        .tunnel-route-arrow { color: #268dcc; font-size: 28px; font-weight: 900; }
        .tunnel-fields-row { padding: 0; }
        .tunnel-fields-row .tunnel-settings-card { margin: 0; }
        .tunnel-settings-card entry, .tunnel-settings-card spinbutton, .tunnel-settings-card combobox button { min-height: 30px; }

        /* Tema chiaro: il workspace generico e le schede informative restano chiari.
           Solo il terminale e le superfici operative RDP/log mantengono il fondo scuro. */
        #workspace, #terminal-notebook, #terminal-notebook stack { background: #ffffff; color: #17202a; }
        .terminal-surface, .terminal-surface scrolledwindow, .terminal-surface viewport { background: #090c11; color: #eef4fb; }
            .terminal-workspace paned > separator { background: #223; min-width: 2px; min-height: 2px; }
            .terminal-workspace paned > separator:hover { background: #3b82f6; }
        .terminal-surface textview, .terminal-surface textview text { background: #0d1117; color: #dbe6f3; caret-color: #78c9ff; }
        #terminal-notebook .session-error-tab,
        #terminal-notebook .session-error-tab scrolledwindow,
        #terminal-notebook .session-error-tab viewport,
        #terminal-notebook .session-error-tab textview,
        #terminal-notebook .session-error-tab textview text { background: #ffffff; color: #17202a; caret-color: #17202a; }
        .rdp-toolbar { background: #15181e; color: #f5f7fb; border-bottom: 1px solid #2a3039; }
        .rdp-toolbar label, .rdp-toolbar button label,
        .rdp-focus-hint label, .rdp-status-card label, .rdp-fullscreen-bar label,
        .notification-banner label { color: #f5f7fb; }
        .rdp-toolbar button { background: #232831; color: #f5f7fb; border-color: #3a424f; }
        .rdp-toolbar button image, .rdp-fullscreen-bar button image { color: #f5f7fb; opacity: 1; }
        .rdp-log-drawer { background: #0d1117; color: #dbe6f3; border-top: 1px solid #303743; }
        .rdp-log-drawer textview, .rdp-log-drawer textview text { background: #090c10; color: #eaf0f7; }
        .rdp-focus-hint, .rdp-status-card, .rdp-fullscreen-bar { background: rgba(20,24,30,.96); color: #eef4fb; border-color: #3b4654; }
        .rdp-status-card.rdp-status-error { background: rgba(52,27,34,.97); color: #ffe5e9; border-color: #8a3d4b; }
        .rdp-fullscreen-bar button { background: #353e4a; color: #f4f7fb; border-color: #4a5666; }
        .notification-banner { background: #17202a; color: #edf5ff; border-color: #30465a; }
        .notification-banner.notice-error { background: #341b22; border-color: #7a3543; color: #ffdce2; }
        .notification-banner.notice-warning { background: #332817; border-color: #725a29; color: #ffe8b2; }
        .notification-banner.notice-success { background: #173024; border-color: #2e6b4a; color: #d7ffe8; }

        /* Tunnel manager 3.0: chrome chiaro, log operativo scuro */
        .tunnel-manager { background: #f5f7fa; color: #17202a; }
        .tunnel-toolbar, .tunnel-action-bar, .tunnel-footer { background: #ffffff; border: 1px solid #d3dbe5; border-radius: 10px; padding: 6px; }
        .tunnel-table-card { background: #ffffff; border: 1px solid #d3dbe5; border-radius: 10px; }
        .tunnel-table-card scrolledwindow { background: #ffffff; }
        .tunnel-table-card treeview.view { background: #ffffff; color: #17202a; }
        .tunnel-table-card treeview.view header button { background: #edf2f7; color: #17202a; border-color: #cbd4df; }
        .tunnel-detail-card { background: #ffffff; border: 1px solid #d3dbe5; border-radius: 10px; padding: 10px; }
        .tunnel-log-drawer { background: #111820; color: #f2f6fb; border: 1px solid #33404e; border-radius: 10px; padding: 8px; }
        .tunnel-log-drawer label { color: #f2f6fb; }
        .tunnel-log-drawer textview, .tunnel-log-drawer textview text { background: #090d12; color: #e7edf5; }

        /* Centro strumenti di rete */
        .network-tools-dialog, .network-tools-shell, .network-tool-page { background: #f5f7fa; color: #17202a; }
        .network-tools-header { background: #ffffff; border-bottom: 1px solid #d3dbe5; padding: 2px; }
        .network-tools-sidebar-frame { background: #eef2f6; border: none; border-right: 1px solid #d3dbe5; }
        .network-tools-sidebar-frame > border, .network-tools-content > border { border: none; }
        .network-tools-sidebar { background: transparent; }
        .network-tools-sidebar row, .network-sidebar-row { background: transparent; border: 1px solid transparent; border-radius: 10px; margin: 2px 0; }
        .network-tools-sidebar row:hover, .network-sidebar-row:hover { background: #e5ebf2; border-color: #d1d9e3; }
        .network-tools-sidebar row:selected, .network-sidebar-row:selected { background: #cce9fb; border-color: #9ed0ee; color: #102231; }
        .network-sidebar-heading { color: #667085; font-size: 10px; font-weight: 800; letter-spacing: 1px; }
        .network-sidebar-title { color: #17202a; font-weight: 700; }
        .network-sidebar-detail { color: #667085; font-size: 10px; }
        .network-tools-sidebar row:selected .network-sidebar-title, .network-tools-sidebar row:selected .network-sidebar-detail { color: #102231; }
        .network-tools-content { background: #f5f7fa; border: none; }
        .network-card { background: #ffffff; border: 1px solid #d3dbe5; border-radius: 12px; }
        .network-card > border { border: none; }
        .tool-heading, .section-heading { color: #17202a; font-weight: 800; font-size: 15px; }
        .table-heading { color: #26364a; font-weight: 700; }
        .service-card { background: #ffffff; }
        .service-title { color: #17202a; font-weight: 800; font-size: 14px; }
        .service-badge { background: #f4f7fa; border: 1px solid #d3dbe5; border-radius: 999px; padding: 4px 9px; }
        .service-action { min-height: 30px; }
        .local-services-switcher { background: #eef2f6; border-bottom: 1px solid #d3dbe5; padding: 5px; }
        .local-services-switcher button { background: #f7f9fb; color: #26364a; border: 1px solid #d3dbe5; border-radius: 8px; padding: 6px 12px; }
        .local-services-switcher button:checked { background: #cce9fb; color: #102231; border-color: #9ed0ee; }
        .share-card { background: #ffffff; }
        .share-list, .share-list.view { background: #ffffff; color: #17202a; }
        .share-list header button { background: #edf2f7; color: #17202a; border-color: #cbd4df; }
        .share-status { background: #f5f8fb; color: #445164; border: 1px solid #d3dbe5; border-radius: 8px; padding: 7px 9px; }
        .network-tool-page treeview.view { background: #ffffff; color: #17202a; }
        .network-tool-page treeview.view header button { background: #edf2f7; color: #17202a; border-color: #cbd4df; }
        .network-tool-page treeview.view:selected { background: #cce9fb; color: #102231; }
        .capture-log, .capture-log text { background: #090d12; color: #e7eef7; font-family: monospace; }
        .security-note { background: #fff7df; color: #694d13; border: 1px solid #e5ca83; border-radius: 9px; padding: 8px 10px; }
        .service-output { background: #111820; color: #e7eef7; border: 1px solid #33404e; border-radius: 9px; padding: 8px 10px; font-family: monospace; }

        /* --- restyle 3.21: palette decisa, testo sempre scuro nel chiaro --- */
        #main-root, window, .background { background-color: #e9edf3; }
        * { color: #1b2530; }
        #sidebar { background: #f7f9fc; border-right: 1px solid #d5dce6; }
        #left-rail { background: #eef2f7; border-right: 1px solid #d5dce6; }
        #workspace, #terminal-notebook, #terminal-notebook stack { background: #dfe4ec; }
        #main-menubar { background: #f7f9fc; border-bottom: 1px solid #d5dce6; color: #1b2530; }
        #main-menubar menuitem { color: #35414f; }
        #ribbon { background: #f7f9fc; border-bottom: 1px solid #d5dce6; }
        #bottom-status { background: #f7f9fc; border-top: 1px solid #d5dce6; color: #5a6674; }
        .side-title { color: #12202f; font-weight: 600; letter-spacing: .3px; }
        .ribbon-button caption, .ribbon-button label { color: #35414f; }
        .session-search, .quick-entry, entry, spinbutton, combobox button {
            background: #ffffff; border: 1px solid #c8d2df; border-radius: 9px; color: #12202f;
        }
        .session-search:focus, .quick-entry:focus, entry:focus { border-color: #2f7fd6; box-shadow: 0 0 0 2px rgba(47,127,214,.18); }
        treeview.view, #session-tree, #session-tree.view, .view { background: transparent; color: #28323d; font-size: 12px; }
        #session-tree.view:selected, #session-tree:selected, #session-tree row:selected, treeview.view:selected, row:selected {
            background: #cfe0f4; color: #0f2438; border-radius: 8px;
        }
        .mini-action { border-radius: 8px; padding: 6px; }
        .mini-action:hover { background: #e4ebf3; }
        #terminal-notebook > header { background: #e9edf3; border-bottom: 1px solid #d5dce6; }
        #terminal-notebook > header tab { background: transparent; border: none; padding: 7px 14px; margin: 0 1px; color: #5a6674; }
        #terminal-notebook > header tab:checked { color: #0f2438; border-bottom: 2px solid #2f7fd6; }
        #terminal-notebook > header tab:hover { background: #dde4ee; }
        button { border-radius: 9px; }

        """

    def on_session_filter_changed(self, entry: Gtk.SearchEntry) -> None:
        self.session_filter_text = entry.get_text().strip().lower()
        self.refresh_sessions()

    @staticmethod
    def _escape(text: str) -> str:
        return GLib.markup_escape_text(text or "")

    def _session_matches_filter(self, node: Node, query: str) -> bool:
        if not query:
            return True
        if isinstance(node, FolderNode):
            hay = node.name.lower()
            return query in hay or any(self._session_matches_filter(child, query) for child in node.children)
        haystack = " ".join(
            [
                node.name,
                node.protocol,
                node.host,
                node.port,
                node.user,
                node.domain,
                node.command,
                node.notes,
                self._session_detail(node),
            ]
        ).lower()
        return query in haystack

    def _session_markup(self, sess: Session) -> str:
        name = self._escape(sess.name)
        lock = "  🔐" if sess.credential_id else ""
        return f"<span>{name}</span>{lock}"

    def _folder_markup(self, folder: FolderNode) -> str:
        return f"<b>{self._escape(folder.name)}</b>"

    def _folder_icon_pixbuf(self, color: str) -> Optional[GdkPixbuf.Pixbuf]:
        color = normalize_folder_color(color)
        cached = self._folder_pixbuf_cache.get(color)
        if cached is not None:
            return cached  # type: ignore[return-value]
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="20" height="17" viewBox="0 0 20 17">
<path d="M1.2 3.2c0-1 .8-1.8 1.8-1.8h4.7l1.7 2h7.6c1 0 1.8.8 1.8 1.8v8.7c0 1-.8 1.8-1.8 1.8H3c-1 0-1.8-.8-1.8-1.8z" fill="{color}"/>
<path d="M1.2 5.2h17.6v8.7c0 1-.8 1.8-1.8 1.8H3c-1 0-1.8-.8-1.8-1.8z" fill="{color}" opacity=".92"/>
<path d="M2 5.2h16" stroke="#ffffff" stroke-opacity=".28"/>
<path d="M1.2 3.2c0-1 .8-1.8 1.8-1.8h4.7l1.7 2h7.6c1 0 1.8.8 1.8 1.8v8.7c0 1-.8 1.8-1.8 1.8H3c-1 0-1.8-.8-1.8-1.8z" fill="none" stroke="#000000" stroke-opacity=".32"/>
</svg>"""
        try:
            loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
            loader.write(svg.encode("utf-8"))
            loader.close()
            pixbuf = loader.get_pixbuf()
            self._folder_pixbuf_cache[color] = pixbuf
            return pixbuf
        except Exception:
            return None

    def _home_session_icon_pixbuf(self) -> Optional[GdkPixbuf.Pixbuf]:
        cached = self._folder_pixbuf_cache.get("__home_session__")
        if cached is not None:
            return cached  # type: ignore[return-value]
        svg = """<svg xmlns="http://www.w3.org/2000/svg" width="22" height="20" viewBox="0 0 22 20">
<path d="M2 8.2 11 1.4l9 6.8v9.2c0 .8-.7 1.5-1.5 1.5H3.5C2.7 18.9 2 18.2 2 17.4z" fill="#4aa3df"/>
<path d="M5.5 9.2v7.2h11V9.2L11 5.1z" fill="#1b75bb" opacity=".92"/>
<path d="M8.2 18.9v-5.4c0-.6.5-1.1 1.1-1.1h3.4c.6 0 1.1.5 1.1 1.1v5.4" fill="#0f1e2b" opacity=".65"/>
<path d="M2 8.2 11 1.4l9 6.8" fill="none" stroke="#dff4ff" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="17.1" cy="4.5" r="2.2" fill="#77e2ff" stroke="#0f1e2b" stroke-width=".8"/>
</svg>"""
        try:
            loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
            loader.write(svg.encode("utf-8"))
            loader.close()
            pixbuf = loader.get_pixbuf()
            self._folder_pixbuf_cache["__home_session__"] = pixbuf
            return pixbuf
        except Exception:
            return None

    def _session_tree_icon_data(
        self,
        _column: Gtk.TreeViewColumn,
        renderer: Gtk.CellRendererPixbuf,
        model: Gtk.TreeModel,
        tree_iter: Gtk.TreeIter,
        _data: object = None,
    ) -> None:
        kind = str(model[tree_iter][self.COL_KIND])
        if kind == "folder":
            pixbuf = model[tree_iter][self.COL_FOLDER_PIXBUF]
            if pixbuf is not None:
                renderer.set_property("icon-name", None)
                renderer.set_property("pixbuf", pixbuf)
            else:
                renderer.set_property("pixbuf", None)
                renderer.set_property("icon-name", "folder-symbolic")
        else:
            renderer.set_property("pixbuf", None)
            renderer.set_property("icon-name", model[tree_iter][self.COL_ICON])
        self._apply_session_tree_cell_background(renderer, model, tree_iter)

    def _session_tree_text_data(
        self,
        _column: Gtk.TreeViewColumn,
        renderer: Gtk.CellRendererText,
        model: Gtk.TreeModel,
        tree_iter: Gtk.TreeIter,
        _data: object = None,
    ) -> None:
        self._apply_session_tree_cell_background(renderer, model, tree_iter)

    def _apply_session_tree_cell_background(
        self,
        renderer: Gtk.CellRenderer,
        model: Gtk.TreeModel,
        tree_iter: Gtk.TreeIter,
    ) -> None:
        """Apply DnD highlighting without painting every normal row black.

        Binding ``cell-background`` directly makes GTK set
        ``cell-background-set`` as a side effect on some themes.  The explicit
        callback always disables it for ordinary rows and only enables it for
        the temporary drag-and-drop highlight.
        """
        enabled = bool(model[tree_iter][self.COL_BG_SET])
        if enabled:
            renderer.set_property("cell-background", str(model[tree_iter][self.COL_BG]))
        renderer.set_property("cell-background-set", enabled)

    def _expanded_folders(self) -> set:
        """Le cartelle aperte nell'albero, come oggetti: sopravvivono a spostamenti e riordini."""
        expanded: set = set()

        def walk(parent: Optional[Gtk.TreeIter]) -> None:
            tree_iter = self.tree_store.iter_children(parent)
            while tree_iter is not None:
                if self.session_tree.row_expanded(self.tree_store.get_path(tree_iter)):
                    try:
                        expanded.add(id(self.store.get_node(self.tree_store[tree_iter][self.COL_PATH])))
                    except Exception:
                        pass
                    walk(tree_iter)
                tree_iter = self.tree_store.iter_next(tree_iter)

        walk(None)
        return expanded

    def refresh_sessions(self) -> None:
        # Prima di ricostruire l'albero si annota cosa era aperto: prima ogni modifica, aggiunta o
        # eliminazione richiudeva tutte le cartelle tranne Home Session.
        expanded = self._expanded_folders() if self.tree_store.iter_n_children(None) else set()
        self.tree_store.clear()
        query = self.session_filter_text
        visible_sessions = 0

        def add_nodes(nodes: List[Node], parent_iter: Optional[Gtk.TreeIter], prefix: str, force_show: bool = False) -> int:
            nonlocal visible_sessions
            added = 0
            for idx, node in enumerate(nodes):
                path = f"{prefix}.{idx}" if prefix else str(idx)
                if isinstance(node, FolderNode):
                    folder_name_matches = bool(query and query in node.name.lower())
                    if not force_show and not self._session_matches_filter(node, query):
                        continue
                    it = self.tree_store.append(parent_iter, [
                        self._folder_markup(node),
                        "Cartella",
                        path,
                        "folder",
                        "",
                        "",
                        3,
                        False,
                        self._home_session_icon_pixbuf() if node.name == HOME_SESSION_NAME and path == self.store.home_path() else self._folder_icon_pixbuf(node.color),
                    ])
                    child_added = add_nodes(node.children, it, path, force_show or folder_name_matches)
                    added += 1 + child_added
                else:
                    if not force_show and not self._session_matches_filter(node, query):
                        continue
                    self.tree_store.append(parent_iter, [
                        self._session_markup(node),
                        self._session_detail(node),
                        path,
                        "session",
                        self.protocol_icon(node.protocol),
                        "",
                        3,
                        False,
                        None,
                    ])
                    visible_sessions += 1
                    added += 1
            return added

        add_nodes(self.store.nodes, None, "")
        self.session_count_label.set_text(str(visible_sessions))
        try:
            if query:
                self.session_tree.expand_all()
            elif self.tree_store.iter_n_children(None) > 0:
                self.session_tree.expand_to_path(Gtk.TreePath.new_from_string("0"))

                def restore(parent: Optional[Gtk.TreeIter]) -> None:
                    tree_iter = self.tree_store.iter_children(parent)
                    while tree_iter is not None:
                        try:
                            node = self.store.get_node(self.tree_store[tree_iter][self.COL_PATH])
                        except Exception:
                            node = None
                        if node is not None and id(node) in expanded:
                            self.session_tree.expand_row(self.tree_store.get_path(tree_iter), False)
                            restore(tree_iter)
                        tree_iter = self.tree_store.iter_next(tree_iter)

                restore(None)
        except Exception:
            pass
        if hasattr(self, "session_action_buttons"):
            self.on_session_selection_changed(self.session_tree.get_selection())

    @staticmethod
    def _session_detail(sess: Session) -> str:
        if sess.protocol == "local":
            return "local shell"
        if sess.protocol == "command":
            return sess.command or "comando personalizzato"
        if sess.protocol == "serial":
            return f"SERIALE  {sess.host or '/dev/ttyUSB0'} @ {sess.port or '115200'} {sess.serial_databits}{sess.serial_parity}{sess.serial_stopbits}"
        target = sess.host or "host non impostato"
        if sess.user:
            if sess.protocol == "rdp" and sess.domain:
                target = f"{sess.domain}\\{sess.user}@{target}"
            else:
                target = f"{sess.user}@{target}"
        if sess.port:
            target = f"{target}:{sess.port}"
        cred = " 🔐" if sess.credential_id else ""
        return f"{sess.protocol.upper()}  {target}{cred}"

    def selected_path_and_node(self) -> Optional[Tuple[str, Node]]:
        selection = self.session_tree.get_selection()
        model, tree_iter = selection.get_selected()
        if tree_iter is None:
            return None
        path = model[tree_iter][self.COL_PATH]
        try:
            return path, self.store.get_node(path)
        except Exception:
            return None

    def selected_is_home_session(self) -> bool:
        selected = self.selected_path_and_node()
        return bool(selected is not None and self.store.is_home_path(selected[0]))

    def on_session_selection_changed(self, _selection: Gtk.TreeSelection) -> None:
        """Disabilita le azioni da cartella normale quando è selezionata Home Session."""
        selected = self.selected_path_and_node()
        has_selection = selected is not None
        home_selected = bool(has_selection and self.store.is_home_path(selected[0]))
        for label in ("Modifica", "Duplica", "Sposta", "Elimina"):
            button = getattr(self, "session_action_buttons", {}).get(label)
            if button is not None:
                button.set_sensitive(has_selection and not home_selected)

    def selected_target_folder_path(self) -> str:
        selected = self.selected_path_and_node()
        if selected is None:
            return self.store.home_path()
        path, node = selected
        if isinstance(node, FolderNode):
            return path
        parent = self.store.parent_path(path)
        return parent or self.store.home_path()

    def new_session_parent_path(self) -> str:
        """Dove finisce una sessione appena creata: in fondo all'elenco.

        Deliberatamente NON usa selected_target_folder_path(): quella
        restituisce la cartella evidenziata nell'albero in quel momento, per
        cui la destinazione dipendeva da dove si trovava il cursore quando si
        premeva "Nuova sessione" — una cartella a caso, dal punto di vista di
        chi la crea, e per giunta diversa a ogni volta.

        La destinazione e' il primo livello di "Home Session", non la radice
        vera ("") dello store: nel modello di FedTermHub la radice contiene
        sempre e solo Home Session, e save() la normalizza a ogni salvataggio
        riportando dentro Home tutto cio' che trova accanto. Puntare alla
        radice funzionava solo grazie a quell'effetto collaterale: qui la
        destinazione e' dichiarata. In fondo al primo livello di Home la sessione compare
        sotto tutte le cartelle, dove si vede subito e da dove l'utente la
        trascina dove vuole. Vale solo per le sessioni: "Nuova cartella" e
        "Importa qui" continuano a rispettare la selezione, perche' li' la
        destinazione e' il senso del comando.
        """
        return self.store.home_path()

    def ensure_vault_unlocked(self, create_if_missing: bool = False) -> bool:
        if self.vault.unlocked:
            return True
        if not CRYPTO_AVAILABLE:
            self.set_status("python3-cryptography non installato. Installa: sudo dnf install python3-cryptography")
            return False
        creating = not self.vault.exists()
        if creating and not create_if_missing:
            self.set_status("Password Manager non ancora creato.")
            return False
        dialog = MasterPasswordDialog(self, mode="create" if creating else "unlock")

        def open_vault() -> Optional[str]:
            try:
                if creating:
                    self.vault.create(dialog.get_password())
                else:
                    self.vault.unlock(dialog.get_password())
                return None
            except InvalidTag:
                return "Password errata. Riprova."
            except Exception as exc:
                return f"Impossibile {'creare' if creating else 'aprire'} l'archivio delle password: {exc}"

        opened = dialog.run_validated(open_vault)
        keep_unlocked = dialog.wants_keep_unlocked()
        dialog.destroy()
        if not opened:
            return False
        self.mark_user_activity()
        message = "Password Manager creato e sbloccato." if creating else "Password Manager sbloccato."
        if keep_unlocked and int(self.settings.vault_auto_lock_minutes) != 0:
            # Spunta nel prompt: disattiva il blocco automatico a tempo e rende la scelta
            # persistente (coerente con le Impostazioni).
            self.settings.vault_auto_lock_minutes = 0
            try:
                self.settings.save(SETTINGS_FILE)
            except Exception:
                pass
            message = message[:-1] + " (resterà sbloccato finché l'app è aperta)."
        self.set_status(message)
        GLib.idle_add(self.start_auto_tunnels)
        return True

    def on_password_manager(self, _button: Gtk.Button) -> None:
        if not self.ensure_vault_unlocked(create_if_missing=True):
            return
        dialog = VaultManagerDialog(self, self.vault)
        dialog.run()
        dialog.destroy()
        if self.vault.unlocked:
            self.set_status("Password Manager chiuso: resta sbloccato finché non lo blocchi o scade il tempo.")
        else:
            self.set_status("Password bloccate.")

    def on_lock_vault(self, _button: Gtk.Button) -> None:
        if not self.vault.unlocked:
            self.set_status("Le password sono già bloccate." if self.vault.exists() else "Password Manager non ancora creato.")
            return
        self.vault.lock()
        self.set_status("Password bloccate: la master password verrà richiesta al prossimo uso.")

    def mark_user_activity(self, *_args) -> bool:
        self._last_user_activity = time.monotonic()
        return False

    def on_user_activity(self, *_args) -> bool:
        return self.mark_user_activity()

    def _security_tick(self) -> bool:
        minutes = int(self.settings.vault_auto_lock_minutes)
        if minutes > 0 and self.vault.unlocked:
            inactive = time.monotonic() - self._last_user_activity
            if inactive >= minutes * 60:
                self.vault.lock()
                self.show_notification("Password bloccate automaticamente per inattività", "info")
                self.set_status("Password bloccate automaticamente.")
        return True

    def on_window_state_event(self, _window: Gtk.Window, event) -> bool:  # noqa: ANN001
        try:
            minimized = bool(event.new_window_state & Gdk.WindowState.ICONIFIED)
        except Exception:
            minimized = False
        if minimized and self.settings.lock_vault_on_minimize and self.vault.unlocked:
            self.vault.lock()
            self.set_status("Password bloccate alla minimizzazione della finestra.")
        return False

    def copy_sensitive_text(self, text: str, label: str = "Dato sensibile") -> None:
        if not text:
            self.set_status(f"{label} vuoto: nulla da copiare.")
            return
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(text, -1)
        clipboard.store()
        seconds = int(self.settings.clear_clipboard_seconds)
        self.set_status(f"{label} copiato negli appunti" + (f"; cancellazione tra {seconds} s." if seconds > 0 else "."))
        self.mark_user_activity()
        if seconds > 0:
            expected = text

            def clear_if_unchanged() -> bool:
                def received(cb: Gtk.Clipboard, current: Optional[str], _data=None) -> None:
                    if current == expected:
                        cb.set_text("", -1)
                        cb.store()
                        self.set_status("Dato sensibile rimosso dagli appunti.")
                try:
                    clipboard.request_text(received, None)
                except Exception:
                    pass
                return False

            GLib.timeout_add_seconds(seconds, clear_if_unchanged)

    @staticmethod
    def _folder_display(folder: str) -> str:
        parts = [part for part in folder.split(" / ") if part]
        if parts and parts[0] == HOME_SESSION_NAME:
            parts = parts[1:]
        return " / ".join(parts)

    def credential_usage(self, cred_id: str) -> List[Tuple[str, str, str]]:
        """Sessioni e tunnel che usano una credenziale: (nome, tipo, cartella)."""
        if not cred_id:
            return []
        found: List[Tuple[str, str, str]] = []
        for _path, sess, folder in self.store.iter_sessions():
            name = sess.name or sess.host
            if sess.credential_id == cred_id:
                kind = "Terminale" if sess.protocol == "local" else sess.protocol.upper()
                found.append((name, kind, self._folder_display(folder)))
            if getattr(sess, "rdp_gateway_credential_id", "") == cred_id:
                found.append((name, "Gateway", self._folder_display(folder)))
        for tunnel in getattr(self.tunnel_store, "tunnels", []):
            if tunnel.credential_id == cred_id:
                found.append((tunnel.name, "Tunnel", ""))
        return found

    def session_group_name(self, session: Session) -> str:
        """La cartella che contiene la sessione (di solito il cliente), vuota se sta nella radice."""
        for _path, sess, folder in self.store.iter_sessions():
            if sess is session:
                display = self._folder_display(folder)
                return display.split(" / ")[-1] if display else ""
        return ""

    def credential_group_suggestions(self) -> Dict[str, str]:
        """Per le credenziali senza gruppo: la cartella delle sessioni che le usano, se e' una sola."""
        folders: Dict[str, set] = {}
        for _path, sess, folder in self.store.iter_sessions():
            display = self._folder_display(folder)
            last = display.split(" / ")[-1] if display else ""
            for cred_id in (sess.credential_id, getattr(sess, "rdp_gateway_credential_id", "")):
                if cred_id:
                    folders.setdefault(cred_id, set()).add(last)
        suggestions: Dict[str, str] = {}
        for cred in self.vault.credentials:
            names = folders.get(cred.id, set())
            if not cred.group and len(names) == 1:
                name = next(iter(names))
                if name:
                    suggestions[cred.id] = name
        return suggestions

    def credential_options_for_dialog(self) -> List[Tuple[str, str]]:
        if not self.vault.unlocked:
            return []
        return self.vault.labels()

    def tunnel_options_for_dialog(self) -> List[Tuple[str, str]]:
        return [(tunnel.id, tunnel.name) for tunnel in self.tunnel_store.tunnels]


    def save_rdp_password_to_vault(self, sess: Session, password: str, ask: bool = True) -> bool:
        """Salva una password RDP nel vault cifrato e collega la credenziale alla sessione.

        La password non viene mai scritta nel file sessioni; nel file sessioni resta solo
        l'ID della credenziale cifrata nel vault.
        """
        if not password:
            return False
        if ask:
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Salvare la password nel Password Manager cifrato?",
            )
            dialog.format_secondary_text(
                "FedTermHub creerà una credenziale riutilizzabile anche su altre sessioni. "
                "Nel file sessioni non verrà salvata la password in chiaro."
            )
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.YES:
                return False

        if not self.ensure_vault_unlocked(create_if_missing=True):
            return False
        name_bits = [sess.name or f"RDP {sess.host}"]
        if sess.domain:
            name_bits.append(sess.domain)
        if sess.user:
            name_bits.append(sess.user)
        cred = Credential(
            name=" - ".join(name_bits),
            username=sess.user,
            domain=sess.domain,
            password=password,
            notes=f"Credenziale RDP creata da FedTermHub per {sess.host}",
            group=self.session_group_name(sess),
        )
        self.vault.credentials.append(cred)
        self.vault.save()
        sess.credential_id = cred.id
        sess.use_credential_password = True
        try:
            self.store.save()
            self.refresh_sessions()
        except Exception:
            pass
        self.set_status(f"Password salvata nel Password Manager e collegata a: {sess.name}")
        return True

    def maybe_save_dialog_password(self, dialog: SessionDialog, session: Session) -> None:
        try:
            if dialog.should_offer_save_password():
                self.save_rdp_password_to_vault(session, dialog.get_transient_password(), ask=True)
        except Exception as exc:
            self.report_session_error("Errore salvataggio password", str(exc), traceback.format_exc())

    def open_local_terminal(self) -> None:
        configured = self.settings.terminal_shell.strip()
        shell = configured if configured and Path(configured).exists() else (os.environ.get("SHELL") or "")
        if not shell or not Path(shell).exists():
            shell = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
        cwd = self.settings.terminal_start_directory.strip()
        if not cwd or not Path(cwd).is_dir():
            cwd = str(Path.home())
        self.add_terminal_tab("Locale", [shell], cwd=cwd, close_on_exit="always")

    def open_command_palette(self) -> None:
        """Apre la ricerca rapida delle sessioni (Ctrl+K)."""
        try:
            palette = CommandPalette(self)
        except Exception as exc:
            self.set_status(f"Command palette non disponibile: {exc}")
            return
        palette.show_all()
        palette.entry.grab_focus()

    def on_quick_connect(self, *_args) -> None:
        text = self.quick.get_text().strip()
        if not text:
            # Dal menu: porta al campo invece di non fare niente.
            if not self.sidebar.get_visible():
                self.toggle_sidebar(True)
            self.quick.grab_focus()
            self.set_status("Scrivi utente@host[:porta], rdp:host, vnc:host o serial:/dev/ttyUSB0@115200 e premi Invio.")
            return
        lower = text.lower()
        try:
            if lower.startswith("rdp:"):
                host, port = split_host_port(text.split(":", 1)[1].strip().lstrip("/"), "3389")
                sess = Session(name=f"RDP {host}", protocol="rdp", host=host, port=port)
            elif lower.startswith("vnc:"):
                host, port = split_host_port(text.split(":", 1)[1].strip().lstrip("/"), "")
                sess = Session(name=f"VNC {host}", protocol="vnc", host=host, port=port)
            elif lower.startswith("serial:"):
                rest = text.split(":", 1)[1].strip()
                device, baud = (rest.split("@", 1) + ["115200"])[:2] if "@" in rest else (rest, "115200")
                sess = Session(name=f"Seriale {device}", protocol="serial", host=device, port=baud)
            else:
                if lower.startswith("ssh://"):
                    text2 = text[6:]
                elif lower.startswith("ssh:"):
                    text2 = text[4:]
                else:
                    text2 = text
                user = ""
                address = text2
                if "@" in text2:
                    user, address = text2.rsplit("@", 1)
                host, port = split_host_port(address, "22")
                sess = Session(name=f"SSH {text2}", protocol="ssh", host=host, port=port, user=user)
            problem = session_problem(sess)
            if problem:
                self.set_status(f"Connessione rapida non valida: {problem}")
                return
            self.open_session(sess)
            self.quick.set_text("")
        except Exception as exc:
            self.set_status(f"Quick connect non valido: {exc}")

    def on_session_activated(self, _tree: Gtk.TreeView, tree_path: Gtk.TreePath, _column: Gtk.TreeViewColumn) -> None:
        tree_iter = self.tree_store.get_iter(tree_path)
        node_path = self.tree_store[tree_iter][self.COL_PATH]
        node = self.store.get_node(node_path)
        if isinstance(node, FolderNode):
            if self.session_tree.row_expanded(tree_path):
                self.session_tree.collapse_row(tree_path)
            else:
                self.session_tree.expand_row(tree_path, False)
            return
        self.open_session(node)

    def on_add_session(self, _button: Gtk.Button) -> None:
        dialog = SessionDialog(self, credentials=self.credential_options_for_dialog(), tunnels=self.tunnel_options_for_dialog())
        session: Optional[Session] = None
        save_password = False
        transient_password = ""
        if dialog.run() == Gtk.ResponseType.OK:
            session = dialog.get_session()
            save_password = dialog.should_offer_save_password()
            transient_password = dialog.get_transient_password()
        dialog.destroy()
        if session is not None:
            self._add_new_session(session, save_password, transient_password)

    def _add_new_session(self, session: Session, save_password: bool = False, transient_password: str = "") -> None:
        """Unico punto in cui una sessione nuova entra nell'albero (menu, tipo specifico, strumenti
        di rete): in fondo al primo livello, selezionata, con la password salvata dopo, quando la
        sessione e' gia' nell'albero e puo' darle il gruppo della sua cartella."""
        self.store.add_node(self.new_session_parent_path(), session)
        if save_password:
            self.save_rdp_password_to_vault(session, transient_password, ask=True)
        self.refresh_sessions()
        # Selezionare la riga nuova serve a far vedere dov'e' finita: in fondo all'albero puo' essere
        # fuori dalla parte visibile.
        self._select_store_path(self.store.find_node_path(session))
        self.set_status(
            f"Sessione aggiunta in fondo all'elenco: {session.name} — trascinala in una cartella se vuoi."
        )

    def on_add_folder(self, _button: Gtk.Button) -> None:
        parent_path = self.selected_target_folder_path()
        dialog = FolderDialog(self, title="Nuova cartella")
        if dialog.run() == Gtk.ResponseType.OK:
            name = dialog.get_name()
            self.store.add_node(parent_path, FolderNode(name=name, color=dialog.get_color()))
            self.refresh_sessions()
            self.set_status(f"Cartella creata in {self.folder_label(parent_path)}: {name}")
        dialog.destroy()

    def on_edit_selected(self, _button: Gtk.Button) -> None:
        selected = self.selected_path_and_node()
        if selected is None:
            self.set_status("Seleziona una sessione o cartella da modificare.")
            return
        path, node = selected
        if isinstance(node, FolderNode):
            if path == self.store.home_path():
                self.set_status("Home Session è la cartella principale e non può essere rinominata.")
                return
            dialog = FolderDialog(self, title="Modifica cartella", name=node.name, color=node.color)
            if dialog.run() == Gtk.ResponseType.OK:
                node.name = dialog.get_name()
                node.color = dialog.get_color()
                self.store.replace_node(path, node)
                self.refresh_sessions()
                self.set_status(f"Cartella aggiornata: {node.name}")
            dialog.destroy()
            return

        dialog = SessionDialog(self, node, credentials=self.credential_options_for_dialog(), tunnels=self.tunnel_options_for_dialog())
        updated: Optional[Session] = None
        save_password = False
        transient_password = ""
        if dialog.run() == Gtk.ResponseType.OK:
            updated = dialog.get_session()
            save_password = dialog.should_offer_save_password()
            transient_password = dialog.get_transient_password()
        dialog.destroy()
        if updated is not None:
            self.store.replace_node(path, updated)
            if save_password:
                self.save_rdp_password_to_vault(updated, transient_password, ask=True)
            self.refresh_sessions()
            self.set_status(f"Sessione aggiornata: {updated.name}")

    def on_duplicate_selected(self, _button: Optional[Gtk.Button]) -> None:
        selected = self.selected_path_and_node()
        if selected is None:
            self.set_status("Seleziona una sessione o cartella da duplicare.")
            return
        path, node = selected
        if path == self.store.home_path():
            self.set_status("Home Session non può essere duplicata: esportala o importa al suo interno.")
            return
        parent_path = self.store.parent_path(path)
        clone = node_from_dict(node_to_dict(node))
        clone.name = f"{clone.name} copia"
        # La copia va subito sotto l'originale, non in fondo alla cartella, ed e' selezionata.
        container = self.store.get_container(parent_path)
        container.insert(container.index(node) + 1, clone)
        self.store.save()
        self.refresh_sessions()
        self._select_store_path(self.store.find_node_path(clone))
        self.set_status(f"Duplicato: {clone.name}")


    def _session_export_payload_for_node(self, node: Node, scope: str) -> Dict[str, object]:
        return {
            "version": APP_VERSION,
            "format": "FedTermHub session package",
            "scope": scope,
            "root": node_to_dict(node),
        }

    def _choose_save_json(self, title: str, default_name: str, parent: Optional[Gtk.Window] = None) -> Optional[Path]:
        dialog = Gtk.FileChooserDialog(title=title, transient_for=parent or self, action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons("_Annulla", Gtk.ResponseType.CANCEL, "_Salva", Gtk.ResponseType.OK)
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name(default_name)
        apply_window_icon(dialog)
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                return Path(dialog.get_filename())
            return None
        finally:
            dialog.destroy()

    def _choose_open_json(self, title: str, parent: Optional[Gtk.Window] = None) -> Optional[Path]:
        dialog = Gtk.FileChooserDialog(title=title, transient_for=parent or self, action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons("_Annulla", Gtk.ResponseType.CANCEL, "_Apri", Gtk.ResponseType.OK)
        apply_window_icon(dialog)
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                return Path(dialog.get_filename())
            return None
        finally:
            dialog.destroy()

    def _node_from_session_package(self, src: Path) -> Node:
        raw = json.loads(src.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("root"), dict):
            return node_from_dict(raw["root"])  # type: ignore[arg-type]
        items = raw.get("items", raw) if isinstance(raw, dict) else raw
        if isinstance(items, list):
            imported = [node_from_dict(x) for x in items if isinstance(x, dict)]
            return FolderNode(name=f"Import {src.stem}", children=imported)
        raise ValueError("Il file non contiene una Home Session, cartella o sessione valida.")

    def export_home_session(self, *_args) -> None:
        try:
            home = self.store.home_node()
            dest = self._choose_save_json("Esporta Home Session", "fedtermhub-home-session.json")
            if dest is None:
                return
            atomic_write_text(dest, json.dumps(self._session_export_payload_for_node(home, "home-session"), indent=2, ensure_ascii=False), mode=0o600)
            self.set_status(f"Home Session esportata in {dest.name}")
        except Exception as exc:
            self.set_status(f"Errore export Home Session: {exc}")

    def import_home_session(self, *_args) -> None:
        try:
            src = self._choose_open_json("Importa Home Session")
            if src is None:
                return
            node = self._node_from_session_package(src)
            if not isinstance(node, FolderNode):
                node = FolderNode(name=HOME_SESSION_NAME, color=HOME_SESSION_COLOR, children=[node])
            if node.name != HOME_SESSION_NAME:
                node = FolderNode(name=HOME_SESSION_NAME, color=HOME_SESSION_COLOR, children=[node])
            confirm = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.OK_CANCEL,
                text="Sostituire l'attuale Home Session?",
            )
            apply_window_icon(confirm)
            confirm.format_secondary_text("Le sessioni attuali verranno sostituite dalla Home Session importata. Le password salvate non vengono modificate.")
            response = confirm.run()
            confirm.destroy()
            if response != Gtk.ResponseType.OK:
                return
            node.name = HOME_SESSION_NAME
            node.color = HOME_SESSION_COLOR
            self.store.nodes = [node]
            self.store.save()
            self.refresh_sessions()
            self.session_tree.expand_all()
            self.set_status(f"Home Session importata da {src.name}")
        except Exception as exc:
            self.set_status(f"Errore import Home Session: {exc}")

    def export_selected_node(self, *_args) -> None:
        try:
            selected = self.selected_path_and_node()
            if selected is None:
                node = self.store.home_node()
                scope = "home-session"
                default_name = "fedtermhub-home-session.json"
            else:
                path, node = selected
                scope = "folder" if isinstance(node, FolderNode) else "session"
                safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", node.name).strip("-") or scope
                default_name = f"fedtermhub-{safe_name}.json"
            dest = self._choose_save_json("Esporta cartella o sessione", default_name)
            if dest is None:
                return
            atomic_write_text(dest, json.dumps(self._session_export_payload_for_node(node, scope), indent=2, ensure_ascii=False), mode=0o600)
            self.set_status(f"Esportato: {node.name} → {dest.name}")
        except Exception as exc:
            self.set_status(f"Errore export selezione: {exc}")

    def import_into_selected_folder(self, *_args) -> None:
        try:
            target = self.selected_target_folder_path()
            src = self._choose_open_json("Importa in cartella selezionata")
            if src is None:
                return
            node = self._node_from_session_package(src)
            # Se il pacchetto contiene Home Session, importiamo il suo contenuto nella cartella scelta
            # invece di creare una Home Session annidata dentro Home Session.
            if isinstance(node, FolderNode) and node.name == HOME_SESSION_NAME:
                imported = FolderNode(name=f"Import {src.stem}", color=node.color, children=node.children)
                node = imported
            self.store.add_node(target, node)
            self.refresh_sessions()
            self.session_tree.expand_all()
            self.set_status(f"Importato '{node.name}' in {self.folder_label(target)}")
        except Exception as exc:
            self.set_status(f"Errore import in cartella: {exc}")

    def _ask_export_password(
        self,
        parent: Optional[Gtk.Window] = None,
        exporting: bool = True,
        check: Optional[Callable[[str], Optional[str]]] = None,
    ) -> Optional[str]:
        dialog = MasterPasswordDialog(parent or self, mode="export" if exporting else "import")
        try:
            ok = dialog.run_validated((lambda: check(dialog.get_password())) if check else None)
            return dialog.get_password() if ok else None
        finally:
            dialog.destroy()

    def export_vault_encrypted(self, parent: Optional[Gtk.Window] = None, *_args) -> None:
        if not isinstance(parent, Gtk.Window):
            parent = None
        if not self.ensure_vault_unlocked(create_if_missing=False):
            return
        try:
            dest = self._choose_save_json("Esporta backup cifrato delle password", "fedtermhub-password-backup.json", parent)
            if dest is None:
                return
            password = self._ask_export_password(parent, exporting=True)
            if password is None:
                return
            payload = PasswordVault.encrypt_credentials_payload(self.vault.credentials, password)
            atomic_write_text(dest, json.dumps(payload, indent=2, ensure_ascii=False), mode=0o600)
            self.set_status(f"Backup cifrato di {len(self.vault.credentials)} credenziali salvato in {dest.name}")
        except Exception as exc:
            self.set_status(f"Errore export credenziali: {exc}")

    def import_vault_encrypted(self, parent: Optional[Gtk.Window] = None, *_args) -> None:
        if not isinstance(parent, Gtk.Window):
            parent = None
        if not self.ensure_vault_unlocked(create_if_missing=True):
            return
        try:
            src = self._choose_open_json("Importa backup cifrato delle password", parent)
            if src is None:
                return
            raw = json.loads(src.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("File credenziali non valido.")
            imported: List[Credential] = []

            def decrypt(password: str) -> Optional[str]:
                try:
                    imported[:] = PasswordVault.decrypt_credentials_payload(raw, password)
                    return None
                except InvalidTag:
                    return "Password errata per questo backup. Riprova."
                except Exception as exc:
                    return f"Il file non è un backup valido: {exc}"

            if self._ask_export_password(parent, exporting=False, check=decrypt) is None:
                return
            confirm = Gtk.MessageDialog(
                transient_for=parent or self,
                flags=0,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.NONE,
                text=f"Importare {len(imported)} credenziali?",
            )
            apply_window_icon(confirm)
            confirm.format_secondary_text(
                "«Aggiungi» le unisce a quelle che hai già. «Sostituisci» cancella le credenziali attuali "
                "e tiene solo quelle del backup."
            )
            confirm.add_buttons("_Annulla", Gtk.ResponseType.CANCEL, "_Sostituisci", Gtk.ResponseType.YES)
            merge = confirm.add_button("A_ggiungi", Gtk.ResponseType.OK)
            merge.get_style_context().add_class("suggested-action")
            confirm.set_default_response(Gtk.ResponseType.OK)
            response = confirm.run()
            confirm.destroy()
            if response not in (Gtk.ResponseType.OK, Gtk.ResponseType.YES):
                return
            if response == Gtk.ResponseType.YES:
                self.vault.credentials = imported
            else:
                existing_ids = {cred.id for cred in self.vault.credentials}
                existing_names = {cred.name for cred in self.vault.credentials}
                for cred in imported:
                    if cred.id in existing_ids:
                        cred.id = uuid.uuid4().hex
                    if cred.name in existing_names:
                        cred.name = f"{cred.name} (importata)"
                    self.vault.credentials.append(cred)
                    existing_ids.add(cred.id)
                    existing_names.add(cred.name)
            self.vault.save()
            self.set_status(f"Importate {len(imported)} credenziali da {src.name}")
        except Exception as exc:
            self.set_status(f"Errore import credenziali: {exc}")

    def export_settings(self, parent: Optional[Gtk.Window] = None) -> None:
        dialog = Gtk.FileChooserDialog(
            title="Esporta impostazioni FedTermHub",
            transient_for=parent or self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_buttons("_Annulla", Gtk.ResponseType.CANCEL, "_Salva", Gtk.ResponseType.OK)
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name("fedtermhub-settings.json")
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                dest = Path(dialog.get_filename())
                atomic_write_text(dest, json.dumps(self.settings.to_dict(), indent=2, ensure_ascii=False), mode=0o600)
                self.set_status(f"Impostazioni esportate in {dest}")
        except Exception as exc:
            self.set_status(f"Errore esportazione impostazioni: {exc}")
        finally:
            dialog.destroy()

    def import_settings(self, parent: Optional[Gtk.Window] = None) -> bool:
        dialog = Gtk.FileChooserDialog(
            title="Importa impostazioni FedTermHub",
            transient_for=parent or self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons("_Annulla", Gtk.ResponseType.CANCEL, "_Apri", Gtk.ResponseType.OK)
        imported = False
        try:
            if dialog.run() != Gtk.ResponseType.OK:
                return False
            src = Path(dialog.get_filename())
            raw = json.loads(src.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not ({"theme", "icon_theme", "terminal_font_family"} & set(raw.keys())):
                raise ValueError("Il file non contiene impostazioni FedTermHub riconoscibili.")
            candidate = AppSettings.load(src)
            self.settings = candidate
            self.settings.save(SETTINGS_FILE)
            self.set_theme_from_menu(self.settings.theme)
            self.refresh_icon_theme_runtime()
            self.apply_layout_preferences()
            self.apply_terminal_preferences()
            self.set_status(f"Impostazioni importate da {src.name}")
            imported = True
        except Exception as exc:
            self.set_status(f"Errore importazione impostazioni: {exc}")
        finally:
            dialog.destroy()
        return imported

    def on_delete_selected(self, _button: Gtk.Button) -> None:
        selected = self.selected_path_and_node()
        if selected is None:
            self.set_status("Seleziona una sessione o cartella da eliminare.")
            return
        path, node = selected
        if path == self.store.home_path():
            self.set_status("Home Session non può essere eliminata.")
            return
        name = node.name
        if isinstance(node, FolderNode) and node.children:
            sessions, folders = self._folder_recursive_stats(node)
            parts = []
            if sessions:
                parts.append("1 sessione" if sessions == 1 else f"{sessions} sessioni")
            if folders:
                parts.append("1 sottocartella" if folders == 1 else f"{folders} sottocartelle")
            message = f"Eliminare la cartella «{name}» con {' e '.join(parts)}?"
        else:
            message = f"Eliminare «{name}»?"
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=message,
        )
        dialog.format_secondary_text("L'operazione modifica solo il file sessioni locale di FedTermHub.")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        self.store.remove_node(path)
        self.refresh_sessions()
        self.set_status(f"Eliminato: {name}")

    def on_move_selected(self, _button: Gtk.Button) -> None:
        selected = self.selected_path_and_node()
        if selected is None:
            self.set_status("Seleziona una sessione o cartella da spostare.")
            return
        path, node = selected
        if path == self.store.home_path():
            self.set_status("Home Session non può essere spostata.")
            return
        current_parent = self.store.parent_path(path) or self.store.home_path()
        folders: List[Tuple[str, str]] = [(self.store.home_path(), HOME_SESSION_NAME)]
        for folder_path, _folder, label in self.store.iter_folders():
            if isinstance(node, FolderNode) and (folder_path == path or folder_path.startswith(path + ".")):
                continue
            folders.append((folder_path, label))

        dialog = MoveDialog(self, folders, current_parent_path=current_parent)
        if dialog.run() != Gtk.ResponseType.OK:
            dialog.destroy()
            return
        target_path = dialog.get_folder_path()
        dialog.destroy()

        try:
            new_path = self.store.move_node(path, target_path)
            self.refresh_sessions()
            self._select_store_path(new_path)
            self.set_status(f"Spostato '{node.name}' in {self.folder_label(target_path)}")
        except Exception as exc:
            self.set_status(f"Errore spostamento: {exc}")

    def folder_label(self, folder_path: str) -> str:
        if folder_path == "":
            return HOME_SESSION_NAME
        node = self.store.get_node(folder_path)
        if isinstance(node, FolderNode):
            return node.name
        return HOME_SESSION_NAME

    def get_credential_for_session(self, sess: Session) -> Optional[Credential]:
        if not sess.credential_id:
            return None
        if not self.vault.unlocked and not self.ensure_vault_unlocked(create_if_missing=False):
            return None
        return self.vault.get(sess.credential_id)

    def effective_identity(self, sess: Session) -> Tuple[str, str, Optional[str]]:
        cred = self.get_credential_for_session(sess)
        user = sess.user
        domain = sess.domain
        password: Optional[str] = None
        if cred:
            user = cred.username or user
            domain = cred.domain or domain
            password = cred.password if sess.use_credential_password else None
        return user, domain, password

    @staticmethod
    def mask_args(args: List[str]) -> str:
        masked = []
        for item in args:
            if item.startswith("/p:"):
                masked.append("/p:********")
            elif item.startswith("/gateway:") and ",p:" in item:
                prefix, password_part = item.split(",p:", 1)
                suffix = ""
                if "," in password_part:
                    _password, rest = password_part.split(",", 1)
                    suffix = "," + rest
                masked.append(prefix + ",p:********" + suffix)
            else:
                masked.append(item)
        return shlex.join(masked)

    @staticmethod
    def proxy_jump_spec(sess: Session) -> str:
        if getattr(sess, "routing_mode", "direct") != "proxy_jump":
            return ""
        host = str(getattr(sess, "jump_host", "") or "").strip()
        if not host:
            return ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        user = str(getattr(sess, "jump_user", "") or "").strip()
        port = str(getattr(sess, "jump_port", "22") or "22").strip()
        endpoint = f"{user}@{host}" if user else host
        if port and port != "22":
            endpoint += f":{port}"
        return endpoint

    def gateway_identity(
        self, sess: Session, target_user: str, target_domain: str, target_password: Optional[str]
    ) -> Tuple[str, str, Optional[str]]:
        if getattr(sess, "rdp_gateway_use_same_credentials", True):
            return target_user, target_domain, target_password
        user = str(getattr(sess, "rdp_gateway_user", "") or "").strip()
        domain = str(getattr(sess, "rdp_gateway_domain", "") or "").strip()
        password: Optional[str] = None
        credential_id = str(getattr(sess, "rdp_gateway_credential_id", "") or "")
        if credential_id:
            if not self.vault.unlocked and not self.ensure_vault_unlocked(create_if_missing=False):
                return user, domain, None
            credential = self.vault.get(credential_id)
            if credential is not None:
                user = credential.username or user
                domain = credential.domain or domain
                password = credential.password
        return user, domain, password

    def open_session(self, sess: Session) -> None:
        tunnel_id = getattr(sess, "required_tunnel_id", "")
        routing_mode = getattr(sess, "routing_mode", "direct") or ("saved_tunnel" if tunnel_id else "direct")
        if tunnel_id and routing_mode == "saved_tunnel":
            tunnel = self.tunnel_store.get(tunnel_id)
            if tunnel is None:
                self.report_session_error(
                    "Tunnel richiesto non trovato",
                    f"La sessione “{sess.name}” è collegata a un tunnel che non esiste più.",
                    f"ID tunnel: {tunnel_id}",
                )
                return
            state = self.get_tunnel_state(tunnel.id)
            if state != "running":
                self.start_tunnel(tunnel)
                self.show_notification(f"Avvio tunnel “{tunnel.name}”…", "info", 2500)
                if getattr(sess, "wait_for_tunnel", True):
                    deadline = time.monotonic() + max(8, self.settings.connection_timeout_seconds + 4)

                    def wait_ready() -> bool:
                        current = self.get_tunnel_state(tunnel.id)
                        if current == "running":
                            self.show_notification(f"Tunnel pronto: {tunnel.name}", "success", 2500)
                            self._open_session_now(sess)
                            return False
                        if current in {"error", "locked"}:
                            self.report_session_error(
                                "Impossibile avviare il tunnel",
                                f"La sessione “{sess.name}” non è stata aperta perché il tunnel “{tunnel.name}” non è pronto.",
                                self.tunnel_log_text(tunnel),
                            )
                            return False
                        if time.monotonic() >= deadline:
                            self.report_session_error(
                                "Timeout avvio tunnel",
                                f"Il tunnel “{tunnel.name}” non è diventato operativo entro il tempo previsto.",
                                self.tunnel_log_text(tunnel),
                            )
                            return False
                        return True

                    GLib.timeout_add(250, wait_ready)
                    return
        self._open_session_now(sess)

    def _open_session_now(self, sess: Session) -> None:
        proto = sess.protocol.lower()
        problem = session_problem(sess)
        if problem:
            self.report_session_error(
                "Sessione non valida",
                f"«{sess.name}»: {problem}. Correggila con Modifica sessione.",
                f"Protocollo: {proto}",
            )
            return
        try:
            if proto == "local":
                self.open_local_terminal()
            elif proto == "ssh":
                self.open_ssh(sess)
            elif proto == "command":
                self.open_command(sess)
            elif proto == "rdp":
                self.launch_rdp(sess)
            elif proto == "vnc":
                self.launch_vnc(sess)
            elif proto == "sftp":
                self.launch_sftp(sess)
            elif proto == "serial":
                self.open_serial(sess)
            else:
                self.set_status(f"Protocollo non gestito: {proto}")
        except Exception as exc:
            self.report_session_error("Errore apertura sessione", str(exc), traceback.format_exc())

    def ssh_common_options(self) -> List[str]:
        """Opzioni OpenSSH comuni a SSH, SFTP e tunnel."""
        options: List[str] = ["-o", f"ConnectTimeout={self.settings.connection_timeout_seconds}"]
        if self.settings.ssh_keepalive_interval > 0:
            options += [
                "-o", f"ServerAliveInterval={self.settings.ssh_keepalive_interval}",
                "-o", f"ServerAliveCountMax={self.settings.ssh_keepalive_count}",
            ]
        strict = self.settings.ssh_strict_host_key
        if strict in {"accept-new", "yes", "no"}:
            options += ["-o", f"StrictHostKeyChecking={strict}"]
        if self.settings.ssh_compression:
            options.append("-C")
        return options

    def ssh_session_options(self) -> List[str]:
        """Opzioni SSH/SFTP interattive con multiplexing locale sicuro.

        Il socket condiviso permette al browser File remoti di riutilizzare una
        sessione SSH già autenticata, senza chiedere nuovamente la password.
        """
        options = self.ssh_common_options()
        runtime_root = Path(os.environ.get("XDG_RUNTIME_DIR", str(CONFIG_DIR)))
        control_dir = runtime_root / "fedtermhub" / "ssh-control"
        try:
            control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(control_dir, 0o700)
            control_path = str(control_dir / "%C")
            options += [
                "-o", "ControlMaster=auto",
                "-o", "ControlPersist=600",
                "-o", f"ControlPath={control_path}",
            ]
        except OSError:
            # Il browser continua a funzionare con chiavi, agent o vault anche
            # quando il runtime directory non permette socket condivisi.
            pass
        return options

    def open_ssh(self, sess: Session) -> None:
        if not sess.host:
            self.set_status("Host SSH mancante.")
            return
        user, _domain, password = self.effective_identity(sess)
        env: Dict[str, str] = {}
        ssh_binary = "ssh"
        argv = [ssh_binary] + self.ssh_session_options()
        jump_spec = self.proxy_jump_spec(sess)
        if getattr(sess, "routing_mode", "direct") == "proxy_jump" and not jump_spec:
            self.report_session_error(
                "Jump host incompleto",
                "Compila il server jump host nella sezione Instradamento avanzato.",
                f"Sessione: {sess.name}",
            )
            return
        if jump_spec:
            argv += ["-J", jump_spec]
        if password:
            sshpass = shutil.which("sshpass")
            if sshpass:
                argv = [sshpass, "-e"] + argv
                env["SSHPASS"] = password
            else:
                self.set_status("Credenziale collegata, ma sshpass non è installato. Installa: sudo dnf install sshpass")
        if sess.port:
            argv += ["-p", sess.port]
        target = sess.host
        if user:
            target = f"{user}@{sess.host}"
        # "--": un host o un utente che comincia con "-" (per esempio da un file di sessioni
        # importato) non deve diventare un'opzione di ssh come -oProxyCommand=...
        argv += ["--", target]
        if sess.command:
            argv.append(sess.command)
        self.add_terminal_tab(sess.name or target, argv, extra_env=env, session=sess, close_on_exit="on_success")

    def open_command(self, sess: Session) -> None:
        if not sess.command:
            self.set_status("Comando mancante.")
            return
        configured = self.settings.terminal_shell.strip()
        shell = configured if configured and Path(configured).exists() else (os.environ.get("SHELL") or "/bin/sh")
        self.add_terminal_tab(sess.name or "Comando", [shell, "-lc", sess.command])

    def open_serial(self, sess: Session) -> None:
        device = sess.host or "/dev/ttyUSB0"
        baud = sess.port or "115200"
        bridge = Path(__file__).with_name("serial_bridge.py")
        argv = [
            sys.executable or "python3",
            str(bridge),
            "--device",
            device,
            "--baud",
            baud,
            "--bytesize",
            sess.serial_databits or "8",
            "--parity",
            (sess.serial_parity or "N").upper(),
            "--stopbits",
            sess.serial_stopbits or "1",
        ]
        # Il ponte seriale non accetta argomenti in piu': un "comando" arrivato da un file importato
        # lo faceva uscire subito ("unrecognized arguments") e la scheda si chiudeva.
        self.add_terminal_tab(sess.name or f"Seriale {device}", argv, session=sess, close_on_exit="on_success")

    def launch_rdp(self, sess: Session) -> None:
        if not sess.host:
            self.report_session_error(
                "Host RDP mancante",
                "Apri Modifica sessione e compila il campo PC / IP.",
                f"Sessione: {sess.name}",
            )
            return

        user, domain, password = self.effective_identity(sess)
        if getattr(sess, "routing_mode", "direct") == "rd_gateway":
            if sess.rdp_mode in {"legacy", "external-legacy"}:
                self.report_session_error(
                    "RD Gateway richiede il backend nativo",
                    "Imposta la modalità RDP nativa nella scheda Schermo.",
                    "Il backend nativo libfreerdp è compatibile con Wayland e X11.",
                    diagnostic_session=sess,
                )
                return
            if not str(getattr(sess, "rdp_gateway_host", "") or "").strip():
                self.report_session_error(
                    "RD Gateway incompleto",
                    "Compila il server RD Gateway nella sezione Instradamento avanzato.",
                    f"Sessione: {sess.name}",
                )
                return
            if not getattr(sess, "rdp_gateway_use_same_credentials", True):
                _gu, _gd, gateway_password = self.gateway_identity(sess, user, domain, password)
                if not gateway_password:
                    self.report_session_error(
                        "Credenziale RD Gateway mancante",
                        "Seleziona una voce del Password Manager per il gateway oppure usa le stesse credenziali RDP.",
                        f"Gateway: {sess.rdp_gateway_host}",
                    )
                    return
        if not password:
            prompt = RdpPasswordDialog(self, sess)
            try:
                response = prompt.run()
                if response != Gtk.ResponseType.OK:
                    self.set_status("Connessione RDP annullata.")
                    return
                entered_user, entered_domain, entered_password, save_it = prompt.get_values()
            finally:
                prompt.destroy()
            if entered_user:
                user = entered_user
                sess.user = entered_user
            if entered_domain:
                domain = entered_domain
                sess.domain = entered_domain
            password = entered_password or None
            if save_it and password:
                self.save_rdp_password_to_vault(sess, password, ask=False)

        if not user:
            self.report_session_error(
                "Utente RDP mancante",
                "Inserisci l'utente Windows nella sessione o nella finestra credenziali.",
                "Esempi: mario.rossi, DOMINIO\\mario.rossi oppure mario.rossi@dominio.local",
            )
            return

        if self.settings.rdp_preflight:
            using_gateway = getattr(sess, "routing_mode", "direct") == "rd_gateway"
            probe_host = sess.rdp_gateway_host if using_gateway else sess.host
            probe_port_text = sess.rdp_gateway_port if using_gateway else (sess.port or "3389")
            probe_port = int(probe_port_text) if str(probe_port_text).isdigit() else (443 if using_gateway else 3389)
            if using_gateway and not probe_host:
                self.report_session_error(
                    "RD Gateway incompleto",
                    "Compila il server RD Gateway nella sezione Instradamento avanzato.",
                    f"Sessione: {sess.name}",
                )
                return
            self.show_notification(f"Verifica {probe_host}:{probe_port} in corso…", "info", 0)

            def worker() -> None:
                try:
                    started = time.monotonic()
                    with net_socket.create_connection((probe_host, probe_port), timeout=float(self.settings.connection_timeout_seconds)):
                        pass
                    elapsed = int((time.monotonic() - started) * 1000)
                    GLib.idle_add(self._launch_rdp_ready, sess, user, domain, password, elapsed)
                except Exception as exc:
                    target_label = "RD Gateway" if using_gateway else "Host RDP"
                    hint = (
                        "Controlla DNS, HTTPS 443, certificato e policy del gateway."
                        if using_gateway
                        else "Controlla VPN, routing, firewall e Desktop remoto sul PC Windows."
                    )
                    GLib.idle_add(
                        self.report_session_error,
                        f"{target_label} non raggiungibile",
                        f"Non riesco a collegarmi a {probe_host}:{probe_port}.",
                        f"Errore rete: {exc}\n{hint}",
                        "",
                        sess,
                    )

            threading.Thread(target=worker, daemon=True).start()
            return

        self._launch_rdp_ready(sess, user, domain, password, -1)

    def _build_native_rdp_options(self, sess: Session, user: str, domain: str, password: Optional[str] = None) -> List[str]:
        """Build libfreerdp options without X11/window-specific arguments."""
        host = sess.host if not sess.port else f"{host_for_uri(sess.host)}:{sess.port}"
        options: List[str] = [f"/v:{host}"]
        if sess.rdp_dynamic_resolution:
            options.append("/dynamic-resolution")
        if sess.rdp_width.isdigit() and sess.rdp_height.isdigit():
            options.append(f"/size:{sess.rdp_width}x{sess.rdp_height}")
        if sess.rdp_scale in {"100", "140", "180"}:
            options.append(f"/scale:{sess.rdp_scale}")
        if sess.rdp_cert_ignore:
            options.append("/cert:ignore")
        if sess.rdp_clipboard:
            options.append("/clipboard")
        if sess.rdp_fonts:
            options.append("+fonts")
        if sess.rdp_auto_reconnect:
            options.append("+auto-reconnect")
        if sess.rdp_sound:
            options.append("/sound")
        if sess.rdp_microphone:
            options.append("/microphone")
        if sess.rdp_printer:
            options.append("/printer")
        if sess.rdp_smartcard:
            options.append("/smartcard")
        if sess.rdp_admin:
            options.append("+admin")
        if sess.rdp_drive:
            options.append(f"/drive:fedtermhub,{normalize_rdp_drive_path(sess.rdp_drive)}")
        if sess.rdp_compression:
            options.append("+compression")

        layout_map = {
            "it": "0x00000410", "us": "0x00000409", "de": "0x00000407",
            "fr": "0x0000040C", "es": "0x0000040A",
        }
        layout = (sess.rdp_keyboard_layout or "auto").strip()
        if layout != "auto":
            options.append(f"/kbd:layout:{layout_map.get(layout, layout)}")
        if sess.rdp_timeout.isdigit():
            options.append(f"/timeout:{sess.rdp_timeout}")
        if sess.rdp_security in {"nla", "tls", "rdp"}:
            options.append(f"/sec:{sess.rdp_security}")
        quality = sess.rdp_quality if sess.rdp_quality in {"lan", "wan", "low"} else "lan"
        options.append({"wan": "/network:wan", "low": "/network:broadband-low"}.get(quality, "/network:lan"))
        if getattr(sess, "routing_mode", "direct") == "rd_gateway":
            gateway_host = str(getattr(sess, "rdp_gateway_host", "") or "").strip()
            gateway_port = str(getattr(sess, "rdp_gateway_port", "443") or "443").strip()
            if not gateway_host:
                raise ValueError("Server RD Gateway mancante")
            gateway_user, gateway_domain, gateway_password = self.gateway_identity(
                sess, user, domain, password
            )
            # L'opzione /gateway: usa la virgola come separatore di sotto-opzioni
            # e la config nativa usa il newline come separatore di riga: questi
            # caratteri nelle credenziali romperebbero il parsing o permetterebbero
            # di iniettare sotto-opzioni arbitrarie.
            for field_name, value in (
                ("utente", gateway_user),
                ("dominio", gateway_domain),
                ("password", gateway_password),
            ):
                if value and any(ch in value for ch in (",", "\n", "\r")):
                    raise ValueError(
                        f"La {field_name} del RD Gateway contiene caratteri non supportati (virgola o fine riga)."
                    )
            gateway_endpoint = gateway_host
            if gateway_port and gateway_port != "443":
                gateway_endpoint += f":{gateway_port}"
            parts = [f"g:{gateway_endpoint}"]
            if gateway_user:
                parts.append(f"u:{gateway_user}")
            if gateway_domain:
                parts.append(f"d:{gateway_domain}")
            if gateway_password:
                parts.append(f"p:{gateway_password}")
            parts.append("usage-method:direct")
            transport = str(getattr(sess, "rdp_gateway_transport", "auto") or "auto").lower()
            if transport not in {"auto", "http", "rpc"}:
                transport = "auto"
            parts.append(f"type:{transport}")
            options.append("/gateway:" + ",".join(parts))
        if user:
            options.append(f"/u:{user}")
        if domain:
            options.append(f"/d:{domain}")
        if sess.command:
            options.extend(shlex.split(sess.command))
        return options

    def open_windows_session_manager(
        self, sess: Session, user: str, domain: str, password: Optional[str]
    ) -> None:
        if WindowsSessionsDialog is None:
            self.report_session_error(
                "Gestione sessioni Windows non disponibile",
                "Il modulo grafico WinRM non è stato caricato.",
                f"{WINDOWS_SESSIONS_IMPORT_ERROR or 'Installa python3-winrm'}\n"
                "Su Fedora: sudo dnf install python3-winrm",
            )
            return
        try:
            dialog = WindowsSessionsDialog(
                self, sess.host, user, domain, password, status_callback=self.set_status
            )
            dialogs = getattr(self, "_windows_session_dialogs", None)
            if dialogs is None:
                dialogs = []
                self._windows_session_dialogs = dialogs
            dialogs.append(dialog)
            dialog.connect(
                "destroy",
                lambda item: dialogs.remove(item) if item in dialogs else None,
            )
            dialog.show()
            dialog.present()
        except Exception as exc:
            self.report_session_error(
                "Impossibile aprire Gestione sessioni Windows",
                str(exc),
                "La gestione richiede WinRM configurato sul server e un account con permessi adeguati.",
            )

    def _external_rdp_options(
        self,
        sess: Session,
        user: str,
        domain: str,
        password: Optional[str],
    ) -> List[str]:
        """Opzioni per il client FreeRDP ufficiale in finestra indipendente.

        La modalità indipendente evita completamente il framebuffer GTK/Python:
        decodifica, compositing e presentazione restano nel client xfreerdp.
        """
        options = self._build_native_rdp_options(sess, user, domain, password)
        # Il client esterno può usare la pipeline grafica completa, mentre il
        # helper integrato usa intenzionalmente GDI software.
        if getattr(sess, "rdp_gfx", True) and not any(item.startswith("/gfx") for item in options):
            options.append("/gfx")
        # FreeRDP 3.x non espone più +async-input e, nelle build Fedora
        # senza WITH_FREERDP_DEPRECATED_COMMANDLINE, rifiuta +bitmap-cache e
        # +offscreen-cache. Usare esclusivamente le opzioni moderne documentate.
        if "+async-update" not in options:
            options.append("+async-update")
        if not any(item.startswith("/cache:") for item in options):
            options.append("/cache:bitmap:on,offscreen:on")
        if not any(item.startswith("/bpp:") for item in options):
            options.append("/bpp:32")
        # L'opzione corrente è /t:<titolo>; /title è solo un alias interno
        # del parser e non è accettato da tutte le build distribuite.
        options.append(f"/t:FedTermHub — {sess.name or sess.host}")
        options.append("/floatbar:sticky:on,default:visible,show:fullscreen")

        if sess.rdp_start_fullscreen:
            options = [item for item in options if not item.startswith("/size:")]
            options.append("/f")
            monitors = enumerate_monitors()
            current_index = current_monitor_index_for_window(self, monitors)
            descriptor, _exact = resolve_monitor_selector(
                sess.rdp_monitor, monitors, current_index=current_index
            )
            if descriptor is not None:
                # FreeRDP seleziona i monitor tramite l'indice restituito dal
                # proprio backend. Su Fedora/KDE gli indici GDK e Xinerama sono
                # normalmente allineati; in caso contrario il WM applica il
                # normale fallback al monitor principale.
                options.extend(["/multimon", f"/monitors:{descriptor.index}"])
        return options

    def _launch_external_rdp(
        self,
        sess: Session,
        user: str,
        domain: str,
        password: Optional[str],
        preflight_ms: int = -1,
    ) -> bool:
        binary = shutil.which("xfreerdp3") or shutil.which("xfreerdp")
        if not binary:
            return False
        caps = probe_freerdp(binary)
        try:
            options = self._external_rdp_options(sess, user, domain, password)
        except Exception as exc:
            self.report_session_error("Opzioni RDP non valide", str(exc), sess.command)
            return True

        args_fd: Optional[int] = None
        launch_argv: List[str]
        if caps.args_from and hasattr(os, "memfd_create"):
            try:
                args_fd = os.memfd_create("fedtermhub-rdp-external", flags=0)
                os.fchmod(args_fd, 0o600)
                safe_options = list(options)
                if password:
                    safe_options.append(f"/p:{password}")
                payload = ("\n".join(safe_options) + "\n").encode(
                    "utf-8", errors="surrogatepass"
                )
                os.write(args_fd, payload)
                os.lseek(args_fd, 0, os.SEEK_SET)
                launch_argv = [binary, f"/args-from:fd:{args_fd}"]
            except Exception as exc:
                if args_fd is not None:
                    try:
                        os.close(args_fd)
                    except OSError:
                        pass
                self.report_session_error(
                    "Avvio RDP esterno non riuscito",
                    "Impossibile preparare il canale sicuro per le credenziali.",
                    str(exc),
                )
                return True
        else:
            # Fedora 44 espone /args-from:fd. Su build prive del supporto non
            # trasferiamo credenziali nella command line né nell'ambiente.
            contains_gateway_secret = any(",p:" in item for item in options)
            if password or contains_gateway_secret:
                self.report_session_error(
                    "FreeRDP troppo vecchio",
                    "La finestra RDP indipendente richiede /args-from:fd per proteggere le credenziali.",
                    f"Client rilevato: {caps.version}",
                )
                return True
            launch_argv = [binary] + options

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        log_path = CACHE_DIR / f"rdp-external-{int(time.time())}-{uuid.uuid4().hex[:6]}.log"
        log_handle = None
        try:
            log_handle = log_path.open("w", encoding="utf-8", buffering=1)
            popen_kwargs = dict(
                stdin=(subprocess.PIPE if password and args_fd is None else subprocess.DEVNULL),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                close_fds=True,
                env={**os.environ, "LC_ALL": "C"},
            )
            if args_fd is not None:
                popen_kwargs["pass_fds"] = (args_fd,)
            process = subprocess.Popen(launch_argv, **popen_kwargs)
            if password and args_fd is None and process.stdin is not None:
                process.stdin.write(f"{password}\n")
                process.stdin.flush()
                process.stdin.close()
        except Exception as exc:
            if log_handle is not None:
                log_handle.close()
            self.report_session_error(
                "Avvio RDP esterno non riuscito",
                str(exc),
                f"Client: {binary}",
            )
            return True
        finally:
            if args_fd is not None:
                try:
                    os.close(args_fd)
                except OSError:
                    pass

        processes = getattr(self, "_external_rdp_processes", None)
        if processes is None:
            processes = set()
            self._external_rdp_processes = processes
        processes.add(process)
        latency = f" Porta verificata in {preflight_ms} ms." if preflight_ms >= 0 else ""
        self.show_notification(
            "RDP indipendente avviato con il client FreeRDP ad alte prestazioni." + latency,
            "info", 4200,
        )
        self.set_status(f"RDP indipendente avviato: {sess.name or sess.host}")

        def watcher() -> None:
            return_code = process.wait()
            try:
                log_handle.close()
            except Exception:
                pass
            GLib.idle_add(processes.discard, process)
            if return_code != 0:
                try:
                    detail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
                except Exception:
                    detail = f"Log: {log_path}"
                GLib.idle_add(
                    self.report_session_error,
                    "Sessione RDP esterna terminata",
                    f"FreeRDP è terminato con codice {return_code}.",
                    detail or f"Log: {log_path}",
                    "",
                    sess,
                )
            return

        threading.Thread(
            target=watcher,
            name=f"fedtermhub-xfreerdp-{process.pid}",
            daemon=True,
        ).start()
        return True

    def _launch_rdp_ready(
        self,
        sess: Session,
        user: str,
        domain: str,
        password: Optional[str],
        preflight_ms: int = -1,
    ) -> bool:
        # Stabilizzazione 3.16.2: la modalità già sganciata torna alla finestra
        # gestita da FedTermHub. Il lancio diretto di xfreerdp viene escluso dal
        # flusso normale perché su Plasma/Wayland perde i controlli della finestra
        # e gli indici monitor Xinerama non coincidono sempre con quelli GDK.
        detached = getattr(sess, "rdp_window_mode", "embedded") == "detached"

        # Sessioni integrate e sganciate usano lo stesso backend nativo stabile;
        # Sgancia continua a trasferire il widget senza riconnettere.
        requested_rdp_mode = "legacy" if sess.rdp_mode in {"legacy", "external-legacy"} else "native"
        if requested_rdp_mode == "native" and NativeRdpTab is not None:
            try:
                native_ok, native_detail = native_backend_status()
            except Exception as exc:
                native_ok, native_detail = False, str(exc)
            if native_ok:
                try:
                    native_options = self._build_native_rdp_options(sess, user, domain, password)
                except Exception as exc:
                    self.report_session_error("Opzioni RDP non valide", str(exc), sess.command)
                    return False
                tab_holder: Dict[str, object] = {}

                def detach_callback(was_fullscreen: bool) -> None:
                    current_tab = tab_holder.get("tab")
                    if current_tab is not None:
                        self.detach_native_rdp_tab(
                            current_tab, sess.rdp_monitor, was_fullscreen
                        )

                tab = NativeRdpTab(
                    sess.name or f"RDP {sess.host}",
                    native_options,
                    password,
                    theme=self.settings.theme,
                    state_callback=self.set_status,
                    session_manager_callback=lambda: self.open_windows_session_manager(
                        sess, user, domain, password
                    ),
                    detach_callback=None if detached else detach_callback,
                    preferred_monitor=sess.rdp_monitor,
                    autostart_on_map=False,
                )
                tab_holder["tab"] = tab
                latency = f" Porta verificata in {preflight_ms} ms." if preflight_ms >= 0 else ""
                if detached:
                    self.open_detached_rdp_window(
                        tab,
                        monitor_selector=sess.rdp_monitor,
                        start_fullscreen=sess.rdp_start_fullscreen,
                        start_backend=True,
                    )
                    self.show_notification(
                        (
                            "RDP aperto in una finestra indipendente"
                            + (" a schermo intero." if sess.rdp_start_fullscreen else ".")
                            + latency
                        ),
                        "info", 3400,
                    )
                else:
                    tab.fullscreen_callback = lambda enabled: self.set_native_rdp_fullscreen(
                        tab, enabled, getattr(tab, "preferred_monitor", sess.rdp_monitor)
                    )
                    tab.close_callback = lambda: self.close_specific_tab(tab)
                    self.add_widget_tab(sess.name or f"RDP {sess.host}", tab)
                    if sess.rdp_start_fullscreen:
                        self.show_notification(
                            "Preparazione RDP immersivo sul monitor selezionato." + latency,
                            "info", 3200,
                        )
                        GLib.timeout_add(
                            120, self._open_native_rdp_fullscreen_and_start, tab, sess.rdp_monitor
                        )
                    else:
                        self.show_notification(
                            "Avvio motore RDP nativo a bassa latenza." + latency, "info", 3200
                        )
                        GLib.idle_add(tab.request_start)
                return False
            self.report_session_error(
                "Backend RDP nativo non disponibile",
                native_detail,
                "Reinstalla FedTermHub per ricompilare il componente nativo. "
                "Puoi selezionare manualmente la modalità legacy solo per diagnosi.",
            )
            return False
        if requested_rdp_mode == "native" and NativeRdpTab is None:
            self.report_session_error(
                "Modulo RDP nativo non caricato",
                str(NATIVE_RDP_IMPORT_ERROR or "native_rdp.py non disponibile"),
                "Esegui nuovamente ./install.sh e controlla fedtermhub --doctor.",
            )
            return False

        binary = shutil.which("xfreerdp3") or shutil.which("xfreerdp")
        if not binary:
            self.report_session_error(
                "FreeRDP non trovato",
                "RDP integrato richiede xfreerdp o xfreerdp3.",
                "Installa: sudo dnf install freerdp xorg-x11-server-Xwayland",
            )
            return False

        caps = probe_freerdp(binary)
        if not caps.parent_window:
            self.report_session_error(
                "FreeRDP non supporta l'integrazione",
                "Il client installato non espone l'opzione /parent-window.",
                f"Client rilevato: {caps.version}\nAggiorna il pacchetto freerdp e riprova.",
            )
            return False
        if not is_x11_display():
            self.report_session_error(
                "Backend grafico non compatibile con RDP interno",
                "FedTermHub non è in esecuzione sul backend X11/XWayland.",
                f"Backend rilevato: {current_gdk_backend()}\nAvvia: GDK_BACKEND=x11 fedtermhub",
            )
            return False

        host = sess.host if not sess.port else f"{host_for_uri(sess.host)}:{sess.port}"
        argv_base = [f"/v:{host}"]
        if sess.rdp_dynamic_resolution:
            argv_base.append("+dynamic-resolution")
        elif sess.rdp_width.isdigit() and sess.rdp_height.isdigit():
            argv_base.append(f"/size:{sess.rdp_width}x{sess.rdp_height}")
        if sess.rdp_smart_sizing and not sess.rdp_dynamic_resolution:
            argv_base.append("/smart-sizing")
        if sess.rdp_scale in {"100", "140", "180"}:
            argv_base.append(f"/scale:{sess.rdp_scale}")
        if sess.rdp_cert_ignore:
            argv_base.append("/cert:ignore")
        if sess.rdp_clipboard:
            argv_base.append("/clipboard")
        if sess.rdp_fonts:
            argv_base.append("+fonts")
        if sess.rdp_auto_reconnect:
            argv_base.append("+auto-reconnect")
        if sess.rdp_sound:
            argv_base.append("/sound")
        if sess.rdp_microphone:
            argv_base.append("/microphone")
        if sess.rdp_printer:
            argv_base.append("/printer")
        if sess.rdp_smartcard:
            argv_base.append("/smartcard")
        if sess.rdp_multimon:
            self.show_notification(
                "RDP interno: Multi monitor è stato disattivato per evitare instabilità XEmbed. La tab usa un singolo desktop adattivo.",
                "warning",
                6500,
            )
        if sess.rdp_admin:
            argv_base.append("+admin")
        if sess.rdp_drive:
            argv_base.append(f"/drive:fedtermhub,{normalize_rdp_drive_path(sess.rdp_drive)}")
        if sess.rdp_gfx:
            argv_base.append("/gfx")
        if sess.rdp_compression:
            argv_base.append("+compression")
        if caps.grab_keyboard and not sess.rdp_grab_keyboard:
            argv_base.append("-grab-keyboard")
        if caps.grab_mouse and not sess.rdp_grab_mouse:
            argv_base.append("-grab-mouse")

        layout_map = {
            "it": "0x00000410", "us": "0x00000409", "de": "0x00000407",
            "fr": "0x0000040C", "es": "0x0000040A",
        }
        layout = (sess.rdp_keyboard_layout or "auto").strip()
        if layout != "auto":
            argv_base.append(f"/kbd:layout:{layout_map.get(layout, layout)}")
        if sess.rdp_timeout.isdigit():
            argv_base.append(f"/timeout:{sess.rdp_timeout}")
        if sess.rdp_security in {"nla", "tls", "rdp"}:
            argv_base.append(f"/sec:{sess.rdp_security}")

        quality = sess.rdp_quality if sess.rdp_quality in {"lan", "wan", "low"} else "lan"
        argv_base.append({"wan": "/network:wan", "low": "/network:broadband-low"}.get(quality, "/network:lan"))
        if user:
            argv_base.append(f"/u:{user}")
        if domain:
            argv_base.append(f"/d:{domain}")

        stdin_secret: Optional[str] = None
        use_args_fd = False
        if password:
            if sess.rdp_password_stdin:
                if not caps.args_from or not hasattr(os, "memfd_create"):
                    self.report_session_error(
                        "Password sicura non supportata",
                        "Servono /args-from:fd e memfd Linux per passare la password senza esporla negli argomenti.",
                        f"Client rilevato: {caps.version}\nAggiorna FreeRDP. In alternativa abilita temporaneamente la modalità password legacy nella sessione.",
                    )
                    return False
                stdin_secret = password
                use_args_fd = True
            else:
                argv_base.append(f"/p:{password}")

        if sess.command:
            try:
                argv_base.extend(shlex.split(sess.command))
            except Exception as exc:
                self.report_session_error("Opzioni RDP non valide", str(exc), sess.command)
                return False

        argv = [binary] + argv_base
        masked = self.mask_args(argv + ["/parent-window:<FedTermHub-tab>"])
        if use_args_fd:
            masked += "  [password protetta via /args-from:fd su memfd]"
        can_embed = hasattr(Gtk, "Socket") and is_x11_display() and caps.parent_window
        tab = RdpTab(
            sess.name or f"RDP {sess.host}", argv, masked, embedded=can_embed,
            theme=self.settings.theme,
            error_callback=lambda title, message, details, command: self.report_session_error(
                title, message, details, command, sess
            ),
            stdin_secret=stdin_secret, use_args_fd=use_args_fd, state_callback=self.set_status,
        )
        self.add_widget_tab(sess.name or f"RDP {sess.host}", tab)
        if can_embed:
            latency = f" Porta verificata in {preflight_ms} ms." if preflight_ms >= 0 else ""
            self.show_notification(
                "Preparazione RDP interna in corso." + latency,
                "info",
                3200,
            )
            self.set_status("Preparazione sessione RDP interna…")
            GLib.idle_add(tab.request_start)
        else:
            self.report_session_error(
                "RDP integrato non disponibile",
                "Gtk.Socket/X11 o /parent-window non sono disponibili.",
                f"Backend: {current_gdk_backend()}\nClient: {caps.version}\nAvvia con GDK_BACKEND=x11 fedtermhub",
            )
        return False

    def launch_vnc(self, sess: Session) -> None:
        if not sess.host:
            self.set_status("Host VNC mancante.")
            return
        binary = shutil.which("vncviewer")
        if not binary:
            self.set_status("TigerVNC client non trovato. Installa: sudo dnf install tigervnc")
            return
        _user, _domain, password = self.effective_identity(sess)
        target = vnc_target(sess.host, sess.port)
        argv = [binary, target]
        if sess.command:
            argv.extend(shlex.split(sess.command))
        try:
            subprocess.Popen(argv)
        except Exception as exc:
            self.set_status(f"Errore avvio VNC: {exc}")
            return
        extra = "Password VNC non autoinviata: vncviewer la chiederà nella sua finestra." if password else ""
        self.add_terminal_tab(
            f"Log VNC {sess.name}",
            ["/bin/sh", "-lc", f"echo Avviato: {shlex.join(argv)}; echo La finestra VNC è esterna.; echo {shlex.quote(extra)}; exec bash"],
        )

    def launch_sftp(self, sess: Session) -> None:
        if not sess.host:
            self.set_status("Host SFTP mancante.")
            return
        user, _domain, password = self.effective_identity(sess)
        env: Dict[str, str] = {}
        sftp_binary = "sftp"
        prefix: List[str] = []
        if password:
            sshpass = shutil.which("sshpass")
            if sshpass:
                prefix = [sshpass, "-e"]
                env["SSHPASS"] = password
            else:
                self.set_status("Credenziale collegata, ma sshpass non è installato. Installa: sudo dnf install sshpass")
        target = sess.host if not user else f"{user}@{sess.host}"
        argv = prefix + [sftp_binary] + self.ssh_session_options()
        jump_spec = self.proxy_jump_spec(sess)
        if getattr(sess, "routing_mode", "direct") == "proxy_jump" and not jump_spec:
            self.report_session_error(
                "Jump host incompleto",
                "Compila il server jump host nella sezione Instradamento avanzato.",
                f"Sessione: {sess.name}",
            )
            return
        if jump_spec:
            argv += ["-J", jump_spec]
        if sess.port:
            argv += ["-P", sess.port]
        argv += ["--", target]
        self.add_terminal_tab(sess.name or f"SFTP {target}", argv, extra_env=env, session=sess, close_on_exit="on_success")

    def add_terminal_tab(
        self,
        title: str,
        argv: List[str],
        extra_env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        session: Optional[Session] = None,
        close_on_exit: str = "never",
    ) -> None:
        """Apre una scheda terminale.

        close_on_exit dice cosa fare quando il programma nella scheda termina:
        "always" (shell locale, strumenti rapidi che dicono "Invio per chiudere"),
        "on_success" (ssh/sftp/seriale: si chiude con "exit", resta con il pulsante di
        riconnessione se la connessione cade), "never" (comandi di cui serve leggere l'output).
        """
        tab = TerminalTab(
            title,
            argv,
            cwd=cwd,
            extra_env=extra_env,
            theme=self.settings.theme,
            settings=self.settings,
        )
        tab.fedtermhub_session = session
        tab.fedtermhub_protocol = (session.protocol.lower() if session is not None else "")
        # Inseguimento della directory: la shell deve emettere OSC 7.
        # In locale ci pensa /etc/profile.d/vte.sh (VTE_VERSION e' impostata).
        # Sugli host remoti via SSH quel file non c'e' quasi mai e VTE_VERSION
        # non attraversa sshd, quindi il supporto va iniettato nella shell.
        # Limitato a ssh: su seriale, RDP o comandi arbitrari l'iniezione
        # finirebbe in un posto sbagliato.
        if tab.fedtermhub_protocol == "ssh" and self.settings.follow_remote_directory:
            tab.osc7_bootstrap = True
        tab.on_directory_change = self._on_terminal_directory_change
        tab.on_last_pane_close = lambda workspace, pane, explicit: self._on_terminal_finished(
            workspace, pane, explicit, close_on_exit
        )
        self.terminal_tabs.append(tab)
        label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        label_box.get_style_context().add_class("tab-label")
        status_dot = Gtk.Label(label="●")
        status_dot.get_style_context().add_class("tab-status")
        status_dot.get_style_context().add_class("is-connected")
        label_box.pack_start(status_dot, False, False, 0)
        label = Gtk.Label(label=title[:32])
        label.set_tooltip_text(title)
        reconnect = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.MENU)
        reconnect.set_tooltip_text("Riconnetti la sessione (Ctrl+Shift+R)")
        reconnect.set_relief(Gtk.ReliefStyle.NONE)
        reconnect.set_can_focus(False)
        reconnect.get_style_context().add_class("tab-reconnect")
        reconnect.set_no_show_all(True)  # visibile solo quando la sessione è caduta
        reconnect.connect("clicked", lambda *_a, t=tab: t.reconnect())
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_tooltip_text("Chiudi tab")
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_can_focus(False)
        close.get_style_context().add_class("tab-close")
        label_box.pack_start(label, True, True, 0)
        label_box.pack_start(reconnect, False, False, 0)
        label_box.pack_start(close, False, False, 0)
        label_box.show_all()

        def refresh_status(workspace) -> None:  # noqa: ANN001
            connected = bool(getattr(workspace, "is_connected", False))
            ctx = status_dot.get_style_context()
            if connected:
                ctx.add_class("is-connected")
                ctx.remove_class("is-disconnected")
                status_dot.set_tooltip_text("Sessione attiva")
                reconnect.hide()
            else:
                ctx.add_class("is-disconnected")
                ctx.remove_class("is-connected")
                status_dot.set_tooltip_text("Sessione caduta — clicca ⟳ per riconnettere")
                reconnect.show()

        # Solo le tab con una sessione (SSH/serial) mostrano stato/riconnessione;
        # per un terminale locale la caduta della shell equivale a "chiuso".
        if session is not None:
            tab.on_state_change = refresh_status
            GLib.idle_add(lambda: (refresh_status(tab), False)[1])
        else:
            status_dot.hide()
        page = self.notebook.append_page(tab, label_box)
        self.notebook.set_tab_reorderable(tab, True)
        close.connect("clicked", lambda _b: self.close_specific_tab(tab))
        tab.show_all()
        self.notebook.set_current_page(page)
        self.set_status(f"Aperto: {title}")

    def _tree_path_for_store_path(self, store_path: str) -> Optional[Gtk.TreePath]:
        """Trova il Gtk.TreePath visibile associato al percorso nel SessionStore."""
        def walk(parent: Optional[Gtk.TreeIter]) -> Optional[Gtk.TreePath]:
            tree_iter = self.tree_store.iter_children(parent)
            while tree_iter is not None:
                if self.tree_store[tree_iter][self.COL_PATH] == store_path:
                    return self.tree_store.get_path(tree_iter)
                child_result = walk(tree_iter)
                if child_result is not None:
                    return child_result
                tree_iter = self.tree_store.iter_next(tree_iter)
            return None

        try:
            return walk(None)
        except Exception:
            return None

    def _select_store_path(self, store_path: str) -> None:
        tree_path = self._tree_path_for_store_path(store_path)
        if tree_path is None:
            return
        try:
            indices = tree_path.get_indices()
            for depth in range(1, len(indices)):
                parent = Gtk.TreePath.new_from_indices(indices[:depth])
                self.session_tree.expand_row(parent, False)
            self.session_tree.get_selection().select_path(tree_path)
            self.session_tree.scroll_to_cell(tree_path, None, True, 0.5, 0.0)
        except Exception:
            pass

    def _cancel_drag_hover_expand(self) -> None:
        if self._drag_hover_expand_id is not None:
            try:
                GLib.source_remove(self._drag_hover_expand_id)
            except Exception:
                pass
        self._drag_hover_expand_id = None
        self._drag_hover_tree_path = None

    def _schedule_drag_folder_expand(self, tree_path: Gtk.TreePath) -> None:
        path_text = tree_path.to_string()
        if self._drag_hover_tree_path == path_text:
            return
        self._cancel_drag_hover_expand()
        self._drag_hover_tree_path = path_text

        def expand_if_still_hovered() -> bool:
            self._drag_hover_expand_id = None
            if self._drag_hover_tree_path != path_text:
                return False
            try:
                path = Gtk.TreePath.new_from_string(path_text)
                if not self.session_tree.row_expanded(path):
                    self.session_tree.expand_row(path, False)
            except Exception:
                pass
            return False

        self._drag_hover_expand_id = GLib.timeout_add(650, expand_if_still_hovered)

    def _session_drop_target(self, x: int, y: int) -> Tuple[str, Optional[int], Optional[Gtk.TreePath], Optional[Gtk.TreeViewDropPosition]]:
        """Calcola cartella, indice e indicatore grafico della destinazione."""
        destination = self.session_tree.get_dest_row_at_pos(int(x), int(y))
        if destination is None:
            return "", None, None, None
        tree_path, position = destination
        tree_iter = self.tree_store.get_iter(tree_path)
        store_path = str(self.tree_store[tree_iter][self.COL_PATH])
        node = self.store.get_node(store_path)
        if isinstance(node, FolderNode):
            # Su una cartella il comportamento è sempre chiaro: inserisci dentro.
            return store_path, None, tree_path, Gtk.TreeViewDropPosition.INTO_OR_AFTER

        parent_path = self.store.parent_path(store_path)
        sibling_index = int(store_path.rsplit(".", 1)[-1])
        before = position in (
            Gtk.TreeViewDropPosition.BEFORE,
            Gtk.TreeViewDropPosition.INTO_OR_BEFORE,
        )
        target_index = sibling_index if before else sibling_index + 1
        indicator = Gtk.TreeViewDropPosition.BEFORE if before else Gtk.TreeViewDropPosition.AFTER
        return parent_path, target_index, tree_path, indicator

    def on_session_drag_begin(self, _tree: Gtk.TreeView, context: Gdk.DragContext) -> None:
        selected = self.selected_path_and_node()
        self._drag_source_store_path = selected[0] if selected is not None else None
        self._clear_drag_hover_visual()
        if selected is None:
            return
        path, node = selected
        if self.store.is_home_path(path):
            self._drag_source_store_path = None
            self._drag_icon_widget = None
            return
        try:
            card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            card.get_style_context().add_class("dnd-drag-card")
            if isinstance(node, FolderNode):
                detail = "Cartella"
                pixbuf = self._folder_icon_pixbuf(node.color)
                image = Gtk.Image.new_from_pixbuf(pixbuf) if pixbuf is not None else Gtk.Image.new_from_icon_name("folder-symbolic", Gtk.IconSize.MENU)
            else:
                icon_name = self.protocol_icon(node.protocol)
                detail = self._session_detail(node)
                image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
            label_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            title = Gtk.Label(label=node.name, xalign=0)
            title.get_style_context().add_class("dnd-drag-title")
            subtitle = Gtk.Label(label=detail, xalign=0)
            subtitle.set_ellipsize(Pango.EllipsizeMode.END)
            subtitle.set_max_width_chars(34)
            subtitle.get_style_context().add_class("dnd-drag-detail")
            label_box.pack_start(title, False, False, 0)
            label_box.pack_start(subtitle, False, False, 0)
            card.pack_start(image, False, False, 0)
            card.pack_start(label_box, True, True, 0)
            card.show_all()
            self._drag_icon_widget = card
            Gtk.drag_set_icon_widget(context, card, 12, 12)
        except Exception:
            self._drag_icon_widget = None

    def _dnd_hover_color(self) -> str:
        return "#244f69" if self.settings.theme == "dark" else "#cdeaff"

    def _clear_drag_hover_visual(self) -> None:
        store_path = self._drag_hover_store_path
        self._drag_hover_store_path = None
        if not store_path:
            return
        tree_path = self._tree_path_for_store_path(store_path)
        if tree_path is None:
            return
        try:
            tree_iter = self.tree_store.get_iter(tree_path)
            self.tree_store[tree_iter][self.COL_BG] = ""
            self.tree_store[tree_iter][self.COL_BG_SET] = False
            self.session_tree.queue_draw()
        except Exception:
            pass

    def _set_drag_hover_visual(self, tree_path: Optional[Gtk.TreePath]) -> None:
        target_store_path: Optional[str] = None
        if tree_path is not None:
            try:
                tree_iter = self.tree_store.get_iter(tree_path)
                target_store_path = str(self.tree_store[tree_iter][self.COL_PATH])
            except Exception:
                target_store_path = None
        if target_store_path == self._drag_hover_store_path:
            return
        self._clear_drag_hover_visual()
        if not target_store_path:
            return
        self._drag_hover_store_path = target_store_path
        current_path = self._tree_path_for_store_path(target_store_path)
        if current_path is None:
            return
        try:
            tree_iter = self.tree_store.get_iter(current_path)
            self.tree_store[tree_iter][self.COL_BG] = self._dnd_hover_color()
            self.tree_store[tree_iter][self.COL_BG_SET] = True
            self.session_tree.queue_draw()
        except Exception:
            self._drag_hover_store_path = None

    def _animate_moved_row(self, store_path: str) -> None:
        if SAFE_MODE or not self.settings.animations:
            return
        dark = self.settings.theme == "dark"
        frames = [
            (("#2d789d" if dark else "#9ddcff"), 24),
            (("#286b8c" if dark else "#b8e6ff"), 19),
            (("#245b77" if dark else "#cceeff"), 14),
            (("#20485e" if dark else "#def4ff"), 9),
            (("#1a3545" if dark else "#edf9ff"), 5),
            ("#000000", 3),
        ]
        index = 0

        def tick() -> bool:
            nonlocal index
            tree_path = self._tree_path_for_store_path(store_path)
            if tree_path is None or index >= len(frames):
                return False
            try:
                tree_iter = self.tree_store.get_iter(tree_path)
                bg, xpad = frames[index]
                self.tree_store[tree_iter][self.COL_BG] = bg
                self.tree_store[tree_iter][self.COL_BG_SET] = index < len(frames) - 1
                self.tree_store[tree_iter][self.COL_TEXT_XPAD] = xpad
                self.session_tree.queue_draw()
            except Exception:
                return False
            index += 1
            return index < len(frames)

        GLib.timeout_add(45, tick)

    def on_session_drag_data_get(
        self, _tree: Gtk.TreeView, _context: Gdk.DragContext, selection_data: Gtk.SelectionData, _info: int, _time: int
    ) -> None:
        selected = self.selected_path_and_node()
        if selected is None:
            return
        source_path, _node = selected
        if self.store.is_home_path(source_path):
            self._drag_source_store_path = None
            return
        self._drag_source_store_path = source_path
        payload = source_path.encode("utf-8")
        selection_data.set(selection_data.get_target(), 8, payload)

    def on_session_drag_motion(self, _tree: Gtk.TreeView, context: Gdk.DragContext, x: int, y: int, event_time: int) -> bool:
        try:
            target_folder, _target_index, tree_path, indicator = self._session_drop_target(x, y)
            source_path = self._drag_source_store_path
            if source_path:
                source_node = self.store.get_node(source_path)
                if isinstance(source_node, FolderNode) and (
                    target_folder == source_path or target_folder.startswith(source_path + ".")
                ):
                    self.session_tree.set_drag_dest_row(None, Gtk.TreeViewDropPosition.BEFORE)
                    self._clear_drag_hover_visual()
                    context.drag_status(Gdk.DragAction.DEFAULT, event_time)
                    self._cancel_drag_hover_expand()
                    return True
            if tree_path is not None and indicator is not None:
                self.session_tree.set_drag_dest_row(tree_path, indicator)
                self._set_drag_hover_visual(tree_path)
                if indicator in (Gtk.TreeViewDropPosition.INTO_OR_BEFORE, Gtk.TreeViewDropPosition.INTO_OR_AFTER):
                    self._schedule_drag_folder_expand(tree_path)
                else:
                    self._cancel_drag_hover_expand()
            else:
                self.session_tree.set_drag_dest_row(None, Gtk.TreeViewDropPosition.BEFORE)
                self._clear_drag_hover_visual()
                self._cancel_drag_hover_expand()
            context.drag_status(Gdk.DragAction.MOVE, event_time)
            return True
        except Exception:
            context.drag_status(Gdk.DragAction.DEFAULT, event_time)
            return True

    def on_session_drag_data_received(
        self, _tree: Gtk.TreeView, context: Gdk.DragContext, x: int, y: int,
        selection_data: Gtk.SelectionData, _info: int, event_time: int
    ) -> None:
        success = False
        try:
            raw = selection_data.get_data()
            source_path = bytes(raw).decode("utf-8").strip() if raw else (self._drag_source_store_path or "")
            if not source_path:
                raise ValueError("Sorgente del trascinamento non disponibile.")
            source_node = self.store.get_node(source_path)
            target_folder, target_index, _tree_path, _indicator = self._session_drop_target(x, y)
            self._clear_drag_hover_visual()
            new_path = self.store.move_node(source_path, target_folder, target_index)
            self.refresh_sessions()
            self._select_store_path(new_path)
            self._animate_moved_row(new_path)
            destination_label = self.folder_label(self.store.parent_path(new_path))
            self.set_status(f"Spostato '{source_node.name}' in {destination_label}")
            self.show_notification(f"{source_node.name} spostato in {destination_label}", "success")
            success = True
        except Exception as exc:
            self.set_status(f"Impossibile spostare la sessione: {exc}")
            self.show_notification(str(exc), "warning")
        finally:
            self._cancel_drag_hover_expand()
            self._clear_drag_hover_visual()
            self._drag_source_store_path = None
            self._drag_icon_widget = None
            try:
                self.session_tree.set_drag_dest_row(None, Gtk.TreeViewDropPosition.BEFORE)
            except Exception:
                pass
            Gtk.drag_finish(context, success, False, event_time)

    def on_session_drag_leave(self, _tree: Gtk.TreeView, _context: Gdk.DragContext, _time: int) -> None:
        self._cancel_drag_hover_expand()
        self._clear_drag_hover_visual()
        try:
            self.session_tree.set_drag_dest_row(None, Gtk.TreeViewDropPosition.BEFORE)
        except Exception:
            pass

    def on_session_drag_end(self, _tree: Gtk.TreeView, _context: Gdk.DragContext) -> None:
        self._cancel_drag_hover_expand()
        self._clear_drag_hover_visual()
        self._drag_source_store_path = None
        self._drag_icon_widget = None
        try:
            self.session_tree.set_drag_dest_row(None, Gtk.TreeViewDropPosition.BEFORE)
        except Exception:
            pass

    def _build_session_context_menu(self, selected: Optional[Tuple[str, Node]]) -> Gtk.Menu:
        """Crea un menu contestuale coerente con il tipo di nodo selezionato.

        Home Session è una radice applicativa protetta: non eredita le azioni
        Modifica, Duplica, Sposta, Colore o Elimina delle cartelle normali.
        """
        menu = Gtk.Menu()

        def add_item(label: str, callback: Callable[..., object]) -> Gtk.MenuItem:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _i, cb=callback: cb(Gtk.Button()))
            menu.append(item)
            return item

        if selected is None:
            add_item("Nuova sessione", self.on_add_session)
            add_item("Nuova cartella", self.on_add_folder)
            return menu

        path, node = selected
        is_home = self.store.is_home_path(path)
        add_item("Espandi o comprimi" if isinstance(node, FolderNode) else "Connetti", self.on_context_open)

        if is_home:
            add_item("Apri tutte le sessioni", self.on_open_all_in_selected_folder)
            menu.append(Gtk.SeparatorMenuItem())
            add_item("Nuova sessione", self.on_add_session)
            add_item("Nuova cartella", self.on_add_folder)
            menu.append(Gtk.SeparatorMenuItem())
            add_item("Esporta tutte le sessioni", self.export_home_session)
            add_item("Importa sessioni qui", self.import_into_selected_folder)
            add_item("Sostituisci tutte le sessioni con un file", self.import_home_session)
            menu.append(Gtk.SeparatorMenuItem())
            add_item("Dettagli", self.on_show_selected_details)
            return menu

        if isinstance(node, Session) and node.protocol.lower() in {"rdp", "ssh", "sftp", "vnc"} and bool(node.host.strip()):
            add_item("Diagnostica connessione", self.on_diagnose_selected)

        add_item("Modifica", self.on_edit_selected)
        add_item("Duplica", self.on_duplicate_selected)
        add_item("Sposta", self.on_move_selected)

        if isinstance(node, FolderNode):
            add_item("Apri tutte le sessioni", self.on_open_all_in_selected_folder)
            color_item = Gtk.MenuItem(label="Colore cartella")
            color_menu = Gtk.Menu()
            for color_name, color_value in FOLDER_COLOR_PRESETS:
                preset = Gtk.MenuItem(label=color_name)
                preset.connect("activate", lambda _i, value=color_value: self.set_selected_folder_color(value))
                color_menu.append(preset)
            color_menu.append(Gtk.SeparatorMenuItem())
            custom = Gtk.MenuItem(label="Personalizzato…")
            custom.connect("activate", lambda *_: self.choose_selected_folder_color())
            color_menu.append(custom)
            color_item.set_submenu(color_menu)
            menu.append(color_item)

        menu.append(Gtk.SeparatorMenuItem())
        add_item("Esporta la selezione", self.export_selected_node)
        if isinstance(node, FolderNode):
            add_item("Importa sessioni qui", self.import_into_selected_folder)
        menu.append(Gtk.SeparatorMenuItem())
        add_item("Dettagli", self.on_show_selected_details)
        menu.append(Gtk.SeparatorMenuItem())
        add_item("Elimina", self.on_delete_selected)
        return menu

    def on_tree_button_press(self, tree: Gtk.TreeView, event) -> bool:  # noqa: ANN001
        if event.type != Gdk.EventType.BUTTON_PRESS or event.button != 3:
            return False
        pos = tree.get_path_at_pos(int(event.x), int(event.y))
        if pos is not None:
            path, _column, _cell_x, _cell_y = pos
            tree.get_selection().select_path(path)
        menu = self._build_session_context_menu(self.selected_path_and_node())
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def on_diagnose_selected(self, _widget: Optional[Gtk.Widget] = None) -> None:
        selected = self.selected_path_and_node()
        if selected is None or not isinstance(selected[1], Session):
            self.show_notification("Seleziona una sessione remota da diagnosticare.", "warning", 3500)
            return
        self.show_connection_diagnostics(selected[1])

    def on_context_open(self, _widget: Gtk.Widget) -> None:
        selected = self.selected_path_and_node()
        if selected is None:
            return
        store_path, node = selected
        if isinstance(node, FolderNode):
            tree_path = self._tree_path_for_store_path(store_path)
            if tree_path is not None:
                if self.session_tree.row_expanded(tree_path):
                    self.session_tree.collapse_row(tree_path)
                else:
                    self.session_tree.expand_row(tree_path, False)
            return
        self.open_session(node)

    def on_open_all_in_selected_folder(self, _button: Optional[Gtk.Button]) -> None:
        selected = self.selected_path_and_node()
        if selected is None or not isinstance(selected[1], FolderNode):
            return
        sessions: List[Session] = []

        def collect(folder: FolderNode) -> None:
            for child in folder.children:
                if isinstance(child, FolderNode):
                    collect(child)
                else:
                    sessions.append(child)

        collect(selected[1])
        if not sessions:
            self.show_notification("La cartella non contiene sessioni.", "info", 2500)
            return
        if len(sessions) > 5:
            confirm = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.OK_CANCEL,
                text=f"Aprire {len(sessions)} sessioni?",
            )
            confirm.format_secondary_text(
                f"Si apriranno {len(sessions)} schede, comprese le sottocartelle di «{selected[1].name}»."
            )
            response = confirm.run()
            confirm.destroy()
            if response != Gtk.ResponseType.OK:
                return
        self.show_notification(f"Apertura di {len(sessions)} sessioni…", "info", 3000)
        for index, session in enumerate(sessions):
            GLib.timeout_add(50 + 180 * index, lambda sess=session: (self.open_session(sess), False)[1])

    @staticmethod
    def _folder_recursive_stats(folder: FolderNode) -> Tuple[int, int]:
        session_count = 0
        folder_count = 0
        for child in folder.children:
            if isinstance(child, FolderNode):
                folder_count += 1
                sub_sessions, sub_folders = AppWindow._folder_recursive_stats(child)
                session_count += sub_sessions
                folder_count += sub_folders
            else:
                session_count += 1
        return session_count, folder_count

    def on_show_selected_details(self, _button: Optional[Gtk.Button]) -> None:
        selected = self.selected_path_and_node()
        if selected is None:
            self.set_status("Seleziona una sessione o cartella.")
            return
        path, node = selected
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.CLOSE,
            text=node.name,
        )
        dialog.set_title("Dettagli")
        if isinstance(node, FolderNode):
            recursive_sessions, recursive_folders = self._folder_recursive_stats(node)
            direct_sessions = sum(isinstance(child, Session) for child in node.children)
            direct_folders = sum(isinstance(child, FolderNode) for child in node.children)
            if self.store.is_home_path(path):
                details = (
                    "Tipo: Radice Home Session\n"
                    "Stato: struttura principale protetta\n\n"
                    f"Contenuto diretto: {direct_sessions} sessioni, {direct_folders} cartelle\n"
                    f"Contenuto complessivo: {recursive_sessions} sessioni, {recursive_folders} sottocartelle"
                )
            else:
                parent = self.folder_label(self.store.parent_path(path))
                details = (
                    f"Tipo: Cartella\n"
                    f"Posizione: {parent}\n"
                    f"Colore icona: {normalize_folder_color(node.color)}\n\n"
                    f"Contenuto diretto: {direct_sessions} sessioni, {direct_folders} cartelle\n"
                    f"Contenuto complessivo: {recursive_sessions} sessioni, {recursive_folders} sottocartelle"
                )
        else:
            fields = [
                f"Tipo: Sessione {node.protocol.upper()}",
                f"Posizione: {self.folder_label(self.store.parent_path(path))}",
            ]
            if node.host:
                fields.append(f"Host: {node.host}")
            if node.port:
                fields.append(f"Porta: {node.port}")
            if node.user:
                fields.append(f"Utente: {node.user}")
            if node.domain:
                fields.append(f"Dominio: {node.domain}")
            fields.append(f"Credenziale salvata: {'collegata' if node.credential_id else 'non collegata'}")
            routing_mode = getattr(node, "routing_mode", "direct")
            if routing_mode == "saved_tunnel" and node.required_tunnel_id:
                tunnel = self.tunnel_store.get(node.required_tunnel_id)
                fields.append(f"Instradamento: tunnel {tunnel.name if tunnel else 'non trovato'}")
            elif routing_mode == "proxy_jump":
                fields.append(f"Instradamento: jump host {self.proxy_jump_spec(node) or 'incompleto'}")
            elif routing_mode == "rd_gateway":
                gateway = node.rdp_gateway_host or "incompleto"
                fields.append(f"Instradamento: RD Gateway {gateway}:{node.rdp_gateway_port or '443'}")
            if node.notes:
                fields.extend(["", "Note:", node.notes])
            details = "\n".join(fields)
        dialog.format_secondary_text(details)
        dialog.run()
        dialog.destroy()

    def set_selected_folder_color(self, color: str) -> None:
        selected = self.selected_path_and_node()
        if selected is None or not isinstance(selected[1], FolderNode):
            return
        path, folder = selected
        if self.store.is_home_path(path):
            self.set_status("Home Session usa un'icona dedicata e non ha un colore personalizzabile.")
            return
        folder.color = normalize_folder_color(color)
        self.store.replace_node(path, folder)
        self.refresh_sessions()
        self._select_store_path(path)
        self.set_status(f"Colore cartella aggiornato: {folder.name}")

    def choose_selected_folder_color(self) -> None:
        selected = self.selected_path_and_node()
        if selected is None or not isinstance(selected[1], FolderNode):
            return
        path, folder = selected
        if self.store.is_home_path(path):
            self.set_status("Home Session usa un'icona dedicata e non ha un colore personalizzabile.")
            return
        dialog = Gtk.ColorChooserDialog(title=f"Colore — {folder.name}", transient_for=self)
        rgba = Gdk.RGBA()
        rgba.parse(normalize_folder_color(folder.color))
        dialog.set_rgba(rgba)
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                chosen = dialog.get_rgba()
                color = "#{:02x}{:02x}{:02x}".format(
                    round(chosen.red * 255), round(chosen.green * 255), round(chosen.blue * 255)
                )
                self.set_selected_folder_color(color)
        finally:
            dialog.destroy()

    def show_notification(self, text: str, level: str = "info", auto_hide_ms: Optional[int] = None) -> None:
        if not hasattr(self, "notification_revealer"):
            return
        if self._notification_hide_id:
            try:
                GLib.source_remove(self._notification_hide_id)
            except Exception:
                pass
            self._notification_hide_id = None
        icons = {
            "error": "dialog-error-symbolic", "warning": "dialog-warning-symbolic",
            "success": "emblem-ok-symbolic", "info": "dialog-information-symbolic",
        }
        self.notification_icon.set_from_icon_name(icons.get(level, icons["info"]), Gtk.IconSize.MENU)
        self.notification_text.set_text(text)
        ctx = self.notification_revealer.get_child().get_style_context()
        for cls in ["notice-info", "notice-error", "notice-warning", "notice-success"]:
            ctx.remove_class(cls)
        ctx.add_class("notice-" + (level if level in {"info", "error", "warning", "success"} else "info"))
        self.notification_revealer.set_reveal_child(True)
        if auto_hide_ms is None:
            auto_hide_ms = int(self.settings.notification_duration_ms)
        if auto_hide_ms > 0:
            self._notification_hide_id = GLib.timeout_add(auto_hide_ms, self.hide_notification)

    def hide_notification(self) -> bool:
        if hasattr(self, "notification_revealer"):
            self.notification_revealer.set_reveal_child(False)
        self._notification_hide_id = None
        return False

    def open_detached_rdp_window(
        self,
        tab,
        monitor_selector: str = "current",
        start_fullscreen: bool = False,
        start_backend: bool = True,
    ) -> RdpDetachedWindow:
        window = RdpDetachedWindow(
            self,
            tab,
            monitor_selector=monitor_selector,
            start_fullscreen=start_fullscreen,
            start_backend=start_backend,
        )
        self.detached_rdp_windows.append(window)
        return window

    def detach_native_rdp_tab(
        self, tab, monitor_selector: str = "current", was_fullscreen: bool = False
    ) -> None:
        if NativeRdpTab is None or not isinstance(tab, NativeRdpTab):
            return
        page_num = self.notebook.page_num(tab)
        if page_num < 0:
            return
        transfer_fullscreen = bool(was_fullscreen or getattr(tab, "fullscreen_active", False))
        if getattr(tab, "fullscreen_active", False):
            self.set_native_rdp_fullscreen(tab, False)
        # Manteniamo un riferimento Python mentre il widget viene rimosso dal
        # notebook, così il processo RDP e il framebuffer non vengono distrutti.
        self.notebook.remove_page(page_num)
        tab.set_detach_callback(None)
        self.open_detached_rdp_window(
            tab,
            monitor_selector=monitor_selector,
            start_fullscreen=transfer_fullscreen,
            start_backend=False,
        )
        # Il reparenting cambia allocation e superficie GTK. Richiediamo un
        # fotogramma completo dopo che la nuova finestra è stata mappata, così
        # non restano porzioni del framebuffer precedente o bande nere.
        if hasattr(tab, "request_full_refresh"):
            GLib.timeout_add(120, tab.request_full_refresh)
        self.set_status("Sessione RDP sganciata — FedTermHub resta disponibile")

    def _on_detached_rdp_destroyed(self, window: RdpDetachedWindow) -> None:
        if window in self.detached_rdp_windows:
            self.detached_rdp_windows.remove(window)
        self.set_status("Finestra RDP indipendente chiusa")

    def _current_monitor_index(self, monitors: List[MonitorDescriptor]) -> Optional[int]:
        return current_monitor_index_for_window(self, monitors)

    def _resolve_rdp_fullscreen_monitor(
        self, selector: str
    ) -> Tuple[Optional[MonitorDescriptor], bool]:
        monitors = enumerate_monitors()
        return resolve_monitor_selector(
            selector, monitors, current_index=self._current_monitor_index(monitors)
        )

    def _verify_rdp_fullscreen_monitor(self, tab, expected_index: int) -> bool:
        if not getattr(tab, "fullscreen_active", False):
            return False
        monitors = enumerate_monitors()
        actual = self._current_monitor_index(monitors)
        if actual is not None and actual != expected_index:
            append_log = getattr(tab, "_append_log", None)
            if callable(append_log):
                append_log(
                    "[FedTermHub] Il compositor non ha spostato la finestra sul monitor "
                    f"richiesto ({expected_index + 1}); monitor attuale: {actual + 1}.\n"
                )
        return False

    def _open_native_rdp_fullscreen_and_start(self, tab, selector: str) -> bool:
        if NativeRdpTab is None or not isinstance(tab, NativeRdpTab):
            return False
        self.set_native_rdp_fullscreen(tab, True, selector)
        GLib.timeout_add(420, tab.request_start)
        return False

    def set_native_rdp_fullscreen(
        self, tab, enabled: bool, monitor_selector: str = "current"
    ) -> None:
        """Enter/leave an immersive RDP-only workspace on a chosen monitor.

        GTK 3 exposes fullscreen_on_monitor as a request to the compositor. The
        selected monitor is resolved again at launch, so disconnected outputs
        fall back safely instead of preventing the RDP session from opening.
        """
        if NativeRdpTab is None or not isinstance(tab, NativeRdpTab):
            return
        if enabled:
            page_num = self.notebook.page_num(tab)
            if page_num < 0:
                return
            if self._rdp_immersive_tab is not None and self._rdp_immersive_tab is not tab:
                try:
                    self._rdp_immersive_tab.set_fullscreen_state(False)
                except Exception:
                    pass
            self.notebook.set_current_page(page_num)
            self._rdp_immersive_tab = tab
            self._notebook_tabs_before_immersive = self.notebook.get_show_tabs()
            for widget in (self.menubar, self.ribbon, self.rail, self.sidebar, self.tabbar_hint, self.footer):
                widget.hide()
            self.notification_revealer.hide()
            self.notebook.set_show_tabs(False)
            self._fullscreen = True
            selector = str(monitor_selector or getattr(tab, "preferred_monitor", "current"))
            tab.preferred_monitor = selector
            descriptor, exact = self._resolve_rdp_fullscreen_monitor(selector)
            requested_label = "monitor corrente"
            if descriptor is not None:
                requested_label = descriptor.label
            try:
                if descriptor is not None and hasattr(self, "fullscreen_on_monitor"):
                    self.fullscreen_on_monitor(self.get_screen(), descriptor.index)
                else:
                    self.fullscreen()
            except Exception:
                self.fullscreen()
                exact = False
            tab.set_fullscreen_state(True)
            append_log = getattr(tab, "_append_log", None)
            if callable(append_log):
                if descriptor is not None:
                    append_log(
                        f"[FedTermHub] Schermo intero richiesto su {descriptor.label}."
                        + ("\n" if exact else " Fallback applicato perché il monitor salvato non è disponibile.\n")
                    )
                else:
                    append_log("[FedTermHub] Monitor non enumerabile: schermo intero automatico.\n")
            fallback_note = "" if exact else " — fallback monitor applicato"
            self.set_status(
                f"RDP a schermo intero su {requested_label}{fallback_note} — "
                "porta il mouse in alto oppure premi F11/Esc per uscire"
            )
            if descriptor is not None:
                GLib.timeout_add(850, self._verify_rdp_fullscreen_monitor, tab, descriptor.index)
            GLib.timeout_add(500, lambda: (tab.canvas.grab_focus(), False)[1])
        else:
            active = self._rdp_immersive_tab
            self._rdp_immersive_tab = None
            self._fullscreen = False
            self.unfullscreen()
            self.menubar.show_all()
            self.ribbon.show_all()
            self.tabbar_hint.show_all()
            self.footer.show_all()
            self.apply_layout_preferences()
            self.notification_revealer.show()
            self.notebook.set_show_tabs(self._notebook_tabs_before_immersive)
            target = active if active is not None else tab
            try:
                target.set_fullscreen_state(False)
            except Exception:
                pass
            self.set_status("Sessione RDP ripristinata in finestra")
            GLib.timeout_add(420, lambda: (target.canvas.grab_focus(), False)[1] if target else False)

    def set_terminal_fullscreen(self, tab, enabled: bool) -> None:
        """Workspace immersivo per le schede terminale (SSH, seriale, locale).

        Riusa lo stesso meccanismo delle sessioni RDP: nasconde menu, ribbon,
        sidebar e barra schede, poi porta la finestra a schermo intero. F11
        (o la voce del menu contestuale) alterna lo stato.
        """
        if not isinstance(tab, TerminalTab):
            return
        if enabled:
            page_num = self.notebook.page_num(tab)
            if page_num < 0:
                return
            self.notebook.set_current_page(page_num)
            self._notebook_tabs_before_immersive = self.notebook.get_show_tabs()
            for widget in (self.menubar, self.ribbon, self.rail, self.sidebar, self.tabbar_hint, self.footer):
                widget.hide()
            self.notification_revealer.hide()
            self.notebook.set_show_tabs(False)
            self._fullscreen = True
            try:
                self.fullscreen()
            except Exception:
                pass
            tab.fullscreen_active = True
            self.set_status("Terminale a schermo intero — premi F11 per uscire")
            GLib.timeout_add(300, lambda: (tab.terminal.grab_focus(), False)[1])
        else:
            self._fullscreen = False
            try:
                self.unfullscreen()
            except Exception:
                pass
            self.menubar.show_all()
            self.ribbon.show_all()
            self.tabbar_hint.show_all()
            self.footer.show_all()
            self.apply_layout_preferences()
            self.notification_revealer.show()
            self.notebook.set_show_tabs(self._notebook_tabs_before_immersive)
            tab.fullscreen_active = False
            self.set_status("Terminale ripristinato in finestra")
            GLib.timeout_add(300, lambda: (tab.terminal.grab_focus(), False)[1])

    def close_specific_tab(self, widget: Gtk.Widget) -> None:
        if NativeRdpTab is not None and isinstance(widget, NativeRdpTab) and getattr(widget, "fullscreen_active", False):
            self.set_native_rdp_fullscreen(widget, False)
        if isinstance(widget, TerminalTab) and getattr(widget, "fullscreen_active", False):
            self.set_terminal_fullscreen(widget, False)
        page_num = self.notebook.page_num(widget)
        if widget in self.terminal_tabs:
            # Una scheda chiusa non deve piu' ricevere i comandi di MultiExec.
            self.terminal_tabs.remove(widget)
        if page_num < 0:
            return
        try:
            stop_method = getattr(widget, "stop", None)
            if callable(stop_method):
                if NativeRdpTab is not None and isinstance(widget, NativeRdpTab):
                    stop_method(silent=True)
                else:
                    stop_method()
        except Exception:
            pass
        self.notebook.remove_page(page_num)
        try:
            widget.destroy()
        except Exception:
            pass
        self.set_status("Scheda chiusa")

    def _on_terminal_finished(self, workspace, pane, explicit: bool, policy: str) -> None:  # noqa: ANN001
        """L'ultimo riquadro di una scheda terminale e' finito o l'utente ha chiesto di chiuderlo."""
        status = getattr(pane, "exit_status", None)
        if explicit or policy == "always" or (policy == "on_success" and status == 0):
            GLib.idle_add(lambda: (self.close_specific_tab(workspace), False)[1])

    def current_rdp_tab(self) -> Optional[RdpTab]:
        try:
            page = self.notebook.get_nth_page(self.notebook.get_current_page())
            return page if isinstance(page, RdpTab) else None
        except Exception:
            return None

    def close_current_tab(self) -> None:
        page_num = self.notebook.get_current_page()
        if page_num < 0:
            return
        self.close_specific_tab(self.notebook.get_nth_page(page_num))

    def _terminal_has_focus(self) -> bool:
        """True se il widget con il fuoco della tastiera e' un terminale VTE."""
        try:
            return isinstance(self.get_focus(), Vte.Terminal)
        except Exception:
            return False

    def on_global_key_press(self, _window: Gtk.Window, event: Gdk.EventKey) -> bool:
        self.mark_user_activity()
        try:
            current_page = self.notebook.get_nth_page(self.notebook.get_current_page())
        except Exception:
            current_page = None
        if NativeRdpTab is not None and isinstance(current_page, NativeRdpTab):
            if event.keyval == Gdk.KEY_F11:
                self.set_native_rdp_fullscreen(
                    current_page, not current_page.fullscreen_active,
                    getattr(current_page, "preferred_monitor", "current"),
                )
                return True
            if current_page.fullscreen_active and event.keyval == Gdk.KEY_Escape:
                self.set_native_rdp_fullscreen(current_page, False)
                return True
        rdp = self.current_rdp_tab()
        if rdp is not None and rdp.keyboard_active:
            return False
        state = event.state & Gtk.accelerator_get_default_mod_mask()
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        key = Gdk.keyval_name(event.keyval) or ""
        # Con il fuoco nel terminale, Ctrl+lettera appartiene al terminale.
        # In GTK3 questo gestore della finestra viene chiamato PRIMA del widget
        # che ha il fuoco: senza questo controllo Ctrl+W chiudeva la scheda
        # invece di cancellare una parola (bash) o cercare (nano), e Ctrl+K,
        # Ctrl+L, Ctrl+T, Ctrl+B (il prefisso di tmux) non arrivavano mai al
        # terminale. Verificato inviando tasti veri con xdotool sotto Xvfb.
        # Ctrl+Shift+V incolla nel terminale (lo gestisce TerminalPane) invece
        # di aprire il gestore password. Le scorciatoie dell'applicazione
        # restano attive quando il fuoco e' altrove e da menu e barra comandi:
        # e' la stessa convenzione di GNOME Terminal.
        if ctrl and self._terminal_has_focus():
            if not shift:
                return False
            if key.lower() == "v":
                return False
        if event.keyval == Gdk.KEY_F11:
            self._fullscreen = not self._fullscreen
            self.fullscreen() if self._fullscreen else self.unfullscreen()
            return True
        if ctrl and shift and key.lower() == "n":
            self.on_add_session(None)
            return True
        # Ctrl+H sui file remoti: convenzione dei file manager. Vale solo
        # quando la sidebar dei file remoti e' quella in primo piano,
        # altrimenti ruberebbe la scorciatoia a chi sta lavorando altrove.
        if ctrl and not shift and key.lower() == "h":
            sidebar = getattr(self, "remote_files_sidebar", None)
            if (
                sidebar is not None
                and self.sidebar_notebook.get_current_page() == self.remote_files_page_num
            ):
                sidebar.toggle_hidden()
                return True
        if ctrl and not shift and key.lower() == "k":
            self.open_command_palette()
            return True
        if ctrl and shift and key.lower() == "t":
            self.on_tunnel_manager(None)
            return True
        # La vecchia Command Palette è stata rimossa dall'HUD: in 3.7
        # le azioni sono organizzate sotto i rispettivi menu padre.
        if ctrl and shift and key.lower() == "v":
            self.on_password_manager(None)
            return True
        if ctrl and not shift and key in {"comma", ","}:
            self.show_settings_dialog()
            return True
        if ctrl and not shift and key.lower() == "t":
            self.open_local_terminal()
            return True
        if ctrl and not shift and key.lower() == "l":
            if not self.sidebar.get_visible():
                self.toggle_sidebar(True)
            self.quick.grab_focus()
            return True
        if ctrl and not shift and key.lower() == "b":
            self.toggle_sidebar()
            return True
        if ctrl and not shift and key.lower() == "w":
            self.close_current_tab()
            return True
        return False

    def on_window_configure(self, _window: Gtk.Window, _event) -> bool:  # noqa: ANN001
        if self._save_settings_source_id is None:
            self._save_settings_source_id = GLib.timeout_add(700, self.save_window_settings_debounced)
        return False

    def save_window_settings_debounced(self) -> bool:
        self._save_settings_source_id = None
        self.save_window_settings()
        return False

    def save_window_settings(self) -> None:
        try:
            width, height = self.get_size()
            self.settings.window_width = int(width)
            self.settings.window_height = int(height)
            if hasattr(self, "paned") and getattr(self, "sidebar", None) is not None and self.sidebar.get_visible():
                self.settings.sidebar_width = max(190, int(self.paned.get_position()))
            self.settings.save(SETTINGS_FILE)
        except Exception:
            pass

    def on_window_delete(self, *_args) -> bool:
        if self.settings.confirm_close_active_sessions and not self._closing_confirmed:
            active_pages = 0
            try:
                for index in range(self.notebook.get_n_pages()):
                    page = self.notebook.get_nth_page(index)
                    if isinstance(page, TerminalTab):
                        if page.title != "Locale":
                            active_pages += 1
                        continue
                    if isinstance(page, SplitTerminalTab):
                        active_pages += 1
                        continue
                    if NativeRdpTab is not None and isinstance(page, NativeRdpTab):
                        active_pages += 1
                        continue
                    if isinstance(page, RdpTab):
                        active_pages += 1
            except Exception:
                pass
            active_detached_rdp = len(self.detached_rdp_windows)
            active_tunnels = sum(1 for proc in self.tunnel_processes.values() if proc.poll() is None)
            active_labs = self.lab_controller.active_count() if self.lab_controller is not None else 0
            if active_pages or active_detached_rdp or active_tunnels or active_labs:
                dialog = Gtk.MessageDialog(
                    transient_for=self,
                    flags=0,
                    message_type=Gtk.MessageType.QUESTION,
                    buttons=Gtk.ButtonsType.NONE,
                    text="Chiudere FedTermHub?",
                )
                dialog.add_buttons("Annulla", Gtk.ResponseType.CANCEL, "Chiudi sessioni", Gtk.ResponseType.OK)
                details = []
                if active_pages:
                    details.append(f"{active_pages} schede operative")
                if active_detached_rdp:
                    details.append(f"{active_detached_rdp} finestre RDP indipendenti")
                if active_tunnels:
                    details.append(f"{active_tunnels} tunnel SSH attivi")
                if active_labs:
                    details.append(f"{active_labs} laboratori temporanei con modifiche non salvate")
                dialog.format_secondary_text("Verranno terminati: " + ", ".join(details) + ".")
                response = dialog.run()
                dialog.destroy()
                if response != Gtk.ResponseType.OK:
                    return True
                self._closing_confirmed = True

        self.save_window_settings()
        for window in list(self.detached_rdp_windows):
            try:
                window.close_session()
            except Exception:
                pass
        for tunnel_id in list(self.tunnel_processes.keys()):
            self.stop_tunnel(tunnel_id)
        try:
            for index in range(self.notebook.get_n_pages()):
                page = self.notebook.get_nth_page(index)
                stop_method = getattr(page, "stop", None)
                if callable(stop_method):
                    if NativeRdpTab is not None and isinstance(page, NativeRdpTab):
                        stop_method(silent=True)
                    else:
                        stop_method()
        except Exception:
            pass
        try:
            if self._security_timer_id:
                GLib.source_remove(self._security_timer_id)
                self._security_timer_id = None
        except Exception:
            pass
        if NETWORK_RUNTIME is not None:
            try:
                NETWORK_RUNTIME.stop_http()
                NETWORK_RUNTIME.stop_capture()
            except Exception:
                pass
        if self.lab_controller is not None:
            try:
                self.lab_controller.stop_all()
            except Exception:
                pass
        self.vault.lock()
        return False

    def set_status(self, text: str) -> None:
        self.status.set_text(text)


class FedTermHubApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self._icon_pixbufs = configure_default_app_icon()

    def do_activate(self) -> None:  # noqa: N802
        win = next((item for item in self.get_windows() if isinstance(item, AppWindow)), None)
        if win is None:
            win = AppWindow(self)
        win.show_all()
        win.apply_layout_preferences()
        win.present()


def main() -> int:
    if DOCTOR_MODE:
        doctor_ok = run_doctor_cli(None)
        return 0 if doctor_ok or not DOCTOR_STRICT else 1
    try:
        GLib.set_prgname(APP_ID)
        GLib.set_application_name(APP_NAME)
    except Exception:
        pass
    sys.argv = strip_private_args(sys.argv)
    app = FedTermHubApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
