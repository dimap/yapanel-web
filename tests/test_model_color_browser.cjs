// Optional real-browser regression suite: npm install --no-save playwright; npx playwright install chromium
// node tests/test_model_color_browser.cjs (uses a loopback fixture with no real speaker access).
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const child = spawn(process.env.PYTHON || 'python3', [path.join(__dirname, 'model_color_fixture.py')], {stdio:['ignore','pipe','pipe']});
let serverError='';child.stderr.on('data', b=>serverError+=b);
const urlReady = new Promise((resolve,reject)=>{
 let text='';child.stdout.on('data',b=>{text+=b;try{resolve(JSON.parse(text.trim()).url);}catch{}});
 child.on('exit',code=>reject(new Error('fixture exited '+code+': '+serverError)));
});
(async()=>{
 const url = await urlReady;
 const browser = await chromium.launch({headless:true,args:['--use-angle=swiftshader','--enable-unsafe-swiftshader']});
 const contexts=[];const cases=[];
 const configure = async(data={})=>{const r=await fetch(url+'/__test__/configure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});assert(r.ok);};
 const newPage = async(options={})=>{const c=await browser.newContext(options);contexts.push(c);return c.newPage();};
 const load = async(page)=>{await page.goto(url);await page.waitForFunction(()=>window.__fixtureReady);};
 const prompt = p=>p.locator('[data-color-form]');
 const caption = p=>p.locator('[data-color-caption]');
 const waitColor = async(p,color)=>p.waitForFunction(color=>window.__deviceColor===color,color);
 const saveAnswer = async(p,color)=>{await p.locator('#model-color-answer').selectOption(color);await p.locator('[data-color-form] button').click();await waitColor(p,color);};
 const snapshot = async(p,name)=>{if(process.env.CHECK_ARTIFACT_DIR){fs.mkdirSync(process.env.CHECK_ARTIFACT_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.CHECK_ARTIFACT_DIR,name+'.png'),fullPage:true});}};
 try {
  const page=await newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await load(page);
  assert(await prompt(page).isVisible());assert.equal(await page.locator('#model-color-answer').inputValue(),'');
  assert(await page.locator('[data-color-form] button').isDisabled());assert.match(await caption(page).textContent(),/условный/);
  // Keep an unfinished choice across ordinary status refreshes; no implicit POST on mount.
  await page.locator('#model-color-answer').selectOption('green');await page.evaluate(()=>refreshFixture());
  assert.equal(await page.locator('#model-color-answer').inputValue(),'green');
  await page.locator('[data-color-form] button').focus();await page.keyboard.press('Enter');await waitColor(page,'green');
  assert(!await prompt(page).isVisible());assert.match(await caption(page).textContent(),/пользователем/);
  cases.push('unknown: explicit choice, keyboard save, no poll reset');
  const second=await newPage();await load(second);await waitColor(second,'green');
  await configure({color:'blue',read:'available'});await second.evaluate(()=>refreshFixture());await waitColor(second,'green');
  await second.locator('#model-color').selectOption('auto');await waitColor(second,'blue');
  assert.match(await caption(second).textContent(),/автоматически/);cases.push('second browser, manual priority and reset to auto');
  await configure({device:'TEST0000000002',color:'',read:'unavailable'});await second.evaluate(()=>refreshFixture());
  assert(await prompt(second).isVisible());assert.equal(await second.evaluate(()=>window.__deviceColor),null);
  cases.push('replacement at same address receives no previous colour');
  await configure({fail_save:true});await second.locator('#model-color-answer').selectOption('green');await second.locator('[data-color-form] button').click();
  await second.locator('[data-color-error]:not([hidden])').waitFor();assert.equal(await second.evaluate(()=>window.__deviceColor),null);
  assert(await prompt(second).isVisible());cases.push('write failure is visible and does not claim success');
  await configure({fail_save:false,online:false});await second.evaluate(()=>refreshFixture());assert(!await prompt(second).isVisible());
  await configure({online:true});await second.evaluate(()=>refreshFixture());assert(await prompt(second).isVisible());
  cases.push('offline hides question; reconnect restores it');
  for(const [read,color,ask] of [['available','unlisted',true],['empty','',true],['error','',false],['available','gray',false]]){
   await configure({read,color,clear:true});await load(second);assert.equal(await prompt(second).isVisible(),ask,read+':'+color);
  }
  cases.push('unsupported, empty, failed and normalized native values');
  await configure({color:'',read:'unavailable',clear:true});
  await page.evaluate(()=>localStorage.setItem('model.preferences.single.mini3pro',JSON.stringify({color:'blue',spin:true})));
  await load(page);assert.match(await page.locator('[data-color-question]').textContent(),/этом браузере/);await waitColor(page,'blue');
  assert.equal(await page.locator('#model-color-answer').inputValue(),'blue');
  // Local appearance must not silently propagate to another browser.
  await load(second);assert(await prompt(second).isVisible());assert.equal(await second.evaluate(()=>window.__deviceColor),null);
  await saveAnswer(page,'blue');await load(second);await waitColor(second,'blue');
  assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('model.preferences.single.mini3pro')).spin),true);
  await second.locator('#model-color').selectOption('auto');await waitColor(second,null);
  await page.evaluate(()=>localStorage.setItem('model.preferences.single.mini3pro',JSON.stringify({color:'green'})));
  await load(page);assert.equal(await page.evaluate(()=>window.__deviceColor),null);assert.match(await page.locator('[data-color-question]').textContent(),/Не удалось/);
  cases.push('confirmed legacy migration; server auto suppresses stale local preferences');
  // Every supported visual model, including the Lite 2 STL renderer and the Midi concept builder.
  for(const model of ['lite','lite2','lite2nc','mini2','mini2nc','mini3','mini3pro','midi','station2']){
   await configure({model,clear:true});await load(second);assert(await prompt(second).isVisible(),model);
   const canvas=second.locator('#model-view .studio-canvas, #model-view canvas:not(#model-viz)');
   assert(await canvas.count(),model+' renderer created');
   assert(!await second.locator('.studio-error').count(),model+' no renderer error');
   if(model==='lite2')await second.waitForFunction(()=>document.querySelector('#model-view canvas:not(#model-viz)')?.style.opacity==='1');
  }
  cases.push('all nine supported models; both rendering paths');
  await configure({model:'mini3pro',clear:true});await second.setViewportSize({width:360,height:800});await load(second);
  const overflow=await second.evaluate(()=>document.documentElement.scrollWidth>innerWidth);assert(!overflow,'mobile viewport');
  await snapshot(second,'unknown-mobile');await saveAnswer(second,'green');await snapshot(second,'green-mobile');
  cases.push('mobile picker and rendered green body');
  const noGL=await newPage({viewport:{width:393,height:852}});
  await noGL.addInitScript(()=>{const original=HTMLCanvasElement.prototype.getContext;HTMLCanvasElement.prototype.getContext=function(kind,...args){return /^webgl|experimental-webgl/.test(kind)?null:original.call(this,kind,...args);};});
  await configure({clear:true});await load(noGL);assert(await prompt(noGL).isVisible());await saveAnswer(noGL,'green');
  await snapshot(noGL,'no-webgl');cases.push('choice and settings work without WebGL');
  let liveRequests=0;const beforeGallery=await page.evaluate(()=>localStorage.getItem('model.preferences.single.mini3pro'));const gallery=await contexts[0].newPage();gallery.on('request',r=>{if(r.url().includes('/api/model-color'))liveRequests++;});
  await gallery.goto(url+'/static/model-preview.html?model=mini3pro');await gallery.locator('#model-color').waitFor();
  await gallery.locator('#model-color').selectOption('green');assert.equal(liveRequests,0);assert.equal(await page.evaluate(()=>localStorage.getItem('model.preferences.single.mini3pro')),beforeGallery);cases.push('gallery stays local');
  assert.deepEqual(errors,[],'live model bootstrap exceptions');
  console.log(JSON.stringify({cases,passed:cases.length},null,2));
 } finally {await browser.close();child.kill('SIGTERM');}
})().catch(e=>{console.error(e);child.kill('SIGTERM');process.exitCode=1;});
