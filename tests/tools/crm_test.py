# Copyright 2026 Rodrigo Palma.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test cases for the tools that send Bitrix24 CRM data to Google Ads."""

import datetime
import hashlib
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import requests
from fastmcp.exceptions import ToolError

from ads_mcp import bitrix24, data_manager
from ads_mcp.tools import crm
from tests.tools.create_test import CreateToolTestCase
from tests.tools.mutate_test import list_tools

WEBHOOK = "https://example.bitrix24.cl/rest/1/secret-token/"


def sha256(value):
    return hashlib.sha256(value.encode()).hexdigest()


def days_ago(days):
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


DEALS = [
    {
        "ID": "11",
        "CATEGORY_ID": "73",
        "CONTACT_ID": "1",
        "MOVED_TIME": f"{days_ago(3)}T19:40:05+03:00",
        "OPPORTUNITY": "1800.00",
        "CURRENCY_ID": "USD",
    },
    {
        "ID": "12",
        "CATEGORY_ID": "73",
        "CONTACT_ID": "2",
        "MOVED_TIME": f"{days_ago(2)}T10:00:00+03:00",
        "OPPORTUNITY": "0.00",
        "CURRENCY_ID": "CLP",
    },
    {
        "ID": "13",
        "CATEGORY_ID": "91",
        "CONTACT_ID": "3",
        "MOVED_TIME": f"{days_ago(1)}T10:00:00+03:00",
        "OPPORTUNITY": "900",
        "CURRENCY_ID": "USD",
    },
    {
        "ID": "14",
        "CATEGORY_ID": "91",
        "CONTACT_ID": None,
        "MOVED_TIME": f"{days_ago(1)}T11:00:00+03:00",
        "OPPORTUNITY": "900",
        "CURRENCY_ID": "USD",
    },
]
CONTACTS = [
    {
        "ID": "1",
        "EMAIL": [{"VALUE": "Ana.Perez@Gmail.com", "VALUE_TYPE": "WORK"}],
        "PHONE": [{"VALUE": "+56 9 1234 5678", "VALUE_TYPE": "MOBILE"}],
    },
    {
        "ID": "2",
        "EMAIL": [{"VALUE": "no-es-correo", "VALUE_TYPE": "WORK"}],
        "PHONE": [{"VALUE": "9 8765 4321", "VALUE_TYPE": "MOBILE"}],
    },
    {"ID": "3", "EMAIL": [], "PHONE": []},
]


class TestBitrix24Client(unittest.TestCase):

    def test_requires_the_webhook(self):
        with patch.dict(os.environ, {bitrix24.WEBHOOK_ENV_VAR: ""}):
            self.assertFalse(bitrix24.configured())
            with self.assertRaisesRegex(ToolError, "not connected"):
                bitrix24.call("crm.deal.list")

    def test_rejects_plain_http(self):
        with patch.dict(
            os.environ, {bitrix24.WEBHOOK_ENV_VAR: "http://x.bitrix24.cl/"}
        ):
            with self.assertRaisesRegex(ToolError, "https"):
                bitrix24.call("crm.deal.list")

    def response(self, body, status=200):
        response = MagicMock(status_code=status)
        response.json.return_value = body
        return response

    @patch.dict(os.environ, {bitrix24.WEBHOOK_ENV_VAR: WEBHOOK})
    def test_errors_do_not_reveal_the_webhook(self):
        with patch(
            "requests.post", side_effect=requests.ConnectionError(WEBHOOK)
        ):
            with self.assertRaises(ToolError) as raised:
                bitrix24.call("crm.deal.list")
        self.assertNotIn("secret-token", str(raised.exception))

        error = {"error": "INVALID_CREDENTIALS", "error_description": "bad"}
        with patch("requests.post", return_value=self.response(error, 401)):
            with self.assertRaisesRegex(ToolError, "INVALID_CREDENTIALS"):
                bitrix24.call("crm.deal.list")

    @patch.dict(os.environ, {bitrix24.WEBHOOK_ENV_VAR: WEBHOOK})
    @patch("time.sleep")
    def test_retries_when_rate_limited(self, sleep):
        responses = [
            self.response({"error": "QUERY_LIMIT_EXCEEDED"}, 503),
            self.response({"result": [1]}),
        ]
        with patch("requests.post", side_effect=responses) as post:
            self.assertEqual(bitrix24.call("crm.deal.list"), {"result": [1]})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.args[0], f"{WEBHOOK}crm.deal.list.json")
        sleep.assert_called_once()

    def test_list_all_follows_the_pages(self):
        pages = [
            {"result": [{"ID": 1}, {"ID": 2}], "next": 2},
            {"result": [{"ID": 3}]},
        ]
        with patch("ads_mcp.bitrix24.call", side_effect=pages) as call:
            items = bitrix24.list_all("crm.deal.list", {"filter": {}})
        self.assertEqual([item["ID"] for item in items], [1, 2, 3])
        self.assertEqual(call.call_args_list[1].args[1]["start"], 2)


class TestContactData(unittest.TestCase):

    def test_phones(self):
        cases = [
            ("+56 9 1234 5678", None, "+56912345678"),
            ("0056 9 1234 5678", None, "+56912345678"),
            ("9 1234 5678", "56", "+56912345678"),
            ("56912345678", "56", "+56912345678"),
            ("09 1234 5678", "+56", "+56912345678"),
            ("9 1234 5678", None, None),
            ("123", "56", None),
        ]
        for value, code, expected in cases:
            with self.subTest(value=value, code=code):
                contact = {"PHONE": [{"VALUE": value}]}
                self.assertEqual(
                    crm._crm_phone(contact, crm._check_country_code(code)),
                    expected,
                )

    def test_email_skips_invalid_values(self):
        contact = {
            "EMAIL": [{"VALUE": "malo"}, {"VALUE": " Ana@UAutonoma.cl "}]
        }
        self.assertEqual(crm._crm_email(contact), "ana@uautonoma.cl")
        self.assertIsNone(crm._crm_email({"EMAIL": None}))

    def test_period(self):
        with self.assertRaisesRegex(ToolError, "90 days"):
            crm._period(days_ago(91), None)
        with self.assertRaisesRegex(ToolError, "before"):
            crm._period(days_ago(3), days_ago(5))
        start, end = crm._period(days_ago(7), None)
        self.assertEqual(end, datetime.date.today())

    def test_country_code(self):
        for invalid in ("0", "abc", "1234"):
            with self.subTest(invalid):
                with self.assertRaises(ToolError):
                    crm._check_country_code(invalid)


def fake_bitrix(method, params, max_items=None):
    if method == "crm.deal.list":
        return DEALS
    if method == "crm.contact.list":
        ids = params["filter"]["ID"]
        return [contact for contact in CONTACTS if contact["ID"] in ids]
    if method == "crm.status.list":
        return [
            {"ENTITY_ID": "DEAL_STAGE", "STATUS_ID": "WON", "NAME": "Ganado"},
            {
                "ENTITY_ID": "DEAL_STAGE_73",
                "STATUS_ID": "C73:NEW",
                "NAME": "Info enviada",
            },
            {
                "ENTITY_ID": "DEAL_STAGE_73",
                "STATUS_ID": "C73:WON",
                "NAME": "Matriculado",
                "SEMANTICS": "S",
            },
            {"ENTITY_ID": "SOURCE", "STATUS_ID": "WON", "NAME": "x"},
        ]
    raise AssertionError(method)


class CrmToolTestCase(CreateToolTestCase):

    def setUp(self):
        super().setUp()
        patchers = [
            patch.dict(os.environ, {data_manager.ENABLE_ENV_VAR: "true"}),
            patch("ads_mcp.bitrix24.list_all", side_effect=fake_bitrix),
            patch(
                "ads_mcp.data_manager.call",
                return_value={"requestId": "req-7"},
            ),
        ]
        started = [patcher.start() for patcher in patchers]
        for patcher in patchers:
            self.addCleanup(patcher.stop)
        self.list_all, self.call = started[1], started[2]

    def sent_json(self):
        self.call.assert_called_once()
        return self.call.call_args.kwargs["json"]


class TestSyncCrmConversions(CrmToolTestCase):

    def setUp(self):
        super().setUp()
        self.set_rows(
            self.make_row(
                {
                    "customer.currency_code": "USD",
                    "customer.time_zone": "America/Santiago",
                    "conversion_action.name": "Matrícula",
                    "conversion_action.type_": self.client.enums.ConversionActionTypeEnum.UPLOAD_CLICKS,
                }
            )
        )

    def sync(self, **kwargs):
        args = {
            "customer_id": "3938508333",
            "conversion_action_id": "555",
            "pipeline_ids": [73, 91, 73],
            "start_date": days_ago(7),
            "default_phone_country_code": "56",
        }
        args.update(kwargs)
        return crm.sync_crm_conversions(**args)

    def test_uploads_won_deals_identified_by_contact_data(self):
        with self.assertLogs("ads_mcp.utils", "INFO") as logs:
            result = self.sync()

        deal_query = self.list_all.call_args_list[0]
        self.assertEqual(deal_query.args[0], "crm.deal.list")
        deal_filter = deal_query.args[1]["filter"]
        self.assertEqual(deal_filter["CATEGORY_ID"], [73, 91])
        self.assertEqual(deal_filter["STAGE_SEMANTIC_ID"], "S")
        self.assertEqual(deal_filter[">=MOVED_TIME"], days_ago(7))
        contact_query = self.list_all.call_args_list[1].args[1]
        self.assertEqual(contact_query["filter"]["ID"], ["1", "2", "3"])

        body = self.sent_json()
        self.assertTrue(body["validateOnly"])
        self.assertEqual(body["destinations"][0]["productDestinationId"], "555")
        first, second = body["events"]
        self.assertEqual(first["transactionId"], "bitrix24-deal-11")
        self.assertEqual(
            first["eventTimestamp"], f"{days_ago(3)}T19:40:05+03:00"
        )
        self.assertEqual(first["conversionValue"], 1800.0)
        self.assertEqual(first["currency"], "USD")
        self.assertEqual(
            first["userData"]["userIdentifiers"],
            [
                {"emailAddress": sha256("anaperez@gmail.com")},
                {"phoneNumber": sha256("+56912345678")},
            ],
        )
        self.assertEqual(second["transactionId"], "bitrix24-deal-12")
        self.assertNotIn("conversionValue", second)
        self.assertEqual(
            second["userData"]["userIdentifiers"],
            [{"phoneNumber": sha256("+56987654321")}],
        )

        self.assertEqual(result["request_id"], "req-7")
        self.assertEqual(
            result["crm"],
            {
                "pipelines": [73, 91],
                "period": {
                    "start": days_ago(7),
                    "end": datetime.date.today().isoformat(),
                },
                "won_deals": 4,
                "conversions": 2,
                "with_email": 1,
                "with_phone": 2,
                "skipped_without_contact_data": 2,
                "skipped_deal_ids": ["13", "14"],
            },
        )
        text = json.dumps(result) + "".join(logs.output)
        for secret in ("anaperez", "Ana.Perez", "1234", "8765"):
            self.assertNotIn(secret, text)

    def test_can_leave_out_the_value(self):
        self.sync(include_value=False, validate_only=False)
        body = self.sent_json()
        self.assertFalse(body["validateOnly"])
        self.assertNotIn("conversionValue", body["events"][0])

    def test_nothing_to_upload(self):
        self.list_all.side_effect = lambda method, params, **kw: []
        result = self.sync()
        self.assertFalse(result["applied"])
        self.assertEqual(result["crm"]["won_deals"], 0)
        self.call.assert_not_called()

    def test_validates_the_inputs(self):
        with self.assertRaisesRegex(ToolError, "pipeline_ids"):
            self.sync(pipeline_ids=[])
        with self.assertRaisesRegex(ToolError, "90 days"):
            self.sync(start_date=days_ago(120))
        self.list_all.assert_not_called()


class TestCustomerMatch(CrmToolTestCase):

    def list_row(self, list_type="CRM_BASED"):
        return self.make_row(
            {
                "user_list.name": "Matriculados Carver",
                "user_list.type_": self.client.enums.UserListTypeEnum[
                    list_type
                ],
            }
        )

    def sync(self, **kwargs):
        args = {
            "customer_id": "3938508333",
            "user_list_id": "777",
            "pipeline_ids": [73],
            "accept_customer_match_terms": True,
            "default_phone_country_code": "56",
        }
        args.update(kwargs)
        return crm.sync_crm_audience(**args)

    def test_creates_the_list(self):
        result = crm.create_customer_match_list(
            customer_id="123",
            name=" Matriculados  Carver ",
            membership_days=365,
        )
        _, operations = self.operations()
        (user_list,) = self.created(operations, "user_list_operation")
        self.assertEqual(user_list.name, "Matriculados Carver")
        self.assertEqual(user_list.membership_life_span, 365)
        self.assertEqual(user_list.membership_status.name, "OPEN")
        self.assertEqual(
            user_list.crm_based_user_list.upload_key_type.name, "CONTACT_INFO"
        )
        self.assertFalse(result["applied"])
        with self.assertRaises(ToolError):
            crm.create_customer_match_list(
                customer_id="123", name="x", membership_days=541
            )

    def test_requires_accepting_the_terms(self):
        with self.assertRaisesRegex(ToolError, "Customer Match terms"):
            self.sync(accept_customer_match_terms=False)
        self.call.assert_not_called()

    def test_only_customer_match_lists(self):
        self.set_rows(self.list_row("RULE_BASED"))
        with self.assertRaisesRegex(ToolError, "RULE_BASED"):
            self.sync()

    def test_adds_hashed_contacts(self):
        self.set_rows(self.list_row())
        with self.assertLogs("ads_mcp.utils", "INFO") as logs:
            result = self.sync(since_date=days_ago(30))

        deal_filter = self.list_all.call_args_list[0].args[1]["filter"]
        self.assertEqual(deal_filter[">=MOVED_TIME"], days_ago(30))
        body = self.sent_json()
        self.assertEqual(body["destinations"][0]["productDestinationId"], "777")
        self.assertEqual(
            body["termsOfService"],
            {"customerMatchTermsOfServiceStatus": "ACCEPTED"},
        )
        self.assertEqual(
            body["audienceMembers"],
            [
                {
                    "userData": {
                        "userIdentifiers": [
                            {"emailAddress": sha256("anaperez@gmail.com")},
                            {"phoneNumber": sha256("+56912345678")},
                        ]
                    }
                },
                {
                    "userData": {
                        "userIdentifiers": [
                            {"phoneNumber": sha256("+56987654321")}
                        ]
                    }
                },
            ],
        )
        self.assertEqual(result["request_ids"], ["req-7"])
        self.assertEqual(result["crm"]["members"], 2)
        self.assertEqual(result["crm"]["contacts_without_data"], 1)
        text = json.dumps(result) + "".join(logs.output)
        self.assertNotIn("anaperez", text)

    def test_all_deals_use_the_creation_date(self):
        self.set_rows(self.list_row())
        self.sync(only_won_deals=False, since_date=days_ago(30))
        deal_filter = self.list_all.call_args_list[0].args[1]["filter"]
        self.assertNotIn("STAGE_SEMANTIC_ID", deal_filter)
        self.assertEqual(deal_filter[">=DATE_CREATE"], days_ago(30))


class TestListCrmPipelines(unittest.TestCase):

    def test_lists_pipelines_with_their_won_stage(self):
        categories = {
            "result": {
                "categories": [
                    {"id": 0, "name": "General"},
                    {"id": 73, "name": "Carver"},
                ]
            }
        }
        with patch("ads_mcp.bitrix24.list_all", side_effect=fake_bitrix):
            with patch("ads_mcp.bitrix24.call", return_value=categories):
                result = crm.list_crm_pipelines()
        self.assertEqual(
            result["pipelines"],
            [
                {"pipeline_id": 0, "name": "General", "won_stage": "Ganado"},
                {
                    "pipeline_id": 73,
                    "name": "Carver",
                    "won_stage": "Matriculado",
                },
            ],
        )


class TestCrmToolsMounting(unittest.IsolatedAsyncioTestCase):

    async def test_only_in_write_config(self):
        names = {
            "mutate_list_crm_pipelines",
            "mutate_sync_crm_conversions",
            "mutate_create_customer_match_list",
            "mutate_sync_crm_audience",
        }
        read = {t.name for t in await list_tools("tools_config.yaml")}
        write = {t.name: t for t in await list_tools("tools_config_write.yaml")}
        self.assertFalse(names & read)
        self.assertTrue(names <= set(write))
        self.assertTrue(
            write["mutate_list_crm_pipelines"].annotations.read_only_hint
        )


if __name__ == "__main__":
    unittest.main()
