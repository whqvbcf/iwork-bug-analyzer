export const defaultSelectors = {
  id:'[data-bug-id]', title:'[data-iwork-title]', description:'[data-iwork-description]',
  field:'[data-iwork-field]', comment:'[data-iwork-comment]', attachment:'[data-iwork-attachment]',
  image:'[data-iwork-image]', group:'[data-iwork-group]', mention:'[data-iwork-file-mention]',
};

const FACT_LABELS = [
  '缺陷描述', '重现步骤', '期望结果', '实际结果', '问题发生时间点', '测试环境',
  '视频/日志信息', '车辆VIN码', '原因定位', '解决措施', '提交链接', '验证版本',
  '验证分支', '验证环境', '验证结论', '日志有效性', '日志是否有效', '有效性'
];

// This function is intentionally self-contained because Chromium serializes it when
// passed to scripting.executeScript. It expands only controls that look like detail,
// comment, attachment, video or log sections, then gives the SPA time to render them.
export async function autoExpandTicket({passes = 6, waitMs = 250} = {}) {
  const limit = Math.max(1, Math.min(10, Number(passes) || 6));
  const delay = Math.max(0, Math.min(1000, Number(waitMs) || 0));
  const seen = new Set();
  let clicked = 0;
  const visible = node => {
    try {
      const style = getComputedStyle(node);
      return !node.hidden && style.display !== 'none' && style.visibility !== 'hidden' && node.getClientRects().length > 0;
    } catch { return false; }
  };
  const text = node => String(node?.innerText ?? node?.textContent ?? node?.getAttribute?.('aria-label') ?? '').trim();
  const wait = () => new Promise(resolve => setTimeout(resolve, delay));
  for (let pass = 0; pass < limit; pass += 1) {
    let controls = [];
    try { controls = [...document.querySelectorAll('button,[role="button"],summary,[aria-expanded="false"],[data-state="closed"]')]; }
    catch { controls = []; }
    for (const node of controls) {
      if (!visible(node) || seen.has(node)) continue;
      const aria = node.getAttribute?.('aria-expanded');
      const state = node.getAttribute?.('data-state');
      const label = text(node);
      const forced = aria === 'false' || state === 'closed';
      const likely = /展开|更多|加载更多|查看全部|显示全部|展开全部|详情|评论|附件|视频|日志|基本信息|问题字段/iu.test(label);
      if (!forced && !likely) continue;
      seen.add(node);
      try { node.click(); clicked += 1; } catch { /* a detached React node; continue scanning */ }
    }
    await wait();
  }
  return {clicked, passes: limit};
}

function extractFacts(value) {
  const labels = FACT_LABELS.slice().sort((a, b) => b.length - a.length);
  const labelPattern = labels.map(label => label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  const facts = {};
  const lines = String(value || '').replace(/\r/g, '').split(/\n+/).map(line => line.trim()).filter(Boolean);
  for (const line of lines) {
    const match = line.match(new RegExp(`^\\s*[\\[【]?\\s*(${labelPattern})\\s*\\*?\\s*[\\]】]?\\s*[：:]?\\s*(.*)$`));
    if (match && match[2].trim()) facts[match[1]] = match[2].trim();
  }
  if (Object.keys(facts).length) return facts;
  const compact = String(value || '').replace(/\\s+/g, ' ').trim();
  const pattern = new RegExp(`(?:^|\\s)[\\[【]?(${labelPattern})\\s*\\*?\\s*[\\]】]?\\s*[：:]?\\s*([\\s\\S]*?)(?=\\s*[\\[【]?(?:${labelPattern})\\s*\\*?\\s*[\\]】]?\\s*[：:]?|$)`, 'g');
  let match;
  while ((match = pattern.exec(compact))) if (match[2].trim()) facts[match[1]] = match[2].trim();
  return facts;
}

function fallbackGroups(value) {
  const result = [];
  const add = name => {
    const clean = String(name || '').replace(/[【】\[\]「」“”]/g, '').trim();
    if (clean && !/^log_[A-Za-z0-9][A-Za-z0-9._-]*$/i.test(clean) && !result.some(item => item.name === clean)) result.push({name: clean});
  };
  const text = String(value || '');
  for (const match of text.matchAll(/(?:钉钉群|日志群|讨论群|群名)\s*[：:]\s*[【\[「“]?([^\s，。；】\]」”]+?)(?:】\]|」|”)?(?=\s|$)/gu)) add(match[1]);
  for (const match of text.matchAll(/(?:见|来自)\s*[：:]?\s*[【\[「“]([^】\]」”\n]+)[】\]」”]\s*(?:钉钉)?群?/gu)) add(match[1]);
  return result;
}

function fallbackFileMentions(value) {
  const result = [];
  const pattern = /(?:^|[^\w\u4e00-\u9fff])([\w\u4e00-\u9fff][\w\u4e00-\u9fff.()（）_ -]*\.(?:zip|7z|rar|tar(?:\.gz)?|tgz|gz|log|txt|trace|perfetto-trace|mp4|png|jpe?g))(?![\w])/giu;
  for (const match of String(value || '').matchAll(pattern)) {
    const name = match[1].trim();
    if (!result.some(item => item.name === name)) result.push({name});
  }
  for (const match of String(value || '').matchAll(/\blog_[A-Za-z0-9][A-Za-z0-9._-]*/gi)) {
    if (!result.some(item => item.name === match[0])) result.push({name:match[0]});
  }
  return result;
}

export function enrichTicketClues(snapshot) {
  const text = [snapshot.description, ...(snapshot.comments || []).map(item=>item.text)].filter(Boolean).join('\n');
  snapshot.groups = (snapshot.groups || []).filter(group=>!/^log_[A-Za-z0-9][A-Za-z0-9._-]*$/i.test(group.name));
  snapshot.file_mentions ||= [];
  for (const group of fallbackGroups(text)) if (!snapshot.groups.some(item=>item.name===group.name)) snapshot.groups.push(group);
  for (const mention of fallbackFileMentions(text)) if (!snapshot.file_mentions.some(item=>item.name===mention.name)) snapshot.file_mentions.push(mention);
  return snapshot;
}

// Intentionally self-contained: Chromium serializes this function for executeScript.
export function collectTicket(selectors = {}) {
  const defaults = {
    id:'[data-bug-id]', title:'[data-iwork-title]', description:'[data-iwork-description]',
    field:'[data-iwork-field]', comment:'[data-iwork-comment]', attachment:'[data-iwork-attachment]',
    image:'[data-iwork-image]', group:'[data-iwork-group]', mention:'[data-iwork-file-mention]',
  };
  const config = {...defaults, ...selectors};
  const gaps = ['仅采集当前已显示的页面内容；未验证折叠区、分页、历史评论或登录权限覆盖'];
  // Chrome serializes this function before running it in the page. Keep the
  // fallback parsers local so that serialized execution has no module-scope
  // dependencies.
  const factLabels = [
    '缺陷描述', '重现步骤', '期望结果', '实际结果', '问题发生时间点', '测试环境',
    '视频/日志信息', '车辆VIN码', '原因定位', '解决措施', '提交链接', '验证版本',
    '验证分支', '验证环境', '验证结论', '日志有效性', '日志是否有效', '有效性'
  ];
  const parseFacts = value => {
    const labels = factLabels.slice().sort((a, b) => b.length - a.length);
    const labelPattern = labels.map(label => label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
    const facts = {};
    const lines = String(value || '').replace(/\r/g, '').split(/\n+/).map(line => line.trim()).filter(Boolean);
    for (const line of lines) {
      const match = line.match(new RegExp(`^\\s*[\\[【]?\\s*(${labelPattern})\\s*\\*?\\s*[\\]】]?\\s*[：:]?\\s*(.*)$`));
      if (match && match[2].trim()) facts[match[1]] = match[2].trim();
    }
    if (Object.keys(facts).length) return facts;
    const compact = String(value || '').replace(/\s+/g, ' ').trim();
    const pattern = new RegExp(`(?:^|\\s)[\\[【]?(${labelPattern})\\s*\\*?\\s*[\\]】]?\\s*[：:]?\\s*([\\s\\S]*?)(?=\\s*[\\[【]?(?:${labelPattern})\\s*\\*?\\s*[\\]】]?\\s*[：:]?|$)`, 'g');
    let match;
    while ((match = pattern.exec(compact))) if (match[2].trim()) facts[match[1]] = match[2].trim();
    return facts;
  };
  const parseGroups = value => {
    const result = [];
    const add = name => {
      const clean = String(name || '').replace(/[【】[\]「」“”]/g, '').trim();
      if (clean && !/^log_[A-Za-z0-9][A-Za-z0-9._-]*$/i.test(clean) && !result.some(item => item.name === clean)) result.push({name: clean});
    };
    const textValue = String(value || '');
    for (const match of textValue.matchAll(/(?:钉钉群|日志群|讨论群|群名)\s*[：:]\s*[【\[「“]?([^\s，。；】\]」”]+?)(?:】\]|」|”)?(?=\s|$)/gu)) add(match[1]);
    for (const match of textValue.matchAll(/(?:见|来自)\s*[：:]?\s*[【\[「“]([^】\]」”\n]+)[】\]」”]\s*(?:钉钉)?群?/gu)) add(match[1]);
    return result;
  };
  const parseFileMentions = value => {
    const result = [];
    const pattern = /(?:^|[^\w\u4e00-\u9fff])([\w\u4e00-\u9fff][\w\u4e00-\u9fff.()（）_ -]*\.(?:zip|7z|rar|tar(?:\.gz)?|tgz|gz|log|txt|trace|perfetto-trace|mp4|png|jpe?g))(?![\w])/giu;
    for (const match of String(value || '').matchAll(pattern)) {
      const name = match[1].trim();
      if (!result.some(item => item.name === name)) result.push({name});
    }
    for (const match of String(value || '').matchAll(/\blog_[A-Za-z0-9][A-Za-z0-9._-]*/gi)) {
      if (!result.some(item => item.name === match[0])) result.push({name:match[0]});
    }
    return result;
  };
  const redact = value => String(value || '')
    .replace(/((?:access[_-]?token|refresh[_-]?token|token|password|secret|authorization|cookie|api[_-]?key|session[_-]?id)\s*[:=]\s*)[^\s&;,]+/gi, '$1[已隐藏]')
    .replace(/Bearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [已隐藏]');
  const text = node => redact(node?.innerText ?? node?.textContent ?? '').trim();
  const query = name => {
    if (!config[name]) return [];
    try { return [...document.querySelectorAll(config[name])]; }
    catch { throw new Error(`“${name}”选择器无效，请在设置中修正`); }
  };
  const visible = node => {
    const style = getComputedStyle(node);
    return !node.hidden && style.display !== 'none' && style.visibility !== 'hidden' && node.getClientRects().length > 0;
  };
  const nodes = name => query(name).filter(visible);
  const bounded = value => {
    if (value.length > 60000) { gaps.push('页面内容超过采集长度限制，已截断；请补充原始正文'); return value.slice(0, 60000); }
    return value;
  };
  const cleanUrl = (value, download = false) => {
    const url = new URL(value, location.href);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('不支持的链接');
    let removed = false;
    for (const key of [...url.searchParams.keys()]) if (/token|auth|password|secret|cookie|session|signature|credential|api.?key/i.test(key)) {
      if (!download) url.searchParams.delete(key);
      removed = true;
    }
    if (!download && url.hash) {
      const fragment=url.hash.slice(1);
      const queryIndex=fragment.indexOf('?');
      const path=queryIndex>=0?fragment.slice(0,queryIndex):fragment;
      if(queryIndex>=0 || (!path.startsWith('/') && path.includes('='))) {
        const params=new URLSearchParams(queryIndex>=0?fragment.slice(queryIndex+1):fragment);
        for(const key of [...params.keys()]) if(/token|auth|password|secret|cookie|session|signature|credential|api.?key/i.test(key)) {params.delete(key);removed=true;}
        const query=params.toString();
        url.hash=queryIndex>=0 ? path+(query?'?'+query:'') : query;
      }
    }
    if (removed) gaps.push(download ? '附件使用临时授权链接；请在链接失效前完成下载' : '页面地址中的凭据已隐藏');
    return url.href;
  };
  const pageUrl = new URL(location.href);
  const ids = query('id').map(node => node.getAttribute('data-bug-id') || node.getAttribute('data-ticket-id') || node.getAttribute('data-issue-id') || node.getAttribute('value') || text(node));
  const path = pageUrl.pathname.match(/\/(?:bugs?|tickets?|issues?)\/(\d+)(?:\/|$)/i);
  const hash = pageUrl.hash.match(/^#\/?(?:bugs?|tickets?|issues?)\/(\d+)(?:[/?]|$)/i);
  if (path) ids.push(path[1]);
  if (hash) ids.push(hash[1]);
  // The production iWork defect route is /development/project/defect?entityId=... .
  for (const key of ['bugId','bug_id','ticketId','ticket_id','issueId','issue_id','entityId']) ids.push(...pageUrl.searchParams.getAll(key));
  const numeric = [...new Set(ids.filter(id => /^\d+$/.test(id)))];
  if (!numeric.length) throw new Error('未找到明确的数字 Bug 编号，请在设置中配置编号字段');
  if (numeric.length > 1) throw new Error('页面中的 Bug 编号冲突，请检查配置');
  const visibleBody = String(document.body?.innerText ?? document.body?.textContent ?? '').trim();
  const facts = parseFacts(visibleBody);
  const hashTitle = (() => {
    try {
      const value = decodeURIComponent(pageUrl.hash.slice(1)).trim();
      return value && !/^\/?(?:bugs?|tickets?|issues?)\/\d+/i.test(value) && value.length < 300 ? value : '';
    } catch { return ''; }
  })();
  const title = bounded(text(nodes('title')[0]) || hashTitle || redact(document.title));
  const selectedDescription = nodes('description').map(text).join('\n\n');
  const factDescription = ['缺陷描述', '重现步骤', '期望结果', '实际结果']
    .filter(key => facts[key]).map(key => `${key}：${facts[key]}`).join('\n');
  const description = bounded(selectedDescription || factDescription || (facts['视频/日志信息'] ? visibleBody : ''));
  if (!description) gaps.push('未找到可见的问题正文，请配置正文选择器');
  if (!nodes('title').length) gaps.push('标题使用浏览器页面标题，请核对是否对应当前 Bug');
  const evidence = (type, attribute) => {
    const seen = new Set();
    return nodes(type).slice(0, 500).flatMap((node, index) => {
      try {
        const rawUrl = node.getAttribute(attribute) || node.getAttribute('data-url');
        if (!rawUrl) throw new Error('缺少下载链接');
        const url = cleanUrl(rawUrl, true);
        const name = node.getAttribute('download') || node.getAttribute('data-name') || node.getAttribute('alt') || String(node.innerText ?? node.textContent ?? '').trim();
        if (!name || name === '.' || name === '..' || /[\\/\x00-\x1f]/.test(name)) throw new Error('缺少有效原始文件名');
        const id = node.getAttribute('data-id') || `${type}-${index + 1}`;
        const key = `${id}\n${url}`;
        if (seen.has(key)) return [];
        seen.add(key);
        return [{id,name,url,transport:'browser'}];
      } catch (error) {gaps.push(`${type === 'image' ? '图片' : '附件'}未采集：${error.message}`);return [];}
    });
  };
  const fields = {};
  for (const node of nodes('field').slice(0, 100)) {
    const key = node.getAttribute('data-iwork-field') || node.getAttribute('data-name');
    if (key && !/password|secret|token|cookie|authorization/i.test(key)) fields[redact(key)] = bounded(text(node));
  }
  for (const [key, value] of Object.entries(facts)) if (!fields[key]) fields[key] = bounded(redact(value));
  const comments = nodes('comment').slice(0, 500).map((node,index) => {
    const comment = {id:node.getAttribute('data-id') || `comment-${index+1}`,text:bounded(text(node))};
    const time = node.getAttribute('data-time');
    if (time) comment.time = redact(time);
    return comment;
  });
  const attachments = evidence('attachment','href');
  const images = evidence('image','src');
  const groups = nodes('group').slice(0,100).map(node => {
    const group = {name:text(node)};
    const id=node.getAttribute('data-conversation-id');
    if (id) group.conversation_id=redact(id);
    return group;
  }).filter(group=>group.name);
  for (const group of parseGroups(visibleBody)) if (!groups.some(item => item.name === group.name)) groups.push(group);
  const file_mentions = nodes('mention').slice(0,100).map(node => {
    const mention = {name:node.getAttribute('data-name') || String(node.innerText ?? node.textContent ?? '').trim()};
    for (const key of ['group','target_id']) {const value=node.getAttribute(`data-${key.replace('_','-')}`);if(value) mention[key]=redact(value);}
    return mention;
  }).filter(mention=>mention.name);
  for (const mention of parseFileMentions(visibleBody)) if (!file_mentions.some(item => item.name === mention.name)) file_mentions.push(mention);
  if (!attachments.length) gaps.push('未发现可见附件；如有附件请配置选择器或展开后重试');
  for (const kind of ['comment','attachment','image','group','mention','field']) {
    const limit=['group','mention','field'].includes(kind)?100:500;
    if(nodes(kind).length>limit) gaps.push(`${kind} 条目超过采集数量限制，请分批补充`);
  }
  const snapshot={schema_version:1,bug_id:numeric[0],url:cleanUrl(location.href),title,description,fields,comments,attachments,images,groups,file_mentions,captured_at:new Date().toISOString(),coverage:{complete:false,gaps:[...new Set(gaps)]}};
  const bytes=()=>new TextEncoder().encode(JSON.stringify(snapshot)).byteLength;
  if(bytes()>700*1024) {
    snapshot.coverage.gaps.push('页面超过单次采集大小限制，已省略部分条目；请分批补充');
    let remaining=bytes();
    for(const key of ['comments','images','attachments','file_mentions','groups']) while(snapshot[key].length && remaining>690*1024) {
      const removed=snapshot[key].pop();
      remaining-=new TextEncoder().encode(JSON.stringify(removed)).byteLength;
    }
    for(const key of Object.keys(snapshot.fields).reverse()) {if(bytes()<=700*1024)break;delete snapshot.fields[key];}
    while(bytes()>700*1024 && snapshot.description.length) snapshot.description=snapshot.description.slice(0,Math.floor(snapshot.description.length/2));
    if(bytes()>700*1024) throw new Error('页面地址或标题过大，无法安全采集');
  }
  return snapshot;
}
