'use strict';
// Actual-source UI controls and synthetic clock only. No HTTP, broker or trades.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.join(__dirname,'../web/basket-panel'),source=fs.readFileSync(path.join(root,'panel.js'),'utf8'),html=fs.readFileSync(path.join(root,'index.html'),'utf8');
const ids=[...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]);assert.equal(new Set(ids).size,ids.length);
for(const id of ['sync-mt5-time','mt5-display-clock','mt5-clock-status','mt5-time-mode'])assert(ids.includes(id));
assert(html.includes('Сверить часы MT5…'));assert(!ids.includes('use-mt5-raw-time'));assert(!ids.includes('resume'));assert(ids.includes('paper-resume'));assert(/id="paper-resume" hidden/.test(html));assert(source.includes("$('paper-resume').hidden=!view.paper?.paused_gap"));
const nodes={},$=id=>nodes[id]??={textContent:'',disabled:false},actions=[],dialogs=[],errors=[];let mono=1000,accept=true,onConfirm=null;
const base=()=>({scope:'live-fixture',revision:42,read_only:true,positions:[{id:'existing'}],orders:[],time_status:{market_time_mode:'fixed_offset_v1',can_switch_raw:false,switch_reason:'Открытые позиции блокируют переключение.',display_clock:{status:'UNALIGNED',broker_time:null,estimated_broker_time:null,elapsed_seconds:null,timestamp_semantics:'BROKER_RAW',verified_utc:false,source_connected:true}}});
const context=vm.createContext({$,view:base(),v3Fresh:true,busy:false,pending:null,performance:{now:()=>mono},Date:class extends Date{static now(){throw Error('Wall clock must not be read');}},confirm:message=>{dialogs.push(message);onConfirm?.();return accept;},send:async(action,revision,scope)=>actions.push({action,revision,scope}),error:message=>errors.push(message)});
vm.runInContext(source.slice(source.indexOf(' // MT5 clock controls are separate'),source.indexOf(' // Activity uses controller snapshots')),context);
const run=code=>vm.runInContext(code,context),render=()=>run('renderMt5TimeControls()'),observe=()=>run('observeMt5DisplayClock();renderMt5TimeControls()');
vm.runInContext(source.slice(source.indexOf(" $('paper-resume').onclick="),source.indexOf(' // Explicit PAPER-only settlement.')),context);
context.mode='paper';
(async()=>{
 observe();assert.equal(actions.length,0,'initial rendering never synchronizes or migrates automatically');assert.equal($('sync-mt5-time').disabled,false,'READ_ONLY and open positions do not block display sync');
 assert.equal($('mt5-display-clock').textContent,'Оценка времени MT5: —');
 accept=false;await $('sync-mt5-time').onclick();assert.equal(actions.length,0,'Cancel leaves display anchor unchanged');assert.equal(run('mt5DisplayAnchor'),null);
 accept=true;await $('sync-mt5-time').onclick();assert.equal(actions.length,1);assert.deepEqual(actions[0].action,{type:'command',text:'/sync_mt5_time'});assert.equal(actions[0].revision,42);assert.equal(actions[0].scope,'live-fixture');
 assert(dialogs.at(-1).includes('может быть задержанным'));assert(dialogs.at(-1).includes('торговый режим и часы Windows не меняются'));
 assert.equal(run('mt5DisplayAnchor'),null,'a receipt alone does not fabricate an aligned clock');
 const stamp=1804800000;context.view.time_status.display_clock={status:'ALIGNED',broker_time:stamp-86400,estimated_broker_time:stamp,elapsed_seconds:86400,timestamp_semantics:'BROKER_RAW',verified_utc:false,source_connected:true};observe();
 assert.equal($('mt5-display-clock').textContent,'Оценка времени MT5: '+new Date(stamp*1000).toISOString().replace('T',' ').slice(0,19));
 mono+=2500;render();assert($('mt5-display-clock').textContent.endsWith(new Date((stamp+2)*1000).toISOString().slice(11,19)),'display advances by monotonic seconds between polls');
 assert(!/UTC|[0-9]T|Z/.test($('mt5-display-clock').textContent));assert.equal(actions.length,1,'clock rendering has no action side effects');
 const before=JSON.stringify(context.view);mono+=500;render();assert.equal(JSON.stringify(context.view),before,'display cannot modify price/source/control data');
 for(const patch of [{status:'UNALIGNED'},{status:'DISCONNECTED'},{source_connected:false},{timestamp_semantics:'VERIFIED'},{verified_utc:true},{estimated_broker_time:null},{estimated_broker_time:Infinity}]){const saved=context.view.time_status.display_clock;context.view.time_status.display_clock={...saved,...patch};observe();assert.equal(run('mt5DisplayAnchor'),null);assert($('mt5-display-clock').textContent.endsWith('—'));context.view.time_status.display_clock=saved;}
 observe();context.v3Fresh=false;render();assert($('mt5-display-clock').textContent.endsWith('—'));assert.equal($('sync-mt5-time').disabled,false);const staleCount=actions.length;await $('sync-mt5-time').onclick();assert.equal(actions.length,staleCount+1);const count=actions.length;context.v3Fresh=true;
 for(const field of ['busy','pending']){context[field]=field==='busy'?true:'unknown-command';render();assert.equal($('sync-mt5-time').disabled,true);await $('sync-mt5-time').onclick();assert.equal(actions.length,count);context[field]=field==='busy'?false:null;}
 observe();mono=-1;render();assert($('mt5-display-clock').textContent.endsWith('—'),'invalid/backward monotonic clock cannot fabricate time');mono=10000;
 context.view.time_status.display_clock.status='UNALIGNED';observe();assert.equal(run('mt5DisplayAnchor'),null,'restart/reconnect invalidates displayed estimate');
 const migratedCount=actions.length;
 context.view.time_status.market_time_mode='fixed_offset_v1';onConfirm=()=>context.view.revision++;await $('sync-mt5-time').onclick();assert.equal(actions.length,migratedCount,'revision change during confirmation requires new confirmation');assert(errors.at(-1).includes('Контекст изменился'));onConfirm=null;
 context.view.scope='different';render();assert($('mt5-display-clock').textContent.endsWith('—'),'old clock does not cross controller scope');
 assert(source.includes("$('reset').onclick=()=>command('/reset_manual'"),'manual reconciliation remains a distinct original control');
 assert(!source.slice(source.indexOf(' // MT5 clock controls are separate'),source.indexOf(' // Activity uses controller snapshots')).includes('Date.now'));
 // Restart continuation remains separate and available only for a persisted pause.
 context.view=base();context.view.paper={paused_gap:false};let resumeBefore=actions.length;
 await $('paper-resume').onclick();assert.equal(actions.length,resumeBefore);
 context.view.paper.paused_gap=true;accept=false;await $('paper-resume').onclick();assert.equal(actions.length,resumeBefore);
 accept=true;await $('paper-resume').onclick();assert.equal(actions.length,resumeBefore+1);assert.equal(actions.at(-1).action.type,'paper_resume');
 console.log('PASS PAPER MT5 display clock controls: explicit confirmations, cancel/race/UNKNOWN/HTTP guards, READ_ONLY/open-position sync, monotonic display-only progression, restart/reconnect invalidation and zero real trades');
})().catch(e=>{console.error(e);process.exitCode=1;});
