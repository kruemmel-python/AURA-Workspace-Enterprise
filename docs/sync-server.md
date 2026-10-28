# Zentraler Synchronisationsserver

Der Build erzeugt `KR_BUILD\windows-x64\aura-sync-server.exe`. Der Dienst benötigt Journal und Server-Token; die zentrale SQLite-Datenbank kann ausdrücklich gesetzt werden und liegt sonst neben dem Journal.

Beim ersten Start des Windows-Clients muss der Betreiber einmalig ein Bootstrap-Passwort mit mindestens 12 Zeichen setzen; es wird nicht im Quellcode hinterlegt:

```powershell
$env:AURA_BOOTSTRAP_PASSWORD = 'ein-langes-einmaliges-startpasswort'
& 'H:\Aurora_Workspace\KR_BUILD\windows-x64\AURA-Workspace-Enterprise.exe'
```

Beim ersten Anmelden fordert der Client den Passwortwechsel zwingend an. Danach die Umgebungsvariable entfernen.

```powershell
$env:AURA_SYNC_JOURNAL = 'H:\Aurora_Workspace\KR_BUILD\central-sync-events.ndjson'
$env:AURA_CENTRAL_DB = 'H:\Aurora_Workspace\KR_BUILD\aura-central.db'
$env:AURA_SYNC_TOKEN = 'einen-langen-zufaelligen-geheimwert-vergeben'
$env:AURA_SYNC_IDENTITIES = '{"android-mitarbeiter-token":{"employeeId":42,"permissions":["leave.request","rooms.book","projects.read"]},"windows-manager-token":{"employeeId":7,"permissions":["leave.approve","rooms.manage","tasks.manage","tickets.manage","projects.manage"]}}'
$env:AURA_SYNC_REQUIRE_IDENTITIES = '1'
$env:AURA_SYNC_LISTEN = '0.0.0.0:8787'
& 'H:\Aurora_Workspace\KR_BUILD\windows-x64\aura-sync-server.exe' --server
```

Für einen produktiven Netzbetrieb muss TLS aktiviert werden. Der Dienst verwendet dann ohne weitere Änderungen am Client `https://…`:

```powershell
$env:AURA_SYNC_TLS_CERT = 'H:\Aurora_Workspace\KR_BUILD\certs\aura-server.crt'
$env:AURA_SYNC_TLS_KEY = 'H:\Aurora_Workspace\KR_BUILD\certs\aura-server.key'
$env:AURA_SYNC_REQUIRE_TLS = '1'
```

Zertifikat und Schlüssel müssen immer gemeinsam gesetzt sein. `AURA_SYNC_REQUIRE_TLS=1` verhindert einen versehentlichen HTTP-Start. Jede Serverantwort trägt zusätzlich `Cache-Control: no-store` und `X-Content-Type-Options: nosniff`.

`AURA_SYNC_REQUIRE_IDENTITIES=1` verlangt eine gültige `AURA_SYNC_IDENTITIES`-Zuordnung beim Start. Eine gesetzte, aber fehlerhafte Zuordnung wird immer abgewiesen; sie fällt niemals auf das globale Legacy-Token zurück.

Der Dienst stellt bereit:

- `GET /health` für Betriebsüberwachung.
- `POST /events` für token-geschützte Ereignisannahme.
- `POST /bookings/check` für die token-geschützte Vorabprüfung einer Raum- und Zeitspanne.
- `POST /bookings/reserve`, `/bookings/commit` und `/bookings/release` für eine kurzlebige, atomare Buchungsreservierung mit verbindlichem Commit oder Rollback.
- `GET /api/v1/sync/changes?cursor=<n>` für den token-geschützten, monotonen Änderungsstrom. Die Antwort enthält `events` und `nextCursor`; Clients speichern nur `nextCursor` in ihrem lokalen SQLite-Cache und können bei Unterbrechung sicher fortsetzen.
- `POST /api/v1/leave-requests`, `GET /api/v1/leave-requests` und `POST /api/v1/leave-requests/{id}/decision` für zentral persistierte Urlaubsanträge. Die Mitarbeiter-ID stammt ausschließlich aus der serverseitigen Token-Zuordnung; Selbstfreigaben und Mehrfachentscheidungen werden serverseitig abgewiesen.
- `POST /api/v1/tasks`, `GET /api/v1/tasks` für zentral angelegte Aufgaben. `tasks.manage` ist für die Anlage erforderlich; Mitarbeitende mit `tasks.read` sehen nur selbst erstellte oder zugewiesene Einträge.
- `POST /api/v1/tasks/{id}/status` für serverseitig geprüfte Aufgabenübergänge. Zulässig sind `open → in_progress/done/cancelled`, `in_progress → done/cancelled/open`, `done → open/in_progress` und `cancelled → open`. Beim Abschluss werden weiterhin Voraussetzungen und Unteraufgaben geprüft.
- `GET /api/v1/projects`, `POST /api/v1/projects`, `PUT /api/v1/projects/{id}` und `POST /api/v1/projects/{id}/archive` für zentral persistierte Projekte mit Fachbereich, Beschreibung, Eigentümer, Status und revisionsfähiger Archivierung. Schreibzugriffe verlangen `projects.manage`; `projects.read` begrenzt die Sicht auf eigene Projekte.
- `POST /api/v1/tickets`, `GET /api/v1/tickets` für zentrale Helpdesk-Tickets. `tickets.manage` steuert die Anlage und vollständige Sicht; die Mitarbeitersicht ist auf eigene bzw. zugewiesene Tickets begrenzt.
- `POST /api/v1/tickets/{id}/status` für zentral geprüfte Übergänge zwischen offen, in Bearbeitung, gelöst und geschlossen.
- `GET /api/v1/rooms`, `POST /api/v1/rooms`, `PUT /api/v1/rooms/{id}` und `POST /api/v1/rooms/{id}/archive` für zentral verwaltete Räume einschließlich Standort, Kapazität, Ausstattung und revisionsfähiger Archivierung. Schreibzugriffe verlangen `rooms.manage`; `includeArchived=true` ist ausschließlich für diese Rolle verfügbar.
- `PUT /api/v1/attachments/{sha256}` und `GET /api/v1/attachments/{sha256}` für zentralen Binäranhangstransport. Der Server akzeptiert maximal 100 MB, prüft den als Pfad und Header angegebenen SHA-256-Wert vor der atomaren Ablage und liefert nur verifizierte Inhalte aus. Das Windows-Programm veröffentlicht zugehörige Anhangsmetadaten erst nach bestätigter Objektablage; andere Clients legen diese Metadaten lokal ab und laden das Objekt beim Öffnen atomar mit erneuter SHA-256-Prüfung nach.
- Serverseitige Prüfung überlappender Raumbuchungen mit `409 Conflict`.
- Wiederherstellung des Buchungsregisters und aller API-Projektionen aus der zentralen SQLite-Ereignishistorie nach Neustart.

Beim Serverstart wird eine zentrale SQLite-Datenbank migriert (`AURA_CENTRAL_DB`; ohne Vorgabe `<Journal>.sqlite`). Jedes vom Server angenommene Ereignis wird transaktional in `central_events` abgelegt. Diese Datenbank ist die führende Quelle für Serverneustarts und den Cursor-Änderungsfeed. Das NDJSON-Journal ist ausschließlich eine Kompatibilitätskopie für ältere/offline arbeitende Clients.

Fällt diese Kompatibilitätskopie aus, bleibt ein zuvor erfolgreich in SQLite gespeichertes Serverereignis gültig und abrufbar; der zentrale Dienst meldet es nicht fälschlich als verlorene Änderung.

`POST /events` prüft die Fachberechtigung zusätzlich zum Token: Buchungsereignisse verlangen `rooms.book`, Raumstammdaten `rooms.manage`, Aufgaben und Freigaben `tasks.manage`, Projekte `projects.manage`, Tickets `tickets.manage`, Facility-Stammdaten `facility.manage`. Kommentare dürfen durch Aufgaben-/Ticketverantwortliche oder mit `sync.events.write` übertragen werden. Die Windows-App sendet Raum-, Aufgaben-, Projekt-, Ticket-, Freigabe- und Kommentardaten mit einer installationsgebundenen Quell-ID; diese Änderungen werden zentral projiziert und bleiben bei Netzstörungen bis zur bestätigten Übertragung in der Outbox.

Für einen Client werden dieselben Serverdaten gesetzt:

```powershell
$env:AURA_SYNC_URL = 'https://server-name:8787'
$env:AURA_SYNC_TOKEN = 'derselbe-geheimwert'
$env:AURA_SYNC_CA_CERT = 'H:\Pfad\zur\unternehmens-ca.pem' # nur bei privater Unternehmens-CA
```

Bei gesetzter `AURA_SYNC_URL` reserviert der Client vor jeder Buchung, Umbuchung und jedem Serientermin den Zeitraum zentral. Erst nach erfolgreicher lokaler SQLite-Transaktion wird die Reservierung serverseitig als Buchung festgeschrieben. Schlägt eine Seite fehl, wird die Reservierung freigegeben beziehungsweise die lokale Änderung kompensiert. Ein `409 Conflict` verhindert die lokale Speicherung und wird in der Oberfläche angezeigt. Reservierungen laufen serverseitig nach 90 Sekunden ab, falls ein Client abstürzt. Ohne `AURA_SYNC_URL` arbeitet der Client weiter offline und persistiert Ereignisse lokal unter `KR_BUILD\sync-outbox.ndjson`. Bei einer privaten Unternehmens-CA setzt der Windows- oder Linux-Client zusätzlich `AURA_SYNC_CA_CERT` auf die PEM-Datei; die TLS-Prüfung bleibt dabei vollständig aktiv.

Ist `AURA_SYNC_URL` gesetzt, aber der Server nicht erreichbar oder antwortet fehlerhaft, wird das Ereignis ebenfalls zunächst in `KR_BUILD\sync-outbox.ndjson` abgelegt. Damit geht eine lokale Änderung bei vorübergehender Netzstörung nicht verloren.

Bereits erfolgreich zentral bestätigte Ereignisse werden nicht in diese Replay-Outbox geschrieben und daher nicht doppelt übertragen.

Beim nächsten Start mit erreichbarem `AURA_SYNC_URL` spielt der Windows-Client diese Outbox automatisch in ursprünglicher Reihenfolge ab. Falls ein Ereignis noch nicht angenommen wird, bleibt es samt allen folgenden Ereignissen in der Datei, sodass die fachliche Reihenfolge erhalten bleibt.

Beim ersten Clientstart erzeugt AURA zusätzlich eine kryptografisch zufällige, dauerhafte Instanz-ID unter `KR_BUILD\sync-client-instance.id`. Sie wird bei zentralen Buchungen übertragen, damit gleiche lokale SQLite-Buchungsnummern unterschiedlicher Geräte niemals als dieselbe Buchung behandelt werden. Diese Datei gehört zur Clientinstallation und darf beim Betrieb nicht kopiert oder zwischen Geräten geteilt werden.

Bei aktivem `AURA_SYNC_URL` ruft der Windows-Client beim Start außerdem den zentralen Cursor-Feed ab. Jeder erfolgreich gelesene Batch wird zunächst fachlich übernommen und danach nach `KR_BUILD\sync-inbox.ndjson` geschrieben; erst anschließend wird `KR_BUILD\sync-cursor.txt` fortgeschrieben. So wird bei einem Prozessabbruch kein bestätigter Änderungsbatch übersprungen. Raum-, Projekt-, Aufgaben-, Ticket-, Freigabe-, Kommentar-, Mitarbeiter-, Organisations-, Abwesenheits- und Facility-Ereignisse werden direkt in die lokale SQLite-Projektion übernommen. Dazu speichert der Client eine dauerhafte Zuordnung aus zentraler Objekt-ID und lokaler Objekt-ID, wodurch auch Umbenennungen, Statuswechsel und Archivierungen anderer Geräte denselben lokalen Datensatz verändern. Bei vorhandenen lokalen Daten werden fehlende Zuordnungen einmalig angelegt und die Objekte in fachlicher Reihenfolge veröffentlicht.

Der Änderungsfeed ist die gemeinsame Transportbasis für Windows-, Linux- und Android-Clients. Der Windows-Client übernimmt die aufgeführten Fachprojektionen periodisch und nutzt die verbindliche zentrale Buchungsprüfung. Linux wird als portabler Kommando-Client ausgeliefert. Die zusätzliche Android-Touch-App `aura-workspace-mobile.apk` verwaltet ihr Verbindungsprofil im privaten App-Speicher und bietet Synchronisationsansicht, Aufgabenanlage, Ticketanlage und -status, Raum-Anlage/-Änderung/-Archivierung sowie Urlaubsanträge. Sie akzeptiert aus Sicherheitsgründen ausschließlich HTTPS; der Server muss deshalb mit TLS betrieben werden.

Für Linux- und Android-Adapter steht zusätzlich das reine Go-Paket `services/portable` bereit. Es kapselt Bearer-Authentifizierung sowie Urlaub, Aufgaben, Projekte, Tickets, Räume, Facility-Assets und Cursor-Feed ohne Win32- oder cgo-Abhängigkeiten. `aura-portable` unterstützt zudem das Erzeugen von Kommentaren und Aufgabenfreigaben sowie ein kontrolliertes `publish-event` für weitere zentral validierte Ereignisse. Es wird im Produktbuild mit `go test ./...` geprüft.

## Plattform-Artefakte

`build-enterprise.ps1 -SmokeTest` ist der gemeinsame Release-Einstieg. Er erzeugt den getesteten Windows-Ordner, `KR_BUILD\linux-x64\aura-workspace-enterprise` als Linux-x64-ELF sowie `KR_BUILD\android-arm64\aura-workspace-mobile.apk` als signierte, universelle Android-Touch-App für API 23 bis 35. Der interne KR-Runner wird nicht als Endkunden-Artefakt ausgeliefert. Alle Build-Caches, temporären Dateien und das lokale Android-Signaturmaterial liegen ausschließlich innerhalb von `H:\Aurora_Workspace`.
