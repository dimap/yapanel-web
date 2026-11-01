package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"time"
)

type message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

func client(proxy string, timeout time.Duration) (*http.Client, error) {
	return clientWithCA(proxy, timeout, caFile)
}

func clientWithCA(proxy string, timeout time.Duration, firmwareCA string) (*http.Client, error) {
	t := http.DefaultTransport.(*http.Transport).Clone()
	// Android stores the firmware CA bundle outside Go's standard Linux paths.
	// Add it to system roots, retaining TLS chain and hostname verification.
	if pem, err := os.ReadFile(firmwareCA); err == nil {
		roots, _ := x509.SystemCertPool()
		if roots == nil {
			roots = x509.NewCertPool()
		}
		if roots.AppendCertsFromPEM(pem) {
			if t.TLSClientConfig == nil {
				t.TLSClientConfig = &tls.Config{}
			} else {
				t.TLSClientConfig = t.TLSClientConfig.Clone()
			}
			t.TLSClientConfig.RootCAs = roots
		}
	}
	t.Proxy = nil
	if proxy = strings.TrimSpace(proxy); proxy != "" {
		u, err := url.Parse(proxy)
		if err != nil || u.Host == "" {
			return nil, errors.New("неверный адрес прокси")
		}
		t.Proxy = http.ProxyURL(u) // net/http умеет http, https и socks5
	}
	return &http.Client{Transport: t, Timeout: timeout}, nil
}

// post отправляет JSON и возвращает тело ответа; ошибки API — их собственным текстом.
func post(ctx context.Context, hc *http.Client, base, path, key string, payload any, limit int64) ([]byte, string, error) {
	if strings.TrimSpace(base) == "" {
		return nil, "", errors.New("не задан адрес API")
	}
	body, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, "POST", strings.TrimRight(base, "/")+path, bytes.NewReader(body))
	if err != nil {
		return nil, "", err
	}
	req.Header.Set("Content-Type", "application/json")
	if key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	resp, err := hc.Do(req)
	if err != nil {
		return nil, "", err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, limit))
	if err != nil {
		return nil, "", err
	}
	if resp.StatusCode/100 != 2 {
		var e struct {
			Error any `json:"error"`
		}
		msg := strings.TrimSpace(string(data))
		if json.Unmarshal(data, &e) == nil && e.Error != nil {
			if m, ok := e.Error.(map[string]any); ok && m["message"] != nil {
				msg = fmt.Sprint(m["message"])
			} else {
				msg = fmt.Sprint(e.Error)
			}
		}
		if len(msg) > 300 {
			msg = msg[:300]
		}
		return nil, "", fmt.Errorf("API ответил %d: %s", resp.StatusCode, msg)
	}
	return data, resp.Header.Get("Content-Type"), nil
}

// chat — OpenAI-совместимый /chat/completions: его понимают почти все провайдеры и локальные серверы.
func chat(ctx context.Context, c LLM, messages []message) (string, error) {
	if c.Model == "" {
		return "", errors.New("не задана модель")
	}
	hc, err := client(c.Proxy, 90*time.Second)
	if err != nil {
		return "", err
	}
	data, _, err := post(ctx, hc, c.BaseURL, "/chat/completions", c.APIKey,
		map[string]any{"model": c.Model, "messages": messages, "max_tokens": c.MaxTokens, "stream": false}, 1<<20)
	if err != nil {
		return "", err
	}
	var r struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err = json.Unmarshal(data, &r); err != nil || len(r.Choices) == 0 {
		return "", errors.New("непонятный ответ API")
	}
	answer := strings.TrimSpace(stripThinking(r.Choices[0].Message.Content))
	if answer == "" {
		return "", errors.New("модель вернула пустой ответ")
	}
	return answer, nil
}

// stripThinking убирает <think>…</think>, который пишут некоторые рассуждающие модели.
func stripThinking(s string) string {
	for {
		i := strings.Index(s, "<think>")
		if i < 0 {
			return s
		}
		j := strings.Index(s[i:], "</think>")
		if j < 0 {
			return s[:i]
		}
		s = s[:i] + s[i+j+len("</think>"):]
	}
}

// speech — OpenAI-совместимый /audio/speech в WAV: его без перекодировки играет aplay колонки.
func speech(ctx context.Context, c TTS, proxy, text string) ([]byte, error) {
	hc, err := client(proxy, 90*time.Second)
	if err != nil {
		return nil, err
	}
	payload := map[string]any{"model": c.Model, "input": text, "voice": c.Voice, "response_format": "wav", "speed": c.Speed}
	data, kind, err := post(ctx, hc, c.BaseURL, "/audio/speech", c.APIKey, payload, 16<<20)
	if err != nil {
		return nil, err
	}
	if len(data) < 44 || string(data[:4]) != "RIFF" {
		return nil, fmt.Errorf("озвучка вернула не WAV (%s)", kind)
	}
	return data, nil
}

// listModels — OpenAI-совместимый GET /models (так же отвечают OpenRouter, Groq, Gemini, Ollama, LM Studio).
func listModels(ctx context.Context, c LLM) ([]string, error) {
	if strings.TrimSpace(c.BaseURL) == "" {
		return nil, errors.New("не задан адрес API")
	}
	hc, err := client(c.Proxy, 30*time.Second)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, "GET", strings.TrimRight(c.BaseURL, "/")+"/models", nil)
	if err != nil {
		return nil, err
	}
	if c.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.APIKey)
	}
	resp, err := hc.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if resp.StatusCode/100 != 2 {
		msg := strings.TrimSpace(string(data))
		if len(msg) > 200 {
			msg = msg[:200]
		}
		return nil, fmt.Errorf("API ответил %d: %s", resp.StatusCode, msg)
	}
	var r struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err = json.Unmarshal(data, &r); err != nil {
		return nil, errors.New("непонятный ответ API")
	}
	ids := make([]string, 0, len(r.Data))
	for _, m := range r.Data {
		if m.ID != "" {
			ids = append(ids, strings.TrimPrefix(m.ID, "models/")) // Gemini отдаёт «models/gemini-…»
		}
	}
	sort.Strings(ids)
	return ids, nil
}

func printModels(cfgPath, overrides string) {
	c, _ := loadConfig(cfgPath)
	if overrides != "-" {
		if b, err := os.ReadFile(overrides); err == nil {
			var o LLM
			if json.Unmarshal(b, &o) == nil {
				if o.BaseURL != "" && keyID(o.BaseURL) != keyID(c.LLM.BaseURL) {
					c.LLM.BaseURL, c.LLM.APIKey = o.BaseURL, c.keyFor(o.BaseURL, "")
				}
				if o.APIKey != "" {
					c.LLM.APIKey = o.APIKey
				}
				c.LLM.Proxy = o.Proxy
			}
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Second)
	defer cancel()
	ids, err := listModels(ctx, c.LLM)
	out := map[string]any{"models": ids}
	if err != nil {
		out = map[string]any{"error": err.Error()}
	}
	json.NewEncoder(os.Stdout).Encode(out)
}
