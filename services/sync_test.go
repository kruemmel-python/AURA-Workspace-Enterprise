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
)

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
	if response := post("manager-token", "/api/v1/leave-requests/"+leave.ID+"/decision", `{"decision":"approved"}`); response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"status":"approved"`) {
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
	if response := request("/api/v1/tasks/"+createdTask.ID+"/status", `{"status":"done"}`); response.Code != http.StatusConflict {
		t.Fatalf("invalid task transition: %d", response.Code)
	}
	if response := request("/api/v1/tasks/"+createdTask.ID+"/status", `{"status":"in_progress"}`); response.Code != http.StatusOK {
		t.Fatalf("valid task transition: %d", response.Code)
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
	handler := syncHandler(filepath.Join(t.TempDir(), "task-approvals.ndjson"), "token")
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
	invalidDecision := `{"id":"client-a:approval-1","taskId":"client-a:41","requestedBy":"client-a:9","approverId":"client-a:10","status":"approved","decidedBy":"client-a:9","active":true}`
	if status := post("manager", "task.approval.decided", invalidDecision); status != http.StatusConflict {
		t.Fatalf("decision authority: %d", status)
	}
	decision := `{"id":"client-a:approval-1","taskId":"client-a:41","requestedBy":"client-a:9","approverId":"client-a:10","status":"approved","decidedBy":"client-a:10","active":true}`
	if status := post("manager", "task.approval.decided", decision); status != http.StatusAccepted {
		t.Fatalf("decision: %d", status)
	}
}

func TestCommentEventsRequireAnExistingSubjectAndAreIdempotent(t *testing.T) {
	t.Setenv("AURA_SYNC_IDENTITIES", `{"writer":{"employeeId":9,"permissions":["tasks.manage","sync.events.write"]}}`)
	handler := syncHandler(filepath.Join(t.TempDir(), "comments.ndjson"), "token")
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
	if status := post("comment.created", comment); status != http.StatusAccepted {
		t.Fatalf("comment: %d", status)
	}
	if status := post("comment.created", comment); status != http.StatusAccepted {
		t.Fatalf("idempotency: %d", status)
	}
}
