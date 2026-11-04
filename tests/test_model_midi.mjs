// Exercise the actual Midi builder with bundled Three.js. Canvas drawing is a
// recorder, not a rasterizer: appearance and GPU/browser performance need QA.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
// Like test_renderer.mjs, load browser .js files explicitly as ESM without
// depending on newer Node's implicit module-syntax detection or a package.json.
const moduleURL=source=>'data:text/javascript;base64,'+Buffer.from(source).toString('base64');
const read=file=>readFile(new URL(file,import.meta.url),'utf8');
const [threeSource,clockSource,midiSource,conceptsSource]=await Promise.all([
  read('../static/vendor/three/three.module.js'),read('../static/clock-geometry.js'),
  read('../static/model-midi.js'),read('../static/model-concepts.js'),
]);
const clockURL=moduleURL(clockSource);
const midiURL=moduleURL(midiSource.replace("'./clock-geometry.js'",JSON.stringify(clockURL)));
const T=await import(moduleURL(threeSource));
const {textToFrame}=await import(clockURL);
const {buildMidi}=await import(midiURL);
const catalog=JSON.parse(await read('../static/model-colors.json'));
const {CONCEPTS}=await import(moduleURL(conceptsSource.replace("'./model-midi.js'",JSON.stringify(midiURL)).replace("import {modelPalette} from './model-colors.js';", 'const catalog='+JSON.stringify(catalog)+'; const modelPalette=id=>catalog.palettes[catalog.aliases[id]||id];')));

class Context {
  constructor() { this.draws=[]; this.globalAlpha=1; this.stack=[]; }
  clearRect() { this.draws=[]; }
  save() { this.stack.push(this.globalAlpha); }
  restore() { this.globalAlpha=this.stack.pop() ?? 1; }
  fill(path) { this.draws.push({path,alpha:this.globalAlpha}); }
  fillRect() { this.draws.push({alpha:this.globalAlpha}); }
  createImageData(w,h) { return {data:new Uint8ClampedArray(w*h*4)}; }
  getImageData(x,y,w,h) { return this.createImageData(w,h); }
  putImageData() {}
}
for(const method of ['translate','scale','rotate','beginPath','moveTo','lineTo','absarc',
  'arc','bezierCurveTo','quadraticCurveTo','roundRect','closePath','stroke']) Context.prototype[method]=function(){};
globalThis.document={createElement(tag){
  assert.equal(tag,'canvas'); const context=new Context();
  return {width:0,height:0,getContext(type){assert.equal(type,'2d');return context;}};
}};
globalThis.Path2D=class {constructor(path){this.path=path;}};
assert.equal(CONCEPTS.midi.build,buildMidi,'gallery and studio select the actual dedicated builder');

const group=new T.Group();
const fabric=new T.MeshStandardMaterial(),rubber=new T.MeshStandardMaterial();
const details=buildMidi({
  T,fabric,rubber,renderer:{capabilities:{getMaxAnisotropy:()=>4}},
  mesh(geometry,material,x=0,y=0,z=0){const m=new T.Mesh(geometry,material);m.position.set(x,y,z);group.add(m);return m;},
  // The rounded-box helper is the studio's responsibility; use real box
  // geometry here to test part placement without copying its implementation.
  roundedBox:(w,h,d)=>new T.BoxGeometry(w,h,d),
  canvasTexture(size,draw){const canvas=document.createElement('canvas');canvas.width=canvas.height=size;draw(canvas.getContext('2d'),size);return new T.CanvasTexture(canvas);},
});
const part=name=>{const m=group.getObjectByName(name);assert(m,name);return m;};
for(const name of ['midi-woven-shell','midi-matte-plastic-panel','midi-contour-light',
  'midi-microphone-switch','midi-usb-c-recess','midi-fabric-display']) part(name);
assert.equal(details.topY,55); assert.equal(details.baseY,-55);
const bounds=new T.Box3().setFromObject(group),size=bounds.getSize(new T.Vector3());
assert(Math.abs(size.x-96)<1 && Math.abs(size.y-110)<1 && Math.abs(size.z-96)<3,'96×96×110 mm envelope');
assert(part('midi-usb-c-recess').position.z<0 && part('midi-fabric-display').position.z>0,'front and rear');
for(const m of group.children){
  assert(m.material.isMeshStandardMaterial || m.material.isMeshBasicMaterial,'supported material family');
  for(const attr of Object.values(m.geometry.attributes))
    assert(Array.from(attr.array).every(Number.isFinite),`${m.name}: finite geometry`);
}
const sleeve=part('midi-woven-shell').geometry;
for(let row=0;row<6;row++)for(const name of ['position','normal']){
  const attr=sleeve.attributes[name];
  for(let axis=0;axis<3;axis++)assert(Math.abs(attr.array[(row*257)*3+axis]-attr.array[(row*257+256)*3+axis])<1e-4,'closed sleeve seam');
}
const screen=part('midi-fabric-display'),ctx=screen.material.map.image.getContext('2d');
assert.equal(screen.visible,false,'no fabricated clock on creation');
details.display(textToFrame('12:34',40));
assert(screen.visible && ctx.draws.length>10,'actual normalized display draws a frame');
const low=ctx.draws[0].alpha;
details.display(textToFrame('12:34',170));
assert(ctx.draws[0].alpha>low,'device brightness affects the visual display');
for(const empty of [null,undefined,[],Array(36).fill(0)]){
  details.display(empty); assert.equal(screen.visible,false); assert.equal(ctx.draws.length,0);
}
details.display(Array(36).fill(NaN));assert.equal(screen.visible,false);
details.ring([[255,0,0]]);
const color=part('midi-contour-light').geometry.attributes.color;
assert(color.getX(0)>.9 && color.getY(0)<.02 && color.getZ(0)<.02,'RGB order');
details.ring([[255,0,0],[0,255,0],[0,0,255]]);
assert(color.getY(86)>.9 && color.getZ(171)>.9,'native zones contribute around the perimeter');
details.ring([[999,-5,NaN]]);
assert(Array.from(color.array).every(n=>Number.isFinite(n)&&n>=0&&n<=1),'invalid RGB is bounded');
const meshes=group.children.length,geometry=group.children.map(m=>m.geometry),materials=group.children.map(m=>m.material);
for(let i=0;i<100;i++){details.display(textToFrame('12:34',i));details.ring(Array.from({length:24},()=>[i,0,255-i]));}
assert.equal(group.children.length,meshes);
assert.deepEqual(group.children.map(m=>m.geometry),geometry,'updates reuse geometry');
assert.deepEqual(group.children.map(m=>m.material),materials,'updates reuse materials');
details.ring([]);assert(color.getX(0)<.03 && color.getY(0)<.03,'empty RGB clears the light');
assert.equal(screen.visible,true,'ring changes do not change clock visibility');
const mapped=group.children.filter(m=>m.material.normalMap||m.material.bumpMap).map(m=>m.name);
assert.deepEqual(mapped,[],'CONTRIBUTION.md excludes normal/bump maps');
console.log('Midi builder: selection, geometry, seam, display, RGB and resource reuse passed (mock canvas; no hardware/GPU claim)');
