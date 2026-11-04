// MIT: approximate gallery-only bodies after Yandex's official specifications and product photos (see SOURCES.md).
// Sizes are in millimetres and match the published width, depth and height. Hardware support is not implied.
const pal=(...items)=>Object.fromEntries(items.map(([key,label,color])=>[key,[label,color]]));
const BLACK=pal(['black','Чёрный','#2f3036']);
// Colour names follow the official "Расцветки" lists on alice.yandex.ru/support.
const STATION=pal(['black','Чёрный','#2f3036'],['grey','Серый','#a9abb1'],['purple','Фиолетовый','#5b43b8']);
const MAX=pal(['graphite','Графит','#34353b'],['beige','Бежевый','#cbbfad'],['green','Зелёный','#2f5a4b'],['turquoise','Бирюза','#3f8f8c'],['blue','Синий','#2f4a7a'],['bordo','Бордовый','#6b2834']);
const MIDI=pal(['black','Чёрный','#41434b'],['emerald','Изумруд','#46b5b5'],['grey','Серый','#a2a4aa'],['orange','Оранжевый','#cf913c'],['crimson','Малиновый','#ad365c']);
const MINI2=pal(['white','Белый','#dcdcdc'],['black','Чёрный','#34353b'],['red','Красный','#b0303c'],['blue','Синий','#2f56a8']);

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

function buildMax(kit) {
  const { T, group, mesh, roundedBox, fabric, plastic, rubber, glass, ledMaterial } = kit;
  const canvasTexture = kit.canvasTexture || ((size, draw) => {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = size;
    draw(canvas.getContext('2d'), size);
    const tex = new T.CanvasTexture(canvas);
    tex.colorSpace = T.SRGBColorSpace;
    return tex;
  });

  // Official dimensions of Yandex Station Max: 141 x 141 x 231 mm
  const w = 141, d = 141, H = 231, r = 12;
  const capH = 10, baseH = 7;
  const h = H - capH - baseH; // 214 mm
  const y0 = (baseH - capH) / 2; // -1.5 mm
  const t = y0 + h / 2; // top of fabric body: 105.5 mm
  const b = y0 - h / 2; // bottom of fabric body: -108.5 mm

  // =========================================================================
  // 1. ДНО КОЛОНКИ (Bottom Base with regulatory plate & cable cutout)
  // =========================================================================
  // Main rubber base foot
  mesh(roundedBox(w - 2, baseH, d - 2, r - 1, 1), rubber, 0, b - baseH / 2);

  // Rear cable relief notch (at bottom back edge)
  const notchW = 44, notchH = baseH + 1, notchD = 14;
  mesh(new T.BoxGeometry(notchW, notchH, notchD), plastic, 0, b - baseH / 2 + 0.5, -d / 2 + notchD / 2 - 1);

  // Bottom plate graphic (Photo 1: Яндекс, Dolby Audio, EAC, Model info)
  const bottomTexture = canvasTexture(512, (ctx, n) => {
    ctx.fillStyle = '#18191c';
    ctx.fillRect(0, 0, n, n);

    // Silicone outer perimeter foot border
    ctx.strokeStyle = '#27282e';
    ctx.lineWidth = 14;
    ctx.strokeRect(36, 36, n - 72, n - 72);

    // Inner recessed plateau
    ctx.fillStyle = '#1c1d22';
    ctx.fillRect(50, 50, n - 100, n - 100);

    // "Яндекс" center logo
    ctx.fillStyle = '#c5c7ce';
    ctx.font = '600 36px "Yandex Sans Text", system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('Яндекс', n / 2, n * 0.32);

    // Model description
    ctx.fillStyle = '#656872';
    ctx.font = '400 13px system-ui, sans-serif';
    ctx.fillText('Домашняя мультимедийная платформа', n / 2, n * 0.44);
    ctx.fillText('«Яндекс.Станция». Модель YNDX-00053.', n / 2, n * 0.49);
    ctx.fillText('20 В ⎓ 3.25 А · Сделано в Китае', n / 2, n * 0.54);

    // EAC and Dolby Audio logos
    ctx.fillStyle = '#9497a1';
    ctx.font = '700 24px system-ui, sans-serif';
    ctx.fillText('EAC', n * 0.34, n * 0.70);

    ctx.font = '600 20px system-ui, sans-serif';
    ctx.fillText('D◖Dolby Audio', n * 0.65, n * 0.70);

    // QR-code icon placeholder
    ctx.strokeStyle = '#4e515a';
    ctx.lineWidth = 2;
    ctx.strokeRect(n * 0.16, n * 0.64, 44, 44);
    ctx.fillStyle = '#60646e';
    ctx.fillRect(n * 0.16 + 8, n * 0.64 + 8, 12, 12);
    ctx.fillRect(n * 0.16 + 24, n * 0.64 + 24, 12, 12);
  });

  const bottomPlate = mesh(
    new T.PlaneGeometry(w - 14, d - 14),
    new T.MeshStandardMaterial({
      map: bottomTexture,
      roughness: 0.85,
      metalness: 0.05
    }),
    0, b - baseH - 0.1
  );
  bottomPlate.rotation.x = Math.PI / 2; // Facing down

  // =========================================================================
  // 4. ЗАД КОЛОНКИ (Back: Extruded Aluminum Radiator / Heatsink & Port Panel)
  // =========================================================================
  const spineW = 42;
  const spineH = h - 1;
  const spineD = 5.5;
  const spineZ = -d / 2 + spineD / 2 - 0.4;

  // Dark anodized metal for heatsink fins
  const metalRadiator = new T.MeshStandardMaterial({
    color: '#28292e',
    metalness: 0.78,
    roughness: 0.32
  });

  // Radiator base plate
  mesh(new T.BoxGeometry(spineW, spineH, spineD), metalRadiator, 0, y0, spineZ);

  // Vertical cooling fins (Photo 4: 11 prominent vertical fins)
  const finCount = 11;
  const finSpacing = (spineW - 6) / (finCount - 1); // ~3.6 mm spacing
  const finH = spineH * 0.75;
  const finY = y0 + (spineH - finH) / 2 - 2;

  for (let i = 0; i < finCount; i++) {
    const finX = -spineW / 2 + 3 + i * finSpacing;
    mesh(new T.BoxGeometry(1.6, finH, 4.2), metalRadiator, finX, finY, spineZ - spineD / 2 - 2.1);
  }

  // Rear Connector / Port Panel (Bottom section below heatsink)
  const portPanelH = spineH * 0.22;
  const portPanelY = y0 - (spineH - portPanelH) / 2 + 1;
  const portMat = new T.MeshStandardMaterial({ color: '#16171a', roughness: 0.7, metalness: 0.2 });
  mesh(new T.BoxGeometry(spineW - 2, portPanelH, spineD + 1), portMat, 0, portPanelY, spineZ - 0.5);

  // Ports: AUX (3.5mm), HDMI, Power barrel
  const jackMat = new T.MeshStandardMaterial({ color: '#52555f', metalness: 0.8, roughness: 0.4 });
  const holeMat = new T.MeshBasicMaterial({ color: '#050507' });

  // 3.5mm AUX Out (circular jack)
  mesh(new T.CylinderGeometry(2.4, 2.4, 1.5, 24), jackMat, -11, portPanelY - 6, spineZ - spineD / 2 - 1.2).rotation.x = Math.PI / 2;
  mesh(new T.CylinderGeometry(1.5, 1.5, 2.0, 24), holeMat, -11, portPanelY - 6, spineZ - spineD / 2 - 1.3).rotation.x = Math.PI / 2;

  // HDMI Out port (trapezoidal / rectangular cutout)
  mesh(new T.BoxGeometry(9.0, 4.2, 2.0), jackMat, 0, portPanelY + 3, spineZ - spineD / 2 - 1.2);
  mesh(new T.BoxGeometry(7.2, 2.8, 2.5), holeMat, 0, portPanelY + 3, spineZ - spineD / 2 - 1.3);

  // DC Power IN port (circular barrel jack)
  mesh(new T.CylinderGeometry(3.6, 3.6, 1.5, 24), jackMat, 12, portPanelY - 6, spineZ - spineD / 2 - 1.2).rotation.x = Math.PI / 2;
  mesh(new T.CylinderGeometry(2.2, 2.2, 2.0, 24), holeMat, 12, portPanelY - 6, spineZ - spineD / 2 - 1.3).rotation.x = Math.PI / 2;
  mesh(new T.CylinderGeometry(0.7, 0.7, 2.2, 16), jackMat, 12, portPanelY - 6, spineZ - spineD / 2 - 1.4).rotation.x = Math.PI / 2;

  // =========================================================================
  // 3. ПЕРЕД И КОРПУС (Acoustic Fabric Wrap + Front LED Matrix Display)
  // =========================================================================
  // Main speaker body wrapped in acoustic fabric (front & side faces)
  mesh(roundedBox(w, h, d, r, 3), fabric, 0, y0);

  // Dynamic 25x16 Dot Matrix Display (shining through acoustic fabric weave)
  const cols = 25, rows = 16;
  const canvasW = 512, canvasH = 328;
  const canvas = document.createElement('canvas');
  canvas.width = canvasW;
  canvas.height = canvasH;
  const ctx = canvas.getContext('2d');
  const tex = new T.CanvasTexture(canvas);
  tex.colorSpace = T.SRGBColorSpace;
  tex.anisotropy = 4;

  // Screen placed on front face (Photo 3: centered vertically slightly above midpoint)
  const matrixW = 86, matrixH = 55;
  const matrixY = y0 + h * 0.08;
  const matrixZ = d / 2 + 0.6;

  const displayMesh = mesh(
    new T.PlaneGeometry(matrixW, matrixH),
    new T.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false, toneMapped: false }),
    0, matrixY, matrixZ
  );
  displayMesh.renderOrder = 3;

  let lastMatrixStr = '';
  function updateDisplay(values) {
    let pixels = null;
    if (Array.isArray(values)) {
      if (values.length >= 400) {
        pixels = values.slice(0, 400);
      } else if (values.length === 36) {
        pixels = new Array(400).fill(0);
        const digitX = [1, 7, 14, 20];
        for (let di = 0; di < 4; di++) {
          const x0 = digitX[di];
          const A = values[di * 8 + 0]; if (A > 0) for (let x = x0 + 1; x < x0 + 4; x++) pixels[3 * cols + x] = A;
          const B = values[di * 8 + 1]; if (B > 0) for (let y = 4; y < 8; y++) pixels[y * cols + x0 + 4] = B;
          const C = values[di * 8 + 2]; if (C > 0) for (let y = 9; y < 13; y++) pixels[y * cols + x0 + 4] = C;
          const D = values[di * 8 + 3]; if (D > 0) for (let x = x0 + 1; x < x0 + 4; x++) pixels[13 * cols + x] = D;
          const E = values[di * 8 + 4]; if (E > 0) for (let y = 9; y < 13; y++) pixels[y * cols + x0 + 4] = E;
          const F = values[di * 8 + 5]; if (F > 0) for (let y = 4; y < 8; y++) pixels[y * cols + x0 + 4] = F;
          const G = values[di * 8 + 6]; if (G > 0) for (let x = x0 + 1; x < x0 + 4; x++) pixels[8 * cols + x] = G;
          const H = values[di * 8 + 7]; if (H > 0 && x0 + 5 < cols) pixels[13 * cols + x0 + 5] = H;
        }
        if (values[32] > 0) { pixels[5 * cols + 12] = values[32]; pixels[6 * cols + 12] = values[32]; }
        if (values[33] > 0) { pixels[10 * cols + 12] = values[33]; pixels[11 * cols + 12] = values[33]; }
        if (values[34] > 0) { for (let x = 11; x < 14; x++) pixels[8 * cols + x] = values[34]; }
        if (values[35] > 0) {
          pixels[3 * cols + 22] = pixels[3 * cols + 23] = values[35];
          pixels[4 * cols + 21] = pixels[4 * cols + 24] = values[35];
          pixels[5 * cols + 22] = pixels[5 * cols + 23] = values[35];
        }
      }
    } else if (values && values.pixels && values.pixels.length >= 400) {
      pixels = values.pixels;
    }

    if (!pixels) return;
    const str = pixels.join(',');
    if (str === lastMatrixStr) return;
    lastMatrixStr = str;

    ctx.clearRect(0, 0, canvasW, canvasH);
    const p = canvasW / cols;
    const ry = (canvasH - p * rows) / 2;
    const iceBlue = '#ebf5ff';

    for (let row = 0; row < rows; row++) {
      for (let col = 0; col < cols; col++) {
        const val = pixels ? (Number(pixels[row * cols + col]) || 0) : 0;
        const alpha = Math.max(0, Math.min(255, val)) / 255;
        const on = alpha > 0.05;

        // Realistic dot through fabric with soft radial glow
        ctx.fillStyle = on ? `rgba(235, 245, 255, ${Math.max(0.72, alpha)})` : 'rgba(255, 255, 255, 0.025)';
        ctx.shadowColor = on ? iceBlue : 'transparent';
        ctx.shadowBlur = on ? p * 0.75 : 0;
        ctx.beginPath();
        ctx.arc((col + 0.5) * p, ry + (row + 0.5) * p, p * 0.33, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    tex.needsUpdate = true;
  }

  // =========================================================================
  // 2. КОЛЬЦО & 5. КОЛЬЦО СВЕРХУ (Top Plate, Well, 24-LED Ring & Volume Wheel)
  // =========================================================================
  // Top plastic cap plate
  mesh(roundedBox(w, capH, d, r, 2), plastic, 0, t + capH / 2);

  // Circular recessed well (depression in top plate around the knob)
  const wellRadius = 51.5;
  const wellDepth = 3.6;
  const wellGeo = new T.RingGeometry(40, wellRadius, 64);
  const wellMat = new T.MeshStandardMaterial({ color: '#1a1b20', roughness: 0.65, metalness: 0.1 });
  const wellMesh = mesh(wellGeo, wellMat, 0, t + capH - 0.2);
  wellMesh.rotation.x = -Math.PI / 2;

  // 24-LED RGB Ring (inside the well recess, under the floating wheel edge)
  const ringRadius = 45.0;
  const ringInnerRadius = 41.5;
  const step = (Math.PI * 2) / 24;
  const ringMaterials = [];

  for (let i = 0; i < 24; i++) {
    const mat = new T.MeshStandardMaterial({
      color: '#16171d',
      emissive: '#000000',
      emissiveIntensity: 0,
      roughness: 0.35,
      metalness: 0.05
    });
    ringMaterials.push(mat);

    // Vertical cylinder arc for light strip
    mesh(new T.CylinderGeometry(ringRadius, ringRadius, 3.2, 8, 1, true, i * step, step * 0.92), mat, 0, t + capH + 1.2);
    // Flat ring face for top illumination
    const topArc = mesh(new T.RingGeometry(ringInnerRadius, ringRadius, 8, 1, i * step, step * 0.92), mat, 0, t + capH + 2.8);
    topArc.rotation.x = -Math.PI / 2;
  }

  let lastRingStr = '';
  function updateRing(leds) {
    if (!leds || !leds.length) return;
    const str = JSON.stringify(leds);
    if (str === lastRingStr) return;
    lastRingStr = str;
    if (!leds || !leds.length) return;
    for (let i = 0; i < 24; i++) {
      const idx = leds.length === 24 ? i : Math.floor(i * leds.length / 24);
      const [r, g, b] = leds[idx] || [0, 0, 0];
      const rN = Math.max(0, Math.min(255, Number(r) || 0)) / 255;
      const gN = Math.max(0, Math.min(255, Number(g) || 0)) / 255;
      const bN = Math.max(0, Math.min(255, Number(b) || 0)) / 255;
      const lit = (rN > 0 || gN > 0 || bN > 0);
      const color = new T.Color(rN, gN, bN);
      ringMaterials[i].color.copy(lit ? color : new T.Color('#16171d'));
      ringMaterials[i].emissive.copy(color);
      ringMaterials[i].emissiveIntensity = lit ? 1.0 : 0;
    }
  }

  // Floating Volume Wheel / Knob (Photo 2 & Photo 5: disc with angled perimeter slits)
  const knobR = 40.5;
  const knobH = 7.5;
  const knobY = t + capH + knobH / 2 + 1.2;

  // Knob rim texture with angled diagonal ventilation slits (Photo 2 & 5: ~60 angled slots)
  const rimTexture = canvasTexture(1024, (ctx, n) => {
    ctx.fillStyle = '#222329';
    ctx.fillRect(0, 0, n, n);

    ctx.fillStyle = '#0f1014';
    const slotCount = 60;
    const slotW = n / slotCount;
    for (let i = 0; i < slotCount; i++) {
      ctx.save();
      ctx.translate(i * slotW + slotW / 2, n / 2);
      ctx.rotate(-0.55); // Diagonal angled slit tilt per photo
      ctx.fillRect(-slotW * 0.22, -n * 0.38, slotW * 0.44, n * 0.76);
      ctx.restore();
    }
  });
  rimTexture.wrapS = T.RepeatWrapping;
  rimTexture.wrapT = T.ClampToEdgeWrapping;

  const knobRimMat = new T.MeshStandardMaterial({
    map: rimTexture,
    color: '#2a2b32',
    roughness: 0.5,
    metalness: 0.15
  });

  mesh(new T.CylinderGeometry(knobR, knobR + 1.2, knobH, 64), knobRimMat, 0, knobY);

  // Top Face of Volume Knob (Photo 5: Alice Button, Mic Mute Button, Center Mic Pinhole)
  const knobTopTexture = canvasTexture(512, (ctx, n) => {
    ctx.fillStyle = '#24252b';
    ctx.fillRect(0, 0, n, n);

    // Subtle concentric grip grooves
    ctx.strokeStyle = '#1d1e23';
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.arc(n / 2, n / 2, n * 0.45, 0, Math.PI * 2);
    ctx.stroke();

    // 1. Left Button: Alice activation (Circle with solid rounded triangle)
    const btnRadius = 42;
    const leftBtnX = n * 0.34;
    const btnY = n / 2;

    ctx.strokeStyle = 'rgba(255, 255, 255, 0.45)';
    ctx.lineWidth = 3.5;
    ctx.beginPath();
    ctx.arc(leftBtnX, btnY, btnRadius, 0, Math.PI * 2);
    ctx.stroke();

    // Alice triangle inside left button
    ctx.fillStyle = 'rgba(255, 255, 255, 0.85)';
    ctx.beginPath();
    const triR = 18;
    ctx.moveTo(leftBtnX, btnY - triR);
    ctx.lineTo(leftBtnX + triR * 0.88, btnY + triR * 0.65);
    ctx.lineTo(leftBtnX - triR * 0.88, btnY + triR * 0.65);
    ctx.closePath();
    ctx.fill();

    // 2. Right Button: Microphone Mute (Circle with mic icon and diagonal slash)
    const rightBtnX = n * 0.66;
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.45)';
    ctx.lineWidth = 3.5;
    ctx.beginPath();
    ctx.arc(rightBtnX, btnY, btnRadius, 0, Math.PI * 2);
    ctx.stroke();

    // Microphone symbol with slash
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.85)';
    ctx.lineWidth = 3.2;
    ctx.beginPath();
    // Mic capsule
    ctx.arc(rightBtnX, btnY - 4, 8, Math.PI, 0, false);
    ctx.lineTo(rightBtnX + 8, btnY + 2);
    ctx.arc(rightBtnX, btnY + 2, 8, 0, Math.PI, false);
    ctx.lineTo(rightBtnX - 8, btnY - 4);
    // Mic cradle
    ctx.moveTo(rightBtnX - 12, btnY - 2);
    ctx.arc(rightBtnX, btnY + 2, 12, Math.PI, 0, true);
    ctx.moveTo(rightBtnX, btnY + 14);
    ctx.lineTo(rightBtnX, btnY + 20);
    // Strike-through slash
    ctx.moveTo(rightBtnX - 14, btnY + 15);
    ctx.lineTo(rightBtnX + 14, btnY - 14);
    ctx.stroke();

    // 3. Center microphone pinhole
    ctx.fillStyle = '#0c0d10';
    ctx.beginPath();
    ctx.arc(n / 2, btnY, 4.5, 0, Math.PI * 2);
    ctx.fill();

    // Secondary beamforming mic apertures
    ctx.fillStyle = '#14151a';
    for (let ang = 0; ang < Math.PI * 2; ang += Math.PI / 2) {
      const hx = n / 2 + Math.cos(ang) * (n * 0.41);
      const hy = n / 2 + Math.sin(ang) * (n * 0.41);
      ctx.beginPath();
      ctx.arc(hx, hy, 2.5, 0, Math.PI * 2);
      ctx.fill();
    }
  });

  const knobCapMesh = mesh(
    new T.CircleGeometry(knobR, 64),
    new T.MeshStandardMaterial({
      map: knobTopTexture,
      roughness: 0.45,
      metalness: 0.1
    }),
    0, knobY + knobH / 2 + 0.05
  );
  knobCapMesh.rotation.x = -Math.PI / 2;

  // Initialize demo display with clean time "12:50" (as seen on Photo 3)
  const initialV = [
    ...[0,1,1,0,0,0,0,0].map(x=>x*255), // 1
    ...[1,1,0,1,1,0,1,0].map(x=>x*255), // 2
    ...[1,0,1,1,0,1,1,0].map(x=>x*255), // 5
    ...[1,1,1,1,1,1,0,0].map(x=>x*255), // 0
    255, 255, 0, 0
  ];
  updateDisplay(initialV);

  return {
    topY: H / 2,
    baseY: -H / 2,
    t,
    display: updateDisplay,
    ring: updateRing
  };
}


export const CONCEPTS={
  // 141 × 141 × 231 mm; pixel LED display 25 × 16 behind the fabric, volume ring on top.
  max:{name:'Яндекс Станция Макс',colors:MAX,build:buildMax},
  // 132 × 132 × 201 mm; LED display 21 × 12, light under the top glass, ambient light on the back, raised on a foot.
  station3:{name:'Яндекс Станция 3',colors:STATION,build:kit=>{
    const s=square(kit,{w:132,H:201,r:30,glow:34,display:kit=>({w:60,h:60,map:dotClock(kit,21,12)}),foot:10}),{mesh,roundedBox,ledMaterial}=kit;
    mesh(roundedBox(84,5,2,2,.8),ledMaterial(),0,-201/2+28,-132/2-.6);return s;}},
  // 96 × 96 × 110 mm; 45-segment display, contour light beneath a matte plastic top.
  // Detailed knitted shell and plastic controls; live visual adapter, no baked demo clock.
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
