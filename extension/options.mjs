import {defaultSelectors} from './lib/collector.mjs';
const names={id:'Bug 编号',title:'问题标题',description:'问题正文',field:'属性字段',comment:'评论',attachment:'附件链接',image:'正文图片',group:'相关群',mention:'提及的文件'};
const hints={id:'必须是明确的数字编号；不要选取标题',field:'使用 data-iwork-field 或 data-name 作为字段名',attachment:'下载原始附件，不使用缩略图',group:'群名；可附带 data-conversation-id',mention:'文件名；可附带 data-group、data-target-id'};
const status=document.getElementById('status');
function tell(message){status.textContent=message;status.hidden=false;}
async function render(reset=false) {
  const {selectors={}}=await chrome.storage.local.get('selectors');
  const values=reset?defaultSelectors:{...defaultSelectors,...selectors};
  document.getElementById('fields').replaceChildren(...Object.keys(defaultSelectors).map(key=>{
    const field=document.createElement('div');field.className='field';
    const label=document.createElement('label');label.htmlFor=key;label.textContent=names[key];
    const input=document.createElement('input');input.id=key;input.name=key;input.value=values[key];input.spellcheck=false;
    const hint=document.createElement('p');hint.className='help';hint.textContent=hints[key] || '留空可跳过该类内容';
    field.append(label,input,hint);return field;
  }));
}
document.getElementById('settings-form').addEventListener('submit',async event=>{
  event.preventDefault();
  const selectors=Object.fromEntries(new FormData(event.currentTarget));
  try{
    for(const [key,value] of Object.entries(selectors)) {if(value.length>2000)throw new Error(`${names[key]}选择器过长`);if(value)try{document.querySelector(value);}catch{throw new Error(`${names[key]}选择器无效`);}}
    await chrome.storage.local.set({selectors});tell('设置已保存。返回问题详情页，再次采集。');
  }catch(error){tell(error.message);}
});
document.getElementById('reset').addEventListener('click',async()=>{await chrome.storage.local.set({selectors:defaultSelectors});await render(true);tell('已恢复默认选择器。');});
document.getElementById('check').addEventListener('click',async()=>{
  try{const response=await chrome.runtime.sendMessage({action:'native',method:'ping',params:{}});if(!response?.ok)throw new Error(response?.error?.message || '无法连接');tell(`本地助手已连接，版本 ${response.result.version}。目录：${response.result.workspace_root}`);}catch{tell('无法连接本地助手。请按 INSTALL.md 完成本机安装，并重新加载扩展。');}
});
await render();
