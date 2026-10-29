// Actual materialized production handlers + DOM; controlled scroll clamping.
// This checks stable node identity, not installed WebEngine pixel geometry.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
const {parseHTML}=await import(pathToFileURL(process.env.AIO_DOM_MODULE).href);
const source=fs.readFileSync(process.argv[2],'utf8'),mode=process.argv[3];
const begin=source.indexOf('    function selectAllAvailableFormats() {');
const end=source.indexOf('    function renderOptions() {',begin);
assert.ok(begin>0&&end>begin,'production selection block must exist');
const {window,document}=parseHTML('<html><body><div id="formats"></div></body></html>');
const formatsArea=document.getElementById('formats');
let resetCount=0,replaceCount=0,summaryCount=0;
const replace=formatsArea.replaceChildren.bind(formatsArea);
formatsArea.replaceChildren=(...nodes)=>{replaceCount++;formatsArea.scrollTop=0;replace(...nodes);};
const formats=[
  {id:'pdf',label:'PDF',category:'documents',enabled:true,verifiedFileIds:['f'],selectableFileIds:['f']},
  {id:'odt',label:'ODT',category:'documents',enabled:true,verifiedFileIds:['f'],selectableFileIds:['f']},
  {id:'png',label:'PNG',category:'images',enabled:true,verifiedFileIds:['f'],selectableFileIds:['f']},
  {id:'disabled',label:'DISABLED',category:'documents',enabled:false,verifiedFileIds:['f'],selectableFileIds:[]},
  {id:'other-file',label:'OTHER',category:'documents',enabled:true,verifiedFileIds:['other'],selectableFileIds:['other']},
  {id:'unverified',label:'UNVERIFIED',category:'documents',enabled:true,verifiedFileIds:[],selectableFileIds:['f']}
];
const selectedFormats=new Set(),selectedFileIds=new Set(['f']);
const context=vm.createContext({document,formatsArea,analysis:{formats},selectedFormats,selectedFileIds,
  selectedCategory:'documents',selectedFormat:null,formatValues:{},currentResult:null,retryResult:null,
  status:'idle',statusMessage:'',realProgress:null,saving:false,
  adapter:{resetProgress(){resetCount++;},inspectFiles(){throw Error('selection may not inspect');},
    convert(){throw Error('selection may not convert');},convertMany(){throw Error('selection may not convert');}},
  selectedFiles:()=>[...selectedFileIds].map(id=>({id})),
  eligibleIds:(fmt,field)=>new Set(fmt[field]||[]),
  formatAvailable:fmt=>fmt.enabled&&fmt.selectableFileIds.some(id=>selectedFileIds.has(id)),
  closePopover(){},openHelp(){},announce(){},esc:value=>String(value),
  renderOptions(){},renderSummary(){summaryCount++;}});
vm.runInContext(source.slice(begin,end),context);
context.render=()=>{context.renderFormats();context.renderOptions();context.renderSummary();};
context.render();
const button=label=>[...formatsArea.querySelectorAll('.aio-cv__format-main')].find(b=>b.getAttribute('aria-label')==='Выбрать '+label);
const ids=()=>[...selectedFormats].sort();
const confirmed=['odt','pdf','png'];
if(mode==='single'){
  const pdf=button('PDF'),wrapper=pdf.parentElement;formatsArea.scrollTop=437;
  pdf.click();
  assert.equal(button('PDF'),pdf,'FORMAT_SELECTION_RECREATED: a click must keep its focusable button');
  assert.equal(pdf.parentElement,wrapper);assert.equal(formatsArea.scrollTop,437);
  assert.equal(pdf.getAttribute('aria-pressed'),'true');assert.equal(wrapper.getAttribute('aria-selected'),'true');
  pdf.click();assert.equal(button('PDF'),pdf);assert.equal(formatsArea.scrollTop,437);
  assert.equal(pdf.getAttribute('aria-pressed'),'false');assert.equal(replaceCount,1);
}else if(mode==='toggle'){
  context.selectAllAvailableFormats();assert.deepEqual(ids(),confirmed);
  context.selectAllAvailableFormats();
  assert.deepEqual(ids(),[],'BULK_SECOND_CLICK_DID_NOT_CLEAR');
  assert.equal(context.selectedFormat,null);assert.equal(resetCount,2);
}else if(mode==='bulk_identity'){
  const cards=[...formatsArea.children];formatsArea.scrollTop=271;
  for(let i=0;i<4;i++){
    context.selectAllAvailableFormats();assert.deepEqual([...formatsArea.children],cards);
    assert.equal(formatsArea.scrollTop,271);
  }
  assert.equal(replaceCount,1);assert.equal(summaryCount,5);
}else if(mode==='eligibility'){
  selectedFormats.add('pdf');context.selectAllAvailableFormats();assert.deepEqual(ids(),confirmed);
  assert.deepEqual([...selectedFileIds],['f']);
  selectedFormats.add('unverified');context.selectAllAvailableFormats();assert.deepEqual(ids(),[]);
  selectedFileIds.clear();selectedFileIds.add('other');context.selectAllAvailableFormats();
  assert.deepEqual(ids(),['other-file']);
}else if(mode==='categories'){
  context.selectAllAvailableFormats();context.selectedCategory='images';context.renderFormats();
  assert.equal(button('PNG').getAttribute('aria-pressed'),'true');
  button('PNG').click();assert.deepEqual(ids(),['odt','pdf']);
  context.selectAllAvailableFormats();assert.deepEqual(ids(),confirmed);
  context.selectAllAvailableFormats();assert.deepEqual(ids(),[]);
}else if(mode==='busy'){
  const pdf=button('PDF');selectedFormats.add('odt');
  for(const state of ['checking','processing']){
    context.status=state;context.selectAllAvailableFormats();pdf.click();assert.deepEqual(ids(),['odt']);
  }
  context.status='idle';context.saving=true;context.selectAllAvailableFormats();pdf.click();
  assert.deepEqual(ids(),['odt']);assert.equal(resetCount,0);
}else if(mode==='empty'){
  selectedFileIds.clear();context.selectAllAvailableFormats();assert.deepEqual(ids(),[]);
  assert.equal(resetCount,0);
}else throw Error('unknown case '+mode);
console.log('AIO_SELECTION_CONTROLS='+JSON.stringify({status:'PASS',case:mode,
  scope:'production handlers and DOM; controlled scroll clamping, not installed GUI'}));
