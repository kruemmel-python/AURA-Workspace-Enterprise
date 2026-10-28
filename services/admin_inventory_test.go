package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func TestAdminInventoryRequiresPrivilegeAndDoesNotExposeTokens(t *testing.T) {
	old := os.Getenv("AURA_SYNC_IDENTITIES")
	defer os.Setenv("AURA_SYNC_IDENTITIES", old)
	os.Setenv("AURA_SYNC_IDENTITIES", `{"private-administrator-token":{"employeeId":7,"permissions":["admin.manage","audit.read"]},"private-employee-token":{"employeeId":8,"permissions":["rooms.book"]}}`)
	handler := syncHandler(t.TempDir()+"/events.ndjson", "bootstrap-token")
	for _, path := range []string{"/api/v1/admin/accounts", "/api/v1/admin/permissions"} {
		for _, tc := range []struct {
			token string
			want  int
		}{{"", 401}, {"private-employee-token", 403}, {"private-administrator-token", 200}} {
			req := httptest.NewRequest(http.MethodGet, path, nil)
			if tc.token != "" {
				req.Header.Set("Authorization", "Bearer "+tc.token)
			}
			rr := httptest.NewRecorder()
			handler.ServeHTTP(rr, req)
			if rr.Code != tc.want {
				t.Fatalf("%s %s: got %d want %d body %s", path, tc.token, rr.Code, tc.want, rr.Body.String())
			}
			if strings.Contains(rr.Body.String(), "private-administrator-token") || strings.Contains(rr.Body.String(), "private-employee-token") {
				t.Fatalf("sensitive token exposed")
			}
			if rr.Code == 200 {
				var v any
				if err := json.Unmarshal(rr.Body.Bytes(), &v); err != nil {
					t.Fatal(err)
				}
			}
		}
	}
}
