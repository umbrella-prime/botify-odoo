import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { chromium } from 'playwright';

const odoo = 'https://odoo.test';
const botify = 'https://botify.test';
const siteKey = 'wsk_fixture';
const widget = `${botify}/widget/v1`;
const frameUrl = `${widget}/frame?site=${siteKey}`;
const rpcPath = '/web/dataset/call_kw';
const identityPath = '/botify_agent/v1/identity';
const sessionId = 'fixture-odoo-session';
const addonRoot = new URL('../static/src/', import.meta.url);
const distribution = new URL('../../../../packages/widget/dist/client/', import.meta.url);
const fixture = `<!doctype html><html lang="en">
<script type="importmap">{"imports":{
  "@web/core/browser/browser":"/stubs.js","@web/core/browser/router":"/stubs.js",
  "@web/core/registry":"/stubs.js","@web/core/user":"/stubs.js",
  "@web/core/l10n/translation":"/stubs.js"
}}</script>
<link rel="stylesheet" href="/botify_service.css">
<script>window.odoo = {csrf_token: 'fixture-csrf-secret'}</script>
<body>
<script type="module">
import { botifyAgentService } from '/botify_service.js';
const bus = {addEventListener() {}, subscribe() {}, start() {}};
await botifyAgentService.start({bus}, {action: {}, bus_service: bus});
</script>
</body></html>`;
const stubs = `
export const browser = window;
export const router = {};
export const routerBus = new EventTarget();
export const registry = {category: () => ({add() {}})};
export const user = {hasGroup: async () => true, activeCompanies: []};
export const _t = (message) => message;
`;
const config = { siteKey, title: 'Botify', locale: 'en' };

test('the addon embeds the real widget across origins, with isolated RPC and working files', async () => {
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({ serviceWorkers: 'block' });
    await context.addCookies([
      {
        name: 'session_id',
        value: sessionId,
        url: odoo,
        httpOnly: true,
        secure: true,
        sameSite: 'Lax',
      },
    ]);
    const authenticatedPosts = new Set();
    const bootstrap = [];
    await context.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin === odoo) {
        // Playwright can answer preflight itself. Record only POSTs that carry the Odoo session.
        if (request.method() === 'POST') {
          const cookie = await request.headerValue('cookie');
          if (cookie?.split(';').some((pair) => pair.trim() === `session_id=${sessionId}`)) {
            authenticatedPosts.add(url.pathname);
          }
        }
        switch (url.pathname) {
          case '/':
            return route.fulfill({ contentType: 'text/html', body: fixture });
          case '/stubs.js':
            return route.fulfill({ contentType: 'text/javascript', body: stubs });
          case '/botify_service.js':
            return route.fulfill({
              contentType: 'text/javascript',
              body: await readFile(new URL('botify_service.js', addonRoot)),
            });
          case '/botify_service.css':
            return route.fulfill({
              contentType: 'text/css',
              body: await readFile(new URL('botify_service.css', addonRoot)),
            });
          case identityPath:
            return route.fulfill({
              json: {
                result: {
                  ok: true,
                  result: {
                    siteKey,
                    frameUrl,
                    assertion: 'o1.employee.signature',
                    loaderUrl: `${widget}/widget.js`,
                  },
                },
              },
            });
          case '/botify_agent/v1/events/pending':
            return route.fulfill({ json: { result: { ok: true, result: { events: [] } } } });
          case rpcPath:
            return route.fulfill({
              // Playwright adds permissive CORS headers to mocked responses unless one is supplied.
              headers: { 'access-control-allow-origin': odoo },
              json: { result: 'mock Odoo RPC' },
            });
        }
      }
      if (url.origin === botify) {
        switch (url.pathname) {
          case '/widget/v1/frame':
            return route.fulfill({
              contentType: 'text/html',
              body: `<!doctype html><link rel="stylesheet" href="frame.css"><div id="botify-widget" data-site-key="${siteKey}"></div><script src="frame.js" defer></script>`,
            });
          case '/widget/v1/frame.css':
            return route.fulfill({
              contentType: 'text/css',
              body: await readFile(new URL('frame.css', distribution)),
            });
          case '/widget/v1/widget.js':
          case '/widget/v1/frame.js':
            return route.fulfill({
              contentType: 'text/javascript',
              body: await readFile(new URL(url.pathname.split('/').at(-1), distribution)),
            });
          case '/widget/v1/sessions':
            bootstrap.push(route.request().postDataJSON());
            return route.fulfill({
              json: {
                token: 'widget-token',
                visitorId: 'visitor',
                sessionId: 'session',
                identity: { level: 'verified', name: 'Employee' },
                config,
              },
            });
          case '/widget/v1/config':
            return route.fulfill({ json: config });
          case '/widget/v1/notifications':
            return route.fulfill({ json: { acks: [], unread: 0 } });
          case '/widget/v1/history':
            return route.fulfill({
              json: {
                items: [
                  {
                    role: 'assistant',
                    text: '[Report](https://botify.test/widget/v1/files/artifact_1)',
                    at: '2026-11-23T00:00:00Z',
                  },
                ],
                waitingApproval: false,
              },
            });
          case '/widget/v1/attachments':
            return route.fulfill({
              status: 201,
              json: {
                id: route.request().headers()['x-file-id'],
                name: 'notes.txt',
                mimeType: 'text/plain',
                sizeBytes: 4,
              },
            });
          case '/widget/v1/files/artifact_1':
            return route.fulfill({
              headers: { 'content-disposition': "attachment; filename*=UTF-8''report.txt" },
              body: 'report',
            });
        }
      }
      return route.fulfill({ status: 404 });
    });

    const page = await context.newPage();
    await page.goto(odoo);
    const launcher = page.locator('[data-botify-widget] button.launcher');
    await launcher.click({ timeout: 2500 });
    const chat = page.frameLocator('[data-botify-widget] iframe');
    await chat.locator('textarea').waitFor({ timeout: 2500 });
    const frame = page.frame({ url: frameUrl });
    assert.ok(frame);
    assert.deepEqual(bootstrap[0].identity, { assertion: 'o1.employee.signature' });
    assert.equal(bootstrap[0].hostOrigin, odoo);
    assert.equal(await page.locator(`script[src^="${botify}"]`).count(), 0);
    assert.ok(authenticatedPosts.has(identityPath), 'The Odoo page should send its session cookie');

    assert.equal(
      await frame.evaluate(() => {
        try {
          return window.parent.odoo.csrf_token;
        } catch (error) {
          return error.name;
        }
      }),
      'SecurityError',
    );
    assert.equal(
      await frame.evaluate(async (endpoint) => {
        try {
          await fetch(endpoint, {
            method: 'POST',
            credentials: 'include',
            headers: {
              'content-type': 'application/json',
            },
            body: '{}',
          });
          return 'accepted';
        } catch (error) {
          return error.name;
        }
      }, odoo + rpcPath),
      'TypeError',
    );
    assert.ok(!authenticatedPosts.has(rpcPath), 'The iframe should send no authenticated Odoo RPC');

    await chat.locator('input[type=file]').setInputFiles({
      name: 'notes.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('test'),
    });
    await chat.locator('.bw-upload-status').filter({ hasText: 'Ready' }).waitFor({ timeout: 5000 });
    const [download] = await Promise.all([
      page.waitForEvent('download', { timeout: 2500 }),
      chat.getByRole('link', { name: 'Report' }).click({ timeout: 2500 }),
    ]);
    assert.equal(download.suggestedFilename(), 'report.txt');
  } finally {
    await browser.close();
  }
});
