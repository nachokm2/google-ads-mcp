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

"""Test cases for shared negative keyword lists."""

import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import negative_lists
from tests.tools.create_test import CreateToolTestCase


class NegativeListTestCase(CreateToolTestCase):

    def setUp(self):
        super().setUp()
        self.linked = []
        self.existing = []
        self.services["GoogleAdsService"].search.side_effect = self.rows_for

    def rows_for(self, customer_id, query):
        enums = self.client.enums
        if "FROM shared_criterion" in query:
            return [
                self.make_row(
                    {
                        "shared_set.id": 9,
                        "shared_criterion.keyword.text": text,
                        "shared_criterion.keyword.match_type": enums.KeywordMatchTypeEnum[
                            match_type
                        ],
                    }
                )
                for text, match_type in self.existing
            ]
        if "FROM campaign_shared_set" in query:
            return [
                self.make_row(
                    {
                        "shared_set.id": 9,
                        "campaign.id": campaign_id,
                        "campaign.name": f"Campaña {campaign_id}",
                    }
                )
                for campaign_id in self.linked
            ]
        if "FROM shared_set" in query:
            return [
                self.make_row(
                    {
                        "shared_set.resource_name": "customers/123/sharedSets/9",
                        "shared_set.id": 9,
                        "shared_set.name": "Genéricas",
                        "shared_set.member_count": len(self.existing),
                        "shared_set.type_": enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS,
                        "shared_set.status": enums.SharedSetStatusEnum.ENABLED,
                    }
                )
            ]
        if "FROM campaign" in query:
            if "advertising_channel_type = 'SEARCH'" in query:
                ids = [1, 2, 3]
            else:
                ids = [
                    int(i)
                    for i in query.split("IN (")[1].split(")")[0].split(", ")
                ]
            return [
                self.make_row(
                    {"campaign.id": i, "campaign.name": f"Campaña {i}"}
                )
                for i in ids
                if i != 404
            ]
        raise AssertionError(query)


class TestParseKeywords(unittest.TestCase):

    def test_editor_syntax(self):
        self.assertEqual(
            negative_lists._parse_keywords(
                [" gratis ", "[PDF]", '"trabajo  de"', "gratis", ""], "BROAD"
            ),
            [("gratis", "BROAD"), ("PDF", "EXACT"), ("trabajo de", "PHRASE")],
        )

    def test_invalid_keywords(self):
        with self.assertRaisesRegex(ToolError, "more than 10 words"):
            negative_lists._parse_keywords(["a b c d e f g h i j k"], "PHRASE")
        with self.assertRaisesRegex(ToolError, "between 1"):
            negative_lists._parse_keywords(["[]", " "], "PHRASE")


class TestCreateList(NegativeListTestCase):

    def test_creates_list_keywords_and_links_in_one_request(self):
        result = negative_lists.create_negative_keyword_list(
            customer_id="123",
            name=" Genéricas  postgrado ",
            keywords=["gratis", "[pdf]"],
            all_search_campaigns=True,
        )
        request, operations = self.operations()
        self.assertTrue(request.validate_only)
        (shared_set,) = self.created(operations, "shared_set_operation")
        self.assertEqual(shared_set.name, "Genéricas postgrado")
        self.assertEqual(shared_set.type_.name, "NEGATIVE_KEYWORDS")
        self.assertEqual(
            shared_set.resource_name, "customers/123/sharedSets/-1"
        )
        keywords = self.created(operations, "shared_criterion_operation")
        self.assertEqual(
            [(k.keyword.text, k.keyword.match_type.name) for k in keywords],
            [("gratis", "PHRASE"), ("pdf", "EXACT")],
        )
        links = self.created(operations, "campaign_shared_set_operation")
        self.assertEqual(
            [link.campaign for link in links],
            [f"customers/123/campaigns/{i}" for i in (1, 2, 3)],
        )
        self.assertEqual(
            result["changes"][0]["campaigns"],
            ["Campaña 1", "Campaña 2", "Campaña 3"],
        )

    def test_unknown_campaign(self):
        with self.assertRaisesRegex(ToolError, "not found"):
            negative_lists.create_negative_keyword_list(
                customer_id="123",
                name="x",
                keywords=["gratis"],
                campaign_ids=["1", "404"],
            )


class TestUpdateList(NegativeListTestCase):

    def test_adds_only_new_keywords(self):
        self.existing = [("gratis", "PHRASE")]
        result = negative_lists.add_to_negative_keyword_list(
            customer_id="123",
            list_id="9",
            keywords=["Gratis", "sueldo", "[gratis]"],
        )
        _, operations = self.operations()
        keywords = self.created(operations, "shared_criterion_operation")
        self.assertEqual(
            [(k.keyword.text, k.keyword.match_type.name) for k in keywords],
            [("sueldo", "PHRASE"), ("gratis", "EXACT")],
        )
        self.assertEqual(keywords[0].shared_set, "customers/123/sharedSets/9")
        self.assertEqual(result["changes"][0]["already_in_list"], 1)

    def test_nothing_new(self):
        self.existing = [("gratis", "PHRASE")]
        with self.assertRaisesRegex(ToolError, "already has"):
            negative_lists.add_to_negative_keyword_list(
                customer_id="123", list_id="9", keywords=["gratis"]
            )

    def test_links_and_unlinks_campaigns(self):
        self.linked = [2]
        result = negative_lists.set_negative_keyword_list_campaigns(
            customer_id="123",
            list_id="9",
            add_campaign_ids=["1", "2"],
            remove_campaign_ids=["3"],
        )
        self.assertEqual(result["changes"][0]["applied_to"], ["Campaña 1"])
        self.assertEqual(result["changes"][0]["removed_from"], [])
        _, operations = self.operations()
        self.assertEqual(len(operations), 1)

        self.services["GoogleAdsService"].mutate.reset_mock()
        result = negative_lists.set_negative_keyword_list_campaigns(
            customer_id="123", list_id="9", remove_campaign_ids=["2"]
        )
        _, operations = self.operations()
        self.assertEqual(
            operations[0].campaign_shared_set_operation.remove,
            "customers/123/campaignSharedSets/2~9",
        )

    def test_nothing_to_link(self):
        self.linked = [1]
        with self.assertRaisesRegex(ToolError, "already use"):
            negative_lists.set_negative_keyword_list_campaigns(
                customer_id="123", list_id="9", add_campaign_ids=["1"]
            )


class TestListLists(NegativeListTestCase):

    def test_lists_with_campaigns_and_keywords(self):
        self.linked = [1, 2]
        self.existing = [("gratis", "PHRASE")]
        result = negative_lists.list_negative_keyword_lists(
            customer_id="123", include_keywords=True
        )
        self.assertEqual(
            result["lists"],
            [
                {
                    "list_id": "9",
                    "name": "Genéricas",
                    "keywords": 1,
                    "campaigns": ["Campaña 1", "Campaña 2"],
                    "keyword_list": [
                        {"text": "gratis", "match_type": "PHRASE"}
                    ],
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
