// Visual identity is independent of the explicitly selected hardware command profile.
export const MODEL_NAMES={max:'Станция Макс',midi:'Станция Миди',lite:'Станция Лайт',lite2:'Станция Лайт 2',lite2nc:'Станция Лайт 2 (без часов)',mini3:'Станция Мини 3',mini3pro:'Станция Мини 3 Про',mini2:'Станция Мини 2 с часами',mini2nc:'Станция Мини 2 (без часов)',station2:'Станция 2'};

export function rendererProfile(profile,status={}){
  const hardware=String(status.hardware_model||'').toLowerCase();
  const hostname=String(status.hostname||'').trim().toLowerCase();
  let id;
  // station-midi/yandexmidi is Station 2; cucumber/cucumber-dvt2 is Midi; yandexstation_2 is Max.
  // Device-tree identity wins over a hostname that the owner may have renamed.
  for(const [platform,model]of Object.entries({yandexstation_2:'max',yandexstation:'max','station-max':'max',cucumber:'midi',bergamot:'mini3',pickle:'mini3pro',plum:'lite2',prune:'lite2nc',yandexmidi:'station2','station-midi':'station2','station midi':'station2','station-mini2':'mini2'})){
    if(new RegExp('(^|[^a-z0-9])'+platform+'([^a-z0-9]|$)').test(hardware)){id=model;break;}
  }
  if(!id&&/\byandexstation_2\b/.test(hardware))id='max';
  if(!id&&/\bstation[ -]max\b/.test(hardware))id='max';
  if(!id&&/\bstation[ -]micro\b/.test(hardware))id='lite';
  if(!id&&/\bstation[ -]mini[ -]?2\b/.test(hardware))id='mini2';
  if(!id&&/\bstation[ -]midi\b/.test(hardware))id='midi';
  // Test Pro first: it shares the Mini 3 prefix, but has a different body and ring.
  if(!id){
    const patterns=[
      ['max',/^yandex-station-max(?:-|$)/],
      ['midi',/^yandex-station-midi(?:-|$)/],
      ['mini3pro',/^yandex-mini(?:-gen|-)?3-?pro(?:-|$)/],
      ['mini3',/^yandex-mini(?:-gen|-)?3(?:-|$)/],
      ['mini2',/^yandex-mini(?:-gen|-)?2(?:-|$)/],
      ['lite2',/^yandex-lite(?:-gen|-)?2(?:-|$)/],
      ['lite',/^yandex-station-lite$/],
      ['station2',/^yandex-station(?:-gen|-)?2(?:-|$)/],
    ];
    id=patterns.find(([,pattern])=>pattern.test(hostname))?.[0];
  }
  id=id||profile.id;
  if(id==='mini2'&&profile.id==='mini2nc')id='mini2nc';
  // Плата plum у Лайт 2 одна и та же с часами и без: если профиль уже определён как «без часов»
  // (по светодиодам), не подменяем его на clock-версию из device-tree. Станция 2 в device-tree
  // тоже встречается у таких плат — визуально это всё равно корпус Лайт 2.
  if(profile.id==='lite2nc'&&(id==='lite2'||id==='station2'))id='lite2nc';
  return {...profile,id,name:MODEL_NAMES[id]||profile.name};
}
