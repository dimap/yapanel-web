// Choose the visual model from the device; hardware capabilities remain explicit.
import {rendererProfile} from './renderer-profile.js';
async function loadProfile(){
  for(;;){
    const auth=await PanelContext.fetch('/api/panel-auth').then(r=>r.json()).catch(()=>null);
    if(auth&&(!auth.enabled||auth.authed)){
      const response=await PanelContext.fetch('/api/profile');if(response.ok)return response.json();
    }
    await new Promise(resolve=>setTimeout(resolve,2000));
  }
}
const profile=await loadProfile();
if(profile.hook_supported===false){
  const reason='На этой модели автозапуск, будильники и сценарии пока не поддерживаются.';
  for(const id of ['al-install','al-save','sc-add','sc-save']){
    const button=document.getElementById(id);if(button){button.disabled=true;button.title=reason;}
  }
  for(const tab of ['alarm','scenes']){
    const head=document.querySelector('#tab-'+tab+' .page-head');
    if(head){const note=document.createElement('p');note.className='hint';note.textContent=reason;head.append(note);}
  }
}
if(profile.ring_preview_supported===false){
  for(const id of ['ra-device','ra-install','ra-load-default']){
    const button=document.getElementById(id);if(button){button.disabled=true;button.title='Воспроизведение и подмены анимаций на этой модели пока не поддерживаются';}
  }
}

// Запуск и будильник проверены только на Лайт 2 — на остальных не прячем, а предупреждаем крупно в шапке.
const untested=(tab,what,text)=>{   // what — фраза целиком: «будильник не тестировался и может…», «сценарии … и могут…»
  const head=document.querySelector('#tab-'+tab+' .page-head');if(!head)return;
  const box=document.createElement('div');box.className='untested';box.setAttribute('role','alert');
  box.innerHTML='<svg width="28" height="28" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 2 1 21h22L12 2zm1 15h-2v-2h2v2zm0-4h-2V9h2v4z"/></svg><div><strong></strong><p></p></div>';
  box.querySelector('strong').textContent='На '+profile.name.replace(/^Станция/,'Станции')+' '+what;
  box.querySelector('p').innerHTML=text+' Заработало или нет — напишите, пожалуйста, в <a href="https://github.com/dimap/yapanel-web/issues" target="_blank" rel="noopener">Issues</a> с моделью и версией прошивки.';
  head.append(box);
};
// На пробу (TRIAL_FEATURES в profiles.py): предупреждение, пока функцию для модели не подтвердили люди (tested)
const tried=f=>profile.id!=='lite2'&&!(profile.tested||[]).includes(f);   // Лайт 2 — всё проверено, даже если tested не пришёл
if(!profile.boot)document.getElementById('boot-card')?.setAttribute('data-profile-hidden','');
if(tried('anims')){
  untested('boot','замена анимаций не тестировалась и может работать не так, как надо','Проверена только на Станции Лайт 2. '+(profile.boot?'':'Редактор часов скрыт — он рассчитан на дисплей Лайт 2. ')+'Свои анимации кольца подменяются при загрузке через хук в <code>rootkeeper.sh</code>; если после перезагрузки остались стандартные, значит, на этой модели хук не срабатывает.');
}
if(tried('scenes')&&profile.hook_supported!==false)
  untested('scenes','сценарии не тестировались и могут работать не так, как надо','Проверены только на Станции Лайт 2: их выполняет тот же скрипт на колонке, что и будильник, и запускается он при загрузке через хук в <code>rootkeeper.sh</code>. Цвет кольца в шаге «Кольцо» рассчитан на кольцо Лайт 2 — на других моделях он может не гореть. Проверьте сценарий кнопкой «Запустить».');
if(tried('alarm')&&profile.hook_supported!==false)
  untested('alarm','будильник не тестировался и может работать не так, как надо','Проверен только на Станции Лайт 2: это скрипт на колонке, он будит звуками и голосом Алисы и запускается при загрузке через хук в <code>rootkeeper.sh</code>. Прежде чем полагаться на него утром, проверьте кнопкой «Проверить» и после перезагрузки колонки.');
// звуки и «Сказать» живут в виджетах «Обзора» — там предупреждение строкой, а не плашкой на всю вкладку
const note=(sel,text)=>{const w=document.querySelector(sel);if(!w)return;const p=document.createElement('p');p.className='hint untested-note';p.textContent=text;w.querySelector('.card-head')?.after(p);};
if(tried('sounds'))note('[data-widget="sounds"]','На этой модели замена звуков не тестировалась — проверьте после замены и напишите в Issues, заработало ли.');
if(tried('btpower'))note('[data-widget="bt"]','Выключатель Bluetooth на этой модели не проверялся — напишите в Issues, выключается ли он и остаётся ли выключенным после перезагрузки.');
if(tried('yam2'))note('#y2-card','yam 2 через панель на этой модели не проверялся: установка, каталог и службы пакетов. Напишите в Issues, заработало ли.');
if(tried('btout')){const r=document.getElementById('bt-out-row');if(r){const p=document.createElement('p');p.className='hint untested-note';p.textContent='Звук через Bluetooth-колонку на этой модели не проверялся — напишите в Issues, заработало ли.';r.after(p);}}
if(tried('say'))note('[data-widget="say"]','На этой модели не тестировалось — напишите в Issues, заработало ли.');
if(tried('privacy')){
  const message=document.createElement('p');message.className='hint untested-note';
  message.textContent='Блокировка аналитики на этой модели не проверена. Нужен подтверждённый штатный хук загрузки.';
  document.querySelector('#privacy-row .set-label')?.append(message);
}
if(tried('viz'))document.getElementById('viz-untested')?.removeAttribute('hidden');
const view=document.getElementById('model-view');
if(profile.id==='midi'){
  window.__midiLiveDisplay=true;
  const {mountMidiControls}=await import('./midi.js');mountMidiControls(profile);
  const {mountMidiTests}=await import('./midi-tests.js');mountMidiTests(profile);
  const {mountMidiClockTests}=await import('./midi-clock-tests.js');mountMidiClockTests(profile);
  const message=document.createElement('p');message.className='hint untested-note';
  message.textContent='3D-модель Миди — реконструкция по фото. Доступные функции и результаты проверок показаны в карточках.';
  view.after(message);
}
const loading=document.createElement('p');loading.className='studio-caption';loading.textContent='Ожидаю данные колонки…';view.append(loading);
let status;
for(;;){
  try{
    const response=await PanelContext.fetch('/api/status');
    if(response.ok){const value=await response.json();if(value.hostname||value.hardware_model){status=value;break;}}
  }catch{}
  if(profile.id==='midi'){status={};break;}
  await new Promise(resolve=>setTimeout(resolve,2000));
}
const visual=rendererProfile(profile,status);
loading.remove();
// «Лайт 2 без часов» — тот же корпус, что у Лайт 2, только без дисплея: рисуем модель Lite 2,
// но прячем светящееся окно часов. Иначе model-studio не знает этот профиль и показывает Станцию 2.
window.__modelNoClock=visual.clock===false;
const gallery = document.getElementById("model-gallery");
if (gallery) gallery.href = PanelContext.url("static/model-preview.html") + "?model=" + encodeURIComponent(visual.id);
window.__deviceColor=status.color||window.__deviceColor;
document.title=visual.name+' — yapanel';
document.getElementById('model-name').textContent=visual.name;
view.dataset.hardwareProfile=profile.id;
if(visual.id!==profile.id){
  const note=document.createElement('p');note.className='hint';note.id='model-profile-note';
  note.textContent=visual.id==='lite'
    ? 'Станция Лайт первого поколения. Для управления пока используется профиль «'+profile.name+'»; совместимость аппаратных функций не подтверждена.'
    : '3D: '+visual.name+'. Профиль управления: '+profile.name+'. Для аппаратных функций выберите --model '+visual.id+' при запуске сервера.';
  view.after(note);
}
const hide=element=>element?.setAttribute('data-profile-hidden','');
if(!profile.clock)hide(document.querySelector('[data-widget="clock"]'));
if(!profile.light)hide(document.querySelector('[data-widget="light"]'));
if(profile.bt_volume===false)hide(document.querySelector('.vol-bt'));
if(profile.id!=='lite2'){
  hide(document.getElementById('pat-play'));
  document.querySelectorAll('[data-k="clock"]').forEach(e=>{if(!profile.clock)hide(e);});
  const lightHint=document.querySelector('[data-widget="light"] .hint');
  if(lightHint)lightHint.textContent=profile.light_note;
}
// У Лайт первого поколения нет дисплея: только светящийся логотип Алисы — прячем всё про часы.
if(visual.id==='lite'){
  hide(document.querySelector('[data-widget="clock"]'));hide(document.getElementById('pat-play'));
  document.querySelectorAll('[data-k="clock"]').forEach(hide);
}
try{
  if(visual.id==='lite2'||visual.id==='lite2nc')await import('./model-lite2.js');
  else{
    let studio;
    try{studio=await import('./model-studio.js');}
    catch(e){try{e.panelStage='import model-studio';}catch{}throw e;}
    try{studio.createModel(visual);}
    catch(e){try{e.panelStage='createModel';}catch{}throw e;}
  }
}catch(e){
  // Пустой виджет без объяснения — худший вариант: показываем причину и даём перезагрузиться.
  // stages и полный стек складываем в localStorage, чтобы причину можно было прочитать без консоли.
  const detail=String(e?.message||e), stage=e?.panelStage?e.panelStage+': ':'';
  try{localStorage.setItem('model.lastError',stage+detail+'\n'+(e?.stack||''));}catch{}
  window.__modelError=stage+detail;
  console.error('3D-модель:',stage+detail,e);
  const box=document.createElement('p');box.className='studio-error';
  box.append('Не удалось показать 3D-модель. '+(stage+detail).slice(0,260)+' ');
  const btn=document.createElement('button');btn.className='btn sm';btn.type='button';btn.textContent='Перезагрузить';btn.onclick=()=>location.reload();
  box.append(btn);
  view.append(box);
}
