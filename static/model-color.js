import {modelPalette, normalizeColor} from './model-colors.js';

const setText = (node, text) => { if (node.textContent !== text) node.textContent = text; };
export function preferenceKey(id) {
  if (window.__modelPreview) return 'model.preview.preferences.' + id;
  return 'model.preferences.' + (window.PanelContext?.deviceId || 'single') + '.' + id;
}
export function readPreferences(id) {
  try {
    const raw = localStorage.getItem(preferenceKey(id)) ||
      (id === 'lite2nc' && localStorage.getItem(preferenceKey('lite2')));
    const value = JSON.parse(raw || '{}');
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch { return {}; }
}

// Starts before WebGL: a failed renderer must not hide the colour question or settings.
export async function initModelColor(id, initialStatus) {
  const panel = document.getElementById('model-color-panel');
  if (!panel || !modelPalette(id)) return null; // Gallery never calls the live API.
  const palette = modelPalette(id), saved = readPreferences(id);
  let legacy = normalizeColor(id, saved.color), state = null, online = false;
  let busy = false, refreshing = false, needsRefresh = true, generation = 0, mode = '';
  let settings = null, actions = {}, displayed = null, error = '', observedDevice = null;
  panel.innerHTML = '<p class="hint" data-color-caption aria-live="polite"></p>' +
    '<form data-color-form hidden><label for="model-color-answer" data-color-question></label>' +
    '<div class="model-color-controls"><select class="field" id="model-color-answer" aria-label="Цвет корпуса станции" required></select>' +
    '<button class="btn sm" type="submit">Сохранить</button></div></form>' +
    '<p class="hint" data-color-error role="status" hidden></p>';
  const caption = panel.querySelector('[data-color-caption]'), form = panel.querySelector('form');
  const question = panel.querySelector('[data-color-question]'), answer = form.querySelector('select');
  const button = form.querySelector('button'), errorBox = panel.querySelector('[data-color-error]');
  answer.add(new Option('Выберите цвет', ''));
  for (const [key, [label]] of Object.entries(palette)) answer.add(new Option(label, key));

  function effective() {
    if (!state) return displayed;
    return state.configured ? state.effective : legacy || state.effective;
  }
  function paint() {
    const color = effective();
    displayed = color;
    window.__deviceColor = color;
    if (actions.color) actions.color(color);
    else window.modelColor?.(color);
  }
  function render() {
    const canChoose = online && state?.device_id && !busy && !refreshing;
    const migration = state && !state.configured && legacy;
    const ask = canChoose && (migration || (!state.effective && ['command_missing', 'empty', 'unsupported'].includes(state.reason)));
    const nextMode = ask ? migration ? 'migration' : 'unknown' : '';
    if (nextMode !== mode) {
      answer.value = migration || '';
      mode = nextMode;
    }
    form.hidden = !ask;
    setText(question, migration
      ? 'В этом браузере уже выбран цвет. Сохранить его для этой колонки во всех браузерах?'
      : 'Не удалось определить цвет корпуса. Какого цвета ваша станция?');
    button.disabled = !canChoose || !answer.value;
    answer.disabled = !canChoose;
    let text = '';
    if (state && effective()) {
      const label = palette[effective()][0];
      text = migration ? 'Цвет выбран только в этом браузере: ' + label + '.'
        : 'Цвет корпуса: ' + label + (state.source === 'user' ? ' — выбран пользователем.' : ' — определён автоматически.');
    } else if (online && state && !['command_failed'].includes(state.reason)) {
      text = 'Показан условный цвет корпуса.';
    }
    if (!online && state) text += ' Нет связи с колонкой; цвет сохранён на экране.';
    if (online && state?.reason === 'command_failed' && state.source !== 'user') text = 'Не удалось прочитать цвет. Повторим после восстановления связи.';
    if (online && state && !state.device_id) text += ' Не удалось определить колонку для сохранения цвета.';
    setText(caption, text);
    setText(errorBox, error); errorBox.hidden = !error;
    panel.hidden = !text && !ask && !error;
    if (settings) {
      settings.disabled = !canChoose;
      settings.options[0].textContent = state?.detected
        ? 'Авто: ' + palette[state.detected][0] : 'Автоопределение (цвет неизвестен)';
      settings.value = migration || state?.selected || 'auto';
    }
  }
  async function request(options) {
    const response = await PanelContext.fetch('/api/model-color' + (options ? '' : '?model=' + encodeURIComponent(id)), options);
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || 'Не удалось прочитать цвет корпуса');
    return value;
  }
  async function refresh() {
    if (refreshing || busy || !online) return;
    refreshing = true;
    const token = ++generation;
    render();
    try {
      const value = await request();
      if (token !== generation) return;
      state = value;
      needsRefresh = false; error = '';
      paint();
    } catch (e) {
      if (token === generation) { error = e.message; needsRefresh = true; }
    } finally {
      refreshing = false;
      render();
    }
  }
  async function save(color) {
    if (!online || !state?.device_id || busy || refreshing) return;
    busy = true; error = '';
    const token = ++generation;
    render();
    try {
      const value = await request({method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({model: id, color, device_id: state.device_id})});
      if (token !== generation) { needsRefresh = true; return; }
      state = value; legacy = null; needsRefresh = false;
      try {
        const old = readPreferences(id); delete old.color;
        localStorage.setItem(preferenceKey(id), JSON.stringify(old));
      } catch {} // Server success is authoritative even when browser storage is unavailable.
      paint();
    } catch (e) {
      if (token === generation) error = e.message;
    } finally {
      busy = false; render();
    }
  }
  form.addEventListener('submit', event => { event.preventDefault(); if (answer.value) void save(answer.value); });
  answer.addEventListener('change', () => { button.disabled = !answer.value || busy; });
  const controller = {
    save,
    attach(select, nextActions) {
      settings = select; actions = nextActions;
      if (state) paint();
      render();
    },
    async updateStatus(status) {
      if (!status) {
        online = false; needsRefresh = true; generation++;
        render(); return;
      }
      online = true;
      const device = status.model_color_device_id;
      if (device && observedDevice && observedDevice !== device) {
        generation++;
        state = null; legacy = null; needsRefresh = true;
        displayed = null; window.__deviceColor = null; window.modelColor?.(null);
      }
      if (device) observedDevice = device;
      if (needsRefresh) { await refresh(); return; }
      if (!state || busy || refreshing) return;
      const detection = status.color_detection;
      if (detection?.state === 'error') {
        state.reason = 'command_failed'; render(); return; // Preserve the last painted value.
      }
      state.detected = normalizeColor(id, status.color);
      state.effective = state.selected || state.detected;
      state.source = state.selected ? 'user' : state.detected ? 'device' : 'unknown';
      state.reason = state.detected ? null : status.color?.trim() ? 'unsupported' : detection?.reason || 'empty';
      error = ''; paint(); render();
    }
  };
  window.__modelColorController = controller;
  if (initialStatus?.hostname || initialStatus?.hardware_model) await controller.updateStatus(initialStatus);
  return controller;
}
