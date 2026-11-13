package main

// Связь с Алисой без её облака: отладочный CLI maind (отмена диалога, текстовый запрос) и журнал maind.log
// (распознанная фраза, состояние Алисы). Строки журнала — как в прошивке Лайт 2 0.369:
//   onPhraseSpotted: алиса …
//   onRequestStateChanged: LISTENING, recognizedPhrase='Спроси gpt сколько будет 2 + 2'
//   onAliceStateChanged: SPEAKING, outputSpeech='…'

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"
	"unicode"
)

const quasar = "/system/vendor/quasar"

// aliceCLI держит один maind --cli открытым: так отмена срабатывает за доли секунды, а не за время запуска.
type cliTiming struct {
	start, line, speech, exit time.Duration
}

var defaultCLITiming = cliTiming{50 * time.Millisecond, 20 * time.Millisecond, 100 * time.Millisecond, 200 * time.Millisecond}

type aliceCLI struct {
	mu           sync.Mutex
	cmd          *exec.Cmd
	in           io.WriteCloser
	done         chan struct{}
	sayMu        sync.Mutex
	speechMu     sync.Mutex
	speechCancel context.CancelFunc
	// Подмена процесса и задержек нужна тестам протокола без maind и колонки.
	command func(context.Context) *exec.Cmd
	timing  *cliTiming
}

func (a *aliceCLI) delays() cliTiming {
	if a.timing != nil {
		return *a.timing
	}
	return defaultCLITiming
}

func (a *aliceCLI) newCommand(ctx context.Context) *exec.Cmd {
	if a.command != nil {
		return a.command(ctx)
	}
	cmd := exec.CommandContext(ctx, quasar+"/maind", "--cli")
	cmd.Env = append(os.Environ(), "LD_LIBRARY_PATH="+quasar+"/libs")
	return cmd
}

// Только под a.mu. Wait завершается до обнуления состояния; ProcessState не читаем.
func (a *aliceCLI) discard() {
	if a.in != nil {
		a.in.Close()
	}
	if a.cmd != nil {
		a.cmd.Process.Kill()
		<-a.done
	}
	a.cmd, a.in, a.done = nil, nil, nil
}

func (a *aliceCLI) ensure() error {
	if a.cmd != nil {
		select {
		case <-a.done:
			a.discard()
		default:
			return nil
		}
	}
	cmd := a.newCommand(context.Background())
	in, err := cmd.StdinPipe()
	if err != nil {
		return err
	}
	if err = cmd.Start(); err != nil {
		in.Close()
		return err
	}
	done := make(chan struct{})
	a.cmd, a.in, a.done = cmd, in, done
	go func() { cmd.Wait(); close(done) }()
	time.Sleep(a.delays().start)
	select {
	case <-done:
		a.discard()
		return fmt.Errorf("CLI Алисы завершился при запуске")
	default:
		return nil
	}
}

func (a *aliceCLI) send(lines ...string) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if err := a.ensure(); err != nil {
		return err
	}
	for i, line := range lines {
		if i > 0 {
			time.Sleep(a.delays().line)
		}
		if _, err := io.WriteString(a.in, line+"\n"); err != nil {
			a.discard()
			return err
		}
	}
	return nil
}

// Управляющий CLI остаётся прогретым даже после озвучки.
func (a *aliceCLI) Warm() error        { return a.send() }
func (a *aliceCLI) Listen() error      { return a.send("s") }
func (a *aliceCLI) PausePlayer() error { return a.send("o") }

func (a *aliceCLI) cancelSpeech() {
	a.speechMu.Lock()
	defer a.speechMu.Unlock()
	if a.speechCancel != nil {
		a.speechCancel()
	}
}

func (a *aliceCLI) Cancel() error {
	a.cancelSpeech()
	return a.send("c")
}

func cliPause(ctx context.Context, delay time.Duration) error {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return ctx.Err()
	}
}

// Каждый текст получает собственную CLI-сессию. Паузы озвучки не блокируют
// Cancel/Listen и прерываются при отмене вопроса; прежний текст не уйдёт позднее.
func (a *aliceCLI) Say(ctx context.Context, text string) error {
	a.sayMu.Lock()
	defer a.sayMu.Unlock()
	ctx, cancel := context.WithCancel(ctx)
	a.speechMu.Lock()
	a.speechCancel = cancel
	a.speechMu.Unlock()
	defer func() {
		cancel()
		a.speechMu.Lock()
		a.speechCancel = nil
		a.speechMu.Unlock()
	}()
	if err := ctx.Err(); err != nil {
		return err
	}
	cmd := a.newCommand(ctx)
	in, err := cmd.StdinPipe()
	if err != nil {
		return err
	}
	if err = cmd.Start(); err != nil {
		in.Close()
		return err
	}
	done := make(chan struct{})
	var waitErr error
	go func() { waitErr = cmd.Wait(); close(done) }()
	defer func() { in.Close(); cmd.Process.Kill(); <-done }()
	timing := a.delays()
	if err = cliPause(ctx, timing.start); err != nil {
		return err
	}
	if _, err = io.WriteString(in, "q\n"); err != nil {
		return err
	}
	if err = cliPause(ctx, timing.line); err != nil {
		return err
	}
	if _, err = io.WriteString(in, "Повтори за мной: "+text+"\n"); err != nil {
		return err
	}
	if err = cliPause(ctx, timing.speech); err != nil {
		return err
	}
	if _, err = io.WriteString(in, "x\n"); err != nil {
		return err
	}
	if err = in.Close(); err != nil {
		return err
	}
	timer := time.NewTimer(timing.exit)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-done:
		return waitErr
	case <-timer.C:
		return fmt.Errorf("CLI Алисы не завершился после озвучки")
	}
}

func (a *aliceCLI) Close() {
	a.cancelSpeech()
	a.mu.Lock()
	defer a.mu.Unlock()
	a.discard()
}

// logEvent — то, что нам нужно из строки журнала.
type logEvent struct {
	kind   string // spotted, phrase, speaking, listening, idle, music
	phrase string
}

func parseLine(line string) (logEvent, bool) {
	switch {
	case strings.Contains(line, "onPhraseSpotted:"):
		return logEvent{kind: "spotted"}, true
	case strings.Contains(line, "onRequestStateChanged: LISTENING, recognizedPhrase='"):
		s := line[strings.Index(line, "recognizedPhrase='")+len("recognizedPhrase='"):]
		if i := strings.LastIndex(s, "'"); i >= 0 {
			s = s[:i]
		}
		return logEvent{kind: "phrase", phrase: strings.TrimSpace(s)}, true
	case strings.Contains(line, "onAliceStateChanged: SPEAKING"):
		return logEvent{kind: "speaking"}, true
	case strings.Contains(line, "onAliceStateChanged: LISTENING"):
		return logEvent{kind: "listening"}, true
	case strings.Contains(line, "onAliceStateChanged: IDLE"):
		return logEvent{kind: "idle"}, true
	// Алиса начинает свою музыку — из приложения, панели, голосом или продолжая после паузы
	case strings.Contains(line, "music_thin_client_on_started"),
		strings.Contains(line, "routeDirective (") && (strings.Contains(line, ")audio_play") || strings.Contains(line, ")audio_player_continue")):
		return logEvent{kind: "music"}, true
	}
	return logEvent{}, false
}

func normalize(s string) string {
	s = strings.ReplaceAll(strings.ToLower(s), "ё", "е")
	return strings.Join(strings.FieldsFunc(s, func(r rune) bool { return !unicode.IsLetter(r) && !unicode.IsDigit(r) }), " ")
}

// matchTrigger: «Спроси GPT, сколько будет 2 + 2» → «сколько будет 2 + 2». Вопрос берём из исходной фразы,
// чтобы не потерять «+» и прочие знаки.
func matchTrigger(phrase string, triggers []string) (string, bool) {
	norm := normalize(phrase)
	for _, t := range triggers {
		t = normalize(t)
		if t == "" || (norm != t && !strings.HasPrefix(norm, t+" ")) {
			continue
		}
		words := strings.Fields(phrase)
		n := len(strings.Fields(t))
		// слова триггера в исходной фразе могут прилипать к знакам («gpt,») — считаем по словам нормализованной
		kept, seen := []string{}, 0
		for _, w := range words {
			if seen < n {
				seen += len(strings.Fields(normalize(w)))
				continue
			}
			kept = append(kept, w)
		}
		return strings.TrimLeft(strings.Join(kept, " "), " ,.:;!?—-"), true
	}
	return "", false
}

// forSpeech готовит ответ модели для «Повтори за мной»: одна строка, без разметки и кавычек, не длиннее limit.
func forSpeech(s string, limit int) string {
	s = strings.NewReplacer("*", "", "_", " ", "#", "", "`", "", "«", "", "»", "", "\"", "", "'", "", ">", "").Replace(s)
	s = strings.Join(strings.Fields(s), " ")
	r := []rune(s)
	if len(r) <= limit {
		return s
	}
	cut := string(r[:limit])
	if i := strings.LastIndexAny(cut, ".!?"); i > limit/2 {
		return cut[:i+1]
	}
	if i := strings.LastIndex(cut, " "); i > 0 {
		return cut[:i] + "…"
	}
	return cut
}
