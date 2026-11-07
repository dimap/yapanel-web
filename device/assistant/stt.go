package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"strings"
	"time"
)

var errNoSpeech = errors.New("STT не распознал речь")

func transcribe(ctx context.Context, c STT, pcm []byte) (string, error) {
	if len(pcm) < 4800 || len(pcm) > 24000*2*21 {
		return "", errors.New("недопустимая длина записи STT")
	}
	var body bytes.Buffer
	w := multipart.NewWriter(&body)
	w.WriteField("model", c.Model)
	w.WriteField("response_format", "json")
	if c.Language != "" {
		w.WriteField("language", c.Language)
	}
	file, err := w.CreateFormFile("file", "speech.wav")
	if err != nil {
		return "", err
	}
	if _, err = file.Write(pcmWAV(pcm, 24000)); err != nil {
		return "", err
	}
	if err = w.Close(); err != nil {
		return "", err
	}
	hc, err := client(c.Proxy, 60*time.Second)
	if err != nil {
		return "", err
	}
	req, err := http.NewRequestWithContext(ctx, "POST", strings.TrimRight(c.BaseURL, "/")+"/audio/transcriptions", &body)
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", w.FormDataContentType())
	if c.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.APIKey)
	}
	resp, err := hc.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, (1<<20)+1))
	if err != nil {
		return "", err
	}
	if len(data) > 1<<20 {
		return "", errors.New("слишком большой ответ STT")
	}
	if resp.StatusCode/100 != 2 {
		return "", fmt.Errorf("API ответил %d: STT: %.300s", resp.StatusCode, strings.TrimSpace(string(data)))
	}
	var out struct {
		Text string `json:"text"`
	}
	if json.Unmarshal(data, &out) != nil {
		return "", errors.New("непонятный ответ STT")
	}
	text := strings.TrimSpace(out.Text)
	if text == "" {
		return "", errNoSpeech
	}
	return text, nil
}
