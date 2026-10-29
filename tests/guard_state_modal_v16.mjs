// Execute actual modal wiring and timer cleanup in a controlled event host.
// This does not substitute for visual or native Windows acceptance.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../src/guard-queue.js',import.meta.url),'utf8');
const start=source.indexOf("    const dialog=bar.querySelector('.aio-guard-popup');\n    bar.querySelector('[data-g-close]')");
const end=source.indexOf("    bar.querySelector('[data-g-jobs]')",start);
assert.ok(start>0&&end>start,'production modal event block must be found');
const helperStart=source.indexOf('  function dropRows(id){');
const helperEnd=source.indexOf('  function cancelPreview(){',helperStart);
assert.ok(helperStart>0&&helperEnd>helperStart,'production row cleanup helper must be found');
const timerStart=source.indexOf('  function stopRecoveryPoll(){');
const timerEnd=source.indexOf('  function scheduleRecoveryPoll(){',timerStart);
assert.ok(timerStart>0&&timerEnd>timerStart,'production recovery timer cleanup must be found');
class Element {
  handlers=new Map();open=true;
  addEventListener(type,listener){
    const listeners=this.handlers.get(type)||[];listeners.push(listener);this.handlers.set(type,listeners);
  }
  dispatch(type,properties={}){
    const event={target:this,button:0,clientX:250,clientY:200,preventDefault(){this.defaultPrevented=true;},...properties};
    for(const listener of this.handlers.get(type)||[])listener(event);
    return event;
  }
  close(){this.open=false;this.dispatch('close');}
  getBoundingClientRect(){return {left:100,top:100,right:500,bottom:400};}
}
const dialog=new Element(),closeButton=new Element();
const stateButton={isConnected:true,expanded:'true',focusCount:0,
  setAttribute(name,value){assert.equal(name,'aria-expanded');this.expanded=value;},
  focus(){this.focusCount++;}};
let rowCleanups=0,timerCleanups=0;
const list={replaceChildren(){rowCleanups++;}};
const bar={querySelector(selector){
  if(selector==='.aio-guard-popup')return dialog;
  if(selector==='[data-g-close]')return closeButton;
  if(selector==='.aio-guard-list')return list;
  throw Error('Unexpected DOM lookup '+selector);
}};
const context=vm.createContext({bar,stateButton,Number,rowCache:{jobId:'fixture'},paintedRows:[{}],
  recoveryPoll:17,clearTimeout(id){assert.equal(id,17);timerCleanups++;}});
vm.runInContext(source.slice(timerStart,timerEnd)+'\n'+source.slice(helperStart,helperEnd)+'\n'+source.slice(start,end),context);
const point=(type,x,y,properties={})=>dialog.dispatch(type,{clientX:x,clientY:y,...properties});
function open(){dialog.open=true;stateButton.expanded='true';context.rowCache={jobId:'fixture'};context.paintedRows=[{}];context.recoveryPoll=17;}

point('pointerdown',120,120);point('pointerup',120,120);
assert.equal(dialog.open,true,'dialog padding is inside the modal');
point('pointerdown',120,120,{target:{}});point('pointerup',20,20);
assert.equal(dialog.open,true,'drag from content into backdrop preserves modal');
point('pointerdown',20,20);point('pointerup',120,120);
assert.equal(dialog.open,true,'drag from backdrop into padding preserves modal');
point('pointerdown',20,20,{button:2});point('pointerup',20,20,{button:2});
assert.equal(dialog.open,true,'secondary pointer button preserves modal');
point('pointerdown',20,20);dialog.dispatch('pointercancel');point('pointerup',20,20);
assert.equal(dialog.open,true,'cancelled pointer cannot dismiss modal');
point('pointerdown',20,20);point('pointerup',20,20);
assert.equal(dialog.open,false,'primary backdrop click closes modal');
assert.equal(stateButton.expanded,'false');assert.equal(stateButton.focusCount,1);
assert.equal(context.rowCache,null,'actual production helper releases the task snapshot');
assert.equal(context.paintedRows,null,'actual production helper releases painted row references');
assert.equal(context.recoveryPoll,null,'actual production helper clears timer ownership');
open();closeButton.dispatch('click');assert.equal(dialog.open,false,'close button closes modal');
open();const escape=dialog.dispatch('cancel');
assert.equal(dialog.open,false,'Escape closes modal');assert.equal(escape.defaultPrevented,true);
assert.equal(stateButton.focusCount,3,'focus returns to State button after each close');
assert.equal(rowCleanups,3,'all three real close paths release hidden task DOM');
assert.equal(timerCleanups,3,'all three close paths stop recovery polling');
open();dialog.dispatch('close');
assert.equal(dialog.open,true,'an old queued close cannot clear a reopened dialog');
assert.notEqual(context.rowCache,null,'reopened dialog keeps its current task snapshot');
assert.equal(stateButton.focusCount,3,'stale close cannot steal focus from a reopened dialog');
assert.equal(rowCleanups,3,'stale close must not dispose the reopened task page');
assert.equal(timerCleanups,3,'stale close must not stop the reopened view timer');
console.log(JSON.stringify({status:'PASS',scope:'actual modal wiring; controlled geometry/events',
  padding:true,drag:true,secondaryButton:true,pointerCancel:true,backdrop:true,closeButton:true,escape:true,focus:true,taskCleanup:true,timerCleanup:true,staleCloseIgnored:true}));
