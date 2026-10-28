#include "../core/enterprise_database.h"
#include <sqlite3.h>
#include <cassert>
#include <algorithm>
#include <filesystem>
#include <iostream>

// Uses the same UTF-8/PBKDF2-SHA256 account representation as the Windows client.
int main() {
    const std::wstring path = L"KR_BUILD/datenbank-ä-🔐.db";
    std::filesystem::remove("KR_BUILD/datenbank-ä-🔐.db");
    EnterpriseDatabase db(path);
    assert(db.ready());
    int64_t employee = 0;
    assert(db.createEmployee({0, L"München 🔐", L"Ingenieurin", L"Entwicklung", L"office", true}, employee, L"Test"));
    auto people = db.employees();
    assert(people.size() == 1 && people[0].name == L"München 🔐");
    assert(db.createOrResetAccount(employee, L"nutzér", L"Initiales-Passwort-2026", false, L"Test"));

    // Deterministic PBKDF2-HMAC-SHA256 vector: password encoded as UTF-8,
    // 210000 rounds, salt 000102...0f, 32-byte output, lowercase hex.
    sqlite3* raw = nullptr;
    assert(sqlite3_open("KR_BUILD/datenbank-ä-🔐.db", &raw) == SQLITE_OK);
    const char* sql = "UPDATE accounts SET password_salt='000102030405060708090a0b0c0d0e0f',"
                      "password_digest='4e12c6bac0fc0bc70d3078c556e24b47f2984759bb9cfcc8b6297a2833c16206'";
    assert(sqlite3_exec(raw, sql, nullptr, nullptr, nullptr) == SQLITE_OK);
    sqlite3_close(raw);
    EnterpriseAccount account;
    assert(db.authenticate(L"nutzér", L"München-🔐-2026-Passwort", 1000, account));
    assert(account.employeeId == employee);
    assert(!db.authenticate(L"nutzér", L"München-🔐-2026-Falsch", 1001, account));
    int64_t task = 0, otherTask = 0;
    assert(db.createTask({0,L"Synchronisation",L"IT",L"Hoch",L"open",employee,true},task,L"Test"));
    assert(db.createTask({0,L"Andere Aufgabe",L"IT",L"Mittel",L"open",employee,true},otherTask,L"Test"));
    assert(db.ensureSyncedExternalId(L"task",task,L"server:task"));
    assert(db.ensureSyncedExternalId(L"task",otherTask,L"server:other"));
    assert(db.ensureSyncedExternalId(L"employee",employee,L"server:employee"));
    EnterpriseSubtask sub{0,0,L"Schritt",L"open",0,12345,true};
    assert(!db.applySyncedSubtask(L"remote:1",L"server:task",L"missing",sub,L"sync"));
    assert(db.subtasks(task).empty());
    assert(db.applySyncedSubtask(L"remote:1",L"server:task",L"server:employee",sub,L"sync"));
    auto items=db.subtasks(task);assert(items.size()==1 && items[0].assigneeId==employee && items[0].dueAt==12345);
    assert(db.applySyncedSubtask(L"remote:1",L"server:task",L"server:employee",sub,L"sync"));
    assert(db.subtasks(task).size()==1);
    auto edited=db.subtasks(task)[0];edited.title=L"Lokale Änderung";assert(db.updateSubtask(edited,L"Test"));
    sub.title=L"Serveränderung";assert(!db.applySyncedSubtask(L"remote:1",L"server:task",L"server:employee",sub,L"sync"));
    assert(db.subtasks(task)[0].title==L"Lokale Änderung");edited.title=L"Schritt";assert(db.updateSubtask(edited,L"Test"));sub.title=L"Schritt";
    assert(!db.applySyncedSubtask(L"remote:1",L"server:other",L"server:employee",sub,L"sync"));
    assert(db.subtasks(otherTask).empty());
    assert(sqlite3_open("KR_BUILD/datenbank-ä-🔐.db", &raw)==SQLITE_OK);
    assert(sqlite3_exec(raw,"CREATE TRIGGER fail_subtask_sync_audit BEFORE INSERT ON audit_log WHEN NEW.action='sync.subtask.applied' BEGIN SELECT RAISE(ABORT,'failure'); END",nullptr,nullptr,nullptr)==SQLITE_OK);
    sub.title=L"Nicht gespeichert";assert(!db.applySyncedSubtask(L"remote:1",L"server:task",L"server:employee",sub,L"sync"));
    assert(db.subtasks(task)[0].title==L"Schritt");
    assert(sqlite3_exec(raw,"DROP TRIGGER fail_subtask_sync_audit",nullptr,nullptr,nullptr)==SQLITE_OK);sqlite3_close(raw);
    sub.title=L"Schritt";sub.status=L"done";assert(db.applySyncedSubtask(L"remote:1",L"server:task",L"server:employee",sub,L"sync"));
    assert(db.updateTaskStatus(task,L"in_progress",L"Test"));assert(db.updateTaskStatus(task,L"done",L"Test"));
    sub.status=L"open";assert(!db.applySyncedSubtask(L"remote:1",L"server:task",L"server:employee",sub,L"sync"));
    assert(db.subtasks(task)[0].status==L"done");
    sub.status=L"done";sub.active=false;assert(db.applySyncedSubtask(L"remote:1",L"server:task",L"server:employee",sub,L"sync"));
    assert(db.subtasks(task).empty() && db.subtasks(task,true).size()==1);
    int64_t queuedId=0;assert(db.createSubtask({0,otherTask,L"Vorhanden",L"open",employee,0,true},queuedId,L"Test"));
    assert(db.configureDesktopSubtaskOutbox(L"windows-client"));
    assert(db.configureDesktopSubtaskOutbox(L"windows-client"));
    assert(!db.configureDesktopSubtaskOutbox(L"other-client"));
    assert(sqlite3_open("KR_BUILD/datenbank-ä-🔐.db", &raw)==SQLITE_OK);
    auto countQueue=[&](){sqlite3_stmt* q=nullptr;assert(sqlite3_prepare_v2(raw,"SELECT count(*) FROM desktop_subtask_sync_outbox",-1,&q,nullptr)==SQLITE_OK);assert(sqlite3_step(q)==SQLITE_ROW);int n=sqlite3_column_int(q,0);sqlite3_finalize(q);return n;};
    assert(countQueue()==1);
    EnterpriseSubtask incoming{0,0,L"Servereintrag",L"open",0,0,true};
    assert(db.applySyncedSubtask(L"server:sub",L"server:other",L"server:employee",incoming,L"sync"));assert(countQueue()==1);
    auto entries=db.subtasks(otherTask);auto local=*std::find_if(entries.begin(),entries.end(),[&](const EnterpriseSubtask& v){return v.id==queuedId;});
    local.title=L"Geändert";assert(db.updateSubtask(local,L"Test"));assert(countQueue()==2);
    assert(sqlite3_exec(raw,"CREATE TRIGGER reject_outbox BEFORE INSERT ON desktop_subtask_sync_outbox BEGIN SELECT RAISE(ABORT,'queue failure'); END",nullptr,nullptr,nullptr)==SQLITE_OK);
    assert(!db.archiveSubtask(queuedId,L"Test"));assert(countQueue()==2);assert(db.subtasks(otherTask).size()==2);
    assert(sqlite3_exec(raw,"DROP TRIGGER reject_outbox",nullptr,nullptr,nullptr)==SQLITE_OK);
    assert(db.archiveSubtask(queuedId,L"Test"));assert(countQueue()==3);
    sqlite3_stmt* q=nullptr;assert(sqlite3_prepare_v2(raw,"SELECT action,json_extract(value_json,'$.title'),json_extract(expected_json,'$.title') FROM desktop_subtask_sync_outbox ORDER BY seq",-1,&q,nullptr)==SQLITE_OK);
    assert(sqlite3_step(q)==SQLITE_ROW && std::string(reinterpret_cast<const char*>(sqlite3_column_text(q,0)))=="create");
    assert(sqlite3_step(q)==SQLITE_ROW && std::string(reinterpret_cast<const char*>(sqlite3_column_text(q,0)))=="update" && std::string(reinterpret_cast<const char*>(sqlite3_column_text(q,2)))=="Vorhanden");
    assert(sqlite3_step(q)==SQLITE_ROW && std::string(reinterpret_cast<const char*>(sqlite3_column_text(q,0)))=="archive");assert(sqlite3_step(q)==SQLITE_DONE);sqlite3_finalize(q);sqlite3_close(raw);
    EnterpriseDatabase reopened(path);assert(reopened.ready() && reopened.configureDesktopSubtaskOutbox(L"windows-client"));
    assert(sqlite3_open("KR_BUILD/datenbank-ä-🔐.db", &raw)==SQLITE_OK);assert(countQueue()==3);
    assert(sqlite3_prepare_v2(raw,"SELECT operation_id FROM desktop_subtask_sync_outbox ORDER BY seq LIMIT 1",-1,&q,nullptr)==SQLITE_OK);assert(sqlite3_step(q)==SQLITE_ROW);std::string operation(reinterpret_cast<const char*>(sqlite3_column_text(q,0)));sqlite3_finalize(q);std::wstring op(operation.begin(),operation.end());
    std::wstring nextOp,nextAction,nextId,nextValue,nextExpected,nextWire;
    assert(reopened.nextDesktopSubtaskOperation(nextOp,nextAction,nextId,nextValue,nextExpected,nextWire));
    assert(nextOp==op && nextAction==L"create" && !nextId.empty() && nextExpected.empty() && nextWire.empty());
    const std::wstring queueExternal=nextId;
    const std::wstring wire=L"{\"clientId\":\"windows-client\",\"operationId\":\""+op+L"\",\"action\":\"create\",\"subtask\":{\"id\":\""+queueExternal+L"\",\"taskId\":\"server:other\",\"title\":\"Vorhanden\",\"status\":\"open\",\"assigneeId\":8,\"dueAt\":0,\"active\":true},\"expected\":null,\"assigneeEmployeeId\":\"server:employee\"}";
    auto forged=wire;forged.replace(forged.find(L"Vorhanden"),9,L"Manipuliert");assert(!reopened.freezeDesktopSubtaskOperation(op,L"https://server",9,forged));
    forged=wire;forged.replace(forged.find(L"server:employee"),15,L"server:otheremp");assert(!reopened.freezeDesktopSubtaskOperation(op,L"https://server",9,forged));
    assert(!reopened.freezeDesktopSubtaskOperation(L"not-the-head",L"https://server",9,wire));
    assert(reopened.freezeDesktopSubtaskOperation(op,L"https://server",9,wire));
    assert(reopened.nextDesktopSubtaskOperation(nextOp,nextAction,nextId,nextValue,nextExpected,nextWire) && nextWire==wire);
    assert(reopened.freezeDesktopSubtaskOperation(op,L"https://server",9,wire));
    assert(!reopened.freezeDesktopSubtaskOperation(op,L"https://other",9,wire));
    assert(!reopened.freezeDesktopSubtaskOperation(op,L"https://server",8,wire));
    assert(!reopened.freezeDesktopSubtaskOperation(op,L"https://server",9,L"different"));
    assert(!reopened.acknowledgeDesktopSubtaskOperation(op,L"https://server",9,L"different",L"Test"));
    assert(sqlite3_exec(raw,"CREATE TRIGGER reject_send_audit BEFORE INSERT ON audit_log WHEN NEW.action='sync.subtask.sent' BEGIN SELECT RAISE(ABORT,'audit failure'); END",nullptr,nullptr,nullptr)==SQLITE_OK);
    assert(!reopened.acknowledgeDesktopSubtaskOperation(op,L"https://server",9,wire,L"Test"));assert(countQueue()==3);
    assert(sqlite3_exec(raw,"DROP TRIGGER reject_send_audit",nullptr,nullptr,nullptr)==SQLITE_OK);
    assert(reopened.acknowledgeDesktopSubtaskOperation(op,L"https://server",9,wire,L"Test"));assert(countQueue()==2);
    EnterpriseSubtask echoed{0,0,L"Vorhanden",L"open",0,0,true};
    assert(reopened.applySyncedSubtask(queueExternal,L"server:other",L"server:employee",echoed,L"sync"));
    assert(countQueue()==2 && reopened.subtasks(otherTask).size()==1);
    echoed.title=L"Fremde Änderung";assert(!reopened.applySyncedSubtask(queueExternal,L"server:other",L"server:employee",echoed,L"sync"));
    assert(reopened.nextDesktopSubtaskOperation(nextOp,nextAction,nextId,nextValue,nextExpected,nextWire) && nextOp!=op && nextAction==L"update" && !nextExpected.empty() && nextWire.empty());
    const std::wstring updateWire=L"{\"clientId\":\"windows-client\",\"operationId\":\""+nextOp+L"\",\"action\":\"update\",\"subtask\":{\"id\":\""+nextId+L"\",\"taskId\":\"server:other\",\"title\":\"Geändert\",\"status\":\"open\",\"assigneeId\":8,\"dueAt\":0,\"active\":true},\"expected\":{\"id\":\""+nextId+L"\",\"taskId\":\"server:other\",\"title\":\"Vorhanden\",\"status\":\"open\",\"assigneeId\":8,\"dueAt\":0,\"active\":true},\"assigneeEmployeeId\":\"server:employee\",\"expectedAssigneeEmployeeId\":\"server:employee\"}";
    auto badBase=updateWire;badBase.replace(badBase.find(L"Vorhanden"),9,L"Unbekannt");assert(!reopened.freezeDesktopSubtaskOperation(nextOp,L"https://server",9,badBase));
    assert(reopened.freezeDesktopSubtaskOperation(nextOp,L"https://server",9,updateWire));
    assert(sqlite3_prepare_v2(raw,"SELECT title,active FROM sync_subtask_shadow WHERE subtask_id=?",-1,&q,nullptr)==SQLITE_OK);sqlite3_bind_int64(q,1,queuedId);assert(sqlite3_step(q)==SQLITE_ROW);assert(std::string(reinterpret_cast<const char*>(sqlite3_column_text(q,0)))=="Vorhanden" && sqlite3_column_int(q,1)==1);sqlite3_finalize(q);
    assert(!reopened.acknowledgeDesktopSubtaskOperation(op,L"https://server",9,wire,L"Test"));sqlite3_close(raw);
    std::cout << "Unicode und PBKDF2-Kompatibilität: OK\n";
}
