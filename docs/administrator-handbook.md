# AURA Workspace Enterprise – Administratorhandbuch

Dieses Dokument beschreibt den sicheren Linux-HTTPS-Betrieb sowie die Verbindung von Windows-, Linux- und Android-Clients.

## Linux-Server

Voraussetzungen: Ubuntu/Debian 22.04+, fester DNS-Name, TCP 8787, Go, GCC, SQLite-Entwicklungspaket und ein vertrauenswürdiges TLS-Zertifikat.

```bash
sudo apt update
sudo apt install -y golang-go build-essential libsqlite3-dev openssl ca-certificates
sudo useradd --system --home /opt/aura --shell /usr/sbin/nologin aura
sudo install -d -o aura -g aura -m 750 /opt/aura /var/lib/aura /etc/aura /var/log/aura
sudo -u aura git clone https://github.com/kruemmel-python/AURA-Workspace-Enterprise.git /opt/aura/src
cd /opt/aura/src/services
sudo -u aura env CGO_ENABLED=1 go build -trimpath -o /opt/aura/aura-sync-server sync.go
```

Der zentrale Server nutzt SQLite über cgo und wird deshalb auf Linux nativ gebaut. Der portable Linux-Client aus dem Release benötigt diesen Build nicht.

## TLS und Identitäten

Legen Sie Zertifikat und Schlüssel unter `/etc/aura/tls/server.crt` und `/etc/aura/tls/server.key` ab. Der Schlüssel muss ausschließlich für den Dienstbenutzer lesbar sein.

```bash
sudo install -d -o aura -g aura -m 750 /etc/aura/tls
sudo chown -R aura:aura /etc/aura/tls
sudo chmod 640 /etc/aura/tls/server.key
```

Jeder Benutzer erhält einen eigenen Bearer-Token. Der Server ordnet den Token fest einer Mitarbeiter-ID und minimalen Fachrechten zu. Mitarbeitende erhalten beispielsweise `leave.request`; Führungskräfte zusätzlich `leave.approve`. Tokens gehören nicht in Git oder Screenshots.

## Dienst

`/etc/aura/aura.env`:

```ini
AURA_SYNC_JOURNAL=/var/lib/aura/central-sync-events.ndjson
AURA_CENTRAL_DB=/var/lib/aura/aura-central.sqlite
AURA_SYNC_TOKEN=not-used-when-identities-are-required
AURA_SYNC_REQUIRE_IDENTITIES=1
AURA_SYNC_LISTEN=0.0.0.0:8787
AURA_SYNC_TLS_CERT=/etc/aura/tls/server.crt
AURA_SYNC_TLS_KEY=/etc/aura/tls/server.key
AURA_SYNC_REQUIRE_TLS=1
AURA_SYNC_IDENTITIES={"mitarbeiter-token":{"employeeId":42,"permissions":["leave.request","tasks.read","tickets.read","projects.read","rooms.read"]},"manager-token":{"employeeId":7,"permissions":["leave.request","leave.approve","rooms.manage","rooms.read","tasks.manage","tasks.read","tickets.manage","tickets.read","projects.manage","projects.read","employees.manage","facility.manage"]}}
```

Sichern Sie die Datei mit `root:aura` und Modus `640`. Der vollständige systemd-Dienst ist im ausgelieferten PDF/DOCX-Handbuch enthalten.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aura-sync
sudo ufw allow 8787/tcp
sudo systemctl status aura-sync --no-pager
curl --fail https://aura.example.com:8787/health
```

## Clients

Windows und Linux erhalten je Benutzer:

```text
AURA_SYNC_URL=https://aura.example.com:8787
AURA_SYNC_TOKEN=<persönlicher-token>
```

In der Android-App: **Zentrale konfigurieren** öffnen, dieselbe HTTPS-Adresse und den persönlichen Token eintragen, Profil speichern und synchronisieren.

## Abnahme

- `/health` antwortet über HTTPS mit HTTP 200.
- Raum-, Projekt-, Aufgaben- und Ticketdaten sind nach Synchronisation auf einem zweiten Client sichtbar.
- Ein Android-Urlaubsantrag erscheint im Windows-Client unter **Administration → Abwesenheiten**; nach Genehmigung zeigt Android unter **Meine Urlaubsanträge** den Status **Genehmigt**.
- Überlappende Raumbuchungen werden zentral abgewiesen.
- Audit, Backup und Wiederherstellung werden vor Produktivsetzung geprüft.

## Betrieb und Fehleranalyse

```bash
sudo journalctl -u aura-sync -f
sudo systemctl stop aura-sync
sudo tar -C /var/lib/aura -czf /var/backups/aura-$(date +%F).tgz aura-central.sqlite central-sync-events.ndjson
sudo systemctl start aura-sync
```

Bei TLS-Fehlern DNS-Name, Zertifikatskette, Ablaufdatum und Firewall prüfen. Bei 401/403 Token, Mitarbeiterzuordnung und Rechte prüfen. Bei Synchronisationsproblemen Cursor-Feed und Client-Outbox prüfen.
