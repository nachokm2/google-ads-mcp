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

"""Test cases for the tools that create Search campaigns."""

import os
import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp.tools import create
from tests.tools.mutate_test import MutateToolTestCase, list_tools

CREATE_TOOL_NAMES = [
    "mutate_add_keywords",
    "mutate_create_ad_group",
    "mutate_create_responsive_search_ad",
    "mutate_create_search_campaign",
]

HEADLINES = ["Postgrados UA", "Magíster Online", "Postula Hoy"]
DESCRIPTIONS = [
    "Programas de postgrado para profesionales.",
    "Conoce nuestros magíster y diplomados.",
]


def _bidding(campaign):
    return type(campaign).pb(campaign).WhichOneof("campaign_bidding_strategy")


class CreateToolTestCase(MutateToolTestCase):

    def operations(self):
        request = self.sent_request("GoogleAdsService", "mutate")
        return request, list(request.mutate_operations)

    def created(self, operations, kind):
        """Returns the resources created by operations of the given kind."""
        return [
            getattr(op, kind).create
            for op in operations
            if type(op).pb(op).WhichOneof("operation") == kind
        ]


class TestCreateSearchCampaign(CreateToolTestCase):

    def setUp(self):
        super().setUp()
        self.set_rows(self.make_row({"customer.currency_code": "CLP"}))

    def create(self, **overrides):
        args = {
            "customer_id": "123",
            "campaign_name": "Magíster  Educación",
            "daily_budget": 5000,
            "final_url": "https://www.uautonoma.cl",
            "headlines": HEADLINES,
            "descriptions": DESCRIPTIONS,
            "keywords": ["magister educacion", "postgrado online"],
        }
        args.update(overrides)
        return create.create_search_campaign(**args)

    def test_builds_complete_campaign_in_one_request(self):
        result = self.create()

        request, operations = self.operations()
        self.assertTrue(request.validate_only)
        self.assertEqual(request.customer_id, "123")

        (budget,) = self.created(operations, "campaign_budget_operation")
        self.assertEqual(
            budget.resource_name, "customers/123/campaignBudgets/-1"
        )
        self.assertEqual(budget.amount_micros, 5_000_000_000)
        self.assertFalse(budget.explicitly_shared)
        self.assertEqual(budget.delivery_method.name, "STANDARD")

        (campaign,) = self.created(operations, "campaign_operation")
        self.assertEqual(campaign.resource_name, "customers/123/campaigns/-2")
        self.assertEqual(campaign.name, "Magíster Educación")
        self.assertEqual(campaign.status.name, "ENABLED")
        self.assertEqual(campaign.advertising_channel_type.name, "SEARCH")
        self.assertEqual(
            campaign.campaign_budget, "customers/123/campaignBudgets/-1"
        )
        self.assertEqual(
            campaign.contains_eu_political_advertising.name,
            "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        )
        self.assertTrue(campaign.network_settings.target_google_search)
        self.assertFalse(campaign.network_settings.target_search_network)
        self.assertFalse(campaign.network_settings.target_content_network)
        self.assertEqual(
            campaign.geo_target_type_setting.positive_geo_target_type.name,
            "PRESENCE",
        )
        self.assertEqual(_bidding(campaign), "target_spend")

        criteria = self.created(operations, "campaign_criterion_operation")
        self.assertEqual(
            [c.location.geo_target_constant for c in criteria[:1]],
            ["geoTargetConstants/2152"],
        )
        self.assertEqual(
            criteria[1].language.language_constant, "languageConstants/1003"
        )

        (ad_group,) = self.created(operations, "ad_group_operation")
        self.assertEqual(ad_group.campaign, "customers/123/campaigns/-2")
        self.assertEqual(ad_group.type_.name, "SEARCH_STANDARD")
        self.assertEqual(ad_group.name, "Magíster Educación")

        keywords = self.created(operations, "ad_group_criterion_operation")
        self.assertEqual(
            [k.keyword.text for k in keywords],
            ["magister educacion", "postgrado online"],
        )
        self.assertEqual(keywords[0].keyword.match_type.name, "PHRASE")
        self.assertEqual(keywords[0].ad_group, "customers/123/adGroups/-3")

        (ad_group_ad,) = self.created(operations, "ad_group_ad_operation")
        ad = ad_group_ad.ad
        self.assertEqual(list(ad.final_urls), ["https://www.uautonoma.cl"])
        self.assertEqual(
            [h.text for h in ad.responsive_search_ad.headlines], HEADLINES
        )
        self.assertEqual(
            [d.text for d in ad.responsive_search_ad.descriptions],
            DESCRIPTIONS,
        )

        # Resources must be created before the ones that reference them.
        kinds = [type(op).pb(op).WhichOneof("operation") for op in operations]
        self.assertEqual(kinds[0], "campaign_budget_operation")
        self.assertEqual(kinds[1], "campaign_operation")
        self.assertLess(
            kinds.index("ad_group_operation"),
            kinds.index("ad_group_criterion_operation"),
        )

        self.assertFalse(result["applied"])
        self.assertEqual(result["changes"][0]["currency"], "CLP")
        self.assertIn("ENABLED", result["warnings"][0])

    def test_returns_created_resources_when_applied(self):
        response = self.client.get_type("MutateGoogleAdsResponse")
        operation_response = self.client.get_type("MutateOperationResponse")
        operation_response.campaign_result.resource_name = (
            "customers/123/campaigns/999"
        )
        response.mutate_operation_responses.append(operation_response)
        self.services["GoogleAdsService"].mutate.return_value = response

        result = self.create(validate_only=False, status="PAUSED")

        request, operations = self.operations()
        self.assertFalse(request.validate_only)
        (campaign,) = self.created(operations, "campaign_operation")
        self.assertEqual(campaign.status.name, "PAUSED")
        self.assertTrue(result["applied"])
        self.assertEqual(
            result["resource_names"], ["customers/123/campaigns/999"]
        )
        self.assertNotIn("warnings", result)

    def test_manual_cpc_sets_campaign_strategy_and_ad_group_bid(self):
        self.create(bidding_strategy="MANUAL_CPC", cpc_bid=300)

        _, operations = self.operations()
        (campaign,) = self.created(operations, "campaign_operation")
        self.assertEqual(_bidding(campaign), "manual_cpc")
        (ad_group,) = self.created(operations, "ad_group_operation")
        self.assertEqual(ad_group.cpc_bid_micros, 300_000_000)

    def test_manual_cpc_requires_bid(self):
        with self.assertRaises(ToolError):
            self.create(bidding_strategy="MANUAL_CPC")
        self.services["GoogleAdsService"].mutate.assert_not_called()

    def test_maximize_clicks_with_cpc_limit(self):
        self.create(cpc_bid=400)

        _, operations = self.operations()
        (campaign,) = self.created(operations, "campaign_operation")
        self.assertEqual(
            campaign.target_spend.cpc_bid_ceiling_micros, 400_000_000
        )

    def test_maximize_conversions(self):
        for target_cpa, expected in ((None, 0), (15000, 15_000_000_000)):
            with self.subTest(target_cpa=target_cpa):
                self.services["GoogleAdsService"].mutate.reset_mock()
                self.create(
                    bidding_strategy="MAXIMIZE_CONVERSIONS",
                    target_cpa=target_cpa,
                )
                _, operations = self.operations()
                (campaign,) = self.created(operations, "campaign_operation")
                self.assertEqual(_bidding(campaign), "maximize_conversions")
                self.assertEqual(
                    campaign.maximize_conversions.target_cpa_micros, expected
                )

    def test_dates_locations_and_search_partners(self):
        self.create(
            start_date="2026-11-01",
            end_date="2026-12-31",
            location_ids=["9047090", 9047090, "2152"],
            language_ids=["1003", "1000"],
            include_search_partners=True,
            path1="magister",
            path2="online",
        )

        _, operations = self.operations()
        (campaign,) = self.created(operations, "campaign_operation")
        self.assertEqual(campaign.start_date_time, "2026-11-01 00:00:00")
        self.assertEqual(campaign.end_date_time, "2026-12-31 23:59:59")
        self.assertTrue(campaign.network_settings.target_search_network)
        criteria = self.created(operations, "campaign_criterion_operation")
        self.assertEqual(len(criteria), 4)
        (ad_group_ad,) = self.created(operations, "ad_group_ad_operation")
        self.assertEqual(ad_group_ad.ad.responsive_search_ad.path1, "magister")

    def test_rejects_invalid_input_before_calling_the_api(self):
        cases = {
            "long headline": {"headlines": HEADLINES + ["x" * 31]},
            "few headlines": {"headlines": HEADLINES[:2]},
            "duplicate headlines": {"headlines": ["A b", "a  B", "C"]},
            "many descriptions": {
                "descriptions": ["d1", "d2", "d3", "d4", "d5"]
            },
            "long description": {"descriptions": ["d1", "x" * 91]},
            "no keywords": {"keywords": [" "]},
            "bad url": {"final_url": "www.uautonoma.cl"},
            "path2 alone": {"path2": "online"},
            "long path": {"path1": "x" * 16},
            "bad date": {"start_date": "01-11-2026"},
            "end before start": {
                "start_date": "2026-12-01",
                "end_date": "2026-11-01",
            },
            "zero budget": {"daily_budget": 0},
            "empty name": {"campaign_name": "  "},
            "bad location": {"location_ids": ["Chile"]},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                with self.assertRaises(ToolError):
                    self.create(**overrides)
        self.services["GoogleAdsService"].search.assert_not_called()
        self.services["GoogleAdsService"].mutate.assert_not_called()

    def test_new_budget_limit_per_currency(self):
        env = create.MAX_NEW_DAILY_BUDGET_ENV_VAR
        with patch.dict(os.environ, {env: "CLP:10000, USD:100"}):
            self.create(daily_budget=10000)
            with self.assertRaises(ToolError) as context:
                self.create(daily_budget=10001)
            self.assertIn("10000 CLP", str(context.exception))

        with patch.dict(os.environ, {env: "USD:100"}):
            with self.assertRaises(ToolError) as context:
                self.create()
            self.assertIn("no limit for CLP", str(context.exception))

        with patch.dict(os.environ, {env: "CLP=10000"}):
            with self.assertRaises(ToolError):
                self.create()

        self.services["GoogleAdsService"].mutate.assert_called_once()

    def test_api_error_becomes_tool_error(self):
        self.services["GoogleAdsService"].mutate.side_effect = (
            self.google_ads_exception("Duplicate campaign name")
        )

        with self.assertRaises(ToolError) as context:
            self.create()

        self.assertIn("Duplicate campaign name", str(context.exception))


class TestTargetedCreateTools(CreateToolTestCase):

    def campaign_row(self, channel="SEARCH", strategy="TARGET_SPEND"):
        return self.make_row(
            {
                "campaign.resource_name": "customers/123/campaigns/456",
                "campaign.name": "Prueba MCP 1",
                "campaign.advertising_channel_type": self.client.enums.AdvertisingChannelTypeEnum[
                    channel
                ],
                "campaign.bidding_strategy_type": self.client.enums.BiddingStrategyTypeEnum[
                    strategy
                ],
            }
        )

    def ad_group_row(self, channel="SEARCH"):
        return self.make_row(
            {
                "ad_group.resource_name": "customers/123/adGroups/789",
                "ad_group.name": "Grupo MCP 1",
                "campaign.name": "Prueba MCP 1",
                "campaign.advertising_channel_type": self.client.enums.AdvertisingChannelTypeEnum[
                    channel
                ],
            }
        )

    def test_create_ad_group_with_keywords_and_ad(self):
        self.set_rows(self.campaign_row())

        result = create.create_ad_group(
            customer_id="123",
            campaign_id="456",
            ad_group_name="Magíster Gestión",
            keywords=["magister gestion", "magister gestion"],
            keyword_match_type="EXACT",
            cpc_bid=250,
            final_url="https://www.uautonoma.cl",
            headlines=HEADLINES,
            descriptions=DESCRIPTIONS,
            status="PAUSED",
        )

        request, operations = self.operations()
        self.assertTrue(request.validate_only)
        (ad_group,) = self.created(operations, "ad_group_operation")
        self.assertEqual(ad_group.campaign, "customers/123/campaigns/456")
        self.assertEqual(ad_group.status.name, "PAUSED")
        self.assertEqual(ad_group.cpc_bid_micros, 250_000_000)
        (keyword,) = self.created(operations, "ad_group_criterion_operation")
        self.assertEqual(keyword.keyword.match_type.name, "EXACT")
        self.assertEqual(keyword.ad_group, ad_group.resource_name)
        (ad_group_ad,) = self.created(operations, "ad_group_ad_operation")
        self.assertEqual(ad_group_ad.ad_group, ad_group.resource_name)
        self.assertEqual(result["changes"][0]["campaign"], "Prueba MCP 1")
        self.assertFalse(result["applied"])

    def test_create_ad_group_without_ad(self):
        self.set_rows(self.campaign_row())

        create.create_ad_group(
            customer_id="123", campaign_id="456", ad_group_name="Vacío"
        )

        _, operations = self.operations()
        self.assertEqual(len(operations), 1)

    def test_create_ad_group_checks_campaign_and_inputs(self):
        cases = [
            ("display campaign", self.campaign_row(channel="DISPLAY"), {}),
            (
                "manual cpc without bid",
                self.campaign_row(strategy="MANUAL_CPC"),
                {},
            ),
            (
                "ad without url",
                self.campaign_row(),
                {"headlines": HEADLINES, "descriptions": DESCRIPTIONS},
            ),
        ]
        for label, row, overrides in cases:
            with self.subTest(label):
                self.set_rows(row)
                with self.assertRaises(ToolError):
                    create.create_ad_group(
                        customer_id="123",
                        campaign_id="456",
                        ad_group_name="Nuevo",
                        **overrides,
                    )
        self.services["GoogleAdsService"].mutate.assert_not_called()

    def test_add_keywords(self):
        self.set_rows(self.ad_group_row())

        result = create.add_keywords(
            customer_id="123",
            ad_group_id="789",
            keywords=["postgrado chile", "magister online"],
            match_type="BROAD",
            cpc_bid=280,
        )

        _, operations = self.operations()
        keywords = self.created(operations, "ad_group_criterion_operation")
        self.assertEqual(len(keywords), 2)
        self.assertEqual(keywords[0].ad_group, "customers/123/adGroups/789")
        self.assertEqual(keywords[0].keyword.match_type.name, "BROAD")
        self.assertEqual(keywords[0].cpc_bid_micros, 280_000_000)
        self.assertFalse(keywords[0].negative)
        self.assertEqual(
            result["changes"][0]["keywords_added"],
            ["postgrado chile", "magister online"],
        )

    def test_add_keywords_rejects_non_search_ad_group(self):
        self.set_rows(self.ad_group_row(channel="PERFORMANCE_MAX"))

        with self.assertRaises(ToolError) as context:
            create.add_keywords(
                customer_id="123",
                ad_group_id="789",
                keywords=["postgrado"],
                match_type="PHRASE",
            )

        self.assertIn("PERFORMANCE_MAX", str(context.exception))

    def test_create_responsive_search_ad(self):
        self.set_rows(self.ad_group_row())

        create.create_responsive_search_ad(
            customer_id="123",
            ad_group_id="789",
            final_url="https://www.uautonoma.cl/postgrados",
            headlines=HEADLINES,
            descriptions=DESCRIPTIONS,
            path1="postgrados",
            status="PAUSED",
        )

        _, operations = self.operations()
        (ad_group_ad,) = self.created(operations, "ad_group_ad_operation")
        self.assertEqual(ad_group_ad.ad_group, "customers/123/adGroups/789")
        self.assertEqual(ad_group_ad.status.name, "PAUSED")
        self.assertEqual(
            ad_group_ad.ad.responsive_search_ad.path1, "postgrados"
        )


class TestCreateToolsMounting(unittest.IsolatedAsyncioTestCase):
    """Verifies that the creation tools follow the mutate namespace."""

    async def test_create_tools_follow_the_mutate_namespace(self):
        default = [t.name for t in await list_tools("tools_config.yaml")]
        write = {t.name: t for t in await list_tools("tools_config_write.yaml")}

        for name in CREATE_TOOL_NAMES:
            self.assertNotIn(name, default)
            self.assertIn(name, write)
            self.assertFalse(write[name].annotations.read_only_hint)


if __name__ == "__main__":
    unittest.main()
