# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""Graphical Windows session administration over WinRM for FedTermHub.

The module is intentionally optional. RDP works without WinRM; the manager is
used only to enumerate, message, disconnect, log off or reset Windows sessions.
Credentials are supplied by the already unlocked FedTermHub vault and are never
written by this module.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple
try:
    from .i18n import tr, trn
except ImportError:  # avvio come script (fedtermhub/fedtermhub.py) o dai test
    from i18n import tr, trn

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

try:
    import winrm  # type: ignore
except Exception:  # pragma: no cover - optional runtime dependency
    winrm = None


WTS_ENUM_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
if (-not ('FedTermHub.WtsAdmin' -as [type])) {
Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
namespace FedTermHub {
  public enum WTS_CONNECTSTATE_CLASS {
    Active, Connected, ConnectQuery, Shadow, Disconnected, Idle,
    Listen, Reset, Down, Init
  }
  public enum WTS_INFO_CLASS {
    InitialProgram, ApplicationName, WorkingDirectory, OEMId,
    SessionId, UserName, WinStationName, DomainName, ConnectState,
    ClientBuildNumber, ClientName, ClientDirectory, ClientProductId,
    ClientHardwareId, ClientAddress, ClientDisplay, ClientProtocolType
  }
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  public struct WTS_SESSION_INFO {
    public Int32 SessionID;
    public IntPtr pWinStationName;
    public WTS_CONNECTSTATE_CLASS State;
  }
  public class SessionRow {
    public int Id { get; set; }
    public string Station { get; set; }
    public string State { get; set; }
    public string User { get; set; }
    public string Domain { get; set; }
    public string Client { get; set; }
  }
  public static class WtsAdmin {
    [DllImport("wtsapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    static extern bool WTSEnumerateSessions(
      IntPtr hServer, int Reserved, int Version,
      out IntPtr ppSessionInfo, out int pCount);
    [DllImport("wtsapi32.dll")]
    static extern void WTSFreeMemory(IntPtr pMemory);
    [DllImport("wtsapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    static extern bool WTSQuerySessionInformation(
      IntPtr hServer, int sessionId, WTS_INFO_CLASS infoClass,
      out IntPtr ppBuffer, out int bytesReturned);

    static string Query(int id, WTS_INFO_CLASS cls) {
      IntPtr buffer = IntPtr.Zero; int bytes = 0;
      try {
        if (!WTSQuerySessionInformation(IntPtr.Zero, id, cls, out buffer, out bytes)
            || buffer == IntPtr.Zero || bytes <= 1) return "";
        return Marshal.PtrToStringUni(buffer) ?? "";
      } finally {
        if (buffer != IntPtr.Zero) WTSFreeMemory(buffer);
      }
    }

    public static SessionRow[] List() {
      IntPtr info = IntPtr.Zero; int count = 0;
      var rows = new List<SessionRow>();
      if (!WTSEnumerateSessions(IntPtr.Zero, 0, 1, out info, out count))
        throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
      try {
        int size = Marshal.SizeOf(typeof(WTS_SESSION_INFO));
        long current = info.ToInt64();
        for (int i = 0; i < count; i++) {
          var item = (WTS_SESSION_INFO)Marshal.PtrToStructure(
            new IntPtr(current), typeof(WTS_SESSION_INFO));
          current += size;
          rows.Add(new SessionRow {
            Id = item.SessionID,
            Station = Marshal.PtrToStringUni(item.pWinStationName) ?? "",
            State = item.State.ToString(),
            User = Query(item.SessionID, WTS_INFO_CLASS.UserName),
            Domain = Query(item.SessionID, WTS_INFO_CLASS.DomainName),
            Client = Query(item.SessionID, WTS_INFO_CLASS.ClientName)
          });
        }
      } finally { if (info != IntPtr.Zero) WTSFreeMemory(info); }
      return rows.ToArray();
    }
  }
}
"@
}
[FedTermHub.WtsAdmin]::List() | ConvertTo-Json -Compress -Depth 3
'''


@dataclass
class WindowsSessionRow:
    session_id: int
    station: str
    state: str
    user: str
    domain: str
    client: str

    @property
    def identity(self) -> str:
        if self.domain and self.user:
            return f"{self.domain}\\{self.user}"
        return self.user or "—"


class WindowsSessionsDialog(Gtk.Dialog):
    """Manage Terminal Services sessions on a Windows host through WinRM."""

    def __init__(
        self,
        parent: Gtk.Window,
        host: str,
        username: str,
        domain: str,
        password: Optional[str],
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(title=tr("Gestisci sessioni Windows"), transient_for=parent, modal=False)
        self.set_default_size(930, 600)
        self.set_resizable(True)
        self.add_button(tr("Chiudi"), Gtk.ResponseType.CLOSE)
        self.connect("response", lambda dialog, _response: dialog.destroy())

        self.password = password or ""
        self.status_callback = status_callback
        self.busy = False

        self.host_entry = Gtk.Entry(text=host)
        self.host_entry.set_placeholder_text(tr("PC o server Windows"))
        self.port_spin = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.port_spin.set_value(5985)
        self.https_check = Gtk.CheckButton(label="HTTPS")
        self.https_check.connect("toggled", self._on_https_toggled)
        self.ignore_cert_check = Gtk.CheckButton(label=tr("Ignora certificato non attendibile"))
        self.ignore_cert_check.set_active(False)
        self.ignore_cert_check.set_tooltip_text(
            tr("Abilita solo per un listener HTTPS interno con certificato self-signed verificato.")
        )

        self.user_entry = Gtk.Entry(text=username)
        self.domain_entry = Gtk.Entry(text=domain)
        self.password_entry = Gtk.Entry()
        self.password_entry.set_text(self.password)
        self.password_entry.set_visibility(False)
        self.password_entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.password_entry.set_placeholder_text(tr("Password WinRM / amministratore"))
        self.password_entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY, "view-reveal-symbolic"
        )
        self.password_entry.set_icon_tooltip_text(
            Gtk.EntryIconPosition.SECONDARY, tr("Mostra o nascondi la password")
        )
        self.password_entry.connect("icon-press", self._toggle_password_visibility)
        self.transport_combo = Gtk.ComboBoxText()
        for key, label in (
            ("ntlm", tr("NTLM / dominio")),
            ("kerberos", tr("Kerberos")),
            ("credssp", tr("CredSSP")),
            ("basic", tr("Basic (solo HTTPS)")),
        ):
            self.transport_combo.append(key, label)
        self.transport_combo.set_active_id("ntlm")

        self.store = Gtk.ListStore(int, str, str, str, str, str)
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.set_headers_visible(True)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        for index, title in enumerate(("ID", tr("Utente"), tr("Stato"), tr("Sessione"), tr("Client"), tr("Dominio"))):
            renderer = Gtk.CellRendererText()
            if index == 0:
                renderer.set_property("xalign", 1.0)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_expand(index in {1, 3, 4})
            self.tree.append_column(column)

        self.spinner = Gtk.Spinner()
        self.status_label = Gtk.Label(label=tr("WinRM non è necessario per RDP: serve solo per amministrare le sessioni."), xalign=0)
        self.status_label.set_line_wrap(True)

        self._build_ui()
        self.show_all()
        self.spinner.hide()

        if winrm is None:
            self._set_status(tr("Modulo WinRM mancante. Installa: sudo dnf install python3-winrm"), error=True)
        elif not self.password:
            self._set_status(tr("Password non disponibile: sblocca il Password Manager o inseriscila nella sessione RDP."), error=True)
        else:
            GLib.idle_add(self.refresh)

    def _build_ui(self) -> None:
        root = self.get_content_area()
        root.set_spacing(12)
        root.set_border_width(14)

        intro = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name("computer-symbolic", Gtk.IconSize.DIALOG)
        intro.pack_start(icon, False, False, 0)
        intro_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        title = Gtk.Label(label=tr("Sessioni Remote Desktop sul server"), xalign=0)
        title.get_style_context().add_class("panel-title")
        desc = Gtk.Label(
            label=(tr("Visualizza le sessioni Windows e scegli un'azione amministrativa. "
                   "Disconnetti conserva programmi e documenti; Logoff e Reset possono perdere dati non salvati.")),
            xalign=0,
        )
        desc.set_line_wrap(True)
        intro_text.pack_start(title, False, False, 0)
        intro_text.pack_start(desc, False, False, 0)
        intro.pack_start(intro_text, True, True, 0)
        root.pack_start(intro, False, False, 0)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        grid.get_style_context().add_class("settings-card")
        grid.set_border_width(12)
        labels = (
            (tr("Host"), self.host_entry),
            (tr("Porta WinRM"), self.port_spin),
            (tr("Utente"), self.user_entry),
            (tr("Dominio"), self.domain_entry),
            (tr("Password"), self.password_entry),
            (tr("Autenticazione"), self.transport_combo),
        )
        for row, (text, widget) in enumerate(labels):
            label = Gtk.Label(label=text, xalign=0)
            label.get_style_context().add_class("field-label")
            grid.attach(label, 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
        checks = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        checks.pack_start(self.https_check, False, False, 0)
        checks.pack_start(self.ignore_cert_check, False, False, 0)
        grid.attach(checks, 1, len(labels), 1, 1)
        grid.set_hexpand(True)
        root.pack_start(grid, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(self.tree)
        root.pack_start(scroll, True, True, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.refresh_button = self._action_button(tr("Aggiorna"), "view-refresh-symbolic", lambda _b: self.refresh())
        self.message_button = self._action_button(tr("Invia messaggio"), "mail-message-new-symbolic", self._on_message)
        self.disconnect_button = self._action_button(tr("Disconnetti"), "network-offline-symbolic", self._on_disconnect)
        self.logoff_button = self._action_button(tr("Logoff"), "system-log-out-symbolic", self._on_logoff)
        self.reset_button = self._action_button(tr("Reset forzato"), "edit-delete-symbolic", self._on_reset)
        for button in (self.refresh_button, self.message_button, self.disconnect_button, self.logoff_button, self.reset_button):
            actions.pack_start(button, False, False, 0)
        actions.pack_end(self.spinner, False, False, 0)
        root.pack_start(actions, False, False, 0)
        root.pack_start(self.status_label, False, False, 0)

    @staticmethod
    def _action_button(label: str, icon: str, callback) -> Gtk.Button:  # noqa: ANN001
        button = Gtk.Button()
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU), False, False, 0)
        row.pack_start(Gtk.Label(label=label), False, False, 0)
        button.add(row)
        button.connect("clicked", callback)
        button.get_style_context().add_class("mini-action")
        return button

    def _toggle_password_visibility(
        self, entry: Gtk.Entry, _position: Gtk.EntryIconPosition, _event
    ) -> None:  # noqa: ANN001
        entry.set_visibility(not entry.get_visibility())
        entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY,
            "view-conceal-symbolic" if entry.get_visibility() else "view-reveal-symbolic",
        )

    def _on_https_toggled(self, check: Gtk.CheckButton) -> None:
        self.port_spin.set_value(5986 if check.get_active() else 5985)

    def _set_busy(self, value: bool) -> None:
        self.busy = value
        for widget in (
            self.refresh_button, self.message_button, self.disconnect_button,
            self.logoff_button, self.reset_button,
        ):
            widget.set_sensitive(not value)
        if value:
            self.spinner.show()
            self.spinner.start()
        else:
            self.spinner.stop()
            self.spinner.hide()

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status_label.set_text(text)
        context = self.status_label.get_style_context()
        context.remove_class("error-text")
        context.remove_class("success-text")
        context.add_class("error-text" if error else "success-text")
        if self.status_callback:
            self.status_callback(text)

    def _endpoint_and_auth(self) -> Tuple[str, str, Tuple[str, str], str]:
        host = self.host_entry.get_text().strip()
        if not host:
            raise ValueError(tr("Host Windows mancante"))
        scheme = "https" if self.https_check.get_active() else "http"
        port = int(self.port_spin.get_value())
        endpoint = f"{scheme}://{host}:{port}/wsman"
        user = self.user_entry.get_text().strip()
        domain = self.domain_entry.get_text().strip()
        auth_user = f"{domain}\\{user}" if domain and "\\" not in user and "@" not in user else user
        password = self.password_entry.get_text()
        if not auth_user or not password:
            raise ValueError(tr("Utente o password WinRM mancanti"))
        transport = self.transport_combo.get_active_id() or "ntlm"
        if transport == "basic" and scheme != "https":
            raise ValueError(tr("L'autenticazione Basic è consentita soltanto con WinRM HTTPS"))
        validation = "ignore" if self.ignore_cert_check.get_active() else "validate"
        return endpoint, transport, (auth_user, password), validation

    @staticmethod
    def _new_session(config):  # noqa: ANN001, ANN205
        if winrm is None:
            raise RuntimeError(tr("python3-winrm non installato"))
        endpoint, transport, auth, validation = config
        return winrm.Session(
            endpoint,
            auth=auth,
            transport=transport,
            server_cert_validation=validation,
            read_timeout_sec=35,
            operation_timeout_sec=25,
        )

    @staticmethod
    def _decode(data) -> str:  # noqa: ANN001
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace").strip()
        return str(data or "").strip()

    def _run_async(self, label: str, worker: Callable[[], object], done: Callable[[object], None]) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self._set_status(label)

        def runner() -> None:
            try:
                result = worker()
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._async_error, str(exc))
                return
            GLib.idle_add(self._async_done, result, done)

        threading.Thread(target=runner, daemon=True).start()

    def _async_error(self, message: str) -> bool:
        self._set_busy(False)
        self._set_status(tr("Operazione WinRM fallita: {message}").format(message=message), error=True)
        return False

    def _async_done(self, result: object, done: Callable[[object], None]) -> bool:
        self._set_busy(False)
        done(result)
        return False

    def refresh(self) -> bool:
        if winrm is None:
            self._set_status(tr("Modulo WinRM mancante. Installa: sudo dnf install python3-winrm"), error=True)
            return False
        try:
            config = self._endpoint_and_auth()
        except Exception as exc:  # noqa: BLE001
            self._set_status(str(exc), error=True)
            return False

        def worker() -> List[WindowsSessionRow]:
            response = self._new_session(config).run_ps(WTS_ENUM_SCRIPT)
            if response.status_code != 0:
                err = self._decode(response.std_err) or self._decode(response.std_out)
                raise RuntimeError(err or tr("PowerShell exit code {status_code}").format(status_code=response.status_code))
            text = self._decode(response.std_out)
            if not text:
                return []
            raw = json.loads(text)
            entries = raw if isinstance(raw, list) else [raw]
            rows: List[WindowsSessionRow] = []
            for item in entries:
                if not isinstance(item, dict):
                    continue
                rows.append(WindowsSessionRow(
                    session_id=int(item.get("Id", -1)),
                    station=str(item.get("Station", "") or ""),
                    state=str(item.get("State", "") or ""),
                    user=str(item.get("User", "") or ""),
                    domain=str(item.get("Domain", "") or ""),
                    client=str(item.get("Client", "") or ""),
                ))
            return sorted(rows, key=lambda row: row.session_id)

        def done(result: object) -> None:
            rows = result if isinstance(result, list) else []
            self.store.clear()
            for row in rows:
                if isinstance(row, WindowsSessionRow):
                    self.store.append([row.session_id, row.identity, row.state, row.station, row.client or "—", row.domain or "—"])
            self._set_status(tr("{n_rows} sessioni rilevate su {strip}.").format(n_rows=len(rows), strip=self.host_entry.get_text().strip()))

        self._run_async(tr("Lettura sessioni Windows in corso…"), worker, done)
        return False

    def _selected(self) -> Optional[Tuple[int, str, str]]:
        model, tree_iter = self.tree.get_selection().get_selected()
        if tree_iter is None:
            self._set_status(tr("Seleziona prima una sessione Windows."), error=True)
            return None
        return int(model[tree_iter][0]), str(model[tree_iter][1]), str(model[tree_iter][2])

    def _confirm(self, title: str, text: str, destructive: bool = False) -> bool:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING if destructive else Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=title,
        )
        dialog.format_secondary_text(text)
        dialog.add_button(tr("Annulla"), Gtk.ResponseType.CANCEL)
        dialog.add_button(tr("Conferma"), Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def _run_session_command(self, command: str, session_id: int, args: Optional[List[str]] = None, success: str = tr("Operazione completata")) -> None:
        params = [str(session_id)] + list(args or [])
        try:
            config = self._endpoint_and_auth()
        except Exception as exc:  # noqa: BLE001
            self._set_status(str(exc), error=True)
            return

        def worker() -> str:
            response = self._new_session(config).run_cmd(command, params)
            out = self._decode(response.std_out)
            err = self._decode(response.std_err)
            if response.status_code != 0:
                raise RuntimeError(err or out or tr("{command}: exit code {status_code}").format(command=command, status_code=response.status_code))
            return out

        def done(_result: object) -> None:
            self._set_status(success)
            GLib.timeout_add(450, self.refresh)

        self._run_async(tr("Esecuzione {command}…").format(command=command), worker, done)

    def _on_disconnect(self, _button: Gtk.Button) -> None:
        selected = self._selected()
        if not selected:
            return
        session_id, user, _state = selected
        if self._confirm(tr("Disconnettere la sessione?"), tr("{user} (ID {session_id}) resterà aperta con programmi e documenti in esecuzione.").format(user=user, session_id=session_id)):
            self._run_session_command("tsdiscon.exe", session_id, success=tr("Sessione {session_id} disconnessa.").format(session_id=session_id))

    def _on_logoff(self, _button: Gtk.Button) -> None:
        selected = self._selected()
        if not selected:
            return
        session_id, user, _state = selected
        if self._confirm(
            tr("Eseguire il logoff?"),
            tr("La sessione {user} (ID {session_id}) verrà chiusa. Le modifiche non salvate possono andare perse.").format(user=user, session_id=session_id),
            destructive=True,
        ):
            self._run_session_command("logoff.exe", session_id, success=tr("Logoff della sessione {session_id} richiesto.").format(session_id=session_id))

    def _on_reset(self, _button: Gtk.Button) -> None:
        selected = self._selected()
        if not selected:
            return
        session_id, user, _state = selected
        if self._confirm(
            tr("Reset forzato della sessione?"),
            tr("Usa questa azione solo per una sessione bloccata. {user} (ID {session_id}) perderà eventuali dati non salvati.").format(user=user, session_id=session_id),
            destructive=True,
        ):
            self._run_session_command("rwinsta.exe", session_id, success=tr("Reset della sessione {session_id} richiesto.").format(session_id=session_id))

    def _on_message(self, _button: Gtk.Button) -> None:
        selected = self._selected()
        if not selected:
            return
        session_id, user, _state = selected
        dialog = Gtk.Dialog(title=tr("Invia messaggio alla sessione"), transient_for=self, modal=True)
        dialog.add_button(tr("Annulla"), Gtk.ResponseType.CANCEL)
        dialog.add_button(tr("Invia"), Gtk.ResponseType.OK)
        entry = Gtk.Entry()
        entry.set_placeholder_text(tr("Messaggio per {user}").format(user=user))
        entry.set_activates_default(True)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_border_width(14)
        box.pack_start(Gtk.Label(label=tr("Sessione {session_id} — {user}").format(session_id=session_id, user=user), xalign=0), False, False, 0)
        box.pack_start(entry, False, False, 8)
        dialog.show_all()
        response = dialog.run()
        message = entry.get_text().strip()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not message:
            return
        # msg.exe riceve gli argomenti tramite cmd.exe: virgolette doppie e
        # newline spezzerebbero il comando remoto.
        sanitized = message.replace('"', "'").replace("\r", " ").replace("\n", " ").strip()
        if not sanitized:
            return
        self._run_session_command("msg.exe", session_id, [sanitized], success=tr("Messaggio inviato alla sessione {session_id}.").format(session_id=session_id))

    def destroy(self) -> None:  # type: ignore[override]
        self.password = ""
        if hasattr(self, "password_entry"):
            self.password_entry.set_text("")
        super().destroy()
