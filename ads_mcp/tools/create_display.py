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

"""Tools for creating Display campaigns with responsive display ads.

They are registered in the `mutate` namespace, so they are enabled together
with the other write tools.
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
    _check_date,
    _check_final_url,
    _check_new_daily_budget,
    _clean_ids,
    _clean_texts,
    _new_campaign_operations,
    _send_operations,
    _set_bidding,
)
from ads_mcp.tools.create_pmax import (
    MAX_LOGOS,
    _AssetRequest,
    _clean_business_name,
    _image_refs,
    _resolve_images,
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

# Landscape and square marketing images together.
MAX_DISPLAY_IMAGES = 15
MAX_DISPLAY_VIDEOS = 5

_AD_GROUP_TEMP_ID = -3


def _append_texts(client: GoogleAdsClient, field, texts: List[str]) -> None:
    for text in texts:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        field.append(asset)


def _append_assets(
    client: GoogleAdsClient, field, type_name: str, resource_names: List[str]
) -> None:
    """Appends AdImageAsset or AdVideoAsset references to an ad field."""
    for resource_name in resource_names:
        item = client.get_type(type_name)
        item.asset = resource_name
        field.append(item)


def _dates(
    start_date: str | None, end_date: str | None
) -> tuple[datetime.date | None, datetime.date | None]:
    start = _check_date(start_date, "start_date")
    end = _check_date(end_date, "end_date")
    if start and end and end < start:
        raise ToolError("end_date cannot be before start_date.")
    return start, end


def _bidding_amounts(
    bidding_strategy: str,
    cpc_bid: float | None,
    target_cpa: float | None,
    target_roas: float | None = None,
) -> tuple[int | None, int | None]:
    """Checks that the bidding options fit the strategy and converts them."""
    if bidding_strategy == "MANUAL_CPC" and cpc_bid is None:
        raise ToolError("MANUAL_CPC bidding needs cpc_bid.")
    if cpc_bid and bidding_strategy not in ("MANUAL_CPC", "MAXIMIZE_CLICKS"):
        raise ToolError(
            "cpc_bid only works with MANUAL_CPC or MAXIMIZE_CLICKS bidding."
        )
    if target_cpa and bidding_strategy != "MAXIMIZE_CONVERSIONS":
        raise ToolError("target_cpa needs MAXIMIZE_CONVERSIONS bidding.")
    if target_roas is not None:
        if bidding_strategy != "MAXIMIZE_CONVERSION_VALUE":
            raise ToolError(
                "target_roas needs MAXIMIZE_CONVERSION_VALUE bidding."
            )
        if target_roas <= 0:
            raise ToolError("target_roas must be greater than zero.")
    cpc_bid_micros = _to_micros(cpc_bid, "cpc_bid") if cpc_bid else None
    target_cpa_micros = (
        _to_micros(target_cpa, "target_cpa") if target_cpa else None
    )
    return cpc_bid_micros, target_cpa_micros


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def create_display_campaign(
    customer_id: str | int,
    campaign_name: str,
    daily_budget: float,
    final_url: str,
    business_name: str,
    headlines: List[str],
    long_headline: str,
    descriptions: List[str],
    marketing_images: List[str],
    square_marketing_images: List[str],
    square_logo_images: List[str] = [],
    landscape_logo_images: List[str] = [],
    youtube_videos: List[str] = [],
    bidding_strategy: Literal[
        "MANUAL_CPC", "MAXIMIZE_CLICKS", "MAXIMIZE_CONVERSIONS"
    ] = "MAXIMIZE_CLICKS",
    cpc_bid: float | None = None,
    target_cpa: float | None = None,
    ad_group_name: str | None = None,
    location_ids: List[str | int] = DEFAULT_LOCATION_IDS,
    language_ids: List[str | int] = DEFAULT_LANGUAGE_IDS,
    start_date: str | None = None,
    end_date: str | None = None,
    status: Status = "ENABLED",
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Creates a complete Display campaign with a responsive display ad.

    Creates the daily budget, the campaign (Google Display Network only) with
    its location and language targeting, one ad group and one responsive
    display ad in one atomic step. If any part fails, nothing is created.
    Google builds the ad in many sizes and formats from the images and texts.

    Images and their required aspect ratio and minimum size:
    marketing_images 1.91:1 (600x314), square_marketing_images 1:1 (300x300),
    square_logo_images 1:1 (128x128) and landscape_logo_images 4:1 (512x128).
    Each image is either an https:// URL of a PNG, JPEG or GIF of at most
    5 MB, or the id of an image asset already in the account (find them with
    the search tool on the asset resource, filtering by asset.type = 'IMAGE').

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
        headlines: 1 to 5 short headlines of at most 30 characters each.
        long_headline: One long headline of at most 90 characters.
        descriptions: 1 to 5 descriptions of at most 90 characters each.
        marketing_images: Landscape (1.91:1) images; with the square images, 2 to 15 in total.
        square_marketing_images: Square images; at least 1.
        square_logo_images: Optional square logos; logos are at most 5 in total.
        landscape_logo_images: Optional landscape (4:1) logos.
        youtube_videos: Up to 5 optional YouTube video ids or URLs.
        bidding_strategy: MANUAL_CPC (needs cpc_bid), MAXIMIZE_CLICKS (cpc_bid is an optional max CPC limit) or MAXIMIZE_CONVERSIONS (target_cpa is optional).
        cpc_bid: The max CPC bid in the account currency.
        target_cpa: The target cost per conversion for MAXIMIZE_CONVERSIONS.
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
    (long_headline,) = _clean_texts([long_headline], "long_headline", 1, 1, 90)
    descriptions = _clean_texts(descriptions, "descriptions", 1, 5, 90)
    image_refs = {
        "MARKETING_IMAGE": _image_refs(
            marketing_images, "MARKETING_IMAGE", 1, MAX_DISPLAY_IMAGES
        ),
        "SQUARE_MARKETING_IMAGE": _image_refs(
            square_marketing_images,
            "SQUARE_MARKETING_IMAGE",
            1,
            MAX_DISPLAY_IMAGES,
        ),
    }
    if sum(len(refs) for refs in image_refs.values()) > MAX_DISPLAY_IMAGES:
        raise ToolError(
            f"Landscape and square images can be at most {MAX_DISPLAY_IMAGES} "
            "in total."
        )
    image_refs["LOGO"] = _image_refs(square_logo_images, "LOGO", 0, MAX_LOGOS)
    image_refs["LANDSCAPE_LOGO"] = _image_refs(
        landscape_logo_images,
        "LANDSCAPE_LOGO",
        0,
        MAX_LOGOS - len(image_refs["LOGO"]),
    )
    video_ids = _video_ids(youtube_videos, 0, MAX_DISPLAY_VIDEOS)
    location_ids = _clean_ids(location_ids, "location_ids")
    language_ids = _clean_ids(language_ids, "language_ids")
    start, end = _dates(start_date, end_date)
    cpc_bid_micros, target_cpa_micros = _bidding_amounts(
        bidding_strategy, cpc_bid, target_cpa
    )

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    currency = _check_new_daily_budget(client, customer_id, budget_micros)
    request = _AssetRequest(client, customer_id)
    images, image_summary = _resolve_images(
        client, customer_id, request, campaign_name, image_refs
    )
    videos = [request.youtube_video(video_id) for video_id in video_ids]

    prefix, campaign = _new_campaign_operations(
        client,
        customer_id,
        campaign_name,
        "DISPLAY",
        budget_micros,
        status,
        start,
        end,
        location_ids,
        language_ids,
    )
    campaign.network_settings.target_google_search = False
    campaign.network_settings.target_search_network = False
    campaign.network_settings.target_content_network = True
    campaign.network_settings.target_partner_search_network = False
    _set_bidding(
        campaign,
        bidding_strategy,
        cpc_bid_micros if bidding_strategy == "MAXIMIZE_CLICKS" else None,
        target_cpa_micros,
    )

    ad_group_resource_name = (
        f"customers/{customer_id}/adGroups/{_AD_GROUP_TEMP_ID}"
    )
    ad_group_operation = client.get_type("MutateOperation")
    ad_group = ad_group_operation.ad_group_operation.create
    ad_group.resource_name = ad_group_resource_name
    ad_group.name = ad_group_name
    ad_group.campaign = campaign.resource_name
    ad_group.type_ = client.enums.AdGroupTypeEnum.DISPLAY_STANDARD
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    if bidding_strategy == "MANUAL_CPC":
        ad_group.cpc_bid_micros = cpc_bid_micros

    ad_operation = client.get_type("MutateOperation")
    ad_group_ad = ad_operation.ad_group_ad_operation.create
    ad_group_ad.ad_group = ad_group_resource_name
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad_group_ad.ad.final_urls.append(final_url)
    display_ad = ad_group_ad.ad.responsive_display_ad
    _append_assets(
        client,
        display_ad.marketing_images,
        "AdImageAsset",
        images["MARKETING_IMAGE"],
    )
    _append_assets(
        client,
        display_ad.square_marketing_images,
        "AdImageAsset",
        images["SQUARE_MARKETING_IMAGE"],
    )
    # In responsive display ads, logo_images are the 4:1 logos.
    _append_assets(
        client, display_ad.square_logo_images, "AdImageAsset", images["LOGO"]
    )
    _append_assets(
        client,
        display_ad.logo_images,
        "AdImageAsset",
        images["LANDSCAPE_LOGO"],
    )
    _append_assets(client, display_ad.youtube_videos, "AdVideoAsset", videos)
    _append_texts(client, display_ad.headlines, headlines)
    display_ad.long_headline.text = long_headline
    _append_texts(client, display_ad.descriptions, descriptions)
    display_ad.business_name = business_name

    resource_names = _send_operations(
        client,
        customer_id,
        prefix + request.asset_operations + [ad_group_operation, ad_operation],
        validate_only,
    )
    warnings = []
    if status == "ENABLED":
        warnings.append(
            "The campaign is created ENABLED: it starts serving and spending "
            "as soon as its ad is approved."
        )
    return _result(
        "create_display_campaign",
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
                "start_date": str(start) if start else "today",
                "end_date": str(end) if end else None,
                "ad_group": ad_group_name,
                "final_url": final_url,
                "business_name": business_name,
                "headlines": headlines,
                "long_headline": long_headline,
                "descriptions": descriptions,
                "images": image_summary,
                "youtube_video_ids": video_ids,
            }
        ],
        resource_names,
        warnings,
    )
