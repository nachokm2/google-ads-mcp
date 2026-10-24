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

"""Tools for creating Performance Max campaigns and asset groups.

They are registered in the `mutate` namespace, so they are enabled together
with the other write tools. Images can be https:// URLs, which are downloaded
and uploaded as new assets, or the ids of image assets already in the account.
"""

import hashlib
from typing import Any, Dict, List, Literal

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

import ads_mcp.utils as utils
from ads_mcp.assets import (
    AssetError,
    check_image_size,
    download_image,
    image_size,
    youtube_video_id,
)
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
)
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

MAX_BUSINESS_NAME_LENGTH = 25
MAX_YOUTUBE_VIDEOS = 15
# LOGO and LANDSCAPE_LOGO images together.
MAX_LOGOS = 5

# Field type -> (minimum count, maximum count, maximum length).
TEXT_LIMITS = {
    "HEADLINE": (3, 15, 30),
    "LONG_HEADLINE": (1, 5, 90),
    "DESCRIPTION": (2, 5, 90),
}
# Field type -> (minimum count, maximum count).
IMAGE_LIMITS = {
    "MARKETING_IMAGE": (1, 20),
    "SQUARE_MARKETING_IMAGE": (1, 20),
    "PORTRAIT_MARKETING_IMAGE": (0, 20),
}

_ASSET_GROUP_TEMP_ID = -3
_FIRST_ASSET_TEMP_ID = -100


class _AssetRequest:
    """Collects the operations of one atomic request that creates assets.

    Identical texts, images and videos are created once and linked as many
    times as needed.
    """

    def __init__(self, client: GoogleAdsClient, customer_id: str):
        self.client = client
        self.customer_id = customer_id
        self.asset_operations: List[Any] = []
        self.campaign_links: List[Any] = []
        self.asset_group_links: List[Any] = []
        self._next_temp_id = _FIRST_ASSET_TEMP_ID
        self._created: Dict[Any, str] = {}

    def _new_asset(self, key):
        """Returns the asset to fill (None if it already exists) and its name."""
        if key in self._created:
            return None, self._created[key]
        resource_name = (
            f"customers/{self.customer_id}/assets/{self._next_temp_id}"
        )
        self._next_temp_id -= 1
        operation = self.client.get_type("MutateOperation")
        asset = operation.asset_operation.create
        asset.resource_name = resource_name
        self.asset_operations.append(operation)
        self._created[key] = resource_name
        return asset, resource_name

    def text(self, text: str) -> str:
        asset, resource_name = self._new_asset(("text", text))
        if asset is not None:
            asset.text_asset.text = text
        return resource_name

    def image(self, data: bytes, name: str) -> str:
        key = ("image", hashlib.sha256(data).hexdigest())
        asset, resource_name = self._new_asset(key)
        if asset is not None:
            asset.name = name
            asset.image_asset.data = data
        return resource_name

    def youtube_video(self, video_id: str) -> str:
        asset, resource_name = self._new_asset(("video", video_id))
        if asset is not None:
            asset.youtube_video_asset.youtube_video_id = video_id
        return resource_name

    def link_to_campaign(
        self, campaign_resource_name: str, asset: str, field_type: str
    ) -> None:
        operation = self.client.get_type("MutateOperation")
        link = operation.campaign_asset_operation.create
        link.campaign = campaign_resource_name
        link.asset = asset
        link.field_type = self.client.enums.AssetFieldTypeEnum[field_type]
        self.campaign_links.append(operation)

    def link_to_asset_group(
        self, asset_group_resource_name: str, asset: str, field_type: str
    ) -> None:
        operation = self.client.get_type("MutateOperation")
        link = operation.asset_group_asset_operation.create
        link.asset_group = asset_group_resource_name
        link.asset = asset
        link.field_type = self.client.enums.AssetFieldTypeEnum[field_type]
        self.asset_group_links.append(operation)


def _search(client: GoogleAdsClient, customer_id: str, query: str) -> List[Any]:
    ga_service = _get_service(client, "GoogleAdsService")
    try:
        return list(ga_service.search(customer_id=customer_id, query=query))
    except GoogleAdsException as ex:
        _raise_tool_error(ex)


def _image_refs(values: List[str], field_type: str, minimum: int, maximum: int):
    refs = list(
        dict.fromkeys(value.strip() for value in values if value.strip())
    )
    if not minimum <= len(refs) <= maximum:
        raise ToolError(
            f"{field_type} needs between {minimum} and {maximum} images, got "
            f"{len(refs)}."
        )
    return refs


def _is_url(ref: str) -> bool:
    return ref.lower().startswith(("https://", "http://"))


def _resolve_images(
    client: GoogleAdsClient,
    customer_id: str,
    request: _AssetRequest,
    name_prefix: str,
    refs_by_field: Dict[str, List[str]],
):
    """Downloads or looks up every image and checks its size for its use.

    Returns the asset resource names per field type and a summary per image.
    """
    asset_ids = {
        ref: _clean_id(ref.rsplit("/", 1)[-1], "image asset id")
        for refs in refs_by_field.values()
        for ref in refs
        if not _is_url(ref)
    }
    existing = {}
    if asset_ids:
        rows = _search(
            client,
            customer_id,
            "SELECT asset.id, asset.resource_name, asset.type, "
            "asset.image_asset.full_size.width_pixels, "
            "asset.image_asset.full_size.height_pixels FROM asset "
            f"WHERE asset.id IN ({', '.join(sorted(set(asset_ids.values())))})",
        )
        existing = {str(row.asset.id): row.asset for row in rows}

    assets: Dict[str, List[str]] = {}
    summary = []
    for field_type, refs in refs_by_field.items():
        assets[field_type] = []
        for index, ref in enumerate(refs, 1):
            try:
                if _is_url(ref):
                    data = download_image(ref)
                    width, height = image_size(data)
                    check_image_size(width, height, field_type)
                    resource_name = request.image(
                        data, f"{name_prefix} {field_type.lower()} {index}"
                    )
                else:
                    asset = existing.get(asset_ids[ref])
                    if asset is None or asset.type_.name != "IMAGE":
                        raise AssetError(
                            "it is not an image asset of this account."
                        )
                    width = asset.image_asset.full_size.width_pixels
                    height = asset.image_asset.full_size.height_pixels
                    check_image_size(width, height, field_type)
                    resource_name = asset.resource_name
            except AssetError as ex:
                raise ToolError(f"{field_type} image {ref!r}: {ex}")
            assets[field_type].append(resource_name)
            summary.append(
                {
                    "field": field_type,
                    "source": ref,
                    "size": f"{width}x{height}",
                }
            )
    return assets, summary


def _texts(headlines, long_headlines, descriptions) -> Dict[str, List[str]]:
    values = {
        "HEADLINE": headlines,
        "LONG_HEADLINE": long_headlines,
        "DESCRIPTION": descriptions,
    }
    return {
        field_type: _clean_texts(
            values[field_type],
            field_type.lower() + "s",
            *TEXT_LIMITS[field_type],
        )
        for field_type in TEXT_LIMITS
    }


def _clean_business_name(business_name: str) -> str:
    business_name = " ".join(business_name.split())
    if not 1 <= len(business_name) <= MAX_BUSINESS_NAME_LENGTH:
        raise ToolError(
            f"business_name must have between 1 and {MAX_BUSINESS_NAME_LENGTH} "
            f"characters, got {business_name!r}."
        )
    return business_name


def _logo_refs(logo_images: List[str], landscape_logo_images: List[str]):
    logos = _image_refs(logo_images, "LOGO", 1, MAX_LOGOS)
    landscape_logos = _image_refs(
        landscape_logo_images, "LANDSCAPE_LOGO", 0, MAX_LOGOS - len(logos)
    )
    return {"LOGO": logos, "LANDSCAPE_LOGO": landscape_logos}


def _video_ids(youtube_videos: List[str]) -> List[str]:
    try:
        video_ids = list(
            dict.fromkeys(youtube_video_id(v) for v in youtube_videos)
        )
    except AssetError as ex:
        raise ToolError(str(ex))
    if len(video_ids) > MAX_YOUTUBE_VIDEOS:
        raise ToolError(
            f"At most {MAX_YOUTUBE_VIDEOS} YouTube videos are allowed, got "
            f"{len(video_ids)}."
        )
    return video_ids


def _asset_group_operation(
    client: GoogleAdsClient,
    request: _AssetRequest,
    customer_id: str,
    campaign_resource_name: str,
    name: str,
    final_url: str,
    status: str,
    texts: Dict[str, List[str]],
    images: Dict[str, List[str]],
    video_ids: List[str],
):
    """Builds the asset group and queues the links to its assets."""
    asset_group_resource_name = (
        f"customers/{customer_id}/assetGroups/{_ASSET_GROUP_TEMP_ID}"
    )
    operation = client.get_type("MutateOperation")
    asset_group = operation.asset_group_operation.create
    asset_group.resource_name = asset_group_resource_name
    asset_group.name = name
    asset_group.campaign = campaign_resource_name
    asset_group.final_urls.append(final_url)
    asset_group.status = client.enums.AssetGroupStatusEnum[status]
    for field_type, values in texts.items():
        for value in values:
            request.link_to_asset_group(
                asset_group_resource_name, request.text(value), field_type
            )
    for field_type, resource_names in images.items():
        for resource_name in resource_names:
            request.link_to_asset_group(
                asset_group_resource_name, resource_name, field_type
            )
    for video_id in video_ids:
        request.link_to_asset_group(
            asset_group_resource_name,
            request.youtube_video(video_id),
            "YOUTUBE_VIDEO",
        )
    return operation


def _ordered(prefix, request: _AssetRequest, asset_group_operation):
    """Orders the operations so that resources exist before being linked."""
    return (
        list(prefix)
        + request.asset_operations
        + request.campaign_links
        + [asset_group_operation]
        + request.asset_group_links
    )


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def create_performance_max_campaign(
    customer_id: str | int,
    campaign_name: str,
    daily_budget: float,
    final_url: str,
    business_name: str,
    headlines: List[str],
    long_headlines: List[str],
    descriptions: List[str],
    logo_images: List[str],
    marketing_images: List[str],
    square_marketing_images: List[str],
    portrait_marketing_images: List[str] = [],
    landscape_logo_images: List[str] = [],
    youtube_videos: List[str] = [],
    bidding_strategy: Literal[
        "MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"
    ] = "MAXIMIZE_CONVERSIONS",
    target_cpa: float | None = None,
    target_roas: float | None = None,
    asset_group_name: str | None = None,
    location_ids: List[str | int] = DEFAULT_LOCATION_IDS,
    language_ids: List[str | int] = DEFAULT_LANGUAGE_IDS,
    start_date: str | None = None,
    end_date: str | None = None,
    status: Status = "ENABLED",
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Creates a complete Performance Max campaign in one atomic step.

    Creates the daily budget, the campaign with its location and language
    targeting, the brand assets (business name and logos, linked to the
    campaign as brand guidelines) and one asset group with its texts, images
    and videos. If any part fails, nothing is created. Performance Max shows
    ads across Search, YouTube, Display, Discover, Gmail and Maps, and Google
    combines the assets automatically.

    Images and their required aspect ratio and minimum size:
    marketing_images 1.91:1 (600x314), square_marketing_images 1:1 (300x300),
    portrait_marketing_images 4:5 (480x600), logo_images 1:1 (128x128) and
    landscape_logo_images 4:1 (512x128). Each image is either an https:// URL
    of a PNG, JPEG or GIF of at most 5 MB, or the id of an image asset already
    in the account (find them with the search tool on the asset resource,
    filtering by asset.type = 'IMAGE').

    Amounts are in the account currency, not in micros. Performance Max bids
    for conversions, so the account should track conversions. With status
    ENABLED (the default) the campaign starts serving and spending once its
    assets are approved; use PAUSED to review it first.

    By default the campaign is only validated, not created. Show the returned
    summary to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer to create the campaign in.
        campaign_name: The name of the campaign; it must be unique in the account.
        daily_budget: The average daily budget in the account currency.
        final_url: The landing page, starting with https://.
        business_name: The advertiser name shown in ads, at most 25 characters.
        headlines: 3 to 15 headlines of at most 30 characters each.
        long_headlines: 1 to 5 long headlines of at most 90 characters each.
        descriptions: 2 to 5 descriptions of at most 90 characters each.
        logo_images: 1 to 5 square logos.
        marketing_images: 1 to 20 landscape (1.91:1) images.
        square_marketing_images: 1 to 20 square images.
        portrait_marketing_images: Up to 20 optional portrait (4:5) images.
        landscape_logo_images: Optional landscape (4:1) logos; logos and landscape logos are at most 5 in total.
        youtube_videos: Up to 15 optional YouTube video ids or URLs; without videos Google may generate them from the images.
        bidding_strategy: MAXIMIZE_CONVERSIONS (target_cpa is optional) or MAXIMIZE_CONVERSION_VALUE (target_roas is optional).
        target_cpa: The target cost per conversion in the account currency.
        target_roas: The target return on ad spend, for example 3.5 for 350%.
        asset_group_name: The name of the asset group; defaults to the campaign name.
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
    asset_group_name = " ".join((asset_group_name or campaign_name).split())
    budget_micros = _to_micros(daily_budget, "daily_budget")
    final_url = _check_final_url(final_url)
    business_name = _clean_business_name(business_name)
    texts = _texts(headlines, long_headlines, descriptions)
    logo_refs = _logo_refs(logo_images, landscape_logo_images)
    image_refs = {
        field_type: _image_refs(values, field_type, *IMAGE_LIMITS[field_type])
        for field_type, values in (
            ("MARKETING_IMAGE", marketing_images),
            ("SQUARE_MARKETING_IMAGE", square_marketing_images),
            ("PORTRAIT_MARKETING_IMAGE", portrait_marketing_images),
        )
    }
    video_ids = _video_ids(youtube_videos)
    location_ids = _clean_ids(location_ids, "location_ids")
    language_ids = _clean_ids(language_ids, "language_ids")
    start = _check_date(start_date, "start_date")
    end = _check_date(end_date, "end_date")
    if start and end and end < start:
        raise ToolError("end_date cannot be before start_date.")
    if bidding_strategy == "MAXIMIZE_CONVERSIONS" and target_roas:
        raise ToolError("target_roas needs MAXIMIZE_CONVERSION_VALUE bidding.")
    if bidding_strategy == "MAXIMIZE_CONVERSION_VALUE" and target_cpa:
        raise ToolError("target_cpa needs MAXIMIZE_CONVERSIONS bidding.")
    if target_roas is not None and target_roas <= 0:
        raise ToolError("target_roas must be greater than zero.")
    target_cpa_micros = (
        _to_micros(target_cpa, "target_cpa") if target_cpa else None
    )

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    currency = _check_new_daily_budget(client, customer_id, budget_micros)
    request = _AssetRequest(client, customer_id)
    images, image_summary = _resolve_images(
        client,
        customer_id,
        request,
        campaign_name,
        {**logo_refs, **image_refs},
    )

    prefix, campaign = _new_campaign_operations(
        client,
        customer_id,
        campaign_name,
        "PERFORMANCE_MAX",
        budget_micros,
        status,
        start,
        end,
        location_ids,
        language_ids,
    )
    campaign.brand_guidelines_enabled = True
    campaign_pb = type(campaign).pb(campaign)
    if bidding_strategy == "MAXIMIZE_CONVERSIONS":
        campaign_pb.maximize_conversions.SetInParent()
        if target_cpa_micros:
            campaign.maximize_conversions.target_cpa_micros = target_cpa_micros
    else:
        campaign_pb.maximize_conversion_value.SetInParent()
        if target_roas:
            campaign.maximize_conversion_value.target_roas = target_roas

    # With brand guidelines, the business name and logos belong to the
    # campaign instead of the asset group.
    request.link_to_campaign(
        campaign.resource_name, request.text(business_name), "BUSINESS_NAME"
    )
    for field_type in ("LOGO", "LANDSCAPE_LOGO"):
        for resource_name in images.pop(field_type):
            request.link_to_campaign(
                campaign.resource_name, resource_name, field_type
            )
    asset_group_operation = _asset_group_operation(
        client,
        request,
        customer_id,
        campaign.resource_name,
        asset_group_name,
        final_url,
        "ENABLED",
        texts,
        images,
        video_ids,
    )

    resource_names = _send_operations(
        client,
        customer_id,
        _ordered(prefix, request, asset_group_operation),
        validate_only,
    )
    warnings = [
        "Performance Max bids for conversions: check that the account "
        "tracks conversions, or the campaign cannot learn which results "
        "matter."
    ]
    if status == "ENABLED":
        warnings.append(
            "The campaign is created ENABLED: it starts serving and spending "
            "as soon as its assets are approved."
        )
    return _result(
        "create_performance_max_campaign",
        customer_id,
        validate_only,
        [
            {
                "campaign": campaign_name,
                "status": status,
                "currency": currency,
                "daily_budget": _from_micros(budget_micros),
                "bidding_strategy": bidding_strategy,
                "target_cpa": (
                    _from_micros(target_cpa_micros)
                    if target_cpa_micros
                    else None
                ),
                "target_roas": target_roas,
                "location_ids": location_ids,
                "language_ids": language_ids,
                "start_date": str(start) if start else "today",
                "end_date": str(end) if end else None,
                "business_name": business_name,
                "asset_group": asset_group_name,
                "final_url": final_url,
                "headlines": texts["HEADLINE"],
                "long_headlines": texts["LONG_HEADLINE"],
                "descriptions": texts["DESCRIPTION"],
                "images": image_summary,
                "youtube_video_ids": video_ids,
            }
        ],
        resource_names,
        warnings,
    )


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def create_asset_group(
    customer_id: str | int,
    campaign_id: str | int,
    asset_group_name: str,
    final_url: str,
    headlines: List[str],
    long_headlines: List[str],
    descriptions: List[str],
    marketing_images: List[str],
    square_marketing_images: List[str],
    portrait_marketing_images: List[str] = [],
    youtube_videos: List[str] = [],
    business_name: str | None = None,
    logo_images: List[str] = [],
    landscape_logo_images: List[str] = [],
    status: Status = "ENABLED",
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Creates an asset group in an existing Performance Max campaign.

    An asset group is a set of texts, images and videos for one theme or
    audience, for example one study program. If the campaign uses brand
    guidelines (campaigns created by this server do), the business name and
    logos are already set on the campaign: leave business_name and the logo
    lists empty. Otherwise they are required and are added to the asset group.

    Images and their required aspect ratio and minimum size:
    marketing_images 1.91:1 (600x314), square_marketing_images 1:1 (300x300),
    portrait_marketing_images 4:5 (480x600), logo_images 1:1 (128x128) and
    landscape_logo_images 4:1 (512x128). Each image is either an https:// URL
    of a PNG, JPEG or GIF of at most 5 MB, or the id of an image asset already
    in the account.

    By default the change is only validated, not applied. Show the returned
    changes to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer that owns the campaign.
        campaign_id: The id of the Performance Max campaign (campaign.id).
        asset_group_name: The name of the new asset group; it must be unique in the campaign.
        final_url: The landing page, starting with https://.
        headlines: 3 to 15 headlines of at most 30 characters each.
        long_headlines: 1 to 5 long headlines of at most 90 characters each.
        descriptions: 2 to 5 descriptions of at most 90 characters each.
        marketing_images: 1 to 20 landscape (1.91:1) images.
        square_marketing_images: 1 to 20 square images.
        portrait_marketing_images: Up to 20 optional portrait (4:5) images.
        youtube_videos: Up to 15 optional YouTube video ids or URLs.
        business_name: Only for campaigns without brand guidelines, at most 25 characters.
        logo_images: Only for campaigns without brand guidelines: 1 to 5 square logos.
        landscape_logo_images: Only for campaigns without brand guidelines: optional 4:1 logos.
        status: The status of the new asset group.
        validate_only: If true, validates the change without applying it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    campaign_id = _clean_id(campaign_id, "campaign_id")
    asset_group_name = " ".join(asset_group_name.split())
    if not asset_group_name:
        raise ToolError("asset_group_name cannot be empty.")
    final_url = _check_final_url(final_url)
    texts = _texts(headlines, long_headlines, descriptions)
    image_refs = {
        field_type: _image_refs(values, field_type, *IMAGE_LIMITS[field_type])
        for field_type, values in (
            ("MARKETING_IMAGE", marketing_images),
            ("SQUARE_MARKETING_IMAGE", square_marketing_images),
            ("PORTRAIT_MARKETING_IMAGE", portrait_marketing_images),
        )
    }
    video_ids = _video_ids(youtube_videos)

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    row = _find_one(
        client,
        customer_id,
        "SELECT campaign.resource_name, campaign.name, "
        "campaign.advertising_channel_type, campaign.brand_guidelines_enabled "
        f"FROM campaign WHERE campaign.id = {campaign_id}",
        f"Campaign {campaign_id} was not found in customer {customer_id}.",
    )
    if row.campaign.advertising_channel_type.name != "PERFORMANCE_MAX":
        raise ToolError(
            f"Campaign {campaign_id} is a "
            f"{row.campaign.advertising_channel_type.name} campaign; this tool "
            "only works with Performance Max campaigns."
        )
    brand_guidelines = row.campaign.brand_guidelines_enabled
    has_brand_assets = bool(
        business_name or logo_images or landscape_logo_images
    )
    if brand_guidelines and has_brand_assets:
        raise ToolError(
            f"Campaign {campaign_id} uses brand guidelines, so its business "
            "name and logos are set on the campaign. Leave business_name and "
            "the logo lists empty."
        )
    if not brand_guidelines:
        if not business_name:
            raise ToolError(
                f"Campaign {campaign_id} does not use brand guidelines, so the "
                "asset group needs business_name and logo_images."
            )
        texts["BUSINESS_NAME"] = [_clean_business_name(business_name)]
        image_refs.update(_logo_refs(logo_images, landscape_logo_images))

    request = _AssetRequest(client, customer_id)
    images, image_summary = _resolve_images(
        client, customer_id, request, asset_group_name, image_refs
    )
    asset_group_operation = _asset_group_operation(
        client,
        request,
        customer_id,
        row.campaign.resource_name,
        asset_group_name,
        final_url,
        status,
        texts,
        images,
        video_ids,
    )
    resource_names = _send_operations(
        client,
        customer_id,
        _ordered([], request, asset_group_operation),
        validate_only,
    )
    return _result(
        "create_asset_group",
        customer_id,
        validate_only,
        [
            {
                "campaign": row.campaign.name,
                "asset_group": asset_group_name,
                "status": status,
                "final_url": final_url,
                "headlines": texts["HEADLINE"],
                "long_headlines": texts["LONG_HEADLINE"],
                "descriptions": texts["DESCRIPTION"],
                "business_name": texts.get("BUSINESS_NAME", [None])[0],
                "images": image_summary,
                "youtube_video_ids": video_ids,
            }
        ],
        resource_names,
    )
