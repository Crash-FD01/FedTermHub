#!/bin/sh
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Aggiunge il repository firmato di FedTermHub al sistema e installa FedTermHub. Da lì gli aggiornamenti
# arrivano insieme a quelli del sistema: «sudo apt upgrade», «sudo dnf upgrade», «sudo zypper update».
#
#   curl -fsSL @SITE_URL@/setup.sh | sudo sh
#   curl -fsSL @SITE_URL@/setup.sh | sudo sh -s -- --remove     (toglie FedTermHub e il repository)
#
# Funziona su Debian 12+, Ubuntu 22.04+, Linux Mint 21+ e derivate, Fedora 43+ (anche Silverblue e
# Kinoite), RHEL/AlmaLinux/Rocky 9+, openSUSE Tumbleweed e Leap 16+, su x86_64 e aarch64.
# Leggilo pure prima di eseguirlo: fa solo quello che c'è scritto qui sotto.
set -eu

SITE="@SITE_URL@"
SOURCE="@REPO_URL@"
MIN_FEDORA=43
MIN_GLIBC=2.34

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf 'Errore: %s\n' "$*" >&2; exit 1; }
from_source() {
    die "$1. Puoi installarlo dai sorgenti: git clone $SOURCE.git && cd FedTermHub && ./install.sh"
}

[ "$(id -u)" = 0 ] || die "servono i permessi di amministratore: curl -fsSL $SITE/setup.sh | sudo sh"

fetch() {  # fetch URL FILE
    if command -v curl > /dev/null 2>&1; then curl -fsSL "$1" -o "$2"
    elif command -v wget > /dev/null 2>&1; then wget -q "$1" -O "$2"
    else die "manca curl (o wget) per scaricare $1"; fi
}

# ------------------------------------------------------------------ che sistema è
ID="" ID_LIKE="" VERSION_ID="" PRETTY_NAME=""
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
fi
family=""
for id in $ID $ID_LIKE; do
    case "$id" in
        fedora) family=fedora ;;
        rhel|centos|rocky|almalinux|ol) family=el ;;
        debian|ubuntu|linuxmint|pop|zorin|elementary|raspbian|kali) family=debian ;;
        opensuse*|suse|sles|sled) family=suse ;;
        arch|archlinux|manjaro|endeavouros|cachyos|garuda) family=arch ;;
    esac
    [ -n "$family" ] && break
done
# RHEL e derivate dichiarano ID_LIKE="rhel centos fedora": è l'ID a dire se è Fedora vera.
[ "$family" = fedora ] && [ "$ID" != fedora ] && family=el
ostree=no
if [ -e /run/ostree-booted ] && command -v rpm-ostree > /dev/null 2>&1; then ostree=yes; fi
major="${VERSION_ID%%.*}"
name="${PRETTY_NAME:-questo sistema}"

if [ "${1:-}" = "--remove" ]; then
    say "Rimuovo FedTermHub e il suo repository"
    if command -v apt-get > /dev/null 2>&1; then
        apt-get remove -y fedtermhub fedtermhub-rdp || true
        rm -f /etc/apt/sources.list.d/fedtermhub.list /etc/apt/keyrings/fedtermhub.asc
    elif [ "$ostree" = yes ]; then
        rpm-ostree uninstall fedtermhub || true
        rm -f /etc/yum.repos.d/fedtermhub.repo
    elif command -v dnf > /dev/null 2>&1; then
        dnf remove -y fedtermhub fedtermhub-rdp || true
        rm -f /etc/yum.repos.d/fedtermhub.repo
    elif command -v zypper > /dev/null 2>&1; then
        zypper --non-interactive remove fedtermhub fedtermhub-rdp || true
        zypper --non-interactive removerepo fedtermhub || true
    fi
    say "Fatto. Le tue sessioni restano in ~/.config/fedtermhub."
    exit 0
fi

# ------------------------------------------------------------------ il pacchetto funziona qui?
case "$(uname -m)" in
    x86_64|aarch64) ;;
    *) from_source "i pacchetti ci sono per x86_64 e aarch64, qui c'è $(uname -m)" ;;
esac
case "$family" in
    fedora)
        if [ "$ostree" = no ] && ! command -v dnf > /dev/null 2>&1; then die "non trovo dnf su $name"; fi
        if [ "${major:-0}" -lt "$MIN_FEDORA" ] 2> /dev/null; then
            die "serve Fedora $MIN_FEDORA o più recente (qui: $name). Aggiorna il sistema."
        fi
        ;;
    el)
        [ "${major:-0}" -ge 9 ] 2> /dev/null || from_source "serve la versione 9 o più recente (qui: $name)"
        ;;
    debian)
        case "$ID" in
            debian) [ "${major:-0}" -ge 12 ] 2> /dev/null || die "serve Debian 12 o più recente (qui: $name)" ;;
            ubuntu) [ "${major:-0}" -ge 22 ] 2> /dev/null || die "serve Ubuntu 22.04 o più recente (qui: $name)" ;;
        esac
        ;;
    suse)
        if [ "$ID" = opensuse-leap ] && [ "${major:-0}" -lt 16 ] 2> /dev/null; then
            die "serve openSUSE Leap 16 o Tumbleweed (Leap 15 ha Python 3.6, troppo vecchio). Qui: $name"
        fi
        ;;
    arch) from_source "per Arch e derivate non ci sono pacchetti" ;;
    *) from_source "non riconosco la distribuzione ($name)" ;;
esac
# I pacchetti per Debian, Ubuntu, RHEL e openSUSE contengono un motore RDP compilato per glibc $MIN_GLIBC.
have="$(getconf GNU_LIBC_VERSION 2> /dev/null | awk '{print $2}')"
if [ "$family" != fedora ]; then
    [ -n "$have" ] || from_source "il pacchetto richiede glibc (questo sistema usa un'altra libreria C)"
    if [ "$(printf '%s\n%s\n' "$MIN_GLIBC" "$have" | sort -V | head -n1)" != "$MIN_GLIBC" ]; then
        from_source "il pacchetto richiede glibc $MIN_GLIBC o più recente, qui c'è la $have"
    fi
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fetch "$SITE/fedtermhub.asc" "$tmp/fedtermhub.asc"
grep -q "BEGIN PGP PUBLIC KEY BLOCK" "$tmp/fedtermhub.asc" || die "la chiave scaricata da $SITE/fedtermhub.asc non è valida"

case "$family" in
    debian)  # ---------------------------------------------- Debian, Ubuntu, Mint, Pop!_OS, Zorin…
        say "Aggiungo il repository APT di FedTermHub"
        install -d -m 755 /etc/apt/keyrings
        install -m 644 "$tmp/fedtermhub.asc" /etc/apt/keyrings/fedtermhub.asc
        echo "deb [signed-by=/etc/apt/keyrings/fedtermhub.asc] $SITE/apt ./" > /etc/apt/sources.list.d/fedtermhub.list
        apt-get update
        say "Installo FedTermHub"
        DEBIAN_FRONTEND=noninteractive apt-get install -y fedtermhub
        upgrade="sudo apt update && sudo apt upgrade"
        ;;
    fedora)  # ---------------------------------------------- Fedora (anche Silverblue, Kinoite, Bazzite…)
        say "Aggiungo il repository di FedTermHub"
        fetch "$SITE/fedtermhub.repo" /etc/yum.repos.d/fedtermhub.repo
        rpm --import "$tmp/fedtermhub.asc"
        if [ "$ostree" = yes ]; then
            say "Installo FedTermHub (sistema immutabile: sarà attivo dopo il riavvio)"
            rpm-ostree install --idempotent fedtermhub
            upgrade="rpm-ostree upgrade (o gli aggiornamenti automatici del sistema)"
        else
            say "Installo FedTermHub"
            dnf install -y fedtermhub
            upgrade="sudo dnf upgrade"
        fi
        ;;
    el)  # -------------------------------------------------- RHEL, AlmaLinux, Rocky, CentOS Stream
        say "Aggiungo il repository di FedTermHub"
        fetch "$SITE/fedtermhub-universal.repo" /etc/yum.repos.d/fedtermhub.repo
        rpm --import "$tmp/fedtermhub.asc"
        say "Installo FedTermHub"
        dnf install -y fedtermhub
        upgrade="sudo dnf upgrade"
        ;;
    suse)  # ------------------------------------------------ openSUSE Tumbleweed, Leap 16
        say "Aggiungo il repository di FedTermHub"
        rpm --import "$tmp/fedtermhub.asc"
        zypper --non-interactive removerepo fedtermhub > /dev/null 2>&1 || true
        zypper --non-interactive addrepo --refresh "$SITE/fedtermhub-universal.repo"
        zypper --non-interactive --gpg-auto-import-keys refresh fedtermhub
        say "Installo FedTermHub"
        zypper --non-interactive install fedtermhub
        upgrade="sudo zypper refresh && sudo zypper update"
        ;;
esac

say "✓ FedTermHub installato. Lo trovi nel menu delle applicazioni, o avvialo con: fedtermhub"
echo "  Gli aggiornamenti arrivano con quelli del sistema: $upgrade"
echo "  Per toglierlo: curl -fsSL $SITE/setup.sh | sudo sh -s -- --remove"
