/* Frontend contract tests; node --test tests/test_converter_guard_bridge.cjs */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '../src/converter-bridge.js'), 'utf8');
const job = 'a'.repeat(32);
const ticket = 't'.repeat(43);

function harness({failed = false, saved = {saved:true}, inspectError = false} = {}) {
  const calls = [], events = [], buttons = [];
  let polls = 0;
  const controller = {};
  for (const name of ['begin','update','beginSave','finishSave','failBatch','dispose'])
    controller[name] = details => events.push({name, ...details});
  const document = {
    body: {dataset:{aioToken:'test-session'}},
    querySelector(selector) { return selector === '.aio-cv' ? {} : {append: node=>buttons.push(node)}; },
    createElement() { return {dataset:{}, addEventListener(name, callback) {this[name]=callback;}, remove() {this.removed=true;}}; }
  };
  const window = {AllInOneBridge: {async saveExcel(result) {calls.push({save:result});return saved;}},
    AIOFileProgress: {attach:()=>controller}};
  async function fetch(url, options={}) {
    calls.push({url, options});
    const route = url.split('/api/converter/')[1].split('?')[0];
    if (route === 'inspect') return {ok:!inspectError, async json(){return inspectError ?
      {code:'INPUT_SIZE',error:'Too large'} : {formats:[{id:'pdf',enabled:true}],files:[]};}};
    if (route === 'jobs' && options.method === 'POST') return {ok:true, async json(){return {jobId:job};}};
    if (route === 'jobs') {
      polls++;
      const finished = polls >= 2;
      return {ok:true, async json(){return {jobId:job,finished,
        files:[{fileId:'f1',name:'a.docx',state:finished?(failed?'error':'ready'):'converting',
          code:failed?'ENGINE_OUTPUT_MISSING':null,error:failed?'No PDF':null,percent:finished?100:null,libreOfficeStarted:true}],
        completed:finished?1:0,total:1,percent:finished?100:0,
        trace:{events:[{sequence:1,stage:'engine_started',libreOfficeStarted:true}]},
        result:finished&&!failed?{jobId:job,name:'a.pdf',downloadUrl:'/api/converter/result?ticket='+ticket,
          outputs:[{fileId:'f1',name:'a.pdf',downloadUrl:'/api/converter/result?ticket='+ticket}]}:null};}};
    }
    return {ok:true, async json(){return {ok:true};}};
  }
  vm.runInNewContext(code, {window,document,fetch,AbortController,console,
    setTimeout: callback=>setTimeout(callback,0)});
  const files = [{id:'f1',file:{name:'a.docx',size:4}}];
  return {bridge:window.AllInOneBridge.converter,calls,events,buttons,files};
}

test('inspection sends only file metadata and session token', async()=>{
  const h=harness(); await h.bridge.inspectFiles({files:h.files});
  assert.deepEqual(JSON.parse(h.calls[0].options.body), {files:[{id:'f1',name:'a.docx',size:4}]});
  assert.equal(h.calls[0].options.headers['X-AIO-Token'],'test-session');
  assert.equal(h.calls.length,1);
});
test('conversion streams the original File object and preserves engine states', async()=>{
  const h=harness(), values=[];
  const out=await h.bridge.convert({files:h.files,formatId:'pdf',onProgress:v=>values.push(v)});
  assert.equal(out.ok,true); assert.equal(out.result.targetReport.succeeded,1);
  const upload=h.calls.find(c=>c.url?.includes('/input?'));
  assert.equal(upload.options.body,h.files[0].file);
  assert.ok(h.events.some(e=>e.name==='update'&&e.state==='converting'&&e.percent===null));
  assert.ok(h.events.some(e=>e.name==='update'&&e.state==='ready'&&e.percent===100));
  assert.ok(values.some(v=>v.percent===null));
  assert.equal(h.buttons[0].removed,true);
});
test('native save is acknowledged before release and saved state', async()=>{
  const h=harness(), out=await h.bridge.convert({files:h.files});
  const result=await h.bridge.saveResult({result:out.result});
  assert.equal(result.saved,true);
  assert.equal(h.calls.findIndex(c=>c.save)>0,true);
  assert.equal(h.calls.findIndex(c=>c.url?.endsWith('/release'))>h.calls.findIndex(c=>c.save),true);
  assert.equal(h.events.find(e=>e.name==='finishSave').saved,true);
});
test('cancelled native save keeps the same result available for retry', async()=>{
  const h=harness({saved:{cancelled:true}}),out=await h.bridge.convert({files:h.files});
  await h.bridge.saveResult({result:out.result});
  await h.bridge.saveResult({result:out.result});
  assert.equal(h.calls.filter(c=>c.save).length,2);
  assert.equal(h.calls.filter(c=>c.url?.endsWith('/release')).length,0);
  assert.equal(h.events.filter(e=>e.name==='finishSave').every(e=>!e.saved),true);
});
test('browser download initiation is not reported as a confirmed save', async()=>{
  const h=harness({saved:{downloadStarted:true}}),out=await h.bridge.convert({files:h.files});
  const saved=await h.bridge.saveResult({result:out.result});
  assert.equal(saved.downloadStarted,true); assert.equal(saved.saved,undefined);
  assert.equal(h.events.find(e=>e.name==='finishSave').saved,false);
});
test('failed batch retains per-file error and releases disk ownership', async()=>{
  const h=harness({failed:true});
  await assert.rejects(h.bridge.convert({files:h.files}),e=>e.code==='ENGINE_OUTPUT_MISSING'&&e.batchReport.failed===1);
  assert.equal(h.calls.filter(c=>c.url?.endsWith('/release')).length,1);
});
test('unsupported options and formats cannot be silently ignored', async()=>{
  const h=harness();
  await assert.rejects(h.bridge.convert({files:h.files,formatId:'docx'}),e=>e.code==='OUTPUT_NOT_CONNECTED');
  await assert.rejects(h.bridge.convert({files:h.files,options:{dpi:72}}),e=>e.code==='OUTPUT_NOT_CONNECTED');
  await assert.rejects(h.bridge.convertMany({files:h.files,formatIds:['pdf','docx']}),e=>e.code==='OUTPUT_NOT_CONNECTED');
  assert.equal(h.calls.length,0);
});
test('deep comparison is explicitly not checked while stage trace is available', async()=>{
  const h=harness(),out=await h.bridge.convert({files:h.files});
  const report=await h.bridge.diagnoseFiles({files:h.files,phase:'after',result:out.result});
  assert.equal(report.result.checks[0].state,'not_checked');
  assert.ok(report.scope.some(s=>s.includes('engine_started')));
});
test('reset disposes progress and releases completed output', async()=>{
  const h=harness(); await h.bridge.convert({files:h.files});
  h.bridge.resetProgress(); await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.calls.filter(c=>c.url?.endsWith('/release')).length,1);
  assert.ok(h.events.some(e=>e.name==='dispose'));
});
test('structured inspection errors reach UI with their rule code', async()=>{
  const h=harness({inspectError:true});
  await assert.rejects(h.bridge.inspectFiles({files:h.files}),e=>e.code==='INPUT_SIZE'&&e.userMessage==='Too large');
});
