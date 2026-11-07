package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func writeConfigFile(t *testing.T, path, model string, mtime time.Time) {
	t.Helper()
	body := []byte(`{"llm":{"base_url":"https://api.example/v1","model":"` + model + `"}}`)
	if err := os.WriteFile(path, body, 0600); err != nil {
		t.Fatal(err)
	}
	if !mtime.IsZero() {
		if err := os.Chtimes(path, mtime, mtime); err != nil {
			t.Fatal(err)
		}
	}
}

func newTestDaemon(dir string) *Daemon {
	return &Daemon{cfgPath: filepath.Join(dir, "config.json"), statusPath: filepath.Join(dir, "status.json"),
		cmdPath: filepath.Join(dir, "cmd.json")}
}

func TestReloadAppliesModelChangeWithSameModTime(t *testing.T) {
	dir := t.TempDir()
	stamp := time.Unix(1700000000, 0)
	writeConfigFile(t, filepath.Join(dir, "config.json"), "old-model", stamp)
	d := newTestDaemon(dir)
	d.reload()
	if got := d.cfg.LLM.Model; got != "old-model" {
		t.Fatalf("стартовый конфиг: %q", got)
	}
	// Панель могла записать файл с тем же временем изменения — модель всё равно применяется.
	writeConfigFile(t, filepath.Join(dir, "config.json"), "new-model", stamp)
	d.reload()
	if got := d.cfg.LLM.Model; got != "new-model" {
		t.Fatalf("после смены модели: %q (mtime не изменился)", got)
	}
}

func TestReloadResetsConversationOnlyWhenModelChanges(t *testing.T) {
	dir := t.TempDir()
	writeConfigFile(t, filepath.Join(dir, "config.json"), "model-a", time.Time{})
	d := newTestDaemon(dir)
	d.reload()
	d.convo = []message{{Role: "user", Content: "привет"}}
	d.reload() // тот же конфиг
	if len(d.convo) != 1 {
		t.Fatal("контекст сброшен без изменений конфига")
	}
	writeConfigFile(t, filepath.Join(dir, "config.json"), "model-b", time.Time{})
	d.reload()
	if d.convo != nil {
		t.Fatal("при смене модели контекст должен очищаться")
	}
	if got := d.cfg.LLM.Model; got != "model-b" {
		t.Fatalf("модель: %q", got)
	}
}
