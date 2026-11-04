// MIT: approximate gallery-only bodies after Yandex's official specifications and product photos (see SOURCES.md).
// Sizes are in millimetres and match the published width, depth and height. Hardware support is not implied.
import {buildMidi} from './model-midi.js';
import {modelPalette} from './model-colors.js';
const pal=(...items)=>Object.fromEntries(items.map(([key,label,color])=>[key,[label,color]]));
const BLACK=pal(['black','Чёрный','#2f3036']);
// Colour names follow the official "Расцветки" lists on alice.yandex.ru/support.
const STATION=pal(['black','Чёрный','#2f3036'],['grey','Серый','#a9abb1'],['purple','Фиолетовый','#5b43b8']);
const MAX=pal(['graphite','Графит','#34353b'],['beige','Бежевый','#cbbfad'],['green','Зелёный','#2f5a4b'],['turquoise','Бирюза','#3f8f8c'],['blue','Синий','#2f4a7a'],['bordo','Бордовый','#6b2834']);
const MIDI=modelPalette('midi');
const MINI2=modelPalette('mini2');

// Builders receive the studio kit: Three.js, shared materials and mesh helpers.
function clock(kit,text='12:35',color='#fff4e4'){
  return kit.canvasTexture(512,(c,n)=>{c.fillStyle=color;c.shadowColor=color;c.shadowBlur=18;c.font='500 170px system-ui,sans-serif';c.textAlign='center';c.textBaseline='middle';c.fillText(text,n/2,n/2);});
}
// LED displays as on the photos: Max and Station 3 use a dot matrix, Midi and Mini 2 seven-segment digits.
const FONT={'1':'010110010010111','2':'111001111100111','3':'111001111001111','5':'111100111001111',':':'000010000010000'};
function dotClock(kit,cols,rows,text='12:35',color='#fff4e4'){
  const lit=new Set(),glyphs=[...text].map(ch=>FONT[ch]),width=glyphs.reduce((w,g,i)=>w+(i?1:0)+(g===FONT[':']?1:3),0);
  let x=Math.floor((cols-width)/2);const y=Math.floor((rows-5)/2);
  for(const g of glyphs){const w=g===FONT[':']?1:3;for(let r=0;r<5;r++)for(let c=0;c<w;c++)if(g[r*3+(w===1?1:c)]==='1')lit.add((y+r)*cols+x+c);x+=w+1;}
  return kit.canvasTexture(512,(c,n)=>{const p=n/cols,ry=(n-p*rows)/2;for(let r=0;r<rows;r++)for(let k=0;k<cols;k++){const on=lit.has(r*cols+k);
    c.fillStyle=on?color:'rgba(255,255,255,.035)';c.shadowColor=color;c.shadowBlur=on?p*.6:0;c.beginPath();c.arc((k+.5)*p,ry+(r+.5)*p,p*.32,0,7);c.fill();}});
}
const SEGS={'1':'0110000','2':'1101101','3':'1111001','5':'1011011'};
function segClock(kit,text='12:35',color='#fff4e4'){
  return kit.canvasTexture(512,(c,n)=>{c.fillStyle=color;c.shadowColor=color;c.shadowBlur=14;const w=70,h=150,t=14,top=(n-h)/2;let x=40;
    for(const ch of text){if(ch===':'){c.fillRect(x+4,top+42,t,t);c.fillRect(x+4,top+h-56,t,t);x+=34;continue;}
      const on=SEGS[ch],bars=[[t,0,w-2*t,t],[w-t,t,t,h/2-t],[w-t,h/2,t,h/2-t],[t,h-t,w-2*t,t],[0,h/2,t,h/2-t],[0,t,t,h/2-t],[t,h/2-t/2,w-2*t,t]];
      bars.forEach(([bx,by,bw,bh],i)=>{if(on[i]==='1')c.fillRect(x+bx,top+by,bw,bh);});x+=w+26;}});
}
function face(kit,map,w,h,x,y,z,parent){
  const m=kit.mesh(new kit.T.PlaneGeometry(w,h),new kit.T.MeshBasicMaterial({map,transparent:true,depthWrite:false,toneMapped:false}),x,y,z,parent);m.renderOrder=2;return m;
}
// Screens: dark glass with a soft Alice-coloured glow, optional clock.
function screenTexture(kit,text){
  return kit.canvasTexture(512,(c,n)=>{const g=c.createLinearGradient(0,0,n,n);g.addColorStop(0,'#0c0e1a');g.addColorStop(.6,'#1b1838');g.addColorStop(1,'#3b2563');c.fillStyle=g;c.fillRect(0,0,n,n);
    if(text){c.fillStyle='#fff';c.font='600 112px system-ui,sans-serif';c.textAlign='center';c.fillText(text,n/2,n/2+36);}});
}
function cyl(kit,r,h,mat,y,open=false){return kit.mesh(new kit.T.CylinderGeometry(r,r,h,128,1,open),mat,0,y);}
function ring(kit,r1,r2,y){const m=kit.mesh(new kit.T.RingGeometry(r1,r2,64),kit.ledMaterial(),0,y);m.rotation.x=-Math.PI/2;return m;}
function curvedClock(kit,r,y){
  const arc=50/r,m=kit.mesh(new kit.T.CylinderGeometry(r,r,25,48,1,true,-arc/2,arc),new kit.T.MeshBasicMaterial({map:segClock(kit),transparent:true,depthWrite:false,toneMapped:false}),0,y);m.renderOrder=2;
}
// Square towers. H is the full published height; the body is what remains after the top and the base.
// top: 'knob' — volume ring and light ring (Station, Max); 'plate' — plate over a contour light (Station 3).
function square(kit,{w,H,r,top='plate',display,glow=12,foot=0}){
  const {mesh,roundedBox,fabric,plastic,rubber,glass,ledMaterial}=kit,cap=top==='knob'?19:7,base=foot||6,h=H-cap-base,y0=(base-cap)/2;
  mesh(roundedBox(w,h,w,r,3),fabric,0,y0);
  if(foot){mesh(roundedBox(w*.72,foot,w*.72,r*.7,2),rubber,0,y0-h/2-foot/2);}else mesh(roundedBox(w-6,6,w-6,r,1),rubber,0,y0-h/2-3);
  const t=y0+h/2;
  if(top==='knob'){mesh(roundedBox(w,8,w,r,2),plastic,0,t+4);cyl(kit,w*.33,3,ledMaterial(),t+9.5);cyl(kit,w*.31,8,plastic,t+15);ring(kit,4,6,t+19.1);}
  else{mesh(roundedBox(w-1,3,w-1,r,1),ledMaterial(),0,t+1.5);mesh(roundedBox(w,4,w,r,1),glass,0,t+5);ring(kit,glow*.75,glow,t+7.1);}
  if(display){const d=typeof display==='number'?{w:display,h:display/2,map:clock(kit)}:display(kit);face(kit,d.map,d.w,d.h,0,y0+h*.12,w/2+.6);}
  return {topY:H/2,baseY:-H/2,t};
}
function puck(kit,{r,H,display}){
  const {rubber,plastic,glass,ledMaterial,fabric}=kit,h=H-8,y0=-1;
  cyl(kit,r,h,fabric,y0);cyl(kit,r-4,3,rubber,y0-h/2-1.5);cyl(kit,r,4,plastic,y0+h/2+2);cyl(kit,r-.6,1,glass,y0+h/2+4.5);
  cyl(kit,r+.3,2,ledMaterial(),y0+h/2-1,true);ring(kit,4,5.5,y0+h/2+5.05);
  if(display)curvedClock(kit,r+.25,y0-2);
  return {topY:H/2,baseY:-H/2};
}
// TVs use the published millimetres (alice.yandex.ru/support/ru/tv/specs): W × H × D without the feet, Hs × Ds with the feet,
// distance between the feet at the front and back. Built full size, then scaled into the studio camera range.
function tv(kit,{W,H,D,Hs,Ds,front,back,bezel=10}){
  const {mesh,roundedBox,plastic,rubber,ledMaterial,fabric,T}=kit,lift=Hs-H,y=lift+H/2;
  mesh(roundedBox(W,H,D*.45,4,1.5),fabric,0,y,-D*.22);mesh(roundedBox(W*.55,H*.55,D*.55,30,4),fabric,0,y-H*.08,-D*.6);
  mesh(new T.PlaneGeometry(W-bezel*2,H-bezel*2-6),new T.MeshBasicMaterial({map:screenTexture(kit),toneMapped:false}),0,y+3,1.5);
  mesh(roundedBox(60,7,6,2,1),ledMaterial(),0,lift+3,0);
  for(const s of[-1,1]){const fx=s*front/2,bx=s*back/2,dz=Ds*.92,len=Math.hypot(fx-bx,dz);
    const foot=mesh(roundedBox(18,10,len,5,2),plastic,(fx+bx)/2,5,0);foot.rotation.y=Math.atan2(fx-bx,dz);
    mesh(roundedBox(22,lift+20,14,4,2),rubber,(fx+bx)/2,(lift+20)/2,-D*.2);}
  kit.group.scale.setScalar(.25);
  return {topY:Hs*.25,baseY:0};
}

export const CONCEPTS={
  // 141 × 141 × 231 mm; pixel LED display 25 × 16 behind the fabric, volume ring on top.
  max:{name:'Яндекс Станция Макс',colors:MAX,build:kit=>square(kit,{w:141,H:231,r:10,top:'knob',display:kit=>({w:84,h:84,map:dotClock(kit,25,16)})})},
  // 132 × 132 × 201 mm; LED display 21 × 12, light under the top glass, ambient light on the back, raised on a foot.
  station3:{name:'Яндекс Станция 3',colors:STATION,build:kit=>{
    const s=square(kit,{w:132,H:201,r:30,glow:34,display:kit=>({w:60,h:60,map:dotClock(kit,21,12)}),foot:10}),{mesh,roundedBox,ledMaterial}=kit;
    mesh(roundedBox(84,5,2,2,.8),ledMaterial(),0,-201/2+28,-132/2-.6);return s;}},
  // 96 × 96 × 110 mm; 45-segment display, contour light beneath a matte plastic top.
  // Detailed knitted shell and plastic controls; live visual adapter, no baked demo clock.
  midi:{name:'Яндекс Станция Миди',colors:MIDI,build:buildMidi},
  // 83 mm × 202 mm; usually lies on its side, the studio turns it after building. Button strip with the Alice button, strap loop.
  street:{name:'Яндекс Станция Стрит',lying:true,colors:pal(['black','Чёрный','#2c2d31'],['green','Зелёный','#a8a46a'],['orange','Оранжевый','#d9792f'],['purple','Фиолетовый','#6a55b8'],['grey','Серый','#b9bbbe']),build:kit=>{
    const {mesh,roundedBox,rubber,plastic,ledMaterial,T}=kit,R=41.5;cyl(kit,R,182,kit.fabric,0);cyl(kit,R+.4,10,rubber,-96);cyl(kit,R+.4,10,rubber,96);
    mesh(roundedBox(17,96,6,8,2),plastic,0,8,R-1.5);
    for(let i=0;i<5;i++){if(i===2)continue;const b=mesh(new T.CylinderGeometry(3.6,3.6,1.6,32),rubber,0,44-i*18,R+1.8);b.rotation.x=Math.PI/2;}
    mesh(new T.TorusGeometry(4.6,1.2,12,48),ledMaterial(),0,8,R+1.9);
    const loop=mesh(new T.TorusGeometry(10,2.2,12,48),rubber,0,104,-R+12);loop.rotation.y=Math.PI/2;
    return {topY:101,baseY:-101};}},
  // 256 × 118 × 240 mm; 10.5″ 1920 × 1200 touch screen on a tilting hinge in front of the speaker base.
  duomax:{name:'Яндекс Станция Дуо Макс',colors:pal(['black','Чёрный','#2f3036'],['bordo','Бордо','#5e2731'],['beige','Бежевый','#cbbfad'],['green','Зелёный','#2f5a4b']),build:kit=>{
    // The speaker sits centred under the screen; the screen hangs on a hinge at the top front and leans back (official photos).
    const {mesh,roundedBox,glass,rubber,plastic,T}=kit;mesh(roundedBox(136,132,104,16,3),kit.fabric,0,68,-7);mesh(roundedBox(128,4,96,14,1),rubber,0,2,-7);
    mesh(roundedBox(130,6,98,15,2),plastic,0,137,-7);mesh(roundedBox(60,22,26,6,2),plastic,0,132,30);
    const screen=new T.Group();screen.position.set(0,152,40);screen.rotation.x=-.34;kit.group.add(screen);mesh(roundedBox(256,168,10,9,2),glass,0,0,0,screen);
    mesh(new T.PlaneGeometry(232,145),new T.MeshBasicMaterial({map:screenTexture(kit,'12:35'),toneMapped:false}),0,0,5.2,screen);return {topY:240,baseY:0};}},
  // 141 × 141 × 231 mm (2018); same body as the first Max, without the display.
  station:{name:'Яндекс Станция',colors:STATION,build:kit=>square(kit,{w:141,H:231,r:10,top:'knob'})},
  // 97 × 54 mm; the clock version has a 32-LED display behind the fabric.
  mini2nc:{name:'Яндекс Станция Мини без часов',colors:MINI2,build:kit=>puck(kit,{r:48.5,H:54})},
  // 90 × 45 mm, gesture sensor on top.
  mini1:{name:'Яндекс Станция Мини (версия 1)',colors:pal(['black','Чёрный','#28292e'],['white','Белый','#dedede']),build:kit=>puck(kit,{r:45,H:45})},
  // 43″ 966 × 578 × 84 mm, 638 × 311 mm with feet (first generation). The specification does not give the feet spacing; it is estimated from photos.
  tv:{name:'ТВ Станция',colors:BLACK,build:kit=>tv(kit,{W:966,H:578,D:84,Hs:638,Ds:311,front:800,back:620})},
  tvminiled:{name:'ТВ Станция MiniLED',colors:BLACK,build:kit=>tv(kit,{W:1226,H:723,D:67,Hs:768,Ds:276,front:1142,back:934,bezel:8})},
  tvprominiled:{name:'ТВ Станция Про MiniLED',colors:BLACK,build:kit=>tv(kit,{W:1226,H:723,D:78,Hs:777,Ds:298,front:1071,back:952,bezel:8})},
  // Second generation (QLED), 43″.
  tv2:{name:'ТВ Станция 2',colors:BLACK,build:kit=>tv(kit,{W:965,H:579,D:84,Hs:636,Ds:303,front:731,back:527})},
  tvbasic:{name:'ТВ Станция Бейсик',colors:BLACK,build:kit=>tv(kit,{W:958,H:570,D:81,Hs:609,Ds:230,front:885,back:746,bezel:12})},
  tvbasicqled:{name:'ТВ Станция Бейсик QLED',colors:BLACK,build:kit=>tv(kit,{W:958,H:570,D:81,Hs:609,Ds:230,front:885,back:746,bezel:12})},
  // Drops: TWS charging case with the lid open and both earbuds in front, after the manufacturer's photos.
  drops:{name:'Яндекс Дропс',colors:pal(['white','Белый','#eeedea'],['black','Чёрный','#25262b']),build:kit=>{
    const {mesh,roundedBox,canvasTexture,ledMaterial,fabric,T}=kit,W=60,H=34,Dp=24;fabric.roughness=.3;   // glossy plastic
    const ink=new T.MeshStandardMaterial({color:'#0b0b0d',roughness:.15}),cup=new T.MeshStandardMaterial({color:'#222328',roughness:.12}),metal=new T.MeshStandardMaterial({color:'#b9bbc0',roughness:.35,metalness:.8});
    const decal=(map,w,h,x,y,z,rx,ry)=>{const m=mesh(new T.PlaneGeometry(w,h),new T.MeshStandardMaterial({map,transparent:true,roughness:.4,polygonOffset:true,polygonOffsetFactor:-1}),x,y,z);m.rotation.set(rx,ry,0);};
    mesh(roundedBox(W,H,Dp,10,3),fabric);mesh(roundedBox(W-6,1,Dp-6,8,.4),ink,0,H/2+.1);
    for(const s of[-1,1]){const c=mesh(new T.SphereGeometry(8,32,16),cup,s*13,H/2,0);c.scale.set(1.25,.3,.75);}
    const lid=new T.Group();lid.position.set(0,H/2,-Dp/2);lid.rotation.x=-1.8;kit.group.add(lid);
    mesh(roundedBox(W,12,Dp,10,3),fabric,0,5,Dp/2-1.5,lid);mesh(roundedBox(W-6,1,Dp-6,8,.4),ink,0,-1.2,Dp/2-1.5,lid);
    for(const s of[-1,1]){const c=mesh(new T.SphereGeometry(8,32,16),cup,s*13,-1.3,Dp/2-1.5,lid);c.scale.set(1.25,.3,.75);}
    mesh(roundedBox(26,6,1,1,.3),metal,0,H/2-4,-Dp/2-.2);                                     // hinge
    decal(canvasTexture(256,(c,n)=>{c.fillStyle='#c4c4c8';c.beginPath();c.arc(n/2,n/2,n/2-4,0,7);c.fill();}),9,9,0,H/2-15,-Dp/2-.05,0,Math.PI);
    decal(canvasTexture(512,(c,n)=>{c.strokeStyle='#cfcfd2';c.lineWidth=5;c.beginPath();c.arc(150,n/2,44,0,7);c.stroke();c.fillStyle='#d8d8dc';c.beginPath();c.roundRect(240,n/2-26,150,52,26);c.fill();c.fillStyle='#16161a';c.beginPath();c.roundRect(252,n/2-16,126,32,16);c.fill();}),Dp-4,Dp-4,0,-H/2-.05,0,Math.PI/2,0);   // button and USB-C
    const led=mesh(new T.CircleGeometry(1.2,16),ledMaterial(),0,6,Dp/2+.05);led.renderOrder=2;
    for(const[x,z,turn]of[[-22,34,.35],[24,30,Math.PI-.45]]){const bud=new T.Group();bud.position.set(x,-H/2+7,z);bud.rotation.y=turn;kit.group.add(bud);
      const stem=mesh(new T.CapsuleGeometry(3.4,22,8,16),fabric,9,-1,0,bud);stem.rotation.z=Math.PI/2;
      mesh(new T.SphereGeometry(7,32,16),fabric,-6,0,0,bud);const tip=mesh(new T.SphereGeometry(6.6,32,16),ink,-11,0,0,bud);tip.scale.set(.55,1,1);}
    return {topY:H/2+30,baseY:-H/2};}},
};
