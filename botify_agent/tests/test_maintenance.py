from datetime import timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import SQL

from ..services import config


@tagged("post_install", "-at_install")
class TestBotifyMaintenance(TransactionCase):
    def _apps_cleanup_migration(self):
        path = Path(__file__).resolve().parents[1] / "migrations" / "3.0.1" / "end-migrate.py"
        spec = spec_from_file_location("botify_agent_apps_cleanup", path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        return migration

    def test_apps_cleanup_removes_stale_module_and_its_metadata(self):
        module = self.env["ir.module.module"].create({"name": "botify_agent_hr", "state": "uninstalled"})
        dependency = self.env["ir.module.module.dependency"].create({"name": "botify_agent", "module_id": module.id})
        xmlid = self.env["ir.model.data"].search([("module", "=", "base"), ("name", "=", "module_botify_agent_hr")])
        self.assertTrue(xmlid.exists())

        self._apps_cleanup_migration().migrate(self.env.cr, "19.0.3.0.0")

        self.assertFalse(module.exists())
        self.assertFalse(dependency.exists())
        self.assertFalse(xmlid.exists())

    def test_apps_cleanup_keeps_installed_modules_and_available_addons(self):
        module = self.env["ir.module.module"].create({"name": "botify_agent_hr", "state": "installed"})
        migration = self._apps_cleanup_migration()
        migration.migrate(self.env.cr, "19.0.3.0.0")
        self.assertTrue(module.exists())

        module.state = "uninstalled"
        with patch.object(migration, "get_module_path", return_value="/extra-addons/botify_agent_hr"):
            migration.migrate(self.env.cr, "19.0.3.0.0")
        self.assertTrue(module.exists())

    def test_neutralization_removes_credentials_and_disables_the_integration(self):
        for key, value in {
            "enabled": "True",
            "api_url": "https://api.botify.test",
            "installation_key": "wsk_live_test",
            "secret": "wss_live_test",
        }.items():
            config.write_parameter(self.env, "botify_agent." + key, value)
        sql = Path(__file__).resolve().parents[1] / "data" / "neutralize.sql"
        self.assertTrue(config.configured(self.env))
        self.env.flush_all()
        self.env.cr.execute(sql.read_text(encoding="utf-8"))
        if invalidate := getattr(self.env.transaction, "invalidate_ormcache", None):
            invalidate("stable")
        else:
            self.env.registry.clear_cache("stable")
        self.env.invalidate_all()
        self.assertFalse(config.configured(self.env))
        settings = config.get_settings(self.env)
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["installation_key"], "")
        self.assertEqual(config.get_secret(self.env), "")
        self.assertEqual(settings["api_url"], "https://api.botify.test")

    def test_deleting_an_employee_preserves_their_operation_log(self):
        employee = new_test_user(self.env, login="botify_audit_employee", groups="base.group_user")
        operation = self.env["botify.agent.operation"].create({
            "call_id": "audit-after-user-deletion", "user_id": employee.id,
        })
        self.assertEqual(operation.user_id, employee)

        employee.unlink()

        self.assertTrue(operation.exists())
        operation.invalidate_recordset(["user_id"])
        self.assertFalse(operation.user_id)
        self.assertEqual(operation.call_id, "audit-after-user-deletion")

    def _set_age(self, records, age):
        records.flush_recordset()
        self.env.cr.execute(SQL(
            "UPDATE %s SET create_date = %s WHERE id IN %s",
            SQL.identifier(records._table), fields.Datetime.now() - age, tuple(records.ids),
        ))
        records.invalidate_recordset(["create_date"])

    def test_operation_cleanup_scrubs_answers_after_an_hour_and_deletes_in_batches(self):
        config.write_parameter(self.env, "botify_agent.retention_days", False)
        operations = self.env["botify.agent.operation"]
        expired = operations.create([
            {"call_id": f"expired-{index}", "user_id": self.env.uid} for index in range(1001)
        ])
        self._set_age(expired, timedelta(days=91))
        self._set_age(expired[-1], timedelta(days=100))
        young, retained = operations.create([
            {"call_id": call_id, "user_id": self.env.uid, "result_json": '{"ok":true}',
             "record_ids": "42", "error_message": "Audit evidence"}
            for call_id in ("new-answer", "expired-answer")
        ])
        self._set_age(young, timedelta(minutes=59))
        self._set_age(retained, timedelta(minutes=61))

        operations._gc()

        self.assertEqual(len(expired.exists()), 1)
        self.assertFalse(expired[-1].exists())
        self.assertEqual(young.answer(), {"ok": True})
        self.assertFalse(retained.result_json)
        self.assertEqual((retained.record_ids, retained.error_message), ("42", "Audit evidence"))

        operations._gc()
        self.assertFalse(expired.exists())
        self.assertTrue(retained.exists())

    def test_event_cleanup_expires_undelivered_events_and_deletes_in_batches(self):
        config.write_parameter(self.env, "botify_agent.retention_days", False)
        events = self.env["botify.agent.event"]
        values = {"user_id": self.env.uid, "kind": "test.happened", "facts_json": "{}"}
        old = events.create([
            dict(values, event_id=f"old-event-{index}", state="delivered") for index in range(1001)
        ])
        self._set_age(old, timedelta(days=91))
        self._set_age(old[-1], timedelta(days=100))
        waiting = events.create(dict(values, state="delivering", lease_until=fields.Datetime.now()))
        self._set_age(waiting, timedelta(days=8))
        young = events.create(values)

        events._gc()

        self.assertEqual(len(old.exists()), 1)
        self.assertFalse(old[-1].exists())
        self.assertEqual(waiting.state, "expired")
        self.assertFalse(waiting.lease_until)
        self.assertEqual(young.state, "pending")

        events._gc()
        self.assertFalse(old.exists())
        self.assertTrue(waiting.exists())

    def test_delivery_skips_events_from_removed_producers(self):
        events = self.env["botify.agent.event"]
        events.create({"user_id": self.env.uid, "kind": "removed.producer", "facts_json": "{}"})

        self.assertFalse(events._lease(self.env.user))
