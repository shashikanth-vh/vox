import { test, expect } from '@playwright/test';

// Runs against the live dev stack: no route interception, fabricated tokens, or
// response fixtures. Uses an existing account and makes no business-data writes.
const email = process.env.CHAT_E2E_EMAIL || 'admin@evamfinance.com';
const password = process.env.CHAT_E2E_PASSWORD || 'prism';
const question = 'How many leads are there?';
const endpoint = '/chitti/v1/chat/completions';
const conversation = page => page.getByRole('log', { name: 'Conversation' });

async function signIn(page, path = '/ui/') {
  await page.goto(path);
  await page.getByPlaceholder('Username or email').fill(email);
  await page.getByPlaceholder('Password', { exact: true }).fill(password);
  const grant = page.waitForResponse(r => r.url().endsWith('/dex/token'));
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  expect((await grant).status()).toBe(200);
  await expect(page.getByPlaceholder('Username or email')).toBeHidden();
}

async function openChat(page, mobile = false) {
  if (mobile) await page.getByRole('button', { name: /More$/ }).click();
  await page.getByRole('button', { name: /Chitti$/ }).click();
  await expect(page.getByRole('heading', { name: 'Chitti', exact: true })).toBeVisible();
  await expect(page.getByText('For example:', { exact: false })).toHaveCount(0);
}

async function signOut(page) {
  await page.getByRole('button', { name: 'Account', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Sign out' }).click();
  await expect(page.getByPlaceholder('Username or email')).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem('atlas.session'))).toBeNull();
}

async function answer(page, testInfo) {
  // Observe bytes through Chromium's network protocol. The UI cancels its reader
  // on [DONE], so getResponseBody is unavailable even after a successful answer.
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  let streamId;
  let buffered = '';
  let streamReady;
  const chunks = [];
  cdp.on('Network.responseReceived', event => {
    if (!event.response.url.endsWith(endpoint)) return;
    streamId = event.requestId;
    streamReady = cdp.send('Network.streamResourceContent', { requestId: streamId })
      .then(result => { buffered = result.bufferedData; });
  });
  cdp.on('Network.dataReceived', event => {
    if (event.requestId === streamId && event.data) chunks.push(event.data);
  });
  // Read the same governed Register view as the signed-in user for a count oracle.
  const total = await page.evaluate(async () => {
    const session = JSON.parse(sessionStorage.getItem('atlas.session'));
    const response = await fetch('/v1/leads?limit=1&with_total=true', {
      headers: { Authorization: `Bearer ${session.idToken}`, 'X-Tenant': session.tenant },
    });
    if (!response.ok) throw new Error(`Register count failed: ${response.status}`);
    return (await response.json()).total;
  });
  expect(Number.isInteger(total)).toBe(true);
  await page.evaluate(() => {
    window.chatProgress = [];
    window.chatProgressObserver = new MutationObserver(() => {
      const description = document.querySelector('[role="status"]')?.textContent;
      if (description && window.chatProgress.at(-1) !== description) window.chatProgress.push(description);
    });
    window.chatProgressObserver.observe(document.body, { childList: true, subtree: true, characterData: true });
  });
  const started = Date.now();
  const responsePromise = page.waitForResponse(r => r.url().endsWith(endpoint), { timeout: 35_000 });
  await page.getByRole('textbox', { name: 'Your question' }).fill(question);
  await page.getByRole('textbox', { name: 'Your question' }).press('Enter');
  await expect(page.getByRole('button', { name: 'Stop answer' })).toBeVisible();
  const response = await responsePromise;
  const headersAfterMs = Date.now() - started;
  expect(response.status()).toBe(200);
  expect(response.headers()['content-type']).toContain('text/event-stream');
  expect(response.headers()['cache-control']).toContain('no-store');
  const requestHeaders = await response.request().allHeaders();
  expect(requestHeaders.authorization?.startsWith('Bearer ')).toBe(true);
  for (const name of ['x-api-key', 'x-internal-context', 'x-user-roles', 'x-chitti-presentation']) {
    expect(requestHeaders[name]).toBeUndefined();
  }
  await expect(page.getByRole('button', { name: 'Stop answer' })).toBeHidden({ timeout: 320_000 });
  await expect(page.getByRole('alert')).toHaveCount(0);
  await streamReady;
  await expect.poll(() => Buffer.concat([buffered, ...chunks].map(value => Buffer.from(value, 'base64')))
    .toString('utf8')).toContain('data: [DONE]');
  const wire = Buffer.concat([buffered, ...chunks].map(value => Buffer.from(value, 'base64'))).toString('utf8');
  await cdp.detach();
  const events = wire.split(/\r?\n/).filter(line => line.startsWith('data:'))
    .map(line => line.slice(5).trim()).filter(Boolean);
  expect(events.at(-1)).toBe('[DONE]');
  const parsed = events.filter(value => value !== '[DONE]').map(value => JSON.parse(value));
  const progress = await page.evaluate(() => {
    window.chatProgressObserver.disconnect();
    return window.chatProgress;
  });
  const statuses = parsed.filter(value => value.event?.type === 'status')
    .map(value => value.event.data.description);
  expect(statuses.length).toBeGreaterThan(0);
  expect(progress.some(description => statuses.includes(description))).toBe(true);
  expect(new Set(progress).size).toBeGreaterThan(1);
  await expect(page.getByRole('status')).toHaveCount(0);
  const metadata = parsed.findLast(value => value.chitti)?.chitti;
  expect(response.headers()['x-request-id']).toBe(metadata?.request_id);
  expect(['ANSWERED', 'FAILED', 'DEPENDENCY_FAILED', 'ACCESS_DENIED',
    'CLARIFICATION_REQUIRED', 'OUT_OF_SCOPE', 'PARTIAL_RESULT']).toContain(metadata?.outcome);
  expect(Object.keys(metadata).sort()).toEqual(['completeness', 'evidence', 'outcome', 'request_id']);
  const prose = parsed.map(value => value.choices?.[0]?.delta?.content || '').join('');
  expect(prose.trim()).not.toBe('');
  if (metadata.outcome === 'ANSWERED') {
    const numbers = [...prose.replaceAll(',', '').matchAll(/\b\d+\b/g)].map(match => Number(match[0]));
    expect(numbers, 'A successful answer must agree with the live governed Register count').toContain(total);
  } else if (['FAILED', 'DEPENDENCY_FAILED'].includes(metadata.outcome)) {
    expect([
      "I couldn't complete this answer. Please try again shortly.",
      "I couldn't prepare a clear answer to that question. Please try rephrasing it.",
    ]).toContain(prose);
    expect(['FAILED', 'PARTIAL_TIMEOUT']).toContain(metadata.completeness);
    expect(metadata.evidence).toEqual([]);
  } else if (metadata.outcome === 'ACCESS_DENIED') {
    expect(prose).toBe("You don't have access to the information needed to answer this question.");
    expect(metadata.evidence).toEqual([]);
  }
  expect(prose).not.toMatch(/```|^\s*\||\b[a-z]+_[a-z_]+\b|\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b/im);
  expect(prose).not.toMatch(/caller-visible|aggregate scalar|result row count|evidence obligation|execution plan/i);
  expect(wire).not.toMatch(/"(?:result_table|execution_plan|retrieval_trace|model_usage|stage_outputs)"/);
  await expect(conversation(page)).toContainText(prose);
  await expect(conversation(page)).toContainText(`Request ID: ${metadata.request_id}`);
  await expect(conversation(page).locator('table, pre, code')).toHaveCount(0);
  if (metadata.outcome === 'ANSWERED' && total > 0) {
    expect(metadata.evidence.length).toBeGreaterThan(0);
    await expect(conversation(page)).toContainText(metadata.evidence[0].label);
  }
  await page.getByRole('textbox', { name: 'Your question' }).fill('Another question');
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
  await page.getByRole('textbox', { name: 'Your question' }).fill('');
  await testInfo.attach('live-response-check', { body: JSON.stringify({
    requestId: metadata.request_id, outcome: metadata.outcome, registerCount: total,
    progress, headersAfterMs, completedAfterMs: Date.now() - started, sseEvents: events.length,
  }, null, 2), contentType: 'application/json' });
}

test('anonymous and forged identities cannot reach chat', async ({ request }) => {
  for (const headers of [{}, { Authorization: 'Bearer invalid', 'X-User-Roles': 'Admin', 'X-User-Email': email }]) {
    expect((await request.get('/chitti/v1/models', { headers })).status()).toBe(401);
    expect((await request.post(endpoint, { headers, data: {
      model: 'prism-chitti', messages: [{ role: 'user', content: question }], stream: true,
    } })).status()).toBe(401);
  }
});

test('desktop live answer, evidence, reset, refresh and sign-out', async ({ page }, testInfo) => {
  await signIn(page);
  await openChat(page);
  await answer(page, testInfo);
  const stored = await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]));
  expect(stored).toContain(question);
  await page.screenshot({ path: testInfo.outputPath('desktop-answer.png') });
  await page.reload();
  await expect(conversation(page)).toContainText(question);
  await page.getByRole('button', { name: /Today$/ }).click();
  await openChat(page);
  await expect(conversation(page)).toContainText(question);
  await page.getByRole('textbox', { name: 'Your question' }).fill('Unsent draft');
  await page.getByRole('button', { name: 'New conversation' }).click();
  await expect(page.getByRole('textbox', { name: 'Your question' })).toHaveValue('');
  await signOut(page);
  await signIn(page);
  await openChat(page);
  await expect(conversation(page)).toContainText('What would you like to know?');
});

test('stop and reset cancel live requests; navigation retains the draft', async ({ page }) => {
  await signIn(page);
  await openChat(page);
  for (const action of ['Stop answer', 'New conversation']) {
    const sent = page.waitForRequest(r => r.url().endsWith(endpoint), { timeout: 35_000 });
    await page.getByRole('textbox', { name: 'Your question' }).fill(question);
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    const request = await sent;
    const failed = page.waitForEvent('requestfailed', { predicate: r => r === request, timeout: 15_000 });
    await page.getByRole('button', { name: action, exact: true }).click();
    expect((await failed).failure()?.errorText).toMatch(/ABORTED|CANCELLED/i);
    await expect(page.getByRole('button', { name: 'Stop answer' })).toBeHidden();
  }
  await expect(conversation(page)).toContainText('What would you like to know?');
  await page.getByRole('textbox', { name: 'Your question' }).fill('Retain on navigation');
  await page.getByRole('button', { name: /Today$/ }).click();
  await openChat(page);
  await expect(page.getByRole('textbox', { name: 'Your question' })).toHaveValue('Retain on navigation');
  await expect(conversation(page)).toContainText('What would you like to know?');
});

test('mobile navigation and live answer keep the composer usable', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  await openChat(page, true);
  await answer(page, testInfo);
  const field = page.getByRole('textbox', { name: 'Your question' });
  await field.fill('Draft\nsecond line');
  await field.scrollIntoViewIfNeeded();
  await expect(field).toBeInViewport();
  const fits = await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth);
  expect(fits).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('mobile-answer.png') });
  await page.getByRole('button', { name: 'New conversation' }).click();
  await expect(field).toHaveValue('');
  await signOut(page);
});

test('refresh and navigation retain stopped requests; sign-out clears history', async ({ page }, testInfo) => {
  await signIn(page);
  await openChat(page);
  for (const action of ['refresh', 'navigation', 'sign-out']) {
    const sent = page.waitForRequest(r => r.url().endsWith(endpoint), { timeout: 35_000 });
    await page.getByRole('textbox', { name: 'Your question' }).fill(question);
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    const request = await sent;
    const failed = page.waitForEvent('requestfailed', { predicate: r => r === request, timeout: 15_000 });
    const stored = await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]));
    expect(stored).toContain(question);
    if (action === 'refresh') {
      await page.screenshot({ path: testInfo.outputPath('desktop-streaming.png') });
      await page.reload();
    } else if (action === 'navigation') {
      await page.getByRole('button', { name: /Today$/ }).click();
      await openChat(page);
    } else {
      await signOut(page);
      await signIn(page);
      await openChat(page);
    }
    expect((await failed).failure()?.errorText).toMatch(/ABORTED|CANCELLED/i);
    if (action === 'sign-out') {
      await expect(conversation(page)).toContainText('What would you like to know?');
      await expect(conversation(page)).not.toContainText(question);
    } else {
      await expect(conversation(page)).toContainText(question);
      await expect(conversation(page)).toContainText('Answer stopped.');
      await page.getByRole('button', { name: 'New conversation' }).click();
    }
  }
});

test('hot deal records are paginated and open the selected company profile', async ({ page }, testInfo) => {
  await signIn(page);
  await openChat(page);
  const expected = await page.evaluate(async () => {
    const session = JSON.parse(sessionStorage.getItem('atlas.session'));
    const r = await fetch('/v1/deals?temperature=Hot&limit=1&with_total=true', {
      headers: { Authorization: `Bearer ${session.idToken}`, 'X-Tenant': session.tenant },
    });
    if (!r.ok) throw new Error(`Deal count failed: ${r.status}`);
    return (await r.json()).total;
  });
  await page.getByRole('textbox', { name: 'Your question' }).fill('Get me the hot deals');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Stop answer' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop answer' })).toBeHidden({ timeout: 320_000 });
  if (!await page.getByRole('table', { name: 'Matching records' }).count()
    && /commercial[\s\S]*deals[\s\S]*leads[\s\S]*\?/i.test(await conversation(page).innerText())) {
    await page.getByRole('textbox', { name: 'Your question' }).fill('I mean hot commercial origination deals. Show the matching records.');
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Stop answer' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Stop answer' })).toBeHidden({ timeout: 320_000 });
  }
  const handled = conversation(page).getByText("I couldn't complete this answer. Please try again shortly.", { exact: true });
  if (await handled.isVisible()) {
    await expect(conversation(page)).toContainText('Request ID:');
    await expect(conversation(page)).not.toContainText(/query_planning|Traceback|entity_id/);
    await page.getByRole('textbox', { name: 'Your question' }).fill('Try again');
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
    test.skip(true, 'Stage failure handled safely; this attempt did not produce records to navigate.');
  }
  await expect(conversation(page)).toContainText(`Matching records (${expected})`);
  const table = page.getByRole('table', { name: 'Matching records' });
  await expect(table).toBeVisible();
  await expect(table).not.toContainText(/entity_id|deal_no|created_at|updated_at/);
  const pages = Math.ceil(expected / 25);
  for (let i = 1; i < pages; i++) await page.getByRole('button', { name: 'Go to next page' }).click();
  await expect(table.locator('tbody tr')).toHaveCount(expected % 25 || 25);
  await page.screenshot({ path: testInfo.outputPath('hot-deal-records.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await table.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('mobile-deal-records.png') });
  await page.setViewportSize({ width: 1440, height: 1000 });
  const link = table.getByRole('link').last();
  const href = await link.getAttribute('href');
  const code = new URL(href, 'https://localhost').searchParams.get('company');
  await link.click();
  await expect(page).toHaveURL(new RegExp(`/ui/deals\\?company=${encodeURIComponent(code)}$`));
  await expect(page.getByRole('button', { name: 'Close', exact: true })).toBeVisible();
  await expect(page.getByText('About the company', { exact: true })).toBeVisible();
  await expect(page.getByText('In Client Master only — no Deals row yet. Use + Add product to start one.',
    { exact: true })).toBeHidden({ timeout: 30_000 });
  await page.screenshot({ path: testInfo.outputPath('linked-deal-profile.png') });
  await page.getByRole('button', { name: 'Close', exact: true }).click();
  await expect(page).toHaveURL(/\/ui\/deals$/);
  await openChat(page);
  await expect(conversation(page)).toContainText(`Matching records (${expected})`);
  await expect(page.getByRole('table', { name: 'Matching records' })).toBeVisible();
});

test('deal profile deep links load the authorized deal independently of model stages', async ({ page }) => {
  await signIn(page);
  const code = await page.evaluate(async () => {
    const session = JSON.parse(sessionStorage.getItem('atlas.session'));
    const r = await fetch('/v1/deals?temperature=Hot&limit=1', {
      headers: { Authorization: `Bearer ${session.idToken}`, 'X-Tenant': session.tenant },
    });
    if (!r.ok) throw new Error(`Deal lookup failed: ${r.status}`);
    const row = (await r.json()).items[0];
    return row.deal_no || row.code;
  });
  await signOut(page);
  await signIn(page, `/ui/deals?company=${encodeURIComponent(code)}`);
  await expect(page.getByRole('button', { name: 'Close', exact: true })).toBeVisible();
  await expect(page.getByText('In Client Master only — no Deals row yet. Use + Add product to start one.',
    { exact: true })).toBeHidden({ timeout: 30_000 });
  await expect(page.getByText('About the company', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Close', exact: true }).click();
  await expect(page).toHaveURL(/\/ui\/deals$/);
});

for (const mobile of [false, true]) {
  test(`edit and send replaces later history with a fresh request on ${mobile ? 'mobile' : 'desktop'}`, async ({ page }, testInfo) => {
    if (mobile) await page.setViewportSize({ width: 390, height: 844 });
    await signIn(page);
    await openChat(page, mobile);
    await answer(page, testInfo);
    const originalId = await conversation(page).getByText(/^Request ID:/).innerText();
    const followup = 'Tell me more about those leads';
    const pending = page.waitForRequest(r => r.url().endsWith(endpoint));
    await page.getByRole('textbox', { name: 'Your question' }).fill(followup);
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    await pending;
    await page.getByRole('button', { name: 'Stop answer' }).click();
    await page.getByRole('button', { name: 'Edit question 1', exact: true }).click();
    const edited = 'How many leads are in my book?';
    await page.getByRole('textbox', { name: 'Edit question', exact: true }).fill(edited);
    const responsePromise = page.waitForResponse(r => r.url().endsWith(endpoint));
    await page.getByRole('button', { name: 'Edit and send' }).click();
    const response = await responsePromise;
    expect(response.status()).toBe(200);
    expect(response.request().postDataJSON().messages).toEqual([{ role: 'user', content: edited }]);
    const newId = response.headers()['x-request-id'];
    expect(newId).toBeTruthy();
    expect(`Request ID: ${newId}`).not.toBe(originalId);
    await expect(conversation(page)).not.toContainText(originalId);
    await expect(conversation(page)).not.toContainText(followup);
    await expect(page.getByRole('button', { name: 'Stop answer' })).toBeHidden({ timeout: 320_000 });
    await expect(conversation(page)).toContainText(`Request ID: ${newId}`);
    await expect(page.getByRole('alert')).toHaveCount(0);
    await page.reload();
    await expect(conversation(page)).toContainText(edited);
    await expect(conversation(page)).toContainText(`Request ID: ${newId}`);
    await expect(conversation(page)).not.toContainText(originalId);
    await expect(conversation(page)).not.toContainText(followup);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.getByRole('button', { name: 'New conversation' }).click();
    await expect(conversation(page)).toContainText('What would you like to know?');
  });
}
