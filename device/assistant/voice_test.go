package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

func TestMicrophoneFormatAndFeedback(t *testing.T) {
	p := make([]byte, 44)
	copy(p, "WPM1")
	binary.LittleEndian.PutUint32(p[4:], 48000)
	binary.LittleEndian.PutUint16(p[8:], 3)
	binary.LittleEndian.PutUint16(p[10:], 16)
	binary.LittleEndian.PutUint32(p[12:], 4)
	binary.LittleEndian.PutUint32(p[16:], 42)
	for i := 0; i < 4; i++ {
		binary.LittleEndian.PutUint16(p[20+i*6:], 100)
		binary.LittleEndian.PutUint16(p[22+i*6:], 300)
		binary.LittleEndian.PutUint16(p[24+i*6:], 30000)
	}
	out, seq, err := decodeMicrophone(p, 2)
	if err != nil || seq != 42 || len(out) != 4 {
		t.Fatalf("%v %d %d", err, seq, len(out))
	}
	for i := 0; i < 4; i += 2 {
		if got := int16(binary.LittleEndian.Uint16(out[i:])); got != 400 {
			t.Fatalf("feedback mixed into speech: %d", got)
		}
	}
	p[4] = 0
	if _, _, err := decodeMicrophone(p, 1); err == nil {
		t.Fatal("accepted wrong rate")
	}
}
func voiceSamples(value int16, n int) []byte {
	b := make([]byte, n*2)
	for i := 0; i < n; i++ {
		binary.LittleEndian.PutUint16(b[i*2:], uint16(value))
	}
	return b
}
func TestEndpointSilenceAndSpeech(t *testing.T) {
	c := defaults().Microphone
	c.SilenceMS = 300
	var v utteranceDetector
	for i := 0; i < 50; i++ {
		if v.push(voiceSamples(10, 480), c) != nil {
			t.Fatal("silence recognized as speech")
		}
	}
	for i := 0; i < 25; i++ {
		if v.push(voiceSamples(3000, 480), c) != nil {
			t.Fatal("speech ended early")
		}
	}
	var result []byte
	for i := 0; i < 20; i++ {
		if b := v.push(voiceSamples(10, 480), c); b != nil {
			result = b
		}
	}
	if len(result) == 0 || len(result) > 52800 {
		t.Fatalf("no bounded utterance: %d", len(result))
	}
	// A click must not be submitted as a user request.
	v = utteranceDetector{}
	for i := 0; i < 50; i++ {
		v.push(voiceSamples(10, 480), c)
	}
	v.push(voiceSamples(3000, 480), c)
	for i := 0; i < 20; i++ {
		if v.push(voiceSamples(10, 480), c) != nil {
			t.Fatal("click submitted")
		}
	}
}
func TestSTTMultipartAndProvider(t *testing.T) {
	pcm := voiceSamples(12, 2400)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/audio/transcriptions" || r.Header.Get("Authorization") != "Bearer secret" {
			t.Error("wrong endpoint or credentials")
		}
		if err := r.ParseMultipartForm(1 << 20); err != nil {
			t.Error(err)
			w.WriteHeader(400)
			return
		}
		defer r.MultipartForm.RemoveAll()
		if r.FormValue("model") != "local-whisper" || r.FormValue("language") != "ru" {
			t.Error("wrong STT settings")
		}
		f, _, err := r.FormFile("file")
		if err != nil {
			t.Error(err)
			return
		}
		defer f.Close()
		header := make([]byte, 44)
		f.Read(header)
		if string(header[:4]) != "RIFF" || binary.LittleEndian.Uint32(header[24:]) != 24000 {
			t.Error("wrong WAV")
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"text":"  Привет, мир  "}`))
	}))
	defer server.Close()
	text, err := transcribe(context.Background(), STT{BaseURL: server.URL + "/v1", APIKey: "secret", Model: "local-whisper", Language: "ru"}, pcm)
	if err != nil || text != "Привет, мир" {
		t.Fatalf("%q %v", text, err)
	}
	c := defaults()
	c.STT.Provider = "model"
	c.LLM = LLM{BaseURL: "https://api.groq.com/openai/v1", APIKey: "groq-secret", Proxy: "http://proxy:8080"}
	stt, err := c.speechRecognition()
	if err != nil || stt.APIKey != "groq-secret" || stt.Model != "whisper-large-v3-turbo" || stt.Proxy != c.LLM.Proxy {
		t.Fatalf("%+v %v", stt, err)
	}
	c.LLM.BaseURL = "http://unknown/v1"
	if _, err = c.speechRecognition(); err == nil {
		t.Fatal("guessed unsupported STT")
	}
}
func TestRealtimeNativeAudio(t *testing.T) {
	done := make(chan struct{})
	audio := []byte{1, 0, 2, 0, 3, 0}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer close(done)
		if r.URL.Path != "/v1/realtime" || r.URL.Query().Get("model") != "voice-model" || r.Header.Get("Authorization") != "Bearer voice-secret" {
			t.Error("wrong realtime endpoint/auth")
		}
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			t.Error(err)
			return
		}
		defer conn.CloseNow()
		ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()
		var update map[string]any
		if wsjson.Read(ctx, conn, &update) != nil {
			return
		}
		if update["type"] != "session.update" {
			t.Error("missing session config")
		}
		session := update["session"].(map[string]any)
		input := session["audio"].(map[string]any)["input"].(map[string]any)
		if input["turn_detection"] != nil {
			t.Error("automatic response could duplicate manual commit")
		}
		wsjson.Write(ctx, conn, map[string]any{"type": "session.updated"})
		var types []string
		var gotAudio []byte
		for {
			var event struct {
				Type  string `json:"type"`
				Audio string `json:"audio"`
			}
			if wsjson.Read(ctx, conn, &event) != nil {
				return
			}
			types = append(types, event.Type)
			if event.Type == "input_audio_buffer.append" {
				b, _ := base64.StdEncoding.DecodeString(event.Audio)
				gotAudio = append(gotAudio, b...)
			}
			if event.Type == "response.create" {
				break
			}
		}
		if !bytes.Equal(gotAudio, voiceSamples(32, 2400)) || strings.Join(types, ",") != "input_audio_buffer.append,input_audio_buffer.commit,response.create" {
			t.Errorf("unexpected input flow: %v", types)
		}
		wsjson.Write(ctx, conn, map[string]any{"type": "response.output_audio.delta", "delta": base64.StdEncoding.EncodeToString(audio)})
		wsjson.Write(ctx, conn, map[string]any{"type": "response.output_audio_transcript.delta", "delta": "Привет"})
		wsjson.Write(ctx, conn, map[string]any{"type": "response.done", "response": map[string]any{"status": "completed"}})
		// Keep the socket alive until the client closes it.
		conn.Read(ctx)
	}))
	defer server.Close()
	var got []byte
	answer, _, err := realtimeTurn(context.Background(), LLM{BaseURL: server.URL + "/v1", Model: "voice-model", APIKey: "voice-secret"}, nil, voiceSamples(32, 2400), func(b []byte) error { got = append(got, b...); return nil })
	if err != nil || answer != "Привет" || !bytes.Equal(got, audio) {
		t.Fatalf("%q %v audio=%v", answer, err, got)
	}
	<-done
}
func TestRealtimeErrorDoesNotSynthesize(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow()
		var event json.RawMessage
		wsjson.Read(r.Context(), conn, &event)
		wsjson.Write(r.Context(), conn, map[string]any{"type": "error", "error": map[string]any{"message": "unsupported voice"}})
	}))
	defer server.Close()
	_, _, err := realtimeTurn(context.Background(), LLM{BaseURL: server.URL, Model: "voice"}, nil, nil, func([]byte) error { t.Fatal("played invalid response"); return nil })
	if err == nil || !strings.Contains(err.Error(), "unsupported voice") {
		t.Fatalf("%v", err)
	}
}

func TestTextModelUsesChosenSynthesizer(t *testing.T) {
	var requests []string
	var requestMu sync.Mutex
	firstStarted := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		requestMu.Lock()
		requests = append(requests, r.URL.Path)
		requestMu.Unlock()
		switch r.URL.Path {
		case "/chat/completions":
			msgs := body["messages"].([]any)
			question := msgs[len(msgs)-1].(map[string]any)["content"]
			if question == "old" {
				close(firstStarted)
				<-r.Context().Done()
				return
			}
			if body["model"] != "my-text-model" {
				t.Error("wrong text model")
			}
			w.Write([]byte(`{"choices":[{"message":{"content":"Новый ответ"}}]}`))
		case "/audio/speech":
			if body["model"] != "my-synth" || body["voice"] != "chosen-voice" || body["input"] != "Новый ответ" {
				t.Errorf("wrong synth: %v", body)
			}
			w.Header().Set("Content-Type", "audio/wav")
			w.Write(pcmWAV(voiceSamples(1, 100), 24000))
		default:
			t.Errorf("unexpected endpoint %s", r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	defer server.Close()
	c := defaults()
	c.LLM = LLM{BaseURL: server.URL, Model: "my-text-model", History: 2}
	c.LED.Enabled = false
	c.TTS = TTS{Provider: "openai", BaseURL: server.URL, Model: "my-synth", Voice: "chosen-voice", Speed: 1}
	played := make(chan struct{}, 1)
	d := &Daemon{cfg: c, state: "idle", playWave: func(ctx context.Context, b []byte) error {
		if string(b[:4]) != "RIFF" {
			t.Error("not WAV")
		}
		played <- struct{}{}
		return nil
	}}
	d.ask("old", "panel")
	<-firstStarted
	d.ask("new", "panel")
	select {
	case <-played:
	case <-time.After(3 * time.Second):
		t.Fatal("new response not played")
	}
	d.turnMu.Lock()
	d.turnMu.Unlock()
	d.mu.Lock()
	defer d.mu.Unlock()
	if len(d.history) != 1 || d.history[0].Question != "new" || d.state != "idle" {
		t.Fatalf("stale turn changed state/history: %+v %s", d.history, d.state)
	}
	requestMu.Lock()
	defer requestMu.Unlock()
	if strings.Join(requests, ",") != "/chat/completions,/chat/completions,/audio/speech" {
		t.Fatalf("unexpected output routing %v", requests)
	}
}

func TestEmptyRecognitionDoesNotSpeakOrEnterHistory(t *testing.T) {
	started := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/audio/transcriptions" {
			t.Errorf("silence reached output endpoint: %s", r.URL.Path)
			w.WriteHeader(500)
			return
		}
		close(started)
		w.Write([]byte(`{"text":"  "}`))
	}))
	defer server.Close()
	c := defaults()
	c.LED.Enabled = false
	c.Enabled = true
	c.Microphone.Source = "microphone"
	c.STT = STT{Provider: "custom", BaseURL: server.URL, Model: "whisper"}
	c.TTS = TTS{Provider: "openai", BaseURL: server.URL, Model: "tts"}
	d := &Daemon{cfg: c, state: "idle", mode: true, micArmed: true}
	d.askAudio(voiceSamples(20, 2400))
	select {
	case <-started:
	case <-time.After(2 * time.Second):
		t.Fatal("STT not called")
	}
	d.turnMu.Lock()
	d.turnMu.Unlock()
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.state != "idle" || d.err != "" || len(d.history) != 0 || !d.mode || d.noSpeechAt.IsZero() {
		t.Fatalf("silence was not ignored: state=%s error=%q history=%v mode=%v", d.state, d.err, d.history, d.mode)
	}
}

func TestRealtimeReturnToAliceWithoutAudio(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow()
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		var ev map[string]any
		if wsjson.Read(ctx, conn, &ev) != nil {
			return
		}
		session := ev["session"].(map[string]any)
		tools := session["tools"].([]any)
		if len(tools) != 1 || tools[0].(map[string]any)["name"] != "return_to_alice" {
			t.Error("exit tool missing")
		}
		wsjson.Write(ctx, conn, map[string]any{"type": "session.updated"})
		for {
			if wsjson.Read(ctx, conn, &ev) != nil {
				return
			}
			if ev["type"] == "response.create" {
				break
			}
		}
		wsjson.Write(ctx, conn, map[string]any{"type": "response.output_item.done", "item": map[string]any{"type": "function_call", "name": "return_to_alice"}})
		conn.Read(ctx)
	}))
	defer server.Close()
	_, _, err := realtimeTurn(context.Background(), LLM{BaseURL: server.URL, Model: "voice"}, []message{{"user", "Верни Алису"}}, nil, func([]byte) error { t.Error("unexpected speech on exit"); return nil })
	if !errors.Is(err, errReturnToAlice) {
		t.Fatalf("exit treated as API error: %v", err)
	}
}

func TestRealtimeCancellationClosesSession(t *testing.T) {
	waiting := make(chan struct{})
	closed := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow()
		defer close(closed)
		var ev map[string]any
		if wsjson.Read(r.Context(), conn, &ev) != nil {
			return
		}
		wsjson.Write(r.Context(), conn, map[string]any{"type": "session.updated"})
		for {
			if wsjson.Read(r.Context(), conn, &ev) != nil {
				return
			}
			if ev["type"] == "response.create" {
				break
			}
		}
		close(waiting)
		conn.Read(r.Context())
	}))
	defer server.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		_, _, err := realtimeTurn(ctx, LLM{BaseURL: server.URL, Model: "voice"}, nil, nil, func([]byte) error { t.Error("cancelled turn played audio"); return nil })
		done <- err
	}()
	select {
	case <-waiting:
	case <-time.After(2 * time.Second):
		t.Fatal("client did not request response")
	}
	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Error("cancellation ignored")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("session cancellation stalled")
	}
	select {
	case <-closed:
	case <-time.After(2 * time.Second):
		t.Fatal("WebSocket left open")
	}
}

func TestRealtimeAPIFailureAnnouncedOnceAndRestoresAlice(t *testing.T) {
	played := make(chan struct{}, 1)
	var mu sync.Mutex
	var paths []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		paths = append(paths, r.URL.Path)
		mu.Unlock()
		switch r.URL.Path {
		case "/v1/realtime":
			w.WriteHeader(401)
		case "/v1/audio/speech":
			w.Header().Set("Content-Type", "audio/wav")
			w.Write(pcmWAV(voiceSamples(1, 100), 24000))
		default:
			t.Errorf("unexpected endpoint: %s", r.URL.Path)
			w.WriteHeader(500)
		}
	}))
	defer server.Close()
	c := defaults()
	c.Enabled = true
	c.LED.Enabled = false
	c.Microphone.Source = "microphone"
	c.LLM = LLM{Transport: "realtime", BaseURL: server.URL + "/v1", Model: "voice"}
	c.TTS = TTS{Provider: "openai", BaseURL: server.URL + "/v1", Model: "chosen-synth", Voice: "chosen-voice"}
	d := &Daemon{cfg: c, state: "idle", mode: true, micArmed: true, playWave: func(context.Context, []byte) error { played <- struct{}{}; return nil }}
	d.askAudio(voiceSamples(20, 2400))
	select {
	case <-played:
	case <-time.After(3 * time.Second):
		t.Fatal("API failure not announced")
	}
	d.turnMu.Lock()
	d.turnMu.Unlock()
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.mode || d.micArmed || d.state != "idle" || len(d.history) != 1 || !strings.Contains(d.err, "401") {
		t.Fatalf("failed to return control: mode=%v armed=%v state=%s history=%d error=%q", d.mode, d.micArmed, d.state, len(d.history), d.err)
	}
	mu.Lock()
	defer mu.Unlock()
	if strings.Join(paths, ",") != "/v1/realtime,/v1/audio/speech" {
		t.Fatalf("repeated error or wrong provider: %v", paths)
	}
}
