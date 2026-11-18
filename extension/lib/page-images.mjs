// Serialized into the ticket page. Session headers and image bytes stay in that
// renderer; native messaging receives only the browser's completed file path.
export async function fetchPageImage(imageUrl, expectedPageUrl = '') {
  try {
    const page = new URL(location.href);
    if (expectedPageUrl) {
      const expected = new URL(expectedPageUrl);
      if (page.origin !== expected.origin || ['businessId', 'entityId'].some(key => page.searchParams.get(key) !== expected.searchParams.get(key))) {
        throw new Error('原工单页面已切换，请重新打开工单采集图片');
      }
    }
    const target = new URL(imageUrl);
    if (!['https:', 'http:'].includes(target.protocol) || target.username || target.password) throw new Error('图片地址无效');
    const headers = {};
    if (target.protocol === 'https:' && ['iwork.faw.cn', 'devops.faw.cn'].includes(target.hostname) && (!target.port || target.port === '443')) {
      let token = '';
      for (const key of ['jwt', 'token', 'access_token', 'accessToken', 'Authorization']) {
        try { token = window.localStorage?.getItem(key) || window.sessionStorage?.getItem(key) || ''; } catch { /* inaccessible storage */ }
        if (token) break;
      }
      if (token.trim().startsWith('{')) {
        try { const value = JSON.parse(token); token = value.value || value.token || value.accessToken || value.jwt || ''; } catch { token = ''; }
      }
      if (token) headers.authorization = /^Bearer\s/i.test(token) ? token : `Bearer ${token}`;
    }
    const response = await window.fetch(target.href, {credentials: 'include', headers, signal: AbortSignal.timeout(30000)});
    if (!response.ok) throw new Error(`评论图片返回 HTTP ${response.status}`);
    const limit = 32 * 1024 * 1024;
    if (Number(response.headers.get('content-length')) > limit) throw new Error('评论图片超过 32 MiB');
    const chunks = [];
    let size = 0;
    const reader = response.body.getReader();
    try {
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > limit) throw new Error('评论图片超过 32 MiB');
        chunks.push(value);
      }
    } finally { await reader.cancel(); }
    const raw = new Blob(chunks);
    const signature = new Uint8Array(await raw.slice(0, 16).arrayBuffer());
    const ascii = String.fromCharCode(...signature);
    const mime = signature[0] === 137 && ascii.slice(1, 4) === 'PNG' ? 'image/png'
      : signature[0] === 255 && signature[1] === 216 && signature[2] === 255 ? 'image/jpeg'
      : /^GIF8[79]a/.test(ascii) ? 'image/gif'
      : ascii.startsWith('RIFF') && ascii.slice(8, 12) === 'WEBP' ? 'image/webp'
      : ascii.startsWith('BM') ? 'image/bmp' : '';
    if (!mime || !size) throw new Error('评论图片响应不是有效图片，可能登录已过期');
    const blob = new Blob(chunks, {type: mime});
    const url = URL.createObjectURL(blob);
    // Reclaimed on navigation, or after downloads have had five minutes to start.
    const expiry = setTimeout(() => URL.revokeObjectURL(url), 300000);
    expiry?.unref?.();
    return {ok: true, url};
  } catch (error) {
    return {ok: false, browser_fallback: error instanceof TypeError, error: String(error?.message || error)};
  }
}
