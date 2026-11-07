const assert = require('node:assert/strict');
const route = require('../static/assistant-route.js');
const c = {enabled:true, microphone:{source:'microphone'},
  llm:{base_url:'http://192.168.2.85:8642/v1',model:'hermes-agent'},
  tts:{provider:'openai',base_url:'http://192.168.2.85:18322/v1',model:'tts-1',voice:'NecoArcRu'},
  stt:{provider:'custom',base_url:'http://user:secret@localhost:18445/v1?api_key=secret',model:'base'}};
const live = {running:true,microphone_installed:true,state:{mode:false,microphone:{ready:true}}};
let r = route.build(c,live);
assert.deepEqual(r.nodes.map(n=>n.kind),['input','stt','model','tts','speaker']);
assert.equal(r.nodes[1].title,'Алиса'); // Direct capture is not used outside mode.
assert.equal(r.nodes[3].title,'NecoArcRu');
assert(r.notes.some(n=>n.text.includes('после включения')));
r = route.build(c,live,'conversation');
assert.equal(r.nodes[1].title,'base');
assert.equal(r.nodes[1].detail,'localhost:18445');
assert(!JSON.stringify(r).includes('secret')); // Never reveal URL credentials/query strings.
assert(r.notes.some(n=>n.text.includes('Docker')));
r = route.build({...c,llm:{...c.llm,transport:'realtime',voice:'marin'}},live,'conversation');
assert.deepEqual(r.nodes.map(n=>n.kind),['input','model','speaker']);
assert(!JSON.stringify(r.nodes).includes('NecoArcRu'));
r = route.build({...c,llm:{...c.llm,transport:'realtime'}},live,'panel');
assert.deepEqual(r.nodes.map(n=>n.kind),['input','model','speaker']);
assert.equal(r.nodes[0].output,'текст');
r = route.build(c,live,'panel');
assert.deepEqual(r.nodes.map(n=>n.kind),['input','model','tts','speaker']);
r = route.build({...c,enabled:false},live);
assert.deepEqual(r.nodes.map(n=>n.kind),['input','stt','speaker']);
assert(!JSON.stringify(r.nodes).includes('hermes'));
r = route.build({...c,stt:{provider:'alice'}},{running:true,state:{mode:true}});
assert(r.nodes[1].bad);
assert(r.notes.some(n=>n.bad&&n.text.includes('захват')));
assert(r.notes.some(n=>n.bad&&n.text.includes('STT')));
assert.notEqual(route.signature(c),route.signature({...c,tts:{...c.tts,model:''}}));
console.log('Assistant route matches current mode, text input, native Realtime, STT readiness and privacy');

// Exercise the real renderer: polling reflects saved settings without erasing
// a local draft, and model names remain text rather than executable markup.
const fs = require('node:fs'), vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname,'../static/index.html'),'utf8');
const start = html.indexOf('function asRouteRender()');
const render = html.slice(start,html.indexOf("function asSelectJourney(",start));
const fields = {};
const ctx = {AssistantRoute:route,window:{AssistantRoute:route},asCfg:c,asRouteRuntime:{...live,config:c},asRouteJourney:'current',
  asRouteForm:()=>ctx.form,$:id=>fields[id] ||= {style:{setProperty(){}}},
  esc:value=>String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'),form:c};
vm.createContext(ctx);vm.runInContext(render,ctx);vm.runInContext('asRouteRender()',ctx);
assert(fields['#as-route-flow'].innerHTML.includes('NecoArcRu'));
assert.equal(fields['#as-route-draft'].hidden, true);
ctx.form={...c,llm:{...c.llm,model:'<img src=x onerror=alert(1)>'}};
vm.runInContext('asRouteRender()',ctx);
assert.equal(fields['#as-route-draft'].hidden, false);
assert(fields['#as-route-flow'].innerHTML.includes('&lt;img'));
assert(!fields['#as-route-flow'].innerHTML.includes('<img'));
ctx.form=c;ctx.asRouteRuntime.config={...c,tts:{...c.tts,voice:'Updated voice'}};
vm.runInContext('asRouteRender()',ctx);
assert(fields['#as-route-flow'].innerHTML.includes('Updated voice'));
assert.equal(fields['#as-route-draft'].hidden, true);
ctx.form={...c,enabled:false};ctx.asRouteRuntime.config=ctx.form;
vm.runInContext('asRouteRender()',ctx);
assert.equal(fields['#as-route-draft'].hidden, true);
assert(!fields['#as-route-flow'].innerHTML.includes('hermes-agent'));
console.log('Route renderer distinguishes drafts from saved runtime configuration and escapes labels');

for (const model of ['gpt-realtime','gpt-realtime-mini','gpt-realtime-1.5','gpt-realtime-2.1-mini','gpt-realtime-2025-08-28','gpt-realtime-mini-2025-10-06','gpt-4o-mini-realtime-preview-2024-12-17']) {
 assert(route.capabilities('https://api.openai.com/v1',model).realtime,model);
 assert(!route.capabilities('https://api.commandcode.ai/provider/v1',model).realtime,model+' is not enabled through a text-only proxy');
}
for (const model of ['gpt-4o-mini','hermes-agent','gpt-realtime-whisper','gpt-realtime-translate','gpt-live']) assert(!route.capabilities('https://api.openai.com/v1',model).realtime,model);
for (const model of ['whisper-1','gpt-4o-mini-tts','text-embedding-3-small','gpt-realtime-translate']) assert(!route.conversationModel('https://api.openai.com/v1',model),model);
assert(route.capabilities('https://api.groq.com/openai/v1','text-model').modelSTT);
assert(!route.capabilities('http://hermes/v1','hermes-agent').modelSTT);
console.log('Protocol and provider STT capabilities exclude unsupported and non-conversational models');
