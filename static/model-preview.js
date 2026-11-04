import {startAsciiBackground} from './ascii-background.js';
import * as geometry from './clock-geometry.js';
import {CONCEPTS} from './model-concepts.js';
window.__modelPreview=true;
const supported={lite:'Станция Лайт',lite2:'Станция Лайт 2',mini3:'Станция Мини 3',mini3pro:'Станция Мини 3 Про',mini2:'Станция Мини 2 с часами',station2:'Станция 2'};
const experimental={midi:'Станция Миди — экспериментальный профиль'};
// Заготовки для будущих участников: только внешний вид, аппаратная совместимость не проверена.
const concepts={lite2nc:'Станция Лайт 2 без часов',...Object.fromEntries(Object.entries(CONCEPTS).filter(([key])=>!(key in experimental)).map(([key,model])=>[key,model.name]))};
const catalog={...supported,...experimental,...concepts};
const requested=new URLSearchParams(location.search).get('model');
const id=Object.prototype.hasOwnProperty.call(catalog,requested)?requested:'lite2';
const picker=document.getElementById('preview-model');
for(const [label,models]of[['Поддерживаются панелью',supported],['Экспериментальные профили',experimental],['Заготовки — совместимость не проверена',concepts]]){
  const group=document.createElement('optgroup');group.label=label;
  for(const [key,name]of Object.entries(models))group.append(new Option(name,key));
  picker.append(group);
}
picker.value=id;
picker.addEventListener('change',()=>{const url=new URL(location.href);url.searchParams.set('model',picker.value);location.assign(url);});
const lite2=id==='lite2'||id==='lite2nc';
window.clockGeom=geometry;window.__deviceColor=id==='lite2'?'copper':id==='lite2nc'?'blue':id==='lite'?'purple':id==='mini2'?'white':CONCEPTS[id]?Object.keys(CONCEPTS[id].colors)[0]:'black';
document.getElementById('model-description').textContent=id in experimental
  ?'Миди: корпус 96 × 96 × 110 мм с текстурой ткани, матовой сенсорной панелью, четырьмя микрофонами, контурным светом и задним USB-C. Приблизительная реконструкция по официальным фото, MIT. Часы и подсветка здесь демонстрационные; проверка на колонке проводится отдельно.'
  :id in concepts
  ?'Заготовка: приблизительная форма по фото производителя, чтобы будущим участникам не пришлось рисовать модель. Совместимость с настоящей колонкой не проверена, панель её пока не поддерживает.'
  :id==='lite2'?'Авторская STL vanyabeat, CC BY-SA 4.0. Геометрия, материалы и свет исходного рендера сохранены.'
  :id==='mini2'?'Приблизительная реконструкция Станции Мини 2 с часами по фото и замерам владельца (97 × 54 мм): дисплей часов сквозь ткань, кольцо Алисы на 6 RGB и четыре официальных цвета. Это не официальный CAD-файл.'
  :'Приблизительная визуальная реконструкция по официальным ракурсам. Лёгкие материалы без теней. Это не официальный CAD-файл.';
const demo=()=>{if(id!=='lite2nc')window.modelShow?.(geometry.textToFrame('12:34',190));window.modelRing?.(id==='midi'?[[125,58,244],[83,91,255],[59,173,231],[171,75,246],[229,83,174],[152,58,245]]:Array.from({length:id==='mini3pro'?16:id==='mini2'?6:id==='lite'?12:3},(_,i)=>[90+i*3,45,170+i*4]));};
document.getElementById('model-view').addEventListener('modelready',demo,{once:true});
if(lite2)await import('./model-lite2.js');
else{const{createModel}=await import('./model-studio.js');createModel({id,name:catalog[id]});}
demo();


startAsciiBackground(document.getElementById('model-viz'));
