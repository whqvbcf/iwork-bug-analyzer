// Read the authenticated iWork API from the page's MAIN world.  The function
// passed to chrome.scripting.executeScript is deliberately self-contained so
// Chromium can serialize it without importing extension modules.
import { enrichTicketClues } from './collector.mjs';

export async function collectIworkApiContext() {
  const pageUrl = new URL(location.href);
  const projectId = pageUrl.searchParams.get('businessId') || '';
  const itemId = pageUrl.searchParams.get('entityId') || '';
  if (!projectId || !itemId) return {ok: false, skipped: true, reason: '缺少 businessId 或 entityId'};

  const readText = value => {
    let hasMention = false;
    const mention = attrs => {
      const name = attrs?.name || attrs?.['data-name'] || attrs?.value || attrs?.label;
      if (typeof name !== 'string' || !name.trim()) return '';
      hasMention = true;
      return '@' + name.trim().replace(/^@+/, '');
    };
    const walk = node => {
      if (node == null) return '';
      if (typeof node === 'string') {
        const trimmed = node.trim();
        if ((trimmed.startsWith('[') || trimmed.startsWith('{')) && trimmed.length < 1000000) {
          try {
            const parsed = JSON.parse(trimmed);
            if (parsed && parsed !== node) return walk(parsed);
          } catch { /* ordinary text */ }
        }
        // HTML mentions may contain empty or nested leaves. Skip their displayed
        // children when a label is available so the name is emitted only once.
        let ignoredTag = '', ignoredDepth = 0;
        return (node.match(/<[^>]+>|[^<]+|</g) || []).map(token => {
          const tag = token.match(/^<\s*(\/?)\s*([\w-]+)/);
          if (ignoredDepth) {
            if (tag?.[2].toLowerCase() === ignoredTag) {
              if (tag[1]) ignoredDepth--;
              else if (!/\/\s*>$/.test(token)) ignoredDepth++;
            }
            return '';
          }
          if (!tag) return token;
          const name = tag[2].toLowerCase();
          if (!tag[1]) {
            const attrs = {};
            for (const attr of token.matchAll(/([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/g)) {
              attrs[attr[1].toLowerCase()] = attr[2] ?? attr[3] ?? attr[4] ?? '';
            }
            if (attrs['data-type'] === 'mention') {
              const label = mention(attrs);
              if (label) {
                if (!/\/\s*>$/.test(token)) { ignoredTag = name; ignoredDepth = 1; }
                return label;
              }
            }
          }
          return name === 'br' || (tag[1] && /^(p|div|li|tr|h[1-6])$/.test(name)) ? '\n' : '';
        }).join('');
      }
      if (typeof node === 'number' || typeof node === 'boolean') return String(node);
      if (Array.isArray(node)) {
        if (typeof node[0] === 'string' && node[1] && typeof node[1] === 'object' && !Array.isArray(node[1])) {
          const [tag, attrs, ...children] = node;
          if (attrs['data-type'] === 'mention' || tag === 'mention') {
            const label = mention(attrs);
            if (label) return label;
          }
          const text = children.map(walk).join('');
          return text + (/^(br|p|div|li|tr|h[1-6])$/i.test(tag) ? '\n' : '');
        }
        return node.map(walk).join('');
      }
      if (typeof node === 'object') {
        if (node.mention) return mention(node.mention);
        if (node.type === 'mention' || node['data-type'] === 'mention') return mention({...node, ...node.attrs});
        if (Array.isArray(node.ops)) return node.ops.map(walk).join('');
        for (const key of ['text', 'contentText', 'content', 'value', 'description', 'name', 'insert', 'plainText']) {
          if (node[key] != null) {
            const text = walk(node[key]);
            if (text) return text;
          }
        }
      }
      return '';
    };
    const text = walk(value).trim();
    return {text, hasMention};
  };
  const asText = value => readText(value).text;
  const envelopeData = payload => payload?.body?.data ?? payload?.data ?? payload?.result?.data ?? payload?.result ?? null;
  const listData = payload => {
    const value = envelopeData(payload);
    return Array.isArray(value) ? value : [];
  };
  const storageValue = (store, key) => {
    try { return store?.getItem(key) || ''; } catch { return ''; }
  };
  const authCandidates = ['jwt', 'token', 'access_token', 'accessToken', 'Authorization'];
  let rawToken = '';
  for (const key of authCandidates) {
    rawToken = storageValue(window.localStorage, key) || storageValue(window.sessionStorage, key);
    if (rawToken) break;
  }
  if (rawToken && typeof rawToken === 'string' && rawToken.trim().startsWith('{')) {
    try {
      const parsed = JSON.parse(rawToken);
      rawToken = parsed.value || parsed.token || parsed.accessToken || parsed.jwt || '';
    } catch { /* leave the original value for the normalizer */ }
  }
  rawToken = String(rawToken || '').trim();
  const authorization = rawToken ? (/^Bearer\s/i.test(rawToken) ? rawToken : `Bearer ${rawToken}`) : '';
  const headers = {
    accept: 'application/json',
    'content-type': 'application/json;charset=UTF-8',
    ...(authorization ? {authorization} : {}),
    useragent: 'pc',
    lang: 'zh-cn',
    qfcsid: 'BA-0208',
    qfctid: 'YQJT'
  };
  const fetchImpl = window.fetch || globalThis.fetch;
  const request = async (url, init = {}) => {
    if (typeof fetchImpl !== 'function') throw new Error('页面没有可用的 fetch');
    const response = await fetchImpl(url, {
      credentials: 'include', ...init,
      headers: {...headers, ...(init.headers || {})}
    });
    const text = await response.text();
    let body;
    try { body = text ? JSON.parse(text) : null; } catch { body = text; }
    if (!response.ok) throw new Error(`iWork 接口返回 HTTP ${response.status}`);
    if (!body || typeof body !== 'object') throw new Error('iWork 接口返回了非 JSON 响应');
    if (body.success === false) throw new Error(String(body.message || body.errMsg || body.errorMsg || 'iWork 接口返回失败'));
    return body;
  };
  const optional = async (label, action) => {
    try { return {label, body: await action(), error: ''}; }
    catch (error) { return {label, body: null, error: String(error?.message || error)}; }
  };
  const id = Number(itemId);
  try {
    const detail = await request('https://devops.faw.cn/api/collaboration-center/api/scrum/item/query', {
      method: 'POST',
      body: JSON.stringify({itemOperateSource: 'DAYU_PROJECT', businessProjectId: String(projectId), itemId: id, type: 'DEFECT', templateType: 'GENERAL'})
    });
    const [commentsResponse, changesResponse, relatedResponse, docsResponse] = await Promise.all([
      optional('comments', () => request(`https://devops.faw.cn/api/collaboration-center/api/scrum/comment/treeList?itemOperateSource=DAYU_PROJECT&businessProjectId=${encodeURIComponent(projectId)}&itemId=${encodeURIComponent(itemId)}&_t=${Date.now()}`)),
      optional('timeline', () => request(`https://devops.faw.cn/api/collaboration-center/api/item/changeRecord/list?itemOperateSource=DAYU_PROJECT&itemId=${encodeURIComponent(itemId)}&type=defect&businessProjectId=${encodeURIComponent(projectId)}&_t=${Date.now()}`)),
      optional('related', () => request(`https://devops.faw.cn/api/collaboration-center/api/scrum/related/item/list?itemOperateSource=DAYU_PROJECT&businessProjectId=${encodeURIComponent(projectId)}&itemId=${encodeURIComponent(itemId)}&_t=${Date.now()}`)),
      optional('docs', () => request('https://devops.faw.cn/api/document/internal/item/docRelatedList', {method: 'POST', body: JSON.stringify({itemOperateSource: 'DAYU_PROJECT', businessProjectId: String(projectId), itemId: id})}))
    ]);
    const detailValue = envelopeData(detail) || {};
    const images = [];
    const extractImages = (content, commentId = '') => {
      const add = attrs => {
        const rawUrl = attrs?.src || attrs?.url || attrs?.fileUrl || attrs?.downloadUrl;
        if (typeof rawUrl !== 'string' || !rawUrl.trim()) return;
        let url;
        try { url = new URL(rawUrl.replaceAll('&amp;', '&'), location.href); } catch { return; }
        if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return;
        let image = images.find(value => value.url === url.href);
        if (!image) {
          const base = decodeURIComponent(url.pathname.split('/').pop() || '');
          const name = String(attrs.name || attrs.fileName || (/\.(png|jpe?g|gif|webp|bmp)$/i.test(base) ? base : `comment-${commentId || 'description'}-${images.length + 1}.png`));
          image = {id: `api-image-${images.length + 1}`, url: url.href, name,
            transport: 'browser', page_fetch: true, comment_ids: []};
          images.push(image);
        }
        if (commentId && !image.comment_ids.includes(commentId)) image.comment_ids.push(commentId);
      };
      const walk = node => {
        if (typeof node === 'string') {
          try { const parsed = JSON.parse(node); if (parsed && typeof parsed === 'object') { walk(parsed); return; } } catch { /* HTML or Markdown */ }
          for (const match of node.matchAll(/<img\b([^>]+)>/gi)) {
            const attrs = {};
            for (const attr of match[1].matchAll(/([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/g)) {
              attrs[attr[1].toLowerCase()] = attr[2] ?? attr[3] ?? attr[4] ?? '';
            }
            add(attrs);
          }
          for (const match of node.matchAll(/!\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/g)) add({src: match[2]});
        } else if (Array.isArray(node)) {
          if (['img', 'image', 'picture'].includes(String(node[0]).toLowerCase())) add(node[1]);
          node.forEach(walk);
        } else if (node && typeof node === 'object') {
          if (/^(?:img|image|picture|photo)$/i.test(node.type || node.tag || '')) add({...node, ...node.attrs});
          if (typeof node.image === 'string') add({src: node.image});
          Object.values(node).forEach(value => { if (value && typeof value === 'object') walk(value); });
        }
      };
      walk(content);
    };
    extractImages(detailValue.description || detailValue.content);
    const comments = [];
    const walkComments = (items, parentId = '') => {
      if (!Array.isArray(items)) return;
      for (const comment of items) {
        if (!comment || typeof comment !== 'object') continue;
        const idValue = String(comment.id ?? comment.commentId ?? comment.createTime ?? `comment-${comments.length + 1}`);
        const rich = readText(comment.content);
        const text = rich.hasMention ? rich.text : asText(comment.contentText || comment.content || comment.text || '');
        extractImages(comment.content, idValue);
        extractImages(comment.contentText, idValue);
        if (text || idValue) comments.push({
          id: idValue,
          parent_id: String(comment.parentId ?? parentId ?? ''),
          author: String(comment.creatorName || comment.author || ''),
          time: String(comment.gmtCreated || comment.createTime || ''),
          text,
          image_ids: images.filter(image => image.comment_ids.includes(idValue)).map(image => image.id)
        });
        walkComments(comment.children, idValue);
      }
    };
    const changes = listData(changesResponse.body).map((change, index) => {
      const oldValue = change?.oldValue == null || change.oldValue === '' ? '（空）' : String(change.oldValue);
      const newValue = change?.newValue == null || change.newValue === '' ? '（空）' : String(change.newValue);
      return {
        id: String(change?.id || change?.changeId || change?.gmtCreated || `timeline-${index + 1}`),
        time: String(change?.gmtCreated || change?.createTime || ''),
        author: String(change?.creatorName || change?.author || ''),
        text: String(change?.changeRecordDesc || `${change?.originKeyName || '字段'}：${oldValue} → ${newValue}`)
      };
    });
    walkComments(listData(commentsResponse.body));
    const commentError = commentsResponse.error;
    const timelineError = changesResponse.error;
    return {
      ok: true,
      project_id: String(projectId),
      bug_id: String(itemId),
      title: asText(detailValue.title || detailValue.subject || detailValue.name),
      description: asText(detailValue.description || detailValue.content || ''),
      comments,
      images,
      timeline: changes,
      comment_coverage: {attempted: true, complete: !commentError, count: comments.length, gaps: commentError ? [`iWork 评论接口读取失败：${commentError}`] : []},
      timeline_coverage: {attempted: true, complete: !timelineError, count: changes.length, gaps: timelineError ? [`iWork 时间线接口读取失败：${timelineError}`] : []},
      api_coverage: {
        detail: true,
        comments: !commentError,
        timeline: !timelineError,
        related: !relatedResponse.error,
        docs: !docsResponse.error,
        gaps: [commentError, timelineError, relatedResponse.error, docsResponse.error].filter(Boolean)
      }
    };
  } catch (error) {
    return {ok: false, error: String(error?.message || error), project_id: String(projectId), bug_id: String(itemId)};
  }
}

export function mergeIworkApiContext(snapshot, context) {
  if (!context?.ok) {
    if (context?.error) snapshot.coverage.gaps.push(`iWork 接口采集失败：${context.error}`);
    return snapshot;
  }
  if (context.title) {
    snapshot.title = context.title;
    if (context.api_coverage?.detail) {
      snapshot.coverage.gaps = snapshot.coverage.gaps.filter(gap =>
        gap !== '标题使用浏览器页面标题，请核对是否对应当前 Bug');
    }
  }
  if (context.description) snapshot.description = context.description;
  if (Array.isArray(context.comments)) snapshot.comments = context.comments;
  if (Array.isArray(context.timeline)) snapshot.timeline = context.timeline;
  if (Array.isArray(context.images)) {
    snapshot.images ||= [];
    for (const image of context.images) {
      const old = snapshot.images.find(item => item.url === image.url);
      if (old) Object.assign(old, image);
      else snapshot.images.push(image);
    }
  }
  if (context.comment_coverage) snapshot.comment_coverage = context.comment_coverage;
  if (context.timeline_coverage) snapshot.timeline_coverage = context.timeline_coverage;
  if (context.api_coverage) snapshot.api_coverage = context.api_coverage;
  if (context.api_coverage?.detail) {
    const read = ['工单详情', ...(context.api_coverage.comments ? ['评论'] : []),
      ...(context.api_coverage.timeline ? ['时间线'] : [])];
    snapshot.coverage.gaps = snapshot.coverage.gaps.map(gap =>
      gap === '仅采集当前已显示的页面内容；未验证折叠区、分页、历史评论或登录权限覆盖'
        ? `已通过接口补采${read.join('、')}；附件、其他分页及权限覆盖仍未证实完整` : gap);
  }
  const gaps = [
    ...(context.comment_coverage?.gaps || []),
    ...(context.timeline_coverage?.gaps || [])
  ];
  snapshot.coverage.gaps.push(...gaps);
  return enrichTicketClues(snapshot);
}
