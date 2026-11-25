from datetime import date
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, new_test_user, tagged

from ..services import config, executor
from ..services.errors import OperationError

LIMITED_ACCESS = {
    "access": "limited",
    "note": "Rows outside the employee's Odoo access are not shown; an empty result does not mean none exist.",
}


@tagged("post_install", "-at_install")
class TestBotifyPrivacy(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        config.write_parameter(cls.env, "botify_agent.personal_data_privacy", False)
        config.write_parameter(cls.env, "botify_agent.allow_team_private_data", False)
        cls.plain = cls._user("plain")
        cls.peer = cls._user("peer")
        # Settings gives this manager native read on the model, without making them an HR Officer.
        cls.manager = cls._user("manager", "base.group_system")
        if "hr.employee" not in cls.env:
            return
        cls.officer = cls._user("hr_officer", "hr.group_hr_user")
        cls.hr_admin = cls._user("hr_admin", "hr.group_hr_manager")
        employees = cls.env["hr.employee"].with_context(tracking_disable=True)
        cls.manager_employee = employees.create({"name": "Phase One Manager", "user_id": cls.manager.id})
        cls.own_employee, cls.peer_employee, cls.officer_employee = employees.create([
            {"name": "Phase One Employee", "user_id": cls.plain.id, "parent_id": cls.manager_employee.id},
            {"name": "Phase One Peer", "user_id": cls.peer.id, "birthday": "1990-08-07", "wage": 13731.0},
            {"name": "Phase One Officer", "user_id": cls.officer.id},
        ])
        cls.own_employee.birthday = "1994-02-03"
        cls.manager_employee.birthday = "1985-01-02"
        cls.officer_employee.birthday = "1988-04-05"

    @classmethod
    def _user(cls, persona, *roles):
        return new_test_user(
            cls.env, login=f"botify_privacy_{persona}",
            groups=",".join(["base.group_user", "botify_agent.group_botify_user", *roles]),
            context={"tracking_disable": True},
            company_id=cls.env.company.id,
        )

    def _require_models(self, *models):
        missing = [model for model in models if model not in self.env]
        if missing:
            self.skipTest("Optional Odoo apps not installed: " + ", ".join(missing))

    def _execute(self, user, model, op, **arguments):
        env = self.env(user=user.id, su=False)
        self.assertFalse(env.su)
        operation = executor.check_operation(env, {"op": op, "model": model, **arguments})
        return executor.run(env, operation)[0]

    def _search_ids(self, user, model, domain, fields=("id",), **arguments):
        answer = self._execute(user, model, "search", domain=domain, fields=list(fields), **arguments)
        return {row["id"] for row in answer["records"]}

    def _assert_denied(self, user, model, op, **arguments):
        with self.assertRaises(OperationError) as refusal:
            self._execute(user, model, op, **arguments)
        self.assertEqual(refusal.exception.code, "ACCESS_DENIED")
        return str(refusal.exception)

    def _assert_same_as_missing(self, user, model, record_id, op="read", **arguments):
        denied = self._assert_denied(user, model, op, ids=[record_id], **arguments)
        missing = self._assert_denied(user, model, op, ids=[2**31 - 1], **arguments)
        self.assertEqual(denied, missing)

    def _time_off_user(self, user):
        # 20 has an explicit Time Off Employee role where 19 granted the equivalent to every internal user.
        group = self.env.ref("hr_holidays.group_hr_holidays_employee", raise_if_not_found=False)
        if group:
            user.group_ids |= group
        return user

    def _leaves(self):
        self._require_models("hr.leave", "hr.leave.allocation", "hr.leave.report.calendar", "resource.calendar.leaves")
        self._time_off_user(self.plain)
        self._time_off_user(self.manager)
        self.time_off_officer = self._user("time_off", "hr_holidays.group_hr_holidays_user")
        self.own_employee.leave_manager_id = self.manager
        self.assertTrue(self.manager.has_group("hr_holidays.group_hr_holidays_responsible"))
        self.assertFalse(self.officer.has_group("hr_holidays.group_hr_holidays_responsible"))
        Leave = self.env["hr.leave"].with_context(tracking_disable=True)
        type_field = "work_entry_type_id" if "work_entry_type_id" in Leave._fields else "holiday_status_id"
        type_model = self.env[Leave._fields[type_field].comodel_name]
        type_values = {"name": "Phase One Test Time Off", "requires_allocation": False, "request_unit": "day"}
        if "unit_of_measure" in type_model._fields:
            type_values.update(code="BOTIFY_PHASE_ONE", unit_of_measure="day")
        leave_type = type_model.create(type_values)
        leave_date = date(2030, 3, 4)
        own, peer = Leave.create([
            {
                type_field: leave_type.id,
                "name": "Phase One Test Leave",
                "employee_id": employee.id,
                "request_date_from": leave_date,
                "request_date_to": leave_date,
            }
            for employee in (self.own_employee, self.peer_employee)
        ])
        return own, peer, type_field, type_values

    def test_privacy_settings_default_to_strict_and_require_settings_access(self):
        self.assertEqual(config.get_privacy_settings(self.env), (True, True))
        Settings = self.env["res.config.settings"]
        settings_values = {
            "botify_personal_data_privacy": "follow_odoo",
            "botify_allow_team_private_data": False,
        }
        with self.assertRaises(AccessError):
            Settings.with_user(self.plain).create(settings_values)
        Settings.with_user(self.manager).create(settings_values).set_values()
        self.assertEqual(config.get_privacy_settings(self.env), (False, False))

    def test_private_employee_fields_and_manager_setting(self):
        self._require_models("hr.employee", "hr.employee.public")
        employees = self.manager_employee | self.own_employee | self.peer_employee
        domain = [["id", "in", employees.ids]]
        self.assertIn(self.peer_employee.id, self._search_ids(
            self.plain, "hr.employee.public", domain, fields=("name",),
        ))
        self._assert_denied(self.plain, "hr.employee", "read", ids=self.peer_employee.ids, fields=["birthday"])
        self.assertEqual(self._search_ids(self.officer, "hr.employee", domain, fields=("birthday",)), set(employees.ids))

        # Exercise the privacy policy even when the installation gives its managers native field access.
        field = employees._fields["birthday"]
        with patch.object(field, "groups", "base.group_system,hr.group_hr_user"):
            self.assertFalse(self.manager.has_group("hr.group_hr_user"))
            native_birthdays = employees.with_user(self.manager).read(["birthday"])
            self.assertEqual({row["id"] for row in native_birthdays}, set(employees.ids))
            self.assertEqual(self._search_ids(
                self.manager, "hr.employee", domain, fields=("birthday",),
            ), {self.own_employee.id, self.manager_employee.id})
            config.write_parameter(self.env, "botify_agent.allow_team_private_data", "False")
            self.assertEqual(self._search_ids(
                self.manager, "hr.employee", domain, fields=("birthday",),
            ), {self.manager_employee.id})

    def test_employee_directory_search_and_reference_use_the_readers_scope(self):
        self._require_models("hr.employee", "hr.employee.public")
        model = "hr.employee"
        domain = [["name", "ilike", "Phase One"]]
        private = self.manager_employee | self.own_employee | self.officer_employee | self.peer_employee
        plain_manager = self._user("plain_manager")
        self.manager_employee.user_id = plain_manager
        # The public SQL view reads hr_employee.user_id, a stored related field.
        self.manager_employee.flush_recordset(["user_id"])
        directory = self.env["hr.employee.public"].with_user(plain_manager)
        self.assertFalse(self.env[model].with_user(plain_manager).has_access("read"))
        self.assertEqual(directory.search([("user_id", "=", plain_manager.id)]).ids, self.manager_employee.ids)
        for user, visible in (
            (self.plain, self.own_employee),
            (plain_manager, self.manager_employee | self.own_employee),
            (self.officer, private),
            (self.hr_admin, private),
        ):
            with self.subTest(user=user.login):
                self.assertEqual(self._search_ids(user, model, domain, fields=("name", "id")), set(visible.ids))
                matches = self._execute(user, model, "name_search", name="Phase One")
                self.assertEqual({row["id"]: row["display_name"] for row in matches},
                                 {employee.id: employee.name for employee in visible})
                groups = self._execute(user, model, "name_search_batch", names=["Phase One Peer", "Phase One Employee"])
                self.assertEqual(
                    [[row["id"] for row in batch["matches"]] for batch in groups["results"]],
                    [[employee.id] if employee in visible else [] for employee in (self.peer_employee, self.own_employee)],
                )
                result = self._execute(user, model, "find", filters=domain, fields=["name", "id"])
                self.assertEqual({row["id"] for row in result["records"]}, set(visible.ids))
        self._assert_denied(self.plain, model, "search", domain=domain, fields=["name", "wage"])
        self._assert_denied(self.officer, model, "search", domain=domain, fields=["name", "wage"])
        result = self._execute(self.hr_admin, model, "search",
                               domain=[["name", "ilike", self.peer_employee.name]], fields=["name", "wage"])
        self.assertEqual(result["records"][0]["wage"], self.peer_employee.wage)

    def test_leave_access_hint_is_independent_of_matching_rows(self):
        own, peer, _, _ = self._leaves()
        model = own._name
        peer_without_leave = self.officer_employee.id
        for employee_id in (peer.employee_id.id, peer_without_leave):
            with self.subTest(employee_id=employee_id):
                domain = [["employee_id", "=", employee_id]]
                self.assertEqual(
                    self._execute(self.plain, model, "search", domain=domain, fields=["id"]),
                    {"records": [], "total": 0, **LIMITED_ACCESS},
                )
                self.assertEqual(
                    self._execute(self.plain, model, "count", domain=domain),
                    {"count": 0, **LIMITED_ACCESS},
                )
                self.assertEqual(
                    self._execute(self.plain, model, "aggregate", domain=domain,
                                  groupby=["state"], aggregates=["__count"]),
                    {"groups": [], **LIMITED_ACCESS},
                )

        shortcut = {"shortcut": "my_time_off", "mode": "count", "from": "2030-03-04", "to": "2030-03-04"}
        self.assertEqual(self._execute(self.plain, None, "find", **shortcut),
                         {"model": model, "count": 1, **LIMITED_ACCESS})

        config.write_parameter(self.env, "botify_agent.personal_data_privacy", "follow_odoo")
        self.assertEqual(
            self._execute(self.plain, model, "search",
                          domain=[["employee_id", "=", peer_without_leave]], fields=["id"]),
            {"records": [], "total": 0},
        )
        self.assertEqual(self._execute(self.plain, None, "find", **shortcut), {"model": model, "count": 1})

    def test_own_leave_search_and_group_support_the_live_field_paths(self):
        own, peer, type_field, _ = self._leaves()
        model = own._name
        fields = ["name", "employee_id", "date_from", "date_to", type_field, "state", "number_of_days"]
        own_domain = [["employee_id.user_id", "=", self.plain.id]]
        peer_domain = [["employee_id.name", "ilike", self.peer_employee.name],
                       ["date_from", ">=", "2030-03-04"], ["state", "in", ["confirm", "validate", "validate1"]]]
        for user, domain, expected in (
            (self.plain, own_domain, own), (self.plain, peer_domain, own.browse()),
            (self.officer, peer_domain, own.browse()), (self.time_off_officer, peer_domain, peer),
        ):
            with self.subTest(user=user.login, domain=domain):
                result = self._execute(user, model, "search", domain=domain, fields=fields, order="date_from desc")
                self.assertEqual([row["id"] for row in result["records"]], expected.ids)
                if expected:
                    self.assertTrue(set(fields) <= result["records"][0].keys())
                    self.assertEqual(result["records"][0]["employee_id"][0], expected.employee_id.id)
                grouped = self._execute(user, model, "aggregate", domain=domain,
                                        groupby=["state"], aggregates=["number_of_days:sum", "__count"])
                self.assertEqual(sum(row["__count"] for row in grouped["groups"]), len(expected))
        found = self._execute(self.plain, model, "find", filters=own_domain, fields=fields, order="date_from desc")
        shortcut = self._execute(self.plain, None, "find", shortcut="my_time_off")
        self.assertEqual([row["id"] for row in found["records"]], own.ids)
        self.assertEqual([row["id"] for row in shortcut["records"]], own.ids)
        self.assertTrue(set(fields) <= shortcut["records"][0].keys())
        self._assert_denied(self.plain, model, "count", domain=[["employee_id.wage", ">", 1]])
        self._assert_denied(self.plain, model, "count", domain=[["name", "ilike", own.name]])

    def test_wage_field_access_precedes_domains_sorts_groups_and_projection(self):
        self._require_models("hr.version")
        version = self.peer_employee.current_version_id
        with self.assertRaises(AccessError):
            self.env["hr.version"].with_user(self.officer).search_count([("wage", ">", 10000)])

        for op, arguments in (
            ("count", {"domain": [["wage", ">", 10000]]}),
            ("count", {"domain": ["&", ["id", "=", 0], ["wage", ">", 10000]]}),
            ("search", {"order": "wage desc", "fields": ["id"]}),
            ("aggregate", {"groupby": ["wage"], "aggregates": ["__count"]}),
            ("aggregate", {"groupby": ["id"], "aggregates": ["wage:sum"]}),
            ("read", {"ids": version.ids, "fields": ["wage"]}),
        ):
            with self.subTest(op=op, arguments=arguments):
                self._assert_denied(self.officer, "hr.version", op, **arguments)
        self._assert_denied(self.officer, "hr.employee", "count", domain=[["version_id.wage", ">", 10000]])
        self._assert_denied(self.plain, "hr.version", "read", ids=version.ids, fields=["wage"])

        answer = self._execute(self.hr_admin, "hr.version", "search",
                               domain=[["id", "=", version.id], ["wage", ">", 10000]],
                               order="wage desc", fields=["wage"])
        self.assertEqual(answer["records"][0]["wage"], 13731.0)

    def test_hourly_cost_is_scoped_beyond_its_native_hr_officer_group(self):
        self._require_models("hr.employee")
        if "hourly_cost" not in self.own_employee._fields:
            self.skipTest("Hourly cost app not installed")
        self.officer_employee.hourly_cost = 41
        self.peer_employee.hourly_cost = 730
        domain = [["id", "in", (self.officer_employee | self.peer_employee).ids]]
        self.assertEqual(self.peer_employee.with_user(self.officer).read(["hourly_cost"])[0]["hourly_cost"], 730)
        self.assertEqual(self._search_ids(
            self.officer, "hr.employee", domain, fields=("hourly_cost",),
        ), {self.officer_employee.id})
        self._assert_same_as_missing(self.officer, "hr.employee", self.peer_employee.id, fields=["hourly_cost"])
        self.assertEqual(self._search_ids(
            self.hr_admin, "hr.employee", domain, fields=("hourly_cost",),
        ), {self.officer_employee.id, self.peer_employee.id})

    def test_time_off_and_allocations_follow_own_responsible_and_all_approver(self):
        own, peer, type_field, type_values = self._leaves()
        self.assertFalse(self.officer.has_group("hr_holidays.group_hr_holidays_user"))
        self._assert_denied(self.plain, own._name, "count", domain=[["name", "ilike", "Phase One"]])
        # A type that requires allocation exercises the native allocation model independently.
        Allocation = self.env["hr.leave.allocation"].with_context(tracking_disable=True)
        types = self.env[Allocation._fields[type_field].comodel_name]
        allocation_values = dict(type_values, name="Phase One Test Allocated Time Off", requires_allocation=True)
        if "code" in allocation_values:
            allocation_values["code"] = "BOTIFY_PHASE_ONE_ALLOCATION"
        leave_type = types.create(allocation_values)
        allocations = Allocation.create([
            {
                "name": "Phase One Test Allocation",
                "employee_id": employee.id,
                type_field: leave_type.id,
                "number_of_days": 4,
            }
            for employee in (self.own_employee, self.peer_employee)
        ])
        for records in (own | peer, allocations):
            own_record, peer_record = records
            domain = ["|", ["id", "=", own_record.id], ["id", "=", peer_record.id]]
            with self.subTest(model=records._name):
                self.assertEqual(self._search_ids(self.plain, records._name, domain), {own_record.id})
                self.assertEqual(self._search_ids(self.officer, records._name, domain), set())
                self.assertEqual(self._search_ids(self.manager, records._name, domain), {own_record.id})
                self.assertEqual(self._search_ids(self.time_off_officer, records._name, domain), set(records.ids))
                self._assert_same_as_missing(self.plain, records._name, peer_record.id, fields=["id"])

    def test_time_off_approver_without_responsible_role_sees_what_native_odoo_shows(self):
        # A raw SQL leave_manager_id skips the ORM grant of the Responsible role (the Odoo 20 seed gap):
        # Odoo then hides the report's time off, and the privacy layer must not widen that.
        own, peer, _type_field, _type_values = self._leaves()
        responsible = self.env.ref("hr_holidays.group_hr_holidays_responsible")
        self.manager.group_ids -= responsible
        self.assertFalse(self.manager.has_group("hr_holidays.group_hr_holidays_responsible"))
        self.assertEqual(self.own_employee.leave_manager_id, self.manager)
        domain = ["|", ["id", "=", own.id], ["id", "=", peer.id]]
        native = set(self.env["hr.leave"].with_user(self.manager).search([("id", "in", (own | peer).ids)]).ids)
        self.assertEqual(native, set())
        self.assertEqual(self._search_ids(self.manager, "hr.leave", domain), native)
        # Setting the approver through the ORM restores the role and the access on both versions.
        self.own_employee.leave_manager_id = False
        self.own_employee.leave_manager_id = self.manager
        self.assertTrue(self.manager.has_group("hr_holidays.group_hr_holidays_responsible"))
        self.assertEqual(self._search_ids(self.manager, "hr.leave", domain), {own.id})

    def test_time_off_report_and_resource_rows_are_scoped_before_search_count_and_group(self):
        own, peer, _type_field, _leave_type = self._leaves()
        own._create_resource_leave()
        peer._create_resource_leave()
        Resources = self.env["resource.calendar.leaves"]
        own_resource = Resources.search([("holiday_id", "=", own.id)])
        peer_resource = Resources.search([("holiday_id", "=", peer.id)])
        self.assertEqual((len(own_resource), len(peer_resource)), (1, 1))
        for model, own_id, peer_id in (
            ("hr.leave.report.calendar", own.id, peer.id),
            ("resource.calendar.leaves", own_resource.id, peer_resource.id),
        ):
            with self.subTest(model=model):
                domain = ["|", ["id", "=", own_id], ["id", "=", peer_id]]
                # The native employee ACL admits the peer here. Botify must narrow it.
                self.assertTrue(self.env[model].with_user(self.plain).search([("id", "=", peer_id)]))
                self.assertEqual(self._search_ids(self.plain, model, domain, limit=1, order="id desc"), {own_id})
                self.assertEqual(self._execute(self.plain, model, "count", domain=domain)["count"], 1)
                groups = self._execute(self.plain, model, "aggregate", domain=domain, groupby=["id"], aggregates=["__count"])
                self.assertEqual(len(groups["groups"]), 1)
                self.assertEqual(groups["groups"][0]["__count"], 1)
                self.assertEqual(self._search_ids(self.time_off_officer, model, domain), {own_id, peer_id})
                self._assert_same_as_missing(self.plain, model, peer_id, fields=["id"])
                self._assert_same_as_missing(self.plain, model, peer_id, op="open_record")

        matches = self._execute(self.plain, Resources._name, "name_search", name=peer_resource.name)
        self.assertNotIn(peer_resource.id, {row["id"] for row in matches})
        matches = self._execute(self.time_off_officer, Resources._name, "name_search_batch", names=[peer_resource.name])
        self.assertIn(peer_resource.id, {row["id"] for row in matches["results"][0]["matches"]})

        # A department manager can use the stock summary, even without being the assigned Time Off approver.
        self._require_models("hr.leave.report")
        department = self.env["hr.department"].create({
            "name": "Phase One Test Department", "manager_id": self.manager_employee.id,
        })
        self.peer_employee.department_id = department
        report = self.env["hr.leave.report"].search([("employee_id", "=", self.peer_employee.id)], limit=1)
        self.assertTrue(report)
        self.assertTrue(report.with_user(self.manager).has_access("read"))
        self._assert_same_as_missing(self.manager, report._name, report.id, fields=["id"])
        self.assertEqual(self._search_ids(self.time_off_officer, report._name, [["id", "=", report.id]]), {report.id})

    def test_follow_odoo_disables_privacy_scope_but_retains_model_field_and_deny_checks(self):
        own, peer, _type_field, _leave_type = self._leaves()
        model = "hr.leave.report.calendar"
        domain = [["id", "in", (own | peer).ids]]
        self.assertEqual(self._search_ids(self.plain, model, domain), {own.id})
        config.write_parameter(self.env, "botify_agent.personal_data_privacy", "follow_odoo")
        self.assertEqual(self._search_ids(self.plain, model, domain), {own.id, peer.id})
        self._assert_denied(self.plain, model, "search", domain=domain, fields=["description"])
        self._assert_denied(self.plain, "hr.version", "count", domain=[["wage", ">", 10000]])
        config.write_parameter(self.env, "botify_agent.refused_read_models", model)
        self._assert_denied(self.plain, model, "search", domain=domain)

    def test_attendance_uses_own_and_native_approver_scope(self):
        self._require_models("hr.attendance", "hr.employee")
        self.own_employee.attendance_manager_id = self.manager
        self.manager.group_ids |= self.env.ref("hr_attendance.group_hr_attendance_officer")
        all_attendance = self._user("attendance", "hr_attendance.group_hr_attendance_user")
        attendance = self.env["hr.attendance"].with_context(tracking_disable=True)
        own, peer = attendance.create([
            {"employee_id": employee.id, "check_in": "2030-03-04 08:00:00", "check_out": "2030-03-04 12:00:00"}
            for employee in (self.own_employee, self.peer_employee)
        ])
        domain = [["id", "in", (own | peer).ids]]
        self.assertEqual(self._search_ids(self.plain, attendance._name, domain), {own.id})
        self.assertEqual(self._search_ids(self.manager, attendance._name, domain), {own.id})
        self.assertEqual(self._search_ids(all_attendance, attendance._name, domain), {own.id, peer.id})
        self._assert_same_as_missing(self.plain, attendance._name, peer.id, fields=["check_in"])

        # A nested write to an admitted employee still needs to check the attendance ID.
        self._assert_same_as_missing(
            self.manager, attendance._name, peer.id, op="write", values={"check_out": "2030-03-04 13:00:00"},
        )
        self._assert_denied(
            self.officer, "hr.employee", "write", ids=self.officer_employee.ids,
            values={"attendance_ids": [[1, peer.id, {"check_out": "2030-03-04 13:00:00"}]]},
        )

    def test_expense_uses_own_and_native_approver_scope(self):
        self._require_models("hr.expense", "hr.employee")
        self.manager.group_ids |= self.env.ref("hr_expense.group_hr_expense_team_approver")
        self.own_employee.expense_manager_id = self.manager
        all_expenses = self._user("expenses", "hr_expense.group_hr_expense_user")
        expense = self.env["hr.expense"].with_context(tracking_disable=True)
        own, peer = expense.create([
            {"employee_id": employee.id, "name": name, "total_amount_currency": 231}
            for employee, name in ((self.own_employee, "Own Receipt"), (self.peer_employee, "Peer Receipt"))
        ])
        domain = [["id", "in", (own | peer).ids]]
        self.assertEqual(self._search_ids(self.plain, expense._name, domain, fields=("total_amount",)), {own.id})
        self.assertEqual(self._search_ids(self.manager, expense._name, domain), {own.id})
        self.assertEqual(self._search_ids(all_expenses, expense._name, domain), {own.id, peer.id})
        self._assert_same_as_missing(self.plain, expense._name, peer.id, fields=["total_amount"])
        self._assert_same_as_missing(self.plain, expense._name, peer.id, op="write", values={"name": "changed"})
        self.assertEqual(peer.name, "Peer Receipt")
        updated = self._execute(self.plain, expense._name, "write", ids=own.ids, values={"name": "Corrected Receipt"})
        self.assertEqual(updated["ids"], own.ids)
        self.assertEqual(own.name, "Corrected Receipt")

    def test_expense_search_supports_the_live_fields_and_keeps_peer_rows_out(self):
        self._require_models("hr.employee", "hr.expense")
        product = self.env["product.product"].create({
            "name": "Phase One Expense Product", "type": "service", "purchase_ok": True,
            "can_be_expensed": True, "standard_price": 0,
        })
        own, peer = self.env["hr.expense"].with_context(tracking_disable=True).create([
            {"employee_id": employee.id, "name": "Receipt", "product_id": product.id, "total_amount_currency": amount}
            for employee, amount in ((self.own_employee, 21), (self.peer_employee, 231))
        ])
        model = own._name
        fields = ["name", "employee_id", "date", "total_amount", "currency_id", "state", "product_id", "payment_mode"]
        own_filter = ["employee_id.name", "ilike", self.own_employee.name]
        peer_filter = ["employee_id.name", "ilike", self.peer_employee.name]
        for domain, expected in (([own_filter], own), ([peer_filter], own.browse()), (["|", own_filter, peer_filter], own)):
            with self.subTest(domain=domain):
                result = self._execute(self.plain, model, "search", domain=domain, fields=fields, order="date desc")
                self.assertEqual([row["id"] for row in result["records"]], expected.ids)
                grouped = self._execute(self.plain, model, "aggregate", domain=domain,
                                        groupby=["state"], aggregates=["total_amount:sum", "__count"])
                self.assertEqual(sum(row["__count"] for row in grouped["groups"]), len(expected))
                self.assertAlmostEqual(sum(row["total_amount:sum"] for row in grouped["groups"]),
                                       sum(expected.mapped("total_amount")))
        found = self._execute(self.plain, model, "find", filters=[own_filter], fields=fields, order="date desc")
        shortcut = self._execute(self.plain, None, "find", shortcut="my_expenses")
        self.assertEqual([row["id"] for row in found["records"]], own.ids)
        self.assertEqual([row["id"] for row in shortcut["records"]], own.ids)
        self.assertTrue(set(fields) <= shortcut["records"][0].keys())
        self.assertEqual(shortcut["records"][0]["product_id"][0], product.id)
        self._assert_same_as_missing(self.plain, model, peer.id, fields=fields)

        directory = self.env["hr.employee.public"]
        rule = {
            "name": "Deny the employee directory hop",
            "model_id": self.env["ir.model"]._get_id(directory._name),
        }
        denied = repr([("id", "!=", self.own_employee.id)])
        if "ir.access" in self.env:
            self.env["ir.access"].create(dict(rule, operation="r", domain=denied))
        else:
            self.env["ir.rule"].create(dict(rule, perm_read=True, domain_force=denied))
        fallback = ["id", "=", own.id]
        self.assertEqual(self._execute(self.plain, model, "count", domain=[fallback]),
                         {"count": 1, **LIMITED_ACCESS})
        self.assertEqual(self._execute(self.plain, model, "count",
                                       domain=["|", "!", own_filter, fallback]), {"count": 0, **LIMITED_ACCESS})

    def test_timesheet_cost_has_a_narrower_scope_than_the_hours(self):
        self._require_models("project.project", "account.analytic.line", "hr.employee")
        Lines = self.env["account.analytic.line"].with_context(tracking_disable=True)
        if "employee_id" not in Lines._fields:
            self.skipTest("Timesheets app not installed")
        self.plain.group_ids |= self.env.ref("hr_timesheet.group_hr_timesheet_user")
        self.hr_admin.group_ids |= self.env.ref("hr_timesheet.group_hr_timesheet_approver")
        approver = self._user("timesheets", "hr_timesheet.group_hr_timesheet_approver")
        plan = self.env["account.analytic.plan"].create({"name": "Phase One Timesheet Plan"})
        account = self.env["account.analytic.account"].create({"name": "Phase One Timesheet Account", "plan_id": plan.id})
        project = self.env["project.project"].create({
            "name": "Phase One Hours Project", "privacy_visibility": "employees",
            "allow_timesheets": True, "account_id": account.id,
        })
        self.own_employee.hourly_cost = 42
        self.peer_employee.hourly_cost = 730
        own, peer = Lines.create([
            {"name": "Phase One Hours", "employee_id": employee.id, "project_id": project.id, "unit_amount": 2}
            for employee in (self.own_employee, self.peer_employee)
        ])
        domain = [["id", "in", (own | peer).ids]]
        self.assertEqual(self._search_ids(self.plain, Lines._name, domain, fields=("unit_amount",)), {own.id})
        self.assertEqual(self._search_ids(approver, Lines._name, domain, fields=("unit_amount",)), {own.id, peer.id})
        self.assertEqual(self._search_ids(self.plain, Lines._name, domain, fields=("amount",)), {own.id})
        self.assertEqual(self._search_ids(approver, Lines._name, domain, fields=("amount",)), set())
        self._assert_same_as_missing(approver, Lines._name, peer.id, fields=["amount"])
        scoped = self._execute(approver, Lines._name, "count", domain=domain + [["amount", "<", -100]])
        self.assertEqual(scoped["count"], 0)
        grouped = self._execute(approver, Lines._name, "aggregate", domain=domain, groupby=["id"], aggregates=["amount:sum"])
        self.assertEqual(grouped["groups"], [])
        self.assertEqual(self._search_ids(self.hr_admin, Lines._name, domain, fields=("amount",)), {own.id, peer.id})

    def test_project_task_keeps_native_project_scope(self):
        self._require_models("project.task")
        Projects = self.env["project.project"]
        Tasks = self.env["project.task"]
        projects = Projects.create([
            {"name": "Phase One Team Project", "privacy_visibility": "employees"},
            {"name": "Phase One Restricted Project", "privacy_visibility": "invited_users"},
        ])
        own, peer = Tasks.create([
            {"name": "Phase One Team Task", "project_id": projects[0].id},
            {"name": "Phase One Restricted Task", "project_id": projects[1].id},
        ])
        project_admin = self._user("project_admin", "project.group_project_manager")
        domain = [["id", "in", (own | peer).ids]]
        self.assertEqual(self._search_ids(self.plain, Tasks._name, domain), {own.id})
        self.assertEqual(self._search_ids(project_admin, Tasks._name, domain), {own.id, peer.id})
        self._assert_same_as_missing(self.plain, Tasks._name, peer.id, fields=["name"])

    def test_relation_never_serializes_a_target_that_fails_its_record_rule(self):
        Partners = self.env["res.partner"]
        hidden = Partners.create({"name": "Phase One Private Relation Canary", "is_company": True})
        visible = Partners.create({"name": "Phase One Public Child", "parent_id": hidden.id})
        rule = {
            "name": "Phase One Test Native Partner Restriction",
            "model_id": self.env["ir.model"]._get_id(Partners._name),
        }
        if "ir.access" in self.env:
            self.env["ir.access"].create(dict(rule, operation="r", domain=repr([("id", "!=", hidden.id)])))
        else:
            self.env["ir.rule"].create(dict(rule, perm_read=True, domain_force=repr([("id", "!=", hidden.id)])))
        self.assertFalse(hidden.with_user(self.plain).has_access("read"))
        # Stock many2one rendering reads the target label under sudo.
        native = visible.with_user(self.plain).read(["parent_id"])[0]["parent_id"]
        self.assertEqual(native[0], hidden.id)
        self.assertIn(hidden.name, native[1])
        try:
            answer = self._execute(self.plain, Partners._name, "read", ids=visible.ids, fields=["parent_id"])
        except OperationError as refusal:
            self.assertEqual(refusal.code, "ACCESS_DENIED")
            self.assertNotIn(hidden.name, str(refusal))
        else:
            self.assertFalse(answer["records"][0]["parent_id"])
            self.assertNotIn(hidden.name, repr(answer))
