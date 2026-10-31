const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
// A syntax error anywhere in the main shell prevents connection handling and
// every widget from starting; extracting individual functions misses this.
for(const match of html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)){
  if(/src=|type=["'](?:module|importmap)/.test(match[1])||!match[2].trim())continue;
  new vm.Script(match[2],{filename:'static/index.html',lineOffset:html.slice(0,match.index).split('\n').length-1});
}
const source=html.slice(html.indexOf('async function loadPanelVersion'),html.indexOf('\nloadPanelVersion().catch'));
const elements={},storage={},requests=[];
const node=id=>elements[id]||=( {style:{},hidden:true,classList:{toggle(){}},setAttribute(){}} );
const c={$:node,store:{get:k=>storage[k],set:(k,v)=>storage[k]=v},confirmAnswer:false,confirm:()=>c.confirmAnswer,
  UPD_HOW:{},toast:t=>c.toastText=t,act:async(b,fn)=>fn(),setTimeout:fn=>fn(),location:{reload:()=>{c.reloaded=true;}},
  PanelContext:{fetch:async()=>({ok:true,status:200,json:async()=>({version:'0.1.4'})})},
  response:{version:'0.1.3',latest:'0.1.4',checked:1700000000,update:true,install:'zip',multi:true},
  api:async route=>{requests.push(route);return route.includes('/update')?{ok:true,version:'0.1.4'}:c.response;}};
vm.createContext(c);
vm.runInContext('let panelVer=null;'+source,c);
(async()=>{
  // Новая версия: плашка на «Обзоре» и строка в «Настройках», без всплывающего окна и без обновления
  await vm.runInContext('loadPanelVersion()',c);
  assert.equal(node('#ov-update').hidden,false,'overview banner shown');
  assert(node('#ov-update-text').textContent.includes('0.1.4'));
  assert.equal(node('#upd-row').hidden,false);
  assert(!requests.some(p=>p.includes('/update')),'must not update without confirmation');
  // Скрытая плашка не возвращается для той же версии
  node('#ov-update-hide').onclick();
  assert.equal(node('#ov-update').hidden,true,'dismissed banner stays hidden');
  // Отказ в подтверждении — ничего не делаем; согласие — обновляем и ждём именно новую версию
  await node('#upd-go').onclick({currentTarget:node('#upd-go')});
  assert(!requests.some(p=>p.includes('/update')),'declined confirmation');
  c.confirmAnswer=true;
  await node('#upd-go').onclick({currentTarget:node('#upd-go')});
  assert(requests.some(p=>p.includes('/api/panel/update')));assert(c.reloaded,'reloads once the new version answers');
  // Ошибка проверки видна, старый успех её не маскирует
  c.response={...c.response,error:'GitHub 503'};
  await vm.runInContext('loadPanelVersion(true)',c).catch(()=>{});
  assert(node('#upd-state').textContent.includes('GitHub 503'));
  // Кнопка «Проверить» показывает результат
  c.response={...c.response,latest:'0.1.3',update:false,error:null};
  await node('#upd-check').onclick({currentTarget:node('#upd-check')});
  assert.equal(node('#upd-check').textContent,'Проверить');assert(c.toastText.includes('последняя'));
  assert(node('#upd-state').textContent.includes('проверено'));
  console.log('Update banner, dismissal, explicit confirmation, version wait, error and refresh feedback cases passed');
})().catch(e=>{console.error(e);process.exitCode=1});

// An unavailable model volume must not be displayed as zero or enable an unknown scale.
const volumeNodes={};
const volumeContext={$:id=>volumeNodes[id]||=( {} )};
vm.createContext(volumeContext);
const volumeStart=html.indexOf('function showLevel(');
vm.runInContext(html.slice(volumeStart,html.indexOf('\n}',volumeStart)+2),volumeContext);
for(const value of [null,undefined,NaN]){
  volumeContext.value=value;vm.runInContext('showLevel(value)',volumeContext);
  assert.equal(volumeNodes['#vol-big'].textContent,'—');
  assert.equal(volumeNodes['#vol-master'].disabled,true);
}
for(const value of [0,40]){
  volumeContext.value=value;vm.runInContext('showLevel(value)',volumeContext);
  assert.equal(volumeNodes['#vol-big'].textContent,value);
  assert.equal(volumeNodes['#vol-master'].disabled,false);
}
console.log('Unknown volume, zero volume and reconnected scale cases passed');

(async()=>{
  const start=html.indexOf('function btOutUse(');
  const end=html.indexOf("$('#bt-out-toggle')",start);
  // Extract just the function; the following toggle handler is outside its contract.
  const btSource=html.slice(start,html.indexOf('\n}',start)+2);
  for(const fail of [false,true]){
    let finish;
    const pending=new Promise((resolve,reject)=>{finish=()=>fail?reject(new Error('connection failed')):resolve({restarted:false});});
    const ctx={store:{get:()=>true,set(){}},mu:{},toast(){},confirm:()=>true,
      api:()=>pending,renderBt(){},aliceRestarting(){},
      act:async(_b,fn)=>{try{return await fn();}catch{return null;}}};
    vm.createContext(ctx);vm.runInContext(btSource,ctx);
    const button={textContent:'Играть через неё'};
    const result=ctx.btOutUse(button,'00:11:22:33:44:55');
    assert.equal(button.textContent,'Подключение и настройка…');
    finish();await result;
    assert.equal(button.textContent,'Играть через неё');
  }
  const hookNodes={};
  const ctx={$:id=>hookNodes[id]||=( {hidden:false,textContent:'',append(){}} ),
    hookInfo:{broken:true,possible:false,hook:false,users:['ассистент'],supported:false,reason:'Модель не поддерживает хук'}};
  vm.createContext(ctx);
  vm.runInContext(html.slice(html.indexOf('function renderHook()'),html.indexOf("$('#hook-repair').onclick")),ctx);
  ctx.renderHook();
  assert.equal(hookNodes['#hook-repair'].hidden,true);
  assert.equal(hookNodes['#hook-warn-text'].textContent,ctx.hookInfo.reason);
  console.log('Bluetooth preparation feedback and unsupported hook controls passed');
})().catch(e=>{console.error(e);process.exitCode=1});
