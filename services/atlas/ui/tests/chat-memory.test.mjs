import test from 'node:test';
import assert from 'node:assert/strict';
import { build } from 'esbuild';

const { outputFiles } = await build({ stdin: { contents: `
  export * from './src/auth/session';
  export * from './src/auth/chatMemory';
`, resolveDir: process.cwd() }, bundle: true, write: false, format: 'esm', platform: 'node' });
const { setSession, clearSession, saveChatMemory, loadChatMemory } = await import(
  `data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);
const storage = new Map();
globalThis.sessionStorage = { getItem: key => storage.get(key) ?? null,
  setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) };
const user = { email: 'one@test', tenant: 'EVAM', userId: 'one', idToken: 'token', roles: ['BDRM'] };
const conversation = { messages: [{ role: 'user', content: 'My deals', complete: true }], input: '', error: '' };

test('login-scoped memory survives token renewal and clears on logout and account changes', () => {
  setSession(user);
  saveChatMemory(user, conversation);
  setSession({ ...user, idToken: 'renewed' });
  assert.deepEqual(loadChatMemory(user).messages, conversation.messages);
  clearSession();
  setSession(user);
  assert.deepEqual(loadChatMemory(user).messages, []);
  for (const field of ['email', 'tenant', 'userId']) {
    setSession(user);
    saveChatMemory(user, conversation);
    setSession({ ...user, [field]: 'other' });
    assert.deepEqual(loadChatMemory(user).messages, []);
  }
});

test('storage errors discard stale saved history and report failure', () => {
  saveChatMemory(user, conversation);
  const setItem = sessionStorage.setItem;
  sessionStorage.setItem = () => { throw new Error('Quota exceeded'); };
  try { assert.equal(saveChatMemory(user, conversation), false); }
  finally { sessionStorage.setItem = setItem; }
  assert.deepEqual(loadChatMemory(user).messages, []);
});
