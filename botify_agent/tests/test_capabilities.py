from odoo import Command
from odoo.tests import TransactionCase, new_test_user, tagged

from ..services import capabilities, config, executor
from ..services.errors import OperationError


@tagged("post_install", "-at_install")
class TestBotifyCapabilities(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.employee = new_test_user(
            cls.env, login="botify_manifest_employee", groups="base.group_user,botify_agent.group_botify_user",
        )
        cls.form = cls.env["ir.ui.view"].create({
            "name": "botify_test_manifest_form",
            "model": "res.partner",
            "priority": 100,
            "arch": """
                <form>
                    <header>
                        <button type="object" name="action_archive" string="Visible archive"/>
                        <button type="object" name="action_unarchive" string="Settings unarchive"
                                groups="base.group_system"/>
                    </header>
                    <sheet><field name="name"/></sheet>
                </form>
            """,
        })
        action = cls.env["ir.actions.act_window"].create({
            "name": "Botify Manifest Contacts",
            "res_model": "res.partner",
            "view_mode": "form",
            "view_id": cls.form.id,
        })
        cls.root_menu = cls.env["ir.ui.menu"].create({
            "name": "Botify Manifest Contacts",
            "sequence": 0,
            "action": f"ir.actions.act_window,{action.id}",
        })
        hidden = action.copy({
            "name": "Botify Hidden Manifest App",
            "group_ids": [Command.set(cls.env.ref("base.group_system").ids)],
        })
        cls.env["ir.ui.menu"].create({
            "name": "Botify Hidden Manifest App",
            "action": f"ir.actions.act_window,{hidden.id}",
        })

    def _manifest(self, user, **options):
        env = self.env(user=user.id, su=False)
        operation = executor.check_operation(env, {"op": "capabilities", **options})
        return executor.run(env, operation)[0]

    def test_menu_app_and_matching_window_model_are_bounded_and_deterministic(self):
        first = self._manifest(self.employee)
        second = self._manifest(self.employee)
        self.assertEqual(first, second)
        self.assertEqual(first["protocol"], 3)
        self.assertTrue(first["addonVersion"])
        self.assertEqual(first["privacyMode"], "strict")
        self.assertRegex(first["hash"], r"^[a-f0-9]{64}$")
        self.assertLessEqual(len(capabilities._json(first)), capabilities.MANIFEST_BYTES)
        self.assertEqual(first["employee"]["name"], self.employee.name)
        self.assertNotIn("Botify Hidden Manifest App", [app["name"] for app in first["apps"]])
        shown = next(app for app in first["apps"] if app["name"] == "Botify Manifest Contacts")
        details = self._manifest(self.employee, app=shown["id"])
        self.assertEqual([model["model"] for model in details["details"]], ["res.partner"])

    def test_fields_and_buttons_use_model_field_and_view_permissions(self):
        detail = self._manifest(self.employee, model="res.partner")["details"][0]
        self.assertIn("R", detail["rights"])
        name = detail["fields"]["name"]
        self.assertEqual(name["type"], "char")
        self.assertIn("ilike", name["operators"])
        index = self._manifest(self.employee)
        app = next(app["id"] for app in index["apps"] if app["name"] == "Botify Manifest Contacts")
        view = self._manifest(self.employee, app=app, model="res.partner")["details"][0]
        self.assertIn("action_archive", [b["name"] for b in view["buttons"]])
        self.assertNotIn("action_unarchive", [b["name"] for b in view["buttons"]])
        config.write_parameter(self.env, "botify_agent.refused_read_fields", "res.partner.name")
        update = self._manifest(self.employee, model="res.partner")["details"][0]
        self.assertNotIn("name", update["fields"])
        with self.assertRaises(OperationError) as error:
            self._manifest(self.employee, model="ir.config_parameter")
        self.assertEqual(error.exception.code, "ACCESS_DENIED")

    def test_personal_models_and_employee_context_respect_role_and_scope(self):
        if "hr.employee.public" not in self.env:
            self.skipTest("HR is optional")
        officer = new_test_user(
            self.env, login="botify_manifest_officer",
            groups="base.group_user,botify_agent.group_botify_user,hr.group_hr_user",
        )
        team = self.env["hr.department"].create({"name": "Manifest Department"})
        manager = self.env["hr.employee"].create({"name": "Manifest Manager"})
        employee = self.env["hr.employee"].create({
            "name": "Manifest Employee",
            "user_id": self.employee.id,
            "parent_id": manager.id,
            "department_id": team.id,
        })
        plain = self._manifest(self.employee)
        hr = self._manifest(officer)
        self.assertEqual(plain["employee"]["id"], employee.id)
        self.assertEqual(plain["employee"]["manager"], {"id": manager.id, "name": manager.name})
        self.assertEqual(plain["employee"]["department"], {"id": team.id, "name": team.name})
        models = {row["model"] for row in plain["models"]}
        self.assertIn("hr.employee.public", models)
        self.assertNotIn("hr.employee", models)
        self.assertNotIn("hr.version", models)
        self.assertIn("hr.employee", {row["model"] for row in hr["models"]})
        self.assertNotEqual(plain["hash"], hr["hash"])
        fields = self._manifest(officer, model="hr.version")["details"][0]["fields"]
        self.assertNotIn("wage", fields)

    def test_hash_includes_current_privacy_and_model_denials(self):
        first = self._manifest(self.employee)
        config.write_parameter(self.env, "botify_agent.personal_data_privacy", "follow_odoo")
        updated = self._manifest(self.employee)
        self.assertEqual(updated["privacyMode"], "follow_odoo")
        self.assertNotEqual(first["hash"], updated["hash"])
        config.write_parameter(self.env, "botify_agent.refused_read_models", "res.partner")
        filtered = self._manifest(self.employee)
        self.assertNotEqual(updated["hash"], filtered["hash"])
        self.assertNotIn("res.partner", [row["model"] for row in filtered["models"]])

    def test_time_off_fields_and_shortcuts_follow_the_installed_models_and_native_access(self):
        if "hr.leave" not in self.env:
            self.skipTest("Time Off is optional")
        group = self.env.ref("hr_holidays.group_hr_holidays_employee", raise_if_not_found=False)
        if group:
            self.employee.group_ids -= group
            self.assertFalse(self.env["hr.leave"].with_user(self.employee).has_access("read"))
            denied = self._manifest(self.employee)
            self.assertFalse(denied["timeOffRequestAccess"])
            self.assertNotIn("hr.leave", {row["model"] for row in denied["models"]})
            self.assertFalse({"my_time_off", "my_allocations", "team_time_off"} & set(denied["shortcuts"]))
            env = self.env(user=self.employee.id, su=False)
            with self.assertRaises(OperationError) as refusal:
                executor.run(env, {"op": "find", "shortcut": "my_time_off"})
            self.assertEqual(refusal.exception.code, "ACCESS_DENIED")
            self.employee.group_ids |= group

        allowed = self._manifest(self.employee)
        self.assertTrue(allowed["timeOffRequestAccess"])
        self.assertTrue({"my_time_off", "my_allocations"} <= set(allowed["shortcuts"]))
        index = {row["model"]: row["fields"] for row in allowed["models"]}
        for model, dates in (
            ("hr.leave", {"request_date_from", "request_date_to"}),
            ("hr.leave.allocation", {"date_from", "date_to"}),
        ):
            with self.subTest(model=model):
                definitions = self.env[model]._fields
                native, other, relation = (
                    ("work_entry_type_id", "holiday_status_id", "hr.work.entry.type")
                    if "work_entry_type_id" in definitions
                    else ("holiday_status_id", "work_entry_type_id", "hr.leave.type")
                )
                self.assertTrue({native, "number_of_days", *dates} <= set(index[model]))
                self.assertNotIn(other, index[model])
                detail = self._manifest(self.employee, model=model)["details"][0]
                self.assertEqual(detail["fields"][native]["relation"], relation)
                self.assertNotIn(other, detail["fields"])
