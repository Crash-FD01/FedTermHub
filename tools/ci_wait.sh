#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Aspetta la fine dei controlli di GitHub Actions su una pull request o un commit e stampa un riassunto
# corto: quante prove sono riuscite, e per quelle fallite il nome, il job e le righe di errore del log.
# Usa solo la REST API (gh api), non GraphQL.
#
#   tools/ci_wait.sh 25                  pull request n. 25 (l'ultimo commit)
#   tools/ci_wait.sh 7b0c716...          un commit
#   tools/ci_wait.sh 25 --windows        in più: il risultato del self-test di Windows, riga per riga
#   tools/ci_wait.sh 25 --windows DIR    in più: le schermate del self-test salvate in DIR
#
# Esce con 0 se tutto è riuscito, 1 se qualcosa è fallito.
set -u
repo="${FTH_REPO:-Crash-FD01/FedTermHub}"
ref="${1:?uso: $0 <numero PR | sha> [--windows [cartella schermate]]}"
windows="${2:-}"
shots="${3:-}"
if [[ "$ref" =~ ^[0-9]+$ ]]; then
    sha="$(gh api "repos/$repo/pulls/$ref" --jq .head.sha)"
else
    sha="$ref"
fi

sleep "${FTH_CI_FIRST_WAIT:-20}"
while true; do
    runs="$(gh api "repos/$repo/commits/$sha/check-runs?per_page=100" \
        --jq '.check_runs[] | "\(.status)\t\(.conclusion)\t\(.id)\t\(.name)"' 2>/dev/null || true)"
    if [ -n "$runs" ] && ! printf '%s\n' "$runs" | awk -F'\t' '$1!="completed"' | grep -q .; then break; fi
    sleep "${FTH_CI_POLL:-60}"
done

ok="$(printf '%s\n' "$runs" | awk -F'\t' '$2=="success"||$2=="skipped"||$2=="neutral"' | wc -l)"
bad="$(printf '%s\n' "$runs" | awk -F'\t' '!($2=="success"||$2=="skipped"||$2=="neutral")')"
echo "CI ${sha:0:7}: $ok controlli riusciti, $(printf '%s' "$bad" | grep -c .) falliti"
status=0
while IFS=$'\t' read -r _st conclusion id name; do
    [ -z "${id:-}" ] && continue
    status=1
    echo "FAIL $name ($conclusion, job $id)"
    gh api "repos/$repo/actions/jobs/$id/logs" 2>/dev/null \
        | grep -E '##\[error\]|Error|FAIL|Traceback|AssertionError' | grep -v -E '^\S+ \s*$' \
        | tail -n 20 | sed -E 's/^[0-9TZ:.-]+ //; s/^/     | /'
done <<< "$bad"

if [ "$windows" = "--windows" ]; then
    id="$(printf '%s\n' "$runs" | awk -F'\t' '$4=="Setup.exe per Windows"{print $3}' | head -n 1)"
    if [ -n "$id" ]; then
        log="$(mktemp)"
        gh api "repos/$repo/actions/jobs/$id/logs" > "$log" 2>/dev/null
        echo "Self-test di Windows (job $id):"
        grep -E '^\S+ (OK   |FAIL )' "$log" | sed -E 's/^\S+ /  /'
        if [ -n "$shots" ]; then
            mkdir -p "$shots"
            python3 - "$log" "$shots" <<'PY'
import base64, re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
for m in re.finditer(r"=== SCREENSHOT (\S+) ===\n(.*?)=== END", text, re.S):
    data = "".join(re.sub(r"^\S+ ", "", line) for line in m.group(2).splitlines())
    path = f"{sys.argv[2]}/{m.group(1)}"
    open(path, "wb").write(base64.b64decode(data))
    print("  schermata:", path)
PY
        fi
        rm -f "$log"
    fi
fi
exit "$status"
