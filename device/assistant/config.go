package main

import (
	"encoding/json"
	"os"
	"strings"
)

// Настройки пишет веб-панель; программа перечитывает файл, когда он меняется.
type LLM struct {
	Transport string `json:"transport"` // chat or realtime
	Voice     string `json:"voice"`     // Realtime output voice

	BaseURL      string `json:"base_url"`
	APIKey       string `json:"api_key"`
	Model        string `json:"model"`
	SystemPrompt string `json:"system_prompt"`
	MaxTokens    int    `json:"max_tokens"`
	History      int    `json:"history"` // сколько последних пар «вопрос — ответ» помнить
	Proxy        string `json:"proxy"`   // http://, https:// или socks5://; пусто — напрямую
}

type TTS struct {
	Provider string  `json:"provider"` // alice — голосом Алисы; openai — OpenAI-совместимый /audio/speech
	BaseURL  string  `json:"base_url"`
	APIKey   string  `json:"api_key"`
	Model    string  `json:"model"`
	Voice    string  `json:"voice"`
	Speed    float64 `json:"speed"`
}

// STT uses an OpenAI-compatible multipart /audio/transcriptions endpoint.
// model reuses the selected LLM provider, custom has independent credentials.
type STT struct {
	Provider string `json:"provider"` // alice, model, custom
	BaseURL  string `json:"base_url"`
	APIKey   string `json:"api_key"`
	Model    string `json:"model"`
	Language string `json:"language"`
	Proxy    string `json:"proxy"`
}

type Microphone struct {
	Source    string  `json:"source"`    // alice or microphone
	Threshold float64 `json:"threshold"` // RMS after gain, PCM16 units
	Gain      float64 `json:"gain"`
	SilenceMS int     `json:"silence_ms"`
}

// Skills — ответы без модели: время/дата и погода. Считаются на колонке, без токенов и задержки API.
type Skills struct {
	Time    bool `json:"time"`
	Weather bool `json:"weather"`
}

// Weather — точка для MET Norway. Город определяет панель (по IP или вручную), демону нужны координаты.
type Weather struct {
	City string  `json:"city"`
	Lat  float64 `json:"lat"`
	Lon  float64 `json:"lon"`
}

type Config struct {
	Enabled    bool              `json:"enabled"`
	Triggers   []string          `json:"triggers"`
	LLM        LLM               `json:"llm"`
	STT        STT               `json:"stt"`
	Microphone Microphone        `json:"microphone"`
	TTS        TTS               `json:"tts"`
	LED        LED               `json:"led"`
	Mode       Mode              `json:"mode"`
	Radio      Radio             `json:"radio"`
	Skills     Skills            `json:"skills"`
	Weather    Weather           `json:"weather"`
	Keys       map[string]string `json:"keys"` // ключ на каждый адрес API: сменили провайдера — ключ прежнего остался
}

// Mode — режим ассистента: после фразы включения все вопросы к «Алисе» уходят модели, без «спроси GPT».
type Mode struct {
	Enter      []string `json:"enter"`       // фразы включения
	Exit       []string `json:"exit"`        // фразы выключения
	TimeoutMin int      `json:"timeout_min"` // минут тишины до автоотключения
	FollowUp   bool     `json:"follow_up"`   // после ответа слушать продолжение без «Алисы»
}

// keyID — один и тот же провайдер при любом написании адреса: без хвостового «/» и регистра хоста.
func keyID(base string) string {
	return strings.ToLower(strings.TrimRight(strings.TrimSpace(base), "/"))
}

// keyFor — ключ для адреса; старые настройки хранили его прямо в llm/tts.
func (c Config) keyFor(base, legacy string) string {
	if k := c.Keys[keyID(base)]; k != "" {
		return k
	}
	return legacy
}

// Без «умной колонки» в промте: иначе модели воображают себя пультом умного дома и отказываются от вопросов.
const defaultPrompt = "Ты эрудированный собеседник и отвечаешь на любые вопросы. Отвечай по-русски и коротко: " +
	"одно-три предложения разговорной речью. Ответ прочитают вслух, поэтому без списков, разметки, эмодзи, ссылок и формул."

func defaults() Config {
	return Config{
		STT:        STT{Provider: "alice", Language: "ru"},
		Microphone: Microphone{Source: "alice", Threshold: 300, Gain: 32, SilenceMS: 900},
		Triggers:   []string{"спроси gpt", "спроси джипити", "спроси чат gpt", "спроси нейросеть"},
		LLM:        LLM{SystemPrompt: defaultPrompt, MaxTokens: 500, History: 6},
		TTS:        TTS{Provider: "alice", Model: "tts-1", Speed: 1},
		LED:        LED{Enabled: true, Color: "#ff8a00"},
		Mode: Mode{Enter: []string{"переключись на gpt", "переключись на ассистента", "включи ассистента"},
			Exit:       []string{"отключись от ассистента", "выключи ассистента", "верни алису", "вернись к алисе"},
			TimeoutMin: 10, FollowUp: true},
		Radio: Radio{Voice: true, Play: []string{"включи станцию", "включи радиостанцию", "поставь станцию"},
			Stop: []string{"выключи станцию", "выключи радиостанцию", "останови станцию"}},
		Skills: Skills{Time: true},
	}
}

func loadConfig(path string) (Config, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return defaults(), err
	}
	return parseConfig(b)
}

func parseConfig(b []byte) (Config, error) {
	c := defaults()
	if strings.TrimSpace(string(b)) == "" {
		return c, nil
	}
	if err := json.Unmarshal(b, &c); err != nil {
		return defaults(), err
	}
	c.LLM.APIKey = c.keyFor(c.LLM.BaseURL, c.LLM.APIKey)
	c.TTS.APIKey = c.keyFor(c.TTS.BaseURL, c.TTS.APIKey)
	c.STT.APIKey = c.keyFor(c.STT.BaseURL, c.STT.APIKey)
	if c.Microphone.Gain < 1 || c.Microphone.Gain > 128 {
		c.Microphone.Gain = 32
	}
	if c.Microphone.Threshold < 50 || c.Microphone.Threshold > 10000 {
		c.Microphone.Threshold = 300
	}
	if c.Microphone.SilenceMS < 300 || c.Microphone.SilenceMS > 3000 {
		c.Microphone.SilenceMS = 900
	}
	if len(c.Mode.Enter) == 0 {
		c.Mode.Enter = defaults().Mode.Enter
	}
	if len(c.Mode.Exit) == 0 {
		c.Mode.Exit = defaults().Mode.Exit
	}
	if len(c.Radio.Play) == 0 {
		c.Radio.Play = defaults().Radio.Play
	}
	if len(c.Radio.Stop) == 0 {
		c.Radio.Stop = defaults().Radio.Stop
	}
	if c.Mode.TimeoutMin <= 0 {
		c.Mode.TimeoutMin = 10
	}
	if len(c.Triggers) == 0 {
		c.Triggers = defaults().Triggers
	}
	if strings.TrimSpace(c.LLM.SystemPrompt) == "" {
		c.LLM.SystemPrompt = defaultPrompt
	}
	if c.LLM.MaxTokens <= 0 {
		c.LLM.MaxTokens = 500
	}
	if c.LLM.History < 0 {
		c.LLM.History = 0
	}
	if c.TTS.Speed <= 0 {
		c.TTS.Speed = 1
	}
	if c.Weather.Lat < -90 || c.Weather.Lat > 90 || c.Weather.Lon < -180 || c.Weather.Lon > 180 {
		c.Weather.Lat, c.Weather.Lon = 0, 0
	}
	return c, nil
}
