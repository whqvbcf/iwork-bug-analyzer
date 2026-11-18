import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { CollectorBridge } from '../../extension/lib/bridge.mjs';

globalThis.crypto ??= webcrypto;

const task={task_id:'t',job_id:'j',name:'原始日志.zip',url:'https://iwork.example/a',relative_path:'iwork-collector/j/t/原始日志.zip'};
function browser() {
  const data={}; const messages=[]; const files=[]; let offline=false;
  return {
    data,messages,files,setOffline:value=>offline=value,
    storage:{local:{get:async key=>({[key]:structuredClone(data[key])}),set:async value=>Object.assign(data,structuredClone(value))}},
    runtime:{sendNativeMessage:async(host,request)=>{
      assert.equal(host,'com.iwork.collector');
      if(offline) throw new Error('offline');
      messages.push(request);
      const result=request.method==='browser_poll'?{tasks:[task]}:request.method==='capture'?{job_id:'j',bug_id:'123',status:'waiting_browser'}:{accepted:true};
      return {id:request.id,ok:true,result};
    }},
    downloads:{download:async options=>{files.push({id:1,state:'in_progress',filename:'/Downloads/'+options.filename,options});return 1;},
      search:async query=>files.filter(file=>query.id===undefined||query.id===file.id),cancel:async id=>{files.find(file=>file.id===id).state='interrupted';}},
  };
}
test('persists original-name download associations and reports browser completion',async()=>{
  const chrome=browser();const bridge=new CollectorBridge(chrome);
  await bridge.poll();
  assert.equal(chrome.files[0].options.filename,task.relative_path);
  assert.equal(chrome.files[0].options.conflictAction,'overwrite');
  assert.equal(chrome.data.collectorState.downloads.t.download_id,1);
  chrome.files[0].state='complete';
  await new CollectorBridge(chrome).poll();
  assert.equal(chrome.files.length,1);
  const complete=chrome.messages.find(message=>message.method==='browser_result'&&message.params.status==='complete');
  assert.equal(complete.params.path,'/Downloads/'+task.relative_path);
});
test('comment image downloads fetch inside the page before starting the browser download', async () => {
  const chrome = browser();
  const original = chrome.runtime.sendNativeMessage;
  chrome.runtime.sendNativeMessage = async (host, request) => request.method === 'browser_poll'
    ? {id: request.id, ok: true, result: {tasks: [{...task, page_fetch: true, page_tab_id: 17,
        page_url: 'https://iwork.faw.cn/development/project/defect?entityId=1'}]}}
    : original(host, request);
  const scripts = [];
  chrome.scripting = {executeScript: async script => {scripts.push(script); return [{result: {ok: true, url: 'blob:https://iwork.faw.cn/image'}}];}};
  const bridge = new CollectorBridge(chrome);
  await bridge.poll();
  assert.equal(scripts[0].world, 'MAIN');
  assert.equal(scripts[0].target.tabId, 17);
  assert.equal(chrome.files[0].options.url, 'blob:https://iwork.faw.cn/image');
  assert.ok(!JSON.stringify(chrome.data).includes('blob:'));
});
test('a third-party image blocked by CORS can still use the browser download session', async () => {
  const chrome = browser();
  const original = chrome.runtime.sendNativeMessage;
  chrome.runtime.sendNativeMessage = async (host, request) => request.method === 'browser_poll'
    ? {id: request.id, ok: true, result: {tasks: [{...task, page_fetch: true, page_tab_id: 17}]}}
    : original(host, request);
  chrome.scripting = {executeScript: async () => [{result: {ok: false, browser_fallback: true, error: 'Failed to fetch'}}]};
  await new CollectorBridge(chrome).poll();
  assert.equal(chrome.files[0]?.options.url, task.url);
});
test('queues failed native notifications and retries on reconnection without duplicate downloads',async()=>{
  const chrome=browser();const bridge=new CollectorBridge(chrome);
  await bridge.poll();
  chrome.setOffline(true);chrome.files[0].state='complete';
  await assert.rejects(()=>bridge.poll(), /offline/);
  assert.ok(chrome.data.collectorState.outbox.some(entry=>entry.status==='complete'));
  chrome.setOffline(false);await new CollectorBridge(chrome).poll();
  assert.equal(chrome.data.collectorState.outbox.length,0);
  assert.equal(chrome.files.length,1);
});
test('capture remembers job summaries but never persists snapshots or signed URLs',async()=>{
  const chrome=browser();const bridge=new CollectorBridge(chrome);
  await bridge.request('capture',{snapshot:{bug_id:'123',url:'https://example.com/?token=secret'}});
  assert.equal(chrome.data.collectorState.jobs.j.bug_id,'123');
  assert.ok(!JSON.stringify(chrome.data).includes('secret'));
});
test('recovers a browser download created before the worker saved its ID',async()=>{
  const chrome=browser();
  chrome.data.collectorState={jobs:{},downloads:{t:{...task,url:undefined,status:'starting'}},outbox:[]};
  chrome.files.push({id:7,state:'complete',filename:'/Downloads/'+task.relative_path});
  await new CollectorBridge(chrome).poll();
  assert.equal(chrome.files.length,1);
  assert.equal(chrome.data.collectorState.downloads.t.download_id,7);
});
test('resume clears failed mappings and stale failure receipts so the same task can retry',async()=>{
  const chrome=browser();const bridge=new CollectorBridge(chrome);
  chrome.data.collectorState={jobs:{},downloads:{t:{...task,status:'failed',download_id:5}},outbox:[{task_id:'t',download_id:5,status:'failed'}]};
  await bridge.request('resume',{job_id:'j'});
  await bridge.poll();
  assert.equal(chrome.files.length,1);
  assert.equal(chrome.data.collectorState.downloads.t.download_id,1);
  assert.ok(!chrome.messages.some(message=>message.method==='browser_result'&&message.params.status==='failed'));
});
test('resume of a retired job clears failed downloads for the returned replacement job',async()=>{
  const chrome=browser();const bridge=new CollectorBridge(chrome);
  chrome.data.collectorState={jobs:{},downloads:{t:{...task,status:'failed',download_id:5}},outbox:[{task_id:'t',download_id:5,status:'failed'}]};
  const original=chrome.runtime.sendNativeMessage;
  chrome.runtime.sendNativeMessage=async(host,request)=>request.method==='resume'
    ? {id:request.id,ok:true,result:{job_id:'j',bug_id:'123',status:'captured'}}
    : original(host,request);
  const result=await bridge.request('resume',{job_id:'retired-job'});
  assert.equal(result.job_id,'j');
  await bridge.poll();
  assert.equal(chrome.files.length,1);
  assert.equal(chrome.data.collectorState.downloads.t.download_id,1);
  assert.ok(!chrome.messages.some(message=>message.method==='browser_result'&&message.params.download_id===5));
});
test('explicit recapture clears failed browser tasks for the returned job while silent capture keeps them',async()=>{
  const chrome=browser();const bridge=new CollectorBridge(chrome);
  chrome.data.collectorState={jobs:{},downloads:{t:{...task,status:'failed',download_id:5}},outbox:[{task_id:'t',status:'failed'}]};
  await bridge.request('capture',{snapshot:{bug_id:'123'}});
  assert.equal(chrome.data.collectorState.downloads.t.status,'failed');
  await bridge.request('capture',{snapshot:{bug_id:'123'},retry_failed:true});
  const retried=chrome.messages.findLastIndex(message=>message.method==='capture');
  const failure=chrome.messages.findIndex(message=>message.method==='browser_result'&&message.params.status==='failed');
  assert.ok(failure>=0&&failure<retried,'deliver the interrupted download before asking the helper to retry');
  assert.equal(chrome.data.collectorState.downloads.t,undefined);
  assert.equal(chrome.data.collectorState.outbox.length,0);
  await bridge.poll();
  assert.equal(chrome.files.length,1);
});
test('explicit recapture preserves an undelivered failure when the helper is still offline',async()=>{
  const chrome=browser();const bridge=new CollectorBridge(chrome);
  chrome.data.collectorState={jobs:{},downloads:{t:{...task,status:'started',download_id:5}},outbox:[]};
  chrome.files.push({id:5,state:'interrupted',filename:'/Downloads/'+task.relative_path,error:'NETWORK_FAILED'});
  chrome.setOffline(true);
  await assert.rejects(()=>bridge.request('capture',{snapshot:{bug_id:'123'},retry_failed:true}),/offline/);
  assert.equal(chrome.data.collectorState.downloads.t.status,'failed');
  assert.equal(chrome.data.collectorState.outbox[0].status,'failed');
  chrome.setOffline(false);
  await bridge.request('capture',{snapshot:{bug_id:'123'},retry_failed:true});
  assert.deepEqual(chrome.messages.map(message=>message.method),['browser_result','capture']);
});
test('does not persist credentials echoed by browser or native error text',async()=>{
  const chrome=browser();
  chrome.runtime.sendNativeMessage=async()=>{throw new Error('failed https://example.com/a?token=private-value&file=1');};
  await assert.rejects(()=>new CollectorBridge(chrome).poll());
  assert.ok(!JSON.stringify(chrome.data).includes('private-value'));
});
test('turns rejected completed handoffs into failed evidence and continues independent downloads',async()=>{
  const chrome=browser();const bridge=new CollectorBridge(chrome);
  await bridge.poll();chrome.files[0].state='complete';
  const original=chrome.runtime.sendNativeMessage;
  chrome.runtime.sendNativeMessage=async(host,request)=>{
    if(request.method==='browser_result'&&request.params.status==='complete') return {id:request.id,ok:false,error:{code:'collector_error',message:'文件不属于本任务已登记的浏览器下载路径'}};
    if(request.method==='browser_poll') return {id:request.id,ok:true,result:{tasks:[{...task,task_id:'other',relative_path:'iwork-collector/j/other/'+task.name}]}};
    return original(host,request);
  };
  await bridge.poll();
  assert.equal(chrome.files.length,2);
  assert.equal(chrome.data.collectorState.downloads.t.status,'failed');
  assert.ok(chrome.messages.some(message=>message.method==='browser_result'&&message.params.task_id==='t'&&message.params.status==='failed'));
});
