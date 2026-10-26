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

"""Read-only Keyword Planner tools: keyword ideas and search volumes.

They are registered in the `reports` namespace. Bids are returned in the
account currency (not micros).
"""

from typing import Any, Dict, List

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.api_core.exceptions import GoogleAPICallError

import ads_mcp.utils as utils
from ads_mcp.errors import format_google_ads_exception
from ads_mcp.tools.reports import _READ_ONLY, _account, _ids, reports_mcp

DEFAULT_LOCATION_IDS = ["2152"]  # Chile
DEFAULT_LANGUAGE_ID = "1003"  # Spanish
MAX_SEED_KEYWORDS = 20
MAX_VOLUME_KEYWORDS = 1000
MAX_IDEAS = 500
# Ideas are read up to this many before sorting them by volume.
_MAX_IDEAS_READ = 2000
_MONTHS = [
    "JANUARY",
    "FEBRUARY",
    "MARCH",
    "APRIL",
    "MAY",
    "JUNE",
    "JULY",
    "AUGUST",
    "SEPTEMBER",
    "OCTOBER",
    "NOVEMBER",
    "DECEMBER",
]


def _keywords(values: List[str], name: str, maximum: int) -> List[str]:
    cleaned = list(
        dict.fromkeys(" ".join(value.split()) for value in values if value)
    )
    cleaned = [value for value in cleaned if value]
    if len(cleaned) > maximum:
        raise ToolError(f"{name} can have at most {maximum} keywords.")
    return cleaned


def _network(client, include_search_partners: bool):
    networks = client.enums.KeywordPlanNetworkEnum
    if include_search_partners:
        return networks.GOOGLE_SEARCH_AND_PARTNERS
    return networks.GOOGLE_SEARCH


def _micros(value: int) -> float | None:
    return round(value / 1_000_000, 2) if value else None


def _metrics(metrics, include_monthly_volumes: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "avg_monthly_searches": metrics.avg_monthly_searches,
        "competition": metrics.competition.name,
        "competition_index": metrics.competition_index,
        "top_of_page_bid_low": _micros(metrics.low_top_of_page_bid_micros),
        "top_of_page_bid_high": _micros(metrics.high_top_of_page_bid_micros),
    }
    if include_monthly_volumes:
        result["monthly_searches"] = [
            {
                "month": (
                    f"{volume.year}-"
                    f"{_MONTHS.index(volume.month.name) + 1:02d}"
                ),
                "searches": volume.monthly_searches,
            }
            for volume in metrics.monthly_search_volumes
            if volume.month.name in _MONTHS
        ]
    return result


def _call(method, request):
    try:
        return method(request=request)
    except GoogleAdsException as ex:
        message = format_google_ads_exception(ex)
        if "DEVELOPER_TOKEN_NOT_APPROVED" in message:
            message += (
                "\nKeyword Planner needs Basic or Standard Google Ads API "
                "access. The server administrator can apply for it for the "
                "Google Cloud project of the OAuth client (Google Ads API > "
                "Access level in the Cloud Console)."
            )
        raise ToolError(message)
    except GoogleAPICallError as ex:
        raise ToolError(f"Keyword Planner could not answer: {ex.message}")


def _targeting(request, location_ids, language_id) -> None:
    request.language = (
        f"languageConstants/{_ids([language_id], 'language_id')[0]}"
    )
    for location_id in _ids(location_ids, "location_ids"):
        request.geo_target_constants.append(f"geoTargetConstants/{location_id}")


@reports_mcp.tool(annotations=_READ_ONLY)
def get_keyword_ideas(
    customer_id: str | int,
    seed_keywords: List[str] = [],
    url: str | None = None,
    location_ids: List[str | int] = DEFAULT_LOCATION_IDS,
    language_id: str | int = DEFAULT_LANGUAGE_ID,
    include_search_partners: bool = False,
    limit: int = 50,
    include_monthly_volumes: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Suggests keywords with their monthly searches, from Keyword Planner.

    Give seed keywords (for example "maestría en psicología"), a page of
    the website, or both. Returns the ideas with the most monthly searches
    first, with competition and the usual range of top of page bids, for the
    chosen countries and language. Useful before creating a campaign or
    launching a program in a new country.

    Args:
        customer_id: The id of the customer whose currency is used for bids.
        seed_keywords: Up to 20 keywords to get ideas from.
        url: Optional page to get ideas from, for example a program page.
        location_ids: Geo target constant ids, for example 2152 Chile, 2604 Peru, 2170 Colombia, 2218 Ecuador, 2214 Dominican Republic.
        language_id: Language constant id; 1003 is Spanish, 1000 English.
        include_search_partners: Whether to include the search partners network.
        limit: Maximum number of ideas, 1 to 500.
        include_monthly_volumes: Whether to include the searches of each of the last 12 months.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    seeds = _keywords(seed_keywords, "seed_keywords", MAX_SEED_KEYWORDS)
    url = (url or "").strip()
    if not seeds and not url:
        raise ToolError("Give seed_keywords, a url, or both.")
    if url and not url.startswith(("https://", "http://")):
        raise ToolError("url must start with https:// or http://.")
    if not 1 <= limit <= MAX_IDEAS:
        raise ToolError(f"limit must be between 1 and {MAX_IDEAS}.")

    currency, _ = _account(customer_id, login_customer_id)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    request = client.get_type("GenerateKeywordIdeasRequest")
    request.customer_id = customer_id
    request.include_adult_keywords = False
    request.keyword_plan_network = _network(client, include_search_partners)
    _targeting(request, location_ids, language_id)
    if seeds and url:
        request.keyword_and_url_seed.url = url
        request.keyword_and_url_seed.keywords.extend(seeds)
    elif seeds:
        request.keyword_seed.keywords.extend(seeds)
    else:
        request.url_seed.url = url

    service = utils.get_googleads_service(
        "KeywordPlanIdeaService", login_customer_id=login_customer_id
    )
    ideas = []
    for idea in _call(service.generate_keyword_ideas, request):
        ideas.append(idea)
        if len(ideas) >= _MAX_IDEAS_READ:
            break
    ideas.sort(
        key=lambda idea: idea.keyword_idea_metrics.avg_monthly_searches,
        reverse=True,
    )
    return {
        "currency": currency,
        "location_ids": list(request.geo_target_constants),
        "language": request.language,
        "ideas_found": len(ideas),
        "ideas": [
            {
                "keyword": idea.text,
                **_metrics(idea.keyword_idea_metrics, include_monthly_volumes),
            }
            for idea in ideas[:limit]
        ],
    }


@reports_mcp.tool(annotations=_READ_ONLY)
def get_keyword_volumes(
    customer_id: str | int,
    keywords: List[str],
    location_ids: List[str | int] = DEFAULT_LOCATION_IDS,
    language_id: str | int = DEFAULT_LANGUAGE_ID,
    include_search_partners: bool = False,
    include_monthly_volumes: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Shows the monthly searches of specific keywords, from Keyword Planner.

    Returns, for each keyword, its average monthly searches over the last 12
    months, competition and the usual range of top of page bids, for the
    chosen countries and language. Google groups close variants (for
    example with and without accents) and reports them together.

    Args:
        customer_id: The id of the customer whose currency is used for bids.
        keywords: 1 to 1000 keywords.
        location_ids: Geo target constant ids, for example 2152 Chile, 2604 Peru, 2170 Colombia, 2218 Ecuador, 2214 Dominican Republic.
        language_id: Language constant id; 1003 is Spanish, 1000 English.
        include_search_partners: Whether to include the search partners network.
        include_monthly_volumes: Whether to include the searches of each of the last 12 months.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    keywords = _keywords(keywords, "keywords", MAX_VOLUME_KEYWORDS)
    if not keywords:
        raise ToolError("keywords needs at least one keyword.")

    currency, _ = _account(customer_id, login_customer_id)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    request = client.get_type("GenerateKeywordHistoricalMetricsRequest")
    request.customer_id = customer_id
    request.keywords.extend(keywords)
    request.include_adult_keywords = False
    request.keyword_plan_network = _network(client, include_search_partners)
    _targeting(request, location_ids, language_id)

    service = utils.get_googleads_service(
        "KeywordPlanIdeaService", login_customer_id=login_customer_id
    )
    response = _call(service.generate_keyword_historical_metrics, request)
    return {
        "currency": currency,
        "location_ids": list(request.geo_target_constants),
        "language": request.language,
        "keywords": [
            {
                "keyword": result.text,
                "close_variants": list(result.close_variants),
                **_metrics(result.keyword_metrics, include_monthly_volumes),
            }
            for result in response.results
        ],
    }
