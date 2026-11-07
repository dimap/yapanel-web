package main

import "testing"

func TestMatchMusicProvider(t *testing.T) {
	cases := map[string]string{
		"включи сберзвук":             "zvuk",
		"Алиса, включи сбер звук!":    "zvuk",
		"переключи на сберзвук":       "zvuk",
		"переключи на сбер музыку":    "zvuk",
		"включи яндекс музыку":        "yandex",
		"Алиса, переключи на яндекс.": "yandex",
		"верни яндекс":                "yandex",
		"какая погода":                "",
		"включи радио":                "",
		"включи свет":                 "",
	}
	for phrase, want := range cases {
		got, ok := matchMusicProvider(phrase)
		if want == "" {
			if ok {
				t.Errorf("%q: не должно быть командой музыки, получили %s", phrase, got)
			}
			continue
		}
		if !ok || got != want {
			t.Errorf("%q: получили %q (%v), хотели %q", phrase, got, ok, want)
		}
	}
}
