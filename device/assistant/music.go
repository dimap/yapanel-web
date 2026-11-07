package main

import (
	"context"
	"strings"
	"time"
)

// musicWish — просьба переключить провайдера музыки. Пишется в status, панель
// забирает её и подтверждает командой music_seen.
type musicWish struct {
	Provider string `json:"provider"`
	Play     bool   `json:"play"`
	At       int64  `json:"at"`
}

var zvukMusicPhrases = []string{
	"включи сберзвук", "включи сбер звук", "включи сбермузыку", "включи сбер музыку",
	"переключи на сберзвук", "переключи на сбер звук", "переключи на сбермузыку", "переключи на сбер музыку",
}

var yandexMusicPhrases = []string{
	"включи яндекс музыку", "включи яндексмузыку", "переключи на яндекс музыку",
	"переключи на яндекс", "верни яндекс",
}

// matchMusicProvider узнаёт команду переключения провайдера по началу фразы.
func matchMusicProvider(phrase string) (string, bool) {
	text := skillText(phrase)
	for _, p := range zvukMusicPhrases {
		if strings.HasPrefix(text, skillText(p)) {
			return "zvuk", true
		}
	}
	for _, p := range yandexMusicPhrases {
		if strings.HasPrefix(text, skillText(p)) {
			return "yandex", true
		}
	}
	return "", false
}

// musicCommand — голосовое «включи сберзвук»/«включи яндекс музыку»: оставляет
// заявку панели и сразу отвечает. Панель сама переключит провайдера и запустит поток.
func (d *Daemon) musicCommand(c Config, phrase string) bool {
	provider, ok := matchMusicProvider(phrase)
	if !ok {
		return false
	}
	d.alice.Cancel()
	d.mu.Lock()
	d.musicWish = &musicWish{Provider: provider, Play: true, At: time.Now().UnixMilli()}
	d.dirty = true
	d.mu.Unlock()
	text := "Включаю Сбер Звук."
	if provider == "yandex" {
		text = "Включаю Яндекс Музыку."
	}
	d.speak(context.Background(), c, text)
	return true
}
