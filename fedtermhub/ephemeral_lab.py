# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Laboratori Linux temporanei per FedTermHub.

Il modulo crea VM libvirt *transienti* sopra overlay QCOW2. L'immagine base
non viene modificata e overlay, seed cloud-init e definizione della VM vengono
eliminati quando si chiude la console grafica o FedTermHub.

La connessione predefinita è ``qemu:///session``: non richiede privilegi root
e limita l'impatto sul sistema. Il modulo non è una sandbox per malware.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import string
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

LAB_URI = "qemu:///session"
LAB_ROOT = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "fedtermhub" / "labs"


@dataclass(frozen=True)
class LabSupport:
    ready: bool
    missing: Tuple[str, ...]
    warnings: Tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class LabConfig:
    name: str
    base_image: Path
    memory_mb: int = 2048
    vcpus: int = 2
    network: str = "isolated"  # isolated | user
    username: str = "fedlab"
    password: str = ""
    connection_uri: str = LAB_URI


@dataclass
class LabInstance:
    identifier: str
    domain_name: str
    runtime_dir: Path
    overlay_path: Path
    config: LabConfig
    viewer: Optional[subprocess.Popen] = None
    created_at: float = field(default_factory=time.time)
    state: str = "starting"
    cleanup_started: bool = False


class LabError(RuntimeError):
    pass


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise LabError(f"Componente richiesto non trovato: {name}")
    return path


def safe_domain_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-.")
    cleaned = cleaned[:42] or "linux-test"
    return f"fth-{cleaned}-{secrets.token_hex(3)}"


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "-_.!"
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(max(12, length)))
        if any(c.islower() for c in value) and any(c.isupper() for c in value) and any(c.isdigit() for c in value):
            return value


def inspect_lab_support() -> LabSupport:
    required = ("virsh", "virt-install", "virt-viewer", "qemu-img")
    missing = tuple(binary for binary in required if not shutil.which(binary))
    warnings: List[str] = []
    if not Path("/dev/kvm").exists():
        warnings.append("/dev/kvm non presente: la virtualizzazione hardware potrebbe essere disabilitata nel BIOS/UEFI.")
    elif not os.access("/dev/kvm", os.R_OK | os.W_OK):
        warnings.append("L'utente non ha accesso a /dev/kvm: la VM potrebbe non partire o usare emulazione lenta.")
    if missing:
        detail = "Mancano: " + ", ".join(missing)
        return LabSupport(False, missing, tuple(warnings), detail)
    try:
        probe = subprocess.run(
            [require_binary("virsh"), "-c", LAB_URI, "list", "--all", "--name"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=12,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return LabSupport(False, (), tuple(warnings), f"Libvirt sessione non raggiungibile: {exc}")
    if probe.returncode != 0:
        return LabSupport(False, (), tuple(warnings), (probe.stderr or probe.stdout or "Errore libvirt").strip()[-700:])
    return LabSupport(True, (), tuple(warnings), "QEMU/KVM, libvirt, virt-install e console grafica disponibili.")


def detect_image_format(image: Path) -> str:
    try:
        result = subprocess.run(
            [require_binary("qemu-img"), "info", "--output=json", str(image)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LabError(f"Impossibile analizzare l'immagine: {exc}") from exc
    if result.returncode != 0:
        raise LabError((result.stderr or "qemu-img info non riuscito").strip()[-700:])
    try:
        value = str(json.loads(result.stdout).get("format") or "").strip()
    except (ValueError, TypeError) as exc:
        raise LabError("Formato immagine non riconosciuto.") from exc
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", value):
        raise LabError("Formato immagine non sicuro o non riconosciuto.")
    return value


def create_cloud_init_files(runtime_dir: Path, config: LabConfig) -> Tuple[Path, Path]:
    # Il seed contiene una credenziale temporanea, vive in una cartella 0700 e
    # viene eliminato insieme al laboratorio.
    user_data = runtime_dir / "user-data"
    meta_data = runtime_dir / "meta-data"
    hostname = re.sub(r"[^a-z0-9-]+", "-", config.name.lower()).strip("-")[:50] or "fedtermhub-lab"
    password = config.password.replace("\\", "\\\\").replace('"', '\\"')
    username = re.sub(r"[^a-z0-9_-]", "", config.username.lower())
    if not username or not re.match(r"^[a-z_]", username):
        username = "fedlab"
    content = (
        "#cloud-config\n"
        f"hostname: {hostname}\n"
        "manage_etc_hosts: true\n"
        "ssh_pwauth: true\n"
        "users:\n"
        f"  - name: {username}\n"
        "    sudo: ALL=(ALL) NOPASSWD:ALL\n"
        "    shell: /bin/bash\n"
        "    lock_passwd: false\n"
        "chpasswd:\n"
        "  expire: false\n"
        "  users:\n"
        f"    - name: {username}\n"
        f"      password: \"{password}\"\n"
        "      type: text\n"
        "package_update: false\n"
        "final_message: 'FedTermHub Lab pronto'\n"
    )
    user_data.write_text(content, encoding="utf-8")
    meta_data.write_text(f"instance-id: {uuid.uuid4()}\nlocal-hostname: {hostname}\n", encoding="utf-8")
    os.chmod(user_data, 0o600)
    os.chmod(meta_data, 0o600)
    return user_data, meta_data


def build_virt_install_command(
    config: LabConfig,
    domain_name: str,
    overlay: Path,
    user_data: Path,
    meta_data: Path,
    virt_install_binary: Optional[str] = None,
) -> List[str]:
    network = "none" if config.network == "isolated" else "user"
    binary = virt_install_binary or shutil.which("virt-install") or "virt-install"
    return [
        binary,
        "--connect", config.connection_uri,
        "--name", domain_name,
        "--memory", str(config.memory_mb),
        "--vcpus", str(config.vcpus),
        "--import",
        "--transient",
        "--noautoconsole",
        "--osinfo", "detect=on,require=off",
        "--disk", f"path={overlay},format=qcow2,bus=virtio,cache=none",
        "--network", network,
        "--graphics", "spice,listen=none",
        "--video", "virtio",
        "--channel", "spicevmc",
        "--console", "pty,target.type=serial",
        "--cloud-init", f"user-data={user_data},meta-data={meta_data},disable=on",
    ]


def build_virt_viewer_command(
    connection_uri: str,
    domain_name: str,
    virt_viewer_binary: Optional[str] = None,
) -> List[str]:
    """Comando per aprire la console grafica della VM.

    `--attach` NON è opzionale: le VM del Lab sono create con
    `--graphics spice,listen=none`, cioè con un display privo di qualunque
    listener (né TCP né socket Unix). Un display così è raggiungibile solo
    tramite virDomainOpenGraphicsFD() di libvirt, che virt-viewer usa
    esclusivamente in modalità attach. Senza il flag, virt-viewer cerca un
    host/porta nell'XML, non lo trova e fallisce con
    "Display can only be attached through libvirt with --attach".

    `listen=none` resta la scelta giusta perché non espone nulla, né in rete
    né sul filesystem: si corregge il viewer, non la VM.
    """
    binary = virt_viewer_binary or shutil.which("virt-viewer") or "virt-viewer"
    return [binary, "--connect", connection_uri, "--attach", "--wait", domain_name]


class EphemeralLabController:
    """Gestisce il ciclo di vita delle VM usa e getta."""

    def __init__(self) -> None:
        self.instances: Dict[str, LabInstance] = {}
        self._lock = threading.RLock()
        # Impostata dalla finestra del Lab: riceve (istanza, motivo) quando la
        # console grafica non si apre, così l'errore non resta confinato in un
        # dialogo di virt-viewer che l'utente chiude senza lasciare traccia.
        self.on_viewer_error: Optional[Callable[[LabInstance, str], None]] = None
        LAB_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(LAB_ROOT, 0o700)
        except OSError:
            pass

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for item in self.instances.values() if item.state not in {"stopped", "error"})

    def list_instances(self) -> List[LabInstance]:
        with self._lock:
            return list(self.instances.values())

    def start_async(
        self,
        config: LabConfig,
        completed: Callable[[bool, str, Optional[LabInstance]], None],
        status: Optional[Callable[[str], None]] = None,
    ) -> None:
        def emit(message: str) -> None:
            if status:
                GLib.idle_add(status, message)

        def worker() -> None:
            instance: Optional[LabInstance] = None
            try:
                support = inspect_lab_support()
                if not support.ready:
                    raise LabError(support.detail)
                image = config.base_image.expanduser().resolve()
                if not image.is_file():
                    raise LabError("Seleziona un'immagine cloud QCOW2 o RAW valida.")
                if config.memory_mb < 768 or config.memory_mb > 32768:
                    raise LabError("La RAM deve essere compresa tra 768 MB e 32768 MB.")
                if config.vcpus < 1 or config.vcpus > 16:
                    raise LabError("Il numero di CPU deve essere compreso tra 1 e 16.")

                identifier = uuid.uuid4().hex
                domain_name = safe_domain_name(config.name)
                runtime_dir = LAB_ROOT / identifier
                runtime_dir.mkdir(mode=0o700)
                overlay = runtime_dir / "overlay.qcow2"
                instance = LabInstance(identifier, domain_name, runtime_dir, overlay, config)
                with self._lock:
                    self.instances[identifier] = instance

                emit("Creo il disco temporaneo…")
                base_format = detect_image_format(image)
                overlay_result = subprocess.run(
                    [require_binary("qemu-img"), "create", "-f", "qcow2", "-F", base_format, "-b", str(image), str(overlay)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if overlay_result.returncode != 0:
                    raise LabError((overlay_result.stderr or overlay_result.stdout or "Creazione overlay fallita").strip()[-900:])
                os.chmod(overlay, 0o600)
                user_data, meta_data = create_cloud_init_files(runtime_dir, config)

                emit("Avvio la VM transitoria…")
                command = build_virt_install_command(
                    config, domain_name, overlay, user_data, meta_data, require_binary("virt-install")
                )
                started = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=180,
                    check=False,
                )
                if started.returncode != 0:
                    raise LabError((started.stderr or started.stdout or "virt-install non riuscito").strip()[-1200:])

                instance.state = "running"
                emit("Apro la console grafica…")
                viewer = subprocess.Popen(
                    build_virt_viewer_command(
                        config.connection_uri, domain_name, require_binary("virt-viewer")
                    ),
                    stdout=subprocess.DEVNULL,
                    # stderr catturato invece che scartato: quando virt-viewer
                    # non riesce ad agganciare il display, il motivo esatto
                    # finisce qui e non solo in un dialogo che l'utente chiude.
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                instance.viewer = viewer
                GLib.idle_add(completed, True, "Laboratorio avviato. Chiudendo la console verrà distrutto.", instance)
                self._watch_viewer(instance)
                return
            except Exception as exc:
                if instance is not None:
                    instance.state = "error"
                    self.destroy(instance.identifier, wait=True)
                GLib.idle_add(completed, False, str(exc), None)

        threading.Thread(target=worker, daemon=True, name="fedtermhub-lab-start").start()

    def _watch_viewer(self, instance: LabInstance) -> None:
        def worker() -> None:
            viewer = instance.viewer
            reason = ""
            if viewer is not None:
                stderr_text = ""
                try:
                    # communicate() attende la fine E svuota la pipe: senza,
                    # un virt-viewer prolisso riempirebbe il buffer e si
                    # bloccherebbe, lasciando la VM in piedi per sempre.
                    if viewer.stderr is not None:
                        stderr_text = (viewer.communicate()[1] or "")
                    else:
                        viewer.wait()
                except Exception:
                    pass
                code = viewer.returncode
                # Due condizioni, entrambe necessarie:
                #   - cleanup_started falso: se la VM la stiamo smontando noi
                #     (pulsante "Termina ed elimina", chiusura dell'app), la
                #     fine del viewer è attesa, non un errore;
                #   - codice POSITIVO: un valore negativo è terminazione da
                #     segnale, cioè il nostro terminate()/kill().
                # Il tempo trascorso non è un criterio utile: virt-viewer resta
                # vivo finché l'utente non chiude il proprio dialogo di errore,
                # quindi un fallimento immediato può manifestarsi minuti dopo.
                if not instance.cleanup_started and isinstance(code, int) and code > 0:
                    reason = stderr_text.strip()[-800:] or (
                        f"virt-viewer è uscito con codice {code} senza aprire la console."
                    )
            self.destroy(instance.identifier, wait=True)
            if reason and self.on_viewer_error is not None:
                callback = self.on_viewer_error
                GLib.idle_add(lambda: (callback(instance, reason), False)[1])
        threading.Thread(target=worker, daemon=True, name=f"lab-watch-{instance.identifier[:8]}").start()

    def open_console(self, identifier: str) -> None:
        with self._lock:
            instance = self.instances.get(identifier)
        if instance is None or instance.state != "running":
            raise LabError("Il laboratorio non è più in esecuzione.")
        if instance.viewer is not None and instance.viewer.poll() is None:
            return
        instance.viewer = subprocess.Popen(
            build_virt_viewer_command(
                instance.config.connection_uri, instance.domain_name, require_binary("virt-viewer")
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self._watch_viewer(instance)

    def destroy(self, identifier: str, wait: bool = False) -> None:
        with self._lock:
            instance = self.instances.get(identifier)
            if instance is None or instance.cleanup_started:
                return
            instance.cleanup_started = True
            instance.state = "stopping"

        def cleanup() -> None:
            viewer = instance.viewer
            if viewer is not None and viewer.poll() is None:
                try:
                    viewer.terminate()
                    viewer.wait(timeout=3)
                except Exception:
                    try:
                        viewer.kill()
                    except Exception:
                        pass
            try:
                subprocess.run(
                    [require_binary("virsh"), "-c", instance.config.connection_uri, "destroy", instance.domain_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    check=False,
                )
            except Exception:
                pass
            # Una VM transitoria scompare allo shutdown/destroy. Undefine è un
            # fallback innocuo nel caso il backend abbia creato un dominio persistente.
            try:
                subprocess.run(
                    [require_binary("virsh"), "-c", instance.config.connection_uri, "undefine", instance.domain_name, "--nvram"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    check=False,
                )
            except Exception:
                pass
            shutil.rmtree(instance.runtime_dir, ignore_errors=True)
            instance.state = "stopped"
            with self._lock:
                self.instances.pop(identifier, None)

        if wait:
            cleanup()
        else:
            threading.Thread(target=cleanup, daemon=True, name=f"lab-stop-{identifier[:8]}").start()

    def stop_all(self) -> None:
        for instance in self.list_instances():
            self.destroy(instance.identifier, wait=True)


class EphemeralLabDialog(Gtk.Dialog):
    """Finestra guidata e compatta per creare laboratori Linux temporanei."""

    def __init__(self, parent: Gtk.Window, controller: EphemeralLabController) -> None:
        super().__init__(title="Lab temporaneo Linux", transient_for=parent, flags=0)
        self.parent_window = parent
        self.controller = controller
        self.set_modal(False)
        # Gruppo finestre dedicato: i dialoghi modali della finestra principale
        # non devono congelare questo pannello non modale (stessa logica della
        # finestra RDP sganciata). I propri MessageDialog (transient_for=self)
        # seguono automaticamente il gruppo.
        self._window_group = Gtk.WindowGroup()
        self._window_group.add_window(self)
        self.set_default_size(760, 620)
        self.set_resizable(True)
        self.add_button("Chiudi", Gtk.ResponseType.CLOSE)
        self.connect("response", lambda dialog, response: dialog.hide() if response == Gtk.ResponseType.CLOSE else None)
        self.connect("delete-event", lambda dialog, _event: (dialog.hide(), True)[1])

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=14)
        self.get_content_area().add(root)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name("computer-symbolic", Gtk.IconSize.DIALOG)
        icon.set_pixel_size(42)
        header.pack_start(icon, False, False, 0)
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label="Macchina Linux usa e getta", xalign=0)
        title.get_style_context().add_class("dialog-heading")
        subtitle = Gtk.Label(
            label="L'immagine base resta intatta. Chiudendo la console, VM e modifiche vengono eliminate.",
            xalign=0,
        )
        subtitle.set_line_wrap(True)
        subtitle.get_style_context().add_class("muted")
        text_box.pack_start(title, False, False, 0)
        text_box.pack_start(subtitle, False, False, 0)
        header.pack_start(text_box, True, True, 0)
        root.pack_start(header, False, False, 0)

        self.support_label = Gtk.Label(label="Controllo supporto…", xalign=0)
        self.support_label.set_line_wrap(True)
        self.support_label.get_style_context().add_class("notification-banner")
        root.pack_start(self.support_label, False, False, 0)

        form = Gtk.Grid(column_spacing=12, row_spacing=9)
        form.set_hexpand(True)
        root.pack_start(form, False, False, 0)

        self.name_entry = Gtk.Entry()
        self.name_entry.set_text("Linux test")
        form.attach(Gtk.Label(label="Nome", xalign=0), 0, 0, 1, 1)
        form.attach(self.name_entry, 1, 0, 1, 1)

        self.image_chooser = Gtk.FileChooserButton(title="Seleziona immagine Linux", action=Gtk.FileChooserAction.OPEN)
        image_filter = Gtk.FileFilter()
        image_filter.set_name("Immagini virtuali")
        for pattern in ("*.qcow2", "*.qcow", "*.img", "*.raw"):
            image_filter.add_pattern(pattern)
        self.image_chooser.add_filter(image_filter)
        form.attach(Gtk.Label(label="Immagine cloud", xalign=0), 0, 1, 1, 1)
        form.attach(self.image_chooser, 1, 1, 1, 1)

        self.preset_combo = Gtk.ComboBoxText()
        self.preset_combo.append("minimal", "Minimo — 1 CPU, 1 GiB")
        self.preset_combo.append("standard", "Standard — 2 CPU, 2 GiB")
        self.preset_combo.append("desktop", "Desktop — 2 CPU, 4 GiB")
        self.preset_combo.set_active_id("standard")
        self.preset_combo.connect("changed", self._preset_changed)
        form.attach(Gtk.Label(label="Profilo", xalign=0), 0, 2, 1, 1)
        form.attach(self.preset_combo, 1, 2, 1, 1)

        self.network_combo = Gtk.ComboBoxText()
        self.network_combo.append("isolated", "Isolata — nessun accesso di rete")
        self.network_combo.append("user", "Internet NAT — rete user-mode")
        self.network_combo.set_active_id("isolated")
        form.attach(Gtk.Label(label="Rete", xalign=0), 0, 3, 1, 1)
        form.attach(self.network_combo, 1, 3, 1, 1)

        credentials = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.username_entry = Gtk.Entry()
        self.username_entry.set_text("fedlab")
        self.password_entry = Gtk.Entry()
        self.password_entry.set_visibility(False)
        self.password_entry.set_text(generate_password())
        reveal = Gtk.ToggleButton(label="Mostra")
        reveal.connect("toggled", lambda button: self.password_entry.set_visibility(button.get_active()))
        credentials.pack_start(Gtk.Label(label="Utente"), False, False, 0)
        credentials.pack_start(self.username_entry, False, False, 0)
        credentials.pack_start(Gtk.Label(label="Password temporanea"), False, False, 0)
        credentials.pack_start(self.password_entry, True, True, 0)
        credentials.pack_start(reveal, False, False, 0)
        form.attach(Gtk.Label(label="Accesso", xalign=0), 0, 4, 1, 1)
        form.attach(credentials, 1, 4, 1, 1)

        advanced = Gtk.Expander(label="Risorse avanzate")
        resources = Gtk.Grid(column_spacing=10, row_spacing=8, margin=8)
        self.cpu_spin = Gtk.SpinButton.new_with_range(1, 16, 1)
        self.cpu_spin.set_value(2)
        self.ram_spin = Gtk.SpinButton.new_with_range(768, 32768, 256)
        self.ram_spin.set_value(2048)
        resources.attach(Gtk.Label(label="CPU virtuali", xalign=0), 0, 0, 1, 1)
        resources.attach(self.cpu_spin, 1, 0, 1, 1)
        resources.attach(Gtk.Label(label="RAM (MB)", xalign=0), 0, 1, 1, 1)
        resources.attach(self.ram_spin, 1, 1, 1, 1)
        advanced.add(resources)
        root.pack_start(advanced, False, False, 0)

        note = Gtk.Label(
            label="La modalità isolata è consigliata. Questo laboratorio non è una sandbox per analizzare malware.",
            xalign=0,
        )
        note.set_line_wrap(True)
        note.get_style_context().add_class("muted")
        root.pack_start(note, False, False, 0)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.status_label = Gtk.Label(label="", xalign=0)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        action_row.pack_start(self.status_label, True, True, 0)
        self.check_button = Gtk.Button.new_with_mnemonic("_Ricontrolla supporto")
        self.check_button.connect("clicked", lambda *_: self.refresh_support())
        self.start_button = Gtk.Button.new_with_mnemonic("_Avvia laboratorio")
        self.start_button.get_style_context().add_class("suggested-action")
        self.start_button.connect("clicked", self.on_start_clicked)
        action_row.pack_start(self.check_button, False, False, 0)
        action_row.pack_start(self.start_button, False, False, 0)
        root.pack_start(action_row, False, False, 0)

        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        root.pack_start(separator, False, False, 0)
        active_header = Gtk.Label(label="Laboratori attivi", xalign=0)
        active_header.get_style_context().add_class("dialog-heading")
        root.pack_start(active_header, False, False, 0)

        self.instance_store = Gtk.ListStore(str, str, str, str)
        self.instance_view = Gtk.TreeView(model=self.instance_store)
        for title_text, column_index in (("Nome", 0), ("Stato", 1), ("Rete", 2)):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            column = Gtk.TreeViewColumn(title_text, renderer, text=column_index)
            column.set_expand(column_index == 0)
            self.instance_view.append_column(column)
        active_scroll = Gtk.ScrolledWindow()
        active_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        active_scroll.set_min_content_height(120)
        active_scroll.add(self.instance_view)
        root.pack_start(active_scroll, True, True, 0)

        instance_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.console_button = Gtk.Button.new_with_mnemonic("Apri _console")
        self.console_button.connect("clicked", self.on_open_console)
        self.stop_button = Gtk.Button.new_with_mnemonic("_Termina ed elimina")
        self.stop_button.get_style_context().add_class("destructive-action")
        self.stop_button.connect("clicked", self.on_stop_selected)
        instance_actions.pack_end(self.stop_button, False, False, 0)
        instance_actions.pack_end(self.console_button, False, False, 0)
        root.pack_start(instance_actions, False, False, 0)

        self._timer = GLib.timeout_add_seconds(1, self.refresh_instances)
        self.controller.on_viewer_error = self._on_viewer_error
        self.connect("destroy", self._on_destroy)
        self.refresh_support()
        self.refresh_instances()
        self.show_all()

    def _on_viewer_error(self, instance: LabInstance, reason: str) -> None:
        """La console non si è aperta: la VM è già stata distrutta, qui si
        spiega all'utente il motivo invece di lasciare un laboratorio fantasma
        nell'elenco."""
        self.status_label.set_text("Console non aperta: laboratorio eliminato.")
        self.refresh_instances()
        self._show_error(
            f"Impossibile aprire la console di «{instance.config.name}».\n\n"
            f"{reason}\n\n"
            "La macchina temporanea è stata eliminata per non lasciarla attiva "
            "e irraggiungibile."
        )

    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        # Il controller sopravvive alla finestra: senza questo, un errore del
        # viewer chiamerebbe metodi su widget già distrutti.
        if self.controller.on_viewer_error == self._on_viewer_error:
            self.controller.on_viewer_error = None
        if getattr(self, "_timer", 0):
            try:
                GLib.source_remove(self._timer)
            except Exception:
                pass
            self._timer = 0

    def _preset_changed(self, _combo: Gtk.ComboBoxText) -> None:
        values = {
            "minimal": (1, 1024),
            "standard": (2, 2048),
            "desktop": (2, 4096),
        }
        cpu, ram = values.get(self.preset_combo.get_active_id() or "standard", (2, 2048))
        self.cpu_spin.set_value(cpu)
        self.ram_spin.set_value(ram)

    def refresh_support(self) -> None:
        self.support_label.set_text("Controllo supporto virtualizzazione…")
        self.start_button.set_sensitive(False)

        def worker() -> None:
            support = inspect_lab_support()
            GLib.idle_add(self._apply_support, support)
        threading.Thread(target=worker, daemon=True, name="lab-support").start()

    def _apply_support(self, support: LabSupport) -> bool:
        if support.ready:
            text = "✓ " + support.detail
            if support.warnings:
                text += "\nAttenzione: " + " ".join(support.warnings)
        else:
            text = "Supporto non pronto: " + support.detail
            if support.missing:
                text += "\nInstalla: sudo dnf install libvirt-daemon-kvm virt-install virt-viewer qemu-img"
        self.support_label.set_text(text)
        self.start_button.set_sensitive(support.ready)
        return False

    def _selected_identifier(self) -> Optional[str]:
        model, tree_iter = self.instance_view.get_selection().get_selected()
        return str(model.get_value(tree_iter, 3)) if tree_iter is not None else None

    def refresh_instances(self) -> bool:
        selected = self._selected_identifier()
        self.instance_store.clear()
        select_path = None
        for index, instance in enumerate(self.controller.list_instances()):
            network = "Isolata" if instance.config.network == "isolated" else "Internet NAT"
            self.instance_store.append([instance.config.name, instance.state, network, instance.identifier])
            if instance.identifier == selected:
                select_path = Gtk.TreePath.new_from_indices([index])
        if select_path is not None:
            self.instance_view.get_selection().select_path(select_path)
        has_items = len(self.instance_store) > 0
        self.console_button.set_sensitive(has_items)
        self.stop_button.set_sensitive(has_items)
        return True

    def on_start_clicked(self, _button: Gtk.Button) -> None:
        filename = self.image_chooser.get_filename()
        if not filename:
            self._show_error("Seleziona un'immagine cloud Linux in formato QCOW2, RAW o IMG.")
            return
        config = LabConfig(
            name=self.name_entry.get_text().strip() or "Linux test",
            base_image=Path(filename),
            memory_mb=int(self.ram_spin.get_value()),
            vcpus=int(self.cpu_spin.get_value()),
            network=self.network_combo.get_active_id() or "isolated",
            username=self.username_entry.get_text().strip() or "fedlab",
            password=self.password_entry.get_text() or generate_password(),
        )
        self.start_button.set_sensitive(False)
        self.status_label.set_text("Preparazione laboratorio…")

        def completed(ok: bool, message: str, _instance: Optional[LabInstance]) -> bool:
            self.start_button.set_sensitive(True)
            self.status_label.set_text(message)
            self.refresh_instances()
            if not ok:
                self._show_error(message)
            else:
                self.password_entry.set_text(generate_password())
            return False

        self.controller.start_async(config, completed, lambda text: self.status_label.set_text(text))

    def on_open_console(self, _button: Gtk.Button) -> None:
        identifier = self._selected_identifier()
        if not identifier:
            return
        try:
            self.controller.open_console(identifier)
        except Exception as exc:
            self._show_error(str(exc))

    def on_stop_selected(self, _button: Gtk.Button) -> None:
        identifier = self._selected_identifier()
        if not identifier:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text="Terminare il laboratorio?",
        )
        dialog.format_secondary_text("La VM e tutte le modifiche nel disco temporaneo verranno eliminate definitivamente.")
        dialog.add_buttons("Annulla", Gtk.ResponseType.CANCEL, "Termina ed elimina", Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self.controller.destroy(identifier)
            self.status_label.set_text("Arresto e pulizia in corso…")

    def _show_error(self, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text="Lab temporaneo",
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()
