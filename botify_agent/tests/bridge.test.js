import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { createContext, SourceTextModule, SyntheticModule } from 'node:vm';

const source = await readFile(new URL('../static/src/botify_service.js', import.meta.url), 'utf8');
const odooOrigin = 'https://odoo.test';
const frameOrigin = 'https://botify.test';
const identity = {
  assertion: 'o1.initial.signature',
  siteKey: 'wsk_odoo',
  frameUrl: `${frameOrigin}/widget/v1/frame?site=wsk_odoo`,
};
const envelope = (type, payload) => ({ botify: 1, type, payload });
const invoke = (name, args) => envelope('action.invoke', { callId: 'call_1', name, args });
const event = (name, data) => envelope('event', { name, data });
const execute = invoke('botify.odoo.execute', { grant: 'g1.payload.signature', refreshCsrf: true });

async function start(frameUrl = identity.frameUrl) {
  const requests = [];
  const replies = [];
  const actions = [];
  const children = [];
  const listeners = new Map();
  const frameWindow = {
    postMessage: (message, origin) => replies.push({ message: structuredClone(message), origin }),
  };
  function element(tag) {
    return {
      tag,
      style: {},
      contentWindow: tag === 'iframe' ? frameWindow : null,
      attributes: {},
      dataset: {},
      setAttribute(name, value) {
        this.attributes[name] = value;
      },
      addEventListener() {},
      append(...nodes) {
        children.push(...nodes);
      },
      appendChild(node) {
        children.push(node);
      },
      focus() {},
    };
  }
  const fetch = async (route, init) => {
    requests.push({
      route,
      csrf: init.headers['X-Botify-CSRF'],
      params: JSON.parse(init.body).params,
    });
    const result = {
      identity: {
        ...identity,
        frameUrl,
        assertion:
          requests.filter((request) => request.route.endsWith('/identity')).length === 1
            ? identity.assertion
            : 'o1.fresh.signature',
      },
      csrf: { csrfToken: 'refreshed-csrf' },
      pending: { events: ['n1.event.signature'] },
      ack: { acknowledged: 1 },
      execute: { ids: [42] },
    }[route.split('/').at(-1)];
    return {
      json: async () => ({
        result: {
          ok: true,
          result,
          ...(route.endsWith('/execute') ? { client: { action: { id: 42 } } } : {}),
        },
      }),
    };
  };
  const window = {
    location: new URL(odooOrigin),
    addEventListener: (type, handler) => listeners.set(type, handler),
  };
  const document = {
    head: element('head'),
    body: element('body'),
    documentElement: { lang: 'en-US' },
    createElement: element,
  };
  const context = createContext({
    window,
    document,
    location: window.location,
    URL,
    TextEncoder,
    odoo: { csrf_token: 'page-csrf' },
  });
  const bus = new EventTarget();
  const busService = { subscribe() {}, start() {} };
  const action = { doAction: (value) => actions.push(value) };
  const imports = {
    '@web/core/browser/browser': { browser: { fetch, setInterval() {} } },
    '@web/core/browser/router': { router: {}, routerBus: bus },
    '@web/core/registry': { registry: { category: () => ({ add() {} }) } },
    '@web/core/user': { user: { hasGroup: async () => true, activeCompanies: [{ id: 7 }] } },
    '@web/core/l10n/translation': { _t: (message) => message },
  };
  const addon = new SourceTextModule(source, { context });
  await addon.link((specifier) => {
    const entries = Object.entries(imports[specifier]);
    return new SyntheticModule(
      entries.map(([name]) => name),
      function () {
        for (const [name, value] of entries) this.setExport(name, value);
      },
      { context },
    );
  });
  await addon.evaluate();
  const service = await addon.namespace.botifyAgentService.start(
    { bus },
    {
      action,
      bus_service: busService,
    },
  );
  const message = async (data, { origin = frameOrigin, source = frameWindow } = {}) => {
    await listeners.get('message')?.({ data, origin, source });
    await new Promise(setImmediate);
  };
  return { service, children, actions, requests, replies, message, action, bus };
}

test('mounts only a cross-origin frame and initializes it with the widget assertion and context', async () => {
  const { children, message, replies } = await start();
  assert.deepEqual(
    children.filter(({ tag }) => tag === 'script'),
    [],
  );
  const frame = children.find(({ tag }) => tag === 'iframe');
  assert.equal(frame?.src, identity.frameUrl);
  assert.match(frame.attributes.sandbox, /\ballow-downloads\b/);
  // The composer is a form (requestSubmit); without allow-forms the sandbox drops every message.
  assert.match(frame.attributes.sandbox, /\ballow-forms\b/);

  await message(envelope('ready', null), { origin: 'https://other.test' });
  await message(envelope('ready', null), { source: {} });
  assert.deepEqual(replies, []);
  await message(envelope('ready', null));
  assert.deepEqual(replies, [
    {
      origin: frameOrigin,
      message: envelope('init', {
        siteKey: 'wsk_odoo',
        locale: 'en',
        identity: { assertion: identity.assertion },
        context: {
          odoo: {
            model: null,
            id: null,
            action: null,
            activeCompanies: [{ id: 7, name: undefined }],
          },
        },
        actions: [],
        open: false,
      }),
    },
    { origin: frameOrigin, message: envelope('notifications', { events: ['n1.event.signature'] }) },
  ]);
});

test('refuses a same-origin or non-HTTP(S) frame URL', async () => {
  for (const url of [`${odooOrigin}/widget/v1/frame`, 'javascript:void(0)']) {
    const { children, service } = await start(url);
    assert.equal(service.available, false);
    assert.deepEqual(children, []);
  }
});

test('drops wrong senders, unknown messages and arguments outside the allow-list', async () => {
  const { message, requests, replies, actions } = await start();
  await message(execute);
  assert.equal(requests.length, 1);
  await message(envelope('ready', null));
  const requestCount = requests.length;
  const replyCount = replies.length;
  await message(execute, { origin: 'https://other.test' });
  await message(execute, { source: {} });
  for (const data of [
    envelope('rpc', { route: '/web/dataset/call_kw' }),
    event('csrf', {}),
    event('identity.expired', { includeSession: true }),
    event('identity.expired', new Date()),
    event('notifications.acked', new Array(1)),
    event('notifications.acked', { acks: [null] }),
    event('notifications.acked', { acks: new Array(1) }),
    event('notifications.acked', { acks: Array(21).fill('a1.receipt.signature') }),
    invoke('website.action', {}),
    invoke('botify.odoo.execute', { grant: 'g1.payload.signature', context: {} }),
    invoke('botify.odoo.execute', { grant: 'g1.payload.signature', refreshCsrf: 'true' }),
    invoke('botify.odoo.execute', { grant: 'invalid' }),
    { ...execute, route: '/web/dataset/call_kw' },
    { ...execute, botify: 2 },
    envelope('state', { open: 'false' }),
    envelope('unread', { count: -1 }),
  ]) {
    await message(data);
  }
  assert.equal(requests.length, requestCount);
  assert.equal(replies.length, replyCount);
  assert.deepEqual(actions, []);
});

test('relays refresh, grants, acknowledgments and toggle with host-owned credentials', async () => {
  const { service, message, requests, replies, actions, action, bus } = await start();
  await message(envelope('ready', null));
  service.toggle();
  assert.deepEqual(replies.at(-1), { origin: frameOrigin, message: envelope('open', null) });
  await message(envelope('state', { open: false }));
  service.toggle();
  assert.deepEqual(replies.at(-1), { origin: frameOrigin, message: envelope('open', null) });

  action.currentController = { state: { model: 'crm.lead', resId: 42 } };
  bus.dispatchEvent(new Event('ACTION_MANAGER:UI-UPDATED'));
  assert.equal(replies.at(-1).message.type, 'context');
  assert.equal(replies.at(-1).message.payload.odoo.model, 'crm.lead');
  assert.equal(replies.at(-1).message.payload.odoo.id, 42);

  await message(event('identity.expired', {}));
  assert.deepEqual(replies.at(-1), {
    origin: frameOrigin,
    message: envelope('identify', { assertion: 'o1.fresh.signature' }),
  });
  await message(execute);
  assert.deepEqual(requests.slice(-2), [
    { route: '/botify_agent/v1/csrf', csrf: 'page-csrf', params: {} },
    {
      route: '/botify_agent/v1/execute',
      csrf: 'refreshed-csrf',
      params: { grant_token: 'g1.payload.signature', context: { allowed_company_ids: [7] } },
    },
  ]);
  assert.deepEqual(actions, [{ id: 42 }]);
  assert.deepEqual(replies.at(-1), {
    origin: frameOrigin,
    message: envelope('action.result', {
      callId: 'call_1',
      ok: true,
      result: { ok: true, result: { ids: [42] } },
    }),
  });

  await message(event('notifications.acked', { acks: ['a1.receipt.signature'] }));
  assert.deepEqual(requests.at(-1), {
    route: '/botify_agent/v1/events/ack',
    csrf: 'refreshed-csrf',
    params: { acks: ['a1.receipt.signature'] },
  });
  assert.doesNotMatch(JSON.stringify(replies), /csrf|client/);
});
