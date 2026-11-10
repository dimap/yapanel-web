// Готовые анимации для LED-экрана 25×16 и кольца.
// Чистые генераторы без DOM: кадры детерминированы, поэтому предпросмотр,
// установка и тесты видят одно и то же. Экран монохромный — яркость 0..255.
(function (global) {
  'use strict';

  const COLS = 25, ROWS = 16, PIXELS = COLS * ROWS;
  const TAU = Math.PI * 2;

  const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
  const byte = v => clamp(Math.round(v), 0, 255);
  const blank = () => new Array(PIXELS).fill(0);

  // mulberry32: одинаковые кадры при каждом открытии панели и в тестах.
  function rng(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function put(v, x, y, val) {
    x = Math.round(x); y = Math.round(y);
    if (x < 0 || x >= COLS || y < 0 || y >= ROWS) return;
    const i = y * COLS + x, b = byte(val);
    if (b > v[i]) v[i] = b;
  }

  function line(v, x0, y0, x1, y1, val) {
    const steps = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0)) | 0;
    if (!steps) return put(v, x0, y0, val);
    for (let i = 0; i <= steps; i++) {
      const t = i / steps;
      put(v, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, val);
    }
  }

  function glow(v, x, y, val) {
    put(v, x, y, val);
    put(v, x - 1, y, val * 0.28);
    put(v, x + 1, y, val * 0.28);
    put(v, x, y - 1, val * 0.28);
    put(v, x, y + 1, val * 0.28);
  }

  function frameSet(count, ms, fn) {
    const frames = [];
    for (let i = 0; i < count; i++) {
      frames.push({ flag: i === 0 ? 1 : 0, v: fn(i, TAU * i / count), ms });
    }
    return { frames };
  }

  // ---------- экран 25×16 ----------

  // Туннель из тонких колец с вращающимися лучами и ярким центром.
  function portal() {
    return frameSet(40, 72, (i, t) => {
      const v = blank();
      for (let y = 0; y < ROWS; y++) {
        for (let x = 0; x < COLS; x++) {
          const dx = (x - 12) / 12.5, dy = (y - 7.5) / 7.5;
          const r = Math.hypot(dx, dy), a = Math.atan2(dy, dx);
          const ring = Math.exp(-Math.pow((r - 0.72) / 0.145, 2));
          let val = ring * (150 + 105 * Math.sin(a * 3 + t * 3));
          val += Math.pow(Math.max(0, Math.sin(a * 4 + t * 2)), 8)
            * 120 * Math.exp(-Math.abs(r - 0.78) / 0.3);
          val += Math.sin(r * 26 - t * 2) * 12 * Math.max(0, 1 - r / 1.2);
          val += Math.exp(-r * r * 10) * 60;
          v[y * COLS + x] = byte(val);
        }
      }
      return v;
    });
  }

  // Плазма: несколько плавающих сгустков света, как в лавовой лампе.
  function plasma() {
    return frameSet(36, 90, (i, t) => {
      const v = blank();
      const blobs = Array.from({ length: 6 }, (_, b) => [
        12.5 + 8.4 * Math.sin(t + b * 1.7),
        7.5 + 5.2 * Math.cos(t * 2 + b * 2.3),
        2.1 + 0.9 * Math.sin(t * 3 + b * 1.1),
        0.72 + 0.28 * Math.sin(t * 2 + b * 0.8),
      ]);
      for (let y = 0; y < ROWS; y++) {
        for (let x = 0; x < COLS; x++) {
          let val = 0;
          for (const [bx, by, br, bs] of blobs) {
            const dx = (x - bx) / br, dy = (y - by) / (br * 0.85);
            val += Math.exp(-(dx * dx + dy * dy) / 2) * bs;
          }
          v[y * COLS + x] = byte(255 * Math.pow(Math.min(1, val * 0.85), 2.0));
        }
      }
      return v;
    });
  }

  // Полёт сквозь звёзды: точки летят из центра и оставляют длинные штрихи.
  function warp() {
    const N = 48, rnd = rng(0x51A7);
    const stars = Array.from({ length: 70 }, () => ({
      a: rnd() * TAU, r: 0.4 + rnd() * 20,
    }));
    const frames = [];
    for (let i = 0; i < N; i++) {
      const v = blank();
      const glowR = 110 + 60 * Math.sin(TAU * i / N * 4);
      put(v, 12, 7, glowR);
      put(v, 12, 8, glowR * 0.8);
      put(v, 13, 7, glowR * 0.5);
      put(v, 11, 8, glowR * 0.5);
      for (const s of stars) {
        const prev = s.r;
        s.r *= 1.155;
        if (s.r > 26) {
          s.a = rnd() * TAU;
          s.r = 0.4 + rnd() * 1.4;
        }
        const cos = Math.cos(s.a), sin = Math.sin(s.a);
        const px = 12.5 + cos * s.r, py = 7.5 + sin * s.r * 0.62;
        const qx = 12.5 + cos * prev, qy = 7.5 + sin * prev * 0.62;
        const bright = 90 + 165 * Math.min(1, s.r / 15);
        line(v, qx, qy, px, py, bright * 0.5);
        put(v, px, py, bright);
      }
      frames.push({ flag: i === 0 ? 1 : 0, v, ms: 62 });
    }
    return { frames };
  }

  // Огонь: шесть отдельных языков с качанием и мерцанием.
  function fire() {
    const rnd = rng(0xF17E);
    const flames = Array.from({ length: 6 }, (_, k) => ({
      x: 3.2 + k * 3.8,
      phase: rnd() * TAU,
      speed: 0.7 + rnd() * 0.6,
      height: 6.5 + rnd() * 5.5,
      width: 1.5 + rnd() * 1.0,
    }));
    return frameSet(40, 78, (i, t) => {
      const v = blank();
      for (const f of flames) {
        const h = f.height * (0.78 + 0.22 * Math.sin(t * 2.4 * f.speed + f.phase));
        const sway = 1.6 * Math.sin(t * 1.3 + f.phase);
        const flicker = 0.72 + 0.28 * Math.sin(t * 3 + f.phase * 2);
        for (let k = 0; k < h; k++) {
          const y = ROWS - 1 - k;
          const p = k / h;
          const w = Math.max(0.55, f.width * (1 - p * 0.75));
          const cx = f.x + sway * p;
          const bright = 255 * flicker * Math.pow(1 - p, 0.9);
          for (let dx = -w; dx <= w; dx += 0.55) put(v, cx + dx, y, bright);
        }
      }
      for (let x = 0; x < COLS; x++) {
        put(v, x, ROWS - 1, 70 + 65 * (0.5 + 0.5 * Math.sin(t * 3 + x * 1.7)));
      }
      return v;
    });
  }

  // Цифровой дождь: 6 колонок символов с хвостами.
  function rain() {
    const N = 40, rnd = rng(0xDA7A);
    const glyphs = [
      [7, 5, 5, 5, 7], [2, 6, 2, 2, 7], [7, 1, 7, 4, 7], [7, 1, 7, 1, 7],
      [5, 5, 7, 1, 1], [7, 4, 7, 1, 7], [7, 4, 7, 5, 7], [7, 1, 1, 2, 2],
      [7, 5, 7, 5, 7], [7, 5, 7, 1, 7],
    ];
    const drops = Array.from({ length: 6 }, (_, k) => ({
      x: 1 + k * 4,
      y: rnd() * 22 - 7,
      speed: 0.5 + rnd() * 0.45,
      glyph: glyphs[(rnd() * glyphs.length) | 0],
    }));
    const frames = [];
    for (let i = 0; i < N; i++) {
      const v = blank();
      for (const d of drops) {
        d.y += d.speed;
        if (d.y > 21) {
          d.y = -7 - rnd() * 6;
          d.speed = 0.5 + rnd() * 0.45;
          d.glyph = glyphs[(rnd() * glyphs.length) | 0];
        }
        const gy = Math.round(d.y);
        // призрачный предыдущий символ
        for (let r = 0; r < 5; r++) {
          for (let c = 0; c < 3; c++) {
            if (d.glyph[r] & (1 << (2 - c))) put(v, d.x + c, gy - 6 + r, 34);
          }
        }
        // сам символ
        for (let r = 0; r < 5; r++) {
          for (let c = 0; c < 3; c++) {
            if (d.glyph[r] & (1 << (2 - c))) put(v, d.x + c, gy + r, 255);
          }
        }
        // хвост над головой
        for (let k = 1; k <= 4; k++) put(v, d.x + 1, gy - k, 130 - k * 26);
      }
      frames.push({ flag: i === 0 ? 1 : 0, v, ms: 76 });
    }
    return { frames };
  }

  // Вращающийся каркасный куб с перспективой.
  function cube() {
    const N = 60, ms = 58;
    const V = [
      [-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
      [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1],
    ];
    const E = [
      [0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4],
      [0, 4], [1, 5], [2, 6], [3, 7],
    ];
    const rot = (p, ax, ay, az) => {
      let [x, y, z] = p;
      let c = Math.cos(ax), s = Math.sin(ax);
      [y, z] = [y * c - z * s, y * s + z * c];
      c = Math.cos(ay); s = Math.sin(ay);
      [x, z] = [x * c + z * s, -x * s + z * c];
      c = Math.cos(az); s = Math.sin(az);
      [x, y] = [x * c - y * s, x * s + y * c];
      return [x, y, z];
    };
    const frames = [];
    for (let i = 0; i < N; i++) {
      const t = TAU * i / N;
      const v = blank();
      const pts = V.map(p => rot(p, t, t * 2, t * 0.5));
      for (const [a, b] of E) {
        const p = pts[a], q = pts[b];
        const depth = (p[2] + q[2]) / 2;
        const bright = 120 + 135 * (depth + 1.7) / 3.4;
        const project = ([x, y, z]) => {
          const f = 2.8 / (z + 3.6);
          return [12.5 + x * f * 7.2, 7.5 + y * f * 7.2];
        };
        const [x0, y0] = project(p), [x1, y1] = project(q);
        line(v, x0, y0, x1, y1, bright * 0.35);
        line(v, x0, y0, x1, y1, bright);
      }
      frames.push({ flag: i === 0 ? 1 : 0, v, ms });
    }
    return { frames };
  }

  // Северное сияние: две мягкие волнистые завесы, медленно плывущие по экрану.
  function aurora() {
    return frameSet(40, 80, (i, t) => {
      const v = blank();
      for (let x = 0; x < COLS; x++) {
        const y1 = 4.6 + 2.4 * Math.sin(x * 0.3 + t) + 0.9 * Math.sin(x * 0.63 - t * 2);
        const y2 = 8.0 + 1.8 * Math.sin(x * 0.42 - t * 1.6 + 1.3);
        const shimmer = 0.62 + 0.38 * Math.sin(x * 0.55 + t * 3);
        for (let y = 0; y < ROWS; y++) {
          const d1 = (y - y1) / 2.1, d2 = (y - y2) / 2.8;
          let val = Math.exp(-d1 * d1) * shimmer + Math.exp(-d2 * d2) * 0.52;
          val *= 0.8 + 0.2 * Math.sin(x * 0.21 - t * 2 + y * 0.2);
          val *= 0.55 + 0.45 * Math.pow(0.5 + 0.5 * Math.sin(x * 1.9 + t * 4 + y * 0.15), 2);
          val *= Math.max(0.12, 1 - 0.75 * Math.max(0, y - 10) / 5);
          v[y * COLS + x] = byte(val * 235);
        }
      }
      return v;
    });
  }

  // Комета на эллиптической орбите, длинный хвост и мерцающие звёзды.
  function comet() {
    const N = 48, rnd = rng(0xC0E7);
    const stars = Array.from({ length: 32 }, () => ({
      x: (rnd() * COLS) | 0, y: (rnd() * ROWS) | 0, phase: rnd() * TAU,
    }));
    const frames = [];
    for (let i = 0; i < N; i++) {
      const t = TAU * i / N;
      const v = blank();
      for (const s of stars) {
        const tw = 28 + 80 * (0.5 + 0.5 * Math.sin(t * 2 + s.phase));
        put(v, s.x, s.y, tw);
      }
      for (let k = 16; k >= 1; k--) {
        const a = t - k * 0.1;
        const x = 12.5 + 9.6 * Math.cos(a), y = 7.5 + 5.6 * Math.sin(a);
        const bright = 250 * Math.pow(1 - k / 18, 2.4);
        if (k < 6) glow(v, x, y, bright);
        else put(v, x, y, bright);
      }
      const hx = 12.5 + 9.6 * Math.cos(t), hy = 7.5 + 5.6 * Math.sin(t);
      glow(v, hx, hy, 255);
      glow(v, hx, hy, 255);
      frames.push({ flag: i === 0 ? 1 : 0, v, ms: 62 });
    }
    return { frames };
  }

  // Пульс: расходящиеся кольца и мягкое свечение в центре.
  function pulse() {
    return frameSet(32, 78, (i, t) => {
      const v = blank();
      for (let y = 0; y < ROWS; y++) {
        for (let x = 0; x < COLS; x++) {
          const r = Math.hypot((x - 12) * 0.95, (y - 7.5) * 1.05);
          let val = Math.exp(-r * r * 5) * 170;
          for (let k = 0; k < 3; k++) {
            const phase = (t / TAU + k / 3) % 1;
            const ring = phase * 14.5;
            const d = r - ring;
            val += Math.exp(-(d * d) / 1.3) * 255 * (1 - phase * 0.72);
          }
          v[y * COLS + x] = byte(val);
        }
      }
      return v;
    });
  }

  // Песочные часы: верх пустеет, низ наполняется, в центре падают крупинки.
  function hourglass() {
    const N = 60, ms = 65;
    const frames = [];
    for (let i = 0; i < N; i++) {
      const phase = i / N;
      const v = blank();
      line(v, 8, 1, 16, 1, 70);
      line(v, 8, 14, 16, 14, 70);
      line(v, 8, 1, 12, 7, 70);
      line(v, 16, 1, 12, 7, 70);
      line(v, 12, 8, 8, 14, 70);
      line(v, 12, 8, 16, 14, 70);

      const topRows = 6 - Math.floor(phase * 6);
      for (let y = 2; y < 2 + topRows; y++) {
        const w = Math.round(4 * (7 - y) / 5);
        for (let x = 12 - w; x <= 12 + w; x++) put(v, x, y, 215);
      }
      const botRows = Math.floor(phase * 6);
      for (let k = 0; k < botRows; k++) {
        const y = 13 - k;
        const w = Math.round(4 * (y - 8) / 5);
        for (let x = 12 - w; x <= 12 + w; x++) put(v, x, y, 215);
      }
      const gy = 8 + ((i * 2) % 5);
      put(v, 12, gy, 255);
      put(v, 11 + (i % 2) * 2, 10 + ((i * 3) % 4), 180);
      frames.push({ flag: i === 0 ? 1 : 0, v, ms });
    }
    return { frames };
  }

  // ---------- кольцо 24 LED ----------

  function hsv(h, s, val) {
    h = ((h % 1) + 1) % 1;
    const i = Math.floor(h * 6), f = h * 6 - i;
    const p = val * (1 - s), q = val * (1 - f * s), t = val * (1 - (1 - f) * s);
    const rgb = [[val, t, p], [q, val, p], [p, val, t], [p, q, val], [t, p, val], [val, p, q]][i % 6];
    return rgb.map(x => byte(x * 255));
  }

  const ringFrame = (colors, ms) => ({ c: colors.map(c => c.map(x => byte(x))), ms });
  const ringText = (frames, loop) => (loop ? 'loop\n' : '')
    + frames.map(f => f.c.map(c => c.map(x => byte(x).toString(16).padStart(2, '0')).join('')).join(' ') + ' ' + f.ms).join('\n') + '\n';

  function ringSet(count, ms, fn) {
    const frames = [];
    for (let i = 0; i < count; i++) frames.push(ringFrame(fn(i, TAU * i / count), ms));
    return { frames, text: ringText(frames, true), loop: true };
  }

  function ringComet() {
    return ringSet(40, 60, (i, t) => {
      const leds = [];
      const head = (i / 40) * 24;
      for (let k = 0; k < 24; k++) {
        let d = (head - k + 24) % 24;
        if (d > 12) d = 24 - d;
        const f = Math.exp(-d / 2.4);
        leds.push([30 + 225 * f, 60 + 190 * f, 90 + 160 * f]);
      }
      return leds;
    });
  }

  function ringBreath() {
    return ringSet(40, 80, (i, t) => {
      const k = Math.pow(0.5 - 0.5 * Math.cos(t), 1.4);
      const leds = [];
      for (let j = 0; j < 24; j++) {
        const depth = 0.82 + 0.18 * Math.sin(j / 24 * TAU + t);
        leds.push([90 * k * depth + 12, 170 * k * depth + 8, 255 * k * depth]);
      }
      return leds;
    });
  }

  function ringRainbow() {
    return ringSet(48, 50, (i, t) => {
      const leds = [];
      for (let j = 0; j < 24; j++) leds.push(hsv(j / 24 + i / 48, 1, 1));
      return leds;
    });
  }

  function ringSparks() {
    const N = 48, rnd = rng(0x5A47);
    const sparks = Array.from({ length: 7 }, () => ({ pos: (rnd() * 24) | 0, life: rnd(), color: rnd() < 0.5 ? 0 : 1 }));
    const frames = [];
    for (let i = 0; i < N; i++) {
      const leds = Array.from({ length: 24 }, () => [6, 10, 20]);
      for (const s of sparks) {
        s.life -= 0.045;
        if (s.life <= 0) {
          s.pos = (rnd() * 24) | 0;
          s.life = 0.6 + rnd() * 0.4;
          s.color = rnd() < 0.5 ? 0 : 1;
        }
        const f = Math.min(1, s.life * 1.6);
        const base = s.color ? [255, 210, 120] : [140, 220, 255];
        const c = leds[s.pos];
        leds[s.pos] = [c[0] + base[0] * f, c[1] + base[1] * f, c[2] + base[2] * f];
      }
      frames.push(ringFrame(leds, 60));
    }
    return { frames, text: ringText(frames, true), loop: true };
  }

  function ringWave() {
    return ringSet(36, 60, (i, t) => {
      const leds = [];
      const head = (i / 36) * 24;
      for (let k = 0; k < 24; k++) {
        const d = (head - k + 24) % 24;
        if (d > 14) leds.push([0, 0, 0]);
        else {
          const f = Math.pow(1 - d / 15, 2);
          leds.push([255 * f, (70 + 150 * f) * f, (20 + 60 * f) * f]);
        }
      }
      return leds;
    });
  }

  function ringChase() {
    return ringSet(36, 55, (i, t) => {
      const leds = Array.from({ length: 24 }, () => [0, 0, 0]);
      for (let s = 0; s < 2; s++) {
        const head = (i / 36) * 24 + s * 12;
        const base = s ? [255, 60, 190] : [0, 210, 255];
        for (let k = 0; k < 24; k++) {
          let d = (head - k + 24) % 24;
          if (d > 8) continue;
          const f = Math.exp(-d / 1.7);
          leds[k] = [
            Math.max(leds[k][0], base[0] * f),
            Math.max(leds[k][1], base[1] * f),
            Math.max(leds[k][2], base[2] * f),
          ];
        }
      }
      return leds;
    });
  }

  const duration = frames => frames.reduce((sum, f) => sum + f.ms, 0);

  global.AnimPresets = {
    version: 1,
    matrix: [
      { id: 'portal', title: 'Портал', desc: 'Вращающийся туннель', build: portal, duration: 2880 },
      { id: 'warp', title: 'Гиперпрыжок', desc: 'Полёт сквозь звёзды', build: warp, duration: 48 * 62 },
      { id: 'plasma', title: 'Плазма', desc: 'Живые волны яркости', build: plasma, duration: 36 * 90 },
      { id: 'cube', title: 'Куб', desc: 'Каркасный 3D-куб', build: cube, duration: 60 * 58 },
      { id: 'comet', title: 'Комета', desc: 'Орбита с хвостом', build: comet, duration: 48 * 62 },
      { id: 'aurora', title: 'Сияние', desc: 'Северное сияние', build: aurora, duration: 40 * 80 },
      { id: 'fire', title: 'Огонь', desc: 'Языки пламени', build: fire, duration: 40 * 78 },
      { id: 'rain', title: 'Цифровой дождь', desc: 'Падающие символы', build: rain, duration: 40 * 76 },
      { id: 'pulse', title: 'Пульс', desc: 'Кольца от центра', build: pulse, duration: 32 * 78 },
      { id: 'hourglass', title: 'Песочные часы', desc: 'Песок и крупинки', build: hourglass, duration: 60 * 65 },
    ],
    ring: [
      { id: 'ring-comet', title: 'Комета', desc: 'Яркая точка с хвостом', build: ringComet, duration: 40 * 60 },
      { id: 'ring-breath', title: 'Дыхание', desc: 'Плавное свечение', build: ringBreath, duration: 40 * 80 },
      { id: 'ring-rainbow', title: 'Радуга', desc: 'Вращение спектра', build: ringRainbow, duration: 48 * 50 },
      { id: 'ring-sparks', title: 'Искры', desc: 'Мерцающие вспышки', build: ringSparks, duration: 48 * 60 },
      { id: 'ring-wave', title: 'Волна', desc: 'Бегущая дуга', build: ringWave, duration: 36 * 60 },
      { id: 'ring-chase', title: 'Погоня', desc: 'Две кометы навстречу', build: ringChase, duration: 36 * 55 },
    ],
    duration,
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = global.AnimPresets;
})(typeof window !== 'undefined' ? window : globalThis);
