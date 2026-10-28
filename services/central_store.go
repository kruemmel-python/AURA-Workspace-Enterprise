package main

/*
#cgo LDFLAGS: -lsqlite3
#include <sqlite3.h>
#include <stdlib.h>
#include <string.h>
static int aura_backup_copy(sqlite3* source, const char* target) {
 sqlite3* dst=0;
 if (sqlite3_open_v2(target,&dst,SQLITE_OPEN_READWRITE|SQLITE_OPEN_CREATE|SQLITE_OPEN_EXCLUSIVE,0)!=SQLITE_OK) { if(dst)sqlite3_close(dst);return 1; }
 sqlite3_backup* backup=sqlite3_backup_init(dst,"main",source,"main");
 if(!backup){sqlite3_close(dst);return 1;}
 int rc=sqlite3_backup_step(backup,-1);
 int finished=sqlite3_backup_finish(backup);
 int close_rc=sqlite3_close(dst);
 return rc==SQLITE_DONE && finished==SQLITE_OK && close_rc==SQLITE_OK ? 0:1;
}
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
static int aura_room_receipt(sqlite3* db, sqlite3_int64 actor, const char* client, const char* operation, const char* fingerprint) {
 sqlite3_stmt* st=0;if(sqlite3_prepare_v2(db,"SELECT fingerprint FROM room_sync_receipts WHERE actor_id=? AND client_id=? AND operation_id=?",-1,&st,0)!=SQLITE_OK)return -1;
 sqlite3_bind_int64(st,1,actor);sqlite3_bind_text(st,2,client,-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,3,operation,-1,SQLITE_TRANSIENT);int step=sqlite3_step(st);int result=0;if(step==SQLITE_ROW){const unsigned char* value=sqlite3_column_text(st,0);result=value && strcmp((const char*)value,fingerprint)==0?1:2;}else if(step!=SQLITE_DONE)result=-1;sqlite3_finalize(st);return result;
}
static int aura_room_receipt_insert(sqlite3* db, sqlite3_int64 actor, const char* client, const char* operation, const char* fingerprint) {
 sqlite3_stmt* st=0;if(sqlite3_prepare_v2(db,"INSERT INTO room_sync_receipts(actor_id,client_id,operation_id,fingerprint) VALUES(?,?,?,?)",-1,&st,0)!=SQLITE_OK)return 1;
 sqlite3_bind_int64(st,1,actor);sqlite3_bind_text(st,2,client,-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,3,operation,-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,4,fingerprint,-1,SQLITE_TRANSIENT);int result=sqlite3_step(st)==SQLITE_DONE?0:1;sqlite3_finalize(st);return result;
}
static int aura_any_receipt(sqlite3* db, const char* client, const char* operation) {
 sqlite3_stmt* st=0;if(sqlite3_prepare_v2(db,"SELECT 1 FROM room_sync_receipts WHERE client_id=? AND operation_id=? LIMIT 1",-1,&st,0)!=SQLITE_OK)return -1;
 sqlite3_bind_text(st,1,client,-1,SQLITE_TRANSIENT);sqlite3_bind_text(st,2,operation,-1,SQLITE_TRANSIENT);
 int step=sqlite3_step(st);int result=step==SQLITE_ROW?1:step==SQLITE_DONE?0:-1;sqlite3_finalize(st);return result;
}
static int aura_prepare_events(sqlite3* db, sqlite3_stmt** statement, sqlite3_int64 cursor) {
  return sqlite3_prepare_v2(db, "SELECT id,topic,payload,occurred_at FROM central_events WHERE id > ? ORDER BY id", -1, statement, 0) == SQLITE_OK
    && sqlite3_bind_int64(*statement, 1, cursor) == SQLITE_OK ? 0 : 1;
}
static int aura_prepare_event_history(sqlite3* db, sqlite3_stmt** statement) {
  return sqlite3_prepare_v2(db, "SELECT id,topic,occurred_at FROM central_events ORDER BY id DESC LIMIT 200", -1, statement, 0);
}
static int aura_prepare_attachment_changes(sqlite3* db, sqlite3_stmt** statement, sqlite3_int64 cursor) {
  return sqlite3_prepare_v2(db, "SELECT payload FROM central_events WHERE id > ? AND topic IN ('attachment.archived','attachment.restored') ORDER BY id", -1, statement, 0) == SQLITE_OK && sqlite3_bind_int64(*statement, 1, cursor) == SQLITE_OK ? 0 : 1;
}
static int aura_latest_event(sqlite3* db, sqlite3_int64* value) {
  sqlite3_stmt* st=0;
  if (sqlite3_prepare_v2(db, "SELECT COALESCE(MAX(id),0) FROM central_events", -1, &st, 0) != SQLITE_OK) return 1;
  int result=1; if(sqlite3_step(st)==SQLITE_ROW){*value=sqlite3_column_int64(st,0);result=0;}
  sqlite3_finalize(st);return result;
}
static int aura_step_row(sqlite3_stmt* statement) { return sqlite3_step(statement) == SQLITE_ROW; }
static sqlite3_int64 aura_column_int64(sqlite3_stmt* statement, int column) { return sqlite3_column_int64(statement, column); }
static const char* aura_column_text(sqlite3_stmt* statement, int column) { const unsigned char* value = sqlite3_column_text(statement, column); return value ? (const char*)value : ""; }
*/
import "C"

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
	"unsafe"
)

func (store *centralStore) attachmentChangedAfter(cursor int64, id string) (bool, error) {
	store.lock.Lock()
	defer store.lock.Unlock()
	var statement *C.sqlite3_stmt
	if C.aura_prepare_attachment_changes(store.db, &statement, C.sqlite3_int64(cursor)) != 0 {
		if statement != nil {
			C.sqlite3_finalize(statement)
		}
		return false, fmt.Errorf("attachment history query failed")
	}
	defer C.sqlite3_finalize(statement)
	for {
		step := C.sqlite3_step(statement)
		if step == C.SQLITE_DONE {
			return false, nil
		}
		if step != C.SQLITE_ROW {
			return false, fmt.Errorf("attachment history query failed")
		}
		var entry struct {
			ID string `json:"id"`
		}
		if json.Unmarshal([]byte(C.GoString(C.aura_column_text(statement, 0))), &entry) != nil || entry.ID == "" {
			return false, fmt.Errorf("invalid attachment history")
		}
		if entry.ID == id {
			return true, nil
		}
	}
}

// centralStore is the authoritative, transactional event store. SQLite is the
// source of truth; the NDJSON journal is only a compatibility mirror for older
// clients and must never be used to rebuild a running central server.
type centralStore struct {
	db   *C.sqlite3
	lock sync.Mutex
}

type eventHistoryEntry struct {
	ID    int64  `json:"id"`
	Topic string `json:"topic"`
	At    string `json:"at"`
}

func (store *centralStore) eventHistory() ([]eventHistoryEntry, error) {
	store.lock.Lock()
	defer store.lock.Unlock()
	var statement *C.sqlite3_stmt
	if C.aura_prepare_event_history(store.db, &statement) != C.SQLITE_OK {
		return nil, fmt.Errorf("central database query failed")
	}
	defer C.sqlite3_finalize(statement)
	entries := []eventHistoryEntry{}
	for {
		step := C.sqlite3_step(statement)
		if step == C.SQLITE_DONE {
			return entries, nil
		}
		if step != C.SQLITE_ROW {
			return nil, fmt.Errorf("central database query failed")
		}
		entries = append(entries, eventHistoryEntry{int64(C.aura_column_int64(statement, 0)), C.GoString(C.aura_column_text(statement, 1)), C.GoString(C.aura_column_text(statement, 2))})
	}
}

func openCentralStore(path string) (*centralStore, error) {
	value := C.CString(path)
	defer C.free(unsafe.Pointer(value))
	store := &centralStore{}
	if C.sqlite3_open_v2(value, &store.db, C.SQLITE_OPEN_READWRITE|C.SQLITE_OPEN_CREATE|C.SQLITE_OPEN_FULLMUTEX, nil) != C.SQLITE_OK {
		return nil, fmt.Errorf("central database open failed")
	}
	for _, migration := range []string{"PRAGMA journal_mode=WAL;", "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY);", "CREATE TABLE IF NOT EXISTS central_events(id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, payload TEXT NOT NULL, occurred_at TEXT NOT NULL);", "INSERT OR IGNORE INTO schema_migrations(version) VALUES(1);", "CREATE TABLE IF NOT EXISTS room_sync_receipts(actor_id INTEGER NOT NULL,client_id TEXT NOT NULL,operation_id TEXT NOT NULL,fingerprint TEXT NOT NULL,PRIMARY KEY(actor_id,client_id,operation_id));", "CREATE INDEX IF NOT EXISTS idx_sync_receipts_client_op ON room_sync_receipts(client_id,operation_id);"} {
		sql := C.CString(migration)
		result := C.aura_exec(store.db, sql)
		C.free(unsafe.Pointer(sql))
		if result != 0 {
			store.close()
			return nil, fmt.Errorf("central database migration failed")
		}
	}
	return store, nil
}

// snapshot writes a transactionally consistent copy without replacing the live database.
// The caller receives only a filename, never a server filesystem path.
func (store *centralStore) snapshot(directory string) (string, error) {
	if store == nil {
		return "", fmt.Errorf("central database required")
	}
	if err := os.MkdirAll(directory, 0700); err != nil {
		return "", err
	}
	if err := os.Chmod(directory, 0700); err != nil {
		return "", err
	}
	filename := "aura-central-" + time.Now().UTC().Format("20060102-150405.000000000") + ".sqlite"
	path := filepath.Join(directory, filename)
	store.lock.Lock()
	target := C.CString(path)
	result := C.aura_backup_copy(store.db, target)
	C.free(unsafe.Pointer(target))
	store.lock.Unlock()
	if result != 0 {
		_ = os.Remove(path)
		return "", fmt.Errorf("sqlite snapshot failed")
	}
	if err := os.Chmod(path, 0600); err != nil {
		return "", err
	}
	return filename, nil
}

func (store *centralStore) close() {
	if store != nil && store.db != nil {
		C.sqlite3_close(store.db)
		store.db = nil
	}
}
func (store *centralStore) append(event syncEvent) error {
	if store == nil {
		return nil
	}
	store.lock.Lock()
	defer store.lock.Unlock()
	topic := C.CString(event.Topic)
	payload := C.CString(event.Payload)
	at := C.CString(event.At)
	defer C.free(unsafe.Pointer(topic))
	defer C.free(unsafe.Pointer(payload))
	defer C.free(unsafe.Pointer(at))
	if C.aura_insert_event(store.db, topic, payload, at) != 0 {
		return fmt.Errorf("central database insert failed")
	}
	return nil
}

// appendMany commits related events together in the authoritative SQLite store.
func (store *centralStore) appendMany(events []syncEvent) error {
	if store == nil {
		return fmt.Errorf("central database required")
	}
	store.lock.Lock()
	defer store.lock.Unlock()
	exec := func(sql string) bool {
		p := C.CString(sql)
		defer C.free(unsafe.Pointer(p))
		return C.aura_exec(store.db, p) == 0
	}
	if !exec("BEGIN IMMEDIATE") {
		return fmt.Errorf("central transaction failed")
	}
	for _, event := range events {
		topic := C.CString(event.Topic)
		payload := C.CString(event.Payload)
		at := C.CString(event.At)
		status := C.aura_insert_event(store.db, topic, payload, at)
		C.free(unsafe.Pointer(topic))
		C.free(unsafe.Pointer(payload))
		C.free(unsafe.Pointer(at))
		if status != 0 {
			exec("ROLLBACK")
			return fmt.Errorf("central database insert failed")
		}
	}
	if !exec("COMMIT") {
		exec("ROLLBACK")
		return fmt.Errorf("central transaction commit failed")
	}
	return nil
}

// roomReceipt returns 0 for an unknown operation, 1 for an exact retry,
// 2 for an ID reused with different content, and -1 on storage failure.
func (store *centralStore) roomReceipt(actor int64, client, operation, fingerprint string) int {
	if store == nil {
		return -1
	}
	store.lock.Lock()
	defer store.lock.Unlock()
	a := C.CString(client)
	b := C.CString(operation)
	c := C.CString(fingerprint)
	defer C.free(unsafe.Pointer(a))
	defer C.free(unsafe.Pointer(b))
	defer C.free(unsafe.Pointer(c))
	return int(C.aura_room_receipt(store.db, C.sqlite3_int64(actor), a, b, c))
}

func (store *centralStore) anyReceipt(client, operation string) int {
	store.lock.Lock()
	defer store.lock.Unlock()
	a, b := C.CString(client), C.CString(operation)
	defer C.free(unsafe.Pointer(a))
	defer C.free(unsafe.Pointer(b))
	return int(C.aura_any_receipt(store.db, a, b))
}

// recordReceipt acknowledges a validated no-op without creating a second event.
func (store *centralStore) recordReceipt(actor int64, client, operation, fingerprint string) error {
	store.lock.Lock()
	defer store.lock.Unlock()
	a, b, c := C.CString(client), C.CString(operation), C.CString(fingerprint)
	defer C.free(unsafe.Pointer(a))
	defer C.free(unsafe.Pointer(b))
	defer C.free(unsafe.Pointer(c))
	if C.aura_room_receipt_insert(store.db, C.sqlite3_int64(actor), a, b, c) != 0 {
		return fmt.Errorf("sync receipt failed")
	}
	return nil
}
func (store *centralStore) appendRoomSync(actor int64, client, operation, fingerprint string, event syncEvent) error {
	if store == nil {
		return fmt.Errorf("central database required")
	}
	store.lock.Lock()
	defer store.lock.Unlock()
	exec := func(sql string) bool {
		p := C.CString(sql)
		defer C.free(unsafe.Pointer(p))
		return C.aura_exec(store.db, p) == 0
	}
	if !exec("BEGIN IMMEDIATE") {
		return fmt.Errorf("transaction failed")
	}
	a := C.CString(client)
	b := C.CString(operation)
	c := C.CString(fingerprint)
	topic := C.CString(event.Topic)
	payload := C.CString(event.Payload)
	at := C.CString(event.At)
	defer C.free(unsafe.Pointer(a))
	defer C.free(unsafe.Pointer(b))
	defer C.free(unsafe.Pointer(c))
	defer C.free(unsafe.Pointer(topic))
	defer C.free(unsafe.Pointer(payload))
	defer C.free(unsafe.Pointer(at))
	if C.aura_insert_event(store.db, topic, payload, at) != 0 || C.aura_room_receipt_insert(store.db, C.sqlite3_int64(actor), a, b, c) != 0 {
		exec("ROLLBACK")
		return fmt.Errorf("sync receipt failed")
	}
	if !exec("COMMIT") {
		exec("ROLLBACK")
		return fmt.Errorf("transaction commit failed")
	}
	return nil
}

// latestCursor is read while the caller holds the booking registry lock.
// This relates a calendar snapshot to the same event history used by clients.
func (store *centralStore) latestCursor() (int64, error) {
	if store == nil {
		return 0, fmt.Errorf("central database unavailable")
	}
	store.lock.Lock()
	defer store.lock.Unlock()
	var cursor C.sqlite3_int64
	if C.aura_latest_event(store.db, &cursor) != 0 {
		return 0, fmt.Errorf("central cursor query failed")
	}
	return int64(cursor), nil
}

// eventsAfter returns the globally ordered server cursor feed. Reading this
// database, rather than the local journal, ensures a restart has the exact
// same history and cursor semantics as a live server.
func (store *centralStore) eventsAfter(cursor int) ([]syncEvent, int, error) {
	if store == nil {
		return nil, cursor, fmt.Errorf("central database unavailable")
	}
	if cursor < 0 {
		return nil, cursor, fmt.Errorf("invalid cursor")
	}
	store.lock.Lock()
	defer store.lock.Unlock()
	var statement *C.sqlite3_stmt
	if C.aura_prepare_events(store.db, &statement, C.sqlite3_int64(cursor)) != 0 {
		return nil, cursor, fmt.Errorf("central database query failed")
	}
	defer C.sqlite3_finalize(statement)
	events := []syncEvent{}
	next := cursor
	for C.aura_step_row(statement) != 0 {
		id := int(C.aura_column_int64(statement, 0))
		events = append(events, syncEvent{Topic: C.GoString(C.aura_column_text(statement, 1)), Payload: C.GoString(C.aura_column_text(statement, 2)), At: C.GoString(C.aura_column_text(statement, 3))})
		next = id
	}
	return events, next, nil
}
