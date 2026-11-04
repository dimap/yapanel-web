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

const view=document.getElementById('model-view');
const loading=document.createElement('p');loading.className='studio-caption';loading.textContent='Ожидаю данные колонки…';view.append(loading);
let status;
for(;;){
  try{
    const response=await PanelContext.fetch('/api/status');
    if(response.ok){const value=await response.json();if(value.hostname||value.hardware_model){status=value;break;}}
  }catch{}
  await new Promise(resolve=>setTimeout(resolve,2000));
}
const visual=rendererProfile(profile,status);
loading.remove();

const gallery = document.getElementById("model-gallery");
if (gallery) gallery.href = PanelContext.url("static/model-preview.html") + "?model=" + encodeURIComponent(visual.id);
window.__deviceColor=status.color||window.__deviceColor;
document.title=visual.name+' — yapanel';
document.getElementById('model-name').textContent=visual.name;
view.dataset.hardwareProfile=profile.id;

const hide=element=>element?.setAttribute('data-profile-hidden','');
if(!profile.clock)hide(document.querySelector('[data-widget="clock"]'));
if(!profile.light)hide(document.querySelector('[data-widget="light"]'));
if(profile.bt_volume===false)hide(document.querySelector('.vol-bt'));

try{
  let studio;
  try{studio=await import('./model-studio.js');}
  catch(e){try{e.panelStage='import model-studio';}catch{}throw e;}
  try{studio.createModel(visual);}
  catch(e){try{e.panelStage='createModel';}catch{}throw e;}
}catch(e){
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
