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

"""Tool for creating several complete Search campaigns in one call.

It is registered in the `mutate` namespace, so it is enabled together with
the other write tools.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from pydantic import BaseModel, ConfigDict, Field

import ads_mcp.utils as utils
from ads_mcp.tools.create import (
    DEFAULT_LANGUAGE_IDS,
    DEFAULT_LOCATION_IDS,
    MAX_KEYWORD_LENGTH,
    MAX_NEW_DAILY_BUDGET_ENV_VAR,
    MatchType,
    Status,
    _ad_texts,
    _check_date,
    _clean_ids,
    _clean_texts,
    _currency_code,
    _keyword_operation,
    _max_new_daily_budget,
    _new_campaign_operations,
    _responsive_search_ad_operation,
    _send_operations,
    _set_bidding,
)
from ads_mcp.tools.mutate import (
    _CREATE_ANNOTATIONS,
    _clean_id,
    _from_micros,
    _result,
    _to_micros,
    mutate_mcp,
)

MAX_CAMPAIGNS_PER_CALL = 20
MAX_AD_GROUPS_PER_CAMPAIGN = 100
MAX_KEYWORDS_PER_AD_GROUP = 200
MAX_NEGATIVE_KEYWORDS = 200
# Google allows at most 3 enabled responsive search ads per ad group.
MAX_ADS_PER_AD_GROUP = 3
# A Google Ads mutate request accepts at most 10,000 operations.
MAX_OPERATIONS = 5000
MAX_REPORTED_ERRORS = 30


class Keyword(BaseModel):
    """A keyword of an ad group."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="The keyword text, at most 80 characters.")
    match_type: MatchType = Field(
        "PHRASE", description="EXACT, PHRASE or BROAD."
    )
    cpc_bid: float | None = Field(
        None,
        description="Optional max CPC bid in the account currency; only with MANUAL_CPC bidding.",
    )


class NegativeKeyword(BaseModel):
    """A search term to exclude."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="The text to exclude, at most 80 characters.")
    match_type: MatchType = Field(
        "PHRASE", description="EXACT, PHRASE or BROAD."
    )


class SearchAd(BaseModel):
    """A responsive search ad."""

    model_config = ConfigDict(extra="forbid")

    final_url: str = Field(
        description="The landing page, starting with https://."
    )
    headlines: List[str] = Field(
        description="3 to 15 headlines of at most 30 characters each."
    )
    descriptions: List[str] = Field(
        description="2 to 4 descriptions of at most 90 characters each."
    )
    path1: str | None = Field(
        None,
        description="Optional first display URL path, at most 15 characters.",
    )
    path2: str | None = Field(
        None,
        description="Optional second display URL path, at most 15 characters.",
    )


class SearchAdGroup(BaseModel):
    """An ad group with its keywords and ads."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description="The ad group name, unique within the campaign."
    )
    keywords: List[Keyword] = Field(description="1 to 200 keywords.")
    ads: List[SearchAd] = Field(description="1 to 3 responsive search ads.")
    cpc_bid: float | None = Field(
        None,
        description="Max CPC bid of the ad group in the account currency; only with MANUAL_CPC bidding, where it defaults to the campaign cpc_bid.",
    )
    negative_keywords: List[NegativeKeyword] = Field(
        [], description="Optional search terms to exclude in this ad group."
    )


class SearchCampaign(BaseModel):
    """A complete Search campaign."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="The campaign name, unique in the account.")
    daily_budget: float = Field(
        description="The average daily budget in the account currency."
    )
    ad_groups: List[SearchAdGroup] = Field(description="1 to 100 ad groups.")
    bidding_strategy: Literal[
        "MANUAL_CPC", "MAXIMIZE_CLICKS", "MAXIMIZE_CONVERSIONS"
    ] = Field("MAXIMIZE_CLICKS", description="The bidding strategy.")
    cpc_bid: float | None = Field(
        None,
        description="With MAXIMIZE_CLICKS, an optional max CPC limit; with MANUAL_CPC, the default bid of the ad groups.",
    )
    target_cpa: float | None = Field(
        None,
        description="Optional target cost per conversion for MAXIMIZE_CONVERSIONS.",
    )
    location_ids: List[str | int] = Field(
        DEFAULT_LOCATION_IDS,
        description="Geo target constant ids; defaults to Chile (2152).",
    )
    language_ids: List[str | int] = Field(
        DEFAULT_LANGUAGE_IDS,
        description="Language constant ids; defaults to Spanish (1003).",
    )
    include_search_partners: bool = Field(
        False, description="Whether to also show ads on search partner sites."
    )
    negative_keywords: List[NegativeKeyword] = Field(
        [],
        description="Optional search terms to exclude in the whole campaign.",
    )
    start_date: str | None = Field(
        None,
        description="Optional start date as YYYY-MM-DD; defaults to today.",
    )
    end_date: str | None = Field(
        None, description="Optional end date as YYYY-MM-DD."
    )
    status: Status = Field("ENABLED", description="The status of the campaign.")


@dataclass
class _AdGroupPlan:
    name: str
    cpc_bid_micros: int | None
    keywords: List[tuple]  # (text, match type, cpc bid micros)
    negative_keywords: List[tuple]  # (text, match type)
    ads: List[tuple]  # (final_url, headlines, descriptions, path1, path2)


@dataclass
class _CampaignPlan:
    spec: SearchCampaign
    name: str
    budget_micros: int
    cpc_bid_micros: int | None
    target_cpa_micros: int | None
    location_ids: List[str]
    language_ids: List[str]
    start: Any
    end: Any
    negative_keywords: List[tuple]
    ad_groups: List[_AdGroupPlan] = field(default_factory=list)


class _Errors:
    """Collects validation errors so the caller sees all of them at once."""

    def __init__(self):
        self.messages: List[str] = []

    def check(self, where: str, function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ToolError as ex:
            self.messages.append(f"{where}: {ex}")
            return None

    def add(self, where: str, message: str) -> None:
        self.messages.append(f"{where}: {message}")


def _name(value: str, what: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ToolError(f"{what} cannot be empty.")
    return value


def _keywords(
    keywords, what: str, maximum: int, allow_bids: bool
) -> List[tuple]:
    """Validates keywords and drops duplicates of the same match type."""
    if len(keywords) > maximum:
        raise ToolError(
            f"At most {maximum} {what} are allowed, got {len(keywords)}."
        )
    result = []
    seen = set()
    for keyword in keywords:
        text = " ".join(keyword.text.split())
        if not text:
            raise ToolError(f"{what} cannot be empty.")
        if len(text) > MAX_KEYWORD_LENGTH:
            raise ToolError(
                f"{what} can have at most {MAX_KEYWORD_LENGTH} characters, "
                f"got {text!r}."
            )
        key = (text.lower(), keyword.match_type)
        if key in seen:
            continue
        seen.add(key)
        cpc_bid = getattr(keyword, "cpc_bid", None)
        if cpc_bid is not None and not allow_bids:
            raise ToolError(
                f"Keyword {text!r} has a cpc_bid, which only works with "
                "MANUAL_CPC bidding."
            )
        bid_micros = _to_micros(cpc_bid, "cpc_bid") if cpc_bid else None
        result.append((text, keyword.match_type, bid_micros))
    return result


def _plan_campaign(
    spec: SearchCampaign, errors: _Errors
) -> _CampaignPlan | None:
    where = f"Campaign {spec.name!r}"
    name = errors.check(where, _name, spec.name, "The campaign name")
    budget_micros = errors.check(
        where, _to_micros, spec.daily_budget, "daily_budget"
    )
    manual_cpc = spec.bidding_strategy == "MANUAL_CPC"
    if spec.target_cpa and spec.bidding_strategy != "MAXIMIZE_CONVERSIONS":
        errors.add(where, "target_cpa needs MAXIMIZE_CONVERSIONS bidding.")
    if spec.cpc_bid and spec.bidding_strategy == "MAXIMIZE_CONVERSIONS":
        errors.add(where, "cpc_bid does not work with MAXIMIZE_CONVERSIONS.")
    cpc_bid_micros = (
        errors.check(where, _to_micros, spec.cpc_bid, "cpc_bid")
        if spec.cpc_bid
        else None
    )
    target_cpa_micros = (
        errors.check(where, _to_micros, spec.target_cpa, "target_cpa")
        if spec.target_cpa
        else None
    )
    start = errors.check(where, _check_date, spec.start_date, "start_date")
    end = errors.check(where, _check_date, spec.end_date, "end_date")
    if start and end and end < start:
        errors.add(where, "end_date cannot be before start_date.")
    plan = _CampaignPlan(
        spec=spec,
        name=name,
        budget_micros=budget_micros,
        cpc_bid_micros=cpc_bid_micros,
        target_cpa_micros=target_cpa_micros,
        location_ids=errors.check(
            where, _clean_ids, spec.location_ids, "location_ids"
        ),
        language_ids=errors.check(
            where, _clean_ids, spec.language_ids, "language_ids"
        ),
        start=start,
        end=end,
        negative_keywords=errors.check(
            where,
            _keywords,
            spec.negative_keywords,
            "negative keywords",
            MAX_NEGATIVE_KEYWORDS,
            False,
        )
        or [],
    )

    if not 1 <= len(spec.ad_groups) <= MAX_AD_GROUPS_PER_CAMPAIGN:
        errors.add(
            where,
            f"needs between 1 and {MAX_AD_GROUPS_PER_CAMPAIGN} ad groups, got "
            f"{len(spec.ad_groups)}.",
        )
    ad_group_names = set()
    for ad_group in spec.ad_groups:
        group_where = f"{where}, ad group {ad_group.name!r}"
        group_name = errors.check(
            group_where, _name, ad_group.name, "The ad group name"
        )
        if group_name and group_name.lower() in ad_group_names:
            errors.add(group_where, "the ad group name is repeated.")
        ad_group_names.add((group_name or "").lower())
        if ad_group.cpc_bid is not None and not manual_cpc:
            errors.add(
                group_where, "cpc_bid on ad groups only works with MANUAL_CPC."
            )
        group_bid_micros = (
            errors.check(group_where, _to_micros, ad_group.cpc_bid, "cpc_bid")
            if ad_group.cpc_bid
            else cpc_bid_micros if manual_cpc else None
        )
        if manual_cpc and not group_bid_micros:
            errors.add(
                group_where,
                "MANUAL_CPC bidding needs a cpc_bid on the ad group or the "
                "campaign.",
            )
        keywords = errors.check(
            group_where,
            _keywords,
            ad_group.keywords,
            "keywords",
            MAX_KEYWORDS_PER_AD_GROUP,
            manual_cpc,
        )
        if keywords is not None and not keywords:
            errors.add(group_where, "needs at least one keyword.")
        negative_keywords = errors.check(
            group_where,
            _keywords,
            ad_group.negative_keywords,
            "negative keywords",
            MAX_NEGATIVE_KEYWORDS,
            False,
        )
        if not 1 <= len(ad_group.ads) <= MAX_ADS_PER_AD_GROUP:
            errors.add(
                group_where,
                f"needs between 1 and {MAX_ADS_PER_AD_GROUP} ads, got "
                f"{len(ad_group.ads)}.",
            )
        ads = []
        for number, ad in enumerate(ad_group.ads, 1):
            texts = errors.check(
                f"{group_where}, ad {number}",
                _ad_texts,
                ad.headlines,
                ad.descriptions,
                ad.final_url,
                ad.path1,
                ad.path2,
            )
            if texts:
                ads.append((*texts, ad.path1, ad.path2))
        plan.ad_groups.append(
            _AdGroupPlan(
                name=group_name,
                cpc_bid_micros=group_bid_micros,
                keywords=keywords or [],
                negative_keywords=negative_keywords or [],
                ads=ads,
            )
        )
    return plan


def _negative_keyword_operation(
    client: GoogleAdsClient,
    text: str,
    match_type: str,
    campaign_resource_name: str | None = None,
    ad_group_resource_name: str | None = None,
):
    operation = client.get_type("MutateOperation")
    if campaign_resource_name:
        criterion = operation.campaign_criterion_operation.create
        criterion.campaign = campaign_resource_name
    else:
        criterion = operation.ad_group_criterion_operation.create
        criterion.ad_group = ad_group_resource_name
    criterion.negative = True
    criterion.keyword.text = text
    criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]
    return operation


def _campaign_operations(
    client: GoogleAdsClient,
    customer_id: str,
    plan: _CampaignPlan,
    next_temp_id,
) -> tuple[List[Any], Dict[str, Any]]:
    """Builds every operation of one campaign and its summary."""
    spec = plan.spec
    operations, campaign = _new_campaign_operations(
        client,
        customer_id,
        plan.name,
        "SEARCH",
        plan.budget_micros,
        spec.status,
        plan.start,
        plan.end,
        plan.location_ids,
        plan.language_ids,
        budget_temp_id=next_temp_id(),
        campaign_temp_id=next_temp_id(),
    )
    campaign.network_settings.target_google_search = True
    campaign.network_settings.target_search_network = (
        spec.include_search_partners
    )
    campaign.network_settings.target_content_network = False
    campaign.network_settings.target_partner_search_network = False
    _set_bidding(
        campaign,
        spec.bidding_strategy,
        (
            plan.cpc_bid_micros
            if spec.bidding_strategy == "MAXIMIZE_CLICKS"
            else None
        ),
        plan.target_cpa_micros,
    )
    for text, match_type, _ in plan.negative_keywords:
        operations.append(
            _negative_keyword_operation(
                client,
                text,
                match_type,
                campaign_resource_name=campaign.resource_name,
            )
        )

    ad_group_summaries = []
    for ad_group_plan in plan.ad_groups:
        ad_group_resource_name = (
            f"customers/{customer_id}/adGroups/{next_temp_id()}"
        )
        ad_group_operation = client.get_type("MutateOperation")
        ad_group = ad_group_operation.ad_group_operation.create
        ad_group.resource_name = ad_group_resource_name
        ad_group.name = ad_group_plan.name
        ad_group.campaign = campaign.resource_name
        ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
        ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
        if ad_group_plan.cpc_bid_micros:
            ad_group.cpc_bid_micros = ad_group_plan.cpc_bid_micros
        operations.append(ad_group_operation)
        for text, match_type, bid_micros in ad_group_plan.keywords:
            operations.append(
                _keyword_operation(
                    client, ad_group_resource_name, text, match_type, bid_micros
                )
            )
        for text, match_type, _ in ad_group_plan.negative_keywords:
            operations.append(
                _negative_keyword_operation(
                    client,
                    text,
                    match_type,
                    ad_group_resource_name=ad_group_resource_name,
                )
            )
        for (
            final_url,
            headlines,
            descriptions,
            path1,
            path2,
        ) in ad_group_plan.ads:
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
        ad_group_summaries.append(
            {
                "ad_group": ad_group_plan.name,
                "cpc_bid": (
                    _from_micros(ad_group_plan.cpc_bid_micros)
                    if ad_group_plan.cpc_bid_micros
                    else None
                ),
                "keywords": [
                    f"{text} [{match_type}]"
                    for text, match_type, _ in ad_group_plan.keywords
                ],
                "negative_keywords": [
                    f"{text} [{match_type}]"
                    for text, match_type, _ in ad_group_plan.negative_keywords
                ],
                "ads": [
                    {
                        "final_url": ad[0],
                        "headlines": ad[1],
                        "descriptions": ad[2],
                    }
                    for ad in ad_group_plan.ads
                ],
            }
        )

    summary = {
        "campaign": plan.name,
        "status": spec.status,
        "daily_budget": _from_micros(plan.budget_micros),
        "bidding_strategy": spec.bidding_strategy,
        "location_ids": plan.location_ids,
        "language_ids": plan.language_ids,
        "include_search_partners": spec.include_search_partners,
        "start_date": str(plan.start) if plan.start else "today",
        "end_date": str(plan.end) if plan.end else None,
        "negative_keywords": [
            f"{text} [{match_type}]"
            for text, match_type, _ in plan.negative_keywords
        ],
        "ad_groups": ad_group_summaries,
    }
    return operations, summary


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def create_search_campaigns(
    customer_id: str | int,
    campaigns: List[SearchCampaign],
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Creates several complete Search campaigns in one atomic step.

    Each campaign has its budget, bidding, location and language targeting,
    optional campaign negative keywords and one or more ad groups. Each ad
    group has its keywords (each with its own match type), 1 to 3 responsive
    search ads and optional negative keywords. Everything is created in a
    single request: if any part fails, nothing is created, and the error
    names the campaign or ad group to fix. Validation errors in the input are
    all reported at once.

    Use it to load a whole account structure, for example one campaign per
    study program with one ad group per theme. For a single campaign with one
    ad group, create_search_campaign is simpler.

    Amounts are in the account currency, not in micros. Campaigns are ENABLED
    unless their status is PAUSED, and start spending once their ads are
    approved.

    By default the campaigns are only validated, not created. Show the
    returned summary to the user and call again with validate_only=false only
    after they confirm.

    Args:
        customer_id: The id of the customer to create the campaigns in.
        campaigns: 1 to 20 campaigns to create.
        validate_only: If true, validates the campaigns without creating them.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    if not 1 <= len(campaigns) <= MAX_CAMPAIGNS_PER_CALL:
        raise ToolError(
            f"campaigns needs between 1 and {MAX_CAMPAIGNS_PER_CALL} "
            f"campaigns, got {len(campaigns)}."
        )
    errors = _Errors()
    plans = [_plan_campaign(spec, errors) for spec in campaigns]
    campaign_names = [plan.name.lower() for plan in plans if plan.name]
    for name in {n for n in campaign_names if campaign_names.count(n) > 1}:
        errors.add(f"Campaign {name!r}", "the campaign name is repeated.")
    if errors.messages:
        shown = errors.messages[:MAX_REPORTED_ERRORS]
        more = len(errors.messages) - len(shown)
        raise ToolError(
            f"Found {len(errors.messages)} problems, nothing was sent to "
            "Google Ads:\n- "
            + "\n- ".join(shown)
            + (f"\n- ...and {more} more." if more else "")
        )

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    currency = _currency_code(client, customer_id)
    max_budget = _max_new_daily_budget(currency)
    if max_budget is not None:
        over = [
            plan.name
            for plan in plans
            if _from_micros(plan.budget_micros) > max_budget
        ]
        if over:
            raise ToolError(
                f"These campaigns have a daily budget above the "
                f"{max_budget:g} {currency} this server allows for new "
                f"campaigns: {over}. Use smaller budgets, or ask the server "
                f"administrator to raise {MAX_NEW_DAILY_BUDGET_ENV_VAR}."
            )

    temp_ids = iter(range(-1, -1_000_000, -1))
    operations = []
    summaries = []
    for plan in plans:
        campaign_operations, summary = _campaign_operations(
            client, customer_id, plan, lambda: next(temp_ids)
        )
        operations.extend(campaign_operations)
        summary["currency"] = currency
        summaries.append(summary)
    if len(operations) > MAX_OPERATIONS:
        raise ToolError(
            f"This load needs {len(operations)} operations, more than the "
            f"{MAX_OPERATIONS} allowed in one call. Split it into several "
            "calls."
        )

    resource_names = _send_operations(
        client, customer_id, operations, validate_only
    )
    warnings = []
    enabled = [plan.name for plan in plans if plan.spec.status == "ENABLED"]
    if enabled:
        warnings.append(
            f"These campaigns are created ENABLED and start serving and "
            f"spending as soon as their ads are approved: {enabled}."
        )
    result = _result(
        "create_search_campaigns",
        customer_id,
        validate_only,
        summaries,
        # The ids of the campaigns and ad groups are enough to find the rest.
        [
            name
            for name in resource_names
            if "/campaigns/" in name or "/adGroups/" in name
        ],
        warnings,
    )
    result["totals"] = {
        "campaigns": len(plans),
        "ad_groups": sum(len(plan.ad_groups) for plan in plans),
        "keywords": sum(
            len(group.keywords) for plan in plans for group in plan.ad_groups
        ),
        "ads": sum(
            len(group.ads) for plan in plans for group in plan.ad_groups
        ),
        "operations": len(operations),
    }
    return result
