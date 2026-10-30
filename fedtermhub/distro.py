# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Differenze fra distribuzioni Linux: gestore dei pacchetti, nomi dei pacchetti, servizi, firewall.

FedTermHub gira su quattro famiglie:

    fedora   Fedora, RHEL, AlmaLinux, Rocky, CentOS Stream (dnf; rpm-ostree sui sistemi immutabili)
    debian   Debian, Ubuntu, Linux Mint, Pop!_OS, Zorin… (apt)
    suse     openSUSE Tumbleweed e Leap (zypper)
    arch     Arch, Manjaro, EndeavourOS… (pacman)

Nel codice i pacchetti si indicano con il nome Fedora; qui sotto c'è come si chiamano altrove. Così un
messaggio come «Installa: sudo dnf install sshpass» diventa «sudo apt install sshpass» su Ubuntu senza
che chi scrive il messaggio debba conoscere tutte le distribuzioni.

FEDTERMHUB_DISTRO=fedora|debian|suse|arch forza la famiglia (per i test e per chi ha un sistema derivato
che non si riconosce da /etc/os-release).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

FAMILIES = ("fedora", "debian", "suse", "arch")

# Nome Fedora -> (Debian/Ubuntu, openSUSE, Arch). None: non esiste come pacchetto in quella famiglia.
# Più nomi separati da spazio: servono tutti.
PACKAGES: Dict[str, Tuple[Optional[str], Optional[str], Optional[str]]] = {
    # interfaccia
    "python3": ("python3", "python3", "python"),
    "python3-gobject": ("python3-gi", "python3-gobject python3-gobject-Gdk", "python-gobject"),
    "gtk3": ("gir1.2-gtk-3.0", "typelib-1_0-Gtk-3_0", "gtk3"),
    "vte291": ("gir1.2-vte-2.91", "typelib-1_0-Vte-2_91", "vte3"),
    "python3-cairo": ("python3-gi-cairo", "python3-gobject-cairo", "python-cairo"),
    "python3-cryptography": ("python3-cryptography", "python3-cryptography", "python-cryptography"),
    "python3-pyserial": ("python3-serial", "python3-pyserial", "python-pyserial"),
    "python3-winrm": ("python3-winrm", "python3-pywinrm", "python-pywinrm"),
    "python3-defusedxml": ("python3-defusedxml", "python3-defusedxml", "python-defusedxml"),
    # connessioni
    "fedtermhub-rdp": ("fedtermhub-rdp", "fedtermhub-rdp", None),
    "openssh-clients": ("openssh-client", "openssh-clients", "openssh"),
    "sshpass": ("sshpass", "sshpass", "sshpass"),
    "telnet": ("telnet", "telnet", "inetutils"),
    "tigervnc": ("tigervnc-viewer", "tigervnc", "tigervnc"),
    "freerdp": ("freerdp3-x11", "freerdp", "freerdp"),
    "freerdp-devel": ("freerdp3-dev", "freerdp-devel", "freerdp"),
    "xorg-x11-server-Xwayland": ("xwayland", "xwayland", "xorg-xwayland"),
    "wl-clipboard": ("wl-clipboard", "wl-clipboard", "wl-clipboard"),
    "xclip": ("xclip", "xclip", "xclip"),
    # strumenti di rete
    "nmap": ("nmap", "nmap", "nmap"),
    "tcpdump": ("tcpdump", "tcpdump", "tcpdump"),
    "iproute": ("iproute2", "iproute2", "iproute2"),
    "openssh-server": ("openssh-server", "openssh-server", "openssh"),
    "vsftpd": ("vsftpd", "vsftpd", "vsftpd"),
    "tftp-server": ("tftpd-hpa", "tftp", "tftp-hpa"),
    "telnet-server": (None, "telnet-server", "inetutils"),
    "nfs-utils": ("nfs-kernel-server", "nfs-kernel-server", "nfs-utils"),
    "tigervnc-x11-server": ("tigervnc-scraping-server", "tigervnc-x11vnc", "tigervnc"),
    "tigervnc-wayland-server": ("tigervnc-scraping-server", None, "tigervnc"),
    "tigervnc-server-common": ("tigervnc-tools", "tigervnc", "tigervnc"),
    "firewalld": ("firewalld", "firewalld", "firewalld"),
    # laboratorio
    "libvirt-daemon-kvm": ("libvirt-daemon-system", "libvirt-daemon-qemu", "libvirt"),
    "libvirt-client": ("libvirt-clients", "libvirt-client", "libvirt"),
    "virt-install": ("virtinst", "virt-install", "virt-install"),
    "virt-viewer": ("virt-viewer", "virt-viewer", "virt-viewer"),
    "qemu-img": ("qemu-utils", "qemu-tools", "qemu-img"),
    "util-linux": ("util-linux", "util-linux", "util-linux"),
    "glibc-common": ("libc-bin", "glibc", "glibc"),
    "NetworkManager": ("network-manager", "NetworkManager", "networkmanager"),
}

# Unità systemd dei servizi locali, per famiglia (stesso ordine: fedora, debian, suse, arch).
SERVICE_UNITS: Dict[str, Tuple[Optional[str], ...]] = {
    "ssh": ("sshd.service", "ssh.service", "sshd.service", "sshd.service"),
    "ftp": ("vsftpd.service", "vsftpd.service", "vsftpd.service", "vsftpd.service"),
    "tftp": ("tftp.socket", "tftpd-hpa.service", "tftp.socket", "tftpd.socket"),
    # Debian e Ubuntu non hanno più un server telnet avviato da systemd (solo inetd).
    "telnet": ("telnet.socket", None, "telnet.socket", "telnet.socket"),
    "nfs": ("nfs-server.service", "nfs-server.service", "nfs-server.service", "nfs-server.service"),
}

# Cartella servita dal server TFTP del pacchetto di ogni famiglia.
TFTP_ROOTS = {"fedora": "/var/lib/tftpboot", "debian": "/srv/tftp", "suse": "/srv/tftpboot", "arch": "/srv/tftp"}


def _read_os_release() -> Dict[str, str]:
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        values: Dict[str, str] = {}
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep and key and not key.startswith("#"):
                values[key.strip()] = value.strip().strip('"').strip("'")
        return values
    return {}


@lru_cache(maxsize=1)
def os_release() -> Dict[str, str]:
    return _read_os_release()


def family_from(release: Dict[str, str]) -> Optional[str]:
    """La famiglia indicata da ID e ID_LIKE di /etc/os-release, se riconoscibile."""
    ids = [release.get("ID", "")] + release.get("ID_LIKE", "").split()
    for value in (item.lower() for item in ids if item):
        if value in {"fedora", "rhel", "centos", "rocky", "almalinux", "ol", "amzn"}:
            return "fedora"
        if value in {"debian", "ubuntu", "linuxmint", "pop", "elementary", "zorin", "raspbian", "kali"}:
            return "debian"
        if value.startswith("opensuse") or value in {"suse", "sles", "sled"}:
            return "suse"
        if value in {"arch", "archlinux", "manjaro", "endeavouros", "cachyos", "garuda"}:
            return "arch"
    return None


@lru_cache(maxsize=1)
def family() -> str:
    forced = os.environ.get("FEDTERMHUB_DISTRO", "").strip().lower()
    if forced in FAMILIES:
        return forced
    found = family_from(os_release())
    if found:
        return found
    # Derivata sconosciuta: decide il gestore dei pacchetti presente.
    for binary, name in (("dnf", "fedora"), ("apt-get", "debian"), ("zypper", "suse"), ("pacman", "arch")):
        if shutil.which(binary):
            return name
    return "fedora"


def is_ostree() -> bool:
    """Fedora Silverblue, Kinoite, Bazzite…: si installa con rpm-ostree e serve un riavvio."""
    return family() == "fedora" and Path("/run/ostree-booted").exists() and bool(shutil.which("rpm-ostree"))


def pretty_name() -> str:
    release = os_release()
    return release.get("PRETTY_NAME") or release.get("NAME") or "Linux"


def package_names(*fedora_names: str) -> List[str]:
    """I nomi dei pacchetti nella famiglia corrente; quelli che non esistono vengono saltati."""
    current = family()
    names: List[str] = []
    for name in fedora_names:
        if current == "fedora":
            mapped: Optional[str] = name
        else:
            mapped = PACKAGES.get(name, (name, name, name))[FAMILIES.index(current) - 1]
        for item in (mapped or "").split():
            if item not in names:
                names.append(item)
    return names


def package_name(fedora_name: str) -> str:
    """Il nome del pacchetto nella famiglia corrente (stringa vuota se non esiste)."""
    return " ".join(package_names(fedora_name))


def install_command(*fedora_names: str) -> str:
    """Il comando da dare in un terminale, per esempio «sudo apt install sshpass»."""
    names = " ".join(package_names(*fedora_names))
    if not names:
        return ""
    current = family()
    if current == "debian":
        return f"sudo apt install {names}"
    if current == "suse":
        return f"sudo zypper install {names}"
    if current == "arch":
        return f"sudo pacman -S {names}"
    if is_ostree():
        return f"sudo rpm-ostree install {names}"
    return f"sudo dnf install {names}"


def install_argv(names: Sequence[str]) -> List[str]:
    """Il comando (senza sudo/pkexec) che installa i pacchetti senza fare domande."""
    current = family()
    if current == "debian":
        return ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", *names]
    if current == "suse":
        return ["zypper", "--non-interactive", "install", *names]
    if current == "arch":
        return ["pacman", "-S", "--needed", "--noconfirm", *names]
    if is_ostree():
        return ["rpm-ostree", "install", "--idempotent", *names]
    return ["dnf", "install", "-y", *names]


def package_installed(name: str, timeout: float = 5) -> bool:
    """True se il pacchetto è installato. Se non si può verificare si considera assente."""
    current = family()
    if current == "debian":
        command = ["dpkg-query", "-W", "-f=${Status}", name]
    elif current == "arch":
        command = ["pacman", "-Q", name]
    else:
        command = ["rpm", "-q", name]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return current != "debian" or "install ok installed" in proc.stdout


def service_unit(key: str) -> Optional[str]:
    """L'unità systemd del servizio nella famiglia corrente (None se lì non c'è)."""
    units = SERVICE_UNITS.get(key)
    if not units:
        return None
    return units[FAMILIES.index(family())]


def serial_group() -> str:
    """Il gruppo che dà accesso alle porte seriali: uucp su Arch, dialout altrove."""
    return "uucp" if family() == "arch" else "dialout"


def tftp_root() -> str:
    return TFTP_ROOTS.get(family(), "/var/lib/tftpboot")


def firewall_tool() -> Optional[str]:
    """«firewalld» (Fedora, RHEL, openSUSE…), «ufw» (Ubuntu, Mint) oppure None."""
    if shutil.which("firewall-cmd"):
        return "firewalld"
    if shutil.which("ufw"):
        return "ufw"
    return None


def firewall_open_commands(ports: Sequence[str], firewalld_services: Sequence[str] = ()) -> List[List[str]]:
    """Comandi che aprono le porte («22/tcp») nel firewall presente; lista vuota se non ce n'è uno.

    Con firewalld si usano i servizi predefiniti quando ci sono (aprono anche le porte accessorie, per
    esempio mountd e rpc-bind per NFS); con ufw si aprono le porte una per una.
    """
    tool = firewall_tool()
    if tool == "firewalld":
        rules = [["firewall-cmd", "--permanent", f"--add-service={service}"] for service in firewalld_services]
        if not rules:
            rules = [["firewall-cmd", "--permanent", f"--add-port={port}"] for port in ports]
        return rules + [["firewall-cmd", "--reload"]]
    if tool == "ufw":
        return [["ufw", "allow", port] for port in ports]
    return []
