// Linux binding to the shared Windows enterprise database. Only authenticated
// sessions may read or mutate data, and write rights are checked per operation.
#include "../../core/enterprise_database.h"
#include "../../contracts/aura_ffi.h"
#include <codecvt>
#include <ctime>
#include <cstring>
#include <fcntl.h>
#include <locale>
#include <memory>
#include <unordered_map>
#include <unordered_set>
#include <sstream>
#include <string>
#include <sys/stat.h>
#include <sys/file.h>
#include <sqlite3.h>
#include <unistd.h>

namespace {
std::wstring widen(const char* s) { return std::wstring_convert<std::codecvt_utf8<wchar_t>>{}.from_bytes(s ? s : ""); }
std::string utf8(const std::wstring& s) { return std::wstring_convert<std::codecvt_utf8<wchar_t>>{}.to_bytes(s); }
std::string quote(const std::wstring& value) {
    const std::string s=utf8(value); std::ostringstream out; out << '"';
    for (unsigned char c:s) {
        switch(c) { case '"': out << "\\\""; break; case '\\': out << "\\\\"; break;
            case '\n': out << "\\n"; break; case '\r': out << "\\r"; break; case '\t': out << "\\t"; break;
            default: if(c<32) { const char* h="0123456789abcdef"; out << "\\u00" << h[c>>4] << h[c&15]; } else out << c; }
    }
    return out.str()+'"';
}
bool read_restore_generation(int fd,uint64_t& generation) {
    generation=0;const ssize_t count=pread(fd,&generation,sizeof(generation),0);
    return count==0 || count==static_cast<ssize_t>(sizeof(generation));
}
// Linux-only schema: Windows installations do not acquire outbox triggers.
bool ensure_linux_room_sync_schema(const char* path) {
    sqlite3* db=nullptr;if(sqlite3_open_v2(path,&db,SQLITE_OPEN_READWRITE|SQLITE_OPEN_FULLMUTEX,nullptr)!=SQLITE_OK){if(db)sqlite3_close(db);return false;}
    sqlite3_busy_timeout(db,5000);
    const char* sql=R"SQL(
        CREATE TABLE IF NOT EXISTS linux_room_sync_config (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), client_id TEXT NOT NULL, server_url TEXT NOT NULL, server_actor INTEGER NOT NULL CHECK(server_actor>0)
        );
        CREATE TABLE IF NOT EXISTS linux_room_sync_outbox (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL UNIQUE, action TEXT NOT NULL,
            room_id INTEGER NOT NULL, room_json TEXT NOT NULL, expected_json TEXT
        );
        CREATE TABLE IF NOT EXISTS linux_room_sync_shadow (room_id INTEGER PRIMARY KEY, room_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS linux_room_sync_guard (singleton INTEGER PRIMARY KEY CHECK(singleton=1), suppressed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS linux_room_sync_resolutions (id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL, actor TEXT NOT NULL, local_json TEXT NOT NULL, remote_json TEXT NOT NULL, previous_operations TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS linux_room_sync_resolution_details (resolution_id INTEGER PRIMARY KEY, choice TEXT NOT NULL CHECK(choice IN ('server','local')), actor_employee_id INTEGER NOT NULL CHECK(actor_employee_id>0), identity_mapping INTEGER NOT NULL CHECK(identity_mapping IN (0,1)), FOREIGN KEY(resolution_id) REFERENCES linux_room_sync_resolutions(id));
        INSERT OR IGNORE INTO linux_room_sync_guard(singleton,suppressed) VALUES(1,0);
        BEGIN IMMEDIATE;
        DROP TRIGGER IF EXISTS linux_room_sync_insert;
        DROP TRIGGER IF EXISTS linux_room_sync_update;
        CREATE TRIGGER linux_room_sync_insert AFTER INSERT ON rooms
        WHEN EXISTS(SELECT 1 FROM linux_room_sync_config WHERE singleton=1) AND NOT EXISTS(SELECT 1 FROM linux_room_sync_guard WHERE singleton=1 AND suppressed=1)
        BEGIN
          INSERT OR IGNORE INTO sync_object_links(object_type,external_id,local_id)
            SELECT 'room',client_id||':room:'||NEW.id,NEW.id FROM linux_room_sync_config WHERE singleton=1;
          INSERT INTO linux_room_sync_outbox(operation_id,action,room_id,room_json,expected_json)
            SELECT lower(hex(randomblob(16))),'create',NEW.id,json_object('id',external_id,'name',NEW.name,'location',NEW.location,'capacity',NEW.capacity,'equipment',NEW.equipment,'active',json('true')),NULL FROM sync_object_links WHERE object_type='room' AND local_id=NEW.id;
          INSERT INTO linux_room_sync_outbox(operation_id,action,room_id,room_json,expected_json)
            SELECT lower(hex(randomblob(16))),'archive',NEW.id,json_object('id',external_id,'name',NEW.name,'location',NEW.location,'capacity',NEW.capacity,'equipment',NEW.equipment,'active',json('false')),json_object('id',external_id,'name',NEW.name,'location',NEW.location,'capacity',NEW.capacity,'equipment',NEW.equipment,'active',json('true')) FROM sync_object_links WHERE object_type='room' AND local_id=NEW.id AND NEW.active=0;
        END;
        CREATE TRIGGER linux_room_sync_update AFTER UPDATE OF name,location,capacity,equipment,active ON rooms
        WHEN EXISTS(SELECT 1 FROM linux_room_sync_config WHERE singleton=1) AND NOT EXISTS(SELECT 1 FROM linux_room_sync_guard WHERE singleton=1 AND suppressed=1) AND (OLD.name IS NOT NEW.name OR OLD.location IS NOT NEW.location OR OLD.capacity IS NOT NEW.capacity OR OLD.equipment IS NOT NEW.equipment OR OLD.active IS NOT NEW.active)
        BEGIN
          INSERT OR IGNORE INTO sync_object_links(object_type,external_id,local_id)
            SELECT 'room',client_id||':room:'||NEW.id,NEW.id FROM linux_room_sync_config WHERE singleton=1;
          INSERT INTO linux_room_sync_outbox(operation_id,action,room_id,room_json,expected_json)
            SELECT lower(hex(randomblob(16))),CASE WHEN OLD.active=1 AND NEW.active=0 THEN 'archive' ELSE 'update' END,NEW.id,
              json_object('id',external_id,'name',NEW.name,'location',NEW.location,'capacity',NEW.capacity,'equipment',NEW.equipment,'active',json(CASE WHEN NEW.active THEN 'true' ELSE 'false' END)),
              json_object('id',external_id,'name',OLD.name,'location',OLD.location,'capacity',OLD.capacity,'equipment',OLD.equipment,'active',json(CASE WHEN OLD.active THEN 'true' ELSE 'false' END)) FROM sync_object_links WHERE object_type='room' AND local_id=NEW.id;
        END;
        COMMIT;
    )SQL";
    const bool ok=sqlite3_exec(db,sql,nullptr,nullptr,nullptr)==SQLITE_OK;if(!ok)sqlite3_exec(db,"ROLLBACK",nullptr,nullptr,nullptr);sqlite3_close(db);return ok;
}
struct WorkspaceLock {
    int fd=-1;
    explicit WorkspaceLock(int value):fd(value) {}
    ~WorkspaceLock(){if(fd>=0){flock(fd,LOCK_UN);close(fd);}}
};
struct Session {
    explicit Session(const char* path,int fd):lock(fd),db(widen(path)),path(path) {}
    WorkspaceLock lock;
    EnterpriseDatabase db;
    std::string path;
    int64_t employee=0;
    uint64_t login_generation=0;
    bool must_change=false;
    std::wstring actor;
    std::string result;
    std::string error;
    bool fail(const std::string& value) { error=value; return false; }
    bool dbfail() { return fail(utf8(db.error())); }
    bool active() {
        if(employee<=0 || must_change) return fail("Bitte zuerst anmelden und gegebenenfalls das Passwort ändern.");
        uint64_t current=0;if(!read_restore_generation(lock.fd,current) || current!=login_generation){employee=0;return fail("Datenbank wurde wiederhergestellt. Bitte erneut anmelden.");}
        bool enabled=false, activeEmployee=false;
        for(const auto& a:db.accounts(true)) if(a.employeeId==employee && a.enabled && !a.mustChangePassword) enabled=true;
        for(const auto& e:db.employees()) if(e.id==employee) activeEmployee=true;
        if(!enabled || !activeEmployee) { employee=0; return fail("Konto oder Mitarbeiter ist nicht mehr aktiv."); }
        return true;
    }
    bool permit(const wchar_t* permission) {
        if(!active()) return false;
        return db.hasPermission(employee,permission) || fail("Für diese Aktion fehlt die Berechtigung.");
    }
    bool permitCollaboration(const std::wstring& subject) {
        if(!active()) return false;
        const auto manage=subject==L"task"?L"tasks.manage":L"tickets.manage";
        const auto read=subject==L"task"?L"tasks.read":L"tickets.read";
        return db.hasPermission(employee,manage) || (db.hasPermission(employee,read) && db.hasPermission(employee,L"collaboration.comment")) || fail("Für diese Aktion fehlt die Berechtigung.");
    }
};
}
extern "C" {
// Build a new database under a private temporary name, then publish it with
// link(2), which refuses to overwrite an existing workspace. On failure, no
// partially initialised target is exposed.
int aura_local_create(const char* path,const char* display_name,const char* username,const char* password) {
    if(!path || !*path) return 0;
    std::string temp=std::string(path)+".init-XXXXXX";
    std::vector<char> writable(temp.begin(),temp.end()); writable.push_back('\0');
    int fd=mkstemp(writable.data()); if(fd<0) return 0;
    close(fd); temp=writable.data();
    bool ok=false;
    try {
        const auto name=widen(display_name), user=widen(username), pass=widen(password);
        if(!name.empty() && !user.empty() && pass.size()>=12) {
            {
                EnterpriseDatabase db(widen(temp.c_str())); int64_t employee=0;
                ok=db.ready() && db.createEmployee({0,name,L"Administrator",L"Administration",L"office",true},employee,L"system-bootstrap")
                    && db.assignRole(employee,L"admin",L"system-bootstrap")
                    && db.createOrResetAccount(employee,user,pass,false,L"system-bootstrap");
            }
            if(ok) ok=link(temp.c_str(),path)==0;
        }
    } catch(...) { ok=false; }
    unlink(temp.c_str());
    unlink((temp+"-wal").c_str()); unlink((temp+"-shm").c_str());unlink((temp+"-journal").c_str());
    return ok ? 1 : 0;
}
// Existing files only. A missing or inaccessible database must never silently
// create an empty workspace. Bootstrap is a separate explicit operation.
void* aura_local_open(const char* path) {
    try {
        struct stat st{};
        if(!path || lstat(path,&st)!=0 || !S_ISREG(st.st_mode)) return nullptr;
        std::string lockPath=std::string(path)+".aura.lock";
        int fd=open(lockPath.c_str(),O_CREAT|O_RDWR|O_CLOEXEC|O_NOFOLLOW,0600);
        if(fd<0)return nullptr;struct stat lockStat{};
        if(fstat(fd,&lockStat)!=0 || !S_ISREG(lockStat.st_mode) || flock(fd,LOCK_SH|LOCK_NB)!=0){close(fd);return nullptr;}
        std::unique_ptr<Session> s;
        try {s=std::make_unique<Session>(path,fd);}catch(...){flock(fd,LOCK_UN);close(fd);throw;}
        if(!s->db.ready() || !ensure_linux_room_sync_schema(path)) return nullptr;
        return s.release();
    } catch(...) { return nullptr; }
}
void aura_local_close(void* handle) { delete static_cast<Session*>(handle); }
const char* aura_local_error(void* handle) { return handle ? static_cast<Session*>(handle)->error.c_str() : "Datenbank konnte nicht geöffnet werden."; }
// Expose only the authenticated local identity; server identity remains separate.
int64_t aura_local_session_employee(void* handle) {
    auto* s=static_cast<Session*>(handle);
    return s && s->active() ? s->employee : 0;
}
const char* aura_local_identity(void* handle) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->active())return nullptr;
    try {
        const bool manageEmployees=s->db.hasPermission(s->employee,L"employees.manage");
        const bool approveLeave=manageEmployees||s->db.hasPermission(s->employee,L"leave.approve");
        const bool manageRooms=s->db.hasPermission(s->employee,L"rooms.manage");
        const bool manageRoles=s->db.hasPermission(s->employee,L"admin.manage");
        std::ostringstream out;out << "{\"employeeId\":" << s->employee << ",\"canManageEmployees\":" << (manageEmployees?"true":"false") << ",\"canManageRooms\":" << (manageRooms?"true":"false") << ",\"canApproveLeave\":" << (approveLeave?"true":"false") << ",\"canRequestLeave\":true,\"canManageTasks\":" << (s->db.hasPermission(s->employee,L"tasks.manage")?"true":"false") << ",\"canManageTickets\":" << (s->db.hasPermission(s->employee,L"tickets.manage")?"true":"false") << ",\"canManageProjects\":" << (s->db.hasPermission(s->employee,L"tasks.manage")?"true":"false") << ",\"canReadTasks\":" << ((s->db.hasPermission(s->employee,L"tasks.read")||s->db.hasPermission(s->employee,L"tasks.manage"))?"true":"false") << ",\"canCollaborate\":" << (s->db.hasPermission(s->employee,L"collaboration.comment")?"true":"false") << ",\"canReadTickets\":" << ((s->db.hasPermission(s->employee,L"tickets.read")||s->db.hasPermission(s->employee,L"tickets.manage"))?"true":"false") << ",\"canManageRoles\":" << (manageRoles?"true":"false") << '}';
        s->result=out.str();return s->result.c_str();
    }catch(...){s->fail("Sitzungsrechte konnten nicht gelesen werden.");return nullptr;}
}
int aura_local_login(void* handle,const char* name,const char* password) {
    auto* s=static_cast<Session*>(handle); if(!s) return 0;
    try {
        s->employee=0; s->must_change=false; s->actor.clear(); s->error.clear();
        EnterpriseAccount account;
        if(!s->db.authenticate(widen(name),widen(password),std::time(nullptr),account)) { s->dbfail(); return 0; }
        for(const auto& e:s->db.employees()) if(e.id==account.employeeId) {
            if(!read_restore_generation(s->lock.fd,s->login_generation)){s->fail("Sitzungssperre konnte nicht gelesen werden.");return 0;}
            s->employee=e.id; s->actor=e.name; s->must_change=account.mustChangePassword;
            s->db.setAuditActor(e.name); return account.mustChangePassword ? 2 : 1;
        }
        s->fail("Zum Konto gehört kein aktiver Mitarbeiter."); return 0;
    } catch(...) { s->fail("Ungültige Zeichencodierung oder interner Fehler."); return 0; }
}
int aura_local_change_password(void* handle,const char* oldpass,const char* newpass) {
    auto* s=static_cast<Session*>(handle); if(!s || s->employee<=0) return 0;
    try {
        uint64_t generation=0;if(!read_restore_generation(s->lock.fd,generation) || generation!=s->login_generation){s->employee=0;return s->fail("Datenbank wurde wiederhergestellt. Bitte erneut anmelden.");}
        bool enabled=false, activeEmployee=false;
        for(const auto& a:s->db.accounts(true)) if(a.employeeId==s->employee && a.enabled) enabled=true;
        for(const auto& e:s->db.employees()) if(e.id==s->employee) activeEmployee=true;
        if(!enabled || !activeEmployee) { s->employee=0; return s->fail("Konto oder Mitarbeiter ist nicht mehr aktiv."); }
        if(!s->db.changePassword(s->employee,widen(oldpass),widen(newpass),s->actor)) return s->dbfail();
        s->must_change=false; return 1;
    } catch(...) { return s->fail("Passwort konnte nicht geändert werden."); }
}
const char* aura_local_rooms(void* handle) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->active()) return nullptr;
    try {
        std::ostringstream out; out << '['; bool first=true;
        for(const auto& r:s->db.rooms()) {
            if(!first) out << ','; first=false;
            EnterpriseBooking booking{};const bool occupied=s->db.activeBookingForRoom(r.id,std::time(nullptr),booking);
            out << "{\"id\":" << r.id << ",\"name\":" << quote(r.name) << ",\"location\":" << quote(r.location)
                << ",\"capacity\":" << r.capacity << ",\"equipment\":" << quote(r.equipment) << ",\"active\":true,\"occupied\":" << (occupied?"true":"false") << ",\"availability\":\"" << (occupied?(booking.status==L"blocked"?"blocked":"booked"):"free") << "\"}";
        }
        out << ']'; s->result=out.str(); return s->result.c_str();
    } catch(...) { s->fail("Raumliste konnte nicht gelesen werden."); return nullptr; }
}
int aura_local_room_save(void* handle,int64_t id,const char* name,const char* location,int capacity,const char* equipment) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"rooms.manage")) return 0;
    try {
        EnterpriseRoom room{id,widen(name),widen(location),capacity,widen(equipment),true}; int64_t created=0;
        const bool ok=id==0 ? s->db.createRoom(room,created,s->actor) : s->db.updateRoom(room,s->actor);
        return ok ? 1 : s->dbfail();
    } catch(...) { return s->fail("Raum konnte nicht gespeichert werden."); }
}
const char* aura_local_tasks(void* handle) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->active()) return nullptr;
    try {
        std::unordered_map<int64_t,std::vector<int64_t>> projects_for_task;
        for(const auto& project:s->db.projects(true))
            for(const auto& task:s->db.tasksForProject(project.id,true)) projects_for_task[task.id].push_back(project.id);
        std::ostringstream out; out << '['; bool first=true;
        for(const auto& t:s->db.tasks()) {
            if(!first) out << ','; first=false;
            out << "{\"id\":" << t.id << ",\"title\":" << quote(t.title) << ",\"department\":" << quote(t.department)
                << ",\"priority\":" << quote(t.priority) << ",\"status\":" << quote(t.status) << ",\"assigneeId\":" << t.assigneeId
                << ",\"dueAt\":" << t.dueAt << ",\"recurrenceDays\":" << t.recurrenceDays
                << ",\"projectId\":" << (projects_for_task[t.id].empty()?0:projects_for_task[t.id][0]) << ",\"projectIds\":[";
            bool first_project=true;for(auto id:projects_for_task[t.id]){if(!first_project)out << ',';first_project=false;out << id;}out << "],\"active\":true}";
        }
        out << ']'; s->result=out.str(); return s->result.c_str();
    } catch(...) { s->fail("Aufgaben konnten nicht geladen werden."); return nullptr; }
}
const char* aura_local_roles(void* handle) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage"))return nullptr;
    try {std::ostringstream out;out << '[';bool first=true;
        for(const auto& role:s->db.roles()) {if(!first)out << ',';first=false;
            out << "{\"id\":" << role.id << ",\"code\":" << quote(role.code) << ",\"name\":" << quote(role.name)
                << ",\"description\":" << quote(role.description) << ",\"permissions\":[";
            bool firstPermission=true;
            for(const auto& permission:s->db.permissionsForRole(role.id)) {
                if(!firstPermission)out << ',';firstPermission=false;out << quote(permission);
            }
            out << "]}";}
        out << ']';s->result=out.str();return s->result.c_str();
    }catch(...){s->fail("Rollen konnten nicht geladen werden.");return nullptr;}
}
int aura_local_role_save(void* handle,int64_t id,const char* code,const char* name,const char* description,const char* permissions) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage"))return 0;
    try {
        const auto role_code=widen(code);
        if(id!=0) {
            const auto own=s->db.rolesForEmployee(s->employee);
            for(const auto& role:s->db.roles()) if(role.id==id) {
                if(role.code==L"admin")return s->fail("Die Systemadministratorrolle ist geschützt.");
                for(const auto& assigned:own) if(assigned==role.code)return s->fail("Die eigene Rolle kann nicht bearbeitet werden.");
            }
        }
        const std::wstring raw=widen(permissions);std::vector<std::wstring> parsed;size_t start=0;
        while(start<=raw.size()) {
            size_t end=raw.find(L',',start);std::wstring item=raw.substr(start,end==std::wstring::npos?end:end-start);
            auto first=item.find_first_not_of(L" \t\r\n"),last=item.find_last_not_of(L" \t\r\n");
            if(first==std::wstring::npos)return s->fail("Berechtigungen dürfen nicht leer sein.");
            parsed.push_back(item.substr(first,last-first+1));if(end==std::wstring::npos)break;start=end+1;
        }
        EnterpriseRole role{id,role_code,widen(name),widen(description),true};int64_t created=0;
        bool ok=id==0?s->db.createRole(role,created,parsed,s->actor):s->db.updateRole(role,parsed,s->actor);
        return ok?1:s->dbfail();
    }catch(...){return s->fail("Rolle konnte nicht gespeichert werden.");}
}
int aura_local_role_archive(void* handle,int64_t id) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage"))return 0;
    try {const auto own=s->db.rolesForEmployee(s->employee);
        for(const auto& role:s->db.roles())if(role.id==id) {
            if(role.code==L"admin")return s->fail("Die Systemadministratorrolle ist geschützt.");
            for(const auto& assigned:own)if(assigned==role.code)return s->fail("Die eigene Rolle kann nicht archiviert werden.");
        }
        return s->db.archiveRole(id,s->actor)?1:s->dbfail();
    }catch(...){return s->fail("Rolle konnte nicht archiviert werden.");}
}
// Publish an SQLite snapshot without ever overwriting the selected target.
// The temporary file lives in the same directory so link(2) is atomic.
int aura_local_backup(void* handle,const char* path) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage"))return 0;
    if(!path||!*path)return s->fail("Ungültiger Sicherungspfad.");
    std::string target(path),temp=target+".init-XXXXXX";
    std::vector<char> writable(temp.begin(),temp.end());writable.push_back('\0');
    int fd=mkstemp(writable.data());if(fd<0)return s->fail("Sicherungsdatei konnte nicht angelegt werden.");
    close(fd);temp=writable.data();bool ok=false;
    try {
        if(s->db.backupTo(widen(temp.c_str()),s->actor,false)) {
            ok=link(temp.c_str(),target.c_str())==0;
            if(!ok)s->fail("Sicherungsziel existiert bereits oder kann nicht erstellt werden.");
            else if(!s->db.recordBackup(widen(target.c_str()),s->actor)) { unlink(target.c_str()); ok=false; s->dbfail(); }
        } else s->dbfail();
    }catch(...){s->fail("Sicherung konnte nicht erstellt werden.");}
    unlink(temp.c_str());unlink((temp+"-wal").c_str());unlink((temp+"-shm").c_str());unlink((temp+"-journal").c_str());
    return ok?1:0;
}
int aura_local_rooms_import(void* handle,const char* path) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"rooms.manage"))return -1;
    try {int count=0;if(!s->db.importRoomsCsv(widen(path),count,s->actor)){s->dbfail();return -1;}return count;}catch(...){s->fail("CSV-Import fehlgeschlagen.");return -1;}
}
int aura_local_rooms_export(void* handle,const char* path) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"rooms.manage"))return 0;if(!path||!*path)return s->fail("Ungültiger Exportpfad.");
    std::string target(path),temp=target+".init-XXXXXX";std::vector<char> name(temp.begin(),temp.end());name.push_back('\0');int fd=mkstemp(name.data());if(fd<0)return s->fail("Exportdatei konnte nicht angelegt werden.");close(fd);temp=name.data();bool ok=false;
    try {if(s->db.exportRoomsCsv(widen(temp.c_str()),s->actor,false)){ok=link(temp.c_str(),target.c_str())==0;if(!ok)s->fail("Exportziel existiert bereits oder kann nicht erstellt werden.");else if(!s->db.recordRoomExport(widen(target.c_str()),s->actor)){unlink(target.c_str());ok=false;s->dbfail();}}else s->dbfail();}catch(...){s->fail("CSV-Export fehlgeschlagen.");}unlink(temp.c_str());return ok?1:0;
}
// Both connections are read-only here. Functional schema objects must match;
// conflict-history tables may have been created after an older snapshot.
bool validate_restore_schema(sqlite3* source,sqlite3* current) {
    sqlite3_stmt* stmt=nullptr;bool ok=sqlite3_prepare_v2(source,"PRAGMA integrity_check",-1,&stmt,nullptr)==SQLITE_OK;
    if(ok){ok=sqlite3_step(stmt)==SQLITE_ROW;const auto* result=ok?sqlite3_column_text(stmt,0):nullptr;ok=result && std::string(reinterpret_cast<const char*>(result))=="ok" && sqlite3_step(stmt)==SQLITE_DONE;}
    if(stmt)sqlite3_finalize(stmt);stmt=nullptr;if(!ok)return false;
    ok=sqlite3_prepare_v2(source,"PRAGMA foreign_key_check",-1,&stmt,nullptr)==SQLITE_OK;
    if(ok)ok=sqlite3_step(stmt)==SQLITE_DONE;if(stmt)sqlite3_finalize(stmt);if(!ok)return false;
    const char* sql="SELECT type,name,tbl_name,coalesce(sql,'') FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name";
    static const std::unordered_set<std::string> optionalHistory={
        "linux_approval_sync_resolutions","linux_booking_sync_resolutions","linux_dependency_sync_conflicts",
        "linux_employee_sync_resolutions","linux_facility_asset_sync_conflicts","linux_leave_sync_resolutions",
        "linux_maintenance_sync_conflicts","linux_maintenance_successor_links","linux_organization_sync_conflicts","linux_project_assignment_sync_conflicts",
        "linux_project_sync_conflicts","linux_room_sync_resolutions","linux_room_sync_resolution_details","linux_subtask_sync_conflicts","desktop_task_recurrence_binding",
        "linux_subtask_sync_resolutions","linux_task_sync_resolutions","linux_ticket_sync_conflicts",
        "linux_task_attachment_sync_conflicts","linux_ticket_attachment_sync_conflicts",
        "linux_task_attachment_sync_archive_actor","linux_ticket_attachment_sync_archive_actor"};
    using SchemaRow=std::vector<std::string>;
    std::unordered_map<std::string,SchemaRow> oldSchema,newSchema;
    auto readSchema=[&](sqlite3* db,std::unordered_map<std::string,SchemaRow>& result){
        sqlite3_stmt* query=nullptr;if(sqlite3_prepare_v2(db,sql,-1,&query,nullptr)!=SQLITE_OK)return false;
        int step;bool valid=true;
        while((step=sqlite3_step(query))==SQLITE_ROW){SchemaRow row;for(int i=0;i<4;i++){const auto* value=sqlite3_column_text(query,i);if(!value){valid=false;break;}row.emplace_back(reinterpret_cast<const char*>(value));}if(!valid)break;if(!result.emplace(row[0]+":"+row[1],row).second){valid=false;break;}}
        sqlite3_finalize(query);return valid&&step==SQLITE_DONE;
    };
    if(!readSchema(source,oldSchema)||!readSchema(current,newSchema))return false;
    for(const auto& entry:oldSchema){auto found=newSchema.find(entry.first);if(found==newSchema.end()||found->second!=entry.second)return false;}
    for(const auto& entry:newSchema){if(oldSchema.count(entry.first))continue;const auto& row=entry.second;
        const bool history=row[0]=="table"&&optionalHistory.count(row[1]);
        const bool restoreTrigger=row[0]=="trigger"&&(row[1]=="linux_task_attachment_sync_restore"||row[1]=="linux_ticket_attachment_sync_restore");
        if(!history&&!restoreTrigger)return false;
    }
    return true;
}
// Restore triggers were introduced after some valid snapshots were made.
// Copy their already-validated current definitions into the private snapshot,
// so a restored configured outbox immediately records later reactivations.
bool upgrade_restore_attachment_triggers(sqlite3* snapshot,sqlite3* current) {
    const char* names[]={"linux_task_attachment_sync_restore","linux_ticket_attachment_sync_restore"};
    for(const char* name:names){
        sqlite3_stmt* existing=nullptr;bool ok=sqlite3_prepare_v2(snapshot,"SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",-1,&existing,nullptr)==SQLITE_OK;
        int step=SQLITE_ERROR;if(ok){sqlite3_bind_text(existing,1,name,-1,SQLITE_STATIC);step=sqlite3_step(existing);ok=step==SQLITE_ROW||step==SQLITE_DONE;}if(existing)sqlite3_finalize(existing);if(!ok)return false;if(step==SQLITE_ROW)continue;
        sqlite3_stmt* query=nullptr;ok=sqlite3_prepare_v2(current,"SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",-1,&query,nullptr)==SQLITE_OK;
        if(ok){sqlite3_bind_text(query,1,name,-1,SQLITE_STATIC);step=sqlite3_step(query);if(step==SQLITE_ROW){const auto* sql=sqlite3_column_text(query,0);ok=sql&&sqlite3_exec(snapshot,reinterpret_cast<const char*>(sql),nullptr,nullptr,nullptr)==SQLITE_OK;}else ok=step==SQLITE_DONE;}
        if(query)sqlite3_finalize(query);if(!ok)return false;
    }
    // Older valid backups have no room-decision details table. Install the
    // current empty table definition before restoring so the next decision can
    // be recorded even before the workspace is reopened.
    sqlite3_stmt* query=nullptr;
    bool ok=sqlite3_prepare_v2(snapshot,"SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_room_sync_resolution_details'",-1,&query,nullptr)==SQLITE_OK;
    int step=ok?sqlite3_step(query):SQLITE_ERROR;if(query)sqlite3_finalize(query);
    if(!ok || (step!=SQLITE_ROW && step!=SQLITE_DONE))return false;
    if(step==SQLITE_DONE){
        query=nullptr;ok=sqlite3_prepare_v2(current,"SELECT sql FROM sqlite_master WHERE type='table' AND name='linux_room_sync_resolution_details'",-1,&query,nullptr)==SQLITE_OK;
        if(ok){step=sqlite3_step(query);const auto* sql=step==SQLITE_ROW?sqlite3_column_text(query,0):nullptr;ok=sql && sqlite3_exec(snapshot,reinterpret_cast<const char*>(sql),nullptr,nullptr,nullptr)==SQLITE_OK;}
        if(query)sqlite3_finalize(query);if(!ok)return false;
    }
    return true;
}
// Restore from a private SQLite snapshot and retain the previous state as a
// separate 0600 backup. Other cooperating sessions prevent exclusive restore.
const char* aura_local_restore(void* handle,const char* sourcePath) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage"))return nullptr;
    if(!sourcePath||!*sourcePath){s->fail("Ungültiger Sicherungspfad.");return nullptr;}
    struct stat sourceStat{},currentStat{};
    if(lstat(sourcePath,&sourceStat)!=0 || !S_ISREG(sourceStat.st_mode) || stat(s->path.c_str(),&currentStat)!=0 || (sourceStat.st_dev==currentStat.st_dev && sourceStat.st_ino==currentStat.st_ino)){s->fail("Bitte eine andere reguläre Sicherungsdatei wählen.");return nullptr;}
    if(flock(s->lock.fd,LOCK_UN)!=0 || flock(s->lock.fd,LOCK_EX|LOCK_NB)!=0){flock(s->lock.fd,LOCK_SH);s->employee=0;s->fail("Andere Linux-Sitzungen sind geöffnet. Bitte diese zuerst schließen und sich erneut anmelden.");return nullptr;}
    struct Downgrade {int fd;~Downgrade(){flock(fd,LOCK_SH);}} downgrade{s->lock.fd};
    if(!s->permit(L"admin.manage"))return nullptr;
    std::string snapshot=s->path+".restore-snapshot-XXXXXX",previous=s->path+".before-restore-XXXXXX";
    std::vector<char> a(snapshot.begin(),snapshot.end());a.push_back('\0');int afd=mkstemp(a.data());if(afd<0){s->fail("Temporäre Datei konnte nicht erstellt werden.");return nullptr;}close(afd);snapshot=a.data();
    struct Cleanup {std::string path;~Cleanup(){unlink(path.c_str());unlink((path+"-wal").c_str());unlink((path+"-shm").c_str());unlink((path+"-journal").c_str());}} cleanup{snapshot};
    sqlite3 *src=nullptr,*copy=nullptr,*cur=nullptr;bool ok=sqlite3_open_v2(sourcePath,&src,SQLITE_OPEN_READONLY,nullptr)==SQLITE_OK && sqlite3_open_v2(snapshot.c_str(),&copy,SQLITE_OPEN_READWRITE,nullptr)==SQLITE_OK;
    if(ok){auto* backup=sqlite3_backup_init(copy,"main",src,"main");if(!backup)ok=false;else {int step=sqlite3_backup_step(backup,-1);int finish=sqlite3_backup_finish(backup);ok=step==SQLITE_DONE && finish==SQLITE_OK;}}
    if(src)sqlite3_close(src);
    if(ok)ok=sqlite3_open_v2(s->path.c_str(),&cur,SQLITE_OPEN_READONLY,nullptr)==SQLITE_OK && validate_restore_schema(copy,cur) && upgrade_restore_attachment_triggers(copy,cur);
    if(cur)sqlite3_close(cur);
    if(ok){sqlite3_stmt* st=nullptr;ok=sqlite3_prepare_v2(copy,"INSERT INTO audit_log(actor,action,subject) VALUES(?, 'database.restore.completed', ?)",-1,&st,nullptr)==SQLITE_OK;if(ok){std::string actor=utf8(s->actor);sqlite3_bind_text(st,1,actor.c_str(),-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,2,sourcePath,-1,SQLITE_TRANSIENT);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);}
    if(copy && sqlite3_close(copy)!=SQLITE_OK)ok=false;
    if(!ok){s->fail("Sicherung ist beschädigt, nicht kompatibel oder konnte nicht geprüft werden.");return nullptr;}
    std::vector<char> b(previous.begin(),previous.end());b.push_back('\0');int bfd=mkstemp(b.data());if(bfd<0){s->fail("Sicherung des aktuellen Zustands konnte nicht angelegt werden.");return nullptr;}close(bfd);previous=b.data();
    if(!s->db.backupTo(widen(previous.c_str()),s->actor,false)){unlink(previous.c_str());s->dbfail();return nullptr;}
    uint64_t generation=0;if(!read_restore_generation(s->lock.fd,generation) || generation==UINT64_MAX || pwrite(s->lock.fd,&++generation,sizeof(generation),0)!=static_cast<ssize_t>(sizeof(generation)) || fsync(s->lock.fd)!=0){s->employee=0;s->fail("Sitzungen konnten nicht ungültig gemacht werden. Vorherige Datenbanksicherung: "+previous);return nullptr;}
    ok=s->db.restoreFrom(widen(snapshot.c_str()),s->actor,false);
    s->employee=0;s->must_change=false;s->actor.clear();s->db.setAuditActor(L"");
    if(!ok){s->fail("Wiederherstellung fehlgeschlagen. Vorherige Datenbanksicherung: "+previous+". Bitte erneut anmelden.");return nullptr;}
    s->result=previous;return s->result.c_str();
}
// Room outbox operations are restricted to administrators. The destination
// identity is stored without its token and cannot be silently changed.
int aura_local_room_sync_enable(void* handle,const char* client,const char* url,int64_t actor) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage"))return 0;
    if(!client||!*client||!url||!*url||actor<=0)return s->fail("Ungültige Synchronisationskonfiguration.");
    sqlite3* db=nullptr;if(sqlite3_open_v2(s->path.c_str(),&db,SQLITE_OPEN_READWRITE|SQLITE_OPEN_FULLMUTEX,nullptr)!=SQLITE_OK){if(db)sqlite3_close(db);return s->fail("Synchronisationsdatenbank nicht verfügbar.");}sqlite3_busy_timeout(db,5000);
    bool ok=sqlite3_exec(db,"BEGIN IMMEDIATE",nullptr,nullptr,nullptr)==SQLITE_OK;sqlite3_stmt* st=nullptr;bool exists=false;
    if(ok)ok=sqlite3_prepare_v2(db,"SELECT server_url,server_actor FROM linux_room_sync_config WHERE singleton=1",-1,&st,nullptr)==SQLITE_OK;
    if(ok){int step=sqlite3_step(st);if(step==SQLITE_ROW){exists=true;const auto* existing=sqlite3_column_text(st,0);ok=existing && std::string(reinterpret_cast<const char*>(existing))==url && sqlite3_column_int64(st,1)==actor;}else if(step!=SQLITE_DONE)ok=false;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok && !exists){ok=sqlite3_prepare_v2(db,"INSERT INTO linux_room_sync_config VALUES(1,?,?,?)",-1,&st,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_text(st,1,client,-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,2,url,-1,SQLITE_TRANSIENT);sqlite3_bind_int64(st,3,actor);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
        if(ok)ok=sqlite3_exec(db,"INSERT OR IGNORE INTO sync_object_links(object_type,external_id,local_id) SELECT 'room',c.client_id||':room:'||r.id,r.id FROM rooms r CROSS JOIN linux_room_sync_config c WHERE c.singleton=1",nullptr,nullptr,nullptr)==SQLITE_OK;
        if(ok)ok=sqlite3_exec(db,"INSERT INTO linux_room_sync_outbox(operation_id,action,room_id,room_json,expected_json) SELECT lower(hex(randomblob(16))),'create',r.id,json_object('id',l.external_id,'name',r.name,'location',r.location,'capacity',r.capacity,'equipment',r.equipment,'active',json('true')),NULL FROM rooms r JOIN sync_object_links l ON l.object_type='room' AND l.local_id=r.id ORDER BY r.id",nullptr,nullptr,nullptr)==SQLITE_OK;
        if(ok)ok=sqlite3_exec(db,"INSERT INTO linux_room_sync_outbox(operation_id,action,room_id,room_json,expected_json) SELECT lower(hex(randomblob(16))),'archive',r.id,json_object('id',l.external_id,'name',r.name,'location',r.location,'capacity',r.capacity,'equipment',r.equipment,'active',json('false')),json_object('id',l.external_id,'name',r.name,'location',r.location,'capacity',r.capacity,'equipment',r.equipment,'active',json('true')) FROM rooms r JOIN sync_object_links l ON l.object_type='room' AND l.local_id=r.id WHERE r.active=0 ORDER BY r.id",nullptr,nullptr,nullptr)==SQLITE_OK;
    }
    if(ok)ok=sqlite3_exec(db,"COMMIT",nullptr,nullptr,nullptr)==SQLITE_OK;if(!ok)sqlite3_exec(db,"ROLLBACK",nullptr,nullptr,nullptr);sqlite3_close(db);
    if(!ok)return s->fail("Synchronisierung konnte nicht aktiviert werden oder ist bereits an einen anderen Server bzw. ein anderes Serverkonto gebunden.");return 1;
}
const char* aura_local_room_sync_next(void* handle) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage"))return nullptr;
    sqlite3* db=nullptr;if(sqlite3_open_v2(s->path.c_str(),&db,SQLITE_OPEN_READONLY|SQLITE_OPEN_FULLMUTEX,nullptr)!=SQLITE_OK){if(db)sqlite3_close(db);s->fail("Synchronisationsdatenbank nicht verfügbar.");return nullptr;}sqlite3_busy_timeout(db,5000);sqlite3_stmt* st=nullptr;
    const char* sql="SELECT json_object('serverUrl',c.server_url,'actor',c.server_actor,'pending',(SELECT count(*) FROM linux_room_sync_outbox),'operation',(SELECT json_object('clientId',c.client_id,'operationId',o.operation_id,'action',o.action,'room',json(o.room_json),'expected',json(o.expected_json)) FROM linux_room_sync_outbox o ORDER BY o.seq LIMIT 1)) FROM linux_room_sync_config c WHERE singleton=1";
    bool ok=sqlite3_prepare_v2(db,sql,-1,&st,nullptr)==SQLITE_OK;int step=ok?sqlite3_step(st):SQLITE_ERROR;
    if(step==SQLITE_ROW){const auto* p=sqlite3_column_text(st,0);if(p)s->result=reinterpret_cast<const char*>(p);else ok=false;}else if(step==SQLITE_DONE)s->result="null";else ok=false;
    if(st)sqlite3_finalize(st);sqlite3_close(db);if(!ok){s->fail("Synchronisationswarteschlange konnte nicht gelesen werden.");return nullptr;}return s->result.c_str();
}
int aura_local_room_sync_ack(void* handle,const char* operation) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage")||!operation||!*operation)return 0;
    sqlite3* db=nullptr;if(sqlite3_open_v2(s->path.c_str(),&db,SQLITE_OPEN_READWRITE|SQLITE_OPEN_FULLMUTEX,nullptr)!=SQLITE_OK){if(db)sqlite3_close(db);return s->fail("Synchronisationsdatenbank nicht verfügbar.");}sqlite3_busy_timeout(db,5000);sqlite3_stmt* st=nullptr;
    bool ok=sqlite3_exec(db,"BEGIN IMMEDIATE",nullptr,nullptr,nullptr)==SQLITE_OK;
    if(ok)ok=sqlite3_prepare_v2(db,"INSERT INTO linux_room_sync_shadow(room_id,room_json) SELECT room_id,room_json FROM linux_room_sync_outbox WHERE seq=(SELECT min(seq) FROM linux_room_sync_outbox) AND operation_id=? ON CONFLICT(room_id) DO UPDATE SET room_json=excluded.room_json",-1,&st,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_text(st,1,operation,-1,SQLITE_TRANSIENT);ok=sqlite3_step(st)==SQLITE_DONE && sqlite3_changes(db)==1;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok)ok=sqlite3_prepare_v2(db,"DELETE FROM linux_room_sync_outbox WHERE seq=(SELECT min(seq) FROM linux_room_sync_outbox) AND operation_id=?",-1,&st,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_text(st,1,operation,-1,SQLITE_TRANSIENT);ok=sqlite3_step(st)==SQLITE_DONE && sqlite3_changes(db)==1;}if(st)sqlite3_finalize(st);
    if(ok)ok=sqlite3_exec(db,"COMMIT",nullptr,nullptr,nullptr)==SQLITE_OK;if(!ok)sqlite3_exec(db,"ROLLBACK",nullptr,nullptr,nullptr);sqlite3_close(db);if(!ok)return s->fail("Warteschlange wurde geändert; bitte erneut synchronisieren.");return 1;
}
// Apply one complete server snapshot under the same write transaction that
// checks the outbox and previous baseline. Trigger suppression is transactional.
// Show the first queued room and its full current local state. The maximum
// sequence detects new edits while a person is reviewing the conflict.
const char* aura_local_room_sync_conflict(void* handle) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage"))return nullptr;
    sqlite3* db=nullptr;if(sqlite3_open_v2(s->path.c_str(),&db,SQLITE_OPEN_READONLY|SQLITE_OPEN_FULLMUTEX,nullptr)!=SQLITE_OK){if(db)sqlite3_close(db);s->fail("Synchronisationsdatenbank nicht verfügbar.");return nullptr;}sqlite3_busy_timeout(db,5000);sqlite3_stmt* st=nullptr;
    const char* sql="SELECT json_object('serverUrl',c.server_url,'actor',c.server_actor,'operationId',o.operation_id,'lastSeq',(SELECT max(seq) FROM linux_room_sync_outbox WHERE room_id=o.room_id),'count',(SELECT count(*) FROM linux_room_sync_outbox WHERE room_id=o.room_id),'local',json_object('id',l.external_id,'name',r.name,'location',r.location,'capacity',r.capacity,'equipment',r.equipment,'active',json(CASE WHEN r.active THEN 'true' ELSE 'false' END))) FROM linux_room_sync_outbox o JOIN rooms r ON r.id=o.room_id JOIN sync_object_links l ON l.object_type='room' AND l.local_id=r.id CROSS JOIN linux_room_sync_config c WHERE c.singleton=1 ORDER BY o.seq LIMIT 1";
    bool ok=sqlite3_prepare_v2(db,sql,-1,&st,nullptr)==SQLITE_OK;int step=ok?sqlite3_step(st):SQLITE_ERROR;if(step==SQLITE_ROW){const auto* value=sqlite3_column_text(st,0);if(value)s->result=reinterpret_cast<const char*>(value);else ok=false;}else if(step==SQLITE_DONE)s->result="null";else ok=false;
    if(st)sqlite3_finalize(st);sqlite3_close(db);if(!ok){s->fail("Raumkonflikt konnte nicht gelesen werden.");return nullptr;}return s->result.c_str();
}
// Keep the reviewed local state but give it a new immutable operation ID and
// the reviewed server base. Preserve the replaced operations in an audit table.
int aura_local_room_sync_resolve(void* handle,const char* operation,int64_t lastSeq,const char* localJson,const char* remoteJson,const char* url,int64_t actor,int acceptServer) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage"))return 0;
    if(!operation||!*operation||!localJson||!remoteJson||!url||actor<=0||lastSeq<=0||(acceptServer!=0&&acceptServer!=1))return s->fail("Ungültige Konfliktauflösung.");
    sqlite3* db=nullptr;if(sqlite3_open_v2(s->path.c_str(),&db,SQLITE_OPEN_READWRITE|SQLITE_OPEN_FULLMUTEX,nullptr)!=SQLITE_OK){if(db)sqlite3_close(db);return s->fail("Synchronisationsdatenbank nicht verfügbar.");}sqlite3_busy_timeout(db,5000);sqlite3_stmt* st=nullptr;int64_t room=0,first=0;bool ok=sqlite3_exec(db,"BEGIN IMMEDIATE",nullptr,nullptr,nullptr)==SQLITE_OK;
    const char* validate="SELECT o.room_id,o.seq FROM linux_room_sync_outbox o JOIN rooms r ON r.id=o.room_id JOIN sync_object_links l ON l.object_type='room' AND l.local_id=r.id JOIN linux_room_sync_config c ON c.singleton=1 WHERE o.seq=(SELECT min(seq) FROM linux_room_sync_outbox) AND o.operation_id=? AND (SELECT max(seq) FROM linux_room_sync_outbox WHERE room_id=o.room_id)=? AND c.server_url=? AND c.server_actor=? AND json_valid(?) AND json_valid(?) AND json(?)=json_object('id',l.external_id,'name',r.name,'location',r.location,'capacity',r.capacity,'equipment',r.equipment,'active',json(CASE WHEN r.active THEN 'true' ELSE 'false' END)) AND json_type(?,'$.id')='text' AND json_extract(?,'$.id')=l.external_id AND json_type(?,'$.name')='text' AND length(json_extract(?,'$.name'))>0 AND json_type(?,'$.location')='text' AND length(json_extract(?,'$.location'))>0 AND json_type(?,'$.capacity')='integer' AND json_extract(?,'$.capacity') BETWEEN 1 AND 2147483647 AND json_type(?,'$.equipment')='text' AND json_type(?,'$.active') IN ('true','false')";
    if(ok)ok=sqlite3_prepare_v2(db,validate,-1,&st,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_text(st,1,operation,-1,SQLITE_TRANSIENT);sqlite3_bind_int64(st,2,lastSeq);sqlite3_bind_text(st,3,url,-1,SQLITE_TRANSIENT);sqlite3_bind_int64(st,4,actor);sqlite3_bind_text(st,5,localJson,-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,6,remoteJson,-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,7,localJson,-1,SQLITE_TRANSIENT);for(int i=8;i<=17;i++)sqlite3_bind_text(st,i,remoteJson,-1,SQLITE_TRANSIENT);ok=sqlite3_step(st)==SQLITE_ROW;if(ok){room=sqlite3_column_int64(st,0);first=sqlite3_column_int64(st,1);}}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok)ok=sqlite3_prepare_v2(db,"INSERT INTO linux_room_sync_resolutions(room_id,actor,local_json,remote_json,previous_operations) SELECT ?,?,?,?,json_group_array(json_object('operationId',operation_id,'action',action,'room',json(room_json),'expected',json(expected_json))) FROM (SELECT operation_id,action,room_json,expected_json FROM linux_room_sync_outbox WHERE room_id=? ORDER BY seq)",-1,&st,nullptr)==SQLITE_OK;
    if(ok){auto name=utf8(s->actor);sqlite3_bind_int64(st,1,room);sqlite3_bind_text(st,2,name.c_str(),-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,3,localJson,-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,4,remoteJson,-1,SQLITE_TRANSIENT);sqlite3_bind_int64(st,5,room);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok){auto resolution=sqlite3_last_insert_rowid(db);ok=sqlite3_prepare_v2(db,"INSERT INTO linux_room_sync_resolution_details(resolution_id,choice,actor_employee_id,identity_mapping) VALUES(?,?,?,0)",-1,&st,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(st,1,resolution);sqlite3_bind_text(st,2,acceptServer?"server":"local",-1,SQLITE_STATIC);sqlite3_bind_int64(st,3,s->employee);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;}
    if(ok && !acceptServer)ok=sqlite3_prepare_v2(db,"UPDATE linux_room_sync_outbox SET operation_id=lower(hex(randomblob(16))),action=CASE WHEN json_extract(?,'$.active')=1 AND json_extract(?,'$.active')=0 THEN 'archive' ELSE 'update' END,room_json=json(?),expected_json=json(?) WHERE seq=? AND operation_id=?",-1,&st,nullptr)==SQLITE_OK;
    if(ok && !acceptServer){sqlite3_bind_text(st,1,remoteJson,-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,2,localJson,-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,3,localJson,-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,4,remoteJson,-1,SQLITE_TRANSIENT);sqlite3_bind_int64(st,5,first);sqlite3_bind_text(st,6,operation,-1,SQLITE_TRANSIENT);ok=sqlite3_step(st)==SQLITE_DONE && sqlite3_changes(db)==1;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok && acceptServer)ok=sqlite3_exec(db,"UPDATE linux_room_sync_guard SET suppressed=1 WHERE singleton=1",nullptr,nullptr,nullptr)==SQLITE_OK;
    if(ok && acceptServer)ok=sqlite3_prepare_v2(db,"UPDATE rooms SET name=json_extract(?,'$.name'),location=json_extract(?,'$.location'),capacity=json_extract(?,'$.capacity'),equipment=json_extract(?,'$.equipment'),active=json_extract(?,'$.active'),updated_at=CURRENT_TIMESTAMP WHERE id=?",-1,&st,nullptr)==SQLITE_OK;
    if(ok && acceptServer){for(int i=1;i<=5;++i)sqlite3_bind_text(st,i,remoteJson,-1,SQLITE_TRANSIENT);sqlite3_bind_int64(st,6,room);ok=sqlite3_step(st)==SQLITE_DONE && sqlite3_changes(db)==1;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok && acceptServer)ok=sqlite3_prepare_v2(db,"INSERT INTO linux_room_sync_shadow(room_id,room_json) VALUES(?,json(?)) ON CONFLICT(room_id) DO UPDATE SET room_json=excluded.room_json",-1,&st,nullptr)==SQLITE_OK;
    if(ok && acceptServer){sqlite3_bind_int64(st,1,room);sqlite3_bind_text(st,2,remoteJson,-1,SQLITE_TRANSIENT);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok)ok=sqlite3_prepare_v2(db,acceptServer?"DELETE FROM linux_room_sync_outbox WHERE room_id=?":"DELETE FROM linux_room_sync_outbox WHERE room_id=? AND seq<>?",-1,&st,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(st,1,room);if(!acceptServer)sqlite3_bind_int64(st,2,first);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok && acceptServer)ok=sqlite3_exec(db,"UPDATE linux_room_sync_guard SET suppressed=0 WHERE singleton=1",nullptr,nullptr,nullptr)==SQLITE_OK;
    if(ok)ok=sqlite3_prepare_v2(db,"INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)",-1,&st,nullptr)==SQLITE_OK;if(ok){auto name=utf8(s->actor);auto id=std::to_string(room);sqlite3_bind_text(st,1,name.c_str(),-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,2,acceptServer?"rooms.sync.accepted_server":"rooms.sync.rebased",-1,SQLITE_STATIC);sqlite3_bind_text(st,3,id.c_str(),-1,SQLITE_TRANSIENT);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);
    if(ok)ok=sqlite3_exec(db,"COMMIT",nullptr,nullptr,nullptr)==SQLITE_OK;if(!ok)sqlite3_exec(db,"ROLLBACK",nullptr,nullptr,nullptr);sqlite3_close(db);if(!ok)return s->fail("Konflikt hat sich geändert oder konnte nicht sicher aufgelöst werden. Bitte erneut prüfen.");return 1;
}
int aura_local_room_sync_rebase(void* handle,const char* operation,int64_t lastSeq,const char* localJson,const char* remoteJson,const char* url,int64_t actor) {
    return aura_local_room_sync_resolve(handle,operation,lastSeq,localJson,remoteJson,url,actor,0);
}
int aura_local_room_sync_apply(void* handle,const char* snapshot) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage"))return -1;
    if(!snapshot || strlen(snapshot)>20*1024*1024){s->fail("Ungültige Raumliste.");return -1;}
    sqlite3* db=nullptr;if(sqlite3_open_v2(s->path.c_str(),&db,SQLITE_OPEN_READWRITE|SQLITE_OPEN_FULLMUTEX,nullptr)!=SQLITE_OK){if(db)sqlite3_close(db);s->fail("Synchronisationsdatenbank nicht verfügbar.");return -1;}
    sqlite3_busy_timeout(db,5000);bool ok=sqlite3_exec(db,"BEGIN IMMEDIATE",nullptr,nullptr,nullptr)==SQLITE_OK;std::string error="Raumabgleich fehlgeschlagen.";int changed=0;sqlite3_stmt* st=nullptr;
    if(ok)ok=sqlite3_prepare_v2(db,"SELECT (SELECT count(*) FROM linux_room_sync_outbox),(SELECT count(*) FROM linux_room_sync_config),json_valid(?),CASE WHEN json_valid(?) THEN json_type(?) ELSE '' END",-1,&st,nullptr)==SQLITE_OK;
    if(ok){for(int i=1;i<=3;i++)sqlite3_bind_text(st,i,snapshot,-1,SQLITE_TRANSIENT);ok=sqlite3_step(st)==SQLITE_ROW && sqlite3_column_int(st,0)==0 && sqlite3_column_int(st,1)==1 && sqlite3_column_int(st,2)==1 && sqlite3_column_text(st,3) && std::string(reinterpret_cast<const char*>(sqlite3_column_text(st,3)))=="array";if(!ok)error="Raumabgleich angehalten: Lokale Änderungen stehen aus oder die Serverliste ist ungültig.";}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok)ok=sqlite3_exec(db,"UPDATE linux_room_sync_guard SET suppressed=1 WHERE singleton=1",nullptr,nullptr,nullptr)==SQLITE_OK;
    const char* query="SELECT json_type(value,'$.id'),json_type(value,'$.name'),json_type(value,'$.location'),json_type(value,'$.capacity'),json_type(value,'$.equipment'),json_type(value,'$.active'),json_extract(value,'$.id'),json_extract(value,'$.name'),json_extract(value,'$.location'),json_extract(value,'$.capacity'),json_extract(value,'$.equipment'),json_extract(value,'$.active'),json_object('id',json_extract(value,'$.id'),'name',json_extract(value,'$.name'),'location',json_extract(value,'$.location'),'capacity',json_extract(value,'$.capacity'),'equipment',json_extract(value,'$.equipment'),'active',json(CASE WHEN json_extract(value,'$.active') THEN 'true' ELSE 'false' END)) FROM json_each(?)";
    if(ok)ok=sqlite3_prepare_v2(db,query,-1,&st,nullptr)==SQLITE_OK;if(ok)sqlite3_bind_text(st,1,snapshot,-1,SQLITE_TRANSIENT);std::unordered_set<std::string> seen;
    while(ok){int step=sqlite3_step(st);if(step==SQLITE_DONE)break;if(step!=SQLITE_ROW || seen.size()>=100000){ok=false;break;}
        auto textcol=[&](int i)->std::string {const auto* p=sqlite3_column_text(st,i);return p?reinterpret_cast<const char*>(p):"";};
        const std::string types[]={textcol(0),textcol(1),textcol(2),textcol(3),textcol(4),textcol(5)};
        std::string id=textcol(6),name=textcol(7),location=textcol(8),equipment=textcol(10),remote=textcol(12);int64_t capacity=sqlite3_column_int64(st,9);int active=sqlite3_column_int(st,11);
        if(types[0]!="text"||types[1]!="text"||types[2]!="text"||types[3]!="integer"||types[4]!="text"||(types[5]!="true"&&types[5]!="false")||id.empty()||name.empty()||location.empty()||capacity<=0||capacity>2147483647||sqlite3_column_bytes(st,6)!=static_cast<int>(id.size())||sqlite3_column_bytes(st,7)!=static_cast<int>(name.size())||sqlite3_column_bytes(st,8)!=static_cast<int>(location.size())||sqlite3_column_bytes(st,10)!=static_cast<int>(equipment.size())||!seen.insert(id).second){ok=false;error="Ungültiger oder doppelter Raum im Serverbestand.";break;}
        sqlite3_stmt* q=nullptr;int64_t local=0;std::string current,shadow;bool hasShadow=false;
        const char* lookup="SELECT r.id,json_object('id',l.external_id,'name',r.name,'location',r.location,'capacity',r.capacity,'equipment',r.equipment,'active',json(CASE WHEN r.active THEN 'true' ELSE 'false' END)),s.room_json FROM sync_object_links l JOIN rooms r ON r.id=l.local_id LEFT JOIN linux_room_sync_shadow s ON s.room_id=r.id WHERE l.object_type='room' AND l.external_id=?";
        ok=sqlite3_prepare_v2(db,lookup,-1,&q,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_text(q,1,id.c_str(),-1,SQLITE_TRANSIENT);int x=sqlite3_step(q);if(x==SQLITE_ROW){local=sqlite3_column_int64(q,0);const auto* c=sqlite3_column_text(q,1);const auto* b=sqlite3_column_text(q,2);if(c)current=reinterpret_cast<const char*>(c);else ok=false;if(b){shadow=reinterpret_cast<const char*>(b);hasShadow=true;}}else if(x!=SQLITE_DONE)ok=false;}if(q)sqlite3_finalize(q);q=nullptr;
        if(ok && local && ((hasShadow && current!=shadow)||(!hasShadow && current!=remote))){ok=false;error="Raumkonflikt: Lokaler Stand hat keinen passenden gemeinsamen Ausgangszustand: "+name;break;}
        if(ok && !local){ok=sqlite3_prepare_v2(db,"INSERT INTO rooms(name,location,capacity,equipment,active) VALUES(?,?,?,?,?)",-1,&q,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_text(q,1,name.c_str(),-1,SQLITE_TRANSIENT);sqlite3_bind_text(q,2,location.c_str(),-1,SQLITE_TRANSIENT);sqlite3_bind_int64(q,3,capacity);sqlite3_bind_text(q,4,equipment.c_str(),-1,SQLITE_TRANSIENT);sqlite3_bind_int(q,5,active);ok=sqlite3_step(q)==SQLITE_DONE;}if(q)sqlite3_finalize(q);q=nullptr;if(!ok){error="Serverraum konnte nicht angelegt werden; möglicherweise existiert derselbe Name lokal: "+name;break;}local=sqlite3_last_insert_rowid(db);
            ok=sqlite3_prepare_v2(db,"INSERT INTO sync_object_links(object_type,external_id,local_id) VALUES('room',?,?)",-1,&q,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_text(q,1,id.c_str(),-1,SQLITE_TRANSIENT);sqlite3_bind_int64(q,2,local);ok=sqlite3_step(q)==SQLITE_DONE;}if(q)sqlite3_finalize(q);q=nullptr;if(ok)++changed;
        } else if(ok && current!=remote){ok=sqlite3_prepare_v2(db,"UPDATE rooms SET name=?,location=?,capacity=?,equipment=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",-1,&q,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_text(q,1,name.c_str(),-1,SQLITE_TRANSIENT);sqlite3_bind_text(q,2,location.c_str(),-1,SQLITE_TRANSIENT);sqlite3_bind_int64(q,3,capacity);sqlite3_bind_text(q,4,equipment.c_str(),-1,SQLITE_TRANSIENT);sqlite3_bind_int(q,5,active);sqlite3_bind_int64(q,6,local);ok=sqlite3_step(q)==SQLITE_DONE && sqlite3_changes(db)==1;}if(q)sqlite3_finalize(q);q=nullptr;if(!ok){error="Serverraum konnte nicht aktualisiert werden: "+name;break;}++changed;}
        if(ok)ok=sqlite3_prepare_v2(db,"INSERT INTO linux_room_sync_shadow(room_id,room_json) VALUES(?,?) ON CONFLICT(room_id) DO UPDATE SET room_json=excluded.room_json",-1,&q,nullptr)==SQLITE_OK;if(ok){sqlite3_bind_int64(q,1,local);sqlite3_bind_text(q,2,remote.c_str(),-1,SQLITE_TRANSIENT);ok=sqlite3_step(q)==SQLITE_DONE;}if(q)sqlite3_finalize(q);
    }
    if(st)sqlite3_finalize(st);st=nullptr;
    if(ok)ok=sqlite3_prepare_v2(db,"SELECT l.external_id FROM linux_room_sync_shadow s JOIN sync_object_links l ON l.object_type='room' AND l.local_id=s.room_id",-1,&st,nullptr)==SQLITE_OK;
    while(ok){int step=sqlite3_step(st);if(step==SQLITE_DONE)break;const auto* id=step==SQLITE_ROW?sqlite3_column_text(st,0):nullptr;if(!id || !seen.count(reinterpret_cast<const char*>(id))){ok=false;error="Ein zuvor synchronisierter Raum fehlt auf dem Server; es wurde nichts übernommen.";}}if(st)sqlite3_finalize(st);st=nullptr;
    if(ok)ok=sqlite3_exec(db,"UPDATE linux_room_sync_guard SET suppressed=0 WHERE singleton=1",nullptr,nullptr,nullptr)==SQLITE_OK;
    if(ok){ok=sqlite3_prepare_v2(db,"INSERT INTO audit_log(actor,action,subject) VALUES(?,'rooms.sync.pulled',?)",-1,&st,nullptr)==SQLITE_OK;if(ok){auto actor=utf8(s->actor);auto count=std::to_string(changed);sqlite3_bind_text(st,1,actor.c_str(),-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,2,count.c_str(),-1,SQLITE_TRANSIENT);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);}
    if(ok)ok=sqlite3_exec(db,"COMMIT",nullptr,nullptr,nullptr)==SQLITE_OK;if(!ok)sqlite3_exec(db,"ROLLBACK",nullptr,nullptr,nullptr);sqlite3_close(db);if(!ok){s->fail(error);return -1;}return changed;
}
const char* aura_local_audit(void* handle,int limit) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage"))return nullptr;
    if(limit<1||limit>1000){s->fail("Ungültige Anzahl von Audit-Einträgen.");return nullptr;}
    try {std::ostringstream out;out << '[';bool first=true;
        for(const auto& entry:s->db.auditLog(limit)){if(!first)out << ',';first=false;out << quote(entry);}
        out << ']';s->result=out.str();return s->result.c_str();
    }catch(...){s->fail("Audit konnte nicht geladen werden.");return nullptr;}
}
int aura_local_assign_role(void* handle,int64_t employee_id,const char* code) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage"))return 0;
    try {const auto role=widen(code);
        if(employee_id==s->employee && role!=L"admin")return s->fail("Die eigene administrative Rolle kann nicht entfernt werden.");
        return s->db.assignRole(employee_id,role,s->actor)?1:s->dbfail();
    }catch(...){return s->fail("Rolle konnte nicht zugewiesen werden.");}
}
const char* aura_local_accounts(void* handle) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage"))return nullptr;
    try {std::unordered_map<int64_t,std::wstring> names;for(const auto& e:s->db.employees(true))names[e.id]=e.name;
        std::ostringstream out;out << '[';bool first=true;
        for(const auto& a:s->db.accounts(true)){if(!first)out << ',';first=false;
            out << "{\"employeeId\":" << a.employeeId << ",\"name\":" << quote(names[a.employeeId])
                << ",\"username\":" << quote(a.username) << ",\"enabled\":" << (a.enabled?"true":"false")
                << ",\"mustChangePassword\":" << (a.mustChangePassword?"true":"false")
                << ",\"lockedUntil\":" << a.lockedUntil << ",\"isSelf\":" << (a.employeeId==s->employee?"true":"false") << '}';}
        out << ']';s->result=out.str();return s->result.c_str();
    }catch(...){s->fail("Konten konnten nicht geladen werden.");return nullptr;}
}
int aura_local_account_create(void* handle,int64_t employee_id,const char* username,const char* password) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage"))return 0;
    try {for(const auto& account:s->db.accounts(true))if(account.employeeId==employee_id)return s->fail("Mitarbeiter hat bereits ein Konto.");
        return s->db.createOrResetAccount(employee_id,widen(username),widen(password),true,s->actor)?1:s->dbfail();
    }catch(...){return s->fail("Konto konnte nicht angelegt werden.");}
}
int aura_local_account_reset(void* handle,int64_t employee_id,const char* password) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage"))return 0;
    if(employee_id==s->employee)return s->fail("Das eigene Passwort über Passwort ändern aktualisieren.");
    try {for(const auto& account:s->db.accounts(true))if(account.employeeId==employee_id) {
            if(!account.enabled)return s->fail("Bitte das deaktivierte Konto zuerst aktivieren.");
            return s->db.createOrResetAccount(employee_id,account.username,widen(password),true,s->actor)?1:s->dbfail();
        }
        return s->fail("Konto nicht gefunden.");
    }catch(...){return s->fail("Passwort konnte nicht zurückgesetzt werden.");}
}
int aura_local_account_enable(void* handle,int64_t employee_id,int enabled) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"admin.manage"))return 0;
    if((enabled!=0&&enabled!=1)||(!enabled&&employee_id==s->employee))return s->fail("Das eigene Konto kann nicht deaktiviert werden.");
    try{return s->db.setAccountEnabled(employee_id,enabled!=0,s->actor)?1:s->dbfail();}
    catch(...){return s->fail("Kontostatus konnte nicht geändert werden.");}
}
const char* aura_local_organizations(void* handle) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"employees.manage"))return nullptr;
    try {std::ostringstream out;out << '[';bool first=true;
        for(const auto& unit:s->db.organizationUnits()){if(!first)out << ',';first=false;
            out << "{\"id\":" << unit.id << ",\"name\":" << quote(unit.name) << ",\"code\":" << quote(unit.code) << ",\"managerId\":" << unit.managerId << '}';}
        out << ']';s->result=out.str();return s->result.c_str();
    }catch(...){s->fail("Organisation konnte nicht geladen werden.");return nullptr;}
}
int aura_local_organization_save(void* handle,int64_t id,const char* name,const char* code,int64_t manager) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"employees.manage"))return 0;
    try {bool valid=(manager==0);for(const auto& employee:s->db.employees())if(employee.id==manager)valid=true;
        if(!valid)return s->fail("Leitung muss ein aktiver Mitarbeiter sein.");
        EnterpriseOrganizationUnit unit{id,widen(name),widen(code),manager,true};int64_t created=0;
        bool ok=id==0?s->db.createOrganizationUnit(unit,created,s->actor):s->db.updateOrganizationUnit(unit,s->actor);return ok?1:s->dbfail();
    }catch(...){return s->fail("Organisation konnte nicht gespeichert werden.");}
}
int aura_local_organization_archive(void* handle,int64_t id) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"employees.manage"))return 0;
    try{return s->db.archiveOrganizationUnit(id,s->actor)?1:s->dbfail();}catch(...){return s->fail("Organisation konnte nicht archiviert werden.");}
}
const char* aura_local_approvals(void* handle) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->active())return nullptr;
    if(!s->db.hasPermission(s->employee,L"tasks.read")&&!s->db.hasPermission(s->employee,L"tasks.manage")){s->fail("Für diese Aktion fehlt die Berechtigung.");return nullptr;}
    try {std::unordered_map<int64_t,std::wstring> titles;for(const auto& task:s->db.tasks(true))titles[task.id]=task.title;
        std::unordered_map<int64_t,std::wstring> names;for(const auto& employee:s->db.employees(true))names[employee.id]=employee.name;
        std::ostringstream out;out << '[';bool first=true;
        for(const auto& a:s->db.taskApprovals(s->employee,true)){if(!first)out << ',';first=false;
            out << "{\"id\":" << a.id << ",\"taskId\":" << a.taskId << ",\"taskTitle\":" << quote(titles[a.taskId])
                << ",\"requestedBy\":" << a.requestedBy << ",\"requesterName\":" << quote(names[a.requestedBy])
                << ",\"approverId\":" << a.approverId << ",\"status\":" << quote(a.status) << '}';}
        out << ']';s->result=out.str();return s->result.c_str();
    }catch(...){s->fail("Freigaben konnten nicht geladen werden.");return nullptr;}
}
int aura_local_approval_create(void* handle,int64_t task_id,int64_t approver_id) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"tasks.manage"))return 0;
    try {int64_t id=0;return s->db.createTaskApproval(task_id,s->employee,approver_id,id,s->actor)?1:s->dbfail();}
    catch(...){return s->fail("Freigabe konnte nicht angefordert werden.");}
}
int aura_local_approval_decide(void* handle,int64_t id,int approved) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->active())return 0;
    if(!s->db.hasPermission(s->employee,L"tasks.read")&&!s->db.hasPermission(s->employee,L"tasks.manage"))return s->fail("Für diese Aktion fehlt die Berechtigung.");
    if(approved!=0&&approved!=1)return s->fail("Ungültige Entscheidung.");
    try{return s->db.decideTaskApproval(id,approved!=0,s->employee,s->actor)?1:s->dbfail();}
    catch(...){return s->fail("Freigabe konnte nicht entschieden werden.");}
}
const char* aura_local_employees(void* handle) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->active())return nullptr;
    try {std::ostringstream out;out << '[';bool first=true;
        for(const auto& e:s->db.employees()) {if(!first)out << ',';first=false;
            const auto roles=s->db.rolesForEmployee(e.id);
            out << "{\"id\":" << e.id << ",\"name\":" << quote(e.name) << ",\"role\":" << quote(e.role)
                << ",\"department\":" << quote(e.department) << ",\"presence\":" << quote(e.presence)
                << ",\"systemRole\":" << quote(roles.empty()?L"":roles.front())
                << ",\"isSelf\":" << (e.id==s->employee?"true":"false") << '}';}
        out << ']';s->result=out.str();return s->result.c_str();
    }catch(...){s->fail("Mitarbeiter konnten nicht geladen werden.");return nullptr;}
}
int aura_local_employee_save(void* handle,int64_t id,const char* name,const char* role,const char* department) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"employees.manage"))return 0;
    try {EnterpriseEmployee e{id,widen(name),widen(role),widen(department),L"office",true};int64_t created=0;
        bool ok=id==0?s->db.createEmployee(e,created,s->actor):s->db.updateEmployee(e,s->actor);
        if(ok && id==s->employee) { s->actor=e.name; s->db.setAuditActor(e.name); }
        return ok?1:s->dbfail();
    }catch(...){return s->fail("Mitarbeiter konnte nicht gespeichert werden.");}
}
int aura_local_employee_save_with_role(void* handle,int64_t id,const char* name,const char* role,const char* department,const char* system_role) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"employees.manage")||!s->permit(L"admin.manage"))return 0;
    try {const auto selected=widen(system_role);if(id==s->employee && selected!=L"admin")return s->fail("Die eigene administrative Rolle kann nicht entfernt werden.");
        EnterpriseEmployee e{id,widen(name),widen(role),widen(department),L"office",true};
        if(!s->db.saveEmployeeWithRole(e,selected,s->actor))return s->dbfail();
        if(id==s->employee){s->actor=e.name;s->db.setAuditActor(e.name);}return 1;
    }catch(...){return s->fail("Mitarbeiter und Systemrolle konnten nicht gespeichert werden.");}
}
int aura_local_employee_presence(void* handle,int64_t id,const char* presence) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->active())return 0;
    if(id!=s->employee&&!s->db.hasPermission(s->employee,L"employees.manage"))return s->fail("Für diese Aktion fehlt die Berechtigung.");
    try{return s->db.updatePresence(id,widen(presence),s->actor,s->employee)?1:s->dbfail();}catch(...){return s->fail("Präsenz konnte nicht geändert werden.");}
}
int aura_local_employee_archive(void* handle,int64_t id) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"employees.manage"))return 0;
    if(id==s->employee)return s->fail("Das eigene aktive Konto kann nicht archiviert werden.");
    try{return s->db.archiveEmployee(id,s->actor)?1:s->dbfail();}catch(...){return s->fail("Mitarbeiter konnte nicht archiviert werden.");}
}
const char* aura_local_leave(void* handle) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->active())return nullptr;
    try {const int64_t filter=(s->db.hasPermission(s->employee,L"employees.manage")||s->db.hasPermission(s->employee,L"leave.approve"))?0:s->employee;
        std::ostringstream out;out << '[';bool first=true;
        for(const auto& r:s->db.leaveRequests(filter)) {if(!first)out << ',';first=false;
            out << "{\"id\":" << r.id << ",\"employeeId\":" << r.employeeId << ",\"startsAt\":" << r.startsAt
                << ",\"endsAt\":" << r.endsAt << ",\"reason\":" << quote(r.reason) << ",\"status\":" << quote(r.status)
                << ",\"decidedBy\":" << r.decidedBy << '}';}
        out << ']';s->result=out.str();return s->result.c_str();
    }catch(...){s->fail("Urlaubsanträge konnten nicht geladen werden.");return nullptr;}
}
int aura_local_leave_create(void* handle,int64_t start,int64_t end,const char* reason) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->active())return 0;
    if(start<=0||end<=start)return s->fail("Ungültiger Zeitraum.");
    try {EnterpriseLeaveRequest request{0,s->employee,start,end,widen(reason),L"pending",0,true};int64_t id=0;
        return s->db.createLeaveRequest(request,id,s->actor)?1:s->dbfail();
    }catch(...){return s->fail("Urlaubsantrag konnte nicht angelegt werden.");}
}
int aura_local_leave_decide(void* handle,int64_t id,int approved) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->active())return 0;
    if(!s->db.hasPermission(s->employee,L"employees.manage")&&!s->db.hasPermission(s->employee,L"leave.approve"))return s->fail("Für diese Aktion fehlt die Berechtigung.");
    if(approved!=0&&approved!=1)return s->fail("Ungültige Entscheidung.");
    try{return s->db.decideLeaveRequest(id,approved!=0,s->employee,s->actor)?1:s->dbfail();}catch(...){return s->fail("Urlaubsantrag konnte nicht entschieden werden.");}
}
int aura_local_leave_decide_compared(void* handle,int64_t id,int approved,int64_t employee,int64_t start,int64_t end,const char* reason,int64_t decidedBy) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->active())return 0;
    if(!s->db.hasPermission(s->employee,L"employees.manage")&&!s->db.hasPermission(s->employee,L"leave.approve"))return s->fail("Für diese Aktion fehlt die Berechtigung.");
    if((approved!=0&&approved!=1)||employee<=0||start<=0||end<=start)return s->fail("Ungültige Entscheidung.");
    try { EnterpriseLeaveRequest expected{id,employee,start,end,widen(reason),L"pending",decidedBy,true}; return s->db.decideLeaveRequest(id,approved!=0,s->employee,s->actor,&expected)?1:s->dbfail(); }
    catch(...){return s->fail("Urlaubsantrag konnte nicht entschieden werden.");}
}
const char* aura_local_tickets(void* handle) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->active()) return nullptr;
    try {
        std::ostringstream out; out << '['; bool first=true;
        for(const auto& ticket:s->db.tickets()) {
            if(!first) out << ','; first=false;
            out << "{\"id\":" << ticket.id << ",\"title\":" << quote(ticket.title) << ",\"category\":" << quote(ticket.category)
                << ",\"priority\":" << quote(ticket.priority) << ",\"status\":" << quote(ticket.status)
                << ",\"assigneeId\":" << ticket.assigneeId << ",\"active\":true}";
        }
        out << ']'; s->result=out.str(); return s->result.c_str();
    } catch(...) { s->fail("Tickets konnten nicht geladen werden."); return nullptr; }
}
int aura_local_ticket_save(void* handle,int64_t id,const char* title,const char* category,const char* priority,int64_t assignee) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"tickets.manage")) return 0;
    try {
        bool activeAssignee=false;
        for(const auto& employee:s->db.employees()) if(employee.id==assignee) activeAssignee=true;
        if(!activeAssignee) return s->fail("Bitte einen aktiven Mitarbeiter zuweisen.");
        EnterpriseTicket ticket{id,widen(title),widen(category),widen(priority),L"open",assignee,true}; int64_t created=0;
        const bool ok=id==0 ? s->db.createTicket(ticket,created,s->actor) : s->db.updateTicket(ticket,s->actor);
        return ok ? 1 : s->dbfail();
    } catch(...) { return s->fail("Ticket konnte nicht gespeichert werden."); }
}
int aura_local_ticket_status(void* handle,int64_t id,const char* status) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"tickets.manage")) return 0;
    try {
        for(const auto& ticket:s->db.tickets()) if(ticket.id==id) {
            const std::string from=utf8(ticket.status);
            if(!status || aura_validate_workflow_transition("ticket",from.c_str(),status)!=0)
                return s->fail("Ungültiger Ticketstatuswechsel.");
            return s->db.updateTicketStatus(id,widen(status),s->actor) ? 1 : s->dbfail();
        }
        return s->fail("Ticket nicht gefunden oder archiviert.");
    } catch(...) { return s->fail("Ticketstatus konnte nicht geändert werden."); }
}
int aura_local_ticket_archive(void* handle,int64_t id) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"tickets.manage")) return 0;
    try { return s->db.archiveTicket(id,s->actor) ? 1 : s->dbfail(); }
    catch(...) { return s->fail("Ticket konnte nicht archiviert werden."); }
}
const char* aura_local_attachments_list(void* handle,const char* subject,int64_t id,bool archived) {
    auto* s=static_cast<Session*>(handle);if(!s)return nullptr;
    try {const auto type=widen(subject);if(type!=L"task"&&type!=L"ticket"){s->fail("Ungültiger Anhangsbezug.");return nullptr;}
        if(!s->active())return nullptr;
        const auto manage=type==L"task"?L"tasks.manage":L"tickets.manage";
        const auto read=type==L"task"?L"tasks.read":L"tickets.read";
        if(!s->db.hasPermission(s->employee,manage) && (archived || !s->db.hasPermission(s->employee,read))){s->fail("Für diese Aktion fehlt die Berechtigung.");return nullptr;}
        if(id<=0){s->fail("Ungültiger Anhangsbezug.");return nullptr;}
        std::ostringstream out;out << '[';bool first=true;
        for(const auto& a:s->db.attachmentsFor(type,id,archived)) {if(!first)out << ',';first=false;
            out << "{\"id\":" << a.id << ",\"name\":" << quote(a.originalName) << ",\"storedPath\":" << quote(a.storedPath)
                << ",\"sizeBytes\":" << a.sizeBytes << ",\"sha256\":" << quote(a.sha256) << ",\"uploadedBy\":" << a.uploadedBy
                << ",\"createdAt\":" << quote(a.createdAt) << ",\"active\":" << (a.active?"true":"false") << '}';}
        out << ']';s->result=out.str();return s->result.c_str();
    }catch(...){s->fail("Anhänge konnten nicht geladen werden.");return nullptr;}
}
const char* aura_local_attachments(void* handle,const char* subject,int64_t id) {return aura_local_attachments_list(handle,subject,id,false);}
const char* aura_local_attachments_all(void* handle,const char* subject,int64_t id) {return aura_local_attachments_list(handle,subject,id,true);}
int aura_local_attachment_create(void* handle,const char* subject,int64_t id,const char* name,const char* path,int64_t size,const char* sha) {
    auto* s=static_cast<Session*>(handle);if(!s)return 0;
    try {const auto type=widen(subject);if(type!=L"task"&&type!=L"ticket")return s->fail("Ungültiger Anhangsbezug.");
        if(!s->permitCollaboration(type))return 0;
        const auto hash=widen(sha);if(size<0||size>100LL*1024*1024||hash.size()!=64||hash.find_first_not_of(L"0123456789abcdef")!=std::wstring::npos)return s->fail("Ungültige Anhangsdaten.");
        EnterpriseAttachment a{0,type,id,widen(name),widen(path),size,hash,s->employee,L"",true};int64_t created=0;
        return s->db.createAttachment(a,created,s->actor)?1:s->dbfail();
    }catch(...){return s->fail("Anhang konnte nicht gespeichert werden.");}
}
int aura_local_attachment_archive(void* handle,const char* subject,int64_t id,int64_t attachment_id) {
    auto* s=static_cast<Session*>(handle);if(!s)return 0;
    try {const auto type=widen(subject);if(type!=L"task"&&type!=L"ticket")return s->fail("Ungültiger Anhangsbezug.");
        if(!s->permitCollaboration(type))return 0;
        bool belongs=false;for(const auto& a:s->db.attachmentsFor(type,id))if(a.id==attachment_id)belongs=true;
        if(!belongs)return s->fail("Anhang gehört nicht zu diesem Vorgang.");
        return s->db.archiveAttachment(attachment_id,s->actor,s->employee)?1:s->dbfail();
    }catch(...){return s->fail("Anhang konnte nicht archiviert werden.");}
}
int aura_local_attachment_restore(void* handle,const char* subject,int64_t id,int64_t attachment_id) {
    auto* s=static_cast<Session*>(handle);if(!s)return 0;
    try {const auto type=widen(subject);if(type!=L"task"&&type!=L"ticket")return s->fail("Ungültiger Anhangsbezug.");
        if(!s->permit(type==L"task"?L"tasks.manage":L"tickets.manage"))return 0;
        bool belongs=false;for(const auto& a:s->db.attachmentsFor(type,id,true))if(a.id==attachment_id&&!a.active)belongs=true;
        if(!belongs)return s->fail("Archivierter Anhang gehört nicht zu diesem Vorgang.");
        // Existing installations may have configured the outbox before the
        // restore trigger existed. Install it before changing the attachment.
        sqlite3* db=nullptr;if(sqlite3_open_v2(s->path.c_str(),&db,SQLITE_OPEN_READWRITE,nullptr)!=SQLITE_OK){if(db)sqlite3_close(db);return s->fail("Anhangsabgleich nicht verfügbar.");}
        sqlite3_busy_timeout(db,5000);const std::string prefix=type==L"task"?"linux_task_attachment_sync":"linux_ticket_attachment_sync";
        sqlite3_stmt* st=nullptr;bool ok=sqlite3_prepare_v2(db,"SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",-1,&st,nullptr)==SQLITE_OK;bool configured=false;
        if(ok){const std::string name=prefix+"_config";sqlite3_bind_text(st,1,name.c_str(),-1,SQLITE_TRANSIENT);const int step=sqlite3_step(st);configured=step==SQLITE_ROW;ok=step==SQLITE_ROW||step==SQLITE_DONE;}if(st)sqlite3_finalize(st);
        if(ok&&configured){
            const std::string guard="SELECT suppressed FROM "+prefix+"_guard WHERE singleton=1";st=nullptr;
            ok=sqlite3_prepare_v2(db,guard.c_str(),-1,&st,nullptr)==SQLITE_OK;
            if(ok)ok=sqlite3_step(st)==SQLITE_ROW&&sqlite3_column_int(st,0)==0;
            if(st)sqlite3_finalize(st);
        }
        if(ok&&configured){const std::string sql="CREATE TRIGGER IF NOT EXISTS "+prefix+"_restore AFTER UPDATE OF active ON attachments WHEN NEW.subject_type='"+(type==L"task"?std::string("task"):std::string("ticket"))+"' AND OLD.active=0 AND NEW.active=1 AND (SELECT suppressed FROM "+prefix+"_guard)=0 BEGIN INSERT INTO "+prefix+"_outbox(operation_id,action,attachment_id) VALUES(lower(hex(randomblob(16))),'restore',NEW.id); END";ok=sqlite3_exec(db,sql.c_str(),nullptr,nullptr,nullptr)==SQLITE_OK;}
        sqlite3_close(db);if(!ok)return s->fail("Anhangswarteschlange konnte nicht eingerichtet werden.");
        return s->db.restoreAttachment(attachment_id,s->actor)?1:s->dbfail();
    }catch(...){return s->fail("Anhang konnte nicht wiederhergestellt werden.");}
}
const char* aura_local_comments(void* handle,const char* subject,int64_t id) {
    auto* s=static_cast<Session*>(handle); if(!s) return nullptr;
    try {
        const auto type=widen(subject);
        if(type!=L"task" && type!=L"ticket") { s->fail("Ungültiger Kommentarbezug."); return nullptr; }
        if(!s->active())return nullptr;
        const auto manage=type==L"task"?L"tasks.manage":L"tickets.manage";
        const auto read=type==L"task"?L"tasks.read":L"tickets.read";
        if(!s->db.hasPermission(s->employee,manage)&&!s->db.hasPermission(s->employee,read)){s->fail("Für diese Aktion fehlt die Berechtigung.");return nullptr;}
        std::unordered_map<int64_t,std::wstring> names;
        for(const auto& employee:s->db.employees(true)) names[employee.id]=employee.name;
        std::ostringstream out; out << '['; bool first=true;
        for(const auto& c:s->db.commentsFor(type,id)) {
            if(!first) out << ','; first=false;
            out << "{\"id\":" << c.id << ",\"authorId\":" << c.authorId << ",\"author\":" << quote(names[c.authorId])
                << ",\"body\":" << quote(c.body) << ",\"createdAt\":" << quote(c.createdAt) << '}';
        }
        out << ']'; s->result=out.str(); return s->result.c_str();
    } catch(...) { s->fail("Kommentare konnten nicht geladen werden."); return nullptr; }
}
int aura_local_comment_add(void* handle,const char* subject,int64_t subject_id,const char* body) {
    auto* s=static_cast<Session*>(handle); if(!s) return 0;
    try {
        const auto type=widen(subject);
        if(type!=L"task" && type!=L"ticket") return s->fail("Ungültiger Kommentarbezug.");
        if(!s->permitCollaboration(type)) return 0;
        EnterpriseComment comment{0,type,subject_id,s->employee,widen(body),L""}; int64_t created=0;
        return s->db.createComment(comment,created,s->actor) ? 1 : s->dbfail();
    } catch(...) { return s->fail("Kommentar konnte nicht gespeichert werden."); }
}
const char* aura_local_assets(void* handle) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"tickets.manage")) return nullptr;
    try { std::ostringstream out; out << '['; bool first=true;
        for(const auto& a:s->db.facilityAssets()) {
            if(!first) out << ','; first=false;
            out << "{\"id\":" << a.id << ",\"name\":" << quote(a.name) << ",\"category\":" << quote(a.category)
                << ",\"location\":" << quote(a.location) << ",\"status\":" << quote(a.status) << '}';
        } out << ']';s->result=out.str();return s->result.c_str();
    } catch(...) {s->fail("Assets konnten nicht geladen werden.");return nullptr;}
}
int aura_local_asset_save(void* handle,int64_t id,const char* name,const char* category,const char* location,const char* status) {
    auto* s=static_cast<Session*>(handle);if(!s || !s->permit(L"tickets.manage"))return 0;
    try {EnterpriseFacilityAsset asset{id,widen(name),widen(category),widen(location),widen(status),true};int64_t created=0;
        bool ok=id==0?s->db.createFacilityAsset(asset,created,s->actor):s->db.updateFacilityAsset(asset,s->actor);
        return ok?1:s->dbfail();
    } catch(...) {return s->fail("Asset konnte nicht gespeichert werden.");}
}
int aura_local_asset_archive(void* handle,int64_t id) {
    auto* s=static_cast<Session*>(handle);if(!s || !s->permit(L"tickets.manage"))return 0;
    try{return s->db.archiveFacilityAsset(id,s->actor)?1:s->dbfail();}catch(...){return s->fail("Asset konnte nicht archiviert werden.");}
}
const char* aura_local_maintenance(void* handle,int64_t asset_id) {
    auto* s=static_cast<Session*>(handle);if(!s || !s->permit(L"tickets.manage"))return nullptr;
    if(asset_id<=0){s->fail("Ungültige Asset-ID.");return nullptr;}
    try {std::ostringstream out;out << '[';bool first=true;
        for(const auto& order:s->db.maintenanceOrders(asset_id)) {
            if(!first)out << ',';first=false;
            out << "{\"id\":" << order.id << ",\"assetId\":" << order.assetId << ",\"title\":" << quote(order.title)
                << ",\"status\":" << quote(order.status) << ",\"dueAt\":" << order.dueAt << ",\"recurrenceDays\":" << order.recurrenceDays << '}';
        }out << ']';s->result=out.str();return s->result.c_str();
    }catch(...){s->fail("Wartungsaufträge konnten nicht geladen werden.");return nullptr;}
}
int aura_local_maintenance_save(void* handle,int64_t asset_id,int64_t id,const char* title,const char* status,int64_t due,int recurrence) {
    auto* s=static_cast<Session*>(handle);if(!s || !s->permit(L"tickets.manage"))return 0;
    try {if(id!=0){bool belongs=false;for(const auto& order:s->db.maintenanceOrders(asset_id))if(order.id==id)belongs=true;
            if(!belongs)return s->fail("Wartungsauftrag gehört nicht zu diesem Asset.");}
        EnterpriseMaintenanceOrder order{id,asset_id,widen(title),widen(status),due,recurrence,true};int64_t created=0;
        bool ok=id==0?s->db.createMaintenanceOrder(order,created,s->actor):s->db.updateMaintenanceOrder(order,s->actor);
        return ok?1:s->dbfail();
    }catch(...){return s->fail("Wartungsauftrag konnte nicht gespeichert werden.");}
}
int aura_local_maintenance_status(void* handle,int64_t asset_id,int64_t id,const char* status) {
    auto* s=static_cast<Session*>(handle);if(!s || !s->permit(L"tickets.manage"))return 0;
    try {bool belongs=false;for(const auto& order:s->db.maintenanceOrders(asset_id))if(order.id==id)belongs=true;
        if(!belongs)return s->fail("Wartungsauftrag gehört nicht zu diesem Asset.");
        return s->db.updateMaintenanceOrderStatus(id,widen(status),s->actor)?1:s->dbfail();
    }catch(...){return s->fail("Wartungsstatus konnte nicht geändert werden.");}
}
int aura_local_maintenance_archive(void* handle,int64_t asset_id,int64_t id) {
    auto* s=static_cast<Session*>(handle);if(!s || !s->permit(L"tickets.manage"))return 0;
    try {bool belongs=false;for(const auto& order:s->db.maintenanceOrders(asset_id))if(order.id==id)belongs=true;
        if(!belongs)return s->fail("Wartungsauftrag gehört nicht zu diesem Asset.");
        return s->db.archiveMaintenanceOrder(id,s->actor)?1:s->dbfail();
    }catch(...){return s->fail("Wartungsauftrag konnte nicht archiviert werden.");}
}
const char* aura_local_projects(void* handle) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->active()) return nullptr;
    try {
        std::ostringstream out; out << '['; bool first=true;
        for(const auto& p:s->db.projects()) {
            if(!first) out << ','; first=false;
            out << "{\"id\":" << p.id << ",\"name\":" << quote(p.name) << ",\"department\":" << quote(p.department)
                << ",\"description\":" << quote(p.description) << ",\"status\":" << quote(p.status)
                << ",\"ownerId\":" << p.ownerId << ",\"active\":true}";
        }
        out << ']'; s->result=out.str(); return s->result.c_str();
    } catch(...) { s->fail("Projekte konnten nicht geladen werden."); return nullptr; }
}
const char* aura_local_projects_for_assignment(void* handle) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"tasks.manage"))return nullptr;
    try {
        std::ostringstream out;out << '[';bool first=true;
        for(const auto& p:s->db.projects(true)) {
            if(!first)out << ',';first=false;
            out << "{\"id\":" << p.id << ",\"name\":" << quote(p.name) << ",\"department\":" << quote(p.department) << ",\"active\":" << (p.active?"true":"false") << '}';
        }
        out << ']';s->result=out.str();return s->result.c_str();
    }catch(...){s->fail("Projekte konnten nicht geladen werden.");return nullptr;}
}
int aura_local_task_save(void* handle,int64_t id,const char* title,const char* department,const char* priority,int64_t assignee,int64_t due,int recurrence) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"tasks.manage")) return 0;
    try {
        EnterpriseTask t{id,widen(title),widen(department),widen(priority),L"open",assignee,true,due,recurrence}; int64_t created=0;
        if(assignee<0) return s->fail("Ungültige Mitarbeiter-ID.");
        const bool ok=id==0 ? s->db.createTask(t,created,s->actor) : s->db.updateTask(t,s->actor);
        return ok ? 1 : s->dbfail();
    } catch(...) { return s->fail("Aufgabe konnte nicht gespeichert werden."); }
}
int aura_local_task_create_in_project(void* handle,const char* title,const char* department,const char* priority,int64_t assignee,int64_t due,int recurrence,int64_t project) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"tasks.manage"))return 0;
    try {if(assignee<0||project<0)return s->fail("Ungültige Mitarbeiter- oder Projekt-ID.");
        EnterpriseTask task{0,widen(title),widen(department),widen(priority),L"open",assignee,true,due,recurrence};int64_t created=0;
        return s->db.createTask(task,created,s->actor,project)?1:s->dbfail();
    }catch(...){return s->fail("Aufgabe konnte nicht angelegt werden.");}
}
int aura_local_task_update_project(void* handle,int64_t id,const char* title,const char* department,const char* priority,int64_t assignee,int64_t due,int recurrence,int64_t expected,int64_t project) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"tasks.manage"))return 0;
    try {if(assignee<0||expected<0||project<0)return s->fail("Ungültige Mitarbeiter- oder Projekt-ID.");
        EnterpriseTask task{id,widen(title),widen(department),widen(priority),L"open",assignee,true,due,recurrence};
        return s->db.updateTask(task,s->actor,expected,project)?1:s->dbfail();
    }catch(...){return s->fail("Aufgabe und Projektzuordnung konnten nicht gespeichert werden.");}
}
int aura_local_task_status(void* handle,int64_t id,const char* status) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"tasks.manage")) return 0;
    try {
        for(const auto& t:s->db.tasks()) if(t.id==id) {
            const std::string from=utf8(t.status);
            if(!status || aura_validate_workflow_transition("task",from.c_str(),status)!=0)
                return s->fail("Ungültiger Aufgabenstatuswechsel.");
            return s->db.updateTaskStatus(id,widen(status),s->actor) ? 1 : s->dbfail();
        }
        return s->fail("Aufgabe nicht gefunden oder archiviert.");
    } catch(...) { return s->fail("Aufgabenstatus konnte nicht geändert werden."); }
}
int aura_local_task_archive(void* handle,int64_t id) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"tasks.manage")) return 0;
    try { return s->db.archiveTask(id,s->actor) ? 1 : s->dbfail(); }
    catch(...) { return s->fail("Aufgabe konnte nicht archiviert werden."); }
}
int aura_local_task_project(void* handle,int64_t task_id,int64_t project_id) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"tasks.manage")) return 0;
    try { return s->db.setTaskProject(task_id,project_id,s->actor) ? 1 : s->dbfail(); }
    catch(...) { return s->fail("Projektzuordnung konnte nicht geändert werden."); }
}
int aura_local_task_project_edge(void* handle,int64_t task_id,int64_t project_id,int expected,int present) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"tasks.manage"))return 0;
    if(task_id<=0||project_id<=0||(expected!=0&&expected!=1)||(present!=0&&present!=1)||expected==present)return s->fail("Ungültige Projektzuordnung.");
    sqlite3* db=nullptr;if(sqlite3_open_v2(s->path.c_str(),&db,SQLITE_OPEN_READWRITE|SQLITE_OPEN_FULLMUTEX,nullptr)!=SQLITE_OK){if(db)sqlite3_close(db);return s->fail("Datenbank nicht verfügbar.");}sqlite3_busy_timeout(db,5000);
    bool ok=sqlite3_exec(db,"BEGIN IMMEDIATE",nullptr,nullptr,nullptr)==SQLITE_OK;sqlite3_stmt* st=nullptr;
    const char* sql=present?"INSERT INTO project_tasks(project_id,task_id) SELECT ?,? WHERE EXISTS(SELECT 1 FROM projects WHERE id=? AND active=1) AND EXISTS(SELECT 1 FROM tasks WHERE id=? AND active=1) AND NOT EXISTS(SELECT 1 FROM project_tasks WHERE project_id=? AND task_id=?)":"DELETE FROM project_tasks WHERE project_id=? AND task_id=? AND EXISTS(SELECT 1 FROM tasks WHERE id=? AND active=1)";
    if(ok)ok=sqlite3_prepare_v2(db,sql,-1,&st,nullptr)==SQLITE_OK;
    if(ok){sqlite3_bind_int64(st,1,project_id);sqlite3_bind_int64(st,2,task_id);sqlite3_bind_int64(st,3,present?project_id:task_id);if(present){sqlite3_bind_int64(st,4,task_id);sqlite3_bind_int64(st,5,project_id);sqlite3_bind_int64(st,6,task_id);}ok=sqlite3_step(st)==SQLITE_DONE&&sqlite3_changes(db)==1;}
    if(st)sqlite3_finalize(st);st=nullptr;
    if(ok)ok=sqlite3_prepare_v2(db,"INSERT INTO audit_log(actor,action,subject) VALUES(?,?,?)",-1,&st,nullptr)==SQLITE_OK;
    if(ok){auto actor=utf8(s->actor);auto subject=std::to_string(project_id)+":"+std::to_string(task_id);sqlite3_bind_text(st,1,actor.c_str(),-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,2,present?"project.task.assigned":"project.task.removed",-1,SQLITE_STATIC);sqlite3_bind_text(st,3,subject.c_str(),-1,SQLITE_TRANSIENT);ok=sqlite3_step(st)==SQLITE_DONE;}if(st)sqlite3_finalize(st);
    if(ok)ok=sqlite3_exec(db,"COMMIT",nullptr,nullptr,nullptr)==SQLITE_OK;if(!ok)sqlite3_exec(db,"ROLLBACK",nullptr,nullptr,nullptr);sqlite3_close(db);
    return ok?1:s->fail("Projektzuordnung hat sich geändert oder konnte nicht gespeichert werden.");
}
const char* aura_local_dependencies(void* handle,int64_t task_id) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->active()) return nullptr;
    if(task_id<=0) { s->fail("Ungültige Aufgaben-ID."); return nullptr; }
    try {
        std::unordered_map<int64_t,std::wstring> names;
        for(const auto& task:s->db.tasks(true)) names[task.id]=task.title;
        std::ostringstream out; out << '['; bool first=true;
        for(const auto& dependency:s->db.taskDependencies(task_id)) {
            if(!first) out << ','; first=false;
            out << "{\"id\":" << dependency.dependsOnTaskId << ",\"title\":" << quote(names[dependency.dependsOnTaskId]) << '}';
        }
        out << ']'; s->result=out.str(); return s->result.c_str();
    } catch(...) { s->fail("Abhängigkeiten konnten nicht geladen werden."); return nullptr; }
}
int aura_local_dependency_change(void* handle,int64_t task_id,int64_t dependency_id,int add) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"tasks.manage")) return 0;
    try {
        const bool ok=add ? s->db.addTaskDependency(task_id,dependency_id,s->actor) : s->db.removeTaskDependency(task_id,dependency_id,s->actor);
        return ok ? 1 : s->dbfail();
    } catch(...) { return s->fail("Abhängigkeit konnte nicht geändert werden."); }
}
const char* aura_local_subtasks(void* handle,int64_t task_id) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->active()) return nullptr;
    if(task_id<=0) { s->fail("Ungültige Aufgaben-ID."); return nullptr; }
    try {
        std::ostringstream out; out << '['; bool first=true;
        for(const auto& sub:s->db.subtasks(task_id)) {
            if(!first) out << ','; first=false;
            out << "{\"id\":" << sub.id << ",\"taskId\":" << sub.taskId << ",\"title\":" << quote(sub.title)
                << ",\"status\":" << quote(sub.status) << ",\"assigneeId\":" << sub.assigneeId << ",\"dueAt\":" << sub.dueAt << '}';
        }
        out << ']'; s->result=out.str(); return s->result.c_str();
    } catch(...) { s->fail("Unteraufgaben konnten nicht geladen werden."); return nullptr; }
}
int aura_local_subtask_save(void* handle,int64_t task_id,int64_t id,const char* title,const char* status,int64_t assignee,int64_t due) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"tasks.manage")) return 0;
    if(task_id<=0 || assignee<0) return s->fail("Ungültige Aufgaben- oder Mitarbeiter-ID.");
    try {
        bool activeTask=false, activeAssignee=(assignee==0);
        for(const auto& task:s->db.tasks()) if(task.id==task_id) activeTask=true;
        for(const auto& employee:s->db.employees()) if(employee.id==assignee) activeAssignee=true;
        if(!activeTask || !activeAssignee) return s->fail("Aufgabe oder Mitarbeiter ist nicht aktiv.");
        if(id!=0) {
            bool belongs=false;
            for(const auto& sub:s->db.subtasks(task_id)) if(sub.id==id) belongs=true;
            if(!belongs) return s->fail("Unteraufgabe gehört nicht zu dieser Aufgabe.");
        }
        EnterpriseSubtask sub{id,task_id,widen(title),widen(status),assignee,due,true}; int64_t created=0;
        const bool ok=id==0 ? s->db.createSubtask(sub,created,s->actor) : s->db.updateSubtask(sub,s->actor);
        return ok ? 1 : s->dbfail();
    } catch(...) { return s->fail("Unteraufgabe konnte nicht gespeichert werden."); }
}
int aura_local_subtask_save_compared(void* handle,int64_t task_id,int64_t id,const char* title,const char* status,int64_t assignee,int64_t due,const char* old_title,const char* old_status,int64_t old_assignee,int64_t old_due) {
    auto* s=static_cast<Session*>(handle);if(!s||!s->permit(L"tasks.manage"))return 0;
    try {if(task_id<=0||id<=0||assignee<0||old_assignee<0)return s->fail("Ungültige Aufgaben- oder Mitarbeiter-ID.");
        bool activeTask=false,activeAssignee=(assignee==0);for(const auto& task:s->db.tasks())if(task.id==task_id)activeTask=true;for(const auto& employee:s->db.employees())if(employee.id==assignee)activeAssignee=true;
        if(!activeTask||!activeAssignee)return s->fail("Aufgabe oder Mitarbeiter ist nicht aktiv.");
        EnterpriseSubtask current{id,task_id,widen(title),widen(status),assignee,due,true};EnterpriseSubtask expected{id,task_id,widen(old_title),widen(old_status),old_assignee,old_due,true};
        return s->db.updateSubtask(current,s->actor,&expected)?1:s->dbfail();
    }catch(...){return s->fail("Unteraufgabe konnte nicht gespeichert werden.");}
}
int aura_local_subtask_archive(void* handle,int64_t task_id,int64_t id) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"tasks.manage")) return 0;
    try {
        bool activeTask=false;
        for(const auto& task:s->db.tasks()) if(task.id==task_id) activeTask=true;
        if(!activeTask) return s->fail("Aufgabe ist nicht aktiv.");
        bool belongs=false;
        for(const auto& sub:s->db.subtasks(task_id)) if(sub.id==id) belongs=true;
        if(!belongs) return s->fail("Unteraufgabe gehört nicht zu dieser Aufgabe.");
        return s->db.archiveSubtask(id,s->actor) ? 1 : s->dbfail();
    } catch(...) { return s->fail("Unteraufgabe konnte nicht archiviert werden."); }
}
int aura_local_subtask_archive_compared(void* handle,int64_t task_id,int64_t id,const char* title,const char* status,int64_t assignee,int64_t due) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"tasks.manage")) return 0;
    try { if(task_id<=0 || id<=0 || assignee<0) return s->fail("Ungültige Aufgaben- oder Mitarbeiter-ID.");
        EnterpriseSubtask expected{id,task_id,widen(title),widen(status),assignee,due,true};
        return s->db.archiveSubtask(id,s->actor,&expected)?1:s->dbfail();
    } catch(...) { return s->fail("Unteraufgabe konnte nicht archiviert werden."); }
}
int aura_local_project_save(void* handle,int64_t id,const char* name,const char* department,const char* description,const char* status,int64_t owner) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"tasks.manage")) return 0;
    try {
        EnterpriseProject p{id,widen(name),widen(department),widen(description),widen(status),owner,true}; int64_t created=0;
        if(owner<0) return s->fail("Ungültige Mitarbeiter-ID.");
        const bool ok=id==0 ? s->db.createProject(p,created,s->actor) : s->db.updateProject(p,s->actor);
        return ok ? 1 : s->dbfail();
    } catch(...) { return s->fail("Projekt konnte nicht gespeichert werden."); }
}
int aura_local_project_status(void* handle,int64_t id,const char* expected,const char* status) {
    auto* s=static_cast<Session*>(handle);if(!s || !s->permit(L"tasks.manage"))return 0;
    try {return s->db.changeProjectStatus(id,widen(expected),widen(status),s->actor)?1:s->dbfail();}
    catch(...) {return s->fail("Projektstatus konnte nicht geändert werden.");}
}
int aura_local_project_archive(void* handle,int64_t id) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"tasks.manage")) return 0;
    try { return s->db.archiveProject(id,s->actor) ? 1 : s->dbfail(); }
    catch(...) { return s->fail("Projekt konnte nicht archiviert werden."); }
}
// A local booking is never sent to the central server. A future connected
// workflow must reserve and commit centrally before writing local changes.
const char* aura_local_bookings(void* handle,int64_t room_id) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->active()) return nullptr;
    if(room_id<=0) { s->fail("Ungültiger Raum."); return nullptr; }
    try {
        std::ostringstream out; out << '['; bool first=true;
        for(const auto& b:s->db.bookingsForRoom(room_id,true)) {
            if(!first) out << ','; first=false;
            out << "{\"id\":" << b.id << ",\"roomId\":" << b.roomId << ",\"userId\":" << b.userId
                << ",\"startsAt\":" << b.startUtc << ",\"endsAt\":" << b.endUtc
                << ",\"attendees\":" << b.attendees << ",\"status\":" << quote(b.status) << '}';
        }
        out << ']'; s->result=out.str(); return s->result.c_str();
    } catch(...) { s->fail("Buchungen konnten nicht geladen werden."); return nullptr; }
}
int aura_local_book(void* handle,int64_t room_id,int64_t start,int64_t end,int attendees,int occurrences,int interval_days) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->active()) return 0;
    if(start<=0 || end<=start || end>253402300799LL || occurrences<1 || occurrences>104 || interval_days<1 || interval_days>366)
        return s->fail("Ungültiger Zeitraum oder Serienabstand.");
    try {
        EnterpriseBooking b{0,room_id,s->employee,start,end,attendees,L"confirmed"};
        if(occurrences==1) { int64_t id=0; return s->db.createBooking(b,id,s->actor,s->employee) ? 1 : s->dbfail(); }
        std::vector<int64_t> ids;
        return s->db.createRecurringBookings(b,interval_days,occurrences,ids,s->actor,s->employee) ? 1 : s->dbfail();
    } catch(...) { return s->fail("Buchung konnte nicht angelegt werden."); }
}
int aura_local_reschedule(void* handle,int64_t room_id,int64_t booking_id,int64_t start,int64_t end,int attendees) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->active()) return 0;
    if(start<=0 || end<=start || end>253402300799LL) return s->fail("Ungültiger Zeitraum.");
    try {
        for(const auto& old:s->db.bookingsForRoom(room_id,true)) if(old.id==booking_id && old.status==L"confirmed") {
            if(old.userId<=0)return s->fail("Diese zentrale Buchung hat keine zugeordnete Person und kann lokal noch nicht geändert werden.");
            if(old.userId!=s->employee && !s->db.hasPermission(s->employee,L"rooms.manage")) return s->fail("Keine Berechtigung für diese Buchung.");
            EnterpriseBooking updated{booking_id,room_id,old.userId,start,end,attendees,L"confirmed"};
            return s->db.rescheduleBooking(updated,s->actor,s->employee) ? 1 : s->dbfail();
        }
        return s->fail("Buchung nicht gefunden oder nicht mehr aktiv.");
    } catch(...) { return s->fail("Buchung konnte nicht geändert werden."); }
}
int aura_local_cancel_booking(void* handle,int64_t room_id,int64_t booking_id) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->active()) return 0;
    try {
        for(const auto& old:s->db.bookingsForRoom(room_id,true)) if(old.id==booking_id && old.status==L"confirmed") {
            if(old.userId<=0)return s->fail("Diese zentrale Buchung hat keine zugeordnete Person und kann lokal noch nicht geändert werden.");
            if(old.userId!=s->employee && !s->db.hasPermission(s->employee,L"rooms.manage")) return s->fail("Keine Berechtigung für diese Buchung.");
            return s->db.cancelBooking(booking_id,s->actor,s->employee) ? 1 : s->dbfail();
        }
        return s->fail("Buchung nicht gefunden oder nicht mehr aktiv.");
    } catch(...) { return s->fail("Buchung konnte nicht storniert werden."); }
}
int aura_local_block_room(void* handle,int64_t room_id,int64_t start,int64_t end,const char* reason) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"rooms.manage")) return 0;
    if(start<=0 || end<=start || end>253402300799LL) return s->fail("Ungültiger Zeitraum.");
    try { int64_t id=0; return s->db.blockRoom(room_id,start,end,widen(reason),id,s->actor,s->employee) ? 1 : s->dbfail(); }
    catch(...) { return s->fail("Sperrzeit konnte nicht angelegt werden."); }
}
int aura_local_room_archive(void* handle,int64_t id) {
    auto* s=static_cast<Session*>(handle); if(!s || !s->permit(L"rooms.manage")) return 0;
    try { return s->db.archiveRoom(id,s->actor) ? 1 : s->dbfail(); }
    catch(...) { return s->fail("Raum konnte nicht archiviert werden."); }
}
}
