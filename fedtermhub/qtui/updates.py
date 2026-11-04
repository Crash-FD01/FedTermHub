# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Avviso delle nuove versioni (solo Windows: su Linux gli aggiornamenti arrivano dal repository).

Una richiesta al giorno all'API pubblica di GitHub, in un thread: nessun dato inviato oltre alla
richiesta stessa, e se la rete non c'è non succede nulla.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.request
from typing import Callable, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from .. import model

REPO = "Crash-FD01/FedTermHub"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
STAMP_FILE = model.CACHE_DIR / "update-check.json"
INTERVAL = 24 * 3600


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


def _due() -> bool:
    try:
        last = json.loads(STAMP_FILE.read_text(encoding="utf-8")).get("checked", 0)
        return time.time() - float(last) > INTERVAL
    except Exception:
        return True


def _remember(latest: str) -> None:
    try:
        STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STAMP_FILE.write_text(json.dumps({"checked": time.time(), "latest": latest}), encoding="utf-8")
    except OSError:
        pass


def _cached_latest() -> str:
    try:
        return str(json.loads(STAMP_FILE.read_text(encoding="utf-8")).get("latest", ""))
    except Exception:
        return ""


def fetch_latest(timeout: float = 10.0) -> Optional[str]:
    request = urllib.request.Request(API_LATEST, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{model.APP_NAME}/{model.APP_VERSION}",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (URL fisso https)
        data = json.loads(response.read().decode("utf-8"))
    tag = str(data.get("tag_name") or "").lstrip("v")
    return tag or None


class _Relay(QObject):
    done = Signal(object)


def check_async(parent: QObject, callback: Callable[[Optional[str]], None]) -> None:
    """Chiama callback(versione_più_recente o None) nel thread dell'interfaccia."""
    relay = _Relay(parent)
    relay.done.connect(callback)
    if not _due():
        relay.done.emit(_cached_latest() or None)
        return

    def worker() -> None:
        try:
            latest = fetch_latest()
        except Exception:
            latest = None
        if latest:
            _remember(latest)
        relay.done.emit(latest)

    threading.Thread(target=worker, name="fth-update-check", daemon=True).start()
