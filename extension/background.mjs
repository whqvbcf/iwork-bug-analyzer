import { CollectorBridge } from './lib/bridge.mjs';

const bridge=new CollectorBridge(chrome);
// All state mutations share this queue, including download callbacks and alarms.
let queue=Promise.resolve();
const serial=work=>{const next=queue.then(work,work);queue=next.catch(()=>{});return next;};
const poll=()=>serial(()=>bridge.poll()).catch(()=>{});
const ensureAlarm=()=>chrome.alarms.create('iwork-collector-poll',{periodInMinutes:0.5});
chrome.runtime.onInstalled.addListener(()=>{ensureAlarm();poll();});
chrome.runtime.onStartup.addListener(()=>{ensureAlarm();poll();});
chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name==='iwork-collector-poll')poll();});
chrome.downloads.onChanged.addListener(change=>{if(change.state)poll();});
chrome.runtime.onMessage.addListener((message,sender,respond)=>{
  // Only packaged extension pages can issue actions; never accept page messages.
  if(sender.id!==chrome.runtime.id || !sender.url?.startsWith(chrome.runtime.getURL(''))) return false;
  const allowed=new Set(['ping','capture','status','list_jobs','resume','cancel','open']);
  if(message?.action==='state') {
    serial(()=>bridge.load()).then(result=>respond({ok:true,result}),error=>respond({ok:false,error:{message:error.message}}));
  } else if(message?.action==='poll') {
    serial(()=>bridge.poll()).then(result=>respond({ok:true,result}),error=>respond({ok:false,error:{message:error.message}}));
  } else if(message?.action==='native' && allowed.has(message.method)) {
    serial(()=>bridge.request(message.method,message.params || {})).then(result=>{respond({ok:true,result});if(['capture','resume'].includes(message.method))poll();},error=>respond({ok:false,error:{message:error.message,code:error.code}}));
  } else {respond({ok:false,error:{message:'不支持的操作'}});}
  return true;
});
