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

"""Tool that copies a Search campaign, for example to another country.

The copy is created in one atomic request: if anything fails, nothing is
created. It is registered in the `mutate` namespace.
"""

from typing import Any, Dict, List

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient

import ads_mcp.utils as utils
from ads_mcp.tools.create import (
    MAX_DESCRIPTION_LENGTH,
    MAX_HEADLINE_LENGTH,
    MAX_KEYWORD_LENGTH,
    MAX_PATH_LENGTH,
    Status,
    _check_date,
    _check_new_daily_budget,
    _clean_ids,
    _new_campaign_operations,
    _send_operations,
    _set_bidding,
)
from ads_mcp.tools.create_pmax import _search
from ads_mcp.tools.mutate import (
    _CREATE_ANNOTATIONS,
    _clean_id,
    _find_one,
    _from_micros,
    _result,
    _to_micros,
    mutate_mcp,
)

MAX_OPERATIONS = 5000
_FIRST_AD_GROUP_TEMP_ID = -3

# Campaign assets that are linked to the copy as they are.
_COPIED_ASSET_FIELD_TYPES = (
    "SITELINK",
    "CALLOUT",
    "STRUCTURED_SNIPPET",
    "CALL",
    "PRICE",
    "PROMOTION",
    "AD_IMAGE",
    "BUSINESS_NAME",
    "BUSINESS_LOGO",
)


class _Texts:
    """Applies the text replacements and collects texts that become invalid."""

    def __init__(self, replacements: Dict[str, str]):
        self.replacements = {
            old: new for old, new in replacements.items() if old
        }
        self.problems: List[str] = []

    def apply(self, text: str, what: str, max_length: int | None) -> str:
        for old, new in self.replacements.items():
            text = text.replace(old, new)
        text = " ".join(text.split())
        if not text:
            self.problems.append(f"{what} becomes empty")
        elif max_length and len(text) > max_length:
            self.problems.append(
                f"{what} {text!r} has {len(text)} characters, more than "
                f"{max_length}"
            )
        return text


def _campaign_row(client: GoogleAdsClient, customer_id: str, campaign_id: str):
    return _find_one(
        client,
        customer_id,
        "SELECT campaign.resource_name, campaign.name, "
        "campaign.advertising_channel_type, campaign.bidding_strategy_type, "
        "campaign.bidding_strategy, "
        "campaign.target_spend.cpc_bid_ceiling_micros, "
        "campaign.maximize_conversions.target_cpa_micros, "
        "campaign.maximize_conversion_value.target_roas, "
        "campaign.target_cpa.target_cpa_micros, "
        "campaign.target_roas.target_roas, "
        "campaign.network_settings.target_google_search, "
        "campaign.network_settings.target_search_network, "
        "campaign.network_settings.target_content_network, "
        "campaign.network_settings.target_partner_search_network, "
        "campaign_budget.amount_micros FROM campaign "
        f"WHERE campaign.id = {campaign_id}",
        f"Campaign {campaign_id} was not found in customer {customer_id}.",
    )


def _copy_bidding(source, campaign) -> str:
    """Gives the copy the bidding of the source and returns its name."""
    if source.bidding_strategy:
        # A portfolio strategy is shared, so the copy uses the same one.
        campaign.bidding_strategy = source.bidding_strategy
        return f"portfolio {source.bidding_strategy}"
    strategy = source.bidding_strategy_type.name
    if strategy == "MANUAL_CPC":
        _set_bidding(campaign, "MANUAL_CPC")
    elif strategy == "TARGET_SPEND":
        _set_bidding(
            campaign,
            "MAXIMIZE_CLICKS",
            cpc_bid_micros=source.target_spend.cpc_bid_ceiling_micros,
        )
    elif strategy in ("MAXIMIZE_CONVERSIONS", "TARGET_CPA"):
        _set_bidding(
            campaign,
            "MAXIMIZE_CONVERSIONS",
            target_cpa_micros=(
                source.maximize_conversions.target_cpa_micros
                or source.target_cpa.target_cpa_micros
            ),
        )
    elif strategy in ("MAXIMIZE_CONVERSION_VALUE", "TARGET_ROAS"):
        _set_bidding(
            campaign,
            "MAXIMIZE_CONVERSION_VALUE",
            target_roas=(
                source.maximize_conversion_value.target_roas
                or source.target_roas.target_roas
            ),
        )
    else:
        raise ToolError(
            f"The campaign uses {strategy} bidding, which this tool cannot "
            "copy yet."
        )
    return strategy


def _text_asset(client: GoogleAdsClient, source_asset, text: str):
    asset = client.get_type("AdTextAsset")
    asset.text = text
    if source_asset.pinned_field:
        asset.pinned_field = source_asset.pinned_field
    return asset


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def clone_search_campaign(
    customer_id: str | int,
    source_campaign_id: str | int,
    new_campaign_name: str,
    location_ids: List[str | int],
    language_ids: List[str | int] | None = None,
    daily_budget: float | None = None,
    text_replacements: Dict[str, str] = {},
    url_replacements: Dict[str, str] = {},
    start_date: str | None = None,
    end_date: str | None = None,
    status: Status = "ENABLED",
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Copies a Search campaign, for example to launch it in another country.

    The copy gets its own budget and the given locations, and keeps the
    source's bidding, networks, languages, ad schedule, negative keywords,
    shared negative keyword lists, sitelinks, callouts and other campaign
    assets, plus every ad group with its keywords, negative keywords and
    responsive search ads. Removed items are left out.

    text_replacements adapts the copy: each key is replaced by its value in
    ad group names, keywords, headlines, descriptions and display paths, for
    example {"Chile": "Perú"}. url_replacements does the same in the final
    URLs of the ads, for example {"/cl/": "/pe/"}. Sitelinks and other
    assets are shared, so their URLs are not changed. Texts that become too
    long are reported and nothing is created.

    Everything is created in one atomic request. By default it is only
    validated, not created. Show the returned summary to the user and call
    again with validate_only=false only after they confirm.

    Args:
        customer_id: The id of the customer.
        source_campaign_id: The id of the Search campaign to copy.
        new_campaign_name: The name of the copy, unique in the account.
        location_ids: Geo target constant ids for the copy, for example 2604 for Peru. Find others with the search tool on the geo_target_constant resource.
        language_ids: Language constant ids; defaults to the source's languages.
        daily_budget: Daily budget in the account currency; defaults to the source's budget.
        text_replacements: Texts to replace in names, keywords and ads, as {"old": "new"}.
        url_replacements: Texts to replace in the final URLs of the ads, as {"old": "new"}.
        start_date: Optional start date as YYYY-MM-DD; defaults to today.
        end_date: Optional end date as YYYY-MM-DD.
        status: ENABLED starts serving once ads are approved; PAUSED creates it paused.
        validate_only: If true, validates the copy without creating it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    campaign_id = _clean_id(source_campaign_id, "source_campaign_id")
    new_campaign_name = " ".join(new_campaign_name.split())
    if not new_campaign_name:
        raise ToolError("new_campaign_name cannot be empty.")
    location_ids = _clean_ids(location_ids, "location_ids")
    start = _check_date(start_date, "start_date")
    end = _check_date(end_date, "end_date")
    if start and end and end < start:
        raise ToolError("end_date cannot be before start_date.")
    texts = _Texts(text_replacements)
    url_texts = _Texts(url_replacements)

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    source = _campaign_row(client, customer_id, campaign_id)
    if source.campaign.advertising_channel_type.name != "SEARCH":
        raise ToolError(
            f"Campaign {campaign_id} is a "
            f"{source.campaign.advertising_channel_type.name} campaign; only "
            "Search campaigns can be copied."
        )
    budget_micros = (
        _to_micros(daily_budget, "daily_budget")
        if daily_budget is not None
        else source.campaign_budget.amount_micros
    )
    currency = _check_new_daily_budget(client, customer_id, budget_micros)

    campaign_filter = f"campaign.id = {campaign_id}"
    criteria = _search(
        client,
        customer_id,
        "SELECT campaign_criterion.type, campaign_criterion.negative, "
        "campaign_criterion.bid_modifier, "
        "campaign_criterion.language.language_constant, "
        "campaign_criterion.keyword.text, "
        "campaign_criterion.keyword.match_type, "
        "campaign_criterion.ad_schedule.day_of_week, "
        "campaign_criterion.ad_schedule.start_hour, "
        "campaign_criterion.ad_schedule.start_minute, "
        "campaign_criterion.ad_schedule.end_hour, "
        "campaign_criterion.ad_schedule.end_minute FROM campaign_criterion "
        f"WHERE {campaign_filter} AND campaign_criterion.status != 'REMOVED' "
        "AND campaign_criterion.type IN ('LANGUAGE', 'KEYWORD', "
        "'AD_SCHEDULE')",
    )
    if language_ids is None:
        language_ids = [
            row.campaign_criterion.language.language_constant.rsplit("/", 1)[1]
            for row in criteria
            if row.campaign_criterion.type_.name == "LANGUAGE"
        ]
    language_ids = _clean_ids(language_ids, "language_ids")

    operations, campaign = _new_campaign_operations(
        client,
        customer_id,
        new_campaign_name,
        "SEARCH",
        budget_micros,
        status,
        start,
        end,
        location_ids,
        language_ids,
    )
    campaign_resource_name = campaign.resource_name
    network = source.campaign.network_settings
    campaign.network_settings.target_google_search = (
        network.target_google_search
    )
    campaign.network_settings.target_search_network = (
        network.target_search_network
    )
    campaign.network_settings.target_content_network = (
        network.target_content_network
    )
    campaign.network_settings.target_partner_search_network = (
        network.target_partner_search_network
    )
    bidding = _copy_bidding(source.campaign, campaign)

    def new_operation():
        operation = client.get_type("MutateOperation")
        operations.append(operation)
        return operation

    negative_count = schedule_count = 0
    for row in criteria:
        source_criterion = row.campaign_criterion
        kind = source_criterion.type_.name
        if kind == "KEYWORD" and source_criterion.negative:
            criterion = new_operation().campaign_criterion_operation.create
            criterion.campaign = campaign_resource_name
            criterion.negative = True
            criterion.keyword.text = texts.apply(
                source_criterion.keyword.text,
                "negative keyword",
                MAX_KEYWORD_LENGTH,
            )
            criterion.keyword.match_type = source_criterion.keyword.match_type
            negative_count += 1
        elif kind == "AD_SCHEDULE":
            criterion = new_operation().campaign_criterion_operation.create
            criterion.campaign = campaign_resource_name
            client.copy_from(
                criterion.ad_schedule, source_criterion.ad_schedule
            )
            if source_criterion.bid_modifier:
                criterion.bid_modifier = source_criterion.bid_modifier
            schedule_count += 1

    shared_sets = _search(
        client,
        customer_id,
        "SELECT campaign_shared_set.shared_set FROM campaign_shared_set "
        f"WHERE {campaign_filter} AND campaign_shared_set.status = 'ENABLED'",
    )
    for row in shared_sets:
        link = new_operation().campaign_shared_set_operation.create
        link.campaign = campaign_resource_name
        link.shared_set = row.campaign_shared_set.shared_set

    field_types = ", ".join(f"'{name}'" for name in _COPIED_ASSET_FIELD_TYPES)
    campaign_assets = _search(
        client,
        customer_id,
        "SELECT campaign_asset.asset, campaign_asset.field_type FROM "
        "campaign_asset WHERE campaign_asset.campaign = "
        f"'{source.campaign.resource_name}' AND campaign_asset.status != "
        f"'REMOVED' AND campaign_asset.field_type IN ({field_types})",
    )
    for row in campaign_assets:
        link = new_operation().campaign_asset_operation.create
        link.campaign = campaign_resource_name
        link.asset = row.campaign_asset.asset
        link.field_type = row.campaign_asset.field_type

    ad_groups = _search(
        client,
        customer_id,
        "SELECT ad_group.id, ad_group.name, ad_group.status, "
        "ad_group.cpc_bid_micros FROM ad_group WHERE "
        f"{campaign_filter} AND ad_group.status != 'REMOVED'",
    )
    new_ad_groups: Dict[int, str] = {}
    for index, row in enumerate(ad_groups):
        resource_name = (
            f"customers/{customer_id}/adGroups/"
            f"{_FIRST_AD_GROUP_TEMP_ID - index}"
        )
        new_ad_groups[row.ad_group.id] = resource_name
        ad_group = new_operation().ad_group_operation.create
        ad_group.resource_name = resource_name
        ad_group.name = texts.apply(row.ad_group.name, "ad group name", 255)
        ad_group.campaign = campaign_resource_name
        ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
        ad_group.status = row.ad_group.status
        if row.ad_group.cpc_bid_micros:
            ad_group.cpc_bid_micros = row.ad_group.cpc_bid_micros

    keywords = _search(
        client,
        customer_id,
        "SELECT ad_group.id, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type, ad_group_criterion.negative, "
        "ad_group_criterion.status, ad_group_criterion.cpc_bid_micros FROM "
        f"ad_group_criterion WHERE {campaign_filter} AND "
        "ad_group_criterion.type = 'KEYWORD' AND ad_group_criterion.status "
        "!= 'REMOVED' AND ad_group.status != 'REMOVED'",
    )
    keyword_count = 0
    for row in keywords:
        source_criterion = row.ad_group_criterion
        criterion = new_operation().ad_group_criterion_operation.create
        criterion.ad_group = new_ad_groups[row.ad_group.id]
        criterion.keyword.text = texts.apply(
            source_criterion.keyword.text, "keyword", MAX_KEYWORD_LENGTH
        )
        criterion.keyword.match_type = source_criterion.keyword.match_type
        if source_criterion.negative:
            criterion.negative = True
            negative_count += 1
        else:
            criterion.status = source_criterion.status
            if source_criterion.cpc_bid_micros:
                criterion.cpc_bid_micros = source_criterion.cpc_bid_micros
            keyword_count += 1

    ads = _search(
        client,
        customer_id,
        "SELECT ad_group.id, ad_group_ad.status, ad_group_ad.ad.final_urls, "
        "ad_group_ad.ad.responsive_search_ad.headlines, "
        "ad_group_ad.ad.responsive_search_ad.descriptions, "
        "ad_group_ad.ad.responsive_search_ad.path1, "
        "ad_group_ad.ad.responsive_search_ad.path2 FROM ad_group_ad WHERE "
        f"{campaign_filter} AND ad_group_ad.status != 'REMOVED' AND "
        "ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD' AND ad_group.status != "
        "'REMOVED'",
    )
    for row in ads:
        source_ad = row.ad_group_ad.ad
        rsa = source_ad.responsive_search_ad
        ad_group_ad = new_operation().ad_group_ad_operation.create
        ad_group_ad.ad_group = new_ad_groups[row.ad_group.id]
        ad_group_ad.status = row.ad_group_ad.status
        for url in source_ad.final_urls:
            ad_group_ad.ad.final_urls.append(
                url_texts.apply(url, "final URL", None)
            )
        new_rsa = ad_group_ad.ad.responsive_search_ad
        for headline in rsa.headlines:
            new_rsa.headlines.append(
                _text_asset(
                    client,
                    headline,
                    texts.apply(headline.text, "headline", MAX_HEADLINE_LENGTH),
                )
            )
        for description in rsa.descriptions:
            new_rsa.descriptions.append(
                _text_asset(
                    client,
                    description,
                    texts.apply(
                        description.text, "description", MAX_DESCRIPTION_LENGTH
                    ),
                )
            )
        for path in ("path1", "path2"):
            if getattr(rsa, path):
                setattr(
                    new_rsa,
                    path,
                    texts.apply(getattr(rsa, path), path, MAX_PATH_LENGTH),
                )

    problems = texts.problems + url_texts.problems
    if problems:
        raise ToolError(
            "After the replacements these texts are not valid, nothing was "
            "created:\n- " + "\n- ".join(dict.fromkeys(problems))
        )
    if len(operations) > MAX_OPERATIONS:
        raise ToolError(
            f"The copy needs {len(operations)} operations, more than the "
            f"{MAX_OPERATIONS} allowed in one request."
        )

    resource_names = _send_operations(
        client, customer_id, operations, validate_only
    )
    warnings = []
    if status == "ENABLED":
        warnings.append(
            "The copy is created ENABLED: it starts serving and spending as "
            "soon as its ads are approved."
        )
    if campaign_assets and url_replacements:
        warnings.append(
            "Sitelinks and other assets are shared with the source campaign, "
            "so their URLs were not changed."
        )
    return _result(
        "clone_search_campaign",
        customer_id,
        validate_only,
        [
            {
                "source_campaign": source.campaign.name,
                "new_campaign": new_campaign_name,
                "status": status,
                "currency": currency,
                "daily_budget": _from_micros(budget_micros),
                "bidding": bidding,
                "location_ids": location_ids,
                "language_ids": language_ids,
                "ad_groups": len(ad_groups),
                "keywords": keyword_count,
                "negative_keywords": negative_count,
                "responsive_search_ads": len(ads),
                "ad_schedules": schedule_count,
                "shared_negative_lists": len(shared_sets),
                "campaign_assets": len(campaign_assets),
                "text_replacements": texts.replacements,
                "url_replacements": url_texts.replacements,
            }
        ],
        resource_names,
        warnings,
    )
