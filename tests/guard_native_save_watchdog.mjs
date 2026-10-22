// F03: actual native-save bridge, with a deterministic clock and host event.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const script=readFileSync(new URL('../src/bridge.js',import.meta.url),'utf8');
const ticket='x'.repeat(43),url='/api/converter-guard/result?ticket='+ticket;
let seq=0,clicked=0,removed=0;
const timers=new Map(),listeners=new Set(),anchors=[];
const window={
  AllInOneNativeShell:true,location:{href:'http://127.0.0.1:8123/'},
  addEventListener:(_name,fn)=>listeners.add(fn),
  removeEventListener:(_name,fn)=>listeners.delete(fn)
};
const body={dataset:{aioToken:'session'},append:anchor=>anchors.push(anchor)};
const document={body,createElement:()=>({
  click(){clicked++;},
  remove(){removed++;}
})};
const context=vm.createContext({window,document,URL,
  setTimeout:(fn,ms)=>{const id=++seq;timers.set(id,{fn,ms});return id;},
  clearTimeout:id=>timers.delete(id)});
vm.runInContext(script,context);
const save=()=>window.AllInOneBridge.saveExcel({name:'result.pdf',downloadUrl:url});

const first=save();
assert.equal(clicked,1);
assert.equal(listeners.size,1);
assert.equal(timers.size,1);
const [timeout]=timers.values();
assert.equal(timeout.ms,30*60*1000);
timeout.fn();
await assert.rejects(first,/Нет подтверждения сохранения/);
assert.equal(listeners.size,0,'watchdog must remove stale native listener');
assert.equal(timers.size,0,'watchdog must not retain a timer');
assert.equal(removed,1,'save anchor must not be retained after failure');

const second=save();
assert.equal(clicked,2);
const secondUrl=new URL(anchors.at(-1).href,window.location.href).href;
assert.notEqual(secondUrl,new URL(anchors[0].href,window.location.href).href,
  'a retry must use a distinct acknowledgement identity');
for(const listener of listeners)
  listener({detail:{sourceUrl:secondUrl,status:'completed',name:'result.pdf'}});
const result=await second;
assert.equal(result.saved,true);
assert.equal(listeners.size,0);
assert.equal(timers.size,0);
assert.equal(removed,2);
console.log('PASS F03: bounded native save, safe late event and retry identity');
