"""Botify's personal-data scope, intersected with native Odoo access.

Only fixed, addon-owned predicates may use internal relation operators. They
match ownership on related rows without including those rows in the result.
Business reads still use the employee's model and record access.
"""

from functools import cached_property

from odoo.fields import Domain

from . import config

EMPLOYEE_MODELS = {"hr.employee", "hr.employee.public", "hr.version", "hr.employee.departure"}
DIRECTORY_FIELDS = ("name", "job_title", "job_id", "department_id", "parent_id", "work_email", "work_phone")
LEAVE_MODELS = {
    "hr.leave", "hr.leave.allocation", "hr.leave.report", "hr.leave.report.calendar",
    "hr.leave.employee.report", "hr.leave.employee.type.report", "hr.work.entry",
}
ATTENDANCE_MODELS = {"hr.attendance", "hr.attendance.overtime.line"}
TIMESHEET_MODELS = {"account.analytic.line", "timesheets.analysis.report", "hr.timesheet.attendance.report"}
RESOURCE_MODELS = {"resource.resource", "resource.calendar", "resource.calendar.leaves"}
EXPENSE_MODELS = {"hr.expense", "hr.expense.split", "hr.expense.split.wizard"}
PERSONAL_MODELS = (
    EMPLOYEE_MODELS | LEAVE_MODELS | ATTENDANCE_MODELS | TIMESHEET_MODELS | RESOURCE_MODELS | EXPENSE_MODELS
)

COMPENSATION_FIELDS = {
    "wage", "contract_wage", "hourly_cost", "salary_distribution", "salary", "fixed_term",
    "trial_date_end", "date_start", "date_end", "is_current", "is_past", "is_future", "is_in_contract",
    "structure_type_id", "contract_date_start", "contract_date_end", "contract_type_id", "contract_template_id",
}
EMPLOYEE_LEAVE_FIELDS = {
    "allocation_display", "allocation_count", "allocation_remaining_display",
    "current_leave_id", "is_absent", "leave_date_from", "leave_date_to",
    "leave_ids", "remaining_leaves", "show_leaves", "leave_manager_id", "leave_status",
}
EMPLOYEE_ATTENDANCE_FIELDS = {
    "attendance_ids", "attendance_state", "attendance_manager_id", "hours_today",
    "hours_last_month", "hours_last_month_overtime", "last_attendance_id", "last_check_in",
    "last_check_out", "overtime_ids", "total_overtime",
}
EMPLOYEE_SCHEDULE_FIELDS = {"resource_id", "resource_calendar_id", "hr_presence_state", "hr_icon_display", "show_hr_icon_display"}
EMPLOYEE_PROFILE_ALIASES = {
    "additional_note", "barcode", "category_ids", "emergency_contact", "emergency_phone",
    "employee_bank_account_ids", "employee_resource_calendar_id", "km_home_work", "pin", "visa_expire",
}
# Reviewed computed projections. Ordinary stored fields use Odoo's field ACLs
# and the owning record's row scope.
SAFE_PROJECTIONS = {
    "hr.leave": {
        "name", "duration_display", "request_date_from", "request_date_to", "request_date_hour_from",
        "request_date_hour_to", "request_unit_half", "request_unit_hours",
    },
    "hr.leave.allocation": {"name", "duration_display", "number_of_days_display", "number_of_hours_display"},
    "hr.leave.report.calendar": {"name", "request_date_hour_from", "request_date_hour_to"},
    "hr.employee": {*DIRECTORY_FIELDS, "legal_name", "birthday", "contract_date_start", "contract_date_end"} | COMPENSATION_FIELDS,
    "hr.employee.public": {"job_title", "phone", "email", "work_location_name", "work_location_type"},
    "hr.version": COMPENSATION_FIELDS,
    "hr.attendance": {"worked_hours", "overtime_hours"},
    "resource.calendar": {"hours_per_day", "hours_per_week", "tz_offset"},
}
POLYMORPHIC_PARENTS = {
    "mail.message": "model",
    "mail.activity": "res_model",
    "mail.followers": "res_model",
    "calendar.event": "res_model",
}


class PrivacyPolicy:
    def __init__(self, env):
        self.env = env
        self.uid = env.uid
        self.strict, self.team_private_data = config.get_privacy_settings(env)

    def protected(self, model):
        name = model._name
        if name == "account.analytic.line":
            return "employee_id" in model._fields and "project_id" in model._fields
        if name == "res.partner.bank":
            return "employee_id" in model._fields
        return name in PERSONAL_MODELS or self.model_hidden(name)

    def model_hidden(self, name):
        return self.strict and (
            name == "mail.tracking.value" or name.startswith(("hr.payslip", "hr.payroll"))
        )

    def field_hidden(self, model, name):
        if not self.strict:
            return False
        model_name = model._name
        if model_name == "hr.expense":
            return name == "duplicate_expense_ids"
        if model_name == "resource.calendar":
            return name in {"leave_ids", "work_resources_count", "version_ids", "employees_count"}
        if model_name == "hr.work.entry.type" and name == "amount_rate":
            return not self._has_group("hr.group_hr_manager")
        if "hr.leave" in self.env:
            if model_name in {"res.partner", "res.users"}:
                return name in {"im_status", "leave_date_to"}
            if model_name in EMPLOYEE_MODELS:
                return name == "im_status"
        return False

    def projection_allowed(self, model, name):
        return name in SAFE_PROJECTIONS.get(model._name, ())

    def _has_group(self, *xmlids):
        return any(self.env.user.has_group(xmlid) for xmlid in xmlids)

    def _company(self, *, shared=False):
        companies = self.env.companies.ids
        return Domain("company_id", "in", companies + [False] if shared else companies)

    def _self(self):
        return Domain("user_id", "=", self.uid)

    def _team(self):
        if not self.team_private_data:
            return Domain.FALSE
        return Domain("parent_id", "any!", self._self() & self._company())

    def _employee(self):
        if self._has_group("hr.group_hr_user"):
            return self._company()
        return self._company() & (self._self() | self._team())

    def employee_directory_scope(self, *, fallback=False):
        if not self.strict:
            return Domain.TRUE
        # An explicit hr.employee read retains its self/team scope when it
        # falls back to public fields. Business rows keep their own role scope.
        return self._employee() if fallback else self._company()

    def _compensation(self):
        if self._has_group("hr.group_hr_manager"):
            return self._company()
        return self._company() & self._self()

    def _leave(self):
        if self._has_group("hr_holidays.group_hr_holidays_user", "hr_holidays.group_hr_holidays_manager"):
            return self._company()
        owners = self._self()
        if self._has_group("hr_holidays.group_hr_holidays_responsible"):
            owners |= Domain("leave_manager_id", "=", self.uid)
        return self._company() & owners

    def _attendance(self):
        if self._has_group("hr_attendance.group_hr_attendance_user", "hr_attendance.group_hr_attendance_manager"):
            return self._company()
        owners = self._self()
        if self._has_group("hr_attendance.group_hr_attendance_officer"):
            owners |= Domain("attendance_manager_id", "=", self.uid)
        return self._company() & owners

    def _expenses(self):
        if self._has_group("hr_expense.group_hr_expense_user", "hr_expense.group_hr_expense_manager", "account.group_account_user"):
            return self._company()
        owners = self._self() | Domain("expense_manager_id", "=", self.uid)
        if self._has_group("hr_expense.group_hr_expense_team_approver"):
            owners |= self._team()
            return self._company() & (
                Domain("employee_id", "any!", owners) | Domain("manager_id", "=", self.uid)
            )
        return self._company() & Domain("employee_id", "any!", owners)

    def _timesheets(self, fields):
        if not self._has_group(
            "hr_timesheet.group_hr_timesheet_approver", "hr_timesheet.group_timesheet_manager", "project.group_project_manager"
        ):
            return Domain("employee_id", "any!", self._self() & self._company())
        if "amount" in fields:
            return Domain("employee_id", "any!", self._compensation())
        return self._company()

    def _resources(self, model):
        material = Domain("resource_type", "=", "material")
        people = Domain("user_id", "=", self.uid)
        if "employee_id" in model._fields:
            people |= Domain("employee_id", "any!", self._employee())
        return self._company(shared=True) & (material | people)

    def _resource_leaves(self, model):
        company = self._company(shared=True)
        if self._has_group("hr_holidays.group_hr_holidays_user", "hr_holidays.group_hr_holidays_manager"):
            return company
        global_leave = Domain("resource_id", "=", False)
        resource = model.env["resource.resource"]
        people = self._self() & self._company(shared=True)
        if "employee_id" in resource._fields and self._has_group("hr_holidays.group_hr_holidays_responsible"):
            people |= Domain("employee_id", "any!", self._leave())
        personal = Domain("resource_id", "any!", people)
        if "holiday_id" not in model._fields:
            return company & (global_leave | personal)
        return company & (
            (Domain("holiday_id", "=", False) & (global_leave | personal))
            | Domain("holiday_id", "any!", Domain("employee_id", "any!", self._leave()))
        )

    def _expense_moves(self, model):
        allowed = self._expenses()
        scope = Domain("expense_ids", "not any!", ~allowed)
        if "existing_expense_ids" in model._fields:
            scope &= Domain("existing_expense_ids", "not any!", ~allowed)
        return scope

    @cached_property
    def _partner_banks(self):
        if self._has_group("hr.group_hr_manager"):
            return self._company(shared=True)
        employees = self.env["hr.employee.public"].search(self._self() & self._company(), order="id")
        own_contacts = (self.env.user.partner_id | employees.work_contact_id).ids
        return Domain("partner_id", "any!", Domain("employee", "=", False) | Domain("id", "in", own_contacts))

    def scope(self, model, fields=()):
        """Return an independent AND operand for the executor's query."""
        if not self.strict:
            return Domain.TRUE
        name = model._name
        fields = set(fields)
        if self.model_hidden(name):
            return Domain.FALSE
        if name in EMPLOYEE_MODELS:
            owner = self._employee() if name != "hr.employee.public" else Domain.TRUE
            if fields & COMPENSATION_FIELDS or any(field.startswith("contract_") for field in fields):
                owner &= self._compensation()
            if fields & EMPLOYEE_LEAVE_FIELDS:
                owner &= self._leave()
            if fields & EMPLOYEE_ATTENDANCE_FIELDS:
                owner &= self._attendance()
            if fields & EMPLOYEE_SCHEDULE_FIELDS:
                owner &= self._employee()
            if name in {"hr.version", "hr.employee.departure"}:
                return Domain("employee_id", "any!", owner)
            return owner
        if name in LEAVE_MODELS:
            return Domain("employee_id", "any!", self._leave())
        if name in ATTENDANCE_MODELS:
            return Domain("employee_id", "any!", self._attendance())
        if name == "hr.expense":
            return self._expenses()
        if name in EXPENSE_MODELS:
            return Domain.FALSE
        if name == "account.analytic.line" and self.protected(model):
            # Non-timesheet analytic entries retain native access.
            return Domain("project_id", "=", False) | self._timesheets(fields)
        if name == "timesheets.analysis.report":
            return self._timesheets(fields)
        if name == "hr.timesheet.attendance.report":
            return Domain("employee_id", "any!", self._attendance())
        if name == "resource.resource":
            return self._resources(model)
        if name == "resource.calendar":
            return self._company(shared=True)
        if name == "resource.calendar.leaves":
            return self._resource_leaves(model)
        if name == "res.partner.bank" and self.protected(model):
            return self._partner_banks
        if name == "account.move" and "expense_ids" in model._fields:
            return self._expense_moves(model)
        if name == "account.move.line" and "expense_id" in model._fields:
            return Domain("expense_id", "not any!", ~self._expenses()) & Domain(
                "move_id", "not any!", ~self._expense_moves(self.env["account.move"])
            )
        if name == "account.payment" and "expense_ids" in model._fields:
            return Domain("move_id", "not any!", ~self._expense_moves(self.env["account.move"]))
        if name == "res.users" and "employee_id" in model._fields and fields & EMPLOYEE_PROFILE_ALIASES:
            return Domain("id", "=", self.uid) | Domain("employee_id", "any!", self._employee())
        if name == "calendar.attendee" and "hr.leave" in self.env:
            return Domain("event_id", "not any!", Domain("res_model", "in", sorted(LEAVE_MODELS)))
        if name in POLYMORPHIC_PARENTS:
            parent = POLYMORPHIC_PARENTS[name]
            if name == "calendar.event":
                # Leave-generated calendar entries name the employee and time off.
                return Domain(parent, "not in", sorted(LEAVE_MODELS))
            return (
                Domain(parent, "not in", sorted(PERSONAL_MODELS))
                & Domain(parent, "not like", "hr.payslip")
                & Domain(parent, "not like", "hr.payroll")
            )
        return Domain.TRUE

    def field_scoped(self, model, name):
        if self.strict and model._name == "res.partner.bank" and self.protected(model):
            return True
        return not self.scope(model, (name,)).is_true()
