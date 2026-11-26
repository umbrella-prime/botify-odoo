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
    const classes = new Set();
    return {
      tag,
      style: {
        setProperty(name, value) {
          this[name] = value;
        },
      },
      contentWindow: tag === 'iframe' ? frameWindow : null,
      attributes: {},
      dataset: {},
      childNodes: [],
      get className() {
        return [...classes].join(' ');
      },
      set className(value) {
        classes.clear();
        for (const name of value.split(' ').filter(Boolean)) classes.add(name);
      },
      classList: {
        contains: (name) => classes.has(name),
        toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
        remove: (name) => classes.delete(name),
      },
      setAttribute(name, value) {
        this.attributes[name] = value;
      },
      replaceChildren(...nodes) {
        this.childNodes = nodes;
      },
      addEventListener() {},
      append(...nodes) {
        children.push(...nodes);
      },
      appendChild(node) {
        this.childNodes.push(node);
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
    createElementNS: (_namespace, tag) => element(tag),
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
  return {
    service,
    readLook: addon.namespace.readLook,
    children,
    actions,
    requests,
    replies,
    message,
    action,
    bus,
  };
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

/** The launcher as drawn: host side and variables, classes, glyph path and label text. */
function drawn(children) {
  const host = children.find(({ tag }) => tag === 'div');
  const launcher = children.find(({ tag }) => tag === 'button');
  const [glyph, ...rest] = launcher.childNodes;
  const label = rest.find(({ className }) => className === 'text');
  return {
    host: host.className,
    vars: Object.fromEntries(Object.entries(host.style).filter(([name]) => name.startsWith('--'))),
    launcher: launcher.className,
    glyph: glyph.childNodes[0].attributes.d,
    label: label ? label.textContent : null,
    innerHTML: launcher.innerHTML ?? label?.innerHTML,
  };
}

const CHAT = 'M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2z';
const DEFAULT_VARS = {
  '--bw-primary': '#4f46e5',
  '--bw-on-primary': '#ffffff',
  '--bw-ring': '#4f46e566',
  '--bw-glow': '#4f46e555',
  '--bw-radius': '16px',
  '--bw-size': '56px',
  '--bw-ox': '20px',
  '--bw-oy': '20px',
  '--bw-width': '380px',
};

test('draws the website default launcher until the frame sends a look', async () => {
  const { children, message } = await start();
  const before = drawn(children);
  assert.deepEqual(before, {
    host: 'o_botify_widget o_botify_right',
    vars: DEFAULT_VARS,
    launcher: 'launcher enter',
    glyph: CHAT,
    label: null,
    innerHTML: undefined,
  });
  const studio = { primary: '#0F766E', position: 'left', style: 'label', label: 'اسألني' };
  // Before the frame is ready, or from anyone but the frame: ignored.
  await message(envelope('look', studio));
  await message(envelope('ready', null));
  await message(envelope('look', studio), { origin: 'https://other.test' });
  await message(envelope('look', studio), { source: {} });
  assert.deepEqual(drawn(children), before);
});

test('follows the widget studio look the frame sends', async () => {
  const { children, message, service } = await start();
  await message(envelope('ready', null));
  await message(
    envelope('look', {
      primary: '#0F766E',
      radius: 8,
      position: 'left',
      width: 'wide',
      style: 'label',
      icon: 'headset',
      label: 'اسألني',
      size: 'lg',
      offsetX: 24,
      offsetY: 24,
      hideOnMobile: true,
      animation: 'pulse',
    }),
  );
  const look = drawn(children);
  assert.equal(look.host, 'o_botify_widget o_botify_left');
  assert.deepEqual(look.vars, {
    '--bw-primary': '#0F766E',
    '--bw-on-primary': '#ffffff',
    '--bw-ring': '#0F766E66',
    '--bw-glow': '#0F766E55',
    '--bw-radius': '8px',
    '--bw-size': '64px',
    '--bw-ox': '24px',
    '--bw-oy': '24px',
    '--bw-width': '440px',
  });
  assert.equal(look.launcher, 'launcher pill no-mobile enter pulse');
  assert.equal(look.label, 'اسألني');
  // Open: the close glyph, no label, the panel shown (on the launcher's side, by host class).
  service.toggle();
  const frame = children.find(({ tag }) => tag === 'iframe');
  assert.equal(frame.classList.contains('open'), true);
  assert.equal(drawn(children).label, null);
  assert.notEqual(drawn(children).glyph, look.glyph);
});

test('ignores or clamps a hostile look and never injects markup', async () => {
  const { children, message, readLook } = await start();
  await message(envelope('ready', null));
  const hostile = {
    primary: 'red;background:url(x)',
    radius: -5,
    position: 'top',
    width: 'full',
    style: 'avatar',
    icon: '<svg onload=alert(1)>',
    label: '  <img src=x onerror=alert(1)><script>alert(1)</script>  ',
    size: 9999,
    offsetX: 9999,
    offsetY: Number.NaN,
    hideOnMobile: 'yes',
    animation: 'spin',
    avatarUrl: 'https://evil.test/x.png',
  };
  await message(envelope('look', hostile));
  const look = drawn(children);
  assert.equal(look.host, 'o_botify_widget o_botify_right');
  assert.deepEqual(look.vars, { ...DEFAULT_VARS, '--bw-radius': '0px', '--bw-ox': '120px' });
  assert.equal(look.launcher, 'launcher enter');
  assert.equal(look.glyph, CHAT);
  assert.equal(look.innerHTML, undefined);
  assert.doesNotMatch(JSON.stringify(look.vars), /url|;|evil/);

  // A label is only ever text: kept verbatim (trimmed) in textContent.
  await message(envelope('look', { style: 'label', label: hostile.label }));
  assert.equal(drawn(children).label, '<img src=x onerror=alert(1)><script>alert(1)</script>');
  assert.equal(drawn(children).innerHTML, undefined);
  assert.equal(readLook({ label: 'x'.repeat(500) }).label.length, 60);

  const inherited = Object.create({ primary: '#000000', position: 'left' });
  for (const payload of [null, 'look', ['#0F766E'], 42, new Date(), inherited]) {
    assert.deepEqual({ ...readLook(payload) }, { ...readLook({}) });
  }
  // A malformed look goes back to the default rather than keeping the previous one.
  await message(envelope('look', { primary: '#0F766E', position: 'left' }));
  await message(envelope('look', 'nonsense'));
  assert.equal(drawn(children).host, 'o_botify_widget o_botify_right');
  assert.equal(drawn(children).vars['--bw-primary'], '#4f46e5');
});
