'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.join(__dirname,'../web/basket-panel'),src=fs.readFileSync(path.join(root,'panel.js'),'utf8'),html=fs.readFileSync(path.join(root,'index.html'),'utf8');
const block=src.slice(src.indexOf(' // Overlap PAPER reset has its own request lane:'),src.indexOf(' const defaultPrefs='));
const handler=src.split('\n').find(line=>line.includes("$('reset').onclick="));
const init=src.slice(src.indexOf(' let cycleResetDeliveries='),src.indexOf(' const names='));
const saved=new Map();let sequence=0;
const fresh=scope=>({scope,revision:1,command_context_token:'stale-token',candle_recovery:{variant:'sl-overlap-recovery-v1'},manual_reason:'MANUAL',read_only:true,migration_block:'old block',time_block:{reason:'old time'},paper:{capture_failure:'QUOTE_CAPTURE_OVERFLOW'},positions:[{id:'p'}],orders:[{id:'o'}],metrics:{recovery_total:'3',all_closed_net:'-2'}});
function fixture(scope='paper-A'){
 const nodes={},$=id=>nodes[id]??={hidden:false,disabled:false,textContent:''};
 const ctx=vm.createContext({$,mode:'paper',view:fresh(scope),busy:true,pending:'earlier-unknown',preview:{editing:true},localStorage:{getItem:k=>saved.get(k)??null,setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},storageKey:k=>k,crypto:{randomUUID:()=>`reset-${++sequence}`},apiPath:k=>'/api/paper/'+k,AbortSignal:{timeout:()=>null},poll:async()=>{},cancelGesture:()=>{},draw:()=>{},confirm:()=>{throw Error('no extra confirmation');},send:()=>{throw Error('must bypass ordinary send');},command:()=>{ctx.legacyCalled=true;}});
 vm.runInContext(block+init+handler,ctx);const run=s=>vm.runInContext(s,ctx);run('renderCycleReset()');return{ctx,run,$,click:()=>$('reset').onclick()};
}
(async()=>{
 let f=fixture(),{ctx,run,$,click}=f,calls=[],release;
 ctx.fetch=async(url,options)=>{calls.push({url,options});return new Promise(r=>release=r);};
 assert.equal($('reset').disabled,false,'busy/manual/readonly/old quote/preview do not block reset');const before=JSON.stringify(ctx.view),first=click();assert.equal(calls.length,1);await click();assert.equal(calls.length,1,'one concurrent delivery only');
 const body=calls[0].options.body,request=JSON.parse(body);assert.deepEqual(request,{id:'reset-1',revision:1,action:{type:'paper_reset_cycle'}});assert.equal(calls[0].options.headers['X-Controller-Scope'],'paper-A');assert.equal(ctx.pending,'earlier-unknown');assert.equal(ctx.busy,true);assert.equal(JSON.stringify(ctx.view),before,'no optimistic state changes');assert.deepEqual(JSON.parse(JSON.stringify(ctx.preview)),{editing:true});
 release({ok:true,json:async()=>({status:'QUEUED'})});await first;run('renderCycleReset()');assert.equal($('reset').disabled,false);assert.equal($('reset').textContent,'Повторить доставку сброса');
 ctx.view.revision=99;const retry=click();assert.equal(calls[1].options.body,body);release({ok:true,json:async()=>({status:'UNKNOWN'})});await retry;assert.equal(sequence,1);assert(run('cycleResetMessage').includes('неизвестен'));
 run("reconcileCycleReset({scope:'other',action_results:{'reset-1':{status:'APPLIED'}}})");assert.equal(run('cycleResetPending'),'reset-1');
 // Lost/uncertain A remains preserved while B may make a new independent reset.
 ctx.view=fresh('paper-B');run('renderCycleReset()');assert.equal($('reset').disabled,false);assert($('reset').textContent.includes('новый цикл'));const bsend=click();const bbody=calls[2].options.body;assert.notEqual(JSON.parse(bbody).id,request.id);assert.equal(calls[2].options.headers['X-Controller-Scope'],'paper-B');release({ok:true,json:async()=>({status:'QUEUED'})});await bsend;
 assert.equal(run('cycleResetDeliveries.size'),2);ctx.view=fresh('paper-A');run('renderCycleReset()');assert.equal(run('cycleResetDelivery.body'),body);assert.equal(run('cycleResetPending'),'reset-1');
 // A full reload recovers both exact envelopes; no new id/revision is invented.
 f=fixture('paper-A');({ctx,run,$,click}=f);assert.equal(run('cycleResetDelivery.body'),body);assert.equal(run('cycleResetDeliveries.size'),2);ctx.view.revision=999;
 for(const status of [400,503,403,429,409,401]){ctx.fetch=async(_url,options)=>{assert.equal(options.body,body);assert.equal(options.headers['X-Controller-Scope'],'paper-A');return{ok:false,status};};await click();assert.equal(run('cycleResetPending'),'reset-1');assert.equal(run('cycleResetDeliveries.size'),2);if([401,403].includes(status))assert.equal($('workspace').hidden,true);$('workspace').hidden=false;}
 ctx.fetch=async(_url,options)=>{assert.equal(options.body,body,'relogin retains exact body');return{ok:true,json:async()=>({status:'APPLIED'})};};await click();assert.equal(run('cycleResetPending'),null);assert.equal(run('cycleResetDeliveries.size'),1,'A receipt does not discard B');assert.equal(sequence,2);assert.equal(ctx.preview,null,'only verified reset clears obsolete preview');assert.equal(ctx.pending,'earlier-unknown');
 ctx.view=fresh('paper-B');run('renderCycleReset()');assert.equal(run('cycleResetDelivery.body'),bbody);run("reconcileCycleReset({scope:'paper-B',action_results:{'reset-2':{status:'REJECTED',reason:'No price'}}})");assert.equal(run('cycleResetPending'),null);assert.equal(run('cycleResetMessage'),'No price');assert.equal(run('cycleResetDeliveries.size'),0);
 // Failure before any server receipt still offers exact retransmission.
 let lostBody;ctx.fetch=async(_url,options)=>{lostBody=options.body;throw Error('lost before server');};await click();const lostId=run('cycleResetPending');assert(lostId);assert(run('cycleResetMessage').includes('Можно повторить доставку'));ctx.view.revision=2000;
 let effects=0;ctx.fetch=async(_url,options)=>{assert.equal(options.body,lostBody);effects++;return{ok:true,json:async()=>({status:'APPLIED'})};};await click();assert.equal(effects,1);assert.equal(run('cycleResetPending'),null);
 // Lost response AFTER application also reuses the idempotency key.
 let serverBody=null,serverEffects=0;ctx.fetch=async(_url,options)=>{if(!serverBody){serverBody=options.body;serverEffects++;throw Error('response lost after APPLIED');}assert.equal(options.body,serverBody);return{ok:true,json:async()=>({status:'APPLIED'})};};await click();await click();assert.equal(serverEffects,1);assert.equal(run('cycleResetPending'),null);
 // Scope switch while POST A is in flight must not settle B or clear B preview.
 ctx.view=fresh('paper-A');ctx.fetch=async(_url,options)=>new Promise(r=>release=r);const late=click();ctx.view=fresh('paper-B');ctx.preview={newScopeEdit:true};release({ok:true,json:async()=>({status:'APPLIED'})});await late;assert.deepEqual(JSON.parse(JSON.stringify(ctx.preview)),{newScopeEdit:true});assert.equal(run('cycleResetScope'),'paper-B');
 ctx.mode='live';run('renderCycleReset()');assert.equal($('paper-reset-cycle-note').hidden,true);await click();assert.equal(ctx.legacyCalled,true);ctx.mode='paper';ctx.legacyCalled=false;ctx.view.candle_recovery.variant='ordinary';await click();assert.equal(ctx.legacyCalled,true);
 ctx.view=fresh('paper-B');ctx.view.paper.last_reset={closed_positions:['p'],cancelled_orders:['o','o2'],written_off_recovery:'3.9',currency:'UST',automatic_restart:true,paper_quote:{bid:'4000.1',ask:'4000.2',timestamp:100,timestamp_semantics:'BROKER_RAW'}};run('renderCycleReset()');assert($('paper-reset-cycle-last').textContent.includes('закрыто позиций 1, отменено ордеров 2'));assert($('paper-reset-cycle-last').textContent.includes('1970-01-01 00:01:40 (время брокера)'));
 ctx.view=null;assert.equal(run('cycleResetBlocked()'),true);assert(html.includes('История и фактический P/L сохраняются'));assert(src.includes('view=v;reconcileCycleReset(v);'));
 console.log('PASS one-click overlap reset: exact-body retry/reload, all HTTP errors preserve envelope, per-scope A→B→A isolation, lost-before/after-apply, independent pending lane, no optimistic state, metadata and LIVE fallback');
})().catch(e=>{console.error(e);process.exitCode=1;});
