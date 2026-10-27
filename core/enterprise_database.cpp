#include "enterprise_database.h"

#include <windows.h>
#include <bcrypt.h>
#include <sqlite3.h>
#include <cstring>
#include <ctime>
#include <fstream>

namespace {
std::string Utf8(const std::wstring& value) {
    if (value.empty()) return {};
    const int count = WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    std::string result(static_cast<size_t>(count), '\0');
    if (count > 0) WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), result.data(), count, nullptr, nullptr);
    return result;
}
std::wstring Wide(const unsigned char* value) {
    if (!value) return L"";
    const char* text = reinterpret_cast<const char*>(value);
    const int bytes = static_cast<int>(std::strlen(text));
    if (bytes == 0) return L"";
    const int count = MultiByteToWideChar(CP_UTF8, 0, text, bytes, nullptr, 0);
    std::wstring result(static_cast<size_t>(count), L'\0');
    if (count > 0) MultiByteToWideChar(CP_UTF8, 0, text, bytes, result.data(), count);
    return result;
}
void BindText(sqlite3_stmt* statement, int index, const std::wstring& value) {
    const std::string utf8 = Utf8(value);
    sqlite3_bind_text(statement, index, utf8.c_str(), static_cast<int>(utf8.size()), SQLITE_TRANSIENT);
}
std::wstring Hex(const std::vector<unsigned char>& bytes) { static constexpr wchar_t symbols[]=L"0123456789abcdef"; std::wstring result; result.reserve(bytes.size()*2); for(unsigned char value:bytes){result+=symbols[value>>4];result+=symbols[value&15];} return result; }
bool PasswordDigest(const std::wstring& password, const std::wstring& saltHex, std::wstring& digest) {
    if(password.size()<12 || saltHex.size()!=32)return false;std::vector<unsigned char> salt(16);for(size_t i=0;i<salt.size();++i){const wchar_t high=saltHex[i*2],low=saltHex[i*2+1];auto hex=[](wchar_t c)->int{return c>=L'0'&&c<=L'9'?c-L'0':c>=L'a'&&c<=L'f'?c-L'a'+10:c>=L'A'&&c<=L'F'?c-L'A'+10:-1;};const int h=hex(high),l=hex(low);if(h<0||l<0)return false;salt[i]=static_cast<unsigned char>((h<<4)|l);}std::string utf8=Utf8(password);std::vector<unsigned char> output(32);BCRYPT_ALG_HANDLE algorithm=nullptr;const bool opened=BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,nullptr,BCRYPT_ALG_HANDLE_HMAC_FLAG)==0;const bool ok=opened&&BCryptDeriveKeyPBKDF2(algorithm,reinterpret_cast<PUCHAR>(utf8.data()),static_cast<ULONG>(utf8.size()),salt.data(),static_cast<ULONG>(salt.size()),210000,output.data(),static_cast<ULONG>(output.size()),0)==0;if(algorithm)BCryptCloseAlgorithmProvider(algorithm,0);if(!ok)return false;digest=Hex(output);return true;
}
bool ConstantTimeEquals(const std::wstring& left,const std::wstring& right){if(left.size()!=right.size())return false;unsigned int difference=0;for(size_t i=0;i<left.size();++i)difference|=static_cast<unsigned int>(left[i]^right[i]);return difference==0;}
}

EnterpriseDatabase::EnterpriseDatabase(const std::wstring& databasePath) {
    const std::string path = Utf8(databasePath);
    if (sqlite3_open_v2(path.c_str(), &db_, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nullptr) != SQLITE_OK) {
        setError("Datenbank konnte nicht geöffnet werden");
        return;
    }
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
    execute(ok ? "COMMIT" : "ROLLBACK");
    if (!ok) setError("Raum konnte nicht angelegt werden");
    return ok;
}

bool EnterpriseDatabase::updateRoom(const EnterpriseRoom& room, const std::wstring& actor) {
    if (room.id <= 0 || room.name.empty() || room.location.empty() || room.capacity <= 0) { error_ = L"Ungültige Raumdaten."; return false; }
    sqlite3_stmt* statement = nullptr;
    const char* sql = "UPDATE rooms SET name=?,location=?,capacity=?,equipment=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1";
    bool ok = sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, room.name); BindText(statement, 2, room.location); sqlite3_bind_int(statement, 3, room.capacity); BindText(statement, 4, room.equipment); sqlite3_bind_int64(statement, 5, room.id); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"room.updated", room.name);
    if (!ok) setError("Raum konnte nicht bearbeitet werden");
    return ok;
}

bool EnterpriseDatabase::archiveRoom(int64_t roomId, const std::wstring& actor) {
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE rooms SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, roomId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"room.archived", std::to_wstring(roomId));
    if (!ok) setError("Raum konnte nicht archiviert werden");
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

bool EnterpriseDatabase::createBooking(const EnterpriseBooking& booking, int64_t& newId, const std::wstring& actor) {
    if (booking.roomId <= 0 || booking.userId <= 0 || booking.endUtc <= booking.startUtc || booking.attendees <= 0) { error_ = L"Ungültige Buchungsdaten."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
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
    execute(ok ? "COMMIT" : "ROLLBACK");
    if (!ok) setError("Buchung konnte nicht angelegt werden");
    return ok;
}

bool EnterpriseDatabase::createRecurringBookings(const EnterpriseBooking& firstBooking, int intervalDays, int occurrences, std::vector<int64_t>& newIds, const std::wstring& actor) {
    newIds.clear();
    if (firstBooking.roomId <= 0 || firstBooking.userId <= 0 || firstBooking.endUtc <= firstBooking.startUtc || firstBooking.attendees <= 0 || intervalDays <= 0 || occurrences < 2 || occurrences > 104) {
        error_ = L"Ungültige Serienbuchungsdaten.";
        return false;
    }
    if (!execute("BEGIN IMMEDIATE")) return false;
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
    execute(ok ? "COMMIT" : "ROLLBACK");
    if (!ok) {
        newIds.clear();
        error_ = L"Die Buchungsserie kollidiert, überschreitet die Kapazität oder konnte nicht gespeichert werden.";
    }
    return ok;
}

bool EnterpriseDatabase::rescheduleBooking(const EnterpriseBooking& booking, const std::wstring& actor) {
    if (booking.id <= 0 || booking.roomId <= 0 || booking.endUtc <= booking.startUtc || booking.attendees <= 0) { error_ = L"Ungültige Umbuchungsdaten."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
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
    execute(ok ? "COMMIT" : "ROLLBACK"); if (!ok) setError("Buchung konnte nicht umgebucht werden"); return ok;
}

bool EnterpriseDatabase::blockRoom(int64_t roomId, int64_t startUtc, int64_t endUtc, const std::wstring& reason, int64_t& newId, const std::wstring& actor) {
    if (roomId <= 0 || endUtc <= startUtc || reason.empty()) { error_ = L"Ungültige Sperrzeit."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr;
    const char* conflict = "SELECT 1 FROM bookings WHERE room_id=? AND status IN ('confirmed','blocked') AND starts_at < ? AND ends_at > ? LIMIT 1";
    bool ok = sqlite3_prepare_v2(db_, conflict, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, roomId); sqlite3_bind_int64(statement, 2, endUtc); sqlite3_bind_int64(statement, 3, startUtc); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    if (!ok) { execute("ROLLBACK"); error_ = L"Sperrzeit kollidiert mit einer Reservierung."; return false; }
    statement = nullptr; ok = sqlite3_prepare_v2(db_, "INSERT INTO bookings(room_id,user_id,starts_at,ends_at,attendees,status) VALUES(?,?,?,?,?,'blocked')", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, roomId); sqlite3_bind_int64(statement, 2, 0); sqlite3_bind_int64(statement, 3, startUtc); sqlite3_bind_int64(statement, 4, endUtc); sqlite3_bind_int(statement, 5, 1); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    if (ok) { newId = sqlite3_last_insert_rowid(db_); ok = audit(actor, L"room.blocked", std::to_wstring(roomId) + L":" + reason); }
    execute(ok ? "COMMIT" : "ROLLBACK"); if (!ok) setError("Sperrzeit konnte nicht angelegt werden"); return ok;
}

bool EnterpriseDatabase::cancelBooking(int64_t bookingId, const std::wstring& actor) {
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE bookings SET status='cancelled' WHERE id=? AND status='confirmed'", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, bookingId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"booking.cancelled", std::to_wstring(bookingId));
    if (!ok) setError("Buchung konnte nicht storniert werden");
    return ok;
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
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "INSERT INTO projects(name,department,description,status,owner_id) VALUES(?,?,?,?,?)", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, project.name); BindText(statement, 2, project.department); BindText(statement, 3, project.description); BindText(statement, 4, project.status.empty() ? L"planned" : project.status); sqlite3_bind_int64(statement, 5, project.ownerId); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    if (ok) { newId = sqlite3_last_insert_rowid(db_); ok = audit(actor, L"project.created", project.name); }
    if (!ok) setError("Projekt konnte nicht angelegt werden"); return ok;
}

bool EnterpriseDatabase::updateProject(const EnterpriseProject& project, const std::wstring& actor) {
    if (project.id <= 0 || project.name.empty() || project.department.empty() || (project.status != L"planned" && project.status != L"active" && project.status != L"on_hold" && project.status != L"completed" && project.status != L"cancelled")) { error_ = L"Ungültige Projektdaten."; return false; }
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE projects SET name=?,department=?,description=?,status=?,owner_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, project.name); BindText(statement, 2, project.department); BindText(statement, 3, project.description); BindText(statement, 4, project.status); sqlite3_bind_int64(statement, 5, project.ownerId); sqlite3_bind_int64(statement, 6, project.id); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"project.updated", std::to_wstring(project.id));
    if (!ok) setError("Projekt konnte nicht bearbeitet werden"); return ok;
}

bool EnterpriseDatabase::archiveProject(int64_t projectId, const std::wstring& actor) {
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE projects SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, projectId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"project.archived", std::to_wstring(projectId));
    if (!ok) setError("Projekt konnte nicht archiviert werden"); return ok;
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

bool EnterpriseDatabase::assignTaskToProject(int64_t taskId, int64_t projectId, const std::wstring& actor) {
    if (taskId <= 0 || projectId <= 0) { error_ = L"Ungültige Aufgaben- oder Projektzuordnung."; return false; }
    sqlite3_stmt* statement = nullptr;
    const char* sql = "INSERT OR IGNORE INTO project_tasks(project_id,task_id) SELECT ?,? WHERE EXISTS(SELECT 1 FROM projects WHERE id=? AND active=1) AND EXISTS(SELECT 1 FROM tasks WHERE id=? AND active=1)";
    bool ok = sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, projectId); sqlite3_bind_int64(statement, 2, taskId); sqlite3_bind_int64(statement, 3, projectId); sqlite3_bind_int64(statement, 4, taskId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"project.task.assigned", std::to_wstring(projectId) + L":" + std::to_wstring(taskId));
    if (!ok) setError("Aufgabe konnte nicht dem Projekt zugeordnet werden"); return ok;
}

bool EnterpriseDatabase::setTaskProject(int64_t taskId, int64_t projectId, const std::wstring& actor) {
    if (taskId <= 0 || projectId < 0 || !execute("BEGIN IMMEDIATE")) { error_ = L"Projektzuordnung konnte nicht geändert werden"; return false; }
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "SELECT 1 FROM tasks WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, taskId); ok = sqlite3_step(statement) == SQLITE_ROW; }
    if (statement) sqlite3_finalize(statement);
    if (ok && projectId > 0) { statement = nullptr; ok = sqlite3_prepare_v2(db_, "SELECT 1 FROM projects WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK; if (ok) { sqlite3_bind_int64(statement, 1, projectId); ok = sqlite3_step(statement) == SQLITE_ROW; } if (statement) sqlite3_finalize(statement); }
    if (ok) ok = execute(("DELETE FROM project_tasks WHERE task_id=" + std::to_string(taskId)).c_str());
    if (ok && projectId > 0) { statement = nullptr; ok = sqlite3_prepare_v2(db_, "INSERT INTO project_tasks(project_id,task_id) VALUES(?,?)", -1, &statement, nullptr) == SQLITE_OK; if (ok) { sqlite3_bind_int64(statement, 1, projectId); sqlite3_bind_int64(statement, 2, taskId); ok = sqlite3_step(statement) == SQLITE_DONE; } if (statement) sqlite3_finalize(statement); }
    if (ok) ok = audit(actor, L"project.task.reassigned", std::to_wstring(projectId) + L":" + std::to_wstring(taskId));
    execute(ok ? "COMMIT" : "ROLLBACK");
    if (!ok) setError("Projektzuordnung konnte nicht geändert werden"); return ok;
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

bool EnterpriseDatabase::createTask(const EnterpriseTask& task, int64_t& newId, const std::wstring& actor) {
    if (task.title.empty() || task.department.empty() || task.priority.empty() || task.dueAt < 0 || task.recurrenceDays < 0 || task.recurrenceDays > 3660) { error_ = L"Ungültige Aufgabendaten."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "INSERT INTO tasks(title,department,priority,status,assignee_id) VALUES(?,?,?,?,?)", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, task.title); BindText(statement, 2, task.department); BindText(statement, 3, task.priority); BindText(statement, 4, task.status.empty() ? L"open" : task.status); sqlite3_bind_int64(statement, 5, task.assigneeId); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    if (ok) { newId = sqlite3_last_insert_rowid(db_); statement = nullptr; ok = sqlite3_prepare_v2(db_, "INSERT INTO task_schedules(task_id,due_at,recurrence_days) VALUES(?,?,?)", -1, &statement, nullptr) == SQLITE_OK; if (ok) { sqlite3_bind_int64(statement, 1, newId); sqlite3_bind_int64(statement, 2, task.dueAt); sqlite3_bind_int(statement, 3, task.recurrenceDays); ok = sqlite3_step(statement) == SQLITE_DONE; } if (statement) sqlite3_finalize(statement); }
    if (ok) ok = audit(actor, L"task.created", task.title);
    execute(ok ? "COMMIT" : "ROLLBACK");
    if (!ok) setError("Aufgabe konnte nicht angelegt werden"); return ok;
}

bool EnterpriseDatabase::updateTask(const EnterpriseTask& task, const std::wstring& actor) {
    if (task.id <= 0 || task.title.empty() || task.department.empty() || task.dueAt < 0 || task.recurrenceDays < 0 || task.recurrenceDays > 3660 || (task.priority != L"Niedrig" && task.priority != L"Mittel" && task.priority != L"Hoch" && task.priority != L"Kritisch")) { error_ = L"Ungültige Aufgabendaten."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE tasks SET title=?,department=?,priority=?,assignee_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, task.title); BindText(statement, 2, task.department); BindText(statement, 3, task.priority); sqlite3_bind_int64(statement, 4, task.assigneeId); sqlite3_bind_int64(statement, 5, task.id); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    statement = nullptr; if (ok) ok = sqlite3_prepare_v2(db_, "INSERT INTO task_schedules(task_id,due_at,recurrence_days) VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET due_at=excluded.due_at,recurrence_days=excluded.recurrence_days", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, task.id); sqlite3_bind_int64(statement, 2, task.dueAt); sqlite3_bind_int(statement, 3, task.recurrenceDays); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"task.updated", std::to_wstring(task.id));
    execute(ok ? "COMMIT" : "ROLLBACK");
    if (!ok) setError("Aufgabe konnte nicht bearbeitet werden"); return ok;
}

bool EnterpriseDatabase::updateTaskStatus(int64_t taskId, const std::wstring& status, const std::wstring& actor) {
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
        if (ok && source.recurrenceDays > 0) {
            const int64_t nextDue = source.dueAt > 0 ? source.dueAt + static_cast<int64_t>(source.recurrenceDays) * 86400 : static_cast<int64_t>(std::time(nullptr)) + static_cast<int64_t>(source.recurrenceDays) * 86400;
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
    execute(ok ? "COMMIT" : "ROLLBACK");
    if (!ok) setError("Aufgabenstatus konnte nicht geändert werden"); return ok;
}

bool EnterpriseDatabase::archiveTask(int64_t taskId, const std::wstring& actor) {
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE tasks SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, taskId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"task.archived", std::to_wstring(taskId));
    if (!ok) setError("Aufgabe konnte nicht archiviert werden"); return ok;
}

bool EnterpriseDatabase::applySyncedTask(const std::wstring& externalId, const EnterpriseTask& task, const std::wstring& actor) {
    if(externalId.empty()||task.title.empty()||task.department.empty()||task.assigneeId<=0||(task.priority!=L"Niedrig"&&task.priority!=L"Mittel"&&task.priority!=L"Hoch"&&task.priority!=L"Kritisch")||(task.status!=L"open"&&task.status!=L"in_progress"&&task.status!=L"done"&&task.status!=L"cancelled")){error_=L"Ungültige zentrale Aufgabendaten.";return false;}if(!execute("BEGIN IMMEDIATE"))return false;sqlite3_stmt* statement=nullptr;int64_t localId=0;bool ok=sqlite3_prepare_v2(db_,"SELECT local_id FROM sync_object_links WHERE object_type='task' AND external_id=?",-1,&statement,nullptr)==SQLITE_OK;if(ok){BindText(statement,1,externalId);if(sqlite3_step(statement)==SQLITE_ROW)localId=sqlite3_column_int64(statement,0);}if(statement)sqlite3_finalize(statement);statement=nullptr;if(ok&&localId==0){ok=sqlite3_prepare_v2(db_,"INSERT INTO tasks(title,department,priority,status,assignee_id,active) VALUES(?,?,?,?,?,?)",-1,&statement,nullptr)==SQLITE_OK;if(ok){BindText(statement,1,task.title);BindText(statement,2,task.department);BindText(statement,3,task.priority);BindText(statement,4,task.status);sqlite3_bind_int64(statement,5,task.assigneeId);sqlite3_bind_int(statement,6,task.active?1:0);ok=sqlite3_step(statement)==SQLITE_DONE;}if(statement)sqlite3_finalize(statement);statement=nullptr;if(ok){localId=sqlite3_last_insert_rowid(db_);ok=sqlite3_prepare_v2(db_,"INSERT INTO sync_object_links(object_type,external_id,local_id) VALUES('task',?,?)",-1,&statement,nullptr)==SQLITE_OK;if(ok){BindText(statement,1,externalId);sqlite3_bind_int64(statement,2,localId);ok=sqlite3_step(statement)==SQLITE_DONE;}}}else if(ok){ok=sqlite3_prepare_v2(db_,"UPDATE tasks SET title=?,department=?,priority=?,status=?,assignee_id=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",-1,&statement,nullptr)==SQLITE_OK;if(ok){BindText(statement,1,task.title);BindText(statement,2,task.department);BindText(statement,3,task.priority);BindText(statement,4,task.status);sqlite3_bind_int64(statement,5,task.assigneeId);sqlite3_bind_int(statement,6,task.active?1:0);sqlite3_bind_int64(statement,7,localId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}}if(statement)sqlite3_finalize(statement);if(ok)ok=audit(actor,L"sync.task.applied",externalId);execute(ok?"COMMIT":"ROLLBACK");if(!ok)setError("Zentrale Aufgabe konnte nicht übernommen werden");return ok;
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
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "DELETE FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, taskId); sqlite3_bind_int64(statement, 2, dependsOnTaskId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"task.dependency.removed", std::to_wstring(taskId) + L"<-" + std::to_wstring(dependsOnTaskId));
    if (!ok) setError("Aufgabenabhängigkeit konnte nicht entfernt werden");
    return ok;
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
    sqlite3_stmt* statement = nullptr; const char* sql = "INSERT INTO task_subtasks(task_id,title,status,assignee_id,due_at) SELECT ?,?,?,?,? WHERE EXISTS(SELECT 1 FROM tasks WHERE id=? AND active=1)";
    bool ok = sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement,1,subtask.taskId); BindText(statement,2,subtask.title); BindText(statement,3,subtask.status.empty()?L"open":subtask.status); sqlite3_bind_int64(statement,4,subtask.assigneeId); sqlite3_bind_int64(statement,5,subtask.dueAt); sqlite3_bind_int64(statement,6,subtask.taskId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement); if (ok) { newId = sqlite3_last_insert_rowid(db_); ok = audit(actor,L"task.subtask.created",std::to_wstring(subtask.taskId)+L":"+subtask.title); } if (!ok) setError("Unteraufgabe konnte nicht angelegt werden"); return ok;
}

bool EnterpriseDatabase::updateSubtask(const EnterpriseSubtask& subtask, const std::wstring& actor) {
    if (subtask.id <= 0 || subtask.title.empty() || subtask.dueAt < 0 || (subtask.status != L"open" && subtask.status != L"in_progress" && subtask.status != L"done" && subtask.status != L"cancelled")) { error_ = L"Ungültige Unteraufgabe."; return false; }
    sqlite3_stmt* statement = nullptr; bool ok = sqlite3_prepare_v2(db_, "UPDATE task_subtasks SET title=?,status=?,assignee_id=?,due_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement,1,subtask.title); BindText(statement,2,subtask.status); sqlite3_bind_int64(statement,3,subtask.assigneeId); sqlite3_bind_int64(statement,4,subtask.dueAt); sqlite3_bind_int64(statement,5,subtask.id); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement); if (ok) ok = audit(actor,L"task.subtask.updated",std::to_wstring(subtask.id)); if (!ok) setError("Unteraufgabe konnte nicht bearbeitet werden"); return ok;
}

bool EnterpriseDatabase::updateSubtaskStatus(int64_t subtaskId, const std::wstring& status, const std::wstring& actor) {
    if (subtaskId <= 0 || (status != L"open" && status != L"in_progress" && status != L"done" && status != L"cancelled")) { error_ = L"Ungültiger Unteraufgabenstatus."; return false; }
    sqlite3_stmt* statement = nullptr; bool ok = sqlite3_prepare_v2(db_, "UPDATE task_subtasks SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement,1,status); sqlite3_bind_int64(statement,2,subtaskId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement); if (ok) ok = audit(actor,L"task.subtask.status.changed",std::to_wstring(subtaskId)+L":"+status); if (!ok) setError("Unteraufgabenstatus konnte nicht geändert werden"); return ok;
}

bool EnterpriseDatabase::archiveSubtask(int64_t subtaskId, const std::wstring& actor) {
    if (subtaskId <= 0) { error_ = L"Ungültige Unteraufgaben-ID."; return false; }
    sqlite3_stmt* statement = nullptr; bool ok = sqlite3_prepare_v2(db_, "UPDATE task_subtasks SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement,1,subtaskId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement); if (ok) ok = audit(actor,L"task.subtask.archived",std::to_wstring(subtaskId)); if (!ok) setError("Unteraufgabe konnte nicht archiviert werden"); return ok;
}

std::vector<EnterpriseAttachment> EnterpriseDatabase::attachmentsFor(const std::wstring& subjectType, int64_t subjectId, bool includeArchived) {
    std::vector<EnterpriseAttachment> result;
    if ((subjectType != L"task" && subjectType != L"ticket") || subjectId <= 0) { error_ = L"Ungültiger Anhangsbezug."; return result; }
    sqlite3_stmt* statement = nullptr; const char* sql = includeArchived ? "SELECT id,subject_type,subject_id,original_name,stored_path,size_bytes,sha256,uploaded_by,created_at,active FROM attachments WHERE subject_type=? AND subject_id=? ORDER BY active DESC,id DESC" : "SELECT id,subject_type,subject_id,original_name,stored_path,size_bytes,sha256,uploaded_by,created_at,active FROM attachments WHERE subject_type=? AND subject_id=? AND active=1 ORDER BY id DESC";
    if (sqlite3_prepare_v2(db_,sql,-1,&statement,nullptr)!=SQLITE_OK){setError("Anhänge konnten nicht geladen werden");return result;} BindText(statement,1,subjectType);sqlite3_bind_int64(statement,2,subjectId);while(sqlite3_step(statement)==SQLITE_ROW)result.push_back({sqlite3_column_int64(statement,0),Wide(sqlite3_column_text(statement,1)),sqlite3_column_int64(statement,2),Wide(sqlite3_column_text(statement,3)),Wide(sqlite3_column_text(statement,4)),sqlite3_column_int64(statement,5),Wide(sqlite3_column_text(statement,6)),sqlite3_column_int64(statement,7),Wide(sqlite3_column_text(statement,8)),sqlite3_column_int(statement,9)!=0});sqlite3_finalize(statement);return result;
}

bool EnterpriseDatabase::createAttachment(const EnterpriseAttachment& attachment, int64_t& newId, const std::wstring& actor) {
    if ((attachment.subjectType!=L"task"&&attachment.subjectType!=L"ticket")||attachment.subjectId<=0||attachment.originalName.empty()||attachment.storedPath.empty()||attachment.sizeBytes<0||attachment.sha256.empty()){error_=L"Ungültiger Anhang.";return false;}
    const char* subjectTable=attachment.subjectType==L"task"?"tasks":"tickets";
    sqlite3_stmt* exists=nullptr;bool ok=sqlite3_prepare_v2(db_,(std::string("SELECT 1 FROM ")+subjectTable+" WHERE id=? AND active=1").c_str(),-1,&exists,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_int64(exists,1,attachment.subjectId);ok=sqlite3_step(exists)==SQLITE_ROW;}
    if(exists)sqlite3_finalize(exists);
    sqlite3_stmt* statement=nullptr;
    if(ok)ok=sqlite3_prepare_v2(db_,"INSERT INTO attachments(subject_type,subject_id,original_name,stored_path,size_bytes,sha256,uploaded_by) VALUES(?,?,?,?,?,?,?)",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){BindText(statement,1,attachment.subjectType);sqlite3_bind_int64(statement,2,attachment.subjectId);BindText(statement,3,attachment.originalName);BindText(statement,4,attachment.storedPath);sqlite3_bind_int64(statement,5,attachment.sizeBytes);BindText(statement,6,attachment.sha256);sqlite3_bind_int64(statement,7,attachment.uploadedBy);ok=sqlite3_step(statement)==SQLITE_DONE;}
    if(statement)sqlite3_finalize(statement);if(ok){newId=sqlite3_last_insert_rowid(db_);ok=audit(actor,L"attachment.created",attachment.subjectType+L":"+std::to_wstring(attachment.subjectId)+L":"+attachment.originalName);}if(!ok)setError("Anhang konnte nicht angelegt werden");return ok;
}

bool EnterpriseDatabase::archiveAttachment(int64_t attachmentId, const std::wstring& actor) {
    if(attachmentId<=0){error_=L"Ungültige Anhangs-ID.";return false;}sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"UPDATE attachments SET active=0 WHERE id=? AND active=1",-1,&statement,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(statement,1,attachmentId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);if(ok)ok=audit(actor,L"attachment.archived",std::to_wstring(attachmentId));if(!ok)setError("Anhang konnte nicht archiviert werden");return ok;
}

bool EnterpriseDatabase::applySyncedAttachment(const std::wstring& externalId,const std::wstring& subjectExternalId,const std::wstring& uploaderExternalId,const EnterpriseAttachment& attachment,const std::wstring& actor){
    if(externalId.empty()||subjectExternalId.empty()||uploaderExternalId.empty()||(attachment.subjectType!=L"task"&&attachment.subjectType!=L"ticket")||attachment.originalName.empty()||attachment.storedPath.empty()||attachment.sizeBytes<0||attachment.sha256.size()!=64){error_=L"Ungültige zentrale Anhangsdaten.";return false;}
    const int64_t subjectId=syncedLocalId(attachment.subjectType,subjectExternalId),uploader=syncedLocalId(L"employee",uploaderExternalId);if(subjectId<=0||uploader<=0){error_=L"Anhangsbezug ist noch nicht synchronisiert.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;sqlite3_stmt* s=nullptr;int64_t local=0;bool ok=sqlite3_prepare_v2(db_,"SELECT local_id FROM sync_object_links WHERE object_type='attachment' AND external_id=?",-1,&s,nullptr)==SQLITE_OK;
    if(ok){BindText(s,1,externalId);if(sqlite3_step(s)==SQLITE_ROW)local=sqlite3_column_int64(s,0);}if(s)sqlite3_finalize(s);s=nullptr;
    if(ok&&local==0){ok=sqlite3_prepare_v2(db_,"INSERT INTO attachments(subject_type,subject_id,original_name,stored_path,size_bytes,sha256,uploaded_by,active) VALUES(?,?,?,?,?,?,?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,attachment.subjectType);sqlite3_bind_int64(s,2,subjectId);BindText(s,3,attachment.originalName);BindText(s,4,attachment.storedPath);sqlite3_bind_int64(s,5,attachment.sizeBytes);BindText(s,6,attachment.sha256);sqlite3_bind_int64(s,7,uploader);sqlite3_bind_int(s,8,attachment.active?1:0);ok=sqlite3_step(s)==SQLITE_DONE;}if(s)sqlite3_finalize(s);s=nullptr;if(ok){local=sqlite3_last_insert_rowid(db_);ok=sqlite3_prepare_v2(db_,"INSERT INTO sync_object_links(object_type,external_id,local_id) VALUES('attachment',?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,externalId);sqlite3_bind_int64(s,2,local);ok=sqlite3_step(s)==SQLITE_DONE;}}}
    else if(ok){ok=sqlite3_prepare_v2(db_,"UPDATE attachments SET subject_type=?,subject_id=?,original_name=?,stored_path=?,size_bytes=?,sha256=?,uploaded_by=?,active=? WHERE id=?",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,attachment.subjectType);sqlite3_bind_int64(s,2,subjectId);BindText(s,3,attachment.originalName);BindText(s,4,attachment.storedPath);sqlite3_bind_int64(s,5,attachment.sizeBytes);BindText(s,6,attachment.sha256);sqlite3_bind_int64(s,7,uploader);sqlite3_bind_int(s,8,attachment.active?1:0);sqlite3_bind_int64(s,9,local);ok=sqlite3_step(s)==SQLITE_DONE;}}
    if(s)sqlite3_finalize(s);if(ok)ok=audit(actor,L"sync.attachment.applied",externalId);execute(ok?"COMMIT":"ROLLBACK");if(!ok)setError("Zentraler Anhang konnte nicht übernommen werden");return ok;
}

bool EnterpriseDatabase::createOrResetAccount(int64_t employeeId,const std::wstring& username,const std::wstring& password,bool mustChangePassword,const std::wstring& actor){
    if(employeeId<=0||username.size()<3||username.find_first_of(L" \t\r\n")!=std::wstring::npos||password.size()<12){error_=L"Benutzername oder Passwort entspricht nicht der Sicherheitsrichtlinie.";return false;}std::vector<unsigned char> salt(16);if(BCryptGenRandom(nullptr,salt.data(),static_cast<ULONG>(salt.size()),BCRYPT_USE_SYSTEM_PREFERRED_RNG)!=0){error_=L"Zufallsquelle für Konto konnte nicht verwendet werden.";return false;}const std::wstring saltHex=Hex(salt);std::wstring digest;if(!PasswordDigest(password,saltHex,digest)){error_=L"Passwort konnte nicht abgeleitet werden.";return false;}sqlite3_stmt* statement=nullptr;const char* sql="INSERT INTO accounts(employee_id,username,password_salt,password_digest,enabled,failed_attempts,locked_until,must_change_password) SELECT ?,?,?,?,1,0,0,? WHERE EXISTS(SELECT 1 FROM employees WHERE id=? AND active=1) ON CONFLICT(employee_id) DO UPDATE SET username=excluded.username,password_salt=excluded.password_salt,password_digest=excluded.password_digest,enabled=1,failed_attempts=0,locked_until=0,must_change_password=excluded.must_change_password,updated_at=CURRENT_TIMESTAMP";bool ok=sqlite3_prepare_v2(db_,sql,-1,&statement,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(statement,1,employeeId);BindText(statement,2,username);BindText(statement,3,saltHex);BindText(statement,4,digest);sqlite3_bind_int(statement,5,mustChangePassword?1:0);sqlite3_bind_int64(statement,6,employeeId);ok=sqlite3_step(statement)==SQLITE_DONE;}if(statement)sqlite3_finalize(statement);if(ok)ok=audit(actor,L"account.credentials.reset",username);if(!ok)setError("Konto konnte nicht gespeichert werden");return ok;
}
bool EnterpriseDatabase::authenticate(const std::wstring& username,const std::wstring& password,int64_t nowUtc,EnterpriseAccount& account){account={};if(username.empty()||password.empty()||nowUtc<=0){error_=L"Ungültige Anmeldeanforderung.";return false;}sqlite3_stmt* statement=nullptr;bool found=sqlite3_prepare_v2(db_,"SELECT employee_id,username,password_salt,password_digest,enabled,failed_attempts,locked_until,must_change_password FROM accounts WHERE username=?",-1,&statement,nullptr)==SQLITE_OK;if(found){BindText(statement,1,username);found=sqlite3_step(statement)==SQLITE_ROW;}if(!found){if(statement)sqlite3_finalize(statement);error_=L"Benutzername oder Passwort ist ungültig.";return false;}const int64_t id=sqlite3_column_int64(statement,0);const std::wstring storedUsername=Wide(sqlite3_column_text(statement,1)),salt=Wide(sqlite3_column_text(statement,2)),expected=Wide(sqlite3_column_text(statement,3));const bool enabled=sqlite3_column_int(statement,4)!=0;const int failures=sqlite3_column_int(statement,5),locked=sqlite3_column_int64(statement,6);const bool mustChange=sqlite3_column_int(statement,7)!=0;sqlite3_finalize(statement);std::wstring actual;const bool valid=enabled&&locked<=nowUtc&&PasswordDigest(password,salt,actual)&&ConstantTimeEquals(expected,actual);sqlite3_stmt* update=nullptr;if(valid){found=sqlite3_prepare_v2(db_,"UPDATE accounts SET failed_attempts=0,locked_until=0 WHERE employee_id=?",-1,&update,nullptr)==SQLITE_OK;if(found){sqlite3_bind_int64(update,1,id);found=sqlite3_step(update)==SQLITE_DONE;}if(update)sqlite3_finalize(update);if(!found){setError("Anmeldestatus konnte nicht aktualisiert werden");return false;}account={id,storedUsername,true,0,0,mustChange};audit(storedUsername,L"account.authenticated",storedUsername);return true;}const int next=failures+1;const int64_t newLocked=next>=5?nowUtc+900:locked;found=sqlite3_prepare_v2(db_,"UPDATE accounts SET failed_attempts=?,locked_until=? WHERE employee_id=?",-1,&update,nullptr)==SQLITE_OK;if(found){sqlite3_bind_int(update,1,next);sqlite3_bind_int64(update,2,newLocked);sqlite3_bind_int64(update,3,id);sqlite3_step(update);}if(update)sqlite3_finalize(update);error_=!enabled?L"Das Konto ist deaktiviert.":locked>nowUtc?L"Das Konto ist vorübergehend gesperrt.":next>=5?L"Zu viele Fehlversuche. Konto 15 Minuten gesperrt.":L"Benutzername oder Passwort ist ungültig.";return false;}
bool EnterpriseDatabase::changePassword(int64_t employeeId,const std::wstring& oldPassword,const std::wstring& newPassword,const std::wstring& actor){if(employeeId<=0||newPassword.size()<12){error_=L"Das neue Passwort muss mindestens 12 Zeichen enthalten.";return false;}sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"SELECT username,password_salt,password_digest FROM accounts WHERE employee_id=? AND enabled=1",-1,&statement,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(statement,1,employeeId);ok=sqlite3_step(statement)==SQLITE_ROW;}if(!ok){if(statement)sqlite3_finalize(statement);error_=L"Aktives Konto nicht gefunden.";return false;}const std::wstring username=Wide(sqlite3_column_text(statement,0)),oldDigest=Wide(sqlite3_column_text(statement,2)),oldSalt=Wide(sqlite3_column_text(statement,1));sqlite3_finalize(statement);std::wstring candidate;if(!PasswordDigest(oldPassword,oldSalt,candidate)||!ConstantTimeEquals(oldDigest,candidate)){error_=L"Aktuelles Passwort ist ungültig.";return false;}return createOrResetAccount(employeeId,username,newPassword,false,actor);}
std::vector<EnterpriseAccount> EnterpriseDatabase::accounts(bool includeDisabled){std::vector<EnterpriseAccount> result;sqlite3_stmt* statement=nullptr;const char* sql=includeDisabled?"SELECT employee_id,username,enabled,failed_attempts,locked_until,must_change_password FROM accounts ORDER BY username":"SELECT employee_id,username,enabled,failed_attempts,locked_until,must_change_password FROM accounts WHERE enabled=1 ORDER BY username";if(sqlite3_prepare_v2(db_,sql,-1,&statement,nullptr)!=SQLITE_OK){setError("Konten konnten nicht geladen werden");return result;}while(sqlite3_step(statement)==SQLITE_ROW)result.push_back({sqlite3_column_int64(statement,0),Wide(sqlite3_column_text(statement,1)),sqlite3_column_int(statement,2)!=0,sqlite3_column_int(statement,3),sqlite3_column_int64(statement,4),sqlite3_column_int(statement,5)!=0});sqlite3_finalize(statement);return result;}
bool EnterpriseDatabase::setAccountEnabled(int64_t employeeId,bool enabled,const std::wstring& actor){if(employeeId<=0){error_=L"Ungültige Konto-ID.";return false;}sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"UPDATE accounts SET enabled=?,failed_attempts=0,locked_until=0,updated_at=CURRENT_TIMESTAMP WHERE employee_id=?",-1,&statement,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int(statement,1,enabled?1:0);sqlite3_bind_int64(statement,2,employeeId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);if(ok)ok=audit(actor,enabled?L"account.enabled":L"account.disabled",std::to_wstring(employeeId));if(!ok)setError("Kontostatus konnte nicht geändert werden");return ok;}

bool EnterpriseDatabase::createTaskApproval(int64_t taskId, int64_t requestedBy, int64_t approverId, int64_t& approvalId, const std::wstring& actor) {
    if (taskId <= 0 || requestedBy <= 0 || approverId <= 0 || requestedBy == approverId) { error_ = L"Antragsteller und freigebende Person müssen unterschiedlich und gültig sein."; return false; }
    sqlite3_stmt* statement = nullptr; const char* sql = "INSERT INTO task_approvals(task_id,requested_by,approver_id) SELECT ?,?,? WHERE EXISTS(SELECT 1 FROM tasks WHERE id=? AND active=1) AND EXISTS(SELECT 1 FROM employees WHERE id=? AND active=1) AND EXISTS(SELECT 1 FROM employees WHERE id=? AND active=1)";
    bool ok = sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, taskId); sqlite3_bind_int64(statement, 2, requestedBy); sqlite3_bind_int64(statement, 3, approverId); sqlite3_bind_int64(statement, 4, taskId); sqlite3_bind_int64(statement, 5, requestedBy); sqlite3_bind_int64(statement, 6, approverId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement); if (ok) { approvalId = sqlite3_last_insert_rowid(db_); ok = audit(actor,L"task.approval.requested",std::to_wstring(taskId)); } if (!ok) setError("Genehmigung konnte nicht angelegt werden"); return ok;
}

bool EnterpriseDatabase::decideTaskApproval(int64_t approvalId, bool approved, int64_t decidedBy, const std::wstring& actor) {
    sqlite3_stmt* statement = nullptr; bool ok = sqlite3_prepare_v2(db_, "UPDATE task_approvals SET status=?,decided_by=?,decided_at=CURRENT_TIMESTAMP WHERE id=? AND approver_id=? AND status='pending'", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, approved ? L"approved" : L"rejected"); sqlite3_bind_int64(statement, 2, decidedBy); sqlite3_bind_int64(statement, 3, approvalId); sqlite3_bind_int64(statement, 4, decidedBy); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement); if (ok) ok = audit(actor,approved ? L"task.approval.approved" : L"task.approval.rejected",std::to_wstring(approvalId)); if (!ok) setError("Genehmigung konnte nicht entschieden werden"); return ok;
}

bool EnterpriseDatabase::applySyncedTaskApproval(const std::wstring& externalId,const std::wstring& taskExternalId,const std::wstring& requesterExternalId,const std::wstring& approverExternalId,const std::wstring& decidedByExternalId,const EnterpriseTaskApproval& approval,const std::wstring& actor){
    const int64_t taskId=syncedLocalId(L"task",taskExternalId),requester=syncedLocalId(L"employee",requesterExternalId),approver=syncedLocalId(L"employee",approverExternalId),decider=decidedByExternalId.empty()?0:syncedLocalId(L"employee",decidedByExternalId);if(externalId.empty()||taskId<=0||requester<=0||approver<=0||requester==approver||(approval.status!=L"pending"&&approval.status!=L"approved"&&approval.status!=L"rejected")||((approval.status==L"approved"||approval.status==L"rejected")&&decider<=0)){error_=L"Ungültige zentrale Freigabe.";return false;}sqlite3_stmt* s=nullptr;int64_t local=0;bool ok=sqlite3_prepare_v2(db_,"SELECT local_id FROM sync_object_links WHERE object_type='task_approval' AND external_id=?",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,externalId);if(sqlite3_step(s)==SQLITE_ROW)local=sqlite3_column_int64(s,0);}if(s)sqlite3_finalize(s);s=nullptr;if(ok&&local==0){ok=sqlite3_prepare_v2(db_,"INSERT INTO task_approvals(task_id,requested_by,approver_id,status,decided_by) VALUES(?,?,?,?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(s,1,taskId);sqlite3_bind_int64(s,2,requester);sqlite3_bind_int64(s,3,approver);BindText(s,4,approval.status);sqlite3_bind_int64(s,5,decider);ok=sqlite3_step(s)==SQLITE_DONE;}if(s)sqlite3_finalize(s);if(ok){local=sqlite3_last_insert_rowid(db_);s=nullptr;ok=sqlite3_prepare_v2(db_,"INSERT INTO sync_object_links(object_type,external_id,local_id) VALUES('task_approval',?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,externalId);sqlite3_bind_int64(s,2,local);ok=sqlite3_step(s)==SQLITE_DONE;}}}else if(ok){ok=sqlite3_prepare_v2(db_,"UPDATE task_approvals SET task_id=?,requested_by=?,approver_id=?,status=?,decided_by=? WHERE id=?",-1,&s,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(s,1,taskId);sqlite3_bind_int64(s,2,requester);sqlite3_bind_int64(s,3,approver);BindText(s,4,approval.status);sqlite3_bind_int64(s,5,decider);sqlite3_bind_int64(s,6,local);ok=sqlite3_step(s)==SQLITE_DONE;}}if(s)sqlite3_finalize(s);if(ok)ok=audit(actor,L"sync.task_approval.applied",externalId);if(!ok)setError("Zentrale Freigabe konnte nicht übernommen werden");return ok;
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
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "INSERT INTO tickets(title,category,priority,status,assignee_id) VALUES(?,?,?,?,?)", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, ticket.title); BindText(statement, 2, ticket.category); BindText(statement, 3, ticket.priority); BindText(statement, 4, ticket.status.empty() ? L"open" : ticket.status); sqlite3_bind_int64(statement, 5, ticket.assigneeId); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement);
    if (ok) { newId = sqlite3_last_insert_rowid(db_); ok = audit(actor, L"ticket.created", ticket.title); }
    if (!ok) setError("Ticket konnte nicht angelegt werden"); return ok;
}

bool EnterpriseDatabase::updateTicket(const EnterpriseTicket& ticket, const std::wstring& actor) {
    if (ticket.id <= 0 || ticket.title.empty() || ticket.category.empty() || (ticket.priority != L"Niedrig" && ticket.priority != L"Mittel" && ticket.priority != L"Hoch" && ticket.priority != L"Kritisch")) { error_ = L"Ungültige Ticketdaten."; return false; }
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE tickets SET title=?,category=?,priority=?,assignee_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, ticket.title); BindText(statement, 2, ticket.category); BindText(statement, 3, ticket.priority); sqlite3_bind_int64(statement, 4, ticket.assigneeId); sqlite3_bind_int64(statement, 5, ticket.id); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"ticket.updated", std::to_wstring(ticket.id));
    if (!ok) setError("Ticket konnte nicht bearbeitet werden"); return ok;
}

bool EnterpriseDatabase::updateTicketStatus(int64_t ticketId, const std::wstring& status, const std::wstring& actor) {
    if (status != L"open" && status != L"in_progress" && status != L"resolved" && status != L"closed") { error_ = L"Ungültiger Ticketstatus."; return false; }
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE tickets SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, status); sqlite3_bind_int64(statement, 2, ticketId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement); if (ok) ok = audit(actor, L"ticket.status.changed", std::to_wstring(ticketId) + L":" + status); if (!ok) setError("Ticketstatus konnte nicht geändert werden"); return ok;
}

bool EnterpriseDatabase::archiveTicket(int64_t ticketId, const std::wstring& actor) {
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE tickets SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, ticketId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement); if (ok) ok = audit(actor, L"ticket.archived", std::to_wstring(ticketId)); if (!ok) setError("Ticket konnte nicht archiviert werden"); return ok;
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
    if (!ok) setError("Kommentar konnte nicht gespeichert werden");
    return ok;
}

bool EnterpriseDatabase::applySyncedComment(const std::wstring& externalId,const std::wstring& subjectExternalId,const std::wstring& authorExternalId,const EnterpriseComment& comment,const std::wstring& actor){
    if(externalId.empty()||authorExternalId.empty()||comment.body.empty()||(comment.subjectType!=L"task"&&comment.subjectType!=L"ticket")){error_=L"Ungültiger zentraler Kommentar.";return false;}
    const int64_t subjectId=syncedLocalId(comment.subjectType,subjectExternalId),authorId=syncedLocalId(L"employee",authorExternalId);if(subjectId<=0||authorId<=0){error_=L"Kommentarbezug ist noch nicht synchronisiert.";return false;}
    if(!execute("BEGIN IMMEDIATE"))return false;sqlite3_stmt* s=nullptr;int64_t local=0;bool ok=sqlite3_prepare_v2(db_,"SELECT local_id FROM sync_object_links WHERE object_type='comment' AND external_id=?",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,externalId);if(sqlite3_step(s)==SQLITE_ROW)local=sqlite3_column_int64(s,0);}if(s)sqlite3_finalize(s);s=nullptr;
    if(ok&&local==0){ok=sqlite3_prepare_v2(db_,"INSERT INTO comments(subject_type,subject_id,author_id,body) VALUES(?,?,?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,comment.subjectType);sqlite3_bind_int64(s,2,subjectId);sqlite3_bind_int64(s,3,authorId);BindText(s,4,comment.body);ok=sqlite3_step(s)==SQLITE_DONE;}if(s)sqlite3_finalize(s);s=nullptr;if(ok){local=sqlite3_last_insert_rowid(db_);ok=sqlite3_prepare_v2(db_,"INSERT INTO sync_object_links(object_type,external_id,local_id) VALUES('comment',?,?)",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,externalId);sqlite3_bind_int64(s,2,local);ok=sqlite3_step(s)==SQLITE_DONE;}}}
    else if(ok){ok=sqlite3_prepare_v2(db_,"UPDATE comments SET subject_type=?,subject_id=?,author_id=?,body=? WHERE id=?",-1,&s,nullptr)==SQLITE_OK;if(ok){BindText(s,1,comment.subjectType);sqlite3_bind_int64(s,2,subjectId);sqlite3_bind_int64(s,3,authorId);BindText(s,4,comment.body);sqlite3_bind_int64(s,5,local);ok=sqlite3_step(s)==SQLITE_DONE;}}
    if(s)sqlite3_finalize(s);if(ok)ok=audit(actor,L"sync.comment.applied",externalId);execute(ok?"COMMIT":"ROLLBACK");if(!ok)setError("Zentraler Kommentar konnte nicht übernommen werden");return ok;
}

std::vector<EnterpriseFacilityAsset> EnterpriseDatabase::facilityAssets(bool includeArchived) {
    std::vector<EnterpriseFacilityAsset> result; sqlite3_stmt* statement = nullptr;
    const char* sql = includeArchived ? "SELECT id,name,category,location,status,active FROM facility_assets ORDER BY active DESC,name" : "SELECT id,name,category,location,status,active FROM facility_assets WHERE active=1 ORDER BY name";
    if(sqlite3_prepare_v2(db_,sql,-1,&statement,nullptr)!=SQLITE_OK){setError("Facility-Assets konnten nicht geladen werden");return result;}while(sqlite3_step(statement)==SQLITE_ROW)result.push_back({sqlite3_column_int64(statement,0),Wide(sqlite3_column_text(statement,1)),Wide(sqlite3_column_text(statement,2)),Wide(sqlite3_column_text(statement,3)),Wide(sqlite3_column_text(statement,4)),sqlite3_column_int(statement,5)!=0});sqlite3_finalize(statement);return result;
}
bool EnterpriseDatabase::createFacilityAsset(const EnterpriseFacilityAsset& asset,int64_t& newId,const std::wstring& actor){if(asset.name.empty()||asset.category.empty()||asset.location.empty()||(asset.status!=L"operational"&&asset.status!=L"maintenance"&&asset.status!=L"out_of_service")){error_=L"Ungültige Asset-Daten.";return false;}sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"INSERT INTO facility_assets(name,category,location,status) VALUES(?,?,?,?)",-1,&statement,nullptr)==SQLITE_OK;if(ok){BindText(statement,1,asset.name);BindText(statement,2,asset.category);BindText(statement,3,asset.location);BindText(statement,4,asset.status);ok=sqlite3_step(statement)==SQLITE_DONE;}if(statement)sqlite3_finalize(statement);if(ok){newId=sqlite3_last_insert_rowid(db_);ok=audit(actor,L"facility_asset.created",asset.name);}if(!ok)setError("Facility-Asset konnte nicht angelegt werden");return ok;}
bool EnterpriseDatabase::updateFacilityAsset(const EnterpriseFacilityAsset& asset,const std::wstring& actor){if(asset.id<=0||asset.name.empty()||asset.category.empty()||asset.location.empty()||(asset.status!=L"operational"&&asset.status!=L"maintenance"&&asset.status!=L"out_of_service")){error_=L"Ungültige Asset-Daten.";return false;}sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"UPDATE facility_assets SET name=?,category=?,location=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1",-1,&statement,nullptr)==SQLITE_OK;if(ok){BindText(statement,1,asset.name);BindText(statement,2,asset.category);BindText(statement,3,asset.location);BindText(statement,4,asset.status);sqlite3_bind_int64(statement,5,asset.id);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);if(ok)ok=audit(actor,L"facility_asset.updated",std::to_wstring(asset.id));if(!ok)setError("Facility-Asset konnte nicht bearbeitet werden");return ok;}
bool EnterpriseDatabase::archiveFacilityAsset(int64_t assetId,const std::wstring& actor){sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"UPDATE facility_assets SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1",-1,&statement,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(statement,1,assetId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);if(ok)ok=audit(actor,L"facility_asset.archived",std::to_wstring(assetId));if(!ok)setError("Facility-Asset konnte nicht archiviert werden");return ok;}

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
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "INSERT INTO maintenance_orders(asset_id,title,status,due_at,recurrence_days) SELECT ?,?,?,?,? WHERE EXISTS(SELECT 1 FROM facility_assets WHERE id=? AND active=1)", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement,1,order.assetId); BindText(statement,2,order.title); BindText(statement,3,order.status.empty()?L"open":order.status); sqlite3_bind_int64(statement,4,order.dueAt); sqlite3_bind_int(statement,5,order.recurrenceDays); sqlite3_bind_int64(statement,6,order.assetId); ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) { newId=sqlite3_last_insert_rowid(db_); ok=audit(actor,L"maintenance_order.created",std::to_wstring(newId)); }
    if (!ok) setError("Wartungsauftrag konnte nicht angelegt werden"); return ok;
}

bool EnterpriseDatabase::updateMaintenanceOrder(const EnterpriseMaintenanceOrder& order, const std::wstring& actor) {
    if (order.id <= 0 || order.assetId <= 0 || order.title.empty() || order.dueAt < 0 || order.recurrenceDays < 0 || order.recurrenceDays > 3660 || (order.status != L"open" && order.status != L"in_progress" && order.status != L"completed" && order.status != L"cancelled")) { error_ = L"Ungültige Wartungsdaten."; return false; }
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE maintenance_orders SET asset_id=?,title=?,status=?,due_at=?,recurrence_days=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1 AND EXISTS(SELECT 1 FROM facility_assets WHERE id=? AND active=1)", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement,1,order.assetId); BindText(statement,2,order.title); BindText(statement,3,order.status); sqlite3_bind_int64(statement,4,order.dueAt); sqlite3_bind_int(statement,5,order.recurrenceDays); sqlite3_bind_int64(statement,6,order.id); sqlite3_bind_int64(statement,7,order.assetId); ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok=audit(actor,L"maintenance_order.updated",std::to_wstring(order.id));
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
        if(ok && source.recurrenceDays>0){const int64_t nextDue=source.dueAt>0?source.dueAt+static_cast<int64_t>(source.recurrenceDays)*86400:static_cast<int64_t>(std::time(nullptr))+static_cast<int64_t>(source.recurrenceDays)*86400;statement=nullptr;ok=sqlite3_prepare_v2(db_,"INSERT INTO maintenance_orders(asset_id,title,status,due_at,recurrence_days) VALUES(?,?,'open',?,?)",-1,&statement,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(statement,1,source.assetId);BindText(statement,2,source.title);sqlite3_bind_int64(statement,3,nextDue);sqlite3_bind_int(statement,4,source.recurrenceDays);ok=sqlite3_step(statement)==SQLITE_DONE;}if(statement)sqlite3_finalize(statement);if(ok)repeatedOrderId=sqlite3_last_insert_rowid(db_);}
    }
    if(ok)ok=audit(actor,L"maintenance_order.status.changed",std::to_wstring(orderId)+L":"+status);if(ok&&repeatedOrderId>0)ok=audit(actor,L"maintenance_order.recurrence.created",std::to_wstring(orderId)+L"->"+std::to_wstring(repeatedOrderId));execute(ok?"COMMIT":"ROLLBACK");if(!ok)setError("Wartungsstatus konnte nicht geändert werden");return ok;
}

bool EnterpriseDatabase::archiveMaintenanceOrder(int64_t orderId, const std::wstring& actor) {
    sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"UPDATE maintenance_orders SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_int64(statement,1,orderId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);
    if(ok)ok=audit(actor,L"maintenance_order.archived",std::to_wstring(orderId));if(!ok)setError("Wartungsauftrag konnte nicht archiviert werden");return ok;
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

bool EnterpriseDatabase::createEmployee(const EnterpriseEmployee& employee, int64_t& newId, const std::wstring& actor) {
    if (employee.name.empty() || employee.role.empty() || employee.department.empty()) { error_ = L"Name, Rolle und Bereich sind Pflichtangaben."; return false; }
    sqlite3_stmt* statement = nullptr; bool ok = sqlite3_prepare_v2(db_, "INSERT INTO employees(name,role,department,presence) VALUES(?,?,?,?)", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, employee.name); BindText(statement, 2, employee.role); BindText(statement, 3, employee.department); BindText(statement, 4, employee.presence.empty() ? L"office" : employee.presence); ok = sqlite3_step(statement) == SQLITE_DONE; }
    if (statement) sqlite3_finalize(statement); if (ok) { newId = sqlite3_last_insert_rowid(db_); ok = audit(actor, L"employee.created", employee.name); } if (!ok) setError("Mitarbeiter konnte nicht angelegt werden"); return ok;
}

bool EnterpriseDatabase::updateEmployee(const EnterpriseEmployee& employee, const std::wstring& actor) {
    if (employee.id <= 0 || employee.name.empty() || employee.role.empty() || employee.department.empty()) { error_ = L"Name, Rolle und Bereich sind Pflichtangaben."; return false; }
    sqlite3_stmt* statement = nullptr;
    bool ok = sqlite3_prepare_v2(db_, "UPDATE employees SET name=?,role=?,department=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, employee.name); BindText(statement, 2, employee.role); BindText(statement, 3, employee.department); sqlite3_bind_int64(statement, 4, employee.id); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement);
    if (ok) ok = audit(actor, L"employee.updated", std::to_wstring(employee.id));
    if (!ok) setError("Mitarbeiter konnte nicht bearbeitet werden"); return ok;
}

bool EnterpriseDatabase::updatePresence(int64_t employeeId, const std::wstring& presence, const std::wstring& actor) {
    if (presence != L"office" && presence != L"remote" && presence != L"meeting" && presence != L"away") { error_ = L"Ungültiger Präsenzstatus."; return false; }
    sqlite3_stmt* statement = nullptr; bool ok = sqlite3_prepare_v2(db_, "UPDATE employees SET presence=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { BindText(statement, 1, presence); sqlite3_bind_int64(statement, 2, employeeId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement); if (ok) ok = audit(actor, L"employee.presence.changed", std::to_wstring(employeeId) + L":" + presence); if (!ok) setError("Präsenz konnte nicht geändert werden"); return ok;
}

bool EnterpriseDatabase::archiveEmployee(int64_t employeeId, const std::wstring& actor) {
    sqlite3_stmt* statement = nullptr; bool ok = sqlite3_prepare_v2(db_, "UPDATE employees SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement, 1, employeeId); ok = sqlite3_step(statement) == SQLITE_DONE && sqlite3_changes(db_) == 1; }
    if (statement) sqlite3_finalize(statement); if (ok) ok = audit(actor, L"employee.archived", std::to_wstring(employeeId)); if (!ok) setError("Mitarbeiter konnte nicht archiviert werden"); return ok;
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
    if (request.employeeId <= 0 || request.endsAt <= request.startsAt || request.reason.empty()) { error_ = L"Ungültiger Urlaubsantrag."; return false; }
    sqlite3_stmt* statement = nullptr; const char* sql = "INSERT INTO leave_requests(employee_id,starts_at,ends_at,reason) SELECT ?,?,?,? WHERE EXISTS(SELECT 1 FROM employees WHERE id=? AND active=1)";
    bool ok = sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) == SQLITE_OK;
    if (ok) { sqlite3_bind_int64(statement,1,request.employeeId);sqlite3_bind_int64(statement,2,request.startsAt);sqlite3_bind_int64(statement,3,request.endsAt);BindText(statement,4,request.reason);sqlite3_bind_int64(statement,5,request.employeeId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1; }
    if(statement)sqlite3_finalize(statement);if(ok){newId=sqlite3_last_insert_rowid(db_);ok=audit(actor,L"leave_request.created",std::to_wstring(newId));}if(!ok)setError("Urlaubsantrag konnte nicht angelegt werden");return ok;
}

bool EnterpriseDatabase::decideLeaveRequest(int64_t requestId, bool approved, int64_t decidedBy, const std::wstring& actor) {
    if(requestId<=0||decidedBy<=0){error_=L"Ungültige Urlaubsfreigabe.";return false;}sqlite3_stmt* statement=nullptr;const char* sql="UPDATE leave_requests SET status=?,decided_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1 AND status='pending' AND employee_id<>?";bool ok=sqlite3_prepare_v2(db_,sql,-1,&statement,nullptr)==SQLITE_OK;if(ok){BindText(statement,1,approved?L"approved":L"rejected");sqlite3_bind_int64(statement,2,decidedBy);sqlite3_bind_int64(statement,3,requestId);sqlite3_bind_int64(statement,4,decidedBy);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);if(ok)ok=audit(actor,approved?L"leave_request.approved":L"leave_request.rejected",std::to_wstring(requestId));if(!ok)setError("Urlaubsantrag konnte nicht entschieden werden");return ok;
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
    sqlite3_stmt* statement=nullptr; bool ok=sqlite3_prepare_v2(db_,"INSERT INTO organization_units(name,code,manager_id) VALUES(?,?,?)",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){BindText(statement,1,unit.name);BindText(statement,2,unit.code);sqlite3_bind_int64(statement,3,unit.managerId);ok=sqlite3_step(statement)==SQLITE_DONE;} if(statement)sqlite3_finalize(statement); if(ok){newId=sqlite3_last_insert_rowid(db_);ok=audit(actor,L"organization_unit.created",unit.code);} if(!ok)setError("Organisationseinheit konnte nicht angelegt werden");return ok;
}

bool EnterpriseDatabase::updateOrganizationUnit(const EnterpriseOrganizationUnit& unit, const std::wstring& actor) {
    if(unit.id<=0||unit.name.empty()||unit.code.empty()){error_=L"Ungültige Organisationseinheit.";return false;}
    sqlite3_stmt* statement=nullptr; bool ok=sqlite3_prepare_v2(db_,"UPDATE organization_units SET name=?,code=?,manager_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){BindText(statement,1,unit.name);BindText(statement,2,unit.code);sqlite3_bind_int64(statement,3,unit.managerId);sqlite3_bind_int64(statement,4,unit.id);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);if(ok)ok=audit(actor,L"organization_unit.updated",std::to_wstring(unit.id));if(!ok)setError("Organisationseinheit konnte nicht bearbeitet werden");return ok;
}

bool EnterpriseDatabase::archiveOrganizationUnit(int64_t unitId, const std::wstring& actor) {
    sqlite3_stmt* statement=nullptr;bool ok=sqlite3_prepare_v2(db_,"UPDATE organization_units SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1",-1,&statement,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_int64(statement,1,unitId);ok=sqlite3_step(statement)==SQLITE_DONE&&sqlite3_changes(db_)==1;}if(statement)sqlite3_finalize(statement);if(ok)ok=audit(actor,L"organization_unit.archived",std::to_wstring(unitId));if(!ok)setError("Organisationseinheit konnte nicht archiviert werden");return ok;
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
    const char* sql = "SELECT 1 FROM employee_roles er JOIN role_permissions rp ON rp.role_id=er.role_id WHERE er.employee_id=? AND (rp.permission='*' OR rp.permission=?) LIMIT 1";
    if (sqlite3_prepare_v2(db_, sql, -1, &statement, nullptr) != SQLITE_OK) { setError("Berechtigung konnte nicht geprüft werden"); return false; }
    sqlite3_bind_int64(statement, 1, employeeId); BindText(statement, 2, permission); const bool allowed = sqlite3_step(statement) == SQLITE_ROW; sqlite3_finalize(statement); return allowed;
}

bool EnterpriseDatabase::backupTo(const std::wstring& destinationPath, const std::wstring& actor) {
    sqlite3* destination = nullptr;
    const std::string path = Utf8(destinationPath);
    if (sqlite3_open_v2(path.c_str(), &destination, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nullptr) != SQLITE_OK) { if (destination) sqlite3_close(destination); error_ = L"Backup-Zieldatei konnte nicht geöffnet werden."; return false; }
    sqlite3_backup* backup = sqlite3_backup_init(destination, "main", db_, "main");
    bool ok = backup != nullptr;
    if (ok) { sqlite3_backup_step(backup, -1); ok = sqlite3_backup_finish(backup) == SQLITE_OK; }
    sqlite3_close(destination);
    if (ok) ok = audit(actor, L"database.backup.created", destinationPath);
    if (!ok) setError("Datenbank-Backup konnte nicht erstellt werden"); return ok;
}

bool EnterpriseDatabase::restoreFrom(const std::wstring& sourcePath, const std::wstring& actor) {
    sqlite3* source = nullptr;
    const std::string path = Utf8(sourcePath);
    if (sqlite3_open_v2(path.c_str(), &source, SQLITE_OPEN_READONLY, nullptr) != SQLITE_OK) { if (source) sqlite3_close(source); error_ = L"Backup-Quelldatei konnte nicht geöffnet werden."; return false; }
    sqlite3_backup* restore = sqlite3_backup_init(db_, "main", source, "main");
    bool ok = restore != nullptr;
    if (ok) { sqlite3_backup_step(restore, -1); ok = sqlite3_backup_finish(restore) == SQLITE_OK; }
    sqlite3_close(source);
    if (ok) ok = audit(actor, L"database.restore.completed", sourcePath);
    if (!ok) setError("Datenbank konnte nicht wiederhergestellt werden"); return ok;
}

bool EnterpriseDatabase::exportRoomsCsv(const std::wstring& destinationPath, const std::wstring& actor) {
    std::ofstream output(Utf8(destinationPath), std::ios::trunc | std::ios::binary);
    if (!output) { error_ = L"Exportdatei konnte nicht erstellt werden."; return false; }
    auto quote = [](const std::wstring& value) { std::string text = Utf8(value); size_t pos = 0; while ((pos = text.find('"', pos)) != std::string::npos) { text.insert(pos, 1, '"'); pos += 2; } return std::string("\"") + text + "\""; };
    output << "id,name,location,capacity,equipment,active\n";
    for (const EnterpriseRoom& room : rooms(true)) output << room.id << ',' << quote(room.name) << ',' << quote(room.location) << ',' << room.capacity << ',' << quote(room.equipment) << ',' << (room.active ? 1 : 0) << "\n";
    const bool ok = static_cast<bool>(output);
    if (ok) audit(actor, L"rooms.exported.csv", destinationPath); else error_ = L"Raumexport konnte nicht geschrieben werden.";
    return ok;
}

bool EnterpriseDatabase::importRoomsCsv(const std::wstring& sourcePath, int& importedCount, const std::wstring& actor) {
    importedCount = 0; std::ifstream input(Utf8(sourcePath), std::ios::binary);
    if (!input) { error_ = L"Importdatei konnte nicht geöffnet werden."; return false; }
    std::string line; std::getline(input, line);
    if (line != "id,name,location,capacity,equipment,active") { error_ = L"Ungültiger CSV-Header."; return false; }
    if (!execute("BEGIN IMMEDIATE")) return false;
    while (std::getline(input, line)) {
        std::vector<std::string> fields; std::string field; bool quoted = false;
        for (size_t i = 0; i < line.size(); ++i) { char c = line[i]; if (c == '"') { if (quoted && i + 1 < line.size() && line[i + 1] == '"') { field += c; ++i; } else quoted = !quoted; } else if (c == ',' && !quoted) { fields.push_back(field); field.clear(); } else field += c; } fields.push_back(field);
        if (fields.size() != 6) { execute("ROLLBACK"); error_ = L"Ungültige CSV-Zeile."; return false; }
        EnterpriseRoom room; room.name = Wide(reinterpret_cast<const unsigned char*>(fields[1].c_str())); room.location = Wide(reinterpret_cast<const unsigned char*>(fields[2].c_str())); room.equipment = Wide(reinterpret_cast<const unsigned char*>(fields[4].c_str()));
        try { room.capacity = std::stoi(fields[3]); } catch (...) { execute("ROLLBACK"); error_ = L"Ungültige Raumkapazität."; return false; }
        sqlite3_stmt* statement = nullptr; bool ok = sqlite3_prepare_v2(db_, "INSERT INTO rooms(name,location,capacity,equipment,active) VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET location=excluded.location,capacity=excluded.capacity,equipment=excluded.equipment,active=excluded.active,updated_at=CURRENT_TIMESTAMP", -1, &statement, nullptr) == SQLITE_OK;
        if (ok) { BindText(statement, 1, room.name); BindText(statement, 2, room.location); sqlite3_bind_int(statement, 3, room.capacity); BindText(statement, 4, room.equipment); sqlite3_bind_int(statement, 5, fields[5] == "1" ? 1 : 0); ok = sqlite3_step(statement) == SQLITE_DONE; }
        if (statement) sqlite3_finalize(statement); if (!ok) { execute("ROLLBACK"); setError("Raumimport fehlgeschlagen"); return false; } ++importedCount;
    }
    const bool committed = execute("COMMIT"); if (committed) audit(actor, L"rooms.imported.csv", sourcePath + L":" + std::to_wstring(importedCount)); return committed;
}

std::vector<std::wstring> EnterpriseDatabase::auditLog(int limit) {
    std::vector<std::wstring> result; sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db_, "SELECT occurred_at,actor,action,subject FROM audit_log ORDER BY id DESC LIMIT ?", -1, &statement, nullptr) != SQLITE_OK) return result;
    sqlite3_bind_int(statement, 1, limit);
    while (sqlite3_step(statement) == SQLITE_ROW) result.push_back(Wide(sqlite3_column_text(statement, 0)) + L" | " + Wide(sqlite3_column_text(statement, 1)) + L" | " + Wide(sqlite3_column_text(statement, 2)) + L" | " + Wide(sqlite3_column_text(statement, 3)));
    sqlite3_finalize(statement); return result;
}
