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

"""Test cases for the read-only report tools."""

import datetime
import re
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.oauth2.credentials import Credentials

from ads_mcp.tools import reports
from tests.tools.mutate_test import list_tools

NOW = datetime.datetime(
    2026, 10, 26, 12, 0, tzinfo=ZoneInfo("America/Santiago")
)


class ReportTestCase(unittest.TestCase):
    """Runs the reports with a fake GoogleAdsService.

    responses maps a FROM resource to the rows it returns; a list of row lists
    returns them on consecutive queries to that resource.
    """

    def setUp(self):
        self.client = GoogleAdsClient(
            credentials=Credentials(token="fake"), use_proto_plus=True
        )
        self.queries = []
        self.responses = {
            "customer": [
                self.row(
                    {
                        "customer.currency_code": "CLP",
                        "customer.time_zone": "America/Santiago",
                    }
                )
            ]
        }
        service = MagicMock()
        service.search_stream.side_effect = self.search_stream
        patchers = [
            patch("ads_mcp.utils.get_googleads_service", return_value=service),
            patch("ads_mcp.tools.reports._now", return_value=NOW),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def search_stream(self, customer_id, query):
        self.queries.append(query)
        resource = re.search(r" FROM (\w+)", query).group(1)
        rows = self.responses.get(resource, [])
        if rows and isinstance(rows[0], list):
            rows = rows.pop(0)
        return [MagicMock(results=rows)]

    def row(self, fields):
        row = self.client.get_type("GoogleAdsRow")
        for path, value in fields.items():
            *parents, leaf = path.split(".")
            message = row
            for parent in parents:
                message = getattr(message, parent)
            setattr(message, leaf, value)
        return row

    def metrics(self, impressions, clicks, cost, conversions=0.0, value=0.0):
        return {
            "metrics.impressions": impressions,
            "metrics.clicks": clicks,
            "metrics.cost_micros": int(cost * 1_000_000),
            "metrics.conversions": conversions,
            "metrics.conversions_value": value,
        }

    def enum(self, name, value):
        return getattr(self.client.enums, name)[value]


class TestPeriods(unittest.TestCase):

    def test_presets_end_yesterday_like_google_ads(self):
        today = datetime.date(2026, 10, 26)
        cases = {
            "TODAY": ("2026-10-26", "2026-10-26"),
            "YESTERDAY": ("2026-10-25", "2026-10-25"),
            "LAST_7_DAYS": ("2026-10-19", "2026-10-25"),
            "LAST_30_DAYS": ("2026-09-26", "2026-10-25"),
            "THIS_MONTH": ("2026-10-01", "2026-10-26"),
            "LAST_MONTH": ("2026-09-01", "2026-09-30"),
        }
        for preset, expected in cases.items():
            with self.subTest(preset):
                start, end = reports._period(preset, None, None, today)
                self.assertEqual((str(start), str(end)), expected)

    def test_explicit_dates_and_errors(self):
        today = datetime.date(2026, 10, 26)
        self.assertEqual(
            reports._period("LAST_7_DAYS", "2026-10-01", "2026-10-10", today),
            (datetime.date(2026, 10, 1), datetime.date(2026, 10, 10)),
        )
        for start, end in (("2026-10-01", None), ("2026-10-10", "2026-10-01")):
            with self.subTest(start=start, end=end):
                with self.assertRaises(ToolError):
                    reports._period("LAST_7_DAYS", start, end, today)

    def test_previous_period_has_the_same_length(self):
        self.assertEqual(
            reports._previous_period(
                datetime.date(2026, 10, 19), datetime.date(2026, 10, 25)
            ),
            (datetime.date(2026, 10, 12), datetime.date(2026, 10, 18)),
        )


class TestPerformanceReport(ReportTestCase):

    def campaign(self, campaign_id, name, **metrics):
        return self.row(
            {
                "campaign.id": campaign_id,
                "campaign.name": name,
                "campaign.status": self.enum("CampaignStatusEnum", "ENABLED"),
                "campaign.advertising_channel_type": self.enum(
                    "AdvertisingChannelTypeEnum", "SEARCH"
                ),
                **self.metrics(**metrics),
            }
        )

    def test_campaign_report_with_ratios_and_comparison(self):
        self.responses["campaign"] = [
            [
                self.campaign(
                    1,
                    "MBA",
                    impressions=1000,
                    clicks=50,
                    cost=25000,
                    conversions=5,
                ),
                self.campaign(2, "Educación", impressions=0, clicks=0, cost=0),
            ],
            [
                self.campaign(
                    1,
                    "MBA",
                    impressions=800,
                    clicks=40,
                    cost=20000,
                    conversions=4,
                )
            ],
        ]
        self.responses["customer"] = [
            self.responses["customer"],
            [self.row({"customer.id": 9, **self.metrics(1000, 50, 25000, 5)})],
            [self.row({"customer.id": 9, **self.metrics(800, 40, 20000, 4)})],
        ]

        result = reports.get_performance_report(
            customer_id="123-456-7890",
            date_range="LAST_7_DAYS",
            compare_previous_period=True,
            limit=10,
        )

        main_query = next(q for q in self.queries if "FROM campaign" in q)
        self.assertIn(
            "segments.date BETWEEN '2026-10-19' AND '2026-10-25'", main_query
        )
        self.assertIn("ORDER BY metrics.cost_micros DESC LIMIT 10", main_query)
        self.assertEqual(result["currency"], "CLP")
        self.assertEqual(
            result["previous_period"],
            {"start": "2026-10-12", "end": "2026-10-18"},
        )
        mba = result["rows"][0]
        self.assertEqual(mba["campaign"], "MBA")
        self.assertEqual(mba["cost"], 25000)
        self.assertEqual(mba["ctr_percent"], 5.0)
        self.assertEqual(mba["avg_cpc"], 500)
        self.assertEqual(mba["conversion_rate_percent"], 10.0)
        self.assertEqual(mba["cost_per_conversion"], 5000)
        self.assertEqual(mba["change_percent"]["cost"], 25.0)
        self.assertEqual(result["rows"][1]["ctr_percent"], None)
        self.assertEqual(result["totals"]["clicks"], 50)
        self.assertEqual(result["totals_change_percent"]["clicks"], 25.0)
        # Without a campaign filter the totals come from the account level.
        self.assertTrue(
            any(q.startswith("SELECT customer.id") for q in self.queries)
        )

    def test_campaign_filter_uses_campaign_totals(self):
        self.responses["campaign"] = [
            self.campaign(1, "MBA", impressions=10, clicks=1, cost=100)
        ]

        result = reports.get_performance_report(
            customer_id="123", campaign_ids=["1", 1]
        )

        self.assertTrue(
            all("campaign.id IN (1)" in q for q in self.queries[1:])
        )
        self.assertEqual(result["totals"]["cost"], 100)

    def test_keyword_level_matches_previous_rows_by_ad_group_and_keyword(self):
        def keyword(ad_group_id, criterion_id, text, clicks):
            return self.row(
                {
                    "ad_group.id": ad_group_id,
                    "ad_group_criterion.criterion_id": criterion_id,
                    "ad_group_criterion.keyword.text": text,
                    "ad_group_criterion.keyword.match_type": self.enum(
                        "KeywordMatchTypeEnum", "PHRASE"
                    ),
                    "ad_group.name": "G",
                    "campaign.name": "C",
                    **self.metrics(100, clicks, clicks * 10),
                }
            )

        self.responses["keyword_view"] = [
            [keyword(7, 70, "mba online", 10)],
            [keyword(8, 70, "otro grupo", 99), keyword(7, 70, "mba online", 5)],
        ]

        result = reports.get_performance_report(
            customer_id="123", level="keyword", compare_previous_period=True
        )

        (row,) = result["rows"]
        self.assertEqual(row["keyword"], "mba online")
        self.assertEqual(row["previous"]["clicks"], 5)
        self.assertIn(
            "ad_group_criterion.criterion_id IN (70)",
            [q for q in self.queries if "keyword_view" in q][1],
        )

    def test_invalid_input(self):
        for kwargs in (
            {"limit": 0},
            {"level": "account", "campaign_ids": ["1"]},
            {"campaign_ids": ["abc"]},
        ):
            with self.subTest(kwargs):
                with self.assertRaises(ToolError):
                    reports.get_performance_report(customer_id="123", **kwargs)


class TestSearchTermsReport(ReportTestCase):

    def term(self, text, status, clicks, conversions, cost):
        return self.row(
            {
                "search_term_view.search_term": text,
                "search_term_view.status": self.enum(
                    "SearchTermTargetingStatusEnum", status
                ),
                "campaign.id": 1,
                "campaign.name": "MBA",
                "ad_group.id": 2,
                "ad_group.name": "G",
                **self.metrics(100, clicks, cost, conversions),
            }
        )

    def test_suggests_negative_keywords(self):
        self.responses["search_term_view"] = [
            self.term("magister gratis online", "NONE", 1, 0, 300),
            self.term("mba ejecutivo", "NONE", 4, 0, 2000),
            self.term("mba santiago", "NONE", 9, 2, 4500),
            self.term("curso gratis", "EXCLUDED", 5, 0, 900),
            self.term("magister en educacion", "ADDED", 6, 0, 1500),
        ]

        result = reports.get_search_terms_report(customer_id="123")

        self.assertEqual(len(result["search_terms"]), 5)
        candidates = {
            c["search_term"]: c for c in result["negative_candidates"]
        }
        self.assertEqual(
            set(candidates), {"magister gratis online", "mba ejecutivo"}
        )
        self.assertEqual(
            candidates["magister gratis online"]["suggested_negative"], "gratis"
        )
        self.assertEqual(
            candidates["magister gratis online"]["match_type"], "PHRASE"
        )
        self.assertEqual(
            candidates["mba ejecutivo"]["suggested_negative"], "mba ejecutivo"
        )
        self.assertEqual(candidates["mba ejecutivo"]["match_type"], "EXACT")
        self.assertIn("metrics.impressions > 0", self.queries[-1])

    def test_custom_threshold_and_words(self):
        self.responses["search_term_view"] = [
            self.term("magister gratis", "NONE", 1, 0, 100),
            self.term("mba ejecutivo", "NONE", 4, 0, 2000),
        ]

        result = reports.get_search_terms_report(
            customer_id="123",
            min_clicks_without_conversions=10,
            irrelevant_words=["ejecutivo"],
        )

        (candidate,) = result["negative_candidates"]
        self.assertEqual(candidate["suggested_negative"], "ejecutivo")


class TestBudgetPacing(ReportTestCase):

    def test_projects_month_spend_and_flags_pacing(self):
        def campaign(campaign_id, name, daily, period="DAILY"):
            return self.row(
                {
                    "campaign.id": campaign_id,
                    "campaign.name": name,
                    "campaign_budget.amount_micros": daily * 1_000_000,
                    "campaign_budget.period": self.enum(
                        "BudgetPeriodEnum", period
                    ),
                }
            )

        def spend(campaign_id, cost):
            return self.row(
                {
                    "campaign.id": campaign_id,
                    "metrics.cost_micros": int(cost * 1_000_000),
                }
            )

        self.responses["campaign"] = [
            [
                campaign(1, "Ritmo justo", 1000),
                campaign(2, "Gasta poco", 1000),
                campaign(3, "Gasta mucho", 1000),
                campaign(4, "Sin gasto", 1000),
                campaign(5, "Presupuesto total", 1000, "CUSTOM_PERIOD"),
            ],
            [spend(1, 25500), spend(2, 10000), spend(3, 40000)],
        ]

        result = reports.get_budget_pacing(customer_id="123")

        # 25.5 days elapsed on October 26 at noon.
        self.assertEqual(result["elapsed_days"], 25.5)
        self.assertEqual(result["days_in_month"], 31)
        by_name = {c["campaign"]: c for c in result["campaigns"]}
        self.assertNotIn("Presupuesto total", by_name)
        self.assertEqual(by_name["Ritmo justo"]["status"], "ON_TRACK")
        self.assertEqual(by_name["Ritmo justo"]["projected_month_spend"], 31000)
        self.assertEqual(by_name["Gasta poco"]["status"], "UNDER")
        self.assertEqual(by_name["Gasta mucho"]["status"], "OVER")
        self.assertEqual(by_name["Sin gasto"]["spend_to_date"], 0)
        self.assertEqual(by_name["Ritmo justo"]["max_month_charge"], 30400)
        self.assertIn(
            "segments.date BETWEEN '2026-10-01' AND '2026-10-26'",
            self.queries[-1],
        )


class TestAssetPerformance(ReportTestCase):

    def test_lists_low_texts_and_pmax_assets_not_eligible(self):
        def text(label, value):
            return self.row(
                {
                    "campaign.name": "MBA",
                    "ad_group.name": "G",
                    "ad_group_ad.ad.id": 5,
                    "ad_group_ad_asset_view.field_type": self.enum(
                        "AssetFieldTypeEnum", "HEADLINE"
                    ),
                    "ad_group_ad_asset_view.performance_label": self.enum(
                        "AssetPerformanceLabelEnum", label
                    ),
                    "asset.text_asset.text": value,
                }
            )

        def pmax(status, value):
            return self.row(
                {
                    "campaign.name": "PMax",
                    "asset_group.name": "AG",
                    "asset_group_asset.field_type": self.enum(
                        "AssetFieldTypeEnum", "MARKETING_IMAGE"
                    ),
                    "asset_group_asset.primary_status": self.enum(
                        "AssetLinkPrimaryStatusEnum", status
                    ),
                    "asset.name": value,
                    "asset.type_": self.enum("AssetTypeEnum", "IMAGE"),
                }
            )

        self.responses["ad_group_ad_asset_view"] = [
            text("BEST", "Postgrados UA"),
            text("LOW", "Postula Hoy"),
            text("LOW", "Clases Flexibles"),
        ]
        self.responses["asset_group_asset"] = [
            pmax("ELIGIBLE", "ok.png"),
            pmax("NOT_ELIGIBLE", "rechazada.png"),
        ]

        result = reports.get_asset_performance(customer_id="123")

        search = result["responsive_search_ads"]
        self.assertEqual(search["label_counts"], {"BEST": 1, "LOW": 2})
        self.assertEqual(
            [entry["text"] for entry in search["to_replace"]],
            ["Postula Hoy", "Clases Flexibles"],
        )
        (not_eligible,) = result["performance_max"]["not_fully_eligible"]
        self.assertEqual(not_eligible["asset"], "rechazada.png")
        self.assertEqual(result["performance_max"]["assets_checked"], 2)


class TestListRecommendations(ReportTestCase):

    def test_lists_recommendations_with_impact_and_details(self):
        budget = self.row(
            {
                "recommendation.resource_name": "customers/123/recommendations/AbC=",
                "recommendation.type_": self.enum(
                    "RecommendationTypeEnum", "CAMPAIGN_BUDGET"
                ),
                "recommendation.campaign": "customers/123/campaigns/1",
                "recommendation.impact.base_metrics.clicks": 10,
                "recommendation.impact.potential_metrics.clicks": 25,
                "recommendation.impact.base_metrics.cost_micros": 1_000_000_000,
                "recommendation.impact.potential_metrics.cost_micros": 1_500_000_000,
                "recommendation.campaign_budget_recommendation.current_budget_amount_micros": 5_000_000_000,
                "recommendation.campaign_budget_recommendation.recommended_budget_amount_micros": 8_000_000_000,
            }
        )
        keyword = self.row(
            {
                "recommendation.resource_name": "customers/123/recommendations/XyZ",
                "recommendation.type_": self.enum(
                    "RecommendationTypeEnum", "KEYWORD"
                ),
                "recommendation.keyword_recommendation.keyword.text": "mba chile",
                "recommendation.keyword_recommendation.keyword.match_type": self.enum(
                    "KeywordMatchTypeEnum", "PHRASE"
                ),
            }
        )
        self.responses["recommendation"] = [budget, keyword]
        self.responses["campaign"] = [
            self.row(
                {
                    "campaign.resource_name": "customers/123/campaigns/1",
                    "campaign.name": "MBA",
                }
            )
        ]

        result = reports.list_recommendations(
            customer_id="123", types=["campaign_budget", "KEYWORD"]
        )

        first, second = result["recommendations"]
        self.assertEqual(first["recommendation_id"], "AbC=")
        self.assertEqual(first["campaign"], "MBA")
        self.assertEqual(first["weekly_impact"]["clicks"], 15)
        self.assertEqual(first["weekly_impact"]["cost"], 500)
        self.assertEqual(
            first["budget"], {"current": 5000, "recommended": 8000}
        )
        self.assertEqual(
            second["keyword"], {"text": "mba chile", "match_type": "PHRASE"}
        )
        query = next(q for q in self.queries if "FROM recommendation" in q)
        self.assertIn("recommendation.impact,", query)
        self.assertIn(
            "recommendation.type IN ('CAMPAIGN_BUDGET', 'KEYWORD')", query
        )

    def test_rejects_invalid_types(self):
        with self.assertRaises(ToolError):
            reports.list_recommendations(
                customer_id="123", types=["KEYWORD' OR 1=1"]
            )


class TestReportsMounting(unittest.IsolatedAsyncioTestCase):

    async def test_reports_are_read_only_and_in_both_configs(self):
        names = [
            "reports_get_asset_performance",
            "reports_get_budget_pacing",
            "reports_get_performance_report",
            "reports_get_search_terms_report",
            "reports_list_recommendations",
        ]
        for config in ("tools_config.yaml", "tools_config_write.yaml"):
            tools = {t.name: t for t in await list_tools(config)}
            for name in names:
                with self.subTest(config=config, tool=name):
                    self.assertIn(name, tools)
                    self.assertTrue(tools[name].annotations.read_only_hint)


if __name__ == "__main__":
    unittest.main()
