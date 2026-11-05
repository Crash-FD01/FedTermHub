; SPDX-FileCopyrightText: 2026 Crash
; SPDX-License-Identifier: GPL-3.0-or-later
;
; Programma di installazione di FedTermHub per Windows (Inno Setup 6).
; Si installa per l'utente, senza permessi di amministratore, in %LOCALAPPDATA%\Programs\FedTermHub (oppure,
; se lo scegli nella prima schermata, per tutti gli utenti). Sessioni, impostazioni e archivio delle password
; restano in %APPDATA%\FedTermHub: aggiornare o disinstallare non li tocca.
;   iscc /DMyAppVersion=0.10.0 packaging\windows\fedtermhub.iss

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{48A70B0B-EF64-410D-905F-87CFA523DAEE}
AppName=FedTermHub
AppVersion={#MyAppVersion}
AppVerName=FedTermHub {#MyAppVersion}
AppPublisher=Crash
AppPublisherURL=https://github.com/Crash-FD01/FedTermHub
AppSupportURL=https://github.com/Crash-FD01/FedTermHub/issues
AppUpdatesURL=https://github.com/Crash-FD01/FedTermHub/releases
AppCopyright=Copyright (C) 2026 Crash
VersionInfoVersion={#MyAppVersion}
DefaultDirName={autopf}\FedTermHub
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist\packages
OutputBaseFilename=FedTermHub-{#MyAppVersion}-Setup
SetupIconFile=..\..\build\windows\fedtermhub.ico
UninstallDisplayIcon={app}\FedTermHub.exe
UninstallDisplayName=FedTermHub
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 1809: serve ConPTY per il terminale e il client OpenSSH di sistema
MinVersion=10.0.17763
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; aggiornamento: via i file della versione prima, così non restano librerie vecchie
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\..\dist\windows\FedTermHub\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\FedTermHub"; Filename: "{app}\FedTermHub.exe"; Comment: "Sessioni SSH, desktop remoto e password in un'unica finestra"
Name: "{autodesktop}\FedTermHub"; Filename: "{app}\FedTermHub.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\FedTermHub.exe"; Description: "{cm:LaunchProgram,FedTermHub}"; Flags: nowait postinstall skipifsilent
; Aggiornamento avviato dal programma (/SILENT /RESTART=1): alla fine FedTermHub si riapre da solo.
Filename: "{app}\FedTermHub.exe"; Flags: nowait runasoriginaluser; Check: RestartAfterUpdate

[Code]
function RestartAfterUpdate: Boolean;
begin
  Result := ExpandConstant('{param:RESTART|0}') = '1';
end;
