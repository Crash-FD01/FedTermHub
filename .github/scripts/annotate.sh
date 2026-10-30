#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Esegue un comando e, se fallisce, ne riporta le ultime righe come annotazione della CI: si leggono
# nella pagina della pull request senza aprire (o scaricare) il log completo.
#
#   .github/scripts/annotate.sh "<titolo>" comando argomenti...
set -uo pipefail
title="$1"
shift
log="$(mktemp)"
"$@" 2>&1 | tee "$log"
status="${PIPESTATUS[0]}"
if [[ "$status" -ne 0 ]]; then
    body="$(grep -v -e '^\s*$' "$log" | tail -40 | sed -e 's/%/%25/g' -e 's/\r//g' | awk 'BEGIN{ORS="%0A"} {print}')"
    echo "::error title=$title (uscita $status)::$body"
fi
rm -f "$log"
exit "$status"
