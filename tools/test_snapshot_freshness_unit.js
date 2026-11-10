'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'../web/basket-panel/panel.js'),'utf8');
const nodes={},$=id=>nodes[id]??={dataset:{},textContent:'',hidden:false,disabled:false};
let now=1000,calls=0,release=null;
const fresh=()=>({contract:'ticket_basket_v1',mode:'PAPER',scope:'paper',revision:4,command_context_token:'a'.repeat(64),phase:'V3_WAIT_ENTRY',snapshot_freshness:{fresh:true,age_seconds:0,max_age_seconds:2},feed:{status:'FRESH',timestamp_semantics:'VERIFIED',age_seconds:0},action_results:{}});
const ctx=vm.createContext({$,performance:{now:()=>now},view:null,mode:'paper',busy:false,polling:false,pending:null,pendingScope:null,lastActionReceipt:null,preview:null,headers:()=>({}),apiPath:k=>'/api/paper/'+k,AbortSignal:{timeout:()=>null},render:()=>{},v3Render:()=>{},connectionLabel:()=>{},updateBotActivity:()=>{},feedUtcVerified:()=>ctx.view?.feed?.timestamp_semantics==='VERIFIED',clearPending:()=>{ctx.pending=null;ctx.pendingScope=null;},notice:()=>{},localStorage:{setItem:()=>{}},storageKey:x=>x,crypto:{randomUUID:()=> 'fixture-request'},fetch:async()=>{calls++;return new Promise(resolve=>{release=resolve;});}});
vm.runInContext(src.slice(src.indexOf(' let v3Fresh=false'),src.indexOf(' const v3Enabled=')),ctx);
vm.runInContext(src.slice(src.indexOf(' async function poll(){'),src.indexOf(' const defaultPrefs=')),ctx);
vm.runInContext(src.slice(src.indexOf(' async function send('),src.indexOf(' async function command(')),ctx);
const run=code=>vm.runInContext(code,ctx);
const accept=(v,start=now)=>{ctx.view=v;ctx.sample=v;ctx.started=start;run('acceptSnapshotFreshness(sample,started)');};
(async()=>{
 assert.equal(run('snapshotCurrent()'),false);
 accept(fresh());assert(run('snapshotCurrent()'));assert(run('quoteCurrent()'));
 now=3001;assert.equal(run('snapshotCurrent()'),false,'click guard expires without relying on timer');
 run('expireSnapshot()');assert.equal(run('v3Fresh'),false);assert.equal(ctx.view.feed.status,'WAITING');
 now=4000;let v=fresh();v.snapshot_freshness.age_seconds=1.5;accept(v,3000);assert.equal(run('snapshotCurrent()'),false,'round trip consumes server remaining lifetime');
 for(const meta of [undefined,{fresh:true,age_seconds:null,max_age_seconds:2},{fresh:true,age_seconds:-1,max_age_seconds:2},{fresh:true,age_seconds:Infinity,max_age_seconds:2},{fresh:true,age_seconds:0,max_age_seconds:999},{fresh:false,age_seconds:0,max_age_seconds:2}]){v=fresh();v.snapshot_freshness=meta;accept(v);assert.equal(run('snapshotCurrent()'),false);}
 v=fresh();v.feed.age_seconds=4.9;accept(v);now+=101;run('expireSnapshot()');assert(run('snapshotCurrent()'));assert.equal(run('quoteCurrent()'),false);assert.equal(ctx.view.feed.status,'WAITING','quote expires independently');
 v=fresh();v.feed.status='WAITING';accept(v);assert.equal(run('quoteCurrent()'),false,'do not upgrade source WAITING');
 v=fresh();v.feed.timestamp_semantics='UNVERIFIED';accept(v);assert.equal(run('quoteCurrent()'),false);
 ctx.busy=false;ctx.polling=false;const p=run('poll()');assert.equal(calls,1);await run('poll()');assert.equal(calls,1,'no overlapping request');now+=2100;release({ok:true,json:async()=>fresh()});await p;assert.equal(run('snapshotCurrent()'),false,'slow success is stale');
 ctx.busy=true;await run('poll()');assert.equal(calls,1,'busy send prevents poll overlap');ctx.busy=false;
 ctx.fetch=async()=>{calls++;const v=fresh();v.snapshot_freshness={fresh:false,age_seconds:3,max_age_seconds:2};return{ok:true,json:async()=>v};};
 await run('poll()');assert.equal(run('v3Fresh'),false,'HTTP200 cached snapshot does not revive freshness');
 const before=calls;await assert.rejects(run("send({type:'v3_entry'})"),/Нет свежего снимка/);assert.equal(calls,before+1,'stale guard refreshes state only, sends no trade');
 // Safe emergency stop remains available, even while publication is stale.
 ctx.fetch=async url=>{calls++;return{ok:true,json:async()=>url.endsWith('action')?{status:'APPLIED'}:fresh()};};
 await run("send({type:'command',text:'/stop'})");assert.equal(calls,before+3);
 // PAPER accepts a slow but server-fresh response, never fabricates local freshness.
 accept(fresh());run('v3Fresh=false');let slowPosts=0;
 ctx.fetch=async url=>{if(url.endsWith('action')){slowPosts++;return{ok:true,json:async()=>({status:'APPLIED'})};}now+=2100;return{ok:true,json:async()=>fresh()};};
 await run("send({type:'v3_settings',entry_distance:'1'})");
 assert.equal(slowPosts,1);assert.equal(run('quoteCurrent()'),false);assert.equal(run('paperServerReady'),true);
 // LIVE does not receive the PAPER-only relaxation.
 ctx.mode='live';accept(fresh());run('v3Fresh=false');slowPosts=0;
 await assert.rejects(run("send({type:'v3_settings'})"),/Нет свежего/);assert.equal(slowPosts,0);ctx.mode='paper';
 // Modal/network gaps: one safe read can recover the same reviewed state.
 accept(fresh());run('v3Fresh=false');let posts=0,gets=0;
 ctx.fetch=async (url,options)=>{if(url.endsWith('action')){posts++;assert.equal(JSON.parse(options.body).context_token,'a'.repeat(64));return{ok:true,json:async()=>({status:'APPLIED'})};}gets++;return{ok:true,json:async()=>fresh()};};
 await run("send({type:'v3_settings',entry_distance:'1'})");
 assert.equal(posts,1);assert.equal(gets,2,'preflight GET plus receipt reconciliation');
 // A changed revision must not silently rebase what was confirmed.
 accept(fresh());run('v3Fresh=false');posts=0;
 ctx.fetch=async url=>{if(url.endsWith('action')){posts++;throw Error('unexpected POST');}return{ok:true,json:async()=>({...fresh(),revision:5})};};
 await assert.rejects(run("send({type:'v3_settings'})"),/изменились/);assert.equal(posts,0);
 // Same revision with a changed server context token also requires reconfirmation.
 accept(fresh());run('v3Fresh=false');posts=0;
 ctx.fetch=async url=>{if(url.endsWith('action')){posts++;throw Error('unexpected POST');}return{ok:true,json:async()=>({...fresh(),command_context_token:'b'.repeat(64)})};};
 await assert.rejects(run("send({type:'v3_settings'})"),/изменились/);assert.equal(posts,0);
 // Offline GET cannot authorize a POST; an unknown POST is never retried.
 accept(fresh());run('v3Fresh=false');posts=0;
 ctx.fetch=async url=>{if(url.endsWith('action'))posts++;throw Error('offline');};
 await assert.rejects(run("send({type:'v3_settings'})"),/Нет свежего/);assert.equal(posts,0);
 accept(fresh());posts=0;
 ctx.fetch=async url=>{if(url.endsWith('action')){posts++;throw Error('response lost');}return{ok:true,json:async()=>fresh()};};
 await assert.rejects(run("send({type:'v3_settings'})"),/response lost/);assert.equal(posts,1);assert(ctx.pending);
 await assert.rejects(run("send({type:'v3_settings'})"),/Повтор запрещён/);assert.equal(posts,1);
 assert(src.includes('setInterval(refreshState,500)'),'half-second cadence');
 console.log('PASS 500ms nonoverlap, monotonic publication/quote expiry, malformed/stale200 fail-closed, slow responses, no stale trade and emergency stop');
})().catch(e=>{console.error(e);process.exitCode=1;});
