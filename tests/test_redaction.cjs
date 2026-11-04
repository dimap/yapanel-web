// Evaluate the real browser function without a speaker or any real credentials.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
const code=html.slice(html.indexOf('function maskLog(t)'),html.indexOf('\nfunction renderLog()'));
const context={status:{},ymLogin:''};vm.createContext(context);vm.runInContext(code,context);
const fixtures=JSON.parse(fs.readFileSync(path.join(__dirname,'fixtures/redaction.json'),'utf8'));
for(const item of fixtures){
  const masked=context.maskLog(item.input);
  for(const hidden of item.hidden)assert(!masked.includes(hidden),item.name+': leaked synthetic value');
  for(const keep of item.keep)assert(masked.includes(keep),item.name+': lost message context '+keep);
  assert.equal(context.maskLog(masked),masked,item.name+': repeat masking changes the result');
}
context.status={device_id:'demo-device-context',ssid:'Demo Home WiFi',address:'198.51.100.9',hostname:'Yandex-Station-DEMO1234'};
context.ymLogin='demo-login';
const contextual=context.maskLog('demo-device-context Demo Home WiFi 198.51.100.9 DEMO1234 demo-login');
for(const secret of Object.values(context.status).concat('DEMO1234',context.ymLogin))assert(!contextual.includes(secret));
context.longLine='endpoint_id="'+'\\'.repeat(10000)+'demo-truncated';
const truncated=vm.runInContext('maskLog(longLine)',context,{timeout:2000});
assert(!truncated.includes('demo-truncated'),'truncated escaped input is masked without pathological backtracking');
console.log('Browser log privacy: synthetic endpoint IDs, URL signatures, credentials, context, versions and idempotence passed');
