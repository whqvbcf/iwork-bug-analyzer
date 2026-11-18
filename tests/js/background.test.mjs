import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';

globalThis.crypto ??= webcrypto;
let listener;
const event={addListener(){}};
globalThis.chrome={
  runtime:{id:'test-extension',getURL:path=>'chrome-extension://test-extension/'+path,
    onInstalled:event,onStartup:event,onMessage:{addListener(value){listener=value;}},
    sendNativeMessage:async(host,request)=>({id:request.id,ok:true,result:{version:'1.0.0'}})},
  alarms:{onAlarm:event,create:async()=>{}},downloads:{onChanged:event},
};
await import('../../extension/background.mjs');

test('accepts packaged options pages even when Chromium identifies their tab',async()=>{
  let resolve;
  const response=new Promise(done=>{resolve=done;});
  const accepted=listener({action:'native',method:'ping',params:{}},
    {id:'test-extension',url:'chrome-extension://test-extension/options.html',tab:{id:9}},resolve);
  assert.equal(accepted,true);
  assert.equal((await response).ok,true);
});
test('rejects page/content-script messages despite matching extension ID',()=>{
  assert.equal(listener({action:'native',method:'ping'},
    {id:'test-extension',url:'https://iwork.example/bug/123',tab:{id:9}},()=>{}),false);
});
