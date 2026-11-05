# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Desktop remoto nell'interfaccia Qt.

Su Windows la scheda ospita il componente RDP di Windows (lo stesso di mstsc: MsRdpClient9
NotSafeForScripting, MsTscAx.dll), incorporato con QAxWidget. Così NLA, RD Gateway, smart card, appunti,
stampanti e aggiornamenti di sicurezza sono quelli di Microsoft, senza librerie da portare con sé.

Su Linux (per ora) l'interfaccia Qt non ha un motore RDP: la versione GTK usa FreeRDP.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .. import model
from ..i18n import tr

IS_WINDOWS = sys.platform == "win32"
# CLSID della classe MsRdpClient9NotSafeForScripting (Windows 8.1 e successivi): la variante «non sicura
# per gli script» è quella che accetta la password in chiaro (ClearTextPassword) da un programma.
RDP_CLSID = "{8B918B82-7985-4C24-89DF-C33AD2BBFBCD}"

# Motivi di disconnessione più comuni (codici di IMsTscAxEvents::OnDisconnected), per un messaggio
# comprensibile; per gli altri si chiede la descrizione al componente (GetErrorDescription).
DISCONNECT_REASONS = {
    0: tr("Nessuna informazione"),
    1: tr("Disconnessione dal computer locale"),
    2: tr("Disconnessione dal server (o sessione chiusa con logoff)"),
    260: tr("Il nome del server non è stato trovato (DNS)"),
    264: tr("Il server non risponde entro il tempo previsto"),
    516: tr("Impossibile stabilire la connessione: controlla indirizzo, porta, rete e firewall"),
    772: tr("Errore di rete durante la connessione"),
    1030: tr("Errore di sicurezza: il server richiede un'autenticazione diversa"),
    2055: tr("Accesso negato: utente o password non corretti"),
    2056: tr("Il server ha rifiutato la connessione"),
    2308: tr("Connessione interrotta: la rete è caduta o il server si è riavviato"),
    2823: tr("L'account non ha il permesso di accedere con Desktop remoto"),
    3335: tr("L'account è bloccato"),
    3591: tr("L'account è disabilitato"),
    3847: tr("La password è scaduta"),
    4615: tr("La password deve essere cambiata prima del primo accesso"),
}
USER_DISCONNECT = {1, 2, 3}


def ax_available() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        from PySide6.QtAxContainer import QAxWidget  # noqa: F401
    except ImportError:
        return False
    return True


class RdpTab(QWidget):
    """Una scheda con un desktop remoto. Segnali: title_changed (testo), closed (la scheda può chiudersi)."""

    title_changed = Signal(str)
    status = Signal(str)

    def __init__(self, parent: Optional[QWidget], sess: model.Session, user: str, domain: str,
                 password: Optional[str], gateway_password: Optional[str] = None) -> None:
        super().__init__(parent)
        self.sess = sess
        self.user = user
        self.domain = domain
        self.password = password
        self.gateway_password = gateway_password
        self.ax = None
        self.connected = False
        self.events: List[str] = []  # eventi ricevuti dal componente (servono anche alla prova automatica)
        self.unset: List[str] = []  # proprietà che il componente non ha accettato (diagnostica)
        self._last_event: Optional[tuple] = None
        self._op = ""  # proprietà o metodo in corso, per capire quale fa scattare un'eccezione COM
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.bar = QWidget()
        bar = QHBoxLayout(self.bar)
        bar.setContentsMargins(8, 4, 8, 4)
        self.state = QLabel(tr("Preparazione del desktop remoto…"))
        self.state.setProperty("role", "muted")
        bar.addWidget(self.state, 1)
        self.reconnect_button = QPushButton(tr("Riconnetti"))
        self.reconnect_button.clicked.connect(self.reconnect)
        self.reconnect_button.hide()
        self.fullscreen_button = QPushButton(tr("Schermo intero"))
        self.fullscreen_button.clicked.connect(self.enter_fullscreen)
        self.external_button = QPushButton(tr("Apri con Connessione Desktop remoto"))
        self.external_button.setToolTip(tr("Apre la sessione in mstsc, la finestra di Windows, invece che nella scheda"))
        self.external_button.clicked.connect(self.open_external)
        bar.addWidget(self.reconnect_button)
        bar.addWidget(self.fullscreen_button)
        bar.addWidget(self.external_button)
        layout.addWidget(self.bar)
        self.host_area = QWidget()
        self.host_layout = QVBoxLayout(self.host_area)
        self.host_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.host_area, 1)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(400)
        self._resize_timer.timeout.connect(self._apply_resolution)
        if ax_available():
            QTimer.singleShot(0, self.connect_session)
        else:
            self.fullscreen_button.hide()
            self.external_button.setVisible(IS_WINDOWS)
            message = QLabel(
                tr("Il desktop remoto incorporato è disponibile nella versione Windows. Su Linux usa per ora "
                   "la versione GTK di FedTermHub (comando fedtermhub).") if not IS_WINDOWS else
                tr("Il componente RDP di Windows non è disponibile: apri la sessione con Connessione Desktop remoto.")
            )
            message.setWordWrap(True)
            message.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.host_layout.addWidget(message)
            self.state.setText(tr("RDP incorporato non disponibile"))

    # ------------------------------------------------------------------ connessione
    def _create_control(self) -> bool:
        from PySide6.QtAxContainer import QAxWidget

        if self.ax is not None:
            self.ax.hide()
            self.ax.deleteLater()
            self.ax = None
        ax = QAxWidget(self.host_area)
        if not ax.setControl(RDP_CLSID):
            ax.deleteLater()
            return False
        ax.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.host_layout.addWidget(ax)
        # Gli eventi COM arrivano tutti dal segnale generico signal(nome, argc, argv): così non dipendono
        # da come PySide6 espone i segnali generati dal componente.
        ax.signal.connect(self._com_event)
        # Anche i segnali con nome, se PySide6 li espone: _dispatch scarta il doppione.
        from PySide6.QtCore import SIGNAL, QObject

        for signature in ("OnConnecting()", "OnConnected()", "OnLoginComplete()", "OnDisconnected(int)",
                          "OnFatalError(int)", "OnWarning(int)", "OnLeaveFullScreenMode()", "OnRequestLeaveFullScreen()"):
            name = signature.split("(")[0]
            try:
                QObject.connect(ax, SIGNAL(signature), lambda *args, n=name: self._dispatch(n, int(args[0]) if args else 0))
            except Exception:  # noqa: BLE001
                pass
        try:
            ax.exception.connect(lambda code, source, desc, _help: self.events.append(
                f"exception:{code & 0xFFFFFFFF:#010x}:{self._op}:{source}:{desc}"))
        except (AttributeError, RuntimeError, TypeError):
            pass
        self.ax = ax
        return True

    @staticmethod
    def _int_arg(argc: int, argv, index: int = 0) -> int:  # noqa: ANN001
        """Il parametro intero numero index di un evento COM (argv punta a DISPPARAMS.rgvarg, al contrario)."""
        try:
            import ctypes

            address = int(argv)
            if not address or index >= argc:
                return 0
            variant = address + (argc - 1 - index) * ctypes.sizeof(ctypes.c_void_p) * 3  # VARIANT: 24 byte su x64
            vt = ctypes.c_ushort.from_address(variant).value
            if vt & 0x4000:  # VT_BYREF
                return ctypes.c_long.from_address(ctypes.c_void_p.from_address(variant + 8).value).value
            return ctypes.c_long.from_address(variant + 8).value
        except Exception:  # noqa: BLE001
            return 0

    def _com_event(self, name: str, argc: int, argv) -> None:  # noqa: ANN001
        self._dispatch(str(name).split("(")[0], self._int_arg(argc, argv))

    def _dispatch(self, event: str, code: int) -> None:
        now = time.monotonic()
        if self._last_event and self._last_event[:2] == (event, code) and now - self._last_event[2] < 0.3:
            return  # lo stesso evento arrivato sia dal segnale generico sia da quello con nome
        self._last_event = (event, code, now)
        handler = {
            "OnConnecting": lambda: self._on_connecting(),
            "OnConnected": lambda: self._on_connected(),
            "OnLoginComplete": lambda: self._on_login(),
            "OnDisconnected": lambda: self._on_disconnected(code),
            "OnFatalError": lambda: self._on_fatal(code),
            "OnWarning": lambda: self._on_warning(code),
            "OnLeaveFullScreenMode": lambda: self._on_leave_fullscreen(),
            "OnRequestLeaveFullScreen": lambda: self._on_leave_fullscreen(),
        }.get(event)
        if handler is not None:
            handler()

    def _set(self, target, name: str, value) -> bool:  # noqa: ANN001
        """Imposta una proprietà COM; una che questa versione di Windows non ha non deve fermare tutto.

        Le proprietà di sola scrittura (ClearTextPassword) non sono nel meta-oggetto di Qt: per quelle si
        passa da dynamicCall, che con il nome di una proprietà e un valore la imposta.
        """
        self._op = name
        try:
            if target.setProperty(name, value):
                return True
        except Exception:  # noqa: BLE001
            pass
        self.unset.append(f"{name}(setProperty)")
        try:
            target.dynamicCall(name, value)
            return True
        except Exception:  # noqa: BLE001
            self.unset.append(name)
            return False

    def configure(self) -> None:
        """Server, credenziali e opzioni della sessione, prima di Connect()."""
        ax = self.ax
        sess = self.sess
        host, port = model.split_host_port(sess.host, sess.port or "3389")
        self._set(ax, "Server", host)
        self._set(ax, "UserName", self.user)
        self._set(ax, "Domain", self.domain)
        self._set(ax, "ConnectingText", tr("Connessione a {host}…").format(host=host))
        self._set(ax, "FullScreenTitle", f"{sess.name or host} — FedTermHub")
        width, height = self._desktop_size()
        self._set(ax, "DesktopWidth", width)
        self._set(ax, "DesktopHeight", height)
        self._set(ax, "ColorDepth", 32)
        adv = ax.querySubObject("AdvancedSettings9") or ax.querySubObject("AdvancedSettings2")
        if adv is not None:
            if port and port != "3389":
                self._set(adv, "RDPPort", int(port))
            if self.password:
                self._set(adv, "ClearTextPassword", self.password)
            self._set(adv, "EnableCredSspSupport", True)  # NLA
            # 0: si collega anche se il certificato non è verificabile (come /cert:ignore su Linux);
            # 2: avvisa e chiede.
            self._set(adv, "AuthenticationLevel", 0 if sess.rdp_cert_ignore else 2)
            self._set(adv, "SmartSizing", False)
            self._set(adv, "RedirectClipboard", bool(sess.rdp_clipboard))
            self._set(adv, "RedirectPrinters", bool(sess.rdp_printer))
            self._set(adv, "RedirectSmartCards", bool(sess.rdp_smartcard))
            self._set(adv, "RedirectDrives", bool(sess.rdp_drive))
            self._set(adv, "ConnectToAdministerServer", bool(sess.rdp_admin))
            self._set(adv, "EnableAutoReconnect", bool(sess.rdp_auto_reconnect))
            self._set(adv, "ContainerHandledFullScreen", 0)
            self._set(adv, "BitmapPersistence", 1)
            self._set(adv, "Compress", 1)
            self._set(adv, "GrabFocusOnConnect", True)
            self._set(adv, "DisplayConnectionBar", True)
            self._set(adv, "PinConnectionBar", False)
        secured = ax.querySubObject("SecuredSettings3") or ax.querySubObject("SecuredSettings2")
        if secured is not None:
            # 0: il suono suona su questo PC; 2: niente audio
            self._set(secured, "AudioRedirectionMode", 0 if sess.rdp_sound else 2)
            self._set(secured, "KeyboardHookMode", 1)  # Alt+Tab, tasto Windows… vanno al PC remoto
        if sess.routing_mode == "rd_gateway" and sess.rdp_gateway_host:
            transport = ax.querySubObject("TransportSettings4") or ax.querySubObject("TransportSettings2")
            if transport is not None:
                gw_host, gw_port = model.split_host_port(sess.rdp_gateway_host, sess.rdp_gateway_port or "443")
                self._set(transport, "GatewayHostname", gw_host if gw_port in {"", "443"} else f"{gw_host}:{gw_port}")
                self._set(transport, "GatewayUsageMethod", 1)  # usa sempre il gateway
                self._set(transport, "GatewayProfileUsageMethod", 1)
                self._set(transport, "GatewayCredsSource", 0)  # utente e password
                self._set(transport, "GatewayCredSharing", 1 if sess.rdp_gateway_use_same_credentials else 0)
                if not sess.rdp_gateway_use_same_credentials and sess.rdp_gateway_user:
                    self._set(transport, "GatewayUsername", sess.rdp_gateway_user)
                    self._set(transport, "GatewayDomain", sess.rdp_gateway_domain)
                    if self.gateway_password:
                        self._set(transport, "GatewayPassword", self.gateway_password)

    def connect_session(self) -> None:
        if not self._create_control():
            self.state.setText(tr("Il componente RDP di Windows non si è avviato."))
            self.reconnect_button.show()
            return
        try:
            self.configure()
            self._op = "Connect"
            self.ax.dynamicCall("Connect()")
        except Exception as exc:  # noqa: BLE001
            self.state.setText(tr("Avvio RDP fallito: {exc}").format(exc=exc))
            self.reconnect_button.show()
            return
        self.reconnect_button.hide()
        if self.sess.rdp_start_fullscreen:
            QTimer.singleShot(1500, self.enter_fullscreen)

    def reconnect(self) -> None:
        self.connected = False
        self.connect_session()

    def disconnect_session(self) -> None:
        if self.ax is not None and self.connected:
            try:
                self.ax.dynamicCall("Disconnect()")
            except Exception:  # noqa: BLE001
                pass

    def enter_fullscreen(self) -> None:
        if self.ax is not None and self.connected:
            self._set(self.ax, "FullScreen", True)

    def _desktop_size(self):  # noqa: ANN202
        size = self.host_area.size()
        width = max(800, size.width()) // 4 * 4  # il componente vuole una larghezza multipla di 4
        height = max(600, size.height())
        return width, height

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802
        super().resizeEvent(event)
        if self.connected:
            self._resize_timer.start()

    def _apply_resolution(self) -> None:
        """La risoluzione remota segue la scheda (Windows 8.1 / Server 2012 R2 e successivi)."""
        if self.ax is None or not self.connected:
            return
        width, height = self._desktop_size()
        try:
            self.ax.dynamicCall("UpdateSessionDisplaySettings(uint,uint,uint,uint,uint,uint,uint)",
                                [width, height, width, height, 0, 100, 100])
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ eventi del componente
    def _on_connecting(self) -> None:
        self.events.append("connecting")
        self.state.setText(tr("Connessione in corso…"))

    def _on_connected(self) -> None:
        self.events.append("connected")
        self.connected = True
        self.state.setText(tr("Connesso, accesso in corso…"))

    def _on_login(self) -> None:
        self.events.append("login")
        self.connected = True
        self.state.setText(tr("Connesso a {host}").format(host=self.sess.host))
        self.status.emit(tr("Sessione attiva"))
        self.ax.setFocus()

    def _describe(self, reason: int) -> str:
        text = DISCONNECT_REASONS.get(reason, "")
        if not text and self.ax is not None:
            try:
                extended = int(self.ax.property("ExtendedDisconnectReason") or 0)
                text = str(self.ax.dynamicCall("GetErrorDescription(uint,uint)", [reason, extended]) or "")
            except Exception:  # noqa: BLE001
                text = ""
        return text or tr("Codice {code}").format(code=reason)

    def _on_disconnected(self, reason: int) -> None:
        self.events.append(f"disconnected:{reason}")
        self.connected = False
        if reason in USER_DISCONNECT:
            self.state.setText(tr("Sessione chiusa"))
        else:
            self.state.setText(tr("Disconnesso: {reason}").format(reason=self._describe(reason)))
        self.reconnect_button.show()
        self.status.emit(self.state.text())

    def _on_fatal(self, code: int) -> None:
        self.events.append(f"fatal:{code}")
        self.connected = False
        self.state.setText(tr("Errore del componente RDP (codice {code})").format(code=code))
        self.reconnect_button.show()

    def _on_warning(self, code: int) -> None:
        self.events.append(f"warning:{code}")
        self.status.emit(tr("Avviso RDP (codice {code})").format(code=code))

    def _on_leave_fullscreen(self) -> None:
        self._set(self.ax, "FullScreen", False)

    # ------------------------------------------------------------------ ripiego: mstsc
    def open_external(self) -> None:
        """Apre la sessione in mstsc con un file .rdp temporaneo (la password non va nel file)."""
        host, port = model.split_host_port(self.sess.host, self.sess.port or "3389")
        address = host if port in {"", "3389"} else f"{host}:{port}"
        lines = [f"full address:s:{address}", "prompt for credentials:i:0",
                 f"administrative session:i:{1 if self.sess.rdp_admin else 0}",
                 f"redirectclipboard:i:{1 if self.sess.rdp_clipboard else 0}",
                 f"redirectprinters:i:{1 if self.sess.rdp_printer else 0}",
                 f"redirectsmartcards:i:{1 if self.sess.rdp_smartcard else 0}",
                 f"screen mode id:i:{2 if self.sess.rdp_start_fullscreen else 1}"]
        if self.user:
            lines.append(f"username:s:{self.domain + chr(92) + self.user if self.domain else self.user}")
        fd, path = tempfile.mkstemp(prefix="fedtermhub-", suffix=".rdp")
        with os.fdopen(fd, "w", encoding="utf-16") as handle:
            handle.write("\r\n".join(lines) + "\r\n")
        try:
            subprocess.Popen(["mstsc", path])  # noqa: S603,S607
        except OSError as exc:
            self.state.setText(tr("Impossibile avviare mstsc: {exc}").format(exc=exc))
            return
        QTimer.singleShot(60_000, lambda: Path(path).unlink(missing_ok=True))
        self.status.emit(tr("Sessione aperta in Connessione Desktop remoto"))

    def close_tab(self) -> None:
        self.disconnect_session()
