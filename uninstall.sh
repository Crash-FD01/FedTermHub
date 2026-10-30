#!/usr/bin/env bash
set -euo pipefail
rm -rf "${HOME}/.local/share/fedtermhub"
rm -f "${HOME}/.local/bin/fedtermhub"
rm -rf "${HOME}/.local/libexec/fedtermhub"  # motore RDP e, se compilata, la sua FreeRDP
rm -f "${HOME}/.local/share/applications/fedtermhub.desktop"
rm -f "${HOME}/.local/share/applications/io.github.crash_fd01.fedtermhub.desktop"
rm -f "${HOME}/.local/share/icons/hicolor/scalable/apps/fedtermhub.svg"
find "${HOME}/.local/share/icons/hicolor" -path '*/apps/io.github.crash_fd01.fedtermhub.png' -delete 2>/dev/null || true
rm -f "${HOME}/.local/share/pixmaps/io.github.crash_fd01.fedtermhub.png"
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${HOME}/.local/share/applications" || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache "${HOME}/.local/share/icons/hicolor" || true
fi
echo "FedTermHub rimosso. Le sessioni salvate restano in ~/.config/fedtermhub; rimuovile a mano se vuoi cancellarle."
