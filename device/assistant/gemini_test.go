package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestIsGemini(t *testing.T) {
	for _, u := range []string{
		"https://generativelanguage.googleapis.com/v1beta/openai",
		"https://generativelanguage.googleapis.com/v1beta",
		"https://generativelanguage.googleapis.com",
	} {
		if !isGemini(u) {
			t.Errorf("expected isGemini(%s) to be true", u)
		}
	}
	if isGemini("https://api.openai.com/v1") {
		t.Error("expected isGemini to be false for openai")
	}
}

func TestChatGeminiNativeProtocol(t *testing.T) {
	var receivedPath string
	var receivedKey string
	var receivedBody map[string]any

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		receivedPath = r.URL.Path
		receivedKey = r.Header.Get("x-goog-api-key")
		json.NewDecoder(r.Body).Decode(&receivedBody)

		resp := map[string]any{
			"candidates": []map[string]any{
				{
					"content": map[string]any{
						"parts": []map[string]any{
							{"text": "reasoning step", "thought": true},
							{"text": "Два плюс **два** будет 4."},
						},
					},
				},
			},
		}
		json.NewEncoder(w).Encode(resp)
	}))
	defer srv.Close()

	c := LLM{
		BaseURL:      srv.URL + "/gemini/v1beta/openai",
		APIKey:       "test-gemini-key",
		Model:        "gemini-3.0-flash",
		SystemPrompt: "Ты эрудированный помощник",
		MaxTokens:    500,
	}

	ans, err := chat(context.Background(), c, []message{
		{"system", c.SystemPrompt},
		{"user", "Сколько будет два плюс два?"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if !strings.HasSuffix(receivedPath, "/models/gemini-3.0-flash:generateContent") {
		t.Errorf("unexpected endpoint: %s", receivedPath)
	}
	if receivedKey != "test-gemini-key" {
		t.Errorf("unexpected key: %s", receivedKey)
	}
	if ans != "Два плюс два будет 4." {
		t.Errorf("unexpected answer: %q", ans)
	}

	// Verify thinking config was sent
	genCfg, ok := receivedBody["generationConfig"].(map[string]any)
	if !ok {
		t.Fatal("missing generationConfig")
	}
	thinkCfg, ok := genCfg["thinkingConfig"].(map[string]any)
	if !ok {
		t.Fatal("missing thinkingConfig")
	}
	if thinkCfg["thinkingLevel"] != "MINIMAL" {
		t.Errorf("expected thinkingLevel MINIMAL, got %v", thinkCfg["thinkingLevel"])
	}

	// Verify gemini-2.5-flash sends thinkingBudget: 0
	c25 := c
	c25.Model = "gemini-2.5-flash"
	_, err = chat(context.Background(), c25, []message{{"user", "ping"}})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	genCfg25 := receivedBody["generationConfig"].(map[string]any)
	thinkCfg25 := genCfg25["thinkingConfig"].(map[string]any)
	if budget, ok := thinkCfg25["thinkingBudget"].(float64); !ok || budget != 0 {
		t.Errorf("expected thinkingBudget 0 for 2.5-flash, got %v", thinkCfg25["thinkingBudget"])
	}
}

func TestCheckProxyDirect(t *testing.T) {
	res := checkProxy(context.Background(), "", "")
	if !res.OK || res.Target != "direct" {
		t.Fatalf("expected direct OK, got %+v", res)
	}
}
