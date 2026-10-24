// Execute the actual modal event wiring with a controlled geometry/event host.
// This does not substitute for visual or native Windows acceptance.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../src/guard-queue.js',import.meta.url),'utf8');
const start=source.indexOf("    const dialog=bar.querySelector('.aio-guard-popup');");
const end=source.indexOf("    bar.querySelector('[data-g-jobs]')",start);
assert.ok(start>0&&end>start,'production modal event block must be found');
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
const bar={querySelector(selector){
  if(selector==='.aio-guard-popup')return dialog;
  if(selector==='[data-g-close]')return closeButton;
  throw Error('Unexpected DOM lookup '+selector);
}};
vm.runInNewContext(source.slice(start,end),{bar,stateButton,Number});
const point=(type,x,y,properties={})=>dialog.dispatch(type,{clientX:x,clientY:y,...properties});
function open(){dialog.open=true;stateButton.expanded='true';}

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
open();closeButton.dispatch('click');assert.equal(dialog.open,false,'close button closes modal');
open();const escape=dialog.dispatch('cancel');
assert.equal(dialog.open,false,'Escape closes modal');assert.equal(escape.defaultPrevented,true);
assert.equal(stateButton.focusCount,3,'focus returns to State button after each close');
console.log(JSON.stringify({status:'PASS',scope:'actual modal wiring; controlled geometry/events',
  padding:true,drag:true,secondaryButton:true,pointerCancel:true,backdrop:true,closeButton:true,escape:true,focus:true}));
