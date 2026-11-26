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
const bus = {addEventListener() {}, subscribe(_, ping) { window.busPing = ping; }, start() {}};
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

/**
 * Opens the fixture Odoo page with the real addon and widget. `server.events` is what Odoo has
 * pending; `server.expired` lists assertions Botify refuses as expired (`true`: every one).
 */
async function openOdoo(browser, { events = [], expired = [], look = null } = {}) {
  const server = { events, unread: events.length, expired, assertions: 0, botifyGets: [] };
  const refused = (assertion) => server.expired === true || server.expired.includes(assertion);
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
  const notifies = [];
  const reads = [];
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
                  assertion: `o1.employee.signature${++server.assertions > 1 ? `.${server.assertions}` : ''}`,
                  loaderUrl: `${widget}/widget.js`,
                },
              },
            },
          });
        case '/botify_agent/v1/events/pending':
          return route.fulfill({
            json: { result: { ok: true, result: { events: server.events } } },
          });
        case rpcPath:
          return route.fulfill({
            // Playwright adds permissive CORS headers to mocked responses unless one is supplied.
            headers: { 'access-control-allow-origin': odoo },
            json: { result: 'mock Odoo RPC' },
          });
      }
    }
    if (url.origin === botify) {
      if (request.method() === 'GET') server.botifyGets.push(url.pathname);
      switch (url.pathname) {
        case '/widget/v1/frame': {
          // As the API embeds it: JSON escaped for a double-quoted attribute.
          const attribute = look
            ? ` data-look="${JSON.stringify(look).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`)}"`
            : '';
          return route.fulfill({
            contentType: 'text/html',
            body: `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="frame.css"><div id="botify-widget" data-site-key="${siteKey}"${attribute}></div><script src="frame.js" defer></script>`,
          });
        }
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
          if (refused(bootstrap.at(-1).identity?.assertion)) {
            return route.fulfill({
              status: 403,
              json: { error: { code: 'forbidden', details: { reason: 'assertion_expired' } } },
            });
          }
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
          notifies.push(route.request().postDataJSON());
          return route.fulfill({ json: { acks: [], unread: server.unread } });
        case '/widget/v1/notifications/read':
          reads.push(1);
          server.unread = 0;
          return route.fulfill({ json: {} });
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
  await load(page, () => page.goto(odoo));
  return { page, server, bootstrap, notifies, reads, authenticatedPosts };
}

/** Navigates and waits until the addon has asked Odoo for pending events (the frame is ready). */
async function load(page, navigate) {
  const pending = page.waitForRequest(`${odoo}/botify_agent/v1/events/pending`);
  await navigate();
  await pending;
}

test('an Odoo page load without opening the chat creates no Botify session', async () => {
  const browser = await chromium.launch();
  try {
    const { page, server, bootstrap, notifies } = await openOdoo(browser);
    // The relay posts an empty `notifications` once the frame is ready; give the frame time to act on it.
    await page.waitForTimeout(1000);
    assert.equal(bootstrap.length, 0, 'No session (and no identity) before the chat opens');
    assert.equal(notifies.length, 0);

    await page.locator('[data-botify-widget] button.launcher').click({ timeout: 2500 });
    await page
      .frameLocator('[data-botify-widget] iframe')
      .locator('textarea')
      .waitFor({ timeout: 2500 });
    await page.waitForTimeout(500);
    assert.equal(bootstrap.length, 1, 'Opening the chat creates exactly one session');

    // With a session, an empty notify (another tab took the events) still refreshes the unread count.
    await page.locator('[data-botify-widget] button.launcher').click();
    server.unread = 2;
    await page.evaluate(() => window.busPing());
    await page
      .locator('[data-botify-widget] .badge')
      .filter({ hasText: '2' })
      .waitFor({ timeout: 2500 });
    // One refresh when opening the chat started the session, one for the ping.
    assert.deepEqual(notifies, [{ events: [] }, { events: [] }]);
    assert.equal(bootstrap.length, 1);
  } finally {
    await browser.close();
  }
});

test('a pending Odoo event still reaches Botify and shows the unread badge without opening the chat', async () => {
  const browser = await chromium.launch();
  try {
    const { page, bootstrap, notifies } = await openOdoo(browser, { events: ['o1.event'] });
    const badge = page.locator('[data-botify-widget] .badge');
    await badge.filter({ hasText: '1' }).waitFor({ timeout: 2500 });
    assert.equal(bootstrap.length, 1);
    assert.deepEqual(notifies, [{ events: ['o1.event'] }]);
  } finally {
    await browser.close();
  }
});

test('an event that arrives after the page-load assertion expired still reaches Botify', async () => {
  const browser = await chromium.launch();
  try {
    const { page, server, bootstrap, notifies } = await openOdoo(browser, {
      expired: ['o1.employee.signature'],
    });
    await page.waitForTimeout(500);
    assert.equal(bootstrap.length, 0);

    // A time-off decision minutes later: the frame's first bootstrap uses the stale page-load assertion.
    server.events = ['o1.event'];
    server.unread = 1;
    await page.evaluate(() => window.busPing());
    await page
      .locator('[data-botify-widget] .badge')
      .filter({ hasText: '1' })
      .waitFor({ timeout: 2500 });
    assert.deepEqual(
      bootstrap.map((body) => body.identity.assertion),
      ['o1.employee.signature', 'o1.employee.signature.2'],
    );
    assert.deepEqual(notifies, [{ events: ['o1.event'] }]);
  } finally {
    await browser.close();
  }
});

test('opening the chat after the page-load assertion expired marks the notifications read', async () => {
  const browser = await chromium.launch();
  try {
    const { page, server, bootstrap, reads } = await openOdoo(browser, {
      expired: ['o1.employee.signature'],
    });
    // Another tab delivered a notification this tab has not seen.
    server.unread = 1;
    await page.locator('[data-botify-widget] button.launcher').click({ timeout: 2500 });
    await page
      .frameLocator('[data-botify-widget] iframe')
      .locator('textarea')
      .waitFor({ timeout: 2500 });
    await page.waitForTimeout(500);
    assert.deepEqual(
      bootstrap.map((body) => body.identity.assertion),
      ['o1.employee.signature', 'o1.employee.signature.2'],
    );
    assert.equal(reads.length, 1, 'Opening the chat marks the notifications read');
    await load(page, () => page.reload());
    await page.waitForTimeout(500);
    assert.equal(await page.locator('[data-botify-widget] .badge').isHidden(), true);
  } finally {
    await browser.close();
  }
});

test('a held event retries a refused assertion once, not in a loop', async () => {
  const browser = await chromium.launch();
  try {
    // Odoo's clock is off: Botify refuses every assertion.
    const { page, server, bootstrap } = await openOdoo(browser, { expired: true });
    await page.waitForTimeout(500);
    assert.equal(bootstrap.length, 0);
    server.events = ['o1.event'];
    await page.evaluate(() => window.busPing());
    await page.waitForTimeout(1500);
    assert.equal(bootstrap.length, 2, 'The stale assertion, then one fresh one');

    // A new relay with the event allows one more try.
    await page.evaluate(() => window.busPing());
    await page.waitForTimeout(1000);
    assert.equal(bootstrap.length, 4);
  } finally {
    await browser.close();
  }
});

test('the unread badge survives a reload with nothing pending, and opening the chat reads it', async () => {
  const browser = await chromium.launch();
  try {
    const { page, server, bootstrap, reads } = await openOdoo(browser, { events: ['o1.event'] });
    const badge = page.locator('[data-botify-widget] .badge');
    await badge.filter({ hasText: '1' }).waitFor({ timeout: 2500 });

    // Botify stored the notification and Odoo acknowledged it: nothing is pending any more.
    server.events = [];
    await load(page, () => page.reload());
    await badge.filter({ hasText: '1' }).waitFor({ timeout: 2500 });
    assert.equal(bootstrap.length, 1, 'The reload alone starts no session');

    await page.locator('[data-botify-widget] button.launcher').click({ timeout: 2500 });
    await badge.waitFor({ state: 'hidden', timeout: 2500 });
    await page.waitForTimeout(500);
    assert.equal(reads.length, 1, 'Opening the chat marks the notifications read');
    await load(page, () => page.reload());
    await page.waitForTimeout(500);
    assert.equal(
      await badge.isHidden(),
      true,
      'A read notification leaves no badge after a reload',
    );
  } finally {
    await browser.close();
  }
});

test('the addon embeds the real widget across origins, with isolated RPC and working files', async () => {
  const browser = await chromium.launch();
  try {
    const { page, bootstrap, authenticatedPosts } = await openOdoo(browser);
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

/** Where the launcher and panel sit, and how they look. */
async function launcherBox(page) {
  await page.mouse.move(0, 0); // off the launcher's hover lift
  await page.waitForTimeout(300);
  return page.evaluate(() => {
    const launcher = document.querySelector('[data-botify-widget] button.launcher');
    const panel = document.querySelector('[data-botify-widget] iframe');
    const style = getComputedStyle(launcher);
    const box = launcher.getBoundingClientRect();
    const frame = panel.getBoundingClientRect();
    return {
      left: Math.round(box.left),
      right: Math.round(window.innerWidth - box.right),
      bottom: Math.round(window.innerHeight - box.bottom),
      height: Math.round(box.height),
      radius: style.borderRadius,
      background: style.backgroundColor,
      text: launcher.innerText.trim(),
      panelLeft: Math.round(frame.left),
      panelRight: Math.round(window.innerWidth - frame.right),
      panelWidth: Math.round(frame.width),
      panelVisible: getComputedStyle(panel).visibility === 'visible',
    };
  });
}

test('the launcher follows the widget studio look the frame embeds', async () => {
  const browser = await chromium.launch();
  try {
    // Defaults: the website's round brand-coloured launcher, bottom-right.
    const plain = await openOdoo(browser);
    const launcher = plain.page.locator('[data-botify-widget] button.launcher');
    await launcher.waitFor({ timeout: 2500 });
    await plain.page.waitForTimeout(600); // the arrival pop
    await launcher.click();
    await plain.page.waitForTimeout(600); // the panel's open transition
    assert.deepEqual(await launcherBox(plain.page), {
      left: (await launcherBox(plain.page)).left,
      right: 20,
      bottom: 20,
      height: 56,
      radius: '999px',
      background: 'rgb(79, 70, 229)',
      text: '',
      panelLeft: (await launcherBox(plain.page)).panelLeft,
      panelRight: 20,
      panelWidth: 380,
      panelVisible: true,
    });

    const studio = await openOdoo(browser, {
      look: {
        primary: '#0F766E',
        radius: 16,
        position: 'left',
        width: 'regular',
        style: 'label',
        icon: 'chat',
        label: 'اسألني',
        size: 'lg',
        offsetX: 24,
        offsetY: 24,
        hideOnMobile: false,
        animation: 'none',
      },
    });
    const styled = studio.page.locator('[data-botify-widget] button.launcher');
    await styled.filter({ hasText: 'اسألني' }).waitFor({ timeout: 2500 });
    await studio.page.waitForTimeout(600);
    // The look came with the frame: a closed chat loads only the frame and its assets.
    assert.deepEqual(studio.bootstrap, []);
    assert.deepEqual([...new Set(studio.server.botifyGets)].sort(), [
      '/widget/v1/frame',
      '/widget/v1/frame.css',
      '/widget/v1/frame.js',
    ]);
    await styled.click();
    await studio.page.waitForTimeout(600);
    const look = await launcherBox(studio.page);
    assert.equal(look.left, 24);
    assert.equal(look.bottom, 24);
    assert.equal(look.height, 64);
    assert.equal(look.background, 'rgb(15, 118, 110)');
    assert.equal(look.panelLeft, 24);
    assert.equal(look.panelVisible, true);
  } finally {
    await browser.close();
  }
});
