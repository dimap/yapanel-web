// Static guards for the Mini 2 visual model: the owner's detailed body must stay merged with the gallery
// concepts, and the browser must explain a failed or lost WebGL context instead of leaving a blank widget.
const fs = require('fs'), path = require('path'), assert = require('assert');
const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const studio = read('static/model-studio.js');
const preview = read('static/model-preview.js');
const model = read('static/model.js');
const sources = read('static/models/SOURCES.md');
const palettes = JSON.parse(read('static/model-colors.json')).palettes;

// PR #10 gallery concepts must survive the Mini 2 merge.
assert(studio.includes("import {CONCEPTS} from './model-concepts.js'"), 'concepts import');
assert(studio.includes('concept.build({'), 'concept builder call');
assert(studio.includes('if(concept?.lying)'), 'lying concept rotation');
assert(studio.includes('bounds.radius'), 'concept camera framing');
assert(studio.includes('if(!mini2&&!concept)'), 'top marks skipped for Mini 2 and concepts');

// Owner's Mini 2 reconstruction, four official colours, shared clock geometry.
assert(studio.includes('else if(mini2)'), 'Mini 2 body branch');
assert(studio.includes("import * as G from './clock-geometry.js'"), 'shared clock geometry import');
assert(studio.includes('97 × 54'), 'Mini 2 dimensions');
assert.deepStrictEqual(palettes.mini2.white, ["Белый", "#dcd8d1", "#e8e5df"]);
assert.deepStrictEqual(palettes.mini2.black, ["Чёрный", "#2b2b2f", "#232326"]);
assert.deepStrictEqual(palettes.mini2.red, ["Красный", "#6e2634", "#601f2d"]);
assert.deepStrictEqual(palettes.mini2.blue, ["Синий", "#2d4070", "#273863"]);
assert(studio.includes('liteSlots(c,R,w=22,h=56,ring=.905)'), 'default Lite 1 slot geometry');
assert(studio.includes('display(values)'), 'Mini 2 canvas display is used by modelShow');
assert(studio.includes('G.DIGIT_X') && studio.includes('G.segShapes'), 'clock segments come from the panel geometry');
assert(studio.includes('RingGeometry(4.3,6.9'), 'visible top Alice ring');
assert(studio.includes('shadowBlur=28') && studio.includes('rgba(30,24,20,.72)'), 'bright clock contrast');
assert(studio.includes('Math.max(.8,a/255)'), 'clock stays visible at low device brightness');
assert(!studio.includes('cc.roundRect(6,4,W-12,H-8,22)'), 'no grey display window');
assert(studio.includes('all six native zones contribute'), 'six native RGB zones are mixed');

// A lost WebGL context, a hidden tab or a render exception must recover or explain itself.
assert(studio.includes("addEventListener('webglcontextlost'"), 'context lost handler');
assert(studio.includes("addEventListener('webglcontextrestored'"), 'context restored handler');
assert(studio.includes("addEventListener('visibilitychange'"), 're-render on tab return');
assert(studio.includes('3D-модель перезапускается'), 'context lost note');
assert(studio.includes('3D-модель не отрисовывается'), 'render error note');
assert(model.includes('Не удалось показать 3D-модель'), 'import failure note');
assert(model.includes("import('./model-studio.js')"), 'dynamic import still used');

// The gallery defaults Mini 2 to white and six ring zones.
assert(preview.includes("id==='mini2'?'white'"), 'preview white default');
assert(preview.includes("id==='mini2'?6"), 'preview six ring zones');
assert(preview.includes('97 × 54'), 'preview description');

// Public files must not leak local absolute paths.
for (const text of [studio, preview, model])
  assert(!/\/home\/|\/Users\/|file:\/\//.test(text), 'no local absolute paths in public model files');

// Sources document the owner's measurements and the mixed diffuser.
assert(sources.includes('Station Mini 2 with clock (YNDX-00020)'), 'Mini 2 sources entry');
assert(sources.includes('Four official colours'), 'Mini 2 colour list');
assert(sources.includes('mean colour is an approximation'), 'diffuser approximation documented');

console.log('Mini 2 model, gallery merge, fallback UI and sources cases passed');
