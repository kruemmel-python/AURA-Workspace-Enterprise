package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
)

// Managed identities and role names are stored in a separate, private JSON
// document. Existing environment-defined credentials remain valid and immutable.
// Only administrator-created accounts are editable by this API.
type managedAccount struct {
	EmployeeID int64  `json:"employeeId"`
	Role       string `json:"role"`
	Enabled    bool   `json:"enabled"`
	Token      string `json:"token"`
}
type managedConfig struct {
	Roles    map[string][]string `json:"roles"`
	Accounts []managedAccount    `json:"accounts"`
}

var managedLock sync.Mutex

func managedPath() string { return os.Getenv("AURA_MANAGED_IDENTITIES_FILE") }
func managedLoad() (managedConfig, error) {
	cfg := managedConfig{Roles: map[string][]string{}, Accounts: []managedAccount{}}
	path := managedPath()
	if path == "" {
		return cfg, nil
	}
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return cfg, nil
	}
	if err != nil {
		return cfg, err
	}
	if err = json.Unmarshal(raw, &cfg); err != nil {
		return cfg, err
	}
	if cfg.Roles == nil {
		cfg.Roles = map[string][]string{}
	}
	if cfg.Accounts == nil {
		cfg.Accounts = []managedAccount{}
	}
	return cfg, nil
}
func managedSave(cfg managedConfig) error {
	path := managedPath()
	if path == "" {
		return errors.New("managed identities file not configured")
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	file, err := os.CreateTemp(dir, ".aura-identity-*")
	if err != nil {
		return err
	}
	tmp := file.Name()
	defer os.Remove(tmp)
	if err = file.Chmod(0600); err != nil {
		file.Close()
		return err
	}
	if _, err = file.Write(raw); err != nil {
		file.Close()
		return err
	}
	if err = file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err = file.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
func managedIdentity(token string) (serverIdentity, bool) {
	managedLock.Lock()
	defer managedLock.Unlock()
	cfg, err := managedLoad()
	if err != nil {
		return serverIdentity{}, false
	}
	for _, v := range cfg.Accounts {
		if v.Enabled && v.Token == token {
			perms, ok := cfg.Roles[v.Role]
			if !ok {
				return serverIdentity{}, false
			}
			return serverIdentity{EmployeeID: v.EmployeeID, Permissions: perms}, true
		}
	}
	return serverIdentity{}, false
}
func validPermissions(p []string) bool {
	if len(p) == 0 || len(p) > 64 {
		return false
	}
	for _, v := range p {
		if v == "" || len(v) > 96 || strings.ContainsAny(v, " \t\n\r") {
			return false
		}
	}
	return true
}
func managedAdminHandler(writer http.ResponseWriter, request *http.Request, identity serverIdentity) bool {
	path := request.URL.Path
	if path != "/api/v1/admin/managed-accounts" && path != "/api/v1/admin/managed-accounts/rotate" && path != "/api/v1/admin/roles" {
		return false
	}
	if !allows(identity, "admin.manage") {
		http.Error(writer, "forbidden", 403)
		return true
	}
	if managedPath() == "" {
		http.Error(writer, "AURA_MANAGED_IDENTITIES_FILE not configured", 503)
		return true
	}
	if request.Method != http.MethodGet && request.Method != http.MethodPost && request.Method != http.MethodPut && request.Method != http.MethodDelete {
		http.Error(writer, "method not allowed", 405)
		return true
	}
	// Credential/role modification requires HTTPS except on loopback-only tests.
	if request.Method != http.MethodGet && request.TLS == nil && !remoteIsLoopback(request.RemoteAddr) {
		http.Error(writer, "TLS required", 403)
		return true
	}
	managedLock.Lock()
	defer managedLock.Unlock()
	cfg, err := managedLoad()
	if err != nil {
		http.Error(writer, "identity database unavailable", 500)
		return true
	}
	writer.Header().Set("Content-Type", "application/json")
	if request.Method == http.MethodGet {
		if path == "/api/v1/admin/roles" {
			_ = json.NewEncoder(writer).Encode(cfg.Roles)
			return true
		}
		records := []map[string]any{}
		for _, a := range cfg.Accounts {
			records = append(records, map[string]any{"employeeId": a.EmployeeID, "role": a.Role, "enabled": a.Enabled})
		}
		_ = json.NewEncoder(writer).Encode(records)
		return true
	}
	defer request.Body.Close()
	var input struct {
		EmployeeID  int64    `json:"employeeId"`
		Role        string   `json:"role"`
		Enabled     *bool    `json:"enabled"`
		Name        string   `json:"name"`
		Permissions []string `json:"permissions"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 65536))
	decoder.DisallowUnknownFields()
	if err = decoder.Decode(&input); err != nil {
		http.Error(writer, "invalid input", 400)
		return true
	}
	if path == "/api/v1/admin/roles" {
		if input.Name == "" || len(input.Name) > 48 || strings.ContainsAny(input.Name, " \r\n\t") {
			http.Error(writer, "invalid role", 400)
			return true
		}
		if request.Method == http.MethodDelete {
			for _, a := range cfg.Accounts {
				if a.Role == input.Name {
					http.Error(writer, "role assigned", 409)
					return true
				}
			}
			if _, ok := cfg.Roles[input.Name]; !ok {
				http.Error(writer, "not found", 404)
				return true
			}
			delete(cfg.Roles, input.Name)
		} else {
			if !validPermissions(input.Permissions) {
				http.Error(writer, "invalid permissions", 400)
				return true
			}
			cfg.Roles[input.Name] = input.Permissions
		}
	} else {
		if input.EmployeeID <= 0 {
			http.Error(writer, "invalid employee", 400)
			return true
		}
		index := -1
		for i, a := range cfg.Accounts {
			if a.EmployeeID == input.EmployeeID {
				index = i
				break
			}
		}
		if path == "/api/v1/admin/managed-accounts/rotate" {
			if request.Method != http.MethodPost {
				http.Error(writer, "method not allowed", 405)
				return true
			}
			if index < 0 {
				http.Error(writer, "not found", 404)
				return true
			}
			if identity.EmployeeID == input.EmployeeID {
				http.Error(writer, "cannot rotate current admin identity", 409)
				return true
			}
			value := make([]byte, 32)
			if _, err = rand.Read(value); err != nil {
				http.Error(writer, "random source unavailable", 500)
				return true
			}
			token := hex.EncodeToString(value)
			cfg.Accounts[index].Token = token
			if err = managedSave(cfg); err != nil {
				http.Error(writer, "save failed", 500)
				return true
			}
			_ = json.NewEncoder(writer).Encode(map[string]any{"employeeId": input.EmployeeID, "tokenOnce": token})
			return true
		}
		if request.Method == http.MethodPost {
			if index >= 0 {
				http.Error(writer, "account exists", 409)
				return true
			}
			if _, ok := cfg.Roles[input.Role]; !ok {
				http.Error(writer, "unknown role", 400)
				return true
			}
			// Existing configured identities cannot be overridden by managed accounts.
			static, err := configuredIdentities()
			if err != nil {
				http.Error(writer, "invalid identity configuration", 500)
				return true
			}
			for _, a := range static {
				if a.EmployeeID == input.EmployeeID {
					http.Error(writer, "identity managed externally", 409)
					return true
				}
			}
			value := make([]byte, 32)
			if _, err = rand.Read(value); err != nil {
				http.Error(writer, "random source unavailable", 500)
				return true
			}
			token := hex.EncodeToString(value)
			cfg.Accounts = append(cfg.Accounts, managedAccount{EmployeeID: input.EmployeeID, Role: input.Role, Enabled: true, Token: token})
			if err = managedSave(cfg); err != nil {
				http.Error(writer, "save failed", 500)
				return true
			}
			writer.WriteHeader(201)
			_ = json.NewEncoder(writer).Encode(map[string]any{"employeeId": input.EmployeeID, "tokenOnce": token})
			return true
		}
		if index < 0 {
			http.Error(writer, "not found", 404)
			return true
		}
		if input.EmployeeID == identity.EmployeeID {
			http.Error(writer, "cannot change current administrator account", 409)
			return true
		}
		if request.Method == http.MethodDelete {
			cfg.Accounts = append(cfg.Accounts[:index], cfg.Accounts[index+1:]...)
		} else {
			if input.Role != "" {
				if _, ok := cfg.Roles[input.Role]; !ok {
					http.Error(writer, "unknown role", 400)
					return true
				}
				cfg.Accounts[index].Role = input.Role
			}
			if input.Enabled != nil {
				cfg.Accounts[index].Enabled = *input.Enabled
			}
		}
	}
	if err = managedSave(cfg); err != nil {
		http.Error(writer, "save failed", 500)
		return true
	}
	_ = json.NewEncoder(writer).Encode(map[string]bool{"ok": true})
	return true
}
func managedAccountsForInventory() []map[string]any {
	managedLock.Lock()
	defer managedLock.Unlock()
	cfg, err := managedLoad()
	if err != nil {
		return nil
	}
	out := []map[string]any{}
	for _, a := range cfg.Accounts {
		out = append(out, map[string]any{"employeeId": a.EmployeeID, "role": a.Role, "enabled": a.Enabled, "permissions": cfg.Roles[a.Role], "source": "managed"})
	}
	sort.Slice(out, func(i, j int) bool { return out[i]["employeeId"].(int64) < out[j]["employeeId"].(int64) })
	return out
}

func remoteIsLoopback(address string) bool {
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		return false
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}
