package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestSkillTextNormalizesPhrases(t *testing.T) {
	for in, want := range map[string]string{
		"Алиса, скажи пожалуйста, который час?": "который час",
		"  КАКАЯ   ПОГОДА  ":                    "какая погода",
		"Сколько времени?":                      "сколько времени",
	} {
		if got := skillText(in); got != want {
			t.Errorf("skillText(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestTimeAndDateSkills(t *testing.T) {
	d := &Daemon{}
	c := Config{Skills: Skills{Time: true}}
	now := time.Date(2026, 11, 6, 15, 42, 0, 0, time.Local)
	// Подменяем «сейчас» через фиксированный текстовый формат: проверяем сами шаблоны и склонения.
	if got := clockText(now); got != "15 часов 42 минуты" {
		t.Fatalf("clockText: %q", got)
	}
	if got := clockText(time.Date(2026, 11, 6, 15, 0, 0, 0, time.Local)); got != "15 часов ровно" {
		t.Fatalf("clockText ровно: %q", got)
	}
	if got := dateText(now); got != "6 ноября, пятница" {
		t.Fatalf("dateText: %q", got)
	}
	for _, q := range []string{"который час", "Сколько времени?", "какое сегодня число", "какой день недели"} {
		if _, ok := d.localAnswer(context.Background(), c, q); !ok {
			t.Errorf("не распознан локальный вопрос %q", q)
		}
	}
	for _, q := range []string{"который час в Лондоне", "расскажи, как узнать время", "какая погода"} {
		if _, ok := d.localAnswer(context.Background(), c, q); ok {
			t.Errorf("ложное срабатывание на %q", q)
		}
	}
}

func TestMetSummary(t *testing.T) {
	const body = `{"properties":{"timeseries":[
	 {"time":"2026-11-07T09:00:00Z","data":{"instant":{"details":{"air_temperature":-1.0}},"next_1_hours":{"summary":{"symbol_code":"partlycloudy_day"}},"next_6_hours":{"summary":{"symbol_code":"cloudy"}}}},
	 {"time":"2026-11-07T10:00:00Z","data":{"instant":{"details":{"air_temperature":-3.0}},"next_1_hours":{"summary":{"symbol_code":"cloudy"}},"next_6_hours":{"summary":{"symbol_code":"cloudy"}}}},
	 {"time":"2026-11-07T12:00:00Z","data":{"instant":{"details":{"air_temperature":4.0}},"next_1_hours":{"summary":{"symbol_code":"fair_day"}},"next_6_hours":{"summary":{"symbol_code":"cloudy"}}}},
	 {"time":"2026-11-07T15:00:00Z","data":{"instant":{"details":{"air_temperature":1.0}},"next_1_hours":{"summary":{"symbol_code":"cloudy"}},"next_6_hours":{"summary":{"symbol_code":"cloudy"}}}},
	 {"time":"2026-11-08T09:00:00Z","data":{"instant":{"details":{"air_temperature":-5.0}},"next_1_hours":{"summary":{"symbol_code":"lightsnow"}},"next_6_hours":{"summary":{"symbol_code":"snow"}}}},
	 {"time":"2026-11-08T12:00:00Z","data":{"instant":{"details":{"air_temperature":1.0}},"next_1_hours":{"summary":{"symbol_code":"snow"}},"next_6_hours":{"summary":{"symbol_code":"snow"}}}},
	 {"time":"2026-11-08T15:00:00Z","data":{"instant":{"details":{"air_temperature":-2.0}},"next_1_hours":{"summary":{"symbol_code":"snow"}},"next_6_hours":{"summary":{"symbol_code":"snow"}}}}
	]}}`
	var f weatherData
	if err := json.Unmarshal([]byte(body), &f); err != nil {
		t.Fatal(err)
	}
	loc := time.FixedZone("MSK", 3*3600)
	now := time.Date(2026, 11, 7, 13, 0, 0, 0, loc)
	if got := metSummary(&f, now, false); got != "Сейчас -1, переменная облачность. Днём от -3 до +4, облачно." {
		t.Fatalf("сегодня: %q", got)
	}
	if got := metSummary(&f, now, true); got != "Завтра от -5 до +1, снег." {
		t.Fatalf("завтра: %q", got)
	}
	if !strings.Contains(metText("heavyrainandthunder_day"), "гроза") {
		t.Fatal("гроза должна распознаваться по подстроке thunder")
	}
}

func TestFetchWeatherParsesMet(t *testing.T) {
	var ua, lat, lon string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ua, lat, lon = r.Header.Get("User-Agent"), r.URL.Query().Get("lat"), r.URL.Query().Get("lon")
		_, _ = io.WriteString(w, `{"properties":{"timeseries":[{"time":"2026-11-07T09:00:00Z",`+
			`"data":{"instant":{"details":{"air_temperature":1.5}},"next_1_hours":{"summary":{"symbol_code":"clearsky_day"}}}}]}}`)
	}))
	defer srv.Close()
	old := weatherEndpoint
	weatherEndpoint = srv.URL
	defer func() { weatherEndpoint = old }()

	f, err := fetchWeather(context.Background(), Config{Weather: Weather{Lat: 55.79, Lon: 49.12}})
	if err != nil {
		t.Fatal(err)
	}
	if len(f.Properties.Timeseries) != 1 || f.Properties.Timeseries[0].Data.Instant.Details.Temperature != 1.5 {
		t.Fatalf("разбор: %+v", f)
	}
	if lat != "55.7900" || lon != "49.1200" {
		t.Fatalf("координаты запроса: %q,%q", lat, lon)
	}
	if !strings.Contains(ua, "yapanel-web") {
		t.Fatalf("MET Norway требует User-Agent, отправлено %q", ua)
	}
}

func TestWeatherSkillWithoutLocationExplainsSetup(t *testing.T) {
	d := &Daemon{}
	got, ok := d.localAnswer(context.Background(), Config{Skills: Skills{Weather: true}}, "какая погода")
	if !ok || !strings.Contains(got, "город") {
		t.Fatalf("без координат ждём подсказку про город, получили %q ok=%v", got, ok)
	}
	if _, ok := d.localAnswer(context.Background(), Config{Skills: Skills{Time: true, Weather: false}}, "какая погода"); ok {
		t.Fatal("выключенный навык не должен отвечать")
	}
	if metText("cloudy_day") != "облачно" {
		t.Fatalf("cloudy_day: %q", metText("cloudy_day"))
	}
}

// fixedTZif собирает минимальный TZif с одним фиксированным смещением — как для Windows в панели.
func fixedTZif(offset int32) []byte {
	var buf bytes.Buffer
	buf.WriteString("TZif")
	buf.Write(make([]byte, 16))
	for _, v := range []uint32{0, 0, 0, 0, 1, 6} { // isut, isstd, leap, timecnt, typecnt, charcnt
		_ = binary.Write(&buf, binary.BigEndian, v)
	}
	_ = binary.Write(&buf, binary.BigEndian, offset)
	buf.WriteByte(0) // isdst
	buf.WriteByte(0) // abbr index
	buf.WriteString("LOCAL\x00")
	return buf.Bytes()
}

func TestLocalZoneUsesCopiedTimezone(t *testing.T) {
	dir := t.TempDir()
	cfg := filepath.Join(dir, "config.json")
	if err := os.WriteFile(cfg, []byte("{}"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "localtime"), fixedTZif(3*3600), 0644); err != nil {
		t.Fatal(err)
	}
	d := &Daemon{cfgPath: cfg}
	if _, off := time.Now().In(d.localZone()).Zone(); off != 3*3600 {
		t.Fatalf("смещение %d, ожидали +3 ч (колонка в UTC)", off)
	}
}

func TestLocalZoneFallsBackToAlarmTimezone(t *testing.T) {
	root := t.TempDir()
	assist := filepath.Join(root, "assistant")
	alarm := filepath.Join(root, "alarm")
	for _, p := range []string{assist, alarm} {
		if err := os.MkdirAll(p, 0755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(alarm, "localtime"), fixedTZif(5*3600), 0644); err != nil {
		t.Fatal(err)
	}
	d := &Daemon{cfgPath: filepath.Join(assist, "config.json")}
	if _, off := time.Now().In(d.localZone()).Zone(); off != 5*3600 {
		t.Fatalf("смещение %d, ожидали +5 ч из часового пояса будильника", off)
	}
}

func TestLocalZoneWithoutFilesKeepsSystemTimezone(t *testing.T) {
	dir := t.TempDir()
	d := &Daemon{cfgPath: filepath.Join(dir, "config.json")}
	if d.localZone() != time.Local {
		t.Fatal("без файлов должен остаться системный часовой пояс")
	}
}
