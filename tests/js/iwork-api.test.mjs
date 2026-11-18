import test from 'node:test';
import assert from 'node:assert/strict';
import { collectIworkApiContext, mergeIworkApiContext } from '../../extension/lib/iwork-api.mjs';

function installPage({ responses, href = 'https://iwork.faw.cn/development/project/defect?businessId=4600000001&entityId=10000001' }) {
  const calls = [];
  const stores = { jwt: 'page-token' };
  const storage = { getItem: key => stores[key] || null };
  globalThis.location = { href };
  globalThis.window = { localStorage: storage, sessionStorage: storage };
  globalThis.fetch = async (url, init = {}) => {
    calls.push({url, init});
    const response = responses.find(item => url.includes(item.path));
    if (!response) throw new Error(`unexpected request: ${url}`);
    return {ok: true, status: 200, text: async () => JSON.stringify(response.body)};
  };
  return calls;
}

test('reads iWork detail, nested comments, and change timeline from page APIs', async () => {
  const calls = installPage({responses: [
    {path: '/scrum/item/query', body: {success: true, data: {id: '10000001', title: '接口标题', description: '接口正文'}}},
    {path: '/scrum/comment/treeList', body: {data: [{id: 'c1', creatorName: '张三', gmtCreated: '2026-07-27 17:30:00', contentText: '日志见群', children: [{id: 'c2', creatorName: '李四', createTime: '2026-07-27 17:31:00', contentText: '补充说明'}]}]}},
    {path: '/item/changeRecord/list', body: {data: [{id: 'h1', originKeyName: '状态', oldValue: '处理中', newValue: '已验证', gmtCreated: '2026-07-27 18:00:00', creatorName: '王五'}]}},
    {path: '/scrum/related/item/list', body: {data: []}},
    {path: '/document/internal/item/docRelatedList', body: {data: []}},
  ]});
  const result = await collectIworkApiContext();
  assert.equal(result.ok, true);
  assert.equal(result.title, '接口标题');
  assert.equal(result.comments.length, 2);
  assert.equal(result.comments[1].parent_id, 'c1');
  assert.equal(result.timeline[0].text, '状态：处理中 → 已验证');
  assert.equal(result.comment_coverage.complete, true);
  assert.equal(result.timeline_coverage.complete, true);
  assert.equal(calls.length, 5);
  assert.match(calls[0].init.headers.authorization, /^Bearer /);
  const merged = mergeIworkApiContext({title: 'DOM', description: 'DOM', comments: [], timeline: [], coverage: {gaps: []}}, result);
  assert.equal(merged.comments.length, 2);
  assert.equal(merged.timeline.length, 1);
});

test('reports optional comment and timeline API failures without discarding detail', async () => {
  installPage({responses: [
    {path: '/scrum/item/query', body: {success: true, data: {id: '10000001', title: '接口标题', description: '接口正文'}}},
  ]});
  globalThis.fetch = async url => {
    if (url.includes('/scrum/item/query')) return {ok: true, status: 200, text: async () => JSON.stringify({data: {id: '10000001', title: '接口标题', description: '接口正文'}})};
    throw new Error('评论接口无权限');
  };
  const result = await collectIworkApiContext();
  assert.equal(result.ok, true);
  assert.equal(result.title, '接口标题');
  assert.equal(result.comments.length, 0);
  assert.equal(result.comment_coverage.complete, false);
  assert.match(result.comment_coverage.gaps.join(' '), /评论/);
});

test('API collector remains self-contained when Chromium serializes the MAIN-world function', async () => {
  installPage({responses: [
    {path: '/scrum/item/query', body: {data: {id: '10000001', title: '标题', description: '正文'}}},
    {path: '/scrum/comment/treeList', body: {data: [{id: 'c1', contentText: '评论'}]}},
    {path: '/item/changeRecord/list', body: {data: []}},
    {path: '/scrum/related/item/list', body: {data: []}},
    {path: '/document/internal/item/docRelatedList', body: {data: []}},
  ]});
  globalThis.window.fetch = globalThis.fetch;
  const isolated = Function(`return (${collectIworkApiContext.toString()})`)();
  const result = await isolated();
  assert.equal(result.ok, true);
  assert.equal(result.comments[0].text, '评论');
});

test('extracts comment and reply images from HTML, JSONML and Quill without losing their owners', async () => {
  installPage({responses: [
    {path: '/scrum/item/query', body: {data: {title: '标题', description: '正文'}}},
    {path: '/scrum/comment/treeList', body: {data: [{id: 'c1', creatorName: '张三', contentText: '看图',
      content: '<p>看图</p><img src="https://devops.faw.cn/a.png?x=1&amp;y=2" name="原图.png">', children: [
        {id: 'c2', creatorName: '李四', content: JSON.stringify(['div', {}, ['img', {src: 'https://devops.faw.cn/b.jpg', name: '回复图.jpg'}]])},
        {id: 'c3', content: {ops: [{insert: {image: 'https://devops.faw.cn/a.png?x=1&y=2'}}]}}
      ]}]}},
    {path: '/item/changeRecord/list', body: {data: []}},
    {path: '/scrum/related/item/list', body: {data: []}},
    {path: '/document/internal/item/docRelatedList', body: {data: []}}
  ]});
  const result = await collectIworkApiContext();
  assert.equal(result.images?.length, 2);
  assert.equal(result.images[0].name, '原图.png');
  assert.deepEqual(result.images[0].comment_ids, ['c1', 'c3']);
  assert.equal(result.comments[1].parent_id, 'c1');
  assert.ok(result.comments[1].image_ids.includes(result.images[1].id));
  const merged = mergeIworkApiContext({comments: [], images: [], coverage: {gaps: []}}, result);
  assert.equal(merged.images.length, 2);
});

test('preserves inline iWork mention names from rich content when contentText omits them', async () => {
  const before = '请协助确认异常重启的原因，';
  const after = '重点看重启前的音频链路。';
  installPage({responses: [
    {path: '/scrum/item/query', body: {data: {title: '标题', description: '正文'}}},
    {path: '/scrum/comment/treeList', body: {data: [{id: 'c9', creatorName: '张三',
      contentText: before + after,
      content: JSON.stringify(['root', {}, ['p', {}, ['span', {'data-type': 'text'}, ['span', {'data-type': 'leaf'}, before]],
        ['span', {id: 123, name: '李四', avatarUrl: 'private-avatar', 'data-type': 'mention'},
          ['span', {'data-type': 'text'}, ['span', {'data-type': 'leaf'}, '']]],
        ['span', {'data-type': 'text'}, ['span', {'data-type': 'leaf'}, after]]],
        ['p', {}, ['span', {}, '下一段原文']]])}]}},
    {path: '/item/changeRecord/list', body: {data: []}},
    {path: '/scrum/related/item/list', body: {data: []}},
    {path: '/document/internal/item/docRelatedList', body: {data: []}}
  ]});
  const isolated = Function(`return (${collectIworkApiContext.toString()})`)();
  const result = await isolated();
  assert.equal(result.comments[0].text, before + '@李四' + after + '\n下一段原文');
  assert.ok(!JSON.stringify(result.comments).includes('private-avatar'));
});

test('preserves multiple Quill mentions and HTML mention labels without duplicate at signs', async () => {
  installPage({responses: [
    {path: '/scrum/item/query', body: {data: {title: '标题', description: '正文'}}},
    {path: '/scrum/comment/treeList', body: {data: [
      {id: 'quill', content: {ops: [{insert: '请'}, {insert: {mention: {id: 1, value: '张三'}}},
        {insert: '和'}, {insert: {mention: {id: 2, value: '@李四'}}}, {insert: '核对\n\n\n第二行'}]}},
      {id: 'html', contentText: '请处理', content: '<p>请<span data-type="mention" data-name="李四"></span>处理</p>'},
      {id: 'plain', contentText: '请@王五处理', content: '<p>旧内容</p>'}
    ]}},
    {path: '/item/changeRecord/list', body: {data: []}},
    {path: '/scrum/related/item/list', body: {data: []}},
    {path: '/document/internal/item/docRelatedList', body: {data: []}}
  ]});
  const result = await collectIworkApiContext();
  assert.equal(result.comments[0].text, '请@张三和@李四核对\n\n\n第二行');
  assert.equal(result.comments[1].text, '请@李四处理');
  assert.equal(result.comments[2].text, '请@王五处理');
});

test('merging API-only description discovers the extensionless log and corrects the false DOM group', () => {
  const name = 'log_E001-10_20260830-012100_20260830153917485';
  const merged = mergeIworkApiContext({description:'', comments:[], groups:[{name}], file_mentions:[],
    coverage:{gaps:[]}}, {ok:true,description:`日志群：\n${name}\n日志及视频统一见：【数字座舱统一日志基地】钉钉群`});
  assert.deepEqual(merged.groups,[{name:'数字座舱统一日志基地'}]);
  assert.deepEqual(merged.file_mentions,[{name}]);
});

test('confirmed API title removes only the obsolete browser-title fallback warning', () => {
  const fallback = '标题使用浏览器页面标题，请核对是否对应当前 Bug';
  const scope = '仅采集当前已显示的页面内容；未验证折叠区、分页、历史评论或登录权限覆盖';
  const merged = mergeIworkApiContext({bug_id:'123', title:'DOM', coverage:{gaps:[fallback,scope]}},
    {ok:true, bug_id:'123', title:'接口确认的标题', api_coverage:{detail:true}});
  assert.equal(merged.title, '接口确认的标题');
  assert.ok(!merged.coverage.gaps.includes(fallback));
  assert.match(merged.coverage.gaps.join('\n'), /附件.*未证实完整/);
});

test('API coverage replaces the DOM-only scope warning while retaining unverified coverage', () => {
  const domOnly = '仅采集当前已显示的页面内容；未验证折叠区、分页、历史评论或登录权限覆盖';
  const merged = mergeIworkApiContext({coverage:{complete:false,gaps:[domOnly]}},
    {ok:true, api_coverage:{detail:true,comments:true,timeline:true}});
  assert.equal(merged.coverage.complete, false);
  assert.ok(!merged.coverage.gaps.includes(domOnly));
  assert.match(merged.coverage.gaps.join('\n'), /已通过接口补采.*详情.*评论.*时间线/);
  assert.match(merged.coverage.gaps.join('\n'), /未证实完整/);
});

