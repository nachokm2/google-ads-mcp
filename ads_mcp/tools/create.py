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

"""Tools for creating Search campaigns, ad groups, keywords and ads.

They are registered in the `mutate` namespace, so they are enabled together
with the other write tools. Like them, they only validate their changes unless
the caller passes validate_only=False.
"""

import datetime
import os
from typing import Any, Dict, List, Literal

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

import ads_mcp.utils as utils
from ads_mcp.tools.mutate import (
    _CREATE_ANNOTATIONS,
    _clean_id,
    _find_one,
    _from_micros,
    _get_service,
    _raise_tool_error,
    _result,
    _to_micros,
    mutate_mcp,
)

# Optional per-currency limit on the daily budget of new campaigns, for
# example "CLP:50000,USD:100". When it is set, currencies it does not list
# cannot be used for new budgets.
MAX_NEW_DAILY_BUDGET_ENV_VAR = "GOOGLE_ADS_MCP_MAX_NEW_DAILY_BUDGET"

DEFAULT_LOCATION_IDS = ["2152"]  # Chile
DEFAULT_LANGUAGE_IDS = ["1003"]  # Spanish

MAX_HEADLINE_LENGTH = 30
MAX_DESCRIPTION_LENGTH = 90
MAX_PATH_LENGTH = 15
MAX_KEYWORD_LENGTH = 80
MAX_KEYWORDS_PER_CALL = 200

# Temporary ids let a single atomic request create resources that reference
# each other before they exist.
_BUDGET_TEMP_ID = -1
_CAMPAIGN_TEMP_ID = -2
_AD_GROUP_TEMP_ID = -3

MatchType = Literal["EXACT", "PHRASE", "BROAD"]
Status = Literal["ENABLED", "PAUSED"]


def _clean_texts(
    texts: List[str], name: str, min_count: int, max_count: int, max_length: int
) -> List[str]:
    """Normalizes whitespace, drops empty and duplicate texts and validates them."""
    cleaned = []
    seen = set()
    for text in texts:
        text = " ".join(text.split())
        if text and text.lower() not in seen:
            seen.add(text.lower())
            cleaned.append(text)
    if not min_count <= len(cleaned) <= max_count:
        raise ToolError(
            f"{name} needs between {min_count} and {max_count} unique texts, "
            f"got {len(cleaned)}."
        )
    too_long = [text for text in cleaned if len(text) > max_length]
    if too_long:
        raise ToolError(
            f"These {name} are longer than {max_length} characters: {too_long}"
        )
    return cleaned


def _check_final_url(final_url: str) -> str:
    final_url = final_url.strip()
    if not final_url.startswith(("https://", "http://")):
        raise ToolError(
            f"final_url must start with https:// or http://, got {final_url!r}."
        )
    return final_url


def _check_paths(path1: str | None, path2: str | None) -> None:
    if path2 and not path1:
        raise ToolError("path2 can only be used together with path1.")
    for path in (path1, path2):
        if path and len(path) > MAX_PATH_LENGTH:
            raise ToolError(
                f"Display URL paths can have at most {MAX_PATH_LENGTH} "
                f"characters, got {path!r}."
            )


def _check_date(value: str | None, name: str) -> datetime.date | None:
    if value is None:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise ToolError(
            f"{name} must use the YYYY-MM-DD format, got {value!r}."
        )


def _clean_ids(values: List[str | int], name: str) -> List[str]:
    cleaned = [_clean_id(value, name) for value in values]
    if not cleaned:
        raise ToolError(f"{name} must contain at least one id.")
    return list(dict.fromkeys(cleaned))


def _max_new_daily_budget(currency: str) -> float | None:
    raw_value = os.environ.get(MAX_NEW_DAILY_BUDGET_ENV_VAR)
    if not raw_value:
        return None
    limits = {}
    for entry in raw_value.replace(",", " ").split():
        code, _, amount = entry.partition(":")
        try:
            limits[code.upper()] = float(amount)
        except ValueError:
            raise ToolError(
                f"{MAX_NEW_DAILY_BUDGET_ENV_VAR} entries must look like "
                f"CLP:50000, got {entry!r}."
            )
    if currency not in limits:
        raise ToolError(
            f"{MAX_NEW_DAILY_BUDGET_ENV_VAR} sets no limit for {currency}, so "
            f"new budgets in {currency} are not allowed. Ask the server "
            "administrator to add one."
        )
    return limits[currency]


def _currency_code(client: GoogleAdsClient, customer_id: str) -> str:
    row = _find_one(
        client,
        customer_id,
        "SELECT customer.currency_code FROM customer",
        f"Customer {customer_id} was not found.",
    )
    return row.customer.currency_code


def _check_new_daily_budget(
    client: GoogleAdsClient, customer_id: str, budget_micros: int
) -> str:
    """Applies the server limit for new budgets and returns the currency."""
    currency = _currency_code(client, customer_id)
    max_budget = _max_new_daily_budget(currency)
    if max_budget is not None and _from_micros(budget_micros) > max_budget:
        raise ToolError(
            f"The daily budget of {_from_micros(budget_micros):g} {currency} "
            f"is above the {max_budget:g} {currency} this server allows for "
            f"new campaigns. Use a smaller budget, or ask the server "
            f"administrator to raise {MAX_NEW_DAILY_BUDGET_ENV_VAR}."
        )
    return currency


def _set_bidding(
    campaign,
    bidding_strategy: str,
    cpc_bid_micros: int | None = None,
    target_cpa_micros: int | None = None,
    target_roas: float | None = None,
) -> None:
    """Sets a standard bidding strategy on a campaign being created.

    cpc_bid_micros is the max CPC limit of MAXIMIZE_CLICKS; for MANUAL_CPC
    the bid belongs to the ad group.
    """
    # SetInParent selects the strategy even when none of its fields are set.
    campaign_pb = type(campaign).pb(campaign)
    if bidding_strategy == "MANUAL_CPC":
        campaign_pb.manual_cpc.SetInParent()
    elif bidding_strategy == "MAXIMIZE_CLICKS":
        campaign_pb.target_spend.SetInParent()
        if cpc_bid_micros:
            campaign.target_spend.cpc_bid_ceiling_micros = cpc_bid_micros
    elif bidding_strategy == "MAXIMIZE_CONVERSIONS":
        campaign_pb.maximize_conversions.SetInParent()
        if target_cpa_micros:
            campaign.maximize_conversions.target_cpa_micros = target_cpa_micros
    elif bidding_strategy == "MAXIMIZE_CONVERSION_VALUE":
        campaign_pb.maximize_conversion_value.SetInParent()
        if target_roas:
            campaign.maximize_conversion_value.target_roas = target_roas
    else:
        raise ToolError(f"Unsupported bidding strategy {bidding_strategy!r}.")


def _new_campaign_operations(
    client: GoogleAdsClient,
    customer_id: str,
    campaign_name: str,
    channel_type: str,
    budget_micros: int,
    status: str,
    start: datetime.date | None,
    end: datetime.date | None,
    location_ids: List[str],
    language_ids: List[str],
    budget_temp_id: int = _BUDGET_TEMP_ID,
    campaign_temp_id: int = _CAMPAIGN_TEMP_ID,
):
    """Builds the budget, campaign and targeting operations of a new campaign.

    Returns the operations and the campaign being created, so the caller can
    set the fields specific to its channel type. Requests that create several
    campaigns pass different temporary ids for each one.
    """
    budget_resource_name = (
        f"customers/{customer_id}/campaignBudgets/{budget_temp_id}"
    )
    campaign_resource_name = (
        f"customers/{customer_id}/campaigns/{campaign_temp_id}"
    )

    budget_operation = client.get_type("MutateOperation")
    budget = budget_operation.campaign_budget_operation.create
    budget.resource_name = budget_resource_name
    # Budget names must be unique, so the creation time is added.
    budget.name = (
        f"{campaign_name} budget {datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
    )
    budget.amount_micros = budget_micros
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False

    campaign_operation = client.get_type("MutateOperation")
    campaign = campaign_operation.campaign_operation.create
    campaign.resource_name = campaign_resource_name
    campaign.name = campaign_name
    campaign.status = client.enums.CampaignStatusEnum[status]
    campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum[
        channel_type
    ]
    campaign.campaign_budget = budget_resource_name
    campaign.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )
    campaign.geo_target_type_setting.positive_geo_target_type = (
        client.enums.PositiveGeoTargetTypeEnum.PRESENCE
    )
    if start:
        campaign.start_date_time = f"{start} 00:00:00"
    if end:
        campaign.end_date_time = f"{end} 23:59:59"

    operations = [budget_operation, campaign_operation]
    for location_id in location_ids:
        operation = client.get_type("MutateOperation")
        criterion = operation.campaign_criterion_operation.create
        criterion.campaign = campaign_resource_name
        criterion.location.geo_target_constant = (
            f"geoTargetConstants/{location_id}"
        )
        operations.append(operation)
    for language_id in language_ids:
        operation = client.get_type("MutateOperation")
        criterion = operation.campaign_criterion_operation.create
        criterion.campaign = campaign_resource_name
        criterion.language.language_constant = (
            f"languageConstants/{language_id}"
        )
        operations.append(operation)
    return operations, campaign


def _send_operations(
    client: GoogleAdsClient,
    customer_id: str,
    operations: List[Any],
    validate_only: bool,
) -> List[str]:
    """Sends the operations in one atomic request and returns the new resources."""
    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = customer_id
    request.mutate_operations.extend(operations)
    request.validate_only = validate_only
    ga_service = _get_service(client, "GoogleAdsService")
    try:
        response = ga_service.mutate(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)
    resource_names = []
    for operation_response in response.mutate_operation_responses:
        result_field = (
            type(operation_response)
            .pb(operation_response)
            .WhichOneof("response")
        )
        if result_field:
            resource_names.append(
                getattr(operation_response, result_field).resource_name
            )
    return resource_names


def _keyword_operation(
    client: GoogleAdsClient,
    ad_group_resource_name: str,
    text: str,
    match_type: str,
    cpc_bid_micros: int | None = None,
):
    operation = client.get_type("MutateOperation")
    criterion = operation.ad_group_criterion_operation.create
    criterion.ad_group = ad_group_resource_name
    criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
    criterion.keyword.text = text
    criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]
    if cpc_bid_micros:
        criterion.cpc_bid_micros = cpc_bid_micros
    return operation


def _responsive_search_ad_operation(
    client: GoogleAdsClient,
    ad_group_resource_name: str,
    final_url: str,
    headlines: List[str],
    descriptions: List[str],
    path1: str | None,
    path2: str | None,
    status: str,
):
    operation = client.get_type("MutateOperation")
    ad_group_ad = operation.ad_group_ad_operation.create
    ad_group_ad.ad_group = ad_group_resource_name
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum[status]
    ad = ad_group_ad.ad
    ad.final_urls.append(final_url)
    for text in headlines:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        ad.responsive_search_ad.headlines.append(asset)
    for text in descriptions:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        ad.responsive_search_ad.descriptions.append(asset)
    if path1:
        ad.responsive_search_ad.path1 = path1
    if path2:
        ad.responsive_search_ad.path2 = path2
    return operation


def _ad_texts(
    headlines: List[str],
    descriptions: List[str],
    final_url: str,
    path1: str | None,
    path2: str | None,
):
    """Validates the parts of a responsive search ad."""
    _check_paths(path1, path2)
    return (
        _check_final_url(final_url),
        _clean_texts(headlines, "headlines", 3, 15, MAX_HEADLINE_LENGTH),
        _clean_texts(
            descriptions, "descriptions", 2, 4, MAX_DESCRIPTION_LENGTH
        ),
    )


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def create_search_campaign(
    customer_id: str | int,
    campaign_name: str,
    daily_budget: float,
    final_url: str,
    headlines: List[str],
    descriptions: List[str],
    keywords: List[str],
    keyword_match_type: MatchType = "PHRASE",
    bidding_strategy: Literal[
        "MANUAL_CPC", "MAXIMIZE_CLICKS", "MAXIMIZE_CONVERSIONS"
    ] = "MAXIMIZE_CLICKS",
    cpc_bid: float | None = None,
    target_cpa: float | None = None,
    ad_group_name: str | None = None,
    location_ids: List[str | int] = DEFAULT_LOCATION_IDS,
    language_ids: List[str | int] = DEFAULT_LANGUAGE_IDS,
    include_search_partners: bool = False,
    path1: str | None = None,
    path2: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    status: Status = "ENABLED",
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Creates a complete Search campaign in one atomic step.

    Creates the daily budget, the campaign, its location and language
    targeting, one ad group, its keywords and one responsive search ad. If any
    part fails, nothing is created. The campaign only shows on Google Search
    (and optionally search partners), never on the Display network.

    Amounts are in the account currency, not in micros. With status ENABLED
    (the default) the campaign starts serving and spending as soon as its ads
    are approved; use PAUSED to review it first.

    By default the campaign is only validated, not created. Show the returned
    summary to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer to create the campaign in.
        campaign_name: The name of the campaign; it must be unique in the account.
        daily_budget: The average daily budget in the account currency.
        final_url: The landing page of the ad, starting with https://.
        headlines: 3 to 15 ad headlines of at most 30 characters each.
        descriptions: 2 to 4 ad descriptions of at most 90 characters each.
        keywords: The keywords of the ad group, at most 200.
        keyword_match_type: The match type of all the keywords.
        bidding_strategy: MANUAL_CPC (needs cpc_bid), MAXIMIZE_CLICKS (cpc_bid is an optional max CPC limit) or MAXIMIZE_CONVERSIONS (target_cpa is optional).
        cpc_bid: The max CPC bid in the account currency.
        target_cpa: The target cost per conversion for MAXIMIZE_CONVERSIONS.
        ad_group_name: The name of the ad group; defaults to the campaign name.
        location_ids: Geo target constant ids to target; defaults to Chile (2152). Find others with the search tool on the geo_target_constant resource.
        language_ids: Language constant ids to target; defaults to Spanish (1003).
        include_search_partners: Whether to also show ads on Google search partner sites.
        path1: Optional first path of the display URL, at most 15 characters.
        path2: Optional second path of the display URL, at most 15 characters.
        start_date: Optional start date as YYYY-MM-DD; defaults to today.
        end_date: Optional end date as YYYY-MM-DD.
        status: The status of the new campaign.
        validate_only: If true, validates the campaign without creating it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    campaign_name = " ".join(campaign_name.split())
    if not campaign_name:
        raise ToolError("campaign_name cannot be empty.")
    ad_group_name = " ".join((ad_group_name or campaign_name).split())
    budget_micros = _to_micros(daily_budget, "daily_budget")
    final_url, headlines, descriptions = _ad_texts(
        headlines, descriptions, final_url, path1, path2
    )
    keywords = _clean_texts(
        keywords, "keywords", 1, MAX_KEYWORDS_PER_CALL, MAX_KEYWORD_LENGTH
    )
    location_ids = _clean_ids(location_ids, "location_ids")
    language_ids = _clean_ids(language_ids, "language_ids")
    start = _check_date(start_date, "start_date")
    end = _check_date(end_date, "end_date")
    if start and end and end < start:
        raise ToolError("end_date cannot be before start_date.")
    if bidding_strategy == "MANUAL_CPC" and cpc_bid is None:
        raise ToolError("MANUAL_CPC bidding needs cpc_bid.")
    cpc_bid_micros = _to_micros(cpc_bid, "cpc_bid") if cpc_bid else None
    target_cpa_micros = (
        _to_micros(target_cpa, "target_cpa") if target_cpa else None
    )

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    currency = _check_new_daily_budget(client, customer_id, budget_micros)

    operations, campaign = _new_campaign_operations(
        client,
        customer_id,
        campaign_name,
        "SEARCH",
        budget_micros,
        status,
        start,
        end,
        location_ids,
        language_ids,
    )
    campaign_resource_name = campaign.resource_name
    ad_group_resource_name = (
        f"customers/{customer_id}/adGroups/{_AD_GROUP_TEMP_ID}"
    )
    campaign.network_settings.target_google_search = True
    campaign.network_settings.target_search_network = include_search_partners
    campaign.network_settings.target_content_network = False
    campaign.network_settings.target_partner_search_network = False
    _set_bidding(campaign, bidding_strategy, cpc_bid_micros, target_cpa_micros)

    ad_group_operation = client.get_type("MutateOperation")
    ad_group = ad_group_operation.ad_group_operation.create
    ad_group.resource_name = ad_group_resource_name
    ad_group.name = ad_group_name
    ad_group.campaign = campaign_resource_name
    ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    if bidding_strategy == "MANUAL_CPC":
        ad_group.cpc_bid_micros = cpc_bid_micros
    operations.append(ad_group_operation)

    for keyword in keywords:
        operations.append(
            _keyword_operation(
                client, ad_group_resource_name, keyword, keyword_match_type
            )
        )
    operations.append(
        _responsive_search_ad_operation(
            client,
            ad_group_resource_name,
            final_url,
            headlines,
            descriptions,
            path1,
            path2,
            "ENABLED",
        )
    )

    resource_names = _send_operations(
        client, customer_id, operations, validate_only
    )
    warnings = []
    if status == "ENABLED":
        warnings.append(
            "The campaign is created ENABLED: it starts serving and spending "
            "as soon as its ads are approved."
        )
    return _result(
        "create_search_campaign",
        customer_id,
        validate_only,
        [
            {
                "campaign": campaign_name,
                "status": status,
                "currency": currency,
                "daily_budget": _from_micros(budget_micros),
                "bidding_strategy": bidding_strategy,
                "cpc_bid": (
                    _from_micros(cpc_bid_micros) if cpc_bid_micros else None
                ),
                "target_cpa": (
                    _from_micros(target_cpa_micros)
                    if target_cpa_micros
                    else None
                ),
                "location_ids": location_ids,
                "language_ids": language_ids,
                "include_search_partners": include_search_partners,
                "start_date": str(start) if start else "today",
                "end_date": str(end) if end else None,
                "ad_group": ad_group_name,
                "keywords": keywords,
                "keyword_match_type": keyword_match_type,
                "final_url": final_url,
                "headlines": headlines,
                "descriptions": descriptions,
            }
        ],
        resource_names,
        warnings,
    )


def _find_search_campaign(
    client: GoogleAdsClient, customer_id: str, campaign_id: str
):
    row = _find_one(
        client,
        customer_id,
        "SELECT campaign.resource_name, campaign.name, "
        "campaign.advertising_channel_type, campaign.bidding_strategy_type "
        f"FROM campaign WHERE campaign.id = {campaign_id}",
        f"Campaign {campaign_id} was not found in customer {customer_id}.",
    )
    if row.campaign.advertising_channel_type.name != "SEARCH":
        raise ToolError(
            f"Campaign {campaign_id} is a "
            f"{row.campaign.advertising_channel_type.name} campaign; this tool "
            "only works with Search campaigns."
        )
    return row


def _find_search_ad_group(
    client: GoogleAdsClient, customer_id: str, ad_group_id: str
):
    row = _find_one(
        client,
        customer_id,
        "SELECT ad_group.resource_name, ad_group.name, campaign.name, "
        "campaign.advertising_channel_type FROM ad_group "
        f"WHERE ad_group.id = {ad_group_id}",
        f"Ad group {ad_group_id} was not found in customer {customer_id}.",
    )
    if row.campaign.advertising_channel_type.name != "SEARCH":
        raise ToolError(
            f"Ad group {ad_group_id} belongs to a "
            f"{row.campaign.advertising_channel_type.name} campaign; this tool "
            "only works with Search campaigns."
        )
    return row


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def create_ad_group(
    customer_id: str | int,
    campaign_id: str | int,
    ad_group_name: str,
    keywords: List[str] = [],
    keyword_match_type: MatchType = "PHRASE",
    cpc_bid: float | None = None,
    final_url: str | None = None,
    headlines: List[str] = [],
    descriptions: List[str] = [],
    path1: str | None = None,
    path2: str | None = None,
    status: Status = "ENABLED",
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Creates an ad group in an existing Search campaign.

    Optionally also creates its keywords and one responsive search ad in the
    same atomic step: pass final_url, headlines and descriptions to create the
    ad. cpc_bid (in the account currency) is required when the campaign uses
    manual CPC bidding.

    By default the change is only validated, not applied. Show the returned
    changes to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer that owns the campaign.
        campaign_id: The id of the Search campaign (campaign.id).
        ad_group_name: The name of the new ad group; it must be unique in the campaign.
        keywords: Optional keywords for the ad group, at most 200.
        keyword_match_type: The match type of all the keywords.
        cpc_bid: The default max CPC bid of the ad group in the account currency.
        final_url: The landing page of the ad, starting with https://.
        headlines: 3 to 15 ad headlines of at most 30 characters each.
        descriptions: 2 to 4 ad descriptions of at most 90 characters each.
        path1: Optional first path of the display URL, at most 15 characters.
        path2: Optional second path of the display URL, at most 15 characters.
        status: The status of the new ad group.
        validate_only: If true, validates the change without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    campaign_id = _clean_id(campaign_id, "campaign_id")
    ad_group_name = " ".join(ad_group_name.split())
    if not ad_group_name:
        raise ToolError("ad_group_name cannot be empty.")
    if keywords:
        keywords = _clean_texts(
            keywords, "keywords", 1, MAX_KEYWORDS_PER_CALL, MAX_KEYWORD_LENGTH
        )
    with_ad = bool(final_url or headlines or descriptions)
    if with_ad:
        if not final_url:
            raise ToolError("final_url is required to create the ad.")
        final_url, headlines, descriptions = _ad_texts(
            headlines, descriptions, final_url, path1, path2
        )
    cpc_bid_micros = _to_micros(cpc_bid, "cpc_bid") if cpc_bid else None

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    row = _find_search_campaign(client, customer_id, campaign_id)
    if (
        row.campaign.bidding_strategy_type.name == "MANUAL_CPC"
        and not cpc_bid_micros
    ):
        raise ToolError(
            f"Campaign {campaign_id} uses manual CPC bidding, so the ad group "
            "needs cpc_bid."
        )

    ad_group_resource_name = (
        f"customers/{customer_id}/adGroups/{_AD_GROUP_TEMP_ID}"
    )
    ad_group_operation = client.get_type("MutateOperation")
    ad_group = ad_group_operation.ad_group_operation.create
    ad_group.resource_name = ad_group_resource_name
    ad_group.name = ad_group_name
    ad_group.campaign = row.campaign.resource_name
    ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    ad_group.status = client.enums.AdGroupStatusEnum[status]
    if cpc_bid_micros:
        ad_group.cpc_bid_micros = cpc_bid_micros
    operations = [ad_group_operation]
    for keyword in keywords:
        operations.append(
            _keyword_operation(
                client, ad_group_resource_name, keyword, keyword_match_type
            )
        )
    if with_ad:
        operations.append(
            _responsive_search_ad_operation(
                client,
                ad_group_resource_name,
                final_url,
                headlines,
                descriptions,
                path1,
                path2,
                "ENABLED",
            )
        )

    resource_names = _send_operations(
        client, customer_id, operations, validate_only
    )
    change: Dict[str, Any] = {
        "campaign": row.campaign.name,
        "ad_group": ad_group_name,
        "status": status,
        "cpc_bid": _from_micros(cpc_bid_micros) if cpc_bid_micros else None,
        "keywords": keywords,
        "keyword_match_type": keyword_match_type if keywords else None,
    }
    if with_ad:
        change.update(
            {
                "final_url": final_url,
                "headlines": headlines,
                "descriptions": descriptions,
            }
        )
    return _result(
        "create_ad_group", customer_id, validate_only, [change], resource_names
    )


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def add_keywords(
    customer_id: str | int,
    ad_group_id: str | int,
    keywords: List[str],
    match_type: MatchType,
    cpc_bid: float | None = None,
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Adds keywords to an ad group of a Search campaign.

    Duplicate keywords in the list are sent once. All keywords are added or,
    if any of them fails, none is. To exclude searches, use
    add_negative_keywords instead.

    By default the change is only validated, not applied. Show the returned
    changes to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer that owns the ad group.
        ad_group_id: The id of the ad group (ad_group.id).
        keywords: The keyword texts to add, at most 200 per call.
        match_type: The match type applied to all of the keywords.
        cpc_bid: Optional max CPC bid for these keywords in the account currency; by default they use the ad group bid.
        validate_only: If true, validates the change without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    ad_group_id = _clean_id(ad_group_id, "ad_group_id")
    keywords = _clean_texts(
        keywords, "keywords", 1, MAX_KEYWORDS_PER_CALL, MAX_KEYWORD_LENGTH
    )
    cpc_bid_micros = _to_micros(cpc_bid, "cpc_bid") if cpc_bid else None

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    row = _find_search_ad_group(client, customer_id, ad_group_id)
    operations = [
        _keyword_operation(
            client,
            row.ad_group.resource_name,
            keyword,
            match_type,
            cpc_bid_micros,
        )
        for keyword in keywords
    ]
    resource_names = _send_operations(
        client, customer_id, operations, validate_only
    )
    return _result(
        "add_keywords",
        customer_id,
        validate_only,
        [
            {
                "campaign": row.campaign.name,
                "ad_group": row.ad_group.name,
                "keywords_added": keywords,
                "match_type": match_type,
                "cpc_bid": (
                    _from_micros(cpc_bid_micros) if cpc_bid_micros else None
                ),
            }
        ],
        resource_names,
    )


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def create_responsive_search_ad(
    customer_id: str | int,
    ad_group_id: str | int,
    final_url: str,
    headlines: List[str],
    descriptions: List[str],
    path1: str | None = None,
    path2: str | None = None,
    status: Status = "ENABLED",
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Creates a responsive search ad in an ad group of a Search campaign.

    Google combines the headlines and descriptions to build each ad shown.
    By default the change is only validated, not applied. Show the returned
    changes to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer that owns the ad group.
        ad_group_id: The id of the ad group (ad_group.id).
        final_url: The landing page of the ad, starting with https://.
        headlines: 3 to 15 headlines of at most 30 characters each.
        descriptions: 2 to 4 descriptions of at most 90 characters each.
        path1: Optional first path of the display URL, at most 15 characters.
        path2: Optional second path of the display URL, at most 15 characters.
        status: The status of the new ad.
        validate_only: If true, validates the change without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    ad_group_id = _clean_id(ad_group_id, "ad_group_id")
    final_url, headlines, descriptions = _ad_texts(
        headlines, descriptions, final_url, path1, path2
    )

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    row = _find_search_ad_group(client, customer_id, ad_group_id)
    operation = _responsive_search_ad_operation(
        client,
        row.ad_group.resource_name,
        final_url,
        headlines,
        descriptions,
        path1,
        path2,
        status,
    )
    resource_names = _send_operations(
        client, customer_id, [operation], validate_only
    )
    return _result(
        "create_responsive_search_ad",
        customer_id,
        validate_only,
        [
            {
                "campaign": row.campaign.name,
                "ad_group": row.ad_group.name,
                "status": status,
                "final_url": final_url,
                "headlines": headlines,
                "descriptions": descriptions,
                "path1": path1,
                "path2": path2,
            }
        ],
        resource_names,
    )
