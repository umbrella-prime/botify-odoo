"""A compact planning index built from this employee's Odoo permissions."""

import hashlib
import json
import re
from functools import cache

from lxml import etree

from odoo.exceptions import AccessError, MissingError, UserError
from odoo.fields import Domain
from odoo.modules.module import get_manifest

from . import classify, config, find, privacy
from .errors import OperationError

PROTOCOL = 3
MANIFEST_BYTES = 6 * 1024
KEY_FIELDS = (
    "name", "state", "stage_id", "date", "date_order", "date_deadline", "request_date_from",
    "request_date_to", "check_in", "check_out", "start", "stop", "user_id", "user_ids",
    "employee_id", "partner_id", "company_id", "amount_total", "total_amount", "unit_amount",
    "amount_residual", "wage",
)
PERSONAL = (
    "hr.employee.public", "hr.employee", "hr.version", "hr.leave", "hr.leave.allocation",
    "hr.attendance", "hr.expense", "account.analytic.line", "project.task", "mail.activity", "calendar.event",
)
BUTTON_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
DETAIL_FIELDS = 40


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@cache
def _addon_version():
    return str(get_manifest("botify_agent")["version"])


def manifest_hash(env):
    """Identity and policy inputs shared by the identity route and capabilities.

    Active companies are a separate dimension of the caller's cache key. The
    initial identity request has no browser company selection in its context.
    """
    privacy, managers = config.get_privacy_settings(env)
    denied_models, denied_fields = config.get_read_denials(env)
    return hashlib.sha256(_json({
        "protocol": PROTOCOL,
        "addonVersion": _addon_version(),
        "uid": env.uid,
        "groups": sorted(env.user._get_group_ids()),
        "companies": sorted(env.user.company_ids.ids),
        "lang": env.user.lang,
        "tz": env.user.tz,
        "modules": sorted(env.registry._init_modules),
        "registry": env.registry.registry_sequence,
        "privacy": privacy,
        "managers": managers,
        "denied_models": sorted(denied_models),
        "denied_fields": sorted(denied_fields),
        "write_denied": sorted(config.get_refused_write_models(env)),
    })).hexdigest()


def _short(value, maximum=80):
    return " ".join(str(value or "").split())[:maximum]


def _readable(env, policy, name):
    model = env.get(name)
    return bool(
        model is not None and not model._abstract and not model._transient
        and not policy.model_hidden(name) and model.has_access("read")
    )


def _menu_catalog(env, policy):
    """Use Odoo's same menu access check and window-action projection as the web client."""
    menus = env["ir.ui.menu"]
    admitted = menus._visible_menu_ids(debug=False)
    menus = menus.search([("id", "in", sorted(admitted))], order="sequence, id")
    roots = menus.filtered(lambda menu: not menu.parent_id)
    root_ids = set(roots.ids)
    ids = set(menus.ids)
    xmlids = roots._get_menuitems_xmlids()
    app_for_root = {
        root.id: xmlids.get(root.id, "").split(".")[0] or f"menu_{root.id}"
        for root in roots
    }
    apps = {}
    by_app = {}
    forms = {}
    groups = set(env.user._get_group_ids())
    action_data = {}

    for menu in menus:
        root = menu
        while root.parent_id and root.parent_id.id in ids:
            root = root.parent_id
        if root.id not in root_ids:
            continue
        action = menu.action
        if not action:
            continue
        app = app_for_root[root.id]
        if action._name != "ir.actions.act_window":
            # Discuss and other installed client apps also have native visible menus.
            apps.setdefault(app, _short(root.name))
            continue
        if action.id not in action_data:
            try:
                window = action._get_action_dict()
            except (AccessError, MissingError):
                continue
            action_data[action.id] = window
        window = action_data[action.id]
        if window.get("group_ids") and not groups.intersection(window["group_ids"]):
            continue
        name = window.get("res_model")
        if not _readable(env, policy, name):
            continue
        apps.setdefault(app, _short(root.name))
        by_app.setdefault(app, set()).add(name)
        for view_id, kind in window.get("views", ()):
            if kind == "form":
                forms.setdefault((app, name), view_id)
                forms.setdefault(name, view_id)
                break
    return apps, by_app, forms


def _rights(model):
    rights = "r"
    if not classify.write_refused(model.env, model._name):
        rights += "".join(
            letter for letter, operation in (("c", "create"), ("w", "write"), ("u", "unlink"))
            if model.has_access(operation)
        )
    return rights.upper()


def _key_fields(model, policy):
    preferred = ()
    if model._name in find.TIME_OFF_MODELS:
        dates = ("request_date_from", "request_date_to") if model._name == "hr.leave" else ("date_from", "date_to")
        preferred = (find.time_off_type_field(model), *dates, "number_of_days", "state", "employee_id")
    elif model._name == "hr.employee.public":
        preferred = privacy.DIRECTORY_FIELDS
    return [
        name for name in dict.fromkeys((model._rec_name, *preferred, *KEY_FIELDS))
        if name in model._fields and policy.visible(model, name)
    ][:8]


def _model_index(model, policy):
    return {
        "model": model._name,
        "name": _short(model._description or model._name, 60),
        "rights": _rights(model),
        "fields": _key_fields(model, policy),
    }


def _label(model, record_id, policy):
    if not record_id or not _readable(model.env, policy, model._name) or not policy.visible(model, "name"):
        return None
    record = model.search(policy.scoped(model, Domain("id", "=", record_id), ["name"]), limit=1, order="id")
    if record:
        return {"id": record.id, "name": _short(record.name)}
    return None


def _employee(env, policy):
    public = "hr.employee.public"
    if not _readable(env, policy, public):
        return {"name": _short(env.user.name)}
    model = env[public]
    fields = [name for name in ("name", "parent_id", "department_id") if policy.visible(model, name)]
    if not all(policy.visible(model, name) for name in ("user_id", "company_id", "id")):
        return {"name": _short(env.user.name)}
    domain = Domain("user_id", "=", env.uid) & Domain("company_id", "=", env.company.id)
    row = model.search_read(policy.scoped(model, domain, fields), fields or ["id"], limit=1, order="id", load=None)
    if not row:
        return {"name": _short(env.user.name)}
    row = row[0]
    employee = {"id": row["id"], "name": _short(row.get("name") or env.user.name)}
    for field, key in (("parent_id", "manager"), ("department_id", "department")):
        if record_id := row.get(field):
            if info := _label(env[model._fields[field].comodel_name], record_id, policy):
                employee[key] = info
    return employee


def _operators(model, field, policy):
    if not field._description_searchable or field.name == "display_name":
        return []
    try:
        policy.require_fields(model, [field.name], use="filter")
    except OperationError:
        return []
    if field.type == "boolean":
        return ["=", "!="]
    operators = ["=", "!=", "in", "not in"]
    if field.type in ("char", "text"):
        operators += ["ilike", "=ilike"]
    if field.type in ("integer", "float", "monetary", "date", "datetime"):
        operators += [">", ">=", "<", "<="]
    return operators


def _buttons(model, view_id):
    try:
        view = model.get_view(view_id=view_id, view_type="form")
        arch = etree.fromstring(view["arch"].encode(), parser=etree.XMLParser(no_network=True, resolve_entities=False))
    except (AccessError, MissingError, UserError, etree.XMLSyntaxError):
        return []
    buttons = {}
    for button in arch.xpath(".//button[@type='object'][not(ancestor::field)]"):
        name = button.get("name", "")
        invisible = button.get("invisible")
        if BUTTON_NAME.fullmatch(name) and invisible not in ("1", "True"):
            buttons.setdefault(name, {"name": name, "label": _short(button.get("string") or name)})
    return [buttons[name] for name in sorted(buttons)[:24]]


def _details(model, policy, view_id, query):
    fields = {}
    names = list(dict.fromkeys((*_key_fields(model, policy), "id", *sorted(model._fields))))
    if query:
        names.sort(key=lambda name: (query not in name.casefold(),))
    for name in names:
        if len(fields) == DETAIL_FIELDS:
            break
        if name.startswith("_") or not policy.visible(model, name):
            continue
        field = model._fields[name]
        meta = {
            "type": field.type,
            "label": _short(field._description_string(model.env) or name),
            "operators": _operators(model, field, policy),
        }
        if field.relational:
            meta["relation"] = field.comodel_name
        if field.type == "selection":
            meta["choices"] = [[value, _short(label, 45)] for value, label in field._description_selection(model.env)][:16]
        fields[name] = meta
    return {**_model_index(model, policy), "fields": fields, "buttons": _buttons(model, view_id)}


def _fits(result, key, value):
    result[key].append(value)
    if len(_json(result)) <= MANIFEST_BYTES:
        return True
    result[key].pop()
    result["truncated"] = True
    return False


def manifest(env, policy, options):
    apps, by_app, forms = _menu_catalog(env, policy)
    result = {
        "protocol": PROTOCOL,
        "addonVersion": _addon_version(),
        "privacyMode": "strict" if policy.privacy.strict else "follow_odoo",
        "hash": manifest_hash(env),
        "apps": [],
        "models": [],
        "shortcuts": find.available_shortcuts(env, policy),
        "employee": _employee(env, policy),
        "companies": [],
        "lang": env.lang or "",
        "tz": env.user.tz or "",
        "truncated": False,
    }
    if "hr.leave" in env:
        result["timeOffRequestAccess"] = _readable(env, policy, "hr.leave")
    for company in env.companies:
        _fits(result, "companies", {"id": company.id, "name": _short(company.name)})
    app = options.get("app")
    query = _short(options.get("query"), 100).casefold()
    requested = options.get("model")
    discovered = set().union(*by_app.values())
    candidates = sorted(discovered | set(PERSONAL), key=lambda name: (name not in PERSONAL, name))
    if app:
        candidates = sorted(by_app.get(app, ()))
    if requested:
        if not _readable(env, policy, requested):
            raise OperationError("ACCESS_DENIED", env._("this model is not available to the agent"))
        candidates = [requested] if not app or requested in candidates else []
    for app_id, name in sorted(apps.items()):
        if (not app or app_id == app) and (not query or requested or query in f"{app_id} {name}".casefold()):
            _fits(result, "apps", {"id": app_id, "name": name})
    details = bool(requested or app or query)
    if details:
        result["details"] = []
    for name in candidates:
        if not _readable(env, policy, name):
            continue
        model = env[name]
        if query and not requested and query not in f"{name} {model._description}".casefold():
            continue
        if not details:
            if not _fits(result, "models", _model_index(model, policy)):
                break
        else:
            if len(result["details"]) == 5:
                result["truncated"] = True
                break
            view_id = forms.get((app, name)) if app else forms.get(name)
            detail = _details(model, policy, view_id, query if requested else "")
            # A selected model always gets its header and buttons. Trim large
            # field lists before dropping the selected model's entire detail.
            while len(_json({**result, "details": [*result["details"], detail]})) > MANIFEST_BYTES and detail["fields"]:
                detail["fields"].popitem()
                result["truncated"] = True
            if not _fits(result, "details", detail):
                break
    return result
