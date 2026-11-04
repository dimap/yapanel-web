// MIT: original visual reconstructions; see models/SOURCES.md.
import * as T from 'three';
import { OrbitControls } from './vendor/three/OrbitControls.js';
import {modelSettings} from './model-settings.js';
import {CONCEPTS} from './model-concepts.js';
import * as G from './clock-geometry.js';
export function createModel(profile){
const view=document.getElementById('model-view'),concept=CONCEPTS[profile.id],lite=profile.id==='lite',midi=profile.id==='midi',mini2=profile.id==='mini2',mini=lite||mini2||profile.id==='mini3pro'||profile.id==='mini3';
view.setAttribute('role','group');view.setAttribute('aria-label','3D-модель: вращайте мышью или пальцем');view.classList.add('studio-view');
// Preserve upstream visualizer canvas and the references held by its animation loop.
// Лайт первого поколения: цвета как в её ledconfigs (purple, yellow, green, pink, red, beige).
// Мини 2: четыре официальных цвета; третий элемент — пластик в цвет корпуса (крышка, задняя планка, ярлык, ободок).
const palette=concept?concept.colors:lite?{purple:['Фиолетовый','#6a3fe6'],yellow:['Жёлтый','#f0c43a'],green:['Зелёный','#59b96d'],pink:['Розовый','#ec2c7c'],red:['Красный','#d9473f'],beige:['Бежевый','#d8c6ab']}:mini2?{white:['Белый','#dcd8d1','#e8e5df'],black:['Чёрный','#2b2b2f','#232326'],red:['Красный','#6e2634','#601f2d'],blue:['Синий','#2d4070','#273863']}:profile.id==='mini3'?{black:['Чёрный','#34343a'],grey:['Серый','#a0a2a8'],violet:['Лиловый','#9b8eaa'],teal:['Бирюзовый','#6aa6a7']}:mini?{black:['Графит','#34343a'],grey:['Серый','#888b91'],blue:['Синий','#355d82']}:{black:['Антрацит','#3b3c40'],blue:['Кобальт','#485c85'],copper:['Медный','#a66d5d'],sand:['Песочный','#c5baaa'],red:['Рубин','#8c3447']};
let renderer;
try{renderer=new T.WebGLRenderer({antialias:true,alpha:true,powerPreference:'low-power'});}catch(e){view.insertAdjacentHTML('beforeend','<p class="studio-error">Для объёмной модели включите WebGL в браузере.</p>');throw e;}
renderer.domElement.className='studio-canvas';renderer.domElement.setAttribute('aria-label',profile.name+' — вращаемая 3D-модель');
renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.outputColorSpace=T.SRGBColorSpace;renderer.toneMapping=T.ACESFilmicToneMapping;renderer.toneMappingExposure=1.15;
view.appendChild(renderer.domElement);
// Служебная подпись поверх модели: потеря контекста или ошибка отрисовки не оставляют пустое место без объяснения.
const note=document.createElement('p');note.className='studio-caption';note.hidden=true;view.append(note);
let dirty=true,failed=false;
renderer.domElement.addEventListener('webglcontextlost',e=>{e.preventDefault();note.hidden=false;note.textContent='3D-модель перезапускается…';});
renderer.domElement.addEventListener('webglcontextrestored',()=>{note.hidden=true;failed=false;dirty=true;});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)dirty=true;});
window.addEventListener('focus',()=>{dirty=true;});
window.addEventListener('pageshow',()=>{dirty=true;});
const scene=new T.Scene(),camera=new T.PerspectiveCamera(30,1,1,2500),controls=new OrbitControls(camera,renderer.domElement);
controls.enableDamping=true;controls.enablePan=false;controls.minDistance=mini?135:240;controls.maxDistance=mini?520:820;controls.maxPolarAngle=Math.PI*.91;controls.autoRotateSpeed=.7;
// Сенсорное вращение и масштабирование модели на телефонах и планшетах
controls.enabled=true;
renderer.domElement.style.touchAction='none';
const group=new T.Group();scene.add(group);
scene.add(new T.HemisphereLight(0xf1f4ff,0x45404b,2));
const keyLight=new T.DirectionalLight(0xfff5e9,3.2);keyLight.position.set(-140,220,180);scene.add(keyLight);
const fill=new T.DirectionalLight(0xd7e4ff,1.5);fill.position.set(160,80,-100);scene.add(fill);
// прорези по краю крышки Лайт: 72 наклонных овала (R — радиус крышки в пикселях холста)
function liteSlots(c,R,w=22,h=56,ring=.905){const k=R/512;c.save();c.translate(R,R);for(let i=0;i<72;i++){c.save();c.rotate(i/72*Math.PI*2);c.translate(0,-R*ring);c.rotate(.62);
  c.beginPath();c.roundRect(-w/2*k,-h/2*k,w*k,h*k,w/2*k);c.fill();c.restore();}c.restore();}
function canvasTexture(size,draw){const canvas=document.createElement('canvas');canvas.width=canvas.height=size;draw(canvas.getContext('2d'),size);const texture=new T.CanvasTexture(canvas);texture.colorSpace=T.SRGBColorSpace;return texture;}
// Match the lightweight material budget of the original Lite 2 renderer.
const fallback=Object.keys(palette)[0];
const fabric=new T.MeshStandardMaterial({color:palette[fallback][1],roughness:.92,metalness:.02});
const plastic=new T.MeshStandardMaterial({color:'#27282d',roughness:.55,metalness:.04});
const rubber=new T.MeshStandardMaterial({color:'#101114',roughness:.85});
const glass=new T.MeshStandardMaterial({color:'#303139',roughness:.35,metalness:.1});
function mesh(geo,mat,x=0,y=0,z=0,parent=group){const m=new T.Mesh(geo,mat);m.position.set(x,y,z);parent.add(m);return m;}
function roundedShape(w,d,r){const s=new T.Shape(),x=-w/2,z=-d/2;s.moveTo(x+r,z);s.lineTo(x+w-r,z);s.quadraticCurveTo(x+w,z,x+w,z+r);s.lineTo(x+w,z+d-r);s.quadraticCurveTo(x+w,z+d,x+w-r,z+d);s.lineTo(x+r,z+d);s.quadraticCurveTo(x,z+d,x,z+d-r);s.lineTo(x,z+r);s.quadraticCurveTo(x,z,x+r,z);return s;}
function roundedBox(w,h,d,r,b=1){const geo=new T.ExtrudeGeometry(roundedShape(w-2*b,d-2*b,Math.max(1,r-b)),{depth:h-2*b,bevelEnabled:true,bevelThickness:b,bevelSize:b,bevelSegments:5,steps:1,curveSegments:16});geo.rotateX(-Math.PI/2);geo.translate(0,-h/2+b,0);geo.computeVertexNormals();return geo;}
const ledMaterials=[],trimMaterials=[];   // trim: материалы в цвет пластика корпуса (перекрашиваются вместе с ним)
function ledMaterial(){const m=new T.MeshStandardMaterial({color:'#181820',emissive:'#000000',emissiveIntensity:0,roughness:.28,metalness:.05});ledMaterials.push(m);return m;}
function port(y,z){mesh(roundedBox(10,4,1.5,1.9,.3),rubber,0,y,z);mesh(roundedBox(6,1.5,1.8,.7,.2),new T.MeshStandardMaterial({color:'#727078',metalness:.7,roughness:.5}),0,y,z-.15);}
let topY,baseY,clockSegments=[],display=null,details={};
if(concept){details=concept.build({T,group,mesh,roundedBox,canvasTexture,fabric,plastic,rubber,glass,ledMaterial,renderer,scene});({topY,baseY}=details);display=details.display||null;}
else if(lite){
  // По фото: прямой цилиндр 88 мм с острым верхним краем (корпус ~44 мм) на чёрном основании ~12 мм,
  // которое сужается книзу и изрезано наклонными канавками.
  mesh(new T.LatheGeometry([[0,-16],[43.7,-16],[44,-15.7],[44,27.6],[43.6,28]].map(p=>new T.Vector2(...p)),160),fabric);
  const cap=mesh(new T.CircleGeometry(43.6,160),fabric,0,28);cap.rotation.x=-Math.PI/2;   // плоская крышка: без сглаженных нормалей края
  // шаг канавок — ровно 1/22 ширины текстуры, иначе на стыке по кругу виден шов
  const ribs=canvasTexture(512,(c,n)=>{c.fillStyle='#3b3c42';c.fillRect(0,0,n,n);c.strokeStyle='#060607';c.lineWidth=11;
    for(let i=-22;i<44;i++){const x=i*n/22;c.beginPath();c.moveTo(x,n*.06);c.lineTo(x+n*.2,n*.94);c.stroke();}});
  ribs.wrapS=T.RepeatWrapping;ribs.repeat.set(3,1);
  mesh(new T.LatheGeometry([[0,-28],[38.6,-28],[40.2,-27.3],[42,-25],[43.3,-20.5],[43.7,-16]].map(p=>new T.Vector2(...p)),160),
    new T.MeshStandardMaterial({color:'#ffffff',map:ribs,roughness:.8}));
  // Светится индикатор в центре крышки (прорези декоративные, часов нет): в покое белый, при подсветке — её цвет.
  // Нейтральный круг, а не логотип производителя.
  const logoTex=canvasTexture(256,(c,n)=>{c.translate(n/2,n/2);const t=70;
    c.beginPath();c.arc(0,0,t,0,Math.PI*2);
    const g=c.createRadialGradient(0,0,6,0,0,t);g.addColorStop(0,'#ffffff');g.addColorStop(1,'#dcdce2');c.fillStyle=g;c.fill();});
  const logo=new T.MeshBasicMaterial({map:logoTex,transparent:true,depthWrite:false,toneMapped:false,polygonOffset:true,polygonOffsetFactor:-2,polygonOffsetUnits:-2});
  logo.userData.logo=true;ledMaterials.push(logo);
  const logoMesh=mesh(new T.PlaneGeometry(15,15),logo,0,28);logoMesh.rotation.x=-Math.PI/2;logoMesh.renderOrder=2;
  // плоская чёрная пластина на стыке корпуса и основания (без надписей)
  const side=.62,tagGroup=new T.Group();tagGroup.position.set(44.45*Math.sin(side),-9.5,44.45*Math.cos(side));tagGroup.rotation.y=side;group.add(tagGroup);
  const tagTex=canvasTexture(256,(c,n)=>{c.fillStyle='#1d1e22';c.fillRect(0,0,n,n);});
  mesh(new T.BoxGeometry(10.5,15,.9),[0,0,0,0,1,0].map(i=>new T.MeshStandardMaterial(i?{color:'#ffffff',map:tagTex,roughness:.75}:{color:'#1d1e22',roughness:.75})),0,0,0,tagGroup);
  // USB-C сзади — рисунком на корпусе: скруглённое окно, внутри тёмный проём и светлый язычок
  const usb=canvasTexture(256,(c,n)=>{c.translate(n/2,n/2);c.fillStyle='#9a9aa2';c.beginPath();c.roundRect(-118,-44,236,88,44);c.fill();
    c.fillStyle='#141418';c.beginPath();c.roundRect(-106,-33,212,66,33);c.fill();c.fillStyle='#5d5d66';c.beginPath();c.roundRect(-64,-9,128,18,5);c.fill();});
  const usbDecal=mesh(new T.PlaneGeometry(9.6,9.6),new T.MeshStandardMaterial({map:usb,transparent:true,roughness:.5,metalness:.3,polygonOffset:true,polygonOffsetFactor:-1}),0,-9,-44.03);
  usbDecal.rotation.y=Math.PI;
  topY=28;baseY=-28;
}else if(mini2){
  // Станция Мини 2 с часами (YNDX-00020) по фото и штангенциркулю владельца и паспортным 97 × 54 мм: прямой
  // цилиндр в ткани, верхний край скруглён плотно (R≈2,2 мм), за ним плоский тканевый поясок и чуть
  // утопленный пластиковый диск крышки. Пластик — в цвет корпуса, резиновое дно серое. Дисплей — ровно
  // посередине высоты, светит сквозь ткань спереди. Профили — дугами по точкам, сегментов по кругу 256.
  const R=48.5,TOP=27,BOT=-27,N=256,V=p=>new T.Vector2(...p);
  const arcPts=(cx,cy,r,a0,a1,n)=>Array.from({length:n+1},(_,i)=>{const a=a0+(a1-a0)*i/n;return[cx+r*Math.cos(a),cy+r*Math.sin(a)];});
  // Внешняя оболочка — ткань: сетчатая вязка с шагом ≈0,9 мм, светлые нити по диагоналям и тёмные
  // ромбики ячеек между ними (как на фото вблизи); цвет корпуса умножается на карту, бликов нет.
  const knit=canvasTexture(256,(c,n)=>{c.fillStyle='#8a8a8e';c.fillRect(0,0,n,n);
    for(const[w,col]of[[30,'#e3e3e5'],[9,'#fbfbfb']]){c.strokeStyle=col;c.lineWidth=w;c.beginPath();
      for(let i=-4;i<=8;i++){const x=i*n/4;c.moveTo(x,0);c.lineTo(x+n,n);c.moveTo(x,n);c.lineTo(x+n,0);}c.stroke();}});
  knit.wrapS=knit.wrapT=T.RepeatWrapping;knit.repeat.set(85,15);knit.anisotropy=4;fabric.map=knit;fabric.roughness=1;
  mesh(new T.CylinderGeometry(42,42,.8,N),new T.MeshStandardMaterial({color:'#6b6c70',emissive:'#232327',roughness:.9}),0,BOT+.2);   // резиновое дно (снизу света нет — лёгкая подсветка материалом)
  mesh(new T.LatheGeometry([[41,BOT+.4],[46.8,BOT+.4],...arcPts(46.8,BOT+2.2,1.8,-Math.PI/2,0,8),[48.6,BOT+2.6]].map(V),N),plastic);   // нижний ободок
  const shell=new T.LatheGeometry([[48.5,BOT+2.6],[48.5,TOP-2.2],...arcPts(46.3,TOP-2.2,2.2,0,Math.PI/2,16),[44.95,TOP],...arcPts(44.95,TOP-.45,.45,Math.PI/2,Math.PI,5),[44.5,TOP-1.6]].map(V),N);
  const shellUV=shell.attributes.uv,shellPos=shell.attributes.position;for(let i=0;i<shellUV.count;i++)shellUV.setY(i,(shellPos.getY(i)-BOT)/(TOP-BOT));   // вязка без растяжения
  mesh(shell,fabric);
  mesh(new T.CylinderGeometry(44.6,44.6,.4,N),rubber,0,TOP-1.8);   // тёмный зазор между пояском и диском
  mesh(new T.LatheGeometry([[0,TOP-2.1],[44.1,TOP-2.1],[44.1,TOP-.85],[43.75,TOP-.5],[0,TOP-.5]].map(V),N),plastic);   // диск крышки с фаской, верх на 0,5 мм ниже пояска
  // Крышка по фото сверху: 72 узкие наклонные прорези у края, 4 микрофона по диагоналям, «−» слева и «+» справа
  // (если смотреть на дисплей), в центре вставка с кольцом подсветки и логотипом Алисы.
  const marks=canvasTexture(1024,(c,n)=>{const R2=n/2,k=R2/44;c.fillStyle='rgba(8,8,12,.92)';liteSlots(c,R2,12,60,.915);c.translate(R2,R2);
    c.fillStyle='rgba(8,8,12,.85)';for(const[x,y]of[[-1,-1],[1,-1],[-1,1],[1,1]]){c.beginPath();c.arc(x*R2*.34,y*R2*.34,.75*k,0,Math.PI*2);c.fill();}
    c.strokeStyle='rgba(20,18,28,.32)';c.lineWidth=.55*k;c.lineCap='round';const d=R2*.55,l=2.4*k;
    c.beginPath();c.moveTo(-d-l,0);c.lineTo(-d+l,0);c.moveTo(d-l,0);c.lineTo(d+l,0);c.moveTo(d,-l);c.lineTo(d,l);c.stroke();});
  marks.anisotropy=8;   // крышка видна под острым углом — иначе прорези размываются
  const marksMesh=mesh(new T.CircleGeometry(43.7,N),new T.MeshBasicMaterial({map:marks,transparent:true,depthWrite:false,polygonOffset:true,polygonOffsetFactor:-1,polygonOffsetUnits:-1}),0,TOP-.45);
  marksMesh.rotation.x=-Math.PI/2;marksMesh.renderOrder=1;
  // Вставка подсветки: кольцо 4,3–6,9 мм светится цветом колонки; без подсветки — белый пластик. Логотип производителя не рисуем.
  const alice=canvasTexture(256,(c,n)=>{c.translate(n/2,n/2);const k=n/16;c.lineWidth=2.6*k;c.strokeStyle='#ffffff';c.beginPath();c.arc(0,0,5.6*k,0,Math.PI*2);c.stroke();
    c.strokeStyle='rgba(60,60,70,.3)';c.lineWidth=.22*k;for(const r of[6.95,4.25]){c.beginPath();c.arc(0,0,r*k,0,Math.PI*2);c.stroke();}});
  const aliceMat=new T.MeshBasicMaterial({map:alice,transparent:true,depthWrite:false,toneMapped:false,polygonOffset:true,polygonOffsetFactor:-2,polygonOffsetUnits:-2});
  aliceMat.userData.logo=true;ledMaterials.push(aliceMat);
  const aliceMesh=mesh(new T.PlaneGeometry(16,16),aliceMat,0,TOP-.4);aliceMesh.rotation.x=-Math.PI/2;aliceMesh.renderOrder=2;
  // Отдельное кольцо диффузора 4,3–6,9 мм: даёт заметное свечение поверх текстуры, без света — белый пластик.
  const ringMat=ledMaterial();ringMat.color.set('#ffffff');
  const ringMesh=mesh(new T.RingGeometry(4.3,6.9,64),ringMat,0,TOP-.35);ringMesh.rotation.x=-Math.PI/2;ringMesh.renderOrder=3;
  // Дисплей: 4 цифры по 7 сегментов, двоеточие, дефис и градус (32 светодиода, точек у цифр нет) — в геометрии
  // панели (clock-geometry.js, 1 ед. = 0,215 мм: цифра 12 × 21 мм, ряд ≈ 64 мм), центр ровно посередине высоты.
  // Сетка ткани вырезает из сегментов точки, под ними размытое свечение; перерисовка только при смене кадра.
  const W=1032,H=348,canvas=document.createElement('canvas'),crisp=document.createElement('canvas');canvas.width=crisp.width=W;canvas.height=crisp.height=H;
  const ctx=canvas.getContext('2d'),cc=crisp.getContext('2d'),tex=new T.CanvasTexture(canvas);tex.colorSpace=T.SRGBColorSpace;tex.anisotropy=4;
  const tile=document.createElement('canvas');tile.width=tile.height=18;const tc=tile.getContext('2d');tc.strokeStyle='rgba(0,0,0,.24)';tc.lineWidth=2.0;
  tc.beginPath();tc.moveTo(0,9);tc.lineTo(9,0);tc.lineTo(18,9);tc.lineTo(9,18);tc.closePath();tc.stroke();const lattice=cc.createPattern(tile,'repeat');
  const paintFrame=(c,v)=>{c.setTransform(3,0,0,3,12,12);c.fillStyle=c.strokeStyle='#ffffff';c.lineWidth=2;const lit=(a,draw)=>{if(!(a>0))return;c.globalAlpha=Math.max(.8,a/255);draw();};   // на колонке яркость часов бывает 13/255 — на белом корпусе это почти не видно
    const bold=d=>{const p=new Path2D(d);c.stroke(p);c.fill(p);};
    G.DIGIT_X.forEach((x,p)=>{for(const[seg,d]of G.segShapes(x))lit(v[p*8+G.SEGS.indexOf(seg)],()=>bold(d));});
    [[G.X_DH,30],[G.X_DL,66]].forEach(([i,y])=>lit(v[i],()=>{c.beginPath();c.arc(146,y,6,0,7);c.stroke();c.fill();}));
    lit(v[G.X_DM],()=>{c.fillRect(137,45,18,6);c.strokeRect(137,45,18,6);});lit(v[G.X_DEG],()=>{c.lineWidth=5;c.beginPath();c.arc(318,10,7,0,7);c.stroke();});c.globalAlpha=1;c.setTransform(1,0,0,1,0,0);};
  let lastFrame='';
  display=values=>{const v=Array.from({length:36},(_,i)=>Number(values?.[i])||0),key=v.join(',');if(key===lastFrame)return;lastFrame=key;
    cc.clearRect(0,0,W,H);paintFrame(cc,v);cc.globalCompositeOperation='destination-out';cc.fillStyle=lattice;cc.fillRect(0,0,W,H);cc.globalCompositeOperation='source-over';
    ctx.clearRect(0,0,W,H);ctx.shadowColor='rgba(255,250,240,.95)';ctx.shadowBlur=28;ctx.drawImage(crisp,0,0);ctx.shadowColor='rgba(30,24,20,.72)';ctx.shadowBlur=11;ctx.drawImage(crisp,0,0);ctx.shadowBlur=0;ctx.shadowColor='transparent';ctx.drawImage(crisp,0,0);tex.needsUpdate=true;};
  const unit=.215,arc=W/3*unit/(R+.15);
  mesh(new T.CylinderGeometry(R+.15,R+.15,H/3*unit,64,1,true,-arc/2,arc),new T.MeshBasicMaterial({map:tex,transparent:true,depthWrite:false,toneMapped:false}),0,0);
  display(new Array(36).fill(0));
  // Задняя планка во всю высоту: пластик в цвет корпуса, по оси сверху вниз — круглая кнопка выключения
  // микрофонов, гнездо AUX 3,5 мм и ниже него круглый фирменный разъём питания (адаптер 15 В, не USB).
  const stripW=25,stripArc=stripW/R,stripTop=TOP-2.2,stripBot=BOT+2.6,stripH=stripTop-stripBot,stripY=(stripTop+stripBot)/2;
  mesh(new T.CylinderGeometry(R+.12,R+.12,stripH,32,1,true,Math.PI-stripArc/2,stripArc),plastic,0,stripY);
  const stripCanvas=document.createElement('canvas');stripCanvas.width=256;stripCanvas.height=512;
  {const c=stripCanvas.getContext('2d'),k=256/stripW,mm=(x,y)=>[x*k,y*k];c.lineCap='round';c.lineJoin='round';
    c.strokeStyle='rgba(0,0,0,.28)';c.lineWidth=.3*k;c.beginPath();c.arc(...mm(12.5,7.5),4.4*k,0,Math.PI*2);c.stroke();   // кнопка
    c.save();c.translate(...mm(12.5,7.5));c.strokeStyle='rgba(0,0,0,.55)';c.lineWidth=.42*k;c.beginPath();c.roundRect(-.75*k,-2.4*k,1.5*k,3*k,.75*k);
    c.moveTo(-1.5*k,-.4*k);c.quadraticCurveTo(-1.5*k,1.6*k,0,1.6*k);c.quadraticCurveTo(1.5*k,1.6*k,1.5*k,-.4*k);c.moveTo(0,1.6*k);c.lineTo(0,2.5*k);c.moveTo(-1*k,2.5*k);c.lineTo(1*k,2.5*k);
    c.moveTo(-1.9*k,1.5*k);c.lineTo(1.9*k,-2.1*k);c.stroke();c.restore();   // перечёркнутый микрофон
    for(const[y,r,rim]of[[27.5,1.75,.4],[37.5,2.7,.6]]){c.fillStyle='#8f8f95';c.beginPath();c.arc(...mm(12.5,y),(r+rim)*k,0,Math.PI*2);c.fill();c.fillStyle='#0b0b0e';c.beginPath();c.arc(...mm(12.5,y),r*k,0,Math.PI*2);c.fill();}
    c.fillStyle='#55555c';c.beginPath();c.arc(...mm(12.5,37.5),.9*k,0,Math.PI*2);c.fill();}   // AUX сверху, под ним питание (контакт в центре)
  const stripTex=new T.CanvasTexture(stripCanvas);stripTex.colorSpace=T.SRGBColorSpace;
  mesh(new T.CylinderGeometry(R+.25,R+.25,stripH,32,1,true,Math.PI-stripArc/2,stripArc),new T.MeshBasicMaterial({map:stripTex,transparent:true,depthWrite:false,polygonOffset:true,polygonOffsetFactor:-1}),0,stripY);
  // Ярлык — мягкая силиконовая лента (без надписи) 9 мм толщиной 1,1 мм: свисает по боку на 12 мм, перегибается через
  // скруглённый край и ложится на тканевый поясок до диска. Справа от дисплея (≈50°). Сетка 6 × 96 сужается
  // по контуру со скруглёнными углами и изгибается по пути ленты (вершины переносятся вдоль неё).
  const side=.87,tagW=9,hang=11.8,thick=1.1,bend=2.25+thick/2,lay=1.9,len=hang+bend*Math.PI/2+lay,Cx=R-2.2,Cy=TOP-2.2;
  const silicone=new T.MeshStandardMaterial({color:plastic.color,roughness:.82});trimMaterials.push(silicone);
  const tagGeo=new T.BoxGeometry(tagW,len,thick,6,96,1);
  {const pos=tagGeo.attributes.position,w=tagW/2,r=2.6,rt=.8,s1=hang,s2=hang+bend*Math.PI/2;
    const edge=sPos=>sPos<r?w-r+Math.sqrt(Math.max(0,r*r-(r-sPos)**2)):sPos>len-rt?w-rt+Math.sqrt(Math.max(0,rt*rt-(sPos-len+rt)**2)):w;   // контур: скруглённые углы
    for(let i=0;i<pos.count;i++){const sPos=pos.getY(i)+len/2,x=pos.getX(i)*edge(sPos)/w,nOff=pos.getZ(i);let rad,y;
      if(sPos<=s1){rad=R+.05+thick/2+nOff;y=TOP-2.2-hang+sPos;}
      else if(sPos<=s2){const phi=(sPos-s1)/bend;rad=Cx+(bend+nOff)*Math.cos(phi);y=Cy+(bend+nOff)*Math.sin(phi);}
      else{rad=Cx-(sPos-s2);y=Cy+bend+nOff;}
      pos.setXYZ(i,x,y,rad);}
    tagGeo.computeVertexNormals();}
  const tag=new T.Group();tag.rotation.y=side;group.add(tag);mesh(tagGeo,silicone,0,0,0,tag);
  topY=TOP;baseY=BOT;
}else if(mini){
  const points=[[0,-35],[43,-35],[47,-34],[49.4,-31],[50.4,-26],[51,-18],[51,20],[50.8,28],[49.6,31],[0,31]].map(p=>new T.Vector2(...p));
  const shell=new T.LatheGeometry(points,192),shellUV=shell.attributes.uv,shellPosition=shell.attributes.position;
  for(let i=0;i<shellUV.count;i++)shellUV.setY(i,(shellPosition.getY(i)+35)/66);
  mesh(shell,fabric);
  mesh(new T.CylinderGeometry(49.5,47.6,3,160),plastic,0,-33.6);mesh(new T.CylinderGeometry(43,43,1.2,128),rubber,0,-35.6);
  if(profile.id!=='mini3')for(let i=0;i<16;i++)mesh(new T.CylinderGeometry(50.7,50.7,2.3,16,1,true,i*Math.PI/8,Math.PI/8+.005),ledMaterial(),0,30.1);
  mesh(new T.CylinderGeometry(50.6,50.8,2,192),plastic,0,32.1);mesh(new T.CylinderGeometry(50.2,50.2,.6,192),glass,0,33.4);topY=33.8;baseY=-36.2;port(-25,-50.1);
  if(profile.id==='mini3'){group.scale.y=60.5/71;const indicator=mesh(new T.RingGeometry(3.6,5.2,64),ledMaterial(),0,33.85);indicator.rotation.x=-Math.PI/2;}
}else{
  const body=roundedBox(123,190,99,21,3),positions=body.attributes.position,uv=body.attributes.uv;
  // ExtrudeGeometry uses millimetres as UVs; normalize to avoid subpixel aliasing.
  for(let i=0;i<uv.count;i++)uv.setXY(i,Math.atan2(positions.getX(i),positions.getZ(i))/(2*Math.PI)+.5,(positions.getY(i)+95)/190);
  mesh(body,fabric,0,-1.5);mesh(roundedBox(118,6,94,19,1),plastic,0,-96.5);mesh(roundedBox(107,1.4,84,16,.5),rubber,0,-99.3);
  mesh(roundedBox(122,2.6,98,21,.7),ledMaterial(),0,94.5);mesh(roundedBox(121,3,97,21,.9),glass,0,97.1);
  const lightMap=canvasTexture(256,(c,n)=>{const g=c.createLinearGradient(0,0,n,n);g.addColorStop(0,'#68d3ff');g.addColorStop(.45,'#9e7bff');g.addColorStop(1,'#e89ad7');c.fillStyle=g;c.fillRect(0,0,n,n);});
  const topMaterial=new T.MeshStandardMaterial({color:'#a5a2b7',map:lightMap,emissive:'#706187',emissiveMap:lightMap,emissiveIntensity:.18,roughness:.6,metalness:.02,transparent:true,opacity:.8});ledMaterials.push(topMaterial);
  const top=mesh(new T.ShapeGeometry(roundedShape(115,91,18),32),topMaterial,0,98.65);top.rotation.x=-Math.PI/2;topY=99;baseY=-100;port(-85,-49.6);
}
const controlsTex=canvasTexture(512,(c,n)=>{c.strokeStyle='#d6d6dc';c.fillStyle='#d6d6dc';c.lineWidth=2;c.lineCap='round';for(const x of[100,412]){c.beginPath();c.moveTo(x-10,256);c.lineTo(x+10,256);if(x>256){c.moveTo(x,246);c.lineTo(x,266);}c.stroke();}c.beginPath();c.ellipse(256,256,25,19,0,0,Math.PI*2);c.stroke();c.beginPath();c.moveTo(242,262);c.quadraticCurveTo(256,225,270,262);c.closePath();c.fill();c.fillStyle='#090a0c';for(const[x,y]of[[162,128],[350,128],[162,384],[350,384]]){c.beginPath();c.arc(x,y,2,0,Math.PI*2);c.fill();}});
// Крышка Лайт по фото сверху: по краю кольцо наклонных прорезей, по диагоналям микрофоны,
// слева «−», справа «+», сверху зачёркнутый микрофон, снизу пауза (логотип Алисы — отдельной деталью).
const liteTex=lite&&canvasTexture(1024,(c,n)=>{const R=n/2;c.translate(R,R);
  const ink='rgba(20,10,40,.34)';c.strokeStyle=ink;c.fillStyle=ink;c.lineCap='round';c.lineJoin='round';
  c.translate(-R,-R);liteSlots(c,R);c.translate(R,R);
  for(const[x,y]of[[-1,-1],[1,-1],[-1,1],[1,1]]){c.beginPath();c.arc(x*R*.49,y*R*.49,6,0,Math.PI*2);c.fillStyle='rgba(10,5,20,.8)';c.fill();}
  c.fillStyle=ink;c.lineWidth=7;const k=R*.48;
  c.beginPath();c.moveTo(-k-38,0);c.lineTo(-k+38,0);c.moveTo(k-38,0);c.lineTo(k+38,0);c.moveTo(k,-38);c.lineTo(k,38);c.stroke();   // − и +
  c.beginPath();c.moveTo(-14,k-34);c.lineTo(-14,k+34);c.moveTo(14,k-34);c.lineTo(14,k+34);c.stroke();                                // пауза
  c.save();c.translate(0,-k);c.scale(1.5,1.5);c.lineWidth=4.6;c.beginPath();c.roundRect(-11,-34,22,38,11);c.moveTo(-19,-8);c.quadraticCurveTo(-19,16,0,16);c.quadraticCurveTo(19,16,19,-8);
  c.moveTo(0,16);c.lineTo(0,28);c.moveTo(-13,28);c.lineTo(13,28);c.moveTo(-22,12);c.lineTo(22,-30);c.stroke();c.restore();            // микрофон выключен
});
if(!mini2&&!concept){
const topMarks=mesh(lite?new T.CircleGeometry(43.5,128):new T.PlaneGeometry(mini?98:117,mini?98:93),lite?new T.MeshBasicMaterial({map:liteTex,transparent:true,depthWrite:false,polygonOffset:true,polygonOffsetFactor:-1,polygonOffsetUnits:-1}):new T.MeshBasicMaterial({map:controlsTex,transparent:true,depthWrite:false,opacity:.72}),0,topY+.05);topMarks.rotation.x=-Math.PI/2;topMarks.renderOrder=1;   // прозрачные слои крышки — всегда в одном порядке, иначе мерцают при вращении
topMarks.castShadow=false;
}
if(mini&&!lite&&!mini2){
  const white=new T.MeshStandardMaterial({color:'#fff9e8',emissive:'#fff2d6',emissiveIntensity:2,roughness:.8,toneMapped:false});
  function horizontal(){const s=new T.Shape();s.moveTo(-3.5,0);s.lineTo(-2.5,1);s.lineTo(2.5,1);s.lineTo(3.5,0);s.lineTo(2.5,-1);s.lineTo(-2.5,-1);s.closePath();return new T.ShapeGeometry(s);}
  const positions=[[0,10,0],[4.2,5,1],[4.2,-5,1],[0,-10,0],[-4.2,-5,1],[-4.2,5,1],[0,0,0]];
  for(let digit=0;digit<4;digit++)for(let j=0;j<8;j++){const[dx,dy,vertical]=positions[j]||[0,3,0],x=[-19,-7,7,19][digit]+dx,theta=x/51;const seg=mesh(j===7?new T.CircleGeometry(.7,12):horizontal(),white,51.3*Math.sin(theta),dy-3,51.3*Math.cos(theta));seg.rotation.y=theta;if(vertical){seg.geometry.scale(1.25,.8,1);seg.rotateZ(Math.PI/2);}seg.castShadow=false;seg.visible=false;clockSegments.push(seg);}
  for(const[x,y]of[[29,10],[0,1],[0,-7],[0,-3]]){const theta=x/51,geometry=clockSegments.length===32?new T.RingGeometry(.9,1.4,20):clockSegments.length===35?new T.PlaneGeometry(3,1):new T.CircleGeometry(.9,16),s=mesh(geometry,white,51.3*Math.sin(theta),y,51.3*Math.cos(theta));s.rotation.y=theta;s.visible=false;s.castShadow=false;clockSegments.push(s);}
}
window.modelShow=values=>{if(display)display(values);else clockSegments.forEach((s,i)=>{s.visible=Number(values?.[i])>0;});dirty=true;};
window.modelRing=leds=>{if(!leds?.length)return;
  if(details.ring){details.ring(leds);dirty=true;return;}
  const lit=mini2&&leds.some(rgb=>rgb.some(v=>v>0));
  // Approximate the central diffuser's mixed hue; all six native zones contribute.
  if(mini2)leds=[[0,1,2].map(c=>leds.reduce((sum,rgb)=>sum+Math.max(0,Math.min(255,Number(rgb?.[c])||0)),0)/leds.length)];
  ledMaterials.forEach((m,i)=>{const rgb=leds[Math.floor(i*leds.length/ledMaterials.length)]||[0,0,0],color=new T.Color().setRGB(...rgb.map(v=>Math.max(0,Math.min(255,v))/255),T.SRGBColorSpace);
  if(mini2&&!lit){m.color.set('#ffffff');m.emissive?.set('#000000');m.emissiveIntensity=0;return;}
  m.color.copy(color);
  if(m.userData.logo){if(Math.max(...rgb)<12)m.color.set('#ffffff');else m.color.copy(color).lerp(new T.Color('#ffffff'),mini2 ? .55 : .15);return;}   // логотип Лайт: без подсветки белый; у Мини 2 остаётся светлым
  m.emissive.copy(color);m.emissiveIntensity=.55;});dirty=true;};
function paint(key){key=palette[key]?key:fallback;const[,body,trim]=palette[key];fabric.color.set(body);if(trim)plastic.color.set(trim);else plastic.color.copy(fabric.color).multiplyScalar(.65);for(const m of trimMaterials)m.color.copy(plastic.color);dirty=true;}
let preference=null;
window.modelColor=key=>{if(!preference)paint(key);};
paint(window.__deviceColor||fallback);
// Gallery-only bodies differ widely in size: frame each one by its bounding sphere.
if(concept?.lying)group.rotation.z=Math.PI/2;
const bounds=concept&&new T.Box3().setFromObject(group).getBoundingSphere(new T.Sphere());
if(bounds){controls.minDistance=bounds.radius*1.6;controls.maxDistance=bounds.radius*6;controls.maxPolarAngle=Math.PI;}
function home(){if(bounds){camera.position.copy(bounds.center).add(new T.Vector3(.38,.42,1).setLength(bounds.radius*(midi?3.9:4.3)));controls.target.copy(bounds.center);}
  else{if(lite)camera.position.set(60,115,215);else camera.position.set(mini?55:240,mini?83:155,mini?258:420);controls.target.set(0,mini?-4:0,0);}controls.update();dirty=true;}home();
modelSettings(profile.id,palette,{color:key=>{preference=key;paint(key||window.__deviceColor||fallback);},home,spin:value=>{controls.autoRotate=value;dirty=true;}});
controls.addEventListener('change',()=>{dirty=true;});controls.addEventListener('start',()=>{view.classList.add('studio-used');});
let visible=true;new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;dirty=true;}).observe(view);
new ResizeObserver(()=>{const{width,height}=view.getBoundingClientRect();if(width&&height){renderer.setSize(width,height);camera.aspect=width/height;camera.updateProjectionMatrix();dirty=true;}}).observe(view);
// без ограничения частоты, как у Лайт 2: с ограничением в 30 к/с вращение дёргалось; в покое кадры не рисуются
function frame(){if(failed)return;requestAnimationFrame(frame);if(document.hidden||!visible)return;controls.update();if(dirty||controls.autoRotate){
  try{renderer.render(scene,camera);}catch(e){failed=true;note.hidden=false;note.textContent='3D-модель не отрисовывается — обновите страницу.';console.error('3D-модель:',e);return;}
  dirty=false;view.dataset.renderer='webgl';view.dataset.model=profile.id;}}requestAnimationFrame(frame);

}
