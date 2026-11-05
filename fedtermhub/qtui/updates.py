# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Nuove versioni (solo Windows: su Linux gli aggiornamenti arrivano dal repository).

All'avvio e poi ogni 6 ore il programma chiede all'API pubblica di GitHub qual è l'ultima release (nessun dato
inviato oltre alla richiesta). Se è più recente propone di aggiornare: scarica il Setup.exe della release,
controlla l'impronta SHA-256 pubblicata in SHA256SUMS, chiude FedTermHub e avvia l'installazione, che alla
fine riapre il programma.

Prima la risposta restava in memoria per 24 ore: una versione uscita dopo il primo avvio della giornata
non veniva vista fino al giorno dopo.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from .. import model
from ..i18n import tr

REPO = "Crash-FD01/FedTermHub"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RECHECK_MS = 6 * 3600 * 1000


def enabled() -> bool:
    return sys.platform == "win32"


def _parts(version: str) -> Tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version)[:4])


def newer(candidate: str, current: str) -> bool:
    """True se candidate (es. "v0.9.14") è più recente di current."""
    try:
        return _parts(candidate) > _parts(current)
    except ValueError:
        return False


@dataclass
class Release:
    version: str
    page: str
    setup_url: str = ""
    setup_name: str = ""
    setup_size: int = 0
    sums_url: str = ""


def parse_release(data: dict) -> Optional[Release]:
    """La release dalla risposta dell'API di GitHub, con il Setup.exe e SHA256SUMS se ci sono."""
    tag = str(data.get("tag_name") or "").lstrip("v")
    if not tag:
        return None
    release = Release(tag, str(data.get("html_url") or RELEASES_PAGE))
    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name.endswith("-Setup.exe") and url.startswith("https://github.com/"):
            release.setup_url, release.setup_name, release.setup_size = url, name, int(asset.get("size") or 0)
        elif name == "SHA256SUMS" and url.startswith("https://github.com/"):
            release.sums_url = url
    return release


def _request(url: str, timeout: float = 15.0):  # noqa: ANN202
    headers = {"User-Agent": f"{model.APP_NAME}/{model.APP_VERSION}"}
    if url.startswith("https://api.github.com/"):
        headers["Accept"] = "application/vnd.github+json"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)  # noqa: S310


def fetch_latest() -> Optional[Release]:
    with _request(API_LATEST) as response:
        return parse_release(json.loads(response.read().decode("utf-8")))


def expected_sha256(sums_text: str, name: str) -> str:
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            return parts[0]
    return ""


def download_setup(release: Release, progress: Callable[[int, int], None], cancel: threading.Event) -> Path:
    """Scarica il Setup.exe nella cartella temporanea e ne controlla l'impronta. Restituisce il percorso."""
    if not release.setup_url:
        raise RuntimeError(tr("la release non contiene il Setup.exe"))
    expected = ""
    if release.sums_url:
        with _request(release.sums_url) as response:
            expected = expected_sha256(response.read().decode("utf-8", "replace"), release.setup_name)
    if not expected:
        raise RuntimeError(tr("impronta SHA-256 del Setup.exe non trovata in SHA256SUMS"))
    folder = Path(tempfile.gettempdir()) / "FedTermHub-aggiornamento"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / release.setup_name
    partial = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    done = 0
    with _request(release.setup_url, timeout=60) as response, open(partial, "wb") as out:
        total = int(response.headers.get("Content-Length") or release.setup_size or 0)
        while True:
            if cancel.is_set():
                raise RuntimeError(tr("download annullato"))
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            progress(done, total)
    if digest.hexdigest() != expected:
        partial.unlink(missing_ok=True)
        raise RuntimeError(tr("il file scaricato non corrisponde all'impronta pubblicata: aggiornamento annullato"))
    os.replace(partial, target)
    return target


def launch_setup(setup: Path) -> None:
    """Avvia l'installazione senza domande; il Setup chiude FedTermHub e lo riapre alla fine.

    Installazione per l'utente (in %LOCALAPPDATA%) o per tutti (Programmi, chiede i permessi): la stessa di prima.
    """
    local = os.environ.get("LOCALAPPDATA", "")
    per_user = bool(local) and Path(sys.executable).resolve().is_relative_to(Path(local).resolve())
    args = [str(setup), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RESTART=1",
            "/CURRENTUSER" if per_user else "/ALLUSERS"]
    subprocess.Popen(args, close_fds=True)  # noqa: S603


class _Relay(QObject):
    done = Signal(object)
    progress = Signal(object, object)
    failed = Signal(str)


def check_async(parent: QObject, callback: Callable[[Optional[Release]], None]) -> None:
    """Chiama callback(release più recente o None) nel thread dell'interfaccia."""
    relay = _Relay(parent)
    relay.done.connect(callback)
    relay.done.connect(relay.deleteLater)

    def worker() -> None:
        try:
            release = fetch_latest()
        except Exception:  # noqa: BLE001 (niente rete: nessun avviso)
            release = None
        relay.done.emit(release)

    threading.Thread(target=worker, name="fth-update-check", daemon=True).start()


def download_async(parent: QObject, release: Release, progress: Callable[[int, int], None],
                   done: Callable[[Path], None], failed: Callable[[str], None], cancel: threading.Event) -> None:
    relay = _Relay(parent)
    relay.progress.connect(progress)
    relay.done.connect(done)
    relay.failed.connect(failed)

    def worker() -> None:
        try:
            path = download_setup(release, relay.progress.emit, cancel)
        except Exception as exc:  # noqa: BLE001
            relay.failed.emit(str(exc))
        else:
            relay.done.emit(path)

    threading.Thread(target=worker, name="fth-update-download", daemon=True).start()
