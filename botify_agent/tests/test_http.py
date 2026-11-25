import hashlib
import hmac
import json
import os
import re
import time
from datetime import timedelta
from unittest.mock import patch

from odoo import SUPERUSER_ID, api, fields
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, JsonRpcException, new_test_user, tagged
from odoo.tools import mute_logger

from ..services import classify, config, executor, grant

SECRET = "wss_module-test-secret-000000000000000000"
KEY = "wsk_module_test_installation"
MODULE = os.path.dirname(os.path.dirname(__file__))


@tagged("post_install", "-at_install")
class TestBotifyAgentHttp(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for key, value in {
            "enabled": "True",
            "api_url": "https://api.botify.test",
            "installation_key": KEY,
            "secret": SECRET,
            "allow_low_write": "True",
            "allow_high_write": "True",
        }.items():
            config.write_parameter(cls.env, "botify_agent." + key, value)
        cls.employee = new_test_user(
            cls.env, login="botify_employee", password="botify_employee_pw",
            groups="base.group_user,base.group_partner_manager,botify_agent.group_botify_user",
        )
        cls.outsider = new_test_user(cls.env, login="no_botify", password="no_botify_pw", groups="base.group_user")
        cls.colleague = new_test_user(
            cls.env, login="botify_colleague", groups="base.group_user,botify_agent.group_botify_user"
        )
        cls.second_employee = new_test_user(
            cls.env, login="botify_employee_2",
            groups="base.group_user,base.group_partner_manager,botify_agent.group_botify_user",
        )
        cls.partner = cls.env["res.partner"].create({"name": "Azure Interior", "phone": "111"})

    def setUp(self):
        super().setUp()
        self.session = self.authenticate("botify_employee", "botify_employee_pw")

    # -- helpers --------------------------------------------------------------

    def csrf(self):
        if callable(csrf_token := getattr(self, "csrf_token", None)):
            return csrf_token()
        secret = config.read_parameter(self.env, "database.secret")
        max_ts = int(time.time() + 3600)
        message = ("%s%s" % (self.session.sid[:42], max_ts)).encode()
        return "%so%s" % (hmac.new(secret.encode("ascii"), message, hashlib.sha1).hexdigest(), max_ts)

    def post(self, route, params=None, headers=None, csrf=True):
        sent = {"X-Botify-CSRF": self.csrf()} if csrf else {}
        sent.update(headers or {})
        return self.make_jsonrpc_request(route, params or {}, headers=sent)

    def grant(self, op, risk_class="low_write", **overrides):
        now = int(time.time())
        payload = {
            "v": 1,
            "iss": KEY,
            "callId": "call-%s:exec" % os.urandom(6).hex(),
            "runId": "run-1",
            "sessionId": "session-1",
            "db": self.env.cr.dbname,
            "uid": self.employee.id,
            "sb": grant.session_binding(SECRET, self.session.sid),
            "riskClass": risk_class,
            "approvalId": None if risk_class == "read" else "appr-1",
            "op": op,
            "iat": now,
            "exp": now + 120,
        }
        payload.update(overrides)
        return payload, grant.sign_grant(SECRET, payload)

    def execute(self, token, context=None):
        return self.post("/botify_agent/v1/execute", {"grant_token": token, **({"context": context} if context else {})})

    def audit(self, call_id):
        """Read from a fresh cursor, after the request finished."""
        with self.registry.cursor() as cr:
            rows = api.Environment(cr, SUPERUSER_ID, {})["botify.agent.operation"].search([("call_id", "=", call_id)])
            return [(row.status, row.error_code, row.user_id.id) for row in rows]

    def write_op(self, values, ids=None):
        return {"op": "write", "model": "res.partner", "ids": ids or [self.partner.id], "values": values}

    def read_operation(self, op, model="res.partner", **arguments):
        _payload, token = self.grant(dict(op=op, model=model, **arguments), risk_class="read")
        return self.execute(token)

    # -- CSRF -----------------------------------------------------------------

    def test_every_route_requires_the_csrf_token(self):
        for route in (
            "/botify_agent/v1/identity",
            "/botify_agent/v1/execute",
            "/botify_agent/v1/events/pending",
            "/botify_agent/v1/events/ack",
        ):
            answer = self.post(route, {}, csrf=False)
            self.assertEqual(answer["error"]["code"], "CSRF_FAILED", route)
            answer = self.post(route, {}, headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(answer["error"]["code"], "CSRF_FAILED", route)
            answer = self.post(route, {}, headers={"X-Botify-CSRF": "0" * 40 + "o1"})
            self.assertEqual(answer["error"]["code"], "CSRF_FAILED", route)

    def test_csrf_route_hands_out_a_working_token_to_same_origin_only(self):
        route = "/botify_agent/v1/csrf"
        answer = self.post(route, csrf=False, headers={"Sec-Fetch-Site": "same-origin"})
        self.assertTrue(answer["ok"], answer)
        token = answer["result"]["csrfToken"]
        self.assertTrue(token)
        fresh = self.post(
            "/botify_agent/v1/identity", csrf=False, headers={"X-Botify-CSRF": token, "Sec-Fetch-Site": "same-origin"}
        )
        self.assertTrue(fresh["ok"], fresh)
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {}):
            answer = self.post(route, csrf=False, headers=headers)
            self.assertEqual(answer["error"]["code"], "CSRF_FAILED", headers)

    # -- identity -------------------------------------------------------------

    def test_identity_asserts_the_session_user_bound_to_this_session(self):
        answer = self.post("/botify_agent/v1/identity")
        self.assertTrue(answer["ok"], answer)
        token = answer["result"]["assertion"]
        payload = grant._open(grant.derive_key(SECRET, "identity"), "o1", token, 4096)
        self.assertEqual(payload["uid"], self.employee.id)
        self.assertEqual(payload["iss"], KEY)
        self.assertEqual(payload["db"], self.env.cr.dbname)
        self.assertEqual(payload["sb"], grant.session_binding(SECRET, self.session.sid))
        self.assertEqual(payload["protocol"], 3)
        self.assertRegex(payload["manifest_hash"], r"^[a-f0-9]{64}$")
        self.assertLessEqual(payload["exp"] - payload["iat"], 120)
        self.assertEqual(answer["result"]["siteKey"], KEY)
        self.assertEqual(
            answer["result"]["frameUrl"], f"https://api.botify.test/widget/v1/frame?site={KEY}"
        )

    def test_identity_refused_outside_the_group_or_when_disabled(self):
        self.session = self.authenticate("no_botify", "no_botify_pw")
        self.assertEqual(self.post("/botify_agent/v1/identity")["error"]["code"], "ACCESS_DENIED")
        config.write_parameter(self.env, "botify_agent.enabled", "False")
        self.session = self.authenticate("botify_employee", "botify_employee_pw")
        self.assertEqual(self.post("/botify_agent/v1/identity")["error"]["code"], "INTEGRATION_DISABLED")

    # -- grant binding --------------------------------------------------------

    def test_grants_bound_to_another_session_user_or_database_are_refused(self):
        op = self.write_op({"phone": "999"})
        cases = {
            "uid": {"uid": self.outsider.id},
            "db": {"db": "another_db"},
            "sb": {"sb": "B" * 43},
            "iss": {"iss": "wsk_another_installation"},
            "expired": {"iat": int(time.time()) - 300, "exp": int(time.time()) - 10},
        }
        for name, overrides in cases.items():
            payload, token = self.grant(op, **overrides)
            answer = self.execute(token)
            self.assertEqual(answer["error"]["code"], "GRANT_REJECTED", name)
            self.assertEqual(self.audit(payload["callId"]), [], name)
        _payload, token = self.grant(op)
        head, body, mac = token.split(".")
        tampered = grant._b64(json.dumps(dict(_payload, op=self.write_op({"phone": "000"}))).encode())
        self.assertEqual(self.execute("%s.%s.%s" % (head, tampered, mac))["error"]["code"], "GRANT_REJECTED")
        self.assertEqual(self.partner.phone, "111")

    # -- execution and audit ---------------------------------------------------

    def test_writes_require_an_approval_id_even_when_their_class_is_enabled(self):

        rules = self.env["botify.agent.field_rule"]
        rules.create([
            {"model": "res.partner", "field": "ref", "risk_class": "financial"},
            {"model": "res.partner", "field": "email", "risk_class": "external_communication"},
        ])
        for risk_class, field, value in (
            ("low_write", "phone", "222"),
            ("high_write", "parent_id", self.env["res.partner"].create({"name": "Parent"}).id),
            ("financial", "ref", "Billing reference"),
            ("external_communication", "email", "botify@example.test"),
            ("destructive", "active", False),
        ):
            with self.subTest(risk_class=risk_class):
                config.write_parameter(self.env, "botify_agent.allow_" + risk_class, "True")
                # The opt-out never covers higher-risk grants.
                if risk_class == "high_write":
                    config.write_parameter(self.env, "botify_agent.allow_simple_changes_without_confirmation", "True")
                op = self.write_op({field: value})
                before = self.partner[field]
                payload, token = self.grant(op, risk_class=risk_class, approvalId=None)
                self.assertEqual(self.execute(token)["error"]["code"], "APPROVAL_REQUIRED")
                self.assertEqual(self.audit(payload["callId"]), [("failed", "APPROVAL_REQUIRED", self.employee.id)])
                self.partner.invalidate_recordset()
                self.assertEqual(self.partner[field], before)
                approved, token = self.grant(op, risk_class=risk_class)
                self.assertTrue(self.execute(token)["ok"])
                row = self.env["botify.agent.operation"].claimed(approved["callId"])
                self.assertEqual(row.approval_id, approved["approvalId"])
                self.partner.invalidate_recordset()
                actual = self.partner[field]
                self.assertEqual(actual.id if field == "parent_id" else actual, value)

    def test_only_an_explicit_opt_in_allows_low_writes_without_approval(self):

        key = "botify_agent.allow_simple_changes_without_confirmation"
        op = self.write_op({"phone": "222"})
        for setting in (False, "False", "True"):
            with self.subTest(setting=setting):
                config.write_parameter(self.env, key, setting)
                _, token = self.grant({"op": "context"}, risk_class="read")
                self.assertIs(self.execute(token)["result"]["allowSimpleChangesWithoutConfirmation"], setting == "True")
                payload, token = self.grant(op, approvalId=None)
                if setting == "True":
                    self.assertTrue(self.execute(token)["ok"])
                    self.assertEqual(self.audit(payload["callId"]), [("done", False, self.employee.id)])
                else:
                    self.assertEqual(self.execute(token)["error"]["code"], "APPROVAL_REQUIRED")

    def test_an_empty_approval_id_is_refused(self):
        for approval_id in ("", "  "):
            with self.subTest(approval_id=approval_id):
                _, token = self.grant(self.write_op({"phone": "222"}), approvalId=approval_id)
                self.assertEqual(self.execute(token)["error"]["code"], "APPROVAL_REQUIRED")

    def test_read_replay_does_not_return_the_previous_payload(self):
        payload, token = self.grant(
            {"op": "read", "model": "res.partner", "ids": self.partner.ids, "fields": ["phone"]},
            risk_class="read",
        )
        self.assertEqual(self.execute(token)["result"]["records"][0]["phone"], "111")
        config.write_parameter(self.env, "botify_agent.refused_read_fields", "res.partner.phone")
        replay = self.execute(token)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["error"]["code"], "CONFLICT")
        self.assertNotIn("result", replay)
        self.assertEqual(self.audit(payload["callId"]), [("done", False, self.employee.id)])
        self.assertEqual(
            self.read_operation("read", ids=self.partner.ids, fields=["phone"])["error"]["code"], "ACCESS_DENIED",
        )

    def test_success_is_audited_and_a_replay_returns_the_stored_result(self):
        payload, token = self.grant(self.write_op({"phone": "222"}))
        answer = self.execute(token)
        self.assertEqual(answer, {"ok": True, "result": {"ids": [self.partner.id], "updated": 1}})
        self.assertEqual(self.audit(payload["callId"]), [("done", False, self.employee.id)])
        self.partner.invalidate_recordset()
        self.assertEqual(self.partner.phone, "222")
        # Attributed to the employee in Odoo's own tracking too.
        self.assertEqual(self.partner.write_uid, self.employee)

        self.partner.phone = "333"
        replay = self.execute(token)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], answer["result"])
        self.partner.invalidate_recordset()
        self.assertEqual(self.partner.phone, "333", "a replay never executes again")
        self.assertEqual(len(self.audit(payload["callId"])), 1)

    def test_a_failed_operation_rolls_back_but_its_audit_stays(self):
        values = {"name": "Renamed", "parent_id": self.partner.id}  # recursive hierarchy: ValidationError
        payload, token = self.grant(self.write_op(values), risk_class="high_write")
        answer = self.execute(token)
        self.assertEqual(answer["error"]["code"], "VALIDATION_FAILED")
        self.partner.invalidate_recordset()
        self.assertEqual(self.partner.name, "Azure Interior")
        self.assertEqual(self.audit(payload["callId"]), [("failed", "VALIDATION_FAILED", self.employee.id)])

    @mute_logger("odoo.addons.botify_agent.services.errors")
    def test_an_unexpected_failure_is_audited_without_leaking_internals(self):
        payload, token = self.grant(self.write_op({"phone": "444"}))
        with patch.object(executor, "run", side_effect=RuntimeError("secret internals")):
            answer = self.execute(token)
        self.assertEqual(answer["error"]["code"], "INTERNAL")
        self.assertNotIn("secret internals", answer["error"]["message"])
        self.assertEqual(self.audit(payload["callId"]), [("failed", "INTERNAL", self.employee.id)])

    def test_odoo_permissions_decide_not_the_grant(self):
        company = self.env["res.company"].create({"name": "Other Co"})
        partner = self.env["res.partner"].create({"name": "Hidden", "company_id": company.id})
        payload, token = self.grant(self.write_op({"phone": "1"}, ids=[partner.id]))
        answer = self.execute(token)
        self.assertEqual(answer["error"]["code"], "ACCESS_DENIED")
        self.assertEqual(self.audit(payload["callId"]), [("failed", "ACCESS_DENIED", self.employee.id)])

    def test_companies_outside_the_employee_are_refused(self):
        other = self.env["res.company"].create({"name": "Other Co"})
        _payload, token = self.grant({"op": "count", "model": "res.partner", "domain": []}, risk_class="read")
        answer = self.execute(token, context={"allowed_company_ids": [other.id]})
        self.assertEqual(answer["error"]["code"], "ACCESS_DENIED")

    def test_company_selection_scopes_each_call_without_changing_the_assertion(self):
        main = self.employee.company_id
        other = self.env["res.company"].create({"name": "Second Office"})
        self.employee.write({"company_ids": [(6, 0, [main.id, other.id])]})
        answer = self.post("/botify_agent/v1/identity")
        self.assertTrue(answer["ok"], answer)
        token = answer["result"]["assertion"]
        payload = grant._open(grant.derive_key(SECRET, "identity"), "o1", token, 4096)
        self.assertCountEqual(payload["allowed_company_ids"], [main.id, other.id])

        for company in (main, other):
            _payload, token = self.grant({"op": "context"}, risk_class="read")
            answer = self.execute(token, context={"allowed_company_ids": [company.id]})
            self.assertTrue(answer["ok"], answer)
            self.assertEqual(answer["result"]["companies"], [{"id": company.id, "name": company.name}])

    # -- classification ----------------------------------------------------------

    def test_writes_to_protected_models_are_refused(self):
        for model, values in (
            ("res.users", {"email": "employee@example.test"}),
            ("res.groups", {"name": "Group"}),
            ("res.company", {"name": "Company"}),
            ("mail.template", {"name": "Template"}),
            ("res.partner.bank", {"acc_number": "1234"}),
            ("ir.config_parameter", {"value": "test"}),
        ):
            with self.subTest(model=model):
                _payload, token = self.grant({"op": "write", "model": model, "ids": [1], "values": values})
                self.assertEqual(self.execute(token)["error"], {
                    "code": "OPERATION_REFUSED", "message": "model_not_writable:%s" % model,
                })

    def test_other_mutations_of_protected_models_are_refused(self):
        for operation in (
            {"op": "create", "values": {"name": "Employee"}},
            {"op": "unlink", "ids": [self.employee.id]},
            {"op": "call", "ids": [self.employee.id], "method": "action_test"},
            {"op": "create_batch", "rows": [{"values": {"name": "Employee"}}]},
            {"op": "write_batch", "rows": [{"id": self.employee.id, "values": {"name": "Employee"}}]},
        ):
            with self.subTest(op=operation["op"]):
                _payload, token = self.grant(dict(operation, model="res.users"))
                self.assertEqual(self.execute(token)["error"], {
                    "code": "OPERATION_REFUSED", "message": "model_not_writable:res.users",
                })

    def test_nested_bank_account_creates_updates_and_deletes_are_refused(self):
        commands = ([0, 0, {"acc_number": "1234"}], [1, 1, {"acc_number": "1234"}], [2, 1])
        for command in commands:
            with self.subTest(command=command[0]):
                _payload, token = self.grant(self.write_op({"bank_ids": [command]}))
                self.assertEqual(self.execute(token)["error"], {
                    "code": "OPERATION_REFUSED", "message": "model_not_writable:res.partner.bank",
                })

    def test_task_assignee_links_keep_their_existing_risk_classes(self):
        if "project.task" not in self.env:
            self.skipTest("project.task requires the Project app")
        uid = self.employee.id
        cases = (
            ([4, uid], "high_write"),
            ([6, 0, [uid]], "high_write"),
            ([3, uid], "destructive"),
            ([5], "destructive"),
        )
        for command, risk in cases:
            with self.subTest(command=command[0]):
                operation = {"op": "write", "model": "project.task", "ids": [1], "values": {"user_ids": [command]}}
                self.assertEqual(classify.classify(self.env, operation), risk)

    def test_writes_to_pricelists_and_leave_allocations_are_financial(self):
        for model, values in (
            ("product.pricelist", {"name": "Wholesale"}),
            ("product.pricelist.item", {"fixed_price": 10}),
            ("hr.leave.allocation", {"number_of_days": 5}),
        ):
            with self.subTest(model=model):
                if model not in self.env:
                    self.skipTest("%s requires an optional Odoo app" % model)
                operation = {"op": "write", "model": model, "ids": [1], "values": values}
                self.assertEqual(classify.classify(self.env, operation), "financial")

    def test_ordinary_leave_requests_are_low_risk(self):
        if "hr.leave" not in self.env:
            self.skipTest("hr.leave requires the Time Off app")
        operation = {"op": "create", "model": "hr.leave", "values": {"request_date_to": "2026-10-08"}}
        self.assertEqual(classify.classify(self.env, operation), "low_write")
        self.assertEqual(classify.classify(self.env, dict(operation, op="write", ids=[1])), "low_write")

    def test_admin_can_refuse_writes_to_extra_models(self):

        config.write_parameter(self.env, "botify_agent.refused_write_models", " res.partner.category, , res.partner  ")
        _payload, token = self.grant(self.write_op({"phone": "123"}))
        self.assertEqual(self.execute(token)["error"], {
            "code": "OPERATION_REFUSED", "message": "model_not_writable:res.partner",
        })
        _payload, token = self.grant({"op": "describe_model", "model": "res.partner"}, risk_class="read")
        self.assertTrue(self.execute(token)["result"]["writeRefused"])
        config.write_parameter(self.env, "botify_agent.refused_write_models", "res.partner.category")
        _payload, token = self.grant(self.write_op({"category_id": [[0, 0, {"name": "New"}]]}))
        self.assertEqual(self.execute(token)["error"], {
            "code": "OPERATION_REFUSED", "message": "model_not_writable:res.partner.category",
        })

    def test_odoo_refuses_a_grant_weaker_than_its_own_classification(self):
        # Changing a relation on an existing record is high_write, whatever the grant says.
        other = self.env["res.partner"].create({"name": "Deco Addict"})
        _payload, token = self.grant(self.write_op({"parent_id": other.id}), risk_class="low_write")
        self.assertEqual(self.execute(token)["error"]["code"], "GRANT_REJECTED")
        # A class Odoo has not enabled is refused even with a matching grant.
        _payload, token = self.grant(self.write_op({"active": False}), risk_class="destructive")
        self.assertEqual(self.execute(token)["error"]["code"], "RISK_CLASS_DISABLED")
        # Protected fields are never written generically.
        for field in ("company_id", "create_uid", "commercial_partner_id"):
            _payload, token = self.grant(self.write_op({field: 1}), risk_class="high_write")
            self.assertEqual(self.execute(token)["error"]["code"], "OPERATION_REFUSED", field)

    def test_classification_does_not_depend_on_value_shape_or_key_order(self):
        env = self.env(user=self.employee)
        partner_write = lambda values: {"op": "write", "model": "res.partner", "ids": [self.partner.id], "values": values}  # noqa: E731
        # Odoo stores bool(value): every falsy value archives.
        for value in (False, 0, None, ""):
            self.assertEqual(classify.classify(env, partner_write({"active": value})), "destructive", value)
        # Financial and destructive at once is refused, whatever the key order.
        for values in ({"bank_ids": [[6, 0, []]], "active": False}, {"active": False, "bank_ids": [[6, 0, []]]}):
            with self.assertRaises(classify.Refused) as refused:
                classify.classify(env, partner_write(values))
            self.assertIn("mixed_high_risk", str(refused.exception))
        # Records whose creation sends messages.
        self.assertEqual(
            classify.classify(env, {"op": "create", "model": "mail.message", "values": {"body": "hi"}}),
            "external_communication",
        )

    def test_a_grant_of_another_top_class_is_refused(self):
        # Odoo sees a financial change; a grant saying destructive does not cover it.

        config.write_parameter(self.env, "botify_agent.allow_financial", "True")
        config.write_parameter(self.env, "botify_agent.allow_destructive", "True")
        _payload, token = self.grant(self.write_op({"bank_ids": [[6, 0, []]]}), risk_class="destructive")
        self.assertEqual(self.execute(token)["error"]["code"], "GRANT_REJECTED")

    def test_business_methods_need_a_registered_contract(self):
        method = self.env["botify.agent.method"].create(
            {
                "model": "res.partner",
                "method": "message_post",
                "risk_class": "low_write",
                "params_schema": json.dumps(
                    {"type": "object", "properties": {"body": {"type": "string", "maxLength": 200}}, "required": ["body"]}
                ),
            }
        )
        call = {"op": "call", "model": "res.partner", "method": "message_post", "ids": [self.partner.id]}

        def run(kwargs, method_name="message_post"):
            return self.execute(self.grant(dict(call, method=method_name, kwargs=kwargs))[1])

        self.assertEqual(run({"body": "hi", "subtype_xmlid": "mail.mt_comment"})["error"]["code"], "INVALID_ARGUMENTS")
        # A refusal after the grant is verified is audited under its callId, and a replay gets it again.
        payload, token = self.grant(dict(call, kwargs={"body": "hi", "extra": 1}))
        self.assertEqual(self.execute(token)["error"]["code"], "INVALID_ARGUMENTS")
        self.assertEqual(self.audit(payload["callId"]), [("failed", "INVALID_ARGUMENTS", self.employee.id)])
        self.assertTrue(self.execute(token)["replayed"])
        self.assertEqual(run({})["error"]["code"], "INVALID_ARGUMENTS")
        self.assertEqual(run({}, "message_subscribe")["error"]["code"], "METHOD_NOT_ALLOWED")
        self.assertEqual(run({}, "_message_log")["error"]["code"], "METHOD_NOT_ALLOWED")
        self.assertTrue(run({"body": "Called by the agent"})["ok"])
        method.active = False
        self.assertEqual(run({"body": "again"})["error"]["code"], "METHOD_NOT_ALLOWED")

    def test_orm_methods_cannot_be_registered_as_business_actions(self):
        for name in ("write", "unlink", "sudo", "with_user"):
            with self.assertRaises(Exception):
                with self.env.cr.savepoint():
                    self.env["botify.agent.method"].create({"model": "res.partner", "method": name, "risk_class": "high_write"})

    # -- reads, links, discovery -------------------------------------------------

    def test_search_counts_the_domain_with_employee_record_rules_before_pagination(self):
        partners = self.env["res.partner"].create([
            {"name": "Page one"},
            {"name": "Page two"},
            {"name": "Excluded by the domain"},
        ])
        company = self.env["res.company"].create({"name": "Other Co"})
        hidden = self.env["res.partner"].create({"name": "Page hidden", "company_id": company.id})
        domain = [["id", "in", partners.ids + hidden.ids], ["name", "=like", "Page %"]]
        for offset, expected_ids in ((0, partners.ids[:1]), (1, partners.ids[1:2]), (2, [])):
            with self.subTest(offset=offset):
                answer = self.read_operation("search", domain=domain, fields=["name"], limit=1, offset=offset, order="id")
                self.assertTrue(answer["ok"], answer)
                result = answer["result"]
                self.assertEqual([row["id"] for row in result["records"]], expected_ids)
                self.assertEqual(result["total"], 2)
                self.assertIs(result["truncated"], True)

        answer = self.read_operation("search", domain=domain, fields=["name"], limit=2)
        self.assertEqual(answer["result"]["total"], 2)
        self.assertNotIn("truncated", answer["result"])

    def test_read_counts_the_distinct_requested_records(self):
        answer = self.read_operation("read", ids=[self.partner.id, self.partner.id], fields=["name"])
        self.assertTrue(answer["ok"], answer)
        self.assertEqual([row["id"] for row in answer["result"]["records"]], [self.partner.id])
        self.assertEqual(answer["result"]["total"], 1)
        self.assertNotIn("truncated", answer["result"])

    def test_reads_return_module_generated_links_and_open_record_stays_in_the_browser(self):
        # The link's origin is web.base.url, never the host the request came in on.
        config.write_parameter(self.env, "web.base.url", "https://erp.example.com")
        _payload, token = self.grant(
            {"op": "search", "model": "res.partner", "domain": [["id", "=", self.partner.id]], "fields": ["name"], "limit": 5, "offset": 0},
            risk_class="read",
        )
        record = self.execute(token)["result"]["records"][0]
        self.assertEqual(record["url"], "https://erp.example.com/odoo/res.partner/%d" % self.partner.id)
        _payload, token = self.grant({"op": "open_record", "model": "res.partner", "ids": [self.partner.id]}, risk_class="read")
        answer = self.execute(token)
        self.assertEqual(answer["client"]["action"]["res_id"], self.partner.id)
        self.assertEqual(answer["result"]["url"], "https://erp.example.com/odoo/res.partner/%d" % self.partner.id)
        # No link for a record the employee cannot read: the operation is refused instead.
        _payload, token = self.grant({"op": "open_record", "model": "ir.config_parameter", "ids": [self.env["ir.config_parameter"].sudo().search([], limit=1).id]}, risk_class="read")
        refused = self.execute(token)
        self.assertFalse(refused["ok"])
        self.assertNotIn("url", str(refused))

    def test_an_unknown_field_is_refused_with_the_closest_real_fields(self):
        _payload, token = self.grant(
            {"op": "search", "model": "res.partner", "domain": [], "fields": ["nmae"], "limit": 1, "offset": 0},
            risk_class="read",
        )
        answer = self.execute(token)
        self.assertEqual(answer["error"]["code"], "VALIDATION_FAILED")
        self.assertIn("unknown field nmae on res.partner", answer["error"]["message"])
        self.assertIn("Did you mean: name", answer["error"]["message"])

    def test_describe_marks_protected_fields_readonly_and_lists_allowed_methods(self):
        self.env["botify.agent.field_rule"].create({"model": "res.partner", "field": "comment", "risk_class": "financial"})
        _payload, token = self.grant({"op": "describe_model", "model": "res.partner"}, risk_class="read")
        described = self.execute(token)["result"]
        self.assertTrue(described["fields"]["company_id"]["readonly"])
        self.assertNotIn("readonly", described["fields"]["phone"])
        self.assertEqual(described["fields"]["comment"]["riskClass"], "financial")
        _payload, token = self.grant({"op": "describe_model", "model": "ir.config_parameter"}, risk_class="read")
        self.assertEqual(self.execute(token)["error"]["code"], "ACCESS_DENIED")

    def test_describe_returns_bounded_single_line_help_for_visible_fields(self):
        fields = self.env["res.partner"]._fields
        with (
            patch.object(fields["phone"], "help", " Phone\n number\t " + "x" * 250),
            patch.object(fields["name"], "help", " \n "),
            patch.object(fields["email"], "help", "Hidden help"),
        ):
            config.write_parameter(self.env, "botify_agent.refused_read_fields", "res.partner.email")
            described = self.read_operation("describe_model")["result"]["fields"]
        self.assertEqual(described["phone"]["string"], "Phone")
        self.assertEqual(described["phone"]["help"], "Phone number " + "x" * 187)
        self.assertNotIn("help", described["name"])
        self.assertNotIn("email", described)

    def test_credentials_are_hidden_from_describe_and_refused_in_reads(self):
        described = self.read_operation("describe_model", "res.users")["result"]
        self.assertIs(described["fieldsHidden"], True)
        self.assertIn("name", described["fields"])
        for name in ("login", "password", "signature"):
            with self.subTest(field=name):
                self.assertNotIn(name, described["fields"])
                for op, arguments in (
                    ("search", {"fields": ["name", name]}),
                    ("read", {"ids": [self.employee.id], "fields": [name]}),
                ):
                    answer = self.read_operation(op, "res.users", **arguments)
                    self.assertEqual(answer["error"]["code"], "ACCESS_DENIED", answer)
                    self.assertIn("hidden by this installation", answer["error"]["message"])
                    self.assertNotIn("result", answer)

    def test_sensitive_name_patterns_include_whole_key_segments(self):
        for name in (
            "password_hash", "access_token", "clientsecret", "api_key", "apikey", "key",
            "x_key_backup", "signing_key", "html_signature", "totp_enabled", "otp_code", "is_private",
        ):
            with self.subTest(field=name):
                answer = self.read_operation("search", fields=["not_a_real_field", name])
                self.assertEqual(answer["error"]["code"], "ACCESS_DENIED", answer)
        for name in ("keyword", "monkey"):
            with self.subTest(field=name):
                answer = self.read_operation("search", fields=[name])
                self.assertEqual(answer["error"]["code"], "VALIDATION_FAILED", answer)

    def test_hidden_fields_cannot_be_used_in_filters_order_or_aggregates(self):
        for op, arguments in (
            ("search", {"domain": [["signature", "=", False]]}),
            ("search", {"order": "name asc, signature DESC"}),
            ("aggregate", {"groupby": ["signature"], "aggregates": ["__count"]}),
            ("aggregate", {"groupby": ["active"], "aggregates": ["signature:count_distinct"]}),
            ("aggregate", {"groupby": ["active"], "aggregates": ["__count"], "domain": [["signature", "=", False]]}),
        ):
            with self.subTest(operation=op, arguments=arguments):
                answer = self.read_operation(op, "res.users", **arguments)
                self.assertEqual(answer["error"]["code"], "ACCESS_DENIED", answer)
                self.assertNotIn("groups", answer)
        for op, arguments in (
            ("count", {"domain": [["user_id.signature", "=", False]]}),
            ("search", {"fields": ["user_id.signature"]}),
        ):
            answer = self.read_operation(op, **arguments)
            self.assertEqual(answer["error"]["code"], "ACCESS_DENIED", answer)

    def test_denied_models_are_not_discoverable_or_readable(self):
        config.write_parameter(
            self.env,
            "botify_agent.refused_read_models", " res.partner.category, , res.country "
        )
        for model in ("ir.config_parameter", "payment.provider", "res.users.apikeys", "auth_oauth.provider", "res.partner.category", "res.country"):
            for op, arguments in (
                ("describe_model", {}),
                ("search", {}),
                ("read", {"ids": [1]}),
                ("count", {}),
                ("aggregate", {"groupby": ["name"], "aggregates": ["__count"]}),
                ("name_search", {"name": "x"}),
                ("name_search_batch", {"names": ["x"]}),
                ("open_record", {"ids": [1]}),
            ):
                with self.subTest(model=model, operation=op):
                    answer = self.read_operation(op, model, **arguments)
                    self.assertEqual(answer["error"]["code"], "ACCESS_DENIED", answer)
        for model in ("res.partner.category", "res.country"):
            _payload, token = self.grant({"op": "list_models", "query": model}, risk_class="read")
            listed = {entry["model"] for entry in self.execute(token)["result"]["models"]}
            self.assertNotIn(model, listed)
            if model == "res.country":
                self.assertIn("res.country.state", listed)
        answer = self.read_operation("count", "res.country.state")
        self.assertTrue(answer["ok"], answer)
        answer = self.read_operation("count", domain=[["country_id.name", "=", "x"]])
        self.assertEqual(answer["error"]["code"], "ACCESS_DENIED", answer)
        answer = self.read_operation("read", ids=[self.partner.id], fields=["country_id"])
        self.assertEqual(answer["error"]["code"], "ACCESS_DENIED", answer)

    def test_admin_can_hide_fields_by_model_and_change_the_setting_live(self):

        config.write_parameter(self.env, "botify_agent.refused_read_fields", " res.partner.phone, , res.country.code ")
        described = self.read_operation("describe_model")["result"]
        self.assertIs(described["fieldsHidden"], True)
        self.assertNotIn("phone", described["fields"])
        self.assertIn("name", described["fields"])
        for op, arguments in (
            ("search", {"fields": ["name", "phone"]}),
            ("read", {"ids": [self.partner.id], "fields": ["phone"]}),
            ("aggregate", {"groupby": ["type"], "aggregates": ["phone:count_distinct"]}),
            ("count", {"domain": [["country_id.code", "=", "US"]]}),
        ):
            with self.subTest(operation=op):
                answer = self.read_operation(op, **arguments)
                self.assertEqual(answer["error"]["code"], "ACCESS_DENIED", answer)
        unknown = self.read_operation("search", fields=["phoen"])
        self.assertEqual(unknown["error"]["code"], "VALIDATION_FAILED")
        self.assertNotIn("phone", unknown["error"]["message"])
        answer = self.read_operation("aggregate", groupby=["type"], aggregates=["__count"], domain=[["id", "=", self.partner.id]])
        self.assertTrue(answer["result"]["groups"])
        config.write_parameter(self.env, "botify_agent.refused_read_fields", "res.country.code")
        answer = self.read_operation("read", ids=[self.partner.id], fields=["phone"])
        self.assertEqual(answer["result"]["records"][0]["phone"], "111")

    def test_hidden_record_names_are_not_returned_as_display_labels(self):
        config.write_parameter(
            self.env,
            "botify_agent.refused_read_fields", "res.partner.name, res.country.name"
        )
        for op, arguments in (
            ("search", {}),
            ("read", {"ids": [self.partner.id], "fields": ["country_id"]}),
            ("name_search", {"name": "Azure"}),
            ("name_search_batch", {"names": ["Azure"]}),
            ("open_record", {"ids": [self.partner.id]}),
        ):
            with self.subTest(operation=op):
                answer = self.read_operation(op, **arguments)
                self.assertEqual(answer["error"]["code"], "ACCESS_DENIED", answer)
        answer = self.read_operation("search", fields=["phone"], domain=[["id", "=", self.partner.id]])
        self.assertEqual(answer["result"]["records"][0]["phone"], "111")

    def test_describe_lists_choices_required_on_create_and_key_fields(self):
        _payload, token = self.grant({"op": "describe_model", "model": "res.partner"}, risk_class="read")
        fields = self.execute(token)["result"]["fields"]
        self.assertIn(["invoice", "Invoice"], fields["type"]["selection"])
        self.assertNotIn("selection", fields["name"])
        # Stored and writable business fields are key; computed and technical ones are not.
        self.assertTrue(fields["name"]["key"])
        self.assertTrue(fields["type"]["key"])
        for name in ("display_name", "create_date", "write_uid", "commercial_partner_id"):
            self.assertNotIn("key", fields[name], name)
        _payload, token = self.grant({"op": "describe_model", "model": "res.users"}, risk_class="read")
        users = self.execute(token)["result"]["fields"]
        categories = self.read_operation("describe_model", "res.partner.category")["result"]["fields"]
        self.assertIs(categories["name"]["requiredOnCreate"], True)
        self.assertIs(users["company_id"]["requiredOnCreate"], False)
        self.assertNotIn("requiredOnCreate", users["id"])

    def test_batch_name_search_returns_matches_per_text_in_one_call(self):
        self.env["res.partner"].create({"name": "Deco Addict"})
        names = ["Azure Interior", "Deco Addict", "Nobody Such"]
        _payload, token = self.grant({"op": "name_search_batch", "model": "res.partner", "names": names, "limit": 5}, risk_class="read")
        results = self.execute(token)["result"]["results"]
        self.assertEqual([row["name"] for row in results], names)
        self.assertIn(self.partner.id, [match["id"] for match in results[0]["matches"]])
        self.assertEqual(results[1]["matches"][0]["display_name"], "Deco Addict")
        self.assertEqual(results[2]["matches"], [])

    def test_batch_name_search_refuses_more_than_fifty_texts_and_is_a_read(self):
        op = {"op": "name_search_batch", "model": "res.partner", "names": ["n%d" % i for i in range(51)]}
        _payload, token = self.grant(op, risk_class="read")
        answer = self.execute(token)
        self.assertEqual(answer["error"]["code"], "VALIDATION_FAILED")
        self.assertIn("at most 50", answer["error"]["message"])
        self.assertEqual(classify.classify(self.env, dict(op, names=["x"])), "read")

    # -- batch writes (ADR 0037) ---------------------------------------------------

    def partners(self, op="create_batch", rows=None):
        return {"op": op, "model": "res.partner", "rows": rows}

    def test_context_lists_the_batch_operations(self):
        _payload, token = self.grant({"op": "context"}, risk_class="read")
        self.assertEqual(self.execute(token)["result"]["operations"], ["create_batch", "write_batch"])

    def test_create_batch_runs_each_row_alone_and_reports_it(self):
        rows = [{"values": {"name": "Batch A"}}, {"values": {"name": "Batch B", "state": "x"}}, {"values": {"name": "Batch C"}}]
        _payload, token = self.grant(self.partners(rows=rows))
        answer = self.execute(token)
        self.assertTrue(answer["ok"], answer)
        result = answer["result"]["rows"]
        self.assertEqual([row["status"] for row in result], ["created", "failed", "created"])
        self.assertEqual(result[1]["message"], "field_not_writable:state")
        names = self.env["res.partner"].browse([result[0]["id"], result[2]["id"]]).mapped("name")
        self.assertEqual(names, ["Batch A", "Batch C"])
        # Replaying the same grant gives the stored answer and creates nothing.
        before = self.env["res.partner"].search_count([("name", "like", "Batch ")])
        replay = self.execute(token)
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.env["res.partner"].search_count([("name", "like", "Batch ")]), before)

    @mute_logger("odoo.sql_db", "odoo.addons.botify_agent.services.errors")
    def test_a_row_failing_in_odoo_does_not_undo_the_others(self):
        rows = [{"values": {"name": "Kept"}}, {"values": {"name": False}}]
        _payload, token = self.grant(self.partners(rows=rows))
        result = self.execute(token)["result"]["rows"]
        self.assertEqual([row["status"] for row in result], ["created", "failed"])
        self.assertTrue(self.env["res.partner"].browse(result[0]["id"]).exists())

    def test_a_row_key_never_creates_the_record_twice(self):
        rows = [{"key": "sheet:7", "values": {"name": "Keyed partner"}}]
        _payload, token = self.grant(self.partners(rows=rows))
        first = self.execute(token)["result"]["rows"][0]
        self.assertEqual(first["status"], "created")
        # The same job in a later conversation: a new grant, the same key.
        _payload, token = self.grant(self.partners(rows=rows))
        again = self.execute(token)["result"]["rows"][0]
        self.assertEqual((again["status"], again["id"]), ("skipped", first["id"]))
        self.assertEqual(self.env["res.partner"].search_count([("name", "=", "Keyed partner")]), 1)

    def test_a_row_key_belongs_to_the_employee_who_used_it(self):
        rows = [{"key": "1", "values": {"name": "Lead of A"}}]
        _payload, token = self.grant(self.partners(rows=rows))
        first = self.execute(token)["result"]["rows"][0]
        self.assertEqual(first["status"], "created")
        # Another employee importing a sheet with the same key gets a record of their own.
        self.session = self.authenticate("botify_employee_2", "botify_employee_2")
        rows = [{"key": "1", "values": {"name": "Lead of B"}}]
        _payload, token = self.grant(self.partners(rows=rows), uid=self.second_employee.id)
        second = self.execute(token)["result"]["rows"][0]
        self.assertEqual(second["status"], "created")
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(self.env["res.partner"].browse(second["id"]).create_uid, self.second_employee)

    def test_a_keyed_record_the_employee_cannot_read_counts_as_absent(self):
        operations = self.env["botify.agent.operation"].with_user(self.employee)
        model = self.env["res.partner"].with_user(self.employee)
        # A contact of a company the employee is not in: the company record rule hides it.
        other_company = self.env["res.company"].create({"name": "Other Co"})
        hidden = self.env["res.partner"].create({"name": "Hidden", "company_id": other_company.id})
        operations._remember_key(model, "9", hidden)
        data, record = operations._keyed_record(model, "9")
        self.assertTrue(data)
        self.assertFalse(record)

    def test_write_batch_is_high_write_and_reclassified_per_row(self):
        other = self.env["res.partner"].create({"name": "Second"})
        rows = [{"id": self.partner.id, "values": {"phone": "501"}}, {"id": other.id, "values": {"phone": "502"}}]
        _payload, token = self.grant(self.partners("write_batch", rows), risk_class="low_write")
        self.assertEqual(self.execute(token)["error"]["code"], "GRANT_REJECTED")
        _payload, token = self.grant(self.partners("write_batch", rows), risk_class="high_write")
        result = self.execute(token)["result"]["rows"]
        self.assertEqual([row["status"] for row in result], ["updated", "updated"])
        self.assertEqual((self.partner.phone, other.phone), ("501", "502"))

    def test_a_batch_class_is_its_strictest_row(self):
        rows = [{"values": {"name": "Plain"}}, {"values": {"name": "Owned", "user_id": self.employee.id}}]
        self.assertEqual(classify.classify(self.env, self.partners(rows=rows)), "low_write")
        rows[1]["values"]["category_id"] = [[6, 0, []]]
        self.assertEqual(classify.classify(self.env, self.partners(rows=rows)), "high_write")

    def test_a_malformed_batch_is_refused_before_anything_runs(self):
        for rows in ([], [{"values": {}}], [{"values": {"name": "x"}, "id": 3}], [{"key": "", "values": {"name": "x"}}]):
            _payload, token = self.grant(self.partners(rows=rows))
            self.assertEqual(self.execute(token)["error"]["code"], "VALIDATION_FAILED", rows)

    def test_stored_editable_computed_fields_are_writable(self):
        # Some Odoo series removed company_registry. Both have an editable
        # stored language compute whose explicit value survives the write.
        field = "company_registry" if "company_registry" in self.partner._fields else "lang"
        value = "REG-1" if field == "company_registry" else "en_US"
        self.partner[field] = False
        native = self.partner._fields[field]
        self.assertTrue(native.compute and native.store and not native.readonly)
        _payload, token = self.grant({"op": "describe_model", "model": "res.partner"}, risk_class="read")
        fields = self.execute(token)["result"]["fields"]
        self.assertNotIn("readonly", fields[field])
        self.assertTrue(fields["display_name"]["readonly"])
        _payload, token = self.grant(self.write_op({field: value}))
        self.assertTrue(self.execute(token)["ok"])
        self.partner.invalidate_recordset()
        self.assertEqual(self.partner[field], value)

    # -- code rules ----------------------------------------------------------------

    def test_no_sudo_on_the_execution_path(self):
        for path in (
            "services/executor.py", "services/classify.py", "services/privacy.py",
            "services/guard.py", "controllers/main.py",
        ):
            with open(os.path.join(MODULE, path), encoding="utf-8") as handle:
                self.assertIsNone(re.search(r"\.sudo\(", handle.read()), path)

    # -- notification events --------------------------------------------------

    def events_model(self):
        model = self.env["botify.agent.event"]
        self.patch(type(model), "_botify_event_kinds", lambda _self: {"test.happened": {"what": ("str", True), "count": ("int", False)}})
        return model

    def pending(self):
        answer = self.post("/botify_agent/v1/events/pending")
        self.assertTrue(answer["ok"], answer)
        return [
            grant._open(grant.derive_key(SECRET, "notification"), "n1", token, 8192)
            for token in answer["result"]["events"]
        ]

    def ack(self, event, **overrides):
        now = int(time.time())
        payload = {"v": 1, "iss": KEY, "db": self.env.cr.dbname, "uid": event.user_id.id,
                   "eventId": event.event_id, "outcome": "delivered", "iat": now, "exp": now + 300}
        payload.update(overrides)
        return grant._sign(grant.derive_key(SECRET, "ack"), "a1", payload)

    def test_notify_records_only_valid_events_for_botify_users(self):
        events = self.events_model()
        event = events._notify(self.employee, "test.happened", {"what": "leave", "count": 2, "extra": None})
        self.assertTrue(event)
        self.assertEqual(json.loads(event.facts_json), {"count": 2, "what": "leave"})
        self.assertEqual(event.state, "pending")
        refused = {
            "unknown kind": (self.employee, "test.other", {"what": "x"}),
            "unknown fact": (self.employee, "test.happened", {"what": "x", "secret": "y"}),
            "missing fact": (self.employee, "test.happened", {"count": 1}),
            "wrong type": (self.employee, "test.happened", {"what": "x", "count": "2"}),
            "bool is not int": (self.employee, "test.happened", {"what": "x", "count": True}),
            "too long": (self.employee, "test.happened", {"what": "x" * 201}),
            "not a Botify user": (self.outsider, "test.happened", {"what": "x"}),
        }
        for name, (user, kind, facts) in refused.items():
            self.assertFalse(events._notify(user, kind, facts), name)
        config.write_parameter(self.env, "botify_agent.enabled", "False")
        self.assertFalse(events._notify(self.employee, "test.happened", {"what": "x"}), "disabled")
        self.assertEqual(events.sudo().search_count([]), 1)

    def test_notify_is_not_callable_from_the_browser_and_events_are_not_readable(self):
        with self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request(
                "/web/dataset/call_kw/botify.agent.event/_notify",
                {"model": "botify.agent.event", "method": "_notify",
                 "args": [self.employee.id, "test.happened", {"what": "x"}], "kwargs": {}},
            )
        self.assertEqual(self.env["botify.agent.event"].sudo().search_count([]), 0)
        with self.assertRaises(AccessError):
            self.env["botify.agent.event"].with_user(self.employee).search([])

    def test_pending_hands_out_only_the_employees_own_events_signed_and_leased(self):
        events = self.events_model()
        own = events._notify(self.employee, "test.happened", {"what": "mine"}, company=self.env.company)
        self.assertTrue(events._notify(self.colleague, "test.happened", {"what": "not mine"}))
        [payload] = self.pending()
        self.assertEqual(
            {key: payload[key] for key in ("iss", "db", "uid", "eventId", "kind", "facts")},
            {"iss": KEY, "db": self.env.cr.dbname, "uid": self.employee.id, "eventId": own.event_id,
             "kind": "test.happened", "facts": {"what": "mine"}},
        )
        self.assertEqual(payload["company"]["id"], self.env.company.id)
        self.assertLessEqual(payload["exp"] - payload["iat"], 300)
        self.assertEqual(self.pending(), [], "leased: a second tab gets nothing")
        self.assertEqual((own.state, own.attempts), ("delivering", 1))
        # Nobody acknowledged it (Botify down): handed out again once the lease is over.
        own.sudo().lease_until = fields.Datetime.now() - timedelta(seconds=1)
        self.assertEqual([payload["eventId"] for payload in self.pending()], [own.event_id])
        self.assertEqual(own.attempts, 2)

    def test_pending_is_refused_outside_the_group_or_when_disabled(self):
        self.session = self.authenticate("no_botify", "no_botify_pw")
        self.assertEqual(self.post("/botify_agent/v1/events/pending")["error"]["code"], "ACCESS_DENIED")
        config.write_parameter(self.env, "botify_agent.enabled", "False")
        self.session = self.authenticate("botify_employee", "botify_employee_pw")
        self.assertEqual(self.post("/botify_agent/v1/events/pending")["error"]["code"], "INTEGRATION_DISABLED")

    def test_only_a_valid_ack_for_the_employees_own_event_changes_it(self):
        events = self.events_model()
        own = events._notify(self.employee, "test.happened", {"what": "mine"})
        other = events._notify(self.colleague, "test.happened", {"what": "not mine"})
        forged = self.ack(own).rsplit(".", 1)[0] + "." + grant._b64(b"x" * 32)
        wrong_purpose = grant._sign(grant.derive_key(SECRET, "grant"), "a1", {"eventId": own.event_id})
        refused = [
            forged,
            wrong_purpose,
            self.ack(own, iss="wsk_another_installation"),
            self.ack(own, db="another_db"),
            self.ack(own, uid=self.outsider.id),
            self.ack(own, iat=int(time.time()) - 400, exp=int(time.time()) - 100),
            self.ack(other),
            self.ack(other, uid=self.employee.id),
        ]
        woken = []
        self.patch(type(self.env["res.users"]), "_bus_send", lambda users, kind, _payload: woken.append((users.ids, kind)))
        answer = self.post("/botify_agent/v1/events/ack", {"acks": refused})
        self.assertEqual(answer["result"]["acknowledged"], 0, answer)
        self.assertEqual((own.state, other.state), ("pending", "pending"))
        self.assertEqual(woken, [], "nothing acknowledged: no tab is woken")
        answer = self.post("/botify_agent/v1/events/ack", {"acks": [self.ack(own)]})
        self.assertEqual(answer["result"]["acknowledged"], 1)
        # The employee's other tabs are woken to refresh their unread count.
        self.assertEqual(woken, [([self.employee.id], "botify_agent/event")])
        replayed = self.post("/botify_agent/v1/events/ack", {"acks": [self.ack(own)]})
        self.assertEqual(replayed["result"]["acknowledged"], 0)
        self.assertEqual(len(woken), 1, "a replayed receipt wakes nobody")
        self.assertEqual(own.state, "delivered")
        self.assertTrue(own.delivered_at)
        # A late "rejected" receipt never undoes a delivery.
        self.post("/botify_agent/v1/events/ack", {"acks": [self.ack(own, outcome="rejected")]})
        self.assertEqual(own.state, "delivered")

    def test_a_rejected_event_fails_and_is_never_handed_out_again(self):
        events = self.events_model()
        event = events._notify(self.employee, "test.happened", {"what": "x"})
        self.post("/botify_agent/v1/events/ack", {"acks": [self.ack(event, outcome="rejected", reason="unknown kind")]})
        self.assertEqual((event.state, event.last_error), ("failed", "unknown kind"))
        self.assertEqual(self.pending(), [])

    def test_undelivered_events_back_off_and_expire_after_a_week(self):
        events = self.events_model()
        event = events._notify(self.employee, "test.happened", {"what": "waiting"})
        leases = []
        for _attempt in range(8):
            event.sudo().lease_until = fields.Datetime.now() - timedelta(seconds=1)
            self.assertEqual(len(self.pending()), 1)
            leases.append(round((event.lease_until - fields.Datetime.now()).total_seconds() / 60))
        # 1, 2, 4 … minutes, capped at an hour; still deliverable after many tries.
        self.assertEqual(leases, [1, 2, 4, 8, 16, 32, 60, 60])
        self.assertEqual((event.state, event.attempts), ("delivering", 8))
        self.env.cr.execute(
            "UPDATE botify_agent_event SET create_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(days=8), event.id),
        )
        events.invalidate_model()
        events._gc()
        self.assertEqual(event.state, "expired")
        event.sudo().lease_until = False
        self.assertEqual(self.pending(), [])

    def test_labels_are_measured_as_botify_measures_them(self):
        events = self.events_model()
        self.assertTrue(events._notify(self.employee, "test.happened", {"what": "🌴" * 100}))
        self.assertFalse(events._notify(self.employee, "test.happened", {"what": "🌴" * 101}))
