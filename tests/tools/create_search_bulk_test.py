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

"""Test cases for creating several Search campaigns in one call."""

import os
import unittest
from unittest.mock import patch

import pydantic
from fastmcp.exceptions import ToolError

from ads_mcp.tools import create, create_search_bulk
from ads_mcp.tools.create_search_bulk import SearchCampaign
from tests.tools.create_test import DESCRIPTIONS, HEADLINES, CreateToolTestCase
from tests.tools.mutate_test import list_tools


def ad(**overrides):
    return {
        "final_url": "https://www.uautonoma.cl",
        "headlines": HEADLINES,
        "descriptions": DESCRIPTIONS,
        **overrides,
    }


def ad_group(name, keyword_texts, **overrides):
    """Builds an ad group; pass keywords=[...] to set full keyword specs."""
    return {
        "name": name,
        "keywords": [{"text": k} for k in keyword_texts],
        "ads": [ad()],
        **overrides,
    }


def campaign(name, ad_groups, **overrides):
    return SearchCampaign.model_validate(
        {
            "name": name,
            "daily_budget": 5000,
            "ad_groups": ad_groups,
            **overrides,
        }
    )


def _kind(operation):
    return type(operation).pb(operation).WhichOneof("operation")


class TestCreateSearchCampaigns(CreateToolTestCase):

    def setUp(self):
        super().setUp()
        self.set_rows(self.make_row({"customer.currency_code": "CLP"}))

    def create(self, campaigns, **kwargs):
        return create_search_bulk.create_search_campaigns(
            customer_id="123", campaigns=campaigns, **kwargs
        )

    def test_creates_several_campaigns_in_one_request(self):
        result = self.create(
            [
                campaign(
                    "Magíster Educación",
                    [
                        ad_group(
                            "Educación online",
                            [
                                "magister educacion online",
                                "postgrado educacion",
                            ],
                            negative_keywords=[{"text": "gratis"}],
                        ),
                        ad_group(
                            "Educación diferencial",
                            ["magister educacion diferencial"],
                            ads=[
                                ad(),
                                ad(final_url="https://www.uautonoma.cl/ed"),
                            ],
                        ),
                    ],
                    negative_keywords=[
                        {"text": "empleo", "match_type": "BROAD"}
                    ],
                ),
                campaign(
                    "Magíster Gestión",
                    [ad_group("Gestión", ["magister gestion"])],
                    status="PAUSED",
                    bidding_strategy="MANUAL_CPC",
                    cpc_bid=300,
                ),
            ]
        )

        request, operations = self.operations()
        self.assertTrue(request.validate_only)
        self.services["GoogleAdsService"].mutate.assert_called_once()

        budgets = self.created(operations, "campaign_budget_operation")
        campaigns = self.created(operations, "campaign_operation")
        ad_groups = self.created(operations, "ad_group_operation")
        self.assertEqual(len(budgets), 2)
        self.assertEqual(
            [c.name for c in campaigns],
            ["Magíster Educación", "Magíster Gestión"],
        )
        # Every temporary resource name is unique in the request.
        temp_names = [r.resource_name for r in budgets + campaigns + ad_groups]
        self.assertEqual(len(temp_names), len(set(temp_names)))
        for budget, campaign_ in zip(budgets, campaigns):
            self.assertEqual(campaign_.campaign_budget, budget.resource_name)

        education, management = campaigns
        self.assertEqual(education.status.name, "ENABLED")
        self.assertEqual(management.status.name, "PAUSED")
        self.assertEqual(
            type(management)
            .pb(management)
            .WhichOneof("campaign_bidding_strategy"),
            "manual_cpc",
        )
        self.assertEqual(
            [g.campaign for g in ad_groups],
            [
                education.resource_name,
                education.resource_name,
                management.resource_name,
            ],
        )
        # MANUAL_CPC ad groups default to the campaign bid.
        self.assertEqual(ad_groups[2].cpc_bid_micros, 300_000_000)

        criteria = self.created(operations, "campaign_criterion_operation")
        negatives = [c for c in criteria if c.negative]
        self.assertEqual(len(negatives), 1)
        self.assertEqual(negatives[0].campaign, education.resource_name)
        self.assertEqual(negatives[0].keyword.match_type.name, "BROAD")

        keywords = self.created(operations, "ad_group_criterion_operation")
        by_group = {}
        for keyword in keywords:
            by_group.setdefault(keyword.ad_group, []).append(
                (keyword.keyword.text, keyword.negative)
            )
        self.assertEqual(
            by_group[ad_groups[0].resource_name],
            [
                ("magister educacion online", False),
                ("postgrado educacion", False),
                ("gratis", True),
            ],
        )
        ads = self.created(operations, "ad_group_ad_operation")
        self.assertEqual(
            [a.ad_group for a in ads],
            [
                ad_groups[0].resource_name,
                ad_groups[1].resource_name,
                ad_groups[1].resource_name,
                ad_groups[2].resource_name,
            ],
        )
        # Parents come before the resources that reference them.
        kinds = [_kind(op) for op in operations]
        first_group = kinds.index("ad_group_operation")
        self.assertLess(kinds.index("campaign_operation"), first_group)
        self.assertLess(
            first_group, kinds.index("ad_group_criterion_operation")
        )

        self.assertEqual(
            result["totals"],
            {
                "campaigns": 2,
                "ad_groups": 3,
                "keywords": 4,
                "ads": 4,
                "operations": len(operations),
            },
        )
        self.assertEqual(result["changes"][0]["currency"], "CLP")
        self.assertIn("Magíster Educación", result["warnings"][0])
        self.assertNotIn("Magíster Gestión", result["warnings"][0])

    def test_returns_only_campaign_and_ad_group_names_when_applied(self):
        response = self.client.get_type("MutateGoogleAdsResponse")
        for field, name in (
            ("campaign_budget_result", "customers/123/campaignBudgets/1"),
            ("campaign_result", "customers/123/campaigns/2"),
            ("ad_group_result", "customers/123/adGroups/3"),
            ("ad_group_criterion_result", "customers/123/adGroupCriteria/3~4"),
        ):
            operation_response = self.client.get_type("MutateOperationResponse")
            getattr(operation_response, field).resource_name = name
            response.mutate_operation_responses.append(operation_response)
        self.services["GoogleAdsService"].mutate.return_value = response

        result = self.create(
            [campaign("Uno", [ad_group("G", ["postgrado"])])],
            validate_only=False,
        )

        self.assertTrue(result["applied"])
        self.assertEqual(
            result["resource_names"],
            ["customers/123/campaigns/2", "customers/123/adGroups/3"],
        )

    def test_reports_every_problem_at_once(self):
        with self.assertRaises(ToolError) as context:
            self.create(
                [
                    campaign(
                        "Uno",
                        [
                            ad_group(
                                "G1",
                                ["postgrado"],
                                ads=[ad(), ad(headlines=["x" * 31, "b", "c"])],
                            ),
                            ad_group("G1", ["x" * 81]),
                            ad_group("G3", ["postgrado"], ads=[ad()] * 4),
                        ],
                    ),
                    campaign(
                        "Dos",
                        [ad_group("G", ["magister"])],
                        bidding_strategy="MANUAL_CPC",
                    ),
                    campaign(
                        "uno",
                        [
                            ad_group(
                                "G",
                                [],
                                keywords=[{"text": "magister", "cpc_bid": 200}],
                            )
                        ],
                    ),
                ]
            )

        message = str(context.exception)
        self.assertIn("nothing was sent", message)
        for expected in (
            "ad group 'G1', ad 2: These headlines are longer than 30",
            "ad group 'G1': the ad group name is repeated",
            "at most 80 characters",
            "needs between 1 and 3 ads, got 4",
            "Campaign 'Dos', ad group 'G': MANUAL_CPC bidding needs a cpc_bid",
            "only works with MANUAL_CPC",
            "the campaign name is repeated",
        ):
            self.assertIn(expected, message)
        self.services["GoogleAdsService"].search.assert_not_called()
        self.services["GoogleAdsService"].mutate.assert_not_called()

    def test_duplicate_keywords_are_sent_once(self):
        self.create(
            [
                campaign(
                    "Uno",
                    [
                        ad_group(
                            "G",
                            [],
                            keywords=[
                                {"text": "Postgrado  Chile"},
                                {"text": "postgrado chile"},
                                {
                                    "text": "postgrado chile",
                                    "match_type": "EXACT",
                                },
                            ],
                        )
                    ],
                )
            ]
        )

        _, operations = self.operations()
        keywords = self.created(operations, "ad_group_criterion_operation")
        self.assertEqual(
            [(k.keyword.text, k.keyword.match_type.name) for k in keywords],
            [("Postgrado Chile", "PHRASE"), ("postgrado chile", "EXACT")],
        )

    def test_limits(self):
        one = campaign("C", [ad_group("G", ["postgrado"])])
        with self.assertRaisesRegex(ToolError, "between 1 and 20 campaigns"):
            self.create([])
        with self.assertRaisesRegex(ToolError, "between 1 and 20 campaigns"):
            self.create([one] * 21)

        with patch.object(create_search_bulk, "MAX_OPERATIONS", 5):
            with self.assertRaisesRegex(ToolError, "Split it"):
                self.create([one])

        with patch.dict(
            os.environ, {create.MAX_NEW_DAILY_BUDGET_ENV_VAR: "CLP:4000"}
        ):
            with self.assertRaisesRegex(ToolError, r"\['C'\]"):
                self.create([one])
        self.services["GoogleAdsService"].mutate.assert_not_called()

    def test_schema_rejects_unknown_fields(self):
        with self.assertRaises(pydantic.ValidationError):
            SearchCampaign.model_validate(
                {
                    "name": "C",
                    "daily_budget": 5000,
                    "ad_groups": [],
                    "presupuesto": 1,
                }
            )


class TestCreateSearchCampaignsMounting(unittest.IsolatedAsyncioTestCase):

    async def test_follows_the_mutate_namespace_with_a_nested_schema(self):
        default = [t.name for t in await list_tools("tools_config.yaml")]
        write = {t.name: t for t in await list_tools("tools_config_write.yaml")}

        self.assertNotIn("mutate_create_search_campaigns", default)
        tool = write["mutate_create_search_campaigns"]
        self.assertFalse(tool.annotations.read_only_hint)
        # FastMCP inlines the nested models; unknown fields are rejected at
        # every level.
        campaign_schema = tool.parameters["properties"]["campaigns"]["items"]
        ad_group_schema = campaign_schema["properties"]["ad_groups"]["items"]
        keyword_schema = ad_group_schema["properties"]["keywords"]["items"]
        for schema in (campaign_schema, ad_group_schema, keyword_schema):
            self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(
            keyword_schema["properties"]["match_type"]["enum"],
            ["EXACT", "PHRASE", "BROAD"],
        )


if __name__ == "__main__":
    unittest.main()
