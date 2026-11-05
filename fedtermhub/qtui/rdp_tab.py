# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Desktop remoto nell'interfaccia Qt.

Su Windows la scheda ospita il componente RDP di Windows (lo stesso di mstsc: MsRdpClient9
NotSafeForScripting, mstscax.dll). Così NLA, RD Gateway, smart card, appunti, stampanti e aggiornamenti di
sicurezza sono quelli di Microsoft, senza librerie da portare con sé.

Il componente sta in una finestra «AtlAxWin» (ATL, presente in ogni Windows) figlia di un QWidget nativo e
si pilota con comtypes: con QAxWidget di PySide6 le proprietà del componente non sono raggiungibili
(il meta-oggetto Python nasconde quello generato da QAxBase) e Connect() fallisce con E_INVALIDARG.

Su Linux (per ora) l'interfaccia Qt non ha un motore RDP: la versione GTK usa FreeRDP.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
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
MSTSCAX = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "mstscax.dll")

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


def _typelib():  # noqa: ANN202
    """Il modulo comtypes di mstscax.dll: già generato nella build, altrimenti generato ora."""
    try:
        from comtypes.gen import MSTSCLib  # type: ignore[import-not-found]

        return MSTSCLib
    except ImportError:
        import comtypes.client

        return comtypes.client.GetModule(MSTSCAX)


def ax_available() -> bool:
    """True se il desktop remoto incorporato si può usare (Windows con comtypes e mstscax.dll)."""
    if not IS_WINDOWS or not os.path.isfile(MSTSCAX):
        return False
    try:
        import comtypes.client  # noqa: F401
    except ImportError:
        return False
    return True


class _ControlHost(QWidget):
    """QWidget nativo che contiene la finestra ATL del componente e la ridimensiona con sé."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.child_hwnd = 0
        self.unknown = None

    def create(self, clsid: str):  # noqa: ANN201
        import ctypes
        from ctypes import wintypes

        import comtypes

        atl = ctypes.WinDLL("atl")
        user32 = ctypes.WinDLL("user32")
        atl.AtlAxWinInit()
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        ws_child, ws_visible, ws_clipchildren, ws_clipsiblings = 0x40000000, 0x10000000, 0x02000000, 0x04000000
        width, height = self._physical_size()
        hwnd = user32.CreateWindowExW(0, "AtlAxWin", clsid, ws_child | ws_visible | ws_clipchildren | ws_clipsiblings,
                                      0, 0, width, height, int(self.winId()), None, None, None)
        if not hwnd:
            raise OSError(ctypes.get_last_error() or "CreateWindowEx(AtlAxWin)")
        self.child_hwnd = hwnd
        unknown = ctypes.POINTER(comtypes.IUnknown)()
        atl.AtlAxGetControl.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.POINTER(comtypes.IUnknown))]
        result = atl.AtlAxGetControl(hwnd, ctypes.byref(unknown))
        if result != 0 or not unknown:
            raise OSError(f"AtlAxGetControl: {result & 0xFFFFFFFF:#010x}")
        self.unknown = unknown
        return unknown

    def _physical_size(self):  # noqa: ANN202
        ratio = self.devicePixelRatioF()
        return max(1, round(self.width() * ratio)), max(1, round(self.height() * ratio))

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802
        super().resizeEvent(event)
        if self.child_hwnd:
            import ctypes

            width, height = self._physical_size()
            ctypes.windll.user32.MoveWindow(self.child_hwnd, 0, 0, width, height, True)  # type: ignore[attr-defined]

    def focusInEvent(self, event) -> None:  # noqa: ANN001, N802
        super().focusInEvent(event)
        if self.child_hwnd:
            import ctypes

            ctypes.windll.user32.SetFocus(self.child_hwnd)  # type: ignore[attr-defined]

    def destroy_control(self) -> None:
        self.unknown = None
        if self.child_hwnd:
            import ctypes

            ctypes.windll.user32.DestroyWindow(self.child_hwnd)  # type: ignore[attr-defined]
            self.child_hwnd = 0


class _EventSink:
    """Riceve gli eventi del componente (IMsTscAxEvents) e li passa alla scheda."""

    def __init__(self, tab: "RdpTab") -> None:
        self.tab = tab

    def _safe(self, func, *args) -> None:  # noqa: ANN001, ANN002
        # Un evento può arrivare mentre la scheda viene chiusa: i widget Qt potrebbero non esserci più.
        try:
            func(*args)
        except RuntimeError:
            pass

    @staticmethod
    def _code(args) -> int:  # noqa: ANN001
        try:
            return int(args[-1])
        except (IndexError, TypeError, ValueError):
            return 0

    def OnConnecting(self, *args) -> None:  # noqa: N802, ANN002
        self._safe(self.tab._on_connecting)

    def OnConnected(self, *args) -> None:  # noqa: N802, ANN002
        self._safe(self.tab._on_connected)

    def OnLoginComplete(self, *args) -> None:  # noqa: N802, ANN002
        self._safe(self.tab._on_login)

    def OnDisconnected(self, *args) -> None:  # noqa: N802, ANN002
        self._safe(self.tab._on_disconnected, self._code(args))

    def OnFatalError(self, *args) -> None:  # noqa: N802, ANN002
        self._safe(self.tab._on_fatal, self._code(args))

    def OnWarning(self, *args) -> None:  # noqa: N802, ANN002
        self._safe(self.tab._on_warning, self._code(args))

    def OnLogonError(self, *args) -> None:  # noqa: N802, ANN002
        self.tab.events.append(f"logon-error:{self._code(args)}")

    def OnLeaveFullScreenMode(self, *args) -> None:  # noqa: N802, ANN002
        self._safe(self.tab._on_leave_fullscreen)

    def OnRequestLeaveFullScreen(self, *args) -> None:  # noqa: N802, ANN002
        self._safe(self.tab._on_leave_fullscreen)

    def OnRequestContainerMinimize(self, *args) -> None:  # noqa: N802, ANN002
        window = self.tab.window()
        if window is not None:
            window.showMinimized()


class RdpTab(QWidget):
    """Una scheda con un desktop remoto. Segnali: status (testo per la barra di stato)."""

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
        self.ax = None  # interfaccia IMsRdpClient del componente
        self.host: Optional[_ControlHost] = None
        self._events_connection = None
        self.connected = False
        self.events: List[str] = []  # eventi ricevuti dal componente (servono anche alla prova automatica)
        self.unset: List[str] = []  # proprietà che il componente non ha accettato (diagnostica)
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
            # Dopo che la scheda è comparsa: il componente prende la dimensione della finestra che lo ospita.
            QTimer.singleShot(50, self.connect_session)
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
        import comtypes.client

        self._release_control()
        host = _ControlHost(self.host_area)
        self.host_layout.addWidget(host)
        host.show()
        try:
            unknown = host.create(RDP_CLSID)
            lib = _typelib()
            ax = None
            for name in ("IMsRdpClient10", "IMsRdpClient9", "IMsRdpClient8", "IMsRdpClient7"):
                interface = getattr(lib, name, None)
                if interface is None:
                    continue
                try:
                    ax = unknown.QueryInterface(interface)
                    break
                except Exception:  # noqa: BLE001
                    continue
            if ax is None:
                raise OSError("IMsRdpClient")
            self._lib = lib
            self._events_connection = comtypes.client.GetEvents(ax, _EventSink(self), interface=lib.IMsTscAxEvents)
        except Exception as exc:  # noqa: BLE001
            host.destroy_control()
            host.deleteLater()
            self.events.append(f"create-error:{exc}")
            return False
        self.host = host
        self.ax = ax
        return True

    def _release_control(self) -> None:
        self._events_connection = None
        self.ax = None
        if self.host is not None:
            self.host.destroy_control()
            self.host.hide()
            self.host.deleteLater()
            self.host = None

    def _set(self, target, name: str, value) -> bool:  # noqa: ANN001
        """Imposta una proprietà COM; una che questa versione di Windows non ha non deve fermare tutto."""
        if target is None:
            self.unset.append(name)
            return False
        try:
            setattr(target, name, value)
            return True
        except Exception:  # noqa: BLE001
            self.unset.append(name)
            return False

    def _sub(self, *names: str):  # noqa: ANN202
        """La prima interfaccia secondaria disponibile (AdvancedSettings9, poi 8...)."""
        for name in names:
            try:
                value = getattr(self.ax, name)
                if value is not None:
                    return value
            except Exception:  # noqa: BLE001
                continue
        return None

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
        if self.password:
            try:
                self.ax.QueryInterface(self._lib.IMsTscNonScriptable).ClearTextPassword = self.password
            except Exception:  # noqa: BLE001
                self.unset.append("ClearTextPassword")
        adv = self._sub("AdvancedSettings9", "AdvancedSettings8", "AdvancedSettings7")
        if adv is not None:
            if port and port != "3389":
                self._set(adv, "RDPPort", int(port))
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
        secured = self._sub("SecuredSettings3", "SecuredSettings2")
        if secured is not None:
            # 0: il suono suona su questo PC; 2: niente audio
            self._set(secured, "AudioRedirectionMode", 0 if sess.rdp_sound else 2)
            self._set(secured, "KeyboardHookMode", 1)  # Alt+Tab, tasto Windows… vanno al PC remoto
        if sess.routing_mode == "rd_gateway" and sess.rdp_gateway_host:
            transport = self._sub("TransportSettings4", "TransportSettings3", "TransportSettings2")
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
            self.ax.Connect()
        except Exception as exc:  # noqa: BLE001
            self.events.append(f"connect-error:{exc}")
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
                self.ax.Disconnect()
            except Exception:  # noqa: BLE001
                pass

    def enter_fullscreen(self) -> None:
        if self.ax is not None and self.connected:
            self._set(self.ax, "FullScreen", True)

    def _desktop_size(self):  # noqa: ANN202
        size = self.host_area.size()
        ratio = self.devicePixelRatioF()
        # La dimensione della scheda (senza barre di scorrimento); il componente vuole una larghezza multipla di 4.
        width = max(640, round(size.width() * ratio)) // 4 * 4
        height = max(480, round(size.height() * ratio))
        return min(width, 8192), min(height, 8192)

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
            self.ax.UpdateSessionDisplaySettings(width, height, width, height, 0, 100, 100)
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
        if self.host is not None:
            self.host.setFocus()
        # Se la scheda ha cambiato dimensione durante la connessione, il desktop remoto si adegua.
        self._resize_timer.start()

    def _describe(self, reason: int) -> str:
        text = DISCONNECT_REASONS.get(reason, "")
        if not text and self.ax is not None:
            try:
                extended = int(self.ax.ExtendedDisconnectReason or 0)
                text = str(self.ax.GetErrorDescription(reason, extended) or "")
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
