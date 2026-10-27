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

"""Tools for shared negative keyword lists.

A list is created once and applied to many campaigns, so a word such as
"gratis" can be blocked in every campaign at the same time. Listing is
read-only and lives in the `reports` namespace; the other tools are in
`mutate`.
"""

from typing import Any, Dict, List

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient

import ads_mcp.utils as utils
from ads_mcp.tools.create import MAX_KEYWORD_LENGTH, MatchType, _send_operations
from ads_mcp.tools.create_pmax import _search
from ads_mcp.tools.mutate import (
    _CREATE_ANNOTATIONS,
    _UPDATE_ANNOTATIONS,
    _clean_id,
    _find_one,
    _result,
    mutate_mcp,
)
from ads_mcp.tools.reports import _READ_ONLY, reports_mcp

MAX_KEYWORDS_PER_CALL = 1000
MAX_KEYWORD_WORDS = 10
_LIST_TEMP_ID = -1


def _parse_keywords(
    keywords: List[str], default_match_type: str
) -> List[tuple[str, str]]:
    """Reads keywords written as in Google Ads Editor.

    "[text]" is exact match, '"text"' is phrase match and plain text uses
    the default match type.
    """
    parsed = []
    seen = set()
    problems = []
    for raw in keywords:
        text = " ".join(raw.split())
        match_type = default_match_type
        if len(text) > 1 and text[0] == "[" and text[-1] == "]":
            text, match_type = text[1:-1].strip(), "EXACT"
        elif len(text) > 1 and text[0] == '"' and text[-1] == '"':
            text, match_type = text[1:-1].strip(), "PHRASE"
        if not text:
            continue
        if len(text) > MAX_KEYWORD_LENGTH:
            problems.append(
                f"{text!r} has more than {MAX_KEYWORD_LENGTH} characters"
            )
        elif len(text.split()) > MAX_KEYWORD_WORDS:
            problems.append(f"{text!r} has more than {MAX_KEYWORD_WORDS} words")
        key = (text.lower(), match_type)
        if key not in seen:
            seen.add(key)
            parsed.append((text, match_type))
    if problems:
        raise ToolError("Invalid keywords:\n- " + "\n- ".join(problems))
    if not 1 <= len(parsed) <= MAX_KEYWORDS_PER_CALL:
        raise ToolError(
            f"keywords needs between 1 and {MAX_KEYWORDS_PER_CALL} keywords."
        )
    return parsed


def _keyword_operation(
    client: GoogleAdsClient, shared_set: str, text: str, match_type: str
):
    operation = client.get_type("MutateOperation")
    criterion = operation.shared_criterion_operation.create
    criterion.shared_set = shared_set
    criterion.keyword.text = text
    criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]
    return operation


def _campaigns(
    client: GoogleAdsClient,
    customer_id: str,
    campaign_ids: List[str | int],
    all_search_campaigns: bool,
) -> Dict[str, str]:
    """Returns the names of the chosen campaigns by id."""
    if all_search_campaigns:
        condition = (
            "campaign.advertising_channel_type = 'SEARCH' AND "
            "campaign.status != 'REMOVED'"
        )
    else:
        ids = [_clean_id(value, "campaign_ids") for value in campaign_ids]
        if not ids:
            return {}
        condition = f"campaign.id IN ({', '.join(dict.fromkeys(ids))})"
    rows = _search(
        client,
        customer_id,
        "SELECT campaign.id, campaign.name, campaign.status FROM campaign "
        f"WHERE {condition}",
    )
    found = {str(row.campaign.id): row.campaign.name for row in rows}
    if not all_search_campaigns:
        missing = [i for i in campaign_ids if _clean_id(i, "x") not in found]
        if missing:
            raise ToolError(f"These campaigns were not found: {missing}")
    return found


def _shared_set(client: GoogleAdsClient, customer_id: str, list_id: str):
    row = _find_one(
        client,
        customer_id,
        "SELECT shared_set.resource_name, shared_set.name, shared_set.type, "
        "shared_set.status FROM shared_set WHERE shared_set.id = "
        f"{list_id}",
        f"Negative keyword list {list_id} was not found.",
    )
    if (
        row.shared_set.type_.name != "NEGATIVE_KEYWORDS"
        or row.shared_set.status.name != "ENABLED"
    ):
        raise ToolError(
            f"Shared set {list_id} is not an enabled negative keyword list."
        )
    return row.shared_set


@reports_mcp.tool(annotations=_READ_ONLY)
def list_negative_keyword_lists(
    customer_id: str | int,
    include_keywords: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Lists the shared negative keyword lists and the campaigns using them.

    Args:
        customer_id: The id of the customer.
        include_keywords: Whether to include the keywords of each list.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    lists = {
        row.shared_set.id: {
            "list_id": str(row.shared_set.id),
            "name": row.shared_set.name,
            "keywords": row.shared_set.member_count,
            "campaigns": [],
        }
        for row in _search(
            client,
            customer_id,
            "SELECT shared_set.id, shared_set.name, shared_set.member_count "
            "FROM shared_set WHERE shared_set.type = 'NEGATIVE_KEYWORDS' AND "
            "shared_set.status = 'ENABLED'",
        )
    }
    for row in _search(
        client,
        customer_id,
        "SELECT shared_set.id, campaign.name, campaign.status FROM "
        "campaign_shared_set WHERE campaign_shared_set.status = 'ENABLED' "
        "AND shared_set.type = 'NEGATIVE_KEYWORDS' AND campaign.status != "
        "'REMOVED'",
    ):
        if row.shared_set.id in lists:
            lists[row.shared_set.id]["campaigns"].append(row.campaign.name)
    if include_keywords and lists:
        for item in lists.values():
            item["keyword_list"] = []
        for row in _search(
            client,
            customer_id,
            "SELECT shared_set.id, shared_criterion.keyword.text, "
            "shared_criterion.keyword.match_type FROM shared_criterion WHERE "
            "shared_set.type = 'NEGATIVE_KEYWORDS' AND shared_set.status = "
            "'ENABLED'",
        ):
            if row.shared_set.id in lists:
                lists[row.shared_set.id]["keyword_list"].append(
                    {
                        "text": row.shared_criterion.keyword.text,
                        "match_type": row.shared_criterion.keyword.match_type.name,
                    }
                )
    return {"lists": list(lists.values())}


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def create_negative_keyword_list(
    customer_id: str | int,
    name: str,
    keywords: List[str],
    match_type: MatchType = "PHRASE",
    campaign_ids: List[str | int] = [],
    all_search_campaigns: bool = False,
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Creates a shared negative keyword list and applies it to campaigns.

    Write keywords as in Google Ads Editor: "[gratis]" is exact match,
    '"curso gratis"' is phrase match and plain text uses match_type. Apply
    the list to specific campaigns, or to every Search campaign with
    all_search_campaigns. Everything is created in one atomic request.

    By default the list is only validated, not created. Show the returned
    summary to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer.
        name: The name of the list, unique in the account.
        keywords: 1 to 1000 negative keywords.
        match_type: Match type for keywords written without brackets or quotes.
        campaign_ids: Campaigns to apply the list to.
        all_search_campaigns: Whether to apply it to every Search campaign that is not removed.
        validate_only: If true, validates the list without creating it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    name = " ".join(name.split())
    if not name:
        raise ToolError("name cannot be empty.")
    parsed = _parse_keywords(keywords, match_type)

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    campaigns = _campaigns(
        client, customer_id, campaign_ids, all_search_campaigns
    )
    shared_set_name = f"customers/{customer_id}/sharedSets/{_LIST_TEMP_ID}"
    operation = client.get_type("MutateOperation")
    shared_set = operation.shared_set_operation.create
    shared_set.resource_name = shared_set_name
    shared_set.name = name
    shared_set.type_ = client.enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS
    operations = [operation]
    for text, keyword_match_type in parsed:
        operations.append(
            _keyword_operation(
                client, shared_set_name, text, keyword_match_type
            )
        )
    for campaign_id in campaigns:
        operation = client.get_type("MutateOperation")
        link = operation.campaign_shared_set_operation.create
        link.campaign = f"customers/{customer_id}/campaigns/{campaign_id}"
        link.shared_set = shared_set_name
        operations.append(operation)

    resource_names = _send_operations(
        client, customer_id, operations, validate_only
    )
    result = _result(
        "create_negative_keyword_list",
        customer_id,
        validate_only,
        [
            {
                "list": name,
                "keywords": [
                    {"text": text, "match_type": keyword_match_type}
                    for text, keyword_match_type in parsed
                ],
                "campaigns": sorted(campaigns.values()),
            }
        ],
        resource_names,
    )
    if resource_names:
        result["list_id"] = resource_names[0].rsplit("/", 1)[1]
    return result


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def add_to_negative_keyword_list(
    customer_id: str | int,
    list_id: str | int,
    keywords: List[str],
    match_type: MatchType = "PHRASE",
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Adds negative keywords to a shared list; every campaign using it gets them.

    Write keywords as in Google Ads Editor: "[gratis]" is exact match,
    '"curso gratis"' is phrase match and plain text uses match_type.
    Keywords already in the list are skipped.

    By default the change is only validated, not applied. Show the returned
    changes to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer.
        list_id: The id of the list (shared_set.id), from list_negative_keyword_lists.
        keywords: 1 to 1000 negative keywords.
        match_type: Match type for keywords written without brackets or quotes.
        validate_only: If true, validates the change without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    list_id = _clean_id(list_id, "list_id")
    parsed = _parse_keywords(keywords, match_type)

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    shared_set = _shared_set(client, customer_id, list_id)
    existing = {
        (
            row.shared_criterion.keyword.text.lower(),
            row.shared_criterion.keyword.match_type.name,
        )
        for row in _search(
            client,
            customer_id,
            "SELECT shared_criterion.keyword.text, "
            "shared_criterion.keyword.match_type FROM shared_criterion WHERE "
            f"shared_set.id = {list_id}",
        )
    }
    new = [
        item for item in parsed if (item[0].lower(), item[1]) not in existing
    ]
    if not new:
        raise ToolError("The list already has all these keywords.")
    operations = [
        _keyword_operation(client, shared_set.resource_name, text, kind)
        for text, kind in new
    ]
    resource_names = _send_operations(
        client, customer_id, operations, validate_only
    )
    return _result(
        "add_to_negative_keyword_list",
        customer_id,
        validate_only,
        [
            {
                "list": shared_set.name,
                "added": [{"text": t, "match_type": m} for t, m in new],
                "already_in_list": len(parsed) - len(new),
            }
        ],
        resource_names,
    )


@mutate_mcp.tool(annotations=_UPDATE_ANNOTATIONS)
def set_negative_keyword_list_campaigns(
    customer_id: str | int,
    list_id: str | int,
    add_campaign_ids: List[str | int] = [],
    remove_campaign_ids: List[str | int] = [],
    all_search_campaigns: bool = False,
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Applies a shared negative keyword list to campaigns, or stops applying it.

    Use add_campaign_ids or all_search_campaigns to apply the list, and
    remove_campaign_ids to stop applying it. The keywords of the list are
    not changed.

    By default the change is only validated, not applied. Show the returned
    changes to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer.
        list_id: The id of the list (shared_set.id).
        add_campaign_ids: Campaigns that should start using the list.
        remove_campaign_ids: Campaigns that should stop using the list.
        all_search_campaigns: Whether to apply it to every Search campaign that is not removed.
        validate_only: If true, validates the change without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    list_id = _clean_id(list_id, "list_id")
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    shared_set = _shared_set(client, customer_id, list_id)
    to_add = _campaigns(
        client, customer_id, add_campaign_ids, all_search_campaigns
    )
    to_remove = _campaigns(client, customer_id, remove_campaign_ids, False)
    if set(to_add) & set(to_remove):
        raise ToolError("A campaign cannot be added and removed at once.")
    if not to_add and not to_remove:
        raise ToolError("Give campaigns to add or remove.")
    linked = {
        str(row.campaign.id)
        for row in _search(
            client,
            customer_id,
            "SELECT campaign.id FROM campaign_shared_set WHERE "
            f"shared_set.id = {list_id} AND campaign_shared_set.status = "
            "'ENABLED'",
        )
    }

    operations = []
    added, removed = [], []
    for campaign_id, campaign_name in to_add.items():
        if campaign_id in linked:
            continue
        operation = client.get_type("MutateOperation")
        link = operation.campaign_shared_set_operation.create
        link.campaign = f"customers/{customer_id}/campaigns/{campaign_id}"
        link.shared_set = shared_set.resource_name
        operations.append(operation)
        added.append(campaign_name)
    for campaign_id, campaign_name in to_remove.items():
        if campaign_id not in linked:
            continue
        operation = client.get_type("MutateOperation")
        operation.campaign_shared_set_operation.remove = (
            f"customers/{customer_id}/campaignSharedSets/"
            f"{campaign_id}~{list_id}"
        )
        operations.append(operation)
        removed.append(campaign_name)
    if not operations:
        raise ToolError("The campaigns already use the list as requested.")

    resource_names = _send_operations(
        client, customer_id, operations, validate_only
    )
    return _result(
        "set_negative_keyword_list_campaigns",
        customer_id,
        validate_only,
        [
            {
                "list": shared_set.name,
                "applied_to": sorted(added),
                "removed_from": sorted(removed),
            }
        ],
        resource_names,
    )
