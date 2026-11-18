import assert from 'node:assert/strict';
import test from 'node:test';
import { progressView } from '../../extension/lib/progress.mjs';

test('shows the active file, stage and elapsed time without inventing file percentages',()=>{
  const view=progressView({status:'processing',progress:{phase:'downloading',name:'车辆日志.zip',
    started_at:'2026-11-13T10:00:00Z',completed:1,total:3,failed:0}},Date.parse('2026-11-13T10:01:30Z'));
  assert.equal(view.stage,'正在下载');
  assert.equal(view.name,'车辆日志.zip');
  assert.match(view.text,/1\s*\/\s*3/);
  assert.match(view.text,/1分30秒/);
  assert.ok(Math.abs(view.percent-100/3)<1e-9); // material count, never download percentage
  assert.equal(view.active,true);
});

test('shows expanded bytes and completed members and recognizes real ready state',()=>{
  const active=progressView({status:'processing',progress:{phase:'extracting',name:'日志.zip',
    expanded_bytes:10485760,files_done:7,total:1,completed:0}});
  assert.match(active.text,/10.0 MiB/);
  assert.match(active.text,/7 个文件/);
  const done=progressView({status:'ready',progress:{phase:'complete',total:1,completed:1}});
  assert.equal(done.stage,'采集完成');
  assert.equal(done.percent,100);
  assert.equal(done.active,false);
  const failed=progressView({status:'partial',progress:{phase:'partial',total:1,completed:0,failed:1}});
  assert.equal(failed.percent,0);
  assert.equal(failed.active,false);
});
