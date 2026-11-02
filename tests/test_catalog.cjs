const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(require('path').join(__dirname,'../static/index.html'),'utf8');
const source = html.slice(html.indexOf('function vcmp('), html.indexOf("$('#yam-filter').oninput = renderYam;"));
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

// Render the real YAM 2 loaders and package action to cover links across install,
// upgrade and removal, including catalog/package responses arriving separately.
(async () => {
  const nodes = {};
  const get = id => nodes[id] ||= {value: '', checked: false, innerHTML: '', textContent: ''};
  const pkg = {name: 'example/player', latest: '1.0.0', summary: 'Player', fits: true,
    homepage: 'https://example.org/player?first=1&second="value"'};
  const installed = {name: pkg.name, version: pkg.latest, target: 'yandexmini_2', services: []};
  const state = {packages: [], catalog: null};
  let packages = [];
  const ctx = {
    $: get, $$: () => [], y2: state, esc: s => String(s ?? '').replace(/[&<>"]/g,
      c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c])), icon: () => '',
    act: async (_btn, fn) => fn(), confirm: () => true, toast() {}, window: {},
    api: async (url, data) => {
      if (url.startsWith('/api/yam2/catalog')) return {configured: true, packages: [pkg]};
      if (url === '/api/yam2/packages') return {packages};
      assert.equal(url, '/api/yam2/pkg');
      packages = data.action === 'install' ? [{...installed, version: pkg.latest}] : [];
      return {};
    }
  };
  vm.createContext(ctx);
  const start = html.indexOf('async function loadYam2Packages(');
  vm.runInContext(html.slice(start, html.indexOf('function y2out(', start)) + '\n' +
    html.slice(html.indexOf('async function yam2Pkg('), html.indexOf('async function yam2Svc(')) + '\n' +
    html.slice(html.indexOf('function vcmp('), html.indexOf('function renderYam()')), ctx);
  const link = 'href="https://example.org/player?first=1&amp;second=&quot;value&quot;" target="_blank" rel="noopener">Сайт ↗</a>';
  await ctx.loadYam2Packages();
  await ctx.loadYam2Catalog();
  assert(get('#y2-catalog').innerHTML.includes(link));
  await ctx.yam2Pkg({textContent: 'Установить'}, 'install', pkg.name);
  assert(get('#y2-installed').innerHTML.includes(link), 'installed package retains its homepage');
  assert(get('#y2-catalog').innerHTML.includes(link), 'installed catalog card retains its homepage');
  assert(get('#y2-catalog').innerHTML.includes('установлено'));
  // A search that hides the catalog card must not hide the installed link.
  get('#y2-filter').value = 'no matches';
  ctx.renderYam2();
  assert(get('#y2-installed').innerHTML.includes(link));
  assert(!get('#y2-catalog').innerHTML.includes(link));
  get('#y2-filter').value = '';
  pkg.latest = '1.1.0';
  ctx.renderYam2();
  assert(get('#y2-installed').innerHTML.includes('Обновить'));
  assert(get('#y2-installed').innerHTML.includes(link));
  await ctx.yam2Pkg({textContent: 'Обновить'}, 'install', pkg.name);
  assert(get('#y2-installed').innerHTML.includes(link));
  for (const homepage of [null, undefined, '', 'javascript:alert(1)', 'http://example.org', '//example.org']) {
    pkg.homepage = homepage;
    ctx.renderYam2();
    assert(!get('#y2-installed').innerHTML.includes('Сайт ↗'));
    assert(!get('#y2-catalog').innerHTML.includes('Сайт ↗'));
  }
  pkg.homepage = 'https://example.org';
  state.catalog = null;
  ctx.renderYam2();
  assert(!get('#y2-installed').innerHTML.includes('Сайт ↗'));
  await ctx.loadYam2Catalog();
  assert(get('#y2-installed').innerHTML.includes('Сайт ↗'), 'late catalog response adds the link');
  await ctx.yam2Pkg({textContent: 'Удалить'}, 'remove', pkg.name);
  assert(!get('#y2-installed').innerHTML.includes('Сайт ↗'));
  assert(get('#y2-catalog').innerHTML.includes('Сайт ↗'));
  console.log('PASS: YAM 2 homepages survive install/update, filtering and late catalog loading; absent/unsafe URLs stay hidden');
})().catch(e => { console.error(e); process.exitCode = 1; });


(async () => {
  const pkgSource = html.slice(html.indexOf('async function yam2Pkg('), html.indexOf('async function yam2Svc('));
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
