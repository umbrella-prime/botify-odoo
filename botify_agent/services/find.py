"""Addon-owned shortcut predicates. User filters are authorized separately."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from pytz import UTC, UnknownTimeZoneError, timezone

from odoo import fields
from odoo.fields import Domain

from .errors import OperationError
from .privacy import DIRECTORY_FIELDS


@dataclass(frozen=True)
class Shortcut:
    model: str
    fields: tuple[str, ...]
    start: str | None
    end: str | None = None


TIME_OFF_MODELS = {"hr.leave", "hr.leave.allocation"}
TIME_OFF_TYPE_FIELDS = ("work_entry_type_id", "holiday_status_id")
LEAVE_TYPES = "my_leave_types"
LEAVE_TYPE_FIELDS = ("name", "requires_allocation", "virtual_remaining_leaves", "request_unit", "unit_of_measure")


def time_off_type_field(model):
    if model._name in TIME_OFF_MODELS:
        return next((name for name in TIME_OFF_TYPE_FIELDS if name in model._fields), None)
    return None


SHORTCUTS = {
    "my_time_off": Shortcut(
        "hr.leave",
        ("name", "employee_id", "date_from", "date_to", "request_date_from", "request_date_to",
         "state", "number_of_days"),
        "request_date_from", "request_date_to",
    ),
    "my_allocations": Shortcut(
        "hr.leave.allocation",
        ("name", "date_from", "date_to", "state", "number_of_days"),
        "date_from", "date_to",
    ),
    "my_tasks": Shortcut("project.task", ("name", "project_id", "stage_id", "state", "date_deadline"), "date_deadline"),
    "my_team": Shortcut("hr.employee.public", DIRECTORY_FIELDS, None),
    "my_activities": Shortcut("mail.activity", ("summary", "activity_type_id", "date_deadline", "res_model", "res_id"), "date_deadline"),
    "my_pipeline": Shortcut("crm.lead", ("name", "stage_id", "partner_id", "expected_revenue", "date_deadline"), "date_deadline"),
    "my_quotations": Shortcut("sale.order", ("name", "state", "partner_id", "date_order", "amount_total", "currency_id"), "date_order"),
    "my_expenses": Shortcut(
        "hr.expense", ("name", "employee_id", "date", "total_amount", "currency_id", "state", "product_id", "payment_mode"), "date",
    ),
    "my_timesheets": Shortcut("account.analytic.line", ("name", "date", "project_id", "task_id", "unit_amount"), "date"),
    "my_attendance": Shortcut("hr.attendance", ("check_in", "check_out", "worked_hours"), "check_in"),
    "my_calendar": Shortcut("calendar.event", ("name", "start", "stop", "allday"), "start", "stop"),
    "team_time_off": Shortcut(
        "hr.leave",
        ("employee_id", "request_date_from", "request_date_to", "state", "number_of_days"),
        "request_date_from", "request_date_to",
    ),
}
APPROVALS = "my_approvals_pending"
EMPLOYEE_SHORTCUTS = {"my_time_off", "my_allocations", "my_expenses", "my_timesheets", "my_attendance"}
DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def shortcut_fields(model, policy, spec):
    return [
        name for name in (*spec.fields, time_off_type_field(model))
        if name in model._fields and policy.visible(model, name)
    ]


def _any_employee(domain):
    # This path uses only fixed ownership predicates. It does not read the
    # private employee record or accept an employee identity from the caller.
    return Domain("employee_id", "any!", domain)


def _has_group(env, *groups):
    return any(env.user.has_group(group) for group in groups)


def _leave_officer(env):
    return _has_group(env, "hr_holidays.group_hr_holidays_user", "hr_holidays.group_hr_holidays_manager")


def _direct_reports(env):
    return Domain("parent_id", "any!", Domain("user_id", "=", env.uid))


def _can_read(env, policy, shortcut, fields):
    model = env.get(shortcut.model)
    return (
        model is not None and not policy.model_hidden(model._name) and model.has_access("read")
        and all(name in model._fields and policy.visible(model, name) for name in fields)
    )


def _directory_match(env, policy, names, domain):
    model = env.get("hr.employee.public")
    if model is None or policy.model_hidden(model._name) or not model.has_access("read"):
        return False
    if not all(name in model._fields and policy.visible(model, name) for name in names):
        return False
    return bool(model.search_count(policy.scoped(model, domain, names), limit=1))


def _approval_plans(env, policy):
    plans = []
    leave = SHORTCUTS["team_time_off"]
    if _can_read(env, policy, leave, ("employee_id", "state")):
        if _leave_officer(env):
            plans.append((leave, Domain("state", "in", ["confirm", "validate1"])))
        elif _has_group(env, "hr_holidays.group_hr_holidays_responsible"):
            domain = _any_employee(Domain("leave_manager_id", "=", env.uid))
            # Company approval comes after first approval on a two-step request.
            domain &= Domain("state", "=", "confirm") & Domain("validation_type", "in", ["manager", "both"])
            plans.append((leave, domain))

    expenses = SHORTCUTS["my_expenses"]
    if _can_read(env, policy, expenses, ("employee_id", "state", "manager_id")):
        pending = Domain("state", "=", "submitted")
        assigned = Domain("expense_manager_id", "=", env.uid)
        if _has_group(env, "hr_expense.group_hr_expense_manager"):
            plans.append((expenses, pending))
        elif _has_group(env, "hr_expense.group_hr_expense_user", "hr_expense.group_hr_expense_team_approver"):
            pending &= _any_employee(Domain("user_id", "!=", env.uid))
            if not _has_group(env, "hr_expense.group_hr_expense_user"):
                pending &= _any_employee(assigned | _direct_reports(env)) | Domain("manager_id", "=", env.uid)
            plans.append((expenses, pending))
        elif _directory_match(env, policy, ("expense_manager_id",), assigned):
            # Odoo also grants assigned expense approvers access without a group.
            plans.append((expenses, pending & _any_employee(assigned & Domain("user_id", "!=", env.uid))))
    return plans


def _shortcut(env, policy, name):
    spec = SHORTCUTS[name]
    if spec.model not in env:
        return []
    uid = env.uid
    if name in EMPLOYEE_SHORTCUTS:
        needed = ("employee_id",)
        domain = _any_employee(Domain("user_id", "=", uid))
        if name == "my_timesheets":
            needed += ("project_id",)
            domain &= Domain("project_id", "!=", False)
    elif name == "my_tasks":
        needed = ("user_ids",)
        domain = Domain("user_ids", "in", [uid])
    elif name == "my_team":
        needed = ("parent_id", "user_id", "company_id")
        company = Domain("company_id", "in", env.companies.ids)
        domain = company & Domain("parent_id", "any", Domain("user_id", "=", uid) & company)
    elif name == "my_calendar":
        needed = ("user_id", "partner_ids")
        domain = Domain("user_id", "=", uid) | Domain("partner_ids", "in", [env.user.partner_id.id])
    elif name == "team_time_off":
        needed = ("employee_id",)
        officer = _leave_officer(env)
        if not (officer or _has_group(env, "hr_holidays.group_hr_holidays_responsible")):
            return []
        other = Domain("user_id", "!=", uid)
        if not officer:
            other &= _direct_reports(env) | Domain("leave_manager_id", "=", uid)
        domain = _any_employee(other)
    else:
        needed = ("user_id",)
        domain = Domain("user_id", "=", uid)
        if name == "my_pipeline":
            needed += ("type", "active")
            domain &= Domain("type", "=", "opportunity") & Domain("active", "=", True)
        elif name == "my_quotations":
            needed += ("state",)
            domain &= Domain("state", "in", ["draft", "sent"])
    return [(spec, domain)] if _can_read(env, policy, spec, needed) else []


def shortcut_plans(env, policy, name):
    if name == APPROVALS:
        return _approval_plans(env, policy)
    return _shortcut(env, policy, name)


def available_shortcuts(env, policy):
    offered = [name for name in (*SHORTCUTS, APPROVALS) if shortcut_plans(env, policy, name)]
    if _leave_types_model(env, policy) is not None:
        offered.append(LEAVE_TYPES)
    return offered


def _leave_types_model(env, policy):
    leave = env.get("hr.leave")
    if leave is None or not _can_read(env, policy, SHORTCUTS["my_time_off"], ("employee_id",)):
        return None
    name = time_off_type_field(leave)
    if not name or not policy.visible(leave, name):
        return None
    types = env[leave._fields[name].comodel_name]
    if not policy.model_hidden(types._name) and types.has_access("read") and policy.visible(types, "requires_allocation"):
        return types
    return None


def leave_types(env, policy, first, last):
    types = _leave_types_model(env, policy)
    if types is None:
        raise OperationError("ACCESS_DENIED", env._("Time Off requests are not available to the agent"))
    type_field = time_off_type_field(env["hr.leave"])
    projections = [name for name in LEAVE_TYPE_FIELDS if name in types._fields and policy.visible(types, name)]
    employee = env.user.employee_id
    if not employee:
        return types, type_field, Domain.FALSE, projections
    first = first or last or fields.Date.context_today(env.user)
    last = last or first
    # Bind the stock balance computation to this employee and the requested start date.
    types = types.with_context(employee_id=employee.id, default_employee_id=employee.id, leave_date_from=first.isoformat())
    native = Domain.TRUE
    if "company_id" in types._fields:
        native &= Domain("company_id", "in", [False, env.company.id])
    if "time_off_selectable" in types._fields:
        native &= Domain("time_off_selectable", "=", True)
        country = env.company.country_id.id
        if country and not types.search_count(Domain("country_id", "=", country), limit=1):
            country = False
        native &= Domain("country_id", "=", country)
    eligible = Domain("requires_allocation", "=", False)
    allocation = SHORTCUTS["my_allocations"]
    can_balance = (
        "virtual_remaining_leaves" in projections
        and _can_read(env, policy, allocation, ("employee_id", type_field, "state", "date_from", "date_to", "number_of_days"))
    )
    if can_balance:
        model = env[allocation.model]
        covers = Domain("date_from", "<=", first) & (Domain("date_to", "=", False) | Domain("date_to", ">=", last))
        own = Domain("employee_id", "=", employee.id) & Domain("state", "=", "validate")
        covered = model.search(policy.scoped(model, own & covers)).mapped(type_field).ids
        candidates = types.search(policy.scoped(types, native & Domain("id", "in", covered)))
        eligible |= Domain("id", "in", candidates.filtered(lambda kind: kind.virtual_remaining_leaves > 0).ids)
    else:
        projections = [name for name in projections if name != "virtual_remaining_leaves"]
    return types, type_field, native & eligible, projections


def periods(env, operation):
    dates = []
    for key in ("from", "to"):
        value = operation.get(key)
        if value is None:
            dates.append(None)
            continue
        if not isinstance(value, str) or not DATE.fullmatch(value):
            raise OperationError("VALIDATION_FAILED", env._("use a date in YYYY-MM-DD format"))
        try:
            parsed = date.fromisoformat(value)
            if parsed in (date.min, date.max):
                raise ValueError("exclusive upper bound is outside the supported date range")
        except ValueError:
            raise OperationError("VALIDATION_FAILED", env._("use a date in YYYY-MM-DD format")) from None
        dates.append(parsed)
    start, end = dates
    if start and end and start > end:
        raise OperationError("VALIDATION_FAILED", env._("the starting date must be on or before the ending date"))
    return start, end


def _utc_midnight(day, tz):
    return tz.localize(datetime.combine(day, time.min)).astimezone(UTC).replace(tzinfo=None)


def date_scope(model, policy, spec, start, end):
    if not start and not end:
        return Domain.TRUE, []
    if spec.start is None:
        raise OperationError("VALIDATION_FAILED", model.env._("dates are not available for this shortcut"))
    names = [spec.start] if spec.end is None else [spec.start, spec.end]
    policy.require_fields(model, names, use="filter")
    if any(name not in model._fields for name in names):
        raise OperationError("VALIDATION_FAILED", model.env._("dates are not available for this shortcut"))
    if model._fields[spec.start].type == "datetime":
        try:
            tz = timezone(model.env.user.tz or "UTC")
        except UnknownTimeZoneError:
            tz = UTC
        domain = Domain.TRUE
        if start:
            domain &= Domain(spec.end or spec.start, ">" if spec.end else ">=", _utc_midnight(start, tz))
        if end:
            domain &= Domain(spec.start, "<", _utc_midnight(end + timedelta(days=1), tz))
        if model._name == "calendar.event":
            # Odoo stores all-day events as calendar dates rather than instants.
            all_day = Domain("allday", "=", True)
            day_fields = ["allday", "start_date", "stop_date"]
            policy.require_fields(model, day_fields, use="filter")
            if start:
                all_day &= Domain("stop_date", ">=", start)
            if end:
                all_day &= Domain("start_date", "<=", end)
            return (domain & Domain("allday", "=", False)) | all_day, [*names, *day_fields]
        return domain, names
    domain = Domain.TRUE
    if start:
        lower = Domain(spec.end or spec.start, ">=", start)
        if spec.end:
            lower |= Domain(spec.end, "=", False)
        domain &= lower
    if end:
        domain &= Domain(spec.start, "<=", end)
    return domain, names
