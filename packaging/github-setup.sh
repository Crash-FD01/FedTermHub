#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Prepara la distribuzione di FedTermHub su GitHub. Va eseguito una volta, sul tuo PC, da una copia
# aggiornata del ramo main:
#
#   git clone https://github.com/Crash-FD01/FedTermHub.git && cd FedTermHub
#   packaging/github-setup.sh
#
# Serve: git, gh (GitHub CLI, con «gh auth login» gia' fatto), gpg. Prima di cambiare qualcosa mostra
# cosa fara' e chiede conferma. Si puo' rieseguire: salta quello che e' gia' fatto.
#
#   1. imposta descrizione, sito, argomenti e le segnalazioni private delle vulnerabilita';
#   2. attiva GitHub Pages (pubblicate dal workflow «Rilascio») e permette ai tag v* di aggiornarle;
#   3. crea la chiave che firma i pacchetti: la privata va nel secret FEDTERMHUB_GPG_PRIVATE_KEY e in una
#      copia di backup per te, la pubblica in packaging/signing-key.asc;
#   4. pubblica il tag della versione: il workflow «Rilascio» costruisce, firma e pubblica tutto.
#
# La chiave privata non passa da nessun altro posto: la generi tu, qui, e finisce solo nel secret di
# GitHub e nel tuo backup.
set -euo pipefail
cd "$(dirname "$0")/.."

DESCRIPTION="Terminals, SSH, RDP/VNC remote desktops and SFTP files in one window, for Linux sysadmins"
TOPICS="linux,fedora,ssh,rdp,vnc,sftp,terminal,session-manager,remote-desktop,sysadmin,gtk3,freerdp,vte"
BACKUP="$HOME/fedtermhub-signing-key-PRIVATE.asc"
SECRET=FEDTERMHUB_GPG_PRIVATE_KEY

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf 'Errore: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" > /dev/null || die "manca $1 (${2})"; }

need git "sudo dnf install git"
need gh "sudo dnf install gh — poi: gh auth login"
need gpg "sudo dnf install gnupg2"
gh auth status > /dev/null 2>&1 || die "prima accedi a GitHub con: gh auth login"

REPO="$(sed -n 's|^SPDX-PackageDownloadLocation = "https://github.com/\(.*\)"|\1|p' REUSE.toml)"
[[ -n "$REPO" ]] || die "non trovo il nome del repository in REUSE.toml"
OWNER_LC="${REPO%%/*}"; OWNER_LC="${OWNER_LC,,}"
SITE_URL="https://$OWNER_LC.github.io/${REPO#*/}"
VERSION="$(sed -n 's/^Version:[[:space:]]*//p' fedtermhub.spec | head -n1)"
TAG="v$VERSION"
LOGIN="$(gh api user --jq .login)"
USER_ID="$(gh api user --jq .id)"

[[ "$(git rev-parse --abbrev-ref HEAD)" == main ]] || die "esegui lo script dal ramo main"
[[ -z "$(git status --porcelain)" ]] || die "ci sono modifiche non salvate: fai commit o annullale prima"
git fetch -q origin main
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || die "il ramo main locale non e' allineato a GitHub: git pull"

bold "FedTermHub $VERSION → https://github.com/$REPO"
echo "  sito e repository dei pacchetti: $SITE_URL"
echo "  accesso a GitHub come: $LOGIN"
read -rp "Procedo? [s/N] " answer
[[ "$answer" =~ ^[sSyY] ]] || exit 0

# ------------------------------------------------------------------ 1. impostazioni
bold "Imposto descrizione, sito e argomenti"
gh repo edit "$REPO" --description "$DESCRIPTION" --homepage "$SITE_URL" --enable-issues \
    --enable-wiki=false --add-topic "$TOPICS" > /dev/null
gh api -X PUT "repos/$REPO/private-vulnerability-reporting" > /dev/null 2>&1 \
    || echo "  (segnalazioni private delle vulnerabilita': attivale da Settings → Security)"

# ------------------------------------------------------------------ 2. GitHub Pages
bold "Attivo GitHub Pages"
if gh api "repos/$REPO/pages" > /dev/null 2>&1; then
    gh api -X PUT "repos/$REPO/pages" -f build_type=workflow > /dev/null
else
    gh api -X POST "repos/$REPO/pages" -f build_type=workflow > /dev/null
fi
# l'ambiente github-pages di norma accetta solo il ramo principale: il sito lo pubblica il tag della versione
gh api -X PUT "repos/$REPO/environments/github-pages" --input - > /dev/null <<'EOF'
{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
EOF
gh api -X POST "repos/$REPO/environments/github-pages/deployment-branch-policies" -f name=main -f type=branch \
    > /dev/null 2>&1 || true   # gia' presente
gh api -X POST "repos/$REPO/environments/github-pages/deployment-branch-policies" -f 'name=v*' -f type=tag \
    > /dev/null 2>&1 || true

# ------------------------------------------------------------------ 3. chiave di firma
if gh secret list -R "$REPO" 2> /dev/null | grep -q "^$SECRET"; then
    echo "  chiave di firma gia' configurata"
else
    bold "Creo la chiave che firma i pacchetti"
    KEYHOME="$(mktemp -d /tmp/fthkey.XXXXXX)"   # percorso corto: il socket di gpg-agent ha un limite
    trap 'GNUPGHOME="$KEYHOME" gpgconf --kill gpg-agent 2>/dev/null || true; rm -rf "$KEYHOME"' EXIT
    chmod 700 "$KEYHOME"
    GNUPGHOME="$KEYHOME" gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
        --quick-gen-key "FedTermHub packages <$USER_ID+$LOGIN@users.noreply.github.com>" rsa4096 sign never
    FPR="$(GNUPGHOME="$KEYHOME" gpg --list-keys --with-colons | awk -F: '/^fpr/{print $10; exit}')"
    [[ -e "$BACKUP" ]] && mv "$BACKUP" "$BACKUP.$(date +%Y%m%d%H%M%S).old"
    (umask 077; GNUPGHOME="$KEYHOME" gpg --armor --export-secret-keys "$FPR" > "$BACKUP")
    gh secret set "$SECRET" -R "$REPO" < "$BACKUP"
    GNUPGHOME="$KEYHOME" gpg --armor --export "$FPR" > packaging/signing-key.asc
    git add packaging/signing-key.asc
    git commit -q -m "Chiave pubblica che firma i pacchetti"
    git push -q origin HEAD:main
    echo "  impronta: $FPR"
    echo "  copia di backup della chiave privata: $BACKUP"
    echo "  → spostala in un posto sicuro (gestore di password, chiavetta) e cancellala da qui."
    echo "    Non va mai nel repository: chi la ha puo' firmare aggiornamenti a nome tuo."
fi

# ------------------------------------------------------------------ 4. rilascio
if git ls-remote --exit-code --tags origin "refs/tags/$TAG" > /dev/null 2>&1; then
    echo "  il tag $TAG e' gia' pubblicato: per ripubblicarlo usa Actions → Rilascio → Run workflow"
else
    bold "Pubblico $TAG: parte il workflow di rilascio"
    git tag -a "$TAG" -m "FedTermHub $VERSION"
    git push -q origin "$TAG"
fi

bold "✓ Fatto"
echo "  Repository:  https://github.com/$REPO"
echo "  Rilascio:    https://github.com/$REPO/actions (qualche minuto; oppure: gh run watch -R $REPO)"
echo "  Poi il sito: $SITE_URL"
echo "  Installazione per gli utenti: curl -fsSL $SITE_URL/setup.sh | sudo sh"
echo
echo "  Prossime versioni: aggiorna Version e %changelog nello spec (e la release nel metainfo),"
echo "  fai commit e push, poi: git tag -a vX.Y.Z -m 'FedTermHub X.Y.Z' && git push origin vX.Y.Z"
