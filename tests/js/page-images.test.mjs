import test from 'node:test';
import assert from 'node:assert/strict';
import { fetchPageImage } from '../../extension/lib/page-images.mjs';

test('page image fetch uses the logged-in session, keeps authorization in the page, and rejects login HTML', async () => {
  globalThis.location = {href: 'https://iwork.faw.cn/development/project/defect?entityId=1'};
  const calls = [];
  globalThis.window = {
    localStorage: {getItem: key => key === 'jwt' ? 'page-token' : null},
    sessionStorage: {getItem: () => null},
    fetch: async (url, options) => {
      calls.push({url, options});
      return new Response(new Uint8Array([137,80,78,71,13,10,26,10]), {headers: {'content-type': 'image/png'}});
    }
  };
  const isolated = Function(`return (${fetchPageImage.toString()})`)();
  const result = await isolated('https://devops.faw.cn/image/1');
  assert.equal(result.ok, true);
  assert.match(result.url, /^blob:/);
  assert.equal(calls[0].options.credentials, 'include');
  assert.equal(calls[0].options.headers.authorization, 'Bearer page-token');
  assert.ok(!JSON.stringify(result).includes('page-token'));
  URL.revokeObjectURL(result.url);
  await isolated('https://cdn.example.test/image.png');
  assert.equal(calls[1].options.headers.authorization, undefined);
  window.fetch = async () => new Response('<html>login</html>', {headers: {'content-type': 'text/html'}});
  assert.equal((await isolated('https://devops.faw.cn/image/1')).ok, false);
  window.fetch = async () => new Response(new Uint8Array([137,80,78,71,13,10,26,10]), {headers: {'content-type': 'application/octet-stream'}});
  const attachment = await isolated('https://devops.faw.cn/image/1');
  assert.equal(attachment.ok, true);
  URL.revokeObjectURL(attachment.url);
  window.fetch = async () => new Response('<html>login</html>', {headers: {'content-type': 'image/png'}});
  assert.equal((await isolated('https://devops.faw.cn/image/1')).ok, false);
  window.fetch = async () => { throw new TypeError('Failed to fetch'); };
  assert.equal((await isolated('https://cdn.example.test/image.png')).browser_fallback, true);
});
