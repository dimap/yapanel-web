const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(require('path').join(__dirname,'../static/index.html'),'utf8');
const nodes={};
const c={patterns:{ring:[],clock:[]},animCustom:{ring:['saved.led'],clock:[]},ra:{name:null},boot:{name:''},
  $:id=>nodes[id]||=( {value:'',dataset:{}} ),esc:s=>s,isMy:n=>String(n||'').startsWith('my/'),
  animLabel:(k,n)=>n,raSetText:t=>{c.text=t;},api:async()=>{throw Error('Catalog unavailable');}};
vm.createContext(c);
const extract=(start,end)=>html.slice(html.indexOf(start),html.indexOf(end,html.indexOf(start)));
vm.runInContext(extract('function fillAnimSelect(kind)', '// «＋ Добавить новую'),c);
vm.runInContext(extract('async function loadRingAnim()', "$('#anim-ring').onchange"),c);
(async()=>{
  c.ra.name='saved.led';
  vm.runInContext("fillAnimSelect('ring')",c);
  assert(nodes['#anim-ring'].innerHTML.includes('saved.led'));
  await vm.runInContext('loadRingAnim()',c);
  assert.equal(nodes['#ra-reset'].hidden,false);
  assert.equal(nodes['#ra-load-default'].hidden,true);
  c.animCustom.ring=[];
  nodes['#anim-ring'].value='';
  vm.runInContext("fillAnimSelect('ring')",c);
  assert(!nodes['#anim-ring'].innerHTML.includes('saved.led'));
  await vm.runInContext('loadRingAnim()',c);
  assert.equal(nodes['#ra-reset'].hidden,true);
  console.log('Saved overrides remain selectable without catalog; reset disappears after removal');
})().catch(e=>{console.error(e);process.exitCode=1;});
