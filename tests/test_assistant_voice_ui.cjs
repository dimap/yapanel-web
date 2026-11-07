const fs = require('fs'), vm = require('vm'), assert = require('assert');
const AssistantRoute = require('../static/assistant-route.js');
const html = fs.readFileSync(require('path').join(__dirname, '../static/index.html'), 'utf8');
const nodes = {}, groups = {};
const get = id => nodes[id] ||= {value:'', checked:false, dataset:{}, options:{},
  focus(){},addEventListener(){},querySelector(s){return this.options[s] ||= {};}};
const context = {AssistantRoute,asRouteRuntime:{},asMicPending:null,asMicNotice:'',asProtocolModels:new Map(),asModelList:{url:'',models:[]},esc:s=>String(s), $:get,$$:s=>groups[s] ||= [{},{}],asTtsView(){},asRouteRender(){},asFill(){},
  asTransportButtons(){},asDirty(){},asConnStore(){},asKeysView(){},asPane(){},asPaneName:'voice',
  act:async (_,fn)=>fn(),api:async (path,data)=>{context.saved={path,data};return {config:data};}};
vm.createContext(context);
const extract=(start,end)=>html.slice(html.indexOf(start),html.indexOf(end,html.indexOf(start)));
vm.runInContext(extract('function asAudioView()', "$('#as-transport').onchange"),context);
const view=()=>vm.runInContext('asAudioView()',context);
get('#as-url').value='http://hermes/v1';get('#as-model').value='hermes-agent';
get('#as-input').value='alice';get('#as-stt').value='custom';get('#as-transport').value='realtime';
view();
assert.equal(get('#as-transport').value,'chat','Hermes cannot select Realtime');
assert(get('#as-stt-card').hidden);assert(!get('#as-tts-card').hidden);assert(get('#as-mic-controls').hidden);assert(get('#as-mic-connect').hidden);
assert(groups['[data-stt-custom]'].every(n=>n.hidden));
get('#as-input').value='microphone';get('#as-stt').value='model';view();
assert.equal(get('#as-stt').value,'custom','unknown provider must use explicit custom STT');
assert(!get('#as-stt-card').hidden);assert(!get('#as-mic-controls').hidden);
let journey, offset;
context.asSelectJourney = value => { journey = value; };
context.window = {scrollBy(options){offset = options.top;}};
let measurements = [250, 90];
get('#as-transport').getBoundingClientRect = () => ({top:measurements.shift()});
vm.runInContext(extract("$('#as-transport').onchange", "['#as-url', '#as-model']"),context);
get('#as-stt').onchange({target:{id:'as-stt'}});
assert.equal(journey,'conversation');
get('#as-transport').onchange({target:{id:'as-transport'},currentTarget:get('#as-transport')});
assert.equal(offset,-160);

assert(groups['[data-stt-custom]'].every(n=>!n.hidden));
get('#as-url').value='https://api.openai.com/v1';get('#as-model').value='gpt-4o-mini';get('#as-stt').value='model';view();
assert(!get('#as-transport-row').hidden,'OpenAI must expose protocol selection before choosing a realtime model');
get('#as-transport').value='realtime';vm.runInContext("asAudioView({target:{id:'as-transport'}})",context);
assert.equal(get('#as-model').value,'gpt-realtime');assert.equal(get('#as-transport').value,'realtime');
get('#as-transport').value='chat';vm.runInContext("asAudioView({target:{id:'as-transport'}})",context);
assert.equal(get('#as-model').value,'gpt-4o-mini');
assert.equal(get('#as-transport').value,'chat');assert(groups['[data-stt-custom]'].every(n=>n.hidden));
get('#as-model').value='gpt-realtime';view();
assert.equal(get('#as-transport').value,'realtime');assert(get('#as-stt-card').hidden);assert(get('#as-tts-card').hidden);
assert(groups['[data-realtime]'].every(n=>!n.hidden));assert(groups['[data-text-mode]'].every(n=>n.hidden));
assert.equal(get('#as-realtime-voice').value,'marin');
get('#as-model').value='gpt-4o-mini-realtime-preview';view();assert.equal(get('#as-realtime-voice').value,'alloy');
get('#as-url').value='https://api.commandcode.ai/provider/v1';view();assert.equal(get('#as-transport').value,'chat');assert(get('#as-transport-row').hidden);
context.asRouteRuntime={microphone_supported:false};view();assert.equal(get('#as-input').value,'alice');assert(get('#as-source-mic-choice').hidden);
context.asRouteRuntime={microphone_installed:true};view();assert(!get('#as-mic-connect').hidden,'disconnect remains accessible');
vm.runInContext(extract("async function asMicrophoneToggle", 'const asLed'),context);
vm.runInContext(extract("$('#as-save').onclick", "$('#as-ask').onclick"),context);
const values={'#as-transport':'realtime','#as-realtime-voice':'marin','#as-model':'gpt-realtime','#as-input':'microphone',
 '#as-stt':'custom','#as-stt-model':'local-whisper','#as-stt-url':'http://stt/v1','#as-stt-key':'test-secret',
 '#as-tts':'openai','#as-tts-model':'my-synth','#as-tts-voice':'my-voice','#as-mic-silence':'900','#as-mic-threshold':'300','#as-mic-gain':'32'};
for (const [id,value]of Object.entries(values)){assert(html.includes(`id="${id.slice(1)}"`),id);get(id).value=value;}
async function save(){await get('#as-save').onclick({currentTarget:{}});return context.saved.data;}
(async()=>{
 let d=await save();assert.equal(d.llm.transport,'realtime');assert.equal(d.llm.voice,'marin');assert(!('stt'in d));assert(!('tts'in d));assert(!('exit'in d.mode));
 get('#as-transport').value='chat';get('#as-input').value='alice';get('#as-tts').value='alice';
 d=await save();assert(!('voice'in d.llm));assert(!('stt'in d));assert.deepEqual(d.microphone,{source:'alice'});assert.deepEqual(d.tts,{provider:'alice'});
 get('#as-input').value='microphone';get('#as-tts').value='openai';d=await save();
 assert.equal(d.tts.voice,'my-voice');assert.equal(d.stt.api_key,'test-secret');assert.equal(d.microphone.silence_ms,900);
 get('#as-stt').value='model';d=await save();assert(!('api_key'in d.stt));assert(!('base_url'in d.stt));
 context.asRouteRuntime={running:true,microphone_installed:false};
 get('#as-input').value='alice';get('#as-url').value='http://hermes/v1';get('#as-model').value='hermes-agent';view();
 assert(get('#as-stt-card').hidden);
 get('#as-input').value='microphone';get('#as-stt').value='custom';view();
 assert(!get('#as-stt-card').hidden);
 let release;
 context.api=()=>new Promise(resolve=>{release=resolve;});
 const button=get('#as-mic-connect');button.dataset.on='';
 const event={currentTarget:button};const operation=button.onclick(event);event.currentTarget=null;
 assert(button.disabled);assert.match(button.textContent,/Подключаю/);assert.match(get('#as-mic-status').textContent,/перезапускаются/);
 view();assert.match(button.textContent,/Подключаю/,'polling must not erase progress');
 release({running:true,microphone_installed:true,state:{microphone:{ready:true}}});await operation;
 assert(!button.disabled);assert.equal(button.dataset.on,'1');assert.match(get('#as-mic-status').textContent,/поток микрофонов получен/);
 context.api=async()=>{throw Object.assign(new Error('Not found'),{status:404});};
 await button.onclick({currentTarget:button});assert(!button.disabled);assert.match(get('#as-mic-status').textContent,/Перезапустите/);
 console.log('Explicit OpenAI protocol, discoverable STT setup, capture progress and inline errors work');
 console.log('Options follow model/provider/input capabilities; inactive settings and keys are not overwritten');
})().catch(e=>{console.error(e);process.exitCode=1;});
