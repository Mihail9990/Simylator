'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const base=path.join(__dirname,'../web/basket-panel'),src=fs.readFileSync(path.join(base,'panel.js'),'utf8'),html=fs.readFileSync(path.join(base,'index.html'),'utf8');
const nodes={},$=id=>nodes[id]??={hidden:false,disabled:false,textContent:''};
let sends=[],prompts=[],errors=[];
const fresh=()=>({scope:'paper-one',revision:4,candle_recovery:{variant:'sl-overlap-recovery-v1'},metrics:{all_closed_net:'-4.28',history_complete:true,total_reset_available:true},manual_reason:'RECOVERY_MANUAL',positions:[{id:'p'}],orders:[{id:'o'}]});
const ctx=vm.createContext({$,view:fresh(),mode:'paper',busy:false,pending:null,confirm:text=>{prompts.push(text);return false;},send:async(...args)=>{sends.push(args);},error:text=>errors.push(text)});
vm.runInContext(src.slice(src.indexOf(' // Display-only total reset:'),src.indexOf(' function render(){')),ctx);
vm.runInContext(src.slice(src.indexOf(' const paperPriceFree='),src.indexOf(' function paperQuoteAvailable')),ctx);
const run=s=>vm.runInContext(s,ctx),click=()=>$('paper-reset-total').onclick();
(async()=>{
 run('renderTotalReset()');assert.equal($('paper-reset-total').hidden,false);assert.equal($('paper-reset-total').disabled,false,'manual recovery/open positions do not block display-only reset');
 assert.equal(run("paperPriceFree({type:'paper_reset_total',confirmed:true})"),true);
 await click();assert.equal(sends.length,0,'cancel does not send');assert(prompts[0].includes('RECOVERY, история сделок, открытые позиции и ордера не изменятся'));
 ctx.confirm=()=>true;const before=JSON.stringify(ctx.view);await click();assert.equal(sends.length,1);assert.deepEqual(JSON.parse(JSON.stringify(sends[0])),[{type:'paper_reset_total',confirmed:true},4,'paper-one']);assert.equal(JSON.stringify(ctx.view),before,'no optimistic accounting/state mutation');
 for(const [key,value] of [['busy',true],['pending','unknown-request']]){ctx[key]=value;run('renderTotalReset()');assert.equal($('paper-reset-total').disabled,true);await click();assert.equal(sends.length,1);ctx[key]=key==='busy'?false:null;}
 for(const change of [v=>v.read_only=true,v=>v.migration_block='blocked',v=>v.metrics.history_complete=false,v=>v.metrics.total_reset_available=false,v=>v.metrics.all_closed_net=null,v=>v.metrics.all_closed_net='NaN']){ctx.view=fresh();change(ctx.view);run('renderTotalReset()');assert.equal($('paper-reset-total').disabled,true);await click();assert.equal(sends.length,1);}
 ctx.view=fresh();ctx.mode='live';run('renderTotalReset()');assert.equal($('paper-reset-total').hidden,true);await click();assert.equal(sends.length,1);
 ctx.mode='paper';ctx.view.candle_recovery.variant='ordinary';run('renderTotalReset()');assert.equal($('paper-reset-total').hidden,true);await click();assert.equal(sends.length,1);
 ctx.view=fresh();ctx.confirm=()=>{ctx.view.revision++;return true;};await click();assert.equal(sends.length,1);assert(errors.at(-1).includes('Состояние изменилось'));
 ctx.view=fresh();ctx.confirm=()=>{ctx.view.scope='other';return true;};await click();assert.equal(sends.length,1);
 ctx.view=fresh();ctx.confirm=()=>true;ctx.send=async()=>{throw Error('UNKNOWN result');};await click();assert.equal(errors.at(-1),'UNKNOWN result');
 ctx.view=fresh();ctx.view.metrics.total_reset_at=100;run('renderTotalReset()');assert.equal($('total-reset-note').hidden,false);assert($('total-reset-note').textContent.includes('1970-01-01 00:01:40 UTC'));
 ctx.view.metrics.total_reset_at=null;run('renderTotalReset()');assert.equal($('total-reset-note').hidden,true);
 ctx.view.metrics.all_closed_net='0';run('renderTotalReset()');assert.equal($('paper-reset-total').disabled,false);
 assert(html.includes('id="paper-reset-total" type="button" hidden'));assert(html.includes('id="total-reset-note" hidden'));
 console.log('PASS display-only total reset: confirmation/cancel, price-free action, manual/open positions, read-only/incomplete/busy/UNKNOWN guards, scope/revision race, no optimistic state mutation and reset timestamp');
})().catch(e=>{console.error(e);process.exitCode=1;});
