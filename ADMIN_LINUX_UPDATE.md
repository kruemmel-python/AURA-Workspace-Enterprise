# AURA – zentrale Linux-Administration (Patch)

Der Patch erweitert den ursprünglichen Read-only-Admin-Bereich. Neue Funktionen:

- Serverseitiger Rollen-Katalog (`GET/PUT/DELETE /api/v1/admin/roles`)
- Serverseitig verwaltete Konten (`GET/POST/PUT/DELETE /api/v1/admin/managed-accounts`)
- Zugangstoken-Erneuerung (`POST /api/v1/admin/managed-accounts/rotate`)
- SQLite Online-Snapshot (`POST /api/v1/admin/backup`)
- Zentrales Ereignis-Audit (bereits vorhandener Endpunkt)
- Offline-Wiederherstellungsprogramm unter `tools/restore-central-offline.py`

## Vor dem Start

Der Go-Server **muss neu kompiliert und neu gestartet** werden. Die aktualisierte GTK-Oberfläche muss ebenfalls neu gestartet werden. Den bestehenden Datenbestand nicht löschen. Für die zusätzliche zentrale Konfiguration zwei neue Umgebungsvariablen setzen, z.B.:

```bash
export AURA_MANAGED_IDENTITIES_FILE="$PWD/KR_BUILD/server-data/managed-identities.json"
export AURA_CENTRAL_BACKUP_DIR="$PWD/KR_BUILD/server-data/backups"
```

Bei laufendem Server neue Umgebungsvariablen werden erst nach dessen Neustart wirksam. `AURA_SYNC_TOKEN` bleibt weiterhin das vorhandene Root-Bootstrap-Token. Bestehende `AURA_SYNC_IDENTITIES`-Konten bleiben unverändert. Neue Konten verwaltet AURA gesondert; ihre 64-stelligen Bearer-Tokens werden **nur bei Anlage/Erneuerung** angezeigt und danach ausschließlich in der privaten Datei gespeichert (Dateirechte 0600). Diese Datei gehört in die Backup-Strategie und nie in ein Repository.

Für administrative Schreibzugriffe ist HTTPS erforderlich; für einen vollständig lokalen Test erlaubt der Server Loopback-Zugriffe über HTTP. Außerhalb des lokalen Geräts kein HTTP einsetzen.

## Zentrale Wiederherstellung

Eine laufende zentrale SQLite-Datenbank wird **nicht** aus der Oberfläche ersetzt. Server zunächst sauber beenden; dann z.B.:

```bash
python3 tools/restore-central-offline.py \
  --database "$PWD/KR_BUILD/server-data/aura-central.db" \
  --backup "$PWD/KR_BUILD/server-data/backups/aura-central-YYYYMMDD-HHMMSS.sqlite" \
  --confirm-server-stopped
```

Das Werkzeug prüft SQLite-Integrität, die erwarteten Tabellen und erstellt zusätzlich eine Sicherung des bisherigen Standes. Während der Wiederherstellung darf **kein** Prozess auf die Datenbank zugreifen. Externe Anhangdateien und die JSON-Identitätsdatei sind gesondert zu sichern und wiederherzustellen. Die zentrale Wiederherstellung ist nicht mit der lokalen Windows-/Linux-Datenbankwiederherstellung gleichzusetzen.

## Einschränkungen

Neue Serverkonten sind **Token-Identitäten**, keine passwortbasierten interaktiven Konten. Die bisherige lokale Konto-/Passwortverwaltung ist unabhängig. Die Serverrollen gelten für neue verwaltete Identitäten; vorhandene über Umgebungsvariablen definierte Identitäten werden nicht verändert. Das Ereignis-Audit enthält die letzten 200 zentralen Ereignisse, ist also kein vollständiger forensischer Audit-Export. Die Wiederherstellung ist ein Offline-Wartungsvorgang.
