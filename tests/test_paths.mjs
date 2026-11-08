import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../static/panel-context.js',import.meta.url),'utf8');
for(const path of ['/', '/index.html','/speaker/room-a/','/speaker/room-b/index.html']){
  const calls=[],scope={window:{},document:{baseURI:'https://panel.test'+path},URL,fetch:(...args)=>calls.push(args)};
  vm.runInNewContext(source,scope);
  const context=scope.window.PanelContext,base=path.replace(/index.html$/,'');
  assert.equal(context.url('/api/status'),'https://panel.test'+base+'api/status');
  assert.equal(context.url('/static/model.js'),'https://panel.test'+base+'static/model.js');
  assert.equal(context.url('/model.stl'),'https://panel.test'+base+'model.stl');
  assert.equal(context.websocket('/api/term?cols=80'),'wss://panel.test'+base+'api/term?cols=80');
  context.fetch('/api/clock',{method:'POST'});assert.equal(calls[0][0],'https://panel.test'+base+'api/clock');
  assert.equal(context.storageKey('theme'),'theme');
  if(context.deviceId)assert.equal(context.storageKey('fs.path'),'speaker.'+context.deviceId+'.fs.path');
  else assert.equal(context.storageKey('fs.path'),'fs.path');
}
const html=await readFile(new URL('../static/index.html',import.meta.url),'utf8');
const app=await readFile(new URL('../static/app.js',import.meta.url),'utf8');
assert(!/\bfetch\(/.test((html+app).replaceAll('PanelContext.fetch(','')));
assert(!/href="\/(static|ca\.crt|manifest)/.test(html));
assert(app.includes('new WebSocket(PanelContext.websocket('));
// Firefox ignores an import map that appears after the first module script; keep it in the head.
const firstModule=html.search(/<script[^>]*\btype=["']module["']/);
const importMap=html.indexOf('type="importmap"');
assert(importMap>0 && importMap<firstModule,'import map must precede every module script');
const manifest=JSON.parse(await readFile(new URL('../static/manifest.webmanifest',import.meta.url),'utf8'));
assert.equal(manifest.scope,'./');assert.equal(manifest.start_url,'./#overview');
// Exercise real service worker handlers without a browser or any network.
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
for(const path of ['/','/speaker/room-a/','/speaker/room-b/']){
  const handlers={},puts=[],deleted=[];
  const base='https://panel.test'+path;
  const sw={
    URL,
    self:{location:{href:base+'sw.js'},addEventListener:(name,fn)=>handlers[name]=fn,clients:{claim:async()=>{}}},
    fetch:async request=>({ok:true,clone:()=>({url:request.url})}),
    caches:{
      open:async()=>({put:async(key,response)=>puts.push({key,response})}),
      match:async()=>null,
      keys:async()=>['panel-v2:'+new URL(path,base).pathname+':shell','panel-v7:'+new URL(path,base).pathname+':shell'],
      delete:async key=>deleted.push(key),
    },
    Response:{error:()=>({error:true})},
  };
  vm.runInNewContext(await readFile(new URL('../static/sw.js',import.meta.url),'utf8'),sw);
  for(const leaf of ['api/status','api/clock','static/models/lite2.stl','ca.crt'])handlers.fetch({request:{method:'GET',url:base+leaf},respondWith:()=>assert.fail('must not cache '+leaf)});
  // Навигация на саму панель кладётся под ключ оболочки; навигация в галерею — нет.
  const panel={method:'GET',url:base,mode:'navigate'};let response;
  handlers.fetch({request:panel,respondWith:p=>response=p});await response;await tick();
  assert.equal(puts.at(-1).key,base,'panel navigation caches the shell');
  const preview={method:'GET',url:base+'static/model-preview.html',mode:'navigate'};
  handlers.fetch({request:preview,respondWith:p=>response=p});await response;await tick();
  assert.equal(puts.at(-1).key,preview,'gallery navigation must not overwrite the shell');
  assert.notEqual(puts.at(-1).key,base);
  // Обновление namespace удаляет старый кэш.
  let activation;handlers.activate({waitUntil:p=>activation=p});await activation;
  assert(deleted.some(key=>key.startsWith('panel-v2:')),'old panel cache is deleted');
  assert(!deleted.some(key=>key.startsWith('panel-v7:')),'current cache is kept');
}
console.log('Page context, scoped storage, WebSocket, manifest, shell-only navigation cache and cache version cases passed');
