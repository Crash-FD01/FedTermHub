# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Network administration toolbox for FedTermHub.

The module keeps all network tools in one consistent GTK window and executes
long-running commands outside the GTK thread. Privileged operations use
polkit/pkexec and always show their output inside the application.
"""
from __future__ import annotations

import atexit
import ipaddress
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tempfile
import uuid
from defusedxml import ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple
try:
    from .i18n import tr, trn
except ImportError:  # avvio come script (fedtermhub/fedtermhub.py) o dai test
    from i18n import tr, trn

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "fedtermhub"
CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
try:
    os.chmod(CONFIG_DIR, 0o700)
except OSError:
    pass
NETWORK_SHARES_FILE = CONFIG_DIR / "network_shares.json"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "fedtermhub"
CAPTURE_DIR = CACHE_DIR / "captures"
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)


def _run(args: Sequence[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), text=True, capture_output=True, timeout=timeout, check=False)


def _async(worker: Callable[[], object], done: Callable[[object, Optional[BaseException]], None]) -> None:
    def runner() -> None:
        try:
            result = worker()
            error: Optional[BaseException] = None
        except BaseException as exc:  # keep GTK alive even on command/parsing errors
            result, error = None, exc
        GLib.idle_add(lambda: (done(result, error), False)[1])

    threading.Thread(target=runner, daemon=True).start()


def _command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def _privileged_command(args: Sequence[str]) -> List[str]:
    if os.geteuid() == 0:
        return list(args)
    pkexec = shutil.which("pkexec")
    return [pkexec, *args] if pkexec else list(args)


def _run_privileged_script(lines: Sequence[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    """Esegue piu' comandi da amministratore con UNA sola richiesta di autorizzazione.

    pkexec chiede la password a ogni chiamata: applicare tre cartelle FTP ne chiedeva una decina di
    fila. Le righe sono gia' comandi di shell quotati (shlex.join) o costrutti di controllo.
    """
    script = tr("set -e\n") + "\n".join(lines) + "\n"
    return _run(_privileged_command(["sh", "-c", script]), timeout=timeout)


def _privileged_error(proc: subprocess.CompletedProcess[str]) -> str:
    if proc.returncode in (126, 127) and os.geteuid() != 0:
        return tr("Autorizzazione negata o annullata.")
    return (proc.stderr or proc.stdout or tr("exit {returncode}").format(returncode=proc.returncode)).strip()


def _split_endpoint(endpoint: str) -> Tuple[str, str]:
    endpoint = endpoint.strip()
    if endpoint.startswith("[") and "]:" in endpoint:
        host, port = endpoint[1:].rsplit("]:", 1)
        return host, port
    if endpoint.count(":") == 1:
        return tuple(endpoint.rsplit(":", 1))  # type: ignore[return-value]
    if ":" in endpoint:
        host, port = endpoint.rsplit(":", 1)
        return host, port
    return endpoint, ""


def _ss_process_text(raw: str) -> str:
    """'users:(("sshd",pid=812,fd=3),("sshd",pid=900,fd=3))' -> 'sshd (pid 812, 900)'.

    Prima veniva mostrato il testo grezzo di ss, con parentesi e fd. Senza privilegi ss non vede i
    processi degli altri utenti: la colonna resta vuota.
    """
    found: Dict[str, List[str]] = {}
    for name, pid in re.findall(r'\("([^"]+)",pid=(\d+)', raw or ""):
        found.setdefault(name, [])
        if pid not in found[name]:
            found[name].append(pid)
    return ", ".join(tr("{name} (pid {join_pids})").format(name=name, join_pids=', '.join(pids)) for name, pids in found.items())


def _interfaces() -> List[str]:
    try:
        raw = _run(["ip", "-j", "link", "show"], timeout=5)
        if raw.returncode == 0:
            return [item.get("ifname", "") for item in json.loads(raw.stdout) if item.get("ifname")]
    except Exception:
        pass
    root = Path("/sys/class/net")
    return sorted(item.name for item in root.iterdir()) if root.exists() else ["any"]


def _detected_networks() -> List[str]:
    networks: List[str] = []
    try:
        raw = _run(["ip", "-j", "address", "show", "up"], timeout=5)
        if raw.returncode == 0:
            for iface in json.loads(raw.stdout):
                if iface.get("ifname") == "lo":
                    continue
                for info in iface.get("addr_info", []):
                    if info.get("family") != "inet" or not info.get("local"):
                        continue
                    address = f"{info['local']}/{info.get('prefixlen', 24)}"
                    try:
                        network = str(ipaddress.ip_interface(address).network)
                    except ValueError:
                        continue
                    if network not in networks:
                        networks.append(network)
    except Exception:
        pass
    return networks or ["192.168.1.0/24"]





def _load_network_shares() -> Dict[str, List[Dict[str, str]]]:
    default: Dict[str, List[Dict[str, str]]] = {"ftp": [], "sftp": []}
    try:
        raw = json.loads(NETWORK_SHARES_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return default
        for key in ("ftp", "sftp"):
            rows = raw.get(key, [])
            if isinstance(rows, list):
                default[key] = [
                    {
                        "name": str(item.get("name", "")).strip(),
                        "path": str(item.get("path", "")).strip(),
                        "mode": str(item.get("mode", "rw" if key == "ftp" else "system")).strip(),
                    }
                    for item in rows
                    if isinstance(item, dict) and str(item.get("name", "")).strip() and str(item.get("path", "")).strip()
                ]
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return default


def _save_network_shares(data: Dict[str, List[Dict[str, str]]]) -> None:
    NETWORK_SHARES_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(NETWORK_SHARES_FILE.parent, 0o700)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{NETWORK_SHARES_FILE.name}.",
        suffix=".tmp",
        dir=NETWORK_SHARES_FILE.parent,
    )
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, NETWORK_SHARES_FILE)
        os.chmod(NETWORK_SHARES_FILE, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _safe_share_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._ -]+", "_", value.strip()).strip(" .")
    return value[:64] or tr("Condivisione")


def _is_mountpoint(path: Path) -> bool:
    try:
        return _run(["findmnt", "-rn", "--target", str(path)], timeout=4).returncode == 0
    except Exception:
        return False

def _primary_ipv4() -> str:
    try:
        raw = _run(["ip", "-j", "route", "get", "1.1.1.1"], timeout=5)
        if raw.returncode == 0:
            rows = json.loads(raw.stdout)
            if rows and rows[0].get("prefsrc"):
                return str(rows[0]["prefsrc"])
    except Exception:
        pass
    return "127.0.0.1"


def _open_path(path: Path) -> None:
    Gio.AppInfo.launch_default_for_uri(path.resolve().as_uri(), None)


def _copy(text: str) -> None:
    Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)


class ToolPage(Gtk.Box):
    def __init__(self, title: str, subtitle: str, icon: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=12)
        self.get_style_context().add_class("network-tool-page")
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        image = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.DIALOG)
        image.set_pixel_size(36)
        header.pack_start(image, False, False, 0)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        heading = Gtk.Label(label=title, xalign=0)
        heading.get_style_context().add_class("tool-heading")
        sub = Gtk.Label(label=subtitle, xalign=0)
        sub.set_line_wrap(True)
        sub.get_style_context().add_class("muted")
        text.pack_start(heading, False, False, 0)
        text.pack_start(sub, False, False, 0)
        header.pack_start(text, True, True, 0)
        self.pack_start(header, False, False, 0)

    def card(self) -> Gtk.Frame:
        frame = Gtk.Frame()
        frame.get_style_context().add_class("network-card")
        return frame

    @staticmethod
    def button(label: str, icon: str = "") -> Gtk.Button:
        button = Gtk.Button()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        if icon:
            image = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            box.pack_start(image, False, False, 0)
        box.pack_start(Gtk.Label(label=label), False, False, 0)
        button.add(box)
        return button

    @staticmethod
    def message(parent: Gtk.Window, title: str, message: str, error: bool = False) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=parent,
            modal=True,
            message_type=Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()


@dataclass
class ServiceDefinition:
    name: str
    unit: str
    description: str
    port: str
    package: str
    firewall_services: Tuple[str, ...]
    insecure: bool = False


class NetworkRuntime:
    def __init__(self) -> None:
        self.http_process: Optional[subprocess.Popen[str]] = None
        self.http_description = ""
        self.vnc_process: Optional[subprocess.Popen[str]] = None
        self.vnc_description = ""
        self.vnc_log: List[str] = []
        self.capture_process: Optional[subprocess.Popen[str]] = None
        self.capture_root_pid: Optional[int] = None
        self.capture_pidfile: Optional[Path] = None
        self.capture_file: Optional[Path] = None
        self.capture_timer: Optional[threading.Timer] = None

    @staticmethod
    def _stop_process(proc: Optional[subprocess.Popen[str]]) -> None:
        if not proc or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def stop_http(self) -> None:
        proc = self.http_process
        self.http_process = None
        self._stop_process(proc)
        self.http_description = ""

    def stop_vnc(self) -> None:
        proc = self.vnc_process
        self.vnc_process = None
        self._stop_process(proc)
        self.vnc_description = ""

    def stop_capture(self) -> None:
        if self.capture_timer:
            self.capture_timer.cancel()
            self.capture_timer = None
        pid = self.capture_root_pid
        if not pid and self.capture_pidfile and self.capture_pidfile.exists():
            try:
                pid = int(self.capture_pidfile.read_text().strip())
            except Exception:
                pid = None
        if pid:
            # tcpdump gira con -Z utente: lascia i privilegi dopo aver aperto l'interfaccia, quindi
            # di norma l'utente puo' fermarlo da se'. Prima si passava sempre da pkexec e fermare la
            # cattura chiedeva di nuovo la password di amministratore.
            try:
                os.kill(pid, signal.SIGINT)
            except PermissionError:
                try:
                    subprocess.Popen(_privileged_command(["kill", "-INT", str(pid)]))
                except Exception:
                    pass
            except (ProcessLookupError, OSError):
                pass
        proc = self.capture_process
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                pass
        self.capture_process = None
        self.capture_root_pid = None


RUNTIME = NetworkRuntime()
atexit.register(RUNTIME.stop_http)
atexit.register(RUNTIME.stop_vnc)
atexit.register(RUNTIME.stop_capture)


class ServicesPage(ToolPage):
    SERVICES = [
        ServiceDefinition(
            tr("SSH / SFTP"),
            "sshd.service",
            tr("Shell sicura e trasferimento file SFTP"),
            "22/tcp",
            "openssh-server",
            ("ssh",),
        ),
        ServiceDefinition(
            "FTP",
            "vsftpd.service",
            tr("Server FTP per utenti locali"),
            "21/tcp",
            "vsftpd",
            ("ftp",),
        ),
        ServiceDefinition(
            "TFTP",
            "tftp.socket",
            tr("Trasferimenti semplici, PXE e apparati di rete"),
            "69/udp",
            "tftp-server",
            ("tftp",),
            insecure=True,
        ),
        ServiceDefinition(
            tr("Telnet"),
            "telnet.socket",
            tr("Accesso remoto legacy senza cifratura"),
            "23/tcp",
            "telnet-server",
            ("telnet",),
            insecure=True,
        ),
        ServiceDefinition(
            "NFS",
            "nfs-server.service",
            tr("Condivisione cartelle per sistemi Linux/Unix"),
            "2049/tcp+udp",
            "nfs-utils",
            ("nfs", "mountd", "rpc-bind"),
        ),
    ]

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(
            tr("Server locali"),
            tr("Avvia servizi, pubblica cartelle e controlla lo stato da un'unica schermata."),
            "network-server-symbolic",
        )
        self.parent = parent
        self.status_labels: Dict[str, Gtk.Label] = {}
        self.package_labels: Dict[str, Gtk.Label] = {}
        self.install_buttons: Dict[str, Gtk.Button] = {}
        self.service_action_buttons: Dict[str, Dict[str, Gtk.Button]] = {}
        self.shares = _load_network_shares()
        self.ftp_store = Gtk.ListStore(str, str, str, str)
        self.sftp_store = Gtk.ListStore(str, str, str, str)

        shell = self.card()
        shell_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        shell.add(shell_box)
        self.local_stack = Gtk.Stack()
        self.local_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.local_stack.set_transition_duration(140)
        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.local_stack)
        switcher.set_halign(Gtk.Align.START)
        switcher.get_style_context().add_class("local-services-switcher")
        shell_box.pack_start(switcher, False, False, 0)
        shell_box.pack_start(self.local_stack, True, True, 0)
        self.pack_start(shell, True, True, 0)

        for name, title, page in (
            ("system", tr("Servizi"), self._build_system_services_tab()),
            ("shares", tr("FTP / SFTP"), self._build_file_shares_tab()),
            ("http", "HTTP", self._build_http_tab()),
            ("nfs", "NFS", self._build_nfs_tab()),
            ("vnc", "VNC", self._build_vnc_tab()),
        ):
            self.local_stack.add_titled(page, name, title)
        self.local_stack.set_visible_child_name("system")

        self.refresh_share_views()
        self.refresh_status()
        self._status_timer = GLib.timeout_add_seconds(4, self.refresh_status)
        self.connect("destroy", self._on_destroy)

    def _build_system_services_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=10)
        intro = Gtk.Label(
            label=tr("I pacchetti già presenti sono evidenziati in verde. Le operazioni amministrative richiedono Polkit."),
            xalign=0,
        )
        intro.set_line_wrap(True)
        intro.get_style_context().add_class("muted")
        outer.pack_start(intro, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(390)
        viewport = Gtk.Viewport()
        viewport.set_shadow_type(Gtk.ShadowType.NONE)
        scroll.add(viewport)
        service_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=2)
        viewport.add(service_list)

        icon_map = {
            "sshd.service": "network-server-symbolic",
            "vsftpd.service": "folder-remote-symbolic",
            "tftp.socket": "network-transmit-receive-symbolic",
            "telnet.socket": "utilities-terminal-symbolic",
            "nfs-server.service": "folder-publicshare-symbolic",
        }
        for service in self.SERVICES:
            frame = self.card()
            frame.get_style_context().add_class("service-card")
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7, margin=10)
            frame.add(body)

            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
            image = Gtk.Image.new_from_icon_name(icon_map.get(service.unit, "network-server-symbolic"), Gtk.IconSize.BUTTON)
            image.set_pixel_size(24)
            header.pack_start(image, False, False, 0)
            titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            name = Gtk.Label(label=service.name, xalign=0)
            name.get_style_context().add_class("service-title")
            detail = Gtk.Label(label=f"{service.description}  •  {service.port}", xalign=0)
            detail.set_line_wrap(True)
            detail.get_style_context().add_class("muted")
            titles.pack_start(name, False, False, 0)
            titles.pack_start(detail, False, False, 0)
            header.pack_start(titles, True, True, 0)

            package_badge = Gtk.Label(label=tr("Controllo…"))
            package_badge.get_style_context().add_class("service-badge")
            package_badge.set_tooltip_text(tr("Pacchetto Fedora: {package}").format(package=service.package))
            self.package_labels[service.unit] = package_badge
            state_badge = Gtk.Label(label=tr("Controllo…"))
            state_badge.get_style_context().add_class("service-badge")
            self.status_labels[service.unit] = state_badge
            header.pack_start(package_badge, False, False, 0)
            header.pack_start(state_badge, False, False, 0)
            if service.insecure:
                warning = Gtk.Image.new_from_icon_name("dialog-warning-symbolic", Gtk.IconSize.MENU)
                warning.set_tooltip_text(tr("Protocollo non cifrato: usalo soltanto su reti fidate."))
                header.pack_start(warning, False, False, 0)
            body.pack_start(header, False, False, 0)

            actions = Gtk.FlowBox()
            actions.set_selection_mode(Gtk.SelectionMode.NONE)
            actions.set_column_spacing(6)
            actions.set_row_spacing(5)
            actions.set_max_children_per_line(5)
            action_map: Dict[str, Gtk.Button] = {}
            for label, icon, action in (
                (tr("Installa"), "system-software-install-symbolic", "install"),
                (tr("Avvia"), "media-playback-start-symbolic", "start"),
                (tr("Ferma"), "media-playback-stop-symbolic", "stop"),
                (tr("Abilita all'avvio"), "emblem-default-symbolic", "enable"),
                (tr("Apri firewall"), "security-high-symbolic", "firewall"),
            ):
                button = self.button(label, icon)
                button.get_style_context().add_class("service-action")
                button.connect("clicked", self.on_service_action, service, action)
                actions.add(button)
                action_map[action] = button
                if action == "install":
                    self.install_buttons[service.unit] = button
            self.service_action_buttons[service.unit] = action_map
            body.pack_start(actions, False, False, 0)
            service_list.pack_start(frame, False, False, 0)

        outer.pack_start(scroll, True, True, 0)

        tftp_card = self.card()
        tftp_card.get_style_context().add_class("service-card")
        tftp_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin=10)
        tftp_card.add(tftp_box)
        tftp_text = Gtk.Label(label=tr("Directory TFTP: /var/lib/tftpboot"), xalign=0)
        tftp_text.set_line_wrap(True)
        tftp_box.pack_start(tftp_text, True, True, 0)
        prepare = self.button(tr("Prepara"), "folder-new-symbolic")
        prepare.connect("clicked", self.prepare_tftp_directory)
        open_dir = self.button(tr("Apri"), "folder-open-symbolic")
        open_dir.connect("clicked", self.open_tftp_directory)
        tftp_box.pack_start(prepare, False, False, 0)
        tftp_box.pack_start(open_dir, False, False, 0)
        outer.pack_start(tftp_card, False, False, 0)
        return outer

    def _build_file_shares_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=10)
        note = Gtk.Label(
            label=(
                tr("Seleziona le cartelle da esporre. SFTP crea collegamenti in ~/FedTermHub-SFTP e mantiene i permessi Linux. "
                "FTP usa ~/FedTermHub-FTP e bind mount gestiti, quindi può condividere anche cartelle esterne alla home.")
            ),
            xalign=0,
        )
        note.set_line_wrap(True)
        note.get_style_context().add_class("muted")
        outer.pack_start(note, False, False, 0)

        columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        columns.set_homogeneous(True)
        columns.pack_start(self._share_protocol_card("ftp", self.ftp_store), True, True, 0)
        columns.pack_start(self._share_protocol_card("sftp", self.sftp_store), True, True, 0)
        outer.pack_start(columns, True, True, 0)

        cfg = Gtk.Label(label=tr("Configurazione locale: {NETWORK_SHARES_FILE}").format(NETWORK_SHARES_FILE=NETWORK_SHARES_FILE), xalign=0)
        cfg.get_style_context().add_class("muted")
        outer.pack_start(cfg, False, False, 0)
        return outer

    def _share_protocol_card(self, protocol: str, store: Gtk.ListStore) -> Gtk.Widget:
        is_ftp = protocol == "ftp"
        frame = self.card()
        frame.get_style_context().add_class("share-card")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)
        frame.add(body)
        title = Gtk.Label(label=tr("Condivisioni FTP") if is_ftp else tr("Condivisioni SFTP"), xalign=0)
        title.get_style_context().add_class("section-heading")
        body.pack_start(title, False, False, 0)
        description = Gtk.Label(
            label=(
                tr("Radice: ~/FedTermHub-FTP. Applica crea i mount e configura la directory iniziale di vsftpd per l'utente corrente.")
                if is_ftp else
                tr("Radice: ~/FedTermHub-SFTP. I collegamenti sono immediatamente visibili via SFTP e seguono i permessi del filesystem.")
            ),
            xalign=0,
        )
        description.set_line_wrap(True)
        description.get_style_context().add_class("muted")
        body.pack_start(description, False, False, 0)

        tree = Gtk.TreeView(model=store)
        tree.set_headers_visible(True)
        tree.set_grid_lines(Gtk.TreeViewGridLines.HORIZONTAL)
        tree.set_size_request(-1, 225)
        tree.get_style_context().add_class("share-list")
        for idx, heading in enumerate((tr("Nome"), tr("Cartella"), tr("Accesso"), tr("Stato"))):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE if idx == 1 else Pango.EllipsizeMode.END)
            renderer.set_property("ypad", 5)
            column = Gtk.TreeViewColumn(heading, renderer, text=idx)
            column.set_resizable(True)
            column.set_expand(idx == 1)
            tree.append_column(column)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(tree)
        body.pack_start(scroll, True, True, 0)

        actions = Gtk.FlowBox()
        actions.set_selection_mode(Gtk.SelectionMode.NONE)
        actions.set_column_spacing(6)
        actions.set_row_spacing(5)
        add = self.button(tr("Aggiungi"), "list-add-symbolic")
        remove = self.button(tr("Rimuovi"), "list-remove-symbolic")
        open_root = self.button(tr("Apri radice"), "folder-open-symbolic")
        apply = self.button(tr("Applica FTP"), "emblem-default-symbolic") if is_ftp else self.button(tr("Aggiorna collegamenti"), "view-refresh-symbolic")
        add.connect("clicked", lambda _b, proto=protocol: self.add_share(proto))
        remove.connect("clicked", lambda _b, proto=protocol, view=tree: self.remove_share(proto, view))
        open_root.connect("clicked", lambda _b, proto=protocol: self.open_share_root(proto))
        apply.connect("clicked", self.apply_ftp_shares if is_ftp else self.apply_sftp_shares)
        for button in (add, remove, open_root, apply):
            actions.add(button)
        body.pack_start(actions, False, False, 0)

        status = Gtk.Label(label="", xalign=0)
        status.set_line_wrap(True)
        status.get_style_context().add_class("share-status")
        body.pack_start(status, False, False, 0)
        if is_ftp:
            self.ftp_tree = tree
            self.ftp_apply_status = status
        else:
            self.sftp_tree = tree
            self.sftp_apply_status = status
        return frame

    def _share_root(self, protocol: str) -> Path:
        return Path.home() / ("FedTermHub-FTP" if protocol == "ftp" else "FedTermHub-SFTP")

    def refresh_share_views(self) -> None:
        for protocol, store in (("ftp", self.ftp_store), ("sftp", self.sftp_store)):
            store.clear()
            root = self._share_root(protocol)
            for item in self.shares.get(protocol, []):
                target = root / item["name"]
                if protocol == "ftp":
                    state = tr("Applicata") if _is_mountpoint(target) else tr("Da applicare")
                    mode = tr("Sola lettura") if item.get("mode") == "ro" else tr("Lettura/scrittura")
                else:
                    state = tr("Pronta") if target.is_symlink() and target.exists() else tr("Da aggiornare")
                    mode = tr("Permessi sistema")
                store.append([item["name"], item["path"], mode, state])

    def add_share(self, protocol: str) -> None:
        dialog = Gtk.Dialog(title=tr("Aggiungi cartella FTP") if protocol == "ftp" else tr("Aggiungi cartella SFTP"), transient_for=self.parent, modal=True)
        dialog.add_buttons(tr("Annulla"), Gtk.ResponseType.CANCEL, tr("Aggiungi"), Gtk.ResponseType.OK)
        dialog.set_default_size(560, 250)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=14)
        dialog.get_content_area().add(box)
        chooser = Gtk.FileChooserButton(title=tr("Cartella da condividere"), action=Gtk.FileChooserAction.SELECT_FOLDER)
        chooser.set_filename(str(Path.home()))
        name = Gtk.Entry()
        name.set_placeholder_text(tr("Nome mostrato al client"))
        mode = Gtk.ComboBoxText()
        mode.append("rw", tr("Lettura e scrittura"))
        mode.append("ro", tr("Sola lettura"))
        mode.set_active_id("rw")
        grid = Gtk.Grid(column_spacing=10, row_spacing=9)
        for row, (label, widget) in enumerate(((tr("Cartella"), chooser), (tr("Nome"), name))):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
        if protocol == "ftp":
            grid.attach(Gtk.Label(label=tr("Accesso"), xalign=0), 0, 2, 1, 1)
            grid.attach(mode, 1, 2, 1, 1)
        box.pack_start(grid, True, True, 0)
        dialog.show_all()
        try:
            if dialog.run() != Gtk.ResponseType.OK:
                return
            source = Path(chooser.get_filename() or "")
            if not source.is_dir():
                self.message(self.parent, tr("Cartella non valida"), tr("Seleziona una cartella esistente."), True)
                return
            share_name = _safe_share_name(name.get_text() or source.name)
            if any(row.get("name") == share_name for row in self.shares.get(protocol, [])):
                self.message(self.parent, tr("Nome già presente"), tr("Scegli un nome differente per la condivisione."), True)
                return
            self.shares.setdefault(protocol, []).append({
                "name": share_name,
                "path": str(source.resolve()),
                "mode": (mode.get_active_id() or "rw") if protocol == "ftp" else "system",
            })
            _save_network_shares(self.shares)
            if protocol == "sftp":
                self.apply_sftp_shares(None)
            else:
                self.refresh_share_views()
        finally:
            dialog.destroy()

    def remove_share(self, protocol: str, tree: Gtk.TreeView) -> None:
        model, tree_iter = tree.get_selection().get_selected()
        if tree_iter is None:
            return
        name = str(model[tree_iter][0])
        root = self._share_root(protocol)
        target = root / name
        if protocol == "ftp" and _is_mountpoint(target):
            proc = _run(_privileged_command(["umount", str(target)]), timeout=90)
            if proc.returncode != 0:
                self.message(self.parent, tr("Condivisione FTP occupata"), (proc.stderr or proc.stdout).strip() or tr("Impossibile smontare la cartella."), True)
                return
        elif protocol == "sftp" and target.is_symlink():
            target.unlink(missing_ok=True)
        self.shares[protocol] = [row for row in self.shares.get(protocol, []) if row.get("name") != name]
        _save_network_shares(self.shares)
        try:
            if target.exists() and target.is_dir() and not any(target.iterdir()):
                target.rmdir()
        except Exception:
            pass
        self.refresh_share_views()

    def open_share_root(self, protocol: str) -> None:
        root = self._share_root(protocol)
        root.mkdir(parents=True, exist_ok=True)
        _open_path(root)

    def apply_sftp_shares(self, _button: Optional[Gtk.Button]) -> None:
        root = self._share_root("sftp")
        root.mkdir(parents=True, exist_ok=True)
        desired = {row["name"]: Path(row["path"]) for row in self.shares.get("sftp", [])}
        try:
            for child in root.iterdir():
                if child.is_symlink() and child.name not in desired:
                    child.unlink()
            for name, source in desired.items():
                target = root / name
                if target.is_symlink() or target.exists():
                    if target.is_symlink() and target.resolve(strict=False) == source.resolve():
                        continue
                    if target.is_symlink():
                        target.unlink()
                    else:
                        raise RuntimeError(tr("{target} esiste già e non è un collegamento gestito.").format(target=target))
                target.symlink_to(source, target_is_directory=True)
            self.sftp_apply_status.set_text(tr("{n_desired} collegamenti SFTP aggiornati in {root}.").format(n_desired=len(desired), root=root))
        except Exception as exc:
            self.sftp_apply_status.set_text(tr("Errore: {exc}").format(exc=exc))
        self.refresh_share_views()

    def apply_ftp_shares(self, _button: Optional[Gtk.Button]) -> None:
        if not self._package_installed("vsftpd"):
            self.message(self.parent, tr("FTP non installato"), tr("Installa prima il pacchetto vsftpd dalla scheda Servizi."), True)
            return
        root = self._share_root("ftp")
        root.mkdir(parents=True, exist_ok=True)
        username = pwd.getpwuid(os.getuid()).pw_name
        shares = [dict(row) for row in self.shares.get("ftp", [])]
        self.ftp_apply_status.set_text(tr("Applicazione condivisioni FTP in corso…"))

        def worker() -> str:
            desired_names = {row["name"] for row in shares}
            lines: List[str] = []
            for child in root.iterdir():
                if child.name not in desired_names and _is_mountpoint(child):
                    lines.append(shlex.join(["umount", str(child)]))
            for row in shares:
                source = Path(row["path"])
                target = root / row["name"]
                if not source.is_dir():
                    raise RuntimeError(tr("Cartella sorgente non disponibile: {source}").format(source=source))
                target.mkdir(parents=True, exist_ok=True)
                quoted = shlex.quote(str(target))
                lines.append(tr("if mountpoint -q {quoted}; then umount {quoted}; fi").format(quoted=quoted))
                lines.append(shlex.join(["mount", "--bind", str(source), str(target)]))
                if row.get("mode") == "ro":
                    lines.append(shlex.join(["mount", "-o", "remount,bind,ro", str(target)]))

            conf_dir = CACHE_DIR / "vsftpd"
            conf_dir.mkdir(parents=True, exist_ok=True)
            user_conf = conf_dir / username
            user_conf.write_text(
                f"local_root={root}\nwrite_enable=YES\nchroot_local_user=YES\nallow_writeable_chroot=YES\n",
                encoding="utf-8",
            )
            os.chmod(user_conf, 0o600)
            # vsftpd.conf su Fedora e' leggibile solo da root: prima veniva letto da qui con i
            # permessi dell'utente e l'applicazione falliva sempre con "Permission denied".
            lines += [
                tr('conf=/etc/vsftpd/vsftpd.conf; [ -f "$conf" ] || conf=/etc/vsftpd.conf'),
                tr('[ -f "$conf" ] || { echo "Configurazione principale vsftpd non trovata." >&2; exit 3; }'),
                tr("dir=$(sed -n 's/^[[:space:]]*user_config_dir[[:space:]]*=[[:space:]]*//p' \"$conf\" | tail -n1)"),
                tr('if [ -z "$dir" ]; then dir=/etc/vsftpd/user_conf; '
                'printf "\\n# Gestito da FedTermHub\\nuser_config_dir=%s\\n" "$dir" >> "$conf"; fi'),
                tr("install -D -m 0644 {quote} \"$dir\"/{quote2}").format(quote=shlex.quote(str(user_conf)), quote2=shlex.quote(username)),
                tr("systemctl restart vsftpd.service"),
            ]
            proc = _run_privileged_script(lines, timeout=240)
            user_conf.unlink(missing_ok=True)
            if proc.returncode != 0:
                raise RuntimeError(_privileged_error(proc))
            return tr("{n_shares} cartelle FTP applicate. Radice utente: {root}").format(n_shares=len(shares), root=root)

        def done(result: object, error: Optional[BaseException]) -> None:
            self.ftp_apply_status.set_text(tr("Errore: {error}").format(error=error) if error else str(result))
            self.refresh_share_views()
            self.refresh_status()

        _async(worker, done)

    def _build_http_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=10)
        card = self.card()
        http_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=12)
        card.add(http_box)
        heading = Gtk.Label(label=tr("Server HTTP temporaneo"), xalign=0)
        heading.get_style_context().add_class("section-heading")
        http_box.pack_start(heading, False, False, 0)
        note = Gtk.Label(
            label=tr("Condividi rapidamente una cartella senza modificare servizi di sistema. Il server resta attivo finché FedTermHub rimane aperto."),
            xalign=0,
        )
        note.set_line_wrap(True)
        note.get_style_context().add_class("muted")
        http_box.pack_start(note, False, False, 0)
        form = Gtk.Grid(column_spacing=10, row_spacing=8)
        self.http_dir = Gtk.FileChooserButton(title=tr("Cartella da condividere"), action=Gtk.FileChooserAction.SELECT_FOLDER)
        # Non l'intera home: con "Rete locale" sarebbe visibile a tutta la rete. Si propone la
        # cartella Pubblici dell'utente, se c'e'.
        public = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PUBLIC_SHARE)
        self.http_dir.set_filename(public if public and Path(public).is_dir() else str(Path.home()))
        self.http_bind = Gtk.ComboBoxText()
        self.http_bind.append("127.0.0.1", tr("Solo questo computer"))
        self.http_bind.append("0.0.0.0", tr("Rete locale"))  # nosec B104
        self.http_bind.set_active_id("127.0.0.1")
        self.http_port = Gtk.SpinButton.new_with_range(1024, 65535, 1)
        self.http_port.set_value(8000)
        for row, (label, widget) in enumerate(((tr("Cartella"), self.http_dir), (tr("Ascolto"), self.http_bind), (tr("Porta"), self.http_port))):
            form.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            form.attach(widget, 1, row, 1, 1)
        http_box.pack_start(form, False, False, 0)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.http_start = self.button(tr("Avvia HTTP"), "media-playback-start-symbolic")
        self.http_stop = self.button(tr("Ferma"), "media-playback-stop-symbolic")
        self.http_copy = self.button(tr("Copia URL"), "edit-copy-symbolic")
        self.http_status = Gtk.Label(label=tr("Fermo"), xalign=0)
        self.http_start.connect("clicked", self.start_http)
        self.http_stop.connect("clicked", self.stop_http)
        self.http_copy.connect("clicked", lambda _b: _copy(RUNTIME.http_description))
        for button in (self.http_start, self.http_stop, self.http_copy):
            actions.pack_start(button, False, False, 0)
        actions.pack_start(self.http_status, True, True, 8)
        http_box.pack_start(actions, False, False, 0)
        outer.pack_start(card, False, False, 0)
        return outer

    def _build_nfs_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=10)
        card = self.card()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=12)
        card.add(box)
        heading = Gtk.Label(label=tr("Condivisione NFS"), xalign=0)
        heading.get_style_context().add_class("section-heading")
        box.pack_start(heading, False, False, 0)
        note = Gtk.Label(
            label=tr("Crea un export gestito da FedTermHub in /etc/exports.d/fedtermhub.exports. Limita sempre l'accesso alla subnet necessaria."),
            xalign=0,
        )
        note.set_line_wrap(True)
        note.get_style_context().add_class("muted")
        box.pack_start(note, False, False, 0)

        form = Gtk.Grid(column_spacing=10, row_spacing=8)
        self.nfs_dir = Gtk.FileChooserButton(title=tr("Cartella da esportare"), action=Gtk.FileChooserAction.SELECT_FOLDER)
        public_dir = Path.home() / tr("Public")
        self.nfs_dir.set_filename(str(public_dir if public_dir.is_dir() else Path.home()))
        self.nfs_clients = Gtk.ComboBoxText.new_with_entry()
        for network in _detected_networks():
            self.nfs_clients.append_text(network)
        self.nfs_clients.set_active(0)
        self.nfs_mode = Gtk.ComboBoxText()
        self.nfs_mode.append("ro", tr("Sola lettura"))
        self.nfs_mode.append("rw", tr("Lettura e scrittura"))
        self.nfs_mode.set_active_id("ro")
        self.nfs_root_squash = Gtk.CheckButton(label=tr("Proteggi root remoto con root_squash"))
        self.nfs_root_squash.set_active(True)
        for row, (label, widget) in enumerate(((tr("Cartella"), self.nfs_dir), (tr("Client autorizzati"), self.nfs_clients), (tr("Permessi"), self.nfs_mode))):
            form.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            form.attach(widget, 1, row, 1, 1)
        form.attach(self.nfs_root_squash, 1, 3, 1, 1)
        box.pack_start(form, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        apply_button = self.button(tr("Applica e avvia NFS"), "document-save-symbolic")
        remove_button = self.button(tr("Rimuovi export"), "edit-delete-symbolic")
        verify_button = self.button(tr("Verifica export"), "view-refresh-symbolic")
        firewall_button = self.button(tr("Apri firewall NFS"), "security-high-symbolic")
        apply_button.connect("clicked", self.apply_nfs_export)
        remove_button.connect("clicked", self.remove_nfs_export)
        verify_button.connect("clicked", self.verify_nfs_export)
        firewall_button.connect("clicked", lambda b: self.on_service_action(b, self.SERVICES[-1], "firewall"))
        for button in (apply_button, remove_button, verify_button, firewall_button):
            actions.pack_start(button, False, False, 0)
        box.pack_start(actions, False, False, 0)
        self.nfs_status = Gtk.Label(label=tr("Nessun export verificato."), xalign=0)
        self.nfs_status.set_line_wrap(True)
        self.nfs_status.set_selectable(True)
        self.nfs_status.get_style_context().add_class("service-output")
        box.pack_start(self.nfs_status, False, False, 0)
        outer.pack_start(card, False, False, 0)
        return outer

    def _build_vnc_tab(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=10)
        card = self.card()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=12)
        card.add(box)
        heading = Gtk.Label(label=tr("Condivisione desktop VNC"), xalign=0)
        heading.get_style_context().add_class("section-heading")
        box.pack_start(heading, False, False, 0)
        session_type = os.environ.get("XDG_SESSION_TYPE", "").lower() or "sconosciuta"
        note = Gtk.Label(
            label=tr("Sessione grafica rilevata: {session_type}. FedTermHub usa w0vncserver su Wayland e x0vncserver su X11.").format(session_type=session_type),
            xalign=0,
        )
        note.set_line_wrap(True)
        note.get_style_context().add_class("muted")
        box.pack_start(note, False, False, 0)

        form = Gtk.Grid(column_spacing=10, row_spacing=8)
        self.vnc_port = Gtk.SpinButton.new_with_range(5900, 5999, 1)
        self.vnc_port.set_value(5900)
        self.vnc_scope = Gtk.ComboBoxText()
        self.vnc_scope.append("local", tr("Solo localhost (consigliato con tunnel SSH)"))
        self.vnc_scope.append("network", tr("Rete locale"))
        self.vnc_scope.set_active_id("local")
        self.vnc_password = Gtk.Entry()
        self.vnc_password.set_visibility(False)
        self.vnc_password.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.vnc_password.set_placeholder_text(tr("Password VNC da 6 a 8 caratteri"))
        self.vnc_query = Gtk.CheckButton(label=tr("Chiedi conferma locale prima di accettare un client"))
        self.vnc_query.set_active(True)
        for row, (label, widget) in enumerate(((tr("Porta"), self.vnc_port), (tr("Accesso"), self.vnc_scope), (tr("Password"), self.vnc_password))):
            form.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            form.attach(widget, 1, row, 1, 1)
        form.attach(self.vnc_query, 1, 3, 1, 1)
        box.pack_start(form, False, False, 0)

        security = Gtk.Label(
            label=tr("La password viene salvata in un file TigerVNC con permessi 0600 e non appare nella command line. Per accessi via rete usa TLS oppure un tunnel SSH."),
            xalign=0,
        )
        security.set_line_wrap(True)
        security.get_style_context().add_class("security-note")
        box.pack_start(security, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        start = self.button(tr("Avvia VNC"), "media-playback-start-symbolic")
        stop = self.button(tr("Ferma"), "media-playback-stop-symbolic")
        firewall = self.button(tr("Apri firewall VNC"), "security-high-symbolic")
        copy = self.button(tr("Copia indirizzo"), "edit-copy-symbolic")
        start.connect("clicked", self.start_vnc)
        stop.connect("clicked", self.stop_vnc)
        firewall.connect("clicked", self.open_vnc_firewall)
        copy.connect("clicked", lambda _b: _copy(RUNTIME.vnc_description))
        for button in (start, stop, firewall, copy):
            actions.pack_start(button, False, False, 0)
        self.vnc_status = Gtk.Label(label=tr("Fermo"), xalign=0)
        actions.pack_start(self.vnc_status, True, True, 8)
        box.pack_start(actions, False, False, 0)
        self.vnc_log_label = Gtk.Label(label="", xalign=0)
        self.vnc_log_label.set_line_wrap(True)
        self.vnc_log_label.set_selectable(True)
        self.vnc_log_label.get_style_context().add_class("service-output")
        box.pack_start(self.vnc_log_label, False, False, 0)
        outer.pack_start(card, False, False, 0)
        return outer

    def _on_destroy(self, *_args) -> None:
        if getattr(self, "_status_timer", 0):
            try:
                GLib.source_remove(self._status_timer)
            except Exception:
                pass
            self._status_timer = 0

    def _package_installed(self, package: str) -> bool:
        # Senza rpm (avvio dai sorgenti su una distribuzione non RPM) l'intera
        # finestra Strumenti di rete non si apriva: FileNotFoundError non era
        # gestito. Un pacchetto che non si puo' verificare si considera assente.
        try:
            return _run(["rpm", "-q", package], timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def refresh_status(self, recheck_packages: bool = False) -> bool:
        """Aggiorna lo stato dei servizi senza bloccare l'interfaccia.

        Prima rpm -q e systemctl giravano nel thread di GTK ogni 4 secondi, per cinque servizi: la
        finestra si bloccava per una frazione di secondo a ogni giro. Ora girano in un thread e i
        pacchetti installati si ricontrollano solo all'apertura e dopo un'installazione.
        """
        self._refresh_local_runtime()
        if getattr(self, "_status_running", False):
            return True
        self._status_running = True
        cached = dict(getattr(self, "_installed_cache", {}))

        def worker() -> Dict[str, Tuple[bool, bool]]:
            result: Dict[str, Tuple[bool, bool]] = {}
            for service in self.SERVICES:
                installed = cached.get(service.package)
                if installed is None or recheck_packages:
                    installed = self._package_installed(service.package)
                active = False
                if installed:
                    try:
                        proc = _run(["systemctl", "is-active", service.unit], timeout=4)
                        active = proc.returncode == 0 and proc.stdout.strip() == "active"
                    except (OSError, subprocess.SubprocessError):
                        active = False
                result[service.unit] = (installed, active)
            return result

        def done(result: object, error: Optional[BaseException]) -> None:
            self._status_running = False
            if error or not isinstance(result, dict):
                return
            self._installed_cache = {
                service.package: result[service.unit][0] for service in self.SERVICES if service.unit in result
            }
            for service in self.SERVICES:
                installed, active = result.get(service.unit, (False, False))
                self._show_service_state(service, installed, active)

        _async(worker, done)
        return True

    def _show_service_state(self, service: ServiceDefinition, installed: bool, active: bool) -> None:
        package_label = self.package_labels.get(service.unit)
        state_label = self.status_labels.get(service.unit)
        actions = self.service_action_buttons.get(service.unit, {})
        install_button = self.install_buttons.get(service.unit)
        if package_label:
            if installed:
                package_label.set_markup(tr('<span foreground="#39c879" weight="bold">● Installato</span>'))
                package_label.get_style_context().add_class("installed-badge")
            else:
                package_label.set_markup(tr('<span foreground="#e8a84a" weight="bold">● Non installato</span>'))
        if install_button:
            install_button.set_sensitive(not installed)
            install_button.set_tooltip_text(tr("Pacchetto già installato") if installed else tr("Installa {package}").format(package=service.package))
        for action_name, button in actions.items():
            if action_name != "install":
                button.set_sensitive(installed)
        if not installed:
            if state_label:
                state_label.set_markup(tr('<span foreground="#8792a2">● Non disponibile</span>'))
            return
        if state_label:
            state_label.set_markup(
                tr('<span foreground="#4cc38a" weight="bold">● Attivo</span>') if active
                else tr('<span foreground="#9aa5b2">● Fermo</span>')
            )
        if actions:
            actions.get("start") and actions["start"].set_sensitive(not active)
            actions.get("stop") and actions["stop"].set_sensitive(active)

    def _refresh_local_runtime(self) -> None:
        """Server HTTP/VNC avviati da FedTermHub: stato in memoria, nessun comando esterno."""
        proc = RUNTIME.http_process
        if proc and proc.poll() is None:
            self.http_status.set_markup(tr("<span foreground=\"#4cc38a\" weight=\"bold\">● Attivo</span> — {markup_escape_text}").format(markup_escape_text=GLib.markup_escape_text(RUNTIME.http_description)))
        else:
            self.http_status.set_markup(tr('<span foreground="#9aa5b2">● Fermo</span>'))
        vnc = RUNTIME.vnc_process
        if vnc and vnc.poll() is None:
            self.vnc_status.set_markup(tr("<span foreground=\"#4cc38a\" weight=\"bold\">● Attivo</span> — {markup_escape_text}").format(markup_escape_text=GLib.markup_escape_text(RUNTIME.vnc_description)))
        else:
            self.vnc_status.set_markup(tr('<span foreground="#9aa5b2">● Fermo</span>'))
        if RUNTIME.vnc_log:
            self.vnc_log_label.set_text("\n".join(RUNTIME.vnc_log[-4:]))
        self.refresh_share_views()


    def _confirm_insecure(self, service: ServiceDefinition, action: str) -> bool:
        if not service.insecure or action not in {"start", "enable", "firewall"}:
            return True
        dialog = Gtk.MessageDialog(
            transient_for=self.parent,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=tr("Abilitare {name}?").format(name=service.name),
        )
        dialog.format_secondary_text(
            tr("{name} non protegge il traffico con cifratura o autenticazione moderna. Usalo solo su una rete fidata e soltanto quando è realmente necessario.").format(name=service.name)
        )
        dialog.add_buttons(tr("Annulla"), Gtk.ResponseType.CANCEL, tr("Continua"), Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def on_service_action(self, _button: Gtk.Button, service: ServiceDefinition, action: str) -> None:
        if not self._confirm_insecure(service, action):
            return
        if action == "install":
            commands = [_privileged_command(["dnf", "install", "-y", service.package])]
        elif action == "firewall":
            if not _command_exists("firewall-cmd"):
                self.message(self.parent, tr("Firewalld non disponibile"), tr("Installa e avvia firewalld per gestire le regole dall'interfaccia."), True)
                return
            # Una sola autorizzazione per tutte le regole e il reload.
            lines = [
                shlex.join(["firewall-cmd", "--permanent", f"--add-service={firewall_service}"])
                for firewall_service in service.firewall_services
            ] + [shlex.join(["firewall-cmd", "--reload"])]
            commands = [_privileged_command(["sh", "-c", tr("set -e\n") + "\n".join(lines)])]
        else:
            verb = "enable" if action == "enable" else action
            args = ["systemctl", verb]
            if action == "enable":
                args.append("--now")
            args.append(service.unit)
            commands = [_privileged_command(args)]

        self.status_labels[service.unit].set_text(tr("Operazione in corso…"))

        def worker() -> str:
            messages = []
            for command in commands:
                proc = _run(command, timeout=180)
                message = (proc.stdout or proc.stderr or tr("exit {returncode}").format(returncode=proc.returncode)).strip()
                messages.append(message)
                if proc.returncode != 0:
                    raise RuntimeError(message)
            return "\n".join(messages)

        def done(_result: object, error: Optional[BaseException]) -> None:
            self.refresh_status(recheck_packages=(action == "install"))
            if error:
                self.message(self.parent, tr("Operazione {name} non riuscita").format(name=service.name), str(error), True)

        _async(worker, done)

    def prepare_tftp_directory(self, _button: Gtk.Button) -> None:
        def worker() -> str:
            proc = _run(_privileged_command(["install", "-d", "-m", "0755", "/var/lib/tftpboot"]), timeout=90)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or tr("Impossibile preparare /var/lib/tftpboot"))
            return tr("/var/lib/tftpboot pronta.")

        _async(worker, lambda result, error: self.message(self.parent, tr("Directory TFTP"), str(error or result), bool(error)))

    def open_tftp_directory(self, _button: Gtk.Button) -> None:
        path = Path("/var/lib/tftpboot")
        if not path.exists():
            self.message(self.parent, tr("Directory TFTP assente"), tr("Premi prima Prepara cartella."), True)
            return
        _open_path(path)

    def start_http(self, _button: Gtk.Button) -> None:
        RUNTIME.stop_http()
        folder = self.http_dir.get_filename() or str(Path.home())
        bind = self.http_bind.get_active_id() or "127.0.0.1"
        port = int(self.http_port.get_value())
        command = [sys.executable or "python3", "-m", "http.server", str(port), "--bind", bind, "--directory", folder]
        try:
            proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except Exception as exc:
            self.message(self.parent, tr("Server HTTP non avviato"), str(exc), True)
            return
        # Si considera avviato quando risponde sulla porta: prima si aspettavano 0,12 s e un server
        # morto subito dopo (porta occupata) veniva mostrato come attivo.
        probe_host = "127.0.0.1" if bind in {"0.0.0.0", "127.0.0.1"} else bind  # nosec B104
        started = time.monotonic()
        listening = False
        while time.monotonic() < started + 3.0 and proc.poll() is None:
            try:
                with socket.create_connection((probe_host, port), timeout=0.3):
                    listening = True
                    break
            except OSError:
                time.sleep(0.1)
        if listening:
            # A rispondere potrebbe essere un altro programma che occupa gia' la porta: in quel caso
            # il nostro server esce subito con "Address already in use". Si controlla che sia vivo.
            time.sleep(max(0.0, started + 0.8 - time.monotonic()))
            listening = proc.poll() is None
        if not listening:
            error = ""
            if proc.poll() is None:
                proc.terminate()
            else:
                error = (proc.stderr.read() if proc.stderr else "").strip().splitlines()[-1:] or [""]
                error = error[0]
            hint = tr("La porta è già usata da un altro programma.") if "Address already in use" in error else error
            self.message(self.parent, tr("Server HTTP non avviato"), hint or tr("Il server non risponde."), True)
            return
        RUNTIME.http_process = proc
        host = _primary_ipv4() if bind == "0.0.0.0" else bind  # nosec B104
        RUNTIME.http_description = f"http://{host}:{port}/"
        self.refresh_status()

    def stop_http(self, _button: Gtk.Button) -> None:
        RUNTIME.stop_http()
        self.refresh_status()

    def _nfs_client_text(self) -> str:
        child = self.nfs_clients.get_child()
        return child.get_text().strip() if isinstance(child, Gtk.Entry) else ""

    @staticmethod
    def _validate_nfs_client(value: str) -> bool:
        if value == "*":
            return True
        try:
            ipaddress.ip_network(value, strict=False)
            return True
        except ValueError:
            return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$", value))

    def apply_nfs_export(self, _button: Gtk.Button) -> None:
        folder = Path(self.nfs_dir.get_filename() or "")
        clients = self._nfs_client_text()
        if not folder.is_dir():
            self.message(self.parent, tr("Cartella NFS non valida"), tr("Seleziona una cartella esistente."), True)
            return
        if not clients or not self._validate_nfs_client(clients):
            self.message(self.parent, tr("Client NFS non validi"), tr("Inserisci una subnet CIDR, un host oppure *."), True)
            return
        mode = self.nfs_mode.get_active_id() or "ro"
        root_option = "root_squash" if self.nfs_root_squash.get_active() else "no_root_squash"
        options = f"{mode},sync,{root_option},no_subtree_check"
        escaped_path = str(folder).replace(" ", "\\040")
        export_line = f"{escaped_path} {clients}({options})\n"
        temp = CACHE_DIR / f"nfs-export-{uuid.uuid4().hex}.tmp"
        temp.write_text(export_line, encoding="utf-8")
        os.chmod(temp, 0o600)
        self.nfs_status.set_text(tr("Applicazione export NFS in corso…"))

        def worker() -> str:
            try:
                proc = _run_privileged_script([
                    shlex.join(["install", "-D", "-m", "0644", str(temp), "/etc/exports.d/fedtermhub.exports"]),
                    shlex.join(["exportfs", "-ra"]),
                    shlex.join(["systemctl", "enable", "--now", "nfs-server.service"]),
                ])
                if proc.returncode != 0:
                    raise RuntimeError(_privileged_error(proc))
                verify = _run(["exportfs", "-v"], timeout=10)
                return verify.stdout.strip() or export_line.strip()
            finally:
                temp.unlink(missing_ok=True)

        def done(result: object, error: Optional[BaseException]) -> None:
            self.nfs_status.set_text(tr("Errore: {error}").format(error=error) if error else str(result))
            self.refresh_status()

        _async(worker, done)

    def remove_nfs_export(self, _button: Gtk.Button) -> None:
        self.nfs_status.set_text(tr("Rimozione export in corso…"))

        def worker() -> str:
            proc = _run_privileged_script([
                shlex.join(["rm", "-f", "/etc/exports.d/fedtermhub.exports"]),
                shlex.join(["exportfs", "-ra"]),
            ])
            if proc.returncode != 0:
                raise RuntimeError(_privileged_error(proc))
            return tr("Export gestito da FedTermHub rimosso.")

        _async(worker, lambda result, error: self.nfs_status.set_text(tr("Errore: {error}").format(error=error) if error else str(result)))

    def verify_nfs_export(self, _button: Gtk.Button) -> None:
        self.nfs_status.set_text(tr("Verifica export…"))

        def worker() -> str:
            proc = _run(["exportfs", "-v"], timeout=10)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or tr("exportfs non disponibile"))
            return proc.stdout.strip() or tr("Nessun export attivo.")

        _async(worker, lambda result, error: self.nfs_status.set_text(tr("Errore: {error}").format(error=error) if error else str(result)))

    def _create_vnc_password_file(self, password: str) -> Path:
        binary = shutil.which("vncpasswd")
        if not binary:
            raise RuntimeError(tr("vncpasswd non trovato. Installa tigervnc-server-common."))
        proc = subprocess.run([binary, "-f"], input=(password + "\n").encode("utf-8"), capture_output=True, timeout=15, check=False)
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip() or tr("Creazione password VNC fallita"))
        password_file = Path.home() / ".config" / "fedtermhub" / "vnc.passwd"
        password_file.parent.mkdir(parents=True, exist_ok=True)
        # Il file nasce direttamente 0600: scriverlo con i permessi di default
        # e restringerlo dopo lascerebbe una breve finestra leggibile ad altri.
        fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, proc.stdout)
        finally:
            os.close(fd)
        os.chmod(password_file, 0o600)
        return password_file

    def start_vnc(self, _button: Gtk.Button) -> None:
        RUNTIME.stop_vnc()
        password = self.vnc_password.get_text()
        if not 6 <= len(password) <= 8:
            self.message(self.parent, tr("Password VNC non valida"), tr("Usa una password da 6 a 8 caratteri."), True)
            return
        session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if session_type == "wayland":
            binary = shutil.which("w0vncserver")
            package = "tigervnc-wayland-server"
        else:
            binary = shutil.which("x0vncserver")
            package = "tigervnc-x11-server"
        if not binary:
            self.message(self.parent, tr("Server VNC non installato"), tr("Installa il pacchetto {package}.").format(package=package), True)
            return
        try:
            password_file = self._create_vnc_password_file(password)
        except Exception as exc:
            self.message(self.parent, tr("Password VNC non preparata"), str(exc), True)
            return
        port = int(self.vnc_port.get_value())
        localhost = "yes" if (self.vnc_scope.get_active_id() or "local") == "local" else "no"
        command = [
            binary,
            "-rfbport",
            str(port),
            "-PasswordFile",
            str(password_file),
            "-SecurityTypes",
            tr("TLSVnc,VncAuth"),
            "-localhost",
            localhost,
        ]
        if self.vnc_query.get_active():
            command.append("-QueryConnect")
        if session_type != "wayland":
            command.extend(["-display", os.environ.get("DISPLAY", ":0"), "-fg"])
        RUNTIME.vnc_log.clear()
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        except Exception as exc:
            self.message(self.parent, tr("Server VNC non avviato"), str(exc), True)
            return
        time.sleep(0.25)
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            self.message(self.parent, tr("Server VNC non avviato"), output.strip() or tr("Codice {returncode}").format(returncode=proc.returncode), True)
            return
        RUNTIME.vnc_process = proc
        RUNTIME.vnc_description = f"vnc://{_primary_ipv4() if localhost == 'no' else '127.0.0.1'}:{port}"
        self.vnc_password.set_text("")

        def reader() -> None:
            if proc.stdout:
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        RUNTIME.vnc_log.append(line)
                        del RUNTIME.vnc_log[:-80]
            GLib.idle_add(lambda: (self.refresh_status(), False)[1])

        threading.Thread(target=reader, daemon=True).start()
        self.refresh_status()

    def stop_vnc(self, _button: Gtk.Button) -> None:
        RUNTIME.stop_vnc()
        self.refresh_status()

    def open_vnc_firewall(self, _button: Gtk.Button) -> None:
        if not _command_exists("firewall-cmd"):
            self.message(self.parent, tr("Firewalld non disponibile"), tr("Installa e avvia firewalld."), True)
            return
        port = int(self.vnc_port.get_value())

        def worker() -> str:
            proc = _run_privileged_script([
                shlex.join(["firewall-cmd", "--permanent", f"--add-port={port}/tcp"]),
                shlex.join(["firewall-cmd", "--reload"]),
            ])
            if proc.returncode != 0:
                raise RuntimeError(_privileged_error(proc))
            return tr("Porta {port}/tcp aperta in firewalld.").format(port=port)

        _async(worker, lambda result, error: self.message(self.parent, tr("Firewall VNC"), str(error or result), bool(error)))


class ListeningPortsPage(ToolPage):
    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(tr("Porte locali in ascolto"), tr("Visualizza TCP e UDP in ascolto, indirizzi locali e processi associati."), "network-idle-symbolic")
        self.parent = parent
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text(tr("Filtra indirizzo, porta o processo…"))
        self.search.set_hexpand(True)
        refresh = self.button(tr("Aggiorna"), "view-refresh-symbolic")
        copy = self.button(tr("Copia endpoint"), "edit-copy-symbolic")
        toolbar.pack_start(self.search, True, True, 0)
        toolbar.pack_start(refresh, False, False, 0)
        toolbar.pack_start(copy, False, False, 0)
        self.pack_start(toolbar, False, False, 0)

        self.store = Gtk.ListStore(str, str, str, str, str)
        self.filter = self.store.filter_new()
        self.filter.set_visible_func(self._visible)
        tree = Gtk.TreeView(model=self.filter)
        self.tree = tree
        for index, title in enumerate((tr("Protocollo"), tr("Stato"), tr("Indirizzo locale"), tr("Porta"), tr("Processo"))):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ypad", 5)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_expand(index in (2, 4))
            tree.append_column(column)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(380)
        scroll.add(tree)
        card = self.card()
        card.add(scroll)
        self.pack_start(card, True, True, 0)
        self.status = Gtk.Label(label="", xalign=0)
        self.status.get_style_context().add_class("muted")
        self.pack_start(self.status, False, False, 0)
        refresh.connect("clicked", lambda _b: self.refresh())
        copy.connect("clicked", self.copy_selected)
        self.search.connect("search-changed", lambda _e: self.filter.refilter())
        self.refresh()

    def _visible(self, model: Gtk.TreeModel, tree_iter: Gtk.TreeIter, _data=None) -> bool:
        query = self.search.get_text().strip().casefold()
        return not query or query in " ".join(str(model[tree_iter][i]) for i in range(5)).casefold()

    def refresh(self) -> None:
        self.status.set_text(tr("Lettura porte…"))

        def worker() -> List[Tuple[str, str, str, str, str]]:
            proc = _run(["ss", "-H", "-lntup"], timeout=10)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or tr("ss non disponibile"))
            rows = []
            for line in proc.stdout.splitlines():
                parts = line.split(maxsplit=6)
                if len(parts) < 6:
                    continue
                proto, state, _recv, _send, local, _peer = parts[:6]
                process = parts[6] if len(parts) > 6 else ""
                host, port = _split_endpoint(local)
                rows.append((proto.upper(), state, host, port, _ss_process_text(process)))
            return rows

        def done(result: object, error: Optional[BaseException]) -> None:
            self.store.clear()
            if error:
                self.status.set_text(tr("Errore: {error}").format(error=error))
                return
            rows = result if isinstance(result, list) else []
            for row in rows:
                self.store.append(list(row))
            self.status.set_text(tr("{n_rows} socket in ascolto").format(n_rows=len(rows)))

        _async(worker, done)

    def copy_selected(self, _button: Gtk.Button) -> None:
        model, tree_iter = self.tree.get_selection().get_selected()
        if tree_iter:
            host, port = model[tree_iter][2], model[tree_iter][3]
            _copy(f"{host}:{port}")


class WakeOnLanPage(ToolPage):
    MAC_RE = re.compile(r"^[0-9a-fA-F]{2}([:\-]?[0-9a-fA-F]{2}){5}$")

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__("Wake-on-LAN", tr("Invia un Magic Packet a un computer della rete locale."), "system-run-symbolic")
        self.parent = parent
        card = self.card()
        form = Gtk.Grid(column_spacing=10, row_spacing=8, margin=12)
        card.add(form)
        self.mac = Gtk.Entry()
        self.mac.set_placeholder_text("AA:BB:CC:DD:EE:FF")
        # 255.255.255.255 esce spesso da una sola interfaccia: si propongono anche i broadcast delle
        # reti a cui il PC e' collegato (per esempio 192.168.1.255), che sono quelli che funzionano.
        self.broadcast = Gtk.ComboBoxText.new_with_entry()
        broadcasts = []
        for network in _detected_networks():
            try:
                address = str(ipaddress.ip_network(network, strict=False).broadcast_address)
            except ValueError:
                continue
            if address not in broadcasts:
                broadcasts.append(address)
        for address in broadcasts + ["255.255.255.255"]:
            self.broadcast.append_text(address)
        self.broadcast.set_active(0)
        self.port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.port.set_value(9)
        self.repeat = Gtk.SpinButton.new_with_range(1, 10, 1)
        self.repeat.set_value(3)
        for row, (label, widget) in enumerate(((tr("Indirizzo MAC"), self.mac), (tr("Broadcast"), self.broadcast), (tr("Porta UDP"), self.port), (tr("Ripetizioni"), self.repeat))):
            form.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            form.attach(widget, 1, row, 1, 1)
        self.pack_start(card, False, False, 0)
        send = self.button(tr("Invia Magic Packet"), "network-transmit-symbolic")
        send.connect("clicked", self.send)
        self.pack_start(send, False, False, 0)
        self.result = Gtk.Label(label="", xalign=0)
        self.result.set_line_wrap(True)
        self.pack_start(self.result, False, False, 0)

    def send(self, _button: Gtk.Button) -> None:
        mac = re.sub(r"[:-]", "", self.mac.get_text().strip())
        if len(mac) != 12 or not all(ch in "0123456789abcdefABCDEF" for ch in mac):
            self.message(self.parent, tr("MAC non valido"), tr("Usa il formato AA:BB:CC:DD:EE:FF."), True)
            return
        packet = bytes.fromhex("FF" * 6 + mac * 16)
        destination = (self.broadcast.get_child().get_text().strip() or "255.255.255.255", int(self.port.get_value()))
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                for _ in range(int(self.repeat.get_value())):
                    sock.sendto(packet, destination)
                    time.sleep(0.05)
            self.result.set_text(tr("Magic Packet inviato a {upper} tramite {v}:{v2}.").format(upper=self.mac.get_text().upper(), v=destination[0], v2=destination[1]))
        except Exception as exc:
            self.message(self.parent, tr("Invio Wake-on-LAN non riuscito"), str(exc), True)


class NetworkScannerPage(ToolPage):
    def __init__(self, parent: Gtk.Window, open_port_scan: Callable[[str], None], create_session: Optional[Callable[[str, str], None]]) -> None:
        super().__init__(tr("Network scanner"), tr("Individua gli host attivi in una subnet senza eseguire una scansione porte completa."), "network-workgroup-symbolic")
        self.parent = parent
        self.open_port_scan_callback = open_port_scan
        self.create_session = create_session
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.target = Gtk.ComboBoxText.new_with_entry()
        for network in _detected_networks():
            self.target.append_text(network)
        self.target.set_active(0)
        self.resolve = Gtk.CheckButton(label=tr("Risolvi nomi DNS"))
        # L'ARP-scan (che riempie MAC e Produttore e trova anche gli host che
        # non rispondono al ping) richiede i privilegi di root: nmap lo fa solo
        # se lanciato via pkexec. Senza, la scansione resta un ping/TCP sweep.
        self.privileged = Gtk.CheckButton(label=tr("ARP con privilegi (MAC/vendor)"))
        self.privileged.set_tooltip_text(
            tr("Esegue nmap tramite pkexec per l'ARP-scan: mostra indirizzo MAC e "
            "produttore e rileva host che ignorano il ping. Chiede la password di sistema.")
        )
        if os.geteuid() != 0 and not _command_exists("pkexec"):
            self.privileged.set_sensitive(False)
            self.privileged.set_tooltip_text(tr("pkexec non disponibile: impossibile elevare i privilegi."))
        scan = self.button(tr("Scansiona rete"), "system-search-symbolic")
        toolbar.pack_start(Gtk.Label(label=tr("Rete/CIDR")), False, False, 0)
        toolbar.pack_start(self.target, True, True, 0)
        toolbar.pack_start(self.resolve, False, False, 0)
        toolbar.pack_start(self.privileged, False, False, 0)
        toolbar.pack_start(scan, False, False, 0)
        self.pack_start(toolbar, False, False, 0)
        self.store = Gtk.ListStore(str, str, str, str)
        self.tree = Gtk.TreeView(model=self.store)
        for index, title in enumerate(("IP", tr("Nome host"), "MAC", tr("Produttore"))):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ypad", 5)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_expand(index in (0, 1, 3))
            self.tree.append_column(column)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(380)
        scroll.add(self.tree)
        card = self.card()
        card.add(scroll)
        self.pack_start(card, True, True, 0)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        port_scan = self.button(tr("Scansiona porte host"), "network-transmit-receive-symbolic")
        new_ssh = self.button(tr("Crea SSH"), "network-server-symbolic")
        new_rdp = self.button(tr("Crea RDP"), "video-display-symbolic")
        actions.pack_start(port_scan, False, False, 0)
        actions.pack_start(new_ssh, False, False, 0)
        actions.pack_start(new_rdp, False, False, 0)
        self.status = Gtk.Label(label="", xalign=0)
        actions.pack_start(self.status, True, True, 8)
        self.pack_start(actions, False, False, 0)
        scan.connect("clicked", self.scan)
        port_scan.connect("clicked", lambda _b: self._use_selected(self.open_port_scan_callback))
        new_ssh.connect("clicked", lambda _b: self._create_selected("ssh"))
        new_rdp.connect("clicked", lambda _b: self._create_selected("rdp"))

    def _target_text(self) -> str:
        child = self.target.get_child()
        return child.get_text().strip() if isinstance(child, Gtk.Entry) else ""

    def scan(self, _button: Gtk.Button) -> None:
        if not _command_exists("nmap"):
            self.message(self.parent, tr("Nmap non installato"), tr("Installa il pacchetto nmap."), True)
            return
        target = self._target_text()
        try:
            ipaddress.ip_network(target, strict=False)
        except ValueError:
            self.message(self.parent, tr("Rete non valida"), tr("Inserisci una subnet CIDR, per esempio 192.168.1.0/24."), True)
            return
        privileged = self.privileged.get_active() and os.geteuid() != 0
        self.status.set_text(tr("Scansione in corso…"))
        self.store.clear()

        def worker() -> Tuple[List[Tuple[str, str, str, str]], str]:
            command = ["nmap", "-sn", "-oX", "-"]
            if not self.resolve.get_active():
                command.append("-n")
            command.append(target)
            if privileged:
                command = _privileged_command(command)
            # L'ARP-scan può richiedere qualche secondo in più su /24 grandi.
            proc = _run(command, timeout=420)
            # pkexec: 126 = autorizzazione negata dall'utente, 127 = errore auth.
            if privileged and proc.returncode in (126, 127):
                raise RuntimeError(tr("Autorizzazione negata: scansione ARP non eseguita."))
            if proc.returncode not in (0, 1) or not proc.stdout.strip():
                detail = proc.stderr.strip() or tr("Nmap non ha prodotto risultati")
                raise RuntimeError(detail)
            root = ET.fromstring(proc.stdout)
            rows = []
            saw_mac = False
            for host in root.findall("host"):
                if host.find("status") is not None and host.find("status").get("state") != "up":
                    continue
                addresses = host.findall("address")
                ip = next((a.get("addr", "") for a in addresses if a.get("addrtype") in ("ipv4", "ipv6")), "")
                mac_node = next((a for a in addresses if a.get("addrtype") == "mac"), None)
                mac = mac_node.get("addr", "") if mac_node is not None else ""
                vendor = mac_node.get("vendor", "") if mac_node is not None else ""
                if mac:
                    saw_mac = True
                hostname_node = host.find("hostnames/hostname")
                hostname = hostname_node.get("name", "") if hostname_node is not None else ""
                rows.append((ip, hostname, mac, vendor))
            # Suggerimento contestuale: host trovati ma nessun MAC = privilegi
            # mancanti (fuori dalla LAN il MAC non è comunque disponibile).
            hint = ""
            if rows and not saw_mac and not privileged:
                hint = tr("  (attiva 'ARP con privilegi' per MAC e produttore)")
            return rows, hint

        def done(result: object, error: Optional[BaseException]) -> None:
            if error:
                self.status.set_text(tr("Errore: {error}").format(error=error))
                return
            rows, hint = result if isinstance(result, tuple) else ([], "")
            for row in rows:
                self.store.append(list(row))
            if not rows:
                self.status.set_text(
                    tr("Nessun host attivo. Verifica la subnet, oppure attiva l'ARP privilegiato.")
                )
            else:
                self.status.set_text(tr("{n_rows} host attivi{hint}").format(n_rows=len(rows), hint=hint))

        _async(worker, done)

    def _selected_ip(self) -> str:
        model, tree_iter = self.tree.get_selection().get_selected()
        return model[tree_iter][0] if tree_iter else ""

    def _use_selected(self, callback: Callable[[str], None]) -> None:
        ip = self._selected_ip()
        if ip:
            callback(ip)

    def _create_selected(self, protocol: str) -> None:
        ip = self._selected_ip()
        if ip and self.create_session:
            self.create_session(protocol, ip, "")


def session_protocol_for_service(port: str, service: str) -> str:
    """Il tipo di sessione per una porta trovata dal port scanner, oppure "" se non ce n'e' uno."""
    service = (service or "").lower()
    if "ssh" in service or port == "22":
        return "ssh"
    if "ms-wbt" in service or "rdp" in service or port == "3389":
        return "rdp"
    if "vnc" in service or (port.isdigit() and 5900 <= int(port) <= 5999):
        return "vnc"
    if "telnet" in service or port == "23":
        return "telnet"
    return ""


class PortScannerPage(ToolPage):
    def __init__(self, parent: Gtk.Window, create_session: Optional[Callable[[str, str], None]]) -> None:
        super().__init__(tr("Port scanner"), tr("Analizza le porte TCP di un host e identifica i servizi disponibili."), "network-transmit-receive-symbolic")
        self.parent = parent
        self.create_session = create_session
        form = Gtk.Grid(column_spacing=8, row_spacing=7)
        self.target = Gtk.Entry()
        self.target.set_placeholder_text(tr("IP o nome host"))
        self.profile = Gtk.ComboBoxText()
        for key, title in (("common", tr("Porte comuni")), ("top100", tr("Top 100")), ("custom", tr("Intervallo personalizzato")), ("full", tr("Tutte le porte TCP"))):
            self.profile.append(key, title)
        self.profile.set_active_id("common")
        self.ports = Gtk.Entry(text="22,53,80,110,143,443,445,3389,5900,8080,8443")
        self.services = Gtk.CheckButton(label=tr("Rileva servizi e versioni"))
        self.skip_ping = Gtk.CheckButton(label=tr("Considera host attivo (-Pn)"))
        for row, (label, widget) in enumerate(((tr("Destinazione"), self.target), (tr("Profilo"), self.profile), (tr("Porte"), self.ports))):
            form.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            form.attach(widget, 1, row, 1, 1)
        form.attach(self.services, 1, 3, 1, 1)
        form.attach(self.skip_ping, 1, 4, 1, 1)
        scan = self.button(tr("Avvia scansione"), "system-search-symbolic")
        form.attach(scan, 1, 5, 1, 1)
        card = self.card()
        card.add(form)
        self.pack_start(card, False, False, 0)
        self.store = Gtk.ListStore(str, str, str, str, str)
        self.tree = Gtk.TreeView(model=self.store)
        for index, title in enumerate((tr("Porta"), tr("Protocollo"), tr("Stato"), tr("Servizio"), tr("Versione"))):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ypad", 5)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_expand(index == 4)
            self.tree.append_column(column)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(330)
        scroll.add(self.tree)
        result_card = self.card()
        result_card.add(scroll)
        self.pack_start(result_card, True, True, 0)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        create = self.button(tr("Crea sessione dal servizio"), "list-add-symbolic")
        copy = self.button(tr("Copia risultato"), "edit-copy-symbolic")
        self.status = Gtk.Label(label="", xalign=0)
        actions.pack_start(create, False, False, 0)
        actions.pack_start(copy, False, False, 0)
        actions.pack_start(self.status, True, True, 8)
        self.pack_start(actions, False, False, 0)
        self.profile.connect("changed", self.on_profile_changed)
        scan.connect("clicked", self.scan)
        create.connect("clicked", self.create_from_selected)
        copy.connect("clicked", self.copy_results)

    def set_target(self, target: str) -> None:
        self.target.set_text(target)

    def on_profile_changed(self, _combo: Gtk.ComboBoxText) -> None:
        profile = self.profile.get_active_id()
        self.ports.set_sensitive(profile in ("common", "custom"))
        if profile == "common":
            self.ports.set_text("22,53,80,110,143,443,445,3389,5900,8080,8443")

    def scan(self, _button: Gtk.Button) -> None:
        if not _command_exists("nmap"):
            self.message(self.parent, tr("Nmap non installato"), tr("Installa il pacchetto nmap."), True)
            return
        target = self.target.get_text().strip()
        # Una destinazione che comincia con "-" verrebbe letta da nmap come opzione (-oN file...).
        if not target or any(ch.isspace() for ch in target) or target.startswith("-"):
            self.message(self.parent, tr("Destinazione non valida"), tr("Inserisci un singolo IP o nome host."), True)
            return
        profile = self.profile.get_active_id() or "common"
        command = ["nmap", "-sT", "--open", "-T4", "-oX", "-"]
        if profile in ("common", "custom"):
            ports = self.ports.get_text().strip()
            if not ports or not re.fullmatch(r"[0-9,\-]+", ports):
                self.message(self.parent, tr("Porte non valide"), tr("Usa numeri, virgole e intervalli, per esempio 22,80,443,8000-8100."), True)
                return
            command += ["-p", ports]
        elif profile == "top100":
            command += ["--top-ports", "100"]
        elif profile == "full":
            command += ["-p-"]
        if self.services.get_active():
            command.append("-sV")
        if self.skip_ping.get_active():
            command.append("-Pn")
        command.append(target)
        self.status.set_text(tr("Scansione in corso…"))
        self.store.clear()

        skip_ping = self.skip_ping.get_active()

        def _parse(stdout: str) -> Tuple[List[Tuple[str, str, str, str, str]], bool]:
            root = ET.fromstring(stdout)
            host = root.find("host")
            host_up = host is not None and (
                host.find("status") is None or host.find("status").get("state") == "up"
            )
            rows = []
            for port in root.findall(".//port"):
                state = port.find("state")
                service = port.find("service")
                version = ""
                if service is not None:
                    version = " ".join(filter(None, [service.get("product", ""), service.get("version", ""), service.get("extrainfo", "")])).strip()
                rows.append((port.get("portid", ""), port.get("protocol", ""), state.get("state", "") if state is not None else "", service.get("name", "") if service is not None else "", version))
            return rows, host_up

        def worker() -> Tuple[List[Tuple[str, str, str, str, str]], str]:
            proc = _run(command, timeout=1800)
            if not proc.stdout.strip():
                raise RuntimeError(proc.stderr.strip() or tr("Nessun risultato Nmap"))
            rows, host_up = _parse(proc.stdout)
            note = ""
            # Host che ignora il ping: nmap lo marca 'down' e non scansiona.
            # Riproviamo automaticamente con -Pn, salvo l'utente l'abbia già messo.
            if not rows and not host_up and not skip_ping:
                retry = list(command)
                retry.insert(-1, "-Pn")
                proc2 = _run(retry, timeout=1800)
                if proc2.stdout.strip():
                    rows, host_up = _parse(proc2.stdout)
                    if rows:
                        note = tr("  (host non risponde al ping: usato -Pn)")
            return rows, note

        def done(result: object, error: Optional[BaseException]) -> None:
            if error:
                self.status.set_text(tr("Errore: {error}").format(error=error))
                return
            rows, note = result if isinstance(result, tuple) else ([], "")
            for row in rows:
                self.store.append(list(row))
            if not rows:
                self.status.set_text(
                    tr("Nessuna porta aperta trovata. Prova 'Considera host attivo (-Pn)' "
                    "o un profilo di porte più ampio.")
                )
            else:
                self.status.set_text(tr("{n_rows} porte aperte{note}").format(n_rows=len(rows), note=note))

        _async(worker, done)

    def create_from_selected(self, _button: Gtk.Button) -> None:
        model, tree_iter = self.tree.get_selection().get_selected()
        if not tree_iter or not self.create_session:
            return
        port = str(model[tree_iter][0])
        protocol = session_protocol_for_service(port, str(model[tree_iter][3]))
        if not protocol:
            # Prima ogni servizio sconosciuto (HTTP, SMB, database...) diventava una sessione SSH.
            self.message(
                self.parent,
                tr("Nessun tipo di sessione per questo servizio"),
                tr("FedTermHub apre sessioni SSH, RDP, VNC e Telnet: la porta {port} ({v}) non corrisponde a nessuna.").format(port=port, v=model[tree_iter][3] or tr('servizio sconosciuto')),
                True,
            )
            return
        # La porta trovata va nella sessione: un SSH sulla 2222 non deve diventare una sessione sulla 22.
        self.create_session(protocol, self.target.get_text().strip(), port)

    def copy_results(self, _button: Gtk.Button) -> None:
        lines = ["\t".join(str(row[i]) for i in range(5)) for row in self.store]
        _copy("\n".join(lines))


class PacketCapturePage(ToolPage):
    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(tr("Cattura pacchetti"), tr("Registra traffico in formato PCAP tramite tcpdump. L'avvio richiede autorizzazione amministrativa."), "network-receive-symbolic")
        self.parent = parent
        form = Gtk.Grid(column_spacing=8, row_spacing=7)
        self.interface = Gtk.ComboBoxText()
        for iface in _interfaces():
            self.interface.append(iface, iface)
        self.interface.set_active_id("any" if "any" in _interfaces() else (_interfaces()[0] if _interfaces() else ""))
        self.filter_entry = Gtk.Entry()
        self.filter_entry.set_placeholder_text(tr("Filtro BPF opzionale, es. host 192.168.1.10 and port 443"))
        self.count = Gtk.SpinButton.new_with_range(0, 10000000, 100)
        self.count.set_value(1000)
        self.duration = Gtk.SpinButton.new_with_range(0, 86400, 10)
        self.duration.set_value(60)
        self.output = Gtk.Entry()
        self._set_default_output()
        output_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        output_box.pack_start(self.output, True, True, 0)
        browse_output = self.button(tr("Sfoglia"), "document-save-symbolic")
        browse_output.connect("clicked", self.choose_output)
        output_box.pack_start(browse_output, False, False, 0)
        for row, (label, widget) in enumerate(((tr("Interfaccia"), self.interface), (tr("Filtro"), self.filter_entry), (tr("Numero pacchetti (0 = illimitato)"), self.count), (tr("Durata secondi (0 = illimitata)"), self.duration), (tr("File di output"), output_box))):
            form.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            form.attach(widget, 1, row, 1, 1)
        card = self.card()
        card.add(form)
        self.pack_start(card, False, False, 0)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.start_button = self.button(tr("Avvia cattura"), "media-record-symbolic")
        self.stop_button = self.button(tr("Ferma"), "media-playback-stop-symbolic")
        open_folder = self.button(tr("Apri cartella"), "folder-open-symbolic")
        open_wireshark = self.button(tr("Apri in Wireshark"), "document-open-symbolic")
        actions.pack_start(self.start_button, False, False, 0)
        actions.pack_start(self.stop_button, False, False, 0)
        actions.pack_start(open_folder, False, False, 0)
        actions.pack_start(open_wireshark, False, False, 0)
        self.status = Gtk.Label(label=tr("Fermo"), xalign=0)
        actions.pack_start(self.status, True, True, 8)
        self.pack_start(actions, False, False, 0)
        self.log_buffer = Gtk.TextBuffer()
        log = Gtk.TextView(buffer=self.log_buffer)
        log.set_editable(False)
        log.set_monospace(True)
        log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        log.get_style_context().add_class("capture-log")
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(180)
        scroll.add(log)
        log_card = self.card()
        log_card.add(scroll)
        self.pack_start(log_card, True, True, 0)
        self.start_button.connect("clicked", self.start_capture)
        self.stop_button.connect("clicked", self.stop_capture)
        open_folder.connect("clicked", lambda _b: _open_path((RUNTIME.capture_file or CAPTURE_DIR).parent if RUNTIME.capture_file else CAPTURE_DIR))
        open_wireshark.connect("clicked", self.open_wireshark)
        self._refresh_state()
        self._state_timer = GLib.timeout_add_seconds(2, self._refresh_state)
        self.connect("destroy", self._on_destroy)

    def _on_destroy(self, *_args) -> None:
        if getattr(self, "_state_timer", 0):
            try:
                GLib.source_remove(self._state_timer)
            except Exception:
                pass
            self._state_timer = 0

    def _set_default_output(self) -> None:
        path = CAPTURE_DIR / f"capture-{datetime.now():%Y%m%d-%H%M%S}.pcap"
        self.output.set_text(str(path))

    def _next_output_name(self, finished: Path) -> bool:
        if Path(self.output.get_text().strip() or "") == finished:
            self._set_default_output()
            if Path(self.output.get_text()) == finished:
                self.output.set_text(str(finished.with_name(f"{finished.stem}-2{finished.suffix}")))
        return False

    def choose_output(self, _button: Gtk.Button) -> None:
        chooser = Gtk.FileChooserDialog(title=tr("Salva cattura PCAP"), transient_for=self.parent, action=Gtk.FileChooserAction.SAVE)
        chooser.add_buttons(tr("Annulla"), Gtk.ResponseType.CANCEL, tr("Seleziona"), Gtk.ResponseType.OK)
        chooser.set_do_overwrite_confirmation(True)
        chooser.set_current_folder(str(CAPTURE_DIR))
        chooser.set_current_name(Path(self.output.get_text() or "capture.pcap").name)
        try:
            if chooser.run() == Gtk.ResponseType.OK:
                self.output.set_text(chooser.get_filename())
        finally:
            chooser.destroy()

    def append_log(self, text: str) -> None:
        end = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end, text.rstrip() + "\n")

    def _refresh_state(self) -> bool:
        proc = RUNTIME.capture_process
        active = bool(proc and proc.poll() is None)
        self.start_button.set_sensitive(not active)
        self.stop_button.set_sensitive(active)
        if active:
            self.status.set_text(tr("Cattura attiva — {capture_file}").format(capture_file=RUNTIME.capture_file))
        elif RUNTIME.capture_file and RUNTIME.capture_file.exists():
            self.status.set_text(tr("Completata — {name} ({st_size} byte)").format(name=RUNTIME.capture_file.name, st_size=RUNTIME.capture_file.stat().st_size))
        else:
            self.status.set_text(tr("Fermo"))
        return True

    def start_capture(self, _button: Gtk.Button) -> None:
        tcpdump = shutil.which("tcpdump")
        if not tcpdump:
            self.message(self.parent, tr("tcpdump non installato"), tr("Installa il pacchetto tcpdump."), True)
            return
        if RUNTIME.capture_process and RUNTIME.capture_process.poll() is None:
            self.message(self.parent, tr("Cattura già attiva"), tr("Ferma prima la cattura corrente."), True)
            return
        interface = self.interface.get_active_id() or "any"
        output = Path(self.output.get_text().strip() or (CAPTURE_DIR / f"capture-{datetime.now():%Y%m%d-%H%M%S}.pcap"))
        output.parent.mkdir(parents=True, exist_ok=True)
        count = int(self.count.get_value())
        duration = int(self.duration.get_value())
        args = [tcpdump, "-i", interface, "-nn", "-U", "-s", "0", "-w", str(output), "-Z", pwd.getpwuid(os.getuid()).pw_name]
        if count:
            args += ["-c", str(count)]
        filter_text = self.filter_entry.get_text().strip()
        if filter_text:
            try:
                args += shlex.split(filter_text)
            except ValueError as exc:
                self.message(self.parent, tr("Filtro non valido"), str(exc), True)
                return
        pidfile = CACHE_DIR / f"tcpdump-{uuid.uuid4().hex}.pid"
        quoted = " ".join(shlex.quote(item) for item in args)
        script = tr("umask 077; echo $$ > {quote}; exec {quoted}").format(quote=shlex.quote(str(pidfile)), quoted=quoted)
        command = _privileged_command(["sh", "-c", script])
        self.append_log(tr("Comando: ") + " ".join(shlex.quote(item) for item in command[:-1]) + tr(" <script protetto>"))
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        except Exception as exc:
            self.message(self.parent, tr("Cattura non avviata"), str(exc), True)
            return
        RUNTIME.capture_process = proc
        RUNTIME.capture_pidfile = pidfile
        RUNTIME.capture_file = output
        RUNTIME.capture_root_pid = None
        if duration:
            RUNTIME.capture_timer = threading.Timer(duration, RUNTIME.stop_capture)
            RUNTIME.capture_timer.daemon = True
            RUNTIME.capture_timer.start()

        def read_output() -> None:
            if proc.stdout:
                for line in proc.stdout:
                    GLib.idle_add(self.append_log, line)
            proc.wait()
            if RUNTIME.capture_process is proc:
                RUNTIME.capture_process = None
                RUNTIME.capture_root_pid = None
                if RUNTIME.capture_timer:
                    RUNTIME.capture_timer.cancel()
                    RUNTIME.capture_timer = None
                if RUNTIME.capture_pidfile:
                    try:
                        RUNTIME.capture_pidfile.unlink(missing_ok=True)
                    except Exception:
                        pass
            GLib.idle_add(self.append_log, tr("Cattura terminata con codice {returncode}.").format(returncode=proc.returncode))
            # _refresh_state restituisce True perche' fa da timer: passata cosi' a idle_add veniva
            # richiamata all'infinito e dopo ogni cattura il programma restava al 100% di CPU.
            GLib.idle_add(lambda: (self._refresh_state(), False)[1])
            # Nome nuovo per la cattura successiva: prima la seconda sovrascriveva la prima.
            GLib.idle_add(self._next_output_name, output)

        threading.Thread(target=read_output, daemon=True).start()

        def read_pid() -> bool:
            if pidfile.exists():
                try:
                    RUNTIME.capture_root_pid = int(pidfile.read_text().strip())
                except Exception:
                    pass
                return False
            return proc.poll() is None

        GLib.timeout_add(250, read_pid)
        self._refresh_state()

    def stop_capture(self, _button: Gtk.Button) -> None:
        self.append_log(tr("Richiesta arresto cattura…"))
        RUNTIME.stop_capture()
        self._refresh_state()

    def open_wireshark(self, _button: Gtk.Button) -> None:
        file = RUNTIME.capture_file
        if not file or not file.exists():
            self.message(self.parent, tr("Nessuna cattura"), tr("Esegui prima una cattura pacchetti."), True)
            return
        wireshark = shutil.which("wireshark")
        if wireshark:
            subprocess.Popen([wireshark, str(file)], start_new_session=True)
        else:
            _open_path(file)


class NetworkToolsDialog(Gtk.Dialog):
    PAGE_NAMES = {
        "services": tr("Server locali"),
        "listeners": tr("Porte in ascolto"),
        "wol": "Wake-on-LAN",
        "network-scan": tr("Network scanner"),
        "port-scan": tr("Port scanner"),
        "capture": tr("Cattura pacchetti"),
    }
    PAGE_DETAILS = {
        "services": ("network-server-symbolic", tr("Servizi e cartelle condivise")),
        "listeners": ("network-idle-symbolic", tr("Socket TCP/UDP locali")),
        "wol": ("system-run-symbolic", tr("Accendi dispositivi in LAN")),
        "network-scan": ("network-workgroup-symbolic", tr("Trova host nella subnet")),
        "port-scan": ("network-transmit-receive-symbolic", tr("Analizza porte e servizi")),
        "capture": ("media-record-symbolic", tr("Registra traffico in PCAP")),
    }

    def __init__(
        self,
        parent: Gtk.Window,
        start_page: str = "services",
        create_session: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        super().__init__(title=tr("Strumenti di rete"), transient_for=parent, modal=False)
        self.set_default_size(1180, 770)
        self.set_size_request(900, 610)
        self.set_resizable(True)
        self.get_style_context().add_class("network-tools-dialog")
        self.add_button(tr("Chiudi"), Gtk.ResponseType.CLOSE)
        self.connect("response", lambda dialog, _response: dialog.destroy())
        self.connect("delete-event", lambda dialog, _event: (dialog.destroy(), True)[1])

        area = self.get_content_area()
        area.set_spacing(0)
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        shell.get_style_context().add_class("network-tools-shell")
        area.add(shell)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, margin=12)
        header.get_style_context().add_class("network-tools-header")
        image = Gtk.Image.new_from_icon_name("preferences-system-network-symbolic", Gtk.IconSize.DIALOG)
        image.set_pixel_size(38)
        header.pack_start(image, False, False, 0)
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        heading = Gtk.Label(label=tr("Centro strumenti di rete"), xalign=0)
        heading.get_style_context().add_class("dialog-heading")
        subtitle = Gtk.Label(label=tr("Servizi, scansione, diagnostica e cattura organizzati per attività."), xalign=0)
        subtitle.get_style_context().add_class("muted")
        text_box.pack_start(heading, False, False, 0)
        text_box.pack_start(subtitle, False, False, 0)
        header.pack_start(text_box, True, True, 0)
        shell.pack_start(header, False, False, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        shell.pack_start(body, True, True, 0)

        sidebar_frame = Gtk.Frame()
        sidebar_frame.set_size_request(235, -1)
        sidebar_frame.get_style_context().add_class("network-tools-sidebar-frame")
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7, margin=8)
        sidebar_frame.add(sidebar_box)
        label = Gtk.Label(label=tr("STRUMENTI"), xalign=0)
        label.get_style_context().add_class("network-sidebar-heading")
        sidebar_box.pack_start(label, False, False, 2)
        self.sidebar_list = Gtk.ListBox()
        self.sidebar_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.sidebar_list.get_style_context().add_class("network-tools-sidebar")
        sidebar_box.pack_start(self.sidebar_list, False, False, 0)
        tip = Gtk.Label(label=tr("Le operazioni privilegiate richiedono conferma Polkit."), xalign=0)
        tip.set_line_wrap(True)
        tip.get_style_context().add_class("muted")
        sidebar_box.pack_end(tip, False, False, 4)
        body.pack_start(sidebar_frame, False, False, 0)

        content_frame = Gtk.Frame()
        content_frame.get_style_context().add_class("network-tools-content")
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(140)
        content_frame.add(self.stack)
        body.pack_start(content_frame, True, True, 0)

        port_page = PortScannerPage(parent, create_session)
        pages = {
            "services": ServicesPage(parent),
            "listeners": ListeningPortsPage(parent),
            "wol": WakeOnLanPage(parent),
            "network-scan": NetworkScannerPage(parent, lambda host: self.open_port_scan(host), create_session),
            "port-scan": port_page,
            "capture": PacketCapturePage(parent),
        }
        self.port_page = port_page
        self._rows: Dict[str, Gtk.ListBoxRow] = {}
        self._row_page_names: Dict[Gtk.ListBoxRow, str] = {}
        for name, page in pages.items():
            self.stack.add_named(page, name)
            row = Gtk.ListBoxRow()
            row.set_activatable(True)
            row.set_selectable(True)
            row.get_style_context().add_class("network-sidebar-row")
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, margin=8)
            icon_name, detail_text = self.PAGE_DETAILS[name]
            icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
            icon.set_pixel_size(22)
            row_box.pack_start(icon, False, False, 0)
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            title = Gtk.Label(label=self.PAGE_NAMES[name], xalign=0)
            title.get_style_context().add_class("network-sidebar-title")
            detail = Gtk.Label(label=detail_text, xalign=0)
            detail.get_style_context().add_class("network-sidebar-detail")
            labels.pack_start(title, False, False, 0)
            labels.pack_start(detail, False, False, 0)
            row_box.pack_start(labels, True, True, 0)
            row.add(row_box)
            self.sidebar_list.add(row)
            self._rows[name] = row
            self._row_page_names[row] = name

        self.sidebar_list.connect("row-selected", self._on_sidebar_selected)
        selected = start_page if start_page in pages else "services"
        self.stack.set_visible_child_name(selected)
        self.sidebar_list.select_row(self._rows[selected])
        self.show_all()

    def _on_sidebar_selected(self, _listbox: Gtk.ListBox, row: Optional[Gtk.ListBoxRow]) -> None:
        if row is None:
            return
        name = self._row_page_names.get(row, "services")
        self.stack.set_visible_child_name(name)

    def open_port_scan(self, host: str) -> None:
        self.port_page.set_target(host)
        self.stack.set_visible_child_name("port-scan")
        row = self._rows.get("port-scan")
        if row is not None:
            self.sidebar_list.select_row(row)


__all__ = [tr("NetworkToolsDialog"), "RUNTIME"]
