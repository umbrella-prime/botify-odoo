"""Runs one granted operation as the logged-in employee (``request.env.user``).

Hard rules (tested): no ``sudo()`` here, public methods only, CRUD or a
registered business method, no caller-supplied context beyond the
allowlist, generic writes never touch state/company or protected fields.
Odoo's access rights and record rules apply alongside the installation's read protections.
"""

import difflib
import re

from odoo import fields as odoo_fields
from odoo.exceptions import AccessError
from odoo.fields import Domain

from . import capabilities, classify, config, errors, find, privacy
from .errors import OperationError

LIMITS = {"records": 80, "fields": 25, "domain": 50, "in_values": 200, "bulk": 50, "references": 50, "groupby": 3, "values": 40, "text": 20000}
MODEL = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z0-9_]+)*$")
FIELD = re.compile(r"^[a-z_][a-z0-9_]*$")
PATH = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*){0,2}$")
METHOD = re.compile(r"^[a-z][a-z0-9_]*$")
ORDER = re.compile(r"^[a-z_][a-z0-9_]*( (asc|desc))?(, ?[a-z_][a-z0-9_]*( (asc|desc))?){0,4}$", re.I)
GROUPBY = re.compile(r"^[a-z_][a-z0-9_]*(:(day|week|month|quarter|year))?$")
AGGREGATE = re.compile(r"^([a-z_][a-z0-9_]*:(sum|avg|min|max|count_distinct)|__count)$")
OPERATORS = {"=", "!=", ">", ">=", "<", "<=", "like", "ilike", "not like", "not ilike", "=like", "=ilike", "in", "not in", "child_of", "parent_of"}
ALLOWED_CONTEXT = ("lang", "tz", "allowed_company_ids")
SELECTION_CHOICES = 50
FIELD_HELP_CHARS = 200
# Audit, chatter and other plumbing fields: never "key" (same list as Botify's schema map).
TECHNICAL_FIELD = re.compile(
    r"^(id|display_name|create_uid|create_date|write_uid|write_date|__last_update)$|^(message|activity|website_message|rating)_"
)
SENSITIVE_FIELD = re.compile(r"password|token|secret|api_?key|(?:^|_)key(?:_|$)|signature|totp|otp|private")
USER_AUTH_FIELD = re.compile(r"^(login(?:_.*)?|log_ids|last_login(?:_.*)?|device_ids)$|^(auth_|oauth_|mfa_|passkey|webauthn_)")
SENSITIVE_MODELS = {"payment.provider", "res.users.apikeys"}
HIDDEN_MODEL_PREFIXES = (*classify.SKIPPED_MODEL_PREFIXES, "auth_")
SCALAR_TYPES = {"boolean", "char", "date", "datetime", "float", "integer", "monetary", "selection", "text"}
NAME_TYPES = {"char", "text"}
LIMITED_ACCESS = {
    "access": "limited",
    "note": "Rows outside the employee's Odoo access are not shown; an empty result does not mean none exist.",
}


class ReadPolicy:
    def __init__(self, env):
        self.models, self.fields = config.get_read_denials(env)
        self.privacy = privacy.PrivacyPolicy(env)
        self.employee_directory = None

    def model_hidden(self, name):
        return (
            name in SENSITIVE_MODELS or name in self.models or name.startswith(HIDDEN_MODEL_PREFIXES)
            or self.privacy.model_hidden(name)
        )

    def field_hidden(self, model, name):
        return (
            SENSITIVE_FIELD.search(name)
            or "%s.%s" % (model._name, name) in self.fields
            or (model._name == "res.users" and USER_AUTH_FIELD.search(name))
            or self.privacy.field_hidden(model, name)
        )

    def require_model(self, model):
        if self.model_hidden(model._name) or not model.has_access("read"):
            raise OperationError("ACCESS_DENIED", model.env._("this model is not available to the agent"))

    def require_fields(self, model, paths, use="project"):
        # Check all denials before suggesting a correction for any unknown field.
        for path in paths:
            self._check_path(model, path.split("."), use)
        return [model._fields[path] if path in model._fields else None for path in paths]

    def _check_path(self, model, names, use, *, source=False, stored_employee_identity=False, seen=frozenset()):
        name, *tail = names
        key = model._name, name
        if (
            key in seen or len(seen) >= 12
            or self.model_hidden(model._name) or self.field_hidden(model, name)
        ):
            self._deny_field(model)
        field = model._fields.get(name)
        private_model = self.employee_directory if model._name == "hr.employee.public" else None
        private_field = private_model._fields.get(name) if private_model is not None else None
        if private_model is not None and (
            not field or (not private_field and name not in privacy.DIRECTORY_FIELDS)
            or self.field_hidden(private_model, name)
        ):
            self._deny_field(model)
        if field is None:
            # A missing field is reported by the caller after checking other inputs.
            for part in tail:
                if SENSITIVE_FIELD.search(part):
                    self._deny_field(model)
            return
        # Odoo 19 publishes this title through a private version link. The
        # link itself is never returned. Keep the deny-list and leaf checks.
        public_job_title_bridge = (
            source and model._name == "hr.employee" and names == ["version_id", "job_title"]
            and field.type == "many2one" and field.comodel_name == "hr.version"
            and {("hr.employee.public", "job_title"), ("hr.employee.public", "employee_id"),
                 ("hr.employee", "job_title")} <= seen
        )
        try:
            # Odoo 20 renamed this to a public Python API.
            check = getattr(model, "check_field_access", None) or model._check_field_access
            if not public_job_title_bridge:
                check(field, "read")
            if private_field is not None and name not in privacy.DIRECTORY_FIELDS:
                check = getattr(private_model, "check_field_access", None) or private_model._check_field_access
                check(private_field, "read")
        except AccessError:
            self._deny_field(model)

        comodel = model.env.get(getattr(field, "comodel_name", None))
        if comodel is not None and self.model_hidden(comodel._name):
            self._deny_field(model)
        next_seen = seen | {key}
        same_employee_version = False
        if tail:
            if comodel is None:
                _refuse(model.env._("invalid relational field path"))
            scoped_target = self.privacy.field_scoped(comodel, tail[0])
            if source:
                # Employee fields delegated to their own version carry the same
                # employee scope on the root field. Other scoped aliases do not.
                same_employee_version = (
                    model._name == "hr.employee" and name in ("version_id", "current_version_id")
                    and comodel._name == "hr.version"
                )
                public_directory_alias = (
                    model._name == "hr.employee.public" and name == "employee_id"
                    and comodel._name == "hr.employee" and self.privacy.projection_allowed(model, tail[0])
                )
                if scoped_target and not (same_employee_version or public_directory_alias or stored_employee_identity):
                    self._deny_field(model)
            elif not comodel.has_access("read") or getattr(field, "bypass_search_access", False) or scoped_target:
                self._deny_field(model)
            self._check_path(comodel, tail, use, source=source, seen=next_seen)

        if field.related:
            # Stock employee identity is materialized on the scoped employee
            # row. Validate the source field without selecting resource rows.
            stored_identity = (
                model._name == "hr.employee" and name in ("name", "user_id")
                and field.store and field.related == "resource_id." + name
            )
            self._check_path(
                model, field.related.split("."), "project",
                source=True, stored_employee_identity=stored_identity, seen=next_seen,
            )
        if name == "display_name" and model._rec_name and model._rec_name != name:
            self._check_path(model, [model._rec_name], "label", source=True, seen=next_seen)
        if not tail and comodel is not None and use in ("project", "group"):
            if field.type in ("many2one", "many2many"):
                # Validate the same label source used when serializing relations.
                target = _relation_model(model, field)
                self._check_path(target, ["id"], "label", seen=next_seen)
                self._check_path(target, ["display_name"], "label", seen=next_seen)
            else:
                self._check_path(comodel, ["id"], "label", seen=next_seen)
        if use in ("filter", "aggregate", "group") and not tail and comodel is not None:
            target = _relation_model(model, field)
            if self.privacy.field_scoped(target, "id"):
                self._deny_field(model)
        if self.privacy.strict and self.privacy.protected(model) and field.type in (
            "properties", "reference", "many2one_reference",
        ):
            self._deny_field(model)
        if self.privacy.protected(model) and (not field.store or field.search):
            if (
                use == "label" or same_employee_version
                or (use in ("project", "filter") and name == "display_name" and not tail)
            ):
                return
            if use != "project" or not self.privacy.projection_allowed(model, name):
                self._deny_field(model)

    def _deny_field(self, model):
        raise OperationError("ACCESS_DENIED", model.env._("a requested field is hidden by this installation"))

    def visible(self, model, name):
        try:
            self.require_fields(model, [name])
        except OperationError:
            return False
        return True

    def scoped(self, model, domain=Domain.TRUE, fields=(), *, privacy_scope=None):
        if privacy_scope is None:
            privacy_scope = self.privacy.scope(model, fields)
        domain = Domain(domain) & privacy_scope
        if self.employee_directory is not None and model._name == "hr.employee.public":
            domain &= self.privacy.employee_directory_scope(fallback=True)
        return domain


def _read_model(env, name, policy, *, allow_directory=True):
    if name not in env or policy.model_hidden(name):
        raise OperationError("ACCESS_DENIED", env._("this model is not available to the agent"))
    model = env[name]
    if allow_directory and name == "hr.employee" and not model.has_access("read") and "hr.employee.public" in env:
        policy.employee_directory = model
        model = env["hr.employee.public"]
    policy.require_model(model)
    return model


def _refuse(message):
    raise OperationError("VALIDATION_FAILED", message)


def _unknown_field(model, name, policy):
    """Refuse an invented field, naming the closest real ones so the caller can correct itself."""
    readable = [
        f for f in model._fields
        if not f.startswith("_") and policy.visible(model, f)
    ]
    close = difflib.get_close_matches(name, readable, n=3, cutoff=0.6)
    close += [f for f in readable if name in f and f not in close][: 3 - len(close)]
    if name in find.TIME_OFF_TYPE_FIELDS and (native := find.time_off_type_field(model)) in readable:
        close = [native, *(field for field in close if field != native)][:3]
    hint = " " + model.env._("Did you mean: %s?", ", ".join(close)) if close else ""
    _refuse(model.env._("unknown field %s on %s.%s Call odoo.describe_model to see the real fields.", name, model._name, hint))


def _ids(env, value, maximum):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        _refuse(env._("ids must list 1 to %d records", maximum))
    if not all(isinstance(i, int) and not isinstance(i, bool) and 0 < i < 2**31 for i in value):
        _refuse(env._("ids must be record ids"))
    return sorted(set(value))


def _domain(model, value, policy, *, resolve_names=False):
    if not isinstance(value, list) or len(value) > LIMITS["domain"]:
        _refuse(model.env._("invalid domain"))
    paths = []
    named = {}
    terms = list(value)
    for index, term in enumerate(value):
        if term in ("&", "|", "!"):
            continue
        if not (isinstance(term, list) and len(term) == 3 and isinstance(term[0], str) and PATH.fullmatch(term[0])):
            _refuse(model.env._("invalid domain term"))
        if not isinstance(term[1], str) or term[1] not in OPERATORS:
            _refuse(model.env._("invalid domain operator"))
        if policy.privacy.protected(model) and term[1] in ("child_of", "parent_of"):
            policy._deny_field(model)
        path, operator, operand = term
        paths.append(path)
        if isinstance(operand, dict):
            text = operand.get("name")
            if (
                not resolve_names or set(operand) != {"name"} or not isinstance(text, str)
                or not text.strip() or len(text) > 200 or not FIELD.fullmatch(path)
                or operator not in ("=", "!=")
            ):
                _refuse(model.env._("a named filter takes one many2one field and = or !="))
            named[index] = text.strip()
            terms[index] = [path, operator, False]
        elif isinstance(operand, list):
            if len(operand) > LIMITS["in_values"]:
                _refuse(model.env._("too many values in a domain term"))
            if any(not isinstance(item, (str, int, float, bool)) for item in operand):
                _refuse(model.env._("invalid domain value"))
        elif operand is not None and not isinstance(operand, (str, int, float, bool)):
            _refuse(model.env._("invalid domain value"))
    employee_field = model._fields.get("employee_id")
    directory = (
        model.env.get("hr.employee.public") if employee_field and employee_field.type == "many2one"
        and employee_field.comodel_name == "hr.employee" else None
    )
    employee_paths = {
        path: path.split(".", 1)[1] for path in paths
        if directory is not None and path.startswith("employee_id.")
    }
    policy.require_fields(model, ["employee_id" if path in employee_paths else path for path in paths], use="filter")
    directory_scope = Domain.TRUE
    employee_guard = Domain.TRUE
    if employee_paths:
        policy.require_model(directory)
        private = model.env["hr.employee"]
        directory_fields = {path.split(".", 1)[0] for path in employee_paths.values()}
        if any(name not in directory._fields or policy.field_hidden(private, name) for name in directory_fields):
            policy._deny_field(model)
        policy.require_fields(directory, list(employee_paths.values()), use="filter")
        directory_scope = policy.scoped(directory, policy.privacy.employee_directory_scope(), directory_fields)
        # The authorized hop stays outside caller-controlled OR and NOT.
        employee_guard = Domain("employee_id", "in", directory._search(directory_scope, active_test=False))
    for path in paths:
        current = directory if path in employee_paths else model
        for part in employee_paths.get(path, path).split("."):
            field = current._fields.get(part)
            if field is None:
                _unknown_field(current, part, policy)
            comodel_name = getattr(field, "comodel_name", None)
            if comodel_name:
                current = current.env[comodel_name]
    # Match the addon label rather than an unchecked custom display-name search.
    label = model._rec_name

    def replace_name(condition):
        if condition.field_expr == "display_name":
            if not label or label == "display_name":
                policy._deny_field(model)
            field = _fields(model, [label], policy, use="filter")[0]
            if not field.store or field.search or field.type not in NAME_TYPES:
                policy._deny_field(model)
            if label not in paths:
                paths.append(label)
            return Domain(label, condition.operator, condition.value)
        if condition.field_expr.endswith(".display_name"):
            policy._deny_field(model)
        return condition

    def employee_condition(condition):
        path = employee_paths.get(condition.field_expr)
        if path is None:
            return condition
        matches = directory._search(directory_scope & Domain(path, condition.operator, condition.value), active_test=False)
        return Domain("employee_id", "in", matches)

    try:
        domain = Domain(terms)
    except ValueError:
        _refuse(model.env._("invalid domain"))
    domain = domain.map_conditions(replace_name)
    if not named:
        return domain.map_conditions(employee_condition) & employee_guard, paths, []

    if any(model._fields[terms[index][0]].type != "many2one" for index in named):
        _refuse(model.env._("a named filter requires a many2one field"))
    unresolved = []
    for index, text in named.items():
        name, operator, _ = terms[index]
        field = model._fields[name]
        matches = _resolve_name(model, field, policy, text)
        if len(matches) == 1:
            terms[index] = [name, operator, matches.id]
        else:
            labels = _labels(matches, policy) if matches else {}
            issue = {
                "field": name, "name": text, "matches": "many" if matches else 0,
                "candidates": [{"id": record_id, "name": label} for record_id, label in labels.items()],
            }
            if issue not in unresolved:
                unresolved.append(issue)
    # A missing or ambiguous name closes the entire query, even under OR or NOT.
    if unresolved:
        return Domain.FALSE, paths, unresolved
    domain = Domain(terms).map_conditions(replace_name).map_conditions(employee_condition)
    return domain & employee_guard, paths, []


def _fields(model, names, policy, use="project"):
    if not isinstance(names, list) or len(names) > LIMITS["fields"]:
        _refuse(model.env._("at most %d fields", LIMITS["fields"]))
    for name in names:
        if not isinstance(name, str) or not PATH.fullmatch(name):
            _refuse(model.env._("fields are plain field names"))
    fields = policy.require_fields(model, names, use=use)
    for name, field in zip(names, fields):
        if not FIELD.fullmatch(name):
            _refuse(model.env._("fields are plain field names"))
        if field is None:
            _unknown_field(model, name, policy)
    return fields


def _order(model, order, policy):
    if not order:
        # Never let an unchecked default order introduce a hidden field.
        return "id", ["id"]
    if not isinstance(order, str) or not ORDER.fullmatch(order):
        _refuse(model.env._("invalid order"))
    terms = [term.lower().split() for term in order.split(",")]
    names = [term[0] for term in terms]
    fields = _fields(model, names, policy, use="order")
    if any(field.type not in SCALAR_TYPES or not field.store for field in fields):
        policy._deny_field(model)
    if "id" not in names:
        terms.append(["id"])
    return ", ".join(" ".join(term) for term in terms), names


def _values(env, values):
    if not isinstance(values, dict) or not 0 < len(values) <= LIMITS["values"]:
        _refuse(env._("values must set 1 to %d fields", LIMITS["values"]))
    for name, value in values.items():
        if not FIELD.match(name):
            _refuse(env._("invalid field name"))
        if isinstance(value, str) and len(value) > LIMITS["text"]:
            _refuse(env._("value of %s is too long", name))
    return values


def _batch_rows(env, op, rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= LIMITS["bulk"]:
        _refuse(env._("a batch takes 1 to %d rows", LIMITS["bulk"]))
    allowed = {"key", "values"} if op == "create_batch" else {"id", "values"}
    for row in rows:
        if not isinstance(row, dict) or not set(row) <= allowed or "values" not in row:
            _refuse(env._("invalid batch row"))
        _values(env, row["values"])
        if op == "write_batch":
            _ids(env, [row.get("id")], 1)
        elif "key" in row and not (isinstance(row["key"], str) and 0 < len(row["key"]) <= 200):
            _refuse(env._("a row key is 1 to 200 characters"))


def _batch_row(env, model, op, index, row, rules, policy):
    """One row in its own savepoint, re-classified and run as the employee: its outcome, never a batch failure."""
    try:
        with env.cr.savepoint():
            classify.row_class(env, op, model, row, rules)
            if op == "write_batch":
                _check_nested_values(model, row["values"], policy)
                record = _existing(model, [row["id"]], policy, row["values"])
                record.write(dict(row["values"]))
                _check_written(record, policy, row["values"])
                return {"row": index, "status": "updated", "id": record.id}
            keys = env["botify.agent.operation"]
            key = row.get("key")
            data, existing = keys._keyed_record(model, key) if key else (None, None)
            if existing and _visible_records(model, existing.ids, policy):
                return {
                    "row": index, "status": "skipped", "id": existing.id,
                    "message": env._("already created by an earlier run"),
                }
            record = _create_record(model, row["values"], policy)
            if key:
                keys._remember_key(model, key, record, data)
            return {"row": index, "status": "created", "id": record.id, **_created_state(record, policy)}
    except classify.Refused as refusal:
        return {"row": index, "status": "failed", "code": refusal.code, "message": str(refusal)[:500]}
    except Exception as failure:  # noqa: BLE001 - a row's failure is reported, the others still run
        normalized = errors.normalize(env, failure)
        return {"row": index, "status": "failed", "code": normalized["code"], "message": normalized["message"]}


def check_operation(env, operation):
    """Structural checks (the grant is authentic; this is defence in depth)."""
    op = operation.get("op")
    if op not in classify.READ_OPS and op not in ("create", "write", "unlink", "call") + classify.BATCH_OPS:
        # Botify uses this exact message to detect older addon versions.
        _refuse("unknown operation")
    model = operation.get("model")
    if op == "find":
        shortcut = operation.get("shortcut")
        if (model is None) == (shortcut is None):
            _refuse(env._("specify a model or one shortcut"))
        if shortcut is not None and (not isinstance(shortcut, str) or shortcut not in (*find.SHORTCUTS, find.APPROVALS, find.LEAVE_TYPES)):
            _refuse(env._("unknown shortcut"))
        mode = operation.get("mode", "records")
        if mode not in ("records", "count", "group"):
            _refuse(env._("invalid find mode"))
        if mode != "group" and any(operation.get(key) is not None for key in ("group_by", "aggregates")):
            _refuse(env._("group fields require group mode"))
        limit = operation.get("limit", 20)
        if type(limit) is not int or not 1 <= limit <= LIMITS["records"]:
            _refuse(env._("invalid find limit"))
        if model is not None and (operation.get("from") is not None or operation.get("to") is not None):
            _refuse(env._("date ranges require a shortcut"))
        find.periods(env, operation)
    if op == "capabilities":
        for key in ("app", "query"):
            value = operation.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 100):
                _refuse(env._("invalid capability filter"))
    if op not in ("context", "list_models", "capabilities", "find") or model is not None:
        if not isinstance(model, str) or not MODEL.fullmatch(model):
            raise OperationError("ACCESS_DENIED", env._("this model is not available to the agent"))
        if op not in classify.READ_OPS and not model.startswith(classify.SKIPPED_MODEL_PREFIXES) and model not in env:
            raise OperationError("RECORD_NOT_FOUND", env._("model %s does not exist", model))
    if op == "call" and not (isinstance(operation.get("method"), str) and METHOD.match(operation["method"])):
        raise OperationError("METHOD_NOT_ALLOWED", env._("only public business methods can be called"))
    if op in ("write", "unlink", "call"):
        operation["ids"] = _ids(env, operation.get("ids"), LIMITS["bulk"])
    elif op in ("read", "open_record"):
        operation["ids"] = _ids(env, operation.get("ids"), LIMITS["records"] if op == "read" else 1)
    if op in ("create", "write"):
        _values(env, operation.get("values"))
    if op in classify.BATCH_OPS:
        _batch_rows(env, op, operation.get("rows"))
    if op == "name_search_batch":
        names = operation.get("names")
        if not isinstance(names, list) or not 1 <= len(names) <= LIMITS["references"]:
            _refuse(env._("name_search_batch takes 1 to at most %d texts", LIMITS["references"]))
        if not all(isinstance(n, str) and 0 < len(n) <= 200 for n in names):
            _refuse(env._("texts must be 1 to 200 characters"))
    if op == "call" and not all(
        isinstance(v, (str, int, float, bool)) or v is None for v in (operation.get("kwargs") or {}).values()
    ):
        _refuse(env._("business action arguments are plain values"))
    return operation


def scoped_env(env, context):
    """The employee's environment, with only allowlisted context keys.

    ``allowed_company_ids`` must be companies the employee belongs to.
    """
    extra = {key: context[key] for key in ALLOWED_CONTEXT if key in (context or {})}
    companies = extra.get("allowed_company_ids")
    if companies is not None:
        if not isinstance(companies, list) or not companies or not set(companies) <= set(env.user.company_ids.ids):
            raise OperationError("ACCESS_DENIED", env._("the selected companies are not allowed for this user"))
    return env(context=dict(env.context, **extra))


def record_link(record):
    """Link to the already authorized record, using the database's base URL."""
    model = record._name
    segment = model if "." in model else "m-" + model
    return "%s/odoo/%s/%d" % (record.get_base_url().rstrip("/"), segment, record.id)


def _form_action(record):
    if get_action := getattr(record, "get_formview_action", None):
        action = get_action()
        if (
            action.get("type") == "ir.actions.act_window"
            and action.get("res_model") == record._name
            and action.get("res_id") == record.id
        ):
            return action
    return {
        "type": "ir.actions.act_window",
        "res_model": record._name,
        "res_id": record.id,
        "views": [(record.get_formview_id(), "form")],
        "target": "current",
        "context": dict(record.env.context),
    }


def _visible_records(model, ids, policy, fields=()):
    if not ids or policy.model_hidden(model._name) or not model.has_access("read"):
        return model.browse()
    domain = policy.scoped(model, Domain("id", "in", ids), fields)
    return model.with_context(active_test=False).search(domain, order="id")._filtered_access("read")


def _existing(model, ids, policy, fields=()):
    policy.require_model(model)
    records = _visible_records(model, ids, policy, fields)
    if len(records) != len(ids):
        raise OperationError("ACCESS_DENIED", model.env._("the requested records are unavailable"))
    return records


def _check_written(records, policy, fields):
    if not policy.privacy.scope(records, fields).is_true():
        _existing(records.browse(), records.ids, policy, fields)


def _create_record(model, values, policy):
    values = dict(values)
    if model._name == "hr.leave" and "employee_id" not in values:
        employee = model.env.user.employee_id
        if not employee:
            raise OperationError("VALIDATION_FAILED", model.env._("no employee is linked to your user in the current company"))
        values["employee_id"] = employee.id
    _check_nested_values(model, values, policy)
    record = model.create(values)
    if model._name == "hr.leave" and not record:
        raise OperationError("VALIDATION_FAILED", model.env._("Odoo did not create the Time Off request. Check the type, allocation and dates."))
    _check_written(record, policy, values)
    return record


def _created_state(record, policy):
    if record._name == "hr.leave" and policy.visible(record, "state"):
        visible = _visible_records(record.browse(), record.ids, policy, ["state"])
        if visible:
            return {"state": visible.state}
    return {}


def _check_nested_values(model, values, policy):
    if not policy.privacy.strict:
        return
    for name, commands in values.items():
        field = model._fields.get(name)
        if field is None or field.type not in ("one2many", "many2many") or not isinstance(commands, list):
            continue
        target = model.env[field.comodel_name]
        scoped = policy.privacy.field_scoped(target, "id")
        for command in commands:
            action = command[0]
            child_values = command[2] if action in (0, 1) else {}
            if scoped:
                if action in (5, 6) or (action == 0 and policy.privacy.protected(target)):
                    raise OperationError("ACCESS_DENIED", model.env._("the requested records are unavailable"))
                if action in (1, 2, 3, 4):
                    ids = _ids(model.env, command[1:2], 1)
                    _existing(target, ids, policy, child_values)
            if child_values:
                _check_nested_values(target, child_values, policy)


def _labels(records, policy):
    name = records._rec_name
    field = records._fields.get(name)
    if (
        field and field.type in NAME_TYPES and policy.visible(records, name)
        and (field.store or not policy.privacy.protected(records) or policy.privacy.projection_allowed(records, name))
    ):
        labels = {row["id"]: row[name] for row in records.read([name], load=None)}
    else:
        labels = {}
    return {
        record.id: labels.get(record.id) or "%s,%d" % (records._name, record.id)
        for record in records
    }


def _relation_model(model, field):
    if field.comodel_name == "hr.employee" and "hr.employee.public" in model.env:
        # Odoo exposes employee names and identifiers via its public directory.
        return model.env["hr.employee.public"]
    return model.env[field.comodel_name]


def _resolve_name(model, field, policy, text):
    target = _relation_model(model, field)
    policy.require_model(target)
    policy.require_fields(target, ["id", "display_name"])
    label = target._rec_name
    label_field = target._fields.get(label)
    if not label_field or label_field.type not in NAME_TYPES:
        _refuse(model.env._("this relation cannot be resolved by name"))
    policy.require_fields(target, [label], use="filter")
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    if label_field.store and not label_field.search:
        domain = Domain(label, "=ilike", escaped)
    elif label_field.inherited and label_field.related and label_field.related.count(".") == 1:
        # Delegated names (users, product variants) ordinarily search through a
        # bypass-access relation. Authorize the source query explicitly.
        relation, name = label_field.related.split(".")
        parent = target.env[target._fields[relation].comodel_name]
        policy.require_model(parent)
        source = parent._fields[name]
        if not source.store or source.search or source.type not in NAME_TYPES:
            _refuse(model.env._("this relation cannot be resolved by name"))
        policy.require_fields(parent, [name], use="filter")
        visible = parent._search(policy.scoped(parent, Domain(name, "=ilike", escaped), [name]))
        domain = Domain(relation, "in", visible)
    else:
        _refuse(model.env._("this relation cannot be resolved by name"))
    domain = policy.scoped(target, domain, ["id", "display_name", label])
    return target.search(domain, limit=5, order="id")


def _relation_values(model, field, ids, policy, labels=False):
    target = _relation_model(model, field)
    names = ["id", "display_name", target._rec_name or "id"]
    if not all(policy.visible(target, name) for name in names):
        return {}
    records = _visible_records(target, ids, policy, names)
    return _labels(records, policy) if labels or field.type == "many2one" else dict.fromkeys(records.ids, True)


def _read_rows(records, names, policy):
    values = records.read([name for name in names if name != "display_name"] or ["id"], load=None)
    if "display_name" in names:
        labels = _labels(records, policy)
        for row in values:
            row["display_name"] = labels[row["id"]]
    for name in names:
        field = records._fields[name]
        if field.type == "many2one":
            allowed = _relation_values(records, field, list({row[name] for row in values if row[name]}), policy)
            for row in values:
                record_id = row[name]
                row[name] = (record_id, allowed[record_id]) if record_id in allowed else False
        elif field.type in ("one2many", "many2many"):
            allowed = _relation_values(records, field, list({i for row in values for i in row[name]}), policy)
            for row in values:
                row[name] = [i for i in row[name] if i in allowed]
    for row in values:
        row["url"] = record_link(records.browse(row["id"]))
    return values


def _aggregate(model, policy, domain, groupby, fields, aggregates, limit):
    # Use raw groups to avoid the web formatter's elevated relation labels
    # and implicit ordering by the related model's default fields.
    for field in fields:
        if field.type != "many2one":
            continue
        target = _relation_model(model, field)
        policy.require_model(target)
        visible = target._search(policy.scoped(target), active_test=False)
        domain &= Domain(field.name, "=", False) | Domain(field.name, "in", visible)
    values = model._read_group(domain, groupby, aggregates, limit=limit)
    if not values:
        return []
    groups = [{"__extra_domain": []} for _ in values]
    for spec, field, column in zip(groupby, fields, zip(*values)):
        if field.relational or field.name == "id":
            ids = [value.id for value in column if value]
            labels = (
                _labels(_visible_records(model, ids, policy, [model._rec_name or "id"]), policy)
                if field.name == "id" else _relation_values(model, field, ids, policy, labels=True)
            )
            for group, record in zip(groups, column):
                if record and record.id not in labels:
                    group.clear()
                if group:
                    value = (record.id, labels[record.id]) if record else False
                    group[spec] = value
                    group["__extra_domain"].append((field.name, "=", record.id if record else False))
        else:
            formatter = model._web_read_group_groupby_formatter(spec, column)
            for group, raw in zip(groups, column):
                if group:
                    value, extra = formatter(raw)
                    group[spec] = value
                    group["__extra_domain"].extend(extra)
    for group, row in zip(groups, values):
        if group:
            group.update(zip(aggregates, row[len(groupby):]))
            group["__extra_domain"] = list(Domain(group["__extra_domain"]))
    return [group for group in groups if group]


def _name_search(model, policy, text, limit):
    candidates = [model._rec_name, *(model._rec_names_search or ())]
    if model._name in ("product.product", "product.template"):
        candidates.extend(("default_code", "barcode"))
    names = []
    for name in dict.fromkeys(candidates):
        field = model._fields.get(name)
        if field and field.type in NAME_TYPES and field.store and not field.search:
            try:
                policy.require_fields(model, [name], use="filter")
            except OperationError:
                continue
            names.append(name)
    domain = Domain.OR(Domain(name, "ilike", text) for name in names) if text else Domain.TRUE
    if text.isascii() and text.isdecimal() and len(text) <= 10 and int(text) < 2**31:
        domain |= Domain("id", "=", int(text))
    domain = policy.scoped(model, domain, ["display_name", model._rec_name or "id", *names])
    records = model.search(domain, limit=limit, order="id")
    return [{"id": record_id, "display_name": label} for record_id, label in _labels(records, policy).items()]


def _check_unchanged(records, expected):
    if not expected:
        return
    for record in records:
        if odoo_fields.Datetime.to_string(record.write_date) != expected:
            raise OperationError("RECORD_CHANGED", records.env._("the record changed since it was read"))


def _json_safe(value):
    if isinstance(value, dict) and value.get("type"):
        # The method opened a wizard or another screen: it needs the employee.
        return {"completed": False, "requiresUserAction": str(value.get("name") or value["type"])[:120]}
    if value is None or isinstance(value, (bool, int, float, str)):
        return {"completed": True, "returned": value if not isinstance(value, str) else value[:500]}
    return {"completed": True}


def _list_models(env, operation, policy):
    query = (operation.get("query") or "")[:100].lower()
    limit = min(int(operation.get("limit") or 30), 50)
    found = []
    # ir.model is not readable by ordinary employees (ACCESS_DENIED every time),
    # so the catalogue comes from the model registry instead: only names and
    # titles, and a model is listed only when the employee may read it.
    for name in sorted(env.registry):
        if policy.model_hidden(name):
            continue
        model = env[name]
        if model._abstract or model._transient:
            continue
        title = model._description or name
        if query and query not in name.lower() and query not in title.lower():
            continue
        if model.has_access("read"):
            found.append({"model": name, "name": title})
        if len(found) >= limit:
            break
    return {"models": found}


def describe_model(model, policy):
    """Fields this employee may read (and whether they may write them) and the allowed methods."""
    env, model_name = model.env, model._name
    policy.require_model(model)
    rules = {rule.field: rule.risk_class for rule in env["botify.agent.field_rule"].search([("model", "=", model_name)])}
    described = {}
    fields_hidden = False
    for name, field in model._fields.items():
        if name.startswith("_"):
            continue
        if not policy.visible(model, name):
            fields_hidden = True
            continue
        meta = {"type": field.type, "string": field.string}
        help_text = " ".join(field.help.split())[:FIELD_HELP_CHARS] if field.help else ""
        if help_text:
            meta["help"] = help_text
        if field.relational:
            meta["relation"] = field.comodel_name
        if classify.writable_problem(model, name):
            meta["readonly"] = True
        if field.required:
            meta["required"] = True
            # Required with no default (and not computed): a create must set it.
            meta["requiredOnCreate"] = field.default is None and not field.compute
        if field.type == "selection":
            meta["selection"] = [[value, label] for value, label in field._description_selection(env)][:SELECTION_CHOICES]
        if field.store and "readonly" not in meta and field.type not in ("binary", "image") and not TECHNICAL_FIELD.match(name):
            meta["key"] = True
        if name in rules:
            meta["riskClass"] = rules[name]
        described[name] = meta
    methods = [
        {"name": row.method, "riskClass": row.risk_class, "description": row.description or row.method, "params": row.schema()}
        for row in env["botify.agent.method"].search([("model", "=", model_name), ("active", "=", True)])
    ]
    title = env["ir.model"]._get(model_name).name or model_name
    result = {"model": model_name, "name": title, "fields": described, "methods": methods}
    if fields_hidden:
        result["fieldsHidden"] = True
    if classify.write_refused(env, model_name):
        result["writeRefused"] = True
    return result


def _record_query(model, policy, operation, *, scope=Domain.TRUE, extra_fields=(), resolve_names=False):
    op = operation["op"]
    raw = operation.get("domain")
    domain, filters, unresolved = _domain(model, [] if raw is None else raw, policy, resolve_names=resolve_names)
    domain &= scope
    detail = {"unresolved": unresolved} if unresolved else {}
    used = [*filters, *extra_fields]

    def scoped_query(fields):
        privacy_scope = policy.privacy.scope(model, fields)
        if model._name in privacy.PERSONAL_MODELS and not privacy_scope.is_true():
            detail.update(LIMITED_ACCESS)
        return policy.scoped(model, domain, privacy_scope=privacy_scope)

    limit = min(int(operation.get("limit") or (80 if op == "aggregate" else 20)), LIMITS["records"])
    if op == "search":
        policy.require_fields(model, ["id"])
        order, order_fields = _order(model, operation.get("order"), policy)
        names = operation.get("fields") or ["display_name"]
        _fields(model, names, policy)
        used.extend([*order_fields, *names])
        if "display_name" in used and model._rec_name:
            used.append(model._rec_name)
        scoped = scoped_query(used)
        records = model.search(scoped, offset=int(operation.get("offset") or 0), limit=limit, order=order)
        total = model.search_count(scoped)
        result = {"records": _read_rows(records, names, policy), "total": total, **detail}
        if total > len(records):
            result["truncated"] = True
        return result
    if op == "count":
        return {"count": model.search_count(scoped_query(used)), **detail}
    groupby = operation.get("groupby") or []
    aggregates = operation.get("aggregates") or []
    if not isinstance(groupby, list) or not 0 < len(groupby) <= LIMITS["groupby"] or not all(isinstance(g, str) and GROUPBY.fullmatch(g) for g in groupby):
        _refuse(model.env._("invalid groupby"))
    if not isinstance(aggregates, list) or not 0 < len(aggregates) <= 10 or not all(isinstance(a, str) and AGGREGATE.fullmatch(a) for a in aggregates):
        _refuse(model.env._("invalid aggregates"))
    groups = [g.split(":")[0] for g in groupby]
    inputs = [a.split(":")[0] for a in aggregates if a != "__count"]
    group_fields = _fields(model, groups, policy, use="group")
    _fields(model, inputs, policy, use="aggregate")
    scoped = scoped_query([*used, *groups, *inputs])
    return {"groups": _aggregate(model, policy, scoped, groupby, group_fields, aggregates, limit), **detail}


def _find(env, policy, operation):
    mode = operation.get("mode", "records")
    query = {
        "op": {"records": "search", "count": "count", "group": "aggregate"}[mode],
        "domain": operation.get("filters"),
        "fields": operation.get("fields"),
        "groupby": operation.get("group_by"),
        "aggregates": operation.get("aggregates"),
        "order": operation.get("order"),
        "limit": operation.get("limit", 20),
    }
    if name := operation.get("model"):
        return _record_query(_read_model(env, name, policy), policy, query, resolve_names=True)

    shortcut = operation["shortcut"]
    first, last = find.periods(env, operation)
    if shortcut == find.LEAVE_TYPES:
        types, type_field, scope, fields = find.leave_types(env, policy, first, last)
        if any(name not in fields and name != "id" for name in query.get("fields") or ()):
            policy._deny_field(types)
        result = _record_query(types, policy, {**query, "fields": query.get("fields") or fields},
                               scope=scope, resolve_names=True)
        return {**result, "model": types._name, "type_field": type_field}

    plans = find.shortcut_plans(env, policy, shortcut)
    if not plans:
        raise OperationError("ACCESS_DENIED", env._("this shortcut is not available to the agent"))
    results = []
    for spec, scope in plans:
        model = env[spec.model]
        period, used = find.date_scope(model, policy, spec, first, last)
        names = query.get("fields") or find.shortcut_fields(model, policy, spec)
        result = _record_query(
            model, policy, {**query, "fields": names or ["id"]},
            scope=scope & period, extra_fields=used, resolve_names=True,
        )
        results.append((spec.model, result))
    if shortcut != find.APPROVALS:
        model, result = results[0]
        return {**result, "model": model}

    access = LIMITED_ACCESS if any(result.get("access") == "limited" for _, result in results) else {}
    unresolved = []
    for _, result in results:
        for issue in result.get("unresolved", []):
            if issue not in unresolved:
                unresolved.append(issue)
    if unresolved:
        empty = {"count": 0} if mode == "count" else {"groups": []} if mode == "group" else {"records": [], "total": 0}
        return {**access, **empty, "unresolved": unresolved}

    # Keep the source model with records and groups in this cross-app queue.
    limit = operation.get("limit", 20)
    if mode == "count":
        return {**access, "count": sum(result["count"] for _, result in results)}
    if mode == "group":
        groups = [{**row, "model": model} for model, result in results for row in result["groups"]]
        return {**access, "groups": groups[:limit]}
    records = [{**row, "model": model} for model, result in results for row in result["records"]]
    total = sum(result["total"] for _, result in results)
    result = {**access, "records": records[:limit], "total": total}
    if total > len(result["records"]):
        result["truncated"] = True
    return result


def run(env, operation):
    """Executes the operation; returns ``(result, client)`` where ``client`` is
    for the employee's browser only (e.g. the form to open), never for Botify."""
    op = operation["op"]
    if op == "context":
        user = env.user
        return {
            "uid": user.id,
            "name": user.name,
            "company": {"id": env.company.id, "name": env.company.name},
            "companies": [{"id": c.id, "name": c.name} for c in env.companies],
            "lang": env.lang,
            "tz": user.tz,
            # What this addon can run beyond single operations (ADR 0037).
            "operations": list(classify.BATCH_OPS),
        }, None
    policy = ReadPolicy(env)
    if op == "capabilities":
        return capabilities.manifest(env, policy, operation), None
    if op == "find":
        return _find(env, policy, operation), None
    if op == "list_models":
        return _list_models(env, operation, policy), None

    name = operation["model"]
    model = _read_model(env, name, policy, allow_directory=op != "describe_model") if op in classify.READ_OPS else env[name]
    if op == "describe_model":
        return describe_model(model, policy), None
    if op in ("read", "name_search", "name_search_batch", "open_record"):
        policy.require_fields(model, ["id"])
    if op in ("name_search", "name_search_batch", "open_record"):
        policy.require_fields(model, ["display_name"])

    if op in ("search", "count", "aggregate"):
        return _record_query(model, policy, operation), None
    if op == "read":
        names = operation.get("fields") or ["display_name"]
        _fields(model, names, policy)
        used = [*names, model._rec_name] if "display_name" in names and model._rec_name else names
        records = _existing(model, operation["ids"], policy, used)
        return {"records": _read_rows(records, names, policy), "total": len(records)}, None
    if op == "name_search":
        return _name_search(model, policy, str(operation.get("name") or "")[:200], min(int(operation.get("limit") or 8), 20)), None
    if op == "name_search_batch":
        limit = min(int(operation.get("limit") or 5), 20)
        return {"results": [
            {"name": text, "matches": _name_search(model, policy, text, limit)} for text in operation["names"]
        ]}, None
    if op == "open_record":
        record = _existing(model, operation["ids"], policy, ["display_name", model._rec_name or "id"])
        return {"id": record.id, "display_name": _labels(record, policy)[record.id], "url": record_link(record)}, {
            "action": _form_action(record)
        }

    if op == "create":
        record = _create_record(model, operation["values"], policy)
        result = {"id": record.id, **_created_state(record, policy)}
        if policy.visible(model, "display_name") and _visible_records(model, record.ids, policy, [model._rec_name or "id"]):
            result.update(display_name=_labels(record, policy)[record.id], url=record_link(record))
        return result, None
    if op == "write":
        for name in operation["values"]:
            problem = classify.writable_problem(model, name)
            if problem:
                raise OperationError("OPERATION_REFUSED", problem)
        records = _existing(model, operation["ids"], policy, operation["values"])
        _check_nested_values(model, operation["values"], policy)
        _check_unchanged(records, operation.get("expectedWriteDate"))
        records.write(dict(operation["values"]))
        _check_written(records, policy, operation["values"])
        return {"ids": records.ids, "updated": len(records)}, None
    if op in classify.BATCH_OPS:
        rules = classify.field_rules(env)
        rows = [_batch_row(env, model, op, index, row, rules, policy) for index, row in enumerate(operation["rows"])]
        return {"rows": rows}, None
    if op == "unlink":
        records = _existing(model, operation["ids"], policy, [model._rec_name or "id"])
        names = list(_labels(records, policy).values())
        records.unlink()
        return {"deleted": operation["ids"], "names": names}, None
    if op == "call":
        # classify() already required an active registry row and a valid contract.
        records = _existing(model, operation["ids"], policy)
        _check_unchanged(records, operation.get("expectedWriteDate"))
        returned = getattr(records, operation["method"])(**(operation.get("kwargs") or {}))
        if not policy.privacy.scope(model).is_true() or policy.privacy.protected(model):
            # Protected business methods can return a record-derived scalar or action label.
            returned = {"type": "ir.actions.act_window"} if isinstance(returned, dict) and returned.get("type") else None
        return dict(_json_safe(returned), ids=records.ids), None
    _refuse("unknown operation")
