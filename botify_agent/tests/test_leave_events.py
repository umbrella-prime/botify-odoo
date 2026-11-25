import json
from datetime import date, timedelta
from unittest import SkipTest

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, new_test_user, tagged

from ..services.config import read_parameter, write_parameter
from ..services.find import time_off_type_field


@tagged("post_install", "-at_install")
class TestLeaveEvents(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if "hr.leave" not in cls.env:
            raise SkipTest("Time Off is not installed")
        leave = cls.env["hr.leave"]
        cls.type_field = time_off_type_field(leave)
        cls.type_model = leave._fields[cls.type_field].comodel_name
        for key, value in {
            "enabled": "True",
            "api_url": "https://api.botify.test",
            "installation_key": "wsk_module_test_installation",
            "secret": "wss_module-test-secret-000000000000000000",
        }.items():
            write_parameter(cls.env, "botify_agent." + key, value)
        write_parameter(cls.env, "botify_agent.leave_notifications_enabled", False)
        write_parameter(cls.env, "botify_agent.excluded_leave_type_ids", False)
        cls.manager = new_test_user(
            cls.env, login="leave_manager", name="Mitchell Manager",
            groups="base.group_user,hr_holidays.group_hr_holidays_manager",
        )
        cls.user = new_test_user(
            cls.env, login="leave_employee", groups="base.group_user,botify_agent.group_botify_user"
        )
        cls.employee = cls.env["hr.employee"].create({"name": "Leave Employee", "user_id": cls.user.id})
        cls.leave_type = cls.make_type("Paid Time Off", "BOTIFY_PTO")

    @classmethod
    def make_type(cls, name, code):
        model = cls.env[cls.type_model]
        values = {"name": name, "requires_allocation": False, "leave_validation_type": "hr", "request_unit": "day"}
        if "unit_of_measure" in model._fields:
            values.update(code=code, unit_of_measure="day", count_as="absence")
        return model.create(values)

    def request(self, employee=None, leave_type=None, start=date(2026, 10, 5)):
        leave_type = leave_type or self.leave_type
        model = self.env["hr.leave"]
        if self.type_field == "work_entry_type_id" and leave_type.leave_validation_type != "no_validation":
            # A Time Off officer otherwise auto-approves requests for another employee.
            model = model.with_context(leave_fast_create=True)
        return model.create({
            "employee_id": (employee or self.employee).id,
            self.type_field: leave_type.id,
            "request_date_from": start,
            "request_date_to": start + timedelta(days=3),
        })

    def events(self):
        return self.env["botify.agent.event"].sudo().search([], order="id")

    def save_settings(self, *, excluded=None, **values):
        settings = self.env["res.config.settings"].create(values)
        if excluded is not None:
            options = settings.botify_leave_type_options.filtered(lambda option: option.type_id in excluded.ids)
            settings.botify_excluded_leave_type_options = [Command.set(options.ids)]
        settings.set_values()

    def test_default_off_applies_to_approvals_and_refusals(self):
        leave = self.request()
        leave.with_user(self.manager).action_approve()
        refused = self.request(start=date(2026, 10, 12))
        refused.with_user(self.manager).action_refuse()

        self.assertEqual((leave.state, refused.state), ("validate", "refuse"))
        self.assertFalse(self.events())

    def test_a_leave_manager_cannot_enable_notifications(self):
        with self.assertRaises(AccessError):
            self.env["res.config.settings"].with_user(self.manager).create({
                "botify_leave_notifications_enabled": True,
            })

    def test_approval_tells_the_employee_with_the_leave_facts(self):
        self.save_settings(botify_leave_notifications_enabled=True)
        leave = self.request()
        leave.with_user(self.manager).action_approve()
        [event] = self.events()
        self.assertEqual(event.botify_leave_type_id, self.leave_type.id)
        self.assertEqual((event.kind, event.user_id, event.company_id), ("leave.approved", self.user, leave.company_id))
        self.assertEqual(
            json.loads(event.facts_json),
            {"leaveId": leave.id, "leaveType": "Paid Time Off", "dateFrom": "2026-10-05", "dateTo": "2026-10-08",
             "days": leave.number_of_days, "decidedBy": "Mitchell Manager"},
        )

    def test_register_hook_twice_emits_one_event_per_approval(self):
        self.save_settings(botify_leave_notifications_enabled=True)
        event_model = self.env["botify.agent.event"]
        event_model._register_hook()
        event_model._register_hook()

        self.request().with_user(self.manager).action_approve()

        self.assertEqual(self.events().mapped("kind"), ["leave.approved"])

    def test_long_names_are_cut_to_fit_not_dropped(self):
        self.save_settings(botify_leave_notifications_enabled=True)
        long_type = self.make_type("🌴" * 150, "BOTIFY_LONG")
        self.request(leave_type=long_type).with_user(self.manager).action_approve()
        [event] = self.events()
        self.assertEqual(json.loads(event.facts_json)["leaveType"], "🌴" * 100)

    def test_refusal_tells_the_employee(self):
        self.save_settings(botify_leave_notifications_enabled=True)
        leave = self.request()
        leave.with_user(self.manager).action_refuse()
        self.assertEqual(self.events().mapped("kind"), ["leave.refused"])

    def test_no_event_without_approval_a_user_or_the_botify_group(self):
        self.save_settings(botify_leave_notifications_enabled=True)
        automatic = self.leave_type.copy({"name": "Automatic", "leave_validation_type": "no_validation"})
        self.request(leave_type=automatic)
        no_user = self.env["hr.employee"].create({"name": "No User"})
        self.request(employee=no_user).with_user(self.manager).action_approve()
        outsider = new_test_user(self.env, login="no_botify_employee", groups="base.group_user")
        other = self.env["hr.employee"].create({"name": "Outsider", "user_id": outsider.id})
        self.request(employee=other).with_user(self.manager).action_approve()
        self.assertFalse(self.events())

    def test_a_broken_integration_never_blocks_the_approval(self):
        self.save_settings(botify_leave_notifications_enabled=True)
        write_parameter(self.env, "botify_agent.secret", "")
        leave = self.request()
        leave.with_user(self.manager).action_approve()
        self.assertEqual(leave.state, "validate")
        self.assertFalse(self.events())

    def test_exclusions_persist_and_skip_both_approvals_and_refusals(self):
        excluded = self.leave_type.copy({"name": "Sick Leave"})
        self.save_settings(
            botify_leave_notifications_enabled=True,
            excluded=excluded,
        )
        settings = self.env["res.config.settings"].create({})
        self.assertTrue(settings.botify_time_off_installed)
        self.assertEqual(settings.botify_excluded_leave_type_options.mapped("type_id"), excluded.ids)
        self.assertEqual(read_parameter(self.env, "botify_agent.excluded_leave_type_ids"), str(excluded.id))

        self.request(leave_type=excluded).with_user(self.manager).action_approve()
        self.request(leave_type=excluded, start=date(2026, 10, 12)).with_user(self.manager).action_refuse()
        allowed = self.request(start=date(2026, 10, 19))
        allowed.with_user(self.manager).action_approve()

        [event] = self.events()
        self.assertEqual(json.loads(event.facts_json)["leaveId"], allowed.id)

    def test_turning_off_notifications_prevents_delivery_of_queued_events(self):
        self.save_settings(botify_leave_notifications_enabled=True)
        self.request().with_user(self.manager).action_approve()
        self.assertTrue(self.events())

        self.save_settings(botify_leave_notifications_enabled=False)

        self.assertFalse(self.env["botify.agent.event"]._lease(self.user))

    def test_opting_in_does_not_deliver_notifications_queued_before_the_upgrade(self):
        self.env["botify.agent.event"].create({
            "user_id": self.user.id,
            "kind": "leave.approved",
            "facts_json": '{"leaveId":1,"leaveType":"Sick Leave","dateFrom":"2026-10-05",'
                          '"dateTo":"2026-10-08","days":4}',
        })
        self.save_settings(botify_leave_notifications_enabled=True)

        self.assertFalse(self.env["botify.agent.event"]._lease(self.user))

    def test_excluding_a_type_also_blocks_notifications_already_queued(self):
        self.save_settings(botify_leave_notifications_enabled=True)
        self.request().with_user(self.manager).action_approve()
        allowed_type = self.leave_type.copy({"name": "Personal Leave"})
        allowed = self.request(leave_type=allowed_type, start=date(2026, 10, 12))
        allowed.with_user(self.manager).action_approve()
        self.assertEqual(len(self.events()), 2)

        self.save_settings(excluded=self.leave_type)

        [event] = self.env["botify.agent.event"]._lease(self.user)
        self.assertEqual(json.loads(event.facts_json)["leaveId"], allowed.id)
