import test from 'node:test';
import assert from 'node:assert/strict';
import { build } from 'esbuild';

const { outputFiles } = await build({ entryPoints: ['src/pages/Chat/chatClient.ts'], bundle: true,
  write: false, format: 'esm', platform: 'node' });
const { readCompletion, complete, discoverModel } = await import(
  `data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

function response(parts, cancel = () => {}) {
  return new Response(new ReadableStream({
    start(controller) { for (const part of parts) controller.enqueue(part); controller.close(); }, cancel,
  }), { headers: { 'Content-Type': 'text/event-stream', 'X-Request-ID': 'req-1' } });
}
const bytes = text => new TextEncoder().encode(text);

test('split UTF-8 and CRLF SSE, evidence and request IDs', async () => {
  const data = bytes(': heartbeat\r\ndata: {"choices":[{"delta":{"content":"₹12"}}]}\r\n\r\n'
    + 'data: {"chitti":{"evidence":[{"reference":"E1","label":"12 records","record_id":"private"}],'
    + '"result_table":[{"private":"secret"}]}}\r\n\r\ndata: [DONE]\r\n\r\n');
  const updates = [];
  await readCompletion(response([...data].map(value => new Uint8Array([value]))), u => updates.push(u));
  assert.equal(updates.map(u => u.text || '').join(''), '₹12');
  assert.equal(updates[0].requestId, 'req-1');
  assert.deepEqual(updates.at(-1).evidence, [{ reference: 'E1', label: '12 records' }]);
  assert.ok(!JSON.stringify(updates).includes('private'));
});

test('missing DONE and malformed data fail visibly', async () => {
  for (const data of ['data: {"choices":[]}\n\n', 'data: malformed\n\n']) {
    await assert.rejects(readCompletion(response([bytes(data)]), () => {}), /interrupted/);
  }
});

test('request uses only bearer/tenant; upstream error details are hidden', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    calls.push({ url, init });
    return new Response('private SQL schema information', { status: 503, headers: { 'X-Request-ID': 'req-2' } });
  });
  await assert.rejects(complete('', { tenant: 'EVAM', idToken: 'token' }, 'prism-chitti',
    [{ role: 'user', content: 'Hello' }], new AbortController().signal, () => {}), error => {
    assert.equal(error.requestId, 'req-2');
    assert.ok(!error.message.includes('SQL'));
    return true;
  });
  assert.equal(calls[0].url, '/chitti/v1/chat/completions');
  assert.deepEqual(calls[0].init.headers, { Authorization: 'Bearer token', 'X-Tenant': 'EVAM',
    'Content-Type': 'application/json' });
  assert.equal(calls[0].init.credentials, 'omit');
});

test('a tokenless session never sends a chat request', async t => {
  const fetch = t.mock.method(globalThis, 'fetch', () => { throw Error('must not fetch'); });
  await assert.rejects(discoverModel('', { idToken: '', tenant: 'EVAM' }, new AbortController().signal), /Sign in/);
  assert.equal(fetch.mock.calls.length, 0);
});

test('abort signal reaches fetch and is preserved', async t => {
  const controller = new AbortController();
  t.mock.method(globalThis, 'fetch', async (_, init) => {
    assert.equal(init.signal, controller.signal);
    throw new DOMException('Aborted', 'AbortError');
  });
  controller.abort();
  await assert.rejects(complete('', { idToken: 'token', tenant: 'EVAM' }, 'prism-chitti', [],
    controller.signal, () => {}), { name: 'AbortError' });
});


test('server progress is preserved verbatim, including long descriptions', async () => {
  const description = 'Matched rm_id → Shubh Dave.\n' + 'Details. '.repeat(40);
  const updates = [];
  await readCompletion(response([bytes(`data: ${JSON.stringify({ event: {
    type: 'status', data: { description },
  } })}\n\ndata: [DONE]\n\n`)]), update => updates.push(update));
  assert.deepEqual(updates.filter(update => update.progress), [{ progress: description }]);
});
