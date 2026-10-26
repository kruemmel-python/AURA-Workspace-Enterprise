package main

/*
#cgo LDFLAGS: -lsqlite3
#include <sqlite3.h>
#include <stdlib.h>
static int aura_exec(sqlite3* db, const char* sql) { return sqlite3_exec(db, sql, 0, 0, 0); }
static int aura_insert_event(sqlite3* db, const char* topic, const char* payload, const char* at) {
  sqlite3_stmt* statement = 0;
  if (sqlite3_prepare_v2(db, "INSERT INTO central_events(topic,payload,occurred_at) VALUES(?,?,?)", -1, &statement, 0) != SQLITE_OK) return 1;
  sqlite3_bind_text(statement, 1, topic, -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(statement, 2, payload, -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(statement, 3, at, -1, SQLITE_TRANSIENT);
  int result = sqlite3_step(statement) == SQLITE_DONE ? 0 : 1;
  sqlite3_finalize(statement); return result;
}
static int aura_prepare_events(sqlite3* db, sqlite3_stmt** statement, sqlite3_int64 cursor) {
  return sqlite3_prepare_v2(db, "SELECT id,topic,payload,occurred_at FROM central_events WHERE id > ? ORDER BY id", -1, statement, 0) == SQLITE_OK
    && sqlite3_bind_int64(*statement, 1, cursor) == SQLITE_OK ? 0 : 1;
}
static int aura_step_row(sqlite3_stmt* statement) { return sqlite3_step(statement) == SQLITE_ROW; }
static sqlite3_int64 aura_column_int64(sqlite3_stmt* statement, int column) { return sqlite3_column_int64(statement, column); }
static const char* aura_column_text(sqlite3_stmt* statement, int column) { const unsigned char* value = sqlite3_column_text(statement, column); return value ? (const char*)value : ""; }
*/
import "C"

import (
	"fmt"
	"sync"
	"unsafe"
)

// centralStore is the authoritative, transactional event store. SQLite is the
// source of truth; the NDJSON journal is only a compatibility mirror for older
// clients and must never be used to rebuild a running central server.
type centralStore struct { db *C.sqlite3; lock sync.Mutex }

func openCentralStore(path string) (*centralStore, error) {
	value := C.CString(path); defer C.free(unsafe.Pointer(value))
	store := &centralStore{}
	if C.sqlite3_open_v2(value, &store.db, C.SQLITE_OPEN_READWRITE|C.SQLITE_OPEN_CREATE|C.SQLITE_OPEN_FULLMUTEX, nil) != C.SQLITE_OK { return nil, fmt.Errorf("central database open failed") }
	for _, migration := range []string{"PRAGMA journal_mode=WAL;", "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY);", "CREATE TABLE IF NOT EXISTS central_events(id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, payload TEXT NOT NULL, occurred_at TEXT NOT NULL);", "INSERT OR IGNORE INTO schema_migrations(version) VALUES(1);"} { sql:=C.CString(migration); result:=C.aura_exec(store.db,sql);C.free(unsafe.Pointer(sql));if result!=0 { store.close();return nil,fmt.Errorf("central database migration failed") } }
	return store,nil
}
func (store *centralStore) close(){if store!=nil&&store.db!=nil{C.sqlite3_close(store.db);store.db=nil}}
func (store *centralStore) append(event syncEvent) error { if store==nil{return nil};store.lock.Lock();defer store.lock.Unlock();topic:=C.CString(event.Topic);payload:=C.CString(event.Payload);at:=C.CString(event.At);defer C.free(unsafe.Pointer(topic));defer C.free(unsafe.Pointer(payload));defer C.free(unsafe.Pointer(at));if C.aura_insert_event(store.db,topic,payload,at)!=0{return fmt.Errorf("central database insert failed")};return nil }

// eventsAfter returns the globally ordered server cursor feed. Reading this
// database, rather than the local journal, ensures a restart has the exact
// same history and cursor semantics as a live server.
func (store *centralStore) eventsAfter(cursor int) ([]syncEvent, int, error) {
	if store == nil { return nil, cursor, fmt.Errorf("central database unavailable") }
	if cursor < 0 { return nil, cursor, fmt.Errorf("invalid cursor") }
	store.lock.Lock(); defer store.lock.Unlock()
	var statement *C.sqlite3_stmt
	if C.aura_prepare_events(store.db, &statement, C.sqlite3_int64(cursor)) != 0 { return nil, cursor, fmt.Errorf("central database query failed") }
	defer C.sqlite3_finalize(statement)
	events := []syncEvent{}
	next := cursor
	for C.aura_step_row(statement) != 0 {
		id := int(C.aura_column_int64(statement, 0))
		events = append(events, syncEvent{Topic:C.GoString(C.aura_column_text(statement, 1)), Payload:C.GoString(C.aura_column_text(statement, 2)), At:C.GoString(C.aura_column_text(statement, 3))})
		next = id
	}
	return events, next, nil
}
