'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'../web/basket-panel/panel.js'),'utf8');
const nodes={},$=id=>nodes[id]??={dataset:{},textContent:'',hidden:false,disabled:false};
let now=1000,calls=0,release=null;
const fresh=()=>({contract:'ticket_basket_v1',mode:'PAPER',paper:{command_quote:{available:true,bid:'100',ask:'101',timestamp:1804800000,age_seconds:0,stale:false,source:'LAST_VERIFIED_MT5'}},scope:'paper',revision:4,command_context_token:'a'.repeat(64),phase:'V3_WAIT_ENTRY',snapshot_freshness:{fresh:true,age_seconds:0,max_age_seconds:2},feed:{status:'FRESH',timestamp_semantics:'VERIFIED',age_seconds:0},action_results:{}});
const ctx=vm.createContext({$,performance:{now:()=>now},view:null,mode:'paper',busy:false,polling:false,pending:null,pendingScope:null,lastActionReceipt:null,preview:null,headers:()=>({}),apiPath:k=>'/api/paper/'+k,AbortSignal:{timeout:()=>null},render:()=>{},v3Render:()=>{},connectionLabel:()=>{},updateBotActivity:()=>{},feedUtcVerified:()=>ctx.view?.feed?.timestamp_semantics==='VERIFIED',clearPending:()=>{ctx.pending=null;ctx.pendingScope=null;},notice:()=>{},localStorage:{setItem:()=>{}},storageKey:x=>x,crypto:{randomUUID:()=> 'fixture-request'},fetch:async()=>{calls++;return new Promise(resolve=>{release=resolve;});}});
vm.runInContext(src.slice(src.indexOf(' let v3Fresh=false'),src.indexOf(' const v3Enabled=')),ctx);
vm.runInContext(src.slice(src.indexOf(' async function poll(){'),src.indexOf(' const defaultPrefs=')),ctx);
vm.runInContext(src.slice(src.indexOf(' async function send('),src.indexOf(' async function command(')),ctx);
const run=code=>vm.runInContext(code,ctx);
const accept=(v,start=now)=>{ctx.view=v;ctx.sample=v;ctx.started=start;run('acceptSnapshotFreshness(sample,started)');};
(async()=>{
 const q={available:true,bid:'100',ask:'101',timestamp:1804800000,age_seconds:600,stale:true,source:'LAST_VERIFIED_MT5'};
 const stale=()=>({...fresh(),feed:{status:'WAITING',timestamp_semantics:'UNVERIFIED',age_seconds:600},paper:{command_quote:{...q}}});
 accept(stale());assert.equal(run('quoteCurrent()'),false);assert.equal(run('paperServerReady'),true);
 assert.equal(run("commandQuoteReady({type:'v3_entry'})"),true);
 assert(run('paperQuoteText()').includes('СТАРАЯ'));assert(run('paperQuoteText()').includes('600.0 с'));
 const future=stale();future.paper.command_quote.age_seconds=-1;accept(future);assert(run('paperQuoteAvailable()'),'matching backend two-second skew tolerance');accept(stale());
 let posts=0;
 ctx.fetch=async (url,options)=>{if(url.endsWith('action')){posts++;assert.equal(JSON.parse(options.body).context_token,'a'.repeat(64));return{ok:true,json:async()=>({status:'APPLIED',paper_quote:q})};}return{ok:true,json:async()=>stale()};};
 for(const type of ['v3_entry','v3_link','v3_adjust_sl','own_entry_sl','paper_interrupt','paper_flatten']){accept(stale());await run('send({type:'+JSON.stringify(type)+'})');}
 assert.equal(posts,6);assert.equal(ctx.lastActionReceipt.paper_quote.age_seconds,600);
 // Backend verified quote, not source feed status, is the only price evidence.
 for(const change of [{available:false},{source:'FAKE'},{bid:null},{bid:''},{bid:'NaN'},{ask:'99'},{timestamp:1e30},{age_seconds:-3},{age_seconds:null}]){const v=stale();Object.assign(v.paper.command_quote,change);accept(v);assert.equal(run('paperQuoteAvailable()'),false);}
 let missing=stale();missing.paper.command_quote={available:false};accept(missing);
 ctx.fetch=async url=>{if(url.endsWith('action')){posts++;return{ok:true,json:async()=>({status:'APPLIED'})};}return{ok:true,json:async()=>missing};};
 const before=posts;await assert.rejects(run("send({type:'v3_entry'})"),/Нет последней проверенной/);assert.equal(posts,before);
 for(const type of ['cancel_initial','v3_cancel_entry','v3_cancel_link','v3_cancel_request','paper_costs']){accept(missing);await run('send({type:'+JSON.stringify(type)+'})');}
 assert.equal(posts,before+5);
 // Missing/stale server publication still forbids a priced action or cancellation.
 missing.snapshot_freshness.fresh=false;accept(missing);
 await assert.rejects(run("send({type:'v3_cancel_entry'})"),/Нет свежего снимка/);assert.equal(posts,before+5);
 // LIVE retains fresh quote guard despite PAPER metadata.
 ctx.mode='live';accept(stale());ctx.fetch=async()=>({ok:true,json:async()=>({...stale(),mode:'LIVE'})});
 await assert.rejects(run("send({type:'v3_entry'})"),/Нет свежей подтверждённой/);ctx.mode='paper';
 accept(stale());ctx.pending='unknown';await assert.rejects(run("send({type:'v3_entry'})"),/Повтор запрещён/);ctx.pending=null;
 ctx.busy=true;await assert.rejects(run("send({type:'v3_entry'})"),/Повтор запрещён/);ctx.busy=false;
 await assert.rejects(run("send({type:'v3_entry'},4,'other')"),/Контекст изменился/);
 console.log('PASS last verified PAPER quote, stale disclosure and receipt, no price fabrication, price-free cancellation, snapshot/busy/UNKNOWN/context guards and LIVE unchanged');
})().catch(e=>{console.error(e);process.exitCode=1;});
