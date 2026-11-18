import test from 'node:test';
import assert from 'node:assert/strict';
import { collectTicket, autoExpandTicket } from '../../extension/lib/collector.mjs';

function element(text, attributes = {}) {
  return {innerText:text,textContent:text,hidden:false,getAttribute:key=>attributes[key] ?? null,getClientRects:()=>[{}],matches:()=>false};
}
function fixture(extra = {}) {
  const nodes = {
    '[data-bug-id]':[element('', {'data-bug-id':'123456'})],
    '[data-iwork-title]':[element('2026 年车机启动失败')],
    '[data-iwork-description]':[element('复现步骤\n启动设备，等待屏幕。 token=secret-value')],
    '[data-iwork-attachment]':[element('日志.zip', {'href':'/download/1','download':'日志.zip','data-id':'attachment-1'})],
    '[data-iwork-comment]':[element('已上传日志', {'data-id':'comment-1'})],
    ...extra,
  };
  globalThis.document = {title:'2026 年车机启动失败',querySelectorAll:selector=>nodes[selector] || []};
  globalThis.location = {href:'https://iwork.example/bug/123456'};
  globalThis.getComputedStyle = ()=>({display:'block',visibility:'visible'});
}
test('captures visible fixture with honest coverage, original names, and redacted text', () => {
  fixture();
  const snapshot = collectTicket();
  assert.equal(snapshot.bug_id,'123456');
  assert.equal(snapshot.title,'2026 年车机启动失败');
  assert.equal(snapshot.attachments[0].name,'日志.zip');
  assert.equal(snapshot.attachments[0].url,'https://iwork.example/download/1');
  assert.equal(snapshot.comments[0].text,'已上传日志');
  assert.equal(snapshot.coverage.complete,false);
  assert.ok(snapshot.coverage.gaps.some(gap=>gap.includes('显示')));
  assert.ok(!JSON.stringify(snapshot).includes('secret-value'));
  assert.ok(!JSON.stringify(snapshot).includes('local_path'));
});
test('rejects title digits and catches invalid configured selectors', () => {
  fixture({'[data-bug-id]':[]});
  globalThis.location.href='https://iwork.example/home';
  assert.throws(()=>collectTicket(), /编号/);
  fixture();
  globalThis.document.querySelectorAll=()=>{throw new Error('invalid selector');};
  assert.throws(()=>collectTicket({id:'['}), /选择器/);
});
test('deduplicates attachments by id and reports unavailable description', () => {
  fixture({'[data-iwork-description]':[], '[data-iwork-attachment]':[
    element('日志.zip',{'href':'/a','download':'日志.zip','data-id':'a'}),
    element('日志.zip',{'href':'/a','download':'日志.zip','data-id':'a'}),
    element('日志.zip',{'href':'/b','download':'日志.zip','data-id':'b'}),
  ]});
  const snapshot=collectTicket();
  assert.equal(snapshot.attachments.length,2);
  assert.ok(snapshot.coverage.gaps.some(gap=>gap.includes('正文')));
});
test('keeps signed download URLs only in transient evidence transport', () => {
  fixture({'[data-iwork-attachment]':[element('日志.zip',{'href':'/a?signature=runtime-only&file=1','download':'日志.zip','data-id':'a'})]});
  const snapshot=collectTicket();
  assert.equal(snapshot.attachments[0].url,'https://iwork.example/a?signature=runtime-only&file=1');
  assert.ok(snapshot.coverage.gaps.some(gap=>gap.includes('临时授权')));
});
test('caps native payload size and reports omitted content', () => {
  fixture({'[data-iwork-comment]':Array.from({length:50},(_,i)=>element('日志'.repeat(30000),{'data-id':String(i)}))});
  const snapshot=collectTicket();
  assert.ok(Buffer.byteLength(JSON.stringify(snapshot))<=700*1024);
  assert.ok(snapshot.coverage.gaps.some(gap=>gap.includes('大小限制')));
});
test('preserves original filenames even when their characters resemble a credential assignment', () => {
  fixture({'[data-iwork-attachment]':[element('token=diagnostic.log',{'href':'/a','download':'token=diagnostic.log','data-id':'a'})],
    '[data-iwork-file-mention]':[element('token=diagnostic.log')]});
  const snapshot=collectTicket();
  assert.equal(snapshot.attachments[0].name,'token=diagnostic.log');
  assert.equal(snapshot.file_mentions[0].name,'token=diagnostic.log');
});
test('preserves SPA ticket source routes while removing fragment query credentials', () => {
  fixture();
  globalThis.location.href='https://iwork.example/app?view=detail#/bug/123456?tab=comments&token=private';
  const snapshot=collectTicket();
  assert.equal(snapshot.bug_id,'123456');
  assert.equal(snapshot.url,'https://iwork.example/app?view=detail#/bug/123456?tab=comments');
  assert.ok(!JSON.stringify(snapshot).includes('private'));
});

test('accepts the real iWork defect entityId as the Bug ID', () => {
  fixture({'[data-bug-id]':[]});
  globalThis.location.href='https://iwork.faw.cn/development/project/defect?businessId=4600000001&entityId=10000004';
  const snapshot=collectTicket();
  assert.equal(snapshot.bug_id,'10000004');
});

test('falls back to the visible real iWork defect text and extracts validity facts', () => {
  const body = `一汽云工作台-缺陷\n基本信息\n[缺陷描述*]：冷启动后黑屏\n[重现步骤*]：启动车辆后等待 30 秒\n[期望结果*]：进入桌面\n[实际结果*]：停留在黑屏\n[问题发生时间点*]：2026-07-27 02:01:00\n[测试环境*]：E001-10\n[视频/日志信息*]：log_E001-10_20260727-020100_20260727170510433.zip\n日志及视频统一见：【数字座舱统一日志基地】钉钉群`;
  fixture({'[data-bug-id]':[],'[data-iwork-description]':[],'[data-iwork-title]':[],'[data-iwork-comment]':[],'[data-iwork-attachment]':[]});
  globalThis.document.body = {innerText: body, textContent: body};
  globalThis.location.href='https://iwork.faw.cn/development/project/defect?businessId=4600000001&entityId=10000001';
  const snapshot=collectTicket();
  assert.equal(snapshot.bug_id,'10000001');
  assert.match(snapshot.description,/冷启动后黑屏/);
  assert.equal(snapshot.fields['视频/日志信息'],'log_E001-10_20260727-020100_20260727170510433.zip');
  assert.equal(snapshot.groups[0].name,'数字座舱统一日志基地');
  assert.equal(snapshot.file_mentions[0].name,'log_E001-10_20260727-020100_20260727170510433.zip');
});

test('automatically expands collapsed iWork evidence sections before collection', async () => {
  let clicks = 0;
  const button = {
    innerText: '展开视频/日志信息', textContent: '展开视频/日志信息', hidden: false,
    getAttribute: key => key === 'aria-expanded' ? 'false' : null,
    getClientRects: () => [{}],
    click: () => { clicks += 1; button.getAttribute = key => key === 'aria-expanded' ? 'true' : null; }
  };
  globalThis.document = {
    querySelectorAll: selector => selector.includes('button') || selector.includes('[role="button"]') ? [button] : [],
    body: {innerText: '正文'}
  };
  globalThis.getComputedStyle = () => ({display:'block', visibility:'visible'});
  const result = await autoExpandTicket({passes: 2, waitMs: 0});
  assert.equal(clicks, 1);
  assert.equal(result.clicked, 1);
});

test('collector remains self-contained when Chromium serializes executeScript functions', () => {
  fixture({'[data-bug-id]':[], '[data-iwork-description]':[], '[data-iwork-title]':[], '[data-iwork-comment]':[], '[data-iwork-attachment]':[]});
  const body = '[视频/日志信息*]：log_E001-10_20260727-020100_20260727170510433.zip\n日志及视频统一见：【数字座舱统一日志基地】钉钉群';
  globalThis.document.body = {innerText: body, textContent: body};
  globalThis.location.href = 'https://iwork.faw.cn/development/project/defect?entityId=10000001';
  const isolated = Function(`return (${collectTicket.toString()})`)();
  assert.doesNotThrow(() => isolated());
  const snapshot = isolated();
  assert.equal(snapshot.file_mentions[0].name, 'log_E001-10_20260727-020100_20260727170510433.zip');
  assert.equal(snapshot.groups[0].name, '数字座舱统一日志基地');
});

test('an explicit log identifier without extension is a file clue and never a group name', () => {
  fixture({'[data-bug-id]':[], '[data-iwork-description]':[], '[data-iwork-title]':[], '[data-iwork-comment]':[], '[data-iwork-attachment]':[]});
  const name = 'log_E001-10_20260830-012100_20260830153917485';
  const body = `[缺陷描述*]：\n时间：13：51\n日志群：\n${name}\n[视频/日志信息*]\n：\n日志及视频统一见：【数字座舱统一日志基地】钉钉群`;
  globalThis.document.body = {innerText: body, textContent: body};
  globalThis.location.href = 'https://iwork.faw.cn/development/project/defect?entityId=10000004';
  const isolated = Function(`return (${collectTicket.toString()})`)();
  const snapshot = isolated();
  assert.deepEqual(snapshot.groups, [{name:'数字座舱统一日志基地'}]);
  assert.ok(snapshot.file_mentions.some(item=>item.name===name));
});
