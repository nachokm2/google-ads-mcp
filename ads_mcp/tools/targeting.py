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

"""Tools that change the targeting of existing campaigns: locations,
languages, ad schedule and device bid adjustments.

They are registered in the `mutate` namespace.
"""

import re
from typing import Any, Dict, List, Literal

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from pydantic import BaseModel, ConfigDict, Field

import ads_mcp.utils as utils
from ads_mcp.tools.create_pmax import _search
from ads_mcp.tools.mutate import (
    _UPDATE_ANNOTATIONS,
    _clean_id,
    _find_one,
    _result,
    _send,
    _update_operation,
    mutate_mcp,
)

Day = Literal[
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
]
_MINUTES = {0: "ZERO", 15: "FIFTEEN", 30: "THIRTY", 45: "FORTY_FIVE"}
_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")
MIN_SCHEDULE_ADJUSTMENT = -90
MAX_ADJUSTMENT = 900


def _campaign(client: GoogleAdsClient, customer_id: str, campaign_id: str):
    return _find_one(
        client,
        customer_id,
        "SELECT campaign.resource_name, campaign.name, "
        "campaign.advertising_channel_type FROM campaign "
        f"WHERE campaign.id = {campaign_id}",
        f"Campaign {campaign_id} was not found in customer {customer_id}.",
    ).campaign


def _criteria(
    client: GoogleAdsClient, customer_id: str, campaign_id: str, kind: str
):
    return _search(
        client,
        customer_id,
        "SELECT campaign_criterion.resource_name, campaign_criterion.negative, "
        "campaign_criterion.bid_modifier, "
        "campaign_criterion.location.geo_target_constant, "
        "campaign_criterion.language.language_constant, "
        "campaign_criterion.device.type, "
        "campaign_criterion.ad_schedule.day_of_week, "
        "campaign_criterion.ad_schedule.start_hour, "
        "campaign_criterion.ad_schedule.start_minute, "
        "campaign_criterion.ad_schedule.end_hour, "
        "campaign_criterion.ad_schedule.end_minute FROM campaign_criterion "
        f"WHERE campaign.id = {campaign_id} AND campaign_criterion.type = "
        f"'{kind}' AND campaign_criterion.status != 'REMOVED'",
    )


def _ids(values: List[str | int], name: str) -> List[str]:
    return list(dict.fromkeys(_clean_id(value, name) for value in values))


def _remove_operation(client: GoogleAdsClient, resource_name: str):
    operation = client.get_type("CampaignCriterionOperation")
    operation.remove = resource_name
    return operation


def _create_operation(client: GoogleAdsClient, campaign_resource_name: str):
    operation = client.get_type("CampaignCriterionOperation")
    operation.create.campaign = campaign_resource_name
    return operation, operation.create


def _modifier(percent: float, name: str, minimum: float) -> float:
    if not minimum <= percent <= MAX_ADJUSTMENT:
        raise ToolError(
            f"{name} must be between {minimum:g} and {MAX_ADJUSTMENT}, got "
            f"{percent:g}."
        )
    return round(1 + percent / 100, 4)


@mutate_mcp.tool(annotations=_UPDATE_ANNOTATIONS)
def update_campaign_locations(
    customer_id: str | int,
    campaign_id: str | int,
    add_location_ids: List[str | int] = [],
    remove_location_ids: List[str | int] = [],
    exclude_location_ids: List[str | int] = [],
    allow_all_locations: bool = False,
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Adds, removes or excludes locations of a campaign.

    add_location_ids starts showing ads in those places, exclude_location_ids
    stops showing them there (for example a city inside a targeted country)
    and remove_location_ids deletes a targeted or excluded location. Find
    location ids with the search tool on the geo_target_constant resource,
    for example 2152 Chile, 2604 Peru, 2170 Colombia, 2218 Ecuador and 2214
    Dominican Republic.

    A campaign without targeted locations shows ads in every country, so the
    tool refuses to remove the last one unless allow_all_locations is true.

    By default the change is only validated, not applied. Show the returned
    changes to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer.
        campaign_id: The id of the campaign.
        add_location_ids: Geo target constant ids to start targeting.
        remove_location_ids: Geo target constant ids to stop targeting or excluding.
        exclude_location_ids: Geo target constant ids to exclude.
        allow_all_locations: Whether the campaign may end up targeting every country.
        validate_only: If true, validates the change without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    campaign_id = _clean_id(campaign_id, "campaign_id")
    add = _ids(add_location_ids, "add_location_ids")
    remove = _ids(remove_location_ids, "remove_location_ids")
    exclude = _ids(exclude_location_ids, "exclude_location_ids")
    if not add and not remove and not exclude:
        raise ToolError("Give locations to add, remove or exclude.")
    repeated = (set(add) & set(exclude)) | (set(remove) & set(add + exclude))
    if repeated:
        raise ToolError(
            f"These locations appear in more than one list: {sorted(repeated)}"
        )

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    campaign = _campaign(client, customer_id, campaign_id)
    current = {}
    for row in _criteria(client, customer_id, campaign_id, "LOCATION"):
        criterion = row.campaign_criterion
        location_id = criterion.location.geo_target_constant.rsplit("/", 1)[1]
        current[location_id] = criterion

    operations = []
    for location_id in remove:
        if location_id not in current:
            raise ToolError(
                f"Location {location_id} is not targeted or excluded by the "
                "campaign."
            )
        operations.append(
            _remove_operation(client, current[location_id].resource_name)
        )
    for location_id, negative in [(i, False) for i in add] + [
        (i, True) for i in exclude
    ]:
        if location_id in current:
            if current[location_id].negative == negative:
                continue
            # Switching between targeted and excluded replaces the criterion.
            operations.append(
                _remove_operation(client, current[location_id].resource_name)
            )
        operation, criterion = _create_operation(client, campaign.resource_name)
        criterion.location.geo_target_constant = (
            f"geoTargetConstants/{location_id}"
        )
        criterion.negative = negative
        operations.append(operation)

    targeted = {
        location_id
        for location_id, criterion in current.items()
        if not criterion.negative
    }
    targeted = (targeted - set(remove) - set(exclude)) | set(add)
    if not targeted and not allow_all_locations:
        raise ToolError(
            "After this change the campaign would target no location, which "
            "means every country. Add a location, or pass "
            "allow_all_locations=true if that is intended."
        )
    if not operations:
        raise ToolError("The campaign already has these locations.")

    resource_names = _send(
        client, "campaign_criterion", customer_id, operations, validate_only
    )
    return _result(
        "update_campaign_locations",
        customer_id,
        validate_only,
        [
            {
                "campaign": campaign.name,
                "added": add,
                "excluded": exclude,
                "removed": remove,
                "targeted_after": sorted(targeted) or "all countries",
            }
        ],
        resource_names,
    )


@mutate_mcp.tool(annotations=_UPDATE_ANNOTATIONS)
def set_campaign_languages(
    customer_id: str | int,
    campaign_id: str | int,
    language_ids: List[str | int],
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Sets the languages of a campaign, replacing the current ones.

    Common ids: 1003 Spanish, 1000 English, 1014 Portuguese. Find others with
    the search tool on the language_constant resource.

    By default the change is only validated, not applied. Show the returned
    changes to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer.
        campaign_id: The id of the campaign.
        language_ids: Language constant ids the campaign should target.
        validate_only: If true, validates the change without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    campaign_id = _clean_id(campaign_id, "campaign_id")
    language_ids = _ids(language_ids, "language_ids")
    if not language_ids:
        raise ToolError("language_ids needs at least one language.")

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    campaign = _campaign(client, customer_id, campaign_id)
    current = {
        row.campaign_criterion.language.language_constant.rsplit("/", 1)[
            1
        ]: row.campaign_criterion.resource_name
        for row in _criteria(client, customer_id, campaign_id, "LANGUAGE")
    }
    operations = [
        _remove_operation(client, resource_name)
        for language_id, resource_name in current.items()
        if language_id not in language_ids
    ]
    for language_id in language_ids:
        if language_id not in current:
            operation, criterion = _create_operation(
                client, campaign.resource_name
            )
            criterion.language.language_constant = (
                f"languageConstants/{language_id}"
            )
            operations.append(operation)
    if not operations:
        raise ToolError("The campaign already targets exactly these languages.")

    resource_names = _send(
        client, "campaign_criterion", customer_id, operations, validate_only
    )
    return _result(
        "set_campaign_languages",
        customer_id,
        validate_only,
        [
            {
                "campaign": campaign.name,
                "languages": {"from": sorted(current), "to": language_ids},
            }
        ],
        resource_names,
    )


class ScheduleSlot(BaseModel):
    """A time window in which the campaign shows ads."""

    model_config = ConfigDict(extra="forbid")

    days: List[Day] = Field(description="The days this window applies to.")
    start_time: str = Field(
        description="Start as HH:MM in the account time zone; minutes must be 00, 15, 30 or 45."
    )
    end_time: str = Field(
        description="End as HH:MM, after the start; 24:00 means midnight."
    )
    bid_adjustment_percent: float | None = Field(
        None,
        description="Optional bid adjustment in this window, from -90 to 900, for example 20 bids 20% more.",
    )


def _time(value: str, name: str) -> tuple[int, int]:
    match = _TIME.match(value.strip())
    if not match:
        raise ToolError(f"{name} must look like 08:30, got {value!r}.")
    hour, minute = int(match.group(1)), int(match.group(2))
    if minute not in _MINUTES or hour > 24 or (hour == 24 and minute):
        raise ToolError(
            f"{name} {value!r} is not valid: hours go from 00 to 24 and "
            "minutes must be 00, 15, 30 or 45."
        )
    return hour, minute


@mutate_mcp.tool(annotations=_UPDATE_ANNOTATIONS)
def set_ad_schedule(
    customer_id: str | int,
    campaign_id: str | int,
    schedule: List[ScheduleSlot],
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Sets the days and hours when a campaign shows ads.

    Replaces the current ad schedule. Times are in the account time zone.
    An empty schedule removes every window, so the campaign shows ads all
    day, every day. Windows of the same day cannot overlap.

    By default the change is only validated, not applied. Show the returned
    changes to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer.
        campaign_id: The id of the campaign.
        schedule: The time windows, for example weekdays from 08:00 to 22:00.
        validate_only: If true, validates the change without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    campaign_id = _clean_id(campaign_id, "campaign_id")
    windows = []
    for index, slot in enumerate(schedule):
        start = _time(slot.start_time, f"schedule[{index}].start_time")
        end = _time(slot.end_time, f"schedule[{index}].end_time")
        if end <= start:
            raise ToolError(
                f"schedule[{index}] ends before it starts "
                f"({slot.start_time}-{slot.end_time})."
            )
        modifier = (
            _modifier(
                slot.bid_adjustment_percent,
                f"schedule[{index}].bid_adjustment_percent",
                MIN_SCHEDULE_ADJUSTMENT,
            )
            if slot.bid_adjustment_percent is not None
            else None
        )
        if not slot.days:
            raise ToolError(f"schedule[{index}] needs at least one day.")
        for day in dict.fromkeys(slot.days):
            windows.append((day, start, end, modifier))
    by_day: Dict[str, List[tuple]] = {}
    for day, start, end, _ in windows:
        for other_start, other_end in by_day.get(day, []):
            if start < other_end and other_start < end:
                raise ToolError(f"Two windows overlap on {day}.")
        by_day.setdefault(day, []).append((start, end))

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    campaign = _campaign(client, customer_id, campaign_id)
    current = _criteria(client, customer_id, campaign_id, "AD_SCHEDULE")
    operations = [
        _remove_operation(client, row.campaign_criterion.resource_name)
        for row in current
    ]
    minutes = client.enums.MinuteOfHourEnum
    for (
        day,
        (start_hour, start_minute),
        (end_hour, end_minute),
        modifier,
    ) in windows:
        operation, criterion = _create_operation(client, campaign.resource_name)
        criterion.ad_schedule.day_of_week = client.enums.DayOfWeekEnum[day]
        criterion.ad_schedule.start_hour = start_hour
        criterion.ad_schedule.start_minute = minutes[_MINUTES[start_minute]]
        criterion.ad_schedule.end_hour = end_hour
        criterion.ad_schedule.end_minute = minutes[_MINUTES[end_minute]]
        if modifier is not None:
            criterion.bid_modifier = modifier
        operations.append(operation)
    if not operations:
        raise ToolError("The campaign has no ad schedule to remove.")

    resource_names = _send(
        client, "campaign_criterion", customer_id, operations, validate_only
    )
    return _result(
        "set_ad_schedule",
        customer_id,
        validate_only,
        [
            {
                "campaign": campaign.name,
                "windows_removed": len(current),
                "schedule": [
                    {
                        "day": day,
                        "from": f"{start[0]:02d}:{start[1]:02d}",
                        "to": f"{end[0]:02d}:{end[1]:02d}",
                        "bid_modifier": modifier,
                    }
                    for day, start, end, modifier in windows
                ]
                or "all day, every day",
            }
        ],
        resource_names,
    )


@mutate_mcp.tool(annotations=_UPDATE_ANNOTATIONS)
def set_device_bid_adjustments(
    customer_id: str | int,
    campaign_id: str | int,
    mobile_percent: float | None = None,
    desktop_percent: float | None = None,
    tablet_percent: float | None = None,
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Sets how much more or less a campaign bids on each device.

    Each value is a percentage from -100 to 900: 20 bids 20% more, -30 bids
    30% less, 0 removes the adjustment and -100 stops showing ads on that
    device. Devices left out keep their current adjustment. Smart bidding
    strategies other than Maximize clicks ignore most adjustments except
    -100.

    By default the change is only validated, not applied. Show the returned
    changes to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer.
        campaign_id: The id of the campaign.
        mobile_percent: Adjustment for mobile phones.
        desktop_percent: Adjustment for computers.
        tablet_percent: Adjustment for tablets.
        validate_only: If true, validates the change without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    campaign_id = _clean_id(campaign_id, "campaign_id")
    requested = {
        device: percent
        for device, percent in (
            ("MOBILE", mobile_percent),
            ("DESKTOP", desktop_percent),
            ("TABLET", tablet_percent),
        )
        if percent is not None
    }
    if not requested:
        raise ToolError("Give an adjustment for at least one device.")
    modifiers = {
        device: _modifier(percent, f"{device.lower()}_percent", -100)
        for device, percent in requested.items()
    }

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    campaign = _campaign(client, customer_id, campaign_id)
    current = {
        row.campaign_criterion.device.type_.name: row.campaign_criterion
        for row in _criteria(client, customer_id, campaign_id, "DEVICE")
    }
    operations = []
    changes = {}
    for device, modifier in modifiers.items():
        existing = current.get(device)
        before = (
            existing.bid_modifier
            if existing and "bid_modifier" in existing
            else None
        )
        if existing:
            operation = _update_operation(
                client,
                "campaign_criterion",
                existing.resource_name,
                bid_modifier=modifier,
            )
        else:
            operation, criterion = _create_operation(
                client, campaign.resource_name
            )
            criterion.device.type_ = client.enums.DeviceEnum[device]
            criterion.bid_modifier = modifier
        operations.append(operation)
        changes[device] = {
            "from_percent": (
                round((before - 1) * 100, 2) if before is not None else 0
            ),
            "to_percent": requested[device],
        }

    resource_names = _send(
        client, "campaign_criterion", customer_id, operations, validate_only
    )
    return _result(
        "set_device_bid_adjustments",
        customer_id,
        validate_only,
        [{"campaign": campaign.name, "devices": changes}],
        resource_names,
    )
