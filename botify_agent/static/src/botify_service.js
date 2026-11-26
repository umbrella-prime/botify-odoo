import { browser } from '@web/core/browser/browser';
import { router, routerBus } from '@web/core/browser/router';
import { _t } from '@web/core/l10n/translation';
import { registry } from '@web/core/registry';
import { user } from '@web/core/user';

/**
 * Loads the Botify assistant for employees in the "Botify Agent user" group
 * and relays the operations Botify grants to this module's `execute` route.
 * The browser relay is a capability transport, not an authorization bypass:
 * every operation carries a grant bound to this Odoo session and runs with
 * the employee's own Odoo rights.
 */

const IDENTITY = '/botify_agent/v1/identity';
const EXECUTE = '/botify_agent/v1/execute';
const CSRF = '/botify_agent/v1/csrf';
const PENDING = '/botify_agent/v1/events/pending';
const ACK = '/botify_agent/v1/events/ack';
const PROTOCOL_VERSION = 1;
const EXECUTE_ACTION = 'botify.odoo.execute';
/** Events Botify has not acknowledged are leased for 60 s, then handed out again. */
const RETRY_MS = 3 * 60 * 1000;

/** A token fetched from the server after the page's own went stale; null until needed. */
let freshCsrfToken = null;

function hasKeys(value, required, optional = []) {
  return (
    Object.prototype.toString.call(value) === '[object Object]' &&
    required.every((key) => Object.hasOwn(value, key)) &&
    Object.keys(value).every((key) => required.includes(key) || optional.includes(key))
  );
}

function isToken(value, pattern, maxLength) {
  return typeof value === 'string' && value.length <= maxLength && pattern.test(value);
}

function isGrantRequest(payload) {
  return (
    hasKeys(payload, ['callId', 'name', 'args']) &&
    typeof payload.callId === 'string' &&
    payload.callId.length > 0 &&
    payload.callId.length <= 192 &&
    payload.name === EXECUTE_ACTION &&
    hasKeys(payload.args, ['grant'], ['refreshCsrf']) &&
    isToken(payload.args.grant, /^g1\.[\w-]+\.[\w-]+$/, 48000) &&
    (!Object.hasOwn(payload.args, 'refreshCsrf') || typeof payload.args.refreshCsrf === 'boolean')
  );
}

function isAck(data) {
  return (
    hasKeys(data, ['acks']) &&
    Array.isArray(data.acks) &&
    data.acks.length > 0 &&
    data.acks.length <= 20 &&
    [...data.acks].every((token) => isToken(token, /^a1\.[\w-]+\.[\w-]+$/, 2048))
  );
}

/**
 * The launcher's look. The Botify frame sends the installation's widget studio
 * settings in a `look` message; only these keys are read, each validated, and
 * anything else keeps the default (the website widget's default look).
 */
const DEFAULT_LOOK = Object.freeze({
  primary: '#4f46e5',
  radius: 16,
  position: 'right',
  width: 'regular',
  style: 'icon',
  icon: 'chat',
  label: '',
  size: 'md',
  offsetX: 20,
  offsetY: 20,
  hideOnMobile: false,
  animation: 'none',
});
const LOOK_ENUMS = {
  position: ['right', 'left'],
  width: ['compact', 'regular', 'wide'],
  style: ['icon', 'label'],
  icon: ['chat', 'sparkle', 'help', 'headset'],
  size: ['sm', 'md', 'lg'],
  animation: ['none', 'pulse', 'bounce'],
};
/** The domain schema's ranges (`WidgetThemeSchema`, `WidgetLauncherSchema`). */
const LOOK_RANGES = { radius: [0, 24], offsetX: [0, 120], offsetY: [0, 120] };
const MAX_LABEL = 60;
/** The website launcher's sizes (`loader.ts`). */
const LAUNCHER_PX = { sm: 48, md: 56, lg: 64 };
const PANEL_PX = { compact: 340, regular: 380, wide: 440 };
/** Launcher glyphs (24×24, filled), as on the website. */
const ICONS = {
  chat: 'M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2z',
  sparkle:
    'M11 2.5l2.2 6.3 6.3 2.2-6.3 2.2L11 19.5l-2.2-6.3L2.5 11l6.3-2.2zM18.5 15l1 2.5 2.5 1-2.5 1-1 2.5-1-2.5-2.5-1 2.5-1z',
  help: 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm1 17h-2v-2h2v2zm2.07-7.75-.9.92C13.45 12.9 13 13.5 13 15h-2v-.5c0-1.1.45-2.1 1.17-2.83l1.24-1.26A2 2 0 0 0 14 9a2 2 0 0 0-4 0H8a4 4 0 0 1 8 0c0 .88-.36 1.68-.93 2.25z',
  headset:
    'M12 1a9 9 0 0 0-9 9v7a3 3 0 0 0 3 3h3v-8H5v-2a7 7 0 0 1 14 0v2h-4v8h4v1h-7v2h6a3 3 0 0 0 3-3V10a9 9 0 0 0-9-9z',
};
const CLOSE_ICON =
  'M6.4 5 12 10.6 17.6 5 19 6.4 13.4 12 19 17.6 17.6 19 12 13.4 6.4 19 5 17.6 10.6 12 5 6.4z';

/** A look from untrusted input: valid fields are kept, numbers clamped, the rest default. */
export function readLook(value) {
  const look = { ...DEFAULT_LOOK };
  if (Object.prototype.toString.call(value) !== '[object Object]') return look;
  const field = (key) => (Object.hasOwn(value, key) ? value[key] : undefined);
  if (typeof field('primary') === 'string' && /^#[0-9a-fA-F]{6}$/.test(field('primary'))) {
    look.primary = field('primary');
  }
  for (const [key, allowed] of Object.entries(LOOK_ENUMS)) {
    if (allowed.includes(field(key))) look[key] = field(key);
  }
  for (const [key, [min, max]] of Object.entries(LOOK_RANGES)) {
    const number = field(key);
    if (typeof number === 'number' && Number.isFinite(number)) {
      look[key] = Math.min(max, Math.max(min, Math.round(number)));
    }
  }
  if (typeof field('hideOnMobile') === 'boolean') look.hideOnMobile = field('hideOnMobile');
  if (typeof field('label') === 'string') look.label = field('label').trim().slice(0, MAX_LABEL);
  return look;
}

/** Text on the primary colour: white while it keeps a 3:1 contrast, else dark ink (`look.ts`). */
function onColor(hex) {
  const channel = (start) => {
    const value = Number.parseInt(hex.slice(start, start + 2), 16) / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  const luminance = 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
  return 1.05 / (luminance + 0.05) >= 3 ? '#ffffff' : '#111318';
}

function icon(path) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  const shape = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  shape.setAttribute('d', path);
  svg.appendChild(shape);
  return svg;
}

function widgetFrameUrl(value) {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value);
    if (
      (url.protocol === 'https:' || url.protocol === 'http:') &&
      url.origin !== window.location.origin &&
      !url.username &&
      !url.password
    ) {
      return url;
    }
  } catch {
    // Invalid installation URL.
  }
  return null;
}

/** JSON-RPC to this module, with the explicit CSRF header its routes require. */
async function call(route, params) {
  try {
    const response = await browser.fetch(route, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-Botify-CSRF': freshCsrfToken ?? odoo.csrf_token,
      },
      body: JSON.stringify({ jsonrpc: '2.0', method: 'call', id: Date.now(), params }),
    });
    const body = await response.json();
    if (body.error) {
      const expired = /SessionExpired/.test(body.error.data?.name || '');
      return {
        ok: false,
        error: {
          code: expired ? 'AUTH_EXPIRED' : 'INTERNAL',
          message: expired ? _t('the Odoo session expired') : _t('Odoo could not answer'),
        },
      };
    }
    return body.result;
  } catch {
    return { ok: false, error: { code: 'INTERNAL', message: _t('Odoo could not be reached') } };
  }
}

/** Re-reads this session's CSRF token from the server (the page's copy can go stale). */
async function refreshCsrfToken() {
  const answer = await call(CSRF, {});
  if (answer?.ok && typeof answer.result?.csrfToken === 'string') {
    freshCsrfToken = answer.result.csrfToken;
  }
}

/**
 * Where the employee is right now. Prefer the action service's current
 * controller: menu clicks update it immediately and only then push the
 * router (debounced, and without a ROUTE_CHANGE). The router alone is for
 * back/forward and in-app `<a href>` navigations.
 */
function pageContext(actionService) {
  const route = router.current || {};
  const controller = actionService.currentController;
  const ctrl = controller?.state || {};
  const act = controller?.action || {};
  const model = ctrl.model || route.model || act.res_model || null;
  const id = ctrl.resId ?? route.resId ?? null;
  const actionKey = ctrl.action || route.action || act.tag || act.path || act.id || null;
  const name = controller?.displayName || act.display_name || act.name || null;
  const view = controller?.props?.type || route.view_type || null;
  const activeCompanies = user.activeCompanies.map(({ id, name }) => ({
    id,
    name: typeof name === 'string' ? name.slice(0, 80) : undefined,
  }));
  const context = {
    odoo: {
      model: model || null,
      id: id ?? null,
      action: actionKey ?? null,
      ...(name ? { name: String(name).slice(0, 120) } : {}),
      ...(view ? { view: String(view).slice(0, 32) } : {}),
      activeCompanies,
    },
  };
  // Keep the selection's IDs if its names would exceed the widget's 4 KB context limit.
  if (new TextEncoder().encode(JSON.stringify(context)).length > 4096) {
    context.odoo.activeCompanies = activeCompanies.map(({ id }) => ({ id }));
  }
  return context;
}

export const botifyAgentService = {
  dependencies: ['action', 'bus_service'],
  async start(env, { action, bus_service }) {
    const unavailable = { available: false, toggle() {} };
    if (!(await user.hasGroup('botify_agent.group_botify_user'))) {
      return unavailable;
    }
    const first = await call(IDENTITY, {});
    if (!first?.ok || !first.result) {
      return unavailable;
    }
    const { frameUrl, siteKey } = first.result;
    const url = widgetFrameUrl(frameUrl);
    if (!url || typeof siteKey !== 'string' || typeof first.result.assertion !== 'string') {
      return unavailable;
    }

    const host = document.createElement('div');
    host.setAttribute('data-botify-widget', '');
    host.className = 'o_botify_widget';
    const frame = document.createElement('iframe');
    frame.id = 'o_botify_chat';
    frame.title = _t('Botify chat');
    frame.className = 'o_botify_panel';
    frame.setAttribute(
      'sandbox',
      'allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads',
    );
    frame.setAttribute('allow', 'clipboard-write');
    frame.setAttribute('referrerpolicy', 'no-referrer');

    const launcher = document.createElement('button');
    launcher.type = 'button';
    launcher.setAttribute('aria-controls', frame.id);
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.hidden = true;

    let look = DEFAULT_LOOK;
    let open = false;
    let ready = false;
    let entering = true;
    /** Draws launcher and panel from the validated look: only CSS variables and classes. */
    const render = () => {
      const vars = {
        '--bw-primary': look.primary,
        '--bw-on-primary': onColor(look.primary),
        '--bw-ring': `${look.primary}66`,
        '--bw-glow': `${look.primary}55`,
        '--bw-radius': `${look.radius}px`,
        '--bw-size': `${LAUNCHER_PX[look.size]}px`,
        '--bw-ox': `${look.offsetX}px`,
        '--bw-oy': `${look.offsetY}px`,
        '--bw-width': `${PANEL_PX[look.width]}px`,
      };
      for (const [name, value] of Object.entries(vars)) host.style.setProperty(name, value);
      host.className = `o_botify_widget o_botify_${look.position}`;
      frame.classList.toggle('open', open);
      frame.setAttribute('aria-hidden', String(!open));
      const text = look.style === 'label' && look.label && !open ? look.label : null;
      launcher.className = 'launcher';
      launcher.classList.toggle('pill', text !== null);
      launcher.classList.toggle('open', open);
      launcher.classList.toggle('no-mobile', look.hideOnMobile);
      launcher.classList.toggle('enter', entering);
      launcher.classList.toggle('bounce', look.animation === 'bounce');
      launcher.classList.toggle('pulse', look.animation === 'pulse' && !open);
      const nodes = [icon(open ? CLOSE_ICON : ICONS[look.icon])];
      if (text) {
        const span = document.createElement('span');
        span.className = 'text';
        span.dir = 'auto';
        span.textContent = text;
        nodes.push(span);
      }
      launcher.replaceChildren(...nodes, badge);
      launcher.setAttribute('aria-expanded', String(open));
      launcher.setAttribute('aria-label', open ? _t('Close Botify chat') : _t('Open Botify chat'));
    };
    launcher.addEventListener('animationend', () => {
      entering = false;
      launcher.classList.remove('enter');
    });

    const post = (type, payload) => {
      if (ready)
        frame.contentWindow?.postMessage({ botify: PROTOCOL_VERSION, type, payload }, url.origin);
    };
    const setOpen = (next) => {
      open = next;
      render();
      if (!open) launcher.focus();
    };
    const toggle = () => {
      setOpen(!open);
      post(open ? 'open' : 'close', null);
    };
    launcher.addEventListener('click', toggle);

    const freshAssertion = async () => {
      const fresh = await call(IDENTITY, {});
      if (
        fresh?.ok &&
        fresh.result?.frameUrl === frameUrl &&
        fresh.result.siteKey === siteKey &&
        typeof fresh.result.assertion === 'string'
      ) {
        return fresh.result.assertion;
      }
      return null;
    };
    const executeGrant = async ({ callId, args: { grant, refreshCsrf } }) => {
      // Botify sets this on its one retry after a CSRF refusal; nothing ran the first time.
      if (refreshCsrf === true) await refreshCsrfToken();
      const answer = await call(EXECUTE, {
        grant_token: grant,
        context: { allowed_company_ids: user.activeCompanies.map((company) => company.id) },
      });
      const result = answer?.ok
        ? { ok: true, result: answer.result }
        : {
            ok: false,
            error: answer?.error ?? { code: 'INTERNAL', message: _t('Odoo could not answer') },
          };
      post('action.result', {
        callId,
        ok: true,
        result: { ...result, ...(answer?.replayed === true ? { replayed: true } : {}) },
      });
      if (answer?.ok && answer.client?.action) {
        // Only Odoo opens the record. The frame receives the operation result.
        await action.doAction(answer.client.action);
      }
    };

    // Untrusted page data for the agent, never authority. Odoo reloads on company switches.
    const setContext = () => {
      if (ready) post('context', pageContext(action));
    };
    // Back/forward and internal <a href> clicks.
    routerBus.addEventListener('ROUTE_CHANGE', setContext);
    // App/menu doAction: controller is committed here; the router push is later.
    env.bus.addEventListener('ACTION_MANAGER:UI-UPDATED', setContext);

    // Notifications: Odoo keeps the events; the bus only says "look". Every
    // relay tells the frame, even with nothing leased: another tab may have taken
    // the events. An empty notify refreshes this tab's unread count only once its
    // chat has a session; it never starts one (no identity on page loads).
    let relaying = null;
    const relay = () => {
      if (!ready) return;
      relaying ??= call(PENDING, {})
        .then((pending) => {
          if (pending?.ok && Array.isArray(pending.result?.events)) {
            const events = pending.result.events
              .filter((token) => typeof token === 'string' && token.length <= 8192)
              .slice(0, 10);
            post('notifications', { events });
          }
        })
        .finally(() => {
          relaying = null;
        });
      return relaying;
    };

    const initFrame = async () => {
      const assertion = ready ? await freshAssertion() : first.result.assertion;
      if (!assertion) return;
      ready = true;
      const lang = document.documentElement.lang.toLowerCase();
      post('init', {
        siteKey,
        locale: lang.startsWith('ar') ? 'ar' : lang.startsWith('en') ? 'en' : null,
        identity: { assertion },
        context: pageContext(action),
        actions: [],
        open,
      });
      relay();
    };
    const onFrameEvent = async (payload) => {
      if (!hasKeys(payload, ['name', 'data'])) return;
      if (payload.name === 'identity.expired' && hasKeys(payload.data, [])) {
        const assertion = await freshAssertion();
        if (assertion) post('identify', { assertion });
      } else if (payload.name === 'notifications.acked' && isAck(payload.data)) {
        await call(ACK, { acks: payload.data.acks });
      }
    };
    const onFrame = async ({ type, payload }) => {
      if (type === 'ready' && payload === null) {
        await initFrame();
        return;
      }
      if (!ready) return;
      switch (type) {
        case 'action.invoke':
          if (isGrantRequest(payload)) await executeGrant(payload);
          break;
        case 'event':
          await onFrameEvent(payload);
          break;
        case 'state':
          if (hasKeys(payload, ['open']) && payload.open === false && open) setOpen(false);
          break;
        case 'look':
          look = readLook(payload);
          render();
          break;
        case 'unread':
          if (
            hasKeys(payload, ['count']) &&
            Number.isSafeInteger(payload.count) &&
            payload.count >= 0
          ) {
            badge.textContent = String(payload.count);
            badge.hidden = payload.count === 0;
          }
          break;
      }
    };
    window.addEventListener('message', (event) => {
      if (
        !frame.contentWindow ||
        event.source !== frame.contentWindow ||
        event.origin !== url.origin
      ) {
        return;
      }
      if (
        hasKeys(event.data, ['botify', 'type', 'payload']) &&
        event.data.botify === PROTOCOL_VERSION
      ) {
        return onFrame(event.data);
      }
    });

    bus_service.subscribe('botify_agent/event', relay);
    bus_service.start();
    browser.setInterval(relay, RETRY_MS);

    frame.src = url.href;
    render();
    host.append(frame, launcher);
    document.body.appendChild(host);
    return { available: true, toggle };
  },
};

registry.category('services').add('botify_agent', botifyAgentService);
