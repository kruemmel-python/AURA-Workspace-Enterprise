# AURA Workspace Enterprise

AURA Workspace Enterprise ist ein natives Office-Management-System für Unternehmen. Es verbindet Raum- und Ressourcenmanagement, Projekte und Aufgaben, Organisation und Personal, Helpdesk/Facility sowie eine persistente, rollenbasierte Mehrbenutzer-Synchronisation in einer installierbaren Windows-, Linux- und Android-Auslieferung.

> Der Produktbuild verwendet die KR-Compiler-Toolchain und legt Build-Ausgaben, Caches und temporäre Dateien ausschließlich innerhalb des Arbeitsbereichs ab.

## Funktionsbereiche

- **Räume & Ressourcen:** Stammdaten, Suche, Ausstattung, Kapazität, Archivierung, Sperrzeiten, Einzel- und Serientermine, Umbuchung, Stornierung, Konflikt- und Rechteprüfung sowie Audit-Historie.
- **Projekte & Aufgaben:** Projekt-CRUD, Status, Zuständigkeiten, Fälligkeiten, Wiederholungen, Unteraufgaben, Abhängigkeiten, Kommentare, Freigaben und revisionsfähige Archivierung.
- **Mitarbeiter & Organisation:** Mitarbeiter, Rollen, Organisationseinheiten, Präsenz und rollenbasierte Fachberechtigungen.
- **Helpdesk & Facility:** Tickets, Zustandswechsel, Zuweisungen, Kommentare, Facility-Assets und Wartungsaufträge.
- **Administration:** Konten, Passwortwechsel, Rollenkatalog, Audit, Backup und Wiederherstellung.
- **Synchronisation:** zentraler Ereignisstrom, persistente Outbox, Cursor-Feed, serverseitige Konfliktentscheidung für Buchungen, stabile Objektzuordnungen und zentraler SHA-256-Objektspeicher für Anhänge.

Die vollständige Nachweisübersicht steht in [docs/function-matrix.md](docs/function-matrix.md). Die Betriebsanleitung für Administratoren und Servicetechniker steht in [docs/administrator-handbook.md](docs/administrator-handbook.md).

## Auslieferungsartefakte

`build-enterprise.ps1 -SmokeTest` erzeugt unter `KR_BUILD`:

| Plattform | Artefakt |
| --- | --- |
| Windows x64 | `windows-x64/AURA-Workspace-Enterprise.exe` einschließlich aller benötigten DLLs und Synchronisationsserver |
| Linux x64 | `linux-x64/aura-workspace-enterprise` (ELF) |
| Android | `android-arm64/aura-workspace-mobile.apk` – universelle Java/Android-Touch-App für API 23 bis 35 |
| Integrität | `release-manifest.json` mit SHA-256-Prüfsummen |

Die mobile Touch-App unterstützt ein privates HTTPS-Verbindungsprofil, zentrale Ansichten sowie Projekte, Aufgaben, Tickets, Räume und Urlaubsanträge.

## Build

Voraussetzung ist die konfigurierte KR-Toolchain unter `H:\KR-Compiler\KR-Compiler-Windows-x64-v4.3.1\toolchain`.

```powershell
Set-Location H:\Aurora_Workspace
.\build-enterprise.ps1 -SmokeTest
```

Der Gesamtbuild prüft die KR-Projektpipeline, Rust-Geschäftsregeln, Enterprise-Datenbanktests, Go-Synchronisationsdienste, Windows-Laufzeit, Linux-ELF und die Android-Signaturen. Alle Build-Caches und temporären Ausgaben bleiben in `H:\Aurora_Workspace`.

## Zentraler Betrieb

Der Server wird mit einem Journal, einer zentralen SQLite-Datenbank, Identitäten und TLS betrieben. Konfigurations- und Sicherheitsdetails enthält [docs/sync-server.md](docs/sync-server.md).

```powershell
$env:AURA_SYNC_JOURNAL = 'H:\Aurora_Workspace\KR_BUILD\central-sync-events.ndjson'
$env:AURA_CENTRAL_DB = 'H:\Aurora_Workspace\KR_BUILD\aura-central.db'
$env:AURA_SYNC_TOKEN = '<separat-vergebener-geheimwert>'
$env:AURA_SYNC_LISTEN = '0.0.0.0:8787'
$env:AURA_SYNC_TLS_CERT = 'H:\Aurora_Workspace\KR_BUILD\certs\aura-server.crt'
$env:AURA_SYNC_TLS_KEY = 'H:\Aurora_Workspace\KR_BUILD\certs\aura-server.key'
$env:AURA_SYNC_REQUIRE_TLS = '1'
& 'H:\Aurora_Workspace\KR_BUILD\windows-x64\aura-sync-server.exe' --server
```

Keine Tokens, Zertifikate, Datenbanken, Anhänge oder erzeugten Caches gehören in ein Quellcode-Repository.

## Sicherheit und Betrieb

- Rollen und Fachberechtigungen werden auf Client und Server geprüft.
- Zentraler Betrieb verwendet TLS und tokengebundene Identitäten.
- Anhänge sind auf 100 MB begrenzt und werden mit SHA-256 geprüft.
- Audit-Ereignisse und Fachobjekte werden persistent in SQLite abgelegt.
- Der Windows-Build verpackt Runtime-DLLs automatisch zusammen mit der EXE.

## Lizenz

Proprietär. Alle Rechte vorbehalten.
