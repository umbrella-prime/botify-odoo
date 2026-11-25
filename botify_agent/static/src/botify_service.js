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
    frame.hidden = true;
    frame.setAttribute(
      'sandbox',
      'allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads',
    );
    frame.setAttribute('allow', 'clipboard-write');
    frame.setAttribute('referrerpolicy', 'no-referrer');

    const launcher = document.createElement('button');
    launcher.type = 'button';
    launcher.className = 'launcher btn btn-primary';
    launcher.textContent = _t('Botify');
    launcher.setAttribute('aria-controls', frame.id);
    launcher.setAttribute('aria-expanded', 'false');
    const badge = document.createElement('span');
    badge.className = 'badge text-bg-danger ms-1';
    badge.hidden = true;
    launcher.append(badge);

    let open = false;
    let ready = false;
    const post = (type, payload) => {
      if (ready)
        frame.contentWindow?.postMessage({ botify: PROTOCOL_VERSION, type, payload }, url.origin);
    };
    const setOpen = (next) => {
      open = next;
      frame.hidden = !open;
      launcher.setAttribute('aria-expanded', String(open));
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
    // relay tells Botify, even with nothing leased: another tab may have taken
    // the events, and an empty notify still refreshes this tab's unread count.
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
    host.append(frame, launcher);
    document.body.appendChild(host);
    return { available: true, toggle };
  },
};

registry.category('services').add('botify_agent', botifyAgentService);
