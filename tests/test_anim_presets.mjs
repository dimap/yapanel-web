// Проверки генераторов готовых анимаций: размеры, яркость, циклы, детерминизм.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const root = new URL('../', import.meta.url);
const read = path => readFileSync(new URL(path, root), 'utf8');

const sandbox = { module: { exports: {} } };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(read('static/anim-presets.js'), sandbox);
const presets = sandbox.module.exports;

assert.equal(presets.version, 1);
assert.equal(presets.matrix.length, 10, 'десять анимаций экрана');
assert.equal(presets.ring.length, 6, 'шесть анимаций кольца');

const duration = frames => frames.reduce((sum, f) => sum + f.ms, 0);

for (const preset of presets.matrix) {
  const built = preset.build();
  const frames = built.frames;
  assert.ok(frames.length >= 20, `${preset.id}: достаточно кадров`);
  assert.ok(duration(frames) >= 1500 && duration(frames) <= 7000, `${preset.id}: длительность цикла`);
  assert.ok(frames[0].flag, `${preset.id}: первый кадр помечен для цикла`);
  assert.ok(frames[0].v.some(v => v > 0), `${preset.id}: первый кадр не пустой`);
  for (const frame of frames) {
    assert.equal(frame.v.length, 400, `${preset.id}: кадр 25×16`);
    assert.ok(frame.ms >= 10 && frame.ms <= 65535, `${preset.id}: длительность кадра`);
    for (const value of frame.v) {
      assert.ok(Number.isInteger(value) && value >= 0 && value <= 255,
        `${preset.id}: яркость 0..255`);
    }
  }
  const mid = frames[Math.floor(frames.length / 2)];
  assert.ok(frames[0].v.some((value, i) => value !== mid.v[i]), `${preset.id}: кадры меняются`);
  assert.deepEqual(preset.build(), built, `${preset.id}: генератор детерминирован`);
}

for (const preset of presets.ring) {
  const built = preset.build();
  const frames = built.frames;
  assert.ok(frames.length >= 20, `${preset.id}: достаточно кадров`);
  assert.ok(built.loop === true, `${preset.id}: кольцо зациклено`);
  assert.ok(built.text.startsWith('loop\n'), `${preset.id}: loop в .led`);
  assert.ok(Buffer.byteLength(built.text, 'utf8') < 64 * 1024, `${preset.id}: файл не огромный`);
  for (const frame of frames) {
    assert.equal(frame.c.length, 24, `${preset.id}: 24 LED`);
    assert.ok(frame.ms >= 10 && frame.ms <= 65535, `${preset.id}: длительность кадра`);
    for (const color of frame.c) {
      assert.equal(color.length, 3, `${preset.id}: RGB`);
      for (const value of color) {
        assert.ok(Number.isInteger(value) && value >= 0 && value <= 255,
          `${preset.id}: цвет 0..255`);
      }
    }
  }
  const mid = frames[Math.floor(frames.length / 2)];
  assert.ok(frames[0].c.some((color, i) => color.some((value, k) => value !== mid.c[i][k])),
    `${preset.id}: кадры меняются`);
  assert.deepEqual(preset.build(), built, `${preset.id}: генератор детерминирован`);
  const firstData = built.text.split('\n')[1].trim().split(/\s+/);
  assert.equal(firstData.length, 25, `${preset.id}: 24 цвета и длительность`);
  assert.ok(firstData.every(token => /^[0-9a-f]{6}$|^\d+$/.test(token)), `${preset.id}: формат .led`);
}

const html = read('static/index.html');
assert.ok(html.includes('static/anim-presets.js?v='), 'скрипт галереи подключён');
assert.ok(html.includes('id="matrix-preset-strip"'), 'галерея экрана есть в разметке');
assert.ok(html.includes('id="ring-preset-strip"'), 'галерея кольца есть в разметке');
assert.ok(html.includes('id="matrix-library"') && html.includes('id="ring-library"'), 'контейнеры галерей');
const app = read('static/app.js');
for (const name of ['mountPresetGallery', 'applyMatrixPreset', 'applyRingPreset', 'startPresetTicker']) {
  assert.ok(app.includes(`function ${name}`), `${name} есть в app.js`);
}
assert.ok(!app.includes('innerHTML = `<canvas'), 'карточки собираются без вложенного canvas');

console.log('Animation presets: sizes, brightness, loops, determinism and UI hooks passed');
