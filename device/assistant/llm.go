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
	"net"
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
	if isGemini(c.BaseURL) {
		return chatGemini(ctx, hc, c, messages)
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

func isGemini(baseURL string) bool {
	u := strings.ToLower(baseURL)
	return strings.Contains(u, "generativelanguage.googleapis.com") || strings.Contains(u, "gemini")
}

// chatGemini — прямой нативный протокол Google Gemini v1beta: generateContent с поддержкой thinkingLevel: MINIMAL.
func chatGemini(ctx context.Context, hc *http.Client, c LLM, messages []message) (string, error) {
	base := strings.TrimRight(strings.TrimSpace(c.BaseURL), "/")
	base = strings.TrimSuffix(base, "/openai")
	if !strings.HasSuffix(base, "/v1beta") && !strings.Contains(base, "/v1beta/") {
		base = base + "/v1beta"
	}
	model := strings.TrimPrefix(c.Model, "models/")
	endpoint := fmt.Sprintf("%s/models/%s:generateContent", base, model)

	var contents []map[string]any
	var sysPrompt string = c.SystemPrompt
	for _, m := range messages {
		role := m.Role
		if role == "system" {
			if sysPrompt == "" {
				sysPrompt = m.Content
			}
			continue
		}
		if role == "assistant" {
			role = "model"
		}
		contents = append(contents, map[string]any{
			"role":  role,
			"parts": []map[string]any{{"text": m.Content}},
		})
	}
	if len(contents) == 0 {
		return "", errors.New("нет сообщений для Gemini")
	}

	genConfig := map[string]any{
		"maxOutputTokens": c.MaxTokens,
	}
	if strings.Contains(c.Model, "3.") || strings.Contains(c.Model, "thinking") {
		genConfig["thinkingConfig"] = map[string]any{
			"thinkingLevel": "MINIMAL",
		}
	}
	payload := map[string]any{
		"contents":         contents,
		"generationConfig": genConfig,
	}
	if sysPrompt != "" {
		payload["systemInstruction"] = map[string]any{
			"parts": []map[string]any{{"text": sysPrompt}},
		}
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	var resp *http.Response
	var data []byte
	for attempt := 0; attempt < 2; attempt++ {
		req, err := http.NewRequestWithContext(ctx, "POST", endpoint, bytes.NewReader(body))
		if err != nil {
			return "", err
		}
		req.Header.Set("Content-Type", "application/json")
		if c.APIKey != "" {
			req.Header.Set("x-goog-api-key", c.APIKey)
		}

		resp, err = hc.Do(req)
		if err != nil {
			return "", err
		}
		data, err = io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		resp.Body.Close()
		if err != nil {
			return "", err
		}
		if (resp.StatusCode == 503 || resp.StatusCode == 429) && attempt == 0 {
			select {
			case <-ctx.Done():
				return "", ctx.Err()
			case <-time.After(800 * time.Millisecond):
				continue
			}
		}
		break
	}
	if resp.StatusCode/100 != 2 {
		var e struct {
			Error struct {
				Message string `json:"message"`
				Status  string `json:"status"`
			} `json:"error"`
		}
		json.Unmarshal(data, &e)
		msg := e.Error.Message
		if msg == "" {
			msg = string(data)
		}
		if len(msg) > 300 {
			msg = msg[:300]
		}
		return "", fmt.Errorf("Gemini API ответил %d: %s", resp.StatusCode, msg)
	}

	var r struct {
		Candidates []struct {
			Content struct {
				Parts []struct {
					Text    string `json:"text"`
					Thought bool   `json:"thought"`
				} `json:"parts"`
			} `json:"content"`
		} `json:"candidates"`
	}
	if err = json.Unmarshal(data, &r); err != nil || len(r.Candidates) == 0 {
		return "", errors.New("непонятный ответ Gemini API")
	}

	var texts []string
	for _, p := range r.Candidates[0].Content.Parts {
		if !p.Thought && p.Text != "" {
			texts = append(texts, p.Text)
		}
	}
	raw := strings.Join(texts, " ")
	answer := strings.TrimSpace(stripThinking(raw))
	replacer := strings.NewReplacer("*", "", "#", "", "`", "", "_", " ")
	answer = strings.Join(strings.Fields(replacer.Replace(answer)), " ")
	if answer == "" {
		return "", errors.New("Gemini вернул пустой ответ")
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
		if isGemini(c.BaseURL) && !strings.Contains(c.BaseURL, "/openai") {
			req.Header.Set("x-goog-api-key", c.APIKey)
		}
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

type ProxyCheckResult struct {
	Proxy   string `json:"proxy"`
	OK      bool   `json:"ok"`
	Ms      int64  `json:"ms"`
	Target  string `json:"target,omitempty"`
	Error   string `json:"error,omitempty"`
	Checked int64  `json:"checked"`
}

func checkProxy(ctx context.Context, proxy, targetURL string) ProxyCheckResult {
	proxy = strings.TrimSpace(proxy)
	now := time.Now().Unix()
	if proxy == "" {
		return ProxyCheckResult{Proxy: "", OK: true, Ms: 0, Target: "direct", Checked: now}
	}
	if targetURL == "" {
		targetURL = "https://generativelanguage.googleapis.com"
	}

	u, err := url.Parse(proxy)
	if err != nil || u.Host == "" {
		return ProxyCheckResult{Proxy: proxy, OK: false, Error: "неверный адрес прокси", Checked: now}
	}
	hostPort := u.Host
	if !strings.Contains(hostPort, ":") {
		if u.Scheme == "https" {
			hostPort += ":443"
		} else {
			hostPort += ":80"
		}
	}

	// 1. Быстрая проверка доступности самого прокси по TCP
	t0 := time.Now()
	conn, err := net.DialTimeout("tcp", hostPort, 2*time.Second)
	if err != nil {
		return ProxyCheckResult{Proxy: proxy, OK: false, Error: "прокси недоступен (TCP): " + err.Error(), Checked: now}
	}
	conn.Close()
	tcpMs := time.Since(t0).Milliseconds()

	// 2. Проверка туннеля через прокси к целевому сервису (Gemini / OpenAI)
	hc, err := client(proxy, 6*time.Second)
	if err != nil {
		return ProxyCheckResult{Proxy: proxy, OK: false, Error: err.Error(), Checked: now}
	}

	t1 := time.Now()
	req, err := http.NewRequestWithContext(ctx, "HEAD", targetURL, nil)
	if err != nil {
		return ProxyCheckResult{Proxy: proxy, OK: false, Error: err.Error(), Checked: now}
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (yapanel; assistantd)")

	resp, err := hc.Do(req)
	totalMs := time.Since(t1).Milliseconds()
	if err != nil {
		req2, err2 := http.NewRequestWithContext(ctx, "GET", targetURL, nil)
		if err2 == nil {
			req2.Header.Set("User-Agent", "Mozilla/5.0 (yapanel; assistantd)")
			resp2, err3 := hc.Do(req2)
			if err3 == nil {
				resp2.Body.Close()
				resp = resp2
				err = nil
			}
		}
	}
	if err != nil {
		return ProxyCheckResult{Proxy: proxy, OK: false, Ms: tcpMs, Error: "ошибка соединения через прокси: " + err.Error(), Checked: now}
	}
	defer resp.Body.Close()

	if resp.StatusCode == 403 {
		return ProxyCheckResult{Proxy: proxy, OK: false, Ms: totalMs, Target: targetURL, Error: "HTTP 403 (геоблокировка не обойдена)", Checked: now}
	}

	return ProxyCheckResult{
		Proxy:   proxy,
		OK:      true,
		Ms:      totalMs,
		Target:  targetURL,
		Checked: now,
	}
}

func printProxyCheck(cfgPath, overrides string) {
	c, _ := loadConfig(cfgPath)
	targetURL := c.LLM.BaseURL
	proxy := c.LLM.Proxy
	if overrides != "-" {
		if b, err := os.ReadFile(overrides); err == nil {
			var o LLM
			if json.Unmarshal(b, &o) == nil {
				if o.BaseURL != "" {
					targetURL = o.BaseURL
				}
				if o.Proxy != "" {
					proxy = o.Proxy
				}
			}
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	res := checkProxy(ctx, proxy, targetURL)
	json.NewEncoder(os.Stdout).Encode(res)
}
