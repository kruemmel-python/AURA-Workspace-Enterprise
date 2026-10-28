#include "enterprise_database.h"

#ifdef _WIN32
#include <windows.h>
#include <bcrypt.h>
#else
#include <codecvt>
#include <locale>
#include <openssl/evp.h>
#include <openssl/rand.h>
#endif
#include <sqlite3.h>
#include <cstring>
#include <ctime>
#include <fstream>
#include <limits>
#include <set>

namespace {
std::string Utf8(const std::wstring& value) {
    if (value.empty()) return {};
#ifdef _WIN32
    const int count = WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    std::string result(static_cast<size_t>(count), '\0');
    if (count > 0) WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), result.data(), count, nullptr, nullptr);
    return result;
#else
    try { return std::wstring_convert<std::codecvt_utf8<wchar_t>>{}.to_bytes(value); }
    catch (const std::range_error&) { return {}; }
#endif
}
std::wstring Wide(const unsigned char* value) {
    if (!value) return L"";
    const char* text = reinterpret_cast<const char*>(value);
#ifdef _WIN32
    const int bytes = static_cast<int>(std::strlen(text));
    if (bytes == 0) return L"";
    const int count = MultiByteToWideChar(CP_UTF8, 0, text, bytes, nullptr, 0);
    std::wstring result(static_cast<size_t>(count), L'\0');
    if (count > 0) MultiByteToWideChar(CP_UTF8, 0, text, bytes, result.data(), count);
    return result;
#else
    try { return std::wstring_convert<std::codecvt_utf8<wchar_t>>{}.from_bytes(text); }
    catch (const std::range_error&) { return {}; }
#endif
}
bool SecureRandom(std::vector<unsigned char>& bytes) {
#ifdef _WIN32
    return BCryptGenRandom(nullptr, bytes.data(), static_cast<ULONG>(bytes.size()), BCRYPT_USE_SYSTEM_PREFERRED_RNG) == 0;
#else
    return RAND_bytes(bytes.data(), static_cast<int>(bytes.size())) == 1;
#endif
}
void BindText(sqlite3_stmt* statement, int index, const std::wstring& value) {
    const std::string utf8 = Utf8(value);
    sqlite3_bind_text(statement, index, utf8.c_str(), static_cast<int>(utf8.size()), SQLITE_TRANSIENT);
}
std::wstring Hex(const std::vector<unsigned char>& bytes) { static constexpr wchar_t symbols[]=L"0123456789abcdef"; std::wstring result; result.reserve(bytes.size()*2); for(unsigned char value:bytes){result+=symbols[value>>4];result+=symbols[value&15];} return result; }
bool PasswordDigest(const std::wstring& password, const std::wstring& saltHex, std::wstring& digest) {
    if (password.size() < 12 || saltHex.size() != 32) return false;
    std::vector<unsigned char> salt(16);
    auto hex = [](wchar_t c) -> int {
        return c >= L'0' && c <= L'9' ? c - L'0' :
               c >= L'a' && c <= L'f' ? c - L'a' + 10 :
               c >= L'A' && c <= L'F' ? c - L'A' + 10 : -1;
    };
    for (size_t i = 0; i < salt.size(); ++i) {
        const int high = hex(saltHex[i * 2]);
        const int low = hex(saltHex[i * 2 + 1]);
        if (high < 0 || low < 0) return false;
        salt[i] = static_cast<unsigned char>((high << 4) | low);
    }
    std::string utf8 = Utf8(password);
    std::vector<unsigned char> output(32);
#ifdef _WIN32
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    const bool opened = BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, BCRYPT_ALG_HANDLE_HMAC_FLAG) == 0;
    const bool ok = opened && BCryptDeriveKeyPBKDF2(algorithm, reinterpret_cast<PUCHAR>(utf8.data()), static_cast<ULONG>(utf8.size()), salt.data(), static_cast<ULONG>(salt.size()), 210000, output.data(), static_cast<ULONG>(output.size()), 0) == 0;
    if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
#else
    const bool ok = PKCS5_PBKDF2_HMAC(utf8.data(), static_cast<int>(utf8.size()), salt.data(), static_cast<int>(salt.size()), 210000, EVP_sha256(), static_cast<int>(output.size()), output.data()) == 1;
#endif
    if (!ok) return false;
    digest = Hex(output);
    return true;
}
bool ConstantTimeEquals(const std::wstring& left,const std::wstring& right){if(left.size()!=right.size())return false;unsigned int difference=0;for(size_t i=0;i<left.size();++i)difference|=static_cast<unsigned int>(left[i]^right[i]);return difference==0;}
}

EnterpriseDatabase::EnterpriseDatabase(const std::wstring& databasePath) {
    const std::string path = Utf8(databasePath);
    if (sqlite3_open_v2(path.c_str(), &db_, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nullptr) != SQLITE_OK) {
        setError("Datenbank konnte nicht geöffnet werden");
        return;
    }
    // Multiple client sessions may initialize or write the same workspace.
    // Let SQLite wait briefly for a writer instead of treating contention as a
    // failed migration and leaving a session unusable.
    sqlite3_busy_timeout(db_, 5000);
    execute("PRAGMA foreign_keys = ON;");
    execute("PRAGMA journal_mode = WAL;");
    migrate();
}

EnterpriseDatabase::~EnterpriseDatabase() { if (db_) sqlite3_close(db_); }
bool EnterpriseDatabase::ready() const { return db_ != nullptr && error_.empty(); }
const std::wstring& EnterpriseDatabase::error() const { return error_; }
void EnterpriseDatabase::setAuditActor(const std::wstring& actor) { auditActor_ = actor; }

void EnterpriseDatabase::setError(const char* context) {
    const char* detail = db_ ? sqlite3_errmsg(db_) : "unbekannter Fehler";
    error_ = Wide(reinterpret_cast<const unsigned char*>(context)) + L": " + Wide(reinterpret_cast<const unsigned char*>(detail));
}
bool EnterpriseDatabase::execute(const char* sql) {
    char* message = nullptr;
    if (sqlite3_exec(db_, sql, nullptr, nullptr, &message) == SQLITE_OK) return true;
    error_ = Wide(reinterpret_cast<const unsigned char*>(message ? message : "SQL-Fehler"));
    sqlite3_free(message);
    return false;
}

bool EnterpriseDatabase::migrate() {
    return execute(R"SQL(
        CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            location TEXT NOT NULL, capacity INTEGER NOT NULL CHECK(capacity > 0),
            equipment TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id), user_id INTEGER NOT NULL,
            starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL, attendees INTEGER NOT NULL CHECK(attendees > 0),
            status TEXT NOT NULL DEFAULT 'confirmed', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(ends_at > starts_at)
        );
        CREATE TABLE IF NOT EXISTS booking_block_details(booking_id INTEGER PRIMARY KEY REFERENCES bookings(id),reason TEXT NOT NULL,created_by INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS linux_booking_sync_actor(singleton INTEGER PRIMARY KEY CHECK(singleton=1), employee_id INTEGER NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO linux_booking_sync_actor VALUES(1,0);
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            actor TEXT NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_object_links (
            object_type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            local_id INTEGER NOT NULL,
            PRIMARY KEY(object_type, external_id),
            UNIQUE(object_type, local_id)
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, department TEXT NOT NULL,
            priority TEXT NOT NULL CHECK(priority IN ('Niedrig','Mittel','Hoch','Kritisch')),
            status TEXT NOT NULL CHECK(status IN ('open','in_progress','done','cancelled')) DEFAULT 'open',
            assignee_id INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            department TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('planned','active','on_hold','completed','cancelled')) DEFAULT 'planned',
            owner_id INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS project_tasks (
            project_id INTEGER NOT NULL REFERENCES projects(id), task_id INTEGER NOT NULL REFERENCES tasks(id),
            PRIMARY KEY(project_id, task_id)
        );
        CREATE TABLE IF NOT EXISTS task_dependencies (
            task_id INTEGER NOT NULL REFERENCES tasks(id),
            depends_on_task_id INTEGER NOT NULL REFERENCES tasks(id),
            PRIMARY KEY(task_id, depends_on_task_id),
            CHECK(task_id <> depends_on_task_id)
        );
        CREATE TABLE IF NOT EXISTS task_subtasks (
            id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
            title TEXT NOT NULL CHECK(length(trim(title)) > 0),
            status TEXT NOT NULL CHECK(status IN ('open','in_progress','done','cancelled')) DEFAULT 'open',
            assignee_id INTEGER NOT NULL DEFAULT 0,
            due_at INTEGER NOT NULL DEFAULT 0 CHECK(due_at >= 0),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sync_subtask_shadow (
            subtask_id INTEGER PRIMARY KEY REFERENCES task_subtasks(id), task_id INTEGER NOT NULL,
            title TEXT NOT NULL, status TEXT NOT NULL, assignee_id INTEGER NOT NULL,
            due_at INTEGER NOT NULL, active INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS desktop_subtask_sync_config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),client_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS desktop_subtask_sync_binding(singleton INTEGER PRIMARY KEY CHECK(singleton=1), server_url TEXT NOT NULL, server_actor INTEGER NOT NULL CHECK(server_actor>0));
        CREATE TABLE IF NOT EXISTS desktop_subtask_sync_transmissions(operation_id TEXT PRIMARY KEY, wire_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS desktop_subtask_sync_guard(singleton INTEGER PRIMARY KEY CHECK(singleton=1),suppressed INTEGER NOT NULL);
        INSERT OR IGNORE INTO desktop_subtask_sync_guard VALUES(1,0);
        CREATE TABLE IF NOT EXISTS desktop_subtask_sync_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL UNIQUE, action TEXT NOT NULL, subtask_id INTEGER NOT NULL, value_json TEXT NOT NULL, expected_json TEXT);
        CREATE TABLE IF NOT EXISTS task_schedules (
            task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
            due_at INTEGER NOT NULL DEFAULT 0 CHECK(due_at >= 0),
            recurrence_days INTEGER NOT NULL DEFAULT 0 CHECK(recurrence_days >= 0 AND recurrence_days <= 3660)
        );
        CREATE TABLE IF NOT EXISTS task_approvals (
            id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id), requested_by INTEGER NOT NULL,
            approver_id INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')) DEFAULT 'pending',
            decided_by INTEGER, requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, decided_at TEXT
        );
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL,
            priority TEXT NOT NULL CHECK(priority IN ('Niedrig','Mittel','Hoch','Kritisch')),
            status TEXT NOT NULL CHECK(status IN ('open','in_progress','resolved','closed')) DEFAULT 'open',
            assignee_id INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY,
            subject_type TEXT NOT NULL CHECK(subject_type IN ('ticket','task')),
            subject_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            body TEXT NOT NULL CHECK(length(trim(body)) > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY,
            subject_type TEXT NOT NULL CHECK(subject_type IN ('task','ticket')),
            subject_id INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            stored_path TEXT NOT NULL COLLATE NOCASE UNIQUE,
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            sha256 TEXT NOT NULL,
            uploaded_by INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS facility_assets (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            category TEXT NOT NULL, location TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('operational','maintenance','out_of_service')) DEFAULT 'operational',
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS maintenance_orders (
            id INTEGER PRIMARY KEY,
            asset_id INTEGER NOT NULL REFERENCES facility_assets(id),
            title TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('open','in_progress','completed','cancelled')) DEFAULT 'open',
            due_at INTEGER NOT NULL DEFAULT 0 CHECK(due_at >= 0),
            recurrence_days INTEGER NOT NULL DEFAULT 0 CHECK(recurrence_days >= 0 AND recurrence_days <= 3660),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE, role TEXT NOT NULL,
            department TEXT NOT NULL, presence TEXT NOT NULL DEFAULT 'office', active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS accounts (
            employee_id INTEGER PRIMARY KEY REFERENCES employees(id), username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_salt TEXT NOT NULL, password_digest TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until INTEGER NOT NULL DEFAULT 0,
            must_change_password INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS leave_requests (
            id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
            starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL, reason TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','cancelled')) DEFAULT 'pending',
            decided_by INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(ends_at > starts_at)
        );
        CREATE TABLE IF NOT EXISTS organization_units (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            code TEXT NOT NULL COLLATE NOCASE UNIQUE, manager_id INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS roles (
            id INTEGER PRIMARY KEY, code TEXT NOT NULL COLLATE NOCASE UNIQUE, name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS employee_roles (
            employee_id INTEGER NOT NULL REFERENCES employees(id), role_id INTEGER NOT NULL REFERENCES roles(id),
            PRIMARY KEY(employee_id, role_id)
        );
        CREATE TABLE IF NOT EXISTS role_permissions (
            role_id INTEGER NOT NULL REFERENCES roles(id), permission TEXT NOT NULL,
            PRIMARY KEY(role_id, permission)
        );
        CREATE INDEX IF NOT EXISTS idx_bookings_room_time ON bookings(room_id, starts_at, ends_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_active_status ON tasks(active, status);
        CREATE INDEX IF NOT EXISTS idx_projects_active_status ON projects(active, status);
        CREATE INDEX IF NOT EXISTS idx_project_tasks_task ON project_tasks(task_id);
        CREATE INDEX IF NOT EXISTS idx_task_dependencies_prerequisite ON task_dependencies(depends_on_task_id);
        CREATE INDEX IF NOT EXISTS idx_task_subtasks_task ON task_subtasks(task_id, active, status);
        CREATE INDEX IF NOT EXISTS idx_task_schedules_due ON task_schedules(due_at);
        CREATE INDEX IF NOT EXISTS idx_task_approvals_pending ON task_approvals(approver_id, status);
        CREATE INDEX IF NOT EXISTS idx_tickets_active_status ON tickets(active, status);
        CREATE INDEX IF NOT EXISTS idx_comments_subject ON comments(subject_type, subject_id, id);
        CREATE INDEX IF NOT EXISTS idx_attachments_subject ON attachments(subject_type, subject_id, active, id);
        CREATE INDEX IF NOT EXISTS idx_facility_assets_active ON facility_assets(active, category, location);
        CREATE INDEX IF NOT EXISTS idx_maintenance_orders_asset ON maintenance_orders(asset_id, active, due_at);
        CREATE INDEX IF NOT EXISTS idx_employees_active ON employees(active, name);
        CREATE INDEX IF NOT EXISTS idx_accounts_username ON accounts(username, enabled);
        CREATE INDEX IF NOT EXISTS idx_leave_requests_employee ON leave_requests(employee_id, active, starts_at);
        CREATE INDEX IF NOT EXISTS idx_organization_units_active ON organization_units(active, name);
        INSERT OR IGNORE INTO roles(code,name,description) VALUES
            ('admin','Systemadministration','Uneingeschränkte Administration'),
            ('facility_manager','Facility Management','Räume und Facility-Tickets verwalten'),
            ('project_manager','Projektmanagement','Aufgaben und Workflows verwalten'),
            ('employee','Mitarbeiter','Standardzugriff auf eigene Vorgänge');
        INSERT OR IGNORE INTO role_permissions(role_id,permission)
            SELECT id,'*' FROM roles WHERE code='admin';
        INSERT OR IGNORE INTO role_permissions(role_id,permission)
            SELECT id,'rooms.manage' FROM roles WHERE code='facility_manager';
        INSERT OR IGNORE INTO role_permissions(role_id,permission)
            SELECT id,'tickets.manage' FROM roles WHERE code='facility_manager';
        INSERT OR IGNORE INTO role_permissions(role_id,permission)
            SELECT id,'tasks.manage' FROM roles WHERE code='project_manager';
    )SQL");
}

std::vector<EnterpriseRoom> EnterpriseDatabase::rooms(bool includeArchived) {
    std::vector<EnterpriseRoom> result;
    const char* sql = includeArchived
        ? "SELECT id,name,location,capacity,equipment,active FROM rooms ORDER BY active DESC,name"
        : "SELECT id,name,location,capacity,equipment,active FROM rooms WHERE active=1 ORDER BY name";
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) != SQLITE_OK) { setError("Räume konnten nicht geladen werden"); return result; }
    while (sqlite3_step(statement) == SQLITE_ROW) {
        result.push_back({sqlite3_column_int64(statement, 0), Wide(sqlite3_column_text(statement, 1)), Wide(sqlite3_column_text(statement, 2)), sqlite3_column_int(statement, 3), Wide(sqlite3_column_text(statement, 4)), sqlite3_column_int(statement, 5) != 0});
    }
    sqlite3_finalize(statement);
    return result;
}

std::vector<EnterpriseRoom> EnterpriseDatabase::searchRooms(const std::wstring& query, bool includeArchived) {
    std::vector<EnterpriseRoom> result;
    const char* sql = includeArchived
        ? "SELECT id,name,location,capacity,equipment,active FROM rooms WHERE name LIKE ? OR location LIKE ? OR equipment LIKE ? ORDER BY active DESC,name"
        : "SELECT id,name,location,capacity,equipment,active FROM rooms WHERE active=1 AND (name LIKE ? OR location LIKE ? OR equipment LIKE ?) ORDER BY name";
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) != SQLITE_OK) { setError("Raumsuche konnte nicht ausgeführt werden"); return result; }
    const std::wstring pattern = L"%" + query + L"%";
    BindText(statement, 1, pattern); BindText(statement, 2, pattern); BindText(statement, 3, pattern);
    while (sqlite3_step(statement) == SQLITE_ROW) result.push_back({sqlite3_column_int64(statement, 0), Wide(sqlite3_column_text(statement, 1)), Wide(sqlite3_column_text(statement, 2)), sqlite3_column_int(statement, 3), Wide(sqlite3_column_text(statement, 4)), sqlite3_column_int(statement, 5) != 0});
    sqlite3_finalize(statement); return result;
}

bool EnterpriseDatabase::audit(const std::wstring& actor, const std::wstring& action, const std::wstring& subject) {
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db_, "INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)", -1, &statement, nullptr) != SQLITE_OK) { setError("Audit konnte nicht gespeichert werden"); return false; }
    BindText(statement, 1, auditActor_.empty() ? actor : auditActor_); BindText(statement, 2, action); BindText(statement, 3, subject);
    const bool ok = sqlite3_step(statement) == SQLITE_DONE;
    if (!ok) setError("Audit konnte nicht gespeichert werden");
    sqlite3_finalize(statement); return ok;
}

bool EnterpriseDatabase::createRoom(const EnterpriseRoom& room, int64_t& newId, const std::wstring& actor) {
    if (room.name.empty() || room.location.empty() || room.capacity <= 0) { error_ = L"Name, Standort und Kapazität sind Pflichtangaben."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr;
    const char* sql = "INSERT INTO rooms(name,location,capacity,equipment) VALUES(?,?,?,?)";
    bool ok = sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, room.name); BindText(statement, 2, room.location); sqlite3_bind_int(statement, 3, room.capacity); BindText(statement, 4, room.equipment); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    if (ok) { newId = sqlite3_last_insert_rowid(db_); ok = audit(actor, L"room.created", room.name); }
    if (ok) ok = execute("COMMIT"); else execute("ROLLBACK");
    if (!ok) { execute("ROLLBACK"); newId = 0; setError("Raum konnte nicht angelegt werden"); }
    return ok;
}

bool EnterpriseDatabase::updateRoom(const EnterpriseRoom& room, const std::wstring& actor) {
    if (room.id <= 0 || room.name.empty() || room.location.empty() || room.capacity <= 0) { error_ = L"Ungültige Raumdaten."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr;
    const char* sql = "UPDATE rooms SET name=?,location=?,capacity=?,equipment=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1";
    bool ok = sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, room.name); BindText(statement, 2, room.location); sqlite3_bind_int(statement, 3, room.capacity); BindText(statement, 4, room.equipment); sqlite3_bind_int64(statement, 5, room.id); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"room.updated", room.name);
    if (ok) ok=execute("COMMIT"); else execute("ROLLBACK");
    if (!ok) { execute("ROLLBACK"); setError("Raum konnte nicht bearbeitet werden"); }
    return ok;
}

bool EnterpriseDatabase::archiveRoom(int64_t roomId, const std::wstring& actor) {
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE rooms SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, roomId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"room.archived", std::to_wstring(roomId));
    if (ok) ok=execute("COMMIT"); else execute("ROLLBACK");
    if (!ok) { execute("ROLLBACK"); setError("Raum konnte nicht archiviert werden"); }
    return ok;
}

bool EnterpriseDatabase::applySyncedRoom(const std::wstring& externalId, const EnterpriseRoom& room, const std::wstring& actor) {
    if (externalId.empty() || room.name.empty() || room.location.empty() || room.capacity <= 0) { error_ = L"Ungültige zentrale Raumdaten."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr; int64_t localId = 0; bool linked = false;
    bool ok = sqlite3_prepare_v2(db_, "SELECT local_id FROM sync_object_links WHERE object_type='room' AND external_id=?", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, externalId); if (sqlite3_step(statement) == SQLITE_ROW) { localId = sqlite3_column_int64(statement, 0); linked = true; } }
    if (statement) sqlite3_finalize(statement); statement = nullptr;
    if (ok && localId == 0) { ok = sqlite3_prepare_v2(db_, "SELECT id FROM rooms WHERE name=? COLLATE NOCASE", -1, &statement, nullptr) == SQLITE_OK; if(ok){BindText(statement,1,room.name);if(sqlite3_step(statement)==SQLITE_ROW)localId=sqlite3_column_int64(statement,0);}if(statement)sqlite3_finalize(statement);statement=nullptr; }
    if (ok && localId == 0) {
        ok = sqlite3_prepare_v2(db_, "INSERT INTO rooms(name,location,capacity,equipment,active) VALUES(?,?,?,?,?)", -1, &statement, nullptr) == SQLITE_OK;
        if (ok) { BindText(statement,1,room.name); BindText(statement,2,room.location); sqlite3_bind_int(statement,3,room.capacity); BindText(statement,4,room.equipment); sqlite3_bind_int(statement,5,room.active?1:0); ok=sqlite3_step(statement)==SQLITE_DONE; }
        if (statement) sqlite3_finalize(statement); statement=nullptr;
        if (ok) { localId=sqlite3_last_insert_rowid(db_); ok=sqlite3_prepare_v2(db_, "INSERT OR IGNORE INTO sync_object_links(object_type,external_id,local_id) VALUES('room',?,?)", -1, &statement, nullptr)==SQLITE_OK; if(ok){BindText(statement,1,externalId);sqlite3_bind_int64(statement,2,localId);ok=sqlite3_step(statement)==SQLITE_DONE;} }
    } else if (ok) {
        if (!linked) { ok=sqlite3_prepare_v2(db_, "INSERT OR IGNORE INTO sync_object_links(object_type,external_id,local_id) VALUES('room',?,?)", -1, &statement, nullptr)==SQLITE_OK; if(ok){BindText(statement,1,externalId);sqlite3_bind_int64(statement,2,localId);ok=sqlite3_step(statement)==SQLITE_DONE;}if(statement)sqlite3_finalize(statement);statement=nullptr; }
        ok = sqlite3_prepare_v2(db_, "UPDATE rooms SET name=?,location=?,capacity=?,equipment=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", -1, &statement, nullptr) == SQLITE_OK;
        if (ok) { BindText(statement,1,room.name); BindText(statement,2,room.location); sqlite3_bind_int(statement,3,room.capacity); BindText(statement,4,room.equipment); sqlite3_bind_int(statement,5,room.active?1:0); sqlite3_bind_int64(statement,6,localId); ok=sqlite3_step(statement)==SQLITE_DONE && sqlite3_changes(db_)==1; }
    }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"sync.room.applied", externalId);
    execute(ok ? "COMMIT" : "ROLLBACK");
    if (!ok) setError("Zentraler Raum konnte nicht übernommen werden");
    return ok;
}

// Apply one complete, ordered server booking state. Remote bookings use an
// explicit identity and a shadow; a local change can never be overwritten.
bool EnterpriseDatabase::applySyncedBooking(const std::wstring& clientId, int64_t remoteId, const std::wstring& roomExternalId, const EnterpriseBooking& incoming, const std::wstring& reason, const std::wstring& actor, bool deferOverlapCheck) {
    if(clientId.empty() || remoteId<=0 || roomExternalId.empty() || incoming.startUtc<=0 || incoming.endUtc<=incoming.startUtc || incoming.attendees<=0 ||
       (incoming.status!=L"confirmed" && incoming.status!=L"blocked" && incoming.status!=L"cancelled") || (incoming.status==L"blocked" && reason.empty())) { error_=L"Ungültige zentrale Buchung.";return false; }
    if(!execute("SAVEPOINT desktop_booking_apply"))return false;
    const int64_t room=syncedLocalId(L"room",roomExternalId);
    bool ok=room>0 && execute(R"SQL(CREATE TABLE IF NOT EXISTS desktop_booking_sync_shadow(client_id TEXT NOT NULL, remote_id INTEGER NOT NULL, booking_id INTEGER NOT NULL UNIQUE REFERENCES bookings(id), room_id INTEGER NOT NULL, starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL, attendees INTEGER NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(client_id,remote_id)))SQL");
    sqlite3_stmt* st=nullptr;int64_t local=0;bool changed=true;
    if(ok)ok=sqlite3_prepare_v2(db_,"SELECT s.booking_id,b.room_id,b.user_id,b.starts_at,b.ends_at,b.attendees,b.status,s.room_id,s.starts_at,s.ends_at,s.attendees,s.status,s.reason FROM desktop_booking_sync_shadow s JOIN bookings b ON b.id=s.booking_id WHERE s.client_id=? AND s.remote_id=?",-1,&st,nullptr)==SQLITE_OK;
    if(ok){BindText(st,1,clientId);sqlite3_bind_int64(st,2,remoteId);int result=sqlite3_step(st);
        if(result==SQLITE_ROW){local=sqlite3_column_int64(st,0);
            ok=sqlite3_column_int64(st,1)==sqlite3_column_int64(st,7) && sqlite3_column_int64(st,2)==0 && sqlite3_column_int64(st,3)==sqlite3_column_int64(st,8) && sqlite3_column_int64(st,4)==sqlite3_column_int64(st,9) && sqlite3_column_int(st,5)==sqlite3_column_int(st,10) && Wide(sqlite3_column_text(st,6))==Wide(sqlite3_column_text(st,11));
            if(ok)changed=sqlite3_column_int64(st,7)!=room || sqlite3_column_int64(st,8)!=incoming.startUtc || sqlite3_column_int64(st,9)!=incoming.endUtc || sqlite3_column_int(st,10)!=incoming.attendees || Wide(sqlite3_column_text(st,11))!=incoming.status || Wide(sqlite3_column_text(st,12))!=reason;
        }else ok=result==SQLITE_DONE;}
    if(st)sqlite3_finalize(st);st=nullptr;
    if(ok && !deferOverlapCheck && incoming.status!=L"cancelled"){
        ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM bookings WHERE room_id=? AND id<>? AND status IN ('confirmed','blocked') AND starts_at<? AND ends_at>? LIMIT 1",-1,&st,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(st,1,room);sqlite3_bind_int64(st,2,local);sqlite3_bind_int64(st,3,incoming.endUtc);sqlite3_bind_int64(st,4,incoming.startUtc);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
    }
    if(ok && changed){
        const char* sql=local?"UPDATE bookings SET room_id=?,user_id=0,starts_at=?,ends_at=?,attendees=?,status=? WHERE id=?":"INSERT INTO bookings(room_id,user_id,starts_at,ends_at,attendees,status) VALUES(?,0,?,?,?,?)";
        ok=sqlite3_prepare_v2(db_,sql,-1,&st,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(st,1,room);sqlite3_bind_int64(st,2,incoming.startUtc);sqlite3_bind_int64(st,3,incoming.endUtc);sqlite3_bind_int(st,4,incoming.attendees);BindText(st,5,incoming.status);if(local)sqlite3_bind_int64(st,6,local);ok=sqlite3_step(st)==SQLITE_DONE && sqlite3_changes(db_)==1;}if(st)sqlite3_finalize(st);st=nullptr;if(ok && !local)local=sqlite3_last_insert_rowid(db_);
        if(ok){ok=sqlite3_prepare_v2(db_,"INSERT INTO desktop_booking_sync_shadow VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(client_id,remote_id) DO UPDATE SET room_id=excluded.room_id,starts_at=excluded.starts_at,ends_at=excluded.ends_at,attendees=excluded.attendees,status=excluded.status,reason=excluded.reason",-1,&st,nullptr)==SQLITE_OK;
            if(ok){BindText(st,1,clientId);sqlite3_bind_int64(st,2,remoteId);sqlite3_bind_int64(st,3,local);sqlite3_bind_int64(st,4,room);sqlite3_bind_int64(st,5,incoming.startUtc);sqlite3_bind_int64(st,6,incoming.endUtc);sqlite3_bind_int(st,7,incoming.attendees);BindText(st,8,incoming.status);BindText(st,9,reason);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;}
        if(ok)ok=audit(actor,L"sync.booking.applied",clientId+L":"+std::to_wstring(remoteId));
    }
    if(ok)ok=execute("RELEASE desktop_booking_apply");if(!ok){execute("ROLLBACK TO desktop_booking_apply");execute("RELEASE desktop_booking_apply");error_=L"Zentrale Buchung konnte nicht übernommen werden: Raumzuordnung, lokaler Stand oder Überschneidung prüfen.";return false;}return true;
}

// Apply a complete snapshot in one transaction, validating overlaps against
// the final calendar rather than transient states while entries are moved.
bool EnterpriseDatabase::applySyncedBookingSnapshot(const std::wstring& snapshotJson,const std::wstring& ownClientId,bool& changed) {
    changed=false;if(snapshotJson.empty()||ownClientId.empty()){error_=L"Ungültiger Buchungsbestand.";return false;}
    struct Item { std::wstring client,room,reason;int64_t id;EnterpriseBooking booking;bool attendeesKnown=false; };
    std::vector<Item> items;std::set<std::pair<std::wstring,int64_t>> keys;sqlite3_stmt* st=nullptr;
    const char* sql=R"SQL(SELECT json_type(?1),j.value,json_type(j.value,'$.clientId'),json_type(j.value,'$.bookingId'),json_type(j.value,'$.roomKey'),json_type(j.value,'$.startsAt'),json_type(j.value,'$.endsAt'),json_type(j.value,'$.attendees'),json_type(j.value,'$.status'),json_type(j.value,'$.reason'),json_extract(j.value,'$.clientId'),json_extract(j.value,'$.bookingId'),json_extract(j.value,'$.roomKey'),json_extract(j.value,'$.startsAt'),json_extract(j.value,'$.endsAt'),COALESCE(json_extract(j.value,'$.attendees'),1),COALESCE(json_extract(j.value,'$.status'),'confirmed'),COALESCE(json_extract(j.value,'$.reason'),'') FROM json_each(?1) j)SQL";
    bool ok=sqlite3_prepare_v2(db_,"SELECT json_valid(?1) AND json_type(?1)='array'",-1,&st,nullptr)==SQLITE_OK;
    if(ok){BindText(st,1,snapshotJson);ok=sqlite3_step(st)==SQLITE_ROW && sqlite3_column_int(st,0)==1;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok)ok=sqlite3_prepare_v2(db_,sql,-1,&st,nullptr)==SQLITE_OK;
    if(ok){BindText(st,1,snapshotJson);int result;
        while((result=sqlite3_step(st))==SQLITE_ROW){
            auto type=[&](int index,const char* expected){const char* actual=reinterpret_cast<const char*>(sqlite3_column_text(st,index));return actual && std::strcmp(actual,expected)==0;};
            if(items.size()>=100000 || !type(0,"array") || !type(2,"text") || !type(3,"integer") || !type(4,"text") || !type(5,"integer") || !type(6,"integer") || (sqlite3_column_type(st,7)!=SQLITE_NULL&&!type(7,"integer")) || (sqlite3_column_type(st,8)!=SQLITE_NULL&&!type(8,"text")) || (sqlite3_column_type(st,9)!=SQLITE_NULL&&!type(9,"text"))){ok=false;break;}
            Item item;item.client=Wide(sqlite3_column_text(st,10));item.id=sqlite3_column_int64(st,11);item.room=Wide(sqlite3_column_text(st,12));item.booking.startUtc=sqlite3_column_int64(st,13);item.booking.endUtc=sqlite3_column_int64(st,14);const int64_t attendees=sqlite3_column_int64(st,15);item.booking.status=Wide(sqlite3_column_text(st,16));item.reason=Wide(sqlite3_column_text(st,17));
            if(item.client.empty()||item.room.empty()||item.id<=0||attendees<0||attendees>2147483647||!keys.emplace(item.client,item.id).second){ok=false;break;}
            item.attendeesKnown=sqlite3_column_type(st,7)!=SQLITE_NULL && attendees>0;
            item.booking.attendees=attendees==0?1:static_cast<int>(attendees);if(item.booking.status.empty())item.booking.status=L"confirmed";
            if(item.booking.startUtc<=0 || item.booking.endUtc<=item.booking.startUtc || (item.booking.status!=L"confirmed" && item.booking.status!=L"cancelled" && item.booking.status!=L"blocked") || (item.booking.status==L"blocked" && item.reason.empty())){ok=false;break;}
            items.push_back(item);
        }
        if(result!=SQLITE_DONE)ok=false;
    }
    if(st)sqlite3_finalize(st);st=nullptr;if(!ok){error_=L"Ungültiger zentraler Buchungsbestand.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    const int before=sqlite3_total_changes(db_);
    ok=execute("CREATE TABLE IF NOT EXISTS desktop_own_booking_shadow(booking_id INTEGER PRIMARY KEY,room_id INTEGER NOT NULL,starts_at INTEGER NOT NULL,ends_at INTEGER NOT NULL,attendees INTEGER NOT NULL,status TEXT NOT NULL)");
    for(const Item& item:items){
        if(!ok)break;
        if(item.client!=ownClientId){if(!applySyncedBooking(item.client,item.id,item.room,item.booking,item.reason,L"sync.remote",true))ok=false;continue;}
        const int64_t room=syncedLocalId(L"room",item.room);
        ok=room>0 && sqlite3_prepare_v2(db_,"SELECT b.room_id,b.starts_at,b.ends_at,b.attendees,b.status,s.room_id,s.starts_at,s.ends_at,s.attendees,s.status FROM bookings b LEFT JOIN desktop_own_booking_shadow s ON s.booking_id=b.id WHERE b.id=?",-1,&st,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(st,1,item.id);ok=sqlite3_step(st)==SQLITE_ROW;}
        int64_t localRoom=0,localStart=0,localEnd=0;int localAttendees=0;std::wstring localStatus;bool hasShadow=false,localMatchesShadow=false;
        if(ok){localRoom=sqlite3_column_int64(st,0);localStart=sqlite3_column_int64(st,1);localEnd=sqlite3_column_int64(st,2);localAttendees=sqlite3_column_int(st,3);localStatus=Wide(sqlite3_column_text(st,4));hasShadow=sqlite3_column_type(st,5)!=SQLITE_NULL;
            localMatchesShadow=hasShadow && localRoom==sqlite3_column_int64(st,5) && localStart==sqlite3_column_int64(st,6) && localEnd==sqlite3_column_int64(st,7) && localAttendees==sqlite3_column_int(st,8) && localStatus==Wide(sqlite3_column_text(st,9));}
        if(st)sqlite3_finalize(st);st=nullptr;
        const int remoteAttendees=item.attendeesKnown?item.booking.attendees:localAttendees;
        const bool localMatchesRemote=ok && localRoom==room && localStart==item.booking.startUtc && localEnd==item.booking.endUtc && localAttendees==remoteAttendees && localStatus==item.booking.status;
        if(ok && !localMatchesRemote){
            if(!localMatchesShadow)ok=false;
            else {
                ok=sqlite3_prepare_v2(db_,"UPDATE bookings SET room_id=?,starts_at=?,ends_at=?,attendees=?,status=? WHERE id=?",-1,&st,nullptr)==SQLITE_OK;
                if(ok){sqlite3_bind_int64(st,1,room);sqlite3_bind_int64(st,2,item.booking.startUtc);sqlite3_bind_int64(st,3,item.booking.endUtc);sqlite3_bind_int(st,4,remoteAttendees);BindText(st,5,item.booking.status);sqlite3_bind_int64(st,6,item.id);ok=sqlite3_step(st)==SQLITE_DONE && sqlite3_changes(db_)==1;}if(st)sqlite3_finalize(st);st=nullptr;
                if(ok)ok=audit(L"sync.remote",L"sync.booking.applied",item.client+L":"+std::to_wstring(item.id));
            }
        }
        if(ok && (!hasShadow || !localMatchesShadow || !localMatchesRemote)){
            ok=sqlite3_prepare_v2(db_,"INSERT INTO desktop_own_booking_shadow VALUES(?,?,?,?,?,?) ON CONFLICT(booking_id) DO UPDATE SET room_id=excluded.room_id,starts_at=excluded.starts_at,ends_at=excluded.ends_at,attendees=excluded.attendees,status=excluded.status",-1,&st,nullptr)==SQLITE_OK;
            if(ok){sqlite3_bind_int64(st,1,item.id);sqlite3_bind_int64(st,2,room);sqlite3_bind_int64(st,3,item.booking.startUtc);sqlite3_bind_int64(st,4,item.booking.endUtc);sqlite3_bind_int(st,5,remoteAttendees);BindText(st,6,item.booking.status);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
        }
    }
    if(ok){ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM bookings a JOIN bookings b ON a.room_id=b.room_id AND a.id<b.id AND a.starts_at<b.ends_at AND a.ends_at>b.starts_at WHERE a.status IN ('confirmed','blocked') AND b.status IN ('confirmed','blocked') LIMIT 1",-1,&st,nullptr)==SQLITE_OK;if(ok)ok=sqlite3_step(st)==SQLITE_DONE;if(st)sqlite3_finalize(st);}
    const bool anyChanges=sqlite3_total_changes(db_)!=before;
    if(ok)ok=execute("COMMIT");if(!ok){execute("ROLLBACK");error_=L"Zentraler Buchungsbestand konnte nicht vollständig übernommen werden: Raumzuordnung, lokaler Stand oder Überschneidung prüfen.";return false;}changed=anyChanges;return true;
}

// Older cancellation events carry only the original client and booking ID.
bool EnterpriseDatabase::cancelSyncedBooking(const std::wstring& clientId, int64_t remoteId, const std::wstring& actor) {
    if(clientId.empty() || remoteId<=0){error_=L"Ungültige zentrale Stornierung.";return false;}
    sqlite3_stmt* st=nullptr;bool exists=false;
    bool ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_booking_sync_shadow'",-1,&st,nullptr)==SQLITE_OK;
    if(ok){int r=sqlite3_step(st);exists=r==SQLITE_ROW;ok=r==SQLITE_ROW || r==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
    if(!ok){setError("Zentrale Stornierung konnte nicht gelesen werden");return false;}
    if(!exists)return true;
    ok=sqlite3_prepare_v2(db_,"SELECT l.external_id,s.starts_at,s.ends_at,s.attendees,s.reason FROM desktop_booking_sync_shadow s LEFT JOIN sync_object_links l ON l.object_type='room' AND l.local_id=s.room_id WHERE s.client_id=? AND s.remote_id=?",-1,&st,nullptr)==SQLITE_OK;
    std::wstring room,reason;EnterpriseBooking booking;bool found=false;
    if(ok){BindText(st,1,clientId);sqlite3_bind_int64(st,2,remoteId);int r=sqlite3_step(st);found=r==SQLITE_ROW;ok=found || r==SQLITE_DONE;if(found){room=Wide(sqlite3_column_text(st,0));booking.startUtc=sqlite3_column_int64(st,1);booking.endUtc=sqlite3_column_int64(st,2);booking.attendees=sqlite3_column_int(st,3);reason=Wide(sqlite3_column_text(st,4));}}
    if(st)sqlite3_finalize(st);if(!ok){setError("Zentrale Stornierung konnte nicht gelesen werden");return false;}if(!found)return true;if(room.empty()){error_=L"Der Raum der zentralen Buchung ist nicht zugeordnet.";return false;}
    booking.status=L"cancelled";return applySyncedBooking(clientId,remoteId,room,booking,reason,actor);
}

bool EnterpriseDatabase::createBooking(const EnterpriseBooking& booking, int64_t& newId, const std::wstring& actor, int64_t actingEmployee) {
    if (booking.roomId <= 0 || booking.userId <= 0 || booking.endUtc <= booking.startUtc || booking.attendees <= 0) { error_ = L"Ungültige Buchungsdaten."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    if (actingEmployee<0 || !execute(("UPDATE linux_booking_sync_actor SET employee_id="+std::to_string(actingEmployee)+" WHERE singleton=1").c_str())) { execute("ROLLBACK"); return false; }
    sqlite3_stmt* statement = nullptr;
    const char* conflict = "SELECT 1 FROM bookings WHERE room_id=? AND status IN ('confirmed','blocked') AND starts_at < ? AND ends_at > ? LIMIT 1";
    bool ok = sqlite3_prepare_v2(db_, conflict, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, booking.roomId); sqlite3_bind_int64(statement, 2, booking.endUtc); sqlite3_bind_int64(statement, 3, booking.startUtc); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    if (!ok) { execute("ROLLBACK"); error_ = L"Der Raum ist in diesem Zeitraum bereits gebucht."; return false; }
    const char* insert = "INSERT INTO bookings(room_id,user_id,starts_at,ends_at,attendees,status) SELECT ?,?,?,?,?,? WHERE EXISTS(SELECT 1 FROM rooms WHERE id=? AND active=1 AND capacity>=?)";
    statement = nullptr; ok = sqlite3_prepare_v2(db_, insert, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, booking.roomId); sqlite3_bind_int64(statement, 2, booking.userId); sqlite3_bind_int64(statement, 3, booking.startUtc); sqlite3_bind_int64(statement, 4, booking.endUtc); sqlite3_bind_int(statement, 5, booking.attendees); BindText(statement, 6, L"confirmed"); sqlite3_bind_int64(statement, 7, booking.roomId); sqlite3_bind_int(statement, 8, booking.attendees); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) { newId = sqlite3_last_insert_rowid(db_); ok = audit(actor, L"booking.created", std::to_wstring(newId)); }
    if(ok)ok=execute("UPDATE linux_booking_sync_actor SET employee_id=0 WHERE singleton=1");
    if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");
    if (!ok) setError("Buchung konnte nicht angelegt werden");
    return ok;
}

bool EnterpriseDatabase::createRecurringBookings(const EnterpriseBooking& firstBooking, int intervalDays, int occurrences, std::vector<int64_t>& newIds, const std::wstring& actor, int64_t actingEmployee) {
    newIds.clear();
    if (firstBooking.roomId <= 0 || firstBooking.userId <= 0 || firstBooking.endUtc <= firstBooking.startUtc || firstBooking.attendees <= 0 || intervalDays <= 0 || occurrences < 2 || occurrences > 104) {
        error_ = L"Ungültige Serienbuchungsdaten.";
        return false;
    }
    if (!execute("BEGIN IMMEDIATE")) return false;
    if (actingEmployee<0 || !execute(("UPDATE linux_booking_sync_actor SET employee_id="+std::to_string(actingEmployee)+" WHERE singleton=1").c_str())) { execute("ROLLBACK"); return false; }
    const char* conflictSql = "SELECT 1 FROM bookings WHERE room_id=? AND status IN ('confirmed','blocked') AND starts_at < ? AND ends_at > ? LIMIT 1";
    const char* insertSql = "INSERT INTO bookings(room_id,user_id,starts_at,ends_at,attendees,status) SELECT ?,?,?,?,?,? WHERE EXISTS(SELECT 1 FROM rooms WHERE id=? AND active=1 AND capacity>=?)";
    bool ok = true;
    for (int index = 0; ok && index < occurrences; ++index) {
        const int64_t offset = static_cast<int64_t>(index) * intervalDays * 86400;
        const int64_t startsAt = firstBooking.startUtc + offset;
        const int64_t endsAt = firstBooking.endUtc + offset;
        sqlite3_stmt* statement = nullptr;
        ok = sqlite3_prepare_v2(db_, conflictSql, -1, &statement, nullptr) == SQLITE_OK;
        if (ok) {
            sqlite3_bind_int64(statement, 1, firstBooking.roomId);
            sqlite3_bind_int64(statement, 2, endsAt);
            sqlite3_bind_int64(statement, 3, startsAt);
            ok = sqlite3_step(statement) == SQLITE_DONE;
        }
        if (statement) sqlite3_finalize(statement);
        if (!ok) break;
        statement = nullptr;
        ok = sqlite3_prepare_v2(db_, insertSql, -1, &statement, nullptr) == SQLITE_OK;
        if (ok) {
            sqlite3_bind_int64(statement, 1, firstBooking.roomId);
            sqlite3_bind_int64(statement, 2, firstBooking.userId);
            sqlite3_bind_int64(statement, 3, startsAt);
            sqlite3_bind_int64(statement, 4, endsAt);
            sqlite3_bind_int(statement, 5, firstBooking.attendees);
            BindText(statement, 6, L"confirmed");
            sqlite3_bind_int64(statement, 7, firstBooking.roomId);
            sqlite3_bind_int(statement, 8, firstBooking.attendees);
            ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1;
        }
        if (statement) sqlite3_finalize(statement);
        if (ok) newIds.push_back(sqlite3_last_insert_rowid(db_));
    }
    if (ok) ok = audit(actor, L"booking.series.created", std::to_wstring(newIds.size()) + L" Buchungen für Raum " + std::to_wstring(firstBooking.roomId));
    if(ok)ok=execute("UPDATE linux_booking_sync_actor SET employee_id=0 WHERE singleton=1");
    if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");
    if (!ok) {
        newIds.clear();
        error_ = L"Die Buchungsserie kollidiert, überschreitet die Kapazität oder konnte nicht gespeichert werden.";
    }
    return ok;
}

bool EnterpriseDatabase::rescheduleBooking(const EnterpriseBooking& booking, const std::wstring& actor, int64_t actingEmployee) {
    if (booking.id <= 0 || booking.roomId <= 0 || booking.endUtc <= booking.startUtc || booking.attendees <= 0) { error_ = L"Ungültige Umbuchungsdaten."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    if (actingEmployee<0 || !execute(("UPDATE linux_booking_sync_actor SET employee_id="+std::to_string(actingEmployee)+" WHERE singleton=1").c_str())) { execute("ROLLBACK"); return false; }
    sqlite3_stmt* statement = nullptr;
    const char* conflict = "SELECT 1 FROM bookings WHERE room_id=? AND id<>? AND status IN ('confirmed','blocked') AND starts_at < ? AND ends_at > ? LIMIT 1";
    bool ok = sqlite3_prepare_v2(db_, conflict, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, booking.roomId); sqlite3_bind_int64(statement, 2, booking.id); sqlite3_bind_int64(statement, 3, booking.endUtc); sqlite3_bind_int64(statement, 4, booking.startUtc); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    if (!ok) { execute("ROLLBACK"); error_ = L"Der Raum ist im gewünschten Zeitraum bereits belegt."; return false; }
    const char* update = "UPDATE bookings SET room_id=?,starts_at=?,ends_at=?,attendees=? WHERE id=? AND status='confirmed' AND EXISTS(SELECT 1 FROM rooms WHERE id=? AND active=1 AND capacity>=?)";
    statement = nullptr; ok = sqlite3_prepare_v2(db_, update, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, booking.roomId); sqlite3_bind_int64(statement, 2, booking.startUtc); sqlite3_bind_int64(statement, 3, booking.endUtc); sqlite3_bind_int(statement, 4, booking.attendees); sqlite3_bind_int64(statement, 5, booking.id); sqlite3_bind_int64(statement, 6, booking.roomId); sqlite3_bind_int(statement, 7, booking.attendees); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"booking.rescheduled", std::to_wstring(booking.id));
    if(ok)ok=execute("UPDATE linux_booking_sync_actor SET employee_id=0 WHERE singleton=1");
    if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK"); if (!ok) setError("Buchung konnte nicht umgebucht werden"); return ok;
}

bool EnterpriseDatabase::blockRoom(int64_t roomId, int64_t startUtc, int64_t endUtc, const std::wstring& reason, int64_t& newId, const std::wstring& actor, int64_t actingEmployee) {
    if (roomId <= 0 || endUtc <= startUtc || reason.empty()) { error_ = L"Ungültige Sperrzeit."; return false; }
    if (actingEmployee<0 || !execute("BEGIN IMMEDIATE")) return false;
    if(!execute(("UPDATE linux_booking_sync_actor SET employee_id="+std::to_string(actingEmployee)+" WHERE singleton=1").c_str())){execute("ROLLBACK");return false;}
    sqlite3_stmt* statement = nullptr;
    const char* conflict = "SELECT 1 FROM bookings WHERE room_id=? AND status IN ('confirmed','blocked') AND starts_at < ? AND ends_at > ? LIMIT 1";
    bool ok = sqlite3_prepare_v2(db_, conflict, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, roomId); sqlite3_bind_int64(statement, 2, endUtc); sqlite3_bind_int64(statement, 3, startUtc); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    if (!ok) { execute("ROLLBACK"); error_ = L"Sperrzeit kollidiert mit einer Reservierung."; return false; }
    statement = nullptr; ok = sqlite3_prepare_v2(db_, "INSERT INTO bookings(room_id,user_id,starts_at,ends_at,attendees,status) VALUES(?,?,?,?,?,'blocked')", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, roomId); sqlite3_bind_int64(statement, 2, 0); sqlite3_bind_int64(statement, 3, startUtc); sqlite3_bind_int64(statement, 4, endUtc); sqlite3_bind_int(statement, 5, 1); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    if (ok) { newId = sqlite3_last_insert_rowid(db_); statement=nullptr;ok=sqlite3_prepare_v2(db_,"INSERT INTO booking_block_details(booking_id,reason,created_by) VALUES(?,?,?)",-1,&statement,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(statement,1,newId);BindText(statement,2,reason);sqlite3_bind_int64(statement,3,actingEmployee);ok=sqlite3_step(statement)==SQLITE_DONE;}if(statement)sqlite3_finalize(statement); }
    if(ok)ok=audit(actor, L"room.blocked", std::to_wstring(roomId) + L":" + reason);
    if(ok)ok=execute("UPDATE linux_booking_sync_actor SET employee_id=0 WHERE singleton=1");
    if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");if(!ok){newId=0;setError("Sperrzeit konnte nicht angelegt werden");}return ok;
}

bool EnterpriseDatabase::cancelBooking(int64_t bookingId, const std::wstring& actor, int64_t actingEmployee) {
    if(bookingId<=0 || actingEmployee<0 || !execute("BEGIN IMMEDIATE"))return false;
    bool ok=execute(("UPDATE linux_booking_sync_actor SET employee_id="+std::to_string(actingEmployee)+" WHERE singleton=1").c_str());
    sqlite3_stmt* statement = nullptr;
    if(ok)ok = sqlite3_prepare_v2(db_, "UPDATE bookings SET status='cancelled' WHERE id=? AND status='confirmed'", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, bookingId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"booking.cancelled", std::to_wstring(bookingId));
    if (ok) ok = execute("UPDATE linux_booking_sync_actor SET employee_id=0 WHERE singleton=1");
    if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");
    if (!ok) setError("Buchung konnte nicht storniert werden");return ok;
}

bool EnterpriseDatabase::activeBookingForRoom(int64_t roomId, int64_t nowUtc, EnterpriseBooking& booking) {
    sqlite3_stmt* statement = nullptr;
    const char* sql = "SELECT id,room_id,user_id,starts_at,ends_at,attendees,status FROM bookings WHERE room_id=? AND status IN ('confirmed','blocked') AND starts_at<=? AND ends_at>? ORDER BY starts_at LIMIT 1";
    if (sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) != SQLITE_OK) { setError("Buchungsstatus konnte nicht geladen werden"); return false; }
    sqlite3_bind_int64(statement, 1, roomId); sqlite3_bind_int64(statement, 2, nowUtc); sqlite3_bind_int64(statement, 3, nowUtc);
    const bool found = sqlite3_step(statement) == SQLITE_ROW;
    if (found) booking = {sqlite3_column_int64(statement, 0), sqlite3_column_int64(statement, 1), sqlite3_column_int64(statement, 2), sqlite3_column_int64(statement, 3), sqlite3_column_int64(statement, 4), sqlite3_column_int(statement, 5), Wide(sqlite3_column_text(statement, 6))};
    sqlite3_finalize(statement);
    return found;
}

std::vector<EnterpriseBooking> EnterpriseDatabase::bookingsForRoom(int64_t roomId, bool includeCancelled) {
    std::vector<EnterpriseBooking> result;
    const char* sql = includeCancelled
        ? "SELECT id,room_id,user_id,starts_at,ends_at,attendees,status FROM bookings WHERE room_id=? ORDER BY starts_at DESC"
        : "SELECT id,room_id,user_id,starts_at,ends_at,attendees,status FROM bookings WHERE room_id=? AND status IN ('confirmed','blocked') ORDER BY starts_at DESC";
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) != SQLITE_OK) { setError("Buchungshistorie konnte nicht geladen werden"); return result; }
    sqlite3_bind_int64(statement, 1, roomId);
    while (sqlite3_step(statement) == SQLITE_ROW) result.push_back({sqlite3_column_int64(statement, 0), sqlite3_column_int64(statement, 1), sqlite3_column_int64(statement, 2), sqlite3_column_int64(statement, 3), sqlite3_column_int64(statement, 4), sqlite3_column_int(statement, 5), Wide(sqlite3_column_text(statement, 6))});
    sqlite3_finalize(statement); return result;
}

std::vector<EnterpriseTask> EnterpriseDatabase::tasks(bool includeArchived) {
    std::vector<EnterpriseTask> result;
    const char* sql = includeArchived
        ? "SELECT t.id,t.title,t.department,t.priority,t.status,t.assignee_id,t.active,COALESCE(s.due_at,0),COALESCE(s.recurrence_days,0) FROM tasks t LEFT JOIN task_schedules s ON s.task_id=t.id ORDER BY t.active DESC,t.updated_at DESC"
        : "SELECT t.id,t.title,t.department,t.priority,t.status,t.assignee_id,t.active,COALESCE(s.due_at,0),COALESCE(s.recurrence_days,0) FROM tasks t LEFT JOIN task_schedules s ON s.task_id=t.id WHERE t.active=1 ORDER BY CASE t.status WHEN 'in_progress' THEN 0 WHEN 'open' THEN 1 ELSE 2 END,t.updated_at DESC";
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) != SQLITE_OK) { setError("Aufgaben konnten nicht geladen werden"); return result; }
    while (sqlite3_step(statement) == SQLITE_ROW) result.push_back({sqlite3_column_int64(statement, 0), Wide(sqlite3_column_text(statement, 1)), Wide(sqlite3_column_text(statement, 2)), Wide(sqlite3_column_text(statement, 3)), Wide(sqlite3_column_text(statement, 4)), sqlite3_column_int64(statement, 5), sqlite3_column_int(statement, 6) != 0, sqlite3_column_int64(statement, 7), sqlite3_column_int(statement, 8)});
    sqlite3_finalize(statement); return result;
}

std::vector<EnterpriseProject> EnterpriseDatabase::projects(bool includeArchived) {
    std::vector<EnterpriseProject> result;
    const char* sql = includeArchived
        ? "SELECT id,name,department,description,status,owner_id,active FROM projects ORDER BY active DESC,updated_at DESC"
        : "SELECT id,name,department,description,status,owner_id,active FROM projects WHERE active=1 ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'planned' THEN 1 ELSE 2 END,updated_at DESC";
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) != SQLITE_OK) { setError("Projekte konnten nicht geladen werden"); return result; }
    while (sqlite3_step(statement) == SQLITE_ROW) result.push_back({sqlite3_column_int64(statement, 0), Wide(sqlite3_column_text(statement, 1)), Wide(sqlite3_column_text(statement, 2)), Wide(sqlite3_column_text(statement, 3)), Wide(sqlite3_column_text(statement, 4)), sqlite3_column_int64(statement, 5), sqlite3_column_int(statement, 6) != 0});
    sqlite3_finalize(statement); return result;
}

bool EnterpriseDatabase::createProject(const EnterpriseProject& project, int64_t& newId, const std::wstring& actor) {
    if (project.name.empty() || project.department.empty()) { error_ = L"Projektname und Bereich sind Pflichtangaben."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "INSERT INTO projects(name,department,description,status,owner_id) VALUES(?,?,?,?,?)", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, project.name); BindText(statement, 2, project.department); BindText(statement, 3, project.description); BindText(statement, 4, project.status.empty() ? L"planned" : project.status); sqlite3_bind_int64(statement, 5, project.ownerId); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    const int64_t inserted=ok ? sqlite3_last_insert_rowid(db_) : 0;
    if (ok) ok=audit(actor, L"project.created", project.name);
    if (ok) ok=execute("COMMIT");
    if (!ok) { execute("ROLLBACK"); newId=0; setError("Projekt konnte nicht angelegt werden"); return false; }
    newId=inserted; return true;
}

bool EnterpriseDatabase::changeProjectStatus(int64_t projectId,const std::wstring& expected,const std::wstring& status,const std::wstring& actor) {
    const auto valid=[](const std::wstring& s){return s==L"planned"||s==L"active"||s==L"on_hold"||s==L"completed"||s==L"cancelled";};
    if(projectId<=0||!valid(expected)||!valid(status)||expected==status){error_=L"Ungültiger Projektstatus.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    sqlite3_stmt* s=nullptr;bool ok=sqlite3_prepare_v2(db_,"UPDATE projects SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1 AND status=?",-1,&s,nullptr)==SQLITE_OK;
    if(ok){BindText(s,1,status);sqlite3_bind_int64(s,2,projectId);BindText(s,3,expected);ok=sqlite3_step(s)==SQLITE_DONE && sqlite3_changes(db_)==1;}
    if(s)sqlite3_finalize(s);
    if(ok)ok=audit(actor,L"project.status.changed",std::to_wstring(projectId)+L":"+expected+L"->"+status);
    if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");
    if(!ok)error_=L"Projektstatus konnte nicht geändert werden. Bitte den Projektstand aktualisieren.";
    return ok;
}

bool EnterpriseDatabase::updateProject(const EnterpriseProject& project, const std::wstring& actor) {
    if (project.id <= 0 || project.name.empty() || project.department.empty() || (project.status != L"planned" && project.status != L"active" && project.status != L"on_hold" && project.status != L"completed" && project.status != L"cancelled")) { error_ = L"Ungültige Projektdaten."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE projects SET name=?,department=?,description=?,status=?,owner_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, project.name); BindText(statement, 2, project.department); BindText(statement, 3, project.description); BindText(statement, 4, project.status); sqlite3_bind_int64(statement, 5, project.ownerId); sqlite3_bind_int64(statement, 6, project.id); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"project.updated", std::to_wstring(project.id));
    if (ok) ok=execute("COMMIT");
    if (!ok) { execute("ROLLBACK"); setError("Projekt konnte nicht bearbeitet werden"); return false; }
    return true;
}

bool EnterpriseDatabase::archiveProject(int64_t projectId, const std::wstring& actor) {
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE projects SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, projectId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"project.archived", std::to_wstring(projectId));
    if (ok) ok=execute("COMMIT");
    if (!ok) { execute("ROLLBACK"); setError("Projekt konnte nicht archiviert werden"); return false; }
    return true;
}

bool EnterpriseDatabase::applySyncedProject(const std::wstring& externalId, const EnterpriseProject& project, const std::wstring& actor) {
    if (externalId.empty() || project.name.empty() || project.department.empty() || project.ownerId <= 0 || (project.status != L"planned" && project.status != L"active" && project.status != L"on_hold" && project.status != L"completed" && project.status != L"cancelled")) { error_ = L"Ungültige zentrale Projektdaten."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr;
    int64_t localId = 0;
    bool linked = false;
    bool ok = sqlite3_prepare_v2(db_, "SELECT local_id FROM sync_object_links WHERE object_type='project' AND external_id=?", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, externalId); if (sqlite3_step(statement) == SQLITE_ROW) { localId = sqlite3_column_int64(statement, 0); linked = true; } }
    if (statement) sqlite3_finalize(statement); statement = nullptr;

    // A newly provisioned client can already contain the same seeded project.
    // Match that stable natural key first and then establish the missing link;
    // otherwise the UNIQUE project name would stop the complete cursor feed.
    if (ok && localId == 0) {
        ok = sqlite3_prepare_v2(db_, "SELECT id FROM projects WHERE name=? COLLATE NOCASE", -1, &statement, nullptr) == SQLITE_OK;
        if (ok) { BindText(statement, 1, project.name); if (sqlite3_step(statement) == SQLITE_ROW) localId = sqlite3_column_int64(statement, 0); }
        if (statement) sqlite3_finalize(statement); statement = nullptr;
    }
    if (ok && localId == 0) {
        ok = sqlite3_prepare_v2(db_, "INSERT INTO projects(name,department,description,status,owner_id,active) VALUES(?,?,?,?,?,?)", -1, &statement, nullptr) == SQLITE_OK;
        if (ok) { BindText(statement, 1, project.name); BindText(statement, 2, project.department); BindText(statement, 3, project.description); BindText(statement, 4, project.status); sqlite3_bind_int64(statement, 5, project.ownerId); sqlite3_bind_int(statement, 6, project.active ? 1 : 0); ok = sqlite3_step(statement) == SQLITE_DONE; }
        if (statement) sqlite3_finalize(statement); statement = nullptr;
        if (ok) localId = sqlite3_last_insert_rowid(db_);
    }
    if (ok && !linked) {
        ok = sqlite3_prepare_v2(db_, "INSERT OR IGNORE INTO sync_object_links(object_type,external_id,local_id) VALUES('project',?,?)", -1, &statement, nullptr) == SQLITE_OK;
        if (ok) { BindText(statement, 1, externalId); sqlite3_bind_int64(statement, 2, localId); ok = sqlite3_step(statement) == SQLITE_DONE; }
        if (statement) sqlite3_finalize(statement); statement = nullptr;
    }
    if (ok) {
        ok = sqlite3_prepare_v2(db_, "UPDATE projects SET name=?,department=?,description=?,status=?,owner_id=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", -1, &statement, nullptr) == SQLITE_OK;
        if (ok) { BindText(statement, 1, project.name); BindText(statement, 2, project.department); BindText(statement, 3, project.description); BindText(statement, 4, project.status); sqlite3_bind_int64(statement, 5, project.ownerId); sqlite3_bind_int(statement, 6, project.active ? 1 : 0); sqlite3_bind_int64(statement, 7, localId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"sync.project.applied", externalId);
    execute(ok ? "COMMIT" : "ROLLBACK");
    if (!ok) setError("Zentrales Projekt konnte nicht übernommen werden");
    return ok;
}

// Apply only against the last acknowledged state, so an incoming assignment
// cannot erase a local edit made while the server was unreachable.
bool EnterpriseDatabase::applySyncedProjectAssignment(const std::wstring& projectExternalId,const std::wstring& taskExternalId,bool present,const std::wstring& actor) {
    if(projectExternalId.empty()||taskExternalId.empty()){error_=L"Ungültige zentrale Projektzuordnung.";return false;}
    if(!execute("SAVEPOINT desktop_assignment_apply"))return false;
    const int64_t project=syncedLocalId(L"project",projectExternalId),task=syncedLocalId(L"task",taskExternalId);
    bool ok=project>0&&task>0&&execute("CREATE TABLE IF NOT EXISTS desktop_project_assignment_sync_shadow(project_id INTEGER NOT NULL,task_id INTEGER NOT NULL,present INTEGER NOT NULL,PRIMARY KEY(project_id,task_id))");
    sqlite3_stmt* st=nullptr;bool configured=false,pending=false;
    if(ok)ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_project_assignment_sync_outbox'",-1,&st,nullptr)==SQLITE_OK;
    if(ok){int result=sqlite3_step(st);configured=result==SQLITE_ROW;ok=result==SQLITE_ROW||result==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok&&configured){ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM desktop_project_assignment_sync_outbox WHERE project_id=? AND task_id=? LIMIT 1",-1,&st,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(st,1,project);sqlite3_bind_int64(st,2,task);int result=sqlite3_step(st);pending=result==SQLITE_ROW;ok=result==SQLITE_ROW||result==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok&&pending){if(execute("RELEASE desktop_assignment_apply"))return true;execute("ROLLBACK TO desktop_assignment_apply");execute("RELEASE desktop_assignment_apply");return false;}
    bool current=false,hasShadow=false,shadow=false;
    if(ok)ok=sqlite3_prepare_v2(db_,"SELECT EXISTS(SELECT 1 FROM project_tasks WHERE project_id=?1 AND task_id=?2),(SELECT present FROM desktop_project_assignment_sync_shadow WHERE project_id=?1 AND task_id=?2)",-1,&st,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_int64(st,1,project);sqlite3_bind_int64(st,2,task);if(sqlite3_step(st)==SQLITE_ROW){current=sqlite3_column_int(st,0)!=0;hasShadow=sqlite3_column_type(st,1)!=SQLITE_NULL;shadow=sqlite3_column_int(st,1)!=0;}else ok=false;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok&&((hasShadow&&current!=shadow)||(!hasShadow&&current))){execute("ROLLBACK TO desktop_assignment_apply");execute("RELEASE desktop_assignment_apply");error_=L"Projektzuordnungskonflikt: Lokale Änderung hat keinen bestätigten Serverstand.";return false;}
    if(ok&&hasShadow&&shadow==present&&current==present){if(execute("RELEASE desktop_assignment_apply"))return true;execute("ROLLBACK TO desktop_assignment_apply");execute("RELEASE desktop_assignment_apply");return false;}
    if(ok&&configured)ok=execute("UPDATE desktop_project_assignment_sync_guard SET suppressed=1 WHERE singleton=1");
    if(ok&&current!=present){const char* sql=present?"INSERT INTO project_tasks(project_id,task_id) VALUES(?,?)":"DELETE FROM project_tasks WHERE project_id=? AND task_id=?";
        ok=sqlite3_prepare_v2(db_,sql,-1,&st,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(st,1,project);sqlite3_bind_int64(st,2,task);ok=sqlite3_step(st)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok){ok=sqlite3_prepare_v2(db_,"INSERT INTO desktop_project_assignment_sync_shadow VALUES(?,?,?) ON CONFLICT(project_id,task_id) DO UPDATE SET present=excluded.present",-1,&st,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(st,1,project);sqlite3_bind_int64(st,2,task);sqlite3_bind_int(st,3,present?1:0);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok&&configured)ok=execute("UPDATE desktop_project_assignment_sync_guard SET suppressed=0 WHERE singleton=1");
    if(ok)ok=audit(actor,L"sync.project_assignment.applied",projectExternalId+L":"+taskExternalId);
    if(ok)ok=execute("RELEASE desktop_assignment_apply");if(!ok){execute("ROLLBACK TO desktop_assignment_apply");execute("RELEASE desktop_assignment_apply");setError("Zentrale Projektzuordnung konnte nicht übernommen werden");return false;}return true;
}

// A complete server snapshot is applied as one transaction. Existing queued
// local changes remain authoritative until their compare-and-swap is accepted.
bool EnterpriseDatabase::applySyncedProjectAssignmentSnapshot(const std::wstring& snapshotJson,bool& changed) {
    changed=false;if(snapshotJson.empty()){error_=L"Ungültiger Projektzuordnungsbestand.";return false;}
    sqlite3_stmt* st=nullptr;bool ok=sqlite3_prepare_v2(db_,"SELECT json_valid(?1) AND json_type(?1)='array'",-1,&st,nullptr)==SQLITE_OK;
    if(ok){BindText(st,1,snapshotJson);ok=sqlite3_step(st)==SQLITE_ROW&&sqlite3_column_int(st,0)==1;}if(st)sqlite3_finalize(st);st=nullptr;
    std::set<std::pair<std::wstring,std::wstring>> edges;
    if(ok)ok=sqlite3_prepare_v2(db_,"SELECT json_type(j.value,'$.projectId'),json_type(j.value,'$.taskId'),json_extract(j.value,'$.projectId'),json_extract(j.value,'$.taskId') FROM json_each(?1) j",-1,&st,nullptr)==SQLITE_OK;
    if(ok){BindText(st,1,snapshotJson);int result;while((result=sqlite3_step(st))==SQLITE_ROW){const auto* a=sqlite3_column_text(st,0);const auto* b=sqlite3_column_text(st,1);std::wstring project=Wide(sqlite3_column_text(st,2)),task=Wide(sqlite3_column_text(st,3));if(!a||!b||std::strcmp(reinterpret_cast<const char*>(a),"text")||std::strcmp(reinterpret_cast<const char*>(b),"text")||project.empty()||task.empty()||edges.size()>=100000||!edges.emplace(project,task).second){ok=false;break;}}if(result!=SQLITE_DONE)ok=false;}
    if(st)sqlite3_finalize(st);st=nullptr;if(!ok){error_=L"Ungültiger zentraler Projektzuordnungsbestand.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;const int before=sqlite3_total_changes(db_);
    for(const auto& edge:edges){if(!applySyncedProjectAssignment(edge.first,edge.second,true,L"sync.remote")){ok=false;break;}}
    std::vector<std::pair<std::wstring,std::wstring>> removed;
    if(ok)ok=execute("CREATE TABLE IF NOT EXISTS desktop_project_assignment_sync_shadow(project_id INTEGER NOT NULL,task_id INTEGER NOT NULL,present INTEGER NOT NULL,PRIMARY KEY(project_id,task_id))");
    if(ok)ok=sqlite3_prepare_v2(db_,"SELECT p.external_id,t.external_id FROM desktop_project_assignment_sync_shadow s LEFT JOIN sync_object_links p ON p.object_type='project' AND p.local_id=s.project_id LEFT JOIN sync_object_links t ON t.object_type='task' AND t.local_id=s.task_id WHERE s.present=1",-1,&st,nullptr)==SQLITE_OK;
    if(ok){int result;while((result=sqlite3_step(st))==SQLITE_ROW){std::wstring p=Wide(sqlite3_column_text(st,0)),t=Wide(sqlite3_column_text(st,1));if(p.empty()||t.empty()){ok=false;break;}if(!edges.count({p,t}))removed.emplace_back(p,t);}if(result!=SQLITE_DONE)ok=false;}if(st)sqlite3_finalize(st);st=nullptr;
    for(const auto& edge:removed){if(!ok)break;if(!applySyncedProjectAssignment(edge.first,edge.second,false,L"sync.remote"))ok=false;}
    const bool anyChanges=sqlite3_total_changes(db_)!=before;
    if(ok)ok=execute("COMMIT");if(!ok){execute("ROLLBACK");error_=L"Der Projektzuordnungsbestand konnte nicht vollständig übernommen werden.";return false;}changed=anyChanges;return true;
}

bool EnterpriseDatabase::assignTaskToProject(int64_t taskId, int64_t projectId, const std::wstring& actor) {
    if (taskId <= 0 || projectId <= 0) { error_ = L"Ungültige Aufgaben- oder Projektzuordnung."; return false; }
    if (!execute("BEGIN IMMEDIATE")) { setError("Projektzuordnung konnte nicht gestartet werden"); return false; }
    sqlite3_stmt* statement = nullptr;
    const char* sql = "INSERT OR IGNORE INTO project_tasks(project_id,task_id) SELECT ?,? WHERE EXISTS(SELECT 1 FROM projects WHERE id=? AND active=1) AND EXISTS(SELECT 1 FROM tasks WHERE id=? AND active=1)";
    bool ok = sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, projectId); sqlite3_bind_int64(statement, 2, taskId); sqlite3_bind_int64(statement, 3, projectId); sqlite3_bind_int64(statement, 4, taskId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"project.task.assigned", std::to_wstring(projectId) + L":" + std::to_wstring(taskId));
    if (ok) { if (!execute("COMMIT")) { execute("ROLLBACK"); ok = false; } } else execute("ROLLBACK");
    if (!ok) setError("Aufgabe konnte nicht dem Projekt zugeordnet werden"); return ok;
}

bool EnterpriseDatabase::setTaskProject(int64_t taskId, int64_t projectId, const std::wstring& actor) {
    if (taskId <= 0 || projectId < 0 || !execute("BEGIN IMMEDIATE")) { error_ = L"Projektzuordnung konnte nicht geändert werden"; return false; }
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "SELECT 1 FROM tasks WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, taskId); ok = sqlite3_step(statement) == SQLITE_ROW; }
    if (statement) sqlite3_finalize(statement);
    if (ok && projectId > 0) { statement = nullptr; ok = sqlite3_prepare_v2(db_, "SELECT 1 FROM projects WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK; if (ok) { sqlite3_bind_int64(statement, 1, projectId); ok = sqlite3_step(statement) == SQLITE_ROW; } if (statement) sqlite3_finalize(statement); }
    bool multipleProjects=false;
    if(ok){statement=nullptr;ok=sqlite3_prepare_v2(db_,"SELECT count(*) FROM project_tasks WHERE task_id=?",-1,&statement,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(statement,1,taskId);ok=sqlite3_step(statement)==SQLITE_ROW;if(ok)multipleProjects=sqlite3_column_int64(statement,0)>1;}if(statement)sqlite3_finalize(statement);if(multipleProjects)ok=false;}
    if (ok) ok = execute(("DELETE FROM project_tasks WHERE task_id=" + std::to_string(taskId)).c_str());
    if (ok && projectId > 0) { statement = nullptr; ok = sqlite3_prepare_v2(db_, "INSERT INTO project_tasks(project_id,task_id) VALUES(?,?)", -1, &statement, nullptr) == SQLITE_OK; if (ok) { sqlite3_bind_int64(statement, 1, projectId); sqlite3_bind_int64(statement, 2, taskId); ok = sqlite3_step(statement) == SQLITE_DONE; } if (statement) sqlite3_finalize(statement); }
    if (ok) ok = audit(actor, L"project.task.reassigned", std::to_wstring(projectId) + L":" + std::to_wstring(taskId));
    execute(ok ? "COMMIT" : "ROLLBACK");
    if (!ok) { if(multipleProjects)error_=L"Die Aufgabe ist mehreren Projekten zugeordnet. Bitte die Zuordnungen einzeln bearbeiten."; else setError("Projektzuordnung konnte nicht geändert werden"); } return ok;
}

std::vector<EnterpriseTask> EnterpriseDatabase::tasksForProject(int64_t projectId, bool includeArchived) {
    std::vector<EnterpriseTask> result;
    const char* sql = includeArchived
        ? "SELECT t.id,t.title,t.department,t.priority,t.status,t.assignee_id,t.active,COALESCE(s.due_at,0),COALESCE(s.recurrence_days,0) FROM tasks t JOIN project_tasks pt ON pt.task_id=t.id LEFT JOIN task_schedules s ON s.task_id=t.id WHERE pt.project_id=? ORDER BY t.active DESC,t.updated_at DESC"
        : "SELECT t.id,t.title,t.department,t.priority,t.status,t.assignee_id,t.active,COALESCE(s.due_at,0),COALESCE(s.recurrence_days,0) FROM tasks t JOIN project_tasks pt ON pt.task_id=t.id LEFT JOIN task_schedules s ON s.task_id=t.id WHERE pt.project_id=? AND t.active=1 ORDER BY t.updated_at DESC";
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) != SQLITE_OK) { setError("Projektaufgaben konnten nicht geladen werden"); return result; }
    sqlite3_bind_int64(statement, 1, projectId);
    while (sqlite3_step(statement) == SQLITE_ROW) result.push_back({sqlite3_column_int64(statement, 0), Wide(sqlite3_column_text(statement, 1)), Wide(sqlite3_column_text(statement, 2)), Wide(sqlite3_column_text(statement, 3)), Wide(sqlite3_column_text(statement, 4)), sqlite3_column_int64(statement, 5), sqlite3_column_int(statement, 6) != 0, sqlite3_column_int64(statement, 7), sqlite3_column_int(statement, 8)});
    sqlite3_finalize(statement); return result;
}

bool EnterpriseDatabase::createTask(const EnterpriseTask& task, int64_t& newId, const std::wstring& actor, int64_t projectId) {
    newId=0;
    if (task.title.empty() || task.department.empty() || task.priority.empty() || task.dueAt < 0 || task.recurrenceDays < 0 || task.recurrenceDays > 3660 || projectId<0) { error_ = L"Ungültige Aufgabendaten."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "INSERT INTO tasks(title,department,priority,status,assignee_id) VALUES(?,?,?,?,?)", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, task.title); BindText(statement, 2, task.department); BindText(statement, 3, task.priority); BindText(statement, 4, task.status.empty() ? L"open" : task.status); sqlite3_bind_int64(statement, 5, task.assigneeId); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    if (ok) { newId = sqlite3_last_insert_rowid(db_); statement = nullptr; ok = sqlite3_prepare_v2(db_, "INSERT INTO task_schedules(task_id,due_at,recurrence_days) VALUES(?,?,?)", -1, &statement, nullptr) == SQLITE_OK; if (ok) { sqlite3_bind_int64(statement, 1, newId); sqlite3_bind_int64(statement, 2, task.dueAt); sqlite3_bind_int(statement, 3, task.recurrenceDays); ok = sqlite3_step(statement) == SQLITE_DONE; } if (statement) sqlite3_finalize(statement); }
    if(ok && projectId>0){statement=nullptr;ok=sqlite3_prepare_v2(db_,"INSERT INTO project_tasks(project_id,task_id) SELECT id,? FROM projects WHERE id=? AND active=1",-1,&statement,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(statement,1,newId);sqlite3_bind_int64(statement,2,projectId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);}
    if (ok) ok = audit(actor, L"task.created", task.title);
    if(ok && projectId>0)ok=audit(actor,L"project.task.reassigned",std::to_wstring(projectId)+L":"+std::to_wstring(newId));
    if(ok)ok=execute("COMMIT");
    if (!ok) {execute("ROLLBACK");newId=0;setError("Aufgabe konnte nicht angelegt werden");} return ok;
}

bool EnterpriseDatabase::updateTask(const EnterpriseTask& task, const std::wstring& actor, int64_t expectedProject, int64_t projectId) {
    if (task.id <= 0 || task.title.empty() || task.department.empty() || task.dueAt < 0 || task.recurrenceDays < 0 || task.recurrenceDays > 3660 || (task.priority != L"Niedrig" && task.priority != L"Mittel" && task.priority != L"Hoch" && task.priority != L"Kritisch")) { error_ = L"Ungültige Aufgabendaten."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr; bool ok=true;
    if(expectedProject>=0 && projectId>=0){
        ok=sqlite3_prepare_v2(db_,"SELECT count(*),COALESCE(min(project_id),0) FROM project_tasks WHERE task_id=?",-1,&statement,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(statement,1,task.id);ok=sqlite3_step(statement)==SQLITE_ROW && sqlite3_column_int64(statement,0)<=1 && sqlite3_column_int64(statement,1)==expectedProject;}
        if(statement)sqlite3_finalize(statement);statement=nullptr;
    } else if(expectedProject!=-1 || projectId!=-1)ok=false;
    if(ok)ok = sqlite3_prepare_v2(db_, "UPDATE tasks SET title=?,department=?,priority=?,assignee_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, task.title); BindText(statement, 2, task.department); BindText(statement, 3, task.priority); sqlite3_bind_int64(statement, 4, task.assigneeId); sqlite3_bind_int64(statement, 5, task.id); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    statement=nullptr;
    if(ok && expectedProject>=0 && projectId!=expectedProject){
        ok=sqlite3_prepare_v2(db_,"DELETE FROM project_tasks WHERE task_id=?",-1,&statement,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(statement,1,task.id);ok=sqlite3_step(statement)==SQLITE_DONE;}if(statement)sqlite3_finalize(statement);statement=nullptr;
        if(ok && projectId>0){ok=sqlite3_prepare_v2(db_,"INSERT INTO project_tasks(project_id,task_id) SELECT id,? FROM projects WHERE id=? AND active=1",-1,&statement,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(statement,1,task.id);sqlite3_bind_int64(statement,2,projectId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);statement=nullptr;}
        if(ok)ok=audit(actor,L"project.task.reassigned",std::to_wstring(projectId)+L":"+std::to_wstring(task.id));
    }
    statement = nullptr; if (ok) ok = sqlite3_prepare_v2(db_, "INSERT INTO task_schedules(task_id,due_at,recurrence_days) VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET due_at=excluded.due_at,recurrence_days=excluded.recurrence_days", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, task.id); sqlite3_bind_int64(statement, 2, task.dueAt); sqlite3_bind_int(statement, 3, task.recurrenceDays); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"task.updated", std::to_wstring(task.id));
    if(ok)ok=execute("COMMIT");
    if (!ok) {execute("ROLLBACK");setError("Aufgabe konnte nicht bearbeitet werden; Projektzuordnung prüfen und Ansicht aktualisieren");} return ok;
}

bool EnterpriseDatabase::desktopTaskRecurrenceLinks(const std::wstring& clientId,const std::wstring& serverUrl,int64_t serverActor,std::vector<std::tuple<int64_t,std::wstring,std::wstring>>& links) {
    links.clear();if(clientId.empty()||serverUrl.empty()||serverActor<=0||!execute("BEGIN IMMEDIATE")){error_=L"Ungültige Folgeaufgaben-Synchronisation.";return false;}
    bool ok=execute("CREATE TABLE IF NOT EXISTS desktop_task_recurrence_binding(singleton INTEGER PRIMARY KEY CHECK(singleton=1),client_id TEXT NOT NULL,server_url TEXT NOT NULL,server_actor INTEGER NOT NULL)");sqlite3_stmt* st=nullptr;
    const char* query="SELECT a.id,a.subject,s.external_id,n.external_id,s.local_id,n.local_id FROM audit_log a LEFT JOIN sync_object_links s ON s.object_type='task' AND s.local_id=CAST(substr(a.subject,1,instr(a.subject,'->')-1) AS INTEGER) LEFT JOIN sync_object_links n ON n.object_type='task' AND n.local_id=CAST(substr(a.subject,instr(a.subject,'->')+2) AS INTEGER) WHERE a.action='task.recurrence.created' ORDER BY a.id";
    if(ok)ok=sqlite3_prepare_v2(db_,query,-1,&st,nullptr)==SQLITE_OK;
    if(ok){int step;while((step=sqlite3_step(st))==SQLITE_ROW){const auto id=sqlite3_column_int64(st,0),sourceId=sqlite3_column_int64(st,4),successorId=sqlite3_column_int64(st,5);const auto subject=Wide(sqlite3_column_text(st,1)),source=Wide(sqlite3_column_text(st,2)),successor=Wide(sqlite3_column_text(st,3));if(id<=0||sourceId<=0||successorId<=0||subject!=std::to_wstring(sourceId)+L"->"+std::to_wstring(successorId)||source.empty()||successor.empty()||source==successor){ok=false;break;}links.emplace_back(id,source,successor);}if(ok)ok=step==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok&&!links.empty())ok=sqlite3_prepare_v2(db_,"INSERT OR IGNORE INTO desktop_task_recurrence_binding VALUES(1,?,?,?)",-1,&st,nullptr)==SQLITE_OK;
    if(ok&&!links.empty()){BindText(st,1,clientId);BindText(st,2,serverUrl);sqlite3_bind_int64(st,3,serverActor);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok&&!links.empty())ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM desktop_task_recurrence_binding WHERE singleton=1 AND client_id=? AND server_url=? AND server_actor=?",-1,&st,nullptr)==SQLITE_OK;
    if(ok&&!links.empty()){BindText(st,1,clientId);BindText(st,2,serverUrl);sqlite3_bind_int64(st,3,serverActor);ok=sqlite3_step(st)==SQLITE_ROW;}if(st)sqlite3_finalize(st);
    if(ok)ok=execute("COMMIT");if(!ok){execute("ROLLBACK");links.clear();error_=L"Folgeaufgaben fehlen oder sind an eine andere Serververbindung gebunden.";}return ok;
}

bool EnterpriseDatabase::applySyncedTaskRecurrenceSnapshot(const std::wstring& snapshotJson,bool& changed) {
    changed=false;sqlite3_stmt* st=nullptr;bool ok=sqlite3_prepare_v2(db_,"SELECT json_valid(?1) AND json_type(?1)='array'",-1,&st,nullptr)==SQLITE_OK;
    if(ok){BindText(st,1,snapshotJson);ok=sqlite3_step(st)==SQLITE_ROW&&sqlite3_column_int(st,0)==1;}if(st)sqlite3_finalize(st);st=nullptr;
    std::vector<std::pair<std::wstring,std::wstring>> links;std::set<std::wstring> sources,successors;
    if(ok)ok=sqlite3_prepare_v2(db_,"SELECT json_type(j.value,'$.sourceId'),json_type(j.value,'$.successorId'),json_extract(j.value,'$.sourceId'),json_extract(j.value,'$.successorId') FROM json_each(?1) j",-1,&st,nullptr)==SQLITE_OK;
    if(ok){BindText(st,1,snapshotJson);int step;while((step=sqlite3_step(st))==SQLITE_ROW){const auto* a=sqlite3_column_text(st,0);const auto* b=sqlite3_column_text(st,1);auto source=Wide(sqlite3_column_text(st,2)),successor=Wide(sqlite3_column_text(st,3));if(!a||!b||std::strcmp(reinterpret_cast<const char*>(a),"text")||std::strcmp(reinterpret_cast<const char*>(b),"text")||source.empty()||successor.empty()||source==successor||links.size()>=100000||!sources.insert(source).second||!successors.insert(successor).second){ok=false;break;}links.emplace_back(source,successor);}if(ok)ok=step==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
    if(!ok){error_=L"Ungültiger Folgeaufgabenbestand.";return false;}if(!execute("BEGIN IMMEDIATE"))return false;
    bool any=false;
    for(const auto& link:links){const auto source=syncedLocalId(L"task",link.first),successor=syncedLocalId(L"task",link.second);if(source<=0||successor<=0||source==successor){ok=false;break;}
        const auto subject=std::to_wstring(source)+L"->"+std::to_wstring(successor);const auto pattern=std::to_string(source)+"->%";bool exists=false;
        ok=sqlite3_prepare_v2(db_,"SELECT subject FROM audit_log WHERE action='task.recurrence.created' AND subject LIKE ?",-1,&st,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_text(st,1,pattern.c_str(),-1,SQLITE_TRANSIENT);int step;while((step=sqlite3_step(st))==SQLITE_ROW){if(Wide(sqlite3_column_text(st,0))!=subject){ok=false;break;}exists=true;}if(ok)ok=step==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;if(!ok)break;
        if(!exists){ok=audit(L"sync.remote",L"task.recurrence.created",subject);if(!ok)break;any=true;}
    }
    if(ok)ok=execute("COMMIT");if(!ok){execute("ROLLBACK");error_=L"Folgeaufgabenbestand enthält fehlende oder widersprüchliche Zuordnungen.";return false;}changed=any;return true;
}

bool EnterpriseDatabase::applySyncedTaskRecurrence(const std::wstring& sourceExternalId,const std::wstring& successorExternalId,const std::wstring& actor) {
    if(sourceExternalId.empty()||successorExternalId.empty()||sourceExternalId==successorExternalId){error_=L"Ungültige Folgeaufgabenzuordnung.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    const int64_t source=syncedLocalId(L"task",sourceExternalId),successor=syncedLocalId(L"task",successorExternalId);
    bool ok=source>0&&successor>0&&source!=successor,exists=false;sqlite3_stmt* st=nullptr;
    const std::string expected=std::to_string(source)+"->"+std::to_string(successor),pattern=std::to_string(source)+"->%";
    if(ok)ok=sqlite3_prepare_v2(db_,"SELECT subject FROM audit_log WHERE action='task.recurrence.created' AND subject LIKE ?",-1,&st,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_text(st,1,pattern.c_str(),-1,SQLITE_TRANSIENT);int step;while((step=sqlite3_step(st))==SQLITE_ROW){const auto* value=sqlite3_column_text(st,0);if(!value||expected!=reinterpret_cast<const char*>(value)){ok=false;break;}exists=true;}if(ok)ok=step==SQLITE_DONE;}if(st)sqlite3_finalize(st);
    if(ok&&!exists)ok=audit(actor,L"task.recurrence.created",std::to_wstring(source)+L"->"+std::to_wstring(successor));
    if(ok)ok=execute("COMMIT");if(!ok){execute("ROLLBACK");error_=L"Folgeaufgabe fehlt lokal oder eine andere Folgeaufgabe ist bereits zugeordnet.";}return ok;
}

bool EnterpriseDatabase::desktopMaintenanceRecurrenceLinks(const std::wstring& clientId,const std::wstring& serverUrl,int64_t serverActor,std::vector<std::tuple<int64_t,std::wstring,std::wstring>>& links) {
    links.clear();if(clientId.empty()||serverUrl.empty()||serverActor<=0||!execute("BEGIN IMMEDIATE")){error_=L"Ungültige Folgewartungsaufträge-Synchronisation.";return false;}
    bool ok=execute("CREATE TABLE IF NOT EXISTS desktop_maintenance_recurrence_binding(singleton INTEGER PRIMARY KEY CHECK(singleton=1),client_id TEXT NOT NULL,server_url TEXT NOT NULL,server_actor INTEGER NOT NULL)");sqlite3_stmt* st=nullptr;
    const char* query="SELECT a.id,a.subject,s.external_id,n.external_id,s.local_id,n.local_id FROM audit_log a LEFT JOIN sync_object_links s ON s.object_type='maintenance_order' AND s.local_id=CAST(substr(a.subject,1,instr(a.subject,'->')-1) AS INTEGER) LEFT JOIN sync_object_links n ON n.object_type='maintenance_order' AND n.local_id=CAST(substr(a.subject,instr(a.subject,'->')+2) AS INTEGER) WHERE a.action='maintenance_order.recurrence.created' ORDER BY a.id";
    if(ok)ok=sqlite3_prepare_v2(db_,query,-1,&st,nullptr)==SQLITE_OK;
    if(ok){int step;while((step=sqlite3_step(st))==SQLITE_ROW){const auto id=sqlite3_column_int64(st,0),sourceId=sqlite3_column_int64(st,4),successorId=sqlite3_column_int64(st,5);const auto subject=Wide(sqlite3_column_text(st,1)),source=Wide(sqlite3_column_text(st,2)),successor=Wide(sqlite3_column_text(st,3));if(id<=0||sourceId<=0||successorId<=0||subject!=std::to_wstring(sourceId)+L"->"+std::to_wstring(successorId)||source.empty()||successor.empty()||source==successor){ok=false;break;}links.emplace_back(id,source,successor);}if(ok)ok=step==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok&&!links.empty())ok=sqlite3_prepare_v2(db_,"INSERT OR IGNORE INTO desktop_maintenance_recurrence_binding VALUES(1,?,?,?)",-1,&st,nullptr)==SQLITE_OK;
    if(ok&&!links.empty()){BindText(st,1,clientId);BindText(st,2,serverUrl);sqlite3_bind_int64(st,3,serverActor);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok&&!links.empty())ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM desktop_maintenance_recurrence_binding WHERE singleton=1 AND client_id=? AND server_url=? AND server_actor=?",-1,&st,nullptr)==SQLITE_OK;
    if(ok&&!links.empty()){BindText(st,1,clientId);BindText(st,2,serverUrl);sqlite3_bind_int64(st,3,serverActor);ok=sqlite3_step(st)==SQLITE_ROW;}if(st)sqlite3_finalize(st);
    if(ok)ok=execute("COMMIT");if(!ok){execute("ROLLBACK");links.clear();error_=L"Folgewartungsaufträge fehlen oder sind an eine andere Serververbindung gebunden.";}return ok;
}

bool EnterpriseDatabase::applySyncedMaintenanceRecurrenceSnapshot(const std::wstring& snapshotJson,bool& changed) {
    changed=false;sqlite3_stmt* st=nullptr;bool ok=sqlite3_prepare_v2(db_,"SELECT json_valid(?1) AND json_type(?1)='array'",-1,&st,nullptr)==SQLITE_OK;
    if(ok){BindText(st,1,snapshotJson);ok=sqlite3_step(st)==SQLITE_ROW&&sqlite3_column_int(st,0)==1;}if(st)sqlite3_finalize(st);st=nullptr;
    std::vector<std::pair<std::wstring,std::wstring>> links;std::set<std::wstring> sources,successors;
    if(ok)ok=sqlite3_prepare_v2(db_,"SELECT json_type(j.value,'$.sourceId'),json_type(j.value,'$.successorId'),json_extract(j.value,'$.sourceId'),json_extract(j.value,'$.successorId') FROM json_each(?1) j",-1,&st,nullptr)==SQLITE_OK;
    if(ok){BindText(st,1,snapshotJson);int step;while((step=sqlite3_step(st))==SQLITE_ROW){const auto* a=sqlite3_column_text(st,0);const auto* b=sqlite3_column_text(st,1);auto source=Wide(sqlite3_column_text(st,2)),successor=Wide(sqlite3_column_text(st,3));if(!a||!b||std::strcmp(reinterpret_cast<const char*>(a),"text")||std::strcmp(reinterpret_cast<const char*>(b),"text")||source.empty()||successor.empty()||source==successor||links.size()>=100000||!sources.insert(source).second||!successors.insert(successor).second){ok=false;break;}links.emplace_back(source,successor);}if(ok)ok=step==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
    if(!ok){error_=L"Ungültiger Wartungsfolge-Bestand.";return false;}if(!execute("BEGIN IMMEDIATE"))return false;
    bool any=false;
    for(const auto& link:links){const auto source=syncedLocalId(L"maintenance_order",link.first),successor=syncedLocalId(L"maintenance_order",link.second);if(source<=0||successor<=0||source==successor){ok=false;break;}
        const auto subject=std::to_wstring(source)+L"->"+std::to_wstring(successor);const auto pattern=std::to_string(source)+"->%";bool exists=false;
        ok=sqlite3_prepare_v2(db_,"SELECT subject FROM audit_log WHERE action='maintenance_order.recurrence.created' AND subject LIKE ?",-1,&st,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_text(st,1,pattern.c_str(),-1,SQLITE_TRANSIENT);int step;while((step=sqlite3_step(st))==SQLITE_ROW){if(Wide(sqlite3_column_text(st,0))!=subject){ok=false;break;}exists=true;}if(ok)ok=step==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;if(!ok)break;
        if(!exists){ok=audit(L"sync.remote",L"maintenance_order.recurrence.created",subject);if(!ok)break;any=true;}
    }
    if(ok)ok=execute("COMMIT");if(!ok){execute("ROLLBACK");error_=L"Wartungsfolge-Bestand enthält fehlende oder widersprüchliche Zuordnungen.";return false;}changed=any;return true;
}

bool EnterpriseDatabase::applySyncedMaintenanceRecurrence(const std::wstring& sourceExternalId,const std::wstring& successorExternalId,const std::wstring& actor) {
    if(sourceExternalId.empty()||successorExternalId.empty()||sourceExternalId==successorExternalId){error_=L"Ungültige Wartungsfolge-Zuordnung.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    const int64_t source=syncedLocalId(L"maintenance_order",sourceExternalId),successor=syncedLocalId(L"maintenance_order",successorExternalId);
    bool ok=source>0&&successor>0&&source!=successor,exists=false;sqlite3_stmt* st=nullptr;
    const std::string expected=std::to_string(source)+"->"+std::to_string(successor),pattern=std::to_string(source)+"->%";
    if(ok)ok=sqlite3_prepare_v2(db_,"SELECT subject FROM audit_log WHERE action='maintenance_order.recurrence.created' AND subject LIKE ?",-1,&st,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_text(st,1,pattern.c_str(),-1,SQLITE_TRANSIENT);int step;while((step=sqlite3_step(st))==SQLITE_ROW){const auto* value=sqlite3_column_text(st,0);if(!value||expected!=reinterpret_cast<const char*>(value)){ok=false;break;}exists=true;}if(ok)ok=step==SQLITE_DONE;}if(st)sqlite3_finalize(st);
    if(ok&&!exists)ok=audit(actor,L"maintenance_order.recurrence.created",std::to_wstring(source)+L"->"+std::to_wstring(successor));
    if(ok)ok=execute("COMMIT");if(!ok){execute("ROLLBACK");error_=L"Folgewartungsauftrag fehlt lokal oder ein anderer Folgewartungsauftrag ist bereits zugeordnet.";}return ok;
}

bool EnterpriseDatabase::updateTaskStatus(int64_t taskId, const std::wstring& status, const std::wstring& actor, int64_t* repeatedTask) {
    if (repeatedTask) *repeatedTask=0;
    if (status != L"open" && status != L"in_progress" && status != L"done" && status != L"cancelled") { error_ = L"Ungültiger Aufgabenstatus."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    if (status == L"done") {
        sqlite3_stmt* dependencyCheck = nullptr;
        bool blocked = false;
        if (sqlite3_prepare_v2(db_, "SELECT 1 FROM task_dependencies d JOIN tasks prerequisite ON prerequisite.id=d.depends_on_task_id WHERE d.task_id=? AND prerequisite.active=1 AND prerequisite.status<>'done' LIMIT 1", -1, &dependencyCheck, nullptr) != SQLITE_OK) { execute("ROLLBACK"); setError("Aufgabenabhängigkeiten konnten nicht geprüft werden"); return false; }
        sqlite3_bind_int64(dependencyCheck, 1, taskId);
        blocked = sqlite3_step(dependencyCheck) == SQLITE_ROW;
        sqlite3_finalize(dependencyCheck);
        if (!blocked) {
            sqlite3_stmt* subtaskCheck = nullptr;
            if (sqlite3_prepare_v2(db_, "SELECT 1 FROM task_subtasks WHERE task_id=? AND active=1 AND status NOT IN ('done','cancelled') LIMIT 1", -1, &subtaskCheck, nullptr) != SQLITE_OK) { execute("ROLLBACK"); setError("Unteraufgaben konnten nicht geprüft werden"); return false; }
            sqlite3_bind_int64(subtaskCheck, 1, taskId); blocked = sqlite3_step(subtaskCheck) == SQLITE_ROW; sqlite3_finalize(subtaskCheck);
        }
        if (blocked) { execute("ROLLBACK"); error_ = L"Die Aufgabe kann erst nach Abschluss aller Voraussetzungen und Unteraufgaben erledigt werden."; return false; }
    }
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE tasks SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, status); sqlite3_bind_int64(statement, 2, taskId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    int64_t repeatedTaskId = 0;
    if (ok && status == L"done") {
        EnterpriseTask source;
        statement = nullptr;
        ok = sqlite3_prepare_v2(db_, "SELECT t.id,t.title,t.department,t.priority,t.status,t.assignee_id,t.active,COALESCE(s.due_at,0),COALESCE(s.recurrence_days,0) FROM tasks t LEFT JOIN task_schedules s ON s.task_id=t.id WHERE t.id=?", -1, &statement, nullptr) == SQLITE_OK;
        if (ok) { sqlite3_bind_int64(statement, 1, taskId); if (sqlite3_step(statement) == SQLITE_ROW) source = {sqlite3_column_int64(statement,0),Wide(sqlite3_column_text(statement,1)),Wide(sqlite3_column_text(statement,2)),Wide(sqlite3_column_text(statement,3)),Wide(sqlite3_column_text(statement,4)),sqlite3_column_int64(statement,5),sqlite3_column_int(statement,6)!=0,sqlite3_column_int64(statement,7),sqlite3_column_int(statement,8)}; else ok = false; }
        if (statement) sqlite3_finalize(statement);
        bool alreadyGenerated=false;
        if(ok && source.recurrenceDays>0){statement=nullptr;ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM audit_log WHERE action='task.recurrence.created' AND subject LIKE ? LIMIT 1",-1,&statement,nullptr)==SQLITE_OK;if(ok){auto pattern=std::to_string(taskId)+"->%";sqlite3_bind_text(statement,1,pattern.c_str(),-1,SQLITE_TRANSIENT);const int result=sqlite3_step(statement);alreadyGenerated=result==SQLITE_ROW;ok=result==SQLITE_ROW||result==SQLITE_DONE;}if(statement)sqlite3_finalize(statement);}
        if (ok && source.recurrenceDays > 0 && !alreadyGenerated) {
            const int64_t delta=static_cast<int64_t>(source.recurrenceDays)*86400;
            const int64_t base=source.dueAt>0?source.dueAt:static_cast<int64_t>(std::time(nullptr));
            if(source.recurrenceDays>3660 || base<0 || base>std::numeric_limits<int64_t>::max()-delta){execute("ROLLBACK");error_=L"Die nächste Aufgabenfälligkeit liegt außerhalb des unterstützten Zeitbereichs.";return false;}
            statement=nullptr;bool valid=sqlite3_prepare_v2(db_,"SELECT 1 FROM task_subtasks WHERE task_id=? AND active=1 AND status<>'cancelled' AND due_at>? LIMIT 1",-1,&statement,nullptr)==SQLITE_OK;
            if(valid){sqlite3_bind_int64(statement,1,taskId);sqlite3_bind_int64(statement,2,std::numeric_limits<int64_t>::max()-delta);valid=sqlite3_step(statement)==SQLITE_DONE;}
            if(statement)sqlite3_finalize(statement);
            if(!valid){execute("ROLLBACK");error_=L"Eine Unteraufgabenfälligkeit liegt außerhalb des unterstützten Zeitbereichs.";return false;}
            const int64_t nextDue=base+delta;
            statement = nullptr; ok = sqlite3_prepare_v2(db_, "INSERT INTO tasks(title,department,priority,status,assignee_id) VALUES(?,?,?,'open',?)", -1, &statement, nullptr) == SQLITE_OK;
            if (ok) { BindText(statement,1,source.title); BindText(statement,2,source.department); BindText(statement,3,source.priority); sqlite3_bind_int64(statement,4,source.assigneeId); ok = sqlite3_step(statement) == SQLITE_DONE; }
            if (statement) sqlite3_finalize(statement);
            if (ok) { repeatedTaskId = sqlite3_last_insert_rowid(db_); statement = nullptr; ok = sqlite3_prepare_v2(db_, "INSERT INTO task_schedules(task_id,due_at,recurrence_days) VALUES(?,?,?)", -1, &statement, nullptr) == SQLITE_OK; if (ok) { sqlite3_bind_int64(statement,1,repeatedTaskId); sqlite3_bind_int64(statement,2,nextDue); sqlite3_bind_int(statement,3,source.recurrenceDays); ok = sqlite3_step(statement) == SQLITE_DONE; } if (statement) sqlite3_finalize(statement); }
            if (ok) { statement = nullptr; ok = sqlite3_prepare_v2(db_, "INSERT INTO project_tasks(project_id,task_id) SELECT project_id,? FROM project_tasks WHERE task_id=?", -1, &statement, nullptr) == SQLITE_OK; if (ok) { sqlite3_bind_int64(statement,1,repeatedTaskId); sqlite3_bind_int64(statement,2,taskId); ok = sqlite3_step(statement) == SQLITE_DONE; } if (statement) sqlite3_finalize(statement); }
            // A recurring work package keeps its actionable breakdown.  The
            // new cycle deliberately starts every carried subtask as open;
            // cancelled items are not resurrected.
            if (ok) { statement = nullptr; ok = sqlite3_prepare_v2(db_, "INSERT INTO task_subtasks(task_id,title,status,assignee_id,due_at) SELECT ?,title,'open',assignee_id,CASE WHEN due_at>0 THEN due_at + ? ELSE 0 END FROM task_subtasks WHERE task_id=? AND active=1 AND status<>'cancelled'", -1, &statement, nullptr) == SQLITE_OK; if (ok) { sqlite3_bind_int64(statement,1,repeatedTaskId); sqlite3_bind_int64(statement,2,static_cast<int64_t>(source.recurrenceDays)*86400); sqlite3_bind_int64(statement,3,taskId); ok = sqlite3_step(statement) == SQLITE_DONE; } if (statement) sqlite3_finalize(statement); }
        }
    }
    if (ok) ok = audit(actor, L"task.status.changed", std::to_wstring(taskId) + L":" + status);
    if (ok && repeatedTaskId > 0) ok = audit(actor, L"task.recurrence.created", std::to_wstring(taskId) + L"->" + std::to_wstring(repeatedTaskId));
    if(ok)ok=execute("COMMIT");
    if(!ok){execute("ROLLBACK");setError("Aufgabenstatus konnte nicht geändert werden");return false;}
    if(repeatedTask)*repeatedTask=repeatedTaskId;
    return true;
}

bool EnterpriseDatabase::archiveTask(int64_t taskId, const std::wstring& actor) {
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE tasks SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, taskId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"task.archived", std::to_wstring(taskId));
    if (!ok) setError("Aufgabe konnte nicht archiviert werden"); return ok;
}

bool EnterpriseDatabase::applySyncedTask(const std::wstring& externalId, const EnterpriseTask& task, const std::wstring& actor, bool schedulePresent) {
    if (externalId.empty() || task.title.empty() || task.department.empty() || task.assigneeId<=0 ||
        (task.priority!=L"Niedrig" && task.priority!=L"Mittel" && task.priority!=L"Hoch" && task.priority!=L"Kritisch") ||
        (task.status!=L"open" && task.status!=L"in_progress" && task.status!=L"done" && task.status!=L"cancelled") ||
        (schedulePresent && (task.dueAt<0 || task.recurrenceDays<0 || task.recurrenceDays>3660))) {
        error_=L"Ungültige zentrale Aufgabendaten."; return false;
    }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement=nullptr; int64_t localId=0;
    bool ok=sqlite3_prepare_v2(db_,"SELECT local_id FROM sync_object_links WHERE object_type='task' AND external_id=?",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){BindText(statement,1,externalId);int step=sqlite3_step(statement);if(step==SQLITE_ROW)localId=sqlite3_column_int64(statement,0);else if(step!=SQLITE_DONE)ok=false;}
    if(statement)sqlite3_finalize(statement);statement=nullptr;
    if(ok && localId==0){
        ok=sqlite3_prepare_v2(db_,"INSERT INTO tasks(title,department,priority,status,assignee_id,active) VALUES(?,?,?,?,?,?)",-1,&statement,nullptr)==SQLITE_OK;
        if(ok){BindText(statement,1,task.title);BindText(statement,2,task.department);BindText(statement,3,task.priority);BindText(statement,4,task.status);sqlite3_bind_int64(statement,5,task.assigneeId);sqlite3_bind_int(statement,6,task.active?1:0);ok=sqlite3_step(statement)==SQLITE_DONE;}
        if(statement)sqlite3_finalize(statement);statement=nullptr;
        if(ok){localId=sqlite3_last_insert_rowid(db_);ok=sqlite3_prepare_v2(db_,"INSERT INTO sync_object_links(object_type,external_id,local_id) VALUES('task',?,?)",-1,&statement,nullptr)==SQLITE_OK;}
        if(ok){BindText(statement,1,externalId);sqlite3_bind_int64(statement,2,localId);ok=sqlite3_step(statement)==SQLITE_DONE;}
    }else if(ok){
        ok=sqlite3_prepare_v2(db_,"UPDATE tasks SET title=?,department=?,priority=?,status=?,assignee_id=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",-1,&statement,nullptr)==SQLITE_OK;
        if(ok){BindText(statement,1,task.title);BindText(statement,2,task.department);BindText(statement,3,task.priority);BindText(statement,4,task.status);sqlite3_bind_int64(statement,5,task.assigneeId);sqlite3_bind_int(statement,6,task.active?1:0);sqlite3_bind_int64(statement,7,localId);ok=sqlite3_step(statement)==SQLITE_DONE && sqlite3_changes(db_)==1;}
    }
    if(statement)sqlite3_finalize(statement);statement=nullptr;
    if(ok && schedulePresent){
        ok=sqlite3_prepare_v2(db_,"INSERT INTO task_schedules(task_id,due_at,recurrence_days) VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET due_at=excluded.due_at,recurrence_days=excluded.recurrence_days",-1,&statement,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(statement,1,localId);sqlite3_bind_int64(statement,2,task.dueAt);sqlite3_bind_int(statement,3,task.recurrenceDays);ok=sqlite3_step(statement)==SQLITE_DONE;}
        if(statement)sqlite3_finalize(statement);
    }
    if(ok)ok=audit(actor,L"sync.task.applied",externalId);
    if(ok)ok=execute("COMMIT");
    if(!ok){execute("ROLLBACK");setError("Zentrale Aufgabe konnte nicht übernommen werden");return false;}
    return true;
}

std::vector<EnterpriseTaskDependency> EnterpriseDatabase::taskDependencies(int64_t taskId) {
    std::vector<EnterpriseTaskDependency> result;
    if (taskId <= 0) { error_ = L"Ungültige Aufgabenabhängigkeit."; return result; }
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db_, "SELECT task_id,depends_on_task_id FROM task_dependencies WHERE task_id=? ORDER BY depends_on_task_id", -1, &statement, nullptr) != SQLITE_OK) { setError("Aufgabenabhängigkeiten konnten nicht geladen werden"); return result; }
    sqlite3_bind_int64(statement, 1, taskId);
    while (sqlite3_step(statement) == SQLITE_ROW) result.push_back({sqlite3_column_int64(statement, 0), sqlite3_column_int64(statement, 1)});
    sqlite3_finalize(statement);
    return result;
}

bool EnterpriseDatabase::addTaskDependency(int64_t taskId, int64_t dependsOnTaskId, const std::wstring& actor) {
    if (taskId <= 0 || dependsOnTaskId <= 0 || taskId == dependsOnTaskId) { error_ = L"Ungültige Aufgabenabhängigkeit."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr;
    const char* cycleSql = "WITH RECURSIVE reachable(id) AS (SELECT depends_on_task_id FROM task_dependencies WHERE task_id=? UNION SELECT d.depends_on_task_id FROM task_dependencies d JOIN reachable r ON d.task_id=r.id) SELECT 1 FROM reachable WHERE id=? LIMIT 1";
    bool ok = sqlite3_prepare_v2(db_, cycleSql, -1, &statement, nullptr) == SQLITE_OK;
    bool createsCycle = false;
    if (ok) { sqlite3_bind_int64(statement, 1, dependsOnTaskId); sqlite3_bind_int64(statement, 2, taskId); createsCycle = sqlite3_step(statement) == SQLITE_ROW; }
    if (statement) sqlite3_finalize(statement);
    if (!ok || createsCycle) { execute("ROLLBACK"); error_ = createsCycle ? L"Diese Abhängigkeit würde einen Zyklus erzeugen." : L"Aufgabenabhängigkeit konnte nicht geprüft werden."; return false; }
    statement = nullptr;
    const char* insertSql = "INSERT INTO task_dependencies(task_id,depends_on_task_id) SELECT ?,? WHERE EXISTS(SELECT 1 FROM tasks WHERE id=? AND active=1) AND EXISTS(SELECT 1 FROM tasks WHERE id=? AND active=1)";
    ok = sqlite3_prepare_v2(db_, insertSql, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, taskId); sqlite3_bind_int64(statement, 2, dependsOnTaskId); sqlite3_bind_int64(statement, 3, taskId); sqlite3_bind_int64(statement, 4, dependsOnTaskId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"task.dependency.added", std::to_wstring(taskId) + L"<-" + std::to_wstring(dependsOnTaskId));
    execute(ok ? "COMMIT" : "ROLLBACK");
    if (!ok) setError("Aufgabenabhängigkeit konnte nicht gespeichert werden");
    return ok;
}

bool EnterpriseDatabase::removeTaskDependency(int64_t taskId, int64_t dependsOnTaskId, const std::wstring& actor) {
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "DELETE FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, taskId); sqlite3_bind_int64(statement, 2, dependsOnTaskId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"task.dependency.removed", std::to_wstring(taskId) + L"<-" + std::to_wstring(dependsOnTaskId));
    if (ok) ok=execute("COMMIT");
    if (!ok) { execute("ROLLBACK"); setError("Aufgabenabhängigkeit konnte nicht entfernt werden"); return false; }
    return true;
}

bool EnterpriseDatabase::configureDesktopDependencyOutbox(const std::wstring& clientId) {
    if(clientId.empty()){error_=L"Synchronisationskennung fehlt.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    bool ok=execute(R"SQL(
      CREATE TABLE IF NOT EXISTS desktop_dependency_sync_config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),client_id TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS desktop_dependency_sync_binding(singleton INTEGER PRIMARY KEY CHECK(singleton=1),server_url TEXT NOT NULL,server_actor INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS desktop_dependency_sync_guard(singleton INTEGER PRIMARY KEY CHECK(singleton=1),suppressed INTEGER NOT NULL);
      INSERT OR IGNORE INTO desktop_dependency_sync_guard VALUES(1,0);
      CREATE TABLE IF NOT EXISTS desktop_dependency_sync_shadow(task_id INTEGER NOT NULL,dependency_id INTEGER NOT NULL,present INTEGER NOT NULL,PRIMARY KEY(task_id,dependency_id));
      CREATE TABLE IF NOT EXISTS desktop_dependency_sync_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL UNIQUE,task_id INTEGER NOT NULL,dependency_id INTEGER NOT NULL,present INTEGER NOT NULL,expected INTEGER NOT NULL,wire_json TEXT);
    )SQL");
    sqlite3_stmt* st=nullptr;bool configured=false;
    if(ok)ok=sqlite3_prepare_v2(db_,"SELECT client_id FROM desktop_dependency_sync_config WHERE singleton=1",-1,&st,nullptr)==SQLITE_OK;
    if(ok){int result=sqlite3_step(st);if(result==SQLITE_ROW){configured=true;ok=Wide(sqlite3_column_text(st,0))==clientId;}else ok=result==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok&&!configured){
        ok=sqlite3_prepare_v2(db_,"INSERT INTO desktop_dependency_sync_config VALUES(1,?)",-1,&st,nullptr)==SQLITE_OK;
        if(ok){BindText(st,1,clientId);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
        if(ok)ok=execute(R"SQL(
          INSERT INTO desktop_dependency_sync_outbox(operation_id,task_id,dependency_id,present,expected)
          SELECT lower(hex(randomblob(16))),d.task_id,d.depends_on_task_id,1,0 FROM task_dependencies d LEFT JOIN desktop_dependency_sync_shadow s ON s.task_id=d.task_id AND s.dependency_id=d.depends_on_task_id WHERE s.present IS NULL OR s.present=0;
          INSERT INTO desktop_dependency_sync_outbox(operation_id,task_id,dependency_id,present,expected)
          SELECT lower(hex(randomblob(16))),s.task_id,s.dependency_id,0,1 FROM desktop_dependency_sync_shadow s WHERE s.present=1 AND NOT EXISTS(SELECT 1 FROM task_dependencies d WHERE d.task_id=s.task_id AND d.depends_on_task_id=s.dependency_id);
          CREATE TRIGGER desktop_dependency_sync_insert AFTER INSERT ON task_dependencies WHEN (SELECT suppressed FROM desktop_dependency_sync_guard WHERE singleton=1)=0 BEGIN
            INSERT INTO desktop_dependency_sync_outbox(operation_id,task_id,dependency_id,present,expected) VALUES(lower(hex(randomblob(16))),NEW.task_id,NEW.depends_on_task_id,1,0);
          END;
          CREATE TRIGGER desktop_dependency_sync_delete AFTER DELETE ON task_dependencies WHEN (SELECT suppressed FROM desktop_dependency_sync_guard WHERE singleton=1)=0 BEGIN
            INSERT INTO desktop_dependency_sync_outbox(operation_id,task_id,dependency_id,present,expected) VALUES(lower(hex(randomblob(16))),OLD.task_id,OLD.depends_on_task_id,0,1);
          END;
        )SQL");
    }
    if(ok)ok=execute("COMMIT");if(!ok){execute("ROLLBACK");error_=L"Voraussetzungenwarteschlange konnte nicht eingerichtet werden.";return false;}return true;
}

bool EnterpriseDatabase::nextDesktopDependencyOperation(std::wstring& operationId,int64_t& taskId,int64_t& dependencyId,bool& present,bool& expected,std::wstring& wireJson) {
    operationId.clear();wireJson.clear();taskId=dependencyId=0;present=expected=false;sqlite3_stmt* st=nullptr;
    bool ok=sqlite3_prepare_v2(db_,R"SQL(SELECT o.operation_id,o.task_id,o.dependency_id,o.present,o.expected,COALESCE(o.wire_json,'') FROM desktop_dependency_sync_outbox o
      WHERE NOT EXISTS(SELECT 1 FROM desktop_dependency_sync_outbox older WHERE older.task_id=o.task_id AND older.dependency_id=o.dependency_id AND older.seq<o.seq)
      ORDER BY CASE WHEN EXISTS(SELECT 1 FROM sync_object_links a WHERE a.object_type='task' AND a.local_id=o.task_id) AND EXISTS(SELECT 1 FROM sync_object_links b WHERE b.object_type='task' AND b.local_id=o.dependency_id) THEN 0 ELSE 1 END,o.seq LIMIT 1)SQL",-1,&st,nullptr)==SQLITE_OK;
    if(ok){int result=sqlite3_step(st);if(result==SQLITE_ROW){operationId=Wide(sqlite3_column_text(st,0));taskId=sqlite3_column_int64(st,1);dependencyId=sqlite3_column_int64(st,2);present=sqlite3_column_int(st,3)!=0;expected=sqlite3_column_int(st,4)!=0;wireJson=Wide(sqlite3_column_text(st,5));ok=!operationId.empty()&&taskId>0&&dependencyId>0&&present!=expected;}else ok=result==SQLITE_DONE;}
    if(st)sqlite3_finalize(st);if(!ok)error_=L"Voraussetzungenwarteschlange konnte nicht gelesen werden.";return ok;
}

bool EnterpriseDatabase::freezeDesktopDependencyOperation(const std::wstring& operationId,const std::wstring& serverUrl,int64_t serverActor,const std::wstring& wireJson) {
    if(operationId.empty()||serverUrl.empty()||serverActor<=0||wireJson.empty()){error_=L"Ungültige Übertragungsdaten.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    sqlite3_stmt* st=nullptr;
    const char* query=R"SQL(SELECT 1 FROM desktop_dependency_sync_outbox o JOIN desktop_dependency_sync_config c ON c.singleton=1 JOIN sync_object_links a ON a.object_type='task' AND a.local_id=o.task_id JOIN sync_object_links b ON b.object_type='task' AND b.local_id=o.dependency_id
      WHERE NOT EXISTS(SELECT 1 FROM desktop_dependency_sync_outbox older WHERE older.task_id=o.task_id AND older.dependency_id=o.dependency_id AND older.seq<o.seq) AND o.operation_id=?1 AND json_valid(?2) AND json_extract(?2,'$.clientId')=c.client_id AND json_extract(?2,'$.operationId')=o.operation_id AND json_extract(?2,'$.taskId')=a.external_id AND json_extract(?2,'$.dependencyId')=b.external_id AND json_type(?2,'$.present') IN ('true','false') AND json_extract(?2,'$.present')=o.present AND json_type(?2,'$.expected') IN ('true','false') AND json_extract(?2,'$.expected')=o.expected AND (o.wire_json IS NULL OR o.wire_json=?2))SQL";
    bool ok=sqlite3_prepare_v2(db_,query,-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,operationId);BindText(st,2,wireJson);ok=sqlite3_step(st)==SQLITE_ROW;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok){ok=sqlite3_prepare_v2(db_,"INSERT OR IGNORE INTO desktop_dependency_sync_binding VALUES(1,?,?)",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,serverUrl);sqlite3_bind_int64(st,2,serverActor);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok){ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM desktop_dependency_sync_binding WHERE server_url=? AND server_actor=?",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,serverUrl);sqlite3_bind_int64(st,2,serverActor);ok=sqlite3_step(st)==SQLITE_ROW;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok){ok=sqlite3_prepare_v2(db_,"UPDATE desktop_dependency_sync_outbox SET wire_json=? WHERE operation_id=?",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,wireJson);BindText(st,2,operationId);ok=sqlite3_step(st)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(st)sqlite3_finalize(st);}
    if(ok)ok=execute("COMMIT");if(!ok){execute("ROLLBACK");error_=L"Voraussetzung oder Serverbindung hat sich geändert.";return false;}return true;
}

bool EnterpriseDatabase::acknowledgeDesktopDependencyOperation(const std::wstring& operationId,const std::wstring& serverUrl,int64_t serverActor,const std::wstring& wireJson,const std::wstring& actor) {
    if(operationId.empty()||serverUrl.empty()||serverActor<=0||wireJson.empty()){error_=L"Ungültige Bestätigung.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    sqlite3_stmt* st=nullptr;int64_t task=0,dependency=0;int present=0;
    bool ok=sqlite3_prepare_v2(db_,"SELECT o.task_id,o.dependency_id,o.present FROM desktop_dependency_sync_outbox o JOIN desktop_dependency_sync_binding b ON b.singleton=1 WHERE NOT EXISTS(SELECT 1 FROM desktop_dependency_sync_outbox older WHERE older.task_id=o.task_id AND older.dependency_id=o.dependency_id AND older.seq<o.seq) AND o.operation_id=? AND o.wire_json=? AND b.server_url=? AND b.server_actor=?",-1,&st,nullptr)==SQLITE_OK;
    if(ok){BindText(st,1,operationId);BindText(st,2,wireJson);BindText(st,3,serverUrl);sqlite3_bind_int64(st,4,serverActor);ok=sqlite3_step(st)==SQLITE_ROW;if(ok){task=sqlite3_column_int64(st,0);dependency=sqlite3_column_int64(st,1);present=sqlite3_column_int(st,2);}}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok){ok=sqlite3_prepare_v2(db_,"INSERT INTO desktop_dependency_sync_shadow VALUES(?,?,?) ON CONFLICT(task_id,dependency_id) DO UPDATE SET present=excluded.present",-1,&st,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(st,1,task);sqlite3_bind_int64(st,2,dependency);sqlite3_bind_int(st,3,present);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok){ok=sqlite3_prepare_v2(db_,"DELETE FROM desktop_dependency_sync_outbox WHERE operation_id=?",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,operationId);ok=sqlite3_step(st)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(st)sqlite3_finalize(st);}
    if(ok)ok=audit(actor,L"sync.task_dependency.sent",operationId);
    if(ok)ok=execute("COMMIT");if(!ok){execute("ROLLBACK");error_=L"Voraussetzungsbestätigung konnte nicht gespeichert werden.";return false;}return true;
}

bool EnterpriseDatabase::configureDesktopProjectAssignmentOutbox(const std::wstring& clientId) {
    if(clientId.empty()){error_=L"Synchronisationskennung fehlt.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    bool ok=execute(R"SQL(
      CREATE TABLE IF NOT EXISTS desktop_project_assignment_sync_config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),client_id TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS desktop_project_assignment_sync_binding(singleton INTEGER PRIMARY KEY CHECK(singleton=1),server_url TEXT NOT NULL,server_actor INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS desktop_project_assignment_sync_guard(singleton INTEGER PRIMARY KEY CHECK(singleton=1),suppressed INTEGER NOT NULL);
      INSERT OR IGNORE INTO desktop_project_assignment_sync_guard VALUES(1,0);
      CREATE TABLE IF NOT EXISTS desktop_project_assignment_sync_shadow(project_id INTEGER NOT NULL,task_id INTEGER NOT NULL,present INTEGER NOT NULL,PRIMARY KEY(project_id,task_id));
      CREATE TABLE IF NOT EXISTS desktop_project_assignment_sync_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL UNIQUE,project_id INTEGER NOT NULL,task_id INTEGER NOT NULL,present INTEGER NOT NULL,expected INTEGER NOT NULL,wire_json TEXT);
    )SQL");
    sqlite3_stmt* st=nullptr;bool configured=false;
    if(ok)ok=sqlite3_prepare_v2(db_,"SELECT client_id FROM desktop_project_assignment_sync_config WHERE singleton=1",-1,&st,nullptr)==SQLITE_OK;
    if(ok){int result=sqlite3_step(st);if(result==SQLITE_ROW){configured=true;ok=Wide(sqlite3_column_text(st,0))==clientId;}else ok=result==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok&&!configured){
        ok=sqlite3_prepare_v2(db_,"INSERT INTO desktop_project_assignment_sync_config VALUES(1,?)",-1,&st,nullptr)==SQLITE_OK;
        if(ok){BindText(st,1,clientId);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
        if(ok)ok=execute(R"SQL(
          INSERT INTO desktop_project_assignment_sync_outbox(operation_id,project_id,task_id,present,expected)
          SELECT lower(hex(randomblob(16))),d.project_id,d.task_id,1,0 FROM project_tasks d LEFT JOIN desktop_project_assignment_sync_shadow s ON s.project_id=d.project_id AND s.task_id=d.task_id WHERE s.present IS NULL OR s.present=0;
          INSERT INTO desktop_project_assignment_sync_outbox(operation_id,project_id,task_id,present,expected)
          SELECT lower(hex(randomblob(16))),s.project_id,s.task_id,0,1 FROM desktop_project_assignment_sync_shadow s WHERE s.present=1 AND NOT EXISTS(SELECT 1 FROM project_tasks d WHERE d.project_id=s.project_id AND d.task_id=s.task_id);
          CREATE TRIGGER desktop_project_assignment_sync_insert AFTER INSERT ON project_tasks WHEN (SELECT suppressed FROM desktop_project_assignment_sync_guard WHERE singleton=1)=0 BEGIN
            INSERT INTO desktop_project_assignment_sync_outbox(operation_id,project_id,task_id,present,expected) VALUES(lower(hex(randomblob(16))),NEW.project_id,NEW.task_id,1,0);
          END;
          CREATE TRIGGER desktop_project_assignment_sync_delete AFTER DELETE ON project_tasks WHEN (SELECT suppressed FROM desktop_project_assignment_sync_guard WHERE singleton=1)=0 BEGIN
            INSERT INTO desktop_project_assignment_sync_outbox(operation_id,project_id,task_id,present,expected) VALUES(lower(hex(randomblob(16))),OLD.project_id,OLD.task_id,0,1);
          END;
        )SQL");
    }
    if(ok)ok=execute("COMMIT");if(!ok){execute("ROLLBACK");error_=L"Projektzuordnungswarteschlange konnte nicht eingerichtet werden.";return false;}return true;
}

bool EnterpriseDatabase::nextDesktopProjectAssignmentOperation(std::wstring& operationId,int64_t& projectId,int64_t& taskId,bool& present,bool& expected,std::wstring& wireJson) {
    operationId.clear();wireJson.clear();projectId=taskId=0;present=expected=false;sqlite3_stmt* st=nullptr;
    bool ok=sqlite3_prepare_v2(db_,R"SQL(SELECT o.operation_id,o.project_id,o.task_id,o.present,o.expected,COALESCE(o.wire_json,'') FROM desktop_project_assignment_sync_outbox o
      WHERE NOT EXISTS(SELECT 1 FROM desktop_project_assignment_sync_outbox older WHERE older.task_id=o.task_id AND older.seq<o.seq)
      ORDER BY CASE WHEN EXISTS(SELECT 1 FROM sync_object_links a WHERE a.object_type='project' AND a.local_id=o.project_id) AND EXISTS(SELECT 1 FROM sync_object_links b WHERE b.object_type='task' AND b.local_id=o.task_id) THEN 0 ELSE 1 END,o.seq LIMIT 1)SQL",-1,&st,nullptr)==SQLITE_OK;
    if(ok){int result=sqlite3_step(st);if(result==SQLITE_ROW){operationId=Wide(sqlite3_column_text(st,0));projectId=sqlite3_column_int64(st,1);taskId=sqlite3_column_int64(st,2);present=sqlite3_column_int(st,3)!=0;expected=sqlite3_column_int(st,4)!=0;wireJson=Wide(sqlite3_column_text(st,5));ok=!operationId.empty()&&projectId>0&&taskId>0&&present!=expected;}else ok=result==SQLITE_DONE;}
    if(st)sqlite3_finalize(st);if(!ok)error_=L"Projektzuordnungswarteschlange konnte nicht gelesen werden.";return ok;
}

bool EnterpriseDatabase::freezeDesktopProjectAssignmentOperation(const std::wstring& operationId,const std::wstring& serverUrl,int64_t serverActor,const std::wstring& wireJson) {
    if(operationId.empty()||serverUrl.empty()||serverActor<=0||wireJson.empty()){error_=L"Ungültige Übertragungsdaten.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    sqlite3_stmt* st=nullptr;
    const char* query=R"SQL(SELECT 1 FROM desktop_project_assignment_sync_outbox o JOIN desktop_project_assignment_sync_config c ON c.singleton=1 JOIN sync_object_links a ON a.object_type='project' AND a.local_id=o.project_id JOIN sync_object_links b ON b.object_type='task' AND b.local_id=o.task_id
      WHERE NOT EXISTS(SELECT 1 FROM desktop_project_assignment_sync_outbox older WHERE older.task_id=o.task_id AND older.seq<o.seq) AND o.operation_id=?1 AND json_valid(?2) AND json_extract(?2,'$.clientId')=c.client_id AND json_extract(?2,'$.operationId')=o.operation_id AND json_extract(?2,'$.projectId')=a.external_id AND json_extract(?2,'$.taskId')=b.external_id AND json_type(?2,'$.present') IN ('true','false') AND json_extract(?2,'$.present')=o.present AND json_type(?2,'$.expected') IN ('true','false') AND json_extract(?2,'$.expected')=o.expected AND (o.wire_json IS NULL OR o.wire_json=?2))SQL";
    bool ok=sqlite3_prepare_v2(db_,query,-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,operationId);BindText(st,2,wireJson);ok=sqlite3_step(st)==SQLITE_ROW;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok){ok=sqlite3_prepare_v2(db_,"INSERT OR IGNORE INTO desktop_project_assignment_sync_binding VALUES(1,?,?)",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,serverUrl);sqlite3_bind_int64(st,2,serverActor);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok){ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM desktop_project_assignment_sync_binding WHERE server_url=? AND server_actor=?",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,serverUrl);sqlite3_bind_int64(st,2,serverActor);ok=sqlite3_step(st)==SQLITE_ROW;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok){ok=sqlite3_prepare_v2(db_,"UPDATE desktop_project_assignment_sync_outbox SET wire_json=? WHERE operation_id=?",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,wireJson);BindText(st,2,operationId);ok=sqlite3_step(st)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(st)sqlite3_finalize(st);}
    if(ok)ok=execute("COMMIT");if(!ok){execute("ROLLBACK");error_=L"Projektzuordnung oder Serverbindung hat sich geändert.";return false;}return true;
}

bool EnterpriseDatabase::acknowledgeDesktopProjectAssignmentOperation(const std::wstring& operationId,const std::wstring& serverUrl,int64_t serverActor,const std::wstring& wireJson,const std::wstring& actor) {
    if(operationId.empty()||serverUrl.empty()||serverActor<=0||wireJson.empty()){error_=L"Ungültige Bestätigung.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    sqlite3_stmt* st=nullptr;int64_t project=0,task=0;int present=0;
    bool ok=sqlite3_prepare_v2(db_,"SELECT o.project_id,o.task_id,o.present FROM desktop_project_assignment_sync_outbox o JOIN desktop_project_assignment_sync_binding b ON b.singleton=1 WHERE NOT EXISTS(SELECT 1 FROM desktop_project_assignment_sync_outbox older WHERE older.task_id=o.task_id AND older.seq<o.seq) AND o.operation_id=? AND o.wire_json=? AND b.server_url=? AND b.server_actor=?",-1,&st,nullptr)==SQLITE_OK;
    if(ok){BindText(st,1,operationId);BindText(st,2,wireJson);BindText(st,3,serverUrl);sqlite3_bind_int64(st,4,serverActor);ok=sqlite3_step(st)==SQLITE_ROW;if(ok){project=sqlite3_column_int64(st,0);task=sqlite3_column_int64(st,1);present=sqlite3_column_int(st,2);}}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok){ok=sqlite3_prepare_v2(db_,"INSERT INTO desktop_project_assignment_sync_shadow VALUES(?,?,?) ON CONFLICT(project_id,task_id) DO UPDATE SET present=excluded.present",-1,&st,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(st,1,project);sqlite3_bind_int64(st,2,task);sqlite3_bind_int(st,3,present);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok){ok=sqlite3_prepare_v2(db_,"DELETE FROM desktop_project_assignment_sync_outbox WHERE operation_id=?",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,operationId);ok=sqlite3_step(st)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(st)sqlite3_finalize(st);}
    if(ok)ok=audit(actor,L"sync.project_assignment.sent",operationId);
    if(ok)ok=execute("COMMIT");if(!ok){execute("ROLLBACK");error_=L"Projektzuordnungsbestätigung konnte nicht gespeichert werden.";return false;}return true;
}

// Apply an incoming edge only if the local copy still matches its last
// confirmed server state. Missing baselines never authorize deleting or
// adopting a pre-existing local edge.
bool EnterpriseDatabase::applySyncedTaskDependency(const std::wstring& taskExternalId, const std::wstring& dependencyExternalId, bool present, const std::wstring& actor) {
    if(taskExternalId.empty()||dependencyExternalId.empty()||taskExternalId==dependencyExternalId){error_=L"Ungültige zentrale Voraussetzung.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    const int64_t task=syncedLocalId(L"task",taskExternalId),dependency=syncedLocalId(L"task",dependencyExternalId);
    if(task<=0||dependency<=0||task==dependency){execute("ROLLBACK");error_=L"Zentrale Voraussetzung: Aufgaben sind nicht eindeutig zugeordnet.";return false;}
    bool ok=execute("CREATE TABLE IF NOT EXISTS desktop_dependency_sync_shadow(task_id INTEGER NOT NULL,dependency_id INTEGER NOT NULL,present INTEGER NOT NULL,PRIMARY KEY(task_id,dependency_id))");
    sqlite3_stmt* st=nullptr;bool configured=false,pending=false;
    if(ok)ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_dependency_sync_outbox'",-1,&st,nullptr)==SQLITE_OK;
    if(ok){int result=sqlite3_step(st);configured=result==SQLITE_ROW;ok=result==SQLITE_ROW||result==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok&&configured){ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM desktop_dependency_sync_outbox WHERE task_id=? AND dependency_id=? LIMIT 1",-1,&st,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(st,1,task);sqlite3_bind_int64(st,2,dependency);int result=sqlite3_step(st);pending=result==SQLITE_ROW;ok=result==SQLITE_ROW||result==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok&&pending){if(execute("COMMIT"))return true;execute("ROLLBACK");return false;}
    bool current=false,hasShadow=false,shadow=false;
    if(ok)ok=sqlite3_prepare_v2(db_,"SELECT EXISTS(SELECT 1 FROM task_dependencies WHERE task_id=?1 AND depends_on_task_id=?2), (SELECT present FROM desktop_dependency_sync_shadow WHERE task_id=?1 AND dependency_id=?2)",-1,&st,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_int64(st,1,task);sqlite3_bind_int64(st,2,dependency);if(sqlite3_step(st)==SQLITE_ROW){current=sqlite3_column_int(st,0)!=0;hasShadow=sqlite3_column_type(st,1)!=SQLITE_NULL;shadow=sqlite3_column_int(st,1)!=0;}else ok=false;}
    if(st)sqlite3_finalize(st);st=nullptr;
    if(ok&&((hasShadow&&current!=shadow)||(!hasShadow&&current))){execute("ROLLBACK");error_=L"Voraussetzungskonflikt: Lokale Änderung hat keinen bestätigten Serverstand.";return false;}
    if(ok&&present&&!current){
        const char* check="WITH RECURSIVE reach(id) AS (SELECT depends_on_task_id FROM task_dependencies WHERE task_id=?1 UNION SELECT d.depends_on_task_id FROM task_dependencies d JOIN reach r ON d.task_id=r.id) SELECT EXISTS(SELECT 1 FROM reach WHERE id=?2), EXISTS(SELECT 1 FROM tasks t JOIN tasks p ON p.id=?1 WHERE t.id=?2 AND t.active=1 AND t.status='done' AND p.active=1 AND p.status<>'done')";
        ok=sqlite3_prepare_v2(db_,check,-1,&st,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(st,1,dependency);sqlite3_bind_int64(st,2,task);if(sqlite3_step(st)!=SQLITE_ROW)ok=false;else if(sqlite3_column_int(st,0)||sqlite3_column_int(st,1)){sqlite3_finalize(st);execute("ROLLBACK");error_=L"Die Servervoraussetzung erzeugt einen Zyklus oder blockiert eine abgeschlossene Aufgabe.";return false;}}
        if(st)sqlite3_finalize(st);st=nullptr;
    }
    if(ok&&configured)ok=execute("UPDATE desktop_dependency_sync_guard SET suppressed=1 WHERE singleton=1");
    if(ok&&current!=present){
        const char* sql=present?"INSERT INTO task_dependencies(task_id,depends_on_task_id) VALUES(?,?)":"DELETE FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?";
        ok=sqlite3_prepare_v2(db_,sql,-1,&st,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(st,1,task);sqlite3_bind_int64(st,2,dependency);ok=sqlite3_step(st)==SQLITE_DONE&&sqlite3_changes(db_)==1;}
        if(st)sqlite3_finalize(st);st=nullptr;
    }
    if(ok){ok=sqlite3_prepare_v2(db_,"INSERT INTO desktop_dependency_sync_shadow VALUES(?,?,?) ON CONFLICT(task_id,dependency_id) DO UPDATE SET present=excluded.present",-1,&st,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(st,1,task);sqlite3_bind_int64(st,2,dependency);sqlite3_bind_int(st,3,present?1:0);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok&&configured)ok=execute("UPDATE desktop_dependency_sync_guard SET suppressed=0 WHERE singleton=1");
    if(ok)ok=audit(actor,L"sync.task_dependency.applied",taskExternalId+L" <- "+dependencyExternalId);
    if(ok)ok=execute("COMMIT");if(!ok){execute("ROLLBACK");setError("Zentrale Voraussetzung konnte nicht übernommen werden");return false;}return true;
}

std::vector<EnterpriseSubtask> EnterpriseDatabase::subtasks(int64_t taskId, bool includeArchived) {
    std::vector<EnterpriseSubtask> result;
    if (taskId <= 0) { error_ = L"Ungültige Aufgaben-ID."; return result; }
    const char* sql = includeArchived ? "SELECT id,task_id,title,status,assignee_id,due_at,active FROM task_subtasks WHERE task_id=? ORDER BY active DESC,due_at,title" : "SELECT id,task_id,title,status,assignee_id,due_at,active FROM task_subtasks WHERE task_id=? AND active=1 ORDER BY CASE status WHEN 'done' THEN 2 WHEN 'cancelled' THEN 3 ELSE 1 END,due_at,title";
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) != SQLITE_OK) { setError("Unteraufgaben konnten nicht geladen werden"); return result; }
    sqlite3_bind_int64(statement, 1, taskId);
    while (sqlite3_step(statement) == SQLITE_ROW) result.push_back({sqlite3_column_int64(statement,0),sqlite3_column_int64(statement,1),Wide(sqlite3_column_text(statement,2)),Wide(sqlite3_column_text(statement,3)),sqlite3_column_int64(statement,4),sqlite3_column_int64(statement,5),sqlite3_column_int(statement,6)!=0});
    sqlite3_finalize(statement); return result;
}

bool EnterpriseDatabase::createSubtask(const EnterpriseSubtask& subtask, int64_t& newId, const std::wstring& actor) {
    if (subtask.taskId <= 0 || subtask.title.empty() || subtask.dueAt < 0 || (subtask.status != L"" && subtask.status != L"open" && subtask.status != L"in_progress" && subtask.status != L"done" && subtask.status != L"cancelled")) { error_ = L"Ungültige Unteraufgabe."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr; const char* sql = "INSERT INTO task_subtasks(task_id,title,status,assignee_id,due_at) SELECT ?,?,?,?,? WHERE EXISTS(SELECT 1 FROM tasks WHERE id=? AND active=1)";
    bool ok = sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement,1,subtask.taskId); BindText(statement,2,subtask.title); BindText(statement,3,subtask.status.empty()?L"open":subtask.status); sqlite3_bind_int64(statement,4,subtask.assigneeId); sqlite3_bind_int64(statement,5,subtask.dueAt); sqlite3_bind_int64(statement,6,subtask.taskId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    const int64_t inserted = ok ? sqlite3_last_insert_rowid(db_) : 0;
    if (ok) ok = audit(actor,L"task.subtask.created",std::to_wstring(subtask.taskId)+L":"+subtask.title);
    if (ok) ok = execute("COMMIT");
    if (!ok) { execute("ROLLBACK"); newId=0; setError("Unteraufgabe konnte nicht angelegt werden"); return false; }
    newId=inserted; return true;
}

bool EnterpriseDatabase::updateSubtask(const EnterpriseSubtask& subtask, const std::wstring& actor, const EnterpriseSubtask* expected) {
    if (subtask.id <= 0 || subtask.title.empty() || subtask.dueAt < 0 || (subtask.status != L"open" && subtask.status != L"in_progress" && subtask.status != L"done" && subtask.status != L"cancelled")) { error_ = L"Ungültige Unteraufgabe."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    const char* sql=expected?"UPDATE task_subtasks SET title=?1,status=?2,assignee_id=?3,due_at=?4,updated_at=CURRENT_TIMESTAMP WHERE id=?5 AND active=1 AND task_id=?6 AND title=?7 AND status=?8 AND assignee_id=?9 AND due_at=?10 AND EXISTS(SELECT 1 FROM tasks WHERE id=?6 AND active=1) AND (?3=0 OR EXISTS(SELECT 1 FROM employees WHERE id=?3 AND active=1))":"UPDATE task_subtasks SET title=?,status=?,assignee_id=?,due_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1";
    sqlite3_stmt* statement = nullptr; bool ok = (!expected || (expected->id==subtask.id && expected->taskId==subtask.taskId && expected->active)) && sqlite3_prepare_v2(db_,sql,-1,&statement,nullptr)==SQLITE_OK;
    if (ok) { BindText(statement,1,subtask.title); BindText(statement,2,subtask.status); sqlite3_bind_int64(statement,3,subtask.assigneeId); sqlite3_bind_int64(statement,4,subtask.dueAt); sqlite3_bind_int64(statement,5,subtask.id);if(expected){sqlite3_bind_int64(statement,6,expected->taskId);BindText(statement,7,expected->title);BindText(statement,8,expected->status);sqlite3_bind_int64(statement,9,expected->assigneeId);sqlite3_bind_int64(statement,10,expected->dueAt);} ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor,L"task.subtask.updated",std::to_wstring(subtask.id));
    if (ok) ok = execute("COMMIT");
    if (!ok) { execute("ROLLBACK"); if(expected)error_=L"Unteraufgabe wurde geändert oder konnte nicht gespeichert werden. Bitte erneut öffnen und prüfen.";else setError("Unteraufgabe konnte nicht bearbeitet werden"); return false; }
    return true;
}

bool EnterpriseDatabase::updateSubtaskStatus(int64_t subtaskId, const std::wstring& status, const std::wstring& actor) {
    if (subtaskId <= 0 || (status != L"open" && status != L"in_progress" && status != L"done" && status != L"cancelled")) { error_ = L"Ungültiger Unteraufgabenstatus."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr; bool ok = sqlite3_prepare_v2(db_, "UPDATE task_subtasks SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement,1,status); sqlite3_bind_int64(statement,2,subtaskId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor,L"task.subtask.status.changed",std::to_wstring(subtaskId)+L":"+status);
    if (ok) ok = execute("COMMIT");
    if (!ok) { execute("ROLLBACK"); setError("Unteraufgabenstatus konnte nicht geändert werden"); return false; }
    return true;
}

bool EnterpriseDatabase::archiveSubtask(int64_t subtaskId, const std::wstring& actor, const EnterpriseSubtask* expected) {
    if (subtaskId <= 0) { error_ = L"Ungültige Unteraufgaben-ID."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr; const char* sql = expected ? "UPDATE task_subtasks SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=?1 AND active=1 AND task_id=?2 AND title=?3 AND status=?4 AND assignee_id=?5 AND due_at=?6 AND EXISTS(SELECT 1 FROM tasks WHERE id=?2 AND active=1)" : "UPDATE task_subtasks SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=?1 AND active=1";
    bool ok = (!expected || (expected->id == subtaskId && expected->active)) && sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement,1,subtaskId); if(expected) { sqlite3_bind_int64(statement,2,expected->taskId); BindText(statement,3,expected->title); BindText(statement,4,expected->status); sqlite3_bind_int64(statement,5,expected->assigneeId); sqlite3_bind_int64(statement,6,expected->dueAt); } ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor,L"task.subtask.archived",std::to_wstring(subtaskId));
    if (ok) ok = execute("COMMIT");
    if (!ok) { execute("ROLLBACK"); if(expected) error_=L"Unteraufgabe wurde geändert oder konnte nicht archiviert werden. Bitte erneut öffnen und prüfen."; else setError("Unteraufgabe konnte nicht archiviert werden"); return false; }
    return true;
}

bool EnterpriseDatabase::configureDesktopSubtaskOutbox(const std::wstring& clientId) {
    if(clientId.empty()){error_=L"Synchronisationskennung fehlt.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    sqlite3_stmt* st=nullptr;bool ok=sqlite3_prepare_v2(db_,"SELECT client_id FROM desktop_subtask_sync_config WHERE singleton=1",-1,&st,nullptr)==SQLITE_OK;bool configured=false;
    if(ok && sqlite3_step(st)==SQLITE_ROW){configured=true;ok=Wide(sqlite3_column_text(st,0))==clientId;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok && configured){if(!execute("COMMIT")){execute("ROLLBACK");return false;}return true;}
    if(ok){ok=sqlite3_prepare_v2(db_,"INSERT INTO desktop_subtask_sync_config VALUES(1,?)",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,clientId);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);}
    // Do not infer a baseline for previously linked records lacking a shadow.
    if(ok){ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM task_subtasks s JOIN sync_object_links l ON l.object_type='subtask' AND l.local_id=s.id LEFT JOIN sync_subtask_shadow b ON b.subtask_id=s.id WHERE b.subtask_id IS NULL LIMIT 1",-1,&st,nullptr)==SQLITE_OK;if(ok)ok=sqlite3_step(st)==SQLITE_DONE;if(st)sqlite3_finalize(st);}
    if(ok)ok=execute(R"SQL(
      INSERT INTO sync_object_links(object_type,external_id,local_id)
      SELECT 'subtask',c.client_id||':subtask:'||s.id,s.id FROM task_subtasks s CROSS JOIN desktop_subtask_sync_config c
      WHERE NOT EXISTS(SELECT 1 FROM sync_object_links l WHERE l.object_type='subtask' AND l.local_id=s.id);
      INSERT INTO desktop_subtask_sync_outbox(operation_id,action,subtask_id,value_json,expected_json)
      SELECT lower(hex(randomblob(16))),'create',s.id,json_object('taskId',s.task_id,'title',s.title,'status',s.status,'assigneeId',s.assignee_id,'dueAt',s.due_at,'active',json('true')),NULL
      FROM task_subtasks s LEFT JOIN sync_subtask_shadow b ON b.subtask_id=s.id WHERE b.subtask_id IS NULL;
      INSERT INTO desktop_subtask_sync_outbox(operation_id,action,subtask_id,value_json,expected_json)
      SELECT lower(hex(randomblob(16))),'archive',s.id,json_object('taskId',s.task_id,'title',s.title,'status',s.status,'assigneeId',s.assignee_id,'dueAt',s.due_at,'active',json('false')),json_object('taskId',s.task_id,'title',s.title,'status',s.status,'assigneeId',s.assignee_id,'dueAt',s.due_at,'active',json('true'))
      FROM task_subtasks s LEFT JOIN sync_subtask_shadow b ON b.subtask_id=s.id WHERE b.subtask_id IS NULL AND s.active=0;
      INSERT INTO desktop_subtask_sync_outbox(operation_id,action,subtask_id,value_json,expected_json)
      SELECT lower(hex(randomblob(16))),CASE WHEN b.active=1 AND s.active=0 THEN 'archive' ELSE 'update' END,s.id,json_object('taskId',s.task_id,'title',s.title,'status',s.status,'assigneeId',s.assignee_id,'dueAt',s.due_at,'active',json(CASE WHEN s.active=1 THEN 'true' ELSE 'false' END)),json_object('taskId',b.task_id,'title',b.title,'status',b.status,'assigneeId',b.assignee_id,'dueAt',b.due_at,'active',json(CASE WHEN b.active=1 THEN 'true' ELSE 'false' END))
      FROM task_subtasks s JOIN sync_subtask_shadow b ON b.subtask_id=s.id WHERE s.task_id IS NOT b.task_id OR s.title IS NOT b.title OR s.status IS NOT b.status OR s.assignee_id IS NOT b.assignee_id OR s.due_at IS NOT b.due_at OR s.active IS NOT b.active;
      CREATE TRIGGER desktop_subtask_sync_insert AFTER INSERT ON task_subtasks WHEN (SELECT suppressed FROM desktop_subtask_sync_guard)=0 BEGIN
        INSERT INTO sync_object_links SELECT 'subtask',client_id||':subtask:'||NEW.id,NEW.id FROM desktop_subtask_sync_config;
        INSERT INTO desktop_subtask_sync_outbox(operation_id,action,subtask_id,value_json) VALUES(lower(hex(randomblob(16))),'create',NEW.id,json_object('taskId',NEW.task_id,'title',NEW.title,'status',NEW.status,'assigneeId',NEW.assignee_id,'dueAt',NEW.due_at,'active',json('true')));
      END;
      CREATE TRIGGER desktop_subtask_sync_update AFTER UPDATE OF task_id,title,status,assignee_id,due_at,active ON task_subtasks WHEN (SELECT suppressed FROM desktop_subtask_sync_guard)=0 AND (OLD.task_id IS NOT NEW.task_id OR OLD.title IS NOT NEW.title OR OLD.status IS NOT NEW.status OR OLD.assignee_id IS NOT NEW.assignee_id OR OLD.due_at IS NOT NEW.due_at OR OLD.active IS NOT NEW.active) BEGIN
        INSERT INTO desktop_subtask_sync_outbox(operation_id,action,subtask_id,value_json,expected_json) VALUES(lower(hex(randomblob(16))),CASE WHEN OLD.active=1 AND NEW.active=0 THEN 'archive' ELSE 'update' END,NEW.id,json_object('taskId',NEW.task_id,'title',NEW.title,'status',NEW.status,'assigneeId',NEW.assignee_id,'dueAt',NEW.due_at,'active',json(CASE WHEN NEW.active=1 THEN 'true' ELSE 'false' END)),json_object('taskId',OLD.task_id,'title',OLD.title,'status',OLD.status,'assigneeId',OLD.assignee_id,'dueAt',OLD.due_at,'active',json(CASE WHEN OLD.active=1 THEN 'true' ELSE 'false' END)));
      END;
    )SQL");
    if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");
    if(!ok)error_=L"Unteraufgabenwarteschlange konnte nicht eingerichtet werden.";return ok;
}

bool EnterpriseDatabase::nextDesktopSubtaskOperation(std::wstring& operationId,std::wstring& action,std::wstring& externalId,std::wstring& valueJson,std::wstring& expectedJson,std::wstring& wireJson) {
    operationId.clear();action.clear();externalId.clear();valueJson.clear();expectedJson.clear();wireJson.clear();
    sqlite3_stmt* st=nullptr;bool ok=sqlite3_prepare_v2(db_,"SELECT o.operation_id,o.action,l.external_id,o.value_json,COALESCE(o.expected_json,''),COALESCE(t.wire_json,'') FROM desktop_subtask_sync_outbox o LEFT JOIN sync_object_links l ON l.object_type='subtask' AND l.local_id=o.subtask_id LEFT JOIN desktop_subtask_sync_transmissions t ON t.operation_id=o.operation_id ORDER BY o.seq LIMIT 1",-1,&st,nullptr)==SQLITE_OK;
    if(ok){int step=sqlite3_step(st);if(step==SQLITE_ROW){operationId=Wide(sqlite3_column_text(st,0));action=Wide(sqlite3_column_text(st,1));externalId=Wide(sqlite3_column_text(st,2));valueJson=Wide(sqlite3_column_text(st,3));expectedJson=Wide(sqlite3_column_text(st,4));wireJson=Wide(sqlite3_column_text(st,5));ok=!operationId.empty()&&!externalId.empty()&&!valueJson.empty();}else ok=step==SQLITE_DONE;}
    if(st)sqlite3_finalize(st);if(!ok)error_=L"Unteraufgabenwarteschlange konnte nicht gelesen werden.";return ok;
}

bool EnterpriseDatabase::freezeDesktopSubtaskOperation(const std::wstring& operationId,const std::wstring& serverUrl,int64_t serverActor,const std::wstring& wireJson) {
    if(operationId.empty()||serverUrl.empty()||serverActor<=0||wireJson.empty()){error_=L"Ungültige Übertragungsdaten.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    sqlite3_stmt* st=nullptr;bool ok=sqlite3_prepare_v2(db_,"SELECT o.operation_id,o.value_json,COALESCE(o.expected_json,''),l.external_id FROM desktop_subtask_sync_outbox o JOIN desktop_subtask_sync_config c ON c.singleton=1 JOIN sync_object_links l ON l.object_type='subtask' AND l.local_id=o.subtask_id WHERE o.seq=(SELECT min(seq) FROM desktop_subtask_sync_outbox) AND json_valid(?) AND json_extract(?,'$.clientId')=c.client_id AND json_extract(?,'$.operationId')=o.operation_id AND json_extract(?,'$.action')=o.action",-1,&st,nullptr)==SQLITE_OK;
    std::wstring value,expected,external;
    if(ok){for(int i=1;i<=4;++i)BindText(st,i,wireJson);ok=sqlite3_step(st)==SQLITE_ROW && Wide(sqlite3_column_text(st,0))==operationId;if(ok){value=Wide(sqlite3_column_text(st,1));expected=Wide(sqlite3_column_text(st,2));external=Wide(sqlite3_column_text(st,3));}}if(st)sqlite3_finalize(st);st=nullptr;
    auto verifyState=[&](const std::wstring& local,const wchar_t* path,const wchar_t* employeePath)->bool {
        const char* sql=R"SQL(SELECT 1 FROM sync_object_links task LEFT JOIN sync_object_links employee ON employee.object_type='employee' AND employee.local_id=json_extract(?1,'$.assigneeId')
          WHERE task.object_type='task' AND task.local_id=json_extract(?1,'$.taskId')
          AND json_type(?2,?4)='object' AND json_extract(?2,?4||'.id')=?3 AND json_extract(?2,?4||'.taskId')=task.external_id
          AND json_type(?2,?4||'.title')='text' AND json_extract(?2,?4||'.title')=json_extract(?1,'$.title')
          AND json_type(?2,?4||'.status')='text' AND json_extract(?2,?4||'.status')=json_extract(?1,'$.status')
          AND json_type(?2,?4||'.dueAt')='integer' AND json_extract(?2,?4||'.dueAt')=json_extract(?1,'$.dueAt')
          AND json_type(?2,?4||'.active') IN ('true','false') AND json_extract(?2,?4||'.active')=json_extract(?1,'$.active')
          AND json_type(?2,?4||'.assigneeId')='integer' AND json_type(?2,?5)='text'
          AND ((json_extract(?1,'$.assigneeId')=0 AND json_extract(?2,?4||'.assigneeId')=0 AND json_extract(?2,?5)='')
               OR (json_extract(?1,'$.assigneeId')>0 AND employee.external_id=json_extract(?2,?5) AND json_extract(?2,?4||'.assigneeId')>0)))SQL";
        sqlite3_stmt* check=nullptr;bool valid=sqlite3_prepare_v2(db_,sql,-1,&check,nullptr)==SQLITE_OK;
        if(valid){BindText(check,1,local);BindText(check,2,wireJson);BindText(check,3,external);BindText(check,4,path);BindText(check,5,employeePath);valid=sqlite3_step(check)==SQLITE_ROW;}if(check)sqlite3_finalize(check);return valid;
    };
    if(ok)ok=verifyState(value,L"$.subtask",L"$.assigneeEmployeeId");
    if(ok && !expected.empty())ok=verifyState(expected,L"$.expected",L"$.expectedAssigneeEmployeeId");
    if(ok && expected.empty()){ok=sqlite3_prepare_v2(db_,"SELECT json_type(?,'$.expected')='null' AND json_type(?,'$.expectedAssigneeEmployeeId') IS NULL",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,wireJson);BindText(st,2,wireJson);ok=sqlite3_step(st)==SQLITE_ROW && sqlite3_column_int(st,0)==1;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok){ok=sqlite3_prepare_v2(db_,"INSERT OR IGNORE INTO desktop_subtask_sync_binding VALUES(1,?,?)",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,serverUrl);sqlite3_bind_int64(st,2,serverActor);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok){ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM desktop_subtask_sync_binding WHERE server_url=? AND server_actor=?",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,serverUrl);sqlite3_bind_int64(st,2,serverActor);ok=sqlite3_step(st)==SQLITE_ROW;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok){ok=sqlite3_prepare_v2(db_,"INSERT OR IGNORE INTO desktop_subtask_sync_transmissions VALUES(?,?)",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,operationId);BindText(st,2,wireJson);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok){ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM desktop_subtask_sync_transmissions WHERE operation_id=? AND wire_json=?",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,operationId);BindText(st,2,wireJson);ok=sqlite3_step(st)==SQLITE_ROW;}if(st)sqlite3_finalize(st);}
    if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");if(!ok)error_=L"Übertragung oder Serverbindung hat sich geändert.";return ok;
}

bool EnterpriseDatabase::acknowledgeDesktopSubtaskOperation(const std::wstring& operationId,const std::wstring& serverUrl,int64_t serverActor,const std::wstring& wireJson,const std::wstring& actor) {
    if(operationId.empty()||serverUrl.empty()||serverActor<=0||wireJson.empty()){error_=L"Ungültige Bestätigung.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    sqlite3_stmt* st=nullptr;bool ok=sqlite3_prepare_v2(db_,"SELECT o.subtask_id,o.value_json FROM desktop_subtask_sync_outbox o JOIN desktop_subtask_sync_transmissions t ON t.operation_id=o.operation_id JOIN desktop_subtask_sync_binding b ON b.singleton=1 WHERE o.seq=(SELECT min(seq) FROM desktop_subtask_sync_outbox) AND o.operation_id=? AND t.wire_json=? AND b.server_url=? AND b.server_actor=?",-1,&st,nullptr)==SQLITE_OK;
    int64_t id=0;std::wstring value;
    if(ok){BindText(st,1,operationId);BindText(st,2,wireJson);BindText(st,3,serverUrl);sqlite3_bind_int64(st,4,serverActor);ok=sqlite3_step(st)==SQLITE_ROW;if(ok){id=sqlite3_column_int64(st,0);value=Wide(sqlite3_column_text(st,1));}}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok){ok=sqlite3_prepare_v2(db_,"INSERT INTO sync_subtask_shadow(subtask_id,task_id,title,status,assignee_id,due_at,active) VALUES(?,json_extract(?,'$.taskId'),json_extract(?,'$.title'),json_extract(?,'$.status'),json_extract(?,'$.assigneeId'),json_extract(?,'$.dueAt'),json_extract(?,'$.active')) ON CONFLICT(subtask_id) DO UPDATE SET task_id=excluded.task_id,title=excluded.title,status=excluded.status,assignee_id=excluded.assignee_id,due_at=excluded.due_at,active=excluded.active",-1,&st,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(st,1,id);for(int i=2;i<=7;++i)BindText(st,i,value);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok){ok=sqlite3_prepare_v2(db_,"DELETE FROM desktop_subtask_sync_outbox WHERE operation_id=?",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,operationId);ok=sqlite3_step(st)==SQLITE_DONE && sqlite3_changes(db_)==1;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok){ok=sqlite3_prepare_v2(db_,"DELETE FROM desktop_subtask_sync_transmissions WHERE operation_id=?",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,operationId);ok=sqlite3_step(st)==SQLITE_DONE && sqlite3_changes(db_)==1;}if(st)sqlite3_finalize(st);}
    if(ok)ok=audit(actor,L"sync.subtask.sent",operationId);
    if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");if(!ok)error_=L"Unteraufgabenbestätigung konnte nicht gespeichert werden.";return ok;
}

bool EnterpriseDatabase::applySyncedSubtask(const std::wstring& externalId, const std::wstring& taskExternalId, const std::wstring& assigneeExternalId, const EnterpriseSubtask& subtask, const std::wstring& actor) {
    if (externalId.empty() || taskExternalId.empty() || subtask.title.empty() || subtask.dueAt < 0 ||
        (subtask.status != L"open" && subtask.status != L"in_progress" && subtask.status != L"done" && subtask.status != L"cancelled")) {
        error_=L"Ungültige zentrale Unteraufgabe."; return false;
    }
    if (!execute("BEGIN IMMEDIATE")) return false;
    const int64_t taskId=syncedLocalId(L"task",taskExternalId);
    const int64_t assignee=assigneeExternalId.empty()?0:syncedLocalId(L"employee",assigneeExternalId);
    bool ok=taskId>0 && (assigneeExternalId.empty() || assignee>0);
    sqlite3_stmt* st=nullptr; int64_t local=0;
    if (ok) {
        ok=sqlite3_prepare_v2(db_,"SELECT status,active FROM tasks WHERE id=?",-1,&st,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(st,1,taskId);ok=sqlite3_step(st)==SQLITE_ROW;if(ok && sqlite3_column_int(st,1) && Wide(sqlite3_column_text(st,0))==L"done" && subtask.active && subtask.status!=L"done" && subtask.status!=L"cancelled")ok=false;}
        if(st)sqlite3_finalize(st);st=nullptr;
    }
    if(ok){ok=sqlite3_prepare_v2(db_,"SELECT s.id,s.task_id FROM sync_object_links l JOIN task_subtasks s ON s.id=l.local_id WHERE l.object_type='subtask' AND l.external_id=?",-1,&st,nullptr)==SQLITE_OK;
        if(ok){BindText(st,1,externalId);if(sqlite3_step(st)==SQLITE_ROW){local=sqlite3_column_int64(st,0);if(sqlite3_column_int64(st,1)!=taskId)ok=false;}} if(st)sqlite3_finalize(st);st=nullptr;}
    // Never overwrite a local edit of a previously imported item.
    if(ok && local){
        ok=sqlite3_prepare_v2(db_,"SELECT (s.task_id=b.task_id AND s.title=b.title AND s.status=b.status AND s.assignee_id=b.assignee_id AND s.due_at=b.due_at AND s.active=b.active),(b.task_id=? AND b.title=? AND b.status=? AND b.assignee_id=? AND b.due_at=? AND b.active=?) FROM task_subtasks s JOIN sync_subtask_shadow b ON b.subtask_id=s.id WHERE s.id=?",-1,&st,nullptr)==SQLITE_OK;
        bool localMatches=false,remoteMatches=false;
        if(ok){sqlite3_bind_int64(st,1,taskId);BindText(st,2,subtask.title);BindText(st,3,subtask.status);sqlite3_bind_int64(st,4,assignee);sqlite3_bind_int64(st,5,subtask.dueAt);sqlite3_bind_int(st,6,subtask.active?1:0);sqlite3_bind_int64(st,7,local);ok=sqlite3_step(st)==SQLITE_ROW;if(ok){localMatches=sqlite3_column_int(st,0)!=0;remoteMatches=sqlite3_column_int(st,1)!=0;}}
        if(st)sqlite3_finalize(st);st=nullptr;
        if(ok && !localMatches && remoteMatches){if(!execute("COMMIT")){execute("ROLLBACK");return false;}return true;}
        if(ok && !localMatches)ok=false;
    }

    if(ok)ok=execute("UPDATE desktop_subtask_sync_guard SET suppressed=1 WHERE singleton=1");
    if(ok && !local){ok=sqlite3_prepare_v2(db_,"INSERT INTO task_subtasks(task_id,title,status,assignee_id,due_at,active) VALUES(?,?,?,?,?,?)",-1,&st,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(st,1,taskId);BindText(st,2,subtask.title);BindText(st,3,subtask.status);sqlite3_bind_int64(st,4,assignee);sqlite3_bind_int64(st,5,subtask.dueAt);sqlite3_bind_int(st,6,subtask.active?1:0);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
        if(ok){local=sqlite3_last_insert_rowid(db_);ok=sqlite3_prepare_v2(db_,"INSERT INTO sync_object_links(object_type,external_id,local_id) VALUES('subtask',?,?)",-1,&st,nullptr)==SQLITE_OK;if(ok){BindText(st,1,externalId);sqlite3_bind_int64(st,2,local);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;}
    }else if(ok){ok=sqlite3_prepare_v2(db_,"UPDATE task_subtasks SET title=?,status=?,assignee_id=?,due_at=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND task_id=?",-1,&st,nullptr)==SQLITE_OK;
        if(ok){BindText(st,1,subtask.title);BindText(st,2,subtask.status);sqlite3_bind_int64(st,3,assignee);sqlite3_bind_int64(st,4,subtask.dueAt);sqlite3_bind_int(st,5,subtask.active?1:0);sqlite3_bind_int64(st,6,local);sqlite3_bind_int64(st,7,taskId);ok=sqlite3_step(st)==SQLITE_DONE && sqlite3_changes(db_)==1;}if(st)sqlite3_finalize(st);}
    if(ok){ok=sqlite3_prepare_v2(db_,"INSERT INTO sync_subtask_shadow(subtask_id,task_id,title,status,assignee_id,due_at,active) VALUES(?,?,?,?,?,?,?) ON CONFLICT(subtask_id) DO UPDATE SET task_id=excluded.task_id,title=excluded.title,status=excluded.status,assignee_id=excluded.assignee_id,due_at=excluded.due_at,active=excluded.active",-1,&st,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(st,1,local);sqlite3_bind_int64(st,2,taskId);BindText(st,3,subtask.title);BindText(st,4,subtask.status);sqlite3_bind_int64(st,5,assignee);sqlite3_bind_int64(st,6,subtask.dueAt);sqlite3_bind_int(st,7,subtask.active?1:0);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);}
    if(ok)ok=execute("UPDATE desktop_subtask_sync_guard SET suppressed=0 WHERE singleton=1");
    if(ok)ok=audit(actor,L"sync.subtask.applied",externalId);
    if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");
    if(!ok)setError("Zentrale Unteraufgabe konnte nicht übernommen werden");return ok;
}

std::vector<EnterpriseAttachment> EnterpriseDatabase::attachmentsFor(const std::wstring& subjectType, int64_t subjectId, bool includeArchived) {
    std::vector<EnterpriseAttachment> result;
    if ((subjectType != L"task" && subjectType != L"ticket") || subjectId <= 0) { error_ = L"Ungültiger Anhangsbezug."; return result; }
    sqlite3_stmt* statement = nullptr; const char* sql = includeArchived ? "SELECT id,subject_type,subject_id,original_name,stored_path,size_bytes,sha256,uploaded_by,created_at,active FROM attachments WHERE subject_type=? AND subject_id=? ORDER BY active DESC,id DESC" : "SELECT id,subject_type,subject_id,original_name,stored_path,size_bytes,sha256,uploaded_by,created_at,active FROM attachments WHERE subject_type=? AND subject_id=? AND active=1 ORDER BY id DESC";
    if (sqlite3_prepare_v2(db_,sql,-1,&statement,nullptr)!=SQLITE_OK){setError("Anhänge konnten nicht geladen werden");return result;} BindText(statement,1,subjectType);sqlite3_bind_int64(statement,2,subjectId);while(sqlite3_step(statement)==SQLITE_ROW)result.push_back({sqlite3_column_int64(statement,0),Wide(sqlite3_column_text(statement,1)),sqlite3_column_int64(statement,2),Wide(sqlite3_column_text(statement,3)),Wide(sqlite3_column_text(statement,4)),sqlite3_column_int64(statement,5),Wide(sqlite3_column_text(statement,6)),sqlite3_column_int64(statement,7),Wide(sqlite3_column_text(statement,8)),sqlite3_column_int(statement,9)!=0});sqlite3_finalize(statement);return result;
}

bool EnterpriseDatabase::createAttachment(const EnterpriseAttachment& attachment, int64_t& newId, const std::wstring& actor) {
    if ((attachment.subjectType!=L"task"&&attachment.subjectType!=L"ticket")||attachment.subjectId<=0||attachment.originalName.empty()||attachment.storedPath.empty()||attachment.sizeBytes<0||attachment.sha256.empty()){error_=L"Ungültiger Anhang.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    const char* subjectTable=attachment.subjectType==L"task"?"tasks":"tickets";
    sqlite3_stmt* exists=nullptr;bool ok=sqlite3_prepare_v2(db_,(std::string("SELECT 1 FROM ")+subjectTable+" WHERE id=? AND active=1").c_str(),-1,&exists,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_int64(exists,1,attachment.subjectId);ok=sqlite3_step(exists)==SQLITE_ROW;}
    if(exists)sqlite3_finalize(exists);
    sqlite3_stmt* statement=nullptr;
    if(ok)ok=sqlite3_prepare_v2(db_,"INSERT INTO attachments(subject_type,subject_id,original_name,stored_path,size_bytes,sha256,uploaded_by) VALUES(?,?,?,?,?,?,?)",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){BindText(statement,1,attachment.subjectType);sqlite3_bind_int64(statement,2,attachment.subjectId);BindText(statement,3,attachment.originalName);BindText(statement,4,attachment.storedPath);sqlite3_bind_int64(statement,5,attachment.sizeBytes);BindText(statement,6,attachment.sha256);sqlite3_bind_int64(statement,7,attachment.uploadedBy);ok=sqlite3_step(statement)==SQLITE_DONE;}
    if(statement)sqlite3_finalize(statement);
    if(ok){newId=sqlite3_last_insert_rowid(db_);ok=audit(actor,L"attachment.created",attachment.subjectType+L":"+std::to_wstring(attachment.subjectId)+L":"+attachment.originalName);}
    if(ok)ok=execute("COMMIT");
    if(!ok){execute("ROLLBACK");newId=0;setError("Anhang konnte nicht angelegt werden");}
    return ok;
}

bool EnterpriseDatabase::archiveAttachment(int64_t attachmentId, const std::wstring& actor, int64_t actingEmployeeId) {
    if(attachmentId<=0){error_=L"Ungültige Anhangs-ID.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"UPDATE attachments SET active=0 WHERE id=? AND active=1",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_int64(statement,1,attachmentId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}
    if(statement)sqlite3_finalize(statement);
    if(ok && actingEmployeeId>0) {
        statement=nullptr;std::string kind;
        ok=sqlite3_prepare_v2(db_,"SELECT subject_type FROM attachments WHERE id=?",-1,&statement,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(statement,1,attachmentId);if(sqlite3_step(statement)==SQLITE_ROW){const auto* value=sqlite3_column_text(statement,0);if(value)kind=reinterpret_cast<const char*>(value);}else ok=false;}
        if(statement)sqlite3_finalize(statement);statement=nullptr;
        if(ok && (kind=="task" || kind=="ticket")) {
            const std::string prefix="linux_"+kind+"_attachment_sync",outbox=prefix+"_outbox",actors=prefix+"_archive_actor";
            ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",-1,&statement,nullptr)==SQLITE_OK;
            bool configured=false;if(ok){sqlite3_bind_text(statement,1,outbox.c_str(),-1,SQLITE_TRANSIENT);const int step=sqlite3_step(statement);configured=step==SQLITE_ROW;ok=step==SQLITE_ROW||step==SQLITE_DONE;}
            if(statement)sqlite3_finalize(statement);statement=nullptr;
            if(ok && configured){
                const std::string guardSql="SELECT suppressed FROM "+prefix+"_guard WHERE singleton=1";
                ok=sqlite3_prepare_v2(db_,guardSql.c_str(),-1,&statement,nullptr)==SQLITE_OK;
                if(ok)ok=sqlite3_step(statement)==SQLITE_ROW&&sqlite3_column_int(statement,0)==0;
                if(statement)sqlite3_finalize(statement);statement=nullptr;
                if(ok)ok=execute(("CREATE TABLE IF NOT EXISTS "+actors+"(seq INTEGER PRIMARY KEY,actor_id INTEGER NOT NULL)").c_str());
                const std::string sql="INSERT INTO "+actors+"(seq,actor_id) SELECT seq,? FROM "+outbox+" WHERE attachment_id=? AND action='archive' ORDER BY seq DESC LIMIT 1";
                if(ok)ok=sqlite3_prepare_v2(db_,sql.c_str(),-1,&statement,nullptr)==SQLITE_OK;
                if(ok){sqlite3_bind_int64(statement,1,actingEmployeeId);sqlite3_bind_int64(statement,2,attachmentId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}
                if(statement)sqlite3_finalize(statement);statement=nullptr;
            }
        } else if(ok)ok=false;
    }
    if(ok)ok=audit(actor,L"attachment.archived",std::to_wstring(attachmentId));
    if(ok)ok=execute("COMMIT");
    if(!ok){execute("ROLLBACK");setError("Anhang konnte nicht archiviert werden");}
    return ok;
}

bool EnterpriseDatabase::restoreAttachment(int64_t attachmentId, const std::wstring& actor) {
    if(attachmentId<=0){error_=L"Ungültige Anhangs-ID.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    sqlite3_stmt* statement=nullptr;
    bool ok=sqlite3_prepare_v2(db_,"UPDATE attachments SET active=1 WHERE id=? AND active=0 AND ((subject_type='task' AND EXISTS(SELECT 1 FROM tasks WHERE id=subject_id AND active=1)) OR (subject_type='ticket' AND EXISTS(SELECT 1 FROM tickets WHERE id=subject_id AND active=1)))",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_int64(statement,1,attachmentId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}
    if(statement)sqlite3_finalize(statement);
    if(ok)ok=audit(actor,L"attachment.restored",std::to_wstring(attachmentId));
    if(ok)ok=execute("COMMIT");
    if(!ok){execute("ROLLBACK");setError("Anhang konnte nicht wiederhergestellt werden");}
    return ok;
}

bool EnterpriseDatabase::applySyncedAttachment(const std::wstring& externalId,const std::wstring& subjectExternalId,const std::wstring& uploaderExternalId,const EnterpriseAttachment& attachment,const std::wstring& actor,bool* wasChanged){
    if(wasChanged)*wasChanged=false;
    if(externalId.empty()||subjectExternalId.empty()||uploaderExternalId.empty()||(attachment.subjectType!=L"task"&&attachment.subjectType!=L"ticket")||attachment.originalName.empty()||attachment.storedPath.empty()||attachment.sizeBytes<0||attachment.sha256.size()!=64){error_=L"Ungültige zentrale Anhangsdaten.";return false;}
    const int64_t subjectId=syncedLocalId(attachment.subjectType,subjectExternalId),uploader=syncedLocalId(L"employee",uploaderExternalId);if(subjectId<=0||uploader<=0){error_=L"Anhangsbezug ist noch nicht synchronisiert.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;sqlite3_stmt* s=nullptr;int64_t local=0;bool ok=sqlite3_prepare_v2(db_,"SELECT local_id FROM sync_object_links WHERE object_type='attachment' AND external_id=?",-1,&s,nullptr)==SQLITE_OK;
    if(ok){BindText(s,1,externalId);if(sqlite3_step(s)==SQLITE_ROW)local=sqlite3_column_int64(s,0);}if(s)sqlite3_finalize(s);s=nullptr;
    bool changed=false;
    if(ok&&local==0){ok=sqlite3_prepare_v2(db_,"INSERT INTO attachments(subject_type,subject_id,original_name,stored_path,size_bytes,sha256,uploaded_by,active) VALUES(?,?,?,?,?,?,?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,attachment.subjectType);sqlite3_bind_int64(s,2,subjectId);BindText(s,3,attachment.originalName);BindText(s,4,attachment.storedPath);sqlite3_bind_int64(s,5,attachment.sizeBytes);BindText(s,6,attachment.sha256);sqlite3_bind_int64(s,7,uploader);sqlite3_bind_int(s,8,attachment.active?1:0);ok=sqlite3_step(s)==SQLITE_DONE;}if(s)sqlite3_finalize(s);s=nullptr;if(ok){local=sqlite3_last_insert_rowid(db_);ok=sqlite3_prepare_v2(db_,"INSERT INTO sync_object_links(object_type,external_id,local_id) VALUES('attachment',?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,externalId);sqlite3_bind_int64(s,2,local);ok=sqlite3_step(s)==SQLITE_DONE;}}changed=ok;}
    else if(ok){
        // File location belongs to this workspace, not the incoming event.
        // Refuse conflicting immutable metadata rather than repointing a
        // linked attachment to a different file or author.
        ok=sqlite3_prepare_v2(db_,"SELECT active FROM attachments WHERE id=? AND subject_type=? AND subject_id=? AND original_name=? AND size_bytes=? AND sha256=? AND uploaded_by=?",-1,&s,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(s,1,local);BindText(s,2,attachment.subjectType);sqlite3_bind_int64(s,3,subjectId);BindText(s,4,attachment.originalName);sqlite3_bind_int64(s,5,attachment.sizeBytes);BindText(s,6,attachment.sha256);sqlite3_bind_int64(s,7,uploader);ok=sqlite3_step(s)==SQLITE_ROW;if(ok)changed=(sqlite3_column_int(s,0)!=0)!=attachment.active;}if(s)sqlite3_finalize(s);s=nullptr;
        if(ok&&changed){ok=sqlite3_prepare_v2(db_,"UPDATE attachments SET active=? WHERE id=?",-1,&s,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int(s,1,attachment.active?1:0);sqlite3_bind_int64(s,2,local);ok=sqlite3_step(s)==SQLITE_DONE&&sqlite3_changes(db_)==1;}}
    }
    if(s)sqlite3_finalize(s);if(ok&&changed)ok=audit(actor,L"sync.attachment.applied",externalId);if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");if(ok&&wasChanged)*wasChanged=changed;if(!ok)setError("Zentraler Anhang konnte nicht übernommen werden");return ok;
}

bool EnterpriseDatabase::createOrResetAccount(int64_t employeeId,const std::wstring& username,const std::wstring& password,bool mustChangePassword,const std::wstring& actor){
    if(employeeId<=0||username.size()<3||username.find_first_of(L" \t\r\n")!=std::wstring::npos||password.size()<12){error_=L"Benutzername oder Passwort entspricht nicht der Sicherheitsrichtlinie.";return false;}std::vector<unsigned char> salt(16);if(!SecureRandom(salt)){error_=L"Zufallsquelle für Konto konnte nicht verwendet werden.";return false;}const std::wstring saltHex=Hex(salt);std::wstring digest;if(!PasswordDigest(password,saltHex,digest)){error_=L"Passwort konnte nicht abgeleitet werden.";return false;}sqlite3_stmt* statement=nullptr;const char* sql="INSERT INTO accounts(employee_id,username,password_salt,password_digest,enabled,failed_attempts,locked_until,must_change_password) SELECT ?,?,?,?,1,0,0,? WHERE EXISTS(SELECT 1 FROM employees WHERE id=? AND active=1) ON CONFLICT(employee_id) DO UPDATE SET username=excluded.username,password_salt=excluded.password_salt,password_digest=excluded.password_digest,enabled=1,failed_attempts=0,locked_until=0,must_change_password=excluded.must_change_password,updated_at=CURRENT_TIMESTAMP";bool ok=sqlite3_prepare_v2(db_,sql,-1,&statement,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(statement,1,employeeId);BindText(statement,2,username);BindText(statement,3,saltHex);BindText(statement,4,digest);sqlite3_bind_int(statement,5,mustChangePassword?1:0);sqlite3_bind_int64(statement,6,employeeId);ok=sqlite3_step(statement)==SQLITE_DONE;}if(statement)sqlite3_finalize(statement);if(ok)ok=audit(actor,L"account.credentials.reset",username);if(!ok)setError("Konto konnte nicht gespeichert werden");return ok;
}
bool EnterpriseDatabase::authenticate(const std::wstring& username,const std::wstring& password,int64_t nowUtc,EnterpriseAccount& account){account={};if(username.empty()||password.empty()||nowUtc<=0){error_=L"Ungültige Anmeldeanforderung.";return false;}sqlite3_stmt* statement=nullptr;bool found=sqlite3_prepare_v2(db_,"SELECT employee_id,username,password_salt,password_digest,enabled,failed_attempts,locked_until,must_change_password FROM accounts WHERE username=?",-1,&statement,nullptr)==SQLITE_OK;if(found){BindText(statement,1,username);found=sqlite3_step(statement)==SQLITE_ROW;}if(!found){if(statement)sqlite3_finalize(statement);error_=L"Benutzername oder Passwort ist ungültig.";return false;}const int64_t id=sqlite3_column_int64(statement,0);const std::wstring storedUsername=Wide(sqlite3_column_text(statement,1)),salt=Wide(sqlite3_column_text(statement,2)),expected=Wide(sqlite3_column_text(statement,3));const bool enabled=sqlite3_column_int(statement,4)!=0;const int failures=sqlite3_column_int(statement,5),locked=sqlite3_column_int64(statement,6);const bool mustChange=sqlite3_column_int(statement,7)!=0;sqlite3_finalize(statement);std::wstring actual;const bool valid=enabled&&locked<=nowUtc&&PasswordDigest(password,salt,actual)&&ConstantTimeEquals(expected,actual);sqlite3_stmt* update=nullptr;if(valid){found=sqlite3_prepare_v2(db_,"UPDATE accounts SET failed_attempts=0,locked_until=0 WHERE employee_id=?",-1,&update,nullptr)==SQLITE_OK;if(found){sqlite3_bind_int64(update,1,id);found=sqlite3_step(update)==SQLITE_DONE;}if(update)sqlite3_finalize(update);if(!found){setError("Anmeldestatus konnte nicht aktualisiert werden");return false;}account={id,storedUsername,true,0,0,mustChange};audit(storedUsername,L"account.authenticated",storedUsername);return true;}const int next=failures+1;const int64_t newLocked=next>=5?nowUtc+900:locked;found=sqlite3_prepare_v2(db_,"UPDATE accounts SET failed_attempts=?,locked_until=? WHERE employee_id=?",-1,&update,nullptr)==SQLITE_OK;if(found){sqlite3_bind_int(update,1,next);sqlite3_bind_int64(update,2,newLocked);sqlite3_bind_int64(update,3,id);sqlite3_step(update);}if(update)sqlite3_finalize(update);error_=!enabled?L"Das Konto ist deaktiviert.":locked>nowUtc?L"Das Konto ist vorübergehend gesperrt.":next>=5?L"Zu viele Fehlversuche. Konto 15 Minuten gesperrt.":L"Benutzername oder Passwort ist ungültig.";return false;}
bool EnterpriseDatabase::changePassword(int64_t employeeId,const std::wstring& oldPassword,const std::wstring& newPassword,const std::wstring& actor){if(employeeId<=0||newPassword.size()<12){error_=L"Das neue Passwort muss mindestens 12 Zeichen enthalten.";return false;}sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"SELECT username,password_salt,password_digest FROM accounts WHERE employee_id=? AND enabled=1",-1,&statement,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(statement,1,employeeId);ok=sqlite3_step(statement)==SQLITE_ROW;}if(!ok){if(statement)sqlite3_finalize(statement);error_=L"Aktives Konto nicht gefunden.";return false;}const std::wstring username=Wide(sqlite3_column_text(statement,0)),oldDigest=Wide(sqlite3_column_text(statement,2)),oldSalt=Wide(sqlite3_column_text(statement,1));sqlite3_finalize(statement);std::wstring candidate;if(!PasswordDigest(oldPassword,oldSalt,candidate)||!ConstantTimeEquals(oldDigest,candidate)){error_=L"Aktuelles Passwort ist ungültig.";return false;}return createOrResetAccount(employeeId,username,newPassword,false,actor);}
std::vector<EnterpriseAccount> EnterpriseDatabase::accounts(bool includeDisabled){std::vector<EnterpriseAccount> result;sqlite3_stmt* statement=nullptr;const char* sql=includeDisabled?"SELECT employee_id,username,enabled,failed_attempts,locked_until,must_change_password FROM accounts ORDER BY username":"SELECT employee_id,username,enabled,failed_attempts,locked_until,must_change_password FROM accounts WHERE enabled=1 ORDER BY username";if(sqlite3_prepare_v2(db_,sql,-1,&statement,nullptr)!=SQLITE_OK){setError("Konten konnten nicht geladen werden");return result;}while(sqlite3_step(statement)==SQLITE_ROW)result.push_back({sqlite3_column_int64(statement,0),Wide(sqlite3_column_text(statement,1)),sqlite3_column_int(statement,2)!=0,sqlite3_column_int(statement,3),sqlite3_column_int64(statement,4),sqlite3_column_int(statement,5)!=0});sqlite3_finalize(statement);return result;}
bool EnterpriseDatabase::setAccountEnabled(int64_t employeeId,bool enabled,const std::wstring& actor){if(employeeId<=0){error_=L"Ungültige Konto-ID.";return false;}sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"UPDATE accounts SET enabled=?,failed_attempts=0,locked_until=0,updated_at=CURRENT_TIMESTAMP WHERE employee_id=?",-1,&statement,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int(statement,1,enabled?1:0);sqlite3_bind_int64(statement,2,employeeId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);if(ok)ok=audit(actor,enabled?L"account.enabled":L"account.disabled",std::to_wstring(employeeId));if(!ok)setError("Kontostatus konnte nicht geändert werden");return ok;}

bool EnterpriseDatabase::createTaskApproval(int64_t taskId, int64_t requestedBy, int64_t approverId, int64_t& approvalId, const std::wstring& actor) {
    if (taskId <= 0 || requestedBy <= 0 || approverId <= 0 || requestedBy == approverId) { error_ = L"Antragsteller und freigebende Person müssen unterschiedlich und gültig sein."; return false; }
    if(!execute("BEGIN IMMEDIATE")){setError("Freigabeänderung konnte nicht gestartet werden");return false;}
    sqlite3_stmt* statement = nullptr; const char* sql = "INSERT INTO task_approvals(task_id,requested_by,approver_id) SELECT ?,?,? WHERE EXISTS(SELECT 1 FROM tasks WHERE id=? AND active=1) AND EXISTS(SELECT 1 FROM employees WHERE id=? AND active=1) AND EXISTS(SELECT 1 FROM employees WHERE id=? AND active=1)";
    bool ok = sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, taskId); sqlite3_bind_int64(statement, 2, requestedBy); sqlite3_bind_int64(statement, 3, approverId); sqlite3_bind_int64(statement, 4, taskId); sqlite3_bind_int64(statement, 5, requestedBy); sqlite3_bind_int64(statement, 6, approverId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement); if (ok) { approvalId = sqlite3_last_insert_rowid(db_); ok = audit(actor,L"task.approval.requested",std::to_wstring(taskId)); } if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK"); if (!ok) setError("Genehmigung konnte nicht angelegt werden"); return ok;
}

bool EnterpriseDatabase::decideTaskApproval(int64_t approvalId, bool approved, int64_t decidedBy, const std::wstring& actor) {
    if(!execute("BEGIN IMMEDIATE")){setError("Freigabeänderung konnte nicht gestartet werden");return false;}
    sqlite3_stmt* statement = nullptr; bool ok = sqlite3_prepare_v2(db_, "UPDATE task_approvals SET status=?,decided_by=?,decided_at=CURRENT_TIMESTAMP WHERE id=? AND approver_id=? AND status='pending'", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, approved ? L"approved" : L"rejected"); sqlite3_bind_int64(statement, 2, decidedBy); sqlite3_bind_int64(statement, 3, approvalId); sqlite3_bind_int64(statement, 4, decidedBy); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement); if (ok) ok = audit(actor,approved ? L"task.approval.approved" : L"task.approval.rejected",std::to_wstring(approvalId)); if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK"); if (!ok) setError("Genehmigung konnte nicht entschieden werden"); return ok;
}

bool EnterpriseDatabase::applySyncedTaskApproval(const std::wstring& externalId,const std::wstring& taskExternalId,const std::wstring& requesterExternalId,const std::wstring& approverExternalId,const std::wstring& decidedByExternalId,const EnterpriseTaskApproval& approval,const std::wstring& actor){
    const int64_t taskId=syncedLocalId(L"task",taskExternalId),requester=syncedLocalId(L"employee",requesterExternalId),approver=syncedLocalId(L"employee",approverExternalId),decider=decidedByExternalId.empty()?0:syncedLocalId(L"employee",decidedByExternalId);
    if(externalId.empty()||taskId<=0||requester<=0||approver<=0||requester==approver||!approval.active||(approval.status!=L"pending"&&approval.status!=L"approved"&&approval.status!=L"rejected")||(approval.status==L"pending"?!decidedByExternalId.empty():decider!=approver)){error_=L"Ungültige zentrale Freigabe.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    sqlite3_stmt* s=nullptr;int64_t local=0;bool ok=sqlite3_prepare_v2(db_,"SELECT local_id FROM sync_object_links WHERE object_type='task_approval' AND external_id=?",-1,&s,nullptr)==SQLITE_OK;
    if(ok){BindText(s,1,externalId);const int step=sqlite3_step(s);if(step==SQLITE_ROW)local=sqlite3_column_int64(s,0);else if(step!=SQLITE_DONE)ok=false;}if(s)sqlite3_finalize(s);s=nullptr;
    bool changed=false,conflict=false;
    if(ok&&local==0){
        ok=sqlite3_prepare_v2(db_,"INSERT INTO task_approvals(task_id,requested_by,approver_id,status,decided_by,decided_at) VALUES(?,?,?,?,?,CASE WHEN ?='pending' THEN NULL ELSE CURRENT_TIMESTAMP END)",-1,&s,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(s,1,taskId);sqlite3_bind_int64(s,2,requester);sqlite3_bind_int64(s,3,approver);BindText(s,4,approval.status);if(decider)sqlite3_bind_int64(s,5,decider);else sqlite3_bind_null(s,5);BindText(s,6,approval.status);ok=sqlite3_step(s)==SQLITE_DONE;}if(s)sqlite3_finalize(s);s=nullptr;
        if(ok){local=sqlite3_last_insert_rowid(db_);ok=sqlite3_prepare_v2(db_,"INSERT INTO sync_object_links(object_type,external_id,local_id) VALUES('task_approval',?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,externalId);sqlite3_bind_int64(s,2,local);ok=sqlite3_step(s)==SQLITE_DONE;}if(s)sqlite3_finalize(s);s=nullptr;}changed=ok;
    }else if(ok){
        ok=sqlite3_prepare_v2(db_,"SELECT task_id,requested_by,approver_id,status,COALESCE(decided_by,0) FROM task_approvals WHERE id=?",-1,&s,nullptr)==SQLITE_OK;
        std::wstring current;int64_t currentDecider=0;
        if(ok){sqlite3_bind_int64(s,1,local);if(sqlite3_step(s)!=SQLITE_ROW)ok=false;else{conflict=sqlite3_column_int64(s,0)!=taskId||sqlite3_column_int64(s,1)!=requester||sqlite3_column_int64(s,2)!=approver;current=Wide(sqlite3_column_text(s,3));currentDecider=sqlite3_column_int64(s,4);}}if(s)sqlite3_finalize(s);s=nullptr;
        if(ok&&!conflict&&current!=L"pending"&&approval.status!=L"pending"&&(current!=approval.status||currentDecider!=decider))conflict=true;
        if(ok&&!conflict&&current==L"pending"&&approval.status!=L"pending"){
            ok=sqlite3_prepare_v2(db_,"UPDATE task_approvals SET status=?,decided_by=?,decided_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",-1,&s,nullptr)==SQLITE_OK;
            if(ok){BindText(s,1,approval.status);sqlite3_bind_int64(s,2,decider);sqlite3_bind_int64(s,3,local);ok=sqlite3_step(s)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(s)sqlite3_finalize(s);s=nullptr;changed=ok;
        }
    }
    if(conflict){execute("ROLLBACK");error_=L"Freigabe enthält eine widersprüchliche Entscheidung oder Zuordnung.";return false;}
    if(ok&&changed)ok=audit(actor,L"sync.task_approval.applied",externalId);
    if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");
    if(!ok)setError("Zentrale Freigabe konnte nicht übernommen werden");return ok;
}

std::vector<EnterpriseTaskApproval> EnterpriseDatabase::taskApprovals(int64_t approverId, bool includeDecided) {
    std::vector<EnterpriseTaskApproval> result; sqlite3_stmt* statement = nullptr;
    std::string sql = "SELECT id,task_id,requested_by,approver_id,status,decided_by FROM task_approvals WHERE ";
    if (!includeDecided) sql += "status='pending' AND ";
    if (approverId > 0) sql += "approver_id=? "; else sql += "1=1 ";
    sql += "ORDER BY id DESC";
    if (sqlite3_prepare_v2(db_, sql.c_str(), -1, &statement, nullptr) != SQLITE_OK) { setError("Freigaben konnten nicht geladen werden"); return result; }
    if (approverId > 0) sqlite3_bind_int64(statement, 1, approverId);
    while (sqlite3_step(statement) == SQLITE_ROW) result.push_back({sqlite3_column_int64(statement,0),sqlite3_column_int64(statement,1),sqlite3_column_int64(statement,2),sqlite3_column_int64(statement,3),Wide(sqlite3_column_text(statement,4)),sqlite3_column_int64(statement,5),true});
    sqlite3_finalize(statement); return result;
}

std::vector<EnterpriseTicket> EnterpriseDatabase::tickets(bool includeArchived) {
    std::vector<EnterpriseTicket> result;
    const char* sql = includeArchived ? "SELECT id,title,category,priority,status,assignee_id,active FROM tickets ORDER BY active DESC,updated_at DESC" : "SELECT id,title,category,priority,status,assignee_id,active FROM tickets WHERE active=1 ORDER BY CASE priority WHEN 'Kritisch' THEN 0 WHEN 'Hoch' THEN 1 ELSE 2 END,updated_at DESC";
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) != SQLITE_OK) { setError("Tickets konnten nicht geladen werden"); return result; }
    while (sqlite3_step(statement) == SQLITE_ROW) result.push_back({sqlite3_column_int64(statement, 0), Wide(sqlite3_column_text(statement, 1)), Wide(sqlite3_column_text(statement, 2)), Wide(sqlite3_column_text(statement, 3)), Wide(sqlite3_column_text(statement, 4)), sqlite3_column_int64(statement, 5), sqlite3_column_int(statement, 6) != 0});
    sqlite3_finalize(statement); return result;
}

bool EnterpriseDatabase::createTicket(const EnterpriseTicket& ticket, int64_t& newId, const std::wstring& actor) {
    if (ticket.title.empty() || ticket.category.empty() || ticket.priority.empty()) { error_ = L"Titel, Kategorie und Priorität sind Pflichtangaben."; return false; }
    if (!execute("BEGIN IMMEDIATE")) { setError("Ticketänderung konnte nicht gestartet werden"); return false; }
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "INSERT INTO tickets(title,category,priority,status,assignee_id) VALUES(?,?,?,?,?)", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, ticket.title); BindText(statement, 2, ticket.category); BindText(statement, 3, ticket.priority); BindText(statement, 4, ticket.status.empty() ? L"open" : ticket.status); sqlite3_bind_int64(statement, 5, ticket.assigneeId); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    if (ok) { newId = sqlite3_last_insert_rowid(db_); ok = audit(actor, L"ticket.created", ticket.title); }
    if (ok) { if (!execute("COMMIT")) { execute("ROLLBACK"); ok = false; } } else execute("ROLLBACK");
    if (!ok) setError("Ticket konnte nicht angelegt werden"); return ok;
}

bool EnterpriseDatabase::updateTicket(const EnterpriseTicket& ticket, const std::wstring& actor) {
    if (ticket.id <= 0 || ticket.title.empty() || ticket.category.empty() || (ticket.priority != L"Niedrig" && ticket.priority != L"Mittel" && ticket.priority != L"Hoch" && ticket.priority != L"Kritisch")) { error_ = L"Ungültige Ticketdaten."; return false; }
    if (!execute("BEGIN IMMEDIATE")) { setError("Ticketänderung konnte nicht gestartet werden"); return false; }
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE tickets SET title=?,category=?,priority=?,assignee_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, ticket.title); BindText(statement, 2, ticket.category); BindText(statement, 3, ticket.priority); sqlite3_bind_int64(statement, 4, ticket.assigneeId); sqlite3_bind_int64(statement, 5, ticket.id); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"ticket.updated", std::to_wstring(ticket.id));
    if (ok) { if (!execute("COMMIT")) { execute("ROLLBACK"); ok = false; } } else execute("ROLLBACK");
    if (!ok) setError("Ticket konnte nicht bearbeitet werden"); return ok;
}

bool EnterpriseDatabase::updateTicketStatus(int64_t ticketId, const std::wstring& status, const std::wstring& actor) {
    if (status != L"open" && status != L"in_progress" && status != L"resolved" && status != L"closed") { error_ = L"Ungültiger Ticketstatus."; return false; }
    if (!execute("BEGIN IMMEDIATE")) { setError("Ticketänderung konnte nicht gestartet werden"); return false; }
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE tickets SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, status); sqlite3_bind_int64(statement, 2, ticketId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement); if (ok) ok = audit(actor, L"ticket.status.changed", std::to_wstring(ticketId) + L":" + status); if (ok) { if (!execute("COMMIT")) { execute("ROLLBACK"); ok = false; } } else execute("ROLLBACK");
    if (!ok) setError("Ticketstatus konnte nicht geändert werden"); return ok;
}

bool EnterpriseDatabase::archiveTicket(int64_t ticketId, const std::wstring& actor) {
    if (!execute("BEGIN IMMEDIATE")) { setError("Ticketänderung konnte nicht gestartet werden"); return false; }
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE tickets SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, ticketId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement); if (ok) ok = audit(actor, L"ticket.archived", std::to_wstring(ticketId)); if (ok) { if (!execute("COMMIT")) { execute("ROLLBACK"); ok = false; } } else execute("ROLLBACK");
    if (!ok) setError("Ticket konnte nicht archiviert werden"); return ok;
}

bool EnterpriseDatabase::applySyncedTicket(const std::wstring& externalId, const EnterpriseTicket& ticket, const std::wstring& actor) {
    if (externalId.empty() || ticket.title.empty() || ticket.category.empty() || ticket.assigneeId <= 0 ||
        (ticket.priority != L"Niedrig" && ticket.priority != L"Mittel" && ticket.priority != L"Hoch" && ticket.priority != L"Kritisch") ||
        (ticket.status != L"open" && ticket.status != L"in_progress" && ticket.status != L"resolved" && ticket.status != L"closed")) {
        error_ = L"Ungültige zentrale Ticketdaten.";
        return false;
    }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr;
    int64_t localId = 0;
    bool ok = sqlite3_prepare_v2(db_, "SELECT local_id FROM sync_object_links WHERE object_type='ticket' AND external_id=?", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, externalId); if (sqlite3_step(statement) == SQLITE_ROW) localId = sqlite3_column_int64(statement, 0); }
    if (statement) sqlite3_finalize(statement);
    statement = nullptr;
    if (ok && localId == 0) {
        ok = sqlite3_prepare_v2(db_, "INSERT INTO tickets(title,category,priority,status,assignee_id,active) VALUES(?,?,?,?,?,?)", -1, &statement, nullptr) == SQLITE_OK;
        if (ok) { BindText(statement,1,ticket.title); BindText(statement,2,ticket.category); BindText(statement,3,ticket.priority); BindText(statement,4,ticket.status); sqlite3_bind_int64(statement,5,ticket.assigneeId); sqlite3_bind_int(statement,6,ticket.active?1:0); ok=sqlite3_step(statement)==SQLITE_DONE; }
        if (statement) sqlite3_finalize(statement);
        statement = nullptr;
        if (ok) {
            localId = sqlite3_last_insert_rowid(db_);
            ok = sqlite3_prepare_v2(db_, "INSERT INTO sync_object_links(object_type,external_id,local_id) VALUES('ticket',?,?)", -1, &statement, nullptr) == SQLITE_OK;
            if (ok) { BindText(statement,1,externalId); sqlite3_bind_int64(statement,2,localId); ok=sqlite3_step(statement)==SQLITE_DONE; }
        }
    } else if (ok) {
        ok = sqlite3_prepare_v2(db_, "UPDATE tickets SET title=?,category=?,priority=?,status=?,assignee_id=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", -1, &statement, nullptr) == SQLITE_OK;
        if (ok) { BindText(statement,1,ticket.title); BindText(statement,2,ticket.category); BindText(statement,3,ticket.priority); BindText(statement,4,ticket.status); sqlite3_bind_int64(statement,5,ticket.assigneeId); sqlite3_bind_int(statement,6,ticket.active?1:0); sqlite3_bind_int64(statement,7,localId); ok=sqlite3_step(statement)==SQLITE_DONE && sqlite3_changes(db_)==1; }
    }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"sync.ticket.applied", externalId);
    execute(ok ? "COMMIT" : "ROLLBACK");
    if (!ok) setError("Zentrales Ticket konnte nicht übernommen werden");
    return ok;
}

std::vector<EnterpriseComment> EnterpriseDatabase::commentsFor(const std::wstring& subjectType, int64_t subjectId) {
    std::vector<EnterpriseComment> result;
    if ((subjectType != L"ticket" && subjectType != L"task") || subjectId <= 0) { error_ = L"Ungültiger Kommentarbezug."; return result; }
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db_, "SELECT id,subject_type,subject_id,author_id,body,created_at FROM comments WHERE subject_type=? AND subject_id=? ORDER BY id", -1, &statement, nullptr) != SQLITE_OK) { setError("Kommentare konnten nicht geladen werden"); return result; }
    BindText(statement, 1, subjectType); sqlite3_bind_int64(statement, 2, subjectId);
    while (sqlite3_step(statement) == SQLITE_ROW) result.push_back({sqlite3_column_int64(statement, 0), Wide(sqlite3_column_text(statement, 1)), sqlite3_column_int64(statement, 2), sqlite3_column_int64(statement, 3), Wide(sqlite3_column_text(statement, 4)), Wide(sqlite3_column_text(statement, 5))});
    sqlite3_finalize(statement);
    return result;
}

bool EnterpriseDatabase::createComment(const EnterpriseComment& comment, int64_t& newId, const std::wstring& actor) {
    if ((comment.subjectType != L"ticket" && comment.subjectType != L"task") || comment.subjectId <= 0 || comment.authorId <= 0 || comment.body.empty()) { error_ = L"Ungültiger Kommentar."; return false; }
    if (!execute("BEGIN IMMEDIATE")) { setError("Kommentar konnte nicht gestartet werden"); return false; }
    const char* subjectSql = comment.subjectType == L"ticket" ? "SELECT 1 FROM tickets WHERE id=? AND active=1" : "SELECT 1 FROM tasks WHERE id=? AND active=1";
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, subjectSql, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, comment.subjectId); ok = sqlite3_step(statement) == SQLITE_ROW; }
    if (statement) sqlite3_finalize(statement);
    statement = nullptr;
    if (ok) ok = sqlite3_prepare_v2(db_, "INSERT INTO comments(subject_type,subject_id,author_id,body) VALUES(?,?,?,?)", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, comment.subjectType); sqlite3_bind_int64(statement, 2, comment.subjectId); sqlite3_bind_int64(statement, 3, comment.authorId); BindText(statement, 4, comment.body); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    if (ok) { newId = sqlite3_last_insert_rowid(db_); ok = audit(actor, comment.subjectType + L".comment.created", std::to_wstring(comment.subjectId) + L":" + std::to_wstring(newId)); }
    if (ok) { if (!execute("COMMIT")) { execute("ROLLBACK"); ok = false; } } else execute("ROLLBACK");
    if (!ok) setError("Kommentar konnte nicht gespeichert werden");
    return ok;
}

bool EnterpriseDatabase::applySyncedComment(const std::wstring& externalId,const std::wstring& subjectExternalId,const std::wstring& authorExternalId,const EnterpriseComment& comment,const std::wstring& actor){
    if(externalId.empty()||authorExternalId.empty()||comment.body.empty()||(comment.subjectType!=L"task"&&comment.subjectType!=L"ticket")){error_=L"Ungültiger zentraler Kommentar.";return false;}
    const int64_t subjectId=syncedLocalId(comment.subjectType,subjectExternalId),authorId=syncedLocalId(L"employee",authorExternalId);if(subjectId<=0||authorId<=0){error_=L"Kommentarbezug ist noch nicht synchronisiert.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;sqlite3_stmt* s=nullptr;int64_t local=0;bool ok=sqlite3_prepare_v2(db_,"SELECT local_id FROM sync_object_links WHERE object_type='comment' AND external_id=?",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,externalId);if(sqlite3_step(s)==SQLITE_ROW)local=sqlite3_column_int64(s,0);}if(s)sqlite3_finalize(s);s=nullptr;
    if(ok&&local==0){ok=sqlite3_prepare_v2(db_,"INSERT INTO comments(subject_type,subject_id,author_id,body,created_at) SELECT ?,?,?,?,CASE WHEN ?='' THEN CURRENT_TIMESTAMP ELSE ? END WHERE ?='' OR strftime('%s',?) IS NOT NULL",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,comment.subjectType);sqlite3_bind_int64(s,2,subjectId);sqlite3_bind_int64(s,3,authorId);BindText(s,4,comment.body);BindText(s,5,comment.createdAt);BindText(s,6,comment.createdAt);BindText(s,7,comment.createdAt);BindText(s,8,comment.createdAt);ok=sqlite3_step(s)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(s)sqlite3_finalize(s);s=nullptr;if(ok){local=sqlite3_last_insert_rowid(db_);ok=sqlite3_prepare_v2(db_,"INSERT INTO sync_object_links(object_type,external_id,local_id) VALUES('comment',?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,externalId);sqlite3_bind_int64(s,2,local);ok=sqlite3_step(s)==SQLITE_DONE;}}}
    else if(ok){
        // Comments are immutable records. A repeated event is harmless only
        // when subject, author and body still match the local record.
        ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM comments WHERE id=? AND subject_type=? AND subject_id=? AND author_id=? AND body=?",-1,&s,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_int64(s,1,local);BindText(s,2,comment.subjectType);sqlite3_bind_int64(s,3,subjectId);sqlite3_bind_int64(s,4,authorId);BindText(s,5,comment.body);ok=sqlite3_step(s)==SQLITE_ROW;}
    }
    if(s)sqlite3_finalize(s);if(ok&&local==0)ok=false;
    if(ok)ok=audit(actor,L"sync.comment.applied",externalId);if(ok)ok=execute("COMMIT");if(!ok){execute("ROLLBACK");setError("Zentraler Kommentar konnte nicht übernommen werden");}return ok;
}

std::vector<EnterpriseFacilityAsset> EnterpriseDatabase::facilityAssets(bool includeArchived) {
    std::vector<EnterpriseFacilityAsset> result; sqlite3_stmt* statement = nullptr;
    const char* sql = includeArchived ? "SELECT id,name,category,location,status,active FROM facility_assets ORDER BY active DESC,name" : "SELECT id,name,category,location,status,active FROM facility_assets WHERE active=1 ORDER BY name";
    if(sqlite3_prepare_v2(db_,sql,-1,&statement,nullptr)!=SQLITE_OK){setError("Facility-Assets konnten nicht geladen werden");return result;}while(sqlite3_step(statement)==SQLITE_ROW)result.push_back({sqlite3_column_int64(statement,0),Wide(sqlite3_column_text(statement,1)),Wide(sqlite3_column_text(statement,2)),Wide(sqlite3_column_text(statement,3)),Wide(sqlite3_column_text(statement,4)),sqlite3_column_int(statement,5)!=0});sqlite3_finalize(statement);return result;
}
bool EnterpriseDatabase::createFacilityAsset(const EnterpriseFacilityAsset& asset,int64_t& newId,const std::wstring& actor){if(asset.name.empty()||asset.category.empty()||asset.location.empty()||(asset.status!=L"operational"&&asset.status!=L"maintenance"&&asset.status!=L"out_of_service")){error_=L"Ungültige Asset-Daten.";return false;}if(!execute("BEGIN IMMEDIATE")){setError("Facility-Änderung konnte nicht gestartet werden");return false;}sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"INSERT INTO facility_assets(name,category,location,status) VALUES(?,?,?,?)",-1,&statement,nullptr)==SQLITE_OK;if(ok){BindText(statement,1,asset.name);BindText(statement,2,asset.category);BindText(statement,3,asset.location);BindText(statement,4,asset.status);ok=sqlite3_step(statement)==SQLITE_DONE;}if(statement)sqlite3_finalize(statement);if(ok){newId=sqlite3_last_insert_rowid(db_);ok=audit(actor,L"facility_asset.created",asset.name);}if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");if(!ok)setError("Facility-Asset konnte nicht angelegt werden");return ok;}
bool EnterpriseDatabase::updateFacilityAsset(const EnterpriseFacilityAsset& asset,const std::wstring& actor){if(asset.id<=0||asset.name.empty()||asset.category.empty()||asset.location.empty()||(asset.status!=L"operational"&&asset.status!=L"maintenance"&&asset.status!=L"out_of_service")){error_=L"Ungültige Asset-Daten.";return false;}if(!execute("BEGIN IMMEDIATE")){setError("Facility-Änderung konnte nicht gestartet werden");return false;}sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"UPDATE facility_assets SET name=?,category=?,location=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1",-1,&statement,nullptr)==SQLITE_OK;if(ok){BindText(statement,1,asset.name);BindText(statement,2,asset.category);BindText(statement,3,asset.location);BindText(statement,4,asset.status);sqlite3_bind_int64(statement,5,asset.id);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);if(ok)ok=audit(actor,L"facility_asset.updated",std::to_wstring(asset.id));if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");if(!ok)setError("Facility-Asset konnte nicht bearbeitet werden");return ok;}
bool EnterpriseDatabase::archiveFacilityAsset(int64_t assetId,const std::wstring& actor){if(!execute("BEGIN IMMEDIATE")){setError("Facility-Änderung konnte nicht gestartet werden");return false;}sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"UPDATE facility_assets SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1",-1,&statement,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(statement,1,assetId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);if(ok)ok=audit(actor,L"facility_asset.archived",std::to_wstring(assetId));if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");if(!ok)setError("Facility-Asset konnte nicht archiviert werden");return ok;}

bool EnterpriseDatabase::applySyncedFacilityAsset(const std::wstring& externalId,const EnterpriseFacilityAsset& asset,const std::wstring& actor){
    if(externalId.empty()||asset.name.empty()||asset.category.empty()||asset.location.empty()||(asset.status!=L"operational"&&asset.status!=L"maintenance"&&asset.status!=L"out_of_service")){error_=L"Ungültige zentrale Facility-Asset-Daten.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false; sqlite3_stmt* s=nullptr; int64_t local=0;
    bool ok=sqlite3_prepare_v2(db_,"SELECT local_id FROM sync_object_links WHERE object_type='facility_asset' AND external_id=?",-1,&s,nullptr)==SQLITE_OK;
    if(ok){BindText(s,1,externalId);if(sqlite3_step(s)==SQLITE_ROW)local=sqlite3_column_int64(s,0);} if(s)sqlite3_finalize(s);s=nullptr;
    if(ok&&local==0){ok=sqlite3_prepare_v2(db_,"INSERT INTO facility_assets(name,category,location,status,active) VALUES(?,?,?,?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,asset.name);BindText(s,2,asset.category);BindText(s,3,asset.location);BindText(s,4,asset.status);sqlite3_bind_int(s,5,asset.active?1:0);ok=sqlite3_step(s)==SQLITE_DONE;}if(s)sqlite3_finalize(s);s=nullptr;if(ok){local=sqlite3_last_insert_rowid(db_);ok=sqlite3_prepare_v2(db_,"INSERT INTO sync_object_links(object_type,external_id,local_id) VALUES('facility_asset',?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,externalId);sqlite3_bind_int64(s,2,local);ok=sqlite3_step(s)==SQLITE_DONE;}}}
    else if(ok){ok=sqlite3_prepare_v2(db_,"UPDATE facility_assets SET name=?,category=?,location=?,status=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,asset.name);BindText(s,2,asset.category);BindText(s,3,asset.location);BindText(s,4,asset.status);sqlite3_bind_int(s,5,asset.active?1:0);sqlite3_bind_int64(s,6,local);ok=sqlite3_step(s)==SQLITE_DONE&&sqlite3_changes(db_)==1;}}
    if(s)sqlite3_finalize(s);if(ok)ok=audit(actor,L"sync.facility_asset.applied",externalId);execute(ok?"COMMIT":"ROLLBACK");if(!ok)setError("Zentrales Facility-Asset konnte nicht übernommen werden");return ok;
}

std::vector<EnterpriseMaintenanceOrder> EnterpriseDatabase::maintenanceOrders(int64_t assetId, bool includeArchived) {
    std::vector<EnterpriseMaintenanceOrder> result; sqlite3_stmt* statement = nullptr;
    const char* sql = includeArchived ? "SELECT id,asset_id,title,status,due_at,recurrence_days,active FROM maintenance_orders WHERE (?=0 OR asset_id=?) ORDER BY active DESC,CASE status WHEN 'in_progress' THEN 0 WHEN 'open' THEN 1 ELSE 2 END,due_at" : "SELECT id,asset_id,title,status,due_at,recurrence_days,active FROM maintenance_orders WHERE active=1 AND (?=0 OR asset_id=?) ORDER BY CASE status WHEN 'in_progress' THEN 0 WHEN 'open' THEN 1 ELSE 2 END,due_at";
    if (sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) != SQLITE_OK) { setError("Wartungsaufträge konnten nicht geladen werden"); return result; }
    sqlite3_bind_int64(statement, 1, assetId); sqlite3_bind_int64(statement, 2, assetId);
    while (sqlite3_step(statement) == SQLITE_ROW) result.push_back({sqlite3_column_int64(statement,0),sqlite3_column_int64(statement,1),Wide(sqlite3_column_text(statement,2)),Wide(sqlite3_column_text(statement,3)),sqlite3_column_int64(statement,4),sqlite3_column_int(statement,5),sqlite3_column_int(statement,6)!=0});
    sqlite3_finalize(statement); return result;
}

bool EnterpriseDatabase::createMaintenanceOrder(const EnterpriseMaintenanceOrder& order, int64_t& newId, const std::wstring& actor) {
    if (order.assetId <= 0 || order.title.empty() || order.dueAt < 0 || order.recurrenceDays < 0 || order.recurrenceDays > 3660) { error_ = L"Ungültige Wartungsdaten."; return false; }
    if(!execute("BEGIN IMMEDIATE")){setError("Wartungsänderung konnte nicht gestartet werden");return false;}
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "INSERT INTO maintenance_orders(asset_id,title,status,due_at,recurrence_days) SELECT ?,?,?,?,? WHERE EXISTS(SELECT 1 FROM facility_assets WHERE id=? AND active=1)", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement,1,order.assetId); BindText(statement,2,order.title); BindText(statement,3,order.status.empty()?L"open":order.status); sqlite3_bind_int64(statement,4,order.dueAt); sqlite3_bind_int(statement,5,order.recurrenceDays); sqlite3_bind_int64(statement,6,order.assetId); ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) { newId=sqlite3_last_insert_rowid(db_); ok=audit(actor,L"maintenance_order.created",std::to_wstring(newId)); }
    if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");
    if (!ok) setError("Wartungsauftrag konnte nicht angelegt werden"); return ok;
}

bool EnterpriseDatabase::updateMaintenanceOrder(const EnterpriseMaintenanceOrder& order, const std::wstring& actor) {
    if (order.id <= 0 || order.assetId <= 0 || order.title.empty() || order.dueAt < 0 || order.recurrenceDays < 0 || order.recurrenceDays > 3660 || (order.status != L"open" && order.status != L"in_progress" && order.status != L"completed" && order.status != L"cancelled")) { error_ = L"Ungültige Wartungsdaten."; return false; }
    if(!execute("BEGIN IMMEDIATE")){setError("Wartungsänderung konnte nicht gestartet werden");return false;}
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE maintenance_orders SET asset_id=?,title=?,status=?,due_at=?,recurrence_days=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1 AND EXISTS(SELECT 1 FROM facility_assets WHERE id=? AND active=1)", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement,1,order.assetId); BindText(statement,2,order.title); BindText(statement,3,order.status); sqlite3_bind_int64(statement,4,order.dueAt); sqlite3_bind_int(statement,5,order.recurrenceDays); sqlite3_bind_int64(statement,6,order.id); sqlite3_bind_int64(statement,7,order.assetId); ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok=audit(actor,L"maintenance_order.updated",std::to_wstring(order.id));
    if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");
    if (!ok) setError("Wartungsauftrag konnte nicht bearbeitet werden"); return ok;
}

bool EnterpriseDatabase::updateMaintenanceOrderStatus(int64_t orderId, const std::wstring& status, const std::wstring& actor) {
    if (orderId <= 0 || (status != L"open" && status != L"in_progress" && status != L"completed" && status != L"cancelled")) { error_ = L"Ungültiger Wartungsstatus."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr; bool ok=sqlite3_prepare_v2(db_,"UPDATE maintenance_orders SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1 AND status<>?",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){BindText(statement,1,status);sqlite3_bind_int64(statement,2,orderId);BindText(statement,3,status);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);
    int64_t repeatedOrderId = 0;
    if (ok && status == L"completed") {
        EnterpriseMaintenanceOrder source; statement = nullptr;
        ok = sqlite3_prepare_v2(db_, "SELECT id,asset_id,title,status,due_at,recurrence_days,active FROM maintenance_orders WHERE id=?", -1, &statement, nullptr) == SQLITE_OK;
        if (ok) { sqlite3_bind_int64(statement,1,orderId); if(sqlite3_step(statement)==SQLITE_ROW) source={sqlite3_column_int64(statement,0),sqlite3_column_int64(statement,1),Wide(sqlite3_column_text(statement,2)),Wide(sqlite3_column_text(statement,3)),sqlite3_column_int64(statement,4),sqlite3_column_int(statement,5),sqlite3_column_int(statement,6)!=0}; else ok=false; }
        if(statement)sqlite3_finalize(statement);
        bool alreadyGenerated=false;
        if(ok&&source.recurrenceDays>0){statement=nullptr;ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM audit_log WHERE action='maintenance_order.recurrence.created' AND subject LIKE ? LIMIT 1",-1,&statement,nullptr)==SQLITE_OK;if(ok){const auto pattern=std::to_string(orderId)+"->%";sqlite3_bind_text(statement,1,pattern.c_str(),-1,SQLITE_TRANSIENT);const int step=sqlite3_step(statement);alreadyGenerated=step==SQLITE_ROW;ok=step==SQLITE_ROW||step==SQLITE_DONE;}if(statement)sqlite3_finalize(statement);}
        if(ok && source.recurrenceDays>0 && !alreadyGenerated){const int64_t delta=static_cast<int64_t>(source.recurrenceDays)*86400;const int64_t base=source.dueAt>0?source.dueAt:static_cast<int64_t>(std::time(nullptr));if(source.recurrenceDays>3660 || base<0 || base>std::numeric_limits<int64_t>::max()-delta){execute("ROLLBACK");error_=L"Die nächste Wartungsfälligkeit liegt außerhalb des unterstützten Zeitbereichs.";return false;}const int64_t nextDue=base+delta;statement=nullptr;ok=sqlite3_prepare_v2(db_,"INSERT INTO maintenance_orders(asset_id,title,status,due_at,recurrence_days) VALUES(?,?,'open',?,?)",-1,&statement,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(statement,1,source.assetId);BindText(statement,2,source.title);sqlite3_bind_int64(statement,3,nextDue);sqlite3_bind_int(statement,4,source.recurrenceDays);ok=sqlite3_step(statement)==SQLITE_DONE;}if(statement)sqlite3_finalize(statement);if(ok)repeatedOrderId=sqlite3_last_insert_rowid(db_);}
    }
    if(ok)ok=audit(actor,L"maintenance_order.status.changed",std::to_wstring(orderId)+L":"+status);if(ok&&repeatedOrderId>0)ok=audit(actor,L"maintenance_order.recurrence.created",std::to_wstring(orderId)+L"->"+std::to_wstring(repeatedOrderId));if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");if(!ok)setError("Wartungsstatus konnte nicht geändert werden");return ok;
}

bool EnterpriseDatabase::archiveMaintenanceOrder(int64_t orderId, const std::wstring& actor) {
    if(!execute("BEGIN IMMEDIATE")){setError("Wartungsänderung konnte nicht gestartet werden");return false;}
    sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"UPDATE maintenance_orders SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_int64(statement,1,orderId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);
    if(ok)ok=audit(actor,L"maintenance_order.archived",std::to_wstring(orderId));if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");
    if(!ok)setError("Wartungsauftrag konnte nicht archiviert werden");return ok;
}

bool EnterpriseDatabase::applySyncedMaintenanceOrder(const std::wstring& externalId,const std::wstring& assetExternalId,const EnterpriseMaintenanceOrder& order,const std::wstring& actor){
    if(externalId.empty()||assetExternalId.empty()||order.title.empty()||order.dueAt<0||order.recurrenceDays<0||order.recurrenceDays>3660||(order.status!=L"open"&&order.status!=L"in_progress"&&order.status!=L"completed"&&order.status!=L"cancelled")){error_=L"Ungültige zentrale Wartungsdaten.";return false;}
    const int64_t assetId=syncedLocalId(L"facility_asset",assetExternalId);if(assetId<=0){error_=L"Zentrales Facility-Asset für Wartungsauftrag fehlt.";return false;}if(!execute("BEGIN IMMEDIATE"))return false;sqlite3_stmt* s=nullptr;int64_t local=0;
    bool ok=sqlite3_prepare_v2(db_,"SELECT local_id FROM sync_object_links WHERE object_type='maintenance_order' AND external_id=?",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,externalId);if(sqlite3_step(s)==SQLITE_ROW)local=sqlite3_column_int64(s,0);}if(s)sqlite3_finalize(s);s=nullptr;
    if(ok&&local==0){ok=sqlite3_prepare_v2(db_,"INSERT INTO maintenance_orders(asset_id,title,status,due_at,recurrence_days,active) VALUES(?,?,?,?,?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(s,1,assetId);BindText(s,2,order.title);BindText(s,3,order.status);sqlite3_bind_int64(s,4,order.dueAt);sqlite3_bind_int(s,5,order.recurrenceDays);sqlite3_bind_int(s,6,order.active?1:0);ok=sqlite3_step(s)==SQLITE_DONE;}if(s)sqlite3_finalize(s);s=nullptr;if(ok){local=sqlite3_last_insert_rowid(db_);ok=sqlite3_prepare_v2(db_,"INSERT INTO sync_object_links(object_type,external_id,local_id) VALUES('maintenance_order',?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,externalId);sqlite3_bind_int64(s,2,local);ok=sqlite3_step(s)==SQLITE_DONE;}}}
    else if(ok){ok=sqlite3_prepare_v2(db_,"UPDATE maintenance_orders SET asset_id=?,title=?,status=?,due_at=?,recurrence_days=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",-1,&s,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(s,1,assetId);BindText(s,2,order.title);BindText(s,3,order.status);sqlite3_bind_int64(s,4,order.dueAt);sqlite3_bind_int(s,5,order.recurrenceDays);sqlite3_bind_int(s,6,order.active?1:0);sqlite3_bind_int64(s,7,local);ok=sqlite3_step(s)==SQLITE_DONE&&sqlite3_changes(db_)==1;}}
    if(s)sqlite3_finalize(s);if(ok)ok=audit(actor,L"sync.maintenance_order.applied",externalId);execute(ok?"COMMIT":"ROLLBACK");if(!ok)setError("Zentraler Wartungsauftrag konnte nicht übernommen werden");return ok;
}

std::vector<EnterpriseEmployee> EnterpriseDatabase::employees(bool includeArchived) {
    std::vector<EnterpriseEmployee> result; sqlite3_stmt* statement = nullptr;
    const char* sql = includeArchived ? "SELECT id,name,role,department,presence,active FROM employees ORDER BY active DESC,name" : "SELECT id,name,role,department,presence,active FROM employees WHERE active=1 ORDER BY name";
    if (sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) != SQLITE_OK) { setError("Mitarbeiter konnten nicht geladen werden"); return result; }
    while (sqlite3_step(statement) == SQLITE_ROW) result.push_back({sqlite3_column_int64(statement, 0), Wide(sqlite3_column_text(statement, 1)), Wide(sqlite3_column_text(statement, 2)), Wide(sqlite3_column_text(statement, 3)), Wide(sqlite3_column_text(statement, 4)), sqlite3_column_int(statement, 5) != 0});
    sqlite3_finalize(statement); return result;
}

bool EnterpriseDatabase::saveEmployeeWithRole(const EnterpriseEmployee& employee, const std::wstring& roleCode, const std::wstring& actor) {
    if (employee.id < 0 || employee.name.empty() || employee.role.empty() || employee.department.empty() || roleCode.empty()) { error_=L"Name, Stellenbezeichnung, Bereich und Systemrolle sind Pflichtangaben."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement=nullptr; int64_t id=employee.id;
    const char* sql=id ? "UPDATE employees SET name=?,role=?,department=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1" : "INSERT INTO employees(name,role,department,presence) VALUES(?,?,?,'office')";
    bool ok=sqlite3_prepare_v2(db_,sql,-1,&statement,nullptr)==SQLITE_OK;
    if(ok){BindText(statement,1,employee.name);BindText(statement,2,employee.role);BindText(statement,3,employee.department);if(id)sqlite3_bind_int64(statement,4,id);ok=sqlite3_step(statement)==SQLITE_DONE && sqlite3_changes(db_)==1;}
    if(statement)sqlite3_finalize(statement);statement=nullptr;
    if(ok&&!id)id=sqlite3_last_insert_rowid(db_);
    if(ok)ok=sqlite3_prepare_v2(db_,"DELETE FROM employee_roles WHERE employee_id=?",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_int64(statement,1,id);ok=sqlite3_step(statement)==SQLITE_DONE;}if(statement)sqlite3_finalize(statement);statement=nullptr;
    if(ok)ok=sqlite3_prepare_v2(db_,"INSERT INTO employee_roles(employee_id,role_id) SELECT ?,id FROM roles WHERE code=? AND active=1",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_int64(statement,1,id);BindText(statement,2,roleCode);ok=sqlite3_step(statement)==SQLITE_DONE && sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);
    if(ok)ok=audit(actor,employee.id?L"employee.updated":L"employee.created",employee.id?std::to_wstring(id):employee.name);
    if(ok)ok=audit(actor,L"employee.role.replaced",std::to_wstring(id)+L":"+roleCode);
    if(ok)ok=execute("COMMIT");
    if(!ok){execute("ROLLBACK");setError("Mitarbeiter und Systemrolle konnten nicht gespeichert werden");}
    return ok;
}

bool EnterpriseDatabase::createEmployee(const EnterpriseEmployee& employee, int64_t& newId, const std::wstring& actor) {
    newId=0;
    if (employee.name.empty() || employee.role.empty() || employee.department.empty()) { error_ = L"Name, Rolle und Bereich sind Pflichtangaben."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr; bool ok = sqlite3_prepare_v2(db_, "INSERT INTO employees(name,role,department,presence) VALUES(?,?,?,?)", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, employee.name); BindText(statement, 2, employee.role); BindText(statement, 3, employee.department); BindText(statement, 4, employee.presence.empty() ? L"office" : employee.presence); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    const int64_t inserted=ok ? sqlite3_last_insert_rowid(db_) : 0;
    if (ok) ok=audit(actor, L"employee.created", employee.name);
    if (ok) ok=execute("COMMIT");
    if (!ok) { execute("ROLLBACK"); setError("Mitarbeiter konnte nicht angelegt werden"); return false; }
    newId=inserted; return true;
}

bool EnterpriseDatabase::updateEmployee(const EnterpriseEmployee& employee, const std::wstring& actor) {
    if (employee.id <= 0 || employee.name.empty() || employee.role.empty() || employee.department.empty()) { error_ = L"Name, Rolle und Bereich sind Pflichtangaben."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr; bool ok = sqlite3_prepare_v2(db_, "UPDATE employees SET name=?,role=?,department=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, employee.name); BindText(statement, 2, employee.role); BindText(statement, 3, employee.department); sqlite3_bind_int64(statement, 4, employee.id); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"employee.updated", std::to_wstring(employee.id));
    if (ok) ok=execute("COMMIT");
    if (!ok) { execute("ROLLBACK"); setError("Mitarbeiter konnte nicht bearbeitet werden"); return false; }
    return true;
}

bool EnterpriseDatabase::updatePresence(int64_t employeeId, const std::wstring& presence, const std::wstring& actor, int64_t actingEmployeeId) {
    if (presence != L"office" && presence != L"remote" && presence != L"meeting" && presence != L"away") { error_ = L"Ungültiger Präsenzstatus."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    // The Linux sync trigger records the authenticated actor in the same
    // transaction; old databases and Windows have no such table.
    sqlite3_stmt* statement = nullptr; bool tracking=false;
    bool ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_employee_sync_actor'",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){const int result=sqlite3_step(statement);tracking=result==SQLITE_ROW;ok=result==SQLITE_ROW||result==SQLITE_DONE;}if(statement)sqlite3_finalize(statement);statement=nullptr;
    if(ok&&tracking){ok=sqlite3_prepare_v2(db_,"UPDATE linux_employee_sync_actor SET employee_id=? WHERE singleton=1",-1,&statement,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(statement,1,actingEmployeeId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);statement=nullptr;}
    if(ok)ok = sqlite3_prepare_v2(db_, "UPDATE employees SET presence=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, presence); sqlite3_bind_int64(statement, 2, employeeId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if(ok&&tracking)ok=execute("UPDATE linux_employee_sync_actor SET employee_id=0 WHERE singleton=1");
    if (ok) ok = audit(actor, L"employee.presence.changed", std::to_wstring(employeeId) + L":" + presence);
    if (ok) ok=execute("COMMIT");
    if (!ok) { execute("ROLLBACK"); setError("Präsenz konnte nicht geändert werden"); return false; }
    return true;
}

bool EnterpriseDatabase::archiveEmployee(int64_t employeeId, const std::wstring& actor) {
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr; bool ok = sqlite3_prepare_v2(db_, "UPDATE employees SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, employeeId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"employee.archived", std::to_wstring(employeeId));
    if (ok) ok=execute("COMMIT");
    if (!ok) { execute("ROLLBACK"); setError("Mitarbeiter konnte nicht archiviert werden"); return false; }
    return true;
}

int64_t EnterpriseDatabase::syncedLocalId(const std::wstring& objectType, const std::wstring& externalId) {
    if (objectType.empty() || externalId.empty()) return 0;
    sqlite3_stmt* statement = nullptr; int64_t result = 0;
    if (sqlite3_prepare_v2(db_, "SELECT local_id FROM sync_object_links WHERE object_type=? AND external_id=?", -1, &statement, nullptr) == SQLITE_OK) {
        BindText(statement, 1, objectType); BindText(statement, 2, externalId);
        if (sqlite3_step(statement) == SQLITE_ROW) result = sqlite3_column_int64(statement, 0);
    }
    if (statement) sqlite3_finalize(statement);
    return result;
}

std::wstring EnterpriseDatabase::syncedExternalId(const std::wstring& objectType,int64_t localId){
    if(objectType.empty()||localId<=0)return L"";sqlite3_stmt* statement=nullptr;std::wstring result;
    if(sqlite3_prepare_v2(db_,"SELECT external_id FROM sync_object_links WHERE object_type=? AND local_id=?",-1,&statement,nullptr)==SQLITE_OK){BindText(statement,1,objectType);sqlite3_bind_int64(statement,2,localId);if(sqlite3_step(statement)==SQLITE_ROW)result=Wide(sqlite3_column_text(statement,0));}if(statement)sqlite3_finalize(statement);return result;
}

bool EnterpriseDatabase::ensureSyncedExternalId(const std::wstring& objectType,int64_t localId,const std::wstring& externalId){
    if(objectType.empty()||localId<=0||externalId.empty())return false;const std::wstring existing=syncedExternalId(objectType,localId);if(!existing.empty())return existing==externalId;sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"INSERT OR IGNORE INTO sync_object_links(object_type,external_id,local_id) VALUES(?,?,?)",-1,&statement,nullptr)==SQLITE_OK;if(ok){BindText(statement,1,objectType);BindText(statement,2,externalId);sqlite3_bind_int64(statement,3,localId);ok=sqlite3_step(statement)==SQLITE_DONE;}if(statement)sqlite3_finalize(statement);return ok;
}

bool EnterpriseDatabase::applySyncedEmployee(const std::wstring& externalId, const EnterpriseEmployee& employee, const std::wstring& actor) {
    if (externalId.empty() || employee.name.empty() || employee.role.empty() || employee.department.empty() ||
        (employee.presence != L"office" && employee.presence != L"remote" && employee.presence != L"meeting" && employee.presence != L"away")) { error_=L"Ungültige zentrale Mitarbeiterdaten."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement=nullptr; int64_t localId=0;
    bool ok=sqlite3_prepare_v2(db_,"SELECT local_id FROM sync_object_links WHERE object_type='employee' AND external_id=?",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){BindText(statement,1,externalId);if(sqlite3_step(statement)==SQLITE_ROW)localId=sqlite3_column_int64(statement,0);} if(statement)sqlite3_finalize(statement);statement=nullptr;
    if(ok&&localId==0){ok=sqlite3_prepare_v2(db_,"INSERT INTO employees(name,role,department,presence,active) VALUES(?,?,?,?,?)",-1,&statement,nullptr)==SQLITE_OK;if(ok){BindText(statement,1,employee.name);BindText(statement,2,employee.role);BindText(statement,3,employee.department);BindText(statement,4,employee.presence);sqlite3_bind_int(statement,5,employee.active?1:0);ok=sqlite3_step(statement)==SQLITE_DONE;}if(statement)sqlite3_finalize(statement);statement=nullptr;if(ok){localId=sqlite3_last_insert_rowid(db_);ok=sqlite3_prepare_v2(db_,"INSERT INTO sync_object_links(object_type,external_id,local_id) VALUES('employee',?,?)",-1,&statement,nullptr)==SQLITE_OK;if(ok){BindText(statement,1,externalId);sqlite3_bind_int64(statement,2,localId);ok=sqlite3_step(statement)==SQLITE_DONE;}}}else if(ok){ok=sqlite3_prepare_v2(db_,"UPDATE employees SET name=?,role=?,department=?,presence=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",-1,&statement,nullptr)==SQLITE_OK;if(ok){BindText(statement,1,employee.name);BindText(statement,2,employee.role);BindText(statement,3,employee.department);BindText(statement,4,employee.presence);sqlite3_bind_int(statement,5,employee.active?1:0);sqlite3_bind_int64(statement,6,localId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}}
    if(statement)sqlite3_finalize(statement);if(ok)ok=audit(actor,L"sync.employee.applied",externalId);execute(ok?"COMMIT":"ROLLBACK");if(!ok)setError("Zentraler Mitarbeiter konnte nicht übernommen werden");return ok;
}

std::vector<EnterpriseLeaveRequest> EnterpriseDatabase::leaveRequests(int64_t employeeId, bool includeClosed) {
    std::vector<EnterpriseLeaveRequest> result; sqlite3_stmt* statement = nullptr;
    std::string sql = "SELECT id,employee_id,starts_at,ends_at,reason,status,decided_by,active FROM leave_requests WHERE ";
    if (!includeClosed) sql += "active=1 AND ";
    if (employeeId > 0) sql += "employee_id=? "; else sql += "1=1 ";
    sql += "ORDER BY starts_at DESC";
    if (sqlite3_prepare_v2(db_, sql.c_str(), -1, &statement, nullptr) != SQLITE_OK) { setError("Urlaubsanträge konnten nicht geladen werden"); return result; }
    if (employeeId > 0) sqlite3_bind_int64(statement, 1, employeeId);
    while (sqlite3_step(statement) == SQLITE_ROW) result.push_back({sqlite3_column_int64(statement,0),sqlite3_column_int64(statement,1),sqlite3_column_int64(statement,2),sqlite3_column_int64(statement,3),Wide(sqlite3_column_text(statement,4)),Wide(sqlite3_column_text(statement,5)),sqlite3_column_int64(statement,6),sqlite3_column_int(statement,7)!=0});
    sqlite3_finalize(statement); return result;
}

bool EnterpriseDatabase::createLeaveRequest(const EnterpriseLeaveRequest& request, int64_t& newId, const std::wstring& actor) {
    newId = 0;
    if (request.employeeId <= 0 || request.endsAt <= request.startsAt || request.reason.empty()) { error_ = L"Ungültiger Urlaubsantrag."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr;
    const char* sql = "INSERT INTO leave_requests(employee_id,starts_at,ends_at,reason) SELECT ?,?,?,? WHERE EXISTS(SELECT 1 FROM employees WHERE id=? AND active=1)";
    bool ok = sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement,1,request.employeeId); sqlite3_bind_int64(statement,2,request.startsAt); sqlite3_bind_int64(statement,3,request.endsAt); BindText(statement,4,request.reason); sqlite3_bind_int64(statement,5,request.employeeId); ok=sqlite3_step(statement)==SQLITE_DONE && sqlite3_changes(db_)==1; }
    if (statement) sqlite3_finalize(statement);
    const int64_t inserted = ok ? sqlite3_last_insert_rowid(db_) : 0;
    if (ok) ok = audit(actor,L"leave_request.created",std::to_wstring(inserted));
    if (ok) ok = execute("COMMIT");
    if (!ok) { execute("ROLLBACK"); setError("Urlaubsantrag konnte nicht angelegt werden"); return false; }
    newId = inserted; return true;
}

bool EnterpriseDatabase::decideLeaveRequest(int64_t requestId, bool approved, int64_t decidedBy, const std::wstring& actor, const EnterpriseLeaveRequest* expected) {
    if (requestId<=0 || decidedBy<=0) { error_=L"Ungültige Urlaubsfreigabe."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement=nullptr;
    const char* sql=expected ? "UPDATE leave_requests SET status=?1,decided_by=?2,updated_at=CURRENT_TIMESTAMP WHERE id=?3 AND active=1 AND status='pending' AND employee_id<>?2 AND employee_id=?4 AND starts_at=?5 AND ends_at=?6 AND reason=?7 AND decided_by=?8" : "UPDATE leave_requests SET status=?1,decided_by=?2,updated_at=CURRENT_TIMESTAMP WHERE id=?3 AND active=1 AND status='pending' AND employee_id<>?2";
    bool ok=(!expected || (expected->id==requestId && expected->active && expected->status==L"pending")) && sqlite3_prepare_v2(db_,sql,-1,&statement,nullptr)==SQLITE_OK;
    if (ok) { BindText(statement,1,approved?L"approved":L"rejected"); sqlite3_bind_int64(statement,2,decidedBy); sqlite3_bind_int64(statement,3,requestId); if(expected){sqlite3_bind_int64(statement,4,expected->employeeId);sqlite3_bind_int64(statement,5,expected->startsAt);sqlite3_bind_int64(statement,6,expected->endsAt);BindText(statement,7,expected->reason);sqlite3_bind_int64(statement,8,expected->decidedBy);} ok=sqlite3_step(statement)==SQLITE_DONE && sqlite3_changes(db_)==1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok=audit(actor,approved?L"leave_request.approved":L"leave_request.rejected",std::to_wstring(requestId));
    if (ok) ok=execute("COMMIT");
    if (!ok) { execute("ROLLBACK"); setError("Urlaubsantrag konnte nicht entschieden werden"); return false; }
    return true;
}

bool EnterpriseDatabase::applySyncedLeaveRequest(const std::wstring& externalId, const EnterpriseLeaveRequest& request, const std::wstring& actor) {
    if(externalId.empty()||request.employeeId<=0||request.startsAt<=0||request.endsAt<=request.startsAt||request.reason.empty()||(request.status!=L"pending"&&request.status!=L"approved"&&request.status!=L"rejected")){error_=L"Ungültige zentrale Abwesenheitsdaten.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;
    sqlite3_stmt* s=nullptr;int64_t local=0;
    // Mobile identities can arrive before their employee master data. Preserve
    // the request and provision an explicit, auditable placeholder instead of
    // rejecting the whole synchronization batch on the foreign key.
    bool ok=sqlite3_prepare_v2(db_,"SELECT 1 FROM employees WHERE id=?",-1,&s,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_int64(s,1,request.employeeId);const int result=sqlite3_step(s);ok=result==SQLITE_ROW||result==SQLITE_DONE;}
    bool employeeExists=ok&&s&&sqlite3_data_count(s)>0;if(s)sqlite3_finalize(s);s=nullptr;
    if(ok&&!employeeExists){ok=sqlite3_prepare_v2(db_,"INSERT INTO employees(id,name,role,department,presence,active) VALUES(?,?,?,?,?,1)",-1,&s,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(s,1,request.employeeId);BindText(s,2,L"Synchronisierte Identität "+std::to_wstring(request.employeeId));BindText(s,3,L"Extern");BindText(s,4,L"Mobile Synchronisation");BindText(s,5,L"unknown");ok=sqlite3_step(s)==SQLITE_DONE;}if(s)sqlite3_finalize(s);s=nullptr;}
    if(ok){ok=sqlite3_prepare_v2(db_,"SELECT local_id FROM sync_object_links WHERE object_type='leave' AND external_id=?",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,externalId);if(sqlite3_step(s)==SQLITE_ROW)local=sqlite3_column_int64(s,0);}if(s)sqlite3_finalize(s);s=nullptr;}
    if(ok&&local>0){
        ok=sqlite3_prepare_v2(db_,"SELECT employee_id,starts_at,ends_at,reason,status,decided_by,active FROM leave_requests WHERE id=?",-1,&s,nullptr)==SQLITE_OK;
        if(ok){
            sqlite3_bind_int64(s,1,local);
            if(sqlite3_step(s)!=SQLITE_ROW)ok=false;
            else {
                const std::wstring existingStatus=Wide(sqlite3_column_text(s,4));
                const bool sameDetails=sqlite3_column_int64(s,0)==request.employeeId&&sqlite3_column_int64(s,1)==request.startsAt&&sqlite3_column_int64(s,2)==request.endsAt&&Wide(sqlite3_column_text(s,3))==request.reason&&sqlite3_column_int(s,6)==(request.active?1:0);
                const bool stalePending=sameDetails&&existingStatus!=L"pending"&&request.status==L"pending";
                const bool competing=sameDetails&&existingStatus!=L"pending"&&request.status!=L"pending"&&(existingStatus!=request.status||sqlite3_column_int64(s,5)!=request.decidedBy);
                if(!sameDetails||competing){sqlite3_finalize(s);execute("ROLLBACK");error_=L"Urlaubskonflikt: Lokale und zentrale Entscheidung unterscheiden sich.";return false;}
                if(stalePending){sqlite3_finalize(s);execute("ROLLBACK");return true;}
            }
        }
        if(s)sqlite3_finalize(s);s=nullptr;
    }
    if(ok&&local==0){ok=sqlite3_prepare_v2(db_,"INSERT INTO leave_requests(employee_id,starts_at,ends_at,reason,status,decided_by,active) VALUES(?,?,?,?,?,?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(s,1,request.employeeId);sqlite3_bind_int64(s,2,request.startsAt);sqlite3_bind_int64(s,3,request.endsAt);BindText(s,4,request.reason);BindText(s,5,request.status);sqlite3_bind_int64(s,6,request.decidedBy);sqlite3_bind_int(s,7,request.active?1:0);ok=sqlite3_step(s)==SQLITE_DONE;}if(s)sqlite3_finalize(s);s=nullptr;if(ok){local=sqlite3_last_insert_rowid(db_);ok=sqlite3_prepare_v2(db_,"INSERT INTO sync_object_links(object_type,external_id,local_id) VALUES('leave',?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,externalId);sqlite3_bind_int64(s,2,local);ok=sqlite3_step(s)==SQLITE_DONE;}}}else if(ok){ok=sqlite3_prepare_v2(db_,"UPDATE leave_requests SET employee_id=?,starts_at=?,ends_at=?,reason=?,status=?,decided_by=?,active=? WHERE id=?",-1,&s,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(s,1,request.employeeId);sqlite3_bind_int64(s,2,request.startsAt);sqlite3_bind_int64(s,3,request.endsAt);BindText(s,4,request.reason);BindText(s,5,request.status);sqlite3_bind_int64(s,6,request.decidedBy);sqlite3_bind_int(s,7,request.active?1:0);sqlite3_bind_int64(s,8,local);ok=sqlite3_step(s)==SQLITE_DONE;}}
    if(s)sqlite3_finalize(s);if(ok)ok=audit(actor,L"sync.leave.applied",externalId);execute(ok?"COMMIT":"ROLLBACK");if(!ok)setError("Zentraler Urlaubsantrag konnte nicht übernommen werden");return ok;
}

std::vector<EnterpriseOrganizationUnit> EnterpriseDatabase::organizationUnits(bool includeArchived) {
    std::vector<EnterpriseOrganizationUnit> result;
    const char* sql = includeArchived ? "SELECT id,name,code,manager_id,active FROM organization_units ORDER BY active DESC,name" : "SELECT id,name,code,manager_id,active FROM organization_units WHERE active=1 ORDER BY name";
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db_,sql,-1,&statement,nullptr)!=SQLITE_OK) { setError("Organisationseinheiten konnten nicht geladen werden"); return result; }
    while(sqlite3_step(statement)==SQLITE_ROW) result.push_back({sqlite3_column_int64(statement,0),Wide(sqlite3_column_text(statement,1)),Wide(sqlite3_column_text(statement,2)),sqlite3_column_int64(statement,3),sqlite3_column_int(statement,4)!=0}); sqlite3_finalize(statement); return result;
}

bool EnterpriseDatabase::createOrganizationUnit(const EnterpriseOrganizationUnit& unit, int64_t& newId, const std::wstring& actor) {
    if(unit.name.empty()||unit.code.empty()){error_=L"Name und Code der Organisationseinheit sind Pflichtangaben.";return false;}
    if(!execute("BEGIN IMMEDIATE")) { setError("Organisationsänderung konnte nicht gestartet werden"); return false; }
    sqlite3_stmt* statement=nullptr; bool ok=sqlite3_prepare_v2(db_,"INSERT INTO organization_units(name,code,manager_id) VALUES(?,?,?)",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){BindText(statement,1,unit.name);BindText(statement,2,unit.code);sqlite3_bind_int64(statement,3,unit.managerId);ok=sqlite3_step(statement)==SQLITE_DONE;} if(statement)sqlite3_finalize(statement); if(ok){newId=sqlite3_last_insert_rowid(db_);ok=audit(actor,L"organization_unit.created",unit.code);} if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");if(!ok)setError("Organisationseinheit konnte nicht angelegt werden");return ok;
}

bool EnterpriseDatabase::updateOrganizationUnit(const EnterpriseOrganizationUnit& unit, const std::wstring& actor) {
    if(unit.id<=0||unit.name.empty()||unit.code.empty()){error_=L"Ungültige Organisationseinheit.";return false;}
    if(!execute("BEGIN IMMEDIATE")) { setError("Organisationsänderung konnte nicht gestartet werden"); return false; }
    sqlite3_stmt* statement=nullptr; bool ok=sqlite3_prepare_v2(db_,"UPDATE organization_units SET name=?,code=?,manager_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){BindText(statement,1,unit.name);BindText(statement,2,unit.code);sqlite3_bind_int64(statement,3,unit.managerId);sqlite3_bind_int64(statement,4,unit.id);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);if(ok)ok=audit(actor,L"organization_unit.updated",std::to_wstring(unit.id));if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");if(!ok)setError("Organisationseinheit konnte nicht bearbeitet werden");return ok;
}

bool EnterpriseDatabase::archiveOrganizationUnit(int64_t unitId, const std::wstring& actor) {
    if(!execute("BEGIN IMMEDIATE")) { setError("Organisationsänderung konnte nicht gestartet werden"); return false; }
    sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"UPDATE organization_units SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_int64(statement,1,unitId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);if(ok)ok=audit(actor,L"organization_unit.archived",std::to_wstring(unitId));if(ok){if(!execute("COMMIT")){execute("ROLLBACK");ok=false;}}else execute("ROLLBACK");if(!ok)setError("Organisationseinheit konnte nicht archiviert werden");return ok;
}

bool EnterpriseDatabase::applySyncedOrganizationUnit(const std::wstring& externalId, const EnterpriseOrganizationUnit& unit, const std::wstring& actor) {
    if(externalId.empty()||unit.name.empty()||unit.code.empty()||unit.managerId<0){error_=L"Ungültige zentrale Organisationseinheit.";return false;}if(!execute("BEGIN IMMEDIATE"))return false;sqlite3_stmt* s=nullptr;int64_t local=0;bool ok=sqlite3_prepare_v2(db_,"SELECT local_id FROM sync_object_links WHERE object_type='organization' AND external_id=?",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,externalId);if(sqlite3_step(s)==SQLITE_ROW)local=sqlite3_column_int64(s,0);}if(s)sqlite3_finalize(s);s=nullptr;if(ok&&local==0){ok=sqlite3_prepare_v2(db_,"INSERT INTO organization_units(name,code,manager_id,active) VALUES(?,?,?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,unit.name);BindText(s,2,unit.code);sqlite3_bind_int64(s,3,unit.managerId);sqlite3_bind_int(s,4,unit.active?1:0);ok=sqlite3_step(s)==SQLITE_DONE;}if(s)sqlite3_finalize(s);s=nullptr;if(ok){local=sqlite3_last_insert_rowid(db_);ok=sqlite3_prepare_v2(db_,"INSERT INTO sync_object_links(object_type,external_id,local_id) VALUES('organization',?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,externalId);sqlite3_bind_int64(s,2,local);ok=sqlite3_step(s)==SQLITE_DONE;}}}else if(ok){ok=sqlite3_prepare_v2(db_,"UPDATE organization_units SET name=?,code=?,manager_id=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,unit.name);BindText(s,2,unit.code);sqlite3_bind_int64(s,3,unit.managerId);sqlite3_bind_int(s,4,unit.active?1:0);sqlite3_bind_int64(s,5,local);ok=sqlite3_step(s)==SQLITE_DONE&&sqlite3_changes(db_)==1;}}if(s)sqlite3_finalize(s);if(ok)ok=audit(actor,L"sync.organization.applied",externalId);execute(ok?"COMMIT":"ROLLBACK");if(!ok)setError("Zentrale Organisationseinheit konnte nicht übernommen werden");return ok;
}

bool EnterpriseDatabase::assignRole(int64_t employeeId, const std::wstring& roleCode, const std::wstring& actor) {
    if (employeeId <= 0 || roleCode.empty() || !execute("BEGIN IMMEDIATE")) { error_ = L"Rolle konnte nicht zugeordnet werden"; return false; }
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "SELECT 1 FROM employees WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, employeeId); ok = sqlite3_step(statement) == SQLITE_ROW; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = execute(("DELETE FROM employee_roles WHERE employee_id=" + std::to_string(employeeId)).c_str());
    statement = nullptr;
    if (ok) ok = sqlite3_prepare_v2(db_, "INSERT INTO employee_roles(employee_id,role_id) SELECT ?,id FROM roles WHERE code=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, employeeId); BindText(statement, 2, roleCode); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"employee.role.replaced", std::to_wstring(employeeId) + L":" + roleCode);
    execute(ok ? "COMMIT" : "ROLLBACK");
    if (!ok) setError("Rolle konnte nicht zugeordnet werden");
    return ok;
}

std::vector<EnterpriseRole> EnterpriseDatabase::roles(bool includeInactive) {
    std::vector<EnterpriseRole> result;
    const char* sql = includeInactive ? "SELECT id,code,name,description,active FROM roles ORDER BY name" : "SELECT id,code,name,description,active FROM roles WHERE active=1 ORDER BY name";
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) != SQLITE_OK) { setError("Rollen konnten nicht geladen werden"); return result; }
    while (sqlite3_step(statement) == SQLITE_ROW) result.push_back({sqlite3_column_int64(statement,0),Wide(sqlite3_column_text(statement,1)),Wide(sqlite3_column_text(statement,2)),Wide(sqlite3_column_text(statement,3)),sqlite3_column_int(statement,4)!=0});
    sqlite3_finalize(statement); return result;
}

std::vector<std::wstring> EnterpriseDatabase::permissionsForRole(int64_t roleId) {
    std::vector<std::wstring> result; sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db_, "SELECT permission FROM role_permissions WHERE role_id=? ORDER BY permission", -1, &statement, nullptr) != SQLITE_OK) { setError("Berechtigungen konnten nicht geladen werden"); return result; }
    sqlite3_bind_int64(statement, 1, roleId); while (sqlite3_step(statement) == SQLITE_ROW) result.push_back(Wide(sqlite3_column_text(statement, 0))); sqlite3_finalize(statement); return result;
}

bool EnterpriseDatabase::createRole(const EnterpriseRole& role, int64_t& newId, const std::vector<std::wstring>& permissions, const std::wstring& actor) {
    if (role.code.empty() || role.name.empty() || permissions.empty() || !execute("BEGIN IMMEDIATE")) { error_ = L"Rolle konnte nicht angelegt werden"; return false; }
    sqlite3_stmt* statement = nullptr; bool ok = sqlite3_prepare_v2(db_, "INSERT INTO roles(code,name,description) VALUES(?,?,?)", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, role.code); BindText(statement, 2, role.name); BindText(statement, 3, role.description); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement); if (ok) newId = sqlite3_last_insert_rowid(db_);
    for (const std::wstring& permission : permissions) { statement = nullptr; if (permission.empty()) { ok = false; break; } if (sqlite3_prepare_v2(db_, "INSERT INTO role_permissions(role_id,permission) VALUES(?,?)", -1, &statement, nullptr) != SQLITE_OK) { ok = false; } else { sqlite3_bind_int64(statement, 1, newId); BindText(statement, 2, permission); ok = sqlite3_step(statement) == SQLITE_DONE; } if (statement) sqlite3_finalize(statement); if (!ok) break; }
    if (ok) ok = audit(actor, L"role.created", role.code); execute(ok ? "COMMIT" : "ROLLBACK"); if (!ok) setError("Rolle konnte nicht angelegt werden"); return ok;
}

bool EnterpriseDatabase::updateRole(const EnterpriseRole& role, const std::vector<std::wstring>& permissions, const std::wstring& actor) {
    if (role.id <= 0 || role.code.empty() || role.name.empty() || permissions.empty() || !execute("BEGIN IMMEDIATE")) { error_ = L"Rolle konnte nicht bearbeitet werden"; return false; }
    sqlite3_stmt* statement = nullptr; bool ok = sqlite3_prepare_v2(db_, "UPDATE roles SET code=?,name=?,description=? WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, role.code); BindText(statement, 2, role.name); BindText(statement, 3, role.description); sqlite3_bind_int64(statement, 4, role.id); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) { statement = nullptr; ok = sqlite3_prepare_v2(db_, "DELETE FROM role_permissions WHERE role_id=?", -1, &statement, nullptr) == SQLITE_OK; if (ok) { sqlite3_bind_int64(statement, 1, role.id); ok = sqlite3_step(statement) == SQLITE_DONE; } if (statement) sqlite3_finalize(statement); }
    for (const std::wstring& permission : permissions) { statement = nullptr; if (permission.empty()) { ok = false; break; } if (sqlite3_prepare_v2(db_, "INSERT INTO role_permissions(role_id,permission) VALUES(?,?)", -1, &statement, nullptr) != SQLITE_OK) { ok = false; } else { sqlite3_bind_int64(statement, 1, role.id); BindText(statement, 2, permission); ok = sqlite3_step(statement) == SQLITE_DONE; } if (statement) sqlite3_finalize(statement); if (!ok) break; }
    if (ok) ok = audit(actor, L"role.updated", role.code); execute(ok ? "COMMIT" : "ROLLBACK"); if (!ok) setError("Rolle konnte nicht bearbeitet werden"); return ok;
}

bool EnterpriseDatabase::archiveRole(int64_t roleId, const std::wstring& actor) {
    sqlite3_stmt* statement = nullptr; bool ok = sqlite3_prepare_v2(db_, "UPDATE roles SET active=0 WHERE id=? AND active=1 AND code<>'admin'", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, roleId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement); if (ok) ok = audit(actor, L"role.archived", std::to_wstring(roleId)); if (!ok) setError("Rolle konnte nicht archiviert werden; Systemadministration bleibt geschützt."); return ok;
}

std::vector<std::wstring> EnterpriseDatabase::rolesForEmployee(int64_t employeeId) {
    std::vector<std::wstring> result; sqlite3_stmt* statement = nullptr;
    const char* sql = "SELECT r.code FROM roles r JOIN employee_roles er ON er.role_id=r.id WHERE er.employee_id=? AND r.active=1 ORDER BY r.code";
    if (sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) != SQLITE_OK) { setError("Benutzerrollen konnten nicht geladen werden"); return result; }
    sqlite3_bind_int64(statement,1,employeeId); while(sqlite3_step(statement)==SQLITE_ROW) result.push_back(Wide(sqlite3_column_text(statement,0))); sqlite3_finalize(statement); return result;
}

bool EnterpriseDatabase::hasPermission(int64_t employeeId, const std::wstring& permission) {
    sqlite3_stmt* statement = nullptr;
    const char* sql = "SELECT 1 FROM employee_roles er JOIN roles r ON r.id=er.role_id AND r.active=1 JOIN role_permissions rp ON rp.role_id=r.id WHERE er.employee_id=? AND (rp.permission='*' OR rp.permission=?) LIMIT 1";
    if (sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) != SQLITE_OK) { setError("Berechtigung konnte nicht geprüft werden"); return false; }
    sqlite3_bind_int64(statement, 1, employeeId); BindText(statement, 2, permission); const bool allowed = sqlite3_step(statement) == SQLITE_ROW; sqlite3_finalize(statement); return allowed;
}

bool EnterpriseDatabase::backupTo(const std::wstring& destinationPath, const std::wstring& actor, bool recordAudit) {
    sqlite3* destination = nullptr;
    const std::string path = Utf8(destinationPath);
    if (sqlite3_open_v2(path.c_str(), &destination, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nullptr) != SQLITE_OK) { if (destination) sqlite3_close(destination); error_ = L"Backup-Zieldatei konnte nicht geöffnet werden."; return false; }
    sqlite3_backup* backup = sqlite3_backup_init(destination, "main", db_, "main");
    bool ok = backup != nullptr;
    if (ok) { const int step = sqlite3_backup_step(backup, -1); const int finish = sqlite3_backup_finish(backup); ok = step == SQLITE_DONE && finish == SQLITE_OK; }
    sqlite3_close(destination);
    if (ok && recordAudit) ok = recordBackup(destinationPath, actor);
    if (!ok) setError("Datenbank-Backup konnte nicht erstellt werden"); return ok;
}

bool EnterpriseDatabase::recordBackup(const std::wstring& destinationPath, const std::wstring& actor) {
    return audit(actor, L"database.backup.created", destinationPath);
}

bool EnterpriseDatabase::restoreFrom(const std::wstring& sourcePath, const std::wstring& actor, bool recordAudit) {
    sqlite3* source = nullptr;
    const std::string path = Utf8(sourcePath);
    if (sqlite3_open_v2(path.c_str(), &source, SQLITE_OPEN_READONLY, nullptr) != SQLITE_OK) { if (source) sqlite3_close(source); error_ = L"Backup-Quelldatei konnte nicht geöffnet werden."; return false; }
    sqlite3_backup* restore = sqlite3_backup_init(db_, "main", source, "main");
    bool ok = restore != nullptr;
    if (ok) { const int step = sqlite3_backup_step(restore, -1); const int finish = sqlite3_backup_finish(restore); ok = step == SQLITE_DONE && finish == SQLITE_OK; }
    sqlite3_close(source);
    if (ok && recordAudit) ok = audit(actor, L"database.restore.completed", sourcePath);
    if (!ok) setError("Datenbank konnte nicht wiederhergestellt werden"); return ok;
}

bool EnterpriseDatabase::exportRoomsCsv(const std::wstring& destinationPath, const std::wstring& actor, bool recordAudit) {
    std::ofstream output(Utf8(destinationPath), std::ios::trunc | std::ios::binary);
    if (!output) { error_ = L"Exportdatei konnte nicht erstellt werden."; return false; }
    auto quote = [](const std::wstring& value) { std::string text = Utf8(value); size_t pos = 0; while ((pos = text.find('"', pos)) != std::string::npos) { text.insert(pos, 1, '"'); pos += 2; } return std::string("\"") + text + "\""; };
    output << "id,name,location,capacity,equipment,active\n";
    for (const EnterpriseRoom& room : rooms(true)) output << room.id << ',' << quote(room.name) << ',' << quote(room.location) << ',' << room.capacity << ',' << quote(room.equipment) << ',' << (room.active ? 1 : 0) << "\n";
    const bool ok = static_cast<bool>(output);
    if (!ok) { error_ = L"Raumexport konnte nicht geschrieben werden."; return false; }
    return !recordAudit || recordRoomExport(destinationPath, actor);
}

bool EnterpriseDatabase::recordRoomExport(const std::wstring& destinationPath, const std::wstring& actor) {
    return audit(actor, L"rooms.exported.csv", destinationPath);
}

bool EnterpriseDatabase::importRoomsCsv(const std::wstring& sourcePath, int& importedCount, const std::wstring& actor) {
    importedCount = 0;
    std::ifstream input(Utf8(sourcePath), std::ios::binary);
    if (!input) { error_ = L"Importdatei konnte nicht geöffnet werden."; return false; }
    std::vector<std::vector<std::string>> records; std::vector<std::string> fields; std::string field;
    bool quoted=false, closed=false, start=true, malformed=false; char c;
    while (input.get(c)) {
        if (c=='\0') { malformed=true; break; }
        if (quoted) { if (c=='"') { if (input.peek()=='"') { input.get(); field+='"'; } else { quoted=false; closed=true; } } else field+=c; continue; }
        if (c=='"') { if (!start || closed) { malformed=true; break; } quoted=true;start=false;continue; }
        if (c==',' || c=='\n' || c=='\r') { fields.push_back(field);field.clear();start=true;closed=false;
            if (c!=',') { if (c=='\r' && input.peek()=='\n') input.get();records.push_back(fields);fields.clear(); } continue; }
        if (closed) { malformed=true;break; } field+=c;start=false;
    }
    if (quoted || input.bad()) malformed=true;
    if (!fields.empty() || !field.empty() || closed) { fields.push_back(field);records.push_back(fields); }
    if (malformed || records.empty() || records[0] != std::vector<std::string>({"id","name","location","capacity","equipment","active"})) {error_=L"Ungültiges CSV-Format oder Header.";return false;}
    if (!execute("BEGIN IMMEDIATE")) return false;
    bool ok=true;int count=0;
    for(size_t i=1;i<records.size() && ok;++i) {
        const auto& f=records[i];if(f.size()!=6 || f[1].empty() || f[2].empty() || (f[5]!="0" && f[5]!="1")) {ok=false;break;}
        int capacity=0;try {size_t pos=0;capacity=std::stoi(f[3],&pos);if(pos!=f[3].size() || capacity<=0) ok=false;}catch(...) {ok=false;}if(!ok)break;
        auto name=Wide(reinterpret_cast<const unsigned char*>(f[1].c_str()));auto location=Wide(reinterpret_cast<const unsigned char*>(f[2].c_str()));auto equipment=Wide(reinterpret_cast<const unsigned char*>(f[4].c_str()));
        if(Utf8(name)!=f[1] || Utf8(location)!=f[2] || Utf8(equipment)!=f[4]) {ok=false;break;}
        sqlite3_stmt* stmt=nullptr;ok=sqlite3_prepare_v2(db_,"INSERT INTO rooms(name,location,capacity,equipment,active) VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET location=excluded.location,capacity=excluded.capacity,equipment=excluded.equipment,active=excluded.active,updated_at=CURRENT_TIMESTAMP",-1,&stmt,nullptr)==SQLITE_OK;
        if(ok) {BindText(stmt,1,name);BindText(stmt,2,location);sqlite3_bind_int(stmt,3,capacity);BindText(stmt,4,equipment);sqlite3_bind_int(stmt,5,f[5]=="1");ok=sqlite3_step(stmt)==SQLITE_DONE;}if(stmt)sqlite3_finalize(stmt);if(ok)++count;
    }
    if(ok)ok=audit(actor,L"rooms.imported.csv",sourcePath+L":"+std::to_wstring(count));
    if(ok)ok=execute("COMMIT");else execute("ROLLBACK");
    if(!ok){execute("ROLLBACK");error_=L"Raumimport fehlgeschlagen: CSV-Daten oder Audit ungültig.";return false;}importedCount=count;return true;
}

std::vector<std::wstring> EnterpriseDatabase::auditLog(int limit) {
    std::vector<std::wstring> result; sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db_, "SELECT occurred_at,actor,action,subject FROM audit_log ORDER BY id DESC LIMIT ?", -1, &statement, nullptr) != SQLITE_OK) return result;
    sqlite3_bind_int(statement, 1, limit);
    while (sqlite3_step(statement) == SQLITE_ROW) result.push_back(Wide(sqlite3_column_text(statement, 0)) + L" | " + Wide(sqlite3_column_text(statement, 1)) + L" | " + Wide(sqlite3_column_text(statement, 2)) + L" | " + Wide(sqlite3_column_text(statement, 3)));
    sqlite3_finalize(statement); return result;
}
