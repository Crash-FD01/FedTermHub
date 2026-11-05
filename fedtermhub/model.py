# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Il nucleo di FedTermHub, senza interfaccia grafica: lo usano sia la versione GTK (Linux) sia quella Qt
(Windows, poi anche Linux).

Contiene percorsi di configurazione, sessioni e cartelle, Password Manager cifrato, tunnel SSH e
impostazioni. I formati dei file sono gli stessi su tutti i sistemi: sessioni e backup del Password
Manager esportati su Linux si importano su Windows e viceversa.

Niente GTK, Qt o altre librerie grafiche qui: un test può importarlo ovunque.
"""
from __future__ import annotations

import base64
import glob
import json
import os
import secrets
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union

try:
    from .i18n import SUPPORTED as I18N_LANGUAGES, tr
except ImportError:  # avvio come script (fedtermhub/fedtermhub.py) o dai test
    from i18n import SUPPORTED as I18N_LANGUAGES, tr  # type: ignore[no-redef]

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    CRYPTO_AVAILABLE = True
except Exception:  # pragma: no cover - dipendenza runtime
    InvalidTag = Exception
    AESGCM = None
    hashes = None
    PBKDF2HMAC = None
    CRYPTO_AVAILABLE = False

# La versione si scrive solo qui (e in fedtermhub.spec, install.sh e nel metainfo): tests/test_versions.py
# controlla che coincidano.
APP_ID = "io.github.crash_fd01.fedtermhub"
APP_NAME = "FedTermHub"
APP_VERSION = "0.10.1"

IS_WINDOWS = sys.platform == "win32"


def _config_root() -> Path:
    """Linux: ~/.config/fedtermhub (XDG). Windows: %APPDATA%\\FedTermHub."""
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")) / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "fedtermhub"


def _cache_root() -> Path:
    """Linux: ~/.cache/fedtermhub (XDG). Windows: %LOCALAPPDATA%\\FedTermHub."""
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / APP_NAME
    return Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "fedtermhub"


CONFIG_DIR = _config_root()
SESSIONS_FILE = CONFIG_DIR / "sessions.json"
TUNNELS_FILE = CONFIG_DIR / "tunnels.json"
VAULT_FILE = CONFIG_DIR / "vault.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
CACHE_DIR = _cache_root()
LOG_FILE = CACHE_DIR / "fedtermhub.log"
KDF_ITERATIONS = 600_000


def normalize_rdp_drive_path(raw: str) -> str:
    """Valida e normalizza la cartella locale condivisa via /drive: FreeRDP.

    L'opzione /drive:nome,percorso usa la virgola come separatore di
    sotto-opzioni e la config del backend nativo usa il newline come
    separatore di riga: questi caratteri nel percorso romperebbero il parsing
    o permetterebbero di iniettare opzioni arbitrarie. Il percorso deve
    inoltre esistere ed essere una cartella, altrimenti FreeRDP fallisce in
    modo poco leggibile solo a connessione avviata.
    """
    candidate = (raw or "").strip()
    if not candidate:
        return ""
    if any(ch in candidate for ch in (",", "\n", "\r")):
        raise ValueError(
            tr("La cartella condivisa contiene caratteri non supportati (virgola o fine riga).")
        )
    expanded = Path(candidate).expanduser()
    if not expanded.exists():
        raise ValueError(tr("La cartella condivisa non esiste: {expanded}").format(expanded=expanded))
    if not expanded.is_dir():
        raise ValueError(tr("La cartella condivisa non è una cartella: {expanded}").format(expanded=expanded))
    return str(expanded)


def split_host_port(text: str, default_port: str = "") -> Tuple[str, str]:
    """Separa host e porta: "host:22", "[fe80::1]:22", "fe80::1" (IPv6 senza porta), "host".

    L'IPv6 con porta va scritto tra parentesi quadre, come in ssh e nei browser: senza parentesi
    i due punti fanno parte dell'indirizzo.
    """
    text = (text or "").strip()
    if text.startswith("["):
        end = text.find("]")
        if end > 0:
            host, rest = text[1:end], text[end + 1:]
            if rest.startswith(":") and rest[1:].isdigit():
                return host, rest[1:]
            return host, default_port
    if text.count(":") == 1:
        host, port = text.split(":", 1)
        if port.isdigit():
            return host, port
    return text, default_port


def host_for_uri(host: str) -> str:
    """IPv6 tra parentesi quadre quando va seguito da :porta."""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def vnc_target(host: str, port: str) -> str:
    """Destinazione per vncviewer: "host:N" e' il display N (porta 5900+N), "host::P" la porta P.

    Il campo della sessione accetta entrambi: un numero piccolo e' un display, uno da 100 in su
    una porta. Prima "5901" veniva passato come display e TigerVNC si collegava alla porta 11801.
    """
    host = host_for_uri(host.strip())
    port = (port or "").strip()
    if not port:
        return host
    if port.isdigit() and int(port) >= 100:
        return f"{host}::{port}"
    return f"{host}:{port}"


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("ascii"))


def ensure_private_config_dir() -> None:
    """Create and harden the per-user state directory.

    Session definitions can contain host names, user names and operational
    notes; keeping the complete directory private avoids leaking metadata to
    other local users. Existing installations are tightened in place.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    for state_file in (SESSIONS_FILE, TUNNELS_FILE, VAULT_FILE, SETTINGS_FILE):
        if state_file.is_file():
            try:
                os.chmod(state_file, 0o600)
            except OSError:
                pass


def atomic_write_text(path: Path, text: str, mode: Optional[int] = None) -> None:
    """Atomically replace a UTF-8 text file without a predictable temp name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        # Su Windows i permessi POSIX non esistono: la cartella %APPDATA% è già privata dell'utente.
        if mode is not None and hasattr(os, "fchmod") and not IS_WINDOWS:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        if mode is not None and not IS_WINDOWS:
            os.chmod(path, mode)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


@dataclass
class Credential:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = tr("Nuova credenziale")
    username: str = ""
    domain: str = ""
    password: str = ""
    notes: str = ""
    # Raggruppamento libero scelto dall'utente, di solito il cliente. Vuoto = "Senza gruppo".
    # Le versioni precedenti non lo conoscono: lo ignorano in lettura.
    group: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "Credential":
        return cls(
            id=str(data.get("id", uuid.uuid4().hex)),
            name=str(data.get("name", tr("Credenziale"))),
            username=str(data.get("username", "")),
            domain=str(data.get("domain", "")),
            password=str(data.get("password", "")),
            notes=str(data.get("notes", "")),
            group=str(data.get("group", "")).strip(),
        )

    def to_dict(self) -> Dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "username": self.username,
            "domain": self.domain,
            "password": self.password,
            "notes": self.notes,
            "group": self.group,
        }

    def account_text(self) -> str:
        """Utente come lo scrive Windows (DOMINIO\\utente) o semplice, per elenchi e riepiloghi."""
        if self.domain and self.username:
            return f"{self.domain}\\{self.username}"
        return self.username or self.domain

    def sort_key(self) -> Tuple[int, str, str]:
        # I gruppi in ordine alfabetico, "Senza gruppo" per ultimo; dentro il gruppo per nome.
        return (0 if self.group else 1, self.group.casefold(), self.name.casefold())


PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
PASSWORD_SYMBOLS = "!#$%&*+-=?@_"


def generate_password(length: int = 20, symbols: bool = True) -> str:
    """Password casuale senza caratteri che si confondono (0/O, 1/l/I).

    Contiene sempre almeno una maiuscola, una minuscola, una cifra e, se richiesto, un simbolo:
    molti sistemi (Active Directory compreso) rifiutano password che non li hanno.
    """
    length = max(8, min(int(length), 128))
    pools = [
        "ABCDEFGHJKLMNPQRSTUVWXYZ",
        "abcdefghijkmnopqrstuvwxyz",
        "23456789",
    ] + ([PASSWORD_SYMBOLS] if symbols else [])
    alphabet = PASSWORD_ALPHABET + (PASSWORD_SYMBOLS if symbols else "")
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if all(any(ch in pool for ch in candidate) for pool in pools):
            return candidate


def password_strength(password: str) -> Tuple[int, str]:
    """Stima semplice della robustezza: (0-4, descrizione).

    Non sostituisce zxcvbn, ma riconosce i casi che contano per l'utente medio: password corte,
    di un solo tipo di carattere o fatte di un carattere ripetuto.
    """
    if not password:
        return 0, ""
    kinds = sum(
        1
        for test in (str.islower, str.isupper, str.isdigit)
        if any(test(ch) for ch in password)
    ) + (1 if any(not ch.isalnum() for ch in password) else 0)
    length = len(password)
    unique = len(set(password))
    if length < 8 or unique <= 2:
        score = 0
    elif length < 10 or kinds <= 1:
        score = 1
    elif length < 12 or kinds == 2:
        score = 2
    elif length < 16 or kinds == 3:
        score = 3
    else:
        score = 4
    return score, (tr("Molto debole"), tr("Debole"), tr("Discreta"), tr("Buona"), tr("Ottima"))[score]


class PasswordVault:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.credentials: List[Credential] = []
        self.unlocked = False
        self._key: Optional[bytes] = None
        self._salt: Optional[bytes] = None
        self._iterations = KDF_ITERATIONS

    def exists(self) -> bool:
        return self.path.exists()

    @staticmethod
    def derive_key(password: str, salt: bytes, iterations: int) -> bytes:
        if not CRYPTO_AVAILABLE or PBKDF2HMAC is None or hashes is None:
            raise RuntimeError(tr("python3-cryptography non disponibile"))
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=iterations,
        )
        return kdf.derive(password.encode("utf-8"))

    def create(self, master_password: str) -> None:
        if not master_password:
            raise ValueError(tr("Master password vuota non consentita."))
        self._salt = secrets.token_bytes(16)
        self._iterations = KDF_ITERATIONS
        self._key = self.derive_key(master_password, self._salt, self._iterations)
        self.credentials = []
        self.unlocked = True
        self.save()

    def unlock(self, master_password: str) -> None:
        if not self.path.exists():
            raise FileNotFoundError(tr("Password Manager non ancora creato."))
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        kdf = raw.get("kdf", {})
        cipher = raw.get("cipher", {})
        salt = b64d(str(kdf.get("salt", "")))
        iterations = int(kdf.get("iterations", KDF_ITERATIONS))
        nonce = b64d(str(cipher.get("nonce", "")))
        payload = b64d(str(raw.get("payload", "")))
        key = self.derive_key(master_password, salt, iterations)
        aes = AESGCM(key)
        plain = aes.decrypt(nonce, payload, None)
        data = json.loads(plain.decode("utf-8"))
        credentials = data.get("credentials", [])
        if not isinstance(credentials, list):
            raise ValueError(tr("Archivio delle password non valido."))
        self.credentials = [Credential.from_dict(x) for x in credentials if isinstance(x, dict)]
        self._salt = salt
        self._iterations = iterations
        self._key = key
        self.unlocked = True

    def lock(self) -> None:
        self.credentials = []
        self._key = None
        self.unlocked = False

    def save(self) -> None:
        if not self.unlocked or self._key is None or self._salt is None:
            raise RuntimeError(tr("Password Manager bloccato."))
        ensure_private_config_dir()
        plain = json.dumps(
            {
                "version": APP_VERSION,
                "credentials": [cred.to_dict() for cred in self.credentials],
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        nonce = secrets.token_bytes(12)
        aes = AESGCM(self._key)
        encrypted = aes.encrypt(nonce, plain, None)
        data = {
            "version": APP_VERSION,
            "format": "FedTermHub encrypted credential vault",
            "kdf": {
                "name": "PBKDF2-HMAC-SHA256",
                "iterations": self._iterations,
                "salt": b64e(self._salt),
            },
            "cipher": {
                "name": "AES-256-GCM",
                "nonce": b64e(nonce),
            },
            "payload": b64e(encrypted),
        }
        atomic_write_text(self.path, json.dumps(data, indent=2, ensure_ascii=False), mode=0o600)

    def get(self, credential_id: str) -> Optional[Credential]:
        for cred in self.credentials:
            if cred.id == credential_id:
                return cred
        return None

    def sorted_credentials(self) -> List[Credential]:
        return sorted(self.credentials, key=Credential.sort_key)

    def groups(self) -> List[str]:
        return sorted({cred.group for cred in self.credentials if cred.group}, key=str.casefold)

    def labels(self) -> List[Tuple[str, str]]:
        # Nelle tendine delle sessioni: stesso ordine della finestra Password, con il gruppo
        # accanto al nome perche' credenziali di clienti diversi hanno spesso lo stesso nome.
        return [
            (cred.id, f"{cred.name}  ·  {cred.group}" if cred.group else cred.name)
            for cred in self.sorted_credentials()
        ]

    def verify_master_password(self, master_password: str) -> bool:
        """Controlla la master password sul file su disco, senza toccare lo stato in memoria."""
        probe = PasswordVault(self.path)
        try:
            probe.unlock(master_password)
            return True
        except InvalidTag:
            return False
        finally:
            probe.lock()

    def change_master_password(self, new_password: str) -> None:
        """Ricifra l'archivio con una nuova master password (salt nuovo, chiave nuova)."""
        if not self.unlocked:
            raise RuntimeError(tr("Password Manager bloccato."))
        if not new_password:
            raise ValueError(tr("Master password vuota non consentita."))
        salt = secrets.token_bytes(16)
        key = self.derive_key(new_password, salt, KDF_ITERATIONS)
        previous = (self._salt, self._iterations, self._key)
        self._salt, self._iterations, self._key = salt, KDF_ITERATIONS, key
        try:
            self.save()
        except Exception:
            self._salt, self._iterations, self._key = previous
            raise

    @staticmethod
    def encrypt_credentials_payload(credentials: List[Credential], master_password: str) -> Dict[str, object]:
        if not master_password:
            raise ValueError(tr("Master password vuota non consentita."))
        salt = secrets.token_bytes(16)
        iterations = KDF_ITERATIONS
        key = PasswordVault.derive_key(master_password, salt, iterations)
        nonce = secrets.token_bytes(12)
        payload = json.dumps(
            {
                "version": APP_VERSION,
                "credentials": [cred.to_dict() for cred in credentials],
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        encrypted = AESGCM(key).encrypt(nonce, payload, None)
        return {
            "version": APP_VERSION,
            "format": "FedTermHub encrypted credential export",
            "warning": tr("Contiene credenziali cifrate. Serve la master password scelta in esportazione."),
            "kdf": {"name": "PBKDF2-HMAC-SHA256", "iterations": iterations, "salt": b64e(salt)},
            "cipher": {"name": "AES-256-GCM", "nonce": b64e(nonce)},
            "payload": b64e(encrypted),
        }

    @staticmethod
    def decrypt_credentials_payload(raw: Dict[str, object], master_password: str) -> List[Credential]:
        if str(raw.get("format", "")) not in {"FedTermHub encrypted credential export", "FedTermHub encrypted credential vault"}:
            raise ValueError(tr("Formato export credenziali non riconosciuto."))
        kdf = raw.get("kdf", {})
        cipher = raw.get("cipher", {})
        if not isinstance(kdf, dict) or not isinstance(cipher, dict):
            raise ValueError(tr("File cifrato non valido."))
        salt = b64d(str(kdf.get("salt", "")))
        iterations = int(kdf.get("iterations", KDF_ITERATIONS))
        nonce = b64d(str(cipher.get("nonce", "")))
        payload = b64d(str(raw.get("payload", "")))
        key = PasswordVault.derive_key(master_password, salt, iterations)
        plain = AESGCM(key).decrypt(nonce, payload, None)
        data = json.loads(plain.decode("utf-8"))
        credentials = data.get("credentials", [])
        if not isinstance(credentials, list):
            raise ValueError(tr("Payload credenziali non valido."))
        return [Credential.from_dict(x) for x in credentials if isinstance(x, dict)]


@dataclass
class AppSettings:
    """Preferenze persistenti dell'applicazione.

    Le impostazioni sono volutamente conservative: valori non validi o file di
    versioni precedenti vengono migrati senza impedire l'avvio del programma.
    """

    theme: str = "dark"  # dark oppure light
    icon_theme: str = "auto"  # auto, Papirus-Dark, Papirus, Adwaita, hicolor
    language: str = "auto"  # auto (lingua del sistema), it, en: vale dal riavvio

    # Layout
    sidebar_width: int = 238
    window_width: int = 1280
    window_height: int = 800
    toolbar_icon_size: int = 22
    show_toolbar_labels: bool = True
    show_left_rail: bool = True
    show_sidebar: bool = True
    show_menubar: bool = True
    show_ribbon: bool = True
    show_statusbar: bool = True
    show_workspace_hint: bool = True
    compact_tabs: bool = True
    animations: bool = True
    startup_mode: str = "terminal"  # terminal oppure empty

    # Terminale
    terminal_font_family: str = "Monospace"
    terminal_font_size: int = 10
    terminal_scrollback_lines: int = 20000
    terminal_cursor_shape: str = "block"  # block, ibeam, underline
    terminal_cursor_blink: bool = True
    terminal_copy_on_select: bool = False
    terminal_scroll_on_output: bool = False
    terminal_audible_bell: bool = False
    terminal_shell: str = ""  # vuoto = shell utente
    terminal_start_directory: str = ""  # vuoto = home

    # Connessioni
    show_error_popups: bool = True
    open_error_tabs: bool = True
    notification_duration_ms: int = 3500
    rdp_preflight: bool = True
    connection_timeout_seconds: int = 12
    # L'albero dei file remoti segue il cd della shell (via OSC 7).
    follow_remote_directory: bool = True
    ssh_keepalive_interval: int = 30
    ssh_keepalive_count: int = 3
    ssh_compression: bool = False
    ssh_strict_host_key: str = "ask"  # ask, accept-new, yes, no

    # Sicurezza
    vault_auto_lock_minutes: int = 15  # 0 = mai
    lock_vault_on_minimize: bool = False
    clear_clipboard_seconds: int = 30  # 0 = mai
    confirm_close_active_sessions: bool = True

    # Avviso delle nuove versioni (versione Windows: su Linux arrivano con il gestore dei pacchetti)
    check_updates: bool = True

    ui_layout_version: int = 3

    @staticmethod
    def _clamp(value: object, minimum: int, maximum: int, fallback: int) -> int:
        try:
            return max(minimum, min(int(value), maximum))
        except Exception:
            return fallback

    @classmethod
    def load(cls, path: Path) -> "AppSettings":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            theme = str(raw.get("theme", "dark")).lower()
            if theme not in {"dark", "light"}:
                theme = "dark"
            icon_theme = str(raw.get("icon_theme", "auto"))
            allowed_icons = {"auto", "Papirus-Dark", "Papirus", "Papirus-Light", "Adwaita", "hicolor"}
            if icon_theme not in allowed_icons:
                icon_theme = "auto"
            language = str(raw.get("language", "auto")).lower()
            if language not in {"auto", *I18N_LANGUAGES}:
                language = "auto"

            layout_version = cls._clamp(raw.get("ui_layout_version", 1), 1, 99, 1)
            sidebar_width = cls._clamp(raw.get("sidebar_width", 238), 190, 520, 238)
            if layout_version < 2 and sidebar_width > 270:
                sidebar_width = 238

            startup_mode = str(raw.get("startup_mode", "terminal"))
            if startup_mode not in {"terminal", "empty"}:
                startup_mode = "terminal"
            cursor_shape = str(raw.get("terminal_cursor_shape", "block"))
            if cursor_shape not in {"block", "ibeam", "underline"}:
                cursor_shape = "block"
            strict_host_key = str(raw.get("ssh_strict_host_key", "ask"))
            if strict_host_key not in {"ask", "accept-new", "yes", "no"}:
                strict_host_key = "ask"

            return cls(
                theme=theme,
                icon_theme=icon_theme,
                language=language,
                sidebar_width=sidebar_width,
                window_width=cls._clamp(raw.get("window_width", 1280), 960, 3000, 1280),
                window_height=cls._clamp(raw.get("window_height", 800), 640, 2000, 800),
                toolbar_icon_size=cls._clamp(raw.get("toolbar_icon_size", 22), 18, 32, 22),
                show_toolbar_labels=bool(raw.get("show_toolbar_labels", True)),
                show_left_rail=bool(raw.get("show_left_rail", True)),
                show_sidebar=bool(raw.get("show_sidebar", True)),
                show_menubar=bool(raw.get("show_menubar", True)),
                show_ribbon=bool(raw.get("show_ribbon", True)),
                show_statusbar=bool(raw.get("show_statusbar", True)),
                show_workspace_hint=bool(raw.get("show_workspace_hint", True)),
                compact_tabs=bool(raw.get("compact_tabs", True)),
                animations=bool(raw.get("animations", True)),
                startup_mode=startup_mode,
                terminal_font_family=str(raw.get("terminal_font_family", "Monospace")) or "Monospace",
                terminal_font_size=cls._clamp(raw.get("terminal_font_size", 10), 7, 28, 10),
                terminal_scrollback_lines=cls._clamp(raw.get("terminal_scrollback_lines", 20000), 1000, 500000, 20000),
                terminal_cursor_shape=cursor_shape,
                terminal_cursor_blink=bool(raw.get("terminal_cursor_blink", True)),
                terminal_copy_on_select=bool(raw.get("terminal_copy_on_select", False)),
                terminal_scroll_on_output=bool(raw.get("terminal_scroll_on_output", False)),
                terminal_audible_bell=bool(raw.get("terminal_audible_bell", False)),
                terminal_shell=str(raw.get("terminal_shell", "")),
                terminal_start_directory=str(raw.get("terminal_start_directory", "")),
                show_error_popups=bool(raw.get("show_error_popups", True)),
                open_error_tabs=bool(raw.get("open_error_tabs", True)),
                notification_duration_ms=cls._clamp(raw.get("notification_duration_ms", 3500), 1000, 15000, 3500),
                rdp_preflight=bool(raw.get("rdp_preflight", True)),
                connection_timeout_seconds=cls._clamp(raw.get("connection_timeout_seconds", 12), 2, 120, 12),
                follow_remote_directory=bool(raw.get("follow_remote_directory", True)),
                ssh_keepalive_interval=cls._clamp(raw.get("ssh_keepalive_interval", 30), 0, 600, 30),
                ssh_keepalive_count=cls._clamp(raw.get("ssh_keepalive_count", 3), 1, 20, 3),
                ssh_compression=bool(raw.get("ssh_compression", False)),
                ssh_strict_host_key=strict_host_key,
                vault_auto_lock_minutes=cls._clamp(raw.get("vault_auto_lock_minutes", 15), 0, 240, 15),
                lock_vault_on_minimize=bool(raw.get("lock_vault_on_minimize", False)),
                clear_clipboard_seconds=cls._clamp(raw.get("clear_clipboard_seconds", 30), 0, 600, 30),
                confirm_close_active_sessions=bool(raw.get("confirm_close_active_sessions", True)),
                check_updates=bool(raw.get("check_updates", True)),
                ui_layout_version=3,
            )
        except Exception:
            return cls()

    def to_dict(self) -> Dict[str, object]:
        return {
            "version": APP_VERSION,
            "theme": self.theme,
            "icon_theme": self.icon_theme,
            "language": self.language,
            "sidebar_width": self.sidebar_width,
            "window_width": self.window_width,
            "window_height": self.window_height,
            "toolbar_icon_size": self.toolbar_icon_size,
            "show_toolbar_labels": self.show_toolbar_labels,
            "show_left_rail": self.show_left_rail,
            "show_sidebar": self.show_sidebar,
            "show_menubar": self.show_menubar,
            "show_ribbon": self.show_ribbon,
            "show_statusbar": self.show_statusbar,
            "show_workspace_hint": self.show_workspace_hint,
            "compact_tabs": self.compact_tabs,
            "animations": self.animations,
            "startup_mode": self.startup_mode,
            "terminal_font_family": self.terminal_font_family,
            "terminal_font_size": self.terminal_font_size,
            "terminal_scrollback_lines": self.terminal_scrollback_lines,
            "terminal_cursor_shape": self.terminal_cursor_shape,
            "terminal_cursor_blink": self.terminal_cursor_blink,
            "terminal_copy_on_select": self.terminal_copy_on_select,
            "terminal_scroll_on_output": self.terminal_scroll_on_output,
            "terminal_audible_bell": self.terminal_audible_bell,
            "terminal_shell": self.terminal_shell,
            "terminal_start_directory": self.terminal_start_directory,
            "show_error_popups": self.show_error_popups,
            "open_error_tabs": self.open_error_tabs,
            "notification_duration_ms": self.notification_duration_ms,
            "rdp_preflight": self.rdp_preflight,
            "connection_timeout_seconds": self.connection_timeout_seconds,
            "follow_remote_directory": self.follow_remote_directory,
            "ssh_keepalive_interval": self.ssh_keepalive_interval,
            "ssh_keepalive_count": self.ssh_keepalive_count,
            "ssh_compression": self.ssh_compression,
            "ssh_strict_host_key": self.ssh_strict_host_key,
            "vault_auto_lock_minutes": self.vault_auto_lock_minutes,
            "lock_vault_on_minimize": self.lock_vault_on_minimize,
            "clear_clipboard_seconds": self.clear_clipboard_seconds,
            "confirm_close_active_sessions": self.confirm_close_active_sessions,
            "check_updates": self.check_updates,
            "ui_layout_version": 3,
        }

    def save(self, path: Path) -> None:
        ensure_private_config_dir()
        atomic_write_text(path, json.dumps(self.to_dict(), indent=2, ensure_ascii=False), mode=0o600)

def detect_serial_ports() -> List[str]:
    """Rileva porte seriali tipiche su Fedora/Linux senza dipendere dalla GUI."""
    patterns = [
        "/dev/ttyUSB*",
        "/dev/ttyACM*",
        "/dev/ttyS*",
        "/dev/serial/by-id/*",
    ]
    ports: List[str] = []
    for pattern in patterns:
        for item in glob.glob(pattern):
            if item not in ports:
                ports.append(item)
    return sorted(ports)


@dataclass
class Session:
    name: str
    protocol: str = "ssh"  # local, ssh, rdp, vnc, sftp, command, serial
    host: str = ""
    port: str = ""
    user: str = ""
    domain: str = ""
    command: str = ""
    notes: str = ""
    credential_id: str = ""
    use_credential_password: bool = True
    serial_databits: str = "8"
    serial_parity: str = "N"
    serial_stopbits: str = "1"
    rdp_mode: str = "native"  # native oppure legacy
    rdp_clipboard: bool = True
    rdp_dynamic_resolution: bool = True
    rdp_fonts: bool = True
    rdp_auto_reconnect: bool = True
    rdp_cert_ignore: bool = True
    rdp_sound: bool = False
    rdp_multimon: bool = False
    rdp_start_fullscreen: bool = False
    rdp_monitor: str = "current"  # current, primary oppure selector monitor:<...>
    rdp_window_mode: str = "embedded"  # embedded oppure detached
    rdp_admin: bool = False
    rdp_drive: str = ""
    rdp_width: str = ""
    rdp_height: str = ""
    rdp_quality: str = "lan"  # lan, wan, low
    rdp_grab_keyboard: bool = True
    rdp_grab_mouse: bool = True
    rdp_keyboard_layout: str = "auto"  # auto, it, us, de, fr, es oppure layout FreeRDP
    rdp_smart_sizing: bool = True
    rdp_microphone: bool = False
    rdp_printer: bool = False
    rdp_smartcard: bool = False
    rdp_gfx: bool = False
    rdp_compression: bool = True
    rdp_scale: str = "100"
    rdp_timeout: str = "12000"
    rdp_security: str = "auto"  # auto, nla, tls, rdp
    rdp_password_stdin: bool = True
    # Instradamento contestuale: diretto, tunnel salvato, jump host o RD Gateway.
    routing_mode: str = "direct"  # direct, saved_tunnel, proxy_jump, rd_gateway
    required_tunnel_id: str = ""
    wait_for_tunnel: bool = True
    jump_host: str = ""
    jump_port: str = "22"
    jump_user: str = ""
    rdp_gateway_host: str = ""
    rdp_gateway_port: str = "443"
    rdp_gateway_user: str = ""
    rdp_gateway_domain: str = ""
    rdp_gateway_credential_id: str = ""
    rdp_gateway_use_same_credentials: bool = True
    rdp_gateway_transport: str = "auto"  # auto, rpc, http

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "Session":
        return cls(
            name=str(data.get("name", tr("Nuova sessione"))),
            protocol=str(data.get("protocol", "ssh")).lower(),
            host=str(data.get("host", "")),
            port=str(data.get("port", "")),
            user=str(data.get("user", "")),
            domain=str(data.get("domain", "")),
            command=str(data.get("command", "")),
            notes=str(data.get("notes", "")),
            credential_id=str(data.get("credential_id", "")),
            use_credential_password=bool(data.get("use_credential_password", True)),
            serial_databits=str(data.get("serial_databits", "8")),
            serial_parity=str(data.get("serial_parity", "N")).upper() or "N",
            serial_stopbits=str(data.get("serial_stopbits", "1")),
            rdp_mode=("legacy" if str(data.get("rdp_mode", "native")) in {"legacy", "external-legacy"} else "native"),
            rdp_clipboard=bool(data.get("rdp_clipboard", True)),
            rdp_dynamic_resolution=bool(data.get("rdp_dynamic_resolution", True)),
            rdp_fonts=bool(data.get("rdp_fonts", True)),
            rdp_auto_reconnect=bool(data.get("rdp_auto_reconnect", True)),
            rdp_cert_ignore=bool(data.get("rdp_cert_ignore", True)),
            rdp_sound=bool(data.get("rdp_sound", False)),
            rdp_multimon=bool(data.get("rdp_multimon", False)),
            rdp_start_fullscreen=bool(data.get("rdp_start_fullscreen", False)),
            rdp_monitor=str(data.get("rdp_monitor", "current")) or "current",
            rdp_window_mode=(
                "detached" if str(data.get("rdp_window_mode", "embedded")) == "detached" else "embedded"
            ),
            rdp_admin=bool(data.get("rdp_admin", False)),
            rdp_drive=str(data.get("rdp_drive", "")),
            rdp_width=str(data.get("rdp_width", "")),
            rdp_height=str(data.get("rdp_height", "")),
            rdp_quality=str(data.get("rdp_quality", "lan")),
            rdp_grab_keyboard=bool(data.get("rdp_grab_keyboard", True)),
            rdp_grab_mouse=bool(data.get("rdp_grab_mouse", True)),
            rdp_keyboard_layout=str(data.get("rdp_keyboard_layout", "auto")),
            rdp_smart_sizing=bool(data.get("rdp_smart_sizing", True)),
            rdp_microphone=bool(data.get("rdp_microphone", False)),
            rdp_printer=bool(data.get("rdp_printer", False)),
            rdp_smartcard=bool(data.get("rdp_smartcard", False)),
            rdp_gfx=bool(data.get("rdp_gfx", False)),
            rdp_compression=bool(data.get("rdp_compression", True)),
            rdp_scale=str(data.get("rdp_scale", "100")),
            rdp_timeout=str(data.get("rdp_timeout", "12000")),
            rdp_security=str(data.get("rdp_security", "auto")),
            rdp_password_stdin=bool(data.get("rdp_password_stdin", True)),
            routing_mode=(
                str(data.get("routing_mode", "")).strip()
                if str(data.get("routing_mode", "")).strip() in {"direct", "saved_tunnel", "proxy_jump", "rd_gateway"}
                else ("saved_tunnel" if str(data.get("required_tunnel_id", "")) else "direct")
            ),
            required_tunnel_id=str(data.get("required_tunnel_id", "")),
            wait_for_tunnel=bool(data.get("wait_for_tunnel", True)),
            jump_host=str(data.get("jump_host", "")),
            jump_port=str(data.get("jump_port", "22")) or "22",
            jump_user=str(data.get("jump_user", "")),
            rdp_gateway_host=str(data.get("rdp_gateway_host", "")),
            rdp_gateway_port=str(data.get("rdp_gateway_port", "443")) or "443",
            rdp_gateway_user=str(data.get("rdp_gateway_user", "")),
            rdp_gateway_domain=str(data.get("rdp_gateway_domain", "")),
            rdp_gateway_credential_id=str(data.get("rdp_gateway_credential_id", "")),
            rdp_gateway_use_same_credentials=bool(data.get("rdp_gateway_use_same_credentials", True)),
            rdp_gateway_transport=str(data.get("rdp_gateway_transport", "auto")) or "auto",
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "type": "session",
            "name": self.name,
            "protocol": self.protocol,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "domain": self.domain,
            "command": self.command,
            "notes": self.notes,
            "credential_id": self.credential_id,
            "use_credential_password": self.use_credential_password,
            "serial_databits": self.serial_databits,
            "serial_parity": self.serial_parity,
            "serial_stopbits": self.serial_stopbits,
            "rdp_mode": self.rdp_mode,
            "rdp_clipboard": self.rdp_clipboard,
            "rdp_dynamic_resolution": self.rdp_dynamic_resolution,
            "rdp_fonts": self.rdp_fonts,
            "rdp_auto_reconnect": self.rdp_auto_reconnect,
            "rdp_cert_ignore": self.rdp_cert_ignore,
            "rdp_sound": self.rdp_sound,
            "rdp_multimon": self.rdp_multimon,
            "rdp_start_fullscreen": self.rdp_start_fullscreen,
            "rdp_monitor": self.rdp_monitor,
            "rdp_window_mode": self.rdp_window_mode,
            "rdp_admin": self.rdp_admin,
            "rdp_drive": self.rdp_drive,
            "rdp_width": self.rdp_width,
            "rdp_height": self.rdp_height,
            "rdp_quality": self.rdp_quality,
            "rdp_grab_keyboard": self.rdp_grab_keyboard,
            "rdp_grab_mouse": self.rdp_grab_mouse,
            "rdp_keyboard_layout": self.rdp_keyboard_layout,
            "rdp_smart_sizing": self.rdp_smart_sizing,
            "rdp_microphone": self.rdp_microphone,
            "rdp_printer": self.rdp_printer,
            "rdp_smartcard": self.rdp_smartcard,
            "rdp_gfx": self.rdp_gfx,
            "rdp_compression": self.rdp_compression,
            "rdp_scale": self.rdp_scale,
            "rdp_timeout": self.rdp_timeout,
            "rdp_security": self.rdp_security,
            "rdp_password_stdin": self.rdp_password_stdin,
            "routing_mode": self.routing_mode,
            "required_tunnel_id": self.required_tunnel_id,
            "wait_for_tunnel": self.wait_for_tunnel,
            "jump_host": self.jump_host,
            "jump_port": self.jump_port,
            "jump_user": self.jump_user,
            "rdp_gateway_host": self.rdp_gateway_host,
            "rdp_gateway_port": self.rdp_gateway_port,
            "rdp_gateway_user": self.rdp_gateway_user,
            "rdp_gateway_domain": self.rdp_gateway_domain,
            "rdp_gateway_credential_id": self.rdp_gateway_credential_id,
            "rdp_gateway_use_same_credentials": self.rdp_gateway_use_same_credentials,
            "rdp_gateway_transport": self.rdp_gateway_transport,
        }


def _port_problem(value: str, label: str = "", allow_zero: bool = False) -> str:
    label = label or tr("porta")
    value = (value or "").strip()
    if not value:
        return ""
    if not value.isdigit() or not ((0 if allow_zero else 1) <= int(value) <= 65535):
        return tr("{label} «{value}» non valida: serve un numero da 1 a 65535").format(label=label, value=value)
    return ""


def session_problem(sess: "Session") -> str:
    """Il primo problema che impedisce di usare la sessione, oppure "".

    Usato al salvataggio (la finestra resta aperta e lo dice), dalla connessione rapida e prima di
    connettersi: un file di sessioni importato puo' contenere valori che nessuna finestra ha
    controllato.
    """
    proto = (sess.protocol or "").lower()
    host = (sess.host or "").strip()
    if proto in {"ssh", "sftp", "rdp", "vnc", "telnet"}:
        if not host:
            return tr("manca l'host (nome o indirizzo del server)")
        if host.startswith("-") or any(ch.isspace() for ch in host):
            return tr("host «{host}» non valido").format(host=host)
        if (sess.user or "").strip().startswith("-"):
            return tr("utente «{user}» non valido").format(user=sess.user)
        problem = _port_problem(sess.port, tr("display/porta") if proto == "vnc" else tr("porta"), allow_zero=proto == "vnc")
        if problem:
            return problem
    if proto in {"ssh", "sftp"} and getattr(sess, "routing_mode", "direct") == "proxy_jump":
        jump = (getattr(sess, "jump_host", "") or "").strip()
        if jump.startswith("-") or any(ch.isspace() for ch in jump):
            return tr("jump host «{jump}» non valido").format(jump=jump)
        problem = _port_problem(getattr(sess, "jump_port", ""), tr("porta del jump host"))
        if problem:
            return problem
    if proto == "serial":
        if not host:
            return tr("manca la porta seriale (per esempio /dev/ttyUSB0)")
        if not (sess.port or "").strip().isdigit():
            return tr("velocità «{port}» non valida: serve un numero, per esempio 115200").format(port=sess.port)
    if proto == "command" and not (sess.command or "").strip():
        return tr("manca il comando da eseguire")
    return ""


HOME_SESSION_NAME = "Home Session"
HOME_SESSION_COLOR = "#4aa3df"
DEFAULT_FOLDER_COLOR = "#f2c94c"
FOLDER_COLOR_PRESETS: List[Tuple[str, str]] = [
    (tr("Giallo"), "#f2c94c"),
    (tr("Blu"), "#4aa3df"),
    (tr("Azzurro"), "#56c7d9"),
    (tr("Verde"), "#53b175"),
    (tr("Arancione"), "#f2994a"),
    (tr("Rosso"), "#eb5757"),
    (tr("Viola"), "#9b73e6"),
    (tr("Rosa"), "#e77fb3"),
    (tr("Grigio"), "#8b97a6"),
]


def normalize_folder_color(value: object) -> str:
    text = str(value or "").strip()
    if len(text) == 7 and text.startswith("#"):
        try:
            int(text[1:], 16)
            return text.lower()
        except ValueError:
            pass
    return DEFAULT_FOLDER_COLOR


@dataclass
class FolderNode:
    name: str
    children: List["Node"] = field(default_factory=list)
    color: str = DEFAULT_FOLDER_COLOR

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "FolderNode":
        raw_children = data.get("children", [])
        children: List[Node] = []
        if isinstance(raw_children, list):
            children = [node_from_dict(x) for x in raw_children if isinstance(x, dict)]
        return cls(
            name=str(data.get("name", tr("Cartella"))),
            children=children,
            color=normalize_folder_color(data.get("color", DEFAULT_FOLDER_COLOR)),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "type": "folder",
            "name": self.name,
            "color": normalize_folder_color(self.color),
            "children": [node_to_dict(child) for child in self.children],
        }


Node = Union[Session, FolderNode]


def node_from_dict(data: Dict[str, object]) -> Node:
    kind = str(data.get("type", "")).lower()
    if kind == "folder" or "children" in data:
        return FolderNode.from_dict(data)
    return Session.from_dict(data)


def node_to_dict(node: Node) -> Dict[str, object]:
    if isinstance(node, FolderNode):
        return node.to_dict()
    return node.to_dict()


SESSION_PACKAGE_FORMAT = "FedTermHub session package"


def session_package(node: Node, scope: str) -> Dict[str, object]:
    """Il contenuto di un file di sessioni esportato (stesso formato su Linux e Windows)."""
    return {"version": APP_VERSION, "format": SESSION_PACKAGE_FORMAT, "scope": scope, "root": node_to_dict(node)}


def node_from_session_package(raw: object, stem: str) -> Node:
    """La cartella o la sessione di un file esportato; accetta anche i formati delle versioni 0.x."""
    if isinstance(raw, dict) and isinstance(raw.get("root"), dict):
        return node_from_dict(raw["root"])  # type: ignore[arg-type]
    items = raw.get("items", raw) if isinstance(raw, dict) else raw
    if isinstance(items, list):
        imported = [node_from_dict(x) for x in items if isinstance(x, dict)]
        return FolderNode(name=tr("Import {stem}").format(stem=stem), children=imported)
    raise ValueError(tr("Il file non contiene una Home Session, cartella o sessione valida."))


def make_default_tree() -> List[Node]:
    base_children: List[Node] = [
        FolderNode(
            name=tr("Terminali"),
            children=[Session(name=tr("Terminale locale"), protocol="local")],
        ),
        FolderNode(
            name=tr("SSH / Linux"),
            children=[
                Session(
                    name=tr("Esempio SSH"),
                    protocol="ssh",
                    host="server.local",
                    port="22",
                    user=os.getenv("USER", ""),
                )
            ],
        ),
        FolderNode(
            name=tr("RDP / Windows"),
            children=[
                Session(
                    name=tr("Esempio RDP dominio"),
                    protocol="rdp",
                    host="192.168.1.10",
                    port="3389",
                    user=tr("utente"),
                    domain=tr("DOMINIO"),
                )
            ],
        ),
        FolderNode(
            name=tr("Seriale / Console"),
            children=[
                Session(
                    name=tr("Console seriale USB 115200"),
                    protocol="serial",
                    host="/dev/ttyUSB0",
                    port="115200",
                    serial_databits="8",
                    serial_parity="N",
                    serial_stopbits="1",
                )
            ],
        ),
    ]
    return [FolderNode(name=HOME_SESSION_NAME, color=HOME_SESSION_COLOR, children=base_children)]


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.nodes: List[Node] = []
        # Messaggio per l'utente quando il file non era leggibile: lo mostra la finestra principale.
        self.load_warning = ""
        self.load()

    def ensure_home_session_root(self) -> bool:
        if len(self.nodes) == 1 and isinstance(self.nodes[0], FolderNode) and self.nodes[0].name == HOME_SESSION_NAME:
            self.nodes[0].color = HOME_SESSION_COLOR
            return False
        home_nodes = [n for n in self.nodes if isinstance(n, FolderNode) and n.name == HOME_SESSION_NAME]
        if home_nodes:
            home = home_nodes[0]
            rest = [n for n in self.nodes if n is not home]
            home.children.extend(rest)
            home.color = HOME_SESSION_COLOR
            self.nodes = [home]
        else:
            self.nodes = [FolderNode(name=HOME_SESSION_NAME, color=HOME_SESSION_COLOR, children=self.nodes)]
        return True

    def home_path(self) -> str:
        return "0"

    def is_home_path(self, path: str) -> bool:
        """True solo per la radice logica e protetta Home Session."""
        return path == self.home_path()

    def home_node(self) -> FolderNode:
        if not self.nodes:
            self.nodes = make_default_tree()
        if not (len(self.nodes) == 1 and isinstance(self.nodes[0], FolderNode) and self.nodes[0].name == HOME_SESSION_NAME):
            self.ensure_home_session_root()
        return self.nodes[0]  # type: ignore[return-value]

    def load(self) -> None:
        ensure_private_config_dir()
        if not self.path.exists():
            self.nodes = make_default_tree()
            self.save()
            return

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                items = raw.get("items", [])
            else:
                items = raw

            if not isinstance(items, list):
                raise ValueError(tr("Formato sessioni non valido: attesa una lista."))

            self.nodes = [node_from_dict(x) for x in items if isinstance(x, dict)]
            if not self.nodes:
                self.nodes = make_default_tree()
                self.save()
                return

            if isinstance(raw, list) and all(isinstance(node, Session) for node in self.nodes):
                self.nodes = [FolderNode(name=tr("Importate dalla 0.1"), children=self.nodes)]
            changed = self.ensure_home_session_root()
            if changed or isinstance(raw, list):
                self.save()
        except Exception as exc:
            # Nome con data: un secondo file rotto non deve sovrascrivere la copia del primo.
            backup = self.path.with_name(f"{self.path.stem}.illeggibile-{time.strftime('%Y%m%d-%H%M%S')}.json")
            try:
                self.path.rename(backup)
            except OSError:
                backup = self.path
            self.nodes = make_default_tree()
            self.save()
            self.load_warning = (
                tr("Il file delle sessioni non era leggibile ({exc}). Ne ho conservato una copia in {backup} e sono ripartito dalle sessioni di esempio.").format(exc=exc, backup=backup)
            )
            print(tr("File sessioni corrotto, salvato come {backup}: {exc}").format(backup=backup, exc=exc), file=sys.stderr)

    def save(self) -> None:
        ensure_private_config_dir()
        # Home Session è una radice speciale, non una cartella personalizzabile.
        # Normalizziamo nome e colore prima di ogni salvataggio per eliminare
        # eventuali proprietà ereditate da versioni precedenti.
        home = self.home_node()
        home.name = HOME_SESSION_NAME
        home.color = HOME_SESSION_COLOR
        data = {
            "version": APP_VERSION,
            "items": [node_to_dict(node) for node in self.nodes],
        }
        atomic_write_text(self.path, json.dumps(data, indent=2, ensure_ascii=False), mode=0o600)

    @staticmethod
    def _parts(path: str) -> List[int]:
        if not path:
            return []
        return [int(x) for x in path.split(".") if x != ""]

    def get_node(self, path: str) -> Node:
        parts = self._parts(path)
        if not parts:
            raise ValueError(tr("La radice non è un nodo."))
        current: Node = self.nodes[parts[0]]
        for part in parts[1:]:
            if not isinstance(current, FolderNode):
                raise ValueError(tr("Percorso non valido: nodo non cartella."))
            current = current.children[part]
        return current

    def get_container(self, folder_path: str) -> List[Node]:
        if folder_path == "":
            return self.nodes
        node = self.get_node(folder_path)
        if not isinstance(node, FolderNode):
            raise ValueError(tr("Il percorso destinazione non è una cartella."))
        return node.children

    def parent_path(self, path: str) -> str:
        parts = path.split(".")
        if len(parts) <= 1:
            return ""
        return ".".join(parts[:-1])

    def add_node(self, parent_folder_path: str, node: Node) -> None:
        self.get_container(parent_folder_path).append(node)
        self.save()

    def replace_node(self, path: str, node: Node) -> None:
        parts = self._parts(path)
        if not parts:
            raise ValueError(tr("Impossibile sostituire la radice."))
        container = self.get_container(".".join(str(x) for x in parts[:-1]))
        container[parts[-1]] = node
        self.save()

    def remove_node(self, path: str) -> Node:
        parts = self._parts(path)
        if not parts or path == self.home_path():
            raise ValueError(tr("Home Session non può essere eliminata."))
        container = self.get_container(".".join(str(x) for x in parts[:-1]))
        node = container.pop(parts[-1])
        self.save()
        return node

    def find_node_path(self, target: Node) -> str:
        """Restituisce il percorso corrente di un nodo usando l'identità Python."""
        def walk(nodes: List[Node], prefix: str) -> Optional[str]:
            for idx, node in enumerate(nodes):
                path = f"{prefix}.{idx}" if prefix else str(idx)
                if node is target:
                    return path
                if isinstance(node, FolderNode):
                    found = walk(node.children, path)
                    if found is not None:
                        return found
            return None

        found = walk(self.nodes, "")
        if found is None:
            raise ValueError(tr("Nodo non trovato dopo lo spostamento."))
        return found

    def move_node(self, source_path: str, target_folder_path: str, target_index: Optional[int] = None) -> str:
        """Sposta un nodo in modo atomico e restituisce il nuovo percorso.

        ``target_folder_path`` è la cartella di destinazione (stringa vuota per
        la radice). ``target_index`` permette anche il riordino prima/dopo una
        sessione; ``None`` inserisce in fondo.
        """
        if not source_path or source_path == self.home_path():
            raise ValueError(tr("Home Session non può essere spostata."))
        source_node = self.get_node(source_path)
        if isinstance(source_node, FolderNode) and (
            target_folder_path == source_path or target_folder_path.startswith(source_path + ".")
        ):
            raise ValueError(tr("Una cartella non può essere spostata dentro sé stessa o una sua sottocartella."))

        source_parts = self._parts(source_path)
        source_parent_path = ".".join(str(x) for x in source_parts[:-1])
        source_index = source_parts[-1]
        source_container = self.get_container(source_parent_path)
        # Conserviamo il riferimento alla lista di destinazione prima del pop:
        # gli indici dei percorsi possono cambiare, l'oggetto lista no.
        target_container = self.get_container(target_folder_path)

        node = source_container.pop(source_index)
        if target_index is None:
            target_index = len(target_container)
        else:
            target_index = max(0, min(int(target_index), len(target_container)))
            if source_container is target_container and source_index < target_index:
                target_index -= 1
        target_container.insert(target_index, node)
        self.save()
        return self.find_node_path(node)

    def iter_folders(self) -> Iterable[Tuple[str, FolderNode, str]]:
        def walk(nodes: List[Node], prefix: str, label_prefix: str) -> Iterable[Tuple[str, FolderNode, str]]:
            for idx, node in enumerate(nodes):
                path = f"{prefix}.{idx}" if prefix else str(idx)
                if isinstance(node, FolderNode):
                    label = f"{label_prefix}{node.name}"
                    yield path, node, label
                    yield from walk(node.children, path, f"{label_prefix}{node.name} / ")

        yield from walk(self.nodes, "", "")

    def iter_sessions(self) -> Iterable[Tuple[str, "Session", str]]:
        """Tutte le sessioni con percorso e cartella di appartenenza.

        Usato dalla command palette per la ricerca rapida: restituisce anche
        il percorso della cartella (``folder``) per dare contesto.
        """
        def walk(nodes: List[Node], prefix: str, folder: str) -> Iterable[Tuple[str, "Session", str]]:
            for idx, node in enumerate(nodes):
                path = f"{prefix}.{idx}" if prefix else str(idx)
                if isinstance(node, FolderNode):
                    child_folder = f"{folder} / {node.name}" if folder else node.name
                    yield from walk(node.children, path, child_folder)
                elif isinstance(node, Session):
                    yield path, node, folder

        yield from walk(self.nodes, "", "")


@dataclass
class TunnelSession:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = tr("Nuovo tunnel")
    tunnel_type: str = "local"  # local, remote, dynamic
    ssh_host: str = ""
    ssh_port: str = "22"
    ssh_user: str = ""
    bind_address: str = "127.0.0.1"
    local_port: str = ""
    remote_host: str = "127.0.0.1"
    remote_port: str = ""
    options: str = ""
    credential_id: str = ""
    use_credential_password: bool = True
    identity_file: str = ""
    address_family: str = "auto"  # auto, ipv4, ipv6
    exit_on_forward_failure: bool = True
    compression: bool = False
    server_alive_interval: int = 30
    server_alive_count: int = 3
    auto_start: bool = False
    auto_restart: bool = True
    restart_delay: int = 5
    verbose_log: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "TunnelSession":
        def _int(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                return max(minimum, min(int(data.get(name, default)), maximum))
            except (TypeError, ValueError):
                return default

        tunnel_type = str(data.get("tunnel_type", "local")).lower()
        if tunnel_type not in {"local", "remote", "dynamic"}:
            tunnel_type = "local"
        address_family = str(data.get("address_family", "auto")).lower()
        if address_family not in {"auto", "ipv4", "ipv6"}:
            address_family = "auto"
        return cls(
            id=str(data.get("id", uuid.uuid4().hex)),
            name=str(data.get("name", tr("Tunnel SSH"))),
            tunnel_type=tunnel_type,
            ssh_host=str(data.get("ssh_host", "")),
            ssh_port=str(data.get("ssh_port", "22")),
            ssh_user=str(data.get("ssh_user", "")),
            bind_address=str(data.get("bind_address", "127.0.0.1")),
            local_port=str(data.get("local_port", "")),
            remote_host=str(data.get("remote_host", "127.0.0.1")),
            remote_port=str(data.get("remote_port", "")),
            options=str(data.get("options", "")),
            credential_id=str(data.get("credential_id", "")),
            use_credential_password=bool(data.get("use_credential_password", True)),
            identity_file=str(data.get("identity_file", "")),
            address_family=address_family,
            exit_on_forward_failure=bool(data.get("exit_on_forward_failure", True)),
            compression=bool(data.get("compression", False)),
            server_alive_interval=_int("server_alive_interval", 30, 0, 3600),
            server_alive_count=_int("server_alive_count", 3, 1, 100),
            auto_start=bool(data.get("auto_start", False)),
            auto_restart=bool(data.get("auto_restart", True)),
            restart_delay=_int("restart_delay", 5, 1, 300),
            verbose_log=bool(data.get("verbose_log", False)),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "tunnel_type": self.tunnel_type,
            "ssh_host": self.ssh_host,
            "ssh_port": self.ssh_port,
            "ssh_user": self.ssh_user,
            "bind_address": self.bind_address,
            "local_port": self.local_port,
            "remote_host": self.remote_host,
            "remote_port": self.remote_port,
            "options": self.options,
            "credential_id": self.credential_id,
            "use_credential_password": self.use_credential_password,
            "identity_file": self.identity_file,
            "address_family": self.address_family,
            "exit_on_forward_failure": self.exit_on_forward_failure,
            "compression": self.compression,
            "server_alive_interval": self.server_alive_interval,
            "server_alive_count": self.server_alive_count,
            "auto_start": self.auto_start,
            "auto_restart": self.auto_restart,
            "restart_delay": self.restart_delay,
            "verbose_log": self.verbose_log,
        }

    def type_label(self) -> str:
        return {
            "local": tr("Locale"),
            "remote": tr("Remoto"),
            "dynamic": "SOCKS",
        }.get(self.tunnel_type, self.tunnel_type.capitalize())

    def listen_endpoint(self) -> str:
        return f"{self.bind_address or '127.0.0.1'}:{self.local_port or '?'}"

    def destination_endpoint(self) -> str:
        if self.tunnel_type == "dynamic":
            return tr("Destinazione scelta dal client SOCKS")
        return f"{self.remote_host or '?'}:{self.remote_port or '?'}"

    def ssh_endpoint(self) -> str:
        prefix = f"{self.ssh_user}@" if self.ssh_user else ""
        return f"{prefix}{self.ssh_host or '?'}:{self.ssh_port or '22'}"

    def summary(self) -> str:
        if self.tunnel_type == "dynamic":
            return tr("SOCKS {listen_endpoint} tramite {ssh_endpoint}").format(listen_endpoint=self.listen_endpoint(), ssh_endpoint=self.ssh_endpoint())
        if self.tunnel_type == "remote":
            return tr("Remoto {listen_endpoint} → {destination_endpoint} tramite {ssh_endpoint}").format(listen_endpoint=self.listen_endpoint(), destination_endpoint=self.destination_endpoint(), ssh_endpoint=self.ssh_endpoint())
        return tr("Locale {listen_endpoint} → {destination_endpoint} tramite {ssh_endpoint}").format(listen_endpoint=self.listen_endpoint(), destination_endpoint=self.destination_endpoint(), ssh_endpoint=self.ssh_endpoint())


class TunnelStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.tunnels: List[TunnelSession] = []
        self.load()

    def load(self) -> None:
        ensure_private_config_dir()
        if not self.path.exists():
            self.tunnels = [
                TunnelSession(
                    name=tr("Esempio tunnel locale RDP"),
                    tunnel_type="local",
                    ssh_host="bastion.local",
                    ssh_port="22",
                    ssh_user=os.getenv("USER", ""),
                    bind_address="127.0.0.1",
                    local_port="13389",
                    remote_host="192.168.1.10",
                    remote_port="3389",
                    exit_on_forward_failure=True,
                    auto_restart=True,
                )
            ]
            self.save()
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = raw.get("items", raw) if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                raise ValueError(tr("Formato tunnel non valido"))
            self.tunnels = [TunnelSession.from_dict(x) for x in items if isinstance(x, dict)]
        except Exception as exc:
            backup = self.path.with_suffix(".broken.json")
            try:
                self.path.rename(backup)
            except OSError:
                backup = self.path
            self.tunnels = []
            self.save()
            print(tr("File tunnel corrotto, salvato come {backup}: {exc}").format(backup=backup, exc=exc), file=sys.stderr)

    def save(self) -> None:
        ensure_private_config_dir()
        data = {"version": APP_VERSION, "items": [t.to_dict() for t in self.tunnels]}
        atomic_write_text(self.path, json.dumps(data, indent=2, ensure_ascii=False), mode=0o600)

    def get(self, tunnel_id: str) -> Optional[TunnelSession]:
        for tunnel in self.tunnels:
            if tunnel.id == tunnel_id:
                return tunnel
        return None

    def upsert(self, tunnel: TunnelSession) -> None:
        for idx, item in enumerate(self.tunnels):
            if item.id == tunnel.id:
                self.tunnels[idx] = tunnel
                self.save()
                return
        self.tunnels.append(tunnel)
        self.save()

    def remove(self, tunnel_id: str) -> None:
        self.tunnels = [x for x in self.tunnels if x.id != tunnel_id]
        self.save()
