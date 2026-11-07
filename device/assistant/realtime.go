package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

var errReturnToAlice = errors.New("возврат к Алисе")

func realtimeURL(base, model string) (string, error) {
	u, err := url.Parse(strings.TrimRight(base, "/"))
	if err != nil || u.Host == "" {
		return "", errors.New("не задан адрес Realtime API")
	}
	switch u.Scheme {
	case "https":
		u.Scheme = "wss"
	case "http":
		u.Scheme = "ws"
	case "ws", "wss":
	default:
		return "", errors.New("неверная схема Realtime API")
	}
	if !strings.HasSuffix(u.Path, "/realtime") {
		u.Path += "/realtime"
	}
	q := u.Query()
	q.Set("model", model)
	u.RawQuery = q.Encode()
	return u.String(), nil
}

// One response uses native Realtime audio output, never a text-model TTS call.
// Previous turns are replayed as text when reconnecting; no open idle API session.
// Local VAD commits complete audio turns. Playback is streamed as deltas arrive.
func realtimeTurn(ctx context.Context, c LLM, history []message, pcm []byte, onAudio func([]byte) error) (answer, transcript string, err error) {
	endpoint, err := realtimeURL(c.BaseURL, c.Model)
	if err != nil {
		return "", "", err
	}
	if c.Model == "" {
		return "", "", errors.New("не задана модель")
	}
	hc, err := client(c.Proxy, 0)
	if err != nil {
		return "", "", err
	}
	conn, resp, err := websocket.Dial(ctx, endpoint, &websocket.DialOptions{HTTPClient: hc, HTTPHeader: http.Header{"Authorization": []string{"Bearer " + c.APIKey}}})
	if err != nil {
		if resp != nil {
			return "", "", fmt.Errorf("API ответил %d: Realtime: %w", resp.StatusCode, err)
		}
		return "", "", err
	}
	defer conn.CloseNow()
	conn.SetReadLimit(2 << 20)
	send := func(v any) error { return wsjson.Write(ctx, conn, v) }
	voice := c.Voice
	if voice == "" {
		voice = "marin"
	}
	session := map[string]any{"type": "realtime", "instructions": c.SystemPrompt + " Если пользователь просит вернуть Алису или выключить ассистента, вызови return_to_alice.",
		"tools": []any{map[string]any{"type": "function", "name": "return_to_alice", "description": "Выключить стороннего ассистента и вернуть управление Алисе по просьбе пользователя.", "parameters": map[string]any{"type": "object", "properties": map[string]any{}, "additionalProperties": false}}}, "output_modalities": []string{"audio"}, "audio": map[string]any{
			"input":  map[string]any{"format": map[string]any{"type": "audio/pcm", "rate": 24000}, "turn_detection": nil, "noise_reduction": map[string]any{"type": "far_field"}},
			"output": map[string]any{"format": map[string]any{"type": "audio/pcm", "rate": 24000}, "voice": voice},
		}}
	if c.MaxTokens > 0 {
		session["max_output_tokens"] = c.MaxTokens
	}
	wantTranscript := len(pcm) > 0 && keyID(c.BaseURL) == "https://api.openai.com/v1"
	if wantTranscript {
		session["audio"].(map[string]any)["input"].(map[string]any)["transcription"] = map[string]any{"model": "gpt-4o-mini-transcribe", "language": "ru"}
	}
	if err = send(map[string]any{"type": "session.update", "session": session}); err != nil {
		return "", "", err
	}
	var ready bool
	for !ready {
		var ev struct {
			Type  string `json:"type"`
			Error struct {
				Message string `json:"message"`
			} `json:"error"`
		}
		if err = wsjson.Read(ctx, conn, &ev); err != nil {
			return "", "", err
		}
		if ev.Type == "error" {
			return "", "", errors.New("Realtime: " + ev.Error.Message)
		}
		ready = ev.Type == "session.updated"
	}
	for _, m := range history {
		if m.Role == "system" {
			continue
		}
		contentType := "input_text"
		if m.Role == "assistant" {
			contentType = "output_text"
		}
		if len(pcm) > 0 && m.Role == "user" && m.Content == "" {
			continue
		}
		if err = send(map[string]any{"type": "conversation.item.create", "item": map[string]any{"type": "message", "role": m.Role, "content": []any{map[string]any{"type": contentType, "text": m.Content}}}}); err != nil {
			return "", "", err
		}
	}
	if len(pcm) > 0 {
		for start := 0; start < len(pcm); start += 4800 {
			end := start + 4800
			if end > len(pcm) {
				end = len(pcm)
			}
			if err = send(map[string]any{"type": "input_audio_buffer.append", "audio": base64.StdEncoding.EncodeToString(pcm[start:end])}); err != nil {
				return "", "", err
			}
		}
		if err = send(map[string]any{"type": "input_audio_buffer.commit"}); err != nil {
			return "", "", err
		}
	}
	if err = send(map[string]any{"type": "response.create"}); err != nil {
		return "", "", err
	}
	audioBytes := 0
	responseDone := false
	readCtx := ctx
	for {
		var ev struct {
			Type       string `json:"type"`
			Delta      string `json:"delta"`
			Transcript string `json:"transcript"`
			Error      struct {
				Message string `json:"message"`
			} `json:"error"`
			Item struct {
				Type string `json:"type"`
				Name string `json:"name"`
			} `json:"item"`
			Response struct {
				Status        string          `json:"status"`
				StatusDetails json.RawMessage `json:"status_details"`
			} `json:"response"`
		}
		if err = wsjson.Read(readCtx, conn, &ev); err != nil {
			if responseDone && ctx.Err() == nil {
				return strings.TrimSpace(answer), transcript, nil
			}
			return answer, transcript, err
		}
		switch ev.Type {
		case "response.output_item.done":
			if ev.Item.Type == "function_call" && ev.Item.Name == "return_to_alice" {
				return answer, transcript, errReturnToAlice
			}
		case "error":
			return answer, transcript, errors.New("Realtime: " + ev.Error.Message)
		case "response.output_audio.delta", "response.audio.delta":
			data, e := base64.StdEncoding.DecodeString(ev.Delta)
			if e != nil || len(data)%2 != 0 {
				return answer, transcript, errors.New("повреждённый звук Realtime")
			}
			audioBytes += len(data)
			if audioBytes > 24000*2*180 {
				return answer, transcript, errors.New("слишком длинный ответ Realtime")
			}
			if err = onAudio(data); err != nil {
				return answer, transcript, err
			}
		case "response.output_audio_transcript.delta", "response.audio_transcript.delta":
			answer += ev.Delta
		case "response.output_audio_transcript.done", "response.audio_transcript.done":
			if ev.Transcript != "" {
				answer = ev.Transcript
			}
		case "conversation.item.input_audio_transcription.completed":
			transcript = ev.Transcript
			if responseDone {
				return strings.TrimSpace(answer), transcript, nil
			}
		case "response.done":
			if ev.Response.Status != "completed" {
				return answer, transcript, fmt.Errorf("Realtime: ответ %s: %.300s", ev.Response.Status, ev.Response.StatusDetails)
			}
			if audioBytes == 0 {
				return answer, transcript, errors.New("Realtime вернул ответ без звука")
			}
			if !wantTranscript || transcript != "" {
				return strings.TrimSpace(answer), transcript, nil
			}
			responseDone = true
			var cancel context.CancelFunc
			readCtx, cancel = context.WithTimeout(ctx, 3*time.Second)
			defer cancel()
		}
	}
}

const realtimeTimeout = 3 * time.Minute
