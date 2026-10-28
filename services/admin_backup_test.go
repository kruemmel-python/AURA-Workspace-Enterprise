package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestAdminBackupRequiresServerAndConfiguredLocation(t *testing.T) {
	_ = os.Setenv("AURA_CENTRAL_BACKUP_DIR", "")
	store, err := openCentralStore(filepath.Join(t.TempDir(), "server.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.close()
	name, err := store.snapshot(filepath.Join(t.TempDir(), "backups"))
	if err != nil {
		t.Fatal(err)
	}
	if name == "" {
		t.Fatal("missing backup name")
	}
}
