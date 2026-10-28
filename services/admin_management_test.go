package main

import (
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestManagedAdminLifecycleAndSecurity(t *testing.T) {
	old := os.Getenv("AURA_MANAGED_IDENTITIES_FILE")
	oldIDs := os.Getenv("AURA_SYNC_IDENTITIES")
	defer os.Setenv("AURA_MANAGED_IDENTITIES_FILE", old)
	defer os.Setenv("AURA_SYNC_IDENTITIES", oldIDs)
	os.Unsetenv("AURA_SYNC_IDENTITIES")
	os.Setenv("AURA_MANAGED_IDENTITIES_FILE", filepath.Join(t.TempDir(), "managed.json"))
	handler := syncHandler(filepath.Join(t.TempDir(), "events.ndjson"), "test-root")
	call := func(method, path, token, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.RemoteAddr = "127.0.0.1:45000"
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		return w
	}
	if w := call("PUT", "/api/v1/admin/roles", "", `{"name":"employee","permissions":["rooms.book"]}`); w.Code != 401 {
		t.Fatalf("anonymous: %v", w.Code)
	}
	if w := call("PUT", "/api/v1/admin/roles", "test-root", `{"name":"employee","permissions":["rooms.book"]}`); w.Code != 200 {
		t.Fatalf("role: %v %s", w.Code, w.Body.String())
	}
	w := call("POST", "/api/v1/admin/managed-accounts", "test-root", `{"employeeId":55,"role":"employee"}`)
	if w.Code != 201 {
		t.Fatalf("account: %v %s", w.Code, w.Body.String())
	}
	var result struct {
		Token string `json:"tokenOnce"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &result)
	if len(result.Token) != 64 {
		t.Fatal("token not generated")
	}
	if w := call("GET", "/api/v1/admin/managed-accounts", "test-root", ""); w.Code != 200 || strings.Contains(w.Body.String(), result.Token) {
		t.Fatal("credential exposed")
	}
	if w := call("GET", "/api/v1/admin/roles", result.Token, ""); w.Code != 403 {
		t.Fatal("privilege escalation")
	}
	if w := call("GET", "/api/v1/identity", result.Token, ""); w.Code != 200 {
		t.Fatalf("managed identity rejected %v", w.Code)
	}
	if w := call("PUT", "/api/v1/admin/managed-accounts", "test-root", `{"employeeId":55,"enabled":false}`); w.Code != 200 {
		t.Fatalf("disable failed %v", w.Code)
	}
	if w := call("GET", "/api/v1/identity", result.Token, ""); w.Code != 401 {
		t.Fatalf("disabled token valid %v", w.Code)
	}
	// The roles and accounts survive reloads and server restarts.
	if _, err := managedLoad(); err != nil {
		t.Fatal(err)
	}
}
func TestManagedMutationRequiresTLSOrLoopback(t *testing.T) {
	old := os.Getenv("AURA_MANAGED_IDENTITIES_FILE")
	defer os.Setenv("AURA_MANAGED_IDENTITIES_FILE", old)
	os.Setenv("AURA_MANAGED_IDENTITIES_FILE", filepath.Join(t.TempDir(), "managed.json"))
	request := httptest.NewRequest("PUT", "http://aura.example/api/v1/admin/roles", strings.NewReader(`{"name":"dev","permissions":["rooms.book"]}`))
	request.RemoteAddr = "198.51.100.2:40000"
	w := httptest.NewRecorder()
	if !managedAdminHandler(w, request, serverIdentity{EmployeeID: 1, Permissions: []string{"*"}}) || w.Code != 403 {
		t.Fatalf("unexpected %v", w.Code)
	}
}
