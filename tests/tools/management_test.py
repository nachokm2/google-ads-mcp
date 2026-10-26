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

"""Test cases for the asset, conversion action and management tools."""

import unittest
from unittest.mock import MagicMock

from fastmcp.exceptions import ToolError
from google.protobuf import any_pb2
from google.rpc import status_pb2

from google.ads.googleads.v26.errors.types.string_length_error import (
    StringLengthErrorEnum,
)

from ads_mcp import errors
from ads_mcp.tools import conversions, create_assets, manage
from ads_mcp.tools.create_assets import Sitelink
from tests.tools.create_test import CreateToolTestCase
from tests.tools.mutate_test import list_tools

NEW_WRITE_TOOLS = [
    "mutate_add_callouts",
    "mutate_add_sitelinks",
    "mutate_add_structured_snippet",
    "mutate_apply_recommendations",
    "mutate_create_offline_conversion_action",
    "mutate_dismiss_recommendations",
    "mutate_remove_ad_group",
    "mutate_remove_campaign",
    "mutate_update_campaign",
]


class ManagementTestCase(CreateToolTestCase):

    def campaign_row(self, **extra):
        return self.make_row(
            {
                "campaign.resource_name": "customers/123/campaigns/456",
                "campaign.name": "MBA 2027",
                "campaign.status": self.client.enums.CampaignStatusEnum.ENABLED,
                **extra,
            }
        )

    def partial_failure(self, failed_index, message):
        """Builds the google.rpc.Status of a partial failure."""
        error = self.client.get_type("GoogleAdsError")
        error.message = message
        element = type(error.location).FieldPathElement(
            field_name="operations", index=failed_index
        )
        error.location.field_path_elements.append(element)
        failure = self.client.get_type("GoogleAdsFailure")
        failure.errors.append(error)
        detail = any_pb2.Any(
            type_url="type.googleapis.com/google.ads.googleads.v26.errors.GoogleAdsFailure",
            value=type(failure).serialize(failure),
        )
        return status_pb2.Status(code=3, details=[detail])


class TestSearchAssets(ManagementTestCase):

    def test_sitelinks_are_created_and_linked_to_the_campaign(self):
        self.set_rows(self.campaign_row())

        result = create_assets.add_sitelinks(
            customer_id="123",
            campaign_id="456",
            sitelinks=[
                Sitelink(
                    text="Magíster Educación",
                    final_url="https://www.uautonoma.cl/educacion",
                    description1="Modalidad online",
                    description2="Postula hoy",
                ),
                Sitelink(
                    text="Diplomados", final_url="https://www.uautonoma.cl"
                ),
            ],
        )

        request, operations = self.operations()
        self.assertTrue(request.validate_only)
        assets = self.created(operations, "asset_operation")
        self.assertEqual(
            [a.sitelink_asset.link_text for a in assets],
            ["Magíster Educación", "Diplomados"],
        )
        self.assertEqual(assets[0].sitelink_asset.description2, "Postula hoy")
        self.assertEqual(
            list(assets[0].final_urls), ["https://www.uautonoma.cl/educacion"]
        )
        links = self.created(operations, "campaign_asset_operation")
        self.assertEqual(
            [(l.campaign, l.asset, l.field_type.name) for l in links],
            [
                (
                    "customers/123/campaigns/456",
                    assets[0].resource_name,
                    "SITELINK",
                ),
                (
                    "customers/123/campaigns/456",
                    assets[1].resource_name,
                    "SITELINK",
                ),
            ],
        )
        self.assertEqual(
            result["changes"][0]["linked_to"], "campaign 'MBA 2027'"
        )

    def test_without_campaign_the_assets_go_to_the_account(self):
        create_assets.add_callouts(
            customer_id="123", callouts=["100% online", "Acreditado"]
        )

        _, operations = self.operations()
        links = self.created(operations, "customer_asset_operation")
        self.assertEqual(
            [l.field_type.name for l in links], ["CALLOUT", "CALLOUT"]
        )
        self.assertEqual(
            self.created(operations, "campaign_asset_operation"), []
        )
        self.services["GoogleAdsService"].search.assert_not_called()

    def test_structured_snippet(self):
        self.set_rows(self.campaign_row())

        create_assets.add_structured_snippet(
            customer_id="123",
            campaign_id="456",
            header="Degree programs",
            values=["Magíster Educación", "MBA", "Diplomados"],
        )

        _, operations = self.operations()
        (asset,) = self.created(operations, "asset_operation")
        self.assertEqual(
            asset.structured_snippet_asset.header, "Degree programs"
        )
        self.assertEqual(len(asset.structured_snippet_asset.values), 3)
        (link,) = self.created(operations, "campaign_asset_operation")
        self.assertEqual(link.field_type.name, "STRUCTURED_SNIPPET")

    def test_rejects_invalid_assets(self):
        cases = [
            (
                create_assets.add_sitelinks,
                {
                    "sitelinks": [
                        Sitelink(
                            text="x" * 26, final_url="https://www.uautonoma.cl"
                        )
                    ]
                },
            ),
            (
                create_assets.add_sitelinks,
                {
                    "sitelinks": [
                        Sitelink(
                            text="MBA",
                            final_url="https://www.uautonoma.cl",
                            description1="Solo una línea",
                        )
                    ]
                },
            ),
            (create_assets.add_callouts, {"callouts": ["x" * 26]}),
            (
                create_assets.add_structured_snippet,
                {"header": "Courses", "values": ["MBA", "Diplomados"]},
            ),
        ]
        for tool, kwargs in cases:
            with self.subTest(tool=tool.__name__, kwargs=kwargs):
                with self.assertRaises(ToolError):
                    tool(customer_id="123", **kwargs)
        self.services["GoogleAdsService"].mutate.assert_not_called()


class TestCreateOfflineConversionAction(ManagementTestCase):

    def test_creates_an_upload_clicks_action_in_the_account_currency(self):
        self.set_rows(self.make_row({"customer.currency_code": "CLP"}))
        response = self.client.get_type("MutateGoogleAdsResponse")
        operation_response = self.client.get_type("MutateOperationResponse")
        operation_response.conversion_action_result.resource_name = (
            "customers/123/conversionActions/789"
        )
        response.mutate_operation_responses.append(operation_response)
        self.services["GoogleAdsService"].mutate.return_value = response

        result = conversions.create_offline_conversion_action(
            customer_id="123",
            name="Matrícula",
            default_value=1500000,
            validate_only=False,
        )

        _, operations = self.operations()
        (action,) = self.created(operations, "conversion_action_operation")
        self.assertEqual(action.type_.name, "UPLOAD_CLICKS")
        self.assertEqual(action.category.name, "CONVERTED_LEAD")
        self.assertEqual(action.counting_type.name, "ONE_PER_CLICK")
        self.assertEqual(action.click_through_lookback_window_days, 90)
        self.assertTrue(action.primary_for_goal)
        self.assertEqual(action.value_settings.default_currency_code, "CLP")
        self.assertEqual(action.value_settings.default_value, 1500000)
        self.assertEqual(result["conversion_action_id"], "789")

    def test_rejects_invalid_settings(self):
        for kwargs in (
            {"name": " "},
            {"name": "M", "click_through_lookback_window_days": 91},
            {"name": "M", "default_value": -1},
            {"name": "M", "always_use_default_value": True},
        ):
            with self.subTest(kwargs):
                with self.assertRaises(ToolError):
                    conversions.create_offline_conversion_action(
                        customer_id="123", **kwargs
                    )


class TestRecommendations(ManagementTestCase):

    def set_recommendations(self, *ids):
        rows = [
            self.make_row(
                {
                    "recommendation.resource_name": f"customers/123/recommendations/{rid}",
                    "recommendation.type_": self.client.enums.RecommendationTypeEnum.CALLOUT_ASSET,
                    "recommendation.campaign": "customers/123/campaigns/456",
                }
            )
            for rid in ids
        ]
        self.services["GoogleAdsService"].search.side_effect = [
            rows,
            [self.campaign_row()],
        ]

    def test_validate_only_lists_without_calling_the_api(self):
        self.set_recommendations("AbC=", "XyZ")

        result = manage.apply_recommendations(
            customer_id="123",
            recommendation_ids=["AbC=", "customers/123/recommendations/XyZ"],
        )

        self.services[
            "RecommendationService"
        ].apply_recommendation.assert_not_called()
        self.assertFalse(result["applied"])
        self.assertIn("cannot validate", result["message"])
        self.assertEqual(
            [
                (c["recommendation_id"], c["type"], c["campaign"])
                for c in result["changes"]
            ],
            [
                ("AbC=", "CALLOUT_ASSET", "MBA 2027"),
                ("XyZ", "CALLOUT_ASSET", "MBA 2027"),
            ],
        )

    def test_apply_reports_partial_failures(self):
        self.set_recommendations("AbC=", "XyZ")
        self.services[
            "RecommendationService"
        ].apply_recommendation.return_value = MagicMock(
            partial_failure_error=self.partial_failure(
                1, "Recommendation expired"
            )
        )

        result = manage.apply_recommendations(
            customer_id="123",
            recommendation_ids=["AbC=", "XyZ"],
            validate_only=False,
        )

        request = self.sent_request(
            "RecommendationService", "apply_recommendation"
        )
        self.assertTrue(request.partial_failure)
        self.assertEqual(
            [op.resource_name for op in request.operations],
            [
                "customers/123/recommendations/AbC=",
                "customers/123/recommendations/XyZ",
            ],
        )
        first, second = result["changes"]
        self.assertEqual(first["status"], "OK")
        self.assertEqual(second["status"], "FAILED")
        self.assertIn("Recommendation expired", second["errors"][0])

    def test_dismiss(self):
        self.set_recommendations("AbC=")
        self.services[
            "RecommendationService"
        ].dismiss_recommendation.return_value = MagicMock(
            partial_failure_error=status_pb2.Status()
        )

        result = manage.dismiss_recommendations(
            customer_id="123", recommendation_ids=["AbC="], validate_only=False
        )

        request = self.sent_request(
            "RecommendationService", "dismiss_recommendation"
        )
        self.assertEqual(
            [op.resource_name for op in request.operations],
            ["customers/123/recommendations/AbC="],
        )
        self.assertEqual(result["changes"][0]["status"], "OK")

    def test_inactive_or_invalid_ids(self):
        self.services["GoogleAdsService"].search.return_value = []
        with self.assertRaisesRegex(ToolError, "not active"):
            manage.apply_recommendations(
                customer_id="123", recommendation_ids=["Gone"]
            )
        for ids in ([], ["bad id'"], ["x"] * 101):
            with self.subTest(ids=ids[:2]):
                with self.assertRaises(ToolError):
                    manage.apply_recommendations(
                        customer_id="123", recommendation_ids=ids
                    )


class TestEditAndRemove(ManagementTestCase):

    def test_update_campaign_name_and_dates(self):
        self.set_rows(
            self.campaign_row(
                **{"campaign.end_date_time": "2026-12-31 23:59:59"}
            )
        )

        result = manage.update_campaign(
            customer_id="123",
            campaign_id="456",
            name="  MBA  2028 ",
            start_date="2026-11-01",
            remove_end_date=True,
        )

        operation = self.sent_request(
            "CampaignService", "mutate_campaigns"
        ).operations[0]
        self.assertEqual(operation.update.name, "MBA 2028")
        self.assertEqual(
            operation.update.start_date_time, "2026-11-01 00:00:00"
        )
        # The end date is cleared: in the mask but without a value.
        self.assertEqual(
            list(operation.update_mask.paths),
            ["name", "start_date_time", "end_date_time"],
        )
        self.assertEqual(operation.update.end_date_time, "")
        change = result["changes"][0]
        self.assertEqual(change["name"], {"from": "MBA 2027", "to": "MBA 2028"})
        self.assertEqual(
            change["end_date"], {"from": "2026-12-31 23:59:59", "to": None}
        )

    def test_update_campaign_validation(self):
        for kwargs in (
            {},
            {"end_date": "2026-12-31", "remove_end_date": True},
            {"start_date": "2026-12-01", "end_date": "2026-11-01"},
            {"name": "  "},
        ):
            with self.subTest(kwargs):
                with self.assertRaises(ToolError):
                    manage.update_campaign(
                        customer_id="123", campaign_id="456", **kwargs
                    )

    def test_remove_campaign_needs_the_exact_name(self):
        self.set_rows(self.campaign_row())

        with self.assertRaisesRegex(ToolError, "confirm_name does not match"):
            manage.remove_campaign(
                customer_id="123", campaign_id="456", confirm_name="mba 2027"
            )
        self.services["CampaignService"].mutate_campaigns.assert_not_called()

        result = manage.remove_campaign(
            customer_id="123", campaign_id="456", confirm_name=" MBA  2027"
        )

        request = self.sent_request("CampaignService", "mutate_campaigns")
        self.assertTrue(request.validate_only)
        self.assertEqual(
            request.operations[0].remove, "customers/123/campaigns/456"
        )
        self.assertEqual(
            result["changes"][0]["status"], {"from": "ENABLED", "to": "REMOVED"}
        )
        self.assertIn("permanent", result["warnings"][0])

    def test_remove_ad_group(self):
        self.set_rows(
            self.make_row(
                {
                    "ad_group.resource_name": "customers/123/adGroups/789",
                    "ad_group.name": "Brand",
                    "ad_group.status": self.client.enums.AdGroupStatusEnum.PAUSED,
                    "campaign.name": "MBA 2027",
                }
            )
        )

        manage.remove_ad_group(
            customer_id="123",
            ad_group_id="789",
            confirm_name="Brand",
            validate_only=False,
        )

        request = self.sent_request("AdGroupService", "mutate_ad_groups")
        self.assertFalse(request.validate_only)
        self.assertEqual(
            request.operations[0].remove, "customers/123/adGroups/789"
        )


class TestErrorFormatting(ManagementTestCase):

    def test_describe_error_includes_code_and_field_path(self):
        error = self.client.get_type("GoogleAdsError")
        error.message = "Too long."
        error.error_code.string_length_error = (
            StringLengthErrorEnum.StringLengthError.TOO_LONG
        )
        error.location.field_path_elements.append(
            type(error.location).FieldPathElement(
                field_name="operations", index=2
            )
        )
        error.location.field_path_elements.append(
            type(error.location).FieldPathElement(field_name="headline")
        )

        self.assertEqual(
            errors.describe_error(error),
            "Too long. (string_length_error: TOO_LONG) at operations[2].headline",
        )

    def test_partial_failure_errors_by_index(self):
        failures = errors.partial_failure_errors(
            self.client, self.partial_failure(3, "Bad gclid")
        )
        self.assertEqual(list(failures), [3])
        self.assertIn("Bad gclid", failures[3][0])
        self.assertEqual(
            errors.partial_failure_errors(self.client, status_pb2.Status()), {}
        )

    def test_transport_error_when_no_failure_was_parsed(self):
        ex = MagicMock(request_id="req-1", failure=None)
        ex.error.details.return_value = "Internal error encountered."
        ex.error.code.return_value.name = "INTERNAL"

        self.assertEqual(
            errors.format_google_ads_exception(ex),
            "Request ID: req-1\nGoogle Ads API Error: Internal error encountered. "
            "(grpc status: INTERNAL)",
        )


class TestManagementToolsMounting(unittest.IsolatedAsyncioTestCase):

    async def test_write_tools_only_in_write_config(self):
        default = [t.name for t in await list_tools("tools_config.yaml")]
        write = {t.name: t for t in await list_tools("tools_config_write.yaml")}
        for name in NEW_WRITE_TOOLS:
            with self.subTest(name):
                self.assertNotIn(name, default)
                self.assertIn(name, write)
                self.assertFalse(write[name].annotations.read_only_hint)


if __name__ == "__main__":
    unittest.main()
