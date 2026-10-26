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

"""Tools to apply or dismiss Google's recommendations, edit campaigns and
remove campaigns or ad groups.

They are registered in the `mutate` namespace, so they are enabled together
with the other write tools.
"""

import re
from typing import Any, Dict, List

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf.field_mask_pb2 import FieldMask
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.errors import partial_failure_errors
from ads_mcp.tools.create import _check_date
from ads_mcp.tools.create_pmax import _search
from ads_mcp.tools.mutate import (
    _UPDATE_ANNOTATIONS,
    _clean_id,
    _find_one,
    _get_service,
    _raise_tool_error,
    _result,
    _send,
    mutate_mcp,
)

MAX_RECOMMENDATIONS_PER_CALL = 100

_APPLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False
)
_RECOMMENDATION_ID = re.compile(r"^[A-Za-z0-9_~=\-]+$")


def _recommendation_names(
    customer_id: str, recommendation_ids: List[str]
) -> List[str]:
    """Turns recommendation ids or resource names into resource names."""
    if not 1 <= len(recommendation_ids) <= MAX_RECOMMENDATIONS_PER_CALL:
        raise ToolError(
            "recommendation_ids needs between 1 and "
            f"{MAX_RECOMMENDATIONS_PER_CALL} ids, got {len(recommendation_ids)}."
        )
    names = []
    for value in recommendation_ids:
        recommendation_id = value.strip().rsplit("/", 1)[-1]
        if not _RECOMMENDATION_ID.match(recommendation_id):
            raise ToolError(f"{value!r} is not a recommendation id.")
        names.append(
            f"customers/{customer_id}/recommendations/{recommendation_id}"
        )
    return list(dict.fromkeys(names))


def _recommendations(
    client: GoogleAdsClient, customer_id: str, names: List[str]
) -> List[Dict[str, Any]]:
    """Looks up the type and campaign of active recommendations."""
    rows = _search(
        client,
        customer_id,
        "SELECT recommendation.resource_name, recommendation.type, "
        "recommendation.campaign FROM recommendation WHERE "
        "recommendation.dismissed = FALSE AND recommendation.resource_name IN ("
        + ", ".join(f"'{name}'" for name in names)
        + ")",
    )
    found = {
        row.recommendation.resource_name: row.recommendation for row in rows
    }
    missing = [name.rsplit("/", 1)[1] for name in names if name not in found]
    if missing:
        raise ToolError(
            f"These recommendations are not active (they may have been "
            f"applied, dismissed or expired): {missing}. List them again with "
            "list_recommendations."
        )
    campaigns = sorted({r.campaign for r in found.values() if r.campaign})
    campaign_names = {}
    if campaigns:
        campaign_names = {
            row.campaign.resource_name: row.campaign.name
            for row in _search(
                client,
                customer_id,
                "SELECT campaign.resource_name, campaign.name FROM campaign "
                "WHERE campaign.resource_name IN ("
                + ", ".join(f"'{name}'" for name in campaigns)
                + ")",
            )
        }
    return [
        {
            "recommendation_id": name.rsplit("/", 1)[1],
            "resource_name": name,
            "type": found[name].type_.name,
            "campaign": campaign_names.get(found[name].campaign),
        }
        for name in names
    ]


def _recommendation_result(
    tool_name: str,
    customer_id: str,
    validate_only: bool,
    recommendations: List[Dict[str, Any]],
    failures: Dict[int, List[str]],
) -> Dict[str, Any]:
    changes = []
    for index, recommendation in enumerate(recommendations):
        entry = {
            key: recommendation[key]
            for key in ("recommendation_id", "type", "campaign")
        }
        if not validate_only:
            errors = failures.get(index)
            entry["status"] = "FAILED" if errors else "OK"
            if errors:
                entry["errors"] = errors
        changes.append(entry)
    result = _result(tool_name, customer_id, validate_only, changes, [])
    if validate_only:
        result["message"] = (
            "Nothing was changed: the Google Ads API cannot validate "
            "recommendations without acting on them, so this lists what "
            "would change. Call again with validate_only=false only after the "
            "user confirms."
        )
    return result


@mutate_mcp.tool(annotations=_APPLY_ANNOTATIONS)
def apply_recommendations(
    customer_id: str | int,
    recommendation_ids: List[str],
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Applies Google recommendations, as listed by list_recommendations.

    Each recommendation is applied with Google's suggested values, for
    example adding the recommended keyword or raising the budget to the
    recommended amount. Recommendations are applied one by one: the result
    says which ones failed and why, and the others are still applied.

    The Google Ads API cannot validate recommendations without applying
    them, so with validate_only=true (the default) this only checks that the
    recommendations are still active and lists what would be applied. Show
    it to the user and call again with validate_only=false only after they
    confirm.

    Args:
        customer_id: The id of the customer.
        recommendation_ids: 1 to 100 recommendation ids from list_recommendations.
        validate_only: If true, lists what would be applied without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    names = _recommendation_names(customer_id, recommendation_ids)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    recommendations = _recommendations(client, customer_id, names)
    failures: Dict[int, List[str]] = {}
    if not validate_only:
        request = client.get_type("ApplyRecommendationRequest")
        request.customer_id = customer_id
        request.partial_failure = True
        for name in names:
            operation = client.get_type("ApplyRecommendationOperation")
            operation.resource_name = name
            request.operations.append(operation)
        try:
            response = _get_service(
                client, "RecommendationService"
            ).apply_recommendation(request=request)
        except GoogleAdsException as ex:
            _raise_tool_error(ex)
        failures = partial_failure_errors(
            client, response.partial_failure_error
        )
    return _recommendation_result(
        "apply_recommendations",
        customer_id,
        validate_only,
        recommendations,
        failures,
    )


@mutate_mcp.tool(annotations=_APPLY_ANNOTATIONS)
def dismiss_recommendations(
    customer_id: str | int,
    recommendation_ids: List[str],
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Dismisses Google recommendations so they stop being suggested.

    With validate_only=true (the default) this only checks that the
    recommendations are still active and lists what would be dismissed. Show
    it to the user and call again with validate_only=false only after they
    confirm.

    Args:
        customer_id: The id of the customer.
        recommendation_ids: 1 to 100 recommendation ids from list_recommendations.
        validate_only: If true, lists what would be dismissed without dismissing it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    names = _recommendation_names(customer_id, recommendation_ids)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    recommendations = _recommendations(client, customer_id, names)
    failures: Dict[int, List[str]] = {}
    if not validate_only:
        request = client.get_type("DismissRecommendationRequest")
        request.customer_id = customer_id
        request.partial_failure = True
        operation_type = type(request).DismissRecommendationOperation
        for name in names:
            request.operations.append(operation_type(resource_name=name))
        try:
            response = _get_service(
                client, "RecommendationService"
            ).dismiss_recommendation(request=request)
        except GoogleAdsException as ex:
            _raise_tool_error(ex)
        failures = partial_failure_errors(
            client, response.partial_failure_error
        )
    return _recommendation_result(
        "dismiss_recommendations",
        customer_id,
        validate_only,
        recommendations,
        failures,
    )


@mutate_mcp.tool(annotations=_UPDATE_ANNOTATIONS)
def update_campaign(
    customer_id: str | int,
    campaign_id: str | int,
    name: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    remove_end_date: bool = False,
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Renames a campaign or changes its start or end date.

    Pass only the settings to change. The start date can only be changed
    before the campaign starts. remove_end_date makes the campaign run
    without an end date. To pause or enable it use set_campaign_status, and
    to change its budget use update_campaign_budget.

    By default the change is only validated, not applied. Show the returned
    changes to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer that owns the campaign.
        campaign_id: The id of the campaign (campaign.id).
        name: The new name, unique in the account.
        start_date: The new start date as YYYY-MM-DD.
        end_date: The new end date as YYYY-MM-DD.
        remove_end_date: Whether to remove the end date.
        validate_only: If true, validates the change without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    campaign_id = _clean_id(campaign_id, "campaign_id")
    if name is not None:
        name = " ".join(name.split())
        if not name:
            raise ToolError("name cannot be empty.")
    start = _check_date(start_date, "start_date")
    end = _check_date(end_date, "end_date")
    if end and remove_end_date:
        raise ToolError("Give end_date or remove_end_date, not both.")
    if start and end and end < start:
        raise ToolError("end_date cannot be before start_date.")
    if name is None and not start and not end and not remove_end_date:
        raise ToolError("Give at least one setting to change.")

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    row = _find_one(
        client,
        customer_id,
        "SELECT campaign.resource_name, campaign.name, campaign.status, "
        "campaign.start_date_time, campaign.end_date_time FROM campaign "
        f"WHERE campaign.id = {campaign_id} AND campaign.status != 'REMOVED'",
        f"Campaign {campaign_id} was not found in customer {customer_id}.",
    )
    campaign = row.campaign
    operation = client.get_type("CampaignOperation")
    operation.update.resource_name = campaign.resource_name
    paths = []
    change: Dict[str, Any] = {
        "resource_name": campaign.resource_name,
        "campaign": campaign.name,
    }
    if name is not None:
        operation.update.name = name
        paths.append("name")
        change["name"] = {"from": campaign.name, "to": name}
    if start:
        operation.update.start_date_time = f"{start} 00:00:00"
        paths.append("start_date_time")
        change["start_date"] = {
            "from": campaign.start_date_time or None,
            "to": str(start),
        }
    if end:
        operation.update.end_date_time = f"{end} 23:59:59"
        paths.append("end_date_time")
        change["end_date"] = {
            "from": campaign.end_date_time or None,
            "to": str(end),
        }
    if remove_end_date:
        # A field in the mask without a value is cleared.
        paths.append("end_date_time")
        change["end_date"] = {
            "from": campaign.end_date_time or None,
            "to": None,
        }
    client.copy_from(operation.update_mask, FieldMask(paths=paths))
    resource_names = _send(
        client, "campaign", customer_id, [operation], validate_only
    )
    return _result(
        "update_campaign", customer_id, validate_only, [change], resource_names
    )


def _check_confirm_name(confirm_name: str, name: str, what: str) -> None:
    if " ".join(confirm_name.split()) != " ".join(name.split()):
        raise ToolError(
            f"confirm_name does not match the name of the {what}. Ask the "
            f"user to type the exact name of the {what} they want to remove."
        )


@mutate_mcp.tool(annotations=_UPDATE_ANNOTATIONS)
def remove_campaign(
    customer_id: str | int,
    campaign_id: str | int,
    confirm_name: str,
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Removes a campaign. This cannot be undone.

    A removed campaign stops serving for good and cannot be enabled again;
    its history stays in reports. To stop it temporarily use
    set_campaign_status with PAUSED instead. As a safeguard, confirm_name
    must be the exact name of the campaign, typed by the user after you
    explain that the removal is permanent.

    By default the removal is only validated, not applied. Call again with
    validate_only=false only after the user confirms.

    Args:
        customer_id: The id of the customer that owns the campaign.
        campaign_id: The id of the campaign (campaign.id).
        confirm_name: The exact name of the campaign, as typed by the user.
        validate_only: If true, validates the removal without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    campaign_id = _clean_id(campaign_id, "campaign_id")
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    row = _find_one(
        client,
        customer_id,
        "SELECT campaign.resource_name, campaign.name, campaign.status "
        f"FROM campaign WHERE campaign.id = {campaign_id} "
        "AND campaign.status != 'REMOVED'",
        f"Campaign {campaign_id} was not found in customer {customer_id}, or "
        "it is already removed.",
    )
    _check_confirm_name(confirm_name, row.campaign.name, "campaign")
    operation = client.get_type("CampaignOperation")
    operation.remove = row.campaign.resource_name
    resource_names = _send(
        client, "campaign", customer_id, [operation], validate_only
    )
    return _result(
        "remove_campaign",
        customer_id,
        validate_only,
        [
            {
                "resource_name": row.campaign.resource_name,
                "campaign": row.campaign.name,
                "status": {"from": row.campaign.status.name, "to": "REMOVED"},
            }
        ],
        resource_names,
        ["Removing a campaign is permanent; it cannot be enabled again."],
    )


@mutate_mcp.tool(annotations=_UPDATE_ANNOTATIONS)
def remove_ad_group(
    customer_id: str | int,
    ad_group_id: str | int,
    confirm_name: str,
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Removes an ad group. This cannot be undone.

    A removed ad group stops serving for good and cannot be enabled again;
    its history stays in reports. To stop it temporarily use
    set_ad_group_status with PAUSED instead. As a safeguard, confirm_name
    must be the exact name of the ad group, typed by the user after you
    explain that the removal is permanent.

    By default the removal is only validated, not applied. Call again with
    validate_only=false only after the user confirms.

    Args:
        customer_id: The id of the customer that owns the ad group.
        ad_group_id: The id of the ad group (ad_group.id).
        confirm_name: The exact name of the ad group, as typed by the user.
        validate_only: If true, validates the removal without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    ad_group_id = _clean_id(ad_group_id, "ad_group_id")
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    row = _find_one(
        client,
        customer_id,
        "SELECT ad_group.resource_name, ad_group.name, ad_group.status, "
        f"campaign.name FROM ad_group WHERE ad_group.id = {ad_group_id} "
        "AND ad_group.status != 'REMOVED'",
        f"Ad group {ad_group_id} was not found in customer {customer_id}, or "
        "it is already removed.",
    )
    _check_confirm_name(confirm_name, row.ad_group.name, "ad group")
    operation = client.get_type("AdGroupOperation")
    operation.remove = row.ad_group.resource_name
    resource_names = _send(
        client, "ad_group", customer_id, [operation], validate_only
    )
    return _result(
        "remove_ad_group",
        customer_id,
        validate_only,
        [
            {
                "resource_name": row.ad_group.resource_name,
                "campaign": row.campaign.name,
                "ad_group": row.ad_group.name,
                "status": {"from": row.ad_group.status.name, "to": "REMOVED"},
            }
        ],
        resource_names,
        ["Removing an ad group is permanent; it cannot be enabled again."],
    )
