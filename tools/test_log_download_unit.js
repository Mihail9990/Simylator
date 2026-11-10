'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'../web/basket-panel/panel.js'),'utf8');
const nodes={'log-scope':{value:'paper'},'logs-download':{},'logs-status':{}},$=id=>nodes[id];
let saved=0,mime='text/plain; charset=utf-8',status=200,content='Manifest\nРусский текст',size=100,filename=null;
const ctx=vm.createContext({$,fetch:async()=>({ok:status===200,status,headers:{get:()=>mime},blob:async()=>({size,text:async()=>content})}),AbortSignal:{timeout:()=>null},URL:{createObjectURL:()=>{saved++;return'blob:test';},revokeObjectURL:()=>{}},document:{createElement:()=>({click(){filename=this.download;},remove(){}}),body:{append:()=>{}}},setTimeout:()=>{}});
vm.runInContext(src.slice(src.indexOf(" $('logs-download').onclick="),src.indexOf(' // Local display preference only')),ctx);
(async()=>{
 await nodes['logs-download'].onclick();assert.equal(saved,1);assert.equal(filename,'tradfi-paper-logs.txt');
 nodes['log-scope'].value='runtime';await nodes['logs-download'].onclick();assert.equal(saved,2);assert.equal(filename,'tradfi-runtime-logs.txt');
 for(const bad of ['text/html','application/json','text/plain; charset=iso-8859-1','']){mime=bad;await nodes['logs-download'].onclick();assert.equal(saved,2);}
 mime='text/plain; charset=utf-8';for(const bad of [' <html>','\n{"error":"failed"}','["failed"]']){content=bad;await nodes['logs-download'].onclick();assert.equal(saved,2);}
 content='Manifest';size=1048577;await nodes['logs-download'].onclick();assert.equal(saved,2);
 size=100;status=401;await nodes['logs-download'].onclick();assert.equal(saved,2);
 console.log('PASS UTF-8 TXT filenames, HTML/JSON/MIME/auth/size rejection without download');
})().catch(e=>{console.error(e);process.exitCode=1;});
