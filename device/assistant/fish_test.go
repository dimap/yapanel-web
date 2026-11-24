package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func TestMsgpackRoundTrip(t *testing.T) {
	in := map[string]any{"event": "start", "request": map[string]any{"text": "", "format": "pcm", "sample_rate": 24000,
		"latency": "normal", "prosody": map[string]any{"speed": 1.5}, "ok": true, "none": nil}, "audio": []byte{1, 2, 3},
		"long": strings.Repeat("я", 300)}
	b, err := msgpackEncode(in)
	if err != nil {
		t.Fatal(err)
	}
	out, err := msgpackDecode(b)
	if err != nil {
		t.Fatal(err)
	}
	m := out.(map[string]any)
	if m["event"] != "start" || m["long"] != in["long"] || !bytes.Equal(m["audio"].([]byte), []byte{1, 2, 3}) {
		t.Fatalf("%v", m)
	}
	req := m["request"].(map[string]any)
	if req["sample_rate"] != int64(24000) || req["prosody"].(map[string]any)["speed"] != 1.5 || req["ok"] != true || req["none"] != nil {
		t.Fatalf("%v", req)
	}
	// стандартные кодировки других библиотек: fixint, uint16, str8, bin8, float32
	for _, tc := range []struct {
		raw  []byte
		want any
	}{{[]byte{0x05}, int64(5)}, {[]byte{0xff}, int64(-1)}, {[]byte{0xcd, 0x01, 0x00}, int64(256)}, {[]byte{0xd0, 0xfe}, int64(-2)},
		{[]byte{0xd9, 2, 'h', 'i'}, "hi"}, {[]byte{0xc4, 1, 9}, []byte{9}}, {[]byte{0xca, 0x3f, 0xc0, 0, 0}, 1.5}} {
		if got, err := msgpackDecode(tc.raw); err != nil || !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%x: %v %v", tc.raw, got, err)
		}
	}
	for _, bad := range [][]byte{nil, {0xc1}, {0xa5, 'a'}, {0x81, 0x01, 0x02}, {0xdb, 0xff, 0xff, 0xff, 0xff}, {0x05, 0x06}} {
		if _, err := msgpackDecode(bad); err == nil {
			t.Errorf("%x должен быть ошибкой", bad)
		}
	}
}

func TestSpeechSplitter(t *testing.T) {
	var s speechSplitter
	var got []string
	for _, d := range []string{"Привет", ", мир! Как ", "дела? Хорошо", ".", " <thi", "nk>скрыто. текст</th", "ink>Конец без точки"} {
		got = append(got, s.push(d)...)
	}
	got = append(got, s.flush())
	want := []string{"Привет, мир!", "Как дела?", "Хорошо.", "Конец без точки"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("%q", got)
	}
	// длинный кусок без точек режется по пробелу
	s = speechSplitter{}
	long := strings.Repeat("слово ", 60)
	parts := s.push(long)
	if len(parts) == 0 || len([]rune(parts[0])) > maxChunkRunes {
		t.Fatalf("%q", parts)
	}
	if strings.Join(strings.Fields(strings.Join(append(parts, s.flush()), " ")), " ") != strings.TrimSpace(long) {
		t.Fatal("текст потерян")
	}
}

func TestStreamTTSOnlyForFish(t *testing.T) {
	c := Config{TTS: TTS{Provider: "openai", BaseURL: "https://api.fish.audio/compat/v1", Stream: true}}
	if !c.streamTTS() {
		t.Fatal("Fish со включённой галочкой должен стримить")
	}
	c.TTS.Stream = false
	if c.streamTTS() {
		t.Fatal("галочка выключена")
	}
	for _, u := range []string{"https://api.openai.com/v1", "http://localhost:8000/v1", "https://api.fish.audio.evil.com/v1", ""} {
		c = Config{TTS: TTS{Provider: "openai", BaseURL: u, Stream: true}}
		if c.streamTTS() {
			t.Errorf("%q не Fish", u)
		}
	}
	c = Config{TTS: TTS{Provider: "alice", BaseURL: "https://api.fish.audio/v1", Stream: true}}
	if c.streamTTS() {
		t.Fatal("с голосом Алисы стрима нет")
	}
	c = Config{LLM: LLM{Transport: "realtime"}, TTS: TTS{Provider: "openai", BaseURL: "https://api.fish.audio/v1", Stream: true}}
	if c.streamTTS() {
		t.Fatal("Realtime не стримится")
	}
	if u, _ := fishLiveURL("https://api.fish.audio/compat/v1"); u != "wss://api.fish.audio/v1/tts/live" {
		t.Fatal(u)
	}
	if fishModel("fish-audio/s2.1-pro-free") != "s2.1-pro-free" || fishModel("s1") != "s1" {
		t.Fatal("модель")
	}
	d, _ := parseConfig([]byte(`{"tts":{"provider":"openai","stream":true}}`))
	if !d.TTS.Stream {
		t.Fatal("stream не читается из конфига")
	}
	if d, _ = parseConfig([]byte(`{}`)); d.TTS.Stream {
		t.Fatal("по умолчанию стрима нет")
	}
}

// sseServer отдаёт ответ модели кусками с паузой, как это делает потоковый API.
func sseServer(t *testing.T, got *map[string]any, parts []string, gap time.Duration) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got != nil {
			decodeBody(r, got)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		for _, p := range parts {
			fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q}}]}\n\n", p)
			w.(http.Flusher).Flush()
			time.Sleep(gap)
		}
		fmt.Fprint(w, "data: [DONE]\n\n")
	}))
}

// fishHandler — Fish Live: проверяет протокол и на каждый flush отвечает куском PCM.
func fishHandler(t *testing.T, texts *[]string, header *http.Header, failAfter int) http.HandlerFunc {
	var mu sync.Mutex
	return func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		*header = r.Header.Clone()
		mu.Unlock()
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow()
		flushes := 0
		for {
			_, data, err := conn.Read(r.Context())
			if err != nil {
				return
			}
			v, err := msgpackDecode(data)
			if err != nil {
				t.Error(err)
				return
			}
			ev := v.(map[string]any)
			reply := func(m map[string]any) {
				b, _ := msgpackEncode(m)
				conn.Write(r.Context(), websocket.MessageBinary, b)
			}
			switch ev["event"] {
			case "start":
				req := ev["request"].(map[string]any)
				if req["format"] != "pcm" || req["sample_rate"] != int64(24000) || req["reference_id"] != "voice1" {
					t.Errorf("start: %v", req)
				}
			case "text":
				mu.Lock()
				*texts = append(*texts, ev["text"].(string))
				mu.Unlock()
			case "flush":
				flushes++
				if failAfter > 0 && flushes > failAfter {
					reply(map[string]any{"event": "finish", "reason": "error"})
					return
				}
				reply(map[string]any{"event": "audio", "audio": []byte{byte(flushes), 0}})
			case "stop":
				reply(map[string]any{"event": "finish", "reason": "stop"})
				return
			}
		}
	}
}

func streamCfg(llm, fish string) Config {
	c, _ := parseConfig([]byte(`{}`))
	c.LLM = LLM{BaseURL: llm, Model: "m", MaxTokens: 77}
	c.TTS = TTS{Provider: "openai", BaseURL: fish, Model: "fish-audio/s2.1-pro-free", Voice: "voice1", Speed: 1, Stream: true, APIKey: "fk"}
	return c
}

func decodeBody(r *http.Request, v *map[string]any) { json.NewDecoder(r.Body).Decode(v) }

func errorsAs(err error, target **fishError) bool { return errors.As(err, target) }

func TestStreamSpeakEndToEnd(t *testing.T) {
	fishScheme = "ws"
	defer func() { fishScheme = "wss" }()
	var body map[string]any
	llm := sseServer(t, &body, []string{"Первое ", "предложение. Вто", "рое тут! ", "Хвост"}, 40*time.Millisecond)
	defer llm.Close()
	var texts []string
	var hdr http.Header
	fish := httptest.NewServer(fishHandler(t, &texts, &hdr, 0))
	defer fish.Close()
	c := streamCfg(llm.URL, fish.URL)
	var audio [][]byte
	var firstAudio time.Time
	start := time.Now()
	answer, played, err := streamSpeak(context.Background(), c, []message{{"user", "q"}}, func(a []byte) error {
		if firstAudio.IsZero() {
			firstAudio = time.Now()
		}
		audio = append(audio, a)
		return nil
	})
	if err != nil || !played || answer != "Первое предложение. Второе тут! Хвост" {
		t.Fatalf("%q %v %v", answer, played, err)
	}
	if !reflect.DeepEqual(texts, []string{"Первое предложение.", "Второе тут!", "Хвост"}) || len(audio) != 3 {
		t.Fatalf("texts=%q audio=%d", texts, len(audio))
	}
	if hdr.Get("Authorization") != "Bearer fk" || hdr.Get("model") != "s2.1-pro-free" {
		t.Fatalf("заголовки: %v", hdr)
	}
	if body["stream"] != true || body["max_tokens"] != float64(77) || body["model"] != "m" || body["tools"] != nil {
		t.Fatalf("запрос к модели: %v", body)
	}
	if firstAudio.Sub(start) > 130*time.Millisecond {
		t.Fatalf("звук пошёл только через %v: потока нет", firstAudio.Sub(start))
	}
}

func TestStreamSpeakFishFailureKeepsAnswer(t *testing.T) {
	fishScheme = "ws"
	defer func() { fishScheme = "wss" }()
	llm := sseServer(t, nil, []string{"Раз. ", "Два. ", "Три. ", "Четыре."}, 10*time.Millisecond)
	defer llm.Close()
	var texts []string
	var hdr http.Header
	fish := httptest.NewServer(fishHandler(t, &texts, &hdr, 1))
	defer fish.Close()
	answer, played, err := streamSpeak(context.Background(), streamCfg(llm.URL, fish.URL), nil, func([]byte) error { return nil })
	var fe *fishError
	if !errorsAs(err, &fe) || !played || answer != "Раз. Два. Три. Четыре." {
		t.Fatalf("%q %v %v", answer, played, err)
	}
}

func TestStreamAnswerFallsBackWhenPCMPlaybackCannotStart(t *testing.T) {
	fishScheme = "ws"
	t.Cleanup(func() { fishScheme = "wss" })
	const want = "Первое предложение. Остаток ответа."
	llm := sseServer(t, nil, []string{"Первое предложение. ", "Остаток ответа."}, 0)
	t.Cleanup(llm.Close)
	var texts []string
	var hdr http.Header
	wav := append([]byte("RIFF"), make([]byte, 40)...)
	mux := http.NewServeMux()
	mux.Handle("/v1/tts/live", fishHandler(t, &texts, &hdr, 0))
	mux.HandleFunc("/audio/speech", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		decodeBody(r, &body)
		if body["input"] != want {
			t.Errorf("fallback input = %q, want %q", body["input"], want)
		}
		w.Write(wav)
	})
	tts := httptest.NewServer(mux)
	t.Cleanup(tts.Close)
	missingPlayer := filepath.Join(t.TempDir(), "aplay")
	pcmAttempts, wavPlays, soundEvents := 0, 0, 0
	d := &Daemon{
		pcmCommand: func(ctx context.Context) *exec.Cmd {
			pcmAttempts++
			return exec.CommandContext(ctx, missingPlayer)
		},
		playWave: func(_ context.Context, got []byte) error {
			wavPlays++
			if !bytes.Equal(got, wav) {
				t.Errorf("fallback returned unexpected WAV: %x", got)
			}
			return nil
		},
	}
	answer, byAlice, err := d.streamAnswer(context.Background(), streamCfg(llm.URL, tts.URL), nil, func() { soundEvents++ })
	if err != nil || byAlice || answer != want {
		t.Fatalf("streamAnswer = %q, %v, %v", answer, byAlice, err)
	}
	if pcmAttempts != 1 || wavPlays != 1 || soundEvents != 1 {
		t.Fatalf("PCM attempts = %d, WAV plays = %d, sound events = %d", pcmAttempts, wavPlays, soundEvents)
	}
}

func TestStreamSpeakModelErrors(t *testing.T) {
	fishScheme = "ws"
	defer func() { fishScheme = "wss" }()
	var texts []string
	var hdr http.Header
	fish := httptest.NewServer(fishHandler(t, &texts, &hdr, 0))
	defer fish.Close()
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(401)
		w.Write([]byte(`{"error":{"message":"bad key"}}`))
	}))
	defer api.Close()
	_, _, err := streamSpeak(context.Background(), streamCfg(api.URL, fish.URL), nil, func([]byte) error { return nil })
	if err == nil || !strings.Contains(err.Error(), "401") || !strings.Contains(err.Error(), "bad key") {
		t.Fatalf("%v", err)
	}
	empty := sseServer(t, nil, nil, 0)
	defer empty.Close()
	if _, _, err = streamSpeak(context.Background(), streamCfg(empty.URL, fish.URL), nil, func([]byte) error { return nil }); err == nil ||
		!strings.Contains(err.Error(), "пустой") {
		t.Fatalf("%v", err)
	}
}

func TestStreamSpeakCancelAndTimeout(t *testing.T) {
	fishScheme = "ws"
	defer func() { fishScheme = "wss" }()
	hang := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"Начало. \"}}]}\n\n")
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer hang.Close()
	var texts []string
	var hdr http.Header
	fish := httptest.NewServer(fishHandler(t, &texts, &hdr, 0))
	defer fish.Close()
	c := streamCfg(hang.URL, fish.URL)
	streamTimeout = 1 * time.Second // секунда
	defer func() { streamTimeout = 90 * time.Second }()
	start := time.Now()
	_, _, err := streamSpeak(context.Background(), c, nil, func([]byte) error { return nil })
	if err == nil || time.Since(start) > 4*time.Second {
		t.Fatalf("таймаут не сработал: %v за %v", err, time.Since(start))
	}
	ctx, cancel := context.WithCancel(context.Background())
	streamTimeout = 90 * time.Second
	go func() { time.Sleep(200 * time.Millisecond); cancel() }()
	start = time.Now()
	if _, _, err = streamSpeak(ctx, c, nil, func([]byte) error { return nil }); err == nil || time.Since(start) > 3*time.Second {
		t.Fatalf("отмена не сработала: %v за %v", err, time.Since(start))
	}
}
