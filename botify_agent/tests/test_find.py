from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, new_test_user, tagged

from ..services import classify, executor, find
from ..services.errors import OperationError

LIMITED_ACCESS = {
    "access": "limited",
    "note": "Rows outside the employee's Odoo access are not shown; an empty result does not mean none exist.",
}


@tagged("post_install", "-at_install")
class TestBotifyFind(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.employee_user = cls._user("employee")
        cls.peer_user = cls._user("peer")
        cls.manager_user = cls._user("manager")
        if "hr.employee" in cls.env:
            cls.manager = cls.env["hr.employee"].create({
                "name": "Find Manager", "user_id": cls.manager_user.id,
            })
            cls.employee, cls.peer = cls.env["hr.employee"].create([
                {"name": "Find Employee", "user_id": cls.employee_user.id, "parent_id": cls.manager.id},
                {"name": "Find Noor", "user_id": cls.peer_user.id},
            ])

    @classmethod
    def _user(cls, suffix):
        return new_test_user(
            cls.env, login="botify_find_" + suffix, groups="base.group_user,botify_agent.group_botify_user",
            company_id=cls.env.company.id, tz="America/New_York",
        )

    def _model(self, name):
        if name not in self.env:
            self.skipTest("Optional model not installed: " + name)
        return self.env[name].with_context(tracking_disable=True)

    def _run(self, user=None, **arguments):
        env = self.env(user=(user or self.employee_user).id, su=False)
        op = executor.check_operation(env, {"op": "find", **arguments})
        self.assertEqual(classify.classify(env, op), "read")
        return executor.run(env, op)[0]

    def _records(self, shortcut, candidates, expected, user=None, **options):
        result = self._run(user, shortcut=shortcut, filters=[["id", "in", candidates.ids]], **options)
        self.assertEqual({row["id"] for row in result["records"]}, set(expected.ids))
        self.assertEqual(result["total"], len(expected))
        return result["records"]

    def _time_off(self):
        Leave = self._model("hr.leave")
        time_off_group = self.env.ref("hr_holidays.group_hr_holidays_employee", raise_if_not_found=False)
        if time_off_group:
            (self.employee_user | self.peer_user).group_ids |= time_off_group
        self.employee.leave_manager_id = self.manager_user
        field = "work_entry_type_id" if "work_entry_type_id" in Leave._fields else "holiday_status_id"
        Types = self.env[Leave._fields[field].comodel_name]
        values = {
            "name": "Find Time Off", "requires_allocation": False, "request_unit": "day",
            "leave_validation_type": "manager",
        }
        if "unit_of_measure" in Types._fields:
            values.update(code="BOTIFY_FIND_TIME_OFF", unit_of_measure="day")
        leave_type = Types.create(values)
        own, peer = Leave.with_context(leave_fast_create=True).create([
            {
                field: leave_type.id, "name": "Find Leave", "employee_id": employee.id,
                "request_date_from": "2030-03-04", "request_date_to": "2030-03-05",
            }
            for employee in (self.employee, self.peer)
        ])
        return own, peer, field, values

    def test_direct_find_returns_records_count_and_groups(self):
        partners = self.env["res.partner"].create([
            {"name": "Find B", "color": 1},
            {"name": "Find A", "color": 1},
            {"name": "Find C", "color": 2},
        ])
        filters = [["id", "in", partners.ids]]
        result = self._run(model=partners._name, filters=filters, order="name asc", fields=["name"], limit=2)
        self.assertEqual([row["name"] for row in result["records"]], ["Find A", "Find B"])
        self.assertEqual(result["total"], 3)
        self.assertTrue(result["truncated"])
        self.assertEqual(self._run(model=partners._name, filters=filters, mode="count"), {"count": 3})
        result = self._run(
            model=partners._name, filters=filters, mode="group", group_by=["color"], aggregates=["__count"],
        )
        self.assertEqual({group["color"]: group["__count"] for group in result["groups"]}, {1: 2, 2: 1})

    def test_name_operands_take_exact_authorized_targets(self):
        parents = self.env["res.partner"]
        allowed = parents.create({"name": "Find Literal 99%"})
        other = parents.create({"name": "Find Literal 99X"})
        children = parents.create([
            {"name": "Find Child A", "parent_id": allowed.id},
            {"name": "Find Child B", "parent_id": other.id},
        ])
        left = [["id", "in", children.ids]]
        op = {"model": parents._name, "fields": ["name"]}
        result = self._run(filters=left + [["parent_id", "=", {"name": "find literal 99%"}]], **op)
        self.assertEqual([row["id"] for row in result["records"]], children.ids[:1])

    def test_unresolved_names_fail_closed_in_all_modes(self):
        partners = self.env["res.partner"]
        name = "Find Duplicate Name"
        partners.create({"name": name, "active": False})
        visible = partners.create([{"name": name} for _ in range(6)])
        missing = {"field": "parent_id", "name": "Find Missing", "matches": 0, "candidates": []}
        many = {
            "field": "parent_id", "name": name, "matches": "many",
            "candidates": [{"id": row.id, "name": name} for row in visible.sorted("id")[:5]],
        }
        self.assertTrue(self._run(model="res.partner", mode="count")["count"])
        # Neither a negated operand nor the OR fallback may return records.
        filters = ["|", "!", ["parent_id", "=", {"name": missing["name"]}],
                   ["parent_id", "!=", {"name": name}]]
        for options, expected in (
            ({"fields": ["name"]}, {"records": [], "total": 0}),
            ({"mode": "count"}, {"count": 0}),
            ({"mode": "group", "group_by": ["color"], "aggregates": ["__count"]}, {"groups": []}),
        ):
            with self.subTest(options=options):
                result = self._run(model="res.partner", filters=filters, **options)
                self.assertEqual(result, {**expected, "unresolved": [missing, many]})

    def test_name_operand_cannot_infer_a_peer_leave(self):
        own, peer, _, _ = self._time_off()
        filters = [["id", "in", (own | peer).ids], ["employee_id", "=", {"name": self.peer.name}]]
        result = self._run(model="hr.leave", filters=filters, fields=["name"])
        self.assertEqual(result, {"records": [], "total": 0, **LIMITED_ACCESS})
        self.assertEqual(self._run(model="hr.leave", filters=filters, mode="count"),
                         {"count": 0, **LIMITED_ACCESS})
        officer = self._user("time_off_officer")
        officer.group_ids |= self.env.ref("hr_holidays.group_hr_holidays_user")
        self.assertEqual(self._run(officer, model="hr.leave", filters=filters, mode="count"),
                         {"count": 1, **LIMITED_ACCESS})

    def test_time_off_and_team_shortcuts_intersect_dates_and_native_roles(self):
        own, peer, field, _ = self._time_off()
        both = own | peer
        rows = self._records("my_time_off", both, own, **{"from": "2030-03-05", "to": "2030-03-05"})
        other_field = "holiday_status_id" if field == "work_entry_type_id" else "work_entry_type_id"
        self.assertEqual(rows[0][field][0], own[field].id)
        self.assertNotIn(other_field, rows[0])
        self._records("my_time_off", both, own.browse(), **{"to": "2030-03-03"})
        team = self._records("team_time_off", both, own, user=self.manager_user)
        self.assertEqual(team[0][field][0], own[field].id)
        with self.assertRaises(OperationError) as typo:
            self._run(model="hr.leave", fields=[other_field])
        self.assertEqual(typo.exception.code, "VALIDATION_FAILED")
        self.assertIn(f"Did you mean: {field}", str(typo.exception))
        with self.assertRaises(OperationError) as refusal:
            self._run(shortcut="team_time_off")
        self.assertEqual(refusal.exception.code, "ACCESS_DENIED")

    def test_team_time_off_is_unavailable_to_a_direct_manager_without_approver_rights(self):
        self._model("hr.leave")
        self.employee.leave_manager_id = False
        time_off_group = self.env.ref("hr_holidays.group_hr_holidays_employee", raise_if_not_found=False)
        if time_off_group:
            self.manager_user.group_ids |= time_off_group
        self.assertEqual(self.employee.parent_id.user_id, self.manager_user)
        env = self.env(user=self.manager_user.id, su=False)
        self.assertTrue(env["hr.leave"].has_access("read"))
        self.assertFalse(env.user.has_group("hr_holidays.group_hr_holidays_responsible"))
        manifest = executor.run(env, {"op": "capabilities"})[0]
        self.assertIn("my_time_off", manifest["shortcuts"])
        self.assertNotIn("team_time_off", manifest["shortcuts"])
        with self.assertRaises(OperationError) as refusal:
            self._run(self.manager_user, shortcut="team_time_off")
        self.assertEqual(refusal.exception.code, "ACCESS_DENIED")

    def test_allocations_shortcut_uses_own_employee(self):
        Allocation = self._model("hr.leave.allocation")
        _, _, field, values = self._time_off()
        Types = self.env[Allocation._fields[field].comodel_name]
        values.update(name="Find Allocation Type", requires_allocation=True)
        if "code" in values:
            values["code"] = "BOTIFY_FIND_ALLOC"
        allocation_type = Types.create(values)
        own, peer = Allocation.create([
            {"name": "Find Allocation", "employee_id": employee.id, field: allocation_type.id, "number_of_days": 5}
            for employee in (self.employee, self.peer)
        ])
        rows = self._records("my_allocations", own | peer, own)
        self.assertEqual(rows[0][field][0], allocation_type.id)

    def test_tasks_shortcut_uses_current_assignee(self):
        Tasks = self._model("project.task")
        project = self.env["project.project"].create({"name": "Find Project", "privacy_visibility": "employees"})
        own, peer = Tasks.create([
            {"name": "Find Task", "project_id": project.id, "user_ids": [Command.set(user.ids)]}
            for user in (self.employee_user, self.peer_user)
        ])
        self._records("my_tasks", own | peer, own)

    def test_activities_shortcut_uses_current_assignee(self):
        partner = self.env["res.partner"].create({"name": "Find Activity Contact"})
        own, peer = [
            partner.activity_schedule("mail.mail_activity_data_todo", user_id=user.id)
            for user in (self.employee_user, self.peer_user)
        ]
        self._records("my_activities", own | peer, own)
        result = self._run(
            model=own._name, filters=[["id", "in", (own | peer).ids], ["user_id", "=", {"name": self.employee_user.name}]],
            fields=["id"],
        )
        self.assertEqual([row["id"] for row in result["records"]], own.ids)

    def test_pipeline_shortcut_only_returns_own_opportunities(self):
        Leads = self._model("crm.lead")
        (self.employee_user | self.peer_user).group_ids |= self.env.ref("sales_team.group_sale_salesman")
        own, peer = Leads.create([
            {"name": "Find Deal", "type": "opportunity", "user_id": user.id}
            for user in (self.employee_user, self.peer_user)
        ])
        self._records("my_pipeline", own | peer, own)

    def test_create_crm_opportunity_with_native_forecast_fields_is_an_ordinary_write(self):
        self._model("crm.lead")
        self.employee_user.group_ids |= self.env.ref("sales_team.group_sale_salesman")
        env = self.env(user=self.employee_user.id, su=False)
        operation = executor.check_operation(env, {
            "op": "create", "model": "crm.lead",
            "values": {"name": "Forecast only", "type": "opportunity", "expected_revenue": 5000, "probability": 60},
        })
        self.assertEqual(classify.classify(env, operation), "low_write")
        result, _ = executor.run(env, operation)
        self.assertEqual(env["crm.lead"].browse(result["id"]).expected_revenue, 5000)

    def _leave_type(self, name, *, needs_allocation):
        Leave = self._model("hr.leave")
        if group := self.env.ref("hr_holidays.group_hr_holidays_employee", raise_if_not_found=False):
            self.employee_user.group_ids |= group
        type_field = "work_entry_type_id" if "work_entry_type_id" in Leave._fields else "holiday_status_id"
        Types = self.env[Leave._fields[type_field].comodel_name]
        values = {
            "name": name, "requires_allocation": needs_allocation, "request_unit": "day",
            "leave_validation_type": "no_validation",
        }
        if "unit_of_measure" in Types._fields:
            values.update(code=name.replace(" ", "_"), unit_of_measure="day", time_off_selectable=True,
                          country_id=self.env.company.country_id.id)
        return type_field, Types.create(values)

    def test_my_leave_types_excludes_types_without_own_valid_balance(self):
        field, free = self._leave_type("Find Free", needs_allocation=False)
        _, paid = self._leave_type("Find Has Days", needs_allocation=True)
        _, peer_only = self._leave_type("Find Peer Days", needs_allocation=True)
        allocations = self.env["hr.leave.allocation"].create([
            {"name": "Find Allocation", "employee_id": employee.id, field: leave_type.id, "number_of_days": 5,
             "date_from": "2030-01-01", "date_to": "2030-12-31", "state": "confirm"}
            for employee, leave_type in ((self.employee, paid), (self.peer, peer_only))
        ])
        allocations.filtered(lambda allocation: allocation.state == "confirm").action_approve()
        self.assertEqual(set(allocations.mapped("state")), {"validate"})
        env = self.env(user=self.employee_user.id, su=False)
        self.assertIn("my_leave_types", executor.run(env, {"op": "capabilities"})[0]["shortcuts"])
        options = {"from": "2030-03-04", "to": "2030-03-05"}
        result = self._run(shortcut="my_leave_types", filters=[["id", "in", (free | paid | peer_only).ids]], **options)
        self.assertEqual(result["model"], free._name)
        self.assertEqual(result["type_field"], field)
        self.assertEqual({row["id"] for row in result["records"]}, {free.id, paid.id})
        self.assertEqual(next(row["virtual_remaining_leaves"] for row in result["records"] if row["id"] == paid.id), 5)

    def test_create_leave_defaults_to_self_and_reports_the_approved_state(self):
        field, leave_type = self._leave_type("Find Automatic", needs_allocation=False)
        env = self.env(user=self.employee_user.id, su=False)
        values = {field: leave_type.id, "request_date_from": "2030-03-04", "request_date_to": "2030-03-04"}
        operation = executor.check_operation(env, {"op": "create", "model": "hr.leave", "values": values})
        self.assertEqual(classify.classify(env, operation), "low_write")
        result, _ = executor.run(env, operation)
        record = self.env["hr.leave"].browse(result["id"])
        self.assertEqual(record.employee_id, self.employee)
        self.assertEqual(result["state"], record.state)
        self.assertEqual(result["state"], "validate")
        self.assertNotIn("employee_id", operation["values"])
        peer_request = {"op": "create", "model": "hr.leave",
                        "values": {**values, "employee_id": self.peer.id}}
        with self.assertRaises(AccessError):
            executor.run(env, executor.check_operation(env, peer_request))

    def test_my_team_reads_direct_reports_and_public_job_fields_without_hr_rights(self):
        self._model("hr.employee.public")
        job = self.env["hr.job"].create({"name": "Find Analyst"})
        department = self.env["hr.department"].create({"name": "Find Operations"})
        self.employee.write({"job_id": job.id, "department_id": department.id,
                             "work_email": "analyst@example.test", "work_phone": "555 0140"})
        self.env.flush_all()
        env = self.env(user=self.manager_user.id, su=False)
        self.assertFalse(env["hr.employee"].has_access("read"))
        self.assertIn("my_team", executor.run(env, {"op": "capabilities"})[0]["shortcuts"])
        team = self._run(self.manager_user, shortcut="my_team")
        self.assertEqual(team["model"], "hr.employee.public")
        self.assertEqual([row["id"] for row in team["records"]], [self.employee.id])
        row = team["records"][0]
        self.assertEqual(row["job_title"], "Find Analyst")
        self.assertEqual(row["department_id"][0], department.id)
        self.assertEqual(row["parent_id"][0], self.manager.id)
        self.assertEqual(row["work_email"], "analyst@example.test")
        self.assertEqual(row["work_phone"], "555 0140")
        public = self._run(self.manager_user, model="hr.employee.public",
                           filters=[["id", "=", self.employee.id]], fields=["job_title"])
        self.assertEqual(public["records"][0]["job_title"], "Find Analyst")
        fields = ["name", "job_title", "job_id", "department_id", "parent_id", "work_email", "work_phone"]
        direct = self._run(self.manager_user, model="hr.employee",
                           filters=[["parent_id", "=", self.manager.id]], fields=fields)
        self.assertEqual([item["id"] for item in direct["records"]], [self.employee.id])
        self.assertEqual(direct["records"][0]["job_id"][0], job.id)
        self.assertEqual(direct["records"][0]["job_title"], "Find Analyst")
        with self.assertRaises(OperationError) as refusal:
            self._run(self.manager_user, model="hr.employee",
                      filters=[["version_id.job_title", "ilike", "Analyst"]], fields=["id"])
        self.assertEqual(refusal.exception.code, "ACCESS_DENIED")
        with self.assertRaises(OperationError):
            self._run(self.manager_user, shortcut="my_team", fields=["wage"])

    def test_native_access_denies_assigned_task_deadline_write_for_plain_internal_user(self):
        tasks = self._model("project.task")
        project = self.env["project.project"].create({"name": "Find Atlas General", "privacy_visibility": "employees"})
        task = tasks.create({"name": "Find Onboarding", "project_id": project.id,
                             "user_ids": [Command.set(self.employee_user.ids)]})
        env = self.env(user=self.employee_user.id, su=False)
        native = task.with_env(env)
        self.assertFalse(env.user.has_group("project.group_project_user"))
        self.assertTrue(native.has_access("read"))
        self.assertFalse(native.has_access("write"))
        values = {"date_deadline": "2030-03-05 09:00:00"}
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            native.write(values)
        operation = executor.check_operation(env, {"op": "write", "model": tasks._name, "ids": task.ids, "values": values})
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            executor.run(env, operation)

    def test_quotations_shortcut_only_returns_own_drafts_and_sent(self):
        Sales = self._model("sale.order")
        (self.employee_user | self.peer_user).group_ids |= self.env.ref("sales_team.group_sale_salesman")
        customer = self.env["res.partner"].create({"name": "Find Quote Contact"})
        own, peer = Sales.create([
            {"partner_id": customer.id, "user_id": user.id}
            for user in (self.employee_user, self.peer_user)
        ])
        self.assertEqual(own.state, "draft")
        self._records("my_quotations", own | peer, own)

    def _expenses(self):
        Expenses = self._model("hr.expense")
        self.manager_user.group_ids |= self.env.ref("hr_expense.group_hr_expense_team_approver")
        self.employee.expense_manager_id = self.manager_user
        product = self.env["product.product"].create({
            "name": "Find Expense Product", "type": "service", "purchase_ok": True,
            "can_be_expensed": True, "standard_price": 0,
        })
        return Expenses.create([
            {
                "name": "Find Expense", "employee_id": employee.id, "product_id": product.id,
                "total_amount_currency": 21, "approval_state": "submitted",
            }
            for employee in (self.employee, self.peer)
        ])

    def test_expenses_shortcut_and_pending_approval_use_distinct_roles(self):
        own, peer = self._expenses()
        rows = self._records("my_expenses", own | peer, own)
        self.assertEqual(rows[0]["state"], "submitted")
        with self.assertRaises(OperationError) as refusal:
            self._run(shortcut=find.APPROVALS, mode="count")
        self.assertEqual(refusal.exception.code, "ACCESS_DENIED")
        result = self._run(
            self.manager_user, shortcut=find.APPROVALS, filters=[["id", "in", (own | peer).ids]], fields=["state"],
        )
        self.assertEqual([(row["model"], row["id"]) for row in result["records"]], [(own._name, own.id)])
        self.assertEqual(result["access"], "limited")
        grouped = self._run(
            self.manager_user, shortcut=find.APPROVALS, filters=[["id", "in", (own | peer).ids]],
            mode="group", group_by=["state"], aggregates=["__count"],
        )
        self.assertEqual(grouped["access"], "limited")
        unresolved = self._run(
            self.manager_user, shortcut=find.APPROVALS,
            filters=[["employee_id", "=", {"name": "No Such Approval Employee"}]], mode="count",
        )
        self.assertTrue(unresolved["unresolved"])
        self.assertEqual(unresolved["access"], "limited")
        self.assertEqual(unresolved["note"], LIMITED_ACCESS["note"])
        self.manager_user.group_ids -= self.env.ref("hr_expense.group_hr_expense_team_approver")
        # Explicit Expense Manager assignment grants native approval even without the team group.
        result = self._run(
            self.manager_user, shortcut=find.APPROVALS, filters=[["id", "in", (own | peer).ids]], mode="count",
        )
        self.assertEqual(result, {"count": 1, **LIMITED_ACCESS})

    def test_pending_time_off_only_for_assigned_approver(self):
        own, peer, _, _ = self._time_off()
        result = self._run(self.manager_user, shortcut=find.APPROVALS, fields=["state"])
        self.assertEqual([(row["model"], row["id"]) for row in result["records"]], [(own._name, own.id)])
        self.assertNotEqual(own.id, peer.id)

    def test_timesheets_shortcut_excludes_peers_for_even_an_approver(self):
        Lines = self._model("account.analytic.line")
        if "employee_id" not in Lines._fields or "project_id" not in Lines._fields:
            self.skipTest("Timesheets not installed")
        self.employee_user.group_ids |= self.env.ref("hr_timesheet.group_hr_timesheet_approver")
        plan = self.env["account.analytic.plan"].create({"name": "Find Analytic Plan"})
        account = self.env["account.analytic.account"].create({"name": "Find Account", "plan_id": plan.id})
        project = self.env["project.project"].create({
            "name": "Find Hours Project", "privacy_visibility": "employees", "allow_timesheets": True,
            "account_id": account.id,
        })
        own, peer = Lines.create([
            {"name": "Find Hours", "employee_id": employee.id, "project_id": project.id, "unit_amount": 2}
            for employee in (self.employee, self.peer)
        ])
        self._records("my_timesheets", own | peer, own)

    def test_attendance_shortcut_converts_local_dates(self):
        Attendance = self._model("hr.attendance")
        self.employee_user.group_ids |= self.env.ref("hr_attendance.group_hr_attendance_own_reader")
        before, own, peer = Attendance.create([
            {"employee_id": employee.id, "check_in": check_in, "check_out": check_out}
            for employee, check_in, check_out in (
                (self.employee, "2030-03-04 04:30:00", "2030-03-04 04:45:00"),
                (self.employee, "2030-03-04 05:00:00", "2030-03-04 12:00:00"),
                (self.peer, "2030-03-04 08:00:00", "2030-03-04 12:00:00"),
            )
        ])
        self._records("my_attendance", before | own | peer, own, **{"from": "2030-03-04", "to": "2030-03-04"})

    def test_calendar_shortcut_uses_organizer_or_attendee(self):
        Calendar = self._model("calendar.event")
        events = Calendar.create([
            {
                "name": "Find Meeting", "start": "2030-03-04 08:00:00", "stop": "2030-03-04 09:00:00",
                "user_id": owner.id, "partner_ids": [Command.set(attendee.partner_id.ids)],
            }
            for owner, attendee in (
                (self.employee_user, self.employee_user),
                (self.peer_user, self.employee_user),
                (self.peer_user, self.peer_user),
            )
        ])
        self._records("my_calendar", events, events[:2], **{"from": "2030-03-04", "to": "2030-03-04"})
