#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Differenze fra distribuzioni (fedtermhub/distro.py), senza bisogno di avere quelle distribuzioni.

Controlla che:
 - la famiglia si riconosca da /etc/os-release (ID e ID_LIKE) per le distribuzioni supportate e le
   derivate più diffuse;
 - i nomi dei pacchetti e i comandi d'installazione mostrati all'utente siano quelli giusti per
   ciascuna famiglia;
 - servizi, cartella TFTP, gruppo delle porte seriali e regole del firewall cambino con la famiglia;
 - ogni pacchetto Fedora citato nel codice (install_command, package_name…) abbia la sua riga nella
   tabella, così un messaggio non suggerisce su Ubuntu un nome che esiste solo su Fedora.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "fedtermhub"))

import distro  # noqa: E402

RELEASES = {
    "fedora": [
        {"ID": "fedora", "VERSION_ID": "44"},
        {"ID": "almalinux", "ID_LIKE": "rhel centos fedora"},
        {"ID": "rocky", "ID_LIKE": "rhel centos fedora"},
        {"ID": "rhel", "ID_LIKE": "fedora"},
        {"ID": "centos", "ID_LIKE": "rhel fedora"},
    ],
    "debian": [
        {"ID": "debian", "VERSION_ID": "12"},
        {"ID": "ubuntu", "ID_LIKE": "debian"},
        {"ID": "linuxmint", "ID_LIKE": "ubuntu debian"},
        {"ID": "pop", "ID_LIKE": "ubuntu debian"},
        {"ID": "zorin", "ID_LIKE": "ubuntu"},
        {"ID": "neon", "ID_LIKE": "ubuntu debian"},
    ],
    "suse": [
        {"ID": "opensuse-tumbleweed", "ID_LIKE": "opensuse suse"},
        {"ID": "opensuse-leap", "ID_LIKE": "suse opensuse"},
        {"ID": "sles", "ID_LIKE": "suse"},
    ],
    "arch": [
        {"ID": "arch"},
        {"ID": "manjaro", "ID_LIKE": "arch"},
        {"ID": "endeavouros", "ID_LIKE": "arch"},
    ],
}


def use_family(name: str) -> None:
    os.environ["FEDTERMHUB_DISTRO"] = name
    distro.family.cache_clear()


def check_detection() -> None:
    for expected, releases in RELEASES.items():
        for release in releases:
            found = distro.family_from(release)
            assert found == expected, f"{release} riconosciuta come {found}, attesa {expected}"
    assert distro.family_from({"ID": "qualcosa-di-nuovo"}) is None
    # una derivata con ID sconosciuto ma ID_LIKE noto
    assert distro.family_from({"ID": "linuxlite", "ID_LIKE": "ubuntu"}) == "debian"
    print("  riconoscimento: Fedora/RHEL, Debian/Ubuntu/Mint, openSUSE e Arch con le derivate")


def check_commands() -> None:
    expected = {
        "fedora": "sudo dnf install sshpass",
        "debian": "sudo apt install sshpass",
        "suse": "sudo zypper install sshpass",
        "arch": "sudo pacman -S sshpass",
    }
    for family, command in expected.items():
        use_family(family)
        assert distro.install_command("sshpass") == command, (family, distro.install_command("sshpass"))
    use_family("debian")
    assert distro.install_command("python3-gobject", "gtk3", "vte291") == \
        "sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91"
    assert distro.package_name("tigervnc") == "tigervnc-viewer"
    assert distro.package_name("telnet-server") == "", "su Debian non c'è un server telnet con systemd"
    assert distro.install_command("telnet-server") == ""
    assert distro.install_argv(["sshpass"])[-1] == "sshpass"
    assert "DEBIAN_FRONTEND=noninteractive" in distro.install_argv(["sshpass"])
    use_family("suse")
    assert distro.install_command("python3-gobject") == "sudo zypper install python3-gobject python3-gobject-Gdk"
    use_family("arch")
    assert distro.package_name("vte291") == "vte3"
    print("  comandi d'installazione e nomi dei pacchetti per apt, dnf, zypper e pacman")


def check_services() -> None:
    use_family("fedora")
    assert distro.service_unit("ssh") == "sshd.service"
    assert distro.tftp_root() == "/var/lib/tftpboot"
    assert distro.serial_group() == "dialout"
    use_family("debian")
    assert distro.service_unit("ssh") == "ssh.service"
    assert distro.service_unit("tftp") == "tftpd-hpa.service"
    assert distro.service_unit("telnet") is None
    assert distro.tftp_root() == "/srv/tftp"
    use_family("arch")
    assert distro.serial_group() == "uucp"

    original = distro.shutil.which
    try:
        distro.shutil.which = lambda name: "/usr/sbin/ufw" if name == "ufw" else None
        rules = distro.firewall_open_commands(["22/tcp"], ["ssh"])
        assert rules == [["ufw", "allow", "22/tcp"]], rules
        distro.shutil.which = lambda name: "/usr/bin/firewall-cmd" if name == "firewall-cmd" else None
        rules = distro.firewall_open_commands(["2049/tcp"], ["nfs", "mountd"])
        assert rules[-1] == ["firewall-cmd", "--reload"] and "--add-service=mountd" in rules[1], rules
        distro.shutil.which = lambda name: None
        assert distro.firewall_open_commands(["22/tcp"]) == []
    finally:
        distro.shutil.which = original
    print("  servizi systemd, cartella TFTP, gruppo seriale e firewall (firewalld, ufw, nessuno)")


def check_every_package_is_mapped() -> None:
    """Ogni nome passato a install_command()/package_name() nel codice ha la sua riga nella tabella."""
    calls = {"install_command", "package_name", "package_names"}
    missing = []
    for path in sorted((PROJECT_ROOT / "fedtermhub").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in calls:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value not in distro.PACKAGES:
                        missing.append(f"{path.name}:{node.lineno} {arg.value}")
    # anche i pacchetti dei servizi e della tabella del doctor (tuple binario -> pacchetto)
    source = (PROJECT_ROOT / "fedtermhub" / "fedtermhub.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith('("') and stripped.endswith('"),') and stripped.count('"') == 4:
            binary, package = stripped[2:-3].split('", "')
            if binary.replace("-", "").replace(".", "").isalnum() and package and " " not in package \
                    and package not in distro.PACKAGES and package[0].islower():
                missing.append(f"fedtermhub.py doctor {binary} -> {package}")
    assert not missing, "pacchetti senza riga in distro.PACKAGES:\n" + "\n".join(missing)
    print("  ogni pacchetto citato nel codice ha i nomi per tutte le famiglie")


def main() -> int:
    saved = os.environ.get("FEDTERMHUB_DISTRO")
    try:
        check_detection()
        check_commands()
        check_services()
        check_every_package_is_mapped()
    finally:
        if saved is None:
            os.environ.pop("FEDTERMHUB_DISTRO", None)
        else:
            os.environ["FEDTERMHUB_DISTRO"] = saved
        distro.family.cache_clear()
    print("OK — distribuzioni: riconoscimento, pacchetti, servizi e firewall")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
