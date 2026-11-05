# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Client SFTP dell'interfaccia Qt, con paramiko.

Su Linux la versione GTK usa il client sftp di OpenSSH con sshpass; su Windows sshpass non esiste e il
client sftp in modalità batch non accetta una password. paramiko parla SSH direttamente: password salvata,
chiavi in ~/.ssh, agente SSH (anche quello di Windows) e jump host.

Le chiavi degli host sono quelle di ~/.ssh/known_hosts, lo stesso file di ssh.exe: una chiave accettata qui
vale anche nel terminale e viceversa. Tutte le operazioni girano in un thread (SftpWorker), una alla volta.
"""
from __future__ import annotations

import os
import posixpath
import queue
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, Signal

from ..i18n import tr

KNOWN_HOSTS = Path.home() / ".ssh" / "known_hosts"


class SftpError(Exception):
    """Errore da mostrare all'utente (già tradotto)."""


class NoSftp(SftpError):
    """Il server SSH non offre SFTP (apparati di rete, sottosistema disattivato)."""


class HostKeyQuestion(Exception):
    """Chiave dell'host mai vista: serve la conferma dell'utente."""

    def __init__(self, host: str, key_type: str, fingerprint: str) -> None:
        super().__init__(host)
        self.host, self.key_type, self.fingerprint = host, key_type, fingerprint


class AuthNeeded(Exception):
    """Nessuna chiave e nessun agente hanno funzionato e non c'è una password: va chiesta."""


@dataclass
class SftpTarget:
    host: str
    port: int = 22
    user: str = ""
    password: Optional[str] = None
    jump_host: str = ""
    jump_port: int = 22
    jump_user: str = ""
    timeout: float = 12.0
    strict: str = "ask"  # ask, accept-new, yes, no (come StrictHostKeyChecking)
    approved_fingerprints: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        target = f"{self.user}@{self.host}" if self.user else self.host
        return target if self.port == 22 else f"{target}:{self.port}"


@dataclass
class RemoteItem:
    name: str
    path: str
    is_dir: bool
    size: int
    mtime: int
    mode: int
    is_link: bool = False

    @property
    def permissions(self) -> str:
        return stat.filemode(self.mode) if self.mode else ""


def fingerprint(key) -> str:  # noqa: ANN001
    import base64
    import hashlib

    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _host_entry(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def _friendly(exc: Exception) -> str:
    import socket

    import paramiko

    if isinstance(exc, SftpError):
        return str(exc)
    if isinstance(exc, paramiko.BadHostKeyException):
        return tr("ATTENZIONE: la chiave dell'host {host} è cambiata rispetto a quella salvata in known_hosts. "
                  "Potrebbe essere un server reinstallato oppure un attacco: controlla prima di continuare.").format(
            host=exc.hostname)
    if isinstance(exc, paramiko.AuthenticationException):
        return tr("Accesso negato: utente, password o chiave non accettati dal server.")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return tr("Il server non risponde (timeout).")
    if isinstance(exc, ConnectionRefusedError):
        return tr("Connessione rifiutata: sul server non c'è SSH su quella porta.")
    if isinstance(exc, socket.gaierror):
        return tr("Nome del server non trovato.")
    if isinstance(exc, PermissionError) or getattr(exc, "errno", None) == 13:
        return tr("Permesso negato sul server.")
    if isinstance(exc, FileNotFoundError) or getattr(exc, "errno", None) == 2:
        return tr("File o cartella inesistente.")
    if isinstance(exc, paramiko.SSHException) and "subsystem" in str(exc).lower():
        return tr("Il server non offre SFTP (sottosistema sftp disattivato).")
    return str(exc) or type(exc).__name__


class SftpSession:
    """Una connessione SFTP sincrona (da usare dentro SftpWorker)."""

    def __init__(self, target: SftpTarget) -> None:
        self.target = target
        self.client = None
        self.jump = None
        self.sftp = None

    def _policy(self):  # noqa: ANN202
        import paramiko

        target = self.target

        class Policy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):  # noqa: ANN001, N802
                fp = fingerprint(key)
                if target.strict == "no":
                    return
                if target.strict == "yes" and fp not in target.approved_fingerprints:
                    raise SftpError(tr("La chiave dell'host {host} non è in known_hosts e le impostazioni chiedono "
                                       "di accettare solo host già conosciuti.").format(host=hostname))
                if target.strict == "accept-new" or fp in target.approved_fingerprints:
                    client.get_host_keys().add(hostname, key.get_name(), key)
                    _save_known_host(hostname, key)
                    return
                raise HostKeyQuestion(hostname, key.get_name(), fp)

        return Policy()

    def _new_client(self):  # noqa: ANN202
        import paramiko

        client = paramiko.SSHClient()
        if KNOWN_HOSTS.is_file():
            try:
                client.load_host_keys(str(KNOWN_HOSTS))
            except Exception:  # noqa: BLE001 (riga illeggibile: si va avanti con le altre)
                pass
        client.set_missing_host_key_policy(self._policy())
        return client

    def _connect_client(self, client, host: str, port: int, user: str, password: Optional[str], sock=None) -> None:  # noqa: ANN001
        import paramiko

        kwargs = dict(hostname=host, port=port, username=user or None, timeout=self.target.timeout,
                      banner_timeout=self.target.timeout, auth_timeout=self.target.timeout, sock=sock,
                      allow_agent=True, look_for_keys=True)
        if password:
            kwargs["password"] = password
        try:
            client.connect(**kwargs)
        except paramiko.AuthenticationException:
            if not password:
                raise AuthNeeded() from None
            raise
        except paramiko.SSHException as exc:
            if not password and "No authentication methods available" in str(exc):
                raise AuthNeeded() from None
            raise

    def open(self) -> str:
        """Si collega e restituisce la cartella iniziale (la home dell'utente)."""
        target = self.target
        sock = None
        try:
            if target.jump_host:
                self.jump = self._new_client()
                self._connect_client(self.jump, target.jump_host, target.jump_port, target.jump_user or target.user,
                                     target.password)
                sock = self.jump.get_transport().open_channel("direct-tcpip", (target.host, target.port), ("127.0.0.1", 0))
            self.client = self._new_client()
            self._connect_client(self.client, target.host, target.port, target.user, target.password, sock=sock)
            self.client.get_transport().set_keepalive(30)
            try:
                self.sftp = self.client.open_sftp()
            except Exception as exc:  # noqa: BLE001
                raise NoSftp(tr("Il server non offre SFTP (sottosistema sftp disattivato).")) from exc
            return self.sftp.normalize(".")
        except (HostKeyQuestion, AuthNeeded, NoSftp):
            self.close()
            raise
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise SftpError(_friendly(exc)) from exc

    def close(self) -> None:
        for item in (self.sftp, self.client, self.jump):
            try:
                if item is not None:
                    item.close()
            except Exception:  # noqa: BLE001
                pass
        self.sftp = self.client = self.jump = None

    @property
    def connected(self) -> bool:
        transport = self.client.get_transport() if self.client is not None else None
        return bool(self.sftp is not None and transport is not None and transport.is_active())

    # ------------------------------------------------------------------ operazioni
    def listdir(self, path: str) -> List[RemoteItem]:
        items = []
        for attr in self.sftp.listdir_attr(path):
            if attr.filename in {".", ".."}:
                continue
            full = posixpath.join(path, attr.filename)
            mode = attr.st_mode or 0
            is_link = stat.S_ISLNK(mode)
            is_dir = stat.S_ISDIR(mode)
            if is_link:
                try:
                    is_dir = stat.S_ISDIR(self.sftp.stat(full).st_mode or 0)
                except OSError:
                    is_dir = False
            items.append(RemoteItem(attr.filename, full, is_dir, attr.st_size or 0, attr.st_mtime or 0, mode, is_link))
        items.sort(key=lambda item: (not item.is_dir, item.name.casefold()))
        return items

    def is_dir(self, path: str) -> bool:
        return stat.S_ISDIR(self.sftp.stat(path).st_mode or 0)

    def download(self, remote: str, local: Path, progress: Callable[[int, int], None]) -> None:
        if self.is_dir(remote):
            local.mkdir(parents=True, exist_ok=True)
            for item in self.listdir(remote):
                self.download(item.path, local / item.name, progress)
            return
        tmp = local.with_name(local.name + ".part")
        self.sftp.get(remote, str(tmp), callback=progress)
        os.replace(tmp, local)

    def upload(self, local: Path, remote_dir: str, progress: Callable[[int, int], None]) -> None:
        remote = posixpath.join(remote_dir, local.name)
        if local.is_dir():
            try:
                self.sftp.mkdir(remote)
            except OSError:
                if not self.is_dir(remote):
                    raise
            for child in sorted(local.iterdir()):
                self.upload(child, remote, progress)
            return
        self.sftp.put(str(local), remote, callback=progress)

    def mkdir(self, path: str) -> None:
        self.sftp.mkdir(path)

    def rename(self, old: str, new: str) -> None:
        self.sftp.rename(old, new)

    def remove(self, path: str) -> None:
        if self.is_dir(path):
            for item in self.listdir(path):
                if item.is_dir and not item.is_link:
                    self.remove(item.path)
                else:
                    self.sftp.remove(item.path)
            self.sftp.rmdir(path)
        else:
            self.sftp.remove(path)


def _save_known_host(hostname: str, key) -> None:  # noqa: ANN001
    """Aggiunge una riga a ~/.ssh/known_hosts (stesso formato di OpenSSH), senza riscrivere il resto."""
    try:
        KNOWN_HOSTS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(KNOWN_HOSTS, "a", encoding="utf-8") as handle:
            handle.write(f"{hostname} {key.get_name()} {key.get_base64()}\n")
    except OSError:
        pass


class SftpWorker(QObject):
    """Esegue le operazioni SFTP in un thread, una dopo l'altra; i risultati tornano come segnali.

    done(tag, risultato), failed(tag, messaggio), host_key(tag, HostKeyQuestion), auth_needed(tag),
    progress(byte_fatti, byte_totali).
    """

    done = Signal(str, object)
    failed = Signal(str, str)
    host_key = Signal(str, object)
    auth_needed = Signal(str)
    progress = Signal(object, object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.session: Optional[SftpSession] = None
        self._jobs: "queue.Queue" = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="fth-sftp", daemon=True)
        self._thread.start()
        self.cancel = threading.Event()

    def _loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            tag, func = job
            try:
                result = func()
            except HostKeyQuestion as question:
                self.host_key.emit(tag, question)
            except AuthNeeded:
                self.auth_needed.emit(tag)
            except NoSftp as exc:
                self.failed.emit("nosftp", str(exc))
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(tag, _friendly(exc))
            else:
                self.done.emit(tag, result)

    def submit(self, tag: str, func: Callable[[], object]) -> None:
        self._jobs.put((tag, func))

    def connect_to(self, target: SftpTarget) -> None:
        def job() -> str:
            if self.session is not None:
                self.session.close()
            session = SftpSession(target)
            home = session.open()
            self.session = session
            return home

        self.submit("connect", job)

    def run(self, tag: str, func: Callable[[SftpSession], object]) -> None:
        def job() -> object:
            if self.session is None or not self.session.connected:
                raise SftpError(tr("Connessione SFTP chiusa: riconnetti."))
            return func(self.session)

        self.submit(tag, job)

    def progress_callback(self) -> Callable[[int, int], None]:
        def report(done: int, total: int) -> None:
            if self.cancel.is_set():
                raise SftpError(tr("Trasferimento annullato."))
            self.progress.emit(done, total)

        return report

    def shutdown(self) -> None:
        def job() -> None:
            if self.session is not None:
                self.session.close()
                self.session = None

        self.cancel.set()
        self._jobs.put(("close", job))
        self._jobs.put(None)
