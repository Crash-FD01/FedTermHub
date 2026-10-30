#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Compila la FreeRDP 3 che il pacchetto fedtermhub-rdp porta con sé.
#
# Molte distribuzioni ancora supportate non hanno FreeRDP 3 (Ubuntu 22.04, Mint 21, RHEL/Alma/Rocky 9) o
# ne hanno versioni diverse fra loro. Il motore RDP nativo usa quindi sempre questa copia, installata in
# /usr/libexec/fedtermhub/lib e trovata tramite RPATH: stessa versione e stesse funzioni ovunque, senza
# toccare la FreeRDP della distribuzione (che resta per chi usa xfreerdp).
#
# Si compila solo ciò che serve al client: niente server, proxy, client X11/SDL, FFmpeg. Restano audio
# (PulseAudio, che anche PipeWire offre), stampanti (CUPS), smart card (PC/SC), Kerberos per NLA e FUSE
# per copiare file dalla sessione remota. Sono librerie presenti con lo stesso nome (soname) su tutte le
# distribuzioni supportate: diventano dipendenze del pacchetto.
#
# Uso: packaging/build-freerdp.sh <cartella di installazione>
#      Al termine: <cartella>/lib (librerie), <cartella>/include, <cartella>/lib/pkgconfig.
# Variabili: FREERDP_VERSION, FREERDP_SHA256 (per provare un'altra versione), FREERDP_TARBALL (archivio
#            già scaricato), JOBS.
set -euo pipefail

# Versione inclusa nei pacchetti. Quando FreeRDP pubblica correzioni di sicurezza si aggiorna qui, con il
# suo SHA-256 (lo stesso del file .sha256 pubblicato accanto all'archivio sulla pagina della release).
FREERDP_VERSION="${FREERDP_VERSION:-3.32.2}"
FREERDP_SHA256="${FREERDP_SHA256:-e95a505c0057e4d06a3b120477c421028769deba4db9c639ef3465d050f0955c}"
URL="https://github.com/FreeRDP/FreeRDP/releases/download/$FREERDP_VERSION/freerdp-$FREERDP_VERSION.tar.gz"

PREFIX="$(mkdir -p "$1" && cd "$1" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
JOBS="${JOBS:-$(nproc)}"

tarball="${FREERDP_TARBALL:-$WORK/freerdp.tar.gz}"
if [[ ! -f "$tarball" ]]; then
    echo "→ Scarico FreeRDP $FREERDP_VERSION"
    curl -fsSL --retry 3 -o "$tarball" "$URL"
fi
echo "$FREERDP_SHA256  $tarball" | sha256sum -c --quiet - || {
    echo "SHA-256 dell'archivio di FreeRDP $FREERDP_VERSION diverso da quello atteso: fermo la build" >&2
    exit 1
}
tar -xzf "$tarball" -C "$WORK"
SRC="$WORK/freerdp-$FREERDP_VERSION"

echo "→ Compilo FreeRDP $FREERDP_VERSION (solo client)"
# CMAKE_INSTALL_RPATH='$ORIGIN': le librerie si trovano fra loro nella cartella in cui sono installate.
cmake -S "$SRC" -B "$WORK/build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_INSTALL_LIBDIR=lib \
    -DCMAKE_INSTALL_RPATH='$ORIGIN' \
    -DCMAKE_SKIP_RPATH=OFF \
    -DBUILD_SHARED_LIBS=ON \
    -DBUILD_TESTING=OFF \
    -DWITH_CLIENT=ON \
    -DWITH_CLIENT_COMMON=ON \
    -DWITH_CLIENT_CHANNELS=ON \
    -DWITH_CHANNELS=ON \
    -DCHANNEL_URBDRC=OFF \
    -DCHANNEL_TSMF=OFF \
    -DCHANNEL_RDPECAM=OFF \
    -DWITH_CLIENT_SDL=OFF \
    -DWITH_CLIENT_SDL2=OFF \
    -DWITH_CLIENT_SDL3=OFF \
    -DWITH_X11=OFF \
    -DWITH_WAYLAND=OFF \
    -DWITH_SERVER=OFF \
    -DWITH_SERVER_CHANNELS=OFF \
    -DWITH_SHADOW=OFF \
    -DWITH_PROXY=OFF \
    -DWITH_PLATFORM_SERVER=OFF \
    -DWITH_SAMPLE=OFF \
    -DWITH_RDTK=OFF \
    -DWITH_WINPR_TOOLS=OFF \
    -DWITH_MANPAGES=OFF \
    -DWITH_INSTALL_CLIENT_DESKTOP_FILES=OFF \
    -DWITH_FFMPEG=OFF \
    -DWITH_DSP_FFMPEG=OFF \
    -DWITH_VIDEO_FFMPEG=OFF \
    -DWITH_SWSCALE=OFF \
    -DWITH_OPENH264=OFF \
    -DWITH_CAIRO=OFF \
    -DWITH_JPEG=OFF \
    -DWITH_GFX_AV1=OFF \
    -DWITH_AAD=OFF \
    -DWITH_JSON_DISABLED=ON \
    -DWITH_WEBVIEW=OFF \
    -DWITH_PKCS11=OFF \
    -DWITH_OPENCL=OFF \
    -DWITH_UNICODE_BUILTIN=ON \
    -DWITH_PULSE=ON \
    -DWITH_ALSA=OFF \
    -DWITH_OSS=OFF \
    -DWITH_SNDIO=OFF \
    -DWITH_CUPS=ON \
    -DWITH_PCSC=ON \
    -DWITH_KRB5=ON \
    -DWITH_FUSE=ON \
    -DWITH_CCACHE=OFF \
    -DWITH_VERBOSE_WINPR_ASSERT=OFF \
    > "$WORK/cmake.log" 2>&1 || { tail -60 "$WORK/cmake.log" >&2; exit 1; }
grep -E "^-- (FreeRDP|Feature|Found|Could NOT)|WITH_(PULSE|CUPS|PCSC|KRB5|FUSE)" "$WORK/cmake.log" | tail -40 || true
cmake --build "$WORK/build" -j "$JOBS" > "$WORK/build.log" 2>&1 || { tail -60 "$WORK/build.log" >&2; exit 1; }
cmake --install "$WORK/build" > /dev/null

# Il pacchetto non porta file che non servono al motore RDP (programmi d'esempio, CMake config).
rm -rf "$PREFIX/bin" "$PREFIX/share" "$PREFIX/lib/cmake"
install -Dm644 "$SRC/LICENSE" "$PREFIX/LICENSE.FreeRDP"
echo "$FREERDP_VERSION" > "$PREFIX/VERSION.FreeRDP"
echo "✓ FreeRDP $FREERDP_VERSION in $PREFIX"
ls "$PREFIX/lib"
