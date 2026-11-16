'use strict';
// Full panel browser check with fixture-only HTTP, no broker/network calls.
const {chromium}=require('playwright'),fs=require('fs'),path=require('path'),assert=require('assert'),vm=require('vm');
const root=path.resolve(__dirname,'../web/basket-panel');
const old=fs.readFileSync(path.join(__dirname,'test_recovery_profile_ui.js'),'utf8');
const fixture=old.slice(old.indexOf('const root='),old.indexOf('async function assertQuoteGutter'));
const ctx=vm.createContext({require,__dirname,path});vm.runInContext(fixture+';globalThis.fixture=state;',ctx);
const state=ctx.fixture;state.mode='PAPER';state.scope='paper-ui';state.recovery_mode='V3';state.feed.timestamp_semantics='BROKER_RAW';state.feed.status='OBSERVED';
state.candle_recovery={variant:'sl-overlap-recovery-v1',thresholds:['3.5','6.5','10','15','21','28','36','50'].map(upper=>({upper,profile_id:'six'})),settings:{},volume:'0.06'};
(async()=>{const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox']});try{for(const width of [320,390,1280]){
 const page=await browser.newPage({viewport:{width,height:844}}),errors=[],actions=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',route=>{const u=new URL(route.request().url());if(u.pathname.endsWith('/state'))return route.fulfill({json:state});if(u.pathname.endsWith('/action')){const action=route.request().postDataJSON().action;actions.push(action);state.candle_recovery.thresholds=action.thresholds;return route.fulfill({json:{status:'APPLIED'}});}const name=u.pathname==='/'?'index.html':path.basename(u.pathname);if(['index.html','panel.js','panel.css'].includes(name))return route.fulfill({body:fs.readFileSync(path.join(root,name),'utf8').replace('__PANEL_ROLE__','PAPER'),contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});return route.abort();});
 await page.goto('http://localhost:8766/');await page.locator('#workspace').waitFor({state:'visible'});await page.locator('#tab-settings').click();await page.locator('#overlap-threshold-settings>summary').click();
 assert.equal(await page.locator('.overlap-threshold-row').count(),8);const first=page.locator('.overlap-threshold-row input').first();await first.fill('4.1');await page.locator('.overlap-threshold-row select').first().selectOption('one');await page.waitForTimeout(1200);assert.equal(await first.inputValue(),'4.1');
 const geometry=await page.locator('.overlap-threshold-row').first().evaluate(e=>({left:e.getBoundingClientRect().left,right:e.getBoundingClientRect().right,width:innerWidth}));assert(geometry.left>=0&&geometry.right<=geometry.width,'mobile row fits viewport');
 page.once('dialog',d=>d.dismiss());await page.locator('#overlap-threshold-reload').click();assert.equal(await first.inputValue(),'4.1');
 page.once('dialog',d=>d.dismiss());await page.locator('#overlap-threshold-save').click();assert.equal(actions.length,0,'cancel never sends');
 page.once('dialog',d=>d.accept());await page.locator('#overlap-threshold-save').click();await page.waitForTimeout(100);assert.equal(actions.length,1);assert.equal(actions[0].thresholds[0].profile_id,'one');assert.equal(actions[0].cycle_id,'c1');assert.equal(actions[0].thresholds[0].upper,'4.1');
 assert.deepEqual(errors,[]);await page.close();
 }console.log('Overlap thresholds browser: mobile/desktop fit, polling draft preservation, cancel, active-cycle mapping save passed');}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1);});
