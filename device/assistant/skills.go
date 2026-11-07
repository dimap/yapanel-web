package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Локальные ответы: короткие стандартные вопросы не уходят модели. Это не «навыки» в смысле
// фреймворка, а строгие шаблоны — всё, что не совпало целиком, идёт обычным путём.

var timePhrases = map[string]bool{
	"который час": true, "который сейчас час": true, "сколько времени": true, "сколько сейчас времени": true,
	"которое сейчас время": true, "сколько время": true, "текущее время": true,
}

var datePhrases = map[string]bool{
	"какое сегодня число": true, "какое число": true, "какое сейчас число": true,
	"какая сегодня дата": true, "какая дата": true, "какая сейчас дата": true,
	"какой сегодня день": true, "какой день недели": true, "какой сегодня день недели": true,
	"какой сейчас день недели": true,
}

var weatherNowPhrases = map[string]bool{
	"какая погода": true, "какая сейчас погода": true, "погода": true, "погода на улице": true,
	"какая погода на улице": true, "погода сегодня": true, "какая сегодня погода": true,
	"погода на сегодня": true, "что с погодой": true, "погода сейчас": true,
}

var weatherTomorrowPhrases = map[string]bool{
	"какая погода завтра": true, "погода на завтра": true, "какая завтра погода": true, "погода завтра": true,
	"какая будет погода завтра": true, "погода на завтрашний день": true,
}

// skillText приводит фразу к виду для сравнения: нижний регистр, ё→е, без пунктуации и лишних пробелов.
func skillText(s string) string {
	s = strings.ToLower(strings.ReplaceAll(s, "ё", "е"))
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= 'а' && r <= 'я', r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == ' ', r == '-':
			b.WriteRune(r)
		default:
			b.WriteRune(' ')
		}
	}
	out := strings.Join(strings.Fields(b.String()), " ")
	fillers := []string{"скажи ", "подскажи ", "пожалуйста ", "плиз ", "алиса "}
	for {
		before := out
		for _, p := range fillers {
			out = strings.TrimPrefix(out, p)
		}
		out = strings.TrimSuffix(out, " пожалуйста")
		if out == before {
			break
		}
	}
	return strings.TrimSpace(out)
}

func ruPlural(n int, one, few, many string) string {
	n %= 100
	if n >= 11 && n <= 14 {
		return many
	}
	switch n % 10 {
	case 1:
		return one
	case 2, 3, 4:
		return few
	}
	return many
}

func clockText(t time.Time) string {
	h, m := t.Hour(), t.Minute()
	hours := fmt.Sprintf("%d %s", h, ruPlural(h, "час", "часа", "часов"))
	if m == 0 {
		return hours + " ровно"
	}
	return fmt.Sprintf("%s %d %s", hours, m, ruPlural(m, "минута", "минуты", "минут"))
}

var monthsGenitive = [...]string{"января", "февраля", "марта", "апреля", "мая", "июня",
	"июля", "августа", "сентября", "октября", "ноября", "декабря"}

var weekdays = [...]string{"воскресенье", "понедельник", "вторник", "среда", "четверг", "пятница", "суббота"}

func dateText(t time.Time) string {
	return fmt.Sprintf("%d %s, %s", t.Day(), monthsGenitive[t.Month()-1], weekdays[t.Weekday()])
}

// localZone — местный часовой пояс. Колонка живёт в UTC, а панель копирует свой /etc/localtime рядом с
// конфигом (как для будильника), поэтому берём TZif оттуда. Без файла остаётся системное время.
func (d *Daemon) localZone() *time.Location {
	dir := filepath.Dir(d.cfgPath)
	for _, path := range []string{
		filepath.Join(dir, "localtime"),
		filepath.Join(filepath.Dir(dir), "alarm", "localtime"),
	} {
		data, err := os.ReadFile(path)
		if err != nil || len(data) == 0 {
			continue
		}
		if loc, err := time.LoadLocationFromTZData("local", data); err == nil {
			return loc
		}
	}
	return time.Local
}

// localAnswer отвечает на стандартный вопрос без модели. ok=false — пусть отвечает модель.
func (d *Daemon) localAnswer(ctx context.Context, c Config, question string) (string, bool) {
	text := skillText(question)
	if text == "" {
		return "", false
	}
	if c.Skills.Time {
		now := time.Now().In(d.localZone())
		if timePhrases[text] {
			return "Сейчас " + clockText(now) + ".", true
		}
		if datePhrases[text] {
			return "Сегодня " + dateText(now) + ".", true
		}
	}
	if c.Skills.Weather {
		if weatherNowPhrases[text] {
			return d.weatherAnswer(ctx, c, false)
		}
		if weatherTomorrowPhrases[text] {
			return d.weatherAnswer(ctx, c, true)
		}
	}
	return "", false
}

type weatherData struct {
	Properties struct {
		Timeseries []weatherPoint `json:"timeseries"`
	} `json:"properties"`
}

type weatherPoint struct {
	Time time.Time `json:"time"`
	Data struct {
		Instant struct {
			Details struct {
				Temperature float64 `json:"air_temperature"`
			} `json:"details"`
		} `json:"instant"`
		Next1 struct {
			Summary struct {
				Symbol string `json:"symbol_code"`
			} `json:"summary"`
		} `json:"next_1_hours"`
		Next6 struct {
			Summary struct {
				Symbol string `json:"symbol_code"`
			} `json:"summary"`
		} `json:"next_6_hours"`
	} `json:"data"`
}

func (p weatherPoint) symbol() string {
	if p.Data.Next1.Summary.Symbol != "" {
		return p.Data.Next1.Summary.Symbol
	}
	return p.Data.Next6.Summary.Symbol
}

func (d *Daemon) weatherAnswer(ctx context.Context, c Config, tomorrow bool) (string, bool) {
	if c.Weather.Lat == 0 && c.Weather.Lon == 0 {
		// Навык включён, но точки нет: молча уходить в модель — хуже всего, пользователь не поймёт причину.
		return "Чтобы я отвечал на вопросы о погоде, укажите город в панели — раздел «Локально».", true
	}
	data, err := fetchWeather(ctx, c)
	if err != nil {
		log.Printf("погода: %v", err)
		return "Не удалось узнать погоду. Подробности — в панели, раздел «Локально».", true
	}
	summary := metSummary(data, time.Now().In(d.localZone()), tomorrow)
	if summary == "" {
		log.Printf("погода: в прогнозе нет данных")
		return "Не удалось узнать погоду. Подробности — в панели, раздел «Локально».", true
	}
	return summary, true
}

// metSummary собирает ответ из почасового прогноза MET Norway: сейчас, остаток дня и завтра.
func metSummary(data *weatherData, now time.Time, tomorrow bool) string {
	ts := data.Properties.Timeseries
	if len(ts) == 0 {
		return ""
	}
	loc := now.Location()
	if tomorrow {
		min, max, sym, ok := metSpan(ts, now, now.AddDate(0, 0, 1).Format("2006-01-02"), loc)
		if !ok {
			return ""
		}
		return fmt.Sprintf("Завтра от %s до %s, %s.", tempText(min), tempText(max), metText(sym))
	}
	cur := ts[0]
	parts := []string{fmt.Sprintf("Сейчас %s, %s.", tempText(cur.Data.Instant.Details.Temperature), metText(cur.symbol()))}
	min, max, sym, ok := metSpan(ts, now, now.Format("2006-01-02"), loc)
	if ok {
		parts = append(parts, fmt.Sprintf("Днём от %s до %s, %s.", tempText(min), tempText(max), metText(sym)))
	}
	return strings.Join(parts, " ")
}

// metSpan — минимум/максимум и преобладающий символ за остаток указанного дня (по местному времени).
func metSpan(ts []weatherPoint, now time.Time, date string, loc *time.Location) (float64, float64, string, bool) {
	var min, max float64
	var noon weatherPoint
	best, count := 99, 0
	found := false
	for _, p := range ts {
		t := p.Time.In(loc)
		if t.Format("2006-01-02") != date || t.Before(now) {
			continue
		}
		v := p.Data.Instant.Details.Temperature
		if !found {
			min, max, found = v, v, true
		} else {
			if v < min {
				min = v
			}
			if v > max {
				max = v
			}
		}
		count++
		if d := absInt(t.Hour() - 12); d < best {
			best, noon = d, p
		}
	}
	if count < 3 {
		return 0, 0, "", false
	}
	sym := noon.Data.Next6.Summary.Symbol
	if sym == "" {
		sym = noon.symbol()
	}
	return min, max, sym, true
}

func absInt(n int) int {
	if n < 0 {
		return -n
	}
	return n
}

// weatherEndpoint вынесен, чтобы тесты подменяли его на локальный сервер.
var weatherEndpoint = "https://api.met.no/weatherapi/locationforecast/2.0/compact"

// checkWeather — диагностика для панели: тот же запрос, что у навыка, но с причиной ошибки в stdout.
// point — «lat,lon» из формы панели, чтобы проверять до сохранения.
func checkWeather(cfgPath, point string) {
	c, err := loadConfig(cfgPath)
	if err != nil {
		fmt.Println("ошибка чтения настроек: " + err.Error())
		return
	}
	if strings.TrimSpace(point) != "" {
		parts := strings.SplitN(point, ",", 2)
		if len(parts) != 2 {
			fmt.Println("ошибка: координаты в виде lat,lon")
			return
		}
		c.Weather.Lat, _ = strconv.ParseFloat(strings.TrimSpace(parts[0]), 64)
		c.Weather.Lon, _ = strconv.ParseFloat(strings.TrimSpace(parts[1]), 64)
	}
	if c.Weather.Lat == 0 && c.Weather.Lon == 0 {
		fmt.Println("ошибка: не заданы координаты")
		return
	}
	data, err := fetchWeather(context.Background(), c)
	if err != nil {
		fmt.Println("ошибка: " + err.Error())
		return
	}
	clock := &Daemon{cfgPath: cfgPath}
	answer := metSummary(data, time.Now().In(clock.localZone()), false)
	if answer == "" {
		fmt.Println("ошибка: в прогнозе нет данных")
		return
	}
	fmt.Println("ok: " + answer)
}

func fetchWeather(ctx context.Context, c Config) (*weatherData, error) {
	q := url.Values{}
	q.Set("lat", strconv.FormatFloat(c.Weather.Lat, 'f', 4, 64))
	q.Set("lon", strconv.FormatFloat(c.Weather.Lon, 'f', 4, 64))
	hc, err := client(c.LLM.Proxy, 10*time.Second)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, "GET", weatherEndpoint+"?"+q.Encode(), nil)
	if err != nil {
		return nil, err
	}
	// MET Norway просит представляться в User-Agent — иначе могут ответить 403.
	req.Header.Set("User-Agent", "yapanel-web/"+version+" (+https://github.com/dimap/yapanel-web)")
	resp, err := hc.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 512<<10))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode/100 != 2 {
		return nil, fmt.Errorf("MET Norway ответил %d", resp.StatusCode)
	}
	var data weatherData
	if err = json.Unmarshal(body, &data); err != nil {
		return nil, err
	}
	return &data, nil
}

func tempText(v float64) string {
	n := int(math.Round(v))
	if n > 0 {
		return fmt.Sprintf("+%d", n)
	}
	return strconv.Itoa(n)
}

// metText переводит symbol_code MET Norway в короткую русскую фразу для озвучки.
func metText(symbol string) string {
	s := strings.ToLower(symbol)
	for _, suffix := range []string{"_day", "_night", "_polartwilight"} {
		s = strings.TrimSuffix(s, suffix)
	}
	if strings.Contains(s, "thunder") {
		return "гроза"
	}
	switch s {
	case "clearsky":
		return "ясно"
	case "fair":
		return "преимущественно ясно"
	case "partlycloudy":
		return "переменная облачность"
	case "cloudy":
		return "облачно"
	case "fog":
		return "туман"
	case "lightrain":
		return "небольшой дождь"
	case "rain":
		return "дождь"
	case "heavyrain":
		return "сильный дождь"
	case "lightrainshowers":
		return "небольшой ливень"
	case "rainshowers":
		return "ливень"
	case "heavyrainshowers":
		return "сильный ливень"
	case "lightsleet":
		return "небольшая морось"
	case "sleet":
		return "мокрый снег"
	case "heavysleet":
		return "сильный мокрый снег"
	case "lightsnow":
		return "небольшой снег"
	case "snow":
		return "снег"
	case "heavysnow":
		return "сильный снег"
	case "lightsnowshowers":
		return "небольшой снегопад"
	case "snowshowers":
		return "снегопад"
	case "heavysnowshowers":
		return "сильный снегопад"
	default:
		return "без осадков"
	}
}
