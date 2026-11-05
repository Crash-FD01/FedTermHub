#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Tutte le prove in locale, con un riassunto corto: una riga per prova, e le ultime righe del log solo per
# quelle che falliscono. I log completi restano in una cartella temporanea (stampata alla fine).
#
#   tools/test_all.sh            prove veloci + Linux (GTK) + Windows (Qt)
#   tools/test_all.sh fast       solo le prove senza interfaccia (nucleo, versioni, traduzioni, temi)
#   tools/test_all.sh linux      veloci + interfaccia GTK (sotto xvfb-run)
#   tools/test_all.sh qt         veloci + interfaccia Qt (self-test compreso)
#
# Python per GTK: il primo fra python3, python3.13, python3.12, python3.11 che importa «gi».
# Python per Qt: $FTH_QT_PYTHON, altrimenti .venv-qt/ (creato al primo uso da packaging/windows/requirements.txt,
# senza i pacchetti solo-Windows). Con FEDTERMHUB_SELFTEST_SSH="host;porta;utente;password" il self-test Qt
# prova anche SSH, SFTP e tunnel.
set -u
cd "$(dirname "$0")/.."
what="${1:-all}"
logs="$(mktemp -d "${TMPDIR:-/tmp}/fth-test.XXXXXX")"
export FEDTERMHUB_LANGUAGE="${FEDTERMHUB_LANGUAGE:-it}"
failed=0
passed=0

run() {  # run <nome> <comando...>
    local name="$1"; shift
    local log="$logs/$name.log" start=$SECONDS
    if timeout "${FTH_TEST_TIMEOUT:-900}" "$@" >"$log" 2>&1; then
        passed=$((passed + 1)); printf 'OK   %-34s %3ss\n' "$name" $((SECONDS - start))
    else
        failed=$((failed + 1)); printf 'FAIL %-34s %3ss\n' "$name" $((SECONDS - start))
        grep -v -E '^\s*$|bus\.cc|command_buffer|DRI3|Fontconfig' "$log" | tail -n 15 | sed 's/^/     | /'
    fi
}

gtk_python() {
    for py in python3 python3.13 python3.12 python3.11; do
        command -v "$py" >/dev/null && "$py" -c 'import gi' 2>/dev/null && { echo "$py"; return; }
    done
}

qt_python() {
    if [ -n "${FTH_QT_PYTHON:-}" ]; then echo "$FTH_QT_PYTHON"; return; fi
    if [ ! -x .venv-qt/bin/python ]; then
        echo "Creo .venv-qt (solo la prima volta, qualche minuto)..." >&2
        python3 -m venv .venv-qt >&2 && .venv-qt/bin/pip install -q \
            $(grep -v -E '^#|pywinpty|comtypes' packaging/windows/requirements.txt) >&2 || return 1
    fi
    echo .venv-qt/bin/python
}

fast() {
    local py
    py="$(gtk_python)"; py="${py:-python3}"
    run compileall "$py" -m compileall -q fedtermhub tests
    run test_commands "$py" tests/test_commands.py
    run test_versions "$py" tests/test_versions.py
    run test_theme_parity "$py" tests/test_theme_parity.py
    run test_distro "$py" tests/test_distro.py
    run test_connection_diagnostics "$py" tests/test_connection_diagnostics.py
    run i18n_catalogo "$py" tools/i18n_update.py --check
    if command -v reuse >/dev/null; then run reuse reuse lint; fi
}

linux() {
    local py
    py="$(gtk_python)"
    if [ -z "$py" ]; then echo "SALTO GTK: nessun python3 con «gi» (python3-gi)"; return; fi
    for t in test_core test_rdp_monitor_ui test_rdp_rendering test_connection_diagnostics_ui test_home_session_ui \
             test_routing_ui test_remote_files test_ephemeral_lab test_external_rdp_cli test_ui_behaviours \
             test_vault_ui test_revisione test_network_tools test_i18n; do
        run "$t" xvfb-run -a -s "-screen 0 1400x900x24" "$py" "tests/$t.py"
    done
}

qt() {
    local py
    py="$(qt_python)" || { echo "SALTO Qt: non riesco a creare .venv-qt"; return; }
    run test_qtui env QT_QPA_PLATFORM=offscreen QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox "$py" tests/test_qtui.py
}

case "$what" in
    fast) fast ;;
    linux) fast; linux ;;
    qt) fast; qt ;;
    all) fast; linux; qt ;;
    *) echo "uso: $0 [all|fast|linux|qt]"; exit 2 ;;
esac
echo "---- $passed riuscite, $failed fallite — log in $logs"
[ "$failed" -eq 0 ]
