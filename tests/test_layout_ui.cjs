const fs = require('fs'), assert = require('assert');
const html = fs.readFileSync(require('path').join(__dirname, '../static/index.html'), 'utf8');

// 1. Сохранённый макет применяется до отрисовки — без мигания старой раскладки.
assert(html.includes("localStorage.getItem('ui.nav') === 'rail'"), 'макет должен читаться в <head>');
assert(html.includes("localStorage.getItem('ui.navCollapsed') === '1'"), 'свёрнутость должна читаться в <head>');

// 2. Переключатель в настройках и кнопка сворачивания в самом меню.
assert(html.includes('id="set-nav"'), 'в «Оформлении» нужен переключатель макета');
assert(html.includes('id="nav-collapse"'), 'нужна кнопка свернуть/развернуть');
assert(html.includes('class="nav-label"'), 'подписи меню должны скрываться отдельно от иконок');

// 3. Боковое меню — только компьютер и планшет; телефонные правила не трогаем.
const rail = html.indexOf(':root[data-nav="rail"] { --rail-w: 248px; }');
assert(rail > 0, 'нет стилей бокового меню');
assert(html.lastIndexOf('@media (min-width: 761px) {', rail) > 0, 'стили бокового меню должны быть только для ≥761px');
assert(!/data-nav="rail"[^\n]*@media \(max-width: 760px\)/.test(html));
assert(html.includes(':root[data-nav="rail"] body.has-minibar aside { bottom: 86px; }'), 'меню должно приподниматься над нижним плеером');

// 4. «Обзор»: третья колонка справа от плиток, «Состояние» — в один ряд.
assert(html.includes('class="ov-body"'), 'нужен каркас «Обзора» для третьей колонки');
assert(html.includes('class="ov-main"'), 'нужна основная часть «Обзора»');
assert(html.includes('id="wz-third"'), 'нужна третья колонка «Обзора»');
assert(html.includes("const ZONES = ['wz-top', 'wz-left', 'wz-right', 'wz-third'];"));
assert(html.includes(':root[data-nav="rail"] .ov-body { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 14px; align-items: start; }'));
assert(html.includes(':root[data-nav="rail"] #stat-row { grid-template-columns: repeat(4, minmax(0, 1fr)); }'));
assert(html.includes(':root[data-nav="rail"] .settings { max-width: none; }'), 'настройки должны занимать всю ширину');
assert(html.includes(':root[data-nav="rail"] #pl-body { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 14px; align-items: start; }'), 'в плеере должна быть третья колонка');
assert(html.includes(':root[data-nav="rail"] #pl-body [data-pl-sec="lyrics"] { grid-column: 2; grid-row: 1; height: 0; min-height: 100%; overflow: hidden; }'), 'текст песни — высотой с карточку плеера');
assert(html.includes('.lyrics .ln.cur { opacity: 1; font-weight: 700; font-size: 1.2em; }'), 'текущая строка текста крупнее');
assert(html.includes('.lyrics .ln { display: block; opacity: .45; transition: opacity .28s ease, font-size .32s ease; }'), 'у текущей строки плавная анимация');
assert(html.includes(':root[data-nav="rail"][data-nav-collapsed="1"] aside { scrollbar-width: none; }'), 'в свёрнутом меню скроллбар скрыт');
assert(html.includes('@container (max-width: 427px)'), 'кнопки плеера уходят под обложку только когда не помещаются');
// 6. Провайдеры музыки: Яндекс по умолчанию, Сбер Звук — переключателем.
assert(html.includes('id="music-provider"'), 'нужен переключатель провайдеров музыки');
assert(html.includes('id="pl-zvuk"'), 'нужна секция «Сбер Звука»');
assert(html.includes('body[data-provider="zvuk"] #pl-zvuk { display: flex; }'), 'секция «Звука» показывается только для своего провайдера');
assert(html.includes("api('/api/music/provider'"), 'переключение провайдера');
assert(html.includes("api('/api/zvuk/token'"), 'токен «Звука»');
assert(html.includes('body[data-provider="zvuk"] #tab-player #pl-body #pl-zvuk { grid-column: 1 / -1; grid-row: 2; }'), 'блоки «Звука» — под плеером и текстом песни');
assert(!html.includes('body[data-provider="zvuk"] .pl-body-cols,'), 'текст песни «Звука» не скрывается');
assert(html.includes('id="zvuk-mood-pad"') && html.includes('id="zvuk-artist-card"'), 'настроение и исполнитель');
assert(html.includes(':root:not([data-nav="rail"]) body[data-provider="zvuk"] #tab-player #pl-body {'), 'в обычном макете текст Звука — не на всю ширину');
assert(html.includes('grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px; align-items: start; }'), 'обычный макет Звука: текст слева, блоки справа');
assert(html.indexOf('id="zvuk-mood-card"') < html.indexOf('id="zvuk-tracks-card"'), 'настроение — над плейлистом');
assert(html.includes('id="zvuk-popular"'), 'ползунок популярности');
assert(html.includes('body:not([data-provider="zvuk"]) #zvuk-artist-card'), 'исполнитель скрыт у Яндекса');
assert(html.includes('grid-column: 1 / -1; grid-row: 3;'), 'исполнитель — на всю ширину снизу');
assert(!html.includes('body[data-provider="zvuk"] [data-mu="dislike"],'), 'дизлайк у Звука не скрыт');
assert(html.includes('const rowAct = kind === \'queue\'') && html.includes('role="button"'), 'строка трека играет по клику');
assert(html.includes('data-zv-list="favorites"'), 'вкладка «Избранное»');
assert(html.includes('zvuk.mood'), 'настроение и популярность сохраняются');
assert(html.includes('api/zvuk/cover'), 'обложка для цвета идёт через панель');
assert(html.includes(':root[data-nav="rail"] body[data-provider="zvuk"] #tab-player #pl-body #pl-zvuk .zvuk-grid'), 'с боковым меню: плейлист слева, настроение справа');
assert(!html.includes('data-zv-play="${i}" title="Играть"'), 'кнопка воспроизведения в строках не нужна');
assert(html.includes('data-zv-add="${i}"'), 'кнопка «в очередь» осталась');
assert(html.includes('const ZVUK_LIST_LIMIT = 10'), 'длинный плейлист — окно из 10 строк');
assert(html.includes('data-zv-more'), 'кнопка открыть/свернуть плейлист');
assert(html.includes('id="zvuk-genre-btn"') && html.includes('id="zvuk-genre-menu"'), 'жанры: компактный селектор с галочками');
assert(html.includes('<option value="foreign">Зарубежное</option>')
  && html.includes('<option value="instrumental">Без слов</option>'), 'язык: любой/русское/зарубежное/без слов');
assert(html.includes("vocal: instr ? 0 : 1"), '«Без слов» — vocal: 0');
assert(html.includes('<option value="">Средняя</option>') && html.includes('zvukPopularOption'), 'популярность: три положения, средняя — NULL');
assert(html.includes('...zvukWaveOptions()'), 'жанр и язык уходят в поток');
assert(html.includes("walk(zvukData || {}, 'zvuk.')") && html.includes("walk(mu.track, 'track.')"), 'в «всё о треке» — полные данные Звука и Яндекса');

// 5. У режимов свои раскладки, старый вид не перезаписывается.
assert(html.includes("const layoutKey = () => navMode() === 'rail' ? 'ov.layout.rail' : 'ov.layout';"));
assert(html.includes("'wz-third': ['bt', 'sounds', 'device']"), 'раскладка с боковым меню — три колонки');
assert(html.includes("'wz-third': [],"), 'классическая раскладка третью колонку не использует');
assert(html.includes("matchMedia('(min-width: 761px)').matches ? 'rail' : 'classic'"), 'на телефоне остаётся классическая раскладка');

assert(html.includes('id="set-accent-custom"') && html.includes('id="accent-pop"'), 'свой цвет: кружок-радуга и попап');
assert(html.includes('id="as-tts-preset"') && html.includes('FishAudio'), 'пресет FishAudio у своего синтезатора');
assert(html.includes('data-preset="https://api.orcarouter.ai/v1"'), 'OrcaRouter в стандартных моделях');
assert(html.includes('accentOn(accent)') && html.includes('--on-accent'), 'текст на акценте — чёрный/белый по яркости');

console.log('Боковое меню включается в настройках, только для широкого экрана и не меняет телефон');
