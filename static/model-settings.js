// Visual preferences: shared body colour in the live panel; local spin and gallery appearance.
import {preferenceKey, readPreferences} from './model-color.js';
export function modelSettings(id, palette, actions) {
  const section = document.getElementById('model-settings');
  if (!section) return;
  const key = preferenceKey(id), saved = readPreferences(id);
  const controller = window.__modelColorController;
  section.innerHTML =
    '<label class="set-item"><div class="set-label"><b>Цвет 3D-модели</b><small>' +
    (controller ? 'Сохраняется для этой колонки во всех браузерах' : 'Только картинка в браузере — подсветка колонки не меняется') + '</small></div>' +
    '<div class="set-ctl"><select class="field" id="model-color" aria-label="Цвет 3D-модели" style="min-height:36px"></select></div></label>' +
    '<label class="set-item"><div class="set-label"><b>Автовращение 3D-модели</b><small>Модель медленно вращается сама</small></div>' +
    '<div class="set-ctl"><input type="checkbox" class="switch" id="model-spin" aria-label="Автовращение 3D-модели"></div></label>' +
    '<div class="set-item"><div class="set-label"><b>Ракурс 3D-модели</b><small>Если модель повернули или приблизили мышью</small></div>' +
    '<div class="set-ctl"><button class="btn sm" id="model-home" type="button">Вернуть исходный</button></div></div>';
  const color = section.querySelector('#model-color'), spin = section.querySelector('#model-spin');
  color.add(new Option(controller ? 'Автоопределение' : 'Как у колонки', 'auto'));
  for (const [name, [label]] of Object.entries(palette)) color.add(new Option(label, name));
  color.value = Object.hasOwn(palette, saved.color) ? saved.color : 'auto';
  spin.checked = saved.spin === true;
  function localApply() {
    const next = readPreferences(id);
    next.spin = spin.checked;
    if (!controller) next.color = color.value === 'auto' ? null : color.value;
    try { localStorage.setItem(key, JSON.stringify(next)); } catch {}
    if (!controller) actions.color?.(next.color);
    actions.spin?.(spin.checked);
  }
  color.addEventListener('change', () => controller ? void controller.save(color.value === 'auto' ? null : color.value) : localApply());
  spin.addEventListener('change', localApply);
  section.querySelector('#model-home').addEventListener('click', () => actions.home?.());
  section.hidden = false;
  if (controller) controller.attach(color, actions);
  localApply();
}
