'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'../web/basket-panel/panel.js'),'utf8');
const nodes={},$=id=>nodes[id]??={dataset:{},textContent:'',hidden:false,disabled:false};
let now=1000,posts=0,gets=0,next=null,fail=false,lastError='',accept=true;
const fresh=()=>({contract:'ticket_basket_v1',mode:'PAPER',scope:'paper',revision:4,cycle_id:'cycle-a',phase:'WAIT_START',settings:{INITIAL_SIZE:'0.1'},positions:[],orders:[],snapshot_freshness:{fresh:true,age_seconds:0,max_age_seconds:2},feed:{status:'FRESH',timestamp_semantics:'VERIFIED',age_seconds:0},action_results:{}});
const ctx=vm.createContext({$,performance:{now:()=>now},view:fresh(),mode:'paper',busy:false,polling:false,pollTask:null,confirmingCommand:false,pending:null,pendingScope:null,lastActionReceipt:null,preview:null,headers:()=>({}),apiPath:k=>'/api/paper/'+k,AbortSignal:{timeout:()=>null},render:()=>{},v3Render:()=>{},connectionLabel:()=>{},updateBotActivity:()=>{},feedUtcVerified:()=>ctx.view?.feed?.timestamp_semantics==='VERIFIED',clearPending:()=>{ctx.pending=null;ctx.pendingScope=null;},notice:()=>{},localStorage:{setItem:()=>{}},storageKey:x=>x,crypto:{randomUUID:()=> 'fixture-request'},error:e=>{lastError=e;},confirm:()=>{now+=5000;return accept;},fetch:async url=>{if(url.endsWith('action')){posts++;return{ok:true,json:async()=>({status:'APPLIED'})};}gets++;if(fail)throw Error('network');return{ok:true,json:async()=>next??fresh()};}});
vm.runInContext(src.slice(src.indexOf(' let v3Fresh=false'),src.indexOf(' const v3Enabled=')),ctx);
vm.runInContext(src.slice(src.indexOf(' async function poll(){'),src.indexOf(' const defaultPrefs=')),ctx);
vm.runInContext(src.slice(src.indexOf(' async function send('),src.indexOf(" const keyboard=")),ctx);
const run=code=>vm.runInContext(code,ctx);
const reset=()=>{ctx.view=fresh();ctx.pending=null;ctx.busy=false;ctx.polling=false;next=null;fail=false;accept=true;lastError='';run('acceptSnapshotFreshness(view,performance.now())');};
(async()=>{
 reset();await run("command('/start','start?')");assert.equal(posts,1);assert.equal(gets,2,'fresh GET before POST plus receipt poll');
 for(const change of [v=>v.revision++,v=>v.scope='other',v=>v.cycle_id='other',v=>v.settings.INITIAL_SIZE='2',v=>v.positions.push({id:'p'}),v=>v.phase='MANUAL']){reset();next=fresh();change(next);const n=posts;await run("command('/start','start?')");assert.equal(posts,n);assert(lastError.includes('подтвердите команду заново'));}
 for(const collection of ['positions','orders'])for(const field of ['size','price','status','command_id']){reset();ctx.view[collection]=[{id:'obligation',size:'0.1',price:'3000',status:'PENDING',command_id:'command-a'}];next=JSON.parse(JSON.stringify(ctx.view));next[collection][0][field]='changed';const n=posts;await run("command('/start','start?')");assert.equal(posts,n,collection+' '+field+' change blocks send');assert(lastError.includes('подтвердите команду заново'));}
 reset();fail=true;await run("command('/start','start?')");assert.equal(posts,1);assert(lastError.includes('Нет свежего'));
 reset();next=fresh();next.snapshot_freshness.fresh=false;await run("command('/start','start?')");assert.equal(posts,1);
 reset();accept=false;const n=gets;await run("command('/start','start?')");assert.equal(gets,n);assert.equal(posts,1);
 reset();ctx.pending='unknown';await run("command('/start','start?')");assert.equal(posts,1);assert(lastError.includes('Повтор запрещён'));
 reset();fail=true;await run("command('/stop','stop?')");assert.equal(posts,2,'stop does not require GET or fresh quote');
 reset();let release;let first=true;ctx.fetch=async url=>{if(url.endsWith('action')){posts++;return{ok:true,json:async()=>({status:'APPLIED'})};}gets++;if(first){first=false;return new Promise(resolve=>{release=resolve;});}return{ok:true,json:async()=>fresh()};};
 const oldPoll=run('poll()'),beforeOverlap=gets,starting=run("command('/start','start?')");assert.equal(gets,beforeOverlap,'wait for active request, do not overlap');release({ok:true,json:async()=>fresh()});await oldPoll;await starting;assert.equal(posts,3);assert.equal(gets,beforeOverlap+2,'new GET after old response, then receipt poll');
 reset();ctx.fetch=async url=>{if(url.endsWith('action')){posts++;throw Error('uncertain outcome');}gets++;return{ok:true,json:async()=>fresh()};};await run("command('/start','start?')");assert.equal(posts,4);assert.equal(ctx.pending,'fixture-request');await run("command('/start','start?')");assert.equal(posts,4,'UNKNOWN never repeats');
 console.log('PASS confirmed START refresh, context/revision/parameters/obligations changes, cancel, failed/stale GET, UNKNOWN and stop');
})().catch(e=>{console.error(e);process.exitCode=1;});
