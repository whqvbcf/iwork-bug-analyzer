import { autoExpandTicket, collectTicket } from './lib/collector.mjs';
import { collectIworkApiContext, mergeIworkApiContext } from './lib/iwork-api.mjs';
import { progressView } from './lib/progress.mjs';

const $=id=>document.getElementById(id);
let jobs=[];
let selected='';
let autoCaptureStarted=false;
async function call(message) {
  const response=await chrome.runtime.sendMessage(message);
  if(!response?.ok)throw new Error(response?.error?.message || '连接失败');
  return response.result;
}
const native=(method,params={})=>call({action:'native',method,params});
function notice(message=''){$('notice').textContent=message;$('notice').hidden=!message;}
function render() {
  const job=jobs.find(value=>value.job_id===selected)||jobs[0];
  $('empty').hidden=Boolean(job);$('job').hidden=!job;$('jobs').hidden=jobs.length<2;
  $('jobs').replaceChildren(...jobs.map(value=>{const option=document.createElement('option');option.value=value.job_id;option.textContent=`Bug ${value.bug_id} · ${progressView(value).stage}`;option.selected=value.job_id===job?.job_id;return option;}));
  if(!job)return;
  selected=job.job_id;
  $('bug-id').textContent=`Bug ${job.bug_id}`;$('job-title').textContent=job.folder || '报告将在本机问题目录生成';
  const view=progressView(job);
  $('stage').textContent=view.stage;
  const terminal=['ready','partial','completed','complete','done','failed','cancelled'].includes(job.status);
  $('activity-name').textContent=view.name;
  $('activity-name').hidden=!view.name;
  $('progress-fill').style.width=`${view.percent??0}%`;
  $('progress-track').classList.toggle('active',view.active);
  $('progress-text').textContent=view.text;
  const gaps=Array.isArray(job.gaps)?job.gaps:[];
  $('gap-box').hidden=!gaps.length;
  const visible=gaps.slice(0,3);
  if(gaps.length>3)visible.push(`其余 ${gaps.length-3} 项见报告。`);
  $('gaps').replaceChildren(...visible.map(gap=>{const item=document.createElement('li');item.textContent=gap;return item;}));
  $('cancel').disabled=terminal;
}
async function refresh(quiet=false) {
  try {
    const result=await native('list_jobs');
    jobs=(result.jobs || []).slice().reverse();
    $('connection').textContent='本地助手已连接';if(!quiet)notice();render();
  } catch(error) {
    $('connection').textContent='本地助手未连接';
    const cached=await call({action:'state'}).catch(()=>({jobs:{}}));
    jobs=Object.values(cached.jobs || {}).reverse();render();
    if(!quiet)notice(`无法连接本地助手。请完成本机安装，再点击刷新。${error.message}`);
  }
}
async function action(button,work) {
  button.disabled=true;
  try{await work();}catch(error){notice(error.message);}finally{button.disabled=false;render();}
}
$('capture').addEventListener('click',()=>action($('capture'),async()=>{
  await captureCurrentTab();
}));
$('refresh').addEventListener('click',()=>action($('refresh'),async()=>{await call({action:'poll'});await refresh();}));
$('jobs').addEventListener('change',()=>{selected=$('jobs').value;render();});
$('settings').addEventListener('click',()=>chrome.runtime.openOptionsPage());
for(const [id,method,extra] of [['open-report','open',{kind:'report'}],['open-folder','open',{kind:'folder'}],['resume','resume',{}],['cancel','cancel',{}]]) {
  $(id).addEventListener('click',()=>action($(id),async()=>{if(!selected)return;if(id==='open-report')notice('正在整理本地材料并更新报告…');const result=await native(method,{job_id:selected,...extra});if(result?.job_id)selected=result.job_id;await refresh();}));
}
await refresh();
// Opening the extension on an iWork ticket performs the one-time page action for the
// user: expand lazy sections, wait for their SPA content, capture, and start the local
// worker. The button remains available for retry when the page is still loading.
async function captureCurrentTab({silent=false}={}) {
  if(!silent)notice('正在自动展开详情并读取当前页面…');
  const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
  if(!tab?.id||!/^https?:/.test(tab.url || ''))throw new Error('请先打开 HTTP 或 HTTPS 的问题详情页');
  if(!/iwork|(?:bugs?|tickets?|issues?|defect)/iu.test(tab.url || '')) {
    if(silent)return null;
  }
  const {selectors={}}=await chrome.storage.local.get('selectors');
  await chrome.scripting.executeScript({target:{tabId:tab.id},func:autoExpandTicket,args:[{passes:8,waitMs:300}]});
  let capture;
  for(let attempt=0;attempt<5;attempt+=1) {
    [capture]=await chrome.scripting.executeScript({target:{tabId:tab.id},func:collectTicket,args:[selectors]});
    const snapshot=capture?.result;
    const hasEvidence=Boolean(snapshot?.description || Object.keys(snapshot?.fields || {}).length || snapshot?.comments?.length || snapshot?.attachments?.length || snapshot?.groups?.length || snapshot?.file_mentions?.length);
    if(hasEvidence || attempt===4)break;
    await new Promise(resolve=>setTimeout(resolve,500));
  }
  if(!capture?.result)throw new Error('无法读取页面，请确认页面已加载');
  let snapshot=capture.result;
  try {
    const [apiCapture]=await chrome.scripting.executeScript({
      target:{tabId:tab.id}, world:'MAIN', func:collectIworkApiContext
    });
    if(apiCapture?.result) snapshot=mergeIworkApiContext(snapshot,apiCapture.result);
  } catch(error) {
    snapshot.coverage.gaps.push(`iWork 评论/时间线接口采集失败：${error.message}`);
  }
  snapshot.page_tab_id = tab.id;
  const result=await native('capture',{snapshot,retry_failed:!silent});
  selected=result.job_id;
  notice('已自动建立本机报告，正在下载、检索和解压证据。');
  await refresh(true);
  return result;
}
setTimeout(()=>{
  if(autoCaptureStarted)return;
  autoCaptureStarted=true;
  captureCurrentTab({silent:true}).catch(error=>{ if(error?.message)notice(error.message); });
},0);
setInterval(()=>refresh(true),2000);
setInterval(()=>{
  const job=jobs.find(value=>value.job_id===selected);
  if(job)$('progress-text').textContent=progressView(job).text;
},1000);
