export const NATIVE_HOST = 'com.iwork.collector';

export function redactError(value) {
  return String(value || '').replace(/((?:token|auth(?:orization)?|password|secret|cookie|session|signature|credential|api.?key)[\w-]*\s*[:=]\s*)[^\s&;,]+/gi,'$1[已隐藏]').replace(/Bearer\s+[A-Za-z0-9._~+/=-]+/gi,'Bearer [已隐藏]');
}

export function extractBugId(pageUrl, dataIds = []) {
  const url = new URL(pageUrl);
  const candidates = [...dataIds];
  const path = url.pathname.match(/\/(?:bugs?|tickets?|issues?)\/(\d+)(?:\/|$)/i);
  if (path) candidates.push(path[1]);
  // iWork's real defect page uses entityId for the numeric defect identity.
  for (const key of ['bugId', 'bug_id', 'ticketId', 'ticket_id', 'issueId', 'issue_id', 'entityId']) candidates.push(...url.searchParams.getAll(key));
  // Some single-page ticket applications keep the trusted route in the fragment.
  const hash = url.hash.match(/^#\/?(?:bugs?|tickets?|issues?)\/(\d+)(?:[/?]|$)/i);
  if (hash) candidates.push(hash[1]);
  const valid = [...new Set(candidates.filter(value => typeof value === 'string' && /^\d+$/.test(value)))];
  if (!valid.length) throw new Error('未找到明确的数字 Bug 编号，请在设置中配置编号字段');
  if (valid.length > 1) throw new Error('页面中的 Bug 编号冲突，请检查配置');
  return valid[0];
}

export function safeName(value) {
  if (typeof value !== 'string' || !value || value === '.' || value === '..' || /[\\/\x00-\x1f]/.test(value)) throw new Error('附件文件名无效');
  return value;
}

export function safeUrl(value, base) {
  if (typeof value !== 'string' || !value.trim()) throw new Error('附件链接无效');
  const url = new URL(value, base);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('附件链接必须是 HTTP 或 HTTPS');
  for (const key of [...url.searchParams.keys()]) if (/token|auth|password|secret|cookie|session|signature|credential|api.?key/i.test(key)) url.searchParams.delete(key);
  url.hash = '';
  return url.href;
}

export function normalizeCandidate(candidate, base) {
  if (!candidate || typeof candidate.id !== 'string' || !candidate.id) throw new Error('附件编号无效');
  return {id:candidate.id, name:safeName(candidate.name), url:safeUrl(candidate.url, base), transport:'browser'};
}

export function nativeRequest(method, params = {}, id = crypto.randomUUID()) {
  return {id, method, params};
}

export function validateNativeResponse(response, id) {
  if (!response || response.id !== id || typeof response.ok !== 'boolean') throw new Error('本地助手响应格式不正确');
  if (!response.ok) {
    const error = new Error(response.error?.message || '本地助手处理失败');
    error.code = response.error?.code || 'NATIVE_ERROR';
    throw error;
  }
  if (!Object.hasOwn(response, 'result')) throw new Error('本地助手响应缺少结果');
  return response.result;
}

export function validateDownloadTask(task) {
  if (!task || !/^[A-Za-z0-9_-]+$/.test(task.task_id) || !/^[A-Za-z0-9_-]+$/.test(task.job_id)) throw new Error('下载任务路径无效');
  safeName(task.name);
  const expected = `iwork-collector/${task.job_id}/${task.task_id}/${task.name}`;
  if (task.relative_path !== expected) throw new Error('下载路径与登记路径不一致');
  const url = new URL(task.url);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new Error('下载链接无效');
  return task;
}
