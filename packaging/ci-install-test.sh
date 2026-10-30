#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Installa FedTermHub su una distribuzione pulita (container della CI) e controlla che funzioni davvero.
#
#   packaging/ci-install-test.sh --packages <cartella>   pacchetti appena costruiti (pull request)
#   packaging/ci-install-test.sh --site <URL>            come un utente: setup.sh dal sito pubblicato
#
# Opzioni: --version X (versione attesa), --source-tests (anche i test dei sorgenti con il Python e il
# GTK della distribuzione). Va eseguito come root dalla radice del repository.
#
# Controlli: installazione con il gestore della distribuzione (firme comprese, con --site), doctor,
# finestra principale che si apre sotto Xvfb senza eccezioni, motore RDP che si collega a un xrdp locale
# e riceve un frame, rimozione pulita (con --site).
set -uo pipefail

MODE="" SOURCE="" EXPECTED="" SOURCE_TESTS=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --packages) MODE=packages; SOURCE="$(cd "$2" && pwd)"; shift ;;  # apt vuole percorsi assoluti
        --site) MODE=site; SOURCE="${2%/}"; shift ;;
        --version) EXPECTED="$2"; shift ;;
        --source-tests) SOURCE_TESTS=1 ;;
        *) echo "argomento sconosciuto: $1" >&2; exit 2 ;;
    esac
    shift
done
[[ -n "$MODE" ]] || { echo "serve --packages o --site" >&2; exit 2; }

. /etc/os-release
NAME_SHORT="${PRETTY_NAME:-$ID}"
FAILED=0
annotate() {  # annotate error|notice|warning <titolo> <file o testo>
    local body="$3"
    [[ -f "$3" ]] && body="$(tail -30 "$3")"
    body="$(sed -e 's/%/%25/g' <<< "$body" | awk 'BEGIN{ORS="%0A"} {print}')"
    echo "::$1 title=$NAME_SHORT · $2::$body"
}
fail() { annotate error "$1" "${2:-}"; FAILED=1; }

family=""
for id in $ID ${ID_LIKE:-}; do
    case "$id" in
        fedora|rhel|centos|rocky|almalinux) family=rpm-dnf ;;
        debian|ubuntu|linuxmint) family=deb ;;
        opensuse*|suse) family=rpm-zypper ;;
    esac
    [[ -n "$family" ]] && break
done
ARCH="$(uname -m)"
echo "== $NAME_SHORT ($family, $ARCH)"

pkg() {  # installa pacchetti di servizio per i controlli
    case "$family" in
        deb) DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$@" ;;
        rpm-dnf) dnf -y -q install "$@" ;;
        rpm-zypper) zypper --non-interactive -q install --no-recommends "$@" ;;
    esac
}

# ------------------------------------------------------------------ strumenti per i controlli
# Uno alla volta: un nome che in questa distribuzione non esiste non deve bloccare gli altri.
tools() { local name; for name in "$@"; do pkg "$name" > /dev/null 2>&1 || echo "  (non disponibile: $name)"; done; }
case "$family" in
    deb)
        apt-get update -q > /dev/null
        tools curl ca-certificates xvfb xauth xdotool dbus dbus-x11 procps openssl python3
        ;;
    rpm-dnf)
        if [[ "$ID" != fedora ]]; then
            dnf -y -q install epel-release > /dev/null 2>&1 || true
            dnf config-manager --set-enabled crb > /dev/null 2>&1 || true
        fi
        tools curl xorg-x11-server-Xvfb xorg-x11-xauth xauth dbus-daemon dbus-tools procps-ng openssl python3 xdotool
        ;;
    rpm-zypper)
        tools curl xvfb-run xorg-x11-server xorg-x11-server-Xvfb xauth dbus-1 dbus-1-daemon procps openssl python3 xdotool
        ;;
    *) fail "Distribuzione" "famiglia non riconosciuta: ID=$ID ID_LIKE=${ID_LIKE:-}"; exit 1 ;;
esac

# xvfb-run dove c'è; altrimenti (RHEL 10 ha solo Xvfb, o niente) lo stesso a mano.
xvfb() {  # xvfb comando argomenti...
    if command -v xvfb-run > /dev/null; then
        xvfb-run -a -s "-screen 0 1400x900x24" "$@"
    elif command -v Xvfb > /dev/null; then
        local display=":$((90 + RANDOM % 9))" xpid status
        Xvfb "$display" -screen 0 1400x900x24 -nolisten tcp > /dev/null 2>&1 &
        xpid=$!
        sleep 1
        DISPLAY="$display" "$@"
        status=$?
        kill "$xpid" 2> /dev/null
        return $status
    elif command -v mutter > /dev/null; then
        # RHEL 10 non ha più Xorg né Xvfb: GTK gira su Wayland, in un mutter senza schermo.
        XDG_RUNTIME_DIR="$(mktemp -d)" dbus-run-session -- sh -c '
            mutter --headless --wayland --no-x11 --virtual-monitor 1400x900 > /tmp/mutter.log 2>&1 &
            for _ in $(seq 1 40); do [ -S "$XDG_RUNTIME_DIR/wayland-0" ] && break; sleep 0.25; done
            WAYLAND_DISPLAY=wayland-0 GDK_BACKEND=wayland "$@"; status=$?
            kill %1 2> /dev/null; exit $status' sh "$@"
    else
        echo "nessun display virtuale (Xvfb o mutter) in questa distribuzione"
        return 127
    fi
}
if ! command -v xvfb-run > /dev/null && ! command -v Xvfb > /dev/null && [[ "$family" == rpm-dnf ]]; then
    tools mutter
fi
HAVE_X=1
command -v xvfb-run > /dev/null || command -v Xvfb > /dev/null || command -v mutter > /dev/null || HAVE_X=0
[[ $HAVE_X -eq 1 ]] || annotate warning "Display" "nessun display virtuale installabile: provati solo doctor e motore RDP senza finestra"

# ------------------------------------------------------------------ installazione
if [[ "$MODE" == site ]]; then
    curl -fsSL "$SOURCE/setup.sh" -o /tmp/setup.sh
    if ! sh /tmp/setup.sh > /tmp/setup.log 2>&1; then
        fail "setup.sh" /tmp/setup.log
        exit 1
    fi
    tail -3 /tmp/setup.log
else
    case "$family" in
        deb)
            deb_arch="$(dpkg --print-architecture)"
            DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$SOURCE"/fedtermhub_*_all.deb \
                "$SOURCE"/fedtermhub-rdp_*_"$deb_arch".deb > /tmp/install.log 2>&1
            ;;
        rpm-dnf)
            dnf -y install "$SOURCE"/fedtermhub-[0-9]*.noarch.rpm "$SOURCE"/fedtermhub-rdp-[0-9]*."$ARCH".rpm > /tmp/install.log 2>&1
            ;;
        rpm-zypper)
            zypper --non-interactive --no-gpg-checks install --allow-unsigned-rpm \
                "$SOURCE"/fedtermhub-[0-9]*.noarch.rpm "$SOURCE"/fedtermhub-rdp-[0-9]*."$ARCH".rpm > /tmp/install.log 2>&1
            ;;
    esac
    if [[ $? -ne 0 ]]; then
        fail "Installazione" /tmp/install.log
        exit 1
    fi
fi

case "$family" in
    deb) installed="$(dpkg-query -W -f='${Version}' fedtermhub 2> /dev/null)"; installed="${installed%-*}" ;;
    *) installed="$(rpm -q --qf '%{VERSION}' fedtermhub 2> /dev/null)" ;;
esac
echo "installata: fedtermhub $installed"
if [[ -n "$EXPECTED" && "$installed" != "$EXPECTED" ]]; then
    fail "Versione" "installata $installed, attesa $EXPECTED: il repository non è aggiornato"
fi
if [[ "$MODE" == site && "$family" != deb ]]; then
    # dnf e zypper con gpgcheck rifiutano già un pacchetto non firmato: qui si mostra la firma.
    sig="$(rpm -qi fedtermhub | sed -n 's/^Signature *: *//p')"
    echo "firma: ${sig:-?}"
    case "$sig" in *"(none)"*) fail "Firma" "pacchetto installato senza firma" ;; esac
fi

# ------------------------------------------------------------------ il programma parte
if [[ $HAVE_X -eq 1 ]]; then doctor() { xvfb fedtermhub --doctor-strict; }; else doctor() { fedtermhub --doctor-strict; }; fi
if ! doctor > /tmp/doctor.txt 2>&1; then
    fail "Doctor" /tmp/doctor.txt
fi
grep -E "Distribuzione|GTK/VTE|helper libfreerdp|Python:" /tmp/doctor.txt || true

cat > /tmp/avvio.sh <<'EOF'
fedtermhub > /tmp/app.log 2>&1 &
pid=$!
# Con X11 (Xvfb) si cerca la finestra con xdotool; su Wayland (mutter senza schermo) xdotool non
# vede le finestre: basta che il programma resti in vita 12 secondi senza eccezioni.
use_xdotool=no
if command -v xdotool > /dev/null && [ -z "${WAYLAND_DISPLAY:-}" ]; then use_xdotool=yes; fi
for i in $(seq 1 60); do
    if [ "$use_xdotool" = yes ]; then
        if xdotool search --name "^FedTermHub " > /dev/null 2>&1; then
            echo "finestra aperta: $(xdotool search --name '^FedTermHub ' getwindowname 2> /dev/null | head -1)"
            kill "$pid"; exit 0
        fi
    elif [ "$i" -ge 24 ]; then
        if kill -0 "$pid" 2> /dev/null; then echo "in esecuzione da 12 secondi"; kill "$pid"; exit 0; fi
    fi
    kill -0 "$pid" 2> /dev/null || break
    sleep 0.5
done
kill "$pid" 2> /dev/null
exit 1
EOF
if [[ $HAVE_X -eq 1 ]]; then
    runner=(bash /tmp/avvio.sh)
    command -v dbus-run-session > /dev/null && runner=(dbus-run-session -- bash /tmp/avvio.sh)
    if ! xvfb "${runner[@]}"; then
        fail "Avvio" "$(cat /tmp/app.log 2> /dev/null | tail -30)"
    fi
    if grep -q Traceback /tmp/app.log 2> /dev/null; then
        fail "Eccezione all'avvio" /tmp/app.log
    fi
fi

# ------------------------------------------------------------------ il motore RDP si collega
HELPER=/usr/libexec/fedtermhub/fedtermrdp-native
if [[ ! -x "$HELPER" ]]; then
    fail "Motore RDP" "$HELPER non installato"
elif pkg xrdp > /tmp/xrdp-install.log 2>&1; then
    # Chiavi e certificato: alcuni pacchetti li creano solo all'avvio del servizio systemd.
    mkdir -p /etc/xrdp /var/run/xrdp
    if [[ ! -s /etc/xrdp/key.pem || ! -s /etc/xrdp/cert.pem ]]; then
        openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=localhost \
            -keyout /etc/xrdp/key.pem -out /etc/xrdp/cert.pem > /dev/null 2>&1
    fi
    if [[ ! -s /etc/xrdp/rsakeys.ini ]] && command -v xrdp-keygen > /dev/null; then
        xrdp-keygen xrdp /etc/xrdp/rsakeys.ini > /dev/null 2>&1 || true
    fi
    xrdp --nodaemon > /tmp/xrdp.log 2>&1 &
    xrdp_pid=$!
    for _ in $(seq 1 20); do
        (exec 3<> /dev/tcp/127.0.0.1/3389) 2> /dev/null && break
        sleep 0.5
    done
    if python3 tests/rdp_engine_probe.py "$HELPER" 127.0.0.1 > /tmp/probe.txt 2>&1; then
        # (le immagini dei container spesso escludono /usr/share/doc: la libreria usata la dice ldd)
        annotate notice "Motore RDP" "$(cat /tmp/probe.txt) — $(ldd "$HELPER" | awk '/libfreerdp3/{print $3}')"
    else
        fail "Motore RDP" "$(cat /tmp/probe.txt; echo; tail -10 /tmp/xrdp.log)"
    fi
    kill "$xrdp_pid" 2> /dev/null || true
else
    annotate warning "Motore RDP" "xrdp non disponibile qui: provato solo l'avvio del motore. $(tail -3 /tmp/xrdp-install.log)"
    usage="$("$HELPER" --help 2>&1)"   # senza argomenti validi stampa l'uso ed esce con errore
    grep -q -- --config-fd <<< "$usage" || fail "Motore RDP" "il motore non parte: $usage"
fi

# ------------------------------------------------------------------ test dei sorgenti con il GTK della distribuzione
if [[ $SOURCE_TESTS -eq 1 ]]; then
    for t in test_core test_distro test_i18n test_vault_ui test_ui_behaviours test_home_session_ui test_revisione; do
        [[ $HAVE_X -eq 1 ]] || break
        if xvfb python3 "tests/$t.py" > "/tmp/$t.log" 2>&1; then
            echo "  OK $t"
        else
            fail "Test $t" "/tmp/$t.log"
        fi
    done
fi

# ------------------------------------------------------------------ rimozione (solo come l'utente)
if [[ "$MODE" == site ]]; then
    sh /tmp/setup.sh --remove > /tmp/remove.log 2>&1
    if command -v dpkg-query > /dev/null && dpkg-query -W -f='${Status}' fedtermhub 2> /dev/null | grep -q "install ok"; then
        fail "Rimozione" "setup.sh --remove ha lasciato il pacchetto"
    elif command -v rpm > /dev/null && [[ "$family" != deb ]] && rpm -q fedtermhub > /dev/null 2>&1; then
        fail "Rimozione" "setup.sh --remove ha lasciato il pacchetto"
    elif [[ -e /etc/yum.repos.d/fedtermhub.repo || -e /etc/apt/sources.list.d/fedtermhub.list ]] \
            || { command -v zypper > /dev/null && zypper lr 2> /dev/null | grep -q fedtermhub; }; then
        fail "Rimozione" "setup.sh --remove ha lasciato il repository"
    else
        echo "rimozione pulita"
    fi
fi

if [[ $FAILED -eq 0 ]]; then
    annotate notice "OK" "fedtermhub $installed installato e funzionante ($ARCH)"
fi
exit $FAILED
