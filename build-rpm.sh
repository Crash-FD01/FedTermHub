#!/usr/bin/env bash
#
# build-rpm.sh — Costruisce l'RPM di FedTermHub su Fedora.
#
# Automatizza l'intera procedura: prepara l'albero ~/rpmbuild, genera il
# tarball sorgente con la struttura che rpmbuild si aspetta, installa le
# dipendenze di build dichiarate nello spec e lancia rpmbuild.
#
# Uso:
#   ./build-rpm.sh              build completa (con la sezione %check / test UI)
#   ./build-rpm.sh --nocheck    salta i test UI (build più veloce, utile se un
#                               test UI fallisce per motivi d'ambiente)
#   ./build-rpm.sh --no-deps    non tentare di installare le dipendenze (dnf)
#   ./build-rpm.sh --install    a fine build, installa l'RPM prodotto con dnf
#
# Va eseguito dalla cartella che contiene questo script, fedtermhub.spec,
# la cartella fedtermhub/ e la cartella native/.
#
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

# ----------------------------------------------------------------- argomenti
RUN_CHECK=1
INSTALL_DEPS=1
DO_INSTALL=0
for arg in "$@"; do
    case "$arg" in
        --nocheck)  RUN_CHECK=0 ;;
        --no-deps)  INSTALL_DEPS=0 ;;
        --install)  DO_INSTALL=1 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//; 1d'
            exit 0
            ;;
        *)
            echo "Argomento non riconosciuto: $arg" >&2
            echo "Usa --help per l'elenco delle opzioni." >&2
            exit 2
            ;;
    esac
done

# ------------------------------------------------------------------- percorsi
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd "$SCRIPT_DIR"

SPEC="fedtermhub.spec"
if [[ ! -f "$SPEC" ]]; then
    echo "ERRORE: $SPEC non trovato. Lancia lo script dalla radice del pacchetto." >&2
    exit 1
fi
if [[ ! -d native || ! -d fedtermhub ]]; then
    echo "ERRORE: cartelle native/ o fedtermhub/ mancanti. Pacchetto incompleto?" >&2
    exit 1
fi

# Ricava Name e Version direttamente dallo spec, così restano sempre allineati.
NAME="$(awk '/^Name:/    {print $2; exit}' "$SPEC")"
VERSION="$(awk '/^Version:/ {print $2; exit}' "$SPEC")"
if [[ -z "$NAME" || -z "$VERSION" ]]; then
    echo "ERRORE: impossibile leggere Name/Version da $SPEC." >&2
    exit 1
fi
PKG="${NAME}-${VERSION}"
echo ">> Pacchetto: $PKG"

# ------------------------------------------------------------- prerequisiti
need_cmd() { command -v "$1" &>/dev/null; }

if (( INSTALL_DEPS )); then
    echo ">> [1/5] Installazione strumenti di build (richiede sudo)..."
    sudo dnf install -y rpm-build rpmdevtools gcc make pkgconf-pkg-config freerdp-devel

    echo ">> Installazione dipendenze dichiarate nello spec (builddep)..."
    # dnf builddep vive in dnf-plugins-core; installiamolo se manca.
    if ! sudo dnf builddep -y "$SPEC" 2>/dev/null; then
        echo "   builddep non disponibile: installo dnf-plugins-core e riprovo..."
        sudo dnf install -y dnf-plugins-core
        sudo dnf builddep -y "$SPEC"
    fi
else
    echo ">> [1/5] Salto installazione dipendenze (--no-deps)."
    for c in rpmbuild gcc make pkg-config; do
        if ! need_cmd "$c"; then
            echo "ERRORE: '$c' non trovato e --no-deps attivo. Installalo a mano." >&2
            exit 1
        fi
    done
fi

# ------------------------------------------------- verifica FreeRDP dev
echo ">> [2/5] Verifica librerie di sviluppo FreeRDP..."
if pkg-config --exists freerdp3 freerdp-client3 winpr3; then
    echo "   Trovato FreeRDP: $(pkg-config --modversion freerdp3 | head -1)"
else
    echo "ERRORE: pkg-config non trova freerdp3/freerdp-client3/winpr3." >&2
    echo "        Installa 'freerdp-devel' (sudo dnf install freerdp-devel)." >&2
    exit 1
fi

# ------------------------------------------------------- albero rpmbuild
echo ">> [3/5] Preparazione albero ~/rpmbuild..."
if need_cmd rpmdev-setuptree; then
    rpmdev-setuptree
else
    mkdir -p "$HOME"/rpmbuild/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
fi
TOPDIR="$(rpm --eval '%{_topdir}')"
: "${TOPDIR:=$HOME/rpmbuild}"

# --------------------------------------------------- tarball sorgente
echo ">> [4/5] Creazione tarball sorgente ${PKG}.tar.gz..."
# %autosetup pretende che il tar estragga in una dir chiamata Name-Version.
# Costruiamo il tar da una copia rinominata in modo che la root sia corretta,
# a prescindere da come si chiama la cartella corrente.
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
# Copiamo il contenuto del pacchetto (escludendo eventuali artefatti locali).
mkdir -p "$WORKDIR/$PKG"
tar --exclude='./.git' \
    --exclude='./native/fedtermrdp-native' \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    -cf - . | tar -xf - -C "$WORKDIR/$PKG"
tar -czf "$TOPDIR/SOURCES/${PKG}.tar.gz" -C "$WORKDIR" "$PKG"
cp -f "$SPEC" "$TOPDIR/SPECS/$SPEC"
echo "   Sorgente: $TOPDIR/SOURCES/${PKG}.tar.gz"

# ------------------------------------------------------------ rpmbuild
echo ">> [5/5] rpmbuild -bb${RUN_CHECK:+ }$( ((RUN_CHECK)) || echo '--nocheck' )..."
RPMBUILD_ARGS=(-bb)
(( RUN_CHECK )) || RPMBUILD_ARGS+=(--nocheck)

set +e
rpmbuild "${RPMBUILD_ARGS[@]}" "$TOPDIR/SPECS/$SPEC"
RC=$?
set -e

if (( RC != 0 )); then
    echo >&2
    echo "!! rpmbuild ha restituito codice $RC." >&2
    echo "!! Se l'errore è nella compilazione di native/fedtermrdp_native.c," >&2
    echo "!! copia l'output completo del compilatore per la diagnosi." >&2
    echo "!! Se è un test UI sotto Xvfb, riprova con:  ./build-rpm.sh --nocheck" >&2
    exit "$RC"
fi

# ------------------------------------------------------------ risultato
RPM_FILE="$(find "$TOPDIR/RPMS" -name "${PKG}-*.rpm" -newermt '-2 min' 2>/dev/null | head -1)"
[[ -z "$RPM_FILE" ]] && RPM_FILE="$(find "$TOPDIR/RPMS" -name "${PKG}-*.rpm" 2>/dev/null | head -1)"

echo
echo "============================================================"
echo " Build completata."
if [[ -n "$RPM_FILE" ]]; then
    echo " RPM: $RPM_FILE"
    echo
    echo " Installa con:"
    echo "   sudo dnf install \"$RPM_FILE\""
else
    echo " RPM prodotto in: $TOPDIR/RPMS/ (nome non rilevato automaticamente)"
fi
echo "============================================================"

if (( DO_INSTALL )) && [[ -n "$RPM_FILE" ]]; then
    echo ">> Installazione RPM (richiede sudo)..."
    sudo dnf install -y "$RPM_FILE"
fi
