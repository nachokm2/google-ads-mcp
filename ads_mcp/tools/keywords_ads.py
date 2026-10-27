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

"""Tools to pause, enable or remove keywords, remove ads and replace
responsive search ads with an edited copy.

Google Ads does not allow editing the texts of a responsive search ad, so
replace_responsive_search_ad creates the edited ad and pauses or removes the
old one in the same atomic request. The tools are in the `mutate` namespace.
"""

from typing import Any, Dict, List, Literal

from fastmcp.exceptions import ToolError

import ads_mcp.utils as utils
from ads_mcp.tools.create import _ad_texts, _send_operations
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

MAX_KEYWORDS_PER_CALL = 200
_NEW_AD_TEMP_ID = -1


@mutate_mcp.tool(annotations=_UPDATE_ANNOTATIONS)
def set_keyword_status(
    customer_id: str | int,
    ad_group_id: str | int,
    status: Literal["ENABLED", "PAUSED", "REMOVED"],
    criterion_ids: List[str | int] = [],
    keyword_texts: List[str] = [],
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Pauses, enables or removes keywords of an ad group.

    Choose the keywords by id (ad_group_criterion.criterion_id) or by text;
    a text selects that keyword in every match type. REMOVED deletes the
    keyword: it cannot be enabled again, but the same text can be added
    later with add_keywords. Negative keywords are not affected.

    By default the change is only validated, not applied. Show the returned
    changes to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer.
        ad_group_id: The id of the ad group (ad_group.id).
        status: ENABLED, PAUSED or REMOVED.
        criterion_ids: Ids of the keywords.
        keyword_texts: Texts of the keywords.
        validate_only: If true, validates the change without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    ad_group_id = _clean_id(ad_group_id, "ad_group_id")
    ids = {_clean_id(value, "criterion_ids") for value in criterion_ids}
    texts = {" ".join(text.split()).lower() for text in keyword_texts}
    texts.discard("")
    if not ids and not texts:
        raise ToolError("Give criterion_ids or keyword_texts.")
    if len(ids) + len(texts) > MAX_KEYWORDS_PER_CALL:
        raise ToolError(
            f"At most {MAX_KEYWORDS_PER_CALL} keywords can change per call."
        )

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = _search(
        client,
        customer_id,
        "SELECT ad_group.name, ad_group_criterion.resource_name, "
        "ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type, ad_group_criterion.status "
        f"FROM ad_group_criterion WHERE ad_group.id = {ad_group_id} AND "
        "ad_group_criterion.type = 'KEYWORD' AND "
        "ad_group_criterion.negative = FALSE AND "
        "ad_group_criterion.status != 'REMOVED'",
    )
    selected = [
        row
        for row in rows
        if str(row.ad_group_criterion.criterion_id) in ids
        or row.ad_group_criterion.keyword.text.lower() in texts
    ]
    found_ids = {str(row.ad_group_criterion.criterion_id) for row in selected}
    found_texts = {
        row.ad_group_criterion.keyword.text.lower() for row in selected
    }
    missing = sorted(ids - found_ids) + sorted(texts - found_texts)
    if missing:
        raise ToolError(
            f"These keywords were not found in ad group {ad_group_id}: "
            f"{missing}"
        )

    operations = []
    changes = []
    for row in selected:
        criterion = row.ad_group_criterion
        if status == "REMOVED":
            operation = client.get_type("AdGroupCriterionOperation")
            operation.remove = criterion.resource_name
        elif criterion.status.name == status:
            continue
        else:
            operation = _update_operation(
                client,
                "ad_group_criterion",
                criterion.resource_name,
                status=client.enums.AdGroupCriterionStatusEnum[status],
            )
        operations.append(operation)
        changes.append(
            {
                "keyword": criterion.keyword.text,
                "match_type": criterion.keyword.match_type.name,
                "criterion_id": str(criterion.criterion_id),
                "status": {"from": criterion.status.name, "to": status},
            }
        )
    if not operations:
        raise ToolError(f"The keywords are already {status}.")

    resource_names = _send(
        client, "ad_group_criterion", customer_id, operations, validate_only
    )
    return _result(
        "set_keyword_status",
        customer_id,
        validate_only,
        [{"ad_group": selected[0].ad_group.name, "keywords": changes}],
        resource_names,
    )


def _ad_row(client, customer_id: str, ad_group_id: str, ad_id: str):
    return _find_one(
        client,
        customer_id,
        "SELECT ad_group.name, ad_group_ad.resource_name, ad_group_ad.status, "
        "ad_group_ad.ad.type, ad_group_ad.ad.final_urls, "
        "ad_group_ad.ad.responsive_search_ad.headlines, "
        "ad_group_ad.ad.responsive_search_ad.descriptions, "
        "ad_group_ad.ad.responsive_search_ad.path1, "
        "ad_group_ad.ad.responsive_search_ad.path2 FROM ad_group_ad WHERE "
        f"ad_group.id = {ad_group_id} AND ad_group_ad.ad.id = {ad_id} AND "
        "ad_group_ad.status != 'REMOVED'",
        f"Ad {ad_id} was not found in ad group {ad_group_id}, or it is "
        "already removed.",
    )


@mutate_mcp.tool(annotations=_UPDATE_ANNOTATIONS)
def remove_ad(
    customer_id: str | int,
    ad_group_id: str | int,
    ad_id: str | int,
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Removes an ad. This cannot be undone.

    A removed ad stops serving for good; its history stays in reports. To
    stop it temporarily use set_ad_status with PAUSED instead.

    By default the removal is only validated, not applied. Explain that it
    is permanent and call again with validate_only=false only after the
    user confirms.

    Args:
        customer_id: The id of the customer.
        ad_group_id: The id of the ad group (ad_group.id).
        ad_id: The id of the ad (ad_group_ad.ad.id).
        validate_only: If true, validates the removal without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    ad_group_id = _clean_id(ad_group_id, "ad_group_id")
    ad_id = _clean_id(ad_id, "ad_id")
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    row = _ad_row(client, customer_id, ad_group_id, ad_id)
    operation = client.get_type("AdGroupAdOperation")
    operation.remove = row.ad_group_ad.resource_name
    resource_names = _send(
        client, "ad_group_ad", customer_id, [operation], validate_only
    )
    return _result(
        "remove_ad",
        customer_id,
        validate_only,
        [
            {
                "ad_group": row.ad_group.name,
                "ad_id": ad_id,
                "ad_type": row.ad_group_ad.ad.type_.name,
                "status": {
                    "from": row.ad_group_ad.status.name,
                    "to": "REMOVED",
                },
            }
        ],
        resource_names,
    )


def _copy_texts(client, assets, replacements: Dict[str, str]):
    """Copies the texts of an ad, keeping their pins, with replacements."""
    copies = []
    for asset in assets:
        text = asset.text
        for old, new in replacements.items():
            if old:
                text = text.replace(old, new)
        copy = client.get_type("AdTextAsset")
        copy.text = " ".join(text.split())
        if asset.pinned_field:
            copy.pinned_field = asset.pinned_field
        copies.append(copy)
    return copies


@mutate_mcp.tool(annotations=_UPDATE_ANNOTATIONS)
def replace_responsive_search_ad(
    customer_id: str | int,
    ad_group_id: str | int,
    ad_id: str | int,
    headlines: List[str] | None = None,
    descriptions: List[str] | None = None,
    final_url: str | None = None,
    path1: str | None = None,
    path2: str | None = None,
    text_replacements: Dict[str, str] = {},
    old_ad_action: Literal["PAUSE", "REMOVE"] = "PAUSE",
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Edits a responsive search ad by replacing it with an edited copy.

    Google Ads does not let you change the texts of an ad, so this creates a
    new ad in the same ad group with the changes and pauses (or removes) the
    old one, in one atomic request. Anything not given is copied from the
    old ad, keeping pinned headlines. text_replacements edits the copied
    texts, for example {"2026": "2027"}; when headlines or descriptions are
    given they replace the old ones entirely and have no pins. The new ad
    starts with no history and goes through review again.

    By default the change is only validated, not applied. Show the returned
    changes to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer.
        ad_group_id: The id of the ad group (ad_group.id).
        ad_id: The id of the responsive search ad to replace (ad_group_ad.ad.id).
        headlines: Optional new headlines, 3 to 15, up to 30 characters each.
        descriptions: Optional new descriptions, 2 to 4, up to 90 characters each.
        final_url: Optional new landing page.
        path1: Optional new first display path; an empty string removes it.
        path2: Optional new second display path; an empty string removes it.
        text_replacements: Texts to replace in the copied headlines and descriptions, as {"old": "new"}.
        old_ad_action: PAUSE keeps the old ad paused; REMOVE deletes it.
        validate_only: If true, validates the change without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    ad_group_id = _clean_id(ad_group_id, "ad_group_id")
    ad_id = _clean_id(ad_id, "ad_id")
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    row = _ad_row(client, customer_id, ad_group_id, ad_id)
    old_ad = row.ad_group_ad.ad
    if old_ad.type_.name != "RESPONSIVE_SEARCH_AD":
        raise ToolError(
            f"Ad {ad_id} is a {old_ad.type_.name}; only responsive search ads "
            "can be replaced."
        )
    rsa = old_ad.responsive_search_ad
    if (
        headlines is None
        and descriptions is None
        and final_url is None
        and path1 is None
        and path2 is None
        and not any(text_replacements)
    ):
        raise ToolError("Give at least one change for the new ad.")

    copied_headlines = _copy_texts(client, rsa.headlines, text_replacements)
    copied_descriptions = _copy_texts(
        client, rsa.descriptions, text_replacements
    )
    path1 = rsa.path1 if path1 is None else path1.strip()
    path2 = rsa.path2 if path2 is None else path2.strip()
    url, new_headlines, new_descriptions = _ad_texts(
        (
            headlines
            if headlines is not None
            else [asset.text for asset in copied_headlines]
        ),
        (
            descriptions
            if descriptions is not None
            else [asset.text for asset in copied_descriptions]
        ),
        final_url or (old_ad.final_urls[0] if old_ad.final_urls else ""),
        path1 or None,
        path2 or None,
    )

    operation = client.get_type("MutateOperation")
    ad_group_ad = operation.ad_group_ad_operation.create
    ad_group_ad.ad_group = f"customers/{customer_id}/adGroups/{ad_group_id}"
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad_group_ad.ad.final_urls.append(url)
    new_rsa = ad_group_ad.ad.responsive_search_ad
    if headlines is None:
        new_rsa.headlines.extend(copied_headlines)
    else:
        for text in new_headlines:
            asset = client.get_type("AdTextAsset")
            asset.text = text
            new_rsa.headlines.append(asset)
    if descriptions is None:
        new_rsa.descriptions.extend(copied_descriptions)
    else:
        for text in new_descriptions:
            asset = client.get_type("AdTextAsset")
            asset.text = text
            new_rsa.descriptions.append(asset)
    if path1:
        new_rsa.path1 = path1
    if path2:
        new_rsa.path2 = path2

    old_operation = client.get_type("MutateOperation")
    if old_ad_action == "REMOVE":
        old_operation.ad_group_ad_operation.remove = (
            row.ad_group_ad.resource_name
        )
    else:
        client.copy_from(
            old_operation.ad_group_ad_operation,
            _update_operation(
                client,
                "ad_group_ad",
                row.ad_group_ad.resource_name,
                status=client.enums.AdGroupAdStatusEnum.PAUSED,
            ),
        )

    resource_names = _send_operations(
        client, customer_id, [operation, old_operation], validate_only
    )
    result = _result(
        "replace_responsive_search_ad",
        customer_id,
        validate_only,
        [
            {
                "ad_group": row.ad_group.name,
                "old_ad_id": ad_id,
                "old_ad": "removed" if old_ad_action == "REMOVE" else "paused",
                "new_ad": {
                    "final_url": url,
                    "headlines": [a.text for a in new_rsa.headlines],
                    "descriptions": [a.text for a in new_rsa.descriptions],
                    "path1": path1 or None,
                    "path2": path2 or None,
                },
            }
        ],
        resource_names,
    )
    if resource_names:
        result["new_ad_id"] = resource_names[0].rsplit("~", 1)[1]
    return result
