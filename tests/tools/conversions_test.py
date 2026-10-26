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

"""Test cases for uploading offline conversions with the Data Manager API."""

import hashlib
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError

from ads_mcp import data_manager
from ads_mcp.tools import conversions
from ads_mcp.tools.conversions import OfflineConversion
from tests.tools.create_test import CreateToolTestCase
from tests.tools.mutate_test import list_tools


def sha256(value):
    return hashlib.sha256(value.encode()).hexdigest()


class TestNormalization(unittest.TestCase):

    def test_emails(self):
        self.assertEqual(
            conversions._normalize_email(" Diego.Perez+postgrado@Gmail.com "),
            "diegoperez@gmail.com",
        )
        self.assertEqual(
            conversions._normalize_email("ana.maria+x@uautonoma.cl"),
            "ana.maria+x@uautonoma.cl",
        )
        self.assertEqual(
            conversions._normalize_email("j o@googlemail.com"),
            "jo@googlemail.com",
        )
        for invalid in ("sin-arroba", "@gmail.com", "ana@local"):
            with self.subTest(invalid):
                with self.assertRaises(ToolError):
                    conversions._normalize_email(invalid)

    def test_phones(self):
        self.assertEqual(
            conversions._normalize_phone("+56 9 (1234) 56-78"), "+56912345678"
        )
        for invalid in ("912345678", "+0123456789", "+56abc"):
            with self.subTest(invalid):
                with self.assertRaises(ToolError):
                    conversions._normalize_phone(invalid)

    def test_timestamps(self):
        zone = "America/Santiago"
        cases = {
            "2026-10-20 10:00:00": "2026-10-20T10:00:00-03:00",
            "2026-10-20T10:00": "2026-10-20T10:00:00-03:00",
            "2026-07-20 10:00:00": "2026-07-20T10:00:00-04:00",
            "2026-10-20 10:00:00Z": "2026-10-20T10:00:00+00:00",
            "2026-10-20 10:00:00 +0200": "2026-10-20T10:00:00+02:00",
        }
        for value, expected in cases.items():
            with self.subTest(value):
                self.assertEqual(
                    conversions._event_timestamp(value, zone), expected
                )
        for invalid in (
            "20-10-2026 10:00",
            "2026-13-01 10:00",
            "2999-01-01 00:00",
        ):
            with self.subTest(invalid):
                with self.assertRaises(ToolError):
                    conversions._event_timestamp(invalid, zone)


class TestUploadOfflineConversions(CreateToolTestCase):

    def setUp(self):
        super().setUp()
        self.set_rows(self.action_row())
        patchers = [
            patch.dict(os.environ, {data_manager.ENABLE_ENV_VAR: "true"}),
            patch(
                "ads_mcp.data_manager.call",
                return_value={"requestId": "req-42"},
            ),
        ]
        self.call = None
        for patcher in patchers:
            started = patcher.start()
            if isinstance(started, MagicMock):
                self.call = started
            self.addCleanup(patcher.stop)

    def action_row(self, action_type="UPLOAD_CLICKS"):
        return self.make_row(
            {
                "customer.currency_code": "CLP",
                "customer.time_zone": "America/Santiago",
                "conversion_action.name": "Matrícula",
                "conversion_action.type_": self.client.enums.ConversionActionTypeEnum[
                    action_type
                ],
            }
        )

    def upload(self, items, **kwargs):
        return conversions.upload_offline_conversions(
            customer_id="123",
            conversion_action_id="789",
            conversions=[OfflineConversion(**item) for item in items],
            **kwargs,
        )

    def test_sends_events_to_the_data_manager_api(self):
        with self.assertLogs("ads_mcp.utils", "INFO") as logs:
            result = self.upload(
                [
                    {
                        "gclid": " Cj0KCQ ",
                        "conversion_date_time": "2026-10-20 10:00:00",
                        "value": 1500000,
                        "order_id": "BITRIX-1",
                    },
                    {
                        "email": "Diego.Perez@Gmail.com",
                        "phone": "+56 9 1234 5678",
                        "conversion_date_time": "2026-10-21 11:30:00-03:00",
                        "value": 100,
                        "currency": "usd",
                    },
                ],
                login_customer_id="456",
            )

        method, path = self.call.call_args.args
        body = self.call.call_args.kwargs["json"]
        self.assertEqual((method, path), ("POST", "events:ingest"))
        self.assertEqual(
            body["destinations"],
            [
                {
                    "operatingAccount": {
                        "accountType": "GOOGLE_ADS",
                        "accountId": "123",
                    },
                    "loginAccount": {
                        "accountType": "GOOGLE_ADS",
                        "accountId": "456",
                    },
                    "productDestinationId": "789",
                }
            ],
        )
        self.assertEqual(body["encoding"], "HEX")
        self.assertTrue(body["validateOnly"])
        first, second = body["events"]
        self.assertEqual(
            first,
            {
                "eventTimestamp": "2026-10-20T10:00:00-03:00",
                "eventSource": "WEB",
                "adIdentifiers": {"gclid": "Cj0KCQ"},
                "conversionValue": 1500000,
                "currency": "CLP",
                "transactionId": "BITRIX-1",
            },
        )
        self.assertEqual(
            second["userData"]["userIdentifiers"],
            [
                {"emailAddress": sha256("diegoperez@gmail.com")},
                {"phoneNumber": sha256("+56912345678")},
            ],
        )
        self.assertEqual(second["currency"], "USD")
        self.assertNotIn("adIdentifiers", second)

        self.assertEqual(result["request_id"], "req-42")
        self.assertEqual(
            result["changes"][0]["identified_by"],
            {"gclid": 1, "email": 1, "phone": 1},
        )
        # Personal data never appears in the response or the logs.
        for text in (json.dumps(result), "\n".join(logs.output)):
            self.assertNotIn("Gmail.com", text)
            self.assertNotIn("1234", text)
            self.assertNotIn(sha256("diegoperez@gmail.com"), text)

    def test_applied_upload_points_to_the_status_tool(self):
        result = self.upload(
            [{"gclid": "abc", "conversion_date_time": "2026-10-20 10:00"}],
            validate_only=False,
        )

        self.assertFalse(self.call.call_args.kwargs["json"]["validateOnly"])
        self.assertTrue(result["applied"])
        self.assertIn("get_conversion_upload_status", result["next_step"])
        # Without a manager account, the customer is its own login account.
        destination = self.call.call_args.kwargs["json"]["destinations"][0]
        self.assertEqual(destination["loginAccount"]["accountId"], "123")

    def test_reports_every_invalid_conversion_without_sending(self):
        with self.assertRaises(ToolError) as context:
            self.upload(
                [
                    {"conversion_date_time": "2026-10-20 10:00"},
                    {
                        "email": "malo",
                        "conversion_date_time": "2026-10-20 10:00",
                    },
                    {"gclid": "abc", "conversion_date_time": "ayer"},
                    {
                        "gclid": "abc",
                        "conversion_date_time": "2026-10-20 10:00",
                        "value": -1,
                    },
                ]
            )

        message = str(context.exception)
        self.assertIn("Found 4 problems", message)
        for index in range(4):
            self.assertIn(f"conversion {index}:", message)
        self.call.assert_not_called()

    def test_requires_an_upload_clicks_action(self):
        self.set_rows(self.action_row("WEBPAGE"))

        with self.assertRaisesRegex(ToolError, "UPLOAD_CLICKS"):
            self.upload(
                [{"gclid": "abc", "conversion_date_time": "2026-10-20 10:00"}]
            )
        self.call.assert_not_called()

    def test_requires_data_manager_on_the_server(self):
        with patch.dict(os.environ, {data_manager.ENABLE_ENV_VAR: ""}):
            with self.assertRaisesRegex(ToolError, data_manager.ENABLE_ENV_VAR):
                self.upload(
                    [
                        {
                            "gclid": "abc",
                            "conversion_date_time": "2026-10-20 10:00",
                        }
                    ]
                )
        self.call.assert_not_called()
        self.services["GoogleAdsService"].search.assert_not_called()


class TestConversionUploadStatus(unittest.TestCase):

    @patch.dict(os.environ, {data_manager.ENABLE_ENV_VAR: "true"})
    @patch("ads_mcp.data_manager.call")
    def test_summarizes_the_status(self, call):
        call.return_value = {
            "requestStatusPerDestination": [
                {
                    "destination": {"productDestinationId": "789"},
                    "requestStatus": "PARTIAL_SUCCESS",
                    "eventsIngestionStatus": {"recordCount": "10"},
                    "errorInfo": {
                        "errorCounts": [
                            {"recordCount": "2", "reason": "EXPIRED_EVENT"}
                        ]
                    },
                }
            ]
        }

        result = conversions.get_conversion_upload_status(request_id=" req-42 ")

        call.assert_called_once_with(
            "GET", "requestStatus:retrieve", params={"requestId": "req-42"}
        )
        self.assertEqual(
            result["destinations"],
            [
                {
                    "conversion_action_id": "789",
                    "status": "PARTIAL_SUCCESS",
                    "conversions_sent": 10,
                    "errors": {"EXPIRED_EVENT": 2},
                    "warnings": {},
                }
            ],
        )

    def test_rejects_invalid_request_ids(self):
        with self.assertRaises(ToolError):
            conversions.get_conversion_upload_status(request_id="a b'")


class TestDataManagerClient(unittest.TestCase):

    def response(self, status, body):
        response = MagicMock(status_code=status, content=b"x")
        response.json.return_value = body
        return response

    @patch("ads_mcp.data_manager._credentials")
    @patch("google.auth.transport.requests.AuthorizedSession")
    def test_errors_include_field_violations_and_a_permission_hint(
        self, session, credentials
    ):
        session.return_value.request.return_value = self.response(
            403,
            {
                "error": {
                    "status": "PERMISSION_DENIED",
                    "message": "Request had insufficient authentication scopes.",
                    "details": [
                        {
                            "fieldViolations": [
                                {
                                    "field": "events[0].eventTimestamp",
                                    "description": "Too old.",
                                }
                            ]
                        },
                        {"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"},
                    ],
                }
            },
        )

        with self.assertRaises(ToolError) as context:
            data_manager.call("POST", "events:ingest", json={})

        message = str(context.exception)
        self.assertIn("403 (PERMISSION_DENIED)", message)
        self.assertIn("events[0].eventTimestamp: Too old.", message)
        self.assertIn("ACCESS_TOKEN_SCOPE_INSUFFICIENT", message)
        self.assertIn("sign in again", message)
        request = session.return_value.request
        self.assertEqual(
            request.call_args.args,
            ("POST", "https://datamanager.googleapis.com/v1/events:ingest"),
        )

    @patch("ads_mcp.data_manager._credentials")
    @patch("google.auth.transport.requests.AuthorizedSession")
    def test_returns_the_json_response(self, session, credentials):
        session.return_value.request.return_value = self.response(
            200, {"requestId": "req-1"}
        )

        self.assertEqual(
            data_manager.call("GET", "requestStatus:retrieve"),
            {"requestId": "req-1"},
        )

    def test_enabled_flag(self):
        for value, expected in (
            ("true", True),
            ("1", True),
            ("", False),
            ("no", False),
        ):
            with self.subTest(value):
                with patch.dict(
                    os.environ, {data_manager.ENABLE_ENV_VAR: value}
                ):
                    self.assertEqual(data_manager.enabled(), expected)


class TestConversionToolsMounting(unittest.IsolatedAsyncioTestCase):

    async def test_conversion_tools_only_in_write_config(self):
        default = [t.name for t in await list_tools("tools_config.yaml")]
        write = [t.name for t in await list_tools("tools_config_write.yaml")]
        for name in (
            "mutate_upload_offline_conversions",
            "mutate_get_conversion_upload_status",
        ):
            with self.subTest(name):
                self.assertNotIn(name, default)
                self.assertIn(name, write)


if __name__ == "__main__":
    unittest.main()
