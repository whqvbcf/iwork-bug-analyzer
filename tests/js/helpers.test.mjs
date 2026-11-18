import test from 'node:test';
import assert from 'node:assert/strict';
import { extractBugId, normalizeCandidate, nativeRequest, validateNativeResponse, validateDownloadTask } from '../../extension/lib/helpers.mjs';

test('identifies only exact numeric IDs in trusted URL paths/query or data fields', () => {
  assert.equal(extractBugId('https://iwork.example/bug/123456?view=detail'), '123456');
  assert.equal(extractBugId('https://iwork.example/detail?bugId=123456'), '123456');
  assert.equal(extractBugId('https://iwork.faw.cn/development/project/defect?businessId=4600000001&entityId=10000004'), '10000004');
  assert.equal(extractBugId('https://iwork.example/detail', ['123456']), '123456');
  assert.throws(() => extractBugId('https://iwork.example/dashboard?version=2026'), /编号/);
  assert.throws(() => extractBugId('https://iwork.example/bug/12abc'), /编号/);
  assert.throws(() => extractBugId('https://iwork.example/bug/123', ['456']), /冲突/);
  assert.throws(() => extractBugId('https://iwork.example/bug/123?bugId=456'), /冲突/);
});

test('normalizes browser evidence while excluding credentials and arbitrary local files', () => {
  const result = normalizeCandidate({id:'a',name:'原始 日志.zip',url:'/download/1?token=secret&file=1',local_path:'/etc/passwd'}, 'https://iwork.example/bug/123');
  assert.deepEqual(result, {id:'a',name:'原始 日志.zip',url:'https://iwork.example/download/1?file=1',transport:'browser'});
  assert.throws(() => normalizeCandidate({id:'a',name:'../x',url:'file:///etc/passwd'}, 'https://example.com'), /文件名|链接/);
  assert.throws(() => normalizeCandidate({id:'a',name:'x',url:'javascript:alert(1)'}, 'https://example.com'), /链接/);
  assert.throws(() => normalizeCandidate({id:'a',name:'x'}, 'https://example.com'), /链接/);
});

test('native envelopes match request id and propagate structured failures', () => {
  assert.deepEqual(nativeRequest('status', {job_id:'j'}, 'r1'), {id:'r1',method:'status',params:{job_id:'j'}});
  assert.deepEqual(validateNativeResponse({id:'r1',ok:true,result:{job_id:'j'}}, 'r1'), {job_id:'j'});
  assert.throws(() => validateNativeResponse({id:'other',ok:true,result:{}}, 'r1'), /响应/);
  assert.throws(() => validateNativeResponse({id:'r1',ok:false,error:{code:'MISSING',message:'host missing'}}, 'r1'), /host missing/);
  assert.throws(() => validateNativeResponse({id:'r1',ok:true}, 'r1'), /响应/);
});

test('download filename must equal the registered path and original basename', () => {
  assert.equal(validateDownloadTask({task_id:'t',job_id:'j',name:'日志.zip',url:'https://example.com/a',relative_path:'iwork-collector/j/t/日志.zip'}).name, '日志.zip');
  for (const relative_path of ['/tmp/日志.zip','iwork-collector/j/t/../日志.zip','iwork-collector/other/t/日志.zip','iwork-collector/j/t/renamed.zip']) {
    assert.throws(() => validateDownloadTask({task_id:'t',job_id:'j',name:'日志.zip',url:'https://example.com/a',relative_path}), /路径/);
  }
});
