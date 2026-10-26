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

"""Test cases for the tools that change the targeting of campaigns."""

import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import targeting
from ads_mcp.tools.targeting import ScheduleSlot
from tests.tools.mutate_test import MutateToolTestCase

CAMPAIGN = "customers/123/campaigns/1"


class TargetingTestCase(MutateToolTestCase):

    def setUp(self):
        super().setUp()
        self.criteria = []
        self.services["GoogleAdsService"].search.side_effect = self.rows_for

    def rows_for(self, customer_id, query):
        if "FROM campaign_criterion" in query:
            return self.criteria
        return [
            self.make_row(
                {
                    "campaign.resource_name": CAMPAIGN,
                    "campaign.name": "IA Perú",
                }
            )
        ]

    def criterion(self, criterion_id, **fields):
        row = self.make_row(
            {
                "campaign_criterion.resource_name": f"{CAMPAIGN}~{criterion_id}".replace(
                    "campaigns/", "campaignCriteria/"
                ),
                **{f"campaign_criterion.{k}": v for k, v in fields.items()},
            }
        )
        return row

    def sent(self):
        request = self.sent_request(
            "CampaignCriterionService", "mutate_campaign_criteria"
        )
        return request, list(request.operations)

    def args(self, **kwargs):
        return {"customer_id": "123", "campaign_id": "1", **kwargs}


class TestLocations(TargetingTestCase):

    def test_adds_excludes_and_removes(self):
        self.criteria = [
            self.criterion(
                "2604",
                **{"location.geo_target_constant": "geoTargetConstants/2604"},
            ),
            self.criterion(
                "2170",
                **{"location.geo_target_constant": "geoTargetConstants/2170"},
            ),
        ]
        result = targeting.update_campaign_locations(
            **self.args(
                add_location_ids=["2218"],
                exclude_location_ids=["2170"],
                remove_location_ids=["2604"],
            )
        )
        request, operations = self.sent()
        self.assertTrue(request.validate_only)
        removed = [op.remove for op in operations if op.remove]
        created = [op.create for op in operations if not op.remove]
        self.assertEqual(
            removed,
            [
                "customers/123/campaignCriteria/1~2604",
                "customers/123/campaignCriteria/1~2170",
            ],
        )
        self.assertEqual(
            [(c.location.geo_target_constant, c.negative) for c in created],
            [
                ("geoTargetConstants/2218", False),
                ("geoTargetConstants/2170", True),
            ],
        )
        self.assertEqual(result["changes"][0]["targeted_after"], ["2218"])

    def test_refuses_to_target_every_country(self):
        self.criteria = [
            self.criterion(
                "2604",
                **{"location.geo_target_constant": "geoTargetConstants/2604"},
            )
        ]
        with self.assertRaisesRegex(ToolError, "every country"):
            targeting.update_campaign_locations(
                **self.args(remove_location_ids=["2604"])
            )
        targeting.update_campaign_locations(
            **self.args(remove_location_ids=["2604"], allow_all_locations=True)
        )
        _, operations = self.sent()
        self.assertEqual(len(operations), 1)

    def test_invalid_requests(self):
        with self.assertRaisesRegex(ToolError, "Give locations"):
            targeting.update_campaign_locations(**self.args())
        with self.assertRaisesRegex(ToolError, "more than one list"):
            targeting.update_campaign_locations(
                **self.args(add_location_ids=["1"], exclude_location_ids=["1"])
            )
        with self.assertRaisesRegex(ToolError, "not targeted"):
            targeting.update_campaign_locations(
                **self.args(
                    add_location_ids=["2604"], remove_location_ids=["2170"]
                )
            )


class TestLanguages(TargetingTestCase):

    def test_replaces_the_languages(self):
        self.criteria = [
            self.criterion(
                "1003",
                **{"language.language_constant": "languageConstants/1003"},
            ),
            self.criterion(
                "1014",
                **{"language.language_constant": "languageConstants/1014"},
            ),
        ]
        result = targeting.set_campaign_languages(
            **self.args(language_ids=["1003", "1000"])
        )
        _, operations = self.sent()
        self.assertEqual(
            [op.remove for op in operations if op.remove],
            ["customers/123/campaignCriteria/1~1014"],
        )
        self.assertEqual(
            [
                op.create.language.language_constant
                for op in operations
                if not op.remove
            ],
            ["languageConstants/1000"],
        )
        self.assertEqual(
            result["changes"][0]["languages"],
            {"from": ["1003", "1014"], "to": ["1003", "1000"]},
        )

    def test_nothing_to_change(self):
        self.criteria = [
            self.criterion(
                "1003",
                **{"language.language_constant": "languageConstants/1003"},
            )
        ]
        with self.assertRaisesRegex(ToolError, "already"):
            targeting.set_campaign_languages(**self.args(language_ids=["1003"]))


class TestAdSchedule(TargetingTestCase):

    def test_replaces_the_schedule(self):
        self.criteria = [self.criterion("300000")]
        targeting.set_ad_schedule(
            **self.args(
                schedule=[
                    ScheduleSlot(
                        days=["MONDAY", "FRIDAY"],
                        start_time="8:15",
                        end_time="22:30",
                        bid_adjustment_percent=20,
                    ),
                    ScheduleSlot(
                        days=["SATURDAY"], start_time="10:00", end_time="24:00"
                    ),
                ]
            )
        )
        _, operations = self.sent()
        self.assertEqual(
            operations[0].remove, "customers/123/campaignCriteria/1~300000"
        )
        monday, friday, saturday = [op.create for op in operations[1:]]
        self.assertEqual(monday.ad_schedule.day_of_week.name, "MONDAY")
        self.assertEqual(monday.ad_schedule.start_hour, 8)
        self.assertEqual(monday.ad_schedule.start_minute.name, "FIFTEEN")
        self.assertEqual(monday.ad_schedule.end_minute.name, "THIRTY")
        self.assertAlmostEqual(monday.bid_modifier, 1.2)
        self.assertEqual(friday.ad_schedule.day_of_week.name, "FRIDAY")
        self.assertEqual(saturday.ad_schedule.end_hour, 24)
        self.assertNotIn("bid_modifier", saturday)

    def test_invalid_windows(self):
        cases = [
            ("08:10", "10:00", "minutes"),
            ("10:00", "09:00", "ends before"),
            ("24:15", "24:30", "hours"),
            ("8", "9:00", "look like"),
        ]
        for start, end, message in cases:
            with self.subTest(start=start, end=end):
                with self.assertRaisesRegex(ToolError, message):
                    targeting.set_ad_schedule(
                        **self.args(
                            schedule=[
                                ScheduleSlot(
                                    days=["MONDAY"],
                                    start_time=start,
                                    end_time=end,
                                )
                            ]
                        )
                    )
        with self.assertRaisesRegex(ToolError, "overlap on MONDAY"):
            targeting.set_ad_schedule(
                **self.args(
                    schedule=[
                        ScheduleSlot(
                            days=["MONDAY"],
                            start_time="08:00",
                            end_time="12:00",
                        ),
                        ScheduleSlot(
                            days=["MONDAY"],
                            start_time="11:00",
                            end_time="13:00",
                        ),
                    ]
                )
            )
        with self.assertRaisesRegex(ToolError, "-90"):
            targeting.set_ad_schedule(
                **self.args(
                    schedule=[
                        ScheduleSlot(
                            days=["MONDAY"],
                            start_time="08:00",
                            end_time="12:00",
                            bid_adjustment_percent=-95,
                        )
                    ]
                )
            )


class TestDeviceAdjustments(TargetingTestCase):

    def test_updates_existing_and_creates_missing(self):
        enums = self.client.enums
        self.criteria = [
            self.criterion(
                "30001",
                **{
                    "device.type_": enums.DeviceEnum.MOBILE,
                    "bid_modifier": 1.25,
                },
            )
        ]
        result = targeting.set_device_bid_adjustments(
            **self.args(mobile_percent=0, tablet_percent=-100)
        )
        _, operations = self.sent()
        mobile, tablet = operations
        self.assertEqual(
            mobile.update.resource_name,
            "customers/123/campaignCriteria/1~30001",
        )
        self.assertEqual(mobile.update.bid_modifier, 1.0)
        self.assertEqual(list(mobile.update_mask.paths), ["bid_modifier"])
        self.assertEqual(tablet.create.device.type_.name, "TABLET")
        self.assertIn("bid_modifier", tablet.create)
        self.assertEqual(tablet.create.bid_modifier, 0.0)
        self.assertEqual(
            result["changes"][0]["devices"],
            {
                "MOBILE": {"from_percent": 25.0, "to_percent": 0},
                "TABLET": {"from_percent": 0, "to_percent": -100},
            },
        )

    def test_invalid_adjustments(self):
        with self.assertRaisesRegex(ToolError, "at least one device"):
            targeting.set_device_bid_adjustments(**self.args())
        with self.assertRaisesRegex(ToolError, "mobile_percent"):
            targeting.set_device_bid_adjustments(
                **self.args(mobile_percent=950)
            )


if __name__ == "__main__":
    unittest.main()
