#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Installazione dai sorgenti nella cartella utente (~/.local), senza pacchetti.
#
# Serve dove non ci sono i pacchetti (Arch, Manjaro, EndeavourOS e altre distribuzioni, architetture
# diverse da x86_64 e aarch64) e a chi sviluppa. Sulle distribuzioni supportate conviene il comando
# del README, che aggiunge il repository e porta gli aggiornamenti con quelli del sistema.
#
# Installa le dipendenze con il gestore della distribuzione (dnf, apt, zypper o pacman) e compila il
# motore RDP nativo: contro la FreeRDP 3 del sistema quando c'è, altrimenti contro una copia di FreeRDP 3
# compilata qui e messa accanto al motore (packaging/build-freerdp.sh).
set -euo pipefail

APP_VERSION="0.9.13"
APPDIR="${HOME}/.local/share/fedtermhub"
BINDIR="${HOME}/.local/bin"
LIBEXECDIR="${HOME}/.local/libexec/fedtermhub"
DESKTOPDIR="${HOME}/.local/share/applications"
ICONROOT="${HOME}/.local/share/icons/hicolor"
PIXMAPDIR="${HOME}/.local/share/pixmaps"
DESKTOP_ID="io.github.crash_fd01.fedtermhub"
NO_DEPS=0
WITH_SERVERS=0
WITH_LAB=0
BUNDLE_FREERDP=0

for arg in "$@"; do
  case "$arg" in
    --no-deps) NO_DEPS=1 ;;
    --with-servers) WITH_SERVERS=1 ;;
    --with-lab) WITH_LAB=1 ;;
    --bundled-freerdp) BUNDLE_FREERDP=1 ;;
    -h|--help)
      echo "Uso: ./install.sh [--no-deps] [--with-servers] [--with-lab] [--bundled-freerdp]"
      echo "  --no-deps          non installa dipendenze (le hai già)"
      echo "  --with-servers     anche i server degli Strumenti di rete (SSH, FTP, TFTP, NFS, VNC)"
      echo "  --with-lab         anche libvirt e QEMU per il laboratorio Linux usa e getta"
      echo "  --bundled-freerdp  compila una copia di FreeRDP 3 anche se il sistema ne ha una"
      exit 0
      ;;
    *) echo "Argomento sconosciuto: $arg" >&2; exit 2 ;;
  esac
done
cd "$(dirname "$0")"

# ------------------------------------------------------------------ distribuzione
FAMILY=""
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  for id in ${ID:-} ${ID_LIKE:-}; do
    case "$id" in
      fedora|rhel|centos|rocky|almalinux|ol) FAMILY=fedora ;;
      debian|ubuntu|linuxmint|pop|zorin|elementary|raspbian|kali) FAMILY=debian ;;
      opensuse*|suse|sles|sled) FAMILY=suse ;;
      arch|archlinux|manjaro|endeavouros|cachyos|garuda) FAMILY=arch ;;
    esac
    [[ -n "$FAMILY" ]] && break
  done
fi
if [[ -z "$FAMILY" ]]; then
  if command -v dnf > /dev/null; then FAMILY=fedora
  elif command -v apt-get > /dev/null; then FAMILY=debian
  elif command -v zypper > /dev/null; then FAMILY=suse
  elif command -v pacman > /dev/null; then FAMILY=arch
  fi
fi
echo "Sistema: ${PRETTY_NAME:-sconosciuto} (famiglia: ${FAMILY:-sconosciuta})"

# Pacchetti indispensabili (l'installazione si ferma se mancano) e facoltativi (installati se esistono).
# Per la compilazione di FreeRDP: le stesse librerie che packaging/build-freerdp.sh si aspetta.
case "$FAMILY" in
  fedora)
    REQUIRED="python3 python3-gobject python3-cairo gtk3 vte291 python3-cryptography openssh-clients iproute
      adwaita-icon-theme gcc make pkgconf-pkg-config"
    FREERDP_SYSTEM="freerdp-devel"
    FREERDP_BUILD="cmake ninja-build openssl-devel zlib-devel krb5-devel pulseaudio-libs-devel cups-devel
      pcsc-lite-devel fuse3-devel fuse3"
    OPTIONAL="freerdp tigervnc python3-defusedxml python3-pyserial python3-winrm sshpass nmap tcpdump telnet
      wl-clipboard xclip papirus-icon-theme"
    SERVERS="openssh-server vsftpd tftp-server telnet-server nfs-utils firewalld tigervnc-server-common
      tigervnc-x11-server tigervnc-wayland-server"
    LAB="libvirt-daemon-kvm libvirt-client virt-install virt-viewer qemu-img"
    ;;
  debian)
    REQUIRED="python3 python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-vte-2.91 python3-cryptography
      openssh-client iproute2 adwaita-icon-theme gcc make pkg-config"
    FREERDP_SYSTEM="freerdp3-dev"
    FREERDP_BUILD="cmake ninja-build libssl-dev zlib1g-dev libkrb5-dev libpulse-dev libcups2-dev
      libpcsclite-dev libfuse3-dev fuse3 curl"
    OPTIONAL="freerdp3-x11 tigervnc-viewer python3-defusedxml python3-serial python3-winrm sshpass nmap tcpdump
      telnet wl-clipboard xclip papirus-icon-theme pkexec"
    SERVERS="openssh-server vsftpd tftpd-hpa nfs-kernel-server ufw tigervnc-scraping-server tigervnc-tools"
    LAB="libvirt-daemon-system libvirt-clients virtinst virt-viewer qemu-utils"
    ;;
  suse)
    REQUIRED="python3 python3-gobject python3-gobject-Gdk python3-gobject-cairo typelib-1_0-Gtk-3_0
      typelib-1_0-Vte-2_91 python3-cryptography openssh-clients iproute2 adwaita-icon-theme gcc make pkg-config"
    FREERDP_SYSTEM="freerdp-devel"
    FREERDP_BUILD="cmake ninja libopenssl-3-devel zlib-devel krb5-devel libpulse-devel cups-devel
      pcsc-lite-devel fuse3-devel fuse3 curl"
    OPTIONAL="freerdp tigervnc python3-defusedxml python3-pyserial python3-pywinrm sshpass nmap tcpdump telnet
      wl-clipboard xclip papirus-icon-theme"
    SERVERS="openssh-server vsftpd tftp telnet-server nfs-kernel-server firewalld tigervnc-x11vnc"
    LAB="libvirt-daemon-qemu libvirt-client virt-install virt-viewer qemu-tools"
    ;;
  arch)
    REQUIRED="python python-gobject python-cairo gtk3 vte3 python-cryptography openssh iproute2
      adwaita-icon-theme base-devel pkgconf"
    FREERDP_SYSTEM="freerdp"
    FREERDP_BUILD="cmake ninja openssl zlib krb5 libpulse libcups pcsclite fuse3 curl"
    OPTIONAL="tigervnc python-defusedxml python-pyserial python-pywinrm sshpass nmap tcpdump inetutils
      wl-clipboard xclip papirus-icon-theme"
    SERVERS="openssh vsftpd tftp-hpa nfs-utils firewalld"
    LAB="libvirt virt-install virt-viewer qemu-img"
    ;;
  *)
    if [[ $NO_DEPS -eq 0 ]]; then
      echo "Gestore dei pacchetti non riconosciuto (dnf, apt, zypper, pacman)." >&2
      echo "Installa a mano Python 3.9+, PyGObject, GTK 3, VTE 2.91, python-cryptography e i file di" >&2
      echo "sviluppo di FreeRDP 3, poi rilancia con: ./install.sh --no-deps" >&2
      exit 1
    fi
    ;;
esac

pkg_install() {  # pkg_install <pacchetti...>: si ferma se uno manca
  case "$FAMILY" in
    fedora) sudo dnf install -y "$@" ;;
    debian) sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
    suse) sudo zypper --non-interactive install --no-recommends "$@" ;;
    arch) sudo pacman -S --needed --noconfirm "$@" ;;
  esac
}
pkg_install_optional() {  # uno alla volta: un pacchetto che qui non esiste non blocca gli altri
  local name
  for name in "$@"; do
    pkg_install "$name" > /dev/null 2>&1 || echo "  (facoltativo non disponibile: $name)"
  done
}
pkg_available() {  # pkg_available <pacchetto>: esiste nei repository della distribuzione?
  case "$FAMILY" in
    fedora) dnf -q list --available "$1" > /dev/null 2>&1 || rpm -q "$1" > /dev/null 2>&1 ;;
    debian) apt-cache show "$1" > /dev/null 2>&1 ;;
    suse) zypper --non-interactive -q info "$1" 2> /dev/null | grep -q "^Name" ;;
    arch) pacman -Si "$1" > /dev/null 2>&1 ;;
  esac
}

freerdp3_system() { pkg-config --exists freerdp3 freerdp-client3 winpr3 2> /dev/null; }

if [[ $NO_DEPS -eq 0 ]]; then
  echo "[1/6] Installo le dipendenze..."
  if [[ "$FAMILY" == debian ]]; then sudo apt-get update -q; fi
  # shellcheck disable=SC2086
  pkg_install $REQUIRED
  # FreeRDP 3 del sistema se c'è (Fedora, Debian 13, Ubuntu 24.04, Tumbleweed, Arch); altrimenti gli
  # strumenti per compilarne una copia.
  if [[ $BUNDLE_FREERDP -eq 0 ]] && pkg_available "$FREERDP_SYSTEM"; then
    pkg_install "$FREERDP_SYSTEM" || true
  fi
  if [[ $BUNDLE_FREERDP -eq 1 ]] || ! freerdp3_system; then
    # shellcheck disable=SC2086
    pkg_install $FREERDP_BUILD
  fi
  # shellcheck disable=SC2086
  pkg_install_optional $OPTIONAL
  if [[ $WITH_SERVERS -eq 1 ]]; then
    echo "Moduli server facoltativi..."
    # shellcheck disable=SC2086
    pkg_install_optional $SERVERS
  fi
  if [[ $WITH_LAB -eq 1 ]]; then
    echo "Laboratorio Linux usa e getta..."
    # shellcheck disable=SC2086
    pkg_install_optional $LAB
  fi
else
  echo "[1/6] Salto l'installazione delle dipendenze (--no-deps)."
fi

echo "[2/6] Compilo il motore RDP nativo..."
rm -rf "${LIBEXECDIR}/lib"
mkdir -p "${LIBEXECDIR}"
if [[ $BUNDLE_FREERDP -eq 0 ]] && freerdp3_system; then
  echo "  con la FreeRDP $(pkg-config --modversion freerdp3) del sistema"
  make -s -C native clean all
else
  echo "  FreeRDP 3 non c'è nel sistema: ne compilo una copia (qualche minuto)"
  BUILD_PREFIX="$(mktemp -d)"
  trap 'rm -rf "$BUILD_PREFIX"' EXIT
  packaging/build-freerdp.sh "$BUILD_PREFIX"
  mkdir -p "${LIBEXECDIR}/lib"
  cp -a "$BUILD_PREFIX"/lib/lib*.so.3* "${LIBEXECDIR}/lib/"
  PKG_CONFIG_PATH="$BUILD_PREFIX/lib/pkgconfig" make -s -C native clean all RPATH="${LIBEXECDIR}/lib"
fi
if [[ ! -x native/fedtermrdp-native ]]; then
  echo "Compilazione del motore RDP fallita: native/fedtermrdp-native non esiste." >&2
  exit 4
fi

echo "[3/6] Copio FedTermHub ${APP_VERSION}..."
mkdir -p "${APPDIR}" "${BINDIR}" "${DESKTOPDIR}" "${ICONROOT}" "${PIXMAPDIR}"
rm -rf "${APPDIR}/fedtermhub" "${APPDIR}/native"
cp -R fedtermhub "${APPDIR}/"
install -m 0755 native/fedtermrdp-native "${LIBEXECDIR}/fedtermrdp-native"
rm -f "${DESKTOPDIR}/fedtermhub.desktop" "${DESKTOPDIR}/FedTermHub.desktop" "${DESKTOPDIR}/io.github.crash_fd01.fedtermhub.desktop"
sed "s|^Exec=.*|Exec=${BINDIR}/fedtermhub|" fedtermhub.desktop > "${DESKTOPDIR}/${DESKTOP_ID}.desktop"
# Rimuove la vecchia icona vettoriale, così non prevale sul nuovo set PNG.
rm -f "${ICONROOT}/scalable/apps/fedtermhub.svg"
while IFS= read -r icon; do
  rel="${icon#share/icons/hicolor/}"
  install -D -m 0644 "${icon}" "${ICONROOT}/${rel}"
done < <(find share/icons/hicolor -type f -name 'io.github.crash_fd01.fedtermhub.png' | sort)
install -m 0644 share/pixmaps/io.github.crash_fd01.fedtermhub.png "${PIXMAPDIR}/io.github.crash_fd01.fedtermhub.png"

echo "[4/6] Creo il launcher..."
cat > "${BINDIR}/fedtermhub" <<EOF
#!/usr/bin/env bash
export FEDTERMHUB_RDP_NATIVE_HELPER="${LIBEXECDIR}/fedtermrdp-native"
exec python3 "${APPDIR}/fedtermhub/fedtermhub.py" "\$@"
EOF
chmod 0755 "${BINDIR}/fedtermhub"
chmod 0755 "${APPDIR}/fedtermhub/fedtermhub.py" "${APPDIR}/fedtermhub/serial_bridge.py" || true

echo "[5/6] Aggiorno cache desktop e icone..."
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${DESKTOPDIR}" || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "${HOME}/.local/share/icons/hicolor" || true
fi
if command -v kbuildsycoca6 >/dev/null 2>&1; then
  kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
elif command -v kbuildsycoca5 >/dev/null 2>&1; then
  kbuildsycoca5 --noincremental >/dev/null 2>&1 || true
fi

echo "[6/6] Verifica rapida..."
"${BINDIR}/fedtermhub" --doctor-strict

echo
echo "Installazione completata. Avvio: ${BINDIR}/fedtermhub"
case ":$PATH:" in
  *":${BINDIR}:"*) ;;
  *) echo "Nota: ${BINDIR} non è nel PATH; aggiungilo o avvia FedTermHub dal menu delle applicazioni." ;;
esac
echo "Le sessioni e il vault esistenti in ~/.config/fedtermhub restano invariati."
