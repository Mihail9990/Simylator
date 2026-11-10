'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'../web/basket-panel/panel.js'),'utf8');
let now=0,nextId=0;const timers=new Map(),nodes={};
const $=id=>nodes[id]??={textContent:'',dataset:{},disabled:false};
const ctx=vm.createContext({$,performance:{now:()=>now},setTimeout:(fn,ms)=>{const id=++nextId;timers.set(id,{fn,at:now+ms});return id;},clearTimeout:id=>timers.delete(id),mode:'paper',view:null,busy:false,pending:null,pendingScope:null,lastActionReceipt:null,feedUtcVerified:()=>ctx.view?.feed?.timestamp_semantics==='VERIFIED',friendlyReason:x=>x,timeSafetyText:()=> 'TIME BLOCK',v3Render:()=>{},connectionLabel:()=>{},poll:()=>{}});
vm.runInContext(src.slice(src.indexOf(' // Activity uses controller snapshots'),src.indexOf(' // V3 reuses scoped receipts')),ctx);
vm.runInContext(src.slice(src.indexOf(' let v3Fresh=false'),src.indexOf(' const v3Enabled=')),ctx);
const run=s=>vm.runInContext(s,ctx),update=()=>run('updateBotActivity()'),warning=()=>$('bot-activity-warning').textContent;
const sample=(status='FRESH',age=0)=>({scope:'paper',phase:'INITIAL_PENDING',orders:[{}],positions:[],snapshot_freshness:{fresh:true,age_seconds:0,max_age_seconds:2},feed:{status,timestamp_semantics:'VERIFIED',age_seconds:age,tick_msc:123,bid:100,ask:101}});
const accept=v=>{ctx.view=v;run('acceptSnapshotFreshness(view,performance.now())');update();};
const advance=ms=>{now+=ms;for(const [id,t] of [...timers])if(t.at<=now){timers.delete(id);t.fn();}};
// No generic red warning, even for a prolonged gap. Guards remain fail-closed.
accept(sample());assert.equal(warning(),'');
for(const status of ['WAITING','FRESH']){
 accept(sample(status));
 if(status==='FRESH'){advance(2100);run('expireSnapshot()');}
 assert.equal(run('quoteCurrent()'),false,'guard blocks immediately');
 assert.equal(warning(),'');
 assert(/Команды недоступны|команды проверяет сервер/.test($('bot-activity-state').textContent));
 advance(30000);update();assert.equal(warning(),'');
}
// Recovery and unchanged prices are not fabricated evidence of freshness.
for(let i=0;i<8;i++){accept(sample('FRESH',0.5));advance(500);assert.equal(warning(),'');}
accept(sample('FRESH',4.9));advance(101);run('expireSnapshot()');
assert.equal(run('quoteCurrent()'),false);assert.equal(warning(),'');
for(const bad of [undefined,NaN,Infinity,6]){
 const v=sample();v.feed.age_seconds=bad;accept(v);
 assert.equal(run('quoteCurrent()'),false);assert.equal(warning(),'');
}
let unverified=sample('WAITING');unverified.feed.timestamp_semantics='UNVERIFIED';
accept(unverified);assert.equal(run('quoteCurrent()'),false);assert.equal(warning(),'');
assert.equal(timers.size,0,'no delayed generic notice');
// Time uncertainty, manual/guard pauses and unknown command outcomes are immediate.
let v=sample('WAITING');v.feed.timestamp_semantics='UNVERIFIED';v.time_block={};v.manual_reason='manual review';v.migration_block='migration';v.phase='SAFE_PAUSE';accept(v);assert(warning().includes('TIME BLOCK'));assert(warning().includes('manual review'));assert(warning().includes('migration'));assert($('bot-activity-state').textContent.includes('требуется проверка'));
ctx.pending='id';ctx.pendingScope='paper';ctx.view.action_results={id:{status:'UNKNOWN'}};update();assert($('bot-activity-action').textContent.includes('неизвестен'));assert($('bot-activity-action').textContent.includes('Повтор запрещён'));
console.log('PASS no generic notice for short/prolonged gaps, immediate stale guards, recovery, invalid age, time/pause/UNKNOWN warnings');
