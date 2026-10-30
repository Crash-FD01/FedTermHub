#!/usr/bin/env bash
set -euo pipefail

APP_VERSION="0.9.10"
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

for arg in "$@"; do
  case "$arg" in
    --no-deps) NO_DEPS=1 ;;
    --with-servers) WITH_SERVERS=1 ;;
    --with-lab) WITH_LAB=1 ;;
    -h|--help)
      echo "Uso: ./install.sh [--no-deps] [--with-servers] [--with-lab]"
      exit 0
      ;;
    *) echo "Argomento sconosciuto: $arg" >&2; exit 2 ;;
  esac
done

if ! command -v dnf >/dev/null 2>&1; then
  echo "DNF non trovato. Questo installer è pensato per Fedora Workstation." >&2
  exit 1
fi

if [[ $NO_DEPS -eq 0 ]]; then
  echo "[1/6] Installo dipendenze Fedora e tool di compilazione..."
  sudo dnf install -y \
    python3-gobject python3-cairo gtk3 vte291 \
    openssh-clients freerdp freerdp-libs freerdp-devel libwinpr-devel \
    gcc make pkgconf-pkg-config \
    tigervnc python3-cryptography python3-defusedxml python3-pyserial python3-winrm \
    sshpass adwaita-icon-theme nmap tcpdump iproute \
    wl-clipboard xclip
  if [[ $WITH_SERVERS -eq 1 ]]; then
    echo "Installazione moduli server opzionali richiesta..."
    sudo dnf install -y \
      openssh-server vsftpd tftp-server telnet-server nfs-utils firewalld \
      tigervnc-server-common tigervnc-x11-server tigervnc-wayland-server
  fi
  if [[ $WITH_LAB -eq 1 ]]; then
    echo "Installazione supporto Lab temporaneo Linux richiesta..."
    sudo dnf install -y \
      libvirt-daemon-kvm libvirt-client virt-install virt-viewer qemu-img
  fi
  sudo dnf install -y papirus-icon-theme || \
    echo "Papirus opzionale non disponibile: verrà usato Adwaita/hicolor."
else
  echo "[1/6] Salto installazione dipendenze (--no-deps)."
fi

echo "[2/6] Compilo il backend RDP nativo libfreerdp3..."
if ! pkg-config --exists freerdp3 freerdp-client3 winpr3; then
  echo "Mancano i file di sviluppo FreeRDP." >&2
  echo "Installa: sudo dnf install freerdp-devel libwinpr-devel gcc make pkgconf-pkg-config" >&2
  exit 3
fi
make -C native clean all
if [[ ! -x native/fedtermrdp-native ]]; then
  echo "Compilazione backend RDP fallita: native/fedtermrdp-native non esiste." >&2
  exit 4
fi

echo "[3/6] Copio FedTermHub ${APP_VERSION}..."
mkdir -p "${APPDIR}" "${BINDIR}" "${LIBEXECDIR}" "${DESKTOPDIR}" "${ICONROOT}" "${PIXMAPDIR}"
rm -rf "${APPDIR}/fedtermhub" "${APPDIR}/native"
cp -R fedtermhub "${APPDIR}/"
mkdir -p "${APPDIR}/native"
install -m 0755 native/fedtermrdp-native "${APPDIR}/native/fedtermrdp-native"
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
chmod 0755 "${APPDIR}/fedtermhub/fedtermhub.py" \
  "${APPDIR}/fedtermhub/serial_bridge.py" \
  "${APPDIR}/fedtermhub/windows_sessions.py" \
  "${APPDIR}/fedtermhub/network_tools.py" \
  "${APPDIR}/fedtermhub/remote_files.py" \
  "${APPDIR}/fedtermhub/ephemeral_lab.py" || true

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
echo "Il backend RDP nativo non usa XEmbed e funziona sia su Wayland sia su X11."
echo "Le sessioni e il vault esistenti in ~/.config/fedtermhub restano invariati."
