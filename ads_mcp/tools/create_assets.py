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

"""Tools for adding sitelinks, callouts and structured snippets.

These assets extend Search ads with extra links and texts. They are linked
to one campaign, or to the whole account when no campaign is given. The
tools are registered in the `mutate` namespace, so they are enabled together
with the other write tools.
"""

from typing import Any, Dict, List, Literal

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from pydantic import BaseModel, ConfigDict, Field

import ads_mcp.utils as utils
from ads_mcp.tools.create import (
    _check_final_url,
    _clean_texts,
    _send_operations,
)
from ads_mcp.tools.mutate import (
    _CREATE_ANNOTATIONS,
    _clean_id,
    _find_one,
    _result,
    mutate_mcp,
)

MAX_SITELINK_TEXT = 25
MAX_SITELINK_DESCRIPTION = 35
MAX_CALLOUT_TEXT = 25
MAX_SNIPPET_VALUE = 25
MAX_ASSETS_PER_CALL = 20

# Google Ads accepts these headers in English and shows them translated.
SnippetHeader = Literal[
    "Degree programs",
    "Courses",
    "Types",
    "Service catalog",
    "Destinations",
    "Amenities",
    "Brands",
    "Models",
    "Neighborhoods",
    "Styles",
    "Shows",
    "Featured hotels",
    "Insurance coverage",
]


class Sitelink(BaseModel):
    """A link to a page of the site shown under the ad."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="The link text, at most 25 characters.")
    final_url: str = Field(
        description="The page it opens, starting with https://."
    )
    description1: str | None = Field(
        None,
        description="Optional first description line, at most 35 characters.",
    )
    description2: str | None = Field(
        None,
        description="Optional second description line, at most 35 characters; required when description1 is set.",
    )


def _target(
    client: GoogleAdsClient, customer_id: str, campaign_id: str | int | None
) -> tuple[str | None, str]:
    """Returns the campaign resource name (None for the account) and a label."""
    if campaign_id is None:
        return None, "account"
    campaign_id = _clean_id(campaign_id, "campaign_id")
    row = _find_one(
        client,
        customer_id,
        "SELECT campaign.resource_name, campaign.name FROM campaign "
        f"WHERE campaign.id = {campaign_id} AND campaign.status != 'REMOVED'",
        f"Campaign {campaign_id} was not found in customer {customer_id}.",
    )
    return row.campaign.resource_name, f"campaign {row.campaign.name!r}"


def _asset_operations(
    client: GoogleAdsClient,
    customer_id: str,
    campaign_resource_name: str | None,
    field_type: str,
    fill_assets: List[Any],
) -> List[Any]:
    """Creates one asset per fill function and links it to the target."""
    operations = []
    links = []
    for number, fill in enumerate(fill_assets, 1):
        asset_operation = client.get_type("MutateOperation")
        asset = asset_operation.asset_operation.create
        asset.resource_name = f"customers/{customer_id}/assets/{-number}"
        fill(asset)
        operations.append(asset_operation)

        link_operation = client.get_type("MutateOperation")
        if campaign_resource_name:
            link = link_operation.campaign_asset_operation.create
            link.campaign = campaign_resource_name
        else:
            link = link_operation.customer_asset_operation.create
        link.asset = asset.resource_name
        link.field_type = client.enums.AssetFieldTypeEnum[field_type]
        links.append(link_operation)
    return operations + links


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def add_sitelinks(
    customer_id: str | int,
    sitelinks: List[Sitelink],
    campaign_id: str | int | None = None,
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Adds sitelinks: extra links under Search ads, for example one per program.

    The sitelinks are linked to the campaign, or to every campaign of the
    account when campaign_id is not given. Google shows the most relevant
    ones; at least 2 are needed for them to appear.

    By default the change is only validated, not applied. Show the returned
    changes to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer.
        sitelinks: 1 to 20 sitelinks.
        campaign_id: Optional campaign id; without it the sitelinks apply to the whole account.
        validate_only: If true, validates the change without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    if not 1 <= len(sitelinks) <= MAX_ASSETS_PER_CALL:
        raise ToolError(
            f"sitelinks needs between 1 and {MAX_ASSETS_PER_CALL} sitelinks."
        )
    cleaned = []
    for sitelink in sitelinks:
        (text,) = _clean_texts(
            [sitelink.text], "sitelink texts", 1, 1, MAX_SITELINK_TEXT
        )
        descriptions = [
            " ".join(d.split())
            for d in (sitelink.description1, sitelink.description2)
            if d and d.strip()
        ]
        if len(descriptions) == 1:
            raise ToolError(
                f"Sitelink {text!r} needs both description lines or none."
            )
        if descriptions:
            _clean_texts(
                descriptions,
                "sitelink descriptions",
                2,
                2,
                MAX_SITELINK_DESCRIPTION,
            )
        cleaned.append(
            (text, _check_final_url(sitelink.final_url), descriptions)
        )

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    campaign_resource_name, target = _target(client, customer_id, campaign_id)

    def filler(text, final_url, descriptions):
        def fill(asset):
            asset.final_urls.append(final_url)
            asset.sitelink_asset.link_text = text
            if descriptions:
                asset.sitelink_asset.description1 = descriptions[0]
                asset.sitelink_asset.description2 = descriptions[1]

        return fill

    operations = _asset_operations(
        client,
        customer_id,
        campaign_resource_name,
        "SITELINK",
        [filler(*entry) for entry in cleaned],
    )
    resource_names = _send_operations(
        client, customer_id, operations, validate_only
    )
    return _result(
        "add_sitelinks",
        customer_id,
        validate_only,
        [
            {
                "linked_to": target,
                "sitelinks": [
                    {
                        "text": text,
                        "final_url": final_url,
                        "descriptions": descriptions,
                    }
                    for text, final_url, descriptions in cleaned
                ],
            }
        ],
        resource_names,
    )


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def add_callouts(
    customer_id: str | int,
    callouts: List[str],
    campaign_id: str | int | None = None,
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Adds callouts: short highlights shown with Search ads.

    For example "100% online", "Acreditado" or "Becas disponibles". They are
    linked to the campaign, or to every campaign of the account when
    campaign_id is not given.

    By default the change is only validated, not applied. Show the returned
    changes to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer.
        callouts: 1 to 20 texts of at most 25 characters each.
        campaign_id: Optional campaign id; without it the callouts apply to the whole account.
        validate_only: If true, validates the change without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    callouts = _clean_texts(
        callouts, "callouts", 1, MAX_ASSETS_PER_CALL, MAX_CALLOUT_TEXT
    )
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    campaign_resource_name, target = _target(client, customer_id, campaign_id)

    def filler(text):
        def fill(asset):
            asset.callout_asset.callout_text = text

        return fill

    operations = _asset_operations(
        client,
        customer_id,
        campaign_resource_name,
        "CALLOUT",
        [filler(text) for text in callouts],
    )
    resource_names = _send_operations(
        client, customer_id, operations, validate_only
    )
    return _result(
        "add_callouts",
        customer_id,
        validate_only,
        [{"linked_to": target, "callouts": callouts}],
        resource_names,
    )


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def add_structured_snippet(
    customer_id: str | int,
    header: SnippetHeader,
    values: List[str],
    campaign_id: str | int | None = None,
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Adds a structured snippet: a header with a list of values under Search ads.

    For example header "Degree programs" (shown in Spanish as "Programas de
    grado") with values "Magíster en Educación", "MBA" and "Diplomados". The
    header is given in English and Google Ads translates it. The snippet is
    linked to the campaign, or to every campaign of the account when
    campaign_id is not given.

    By default the change is only validated, not applied. Show the returned
    changes to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer.
        header: The header of the snippet.
        values: 3 to 10 values of at most 25 characters each.
        campaign_id: Optional campaign id; without it the snippet applies to the whole account.
        validate_only: If true, validates the change without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    values = _clean_texts(values, "values", 3, 10, MAX_SNIPPET_VALUE)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    campaign_resource_name, target = _target(client, customer_id, campaign_id)

    def fill(asset):
        asset.structured_snippet_asset.header = header
        asset.structured_snippet_asset.values.extend(values)

    operations = _asset_operations(
        client,
        customer_id,
        campaign_resource_name,
        "STRUCTURED_SNIPPET",
        [fill],
    )
    resource_names = _send_operations(
        client, customer_id, operations, validate_only
    )
    return _result(
        "add_structured_snippet",
        customer_id,
        validate_only,
        [{"linked_to": target, "header": header, "values": values}],
        resource_names,
    )
