import test, { afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'https://prism.test/ui/', pretendToBeVisual: true });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.sessionStorage = dom.window.sessionStorage;
afterEach(() => sessionStorage.clear());
Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true });
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.DocumentFragment = dom.window.DocumentFragment;
globalThis.getComputedStyle = dom.window.getComputedStyle;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.requestAnimationFrame = dom.window.requestAnimationFrame.bind(dom.window);
globalThis.cancelAnimationFrame = dom.window.cancelAnimationFrame.bind(dom.window);
dom.window.HTMLElement.prototype.scrollIntoView = () => {};
const React = (await import('react')).default;
const { render, fireEvent, screen, waitFor, cleanup, act } = await import('@testing-library/react');

await mkdir('node_modules/.cache', { recursive: true });
const outfile = resolve(`node_modules/.cache/chat-test-${process.pid}.mjs`);
await build({ entryPoints: ['src/pages/Chat/ChatPage.tsx'], bundle: true, platform: 'node', format: 'esm',
  outfile, packages: 'external', jsx: 'automatic', define: { 'import.meta.env.BASE_URL': '"/ui/"' }, plugins: [{ name: 'session-fixture', setup(build) {
    build.onResolve({ filter: /auth\/AuthContext|api\/axiosClient/ }, args => ({ path: args.path, namespace: 'fixture' }));
    build.onLoad({ filter: /.*/, namespace: 'fixture' }, args => ({ contents: args.path.includes('AuthContext')
      ? 'export const useAuth = () => globalThis.chatAuth;'
      : 'export const PRISM_BASE_URL = "";', loader: 'js' }));
  } }] });
const ChatPage = (await import(pathToFileURL(outfile).href)).default;
const encoder = new TextEncoder();
const session = () => ({ email: 'reader@test', userId: 'one', tenant: 'EVAM', idToken: 'verified', roles: ['BDRM'] });

function mockFetch(t, { pending = false, tables = undefined } = {}) {
  let signal;
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    if (url.endsWith('/models')) return Response.json({ data: [{ id: 'prism-chitti' }] });
    signal = init.signal;
    if (pending) return new Promise((resolve, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
    });
    const chunk = JSON.stringify({ choices: [{ delta: { content: 'There are 12 active leads. <img src=x onerror=alert(1)>' } }],
      chitti: { result_table: [{ entity_id: 'SECRET-ID' }], tables, evidence: [{ reference: 'E1', label: '12 records' }] } });
    return new Response(new ReadableStream({ start(c) {
      c.enqueue(encoder.encode(`data: ${chunk}\n\ndata: [DONE]\n\n`)); c.close();
    } }), { headers: { 'Content-Type': 'text/event-stream', 'X-Request-ID': 'support-123' } });
  });
  return () => signal;
}
function ask() {
  fireEvent.change(screen.getByLabelText('Your question'), { target: { value: 'How many active leads?' } });
  fireEvent.keyDown(screen.getByLabelText('Your question'), { key: 'Enter' });
}

test('keyboard send, safe text, evidence, request ID, reset', async t => {
  t.after(cleanup);
  globalThis.chatAuth = { session: session(), signOut() {} };
  mockFetch(t);
  render(React.createElement(ChatPage));
  ask();
  await screen.findByText(/There are 12 active leads/);
  assert.equal(document.querySelector('img'), null);
  assert.equal(screen.queryByText(/SECRET-ID/), null);
  assert.ok(screen.getByText('[E1] 12 records'));
  assert.ok(screen.getByText('Request ID: support-123'));
  fireEvent.click(screen.getByText('New conversation'));
  assert.equal(screen.queryByText(/There are 12 active leads/), null);
  assert.equal(window.localStorage.length, 0);
  assert.equal(window.sessionStorage.length, 0);
});

test('stop cancels the in-flight request', async t => {
  t.after(cleanup);
  globalThis.chatAuth = { session: session(), signOut() {} };
  const signal = mockFetch(t, { pending: true });
  render(React.createElement(ChatPage));
  ask();
  await waitFor(() => assert.ok(signal()));
  fireEvent.click(screen.getByText('Stop answer'));
  assert.ok(signal().aborted);
  assert.ok(screen.getByText('Answer stopped.'));
});

for (const changed of ['email', 'tenant', 'userId']) {
  test(`${changed} change clears the conversation and cancels work`, async t => {
    t.after(cleanup);
    globalThis.chatAuth = { session: session(), signOut() {} };
    const signal = mockFetch(t, { pending: true });
    const view = render(React.createElement(ChatPage));
    ask();
    await waitFor(() => assert.ok(signal()));
    globalThis.chatAuth = { ...globalThis.chatAuth, session: { ...session(), [changed]: 'different' } };
    view.rerender(React.createElement(ChatPage));
    assert.ok(signal().aborted);
    assert.equal(screen.queryByText('How many active leads?'), null);
  });
}

test('sign-out clears answers and cancels pending work', async t => {
  t.after(cleanup);
  globalThis.chatAuth = { session: session(), signOut() {} };
  const signal = mockFetch(t, { pending: true });
  const view = render(React.createElement(ChatPage));
  ask();
  await waitFor(() => assert.ok(signal()));
  globalThis.chatAuth = { ...globalThis.chatAuth, session: null };
  view.rerender(React.createElement(ChatPage));
  assert.ok(signal().aborted);
  assert.equal(screen.queryByText('How many active leads?'), null);
});

test('all 75 business records are paginated and link to their deal profiles', async t => {
  t.after(cleanup);
  globalThis.chatAuth = { session: session(), signOut() {} };
  mockFetch(t, { tables: [{ title: 'Matching records', columns: ['Company', 'Temperature'],
    total: 75, partial: false, rows: Array.from({ length: 75 }, (_, i) => ({
      values: [`Company ${i + 1}`, 'Hot'], deal: `DEAL-${i + 1}`, url: 'javascript:alert(1)',
    })) }] });
  render(React.createElement(ChatPage));
  ask();
  await screen.findByText('Matching records (75)');
  assert.equal(screen.getByRole('link', { name: 'Open deal DEAL-1' }).getAttribute('href'), '/ui/deals?company=DEAL-1');
  assert.equal(screen.queryByText('Company 75'), null);
  fireEvent.click(screen.getByRole('button', { name: 'Go to next page' }));
  fireEvent.click(screen.getByRole('button', { name: 'Go to next page' }));
  assert.ok(screen.getByText('Company 75'));
  assert.equal(screen.getByRole('link', { name: 'Open deal DEAL-75' }).getAttribute('href'), '/ui/deals?company=DEAL-75');
  assert.equal(screen.queryByText('SECRET-ID'), null);
  fireEvent.click(screen.getByText('New conversation'));
  assert.equal(screen.queryByRole('table'), null);
});


for (const ending of ['answer', 'stop', 'reset']) {
  test(`live progress changes before the answer and clears on ${ending}`, async t => {
    t.after(cleanup);
    globalThis.chatAuth = { session: session(), signOut() {} };
    let stream;
    t.mock.method(globalThis, 'fetch', async url => {
      if (url.endsWith('/models')) return Response.json({ data: [{ id: 'prism-chitti' }] });
      return new Response(new ReadableStream({ start(controller) { stream = controller; } }),
        { headers: { 'Content-Type': 'text/event-stream' } });
    });
    render(React.createElement(ChatPage));
    assert.equal(screen.queryByText(/For example:/), null);
    ask();
    await waitFor(() => assert.ok(stream));
    for (const description of ['Understanding your question…', 'Finding and calculating the results…']) {
      await act(async () => stream.enqueue(encoder.encode(`data: ${JSON.stringify({
        event: { type: 'status', data: { description } },
      })}\n\n`)));
      assert.equal(screen.getByRole('status').textContent, description);
    }
    if (ending === 'stop') fireEvent.click(screen.getByText('Stop answer'));
    if (ending === 'reset') fireEvent.click(screen.getByText('New conversation'));
    await act(async () => {
      stream.enqueue(encoder.encode('data: {"choices":[{"delta":{"content":"There are 12 leads."}}]}\n\ndata: [DONE]\n\n'));
    });
    assert.equal(screen.queryByRole('status'), null);
    assert.equal(Boolean(screen.queryByText('There are 12 leads.')), ending === 'answer');
    assert.equal(screen.queryByText('Finding and calculating the results…'), null);
  });
}


test('returning to chat restores answers, records and drafts; reset stays cleared', async t => {
  t.after(cleanup);
  globalThis.chatAuth = { session: session(), signOut() {} };
  mockFetch(t, { tables: [{ title: 'Matching records', columns: ['Company'], total: 1,
    partial: false, rows: [{ values: ['Acme'], deal: 'ACME' }] }] });
  let view = render(React.createElement(ChatPage));
  ask();
  await screen.findByRole('table');
  fireEvent.change(screen.getByLabelText('Your question'), { target: { value: 'Tell me more' } });
  view.unmount();
  view = render(React.createElement(ChatPage));
  assert.ok(screen.getByText(/There are 12 active leads/));
  assert.ok(screen.getByRole('link', { name: 'Open deal ACME' }));
  assert.equal(screen.getByLabelText('Your question').value, 'Tell me more');
  fireEvent.click(screen.getByText('New conversation'));
  view.unmount();
  render(React.createElement(ChatPage));
  assert.equal(screen.queryByRole('table'), null);
  assert.equal(screen.queryByText(/There are 12 active leads/), null);
  assert.equal(screen.getByLabelText('Your question').value, '');
});

test('leaving an active request cancels it and restores it as stopped', async t => {
  t.after(cleanup);
  globalThis.chatAuth = { session: session(), signOut() {} };
  const signal = mockFetch(t, { pending: true });
  const view = render(React.createElement(ChatPage));
  ask();
  await waitFor(() => assert.ok(signal()));
  view.unmount();
  assert.ok(signal().aborted);
  render(React.createElement(ChatPage));
  assert.ok(screen.getByText('How many active leads?'));
  assert.ok(screen.getByText('Answer stopped.'));
  assert.equal(screen.queryByRole('status'), null);
});

function storedConversation() {
  const messages = Array.from({ length: 3 }, (_, i) => [
    { role: 'user', content: `Question ${i + 1}`, complete: true },
    { role: 'assistant', content: `Answer ${i + 1}`, complete: true, requestId: `old-${i + 1}`,
      evidence: [{ reference: 'E1', label: `Evidence ${i + 1}` }],
      tables: [{ title: `Records ${i + 1}`, columns: ['Company'], total: 1, partial: false,
        rows: [{ values: [`Company ${i + 1}`], deal: `DEAL-${i + 1}` }] }] },
  ]).flat();
  sessionStorage.setItem('atlas.chitti.conversation', JSON.stringify({
    owner: JSON.stringify(['EVAM', 'reader@test', 'one']), messages, input: 'Unsent follow-up', error: '',
  }));
  return messages;
}

for (const number of [1, 2, 3]) {
  test(`editing question ${number} sends preceding history and replaces later answers and IDs`, async t => {
    t.after(cleanup);
    globalThis.chatAuth = { session: session(), signOut() {} };
    const original = storedConversation();
    mockFetch(t);
    const view = render(React.createElement(ChatPage));
    fireEvent.click(screen.getByRole('button', { name: `Edit question ${number}` }));
    fireEvent.change(screen.getByLabelText('Edit question'), { target: { value: 'Corrected question' } });
    fireEvent.click(screen.getByRole('button', { name: 'Edit and send' }));
    await screen.findByText('Request ID: support-123');
    const request = fetch.mock.calls.find(call => call.arguments[0].endsWith('/completions'));
    assert.deepEqual(JSON.parse(request.arguments[1].body).messages, [
      ...original.slice(0, (number - 1) * 2).map(({ role, content }) => ({ role, content })),
      { role: 'user', content: 'Corrected question' },
    ]);
    for (let i = 1; i <= 3; i++) {
      assert.equal(Boolean(screen.queryByText(`Request ID: old-${i}`)), i < number);
      assert.equal(Boolean(screen.queryByText(`Evidence ${i}`, { exact: false })), i < number);
      assert.equal(Boolean(screen.queryByRole('link', { name: `Open deal DEAL-${i}` })), i < number);
    }
    assert.equal(screen.getByLabelText('Your question').value, 'Unsent follow-up');
    view.unmount();
    render(React.createElement(ChatPage));
    assert.ok(screen.getByText('Corrected question'));
    assert.ok(screen.getByText('Request ID: support-123'));
    assert.equal(screen.queryByText(`Question ${number}`), null);
  });
}

test('blank edit, cancel, Escape and reset do not send requests', async t => {
  t.after(cleanup);
  globalThis.chatAuth = { session: session(), signOut() {} };
  storedConversation();
  const fetch = t.mock.method(globalThis, 'fetch', () => { throw Error('must not send'); });
  render(React.createElement(ChatPage));
  fireEvent.click(screen.getByRole('button', { name: 'Edit question 2' }));
  fireEvent.change(screen.getByLabelText('Edit question'), { target: { value: ' ' } });
  assert.ok(screen.getByRole('button', { name: 'Edit and send' }).disabled);
  fireEvent.keyDown(screen.getByLabelText('Edit question'), { key: 'Enter' });
  fireEvent.click(screen.getByText('Cancel edit'));
  assert.ok(screen.getByText('Question 2'));
  assert.ok(screen.getByText('Answer 3'));
  fireEvent.click(screen.getByRole('button', { name: 'Edit question 1' }));
  fireEvent.keyDown(screen.getByLabelText('Edit question'), { key: 'Escape' });
  assert.equal(screen.queryByLabelText('Edit question'), null);
  fireEvent.click(screen.getByRole('button', { name: 'Edit question 3' }));
  fireEvent.click(screen.getByText('New conversation'));
  assert.equal(screen.queryByLabelText('Edit question'), null);
  assert.equal(screen.queryByText('Answer 3'), null);
  assert.equal(fetch.mock.calls.length, 0);
});

test('failed edited request retains the edit and new failure ID without reviving discarded answers', async t => {
  t.after(cleanup);
  globalThis.chatAuth = { session: session(), signOut() {} };
  storedConversation();
  t.mock.method(globalThis, 'fetch', async url => url.endsWith('/models')
    ? Response.json({ data: [{ id: 'prism-chitti' }] })
    : new Response('upstream failure', { status: 503, headers: { 'X-Request-ID': 'new-failure' } }));
  const view = render(React.createElement(ChatPage));
  fireEvent.click(screen.getByRole('button', { name: 'Edit question 2' }));
  fireEvent.change(screen.getByLabelText('Edit question'), { target: { value: 'Edited request' } });
  fireEvent.keyDown(screen.getByLabelText('Edit question'), { key: 'Enter' });
  await screen.findByText('Request ID: new-failure');
  assert.ok(screen.getByRole('alert'));
  view.unmount();
  render(React.createElement(ChatPage));
  assert.ok(screen.getByText('Edited request'));
  assert.ok(screen.getByText('Request ID: old-1'));
  assert.equal(screen.queryByText('Answer 2'), null);
  assert.equal(screen.queryByText('Question 3'), null);
});
