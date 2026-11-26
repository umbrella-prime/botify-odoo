# Botify AI Assistant for Odoo

An AI assistant inside Odoo that answers questions and does the work for each employee, with that employee's own Odoo
permissions. The Odoo module is `botify_agent`; it connects your Odoo to [Botify](https://botifyarabia.ai), an AI
assistant service by Umbrella Prime.

- Reads and changes run as the signed-in employee, under their access rights and record rules.
- Changes wait for the employee's confirmation, unless the administrator turns on **Allow simple changes without
  confirmation** for low-risk changes (off by default). High-impact changes, deletions, financial operations and
  messages to external people always do. A fresh install can only read; the administrator turns on each kind of change.
- Only members of the group **Botify Agent user** see the assistant. Every operation is logged in Odoo.

This repository is published from Botify's source; each branch holds one Odoo series. Please send issues and
questions to hello@umbrellaprime.sa.

## Requirements

- Odoo **19** (branch `19.0`) or **20** (branch `20.0`), **Community** or **Enterprise**.
- **Odoo.sh or on-premise.** Odoo Online does not allow custom Python modules, so the module cannot be installed there.
- A Botify workspace with an Odoo connection ([botifyarabia.ai](https://botifyarabia.ai)).
- Depends only on the standard `base`, `web`, `mail` and `bus` modules. Time-off notifications need the Time Off app.

## Install

1. Put this repository's branch for your Odoo series on the addons path:
   - Odoo.sh: add it as a submodule of your project repository, on branch `19.0` or `20.0`.
   - On-premise: `git clone -b 20.0 https://github.com/umbrella-prime/botify-odoo.git` (or `-b 19.0`), then add the
     folder to `addons_path`.
2. Restart Odoo, open **Apps**, **Update Apps List**, and install **Botify AI Assistant**.

## Configure

1. In Botify, open your assistant, then **Odoo**, **Connect an Odoo**. Enter your Odoo address and database name,
   create the secret, and copy the Botify API URL, installation key and installation secret.
2. In Odoo **Settings, Botify Agent**, paste the three values, tick **Botify agent**, and choose which kinds of change
   to allow (they must also be allowed in Botify).
3. Add employees to the group **Botify Agent user**. They see the Botify chat button after reloading Odoo.
4. Optional: in Botify, change how the chat button looks (colour, side, size, icon or label, distance from the edge)
   in the widget studio of this Odoo installation. Odoo shows the new look after a reload.
5. If your Odoo sends a Content Security Policy, allow the Botify API origin in `frame-src`.

## Upgrade

Pull the branch, restart Odoo, and upgrade **Botify AI Assistant** from Apps (or `odoo -u botify_agent`). Reload open
Odoo tabs afterwards. Existing installs of the module keep their settings: the technical name stays `botify_agent`.

## Data sent to Botify

Nothing is sent to Botify until an administrator enters the Botify connection (API URL, installation key and
installation secret) in **Settings, Botify Agent** and ticks **Botify agent**. Until then every module route refuses
with `INTEGRATION_DISABLED` and no chat is loaded. After that, only employees in the group **Botify Agent user** send
data. When Odoo loads for such an employee, the browser only downloads the chat window from Botify and sends nothing
about the employee. Who the employee is is sent when they open the chat, or when a time-off notification is pending
for them (with that notification). The open page is sent with each chat message. Chat messages, the records the
assistant reads and the capabilities summary are sent only when they use the assistant:

| What | Details |
|---|---|
| Who the employee is | Odoo database name, user ID, partner ID, name, language, time zone, current and allowed company IDs, current company name; signed and valid for 60 seconds |
| Where the employee is | Model, record ID, record name, menu action and view type of the open Odoo page; the companies selected in Odoo |
| What the employee can use | The Odoo apps and menus they can open, the models and fields they can read, and their own employee name, manager and department |
| Chat messages | What the employee writes to the assistant |
| Records the assistant reads | Read with that employee's own access only, plus the result of each change the employee approves |
| Time-off decisions | Leave type, dates, days and who decided; only when the administrator turns on time-off notifications |

The Odoo server never connects to Botify: the employee's browser exchanges this data with Botify through the chat
window. The installation secret stays on the Odoo server and is never sent to employees' browsers or to Botify. No Odoo
password or API key is sent. Botify processes the messages and records with an AI model to answer the employee.

Where the code enforces this:

- `botify_agent/services/config.py`, `configured()`: the integration counts as on only when it is enabled and the API
  URL, installation key and secret are all set.
- `botify_agent/controllers/main.py`, `_available()`: every module route (`identity`, `csrf`, `execute`,
  `events/pending`, `events/ack`) calls it first and refuses unless the integration is configured and the employee is
  in the **Botify Agent user** group. `identity()` lists exactly the identity fields above.
- `botify_agent/static/src/botify_service.js`, `start()`: the browser loads the Botify chat only for group members and
  only after `/botify_agent/v1/identity` succeeds; `pageContext()` builds the page details above.
- `packages/widget/src/client/frame.ts` (the chat window, in the Botify repository): `openSession()` and
  `relayNotifications()` start the Botify session, which carries the identity, only when the chat opens or a
  notification is pending; the open page goes only with `POST messages`.
- `botify_agent/services/capabilities.py`, `manifest()`: builds the capabilities summary only from what the employee
  can read, under the module's privacy settings.
- `botify_agent/models/botify_event.py`: `_botify_notify()` records no time-off event unless notifications are on,
  and `_event_values()` none unless the integration is configured and the employee is in the group.

## License

LGPL-3. See [LICENSE](LICENSE).
