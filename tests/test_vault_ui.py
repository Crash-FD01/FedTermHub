#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Crash
# SPDX-License-Identifier: GPL-3.0-or-later
"""La finestra Password e le sue finestre di supporto, provate con GTK reale.

1. Modello: gruppo salvato e letto (anche da un archivio delle versioni precedenti, che non lo
   ha), ordine per gruppo con "Senza gruppo" in fondo, cambio della master password.
2. Generatore e robustezza delle password.
3. Finestra Password: pagine vuota / nessuna selezione / dettaglio, intestazioni dei gruppi,
   ricerca a piu' parole, sessioni che usano una credenziale, raggruppamento suggerito per
   cartella, Ctrl+C che copia la password solo quando il fuoco non e' in un campo di testo.
4. Master password sbagliata: l'errore resta nella finestra e si riprova, invece di chiuderla.
5. La password RDP salvata dalla sessione finisce nel gruppo della cartella della sessione.

Uso:  xvfb-run -a python3 tests/test_vault_ui.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# I test controllano i testi originali in italiano, qualunque sia la lingua del sistema.
os.environ["FEDTERMHUB_LANGUAGE"] = "it"
sys.path.insert(0, str(PROJECT_ROOT / "fedtermhub"))


def pump(seconds: float = 0.2) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.01)


def key_event(widget: Gtk.Widget, keyval: int, ctrl: bool = True):
    ev = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    ev.key.keyval = keyval
    ev.key.state = Gdk.ModifierType.CONTROL_MASK if ctrl else Gdk.ModifierType(0)
    ev.key.window = widget.get_window()
    return ev  # l'evento intero: ev.key da solo punterebbe a memoria liberata


def check_model(app, tmp: Path) -> None:
    C = app.Credential
    old_format = {"id": "x1", "name": "Vecchia", "username": "u", "password": "p"}
    assert C.from_dict(old_format).group == "", "un archivio senza gruppo deve restare leggibile"
    assert C.from_dict(C(name="n", group="Studio Rossi").to_dict()).group == "Studio Rossi"
    assert C(name="n", username="mario", domain="ROSSI").account_text() == "ROSSI\\mario"

    vault = app.PasswordVault(tmp / "vault-model.json")
    vault.create("vecchia-master-1")
    vault.credentials = [
        C(name="zeta"), C(name="beta", group="Officine Bianchi"), C(name="Alfa", group="studio Rossi"),
        C(name="alfa", group="Officine Bianchi"),
    ]
    vault.save()
    order = [(c.group, c.name) for c in vault.sorted_credentials()]
    assert order == [("Officine Bianchi", "alfa"), ("Officine Bianchi", "beta"), ("studio Rossi", "Alfa"), ("", "zeta")], order
    assert vault.groups() == ["Officine Bianchi", "studio Rossi"]
    assert dict(vault.labels())[vault.credentials[1].id] == "beta  ·  Officine Bianchi"

    vault.change_master_password("nuova-master-2")
    assert not vault.verify_master_password("vecchia-master-1")
    assert vault.verify_master_password("nuova-master-2")
    reopened = app.PasswordVault(tmp / "vault-model.json")
    reopened.unlock("nuova-master-2")
    assert len(reopened.credentials) == 4 and {c.group for c in reopened.credentials} == {"", "Officine Bianchi", "studio Rossi"}
    print("  modello: gruppo letto/scritto, ordine per gruppo, cambio master password")


def check_generator() -> None:
    import fedtermhub as app  # noqa: PLC0415

    seen = set()
    for _ in range(200):
        pw = app.generate_password()
        assert len(pw) == 20
        assert any(c.isupper() for c in pw) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw)
        assert any(c in app.PASSWORD_SYMBOLS for c in pw)
        assert not set(pw) & set("0O1lI"), "caratteri che si confondono"
        seen.add(pw)
    assert len(seen) == 200
    assert app.password_strength("")[0] == 0
    assert app.password_strength("aaaaaaaaaaaa")[0] == 0
    assert app.password_strength("password")[0] <= 1
    assert app.password_strength(app.generate_password())[0] == 4
    print("  generatore: 20 caratteri, tutti i tipi, niente ambigui; robustezza coerente")


def build_window(app):
    application = Gtk.Application(application_id="io.github.crash_fd01.fedtermhub.VaultUiTest")
    assert application.register(None)
    window = app.AppWindow(application)
    window.show_all()
    return window


def check_manager(app, window) -> None:
    C, S, F = app.Credential, app.Session, app.FolderNode
    rossi_admin = C(name="Administrator Rossi", username="Administrator", domain="ROSSI", password="Segreta-Rossi-1")
    rossi_fw = C(name="firewall Rossi", username="admin", password="fw")
    sciolta = C(name="Senza sessioni", username="x", password="y")
    condivisa = C(name="Condivisa", username="ops", password="z")
    home = window.store.home_node()
    home.children = [
        F(name="Clienti", children=[
            F(name="Studio Rossi", children=[
                S(name="dc01", protocol="rdp", host="10.0.0.10", credential_id=rossi_admin.id),
                S(name="app02", protocol="rdp", host="10.0.0.12", credential_id=rossi_admin.id),
                S(name="fw", protocol="ssh", host="10.0.0.1", credential_id=rossi_fw.id),
                S(name="ops-rossi", protocol="ssh", host="10.0.0.2", credential_id=condivisa.id),
            ]),
            F(name="Officine Bianchi", children=[
                S(name="ops-bianchi", protocol="ssh", host="10.1.0.2", credential_id=condivisa.id),
            ]),
        ]),
    ]
    window.store.save()
    window.vault.create("master-di-prova")

    # Archivio vuoto: pagina di benvenuto con il pulsante per la prima credenziale.
    dialog = app.VaultManagerDialog(window, window.vault)
    pump()
    assert dialog.stack.get_visible_child_name() == "empty"
    dialog.destroy()

    window.vault.credentials = [rossi_admin, rossi_fw, sciolta, condivisa]
    window.vault.save()

    usage = window.credential_usage(rossi_admin.id)
    assert [(n, k, f) for n, k, f in usage] == [("dc01", "RDP", "Clienti / Studio Rossi"), ("app02", "RDP", "Clienti / Studio Rossi")], usage
    suggestions = window.credential_group_suggestions()
    assert suggestions == {rossi_admin.id: "Studio Rossi", rossi_fw.id: "Studio Rossi"}, \
        "si suggerisce solo con una cartella sola: niente per quella condivisa fra clienti o non usata"

    dialog = app.VaultManagerDialog(window, window.vault)
    pump()
    assert dialog.stack.get_visible_child_name() == "nothing", "con credenziali e nessuna selezione: 'Scegli una credenziale'"
    assert dialog.hint.get_visible(), "il suggerimento di raggruppamento deve comparire"
    assert all(row.get_header() is None for row in dialog.listbox.get_children()), "senza gruppi niente intestazioni"

    dialog.on_apply_groups(None)
    pump()
    assert rossi_admin.group == "Studio Rossi" and rossi_fw.group == "Studio Rossi" and condivisa.group == ""
    assert not dialog.hint.get_visible()
    dialog.show_all()
    pump()
    assert not dialog.hint.get_visible(), "un show_all() successivo non deve far ricomparire la barra"
    headers = [row.get_header().get_text() for row in dialog.listbox.get_children() if row.get_header() is not None]
    assert headers == ["STUDIO ROSSI", "SENZA GRUPPO"], headers
    reopened = app.PasswordVault(window.vault.path)
    reopened.unlock("master-di-prova")
    assert {c.name: c.group for c in reopened.credentials}["Administrator Rossi"] == "Studio Rossi", "gruppi non salvati"

    opened_editor = []
    original_editor_run = app.CredentialDialog.run
    app.CredentialDialog.run = lambda self: (opened_editor.append(1), Gtk.ResponseType.CANCEL)[1]
    try:
        dialog.select_credential(rossi_admin.id)
        pump()
        dialog._rows[rossi_admin.id].emit("activate")
        pump()
    finally:
        app.CredentialDialog.run = original_editor_run
    assert not opened_editor, "scegliere una credenziale non deve aprire la modifica"
    assert dialog.stack.get_visible_child_name() == "detail"
    assert dialog.d_title.get_text() == "Administrator Rossi"
    assert dialog.d_password.get_text() == "•" * 12, "la password si vede solo chiedendolo"
    dialog.reveal.set_active(True)
    assert dialog.d_password.get_text() == "Segreta-Rossi-1"
    dialog.select_credential(rossi_fw.id)
    pump()
    assert not dialog.reveal.get_active() and dialog.d_password.get_text() == "•" * 12, \
        "cambiando credenziale la password torna nascosta"
    assert not dialog.d_rows["domain"][0].get_visible(), "un campo vuoto non occupa spazio"

    dialog.search.set_text("rossi admin")
    pump(0.4)
    names = [window.vault.get(row.cred_id).name for row in dialog.listbox.get_children()]
    assert names == ["Administrator Rossi", "firewall Rossi"], names
    dialog.search.set_text("nessuna-corrispondenza")
    pump(0.4)
    assert not dialog.listbox.get_children() and "nessuna-corrispondenza" in dialog.no_match.get_text()
    dialog.search.set_text("")
    pump(0.4)

    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    clipboard.set_text("prima", -1)
    dialog.select_credential(rossi_admin.id)
    pump()
    dialog.search.grab_focus()
    pump()
    assert not dialog.on_key_press(dialog, key_event(dialog, Gdk.KEY_c)), "Ctrl+C nella ricerca copia il testo cercato"
    dialog._rows[rossi_admin.id].grab_focus()
    pump()
    assert dialog.on_key_press(dialog, key_event(dialog, Gdk.KEY_c))
    assert clipboard.wait_for_text() == "Segreta-Rossi-1"
    assert dialog.on_key_press(dialog, key_event(dialog, Gdk.KEY_b))
    assert clipboard.wait_for_text() == "ROSSI\\Administrator", "per gli account di dominio si copia DOMINIO\\utente"

    editor = app.CredentialDialog(dialog, groups=window.vault.groups())
    assert not editor.save_button.get_sensitive(), "senza nome Salva resta disattivo"
    editor.name.set_text("Nuova")
    assert editor.save_button.get_sensitive()
    assert not editor.server_hint.get_visible(), "nessun avviso per una credenziale nuova"
    editor.destroy()
    editor = app.CredentialDialog(dialog, rossi_admin, groups=window.vault.groups())
    editor.on_generate(None)
    assert editor.password.get_visibility() and len(editor.password.get_text()) == 20
    assert editor.server_hint.get_visible(), "cambiando una password salvata si ricorda di cambiarla sul server"
    assert editor.get_credential().group == "Studio Rossi" and editor.get_credential().id == rossi_admin.id
    editor.destroy()

    window.vault.lock()
    pump(2.5)
    assert dialog._watch == 0, "bloccato l'archivio, la finestra si chiude da sola"
    dialog.destroy()
    window.vault.unlock("master-di-prova")
    print("  finestra Password: pagine, gruppi suggeriti e salvati, ricerca, uso, copia, blocco")


class ScriptedRun:
    """Sostituisce Gtk.Dialog.run: a ogni giro scrive la password successiva e preme OK."""

    def __init__(self, passwords):
        self.passwords = list(passwords)
        self.errors = []

    def __call__(self, dialog):
        if dialog.error.get_visible():
            self.errors.append(dialog.error.get_text())
        if not self.passwords:
            return Gtk.ResponseType.CANCEL
        dialog.password.set_text(self.passwords.pop(0))
        return Gtk.ResponseType.OK


def check_unlock_retry(app, window) -> None:
    window.vault.lock()
    scripted = ScriptedRun(["sbagliata-1", "master-di-prova"])
    original = app.MasterPasswordDialog.run
    app.MasterPasswordDialog.run = lambda self: scripted(self)
    try:
        assert window.ensure_vault_unlocked()
    finally:
        app.MasterPasswordDialog.run = original
    assert window.vault.unlocked
    assert scripted.errors == ["Password errata. Riprova."], scripted.errors

    window.vault.lock()
    scripted = ScriptedRun(["sbagliata-1"])
    app.MasterPasswordDialog.run = lambda self: scripted(self)
    try:
        assert not window.ensure_vault_unlocked(), "annullare dopo un errore lascia l'archivio bloccato"
    finally:
        app.MasterPasswordDialog.run = original
    assert not window.vault.unlocked

    dialog = app.MasterPasswordDialog(window, mode="create")
    dialog.password.set_text("corta")
    dialog.confirm.set_text("corta")
    assert "8 caratteri" in (dialog.local_error() or "")
    dialog.password.set_text("abbastanza-lunga")
    dialog.confirm.set_text("diversa-lunga")
    assert dialog.local_error() == "Le due password non coincidono."
    dialog.confirm.set_text("abbastanza-lunga")
    assert dialog.local_error() is None
    dialog.destroy()
    window.vault.unlock("master-di-prova")
    print("  master password: errore nella finestra, nuovo tentativo, regole di creazione")


def check_rdp_save_group(app, window) -> None:
    session = window.store.home_node().children[0].children[1].children[0]  # ops-bianchi
    assert window.save_rdp_password_to_vault(session, "pw-rdp", ask=False)
    cred = window.vault.get(session.credential_id)
    assert cred is not None and cred.group == "Officine Bianchi", cred
    print("  password RDP salvata dalla sessione: gruppo = cartella della sessione")


def main() -> int:
    if Gdk.Display.get_default() is None:
        print("VAULT_UI_SKIP: nessun display GTK")
        return 0
    with tempfile.TemporaryDirectory() as temp_dir:
        tmp = Path(temp_dir)
        os.environ["XDG_CONFIG_HOME"] = str(tmp / "config")
        os.environ["XDG_CACHE_HOME"] = str(tmp / "cache")
        config_dir = tmp / "config" / "fedtermhub"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "settings.json").write_text(
            json.dumps({"startup_mode": "empty", "theme": "dark", "show_error_popups": False}), encoding="utf-8"
        )
        import fedtermhub as app  # noqa: PLC0415

        app.KDF_ITERATIONS = 2_000  # la robustezza del KDF non e' oggetto di questo test
        check_model(app, tmp)
        check_generator()
        window = build_window(app)
        check_manager(app, window)
        check_unlock_retry(app, window)
        check_rdp_save_group(app, window)
        window.destroy()
    print("VAULT_UI_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
