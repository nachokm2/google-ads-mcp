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

"""Test cases for the tools that create Display and Demand Gen campaigns."""

import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import create_demand_gen, create_display
from tests.assets_test import png
from tests.tools.create_pmax_test import (
    LANDSCAPE,
    LOGO,
    PORTRAIT,
    SQUARE,
    PmaxToolTestCase,
)
from tests.tools.mutate_test import list_tools

STAGE3_TOOL_NAMES = [
    "mutate_create_demand_gen_image_campaign",
    "mutate_create_demand_gen_video_campaign",
    "mutate_create_display_campaign",
]

LANDSCAPE_LOGO = "https://img.example/1200x300.png"
TALL_PORTRAIT = "https://img.example/1080x1920.png"
VIDEO_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def _kind(operation):
    return type(operation).pb(operation).WhichOneof("operation")


def _bidding(campaign):
    return type(campaign).pb(campaign).WhichOneof("campaign_bidding_strategy")


class TestCreateDisplayCampaign(PmaxToolTestCase):

    def setUp(self):
        super().setUp()
        self.set_rows(self.make_row({"customer.currency_code": "CLP"}))

    def create(self, **overrides):
        args = {
            "customer_id": "123",
            "campaign_name": "Display Magíster",
            "daily_budget": 3000,
            "final_url": "https://www.uautonoma.cl",
            "business_name": "Universidad Autónoma",
            "headlines": ["Postgrados UA", "Magíster Online"],
            "long_headline": "Magíster y diplomados para profesionales",
            "descriptions": ["Clases flexibles. Postula hoy."],
            "marketing_images": [LANDSCAPE],
            "square_marketing_images": [SQUARE],
        }
        args.update(overrides)
        return create_display.create_display_campaign(**args)

    def test_builds_display_campaign_and_responsive_display_ad(self):
        result = self.create(
            square_logo_images=[LOGO],
            landscape_logo_images=[LANDSCAPE_LOGO],
            youtube_videos=[VIDEO_URL],
        )

        request, operations = self.operations()
        self.assertTrue(request.validate_only)
        kinds = [_kind(op) for op in operations]
        self.assertEqual(
            kinds[-2:], ["ad_group_operation", "ad_group_ad_operation"]
        )
        self.assertLess(
            max(i for i, k in enumerate(kinds) if k == "asset_operation"),
            kinds.index("ad_group_operation"),
        )

        (campaign,) = self.created(operations, "campaign_operation")
        self.assertEqual(campaign.advertising_channel_type.name, "DISPLAY")
        self.assertTrue(campaign.network_settings.target_content_network)
        self.assertFalse(campaign.network_settings.target_google_search)
        self.assertEqual(_bidding(campaign), "target_spend")
        self.assertEqual(
            len(self.created(operations, "campaign_criterion_operation")), 2
        )

        (ad_group,) = self.created(operations, "ad_group_operation")
        self.assertEqual(ad_group.type_.name, "DISPLAY_STANDARD")

        assets = {
            a.resource_name: a
            for a in self.created(operations, "asset_operation")
        }
        (ad_group_ad,) = self.created(operations, "ad_group_ad_operation")
        self.assertEqual(ad_group_ad.ad_group, ad_group.resource_name)
        ad = ad_group_ad.ad.responsive_display_ad
        self.assertEqual(
            list(ad_group_ad.ad.final_urls), ["https://www.uautonoma.cl"]
        )

        def data(images):
            return [assets[image.asset].image_asset.data for image in images]

        self.assertEqual(data(ad.marketing_images), [png(1200, 628)])
        self.assertEqual(data(ad.square_marketing_images), [png(1200, 1200)])
        # Square logos go to square_logo_images; 4:1 logos to logo_images.
        self.assertEqual(data(ad.square_logo_images), [png(600, 600)])
        self.assertEqual(data(ad.logo_images), [png(1200, 300)])
        (video,) = ad.youtube_videos
        self.assertEqual(
            assets[video.asset].youtube_video_asset.youtube_video_id,
            "jNQXAC9IVRw",
        )
        self.assertEqual(
            [h.text for h in ad.headlines], ["Postgrados UA", "Magíster Online"]
        )
        self.assertEqual(
            ad.long_headline.text, "Magíster y diplomados para profesionales"
        )
        self.assertEqual(ad.business_name, "Universidad Autónoma")
        self.assertIn("ENABLED", result["warnings"][0])

    def test_manual_cpc_bid_goes_to_the_ad_group(self):
        self.create(bidding_strategy="MANUAL_CPC", cpc_bid=200)

        _, operations = self.operations()
        (campaign,) = self.created(operations, "campaign_operation")
        self.assertEqual(_bidding(campaign), "manual_cpc")
        (ad_group,) = self.created(operations, "ad_group_operation")
        self.assertEqual(ad_group.cpc_bid_micros, 200_000_000)

    def test_rejects_invalid_input(self):
        many = [
            f"https://img.example/{600 + i}x{600 + i}.png" for i in range(15)
        ]
        cases = {
            "no square image": {"square_marketing_images": []},
            "too many images": {"square_marketing_images": many},
            "too many logos": {
                "square_logo_images": [LOGO, "https://img.example/700x700.png"],
                "landscape_logo_images": [
                    LANDSCAPE_LOGO,
                    "https://img.example/1600x400.png",
                    "https://img.example/2000x500.png",
                    "https://img.example/2400x600.png",
                ],
            },
            "long headline": {"headlines": ["x" * 31]},
            "long long headline": {"long_headline": "x" * 91},
            "too many videos": {
                "youtube_videos": [f"vid{i:08d}" for i in range(6)]
            },
            "manual cpc without bid": {"bidding_strategy": "MANUAL_CPC"},
            "cpc bid with conversions": {
                "bidding_strategy": "MAXIMIZE_CONVERSIONS",
                "cpc_bid": 100,
            },
            "landscape logo with wrong ratio": {
                "landscape_logo_images": [LOGO]
            },
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                with self.assertRaises(ToolError):
                    self.create(**overrides)
        self.services["GoogleAdsService"].mutate.assert_not_called()


class DemandGenTestCase(PmaxToolTestCase):

    def set_minimum_budget(self, minimum_micros):
        self.services["GoogleAdsService"].search.side_effect = [
            [self.make_row({"customer.currency_code": "CLP"})],
            [
                self.make_row(
                    {
                        "currency_constant.demand_gen_info.minimum_budget_per_day_micros": minimum_micros
                    }
                )
            ],
        ]

    def setUp(self):
        super().setUp()
        self.set_minimum_budget(1_000_000_000)

    def ad_group(self, operations):
        (ad_group,) = self.created(operations, "ad_group_operation")
        return ad_group

    def selected_channels(self, ad_group):
        channels = (
            ad_group.demand_gen_ad_group_settings.channel_controls.selected_channels
        )
        return {
            name
            for name in (
                "youtube_in_stream",
                "youtube_in_feed",
                "youtube_shorts",
                "discover",
                "gmail",
                "display",
                "maps",
            )
            if getattr(channels, name)
        }


class TestCreateDemandGenVideoCampaign(DemandGenTestCase):

    def create(self, **overrides):
        args = {
            "customer_id": "123",
            "campaign_name": "YouTube Magíster",
            "daily_budget": 5000,
            "final_url": "https://www.uautonoma.cl",
            "business_name": "Universidad Autónoma",
            "youtube_videos": [VIDEO_URL],
            "headlines": ["Magíster y diplomados para profesionales"],
            "long_headlines": ["Especialízate con la Universidad Autónoma"],
            "descriptions": ["Clases flexibles. Postula hoy."],
            "logo_images": [LOGO],
        }
        args.update(overrides)
        return create_demand_gen.create_demand_gen_video_campaign(**args)

    def test_builds_youtube_only_video_campaign(self):
        result = self.create()

        request, operations = self.operations()
        self.assertTrue(request.validate_only)
        (campaign,) = self.created(operations, "campaign_operation")
        self.assertEqual(campaign.advertising_channel_type.name, "DEMAND_GEN")
        self.assertEqual(_bidding(campaign), "maximize_conversions")
        # Demand Gen targets locations and languages on the ad group.
        self.assertEqual(
            self.created(operations, "campaign_criterion_operation"), []
        )
        criteria = self.created(operations, "ad_group_criterion_operation")
        self.assertEqual(
            [c.location.geo_target_constant for c in criteria][0],
            "geoTargetConstants/2152",
        )
        self.assertEqual(
            criteria[1].language.language_constant, "languageConstants/1003"
        )

        ad_group = self.ad_group(operations)
        self.assertEqual(ad_group.type_.name, "UNSPECIFIED")
        self.assertEqual(
            ad_group.demand_gen_ad_group_settings.channel_controls.channel_config.name,
            "SELECTED_CHANNELS",
        )
        self.assertEqual(
            self.selected_channels(ad_group),
            {"youtube_in_stream", "youtube_in_feed", "youtube_shorts"},
        )

        assets = {
            a.resource_name: a
            for a in self.created(operations, "asset_operation")
        }
        (ad_group_ad,) = self.created(operations, "ad_group_ad_operation")
        self.assertEqual(ad_group_ad.ad_group, ad_group.resource_name)
        self.assertEqual(ad_group_ad.ad.name, "YouTube Magíster video")
        video_ad = ad_group_ad.ad.demand_gen_video_responsive_ad
        (video,) = video_ad.videos
        self.assertEqual(
            assets[video.asset].youtube_video_asset.youtube_video_id,
            "jNQXAC9IVRw",
        )
        (logo,) = video_ad.logo_images
        self.assertEqual(assets[logo.asset].image_asset.data, png(600, 600))
        self.assertEqual(video_ad.business_name.text, "Universidad Autónoma")
        self.assertEqual(len(video_ad.headlines), 1)
        self.assertEqual(len(video_ad.long_headlines), 1)
        self.assertEqual(
            result["changes"][0]["channels"],
            create_demand_gen.DEFAULT_VIDEO_CHANNELS,
        )
        self.assertEqual(len(result["warnings"]), 2)

    def test_custom_channels_and_bidding(self):
        self.create(
            channels=["YOUTUBE_IN_FEED", "DISCOVER", "DISCOVER"],
            bidding_strategy="MAXIMIZE_CONVERSION_VALUE",
            target_roas=4.0,
        )

        _, operations = self.operations()
        self.assertEqual(
            self.selected_channels(self.ad_group(operations)),
            {"youtube_in_feed", "discover"},
        )
        (campaign,) = self.created(operations, "campaign_operation")
        self.assertEqual(campaign.maximize_conversion_value.target_roas, 4.0)

    def test_rejects_budget_below_demand_gen_minimum(self):
        self.set_minimum_budget(10_000_000_000)

        with self.assertRaisesRegex(ToolError, "at least 10000 CLP"):
            self.create()
        self.services["GoogleAdsService"].mutate.assert_not_called()

    def test_rejects_invalid_input(self):
        cases = {
            "no video": {"youtube_videos": []},
            "not youtube": {"youtube_videos": ["https://vimeo.com/1"]},
            "headline over 40": {"headlines": ["x" * 41]},
            "no logo": {"logo_images": []},
            "no channels": {"channels": []},
            "roas with conversions": {"target_roas": 2.0},
            "long business name": {"business_name": "x" * 26},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                with self.assertRaises(ToolError):
                    self.create(**overrides)
        self.services["GoogleAdsService"].mutate.assert_not_called()


class TestCreateDemandGenImageCampaign(DemandGenTestCase):

    def create(self, **overrides):
        args = {
            "customer_id": "123",
            "campaign_name": "Imágenes Magíster",
            "daily_budget": 5000,
            "final_url": "https://www.uautonoma.cl",
            "business_name": "Universidad Autónoma",
            "headlines": ["Postgrados UA"],
            "descriptions": ["Clases flexibles. Postula hoy."],
            "logo_images": [LOGO],
            "marketing_images": [LANDSCAPE],
        }
        args.update(overrides)
        return create_demand_gen.create_demand_gen_image_campaign(**args)

    def test_builds_image_campaign_with_every_image_ratio(self):
        self.create(
            square_marketing_images=[SQUARE],
            portrait_marketing_images=[PORTRAIT],
            tall_portrait_marketing_images=[TALL_PORTRAIT],
            bidding_strategy="MAXIMIZE_CLICKS",
            cpc_bid=500,
        )

        _, operations = self.operations()
        (campaign,) = self.created(operations, "campaign_operation")
        self.assertEqual(
            campaign.target_spend.cpc_bid_ceiling_micros, 500_000_000
        )
        self.assertEqual(
            self.selected_channels(self.ad_group(operations)),
            {"youtube_in_feed", "discover", "gmail"},
        )
        assets = {
            a.resource_name: a
            for a in self.created(operations, "asset_operation")
        }
        (ad_group_ad,) = self.created(operations, "ad_group_ad_operation")
        image_ad = ad_group_ad.ad.demand_gen_multi_asset_ad

        def data(images):
            return [assets[image.asset].image_asset.data for image in images]

        self.assertEqual(data(image_ad.marketing_images), [png(1200, 628)])
        self.assertEqual(
            data(image_ad.square_marketing_images), [png(1200, 1200)]
        )
        self.assertEqual(
            data(image_ad.portrait_marketing_images), [png(960, 1200)]
        )
        self.assertEqual(
            data(image_ad.tall_portrait_marketing_images), [png(1080, 1920)]
        )
        self.assertEqual(data(image_ad.logo_images), [png(600, 600)])
        self.assertEqual(image_ad.business_name, "Universidad Autónoma")

    def test_rejects_invalid_input(self):
        many = [
            f"https://img.example/{600 + i}x{600 + i}.png" for i in range(20)
        ]
        cases = {
            "no landscape or square": {
                "marketing_images": [],
                "portrait_marketing_images": [PORTRAIT],
            },
            "too many images": {"square_marketing_images": many},
            "wrong tall ratio": {"tall_portrait_marketing_images": [PORTRAIT]},
            "headline over 30": {"headlines": ["x" * 31]},
            "no logo": {"logo_images": []},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                with self.assertRaises(ToolError):
                    self.create(**overrides)
        self.services["GoogleAdsService"].mutate.assert_not_called()


class TestStage3ToolsMounting(unittest.IsolatedAsyncioTestCase):

    async def test_tools_follow_the_mutate_namespace(self):
        default = [t.name for t in await list_tools("tools_config.yaml")]
        write = [t.name for t in await list_tools("tools_config_write.yaml")]

        for name in STAGE3_TOOL_NAMES:
            self.assertNotIn(name, default)
            self.assertIn(name, write)


if __name__ == "__main__":
    unittest.main()
