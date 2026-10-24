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

"""Test cases for the tools that create Performance Max campaigns."""

import re
import unittest
from collections import Counter
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp.tools import create_pmax
from tests.assets_test import png
from tests.tools.create_test import CreateToolTestCase
from tests.tools.mutate_test import list_tools

PMAX_TOOL_NAMES = [
    "mutate_create_asset_group",
    "mutate_create_performance_max_campaign",
]

LANDSCAPE = "https://img.example/1200x628.png"
SQUARE = "https://img.example/1200x1200.png"
PORTRAIT = "https://img.example/960x1200.png"
LOGO = "https://img.example/600x600.png"


def fake_download(url):
    """Returns a PNG whose size is the WIDTHxHEIGHT written in the URL."""
    width, height = map(int, re.search(r"(\d+)x(\d+)", url).groups())
    return png(width, height)


def _kind(operation):
    return type(operation).pb(operation).WhichOneof("operation")


class PmaxToolTestCase(CreateToolTestCase):

    def setUp(self):
        super().setUp()
        patcher = patch(
            "ads_mcp.tools.create_pmax.download_image",
            side_effect=fake_download,
        )
        self.download = patcher.start()
        self.addCleanup(patcher.stop)

    def links(self, operations, kind):
        """Maps each linked asset field type to the linked asset names."""
        result = {}
        for link in self.created(operations, kind):
            result.setdefault(link.field_type.name, []).append(link.asset)
        return result


class TestCreatePerformanceMaxCampaign(PmaxToolTestCase):

    def setUp(self):
        super().setUp()
        self.set_rows(self.make_row({"customer.currency_code": "CLP"}))

    def create(self, **overrides):
        args = {
            "customer_id": "123",
            "campaign_name": "PMax Magíster",
            "daily_budget": 8000,
            "final_url": "https://www.uautonoma.cl/postgrados",
            "business_name": "Universidad Autónoma",
            "headlines": ["Postgrados UA", "Magíster Online", "Postula Hoy"],
            "long_headlines": ["Magíster y diplomados para profesionales"],
            "descriptions": ["Clases flexibles.", "Postula en línea hoy."],
            "logo_images": [LOGO],
            "marketing_images": [LANDSCAPE],
            "square_marketing_images": [SQUARE],
        }
        args.update(overrides)
        return create_pmax.create_performance_max_campaign(**args)

    def test_builds_campaign_brand_assets_and_asset_group(self):
        result = self.create(
            portrait_marketing_images=[PORTRAIT],
            youtube_videos=["https://youtu.be/dQw4w9WgXcQ"],
        )

        request, operations = self.operations()
        self.assertTrue(request.validate_only)
        kinds = [_kind(op) for op in operations]
        # Resources come before the links that reference them.
        self.assertEqual(
            kinds[:2], ["campaign_budget_operation", "campaign_operation"]
        )
        self.assertLess(
            max(i for i, k in enumerate(kinds) if k == "asset_operation"),
            kinds.index("campaign_asset_operation"),
        )
        self.assertLess(
            max(
                i
                for i, k in enumerate(kinds)
                if k == "campaign_asset_operation"
            ),
            kinds.index("asset_group_operation"),
        )
        self.assertLess(
            kinds.index("asset_group_operation"),
            kinds.index("asset_group_asset_operation"),
        )

        (campaign,) = self.created(operations, "campaign_operation")
        self.assertEqual(
            campaign.advertising_channel_type.name, "PERFORMANCE_MAX"
        )
        self.assertTrue(campaign.brand_guidelines_enabled)
        self.assertEqual(campaign.status.name, "ENABLED")
        self.assertEqual(
            type(campaign).pb(campaign).WhichOneof("campaign_bidding_strategy"),
            "maximize_conversions",
        )
        (budget,) = self.created(operations, "campaign_budget_operation")
        self.assertFalse(budget.explicitly_shared)

        assets = {
            a.resource_name: a
            for a in self.created(operations, "asset_operation")
        }
        campaign_links = self.links(operations, "campaign_asset_operation")
        self.assertEqual(set(campaign_links), {"BUSINESS_NAME", "LOGO"})
        (business_name,) = campaign_links["BUSINESS_NAME"]
        self.assertEqual(
            assets[business_name].text_asset.text, "Universidad Autónoma"
        )
        (logo,) = campaign_links["LOGO"]
        self.assertEqual(assets[logo].image_asset.data, png(600, 600))

        (asset_group,) = self.created(operations, "asset_group_operation")
        self.assertEqual(asset_group.campaign, campaign.resource_name)
        self.assertEqual(
            list(asset_group.final_urls),
            ["https://www.uautonoma.cl/postgrados"],
        )
        self.assertEqual(asset_group.name, "PMax Magíster")
        for link in self.created(operations, "asset_group_asset_operation"):
            self.assertEqual(link.asset_group, asset_group.resource_name)

        group_links = self.links(operations, "asset_group_asset_operation")
        self.assertEqual(
            Counter(
                {field: len(names) for field, names in group_links.items()}
            ),
            Counter(
                HEADLINE=3,
                LONG_HEADLINE=1,
                DESCRIPTION=2,
                MARKETING_IMAGE=1,
                SQUARE_MARKETING_IMAGE=1,
                PORTRAIT_MARKETING_IMAGE=1,
                YOUTUBE_VIDEO=1,
            ),
        )
        (video,) = group_links["YOUTUBE_VIDEO"]
        self.assertEqual(
            assets[video].youtube_video_asset.youtube_video_id, "dQw4w9WgXcQ"
        )

        change = result["changes"][0]
        self.assertEqual(change["currency"], "CLP")
        self.assertIn(
            {"field": "LOGO", "source": LOGO, "size": "600x600"},
            change["images"],
        )
        self.assertEqual(len(result["warnings"]), 2)

    def test_identical_assets_are_created_once(self):
        self.create(
            headlines=["Postgrados UA", "Magíster Online", "Postula Hoy"],
            long_headlines=["Postgrados UA"],
            square_marketing_images=[LOGO],
        )

        _, operations = self.operations()
        assets = self.created(operations, "asset_operation")
        texts = [a.text_asset.text for a in assets if a.text_asset.text]
        self.assertEqual(texts.count("Postgrados UA"), 1)
        images = [a for a in assets if a.image_asset.data]
        self.assertEqual(
            len(images), 2
        )  # landscape image and the shared square.

    def test_uses_existing_image_assets(self):
        asset_row = self.make_row(
            {
                "asset.id": 555,
                "asset.resource_name": "customers/123/assets/555",
                "asset.type_": self.client.enums.AssetTypeEnum.IMAGE,
                "asset.image_asset.full_size.width_pixels": 1200,
                "asset.image_asset.full_size.height_pixels": 1200,
            }
        )
        self.services["GoogleAdsService"].search.side_effect = [
            [self.make_row({"customer.currency_code": "CLP"})],
            [asset_row],
        ]

        self.create(
            logo_images=["555"],
            square_marketing_images=["customers/123/assets/555"],
        )

        search_queries = [
            c.kwargs["query"]
            for c in self.services["GoogleAdsService"].search.call_args_list
        ]
        self.assertIn("asset.id IN (555)", search_queries[1])
        _, operations = self.operations()
        self.assertEqual(
            self.download.call_count, 1
        )  # only the landscape image.
        self.assertEqual(
            self.links(operations, "campaign_asset_operation")["LOGO"],
            ["customers/123/assets/555"],
        )

    def test_maximize_conversion_value_with_target_roas(self):
        self.create(
            bidding_strategy="MAXIMIZE_CONVERSION_VALUE", target_roas=3.5
        )

        _, operations = self.operations()
        (campaign,) = self.created(operations, "campaign_operation")
        self.assertEqual(campaign.maximize_conversion_value.target_roas, 3.5)

    def test_rejects_invalid_input(self):
        cases = {
            "wrong ratio": {"marketing_images": [SQUARE]},
            "small logo": {"logo_images": ["https://img.example/100x100.png"]},
            "no square images": {"square_marketing_images": []},
            "too many logos": {
                "logo_images": [
                    LOGO,
                    "https://img.example/700x700.png",
                    "https://img.example/800x800.png",
                ],
                "landscape_logo_images": [
                    "https://img.example/1200x300.png",
                    "https://img.example/1600x400.png",
                    "https://img.example/2000x500.png",
                ],
            },
            "long business name": {
                "business_name": "Universidad Autónoma de Chile"
            },
            "no long headline": {"long_headlines": []},
            "bad video": {"youtube_videos": ["https://vimeo.com/1"]},
            "roas with conversions": {"target_roas": 3.0},
            "cpa with value": {
                "bidding_strategy": "MAXIMIZE_CONVERSION_VALUE",
                "target_cpa": 5000,
            },
            "bad asset id": {"logo_images": ["logo.png"]},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                with self.assertRaises(ToolError):
                    self.create(**overrides)
        self.services["GoogleAdsService"].mutate.assert_not_called()

    def test_error_names_the_image(self):
        with self.assertRaises(ToolError) as context:
            self.create(marketing_images=[SQUARE])

        self.assertIn("MARKETING_IMAGE", str(context.exception))
        self.assertIn(SQUARE, str(context.exception))

    def test_existing_asset_must_be_an_image(self):
        asset_row = self.make_row(
            {
                "asset.id": 777,
                "asset.resource_name": "customers/123/assets/777",
                "asset.type_": self.client.enums.AssetTypeEnum.TEXT,
            }
        )
        self.services["GoogleAdsService"].search.side_effect = [
            [self.make_row({"customer.currency_code": "CLP"})],
            [asset_row],
        ]

        with self.assertRaisesRegex(ToolError, "not an image asset"):
            self.create(logo_images=["777"])


class TestCreateAssetGroup(PmaxToolTestCase):

    def campaign_row(self, channel="PERFORMANCE_MAX", brand_guidelines=True):
        return self.make_row(
            {
                "campaign.resource_name": "customers/123/campaigns/456",
                "campaign.name": "PMax Magíster",
                "campaign.advertising_channel_type": self.client.enums.AdvertisingChannelTypeEnum[
                    channel
                ],
                "campaign.brand_guidelines_enabled": brand_guidelines,
            }
        )

    def create(self, **overrides):
        args = {
            "customer_id": "123",
            "campaign_id": "456",
            "asset_group_name": "Magíster en Educación",
            "final_url": "https://www.uautonoma.cl/educacion",
            "headlines": ["Magíster Educación", "Postgrados UA", "Postula Hoy"],
            "long_headlines": ["Especialízate en educación con la UA"],
            "descriptions": ["Clases flexibles.", "Postula en línea hoy."],
            "marketing_images": [LANDSCAPE],
            "square_marketing_images": [SQUARE],
        }
        args.update(overrides)
        return create_pmax.create_asset_group(**args)

    def test_creates_asset_group_in_campaign_with_brand_guidelines(self):
        self.set_rows(self.campaign_row())

        result = self.create(status="PAUSED")

        _, operations = self.operations()
        self.assertEqual(
            self.created(operations, "campaign_asset_operation"), []
        )
        (asset_group,) = self.created(operations, "asset_group_operation")
        self.assertEqual(asset_group.campaign, "customers/123/campaigns/456")
        self.assertEqual(asset_group.status.name, "PAUSED")
        group_links = self.links(operations, "asset_group_asset_operation")
        self.assertNotIn("BUSINESS_NAME", group_links)
        self.assertNotIn("LOGO", group_links)
        self.assertEqual(result["changes"][0]["campaign"], "PMax Magíster")

    def test_brand_assets_go_to_the_asset_group_without_brand_guidelines(self):
        self.set_rows(self.campaign_row(brand_guidelines=False))

        self.create(business_name="UA", logo_images=[LOGO])

        _, operations = self.operations()
        group_links = self.links(operations, "asset_group_asset_operation")
        self.assertEqual(len(group_links["BUSINESS_NAME"]), 1)
        self.assertEqual(len(group_links["LOGO"]), 1)

    def test_checks_campaign_and_brand_assets(self):
        cases = [
            ("search campaign", self.campaign_row(channel="SEARCH"), {}),
            (
                "brand assets with brand guidelines",
                self.campaign_row(),
                {"business_name": "UA", "logo_images": [LOGO]},
            ),
            (
                "missing brand assets",
                self.campaign_row(brand_guidelines=False),
                {},
            ),
        ]
        for label, row, overrides in cases:
            with self.subTest(label):
                self.set_rows(row)
                with self.assertRaises(ToolError):
                    self.create(**overrides)
        self.services["GoogleAdsService"].mutate.assert_not_called()


class TestPmaxToolsMounting(unittest.IsolatedAsyncioTestCase):

    async def test_pmax_tools_follow_the_mutate_namespace(self):
        default = [t.name for t in await list_tools("tools_config.yaml")]
        write = [t.name for t in await list_tools("tools_config_write.yaml")]

        for name in PMAX_TOOL_NAMES:
            self.assertNotIn(name, default)
            self.assertIn(name, write)


if __name__ == "__main__":
    unittest.main()
