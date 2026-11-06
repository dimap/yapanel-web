const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(require('path').join(__dirname,'../static/index.html'),'utf8');
const app = fs.readFileSync(require('path').join(__dirname,'../static/app.js'),'utf8');
const ui = html + '\n' + app;
const source = ui.slice(ui.indexOf('function vcmp('), ui.indexOf("$('#yam-filter').oninput = renderYam;"));
const elements = {};
const node = id => elements[id] ||= {value:'',innerHTML:'',textContent:''};
const context = {$:node,$$:()=>[],esc:x=>String(x??''),icon:()=>'',yam:{plugins:[{manifest:{id:'station-airplay',name:'Station airplay',version:'1.4.0',launch:{mode:'service'}},enabled:true,running:true,compatible:true}],registry:[{id:'station-airplay',name:'Station airplay',version:'1.1.5',description:''},{id:'station-airplay',name:'Station airplay',version:'1.4.0',description:''},{id:'station-airplay',name:'Station airplay',version:'1.2.0',description:''},{id:'sqlite',name:'sqlite',version:'3.45.3',description:''}]}};
vm.createContext(context);vm.runInContext(source+'\nrenderYam();',context);
const catalog=node('#yam-catalog').innerHTML;
assert.equal((catalog.match(/Station airplay/g)||[]).length,1);
assert(catalog.includes('1.4.0') && !catalog.includes('1.1.5') && !catalog.includes('1.2.0'));
assert.equal((catalog.match(/установлено/g)||[]).length,1);
assert(catalog.includes('sqlite'));
context.yam.registry.push({id:'station-airplay',name:'Station airplay',version:'1.5.0',description:''});
vm.runInContext('renderYam();',context);
assert(node('#yam-catalog').innerHTML.includes('Обновить с 1.4.0'));
node('#yam-filter').value='sqlite';vm.runInContext('renderYam();',context);
assert(!node('#yam-catalog').innerHTML.includes('Station airplay'));
console.log('PASS: one latest version per ID; one installed badge; update and search preserved');


(async () => {
  const pkgSource = ui.slice(ui.indexOf('async function yam2Pkg('), ui.indexOf('async function yam2Svc('));
  for (const action of ['install', 'remove']) {
    const calls = [];
    const nav = {hidden: true};
    const ctx = {
      api: async () => ({}), act: async (btn, fn) => fn(), confirm: () => true,
      toast: () => {}, loadYam2Packages: async () => calls.push('packages'),
      window: {wwLoad: async () => { nav.hidden = action !== 'install'; calls.push('wakeword'); }}
    };
    vm.createContext(ctx);
    vm.runInContext(pkgSource, ctx);
    await ctx.yam2Pkg({textContent: 'Install'}, action, 'tangenx/wakeword@1.2.3');
    assert.deepEqual(calls, ['wakeword', 'packages']);
    assert.equal(nav.hidden, action !== 'install');
    calls.length = 0;
    await ctx.yam2Pkg({textContent: 'Install'}, action, 'yam/other');
    assert.deepEqual(calls, ['packages']);
    ctx.api = async () => ({need_yes: true, text: 'confirm'});
    ctx.confirm = () => false;
    ctx.y2out = () => {};
    calls.length = 0;
    await ctx.yam2Pkg({textContent: 'Install'}, 'install', 'tangenx/wakeword');
    assert.deepEqual(calls, []);
  }
  console.log('PASS: wakeword navigation refreshes immediately after package changes');
})().catch(e => { console.error(e); process.exitCode = 1; });
