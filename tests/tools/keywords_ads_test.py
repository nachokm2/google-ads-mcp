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

"""Test cases for keyword status, ad removal and ad replacement."""

import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import keywords_ads
from tests.tools.create_test import CreateToolTestCase


class TestKeywordStatus(CreateToolTestCase):

    def setUp(self):
        super().setUp()
        enums = self.client.enums
        self.set_rows(
            *[
                self.make_row(
                    {
                        "ad_group.name": "IA",
                        "ad_group_criterion.resource_name": f"customers/123/adGroupCriteria/5~{cid}",
                        "ad_group_criterion.criterion_id": cid,
                        "ad_group_criterion.keyword.text": text,
                        "ad_group_criterion.keyword.match_type": enums.KeywordMatchTypeEnum[
                            match_type
                        ],
                        "ad_group_criterion.status": enums.AdGroupCriterionStatusEnum[
                            status
                        ],
                    }
                )
                for cid, text, match_type, status in (
                    (1, "magister ia", "PHRASE", "ENABLED"),
                    (2, "magister ia", "EXACT", "PAUSED"),
                    (3, "postgrado", "BROAD", "ENABLED"),
                )
            ]
        )

    def change(self, **kwargs):
        return keywords_ads.set_keyword_status(
            customer_id="123", ad_group_id="5", **kwargs
        )

    def sent(self):
        request = self.sent_request(
            "AdGroupCriterionService", "mutate_ad_group_criteria"
        )
        return list(request.operations)

    def test_pauses_by_text_in_every_match_type(self):
        result = self.change(status="PAUSED", keyword_texts=["Magister  IA"])
        (operation,) = self.sent()
        self.assertEqual(
            operation.update.resource_name, "customers/123/adGroupCriteria/5~1"
        )
        self.assertEqual(operation.update.status.name, "PAUSED")
        self.assertEqual(list(operation.update_mask.paths), ["status"])
        self.assertIn("negative = FALSE", self.query())
        (change,) = result["changes"][0]["keywords"]
        self.assertEqual(change["status"], {"from": "ENABLED", "to": "PAUSED"})

    def test_removes_by_id(self):
        self.change(status="REMOVED", criterion_ids=["2", "3"])
        operations = self.sent()
        self.assertEqual(
            [op.remove for op in operations],
            [
                "customers/123/adGroupCriteria/5~2",
                "customers/123/adGroupCriteria/5~3",
            ],
        )

    def test_errors(self):
        with self.assertRaisesRegex(ToolError, "Give criterion_ids"):
            self.change(status="PAUSED")
        with self.assertRaisesRegex(ToolError, "not found.*'99'"):
            self.change(status="PAUSED", criterion_ids=["1", "99"])
        with self.assertRaisesRegex(ToolError, "already ENABLED"):
            self.change(status="ENABLED", criterion_ids=["1", "3"])


class TestAds(CreateToolTestCase):

    def ad_row(self, ad_type="RESPONSIVE_SEARCH_AD"):
        enums = self.client.enums
        row = self.make_row(
            {
                "ad_group.name": "IA",
                "ad_group_ad.resource_name": "customers/123/adGroupAds/5~77",
                "ad_group_ad.status": enums.AdGroupAdStatusEnum.ENABLED,
                "ad_group_ad.ad.type_": enums.AdTypeEnum[ad_type],
                "ad_group_ad.ad.responsive_search_ad.path1": "ia",
            }
        )
        ad = row.ad_group_ad.ad
        ad.final_urls.append("https://carver.edu/ia")
        for text in ("Magíster IA 2026", "100% Online", "Postula Hoy"):
            asset = self.client.get_type("AdTextAsset")
            asset.text = text
            ad.responsive_search_ad.headlines.append(asset)
        ad.responsive_search_ad.headlines[0].pinned_field = (
            enums.ServedAssetFieldTypeEnum.HEADLINE_1
        )
        for text in ("Admisión 2026 abierta.", "Clases en vivo."):
            asset = self.client.get_type("AdTextAsset")
            asset.text = text
            ad.responsive_search_ad.descriptions.append(asset)
        return row

    def test_replaces_with_edited_copy_and_pauses_the_old_ad(self):
        self.set_rows(self.ad_row())
        result = keywords_ads.replace_responsive_search_ad(
            customer_id="123",
            ad_group_id="5",
            ad_id="77",
            text_replacements={"2026": "2027"},
            path2="online",
        )
        request, operations = self.operations()
        self.assertTrue(request.validate_only)
        new_ad, old_ad = operations
        created = new_ad.ad_group_ad_operation.create
        self.assertEqual(created.ad_group, "customers/123/adGroups/5")
        rsa = created.ad.responsive_search_ad
        self.assertEqual(rsa.headlines[0].text, "Magíster IA 2027")
        self.assertEqual(rsa.headlines[0].pinned_field.name, "HEADLINE_1")
        self.assertEqual(rsa.descriptions[0].text, "Admisión 2027 abierta.")
        self.assertEqual((rsa.path1, rsa.path2), ("ia", "online"))
        self.assertEqual(list(created.ad.final_urls), ["https://carver.edu/ia"])
        update = old_ad.ad_group_ad_operation
        self.assertEqual(
            update.update.resource_name, "customers/123/adGroupAds/5~77"
        )
        self.assertEqual(update.update.status.name, "PAUSED")
        self.assertEqual(result["changes"][0]["old_ad"], "paused")

    def test_new_texts_and_removing_the_old_ad(self):
        self.set_rows(self.ad_row())
        keywords_ads.replace_responsive_search_ad(
            customer_id="123",
            ad_group_id="5",
            ad_id="77",
            headlines=["Nuevo título", "Otro título", "Tercero"],
            final_url="https://carver.edu/ia-2027",
            path1="",
            old_ad_action="REMOVE",
        )
        _, (new_ad, old_ad) = self.operations()
        rsa = new_ad.ad_group_ad_operation.create.ad.responsive_search_ad
        self.assertEqual(
            [h.text for h in rsa.headlines],
            ["Nuevo título", "Otro título", "Tercero"],
        )
        self.assertFalse(rsa.headlines[0].pinned_field)
        self.assertEqual(rsa.path1, "")
        self.assertEqual(
            old_ad.ad_group_ad_operation.remove, "customers/123/adGroupAds/5~77"
        )

    def test_rejects_invalid_replacements(self):
        self.set_rows(self.ad_row())
        with self.assertRaisesRegex(ToolError, "longer than 30"):
            keywords_ads.replace_responsive_search_ad(
                customer_id="123",
                ad_group_id="5",
                ad_id="77",
                text_replacements={"IA": "Inteligencia Artificial"},
            )
        with self.assertRaisesRegex(ToolError, "at least one change"):
            keywords_ads.replace_responsive_search_ad(
                customer_id="123", ad_group_id="5", ad_id="77"
            )
        self.set_rows(self.ad_row("EXPANDED_TEXT_AD"))
        with self.assertRaisesRegex(ToolError, "only responsive search ads"):
            keywords_ads.replace_responsive_search_ad(
                customer_id="123", ad_group_id="5", ad_id="77", path1="x"
            )

    def test_removes_an_ad(self):
        self.set_rows(self.ad_row())
        result = keywords_ads.remove_ad(
            customer_id="123", ad_group_id="5", ad_id="77"
        )
        request = self.sent_request("AdGroupAdService", "mutate_ad_group_ads")
        self.assertEqual(
            request.operations[0].remove, "customers/123/adGroupAds/5~77"
        )
        self.assertEqual(
            result["changes"][0]["status"], {"from": "ENABLED", "to": "REMOVED"}
        )


if __name__ == "__main__":
    unittest.main()
