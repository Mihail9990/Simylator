'use strict';
// Exercise the shipped interrupt handler, including dismissal and stale views.
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync('web/basket-panel/panel.js','utf8');
const html=fs.readFileSync('web/basket-panel/index.html','utf8');
assert(html.includes('id="paper-interrupt">Прервать цикл'));
const elements={'paper-interrupt':{}};
const state={scope:'PAPER-fixture',revision:7,cycle_id:'cycle-1',positions:[{id:'position-1'}],orders:[{id:'order-1'}],paper:{paused_gap:false,commission_per_lot_side:'2'},feed:{status:'FRESH'}};
let approve=false,calls=[],dialogs=[];
const ctx=vm.createContext({$:id=>elements[id],mode:'paper',view:state,v3Fresh:true,pending:null,busy:false,
 confirm:text=>{dialogs.push(text);return approve;},send:async(...args)=>{calls.push(args);ctx.pending='pending-id';},error:message=>assert.fail(message)});
vm.runInContext(source.slice(source.indexOf(' // Explicit PAPER-only settlement.'),source.indexOf(" $('paper-flatten').onclick=")),ctx);
(async()=>{
 const click=elements['paper-interrupt'].onclick;
 await click();assert.equal(dialogs.length,1);assert.equal(calls.length,0,'Cancel must not send');
 approve=true;await click();assert.equal(calls.length,1);const [a,revision,scope]=calls[0];
 assert.equal(a.type,'paper_interrupt');assert.equal(a.confirmed,true);assert.equal(a.gap_confirmed,false);
 assert.deepEqual(Array.from(a.expected_positions),['position-1']);assert.deepEqual(Array.from(a.expected_orders),['order-1']);
 assert.equal(a.cycle_id,'cycle-1');assert.equal(revision,7);assert.equal(scope,'PAPER-fixture');
 assert(dialogs[1].includes('BUY по актуальному Bid, SELL по актуальному Ask'));
 assert(dialogs[1].includes('RECOVERY'));assert(dialogs[1].includes('Новый цикл автоматически не начнётся'));
 await click();assert.equal(calls.length,1,'Repeated pending click must not send');ctx.pending=null;
 for(const [key,value] of [['v3Fresh',false],['busy',true],['mode','live']]){
  const old=ctx[key];ctx[key]=value;await click();assert.equal(calls.length,1);ctx[key]=old;
 }
 for(const [key,value] of [['cycle_id',null],['time_block',{reason:'clock'}],['migration_block','legacy'],['read_only',true]]){
  const old=state[key];state[key]=value;await click();assert.equal(calls.length,1);state[key]=old;
 }
 state.feed.status='WAITING';await click();assert.equal(calls.length,1);state.feed.status='FRESH';
 state.paper.paused_gap=true;await click();assert.equal(calls.length,2);assert.equal(calls[1][0].gap_confirmed,true);
 assert(dialogs.at(-1).includes('Пропущенные тики и исполнения не восстанавливаются'));
 console.log('PAPER interruption UI: cancel, repeated click, scope, stale and gap confirmation passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
