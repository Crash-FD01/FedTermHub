#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Costruisce i pacchetti .deb e .rpm «universali» di FedTermHub in dist/packages/: uno stesso pacchetto per
# Debian, Ubuntu, Mint e derivate (.deb) e per RHEL/Alma/Rocky e openSUSE (.rpm). Fedora ha invece il suo
# RPM costruito da fedtermhub.spec, che usa la FreeRDP 3 del sistema.
#
# Due pacchetti:
#
#   fedtermhub       il programma (Python puro: «all» / «noarch»), che usa GTK 3, VTE e PyGObject del
#                    sistema. Le dipendenze sono scritte in modo che lo stesso pacchetto vada bene ovunque:
#                    per l'RPM con le dipendenze «rich» (gtk3 su RHEL, typelib-1_0-Gtk-3_0 su openSUSE),
#                    per il .deb con i nomi di Debian, che Ubuntu e Mint condividono.
#   fedtermhub-rdp   il motore RDP nativo (C) con la sua copia di FreeRDP 3 (packaging/build-freerdp.sh),
#                    per x86_64 e aarch64. Va costruito sulla distribuzione con la glibc più vecchia fra
#                    quelle supportate (AlmaLinux 9, glibc 2.34): così parte anche su tutte le altre.
#
# Uso: packaging/build-packages.sh [x86_64|aarch64]
# Variabili: SKIP_MAIN=1 (solo il motore RDP: il pacchetto principale è uguale per tutte le
#            architetture, basta costruirlo una volta), SKIP_RDP=1, SKIP_DEB=1, SKIP_RPM=1,
#            FREERDP_PREFIX (FreeRDP già compilata da build-freerdp.sh).
# Serve: dpkg-deb, rpmbuild, objdump e, per il motore RDP, gcc, make, cmake, ninja, pkg-config e le
#        librerie di sviluppo elencate in build-freerdp.sh.
set -euo pipefail

ARCH="${1:-$(uname -m)}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/dist/packages"
MAINTAINER="${MAINTAINER:-Crash <Crash-FD01@users.noreply.github.com>}"
HOMEPAGE="https://github.com/Crash-FD01/FedTermHub"
APP_ID="io.github.crash_fd01.fedtermhub"
# Epoch 1: le prime build private usavano la numerazione 3.x (vedi fedtermhub.spec). Senza, dnf vedrebbe
# 0.9.x come un downgrade rispetto ai pacchetti già installati.
EPOCH=1
RELEASE=1
VERSION="$(sed -n 's/^APP_VERSION = "\([^"]*\)"/\1/p' "$ROOT/fedtermhub/fedtermhub.py")"
[[ -n "$VERSION" ]] || { echo "APP_VERSION non trovata in fedtermhub/fedtermhub.py" >&2; exit 1; }
case "$ARCH" in
    x86_64) DEB_ARCH=amd64 ;;
    aarch64) DEB_ARCH=arm64 ;;
    *) echo "Architettura non supportata: $ARCH" >&2; exit 1 ;;
esac

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT"
SUMMARY="Session manager for terminals, SSH, RDP, VNC and SFTP"
DESC="FedTermHub brings local terminals, SSH sessions, RDP and VNC desktops, SFTP
 file transfer, serial consoles and SSH tunnels together in one GTK application,
 with sessions organized in folders and credentials kept in an encrypted vault."
RDP_SUMMARY="Native RDP engine for FedTermHub (FreeRDP 3)"
RDP_DESC="The native RDP backend of FedTermHub, built directly on FreeRDP 3: it renders
 remote desktops inside FedTermHub tabs and detached windows."

deb_copyright() {  # deb_copyright <pacchetto>
    cat <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: FedTermHub
Upstream-Contact: $MAINTAINER
Source: $HOMEPAGE

Files: *
Copyright: 2026 Crash
License: GPL-3.0-or-later
 On Debian systems, the complete text of the GNU General Public License version 3 can be found in
 /usr/share/common-licenses/GPL-3.
EOF
}

# ================================================================== fedtermhub (programma)
if [[ "${SKIP_MAIN:-0}" != 1 ]]; then
    echo "→ fedtermhub $VERSION"
    STAGE="$WORK/main"
    APPDIR="$STAGE/usr/share/fedtermhub"
    mkdir -p "$APPDIR"
    # solo i file del programma: niente cache di Python né file dei test
    tar -C "$ROOT" --exclude='__pycache__' --exclude='*.pyc' -cf - fedtermhub | tar -C "$APPDIR" -xf -
    find "$APPDIR" -type d -exec chmod 0755 {} +
    find "$APPDIR" -type f -exec chmod 0644 {} +
    chmod 0755 "$APPDIR/fedtermhub/fedtermhub.py"
    install -Dm755 /dev/stdin "$STAGE/usr/bin/fedtermhub" <<'EOF'
#!/bin/sh
# FedTermHub (pacchetto). Il motore RDP nativo, con la sua FreeRDP 3, è nel pacchetto fedtermhub-rdp.
FEDTERMHUB_RDP_NATIVE_HELPER="${FEDTERMHUB_RDP_NATIVE_HELPER:-/usr/libexec/fedtermhub/fedtermrdp-native}"
export FEDTERMHUB_RDP_NATIVE_HELPER
exec /usr/bin/python3 /usr/share/fedtermhub/fedtermhub/fedtermhub.py "$@"
EOF
    install -Dm644 "$ROOT/fedtermhub.desktop" "$STAGE/usr/share/applications/$APP_ID.desktop"
    install -Dm644 "$ROOT/packaging/$APP_ID.metainfo.xml" "$STAGE/usr/share/metainfo/$APP_ID.metainfo.xml"
    (cd "$ROOT/share/icons/hicolor" && find . -name "$APP_ID.png" -print0) | while IFS= read -r -d '' icon; do
        install -Dm644 "$ROOT/share/icons/hicolor/$icon" "$STAGE/usr/share/icons/hicolor/$icon"
    done
    install -Dm644 "$ROOT/README.md" "$STAGE/usr/share/doc/fedtermhub/README.md"
    install -Dm644 "$ROOT/docs/GUIDA.md" "$STAGE/usr/share/doc/fedtermhub/GUIDA.md"

    # .pyc compilati all'installazione per il Python della macchina (che cambia da una distribuzione
    # all'altra), tolti alla disinstallazione: /usr/share non è scrivibile dall'utente e senza Python
    # ricompilerebbe tutto a ogni avvio.
    POST_COMPILE='/usr/bin/python3 -m compileall -q -f /usr/share/fedtermhub/fedtermhub >/dev/null 2>&1 || :'
    PRE_CLEAN='find /usr/share/fedtermhub -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || :'

    if [[ "${SKIP_DEB:-0}" != 1 ]]; then
        DEBROOT="$WORK/deb-main"
        cp -a "$STAGE" "$DEBROOT"
        mkdir -p "$DEBROOT/DEBIAN"
        deb_copyright fedtermhub | install -Dm644 /dev/stdin "$DEBROOT/usr/share/doc/fedtermhub/copyright"
        cat > "$DEBROOT/DEBIAN/control" <<EOF
Package: fedtermhub
Version: $VERSION-$RELEASE
Architecture: all
Maintainer: $MAINTAINER
Installed-Size: $(du -sk "$DEBROOT" --exclude=DEBIAN | cut -f1)
Depends: python3 (>= 3.9), python3-gi, python3-gi-cairo, gir1.2-gtk-3.0, gir1.2-vte-2.91,
 python3-cryptography, openssh-client, iproute2, adwaita-icon-theme, fedtermhub-rdp (= $VERSION-$RELEASE)
Recommends: python3-defusedxml, sshpass, nmap, tcpdump, tigervnc-viewer, telnet, python3-serial,
 python3-winrm, wl-clipboard, xclip, pkexec | policykit-1, papirus-icon-theme, freerdp3-x11
Suggests: openssh-server, vsftpd, tftpd-hpa, nfs-kernel-server, ufw | firewalld,
 tigervnc-scraping-server, tigervnc-tools, libvirt-daemon-system, libvirt-clients, virtinst,
 virt-viewer, qemu-utils
Section: net
Priority: optional
Homepage: $HOMEPAGE
Description: $SUMMARY
 $DESC
EOF
        printf '#!/bin/sh\nset -e\n%s\n' "$POST_COMPILE" > "$DEBROOT/DEBIAN/postinst"
        printf '#!/bin/sh\nset -e\n%s\n' "$PRE_CLEAN" > "$DEBROOT/DEBIAN/prerm"
        chmod 0755 "$DEBROOT/DEBIAN/postinst" "$DEBROOT/DEBIAN/prerm"
        (cd "$DEBROOT" && find . -path ./DEBIAN -prune -o -type f -printf '%P\0' | sort -z | xargs -0 md5sum > DEBIAN/md5sums)
        dpkg-deb --root-owner-group -Zxz --build "$DEBROOT" "$OUT/fedtermhub_${VERSION}-${RELEASE}_all.deb" > /dev/null
        echo "  dist/packages/fedtermhub_${VERSION}-${RELEASE}_all.deb"
    fi

    if [[ "${SKIP_RPM:-0}" != 1 ]]; then
        TOP="$WORK/rpm-main"
        mkdir -p "$TOP"/{SPECS,RPMS,BUILD,BUILDROOT}
        # Dipendenze «rich»: la prima alternativa è il nome di RHEL, la seconda quello di openSUSE. Le
        # condizioni «if» valgono solo dove c'è il pacchetto citato (per esempio i binding Gdk e cairo di
        # PyGObject, che openSUSE mette in pacchetti separati).
        cat > "$TOP/SPECS/fedtermhub.spec" <<EOF
Name:           fedtermhub
Epoch:          $EPOCH
Version:        $VERSION
Release:        $RELEASE
Summary:        $SUMMARY
License:        GPL-3.0-or-later
URL:            $HOMEPAGE
BuildArch:      noarch
AutoReqProv:    no
Requires:       python3 >= 3.9
Requires:       python3-gobject
Requires:       python3-cairo
Requires:       python3-cryptography
Requires:       (gtk3 or typelib-1_0-Gtk-3_0)
Requires:       (vte291 or typelib-1_0-Vte-2_91)
Requires:       (python3-gobject-Gdk if typelib-1_0-Gtk-3_0)
Requires:       (python3-gobject-cairo if typelib-1_0-Gtk-3_0)
Requires:       openssh-clients
Requires:       (iproute or iproute2)
Requires:       adwaita-icon-theme
Requires:       fedtermhub-rdp = $EPOCH:$VERSION-$RELEASE
Recommends:     polkit
Recommends:     freerdp
Recommends:     python3-defusedxml
Recommends:     sshpass
Recommends:     nmap
Recommends:     tcpdump
Recommends:     tigervnc
Recommends:     telnet
Recommends:     python3-pyserial
Recommends:     (python3-winrm or python3-pywinrm)
Recommends:     wl-clipboard
Recommends:     xclip
Recommends:     papirus-icon-theme
Suggests:       openssh-server
Suggests:       vsftpd
Suggests:       (tftp-server or tftp)
Suggests:       (nfs-utils or nfs-kernel-server)
Suggests:       firewalld
Suggests:       (tigervnc-x11-server or tigervnc-x11vnc)
Suggests:       (libvirt-daemon-kvm or libvirt-daemon-qemu)
Suggests:       virt-install
Suggests:       virt-viewer
Suggests:       (qemu-img or qemu-tools)

%global __os_install_post %{nil}
%global debug_package %{nil}

%description
$DESC

%install
cp -a "$STAGE"/. %{buildroot}/

%post
$POST_COMPILE

%preun
if [ "\$1" = 0 ]; then $PRE_CLEAN; fi

%files
%license LICENSE
/usr/bin/fedtermhub
/usr/share/fedtermhub
/usr/share/applications/$APP_ID.desktop
/usr/share/metainfo/$APP_ID.metainfo.xml
/usr/share/icons/hicolor/*/apps/$APP_ID.png
%doc /usr/share/doc/fedtermhub
EOF
        cp "$ROOT/LICENSE" "$TOP/BUILD/LICENSE"
        rpmbuild --quiet -bb --define "_topdir $TOP" --define "_builddir $TOP/BUILD" \
            --define "_binary_payload w19.zstdio" "$TOP/SPECS/fedtermhub.spec"
        cp "$TOP"/RPMS/noarch/fedtermhub-*.rpm "$OUT/"
        echo "  dist/packages/fedtermhub-$VERSION-$RELEASE.noarch.rpm"
    fi
fi

# ================================================================== fedtermhub-rdp (motore RDP nativo)
if [[ "${SKIP_RDP:-0}" != 1 ]]; then
    echo "→ fedtermhub-rdp $VERSION ($ARCH)"
    # FreeRDP 3 compilata qui e portata nel pacchetto (packaging/build-freerdp.sh): molte distribuzioni
    # supportate non l'hanno, e le altre ne hanno versioni diverse. FREERDP_PREFIX permette di riusare una
    # compilazione già fatta (la CI la tiene in cache).
    FRDP="${FREERDP_PREFIX:-$WORK/freerdp}"
    [[ -f "$FRDP/lib/pkgconfig/freerdp3.pc" ]] || "$ROOT/packaging/build-freerdp.sh" "$FRDP"
    FREERDP_VERSION="$(cat "$FRDP/VERSION.FreeRDP")"

    BUILD="$WORK/native"
    cp -a "$ROOT/native" "$BUILD"
    # Flag di hardening della distribuzione su cui si compila (Debian/Ubuntu: dpkg-buildflags; RHEL:
    # %{build_cflags}), più -z relro -z now dal Makefile: l'helper maneggia credenziali.
    if command -v dpkg-buildflags > /dev/null; then
        export DEB_BUILD_MAINT_OPTIONS="hardening=+all"
        CFLAGS="$(dpkg-buildflags --get CFLAGS)"
        CPPFLAGS="$(dpkg-buildflags --get CPPFLAGS)"
        LDFLAGS="$(dpkg-buildflags --get LDFLAGS)"
        export CFLAGS CPPFLAGS LDFLAGS
    elif [[ -n "$(rpm --eval '%{?build_cflags}' 2> /dev/null)" ]]; then
        CFLAGS="$(rpm --eval '%{build_cflags}')"
        LDFLAGS="$(rpm --eval '%{build_ldflags}')"
        export CFLAGS LDFLAGS
    fi
    PKG_CONFIG_PATH="$FRDP/lib/pkgconfig" make -s -C "$BUILD" clean all RPATH='$$ORIGIN/lib'

    RSTAGE="$WORK/rdp"
    LIBEXEC="$RSTAGE/usr/libexec/fedtermhub"
    install -Dm755 "$BUILD/fedtermrdp-native" "$LIBEXEC/fedtermrdp-native"
    mkdir -p "$LIBEXEC/lib"
    # Solo le librerie di esecuzione (nome «soname» e file reale), non i collegamenti per lo sviluppo.
    for lib in libfreerdp3 libfreerdp-client3 libwinpr3; do
        cp -a "$FRDP"/lib/"$lib".so.3* "$LIBEXEC/lib/"
    done
    # Anche libfuse3: FreeRDP la usa per i file copiati dalla sessione remota, e le distribuzioni recenti
    # (FUSE 3.17: Debian 13, Fedora 42+, Tumbleweed) l'hanno con un altro soname (libfuse3.so.4) rispetto
    # alle meno recenti (libfuse3.so.3). Il montaggio passa comunque da fusermount3 del sistema, che parla
    # lo stesso protocollo in tutte le versioni.
    case "$ARCH" in x86_64) ldarch="x86-64" ;; aarch64) ldarch="AArch64" ;; esac
    fuse_lib="$(ldconfig -p | awk -v a="$ldarch" '$1 == "libfuse3.so.3" && index($0, a) {print $NF; exit}')"
    if [[ -z "$fuse_lib" ]]; then
        echo "libfuse3.so.3 ($ARCH) non trovata sul sistema di build" >&2
        exit 1
    fi
    install -m755 "$(readlink -f "$fuse_lib")" "$LIBEXEC/lib/libfuse3.so.3"
    find "$LIBEXEC" -type f -name '*.so*' -exec strip --strip-unneeded {} +
    strip --strip-unneeded "$LIBEXEC/fedtermrdp-native"

    # Parte e trova le sue librerie (con -z now un simbolo mancante lo fermerebbe subito); senza
    # argomenti stampa l'uso ed esce con errore, ed è quello che ci si aspetta.
    usage="$("$LIBEXEC/fedtermrdp-native" --help 2>&1 || true)"
    if ! grep -q -- --config-fd <<< "$usage"; then
        echo "Il motore RDP compilato non parte: $usage" >&2
        exit 1
    fi
    if ldd "$LIBEXEC/fedtermrdp-native" | grep -E 'lib(freerdp|winpr|fuse3)' | grep -v "$LIBEXEC/lib/"; then
        echo "Il motore RDP non usa le librerie incluse nel pacchetto" >&2
        exit 1
    fi

    # Licenze dei componenti inclusi: FreeRDP (Apache-2.0) e libfuse (LGPL-2.1).
    DOCDIR="$RSTAGE/usr/share/doc/fedtermhub-rdp"
    install -Dm644 "$FRDP/LICENSE.FreeRDP" "$DOCDIR/LICENSE.FreeRDP"
    for candidate in /usr/share/licenses/fuse3-libs/LGPL2.txt /usr/share/licenses/fuse3/LGPL2.txt \
                     /usr/share/licenses/fuse3-libs/COPYING.LIB /usr/share/licenses/fuse3/COPYING.LIB \
                     /usr/share/doc/libfuse3-3/copyright; do
        if [[ -f "$candidate" ]]; then
            install -Dm644 "$candidate" "$DOCDIR/LICENSE.libfuse3"
            break
        fi
    done
    if [[ ! -f "$DOCDIR/LICENSE.libfuse3" ]]; then
        curl -fsSL https://raw.githubusercontent.com/libfuse/libfuse/master/LGPL2.txt \
            | install -Dm644 /dev/stdin "$DOCDIR/LICENSE.libfuse3"
    fi
    cat > "$DOCDIR/BUNDLED-COMPONENTS.txt" <<EOF
Componenti inclusi in /usr/libexec/fedtermhub/lib
=================================================

FreeRDP $FREERDP_VERSION (libfreerdp3, libfreerdp-client3, libwinpr3): licenza Apache-2.0, LICENSE.FreeRDP.
  Sorgenti: https://github.com/FreeRDP/FreeRDP/releases/tag/$FREERDP_VERSION
  Compilata senza modifiche con packaging/build-freerdp.sh di FedTermHub.

libfuse3 ($(basename "$(readlink -f "$fuse_lib")")): licenza LGPL-2.1, LICENSE.libfuse3.
  Sorgenti: https://github.com/libfuse/libfuse
  Libreria condivisa usata senza modifiche: si può sostituire con un'altra versione compatibile
  (libfuse3.so.3) mettendola al suo posto.
EOF

    # Librerie del sistema di cui hanno bisogno l'helper e le librerie incluse (escluse quelle incluse e
    # quelle di glibc): diventano le dipendenze dei pacchetti.
    ELFS=("$LIBEXEC/fedtermrdp-native" "$LIBEXEC"/lib/*.so.*)
    mapfile -t SYSLIBS < <(for f in "${ELFS[@]}"; do [[ -L "$f" ]] || objdump -p "$f"; done \
        | awk '/NEEDED/{print $2}' | sort -u \
        | grep -vxF -f <(cd "$LIBEXEC/lib" && ls) \
        | grep -Ev '^(libc|libm|libdl|libpthread|librt|ld-linux[^ ]*)\.so')
    GLIBC_MIN="$(for f in "${ELFS[@]}"; do [[ -L "$f" ]] || objdump -T "$f"; done \
        | grep -o 'GLIBC_[0-9.]*' | sed 's/GLIBC_//' | sort -Vu | tail -n1)"
    echo "  FreeRDP $FREERDP_VERSION inclusa; glibc >= $GLIBC_MIN; librerie di sistema: ${SYSLIBS[*]}"

    RDP_DESC_FULL="$RDP_DESC
 .
 It carries its own copy of FreeRDP $FREERDP_VERSION (and libfuse3), so the same
 package works on every supported distribution."

    if [[ "${SKIP_DEB:-0}" != 1 ]]; then
        DEBROOT="$WORK/deb-rdp"
        cp -a "$RSTAGE" "$DEBROOT"
        mkdir -p "$DEBROOT/DEBIAN"
        {
            deb_copyright fedtermhub-rdp
            cat <<EOF

Files: usr/libexec/fedtermhub/lib/libfreerdp* usr/libexec/fedtermhub/lib/libwinpr*
Copyright: FreeRDP contributors
License: Apache-2.0
 See /usr/share/doc/fedtermhub-rdp/LICENSE.FreeRDP.

Files: usr/libexec/fedtermhub/lib/libfuse3*
Copyright: libfuse contributors
License: LGPL-2.1
 See /usr/share/doc/fedtermhub-rdp/LICENSE.libfuse3.
EOF
        } > "$DEBROOT/usr/share/doc/fedtermhub-rdp/copyright"
        # Nomi Debian delle librerie (quelli di Debian 12 e Ubuntu 22.04). Le distribuzioni passate alla
        # transizione «t64» (Debian 13, Ubuntu 24.04) hanno libssl3t64, libcups2t64…: vanno bene entrambi.
        deps=("fedtermhub (= $VERSION-$RELEASE)" "libc6 (>= $GLIBC_MIN)")
        for soname in "${SYSLIBS[@]}"; do
            case "$soname" in
                libz.so.1) deps+=("zlib1g") ;;
                libssl.so.3 | libcrypto.so.3) deps+=("libssl3 | libssl3t64") ;;
                libkrb5.so.3) deps+=("libkrb5-3") ;;
                libk5crypto.so.3) deps+=("libk5crypto3") ;;
                libgssapi_krb5.so.2) deps+=("libgssapi-krb5-2") ;;
                libcom_err.so.2) deps+=("libcom-err2") ;;
                libgcc_s.so.1) deps+=("libgcc-s1") ;;
                libcups.so.2) deps+=("libcups2 | libcups2t64") ;;
                libpulse.so.0) deps+=("libpulse0") ;;
                *)
                    echo "Nessun pacchetto Debian noto per $soname: aggiungilo in build-packages.sh" >&2
                    exit 1
                    ;;
            esac
        done
        DEPENDS="$(printf '%s\n' "${deps[@]}" | awk '!seen[$0]++' | paste -sd ',' - | sed 's/,/, /g')"
        cat > "$DEBROOT/DEBIAN/control" <<EOF
Package: fedtermhub-rdp
Version: $VERSION-$RELEASE
Architecture: $DEB_ARCH
Maintainer: $MAINTAINER
Installed-Size: $(du -sk "$DEBROOT" --exclude=DEBIAN | cut -f1)
Depends: $DEPENDS
Recommends: fuse3, pcscd
Section: net
Priority: optional
Homepage: $HOMEPAGE
Description: $RDP_SUMMARY
 $RDP_DESC_FULL
EOF
        (cd "$DEBROOT" && find . -path ./DEBIAN -prune -o -type f -printf '%P\0' | sort -z | xargs -0 md5sum > DEBIAN/md5sums)
        dpkg-deb --root-owner-group -Zxz --build "$DEBROOT" "$OUT/fedtermhub-rdp_${VERSION}-${RELEASE}_${DEB_ARCH}.deb" > /dev/null
        echo "  dist/packages/fedtermhub-rdp_${VERSION}-${RELEASE}_${DEB_ARCH}.deb  (Depends: $DEPENDS)"
    fi

    if [[ "${SKIP_RPM:-0}" != 1 ]]; then
        TOP="$WORK/rpm-rdp"
        mkdir -p "$TOP"/{SPECS,RPMS,BUILD,BUILDROOT}
        # Dipendenze per nome di libreria, uguali su Fedora, RHEL e openSUSE. Le librerie incluse non
        # vanno né richieste né offerte al resto del sistema: AutoReqProv spento e righe scritte qui.
        suffix=""
        if [[ "$ARCH" == x86_64 || "$ARCH" == aarch64 ]]; then suffix="()(64bit)"; fi
        RPM_REQUIRES="$(printf 'Requires:       %s\n' "glibc >= $GLIBC_MIN" "${SYSLIBS[@]/%/$suffix}")"
        RPM_DESC="$(sed -e 's/^ //' -e 's/^\.$//' <<< "$RDP_DESC_FULL")"
        cat > "$TOP/SPECS/fedtermhub-rdp.spec" <<EOF
Name:           fedtermhub-rdp
Epoch:          $EPOCH
Version:        $VERSION
Release:        $RELEASE
Summary:        $RDP_SUMMARY
License:        GPL-3.0-or-later AND Apache-2.0 AND LGPL-2.1-only
URL:            $HOMEPAGE
BuildArch:      $ARCH
AutoReqProv:    no
Requires:       fedtermhub = $EPOCH:$VERSION-$RELEASE
$RPM_REQUIRES
Recommends:     fuse3
Recommends:     pcsc-lite

%global __os_install_post %{nil}
%global debug_package %{nil}
%global _build_id_links none

%description
$RPM_DESC

%install
cp -a "$RSTAGE"/. %{buildroot}/

%files
%license LICENSE
/usr/libexec/fedtermhub
%doc /usr/share/doc/fedtermhub-rdp
EOF
        cp "$ROOT/LICENSE" "$TOP/BUILD/LICENSE"
        rpmbuild --quiet -bb --target "$ARCH" --define "_topdir $TOP" --define "_builddir $TOP/BUILD" \
            --define "_binary_payload w19.zstdio" "$TOP/SPECS/fedtermhub-rdp.spec"
        cp "$TOP"/RPMS/"$ARCH"/fedtermhub-rdp-*.rpm "$OUT/"
        echo "  dist/packages/fedtermhub-rdp-$VERSION-$RELEASE.$ARCH.rpm"
        rpm -qp --requires "$OUT/fedtermhub-rdp-$VERSION-$RELEASE.$ARCH.rpm" | sed 's/^/    requires: /'
    fi
fi
echo "✓ Pacchetti in $OUT"
