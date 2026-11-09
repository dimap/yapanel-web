import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';

const app = await readFile(new URL('../static/app.js', import.meta.url), 'utf8');
const html = await readFile(new URL('../static/index.html', import.meta.url), 'utf8');

// The mobile bottom bar has at least five visible items; a fixed four-column
// grid wrapped the last one under the viewport.
const mbarRule = html.match(/\.mbar\s*\{([^}]*)\}/)?.[1] || '';
assert(!mbarRule.includes('repeat(4,'), 'mobile bar must not force four columns');
assert(/\.mbar a, \.mbar button\s*\{[^}]*flex:\s*1 1 0/.test(html), 'mobile bar items must share width');

// Every id (and every tab page) must be unique: a duplicated <main> makes the
// browser show two copies of the same page side by side.
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
const duplicateIds = ids.filter((id, index) => ids.indexOf(id) !== index);
assert.deepEqual(duplicateIds, [], `duplicate ids in index.html: ${duplicateIds.join(', ')}`);
const pageIds = [...html.matchAll(/<main[^>]*\bid="(tab-[^"]+)"/g)].map(match => match[1]);
assert.equal(new Set(pageIds).size, pageIds.length, 'each tab page must appear once');

// ---------- 1. Единый visibility-aware планировщик ----------
assert(app.includes('const Scheduler = (() => {'), 'Scheduler must exist');
assert(app.includes("document.addEventListener('visibilitychange'"), 'Scheduler must listen to visibilitychange');
assert(app.includes('document.hidden'), 'Scheduler must check document.hidden');
assert(app.includes('const every = (ms, fn) => Scheduler.add(ms, fn)'), 'every() wrapper must exist');
for (const legacy of [
  'setInterval(updateSub', 'setInterval(loadDispState', 'setInterval(loadMusic',
  'setInterval(refreshStatus', 'setInterval(loadSparks', 'setInterval(loadBt',
  'setInterval(loadParty', 'setInterval(loadAssistant', 'setInterval(loadCharts',
  'setInterval(loadScenes', 'setInterval(loadAlarms', 'setInterval(connCheck',
]) assert(!app.includes(legacy), `legacy timer still present: ${legacy}`);
assert(!app.includes('setTimeout(pollLeds'), 'LED fallback must not reschedule itself');
assert(!app.includes('setTimeout(liveLeds'), 'LED live poll must not reschedule itself');
assert(app.includes('if (!online || document.hidden || !$(\'#tab-party\')') ||
       app.includes("if (!online || document.hidden || !$('#tab-party')"), 'party poll must be visibility-aware');
assert(app.includes("if (!online || document.hidden || $('#tab-assistant').hidden) return;"), 'assistant poll must be visibility-aware');
assert(app.includes("if (!online || document.hidden || shownTab !== 'charts') return;"), 'charts poll must be visibility-aware');
assert(app.includes("if (!online || document.hidden || $('#tab-scenes').hidden) return;"), 'scenes poll must be visibility-aware');

// Исполняем сам планировщик с поддельными часами: скрытая вкладка не будит задачи,
// а при возврате задача выполняется сразу.
const start = app.indexOf('const Scheduler = (() => {');
const blockEnd = '\n})();';
const end = app.indexOf(blockEnd, start);
assert(start >= 0 && end > start, 'scheduler block must be extractable');
const schedulerSource = app.slice(start, end + blockEnd.length) + '\n;globalThis.__Scheduler = Scheduler;';

let now = 0, timers = [], timerSeq = 0;
const fakeSetTimeout = (fn, ms) => {
  const timer = { id: ++timerSeq, at: now + Math.max(0, ms), fn, canceled: false };
  timers.push(timer);
  return timer;
};
const fakeClearTimeout = timer => { if (timer) timer.canceled = true; };
const listeners = {};
const fakeDocument = {
  hidden: false,
  addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
};
const schedulerContext = {
  document: fakeDocument,
  setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout,
  Date: { now: () => now },
};
vm.createContext(schedulerContext);
vm.runInContext(schedulerSource, schedulerContext);
const Scheduler = schedulerContext.__Scheduler;
let calls = 0;
Scheduler.add(1000, () => { calls++; });
const advance = ms => {
  const target = now + ms;
  for (let guard = 0; guard < 10000; guard++) {
    timers = timers.filter(timer => !timer.canceled);
    const due = timers.filter(timer => timer.at <= target).sort((a, b) => a.at - b.at)[0];
    if (!due) break;
    now = due.at;
    timers = timers.filter(timer => timer !== due);
    due.fn();
  }
  now = target;
};
advance(2500);
assert.equal(calls, 2, 'visible scheduler runs periodic tasks');
fakeDocument.hidden = true;
for (const fn of listeners.visibilitychange || []) fn();
const beforeHidden = calls;
advance(5000);
assert.equal(calls, beforeHidden, 'hidden scheduler must not run tasks');
fakeDocument.hidden = false;
for (const fn of listeners.visibilitychange || []) fn();
advance(0);
assert.equal(calls, beforeHidden + 1, 'visible scheduler catches up immediately');

// ---------- 2. Экранирование пользовательских данных ----------
assert(app.includes('<span>${esc(b.name)}</span>'), 'lamp name must be escaped');
assert(app.includes('data-key="${esc(key)}" data-v="${esc(v)}" data-dots="${esc(dots)}"'), 'secret() attributes must be escaped');
assert(app.includes('aria-label="Редактировать лампу ${esc(b.name)}"'), 'lamp edit button must name the lamp');
assert(app.includes('aria-label="Удалить лампу ${esc(b.name)}"'), 'lamp delete button must name the lamp');
assert(app.includes('esc(a.label || daysText(a.days))'), 'alarm label must be escaped');
assert(app.includes('esc(s.name)'), 'scene name must be escaped');
assert(app.includes('esc(e.name)'), 'file name must be escaped');
assert(app.includes('esc(h.question)'), 'assistant history question must be escaped');
assert(app.includes('out.textContent'), 'logs must be rendered as text, not HTML');
assert(app.includes("${tag ? `<span class=\"badge${cur ? ' ok' : ''}\">${esc(tag)}</span>`"), 'track tag must be escaped');
assert(app.includes('cssUrl(url)'), 'cover URL must go through cssUrl()');
for (const raw of [
  '${b.name}</span>', '${x.name}</b>', '${a.label}</b>', '${s.name}</b>',
  '${e.name}</span>', '${h.question}</b>', '${a.query}</div>', '${last.name}</b>',
]) assert(!app.includes(raw), `raw non-escaped template found: ${raw}`);

// ---------- 3. Производительность: списки не перерисовываются зря ----------
assert(app.includes('function setListHtml(el, html)'), 'setListHtml helper must exist');
for (const marker of [
  "setListHtml($('#bt-list')", "setListHtml($('#al-list')", "setListHtml($('#sc-list')",
  'setListHtml(listEl,', 'setListHtml(body,', "setListHtml($('#sounds')", "setListHtml($('#as-hist')",
]) assert(app.includes(marker), `large list must use setListHtml: ${marker}`);
assert(app.includes('function renderMusicProgress()'), 'progress-only renderer must exist');
assert(app.includes('every(250, () => muState()?.playing && !mu.seeking && renderMusicProgress())'),
  '250 ms tick must update only progress');

// ---------- 4. Доступность и подтверждения ----------
assert(html.includes(':focus-visible {'), 'global focus-visible style must exist');
assert(html.includes('id="toast" role="status" aria-live="polite"'), 'toast must be announced');
assert(html.includes('id="alarm-bar" role="alert"'), 'alarm bar must be announced');
assert(html.includes('id="al-sound-play" title="Послушать" aria-label="Послушать выбранный звук"'), 'sound play button needs aria-label');
assert(html.includes('id="al-sound-up" title="Загрузить свой" aria-label="Загрузить свой звук"'), 'sound upload button needs aria-label');
assert(html.includes('id="dsp-eq-dialog"') && html.includes('id="dsp-open-eq"'), 'full frequency-response dialog must exist');
assert(app.includes("action: 'set_eq'"), 'EQ dialog must save bands through /api/dsp');
assert(html.includes('id="party-bulb-cancel-x" title="Закрыть" aria-label="Закрыть редактор лампы"'), 'bulb editor close needs aria-label');
assert(html.includes('id="party-scan-close-btn" title="Закрыть" aria-label="Закрыть поиск ламп"'), 'scan close needs aria-label');
assert(html.includes('id="party-import-close-btn" title="Закрыть" aria-label="Закрыть импорт ламп"'), 'import close needs aria-label');
assert(app.includes('function fsAction(a, e)') && /Удалить \$\{e\.is_dir/.test(app), 'fs delete must ask for confirmation');
assert(app.includes('confirm(\'Удалить с колонки всё, что поставила панель?'), 'footprint reset must ask for confirmation');
assert(app.includes('confirm(\'Удалить эту лампу из светомузыки?\')'), 'lamp delete must ask for confirmation');

console.log('Scheduler visibility, escaping, list rendering, ARIA and confirmation cases passed');
