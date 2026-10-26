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

"""Test cases for the Keyword Planner tools."""

import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException

from ads_mcp.tools import keyword_planner
from tests.tools.mutate_test import list_tools
from tests.tools.reports_test import ReportTestCase


class KeywordPlannerTestCase(ReportTestCase):

    def setUp(self):
        super().setUp()
        patcher = patch(
            "ads_mcp.utils.get_googleads_client", return_value=self.client
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # The patched get_googleads_service returns one mock for every service.
        import ads_mcp.utils as utils

        self.service = utils.get_googleads_service()

    def metrics(self, message, searches, competition="HIGH", low=0, high=0):
        enums = self.client.enums
        message.avg_monthly_searches = searches
        message.competition = enums.KeywordPlanCompetitionLevelEnum[competition]
        message.competition_index = 80
        message.low_top_of_page_bid_micros = low
        message.high_top_of_page_bid_micros = high
        volume = self.client.get_type("MonthlySearchVolume")
        volume.year = 2026
        volume.month = enums.MonthOfYearEnum.SEPTEMBER
        volume.monthly_searches = searches + 10
        message.monthly_search_volumes.append(volume)

    def idea(self, text, searches, **kwargs):
        idea = self.client.get_type("GenerateKeywordIdeaResult")
        idea.text = text
        self.metrics(idea.keyword_idea_metrics, searches, **kwargs)
        return idea


class TestKeywordIdeas(KeywordPlannerTestCase):

    def test_sorts_ideas_by_searches(self):
        self.service.generate_keyword_ideas.return_value = [
            self.idea("magister psicologia", 90, low=1_200_000, high=4_500_000),
            self.idea("maestria en psicologia", 1300, competition="MEDIUM"),
            self.idea("psicologia clinica online", 480),
        ]
        result = keyword_planner.get_keyword_ideas(
            customer_id="123",
            seed_keywords=[
                "maestría en psicología",
                " maestría  en psicología",
            ],
            url="https://carver.edu/psicologia",
            location_ids=["2170", "2604"],
            limit=2,
        )

        request = self.service.generate_keyword_ideas.call_args.kwargs[
            "request"
        ]
        self.assertEqual(request.customer_id, "123")
        self.assertEqual(request.language, "languageConstants/1003")
        self.assertEqual(
            list(request.geo_target_constants),
            ["geoTargetConstants/2170", "geoTargetConstants/2604"],
        )
        self.assertEqual(request.keyword_plan_network.name, "GOOGLE_SEARCH")
        self.assertEqual(
            request.keyword_and_url_seed.url, "https://carver.edu/psicologia"
        )
        self.assertEqual(
            list(request.keyword_and_url_seed.keywords),
            ["maestría en psicología"],
        )
        self.assertEqual(result["currency"], "CLP")
        self.assertEqual(result["ideas_found"], 3)
        self.assertEqual(
            result["ideas"],
            [
                {
                    "keyword": "maestria en psicologia",
                    "avg_monthly_searches": 1300,
                    "competition": "MEDIUM",
                    "competition_index": 80,
                    "top_of_page_bid_low": None,
                    "top_of_page_bid_high": None,
                },
                {
                    "keyword": "psicologia clinica online",
                    "avg_monthly_searches": 480,
                    "competition": "HIGH",
                    "competition_index": 80,
                    "top_of_page_bid_low": None,
                    "top_of_page_bid_high": None,
                },
            ],
        )

    def test_seed_types_and_monthly_volumes(self):
        self.service.generate_keyword_ideas.return_value = [
            self.idea("mba online", 50, low=1_234_567)
        ]
        result = keyword_planner.get_keyword_ideas(
            customer_id="123",
            url="https://carver.edu/mba",
            include_search_partners=True,
            include_monthly_volumes=True,
        )
        request = self.service.generate_keyword_ideas.call_args.kwargs[
            "request"
        ]
        self.assertEqual(request.url_seed.url, "https://carver.edu/mba")
        self.assertEqual(
            request.keyword_plan_network.name, "GOOGLE_SEARCH_AND_PARTNERS"
        )
        (idea,) = result["ideas"]
        self.assertEqual(idea["top_of_page_bid_low"], 1.23)
        self.assertEqual(
            idea["monthly_searches"], [{"month": "2026-09", "searches": 60}]
        )

    def test_invalid_input(self):
        with self.assertRaisesRegex(ToolError, "seed_keywords, a url"):
            keyword_planner.get_keyword_ideas(customer_id="123")
        with self.assertRaisesRegex(ToolError, "https"):
            keyword_planner.get_keyword_ideas(
                customer_id="123", url="carver.edu"
            )
        with self.assertRaisesRegex(ToolError, "at most 20"):
            keyword_planner.get_keyword_ideas(
                customer_id="123", seed_keywords=[f"k{i}" for i in range(21)]
            )
        with self.assertRaisesRegex(ToolError, "limit"):
            keyword_planner.get_keyword_ideas(
                customer_id="123", seed_keywords=["mba"], limit=501
            )

    def test_explains_the_access_level_needed(self):
        error = MagicMock()
        error.message = (
            "This method is not allowed for use with explorer access."
        )
        error.error_code = MagicMock()
        ex = GoogleAdsException(
            MagicMock(), MagicMock(), MagicMock(), MagicMock()
        )
        ex.failure = MagicMock(errors=[error])
        ex.request_id = "req-1"
        self.service.generate_keyword_ideas.side_effect = ex
        with patch(
            "ads_mcp.tools.keyword_planner.format_google_ads_exception",
            return_value="Error (authorization_error: DEVELOPER_TOKEN_NOT_APPROVED)",
        ):
            with self.assertRaisesRegex(ToolError, "Basic or Standard"):
                keyword_planner.get_keyword_ideas(
                    customer_id="123", seed_keywords=["mba"]
                )


class TestKeywordVolumes(KeywordPlannerTestCase):

    def test_reports_each_keyword(self):
        response = self.client.get_type(
            "GenerateKeywordHistoricalMetricsResponse"
        )
        result = self.client.get_type("GenerateKeywordHistoricalMetricsResult")
        result.text = "maestria inteligencia artificial"
        result.close_variants.append("maestría inteligencia artificial")
        self.metrics(result.keyword_metrics, 720, high=2_000_000)
        response.results.append(result)
        self.service.generate_keyword_historical_metrics.return_value = response

        output = keyword_planner.get_keyword_volumes(
            customer_id="123",
            keywords=["maestria inteligencia artificial", ""],
            location_ids=["2604"],
            language_id="1003",
        )
        request = (
            self.service.generate_keyword_historical_metrics.call_args.kwargs[
                "request"
            ]
        )
        self.assertEqual(
            list(request.keywords), ["maestria inteligencia artificial"]
        )
        self.assertEqual(
            list(request.geo_target_constants), ["geoTargetConstants/2604"]
        )
        self.assertEqual(
            output["keywords"],
            [
                {
                    "keyword": "maestria inteligencia artificial",
                    "close_variants": ["maestría inteligencia artificial"],
                    "avg_monthly_searches": 720,
                    "competition": "HIGH",
                    "competition_index": 80,
                    "top_of_page_bid_low": None,
                    "top_of_page_bid_high": 2.0,
                }
            ],
        )
        with self.assertRaisesRegex(ToolError, "at least one"):
            keyword_planner.get_keyword_volumes(customer_id="123", keywords=[])


class TestKeywordPlannerMounting(unittest.IsolatedAsyncioTestCase):

    async def test_available_in_both_configs(self):
        for config in ("tools_config.yaml", "tools_config_write.yaml"):
            tools = {t.name: t for t in await list_tools(config)}
            for name in (
                "reports_get_keyword_ideas",
                "reports_get_keyword_volumes",
            ):
                self.assertIn(name, tools)
                self.assertTrue(tools[name].annotations.read_only_hint)


if __name__ == "__main__":
    unittest.main()
