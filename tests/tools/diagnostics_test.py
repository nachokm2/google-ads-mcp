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

"""Test cases for the account alerts, policy issues and change history."""

import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp.tools import diagnostics
from tests.tools.mutate_test import list_tools
from tests.tools.reports_test import NOW, ReportTestCase


class DiagnosticsTestCase(ReportTestCase):

    def setUp(self):
        super().setUp()
        patcher = patch("ads_mcp.tools.diagnostics._now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stats(self, campaign_id, cost=0, impressions=0, conversions=0.0):
        return self.row(
            {
                "campaign.id": campaign_id,
                "metrics.cost_micros": int(cost * 1_000_000),
                "metrics.impressions": impressions,
                "metrics.conversions": conversions,
            }
        )


class TestAccountAlerts(DiagnosticsTestCase):

    def campaign(self, campaign_id, name, status, reasons=(), end=""):
        row = self.row(
            {
                "campaign.id": campaign_id,
                "campaign.name": name,
                "campaign.primary_status": self.enum(
                    "CampaignPrimaryStatusEnum", status
                ),
                "campaign.end_date_time": end,
                "campaign_budget.amount_micros": 10_000_000_000,
            }
        )
        for reason in reasons:
            row.campaign.primary_status_reasons.append(
                self.enum("CampaignPrimaryStatusReasonEnum", reason)
            )
        return row

    def test_flags_problems_by_severity(self):
        self.responses["campaign"] = [
            [
                self.campaign(1, "IA Chile", "ELIGIBLE"),
                self.campaign(
                    2, "Psicología", "LIMITED", ["BUDGET_CONSTRAINED"]
                ),
                self.campaign(
                    3,
                    "Display",
                    "NOT_ELIGIBLE",
                    ["HAS_ADS_DISAPPROVED"],
                    end="2026-10-30 23:59:59",
                ),
                self.campaign(4, "Sana", "ELIGIBLE"),
            ],
            # Yesterday.
            [
                self.stats(2, cost=30000, impressions=900),
                self.stats(4, 9000, 100),
            ],
            # The 7 days before yesterday.
            [
                self.stats(1, cost=7000, impressions=500),
                self.stats(2, cost=70000, impressions=5000),
                self.stats(4, cost=63000, impressions=700),
            ],
            # The last 7 days.
            [self.stats(4, conversions=2)],
            # The 7 days before those.
            [self.stats(2, conversions=5), self.stats(4, conversions=1)],
        ]
        self.responses["ad_group_ad"] = [
            self.row({"campaign.id": 3}),
            self.row({"campaign.id": 3}),
        ]

        result = diagnostics.get_account_alerts(customer_id="123")

        alerts = [
            (a["severity"], a["type"], a["campaign"]) for a in result["alerts"]
        ]
        self.assertEqual(
            alerts,
            [
                ("HIGH", "STOPPED_SERVING", "IA Chile"),
                ("HIGH", "CANNOT_SERVE", "Display"),
                ("HIGH", "ADS_DISAPPROVED", "Display"),
                ("MEDIUM", "SPEND_DROP", "IA Chile"),
                ("MEDIUM", "LIMITED_BY_BUDGET", "Psicología"),
                ("MEDIUM", "SPEND_SPIKE", "Psicología"),
                ("MEDIUM", "CONVERSIONS_STOPPED", "Psicología"),
                ("LOW", "ENDING_SOON", "Display"),
            ],
        )
        self.assertEqual(result["summary"], {"HIGH": 3, "MEDIUM": 4, "LOW": 1})
        self.assertEqual(
            result["spend"],
            {"yesterday": 39000.0, "average_previous_7_days": 20000.0},
        )
        spike = result["alerts"][5]
        self.assertEqual(
            spike["details"],
            {"spend_yesterday": 30000.0, "average_previous_7_days": 10000.0},
        )
        self.assertEqual(
            result["alerts"][1]["details"]["reasons"], ["HAS_ADS_DISAPPROVED"]
        )
        self.assertEqual(result["alerts"][2]["details"], {"disapproved_ads": 2})
        # Yesterday is 2026-10-25 in the account time zone.
        self.assertIn("BETWEEN '2026-10-25' AND '2026-10-25'", self.queries[2])
        self.assertIn("BETWEEN '2026-10-18' AND '2026-10-24'", self.queries[3])
        self.assertIn("BETWEEN '2026-10-19' AND '2026-10-25'", self.queries[4])
        self.assertIn("BETWEEN '2026-10-12' AND '2026-10-18'", self.queries[5])

    def test_quiet_account(self):
        result = diagnostics.get_account_alerts(customer_id="123")
        self.assertEqual(result["alerts"], [])
        self.assertEqual(result["enabled_campaigns"], 0)


class TestPolicyIssues(DiagnosticsTestCase):

    def test_lists_ads_and_assets_with_policies(self):
        ad = self.row(
            {
                "campaign.id": 1,
                "campaign.name": "IA Chile",
                "ad_group.name": "IA",
                "ad_group_ad.ad.id": 77,
                "ad_group_ad.ad.type_": self.enum(
                    "AdTypeEnum", "RESPONSIVE_SEARCH_AD"
                ),
                "ad_group_ad.policy_summary.approval_status": self.enum(
                    "PolicyApprovalStatusEnum", "DISAPPROVED"
                ),
                "ad_group_ad.policy_summary.review_status": self.enum(
                    "PolicyReviewStatusEnum", "REVIEWED"
                ),
            }
        )
        ad.ad_group_ad.ad.final_urls.append("https://carver.edu/ia")
        entry = self.client.get_type("PolicyTopicEntry")
        entry.topic = "DESTINATION_NOT_WORKING"
        entry.type_ = self.enum("PolicyTopicEntryTypeEnum", "PROHIBITED")
        evidence = self.client.get_type("PolicyTopicEvidence")
        evidence.website_list.websites.append("carver.edu/ia")
        entry.evidences.append(evidence)
        ad.ad_group_ad.policy_summary.policy_topic_entries.append(entry)
        asset = self.row(
            {
                "campaign.id": 1,
                "campaign.name": "IA Chile",
                "asset.id": 5,
                "asset.type_": self.enum("AssetTypeEnum", "SITELINK"),
                "campaign_asset.field_type": self.enum(
                    "AssetFieldTypeEnum", "SITELINK"
                ),
                "asset.policy_summary.approval_status": self.enum(
                    "PolicyApprovalStatusEnum", "APPROVED_LIMITED"
                ),
            }
        )
        self.responses["ad_group_ad"] = [ad]
        self.responses["campaign_asset"] = [asset]

        result = diagnostics.get_policy_issues(
            customer_id="123", campaign_ids=["1"]
        )

        (listed_ad,) = result["ads"]
        self.assertEqual(listed_ad["approval_status"], "DISAPPROVED")
        self.assertEqual(listed_ad["final_urls"], ["https://carver.edu/ia"])
        self.assertEqual(
            listed_ad["policies"],
            [
                {
                    "topic": "DESTINATION_NOT_WORKING",
                    "type": "PROHIBITED",
                    "evidence": ["carver.edu/ia"],
                }
            ],
        )
        (listed_asset,) = result["campaign_assets"]
        self.assertEqual(listed_asset["used_as"], "SITELINK")
        self.assertEqual(listed_asset["approval_status"], "APPROVED_LIMITED")
        self.assertEqual(
            result["summary"],
            {"ads": 1, "ads_disapproved": 1, "campaign_assets": 1},
        )
        self.assertIn("AND campaign.id IN (1)", self.queries[0])
        self.assertIn("'APPROVED_LIMITED'", self.queries[0])

    def test_only_disapproved(self):
        diagnostics.get_policy_issues(customer_id="123", include_limited=False)
        self.assertIn("IN ('DISAPPROVED')", self.queries[0])


class TestChangeHistory(DiagnosticsTestCase):

    def test_lists_changes_with_old_and_new_values(self):
        row = self.row(
            {
                "change_event.change_date_time": "2026-10-26 09:05:38",
                "change_event.change_resource_type": self.enum(
                    "ChangeEventResourceTypeEnum", "CAMPAIGN_BUDGET"
                ),
                "change_event.change_resource_name": "customers/123/campaignBudgets/9",
                "change_event.client_type": self.enum(
                    "ChangeClientTypeEnum", "GOOGLE_ADS_API"
                ),
                "change_event.user_email": "rodrigo@example.com",
                "change_event.resource_change_operation": self.enum(
                    "ResourceChangeOperationEnum", "UPDATE"
                ),
                "change_event.old_resource.campaign_budget.amount_micros": 5_000_000_000,
                "change_event.new_resource.campaign_budget.amount_micros": 7_500_000_000,
                "change_event.old_resource.campaign_budget.status": self.enum(
                    "BudgetStatusEnum", "ENABLED"
                ),
                "campaign.name": "IA Chile",
            }
        )
        row.change_event.changed_fields.paths.append("amount_micros")
        self.responses["change_event"] = [row]

        result = diagnostics.get_change_history(
            customer_id="123",
            days=3,
            campaign_ids=["1"],
            user_email="rodrigo@example.com",
        )

        (change,) = result["changes"]
        self.assertEqual(change["source"], "GOOGLE_ADS_API")
        self.assertEqual(change["resource_type"], "CAMPAIGN_BUDGET")
        self.assertEqual(
            change["values"], {"amount_micros": {"from": 5000.0, "to": 7500.0}}
        )
        self.assertEqual(result["summary"]["by_source"], {"GOOGLE_ADS_API": 1})
        query = self.queries[-1]
        self.assertIn(
            "change_event.change_date_time >= '2026-10-23 12:05:00'", query
        )
        self.assertIn(
            "change_event.campaign IN ('customers/123/campaigns/1')", query
        )
        self.assertIn("change_event.user_email = 'rodrigo@example.com'", query)
        self.assertIn("LIMIT 200", query)

    def test_limits(self):
        with self.assertRaisesRegex(ToolError, "days"):
            diagnostics.get_change_history(customer_id="123", days=31)
        with self.assertRaisesRegex(ToolError, "limit"):
            diagnostics.get_change_history(customer_id="123", limit=0)


class TestDiagnosticsMounting(unittest.IsolatedAsyncioTestCase):

    async def test_read_only_in_both_configs(self):
        names = (
            "reports_get_account_alerts",
            "reports_get_policy_issues",
            "reports_get_change_history",
        )
        for config in ("tools_config.yaml", "tools_config_write.yaml"):
            tools = {t.name: t for t in await list_tools(config)}
            for name in names:
                self.assertTrue(tools[name].annotations.read_only_hint)


if __name__ == "__main__":
    unittest.main()
