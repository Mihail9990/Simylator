'use strict';
// Fixture-only full panel test; never connects to a broker.
const {chromium}=require('playwright'),fs=require('fs'),path=require('path'),assert=require('assert'),vm=require('vm');
const root=path.resolve(__dirname,'../web/basket-panel');
const fixtureSource=fs.readFileSync(path.join(__dirname,'test_recovery_profile_ui.js'),'utf8');
const fixture=fixtureSource.slice(fixtureSource.indexOf('const root='),fixtureSource.indexOf('async function assertQuoteGutter'));
const ctx=vm.createContext({require,__dirname,path});vm.runInContext(fixture+';globalThis.fixture=state;',ctx);
(async()=>{const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox']});try{for(const width of [320,390,1280]){
 const state=JSON.parse(JSON.stringify(ctx.fixture));state.mode='PAPER';state.scope='paper-reset-fixture';state.recovery_mode='V3';state.candle_recovery={variant:'sl-overlap-recovery-v1'};state.metrics={currency:'UST',recovery_total:'3.2',all_closed_net:'-4.1',history_complete:true,total_reset_available:true};state.position_history=[];
 const page=await browser.newPage({viewport:{width,height:844}}),errors=[],actions=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',route=>{const u=new URL(route.request().url());if(u.pathname.endsWith('/state'))return route.fulfill({json:state});if(u.pathname.endsWith('/action')){const request=route.request().postDataJSON();actions.push(request.action);state.action_results[request.id]={status:'APPLIED',revision:state.revision+1};state.revision++;state.metrics.all_closed_net='0';state.metrics.total_reset_at=100;return route.fulfill({json:{status:'APPLIED'}});}const name=u.pathname==='/'?'index.html':path.basename(u.pathname);if(['index.html','panel.js','panel.css'].includes(name))return route.fulfill({body:fs.readFileSync(path.join(root,name),'utf8'),contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});return route.abort();});
 await page.goto('http://localhost:8766/');await page.locator('#workspace').waitFor({state:'visible'});const button=page.locator('#paper-reset-total');await button.waitFor({state:'visible'});assert(await button.isEnabled());
 const box=await button.boundingBox();assert(box&&box.x>=0&&box.x+box.width<=width,'reset button fits viewport');
 page.once('dialog',d=>d.dismiss());await button.click();assert.equal(actions.length,0);
 page.once('dialog',d=>d.accept());await button.click();await page.waitForFunction(()=>document.querySelector('#activity-total-net').textContent==='0 UST');assert.deepEqual(actions,[{type:'paper_reset_total',confirmed:true}]);assert.equal(await page.locator('#activity-recovery').textContent(),'-3.2 UST');assert((await page.locator('#total-reset-note').textContent()).includes('UTC'));
 assert.deepEqual(errors,[]);await page.close();
 }console.log('PASS total-reset browser: mobile/desktop geometry, cancel, single confirmed request, total zero, unchanged RECOVERY and reset timestamp');}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1);});
