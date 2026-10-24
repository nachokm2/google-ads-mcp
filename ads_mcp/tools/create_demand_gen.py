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

"""Tools for creating Demand Gen campaigns with video or image ads.

The Google Ads API cannot create classic Video campaigns, so YouTube video ads
are created as Demand Gen campaigns whose ad group only serves on YouTube.
The tools are registered in the `mutate` namespace, so they are enabled
together with the other write tools.
"""

import datetime
from typing import Any, Dict, List, Literal

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient

import ads_mcp.utils as utils
from ads_mcp.tools.create import (
    DEFAULT_LANGUAGE_IDS,
    DEFAULT_LOCATION_IDS,
    Status,
    _check_final_url,
    _check_new_daily_budget,
    _clean_ids,
    _clean_texts,
    _new_campaign_operations,
    _send_operations,
    _set_bidding,
)
from ads_mcp.tools.create_display import (
    _append_assets,
    _append_texts,
    _bidding_amounts,
    _dates,
)
from ads_mcp.tools.create_pmax import (
    MAX_LOGOS,
    _AssetRequest,
    _clean_business_name,
    _image_refs,
    _resolve_images,
    _search,
    _video_ids,
)
from ads_mcp.tools.mutate import (
    _CREATE_ANNOTATIONS,
    _clean_id,
    _from_micros,
    _result,
    _to_micros,
    mutate_mcp,
)

Channel = Literal[
    "YOUTUBE_IN_STREAM",
    "YOUTUBE_IN_FEED",
    "YOUTUBE_SHORTS",
    "DISCOVER",
    "GMAIL",
    "DISPLAY",
    "MAPS",
]
DEFAULT_VIDEO_CHANNELS = [
    "YOUTUBE_IN_STREAM",
    "YOUTUBE_IN_FEED",
    "YOUTUBE_SHORTS",
]
DEFAULT_IMAGE_CHANNELS = ["YOUTUBE_IN_FEED", "DISCOVER", "GMAIL"]
DemandGenBidding = Literal[
    "MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE", "MAXIMIZE_CLICKS"
]

MAX_VIDEO_HEADLINE_LENGTH = 40
MAX_DEMAND_GEN_VIDEOS = 5
# Marketing images of every aspect ratio together.
MAX_DEMAND_GEN_IMAGES = 20

_AD_GROUP_TEMP_ID = -3


def _check_minimum_budget(
    client: GoogleAdsClient, customer_id: str, currency: str, budget_micros: int
) -> None:
    """Rejects budgets below the Demand Gen minimum for the currency."""
    rows = _search(
        client,
        customer_id,
        "SELECT currency_constant.demand_gen_info.minimum_budget_per_day_micros "
        f"FROM currency_constant WHERE currency_constant.code = '{currency}'",
    )
    minimum = (
        rows[0].currency_constant.demand_gen_info.minimum_budget_per_day_micros
        if rows
        else 0
    )
    if minimum and budget_micros < minimum:
        raise ToolError(
            "Demand Gen campaigns need a daily budget of at least "
            f"{_from_micros(minimum):g} {currency}."
        )


def _clean_channels(channels: List[str]) -> List[str]:
    channels = list(dict.fromkeys(channels))
    if not channels:
        raise ToolError("channels must contain at least one channel.")
    return channels


def _demand_gen_operations(
    client: GoogleAdsClient,
    customer_id: str,
    campaign_name: str,
    ad_group_name: str,
    budget_micros: int,
    status: str,
    start: datetime.date | None,
    end: datetime.date | None,
    location_ids: List[str],
    language_ids: List[str],
    channels: List[str],
    bidding_strategy: str,
    cpc_bid_micros: int | None,
    target_cpa_micros: int | None,
    target_roas: float | None,
):
    """Builds the budget, campaign and ad group of a Demand Gen campaign.

    Demand Gen targets locations and languages on the ad group, which also
    selects the channels where the ads appear. Returns the operations that go
    before and after the asset operations, and the ad group resource name.
    """
    prefix, campaign = _new_campaign_operations(
        client,
        customer_id,
        campaign_name,
        "DEMAND_GEN",
        budget_micros,
        status,
        start,
        end,
        [],
        [],
    )
    _set_bidding(
        campaign,
        bidding_strategy,
        cpc_bid_micros,
        target_cpa_micros,
        target_roas,
    )

    ad_group_resource_name = (
        f"customers/{customer_id}/adGroups/{_AD_GROUP_TEMP_ID}"
    )
    ad_group_operation = client.get_type("MutateOperation")
    ad_group = ad_group_operation.ad_group_operation.create
    ad_group.resource_name = ad_group_resource_name
    ad_group.name = ad_group_name
    ad_group.campaign = campaign.resource_name
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    controls = ad_group.demand_gen_ad_group_settings.channel_controls
    controls.channel_config = (
        client.enums.DemandGenChannelConfigEnum.SELECTED_CHANNELS
    )
    for channel in channels:
        setattr(controls.selected_channels, channel.lower(), True)

    suffix = [ad_group_operation]
    for location_id in location_ids:
        operation = client.get_type("MutateOperation")
        criterion = operation.ad_group_criterion_operation.create
        criterion.ad_group = ad_group_resource_name
        criterion.location.geo_target_constant = (
            f"geoTargetConstants/{location_id}"
        )
        suffix.append(operation)
    for language_id in language_ids:
        operation = client.get_type("MutateOperation")
        criterion = operation.ad_group_criterion_operation.create
        criterion.ad_group = ad_group_resource_name
        criterion.language.language_constant = (
            f"languageConstants/{language_id}"
        )
        suffix.append(operation)
    return prefix, suffix, ad_group_resource_name


def _ad_operation(
    client: GoogleAdsClient,
    ad_group_resource_name: str,
    final_url: str,
    name: str,
):
    """Returns the operation and the ad of a new ad group ad."""
    operation = client.get_type("MutateOperation")
    ad_group_ad = operation.ad_group_ad_operation.create
    ad_group_ad.ad_group = ad_group_resource_name
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad_group_ad.ad.name = name
    ad_group_ad.ad.final_urls.append(final_url)
    return operation, ad_group_ad.ad


def _warnings(status: str, bidding_strategy: str) -> List[str]:
    warnings = []
    if bidding_strategy != "MAXIMIZE_CLICKS":
        warnings.append(
            f"{bidding_strategy} bidding needs conversion tracking in the "
            "account to learn which results matter."
        )
    if status == "ENABLED":
        warnings.append(
            "The campaign is created ENABLED: it starts serving and spending "
            "as soon as its ad is approved."
        )
    return warnings


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def create_demand_gen_video_campaign(
    customer_id: str | int,
    campaign_name: str,
    daily_budget: float,
    final_url: str,
    business_name: str,
    youtube_videos: List[str],
    headlines: List[str],
    long_headlines: List[str],
    descriptions: List[str],
    logo_images: List[str],
    channels: List[Channel] = DEFAULT_VIDEO_CHANNELS,
    bidding_strategy: DemandGenBidding = "MAXIMIZE_CONVERSIONS",
    cpc_bid: float | None = None,
    target_cpa: float | None = None,
    target_roas: float | None = None,
    ad_group_name: str | None = None,
    location_ids: List[str | int] = DEFAULT_LOCATION_IDS,
    language_ids: List[str | int] = DEFAULT_LANGUAGE_IDS,
    start_date: str | None = None,
    end_date: str | None = None,
    status: Status = "ENABLED",
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Creates a campaign of YouTube video ads (a Demand Gen video campaign).

    The Google Ads API cannot create classic Video campaigns; this creates a
    Demand Gen campaign with one ad group and one video responsive ad, in one
    atomic step. By default the ads only appear on YouTube (in-stream, in-feed
    and Shorts); pass channels to also use Discover, Gmail, Display or Maps.
    The videos must already be published on YouTube.

    logo_images are square (1:1, at least 128x128). Each image is either an
    https:// URL of a PNG, JPEG or GIF of at most 5 MB, or the id of an image
    asset already in the account.

    Amounts are in the account currency, not in micros. With status ENABLED
    (the default) the campaign starts serving and spending as soon as its ad
    is approved; use PAUSED to review it first.

    By default the campaign is only validated, not created. Show the returned
    summary to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer to create the campaign in.
        campaign_name: The name of the campaign; it must be unique in the account.
        daily_budget: The average daily budget in the account currency.
        final_url: The landing page of the ad, starting with https://.
        business_name: The advertiser name shown in the ad, at most 25 characters.
        youtube_videos: 1 to 5 YouTube video ids or URLs.
        headlines: 1 to 5 headlines of at most 40 characters each.
        long_headlines: 1 to 5 long headlines of at most 90 characters each.
        descriptions: 1 to 5 descriptions of at most 90 characters each.
        logo_images: 1 to 5 square logos.
        channels: Where the ads appear; defaults to the three YouTube channels.
        bidding_strategy: MAXIMIZE_CONVERSIONS (target_cpa is optional), MAXIMIZE_CONVERSION_VALUE (target_roas is optional) or MAXIMIZE_CLICKS (cpc_bid is an optional max CPC limit).
        cpc_bid: The max CPC limit for MAXIMIZE_CLICKS in the account currency.
        target_cpa: The target cost per conversion for MAXIMIZE_CONVERSIONS.
        target_roas: The target return on ad spend, for example 3.5 for 350%.
        ad_group_name: The name of the ad group; defaults to the campaign name.
        location_ids: Geo target constant ids to target; defaults to Chile (2152).
        language_ids: Language constant ids to target; defaults to Spanish (1003).
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
    final_url = _check_final_url(final_url)
    business_name = _clean_business_name(business_name)
    video_ids = _video_ids(youtube_videos, 1, MAX_DEMAND_GEN_VIDEOS)
    headlines = _clean_texts(
        headlines, "headlines", 1, 5, MAX_VIDEO_HEADLINE_LENGTH
    )
    long_headlines = _clean_texts(long_headlines, "long_headlines", 1, 5, 90)
    descriptions = _clean_texts(descriptions, "descriptions", 1, 5, 90)
    image_refs = {"LOGO": _image_refs(logo_images, "LOGO", 1, MAX_LOGOS)}
    channels = _clean_channels(channels)
    location_ids = _clean_ids(location_ids, "location_ids")
    language_ids = _clean_ids(language_ids, "language_ids")
    start, end = _dates(start_date, end_date)
    cpc_bid_micros, target_cpa_micros = _bidding_amounts(
        bidding_strategy, cpc_bid, target_cpa, target_roas
    )

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    currency = _check_new_daily_budget(client, customer_id, budget_micros)
    _check_minimum_budget(client, customer_id, currency, budget_micros)
    request = _AssetRequest(client, customer_id)
    images, image_summary = _resolve_images(
        client, customer_id, request, campaign_name, image_refs
    )
    videos = [request.youtube_video(video_id) for video_id in video_ids]

    prefix, suffix, ad_group_resource_name = _demand_gen_operations(
        client,
        customer_id,
        campaign_name,
        ad_group_name,
        budget_micros,
        status,
        start,
        end,
        location_ids,
        language_ids,
        channels,
        bidding_strategy,
        cpc_bid_micros,
        target_cpa_micros,
        target_roas,
    )
    ad_operation, ad = _ad_operation(
        client, ad_group_resource_name, final_url, f"{campaign_name} video"
    )
    video_ad = ad.demand_gen_video_responsive_ad
    _append_texts(client, video_ad.headlines, headlines)
    _append_texts(client, video_ad.long_headlines, long_headlines)
    _append_texts(client, video_ad.descriptions, descriptions)
    _append_assets(client, video_ad.videos, "AdVideoAsset", videos)
    _append_assets(client, video_ad.logo_images, "AdImageAsset", images["LOGO"])
    video_ad.business_name.text = business_name

    resource_names = _send_operations(
        client,
        customer_id,
        prefix + request.asset_operations + suffix + [ad_operation],
        validate_only,
    )
    return _result(
        "create_demand_gen_video_campaign",
        customer_id,
        validate_only,
        [
            {
                "campaign": campaign_name,
                "status": status,
                "currency": currency,
                "daily_budget": _from_micros(budget_micros),
                "channels": channels,
                "bidding_strategy": bidding_strategy,
                "location_ids": location_ids,
                "language_ids": language_ids,
                "start_date": str(start) if start else "today",
                "end_date": str(end) if end else None,
                "ad_group": ad_group_name,
                "final_url": final_url,
                "business_name": business_name,
                "youtube_video_ids": video_ids,
                "headlines": headlines,
                "long_headlines": long_headlines,
                "descriptions": descriptions,
                "images": image_summary,
            }
        ],
        resource_names,
        _warnings(status, bidding_strategy),
    )


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def create_demand_gen_image_campaign(
    customer_id: str | int,
    campaign_name: str,
    daily_budget: float,
    final_url: str,
    business_name: str,
    headlines: List[str],
    descriptions: List[str],
    logo_images: List[str],
    marketing_images: List[str] = [],
    square_marketing_images: List[str] = [],
    portrait_marketing_images: List[str] = [],
    tall_portrait_marketing_images: List[str] = [],
    channels: List[Channel] = DEFAULT_IMAGE_CHANNELS,
    bidding_strategy: DemandGenBidding = "MAXIMIZE_CONVERSIONS",
    cpc_bid: float | None = None,
    target_cpa: float | None = None,
    target_roas: float | None = None,
    ad_group_name: str | None = None,
    location_ids: List[str | int] = DEFAULT_LOCATION_IDS,
    language_ids: List[str | int] = DEFAULT_LANGUAGE_IDS,
    start_date: str | None = None,
    end_date: str | None = None,
    status: Status = "ENABLED",
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Creates a Demand Gen campaign of image ads.

    Creates the budget, the campaign, one ad group and one multi-image ad in
    one atomic step. By default the ads appear on YouTube in-feed, Discover
    and Gmail; pass channels to choose others.

    Images and their required aspect ratio and minimum size:
    marketing_images 1.91:1 (600x314), square_marketing_images 1:1 (300x300),
    portrait_marketing_images 4:5 (480x600),
    tall_portrait_marketing_images 9:16 (600x1067) and logo_images 1:1
    (128x128). At least one landscape or square image is required, and the
    marketing images are at most 20 in total. Each image is either an https://
    URL of a PNG, JPEG or GIF of at most 5 MB, or the id of an image asset
    already in the account.

    Amounts are in the account currency, not in micros. With status ENABLED
    (the default) the campaign starts serving and spending as soon as its ad
    is approved; use PAUSED to review it first.

    By default the campaign is only validated, not created. Show the returned
    summary to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer to create the campaign in.
        campaign_name: The name of the campaign; it must be unique in the account.
        daily_budget: The average daily budget in the account currency.
        final_url: The landing page of the ad, starting with https://.
        business_name: The advertiser name shown in the ad, at most 25 characters.
        headlines: 1 to 5 headlines of at most 30 characters each.
        descriptions: 1 to 5 descriptions of at most 90 characters each.
        logo_images: 1 to 5 square logos.
        marketing_images: Landscape (1.91:1) images.
        square_marketing_images: Square (1:1) images.
        portrait_marketing_images: Portrait (4:5) images.
        tall_portrait_marketing_images: Tall portrait (9:16) images.
        channels: Where the ads appear; defaults to YouTube in-feed, Discover and Gmail.
        bidding_strategy: MAXIMIZE_CONVERSIONS (target_cpa is optional), MAXIMIZE_CONVERSION_VALUE (target_roas is optional) or MAXIMIZE_CLICKS (cpc_bid is an optional max CPC limit).
        cpc_bid: The max CPC limit for MAXIMIZE_CLICKS in the account currency.
        target_cpa: The target cost per conversion for MAXIMIZE_CONVERSIONS.
        target_roas: The target return on ad spend, for example 3.5 for 350%.
        ad_group_name: The name of the ad group; defaults to the campaign name.
        location_ids: Geo target constant ids to target; defaults to Chile (2152).
        language_ids: Language constant ids to target; defaults to Spanish (1003).
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
    final_url = _check_final_url(final_url)
    business_name = _clean_business_name(business_name)
    headlines = _clean_texts(headlines, "headlines", 1, 5, 30)
    descriptions = _clean_texts(descriptions, "descriptions", 1, 5, 90)
    image_refs = {
        field_type: _image_refs(values, field_type, 0, MAX_DEMAND_GEN_IMAGES)
        for field_type, values in (
            ("MARKETING_IMAGE", marketing_images),
            ("SQUARE_MARKETING_IMAGE", square_marketing_images),
            ("PORTRAIT_MARKETING_IMAGE", portrait_marketing_images),
            ("TALL_PORTRAIT_MARKETING_IMAGE", tall_portrait_marketing_images),
        )
    }
    if (
        not image_refs["MARKETING_IMAGE"]
        and not image_refs["SQUARE_MARKETING_IMAGE"]
    ):
        raise ToolError("At least one landscape or square image is required.")
    if sum(len(refs) for refs in image_refs.values()) > MAX_DEMAND_GEN_IMAGES:
        raise ToolError(
            f"Marketing images can be at most {MAX_DEMAND_GEN_IMAGES} in total."
        )
    image_refs["LOGO"] = _image_refs(logo_images, "LOGO", 1, MAX_LOGOS)
    channels = _clean_channels(channels)
    location_ids = _clean_ids(location_ids, "location_ids")
    language_ids = _clean_ids(language_ids, "language_ids")
    start, end = _dates(start_date, end_date)
    cpc_bid_micros, target_cpa_micros = _bidding_amounts(
        bidding_strategy, cpc_bid, target_cpa, target_roas
    )

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    currency = _check_new_daily_budget(client, customer_id, budget_micros)
    _check_minimum_budget(client, customer_id, currency, budget_micros)
    request = _AssetRequest(client, customer_id)
    images, image_summary = _resolve_images(
        client, customer_id, request, campaign_name, image_refs
    )

    prefix, suffix, ad_group_resource_name = _demand_gen_operations(
        client,
        customer_id,
        campaign_name,
        ad_group_name,
        budget_micros,
        status,
        start,
        end,
        location_ids,
        language_ids,
        channels,
        bidding_strategy,
        cpc_bid_micros,
        target_cpa_micros,
        target_roas,
    )
    ad_operation, ad = _ad_operation(
        client, ad_group_resource_name, final_url, f"{campaign_name} images"
    )
    image_ad = ad.demand_gen_multi_asset_ad
    for field_type, field in (
        ("MARKETING_IMAGE", image_ad.marketing_images),
        ("SQUARE_MARKETING_IMAGE", image_ad.square_marketing_images),
        ("PORTRAIT_MARKETING_IMAGE", image_ad.portrait_marketing_images),
        (
            "TALL_PORTRAIT_MARKETING_IMAGE",
            image_ad.tall_portrait_marketing_images,
        ),
        ("LOGO", image_ad.logo_images),
    ):
        _append_assets(client, field, "AdImageAsset", images[field_type])
    _append_texts(client, image_ad.headlines, headlines)
    _append_texts(client, image_ad.descriptions, descriptions)
    image_ad.business_name = business_name

    resource_names = _send_operations(
        client,
        customer_id,
        prefix + request.asset_operations + suffix + [ad_operation],
        validate_only,
    )
    return _result(
        "create_demand_gen_image_campaign",
        customer_id,
        validate_only,
        [
            {
                "campaign": campaign_name,
                "status": status,
                "currency": currency,
                "daily_budget": _from_micros(budget_micros),
                "channels": channels,
                "bidding_strategy": bidding_strategy,
                "location_ids": location_ids,
                "language_ids": language_ids,
                "start_date": str(start) if start else "today",
                "end_date": str(end) if end else None,
                "ad_group": ad_group_name,
                "final_url": final_url,
                "business_name": business_name,
                "headlines": headlines,
                "descriptions": descriptions,
                "images": image_summary,
            }
        ],
        resource_names,
        _warnings(status, bidding_strategy),
    )
