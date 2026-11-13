// assistantd — свой голосовой помощник на Яндекс Станции: «Алиса, спроси GPT …» уходит выбранной модели
// (OpenAI-совместимый API), ответ Алисы отменяется, ответ модели читает Алиса или OpenAI-совместимый TTS.
// Работает на самой колонке; настройки, команды и состояние — файлы, которые читает и пишет веб-панель.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"
)

var version = "dev"

const (
	maindLog   = "/data/quasar/daemons_logs/maind.log"
	sayLimit   = 900 // символов в одном «Повтори за мной»: длиннее Алиса читает минуту и больше
	historyMax = 20
)

// Код ответа API из текста ошибки post(): «API ответил 401: …».
var httpStatus = regexp.MustCompile(`API ответил (\d{3})`)

type entry struct {
	At         int64  `json:"at"`
	Source     string `json:"source"` // voice — голосом, panel — проверка из панели
	Question   string `json:"question"`
	Answer     string `json:"answer,omitempty"`
	Error      string `json:"error,omitempty"`
	Ms         int64  `json:"ms,omitempty"`
	STTMs      int64  `json:"stt_ms,omitempty"`
	ModelMs    int64  `json:"model_ms,omitempty"`
	TTSMs      int64  `json:"tts_ms,omitempty"`
	PlaybackMs int64  `json:"playback_ms,omitempty"`
	Local      bool   `json:"local,omitempty"` // ответил локальный навык, без модели
}

type voiceTimingKey struct{}
type voiceTurnKey struct{}

type Daemon struct {
	cfgPath, statusPath, cmdPath string

	mu       sync.Mutex
	cfg      Config
	cfgErr   string
	cfgSum   [sha256.Size]byte
	state    string // idle, thinking, speaking
	err      string
	history  []entry
	convo    []message
	lastTurn time.Time
	cancel   context.CancelFunc
	dirty    bool

	turn          uint64
	turnMu        sync.Mutex
	outputMu      sync.Mutex
	micReady      bool
	micError      string
	playbackEnd   time.Time
	outputActive  bool
	playWave      func(context.Context, []byte) error
	micArmed      bool
	noSpeechAt    time.Time
	noSpeechLevel float64

	alice aliceCLI
	// своя речь Алисы: после неё она по привычке слушает ещё 5 секунд — это гасим
	sayAt    time.Time
	ours     bool
	wokeUser bool
	// режим ассистента: все вопросы к «Алисе» — модели; modeAt — последний разговор (для автоотключения)
	mode   bool
	modeAt time.Time
	// одноразовый маршрут: следующая фраза после своего слова уходит модели, режим не включается
	route   bool
	routeAt time.Time
	// своё радио: на время разговора с Алисой ставим на паузу; resume=false — Алисе велели включить что-то своё
	radio  player
	resume bool
	// состояние самой Алисы по журналу: idle, listening, busy, speaking
	aliceState string
	lastRadio  string
	// последняя станция, которую не нашли по голосу, — панель предложит привязать это название
	radioMiss map[string]any
	// просьба переключить провайдера музыки — панель забирает и подтверждает music_seen
	musicWish *musicWish
	// когда включили своё радио: в первые секунды отклики плеера Алисы на паузу не считаем её новой музыкой
	radioAt time.Time

	// статус прокси и подключения к LLM API
	proxyStatus   ProxyCheckResult
	proxyChecked  time.Time
	proxyChecking bool
}

func (d *Daemon) reload() {
	b, err := os.ReadFile(d.cfgPath)
	if err != nil {
		return
	}
	sum := sha256.Sum256(b)
	d.mu.Lock()
	unchanged := sum == d.cfgSum
	d.mu.Unlock()
	if unchanged {
		return
	}
	c, err := parseConfig(b)
	d.mu.Lock()
	prev := d.cfg.LLM
	if d.cancel != nil {
		d.cancel()
		d.turn++
	}
	d.state = "idle"
	if !c.Enabled {
		d.mode = false
		os.Remove(microphoneLease)
	}
	// Смена модели или адреса API — новый разговор: старые реплики не уходят чужому провайдеру
	if prev.BaseURL != c.LLM.BaseURL || prev.Model != c.LLM.Model || prev.Transport != c.LLM.Transport {
		d.convo = nil
		d.lastTurn = time.Time{}
	}
	d.cfg, d.cfgSum, d.cfgErr, d.dirty = c, sum, "", true
	if err != nil {
		d.cfgErr = "настройки не прочитаны: " + err.Error()
	}
	d.mu.Unlock()
}

func (d *Daemon) set(state, errText string) {
	d.mu.Lock()
	d.state, d.dirty = state, true
	if errText != "" || state == "thinking" {
		d.err = errText
	}
	d.mu.Unlock()
}

// A late response from a cancelled request cannot overwrite the next turn.
func (d *Daemon) setContext(ctx context.Context, state, errText string) {
	turn, ok := ctx.Value(voiceTurnKey{}).(uint64)
	if !ok {
		d.set(state, errText)
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.turn != turn || ctx.Err() != nil {
		return
	}
	d.state, d.dirty = state, true
	if errText != "" || state == "thinking" {
		d.err = errText
	}
}

func (d *Daemon) returnToAlice(turn uint64) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.turn != turn {
		return
	}
	d.mode, d.micArmed, d.state, d.dirty = false, false, "idle", true
	os.Remove(microphoneLease)
}

func (d *Daemon) checkProxyBg(force bool) {
	d.mu.Lock()
	if d.proxyChecking || (!force && time.Since(d.proxyChecked) < 60*time.Second) {
		d.mu.Unlock()
		return
	}
	d.proxyChecking = true
	proxy := d.cfg.LLM.Proxy
	baseURL := d.cfg.LLM.BaseURL
	d.mu.Unlock()

	defer func() {
		d.mu.Lock()
		d.proxyChecking = false
		d.mu.Unlock()
	}()

	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	res := checkProxy(ctx, proxy, baseURL)

	d.mu.Lock()
	d.proxyStatus = res
	d.proxyChecked = time.Now()
	d.dirty = true
	d.mu.Unlock()
}

func (d *Daemon) writeStatus() {
	// радио меняется само (название трека, обрыв потока) — пишем состояние, когда оно изменилось
	radio, _ := json.Marshal(d.radio.Status())
	d.mu.Lock()
	if string(radio) != d.lastRadio {
		d.lastRadio, d.dirty = string(radio), true
	}
	if !d.dirty {
		d.mu.Unlock()
		return
	}
	d.dirty = false
	s := map[string]any{"pid": os.Getpid(), "version": version, "state": d.state, "error": d.err,
		"config_error": d.cfgErr, "enabled": d.cfg.Enabled, "history": d.history, "at": time.Now().Unix(),
		"microphone": map[string]any{"ready": d.micReady, "error": d.micError},
		"skills":     map[string]any{"time": d.cfg.Skills.Time, "weather": d.cfg.Skills.Weather},
		"mode":       d.mode, "route": d.route, "radio": d.radio.Status(), "radio_miss": d.radioMiss,
		"music": d.musicWish, "proxy": d.proxyStatus}
	if d.mode {
		s["mode_until"] = d.modeAt.Add(time.Duration(d.cfg.Mode.TimeoutMin) * time.Minute).Unix()
	}
	d.mu.Unlock()
	b, _ := json.Marshal(s)
	tmp := d.statusPath + ".tmp"
	if os.WriteFile(tmp, b, 0600) == nil {
		os.Rename(tmp, d.statusPath)
	}
}

// ask — один вопрос: модель → озвучка. Новый вопрос прерывает предыдущий.
func (d *Daemon) ask(question, source string) { d.startTurn(question, source, nil) }
func (d *Daemon) askAudio(pcm []byte)         { d.startTurn("", "microphone", pcm) }

func (d *Daemon) startTurn(question, source string, pcm []byte) {
	d.mu.Lock()
	if d.cancel != nil {
		d.cancel()
	}
	ctx, cancel := context.WithTimeout(context.Background(), realtimeTimeout)
	d.cancel = cancel
	d.turn++
	turn := d.turn
	ctx = context.WithValue(ctx, voiceTurnKey{}, turn)
	c := d.cfg
	if time.Since(d.lastTurn) > 10*time.Minute {
		d.convo = nil
	}
	previous := append([]message(nil), d.convo...)
	d.state, d.err, d.dirty = "thinking", "", true
	if len(pcm) == 0 {
		d.modeAt = time.Now()
	}
	d.mu.Unlock()
	go func() {
		defer cancel()
		// A cancelled player/CLI finishes before the replacement turn can speak.
		d.turnMu.Lock()
		defer d.turnMu.Unlock()
		if ctx.Err() != nil {
			return
		}
		started := time.Now()
		e := entry{At: started.Unix(), Source: source, Question: question}
		ctx = context.WithValue(ctx, voiceTimingKey{}, &e)
		glowCtx, stopGlow := context.WithCancel(ctx)
		glowDone := make(chan struct{})
		go glow(glowCtx, c.LED, glowDone)
		unglow := func() { stopGlow(); <-glowDone }
		defer unglow()
		var answer string
		var err error
		if len(pcm) > 0 && c.LLM.Transport != "realtime" {
			var stt STT
			stt, err = c.speechRecognition()
			if err == nil {
				sttStart := time.Now()
				question, err = transcribe(ctx, stt, pcm)
				e.STTMs = time.Since(sttStart).Milliseconds()
				question = strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(question), "Алиса,"))
				if q, ok := matchTrigger(question, c.Triggers); ok {
					question = q
				}
				e.Question = question
			}
			if err == nil && d.audioControl(question, turn) {
				return
			}
		}
		msgs := append([]message{{"system", c.LLM.SystemPrompt}}, previous...)
		if question != "" {
			msgs = append(msgs, message{"user", question})
		}
		byAlice := false
		// Стандартные вопросы (время, дата, погода) отвечаем на месте: без модели и без задержки API.
		local := false
		if err == nil {
			if text, ok := d.localAnswer(ctx, c, question); ok {
				local, answer, e.Local = true, text, true
				log.Printf("локальный ответ (%d симв.)", len([]rune(text)))
				d.setContext(ctx, "speaking", "")
				if c.TTS.Provider != "openai" {
					unglow()
				}
				byAlice, err = d.speak(ctx, c, answer)
			}
		}
		if err == nil && !local && c.LLM.Transport == "realtime" {
			player := &pcmPlayback{ctx: ctx}
			d.outputMu.Lock()
			answer, question, err = realtimeTurn(ctx, c.LLM, msgs, pcm, func(audio []byte) error {
				if ctx.Err() != nil {
					return ctx.Err()
				}
				d.setContext(ctx, "speaking", "")
				return player.Write(audio)
			})
			if err != nil {
				player.Abort()
			}
			playErr := player.Close()
			d.outputMu.Unlock()
			if err == nil {
				err = playErr
			}
			if errors.Is(err, errReturnToAlice) {
				d.returnToAlice(turn)
				return
			}
			if e.Question == "" {
				e.Question = question
			}
			d.mu.Lock()
			d.playbackEnd = time.Now()
			d.mu.Unlock()
		} else if err == nil && !local {
			modelStart := time.Now()
			answer, err = chat(ctx, c.LLM, msgs)
			e.ModelMs = time.Since(modelStart).Milliseconds()
			if err == nil {
				d.setContext(ctx, "speaking", "")
				if c.TTS.Provider != "openai" {
					unglow()
				}
				byAlice, err = d.speak(ctx, c, answer)
			}
		}
		if errors.Is(err, errNoSpeech) {
			d.mu.Lock()
			if d.turn == turn {
				d.state, d.err, d.dirty = "idle", "", true
				d.noSpeechAt = time.Now()
				d.noSpeechLevel = pcmRMS(pcm)
			}
			d.mu.Unlock()
			return // Silence is not an API failure and must never be spoken aloud.
		}
		if ctx.Err() == context.Canceled {
			return
		}
		unglow()
		if err != nil {
			if len(pcm) > 0 {
				// Real service failures are announced once, then Alice gets the input
				// back. A missing local sidecar cannot start a spoken error loop.
				d.mu.Lock()
				if d.turn == turn {
					d.mode, d.micArmed, d.dirty = false, false, true
					os.Remove(microphoneLease)
				}
				d.mu.Unlock()
			}
			d.notifyError(ctx, c, err)
			e.Error = err.Error()
			log.Printf("ассистент: %v", err)
		}
		e.Answer = answer
		e.Ms = time.Since(started).Milliseconds()
		d.mu.Lock()
		if d.turn != turn {
			d.mu.Unlock()
			return
		}
		if err == nil {
			if e.Question == "" {
				e.Question = "Голосовой вопрос"
			}
			d.convo = append(d.convo, message{"user", e.Question}, message{"assistant", answer})
			if n := 2 * c.LLM.History; len(d.convo) > n {
				d.convo = d.convo[len(d.convo)-n:]
			}
			d.lastTurn = time.Now()
			d.modeAt = d.lastTurn
		}
		d.history = append(d.history, e)
		if len(d.history) > historyMax {
			d.history = d.history[len(d.history)-historyMax:]
		}
		d.state, d.err, d.dirty = "idle", e.Error, true
		if !c.Mode.FollowUp {
			d.micArmed = false
		}
		d.mu.Unlock()
		if err == nil && !byAlice && c.Mode.FollowUp && d.inMode() && c.Microphone.Source != "microphone" {
			d.alice.Listen()
		}
	}()
}

// Local control remains available while Alice's input is exclusively routed.
func (d *Daemon) audioControl(text string, turn uint64) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.turn != turn {
		return true
	}
	if _, ok := matchTrigger(text, d.cfg.Mode.Exit); ok {
		d.mode, d.micArmed, d.state, d.dirty = false, false, "idle", true
		os.Remove(microphoneLease)
		return true
	}
	switch normalize(text) {
	case "стоп", "хватит", "замолчи", "отмена":
		d.state, d.dirty = "idle", true
		return true
	case "новый разговор", "забудь", "забудь все":
		d.convo = nil
		d.state, d.dirty = "idle", true
		return true
	}
	return false
}

func speakGlagol(ctx context.Context, text string) bool {
	client := &http.Client{Timeout: 3 * time.Second}
	body, _ := json.Marshal(map[string]string{"action": "speak_text", "query": text})
	req, err := http.NewRequestWithContext(ctx, "POST", "http://127.0.0.1:8099/api", bytes.NewReader(body))
	if err != nil {
		return false
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// speak озвучивает текст выбранным голосом; byAlice — прочитала Алиса (в том числе запасным путём,
// тогда её микрофон после ответа уже открыт сам). Если голос ассистента недоступен (адрес, ключ, сеть,
// aplay) — текст читает Алиса, чтобы ответ и голосовые ошибки всё равно прозвучали.
func (d *Daemon) speak(ctx context.Context, c Config, text string) (byAlice bool, err error) {
	d.outputMu.Lock()
	defer d.outputMu.Unlock()
	d.mu.Lock()
	d.outputActive = true
	d.mu.Unlock()
	defer func() { d.mu.Lock(); d.playbackEnd = time.Now(); d.outputActive = false; d.mu.Unlock() }()
	if err := ctx.Err(); err != nil {
		return false, err
	}
	if c.TTS.Provider != "openai" {
		if speakGlagol(ctx, text) {
			return true, nil
		}
		return true, d.aliceSay(ctx, text)
	}
	voiceErr := func() error {
		start := time.Now()
		wav, err := speech(ctx, c.TTS, c.LLM.Proxy, text)
		if timing, ok := ctx.Value(voiceTimingKey{}).(*entry); ok {
			timing.TTSMs += time.Since(start).Milliseconds()
		}
		if err != nil {
			return err
		}
		playStart := time.Now()
		defer func() {
			if timing, ok := ctx.Value(voiceTimingKey{}).(*entry); ok {
				timing.PlaybackMs += time.Since(playStart).Milliseconds()
			}
		}()
		if d.playWave != nil {
			return d.playWave(ctx, wav)
		}
		cmd := exec.CommandContext(ctx, "/usr/bin/aplay", "-q", "-")
		// при отмене — мягко: aplay тоже пишет в общий микшер dmix, и SIGKILL может оставить его захваченным
		cmd.Cancel = func() error { return cmd.Process.Signal(os.Interrupt) }
		cmd.WaitDelay = 3 * time.Second
		cmd.Stdin = bytes.NewReader(wav)
		if out, err := cmd.CombinedOutput(); err != nil {
			return fmt.Errorf("aplay: %v %s", err, bytes.TrimSpace(out))
		}
		return nil
	}()
	if voiceErr == nil {
		return false, nil
	}
	if ctx.Err() != nil {
		return false, voiceErr // ответ отменили новым вопросом — Алисе читать нечего
	}
	log.Printf("озвучка ассистента не сработала (%v) — читаю голосом Алисы", voiceErr)
	return true, d.aliceSay(ctx, text)
}

func (d *Daemon) aliceSay(ctx context.Context, text string) error {
	d.mu.Lock()
	d.sayAt, d.ours, d.wokeUser = time.Now(), false, false
	d.mu.Unlock()
	return d.alice.Say(ctx, forSpeech(text, sayLimit))
}

// notifyError — ошибку ассистента (сеть, ключ, пустой ответ) слышно голосом: в панели она видна,
// но колонка должна сказать сама. Озвучка тем же голосом, что читает ответы; не выйдет — speak
// перейдёт на голос Алисы.
func (d *Daemon) notifyError(ctx context.Context, c Config, err error) {
	if ctx.Err() != nil {
		return // вопрос прерван новым — о старой ошибке молчим
	}
	d.setContext(ctx, "speaking", "")
	if _, e := d.speak(ctx, c, voiceError(err)); e != nil {
		log.Printf("не удалось сообщить об ошибке голосом: %v", e)
	}
}

// Общая фраза на случай, который не объяснить одной причиной.
const genericError = "Ошибка ассистента. Пожалуйста, проверьте веб-панель управления для подробностей."

// voiceError — что сказать голосом вместо текста ошибки: провайдеры отвечают по-английски, а голосовые
// модели английский не читают; по «API ответил 401» всё равно ничего не понять. Частые причины называем
// по-русски и по делу, остальное — общая фраза: подробности остаются в панели (и в журнале).
func voiceError(err error) string {
	msg := err.Error()
	switch {
	case strings.Contains(msg, "модель вернула пустой ответ"):
		return "Модель вернула пустой ответ. Попробуйте спросить ещё раз."
	case strings.Contains(msg, "не задан адрес API"):
		return "Не задан адрес АПИ ассистента. Укажите его в веб-панели."
	case strings.Contains(msg, "не задана модель"):
		return "Не выбрана модель ассистента. Укажите её в веб-панели."
	case strings.Contains(msg, "неверный адрес прокси"):
		return "Неверный адрес прокси. Проверьте его в веб-панели."
	case strings.Contains(msg, "непонятный ответ API"):
		return "Ассистент вернул непонятный ответ. Подробности — в веб-панели."
	case errors.Is(err, context.DeadlineExceeded):
		return "Ассистент не ответил вовремя. Попробуйте ещё раз."
	}
	var uerr *url.Error
	if errors.As(err, &uerr) {
		if uerr.Timeout() {
			return "Ассистент не ответил вовремя. Попробуйте ещё раз."
		}
		return "Нет связи с сервером ассистента. Проверьте интернет и адрес АПИ в веб-панели."
	}
	if m := httpStatus.FindStringSubmatch(msg); m != nil {
		switch code := m[1]; {
		case code == "401" || code == "403":
			return "Ключ АПИ не подошёл. Проверьте ключ в веб-панели."
		case code == "404":
			return "Адрес АПИ или модель не найдены. Проверьте настройки в веб-панели."
		case code == "408" || code == "429":
			return "Сервер ассистента просит подождать: слишком много запросов. Попробуйте позже."
		case strings.HasPrefix(code, "5"):
			return "Сервер ассистента ответил ошибкой. Попробуйте позже."
		default:
			return "Ассистент отклонил запрос. Подробности — в веб-панели."
		}
	}
	return genericError
}

func (d *Daemon) inMode() bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.mode
}

// setRoute — одноразовый маршрут: следующая фраза уходит модели, без режима и озвучки.
func (d *Daemon) setRoute(on bool) {
	d.mu.Lock()
	d.route, d.routeAt, d.dirty = on, time.Now(), true
	d.mu.Unlock()
}

func (d *Daemon) routeTimeout() {
	d.mu.Lock()
	expired := d.route && time.Since(d.routeAt) > 15*time.Second
	d.mu.Unlock()
	if expired {
		d.setRoute(false)
	}
}

// setMode включает и выключает режим ассистента; сообщает голосом и вспышкой кольца.
func (d *Daemon) setMode(on bool, announce bool) {
	d.mu.Lock()
	if on && !d.cfg.Enabled {
		d.err, d.dirty = "сначала включите помощник", true
		d.mu.Unlock()
		return
	}
	if on && d.cfg.Microphone.Source == "microphone" {
		var err error
		if !d.micReady {
			err = errors.New("сначала подключите микрофонный захват")
		}
		if d.cfg.LLM.Transport != "realtime" {
			if _, e := d.cfg.speechRecognition(); e != nil {
				err = e
			}
		}
		if err != nil {
			d.err, d.dirty = err.Error(), true
			d.mu.Unlock()
			return
		}
	}
	changed := d.mode != on
	d.mode, d.modeAt, d.micArmed, d.dirty = on, time.Now(), on, true
	if !on {
		os.Remove(microphoneLease)
	}
	c := d.cfg
	if !changed {
		d.mu.Unlock()
		return
	}
	if d.cancel != nil {
		d.cancel()
	}
	d.turn++
	d.state = "idle"
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	d.cancel = cancel
	d.mu.Unlock()
	if !announce {
		cancel()
		return
	}
	log.Printf("режим ассистента: %v", on)
	go func() {
		defer cancel()
		glowCtx, stopGlow := context.WithTimeout(ctx, 900*time.Millisecond)
		done := make(chan struct{})
		glow(glowCtx, c.LED, done)
		stopGlow()
		if ctx.Err() != nil {
			return
		}
		text := "Возвращаю Алису."
		if on {
			text = "Переключаюсь на ассистента. Спрашивайте."
		}
		if _, err := d.speak(ctx, c, text); err != nil && ctx.Err() == nil {
			log.Printf("не удалось сообщить о режиме: %v", err)
		}
	}()
}

// stopAnswer обрывает текущий ответ модели (запрос, озвучку и речь Алисы).
func (d *Daemon) stopAnswer() {
	d.mu.Lock()
	if d.cancel != nil {
		d.cancel()
	}
	d.turn++
	d.mu.Unlock()
	d.alice.Cancel()
	d.set("idle", "")
}

// resumeRadio возвращает радио, когда и Алиса, и ассистент замолчали: ответ модели может прийти позже,
// чем Алиса вернулась в покой после отмены своего ответа.
func (d *Daemon) resumeRadio() {
	quiet := 0
	for i := 0; i < 600; i++ {
		time.Sleep(300 * time.Millisecond)
		d.mu.Lock()
		idle := d.aliceState == "idle" && d.state == "idle"
		again := d.resume // за это время Алису позвали снова — возобновит следующий покой
		d.mu.Unlock()
		if again {
			return
		}
		if !idle {
			quiet = 0
			continue
		}
		if quiet++; quiet >= 3 {
			d.radio.Resume()
			return
		}
	}
}

// onRadio — своё радио: пауза на время разговора с Алисой и команды голосом. true — фраза обработана.
func (d *Daemon) onRadio(ev logEvent, c Config, ours bool) bool {
	switch ev.kind {
	case "spotted":
		if d.radio.Pause() {
			d.mu.Lock()
			d.resume = true
			d.mu.Unlock()
		}
	case "listening":
		if !ours && d.radio.Pause() {
			d.mu.Lock()
			d.resume = true
			d.mu.Unlock()
		}
	case "music":
		// своё радио и музыка Алисы одновременно звучат кашей — уступаем Алисе
		d.mu.Lock()
		fresh := time.Since(d.radioAt) < 4*time.Second
		d.mu.Unlock()
		if d.radio.Active() && !fresh {
			log.Printf("радио выключено: Алиса включила музыку")
			d.radio.Stop()
			d.mu.Lock()
			d.resume = false
			d.mu.Unlock()
		}
	case "idle":
		d.mu.Lock()
		resume := d.resume
		d.resume = false
		d.mu.Unlock()
		if resume {
			go d.resumeRadio()
		}
	case "phrase":
		if !c.Radio.Voice {
			break
		}
		if name, ok := matchTrigger(ev.phrase, c.Radio.Play); ok {
			d.alice.Cancel()
			if ownRadioDisabled {
				d.speak(context.Background(), c, "Свои радиостанции пока отключены. Попросите Алису включить её радио.")
				return true
			}
			st, found := findStation(name, c.Radio.Stations)
			if name == "" && len(c.Radio.Stations) > 0 {
				st, found = c.Radio.Stations[0], true
			}
			d.mu.Lock()
			d.resume = false
			d.mu.Unlock()
			if !found {
				d.mu.Lock()
				d.radioMiss, d.dirty = map[string]any{"phrase": name, "at": time.Now().Unix()}, true
				d.mu.Unlock()
				d.speak(context.Background(), c, "Такой станции нет в списке. Привяжите это название в панели.")
				return true
			}
			log.Printf("радио голосом: %s", st.Name)
			d.mu.Lock()
			d.radioAt = time.Now()
			d.mu.Unlock()
			if err := d.radio.Start(st, c.Radio.Proxy); err != nil {
				log.Printf("радио: %v", err)
			}
			// музыка Алисы на время разговора только приглушена и после отмены заиграла бы поверх станции
			go func() { time.Sleep(700 * time.Millisecond); d.alice.PausePlayer() }()
			return true
		}
		_, stop := matchTrigger(ev.phrase, c.Radio.Stop)
		switch normalize(ev.phrase) {
		case "стоп", "хватит", "выключи", "выключи радио", "тишина":
			stop = stop || d.radio.Active()
		}
		if stop {
			d.alice.Cancel()
			d.radio.Stop()
			d.mu.Lock()
			d.resume = false
			d.mu.Unlock()
			return true
		}
		if alicePlayback(ev.phrase) {
			d.mu.Lock()
			d.resume = false // Алисе велели включить своё — радио не возвращаем
			d.mu.Unlock()
		}
	}
	return false
}

func (d *Daemon) onEvent(ev logEvent) {
	d.mu.Lock()
	c := d.cfg
	oursBefore := d.ours || time.Since(d.sayAt) < 3*time.Second
	switch ev.kind {
	case "phrase":
		d.aliceState = "busy"
	case "speaking", "listening", "idle":
		d.aliceState = ev.kind
	}
	d.mu.Unlock()
	if d.onRadio(ev, c, oursBefore) {
		return
	}
	d.mu.Lock()
	recent := time.Since(d.sayAt) < time.Minute
	switch ev.kind {
	case "spotted":
		d.wokeUser = true
	case "speaking":
		if recent {
			d.ours, d.wokeUser = true, false
		}
	case "listening":
		if d.ours && !d.wokeUser {
			d.ours = false
			if d.mode && c.Mode.FollowUp {
				break // беседа: Алиса договорила ответ и слушает продолжение — оно тоже уйдёт модели
			}
			d.mu.Unlock()
			d.alice.Cancel() // Алиса договорила наш ответ и ждёт продолжения — не ждём
			return
		}
	case "idle":
		d.ours = false
	}
	d.mu.Unlock()
	d.mu.Lock()
	exclusive := c.Enabled && c.Microphone.Source == "microphone" && d.mode
	d.mu.Unlock()
	if c.Enabled && ev.kind == "phrase" && !oursBefore && d.musicCommand(c, ev.phrase) {
		return
	}
	if exclusive {
		if ev.kind == "spotted" {
			d.mu.Lock()
			d.micArmed = true
			d.mu.Unlock()
			d.stopAnswer()
			d.alice.Cancel()
		}
		if ev.kind == "speaking" && !oursBefore {
			d.alice.Cancel()
		}
		if ev.kind == "phrase" {
			d.alice.Cancel()
			return
		}
	}
	if ev.kind != "phrase" || !c.Enabled {
		return
	}
	d.mu.Lock()
	route := d.route
	if route {
		d.route, d.dirty = false, true
	}
	d.mu.Unlock()
	if route {
		question := ev.phrase
		d.alice.Cancel()
		log.Printf("вопрос голосом по слову (%d симв.)", len([]rune(question)))
		switch normalize(question) {
		case "":
			return
		case "стоп", "хватит", "замолчи", "тихо", "отмена", "помолчи":
			d.stopAnswer()
			return
		}
		d.ask(question, "voice")
		return
	}
	if _, ok := matchTrigger(ev.phrase, c.Mode.Enter); ok {
		d.alice.Cancel()
		d.setMode(true, true)
		return
	}
	if _, ok := matchTrigger(ev.phrase, c.Mode.Exit); ok {
		d.alice.Cancel()
		d.stopAnswer()
		d.setMode(false, true)
		return
	}
	question, ok := matchTrigger(ev.phrase, c.Triggers)
	mode := d.inMode()
	if !ok && !mode {
		return
	}
	if !ok {
		question = ev.phrase // режим ассистента: модели уходит вся фраза
	}
	d.alice.Cancel() // до того, как Алиса начнёт отвечать сама: у неё на это около секунды
	if mode {
		d.mu.Lock()
		d.modeAt = time.Now()
		d.mu.Unlock()
	}
	log.Printf("вопрос голосом (%d симв.)", len([]rune(question)))
	switch normalize(question) {
	case "":
		return
	case "стоп", "хватит", "замолчи", "тихо", "отмена", "помолчи":
		d.stopAnswer()
		return
	case "новый разговор", "забудь", "забудь все":
		d.mu.Lock()
		d.convo = nil
		d.mu.Unlock()
		d.speak(context.Background(), c, "Хорошо, начинаем новый разговор.")
		return
	}
	if conf, err := os.ReadFile(scenesConf); err == nil {
		if sc, ok := findScene(string(conf), question, time.Now()); ok {
			if err := runScene(sc.id); err != nil {
				log.Printf("сценарий %s: %v", sc.id, err)
				return
			}
			log.Printf("сценарий по команде: %s", sc.id)
			d.speak(context.Background(), c, "Запускаю «"+sc.name+"».")
			return
		}
	}
	d.ask(question, "voice")
}

// command — запрос из панели: {"ask": "текст"} (проверка без голоса), {"say": "текст"} (произнести как есть),
// {"stop": true}, {"mode": true|false},
// {"radio": {"name": …, "url": …}}, {"radio_stop": true}.
func (d *Daemon) command() {
	b, err := os.ReadFile(d.cmdPath)
	if err != nil {
		return
	}
	os.Remove(d.cmdPath)
	var cmd struct {
		Ask       string   `json:"ask"`
		Say       string   `json:"say"`
		Stop      bool     `json:"stop"`
		Mode      *bool    `json:"mode"`
		Route     *bool    `json:"route"`
		Radio     *Station `json:"radio"`
		RadioStop bool     `json:"radio_stop"`
		MissSeen   bool     `json:"radio_miss_seen"`
		MusicSeen  bool     `json:"music_seen"`
		CheckProxy bool     `json:"check_proxy"`
	}
	if json.Unmarshal(b, &cmd) != nil {
		return
	}
	if cmd.CheckProxy {
		go d.checkProxyBg(true)
	}
	if cmd.Stop {
		d.stopAnswer()
	}
	if cmd.Mode != nil {
		d.setMode(*cmd.Mode, true)
	}
	if cmd.Route != nil {
		d.setRoute(*cmd.Route)
	}
	if cmd.Ask != "" {
		d.ask(cmd.Ask, "panel")
	}
	if cmd.Say != "" {
		d.mu.Lock()
		c := d.cfg
		d.mu.Unlock()
		go func() {
			if _, err := d.speak(context.Background(), c, cmd.Say); err != nil {
				log.Printf("сказать: %v", err)
			}
		}()
	}
	if cmd.RadioStop {
		d.radio.Stop()
	}
	if cmd.Radio != nil && ownRadioDisabled {
		log.Printf("радио: свои станции временно отключены")
		cmd.Radio = nil
	}
	if cmd.Radio != nil {
		d.mu.Lock()
		proxy := d.cfg.Radio.Proxy
		d.radioAt = time.Now()
		d.mu.Unlock()
		if err := d.radio.Start(*cmd.Radio, proxy); err != nil {
			log.Printf("радио: %v", err)
		}
	}
	if cmd.MissSeen {
		d.mu.Lock()
		d.radioMiss = nil
		d.mu.Unlock()
	}
	if cmd.MusicSeen {
		d.mu.Lock()
		d.musicWish = nil
		d.mu.Unlock()
	}
	if cmd.RadioStop || cmd.Radio != nil || cmd.MissSeen || cmd.MusicSeen {
		d.mu.Lock()
		d.dirty = true
		d.mu.Unlock()
	}
}

// modeTimeout выключает режим ассистента после долгой тишины — чтобы не застрять в нём, забыв.
func (d *Daemon) modeTimeout() {
	d.mu.Lock()
	expired := d.mode && d.state == "idle" && time.Since(d.modeAt) > time.Duration(d.cfg.Mode.TimeoutMin)*time.Minute
	d.mu.Unlock()
	if expired {
		log.Printf("режим ассистента выключен по тишине")
		d.setMode(false, false)
	}
}

// follow читает журнал с конца (старые разговоры при перезапуске не повторяются) и переживает его ротацию.
func (d *Daemon) follow(ctx context.Context, path string) {
	var f *os.File
	var info os.FileInfo
	var offset int64
	var pending []byte
	first := true
	tick := time.NewTicker(50 * time.Millisecond)
	defer tick.Stop()
	proxyTick := time.NewTicker(60 * time.Second)
	defer proxyTick.Stop()
	buf := make([]byte, 64<<10)
	for {
		select {
		case <-ctx.Done():
			return
		case <-proxyTick.C:
			go d.checkProxyBg(false)
		case <-tick.C:
		}
		d.reload()
		d.command()
		d.modeTimeout()
		d.routeTimeout()
		d.writeStatus()
		st, err := os.Stat(path)
		if err != nil {
			continue
		}
		if f == nil || !os.SameFile(info, st) || st.Size() < offset {
			if f != nil {
				f.Close()
			}
			if f, err = os.Open(path); err != nil {
				f = nil
				continue
			}
			info, offset, pending = st, 0, nil
			if first {
				offset, first = st.Size(), false
			}
			f.Seek(offset, io.SeekStart)
		}
		for {
			n, _ := f.Read(buf)
			if n == 0 {
				break
			}
			offset += int64(n)
			pending = append(pending, buf[:n]...)
			for {
				i := bytes.IndexByte(pending, '\n')
				if i < 0 {
					break
				}
				if ev, ok := parseLine(string(pending[:i])); ok {
					d.onEvent(ev)
				}
				pending = pending[i+1:]
			}
			if len(pending) > 1<<20 {
				pending = nil
			}
		}
	}
}

func main() {
	dir := flag.String("dir", "/data/quasar/webpanel/assistant", "папка с config.json")
	showVersion := flag.Bool("version", false, "показать версию")
	models := flag.String("models", "", "вывести модели API в JSON; значение — файл с base_url/api_key/proxy поверх настроек или -")
	checkProxyFlag := flag.String("check-proxy", "", "проверить прокси и доступность API; значение — файл с base_url/proxy или -")
	weather := flag.String("weather", "", "проверить запрос погоды: \"lat,lon\" или пусто — из настроек; и выйти")
	flag.Parse()
	if *showVersion {
		fmt.Println(version)
		return
	}
	if *models != "" {
		printModels(*dir+"/config.json", *models)
		return
	}
	if *checkProxyFlag != "" {
		printProxyCheck(*dir+"/config.json", *checkProxyFlag)
		return
	}
	if *weather != "" {
		checkWeather(*dir+"/config.json", *weather)
		return
	}
	log.SetFlags(log.Ldate | log.Ltime)
	d := &Daemon{cfgPath: *dir + "/config.json", statusPath: "/tmp/webpanel_assistant.json",
		cmdPath: "/tmp/webpanel_assistant.cmd", state: "idle", cfg: defaults(), dirty: true}
	d.reload()
	go d.checkProxyBg(false)
	// станция так и не заиграла — объясняем голосом, а не молчим (подробности — в панели)
	d.radio.onFail = func(spoken string) {
		d.mu.Lock()
		c := d.cfg
		d.mu.Unlock()
		log.Printf("радио: %s", spoken)
		d.speak(context.Background(), c, spoken)
	}
	log.Printf("assistantd %s запущен", version)
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	go d.microphone(ctx)
	go func() {
		if err := d.alice.Warm(); err != nil {
			log.Printf("CLI Алисы не запустился: %v", err)
		}
	}()
	d.follow(ctx, maindLog)
	d.stopAnswer()
	d.radio.Shutdown()
	d.alice.Close()
	os.Remove(d.statusPath)
}
