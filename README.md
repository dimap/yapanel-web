<div align="center">

<img src="static/icons/icon-192.png" width="76" alt="Иконка yapanel">

# yapanel

**Панель управления Яндекс Станцией Макс**

Музыка, звук, свет, микрофоны, LED-экран и root-инструменты — в одном окне.  
Работает на самой колонке или на компьютере в той же Wi-Fi-сети.

[![Release](https://img.shields.io/github/v/release/Timobzdecc/yapanel-web_max?style=for-the-badge&logo=github&label=release)](https://github.com/Timobzdecc/yapanel-web_max/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-blue?style=for-the-badge)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Station%20Max-ff6a00?style=for-the-badge)](https://github.com/Timobzdecc/yapanel-web_max)

</div>

![Обзор панели управления Яндекс Станцией Макс](docs/screenshots/readme-overview.png)

> [!WARNING]
> Неофициальный проект, не связан с Яндексом и не одобрен им. Панель работает только на колонке, где **уже получен root-доступ по adb**, и сама колонку не рутирует. Консоль и файловый менеджер дают полный root-доступ к системе — используйте осознанно.

## 🎉 Что нового в 0.3.10

**Звуки Bluetooth, галерея анимаций и кольцо, которое снова светится.**

- **Звуки Bluetooth** — свой звук включения и выключения, «тишина вместо фразы», загрузка через панель и автозапуск после перезагрузки.
- **Галерея анимаций** — 10 пресетов для LED-экрана 25×16 и 6 цветных для кольца. Живые превью, установка на загрузку и системные анимации.
- **Кольцо починено** — «На колонке» больше не падает, установка показывает «изменена», после ребута ничего не откатывается.
- **Компактный редактор кольца** — кадры с мини-кольцами, выбор точки кликом, пикер цвета и «Залить все».

**[Скачать 0.3.10](https://github.com/Timobzdecc/yapanel-web_max/releases/tag/v0.3.10)** · **[Полный чейнжлог](https://github.com/Timobzdecc/yapanel-web_max/blob/main/docs/release-notes/v0.3.10.md)**

## 📸 Скриншоты

<table>
  <tr>
    <td width="50%">
      <img src="docs/screenshots/readme-animations.png" alt="Галерея анимаций LED-экрана">
      <br><b>Анимации LED-экрана</b><br>
      <sub>10 готовых пресетов: портал, плазма, куб, огонь, цифровой дождь и другие.</sub>
    </td>
    <td width="50%">
      <img src="docs/screenshots/readme-ring.png" alt="Редактор анимаций кольца">
      <br><b>Кольцо 24 LED</b><br>
      <sub>Цветные пресеты, компактные кадры и правка цвета выбранной точки.</sub>
    </td>
  </tr>
  <tr>
    <td width="50%">
      <img src="docs/screenshots/readme-bluetooth.png" alt="Звуки Bluetooth в панели">
      <br><b>Звуки Bluetooth</b><br>
      <sub>Свои джинглы, тишина вместо фразы Алисы и автозапуск демона.</sub>
    </td>
    <td width="50%">
      <img src="docs/screenshots/readme-charts.png" alt="Графики температуры, памяти и нагрузки">
      <br><b>Мониторинг</b><br>
      <sub>Температура, память, нагрузка, Wi-Fi и диск — с историей за сутки.</sub>
    </td>
  </tr>
</table>

<details>
<summary><b>📱 Мобильная версия</b></summary>
<br>
<img src="docs/screenshots/readme-mobile.png" width="320" alt="Мобильная версия панели">
</details>

## Зачем это нужно

- **Всё в одном месте.** Музыка, громкость, Bluetooth, радио, свет, DSP, слова активации, LED-матрица и root-инструменты — в одном интерфейсе.
- **Живёт на самой колонке.** Не нужен включённый компьютер: панель стартует вместе со станцией и работает с любого устройства в сети.
- **Не зависит от облака.** Светомузыка общается с лампами Tuya напрямую по Wi-Fi, музыка и управление идут через локальные сервисы. Интернет пропал — панель и свет продолжают работать.
- **Даёт доступ к железу.** Аппаратный DSP усилителей Silergy, Hi-Fi-калибровка, ночной режим, клубный бас — всё в пару кликов.
- **Свои слова активации.** Колонку можно научить реагировать на «Алёша» или «Джарвис», не заменяя штатную Алису и не занимая микрофон вторым приложением.
- **Свой AI-ассистент.** GPT, Claude, DeepSeek, Ollama — ответы можно озвучивать голосом Алисы или внешним TTS.
- **Светомузыка и Ambilight.** 12 пресетов, пространственное расположение ламп и синхронизация света с фильмом.
- **Понятные root-инструменты.** Консоль, файлы, логи, температура и память — без запоминания adb-команд.

## 🚀 Установка

### Windows — самый простой путь

Python на компьютер ставить не нужно.

[![Windows x64](https://img.shields.io/badge/Windows-x64-0078D4?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/Timobzdecc/yapanel-web_max/releases/download/v0.3.10/yapanel-web-0.3.10-windows-x64.zip)
[![Windows arm64](https://img.shields.io/badge/Windows-arm64-0078D4?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/Timobzdecc/yapanel-web_max/releases/download/v0.3.10/yapanel-web-0.3.10-windows-arm64.zip)

1. Скачайте архив и распакуйте его.
2. Запустите **`install_to_max.bat`**.
3. Введите IP станции, например `192.168.1.100`.

Установщик сам проверит adb и root, подключится к станции, доустановит нужные библиотеки, зальёт панель в `/data/yapanel`, разложит файлы слов активации в `/data/wakeword` (включая `frida-inject`, negatives и jar), настроит автозапуск и откроет панель в браузере.

Если что-то пойдёт не так — нет ADB, root или Python — он напишет понятную ошибку и не закроет окно.

Нужны только **root** и **Python 3** на колонке. Отдельные `maxctl` и `YAM` для слов активации не требуются.

<details>
<summary><b>Ручная установка</b></summary>

### На самой колонке

Станция Макс — полноценный ARM-компьютер, панель на ней работает без внешнего сервера. Положите файлы в `/data/yapanel` и запустите:

```bash
adb shell "/data/python/bin/python3 /data/yapanel/server.py --host 0.0.0.0 --port 8765 --device local"
```

Автозапуск на Максе идёт через `adbd_watchdog.sh`, который при загрузке вызывает `/data/quasar/webpanel/boot_hook.sh`. Этот хук панель создаёт сама — править `rootkeeper.sh` не нужно. Включить автозапуск можно в настройках панели.

### На компьютере или сервере

**Windows**

```cmd
git clone https://github.com/Timobzdecc/yapanel-web_max.git
cd yapanel-web_max
start.bat
```

**Linux**

```bash
sudo apt update && sudo apt install -y python3 adb ffmpeg
git clone https://github.com/Timobzdecc/yapanel-web_max.git
cd yapanel-web_max
chmod +x start.sh
./start.sh 192.168.1.50
```

**macOS**

```bash
brew install python ffmpeg
brew install --cask android-platform-tools
git clone https://github.com/Timobzdecc/yapanel-web_max.git
cd yapanel-web_max
sh start.command
```

**Docker**

```yaml
services:
  panel:
    image: ghcr.io/timobzdecc/yapanel-web_max:latest
    container_name: yapanel-web-max
    network_mode: host
    environment:
      ADB_SERIAL: "192.168.1.50"  # IP вашей Станции Макс
      PANEL_PORT: "8765"
      PANEL_HTTPS_PORT: "8766"
    volumes:
      - ./data:/data
    restart: unless-stopped
```

</details>

## Что умеет панель

### 🎵 Музыка и звук

Трек, исполнитель, обложка, прогресс и перемотка. Тексты песен подтягиваются из Яндекс Музыки и [LRCLIB](https://lrclib.net) и подсвечиваются в такт — получается караоке. Ещё есть очередь, похожие треки, громкость по таблице Quasar или напрямую через микшер, Bluetooth-треки и интернет-радио.

### 🎛️ Звук и DSP

Панель общается с усилителями Silergy SY6045S напрямую по I2C и настраивает аппаратные фильтры. Отсюда — пресеты **Hi-Fi Studio Flat**, **Ночной**, **Deep Punch** и **Club Bass**, регулятор баса от −6 до +8 дБ, график АЧХ, отключение компрессора DRC и аккуратный ресемплинг Lossless 44.1 → 48 кГц. Всё доступно и через REST API.

### 💡 Светомузыка и Ambilight

Лампы Tuya, Smart Life, MOES и Gauss управляются локально, без облака. В панели 12 пресетов — от океана и костра до киберпанка и диско. Можно указать, где стоит каждая лампа, включить бегущие волны, «радар» и режим кинотеатра с синхронизацией по таймкоду фильма.

Для локального управления нужен `local_key` лампы. Его можно получить через [iot.tuya.com](https://iot.tuya.com) и `python -m tinytuya wizard`, из Home Assistant или найти лампы прямо в панели: «Светомузыка» → «Поиск в Wi-Fi».

### 🎙 Свои слова активации

Колонку можно научить слову «Алёша», «Джарвис» или любому другому. Записи и модель хранятся на станции в `/data/wakeword` и никуда не отправляются.

Активация идёт через штатный SDK: Frida-агент внутри `com.yandex.io.sdk` вызывает `startConversation()`. Микрофон остаётся у SDK, второй `AudioRecord` не создаётся, штатная Алиса продолжает работать. В панели есть запись, обучение с прогрессом, чувствительность, история срабатываний и автозапуск. API демона слушает только `127.0.0.1:8780`.

Автор оригинала — [tangenx](https://github.com/tangenx), проект [station-wakeword](https://github.com/tangenx/station-wakeword). Порт для Макса и интеграция в панель — доработка этого форка.

### 🤖 Свой AI-ассистент

Запросы можно отправлять в GPT-4o, Claude, DeepSeek, Groq, OpenRouter или локальные модели через Ollama / LM Studio. Ассистент слушает речевой стек Android, при триггерной фразе останавливает Алису и отвечает сам — голосом Алисы или внешним TTS.

### 💡 LED-матрица, кольцо и 3D-модель

Матрица 25×16 показывает время, погоду, текст и анимации, а во время музыки — эквалайзер. Кольцо из 24 RGB-светодиодов настраивается по цвету и яркости. В браузере есть живая 3D-модель станции: видно, что показывает экран и как светится кольцо.

### ⚙️ Root-инструменты

Консоль на xterm.js с PTY и автодополнением, файловый менеджер по `/data` и системным разделам, температура, память, Load Average, перезапуск Алисы, `logcat` и `dmesg`.

### 🔒 Безопасность

Пароль с PBKDF2, проверка Origin/CSRF и локальный HTTPS для микрофона браузера. Всё общение — по локальной сети.

## ⚙️ Параметры командной строки

| Аргумент | Переменная | По умолчанию | Описание |
|---|---|---|---|
| `--device` | `ADB_SERIAL` | Сохранённый адрес | IP станции, USB-серийник или `local` для запуска на колонке |
| `--find` | — | — | Найти станцию в сети |
| `--host` | `PANEL_HOST` | `127.0.0.1` | Интерфейс (`0.0.0.0` — доступ с других устройств) |
| `--port` | `PANEL_PORT` | `8765` | HTTP-порт панели |
| `--https-port` | `PANEL_HTTPS_PORT` | `8766` | HTTPS-порт для микрофона браузера (`0` — выключить) |
| `--data-dir` | `PANEL_DATA_DIR` | Папка с `server.py` | Где хранить настройки и метрики |
| `--open` | — | — | Открыть панель в браузере после старта |

## 🧪 Разработка

```bash
python -m pytest tests -q
node tests/test_ui_hardening.mjs
node tests/test_anim_presets.mjs
```

Панель — Python + ванильный JS без сборки: `server.py`, `api/`, `static/`. Устройство подключается через `adb`; на самой станции панель работает в режиме `--device local`.

## Лицензия и благодарности

* Код панели — **[MIT](LICENSE)**.
* База — **yapanel-web** / **[dimap](https://github.com/dimap/yandex-station-web)**, адаптированная под Станцию Макс.
* Вкладка «Активация» — проект **[station-wakeword](https://github.com/tangenx/station-wakeword)** автора **[tangenx](https://github.com/tangenx)**; порт для Макса, Frida-активация и интеграция в панель — доработка форка.
* `frida-inject` — [Frida](https://frida.re), LGPL-2.1.
* [three.js](https://threejs.org), [xterm.js](https://xtermjs.org), [QRCode.js](https://davidshimjs.github.io/qrcodejs/) — MIT.
* Тексты песен — [LRCLIB](https://lrclib.net).
* Протокол Glagol — спасибо сообществу [YandexStation для Home Assistant](https://github.com/AlexxIT/YandexStation).

*«Яндекс», «Алиса» и «Яндекс Станция» — товарные знаки ООО «Яндекс». Проект независимый и не связан с компанией.*
