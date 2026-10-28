package main

import (
	"aura/services/portable"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestCentralEventHistoryRequiresPermissionAndExcludesPayload(t *testing.T) {
	dir := t.TempDir()
	store, err := openCentralStore(filepath.Join(dir, "central.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	previous := centralMirror
	centralMirror = store
	defer func() { centralMirror = previous; store.close() }()
	t.Setenv("AURA_SYNC_IDENTITIES", `{"reader":{"employeeId":1,"permissions":["audit.read"]},"other":{"employeeId":2,"permissions":["rooms.manage"]}}`)
	for i := 0; i < 205; i++ {
		if err := store.append(syncEvent{Topic: fmt.Sprintf("event.%d", i), Payload: "private content", At: "2026-01-01T00:00:00Z"}); err != nil {
			t.Fatal(err)
		}
	}
	server := httptest.NewServer(syncHandler(filepath.Join(dir, "events"), "token"))
	defer server.Close()
	client := portable.Client{BaseURL: server.URL, Token: "reader"}
	entries, err := client.EventHistory(context.Background())
	if err != nil || len(entries) != 200 || entries[0].Topic != "event.204" || entries[199].Topic != "event.5" {
		t.Fatalf("history: %v %v", entries, err)
	}
	client.Token = "other"
	if _, err := client.EventHistory(context.Background()); err == nil {
		t.Fatal("missing permission was accepted")
	}
	req, _ := http.NewRequest(http.MethodGet, server.URL+"/api/v1/admin/event-history", nil)
	req.Header.Set("Authorization", "Bearer reader")
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var raw []map[string]any
	if err := json.NewDecoder(response.Body).Decode(&raw); err != nil {
		t.Fatal(err)
	}
	if _, ok := raw[0]["payload"]; ok {
		t.Fatal("private payload exposed")
	}
}

func TestLegacyCollaborationEventsRequireSubjectPermission(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"task":{"employeeId":1,"permissions":["tasks.manage"]},"ticket":{"employeeId":2,"permissions":["tickets.manage"]},"comment":{"employeeId":3,"permissions":["collaboration.comment"]}}`)
	handler := syncHandler(filepath.Join(t.TempDir(), "events"), "token")
	for _, kind := range []string{"task", "ticket"} {
		for _, topic := range []string{"comment.created", "attachment.created", "attachment.archived"} {
			payload := fmt.Sprintf(`{"id":"id","subjectType":%q,"subjectId":"missing","authorId":"author","body":"test","originalName":"file","sizeBytes":1,"sha256":"%s","uploadedBy":"author","active":true}`, kind, strings.Repeat("a", 64))
			data, _ := json.Marshal(syncEvent{Topic: topic, Payload: payload})
			for _, account := range []string{"task", "ticket", "comment"} {
				req := httptest.NewRequest(http.MethodPost, "/events", bytes.NewReader(data))
				req.Header.Set("Authorization", "Bearer "+account)
				result := httptest.NewRecorder()
				handler.ServeHTTP(result, req)
				allowed := account == kind || (account == "comment" && topic == "comment.created")
				if allowed && result.Code != http.StatusConflict {
					t.Fatalf("%s %s as %s: expected missing subject conflict, got %d %s", kind, topic, account, result.Code, result.Body.String())
				}
				if !allowed && result.Code != http.StatusForbidden {
					t.Fatalf("%s %s as %s: expected forbidden, got %d %s", kind, topic, account, result.Code, result.Body.String())
				}
			}
		}
	}
	post := func(account, topic, payload string) int {
		data, _ := json.Marshal(syncEvent{Topic: topic, Payload: payload})
		req := httptest.NewRequest(http.MethodPost, "/events", bytes.NewReader(data))
		req.Header.Set("Authorization", "Bearer "+account)
		result := httptest.NewRecorder()
		handler.ServeHTTP(result, req)
		return result.Code
	}
	if status := post("task", "task.created", `{"id":"task-1","title":"Task","department":"IT","priority":"Mittel","status":"open","assigneeId":1,"createdBy":1,"active":true}`); status != http.StatusAccepted {
		t.Fatalf("task create: %d", status)
	}
	if status := post("ticket", "ticket.created", `{"id":"ticket-1","title":"Ticket","category":"IT","priority":"Mittel","status":"open","assigneeId":2,"createdBy":2,"active":true}`); status != http.StatusAccepted {
		t.Fatalf("ticket create: %d", status)
	}
	for _, kind := range []string{"task", "ticket"} {
		other := "task"
		if kind == "task" {
			other = "ticket"
		}
		payload := fmt.Sprintf(`{"id":%q,"subjectType":%q,"subjectId":%q,"authorId":"author","body":"test"}`, "comment-"+kind, kind, kind+"-1")
		if status := post(other, "comment.created", payload); status != http.StatusForbidden {
			t.Fatalf("%s comment cross-permission: %d", kind, status)
		}
		if status := post(kind, "comment.created", payload); status != http.StatusAccepted {
			t.Fatalf("%s comment create: %d", kind, status)
		}
	}
}

func TestAttachmentStoreValidatesHashAndServesContent(t *testing.T) {
	journal := filepath.Join(t.TempDir(), "attachments.ndjson")
	handler := syncHandler(journal, "token")
	content := []byte("vertraulicher Testanhang")
	sum := sha256.Sum256(content)
	digest := hex.EncodeToString(sum[:])
	upload := httptest.NewRequest(http.MethodPut, "/api/v1/attachments/"+digest, bytes.NewReader(content))
	upload.Header.Set("Authorization", "Bearer token")
	upload.Header.Set("X-Aura-SHA256", digest)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, upload)
	if response.Code != http.StatusCreated {
		t.Fatalf("upload: %d", response.Code)
	}
	download := httptest.NewRequest(http.MethodGet, "/api/v1/attachments/"+digest, nil)
	download.Header.Set("Authorization", "Bearer token")
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, download)
	if response.Code != http.StatusOK || !bytes.Equal(response.Body.Bytes(), content) {
		t.Fatalf("download: %d %q", response.Code, response.Body.Bytes())
	}
	bad := httptest.NewRequest(http.MethodPut, "/api/v1/attachments/"+digest, bytes.NewBufferString("manipuliert"))
	bad.Header.Set("Authorization", "Bearer token")
	bad.Header.Set("X-Aura-SHA256", digest)
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, bad)
	if response.Code != http.StatusUnprocessableEntity {
		t.Fatalf("hash: %d", response.Code)
	}
}

func TestWorkspaceAttachmentUploadUsesCentralObjectStore(t *testing.T) {
	journal := filepath.Join(t.TempDir(), "desktop-attachment.ndjson")
	server := httptest.NewServer(syncHandler(journal, "desktop-token"))
	defer server.Close()
	path := filepath.Join(t.TempDir(), "evidence.txt")
	content := []byte("desktop attachment transfer")
	if err := os.WriteFile(path, content, 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "desktop-token")
	if code := uploadAttachmentFile(path); code != 0 {
		t.Fatalf("upload returned %d", code)
	}
	sum := sha256.Sum256(content)
	digest := hex.EncodeToString(sum[:])
	if _, err := os.Stat(journal + ".attachments" + string(os.PathSeparator) + digest); err != nil {
		t.Fatalf("central object missing: %v", err)
	}
}

func TestLegacyAttachmentRetryPreservesArchivedState(t *testing.T) {
	journal := filepath.Join(t.TempDir(), "events")
	store, err := openCentralStore(journal + ".sqlite")
	if err != nil {
		t.Fatal(err)
	}
	previous := centralMirror
	centralMirror = store
	defer func() { centralMirror.close(); centralMirror = previous }()
	handler := syncHandler(journal, "token")
	post := func(topic, payload string) int {
		data, _ := json.Marshal(syncEvent{Topic: topic, Payload: payload})
		req := httptest.NewRequest(http.MethodPost, "/events", bytes.NewReader(data))
		req.Header.Set("Authorization", "Bearer token")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, req)
		return response.Code
	}
	task := `{"id":"task-1","title":"Task","department":"IT","priority":"Mittel","status":"open","assigneeId":1,"createdBy":1,"active":true}`
	if status := post("task.created", task); status != http.StatusAccepted {
		t.Fatalf("task: %d", status)
	}
	create := fmt.Sprintf(`{"id":"attachment-1","subjectType":"task","subjectId":"task-1","originalName":"evidence.txt","sizeBytes":10,"sha256":"%s","uploadedBy":"employee-1","active":true}`, strings.Repeat("a", 64))
	archive := strings.Replace(create, `"active":true`, `"active":false`, 1)
	if status := post("attachment.archived", archive); status != http.StatusConflict {
		t.Fatalf("missing archive: %d", status)
	}
	if status := post("attachment.created", create); status != http.StatusAccepted {
		t.Fatalf("create: %d", status)
	}
	if status := post("attachment.created", create); status != http.StatusAccepted {
		t.Fatalf("create retry: %d", status)
	}
	if status := post("attachment.archived", archive); status != http.StatusAccepted {
		t.Fatalf("archive: %d", status)
	}
	store.close()
	store, err = openCentralStore(journal + ".sqlite")
	if err != nil {
		centralMirror = nil
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "token")
	if status := post("attachment.created", create); status != http.StatusAccepted {
		t.Fatalf("late create retry: %d", status)
	}
	if status := post("attachment.archived", archive); status != http.StatusAccepted {
		t.Fatalf("archive retry: %d", status)
	}
	if status := post("attachment.created", strings.Replace(create, "evidence.txt", "other.txt", 1)); status != http.StatusConflict {
		t.Fatalf("conflicting metadata: %d", status)
	}
	req := httptest.NewRequest(http.MethodGet, "/api/v1/attachment-sync?subjectType=task", nil)
	req.Header.Set("Authorization", "Bearer token")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, req)
	var attachments []centralAttachment
	if response.Code != http.StatusOK || json.Unmarshal(response.Body.Bytes(), &attachments) != nil || len(attachments) != 1 || attachments[0].Active {
		t.Fatalf("archived state: %d %s", response.Code, response.Body.String())
	}
	events, _, err := store.eventsAfter(0)
	if err != nil {
		t.Fatal(err)
	}
	count := 0
	for _, event := range events {
		if strings.HasPrefix(event.Topic, "attachment.") {
			count++
		}
	}
	if count != 2 {
		t.Fatalf("expected one create and one archive, got %d", count)
	}
}

func TestDesktopAttachmentArchiveReceiptSurvivesRestoreAndRestart(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"token":{"employeeId":1,"permissions":["*"]},"other":{"employeeId":2,"permissions":["*"]}}`)
	journal := filepath.Join(t.TempDir(), "events")
	store, err := openCentralStore(journal + ".sqlite")
	if err != nil {
		t.Fatal(err)
	}
	previous := centralMirror
	centralMirror = store
	defer func() { centralMirror.close(); centralMirror = previous }()
	handler := syncHandler(journal, "token")
	post := func(path string, event syncEvent) int {
		data, _ := json.Marshal(event)
		req := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(data))
		req.Header.Set("Authorization", "Bearer token")
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, req)
		return recorder.Code
	}
	task := `{"id":"task-1","title":"Task","department":"IT","priority":"Mittel","status":"open","assigneeId":1,"createdBy":1,"active":true}`
	if post("/events", syncEvent{Topic: "task.created", Payload: task}) != 202 {
		t.Fatal("task")
	}
	create := fmt.Sprintf(`{"id":"attachment-1","subjectType":"task","subjectId":"task-1","originalName":"evidence.txt","sizeBytes":10,"sha256":"%s","uploadedBy":"employee-1","active":true}`, strings.Repeat("a", 64))
	if post("/events", syncEvent{Topic: "attachment.created", Payload: create}) != 202 {
		t.Fatal("create")
	}
	archive := syncEvent{Topic: "attachment.archived", Payload: strings.Replace(create, `"active":true`, `"active":false,"baseCursor":2`, 1), ClientID: "desktop-a", OperationID: "op-a"}
	path := "/api/v1/desktop-attachment-events"
	missingBase := archive
	missingBase.Payload = strings.Replace(archive.Payload, `,"baseCursor":2`, ``, 1)
	if post(path, missingBase) != http.StatusConflict {
		t.Fatal("missing base cursor accepted")
	}
	if post(path, archive) != 202 {
		t.Fatal("archive")
	}
	// Simulate a restore committed by another client, then restart from SQLite.
	if err := persistEvent(journal, syncEvent{Topic: "attachment.restored", Payload: create, At: "2026-01-01T00:00:00Z"}); err != nil {
		t.Fatal(err)
	}
	store.close()
	store, err = openCentralStore(journal + ".sqlite")
	if err != nil {
		centralMirror = nil
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "token")
	legacy := archive
	legacy.ClientID = ""
	legacy.OperationID = ""
	if post("/events", legacy) != http.StatusConflict {
		t.Fatal("legacy archive after restore must be reconciled")
	}
	if post(path, archive) != 202 {
		t.Fatal("retry")
	}
	changed := archive
	changed.Payload = strings.Replace(archive.Payload, "evidence.txt", "other.txt", 1)
	if post(path, changed) != 409 {
		t.Fatal("operation reuse accepted")
	}
	req := httptest.NewRequest(http.MethodGet, "/api/v1/attachment-sync?subjectType=task", nil)
	req.Header.Set("Authorization", "Bearer token")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, req)
	var entries []centralAttachment
	if json.Unmarshal(recorder.Body.Bytes(), &entries) != nil || len(entries) != 1 || !entries[0].Active {
		t.Fatalf("retry undid restore: %s", recorder.Body.String())
	}
	stale := archive
	stale.OperationID = "op-discard"
	if post(path, stale) != http.StatusConflict {
		t.Fatal("stale archive accepted")
	}
	abandon := func(expected centralAttachment) int {
		data, _ := json.Marshal(struct {
			Event    syncEvent         `json:"event"`
			Expected centralAttachment `json:"expected"`
		}{stale, expected})
		req := httptest.NewRequest(http.MethodPost, "/api/v1/desktop-attachment-abandon", bytes.NewReader(data))
		req.Header.Set("Authorization", "Bearer token")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, req)
		return response.Code
	}
	wrong := entries[0]
	wrong.Active = false
	if abandon(wrong) != http.StatusConflict {
		t.Fatal("changed review accepted")
	}
	if abandon(entries[0]) != http.StatusAccepted {
		t.Fatal("abandon failed")
	}
	if abandon(entries[0]) != http.StatusAccepted {
		t.Fatal("abandon retry failed")
	}
	if post(path, stale) != http.StatusConflict {
		t.Fatal("abandoned operation accepted")
	}
	store.close()
	store, err = openCentralStore(journal + ".sqlite")
	if err != nil {
		centralMirror = nil
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "token")
	if post(path, stale) != http.StatusConflict || abandon(entries[0]) != http.StatusAccepted {
		t.Fatal("abandon fence did not survive restart")
	}
	otherData, _ := json.Marshal(stale)
	otherReq := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(otherData))
	otherReq.Header.Set("Authorization", "Bearer other")
	otherResult := httptest.NewRecorder()
	handler.ServeHTTP(otherResult, otherReq)
	if otherResult.Code != http.StatusConflict {
		t.Fatalf("other account bypassed fence: %d", otherResult.Code)
	}
	archive.OperationID = "op-b"
	if post(path, archive) != http.StatusConflict {
		t.Fatal("stale offline archive accepted after restore")
	}
	archive.Payload = strings.Replace(archive.Payload, `"baseCursor":2`, `"baseCursor":4`, 1)
	if post(path, archive) != 202 {
		t.Fatal("new archive")
	}
	// A new operation sent while already archived also receives a durable receipt.
	archive.OperationID = "op-c"
	if post(path, archive) != 202 {
		t.Fatal("archive no-op")
	}
	if err := persistEvent(journal, syncEvent{Topic: "attachment.restored", Payload: create, At: "2026-01-01T00:00:01Z"}); err != nil {
		t.Fatal(err)
	}
	handler = syncHandler(journal, "token")
	if post(path, archive) != 202 {
		t.Fatal("no-op receipt retry")
	}
	recorder = httptest.NewRecorder()
	handler.ServeHTTP(recorder, req)
	if json.Unmarshal(recorder.Body.Bytes(), &entries) != nil || len(entries) != 1 || !entries[0].Active {
		t.Fatalf("no-op retry undid restore: %s", recorder.Body.String())
	}
}

func TestDesktopAttachmentArchiveOutboxKeepsOperationID(t *testing.T) {
	journal := filepath.Join(t.TempDir(), "outbox")
	var received []syncEvent
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/desktop-attachment-events" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		var event syncEvent
		if json.NewDecoder(r.Body).Decode(&event) != nil {
			t.Error("invalid event")
		}
		received = append(received, event)
		if len(received) == 1 {
			w.WriteHeader(http.StatusServiceUnavailable)
		} else {
			w.WriteHeader(http.StatusAccepted)
		}
	}))
	defer server.Close()
	t.Setenv("AURA_SYNC_JOURNAL", journal)
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "token")
	t.Setenv("AURA_SYNC_CLIENT_ID", "desktop-a")
	if publishEvent(syncEvent{Topic: "attachment.archived", Payload: `{"id":"a"}`}) == 0 {
		t.Fatal("expected failed send")
	}
	if flushOutbox() != 0 {
		t.Fatal("retry failed")
	}
	if len(received) != 2 || received[0].OperationID == "" || received[0].OperationID != received[1].OperationID || received[0].ClientID != "desktop-a" {
		t.Fatalf("operation not preserved: %+v", received)
	}
}

func TestDesktopOutboxStatusReportsConflictAndRecovery(t *testing.T) {
	status := http.StatusConflict
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(status) }))
	defer server.Close()
	journal := filepath.Join(t.TempDir(), "outbox")
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "token")
	t.Setenv("AURA_SYNC_JOURNAL", journal)
	if err := persistEvent(journal, syncEvent{Topic: "task.updated", Payload: `{}`}); err != nil {
		t.Fatal(err)
	}
	code := flushOutbox()
	setDesktopSyncStatus(code)
	if code != 5 {
		t.Fatalf("conflict code %d", code)
	}
	eventLog.RLock()
	message := eventLog.latest
	eventLog.RUnlock()
	if !strings.Contains(message, "Konflikt") {
		t.Fatalf("status: %s", message)
	}
	queued, _, err := changesSince(journal, 0)
	if err != nil || len(queued) != 1 {
		t.Fatalf("outbox lost: %+v %v", queued, err)
	}
	status = http.StatusAccepted
	code = flushOutbox()
	setDesktopSyncStatus(code)
	eventLog.RLock()
	message = eventLog.latest
	eventLog.RUnlock()
	if code != 0 || !strings.Contains(message, "abgeschlossen") {
		t.Fatalf("recovery: %d %s", code, message)
	}
}

func TestDesktopAttachmentAbandonRetainsUnrelatedOutbox(t *testing.T) {
	dir := t.TempDir()
	journal := filepath.Join(dir, "server")
	store, err := openCentralStore(journal + ".sqlite")
	if err != nil {
		t.Fatal(err)
	}
	previous := centralMirror
	centralMirror = store
	defer func() { centralMirror = previous; store.close() }()
	task := `{"id":"task-1","title":"Task","department":"IT","priority":"Mittel","status":"open","assigneeId":1,"createdBy":1,"active":true}`
	remote := centralAttachment{ID: "desktop:attachment", SubjectType: "task", SubjectID: "task-1", OriginalName: "file.txt", SizeBytes: 5, SHA256: strings.Repeat("a", 64), UploadedBy: "desktop:employee", Active: true}
	payload, _ := json.Marshal(remote)
	for _, event := range []syncEvent{{Topic: "task.created", Payload: task}, {Topic: "attachment.created", Payload: string(payload)}} {
		if err := store.append(event); err != nil {
			t.Fatal(err)
		}
	}
	handler := syncHandler(journal, "token")
	server := httptest.NewServer(handler)
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "token")
	t.Setenv("AURA_SYNC_CLIENT_ID", "desktop")
	outbox := filepath.Join(dir, "outbox")
	t.Setenv("AURA_SYNC_JOURNAL", outbox)
	archived := remote
	archived.Active = false
	archivePayload, _ := json.Marshal(archived)
	event := syncEvent{Topic: "attachment.archived", Payload: string(archivePayload), ClientID: "desktop", OperationID: "op-1"}
	next := syncEvent{Topic: "task.updated", Payload: `{"id":"another"}`}
	first, _ := json.Marshal(event)
	second, _ := json.Marshal(next)
	initial := append(append(first, '\n'), append(second, '\n')...)
	if err := os.WriteFile(outbox, initial, 0600); err != nil {
		t.Fatal(err)
	}
	review, code := inspectDesktopAttachmentArchive()
	if code != 0 || review.Expected != remote || review.Event != event {
		t.Fatalf("inspect: %+v code %d", review, code)
	}
	bad := review
	bad.Expected.Active = false
	if code := abandonDesktopAttachmentArchive(bad, false); code != 5 {
		t.Fatalf("stale review: %d", code)
	}
	unchanged, err := os.ReadFile(outbox)
	if err != nil || !bytes.Equal(unchanged, initial) {
		t.Fatal("outbox changed on rejected review")
	}
	duplicate := event
	duplicate.OperationID = "op-2"
	duplicateJSON, _ := json.Marshal(duplicate)
	withLater := append(append([]byte{}, initial...), append(duplicateJSON, '\n')...)
	if err := os.WriteFile(outbox, withLater, 0600); err != nil {
		t.Fatal(err)
	}
	if code := abandonDesktopAttachmentArchive(review, false); code != 5 {
		t.Fatalf("later operation accepted: %d", code)
	}
	if err := os.WriteFile(outbox, initial, 0600); err != nil {
		t.Fatal(err)
	}
	if code := abandonDesktopAttachmentArchive(review, false); code != 0 {
		t.Fatalf("prepare: %d", code)
	}
	unchanged, err = os.ReadFile(outbox)
	if err != nil || !bytes.Equal(unchanged, initial) {
		t.Fatal("prepare removed outbox before local apply")
	}
	if code := abandonDesktopAttachmentArchive(review, true); code != 0 {
		t.Fatalf("abandon: %d", code)
	}
	remainder, err := os.ReadFile(outbox)
	if err != nil || !bytes.Equal(remainder, append(second, '\n')) {
		t.Fatalf("outbox remainder: %q %v", remainder, err)
	}
	req := httptest.NewRequest(http.MethodPost, "/api/v1/desktop-attachment-events", bytes.NewReader(first))
	req.Header.Set("Authorization", "Bearer token")
	result := httptest.NewRecorder()
	handler.ServeHTTP(result, req)
	if result.Code != http.StatusConflict {
		t.Fatalf("late event: %d", result.Code)
	}
}

func TestProjectStatusRequiresPermissionAndExpectedStatus(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":1,"permissions":["projects.manage"]},"reader":{"employeeId":2,"permissions":["projects.read"]}}`)
	handler := syncHandler(filepath.Join(t.TempDir(), "events"), "token")
	request := func(method, path, token, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+token)
		out := httptest.NewRecorder()
		handler.ServeHTTP(out, req)
		return out
	}
	created := request(http.MethodPost, "/api/v1/projects", "manager", `{"name":"Original","department":"IT","description":"Original","status":"planned","ownerId":1}`)
	var project centralProject
	if created.Code != 201 || json.Unmarshal(created.Body.Bytes(), &project) != nil {
		t.Fatalf("create: %d %s", created.Code, created.Body.String())
	}
	update := request(http.MethodPut, "/api/v1/projects/"+project.ID, "manager", `{"name":"Neu","department":"IT","description":"Neuer Inhalt","status":"planned","ownerId":1}`)
	if update.Code != 200 {
		t.Fatalf("update: %d", update.Code)
	}
	body := fmt.Sprintf(`{"id":%q,"expected":"planned","status":"active"}`, project.ID)
	if result := request(http.MethodPost, "/api/v1/project-status", "reader", body); result.Code != 403 {
		t.Fatalf("permission: %d", result.Code)
	}
	result := request(http.MethodPost, "/api/v1/project-status", "manager", body)
	if result.Code != 200 || json.Unmarshal(result.Body.Bytes(), &project) != nil || project.Name != "Neu" || project.Description != "Neuer Inhalt" || project.Status != "active" {
		t.Fatalf("status: %d %s", result.Code, result.Body.String())
	}
	if result = request(http.MethodPost, "/api/v1/project-status", "manager", body); result.Code != 409 {
		t.Fatalf("stale: %d", result.Code)
	}
}

func TestRoomAvailabilityIncludesBookingsAndUnknownLegacyRooms(t *testing.T) {
	dir := t.TempDir()
	store, err := openCentralStore(filepath.Join(dir, "central.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	previous := centralMirror
	centralMirror = store
	defer func() { centralMirror = previous; store.close() }()
	for _, room := range []centralRoom{{ID: "r1", Name: "A", Location: "Berlin", Capacity: 4, Active: true}, {ID: "r2", Name: "B", Location: "Berlin", Capacity: 4, Active: true}} {
		payload, _ := json.Marshal(room)
		if err := store.append(syncEvent{Topic: "room.created", Payload: string(payload)}); err != nil {
			t.Fatal(err)
		}
	}
	now := time.Now().Unix()
	booking := bookingPayload{ClientID: "test", BookingID: 1, RoomID: 1, RoomKey: "r1", StartsAt: now - 30, EndsAt: now + 300, Status: "blocked"}
	payload, _ := json.Marshal(booking)
	if err := store.append(syncEvent{Topic: "booking.created", Payload: string(payload)}); err != nil {
		t.Fatal(err)
	}
	get := func() map[string]portable.Room {
		handler := syncHandler(filepath.Join(dir, "events"), "token")
		req := httptest.NewRequest(http.MethodGet, "/api/v1/rooms?availability=true", nil)
		req.Header.Set("Authorization", "Bearer token")
		out := httptest.NewRecorder()
		handler.ServeHTTP(out, req)
		var rooms []portable.Room
		if out.Code != 200 || json.Unmarshal(out.Body.Bytes(), &rooms) != nil {
			t.Fatalf("rooms %d %s", out.Code, out.Body.String())
		}
		result := map[string]portable.Room{}
		for _, room := range rooms {
			result[room.ID] = room
		}
		return result
	}
	states := get()
	if states["r1"].Occupied == nil || !*states["r1"].Occupied || states["r1"].Availability != "blocked" || states["r2"].Occupied == nil || *states["r2"].Occupied || states["r2"].Availability != "free" {
		t.Fatalf("known rooms: %+v", states)
	}
	booking.BookingID = 2
	booking.RoomKey = ""
	payload, _ = json.Marshal(booking)
	if err := store.append(syncEvent{Topic: "booking.created", Payload: string(payload)}); err != nil {
		t.Fatal(err)
	}
	states = get()
	if states["r1"].Occupied == nil || !*states["r1"].Occupied || states["r2"].Occupied != nil || states["r2"].Availability != "unknown" {
		t.Fatalf("unmapped legacy booking: %+v", states)
	}
}

func TestDesktopAttachmentUploadQueuedUntilVerifiedAndPublished(t *testing.T) {
	dir := t.TempDir()
	journal := filepath.Join(dir, "server.ndjson")
	handler := syncHandler(journal, "token")
	failed := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/api/v1/attachments/") && r.Method == http.MethodPut && !failed {
			failed = true
			http.Error(w, "offline", 503)
			return
		}
		handler.ServeHTTP(w, r)
	}))
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "token")
	t.Setenv("AURA_SYNC_CLIENT_ID", "desktop")
	t.Setenv("AURA_SYNC_JOURNAL", filepath.Join(dir, "outbox"))
	task := syncEvent{Topic: "task.created", Payload: `{"id":"task-1","title":"Task","department":"IT","priority":"Mittel","status":"open","assigneeId":1,"createdBy":1,"active":true}`}
	data, _ := json.Marshal(task)
	req, _ := http.NewRequest(http.MethodPost, server.URL+"/events", bytes.NewReader(data))
	req.Header.Set("Authorization", "Bearer token")
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != 202 {
		t.Fatalf("task: %d", response.StatusCode)
	}
	content := []byte("offline attachment")
	sum := sha256.Sum256(content)
	digest := hex.EncodeToString(sum[:])
	path := filepath.Join(dir, "evidence.txt")
	if err := os.WriteFile(path, content, 0600); err != nil {
		t.Fatal(err)
	}
	metadata, _ := json.Marshal(centralAttachment{ID: "desktop:attachment", SubjectType: "task", SubjectID: "task-1", OriginalName: "evidence.txt", SizeBytes: int64(len(content)), SHA256: digest, UploadedBy: "desktop:employee", Active: true})
	pending, _ := json.Marshal(desktopAttachmentUpload{Path: path, Metadata: string(metadata)})
	if publishEvent(syncEvent{Topic: "desktop.attachment.upload", Payload: string(pending)}) == 0 {
		t.Fatal("first upload must fail")
	}
	if err := os.WriteFile(path, []byte("different content!"), 0600); err != nil {
		t.Fatal(err)
	}
	if flushOutbox() == 0 {
		t.Fatal("modified attachment accepted")
	}
	if err := os.WriteFile(path, content, 0600); err != nil {
		t.Fatal(err)
	}
	if code := flushOutbox(); code != 0 {
		t.Fatalf("retry: %d", code)
	}
	events, _, err := changesSince(journal, 0)
	if err != nil {
		t.Fatal(err)
	}
	count := 0
	for _, event := range events {
		if event.Topic == "attachment.created" {
			count++
			if strings.Contains(event.Payload, path) {
				t.Fatal("local path exposed to server")
			}
		}
	}
	if count != 1 {
		t.Fatalf("metadata events: %d", count)
	}
	if _, err := os.Stat(filepath.Join(journal+".attachments", digest)); err != nil {
		t.Fatal(err)
	}
	outbox, err := os.ReadFile(filepath.Join(dir, "outbox"))
	if err != nil || len(outbox) != 0 {
		t.Fatalf("outbox: %q %v", outbox, err)
	}
}

func TestDesktopAttachmentSnapshotScopesAndCursor(t *testing.T) {
	journal := filepath.Join(t.TempDir(), "events")
	store, err := openCentralStore(journal + ".sqlite")
	if err != nil {
		t.Fatal(err)
	}
	previous := centralMirror
	centralMirror = store
	defer func() { centralMirror = previous; store.close() }()
	t.Setenv("AURA_SYNC_IDENTITIES", `{"task":{"employeeId":1,"permissions":["tasks.manage","sync.events.read"]},"reader":{"employeeId":2,"permissions":["sync.events.read"]}}`)
	items := []centralAttachment{
		{ID: "device-a:1", SubjectType: "task", SubjectID: "task-1", OriginalName: "a.txt", SizeBytes: 1, SHA256: strings.Repeat("a", 64), UploadedBy: "employee-a", Active: false},
		{ID: "device-a:2", SubjectType: "ticket", SubjectID: "ticket-1", OriginalName: "b.txt", SizeBytes: 1, SHA256: strings.Repeat("b", 64), UploadedBy: "employee-a", Active: true},
		{ID: "device-b:3", SubjectType: "task", SubjectID: "task-1", OriginalName: "c.txt", SizeBytes: 1, SHA256: strings.Repeat("c", 64), UploadedBy: "employee-a", Active: true},
	}
	for _, item := range items {
		payload, _ := json.Marshal(item)
		if err := store.append(syncEvent{Topic: "attachment.created", Payload: string(payload), At: "2026-01-01T00:00:00Z"}); err != nil {
			t.Fatal(err)
		}
	}
	handler := syncHandler(journal, "token")
	request := func(token, clientID string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/desktop-attachment-snapshot?clientId="+clientID, nil)
		req.Header.Set("Authorization", "Bearer "+token)
		out := httptest.NewRecorder()
		handler.ServeHTTP(out, req)
		return out
	}
	if result := request("reader", "device-a"); result.Code != 403 {
		t.Fatalf("unauthorized: %d", result.Code)
	}
	result := request("task", "device-a")
	var snapshot struct {
		Cursor      int64               `json:"cursor"`
		Attachments []centralAttachment `json:"attachments"`
	}
	if result.Code != 200 || json.Unmarshal(result.Body.Bytes(), &snapshot) != nil || snapshot.Cursor != 3 || len(snapshot.Attachments) != 1 || snapshot.Attachments[0].ID != "device-a:1" || snapshot.Attachments[0].Active {
		t.Fatalf("snapshot: %d %s", result.Code, result.Body.String())
	}
}

func TestDesktopAttachmentSnapshotErrorCodes(t *testing.T) {
	status := http.StatusNotFound
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(status) }))
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "token")
	t.Setenv("AURA_SYNC_CLIENT_ID", "client")
	for _, test := range []struct{ status, code int }{{404, 11}, {403, 12}, {409, 8}, {500, 8}} {
		status = test.status
		if _, _, code := desktopAttachmentSnapshot(); code != test.code {
			t.Fatalf("status %d: got %d", status, code)
		}
	}
}

func TestAttachmentMetadataRequiresCentralObjectAndReplays(t *testing.T) {
	journal := filepath.Join(t.TempDir(), "attachment-events.ndjson")
	server := httptest.NewServer(syncHandler(journal, "token"))
	defer server.Close()
	content := []byte("synchronisierter Nachweis")
	sum := sha256.Sum256(content)
	digest := hex.EncodeToString(sum[:])
	upload := func(method, path, body string) int {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer token")
		if method == http.MethodPut {
			req.Header.Set("X-Aura-SHA256", digest)
		}
		response := httptest.NewRecorder()
		syncHandler(journal, "token").ServeHTTP(response, req)
		return response.Code
	}
	// Use the same in-memory server handler through HTTP for the object first.
	req, _ := http.NewRequest(http.MethodPut, server.URL+"/api/v1/attachments/"+digest, bytes.NewReader(content))
	req.Header.Set("Authorization", "Bearer token")
	req.Header.Set("X-Aura-SHA256", digest)
	if response, err := http.DefaultClient.Do(req); err != nil || response.StatusCode != http.StatusCreated {
		if response != nil {
			response.Body.Close()
		}
		t.Fatalf("central object upload: %v", err)
	} else {
		response.Body.Close()
	}
	_ = upload
	task := `{"topic":"task.created","payload":"{\"id\":\"task-evidence\",\"title\":\"Nachweis\",\"department\":\"IT\",\"priority\":\"Mittel\",\"status\":\"open\",\"assigneeId\":1,\"createdBy\":1,\"active\":true}"}`
	req, _ = http.NewRequest(http.MethodPost, server.URL+"/events", strings.NewReader(task))
	req.Header.Set("Authorization", "Bearer token")
	if response, err := http.DefaultClient.Do(req); err != nil || response.StatusCode != http.StatusAccepted {
		if response != nil {
			response.Body.Close()
		}
		t.Fatalf("task event: %v", err)
	} else {
		response.Body.Close()
	}
	attachment := `{"topic":"attachment.created","payload":"{\"id\":\"attachment-evidence\",\"subjectType\":\"task\",\"subjectId\":\"task-evidence\",\"originalName\":\"nachweis.txt\",\"sizeBytes\":24,\"sha256\":\"` + digest + `\",\"uploadedBy\":\"employee-1\",\"active\":true}"}`
	req, _ = http.NewRequest(http.MethodPost, server.URL+"/events", strings.NewReader(attachment))
	req.Header.Set("Authorization", "Bearer token")
	if response, err := http.DefaultClient.Do(req); err != nil || response.StatusCode != http.StatusAccepted {
		if response != nil {
			response.Body.Close()
		}
		t.Fatalf("attachment event: %v", err)
	} else {
		response.Body.Close()
	}
	feed, _ := http.NewRequest(http.MethodGet, server.URL+"/api/v1/sync/changes?cursor=0", nil)
	feed.Header.Set("Authorization", "Bearer token")
	response, err := http.DefaultClient.Do(feed)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var result struct {
		Events []syncEvent `json:"events"`
	}
	if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, event := range result.Events {
		if event.Topic == "attachment.created" && strings.Contains(event.Payload, digest) {
			found = true
		}
	}
	if !found {
		t.Fatalf("attachment metadata missing from change feed: %#v", result.Events)
	}
}

func TestPersistEventWritesJsonLine(t *testing.T) {
	path := filepath.Join(t.TempDir(), "outbox.ndjson")
	if err := persistEvent(path, syncEvent{Topic: "booking.created", Payload: `{"bookingId":7}`, At: "2026-10-26T10:00:00Z"}); err != nil {
		t.Fatal(err)
	}
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(content), `"topic":"booking.created"`) || !strings.HasSuffix(string(content), "\n") {
		t.Fatalf("unexpected journal: %q", content)
	}
}

func TestSyncHandlerRequiresTokenAndPersistsEvent(t *testing.T) {
	path := filepath.Join(t.TempDir(), "server-outbox.ndjson")
	handler := syncHandler(path, "top-secret")
	forbidden := httptest.NewRequest(http.MethodPost, "/events", bytes.NewBufferString(`{"topic":"booking.created"}`))
	if response := httptest.NewRecorder(); func() int { handler.ServeHTTP(response, forbidden); return response.Code }() != http.StatusUnauthorized {
		t.Fatal("missing token must be rejected")
	}
	request := httptest.NewRequest(http.MethodPost, "/events", bytes.NewBufferString(`{"topic":"task.updated","payload":"{\"id\":\"task-1\",\"title\":\"Server-Prüfung\",\"department\":\"IT\",\"priority\":\"Mittel\",\"status\":\"open\",\"assigneeId\":1,\"createdBy\":1,\"active\":true}"}`))
	request.Header.Set("Authorization", "Bearer top-secret")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusAccepted {
		t.Fatalf("unexpected status %d", response.Code)
	}
	if response.Header().Get("Cache-Control") != "no-store" || response.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Fatalf("security headers missing: %#v", response.Header())
	}
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(content), "task.updated") {
		t.Fatalf("event not persisted: %q", content)
	}
}

func TestSyncChangeFeedReturnsOnlyEventsAfterCursor(t *testing.T) {
	path := filepath.Join(t.TempDir(), "changes.ndjson")
	if err := persistEvent(path, syncEvent{Topic: "leave.requested", Payload: `{"id":"leave-1"}`}); err != nil {
		t.Fatal(err)
	}
	if err := persistEvent(path, syncEvent{Topic: "leave.decided", Payload: `{"id":"leave-1"}`}); err != nil {
		t.Fatal(err)
	}
	handler := syncHandler(path, "token")
	request := httptest.NewRequest(http.MethodGet, "/api/v1/sync/changes?cursor=1", nil)
	request.Header.Set("Authorization", "Bearer token")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), "leave.decided") || strings.Contains(response.Body.String(), "leave.requested") {
		t.Fatalf("unexpected change feed: %d %s", response.Code, response.Body.String())
	}
}

func TestFetchChangesUsesAuthenticatedCursorFeed(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/api/v1/sync/changes" || request.URL.Query().Get("cursor") != "4" || request.Header.Get("Authorization") != "Bearer device-token" {
			http.Error(writer, "bad request", 400)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`{"events":[{"topic":"room.created","payload":"{}","at":"2026-10-26T10:00:00Z"}],"nextCursor":5}`))
	}))
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "device-token")
	events, next, code := fetchChanges(4)
	if code != 0 || next != 5 || len(events) != 1 || events[0].Topic != "room.created" {
		t.Fatalf("unexpected change batch: events=%#v next=%d code=%d", events, next, code)
	}
}

func TestFetchChangesAcceptsFeedLargerThanOneMiB(t *testing.T) {
	largePayload := strings.Repeat("a", 2<<20)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		_ = json.NewEncoder(writer).Encode(struct {
			Events     []syncEvent `json:"events"`
			NextCursor int         `json:"nextCursor"`
		}{[]syncEvent{{Topic: "task.updated", Payload: largePayload}}, 5})
	}))
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "device-token")
	events, next, code := fetchChanges(4)
	if code != 0 || next != 5 || len(events) != 1 || events[0].Payload != largePayload {
		t.Fatalf("large feed: code=%d next=%d events=%d", code, next, len(events))
	}
	encoded, err := json.Marshal(events)
	if err != nil || len(encoded) <= 1<<20 || len(encoded) >= 8<<20 {
		t.Fatalf("unexpected encoded feed size: %d, %v", len(encoded), err)
	}
}

func TestConfiguredIdentitiesCannotBeChosenByRequestBody(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"employee-token":{"employeeId":7,"permissions":["leave.request"]},"manager-token":{"employeeId":9,"permissions":["leave.approve"]}}`)
	employee, ok := identityFor("legacy-token", "Bearer employee-token")
	if !ok || employee.EmployeeID != 7 || !allows(employee, "leave.request") {
		t.Fatal("employee identity was not server-resolved")
	}
	if _, ok := identityFor("legacy-token", "Bearer unknown-token"); ok {
		t.Fatal("unknown identity token accepted")
	}
}

func TestInvalidConfiguredIdentitiesFailClosed(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{invalid-json`)
	if _, ok := identityFor("legacy-token", "Bearer legacy-token"); ok {
		t.Fatal("malformed identity configuration fell back to legacy administrator")
	}
	t.Setenv("AURA_SYNC_IDENTITIES", `{"token":{"employeeId":0,"permissions":[]}}`)
	if _, ok := identityFor("legacy-token", "Bearer token"); ok {
		t.Fatal("invalid configured identity was accepted")
	}
}

func TestGenericEventsRequireTopicPermission(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"leave-user":{"employeeId":7,"permissions":["leave.request"]},"room-user":{"employeeId":8,"permissions":["rooms.book"]}}`)
	handler := syncHandler(filepath.Join(t.TempDir(), "event-permissions.ndjson"), "legacy")
	post := func(token, body string) int {
		request := httptest.NewRequest(http.MethodPost, "/events", bytes.NewBufferString(body))
		request.Header.Set("Authorization", "Bearer "+token)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response.Code
	}
	if status := post("leave-user", `{"topic":"booking.created","payload":"{\"clientId\":\"client-a\",\"bookingId\":1,\"roomId\":3,\"startsAt\":100,\"endsAt\":200}"}`); status != http.StatusForbidden {
		t.Fatalf("unprivileged booking event accepted: %d", status)
	}
	if status := post("room-user", `{"topic":"booking.created","payload":"{\"clientId\":\"client-a\",\"bookingId\":1,\"roomId\":3,\"startsAt\":100,\"endsAt\":200}"}`); status != http.StatusAccepted {
		t.Fatalf("privileged booking event rejected: %d", status)
	}
}

func TestCentralLeaveWorkflowUsesTokenIdentityAndRejectsSelfApproval(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"employee-token":{"employeeId":7,"permissions":["leave.request"]},"manager-token":{"employeeId":9,"permissions":["leave.approve"]}}`)
	path := filepath.Join(t.TempDir(), "leaves.ndjson")
	handler := syncHandler(path, "legacy-token")
	post := func(token, route, body string) *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, route, bytes.NewBufferString(body))
		request.Header.Set("Authorization", "Bearer "+token)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response
	}
	created := post("employee-token", "/api/v1/leave-requests", `{"startsAt":100,"endsAt":200,"reason":"Erholung","employeeId":999}`)
	if created.Code != http.StatusCreated || !strings.Contains(created.Body.String(), `"employeeId":7`) {
		t.Fatalf("leave request was not bound to identity: %d %s", created.Code, created.Body.String())
	}
	var leave leaveRequest
	if err := json.Unmarshal(created.Body.Bytes(), &leave); err != nil {
		t.Fatal(err)
	}
	if response := post("employee-token", "/api/v1/leave-requests/"+leave.ID+"/decision", `{"decision":"approved"}`); response.Code != http.StatusForbidden {
		t.Fatalf("self approval status %d", response.Code)
	}
	stale := leave
	stale.EndsAt++
	bad, _ := json.Marshal(map[string]any{"decision": "approved", "expected": stale, "expectedEmployeeExternalId": ""})
	if response := post("manager-token", "/api/v1/leave-requests/"+leave.ID+"/decision", string(bad)); response.Code != http.StatusConflict {
		t.Fatalf("stale decision: %d %s", response.Code, response.Body.String())
	}
	badIdentity, _ := json.Marshal(map[string]any{"decision": "approved", "expected": leave, "expectedEmployeeExternalId": "wrong-person"})
	if response := post("manager-token", "/api/v1/leave-requests/"+leave.ID+"/decision", string(badIdentity)); response.Code != http.StatusConflict {
		t.Fatalf("stale identity: %d %s", response.Code, response.Body.String())
	}
	good, _ := json.Marshal(map[string]any{"decision": "approved", "expected": leave, "expectedEmployeeExternalId": ""})
	if response := post("manager-token", "/api/v1/leave-requests/"+leave.ID+"/decision", string(good)); response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"status":"approved"`) {
		t.Fatalf("manager decision failed: %d %s", response.Code, response.Body.String())
	}
}

func TestPortableLeaveRequestManagerApprovalAndChangeFeed(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"android":{"employeeId":42,"permissions":["leave.request"]},"manager":{"employeeId":7,"permissions":["leave.approve"]}}`)
	handler := syncHandler(filepath.Join(t.TempDir(), "portable-leave.ndjson"), "legacy")
	server := httptest.NewServer(handler)
	defer server.Close()
	employee := portable.Client{BaseURL: server.URL, Token: "android"}
	manager := portable.Client{BaseURL: server.URL, Token: "manager"}
	request, err := employee.CreateLeave(context.Background(), 1000, 2000, "Erholung")
	if err != nil {
		t.Fatal(err)
	}
	if request.EmployeeID != 42 || request.Status != "pending" {
		t.Fatalf("request identity/status incorrect: %#v", request)
	}
	approved, err := manager.DecideLeave(context.Background(), request.ID, "approved")
	if err != nil {
		t.Fatal(err)
	}
	if approved.Status != "approved" || approved.DecidedBy != 7 {
		t.Fatalf("approval incorrect: %#v", approved)
	}
	feed, err := employee.Changes(context.Background(), 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(feed.Events) != 2 || feed.Events[0].Topic != "leave.requested" || feed.Events[1].Topic != "leave.decided" {
		t.Fatalf("leave feed incorrect: %#v", feed)
	}
}

func TestCentralStoreMigratesAndPersistsEvents(t *testing.T) {
	store, err := openCentralStore(filepath.Join(t.TempDir(), "central.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.close()
	if err := store.append(syncEvent{Topic: "leave.requested", Payload: `{"id":"leave-1"}`, At: "2026-10-26T10:00:00Z"}); err != nil {
		t.Fatal(err)
	}
	events, cursor, err := store.eventsAfter(0)
	if err != nil || cursor != 1 || len(events) != 1 || events[0].Topic != "leave.requested" {
		t.Fatalf("durable cursor feed: events=%v cursor=%d err=%v", events, cursor, err)
	}
}

func TestAuthoritativeStoreSurvivesCompatibilityJournalFailure(t *testing.T) {
	dir := t.TempDir()
	store, err := openCentralStore(filepath.Join(dir, "central.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.close()
	previous := centralMirror
	centralMirror = store
	defer func() { centralMirror = previous }()
	if err := persistEvent(dir, syncEvent{Topic: "task.created", Payload: `{"id":"task-1"}`, At: "2026-10-26T10:00:00Z"}); err != nil {
		t.Fatalf("authoritative event rejected after mirror failure: %v", err)
	}
	events, cursor, err := store.eventsAfter(0)
	if err != nil || cursor != 1 || len(events) != 1 || events[0].Topic != "task.created" {
		t.Fatalf("authoritative event missing: events=%v cursor=%d err=%v", events, cursor, err)
	}
}

func TestCentralDatabaseIsAuthoritativeAcrossServerRestart(t *testing.T) {
	dir := t.TempDir()
	journal := filepath.Join(dir, "mirror.ndjson")
	if err := os.WriteFile(journal, []byte(`{"topic":"room.created","payload":"{\\"id\\":\\"journal-only\\",\\"name\\":\\"Wrong\\",\\"location\\":\\"X\\",\\"capacity\\":1,\\"active\\":true}","at":"2026-10-26T10:00:00Z"}`+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	store, err := openCentralStore(filepath.Join(dir, "central.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.close()
	if err := store.append(syncEvent{Topic: "room.created", Payload: `{"id":"db-room","name":"Alpha","location":"Berlin","capacity":12,"active":true}`, At: "2026-10-26T10:00:01Z"}); err != nil {
		t.Fatal(err)
	}
	previous := centralMirror
	centralMirror = store
	defer func() { centralMirror = previous }()
	handler := syncHandler(journal, "token")
	request := httptest.NewRequest(http.MethodGet, "/api/v1/rooms", nil)
	request.Header.Set("Authorization", "Bearer token")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), "db-room") || strings.Contains(response.Body.String(), "journal-only") {
		t.Fatalf("SQLite authority not used: %d %s", response.Code, response.Body.String())
	}
	request = httptest.NewRequest(http.MethodGet, "/api/v1/sync/changes?cursor=0", nil)
	request.Header.Set("Authorization", "Bearer token")
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), "db-room") || strings.Contains(response.Body.String(), "journal-only") {
		t.Fatalf("central cursor feed not used: %d %s", response.Code, response.Body.String())
	}
}

func TestCentralTaskApiRequiresManagerAndFiltersEmployeeView(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage","tasks.read"]},"employee":{"employeeId":7,"permissions":["tasks.read"]}}`)
	handler := syncHandler(filepath.Join(t.TempDir(), "tasks.ndjson"), "legacy")
	post := func(token, body string) *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, "/api/v1/tasks", bytes.NewBufferString(body))
		request.Header.Set("Authorization", "Bearer "+token)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response
	}
	if response := post("employee", `{"title":"Unzulässig","department":"IT","priority":"Hoch"}`); response.Code != http.StatusForbidden {
		t.Fatalf("employee created task: %d", response.Code)
	}
	if response := post("manager", `{"title":"Inventur","department":"IT","priority":"Hoch","assigneeId":7}`); response.Code != http.StatusCreated {
		t.Fatalf("manager task: %d %s", response.Code, response.Body.String())
	}
	request := httptest.NewRequest(http.MethodGet, "/api/v1/tasks", nil)
	request.Header.Set("Authorization", "Bearer employee")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), "Inventur") {
		t.Fatalf("employee task view: %d %s", response.Code, response.Body.String())
	}
}

func TestCentralTaskAndTicketStatusTransitionsAreValidated(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage","tickets.manage"]}}`)
	handler := syncHandler(filepath.Join(t.TempDir(), "workflow.ndjson"), "legacy")
	request := func(route, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(http.MethodPost, route, bytes.NewBufferString(body))
		r.Header.Set("Authorization", "Bearer manager")
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	task := request("/api/v1/tasks", `{"title":"Inventur","department":"IT","priority":"Hoch"}`)
	if task.Code != http.StatusCreated {
		t.Fatalf("task create: %d", task.Code)
	}
	var createdTask centralTask
	_ = json.Unmarshal(task.Body.Bytes(), &createdTask)
	if response := request("/api/v1/tasks/"+createdTask.ID+"/status", `{"status":"open"}`); response.Code != http.StatusConflict {
		t.Fatalf("invalid task transition: %d", response.Code)
	}
	if response := request("/api/v1/tasks/"+createdTask.ID+"/status", `{"status":"in_progress"}`); response.Code != http.StatusOK {
		t.Fatalf("valid task transition: %d", response.Code)
	}
	for _, status := range []string{"done", "open", "done", "in_progress"} {
		if response := request("/api/v1/tasks/"+createdTask.ID+"/status", `{"status":"`+status+`"}`); response.Code != http.StatusOK {
			t.Fatalf("task transition to %s: %d %s", status, response.Code, response.Body.String())
		}
	}
	ticket := request("/api/v1/tickets", `{"title":"Drucker","category":"IT","priority":"Mittel"}`)
	if ticket.Code != http.StatusCreated {
		t.Fatalf("ticket create: %d", ticket.Code)
	}
	var createdTicket centralTicket
	_ = json.Unmarshal(ticket.Body.Bytes(), &createdTicket)
	if response := request("/api/v1/tickets/"+createdTicket.ID+"/status", `{"status":"resolved"}`); response.Code != http.StatusConflict {
		t.Fatalf("invalid ticket transition: %d", response.Code)
	}
	if response := request("/api/v1/tickets/"+createdTicket.ID+"/status", `{"status":"in_progress"}`); response.Code != http.StatusOK {
		t.Fatalf("valid ticket transition: %d", response.Code)
	}
	for _, status := range []string{"open", "in_progress", "resolved", "closed", "open"} {
		if response := request("/api/v1/tickets/"+createdTicket.ID+"/status", `{"status":"`+status+`"}`); response.Code != http.StatusOK {
			t.Fatalf("ticket transition to %s: %d %s", status, response.Code, response.Body.String())
		}
	}
}

func TestCentralProjectsArePermissionCheckedArchivedAndReplayed(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["projects.manage","projects.read"]},"employee":{"employeeId":7,"permissions":["projects.read"]}}`)
	path := filepath.Join(t.TempDir(), "projects.ndjson")
	handler := syncHandler(path, "legacy")
	call := func(token, method, route, body string) *httptest.ResponseRecorder {
		request := httptest.NewRequest(method, route, bytes.NewBufferString(body))
		request.Header.Set("Authorization", "Bearer "+token)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response
	}
	if response := call("employee", http.MethodPost, "/api/v1/projects", `{"name":"Unzulässig","department":"IT","status":"planned"}`); response.Code != http.StatusForbidden {
		t.Fatalf("employee created project: %d", response.Code)
	}
	created := call("manager", http.MethodPost, "/api/v1/projects", `{"name":"Migration","department":"IT","description":"ERP-Ablösung","status":"planned","ownerId":7}`)
	if created.Code != http.StatusCreated {
		t.Fatalf("project create: %d %s", created.Code, created.Body.String())
	}
	var project centralProject
	if json.Unmarshal(created.Body.Bytes(), &project) != nil || project.ID == "" {
		t.Fatal("missing project identity")
	}
	visible := call("employee", http.MethodGet, "/api/v1/projects", "")
	if visible.Code != http.StatusOK || !strings.Contains(visible.Body.String(), project.ID) {
		t.Fatalf("project owner cannot read own project: %d %s", visible.Code, visible.Body.String())
	}
	updated := call("manager", http.MethodPut, "/api/v1/projects/"+project.ID, `{"name":"Migration","department":"IT","description":"ERP-Ablösung","status":"active","ownerId":7}`)
	if updated.Code != http.StatusOK || !strings.Contains(updated.Body.String(), `"status":"active"`) {
		t.Fatalf("project update: %d %s", updated.Code, updated.Body.String())
	}
	if archived := call("manager", http.MethodPost, "/api/v1/projects/"+project.ID+"/archive", ""); archived.Code != http.StatusNoContent {
		t.Fatalf("project archive: %d", archived.Code)
	}
	replayed := syncHandler(path, "legacy")
	history := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	history.Header.Set("Authorization", "Bearer manager")
	response := httptest.NewRecorder()
	replayed.ServeHTTP(response, history)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"active":false`) || !strings.Contains(response.Body.String(), `"status":"active"`) {
		t.Fatalf("project replay failed: %d %s", response.Code, response.Body.String())
	}
}

func TestPortableClientUsesActualCentralProjectContract(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["projects.manage","projects.read"]}}`)
	server := httptest.NewServer(syncHandler(filepath.Join(t.TempDir(), "portable-project.ndjson"), "legacy"))
	defer server.Close()
	client := portable.Client{BaseURL: server.URL, Token: "manager"}
	project, err := client.CreateProject(context.Background(), "Rollout", "Operations", "Standorte aktivieren", "planned", 9)
	if err != nil || project.ID == "" {
		t.Fatalf("portable project creation: %#v %v", project, err)
	}
	project.Status = "active"
	updated, err := client.UpdateProject(context.Background(), project)
	if err != nil || updated.Status != "active" {
		t.Fatalf("portable project update: %#v %v", updated, err)
	}
	if err := client.ArchiveProject(context.Background(), project.ID); err != nil {
		t.Fatal(err)
	}
	projects, err := client.Projects(context.Background())
	if err != nil || len(projects) != 1 || projects[0].Active {
		t.Fatalf("portable project list: %#v %v", projects, err)
	}
}

func TestWindowsProjectEventsAreAuthorizedProjectedAndIncludedInFeed(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["projects.manage","projects.read"]},"employee":{"employeeId":7,"permissions":["projects.read"]}}`)
	handler := syncHandler(filepath.Join(t.TempDir(), "windows-project-events.ndjson"), "legacy")
	event := `{"topic":"project.created","payload":"{\"id\":\"device-a:41\",\"name\":\"Standort-Rollout\",\"department\":\"Operations\",\"description\":\"Nordregion\",\"status\":\"active\",\"ownerId\":7,\"active\":true}"}`
	post := func(token string) *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, "/events", bytes.NewBufferString(event))
		request.Header.Set("Authorization", "Bearer "+token)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response
	}
	if response := post("employee"); response.Code != http.StatusForbidden {
		t.Fatalf("project event accepted without manager right: %d", response.Code)
	}
	if response := post("manager"); response.Code != http.StatusAccepted {
		t.Fatalf("project event rejected: %d %s", response.Code, response.Body.String())
	}
	projects := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	projects.Header.Set("Authorization", "Bearer manager")
	listed := httptest.NewRecorder()
	handler.ServeHTTP(listed, projects)
	if listed.Code != http.StatusOK || !strings.Contains(listed.Body.String(), "device-a:41") {
		t.Fatalf("project event was not projected: %d %s", listed.Code, listed.Body.String())
	}
	feed := httptest.NewRequest(http.MethodGet, "/api/v1/sync/changes?cursor=0", nil)
	feed.Header.Set("Authorization", "Bearer manager")
	changes := httptest.NewRecorder()
	handler.ServeHTTP(changes, feed)
	if changes.Code != http.StatusOK || !strings.Contains(changes.Body.String(), "project.created") {
		t.Fatalf("project event missing from feed: %d %s", changes.Code, changes.Body.String())
	}
}

func TestWindowsTaskEventsAreAuthorizedProjectedAndArchived(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage","tasks.read"]},"employee":{"employeeId":7,"permissions":["tasks.read"]}}`)
	handler := syncHandler(filepath.Join(t.TempDir(), "windows-task-events.ndjson"), "legacy")
	post := func(token, topic, payload string) *httptest.ResponseRecorder {
		body := fmt.Sprintf(`{"topic":%q,"payload":%q}`, topic, payload)
		request := httptest.NewRequest(http.MethodPost, "/events", bytes.NewBufferString(body))
		request.Header.Set("Authorization", "Bearer "+token)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response
	}
	payload := `{"id":"device-a:51","title":"Sicherheitsreview","department":"IT","priority":"Hoch","status":"open","assigneeId":7,"createdBy":9,"active":true}`
	if response := post("employee", "task.created", payload); response.Code != http.StatusForbidden {
		t.Fatalf("task event accepted without manager right: %d", response.Code)
	}
	if response := post("manager", "task.created", payload); response.Code != http.StatusAccepted {
		t.Fatalf("task event rejected: %d %s", response.Code, response.Body.String())
	}
	list := httptest.NewRequest(http.MethodGet, "/api/v1/tasks", nil)
	list.Header.Set("Authorization", "Bearer employee")
	listed := httptest.NewRecorder()
	handler.ServeHTTP(listed, list)
	if listed.Code != http.StatusOK || !strings.Contains(listed.Body.String(), "Sicherheitsreview") {
		t.Fatalf("task event was not projected: %d %s", listed.Code, listed.Body.String())
	}
	archived := `{"id":"device-a:51","title":"Sicherheitsreview","department":"IT","priority":"Hoch","status":"cancelled","assigneeId":7,"createdBy":9,"active":false}`
	if response := post("manager", "task.archived", archived); response.Code != http.StatusAccepted {
		t.Fatalf("task archive rejected: %d", response.Code)
	}
	listed = httptest.NewRecorder()
	handler.ServeHTTP(listed, list)
	if listed.Code != http.StatusOK || strings.Contains(listed.Body.String(), "Sicherheitsreview") {
		t.Fatalf("archived task stayed visible: %d %s", listed.Code, listed.Body.String())
	}
}

func TestWindowsTicketEventsAreAuthorizedProjectedAndArchived(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tickets.manage","tickets.read"]},"employee":{"employeeId":7,"permissions":["tickets.read"]}}`)
	handler := syncHandler(filepath.Join(t.TempDir(), "windows-ticket-events.ndjson"), "legacy")
	post := func(token, topic, payload string) *httptest.ResponseRecorder {
		body := fmt.Sprintf(`{"topic":%q,"payload":%q}`, topic, payload)
		request := httptest.NewRequest(http.MethodPost, "/events", bytes.NewBufferString(body))
		request.Header.Set("Authorization", "Bearer "+token)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response
	}
	payload := `{"id":"device-a:61","title":"Projektor defekt","category":"Facility","priority":"Hoch","status":"open","assigneeId":7,"createdBy":9,"active":true}`
	if response := post("employee", "ticket.created", payload); response.Code != http.StatusForbidden {
		t.Fatalf("ticket event accepted without manager right: %d", response.Code)
	}
	if response := post("manager", "ticket.created", payload); response.Code != http.StatusAccepted {
		t.Fatalf("ticket event rejected: %d %s", response.Code, response.Body.String())
	}
	list := httptest.NewRequest(http.MethodGet, "/api/v1/tickets", nil)
	list.Header.Set("Authorization", "Bearer employee")
	listed := httptest.NewRecorder()
	handler.ServeHTTP(listed, list)
	if listed.Code != http.StatusOK || !strings.Contains(listed.Body.String(), "Projektor defekt") {
		t.Fatalf("ticket event was not projected: %d %s", listed.Code, listed.Body.String())
	}
	archived := `{"id":"device-a:61","title":"Projektor defekt","category":"Facility","priority":"Hoch","status":"closed","assigneeId":7,"createdBy":9,"active":false}`
	if response := post("manager", "ticket.archived", archived); response.Code != http.StatusAccepted {
		t.Fatalf("ticket archive rejected: %d", response.Code)
	}
	listed = httptest.NewRecorder()
	handler.ServeHTTP(listed, list)
	if listed.Code != http.StatusOK || strings.Contains(listed.Body.String(), "Projektor defekt") {
		t.Fatalf("archived ticket stayed visible: %d %s", listed.Code, listed.Body.String())
	}
}

func TestWindowsRoomEventsAreAuthorizedProjectedAndArchived(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["rooms.manage","rooms.read"]},"employee":{"employeeId":7,"permissions":["rooms.read"]}}`)
	handler := syncHandler(filepath.Join(t.TempDir(), "windows-room-events.ndjson"), "legacy")
	post := func(token, topic, payload string) *httptest.ResponseRecorder {
		body := fmt.Sprintf(`{"topic":%q,"payload":%q}`, topic, payload)
		request := httptest.NewRequest(http.MethodPost, "/events", bytes.NewBufferString(body))
		request.Header.Set("Authorization", "Bearer "+token)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response
	}
	payload := `{"id":"device-a:71","name":"Konferenz Nord","location":"Berlin","capacity":16,"equipment":"Display, VC","active":true}`
	if response := post("employee", "room.created", payload); response.Code != http.StatusForbidden {
		t.Fatalf("room event accepted without manager right: %d", response.Code)
	}
	if response := post("manager", "room.created", payload); response.Code != http.StatusAccepted {
		t.Fatalf("room event rejected: %d %s", response.Code, response.Body.String())
	}
	list := httptest.NewRequest(http.MethodGet, "/api/v1/rooms", nil)
	list.Header.Set("Authorization", "Bearer employee")
	listed := httptest.NewRecorder()
	handler.ServeHTTP(listed, list)
	if listed.Code != http.StatusOK || !strings.Contains(listed.Body.String(), "Konferenz Nord") {
		t.Fatalf("room event was not projected: %d %s", listed.Code, listed.Body.String())
	}
	archived := `{"id":"device-a:71","name":"Konferenz Nord","location":"Berlin","capacity":16,"equipment":"Display, VC","active":false}`
	if response := post("manager", "room.archived", archived); response.Code != http.StatusAccepted {
		t.Fatalf("room archive rejected: %d", response.Code)
	}
	listed = httptest.NewRecorder()
	handler.ServeHTTP(listed, list)
	if listed.Code != http.StatusOK || strings.Contains(listed.Body.String(), "Konferenz Nord") {
		t.Fatalf("archived room stayed visible: %d %s", listed.Code, listed.Body.String())
	}
}

func TestWindowsEmployeeEventsAreAuthorizedPersistedAndIncludedInFeed(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["employees.manage"]},"employee":{"employeeId":7,"permissions":["employees.read"]}}`)
	path := filepath.Join(t.TempDir(), "employees.ndjson")
	handler := syncHandler(path, "legacy")
	post := func(token, topic, payload string) *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, "/events", bytes.NewBufferString(fmt.Sprintf(`{"topic":%q,"payload":%q}`, topic, payload)))
		request.Header.Set("Authorization", "Bearer "+token)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response
	}
	payload := `{"id":"desktop-a:41","name":"Mara Beispiel","role":"Leitung","department":"Operations","presence":"office","active":true}`
	if response := post("employee", "employee.created", payload); response.Code != http.StatusForbidden {
		t.Fatalf("unprivileged employee event: %d", response.Code)
	}
	if response := post("manager", "employee.created", payload); response.Code != http.StatusAccepted {
		t.Fatalf("employee create: %d %s", response.Code, response.Body.String())
	}
	updated := `{"id":"desktop-a:41","name":"Mara Beispiel","role":"Leitung","department":"Operations","presence":"remote","active":true}`
	if response := post("manager", "employee.presence.changed", updated); response.Code != http.StatusAccepted {
		t.Fatalf("employee presence: %d", response.Code)
	}
	archived := `{"id":"desktop-a:41","name":"Mara Beispiel","role":"Leitung","department":"Operations","presence":"remote","active":true}`
	if response := post("manager", "employee.archived", archived); response.Code != http.StatusAccepted {
		t.Fatalf("employee archive: %d", response.Code)
	}
	feed := httptest.NewRequest(http.MethodGet, "/api/v1/sync/changes?cursor=0", nil)
	feed.Header.Set("Authorization", "Bearer manager")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, feed)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), "employee.archived") {
		t.Fatalf("employee event missing from cursor feed: %d %s", response.Code, response.Body.String())
	}
}

func TestFacilityAssetEventsRequireFacilityPermissionAndValidatePayload(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"facility":{"employeeId":9,"permissions":["facility.manage"]},"employee":{"employeeId":7,"permissions":["facility.read"]}}`)
	handler := syncHandler(filepath.Join(t.TempDir(), "facility-assets.ndjson"), "legacy")
	post := func(token, topic, payload string) *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, "/events", bytes.NewBufferString(fmt.Sprintf(`{"topic":%q,"payload":%q}`, topic, payload)))
		request.Header.Set("Authorization", "Bearer "+token)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response
	}
	valid := `{"id":"desktop-a:asset-8","name":"Display Ost","category":"Konferenztechnik","location":"Berlin / 2. OG","status":"operational","active":true}`
	if response := post("employee", "facility.asset.created", valid); response.Code != http.StatusForbidden {
		t.Fatalf("unprivileged facility event: %d", response.Code)
	}
	if response := post("facility", "facility.asset.created", `{"id":"x","name":"Display","category":"A","location":"B","status":"unknown"}`); response.Code != http.StatusBadRequest {
		t.Fatalf("invalid asset accepted: %d", response.Code)
	}
	if response := post("facility", "facility.asset.created", valid); response.Code != http.StatusAccepted {
		t.Fatalf("asset create: %d %s", response.Code, response.Body.String())
	}
	maintenance := `{"id":"desktop-a:maintenance-8","assetId":"desktop-a:asset-8","title":"Kalibrierung","status":"open","dueAt":50000,"recurrenceDays":30,"active":true}`
	if response := post("employee", "facility.maintenance.created", maintenance); response.Code != http.StatusForbidden {
		t.Fatalf("unprivileged maintenance event: %d", response.Code)
	}
	if response := post("facility", "facility.maintenance.created", maintenance); response.Code != http.StatusAccepted {
		t.Fatalf("maintenance create: %d %s", response.Code, response.Body.String())
	}
	if response := post("facility", "facility.maintenance.updated", `{"id":"desktop-a:maintenance-x","assetId":"unknown","title":"Bad","status":"open","dueAt":0,"recurrenceDays":0,"active":true}`); response.Code != http.StatusConflict {
		t.Fatalf("maintenance accepted missing asset: %d", response.Code)
	}
	archived := `{"id":"desktop-a:asset-8","name":"Display Ost","category":"Konferenztechnik","location":"Berlin / 2. OG","status":"operational","active":true}`
	if response := post("facility", "facility.asset.archived", archived); response.Code != http.StatusAccepted {
		t.Fatalf("asset archive: %d", response.Code)
	}
	if response := post("facility", "facility.maintenance.updated", maintenance); response.Code != http.StatusConflict {
		t.Fatalf("maintenance accepted for archived asset: %d", response.Code)
	}
	listed := httptest.NewRequest(http.MethodGet, "/api/v1/facility-assets?includeArchived=true", nil)
	listed.Header.Set("Authorization", "Bearer facility")
	listedResponse := httptest.NewRecorder()
	handler.ServeHTTP(listedResponse, listed)
	if listedResponse.Code != http.StatusOK || !strings.Contains(listedResponse.Body.String(), `"active":false`) {
		t.Fatalf("archived asset missing from facility list: %d %s", listedResponse.Code, listedResponse.Body.String())
	}
	feed := httptest.NewRequest(http.MethodGet, "/api/v1/sync/changes?cursor=0", nil)
	feed.Header.Set("Authorization", "Bearer facility")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, feed)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), "facility.asset.archived") {
		t.Fatalf("asset event missing from cursor feed: %d %s", response.Code, response.Body.String())
	}
}

func TestWindowsLeaveEventsAreAuthorizedAndValidated(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"employee":{"employeeId":7,"permissions":["leave.request"]},"manager":{"employeeId":9,"permissions":["leave.approve"]}}`)
	handler := syncHandler(filepath.Join(t.TempDir(), "leave-events.ndjson"), "legacy")
	post := func(token, topic, payload string) *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, "/events", bytes.NewBufferString(fmt.Sprintf(`{"topic":%q,"payload":%q}`, topic, payload)))
		request.Header.Set("Authorization", "Bearer "+token)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response
	}
	pending := `{"id":"desktop-a:leave-9","employeeId":7,"startsAt":20000,"endsAt":30000,"reason":"Erholung","status":"pending","decidedBy":0}`
	if response := post("manager", "leave.requested", pending); response.Code != http.StatusForbidden {
		t.Fatalf("manager could submit leave without request permission: %d", response.Code)
	}
	if response := post("employee", "leave.requested", pending); response.Code != http.StatusAccepted {
		t.Fatalf("leave request: %d %s", response.Code, response.Body.String())
	}
	if response := post("manager", "leave.decided", `{"id":"desktop-a:leave-9","employeeId":7,"startsAt":20000,"endsAt":30000,"reason":"Erholung","status":"approved","decidedBy":7}`); response.Code != http.StatusConflict {
		t.Fatalf("self approval accepted: %d", response.Code)
	}
	if response := post("manager", "leave.decided", `{"id":"desktop-a:leave-9","employeeId":7,"startsAt":20000,"endsAt":30000,"reason":"Erholung","status":"approved","decidedBy":9}`); response.Code != http.StatusAccepted {
		t.Fatalf("leave decision: %d %s", response.Code, response.Body.String())
	}
	list := httptest.NewRequest(http.MethodGet, "/api/v1/leave-requests", nil)
	list.Header.Set("Authorization", "Bearer manager")
	listed := httptest.NewRecorder()
	handler.ServeHTTP(listed, list)
	if listed.Code != http.StatusOK || !strings.Contains(listed.Body.String(), `"status":"approved"`) {
		t.Fatalf("central leave projection missing: %d %s", listed.Code, listed.Body.String())
	}
}

func TestCentralRoomApiRequiresPermissionAndValidCapacity(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["rooms.manage","rooms.read"]},"employee":{"employeeId":7,"permissions":["rooms.read"]}}`)
	handler := syncHandler(filepath.Join(t.TempDir(), "rooms.ndjson"), "legacy")
	post := func(token, body string) *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, "/api/v1/rooms", bytes.NewBufferString(body))
		request.Header.Set("Authorization", "Bearer "+token)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response
	}
	if response := post("employee", `{"name":"Alpha","location":"Berlin","capacity":8}`); response.Code != http.StatusForbidden {
		t.Fatalf("employee created room: %d", response.Code)
	}
	if response := post("manager", `{"name":"Alpha","location":"Berlin","capacity":0}`); response.Code != http.StatusBadRequest {
		t.Fatalf("invalid room accepted: %d", response.Code)
	}
	if response := post("manager", `{"name":"Alpha","location":"Berlin","capacity":8,"equipment":"Display"}`); response.Code != http.StatusCreated {
		t.Fatalf("room creation failed: %d", response.Code)
	}
}

func TestPortableClientUsesActualCentralRoomContract(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["rooms.manage","rooms.read"]}}`)
	handler := syncHandler(filepath.Join(t.TempDir(), "portable-contract.ndjson"), "legacy")
	server := httptest.NewServer(handler)
	defer server.Close()
	client := portable.Client{BaseURL: server.URL, Token: "manager"}
	room, err := client.CreateRoom(context.Background(), "Portable Alpha", "Berlin", 10, "Display")
	if err != nil {
		t.Fatal(err)
	}
	if room.ID == "" || room.Name != "Portable Alpha" {
		t.Fatalf("unexpected created room: %#v", room)
	}
	rooms, err := client.Rooms(context.Background(), false)
	if err != nil {
		t.Fatal(err)
	}
	if len(rooms) != 1 || rooms[0].ID != room.ID {
		t.Fatalf("portable room list mismatch: %#v", rooms)
	}
	room.Capacity = 14
	updated, err := client.UpdateRoom(context.Background(), room)
	if err != nil || updated.Capacity != 14 {
		t.Fatalf("portable room update failed: %#v %v", updated, err)
	}
	if err := client.ArchiveRoom(context.Background(), room.ID); err != nil {
		t.Fatal(err)
	}
	rooms, err = client.Rooms(context.Background(), false)
	if err != nil || len(rooms) != 0 {
		t.Fatalf("archived room stayed visible: %#v %v", rooms, err)
	}
}

func TestCentralRoomsCanBeUpdatedArchivedAndReplayed(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["rooms.manage","rooms.read"]}}`)
	path := filepath.Join(t.TempDir(), "central-rooms.ndjson")
	handler := syncHandler(path, "legacy")
	call := func(method, route, body string) *httptest.ResponseRecorder {
		request := httptest.NewRequest(method, route, bytes.NewBufferString(body))
		request.Header.Set("Authorization", "Bearer manager")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response
	}
	created := call(http.MethodPost, "/api/v1/rooms", `{"name":"Alpha","location":"Berlin","capacity":8,"equipment":"Display"}`)
	if created.Code != http.StatusCreated {
		t.Fatalf("create room: %d %s", created.Code, created.Body.String())
	}
	var room centralRoom
	if json.Unmarshal(created.Body.Bytes(), &room) != nil || room.ID == "" {
		t.Fatal("missing room identity")
	}
	updated := call(http.MethodPut, "/api/v1/rooms/"+room.ID, `{"name":"Alpha 2","location":"Berlin-Mitte","capacity":12,"equipment":"Display, VC"}`)
	if updated.Code != http.StatusOK || !strings.Contains(updated.Body.String(), "Alpha 2") {
		t.Fatalf("update room: %d %s", updated.Code, updated.Body.String())
	}
	if archived := call(http.MethodPost, "/api/v1/rooms/"+room.ID+"/archive", ""); archived.Code != http.StatusNoContent {
		t.Fatalf("archive room: %d", archived.Code)
	}
	if active := call(http.MethodGet, "/api/v1/rooms", ""); active.Code != http.StatusOK || strings.Contains(active.Body.String(), room.ID) {
		t.Fatalf("archived room visible: %d %s", active.Code, active.Body.String())
	}
	if archived := call(http.MethodGet, "/api/v1/rooms?includeArchived=true", ""); archived.Code != http.StatusOK || !strings.Contains(archived.Body.String(), `"active":false`) {
		t.Fatalf("archive history missing: %d %s", archived.Code, archived.Body.String())
	}
	replayed := syncHandler(path, "legacy")
	request := httptest.NewRequest(http.MethodGet, "/api/v1/rooms?includeArchived=true", nil)
	request.Header.Set("Authorization", "Bearer manager")
	response := httptest.NewRecorder()
	replayed.ServeHTTP(response, request)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), "Alpha 2") || !strings.Contains(response.Body.String(), `"active":false`) {
		t.Fatalf("room replay failed: %d %s", response.Code, response.Body.String())
	}
}

func TestSyncHandlerRejectsConflictingBookings(t *testing.T) {
	path := filepath.Join(t.TempDir(), "conflicts.ndjson")
	handler := syncHandler(path, "token")
	post := func(body string) int {
		request := httptest.NewRequest(http.MethodPost, "/events", bytes.NewBufferString(body))
		request.Header.Set("Authorization", "Bearer token")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response.Code
	}
	if status := post(`{"topic":"booking.created","payload":"{\"bookingId\":1,\"roomId\":8,\"startsAt\":100,\"endsAt\":200}"}`); status != http.StatusAccepted {
		t.Fatalf("initial booking: %d", status)
	}
	if status := post(`{"topic":"booking.created","payload":"{\"bookingId\":2,\"roomId\":8,\"startsAt\":150,\"endsAt\":250}"}`); status != http.StatusConflict {
		t.Fatalf("conflict not rejected: %d", status)
	}
	if status := post(`{"topic":"booking.created","payload":"{\"bookingId\":3,\"roomId\":8,\"startsAt\":200,\"endsAt\":300}"}`); status != http.StatusAccepted {
		t.Fatalf("adjacent booking rejected: %d", status)
	}
}

func TestCentralBookingsSeparateSameLocalIdByClientInstance(t *testing.T) {
	handler := syncHandler(filepath.Join(t.TempDir(), "client-identity.ndjson"), "token")
	post := func(body string) int {
		request := httptest.NewRequest(http.MethodPost, "/events", bytes.NewBufferString(body))
		request.Header.Set("Authorization", "Bearer token")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response.Code
	}
	payload := func(topic, client string, start, end int64) string {
		body, _ := json.Marshal(bookingPayload{ClientID: client, BookingID: 1, RoomID: 8, StartsAt: start, EndsAt: end})
		event, _ := json.Marshal(syncEvent{Topic: topic, Payload: string(body)})
		return string(event)
	}
	if status := post(payload("booking.created", "client-a", 100, 200)); status != http.StatusAccepted {
		t.Fatalf("first client booking: %d", status)
	}
	if status := post(payload("booking.created", "client-b", 200, 300)); status != http.StatusAccepted {
		t.Fatalf("second client local id collided: %d", status)
	}
	if status := post(payload("booking.rescheduled", "client-b", 150, 250)); status != http.StatusConflict {
		t.Fatalf("conflict was not checked between client instances: %d", status)
	}
}

func TestSyncHandlerPreflightRejectsCentralConflict(t *testing.T) {
	path := filepath.Join(t.TempDir(), "preflight.ndjson")
	handler := syncHandler(path, "token")
	post := func(path string, body string) int {
		request := httptest.NewRequest(http.MethodPost, path, bytes.NewBufferString(body))
		request.Header.Set("Authorization", "Bearer token")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response.Code
	}
	if status := post("/events", `{"topic":"booking.created","payload":"{\"bookingId\":1,\"roomId\":8,\"startsAt\":100,\"endsAt\":200}"}`); status != http.StatusAccepted {
		t.Fatalf("initial booking: %d", status)
	}
	if status := post("/bookings/check", `{"bookingId":0,"roomId":8,"startsAt":150,"endsAt":250}`); status != http.StatusConflict {
		t.Fatalf("preflight conflict not rejected: %d", status)
	}
	if status := post("/bookings/check", `{"bookingId":0,"roomId":8,"startsAt":200,"endsAt":250}`); status != http.StatusNoContent {
		t.Fatalf("adjacent preflight rejected: %d", status)
	}
	if status := post("/bookings/check", `{"bookingId":1,"roomId":8,"startsAt":120,"endsAt":220}`); status != http.StatusNoContent {
		t.Fatalf("reschedule self-overlap rejected: %d", status)
	}
}

func TestSyncHandlerReservationAtomicallyBlocksAndCommitsBooking(t *testing.T) {
	path := filepath.Join(t.TempDir(), "reservations.ndjson")
	handler := syncHandler(path, "token")
	post := func(route, body string) int {
		request := httptest.NewRequest(http.MethodPost, route, bytes.NewBufferString(body))
		request.Header.Set("Authorization", "Bearer token")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response.Code
	}
	if status := post("/bookings/reserve", `{"reservationId":-11,"bookingId":0,"roomId":8,"startsAt":100,"endsAt":200}`); status != http.StatusNoContent {
		t.Fatalf("reserve: %d", status)
	}
	if status := post("/bookings/reserve", `{"reservationId":-12,"bookingId":0,"roomId":8,"startsAt":150,"endsAt":250}`); status != http.StatusConflict {
		t.Fatalf("reservation conflict not rejected: %d", status)
	}
	if status := post("/bookings/commit", `{"reservationId":-11,"bookingId":71,"topic":"booking.created"}`); status != http.StatusAccepted {
		t.Fatalf("commit: %d", status)
	}
	if status := post("/bookings/reserve", `{"reservationId":-13,"bookingId":0,"roomId":8,"startsAt":150,"endsAt":250}`); status != http.StatusConflict {
		t.Fatalf("committed booking did not remain authoritative: %d", status)
	}
	content, err := os.ReadFile(path)
	if err != nil || !strings.Contains(string(content), `booking.created`) || !strings.Contains(string(content), `\"bookingId\":71`) {
		t.Fatalf("central commit was not journaled: %v %q", err, content)
	}
}

func TestSyncHandlerReleaseMakesReservationAvailableAgain(t *testing.T) {
	path := filepath.Join(t.TempDir(), "release.ndjson")
	handler := syncHandler(path, "token")
	post := func(route, body string) int {
		request := httptest.NewRequest(http.MethodPost, route, bytes.NewBufferString(body))
		request.Header.Set("Authorization", "Bearer token")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response.Code
	}
	if status := post("/bookings/reserve", `{"reservationId":-21,"bookingId":0,"roomId":4,"startsAt":100,"endsAt":200}`); status != http.StatusNoContent {
		t.Fatalf("reserve: %d", status)
	}
	if status := post("/bookings/release", `{"reservationId":-21}`); status != http.StatusNoContent {
		t.Fatalf("release: %d", status)
	}
	if status := post("/bookings/reserve", `{"reservationId":-22,"bookingId":0,"roomId":4,"startsAt":100,"endsAt":200}`); status != http.StatusNoContent {
		t.Fatalf("released slot remained blocked: %d", status)
	}
}

func TestSyncHandlerRestoresBookingConflictsFromJournal(t *testing.T) {
	path := filepath.Join(t.TempDir(), "replay.ndjson")
	if err := persistEvent(path, syncEvent{Topic: "booking.created", Payload: `{"bookingId":7,"roomId":3,"startsAt":500,"endsAt":600}`}); err != nil {
		t.Fatal(err)
	}
	handler := syncHandler(path, "token")
	request := httptest.NewRequest(http.MethodPost, "/events", bytes.NewBufferString(`{"topic":"booking.created","payload":"{\"bookingId\":8,\"roomId\":3,\"startsAt\":550,\"endsAt\":650}"}`))
	request.Header.Set("Authorization", "Bearer token")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusConflict {
		t.Fatalf("journal replay failed: %d", response.Code)
	}
}

func TestPublishEventSendsToConfiguredServer(t *testing.T) {
	journal := filepath.Join(t.TempDir(), "client-outbox.ndjson")
	received := false
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer client-token" {
			http.Error(writer, "unauthorized", http.StatusUnauthorized)
			return
		}
		received = request.URL.Path == "/events"
		writer.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()
	t.Setenv("AURA_SYNC_JOURNAL", journal)
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "client-token")
	if code := publishEvent(syncEvent{Topic: "task.updated", Payload: `{}`}); code != 0 {
		t.Fatalf("publish failed: %d", code)
	}
	if !received {
		t.Fatal("central server did not receive event")
	}
	if _, err := os.Stat(journal); !os.IsNotExist(err) {
		t.Fatalf("successful event must not remain in replay outbox: %v", err)
	}
}

func TestPublishEventQueuesWhenCentralServerIsUnavailable(t *testing.T) {
	journal := filepath.Join(t.TempDir(), "offline-outbox.ndjson")
	t.Setenv("AURA_SYNC_JOURNAL", journal)
	t.Setenv("AURA_SYNC_URL", "http://127.0.0.1:1")
	t.Setenv("AURA_SYNC_TOKEN", "client-token")
	if code := publishEvent(syncEvent{Topic: "task.updated", Payload: `{}`}); code != 7 {
		t.Fatalf("offline publish code: %d", code)
	}
	content, err := os.ReadFile(journal)
	if err != nil || !strings.Contains(string(content), "task.updated") {
		t.Fatalf("offline event was not queued: %v %q", err, content)
	}
}

func TestPublishEventQueuesCommittedChangesWithoutToken(t *testing.T) {
	journal := filepath.Join(t.TempDir(), "no-token.ndjson")
	t.Setenv("AURA_SYNC_JOURNAL", journal)
	t.Setenv("AURA_SYNC_URL", "http://127.0.0.1:1")
	t.Setenv("AURA_SYNC_TOKEN", "")
	t.Setenv("AURA_SYNC_CLIENT_ID", "test-desktop")
	event := syncEvent{Topic: "comment.created", Payload: `{"id":"c1","body":"Gespeichert"}`}
	if code := publishEvent(event); code != 6 {
		t.Fatalf("missing token: %d", code)
	}
	if code := publishEvent(syncEvent{Topic: "booking.cancelled", Payload: `{"bookingId":5}`}); code != 6 {
		t.Fatalf("booking token: %d", code)
	}
	data, err := os.ReadFile(journal)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) != 1 {
		t.Fatalf("unexpected queued operations: %d", len(lines))
	}
	var queued syncEvent
	if json.Unmarshal([]byte(lines[0]), &queued) != nil || queued != event {
		t.Fatalf("queued event: %+v", queued)
	}
	var received []syncEvent
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var e syncEvent
		if r.Header.Get("Authorization") != "Bearer token" || json.NewDecoder(r.Body).Decode(&e) != nil {
			http.Error(w, "invalid", 400)
			return
		}
		received = append(received, e)
		w.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "token")
	if code := flushOutbox(); code != 0 {
		t.Fatalf("replay: %d", code)
	}
	data, err = os.ReadFile(journal)
	if err != nil || len(data) != 0 || len(received) != 1 || received[0] != event {
		t.Fatalf("replay mismatch: %v %+v", err, received)
	}
}

func TestDesktopCancellationFailureDoesNotQueueDelayedCancellation(t *testing.T) {
	journal := filepath.Join(t.TempDir(), "cancel-outbox.ndjson")
	t.Setenv("AURA_SYNC_JOURNAL", journal)
	t.Setenv("AURA_SYNC_CLIENT_ID", "desktop-a")
	t.Setenv("AURA_SYNC_TOKEN", "token")
	prior := syncEvent{Topic: "comment.created", Payload: `{"id":"existing"}`}
	if err := persistEvent(journal, prior); err != nil {
		t.Fatal(err)
	}
	before, err := os.ReadFile(journal)
	if err != nil {
		t.Fatal(err)
	}
	status := http.StatusServiceUnavailable
	var received syncEvent
	var topics []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if json.NewDecoder(r.Body).Decode(&received) != nil {
			http.Error(w, "invalid", 400)
			return
		}
		topics = append(topics, received.Topic)
		w.WriteHeader(status)
	}))
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	if code := cancelDesktopBooking(`{"bookingId":42}`); code == 0 {
		t.Fatal("failed cancellation reported success")
	}
	after, err := os.ReadFile(journal)
	if err != nil || !bytes.Equal(before, after) {
		t.Fatal("failure modified outbox")
	}
	if strings.Join(topics, ",") != "comment.created" {
		t.Fatalf("cancellation overtook failed replay: %v", topics)
	}
	status = http.StatusAccepted
	topics = nil
	if code := cancelDesktopBooking(`{"bookingId":42}`); code != 0 {
		t.Fatalf("successful cancellation: %d", code)
	}
	var booking bookingPayload
	if json.Unmarshal([]byte(received.Payload), &booking) != nil || received.Topic != "booking.cancelled" || booking.ClientID != "desktop-a" || booking.BookingID != 42 {
		t.Fatalf("wrong event: %+v", received)
	}
	after, err = os.ReadFile(journal)
	if err != nil || len(after) != 0 || strings.Join(topics, ",") != "comment.created,booking.cancelled" {
		t.Fatalf("incorrect replay and cancellation ordering: %v", topics)
	}
	t.Setenv("AURA_SYNC_URL", "")
	if code := cancelDesktopBooking(`{"bookingId":43}`); code != 0 {
		t.Fatalf("offline cancellation: %d", code)
	}
	data, err := os.ReadFile(journal)
	if err != nil || len(strings.Split(strings.TrimSpace(string(data)), "\n")) != 1 {
		t.Fatal("offline cancellation was not queued")
	}
}

func TestFlushOutboxReplaysInOrderAndClearsDeliveredEvents(t *testing.T) {
	journal := filepath.Join(t.TempDir(), "replay-outbox.ndjson")
	events := []syncEvent{{Topic: "task.created", Payload: `{"id":"1"}`}, {Topic: "task.status.changed", Payload: `{"id":"1","status":"done"}`}}
	for _, event := range events {
		if err := persistEvent(journal, event); err != nil {
			t.Fatal(err)
		}
	}
	received := []string{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var event syncEvent
		_ = json.NewDecoder(r.Body).Decode(&event)
		received = append(received, event.Topic)
		w.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()
	t.Setenv("AURA_SYNC_JOURNAL", journal)
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "token")
	if code := flushOutbox(); code != 0 {
		t.Fatalf("flush failed: %d", code)
	}
	content, err := os.ReadFile(journal)
	if err != nil || len(content) != 0 {
		t.Fatalf("outbox was not cleared: %v %q", err, content)
	}
	if strings.Join(received, ",") != "task.created,task.status.changed" {
		t.Fatalf("outbox order changed: %v", received)
	}
}

func TestPublishEventPreservesPendingOrderAcrossFailure(t *testing.T) {
	journal := filepath.Join(t.TempDir(), "ordered.ndjson")
	t.Setenv("AURA_SYNC_JOURNAL", journal)
	t.Setenv("AURA_SYNC_TOKEN", "token")
	first := syncEvent{Topic: "task.created", Payload: `{"id":"one"}`}
	second := syncEvent{Topic: "task.updated", Payload: `{"id":"one"}`}
	if err := persistEvent(journal, first); err != nil {
		t.Fatal(err)
	}
	accept := false
	var calls []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var e syncEvent
		if json.NewDecoder(r.Body).Decode(&e) != nil {
			http.Error(w, "bad", 400)
			return
		}
		calls = append(calls, e.Topic)
		if !accept {
			http.Error(w, "offline", 503)
			return
		}
		w.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	if code := publishEvent(second); code == 0 {
		t.Fatal("failed replay reported success")
	}
	if strings.Join(calls, ",") != "task.created" {
		t.Fatalf("event overtook queue: %v", calls)
	}
	data, err := os.ReadFile(journal)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) != 2 {
		t.Fatalf("queue count: %d", len(lines))
	}
	var a, b syncEvent
	if json.Unmarshal([]byte(lines[0]), &a) != nil || json.Unmarshal([]byte(lines[1]), &b) != nil || a != first || b != second {
		t.Fatalf("queue changed: %+v %+v", a, b)
	}
	accept = true
	calls = nil
	if code := flushOutbox(); code != 0 {
		t.Fatalf("retry: %d", code)
	}
	if strings.Join(calls, ",") != "task.created,task.updated" {
		t.Fatalf("retry order: %v", calls)
	}
	data, err = os.ReadFile(journal)
	if err != nil || len(data) != 0 {
		t.Fatal("queue not cleared")
	}
}

func TestPublishCannotOvertakeInFlightReplay(t *testing.T) {
	journal := filepath.Join(t.TempDir(), "concurrent.ndjson")
	t.Setenv("AURA_SYNC_JOURNAL", journal)
	t.Setenv("AURA_SYNC_TOKEN", "token")
	if err := persistEvent(journal, syncEvent{Topic: "task.created", Payload: `{"id":"one"}`}); err != nil {
		t.Fatal(err)
	}
	firstSeen := make(chan struct{})
	release := make(chan struct{})
	calls := make(chan string, 2)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var event syncEvent
		if json.NewDecoder(r.Body).Decode(&event) != nil {
			http.Error(w, "bad", 400)
			return
		}
		calls <- event.Topic
		if event.Topic == "task.created" {
			close(firstSeen)
			<-release
		}
		w.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	flushed := make(chan int, 1)
	go func() { flushed <- flushOutbox() }()
	<-firstSeen
	published := make(chan int, 1)
	go func() { published <- publishEvent(syncEvent{Topic: "task.updated", Payload: `{"id":"one"}`}) }()
	select {
	case topic := <-calls:
		if topic != "task.created" {
			t.Fatalf("first event: %s", topic)
		}
	default:
		t.Fatal("missing first event")
	}
	var overtook string
	select {
	case overtook = <-calls:
	case <-time.After(100 * time.Millisecond):
	}
	close(release)
	if overtook != "" {
		t.Fatalf("event overtook in-flight replay: %s", overtook)
	}
	if code := <-flushed; code != 0 {
		t.Fatalf("flush: %d", code)
	}
	if code := <-published; code != 0 {
		t.Fatalf("publish: %d", code)
	}
	if topic := <-calls; topic != "task.updated" {
		t.Fatalf("second event: %s", topic)
	}
	data, err := os.ReadFile(journal)
	if err != nil || len(data) != 0 {
		t.Fatal("unexpected remaining queue")
	}
}

func TestPreflightBookingUsesConfiguredServer(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/bookings/check" || request.Header.Get("Authorization") != "Bearer client-token" {
			http.Error(writer, "unexpected request", http.StatusBadRequest)
			return
		}
		writer.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "client-token")
	t.Setenv("AURA_SYNC_CLIENT_ID", "test-client-a")
	if code := preflightBooking(0, 7, 100, 200); code != 0 {
		t.Fatalf("preflight failed: %d", code)
	}
}

func TestTaskApprovalEventsRequireAnActiveTaskAndEnforceDecisionAuthority(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage"]},"reader":{"employeeId":7,"permissions":["tasks.read"]}}`)
	journal := filepath.Join(t.TempDir(), "task-approvals.ndjson")
	store, err := openCentralStore(journal + ".sqlite")
	if err != nil {
		t.Fatal(err)
	}
	previous := centralMirror
	centralMirror = store
	defer func() { centralMirror.close(); centralMirror = previous }()
	handler := syncHandler(journal, "token")
	post := func(token, topic, payload string) int {
		body, _ := json.Marshal(syncEvent{Topic: topic, Payload: payload})
		request := httptest.NewRequest(http.MethodPost, "/events", bytes.NewReader(body))
		request.Header.Set("Authorization", "Bearer "+token)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response.Code
	}
	task := `{"id":"client-a:41","title":"Beschaffung freigeben","department":"IT","priority":"Hoch","status":"open","assigneeId":9,"createdBy":9,"active":true}`
	if status := post("manager", "task.created", task); status != http.StatusAccepted {
		t.Fatalf("task: %d", status)
	}
	approval := `{"id":"client-a:approval-1","taskId":"client-a:41","requestedBy":"client-a:9","approverId":"client-a:10","status":"pending","decidedBy":"","active":true}`
	if status := post("reader", "task.approval.requested", approval); status != http.StatusForbidden {
		t.Fatalf("permission: %d", status)
	}
	if status := post("manager", "task.approval.requested", approval); status != http.StatusAccepted {
		t.Fatalf("request: %d", status)
	}
	if status := post("manager", "task.approval.requested", approval); status != http.StatusAccepted {
		t.Fatalf("request retry: %d", status)
	}
	invalidDecision := `{"id":"client-a:approval-1","taskId":"client-a:41","requestedBy":"client-a:9","approverId":"client-a:10","status":"approved","decidedBy":"client-a:9","active":true}`
	if status := post("manager", "task.approval.decided", invalidDecision); status != http.StatusConflict {
		t.Fatalf("decision authority: %d", status)
	}
	decision := `{"id":"client-a:approval-1","taskId":"client-a:41","requestedBy":"client-a:9","approverId":"client-a:10","status":"approved","decidedBy":"client-a:10","active":true}`
	if status := post("manager", "task.approval.decided", strings.Replace(decision, `"requestedBy":"client-a:9"`, `"requestedBy":"other"`, 1)); status != http.StatusConflict {
		t.Fatalf("changed requester: %d", status)
	}
	if status := post("manager", "task.approval.decided", decision); status != http.StatusAccepted {
		t.Fatalf("decision: %d", status)
	}
	if status := post("manager", "task.approval.requested", approval); status != http.StatusAccepted {
		t.Fatalf("late request retry: %d", status)
	}
	if status := post("manager", "task.approval.decided", decision); status != http.StatusAccepted {
		t.Fatalf("decision retry: %d", status)
	}
	if status := post("manager", "task.approval.decided", strings.Replace(decision, `"status":"approved"`, `"status":"rejected"`, 1)); status != http.StatusConflict {
		t.Fatalf("opposite decision: %d", status)
	}
	if status := post("manager", "task.archived", task); status != http.StatusAccepted {
		t.Fatalf("task archive: %d", status)
	}
	store.close()
	store, err = openCentralStore(journal + ".sqlite")
	if err != nil {
		centralMirror = nil
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "token")
	if status := post("manager", "task.approval.requested", approval); status != http.StatusAccepted {
		t.Fatalf("request retry after restart and archive: %d", status)
	}
	if status := post("manager", "task.approval.decided", decision); status != http.StatusAccepted {
		t.Fatalf("decision retry after restart and archive: %d", status)
	}
	events, _, err := store.eventsAfter(0)
	if err != nil {
		t.Fatal(err)
	}
	var approvalEvents []syncEvent
	for _, event := range events {
		if strings.HasPrefix(event.Topic, "task.approval.") {
			approvalEvents = append(approvalEvents, event)
		}
	}
	if len(approvalEvents) != 2 || approvalEvents[0].Topic != "task.approval.requested" || approvalEvents[1].Topic != "task.approval.decided" {
		t.Fatalf("approval history: %+v", approvalEvents)
	}
}

func TestCommentEventsRequireAnExistingSubjectAndAreIdempotent(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"writer":{"employeeId":9,"permissions":["tasks.manage","sync.events.write"]}}`)
	journal := filepath.Join(t.TempDir(), "comments.ndjson")
	store, err := openCentralStore(journal + ".sqlite")
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "token")
	post := func(topic, payload string) int {
		body, _ := json.Marshal(syncEvent{Topic: topic, Payload: payload})
		request := httptest.NewRequest(http.MethodPost, "/events", bytes.NewReader(body))
		request.Header.Set("Authorization", "Bearer writer")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		return response.Code
	}
	comment := `{"id":"client-a:comment-1","subjectType":"task","subjectId":"client-a:42","authorId":"client-a:9","body":"Bitte Security-Review ergänzen."}`
	if status := post("comment.created", comment); status != http.StatusConflict {
		t.Fatalf("missing task: %d", status)
	}
	task := `{"id":"client-a:42","title":"Review","department":"IT","priority":"Mittel","status":"open","assigneeId":9,"createdBy":9,"active":true}`
	if status := post("task.created", task); status != http.StatusAccepted {
		t.Fatalf("task: %d", status)
	}
	if status := post("comment.created", strings.Replace(comment, `"body":`, `"createdAt":"invalid","body":`, 1)); status != http.StatusBadRequest {
		t.Fatalf("invalid creation time: %d", status)
	}
	comment = strings.Replace(comment, `"body":`, `"createdAt":"2025-01-01T12:00:00Z","body":`, 1)
	if status := post("comment.created", comment); status != http.StatusAccepted {
		t.Fatalf("comment: %d", status)
	}
	if status := post("comment.created", comment); status != http.StatusAccepted {
		t.Fatalf("idempotency: %d", status)
	}
	for _, changed := range []string{
		strings.Replace(comment, "Security-Review", "Architektur-Review", 1),
		strings.Replace(comment, "client-a:9", "client-a:8", 1),
		strings.Replace(comment, `"body":`, `"createdAt":"2026-01-01T00:00:00Z","body":`, 1),
	} {
		if status := post("comment.created", changed); status != http.StatusConflict {
			t.Fatalf("different comment accepted: %d", status)
		}
	}
	if status := post("task.archived", strings.Replace(task, `"active":true`, `"active":false`, 1)); status != http.StatusAccepted {
		t.Fatalf("archive task: %d", status)
	}
	if status := post("comment.created", comment); status != http.StatusAccepted {
		t.Fatalf("retry after task archived: %d", status)
	}
	if status := post("comment.created", strings.Replace(comment, "comment-1", "comment-new", 1)); status != http.StatusConflict {
		t.Fatalf("new comment on archived task: %d", status)
	}
	store.close()
	store, err = openCentralStore(journal + ".sqlite")
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "token")
	if status := post("comment.created", comment); status != http.StatusAccepted {
		t.Fatalf("retry after restart: %d", status)
	}
	if status := post("comment.created", strings.Replace(comment, "Security-Review", "Architektur-Review", 1)); status != http.StatusConflict {
		t.Fatalf("conflict after restart: %d", status)
	}
	events, _, err := changesSince(journal, 0)
	if err != nil {
		t.Fatal(err)
	}
	count := 0
	for _, event := range events {
		if event.Topic == "comment.created" {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("unexpected number of comments: %d", count)
	}
}

func TestCanonicalBookingIdentityAcrossClientsAndReplay(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["rooms.manage","rooms.book"]},"reader":{"employeeId":7,"permissions":["rooms.read"]}}`)
	journal := filepath.Join(t.TempDir(), "canonical-bookings.ndjson")
	handler := syncHandler(journal, "legacy")
	post := func(path, token, body string) int {
		req := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+token)
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		return rec.Code
	}
	for _, id := range []string{"room-A", "room-B"} {
		payload := fmt.Sprintf(`{"id":%q,"name":%q,"location":"Berlin","capacity":8,"active":true}`, id, id)
		event, _ := json.Marshal(syncEvent{Topic: "room.created", Payload: payload})
		if status := post("/events", "manager", string(event)); status != 202 {
			t.Fatalf("create %s: %d", id, status)
		}
	}
	if status := post("/bookings/reserve", "reader", `{"clientId":"c","reservationId":-1,"roomId":1,"roomKey":"room-A","startsAt":100,"endsAt":200}`); status != 403 {
		t.Fatalf("permission: %d", status)
	}
	if status := post("/bookings/reserve", "manager", `{"clientId":"a","reservationId":-1,"roomId":3,"roomKey":"missing","startsAt":100,"endsAt":200}`); status != 404 {
		t.Fatalf("missing room: %d", status)
	}
	if status := post("/bookings/reserve", "manager", `{"clientId":"a","reservationId":-1,"roomId":3,"roomKey":"room-A","startsAt":100,"endsAt":200}`); status != 204 {
		t.Fatalf("reserve: %d", status)
	}
	if status := post("/bookings/reserve", "manager", `{"clientId":"b","reservationId":-2,"roomId":99,"roomKey":"room-A","startsAt":150,"endsAt":250}`); status != 409 {
		t.Fatalf("same room different local IDs: %d", status)
	}
	if status := post("/bookings/reserve", "manager", `{"clientId":"b","reservationId":-3,"roomId":3,"roomKey":"room-B","startsAt":150,"endsAt":250}`); status != 204 {
		t.Fatalf("different room same local ID: %d", status)
	}
	if status := post("/bookings/commit", "manager", `{"clientId":"a","reservationId":-1,"bookingId":1,"topic":"booking.created"}`); status != 202 {
		t.Fatalf("commit: %d", status)
	}
	handler = syncHandler(journal, "legacy")
	if status := post("/bookings/reserve", "manager", `{"clientId":"b","reservationId":-4,"roomId":99,"roomKey":"room-A","startsAt":150,"endsAt":250}`); status != 409 {
		t.Fatalf("replay lost identity: %d", status)
	}
	if status := post("/bookings/reserve", "manager", `{"clientId":"a","reservationId":-6,"bookingId":1,"roomId":3,"startsAt":300,"endsAt":400}`); status != 409 {
		t.Fatalf("canonical reservation downgrade: %d", status)
	}
	legacyUpdate := syncEvent{Topic: "booking.rescheduled", Payload: `{"clientId":"a","bookingId":1,"roomId":3,"startsAt":300,"endsAt":400}`}
	event, _ := json.Marshal(legacyUpdate)
	if status := post("/events", "manager", string(event)); status != 202 {
		t.Fatalf("legacy update: %d", status)
	}
	handler = syncHandler(journal, "legacy")
	if status := post("/bookings/reserve", "manager", `{"clientId":"b","reservationId":-7,"roomId":999,"roomKey":"room-A","startsAt":350,"endsAt":375}`); status != 409 {
		t.Fatalf("legacy update lost canonical key: %d", status)
	}
	if status := post("/bookings/reserve", "manager", `{"clientId":"legacy","reservationId":-5,"roomId":500,"startsAt":350,"endsAt":375}`); status != 409 {
		t.Fatalf("unsafe mixed legacy reservation: %d", status)
	}
}

func TestKeyedClientRequiresAdvertisedServerCapability(t *testing.T) {
	var writes int
	legacy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			writes++
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Setenv("AURA_SYNC_URL", legacy.URL)
	t.Setenv("AURA_SYNC_TOKEN", "token")
	t.Setenv("AURA_SYNC_CLIENT_ID", "desktop-a")
	if code := reserveBookingWithRoomKey(-1, 0, 3, 100, 200, "shared-room"); code != 8 || writes != 0 {
		t.Fatalf("legacy server accepted key: code=%d writes=%d", code, writes)
	}
	legacy.Close()
	journal := filepath.Join(t.TempDir(), "keyed-client.ndjson")
	server := httptest.NewServer(syncHandler(journal, "token"))
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	req := httptest.NewRequest(http.MethodPost, "/events", strings.NewReader(`{"topic":"room.created","payload":"{\"id\":\"shared-room\",\"name\":\"A\",\"location\":\"B\",\"capacity\":5,\"active\":true}"}`))
	req.Header.Set("Authorization", "Bearer token")
	r := httptest.NewRecorder()
	sync := syncHandler(journal, "token")
	sync.ServeHTTP(r, req)
	if r.Code != 202 {
		t.Fatalf("room setup: %d", r.Code)
	}
	server.Close()
	server = httptest.NewServer(syncHandler(journal, "token"))
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	if code := reserveBookingWithRoomKey(-1, 0, 3, 100, 200, "shared-room"); code != 0 {
		t.Fatalf("keyed reserve: %d", code)
	}
	if code := commitBooking(-1, 1, false); code != 0 {
		t.Fatalf("keyed commit: %d", code)
	}
	events, _, err := changesSince(journal, 0)
	if err != nil {
		t.Fatal(err)
	}
	var booking bookingPayload
	if err = json.Unmarshal([]byte(events[len(events)-1].Payload), &booking); err != nil || booking.RoomKey != "shared-room" {
		t.Fatalf("key absent after commit: %+v %v", booking, err)
	}
}

func TestCentralRoomBookingsSeriesPermissionsAndReplay(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"admin":{"employeeId":9,"permissions":["rooms.manage"]},"booker":{"employeeId":7,"permissions":["rooms.book","rooms.read"]},"other":{"employeeId":8,"permissions":["rooms.book","rooms.read"]}}`)
	dir := t.TempDir()
	store, err := openCentralStore(filepath.Join(dir, "central.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(filepath.Join(dir, "events.ndjson"), "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	r := call("POST", "/api/v1/rooms", "admin", `{"name":"A","location":"Berlin","capacity":4}`)
	if r.Code != 201 {
		t.Fatalf("room: %d %s", r.Code, r.Body.String())
	}
	var room centralRoom
	_ = json.Unmarshal(r.Body.Bytes(), &room)
	path := "/api/v1/room-bookings?roomKey=" + room.ID
	r = call("POST", path, "booker", `{"startsAt":100000,"endsAt":103600,"attendees":5,"occurrences":1,"intervalDays":7}`)
	if r.Code != 400 {
		t.Fatalf("capacity: %d", r.Code)
	}
	r = call("POST", path, "booker", `{"startsAt":100000,"endsAt":103600,"attendees":2,"occurrences":2,"intervalDays":1}`)
	if r.Code != 200 {
		t.Fatalf("series: %d %s", r.Code, r.Body.String())
	}
	var values []bookingPayload
	if err := json.Unmarshal(r.Body.Bytes(), &values); err != nil || len(values) != 2 {
		t.Fatalf("series response %s %v", r.Body.String(), err)
	}
	r = call("POST", path, "other", `{"startsAt":103000,"endsAt":104000,"attendees":1,"occurrences":2,"intervalDays":1}`)
	if r.Code != 409 {
		t.Fatalf("overlap: %d", r.Code)
	}
	reserve := fmt.Sprintf(`{"clientId":"windows","reservationId":-1,"roomId":33,"roomKey":%q,"startsAt":100000,"endsAt":101000}`, room.ID)
	r = call("POST", "/bookings/reserve", "admin", reserve)
	if r.Code != 409 {
		t.Fatalf("windows conflict: %d", r.Code)
	}
	body := fmt.Sprintf(`{"clientId":%q,"bookingId":%d}`, values[0].ClientID, values[0].BookingID)
	if r = call("DELETE", path, "other", body); r.Code != 403 {
		t.Fatalf("unauthorized cancel: %d", r.Code)
	}
	if r = call("DELETE", path, "booker", body); r.Code != 204 {
		t.Fatalf("cancel: %d %s", r.Code, r.Body.String())
	}
	if r = call("POST", path, "booker", `{"startsAt":200000,"endsAt":201000,"attendees":1,"occurrences":1,"intervalDays":1,"blocked":true,"reason":"Wartung"}`); r.Code != 403 {
		t.Fatalf("block permissions: %d", r.Code)
	}
	handler = syncHandler(filepath.Join(dir, "events.ndjson"), "legacy")
	r = call("GET", path, "booker", "")
	var after []bookingPayload
	if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &after) != nil || len(after) != 2 {
		t.Fatalf("replay: %d %s", r.Code, r.Body.String())
	}
	count := map[string]int{}
	for _, v := range after {
		count[v.Status]++
	}
	if count["confirmed"] != 1 || count["cancelled"] != 1 {
		t.Fatalf("replay statuses: %+v", after)
	}
}

func TestRoomSyncIdempotenceConflictAndDurableReceipt(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"editor":{"employeeId":9,"permissions":["rooms.manage"]},"reader":{"employeeId":7,"permissions":["rooms.read"]},"editor2":{"employeeId":8,"permissions":["rooms.manage"]}}`)
	dir := t.TempDir()
	db := filepath.Join(dir, "central.sqlite")
	journal := filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	post := func(token, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, "/api/v1/room-sync", strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	create := `{"clientId":"linux-a","operationId":"op-1","action":"create","room":{"id":"linux-a:room:1","name":"Raum A","location":"Berlin","capacity":4,"equipment":"","active":true},"expected":null}`
	if r := post("reader", create); r.Code != 403 {
		t.Fatalf("permission: %d", r.Code)
	}
	if r := post("editor", create); r.Code != 202 {
		t.Fatalf("create: %d %s", r.Code, r.Body.String())
	}
	update := `{"clientId":"linux-a","operationId":"op-2","action":"update","room":{"id":"linux-a:room:1","name":"Raum A","location":"Bonn","capacity":4,"equipment":"","active":true},"expected":{"id":"linux-a:room:1","name":"Raum A","location":"Berlin","capacity":4,"equipment":"","active":true}}`
	if r := post("editor", update); r.Code != 202 {
		t.Fatalf("update: %d %s", r.Code, r.Body.String())
	}
	if r := post("editor", create); r.Code != 202 {
		t.Fatalf("retry: %d", r.Code)
	}
	if r := post("editor", strings.Replace(create, "Raum A", "Anderer", 1)); r.Code != 409 {
		t.Fatalf("operation collision: %d", r.Code)
	}
	stale := strings.Replace(update, "op-2", "op-3", 1)
	if r := post("editor", stale); r.Code != 409 {
		t.Fatalf("stale write: %d", r.Code)
	}
	if r := post("editor2", create); r.Code != 409 {
		t.Fatalf("receipt accepted for another identity: %d", r.Code)
	}
	events, _, err := store.eventsAfter(0)
	if err != nil || len(events) != 2 {
		t.Fatalf("event count before restart: %d %v", len(events), err)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := post("editor", create); r.Code != 202 {
		t.Fatalf("retry after restart: %d", r.Code)
	}
	request := httptest.NewRequest(http.MethodGet, "/api/v1/rooms", nil)
	request.Header.Set("Authorization", "Bearer editor")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	var rooms []centralRoom
	if err := json.Unmarshal(response.Body.Bytes(), &rooms); err != nil || len(rooms) != 1 || rooms[0].Location != "Bonn" {
		t.Fatalf("stale retry overwrote data: %s %v", response.Body.String(), err)
	}
	events, _, err = store.eventsAfter(0)
	if err != nil || len(events) != 2 {
		t.Fatalf("duplicate persisted: %d %v", len(events), err)
	}
	if err := store.appendRoomSync(9, "linux-a", "op-1", "different", syncEvent{Topic: "room.updated", Payload: "{}", At: "x"}); err == nil {
		t.Fatal("duplicate receipt unexpectedly committed")
	}
	events, _, err = store.eventsAfter(0)
	if err != nil || len(events) != 2 {
		t.Fatalf("failed receipt did not roll back event: %d %v", len(events), err)
	}
}

func TestEmployeeAccountLinkRequiresExistingUniqueAccountAndSurvivesLegacyEvents(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"secret-admin":{"employeeId":9,"permissions":["employees.manage"]},"secret-reader":{"employeeId":7,"permissions":["employees.read"]},"second-reader-token":{"employeeId":7,"permissions":["employees.read"]}}`)
	journal := filepath.Join(t.TempDir(), "employee-links.ndjson")
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	create := func(name string) centralEmployee {
		r := call("POST", "/api/v1/employees", "secret-admin", fmt.Sprintf(`{"name":%q,"role":"IT","department":"Support","presence":"office"}`, name))
		if r.Code != 201 {
			t.Fatalf("employee create %d %s", r.Code, r.Body.String())
		}
		var v centralEmployee
		if json.Unmarshal(r.Body.Bytes(), &v) != nil {
			t.Fatal("invalid employee")
		}
		return v
	}
	one, two := create("Ada"), create("Mira")
	accounts := call("GET", "/api/v1/employee-accounts", "secret-admin", "")
	if accounts.Code != 200 || strings.Contains(accounts.Body.String(), "secret-") {
		t.Fatalf("account listing leaked credentials: %d %s", accounts.Code, accounts.Body.String())
	}
	var ids []map[string]any
	if json.Unmarshal(accounts.Body.Bytes(), &ids) != nil || len(ids) != 2 {
		t.Fatalf("identities should be deduplicated: %s", accounts.Body.String())
	}
	if r := call("GET", "/api/v1/employee-accounts", "secret-reader", ""); r.Code != 403 {
		t.Fatalf("listing permission: %d", r.Code)
	}
	path := "/api/v1/employee-account-link?id=" + one.ID
	if r := call("PUT", path, "secret-admin", `{"accountEmployeeId":12345}`); r.Code != 400 {
		t.Fatalf("unknown identity: %d", r.Code)
	}
	if r := call("PUT", path, "secret-reader", `{"accountEmployeeId":7}`); r.Code != 403 {
		t.Fatalf("write permission: %d", r.Code)
	}
	if r := call("PUT", path, "secret-admin", `{"accountEmployeeId":7}`); r.Code != 200 {
		t.Fatalf("link: %d %s", r.Code, r.Body.String())
	}
	if r := call("PUT", "/api/v1/employee-account-link?id="+two.ID, "secret-admin", `{"accountEmployeeId":7}`); r.Code != 409 {
		t.Fatalf("duplicate: %d", r.Code)
	}
	payload := fmt.Sprintf(`{"id":%q,"name":"Ada Geändert","role":"IT","department":"Support","presence":"remote","accountEmployeeId":9,"active":true}`, one.ID)
	event, _ := json.Marshal(syncEvent{Topic: "employee.updated", Payload: payload})
	if r := call("POST", "/events", "secret-admin", string(event)); r.Code != 202 {
		t.Fatalf("legacy event: %d %s", r.Code, r.Body.String())
	}
	handler = syncHandler(journal, "legacy")
	r := call("GET", "/api/v1/employees", "secret-admin", "")
	var employees []centralEmployee
	if err := json.Unmarshal(r.Body.Bytes(), &employees); err != nil {
		t.Fatal(err)
	}
	var found centralEmployee
	for _, e := range employees {
		if e.ID == one.ID {
			found = e
		}
	}
	if found.AccountEmployeeID != 7 || found.Name != "Ada Geändert" || found.Presence != "remote" {
		t.Fatalf("link lost or forged on replay: %+v", found)
	}
	if r := call("PUT", path, "secret-admin", `{"accountEmployeeId":0}`); r.Code != 200 {
		t.Fatalf("unlink: %d", r.Code)
	}
	if r := call("PUT", "/api/v1/employee-account-link?id="+two.ID, "secret-admin", `{"accountEmployeeId":7}`); r.Code != 200 {
		t.Fatalf("reassign: %d", r.Code)
	}
}

func TestEmployeeSyncConflictPermissionsAndDurability(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"editor":{"employeeId":9,"permissions":["employees.manage"]},"reader":{"employeeId":7,"permissions":["employees.read"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	request := func(method, path, token, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	create := `{"clientId":"linux","operationId":"one","action":"create","employee":{"id":"linux:employee:1","name":"Ada","role":"Developer","department":"IT","presence":"office","active":true},"expected":null}`
	if r := request("POST", "/api/v1/employee-sync", "reader", create); r.Code != 403 {
		t.Fatalf("permission: %d", r.Code)
	}
	if r := request("POST", "/api/v1/employee-sync", "editor", create); r.Code != 202 {
		t.Fatalf("create: %d %s", r.Code, r.Body.String())
	}
	if r := request("POST", "/api/v1/employee-sync", "editor", create); r.Code != 202 {
		t.Fatalf("retry: %d", r.Code)
	}
	if r := request("POST", "/api/v1/employee-sync", "editor", strings.Replace(create, "Ada", "Anne", 1)); r.Code != 409 {
		t.Fatalf("operation collision: %d", r.Code)
	}
	update := `{"clientId":"linux","operationId":"two","action":"update","employee":{"id":"linux:employee:1","name":"Ada","role":"Developer","department":"IT","presence":"remote","active":true},"expected":{"id":"linux:employee:1","name":"Ada","role":"Developer","department":"IT","presence":"office","active":true}}`
	if r := request("POST", "/api/v1/employee-sync", "editor", update); r.Code != 202 {
		t.Fatalf("update: %d %s", r.Code, r.Body.String())
	}
	if r := request("POST", "/api/v1/employee-sync", "editor", strings.Replace(update, `"two"`, `"three"`, 1)); r.Code != 409 {
		t.Fatalf("stale: %d", r.Code)
	}
	forged := strings.Replace(update, `"two"`, `"four"`, 1)
	forged = strings.Replace(forged, `"id":"linux:employee:1"`, `"accountEmployeeId":7,"id":"linux:employee:1"`, 1)
	forged = strings.Replace(forged, `"presence":"office"`, `"presence":"remote"`, 1)
	if r := request("POST", "/api/v1/employee-sync", "editor", forged); r.Code != 409 {
		t.Fatalf("forged account: %d", r.Code)
	}
	duplicate := strings.Replace(strings.Replace(create, `"one"`, `"five"`, 1), `linux:employee:1`, `linux:employee:2`, 1)
	if r := request("POST", "/api/v1/employee-sync", "editor", duplicate); r.Code != 409 {
		t.Fatalf("duplicate name: %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := request("POST", "/api/v1/employee-sync", "editor", create); r.Code != 202 {
		t.Fatalf("restart retry: %d", r.Code)
	}
	events, _, err := store.eventsAfter(0)
	if err != nil || len(events) != 2 {
		t.Fatalf("event count %d: %v", len(events), err)
	}
}

func TestLeaveSyncIdentityConflictsAndRestart(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"requester":{"employeeId":7,"permissions":["leave.request"]},"manager":{"employeeId":9,"permissions":["leave.approve"]},"other":{"employeeId":8,"permissions":["leave.request","leave.approve"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	post := func(token, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, "/api/v1/leave-sync", strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	create := `{"clientId":"linux","operationId":"request-1","action":"request","leave":{"id":"linux:leave:1","employeeId":7,"startsAt":1800000000,"endsAt":1800086400,"reason":"Urlaub","status":"pending"},"expected":null}`
	if r := post("other", create); r.Code != 403 {
		t.Fatalf("identity forgery: %d", r.Code)
	}
	if r := post("manager", create); r.Code != 403 {
		t.Fatalf("missing request permission: %d", r.Code)
	}
	if r := post("requester", create); r.Code != 202 {
		t.Fatalf("request: %d %s", r.Code, r.Body.String())
	}
	if r := post("requester", create); r.Code != 202 {
		t.Fatalf("retry: %d", r.Code)
	}
	if r := post("requester", strings.Replace(create, "Urlaub", "Andere", 1)); r.Code != 409 {
		t.Fatalf("operation collision: %d", r.Code)
	}
	decision := `{"clientId":"linux","operationId":"decision-1","action":"decide","leave":{"id":"linux:leave:1","employeeId":7,"startsAt":1800000000,"endsAt":1800086400,"reason":"Urlaub","status":"approved","decidedBy":9},"expected":{"id":"linux:leave:1","employeeId":7,"startsAt":1800000000,"endsAt":1800086400,"reason":"Urlaub","status":"pending"}}`
	if r := post("other", decision); r.Code != 403 {
		t.Fatalf("forged decider: %d", r.Code)
	}
	self := strings.Replace(decision, `"decidedBy":9`, `"decidedBy":7`, 1)
	if r := post("requester", self); r.Code != 403 {
		t.Fatalf("self decision: %d", r.Code)
	}
	modified := strings.Replace(decision, "Urlaub", "Manipuliert", 1)
	if r := post("manager", modified); r.Code != 409 {
		t.Fatalf("changed leave details: %d", r.Code)
	}
	if r := post("manager", decision); r.Code != 202 {
		t.Fatalf("decision: %d %s", r.Code, r.Body.String())
	}
	if r := post("manager", decision); r.Code != 202 {
		t.Fatalf("decision retry: %d", r.Code)
	}
	if r := post("manager", strings.Replace(decision, "decision-1", "decision-2", 1)); r.Code != 409 {
		t.Fatalf("stale decision: %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := post("requester", create); r.Code != 202 {
		t.Fatalf("request retry after restart: %d", r.Code)
	}
	if r := post("manager", decision); r.Code != 202 {
		t.Fatalf("decision retry after restart: %d", r.Code)
	}
	events, _, err := store.eventsAfter(0)
	if err != nil || len(events) != 2 {
		t.Fatalf("event count %d: %v", len(events), err)
	}
}

func TestCentralScheduledTasksAndLegacyUpdates(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage","projects.manage"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer manager")
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	r := call("POST", "/api/v1/tasks", `{"title":"Bericht","department":"IT","priority":"Mittel","assigneeId":9,"dueAt":1900000000,"recurrenceDays":7}`)
	if r.Code != 201 {
		t.Fatalf("create: %d %s", r.Code, r.Body.String())
	}
	var task centralTask
	if err := json.Unmarshal(r.Body.Bytes(), &task); err != nil || task.DueAt != 1900000000 || task.RecurrenceDays != 7 {
		t.Fatalf("schedule: %s %v", r.Body.String(), err)
	}
	var projectIDs []string
	for _, name := range []string{"Eins", "Zwei"} {
		p := call("POST", "/api/v1/projects", fmt.Sprintf(`{"name":%q,"department":"IT","status":"planned","ownerId":9}`, name))
		var project centralProject
		if p.Code != 201 || json.Unmarshal(p.Body.Bytes(), &project) != nil {
			t.Fatalf("project: %d %s", p.Code, p.Body.String())
		}
		projectIDs = append(projectIDs, project.ID)
		body := fmt.Sprintf(`{"clientId":"test","operationId":%q,"projectId":%q,"taskId":%q,"present":true,"expected":false}`, name, project.ID, task.ID)
		if result := call("POST", "/api/v1/project-assignment-sync", body); result.Code != 202 {
			t.Fatalf("assignment: %d %s", result.Code, result.Body.String())
		}
	}
	r = call("PUT", "/api/v1/tasks/"+task.ID, `{"title":"Bericht neu","department":"IT","priority":"Mittel","assigneeId":9}`)
	if r.Code != 200 {
		t.Fatal(r.Body.String())
	}
	if err := json.Unmarshal(r.Body.Bytes(), &task); err != nil || task.DueAt != 1900000000 || task.RecurrenceDays != 7 {
		t.Fatalf("legacy edit lost schedule: %s", r.Body.String())
	}
	legacyPayload := fmt.Sprintf(`{"id":%q,"title":"Bericht neu","department":"IT","priority":"Mittel","status":"open","assigneeId":9,"createdBy":9,"active":true}`, task.ID)
	legacyEvent := fmt.Sprintf(`{"topic":"task.updated","payload":%q}`, legacyPayload)
	if r := call("POST", "/events", legacyEvent); r.Code != 202 {
		t.Fatalf("legacy event: %d %s", r.Code, r.Body.String())
	}
	r = call("GET", "/api/v1/tasks", "")
	var legacy []centralTask
	if err := json.Unmarshal(r.Body.Bytes(), &legacy); err != nil || len(legacy) != 1 || legacy[0].DueAt != 1900000000 || legacy[0].RecurrenceDays != 7 {
		t.Fatalf("legacy event lost schedule: %s", r.Body.String())
	}
	if r := call("POST", "/api/v1/tasks/"+task.ID+"/status", `{"status":"in_progress"}`); r.Code != 200 {
		t.Fatal(r.Body.String())
	}
	if r := call("POST", "/api/v1/tasks/"+task.ID+"/status", `{"status":"done"}`); r.Code != 200 {
		t.Fatal(r.Body.String())
	}
	if r := call("POST", "/api/v1/tasks/"+task.ID+"/status", `{"status":"done"}`); r.Code != 409 {
		t.Fatalf("double completion: %d", r.Code)
	}
	r = call("GET", "/api/v1/tasks", "")
	var all []centralTask
	if err := json.Unmarshal(r.Body.Bytes(), &all); err != nil || len(all) != 2 {
		t.Fatalf("tasks: %s", r.Body.String())
	}
	var next centralTask
	for _, v := range all {
		if v.ID != task.ID {
			next = v
		}
	}
	if next.Status != "open" || next.DueAt != 1900000000+7*86400 || next.RecurrenceDays != 7 || next.Title != "Bericht neu" {
		t.Fatalf("next: %+v", next)
	}
	checkAssignments := func() {
		r := call("GET", "/api/v1/project-assignment-sync", "")
		var edges []map[string]string
		if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &edges) != nil {
			t.Fatalf("assignments: %d %s", r.Code, r.Body.String())
		}
		found := map[string]bool{}
		for _, edge := range edges {
			if edge["taskId"] == next.ID {
				found[edge["projectId"]] = true
			}
		}
		for _, id := range projectIDs {
			if !found[id] {
				t.Fatalf("missing successor assignment: %s", r.Body.String())
			}
		}
	}
	checkAssignments()
	for _, status := range []string{"open", "done"} {
		if result := call("POST", "/api/v1/tasks/"+task.ID+"/status", fmt.Sprintf(`{"status":%q}`, status)); result.Code != 200 {
			t.Fatalf("recompletion: %d %s", result.Code, result.Body.String())
		}
	}
	if result := call("GET", "/api/v1/tasks", ""); json.Unmarshal(result.Body.Bytes(), &all) != nil || len(all) != 2 {
		t.Fatalf("duplicate successor: %s", result.Body.String())
	}
	r = call("PUT", "/api/v1/tasks/"+next.ID, `{"title":"Bericht neu","department":"IT","priority":"Mittel","assigneeId":9,"dueAt":0,"recurrenceDays":0}`)
	if r.Code != 200 {
		t.Fatal(r.Body.String())
	}
	if err := json.Unmarshal(r.Body.Bytes(), &next); err != nil || next.DueAt != 0 || next.RecurrenceDays != 0 {
		t.Fatalf("clear: %s", r.Body.String())
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	checkAssignments()
	for _, status := range []string{"open", "done"} {
		if result := call("POST", "/api/v1/tasks/"+task.ID+"/status", fmt.Sprintf(`{"status":%q}`, status)); result.Code != 200 {
			t.Fatalf("recompletion after restart: %d %s", result.Code, result.Body.String())
		}
	}
	r = call("GET", "/api/v1/tasks", "")
	all = nil
	if err := json.Unmarshal(r.Body.Bytes(), &all); err != nil || len(all) != 2 {
		t.Fatalf("restart: %s", r.Body.String())
	}
	for _, v := range all {
		if v.ID == task.ID && v.DueAt != 1900000000 {
			t.Fatalf("restart lost schedule: %+v", v)
		}
	}
	if r := call("POST", "/api/v1/tasks", `{"title":"x","department":"IT","priority":"Mittel","assigneeId":9,"dueAt":-1}`); r.Code != 400 {
		t.Fatalf("negative due: %d", r.Code)
	}
}

func TestTaskRecurrenceReservationSurvivesRestart(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path string, body any) *httptest.ResponseRecorder {
		var data []byte
		if body != nil {
			data, _ = json.Marshal(body)
		}
		req := httptest.NewRequest(method, path, bytes.NewReader(data))
		req.Header.Set("Authorization", "Bearer manager")
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	created := call("POST", "/api/v1/tasks", map[string]any{"title": "Offline", "department": "IT", "priority": "Mittel", "assigneeId": 9, "dueAt": 1900000000, "recurrenceDays": 7})
	var source centralTask
	if created.Code != 201 || json.Unmarshal(created.Body.Bytes(), &source) != nil {
		t.Fatal(created.Body.String())
	}
	completed := source
	completed.Status = "done"
	successorID := "linux:task:successor"
	op := map[string]any{"clientId": "linux", "operationId": "source-done", "action": "update", "task": completed, "expected": source, "recurrenceSuccessorId": successorID}
	invalid := map[string]any{"clientId": "linux", "operationId": "bad-namespace", "action": "update", "task": completed, "expected": source, "recurrenceSuccessorId": "another:task:1"}
	if r := call("POST", "/api/v1/task-sync", invalid); r.Code != 409 {
		t.Fatalf("foreign namespace reserved: %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", "/api/v1/task-sync", op); r.Code != 202 {
		t.Fatalf("reserve: %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", "/api/v1/task-sync", op); r.Code != 202 {
		t.Fatalf("retry: %d", r.Code)
	}
	if r := call("POST", "/api/v1/tasks/"+source.ID+"/status", map[string]string{"status": "open"}); r.Code != 200 {
		t.Fatal(r.Body.String())
	}
	if r := call("POST", "/api/v1/tasks/"+source.ID+"/status", map[string]string{"status": "done"}); r.Code != 409 {
		t.Fatalf("unfinished reservation: %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := call("POST", "/api/v1/tasks/"+source.ID+"/status", map[string]string{"status": "done"}); r.Code != 409 {
		t.Fatalf("reservation lost on restart: %d", r.Code)
	}
	successor := source
	successor.ID = successorID
	successor.DueAt += 7 * 86400
	if r := call("POST", "/api/v1/task-sync", map[string]any{"clientId": "other-device", "operationId": "wrong-owner", "action": "create", "task": successor, "expected": nil}); r.Code != 409 {
		t.Fatalf("other client used reserved successor: %d %s", r.Code, r.Body.String())
	}
	payload, _ := json.Marshal(successor)
	if r := call("POST", "/events", map[string]any{"topic": "task.created", "payload": string(payload)}); r.Code != 409 {
		t.Fatalf("legacy event used reserved successor: %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", "/api/v1/task-sync", map[string]any{"clientId": "linux", "operationId": "successor", "action": "create", "task": successor, "expected": nil}); r.Code != 202 {
		t.Fatalf("successor create: %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", "/api/v1/tasks/"+source.ID+"/status", map[string]string{"status": "done"}); r.Code != 200 {
		t.Fatal(r.Body.String())
	}
	var tasks []centralTask
	r := call("GET", "/api/v1/tasks", nil)
	if json.Unmarshal(r.Body.Bytes(), &tasks) != nil || len(tasks) != 2 {
		t.Fatalf("duplicate successor: %s", r.Body.String())
	}
}

func TestOwnerlessRecurrenceReservationClaimsOriginalReceipt(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage"]},"other":{"employeeId":10,"permissions":["tasks.manage"]}}`)
	dir := t.TempDir()
	path, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(path)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(token, path string, body any) *httptest.ResponseRecorder {
		data, _ := json.Marshal(body)
		req := httptest.NewRequest("POST", path, bytes.NewReader(data))
		req.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	r := call("manager", "/api/v1/tasks", map[string]any{"title": "Alt", "department": "IT", "priority": "Mittel", "assigneeId": 9, "dueAt": 1900000000, "recurrenceDays": 7})
	var source centralTask
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &source) != nil {
		t.Fatal(r.Body.String())
	}
	completed := source
	completed.Status = "done"
	successor := "linux:task:old"
	op := struct {
		ClientID              string       `json:"clientId"`
		OperationID           string       `json:"operationId"`
		Action                string       `json:"action"`
		Task                  centralTask  `json:"task"`
		Expected              *centralTask `json:"expected"`
		RecurrenceSuccessorID string       `json:"recurrenceSuccessorId,omitempty"`
	}{"linux", "old-op", "update", completed, &source, successor}
	canonical, _ := json.Marshal(op)
	sum := sha256.Sum256(canonical)
	fingerprint := hex.EncodeToString(sum[:])
	payload, _ := json.Marshal(struct {
		centralTask
		SuccessorID string `json:"recurrenceSuccessorId"`
	}{completed, successor})
	if err := store.appendRoomSync(9, "linux", "old-op", fingerprint, syncEvent{Topic: "task.updated", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}); err != nil {
		t.Fatal(err)
	}
	handler = syncHandler(journal, "legacy")
	if r := call("other", "/api/v1/task-sync", op); r.Code != 409 {
		t.Fatalf("unproven actor: %d %s", r.Code, r.Body.String())
	}
	changed := op
	changed.Task.Title = "abweichend"
	if r := call("manager", "/api/v1/task-sync", changed); r.Code != 409 {
		t.Fatalf("unproven payload: %d %s", r.Code, r.Body.String())
	}
	if r := call("manager", "/api/v1/task-sync", op); r.Code != 202 {
		t.Fatalf("claim: %d %s", r.Code, r.Body.String())
	}
	store.close()
	store, err = openCentralStore(path)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := call("manager", "/api/v1/task-sync", op); r.Code != 202 {
		t.Fatalf("retry after restart: %d %s", r.Code, r.Body.String())
	}
	next := source
	next.ID = successor
	next.DueAt += 7 * 86400
	if r := call("manager", "/api/v1/task-sync", map[string]any{"clientId": "linux", "operationId": "next", "action": "create", "task": next, "expected": nil}); r.Code != 202 {
		t.Fatalf("successor after claim: %d %s", r.Code, r.Body.String())
	}
}

func TestCentralTaskDependenciesPermissionsCyclesAndReplay(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage"]},"reader":{"employeeId":7,"permissions":["tasks.read"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	create := func(title string) centralTask {
		r := call("POST", "/api/v1/tasks", "manager", fmt.Sprintf(`{"title":%q,"department":"IT","priority":"Mittel","assigneeId":9}`, title))
		if r.Code != 201 {
			t.Fatal(r.Body.String())
		}
		var v centralTask
		if err := json.Unmarshal(r.Body.Bytes(), &v); err != nil {
			t.Fatal(err)
		}
		return v
	}
	a, b, c := create("A"), create("B"), create("C")
	dep := func(x, y string) string { return "/api/v1/task-dependencies?taskId=" + x + "&dependencyId=" + y }
	if r := call("POST", dep(a.ID, b.ID), "reader", ""); r.Code != 403 {
		t.Fatalf("reader add: %d", r.Code)
	}
	if r := call("GET", dep(a.ID, b.ID), "reader", ""); r.Code != 404 {
		t.Fatalf("private task: %d", r.Code)
	}
	if r := call("POST", dep(a.ID, b.ID), "manager", ""); r.Code != 204 {
		t.Fatalf("add: %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", dep(b.ID, c.ID), "manager", ""); r.Code != 204 {
		t.Fatal(r.Body.String())
	}
	if r := call("POST", dep(c.ID, a.ID), "manager", ""); r.Code != 409 {
		t.Fatalf("cycle: %d", r.Code)
	}
	if r := call("POST", dep(a.ID, a.ID), "manager", ""); r.Code != 400 {
		t.Fatalf("self edge: %d", r.Code)
	}
	if r := call("POST", "/api/v1/tasks/"+a.ID+"/status", "manager", `{"status":"in_progress"}`); r.Code != 200 {
		t.Fatal(r.Body.String())
	}
	if r := call("POST", "/api/v1/tasks/"+a.ID+"/status", "manager", `{"status":"done"}`); r.Code != 409 {
		t.Fatalf("blocked completion: %d", r.Code)
	}
	payload := fmt.Sprintf(`{"id":%q,"title":"A","department":"IT","priority":"Mittel","status":"done","assigneeId":9,"createdBy":9,"active":true}`, a.ID)
	if r := call("POST", "/events", "manager", fmt.Sprintf(`{"topic":"task.status.changed","payload":%q}`, payload)); r.Code != 409 {
		t.Fatalf("event bypass: %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := call("GET", dep(a.ID, b.ID), "manager", ""); r.Code != 200 || !strings.Contains(r.Body.String(), b.ID) {
		t.Fatalf("dependency replay: %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", "/api/v1/tasks/"+a.ID+"/status", "manager", `{"status":"done"}`); r.Code != 409 {
		t.Fatalf("restart blocked: %d", r.Code)
	}
	if r := call("DELETE", dep(a.ID, b.ID), "manager", ""); r.Code != 204 {
		t.Fatalf("remove: %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", "/api/v1/tasks/"+a.ID+"/status", "manager", `{"status":"done"}`); r.Code != 200 {
		t.Fatalf("completion after remove: %d %s", r.Code, r.Body.String())
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := call("GET", dep(a.ID, b.ID), "manager", ""); r.Code != 200 || strings.Contains(r.Body.String(), b.ID) {
		t.Fatalf("removal replay: %d %s", r.Code, r.Body.String())
	}
}

func TestCentralSubtasksCompletionRecurrenceAndReplay(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage"]},"reader":{"employeeId":7,"permissions":["tasks.read"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	r := call("POST", "/api/v1/tasks", "manager", `{"title":"Review","department":"IT","priority":"Mittel","assigneeId":9,"dueAt":1900000000,"recurrenceDays":7}`)
	if r.Code != 201 {
		t.Fatal(r.Body.String())
	}
	var task centralTask
	_ = json.Unmarshal(r.Body.Bytes(), &task)
	path := "/api/v1/task-subtasks?taskId=" + task.ID
	if r := call("POST", path, "reader", `{"title":"A","status":"open","assigneeId":0,"dueAt":0}`); r.Code != 403 {
		t.Fatalf("permission: %d", r.Code)
	}
	if r := call("GET", path, "reader", ""); r.Code != 404 {
		t.Fatalf("private task: %d", r.Code)
	}
	r = call("POST", path, "manager", `{"title":"Prüfen","status":"open","assigneeId":9,"dueAt":1900000000}`)
	if r.Code != 201 {
		t.Fatalf("create: %d %s", r.Code, r.Body.String())
	}
	var sub centralSubtask
	_ = json.Unmarshal(r.Body.Bytes(), &sub)
	before, _ := json.Marshal(sub)
	compared := fmt.Sprintf(`{"title":"Prüfen","status":"open","assigneeId":9,"dueAt":1900000000,"expected":%s}`, before)
	if r := call("PUT", path+"&id="+sub.ID, "manager", compared); r.Code != 200 {
		t.Fatalf("compared update: %d %s", r.Code, r.Body.String())
	}
	stale := sub
	stale.Title = "Anderer Stand"
	old, _ := json.Marshal(stale)
	if r := call("PUT", path+"&id="+sub.ID, "manager", fmt.Sprintf(`{"title":"Unerwünscht","status":"done","assigneeId":9,"dueAt":0,"expected":%s}`, old)); r.Code != 409 {
		t.Fatalf("stale subtask: %d %s", r.Code, r.Body.String())
	}
	if r := call("DELETE", path+"&id="+sub.ID, "manager", fmt.Sprintf(`{"expected":%s}`, old)); r.Code != 409 {
		t.Fatalf("stale archive: %d %s", r.Code, r.Body.String())
	}
	temporary := call("POST", path, "manager", `{"title":"Archivtest","status":"open","assigneeId":0,"dueAt":0}`)
	if temporary.Code != 201 {
		t.Fatal(temporary.Body.String())
	}
	var archiveSub centralSubtask
	_ = json.Unmarshal(temporary.Body.Bytes(), &archiveSub)
	archiveExpected, _ := json.Marshal(archiveSub)
	if r := call("DELETE", path+"&id="+archiveSub.ID, "manager", fmt.Sprintf(`{"expected":%s}`, archiveExpected)); r.Code != 200 {
		t.Fatalf("compared archive: %d %s", r.Code, r.Body.String())
	}
	r = call("POST", path, "manager", `{"title":"Entfällt","status":"cancelled","assigneeId":0,"dueAt":0}`)
	if r.Code != 201 {
		t.Fatal(r.Body.String())
	}
	if r := call("POST", "/api/v1/tasks/"+task.ID+"/status", "manager", `{"status":"in_progress"}`); r.Code != 200 {
		t.Fatal(r.Body.String())
	}
	if r := call("POST", "/api/v1/tasks/"+task.ID+"/status", "manager", `{"status":"done"}`); r.Code != 409 {
		t.Fatalf("unfinished: %d", r.Code)
	}
	payload := fmt.Sprintf(`{"id":%q,"title":"Review","department":"IT","priority":"Mittel","status":"done","assigneeId":9,"createdBy":9,"active":true}`, task.ID)
	if r := call("POST", "/events", "manager", fmt.Sprintf(`{"topic":"task.status.changed","payload":%q}`, payload)); r.Code != 409 {
		t.Fatalf("event bypass: %d", r.Code)
	}
	if r := call("PUT", path+"&id="+sub.ID, "manager", `{"title":"Prüfen","status":"done","assigneeId":9,"dueAt":1900000000}`); r.Code != 200 {
		t.Fatal(r.Body.String())
	}
	if r := call("POST", "/api/v1/tasks/"+task.ID+"/status", "manager", `{"status":"done"}`); r.Code != 200 {
		t.Fatalf("complete: %d %s", r.Code, r.Body.String())
	}
	r = call("GET", "/api/v1/tasks", "manager", "")
	var tasks []centralTask
	_ = json.Unmarshal(r.Body.Bytes(), &tasks)
	var next centralTask
	for _, v := range tasks {
		if v.ID != task.ID {
			next = v
		}
	}
	if next.ID == "" {
		t.Fatalf("missing recurrence: %s", r.Body.String())
	}
	r = call("GET", "/api/v1/task-subtasks?taskId="+next.ID, "manager", "")
	var children []centralSubtask
	_ = json.Unmarshal(r.Body.Bytes(), &children)
	if len(children) != 1 || children[0].Title != "Prüfen" || children[0].Status != "open" || children[0].DueAt != 1900000000+7*86400 {
		t.Fatalf("recurrence subtasks: %s", r.Body.String())
	}
	if r := call("POST", path, "manager", `{"title":"Late","status":"open","assigneeId":0,"dueAt":0}`); r.Code != 409 {
		t.Fatalf("open subtask on completed parent: %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	r = call("GET", "/api/v1/task-subtasks?taskId="+next.ID, "manager", "")
	children = nil
	_ = json.Unmarshal(r.Body.Bytes(), &children)
	if len(children) != 1 || children[0].Status != "open" {
		t.Fatalf("replay: %s", r.Body.String())
	}
	if r := call("DELETE", "/api/v1/task-subtasks?taskId="+next.ID+"&id="+children[0].ID, "manager", ""); r.Code != 200 {
		t.Fatalf("archive: %d %s", r.Code, r.Body.String())
	}
}

func TestTaskSyncDurabilityConflictsAndCompletionRules(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage"]},"reader":{"employeeId":7,"permissions":["tasks.read"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	create := `{"clientId":"linux","operationId":"one","action":"create","task":{"id":"linux:task:1","title":"Review","department":"IT","priority":"Mittel","status":"open","assigneeId":9,"createdBy":9,"active":true,"dueAt":1900000000,"recurrenceDays":7},"expected":null}`
	if r := call("POST", "/api/v1/task-sync", "reader", create); r.Code != 403 {
		t.Fatalf("permission: %d", r.Code)
	}
	forged := strings.Replace(create, `"createdBy":9`, `"createdBy":7`, 1)
	if r := call("POST", "/api/v1/task-sync", "manager", forged); r.Code != 403 {
		t.Fatalf("creator: %d", r.Code)
	}
	if r := call("POST", "/api/v1/task-sync", "manager", create); r.Code != 202 {
		t.Fatalf("create %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", "/api/v1/task-sync", "manager", create); r.Code != 202 {
		t.Fatalf("retry: %d", r.Code)
	}
	if r := call("POST", "/api/v1/task-sync", "manager", strings.Replace(create, "Review", "Other", 1)); r.Code != 409 {
		t.Fatalf("operation reuse: %d", r.Code)
	}
	update := `{"clientId":"linux","operationId":"two","action":"update","task":{"id":"linux:task:1","title":"Review","department":"IT","priority":"Mittel","status":"done","assigneeId":9,"createdBy":9,"active":true,"dueAt":1900000000,"recurrenceDays":7},"expected":{"id":"linux:task:1","title":"Review","department":"IT","priority":"Mittel","status":"open","assigneeId":9,"createdBy":9,"active":true,"dueAt":1900000000,"recurrenceDays":7}}`
	r := call("POST", "/api/v1/task-subtasks?taskId=linux:task:1", "manager", `{"title":"Check","status":"open","assigneeId":9,"dueAt":0}`)
	if r.Code != 201 {
		t.Fatalf("subtask: %d %s", r.Code, r.Body.String())
	}
	var sub centralSubtask
	_ = json.Unmarshal(r.Body.Bytes(), &sub)
	if r := call("POST", "/api/v1/task-sync", "manager", update); r.Code != 409 {
		t.Fatalf("open subtask: %d", r.Code)
	}
	if r := call("PUT", "/api/v1/task-subtasks?taskId=linux:task:1&id="+sub.ID, "manager", `{"title":"Check","status":"done","assigneeId":9,"dueAt":0}`); r.Code != 200 {
		t.Fatal(r.Body.String())
	}
	if r := call("POST", "/api/v1/task-sync", "manager", update); r.Code != 202 {
		t.Fatalf("completion: %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", "/api/v1/task-sync", "manager", strings.Replace(update, `"two"`, `"three"`, 1)); r.Code != 409 {
		t.Fatalf("stale: %d", r.Code)
	}
	r = call("GET", "/api/v1/tasks?includeArchived=true", "manager", "")
	var tasks []centralTask
	_ = json.Unmarshal(r.Body.Bytes(), &tasks)
	if len(tasks) != 1 || tasks[0].Status != "done" {
		t.Fatalf("duplicate successor: %s", r.Body.String())
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := call("POST", "/api/v1/task-sync", "manager", create); r.Code != 202 {
		t.Fatalf("retry after restart: %d", r.Code)
	}
	if r := call("POST", "/api/v1/task-sync", "manager", update); r.Code != 202 {
		t.Fatalf("completion retry after restart: %d", r.Code)
	}
}

func TestSubtaskSyncIdempotenceAndParentRules(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage"]},"reader":{"employeeId":7,"permissions":["tasks.read"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	r := call("POST", "/api/v1/tasks", "manager", `{"title":"Parent","department":"IT","priority":"Mittel","assigneeId":9}`)
	if r.Code != 201 {
		t.Fatal(r.Body.String())
	}
	var task centralTask
	_ = json.Unmarshal(r.Body.Bytes(), &task)
	sub := centralSubtask{ID: "linux:subtask:1", TaskID: task.ID, Title: "A", Status: "open", AssigneeID: 0, DueAt: 100, Active: true}
	op := struct {
		ClientID    string          `json:"clientId"`
		OperationID string          `json:"operationId"`
		Action      string          `json:"action"`
		Subtask     centralSubtask  `json:"subtask"`
		Expected    *centralSubtask `json:"expected"`
	}{"linux", "one", "create", sub, nil}
	body := func() string { v, _ := json.Marshal(op); return string(v) }
	if r := call("POST", "/api/v1/subtask-sync", "reader", body()); r.Code != 403 {
		t.Fatalf("permission %d", r.Code)
	}
	if r := call("POST", "/api/v1/subtask-sync", "manager", body()); r.Code != 202 {
		t.Fatalf("create %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", "/api/v1/subtask-sync", "manager", body()); r.Code != 202 {
		t.Fatalf("retry %d", r.Code)
	}
	op.Subtask.Title = "other"
	if r := call("POST", "/api/v1/subtask-sync", "manager", body()); r.Code != 409 {
		t.Fatalf("reused operation %d", r.Code)
	}
	op.OperationID = "two"
	op.Action = "update"
	op.Expected = &sub
	op.Subtask = sub
	op.Subtask.Status = "done"
	if r := call("POST", "/api/v1/subtask-sync", "manager", body()); r.Code != 202 {
		t.Fatalf("update %d %s", r.Code, r.Body.String())
	}
	op.OperationID = "three"
	if r := call("POST", "/api/v1/subtask-sync", "manager", body()); r.Code != 409 {
		t.Fatalf("stale %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	op.OperationID = "two"
	if r := call("POST", "/api/v1/subtask-sync", "manager", body()); r.Code != 202 {
		t.Fatalf("restart retry %d", r.Code)
	}
	r = call("GET", "/api/v1/subtask-sync", "manager", "")
	var all []centralSubtask
	if err := json.Unmarshal(r.Body.Bytes(), &all); err != nil || len(all) != 1 || all[0].Status != "done" {
		t.Fatalf("replay %s", r.Body.String())
	}
	op.OperationID = "four"
	op.Action = "archive"
	current := op.Subtask
	op.Expected = &current
	op.Subtask.Active = false
	if r := call("POST", "/api/v1/subtask-sync", "manager", body()); r.Code != 202 {
		t.Fatalf("archive %d %s", r.Code, r.Body.String())
	}
	r = call("GET", "/api/v1/subtask-sync", "manager", "")
	all = nil
	_ = json.Unmarshal(r.Body.Bytes(), &all)
	if len(all) != 1 || all[0].Active {
		t.Fatalf("archived snapshot %s", r.Body.String())
	}
}

func TestDependencySyncDurabilityAndCycles(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage"]},"reader":{"employeeId":7,"permissions":["tasks.read"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	makeTask := func(title string) string {
		r := call("POST", "/api/v1/tasks", "manager", fmt.Sprintf(`{"title":%q,"department":"IT","priority":"Mittel","assigneeId":9}`, title))
		var v centralTask
		if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &v) != nil {
			t.Fatal(r.Body.String())
		}
		return v.ID
	}
	a, b := makeTask("A"), makeTask("B")
	makeOp := func(op, x, y string, present, expected bool) string {
		return fmt.Sprintf(`{"clientId":"linux","operationId":%q,"taskId":%q,"dependencyId":%q,"present":%t,"expected":%t}`, op, x, y, present, expected)
	}
	add := makeOp("one", a, b, true, false)
	if r := call("POST", "/api/v1/dependency-sync", "reader", add); r.Code != 403 {
		t.Fatalf("permission: %d", r.Code)
	}
	if r := call("POST", "/api/v1/dependency-sync", "manager", add); r.Code != 202 {
		t.Fatalf("add: %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", "/api/v1/dependency-sync", "manager", add); r.Code != 202 {
		t.Fatalf("retry: %d", r.Code)
	}
	if r := call("POST", "/api/v1/dependency-sync", "manager", makeOp("one", b, a, true, false)); r.Code != 409 {
		t.Fatalf("identity reuse: %d", r.Code)
	}
	if r := call("POST", "/api/v1/dependency-sync", "manager", makeOp("cycle", b, a, true, false)); r.Code != 409 {
		t.Fatalf("cycle: %d", r.Code)
	}
	if r := call("POST", "/api/v1/dependency-sync", "manager", makeOp("stale", a, b, true, false)); r.Code != 409 {
		t.Fatalf("stale: %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := call("POST", "/api/v1/dependency-sync", "manager", add); r.Code != 202 {
		t.Fatalf("restart retry: %d", r.Code)
	}
	r := call("GET", "/api/v1/dependency-sync", "manager", "")
	if r.Code != 200 || !strings.Contains(r.Body.String(), a) || !strings.Contains(r.Body.String(), b) {
		t.Fatalf("replay: %d %s", r.Code, r.Body.String())
	}
	remove := makeOp("two", a, b, false, true)
	if r := call("POST", "/api/v1/dependency-sync", "manager", remove); r.Code != 202 {
		t.Fatalf("remove: %d %s", r.Code, r.Body.String())
	}
	r = call("GET", "/api/v1/dependency-sync", "manager", "")
	if r.Body.String() != "[]\n" {
		t.Fatalf("not removed: %s", r.Body.String())
	}
	// Exercise the same HTTP transport exported to the Windows client.
	server := httptest.NewServer(handler)
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "manager")
	windowsAdd := makeOp("windows-add", a, b, true, false)
	if code := sendDesktopDependency(windowsAdd); code != 0 {
		t.Fatalf("desktop add: %d", code)
	}
	if code := sendDesktopDependency(windowsAdd); code != 0 {
		t.Fatalf("desktop retry: %d", code)
	}
	if code := sendDesktopDependency(makeOp("windows-stale", a, b, true, false)); code != 9 {
		t.Fatalf("desktop conflict: %d", code)
	}
	t.Setenv("AURA_SYNC_TOKEN", "reader")
	if code := sendDesktopDependency(makeOp("windows-remove", a, b, false, true)); code != 8 {
		t.Fatalf("desktop permissions: %d", code)
	}
}

func TestProjectSyncDurableReceiptsAndConflicts(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["projects.manage"]},"reader":{"employeeId":7,"permissions":["projects.read"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	create := `{"clientId":"linux","operationId":"one","action":"create","project":{"id":"linux:project:1","name":"Nord","department":"IT","description":"Test","status":"planned","ownerId":9,"active":true},"expected":null}`
	if r := call("POST", "/api/v1/project-sync", "reader", create); r.Code != 403 {
		t.Fatalf("permission: %d", r.Code)
	}
	if r := call("POST", "/api/v1/project-sync", "manager", create); r.Code != 202 {
		t.Fatalf("create: %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", "/api/v1/project-sync", "manager", create); r.Code != 202 {
		t.Fatalf("retry: %d", r.Code)
	}
	if r := call("POST", "/api/v1/project-sync", "manager", strings.Replace(create, "Nord", "Anders", 1)); r.Code != 409 {
		t.Fatalf("identity reuse: %d", r.Code)
	}
	duplicate := strings.Replace(strings.Replace(create, `"one"`, `"two"`, 1), `linux:project:1`, `linux:project:2`, 1)
	if r := call("POST", "/api/v1/project-sync", "manager", duplicate); r.Code != 409 {
		t.Fatalf("duplicate name: %d", r.Code)
	}
	update := `{"clientId":"linux","operationId":"three","action":"update","project":{"id":"linux:project:1","name":"Nord","department":"IT","description":"Neu","status":"active","ownerId":9,"active":true},"expected":{"id":"linux:project:1","name":"Nord","department":"IT","description":"Test","status":"planned","ownerId":9,"active":true}}`
	if r := call("POST", "/api/v1/project-sync", "manager", update); r.Code != 202 {
		t.Fatalf("update: %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", "/api/v1/project-sync", "manager", strings.Replace(update, `"three"`, `"four"`, 1)); r.Code != 409 {
		t.Fatalf("stale: %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := call("POST", "/api/v1/project-sync", "manager", create); r.Code != 202 {
		t.Fatalf("retry after restart: %d", r.Code)
	}
	r := call("GET", "/api/v1/project-sync", "manager", "")
	var projects []centralProject
	if err := json.Unmarshal(r.Body.Bytes(), &projects); err != nil || len(projects) != 1 || projects[0].Description != "Neu" {
		t.Fatalf("replay: %s", r.Body.String())
	}
}

func TestProjectAssignmentSyncDurabilityPermissionsAndConflicts(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage","projects.manage","sync.events.read"]},"tasks":{"employeeId":7,"permissions":["tasks.manage"]},"projects":{"employeeId":8,"permissions":["projects.manage"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	project := `{"clientId":"linux","operationId":"project","action":"create","project":{"id":"p1","name":"Projekt","department":"IT","status":"active","ownerId":9,"active":true},"expected":null}`
	if r := call("POST", "/api/v1/project-sync", "manager", project); r.Code != 202 {
		t.Fatalf("project %d %s", r.Code, r.Body.String())
	}
	r := call("POST", "/api/v1/tasks", "manager", `{"title":"Aufgabe","department":"IT","priority":"Mittel","assigneeId":9}`)
	var task centralTask
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &task) != nil {
		t.Fatalf("task %d %s", r.Code, r.Body.String())
	}
	op := func(id, project, task string, present, expected bool) string {
		return fmt.Sprintf(`{"clientId":"linux","operationId":%q,"projectId":%q,"taskId":%q,"present":%t,"expected":%t}`, id, project, task, present, expected)
	}
	add := op("add", "p1", task.ID, true, false)
	for _, token := range []string{"tasks", "projects"} {
		for _, method := range []string{"GET", "POST"} {
			if r := call(method, "/api/v1/project-assignment-sync", token, add); r.Code != 403 {
				t.Fatalf("%s %s permission %d", token, method, r.Code)
			}
		}
	}
	if r := call("POST", "/api/v1/project-assignment-sync", "manager", op("missing", "none", task.ID, true, false)); r.Code != 409 {
		t.Fatalf("missing project %d", r.Code)
	}
	if r := call("POST", "/api/v1/project-assignment-sync", "manager", op("missing-task", "p1", "none", true, false)); r.Code != 409 {
		t.Fatalf("missing task %d", r.Code)
	}
	for i := 0; i < 2; i++ {
		if r := call("POST", "/api/v1/project-assignment-sync", "manager", add); r.Code != 202 {
			t.Fatalf("add/retry %d %s", r.Code, r.Body.String())
		}
	}
	if r := call("POST", "/api/v1/project-assignment-sync", "manager", op("stale", "p1", task.ID, true, false)); r.Code != 409 {
		t.Fatalf("stale %d", r.Code)
	}
	if r := call("POST", "/api/v1/project-assignment-sync", "manager", op("add", "p1", task.ID, false, true)); r.Code != 409 {
		t.Fatalf("reuse %d", r.Code)
	}
	if r := call("POST", "/events", "manager", `{"topic":"central.project_assignment.added","payload":"{}"}`); r.Code != 403 {
		t.Fatalf("forged event %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := call("POST", "/api/v1/project-assignment-sync", "manager", add); r.Code != 202 {
		t.Fatalf("restart retry %d", r.Code)
	}
	r = call("GET", "/api/v1/project-assignment-sync", "manager", "")
	if r.Code != 200 || !strings.Contains(r.Body.String(), task.ID) || !strings.Contains(r.Body.String(), "p1") {
		t.Fatalf("replay %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", "/api/v1/project-assignment-sync", "manager", op("remove", "p1", task.ID, false, true)); r.Code != 202 {
		t.Fatalf("remove %d %s", r.Code, r.Body.String())
	}
	if r := call("GET", "/api/v1/project-assignment-sync", "manager", ""); r.Body.String() != "[]\n" {
		t.Fatalf("not removed %s", r.Body.String())
	}
	feedResponse := call("GET", "/api/v1/sync/changes?desktop=1&cursor=0", "manager", "")
	var feed struct {
		Events []syncEvent `json:"events"`
	}
	if feedResponse.Code != 200 || json.Unmarshal(feedResponse.Body.Bytes(), &feed) != nil {
		t.Fatalf("desktop feed: %d %s", feedResponse.Code, feedResponse.Body.String())
	}
	var topics []string
	for _, event := range feed.Events {
		if strings.HasPrefix(event.Topic, "central.project_assignment.") {
			var edge struct {
				ProjectID string `json:"projectId"`
				TaskID    string `json:"taskId"`
			}
			if json.Unmarshal([]byte(event.Payload), &edge) != nil || edge.ProjectID != "p1" || edge.TaskID != task.ID {
				t.Fatalf("invalid desktop assignment event: %+v", event)
			}
			topics = append(topics, event.Topic)
		}
	}
	if strings.Join(topics, ",") != "central.project_assignment.added,central.project_assignment.removed" {
		t.Fatalf("desktop assignment order: %v", topics)
	}
	server := httptest.NewServer(handler)
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "manager")
	desktop := op("desktop-add", "p1", task.ID, true, false)
	if code := sendDesktopProjectAssignment(desktop); code != 0 {
		t.Fatalf("desktop send: %d", code)
	}
	if code := sendDesktopProjectAssignment(desktop); code != 0 {
		t.Fatalf("desktop retry: %d", code)
	}
	if code := sendDesktopProjectAssignment(op("desktop-stale", "p1", task.ID, true, false)); code != 9 {
		t.Fatalf("desktop conflict: %d", code)
	}
	if r := call("GET", "/api/v1/desktop-project-assignment-snapshot", "tasks", ""); r.Code != 403 {
		t.Fatalf("desktop snapshot permissions: %d", r.Code)
	}
	assignments, at, code := desktopProjectAssignmentSnapshot()
	if code != 0 || at <= 0 || len(assignments) != 1 || assignments[0]["projectId"] != "p1" || assignments[0]["taskId"] != task.ID {
		t.Fatalf("desktop snapshot: %d %d %+v", code, at, assignments)
	}
	feedNow := call("GET", "/api/v1/sync/changes?desktop=1&cursor=0", "manager", "")
	var feedState struct {
		NextCursor int64 `json:"nextCursor"`
	}
	if feedNow.Code != 200 || json.Unmarshal(feedNow.Body.Bytes(), &feedState) != nil || feedState.NextCursor != at {
		t.Fatalf("snapshot cursor: %d %s", feedNow.Code, feedNow.Body.String())
	}
	server.Close()
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	restarted := call("GET", "/api/v1/desktop-project-assignment-snapshot", "manager", "")
	var saved struct {
		Cursor      int64               `json:"cursor"`
		Assignments []map[string]string `json:"assignments"`
	}
	if restarted.Code != 200 || json.Unmarshal(restarted.Body.Bytes(), &saved) != nil || saved.Cursor != at || len(saved.Assignments) != 1 || saved.Assignments[0]["projectId"] != "p1" || saved.Assignments[0]["taskId"] != task.ID {
		t.Fatalf("snapshot restart: %d %s", restarted.Code, restarted.Body.String())
	}
}

func TestPersonalTaskCommentSyncAuthorAndVisibility(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage","employees.manage"]},"user":{"employeeId":7,"permissions":["tasks.read","collaboration.comment"]},"other":{"employeeId":8,"permissions":["tasks.read","collaboration.comment"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	r := call("POST", "/api/v1/employees", "manager", `{"name":"Ada","role":"IT","department":"IT","presence":"office"}`)
	var e centralEmployee
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &e) != nil {
		t.Fatal(r.Body.String())
	}
	if r = call("PUT", "/api/v1/employee-account-link?id="+e.ID, "manager", `{"accountEmployeeId":7}`); r.Code != 200 {
		t.Fatal(r.Body.String())
	}
	r = call("POST", "/api/v1/tasks", "manager", `{"title":"A","department":"IT","priority":"Mittel","assigneeId":7}`)
	var task centralTask
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &task) != nil {
		t.Fatal(r.Body.String())
	}
	op := func(author string) string {
		b, _ := json.Marshal(map[string]any{"clientId": "linux", "operationId": "op1", "comment": map[string]any{"id": "linux:comment:1", "subjectType": "task", "subjectId": task.ID, "authorId": author, "body": "Hallo", "createdAt": "2026-01-01T12:00:00Z"}})
		return string(b)
	}
	path := "/api/v1/personal-task-comment-sync"
	if r = call("POST", path, "other", op(e.ID)); r.Code != 403 {
		t.Fatalf("foreign actor %d %s", r.Code, r.Body.String())
	}
	if r = call("POST", path, "user", op("7")); r.Code != 403 {
		t.Fatalf("forged author %d %s", r.Code, r.Body.String())
	}
	r = call("POST", "/api/v1/tasks", "manager", `{"title":"B","department":"IT","priority":"Mittel","assigneeId":8}`)
	var hidden centralTask
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &hidden) != nil {
		t.Fatal(r.Body.String())
	}
	if r = call("POST", path, "user", strings.Replace(op(e.ID), task.ID, hidden.ID, 1)); r.Code != 403 {
		t.Fatalf("inaccessible task %d %s", r.Code, r.Body.String())
	}
	if r = call("GET", path, "user", ""); r.Code != 405 {
		t.Fatalf("personal listing %d", r.Code)
	}
	for i := 0; i < 2; i++ {
		if r = call("POST", path, "user", op(e.ID)); r.Code != 202 {
			t.Fatalf("personal comment %d %s", r.Code, r.Body.String())
		}
	}
	hash := sha256.Sum256([]byte("abc"))
	digest := hex.EncodeToString(hash[:])
	upload := func(subject, viewer string) *httptest.ResponseRecorder {
		req := httptest.NewRequest("PUT", "/api/v1/personal-attachment-objects/"+digest+"?subjectType=task&subjectId="+subject+"&viewerEmployeeId="+viewer, strings.NewReader("abc"))
		req.Header.Set("Authorization", "Bearer user")
		req.Header.Set("X-Aura-SHA256", digest)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	if r = upload(hidden.ID, e.ID); r.Code != 403 {
		t.Fatalf("hidden upload %d", r.Code)
	}
	if r = upload(task.ID, "wrong"); r.Code != 403 {
		t.Fatalf("wrong uploader %d", r.Code)
	}
	badUpload := httptest.NewRequest("PUT", "/api/v1/personal-attachment-objects/"+digest+"?subjectType=task&subjectId="+task.ID+"&viewerEmployeeId="+e.ID, strings.NewReader("wrong"))
	badUpload.Header.Set("Authorization", "Bearer user")
	badUpload.Header.Set("X-Aura-SHA256", digest)
	badResult := httptest.NewRecorder()
	handler.ServeHTTP(badResult, badUpload)
	if badResult.Code != 422 {
		t.Fatalf("invalid object hash %d", badResult.Code)
	}
	if r = upload(task.ID, e.ID); r.Code != 201 {
		t.Fatalf("upload %d %s", r.Code, r.Body.String())
	}
	attachment := map[string]any{"id": "linux:attachment:1", "subjectType": "task", "subjectId": task.ID, "originalName": "a.txt", "sizeBytes": 3, "sha256": digest, "uploadedBy": e.ID, "active": true}
	body, _ := json.Marshal(map[string]any{"clientId": "linux", "operationId": "attachment-op", "action": "create", "attachment": attachment, "expected": nil})
	for i := 0; i < 2; i++ {
		if r = call("POST", "/api/v1/personal-attachment-sync?subjectType=task", "user", string(body)); r.Code != 202 {
			t.Fatalf("personal attachment %d %s", r.Code, r.Body.String())
		}
	}
	expected := make(map[string]any)
	for k, v := range attachment {
		expected[k] = v
	}
	expected["importedBy"] = 7
	archived := make(map[string]any)
	for k, v := range expected {
		archived[k] = v
	}
	archived["active"] = false
	archiveBody, _ := json.Marshal(map[string]any{"clientId": "linux", "operationId": "archive-op", "action": "archive", "attachment": archived, "expected": expected, "actorExternalId": e.ID})
	if r = call("POST", "/api/v1/personal-attachment-sync?subjectType=task", "other", string(archiveBody)); r.Code != 403 {
		t.Fatalf("foreign archive %d", r.Code)
	}
	forgedArchive, _ := json.Marshal(map[string]any{"clientId": "linux", "operationId": "archive-forged", "action": "archive", "attachment": archived, "expected": expected, "actorExternalId": "wrong"})
	if r = call("POST", "/api/v1/personal-attachment-sync?subjectType=task", "user", string(forgedArchive)); r.Code != 403 {
		t.Fatalf("forged archive actor %d %s", r.Code, r.Body.String())
	}
	for i := 0; i < 2; i++ {
		if r = call("POST", "/api/v1/personal-attachment-sync?subjectType=task", "user", string(archiveBody)); r.Code != 202 {
			t.Fatalf("personal archive %d %s", r.Code, r.Body.String())
		}
	}
	statesBody, _ := json.Marshal(map[string]any{"subjectType": "task", "subjectId": task.ID, "viewerEmployeeId": e.ID, "known": []any{expected}})
	if r = call("POST", "/api/v1/personal-attachment-states", "user", string(statesBody)); r.Code != 200 || !strings.Contains(r.Body.String(), `"active":false`) {
		t.Fatalf("known archive status %d %s", r.Code, r.Body.String())
	}
	if r = call("POST", "/api/v1/personal-attachment-states", "other", string(statesBody)); r.Code != 409 {
		t.Fatalf("wrong viewer status %d", r.Code)
	}
	incorrect := make(map[string]any)
	for k, v := range expected {
		incorrect[k] = v
	}
	incorrect["sha256"] = strings.Repeat("0", 64)
	wrongStates, _ := json.Marshal(map[string]any{"subjectType": "task", "subjectId": task.ID, "viewerEmployeeId": e.ID, "known": []any{incorrect}})
	if r = call("POST", "/api/v1/personal-attachment-states", "user", string(wrongStates)); r.Code != 409 {
		t.Fatalf("unknown metadata status %d", r.Code)
	}
	events, _, eventErr := store.eventsAfter(0)
	if eventErr != nil {
		t.Fatal(eventErr)
	}
	foundArchive := false
	for _, event := range events {
		if event.Topic == "attachment.archived" && strings.Contains(event.Payload, `"archiveActorId":7`) && strings.Contains(event.Payload, `"archiveActorExternalId":`) {
			foundArchive = true
		}
	}
	if !foundArchive {
		t.Fatal("personal archive actor missing from event history")
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r = call("POST", path, "user", op(e.ID)); r.Code != 202 {
		t.Fatalf("retry after restart %d %s", r.Code, r.Body.String())
	}
	if r = call("POST", "/api/v1/personal-attachment-sync?subjectType=task", "user", string(body)); r.Code != 202 {
		t.Fatalf("attachment retry after restart %d %s", r.Code, r.Body.String())
	}
	if r = call("POST", "/api/v1/personal-attachment-sync?subjectType=task", "user", string(archiveBody)); r.Code != 202 {
		t.Fatalf("archive retry after restart %d %s", r.Code, r.Body.String())
	}
}

func TestPersonalTicketCommentSyncAuthorAndVisibility(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tickets.manage","employees.manage"]},"user":{"employeeId":7,"permissions":["tickets.read","collaboration.comment"]},"other":{"employeeId":8,"permissions":["tickets.read","collaboration.comment"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	r := call("POST", "/api/v1/employees", "manager", `{"name":"Ada","role":"IT","department":"IT","presence":"office"}`)
	var e centralEmployee
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &e) != nil {
		t.Fatal(r.Body.String())
	}
	if r = call("PUT", "/api/v1/employee-account-link?id="+e.ID, "manager", `{"accountEmployeeId":7}`); r.Code != 200 {
		t.Fatal(r.Body.String())
	}
	r = call("POST", "/api/v1/tickets", "manager", `{"title":"A","category":"IT","priority":"Mittel","assigneeId":7}`)
	var ticket centralTicket
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &ticket) != nil {
		t.Fatal(r.Body.String())
	}
	op := func(author string) string {
		b, _ := json.Marshal(map[string]any{"clientId": "linux", "operationId": "op1", "comment": map[string]any{"id": "linux:comment:1", "subjectType": "ticket", "subjectId": ticket.ID, "authorId": author, "body": "Hallo", "createdAt": "2026-01-01T12:00:00Z"}})
		return string(b)
	}
	path := "/api/v1/personal-ticket-comment-sync"
	if r = call("POST", path, "other", op(e.ID)); r.Code != 403 {
		t.Fatalf("foreign actor %d %s", r.Code, r.Body.String())
	}
	if r = call("POST", path, "user", op("7")); r.Code != 403 {
		t.Fatalf("forged author %d %s", r.Code, r.Body.String())
	}
	r = call("POST", "/api/v1/tickets", "manager", `{"title":"B","category":"IT","priority":"Mittel","assigneeId":8}`)
	var hidden centralTicket
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &hidden) != nil {
		t.Fatal(r.Body.String())
	}
	if r = call("POST", path, "user", strings.Replace(op(e.ID), ticket.ID, hidden.ID, 1)); r.Code != 403 {
		t.Fatalf("inaccessible ticket %d %s", r.Code, r.Body.String())
	}
	if r = call("GET", path, "user", ""); r.Code != 405 {
		t.Fatalf("personal listing %d", r.Code)
	}
	for i := 0; i < 2; i++ {
		if r = call("POST", path, "user", op(e.ID)); r.Code != 202 {
			t.Fatalf("personal comment %d %s", r.Code, r.Body.String())
		}
	}
	hash := sha256.Sum256([]byte("abc"))
	digest := hex.EncodeToString(hash[:])
	upload := func(subject, viewer string) *httptest.ResponseRecorder {
		req := httptest.NewRequest("PUT", "/api/v1/personal-attachment-objects/"+digest+"?subjectType=ticket&subjectId="+subject+"&viewerEmployeeId="+viewer, strings.NewReader("abc"))
		req.Header.Set("Authorization", "Bearer user")
		req.Header.Set("X-Aura-SHA256", digest)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	if r = upload(hidden.ID, e.ID); r.Code != 403 {
		t.Fatalf("hidden upload %d", r.Code)
	}
	if r = upload(ticket.ID, "wrong"); r.Code != 403 {
		t.Fatalf("wrong uploader %d", r.Code)
	}
	badUpload := httptest.NewRequest("PUT", "/api/v1/personal-attachment-objects/"+digest+"?subjectType=ticket&subjectId="+ticket.ID+"&viewerEmployeeId="+e.ID, strings.NewReader("wrong"))
	badUpload.Header.Set("Authorization", "Bearer user")
	badUpload.Header.Set("X-Aura-SHA256", digest)
	badResult := httptest.NewRecorder()
	handler.ServeHTTP(badResult, badUpload)
	if badResult.Code != 422 {
		t.Fatalf("invalid object hash %d", badResult.Code)
	}
	if r = upload(ticket.ID, e.ID); r.Code != 201 {
		t.Fatalf("upload %d %s", r.Code, r.Body.String())
	}
	attachment := map[string]any{"id": "linux:attachment:1", "subjectType": "ticket", "subjectId": ticket.ID, "originalName": "a.txt", "sizeBytes": 3, "sha256": digest, "uploadedBy": e.ID, "active": true}
	body, _ := json.Marshal(map[string]any{"clientId": "linux", "operationId": "attachment-op", "action": "create", "attachment": attachment, "expected": nil})
	for i := 0; i < 2; i++ {
		if r = call("POST", "/api/v1/personal-attachment-sync?subjectType=ticket", "user", string(body)); r.Code != 202 {
			t.Fatalf("personal attachment %d %s", r.Code, r.Body.String())
		}
	}
	expected := make(map[string]any)
	for k, v := range attachment {
		expected[k] = v
	}
	expected["importedBy"] = 7
	archived := make(map[string]any)
	for k, v := range expected {
		archived[k] = v
	}
	archived["active"] = false
	archiveBody, _ := json.Marshal(map[string]any{"clientId": "linux", "operationId": "archive-op", "action": "archive", "attachment": archived, "expected": expected, "actorExternalId": e.ID})
	if r = call("POST", "/api/v1/personal-attachment-sync?subjectType=ticket", "other", string(archiveBody)); r.Code != 403 {
		t.Fatalf("foreign archive %d", r.Code)
	}
	forgedArchive, _ := json.Marshal(map[string]any{"clientId": "linux", "operationId": "archive-forged", "action": "archive", "attachment": archived, "expected": expected, "actorExternalId": "wrong"})
	if r = call("POST", "/api/v1/personal-attachment-sync?subjectType=ticket", "user", string(forgedArchive)); r.Code != 403 {
		t.Fatalf("forged archive actor %d %s", r.Code, r.Body.String())
	}
	for i := 0; i < 2; i++ {
		if r = call("POST", "/api/v1/personal-attachment-sync?subjectType=ticket", "user", string(archiveBody)); r.Code != 202 {
			t.Fatalf("personal archive %d %s", r.Code, r.Body.String())
		}
	}
	statesBody, _ := json.Marshal(map[string]any{"subjectType": "ticket", "subjectId": ticket.ID, "viewerEmployeeId": e.ID, "known": []any{expected}})
	if r = call("POST", "/api/v1/personal-attachment-states", "user", string(statesBody)); r.Code != 200 || !strings.Contains(r.Body.String(), `"active":false`) {
		t.Fatalf("known archive status %d %s", r.Code, r.Body.String())
	}
	if r = call("POST", "/api/v1/personal-attachment-states", "other", string(statesBody)); r.Code != 409 {
		t.Fatalf("wrong viewer status %d", r.Code)
	}
	incorrect := make(map[string]any)
	for k, v := range expected {
		incorrect[k] = v
	}
	incorrect["sha256"] = strings.Repeat("0", 64)
	wrongStates, _ := json.Marshal(map[string]any{"subjectType": "ticket", "subjectId": ticket.ID, "viewerEmployeeId": e.ID, "known": []any{incorrect}})
	if r = call("POST", "/api/v1/personal-attachment-states", "user", string(wrongStates)); r.Code != 409 {
		t.Fatalf("unknown metadata status %d", r.Code)
	}
	events, _, eventErr := store.eventsAfter(0)
	if eventErr != nil {
		t.Fatal(eventErr)
	}
	foundArchive := false
	for _, event := range events {
		if event.Topic == "attachment.archived" && strings.Contains(event.Payload, `"archiveActorId":7`) && strings.Contains(event.Payload, `"archiveActorExternalId":`) {
			foundArchive = true
		}
	}
	if !foundArchive {
		t.Fatal("personal archive actor missing from event history")
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r = call("POST", path, "user", op(e.ID)); r.Code != 202 {
		t.Fatalf("retry after restart %d %s", r.Code, r.Body.String())
	}
	if r = call("POST", "/api/v1/personal-attachment-sync?subjectType=ticket", "user", string(body)); r.Code != 202 {
		t.Fatalf("attachment retry after restart %d %s", r.Code, r.Body.String())
	}
	if r = call("POST", "/api/v1/personal-attachment-sync?subjectType=ticket", "user", string(archiveBody)); r.Code != 202 {
		t.Fatalf("archive retry after restart %d %s", r.Code, r.Body.String())
	}
}

func TestTaskCommentSyncDurabilityAndAuthorMapping(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage","employees.manage"]},"tasks":{"employeeId":7,"permissions":["tasks.manage"]},"employee":{"employeeId":8,"permissions":["employees.manage"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	r := call("POST", "/api/v1/tasks", "manager", `{"title":"A","department":"IT","priority":"Mittel","assigneeId":9}`)
	var task centralTask
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &task) != nil {
		t.Fatalf("task %d %s", r.Code, r.Body.String())
	}
	r = call("POST", "/api/v1/employees", "manager", `{"name":"Test","role":"IT","department":"IT","presence":"office"}`)
	var employee centralEmployee
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &employee) != nil {
		t.Fatalf("employee %d %s", r.Code, r.Body.String())
	}
	op := func(id, subject, author, body string) string {
		return fmt.Sprintf(`{"clientId":"linux","operationId":%q,"comment":{"id":"linux-comment-1","subjectType":"task","subjectId":%q,"authorId":%q,"body":%q,"createdAt":"2026-01-01T12:34:56Z"}}`, id, subject, author, body)
	}
	add := op("one", task.ID, "7", "Hallo")
	for _, token := range []string{"tasks", "employee"} {
		for _, method := range []string{"GET", "POST"} {
			if r := call(method, "/api/v1/task-comment-sync", token, add); r.Code != 403 {
				t.Fatalf("permissions %s %s: %d", method, token, r.Code)
			}
		}
	}
	if r := call("POST", "/api/v1/task-comment-sync", "manager", add); r.Code != 409 {
		t.Fatalf("unmapped author %d", r.Code)
	}
	if r := call("PUT", "/api/v1/employee-account-link?id="+employee.ID, "manager", `{"accountEmployeeId":7}`); r.Code != 200 {
		t.Fatalf("map %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", "/api/v1/task-comment-sync", "manager", op("missing", "missing", "7", "Hallo")); r.Code != 409 {
		t.Fatalf("missing task %d", r.Code)
	}
	for i := 0; i < 2; i++ {
		if r := call("POST", "/api/v1/task-comment-sync", "manager", add); r.Code != 202 {
			t.Fatalf("add %d %s", r.Code, r.Body.String())
		}
	}
	if r := call("POST", "/api/v1/task-comment-sync", "manager", op("one", task.ID, "7", "Changed")); r.Code != 409 {
		t.Fatalf("reuse %d", r.Code)
	}
	if r := call("POST", "/api/v1/task-comment-sync", "manager", op("different", task.ID, "7", "Hallo")); r.Code != 409 {
		t.Fatalf("duplicate ID %d", r.Code)
	}
	forged := strings.Replace(op("forged", task.ID, "7", "Hallo"), `"createdAt":`, `"importedBy":7,"createdAt":`, 1)
	if r := call("POST", "/api/v1/task-comment-sync", "manager", forged); r.Code != 400 {
		t.Fatalf("forged %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := call("POST", "/api/v1/task-comment-sync", "manager", add); r.Code != 202 {
		t.Fatalf("restart retry %d", r.Code)
	}
	r = call("GET", "/api/v1/task-comment-sync", "manager", "")
	var comments []centralComment
	if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &comments) != nil || len(comments) != 1 || comments[0].AuthorID != "7" || comments[0].ImportedBy != 9 || comments[0].Body != "Hallo" || comments[0].CreatedAt != "2026-01-01T12:34:56Z" {
		t.Fatalf("replay %d %s", r.Code, r.Body.String())
	}
	external := strings.Replace(op("external", task.ID, employee.ID, "Externer Autor"), "linux-comment-1", "linux-comment-2", 1)
	if r := call("POST", "/api/v1/task-comment-sync", "manager", external); r.Code != 202 {
		t.Fatalf("external author %d %s", r.Code, r.Body.String())
	}
	missing := strings.Replace(op("missing-author", task.ID, "missing-employee", "Unbekannt"), "linux-comment-1", "linux-comment-3", 1)
	if r := call("POST", "/api/v1/task-comment-sync", "manager", missing); r.Code != 409 {
		t.Fatalf("unknown external author %d", r.Code)
	}
}

func TestTicketSyncPermissionsConflictAndReplay(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tickets.manage"]},"reader":{"employeeId":7,"permissions":["tickets.read"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, "/api/v1/ticket-sync", strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	create := `{"clientId":"linux","operationId":"one","action":"create","ticket":{"id":"t1","title":"Test","category":"IT","priority":"Mittel","status":"open","assigneeId":9,"createdBy":9,"active":true},"expected":null}`
	if r := call("GET", "reader", ""); r.Code != 403 {
		t.Fatalf("read permission %d", r.Code)
	}
	if r := call("POST", "reader", create); r.Code != 403 {
		t.Fatalf("write permission %d", r.Code)
	}
	forged := strings.Replace(create, `"createdBy":9`, `"createdBy":7`, 1)
	if r := call("POST", "manager", forged); r.Code != 403 {
		t.Fatalf("creator %d", r.Code)
	}
	for i := 0; i < 2; i++ {
		if r := call("POST", "manager", create); r.Code != 202 {
			t.Fatalf("create %d %s", r.Code, r.Body.String())
		}
	}
	if r := call("POST", "manager", strings.Replace(create, "Test", "Other", 1)); r.Code != 409 {
		t.Fatalf("reuse %d", r.Code)
	}
	stale := strings.Replace(create, `"operationId":"one"`, `"operationId":"other"`, 1)
	if r := call("POST", "manager", stale); r.Code != 409 {
		t.Fatalf("duplicate %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := call("POST", "manager", create); r.Code != 202 {
		t.Fatalf("restart retry %d", r.Code)
	}
	r := call("GET", "manager", "")
	var tickets []centralTicket
	if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &tickets) != nil || len(tickets) != 1 || !tickets[0].Active || tickets[0].Title != "Test" {
		t.Fatalf("replay %d %s", r.Code, r.Body.String())
	}
}

func TestTicketCommentSyncDurabilityAndAuthorMapping(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tickets.manage","employees.manage"]},"tickets":{"employeeId":7,"permissions":["tickets.manage"]},"employee":{"employeeId":8,"permissions":["employees.manage"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	r := call("POST", "/api/v1/tickets", "manager", `{"title":"A","category":"IT","priority":"Mittel","assigneeId":9}`)
	var ticket centralTicket
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &ticket) != nil {
		t.Fatalf("ticket %d %s", r.Code, r.Body.String())
	}
	r = call("POST", "/api/v1/employees", "manager", `{"name":"Test","role":"IT","department":"IT","presence":"office"}`)
	var employee centralEmployee
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &employee) != nil {
		t.Fatalf("employee %d %s", r.Code, r.Body.String())
	}
	op := func(id, subject, author, body string) string {
		return fmt.Sprintf(`{"clientId":"linux","operationId":%q,"comment":{"id":"linux-comment-1","subjectType":"ticket","subjectId":%q,"authorId":%q,"body":%q,"createdAt":"2026-01-01T12:34:56Z"}}`, id, subject, author, body)
	}
	add := op("one", ticket.ID, "7", "Hallo")
	for _, token := range []string{"tickets", "employee"} {
		for _, method := range []string{"GET", "POST"} {
			if r := call(method, "/api/v1/ticket-comment-sync", token, add); r.Code != 403 {
				t.Fatalf("permissions %s %s: %d", method, token, r.Code)
			}
		}
	}
	if r := call("POST", "/api/v1/ticket-comment-sync", "manager", add); r.Code != 409 {
		t.Fatalf("unmapped author %d", r.Code)
	}
	if r := call("PUT", "/api/v1/employee-account-link?id="+employee.ID, "manager", `{"accountEmployeeId":7}`); r.Code != 200 {
		t.Fatalf("map %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", "/api/v1/ticket-comment-sync", "manager", op("missing", "missing", "7", "Hallo")); r.Code != 409 {
		t.Fatalf("missing ticket %d", r.Code)
	}
	for i := 0; i < 2; i++ {
		if r := call("POST", "/api/v1/ticket-comment-sync", "manager", add); r.Code != 202 {
			t.Fatalf("add %d %s", r.Code, r.Body.String())
		}
	}
	if r := call("POST", "/api/v1/ticket-comment-sync", "manager", op("one", ticket.ID, "7", "Changed")); r.Code != 409 {
		t.Fatalf("reuse %d", r.Code)
	}
	if r := call("POST", "/api/v1/ticket-comment-sync", "manager", op("different", ticket.ID, "7", "Hallo")); r.Code != 409 {
		t.Fatalf("duplicate ID %d", r.Code)
	}
	forged := strings.Replace(op("forged", ticket.ID, "7", "Hallo"), `"createdAt":`, `"importedBy":7,"createdAt":`, 1)
	if r := call("POST", "/api/v1/ticket-comment-sync", "manager", forged); r.Code != 400 {
		t.Fatalf("forged %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := call("POST", "/api/v1/ticket-comment-sync", "manager", add); r.Code != 202 {
		t.Fatalf("restart retry %d", r.Code)
	}
	r = call("GET", "/api/v1/ticket-comment-sync", "manager", "")
	var comments []centralComment
	if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &comments) != nil || len(comments) != 1 || comments[0].AuthorID != "7" || comments[0].ImportedBy != 9 || comments[0].Body != "Hallo" || comments[0].CreatedAt != "2026-01-01T12:34:56Z" {
		t.Fatalf("replay %d %s", r.Code, r.Body.String())
	}
	external := strings.Replace(op("external", ticket.ID, employee.ID, "Externer Autor"), "linux-comment-1", "linux-comment-2", 1)
	if r := call("POST", "/api/v1/ticket-comment-sync", "manager", external); r.Code != 202 {
		t.Fatalf("external author %d %s", r.Code, r.Body.String())
	}
	missing := strings.Replace(op("missing-author", ticket.ID, "missing-employee", "Unbekannt"), "linux-comment-1", "linux-comment-3", 1)
	if r := call("POST", "/api/v1/ticket-comment-sync", "manager", missing); r.Code != 409 {
		t.Fatalf("unknown external author %d", r.Code)
	}
}

func TestAttachmentSyncVerifiedObjectDurabilityAndConflict(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage","employees.manage"]},"tasks":{"employeeId":7,"permissions":["tasks.manage"]},"employee":{"employeeId":8,"permissions":["employees.manage"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	r := call("POST", "/api/v1/tasks", "manager", `{"title":"A","department":"IT","priority":"Mittel","assigneeId":9}`)
	var task centralTask
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &task) != nil {
		t.Fatalf("task %d %s", r.Code, r.Body.String())
	}
	r = call("POST", "/api/v1/employees", "manager", `{"name":"Test","role":"IT","department":"IT","presence":"office"}`)
	var employee centralEmployee
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &employee) != nil {
		t.Fatalf("employee %d %s", r.Code, r.Body.String())
	}
	if r := call("PUT", "/api/v1/employee-account-link?id="+employee.ID, "manager", `{"accountEmployeeId":7}`); r.Code != 200 {
		t.Fatalf("link %d %s", r.Code, r.Body.String())
	}
	content := "Anhang Inhalt"
	hash := sha256.Sum256([]byte(content))
	digest := hex.EncodeToString(hash[:])
	endpoint := "/api/v1/attachment-sync?subjectType=task"
	attachment := centralAttachment{ID: "local:attachment:1", SubjectType: "task", SubjectID: task.ID, OriginalName: "bericht.txt", SizeBytes: int64(len(content)), SHA256: digest, UploadedBy: employee.ID, Active: true}
	operation := func(id, action string, a centralAttachment, expected *centralAttachment) string {
		b, _ := json.Marshal(map[string]any{"clientId": "linux", "operationId": id, "action": action, "attachment": a, "expected": expected})
		return string(b)
	}
	create := operation("one", "create", attachment, nil)
	for _, token := range []string{"tasks", "employee"} {
		for _, method := range []string{"GET", "POST"} {
			if r := call(method, endpoint, token, create); r.Code != 403 {
				t.Fatalf("permission %s %s: %d", token, method, r.Code)
			}
		}
	}
	if r := call("POST", endpoint, "manager", create); r.Code != 409 {
		t.Fatalf("missing object %d", r.Code)
	}
	upload := httptest.NewRequest("PUT", "/api/v1/attachments/"+digest, strings.NewReader(content))
	upload.Header.Set("Authorization", "Bearer manager")
	upload.Header.Set("X-Aura-SHA256", digest)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, upload)
	if response.Code != 201 {
		t.Fatalf("upload %d %s", response.Code, response.Body.String())
	}
	object := filepath.Join(journal+".attachments", digest)
	if err := os.WriteFile(object, []byte(strings.Repeat("x", len(content))), 0600); err != nil {
		t.Fatal(err)
	}
	if r := call("POST", endpoint, "manager", create); r.Code != 409 {
		t.Fatalf("corrupt object %d", r.Code)
	}
	if err := os.WriteFile(object, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if r := call("POST", endpoint, "manager", create); r.Code != 202 {
			t.Fatalf("create/retry %d %s", r.Code, r.Body.String())
		}
	}
	changed := attachment
	changed.OriginalName = "anders.txt"
	if r := call("POST", endpoint, "manager", operation("one", "create", changed, nil)); r.Code != 409 {
		t.Fatalf("reuse %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := call("POST", endpoint, "manager", create); r.Code != 202 {
		t.Fatalf("restart retry %d", r.Code)
	}
	r = call("GET", endpoint, "manager", "")
	var values []centralAttachment
	if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &values) != nil || len(values) != 1 || values[0].ImportedBy != 9 || values[0].UploadedBy != employee.ID {
		t.Fatalf("replay %d %s", r.Code, r.Body.String())
	}
	current := values[0]
	archived := current
	archived.Active = false
	bad := archived
	bad.SHA256 = strings.Repeat("0", 64)
	if r := call("POST", endpoint, "manager", operation("bad", "archive", bad, &current)); r.Code != 409 {
		t.Fatalf("metadata change %d", r.Code)
	}
	// The legacy ingestion path must not let a writer alter metadata of an imported attachment.
	forged := attachment
	forged.OriginalName = "gefälscht.txt"
	payload, _ := json.Marshal(forged)
	event, _ := json.Marshal(syncEvent{Topic: "attachment.created", Payload: string(payload)})
	if r := call("POST", "/events", "manager", string(event)); r.Code != 409 {
		t.Fatalf("legacy overwrite %d %s", r.Code, r.Body.String())
	}
	archive := operation("two", "archive", archived, &current)
	if r := call("POST", endpoint, "manager", archive); r.Code != 202 {
		t.Fatalf("archive %d %s", r.Code, r.Body.String())
	}
	r = call("GET", endpoint, "manager", "")
	values = nil
	if json.Unmarshal(r.Body.Bytes(), &values) != nil || len(values) != 1 || values[0].Active || values[0].ImportedBy != 9 {
		t.Fatalf("archived %s", r.Body.String())
	}
	restore := operation("restore", "restore", current, &archived)
	forgedRestore, _ := json.Marshal(syncEvent{Topic: "attachment.restored", Payload: string(payload)})
	if r := call("POST", "/events", "manager", string(forgedRestore)); r.Code != 403 {
		t.Fatalf("forged restore %d", r.Code)
	}
	if err := os.WriteFile(object, []byte(strings.Repeat("x", len(content))), 0600); err != nil {
		t.Fatal(err)
	}
	if r := call("POST", endpoint, "manager", restore); r.Code != 409 {
		t.Fatalf("corrupt restore %d", r.Code)
	}
	if err := os.WriteFile(object, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if r := call("POST", endpoint, "manager", restore); r.Code != 202 {
			t.Fatalf("restore/retry %d %s", r.Code, r.Body.String())
		}
	}
	if r := call("POST", endpoint, "manager", operation("stale-restore", "restore", current, &archived)); r.Code != 409 {
		t.Fatalf("stale restore %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	r = call("GET", endpoint, "manager", "")
	values = nil
	if json.Unmarshal(r.Body.Bytes(), &values) != nil || len(values) != 1 || !values[0].Active {
		t.Fatalf("restore replay %s", r.Body.String())
	}
}

func TestOrganizationSyncPermissionsIdentityAndRestart(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["employees.manage"]},"reader":{"employeeId":7,"permissions":["employees.read"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, "/api/v1/organization-sync", strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	create := `{"clientId":"linux","operationId":"one","action":"create","organization":{"id":"org1","name":"Entwicklung","code":"DEV","managerId":0,"active":true},"expected":null}`
	for _, method := range []string{"GET", "POST"} {
		if r := call(method, "reader", create); r.Code != 403 {
			t.Fatalf("permission %s %d", method, r.Code)
		}
	}
	for i := 0; i < 2; i++ {
		if r := call("POST", "manager", create); r.Code != 202 {
			t.Fatalf("create %d %s", r.Code, r.Body.String())
		}
	}
	if r := call("POST", "manager", strings.Replace(create, "Entwicklung", "Anders", 1)); r.Code != 409 {
		t.Fatalf("reuse %d", r.Code)
	}
	duplicate := strings.Replace(strings.Replace(strings.Replace(create, `"one"`, `"two"`, 1), `"org1"`, `"org2"`, 1), `"Entwicklung"`, `"Anders"`, 1)
	if r := call("POST", "manager", duplicate); r.Code != 409 {
		t.Fatalf("duplicate code %d", r.Code)
	}
	duplicate = strings.Replace(strings.Replace(strings.Replace(create, `"one"`, `"three"`, 1), `"org1"`, `"org3"`, 1), `"DEV"`, `"OTHER"`, 1)
	if r := call("POST", "manager", duplicate); r.Code != 409 {
		t.Fatalf("duplicate name %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := call("POST", "manager", create); r.Code != 202 {
		t.Fatalf("restart retry %d", r.Code)
	}
	r := call("GET", "manager", "")
	var values []centralOrganization
	if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &values) != nil || len(values) != 1 || values[0].ManagerID != 0 || values[0].Code != "DEV" {
		t.Fatalf("replay %d %s", r.Code, r.Body.String())
	}
}

func TestFacilityAssetSyncPermissionsIdentityAndRestart(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["facility.manage"]},"reader":{"employeeId":7,"permissions":["facility.read"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, "/api/v1/facility-asset-sync", strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	create := `{"clientId":"linux","operationId":"one","action":"create","facilityAsset":{"id":"org1","name":"Entwicklung","category":"Technik","location":"EG","status":"operational","active":true},"expected":null}`
	for _, method := range []string{"GET", "POST"} {
		if r := call(method, "reader", create); r.Code != 403 {
			t.Fatalf("permission %s %d", method, r.Code)
		}
	}
	for i := 0; i < 2; i++ {
		if r := call("POST", "manager", create); r.Code != 202 {
			t.Fatalf("create %d %s", r.Code, r.Body.String())
		}
	}
	if r := call("POST", "manager", strings.Replace(create, "Entwicklung", "Anders", 1)); r.Code != 409 {
		t.Fatalf("reuse %d", r.Code)
	}
	duplicate := strings.Replace(strings.Replace(create, `"one"`, `"two"`, 1), `"org1"`, `"org2"`, 1)
	if r := call("POST", "manager", duplicate); r.Code != 409 {
		t.Fatalf("duplicate name %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := call("POST", "manager", create); r.Code != 202 {
		t.Fatalf("restart retry %d", r.Code)
	}
	r := call("GET", "manager", "")
	var values []centralFacilityAsset
	if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &values) != nil || len(values) != 1 || values[0].Status != "operational" || values[0].Category != "Technik" {
		t.Fatalf("replay %d %s", r.Code, r.Body.String())
	}
}

func TestMaintenanceSyncReceiptAndNoDuplicateRecurrence(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["facility.manage"]},"reader":{"employeeId":7,"permissions":["facility.read"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	asset := `{"clientId":"linux","operationId":"asset","action":"create","facilityAsset":{"id":"a1","name":"Anlage","category":"IT","location":"EG","status":"operational","active":true},"expected":null}`
	if r := call("POST", "/api/v1/facility-asset-sync", "manager", asset); r.Code != 202 {
		t.Fatalf("asset %d %s", r.Code, r.Body.String())
	}
	item := centralMaintenanceOrder{ID: "m1", AssetID: "a1", Title: "Wartung", Status: "open", DueAt: 1900000000, RecurrenceDays: 7, Active: true}
	op := func(id, action string, item centralMaintenanceOrder, expected *centralMaintenanceOrder) string {
		v, _ := json.Marshal(map[string]any{"clientId": "linux", "operationId": id, "action": action, "maintenanceOrder": item, "expected": expected})
		return string(v)
	}
	create := op("one", "create", item, nil)
	endpoint := "/api/v1/maintenance-sync"
	for _, method := range []string{"GET", "POST"} {
		if r := call(method, endpoint, "reader", create); r.Code != 403 {
			t.Fatalf("permission %s %d", method, r.Code)
		}
	}
	missing := item
	missing.AssetID = "missing"
	if r := call("POST", endpoint, "manager", op("missing", "create", missing, nil)); r.Code != 409 {
		t.Fatalf("missing parent %d", r.Code)
	}
	for i := 0; i < 2; i++ {
		if r := call("POST", endpoint, "manager", create); r.Code != 202 {
			t.Fatalf("create %d %s", r.Code, r.Body.String())
		}
	}
	done := item
	done.Status = "completed"
	update := op("two", "update", done, &item)
	if r := call("POST", endpoint, "manager", update); r.Code != 202 {
		t.Fatalf("complete %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", endpoint, "manager", op("stale", "update", done, &item)); r.Code != 409 {
		t.Fatalf("stale %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := call("POST", endpoint, "manager", update); r.Code != 202 {
		t.Fatalf("retry after restart %d", r.Code)
	}
	r := call("GET", endpoint, "manager", "")
	var all []centralMaintenanceOrder
	if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &all) != nil || len(all) != 1 || all[0].Status != "completed" {
		t.Fatalf("unexpected successor or replay %d %s", r.Code, r.Body.String())
	}
}

func TestMaintenanceReservationProtectsSuccessorAfterRestart(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["facility.manage"]},"other":{"employeeId":8,"permissions":["facility.manage"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token string, body any) *httptest.ResponseRecorder {
		data, _ := json.Marshal(body)
		req := httptest.NewRequest(method, path, bytes.NewReader(data))
		req.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	check := func(r *httptest.ResponseRecorder, want int) {
		if r.Code != want {
			t.Fatalf("got %d, want %d: %s", r.Code, want, r.Body.String())
		}
	}
	check(call("POST", "/api/v1/facility-asset-sync", "manager", map[string]any{"clientId": "linux", "operationId": "asset", "action": "create", "facilityAsset": map[string]any{"id": "a1", "name": "Anlage", "category": "IT", "location": "EG", "status": "operational", "active": true}, "expected": nil}), 202)
	source := centralMaintenanceOrder{ID: "m1", AssetID: "a1", Title: "Wartung", Status: "open", DueAt: 1900000000, RecurrenceDays: 7, Active: true}
	op := func(client, id, action string, item centralMaintenanceOrder, expected *centralMaintenanceOrder, successor string) map[string]any {
		v := map[string]any{"clientId": client, "operationId": id, "action": action, "maintenanceOrder": item, "expected": expected}
		if successor != "" {
			v["recurrenceSuccessorId"] = successor
		}
		return v
	}
	endpoint := "/api/v1/maintenance-sync"
	check(call("POST", endpoint, "manager", op("linux", "create", "create", source, nil, "")), 202)
	done := source
	done.Status = "completed"
	child := source
	child.ID = "linux:maintenance_order:2"
	child.DueAt += 7 * 86400
	completion := op("linux", "complete", "update", done, &source, child.ID)
	check(call("POST", endpoint, "manager", completion), 202)
	check(call("POST", endpoint, "other", op("linux", "stolen", "create", child, nil, "")), 409)
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	check(call("POST", endpoint, "manager", completion), 202)
	check(call("POST", endpoint, "other", op("linux", "stolen2", "create", child, nil, "")), 409)
	path := "/api/v1/maintenance?assetId=a1&id=m1"
	check(call("PATCH", path, "manager", map[string]string{"status": "open"}), 200)
	check(call("PATCH", path, "manager", map[string]string{"status": "completed"}), 409)
	check(call("POST", endpoint, "other", op("other", "duplicate", "update", done, &source, "")), 409)
	changedAsset := source
	changedAsset.AssetID = "another-asset"
	data, _ := json.Marshal(changedAsset)
	check(call("POST", "/events", "manager", syncEvent{Topic: "facility.maintenance.updated", Payload: string(data)}), 409)
	data, _ = json.Marshal(child)
	check(call("POST", "/events", "manager", syncEvent{Topic: "facility.maintenance.created", Payload: string(data)}), 409)
	check(call("POST", endpoint, "manager", op("linux", "child", "create", child, nil, "")), 202)
	check(call("PATCH", path, "manager", map[string]string{"status": "completed"}), 200)
	r := call("GET", endpoint, "manager", nil)
	var all []centralMaintenanceOrder
	if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &all) != nil || len(all) != 2 {
		t.Fatalf("orders: %d %s", r.Code, r.Body.String())
	}
}

func TestApprovalSyncIdentityVisibilityAndDurability(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"requester":{"employeeId":9,"permissions":["tasks.manage"]},"approver":{"employeeId":8,"permissions":["tasks.read"]},"other":{"employeeId":7,"permissions":["tasks.read"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	r := call("POST", "/api/v1/tasks", "requester", `{"title":"Freigabe","department":"IT","priority":"Mittel","assigneeId":9}`)
	var task centralTask
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &task) != nil {
		t.Fatalf("task %d %s", r.Code, r.Body.String())
	}
	pending := centralTaskApproval{ID: "approval1", TaskID: task.ID, RequestedBy: "9", ApproverID: "8", Status: "pending", Active: true}
	op := func(id, action string, a centralTaskApproval, expected *centralTaskApproval) string {
		b, _ := json.Marshal(map[string]any{"clientId": "linux", "operationId": id, "action": action, "approval": a, "expected": expected})
		return string(b)
	}
	request := op("one", "request", pending, nil)
	path := "/api/v1/approval-sync"
	if r := call("POST", path, "other", request); r.Code != 403 {
		t.Fatalf("forged request %d", r.Code)
	}
	for i := 0; i < 2; i++ {
		if r := call("POST", path, "requester", request); r.Code != 202 {
			t.Fatalf("request %d %s", r.Code, r.Body.String())
		}
	}
	if r := call("GET", path, "other", ""); r.Code != 200 || r.Body.String() != "[]\n" {
		t.Fatalf("privacy %d %s", r.Code, r.Body.String())
	}
	approved := pending
	approved.Status = "approved"
	approved.DecidedBy = "8"
	decision := op("two", "decide", approved, &pending)
	if r := call("POST", path, "requester", decision); r.Code != 403 {
		t.Fatalf("forged decision %d", r.Code)
	}
	if r := call("POST", path, "approver", decision); r.Code != 202 {
		t.Fatalf("decision %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", path, "approver", op("stale", "decide", approved, &pending)); r.Code != 409 {
		t.Fatalf("stale decision %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r := call("POST", path, "approver", decision); r.Code != 202 {
		t.Fatalf("restart retry %d", r.Code)
	}
	for _, token := range []string{"requester", "approver"} {
		r := call("GET", path, token, "")
		var values []centralTaskApproval
		if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &values) != nil || len(values) != 1 || values[0].Status != "approved" || values[0].DecidedBy != "8" {
			t.Fatalf("snapshot %s %d %s", token, r.Code, r.Body.String())
		}
	}
}

func TestDesktopHistoricApprovalAccountMapping(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage","employees.manage"]},"approver":{"employeeId":8,"permissions":["tasks.read"]}}`)
	dir := t.TempDir()
	journal := filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(filepath.Join(dir, "central.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	server := httptest.NewServer(handler)
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "manager")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	taskResponse := call("POST", "/api/v1/tasks", "manager", `{"title":"Old approval","department":"IT","priority":"Mittel","assigneeId":9}`)
	var task centralTask
	if taskResponse.Code != 201 || json.Unmarshal(taskResponse.Body.Bytes(), &task) != nil {
		t.Fatal(taskResponse.Body.String())
	}
	approval := centralTaskApproval{ID: "historic-approval", TaskID: task.ID, RequestedBy: "9", ApproverID: "8", Status: "pending", Active: true}
	data, _ := json.Marshal(map[string]any{"clientId": "old-linux", "operationId": "one", "action": "request", "approval": approval})
	if r := call("POST", "/api/v1/approval-sync", "manager", string(data)); r.Code != 202 {
		t.Fatalf("request: %d %s", r.Code, r.Body.String())
	}
	childResponse := call("POST", "/api/v1/task-subtasks?taskId="+task.ID, "manager", `{"title":"Teilaufgabe","status":"open","assigneeId":8,"dueAt":123}`)
	if childResponse.Code != 201 {
		t.Fatalf("subtask: %d %s", childResponse.Code, childResponse.Body.String())
	}
	desktop := "/api/v1/sync/changes?desktop=1&cursor=0"
	if _, cursor, code := fetchChanges(0); code == 0 || cursor != 0 {
		t.Fatal("native client advanced without account mapping")
	}
	if r := call("GET", desktop, "manager", ""); r.Code != 409 {
		t.Fatalf("missing mapping: %d %s", r.Code, r.Body.String())
	}
	ids := map[int]string{}
	for _, id := range []int{9, 8} {
		body := fmt.Sprintf(`{"name":"Employee %d","role":"Team","department":"IT","presence":"office"}`, id)
		r := call("POST", "/api/v1/employees", "manager", body)
		var employee centralEmployee
		if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &employee) != nil {
			t.Fatalf("employee: %d %s", r.Code, r.Body.String())
		}
		ids[id] = employee.ID
		r = call("PUT", "/api/v1/employee-account-link?id="+employee.ID, "manager", fmt.Sprintf(`{"accountEmployeeId":%d}`, id))
		if r.Code != 200 {
			t.Fatalf("link: %d %s", r.Code, r.Body.String())
		}
	}
	if actor, account, code := desktopSubtaskContext(""); code != 0 || actor != 9 || account != 0 {
		t.Fatalf("empty context %d %d %d", actor, account, code)
	}
	if actor, account, code := desktopSubtaskContext(ids[8]); code != 0 || actor != 9 || account != 8 {
		t.Fatalf("mapped context %d %d %d", actor, account, code)
	}
	if _, _, code := desktopSubtaskContext("unknown"); code != 9 {
		t.Fatalf("missing context %d", code)
	}
	if r := call("GET", "/api/v1/desktop-subtask-context?employee="+ids[8], "approver", ""); r.Code != 403 {
		t.Fatalf("context permissions %d", r.Code)
	}
	desktopSub := centralSubtask{ID: "windows:subtask:1", TaskID: task.ID, Title: "Windows", Status: "open", AssigneeID: 8, Active: true}
	wire := func(op string, sub centralSubtask, expected *centralSubtask, key string) string {
		value := map[string]any{"clientId": "windows", "operationId": op, "subtask": sub, "expected": expected, "assigneeEmployeeId": key}
		if expected == nil {
			value["action"] = "create"
		} else {
			value["action"] = "update"
			value["expectedAssigneeEmployeeId"] = key
		}
		b, _ := json.Marshal(value)
		return string(b)
	}
	createBody := wire("desktop-1", desktopSub, nil, ids[8])
	if r := call("POST", "/api/v1/desktop-subtask-sync", "approver", createBody); r.Code != 403 {
		t.Fatalf("reader write: %d", r.Code)
	}
	if code := sendDesktopSubtask(createBody); code != 0 {
		t.Fatalf("desktop transport create: %d", code)
	}
	if r := call("POST", "/api/v1/desktop-subtask-sync", "manager", createBody); r.Code != 202 {
		t.Fatalf("desktop retry: %d", r.Code)
	}
	updated := desktopSub
	updated.Title = "Windows geändert"
	key := ids[8]
	updateBody := wire("desktop-2", updated, &desktopSub, key)
	if r := call("POST", "/api/v1/desktop-subtask-sync", "manager", updateBody); r.Code != 202 {
		t.Fatalf("desktop update: %d %s", r.Code, r.Body.String())
	}
	stale := updated
	stale.Title = "Veraltet"
	if code := sendDesktopSubtask(wire("desktop-3", stale, &desktopSub, key)); code != 9 {
		t.Fatalf("stale transport update: %d", code)
	}
	if r := call("PUT", "/api/v1/employee-account-link?id="+ids[8], "manager", `{"accountEmployeeId":0}`); r.Code != 200 {
		t.Fatalf("unlink: %d", r.Code)
	}
	if _, _, code := desktopSubtaskContext(ids[8]); code != 9 {
		t.Fatalf("unlinked context %d", code)
	}
	if r := call("POST", "/api/v1/desktop-subtask-sync", "manager", updateBody); r.Code != 202 {
		t.Fatalf("accepted retry after unlink: %d", r.Code)
	}
	if r := call("POST", "/api/v1/desktop-subtask-sync", "manager", wire("desktop-4", stale, &updated, key)); r.Code != 409 {
		t.Fatalf("unmapped write: %d", r.Code)
	}
	if r := call("PUT", "/api/v1/employee-account-link?id="+ids[8], "manager", `{"accountEmployeeId":8}`); r.Code != 200 {
		t.Fatalf("relink: %d", r.Code)
	}
	beforeAuthors, err := store.latestCursor()
	if err != nil {
		t.Fatal(err)
	}
	commentPayload, _ := json.Marshal(centralComment{ID: "central-comment", SubjectType: "task", SubjectID: task.ID, AuthorID: "9", Body: "Test"})
	attachmentPayload, _ := json.Marshal(centralAttachment{ID: "central-attachment", SubjectType: "task", SubjectID: task.ID, OriginalName: "a.txt", SHA256: strings.Repeat("a", 64), UploadedBy: "8", Active: true})
	if err := persistEvent(journal, syncEvent{Topic: "comment.created", Payload: string(commentPayload), At: "2024-02-03T12:34:56Z"}); err != nil {
		t.Fatal(err)
	}
	if err := persistEvent(journal, syncEvent{Topic: "attachment.created", Payload: string(attachmentPayload)}); err != nil {
		t.Fatal(err)
	}
	if r := call("PUT", "/api/v1/employee-account-link?id="+ids[8], "manager", `{"accountEmployeeId":0}`); r.Code != 200 {
		t.Fatalf("unlink attachment author: %d", r.Code)
	}
	if r := call("GET", fmt.Sprintf("/api/v1/sync/changes?desktop=1&cursor=%d", beforeAuthors), "manager", ""); r.Code != 409 {
		t.Fatalf("missing attachment author mapping: %d", r.Code)
	}
	if r := call("PUT", "/api/v1/employee-account-link?id="+ids[8], "manager", `{"accountEmployeeId":8}`); r.Code != 200 {
		t.Fatalf("relink attachment author: %d", r.Code)
	}
	r := call("GET", desktop, "manager", "")
	var feed struct {
		Events     []syncEvent `json:"events"`
		NextCursor int         `json:"nextCursor"`
	}
	if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &feed) != nil {
		t.Fatalf("feed: %d %s", r.Code, r.Body.String())
	}
	known := map[string]bool{}
	found := false
	foundSubtask := false
	foundComment, foundAttachment := false, false
	for _, event := range feed.Events {
		if strings.HasPrefix(event.Topic, "employee.") {
			var e centralEmployee
			if json.Unmarshal([]byte(event.Payload), &e) == nil {
				known[e.ID] = true
			}
		}
		if event.Topic == "central.task_subtask.saved" {
			var child struct {
				centralSubtask
				AssigneeEmployeeID string `json:"assigneeEmployeeId"`
			}
			if json.Unmarshal([]byte(event.Payload), &child) != nil || child.AssigneeID != 8 || child.AssigneeEmployeeID != ids[8] || !known[child.AssigneeEmployeeID] {
				t.Fatalf("subtask mapping/order: %s", event.Payload)
			}
			foundSubtask = true
		}
		if event.Topic == "comment.created" {
			var c centralComment
			if json.Unmarshal([]byte(event.Payload), &c) != nil || c.AuthorID != ids[9] || !known[c.AuthorID] || c.CreatedAt != "2024-02-03T12:34:56Z" {
				t.Fatalf("comment mapping: %s", event.Payload)
			}
			foundComment = true
		}
		if event.Topic == "attachment.created" {
			var a centralAttachment
			if json.Unmarshal([]byte(event.Payload), &a) != nil || a.UploadedBy != ids[8] || !known[a.UploadedBy] {
				t.Fatalf("attachment mapping: %s", event.Payload)
			}
			foundAttachment = true
		}
		if event.Topic == "task.approval.requested" {
			var a centralTaskApproval
			if json.Unmarshal([]byte(event.Payload), &a) != nil {
				t.Fatal(event.Payload)
			}
			if a.ID == approval.ID {
				found = true
				if a.RequestedBy != ids[9] || a.ApproverID != ids[8] || !known[a.RequestedBy] || !known[a.ApproverID] {
					t.Fatalf("translation or order: %+v", a)
				}
			}
		}
	}
	if !found || !foundSubtask || !foundComment || !foundAttachment || feed.NextCursor <= 0 {
		t.Fatal("approval missing")
	}
	nativeEvents, nativeCursor, code := fetchChanges(0)
	if code != 0 || nativeCursor != feed.NextCursor || len(nativeEvents) != len(feed.Events) {
		t.Fatalf("native client feed: %d %d", code, nativeCursor)
	}
	r = call("GET", "/api/v1/sync/changes?cursor=0", "manager", "")
	var raw struct {
		Events     []syncEvent `json:"events"`
		NextCursor int         `json:"nextCursor"`
	}
	if json.Unmarshal(r.Body.Bytes(), &raw) != nil || raw.NextCursor != feed.NextCursor {
		t.Fatal("cursor changed")
	}
	found = false
	for _, event := range raw.Events {
		if event.Topic == "task.approval.requested" {
			var a centralTaskApproval
			_ = json.Unmarshal([]byte(event.Payload), &a)
			if a.ID == approval.ID {
				found = true
				if a.RequestedBy != "9" || a.ApproverID != "8" {
					t.Fatalf("journal rewritten: %+v", a)
				}
			}
		}
	}
	if !found {
		t.Fatal("raw approval missing")
	}
	if r = call("GET", fmt.Sprintf("/api/v1/sync/changes?desktop=1&cursor=%d", feed.NextCursor), "manager", ""); r.Code != 200 || !strings.Contains(r.Body.String(), `"events":[]`) {
		t.Fatalf("next page: %d %s", r.Code, r.Body.String())
	}
}

func TestBookingSyncIdentityConflictsAndRestart(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"admin":{"employeeId":9,"permissions":["rooms.manage","sync.events.read"]},"alice":{"employeeId":8,"permissions":["rooms.book"]},"bob":{"employeeId":7,"permissions":["rooms.book"]},"reader":{"employeeId":6,"permissions":["rooms.read"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	roomResponse := call("POST", "/api/v1/rooms", "admin", `{"name":"Saal","location":"Berlin","capacity":4}`)
	var room centralRoom
	if roomResponse.Code != 201 || json.Unmarshal(roomResponse.Body.Bytes(), &room) != nil {
		t.Fatalf("room %d %s", roomResponse.Code, roomResponse.Body.String())
	}
	v := bookingPayload{ClientID: "alice-device", BookingID: 1, RoomID: 1, RoomKey: room.ID, UserID: 8, Attendees: 2, Status: "confirmed", StartsAt: 1900000000, EndsAt: 1900003600}
	op := func(operation, action, client string, b bookingPayload, expected *bookingPayload) string {
		data, _ := json.Marshal(map[string]any{"clientId": client, "operationId": operation, "action": action, "booking": b, "expected": expected})
		return string(data)
	}
	create := op("one", "create", "alice-device", v, nil)
	path := "/api/v1/booking-sync"
	if r := call("POST", path, "reader", create); r.Code != 403 {
		t.Fatalf("reader: %d", r.Code)
	}
	if r := call("POST", path, "bob", create); r.Code != 403 {
		t.Fatalf("forged actor: %d", r.Code)
	}
	for i := 0; i < 2; i++ {
		if r := call("POST", path, "alice", create); r.Code != 202 {
			t.Fatalf("create: %d %s", r.Code, r.Body.String())
		}
	}
	collision := v
	collision.ClientID = "bob-device"
	collision.UserID = 7
	if r := call("POST", path, "bob", op("other", "create", "bob-device", collision, nil)); r.Code != 409 {
		t.Fatalf("overlap: %d", r.Code)
	}
	changed := v
	changed.StartsAt += 7200
	changed.EndsAt += 7200
	update := op("two", "reschedule", "alice-device", changed, &v)
	if r := call("POST", path, "alice", update); r.Code != 202 {
		t.Fatalf("reschedule: %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", path, "alice", op("three", "reschedule", "alice-device", v, &v)); r.Code != 409 {
		t.Fatalf("stale: %d", r.Code)
	}
	if r := call("PUT", "/api/v1/rooms/"+room.ID, "admin", `{"name":"Saal","location":"Berlin","capacity":1}`); r.Code != 200 {
		t.Fatalf("reduce capacity: %d %s", r.Code, r.Body.String())
	}
	cancelled := changed
	cancelled.Status = "cancelled"
	cancel := op("four", "cancel", "alice-device", cancelled, &changed)
	if r := call("POST", path, "alice", cancel); r.Code != 202 {
		t.Fatalf("cancel: %d %s", r.Code, r.Body.String())
	}
	if r := call("PUT", "/api/v1/rooms/"+room.ID, "admin", `{"name":"Saal","location":"Berlin","capacity":4}`); r.Code != 200 {
		t.Fatalf("restore capacity: %d %s", r.Code, r.Body.String())
	}
	feedResponse := call("GET", "/api/v1/sync/changes?desktop=1&cursor=0", "admin", "")
	var feed struct {
		Events     []syncEvent `json:"events"`
		NextCursor int64       `json:"nextCursor"`
	}
	if feedResponse.Code != 200 || json.Unmarshal(feedResponse.Body.Bytes(), &feed) != nil {
		t.Fatalf("desktop feed: %d %s", feedResponse.Code, feedResponse.Body.String())
	}
	var bookingEvents []syncEvent
	for _, event := range feed.Events {
		if strings.HasPrefix(event.Topic, "booking.") {
			bookingEvents = append(bookingEvents, event)
		}
	}
	if len(bookingEvents) != 3 {
		t.Fatalf("desktop booking events: %+v", bookingEvents)
	}
	for i, expected := range []bookingPayload{v, changed, cancelled} {
		var actual bookingPayload
		if json.Unmarshal([]byte(bookingEvents[i].Payload), &actual) != nil || actual != expected {
			t.Fatalf("desktop booking event %d: %+v", i, bookingEvents[i])
		}
	}

	if response := call("GET", "/api/v1/desktop-booking-snapshot", "alice", ""); response.Code != 403 {
		t.Fatalf("snapshot feed permission: %d", response.Code)
	}
	snapshot := call("GET", "/api/v1/desktop-booking-snapshot", "admin", "")
	var snapshotData struct {
		Cursor   int64            `json:"cursor"`
		Bookings []bookingPayload `json:"bookings"`
	}
	if snapshot.Code != 200 || json.Unmarshal(snapshot.Body.Bytes(), &snapshotData) != nil || snapshotData.Cursor != feed.NextCursor || len(snapshotData.Bookings) != 1 || snapshotData.Bookings[0] != cancelled {
		t.Fatalf("desktop snapshot: %d %s", snapshot.Code, snapshot.Body.String())
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	restartedSnapshot := call("GET", "/api/v1/desktop-booking-snapshot", "admin", "")
	var restartedData struct {
		Cursor   int64            `json:"cursor"`
		Bookings []bookingPayload `json:"bookings"`
	}
	if restartedSnapshot.Code != 200 || json.Unmarshal(restartedSnapshot.Body.Bytes(), &restartedData) != nil || restartedData.Cursor != snapshotData.Cursor || len(restartedData.Bookings) != 1 || restartedData.Bookings[0] != cancelled {
		t.Fatalf("desktop snapshot restart: %d %s", restartedSnapshot.Code, restartedSnapshot.Body.String())
	}
	if r := call("POST", path, "alice", cancel); r.Code != 202 {
		t.Fatalf("retry restart: %d", r.Code)
	}
	r := call("GET", path, "alice", "")
	var all []bookingPayload
	if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &all) != nil || len(all) != 1 || all[0] != cancelled {
		t.Fatalf("snapshot %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", path, "bob", op("other", "create", "bob-device", collision, nil)); r.Code != 202 {
		t.Fatalf("released slot: %d %s", r.Code, r.Body.String())
	}
	server := httptest.NewServer(handler)
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "admin")
	desktopBookings, desktopCursor, desktopCode := desktopBookingSnapshot()
	if desktopCode != 0 || desktopCursor <= snapshotData.Cursor || len(desktopBookings) != 2 {
		t.Fatalf("desktop snapshot transport: %d %d %+v", desktopCode, desktopCursor, desktopBookings)
	}
	t.Setenv("AURA_SYNC_TOKEN", "alice")
	if _, _, code := desktopBookingSnapshot(); code != 8 {
		t.Fatalf("desktop snapshot unauthorized: %d", code)
	}
	client := portable.Client{BaseURL: server.URL, Token: "alice"}
	capability, err := client.Capabilities(context.Background())
	if err != nil || capability.BookingSync != 1 {
		t.Fatalf("booking capability: %+v %v", capability, err)
	}
	person, err := client.Identity(context.Background())
	if err != nil || !person.CanBookRooms || person.EmployeeID != 8 {
		t.Fatalf("booking identity: %+v %v", person, err)
	}
	portableBooking := portable.RoomBooking{ClientID: "alice-device", BookingID: 2, RoomID: 1, RoomKey: room.ID, UserID: 8, Attendees: 2, Status: "confirmed", StartsAt: 1900020000, EndsAt: 1900023600}
	if err := client.SyncBooking(context.Background(), portable.BookingSyncOperation{ClientID: "alice-device", OperationID: "portable", Action: "create", Booking: portableBooking}); err != nil {
		t.Fatalf("portable sync: %v", err)
	}
	values, err := client.BookingsForSync(context.Background())
	if err != nil || len(values) != 3 {
		t.Fatalf("portable snapshot: %d %v", len(values), err)
	}
}

func TestDesktopLeaveFeedUsesProvenAccountIdentities(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["employees.manage","leave.approve","sync.events.read"]},"employee":{"employeeId":8,"permissions":["leave.request"]}}`)
	journal := filepath.Join(t.TempDir(), "desktop-leave.ndjson")
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	response := call("POST", "/api/v1/leave-requests", "employee", `{"startsAt":1000,"endsAt":2000,"reason":"Erholung"}`)
	var leave leaveRequest
	if response.Code != 201 || json.Unmarshal(response.Body.Bytes(), &leave) != nil {
		t.Fatalf("request: %d %s", response.Code, response.Body.String())
	}
	response = call("POST", "/api/v1/leave-requests/"+leave.ID+"/decision", "manager", `{"decision":"approved"}`)
	if response.Code != 200 {
		t.Fatalf("decision: %d %s", response.Code, response.Body.String())
	}
	desktop := "/api/v1/sync/changes?desktop=1&cursor=0"
	if r := call("GET", desktop, "manager", ""); r.Code != 409 {
		t.Fatalf("missing mapping: %d %s", r.Code, r.Body.String())
	}
	ids := map[int]string{}
	for _, id := range []int{8, 9} {
		r := call("POST", "/api/v1/employees", "manager", fmt.Sprintf(`{"name":"Person %d","role":"Team","department":"IT","presence":"office"}`, id))
		var employee centralEmployee
		if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &employee) != nil {
			t.Fatalf("employee: %d %s", r.Code, r.Body.String())
		}
		ids[id] = employee.ID
		r = call("PUT", "/api/v1/employee-account-link?id="+employee.ID, "manager", fmt.Sprintf(`{"accountEmployeeId":%d}`, id))
		if r.Code != 200 {
			t.Fatalf("link: %d %s", r.Code, r.Body.String())
		}
	}
	// Recovery must preserve the account provenance, not infer it from a number.
	handler = syncHandler(journal, "legacy")
	r := call("GET", desktop, "manager", "")
	if r.Code != 200 {
		t.Fatalf("feed: %d %s", r.Code, r.Body.String())
	}
	var feed struct {
		Events []syncEvent `json:"events"`
	}
	if err := json.Unmarshal(r.Body.Bytes(), &feed); err != nil {
		t.Fatal(err)
	}
	count := 0
	for _, event := range feed.Events {
		if event.Topic != "leave.requested" && event.Topic != "leave.decided" {
			continue
		}
		var value struct {
			Employee string `json:"employeeExternalId"`
			Decider  string `json:"deciderExternalId"`
		}
		if err := json.Unmarshal([]byte(event.Payload), &value); err != nil {
			t.Fatal(err)
		}
		if value.Employee != ids[8] || (event.Topic == "leave.decided" && value.Decider != ids[9]) || (event.Topic == "leave.requested" && value.Decider != "") {
			t.Fatalf("mapping: %#v", value)
		}
		count++
	}
	if count != 2 {
		t.Fatalf("count %d", count)
	}
	legacy := `{"id":"desktop:leave:42","employeeId":8,"startsAt":4000,"endsAt":5000,"reason":"Alt","status":"pending","identityKind":"server-account-v1"}`
	event, _ := json.Marshal(syncEvent{Topic: "leave.requested", Payload: legacy})
	if r := call("POST", "/events", "employee", string(event)); r.Code != 202 {
		t.Fatalf("legacy: %d %s", r.Code, r.Body.String())
	}
	if r := call("GET", desktop, "manager", ""); r.Code != 409 {
		t.Fatalf("ambiguous event was translated: %d %s", r.Code, r.Body.String())
	}
	handler = syncHandler(journal, "legacy")
	if r := call("GET", desktop, "manager", ""); r.Code != 409 {
		t.Fatalf("legacy origin was lost on recovery: %d %s", r.Code, r.Body.String())
	}
}

func TestDesktopLeaveEventsUseSharedEmployeeIdentity(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["employees.manage","leave.approve","sync.events.read"]},"employee":{"employeeId":8,"permissions":["leave.request"]},"other":{"employeeId":7,"permissions":["leave.request"]}}`)
	journal := filepath.Join(t.TempDir(), "desktop-leave.ndjson")
	store, err := openCentralStore(filepath.Join(t.TempDir(), "central.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	ids := map[int]string{}
	for _, id := range []int{8, 9} {
		r := call("POST", "/api/v1/employees", "manager", fmt.Sprintf(`{"name":"Person %d","role":"Team","department":"IT","presence":"office"}`, id))
		var employee centralEmployee
		if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &employee) != nil {
			t.Fatalf("employee: %d %s", r.Code, r.Body.String())
		}
		ids[id] = employee.ID
		if r = call("PUT", "/api/v1/employee-account-link?id="+employee.ID, "manager", fmt.Sprintf(`{"accountEmployeeId":%d}`, id)); r.Code != 200 {
			t.Fatalf("link: %d %s", r.Code, r.Body.String())
		}
	}
	makeEvent := func(topic, employee, decider, status string) string {
		payload, _ := json.Marshal(map[string]any{"id": "desktop:leave:1", "employeeExternalId": employee, "deciderExternalId": decider, "startsAt": 1000, "endsAt": 2000, "reason": "Urlaub", "status": status})
		event, _ := json.Marshal(syncEvent{Topic: topic, Payload: string(payload)})
		return string(event)
	}
	path := "/api/v1/desktop-leave-events"
	request := makeEvent("desktop.leave.requested", ids[8], "", "pending")
	if r := call("POST", path, "other", request); r.Code != 403 {
		t.Fatalf("forged request: %d", r.Code)
	}
	if r := call("POST", path, "employee", makeEvent("desktop.leave.requested", "8", "", "pending")); r.Code != 409 {
		t.Fatalf("numeric local id accepted: %d", r.Code)
	}
	if r := call("POST", path, "employee", request); r.Code != 202 {
		t.Fatalf("request: %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", path, "employee", request); r.Code != 202 {
		t.Fatalf("retry: %d", r.Code)
	}
	decision := makeEvent("desktop.leave.decided", ids[8], ids[9], "approved")
	if r := call("POST", path, "employee", decision); r.Code != 403 {
		t.Fatalf("forged decision: %d", r.Code)
	}
	if r := call("POST", path, "manager", decision); r.Code != 202 {
		t.Fatalf("decision: %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", path, "employee", request); r.Code != 202 {
		t.Fatalf("request retry after decision: %d %s", r.Code, r.Body.String())
	}
	handler = syncHandler(journal, "legacy")
	if r := call("POST", path, "employee", request); r.Code != 202 {
		t.Fatalf("request retry after restart: %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", path, "manager", decision); r.Code != 202 {
		t.Fatalf("restart retry: %d", r.Code)
	}
	if r := call("POST", path, "manager", makeEvent("desktop.leave.decided", ids[8], ids[9], "rejected")); r.Code != 409 {
		t.Fatalf("conflicting decision: %d", r.Code)
	}
	r := call("GET", "/api/v1/leave-requests", "manager", "")
	var leaves []leaveRequest
	if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &leaves) != nil || len(leaves) != 1 || leaves[0].EmployeeID != 8 || leaves[0].DecidedBy != 9 || leaves[0].Status != "approved" {
		t.Fatalf("central projection: %d %s", r.Code, r.Body.String())
	}
}

func TestOwnPresenceIsAccountScopedAndChecksExpectedState(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"admin":{"employeeId":9,"permissions":["employees.manage"]},"ada":{"employeeId":8,"permissions":["leave.request"]},"other":{"employeeId":7,"permissions":["leave.request"]}}`)
	journal := filepath.Join(t.TempDir(), "presence.ndjson")
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	if r := call("GET", "/api/v1/my-presence", "ada", ""); r.Code != 409 {
		t.Fatalf("unmapped: %d", r.Code)
	}
	r := call("POST", "/api/v1/employees", "admin", `{"name":"Ada","role":"IT","department":"Team","presence":"office"}`)
	var employee centralEmployee
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &employee) != nil {
		t.Fatalf("create: %d %s", r.Code, r.Body.String())
	}
	if r := call("PUT", "/api/v1/employee-account-link?id="+employee.ID, "admin", `{"accountEmployeeId":8}`); r.Code != 200 {
		t.Fatalf("link: %d %s", r.Code, r.Body.String())
	}
	if r := call("PATCH", "/api/v1/my-presence", "other", `{"presence":"remote","expected":"office"}`); r.Code != 409 {
		t.Fatalf("unmapped write: %d", r.Code)
	}
	if r := call("PATCH", "/api/v1/my-presence", "ada", `{"presence":"remote","expected":"office","employeeId":9}`); r.Code != 400 {
		t.Fatalf("foreign id accepted: %d", r.Code)
	}
	if r := call("PATCH", "/api/v1/my-presence", "ada", `{"presence":"remote","expected":"office","employeeId":"other-employee"}`); r.Code != 409 {
		t.Fatalf("wrong shared identity accepted: %d", r.Code)
	}
	if r := call("PATCH", "/api/v1/my-presence", "ada", `{"presence":"remote","expected":"office"}`); r.Code != 200 {
		t.Fatalf("update: %d %s", r.Code, r.Body.String())
	}
	if r := call("PATCH", "/api/v1/my-presence", "ada", `{"presence":"away","expected":"office"}`); r.Code != 409 {
		t.Fatalf("stale update: %d", r.Code)
	}
	handler = syncHandler(journal, "legacy")
	if r := call("GET", "/api/v1/my-presence", "ada", ""); r.Code != 200 || !strings.Contains(r.Body.String(), `"presence":"remote"`) {
		t.Fatalf("restart: %d %s", r.Code, r.Body.String())
	}
}

func TestLegacyLeaveIdentityIsNotAttributedByCoincidentalNumber(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["leave.request","leave.approve"]},"employee":{"employeeId":8,"permissions":["leave.request"]}}`)
	journal := filepath.Join(t.TempDir(), "legacy-leave.ndjson")
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	// The legacy number 8 is a desktop-local employee ID, not proof of account 8.
	old := `{"id":"old-windows:leave:1","employeeId":8,"startsAt":1000,"endsAt":2000,"reason":"Altbestand","status":"pending"}`
	event, _ := json.Marshal(syncEvent{Topic: "leave.requested", Payload: old})
	if r := call("POST", "/events", "employee", string(event)); r.Code != 202 {
		t.Fatalf("legacy event: %d %s", r.Code, r.Body.String())
	}
	handler = syncHandler(journal, "legacy")
	if r := call("GET", "/api/v1/leave-requests", "employee", ""); r.Code != 200 || strings.Contains(r.Body.String(), "Altbestand") {
		t.Fatalf("unverified data disclosed to coincidental account: %d %s", r.Code, r.Body.String())
	}
	if r := call("GET", "/api/v1/leave-requests", "manager", ""); r.Code != 200 || !strings.Contains(r.Body.String(), `"identityVerified":false`) {
		t.Fatalf("unverified data not flagged: %d %s", r.Code, r.Body.String())
	}
	if r := call("POST", "/api/v1/leave-requests/old-windows:leave:1/decision", "manager", `{"decision":"approved"}`); r.Code != 409 {
		t.Fatalf("unverified decision: %d %s", r.Code, r.Body.String())
	}
	verified := call("POST", "/api/v1/leave-requests", "employee", `{"startsAt":3000,"endsAt":4000,"reason":"Verifiziert"}`)
	var item leaveRequest
	if verified.Code != 201 || json.Unmarshal(verified.Body.Bytes(), &item) != nil {
		t.Fatalf("verified create: %d %s", verified.Code, verified.Body.String())
	}
	forged, _ := json.Marshal(syncEvent{Topic: "leave.requested", Payload: fmt.Sprintf(`{"id":%q,"employeeId":8,"startsAt":3000,"endsAt":4000,"reason":"Änderung","status":"pending"}`, item.ID)})
	if r := call("POST", "/events", "employee", string(forged)); r.Code != 409 {
		t.Fatalf("verified data overwritten by legacy event: %d %s", r.Code, r.Body.String())
	}
	if r := call("GET", "/api/v1/leave-requests", "employee", ""); r.Code != 200 || !strings.Contains(r.Body.String(), `"identityVerified":true`) || strings.Contains(r.Body.String(), "Änderung") {
		t.Fatalf("verified listing: %d %s", r.Code, r.Body.String())
	}
}

func TestLegacyLeaveReconciliationIsExplicitAuditedAndSurvivesRestart(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["employees.manage","leave.approve","sync.events.read"]},"employee":{"employeeId":8,"permissions":["leave.request"]}}`)
	journal := filepath.Join(t.TempDir(), "legacy-reconcile.ndjson")
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	legacy := leaveRequest{ID: "old:leave:1", EmployeeID: 77, StartsAt: 1000, EndsAt: 2000, Reason: "Alt", Status: "pending"}
	raw, _ := json.Marshal(legacy)
	event, _ := json.Marshal(syncEvent{Topic: "leave.requested", Payload: string(raw)})
	if r := call("POST", "/events", "employee", string(event)); r.Code != 202 {
		t.Fatalf("legacy: %d %s", r.Code, r.Body.String())
	}
	if r := call("GET", "/api/v1/sync/changes?desktop=1", "manager", ""); r.Code != 409 {
		t.Fatalf("unresolved feed: %d", r.Code)
	}
	r := call("POST", "/api/v1/employees", "manager", `{"name":"Ada","role":"Team","department":"IT","presence":"office"}`)
	var employee centralEmployee
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &employee) != nil {
		t.Fatalf("employee: %d %s", r.Code, r.Body.String())
	}
	if r = call("PUT", "/api/v1/employee-account-link?id="+employee.ID, "manager", `{"accountEmployeeId":8}`); r.Code != 200 {
		t.Fatalf("link: %d", r.Code)
	}
	input, _ := json.Marshal(map[string]any{"expected": legacy, "employeeExternalId": employee.ID, "deciderExternalId": ""})
	if r = call("POST", "/api/v1/legacy-leave-reconcile", "employee", string(input)); r.Code != 403 {
		t.Fatalf("unauthorized: %d", r.Code)
	}
	wrong := legacy
	wrong.Reason = "Falsch"
	stale, _ := json.Marshal(map[string]any{"expected": wrong, "employeeExternalId": employee.ID, "deciderExternalId": ""})
	if r = call("POST", "/api/v1/legacy-leave-reconcile", "manager", string(stale)); r.Code != 409 {
		t.Fatalf("stale: %d", r.Code)
	}
	if r = call("POST", "/api/v1/legacy-leave-reconcile", "manager", string(input)); r.Code != 200 {
		t.Fatalf("reconcile: %d %s", r.Code, r.Body.String())
	}
	if r = call("POST", "/api/v1/legacy-leave-reconcile", "manager", string(input)); r.Code != 200 {
		t.Fatalf("retry: %d", r.Code)
	}
	handler = syncHandler(journal, "legacy")
	if r = call("POST", "/api/v1/legacy-leave-reconcile", "manager", string(input)); r.Code != 200 {
		t.Fatalf("retry after restart: %d", r.Code)
	}
	r = call("GET", "/api/v1/leave-requests", "employee", "")
	if r.Code != 200 || !strings.Contains(r.Body.String(), `"employeeId":8`) || !strings.Contains(r.Body.String(), `"identityVerified":true`) {
		t.Fatalf("recovered: %d %s", r.Code, r.Body.String())
	}
	r = call("GET", "/api/v1/sync/changes?desktop=1", "manager", "")
	if r.Code != 200 || !strings.Contains(r.Body.String(), "central.leave.legacy.superseded") || !strings.Contains(r.Body.String(), "employeeExternalId") {
		t.Fatalf("desktop feed: %d %s", r.Code, r.Body.String())
	}
	events, _, err := changesSince(journal, 0)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, e := range events {
		if e.Topic == "leave.identity.reconciled" {
			found = true
			if !strings.Contains(e.Payload, `"employeeId":77`) || !strings.Contains(e.Payload, `"reconciledBy":9`) {
				t.Fatalf("audit payload: %s", e.Payload)
			}
		}
	}
	if !found {
		t.Fatal("no audit event")
	}
	forged, _ := json.Marshal(syncEvent{Topic: "leave.identity.reconciled", Payload: "{}"})
	if r = call("POST", "/events", "manager", string(forged)); r.Code != 403 {
		t.Fatalf("forged audit: %d", r.Code)
	}
}

func TestVerifiedLegacyLeaveRequiresExplicitOriginConfirmation(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["employees.manage","leave.approve","sync.events.read"]},"employee":{"employeeId":8,"permissions":["leave.request"]}}`)
	journal := filepath.Join(t.TempDir(), "old-account-leave.ndjson")
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	r := call("POST", "/api/v1/employees", "manager", `{"name":"Ada","role":"Team","department":"IT","presence":"office"}`)
	var employee centralEmployee
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &employee) != nil {
		t.Fatal(r.Body.String())
	}
	if r = call("PUT", "/api/v1/employee-account-link?id="+employee.ID, "manager", `{"accountEmployeeId":8}`); r.Code != 200 {
		t.Fatal(r.Body.String())
	}
	old := leaveRequest{ID: "old-account:1", EmployeeID: 8, StartsAt: 1000, EndsAt: 2000, Reason: "Alt", Status: "pending"}
	if err := persistEvent(journal, syncEvent{Topic: "leave.requested", Payload: string(encodeLeaveEvent(old, true))}); err != nil {
		t.Fatal(err)
	}
	handler = syncHandler(journal, "legacy")
	if r = call("GET", "/api/v1/leave-requests", "manager", ""); r.Code != 200 || !strings.Contains(r.Body.String(), `"identityOriginVerified":false`) {
		t.Fatalf("unknown origin: %d %s", r.Code, r.Body.String())
	}
	other := old
	other.EmployeeID = 9
	bad, _ := json.Marshal(map[string]any{"expected": other, "employeeExternalId": employee.ID, "deciderExternalId": ""})
	if r = call("POST", "/api/v1/legacy-leave-reconcile", "manager", string(bad)); r.Code != 409 {
		t.Fatalf("stale confirmation: %d", r.Code)
	}
	input, _ := json.Marshal(map[string]any{"expected": old, "employeeExternalId": employee.ID, "deciderExternalId": ""})
	for i := 0; i < 2; i++ {
		if r = call("POST", "/api/v1/legacy-leave-reconcile", "manager", string(input)); r.Code != 200 {
			t.Fatalf("confirm: %d %s", r.Code, r.Body.String())
		}
	}
	handler = syncHandler(journal, "legacy")
	if r = call("GET", "/api/v1/leave-requests", "manager", ""); r.Code != 200 || !strings.Contains(r.Body.String(), `"identityOriginVerified":true`) {
		t.Fatalf("confirmed origin: %d %s", r.Code, r.Body.String())
	}
	if r = call("PUT", "/api/v1/employee-account-link?id="+employee.ID, "manager", `{"accountEmployeeId":0}`); r.Code != 200 {
		t.Fatal(r.Body.String())
	}
	if r = call("GET", "/api/v1/leave-requests", "employee", ""); r.Code != 200 || strings.Contains(r.Body.String(), "old-account:1") {
		t.Fatalf("disclosed after unlink: %d %s", r.Code, r.Body.String())
	}
	if r = call("GET", "/api/v1/sync/changes?desktop=1", "manager", ""); r.Code != 409 {
		t.Fatalf("desktop feed after unlink: %d", r.Code)
	}
}

func TestLegacyDecidedLeaveRequiresTwoDifferentMappedPeople(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["employees.manage","leave.approve"]},"employee":{"employeeId":8,"permissions":["leave.request"]}}`)
	journal := filepath.Join(t.TempDir(), "legacy-decided.ndjson")
	handler := syncHandler(journal, "legacy")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	legacy := leaveRequest{ID: "legacy:decided", EmployeeID: 77, StartsAt: 100, EndsAt: 200, Reason: "Alt entschieden", Status: "approved", DecidedBy: 55}
	body, _ := json.Marshal(legacy)
	event, _ := json.Marshal(syncEvent{Topic: "leave.decided", Payload: string(body)})
	if r := call("POST", "/events", "manager", string(event)); r.Code != 202 {
		t.Fatalf("legacy: %d %s", r.Code, r.Body.String())
	}
	ids := map[int]string{}
	for _, id := range []int{8, 9} {
		r := call("POST", "/api/v1/employees", "manager", fmt.Sprintf(`{"name":"Person %d","role":"Team","department":"IT","presence":"office"}`, id))
		var person centralEmployee
		if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &person) != nil {
			t.Fatal(r.Body.String())
		}
		ids[id] = person.ID
		if r = call("PUT", "/api/v1/employee-account-link?id="+person.ID, "manager", fmt.Sprintf(`{"accountEmployeeId":%d}`, id)); r.Code != 200 {
			t.Fatal(r.Body.String())
		}
	}
	makeInput := func(decider string) string {
		data, _ := json.Marshal(map[string]any{"expected": legacy, "employeeExternalId": ids[8], "deciderExternalId": decider})
		return string(data)
	}
	if r := call("POST", "/api/v1/legacy-leave-reconcile", "manager", makeInput("")); r.Code != 409 {
		t.Fatalf("missing decider: %d", r.Code)
	}
	if r := call("POST", "/api/v1/legacy-leave-reconcile", "manager", makeInput(ids[8])); r.Code != 409 {
		t.Fatalf("self decision: %d", r.Code)
	}
	if r := call("POST", "/api/v1/legacy-leave-reconcile", "manager", makeInput(ids[9])); r.Code != 200 {
		t.Fatalf("reconcile decision: %d %s", r.Code, r.Body.String())
	}
	handler = syncHandler(journal, "legacy")
	if r := call("GET", "/api/v1/leave-requests", "employee", ""); r.Code != 200 || !strings.Contains(r.Body.String(), `"employeeId":8`) || !strings.Contains(r.Body.String(), `"decidedBy":9`) || !strings.Contains(r.Body.String(), `"status":"approved"`) {
		t.Fatalf("recovered: %d %s", r.Code, r.Body.String())
	}
}

func TestDesktopBookingDetailsUseVerifiedAccount(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"admin":{"employeeId":9,"permissions":["rooms.manage","employees.manage"]},"alice":{"employeeId":8,"permissions":["rooms.book"]},"bob":{"employeeId":7,"permissions":["rooms.book"]}}`)
	dir := t.TempDir()
	store, err := openCentralStore(filepath.Join(dir, "central.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	h := syncHandler(filepath.Join(dir, "events.ndjson"), "legacy")
	call := func(path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest("POST", path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w
	}
	rr := call("/api/v1/rooms", "admin", `{"name":"Raum","location":"Bonn","capacity":4}`)
	var room centralRoom
	if rr.Code != 201 || json.Unmarshal(rr.Body.Bytes(), &room) != nil {
		t.Fatalf("room: %d %s", rr.Code, rr.Body.String())
	}
	er := call("/api/v1/employees", "admin", `{"name":"Alice","role":"Team","department":"IT","presence":"office"}`)
	var employee centralEmployee
	if er.Code != 201 || json.Unmarshal(er.Body.Bytes(), &employee) != nil {
		t.Fatalf("employee: %d %s", er.Code, er.Body.String())
	}
	linkReq := httptest.NewRequest("PUT", "/api/v1/employee-account-link?id="+employee.ID, strings.NewReader(`{"accountEmployeeId":8}`))
	linkReq.Header.Set("Authorization", "Bearer admin")
	linkRes := httptest.NewRecorder()
	h.ServeHTTP(linkRes, linkReq)
	if linkRes.Code != 200 {
		t.Fatalf("link: %d %s", linkRes.Code, linkRes.Body.String())
	}
	reservation := func(id int64, key string, attendees int) string {
		return fmt.Sprintf(`{"clientId":"desktop-a","reservationId":%d,"bookingId":0,"roomId":12,"roomKey":%q,"startsAt":1900000000,"endsAt":1900003600,"employeeKey":%q,"attendees":%d}`, id, room.ID, key, attendees)
	}
	if r := call("/bookings/reserve", "bob", reservation(-1, employee.ID, 2)); r.Code != 403 {
		t.Fatalf("forged employee: %d", r.Code)
	}
	if r := call("/bookings/reserve", "alice", reservation(-1, employee.ID, 5)); r.Code != 403 {
		t.Fatalf("capacity: %d", r.Code)
	}
	if r := call("/bookings/reserve", "alice", reservation(-1, employee.ID, 2)); r.Code != 204 {
		t.Fatalf("reserve: %d %s", r.Code, r.Body.String())
	}
	if r := call("/bookings/release", "bob", `{"clientId":"desktop-a","reservationId":-1}`); r.Code != 403 {
		t.Fatalf("forged release: %d", r.Code)
	}
	commit := `{"clientId":"desktop-a","reservationId":-1,"bookingId":17,"topic":"booking.created"}`
	if r := call("/bookings/commit", "bob", commit); r.Code != 403 {
		t.Fatalf("forged commit: %d", r.Code)
	}
	if r := call("/bookings/commit", "alice", commit); r.Code != 202 {
		t.Fatalf("commit: %d %s", r.Code, r.Body.String())
	}
	get := httptest.NewRequest("GET", "/api/v1/booking-sync", nil)
	get.Header.Set("Authorization", "Bearer alice")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, get)
	var bookings []bookingPayload
	if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &bookings) != nil || len(bookings) != 1 || bookings[0].UserID != 8 || bookings[0].RoomID != 1 || bookings[0].Attendees != 2 || bookings[0].Status != "confirmed" {
		t.Fatalf("booking metadata: %d %s", w.Code, w.Body.String())
	}
	// The desktop's subsequent legacy event must not erase verified details.
	legacyPayload := fmt.Sprintf(`{"clientId":"desktop-a","bookingId":17,"roomId":12,"roomKey":%q,"startsAt":1900000000,"endsAt":1900003600}`, room.ID)
	eventBody, _ := json.Marshal(syncEvent{Topic: "booking.created", Payload: legacyPayload})
	if r := call("/events", "alice", string(eventBody)); r.Code != 202 {
		t.Fatalf("matching legacy event: %d %s", r.Code, r.Body.String())
	}
	cancelBody, _ := json.Marshal(syncEvent{Topic: "booking.cancelled", Payload: `{"clientId":"desktop-a","bookingId":17}`})
	if r := call("/events", "bob", string(cancelBody)); r.Code != 403 {
		t.Fatalf("forged cancellation: %d", r.Code)
	}
	get2 := httptest.NewRequest("GET", "/api/v1/booking-sync", nil)
	get2.Header.Set("Authorization", "Bearer alice")
	w2 := httptest.NewRecorder()
	h.ServeHTTP(w2, get2)
	var after []bookingPayload
	if w2.Code != 200 || json.Unmarshal(w2.Body.Bytes(), &after) != nil || len(after) != 1 || after[0].UserID != 8 || after[0].Attendees != 2 {
		t.Fatalf("metadata lost: %s", w2.Body.String())
	}
	legacy := fmt.Sprintf(`{"clientId":"desktop-a","reservationId":-2,"bookingId":17,"roomId":12,"roomKey":%q,"startsAt":1900010000,"endsAt":1900013600}`, room.ID)
	if r := call("/bookings/reserve", "alice", legacy); r.Code != 403 {
		t.Fatalf("legacy overwrite: %d", r.Code)
	}
	server := httptest.NewServer(h)
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "alice")
	t.Setenv("AURA_SYNC_CLIENT_ID", "desktop-a")
	if code := reserveBookingWithDetails(-3, 0, 12, 1900020000, 1900023600, room.ID, employee.ID, 3); code != 0 {
		t.Fatalf("desktop reserve transport: %d", code)
	}
	if code := commitBooking(-3, 18, false); code != 0 {
		t.Fatalf("desktop commit transport: %d", code)
	}
	get3 := httptest.NewRequest("GET", "/api/v1/booking-sync", nil)
	get3.Header.Set("Authorization", "Bearer alice")
	w3 := httptest.NewRecorder()
	h.ServeHTTP(w3, get3)
	var final []bookingPayload
	if json.Unmarshal(w3.Body.Bytes(), &final) != nil || len(final) != 2 || final[1].BookingID != 18 || final[1].UserID != 8 || final[1].Attendees != 3 {
		t.Fatalf("desktop transport metadata: %s", w3.Body.String())
	}
	if r := call("/events", "alice", string(cancelBody)); r.Code != 202 {
		t.Fatalf("owner cancellation: %d", r.Code)
	}
	legacyNew := fmt.Sprintf(`{"clientId":"desktop-a","reservationId":-4,"bookingId":0,"roomId":12,"roomKey":%q,"startsAt":1900040000,"endsAt":1900043600}`, room.ID)
	if r := call("/bookings/reserve", "alice", legacyNew); r.Code != 204 {
		t.Fatalf("legacy reserve: %d", r.Code)
	}
	if r := call("/bookings/commit", "alice", `{"clientId":"desktop-a","reservationId":-4,"bookingId":17,"topic":"booking.created"}`); r.Code != 403 {
		t.Fatalf("legacy resurrection: %d", r.Code)
	}
	if r := call("/events", "alice", string(eventBody)); r.Code != 409 {
		t.Fatalf("legacy event resurrection: %d", r.Code)
	}

}

func TestDesktopBookingDetailsRequireServerCapability(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/api/v1/capabilities" {
			t.Errorf("unexpected request: %s", r.URL.Path)
			w.WriteHeader(500)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"canonicalRoomBooking":1}`))
	}))
	defer server.Close()
	t.Setenv("AURA_SYNC_URL", server.URL)
	t.Setenv("AURA_SYNC_TOKEN", "token")
	t.Setenv("AURA_SYNC_CLIENT_ID", "desktop")
	if code := reserveBookingWithDetails(-1, 0, 1, 100, 200, "room:1", "employee:1", 2); code != 8 || calls != 1 {
		t.Fatalf("old server accepted: code=%d calls=%d", code, calls)
	}
}

func TestRoomIdentityConflictAbandonmentIsDurable(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"admin":{"employeeId":9,"permissions":["rooms.manage","sync.events.write"]},"reader":{"employeeId":8,"permissions":["rooms.read"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(path, token string, body any) *httptest.ResponseRecorder {
		payload, _ := json.Marshal(body)
		r := httptest.NewRequest("POST", path, bytes.NewReader(payload))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	r := call("/api/v1/rooms", "admin", map[string]any{"name": "Saal", "location": "Bonn", "capacity": 8})
	var remote centralRoom
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &remote) != nil {
		t.Fatalf("room %d %s", r.Code, r.Body.String())
	}
	op := roomSyncOperation{ClientID: "linux", OperationID: "create-1", Action: "create", Room: centralRoom{ID: "linux:room:1", Name: "Saal", Location: "Berlin", Capacity: 6, Active: true}}
	request := map[string]any{"operation": op, "remote": remote}
	if r = call("/api/v1/room-sync", "admin", op); r.Code != 409 {
		t.Fatalf("name collision %d", r.Code)
	}
	if r = call("/api/v1/room-sync-abandon", "reader", request); r.Code != 403 {
		t.Fatalf("permissions %d", r.Code)
	}
	stale := remote
	stale.Capacity++
	if r = call("/api/v1/room-sync-abandon", "admin", map[string]any{"operation": op, "remote": stale}); r.Code != 409 {
		t.Fatalf("stale review %d", r.Code)
	}
	if r = call("/api/v1/room-sync-abandon", "admin", request); r.Code != 202 {
		t.Fatalf("abandon %d %s", r.Code, r.Body.String())
	}
	if r = call("/api/v1/room-sync-abandon", "admin", request); r.Code != 202 {
		t.Fatalf("retry %d", r.Code)
	}
	modified := op
	modified.Room.Location = "Anders"
	if r = call("/api/v1/room-sync-abandon", "admin", map[string]any{"operation": modified, "remote": remote}); r.Code != 409 {
		t.Fatalf("operation identity reused: %d", r.Code)
	}
	accepted := op
	accepted.OperationID = "accepted"
	accepted.Room.ID = "linux:room:2"
	accepted.Room.Name = "Anderer Saal"
	if r = call("/api/v1/room-sync", "admin", accepted); r.Code != 202 {
		t.Fatalf("accept room: %d", r.Code)
	}
	if r = call("/api/v1/room-sync-abandon", "admin", map[string]any{"operation": accepted, "remote": remote}); r.Code != 409 {
		t.Fatalf("accepted operation abandoned: %d", r.Code)
	}
	if r = call("/api/v1/room-sync", "admin", op); r.Code != 409 {
		t.Fatalf("late create %d", r.Code)
	}
	forged := syncEvent{Topic: "central.room_sync.abandoned", Payload: "{}"}
	if r = call("/events", "admin", forged); r.Code != 403 {
		t.Fatalf("forged fence %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r = call("/api/v1/room-sync-abandon", "admin", request); r.Code != 202 {
		t.Fatalf("retry after restart %d", r.Code)
	}
	if r = call("/api/v1/room-sync", "admin", op); r.Code != 409 {
		t.Fatalf("late create after restart %d", r.Code)
	}
}

func TestEmployeeIdentityConflictAbandonmentIsDurable(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"admin":{"employeeId":9,"permissions":["employees.manage","sync.events.write"]},"reader":{"employeeId":8,"permissions":["employees.read"]}}`)
	dir := t.TempDir()
	db, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(path, token string, body any) *httptest.ResponseRecorder {
		payload, _ := json.Marshal(body)
		r := httptest.NewRequest("POST", path, bytes.NewReader(payload))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	r := call("/api/v1/employees", "admin", map[string]any{"name": "Ada", "role": "Developer", "department": "IT", "presence": "office"})
	var remote centralEmployee
	if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &remote) != nil {
		t.Fatalf("employee %d %s", r.Code, r.Body.String())
	}
	op := employeeSyncOperation{ClientID: "linux", OperationID: "create-1", Action: "create", Employee: centralEmployee{ID: "linux:employee:1", Name: "Ada", Role: "Developer", Department: "Lokal", Presence: "office", Active: true}}
	request := map[string]any{"operation": op, "remote": remote}
	if r = call("/api/v1/employee-sync", "admin", op); r.Code != 409 {
		t.Fatalf("name collision: %d", r.Code)
	}
	if r = call("/api/v1/employee-sync-abandon", "reader", request); r.Code != 403 {
		t.Fatalf("permissions: %d", r.Code)
	}
	stale := remote
	stale.Department = "anders"
	if r = call("/api/v1/employee-sync-abandon", "admin", map[string]any{"operation": op, "remote": stale}); r.Code != 409 {
		t.Fatalf("stale review: %d", r.Code)
	}
	if r = call("/api/v1/employee-sync-abandon", "admin", request); r.Code != 202 {
		t.Fatalf("abandon: %d %s", r.Code, r.Body.String())
	}
	if r = call("/api/v1/employee-sync-abandon", "admin", request); r.Code != 202 {
		t.Fatalf("retry: %d", r.Code)
	}
	altered := op
	altered.Employee.Department = "anders"
	if r = call("/api/v1/employee-sync-abandon", "admin", map[string]any{"operation": altered, "remote": remote}); r.Code != 409 {
		t.Fatalf("operation reuse: %d", r.Code)
	}
	if r = call("/api/v1/employee-sync", "admin", op); r.Code != 409 {
		t.Fatalf("late create: %d", r.Code)
	}
	forged := syncEvent{Topic: "central.employee_sync.abandoned", Payload: "{}"}
	if r = call("/events", "admin", forged); r.Code != 403 {
		t.Fatalf("forged fence: %d", r.Code)
	}
	store.close()
	store, err = openCentralStore(db)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	if r = call("/api/v1/employee-sync-abandon", "admin", request); r.Code != 202 {
		t.Fatalf("retry after restart: %d", r.Code)
	}
	if r = call("/api/v1/employee-sync", "admin", op); r.Code != 409 {
		t.Fatalf("late create after restart: %d", r.Code)
	}
}

func TestLegacyRecurrencePreflightRejectsUnsendableCompletion(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage"]}}`)
	dir := t.TempDir()
	store, err := openCentralStore(filepath.Join(dir, "central.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(filepath.Join(dir, "events.ndjson"), "legacy")
	call := func(method, path string, body any) *httptest.ResponseRecorder {
		data, _ := json.Marshal(body)
		req := httptest.NewRequest(method, path, bytes.NewReader(data))
		req.Header.Set("Authorization", "Bearer manager")
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	makeTask := func(title string) centralTask {
		r := call("POST", "/api/v1/tasks", map[string]any{"title": title, "department": "IT", "priority": "Mittel", "assigneeId": 9, "recurrenceDays": 7})
		var task centralTask
		if r.Code != 201 || json.Unmarshal(r.Body.Bytes(), &task) != nil {
			t.Fatalf("create: %d %s", r.Code, r.Body.String())
		}
		return task
	}
	parent := makeTask("Aufgabe mit Unteraufgabe")
	sub := centralSubtask{ID: "linux:subtask:preflight", TaskID: parent.ID, Title: "Offen", Status: "open", Active: true}
	subOp := map[string]any{"clientId": "linux", "operationId": "sub", "action": "create", "subtask": sub, "expected": nil}
	if r := call("POST", "/api/v1/subtask-sync", subOp); r.Code != 202 {
		t.Fatalf("subtask: %d %s", r.Code, r.Body.String())
	}
	parentDone := parent
	parentDone.Status = "done"
	parentOp := taskSyncOperation{ClientID: "linux", OperationID: "parent-done", Action: "update", Task: parentDone, Expected: &parent}
	parentReserve := map[string]any{"clientId": "linux", "operationId": "parent-reserve", "original": parentOp, "successorId": "linux:task:parent-next"}
	if r := call("POST", "/api/v1/task-recurrence-reserve", parentReserve); r.Code != 409 {
		t.Fatalf("unfinished subtask: %d %s", r.Code, r.Body.String())
	}
	source, dependency := makeTask("Quelle"), makeTask("Voraussetzung")
	r := call("POST", "/api/v1/dependency-sync", map[string]any{"clientId": "linux", "operationId": "edge", "taskId": source.ID, "dependencyId": dependency.ID, "present": true, "expected": false})
	if r.Code != 202 {
		t.Fatalf("dependency: %d %s", r.Code, r.Body.String())
	}
	done := source
	done.Status = "done"
	original := taskSyncOperation{ClientID: "linux", OperationID: "original", Action: "update", Task: done, Expected: &source}
	reserve := map[string]any{"clientId": "linux", "operationId": "reserve", "original": original, "successorId": "linux:task:next"}
	if r = call("POST", "/api/v1/task-recurrence-reserve", reserve); r.Code != 409 {
		t.Fatalf("unfinished prerequisite: %d %s", r.Code, r.Body.String())
	}
	reserve["operationId"] = "original"
	if r = call("POST", "/api/v1/task-recurrence-reserve", reserve); r.Code != 400 {
		t.Fatalf("same operation ID: %d %s", r.Code, r.Body.String())
	}
	r = call("POST", "/api/v1/dependency-sync", map[string]any{"clientId": "linux", "operationId": "remove", "taskId": source.ID, "dependencyId": dependency.ID, "present": false, "expected": true})
	if r.Code != 202 {
		t.Fatalf("remove: %d %s", r.Code, r.Body.String())
	}
	if r = call("POST", "/api/v1/tasks/"+source.ID+"/status", map[string]string{"status": "done"}); r.Code != 200 {
		t.Fatalf("completion after rejection: %d %s", r.Code, r.Body.String())
	}
}

func TestMaintenanceRecurrenceOverflowDoesNotCommit(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["facility.manage"]}}`)
	dir := t.TempDir()
	store, err := openCentralStore(filepath.Join(dir, "central.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(filepath.Join(dir, "events.ndjson"), "legacy")
	call := func(method, path string, body any) *httptest.ResponseRecorder {
		data, _ := json.Marshal(body)
		req := httptest.NewRequest(method, path, bytes.NewReader(data))
		req.Header.Set("Authorization", "Bearer manager")
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	asset := call("POST", "/api/v1/facility-asset-sync", map[string]any{"clientId": "linux", "operationId": "asset", "action": "create", "facilityAsset": map[string]any{"id": "a1", "name": "Anlage", "category": "IT", "location": "EG", "status": "operational", "active": true}, "expected": nil})
	if asset.Code != 202 {
		t.Fatalf("asset: %d %s", asset.Code, asset.Body.String())
	}
	path := "/api/v1/maintenance?assetId=a1"
	created := call("POST", path, map[string]any{"title": "Grenze", "dueAt": int64(^uint64(0) >> 1), "recurrenceDays": 1})
	var order centralMaintenanceOrder
	if created.Code != 201 || json.Unmarshal(created.Body.Bytes(), &order) != nil {
		t.Fatalf("create: %d %s", created.Code, created.Body.String())
	}
	changed := call("PATCH", path+"&id="+order.ID, map[string]string{"status": "completed"})
	if changed.Code != 400 {
		t.Fatalf("overflow: %d %s", changed.Code, changed.Body.String())
	}
	result := call("GET", path, nil)
	var all []centralMaintenanceOrder
	if result.Code != 200 || json.Unmarshal(result.Body.Bytes(), &all) != nil || len(all) != 1 || all[0].Status != "open" {
		t.Fatalf("not rolled back: %d %s", result.Code, result.Body.String())
	}
}

func TestMaintenanceRecurrenceReopenAndRestart(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["facility.manage"]}}`)
	dir := t.TempDir()
	database, journal := filepath.Join(dir, "central.sqlite"), filepath.Join(dir, "events.ndjson")
	store, err := openCentralStore(database)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	handler := syncHandler(journal, "legacy")
	call := func(method, path string, body any) *httptest.ResponseRecorder {
		data, _ := json.Marshal(body)
		req := httptest.NewRequest(method, path, bytes.NewReader(data))
		req.Header.Set("Authorization", "Bearer manager")
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	asset := call("POST", "/api/v1/facility-asset-sync", map[string]any{"clientId": "linux", "operationId": "asset", "action": "create", "facilityAsset": map[string]any{"id": "a1", "name": "Anlage", "category": "IT", "location": "EG", "status": "operational", "active": true}, "expected": nil})
	if asset.Code != 202 {
		t.Fatalf("asset: %d %s", asset.Code, asset.Body.String())
	}
	forged := call("POST", "/events", syncEvent{Topic: "central.maintenance_recurrence.linked", Payload: `{"sourceId":"a","successorId":"b"}`})
	if forged.Code != 403 {
		t.Fatalf("forged recurrence link: %d %s", forged.Code, forged.Body.String())
	}
	path := "/api/v1/maintenance?assetId=a1"
	created := call("POST", path, map[string]any{"title": "Regelmäßig", "dueAt": 1900000000, "recurrenceDays": 7})
	var source centralMaintenanceOrder
	if created.Code != 201 || json.Unmarshal(created.Body.Bytes(), &source) != nil {
		t.Fatal(created.Body.String())
	}
	status := func(id, value string) {
		r := call("PATCH", path+"&id="+id, map[string]string{"status": value})
		if r.Code != 200 {
			t.Fatalf("%s: %d %s", value, r.Code, r.Body.String())
		}
	}
	count := func(want int) {
		r := call("GET", path, nil)
		var orders []centralMaintenanceOrder
		if r.Code != 200 || json.Unmarshal(r.Body.Bytes(), &orders) != nil || len(orders) != want {
			t.Fatalf("count: %d %s", r.Code, r.Body.String())
		}
	}
	status(source.ID, "completed")
	count(2)
	linksResp := call("GET", "/api/v1/maintenance-recurrence-link", nil)
	var links []map[string]string
	if linksResp.Code != 200 || json.Unmarshal(linksResp.Body.Bytes(), &links) != nil || len(links) != 1 || links[0]["sourceId"] != source.ID {
		t.Fatalf("links: %d %s", linksResp.Code, linksResp.Body.String())
	}
	// Desktop snapshots must describe the same event cursor and exclude pending children.
	denied := call("GET", "/api/v1/desktop-maintenance-recurrence-snapshot", nil)
	if denied.Code != 403 {
		t.Fatalf("snapshot without event-read permission: %d", denied.Code)
	}
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["facility.manage","sync.events.read"]}}`)
	snapshot := call("GET", "/api/v1/desktop-maintenance-recurrence-snapshot", nil)
	var desktop struct {
		Cursor int64               `json:"cursor"`
		Links  []map[string]string `json:"links"`
	}
	if snapshot.Code != 200 || json.Unmarshal(snapshot.Body.Bytes(), &desktop) != nil || desktop.Cursor <= 0 || len(desktop.Links) != 1 || desktop.Links[0]["sourceId"] != source.ID || desktop.Links[0]["successorId"] != links[0]["successorId"] {
		t.Fatalf("desktop recurrence snapshot: %d %s", snapshot.Code, snapshot.Body.String())
	}
	link := map[string]string{"clientId": "linux", "operationId": "link-1", "sourceId": source.ID, "successorId": links[0]["successorId"]}
	for i := 0; i < 2; i++ {
		r := call("POST", "/api/v1/maintenance-recurrence-link", link)
		if r.Code != 202 {
			t.Fatalf("link retry: %d %s", r.Code, r.Body.String())
		}
	}
	bad := map[string]string{"clientId": "linux", "operationId": "cycle", "sourceId": links[0]["successorId"], "successorId": source.ID}
	if r := call("POST", "/api/v1/maintenance-recurrence-link", bad); r.Code != 409 {
		t.Fatalf("cycle accepted: %d %s", r.Code, r.Body.String())
	}
	bad["operationId"] = "link-1"
	if r := call("POST", "/api/v1/maintenance-recurrence-link", bad); r.Code != 409 {
		t.Fatalf("reused identity: %d %s", r.Code, r.Body.String())
	}

	status(source.ID, "open")
	status(source.ID, "completed")
	count(2)
	store.close()
	store, err = openCentralStore(database)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	status(source.ID, "open")
	status(source.ID, "completed")
	count(2)
	// Simulate the adjacent historical status/create pair written before links.
	legacy := call("POST", path, map[string]any{"title": "Alt", "dueAt": 1900100000, "recurrenceDays": 3})
	var old centralMaintenanceOrder
	if legacy.Code != 201 || json.Unmarshal(legacy.Body.Bytes(), &old) != nil {
		t.Fatal(legacy.Body.String())
	}
	successor := old
	successor.ID = "legacy-successor"
	successor.DueAt += 3 * 86400
	old.Status = "completed"
	a, _ := json.Marshal(old)
	b, _ := json.Marshal(successor)
	at := "2025-01-01T00:00:00Z"
	if err := persistRelatedEvents(journal, []syncEvent{{Topic: "facility.maintenance.status.changed", Payload: string(a), At: at}, {Topic: "facility.maintenance.created", Payload: string(b), At: at}}); err != nil {
		t.Fatal(err)
	}
	store.close()
	store, err = openCentralStore(database)
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	handler = syncHandler(journal, "legacy")
	status(old.ID, "open")
	status(old.ID, "completed")
	count(4)
	secondAsset := call("POST", "/api/v1/facility-asset-sync", map[string]any{"clientId": "linux", "operationId": "asset-2", "action": "create", "facilityAsset": map[string]any{"id": "a2", "name": "Zweite Anlage", "category": "IT", "location": "EG", "status": "operational", "active": true}, "expected": nil})
	if secondAsset.Code != 202 {
		t.Fatalf("second asset: %d %s", secondAsset.Code, secondAsset.Body.String())
	}
	moved := successor
	moved.AssetID = "a2"
	changed := call("POST", "/api/v1/maintenance-sync", map[string]any{"clientId": "linux", "operationId": "move-successor", "action": "update", "maintenanceOrder": moved, "expected": successor})
	if changed.Code != 202 {
		t.Fatalf("move successor: %d %s", changed.Code, changed.Body.String())
	}
	replay := call("POST", "/api/v1/maintenance-recurrence-link", map[string]string{"clientId": "linux", "operationId": "historic-link", "sourceId": old.ID, "successorId": successor.ID})
	if replay.Code != 202 {
		t.Fatalf("historical link: %d %s", replay.Code, replay.Body.String())
	}
}

func TestCentralTaskCreateWithProjectAtomic(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"manager":{"employeeId":9,"permissions":["tasks.manage","projects.manage","sync.events.read"]},"taskonly":{"employeeId":8,"permissions":["tasks.manage"]}}`)
	dir := t.TempDir()
	store, err := openCentralStore(filepath.Join(dir, "central.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	journal := filepath.Join(dir, "events.ndjson")
	handler := syncHandler(journal, "legacy")
	call := func(token, method, path, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	response := call("manager", "POST", "/api/v1/projects", `{"name":"Projekt","department":"IT","status":"planned","ownerId":9}`)
	var project centralProject
	if response.Code != 201 || json.Unmarshal(response.Body.Bytes(), &project) != nil {
		t.Fatalf("project: %d %s", response.Code, response.Body.String())
	}
	body := fmt.Sprintf(`{"title":"Direkt","department":"IT","priority":"Mittel","assigneeId":9,"projectId":%q}`, project.ID)
	if r := call("taskonly", "POST", "/api/v1/tasks", body); r.Code != 403 {
		t.Fatalf("permission: %d %s", r.Code, r.Body.String())
	}
	if r := call("manager", "POST", "/api/v1/tasks", `{"title":"Falsch","department":"IT","priority":"Mittel","projectId":"missing"}`); r.Code != 409 {
		t.Fatalf("unknown project: %d %s", r.Code, r.Body.String())
	}
	response = call("manager", "POST", "/api/v1/tasks", body)
	var task centralTask
	if response.Code != 201 || json.Unmarshal(response.Body.Bytes(), &task) != nil {
		t.Fatalf("task: %d %s", response.Code, response.Body.String())
	}
	update := fmt.Sprintf(`{"title":"Geändert","department":"IT","priority":"Hoch","assigneeId":9,"expectedProjectId":%q,"projectId":""}`, project.ID)
	if r := call("taskonly", "PUT", "/api/v1/tasks/"+task.ID, update); r.Code != 403 {
		t.Fatalf("update permissions: %d %s", r.Code, r.Body.String())
	}
	if r := call("manager", "PUT", "/api/v1/tasks/"+task.ID, update); r.Code != 200 {
		t.Fatalf("update: %d %s", r.Code, r.Body.String())
	}
	if r := call("manager", "PUT", "/api/v1/tasks/"+task.ID, update); r.Code != 409 {
		t.Fatalf("stale update: %d %s", r.Code, r.Body.String())
	}
	restore := fmt.Sprintf(`{"title":"Geändert","department":"IT","priority":"Hoch","assigneeId":9,"expectedProjectId":"","projectId":%q}`, project.ID)
	if r := call("manager", "PUT", "/api/v1/tasks/"+task.ID, restore); r.Code != 200 {
		t.Fatalf("restore: %d %s", r.Code, r.Body.String())
	}
	response = call("manager", "POST", "/api/v1/projects", `{"name":"Zweites","department":"IT","status":"planned","ownerId":9}`)
	var second centralProject
	if response.Code != 201 || json.Unmarshal(response.Body.Bytes(), &second) != nil {
		t.Fatalf("second project: %d %s", response.Code, response.Body.String())
	}
	add := fmt.Sprintf(`{"clientId":"test","operationId":"add-second","projectId":%q,"taskId":%q,"present":true,"expected":false}`, second.ID, task.ID)
	if r := call("manager", "POST", "/api/v1/project-assignment-sync", add); r.Code != 202 {
		t.Fatalf("second edge: %d %s", r.Code, r.Body.String())
	}
	if r := call("manager", "PUT", "/api/v1/tasks/"+task.ID, update); r.Code != 409 {
		t.Fatalf("multiple project overwrite: %d %s", r.Code, r.Body.String())
	}
	if r := call("manager", "PUT", "/api/v1/tasks/"+task.ID, `{"title":"Mehrere behalten","department":"IT","priority":"Hoch","assigneeId":9}`); r.Code != 200 {
		t.Fatalf("ordinary update: %d %s", r.Code, r.Body.String())
	}
	response = call("manager", "GET", "/api/v1/project-assignment-sync", "")
	var both []map[string]string
	if response.Code != 200 || json.Unmarshal(response.Body.Bytes(), &both) != nil || len(both) != 2 {
		t.Fatalf("ordinary edit lost project assignments: %d %s", response.Code, response.Body.String())
	}
	if r := call("manager", "PUT", "/api/v1/tasks/"+task.ID, `{"title":"Unvollständig","department":"IT","priority":"Hoch","assigneeId":9,"projectId":""}`); r.Code != 400 {
		t.Fatalf("partial assignment accepted: %d %s", r.Code, r.Body.String())
	}
	remove := fmt.Sprintf(`{"clientId":"test","operationId":"remove-second","projectId":%q,"taskId":%q,"present":false,"expected":true}`, second.ID, task.ID)
	if r := call("manager", "POST", "/api/v1/project-assignment-sync", remove); r.Code != 202 {
		t.Fatalf("remove second: %d %s", r.Code, r.Body.String())
	}
	handler = syncHandler(journal, "legacy")
	response = call("manager", "GET", "/api/v1/project-assignment-sync", "")
	var edges []map[string]string
	if response.Code != 200 || json.Unmarshal(response.Body.Bytes(), &edges) != nil || len(edges) != 1 || edges[0]["taskId"] != task.ID || edges[0]["projectId"] != project.ID {
		t.Fatalf("restored edge: %d %s", response.Code, response.Body.String())
	}
	feedResponse := call("manager", "GET", "/api/v1/sync/changes?desktop=1&cursor=0", "")
	var feed struct {
		Events []syncEvent `json:"events"`
	}
	if feedResponse.Code != 200 || json.Unmarshal(feedResponse.Body.Bytes(), &feed) != nil {
		t.Fatalf("desktop feed: %d %s", feedResponse.Code, feedResponse.Body.String())
	}
	var sequence []string
	for _, event := range feed.Events {
		if event.Topic == "task.created" || event.Topic == "task.updated" {
			var value centralTask
			if json.Unmarshal([]byte(event.Payload), &value) == nil && value.ID == task.ID {
				sequence = append(sequence, event.Topic)
			}
		}
		if strings.HasPrefix(event.Topic, "central.project_assignment.") {
			var edge struct {
				ProjectID string `json:"projectId"`
				TaskID    string `json:"taskId"`
			}
			if json.Unmarshal([]byte(event.Payload), &edge) == nil && edge.TaskID == task.ID && edge.ProjectID == project.ID {
				sequence = append(sequence, event.Topic)
			}
		}
	}
	want := "task.created,central.project_assignment.added,task.updated,central.project_assignment.removed,task.updated,central.project_assignment.added,task.updated"
	if strings.Join(sequence, ",") != want {
		t.Fatalf("desktop event order: %v", sequence)
	}
	if r := call("manager", "POST", "/api/v1/projects/"+project.ID+"/archive", "{}"); r.Code != 204 {
		t.Fatalf("archive: %d %s", r.Code, r.Body.String())
	}
	if r := call("manager", "POST", "/api/v1/tasks", body); r.Code != 409 {
		t.Fatalf("archived project: %d %s", r.Code, r.Body.String())
	}
	response = call("manager", "GET", "/api/v1/tasks", "")
	var tasks []centralTask
	if response.Code != 200 || json.Unmarshal(response.Body.Bytes(), &tasks) != nil || len(tasks) != 1 {
		t.Fatalf("unexpected tasks: %d %s", response.Code, response.Body.String())
	}
}
