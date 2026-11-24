package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/coder/websocket"
)

// Потоковая озвучка: LLM отвечает с stream:true, готовые предложения сразу уходят в Fish Audio Live
// (WebSocket, msgpack), а PCM из ответа играет aplay, не дожидаясь конца генерации.

func isFishURL(base string) bool {
	u, err := url.Parse(strings.TrimSpace(base))
	return err == nil && strings.EqualFold(u.Hostname(), "api.fish.audio")
}

// streamTTS — потоковый режим включён и возможен: только Fish Audio и обычный текстовый API.
func (c Config) streamTTS() bool {
	return c.TTS.Stream && c.TTS.Provider == "openai" && isFishURL(c.TTS.BaseURL) && c.LLM.Transport != "realtime"
}

// fishScheme меняют только тесты: у локального сервера нет TLS.
var fishScheme = "wss"

// fishLiveURL: https://api.fish.audio/compat/v1 → wss://api.fish.audio/v1/tts/live
func fishLiveURL(base string) (string, error) {
	u, err := url.Parse(strings.TrimSpace(base))
	if err != nil || u.Host == "" {
		return "", errors.New("не задан адрес Fish Audio")
	}
	u.Scheme, u.Path, u.RawQuery = fishScheme, "/v1/tts/live", ""
	return u.String(), nil
}

// fishModel — в compat-API модель называется «fish-audio/s2.1-pro-free», в Live — «s2.1-pro-free».
func fishModel(m string) string { return strings.TrimPrefix(strings.TrimSpace(m), "fish-audio/") }

// fishLive отправляет предложения из texts в Fish и отдаёт PCM (24 кГц, S16_LE) в onAudio по мере прихода.
// Возвращается, когда Fish прислал finish; закрытие texts — конец текста.
func fishLive(ctx context.Context, c TTS, proxy string, texts <-chan string, onAudio func([]byte) error) error {
	endpoint, err := fishLiveURL(c.BaseURL)
	if err != nil {
		return err
	}
	hc, err := client(proxy, 0)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	conn, resp, err := websocket.Dial(ctx, endpoint, &websocket.DialOptions{HTTPClient: hc,
		HTTPHeader: http.Header{"Authorization": []string{"Bearer " + c.APIKey}, "model": []string{fishModel(c.Model)}}})
	if err != nil {
		if resp != nil {
			return fmt.Errorf("Fish Audio ответил %d", resp.StatusCode)
		}
		return err
	}
	defer conn.CloseNow()
	conn.SetReadLimit(16 << 20)
	send := func(m map[string]any) error {
		b, err := msgpackEncode(m)
		if err != nil {
			return err
		}
		return conn.Write(ctx, websocket.MessageBinary, b)
	}
	req := map[string]any{"text": "", "format": "pcm", "sample_rate": 24000, "latency": "normal"}
	if c.Voice != "" {
		req["reference_id"] = c.Voice
	}
	if c.Speed > 0 && c.Speed != 1 {
		req["prosody"] = map[string]any{"speed": min(2.0, max(0.5, c.Speed))}
	}
	if err = send(map[string]any{"event": "start", "request": req}); err != nil {
		return err
	}
	writeErr := make(chan error, 1)
	go func() {
		for t := range texts {
			if err := send(map[string]any{"event": "text", "text": t}); err != nil {
				writeErr <- err
				cancel()
				return
			}
			if err := send(map[string]any{"event": "flush"}); err != nil {
				writeErr <- err
				cancel()
				return
			}
		}
		if err := send(map[string]any{"event": "stop"}); err != nil {
			writeErr <- err
			cancel()
			return
		}
		writeErr <- nil
	}()
	for {
		_, data, err := conn.Read(ctx)
		if err != nil {
			select {
			case werr := <-writeErr:
				if werr != nil {
					err = werr
				}
			default:
			}
			return err
		}
		v, err := msgpackDecode(data)
		ev, _ := v.(map[string]any)
		if err != nil || ev == nil {
			return errors.New("Fish Audio прислал непонятный кадр")
		}
		switch ev["event"] {
		case "audio":
			if audio, ok := ev["audio"].([]byte); ok && len(audio) > 0 {
				if err = onAudio(audio); err != nil {
					return err
				}
			}
		case "finish":
			if r, _ := ev["reason"].(string); r == "error" {
				return errors.New("Fish Audio не смог озвучить текст")
			}
			return nil
		}
	}
}

// streamChat — /chat/completions со stream:true; delta вызывается на каждый кусок текста.
func streamChat(ctx context.Context, c LLM, messages []message, delta func(string) error) error {
	if c.Model == "" {
		return errors.New("не задана модель")
	}
	if strings.TrimSpace(c.BaseURL) == "" {
		return errors.New("не задан адрес API")
	}
	hc, err := client(c.Proxy, 0)
	if err != nil {
		return err
	}
	payload := map[string]any{"model": c.Model, "messages": messages, "max_tokens": c.MaxTokens, "stream": true}
	body, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, "POST", strings.TrimRight(c.BaseURL, "/")+"/chat/completions", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")
	if c.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.APIKey)
	}
	resp, err := hc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
		return apiError(resp.StatusCode, data)
	}
	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 64<<10), 1<<20)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		data, ok := strings.CutPrefix(line, "data:")
		if !ok {
			continue
		}
		if data = strings.TrimSpace(data); data == "[DONE]" {
			return nil
		}
		var ev struct {
			Choices []struct {
				Delta struct {
					Content string `json:"content"`
				} `json:"delta"`
			} `json:"choices"`
			Error any `json:"error"`
		}
		if json.Unmarshal([]byte(data), &ev) != nil {
			continue
		}
		if ev.Error != nil {
			return fmt.Errorf("API: %v", ev.Error)
		}
		if len(ev.Choices) > 0 && ev.Choices[0].Delta.Content != "" {
			if err = delta(ev.Choices[0].Delta.Content); err != nil {
				return err
			}
		}
	}
	return sc.Err()
}

var sentenceEnd = regexp.MustCompile(`[.!?…]+["»”']*\s`)

const maxChunkRunes = 180

// streamTimeout ограничивает весь запрос вместе с озвучкой, как 90 секунд у обычного chat().
var streamTimeout = 90 * time.Second

// speechSplitter режет поток текста на предложения (или куски до maxChunkRunes) и убирает <think>…</think>.
type speechSplitter struct {
	raw  string // весь полученный текст
	sent int    // сколько байт видимого текста уже отдано
}

// push добавляет кусок и возвращает готовые к озвучке предложения.
func (s *speechSplitter) push(delta string) []string {
	s.raw += delta
	vis := stripThinking(s.raw)
	// начало тега <think> может прийти по частям: пока он не дописан, не озвучиваем
	for n := min(len("<think>")-1, len(vis)); n > 0; n-- {
		if strings.HasSuffix(vis, "<think>"[:n]) {
			vis = vis[:len(vis)-n]
			break
		}
	}
	if len(vis) < s.sent {
		return nil
	}
	var out []string
	for {
		rest := vis[s.sent:]
		var end int
		if m := sentenceEnd.FindStringIndex(rest); m != nil {
			end = m[1]
		} else if r := []rune(rest); len(r) >= maxChunkRunes {
			end = len(string(r[:maxChunkRunes]))
			if i := strings.LastIndex(rest[:end], " "); i > 0 {
				end = i + 1
			}
		} else {
			return out
		}
		if t := strings.TrimSpace(rest[:end]); t != "" {
			out = append(out, t)
		}
		s.sent += end
	}
}

// flush — остаток текста после конца ответа.
func (s *speechSplitter) flush() string {
	vis := stripThinking(s.raw)
	if len(vis) < s.sent {
		return ""
	}
	t := strings.TrimSpace(vis[s.sent:])
	s.sent = len(vis)
	return t
}

// fishError — сбой озвучки Fish Audio (а не модели): текст ответа при этом получен целиком.
type fishError struct{ error }

func (e *fishError) Unwrap() error { return e.error }

// streamSpeak: ответ модели читается потоком и сразу озвучивается Fish Audio. Возвращает полный текст ответа.
// Сбой Fish приходит как *fishError вместе с полным текстом ответа: вызывающий озвучит его запасным способом.
func streamSpeak(ctx context.Context, c Config, messages []message, play func([]byte) error) (answer string, played bool, err error) {
	ctx, cancel := context.WithTimeout(ctx, streamTimeout)
	defer cancel()
	texts := make(chan string, 32)
	var split speechSplitter
	fishDone := make(chan error, 1)
	go func() {
		fishDone <- fishLive(ctx, c.TTS, c.LLM.Proxy, texts, func(a []byte) error {
			if err := play(a); err != nil {
				return err
			}
			// The receiver of fishDone reads played only after playback completes.
			played = true
			return nil
		})
	}()
	var fishErr error
	fishAlive := true
	push := func(t string) {
		if !fishAlive {
			return
		}
		select {
		case texts <- t:
		case fishErr = <-fishDone:
			fishAlive = false
		case <-ctx.Done():
		}
	}
	llmErr := streamChat(ctx, c.LLM, messages, func(d string) error {
		for _, t := range split.push(d) {
			push(t)
		}
		return ctx.Err()
	})
	if llmErr == nil {
		if t := split.flush(); t != "" {
			push(t)
		}
	}
	close(texts)
	if fishAlive {
		fishErr = <-fishDone
	}
	answer = strings.TrimSpace(stripThinking(split.raw))
	switch {
	case llmErr != nil:
		return answer, played, llmErr
	case answer == "":
		return "", played, errors.New("модель вернула пустой ответ")
	}
	if err := ctx.Err(); err != nil {
		return answer, played, err
	}
	if fishErr == nil && !played {
		fishErr = errors.New("Fish Audio завершил озвучку без аудио")
	}
	if fishErr != nil {
		return answer, played, &fishError{fishErr}
	}
	return answer, played, nil
}
