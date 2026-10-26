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

"""Test cases for copying Search campaigns."""

import os
import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp.tools import clone
from ads_mcp.tools.create import MAX_NEW_DAILY_BUDGET_ENV_VAR
from tests.tools.create_test import CreateToolTestCase


class TestCloneSearchCampaign(CreateToolTestCase):

    def setUp(self):
        super().setUp()
        self.channel = "SEARCH"
        self.bidding = {
            "campaign.bidding_strategy_type": self.client.enums.BiddingStrategyTypeEnum.TARGET_SPEND,
            "campaign.target_spend.cpc_bid_ceiling_micros": 900_000,
        }
        self.services["GoogleAdsService"].search.side_effect = self.rows_for

    def rows_for(self, customer_id, query):
        enums = self.client.enums
        if "FROM campaign_criterion" in query:
            return [
                self.make_row(
                    {
                        "campaign_criterion.type_": enums.CriterionTypeEnum.LANGUAGE,
                        "campaign_criterion.language.language_constant": "languageConstants/1003",
                    }
                ),
                self.make_row(
                    {
                        "campaign_criterion.type_": enums.CriterionTypeEnum.KEYWORD,
                        "campaign_criterion.negative": True,
                        "campaign_criterion.keyword.text": "gratis Chile",
                        "campaign_criterion.keyword.match_type": enums.KeywordMatchTypeEnum.PHRASE,
                    }
                ),
                self.make_row(
                    {
                        "campaign_criterion.type_": enums.CriterionTypeEnum.AD_SCHEDULE,
                        "campaign_criterion.ad_schedule.day_of_week": enums.DayOfWeekEnum.MONDAY,
                        "campaign_criterion.ad_schedule.start_hour": 8,
                        "campaign_criterion.ad_schedule.end_hour": 20,
                        "campaign_criterion.bid_modifier": 1.2,
                    }
                ),
            ]
        if "FROM campaign_shared_set" in query:
            return [
                self.make_row(
                    {
                        "campaign_shared_set.shared_set": "customers/123/sharedSets/9"
                    }
                )
            ]
        if "FROM campaign_asset" in query:
            return [
                self.make_row(
                    {
                        "campaign_asset.asset": "customers/123/assets/5",
                        "campaign_asset.field_type": enums.AssetFieldTypeEnum.SITELINK,
                    }
                )
            ]
        if "FROM ad_group_criterion" in query:
            return [
                self.make_row(
                    {
                        "ad_group.id": 10,
                        "ad_group_criterion.keyword.text": "magister chile",
                        "ad_group_criterion.keyword.match_type": enums.KeywordMatchTypeEnum.PHRASE,
                        "ad_group_criterion.status": enums.AdGroupCriterionStatusEnum.ENABLED,
                        "ad_group_criterion.cpc_bid_micros": 500_000,
                    }
                ),
                self.make_row(
                    {
                        "ad_group.id": 11,
                        "ad_group_criterion.keyword.text": "trabajo",
                        "ad_group_criterion.keyword.match_type": enums.KeywordMatchTypeEnum.BROAD,
                        "ad_group_criterion.negative": True,
                    }
                ),
            ]
        if "FROM ad_group_ad" in query:
            row = self.make_row(
                {
                    "ad_group.id": 10,
                    "ad_group_ad.status": enums.AdGroupAdStatusEnum.ENABLED,
                    "ad_group_ad.ad.responsive_search_ad.path1": "chile",
                }
            )
            ad = row.ad_group_ad.ad
            ad.final_urls.append("https://carver.edu/cl/ia")
            rsa = ad.responsive_search_ad
            for text in ("Magíster IA Chile", "Online", "Postula"):
                asset = self.client.get_type("AdTextAsset")
                asset.text = text
                rsa.headlines.append(asset)
            rsa.headlines[0].pinned_field = (
                enums.ServedAssetFieldTypeEnum.HEADLINE_1
            )
            for text in ("Estudia en Chile", "100% online"):
                asset = self.client.get_type("AdTextAsset")
                asset.text = text
                rsa.descriptions.append(asset)
            return [row]
        if "FROM ad_group " in query:
            return [
                self.make_row(
                    {
                        "ad_group.id": 10,
                        "ad_group.name": "IA Chile",
                        "ad_group.status": enums.AdGroupStatusEnum.ENABLED,
                        "ad_group.cpc_bid_micros": 700_000,
                    }
                ),
                self.make_row(
                    {
                        "ad_group.id": 11,
                        "ad_group.name": "Genérico",
                        "ad_group.status": enums.AdGroupStatusEnum.PAUSED,
                    }
                ),
            ]
        if "FROM campaign " in query:
            return [
                self.make_row(
                    {
                        "campaign.resource_name": "customers/123/campaigns/1",
                        "campaign.name": "Inteligencia Artificial - Chile",
                        "campaign.advertising_channel_type": enums.AdvertisingChannelTypeEnum[
                            self.channel
                        ],
                        "campaign.network_settings.target_google_search": True,
                        "campaign.network_settings.target_search_network": True,
                        "campaign_budget.amount_micros": 20_000_000,
                        **self.bidding,
                    }
                )
            ]
        if "FROM customer" in query:
            return [self.make_row({"customer.currency_code": "USD"})]
        raise AssertionError(query)

    def clone(self, **overrides):
        args = {
            "customer_id": "123",
            "source_campaign_id": "1",
            "new_campaign_name": "Inteligencia Artificial - Perú",
            "location_ids": ["2604"],
            "text_replacements": {"Chile": "Perú", "chile": "peru"},
            "url_replacements": {"/cl/": "/pe/"},
        }
        args.update(overrides)
        return clone.clone_search_campaign(**args)

    def test_copies_the_whole_campaign_in_one_request(self):
        result = self.clone()

        request, operations = self.operations()
        self.assertTrue(request.validate_only)
        (budget,) = self.created(operations, "campaign_budget_operation")
        self.assertEqual(budget.amount_micros, 20_000_000)
        (campaign,) = self.created(operations, "campaign_operation")
        self.assertEqual(campaign.name, "Inteligencia Artificial - Perú")
        self.assertEqual(campaign.status.name, "ENABLED")
        self.assertTrue(campaign.network_settings.target_search_network)
        self.assertEqual(campaign.target_spend.cpc_bid_ceiling_micros, 900_000)

        criteria = self.created(operations, "campaign_criterion_operation")
        locations = [
            c.location.geo_target_constant
            for c in criteria
            if c.location.geo_target_constant
        ]
        languages = [
            c.language.language_constant
            for c in criteria
            if c.language.language_constant
        ]
        self.assertEqual(locations, ["geoTargetConstants/2604"])
        self.assertEqual(languages, ["languageConstants/1003"])
        (negative,) = [c for c in criteria if c.negative]
        self.assertEqual(negative.keyword.text, "gratis Perú")
        (schedule,) = [c for c in criteria if c.ad_schedule.start_hour]
        self.assertEqual(schedule.ad_schedule.day_of_week.name, "MONDAY")
        self.assertEqual(schedule.ad_schedule.end_hour, 20)
        self.assertAlmostEqual(schedule.bid_modifier, 1.2)

        (shared,) = self.created(operations, "campaign_shared_set_operation")
        self.assertEqual(shared.shared_set, "customers/123/sharedSets/9")
        self.assertEqual(shared.campaign, "customers/123/campaigns/-2")
        (asset,) = self.created(operations, "campaign_asset_operation")
        self.assertEqual(asset.field_type.name, "SITELINK")

        first, second = self.created(operations, "ad_group_operation")
        self.assertEqual(first.name, "IA Perú")
        self.assertEqual(first.resource_name, "customers/123/adGroups/-3")
        self.assertEqual(first.cpc_bid_micros, 700_000)
        self.assertEqual(second.resource_name, "customers/123/adGroups/-4")
        self.assertEqual(second.status.name, "PAUSED")

        keyword, negative_keyword = self.created(
            operations, "ad_group_criterion_operation"
        )
        self.assertEqual(keyword.keyword.text, "magister peru")
        self.assertEqual(keyword.ad_group, "customers/123/adGroups/-3")
        self.assertEqual(keyword.cpc_bid_micros, 500_000)
        self.assertTrue(negative_keyword.negative)
        self.assertEqual(negative_keyword.ad_group, "customers/123/adGroups/-4")

        (ad,) = self.created(operations, "ad_group_ad_operation")
        self.assertEqual(list(ad.ad.final_urls), ["https://carver.edu/pe/ia"])
        rsa = ad.ad.responsive_search_ad
        self.assertEqual(rsa.headlines[0].text, "Magíster IA Perú")
        self.assertEqual(rsa.headlines[0].pinned_field.name, "HEADLINE_1")
        self.assertEqual(rsa.descriptions[0].text, "Estudia en Perú")
        self.assertEqual(rsa.path1, "peru")

        (change,) = result["changes"]
        self.assertEqual(change["bidding"], "TARGET_SPEND")
        self.assertEqual(change["ad_groups"], 2)
        self.assertEqual(change["keywords"], 1)
        self.assertEqual(change["negative_keywords"], 2)
        self.assertEqual(change["campaign_assets"], 1)
        self.assertIn("shared with the source", " ".join(result["warnings"]))

    def test_rejects_texts_that_become_too_long(self):
        with self.assertRaisesRegex(ToolError, "headline .* more than 30"):
            self.clone(
                text_replacements={"Chile": "República Dominicana y más"}
            )
        self.services["GoogleAdsService"].mutate.assert_not_called()

    def test_only_search_campaigns(self):
        self.channel = "PERFORMANCE_MAX"
        with self.assertRaisesRegex(ToolError, "only Search"):
            self.clone()

    def test_budget_override_respects_the_server_limit(self):
        with patch.dict(os.environ, {MAX_NEW_DAILY_BUDGET_ENV_VAR: "USD:60"}):
            with self.assertRaisesRegex(ToolError, "above the 60 USD"):
                self.clone(daily_budget=80)
            self.clone(daily_budget=15, status="PAUSED", language_ids=["1000"])
        _, operations = self.operations()
        (budget,) = self.created(operations, "campaign_budget_operation")
        self.assertEqual(budget.amount_micros, 15_000_000)
        criteria = self.created(operations, "campaign_criterion_operation")
        self.assertIn(
            "languageConstants/1000",
            [c.language.language_constant for c in criteria],
        )

    def test_copies_maximize_conversions_targets(self):
        self.bidding = {
            "campaign.bidding_strategy_type": self.client.enums.BiddingStrategyTypeEnum.TARGET_CPA,
            "campaign.target_cpa.target_cpa_micros": 25_000_000,
        }
        self.clone()
        _, operations = self.operations()
        (campaign,) = self.created(operations, "campaign_operation")
        self.assertEqual(
            campaign.maximize_conversions.target_cpa_micros, 25_000_000
        )

    def test_unsupported_bidding(self):
        self.bidding = {
            "campaign.bidding_strategy_type": self.client.enums.BiddingStrategyTypeEnum.TARGET_IMPRESSION_SHARE
        }
        with self.assertRaisesRegex(ToolError, "TARGET_IMPRESSION_SHARE"):
            self.clone()


if __name__ == "__main__":
    unittest.main()
