const $ = s => document.querySelector(s);
let online = false;   // есть ли связь с колонкой (см. «подключение» внизу)
const $$ = s => [...document.querySelectorAll(s)];
const icon = (id, size = 16) => `<svg width="${size}" height="${size}" aria-hidden="true"><use href="#${id}"/></svg>`;

function toast(msg, err = false) {
  const t = $('#toast');
  t.textContent = msg; t.className = 'show' + (err ? ' err' : '');
  clearTimeout(toast.t); toast.t = setTimeout(() => t.className = '', err ? 5000 : Math.max(1800, msg.length * 55));
}

async function api(path, body, opts = {}) {
  const init = body === undefined ? {} : {
    method: 'POST',
    headers: opts.headers || { 'Content-Type': 'application/json' },
    body: opts.raw ? body : JSON.stringify(body),
  };
  const r = await PanelContext.fetch(path, init);
  const data = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok) {
    const e = new Error(data.error || r.statusText);
    // «нет связи» объяснит экран подключения — без тостов
    if (r.status === 401 && data.auth) { e.offline = true; connCheck(); throw e; }
    if (data.offline || /^(Колонка не ответила|Связь с колонкой потеряна)/.test(e.message)) { e.offline = true; connCheck(); }
    throw e;
  }
  return data;
}

async function act(btn, fn, okMsg) {
  if (btn) btn.disabled = true;
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; }
  catch (e) { if (!e.offline) toast(e.message, true); }
  finally { if (btn) btn.disabled = false; }
}

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// ---------- единый планировщик периодических задач ----------
// Все частые опросы идут через один таймер. Пока вкладка скрыта, задачи стоят
// и не дёргают колонку; при возврате выполняются сразу, не ожидая интервала.
const Scheduler = (() => {
  const tasks = new Map();
  let timer = 0, timerAt = 0, seq = 0;
  const now = () => Date.now();

  function arm(at) {
    if (document.hidden) return;
    if (timer && timerAt <= at) return;
    if (timer) clearTimeout(timer);
    timerAt = at;
    timer = setTimeout(tick, Math.max(0, at - now()));
  }
  function tick() {
    timer = 0; timerAt = 0;
    if (document.hidden) return;
    const t = now();
    for (const task of tasks.values()) {
      if (task.next > t) continue;
      task.next = t + task.ms;
      if (task.running) continue;       // долгий запрос не наслаивается сам на себя
      task.running = true;
      let r;
      try { r = task.fn(); } catch { task.running = false; continue; }
      if (r && typeof r.then === 'function') r.catch(() => {}).finally(() => { task.running = false; });
      else task.running = false;
    }
    // Пересчитываем после задач: setInterval мог изменить следующий запуск.
    let next = Infinity;
    for (const task of tasks.values()) next = Math.min(next, task.next);
    if (next !== Infinity) arm(next);
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { clearTimeout(timer); timer = 0; timerAt = 0; return; }
    const t = now();
    for (const task of tasks.values()) task.next = Math.min(task.next, t);
    arm(t);
  });

  function add(ms, fn) {
    const id = ++seq, delay = Math.max(16, +ms || 0);
    tasks.set(id, { ms: delay, fn, next: now() + delay, running: false });
    arm(tasks.get(id).next);
    return id;
  }
  function remove(id) { tasks.delete(id); }
  function setInterval(id, ms) {
    const task = tasks.get(id), delay = Math.max(16, +ms || 0);
    if (!task || task.ms === delay) return;
    task.ms = delay;
    task.next = now() + delay;       // новый интервал — от текущего момента, как setTimeout после ответа
    arm(task.next);
  }
  return { add, remove, setInterval, size: () => tasks.size };
})();
const every = (ms, fn) => Scheduler.add(ms, fn);

// Большие списки перерисовываем, только когда их разметка реально изменилась:
// иначе innerHTML на каждом опросе пересоздаёт узлы и сбрасывает фокус/выделение.
function setListHtml(el, html) {
  if (!el || el._listHtml === html) return false;
  el._listHtml = html;
  el.innerHTML = html;
  return true;
}
// Адрес обложки — в CSS. Кавычки и переводы строк экранируем, чтобы данные API
// не могли выйти из url("…") и добавить свои свойства.
const cssUrl = u => /^(https?:)?\/\//i.test(String(u || '')) ? `url("${String(u).replace(/["\\\n\r]/g, c => '\\' + c)}")` : '';
// Скрытые значения (Device ID, IP, Wi‑Fi, логин): точки, по нажатию — значение, по повторному — снова точки.
// Приватный режим (по умолчанию включён) — защита для тех, кто скриншотит панель не глядя; выключается в «Настроить».
const revealed = new Set();
let privateOff = false;
const DOTS = '••••••••';
const secret = (key, v, dots = DOTS) => {
  if (!v) return '—';
  const on = privateOff || revealed.has(key);
  return `<button class="secret${on ? ' shown' : ''}" data-key="${esc(key)}" data-v="${esc(v)}" data-dots="${esc(dots)}" title="Показать / скрыть" aria-label="Показать или скрыть значение">${on ? esc(v) : dots}</button>`;
};
function applyPrivate() {
  $$('.secret').forEach(x => {
    const on = privateOff || revealed.has(x.dataset.key);
    x.textContent = on ? x.dataset.v : x.dataset.dots || DOTS;
    x.classList.toggle('shown', on);
  });
}
// Имя хоста и Bluetooth-имя колонки кончаются куском серийного номера (Yandex-Lite-gen2-NJ0C) — его и прячем.
const hostHtml = h => {
  const m = /^(.*[-_])([A-Za-z0-9]{4,})$/.exec(h || '');
  return m ? esc(m[1]) + secret('serial', m[2], '••••') : esc(h || '—');
};
document.addEventListener('click', e => {
  const b = e.target.closest('.secret');
  if (!b) return;
  if (privateOff) return;
  revealed.has(b.dataset.key) ? revealed.delete(b.dataset.key) : revealed.add(b.dataset.key);
  applyPrivate();
});
const pctNum = v => Math.round(v / 255 * 100);
const pct = v => pctNum(v) + '%';
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

function splitUptime(s) {
  s = Math.floor(+s); const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  if (d) return [d, `д ${h} ч`];
  if (h) return [h, `ч ${m} мин`];
  return [m, 'мин'];
}

// ---------- статус ----------
let volDragging = false, lastOk = 0, status = null, ymLogin = '';
// ---------- плитки «Обзора» ----------
// Четыре слота, в каждом — что выбрано в «Настройках» → «Оформление» (ov.tiles). Значения — из статуса (раз в 5 с),
// процессор и место — из истории «Графиков» (её и так собирает сервер; обновляется с мини-графиками).
const TILES = {
  // span — наименьший размах графика: колебание на полградуса не должно рисоваться пиком во всю высоту
  temp: { label: 'Температура', spark: 'temp', span: 8 }, mem: { label: 'Память', spark: 'mem', span: 40 },
  load: { label: 'Нагрузка', spark: 'load', span: 2 }, cpu: { label: 'Процессор', spark: 'cpu', span: 30 },
  uptime: { label: 'Аптайм' }, rssi: { label: 'Wi‑Fi', spark: 'rssi', span: 12 }, disk: { label: 'Место в /data', spark: 'disk', span: 10 },
};
const TILE_DEFAULT = ['temp', 'mem', 'load', 'uptime'];
const tileVals = {};
function tileSlots() {
  try { const v = JSON.parse(store.get('ov.tiles')); if (Array.isArray(v) && v.length === 4 && v.every(k => TILES[k])) return v; } catch {}
  return TILE_DEFAULT;
}
const dec = x => String(x).replace('.', ',');
function tilesFromStatus(s) {
  const temp = s.temp ? s.temp / 1000 : null;
  tileVals.temp = { v: temp === null ? '—' : temp.toFixed(0), u: '°C', bar: temp === null ? 0 : temp / 85, meta: 'процессор' };
  const used = (s.mem_total - s.mem_avail) / 1024, total = s.mem_total / 1024;
  tileVals.mem = { v: Math.round(used), u: `из ${Math.round(total)} МБ`, bar: used / total, meta: `свободно ${Math.round(s.mem_avail / 1024)} МБ` };
  const rssi = +s.RSSI;
  tileVals.rssi = { v: s.RSSI ? String(rssi).replace('-', '−') : '—', u: 'дБм', bar: s.RSSI ? (rssi + 90) / 60 : 0,
    metaHtml: [s.ssid && { html: secret('ssid', s.ssid) }, s.freq && dec((s.freq / 1000).toFixed(1)) + ' ГГц', s.LINKSPEED && s.LINKSPEED + ' Мбит/с']
      .filter(Boolean).map(x => x.html ?? esc(x)).join(' · ') || '—' };
  const [uv, uu] = splitUptime(s.uptime), boot = new Date(Date.now() - (+s.uptime || 0) * 1000);
  tileVals.uptime = { v: uv, u: uu, bar: null, meta: 'включена ' + boot.toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) };
  const la = (s.load || '').split(' ');
  tileVals.load = { v: la[0] ? dec(la[0]) : '—', u: 'за минуту', bar: +la[0] / 4,   // 4 ядра: 4 — загружены все
    meta: la[1] ? `5 мин ${dec(la[1])} · 15 мин ${dec(la[2])}` : '—' };
  renderTiles();
}
function tilesFromMetrics(d) {
  const last = a => { for (let i = (a || []).length - 1; i >= 0; i--) if (a[i] != null) return a[i]; return null; };
  const cpu = last(d.cpu), disk = last(d.disk), dt = d.totals?.disk;
  tileVals.cpu = { v: cpu === null ? '—' : Math.round(cpu), u: '%', bar: cpu === null ? 0 : cpu / 100, meta: 'все ядра' };
  tileVals.disk = { v: disk === null ? '—' : Math.round(disk), u: dt ? `из ${dt} МБ` : 'МБ', bar: disk !== null && dt ? disk / dt : 0,
    meta: disk !== null && dt ? `свободно ${Math.round(dt - disk)} МБ` : 'занято' };
  renderTiles();
}
function buildTiles() {
  const row = $('#stat-row');
  $$('#stat-row > .stat').forEach(x => x.remove());
  row.insertAdjacentHTML('beforeend', tileSlots().map(k => `<div class="card stat" data-tile="${k}"${TILES[k].spark ? ` data-spark="${TILES[k].spark}"` : ''}>
    <div class="label">${esc(TILES[k].label)}</div><div class="value"><b>—</b><span></span></div><div class="bar"><i></i></div><div class="meta">—</div></div>`).join(''));
  renderTiles();
  if (sparkData) drawSparks(sparkData);
}
function renderTiles() {
  $$('#stat-row > .stat').forEach(el => {
    const t = tileVals[el.dataset.tile]; if (!t) return;
    el.querySelector('.value b').textContent = t.v;
    el.querySelector('.value span').textContent = t.u || '';
    el.querySelector('.bar').classList.toggle('none', t.bar === null);
    el.querySelector('.bar i').style.width = t.bar === null ? '0' : clamp(t.bar * 100, 0, 100) + '%';
    if (t.metaHtml !== undefined) el.querySelector('.meta').innerHTML = t.metaHtml; else el.querySelector('.meta').textContent = t.meta;
  });
}
function fillTileSettings() {
  const slots = tileSlots();
  $('#set-tiles').innerHTML = slots.map((k, i) => `<select class="field" data-slot="${i}" aria-label="Плитка ${i + 1}">${Object.entries(TILES)
    .map(([key, d]) => `<option value="${key}" ${key === k ? 'selected' : ''}>${esc(d.label)}</option>`).join('')}</select>`).join('');
}
$('#set-tiles').onchange = () => {
  store.set('ov.tiles', JSON.stringify($$('#set-tiles select').map(s => s.value)));
  buildTiles();
};

// ---------- мини-графики в плитках «Обзора» ----------
// Та же история, что у «Графиков» (сервер собирает её сам), — последние 15 минут; раз в 30 с, пока виден «Обзор».
let sparkData = null;
function sparkPath(vals, w, h, span = 0) {
  const xs = vals.map((v, i) => [i, v]).filter(([, v]) => v != null);
  if (xs.length < 2) return null;
  let lo = Math.min(...xs.map(x => x[1])), hi = Math.max(...xs.map(x => x[1]));
  if (hi - lo < span) { const mid = (hi + lo) / 2; lo = mid - span / 2; hi = mid + span / 2; }
  if (hi - lo < 1e-6) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * .15, X = i => i / (vals.length - 1) * w, Y = v => h - 4 - (v - lo + pad) / (hi - lo + 2 * pad) * (h - 8);
  let line = '', area = '', run = [];
  // сглаживание: Catmull-Rom → кубические кривые; без острых углов на скачках
  const smooth = pts => pts.map((p, k) => {
    if (!k) return `M${p[0].toFixed(1)},${p[1].toFixed(1)}`;
    const p0 = pts[k - 2] || pts[k - 1], p1 = pts[k - 1], p3 = pts[k + 1] || p;
    const c1 = [p1[0] + (p[0] - p0[0]) / 6, p1[1] + (p[1] - p0[1]) / 6], c2 = [p[0] - (p3[0] - p1[0]) / 6, p[1] - (p3[1] - p1[1]) / 6];
    return `C${c1.map(x => x.toFixed(1))} ${c2.map(x => x.toFixed(1))} ${p[0].toFixed(1)},${p[1].toFixed(1)}`;
  }).join('');
  const flush = () => {
    if (run.length > 1) {
      const pts = run.map(([i, v]) => [X(i), Y(v)]), seg = smooth(pts);
      line += seg; area += seg + `L${pts[pts.length - 1][0].toFixed(1)},${h}L${pts[0][0].toFixed(1)},${h}Z`;
    }
    run = [];
  };
  vals.forEach((v, i) => v == null ? flush() : run.push([i, v]));   // разрыв — колонки не было на связи
  flush();
  return { line, area };
}
async function loadSparks() {
  if (!online || document.hidden || $('#tab-overview').hidden) return;
  try {
    const d = sparkData = await api('/api/metrics?range=900');
    drawSparks(d);
    tilesFromMetrics(d);
  } catch {}
}
every(30000, loadSparks);
function drawSparks(d) {
    $$('#stat-row > .stat').forEach(tile => { if (!tile.dataset.spark) tile.querySelector('.spark')?.remove(); });
    $$('.stat[data-spark]').forEach(tile => {
      const p = sparkPath(d[tile.dataset.spark] || [], 300, 42, TILES[tile.dataset.tile]?.span);
      let svg = tile.querySelector('.spark');
      if (!p) { svg?.remove(); return; }
      if (!svg) tile.insertAdjacentHTML('afterbegin', '<svg class="spark" viewBox="0 0 300 42" preserveAspectRatio="none" aria-hidden="true"><path class="a"/><path class="l"/></svg>'), svg = tile.querySelector('.spark');
      svg.querySelector('.a').setAttribute('d', p.area);
      svg.querySelector('.l').setAttribute('d', p.line);
    });
}

async function refreshStatus() {
  try {
    const s = status = await api('/api/status');
    lastOk = Date.now();
    $('#conn').className = 'dot on';
    $('#conn-text').textContent = s.via === 'agent' ? 'Защищённое подключение' : 'Подключено по adb';
    $('#conn-ip').innerHTML = secret('ip', s.ip_address);

    tilesFromStatus(s);
    if (s.model && $('#model-name')) {
      const mn = { max: 'Станция Макс', midi: 'Станция Миди', lite: 'Станция Лайт', lite2: 'Станция Лайт 2', station2: 'Станция 2' }[s.model];
      if (mn) $('#model-name').textContent = mn;
    }
    if (s.model === 'max') loadDSP();
    window.__isMatrix = !!(s.matrix && s.matrix.cols === 25);
    try { localStorage.setItem('is_matrix', window.__isMatrix ? '1' : '0'); } catch {}
    const extrasEl = document.querySelector('.extras');
    let matrixChips = $('#matrix-chips');
    if (window.__isMatrix) {
      if (extrasEl) extrasEl.style.display = 'none';
      if (!matrixChips && extrasEl) {
        matrixChips = document.createElement('div');
        matrixChips.id = 'matrix-chips';
        matrixChips.className = 'chips';
        matrixChips.style.marginTop = '6px';
        matrixChips.innerHTML = '<span style="font-size:12px;opacity:0.6;margin-right:4px">Символы:</span>' +
          [':', '°', '-', '+', '*', 'ТЕСТ', 'АЛИСА'].map(x => `<button class="pill sm" type="button" data-sym="${x}">${x}</button>`).join('');
        extrasEl.parentNode.insertBefore(matrixChips, extrasEl.nextSibling);
        matrixChips.querySelectorAll('[data-sym]').forEach(b => {
          b.onclick = () => {
            const sym = b.dataset.sym;
            const input = $('#clock-text');
            if (sym === 'ТЕСТ' || sym === 'АЛИСА') input.value = sym;
            else input.value = (input.value || '') + sym;
            clockPreview();
          };
        });
      }
      if (!window.__matrixInitDone) {
        window.__matrixInitDone = true;
        clockPreview();
      }
    } else {
      if (extrasEl) extrasEl.style.display = 'flex';
      if (matrixChips) matrixChips.remove();
    }

    if (!volDragging) {
      const [m, b] = [s.vol_level, +s.vol_bt || 0];
      showLevel(m);
      volMuted = !!s.muted;
      renderMute();
      $('#vol-bt').max = 255; $('#vol-bt').value = b; $('#vol-bt-v').textContent = pct(b);
    }
    $('#ffmpeg-hint').textContent = s.ffmpeg ? 'ffmpeg найден — подходят любые форматы.' : 'ffmpeg не найден — подходят только WAV.';


    window.__deviceColor = s.color;
    window.modelColor?.(s.color);
    $('#dev-info').innerHTML = [
      ['Quasar', s.quasar === 'running' ? 'работает' : 'остановлен'],
      ['Версия', s.quasar_version], ['Device ID', { html: secret('id', s.device_id) }], ['Хост', { html: hostHtml(s.hostname) }],
      ['Ядро', s.kernel], ['MAC', { html: secret('mac', s.address) }], ['Время', s.date],
    ].map(([k, v]) => `<dt>${k}</dt><dd>${v?.html ?? esc(v ?? '—')}</dd>`).join('');
    updateSub();
  } catch {}   // о потере связи расскажет экран подключения
}
function updateSub() {
  if (!status) return;
  const ago = Math.round((Date.now() - lastOk) / 1000);
  const ver = (status.quasar_version || '').split('.').slice(0, 4).join('.');
  const alive = status.quasar === 'running';
  $('#ov-sub').innerHTML = `<span class="dot ${alive ? 'on' : 'off'}" title="${alive ? 'Алиса работает' : 'Алиса остановлена'}" style="display:inline-block;margin-right:6px;vertical-align:1px"></span>`
    + hostHtml(status.hostname) + esc(` · Quasar ${ver} · ${ago} с назад`);
}
every(1000, updateSub);

// ---------- громкость ----------
// Динамик — в уровнях Алисы 0–10 (как на колонке), Bluetooth — сырой микшер 0–255.
function showLevel(level) {
  const known = level != null && Number.isFinite(+level);
  $('#vol-master').disabled = !known;
  if (known) $('#vol-master').value = Math.round(level);
  $('#vol-big').textContent = known ? Math.round(level) : '—';
}
// Mute: выключаем звук, запоминая громкость на сервере; повторное нажатие возвращает её.
let volMuted = false;
function renderMute() {
  $$('#vol-mute, .vol-mute').forEach(b => {
    b.classList.toggle('on', volMuted);
    b.setAttribute('aria-pressed', volMuted ? 'true' : 'false');
    const title = volMuted ? 'Включить звук' : 'Выключить звук';
    b.title = title; b.setAttribute('aria-label', title);
  });
}
$$('#vol-mute, .vol-mute').forEach(b => b.onclick = () => {
  const mute = !volMuted;
  act(b, async () => {
    const r = await api('/api/mute', { on: mute });
    volMuted = !!r.muted;
    if (typeof r.volume === 'number') showLevel(r.volume);
    renderMute();
  }, mute ? 'Звук выключен' : 'Звук включён');
});
function unmuteUi(level) {
  if (level > 0 && volMuted) { volMuted = false; renderMute(); }
}
let volTimer;
for (const [id, key] of [['#vol-master', 'level'], ['#vol-bt', 'bt']]) {
  const el = $(id);
  el.addEventListener('input', () => {
    volDragging = true;
    if (key === 'level') { showLevel(+el.value); unmuteUi(+el.value); }
    else $('#vol-bt-v').textContent = pct(el.value);
    clearTimeout(volTimer);
    volTimer = setTimeout(() => act(null, () => api('/api/volume', { [key]: +el.value })), 150);
  });
  el.addEventListener('change', () => setTimeout(() => volDragging = false, 1500));
}
$$('[data-level]').forEach(b => b.onclick = () => act(b, async () => {
  await api('/api/volume', { level: +b.dataset.level });
  showLevel(+b.dataset.level);
  unmuteUi(+b.dataset.level);
}));

// ---------- звуки ----------
let sounds = [], sndFolder = null, sndCustom = new Set(), sndTarget = null;
const FOLDER_NAMES = { '': 'Корень', common: 'Общие', 'ru-RU': 'Русский', 'ar-EG': 'Арабский (Египет)', 'ar-SA': 'Арабский (Саудовская Аравия)', 'kk-KZ': 'Казахский' };
const folderOf = n => n.includes('/') ? n.slice(0, n.lastIndexOf('/')) : '';
function renderFolders() {
  const counts = {};
  sounds.forEach(n => counts[folderOf(n)] = (counts[folderOf(n)] || 0) + 1);
  const folders = Object.keys(counts).sort((a, b) => (a === 'common' ? -1 : b === 'common' ? 1 : a.localeCompare(b)));
  if (!folders.includes(sndFolder)) sndFolder = folders.includes(store.get('snd.folder')) ? store.get('snd.folder') : folders[0];
  $('#snd-folders').innerHTML = folders.map(f =>
    `<button data-f="${esc(f)}" class="${f === sndFolder ? 'active' : ''}" aria-pressed="${f === sndFolder}">${esc(FOLDER_NAMES[f] || f)}<span>${counts[f]}</span></button>`).join('');
  $$('#snd-folders button').forEach(b => b.onclick = () => { sndFolder = b.dataset.f; store.set('snd.folder', sndFolder); renderFolders(); renderSounds(); });
}
function renderSounds() {
  const f = $('#snd-filter').value.toLowerCase();
  // С поиском ищем по всем папкам и показываем папку рядом с именем.
  const list = f ? sounds.filter(n => n.toLowerCase().includes(f)) : sounds.filter(n => folderOf(n) === sndFolder);
  $('#snd-folders').style.opacity = f ? .5 : 1;
  const html = list.length ? list.map(n => {
    const base = n.slice(n.lastIndexOf('/') + 1);
    const m = base.match(/^(.*)\.([^.]+)$/) || [base, base, ''];
    const dir = folderOf(n);
    const own = sndCustom.has(n);
    return `<div class="sound"><button class="play" data-s="${esc(n)}" aria-label="Проиграть ${esc(m[1])}">${icon('i-play', 14)}</button>
      <div class="nm" title="${esc(n)}">${esc(m[1])}</div>${own ? '<span class="badge ok">свой</span>' : ''}
      <span class="ext">${f && dir ? esc(dir) + ' · ' : ''}${esc(m[2])}</span>
      <span class="acts">${own ? `<button class="btn sm icon ghost" data-snd-orig="${esc(n)}" title="Послушать стандартный" aria-label="Послушать стандартный ${esc(m[1])}">${icon('i-music', 14)}</button>
        <button class="btn sm icon ghost" data-snd-reset="${esc(n)}" title="Вернуть стандартный" aria-label="Вернуть стандартный ${esc(m[1])}">${icon('i-refresh', 14)}</button>` : ''}
        <button class="btn sm icon ghost" data-snd-replace="${esc(n)}" title="Заменить своим" aria-label="Заменить ${esc(m[1])} своим">${icon('i-ul', 14)}</button></span></div>`;
  }).join('') : '<div class="empty">Ничего не найдено</div>';
  if (!setListHtml($('#sounds'), html)) return;
  $$('#sounds .play').forEach(b => b.onclick = () => act(b, () => api('/api/play', { name: b.dataset.s }), '▶ ' + b.dataset.s));
  $$('#sounds [data-snd-orig]').forEach(b => b.onclick = () => act(b, () => api('/api/sounds/play-original', { name: b.dataset.sndOrig }), '▶ стандартный'));
  $$('#sounds [data-snd-reset]').forEach(b => b.onclick = () => act(b, async () => setSndCustom(await api('/api/sounds/reset', { name: b.dataset.sndReset })), 'Вернул стандартный звук'));
  $$('#sounds [data-snd-replace]').forEach(b => b.onclick = () => { sndTarget = b.dataset.sndReplace; $('#snd-replace-file').click(); });
}
function setSndCustom(r) {
  sndCustom = new Set(r.custom || []);
  renderSounds();
  if (!$('#tab-boot').hidden) loadBoot();   // звук запуска показывает и вкладка «Анимации»
}
// ---------- сказать на колонке ----------
const SAY_HINT = {
  alice: 'Голосом Алисы («Повтори за мной») — без входа в Яндекс, но нужен интернет: Алиса озвучивает через своё облако.',
  assistant: 'Голосом ассистента — его синтезатор из вкладки «Ассистент» (или тоже голос Алисы, если он так настроен).',
};
async function loadSay() {
  const a = await api('/api/assistant').catch(() => null);
  $('#say-voice').hidden = !(a?.installed && a?.running);
  if ($('#say-voice').hidden) $('#say-voice').value = 'alice';
  else $('#say-voice').value = store.get('say.voice') || 'alice';
  $('#say-hint').textContent = SAY_HINT[$('#say-voice').value];
  await loadPhrases();
}
// ---------- свои фразы ----------
let phrases = [];
const SAY_METHODS = { glagol: 'голос Алисы', tts: 'TTS (Google)', maind: 'maind', assistant: 'ассистент' };
function renderPresets() {
  const box = $('#say-presets');
  box.innerHTML = phrases.map(p =>
    `<span style="display:inline-flex;align-items:center;gap:4px">` +
    `<button class="pill" data-phrase="${esc(p.id)}" title="${esc(p.text)}">${esc(p.name)}</button>` +
    `<button class="pill" data-phrase-del="${esc(p.id)}" title="Удалить" aria-label="Удалить ${esc(p.name)}">×</button></span>`).join('');
  $$('#say-presets [data-phrase]').forEach(b => b.onclick = () => act(b, async () => {
    const r = await api('/api/phrases/say', { id: b.dataset.phrase });
    toast('🗣 ' + (SAY_METHODS[r.method] || r.method || 'сказано'));
  }));
  $$('#say-presets [data-phrase-del]').forEach(b => b.onclick = () => act(b, async () => {
    setPhrases(await api('/api/phrases', { phrases: phrases.filter(p => p.id !== b.dataset.phraseDel) }));
  }, 'Фраза удалена'));
}
function setPhrases(r) { phrases = r.phrases || []; renderPresets(); }
function applyGreeting(g) {
  g = g || {};
  $('#greet-on').checked = !!g.on;
  $('#greet-text').value = g.text || '';
  $('#greet-delay').value = g.delay || 45;
}
async function loadPhrases() {
  const r = await api('/api/phrases').catch(() => null);
  if (!r) return;
  setPhrases(r);
  applyGreeting(r.greeting);
}
$('#preset-add').onclick = () => act($('#preset-add'), async () => {
  const text = $('#preset-text').value.trim(); if (!text) return toast('Введите текст фразы', true);
  const name = $('#preset-name').value.trim() || 'Фраза';
  setPhrases(await api('/api/phrases', { phrases: phrases.concat([{ name, text }]) }));
  $('#preset-name').value = ''; $('#preset-text').value = '';
}, 'Фраза сохранена');
$('#greet-save').onclick = () => act($('#greet-save'), async () => {
  const r = await api('/api/phrases/greeting', {
    on: $('#greet-on').checked, text: $('#greet-text').value.trim(), delay: +$('#greet-delay').value || 45,
  });
  applyGreeting(r.greeting);
}, 'Приветствие сохранено');
$('#greet-say').onclick = () => act($('#greet-say'), async () => {
  const r = await api('/api/phrases/greeting/say');
  toast('🗣 ' + (SAY_METHODS[r.method] || r.method || 'сказано'));
}, null);
$('#say-voice').onchange = e => { store.set('say.voice', e.target.value); $('#say-hint').textContent = SAY_HINT[e.target.value]; };
$('#say-form').onsubmit = e => {
  e.preventDefault();
  const text = $('#say-text').value.trim(); if (!text) return;
  act(e.submitter, async () => {
    const r = await api('/api/say', { text, voice: $('#say-voice').value });
    $('#say-text').select();
    toast('🗣 ' + text + ' · ' + (SAY_METHODS[r.method] || r.method || ''));
  });
};
$('#snd-replace-file').onchange = async e => {
  const f = e.target.files[0], name = sndTarget;
  e.target.value = '';
  if (!f || !name) return;
  toast('Перекодирую и заменяю ' + name + '…');
  const btn = $(`#sounds [data-snd-replace="${CSS.escape(name)}"]`);
  await act(btn, async () => setSndCustom(await api('/api/sounds/replace', f,
    { raw: true, headers: { 'Content-Type': 'application/octet-stream', 'X-Sound': encodeURIComponent(name) } })), 'Звук заменён: ' + name);
};
async function loadSounds() {
  sounds = await api('/api/sounds');
  sndCustom = new Set((await api('/api/sounds/custom').catch(() => ({}))).custom || []);
  renderFolders(); renderSounds();
}
$('#snd-filter').oninput = renderSounds;
$('#stop-sound').onclick = e => act(e.currentTarget, () => api('/api/stop', {}), 'Остановлено');

async function uploadFile(f) {
  if (!f) return;
  toast('Загрузка ' + f.name + '…');
  await act(null, () => api('/api/upload', f, {
    raw: true, headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(f.name) },
  }), '▶ ' + f.name);
}
const drop = $('#drop');
drop.onclick = () => $('#file').click();
$('#file').onchange = e => { uploadFile(e.target.files[0]); e.target.value = ''; };
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); uploadFile(e.dataTransfer.files[0]); };

// ---------- дисплей часов: модель и отрисовка ----------
// Кадр — 36 яркостей в порядке сервера: 4 позиции × сегменты A–H, затем DH, DL, DM, DEGREE.
const SEGS = 'ABCDEFGH';
const X_DEG = 32, X_DH = 33, X_DL = 34, X_DM = 35, NLED = 36;
const SEGMENTS = {
  '0': 'ABCDEF', '1': 'BC', '2': 'ABDEG', '3': 'ABCDG', '4': 'BCFG', '5': 'ACDFG', '6': 'ACDEFG', '7': 'ABC', '8': 'ABCDEFG', '9': 'ABCDFG',
  A: 'ABCEFG', B: 'CDEFG', C: 'ADEF', D: 'BCDEG', E: 'ADEFG', F: 'AEFG', G: 'ACDEF', H: 'BCEFG', I: 'EF', J: 'BCDE',
  L: 'DEF', N: 'CEG', O: 'CDEG', P: 'ABEFG', R: 'EG', S: 'ACDFG', T: 'DEFG', U: 'BCDEF', Y: 'BCDFG', '-': 'G', '_': 'D', ' ': '', '°': 'ABFG',
};
const blankFrame = () => new Array(NLED).fill(0);

function textToFrame(text, bright = 255, extras = {}) {
  const v = blankFrame();
  let pos = -1;
  for (const ch of String(text).toUpperCase()) {
    if (ch === ':') { v[X_DH] = v[X_DL] = bright; continue; }
    if (ch === '.') { if (pos >= 0) v[pos * 8 + 7] = bright; continue; }
    if (++pos > 3) break;
    for (const s of SEGMENTS[ch] ?? '') v[pos * 8 + SEGS.indexOf(s)] = bright;
  }
  if (extras.dh) v[X_DH] = bright;
  if (extras.dl) v[X_DL] = bright;
  if (extras.dm) v[X_DM] = bright;
  if (extras.degree) v[X_DEG] = bright;
  return v;
}

// Геометрия: цифра 56×96, толщина сегмента 10; между 2-й и 3-й цифрой — двоеточие и средняя точка.
const DIGIT_X = [0, 70, 172, 242];
function segShapes(x) {
  const t = 10, w = 56, h = 96, m = h / 2;
  return [
    ['A', `M${x + 6} 0h${w - 12}l-6 ${t}h${-(w - 24)}z`],
    ['B', `M${x + w} 6v${m - 9}l-${t} -5v${-(m - 21)}z`],
    ['C', `M${x + w} ${m + 3}v${m - 9}l-${t} -6v${-(m - 21)}z`],
    ['D', `M${x + 6} ${h}h${w - 12}l-6 -${t}h${-(w - 24)}z`],
    ['E', `M${x} ${m + 3}v${m - 9}l${t} -6v${-(m - 21)}z`],
    ['F', `M${x} 6v${m - 9}l${t} -5v${-(m - 21)}z`],
    ['G', `M${x + 4} ${m}l6 -${t / 2}h${w - 20}l6 ${t / 2}l-6 ${t / 2}h${-(w - 20)}z`],
  ];
}

// =========================================================================
// Station Max 25x16 LED Dot Matrix Display Renderer
// =========================================================================
const MAX_FONT_4X7 = {"0": ["0110", "1001", "1001", "1001", "1001", "1001", "0110"], "1": ["0010", "0110", "0010", "0010", "0010", "0010", "0111"], "2": ["0110", "1001", "0001", "0010", "0100", "1000", "1111"], "3": ["1110", "0001", "0001", "0110", "0001", "0001", "1110"], "4": ["1001", "1001", "1001", "1111", "0001", "0001", "0001"], "5": ["1111", "1000", "1000", "1110", "0001", "0001", "1110"], "6": ["0110", "1000", "1000", "1110", "1001", "1001", "0110"], "7": ["1111", "0001", "0010", "0010", "0100", "0100", "0100"], "8": ["0110", "1001", "1001", "0110", "1001", "1001", "0110"], "9": ["0110", "1001", "1001", "0111", "0001", "0001", "0110"], " ": ["00", "00", "00", "00", "00", "00", "00"], ":": ["0", "0", "1", "0", "1", "0", "0"], ".": ["0", "0", "0", "0", "0", "0", "1"], ",": ["0", "0", "0", "0", "0", "1", "1"], "-": ["000", "000", "000", "111", "000", "000", "000"], "+": ["000", "010", "010", "111", "010", "010", "000"], "*": ["0000", "1001", "0110", "1111", "0110", "1001", "0000"], "°": ["110", "110", "000", "000", "000", "000", "000"], "/": ["0001", "0001", "0010", "0100", "0100", "1000", "1000"], "\\": ["1000", "1000", "0100", "0010", "0010", "0001", "0001"], "!": ["1", "1", "1", "1", "0", "0", "1"], "?": ["0110", "1001", "0001", "0010", "0010", "0000", "0010"], "=": ["000", "000", "111", "000", "111", "000", "000"], "_": ["0000", "0000", "0000", "0000", "0000", "0000", "1111"], "%": ["1001", "1001", "0010", "0100", "1001", "1001", "0000"], "(": ["010", "100", "100", "100", "100", "100", "010"], ")": ["010", "001", "001", "001", "001", "001", "010"], "A": ["0110", "1001", "1001", "1111", "1001", "1001", "1001"], "B": ["1110", "1001", "1001", "1110", "1001", "1001", "1110"], "C": ["0111", "1000", "1000", "1000", "1000", "1000", "0111"], "D": ["1110", "1001", "1001", "1001", "1001", "1001", "1110"], "E": ["1111", "1000", "1000", "1110", "1000", "1000", "1111"], "F": ["1111", "1000", "1000", "1110", "1000", "1000", "1000"], "G": ["0111", "1000", "1000", "1011", "1001", "1001", "0111"], "H": ["1001", "1001", "1001", "1111", "1001", "1001", "1001"], "I": ["111", "010", "010", "010", "010", "010", "111"], "J": ["0001", "0001", "0001", "0001", "1001", "1001", "0110"], "K": ["1001", "1010", "1100", "1100", "1010", "1001", "1001"], "L": ["1000", "1000", "1000", "1000", "1000", "1000", "1111"], "M": ["10001", "11011", "10101", "10001", "10001", "10001", "10001"], "N": ["1001", "1101", "1101", "1011", "1011", "1001", "1001"], "O": ["0110", "1001", "1001", "1001", "1001", "1001", "0110"], "P": ["1110", "1001", "1001", "1110", "1000", "1000", "1000"], "Q": ["0110", "1001", "1001", "1001", "1011", "1001", "0111"], "R": ["1110", "1001", "1001", "1110", "1010", "1001", "1001"], "S": ["0111", "1000", "1000", "0110", "0001", "0001", "1110"], "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"], "U": ["1001", "1001", "1001", "1001", "1001", "1001", "0110"], "V": ["1001", "1001", "1001", "1001", "1001", "0110", "0100"], "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"], "X": ["1001", "1001", "0110", "0110", "0110", "1001", "1001"], "Y": ["1001", "1001", "1001", "0110", "0010", "0010", "0010"], "Z": ["1111", "0001", "0010", "0100", "0100", "1000", "1111"], "А": ["0110", "1001", "1001", "1111", "1001", "1001", "1001"], "Б": ["1111", "1000", "1000", "1110", "1001", "1001", "1110"], "В": ["1110", "1001", "1001", "1110", "1001", "1001", "1110"], "Г": ["1111", "1000", "1000", "1000", "1000", "1000", "1000"], "Д": ["01100", "01100", "01100", "01100", "01100", "11111", "10001"], "Е": ["1111", "1000", "1000", "1110", "1000", "1000", "1111"], "Ё": ["0101", "0000", "1111", "1000", "1110", "1000", "1111"], "Ж": ["10101", "10101", "10101", "01110", "10101", "10101", "10101"], "З": ["1110", "0001", "0001", "0110", "0001", "0001", "1110"], "И": ["1001", "1001", "1011", "1101", "1101", "1001", "1001"], "Й": ["0110", "0000", "1001", "1011", "1101", "1001", "1001"], "К": ["1001", "1010", "1100", "1100", "1010", "1001", "1001"], "Л": ["0111", "1001", "1001", "1001", "1001", "1001", "1001"], "М": ["10001", "11011", "10101", "10001", "10001", "10001", "10001"], "Н": ["1001", "1001", "1001", "1111", "1001", "1001", "1001"], "О": ["0110", "1001", "1001", "1001", "1001", "1001", "0110"], "П": ["1111", "1001", "1001", "1001", "1001", "1001", "1001"], "Р": ["1110", "1001", "1001", "1110", "1000", "1000", "1000"], "С": ["0111", "1000", "1000", "1000", "1000", "1000", "0111"], "Т": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"], "У": ["1001", "1001", "1001", "0111", "0001", "0001", "1110"], "Ф": ["0110", "1001", "1111", "1001", "0110", "0010", "0010"], "Х": ["1001", "1001", "0110", "0110", "0110", "1001", "1001"], "Ц": ["10010", "10010", "10010", "10010", "10010", "11111", "00001"], "Ч": ["1001", "1001", "1001", "0111", "0001", "0001", "0001"], "Ш": ["10101", "10101", "10101", "10101", "10101", "10101", "11111"], "Щ": ["101010", "101010", "101010", "101010", "101010", "111111", "000001"], "Ъ": ["1100", "0100", "0100", "0110", "0101", "0101", "0110"], "Ы": ["10001", "10001", "10001", "11101", "10011", "10011", "11101"], "Ь": ["1000", "1000", "1000", "1110", "1001", "1001", "1110"], "Э": ["1110", "0001", "0001", "0111", "0001", "0001", "1110"], "Ю": ["10110", "10101", "10101", "11101", "10101", "10101", "10110"], "Я": ["0111", "1001", "1001", "0111", "0101", "1001", "1001"]};

function renderMatrixText(text, brightness = 180) {
  const cols = 25, rows = 16;
  const grid = new Array(cols * rows).fill(0);
  const glyphs = [];
  let totalW = 0;
  for (const ch of String(text || '')) {
    const up = ch.toUpperCase();
    const g = MAX_FONT_4X7[up] || MAX_FONT_4X7[' '];
    const gw = g[0].length;
    glyphs.push({ g, gw });
    totalW += gw + 1;
  }
  if (totalW > 0) totalW -= 1;
  const startX = Math.max(0, Math.floor((cols - totalW) / 2));
  const startY = Math.max(0, Math.floor((rows - 7) / 2));
  let curX = startX;
  for (const { g, gw } of glyphs) {
    for (let r = 0; r < 7; r++) {
      for (let c = 0; c < gw; c++) {
        if (g[r][c] === '1') {
          const y = startY + r;
          const x = curX + c;
          if (y >= 0 && y < rows && x >= 0 && x < cols) {
            grid[y * cols + x] = Math.max(0, Math.min(255, brightness));
          }
        }
      }
    }
    curX += gw + 1;
  }
  return grid;
}

function v36ToMatrix(v) {
  if (!v || v.length === 400) return v || new Array(400).fill(0);
  const cols = 25, rows = 16;
  const grid = new Array(cols * rows).fill(0);
  const digit_x = [1, 7, 14, 20];
  for (let d = 0; d < 4; d++) {
    const x0 = digit_x[d];
    let b = Math.max(0, Math.min(255, v[d * 8 + 0] || 0));
    if (b > 0) { for (let x = x0 + 1; x < x0 + 4; x++) grid[3 * cols + x] = b; }
    b = Math.max(0, Math.min(255, v[d * 8 + 1] || 0));
    if (b > 0) { for (let y = 4; y < 8; y++) grid[y * cols + x0 + 4] = b; }
    b = Math.max(0, Math.min(255, v[d * 8 + 2] || 0));
    if (b > 0) { for (let y = 9; y < 13; y++) grid[y * cols + x0 + 4] = b; }
    b = Math.max(0, Math.min(255, v[d * 8 + 3] || 0));
    if (b > 0) { for (let x = x0 + 1; x < x0 + 4; x++) grid[13 * cols + x] = b; }
    b = Math.max(0, Math.min(255, v[d * 8 + 4] || 0));
    if (b > 0) { for (let y = 9; y < 13; y++) grid[y * cols + x0] = b; }
    b = Math.max(0, Math.min(255, v[d * 8 + 5] || 0));
    if (b > 0) { for (let y = 4; y < 8; y++) grid[y * cols + x0] = b; }
    b = Math.max(0, Math.min(255, v[d * 8 + 6] || 0));
    if (b > 0) { for (let x = x0 + 1; x < x0 + 4; x++) grid[8 * cols + x] = b; }
    b = Math.max(0, Math.min(255, v[d * 8 + 7] || 0));
    if (b > 0 && x0 + 5 < cols) grid[13 * cols + x0 + 5] = b;
  }
  const b_deg = Math.max(0, Math.min(255, v[32] || 0));
  if (b_deg > 0) {
    grid[3 * cols + 22] = grid[3 * cols + 23] = b_deg;
    grid[4 * cols + 21] = grid[4 * cols + 24] = b_deg;
    grid[5 * cols + 22] = grid[5 * cols + 23] = b_deg;
  }
  const b_dh = Math.max(0, Math.min(255, v[33] || 0));
  if (b_dh > 0) { grid[5 * cols + 12] = grid[6 * cols + 12] = b_dh; }
  const b_dl = Math.max(0, Math.min(255, v[34] || 0));
  if (b_dl > 0) { grid[10 * cols + 12] = grid[11 * cols + 12] = b_dl; }
  const b_dm = Math.max(0, Math.min(255, v[35] || 0));
  if (b_dm > 0) { for (let x = 11; x < 14; x++) grid[8 * cols + x] = b_dm; }
  return grid;
}

function toMatrixFrame(v) {
  if (!v) return new Array(400).fill(0);
  if (v.length === 400) return v;
  if (v.length === 36) return v36ToMatrix(v);
  const res = new Array(400).fill(0);
  for (let i = 0; i < Math.min(400, v.length); i++) res[i] = v[i];
  return res;
}

function matrixSVG(pixels, { thumb = false } = {}) {
  pixels = toMatrixFrame(pixels);
  let s = `<svg viewBox="-4 -4 358 230" class="matrix-display" style="width:100%;${thumb ? '' : 'max-width:380px;'}height:auto;display:block;margin:0 auto;">`;
  s += '<rect x="-4" y="-4" width="358" height="230" rx="12" fill="#0d0e11"/>';
  const cols = 25, rows = 16;
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const i = r * cols + c;
      const v = (pixels && pixels[i] != null) ? pixels[i] : 0;
      const cx = 8 + c * 14.0;
      const cy = 8 + r * 13.8;
      const op = v > 0 ? (0.28 + 0.72 * (v / 255)).toFixed(2) : '0.10';
      const fill = v > 0 ? '#ffffff' : '#23242a';
      const filter = (!thumb && v > 140) ? ' filter="drop-shadow(0 0 2px rgba(255,255,255,0.8))"' : '';
      s += `<circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="4.2" fill="${fill}" fill-opacity="${op}"${filter} data-i="${i}"/>`;
    }
  }
  s += '</svg>';
  return s;
}

function updateMatrixSVG(el, pixels) {
  if (!el || !pixels) return;
  const key = (pixels.length === 400) ? pixels.join(',') : null;
  if (key && el._lastKey === key) return;
  el._lastKey = key;

  let svg = el.querySelector('svg.matrix-display');
  if (!svg || !el._circles || el._circles.length !== 400) {
    el.innerHTML = matrixSVG(pixels);
    svg = el.querySelector('svg.matrix-display');
    el._circles = svg ? svg.querySelectorAll('circle[data-i]') : null;
    return;
  }
  const circles = el._circles;
  for (let i = 0; i < 400; i++) {
    const v = pixels[i] || 0;
    const c = circles[i];
    const op = v > 0 ? (0.28 + 0.72 * (v / 255)).toFixed(2) : '0.10';
    const fill = v > 0 ? '#ffffff' : '#23242a';
    if (c.getAttribute('fill') !== fill) c.setAttribute('fill', fill);
    if (c.getAttribute('fill-opacity') !== op) c.setAttribute('fill-opacity', op);
  }
}

function clockSVG(v, { thumb = false } = {}) {
  const lvl = i => v[i] / 255;
  const cls = i => v[i] > 0 ? 'seg-on' : 'seg-off';
  // горящий сегмент заметно ярче погасшего, а полутона различимы между собой
  const op = i => v[i] > 0 ? ` fill-opacity="${(0.3 + 0.7 * lvl(i)).toFixed(2)}"` : '';
  let s = `<svg viewBox="-4 -4 340 112" role="img" aria-label="Дисплей часов">`;
  DIGIT_X.forEach((x, p) => {
    for (const [seg, d] of segShapes(x)) {
      const i = p * 8 + SEGS.indexOf(seg);
      s += `<path d="${d}" class="${cls(i)}"${op(i)} data-i="${i}"/>`;
    }
    const i = p * 8 + 7;
    s += `<circle cx="${x + 28}" cy="36" r="4.5" class="${cls(i)}"${op(i)} data-i="${i}"/>`;   // точка H — чуть выше центра цифры
  });
  [[X_DH, 30], [X_DL, 66]].forEach(([i, y]) =>
    s += `<circle cx="146" cy="${y}" r="6" class="${cls(i)}"${op(i)} data-i="${i}"/>`);
  s += `<rect x="137" y="45" width="18" height="6" rx="2" class="${cls(X_DM)}"${op(X_DM)} data-i="${X_DM}"/>`;   // дефис
  const di = X_DEG;
  s += `<circle cx="318" cy="10" r="7" stroke-width="4" class="${v[di] > 0 ? 'deg-on' : 'deg-off'}"${v[di] > 0 ? ` stroke-opacity="${Math.max(.18, lvl(di)).toFixed(2)}"` : ''} data-i="${di}"/>`;
  if (!thumb) s += `<circle cx="318" cy="10" r="11" fill="transparent" data-i="${di}"/>`;
  return s + '</svg>';
}

// Геометрия дисплея и вывод на 3D-модель (модуль модели подключается отдельно и задаёт window.modelShow).
window.clockGeom = { segShapes, DIGIT_X, SEGS, X_DH, X_DL, X_DM, X_DEG };
const toModel = v => {
  window.modelShow?.(v);
  if (window.__isMatrix && Array.isArray(v) && v.length === 400) {
    const el = $('#clock-preview');
    if (el && Date.now() >= (el._until || 0)) {
      const isPinned = !!(dispState && dispState.mode);
      const isEditingText = (document.activeElement === $('#clock-text') && $('#clock-text').value);
      const isCustomTab = (dispMode !== 'text');
      if (isPinned || (!isEditingText && !isCustomTab && !$('#clock-text').value)) {
        updateMatrixSVG(el, v);
      }
    }
  } else if (window.__midiLiveDisplay && Array.isArray(v)) {
    $('#clock-preview').innerHTML = clockSVG(v);
  }
};
const toRing = leds => window.modelRing?.(leds);

// Проигрывание кадров в браузере (предпросмотр); флаг 1 у кадра — начало цикла.
function animate(el, frames, { seconds = 10, onFrame } = {}) {
  clearTimeout(el._anim);
  if (!frames.length) return;
  const until = el._until = Date.now() + seconds * 1000;
  const loopAt = frames.findIndex(f => f.flag);
  let i = 0;
  const step = () => {
    if (Date.now() > until) return el._animDone?.();
    if (i >= frames.length) {
      if (loopAt < 0) return el._animDone?.();
      i = loopAt;
    }
    el.innerHTML = (window.__isMatrix ? matrixSVG(frames[i].v) : clockSVG(frames[i].v));
    if (el.id === 'clock-preview') toModel(frames[i].v);
    onFrame?.(i);
    el._anim = setTimeout(step, Math.max(16, frames[i++].ms));
  };
  step();
}

// ---------- анимации ----------
let patterns = {}, patKind = 'ring';
// Человекочитаемые названия анимаций; системное имя остаётся сбоку.
const PAT_NAMES = {
  alice_listening: 'Алиса слушает', alice_speaking: 'Алиса говорит', alice_thinking: 'Алиса думает',
  alice_pro_listening: 'Алиса Про слушает', alice_pro_speaking: 'Алиса Про говорит', alice_pro_thinking: 'Алиса Про думает',
  intonation_spotting: 'Распознала интонацию',
  player_continue: 'Продолжить', player_pause: 'Пауза', player_next_track: 'Следующий трек', player_previous_track: 'Предыдущий трек',
  volume: 'Громкость', bluetooth_on: 'Bluetooth включён', bluetooth_connected: 'Bluetooth подключён', bluetooth_playing: 'Музыка по Bluetooth',
  call_incoming: 'Входящий звонок', call_outgoing: 'Исходящий звонок', call_connecting: 'Соединяю', call_connected: 'Разговор',
  call_connected_nanny: 'Разговор (радионяня)', call_ended: 'Звонок завершён', ringing: 'Звонок',
  timer: 'Таймер', notification_available: 'Есть уведомление', notification_passive: 'Тихое уведомление',
  iot_discovery: 'Поиск устройств умного дома', boot: 'Загрузка', configure: 'Настройка', configuring: 'Настройка',
  connecting_to_network: 'Подключение к сети', clock_sync_1: 'Синхронизация часов 1', clock_sync_2: 'Синхронизация часов 2',
  update_downloading: 'Скачивание обновления', update_applying: 'Установка обновления', confirm_data_reset: 'Подтвердите сброс',
  error: 'Ошибка', progress: 'Прогресс', soundinit: 'Инициализация звука', qc: 'Заводской тест',
  battery_cold: 'Батарея замёрзла', battery_hot: 'Батарея перегрелась', low_battery: 'Низкий заряд', blink: 'Моргание',
};
const PAT_GROUPS = [
  ['Свои', /^my\//],
  ['Алиса', /^(alice_|intonation)/], ['Плеер и звук', /^(player_|volume|bluetooth_)/], ['Звонки', /^(call_|ringing)/],
  ['Уведомления и таймер', /^(timer|notification_|iot_)/], ['Система', /^(boot|configur|connecting|clock_sync|update_|confirm_|error|progress|soundinit|qc|battery_|low_battery)/],
];
const patBase = n => n.replace(/\.(bin)?s?cled(\.zst)?$|\.led$/, '');
function renderPatterns() {
  const list = patterns[patKind] || [];
  const groups = PAT_GROUPS.map(([title, re]) => [title, list.filter(n => re.test(patBase(n)))]);
  const rest = list.filter(n => !PAT_GROUPS.some(([, re]) => re.test(patBase(n))));
  if (rest.length) groups.push(['Прочее', rest]);
  $('#patterns').innerHTML = groups.filter(([, items]) => items.length).map(([title, items]) =>
    `<div class="pat-group">${esc(title)}</div>` + items.map(n => {
      const base = patBase(n);
      return `<button class="pat" data-p="${esc(n)}"><span class="pat-play">${icon('i-play', 11)}</span>
        <span class="pat-name">${esc(animTitle(n))}</span><span class="pat-sys">${esc(base)}</span></button>`;
    }).join('')).join('');
  $$('#patterns .pat').forEach(b => b.onclick = () => act(b, async () => {
    const seconds = +$('#pat-sec').value || 5;
    await api('/api/pattern', { kind: patKind, name: b.dataset.p, seconds });
    if (patKind === 'ring') animateRing(await api('/api/ring/frames?name=' + encodeURIComponent(b.dataset.p)), seconds);
    if (patKind === 'clock') {
      const frames = await api('/api/clock/frames?name=' + encodeURIComponent(b.dataset.p));
      const el = $('#clock-preview');
      el._animDone = clockPreview;
      animate(el, frames, { seconds });
    }
  }, '✦ ' + b.querySelector('.pat-name').textContent));
}
$$('#pat-kind button').forEach(b => b.onclick = () => {
  $$('#pat-kind button').forEach(x => x.classList.toggle('active', x === b));
  patKind = b.dataset.k; renderPatterns();
});
$('#pat-stop').onclick = e => act(e.currentTarget, async () => {
  await api('/api/pattern/stop', {});
  clearTimeout($('#clock-preview')._anim); clockPreview();
  clearTimeout(ringTimer); ringAnimUntil = 0;
}, 'Анимации остановлены');

// ---------- кольцо ----------
const hex2rgb = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
let ringHex = '#FF5A00';
function pickColor(hex) {
  ringHex = hex.toUpperCase();
  $('#ring-preview').style.setProperty('--ring', ringHex);
  $$('#swatches .sw').forEach(s => s.classList.toggle('sel', s.dataset.hex === ringHex));
}
function syncRingPreview(leds) {
  if (!Array.isArray(leds) || !leds.length) return;
  const isLit = leds.some(c => Array.isArray(c) ? c.some(v => v > 10) : false);
  if (isLit) {
    const firstLit = leds.find(c => Array.isArray(c) && c.some(v => v > 10)) || leds[0];
    const hex = '#' + firstLit.map(x => Math.min(255, Math.max(0, x)).toString(16).padStart(2, '0')).join('');
    $('#ring-preview').style.setProperty('--ring', hex);
    $('#ring-preview').classList.remove('off');
  } else if (!window.__ringUserActive) {
    $('#ring-preview').classList.add('off');
  }
}
const setRing = (btn, rgb, on) => act(btn, async () => {
  window.__ringUserActive = on;
  await api('/api/ring', { red: rgb[0], green: rgb[1], blue: rgb[2] });
  $('#ring-preview').classList.toggle('off', !on);
  toRing([rgb, rgb, rgb]);
});
$$('#swatches .sw').forEach(b => b.onclick = () => { pickColor(b.dataset.hex); setRing(b, hex2rgb(ringHex), true); });
$('#ring-color').oninput = e => pickColor(e.target.value);
$('#ring-color').onchange = e => setRing(null, hex2rgb(e.target.value), true);
$('#ring-set').onclick = e => setRing(e.currentTarget, hex2rgb(ringHex), true);
$('#ring-off').onclick = e => { window.__ringUserActive = false; setRing(e.currentTarget, [0, 0, 0], false); };
$('#ring-reset').onclick = e => act(e.currentTarget, async () => {
  window.__ringUserActive = false;
  const r = await api('/api/ring/reset', {});
  toRing(r.leds);
  $('#ring-preview').classList.toggle('off', r.leds.every(c => c.every(x => !x)));
}, 'Кольцо снова у Алисы');
pickColor(ringHex);

// ---------- часы: режимы и закрепление ----------
// «Закрепить» запускает на колонке скрипт, который сам перерисовывает дисплей раз в полсекунды
// (Quasar обновляет часы раз в минуту и иначе затирает наш вывод). «Один раз» — просто запись.
const pad2 = n => String(n).padStart(2, '0');
const pcTime = () => { const d = new Date(); return pad2(d.getHours()) + ':' + pad2(d.getMinutes()); };
const clockExtras = () => ({ dh: $('#x-dh').checked, dl: $('#x-dl').checked, dm: $('#x-dm').checked, degree: $('#x-degree').checked });
try { window.__isMatrix = (localStorage.getItem('is_matrix') === '1'); } catch {}
let dispMode = 'text', dispState = { mode: null }, dispStateAt = 0;
const DISP_NAMES = { frame: 'текст', date: 'дата', temp: 'температура', timer: 'таймер' };
function timerSeconds() {
  const m = ($('#timer-val').value || '').match(/^(\d{1,2})(?::(\d{1,2}))?$/);
  return m ? (+m[1]) * 60 + (+(m[2] || 0)) : 0;
}
function clockFrame() {
  const br = +$('#clock-br').value;
  if (dispMode === 'date') {   // 24.10 — разделитель как у колонки: нижняя точка двоеточия
    const d = new Date(), v = textToFrame(`${pad2(d.getDate())}${pad2(d.getMonth() + 1)}`, br);
    v[X_DL] = br;
    return v;
  }
  if (dispMode === 'temp') return textToFrame(String(Math.round((status?.temp || 0) / 1000)).padStart(4, ' '), br, { degree: true });
  if (dispMode === 'timer') { const s = timerSeconds(); return textToFrame(`${pad2(Math.min(99, Math.floor(s / 60)))}:${pad2(s % 60)}`, br); }
  return textToFrame($('#clock-text').value || pcTime(), br, clockExtras());
}
let __lastMatrixPixels = null;
function clockPreview() {
  if (window.__midiLiveDisplay) return;
  const el = $('#clock-preview');
  clearTimeout(el._anim);
  el._until = 0;
  if (window.__isMatrix) {
    let px = null;
    const txt = $('#clock-text').value;
    const br = +$('#clock-br').value;
    if (dispMode === 'text' && txt) px = renderMatrixText(txt, br);
    else if (dispMode === 'timer') { const s = timerSeconds(); px = renderMatrixText(`${pad2(Math.floor(s/60))}:${pad2(s%60)}`, br); }
    else if (dispMode === 'date') { const d = new Date(); px = renderMatrixText(`${pad2(d.getDate())}.${pad2(d.getMonth()+1)}`, br); }
    else if (dispMode === 'temp') {
      const rawT = status?.temp != null ? Number(status.temp) : 48000;
      const deg = Math.round(rawT > 200 ? rawT / 1000 : rawT);
      px = renderMatrixText(`${deg}°`, br);
    }
    else if (__lastMatrixPixels) px = __lastMatrixPixels;
    else px = renderMatrixText(pcTime(), br);
    updateMatrixSVG(el, px);
    window.modelShow?.(px);
    return;
  }
  el.innerHTML = clockSVG(clockFrame());
}
['#clock-text', '#x-dh', '#x-dl', '#x-dm', '#x-degree', '#timer-val'].forEach(s => $(s).addEventListener('input', clockPreview));
$('#clock-br').oninput = e => { $('#clock-br-v').textContent = e.target.value; clockPreview(); };
$$('#disp-mode button').forEach(b => b.onclick = () => {
  dispMode = b.dataset.m;
  $$('#disp-mode button').forEach(x => x.classList.toggle('active', x === b));
  $('#dm-text').hidden = dispMode !== 'text';
  $('#dm-timer').hidden = dispMode !== 'timer';
  $('#clock-set').hidden = dispMode !== 'text';
  $('#disp-pin').textContent = dispMode === 'timer' ? 'Запустить таймер' : 'Закрепить';
  clockPreview();
});
$$('#dm-timer [data-t]').forEach(b => b.onclick = () => { const s = +b.dataset.t; $('#timer-val').value = `${pad2(s / 60 | 0)}:${pad2(s % 60)}`; clockPreview(); });

function renderDispState() {
  const st = dispState, on = !!st.mode;
  $('#disp-badge').hidden = !on;
  if (!on) return;
  let text = 'закреплено: ' + (DISP_NAMES[st.mode] || st.mode);
  if (st.mode === 'timer' && st.left != null) {
    const left = Math.max(0, st.left - Math.round((Date.now() - dispStateAt) / 1000));
    text = `таймер ${pad2(left / 60 | 0)}:${pad2(left % 60)}`;
  }
  $('#disp-badge').textContent = text;
  $('#disp-badge').className = 'badge ok';
}
async function loadDispState() {
  if (document.hidden || $('#tab-overview').hidden) return;
  try { dispState = await api('/api/display'); dispStateAt = Date.now(); renderDispState(); } catch {}
}
every(5000, loadDispState);
every(1000, () => dispState.mode === 'timer' && renderDispState());

$('#disp-pin').onclick = e => act(e.currentTarget, async () => {
  let body = { mode: dispMode, brightness: +$('#clock-br').value };
  if (window.__isMatrix) {
    if (dispMode === 'text') {
      const txt = $('#clock-text').value || pcTime();
      body = { mode: 'text', text: txt, brightness: +$('#clock-br').value };
    }
  } else {
    if (dispMode === 'text') { body.mode = 'frame'; body.v = clockFrame(); }
  }
  if (dispMode === 'timer') {
    body.seconds = timerSeconds();
    if (!body.seconds) throw new Error('Время таймера — в виде мм:сс, например 05:00');
  }
  $('#clock-text').blur();
  $('#timer-val').blur();
  dispState = await api('/api/display', body);
  dispStateAt = Date.now();
  renderDispState();
  if (window.__isMatrix) {
    clockPreview();
  }
}, dispMode === 'timer' ? 'Таймер запущен' : 'Закреплено');
$('#disp-stop').onclick = e => act(e.currentTarget, async () => {
  dispState = await api('/api/display/stop', {});
  await api('/api/pattern/stop', {}).catch(() => {});
  dispMode = 'text';
  $$('#disp-mode button').forEach(x => x.classList.toggle('active', x.dataset.m === 'text'));
  $('#dm-text').hidden = false;
  $('#dm-timer').hidden = true;
  $('#clock-set').hidden = false;
  $('#disp-pin').textContent = 'Закрепить';
  __lastMatrixPixels = null;
  const el = $('#clock-preview');
  if (el) { el._lastKey = null; el._circles = null; }
  renderDispState();
  $('#clock-text').value = '';
  clockPreview();
}, 'Дисплей снова показывает часы');
function sendClock(btn) {
  const text = $('#clock-text').value || pcTime();
  clockPreview();
  return act(btn, async () => {
    if (window.__isMatrix) {
      await api('/api/clock', { text, brightness: +$('#clock-br').value });
      $('#clock-preview')._until = Date.now() + 5000;
      const px = renderMatrixText(text, +$('#clock-br').value);
      toModel(px);
      $('#clock-preview').innerHTML = matrixSVG(px);
    } else {
      await api('/api/clock', { v: clockFrame(), text });
      toModel(clockFrame());
    }
    dispState = { mode: null };
    renderDispState();
  },
             'На дисплее: ' + text);
}
$('#clock-set').onclick = e => sendClock(e.currentTarget);
$('#clock-text').onkeydown = e => { if (e.key === 'Enter') sendClock(null); };
clockPreview();
every(15000, () => (dispMode !== 'text' || !$('#clock-text').value) && !(dispState && dispState.mode) && Date.now() > ($('#clock-preview')._until || 0) && clockPreview());

// Живое состояние дисплея и кольца для модели, пока «Обзор» на экране. Локальные анимации
// (запущенные из панели) рисуются по кадрам сами, опрос их не перебивает.
let ringAnimUntil = 0, ringLit = false, ringTimer;
let ledStreamSource = null, ledTask = 0, liveTask = 0;
function initRealtimeLedStream() {
  if (window.EventSource) {
    try { if (ledStreamSource) ledStreamSource.close(); } catch {}
    ledStreamSource = new EventSource('/api/leds/stream');
    ledStreamSource.onmessage = e => {
      if (document.hidden || $('#tab-overview').hidden) return;
      try {
        const s = JSON.parse(e.data);
        if (s.pixels) {
          __lastMatrixPixels = s.pixels;
          if (Date.now() >= ($('#clock-preview')._until || 0)) {
            toModel(s.pixels);
          }
        }
        else if (s.v && Date.now() >= ($('#clock-preview')._until || 0)) toModel(s.v);
        if (Array.isArray(s.ring) && s.ring.length && Date.now() >= ringAnimUntil) {
          toRing(s.ring_all || s.ring);
          syncRingPreview(s.ring_all || s.ring);
        }
      } catch {}
    };
    ledStreamSource.onerror = () => {
      try { ledStreamSource.close(); } catch {}
      ledStreamSource = null;
      setTimeout(initRealtimeLedStream, 3000);
    };
  }
}
initRealtimeLedStream();

async function pollLeds() {
  // 20 FPS нужны только пока «Обзор» на экране; на других вкладках — редкая проверка.
  if (!online || document.hidden || $('#tab-overview').hidden) { Scheduler.setInterval(ledTask, 2000); return; }
  if (!ledStreamSource) {
    try {
      const s = await api('/api/clock/state');
      if (Array.isArray(s.v) && Date.now() >= ($('#clock-preview')._until || 0)) toModel(s.pixels || s.v);
      if (Array.isArray(s.ring) && s.ring.length && Date.now() >= ringAnimUntil) {
        toRing(s.ring_all || s.ring);
        syncRingPreview(s.ring_all || s.ring);
      }
    } catch {}
  }
  Scheduler.setInterval(ledTask, ledStreamSource ? 2000 : 50); // 20 FPS fallback
}
ledTask = every(50, pollLeds);
// Быстрый поток — только пока «Обзор» на экране, визуализатор включён и музыка играет.
// Пока играет музыка, Quasar рисует на часах эквалайзер: 4 цифры — 4 полосы, в каждой столбик из D, G, A.
const isSpectrum = v => [0, 1, 2, 3].every(p => !v[p * 8 + 1] && !v[p * 8 + 2] && !v[p * 8 + 4] && !v[p * 8 + 5] && !v[p * 8 + 7])
  && !v[X_DEG] && !v[X_DH] && !v[X_DL] && !v[X_DM];
async function liveLeds() {
  const want = online && !document.hidden && (!$('#tab-overview').hidden || (!$('#tab-player').hidden && plMobile()))
    && !window.__vizOff && !!muState()?.playing;
  const sseActive = ledStreamSource && ledStreamSource.readyState === 1;
  if (want && !sseActive) {
    try {
      const r = await api('/api/leds/live');
      const liveBands = r.spectrum?.bands?.length === 4;
      if (liveBands) window.__spectrum = { bands: r.spectrum.bands, t: performance.now() };
      if (r.v) {
        const v = r.v;
        if (!liveBands && isSpectrum(v)) window.__spectrum = { bands: [0, 1, 2, 3].map(p => (v[p * 8] + v[p * 8 + 3] + v[p * 8 + 6]) / 756), t: performance.now() };
        if (Date.now() >= ($('#clock-preview')._until || 0)) toModel(r.pixels || v);
      }
      if (Array.isArray(r.ring) && r.ring.length && Date.now() >= ringAnimUntil) toRing(r.ring_all || r.ring);
    } catch {}
  }
  const spectrumNow = window.__spectrum && performance.now() - window.__spectrum.t < 2500;
  const delay = want ? (sseActive ? 1000 : (spectrumNow ? 50 : 700)) : 500;
  Scheduler.setInterval(liveTask, delay);
}
liveTask = every(1000, liveLeds);   // первый запуск — когда весь скрипт уже загружен
function animateRing({ frames, loop }, seconds) {
  clearTimeout(ringTimer);
  if (!frames.length) return;
  const until = ringAnimUntil = Date.now() + seconds * 1000;
  let i = 0;
  const step = () => {
    if (Date.now() > until) { ringAnimUntil = 0; return; }
    if (i >= frames.length) {
      if (!loop) { ringAnimUntil = 0; return; }
      i = 0;
    }
    toRing(frames[i].c);
    ringTimer = setTimeout(step, Math.max(16, frames[i++].ms));
  };
  step();
}

// ---------- заставка при загрузке ----------
// Редактор часов правит любую анимацию из ledpatterns/clock; у заставки (boot) — своя пауза и своё хранение.
const BOOT_ANIM = 'boot.binscled.zst';
const boot = { frames: [], sel: 0, def: [], status: null, name: BOOT_ANIM };
let animCustom = { clock: [], ring: [] };
const isBootAnim = () => boot.name === BOOT_ANIM;
const bootCur = () => boot.frames[boot.sel];
// Пустой первый кадр заставки — это пауза перед ней: показываем её отдельным полем, а не кадром.
function setBootFrames(frames) {
  frames = structuredClone(frames || []);
  const lead = isBootAnim() && frames[0] && frames[0].v.every(x => x === 0) ? frames.shift().ms : 0;
  $('#boot-pause').value = lead;
  $$('[data-boot-only]').forEach(e => e.hidden = !isBootAnim());
  boot.frames = frames; boot.sel = 0;
  checkPause();
}
function bootFull() {
  const isM = window.__isMatrix;
  const blank = isM ? () => new Array(400).fill(0) : blankFrame;
  const pause = isBootAnim() ? clamp(+$('#boot-pause').value || 0, 0, 65535) : 0;
  return pause ? [{ flag: 0, v: blank(), ms: pause }, ...boot.frames] : boot.frames;
}
// Свои анимации — «my/имя»: отдельные файлы, встроенные не трогают. Встроенная, заменённая своей версией, — «изменена».
const isMy = n => String(n || '').startsWith('my/');
const myBase = n => patBase(String(n).slice(3));
const animTitle = n => isMy(n) ? myBase(n).replace(/_/g, ' ') : PAT_NAMES[patBase(n)] || patBase(n).replace(/_/g, ' ');
const animLabel = (kind, n) => animTitle(n) + (animCustom[kind].includes(n) ? ' · изменена' : '');
function fillAnimSelect(kind) {
  const sel = $('#anim-' + kind), cur = sel.value || (kind === 'clock' ? boot.name : ra.name);
  const sort = l => l.sort((a, b) => animLabel(kind, a).localeCompare(animLabel(kind, b), 'ru'));
  const all = patterns[kind] || [], mine = sort(all.filter(isMy)), std = sort(all.filter(n => !isMy(n)));
  const opts = l => l.map(n => `<option value="${esc(n)}">${esc(animLabel(kind, n))}</option>`).join('');
  sel.innerHTML = (mine.length ? `<optgroup label="Свои">${opts(mine)}</optgroup>` : '') + `<optgroup label="Встроенные">${opts(std)}</optgroup>`
    + '<option value="__new">＋ Добавить новую…</option>';
  if (all.includes(cur)) sel.value = cur;
  sel.dataset.cur = sel.value;
}
// «＋ Добавить новую…» в списке: своя анимация — копия открытой сейчас, её и правим; встроенная не меняется
async function myNewFromSelect(kind, payload, reload) {
  const sel = $('#anim-' + kind), from = sel.dataset.cur;
  const n = prompt(`Название новой анимации (буквы, цифры, «_» и «-»).\nОна начнётся с копии открытой сейчас «${animTitle(from)}» — дальше правьте как хотите, встроенная не изменится.`);
  if (!n || !n.trim()) { sel.value = from; return; }
  await act(null, async () => { await mySave(kind, n.trim().replace(/\s+/g, '_'), payload()); await reload(); }, 'Своя анимация добавлена');
  if (sel.value === '__new') sel.value = from;
  sel.dataset.cur = sel.value;
}
// Сохранить как свою: имя спрашиваем, файл кладём в webpanel/anims; выбираем её в списке
async function mySave(kind, name, payload) {
  const r = await api('/api/anim/my', { kind, name, ...payload });
  patterns = r.patterns;
  fillAnimSelect(kind); renderPatterns?.();
  $('#anim-' + kind).value = r.name;
  return r.name;
}
function checkPause() {
  const short = (+$('#boot-pause').value || 0) < 3000;
  $('#boot-pause-hint').style.color = short ? 'var(--danger)' : '';
}
$('#boot-pause').oninput = checkPause;
$('#boot-always').onchange = e => act(e.target, async () => {
  try { boot.status = await api('/api/clock/boot/always', { enabled: e.target.checked }); }
  finally { renderBootStatus(); }
}, e.target.checked ? 'Заставка и звук будут при каждой загрузке' : 'Заставка и звук — только когда Quasar сам решит');
function renderBoot() {
  const isM = window.__isMatrix;
  const blank = isM ? () => new Array(400).fill(0) : blankFrame;
  const drawSVG = (v, opts) => isM ? matrixSVG(v, opts) : clockSVG(v, opts);

  if (!boot.frames.length) boot.frames = [{ flag: 0, v: blank(), ms: 1000 }];
  boot.sel = clamp(boot.sel, 0, boot.frames.length - 1);
  if (isM) bootCur().v = toMatrixFrame(bootCur().v);

  $('#boot-frames').innerHTML = boot.frames.map((f, i) =>
    `<button class="frame-thumb ${i === boot.sel ? 'sel' : ''}" data-k="${i}" aria-label="Кадр ${i + 1}">${drawSVG(f.v, { thumb: true })}<span>${i + 1} · ${f.ms} мс</span></button>`).join('')
    + `<button class="frame-add" id="boot-add" aria-label="Добавить кадр">${icon('i-plus', 18)}</button>`;
  $$('#boot-frames .frame-thumb').forEach(b => b.onclick = () => { boot.sel = +b.dataset.k; renderBoot(); });
  $('#boot-add').onclick = () => { boot.frames.splice(boot.sel + 1, 0, { flag: 0, v: blank(), ms: 500 }); boot.sel++; renderBoot(); };
  $('#boot-editor').innerHTML = drawSVG(bootCur().v);
  $('#boot-ms').value = bootCur().ms;
}
function renderBootStatus() {
  const s = boot.status;
  if (!s) return;
  $('#boot-always').checked = !!s.always;
  // своё сохранено, а строки автозапуска нет — после перезагрузки заставка и звук не подменятся
  const needHook = s.custom || s.sound_custom || s.always;
  const needHookAll = needHook || animCustom.clock.length || animCustom.ring.length;
  if (window.__isMatrix) {
    const cardHead = $('#boot-card .card-head h2');
    if (cardHead) cardHead.textContent = 'Анимации LED-экрана 25×16';
    const sub = $('#tab-boot .page-head .sub');
    if (sub) sub.textContent = 'Анимации кольца и LED-экрана 25×16, заставка при включении';
    const hint = $('#boot-card .hint');
    if (hint) hint.textContent = 'Нажимайте на точки матрицы 25×16, чтобы зажечь их выбранной яркостью или погасить. Правая кнопка мыши (на телефоне — долгое нажатие) берёт яркость точки в ползунок.';
    const brLabel = $('label[for="boot-br"]');
    if (brLabel) brLabel.textContent = 'Яркость точки';
    $('#boot-text').placeholder = 'HI : YA';
    $('#boot-text').maxLength = 20;
  }
  $$('[data-hook-badge]').forEach(b => {
    b.textContent = s.hook ? 'хук подключён' : needHookAll ? 'хук не подключён' : 'хук не нужен';
    b.className = 'badge' + (s.hook ? ' ok' : needHookAll ? ' bad' : '');
    b.title = s.hook ? 'Хук автозапуска подключает свои анимации и звуки при каждой загрузке колонки.'
      : needHookAll ? 'Свои анимации или звуки сохранены, но хук автозапуска не подключён — после перезагрузки вернутся стандартные. Сохраните их ещё раз — панель подключит хук.'
      : 'Хук понадобится, когда появится что-то своё: без него после перезагрузки вернутся стандартные.';
  });
  renderClockStatus();
}
function renderClockStatus() {
  const s = boot.status;
  if (isBootAnim()) {
    if (!s) return;
    if (!s.supported) { $('#boot-status').innerHTML = '<span class="badge bad">прошивка не поддерживается</span>'; return; }
    $('#boot-status').innerHTML = s.custom
      ? `<span class="badge ok">изменена</span>${s.mounted ? '' : '<span class="badge bad">не подменена</span>'}`
      : '<span class="badge">стандартная</span>';
  } else {
    $('#boot-status').innerHTML = isMy(boot.name) ? '<span class="badge ok">своя анимация</span>'
      : animCustom.clock.includes(boot.name) ? '<span class="badge ok">изменена</span>' : '<span class="badge">стандартная</span>';
  }
  const my = isMy(boot.name);
  $('#boot-install').textContent = my ? 'Сохранить' : isBootAnim() ? 'Установить на загрузку' : 'Установить на колонку';
  $('#boot-reset').hidden = $('#boot-load-default').hidden = my;
  $('#boot-del-row').hidden = !my;
}
async function loadBoot() {
  try {
    if (!patterns.clock) patterns = await api('/api/patterns');
    [boot.status, animCustom] = await Promise.all([api('/api/clock/boot'), api('/api/anim/custom')]);
    fillAnimSelect('clock'); fillAnimSelect('ring');
    renderBootStatus();
    await loadClockAnim();
    if (!ra.name && $('#anim-ring').value) loadRingAnim();
  } catch (e) { if (!e.offline) $('#boot-status').innerHTML = `<span class="badge bad">${esc(e.message)}</span>`; }
}
async function loadClockAnim() {
  boot.name = $('#anim-clock').value || BOOT_ANIM;
  if (isBootAnim()) {
    boot.def = boot.status?.default || [];
    setBootFrames(boot.status?.frames || boot.def);
  } else {
    const name = encodeURIComponent(boot.name);
    const [orig, cur] = await Promise.all([isMy(boot.name) ? { frames: [] } : api(`/api/anim/original?kind=clock&name=${name}`), api('/api/clock/frames?name=' + name)]);
    boot.def = orig.frames || [];
    setBootFrames(cur);
  }
  if (window.__isMatrix) {
    boot.frames.forEach(f => { f.v = toMatrixFrame(f.v); });
    boot.def.forEach(f => { f.v = toMatrixFrame(f.v); });
  }
  renderClockStatus(); renderBoot();
}
$('#anim-clock').onchange = e => {
  if (e.target.value === '__new') return myNewFromSelect('clock', () => ({ frames: bootFull() }), loadClockAnim);
  e.target.dataset.cur = e.target.value;
  loadClockAnim().catch(err => err.offline || toast(err.message, true));
};
// Кисть яркости: щелчок зажигает сегмент её яркостью, повторный по такому же — гасит; правый — пипетка.
const bootBrush = () => clamp(+$('#boot-br').value || 255, 1, +$('#boot-br').max);
const setBrush = x => { $('#boot-br').value = x; $('#boot-br-v').textContent = bootBrush(); };
$('#boot-br').oninput = () => setBrush($('#boot-br').value);
let bootPainting = false;
function handleEditorDot(e, isDrag = false) {
  const el = e.target.closest('[data-i]');
  if (!el) return;
  const i = +el.dataset.i;
  const v = window.__isMatrix ? toMatrixFrame(bootCur().v) : bootCur().v;
  bootCur().v = v;
  const brush = bootBrush();
  if (isDrag) {
    if (v[i] !== brush) {
      v[i] = brush;
      renderBoot();
    }
  } else {
    v[i] = v[i] === brush ? 0 : brush;
    renderBoot();
  }
}
$('#boot-editor').onmousedown = e => {
  if (e.button === 0) {
    bootPainting = true;
    handleEditorDot(e, false);
  }
};
window.addEventListener('mouseup', () => { bootPainting = false; });
$('#boot-editor').onmousemove = e => {
  if (bootPainting && e.buttons === 1 && window.__isMatrix) {
    handleEditorDot(e, true);
  }
};
$('#boot-editor').ontouchmove = e => {
  if (!window.__isMatrix) return;
  const touch = e.touches[0];
  const el = document.elementFromPoint(touch.clientX, touch.clientY);
  const circle = el?.closest?.('[data-i]');
  if (circle) {
    const i = +circle.dataset.i;
    const v = toMatrixFrame(bootCur().v);
    bootCur().v = v;
    const brush = bootBrush();
    if (v[i] !== brush) {
      v[i] = brush;
      renderBoot();
    }
  }
};
$('#boot-editor').onclick = e => {
  if (!window.__isMatrix) handleEditorDot(e, false);
};
$('#boot-editor').oncontextmenu = e => {
  const i = e.target.closest('[data-i]')?.dataset.i;
  if (i === undefined) return;
  e.preventDefault();
  const v = window.__isMatrix ? toMatrixFrame(bootCur().v) : bootCur().v;
  const x = v[i];
  if (x > 0) setBrush(x);
};
$('#boot-br-all').onclick = () => {
  const v = window.__isMatrix ? toMatrixFrame(bootCur().v) : bootCur().v;
  bootCur().v = v.map(x => x > 0 ? bootBrush() : 0);
  renderBoot();
};
$('#boot-ms').onchange = e => { bootCur().ms = clamp(+e.target.value || 100, 10, 65535); renderBoot(); };
const applyBootText = () => {
  if (window.__isMatrix) {
    bootCur().v = renderMatrixText($('#boot-text').value, bootBrush());
  } else {
    bootCur().v = textToFrame($('#boot-text').value).map(x => x > 0 ? bootBrush() : 0);
  }
  renderBoot();
};
$('#boot-text-apply').onclick = applyBootText;
$('#boot-text').onkeydown = e => { if (e.key === 'Enter') applyBootText(); };
$('#boot-dup').onclick = () => { boot.frames.splice(boot.sel + 1, 0, structuredClone(bootCur())); boot.sel++; renderBoot(); };
$('#boot-clear').onclick = () => {
  bootCur().v = window.__isMatrix ? new Array(400).fill(0) : blankFrame();
  renderBoot();
};
$('#boot-del').onclick = () => { boot.frames.splice(boot.sel, 1); renderBoot(); };
const moveFrame = d => {
  const j = boot.sel + d;
  if (j < 0 || j >= boot.frames.length) return;
  [boot.frames[boot.sel], boot.frames[j]] = [boot.frames[j], boot.frames[boot.sel]];
  boot.sel = j; renderBoot();
};
$('#boot-left').onclick = () => moveFrame(-1);
$('#boot-right').onclick = () => moveFrame(1);
const bootDuration = () => Math.ceil(bootFull().reduce((a, f) => a + f.ms, 0) / 1000) + 1;
$('#boot-preview').onclick = () => {
  const el = $('#boot-editor');
  el._animDone = renderBoot;
  const off = bootFull().length - boot.frames.length; // кадр паузы не подсвечиваем
  animate(el, bootFull(), { seconds: bootDuration(), onFrame: i => $$('#boot-frames .frame-thumb').forEach((b, k) => b.classList.toggle('sel', k === i - off)) });
};
$('#boot-device').onclick = e => act(e.currentTarget, () => api('/api/clock/play', { frames: bootFull(), seconds: bootDuration() }), 'Заставка играет на колонке');
$('#boot-load-default').onclick = () => {
  if (!boot.def.length) return toast('Стандартная анимация недоступна', true);
  setBootFrames(boot.def); renderBoot();
};
const clockAnimTitle = () => isBootAnim() ? 'заставку' : `анимацию «${animTitle(boot.name)}»`;
$('#boot-del-my').onclick = e => confirm(`Удалить свою анимацию «${animTitle(boot.name)}»?`) && act(e.currentTarget, async () => {
  patterns = (await api('/api/anim/my/delete', { kind: 'clock', name: boot.name })).patterns;
  $('#anim-clock').value = BOOT_ANIM; fillAnimSelect('clock'); renderPatterns(); await loadClockAnim();
}, 'Своя анимация удалена');
$('#boot-install').onclick = e => isMy(boot.name)
  ? act(e.currentTarget, () => mySave('clock', myBase(boot.name), { frames: bootFull() }), 'Своя анимация сохранена')
  : confirm(`Установить эту ${clockAnimTitle()} на колонку?`) && act(e.currentTarget, async () => {
  if (isBootAnim()) boot.status = await api('/api/clock/boot', { frames: bootFull() });
  else {
    animCustom = await api('/api/anim/install', { kind: 'clock', name: boot.name, frames: bootFull() });
    boot.status = await api('/api/clock/boot');
    animPending(true);
  }
  if (isBootAnim()) animCustom = await api('/api/anim/custom');
  fillAnimSelect('clock'); renderBootStatus();
}, isBootAnim() ? 'Заставка установлена — покажется при следующей загрузке' : 'Анимация установлена — перезапустите Алису, чтобы она её подхватила');
$('#boot-reset').onclick = e => confirm(`Вернуть стандартную ${clockAnimTitle()}?`) && act(e.currentTarget, async () => {
  if (isBootAnim()) await api('/api/clock/boot/reset', {});
  else { await api('/api/anim/reset', { kind: 'clock', name: boot.name }); animPending(true); }
  await loadBoot();
}, 'Стандартная анимация восстановлена');

// Quasar читает анимации при запуске: после установки или возврата предлагаем перезапустить Алису.
// Заставке не нужно — она и так показывается при следующей загрузке.
const animPending = on => $$('[data-anim-pending]').forEach(e => e.hidden = !on);
$$('[data-anim-restart]').forEach(b => b.onclick = e => confirm('Перезапустить Алису? Она будет недоступна ~30 с.') &&
  act(e.currentTarget, async () => { await api('/api/system', { action: 'quasar_restart' }); animPending(false); aliceRestarting(); }, 'Алиса перезапускается — после этого покажет новые анимации'));

// ---------- анимации кольца ----------
// Файл .led — текст; простые (только кадры и loop) правим и визуально, остальные — текстом.
const ra = { name: null, frames: [], loop: false, simple: true, def: null, timer: 0 };
const rgbHex = c => c.map(x => clamp(x | 0, 0, 255).toString(16).padStart(2, '0')).join('');
function parseLedJS(text) {
  const res = { frames: [], loop: false, simple: true };
  for (const line of text.split('\n')) {
    const p = line.trim().split(/\s+/).filter(Boolean);
    if (!p.length) continue;
    if (p.length === 1 && p[0] === 'loop') { res.loop = true; continue; }
    const cols = p.slice(0, -1), ms = +p[p.length - 1];
    if (p.length >= 2 && Number.isFinite(ms) && cols.every(h => /^[0-9a-f]{6}$/i.test(h))) {
      res.frames.push({ c: cols.map(h => hex2rgb('#' + h)), ms }); continue;
    }
    res.simple = false;
    // rotate: кадр всё равно показываем в предпросмотре
    if (p[0] === 'rotate' && p.slice(1, -1).every(h => /^[0-9a-f]{6}$/i.test(h))) res.frames.push({ c: p.slice(1, -1).map(h => hex2rgb('#' + h)), ms });
  }
  return res;
}
const raText = () => (ra.loop ? 'loop\n' : '') + ra.frames.map(f => f.c.map(rgbHex).join(' ') + ' ' + f.ms).join('\n') + '\n';
const raPixels = () => ra.frames[0]?.c.length || 3;
function raShow(c) {
  const spans = $$('#ra-preview span');
  spans.forEach((s, i) => { const v = c?.[i] || [0, 0, 0]; s.style.background = `rgb(${v})`; s.style.setProperty('--glow', v.some(x => x > 20) ? `rgba(${v},.6)` : 'transparent'); });
}
function renderRa() {
  $('#ra-preview').innerHTML = '<span></span>'.repeat(raPixels());
  raShow(ra.frames[0]?.c);
  $('#ra-loop').checked = ra.loop;
  $('#ra-loop').disabled = !ra.simple;
  $('#ra-complex').hidden = ra.simple;
  $('#ra-frames').hidden = $('#ra-visual-btns').hidden = !ra.simple;
  $('#ra-frames').innerHTML = ra.frames.map((f, i) => `<div class="ra-frame" data-i="${i}"><span class="n">${i + 1}</span>
    ${f.c.map((c, k) => `<input type="color" data-k="${k}" value="#${rgbHex(c)}" aria-label="Кадр ${i + 1}, точка ${k + 1}">`).join('')}
    <input type="number" class="field mono" min="10" max="600000" step="10" value="${f.ms}" aria-label="Длительность кадра ${i + 1}, мс"><span class="meta">мс</span>
    <button class="btn sm icon ghost" data-a="dup" title="Дублировать кадр" aria-label="Дублировать кадр ${i + 1}">${icon('i-plus', 14)}</button>
    <button class="btn sm icon ghost" data-a="del" title="Удалить кадр" aria-label="Удалить кадр ${i + 1}">${icon('i-trash', 14)}</button></div>`).join('');
  const s = $('#ra-status');
  const my = isMy(ra.name);
  s.innerHTML = my ? '<span class="badge ok">своя анимация</span>' : animCustom.ring.includes(ra.name) ? '<span class="badge ok">изменена</span>' : '<span class="badge">стандартная</span>';
  $('#ra-install').textContent = my ? 'Сохранить' : 'Установить на колонку';
  $('#ra-reset').hidden = $('#ra-load-default').hidden = my;
  $('#ra-del-row').hidden = !my;
}
function raSetText(text) {
  $('#ra-text').value = text;
  Object.assign(ra, parseLedJS(text));
  renderRa();
}
function raChanged() { $('#ra-text').value = raText(); }
$('#ra-frames').oninput = e => {
  const row = e.target.closest('.ra-frame'); if (!row) return;
  const f = ra.frames[+row.dataset.i];
  if (e.target.type === 'color') { f.c[+e.target.dataset.k] = hex2rgb(e.target.value); raShow(f.c); }
  else f.ms = clamp(+e.target.value || 100, 10, 600000);
  raChanged();
};
$('#ra-frames').onclick = e => {
  const b = e.target.closest('[data-a]'); if (!b) return;
  const i = +b.closest('.ra-frame').dataset.i;
  if (b.dataset.a === 'dup') ra.frames.splice(i + 1, 0, structuredClone(ra.frames[i]));
  else ra.frames.splice(i, 1);
  raChanged(); renderRa();
};
$('#ra-add').onclick = () => { ra.frames.push({ c: Array.from({ length: raPixels() }, () => hex2rgb(ringHex)), ms: 200 }); raChanged(); renderRa(); };
$('#ra-loop').onchange = e => { ra.loop = e.target.checked; raChanged(); };
$('#ra-text').oninput = e => { Object.assign(ra, parseLedJS(e.target.value)); renderRa(); };
const raSeconds = () => {
  const total = ra.frames.reduce((a, f) => a + f.ms, 0) / 1000;
  return clamp(Math.ceil(ra.loop || !ra.simple ? Math.max(total * 2, 5) : total + 1), 2, 30);
};
$('#ra-preview-btn').onclick = () => {
  clearTimeout(ra.timer);
  if (!ra.frames.length) return toast('Добавьте хотя бы один кадр для предпросмотра', true);
  const until = Date.now() + raSeconds() * 1000;
  let i = 0;
  const step = () => {
    if (Date.now() > until || (i >= ra.frames.length && !ra.loop)) return raShow(ra.frames[0]?.c);
    if (i >= ra.frames.length) i = 0;
    raShow(ra.frames[i].c);
    ra.timer = setTimeout(step, Math.max(16, ra.frames[i++].ms));
  };
  step();
};
$('#ra-device').onclick = e => act(e.currentTarget, () => api('/api/ring/try', { text: $('#ra-text').value, seconds: raSeconds() }), 'Анимация играет на колонке');
async function loadRingAnim() {
  ra.name = $('#anim-ring').value; ra.def = null;
  raSetText((await api('/api/ring/frames?name=' + encodeURIComponent(ra.name))).text || '');
}
$('#anim-ring').onchange = e => {
  if (e.target.value === '__new') return myNewFromSelect('ring', () => ({ text: $('#ra-text').value }), loadRingAnim);
  e.target.dataset.cur = e.target.value;
  loadRingAnim().catch(err => err.offline || toast(err.message, true));
};
$('#ra-load-default').onclick = e => act(e.currentTarget, async () => {
  ra.def ??= (await api('/api/anim/original?kind=ring&name=' + encodeURIComponent(ra.name))).text;
  raSetText(ra.def);
});
const ringAnimTitle = () => `«${animTitle(ra.name)}»`;
$('#ra-del-my').onclick = e => confirm(`Удалить свою анимацию кольца ${ringAnimTitle()}?`) && act(e.currentTarget, async () => {
  patterns = (await api('/api/anim/my/delete', { kind: 'ring', name: ra.name })).patterns;
  $('#anim-ring').value = ''; fillAnimSelect('ring'); renderPatterns(); await loadRingAnim();
}, 'Своя анимация удалена');
$('#ra-install').onclick = e => isMy(ra.name)
  ? act(e.currentTarget, () => mySave('ring', myBase(ra.name), { text: $('#ra-text').value }), 'Своя анимация сохранена')
  : confirm(`Установить анимацию кольца ${ringAnimTitle()} на колонку?`) && act(e.currentTarget, async () => {
  animCustom = await api('/api/anim/install', { kind: 'ring', name: ra.name, text: $('#ra-text').value });
  boot.status = await api('/api/clock/boot');   // хук мог подключиться только что
  fillAnimSelect('ring'); renderRa(); renderBootStatus(); animPending(true);
}, 'Анимация кольца установлена — перезапустите Алису, чтобы она её подхватила');
$('#ra-reset').onclick = e => confirm(`Вернуть стандартную анимацию кольца ${ringAnimTitle()}?`) && act(e.currentTarget, async () => {
  animCustom = await api('/api/anim/reset', { kind: 'ring', name: ra.name });
  boot.status = await api('/api/clock/boot');
  fillAnimSelect('ring'); renderBootStatus(); animPending(true); await loadRingAnim();
}, 'Стандартная анимация восстановлена');

// ---------- bluetooth ----------

// Виджет Bluetooth: сопряжённые устройства, видимость с таймером. Опрашиваем, только пока виден «Обзор».
let btData = null, btTask = 0;
const BT_ICONS = { phone: 'i-bt', 'audio-card': 'i-vol', 'audio-headset': 'i-vol', 'audio-headphones': 'i-vol', computer: 'i-term' };
async function loadBt() {
  if (!online || document.hidden || $('#tab-overview').hidden || $('[data-widget="bt"]').closest('#wz-hidden')) return;
  try { btData = await api('/api/bt'); renderBt(); } catch (e) { if (!e.offline) setListHtml($('#bt-list'), `<div class="empty">${esc(e.message)}</div>`); }
  Scheduler.setInterval(btTask, btData?.pair_until ? 2000 : 5000);
}
btTask = every(3000, loadBt);
function btLeft() {
  const left = btData?.pair_until ? Math.max(0, Math.round(btData.pair_until - Date.now() / 1000)) : 0;
  $('#bt-pair-left').hidden = !left;
  $('#bt-pair-left').textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
}
every(1000, () => btData?.pair_until && btLeft());
function renderBt() {
  const d = btData, conn = d.devices.filter(x => x.connected), vis = d.discoverable;
  const card = $('[data-widget="bt"]');
  $('#bt-power').checked = d.powered;
  $('#bt-off-hint').hidden = d.powered;
  for (const el of [$('#bt-pair-row'), $('#bt-list'), card.querySelector('.vol-bt'), $('#bt-out-row')]) el.hidden = !d.powered;
  // при выводе на Bluetooth-колонку ползунка громкости нет: остаются громкость Алисы и сама колонка
  if (d.out?.on && d.out.state === 'bt') card.querySelector('.vol-bt').hidden = true;
  if (d.out?.supported === false) {
    if (card.querySelector('.vol-bt')) card.querySelector('.vol-bt').hidden = true;
    if ($('#bt-out-row')) $('#bt-out-row').hidden = true;
  }
  if (!d.powered) $('#bt-out-pick').hidden = true;
  $('#bt-badge').textContent = !d.powered ? 'выключен' : conn.length ? 'подключено' : 'включён';
  $('#bt-badge').className = 'badge' + (d.powered ? ' ok' : '');
  $('.bt-pair').classList.toggle('on', vis);
  $('#bt-pair-text').textContent = vis ? 'Колонка видна' : 'Колонка скрыта';
  $('#bt-pair-hint').innerHTML = vis ? `Выберите «${d.alias ? hostHtml(d.alias) : 'колонку'}» в настройках Bluetooth телефона`
    : `Чтобы подключить новое устройство, сделайте её видимой на 3 минуты`;
  $('#bt-pair-btn').textContent = vis ? 'Скрыть' : 'Сделать видимой';
  $('#bt-pair-btn').classList.toggle('primary', !vis);
  btLeft();
  renderBtOut();
  setListHtml($('#bt-list'), d.devices.length ? d.devices.map(x => `<div class="bt-row${x.connected ? ' on' : ''}" data-mac="${esc(x.mac)}">
      <div class="bt-ic">${icon(BT_ICONS[x.icon] || 'i-bt', 18)}</div>
      <div class="info"><b>${x.name ? secret('btn-' + x.mac, x.name) : 'Без имени'}</b>
        <span class="meta">${[x.connected ? 'подключено' : 'сопряжено', x.battery != null ? `батарея ${x.battery}%` : ''].filter(Boolean).join(' · ')} · ${secret('bt-' + x.mac, x.mac)}</span></div>
      <div class="actions">${x.connected ? '<button class="btn sm" data-bta="disconnect">Отключить</button>' : '<button class="btn sm" data-bta="connect">Подключить</button>'}
        <button class="btn sm ghost" data-bta="remove" title="Забыть устройство" aria-label="Забыть устройство">${icon('i-trash', 16)}</button></div></div>`).join('')
    : '<div class="empty">Сопряжённых устройств нет</div>');
}
const btDo = (btn, body, msg) => act(btn, async () => { btData = await api('/api/bt', body); renderBt(); }, msg);
$('#bt-power').onchange = e => {
  const on = e.target.checked;
  if (!on && !confirm('Выключить Bluetooth колонки? Телефоны не смогут подключиться, музыки по Bluetooth не будет. '
      + 'Он останется выключенным и после перезагрузки — пока не включите здесь или фразой «Алиса, включи блютуз».')) { e.target.checked = true; return; }
  btDo(e.target, { action: on ? 'power_on' : 'power_off' }, on ? 'Bluetooth включён' : 'Bluetooth выключен')
    .then(() => { if (btData) $('#bt-power').checked = btData.powered; });
};
// ---- звук через Bluetooth-колонку ----
let btEq = null;
function renderBtOut() {
  const o = btData?.out || { on: false }, last = !o.on && o.last;
  const self = `из колонки «${esc($('#model-name').textContent.trim() || 'Станция')}»`;   // откуда звук на самом деле
  const bt = n => n === 'Bluetooth-колонка' ? 'Bluetooth-колонку' : `«${esc(n)}»`;
  $('#bt-out-row').classList.toggle('on', o.on && o.state === 'bt');
  $('#bt-out-text').innerHTML = !o.on ? `Звук — ${self}`
    : o.state === 'bt' ? `Звук — через ${bt(o.name)}` : `Звук — ${self} · ждём ${bt(o.name)}`;
  $('#bt-out-hint').textContent = !o.on ? (last ? `Можно вернуть звук в «${last.name}» — она уже сопряжена. Алиса перезапустится, играющая музыка остановится`
      : 'Можно играть через Bluetooth-колонку: Алиса слушает и отвечает, а звук — из неё')
    : o.state === 'bt' ? 'Алиса, музыка, её звуки и звуки панели — из Bluetooth-колонки'
    : 'Bluetooth-колонка не подключена, поэтому звук пока из самой колонки. Включите её — подключение и звук вернутся сами';
  $('#bt-out-btn').textContent = o.on ? (o.state === 'bt' ? 'Вернуть звук в колонку' : 'Больше не ждать') : last ? `Играть через «${last.name}»` : 'Выбрать колонку';
  $('#bt-out-btn').classList.toggle('primary', !o.on);
  $('#bt-out-other').hidden = !last;
  if (o.on) $('#bt-out-pick').hidden = true;
  // эквалайзер имеет смысл только когда звук реально идёт в Bluetooth-колонку
  const eqRow = $('#bt-eq-row');
  eqRow.hidden = !(o.on && o.state === 'bt');
  if (!eqRow.hidden && !btEq) loadBtEq();
}
// ---- Аппаратный DSP Станции Макс (Silergy SY6045S) ----
let dspState = null;
let dspEqOpen = false;
let dspEqValues = null;
let dspEqSaved = null;
let dspEqRev = 0;
let dspEqTimer = 0;
let dspEqBusy = false;
let dspEqDragging = null;
async function loadDSP() {
  if (document.hidden || $('#tab-overview').hidden) return;
  try {
    dspState = await api('/api/dsp');
    renderDSP();
  } catch (e) {
    if (!e.offline && $('#card-dsp')) {
      // Игнорируем или скрываем если не поддерживается
    }
  }
}

function renderDSP() {
  const card = $('#card-dsp');
  if (!card) return;
  if (dspState && dspState.supported === false) {
    card.hidden = true;
    return;
  }
  if (!dspState) return;
  card.hidden = false;

  // Бейдж
  const b = $('#dsp-badge');
  if (b) {
    const prof = dspState.profiles?.find(p => p.id === dspState.active_profile);
    b.textContent = prof ? prof.badge || prof.name : dspState.active_profile;
    b.className = 'badge ' + (dspState.active_profile === 'stock' ? 'bad' : 'ok');
  }

  // Кнопки профилей
  $$('#dsp-profiles .pill').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.dspProfile === dspState.active_profile);
  });

  // Регулятор мощности баса
  const bg = dspState.params?.bass_gain ?? 0;
  const sl = $('#dsp-slider-bass');
  const slVal = $('#dsp-slider-bass-val');
  if (sl && sl !== document.activeElement) sl.value = bg;
  if (slVal) slVal.textContent = (bg > 0 ? '+' : '') + Number(bg).toFixed(1) + ' дБ';

  // Чекбоксы фильтров
  if (dspState.filters) {
    if ($('#dsp-filter-subsonic')) $('#dsp-filter-subsonic').checked = !!dspState.filters.subsonic;
    if ($('#dsp-filter-notch120')) $('#dsp-filter-notch120').checked = !!dspState.filters.notch120;
    if ($('#dsp-filter-phase')) $('#dsp-filter-phase').checked = !!dspState.filters.phase;
    if ($('#dsp-filter-treble')) $('#dsp-filter-treble').checked = !!dspState.filters.treble;
    if ($('#dsp-filter-drc_bypass')) $('#dsp-filter-drc_bypass').checked = !!dspState.filters.drc_bypass;
  }

  // Описание
  const descEl = $('#dsp-desc');
  if (descEl) {
    const prof = dspState.profiles?.find(p => p.id === dspState.active_profile);
    if (prof) descEl.textContent = prof.desc;
  }

  // Отрисовка графика АЧХ
  if (dspState.response && dspState.response.freqs) {
    renderDSPCurve(dspState.response);
  }
  if (dspEqOpen) renderDSPEq();
}

let dspCurveCurrent = null;
let dspCurveAnimId = null;
let dspInspectorInited = false;

function renderDSPCurve(resp) {
  if (!resp || !resp.freqs || !resp.woofer_db) return;
  const freqs = resp.freqs;
  const toX = f => (Math.log10(Math.max(f, 20) / 20) / 3.0) * 400;
  const toY = db => {
    const clamped = Math.max(-36, Math.min(12, db));
    return 30 - (clamped * 2.2);
  };

  const makeLinePath = arr => {
    if (!arr || !arr.length) return '';
    return arr.map((db, i) => `${i === 0 ? 'M' : 'L'} ${toX(freqs[i]).toFixed(1)} ${toY(db).toFixed(1)}`).join(' ');
  };

  const makeAreaPath = arr => {
    if (!arr || !arr.length) return '';
    const line = arr.map((db, i) => `${i === 0 ? 'M' : 'L'} ${toX(freqs[i]).toFixed(1)} ${toY(db).toFixed(1)}`).join(' ');
    const lastX = toX(freqs[arr.length - 1]).toFixed(1);
    const firstX = toX(freqs[0]).toFixed(1);
    return `${line} L ${lastX} 110 L ${firstX} 110 Z`;
  };

  const pWoofer = $('#dsp-curve-woofer');
  const pTweeter = $('#dsp-curve-tweeter');
  const pSystem = $('#dsp-curve-system');
  const pArea = $('#dsp-curve-area');

  const targetW = resp.woofer_db;
  const targetT = resp.tweeter_db;
  const targetS = resp.system_db;

  const applyFrame = (wArr, tArr, sArr) => {
    if (pWoofer) pWoofer.setAttribute('d', makeLinePath(wArr));
    if (pTweeter) pTweeter.setAttribute('d', makeLinePath(tArr));
    if (pSystem) pSystem.setAttribute('d', makeLinePath(sArr));
    if (pArea) pArea.setAttribute('d', makeAreaPath(sArr));
  };

  initDSPInspector(freqs, toX, toY);

  const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
  if (!dspCurveCurrent || reducedMotion) {
    if (dspCurveAnimId) cancelAnimationFrame(dspCurveAnimId);
    dspCurveCurrent = { w: [...targetW], t: [...targetT], s: [...targetS] };
    applyFrame(targetW, targetT, targetS);
    return;
  }

  // Smooth animation tween
  if (dspCurveAnimId) cancelAnimationFrame(dspCurveAnimId);

  const startW = [...dspCurveCurrent.w];
  const startT = [...dspCurveCurrent.t];
  const startS = [...dspCurveCurrent.s];
  const startTime = performance.now();
  const duration = 380; // 380 ms smooth natural easing

  const easeOutCubic = t => 1 - Math.pow(1 - t, 3);

  const step = now => {
    const elapsed = now - startTime;
    const progress = Math.min(1.0, elapsed / duration);
    const ease = easeOutCubic(progress);

    const curW = startW.map((v, i) => v + (targetW[i] - v) * ease);
    const curT = startT.map((v, i) => v + (targetT[i] - v) * ease);
    const curS = startS.map((v, i) => v + (targetS[i] - v) * ease);

    dspCurveCurrent = { w: curW, t: curT, s: curS };
    applyFrame(curW, curT, curS);

    if (progress < 1.0) {
      dspCurveAnimId = requestAnimationFrame(step);
    } else {
      dspCurveAnimId = null;
      dspCurveCurrent = { w: [...targetW], t: [...targetT], s: [...targetS] };
      applyFrame(targetW, targetT, targetS);
    }
  };

  dspCurveAnimId = requestAnimationFrame(step);
}

function initDSPInspector(freqs, toX, toY) {
  if (dspInspectorInited) return;
  const svg = $('#dsp-svg');
  const line = $('#dsp-cursor-line');
  const dot = $('#dsp-cursor-dot');
  const label = $('#dsp-inspect-val');
  if (!svg || !line || !dot) return;
  dspInspectorInited = true;

  const handlePointer = e => {
    if (!dspCurveCurrent?.s || !freqs) return;
    const rect = svg.getBoundingClientRect();
    const clientX = e.touches ? e.touches[0].clientX : e.clientX;
    const svgX = ((clientX - rect.left) / rect.width) * 400;
    if (svgX < 0 || svgX > 400) return;

    let closestIdx = 0;
    let minDiff = Infinity;
    for (let i = 0; i < freqs.length; i++) {
      const diff = Math.abs(toX(freqs[i]) - svgX);
      if (diff < minDiff) {
        minDiff = diff;
        closestIdx = i;
      }
    }

    const f = freqs[closestIdx];
    const curDb = dspCurveCurrent.s[closestIdx];
    const xPos = toX(f);
    const yPos = toY(curDb);

    line.setAttribute('x1', xPos.toFixed(1));
    line.setAttribute('x2', xPos.toFixed(1));
    line.style.display = 'block';

    dot.setAttribute('cx', xPos.toFixed(1));
    dot.setAttribute('cy', yPos.toFixed(1));
    dot.style.display = 'block';

    if (label) {
      const fStr = f >= 1000 ? (f / 1000).toFixed(1) + ' kHz' : Math.round(f) + ' Hz';
      const dbStr = (curDb > 0 ? '+' : '') + curDb.toFixed(1) + ' dB';
      label.innerHTML = `<span style="color:var(--text)"><b>${fStr}</b></span>: <span style="color:var(--accent, #ffcc00)"><b>${dbStr}</b></span>`;
    }
  };

  const handleLeave = () => {
    line.style.display = 'none';
    dot.style.display = 'none';
    if (label) label.textContent = '20Hz – 20kHz';
  };

  svg.addEventListener('mousemove', handlePointer);
  svg.addEventListener('touchmove', handlePointer, { passive: true });
  svg.addEventListener('mouseleave', handleLeave);
  svg.addEventListener('touchend', handleLeave);
}

// ---- Полная настройка АЧХ: 10 аппаратных полос ----
const DSP_EQ_TOP = 16, DSP_EQ_BOTTOM = 268, DSP_EQ_LEFT = 56, DSP_EQ_RIGHT = 784;
const DSP_EQ_DB_TOP = 12, DSP_EQ_DB_BOTTOM = -18;
const dspEqX = f => DSP_EQ_LEFT + (Math.log10(Math.max(f, 20) / 20) / 3) * (DSP_EQ_RIGHT - DSP_EQ_LEFT);
const dspEqY = db => {
  const clamped = Math.max(DSP_EQ_DB_BOTTOM, Math.min(DSP_EQ_DB_TOP, db));
  return DSP_EQ_TOP + ((DSP_EQ_DB_TOP - clamped) / (DSP_EQ_DB_TOP - DSP_EQ_DB_BOTTOM)) * (DSP_EQ_BOTTOM - DSP_EQ_TOP);
};

function dspEqStatus(text) {
  const el = $('#dsp-eq-status');
  if (el) el.textContent = text;
}

function dspPeakingDb(f, f0, q, gain) {
  if (!gain || Math.abs(gain) < 0.05) return 0;
  const A = Math.pow(10, gain / 40);
  const w0 = 2 * Math.PI * f0 / 48000;
  const alpha = Math.sin(w0) / (2 * q);
  const c = Math.cos(w0);
  const b0 = 1 + alpha * A, b1 = -2 * c, b2 = 1 - alpha * A;
  const a0 = 1 + alpha / A, a1 = -2 * c, a2 = 1 - alpha / A;
  const w = 2 * Math.PI * f / 48000;
  const cw = Math.cos(w), sw = Math.sin(w), c2 = Math.cos(2 * w), s2 = Math.sin(2 * w);
  const nr = b0 + b1 * cw + b2 * c2, ni = -b1 * sw - b2 * s2;
  const dr = a0 + a1 * cw + a2 * c2, di = -a1 * sw - a2 * s2;
  const mag = Math.sqrt((nr * nr + ni * ni) / Math.max(dr * dr + di * di, 1e-18));
  return 20 * Math.log10(Math.max(mag, 1e-6));
}

function dspEqSumAt(f, values) {
  const bands = dspState?.eq_bands || [];
  const current = values || dspEqValuesNow();
  let sum = 0;
  for (let i = 0; i < bands.length; i++) {
    const band = bands[i];
    sum += dspPeakingDb(f, band.freq, Number(band.q) || 1.41, Number(current[i]) || 0);
  }
  return sum;
}

function dspEqResponse(freqs, values) {
  const bands = dspState?.eq_bands || [];
  return freqs.map(f => {
    let sum = 0;
    for (let i = 0; i < bands.length; i++) {
      const band = bands[i];
      sum += dspPeakingDb(f, band.freq, Number(band.q) || 1.41, Number(values[i]) || 0);
    }
    return sum;
  });
}

function dspEqDrawGrid() {
  const grid = $('#dsp-eq-grid');
  if (!grid || grid.dataset.ready) return;
  grid.dataset.ready = '1';
  const parts = [];
  for (const db of [12, 6, 0, -6, -12, -18]) {
    const y = dspEqY(db).toFixed(1);
    parts.push(`<line x1="${DSP_EQ_LEFT}" y1="${y}" x2="${DSP_EQ_RIGHT}" y2="${y}" stroke="var(--line)" stroke-width="1" ${db === 0 ? '' : 'stroke-dasharray="3 3"'} opacity="${db === 0 ? '.9' : '.5'}"/>`);
    parts.push(`<text x="${DSP_EQ_LEFT - 8}" y="${(Number(y) + 4).toFixed(1)}" text-anchor="end" fill="var(--muted)" font-size="11" font-family="monospace">${db > 0 ? '+' : ''}${db} dB</text>`);
  }
  for (const f of [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]) {
    const x = dspEqX(f).toFixed(1);
    parts.push(`<line x1="${x}" y1="${DSP_EQ_TOP}" x2="${x}" y2="${DSP_EQ_BOTTOM}" stroke="var(--line)" stroke-width="1" stroke-dasharray="3 3" opacity=".4"/>`);
    parts.push(`<text x="${x}" y="${DSP_EQ_BOTTOM + 18}" text-anchor="middle" fill="var(--muted)" font-size="11" font-family="monospace">${f >= 1000 ? (f / 1000) + 'k' : f}</text>`);
  }
  grid.innerHTML = parts.join('');
}

function dspEqDrawCurve(freqs, total, eq) {
  const line = arr => arr.map((db, i) => `${i ? 'L' : 'M'} ${dspEqX(freqs[i]).toFixed(1)} ${dspEqY(db).toFixed(1)}`).join(' ');
  const pSaved = $('#dsp-eq-saved');
  const pLine = $('#dsp-eq-line');
  const pArea = $('#dsp-eq-area');
  if (pSaved) pSaved.setAttribute('d', line(total));
  if (pLine) pLine.setAttribute('d', line(eq));
  if (pArea) {
    const bottom = DSP_EQ_BOTTOM.toFixed(1);
    pArea.setAttribute('d', `${line(eq)} L ${dspEqX(freqs[freqs.length - 1]).toFixed(1)} ${bottom} L ${dspEqX(freqs[0]).toFixed(1)} ${bottom} Z`);
  }
}

function dspEqDrawPoints(bands, values) {
  const group = $('#dsp-eq-points');
  if (!group) return;
  if (group.dataset.bands !== String(bands.length)) {
    group.dataset.bands = String(bands.length);
    group.innerHTML = bands.map((band, i) => `<circle data-eq-point="${i}" r="7" fill="var(--accent)" stroke="#fff" stroke-width="2" style="cursor:ns-resize" tabindex="0" role="slider" aria-label="${esc(band.label)}" aria-valuemin="${band.min ?? -12}" aria-valuemax="${band.max ?? 9}" aria-valuenow="0"><title>${esc(band.label)}</title></circle>`).join('');
  }
  $$('#dsp-eq-points circle').forEach((circle, i) => {
    const gain = Number(values[i] || 0);
    circle.setAttribute('cx', dspEqX(bands[i].freq).toFixed(1));
    circle.setAttribute('cy', dspEqY(dspEqSumAt(bands[i].freq, values)).toFixed(1));
    circle.setAttribute('aria-valuenow', String(gain));
    circle.dataset.gain = String(gain);
  });
}

function dspEqValuesNow() {
  const count = (dspState?.eq_bands || []).length || 10;
  if (!dspEqValues) dspEqValues = Array(count).fill(0);
  while (dspEqValues.length < count) dspEqValues.push(0);
  return dspEqValues;
}

function renderDSPEqCurveOnly() {
  const resp = dspState?.response;
  if (!resp?.freqs || !resp.system_db || !dspEqSaved) return;
  const values = dspEqValuesNow();
  const eq = dspEqResponse(resp.freqs, values);
  const savedEq = dspEqResponse(resp.freqs, dspEqSaved);
  const clamp = v => Math.max(DSP_EQ_DB_BOTTOM, Math.min(DSP_EQ_DB_TOP, v));
  const total = resp.system_db.map((v, i) => clamp(v + eq[i] - savedEq[i]));
  dspEqDrawCurve(resp.freqs, total, eq.map(clamp));
}

function renderDSPEq() {
  if (!dspState?.supported) return;
  const bands = dspState.eq_bands || [];
  if (!bands.length) return;
  dspEqDrawGrid();
  const values = dspEqValuesNow();
  if (!dspEqSaved) dspEqSaved = values.slice();

  const wrap = $('#dsp-eq-bands');
  if (wrap && wrap.dataset.bands !== String(bands.length)) {
    wrap.dataset.bands = String(bands.length);
    const row = (band, i) => `
      <label class="dsp-eq-band">
        <span>${esc(band.label)}</span>
        <input type="range" min="${band.min ?? -12}" max="${band.max ?? 9}" step="0.5" value="0" data-eq-band="${i}" aria-label="${esc(band.label)}">
        <b id="dsp-eq-val-${i}">0.0 дБ</b>
      </label>`;
    const half = Math.ceil(bands.length / 2);
    wrap.innerHTML = `<div class="dsp-eq-col">${bands.slice(0, half).map(row).join('')}</div>`
      + `<div class="dsp-eq-col">${bands.slice(half).map((band, i) => row(band, i + half)).join('')}</div>`;
    $$('#dsp-eq-bands input').forEach(input => {
      input.addEventListener('input', () => dspEqSetValue(Number(input.dataset.eqBand), Number(input.value), true));
      input.addEventListener('change', () => dspEqSchedule());
    });
  }
  $$('#dsp-eq-bands input').forEach((input, i) => {
    input.value = values[i] ?? 0;
    const val = $('#dsp-eq-val-' + i);
    if (val) val.textContent = (values[i] > 0 ? '+' : '') + Number(values[i] || 0).toFixed(1) + ' дБ';
  });
  dspEqDrawPoints(bands, values);
  renderDSPEqCurveOnly();
}

function dspEqSetValue(index, raw, fromSlider = false) {
  const bands = dspState?.eq_bands || [];
  const band = bands[index];
  if (!band) return;
  const values = dspEqValuesNow();
  const min = Number(band.min ?? -12), max = Number(band.max ?? 9);
  const value = Math.max(min, Math.min(max, Math.round(Number(raw) * 2) / 2));
  values[index] = value;
  const input = document.querySelector(`#dsp-eq-bands input[data-eq-band="${index}"]`);
  if (input && !fromSlider) input.value = value;
  const val = $('#dsp-eq-val-' + index);
  if (val) val.textContent = (value > 0 ? '+' : '') + value.toFixed(1) + ' дБ';
  dspEqDrawPoints(bands, values);
  renderDSPEqCurveOnly();
  dspEqSchedule();
}

function dspEqSetTotal(index, desiredDb) {
  const band = (dspState?.eq_bands || [])[index];
  if (!band) return;
  const values = dspEqValuesNow();
  const currentEq = dspEqSumAt(band.freq, values);
  dspEqSetValue(index, Number(values[index] || 0) + (desiredDb - currentEq));
}

function dspEqSchedule() {
  dspEqRev++;
  dspEqStatus('Есть изменения');
  clearTimeout(dspEqTimer);
  dspEqTimer = setTimeout(dspEqCommit, 250);
}

async function dspEqCommit() {
  if (dspEqBusy || !dspEqValues) return;
  const rev = dspEqRev;
  const values = dspEqValues.slice();
  dspEqBusy = true;
  dspEqStatus('Применяю…');
  try {
    const res = await api('/api/dsp', { action: 'set_eq', bands: values });
    if (res && res.ok === false) throw new Error(res.error || 'Не удалось применить эквалайзер');
    dspEqSaved = values.slice();
    if (res && res.supported !== undefined) dspState = res;
    else if (dspState) { dspState.eq = values.slice(); dspState.active_profile = 'custom'; }
    if (rev === dspEqRev) {
      dspEqStatus('Сохранено');
      renderDSP();
      renderDSPEq();
    }
  } catch (e) {
    dspEqStatus('Ошибка: ' + e.message);
    toast(e.message, true);
  } finally {
    dspEqBusy = false;
    if (rev !== dspEqRev) dspEqCommit();
  }
}

function dspEqPointerFreq(e) {
  const svg = $('#dsp-eq-svg');
  const rect = svg.getBoundingClientRect();
  const x = ((e.clientX - rect.left) / rect.width) * 800;
  const ratio = Math.max(0, Math.min(1, (x - DSP_EQ_LEFT) / (DSP_EQ_RIGHT - DSP_EQ_LEFT)));
  return 20 * Math.pow(1000, ratio);
}

function dspEqPointerGain(e) {
  const svg = $('#dsp-eq-svg');
  const rect = svg.getBoundingClientRect();
  const y = ((e.clientY - rect.top) / rect.height) * 300;
  const db = DSP_EQ_DB_TOP - ((y - DSP_EQ_TOP) / (DSP_EQ_BOTTOM - DSP_EQ_TOP)) * (DSP_EQ_DB_TOP - DSP_EQ_DB_BOTTOM);
  return db;
}

function dspEqNearestBand(x) {
  const bands = dspState?.eq_bands || [];
  let best = 0, distance = Infinity;
  bands.forEach((band, i) => {
    const d = Math.abs(dspEqX(band.freq) - x);
    if (d < distance) { distance = d; best = i; }
  });
  return best;
}

function dspEqBindGraph() {
  const svg = $('#dsp-eq-svg');
  if (!svg || svg.dataset.bound) return;
  svg.dataset.bound = '1';
  const cursor = $('#dsp-eq-cursor');
  const readout = $('#dsp-eq-readout');

  const moveCursor = (freq, label) => {
    if (!cursor) return;
    const x = dspEqX(freq).toFixed(1);
    cursor.setAttribute('x1', x);
    cursor.setAttribute('x2', x);
    cursor.style.display = 'block';
    if (readout) readout.textContent = label;
  };

  svg.addEventListener('pointerdown', e => {
    if (!dspEqOpen) return;
    const rect = svg.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * 800;
    const point = e.target.closest?.('[data-eq-point]');
    dspEqDragging = point ? Number(point.dataset.eqPoint) : dspEqNearestBand(x);
    svg.setPointerCapture?.(e.pointerId);
    dspEqSetTotal(dspEqDragging, dspEqPointerGain(e));
    e.preventDefault();
  });

  svg.addEventListener('pointermove', e => {
    if (!dspEqOpen) return;
    const index = dspEqDragging ?? dspEqNearestBand(dspEqX(dspEqPointerFreq(e)));
    const band = (dspState?.eq_bands || [])[index];
    if (!band) return;
    if (dspEqDragging !== null) dspEqSetTotal(index, dspEqPointerGain(e));
    const gain = Number(dspEqValuesNow()[index] || 0);
    const sum = dspEqSumAt(band.freq);
    moveCursor(band.freq, `${band.label}: полоса ${gain > 0 ? '+' : ''}${gain.toFixed(1)} дБ · EQ ${sum > 0 ? '+' : ''}${sum.toFixed(1)} дБ`);
    if (dspEqDragging !== null) e.preventDefault();
  });

  const endDrag = e => {
    if (dspEqDragging === null) return;
    dspEqDragging = null;
    svg.releasePointerCapture?.(e.pointerId);
    dspEqSchedule();
  };
  svg.addEventListener('pointerup', endDrag);
  svg.addEventListener('pointercancel', endDrag);
  svg.addEventListener('pointerleave', () => {
    if (dspEqDragging !== null) return;
    if (cursor) cursor.style.display = 'none';
    if (readout) readout.textContent = '20 Гц – 20 кГц';
  });
  svg.addEventListener('keydown', e => {
    const point = e.target.closest?.('[data-eq-point]');
    if (!point) return;
    const index = Number(point.dataset.eqPoint);
    const step = e.shiftKey ? 1 : 0.5;
    const current = Number(dspEqValuesNow()[index] || 0);
    if (e.key === 'ArrowUp' || e.key === 'ArrowRight') { dspEqSetValue(index, current + step); e.preventDefault(); }
    else if (e.key === 'ArrowDown' || e.key === 'ArrowLeft') { dspEqSetValue(index, current - step); e.preventDefault(); }
  });
}

function openDSPEq() {
  if (!dspState?.supported) { toast('Аппаратный DSP доступен только на Станции Макс', true); return; }
  const dialog = $('#dsp-eq-dialog');
  if (!dialog || dialog.open) return;
  const count = (dspState.eq_bands || []).length || 10;
  dspEqValues = (dspState.eq || []).slice(0, count);
  while (dspEqValues.length < count) dspEqValues.push(0);
  dspEqSaved = dspEqValues.slice();
  dspEqRev = 0;
  dspEqBusy = false;
  dspEqDragging = null;
  dspEqDrawGrid();
  dialog.showModal();
  dspEqOpen = true;
  dspEqStatus('Сохранено');
  renderDSPEq();
}

function closeDSPEq() {
  const dialog = $('#dsp-eq-dialog');
  if (dialog?.open) dialog.close();
  dspEqOpen = false;
  dspEqDragging = null;
}

// Привязка кликов по кнопкам пресетов
document.addEventListener('DOMContentLoaded', () => {
  $$('#dsp-profiles .pill').forEach(btn => {
    btn.onclick = () => act(btn, async () => {
      const prof = btn.dataset.dspProfile;
      $$('#dsp-profiles .pill').forEach(b => b.classList.toggle('active', b.dataset.dspProfile === prof));
      const res = await api('/api/dsp', { profile: prof });
      if (res && res.supported !== undefined) {
        dspState = res;
      } else if (dspState && res && res.profile) {
        dspState.active_profile = res.profile;
      }
      renderDSP();
      toast(`Режим звука: ${btn.textContent.trim()}`);
    });
  });

  // Привязка регулятора мощности баса
  const sl = $('#dsp-slider-bass');
  const slVal = $('#dsp-slider-bass-val');
  if (sl) {
    sl.oninput = () => {
      const v = parseFloat(sl.value);
      if (slVal) slVal.textContent = (v > 0 ? '+' : '') + v.toFixed(1) + ' дБ';
      if (dspState && dspState.response && dspState.response.freqs) {
        const baseGain = (dspState.params && dspState.params.bass_gain !== undefined) ? dspState.params.bass_gain : 0;
        const diff = v - baseGain;
        const previewResp = {
          freqs: dspState.response.freqs,
          woofer_db: dspState.response.woofer_db.map((val, i) => {
            const f = dspState.response.freqs[i];
            const shelfFactor = 1.0 / (1.0 + Math.pow(f / 60.0, 2));
            return val + diff * shelfFactor;
          }),
          tweeter_db: [...dspState.response.tweeter_db],
          system_db: dspState.response.system_db.map((val, i) => {
            const f = dspState.response.freqs[i];
            const shelfFactor = 1.0 / (1.0 + Math.pow(f / 60.0, 2));
            return val + diff * shelfFactor;
          })
        };
        renderDSPCurve(previewResp);
      }
    };
    sl.onchange = async () => {
      const v = parseFloat(sl.value);
      try {
        const res = await api('/api/dsp', { action: 'set_params', bass_gain: v });
        if (res && res.supported !== undefined) {
          dspState = res;
        } else if (dspState && dspState.params) {
          dspState.params.bass_gain = v;
          dspState.active_profile = 'custom';
        }
        renderDSP();
        toast(`Бас: ${(v > 0 ? '+' : '') + v.toFixed(1)} дБ`);
      } catch (e) {
        toast(e.message, true);
      }
    };
  }

  // Привязка чекбоксов фильтров
  ['subsonic', 'notch120', 'phase', 'treble', 'drc_bypass'].forEach(id => {
    const chk = $('#dsp-filter-' + id);
    if (chk) {
      chk.onchange = async () => {
        try {
          const res = await api('/api/dsp', { action: 'set_filter', filter: id, enabled: chk.checked });
          if (res && res.supported !== undefined) {
            dspState = res;
          } else if (dspState) {
            dspState.filters = dspState.filters || {};
            dspState.filters[id] = chk.checked;
          }
          renderDSP();
        } catch (e) {
          chk.checked = !chk.checked;
          toast(e.message, true);
        }
      };
    }
  });

  // Большое окно настройки АЧХ
  dspEqDrawGrid();
  dspEqBindGraph();
  const eqOpen = $('#dsp-open-eq');
  if (eqOpen) eqOpen.onclick = openDSPEq;
  const eqGraph = $('#dsp-graph-open');
  if (eqGraph) {
    eqGraph.onclick = openDSPEq;
    eqGraph.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDSPEq(); }
    });
  }
  const eqDialog = $('#dsp-eq-dialog');
  if (eqDialog) {
    const eqClose = $('#dsp-eq-close');
    const eqDone = $('#dsp-eq-done');
    if (eqClose) eqClose.onclick = closeDSPEq;
    if (eqDone) eqDone.onclick = closeDSPEq;
    eqDialog.addEventListener('close', () => { dspEqOpen = false; dspEqDragging = null; });
    eqDialog.addEventListener('click', e => { if (e.target === eqDialog) closeDSPEq(); });
  }
  const eqFlat = $('#dsp-eq-flat');
  if (eqFlat) eqFlat.onclick = () => {
    if (!dspEqValues) return;
    dspEqValues = dspEqValues.map(() => 0);
    dspEqStatus('Есть изменения');
    renderDSPEq();
    dspEqSchedule();
  };
});


// ---- Светомузыка (Party Mode) ----
let partyTask = 0;
let partyData = null;
let partyBulbsConfig = [];
let partyEventsBound = false;
let partyCinemaSeeking = false;

const CINEMA_SPELL_NAMES = {
  wand_spark: 'Люмос',
  spell_lumos: 'Люмос',
  spell_lumos_2: 'Люмос',
  spell_lumos_maxima: 'Люмос Максима',
  spell_patronus: 'Экспекто Патронум',
  great_patronus: 'Экспекто Патронум',
  spell_avada: 'Авада',
  spell_expelliarmus: 'Экспеллиармус',
  spell_bombarda: 'Бомбарда',
  spell_ridikulus: 'Ридикулус',
};

const fmtCinemaTime = s => {
  s = Math.max(0, Math.floor(Number(s) || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h > 0 ? h + ':' + String(m).padStart(2, '0') : String(m)) + ':' + String(sec).padStart(2, '0');
};

async function loadParty() {
  initPartyEvents();
  if (!online || document.hidden || !$('#tab-party') || $('#tab-party').hidden) return;
  try {
    partyData = await api('/api/party/status');
    try { partyBulbsConfig = await api('/api/party/bulbs'); } catch {}
    renderParty();
  } catch (e) {
    if (!e.offline) {
      const b = $('#party-state-badge');
      if (b) { b.textContent = 'ошибка'; b.className = 'badge bad'; }
    }
  }
  Scheduler.setInterval(partyTask, (partyData && partyData.running) ? 1500 : 4000);
}
partyTask = every(4000, loadParty);

function renderParty() {
  const d = partyData;
  if (!d) return;

  // Service badge & button
  const badge = $('#party-state-badge');
  const toggleBtn = $('#party-toggle-service');
  if (badge && toggleBtn) {
    if (d.running) {
      badge.textContent = 'активна';
      badge.className = 'badge ok';
      toggleBtn.textContent = 'Остановить службу';
      toggleBtn.className = 'btn sm danger-soft';
    } else {
      badge.textContent = 'остановлена';
      badge.className = 'badge';
      toggleBtn.textContent = 'Запустить службу';
      toggleBtn.className = 'btn sm primary';
    }
  }

  // Active Mode Grid & Badge
  const activeMode = d.active_mode || 'party';
  const modeBtn = $(`#party-modes-grid button[data-mode="${activeMode}"]`);
  const modeInfo = Array.isArray(d.modes) ? d.modes.find(m => m.id === activeMode) : null;
  const activeBadge = $('#party-active-mode-badge');
  if (activeBadge) {
    const iconSpan = modeBtn?.querySelector('span')?.textContent || modeInfo?.icon || '';
    const nameB = modeBtn?.querySelector('b')?.textContent || modeInfo?.title || (activeMode === 'cinema' ? 'Кинотеатр 4D' : activeMode);
    activeBadge.textContent = `${iconSpan} ${nameB}`.trim();
  }
  $$('#party-modes-grid button').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === activeMode);
  });

  // Speed & Brightness Sliders
  const slSpeed = $('#party-slider-speed');
  const slSpeedVal = $('#party-slider-speed-val');
  if (slSpeed && slSpeed !== document.activeElement) {
    const curSpeed = Number(d.speed ?? 1.0);
    slSpeed.value = curSpeed;
    if (slSpeedVal) slSpeedVal.textContent = curSpeed.toFixed(1) + 'x';
  }

  const slBright = $('#party-slider-brightness');
  const slBrightVal = $('#party-slider-brightness-val');
  if (slBright && slBright !== document.activeElement) {
    const curBright = Number(d.brightness ?? 80);
    slBright.value = curBright;
    if (slBrightVal) slBrightVal.textContent = curBright + '%';
  }

  // Master switch
  const masterSw = $('#party-master-toggle');
  if (masterSw && masterSw !== document.activeElement) {
    masterSw.checked = !!d.master_enabled;
  }

  // Strobe indicator
  const strDot = $('#party-strobe-dot');
  const strTxt = $('#party-strobe-text');
  if (strDot && strTxt) {
    if (d.strobe_active && !d.in_silence) {
      strDot.className = 'dot on';
      strTxt.textContent = 'АКТИВЕН ⚡';
    } else {
      strDot.className = 'dot';
      strTxt.textContent = 'Покой / Белый';
    }
  }

  // Bluetooth TV mode buttons
  const btMode = d.bt_mode || 'off';
  $$('#party-bt-mode button').forEach(b => {
    b.classList.toggle('active', b.dataset.v === btMode);
  });

  // BT badge
  const btDot = $('#party-bt-dot');
  const btTxt = $('#party-bt-text');
  if (btDot && btTxt) {
    if (d.bt_playing) {
      btDot.className = 'dot on';
      btTxt.textContent = 'Играет звук с ТВ';
    } else {
      btDot.className = 'dot';
      btTxt.textContent = 'Ожидание';
    }
  }

  // Color interval buttons
  const colInt = String(d.color_interval || 16);
  $$('#party-col-interval button').forEach(b => {
    b.classList.toggle('active', b.dataset.v === colInt);
  });

  // Sync mode buttons
  const syncMode = d.sync_mode || 'unison';
  $$('#party-sync-mode button').forEach(b => {
    b.classList.toggle('active', b.dataset.v === syncMode);
  });

  // Cinema 4D
  const cin = d.cinema || {};
  const cinPlaying = !!cin.playing;
  const cinTime = Math.max(0, Math.round(Number(cin.time_sec) || 0));
  const cinDuration = Math.max(0, Math.round(Number(cin.duration_sec) || 0));
  const cinSeekMax = Math.max(1, cinDuration);
  const cinOffset = Number(cin.offset_sec) || 0;
  const cinBadge = $('#party-cinema-badge');
  if (cinBadge) {
    cinBadge.textContent = d.cinema ? (cinPlaying ? '▶ Играет' : '⏸ Пауза') : '—';
    cinBadge.className = 'badge' + (cinPlaying ? ' ok' : '');
  }
  const cinTitle = $('#party-cinema-title');
  if (cinTitle) {
    cinTitle.textContent = cin.title
      ? `${cin.title} · Ambilight и магические спецэффекты`
      : 'Ambilight и магические спецэффекты в такт фильму. Запустите воспроизведение или выберите режим «Кинотеатр 4D».';
  }
  const cinToggle = $('#party-cinema-toggle');
  if (cinToggle) {
    cinToggle.textContent = cinPlaying ? '⏸ Пауза' : '▶ Играть';
    cinToggle.className = 'btn sm' + (cinPlaying ? '' : ' primary');
  }
  const cinSeek = $('#party-cinema-seek');
  if (cinSeek) {
    if (Number(cinSeek.max) !== cinSeekMax) cinSeek.max = cinSeekMax;
    if (!partyCinemaSeeking) cinSeek.value = Math.min(cinTime, cinSeekMax);
  }
  const cinShownTime = partyCinemaSeeking && cinSeek ? Number(cinSeek.value) : cinTime;
  const cinTimeEl = $('#party-cinema-time');
  if (cinTimeEl) cinTimeEl.textContent = `${fmtCinemaTime(cinShownTime)} / ${fmtCinemaTime(cinDuration)}`;
  const cinScene = $('#party-cinema-scene');
  if (cinScene) cinScene.textContent = `Сцена: ${cin.scene || '—'}`;
  const cinSpell = $('#party-cinema-spell');
  if (cinSpell) {
    const spellName = cin.active_spell ? (CINEMA_SPELL_NAMES[cin.active_spell] || cin.active_spell) : '—';
    cinSpell.textContent = `Спелл: ${spellName}`;
  }
  const cinOffsetEl = $('#party-cinema-offset');
  if (cinOffsetEl) cinOffsetEl.textContent = `Смещение: ${cinOffset > 0 ? '+' : ''}${cinOffset.toFixed(1)} с`;

  // Bulbs list & count
  const cfgList = Array.isArray(partyBulbsConfig) ? partyBulbsConfig : [];
  const statusBulbs = (d && Array.isArray(d.bulbs)) ? d.bulbs : [];
  const bulbs = cfgList.length > 0 ? cfgList.map(b => {
    const live = statusBulbs.find(x => x.id === b.id);
    return live ? { ...b, connected: live.connected, val: live.val } : b;
  }) : statusBulbs;
  let onlineCount = 0;
  const listEl = $('#party-bulbs-list');
  if (listEl) {
    if (bulbs.length > 0) {
      let html = '';
      bulbs.forEach(b => {
        if (b.connected) onlineCount++;
        const dotClass = b.connected ? 'dot on' : 'dot off';
        const statusTxt = b.connected ? 'В сети' : 'Сбой / поиск...';
        const roleTxt = b.role === 'kick' ? 'Люстра (Kick 50–120 Гц)'
          : (b.role === 'ambient' ? 'Люстра (Фон)'
          : (b.side === 'right' ? 'Правая бра (Snare 1 кГц)' : 'Левая бра (Snare 1 кГц)'));
        html += `
          <div style="display:flex; align-items:center; justify-content:space-between; padding:10px 14px; background:var(--card-sub); border:1px solid var(--border); border-radius:12px;">
            <div>
              <div style="font-weight:600; font-size:14px; display:flex; align-items:center; gap:6px;">
                <span>${esc(b.name)}</span>
                <span class="${dotClass}" title="${esc(statusTxt)}"></span>
              </div>
              <div class="meta" style="font-size:12px; margin-top:2px;">${esc(b.ip)} • ${esc(roleTxt)}</div>
            </div>
            <div style="display:flex; align-items:center; gap:6px; font-size:13px; font-weight:500;">
              <button class="btn sm" type="button" data-party-test="${esc(b.id)}" title="Мигнуть 3 раза зелёным светом">💡 Тест</button>
              <button class="btn sm" type="button" data-party-edit="${esc(b.id)}" title="Редактировать" aria-label="Редактировать лампу ${esc(b.name)}">✏️</button>
              <button class="btn sm danger" type="button" data-party-del="${esc(b.id)}" title="Удалить лампу" aria-label="Удалить лампу ${esc(b.name)}">🗑️</button>
            </div>
          </div>
        `;
      });
      setListHtml(listEl, html);
    } else {
      setListHtml(listEl, '<div class="empty" style="text-align:center; padding:16px;">Лампы пока не привязаны.<br>Нажмите <b>«➕ Добавить лампу»</b> или <b>«🔍 Поиск в Wi-Fi»</b>, чтобы подключить умные лампы Tuya/Smart Life к колонке.</div>');
    }
  }

  const bulbsCountBadge = $('#party-bulbs-count');
  if (bulbsCountBadge) {
    if (bulbs.length > 0) {
      bulbsCountBadge.textContent = `${onlineCount} из ${bulbs.length} в сети`;
      bulbsCountBadge.className = 'badge' + (onlineCount === bulbs.length ? ' ok' : ' warn');
    } else {
      bulbsCountBadge.textContent = 'нет данных';
      bulbsCountBadge.className = 'badge';
    }
  }

  // Live system state
  const statSrc = $('#party-stat-src');
  if (statSrc) {
    if (d.yandex_playing) {
      statSrc.innerHTML = '<span class="dot on"></span><span>Яндекс Музыка</span>';
    } else if (d.bt_playing) {
      statSrc.innerHTML = '<span class="dot on"></span><span>Bluetooth ТВ</span>';
    } else {
      statSrc.innerHTML = '<span class="dot"></span><span class="meta">Тишина</span>';
    }
  }

  const statStrobe = $('#party-stat-strobe');
  if (statStrobe) {
    if (d.strobe_active && !d.in_silence) {
      statStrobe.innerHTML = '<span class="dot on"></span><span style="color:var(--ok)">Активен ⚡</span>';
    } else {
      statStrobe.innerHTML = '<span class="dot"></span><span class="meta">Покой / Белый</span>';
    }
  }

  const statWorkers = $('#party-stat-workers');
  if (statWorkers) {
    statWorkers.textContent = `${d.workers_count || bulbs.length} ламп + лента`;
  }

  const strip = d.strip || {};
  const statStrip = $('#party-stat-strip');
  if (statStrip) {
    if (strip.enabled) {
      statStrip.innerHTML = '<span class="dot on"></span><span>Активна</span>';
      statStrip.title = strip.device_id ? `Устройство: ${strip.device_id}` : '';
    } else if (strip.configured) {
      statStrip.innerHTML = '<span class="dot"></span><span class="meta">Настроена, выкл.</span>';
      statStrip.title = strip.device_id ? `Устройство: ${strip.device_id}` : '';
    } else {
      statStrip.innerHTML = '<span class="dot"></span><span class="meta">Не настроена</span>';
      statStrip.title = '';
    }
  }

  const audio = d.audio || {};
  const statAudio = $('#party-stat-audio');
  if (statAudio) {
    if (audio.capturing) {
      statAudio.innerHTML = `<span class="dot on"></span><span>${audio.media_playing ? 'Захват · звук идёт' : 'Захват активен'}</span>`;
      const beat = Number(audio.last_beat_sec);
      statAudio.title = Number.isFinite(beat) ? `Последний бит: ${beat.toFixed(1)} с назад` : '';
    } else {
      statAudio.innerHTML = '<span class="dot"></span><span class="meta">Нет захвата</span>';
      statAudio.title = '';
    }
  }

  const statError = $('#party-stat-error');
  if (statError) {
    const lastError = String(strip.last_error || audio.error || d.last_error || '').trim();
    statError.innerHTML = lastError
      ? `<span style="color:var(--danger); font-weight:600;">${esc(lastError)}</span>`
      : '<span class="meta">—</span>';
    statError.title = lastError;
  }
}

function initPartyEvents() {
  if (partyEventsBound) return;
  const tab = $('#tab-party');
  if (!tab) return;
  partyEventsBound = true;

  // Mode Selection Clicks
  const modesGrid = $('#party-modes-grid');
  if (modesGrid) {
    modesGrid.onclick = e => {
      const btn = e.target.closest('button[data-mode]');
      if (!btn) return;
      const mode = btn.dataset.mode;
      $$('#party-modes-grid button').forEach(b => b.classList.toggle('active', b === btn));
      const activeBadge = $('#party-active-mode-badge');
      if (activeBadge) {
        activeBadge.textContent = btn.querySelector('b')?.textContent?.trim() || mode;
      }
      act(btn, async () => {
        await api('/api/party/settings', { active_mode: mode, master_enabled: true });
        await loadParty();
        toast(`Режим: ${btn.querySelector('b')?.textContent || mode}`);
      });
    };
  }

  // Speed Slider
  const slSpeed = $('#party-slider-speed');
  const slSpeedVal = $('#party-slider-speed-val');
  if (slSpeed) {
    slSpeed.oninput = () => {
      if (slSpeedVal) slSpeedVal.textContent = Number(slSpeed.value).toFixed(1) + 'x';
    };
    slSpeed.onchange = async () => {
      await api('/api/party/settings', { speed: parseFloat(slSpeed.value) });
      await loadParty();
    };
  }

  // Brightness Slider
  const slBright = $('#party-slider-brightness');
  const slBrightVal = $('#party-slider-brightness-val');
  if (slBright) {
    slBright.oninput = () => {
      if (slBrightVal) slBrightVal.textContent = slBright.value + '%';
    };
    slBright.onchange = async () => {
      await api('/api/party/settings', { brightness: parseInt(slBright.value, 10) });
      await loadParty();
    };
  }

  const btnService = $('#party-toggle-service');
  if (btnService) {
    btnService.onclick = e => {
      const isRunning = partyData && partyData.running;
      const action = isRunning ? 'stop' : 'start';
      return act(e.currentTarget, async () => {
        await api('/api/party/service', { action });
        await loadParty();
      }, isRunning ? 'Служба светомузыки остановлена' : 'Служба светомузыки запущена');
    };
  }

  const masterSw = $('#party-master-toggle');
  if (masterSw) {
    masterSw.onchange = e => {
      const enabled = e.target.checked;
      act(e.target, async () => {
        await api('/api/party/settings', { master_enabled: enabled });
        await loadParty();
      });
    };
  }

  const btModeGroup = $('#party-bt-mode');
  if (btModeGroup) {
    btModeGroup.onclick = e => {
      const btn = e.target.closest('button[data-v]');
      if (!btn) return;
      const mode = btn.dataset.v;
      act(btn, async () => {
        await api('/api/party/settings', { bt_mode: mode });
        await loadParty();
      });
    };
  }

  const colGroup = $('#party-col-interval');
  if (colGroup) {
    colGroup.onclick = e => {
      const btn = e.target.closest('button[data-v]');
      if (!btn) return;
      const val = parseInt(btn.dataset.v, 10);
      act(btn, async () => {
        await api('/api/party/settings', { color_interval: val });
        await loadParty();
      });
    };
  }

  const syncGroup = $('#party-sync-mode');
  if (syncGroup) {
    syncGroup.onclick = e => {
      const btn = e.target.closest('button[data-v]');
      if (!btn) return;
      const mode = btn.dataset.v;
      act(btn, async () => {
        await api('/api/party/settings', { sync_mode: mode });
        await loadParty();
      });
    };
  }

  // --- Кинотеатр 4D ---
  const cinToggle = $('#party-cinema-toggle');
  if (cinToggle) {
    cinToggle.onclick = e => {
      const isPlaying = !!(partyData && partyData.cinema && partyData.cinema.playing);
      return act(e.currentTarget, async () => {
        await api('/api/party/cinema/control', { action: isPlaying ? 'pause' : 'play' });
        await loadParty();
      }, isPlaying ? 'Кинотеатр: пауза' : 'Кинотеатр: воспроизведение');
    };
  }

  const cinSync = $('#party-cinema-sync');
  if (cinSync) {
    cinSync.onclick = e => {
      return act(e.currentTarget, async () => {
        await api('/api/party/cinema/control', { action: 'sync', value: 32 });
        await loadParty();
      }, 'Синхронизация с cue 32 с');
    };
  }

  const cinBack = $('#party-cinema-back');
  if (cinBack) {
    cinBack.onclick = e => {
      return act(e.currentTarget, async () => {
        await api('/api/party/cinema/control', { action: 'offset', value: -1 });
        await loadParty();
      }, 'Смещение: −1 с');
    };
  }

  const cinFwd = $('#party-cinema-fwd');
  if (cinFwd) {
    cinFwd.onclick = e => {
      return act(e.currentTarget, async () => {
        await api('/api/party/cinema/control', { action: 'offset', value: 1 });
        await loadParty();
      }, 'Смещение: +1 с');
    };
  }

  const cinSeek = $('#party-cinema-seek');
  if (cinSeek) {
    const markSeeking = () => { partyCinemaSeeking = true; };
    const releaseSeeking = () => { partyCinemaSeeking = false; };
    cinSeek.addEventListener('pointerdown', markSeeking);
    cinSeek.addEventListener('pointerup', releaseSeeking);
    cinSeek.addEventListener('pointercancel', releaseSeeking);
    cinSeek.addEventListener('blur', releaseSeeking);
    cinSeek.oninput = () => {
      partyCinemaSeeking = true;
      const t = $('#party-cinema-time');
      if (t) t.textContent = `${fmtCinemaTime(cinSeek.value)} / ${fmtCinemaTime(cinSeek.max)}`;
    };
    cinSeek.onchange = e => {
      const val = Number(cinSeek.value) || 0;
      const wasPlaying = !!(partyData && partyData.cinema && partyData.cinema.playing);
      partyCinemaSeeking = false;
      act(e.target, async () => {
        await api('/api/party/cinema/control', { action: 'seek', value: val });
        if (!wasPlaying) await api('/api/party/cinema/control', { action: 'pause' });
        await loadParty();
      });
    };
  }

  const cinSpells = $('#party-cinema-spells');
  if (cinSpells) {
    cinSpells.onclick = e => {
      const btn = e.target.closest('button[data-spell]');
      if (!btn) return;
      return act(btn, async () => {
        await api('/api/party/cinema/spell', { spell: btn.dataset.spell, direction: null });
        await loadParty();
      }, `Спелл: ${btn.textContent.trim()}`);
    };
  }

  const reconnectBtn = $('#party-reconnect-btn');
  if (reconnectBtn) {
    reconnectBtn.onclick = e => {
      return act(e.currentTarget, async () => {
        await api('/api/party/reconnect_all', {});
        await new Promise(r => setTimeout(r, 1200));
        await loadParty();
      }, 'Лампы переподключены');
    };
  }

  // --- Bulb pairing & management handlers ---
  const bulbsList = $('#party-bulbs-list');
  if (bulbsList) {
    bulbsList.onclick = async e => {
      const btnTest = e.target.closest('[data-party-test]');
      if (btnTest) {
        const id = btnTest.dataset.partyTest;
        act(btnTest, async () => {
          await api('/api/party/bulbs/test', { id, action: 'flash' });
        }, 'Проверка лампы: тройная вспышка');
        return;
      }
      const btnDel = e.target.closest('[data-party-del]');
      if (btnDel) {
        const id = btnDel.dataset.partyDel;
        if (!confirm('Удалить эту лампу из светомузыки?')) return;
        act(btnDel, async () => {
          await api('/api/party/bulbs/delete', { id });
          await loadParty();
        }, 'Лампа удалена');
        return;
      }
      const btnEdit = e.target.closest('[data-party-edit]');
      if (btnEdit) {
        const id = btnEdit.dataset.partyEdit;
        let b = (Array.isArray(partyBulbsConfig) ? partyBulbsConfig : []).find(x => x.id === id);
        if (!b && partyData && Array.isArray(partyData.bulbs)) {
          b = partyData.bulbs.find(x => x.id === id);
        }
        if (!b) return;
        $('#party-bulb-ed-title').textContent = '✏️ Редактирование лампы';
        $('#party-bulb-edit-mode').value = 'edit';
        $('#party-bulb-name').value = b.name || '';
        $('#party-bulb-ip').value = b.ip || '';
        $('#party-bulb-id').value = b.id || '';
        $('#party-bulb-key').value = b.key || '';
        $('#party-bulb-role').value = b.role || 'kick';
        $('#party-bulb-side').value = b.side || '';
        $('#party-bulb-ver').value = String(b.ver || 3.5);
        $('#party-bulb-editor').hidden = false;
        $('#party-bulb-editor').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        return;
      }
    };
  }

  // Add Bulb Button
  const btnAddBulb = $('#party-bulb-add-btn');
  if (btnAddBulb) {
    btnAddBulb.onclick = () => {
      $('#party-bulb-ed-title').textContent = '➕ Привязать умную лампу Tuya';
      $('#party-bulb-edit-mode').value = 'add';
      $('#party-bulb-name').value = '';
      $('#party-bulb-ip').value = '';
      $('#party-bulb-id').value = '';
      $('#party-bulb-key').value = '';
      $('#party-bulb-role').value = 'kick';
      $('#party-bulb-side').value = '';
      $('#party-bulb-ver').value = '3.5';
      $('#party-bulb-editor').hidden = false;
      $('#party-bulb-editor').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    };
  }

  const cancelBulbEdit = () => { $('#party-bulb-editor').hidden = true; };
  if ($('#party-bulb-cancel-btn')) $('#party-bulb-cancel-btn').onclick = cancelBulbEdit;
  if ($('#party-bulb-cancel-x')) $('#party-bulb-cancel-x').onclick = cancelBulbEdit;

  // Save Bulb
  const btnSaveBulb = $('#party-bulb-save-btn');
  if (btnSaveBulb) {
    btnSaveBulb.onclick = () => {
      const b_id = $('#party-bulb-id').value.trim();
      const ip = $('#party-bulb-ip').value.trim();
      const key = $('#party-bulb-key').value.trim();
      const name = $('#party-bulb-name').value.trim() || `Лампа ${ip || b_id.slice(0, 6)}`;
      const role = $('#party-bulb-role').value;
      const side = $('#party-bulb-side').value;
      const ver = parseFloat($('#party-bulb-ver').value) || 3.5;

      if (!b_id) return toast('Укажите ID устройства (Device ID)', true);
      if (!ip) return toast('Укажите IP-адрес лампы в Wi-Fi', true);
      if (!key) return toast('Укажите локальный ключ (Local Key)', true);

      const pos_x = side === 'left' ? -1.0 : (side === 'right' ? 1.0 : 0.0);

      act(btnSaveBulb, async () => {
        await api('/api/party/bulbs/add', { id: b_id, name, ip, key, role, side, pos_x, ver, enabled: true });
        $('#party-bulb-editor').hidden = true;
        await loadParty();
      }, `Лампа «${name}» успешно сохранена`);
    };
  }

  // Wi-Fi Scan Panel
  const btnScanBulbs = $('#party-bulbs-scan-btn');
  if (btnScanBulbs) {
    btnScanBulbs.onclick = () => {
      const p = $('#party-scan-panel');
      if (p) {
        p.hidden = !p.hidden;
        if (!p.hidden) p.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    };
  }
  if ($('#party-scan-close-btn')) {
    $('#party-scan-close-btn').onclick = () => { $('#party-scan-panel').hidden = true; };
  }

  const btnScanStart = $('#party-scan-start-btn');
  if (btnScanStart) {
    btnScanStart.onclick = () => {
      const stat = $('#party-scan-status');
      const resContainer = $('#party-scan-results');
      if (stat) stat.textContent = 'Идёт поиск ламп Tuya в сети...';
      if (resContainer) resContainer.innerHTML = '<div class="meta" style="padding:10px 0;">Слушаем UDP анонсы на портах 6666/6667...</div>';
      act(btnScanStart, async () => {
        const found = await api('/api/party/bulbs/scan?timeout=3');
        if (stat) stat.textContent = `Найдено устройств: ${found.length}`;
        if (resContainer) {
          if (!found.length) {
            resContainer.innerHTML = '<div class="empty">Устройства Tuya не ответили за 3 сек. Убедитесь, что лампы включены в розетку и подключены к тому же роутеру Wi-Fi.</div>';
          } else {
            resContainer.innerHTML = found.map(dev => `
              <div style="display:flex; align-items:center; justify-content:space-between; padding:10px 14px; background:var(--card); border:1px solid var(--border); border-radius:10px; gap:8px; flex-wrap:wrap;">
                <div>
                  <div style="font-weight:600; font-size:14px;">${esc(dev.name || 'Умная лампа Tuya')}</div>
                  <div class="meta" style="font-size:12px; margin-top:2px;">IP: ${esc(dev.ip)} • ID: <span class="mono">${esc(dev.id)}</span> • v${esc(dev.ver)}</div>
                </div>
                <div>
                  ${dev.is_paired ? '<span class="badge ok">Уже привязана</span>' : `<button class="btn sm primary" type="button" data-party-pair-ip="${esc(dev.ip)}" data-party-pair-id="${esc(dev.id)}" data-party-pair-ver="${esc(dev.ver)}">➕ Привязать</button>`}
                </div>
              </div>
            `).join('');
          }
        }
      });
    };
  }

  // Pre-fill from scan results
  const scanResults = $('#party-scan-results');
  if (scanResults) {
    scanResults.onclick = e => {
      const pairBtn = e.target.closest('[data-party-pair-id]');
      if (!pairBtn) return;
      const ip = pairBtn.dataset.partyPairIp || '';
      const id = pairBtn.dataset.partyPairId || '';
      const ver = pairBtn.dataset.partyPairVer || '3.5';
      $('#party-bulb-ed-title').textContent = '➕ Привязать найденную лампу';
      $('#party-bulb-edit-mode').value = 'add';
      $('#party-bulb-name').value = `Лампа ${ip}`;
      $('#party-bulb-ip').value = ip;
      $('#party-bulb-id').value = id;
      $('#party-bulb-ver').value = ver;
      $('#party-bulb-key').value = '';
      $('#party-bulb-role').value = 'kick';
      $('#party-bulb-side').value = '';
      $('#party-bulb-editor').hidden = false;
      $('#party-bulb-key').focus();
      $('#party-bulb-editor').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      toast('Параметры лампы подставлены. Введите Local Key для завершения');
    };
  }

  // Import JSON Panel
  const btnImportBulbs = $('#party-bulbs-import-btn');
  if (btnImportBulbs) {
    btnImportBulbs.onclick = () => {
      const p = $('#party-import-panel');
      if (p) {
        p.hidden = !p.hidden;
        if (!p.hidden) p.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    };
  }
  if ($('#party-import-close-btn')) {
    $('#party-import-close-btn').onclick = () => { $('#party-import-panel').hidden = true; };
  }
  if ($('#party-import-cancel-btn')) {
    $('#party-import-cancel-btn').onclick = () => { $('#party-import-panel').hidden = true; };
  }

  const btnImportApply = $('#party-import-apply-btn');
  if (btnImportApply) {
    btnImportApply.onclick = () => {
      const text = ($('#party-import-text')?.value || '').trim();
      if (!text) return toast('Вставьте JSON список ламп', true);
      let parsed;
      try {
        parsed = JSON.parse(text);
      } catch (e) {
        return toast('Ошибка синтаксиса JSON: ' + e.message, true);
      }
      act(btnImportApply, async () => {
        const r = await api('/api/party/bulbs/import', parsed);
        $('#party-import-panel').hidden = true;
        await loadParty();
        toast(`Импортировано ламп: ${r.count || 0}`);
      });
    };
  }

  const btnImportExport = $('#party-import-export-btn');
  if (btnImportExport) {
    btnImportExport.onclick = () => {
      act(btnImportExport, async () => {
        const list = await api('/api/party/bulbs');
        const txt = $('#party-import-text');
        if (txt) {
          txt.value = JSON.stringify(list, null, 2);
          toast('Текущий список ламп выгружен в окно');
        }
      });
    };
  }

  const restoreWhiteBtn = $('#party-restore-white-btn');
  if (restoreWhiteBtn) {
    restoreWhiteBtn.onclick = e => {
      return act(e.currentTarget, async () => {
        await api('/api/party/restore_white', {});
        await loadParty();
      }, 'Включён тёплый свет 3000K');
    };
  }
}

document.addEventListener('DOMContentLoaded', initPartyEvents);


// ---- эквалайзер Bluetooth-выхода ----
const EQ_NAMES = { flat: 'Без изменений', rock: 'Рок', electronic: 'Электроника', vocal: 'Вокал / подкаст', night: 'Ночной', custom: 'Свой' };
const EQ_DEFAULT = [60, 230, 910, 3600, 14000];
const fmtDb = g => (g > 0 ? '+' : '') + g + ' дБ';
async function loadBtEq() {
  try { btEq = await api('/api/btout/eq'); renderBtEq(); } catch { btEq = null; }
}
function buildEqCustom() {
  const box = $('#bt-eq-custom');
  if (!box || box.children.length) return;
  box.innerHTML = EQ_DEFAULT.map((f, i) => `<div class="eq-band">
      <input type="number" class="field eq-f" min="20" max="20000" step="10" value="${f}" aria-label="Частота ${i + 1}, Гц">
      <span class="meta">Гц</span>
      <input type="range" class="eq-g" min="-12" max="12" step="1" value="0" aria-label="Усиление ${i + 1}, дБ">
      <span class="mono eq-gv">0 дБ</span></div>`).join('')
    + '<p class="hint">Перекрывающиеся полосы складываются. Для более заметного эффекта попробуйте также убавить соседние частоты.</p>';
  box.addEventListener('input', e => {
    const band = e.target.closest('.eq-band');
    if (!band) return;
    band.querySelector('.eq-gv').textContent = fmtDb(+band.querySelector('.eq-g').value);
    scheduleEqCustom();
  });
  box.addEventListener('change', e => {
    if (e.target.matches('.eq-f')) { clampEqFreq(e.target); scheduleEqCustom(); }
  });
}
function clampEqFreq(inp) {
  inp.value = Math.max(20, Math.min(20000, Math.round(+inp.value) || 60));
}
function eqCustomBands() {
  return $$('#bt-eq-custom .eq-band').map(b => ({
    f: Math.max(20, Math.min(20000, Math.round(+b.querySelector('.eq-f').value) || 60)),
    g: Math.max(-12, Math.min(12, Math.round(+b.querySelector('.eq-g').value) || 0)) }));
}
function fillEqCustom(bands) {
  buildEqCustom();
  const rows = $$('#bt-eq-custom .eq-band');
  (bands || []).slice(0, rows.length).forEach((b, i) => {
    const g = Math.round(b.g);
    rows[i].querySelector('.eq-f').value = Math.round(b.f);
    rows[i].querySelector('.eq-g').value = g;
    rows[i].querySelector('.eq-gv').textContent = fmtDb(g);
  });
}
let btEqTimer = 0;
function scheduleEqCustom() {
  clearTimeout(btEqTimer);
  btEqTimer = setTimeout(async () => {
    try {
      btEq = await api('/api/btout/eq', { preset: 'custom', bands: eqCustomBands() });
      drawEqCurve();
    } catch (e) { toast(e.message, true); }
  }, 250);
}
function renderBtEq() {
  if (!btEq) return;
  const sel = $('#bt-eq');
  const names = btEq.presets || Object.keys(EQ_NAMES);
  if (sel.options.length !== names.length)
    sel.innerHTML = names.map(v => `<option value="${v}">${EQ_NAMES[v] || v}</option>`).join('');
  sel.value = btEq.preset;
  $('#bt-eq-custom').hidden = btEq.preset !== 'custom';
  if (btEq.preset === 'custom') fillEqCustom(btEq.custom);
  drawEqCurve();
}
function drawEqCurve() {
  const cv = $('#bt-eq-curve');
  if (!cv || !btEq) return;
  const ctx = cv.getContext('2d'), w = cv.width, h = cv.height;
  const padL = 28, padR = 6, padT = 6, padB = 15;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  ctx.clearRect(0, 0, w, h);
  const css = getComputedStyle(document.documentElement);
  const grid = css.getPropertyValue('--line-2').trim() || '#ddd';
  const muted = css.getPropertyValue('--muted').trim() || '#888';
  const accent = css.getPropertyValue('--accent').trim() || '#e8590c';
  const X = f => padL + Math.log10(f / 20) / Math.log10(1000) * plotW;
  const Y = d => padT + (1 - (d + 15) / 30) * plotH;
  const marks = [[20, '20'], [50, '50'], [100, '100'], [200, '200'], [500, '500'], [1000, '1k'],
                 [2000, '2k'], [5000, '5k'], [10000, '10k'], [20000, '20k']];
  ctx.lineWidth = 1; ctx.strokeStyle = grid;
  marks.forEach(([f]) => { ctx.beginPath(); ctx.moveTo(X(f), padT); ctx.lineTo(X(f), padT + plotH); ctx.stroke(); });
  ctx.beginPath(); ctx.moveTo(padL, Y(0)); ctx.lineTo(w - padR, Y(0)); ctx.stroke();
  ctx.fillStyle = muted; ctx.font = '9px ui-monospace, monospace'; ctx.textBaseline = 'top';
  marks.forEach(([f, label]) => {
    const x = X(f);
    ctx.textAlign = x < padL + 10 ? 'left' : x > w - padR - 10 ? 'right' : 'center';
    ctx.fillText(label, x, padT + plotH + 3);
  });
  ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  for (const d of [12, 0, -12]) ctx.fillText((d > 0 ? '+' : '') + d, padL - 4, Y(d));
  ctx.strokeStyle = accent; ctx.lineWidth = 2; ctx.beginPath();
  (btEq.freqs || []).forEach((f, i) => { const x = X(f), y = Y(btEq.db[i]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.stroke();
}
$('#bt-eq').onchange = e => {
  const preset = e.target.value, name = EQ_NAMES[preset] || preset;
  $('#bt-eq-custom').hidden = preset !== 'custom';
  if (preset === 'custom') fillEqCustom(btEq?.custom);
  act(e.target, async () => {
    btEq = await api('/api/btout/eq', { preset, bands: preset === 'custom' ? eqCustomBands() : undefined });
    if (btEq.preset === 'custom') fillEqCustom(btEq.custom);
    drawEqCurve();
  }, 'Эквалайзер: ' + name);
};
// «Выбрать колонку» / «Другая колонка»: ищем рядом и показываем список
function btOutScan(b) {
  const pick = $('#bt-out-pick'); pick.hidden = false;
  $('#bt-out-list').innerHTML = '<div class="empty">Ищу устройства рядом… ~15 с</div>';
  act(b, async () => {
    const r = await api('/api/btout/scan', { seconds: 12 });
    $('#bt-out-list').innerHTML = r.devices.length ? r.devices.map(x => `<div class="bt-row" data-mac="${esc(x.mac)}">
        <div class="bt-ic">${icon(BT_ICONS[x.icon] || 'i-bt', 18)}</div>
        <div class="info"><b>${esc(x.name || 'Без имени')}</b><span class="meta">${[x.audio ? 'колонка или наушники' : 'другое устройство', x.paired ? 'сопряжено' : '', x.rssi != null ? `сигнал ${x.rssi} dBm` : ''].filter(Boolean).join(' · ')}</span></div>
        <div class="actions"><button class="btn sm${x.audio ? ' primary' : ''}">Играть через неё</button></div></div>`).join('')
      : '<div class="empty">Ничего не нашлось — колонка в режиме сопряжения? Попробуйте ещё раз</div>';
  });
}
function btOutUse(b, mac) {
  // первый раз — честно предупреждаем, что меняется и как вернуть
  if (!store.get('btout.ok') && !confirm('Чтобы звук шёл в Bluetooth-колонку, панель:\n'
      + '• подменит настройку звука /etc/asound.conf своей копией (системный раздел не меняется, подмена накрывает его поверх);\n'
      + '• загрузит модуль ядра snd-aloop (виртуальная петля для смешивания звука);\n'
      + '• оставит на колонке сторожа, который держит это, пока Bluetooth-колонка подключена, и запускается после перезагрузки.\n\n'
      + 'Алиса перезапустится (~30 с), играющая музыка остановится — включите её снова. Вернуть всё как было — кнопкой «Вернуть звук в колонку» здесь же '
      + '(или в «Настройках» → «Своё на колонке»). Продолжить?')) return;
  else if (store.get('btout.ok') && mu.info?.glagol?.state?.playing
           && !confirm('Перевести звук в Bluetooth-колонку? Алиса перезапустится (~30 с), музыка остановится — включите её снова.')) return;
  store.set('btout.ok', '1');
  toast('Подключаю колонку — Алиса перезапустится и заговорит из неё');
  const label = b?.textContent;
  if (b) b.textContent = 'Подключение и настройка…';
  return act(b, async () => { btData = await api('/api/btout/use', { mac }); renderBt(); if (btData.eq_error) toast('Звук подключён, но эквалайзер не применён: ' + btData.eq_error, true); else toast('Звук — через Bluetooth-колонку'); if (btData.restarted) aliceRestarting('Звук переходит на Bluetooth-колонку — Алиса перезапускается, это около 30 секунд'); }).finally(() => { if (b) b.textContent = label; });
}
$('#bt-out-btn').onclick = e => {
  const b = e.currentTarget, o = btData?.out || {};
  if (o.on) {
    if (o.state === 'bt' && !confirm('Вернуть звук в саму колонку? Алиса перезапустится (~30 с), играющая музыка остановится — включите её снова.')) return;
    return act(b, async () => { btData = await api('/api/btout/off', {}); renderBt(); if (btData.restarted) aliceRestarting('Звук возвращается во встроенный динамик — Алиса перезапускается, это около 30 секунд'); }, 'Звук — во встроенный динамик');
  }
  if (o.last) return btOutUse(b, o.last.mac);
  btOutScan(b);
};
$('#bt-out-other').onclick = e => btOutScan(e.currentTarget);
$('#bt-out-list').onclick = e => {
  const b = e.target.closest('button'); if (b) btOutUse(b, b.closest('.bt-row').dataset.mac);
};
$('#bt-pair-btn').onclick = e => btDo(e.currentTarget, { action: btData?.discoverable ? 'pair_off' : 'pair_on' },
  btData?.discoverable ? 'Колонка скрыта' : 'Колонка видна 3 минуты');
$('#bt-list').onclick = e => {
  const b = e.target.closest('[data-bta]'); if (!b) return;
  const row = b.closest('.bt-row'), dev = btData.devices.find(x => x.mac === row.dataset.mac), a = b.dataset.bta;
  if (a === 'remove' && !confirm(`Забыть «${dev?.name || dev?.mac}»? Чтобы подключиться снова, понадобится новое сопряжение.`)) return;
  if (a === 'connect') toast('Подключаемся… устройство должно быть рядом и с включённым Bluetooth');
  btDo(b, { action: a, mac: row.dataset.mac }, { connect: 'Подключено', disconnect: 'Отключено', remove: 'Устройство забыто' }[a]);
};
loadBt();
  loadDSP();

// ---------- система ----------
$('#q-restart').onclick = e => confirm('Перезапустить Quasar? Алиса будет недоступна ~30 с.') &&
  act(e.currentTarget, async () => { await api('/api/system', { action: 'quasar_restart' }); aliceRestarting(); }, 'Quasar перезапускается…');
$('#reboot').onclick = e => confirm('Перезагрузить колонку?') &&
  act(e.currentTarget, () => api('/api/system', { action: 'reboot' }), 'Перезагрузка…');

// ---------- логи ----------
let logText = '';
// В логах Quasar — Device ID, номер аккаунта, сеть: для скриншотов прячем всё, что известно о колонке и аккаунте.
function maskLog(t) {
  const st = status || {}, serial = /[-_]([A-Za-z0-9]{4,})$/.exec(st.hostname || '')?.[1];
  // номер аккаунта Яндекса встречается в разном виде (uid: …, id=…, Account …) — находим и прячем везде
  const uids = new Set([...t.matchAll(/(?:passport_uid|puid|uid)"?\s*[:=]\s*"?(\d{5,})/gi)].map(m => m[1]));
  for (const v of [st.device_id, st.ssid, st.address, ymLogin, serial, ...uids].filter(v => v && v.length >= 4))
    t = t.split(v).join('••••');
  return t.replace(/\b(?:y0_|AQAAAA)[\w-]{10,}/g, '••••')                           // OAuth-токены
    .replace(/(token["']?\s*[:=]\s*["']?)[\w.-]{20,}/gi, '$1••••')
    .replace(/(\b[Aa]ccount[ :=]+)\d{5,}/g, '$1••••')
    .replace(/("wifi_hash"\s*:\s*")[0-9a-f]+/g, '$1••••')
    // MAC роутера (BSSID) находится в открытых базах точек Wi‑Fi — по нему можно узнать адрес
    .replace(/\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b/gi, '••:••:••:••:••:••');
}
function renderLog() {
  const f = $('#log-filter').value.toLowerCase();
  const out = $('#log-out');
  const atBottom = out.scrollTop + out.clientHeight >= out.scrollHeight - 20;
  const text = $('#log-mask').checked ? maskLog(logText) : logText;
  out.textContent = f ? text.split('\n').filter(l => l.toLowerCase().includes(f)).join('\n') : text;
  if (atBottom) out.scrollTop = out.scrollHeight;
}
async function loadLog(btn) {
  const r = await act(btn, () => api(`/api/log?name=${encodeURIComponent($('#log-name').value)}&lines=${$('#log-lines').value}`));
  if (r) { logText = r.text; renderLog(); }
}
$('#log-load').onclick = e => loadLog(e.currentTarget);
// Отчёт для Issue — и в «Настройках», и на экране подключения (там он нужнее всего)
// Буфер обмена браузер даёт только сразу после нажатия, а отчёт собирается на сервере: поэтому запись начинаем
// в том же нажатии с обещанием текста (ClipboardItem), а если не вышло — показываем отчёт в поле, уже выделенным.
$$('[data-report]').forEach(b => b.onclick = e => act(e.currentTarget, async () => {
  const box = $('#' + b.dataset.report);
  const text = api('/api/report').then(r => r.text);
  let copied = false;
  if (navigator.clipboard?.write && window.ClipboardItem && isSecureContext) {
    copied = await navigator.clipboard.write([new ClipboardItem({ 'text/plain': text.then(t => new Blob([t], { type: 'text/plain' })) })])
      .then(() => true, () => false);
  }
  const t = await text;
  if (!copied) copied = await copyText(t);
  box.value = t; box.hidden = false;
  box.style.height = Math.min(420, box.scrollHeight + 4) + 'px';
  if (!copied) { box.focus(); box.select(); }
  toast(copied ? 'Отчёт скопирован — вставьте его в новое Issue и проверьте перед отправкой'
    : 'Браузер не дал скопировать — отчёт ниже уже выделен, нажмите Ctrl+C (⌘C)', !copied);
}));
$('#log-name').onchange = () => { $('#log-out').scrollTop = 1e9; loadLog(); };
$('#log-lines').onchange = () => loadLog();
$('#log-filter').oninput = renderLog;
$('#log-mask').onchange = renderLog;
every(3000, () => $('#log-follow').checked && !document.hidden && !$('#tab-logs').hidden && loadLog());

// ---------- консоль ----------
// xterm.js ↔ WebSocket /api/term ↔ adb shell с псевдотерминалом на сервере. Сеанс живёт, пока открыта страница.
let term = null, termFit = null, termWs = null;
// Буфер обмена: navigator.clipboard есть только на HTTPS и localhost, иначе — старый execCommand через textarea.
// Сохранить файл из JS. В приложении для Android WebView не скачивает blob: — отдаём файл приложению.
async function saveBlob(blob, name) {
  if (window.AndroidPanel) {
    const b64 = await new Promise((ok, fail) => { const r = new FileReader(); r.onload = () => ok(String(r.result).split(',')[1] || ''); r.onerror = fail; r.readAsDataURL(blob); });
    if (!AndroidPanel.save(name, blob.type || 'application/octet-stream', b64)) throw new Error('не удалось сохранить файл');
    return;
  }
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
async function copyText(text) {
  try { if (navigator.clipboard && isSecureContext) { await navigator.clipboard.writeText(text); return true; } } catch {}
  const ta = document.createElement('textarea');
  ta.value = text; ta.setAttribute('readonly', ''); ta.style.cssText = 'position:fixed;left:-9999px;top:0';
  document.body.append(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch {}
  ta.remove();
  term?.focus();
  return ok;
}
function termState(text, ok) { $('#term-state').textContent = text; $('#term-state').className = 'badge' + (ok ? ' ok' : ''); }
function termInit() {
  if (term) return true;
  if (!window.Terminal||!window.FitAddon) { $('#term').textContent=document.readyState==='loading'?'Загрузка терминала…':'Не загрузился xterm.js'; return false; }
  $('#term').textContent='';
  term = new Terminal({
    fontFamily: "'JetBrains Mono', ui-monospace, Menlo, Consolas, monospace", fontSize: 13, lineHeight: 1.2,
    cursorBlink: true, scrollback: 5000, allowProposedApi: false,
    theme: { background: '#111116', foreground: '#E6E6EB', cursor: '#FF9A5C', selectionBackground: '#E8590C66',
             black: '#16161D', brightBlack: '#5F5F6E' },
  });
  termFit = new FitAddon.FitAddon();
  term.loadAddon(termFit);
  term.open($('#term'));
  // Копирование как в обычных терминалах: выделил мышью — уже в буфере; Ctrl+C при выделении — копировать
  // (без выделения — прервать команду), Ctrl+Shift+C — копировать.
  const copySel = () => {
    const text = term.getSelection();
    if (!text) return false;
    copyText(text).then(ok => ok && toast('Скопировано'));
    return true;
  };
  $('#term').addEventListener('mouseup', () => setTimeout(copySel, 0));
  term.attachCustomKeyEventHandler(e => {
    if (e.type !== 'keydown' || !e.ctrlKey || e.altKey || e.code !== 'KeyC') return true;
    if (e.shiftKey || term.hasSelection()) { copySel(); term.clearSelection(); e.preventDefault(); return false; }
    return true;
  });
  term.onData(d => {
    d = termMods(d);
    if (termWs?.readyState === 1) termWs.send(JSON.stringify({ t: 'i', d }));
    else if (d === '\r') termConnect();          // сеанс закрыт — Enter открывает новый
  });
  term.onResize(({ cols, rows }) => termWs?.readyState === 1 && termWs.send(JSON.stringify({ t: 'r', c: cols, r: rows })));
  new ResizeObserver(() => { if (shownTab === 'console') try { termFit.fit(); } catch {} }).observe($('#term'));
  return true;
}
function termConnect() {
  if (!termInit() || termWs) return;
  try { termFit.fit(); } catch {}
  termState('подключение…');
  const ws = new WebSocket(PanelContext.websocket(`/api/term?cols=${term.cols}&rows=${term.rows}`));
  ws.binaryType = 'arraybuffer';
  termWs = ws;
  ws.onopen = () => { termState('подключено', true); term.focus(); };
  ws.onmessage = e => term.write(new Uint8Array(e.data));
  ws.onclose = () => {
    if (termWs !== ws) return;
    termWs = null;
    termState('закрыт');
    term.write('\r\n\x1b[90m[сеанс закрыт — Enter, чтобы открыть новый]\x1b[0m\r\n');
  };
}
// Полоса клавиш на телефоне. Кнопки не забирают фокус (pointerdown → preventDefault): клавиатура не прячется.
const termMod = { ctrl: false, alt: false };
function termModSet(m, on) {
  termMod[m] = on;
  $(`#term-keys [data-mod="${m}"]`).setAttribute('aria-pressed', on);
}
// залипшие Ctrl/Alt применяем к следующему вводу с клавиатуры телефона и снимаем
function termMods(d) {
  if (!termMod.ctrl && !termMod.alt) return d;
  if (termMod.ctrl && d.length === 1) {
    const c = d.toLowerCase().charCodeAt(0);
    if (c >= 97 && c <= 122) d = String.fromCharCode(c - 96);          // Ctrl+буква
    else if ('@[\\]^_'.includes(d)) d = String.fromCharCode(d.charCodeAt(0) & 31);
    else if (d === ' ') d = '\x00';
  }
  if (termMod.alt) d = '\x1b' + d;
  termModSet('ctrl', false); termModSet('alt', false);
  return d;
}
function termKey(k) {
  const app = term?.modes?.applicationCursorKeysMode, csi = app ? '\x1bO' : '\x1b[';
  return { esc: '\x1b', tab: '\t', up: csi + 'A', down: csi + 'B', right: csi + 'C', left: csi + 'D',
           home: '\x1b[H', end: '\x1b[F', pgup: '\x1b[5~', pgdn: '\x1b[6~',
           '^C': '\x03', '^D': '\x04', '^Z': '\x1a', '^L': '\x0c' }[k];
}
$('#term-keys').addEventListener('pointerdown', e => { if (e.target.closest('button')) e.preventDefault(); });
$('#term-keys').addEventListener('click', async e => {
  const b = e.target.closest('button');
  if (!b || !term) return;
  if (b.dataset.mod) { termModSet(b.dataset.mod, !termMod[b.dataset.mod]); return term.focus(); }
  let d;
  if (b.dataset.k === 'paste') {
    try { d = await navigator.clipboard.readText(); } catch { return toast('Буфер обмена недоступен — вставьте долгим нажатием', true); }
  } else d = b.dataset.t ?? termKey(b.dataset.k);
  if (!d) return;
  // стрелки и Tab с залипшим Alt — тоже через ESC-префикс; Ctrl к ним не применяем
  if (b.dataset.t || b.dataset.k === 'paste') d = termMods(d);
  else if (termMod.alt) { d = '\x1b' + d; termModSet('alt', false); termModSet('ctrl', false); }
  if (termWs?.readyState === 1) termWs.send(JSON.stringify({ t: 'i', d }));
  else if (d === '\r') termConnect();
  term.focus();
});
// Экранная клавиатура на телефоне: видимая область (visualViewport) заметно ниже обычной — значит, открыта.
// Тогда терминал занимает её целиком, и полоса клавиш оказывается прямо над клавиатурой.
(() => {
  const vv = window.visualViewport;
  if (!vv) return;
  let base = 0, orient = screen.orientation?.type;
  const upd = () => {
    if (screen.orientation?.type !== orient) { orient = screen.orientation?.type; base = 0; }
    base = Math.max(base, vv.height);
    const kb = plMobile() && shownTab === 'console' && base - vv.height > 150;
    const st = document.documentElement.style;
    st.setProperty('--vv-h', vv.height + 'px');
    st.setProperty('--vv-top', vv.offsetTop + 'px');
    if (kb !== document.body.classList.contains('term-kb')) {
      document.body.classList.toggle('term-kb', kb);
      if (kb) requestAnimationFrame(() => { try { termFit?.fit(); term?.scrollToBottom(); } catch {} });
    }
  };
  vv.addEventListener('resize', upd);
  vv.addEventListener('scroll', upd);
  window.addEventListener('hashchange', () => setTimeout(upd));
  setTimeout(upd);   // высота без клавиатуры — сразу (после всего скрипта: shownTab объявлен ниже)
})();
// «Вставить» — только где браузер даёт читать буфер (HTTPS, localhost — в том числе приложение)
$('#term-paste').hidden = !(navigator.clipboard?.readText && isSecureContext);
$('#term-new').onclick = () => {
  if (termWs) { const old = termWs; termWs = null; old.close(); }
  termInit() && term.reset();
  termConnect();
};

// ---------- вкладки ----------
const store = {
  get: k => { try { return localStorage.getItem(PanelContext.storageKey(k)); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(PanelContext.storageKey(k), v); } catch {} },
};
// 'radio' временно убрана из списка вкладок вместе с пунктом меню; код вкладки остаётся
const TABS = ['overview', 'player', 'led', 'alarm', 'scenes', 'assistant', 'wakeword', 'charts', 'boot', 'party', 'apps', 'files', 'logs', 'console', 'settings'];
let bootLoaded = false;
let logsLoaded = false;
let shownTab = null;
// Телефон: «Ещё» открывает лист со всеми разделами. Если открыт раздел не из нижней панели — «Ещё» показывает его.
function mbarMore(tab) {
  const b = $('#mbar-more'), main = $$('.mbar a').some(a => a.dataset.tab === tab);
  const link = main ? null : $(`nav.side a[data-tab="${tab}"]`);
  b.classList.toggle('active', !!link);
  b.querySelector('use').setAttribute('href', link ? link.querySelector('use').getAttribute('href') : '#i-more');
  b.querySelector('span').textContent = link ? link.textContent.trim() : 'Ещё';
}
function navSheet(open) {
  document.body.classList.toggle('nav-open', open);
  $('#mbar-more').setAttribute('aria-expanded', open);
}
$('#mbar-more').onclick = () => navSheet(!document.body.classList.contains('nav-open'));
$('#nav-backdrop').onclick = () => navSheet(false);
document.addEventListener('keydown', e => { if (e.key === 'Escape' && document.body.classList.contains('nav-open')) navSheet(false); });
$('#nav-sheet').addEventListener('click', e => { if (e.target.closest('a')) navSheet(false); });

let prevTab = 'overview', plReturn = null;
function showTab() {
  const h = location.hash.slice(1);
  const tab = TABS.includes(h) ? h : 'overview';
  const from = document.body.dataset.tab;
  // другая вкладка — начинаем её сверху (например, переход в «Плеер» из нижнего плеера); но из плеера на
  // телефоне возвращаемся туда же, где были: плеер лишь «накрывал» вкладку
  let scrollTo = null;
  if (online && shownTab && tab !== shownTab) {
    if (tab === 'player') plReturn = { tab: from, y: scrollY };
    scrollTo = from === 'player' && plReturn?.tab === tab ? plReturn.y : 0;
    window.scrollTo({ top: 0 });
  }
  if (online) shownTab = tab;
  if (from && from !== tab) prevTab = from;
  document.body.dataset.tab = tab;
  if (tab !== 'player') plSheet('');
  mSheetShow(null);
  $$('nav.side a, .mbar a').forEach(a => a.classList.toggle('active', a.dataset.tab === tab));
  mbarMore(tab);
  navSheet(false);
  $('#conn-screen').hidden = online;
  $$('main[data-page]').forEach(m => m.hidden = !online || m.id !== 'tab-' + tab);
  // прокрутку возвращаем, когда вкладка уже показана: пока открыт плеер, страница закреплена и прокручивать некуда
  if (scrollTo) window.scrollTo({ top: scrollTo });
  if (!online) return;
  // телефон: плеер выезжает снизу, а после него вкладка, панель и мини-плеер плавно проявляются
  if (plMobile() && from && from !== tab && (tab === 'player' || from === 'player')) {
    const cls = tab === 'player' ? 'pl-in' : 'from-player';
    document.body.classList.remove('pl-in', 'from-player');
    void document.body.offsetWidth;   // перезапуск анимации
    document.body.classList.add(cls);
    clearTimeout(showTab.anim); showTab.anim = setTimeout(() => document.body.classList.remove(cls), 450);
  }
  if (tab === 'files' && !fs.loaded) fsOpen(store.get('fs.cwd') || '/data/quasar');
  if (tab === 'logs' && !logsLoaded) {
    logsLoaded = true;
    api('/api/logs').then(l => {
      $('#log-name').innerHTML = '<option value="panel">журнал панели</option>'
        + l.map(n => `<option ${n === 'maind.log' ? 'selected' : ''}>${esc(n)}</option>`).join('');
      loadLog();
    }).catch(e => toast(e.message, true));
  }
  if (tab === 'console') setTimeout(() => { termConnect(); try { termFit?.fit(); } catch {} term?.focus(); }, 0);
  if (tab === 'boot' && !bootLoaded) { bootLoaded = true; loadBoot(); }
  if (tab === 'party') loadParty();
  if (tab === 'wakeword') { if (window.wwLoad) window.wwLoad(); if (window.wwActionsLoad) window.wwActionsLoad(); }
  loadHook();
  if (tab === 'apps') { loadYam2(); loadYam(); }
  if (tab === 'alarm') loadAlarms();
  if (tab === 'scenes') loadScenes();
  rdPoll(tab === 'radio');
  asPoll(tab === 'assistant');
  if (tab === 'charts') loadCharts();
  if (tab === 'settings') { loadAccess(); loadAgent(); loadFootprint(); loadPanelVersion().catch(() => {}); loadPanelModel(); }
  if (tab === 'overview') loadBt();
  loadDSP();
  if (tab === 'player') { renderMusic(); loadPlayerExtras(); loadMusic(); }
  updateMinibar();
}
window.addEventListener('hashchange', showTab);

// ---------- музыка (Glagol + API Яндекс Музыки) ----------
// Сервер держит соединение с колонкой; страница раз в секунду берёт последнее состояние, а позицию в треке
// между обновлениями досчитывает сама. Одно и то же состояние рисуется в трёх местах: виджет на «Обзоре»,
// нижний плеер и вкладка «Плеер» — элементы помечены общими классами (.mu-title, .mu-seek…) и data-mu.
const mu = { info: null, seeking: false, volSet: 0, qr: null, qrTimer: null, trackId: null, track: null, extras: {} };
const fmtDur = s => { s = Math.max(0, Math.floor(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
const muState = () => mu.info?.glagol?.state;
const muPlayer = () => muState()?.playerState;
const muHas = () => { const p = muPlayer(); return !!(p && (p.title || p.subtitle)); };
// на телефоне мини-плеер есть всегда: кнопки «Плеер» в нижней панели нет, в плеер ведёт он
const minibarOn = () => store.get('ui.minibar') !== '0' || plMobile();
const coverUrl = (uri, size) => uri ? (uri.includes('://') ? uri : 'https://' + uri.replace('%%', `${size}x${size}`)) : '';
// ---- перезапуск Алисы: экран ожидания вместо ошибок Glagol ----
// Включается при перезапуске из панели и сам, если Алиса (Glagol) пропала при живой колонке. Уходит, когда Glagol
// снова на связи (не раньше чем через 8 с после начала — до перезапуска он ещё «подключён»), или через 90 с.
let aliceWait = null, gWas = null;
function aliceRestarting(text, title) {
  aliceWait = { at: Date.now(), text, title };
  aliceWaitRender();
  loadMusic();
}
function aliceWaitActive() {
  if (!aliceWait) return false;
  const age = Date.now() - aliceWait.at;
  if (age > 90000 || (age > 8000 && mu.info?.glagol?.connected)) aliceWait = null;
  return !!aliceWait;
}
function aliceWaitRender() {
  const on = online && aliceWaitActive();
  $('#alice-wait').hidden = !on;
  if (!on) return;
  $('#alice-wait-title').textContent = aliceWait.title || 'Алиса перезапускается';
  $('#alice-wait-text').textContent = aliceWait.text || 'Обычно это около 30 секунд — пока она недоступна, музыка и команды не работают';
}
every(1000, aliceWaitRender);

// Мгновенный отклик кнопок плеера. muWant — что мы попросили; пока колонка не подтвердила (до 5 с), опрос
// состояния его не перетирает. Позицию трека при этом замораживаем или продолжаем с текущей.
let muWant = null;
function muOptimistic(playing) {
  const g = mu.info?.glagol, s = g?.state;
  if (!s) return;
  if (s.playerState) s.playerState.progress = musicPos();
  g.updated = g.progress_at = Date.now() / 1000;
  s.playing = playing;
  muWant = { playing, until: Date.now() + 5000 };
  renderMusic();
}
// «назад»/«вперёд»: короткая вспышка на самой кнопке — видно, что нажатие принято
function muPulse(btn) {
  if (!btn) return;
  btn.classList.remove('pulse'); void btn.offsetWidth; btn.classList.add('pulse');
}
const muCmd = (btn, payload, msg) => act(btn, async () => {
  const r = await api('/api/music/cmd', payload);
  if (r.status && r.status !== 'SUCCESS' && r.status !== 'timeout') throw new Error('Колонка ответила: ' + r.status);
  setTimeout(loadMusic, 200); setTimeout(loadMusic, 700);   // новое состояние колонки — как можно раньше
  return r;
}, msg);

// кнопки управления — одинаковые везде
const MU_BTNS = { dislike: ['i-dislike', 18, 'Дизлайк'], shuffle: ['i-shuffle', 18, 'Перемешать'], prev: ['i-prev', 20, 'Предыдущий'],
                  play: ['i-play', 24, 'Играть'], next: ['i-next', 20, 'Следующий'], repeat: ['i-repeat', 18, 'Повтор'], like: ['i-like', 18, 'Лайк'] };
$$('[data-mu-controls]').forEach(el => {
  const set = el.dataset.muControls === 'bar' ? ['shuffle', 'prev', 'play', 'next', 'repeat'] : Object.keys(MU_BTNS);
  el.innerHTML = set.map(k => `<button class="cbtn${k === 'play' ? ' big' : ''}" data-mu="${k}" aria-label="${MU_BTNS[k][2]}">${icon(MU_BTNS[k][0], MU_BTNS[k][1])}</button>`).join('');
});

function updateMinibar() {
  const show = online && minibarOn() && !!mu.info?.auth?.logged_in && $('#tab-player').hidden;
  $('#minibar').hidden = !show;
  document.body.classList.toggle('has-minibar', show);
}

let voiceInput = null;
async function loadMusic() {
  const need = !$('#tab-overview').hidden || !$('#tab-player').hidden || (minibarOn() && mu.info?.auth?.logged_in !== false) || !!aliceWait;
  if (!online) { voiceInput?.update(false); return; }
  if (document.hidden || !need) return;
  try { mu.info = await api('/api/music'); } catch { voiceInput?.update(false); return; }
  // Алиса пропала при живой колонке (её перезапустили — например, сторож Bluetooth-колонки) — экран ожидания
  const gNow = !!mu.info?.glagol?.connected;
  if (gWas && !gNow && online && mu.info?.auth?.logged_in && !aliceWait) aliceRestarting('Ждём, пока она снова выйдет на связь — обычно это меньше минуты', 'Алиса недоступна');
  gWas = gNow;
  const s = mu.info?.glagol?.state;
  if (muWant && s) {   // колонка ещё не подтвердила паузу/игру — показываем то, что попросили
    if (Date.now() > muWant.until || !!s.playing === muWant.playing) muWant = null;
    else s.playing = muWant.playing;
  }
  renderMusic();
}
every(1000, loadMusic);

function musicPos() {
  const s = muState(), p = s?.playerState;
  if (!p) return 0;
  // + сколько прошло с момента, когда позиция измерена (по Bluetooth её берёт сервер у телефона — progress_at)
  const since = s.playing ? (Date.now() / 1000 - (mu.info.glagol.progress_at || mu.info.glagol.updated)) : 0;
  return Math.min(p.duration || Infinity, (p.progress || 0) + Math.max(0, since));
}

// Ссылки на music.yandex.ru — по данным трека, которые уже пришли из API Яндекс Музыки.
const YM = 'https://music.yandex.ru';
const ymA = (path, text, cls = '') => `<a class="ym${cls ? ' ' + cls : ''}" href="${YM}${esc(path)}" target="_blank" rel="noopener" title="Открыть в Яндекс Музыке">${esc(text)}</a>`;
const ymTrackPath = t => t.album?.id ? `/album/${t.album.id}/track/${t.id}` : `/track/${t.id}`;
// Источник воспроизведения: альбом, плейлист (владелец:номер) или исполнитель
function ymSourcePath(p) {
  const id = String(p?.playlistId || '');
  if (!id || id === 'user:onyourwave') return null;
  if (p.playlistType === 'Album') return `/album/${id}`;
  if (p.playlistType === 'Artist') return `/artist/${id}`;
  if (p.playlistType === 'Playlist') { const [owner, kind] = id.includes(':') ? id.split(':') : [p.playlistPuid, id]; return owner && kind ? `/users/${owner}/playlists/${kind}` : null; }
  return null;
}
// Название и исполнитель сменились — старый текст гаснет и уходит вверх, новый проявляется (во всех плеерах).
// Та же надпись, только ставшая ссылкой (пришли подробности трека), — сразу, без мигания.
function swapText(e, html, text, after) {
  if (e._html === html) return;
  if (e._next != null) { e._next = html; e._txt = text; return; }   // идёт смена — покажем уже последнее
  if (e._txt === undefined || e._txt === text || matchMedia('(prefers-reduced-motion: reduce)').matches) {
    e._txt = text; setHtml(e, html); after?.(); return;
  }
  e._txt = text; e._next = html;
  e.classList.add('tx-out');
  setTimeout(() => { setHtml(e, e._next); e._next = null; after?.(); e.classList.remove('tx-out'); }, 180);
}
// Ставим разметку, только если она поменялась: иначе renderMusic (4 раза в секунду) пересоздаёт ссылку под курсором и клик теряется.
const setHtml = (e, html) => { if (e._html !== html) { e._html = html; e.innerHTML = html; } };

// Плавный прогресс — только ползунки и время, без перерисовки обложки, ссылок и кнопок.
function renderMusicProgress() {
  const s = muState(), p = muPlayer();
  const isBt = p?.playerType === 'bluetooth';
  const pos = musicPos();
  $$('input.mu-seek').forEach(r => {
    // по Bluetooth позиция приходит верная, но перематывать колонка не умеет — только на телефоне
    r.disabled = !p?.duration || isBt;
    r.title = isBt ? 'По Bluetooth перематывать можно только на телефоне' : '';
    if (!mu.seeking) { r.max = Math.round(p?.duration || 100); r.value = Math.round(pos); }
  });
  if (!mu.seeking) $$('.mu-pos').forEach(e => e.textContent = fmtDur(pos));
  $$('.mu-dur').forEach(e => e.textContent = fmtDur(p?.duration));
  if (typeof s?.volume === 'number' && Date.now() - mu.volSet > 2500) {
    $$('input.mu-vol').forEach(r => r.value = Math.round(s.volume * 100));
    $$('.mu-vol-v').forEach(e => e.textContent = Math.round(s.volume * 100));
  }
}

function renderMusic() {
  const i = mu.info;
  if (!i) return;
  const logged = i.auth.logged_in;
  const commandsReady = online && logged && !!i.glagol?.connected;
  $('[data-widget="alice"]').hidden = !commandsReady;
  $('#pl-command').hidden = !commandsReady;
  $$('.mu-ask-form').forEach(form => { form.elements.text.disabled = !commandsReady; });
  voiceInput?.update(commandsReady);
  updateMinibar();
  $('#mu-login').hidden = logged;
  $('#mu-player').hidden = !logged;
  $('#mu-logout').hidden = !logged;
  ymLogin = logged ? i.auth.login || '' : '';
  $('#mu-account').innerHTML = ymLogin ? secret('login', ymLogin) : '';
  $('#pl-login').hidden = logged;
  $('#pl-body').hidden = !logged;
  if (!logged) { if (!mu.qr && !$('#tab-overview').hidden) startQr(); return; }
  stopQr();

  const g = i.glagol, s = g.state, p = s?.playerState, has = muHas();
  const isBt = p?.playerType === 'bluetooth';
  document.body.classList.toggle('mu-bt', isBt);
  $('#mu-err').hidden = g.connected || aliceWaitActive();
  $('#mu-err').textContent = g.connected ? '' : (g.error || 'Подключаемся к колонке…');
  const title = has ? p.title : 'Ничего не играет';
  const artist = has ? (p.subtitle || '') + (isBt && p.extra?.album ? ' — ' + p.extra.album : '') : (g.connected ? 'Нажмите ▶ — включится «Моя волна»' : '');
  // В виджете и на вкладке «Плеер» название и исполнители — ссылки на Яндекс Музыку (нижний плеер ведёт во вкладку).
  const t = !isBt && mu.track && mu.track.id === String(p?.id) ? mu.track : null;
  const links = !plMobile();   // на телефоне ссылок наружу нет: исполнитель открывает «Об исполнителе»
  $$('.mu-title').forEach(e => {
    const html = t && links && e.closest('#music-card, #tab-player') ? ymA(ymTrackPath(t), title) : esc(title);
    if (e.classList.contains('mq-line')) swapText(e, `<span class="mq">${html}</span>`, title, () => mqFit(e));
    else swapText(e, html, title);
  });
  const artists = t?.artistList?.length ? t.artistList.map(a => a.name).join(', ') : artist;
  $$('.mu-artist').forEach(e => {
    if (t?.artistList?.length && links && e.closest('#music-card, #tab-player')) swapText(e, t.artistList.map(a => ymA(`/artist/${a.id}`, a.name)).join(', '), artists);
    else swapText(e, esc(artists), artists);
  });
  const cover = has && !isBt ? coverUrl(p.extra?.coverURI, 400) : '';
  $$('.mu-cover').forEach(e => {
    const url = e.classList.contains('pl-cover') && mu.track?.coverBig ? mu.track.coverBig : cover;
    e.style.backgroundImage = url ? cssUrl(url) : '';
    if (e.firstElementChild) e.firstElementChild.style.display = url ? 'none' : '';
  });
  plTint(has && !isBt ? coverUrl(p.extra?.coverURI, 50) : '');
  if (!s?.playing !== plPausedShown) { plPausedShown = !s?.playing; plTintApply(plTintHsl); }

  const playing = !!s?.playing, rep = p?.entityInfo?.repeatMode || 'None';
  $$('[data-mu="play"]').forEach(b => { b.innerHTML = icon(playing ? 'i-pause' : 'i-play', 24); b.setAttribute('aria-label', playing ? 'Пауза' : 'Играть'); });
  $$('[data-mu="prev"]').forEach(b => b.disabled = !p?.hasPrev);
  $$('[data-mu="next"]').forEach(b => b.disabled = !p?.hasNext);
  $$('[data-mu="repeat"]').forEach(b => {
    b.classList.toggle('on', rep !== 'None');
    b.innerHTML = icon('i-repeat', 18) + (rep === 'One' ? '<span class="one">1</span>' : '');
    b.setAttribute('aria-label', { None: 'Повтор выключен', All: 'Повтор всех', One: 'Повтор трека' }[rep]);
  });
  $$('[data-mu="shuffle"]').forEach(b => b.classList.toggle('on', !!p?.entityInfo?.shuffled));
  $$('[data-mu="like"]').forEach(b => { b.classList.toggle('liked', !!mu.track?.liked); b.setAttribute('aria-label', mu.track?.liked ? 'Убрать лайк' : 'Лайк'); });
  $$('[data-mu="shuffle"], [data-mu="repeat"], [data-mu="like"], [data-mu="dislike"]').forEach(b => b.disabled = !has);
  renderMusicProgress();

  if (p?.id && p.id !== mu.trackId) {   // сменился трек — подробности и всё для вкладки «Плеер»
    mu.trackId = p.id; mu.track = null; mu.extras = {};
    if (!isBt) api('/api/music/info?id=' + encodeURIComponent(p.id)).then(t => { if (mu.trackId === p.id) { mu.track = t; renderMusic(); } }).catch(() => {});
    if (!$('#tab-player').hidden) loadPlayerExtras();
  }
  if (!$('#tab-player').hidden) renderPlayerTab();
}
every(250, () => muState()?.playing && !mu.seeking && renderMusicProgress());   // плавный прогресс

// ----- вкладка «Плеер» -----
const PL_TYPES = { Radio: 'Радио', Album: 'Альбом', Playlist: 'Плейлист', Artist: 'Исполнитель', Track: 'Трек', FmRadio: 'FM-радио' };
function renderPlayerTab() {
  const s = muState(), p = muPlayer(), t = mu.track;
  const q = mu.extras.queue;
  const src = q?.title || (p?.playlistId === 'user:onyourwave' ? 'Моя волна' : p?.playlistDescription);
  const isBt = p?.playerType === 'bluetooth';
  const srcPath = !isBt && muHas() ? ymSourcePath(p) : null;
  setHtml($('#pl-source'), isBt ? 'Bluetooth · звук с телефона или компьютера'
    : muHas() ? esc(PL_TYPES[p.playlistType] || p.playlistType || '') + (src ? ' · ' + (srcPath ? ymA(srcPath, src) : esc(src)) : '') : 'Ничего не играет');
  $('#pl-msrc').textContent = isBt ? 'Bluetooth' : muHas() ? (src || PL_TYPES[p.playlistType] || p.playlistType || '') : '';
  $('#pl-kind').textContent = p ? (isBt ? 'Bluetooth' : PL_TYPES[p.type] || p.type || 'Трек') + (s?.playing ? ' · играет' : ' · пауза') : '—';
  const meta = [];
  if (t) {
    if (t.album?.title) meta.push(t.album.id ? ymA(`/album/${t.album.id}`, `Альбом «${t.album.title}»`, 'badge') : `<span class="badge">${esc(`Альбом «${t.album.title}»`)}</span>`);
    for (const m of [t.album?.year, t.album?.genre, t.album?.label, t.album?.trackCount && `${t.album.trackCount} треков в альбоме`, t.explicit && '18+'])
      if (m) meta.push(`<span class="badge">${esc(m)}</span>`);
  }
  setHtml($('#pl-meta'), meta.join(''));
  // текст: если есть синхронизация — подсвечиваем текущую строку
  const ly = mu.extras.lyrics;
  if (ly?.sync) {
    const pos = musicPos(), idx = ly.sync.reduce((k, l, j) => l[0] <= pos ? j : k, -1);
    if (idx !== mu.extras.lyIdx) {
      mu.extras.lyIdx = idx;
      $$('#pl-lyrics .ln').forEach((el, j) => el.classList.toggle('cur', j === idx));
      // Прокручиваем только сам блок текста (не страницу) и не мешаем, если его недавно крутили рукой.
      const box = $('#pl-lyrics'), line = $(`#pl-lyrics .ln:nth-child(${idx + 1})`);
      if (line && Date.now() - (box._userScroll || 0) > 6000) {
        box._auto = Date.now();
        box.scrollTo({ top: line.offsetTop - box.clientHeight / 2 + line.offsetHeight / 2, behavior: 'smooth' });   // offsetTop — от самого блока (position: relative)
      }
    }
  }
  if ($('#pl-raw-box').open) renderRaw();
}
function renderRaw() {
  const out = [], walk = (o, pre) => Object.entries(o || {}).forEach(([k, v]) =>
    v && typeof v === 'object' && !Array.isArray(v) ? walk(v, pre + k + '.') : out.push([pre + k, Array.isArray(v) ? `[${v.length}]` : String(v)]));
  walk(muState(), '');
  $('#pl-raw').innerHTML = out.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v.length > 140 ? v.slice(0, 140) + '…' : v)}</dd>`).join('');
}
$('#pl-raw-box').addEventListener('toggle', renderRaw);
// ---- телефон: листы снизу (текст, очередь, похожие, исполнитель, просьба Алисе), назад и «смахнуть вниз» ----
// ---- телефон: листы снизу для длинных списков ----
// mSheet(первый, …остальные, {title, label}) оборачивает элементы в лист; на телефоне вместо них — кнопка.
var mSheets = [];   // var: showTab может закрыть листы раньше, чем дойдёт до этой строки
function mSheet(nodes, title, label) {
  const first = nodes[0], wrap = document.createElement('div');
  wrap.className = 'msheet';
  wrap.innerHTML = `<div class="msheet-head"><b>${esc(title)}</b><button type="button" class="btn sm icon" aria-label="Закрыть">✕</button></div>`;
  first.before(wrap);
  nodes.forEach(n => wrap.append(n));
  const open = document.createElement('button');
  open.type = 'button'; open.className = 'btn msheet-open';
  open.innerHTML = `${icon('i-log', 16)}<span>${esc(label || title)}</span><span class="meta"></span>`;
  wrap.before(open);
  open.onclick = () => mSheetShow(wrap);
  wrap.querySelector('.msheet-head button').onclick = () => mSheetShow(null);
  mSheets.push(wrap);
  sheetDrag(wrap, () => wrap.classList.contains('open'), () => mSheetShow(null));
  return { wrap, open, count: text => { open.querySelector('.meta').textContent = text || ''; } };
}
function mSheetShow(wrap) {
  if (wrap && !plMobile()) return;
  (mSheets || []).forEach(w => w.classList.toggle('open', w === wrap));
  $('#msheet-bg').classList.toggle('open', !!wrap);
  document.body.classList.toggle('msheet-on', !!wrap);
}
const mSheetOf = el => el.closest('.msheet');
$('#msheet-bg').onclick = () => mSheetShow(null);
document.addEventListener('keydown', e => { if (e.key === 'Escape' && document.body.classList.contains('msheet-on')) mSheetShow(null); });
// Лист снизу закрывается свайпом вниз из любого места, если он (и список под пальцем) прокручен до верха —
// не нужно тянуться к его верхнему краю. Лист едет за пальцем; отпустили ниже ~90 px — закрыт, иначе встаёт назад.
function sheetDrag(el, isOpen, close) {
  let y0 = 0, dy = 0, atTop = false, dragging = false;
  el.addEventListener('touchstart', e => {
    if (!isOpen() || e.touches.length !== 1) return;
    y0 = e.touches[0].clientY; dy = 0; dragging = false;
    // прокручен ли до верха и сам лист, и всё прокручиваемое под пальцем (текст песни, журнал)
    atTop = true;
    for (let n = e.target; n && n !== el.parentElement; n = n.parentElement) if (n.scrollTop > 0) { atTop = false; break; }
  }, { passive: true });
  el.addEventListener('touchmove', e => {
    if (!atTop || !isOpen()) return;
    dy = e.touches[0].clientY - y0;
    if (dy <= 0 && !dragging) { atTop = false; return; }   // повели вверх — это прокрутка, не закрытие
    if (e.target.closest('input[type=range], textarea')) return;
    e.preventDefault(); dragging = true;
    el.style.transition = 'none'; el.style.transform = `translateY(${Math.max(0, dy)}px)`;
  }, { passive: false });
  el.addEventListener('touchend', () => {
    if (!dragging) return;
    dragging = false; el.style.transition = '';
    if (dy > 90) close();
    el.style.transform = '';
  });
}
sheetDrag($('#nav-sheet'), () => document.body.classList.contains('nav-open'), () => navSheet(false));
$$('#tab-player [data-pl-sec]').forEach(x => sheetDrag(x, () => x.classList.contains('open'), () => plSheet('')));
sheetDrag($('#pl-command'), () => $('#tab-player').classList.contains('pl-ask'), () => plSheet(''));
sheetDrag($('#sc-add-step'), () => $('#sc-board').classList.contains('pal-open'), () => $('#sc-board').classList.remove('pal-open'));

// какие списки уходят в листы
mSheet([$('#snd-folders'), $('#snd-filter').closest('.search'), $('#sounds')], 'Звуки колонки', 'Все звуки');
mSheet([$('#pat-kind').closest('.card-head'), $('#patterns')], 'Анимации', 'Анимации кольца и часов');
mSheet([$('#al-log')], 'Журнал будильника', 'Журнал');
mSheet([$('#sc-log')], 'Журнал сценариев', 'Журнал');
mSheet([$('#y2-log')], 'Лог службы', 'Лог службы');
mSheet([$('#yam-log')], 'Лог пакета', 'Лог пакета');

// ---- телефон: плеер в цветах обложки ----
// Маленькую обложку (её сервер разрешает читать) рисуем на холсте 24×24 и ищем самый «живой» цвет: оттенки
// раскладываем по 12 корзинам, вес пикселя — насыщенность и сила цвета (тёмные цветные тоже идут, чуть легче).
// Бесцветная (чёрно-белая) обложка — оставляем обычный акцент. Из оттенка — акцент, мягкий фон и цвет текста под тему.
let plTintFor = null, plTintHsl = null, plPausedShown = false;
function plTint(url) {
  if (url === plTintFor) return;
  plTintFor = url;
  if (!url) return plTintApply(null);
  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    if (plTintFor !== url) return;
    try {
      const c = document.createElement('canvas'); c.width = c.height = 24;
      const g = c.getContext('2d', { willReadFrequently: true }); g.drawImage(img, 0, 0, 24, 24);
      const d = g.getImageData(0, 0, 24, 24).data;
      // сначала яркие цвета; нет их — приглушённые (серо-бирюзовая, тёмная обложка тоже со своим оттенком)
      const pass = minChroma => {
        const bins = Array.from({ length: 12 }, () => ({ w: 0, h: 0, s: 0 }));
        let total = 0;
        for (let i = 0; i < d.length; i += 4) {
          const r = d[i] / 255, gg = d[i + 1] / 255, b = d[i + 2] / 255, max = Math.max(r, gg, b), min = Math.min(r, gg, b), l = (max + min) / 2;
          // почти чёрное, почти белое и серое без оттенка (шум JPEG) не считаем; тёмные цветные — считаем
          if (max - min < minChroma || l < .04 || l > .95) continue;
          const sat = (max - min) / (1 - Math.abs(2 * l - 1));
          let h = max === r ? (gg - b) / (max - min) : max === gg ? 2 + (b - r) / (max - min) : 4 + (r - gg) / (max - min);
          h = (h * 60 + 360) % 360;
          const w = sat * (max - min) * (.4 + .6 * (1 - Math.abs(l - .5) * 2)), k = bins[Math.floor(h / 30) % 12];
          k.w += w; k.h += h * w; k.s += sat * w; total += w;
        }
        const best = bins.reduce((x, y) => y.w > x.w ? y : x);
        return { total, h: best.w ? Math.round(best.h / best.w) : 0, s: best.w ? best.s / best.w : 0 };
      };
      let col = pass(.07), muted = false;
      if (col.total < 3) { col = pass(.025); muted = true; }
      plTintApply(col.total < (muted ? .6 : 3) ? null : { h: col.h, s: Math.min(.9, Math.max(muted ? .3 : .45, col.s)) });
    } catch { plTintApply(null); }
  };
  img.onerror = () => plTintApply(null);
  img.src = url;
}
function plTintApply(hsl) {
  plTintHsl = hsl;
  document.body.classList.add('pl-tinted');
  // кнопки остаются в акценте темы — в цвет обложки только фон и визуализатор; у чёрно-белой обложки — акцент.
  // На паузе цвет приглушается почти до серого (плавно, как и смена трека) — видно, что музыка стоит
  const dark = getComputedStyle(document.documentElement).colorScheme.includes('dark');
  const base = hsl ? `hsl(${hsl.h} ${Math.round(hsl.s * 100)}% ${dark ? 60 : 50}%)`
    : getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
  document.body.style.setProperty('--pl-tint', plPausedShown ? `color-mix(in srgb, ${base} 25%, ${dark ? '#55555f' : '#b4b4bc'})` : base);
}
// визуализатор берёт текущий (перетекающий) цвет, а не конечный
const plTintColor = () => plMobile() && document.body.classList.contains('pl-tinted') ? getComputedStyle(document.body).getPropertyValue('--pl-tint').trim() || null : null;
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => plTintApply(plTintHsl));
addEventListener('themechange', () => plTintApply(plTintHsl));

// Бегущая строка: сдвиг — на сколько текст длиннее строки, скорость ~40 px/с
function mqFit(el) {
  const inner = el?.querySelector('.mq');
  if (!inner || !el.clientWidth) return;
  el.classList.remove('run');
  const d = inner.scrollWidth - el.clientWidth;
  if (d <= 2) return;
  el.style.setProperty('--mq', -(d + 24) + 'px');
  el.style.setProperty('--mqt', (3 + d / 40).toFixed(1) + 's');
  el.classList.add('run');
}
const mqObserver = new ResizeObserver(entries => entries.forEach(x => mqFit(x.target)));
$$('.mq-line').forEach(el => mqObserver.observe(el));
function plMobile() { return matchMedia('(max-width: 760px)').matches; }
var plOpen = '';   // var: showTab может вызвать plSheet раньше, чем дойдёт до этой строки
function plSheet(name) {
  plOpen = name;
  $$('#tab-player [data-pl-sec]').forEach(x => x.classList.toggle('open', x.dataset.plSec === name));
  $('#tab-player').classList.toggle('pl-ask', name === 'ask');
  $('#pl-sheet-bg').classList.toggle('open', !!name);
  $$('.pl-mtabs [data-pl-open]').forEach(b => b.setAttribute('aria-pressed', b.dataset.plOpen === name));
  if (name === 'ask') setTimeout(() => $('#pl-command textarea')?.focus(), 60);
}
$$('.pl-mtabs [data-pl-open]').forEach(b => b.onclick = () => plSheet(plOpen === b.dataset.plOpen ? '' : b.dataset.plOpen));
$('#pl-sheet-bg').onclick = () => plSheet('');
// исполнитель — по нажатию и на его имя, и на название трека
['.pl-artist', '.pl-title'].forEach(sel => $('#tab-player ' + sel).addEventListener('click', () => {
  if (plMobile() && !document.body.classList.contains('mu-bt') && muHas()) plSheet('artist');
}));
document.addEventListener('keydown', e => { if (e.key === 'Escape' && plOpen) plSheet(''); });
const plBack = () => { location.hash = prevTab && prevTab !== 'player' ? prevTab : 'overview'; };
// закрыть плеер: уезжает вниз и гаснет (как после смахивания), потом — прошлая вкладка
function plClose() {
  const body = $('#pl-body');
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return plBack();
  body.style.transform = 'translateY(100%)'; body.style.opacity = '0';
  setTimeout(() => { plBack(); body.style.transform = body.style.opacity = ''; }, 230);
}
$('#pl-back').onclick = plClose;
// Жесты плеера на телефоне. Вниз (за верх, обложку или название) — закрыть плеер и вернуться назад.
// Вбок по обложке или названию — сменить трек: влево следующий, вправо предыдущий; обложка уезжает за пальцем,
// а новая выезжает с другой стороны, когда колонка переключит трек. Направление — по первым ~10 px движения.
function plGesture(e) {
  if (!plMobile() || e.pointerType === 'mouse' || e.button || e.target.closest('button, input, textarea, a, [data-pl-sec], .pl-mtabs, .pl-controls, .mu-volrow, .progress'))
    return;
  // вбок — только над перемоткой: обложка с фоном вокруг неё и название; вниз — откуда угодно
  const allowSide = !plSwapping && !!e.target.closest('.pl-cover-box, .pl-title, .pl-artist');
  const body = $('#pl-body'), cover = $('#tab-player .pl-cover'), el = e.currentTarget, x0 = e.clientX, y0 = e.clientY;
  let mode = '', dx = 0, dy = 0;
  const move = ev => {
    dx = ev.clientX - x0; dy = ev.clientY - y0;
    if (!mode && Math.hypot(dx, dy) > 10) {
      mode = Math.abs(dx) > Math.abs(dy) ? (allowSide ? 'side' : 'none') : 'down';   // вбок, пока трек меняется, — ничего
      // захватываем касание, только когда это уже свайп: иначе нажатие на название ушло бы не ему, а плееру
      try { el.setPointerCapture(ev.pointerId); } catch {}
    }
    if (mode === 'down') {
      body.classList.add('dragging');
      body.style.transform = `translateY(${Math.max(0, dy)}px)`; body.style.opacity = String(1 - Math.min(Math.max(0, dy) / 600, .5));
    } else if (mode === 'side') {
      cover.style.transition = 'none';
      cover.style.transform = `translateX(${dx}px) rotate(${dx / 40}deg)`; cover.style.opacity = String(1 - Math.min(Math.abs(dx) / 400, .6));
    }
  };
  const up = () => {
    el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up); el.removeEventListener('pointercancel', up);
    if (mode === 'down') {
      body.classList.remove('dragging');
      if (dy > 120) plClose(); else body.style.transform = body.style.opacity = '';
    } else if (mode === 'side') plSwipeEnd(dx);
  };
  el.addEventListener('pointermove', move); el.addEventListener('pointerup', up); el.addEventListener('pointercancel', up);
}
async function plSwipeEnd(dx) {
  const cover = $('#tab-player .pl-cover'), p = muPlayer(), dir = dx < 0 ? 'next' : 'prev';
  const ok = Math.abs(dx) > 80 && (dir === 'next' ? p?.hasNext : p?.hasPrev);
  cover.style.transition = 'transform .25s ease, opacity .25s ease';
  if (!ok) { cover.style.transform = cover.style.opacity = ''; return; }
  plCoverSwap(dir, () => musicAction(dir, null, true));
}
// Смена трека с анимацией обложки — после свайпа и по кнопкам «назад»/«вперёд», на телефоне и на компьютере:
// все видимые обложки (плеер, виджет, мини-плеер) уезжают (вперёд — влево, назад — вправо), а когда колонка
// переключит трек, новые выезжают с другой стороны. В плеере на телефоне — далеко, за край; в остальных — коротко, не выходя за карточку.
let plSwapping = false;
async function plCoverSwap(dir, send) {
  const covers = $$('.mu-cover').filter(e => e.offsetParent), sign = dir === 'next' ? -1 : 1, was = mu.trackId;
  if (plSwapping) return;   // трек уже меняется — второй раз не переключаем, пока новый не встанет на место
  if (!covers.length) return send();
  plSwapping = true;
  const far = c => c.classList.contains('pl-cover') && plMobile();
  covers.forEach(c => {
    c.style.transition = 'transform .25s ease, opacity .25s ease';
    c.style.transform = far(c) ? `translateX(${sign * 120}%) rotate(${sign * 8}deg)` : `translateX(${sign * 18}px) rotate(${sign * 6}deg) scale(.86)`;
    c.style.opacity = '0';
  });
  send();
  // ждём новый трек (не дольше 3 с), потом новая обложка выезжает с другой стороны
  for (let i = 0; i < 30 && mu.trackId === was; i++) await new Promise(r => setTimeout(r, 100));
  await new Promise(r => setTimeout(r, 150));   // дать обложке смениться
  covers.forEach(c => {
    c.style.transition = 'none';
    c.style.transform = far(c) ? `translateX(${-sign * 60}%)` : `translateX(${-sign * 14}px) rotate(${-sign * 5}deg) scale(.86)`;
  });
  void document.body.offsetWidth;
  covers.forEach(c => {
    c.style.transition = 'transform .35s cubic-bezier(.2, .8, .2, 1), opacity .35s ease';
    c.style.transform = c.style.opacity = '';
  });
  await new Promise(r => setTimeout(r, 350));   // новая обложка встала — можно переключать дальше
  plSwapping = false;
}
$('#pl-body').addEventListener('pointerdown', plGesture);
// Обложка на телефоне: тап — пауза/играть; двойной тап по краю (треть ширины) — перемотка на 10 с в ту сторону.
// По краю одиночный тап ждём 250 мс — вдруг будет второй.
let plTapT = 0, plTapTimer = null;
$('#tab-player .pl-cover-box').addEventListener('click', e => {
  if (!plMobile() || plSwapping) return;
  const r = $('#tab-player .pl-cover').getBoundingClientRect(), x = (e.clientX - r.left) / r.width;
  const side = x < 1 / 3 ? -1 : x > 2 / 3 ? 1 : 0, now = Date.now();
  if (side && now - plTapT < 300 && plTapTimer) {
    clearTimeout(plTapTimer); plTapTimer = null; plTapT = 0;
    const p = muPlayer();
    if (!p?.duration || document.body.classList.contains('mu-bt')) return;
    const pos = Math.max(0, Math.min(p.duration - 1, musicPos() + side * 10));
    const tip = document.createElement('span'); tip.className = 'pl-seek-tip'; tip.textContent = side < 0 ? '« 10 с' : '10 с »';
    tip.style[side < 0 ? 'left' : 'right'] = '18%'; $('#tab-player .pl-cover-box').append(tip); setTimeout(() => tip.remove(), 650);
    p.progress = pos; mu.info.glagol.updated = mu.info.glagol.progress_at = Date.now() / 1000; renderMusic();
    return muCmd(null, { command: 'rewind', position: Math.round(pos) });
  }
  plTapT = now;
  clearTimeout(plTapTimer);
  plTapTimer = setTimeout(() => { plTapTimer = null; musicAction('play', null); }, side ? 260 : 0);
});
// Компьютер: выбор колонки рядом с громкостью большого плеера — тот же список, что в боковом меню
(async () => {
  if (!PanelContext.deviceId) return;
  try {
    const list = (await PanelContext.fetch('/api/speakers').then(x => x.json())).devices || [];
    if (list.length < 2) return;
    const sel = $('#pl-speaker');
    sel.innerHTML = list.map(d => `<option value="${esc(d.id)}" ${d.id === PanelContext.deviceId ? 'selected' : ''}>${esc(d.name)}</option>`).join('');
    sel.onchange = () => { sel.disabled = true; location.assign('/speaker/' + encodeURIComponent(sel.value) + '/#player'); };
    $('#pl-speaker-row').hidden = false;
  } catch {}
})();
// Громкость и колонки — листом по кнопке в углу; колонки — если панель управляет несколькими (speakers.json)
$('#pl-vol-btn').onclick = async () => {
  plSheet(plOpen === 'vol' ? '' : 'vol');
  if (plOpen !== 'vol' || !PanelContext.deviceId) return;
  try {
    const r = await PanelContext.fetch('/api/speakers').then(x => x.json());
    const list = r.devices || [];
    $('#pl-speakers').hidden = list.length < 2;
    $('#pl-speakers-list').innerHTML = list.map(d => `<a class="trow${d.id === PanelContext.deviceId ? ' cur' : ''}" href="/speaker/${encodeURIComponent(d.id)}/#player">
      <span class="ph" style="display:grid;place-items:center">${icon('i-vol', 16)}</span><span class="tt"><b>${esc(d.name)}</b><span>${d.id === PanelContext.deviceId ? 'сейчас управляете ей' : 'перейти к ней'}</span></span></a>`).join('');
  } catch { $('#pl-speakers').hidden = true; }
};
// на телефоне подсказка короче: «Попросить Алису» и так написано на кнопке, а длинная не влезает
const plPlaceholder = () => { $('#pl-command textarea').placeholder = plMobile() ? 'Например, «включи джаз»' : 'Попросить Алису: «включи джаз»'; };
plPlaceholder();
matchMedia('(max-width: 760px)').addEventListener('change', () => { plSheet(''); plPlaceholder(); plTintApply(plTintHsl); renderMusic(); });
// ручная прокрутка текста (колесо, тач, перетаскивание полосы) — ставит автопрокрутку на паузу
['wheel', 'touchmove', 'pointerdown'].forEach(ev => $('#pl-lyrics').addEventListener(ev, () => { $('#pl-lyrics')._userScroll = Date.now(); }, { passive: true }));
const trackRow = (t, cur, tag) => `<button class="trow${cur ? ' cur' : ''}" data-play="${esc(t.id)}" ${t.available === false ? 'disabled' : ''}>
  ${t.cover ? `<img src="${esc(t.cover)}" alt="" loading="lazy">` : '<span class="ph"></span>'}
  <span class="tt"><b>${esc(t.title)}</b><span>${esc(t.artists)}</span></span>
  ${tag ? `<span class="badge${cur ? ' ok' : ''}">${esc(tag)}</span>` : ''}<span class="td">${t.durationMs ? fmtDur(t.durationMs / 1000) : ''}</span></button>`;
async function loadPlayerExtras() {
  const id = mu.trackId;
  if (!id || mu.extras.for === id) return;
  mu.extras = { for: id };
  $('#pl-lyrics').innerHTML = $('#pl-queue').innerHTML = $('#pl-similar').innerHTML = '<div class="empty">Загрузка…</div>';
  const p = muPlayer(), isBt = p?.playerType === 'bluetooth';
  // По Bluetooth у трека нет id Яндекса: текст ищем в LRCLIB по названию, очередь и похожие не нужны.
  const lyUrl = isBt ? '/api/music/lyrics?' + new URLSearchParams({ title: p.title || '', artist: p.subtitle || '', album: p.extra?.album || '', duration: p.duration || 0 })
                     : '/api/music/lyrics?id=' + id;
  const none = Promise.reject(new Error('—'));
  none.catch(() => {});
  const [ly, q, sim] = await Promise.allSettled([api(lyUrl), isBt ? none : api('/api/music/queue'), isBt ? none : api('/api/music/similar?id=' + id)]);
  if (mu.trackId !== id) return;
  const L = ly.status === 'fulfilled' ? ly.value : {};
  if (L.text) {
    mu.extras.lyrics = L;
    $('#pl-lyrics').innerHTML = L.sync
      ? L.sync.map(l => `<span class="ln">${esc(l[1]) || '&nbsp;'}</span>`).join('')
      : esc(L.text);
    $('#pl-ly-meta').textContent = L.sync ? 'идёт вместе с песней' : '';
  } else {
    $('#pl-lyrics').innerHTML = `<div class="empty">${L.instrumental ? 'Инструментальный трек — без слов' : 'Текст не нашёлся ни в Яндекс Музыке, ни в LRCLIB'}</div>`;
    $('#pl-ly-meta').textContent = '';
  }
  // откуда текст
  const SRC = { 'Яндекс Музыка': 'Текст: Яндекс Музыка', 'LRCLIB': 'Текст: <a href="https://lrclib.net" target="_blank" rel="noopener">LRCLIB</a> — открытая база текстов песен' };
  $('#pl-ly-src').hidden = !L.source;
  $('#pl-ly-src').innerHTML = SRC[L.source] || '';
  if (q.status === 'fulfilled') {
    mu.extras.queue = q.value;
    $('#pl-q-title').textContent = q.value.title ? `Очередь · ${q.value.title}` : 'Очередь';
    $('#pl-q-count').textContent = q.value.tracks.length > 3 ? `${q.value.tracks.length} треков` : '';
    const cur = q.value.current, tagOf = k => cur == null ? '' : k === cur ? 'сейчас' : k === cur + 1 ? 'далее' : k === cur - 1 ? 'было' : '';
    $('#pl-queue').innerHTML = q.value.tracks.map((t, k) => trackRow(t, k === cur, tagOf(k))).join('') || '<div class="empty">Пусто</div>';
    // только сам список: scrollIntoView прокрутил бы и родителей — на телефоне закреплённую страницу плеера,
    // и он при открытии улетал вверх и тут же возвращался
    const row = $('#pl-queue .trow.cur'), list = $('#pl-queue');
    if (row) list.scrollTop += row.getBoundingClientRect().top - list.getBoundingClientRect().top - (list.clientHeight - row.offsetHeight) / 2;
  } else $('#pl-queue').innerHTML = `<div class="empty">${esc(q.reason?.message || 'Не удалось')}</div>`;
  $('#pl-similar').innerHTML = sim.status === 'fulfilled' && sim.value.length ? sim.value.map(t => trackRow(t)).join('') : '<div class="empty">Нет</div>';
  if (isBt) return;
  // исполнитель — когда приедут подробности о треке
  const waitTrack = async () => { for (let k = 0; k < 20 && !mu.track && mu.trackId === id; k++) await new Promise(r => setTimeout(r, 300)); };
  await waitTrack();
  const aid = mu.track?.artistList?.[0]?.id;
  if (!aid) { $('#pl-artist').innerHTML = '<div class="empty">—</div>'; return; }
  try {
    const a = await api('/api/music/artist?id=' + aid);
    if (mu.trackId !== id) return;
    $('#pl-artist').innerHTML = `<div class="artist-grid">
      ${a.cover ? `<img src="${esc(a.cover)}" ${a.cover_big ? `srcset="${esc(a.cover)} 300w, ${esc(a.cover_big)} 1000w" sizes="(max-width: 760px) 100vw, 140px"` : ''} alt="" style="object-fit:cover">` : '<span></span>'}
      <div class="col" style="gap:8px">
        <div style="font-size:22px;font-weight:800">${ymA(`/artist/${a.id}`, a.name)}</div>
        <div class="pl-meta">${(a.genres || []).map(g => `<span class="badge">${esc(g)}</span>`).join('')}
          ${a.likes ? `<span class="badge">♥ ${a.likes.toLocaleString('ru')}</span>` : ''}
          ${a.tracks ? `<span class="badge">${a.tracks} треков</span>` : ''}${a.albums ? `<span class="badge">${a.albums} альбомов</span>` : ''}</div>
        ${a.description ? `<p class="hint" style="font-size:14px">${esc(a.description)}</p>` : ''}
        ${a.similar?.length ? `<div class="meta">Похожие: ${a.similar.map(x => x.id ? ymA(`/artist/${x.id}`, x.name) : esc(x.name)).join(', ')}</div>` : ''}
        <div><a class="btn sm" href="${YM}/artist/${esc(a.id)}" target="_blank" rel="noopener">Открыть в Яндекс Музыке ↗</a></div>
        ${a.popular?.length ? `<b style="margin-top:6px">Популярное</b><div class="tlist">${a.popular.map(t => trackRow(t)).join('')}</div>` : ''}
      </div></div>`;
  } catch (e) { $('#pl-artist').innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

// ----- управление (общие обработчики) -----
function musicAction(a, btn, noAnim) {
  const s = muState(), p = muPlayer();
  // «назад»/«вперёд» — с анимацией обложки (как свайп на телефоне), везде, где есть обложка
  if ((a === 'prev' || a === 'next') && !noAnim && btn && (a === 'next' ? p?.hasNext : p?.hasPrev)
      && !matchMedia('(prefers-reduced-motion: reduce)').matches)
    return plCoverSwap(a, () => musicAction(a, btn, true));
  if (a === 'play') {
    if (!muHas() && !s?.playing) return muCmd(btn, { command: 'sendText', text: 'Включи мою волну' }, 'Включаю «Мою волну»');
    // пауза и «играть» — на странице сразу, не дожидаясь колонки; не вышло — вернём как есть
    const want = !s?.playing;
    muOptimistic(want);
    return muCmd(null, { command: want ? 'play' : 'stop' }).then(r => { if (!r) { muWant = null; loadMusic(); } });
  }
  if (a === 'prev' || a === 'next') {
    if (plSwapping && !noAnim) return;   // идёт смена трека — ждём новый
    muPulse(btn); return muCmd(null, { command: a });
  }
  if (a === 'shuffle') return muCmd(btn, { command: 'shuffle', enable: !p?.entityInfo?.shuffled });
  if (a === 'repeat') return muCmd(btn, { command: 'repeat', mode: { None: 'All', All: 'One', One: 'None' }[p?.entityInfo?.repeatMode || 'None'] });
  if (!p?.id) return;
  if (a === 'like') return act(btn, async () => {
    const r = await api('/api/music/like', { id: p.id, like: !mu.track?.liked });
    if (mu.track) mu.track.liked = r.liked;
    renderMusic();
    toast(r.liked ? '♥ В «Мне нравится»' : 'Лайк убран');
  });
  if (a === 'dislike') return act(btn, () => api('/api/music/dislike', { id: p.id }), 'Дизлайк — дальше');
}
document.addEventListener('click', e => {
  const b = e.target.closest('[data-mu]');
  if (b) return musicAction(b.dataset.mu, b);
  const r = e.target.closest('[data-play]');
  if (r) act(r, () => api('/api/music/play', { id: r.dataset.play, type: 'track' }), 'Включаю');
});
document.addEventListener('input', e => {
  if (e.target.matches('input.mu-seek')) { mu.seeking = true; $$('.mu-pos').forEach(x => x.textContent = fmtDur(+e.target.value)); }
  if (e.target.matches('input.mu-vol')) { mu.volSet = Date.now(); $$('.mu-vol-v').forEach(x => x.textContent = e.target.value); unmuteUi(+e.target.value); }
});
document.addEventListener('change', e => {
  if (e.target.matches('input.mu-seek'))
    muCmd(null, { command: 'rewind', position: +e.target.value }).finally(() => setTimeout(() => mu.seeking = false, 800));
  if (e.target.matches('input.mu-vol')) { mu.volSet = Date.now(); act(null, () => api('/api/volume', { level: +e.target.value })); }
});
async function sendAlice(form, submitter) {
  const input = form.elements.text, text = input.value.trim();
  if (!online || !mu.info?.auth?.logged_in || !mu.info?.glagol?.connected || input.disabled || !text || !input.reportValidity()) return false;
  const result = await muCmd(submitter, { command: 'sendText', text }, 'Алиса: «' + text + '»');
  if (result?.status === 'timeout') { toast('Колонка не подтвердила команду. Текст сохранён.'); return false; }
  if (result && input.value.trim() === text) { input.value = ''; input.dispatchEvent(new Event('input')); }
  return !!result;
}
document.addEventListener('submit', e => {
  if (!e.target.matches('.mu-ask-form')) return;
  e.preventDefault();
  sendAlice(e.target, e.submitter);
});
$('#mu-logout').onclick = e => confirm('Выйти из аккаунта Яндекса? Токен будет удалён с этого компьютера.') &&
  act(e.currentTarget, async () => { await api('/api/auth/logout', {}); mu.qr = null; await loadMusic(); }, 'Вы вышли из аккаунта');

// вход по QR: показываем код и раз в 2 с спрашиваем, подтвердили ли вход на телефоне
async function startQr() {
  mu.qr = 'pending';
  $('#mu-qr').innerHTML = '<div class="empty">QR-код…</div>';
  $('#mu-qr-status').textContent = '';
  try {
    const [r, a] = await Promise.all([api('/api/auth/qr', {}), api('/api/auth')]);
    mu.qr = r.link;
    $('#mu-qr-link').href = r.link;   // телефон: та же ссылка, что в QR, — открываем её прямо здесь
    $('#mu-qr-link').removeAttribute('aria-disabled');
    $('#mu-oauth').href = a.oauth_url;
    const q = qrcode(0, 'M');
    q.addData(r.link);
    q.make();
    $('#mu-qr').innerHTML = q.createSvgTag({ cellSize: 4, margin: 0, scalable: true });
    $('#mu-qr-status').textContent = 'Ждём подтверждения на телефоне…';
    if (mu.qrTimer) Scheduler.remove(mu.qrTimer);
    mu.qrTimer = every(2000, pollQr);
  } catch (e) {
    mu.qr = 'failed';
    $('#mu-qr').innerHTML = '<div class="empty">QR недоступен</div>';
    $('#mu-qr-status').textContent = e.offline ? '' : e.message + ' — войдите через Яндекс ниже.';
    api('/api/auth').then(a => $('#mu-oauth').href = a.oauth_url).catch(() => {});
  }
}
function stopQr() { if (mu.qrTimer) Scheduler.remove(mu.qrTimer); mu.qrTimer = null; mu.qr = null; }
async function pollQr() {
  try {
    const r = await api('/api/auth/qr');
    if (r.state === 'done') { stopQr(); toast('Вход выполнен' + (r.login ? ': ' + r.login : '')); loadMusic(); }
    else if (r.state === 'expired' || r.state === 'none') {
      if (mu.qrTimer) Scheduler.remove(mu.qrTimer);
      mu.qrTimer = null;
      $('#mu-qr-link').setAttribute('aria-disabled', 'true');
      $('#mu-qr-status').innerHTML = (plMobile() ? 'Ссылка для входа устарела.' : 'QR-код устарел.') + ' <button class="btn sm" id="mu-qr-new">Новый код</button>';
      $('#mu-qr-new').onclick = () => startQr();
    }
  } catch (e) { if (!e.offline) $('#mu-qr-status').textContent = e.message; }
}
$('#mu-oauth-go').onclick = e => act(e.currentTarget, async () => {
  const r = await api('/api/auth/oauth', { url: $('#mu-oauth-url').value });
  $('#mu-oauth-url').value = '';
  stopQr();
  await loadMusic();
  return r;
}, 'Вход выполнен');

// ---------- ASCII-визуализатор за моделью ----------
// Звука у панели нет, но пока играет музыка, Quasar сам рисует на часах эквалайзер из 4 полос — его мы
// получаем потоком (liveLeds) и раскладываем по кругу. Без него — синтетика: лучи пульсируют в темпе.
// В покое (по желанию) — медленно переливающийся фон. Цвет — акцент темы.
(() => {
  const cv = $('#model-viz'), ctx = cv.getContext('2d');
  const RAMP = ' .·:-=+*%#@', FS = 13;
  let cw = 0, ch = 0, energy = 0, last = 0, color = '#E8590C';
  const sb = [0, 0, 0, 0];   // сглаженные полосы спектра
  const rings = [];          // кольца, пущенные по пикам (а не по таймеру)
  let barsH = [];            // высоты столбиков классического эквалайзера
  let lastRing = 0, bassAvg = 0;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const readColor = () => { color = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || color; };
  readColor();
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', readColor);
  window.addEventListener('themechange', readColor);
  new ResizeObserver(() => {
    const r = cv.getBoundingClientRect(), dpr = Math.min(devicePixelRatio, 2);
    cw = r.width; ch = r.height;
    cv.width = Math.round(cw * dpr); cv.height = Math.round(ch * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }).observe(cv);

  function frame(now) {
    requestAnimationFrame(frame);
    if (document.hidden || !cv.offsetParent || !cw || now - last < 33) return;   // ~30 к/с и только на экране
    last = now;
    if (window.__vizOff) { cv.style.display = 'none'; if (energy) { ctx.clearRect(0, 0, cw, ch); energy = 0; } return; } else { cv.style.display = 'block'; }
    const t = now / 1000;
    // фон в покое: пока музыка не играет, символы медленно переливаются (выключается в «Настроить»)
    const idle = window.__vizIdleOff ? 0 : 1 - energy, ti = reduced.matches ? 0 : t;
    const playing = !!mu.info?.glagol?.state?.playing;
    energy += ((playing ? 1 : 0.12) - energy) * 0.05;          // плавно разгоняется и затихает
    const beat = Math.exp(-((t * 2) % 1) * 5);                 // запасной пульс 120 уд/мин
    // Настоящий спектр с колонки, если он свежий: сглаживаем полосы, пульс — от общей громкости.
    const sp = window.__spectrum, live = !!sp && now - sp.t < 700;
    if (live) sp.bands.forEach((b, k) => { sb[k] += (b - sb[k]) * 0.7; }); else sb.forEach((_, k) => { sb[k] *= 0.9; });
    // Без живого спектра, но пока играет, рисуем синтетические полосы — чтобы стили не пустовали.
    const bands = live ? sb : [0, 1, 2, 3].map(k => 0.22 + 0.55 * Math.max(0, Math.sin(t * (1.1 + k * .8) + k * 2.1)) * (0.45 + 0.55 * beat));
    const loud = (bands[0] + bands[1] + bands[2] + bands[3]) / 4;
    const style = window.__vizStyle || 'rays';
    // Кольца пускаем по пикам баса (или по биту, если живого спектра нет), а не по таймеру:
    // так они совпадают с музыкой и читаются.
    bassAvg += (bands[0] - bassAvg) * (live ? 0.04 : 0.1);
    const onPeak = live ? bands[0] - bassAvg > 0.1 && bands[0] > 0.3 : beat > 0.85;
    const stale = live && now - lastRing > 1200 && loud > 0.15;   // давно не было пиков — всё равно пускаем
    if (energy > 0.4 && (onPeak || stale) && now - lastRing > 220) {
      rings.push({ t0: now, band: live ? bands[0] : 0.5 + 0.5 * beat });
      lastRing = now;
    }
    if (rings.length > 8) rings.splice(0, rings.length - 8);
    for (let i = rings.length - 1; i >= 0; i--) if (now - rings[i].t0 > 1400) rings.splice(i, 1);
    ctx.clearRect(0, 0, cw, ch);
    if (energy < 0.02 && idle < 0.02) return;
    ctx.font = `${FS}px 'JetBrains Mono', monospace`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = color;
    const cx = cw / 2, cy = ch / 2, colW = FS * 0.62, rowH = FS * 1.1, maxR = Math.hypot(cx, cy), minHalf = Math.min(cw, ch) / 2, idleR = Math.min(cw, ch) * 0.75;
    // «Полосы»: четыре прямоугольных столбика снизу вверх — по одному на полосу (полос у нас 4).
    if (style === 'bars') {
      if (barsH.length !== 4) barsH = [0, 0, 0, 0];
      for (let k = 0; k < 4; k++) {
        const target = playing ? 0.08 + 0.9 * bands[k] : 0;
        barsH[k] += (target - barsH[k]) * (playing ? 0.7 : 0.12);
      }
    }
    for (let y = rowH / 2; y < ch; y += rowH) {
      for (let x = colW / 2; x < cw; x += colW) {
        const dx = x - cx, dy = y - cy, dist = Math.hypot(dx, dy);
        const r = dist / maxR, rn = dist / minHalf, a = Math.atan2(dy, dx);
        // Лучи: справа низкие, слева высокие, зеркально сверху и снизу.
        const u = Math.abs(a) / Math.PI * 3, k = Math.min(2, Math.floor(u)), f = u - k;
        const band = bands[k] * (1 - f) + bands[k + 1] * f;
        // Спектр по кругу: один сектор 90°, отражённый по обеим осям, — получается симметричный веер.
        const qa = Math.min(Math.abs(a), Math.PI - Math.abs(a));
        const uq = qa / (Math.PI / 2) * 3, kq = Math.min(2, Math.floor(uq)), fq = uq - kq;
        const bandQuad = bands[kq] * (1 - fq) + bands[kq + 1] * fq;
        let v = 0;
        if (style === 'rings') {
          // Кольца живут в видимой зоне вокруг корпуса: появляются на 0.6 и идут до края,
          // яркость держится почти до конца — раньше гасли, не дойдя до края.
          for (const rg of rings) {
            const age = (now - rg.t0) / 1000;
            const rad = 0.6 + age * 0.35, wide = 0.02 + 0.08 * rg.band;
            const alpha = (0.55 + 0.45 * rg.band) * Math.max(0, Math.min(1, (1.06 - rad) / 0.12))
              * Math.min(1, age / 0.08);
            const d = Math.abs(rn - rad);
            if (d < wide) v = Math.max(v, Math.pow(1 - d / wide, 0.7) * alpha * energy);
          }
        } else if (style === 'orbit') {
          // Столбики наружу от центра (сектор 90° и зеркала). Внутренний радиус маленький:
          // начало прячется за корпусом, а внешний край уходит дальше и читается на просвет.
          const inner = 0.35, len = 0.12 + 0.62 * bandQuad;
          if (rn > inner && rn < inner + len) {
            const t = (rn - inner) / len;
            const endFade = Math.max(0, Math.min(1, (1.08 - rn) / 0.12));
            v = (0.25 + 0.75 * bandQuad) * (1 - 0.25 * t) * endFade * energy;
          }
          if (Math.abs(rn - inner) < 0.012) v = Math.max(v, 0.14 * energy * (0.4 + 0.6 * bandQuad));
        } else if (style === 'bars') {
          // Четыре прямоугольных столбика снизу вверх, по полосе на каждый, с промежутками.
          const slot = (x / cw) * 4, bi = Math.min(3, Math.floor(slot)), within = slot - bi;
          if (within > 0.08 && within < 0.92) {
            const h = barsH[bi] || 0;
            const t = y / ch;                         // 0 сверху, 1 снизу
            if (h > 0.01 && t > 1 - h) {
              const k2 = (t - (1 - h)) / h;           // 0 у вершины, 1 у основания
              v = 0.62 + 0.38 * (1 - k2);
            }
          }
        } else {
          // Лучи из центра: дальность зависит от полосы в этом направлении. Живой спектр был ярче —
          // для него и приглушение, и радиус меньше; фейковый оставляем заметным.
          const reach = (0.46 + 0.5 * band) * energy * (0.85 + 0.3 * loud) * (live ? 0.9 : 1);
          v = r < reach ? 1 - (r / reach) ** 2 : 0;
          v = Math.max(v, Math.max(0, Math.sin(r * 16 - t * 3.4)) ** 3 * 0.5 * energy * (1 - r));   // волны-кольца
        }
        if (idle) {
          const wave = 0.78 + 0.12 * Math.sin(a * 3 + ti * 0.8) + 0.07 * Math.sin(a * 7 - ti);
          // сплошное пятно из центра; крупнее модели, чтобы край выходил из-за корпуса
          v = Math.max(v, Math.max(0, 1 - (Math.hypot(dx, dy) / idleR / wave) ** 2) * 0.45 * idle);
        }
        if (v < 0.08) continue;
        ctx.globalAlpha = (live ? 0.7 : 1) * Math.min(0.8, v * 0.85);
        ctx.fillText(RAMP[Math.min(RAMP.length - 1, Math.floor(v * RAMP.length))], x, y);
      }
    }
    ctx.globalAlpha = 1;
  }
  requestAnimationFrame(frame);
})();

// ---------- yam 2: пакетный менеджер на колонке ----------
const y2 = { status: null, packages: [], catalog: null };
async function loadYam2(refresh) {
  let st;
  try { st = await api('/api/yam2/status' + (refresh ? '?refresh=1' : '')); }
  catch (e) { $('#y2-ver').textContent = e.message; return; }
  y2.status = st;
  const plat = ' · платформа ' + st.platform;
  $('#y2-badge').textContent = st.installed ? 'установлен' : 'не установлен';
  $('#y2-badge').className = 'badge' + (st.installed ? ' ok' : '');
  $('#y2-intro').hidden = st.installed;
  $('#y2-install').hidden = st.installed || !st.platform;
  $('#y2-update').hidden = !st.update;
  $('#y2-notes').hidden = !st.latest_url;
  if (st.latest_url) $('#y2-notes').href = st.latest_url;
  $('#y2-ver').textContent = !st.platform ? 'не удалось определить платформу колонки для yam'
    : !st.installed ? `последний релиз — ${st.latest || '—'}` + plat
    : st.error && !st.version ? st.error
    : !st.version ? `сборка не из последних релизов yam, последний — ${st.latest || '—'}` + plat
    : st.update ? `установлен ${st.version}, доступен ${st.latest}` + plat : `${st.version} — последний` + plat;
  $('#y2-ver').classList.toggle('warn', !!st.update);
  $('#y2-ready').hidden = !st.installed;
  $('#y2-on').hidden = !st.installed;
  const reg = (st.registries || []).find(r => r.name === 'official');
  $('#y2-reg').textContent = reg ? reg.url : 'не подключён — без него yam не найдёт пакетов';
  $('#y2-reg-add').hidden = !!reg;
  $('#y2-legacy-note').hidden = !st.legacy?.running;
  // старый yam — свёрнут, если стоит новый; раскрыт, если есть только старый
  const leg = $('#yam-legacy');
  if (!leg.dataset.touched) leg.open = !st.installed && !!st.legacy?.installed;
  $('#yam-legacy-state').textContent = st.legacy?.installed ? (st.legacy.running ? '— работает' : '— установлен, остановлен') : '— не установлен';
  if (!st.installed) return;
  await Promise.all([loadYam2Packages(), loadYam2Catalog()]);
}
async function loadYam2Packages() {
  try { y2.packages = (await api('/api/yam2/packages')).packages; }
  catch (e) { y2.packages = []; $('#y2-installed').innerHTML = `<p class="hint">${esc(e.message)}</p>`; return; }
  renderYam2();
}
async function loadYam2Catalog(refresh) {
  try { y2.catalog = await api('/api/yam2/catalog' + (refresh ? '?refresh=1' : '')); }
  catch (e) { y2.catalog = { error: e.message, packages: [] }; }
  renderYam2();
}
function renderYam2() {
  const inst = Object.fromEntries(y2.packages.map(p => [p.name, p]));
  const cat = Object.fromEntries((y2.catalog?.packages || []).map(p => [p.name, p]));
  const newer = p => cat[p.name] && vcmp(cat[p.name].latest, p.version) > 0 ? cat[p.name].latest : null;
  const updates = y2.packages.filter(newer).length;
  $('#y2-count').textContent = y2.packages.length ? `${y2.packages.length} шт.` + (updates ? ` · обновлений: ${updates}` : '') : '';
  $('#y2-installed').innerHTML = y2.packages.length ? y2.packages.map(p => `<div class="inst" data-name="${esc(p.name)}">
      <div class="who"><b>${esc(p.name)} <span class="mono meta">${esc(p.version)}</span>${newer(p) ? ` <span class="badge" style="background:var(--accent-soft);color:var(--accent-ink)">доступна ${esc(newer(p))}</span>` : ''}</b>
        <span class="meta">${esc(cat[p.name]?.summary || p.target)}</span></div>
      <div class="row" style="gap:6px">
        ${newer(p) ? `<button class="btn sm primary" data-a="install">Обновить</button>` : ''}
        <button class="btn sm icon danger" data-a="remove" aria-label="Удалить ${esc(p.name)}">${icon('i-trash', 14)}</button>
      </div>
      ${p.services.map(s => `<div class="svc" data-id="${esc(s.id)}">
        <span class="mono">${esc(s.name)} <span class="meta">${esc(s.exec.join(' '))}</span></span>
        <span class="badge${s.running ? ' ok' : ''}">${s.running ? 'работает' : 'остановлена'}</span>
        <label class="meta"><input type="checkbox" data-a="boot" ${s.boot ? 'checked' : ''}>При загрузке</label>
        ${s.running ? '<button class="btn sm" data-a="stop">Стоп</button>' : '<button class="btn sm primary" data-a="start">Запустить</button>'}
        <button class="btn sm" data-a="log">Лог</button>
        ${s.running && s.id.startsWith('yam~luna~') && y2.status?.host ? `<a class="btn sm" href="http://${esc(y2.status.host)}:8099/" target="_blank" rel="noopener">Веб-интерфейс ↗</a>` : ''}</div>`).join('')}
    </div>`).join('') : '<p class="hint">Пока ничего не установлено.</p>';
  $$('#y2-installed .inst > .row button').forEach(b => b.onclick = () => yam2Pkg(b, b.dataset.a, b.closest('.inst').dataset.name));
  $$('#y2-installed .svc [data-a]').forEach(b => {
    const id = b.closest('.svc').dataset.id;
    if (b.dataset.a === 'boot') b.onchange = () => yam2Svc(b, id, b.checked ? 'boot' : 'noboot');
    else b.onclick = () => b.dataset.a === 'log' ? yam2Log(b, id) : yam2Svc(b, id, b.dataset.a);
  });

  const c = y2.catalog;
  if (!c) return;
  if (c.error || !c.configured) {
    $('#y2-catalog').innerHTML = `<div class="empty">${esc(c.error || 'Подключите каталог пакетов выше')}</div>`;
    return;
  }
  const f = $('#y2-filter').value.toLowerCase(), all = $('#y2-all').checked;
  const list = c.packages.filter(p => (all || p.fits) && (!f || (p.name + ' ' + (p.summary || '')).toLowerCase().includes(f)));
  $('#y2-catalog').innerHTML = list.length ? list.map(p => {
    const have = inst[p.name];
    const action = !p.fits ? '' : !have ? 'Установить' : vcmp(p.latest, have.version) > 0 ? `Обновить с ${esc(have.version)}` : '';
    const sup = p.panel, nm = t => (sup && sup.names && sup.names[t]) || t;
    return `<div class="app${have ? ' installed' : ''}${p.fits ? '' : ' other'}">
      <div class="app-top"><b>${esc(p.name)}</b><span class="mono">${esc(p.latest)}</span></div>
      <p>${esc(p.summary)}</p>
      ${sup ? `<div class="app-panel">
        <span class="badge ok">Интеграция с панелью</span>
        <span class="meta">вкладка «${esc(sup.tab || 'Активация')}»</span>
        <span class="meta">Проверено: ${sup.tested.map(t => esc(nm(t))).join(', ')}</span>
        ${sup.untested.length ? `<span class="meta">Не тестировалось: ${sup.untested.map(t => esc(nm(t))).join(', ')}</span>` : ''}
        ${sup.unsupported.length ? `<span class="meta">Не поддерживается: ${sup.unsupported.map(t => esc(nm(t))).join(', ')}</span>` : ''}
        ${sup.untested.includes(c.platform) || sup.unsupported.includes(c.platform)
          ? `<span class="meta warn">Ваша станция (${esc(nm(c.platform))}) — ${sup.unsupported.includes(c.platform) ? 'не поддерживается' : 'не тестировалась'}; на свой риск</span>` : ''}
      </div>` : ''}
      ${p.fits ? '' : `<span class="meta">не для ${esc(c.platform)}: ${esc((p.targets || []).join(', '))}</span>`}
      <div class="row">${action ? `<button class="btn sm${have ? '' : ' primary'}" data-name="${esc(p.name)}">${action}</button>`
        : have ? '<span class="badge ok">установлено</span>' : ''}
        ${/^https:\/\//.test(p.homepage || '') ? `<a class="btn sm" href="${esc(p.homepage)}" target="_blank" rel="noopener">Сайт ↗</a>` : ''}</div>
    </div>`;
  }).join('') : `<div class="empty">${c.packages.length ? 'Ничего не найдено' : 'Каталог пуст'}</div>`;
  $$('#y2-catalog button[data-name]').forEach(b => b.onclick = () => yam2Pkg(b, 'install', b.dataset.name));
}
function y2out(text, bad) {
  const o = $('#y2-out');
  o.hidden = !text; o.textContent = text || ''; o.classList.toggle('warn', !!bad);
}
async function yam2Pkg(btn, action, name, yes) {
  if (action === 'remove' && !confirm(`Удалить пакет ${name}? Его службы будут остановлены.`)) return;
  const label = btn.textContent;
  if (action === 'install') btn.textContent = 'Ставлю…';
  const r = await act(btn, () => api('/api/yam2/pkg', { action, name, yes: !!yes }));
  btn.textContent = label;
  if (!r) return;
  if (r.need_yes) {
    y2out(r.text, true);
    if (confirm(`yam просит подтверждения для ${name}:\n\n${r.text}\n\nУстановить всё равно?`)) return yam2Pkg(btn, action, name, true);
    return;
  }
  toast(action === 'install' ? 'Установлен: ' + name : 'Удалён: ' + name);
  if (name.split('@')[0] === 'tangenx/wakeword' && window.wwLoad) await window.wwLoad();
  await loadYam2Packages();
}
async function yam2Svc(el, id, action, stopLegacy) {
  const r = await act(el, () => api('/api/yam2/service', { id, action, stop_legacy: !!stopLegacy }));
  if (r?.need_stop_legacy) {
    if (confirm('Работает старый yamd: он занимает порт 8099, нужный Luna. Остановить yamd и выключить его автозапуск, чтобы запустить Luna?'))
      return yam2Svc(el, id, action, true);
  } else if (r && action === 'start' && !r.running) {
    toast('Служба не запустилась — смотрите лог', true);
    yam2Log(null, id);
  } else if (r) toast({ start: 'Служба запущена', stop: 'Служба остановлена', boot: 'Будет запускаться при загрузке', noboot: 'Автозапуск выключен' }[action]);
  await loadYam2Packages();
  if (stopLegacy) loadYam();
  if (action === 'boot' || action === 'noboot') loadHook();
}
async function yam2Log(btn, id) {
  $('#y2-log-id').textContent = id.replaceAll('~', '/');
  const r = await act(btn, () => api('/api/yam2/log?id=' + encodeURIComponent(id)));
  if (r) { $('#y2-log').textContent = r.text || '(пусто)'; $('#y2-log').scrollTop = 1e9; mSheetShow(mSheetOf($('#y2-log'))); }
}
$('#y2-install').onclick = e => {
  const b = e.currentTarget;
  return act(b, async () => {
    b.textContent = 'Скачиваю и проверяю…';
    try { const r = await api('/api/yam2/install', {}); toast('yam ' + (r.installed_tag || '') + ' установлен'); }
    finally { b.textContent = 'Установить yam 2'; }
    await loadYam2();
  });
};
$('#y2-update').onclick = e => act(e.currentTarget, async () => { const r = await api('/api/yam2/update', {}); y2out(r.text); await loadYam2(); }, 'yam обновлён');
$('#y2-reg-add').onclick = e => act(e.currentTarget, async () => { await api('/api/yam2/registry', {}); await loadYam2(); }, 'Каталог подключён');
$('#y2-doctor').onclick = e => act(e.currentTarget, async () => {
  const r = await api('/api/yam2/doctor', {});
  y2out(r.text, !r.ok); $('#y2-repair').hidden = r.ok;
});
$('#y2-repair').onclick = e => act(e.currentTarget, async () => {
  const r = await api('/api/yam2/doctor', { repair: true });
  y2out(r.text, !r.ok); $('#y2-repair').hidden = r.ok;
});
$('#y2-uninstall').onclick = e => {
  if (!confirm('Удалить yam 2 вместе со всеми его пакетами и их данными (/data/yam)? Старый yamd это не затронет.')) return;
  act(e.currentTarget, async () => { await api('/api/yam2/uninstall', {}); y2out(''); await loadYam2(); loadHook(); }, 'yam 2 удалён');
};
$('#y2-filter').oninput = renderYam2;
$('#y2-all').onchange = renderYam2;
$('#y2-refresh').onclick = e => act(e.currentTarget, async () => { await loadYam2(true); await loadYam2Catalog(true); });
$('#yam-legacy > summary').addEventListener('click', () => { $('#yam-legacy').dataset.touched = 1; });
// ---------- приложения (yam) ----------
const yam = { registry: [], plugins: [] };
async function loadYam() {
  let st;
  try { st = await api('/api/yam/status'); } catch (e) { return e.offline || toast(e.message, true); }
  yam.status = st;
  $('#yam-missing').hidden = st.installed;
  $('#yam-card').hidden = !st.installed;
  if (!st.installed) { $('#yam-on').hidden = true; return; }
  $('#yam-badge').textContent = st.running ? 'yamd работает' : 'yamd остановлен';
  $('#yam-badge').className = 'badge' + (st.running ? ' ok' : ' bad');
  $('#yam-start').hidden = st.running;
  $('#yam-off').hidden = st.running;
  $('#yam-autostart').checked = st.autostart;
  $('#yam-on').hidden = !st.running;
  $('#yam-web').hidden = !(st.running && st.web_url);
  if (st.web_url) $('#yam-web').href = st.web_url;
  loadYamVersion();          // GitHub может отвечать долго — версию дорисуем отдельно
  if (!st.running) return;
  const [plugins, reg, settings] = await Promise.all([
    api('/api/yam/plugins').catch(e => (toast(e.message, true), [])),
    api('/api/yam/registry').catch(e => (toast('Реестр: ' + e.message, true), { packages: [] })),
    api('/api/yam/settings').catch(() => null),
  ]);
  yam.plugins = plugins; yam.registry = reg.packages || [];
  if (settings) $('#yam-binaries').checked = settings.allow_plugin_binaries;
  renderYam();
}
const yamCall = (btn, body, msg) => act(btn, async () => { await api('/api/yam/action', body); await loadYam(); }, msg);
// Сравнение версий вида 1.2.10 (и v-префикса): «1.2.10» новее «1.2.9», «1.0.0-beta» старше «1.0.0».
function vcmp(a, b) {
  const [ma, pa = ''] = String(a || '').replace(/^v/, '').split('-'), [mb, pb = ''] = String(b || '').replace(/^v/, '').split('-');
  const xa = ma.split('.'), xb = mb.split('.');
  for (let i = 0; i < Math.max(xa.length, xb.length); i++) {
    const d = (+xa[i] || 0) - (+xb[i] || 0);
    if (d) return Math.sign(d);
  }
  if (pa === pb) return 0;
  return !pa ? 1 : !pb ? -1 : Math.sign(pa.localeCompare(pb));   // финальная версия новее предварительной
}
// Самая новая версия каждого пакета в реестре
const yamLatest = () => yam.registry.reduce((m, p) => (!m[p.id] || vcmp(p.version, m[p.id].version) > 0 ? { ...m, [p.id]: p } : m), {});
async function loadYamVersion(refresh) {
  $('#yam-ver').textContent = 'проверяем…';
  $('#yam-update').hidden = $('#yam-notes').hidden = true;
  let v;
  try { v = await api('/api/yam/version' + (refresh ? '?refresh=1' : '')); } catch (e) { $('#yam-ver').textContent = e.message; return; }
  const date = d => d ? ' от ' + new Date(d).toLocaleDateString('ru-RU') : '';
  $('#yam-ver').textContent = v.error ? v.error
    : !v.installed ? `сборка не из релизов yam (sha256 ${v.sha}…), последний релиз — ${v.latest || '—'}`
    : v.update ? `установлена ${v.installed}${date(v.installed_date)}, доступна ${v.latest}${date(v.latest_date)}`
    : `${v.installed} — последняя`;
  $('#yam-ver').classList.toggle('warn', !!v.update);
  $('#yam-update').hidden = !v.update;
  if (v.latest_url) { $('#yam-notes').href = v.latest_url; $('#yam-notes').hidden = false; }
}
function renderYam() {
  const inst = Object.fromEntries(yam.plugins.map(p => [p.manifest.id, p]));
  const latest = yamLatest();
  const newer = m => latest[m.id] && vcmp(latest[m.id].version, m.version) > 0 ? latest[m.id] : null;
  const updates = yam.plugins.filter(p => newer(p.manifest)).length;
  $('#yam-count').textContent = yam.plugins.length ? `${yam.plugins.length} шт.` + (updates ? ` · обновлений: ${updates}` : '') : '';
  $('#yam-installed').innerHTML = yam.plugins.length ? yam.plugins.map(p => {
    const m = p.manifest, kind = `${m.launch?.kind || 'lua'} · ${m.launch?.mode || 'oneshot'}`;
    return `<div class="inst" data-id="${esc(m.id)}">
      <div class="who"><b>${esc(m.name || m.id)} <span class="mono meta">${esc(m.version)}</span>${newer(m) ? ` <span class="badge" style="background:var(--accent-soft);color:var(--accent-ink)">доступна ${esc(newer(m).version)}</span>` : ''}</b>
        <span class="meta">${esc(kind)}${p.compatible ? '' : ' · <span style="color:var(--danger)">' + esc(p.incompatibility || 'несовместим') + '</span>'}</span></div>
      <span class="badge${p.running ? ' ok' : ''}">${p.running ? 'работает' : p.enabled ? 'включён' : 'выключен'}</span>
      <div class="row" style="gap:6px">
        ${newer(m) ? `<button class="btn sm primary" data-a="update" data-v="${esc(newer(m).version)}">Обновить</button>` : ''}
        <button class="btn sm" data-a="toggle">${p.enabled ? 'Выключить' : 'Включить'}</button>
        ${p.running ? '<button class="btn sm" data-a="stop">Стоп</button>' : `<button class="btn sm" data-a="run" ${p.enabled ? '' : 'disabled title="Сначала включите"'}>Запустить</button>`}
        <button class="btn sm" data-a="logs">Лог</button>
        <button class="btn sm icon danger" data-a="remove" aria-label="Удалить ${esc(m.id)}">${icon('i-trash', 14)}</button>
      </div></div>`;
  }).join('') : '<p class="hint">Пока ничего не установлено.</p>';
  const showLog = async (b, id) => {
    $('#yam-log-id').textContent = id;
    const r = await act(b, () => api('/api/yam/logs?id=' + encodeURIComponent(id)));
    if (r) { $('#yam-log').textContent = r.text || '(пусто)'; $('#yam-log').scrollTop = 1e9; mSheetShow(mSheetOf($('#yam-log'))); }
  };
  $$('#yam-installed .inst button').forEach(b => b.onclick = async () => {
    const id = b.closest('.inst').dataset.id, a = b.dataset.a;
    if (a === 'logs') return showLog(b, id);
    if (a === 'update') {
      return act(b, async () => { b.textContent = 'Скачиваю…'; await api('/api/yam/install', { id, version: b.dataset.v }); await loadYam(); },
                 `Обновлён: ${id} → ${b.dataset.v}`);
    }
    if (a === 'remove' && !confirm(`Удалить пакет ${id}?`)) return;
    await yamCall(b, { action: a, id }, { toggle: 'Готово', run: 'Запущен: ' + id, stop: 'Остановлен: ' + id, remove: 'Удалён: ' + id }[a]);
    // разовые пакеты (fastfetch и т. п.) выводят результат в лог — показываем его сразу
    if (a === 'run' && (inst[id]?.manifest.launch?.mode || 'oneshot') === 'oneshot') {
      await new Promise(r => setTimeout(r, 1500));
      await showLog(null, id);
      if (!plMobile()) $('#yam-log').scrollIntoView({ behavior: 'smooth', block: 'nearest' });   // на телефоне лог — в листе
    }
  });

  const f = $('#yam-filter').value.toLowerCase();
  const list = Object.values(latest).filter(p => !f || (p.id + ' ' + p.name + ' ' + p.description).toLowerCase().includes(f));
  $('#yam-catalog').innerHTML = list.length ? list.map(p => {
    const have = inst[p.id];
    const action = !have ? 'Установить' : vcmp(p.version, have.manifest.version) > 0 ? `Обновить с ${esc(have.manifest.version)}` : '';
    return `<div class="app${have ? ' installed' : ''}">
      <div class="app-top"><b>${esc(p.name)}</b><span class="mono">${esc(p.version)}</span></div>
      <p>${esc(p.description)}</p>
      <div class="row">${action ? `<button class="btn sm${have ? '' : ' primary'}" data-id="${esc(p.id)}" data-v="${esc(p.version)}">${action}</button>` : '<span class="badge ok">установлено</span>'}</div>
    </div>`;
  }).join('') : '<div class="empty">Ничего не найдено</div>';
  $$('#yam-catalog button[data-id]').forEach(b => b.onclick = () => act(b, async () => {
    b.textContent = 'Скачиваю…';
    await api('/api/yam/install', { id: b.dataset.id, version: b.dataset.v });
    await loadYam();
  }, 'Установлен: ' + b.dataset.id));
}
$('#yam-filter').oninput = renderYam;
$('#yam-refresh').onclick = e => act(e.currentTarget, async () => { await api('/api/yam/registry?refresh=1'); await loadYam(); loadYamVersion(true); });
$('#yam-autostart').onchange = e => act(e.target, async () => {
  const st = await api('/api/yam/autostart', { enabled: e.target.checked });
  e.target.checked = st.autostart;
}, e.target.checked ? 'yamd будет запускаться при загрузке' : 'Автозапуск yamd выключен');
$('#yam-install').onclick = e => {
  const btn = e.currentTarget;
  act(btn, async () => {
    btn.textContent = 'Скачиваю и устанавливаю…';
    try { const r = await api('/api/yam/install-daemon', {}); toast('yam ' + (r.version || '') + ' установлен и запущен'); }
    finally { btn.textContent = 'Установить yam'; }
    await loadYam();
  });
};
$('#yam-update').onclick = () => {
  // Станция 2 и Мини 3 Про: перед заменой — резервная копия, прошлые копии при этом удаляются
  const b = yam.status?.backups;
  $('#yam-update-text').textContent = 'Обновить yamd? Пакеты временно остановятся и запустятся снова, пакеты и настройки сохранятся.'
    + (!b ? '' : ' Перед заменой будет создана резервная копия.'
      + (b.count ? ` Старые резервные копии (${b.count} шт., ${(b.kb / 1024).toFixed(1)} МБ) будут удалены.` : ''));
  $('#yam-update-confirm').hidden = false;
};
$('#yam-update-cancel').onclick = () => { $('#yam-update-confirm').hidden = true; };
$('#yam-update-confirm-btn').onclick = e => {
  const btn = e.currentTarget, result = $('#yam-update-result');
  act(btn, async () => {
    $('#yam-update').disabled = true;
    $('#yam-update-cancel').disabled = true;
    result.hidden = false; result.classList.remove('warn');
    result.textContent = 'Скачиваю и проверяю yamd. Не закрывайте страницу…';
    try {
      const r = await api('/api/yam/install-daemon', { drop_backups: !!yam.status?.backups?.count });
      $('#yam-update-confirm').hidden = true;
      result.textContent = 'yamd обновлён до ' + (r.version || 'последней версии') + '. Пакеты и настройки сохранены.';
      toast(result.textContent);
      await loadYam();
    } catch (error) {
      result.textContent = 'Не удалось обновить yamd: ' + error.message;
      result.classList.add('warn');
      throw error;
    } finally {
      $('#yam-update').disabled = false;
      $('#yam-update-cancel').disabled = false;
    }
  });
};
$('#yam-start').onclick = e => act(e.currentTarget, async () => { await api('/api/yam/start', {}); await loadYam(); }, 'yamd запущен');
$('#yam-binaries').onchange = e => act(e.target, async () => {
  const r = await api('/api/yam/settings', { allow_plugin_binaries: e.target.checked });
  e.target.checked = r.allow_plugin_binaries;
}, e.target.checked ? 'Бинарные пакеты разрешены' : 'Бинарные пакеты запрещены');

// ---------- файлы ----------
const fs = { cwd: '/', entries: [], writable: false, sort: 'name', dir: 1, sel: null, loaded: false };
const AUDIO = /\.(wav|mp3|opus|ogg|oga|flac|m4a|aac)$/i;
const join = (dir, name) => (dir === '/' ? '' : dir) + '/' + name;
const parent = p => p.replace(/\/[^/]*$/, '') || '/';

function fmtSize(n) {
  if (n < 1024) return n + ' Б';
  const u = ['КБ', 'МБ', 'ГБ']; let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return n.toFixed(n < 10 ? 1 : 0) + ' ' + u[i];
}
function fmtTime(t) {
  if (!t) return '—';
  const d = new Date(t * 1000), p = x => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function fsOpen(path, keepSel = false) {
  try {
    const r = await api('/api/fs/list?path=' + encodeURIComponent(path));
    Object.assign(fs, { cwd: r.path, entries: r.entries, writable: r.writable, loaded: true });
    if (!keepSel) fs.sel = null;
    store.set('fs.cwd', r.path);
    $('#fs-filter').value = keepSel ? $('#fs-filter').value : '';
    renderCrumbs(); renderFs();
  } catch (e) {
    toast(e.message, true);
    if (!fs.loaded && path !== '/') fsOpen('/');
  }
}

function renderCrumbs() {
  const parts = fs.cwd.split('/').filter(Boolean);
  let acc = '';
  $('#fs-crumbs').innerHTML = '<a data-p="/">/</a>' + parts.map((p, i) => {
    acc += '/' + p;
    return (i ? '<span>/</span>' : '') + `<a data-p="${esc(acc)}">${esc(p)}</a>`;
  }).join('');
  $$('#fs-crumbs a').forEach(a => a.onclick = () => fsOpen(a.dataset.p));
  const rw = $('#fs-rw');
  rw.textContent = fs.writable ? 'запись' : 'только чтение';
  rw.className = 'badge' + (fs.writable ? ' ok' : '');
  ['#fs-upload', '#fs-mkdir', '#fs-newfile'].forEach(s => $(s).disabled = !fs.writable);
}

function renderFs() {
  const f = $('#fs-filter').value.toLowerCase(), hidden = $('#fs-hidden').checked;
  const list = fs.entries
    .filter(e => (hidden || !e.name.startsWith('.')) && (!f || e.name.toLowerCase().includes(f)))
    .sort((a, b) => (b.is_dir - a.is_dir) || fs.dir * (fs.sort === 'name'
      ? a.name.localeCompare(b.name, 'ru', { numeric: true }) : a[fs.sort] - b[fs.sort]));
  $$('table.fs th[data-sort]').forEach(th => {
    th.textContent = th.textContent.replace(/ [▲▼]$/, '') + (th.dataset.sort === fs.sort ? (fs.dir > 0 ? ' ▲' : ' ▼') : '');
  });
  const body = $('#fs-body');
  if (!list.length) {
    setListHtml(body, `<tr><td colspan="5"><div class="empty">${fs.entries.length ? 'Ничего не найдено' : 'Пустая папка'}</div></td></tr>`);
    return;
  }
  const html = list.map(e => {
    const ico = e.is_dir ? 'i-folder' : e.type === 'link' ? 'i-link' : AUDIO.test(e.name) ? 'i-music' : 'i-file';
    return `<tr data-name="${esc(e.name)}" class="${e.is_dir ? 'dir' : ''} ${fs.sel === e.name ? 'sel' : ''}">
      <td class="name"><span class="n">${icon(ico, 18)}${esc(e.name)}</span>${e.target ? ` <span class="tgt">→ ${esc(e.target)}</span>` : ''}</td>
      <td class="num">${e.is_dir ? '' : fmtSize(e.size)}</td>
      <td class="dim hide-sm">${fmtTime(e.mtime)}</td>
      <td class="dim hide-sm">${esc(e.mode)}</td>
      <td class="act">
        <button class="btn sm icon ghost" data-a="dl" aria-label="Скачать${e.is_dir ? ' архивом' : ''}" title="Скачать${e.is_dir ? ' (.tar.gz)' : ''}">${icon('i-dl')}</button>
        ${fs.writable ? `<button class="btn sm icon ghost" data-a="mv" aria-label="Переименовать" title="Переименовать">${icon('i-edit')}</button>
        <button class="btn sm icon ghost danger" data-a="rm" aria-label="Удалить" title="Удалить">${icon('i-trash')}</button>` : ''}
      </td></tr>`;
  }).join('');
  if (!setListHtml(body, html)) return;
  $$('#fs-body tr[data-name]').forEach(tr => {
    const e = fs.entries.find(x => x.name === tr.dataset.name);
    tr.onclick = ev => {
      const a = ev.target.closest('button')?.dataset.a;
      if (a) return fsAction(a, e);
      if (e.is_dir) return fsOpen(join(fs.cwd, e.name));
      fs.sel = e.name; renderFs(); fsPreview(e);
    };
  });
}

async function fsAction(a, e) {
  const p = join(fs.cwd, e.name);
  if (a === 'dl') {
    toast(e.is_dir ? 'Упаковка папки…' : 'Скачивание…');
    const link = document.createElement('a');
    link.href = PanelContext.url('/api/fs/download?path=' + encodeURIComponent(p));
    link.download = ''; document.body.append(link); link.click(); link.remove();
  } else if (a === 'mv') {
    const name = prompt('Новое имя', e.name);
    if (!name || name === e.name) return;
    if (await act(null, () => api('/api/fs/rename', { path: p, name }), 'Переименовано')) {
      if (fs.sel === e.name) fs.sel = name;
      fsOpen(fs.cwd, true);
    }
  } else if (a === 'rm') {
    if (!confirm(`Удалить ${e.is_dir ? 'папку со всем содержимым' : 'файл'}\n${p}?`)) return;
    if (await act(null, () => api('/api/fs/delete', { path: p }), 'Удалено')) {
      if (fs.sel === e.name) clearPreview();
      fsOpen(fs.cwd, true);
    }
  }
}

function clearPreview() {
  fs.sel = null;
  $('#fs-preview').innerHTML = '<h2>Просмотр</h2><div class="empty">Выберите файл</div>';
}

async function fsPreview(e, isNew = false) {
  const p = join(fs.cwd, e.name), pv = $('#fs-preview');
  pv.innerHTML = `<h2>Просмотр</h2><div class="pv-name">${esc(p)}</div>
    <div class="row" id="pv-actions" style="gap:8px"></div><div id="pv-body"><div class="empty">Загрузка…</div></div>`;
  const actions = $('#pv-actions');
  const addBtn = (html, fn, cls = '') => {
    const b = document.createElement('button'); b.innerHTML = html; b.className = 'btn sm ' + cls;
    b.onclick = () => fn(b); actions.append(b); return b;
  };
  if (!isNew) addBtn(icon('i-dl', 14) + 'Скачать', () => fsAction('dl', e));
  if (AUDIO.test(e.name)) {
    addBtn(icon('i-play', 14) + 'Играть на колонке', b => act(b, () => api('/api/fs/play', { path: p }), '▶ ' + e.name), 'primary');
    addBtn(icon('i-stop', 14) + 'Стоп', b => act(b, () => api('/api/stop', {})), 'danger-soft');
    $('#pv-body').innerHTML = `<p class="hint">${fmtSize(e.size)} · ${fmtTime(e.mtime)}</p>`;
    return;
  }
  let r = { text: '', binary: false, truncated: false };
  if (!isNew) {
    try { r = await api('/api/fs/read?path=' + encodeURIComponent(p)); }
    catch (err) { $('#pv-body').innerHTML = `<div class="empty">${esc(err.message)}</div>`; return; }
    if (fs.sel !== e.name) return; // пока грузили, выбрали другой файл
  }
  const body = $('#pv-body');
  if (r.binary) {
    body.innerHTML = `<p class="hint" style="margin-bottom:8px">Бинарный файл, ${fmtSize(e.size)}. Первые 4 КБ:</p><pre class="out"></pre>`;
    body.querySelector('pre').textContent = r.text;
    return;
  }
  const editable = fs.writable && !r.truncated;
  body.innerHTML = `<textarea spellcheck="false" aria-label="Содержимое файла" ${editable ? '' : 'readonly'}></textarea>
    ${r.truncated ? '<p class="hint">Показаны первые 512 КБ — редактирование отключено.</p>' : ''}
    ${!fs.writable ? '<p class="hint">Папка только для чтения.</p>' : ''}`;
  const ta = body.querySelector('textarea');
  ta.value = r.text;
  if (editable) {
    const save = addBtn('Сохранить', b => act(b, async () => {
      await api('/api/fs/write', { path: p, text: ta.value });
      save.textContent = 'Сохранить'; isNew && fsOpen(fs.cwd, true); isNew = false;
    }, 'Сохранено'), 'primary');
    ta.oninput = () => save.textContent = 'Сохранить •';
    ta.onkeydown = ev => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key === 's') { ev.preventDefault(); save.click(); }
      if (ev.key === 'Tab') { ev.preventDefault(); ta.setRangeText('\t', ta.selectionStart, ta.selectionEnd, 'end'); }
    };
  }
}

async function fsUpload(files) {
  if (!fs.writable) return toast('Папка только для чтения', true);
  for (const f of files) {
    toast(`Загрузка ${f.name} (${fmtSize(f.size)})…`);
    const ok = await act(null, () => api('/api/fs/upload', f, {
      raw: true, headers: { 'Content-Type': 'application/octet-stream',
        'X-Filename': encodeURIComponent(f.name), 'X-Dir': encodeURIComponent(fs.cwd) },
    }), 'Загружено: ' + f.name);
    if (!ok) break;
  }
  fsOpen(fs.cwd, true);
}

$('#fs-up').onclick = () => fsOpen(parent(fs.cwd));
$('#fs-refresh').onclick = () => fsOpen(fs.cwd, true);
$('#fs-bookmarks').onchange = e => { if (e.target.value) fsOpen(e.target.value); e.target.value = ''; };
$('#fs-filter').oninput = renderFs;
$('#fs-hidden').onchange = renderFs;
$$('table.fs th[data-sort]').forEach(th => th.onclick = () => {
  fs.dir = fs.sort === th.dataset.sort ? -fs.dir : 1; fs.sort = th.dataset.sort; renderFs();
});
$('#fs-upload').onclick = () => $('#fs-file').click();
$('#fs-file').onchange = e => { fsUpload([...e.target.files]); e.target.value = ''; };
$('#fs-mkdir').onclick = async () => {
  const name = prompt('Имя новой папки');
  if (name && await act(null, () => api('/api/fs/mkdir', { path: join(fs.cwd, name) }), 'Папка создана')) fsOpen(fs.cwd, true);
};
$('#fs-newfile').onclick = () => {
  const name = prompt('Имя нового файла');
  if (!name || name.includes('/')) return;
  fs.sel = name; renderFs();
  fsPreview({ name, size: 0, mtime: 0 }, true);
};
const wrap = $('#fs-wrap');
wrap.ondragover = e => { e.preventDefault(); wrap.classList.add('over'); };
wrap.ondragleave = e => { if (!wrap.contains(e.relatedTarget)) wrap.classList.remove('over'); };
wrap.ondrop = e => { e.preventDefault(); wrap.classList.remove('over'); fsUpload([...e.dataTransfer.files]); };

// ---------- настройка «Обзора»: виджеты можно убирать, возвращать и переставлять ----------
const LAYOUT_KEY = 'ov.layout', ZONES = ['wz-top', 'wz-left', 'wz-right'];
const widgetEls = () => $$('[data-widget]');
function readLayout() {
  const ids = el => [...el.children].filter(c => c.dataset.widget).map(c => c.dataset.widget);
  return { zones: Object.fromEntries(ZONES.map(z => [z, ids($('#' + z))])), hidden: ids($('#wz-hidden')) };
}
const DEFAULT_LAYOUT = { zones: {
  'wz-top': ['stats'],
  'wz-left': ['model', 'alice', 'say', 'bt', 'sounds'],
  'wz-right': ['music', 'volume', 'dsp', 'clock', 'light', 'device'],
}, hidden: [] };
const homeZone = id => ZONES.find(z => DEFAULT_LAYOUT.zones[z].includes(id)) || 'wz-right';
function applyLayout(l) {
  const byId = Object.fromEntries(widgetEls().map(w => [w.dataset.widget, w]));
  const put = (parent, id) => { if (byId[id]) { parent.append(byId[id]); delete byId[id]; } };
  ZONES.forEach(z => (l.zones?.[z] || []).forEach(id => put($('#' + z), id)));
  (l.hidden || []).forEach(id => put($('#wz-hidden'), id));
  // виджеты, которых не было в сохранённой раскладке (появились в новой версии панели), — на своё место
  Object.keys(byId).forEach(id => put($('#' + homeZone(id)), id));
  // Place the newly introduced Alice card beneath the model in existing saved layouts.
  if (!Object.values(l.zones || {}).flat().includes('alice') && !(l.hidden || []).includes('alice'))
    $('[data-widget="model"]').after($('[data-widget="alice"]'));
}
function saveLayout() {
  store.set(LAYOUT_KEY, JSON.stringify(readLayout()));
  renderHiddenList();
}
function renderHiddenList() {
  const hidden = [...$('#wz-hidden').children];
  $('#ov-hidden-list').innerHTML = hidden.length
    ? hidden.map(w => `<button class="chip" data-id="${esc(w.dataset.widget)}">+ ${esc(w.dataset.title)}</button>`).join('')
    : '<span class="meta">Все виджеты на месте.</span>';
  $$('#ov-hidden-list button').forEach(b => b.onclick = () => {
    $('#' + homeZone(b.dataset.id)).append($(`[data-widget="${b.dataset.id}"]`));
    saveLayout();
  });
}
function moveWidget(w, how) {
  const zone = w.parentElement;
  const sib = dir => { let s = w[dir]; while (s && !s.dataset.widget) s = s[dir]; return s; };
  if (how === 'up') { const s = sib('previousElementSibling'); if (s) zone.insertBefore(w, s); }
  else if (how === 'down') { const s = sib('nextElementSibling'); if (s) s.after(w); }
  else if (how === 'side') $(zone.id === 'wz-left' ? '#wz-right' : '#wz-left').append(w);
  else if (how === 'hide') $('#wz-hidden').append(w);
  saveLayout();
}
function widgetBar(w) {
  const b = document.createElement('div');
  b.className = 'wbar';
  b.innerHTML = `<span class="grip" title="Перетащить">⠿ ${esc(w.dataset.title)}</span>
    <button data-m="up" aria-label="Выше" title="Выше">↑</button><button data-m="down" aria-label="Ниже" title="Ниже">↓</button>
    <button data-m="side" aria-label="В другую колонку" title="В другую колонку">⇄</button>
    <button data-m="hide" aria-label="Убрать виджет" title="Убрать">✕</button>`;
  b.querySelector('.grip').addEventListener('pointerdown', () => { w.draggable = true; });
  b.addEventListener('click', e => { const m = e.target.closest('button')?.dataset.m; if (m) moveWidget(w, m); });
  return b;
}
function setEditing(on) {
  document.body.classList.toggle('editing', on);
  $('#ov-editbar').hidden = !on;
  $('#ov-edit').classList.toggle('primary', on);
  $('#ov-edit').setAttribute('aria-pressed', on);
  $('#ov-edit span').textContent = on ? 'Готово' : 'Настроить';
  widgetEls().forEach(w => { if (!w.querySelector(':scope > .wbar')) w.prepend(widgetBar(w)); w.draggable = false; });
  renderHiddenList();
}
$('#ov-edit').onclick = () => setEditing(!document.body.classList.contains('editing'));
$('#set-layout-edit').onclick = () => { location.hash = '#overview'; setTimeout(() => { setEditing(true); window.scrollTo({ top: 0 }); }, 0); };
$('#set-layout-reset').onclick = () => $('#ov-reset').click();
$('#ov-done').onclick = () => setEditing(false);
$('#ov-reset').onclick = () => {
  applyLayout(DEFAULT_LAYOUT); $$('#wz-hidden > [data-widget]').forEach(w => $('#' + homeZone(w.dataset.widget)).append(w)); saveLayout();
  store.set('ov.tiles', JSON.stringify(TILE_DEFAULT)); buildTiles(); fillTileSettings();   // и плитку «Состояние»
  toast('Раскладка по умолчанию');
};
// Перетаскивание: виджет переезжает прямо во время движения, место выбирается по середине соседей.
let dragW = null;
document.addEventListener('dragstart', e => {
  const w = e.target.closest?.('[data-widget]');
  if (!document.body.classList.contains('editing') || !w) return;
  dragW = w;
  w.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', w.dataset.widget);
});
document.addEventListener('dragend', () => {
  if (!dragW) return;
  dragW.classList.remove('dragging');
  dragW.draggable = false;
  dragW = null;
  saveLayout();
});
ZONES.forEach(z => $('#' + z).addEventListener('dragover', e => {
  if (!dragW) return;
  e.preventDefault();
  const zone = e.currentTarget;
  const before = [...zone.children].filter(c => c.dataset.widget && c !== dragW)
    .find(c => { const r = c.getBoundingClientRect(); return e.clientY < r.top + r.height / 2; });
  if (before) { if (dragW.nextElementSibling !== before) zone.insertBefore(dragW, before); }
  else if (zone.lastElementChild !== dragW) zone.append(dragW);
}));
applyLayout(DEFAULT_LAYOUT);
try { const saved = JSON.parse(store.get(LAYOUT_KEY)); if (saved) applyLayout(saved); } catch {}

// Оформление: тема, акцент, визуализатор (храним в браузере; применяется ещё в <head>).
function applyUI() {
  const root = document.documentElement, theme = store.get('ui.theme') || 'auto', accent = store.get('ui.accent') || '';
  if (theme === 'auto') delete root.dataset.theme; else root.dataset.theme = theme;
  for (const [k, v] of [['--accent', accent], ['--accent-ink', accent && `color-mix(in srgb, ${accent} 72%, var(--text))`],
                        ['--accent-soft', accent && `color-mix(in srgb, ${accent} 15%, var(--card))`]])
    v ? root.style.setProperty(k, v) : root.style.removeProperty(k);
  window.__vizOff = store.get('ui.viz') !== '1';
  window.__vizStyle = store.get('ui.vizStyle') || 'rays';
  $$('#set-theme button').forEach(b => b.classList.toggle('active', b.dataset.v === theme));
  const glow = store.get('ui.glow') || 'cursor';
  if (glow === 'off') delete root.dataset.glow; else root.dataset.glow = glow;
  $$('#set-glow button').forEach(b => b.classList.toggle('active', b.dataset.v === glow));
  $$('#set-accent .sw').forEach(b => b.classList.toggle('sel', b.dataset.v === accent));
  $('#set-viz').checked = !window.__vizOff;
  $('#set-viz-style').value = window.__vizStyle;
  window.__vizIdleOff = store.get('ui.vizIdle') === '0';
  $('#set-viz-idle').checked = !window.__vizIdleOff;
  privateOff = store.get('ui.private') === '0';
  $('#set-private').checked = !privateOff;
  $('#log-mask').checked = !privateOff;
  applyPrivate();
  if (logText) renderLog();
  $('#set-minibar').checked = store.get('ui.minibar') !== '0';
  updateMinibar();
  appBars();
  window.dispatchEvent(new Event('themechange'));
}
// приложение для Android: светлые или тёмные значки статус-бара под тему страницы
function appBars() {
  if (!window.AndroidPanel) return;
  const t = document.documentElement.dataset.theme;
  AndroidPanel.theme(t ? t === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches);
}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', appBars);
if (window.AndroidPanel) addEventListener('scroll', () => document.documentElement.classList.toggle('scrolled', scrollY > 4), { passive: true });
$$('#set-theme button').forEach(b => b.onclick = () => { store.set('ui.theme', b.dataset.v); applyUI(); });
$$('#set-glow button').forEach(b => b.onclick = () => { store.set('ui.glow', b.dataset.v); applyUI(); });
// «За указателем»: где мышь внутри блока — оттуда и светит
document.addEventListener('pointermove', e => {
  if (document.documentElement.dataset.glow !== 'cursor' || e.pointerType === 'touch') return;
  const c = e.target.closest?.('.card'); if (!c) return;
  const r = c.getBoundingClientRect();
  c.style.setProperty('--mx', (e.clientX - r.left) + 'px'); c.style.setProperty('--my', (e.clientY - r.top) + 'px');
}, { passive: true });
$$('#set-accent .sw').forEach(b => b.onclick = () => { store.set('ui.accent', b.dataset.v); applyUI(); });
$('#set-viz').onchange = e => { store.set('ui.viz', e.target.checked ? '1' : '0'); applyUI(); };
$('#set-viz-style').onchange = e => { store.set('ui.vizStyle', e.target.value); applyUI(); };
$('#set-viz-idle').onchange = e => { store.set('ui.vizIdle', e.target.checked ? '1' : '0'); applyUI(); };
// Живой спектр с колонки: программа читает захват её усилителя, панель получает полосы потоком светодиодов.
let vizLive = null;
function renderVizRow() {
  const box = $('#set-viz-live'), note = $('#viz-row small');
  box.disabled = !vizLive;
  box.checked = !!vizLive?.running;
  note.textContent = !vizLive ? 'Состояние недоступно: нет связи с колонкой'
    : !vizLive.installed ? 'Экспериментально: включите — панель поставит на колонку программу (2 МБ), которая слушает не микрофон, а сам усилитель. «Убрать всё» её удаляет'
    : vizLive.running ? 'Включён: спектр идёт с колонки живьём. Работает, пока панель на связи'
    : 'Выключен. Программа на колонке осталась — включите снова или уберите через «Убрать всё» в настройках';
}
async function loadViz() {
  try { vizLive = await api('/api/viz'); } catch { vizLive = null; }
  renderVizRow();
}
$('#set-viz-live').onchange = async e => {
  const want = e.target.checked;
  e.target.disabled = true;
  try {
    vizLive = await api('/api/viz', { on: want });
    if (want && !vizLive.running) throw new Error(vizLive.error || 'визуализатор не запустился');
    toast(want ? 'Живой спектр включён' : 'Живой спектр выключен');
  } catch (err) {
    e.target.checked = !want;
    toast(err.message, true);
  } finally {
    renderVizRow();
    if (vizLive) e.target.checked = !!vizLive.running;
  }
};
$('#set-private').onchange = e => { store.set('ui.private', e.target.checked ? '1' : '0'); applyUI(); };
$('#set-minibar').onchange = e => { store.set('ui.minibar', e.target.checked ? '1' : '0'); applyUI(); loadMusic(); };
applyUI();
loadViz();
buildTiles(); fillTileSettings();

// Доступ: пароль панели и установка как приложение.
let accessInfo = null, installEvt = null;
const isLocalHost = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
async function authPost(path, body) {
  const r = await PanelContext.fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
}
async function loadAccess() {
  try { accessInfo = await (await PanelContext.fetch('/api/panel-auth')).json(); } catch { return; }
  const a = accessInfo, st = $('#pw-state');
  st.textContent = a.enabled ? 'Задан. Вход запоминается в этом браузере на 90 дней'
    : isLocalHost ? 'Не задан. Пока панель открыта только на этом компьютере, это не страшно'
    : 'Не задан — любой в вашей сети может управлять колонкой и её файлами';
  st.classList.toggle('warn', !a.enabled && !isLocalHost);
  $('#pw-set').textContent = a.enabled ? 'Сменить' : 'Задать';
  $('#pw-off').hidden = $('#pw-logout').hidden = !a.enabled;
  $('#pw-cur').hidden = !a.enabled;
  const others = Math.max(0, (a.sessions || 0) - 1);
  $('#pw-others-row').hidden = !others;
  $('#pw-others-n').textContent = `Ещё ${others} — браузеры, где выполнен вход`;
  renderInstall();
}
function pwForm(mode) {
  const f = $('#pw-form');
  f.hidden = !mode; f.dataset.mode = mode || '';
  if (!mode) return;
  f.reset();
  const off = mode === 'off';
  $('#pw-cur').hidden = !accessInfo?.enabled;
  $('#pw-new').hidden = $('#pw-new2').hidden = off;
  $('#pw-save').textContent = off ? 'Убрать пароль' : 'Сохранить';
  $('#pw-save').classList.toggle('danger', off);
  (accessInfo?.enabled ? $('#pw-cur') : $('#pw-new')).focus();
}
$('#pw-set').onclick = () => pwForm('set');
$('#pw-off').onclick = () => pwForm('off');
$('#pw-cancel').onclick = () => pwForm(null);
$('#pw-form').onsubmit = e => {
  e.preventDefault();
  const off = $('#pw-form').dataset.mode === 'off', cur = $('#pw-cur').value, nw = $('#pw-new').value;
  if (!off && nw !== $('#pw-new2').value) return toast('Пароли не совпадают', true);
  act($('#pw-save'), async () => {
    if (off) await authPost('/api/panel-auth/disable', { current: cur });
    else await authPost('/api/panel-auth/password', { current: cur, password: nw });
    pwForm(null);
    await loadAccess();
  }, off ? 'Пароль убран' : 'Пароль сохранён, другие входы завершены');
};
$('#pw-logout').onclick = e => act(e.currentTarget, async () => { await authPost('/api/panel-auth/logout'); await connCheck(); });
$('#pw-others').onclick = e => act(e.currentTarget, async () => {
  const r = await authPost('/api/panel-auth/logout-others'); await loadAccess(); return r;
}, 'Другие входы завершены');

// Что панель поставила на колонку, и полный откат.
let footprint = null;
// Защищённое соединение: агент на колонке вместо голого adb (adb остаётся запасным путём и для установки)
async function loadAgent() {
  try { renderAgent(await api('/api/agent')); } catch (e) { if (!e.offline) $('#agent-state').textContent = e.message; }
}
function renderAgent(a) {
  $('#agent-state').textContent = !a.network ? 'Нужно подключение к колонке по сети (по IP-адресу), не по USB'
    : !a.installed ? 'Выключен — панель работает через adb'
    : a.active ? `Включён${a.version ? ', версия ' + a.version : ''}: команды, файлы и консоль идут по защищённому каналу`
                 + (a.outdated ? '. Есть новая версия' : '') + (a.boot ? '' : '. Автозапуск не подключён — после перезагрузки колонки его запустит панель')
    : a.paired ? 'Установлен, но не отвечает — пока работаем через adb' + (a.error ? ` (${a.error})` : '')
    : 'Установлен на колонке, но эта панель ещё не подключена';
  $('#agent-install').hidden = !a.network || (a.active && !a.outdated);
  $('#agent-install').textContent = !a.installed || !a.paired ? 'Включить' : a.outdated ? 'Обновить' : 'Перезапустить';
  $('#agent-remove').hidden = !a.installed;
}
$('#agent-install').onclick = e => act(e.currentTarget, async () => renderAgent(await api('/api/agent/install', {})), 'Агент включён');
$('#agent-remove').onclick = e => confirm('Выключить агента и удалить его с колонки? Панель продолжит работать через adb.')
  && act(e.currentTarget, async () => { renderAgent(await api('/api/agent/remove', {})); loadFootprint(); }, 'Агент выключен');

async function loadFootprint() {
  try { footprint = await api('/api/footprint'); } catch { return; }
  const f = footprint, kb = x => x >= 1024 ? (x / 1024).toFixed(1) + ' МБ' : x + ' КБ';
  $('#fp-list').innerHTML = f.items.map(i => `<div class="fp-item${i.on ? ' on' : ''}"><i></i><span>${esc(i.name)}`
    + `${i.on && (i.detail || i.size_kb) ? ` <small>${esc([i.detail, i.size_kb ? kb(i.size_kb) : ''].filter(Boolean).join(' · '))}</small>` : ''}`
    + `${i.on ? '' : ' <small>— нет</small>'}</span></div>`).join('');
  $('#fp-info').textContent = f.any ? `Всё лежит в ${f.dir} · ${kb(f.size_kb)}` + (f.yam_installed ? ' · yam не затрагивается, у него своё удаление' : '')
    : 'Сейчас на колонке ничего от панели нет';
  $('#fp-reset').disabled = !f.any;
  loadPrivacy();
}
// ---------- «Ассистент»: свой голосовой помощник на колонке ----------
const AS_STATE = { idle: ['Слушает', 'ok'], thinking: ['Думает…', ''], speaking: ['Отвечает', 'ok'] };
const AS_TTS_HINT = {
  alice: 'Ответ читает сама Алиса («Окей, повторяю: …»). Бесплатно и без ключа, но интонация ровная, без знаков препинания.',
  openai: 'Любой сервер с OpenAI-совместимым /audio/speech: OpenAI, Piper на компьютере через openedai-speech, голоса Edge через openai-edge-tts.',
};
let asTask = 0, asCfg = null;
function asPoll(on) {
  if (on) loadAssistant(true);
}
async function loadAssistant(fill) {
  if (!online || document.hidden || $('#tab-assistant').hidden) return;
  let st;
  try { st = await api('/api/assistant'); } catch (e) { return e.offline || toast(e.message, true); }
  $('#as-experimental').hidden = !st.experimental;
  if (st.model_name) $('#as-exp-model').textContent = st.model_name.replace(/^Станция/, 'Станции');
  $('#as-missing').hidden = st.installed;
  $('#as-on').hidden = !st.installed;
  $('#as-autostart').checked = st.autostart !== false;
  $('#as-autostart').disabled = !st.installed;
  $('#as-update').hidden = !st.outdated;
  $('#as-daemon-stop').disabled = !st.running;
  $('#as-restart').textContent = st.running ? 'Перезапустить' : 'Запустить';
  const s = st.state || {}, c = st.config || {};
  const [label, cls] = !st.installed ? ['Не установлен', ''] : !st.running ? ['Остановлен', 'bad']
    : !c.enabled ? ['Выключен', ''] : AS_STATE[s.state] || ['—', ''];
  $('#as-badge').textContent = label;
  $('#as-badge').className = 'badge' + (cls ? ' ' + cls : '');
  const err = s.config_error || s.error;
  $('#as-error').hidden = !err;
  $('#as-error').textContent = err ? 'Последняя ошибка: ' + err : '';
  setListHtml($('#as-hist'), (s.history || []).slice().reverse().map(h => `<div><b>${esc(h.question)}</b>`
    + (h.answer ? esc(h.answer) : `<span class="err">${esc(h.error || '')}</span>`)
    + `<small>${new Date(h.at * 1000).toLocaleTimeString()} · ${h.source === 'voice' ? 'голосом' : 'из панели'}`
    + `${h.ms ? ' · ' + (h.ms / 1000).toFixed(1) + ' с' : ''}</small></div>`).join(''));
  const until = s.mode_until ? new Date(s.mode_until * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
  $('#as-mode-state').textContent = s.mode ? 'включён' + (until ? ` · отключится в ${until}, если молчать` : '') : 'выключен';
  $('#as-mode-btn').textContent = s.mode ? 'Выключить' : 'Включить';
  $('#as-mode-btn').className = 'btn sm' + (s.mode ? '' : ' primary');
  $('#as-mode-btn').dataset.on = s.mode ? '1' : '';
  $('#as-mode-btn').disabled = !st.running || !c.enabled;
  if (s.mode && st.running && c.enabled) { $('#as-badge').textContent = 'Режим ассистента'; $('#as-badge').className = 'badge ok'; }
  if (fill && st.installed) asFill(c);
  Scheduler.setInterval(asTask, s.state && s.state !== 'idle' ? 1000 : 4000);
}
asTask = every(4000, loadAssistant);
function asFill(c) {
  asCfg = c;
  const llm = c.llm || {}, tts = c.tts || {};
  $('#as-enabled').checked = !!c.enabled;
  $('#as-led').checked = c.led?.enabled ?? true;
  $('#as-led-color').value = c.led?.color || '#ff8a00';
  $('#as-triggers').value = (c.triggers || ['спроси gpt', 'спроси джипити', 'спроси чат gpt', 'спроси нейросеть']).join(', ');
  const mode = c.mode || {};
  $('#as-mode-enter').value = (mode.enter || ['переключись на gpt', 'переключись на ассистента', 'включи ассистента']).join(', ');
  $('#as-mode-exit').value = (mode.exit || ['отключись от ассистента', 'выключи ассистента', 'верни алису', 'вернись к алисе']).join(', ');
  $('#as-mode-timeout').value = mode.timeout_min || 10;
  $('#as-mode-follow').checked = mode.follow_up ?? true;
  $('#as-url').value = llm.base_url || '';
  $('#as-key').value = '';
  $('#as-model').value = llm.model || '';
  $('#as-prompt').value = llm.system_prompt || 'Ты эрудированный собеседник и отвечаешь на любые вопросы. Отвечай по-русски и коротко: '
    + 'одно-три предложения разговорной речью. Ответ прочитают вслух, поэтому без списков, разметки, эмодзи, ссылок и формул.';
  $('#as-tokens').value = llm.max_tokens || 500;
  $('#as-history').value = llm.history ?? 6;
  $('#as-proxy').value = llm.proxy || '';
  if (llm.base_url && (c.keys || {})[asKeyId(llm.base_url)]) asModels(true);
  $('#as-tts').value = tts.provider || 'alice';
  $('#as-tts-url').value = tts.base_url || '';
  $('#as-tts-key').value = '';
  $('#as-tts-model').value = tts.model || 'tts-1';
  $('#as-tts-voice').value = tts.voice || '';
  $('#as-tts-speed').value = tts.speed || 1;
  asTtsView();
  asKeysView();
}
// Ключи хранятся по адресу API — как keyID в device/assistant
const asKeyId = url => String(url || '').trim().replace(/\/+$/, '').toLowerCase();
function asProvider(url) {
  const opt = [...$('#as-preset').options].find(o => o.value && asKeyId(o.value) === url);
  return opt ? opt.textContent : url.replace(/^https?:\/\//, '').split('/')[0];
}
function asKeysView() {
  const keys = asCfg?.keys || {};
  [['#as-url', '#as-key'], ['#as-tts-url', '#as-tts-key']].forEach(([u, k]) => {
    const has = keys[asKeyId($(u).value)];
    $(k).placeholder = has ? 'сохранён ' + has : 'не задан';
  });
  $('#as-keys').innerHTML = Object.entries(keys).map(([url, m]) =>
    `<span title="${esc(url)}">${esc(asProvider(url))} ${esc(m)}<button type="button" data-url="${esc(url)}" aria-label="Удалить ключ ${esc(asProvider(url))}">×</button></span>`).join('');
}
['#as-url', '#as-tts-url'].forEach(id => $(id).addEventListener('input', asKeysView));
$('#as-keys').onclick = e => {
  const url = e.target.closest('button')?.dataset.url;
  if (!url || !confirm(`Удалить с колонки ключ для ${asProvider(url)}?`)) return;
  act(null, async () => asFill((await api('/api/assistant', { delete_key: url })).config), 'Ключ удалён');
};
// Панель открыта по HTTP с другого устройства: ключ уйдёт по сети открытым текстом. Не запрещаем — в домашней сети
// это обычный случай, — но предупреждаем и даём ссылку на HTTPS, если он включён.
function asHttpWarning() {
  const plain = location.protocol === 'http:' && !['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
  const port = accessInfo?.ports?.https;
  const url = port && Object.assign(new URL(location.href), { protocol: 'https:', port: String(port) }).href;
  $$('.as-http').forEach(el => {
    if (el.matches('[data-tts]') && $('#as-tts').value !== 'openai') { el.hidden = true; return; }
    el.hidden = !plain;
    el.innerHTML = 'Панель открыта по HTTP: ключ уйдёт до компьютера с панелью открытым текстом. В своей домашней сети риск '
      + 'невелик, в чужой или общей — заметнее. ' + (url ? `Безопаснее <a href="${esc(url)}">открыть панель по HTTPS</a>.`
      : 'Безопаснее включить HTTPS («Настройки» → «Доступ»).');
  });
}
function asTtsView() {
  const v = $('#as-tts').value;
  $$('[data-tts]').forEach(el => el.hidden = v !== 'openai');
  $('#as-tts-hint').textContent = AS_TTS_HINT[v];
  asHttpWarning();
  // что при этом всё равно уходит Яндексу — зависит от способа озвучки
  $('#as-privacy').innerHTML = '<b>Обратите внимание!</b> ' + esc('Яндекс по-прежнему получает голосовые вопросы: «Алиса, спроси GPT, …» распознаёт её облако, '
    + 'звук и текст фразы уходят на серверы Яндекса, как обычный запрос к Алисе. '
    + (v === 'alice' ? 'Ответ модели тоже уходит Яндексу: Алиса озвучивает его запросом «Повтори за мной».'
      : 'Ответ модели Яндекс не получает — его озвучивает выбранный синтезатор.')
    + ' Вопросы из «Проверки» Яндекс не видит.' + (v === 'alice' ? ' Только ответ на них.' : ''));
}
$('#as-tts').onchange = asTtsView;
const asLed = () => act(null, async () => asFill((await api('/api/assistant', { led: { enabled: $('#as-led').checked, color: $('#as-led-color').value } })).config));
$('#as-led').onchange = asLed;
$('#as-led-color').onchange = asLed;
// Модели спрашивает сама колонка (её сеть и прокси); ключ — из поля или уже сохранённый
let asModelsReq = 0;
async function asModels(quiet) {
  const url = $('#as-url').value.trim();
  if (!url) return quiet || toast('Сначала укажите адрес API', true);
  const req = ++asModelsReq, hint = $('#as-models-hint');
  hint.textContent = 'Запрашиваю список моделей…';
  try {
    const r = await api('/api/assistant/models', { base_url: url, api_key: $('#as-key').value, proxy: $('#as-proxy').value.trim() });
    if (req !== asModelsReq) return;
    $('#as-models').innerHTML = r.models.map(m => `<option value="${esc(m)}">`).join('');
    hint.textContent = r.models.length ? `Доступно моделей: ${r.models.length}. Начните вводить название или нажмите на поле.` : 'API не вернул ни одной модели.';
  } catch (e) {
    if (req === asModelsReq) hint.textContent = 'Список не получен: ' + e.message;
  }
}
$('#as-models-btn').onclick = () => asModels();
['#as-url', '#as-key', '#as-proxy'].forEach(id => $(id).addEventListener('change', () => asModels(true)));
$('#as-update').onclick = e => act(e.currentTarget, async () => { await api('/api/assistant/install', {}); await loadAssistant(); }, 'Программа на колонке обновлена');
$('#as-preset').onchange = e => { if (e.target.value) { $('#as-url').value = e.target.value; asKeysView(); asModels(true); } e.target.value = ''; };
$('#as-install').onclick = e => act(e.currentTarget, async () => { await api('/api/assistant/install', {}); await loadAssistant(true); },
  'Помощник установлен. Укажите модель и включите его');
$('#as-mode-btn').onclick = e => {
  const on = !e.currentTarget.dataset.on;
  act(e.currentTarget, async () => { await api('/api/assistant/mode', { on }); setTimeout(loadAssistant, 400); },
    on ? 'Режим ассистента включён — колонка скажет об этом' : 'Режим ассистента выключен');
};
$('#as-mode-follow').onchange = e => act(null, async () => asFill((await api('/api/assistant', { mode: { follow_up: e.target.checked } })).config));
$('#as-restart').onclick = e => act(e.currentTarget, async () => { await api('/api/assistant/start', {}); await loadAssistant(); }, 'Перезапущен');
$('#as-daemon-stop').onclick = e => act(e.currentTarget, async () => { await api('/api/assistant/daemon/stop', {}); }, 'Демон остановлен')
  .then(() => loadAssistant());
$('#as-enabled').onchange = e => act(null, async () => { await api('/api/assistant', { enabled: e.target.checked }); await loadAssistant(); },
  e.target.checked ? 'Помощник отвечает на фразу' : 'Помощник выключен');
$('#as-autostart').onchange = e => act(null, async () => {
  await api('/api/assistant/autostart', { on: e.target.checked }); await loadAssistant();
}, e.target.checked ? 'Ассистент будет запускаться после перезагрузки' : 'Автозапуск ассистента выключен');
$('#as-save').onclick = e => act(e.currentTarget, async () => {
  const num = (id, d) => Number($(id).value) || d;
  const st = await api('/api/assistant', {
    triggers: $('#as-triggers').value.split(',').map(s => s.trim()).filter(Boolean),
    mode: { enter: $('#as-mode-enter').value.split(',').map(s => s.trim()).filter(Boolean),
      exit: $('#as-mode-exit').value.split(',').map(s => s.trim()).filter(Boolean),
      timeout_min: Math.max(1, Math.min(240, Number($('#as-mode-timeout').value) || 10)), follow_up: $('#as-mode-follow').checked },
    llm: { base_url: $('#as-url').value.trim(), api_key: $('#as-key').value, model: $('#as-model').value.trim(),
      system_prompt: $('#as-prompt').value.trim(), max_tokens: num('#as-tokens', 500), history: Math.max(0, Number($('#as-history').value) || 0),
      proxy: $('#as-proxy').value.trim() },
    tts: { provider: $('#as-tts').value, base_url: $('#as-tts-url').value.trim(), api_key: $('#as-tts-key').value,
      model: $('#as-tts-model').value.trim(), voice: $('#as-tts-voice').value.trim(), speed: num('#as-tts-speed', 1) },
  });
  asFill(st.config);
}, 'Настройки сохранены');
$('#as-ask').onclick = e => {
  const text = $('#as-question').value.trim();
  if (!text) return;
  act(e.currentTarget, async () => { await api('/api/assistant/ask', { text }); setTimeout(loadAssistant, 500); }, 'Вопрос отправлен — ответ прозвучит на колонке');
};
$('#as-stop').onclick = e => act(e.currentTarget, async () => { await api('/api/assistant/stop', {}); await loadAssistant(); });
$('#as-remove').onclick = e => confirm('Удалить помощника с колонки вместе с настройками и ключами?') &&
  act(e.currentTarget, async () => { await api('/api/assistant/remove', {}); await loadAssistant(true); }, 'Помощник удалён');

// ---------- «Радио»: станции Алисы и свои потоки (играет программа панели на колонке) ----------
// Атрибуты кнопок — data-rd-*: data-play и data-mu уже заняты общими обработчиками плеера Алисы.
const RD_ALICE = ['Европа Плюс', 'Русское радио', 'Авторадио', 'Радио Рекорд', 'Наше радио', 'Ретро FM', 'DFM', 'Love Radio',
  'Хит FM', 'Радио Energy', 'Маяк', 'Дорожное радио', 'Радио Шансон', 'Relax FM', 'Монте-Карло', 'Business FM', 'Детское радио'];
let rd = { st: null, task: 0, checked: {} };
// Пометка у станции — только если с потоком беда; нормальные станции без лишних надписей.
function rdCheckText(c) {
  if (!c || c.state === 'ok') return '';
  const where = c.host ? ` Сервер ${c.host}${c.ip ? ' (' + c.ip + ')' : ''}.` : '';
  if (c.state === 'blocked') return `Поток ${c.detail} — похоже на блокировку провайдера.${where} Добавьте его в обход на роутере или укажите прокси ниже.`;
  if (c.state === 'slow') return `Поток идёт медленнее, чем играет (${c.detail.split(' — ')[0]}) — будет прерываться.`;
  return `Станция не работает: ${c.detail}.`;
}
const rdCheckNote = c => { const s = rdCheckText(c); return s ? `<span class="rd-warn" title="${esc(s)}" aria-label="${esc(s)}">⚠</span>` : ''; };
// Проверить станцию (с компьютера с панелью) и сказать, если с ней беда
async function rdCheck(url, quiet) {
  if (rd.checked[url]) return;
  rd.checked[url] = true;
  try {
    const c = await api('/api/radio/check', { url });
    const s = rdCheckText(c);
    if (s && !quiet) toast(s, true);
    await loadRadio();
  } catch {}
}
function rdPoll(on) {
  if (on) loadRadio();
}
async function loadRadio() {
  if (!online || document.hidden || $('#tab-radio').hidden) return;
  let st;
  try { st = await api('/api/radio'); } catch (e) { return e.offline || toast(e.message, true); }
  rd.st = st;
  // промах голосом: предлагаем привязать услышанное название к станции
  const miss = st.miss?.phrase && st.stations.length ? st.miss : null;
  $('#rd-miss').hidden = !miss;
  if (miss && rd.missShown !== miss.at) {
    rd.missShown = miss.at;
    $('#rd-miss-phrase').textContent = miss.phrase;
    $('#rd-miss-station').innerHTML = st.stations.map((s, i) => `<option value="${i}">${esc(s.name)}</option>`).join('');
  }
  if (JSON.stringify(st.alice) !== rd.alice) { rd.alice = JSON.stringify(st.alice); rdAliceView(st.alice || []); }
  $('#rd-missing').hidden = st.installed;
  $('#rd-unsupported').hidden = st.supported !== false;
  $('#rd-install').hidden = st.supported === false;
  $('#rd-own').hidden = !st.installed;
  $('#rd-own-off').hidden = !st.own_disabled;
  const now = st.now || {};
  $('#rd-now').hidden = !(now.playing || now.paused || now.retrying || now.error && now.name);
  // подробности проверки — под проигрывателем, где для них есть место
  const note = rdCheckText(st.checks?.[now.url]);
  $('#rd-now-note').hidden = !note || !now.url;
  $('#rd-now-note').textContent = note;
  $('#rd-now-name').textContent = now.name || '—';
  // «souphttpsrc0: Bad Request» → понятная причина: поток отклонил сервер станции, а не колонка
  const err = now.error && (/^souphttpsrc\d*:\s*(.*)$/.exec(now.error)?.[1] || now.error);
  const why = /missing a plug-in/i.test(now.error || '')
    ? 'Формат этой станции колонка не умеет (обычно это OGG Vorbis). Найдите у неё поток MP3 или AAC.'
    : /souphttpsrc/.test(now.error || '') ? `Станция не отдаёт поток (сервер ответил: ${err}). Попробуйте другой адрес потока.` : 'Ошибка: ' + err;
  $('#rd-now-title').textContent = err && !now.retrying ? why
    : now.retrying ? 'поток прервался — переподключаюсь…' : now.paused ? 'пауза, пока вы говорите с Алисой' : now.title || 'играет';
  $('#rd-count').textContent = st.stations.length ? `${st.stations.length}` : '';
  $('#rd-list').innerHTML = st.stations.length ? st.stations.map((s, i) => `<div class="rd-row${now.url === s.url && now.playing ? ' on' : ''}">
      <button class="btn sm icon" data-rd-play="${i}" aria-label="Слушать ${esc(s.name)}"><svg width="14" height="14" aria-hidden="true"><use href="#i-play"/></svg></button>
      <div class="rd-name"><b>${esc(s.name)}</b><small>${s.aliases?.length ? 'голосом: ' + s.aliases.map(esc).join(', ') + ' · ' : ''}${esc(s.url)}</small></div>${rdCheckNote(st.checks?.[s.url])}
      <button class="btn sm icon" data-rd-voice="${i}" aria-label="Как называть голосом ${esc(s.name)}" title="Как называть голосом">🗣</button>
      <button class="btn sm icon" data-rd-ren="${i}" aria-label="Переименовать ${esc(s.name)}" title="Переименовать">✎</button>
      <button class="btn sm icon" data-rd-del="${i}" aria-label="Удалить ${esc(s.name)}">×</button></div>`).join('')
    : '<div class="empty">Добавьте станцию ниже или найдите в каталоге.</div>';
  $('#rd-voice').checked = st.voice;
  if (document.activeElement !== $('#rd-proxy-form').elements.proxy) $('#rd-proxy-form').elements.proxy.value = st.proxy || '';
  // станция играет пару секунд и молчит — проверяем, не блокировка ли
  if (now.url && /почти не отдаёт|souphttpsrc/.test(now.error || '')) rdCheck(now.url, true);
  $('#rd-voice-hint').textContent = `— «Алиса, ${st.play[0]} <название>», «Алиса, ${st.stop[0]}»`;
  Scheduler.setInterval(rd.task, now.playing || now.paused || now.retrying ? 4000 : 10000);
}
rd.task = every(10000, loadRadio);
// Логотип станции — из каталога radio-browser.info (панель скачивает и хранит его сама); не нашёлся — первые буквы.
const rdKey = s => String(s || '').toLowerCase().replace(/ё/g, 'е').replace(/^радио\s+/, '').replace(/[^a-zа-я0-9]+/g, '');
function rdAliceView(learned = []) {
  const names = [...RD_ALICE, ...learned.map(s => s.title).filter(t => !RD_ALICE.some(n => rdKey(n) === rdKey(t)))];
  $('#rd-alice').innerHTML = names.map(n => {
    const initials = n.replace(/^радио\s+/i, '').split(/\s+/).map(w => w[0]).join('').slice(0, 2).toUpperCase();
    const logo = PanelContext.url('/api/radio/logo?name=' + encodeURIComponent(n));
    return `<button class="btn sm" data-alice="${esc(n)}"><span class="rd-logo" aria-hidden="true">${esc(initials)}</span>`
      + `<img src="${esc(logo)}" alt="" hidden>${esc(n)}</button>`;
  }).join('');
  // буквы видны, пока логотип грузится; загрузился — показываем его
  $$('#rd-alice img').forEach(img => img.addEventListener('load', () => { img.hidden = false; img.previousElementSibling.remove(); }, { once: true }));
}
rdAliceView();
const rdAlice = (btn, name) => act(btn, async () => { await api('/api/radio/alice', { name }); setTimeout(loadRadio, 800); }, `Алиса включает «${name}»`);
$('#rd-alice').onclick = e => { const b = e.target.closest('[data-alice]'); if (b) rdAlice(b, b.dataset.alice); };
$('#rd-alice-form').onsubmit = e => { e.preventDefault(); const n = e.target.elements.name.value.trim(); if (n) rdAlice(e.submitter, n); };
const rdPlay = (btn, s) => act(btn, async () => { await api('/api/radio/play', s); setTimeout(loadRadio, 1500); }, `Включаю «${s.name}»`);
const rdSave = (btn, stations, msg) => act(btn, async () => { rd.st = await api('/api/radio', { stations }); await loadRadio(); }, msg);
$('#rd-list').onclick = e => {
  const play = e.target.closest('[data-rd-play]'), del = e.target.closest('[data-rd-del]'), ren = e.target.closest('[data-rd-ren]');
  if (play) rdPlay(play, rd.st.stations[+play.dataset.rdPlay]);
  const voice = e.target.closest('[data-rd-voice]');
  if (voice) {
    const i = +voice.dataset.rdVoice, s = rd.st.stations[i];
    const v = prompt(`Как вы называете «${s.name}» голосом? Несколько вариантов — через запятую:`, (s.aliases || []).join(', '));
    if (v !== null) rdSave(voice, rd.st.stations.map((x, k) => k === i ? { ...x, aliases: v.split(',').map(a => a.trim()).filter(Boolean) } : x), 'Сохранено');
  }
  if (ren) {
    const i = +ren.dataset.rdRen, name = prompt('Название — как вы будете называть станцию голосом:', rd.st.stations[i].name)?.trim();
    if (name) rdSave(ren, rd.st.stations.map((s, k) => k === i ? { ...s, name } : s), 'Название изменено');
  }
  if (del) {
    const s = rd.st.stations[+del.dataset.rdDel];
    if (confirm(`Удалить станцию «${s.name}»?`)) rdSave(del, rd.st.stations.filter((_, i) => i !== +del.dataset.rdDel), 'Станция удалена');
  }
};
$('#rd-add').onsubmit = e => {
  e.preventDefault();
  const f = e.target.elements, s = { name: f.name.value.trim(), url: f.url.value.trim() };
  rdSave(e.submitter, [...(rd.st?.stations || []), s], 'Станция добавлена').then(() => { f.name.value = ''; f.url.value = ''; rdCheck(s.url); });
};
$('#rd-miss-bind').onclick = e => {
  const i = +$('#rd-miss-station').value, phrase = $('#rd-miss-phrase').textContent;
  act(e.currentTarget, async () => {
    await api('/api/radio', { stations: rd.st.stations.map((s, k) => k === i ? { ...s, aliases: [...new Set([...(s.aliases || []), phrase])] } : s) });
    await api('/api/radio/miss-seen', {});
    await loadRadio();
  }, `«${phrase}» теперь включает «${rd.st.stations[i].name}»`);
};
$('#rd-miss-skip').onclick = e => act(e.currentTarget, async () => { await api('/api/radio/miss-seen', {}); await loadRadio(); });
$('#rd-proxy-form').onsubmit = e => {
  e.preventDefault();
  act(e.submitter, async () => { await api('/api/radio', { proxy: e.target.elements.proxy.value.trim() }); rd.checked = {}; await loadRadio(); },
    e.target.elements.proxy.value.trim() ? 'Прокси сохранён — станции пойдут через него' : 'Прокси выключен');
};
$('#rd-stop').onclick = e => act(e.currentTarget, async () => { await api('/api/radio/stop', {}); setTimeout(loadRadio, 500); });
$('#rd-voice').onchange = e => act(null, async () => { await api('/api/radio', { voice: e.target.checked }); await loadRadio(); });
$('#rd-install').onclick = e => act(e.currentTarget, async () => { await api('/api/assistant/install', {}); await loadRadio(); }, 'Программа установлена');
let rdFound = [];
$('#rd-search').onsubmit = e => {
  e.preventDefault();
  const f = e.target.elements;
  act(e.submitter, async () => {
    const r = await api(`/api/radio/search?q=${encodeURIComponent(f.q.value.trim())}&country=${f.country.value}`);
    rdFound = r.stations;
    $('#rd-results').innerHTML = rdFound.length ? rdFound.map((s, i) => `<div class="rd-row">
        <button class="btn sm icon" data-rd-try="${i}" aria-label="Слушать ${esc(s.name)}"><svg width="14" height="14" aria-hidden="true"><use href="#i-play"/></svg></button>
        <div class="rd-name"><b>${esc(s.name)}</b><small>${[s.codec, s.bitrate ? s.bitrate + ' кбит/с' : '', s.country, s.tags].filter(Boolean).map(esc).join(' · ')}</small></div>
        <button class="btn sm" data-rd-add="${i}">Добавить</button></div>`).join('') : '<div class="empty">Ничего не нашлось.</div>';
  });
};
$('#rd-results').onclick = e => {
  const t = e.target.closest('[data-rd-try]'), a = e.target.closest('[data-rd-add]');
  if (t) { if (!rd.st?.installed) return toast('Сначала установите программу на колонку', true); rdPlay(t, rdFound[+t.dataset.rdTry]); }
  if (a) {
    if (!rd.st?.installed) return toast('Сначала установите программу на колонку', true);
    const s = rdFound[+a.dataset.rdAdd];
    if (rd.st.stations.some(x => x.url === s.url)) return toast('Эта станция уже в списке');
    rdSave(a, [...rd.st.stations, { name: s.name, url: s.url }], `«${s.name}» добавлена. Для голоса задайте название, как вы его произносите (✎)`)
      .then(() => rdCheck(s.url));
  }
};

async function loadPrivacy() {
  let p;
  try { p = await api('/api/privacy'); } catch (err) {
    $('#privacy-on').disabled = true;
    $('#privacy-state').textContent = 'Не удалось проверить hosts: ' + err.message;
    return;
  }
  if (Object.prototype.hasOwnProperty.call(p, 'backend')) {
    $('#privacy-on').checked = !!p.enabled;
    $('#privacy-purge').hidden = p.backend !== 'panel' || !(p.enabled && p.queued > 16);
    $('#privacy-on').disabled = !p.managed || !p.applied;
    if (!p.managed || !p.applied) {
      $('#privacy-state').textContent = p.error || 'Состояние hosts не подтверждено';
    } else if (p.backend === 'adbguard-v2') {
      $('#privacy-state').textContent = p.enabled
        ? 'Аналитика заблокирована через adbguard v2. OTA защищена независимо от тумблера.'
        : 'Блокировка аналитики выключена. Защита OTA остаётся включённой.';
    } else {
      $('#privacy-state').textContent = p.enabled
        ? 'Блокировка аналитики включена средствами WEB-панели.'
        : 'Блокировка аналитики WEB-панели выключена.';
    }
    return;
  }
  $('#privacy-on').checked = p.enabled;
  $('#privacy-purge').hidden = !(p.enabled && p.queued > 16);
  $('#privacy-on').disabled = false;
  const q = p.queued > 16 ? ` Сейчас в очереди ${(p.queued / 1024).toFixed(1)} КБ.` : '';
  $('#privacy-state').textContent = p.enabled
    ? (p.blocked ? `Включено: ${p.hosts.join(', ')} → 127.0.0.1 / ::1. Неотправленная аналитика копится на колонке `
        + `(не больше 1 МБ) и не уходит; она удаляется кнопкой и при выключении блокировки.${q}`
      : 'Включено, но адрес аналитики всё ещё доступен — попробуйте выключить и включить снова')
    : 'Блокировка выключена. Адреса аналитики не перенаправляются, системный раздел не меняется.';
}
$('#privacy-purge').onclick = e => {
  e.preventDefault();   // кнопка внутри <label> — не переключать блокировку
  act(e.currentTarget, async () => { await api('/api/privacy/purge', {}); await loadPrivacy(); }, 'Накопленная аналитика удалена');
};
$('#privacy-on').onchange = e => {
  const on = e.target.checked;
  e.target.disabled = true;
  api('/api/privacy', {enabled: on})
    .then(result => {
      if (result && result.backend === 'adbguard-v2' && (!result.applied || result.enabled !== on))
        throw new Error(result.error || 'adbguard не подтвердил переключение');
      toast(on ? 'Аналитика заблокирована' : 'Блокировка аналитики снята');
    })
    .catch(err => toast(err.message, true))
    .finally(() => { loadPrivacy(); loadFootprint(); });
};
$('#fp-reset').onclick = e => {
  if (!confirm('Удалить с колонки всё, что поставила панель?\n\n• своя заставка и звук запуска — вернутся стандартные;\n'
    + '• будильники и их звуки;\n• голосовой помощник и его ключи;\n• блокировка аналитики;\n• флаги автозапуска;\n• настройки автозапуска и копии оригиналов.\n\nКолонку перезагружать не нужно.')) return;
  act(e.currentTarget, async () => {
    footprint = await api('/api/footprint/reset', {});
    await loadFootprint();
    if (footprint?.note) toast(footprint.note, true);
    bootLoaded = false; alarmsData = null;
  }, 'С колонки удалено всё, что ставила панель');
};

// Резервная копия: файлы панели с колонки + настройки этого браузера (localStorage).
const UI_KEYS = /^(ui|ov|ch|snd)\./;
$('#bk-save').onclick = e => act(e.currentTarget, async () => {
  const ui = {};
  try { for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); if (UI_KEYS.test(k)) ui[k] = localStorage.getItem(PanelContext.storageKey(k)); } } catch {}
  const r = await PanelContext.fetch('/api/backup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ui }) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  const name = /filename="([^"]+)"/.exec(r.headers.get('Content-Disposition') || '')?.[1] || 'stantsiya-backup.tar.gz';
  await saveBlob(await r.blob(), name);
}, 'Копия скачана');
$('#bk-restore').onclick = () => $('#bk-file').click();
$('#bk-file').onchange = async e => {
  const f = e.target.files[0]; e.target.value = '';
  if (!f || !confirm(`Восстановить из «${f.name}»?\n\nТо, что сейчас стоит на колонке от панели (заставка, звук запуска, будильники), `
    + 'будет заменено содержимым копии. Оформление и раскладка этого браузера тоже вернутся из копии.')) return;
  toast('Восстанавливаем… это может занять минуту');
  const r = await act($('#bk-restore'), () => api('/api/backup/restore', f, { raw: true, headers: { 'Content-Type': 'application/gzip' } }));
  if (!r) return;
  try { for (const [k, v] of Object.entries(r.ui || {})) if (UI_KEYS.test(k)) localStorage.setItem(PanelContext.storageKey(k), v); } catch {}
  applyUI(); buildTiles(); fillTileSettings();
  try { const saved = JSON.parse(store.get(LAYOUT_KEY)); if (saved) applyLayout(saved); } catch {}
  bootLoaded = false; alarmsData = null;
  await loadFootprint();
  const when = r.created ? new Date(r.created.replace(/([+-]\d\d)(\d\d)$/, '$1:$2')).toLocaleString('ru-RU') : '';
  toast('Восстановлено' + (when ? ` (копия от ${when})` : '') + ': ' + (r.restored.length ? r.restored.join(', ') : 'на колонке ничего не было'));
};

// Установка: Chrome/Edge дают событие beforeinstallprompt, но только на HTTPS (или localhost).
window.addEventListener('beforeinstallprompt', e => { e.preventDefault(); installEvt = e; renderInstall(); });
window.addEventListener('appinstalled', () => { installEvt = null; renderInstall(); toast('Панель установлена'); });
function renderInstall() {
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  const hint = $('#app-hint'), st = $('#app-state'), a = accessInfo || {}, httpsPort = a.ports?.https;
  $('#app-install').hidden = !installEvt;
  // сертификат нужен другим устройствам, чтобы открыть панель по HTTPS
  $('#app-ca-row').hidden = !(a.ca && httpsPort);
  hint.hidden = true;
  if (standalone) { st.textContent = 'Панель уже открыта как приложение'; return; }
  if (installEvt) { st.textContent = 'Появится иконка, панель откроется в отдельном окне'; return; }
  if (!isSecureContext) {
    st.textContent = 'Браузер разрешает установку только по HTTPS';
    if (httpsPort) {
      const url = `https://${location.hostname}:${httpsPort}${PanelContext.basePath}`;
      hint.innerHTML = `Откройте панель по адресу <a href="${url}">${esc(url)}</a>. Чтобы браузер ей доверял, один раз установите сертификат (строка ниже): `
        + 'на Android — «Настройки → Безопасность → Шифрование и учётные данные → Установить сертификат → Сертификат ЦС»; '
        + 'на iPhone — открыть файл, затем «Основные → Об этом устройстве → Доверие сертификатам».';
    } else hint.textContent = 'HTTPS недоступен. Подробности — в журнале запуска панели.';
    hint.hidden = false;
    return;
  }
  st.textContent = 'Через меню браузера: «Установить приложение» или «На экран „Домой“»';
}
$('#app-install').onclick = async () => {
  if (!installEvt) return;
  installEvt.prompt();
  await installEvt.userChoice.catch(() => null);
  installEvt = null; renderInstall();
};
if ('serviceWorker' in navigator && isSecureContext) navigator.serviceWorker.register(PanelContext.url('/sw.js'), {scope:PanelContext.basePath}).catch(() => {});
loadAccess();

// ---------- графики ----------
// Каждая метрика — свой график с одной осью; наведение двигает перекрестие сразу на всех.
const CHARTS = [
  { k: 'cpu', name: 'Процессор', unit: '%', min: 0, max: 100, fmt: v => Math.round(v) },
  { k: 'temp', name: 'Температура', unit: '°C', pad: 2, fmt: v => v.toFixed(1) },
  { k: 'mem', name: 'Память занята', unit: 'МБ', min: 0, total: true, fmt: v => Math.round(v) },
  { k: 'rssi', name: 'Сигнал Wi‑Fi', unit: 'дБм', pad: 3, fmt: v => Math.round(v), note: 'ближе к нулю — лучше' },
  { k: 'disk', name: 'Диск /data занят', unit: 'МБ', min: 0, total: true, fmt: v => v.toFixed(1) },
  { k: 'load', name: 'Средняя нагрузка', unit: '', min: 0, fmt: v => v.toFixed(2), note: 'load average за минуту' },
];
const EV_TEXT = { reboot: '⟲', offline: '⨯', online: '✓', alarm: '⏰' };
let chData = null, chRange = +(store.get('ch.range') || 3600), chHover = null;
$('#ch-grid').innerHTML = CHARTS.map(c => `<section class="card chart-card">
  <div class="chart-head"><h2>${c.name}</h2><div class="chart-val" id="ch-${c.k}-v">—</div></div>
  <div class="chart-sub" id="ch-${c.k}-sub">${c.note || ''}</div>
  <svg class="chart-svg" id="ch-${c.k}" role="img" aria-label="${c.name}"></svg></section>`).join('');
const hm = t => { const d = new Date(t * 1000); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); };
function niceTicks(lo, hi, n = 3) {
  const span = hi - lo || 1, raw = span / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= n) || 10 * mag;
  // шкала накрывает данные целиком: от «круглого» снизу до «круглого» сверху
  const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step - 1e-9) * step, out = [];
  for (let v = a; v <= b + step * 1e-9; v += step) out.push(+v.toFixed(6));
  return { ticks: out, lo: out[0], hi: out[out.length - 1] };
}
async function loadCharts() {
  if (!online || document.hidden || shownTab !== 'charts') return;
  try { chData = await api('/api/metrics?range=' + chRange); renderCharts(); } catch (e) { if (!e.offline) toast(e.message, true); }
}
every(10000, loadCharts);
function chGeom(svg) {
  const W = svg.clientWidth || 600, H = svg.clientHeight || 150;
  return { W, H, L: 40, R: 8, T: 8, B: 20 };
}
function xOf(g, t) { return g.L + (t - chData.from) / (chData.to - chData.from) * (g.W - g.L - g.R); }
function renderCharts() {
  const d = chData; if (!d) return;
  $$('#ch-range button').forEach(b => b.classList.toggle('active', +b.dataset.v === chRange));
  const xt = { 900: 300, 3600: 900, 21600: 3600, 86400: 4 * 3600 }[chRange] || 3600;
  const xTicks = []; for (let t = Math.ceil(d.from / xt) * xt; t <= d.to; t += xt) xTicks.push(t);
  for (const c of CHARTS) {
    const svg = $('#ch-' + c.k), g = chGeom(svg), vals = d[c.k];
    const pts = d.t.map((t, i) => [t, vals[i]]).filter(p => p[1] != null);
    svg.setAttribute('viewBox', `0 0 ${g.W} ${g.H}`);
    if (!pts.length) { svg.innerHTML = `<text class="axis" x="${g.W / 2}" y="${g.H / 2}" text-anchor="middle">Данных пока нет</text>`; c.y = null; continue; }
    // память и диск — от нуля до полного объёма, чтобы было видно долю занятого
    const cmax = c.total && d.totals?.[c.k] ? d.totals[c.k] : c.max;
    let lo = c.min ?? Math.min(...pts.map(p => p[1])) - (c.pad || 0), hi = cmax ?? Math.max(...pts.map(p => p[1])) + (c.pad || 0);
    if (c.min != null && cmax == null) hi = Math.max(hi * 1.1, lo + 1);
    const nt = niceTicks(lo, hi); lo = c.min ?? nt.lo; hi = cmax ?? nt.hi;
    const y = v => g.T + (1 - (v - lo) / (hi - lo || 1)) * (g.H - g.T - g.B);
    c.y = y; c.g = g;
    // линия рвётся там, где данных не было (null от сервера)
    let path = '', area = '', run = [];
    const flush = () => {
      if (!run.length) return;
      const seg = run.map((p, i) => {
        const X = xOf(g, p[0]).toFixed(1), Y = y(p[1]).toFixed(1);
        if (c.step && i) return `H${X}V${Y}`;
        return (i ? 'L' : 'M') + X + ',' + Y;
      }).join('');
      path += seg;
      area += seg + `V${(g.H - g.B).toFixed(1)}H${xOf(g, run[0][0]).toFixed(1)}Z`;
      run = [];
    };
    d.t.forEach((t, i) => { if (vals[i] == null) flush(); else run.push([t, vals[i]]); });
    flush();
    svg.innerHTML =
      (cmax && c.total ? [0, Math.round(cmax / 2), cmax] : nt.ticks.filter(v => v >= lo && v <= hi)).map(v => `<line class="grid" x1="${g.L}" x2="${g.W - g.R}" y1="${y(v)}" y2="${y(v)}"/>`
        + `<text class="axis" x="${g.L - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`).join('')
      + xTicks.map(t => `<text class="axis" x="${xOf(g, t)}" y="${g.H - 4}" text-anchor="middle">${hm(t)}</text>`).join('')
      + d.events.map(e => `<line class="ev" x1="${xOf(g, e.t)}" x2="${xOf(g, e.t)}" y1="${g.T}" y2="${g.H - g.B}"><title>${hm(e.t)} ${esc(e.text)}</title></line>`).join('')
      + `<path class="area" d="${area}"/><path class="line" d="${path}"/>`
      + `<g class="hover" visibility="hidden"><line class="cross" y1="${g.T}" y2="${g.H - g.B}"/><circle class="dot" r="4"/></g>`;
  }
  // полоса воспроизведения
  const ps = $('#ch-play'), pg = { ...chGeom(ps), T: 4, B: 18 };
  ps.setAttribute('viewBox', `0 0 ${pg.W} ${pg.H}`);
  const bw = Math.max(1, (pg.W - pg.L - pg.R) * d.step / (d.to - d.from));
  ps.innerHTML = `<line class="grid" x1="${pg.L}" x2="${pg.W - pg.R}" y1="${pg.H - pg.B}" y2="${pg.H - pg.B}"/>`
    + d.t.map((t, i) => d.play[i] ? `<rect class="bar" x="${(xOf(pg, t) - bw / 2).toFixed(1)}" y="${pg.T}" width="${bw.toFixed(1)}" height="${pg.H - pg.T - pg.B}" rx="1"/>` : '').join('')
    + xTicks.map(t => `<text class="axis" x="${xOf(pg, t)}" y="${pg.H - 3}" text-anchor="middle">${hm(t)}</text>`).join('')
    + `<g class="hover" visibility="hidden"><line class="cross" y1="${pg.T}" y2="${pg.H - pg.B}"/></g>`;
  const played = d.play.filter(Boolean).length * d.step;
  $('#ch-play-v').innerHTML = played ? `${played >= 3600 ? (played / 3600).toFixed(1) + '<span>ч</span>' : Math.round(played / 60) + '<span>мин</span>'}` : '—';
  setListHtml($('#ch-events'), d.events.length ? d.events.slice().reverse().map(e =>
    `<div class="ev-row"><time>${hm(e.t)}</time><span>${EV_TEXT[e.type] || '•'} ${esc(e.text)}</span></div>`).join('')
    : '<span class="meta">За этот период ничего не случилось</span>');
  showHover(chHover);
}
function showHover(t) {
  const d = chData; if (!d) return;
  let i = -1;
  if (t != null) {
    let best = Infinity;
    d.t.forEach((x, k) => { const dd = Math.abs(x - t); if (dd < best) { best = dd; i = k; } });
  }
  for (const c of CHARTS) {
    const svg = $('#ch-' + c.k), h = svg.querySelector('.hover');
    const vals = d[c.k];
    // без наведения — последнее значение
    let k = i;
    if (k < 0) for (k = vals.length - 1; k >= 0 && vals[k] == null; k--);
    const v = k >= 0 ? vals[k] : null;
    const tot = c.total && d.totals?.[c.k];
    $('#ch-' + c.k + '-v').innerHTML = v == null ? '—' : `${c.fmt(v)}<span>${c.unit}${tot ? ` из ${tot} · ${Math.round(v / tot * 100)}%` : ''}</span>`;
    $('#ch-' + c.k + '-sub').textContent = i >= 0 ? `в ${hm(d.t[i])}` + (d.step > d.interval ? ` · среднее за ${d.step >= 60 ? Math.round(d.step / 60) + ' мин' : d.step + ' с'}` : '') : (c.note || 'сейчас');
    if (!h || !c.y) continue;
    if (i < 0) { h.setAttribute('visibility', 'hidden'); continue; }
    const X = xOf(c.g, d.t[i]);
    h.setAttribute('visibility', 'visible');
    h.querySelector('.cross').setAttribute('x1', X); h.querySelector('.cross').setAttribute('x2', X);
    const dot = h.querySelector('.dot');
    if (v == null || k !== i) dot.setAttribute('visibility', 'hidden');
    else { dot.setAttribute('visibility', 'visible'); dot.setAttribute('cx', X); dot.setAttribute('cy', c.y(v)); }
  }
  const ph = $('#ch-play .hover');
  if (ph) {
    if (i < 0) ph.setAttribute('visibility', 'hidden');
    else { const X = xOf({ ...chGeom($('#ch-play')) }, d.t[i]); ph.setAttribute('visibility', 'visible'); ph.querySelector('.cross').setAttribute('x1', X); ph.querySelector('.cross').setAttribute('x2', X); }
    $('#ch-play-sub').textContent = i >= 0 ? `в ${hm(d.t[i])}: ${d.play[i] ? 'играла' : 'тишина'}` : 'когда колонка что-то играла (музыка, ответы Алисы, звуки)';
  }
}
function chPointer(e) {
  const svg = e.currentTarget, r = svg.getBoundingClientRect(), g = chGeom(svg);
  const fx = (e.clientX - r.left - g.L) / (g.W - g.L - g.R);
  if (!chData || fx < 0 || fx > 1) { chHover = null; return showHover(null); }
  chHover = chData.from + fx * (chData.to - chData.from);
  showHover(chHover);
}
$$('#tab-charts .chart-svg').forEach(svg => {
  svg.addEventListener('pointermove', chPointer);
  svg.addEventListener('pointerleave', () => { chHover = null; showHover(null); });
});
$$('#ch-range button').forEach(b => b.onclick = () => { chRange = +b.dataset.v; store.set('ch.range', chRange); loadCharts(); });
new ResizeObserver(() => shownTab === 'charts' && renderCharts()).observe($('#ch-grid'));
$('#ch-clear').onclick = e => {
  if (!confirm('Стереть всю историю графиков?')) return;
  act(e.currentTarget, async () => { await api('/api/metrics/clear', {}); await loadCharts(); }, 'История очищена');
};
$('#ch-csv').onclick = () => {
  const d = chData; if (!d) return;
  const rows = [['время', ...CHARTS.map(c => c.name + (c.unit ? ', ' + c.unit : '')), 'играла']];
  d.t.forEach((t, i) => rows.push([new Date(t * 1000).toLocaleString('ru-RU'), ...CHARTS.map(c => d[c.k][i] ?? ''), d.play[i] ?? '']));
  const blob = new Blob(['\ufeff' + rows.map(r => r.join(';')).join('\n')], { type: 'text/csv;charset=utf-8' });
  saveBlob(blob, `stantsiya-${hm(d.to).replace(':', '')}.csv`).catch(e => toast(e.message, true));
};

// ---------- будильник ----------
// Будильники хранятся на колонке (alarms.conf), панель только редактирует их и показывает состояние.
const DAY_NAMES = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
const AL_MODES = {
  wave: { name: 'Моя волна', hint: 'Алиса включит «Мою волну».' },
  show: { name: 'Утреннее шоу', hint: 'Утреннее шоу Алисы: музыка, погода, новости.' },
  music: { name: 'Музыка', cap: 'Что включить', ph: 'например: плейлист дня, Queen, джаз', hint: 'Алиса получит запрос «включи …».' },
  text: { name: 'Запрос Алисе', cap: 'Что сказать Алисе', ph: 'например: доброе утро', hint: 'Запрос уходит Алисе как есть, будто вы сказали его вслух.' },
  sound: { name: 'Звук', hint: 'Играет по кругу прямо с колонки — не нужен ни интернет, ни Алиса.' },
};
let alarmsData = null, alEditing = null, alUploaded = [];
// у системных звуков в sound_name лежит путь (common/timer.wav), у своих — имя файла
const sndName = a => a.sound.startsWith('u_') ? a.sound_name : (a.sound_name || 'common/timer.wav').split('/').pop().replace(/\.[^.]+$/, '');
function daysText(d) {
  if (!d) return 'Один раз';
  if (d === '1234567') return 'Каждый день';
  if (d === '12345') return 'По будням';
  if (d === '67') return 'По выходным';
  return [...d].map(x => DAY_NAMES[x - 1]).join(', ');
}
function nextFire(a) {
  // ближайшее срабатывание по времени браузера (часовой пояс — как у компьютера с панелью)
  const [h, m] = a.time.split(':').map(Number), now = new Date();
  for (let i = 0; i < 8; i++) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() + i, h, m);
    const dow = (d.getDay() + 6) % 7 + 1;
    if (d > now && (!a.days || a.days.includes(dow))) return d;
  }
  return null;
}
function inText(ms) {
  const min = Math.round(ms / 60000), h = Math.floor(min / 60), m = min % 60;
  return 'через ' + (h ? `${h} ч ` : '') + (m || !h ? `${m} мин` : '');
}
async function loadAlarms() {
  try { alarmsData = await api('/api/alarms'); } catch (e) { if (!e.offline) setListHtml($('#al-list'), `<div class="empty">${esc(e.message)}</div>`); return; }
  renderAlarms();
}
function renderAlarms() {
  const d = alarmsData; if (!d) return;
  // На Станции Макс будильники не поддерживаются, и ответ может быть без
  // массива log/alarms. Не роняем остальную панель из-за необязательных полей.
  if (!Array.isArray(d.alarms)) d.alarms = [];
  if (!Array.isArray(d.log)) d.log = [];
  const on = d.alarms.filter(a => a.on);
  const next = on.map(a => [a, nextFire(a)]).filter(x => x[1]).sort((x, y) => x[1] - y[1])[0];
  $('#al-badge').textContent = !d.alarms.length ? 'нет' : next ? `ближайший ${inText(next[1] - Date.now())}` : 'все выключены';
  $('#al-badge').className = 'badge' + (on.length && d.running ? ' ok' : on.length && !d.running ? ' bad' : '');
  if (on.length && !d.running) $('#al-badge').textContent = 'служба не запущена — проверьте поддержку профиля и хук';
  setListHtml($('#al-list'), d.alarms.length ? d.alarms.map(a => {
    const m = AL_MODES[a.mode] || AL_MODES.sound;
    const what = a.mode === 'music' ? `Включить: ${a.query}` : a.mode === 'text' ? `«${a.query}»` : m.name;
    const extra = [a.mode !== 'sound' ? `иначе звук «${sndName(a)}»` : `звук «${sndName(a)}»`,
                   `громкость ${a.volume * 10}%`, a.ramp ? `нарастание ${a.ramp >= 60 ? a.ramp / 60 + ' мин' : a.ramp + ' с'}` : ''].filter(Boolean).join(' · ');
    return `<div class="al-row${a.on ? '' : ' off'}" data-id="${esc(a.id)}">
      <div class="al-time">${esc(a.time)}</div>
      <div class="al-info"><b>${esc(a.label || daysText(a.days))}</b>
        <div class="meta">${esc(a.label ? daysText(a.days) + ' · ' : '')}${esc(what)}</div><div class="meta">${esc(extra)}</div></div>
      <div class="actions">
        <input type="checkbox" data-a="toggle" ${a.on ? 'checked' : ''} aria-label="Включён" title="Включён">
        <button class="btn sm" data-a="test" title="Сработает прямо сейчас">Проверить</button>
        <button class="btn sm" data-a="edit">Изменить</button>
      </div></div>`;
  }).join('') : '<div class="empty">Будильников пока нет</div>');
  $('#al-tz').textContent = d.pc_tz || 'неизвестен (используется UTC)';
  $('#al-devtime').textContent = d.device_time?.hm ? `на колонке ${d.device_time.hm} ${d.device_time.zone || ''}` : '';
  // в журнале демона — id; показываем вместо них время и подпись
  const alName = id => { const a = d.alarms.find(x => x.id === id); return a ? a.time + (a.label ? ' ' + a.label : '') : id; };
  $('#al-log').textContent = d.log.length ? d.log.slice().reverse().map(l => l.replace(/ ([0-9a-f]{8}): /, (_, id) => ` ${alName(id)}: `)).join('\n') : 'Пока пусто';
  const sn = d.snooze;
  $('#al-snoozed').hidden = !sn;
  if (sn) {
    const a = d.alarms.find(x => x.id === sn.id), t = new Date(sn.at * 1000);
    $('#al-snoozed-text').textContent = `Отложенный будильник${a?.label ? ' «' + a.label + '»' : ''} зазвонит в ${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`;
  }
  renderAlarmBar();
  // без установки на колонку — только карточка установки
  const inst = d.installed;
  $('#al-setup').hidden = inst;
  ['#al-list-card', '#al-cols', '#al-add'].forEach(x => $(x).hidden = !inst);
  $('#al-add').disabled = d.supported === false;
  $$('#al-list [data-a]').forEach(b => { b.disabled = d.supported === false; });
  if (!inst) { $('#al-editor').hidden = true; $('#al-snoozed').hidden = true; }
  $('#al-install-note').textContent = d.ffmpeg ? '' : 'Нужен ffmpeg на компьютере — им перекодируется звук.';
  $('#al-install').disabled = !d.ffmpeg || d.supported === false;
  if (d.supported === false) { $('#al-install-note').textContent = d.reason; $('#al-save').disabled = true; }
  $('#al-inst-info').textContent = `${d.dir}${d.size_kb != null ? ' · ' + (d.size_kb >= 1024 ? (d.size_kb / 1024).toFixed(1) + ' МБ' : d.size_kb + ' КБ') : ''}`
    + (d.outdated ? ' · есть новая версия скрипта' : '');
  $('#al-update').hidden = !d.outdated || d.supported === false;
}
function renderAlarmBar() {
  const st = alarmsData?.state, bar = $('#alarm-bar');
  bar.hidden = !st?.ringing;
  if (!st?.ringing) return;
  const a = alarmsData.alarms.find(x => x.id === st.id);
  $('#ab-title').textContent = 'Будильник' + (a ? ' ' + a.time : '') + (a?.label ? ' · ' + a.label : '');
  $('#ab-sub').textContent = st.via === 'alice' ? 'играет Алиса' : st.via === 'starting' ? 'включаем…' : 'играет звук';
  $('#ab-snooze').hidden = !a?.snooze;
  if (a?.snooze) $('#ab-snooze').textContent = `Отложить на ${a.snooze} мин`;
}
// на колонке системный звук хранится перекодированным (s_*.wav) — серверу отдаём исходный путь
const alToSave = a => ({ ...a, sound: a.sound.startsWith('s_') ? 'sys:' + a.sound_name : a.sound });
async function saveAlarms(list, msg) {
  alarmsData = await api('/api/alarms', { alarms: list });
  renderAlarms();
  if (msg) toast(msg);
}
$('#al-list').onclick = e => {
  const b = e.target.closest('[data-a]'), row = e.target.closest('.al-row');
  if (!b || !row) return;
  const a = alarmsData.alarms.find(x => x.id === row.dataset.id);
  if (b.dataset.a === 'edit') openAlarm(a);
  if (b.dataset.a === 'test') act(b, async () => { await api('/api/alarms/test', { id: a.id }); setTimeout(loadAlarms, 2500); }, 'Сейчас зазвонит…');
};
$('#al-list').onchange = e => {
  if (e.target.dataset.a !== 'toggle') return;
  const id = e.target.closest('.al-row').dataset.id;
  const list = alarmsData.alarms.map(a => alToSave(a.id === id ? { ...a, on: e.target.checked } : a));
  act(null, () => saveAlarms(list, e.target.checked ? 'Будильник включён' : 'Будильник выключен'));
};
$('#al-days').innerHTML = DAY_NAMES.map((n, i) => `<button type="button" class="chip" data-d="${i + 1}" aria-pressed="false">${n}</button>`).join('');
$('#al-days').onclick = e => { const c = e.target.closest('.chip'); if (c) c.setAttribute('aria-pressed', c.getAttribute('aria-pressed') !== 'true'); };
$('#al-presets').onclick = e => { const c = e.target.closest('.chip'); if (c) setDays(c.dataset.d); };
function setDays(d) { $$('#al-days .chip').forEach(c => c.setAttribute('aria-pressed', d.includes(c.dataset.d))); }
const getDays = () => $$('#al-days .chip').filter(c => c.getAttribute('aria-pressed') === 'true').map(c => c.dataset.d).join('');
function fillSounds(sel) {
  const groups = {};
  (sounds || []).forEach(n => { const [g, ...r] = n.split('/'); (groups[r.length ? g : ''] ||= []).push(n); });
  const up = [...new Map([...alUploaded, ...(alarmsData?.alarms || []).filter(a => a.sound.startsWith('u_')).map(a => ({ sound: a.sound, sound_name: a.sound_name }))]
    .map(u => [u.sound, u])).values()];
  $('#al-sound').innerHTML = (up.length ? `<optgroup label="Свои">${up.map(u => `<option value="${esc(u.sound)}">${esc(u.sound_name)}</option>`).join('')}</optgroup>` : '')
    + Object.entries(groups).map(([g, l]) => `<optgroup label="${esc(g || 'Система')}">${l.map(n => `<option value="sys:${esc(n)}">${esc(n.split('/').pop().replace(/\.[^.]+$/, ''))}</option>`).join('')}</optgroup>`).join('');
  $('#al-sound').value = sel;
  if ($('#al-sound').selectedIndex < 0) $('#al-sound').value = 'sys:common/timer.wav';
}
function alModeUI() {
  const m = $('#al-mode').value, info = AL_MODES[m];
  $('#al-query-l').hidden = !info.cap;
  if (info.cap) { $('#al-query-cap').textContent = info.cap; $('#al-query').placeholder = info.ph; }
  $('#al-mode-hint').textContent = info.hint + (m === 'sound' ? '' : ' Нужен интернет; без него или если Алиса не ответит за 30 с — играет звук ниже.');
  $('#al-sound-cap').textContent = m === 'sound' ? 'Звук' : 'Звук, если Алиса не сможет';
  $('#al-dur').querySelector('[value="0"]').disabled = m === 'sound';
  if (m === 'sound' && $('#al-dur').value === '0') $('#al-dur').value = '15';
}
$('#al-mode').onchange = alModeUI;
$('#al-vol').oninput = () => $('#al-vol-v').textContent = $('#al-vol').value * 10;
function openAlarm(a) {
  alEditing = a || null;
  const x = a || { time: '07:30', days: '12345', mode: 'wave', query: '', sound: '', volume: 5, ramp: 180, duration: 15, snooze: 9, label: '' };
  $('#al-ed-title').textContent = a ? 'Будильник ' + a.time : 'Новый будильник';
  $('#al-time').value = x.time; $('#al-label').value = x.label; setDays(x.days);
  $('#al-mode').value = x.mode; $('#al-query').value = x.query;
  fillSounds(x.sound.startsWith('u_') ? x.sound : x.sound.startsWith('s_') ? 'sys:' + x.sound_name : 'sys:common/timer.wav');
  $('#al-vol').value = x.volume; $('#al-vol').oninput();
  $('#al-ramp').value = String(x.ramp); if ($('#al-ramp').selectedIndex < 0) $('#al-ramp').value = '180';
  $('#al-dur').value = String(x.duration); if ($('#al-dur').selectedIndex < 0) $('#al-dur').value = '15';
  $('#al-snz').value = String(x.snooze); if ($('#al-snz').selectedIndex < 0) $('#al-snz').value = '9';
  $('#al-del').hidden = !a;
  alModeUI();
  $('#al-editor').hidden = false;
  $('#al-editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
$('#al-add').onclick = () => openAlarm(null);
// ---------- сценарии ----------
// Сценарий — граф нод; на колонке его выполняет демон будильника (scenes.conf). У «Условия» и «Повтора»
// два выхода: next (да / ещё круг) и else (нет / круги кончились). Простой сценарий — цепочка без ветвлений —
// можно править и списком; с ветвлениями — только на поле.
const SC_NODES = {
  query: { name: 'Запрос Алисе', icon: 'i-mic', group: 'Алиса', def: () => ({ text: '' }) },
  say: { name: 'Алиса скажет', icon: 'i-mic', group: 'Алиса', def: () => ({ text: '' }) },
  ask: { name: 'Спросить ассистента', icon: 'i-mic', group: 'Алиса', def: () => ({ text: '' }), assistant: true },
  assistant: { name: 'Режим ассистента', icon: 'i-mic', group: 'Алиса', def: () => ({ mode: true }), assistant: true },
  vol: { name: 'Громкость', icon: 'i-vol', group: 'Звук и свет', def: () => ({ level: 3, ramp: 0 }) },
  sound: { name: 'Звук', icon: 'i-music', group: 'Звук и свет', def: () => ({ sound: 'sys:common/timer.wav' }) },
  ring: { name: 'Кольцо', icon: 'i-palette', group: 'Звук и свет', def: () => ({ color: ringHex || '#ff5a00', secs: 10 }) },
  anim: { name: 'Анимация', icon: 'i-palette', group: 'Звук и свет', def: () => ({ anim: 'ring:' + ((patterns.ring || []).find(n => n.startsWith('alice_thinking')) || (patterns.ring || [])[0] || ''), secs: 5 }) },
  stop: { name: 'Стоп', icon: 'i-stop', group: 'Звук и свет', def: () => ({}) },
  wait: { name: 'Пауза', icon: 'i-alarm', group: 'Время', def: () => ({ secs: 60 }) },
  wait_until: { name: 'Ждать до', icon: 'i-alarm', group: 'Время', def: () => ({ time: '07:00' }) },
  wait_quiet: { name: 'Ждать тишины', icon: 'i-vol', group: 'Время', def: () => ({ secs: 600 }) },
  if: { name: 'Условие', icon: 'i-shuffle', group: 'Логика', def: () => ({ check: 'time', arg: '22:00-07:00' }), outs: ['да', 'нет'] },
  repeat: { name: 'Повторить', icon: 'i-repeat', group: 'Логика', def: () => ({ count: 3 }), outs: ['ещё', 'готово'] },
  scene: { name: 'Другой сценарий', icon: 'i-flow', group: 'Логика', def: () => ({ target: '' }) },
  note: { name: 'Заметка', icon: 'i-edit', group: 'Логика', def: () => ({ text: '' }), noPorts: true },
};
const SC_CHECKS = {
  time: { name: 'время между', ph: '22:00-07:00' }, days: { name: 'день недели', ph: '67' },
  playing: { name: 'что-то играет' }, online: { name: 'есть интернет' }, vol_above: { name: 'громкость выше', ph: '5' },
  chance: { name: 'случайно, % «да»', ph: '50' }, assistant_mode: { name: 'включён режим ассистента' },
};
const SC_RAMPS = [[0, 'сразу'], [30, 'за 30 с'], [60, 'за 1 мин'], [180, 'за 3 мин'], [300, 'за 5 мин'], [600, 'за 10 мин'], [1800, 'за 30 мин']];
let scData = null, scEdit = null, scTask = 0, scSel = null, scHasAssistant = false;
const durText = s => s >= 3600 ? `${+(s / 3600).toFixed(1)} ч` : s >= 60 ? `${+(s / 60).toFixed(1)} мин` : `${s} с`;
const sndTitle = v => v.startsWith('u_') ? (alUploaded.find(u => u.sound === v)?.sound_name || 'свой звук') : v.replace(/^sys:/, '').split('/').pop().replace(/\.[^.]+$/, '');
function nodeText(n) {
  switch (n.kind) {
    case 'query': return `«${n.text}»`;
    case 'say': return `скажет «${n.text}»`;
    case 'ask': return `ассистенту «${n.text}»`;
    case 'assistant': return n.mode ? 'режим ассистента: вкл' : 'режим ассистента: выкл';
    case 'vol': return `громкость ${n.level * 10}%` + (n.ramp ? ` ${SC_RAMPS.find(r => r[0] === n.ramp)?.[1] || durText(n.ramp)}` : '');
    case 'sound': return `звук «${sndTitle(n.sound)}»`;
    case 'ring': return `кольцо ${durText(n.secs)}`;
    case 'wait': return `пауза ${durText(n.secs)}`;
    case 'wait_until': return `ждать до ${n.time}`;
    case 'wait_quiet': return 'ждать тишины';
    case 'if': return `если ${SC_CHECKS[n.check]?.name || ''}${n.arg && SC_CHECKS[n.check]?.ph ? ' ' + n.arg : ''}`;
    case 'repeat': return `повторить ${n.count} раз`;
    case 'scene': return `сценарий «${scData?.scenes.find(s => s.id === n.target)?.name || '?'}»`;
    case 'anim': return `анимация «${animName(n.anim)}»`;
    case 'note': return 'заметка';
    default: return 'стоп';
  }
}
const scNode = (s, id) => s.nodes.find(n => n.id === id);
const animName = v => animTitle(String(v || '').replace(/^\w+:/, ''));
// цепочка от «Запуска» по next; linear — сценарий без ветвлений, все ноды (кроме заметок) в ней
function scChain(s) {
  const out = [], seen = new Set();
  for (let n = scNode(s, s.start); n && !seen.has(n.id); n = scNode(s, n.next)) { seen.add(n.id); out.push(n); }
  const linear = out.every(n => !SC_NODES[n.kind].outs) && s.nodes.every(n => n.kind === 'note' || seen.has(n.id))
    && !(out.length && scNode(s, out[out.length - 1].next)) && !s.nodes.some(n => n.kind === 'note');
  return { chain: out, linear };
}
async function loadScenes() {
  if (!online || document.hidden || $('#tab-scenes').hidden) return;
  try {
    [scData, scHasAssistant] = await Promise.all([api('/api/scenes'), api('/api/assistant').then(a => !!a.installed, () => false)]);
    if (!patterns.ring) patterns = await api('/api/patterns').catch(() => ({}));   // для ноды «Анимация»
  } catch (e) { if (!e.offline) setListHtml($('#sc-list'), `<div class="empty">${esc(e.message)}</div>`); return; }
  renderScenes();
  // пока что-то идёт — обновляем, чтобы кнопка «Остановить» и журнал не отставали
  Scheduler.setInterval(scTask, scData.running.length ? 2000 : 15000);
}
scTask = every(15000, loadScenes);
function renderScenes() {
  const d = scData; if (!d) return;
  const on = d.scenes.filter(s => s.on).length;
  if (d.supported === false) { $('#sc-add').disabled = true; $('#sc-save').disabled = true; }
  $('#sc-badge').textContent = !d.scenes.length ? 'нет' : on && !d.daemon ? 'служба не запущена — проверьте поддержку профиля и хук' : `включено ${on} из ${d.scenes.length}`;
  $('#sc-badge').className = 'badge' + (on && d.daemon ? ' ok' : on && !d.daemon ? ' bad' : '');
  setListHtml($('#sc-list'), d.scenes.length ? d.scenes.map(s => {
    const run = d.running.includes(s.id), { chain, linear } = scChain(s);
    return `<div class="al-row${s.on ? '' : ' off'}" data-id="${esc(s.id)}">
      <div class="al-time">${({ boot: icon('i-power', 30), voice: icon('i-mic', 30), manual: icon('i-play', 30) })[s.trigger] || esc(s.time)}</div>
      <div class="al-info"><b>${esc(s.name)}</b>
        <div class="meta">${s.trigger === 'boot' ? 'после включения колонки' : s.trigger === 'manual' ? 'только вручную — кнопкой или из другого сценария' : s.trigger === 'voice' ? esc(`по команде ассистенту: «${s.phrases.join('», «')}»`) : esc(daysText(s.days))}${run ? ' · <span style="color:var(--ok-ink);font-weight:700">выполняется</span>' : ''}</div>
        <div class="sc-flow">${chain.slice(0, 6).map(n => `<span>${esc(nodeText(n))}</span>`).join('')}${chain.length > 6 || !linear ? `<span>${linear ? '…' : 'есть ветвления'}</span>` : ''}</div></div>
      <div class="actions">
        <input type="checkbox" data-a="toggle" ${s.on ? 'checked' : ''} aria-label="Включён" title="Включён">
        ${run ? '<button class="btn sm danger-soft" data-a="stop">Остановить</button>' : '<button class="btn sm" data-a="run" title="Выполнить прямо сейчас">Запустить</button>'}
        <button class="btn sm" data-a="edit">Изменить</button>
      </div></div>`;
  }).join('') : '<div class="empty">Сценариев пока нет. Например: «Ночь» — в 23:00 громкость 20% и «Алиса, включи режим не беспокоить».</div>');
  $('#sc-log').textContent = d.log.length ? d.log.slice().reverse().join('\n') : 'Пока пусто';
}
const scSave = list => api('/api/scenes', { scenes: list }).then(r => { scData = r; renderScenes(); return r; });
$('#sc-list').onclick = e => {
  const b = e.target.closest('[data-a]'); if (!b) return;
  const id = b.closest('.al-row').dataset.id, s = scData.scenes.find(x => x.id === id);
  if (b.dataset.a === 'edit') return openScene(s);
  if (b.dataset.a === 'run') return act(b, async () => { await api('/api/scenes/run', { id }); setTimeout(loadScenes, 1500); }, `Сценарий «${s.name}» запущен`);
  if (b.dataset.a === 'stop') return act(b, async () => { await api('/api/scenes/stop', { id }); await loadScenes(); }, 'Сценарий остановлен');
};
$('#sc-list').onchange = e => {
  if (e.target.dataset.a !== 'toggle') return;
  const id = e.target.closest('.al-row').dataset.id;
  act(e.target, () => scSave(scData.scenes.map(s => s.id === id ? { ...s, on: e.target.checked } : s)));
};

// ---- редактор: общие части ----
$('#sc-days').innerHTML = DAY_NAMES.map((n, i) => `<button type="button" class="chip" data-d="${i + 1}" aria-pressed="false">${n}</button>`).join('');
$('#sc-days').onclick = e => { const c = e.target.closest('.chip'); if (c) { c.setAttribute('aria-pressed', c.getAttribute('aria-pressed') !== 'true'); scFieldStart(); } };
$('#sc-presets').onclick = e => { const c = e.target.closest('.chip'); if (c) { $$('#sc-days .chip').forEach(x => x.setAttribute('aria-pressed', c.dataset.d.includes(x.dataset.d))); scFieldStart(); } };
const scDays = () => $$('#sc-days .chip').filter(c => c.getAttribute('aria-pressed') === 'true').map(c => c.dataset.d).join('');
function scTriggerUI() {
  const v = $('#sc-trigger').value;
  $$('[data-sc-time]').forEach(x => x.hidden = v === 'boot' || v === 'manual');
  $('#sc-time').closest('label').hidden = v !== 'time';
  $$('[data-sc-voice]').forEach(x => x.hidden = v !== 'voice');
  $('#sc-boot-hint').hidden = v !== 'boot' && v !== 'manual';
  $('#sc-boot-hint').textContent = v === 'manual'
    ? 'Сам не запускается: только кнопкой «Запустить» здесь или нодой «Другой сценарий» из другого сценария.'
    : 'Запустится примерно через минуту после того, как Алиса загрузится, — при каждом включении колонки.';
  $('#sc-days-cap').textContent = v === 'voice' ? 'В какие дни (ни одного — в любой)' : 'Повторять';
  $('#sc-once').hidden = v === 'voice';
  $('#sc-voice-hint').textContent = scHasAssistant
    ? 'Скажите «Алиса, спроси GPT, <фраза>» — или просто фразу, если включён режим ассистента. Алиса такие команды сама не поймёт, их ловит ассистент. Дни ниже — когда команда работает.'
    : 'Нужен ассистент (вкладка «Ассистент»): команды ловит он — Алиса сама их не поймёт.';
  scFieldStart();
}
const scPhrases = () => $('#sc-phrases').value.split(',').map(x => x.trim()).filter(Boolean);
$('#sc-trigger').onchange = scTriggerUI;
$('#sc-time').onchange = () => scFieldStart();
$('#sc-phrases').oninput = () => scFieldStart();
const scTrigText = () => ({ boot: 'после включения колонки', manual: 'вручную', voice: `по команде «${scPhrases()[0] || '…'}»` })[$('#sc-trigger').value]
  || `${$('#sc-time').value || '—'} · ${daysText(scDays())}`;
function soundOptions() {
  const groups = {};
  (sounds || []).forEach(n => { const [g, ...r] = n.split('/'); (groups[r.length ? g : ''] ||= []).push(n); });
  return (alUploaded.length ? `<optgroup label="Свои">${alUploaded.map(u => `<option value="${esc(u.sound)}">${esc(u.sound_name)}</option>`).join('')}</optgroup>` : '')
    + Object.entries(groups).map(([g, l]) => `<optgroup label="${esc(g || 'Система')}">${l.map(n => `<option value="sys:${esc(n)}">${esc(n.split('/').pop().replace(/\.[^.]+$/, ''))}</option>`).join('')}</optgroup>`).join('');
}
function nodeFields(n) {
  switch (n.kind) {
    case 'query': return `<input type="text" class="field" data-f="text" maxlength="200" value="${esc(n.text)}" placeholder="включи режим не беспокоить" aria-label="Что попросить">`;
    case 'say': return `<input type="text" class="field" data-f="text" maxlength="200" value="${esc(n.text)}" placeholder="Пора спать" aria-label="Что сказать">`;
    case 'ask': return `<input type="text" class="field" data-f="text" maxlength="200" value="${esc(n.text)}" placeholder="расскажи сказку на ночь" aria-label="Вопрос ассистенту">`
      + (scHasAssistant ? '' : '<span class="meta">ассистент не установлен — шаг пропустится</span>');
    case 'assistant': return `<select class="field" data-f="mode" aria-label="Режим ассистента"><option value="on" ${n.mode ? 'selected' : ''}>включить</option><option value="off" ${n.mode ? '' : 'selected'}>выключить</option></select>
      <span class="meta">все вопросы к Алисе — своей модели</span>`;
    case 'vol': return `<input type="range" min="0" max="10" data-f="level" value="${n.level}" aria-label="Громкость"><span class="mono meta" style="min-width:38px">${n.level * 10}%</span>
      <select class="field" data-f="ramp" aria-label="Как менять">${SC_RAMPS.map(([v, t]) => `<option value="${v}" ${v === n.ramp ? 'selected' : ''}>${t}</option>`).join('')}</select>`;
    case 'sound': return `<select class="field" data-f="sound" style="flex:1;min-width:140px" aria-label="Звук">${soundOptions()}</select>`;
    case 'ring': return `<input type="color" data-f="color" value="${esc(n.color)}" aria-label="Цвет"><input type="number" class="field mono" data-f="secs" min="1" max="600" value="${n.secs}" aria-label="Секунд"><span class="meta">с</span>`;
    case 'wait': return `<input type="number" class="field mono" data-f="mins" min="0.1" max="1440" step="0.5" value="${+(n.secs / 60).toFixed(2)}" aria-label="Минут"><span class="meta">мин</span>`;
    case 'wait_until': return `<input type="time" class="field" data-f="time" value="${esc(n.time)}" aria-label="До какого времени">`;
    case 'wait_quiet': return `<span class="meta">пока Алиса и музыка не замолчат, но не дольше</span><input type="number" class="field mono" data-f="mins" min="1" max="120" value="${Math.round(n.secs / 60)}" aria-label="Минут"><span class="meta">мин</span>`;
    case 'scene': return `<select class="field" data-f="target" aria-label="Какой сценарий" style="flex:1;min-width:140px"><option value="">— выберите —</option>${(scData?.scenes || [])
      .filter(s => s.id && s.id !== scEdit.id).map(s => `<option value="${esc(s.id)}" ${s.id === n.target ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}</select>`;
    case 'if': return `<select class="field" data-f="check" aria-label="Условие">${Object.entries(SC_CHECKS).map(([k, c]) => `<option value="${k}" ${k === n.check ? 'selected' : ''}>${c.name}</option>`).join('')}</select>`
      + (SC_CHECKS[n.check]?.ph ? `<input type="text" class="field mono" data-f="arg" value="${esc(n.arg)}" placeholder="${SC_CHECKS[n.check].ph}" aria-label="Значение" style="width:120px">` : '');
    case 'repeat': return `<input type="number" class="field mono" data-f="count" min="1" max="20" value="${n.count}" aria-label="Сколько раз"><span class="meta">раз (до 20), потом «готово»</span>`;
    case 'anim': return `<select class="field" data-f="anim" aria-label="Анимация" style="flex:1;min-width:140px">${[['ring', 'Кольцо'], ['clock', 'Часы']]
      .filter(([k]) => (patterns[k] || []).length).map(([k, g]) => `<optgroup label="${g}">${patterns[k].map(p => `<option value="${k}:${esc(p)}" ${n.anim === k + ':' + p ? 'selected' : ''}>${esc(animName(p))}</option>`).join('')}</optgroup>`).join('')}</select>
      <input type="number" class="field mono" data-f="secs" min="1" max="600" value="${n.secs}" aria-label="Секунд"><span class="meta">с</span>`;
    case 'note': return `<textarea class="field" data-f="text" rows="2" maxlength="200" placeholder="Заметка для себя" aria-label="Заметка">${esc(n.text)}</textarea>`;
    default: return '<span class="meta">остановить музыку и ответ Алисы</span>';
  }
}
function scInput(e) {
  const row = e.target.closest('[data-n]'), f = e.target.dataset.f; if (!row || !f) return;
  const n = scNode(scEdit, row.dataset.n);
  if (f === 'mins') n.secs = clamp(Math.round((+e.target.value || 1) * 60), 1, 86400);
  else if (['level', 'ramp', 'secs', 'count'].includes(f)) n[f] = +e.target.value;
  else if (f === 'mode') n.mode = e.target.value === 'on';
  else n[f] = e.target.value;
  if (f === 'level') e.target.nextElementSibling.textContent = n.level * 10 + '%';
  if (f === 'check') { n.arg = ''; scRender(); }
}
const scNextId = () => String(Math.max(0, ...scEdit.nodes.map(n => +n.id)) + 1);

// ---- список: только для цепочки без ветвлений ----
function renderScList() {
  const { chain, linear } = scChain(scEdit);
  if (!linear) {
    $('#sc-steps').innerHTML = '<div class="empty">В этом сценарии есть условия, повторы или заметки — его можно править только на поле. <button type="button" class="link" data-v="field">Открыть поле</button></div>';
    return;
  }
  $('#sc-steps').innerHTML = chain.length ? chain.map((n, i) => `<div class="sc-step" data-n="${esc(n.id)}">
    <span class="n">${i + 1}</span><span class="k">${icon(SC_NODES[n.kind].icon, 14)}${esc(SC_NODES[n.kind].name)}</span>
    <div class="v">${nodeFields(n)}</div>
    <span class="ctl"><button type="button" class="btn sm icon ghost" data-m="-1" aria-label="Выше" title="Выше">↑</button>
      <button type="button" class="btn sm icon ghost" data-m="1" aria-label="Ниже" title="Ниже">↓</button>
      <button type="button" class="btn sm icon ghost" data-m="del" aria-label="Удалить шаг" title="Удалить шаг">${icon('i-trash', 14)}</button></span></div>`).join('')
    : '<div class="empty">Добавьте первый шаг кнопками ниже</div>';
  scSoundValues($('#sc-steps'));
}
function scSetChain(chain) {   // перестраиваем связи по порядку списка
  scEdit.start = chain[0]?.id || '';
  chain.forEach((n, i) => { n.next = chain[i + 1]?.id || ''; n.else = ''; });
}
$('#sc-steps').oninput = scInput;
$('#sc-steps').onclick = e => {
  if (e.target.closest('[data-v]')) return scView('field');
  const b = e.target.closest('[data-m]'); if (!b) return;
  const chain = scChain(scEdit).chain, i = chain.findIndex(n => n.id === b.closest('[data-n]').dataset.n);
  if (b.dataset.m === 'del') { const [gone] = chain.splice(i, 1); scEdit.nodes = scEdit.nodes.filter(n => n !== gone); }
  else { const j = i + +b.dataset.m; if (j < 0 || j >= chain.length) return; [chain[i], chain[j]] = [chain[j], chain[i]]; }
  scSetChain(chain); scRender();
};
function scSoundValues(box) {
  box.querySelectorAll('[data-f="sound"]').forEach(s => { s.value = scNode(scEdit, s.closest('[data-n]').dataset.n).sound; if (s.selectedIndex < 0) s.value = 'sys:common/timer.wav'; });
}

// ---- поле: ноды, связи и перетаскивание ----
const SC_W = 236;
// Место ноды «Запуск» — в браузере. store хранит только строки: объект — через JSON, иначе вместо места
// сохранится «[object Object]», и всё, что ставится рядом с «Запуском», получит NaN.
const scXY = v => Number.isFinite(v?.x) && Number.isFinite(v?.y);
function scStartPos() {
  if (!scXY(scEdit._start)) {
    let v = null;
    try { v = JSON.parse(store.get('sc.start.' + (scEdit.id || 'new'))); } catch {}
    scEdit._start = scXY(v) ? v : { x: 24, y: 40 };
  }
  return scEdit._start;
}
const scStartSave = (id, p) => store.set('sc.start.' + (id || 'new'), JSON.stringify({ x: p.x, y: p.y }));
function renderScField() {
  if (scEdit.nodes.some(n => !scXY(n))) scLayout();   // место без чисел — пересчитываем, иначе нода застрянет в углу
  const f = $('#sc-field'), s = scEdit, st = scStartPos();
  const port = (key, label) => `<span class="port out" data-port="${key}">${label ? `<i>${label}</i>` : ''}</span>`;
  f.innerHTML = '<div class="sc-canvas"><svg class="sc-wires"></svg>'
    + `<div class="sc-node trig" data-n="start" style="left:${st.x}px;top:${st.y}px"><header>${icon('i-power', 14)}Запуск</header><div class="v"><span class="meta" id="sc-start-text">${esc(scTrigText())}</span></div>${port('start')}</div>`
    + s.nodes.map(n => {
      const def = SC_NODES[n.kind];
      return `<div class="sc-node k-${n.kind}${scSel === n.id ? ' sel' : ''}" data-n="${esc(n.id)}" style="left:${n.x}px;top:${n.y}px">
        <header>${icon(def.icon, 14)}${esc(def.name)}<button type="button" data-m="del" aria-label="Удалить ноду" title="Удалить ноду">×</button></header>
        <div class="v">${nodeFields(n)}</div>
        ${def.noPorts ? '' : '<span class="port in"></span>' + (def.outs ? port('next', def.outs[0]) + port('else', def.outs[1]) : port('next'))}</div>`;
    }).join('') + '</div>';
  scSoundValues(f);
  scApplyPan();
  scWires();
}
const scFieldStart = () => { const t = $('#sc-start-text'); if (t) t.textContent = scTrigText(); };
function scPortXY(el) {
  const r = el.getBoundingClientRect(), c = $('#sc-field .sc-canvas').getBoundingClientRect();
  return { x: r.left + r.width / 2 - c.left, y: r.top + r.height / 2 - c.top };
}
const scCurve = (a, b) => { const dx = Math.max(40, Math.abs(b.x - a.x) / 2); return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`; };
function scWires(tmp) {
  const c = $('#sc-field .sc-canvas'); if (!c) return;
  const nodes = [...c.querySelectorAll('.sc-node')];
  const inOf = id => c.querySelector(`.sc-node[data-n="${CSS.escape(id)}"] .port.in`);
  const link = (from, port, to) => { const a = c.querySelector(`.sc-node[data-n="${CSS.escape(from)}"] [data-port="${port}"]`), b = to && inOf(to); return a && b ? `<path class="w-${port}" d="${scCurve(scPortXY(a), scPortXY(b))}"/>` : ''; };
  let d = link('start', 'start', scEdit.start);
  scEdit.nodes.forEach(n => { d += link(n.id, 'next', n.next); if (SC_NODES[n.kind].outs) d += link(n.id, 'else', n.else); });
  if (tmp) d += `<path class="tmp" d="${scCurve(tmp.a, tmp.b)}"/>`;
  c.querySelector('.sc-wires').innerHTML = d;
}
function scSetLink(from, port, to) {
  if (from === 'start') scEdit.start = to;
  else scNode(scEdit, from)[port === 'else' ? 'else' : 'next'] = to;
}
$('#sc-field').addEventListener('pointerdown', e => {
  const c = $('#sc-field .sc-canvas'), nodeEl = e.target.closest('.sc-node');
  if (!nodeEl && !e.button) return scPan(e);
  if (!nodeEl || e.button) return;
  const id = nodeEl.dataset.n, cr = () => c.getBoundingClientRect();
  const portEl = e.target.closest('.port.out');
  if (portEl) {
    // тянем связь: отпустили над нодой — выход ведёт к ней, на пустом месте — связь убрана
    e.preventDefault();
    const a = scPortXY(portEl);
    let over = null;
    const move = ev => {
      over?.classList.remove('drop');
      over = document.elementFromPoint(ev.clientX, ev.clientY)?.closest('#sc-field .sc-node:not(.trig):not(.k-note)');
      over?.classList.add('drop');
      scWires({ a, b: { x: ev.clientX - cr().left, y: ev.clientY - cr().top } });
    };
    const up = () => {
      removeEventListener('pointermove', move); removeEventListener('pointerup', up);
      over?.classList.remove('drop');
      scSetLink(id, portEl.dataset.port, over ? over.dataset.n : '');
      scWires();
    };
    addEventListener('pointermove', move); addEventListener('pointerup', up);
    return;
  }
  if (id !== 'start') { scSel = id; $$('#sc-field .sc-node').forEach(x => x.classList.toggle('sel', x === nodeEl)); }
  if (!e.target.closest('header') || e.target.closest('button')) return;
  // перетаскиваем ноду за заголовок; указатель захватываем — перенос не срывается, даже если мышь обогнала ноду
  e.preventDefault();
  const head = e.target.closest('header');
  const p = id === 'start' ? scStartPos() : scNode(scEdit, id), sx = e.clientX - p.x, sy = e.clientY - p.y;
  try { head.setPointerCapture(e.pointerId); } catch {}
  const move = ev => {
    p.x = Math.round(ev.clientX - sx); p.y = Math.round(ev.clientY - sy);
    nodeEl.style.left = p.x + 'px'; nodeEl.style.top = p.y + 'px';
    scWires();
  };
  const up = () => {
    head.removeEventListener('pointermove', move); head.removeEventListener('pointerup', up); head.removeEventListener('pointercancel', up);
    if (id === 'start') scStartSave(scEdit.id, p);
  };
  head.addEventListener('pointermove', move); head.addEventListener('pointerup', up); head.addEventListener('pointercancel', up);
});
// Поле бесконечное: сдвиг холста хранится в редактируемом сценарии (на время правки), фон-сетка едет вместе с ним
const scPanXY = () => scEdit._pan || (scEdit._pan = { x: 0, y: 0 });
function scApplyPan(glide) {
  const c = $('#sc-field .sc-canvas'), p = scPanXY();
  if (!c) return;
  c.classList.toggle('glide', !!glide);
  c.style.transform = `translate(${p.x}px, ${p.y}px)`;
  $('#sc-field').style.backgroundPosition = `${p.x}px ${p.y}px`;
  if (glide) setTimeout(() => c.classList.remove('glide'), 320);
}
// Тянем фон — двигаем поле (мышью и пальцем), а не выделяем текст
function scPan(e) {
  const f = $('#sc-field'), p = scPanXY(), sx = e.clientX - p.x, sy = e.clientY - p.y;
  e.preventDefault();
  try { f.setPointerCapture(e.pointerId); } catch {}
  f.classList.add('panning');
  const move = ev => { p.x = Math.round(ev.clientX - sx); p.y = Math.round(ev.clientY - sy); scApplyPan(); };
  const up = () => {
    f.classList.remove('panning');
    f.removeEventListener('pointermove', move); f.removeEventListener('pointerup', up); f.removeEventListener('pointercancel', up);
  };
  f.addEventListener('pointermove', move); f.addEventListener('pointerup', up); f.addEventListener('pointercancel', up);
}
// На весь экран колесо и тачпад двигают поле; в обычном виде колесо прокручивает страницу
$('#sc-field').addEventListener('wheel', e => {
  if (!document.body.classList.contains('sc-full')) return;
  e.preventDefault();
  const p = scPanXY();
  p.x -= e.shiftKey && !e.deltaX ? e.deltaY : e.deltaX; p.y -= e.shiftKey && !e.deltaX ? 0 : e.deltaY;
  scApplyPan();
}, { passive: false });
$('#sc-field').oninput = scInput;
$('#sc-field').addEventListener('click', e => {
  const b = e.target.closest('[data-m="del"]'); if (!b) return;
  const id = b.closest('.sc-node').dataset.n;
  scEdit.nodes = scEdit.nodes.filter(n => n.id !== id);
  if (scEdit.start === id) scEdit.start = '';
  scEdit.nodes.forEach(n => { if (n.next === id) n.next = ''; if (n.else === id) n.else = ''; });
  if (scSel === id) scSel = null;
  scRender();
});

// ---- добавление нод ----
function renderScPalette() {
  const field = !$('#sc-field').hidden, groups = {};
  Object.entries(SC_NODES).forEach(([k, v]) => {
    if (v.assistant && !scHasAssistant) return;
    if (!field && (v.outs || v.noPorts)) return;   // ветвления и заметки — только на поле
    (groups[v.group] ||= []).push(`<button type="button" class="btn sm" data-add="${k}">${icon(v.icon, 14)}${esc(v.name)}</button>`);
  });
  $('#sc-add-step').innerHTML = Object.entries(groups).map(([g, b]) => `<div class="grp"><span class="meta">${g}</span>${b.join('')}</div>`).join('')
    + (field ? '' : '<p class="hint">Условия, повторы и заметки — на поле.</p>');
}
$('#sc-add-step').onclick = e => {
  const b = e.target.closest('[data-add]'); if (!b) return;
  const kind = b.dataset.add, n = { id: scNextId(), kind, ...SC_NODES[kind].def(), next: '', else: '', x: 0, y: 0 };
  if ($('#sc-field').hidden) {   // список: в конец цепочки
    const chain = scChain(scEdit).chain;
    scEdit.nodes.push(n); scSetChain([...chain, n]);
  } else {
    // поле: рядом с выбранной нодой и сразу с ней связанная, если у неё свободный выход
    const from = scSel && scNode(scEdit, scSel) || null, f = $('#sc-field');
    const base = (scXY(from) ? from : null) || (scEdit.nodes.length ? null : scStartPos());
    n.x = base ? base.x + (base === scStartPos() ? 230 : SC_W + 40) : 40 - scPanXY().x;
    n.y = base ? base.y : 60 - scPanXY().y + (scEdit.nodes.length % 4) * 40;
    scEdit.nodes.push(n);
    if (n.kind !== 'note') {
      if (from && from.kind !== 'note' && !from.next) from.next = n.id;
      else if (!from && !scEdit.start) scEdit.start = n.id;
    }
    scSel = n.id;
  }
  scRender();
  if (!$('#sc-field').hidden) scReveal(n.id);
  $('#sc-board').classList.remove('pal-open');
};
// Новая нода могла встать за краем видимой части поля — подвигаем поле к ней
function scReveal(id) {
  const f = $('#sc-field'), el = f.querySelector(`.sc-node[data-n="${CSS.escape(id)}"]`);
  if (!el) return;
  const r = el.getBoundingClientRect(), fr = f.getBoundingClientRect();
  if (r.left >= fr.left && r.right <= fr.right && r.top >= fr.top && r.bottom <= fr.bottom) return;
  const p = scPanXY();
  p.x = Math.round(fr.width / 2 - el.offsetLeft - el.offsetWidth / 2);
  p.y = Math.round(fr.height / 3 - el.offsetTop);
  scApplyPan(true);
}

// ---- вид, открытие, сохранение ----
function scRender() {
  if (!scEdit) return;
  const field = !$('#sc-field').hidden;
  if (field) renderScField(); else renderScList();
  renderScPalette();
}
function scView(v) {
  if (v === 'list' && !scChain(scEdit).linear) v = 'field';
  $$('#sc-view button').forEach(b => b.classList.toggle('active', b.dataset.v === v));
  $('#sc-view [data-v="list"]').disabled = !scChain(scEdit).linear;
  $('#sc-view [data-v="list"]').title = scChain(scEdit).linear ? '' : 'В сценарии есть ветвления — только поле';
  store.set('sc.view', v);
  $('#sc-steps').hidden = v === 'field';
  $('#sc-field').hidden = $('#sc-field-hint').hidden = $('#sc-arrange').hidden = v !== 'field';
  $$('.sc-arrange-row [data-sc-center]').forEach(b => b.hidden = v !== 'field');
  $('#sc-board').classList.toggle('fieldmode', v === 'field');
  if (v === 'field') scLayout();   // ноды, добавленные в списке, места на поле ещё не имеют
  scRender();
}
$$('#sc-view button').forEach(b => b.onclick = () => scView(b.dataset.v));
$('#sc-arrange').onclick = $('#sc-arrange-full').onclick = () => { scLayout(true); renderScField(); };
// Поле на весь экран: на телефоне — единственный способ его увидеть, на компьютере — кнопка в углу поля
function scFull(open) {
  if (open) scView('field');
  $('#sc-board').classList.toggle('full-open', open);
  if (!open) $('#sc-board').classList.remove('pal-open');
  document.body.classList.toggle('sc-full', open);
  if (scEdit && (open || $('#sc-field').offsetParent)) renderScField();   // размеры поля изменились — связи пересчитать
}
$('#sc-open-field').onclick = $('#sc-expand').onclick = () => scFull(true);
// «К нодам»: ноды — по центру поля, а если не влезают — от левого верхнего края
function scCenter() {
  const f = $('#sc-field'), nodes = [...f.querySelectorAll('.sc-node')];
  if (!nodes.length) return;
  const x0 = Math.min(...nodes.map(n => n.offsetLeft)), y0 = Math.min(...nodes.map(n => n.offsetTop));
  const x1 = Math.max(...nodes.map(n => n.offsetLeft + n.offsetWidth)), y1 = Math.max(...nodes.map(n => n.offsetTop + n.offsetHeight));
  const at = (a, b, size) => Math.round(b - a + 40 > size ? 20 - a : (size - a - b) / 2);
  Object.assign(scPanXY(), { x: at(x0, x1, f.clientWidth), y: at(y0, y1, f.clientHeight) });
  scApplyPan(true);
}
$$('[data-sc-center]').forEach(b => b.onclick = scCenter);
$('#sc-full-close').onclick = () => scFull(false);
$('#sc-fab').onclick = () => $('#sc-board').classList.toggle('pal-open');
$('#sc-pal-backdrop').onclick = () => $('#sc-board').classList.remove('pal-open');
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape' || !document.body.classList.contains('sc-full')) return;
  if ($('#sc-board').classList.contains('pal-open')) $('#sc-board').classList.remove('pal-open'); else scFull(false);
});
addEventListener('hashchange', () => { if (document.body.classList.contains('sc-full')) scFull(false); });
// Ноды без места (из списка) — по порядку цепочки змейкой; all — разложить заново все.
// Ноды, оказавшиеся в одной точке, тоже разводим: иначе верхняя закрывает остальные.
function scLayout(all) {
  const { chain } = scChain(scEdit);
  const order = [...chain, ...scEdit.nodes.filter(n => !chain.includes(n))];
  const seen = new Set();
  order.forEach((n, i) => {
    if (all || !scXY(n) || (!n.x && !n.y) || seen.has(n.x + ',' + n.y)) { n.x = 260 + (i % 3) * (SC_W + 14); n.y = 30 + Math.floor(i / 3) * 190 + (i % 2) * 30; }
    seen.add(n.x + ',' + n.y);
  });
}
function openScene(s) {
  scEdit = structuredClone(s || { id: '', name: '', trigger: 'time', time: '23:00', days: '1234567', on: true, start: '', nodes: [] });
  delete scEdit._start; scSel = null;
  scLayout();
  $('#sc-ed-title').textContent = s ? 'Сценарий' : 'Новый сценарий';
  $('#sc-name').value = scEdit.name;
  $('#sc-trigger').value = scEdit.trigger;
  $('#sc-time').value = scEdit.time || '23:00';
  $('#sc-phrases').value = (scEdit.phrases || []).join(', ');
  $$('#sc-days .chip').forEach(c => c.setAttribute('aria-pressed', scEdit.days.includes(c.dataset.d)));
  $('#sc-del').hidden = !s;
  $('#sc-trigger [value="voice"]').disabled = !scHasAssistant && scEdit.trigger !== 'voice';
  $('#sc-editor').hidden = false;
  scTriggerUI();
  scView(store.get('sc.view') || 'list');
  $('#sc-editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
$('#sc-add').onclick = () => openScene(null);
$('#sc-cancel').onclick = () => { scFull(false); $('#sc-editor').hidden = true; scEdit = null; };
$('#sc-form').onsubmit = e => {
  scFull(false);
  e.preventDefault();
  scLayout();
  const { _start, ...rest } = scEdit;
  const s = { ...rest, name: $('#sc-name').value.trim(), trigger: $('#sc-trigger').value, time: $('#sc-time').value, days: scDays(), phrases: scPhrases() };
  if (s.trigger === 'voice' && !s.phrases.length) return toast('Укажите фразу, по которой ассистент запустит сценарий', true);
  if (!s.nodes.some(n => n.kind !== 'note')) return toast('Добавьте хотя бы один шаг', true);
  const list = s.id ? scData.scenes.map(x => x.id === s.id ? s : x) : [...scData.scenes, s];
  act($('#sc-save'), async () => {
    const r = await scSave(list);
    const saved = s.id ? r.scenes.find(x => x.id === s.id) : r.scenes[r.scenes.length - 1];
    if (scXY(_start) && saved) scStartSave(saved.id, _start);
    $('#sc-editor').hidden = true; scEdit = null;
  }, `Сценарий «${s.name}» сохранён`);
};
$('#sc-del').onclick = e => confirm(`Удалить сценарий «${scEdit.name}»?`) &&
  act(e.currentTarget, async () => { await scSave(scData.scenes.filter(x => x.id !== scEdit.id)); $('#sc-editor').hidden = true; scEdit = null; }, 'Сценарий удалён');

$('#al-cancel').onclick = () => { $('#al-editor').hidden = true; };
$('#al-form').onsubmit = e => {
  e.preventDefault();
  const snd = $('#al-sound').value, opt = $('#al-sound').selectedOptions[0];
  const item = {
    id: alEditing?.id, on: alEditing ? alEditing.on : true, time: $('#al-time').value, days: getDays(),
    mode: $('#al-mode').value, query: $('#al-query').value.trim(), label: $('#al-label').value.trim(),
    sound: snd, sound_name: snd.startsWith('sys:') ? snd.slice(4) : opt?.textContent || '',
    volume: +$('#al-vol').value, ramp: +$('#al-ramp').value, duration: +$('#al-dur').value, snooze: +$('#al-snz').value,
  };
  if (alEditing && (alEditing.time !== item.time || alEditing.days !== item.days)) item.on = true;
  const list = (alarmsData?.alarms || []).filter(a => a.id !== alEditing?.id).map(alToSave);
  list.push(item);
  list.sort((a, b) => a.time.localeCompare(b.time));
  act($('#al-save'), async () => { await saveAlarms(list); $('#al-editor').hidden = true; }, 'Будильник сохранён на колонке');
};
$('#al-del').onclick = e => {
  if (!alEditing || !confirm('Удалить будильник ' + alEditing.time + '?')) return;
  const list = alarmsData.alarms.filter(a => a.id !== alEditing.id).map(alToSave);
  act(e.currentTarget, async () => { await saveAlarms(list); $('#al-editor').hidden = true; }, 'Будильник удалён');
};
$('#al-sound-play').onclick = e => {
  const v = $('#al-sound').value;
  if (!v.startsWith('sys:')) return toast('Свой звук можно послушать кнопкой «Проверить» после сохранения');
  act(e.currentTarget, () => api('/api/play', { name: v.slice(4) }));
};
$('#al-sound-up').onclick = () => $('#al-sound-file').click();
$('#al-sound-file').onchange = async e => {
  const f = e.target.files[0]; e.target.value = '';
  if (!f) return;
  toast('Загружаем ' + f.name + '…');
  const r = await act($('#al-sound-up'), () => api('/api/alarms/sound', f, {
    raw: true, headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(f.name) } }), 'Звук загружен');
  if (r) { alUploaded.push(r); fillSounds(r.sound); }
};
$('#al-install').onclick = e => act(e.currentTarget, async () => { alarmsData = await api('/api/alarms/install', {}); renderAlarms(); }, 'Будильник установлен на колонку');
$('#al-update').onclick = e => act(e.currentTarget, async () => { alarmsData = await api('/api/alarms/install', {}); renderAlarms(); }, 'Скрипт обновлён');
$('#al-uninstall').onclick = e => {
  const n = alarmsData?.alarms.length || 0;
  if (!confirm('Удалить будильник с колонки?' + (n ? ` Все будильники (${n}) и загруженные звуки будут удалены.` : '')
               + ' Строка автозапуска уберётся, если она больше ничему не нужна.')) return;
  act(e.currentTarget, async () => { alarmsData = await api('/api/alarms/uninstall', {}); renderAlarms(); }, 'Будильник удалён с колонки');
};
$('#al-snooze-cancel').onclick = e => act(e.currentTarget, async () => { await api('/api/alarms/cmd', { action: 'cancel_snooze' }); await loadAlarms(); }, 'Отложенный будильник отменён');
$('#ab-stop').onclick = e => act(e.currentTarget, async () => { await api('/api/alarms/cmd', { action: 'stop' }); setTimeout(loadAlarms, 1500); }, 'Будильник выключен');
$('#ab-snooze').onclick = e => act(e.currentTarget, async () => { await api('/api/alarms/cmd', { action: 'snooze' }); setTimeout(loadAlarms, 1500); }, 'Отложено');
// Состояние: на вкладке — часто, на остальных — реже и только если будильники вообще есть.
every(3000, () => {
  if (!online || document.hidden) return;
  const onTab = shownTab === 'alarm';
  if (onTab || alarmsData?.state?.ringing || (alarmsData?.installed && alarmsData.alarms.some(a => a.on))) {
    if (onTab || Date.now() - (loadAlarms.t || 0) > 15000 || alarmsData?.state?.ringing) { loadAlarms.t = Date.now(); loadAlarms(); }
  }
});

// ---------- версия панели и обновление ----------
// Сервер раз в 6 часов спрашивает GitHub о последнем релизе. В «Обзоре» — пометка (её можно скрыть
// до следующей версии), в «Настройках» → «О панели» — само обновление.
let panelVer = null;
const UPD_HOW = {
  zip: 'Панель скачает релиз с GitHub, заменит свои файлы и перезапустится. Вход в Яндекс, пароль, история и папка runtime сохранятся.',
  git: 'Панель установлена через git: выполнит git pull и перезапустится.',
  docker: 'Панель работает в Docker — образ обновляется снаружи командой ниже. Данные в томе /data сохранятся.',
  android: 'Приложение для Android: скачайте новый APK по ссылке «Что нового» и установите поверх — данные сохранятся.',
};
async function loadPanelModel() {
  let p;
  try { p = await api('/api/profile'); } catch { return; }
  // LED-панель (88 диодов через led-play) есть только у Станции 2 — пункт меню по флагу профиля
  $('nav.side a[data-tab="led"]').hidden = !p.ledpanel;
  // Скрываем разделы, не поддерживаемые моделью
  if (p.alarms === false) {
    $$('a[data-tab="alarm"]').forEach(el => el.hidden = true);
    if (location.hash === '#alarm') location.hash = '#overview';
  }
  if (p.scenes === false) {
    $$('a[data-tab="scenes"]').forEach(el => el.hidden = true);
    if (location.hash === '#scenes') location.hash = '#overview';
  }
  if (p.assistant === false) {
    $$('a[data-tab="assistant"]').forEach(el => el.hidden = true);
    if (location.hash === '#assistant') location.hash = '#overview';
  }
  if (p.apps === false) {
    $$('a[data-tab="apps"]').forEach(el => el.hidden = true);
    if (location.hash === '#apps') location.hash = '#overview';
  }
  if (p.alarms === false && p.scenes === false) {
    $$('.mbar-opt').forEach(el => el.hidden = false);
  }
  if (!p.ledpanel && location.hash === '#led') location.hash = '#overview';
  if (p.clock_max) {
    const slider = $('#clock-br');
    slider.max = p.clock_max;
    slider.value = Math.min(+slider.value, p.clock_max);
    $('#clock-br-v').textContent = slider.value;
    clockPreview();
    $('#boot-br').max = p.clock_max;
    setBrush(Math.min(bootBrush(), p.clock_max));
  }
  $('#upd-model').textContent = p.name;
  $('#upd-model-how').textContent = p.auto
    ? 'Определена автоматически по плате колонки. Задать вручную — параметр --model'
    : `Задана вручную: --model ${p.id}. Определять автоматически — --model auto (так по умолчанию)`;
}
async function loadPanelVersion(refresh) {
  try { panelVer = await api('/api/panel/version' + (refresh ? '?refresh=1' : '')); }
  catch (e) {
    if (!e.offline) $('#upd-state').textContent = 'Не удалось проверить обновления: ' + e.message;
    throw e;
  }
  renderPanelVersion();
}
function renderPanelVersion() {
  const v = panelVer; if (!v) return;
  const date = d => d ? new Date(d).toLocaleDateString('ru-RU') : '';
  const uv = $('#upd-version'); if (uv) uv.textContent = v.version;
  const ur = $('#upd-repo'); if (ur) ur.href = v.repo;
  const checked = v.checked ? ' · проверено ' + new Date(v.checked * 1000).toLocaleTimeString('ru-RU') : '';
  const us = $('#upd-state');
  if (us) {
    us.textContent = v.error ? 'Не удалось проверить: ' + v.error
      : v.update ? 'Доступна версия ' + v.latest + checked
      : v.latest ? 'Установлена последняя версия' + checked : 'Нет данных о релизе';
    if (us.classList && us.classList.toggle) us.classList.toggle('warn', !!v.update || !!v.error);
  }
  const row = $('#upd-row'); if (row) row.hidden = !v.update;
  if (v.update) {
    const ul = $('#upd-latest'); if (ul) ul.textContent = v.latest + (v.latest_date ? ` от ${date(v.latest_date)}` : '');
    const uh = $('#upd-how'); if (uh) uh.textContent = v.multi ? 'Обновятся шлюз и все процессы колонок; данные сохранятся.' : (typeof UPD_HOW !== 'undefined' && UPD_HOW[v.install] ? UPD_HOW[v.install] : '');
    const un = $('#upd-notes'); if (un) un.href = v.url || v.repo;
    const ug = $('#upd-go'); if (ug) ug.hidden = v.install === 'docker' || v.install === 'android';
  }
  const cmd = $('#upd-cmd');
  if (cmd) {
    cmd.hidden = !(v.update && v.install === 'docker');
    cmd.textContent = 'docker compose pull && docker compose up -d';
  }
  const ov = $('#ov-update'); if (ov) ov.hidden = !v.update || (typeof store !== 'undefined' && store.get && store.get('upd.hide') === v.latest);
  const ot = $('#ov-update-text'); if (ot) ot.textContent = `Доступна новая версия панели — ${v.latest}`;
}
if ($('#ov-update-hide')) $('#ov-update-hide').onclick = () => { store.set('upd.hide', panelVer?.latest || ''); renderPanelVersion(); };
if ($('#upd-check')) $('#upd-check').onclick = e => act(e.currentTarget, async () => {
  const btn = e.currentTarget; btn.textContent = 'Проверяю…'; if ($('#upd-state')) $('#upd-state').textContent = 'Проверяю релизы на GitHub…';
  try { await loadPanelVersion(true); if (!panelVer.error && !panelVer.update) toast('Проверено: установлена последняя версия'); }
  finally { btn.textContent = 'Проверить'; }
});
if ($('#upd-go')) $('#upd-go').onclick = e => {
  if (!confirm(`Обновить панель до ${panelVer.latest}? Сервер перезапустится, страница обновится сама.`)) return;
  const btn = e.currentTarget;
  return act(btn, async () => {
    btn.textContent = 'Обновляем…';
    try {
      const updated = await api('/api/panel/update', {});
      toast('Обновлено, перезапускаемся…');
      const until = Date.now() + 60000;
      await new Promise(r => setTimeout(r, 2500));
      while (Date.now() < until) {
        try { const r = await PanelContext.fetch('/api/panel/version'); if (r.status === 401 || (r.ok && (await r.json()).version === updated.version)) { location.reload(); return; } } catch {}
        await new Promise(r => setTimeout(r, 1000));
      }
      throw new Error('Сервер не поднялся за минуту — запустите панель вручную');
    } finally { btn.textContent = 'Обновить'; }
  });
};
loadPanelVersion().catch(() => {});
// Раз в 6 часов проверяем GitHub в фоне; скрытая вкладка не опрашивает сеть.
every(6 * 3600 * 1000, () => loadPanelVersion(true).catch(() => {}));

// ---------- подключение ----------
// Сервер сам переподключается к колонке; страница только показывает состояние и после
// подключения (в том числе повторного) заново загружает данные.
let connInfo = null, retryDeadline = 0, connTask = 0;
const CONN_TEXT = {
  server: ['Сервер панели не отвечает', 'Проверьте, что server.py запущен, — страница переподключится сама.', ''],
  connecting: ['Подключаемся к колонке', 'Устанавливаем соединение по adb…', ''],
  waiting: ['Ожидание колонки', 'Колонка пока не в сети: включите её или проверьте Wi‑Fi. Панель подключится сама, как только она появится.', 'wait'],
  adb_error: ['Нет подключения по adb', 'Колонка в сети, но adb не отвечает.', 'err'],
  locked: ['Вход в панель', 'Панель защищена паролем.', 'lock'],
};
function renderConn() {
  const i = connInfo || { state: 'connecting' };
  // адреса нет (первый запуск): панель ищет колонку в сети или ждёт её по USB
  const [title, text, ring] = !i.serial && i.state === 'waiting' && i.state !== 'locked'
    ? ['Ищем колонку', 'Адрес колонки ещё не задан — ищем её в домашней сети. Колонку можно подключить и кабелем USB.', 'wait']
    : CONN_TEXT[i.state] || CONN_TEXT.connecting;
  $('#cs-title').textContent = title;
  let more = '';
  if (i.state === 'adb_error') {
    const left = Math.max(0, Math.ceil((retryDeadline - Date.now()) / 1000));
    more = left ? ` Переподключение через ${left} с.` : ' Переподключаемся…';
    if (i.attempts > 1) more += ` Попытка ${i.attempts}.`;
  }
  $('#cs-text').textContent = text + more;
  $('#cs-addr').innerHTML = i.serial ? secret('ip', i.serial) : '';
  $('#cs-ring').className = 'conn-ring' + (ring ? ' ' + ring : '');
  $('#cs-ring').innerHTML = i.state === 'locked' ? icon('i-lock', 28) : '';
  const locked = i.state === 'locked', form = $('#cs-login');
  if (form.hidden === locked) { form.hidden = !locked; if (locked) setTimeout(() => $('#cs-pass').focus(), 0); }
  $('#cs-retry').hidden = !['adb_error', 'waiting'].includes(i.state) || !i.serial;   // без адреса повторять нечего
  $('#cs-find').hidden = ['locked', 'server', 'online'].includes(i.state) || (!$('#finder').hidden && $('#finder').parentElement.id === 'cs-find-slot');
  maybeAutoFind(i);
  if (i.serial) $('#set-addr').innerHTML = secret('ip', i.serial);
  $('#cs-log-box').hidden = !(i.log && i.log.length);
  $('#cs-report-row').hidden = !(i.log && i.log.length) || i.state === 'locked';
  if (i.log) {
    const p = x => String(x).padStart(2, '0');
    $('#cs-log').textContent = i.log.map(l => {
      const d = new Date(l.t * 1000);
      return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}  ${l.level === 'warn' ? '⚠ ' : ''}${l.msg}`;
    }).join('\n');
  }
  $('#conn-box').hidden = !online;
}
async function connCheck() {
  try {
    const r = await PanelContext.fetch('/api/conn');
    connInfo = r.status === 401 ? { state: 'locked' } : await r.json();
  } catch { connInfo = { state: 'server' }; }
  if (connInfo.retry_in != null) retryDeadline = Date.now() + connInfo.retry_in * 1000;
  const was = online;
  online = connInfo.state === 'online';
  renderConn();
  if (online !== was) { online ? onConnected() : showTab(); updateMinibar(); }
  Scheduler.setInterval(connTask, online ? 3000 : connInfo.state === 'locked' ? 10000 : 1000);
}
connTask = every(3000, connCheck);
$('#cs-login').onsubmit = async e => {
  e.preventDefault();
  const btn = $('#cs-login-btn'), err = $('#cs-login-err');
  btn.disabled = true; err.textContent = '';
  try {
    const r = await PanelContext.fetch('/api/panel-auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                     body: JSON.stringify({ password: $('#cs-pass').value }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.statusText);
    $('#cs-pass').value = '';
    await connCheck();
    loadAccess();
  } catch (x) { err.textContent = x.message; $('#cs-pass').select(); }
  finally { btn.disabled = false; }
};
$('#cs-retry').onclick = e => act(e.currentTarget, async () => { await PanelContext.fetch('/api/conn/retry', { method: 'POST' }); await connCheck(); });

let statusTask = 0;
// ---------- поиск колонки в сети ----------
// Тот же поиск, что ./start.sh --find: порт adb 5555 в подсетях компьютера, затем проверка платы.
// Выбранный адрес панель запоминает и перезапускается с ним.
$$('[data-finder]').forEach(b => b.onclick = () => {
  const f = $('#finder'), slot = $('#' + b.dataset.finder);
  const open = f.hidden || f.parentElement !== slot;
  slot.append(f); f.hidden = !open;
  if (b.id === 'cs-find') b.hidden = open;
  if (open && !$('#find-list').children.length) $('#find-go').click();
});
// Поиск идёт на сервере в фоне; страница опрашивает ход и показывает, что сейчас проверяется.
const FIND_STAGE = {
  nets: () => 'Определяю домашнюю сеть…',
  ports: (d, t) => `Ищу устройства с adb: проверено ${d} из ${t} адресов`,
  check: (d, t) => t ? `Нашлись устройства с adb — проверяю, колонки ли это (${d} из ${t})` : 'Устройств с adb не нашлось',
};
async function findSpeakers() {
  const call = async method => (await PanelContext.fetch('/api/conn/find', { method })).json();
  $('#find-list').innerHTML = '';
  $('#find-progress').hidden = false; $('#find-progress').value = 0;
  let r;
  try {
    r = await call('POST');
    while (r.running && !r.error) {
      $('#find-state').textContent = (FIND_STAGE[r.stage] || FIND_STAGE.nets)(r.done, r.total);
      $('#find-progress').value = r.total ? (r.stage === 'check' ? .9 + .1 * r.done / r.total : .9 * r.done / r.total) : 0;
      await new Promise(res => setTimeout(res, 400));
      r = await call('GET');
    }
  } catch { r = { error: 'сервер панели не ответил' }; }
  $('#find-progress').hidden = true;
  if (r.error) { $('#find-state').textContent = r.error; return; }
  $('#find-state').textContent = r.found.length ? `Сеть ${r.nets.join(', ')}:` : `В сети ${r.nets.join(', ')} колонок не нашлось: проверьте, что колонка включена, в той же сети и adb по сети на ней включён. Можно ввести IP вручную.`;
  $('#find-list').innerHTML = r.found.map(x => `<div class="find-row"><div class="nm"><b>${esc(x.name)}</b><span>${secret('ip', x.ip)}</span></div>
    ${x.current ? '<span class="badge ok">подключена</span>' : `<button class="btn sm primary" data-use="${esc(x.serial)}">Подключить</button>`}</div>`).join('');
  $$('#find-list [data-use]').forEach(b => b.onclick = () => useSpeaker(b.dataset.use, b));
  // первый запуск без адреса: одна колонка в сети — сервер сам запомнил её и перезапускается
  if (r.auto) {
    $('#find-state').textContent = `Нашлась ${r.found[0].name} — подключаю…`;
    $('#find-list').innerHTML = '';
    setTimeout(connCheck, 1500);
  }
}
$('#find-go').onclick = e => act(e.currentTarget, () => findSpeakers());
// Адреса ещё нет (первый запуск через start.bat / start.sh): открываем поиск на экране подключения сами
let autoFindDone = false;
function maybeAutoFind(i) {
  if (autoFindDone || i.serial || online || !['waiting', 'adb_error'].includes(i.state)) return;
  autoFindDone = true;
  const f = $('#finder');
  $('#cs-find-slot').append(f); f.hidden = false;
  act($('#find-go'), () => findSpeakers());
}
async function useSpeaker(serial, btn) {
  await act(btn, async () => {
    const r = await (await PanelContext.fetch('/api/conn/use', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ serial }) })).json();
    if (r.error) throw new Error(r.error);
    $('#finder').hidden = true;
    setTimeout(connCheck, 1500);   // сервер перезапускается — экран подключения покажет, как идёт
  }, 'Панель перезапускается с колонкой ' + serial.replace(/:5555$/, ''));
}
$('#find-manual').onsubmit = e => { e.preventDefault(); const ip = $('#find-ip').value.trim(); if (ip) useSpeaker(ip, e.submitter); };

// ---------- хук загрузки ----------
// Своё на колонке (звуки, анимации, будильник, ассистент…) после перезагрузки держится на строке в rootkeeper.sh.
// Панель подключает её сама при сохранении; плашка — на случай, когда строки нет или она от старой версии.
let hookInfo = null;
async function loadHook() {
  try { hookInfo = await api('/api/hook'); } catch { return; }
  renderHook();
}
function renderHook() {
  const w = $('#hook-warn'), h = hookInfo;
  w.hidden = !h || !h.broken;
  if (w.hidden) return;
  const what = h.users.join(', ');
  $('#hook-warn-title').textContent = 'После перезагрузки колонки своё пропадёт';
  $('#hook-warn-text').textContent = !h.possible
    ? `На колонке есть своё (${what}), но подключить его к загрузке не к чему: нет ${h.rootkeeper}, штатная загрузка его не вызывает, а /etc/init.d — на корне только для чтения. Кнопка ниже положит туда свой сценарий автозапуска, на секунду перемонтировав корень в режим записи и вернув как было. До перезагрузки всё работает, после — вернутся стандартные, а будильник и ассистент не запустятся.`
    : h.hook
      ? `Хук запуска (${h.file || h.rootkeeper}) — от прошлой версии панели и подключит не всё (${what}). Обновите её.`
      : `На колонке есть своё (${what}), но хук запуска не подключён — после перезагрузки вернутся стандартные, а будильник и ассистент не запустятся.`;
  $('#hook-repair').hidden = h.supported === false;
  if (h.supported === false) $('#hook-warn-text').textContent = h.reason || 'Автозапуск для этой модели пока не поддерживается';
  $('#hook-repair').textContent = h.hook ? 'Обновить хук' : 'Подключить хук';
  const head = $('main[data-page]:not([hidden]) .page-head');
  if (head && w.parentElement !== head) head.append(w);
}
$('#hook-repair').onclick = e => {
  if (hookInfo?.supported === false) return toast(hookInfo.reason, true);
  const remount = hookInfo && !hookInfo.possible;
  if (remount && !confirm('Чтобы положить сценарий автозапуска в /etc/init.d, панель на секунду перемонтирует '
    + 'корень колонки в режим записи и вернёт его в только-чтение. Продолжить?')) return;
  act(e.currentTarget, async () => {
    hookInfo = await api('/api/hook/repair', remount ? { remount: true } : {});
    renderHook();
    if (bootLoaded) loadBoot();
  }, 'Хук подключён — своё переживёт перезагрузку');
};

function onConnected() {
  // После переподключения всё, что могло не загрузиться, грузим заново.
  fs.loaded = false; logsLoaded = false; bootLoaded = false;
  showTab();
  refreshStatus();
  if (statusTask) Scheduler.remove(statusTask);
  statusTask = every(5000, refreshStatus);
  loadSounds().catch(e => { if (!e.offline) setListHtml($('#sounds'), `<div class="empty">${esc(e.message)}</div>`); });
  loadSay();
  loadSparks();
  loadViz();
  api('/api/patterns').then(p => { patterns = p; renderPatterns(); }).catch(e => e.offline || toast(e.message, true));
  loadMusic();
  loadDispState();
  loadAlarms();
  loadBt();
  loadDSP();
  loadPanelModel();
}

// ---------- старт ----------
renderConn();
connCheck();
// A fast local worker can connect before the vendor scripts below have loaded.
document.addEventListener('DOMContentLoaded',()=>{if(online&&!$('#tab-console').hidden)termConnect();});
