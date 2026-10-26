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

"""Read-only report tools: performance, search terms, budget pacing, asset
performance and recommendations.

Amounts are returned in the account currency (not micros) and dates are
computed in the account time zone.
"""

import calendar
import datetime
from typing import Any, Dict, List, Literal
from zoneinfo import ZoneInfo

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.api_core.exceptions import GoogleAPICallError
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.errors import format_google_ads_exception

reports_mcp = FastMCP("reports")

_READ_ONLY = ToolAnnotations(readOnlyHint=True)

DateRange = Literal[
    "TODAY",
    "YESTERDAY",
    "LAST_7_DAYS",
    "LAST_14_DAYS",
    "LAST_30_DAYS",
    "THIS_MONTH",
    "LAST_MONTH",
]
SortMetric = Literal["cost", "clicks", "impressions", "conversions"]

MAX_ROWS = 500
# Google may spend up to 30.4 times the average daily budget in a month.
DAYS_PER_BILLING_MONTH = 30.4
# Pacing below or above these percentages of the expected spend is flagged.
UNDER_PACING_PERCENT = 80
OVER_PACING_PERCENT = 110

# Search terms that rarely bring students to a paid postgraduate program.
DEFAULT_IRRELEVANT_WORDS = [
    "gratis",
    "gratuito",
    "gratuita",
    "empleo",
    "trabajo",
    "sueldo",
    "pdf",
    "descargar",
    "que es",
    "qué es",
    "definicion",
    "definición",
    "significado",
    "ejemplos",
]


def _ids(values: List[str | int], name: str) -> List[str]:
    cleaned = [utils.clean_customer_id(value) for value in values]
    if not all(cleaned):
        raise ToolError(f"{name} must contain numeric ids, got {values!r}.")
    return list(dict.fromkeys(cleaned))


def _rows(customer_id: str, query: str, login_customer_id) -> List[Any]:
    ga_service = utils.get_googleads_service(
        "GoogleAdsService", login_customer_id=login_customer_id
    )
    utils.logger.info(f"ads_mcp.reports query {query}")
    try:
        return [
            row
            for batch in ga_service.search_stream(
                customer_id=customer_id, query=query
            )
            for row in batch.results
        ]
    except GoogleAdsException as ex:
        raise ToolError(format_google_ads_exception(ex))
    except GoogleAPICallError as ex:
        raise ToolError(f"Google Ads could not run the query: {ex.message}")


def _account(customer_id: str, login_customer_id) -> tuple[str, str]:
    """Returns the currency and time zone of the account."""
    rows = _rows(
        customer_id,
        "SELECT customer.currency_code, customer.time_zone FROM customer",
        login_customer_id,
    )
    if not rows:
        raise ToolError(f"Customer {customer_id} was not found.")
    return rows[0].customer.currency_code, rows[0].customer.time_zone


def _now(time_zone: str) -> datetime.datetime:
    return datetime.datetime.now(ZoneInfo(time_zone))


def _period(
    date_range: str,
    start_date: str | None,
    end_date: str | None,
    today: datetime.date,
) -> tuple[datetime.date, datetime.date]:
    """Resolves a date range preset or explicit dates to (start, end)."""
    if start_date or end_date:
        if not (start_date and end_date):
            raise ToolError("Give both start_date and end_date, or neither.")
        try:
            start = datetime.date.fromisoformat(start_date)
            end = datetime.date.fromisoformat(end_date)
        except ValueError:
            raise ToolError("Dates must use the YYYY-MM-DD format.")
        if end < start:
            raise ToolError("end_date cannot be before start_date.")
        return start, end
    yesterday = today - datetime.timedelta(days=1)
    if date_range == "TODAY":
        return today, today
    if date_range == "YESTERDAY":
        return yesterday, yesterday
    if date_range == "THIS_MONTH":
        return today.replace(day=1), today
    if date_range == "LAST_MONTH":
        end = today.replace(day=1) - datetime.timedelta(days=1)
        return end.replace(day=1), end
    days = {"LAST_7_DAYS": 7, "LAST_14_DAYS": 14, "LAST_30_DAYS": 30}[
        date_range
    ]
    return today - datetime.timedelta(days=days), yesterday


def _previous_period(
    start: datetime.date, end: datetime.date
) -> tuple[datetime.date, datetime.date]:
    length = end - start
    previous_end = start - datetime.timedelta(days=1)
    return previous_end - length, previous_end


def _date_condition(start: datetime.date, end: datetime.date) -> str:
    return f"segments.date BETWEEN '{start}' AND '{end}'"


_METRIC_FIELDS = [
    "metrics.impressions",
    "metrics.clicks",
    "metrics.cost_micros",
    "metrics.conversions",
    "metrics.conversions_value",
]


def _metrics(row) -> Dict[str, float]:
    metrics = row.metrics
    return {
        "impressions": metrics.impressions,
        "clicks": metrics.clicks,
        "cost": metrics.cost_micros / 1_000_000,
        "conversions": metrics.conversions,
        "conversions_value": metrics.conversions_value,
    }


_EMPTY = {
    "impressions": 0,
    "clicks": 0,
    "cost": 0.0,
    "conversions": 0.0,
    "conversions_value": 0.0,
}


def _add(totals: Dict[str, float], values: Dict[str, float]) -> None:
    for key in (
        "impressions",
        "clicks",
        "cost",
        "conversions",
        "conversions_value",
    ):
        totals[key] = totals.get(key, 0) + values[key]


def _with_ratios(values: Dict[str, float]) -> Dict[str, Any]:
    """Rounds the metrics and adds CTR, CPC, conversion rate and CPA."""
    clicks = values["clicks"]
    conversions = values["conversions"]
    return {
        "impressions": int(values["impressions"]),
        "clicks": int(clicks),
        "cost": round(values["cost"], 2),
        "conversions": round(conversions, 2),
        "conversions_value": round(values["conversions_value"], 2),
        "ctr_percent": (
            round(clicks / values["impressions"] * 100, 2)
            if values["impressions"]
            else None
        ),
        "avg_cpc": round(values["cost"] / clicks, 2) if clicks else None,
        "conversion_rate_percent": (
            round(conversions / clicks * 100, 2) if clicks else None
        ),
        "cost_per_conversion": (
            round(values["cost"] / conversions, 2) if conversions else None
        ),
    }


def _change(current: float | None, previous: float | None) -> float | None:
    if current is None or not previous:
        return None
    return round((current - previous) / previous * 100, 1)


# Per level: the resource, its attribute fields, how to filter out removed
# entities, the id used to compare periods and how to describe a row.
_LEVELS = {
    "account": {
        "resource": "customer",
        "fields": ["customer.id", "customer.descriptive_name"],
        "conditions": [],
        "key": lambda row: str(row.customer.id),
        "id_filter": None,
        "describe": lambda row: {"account": row.customer.descriptive_name},
    },
    "campaign": {
        "resource": "campaign",
        "fields": [
            "campaign.id",
            "campaign.name",
            "campaign.status",
            "campaign.advertising_channel_type",
        ],
        "conditions": ["campaign.status != 'REMOVED'"],
        "key": lambda row: str(row.campaign.id),
        "id_filter": "campaign.id",
        "describe": lambda row: {
            "campaign_id": str(row.campaign.id),
            "campaign": row.campaign.name,
            "status": row.campaign.status.name,
            "channel": row.campaign.advertising_channel_type.name,
        },
    },
    "ad_group": {
        "resource": "ad_group",
        "fields": [
            "ad_group.id",
            "ad_group.name",
            "ad_group.status",
            "campaign.name",
        ],
        "conditions": [
            "ad_group.status != 'REMOVED'",
            "campaign.status != 'REMOVED'",
        ],
        "key": lambda row: str(row.ad_group.id),
        "id_filter": "ad_group.id",
        "describe": lambda row: {
            "ad_group_id": str(row.ad_group.id),
            "ad_group": row.ad_group.name,
            "status": row.ad_group.status.name,
            "campaign": row.campaign.name,
        },
    },
    "keyword": {
        "resource": "keyword_view",
        "fields": [
            "ad_group.id",
            "ad_group_criterion.criterion_id",
            "ad_group_criterion.keyword.text",
            "ad_group_criterion.keyword.match_type",
            "ad_group_criterion.status",
            "ad_group.name",
            "campaign.name",
        ],
        "conditions": [
            "ad_group_criterion.status != 'REMOVED'",
            "ad_group.status != 'REMOVED'",
            "campaign.status != 'REMOVED'",
        ],
        "key": lambda row: (
            f"{row.ad_group.id}~{row.ad_group_criterion.criterion_id}"
        ),
        "id_filter": "ad_group_criterion.criterion_id",
        "describe": lambda row: {
            "ad_group_id": str(row.ad_group.id),
            "criterion_id": str(row.ad_group_criterion.criterion_id),
            "keyword": row.ad_group_criterion.keyword.text,
            "match_type": row.ad_group_criterion.keyword.match_type.name,
            "status": row.ad_group_criterion.status.name,
            "ad_group": row.ad_group.name,
            "campaign": row.campaign.name,
        },
    },
    "ad": {
        "resource": "ad_group_ad",
        "fields": [
            "ad_group.id",
            "ad_group_ad.ad.id",
            "ad_group_ad.ad.type",
            "ad_group_ad.status",
            "ad_group.name",
            "campaign.name",
        ],
        "conditions": [
            "ad_group_ad.status != 'REMOVED'",
            "ad_group.status != 'REMOVED'",
            "campaign.status != 'REMOVED'",
        ],
        "key": lambda row: f"{row.ad_group.id}~{row.ad_group_ad.ad.id}",
        "id_filter": "ad_group_ad.ad.id",
        "describe": lambda row: {
            "ad_group_id": str(row.ad_group.id),
            "ad_id": str(row.ad_group_ad.ad.id),
            "ad_type": row.ad_group_ad.ad.type_.name,
            "status": row.ad_group_ad.status.name,
            "ad_group": row.ad_group.name,
            "campaign": row.campaign.name,
        },
    },
}


def _level_query(
    level: str,
    conditions: List[str],
    order_by: str | None = None,
    limit: int | None = None,
) -> str:
    spec = _LEVELS[level]
    query = (
        f"SELECT {', '.join(spec['fields'] + _METRIC_FIELDS)} "
        f"FROM {spec['resource']} WHERE "
        + " AND ".join(spec["conditions"] + conditions)
    )
    if order_by:
        query += f" ORDER BY {order_by} DESC"
    if limit:
        query += f" LIMIT {limit}"
    return query


_SORT_FIELDS = {
    "cost": "metrics.cost_micros",
    "clicks": "metrics.clicks",
    "impressions": "metrics.impressions",
    "conversions": "metrics.conversions",
}


@reports_mcp.tool(annotations=_READ_ONLY)
def get_performance_report(
    customer_id: str | int,
    level: Literal["account", "campaign", "ad_group", "keyword", "ad"] = (
        "campaign"
    ),
    date_range: DateRange = "LAST_30_DAYS",
    start_date: str | None = None,
    end_date: str | None = None,
    compare_previous_period: bool = False,
    campaign_ids: List[str | int] = [],
    sort_by: SortMetric = "cost",
    limit: int = 50,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Reports impressions, clicks, cost, conversions and their ratios.

    Returns one row per account, campaign, ad group, keyword or ad, sorted by
    the chosen metric, plus the totals of the period. Costs are in the
    account currency (not micros). Ratios are computed for you: CTR, average
    CPC, conversion rate and cost per conversion. With compare_previous_period
    each row and the totals also include the previous period of the same
    length and the change in percent.

    Removed campaigns, ad groups, keywords and ads are left out. Presets
    follow Google Ads: LAST_7_DAYS, LAST_14_DAYS and LAST_30_DAYS end
    yesterday; dates use the account time zone.

    Args:
        customer_id: The id of the customer.
        level: The level of the rows.
        date_range: A date range preset; ignored when start_date and end_date are given.
        start_date: Optional start date as YYYY-MM-DD.
        end_date: Optional end date as YYYY-MM-DD.
        compare_previous_period: Whether to compare with the previous period of the same length.
        campaign_ids: Optional campaign ids to limit the report to.
        sort_by: The metric that orders the rows, highest first.
        limit: The maximum number of rows, up to 500.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    if not 1 <= limit <= MAX_ROWS:
        raise ToolError(f"limit must be between 1 and {MAX_ROWS}.")
    campaign_ids = _ids(campaign_ids, "campaign_ids")
    if campaign_ids and level == "account":
        raise ToolError("campaign_ids cannot be used with the account level.")
    currency, time_zone = _account(customer_id, login_customer_id)
    start, end = _period(
        date_range, start_date, end_date, _now(time_zone).date()
    )
    filters = (
        [f"campaign.id IN ({', '.join(campaign_ids)})"] if campaign_ids else []
    )
    spec = _LEVELS[level]

    rows = _rows(
        customer_id,
        _level_query(
            level,
            [_date_condition(start, end)] + filters,
            _SORT_FIELDS[sort_by],
            limit,
        ),
        login_customer_id,
    )
    report = {spec["key"](row): row for row in rows}

    def totals_for(period_start, period_end):
        totals: Dict[str, float] = {}
        # The account level includes the spend of removed campaigns, so it is
        # used unless the report is limited to some campaigns.
        totals_level = "campaign" if filters else "account"
        for row in _rows(
            customer_id,
            _level_query(
                totals_level,
                [_date_condition(period_start, period_end)] + filters,
            ),
            login_customer_id,
        ):
            _add(totals, _metrics(row))
        return _with_ratios(totals) if totals else _with_ratios(_EMPTY)

    result: Dict[str, Any] = {
        "currency": currency,
        "period": {"start": str(start), "end": str(end)},
        "level": level,
        "rows": [],
        "totals": totals_for(start, end),
    }

    previous = {}
    if compare_previous_period:
        previous_start, previous_end = _previous_period(start, end)
        result["previous_period"] = {
            "start": str(previous_start),
            "end": str(previous_end),
        }
        result["previous_totals"] = totals_for(previous_start, previous_end)
        result["totals_change_percent"] = {
            key: _change(result["totals"][key], result["previous_totals"][key])
            for key in ("impressions", "clicks", "cost", "conversions")
        }
        if report and spec["id_filter"]:
            ids = sorted(
                {key.split("~")[-1] for key in report}
            )  # criterion and ad ids are filtered and then matched by key.
            previous_rows = _rows(
                customer_id,
                _level_query(
                    level,
                    [
                        _date_condition(previous_start, previous_end),
                        f"{spec['id_filter']} IN ({', '.join(ids)})",
                    ]
                    + filters,
                ),
                login_customer_id,
            )
            previous = {spec["key"](row): row for row in previous_rows}
        elif report:
            previous = {
                spec["key"](row): row
                for row in _rows(
                    customer_id,
                    _level_query(
                        level,
                        [_date_condition(previous_start, previous_end)],
                    ),
                    login_customer_id,
                )
            }

    for key, row in report.items():
        values = _with_ratios(_metrics(row))
        entry = {**spec["describe"](row), **values}
        if compare_previous_period:
            before = (
                _with_ratios(_metrics(previous[key]))
                if key in previous
                else _with_ratios(_EMPTY)
            )
            entry["previous"] = before
            entry["change_percent"] = {
                metric: _change(values[metric], before[metric])
                for metric in ("impressions", "clicks", "cost", "conversions")
            }
        result["rows"].append(entry)
    return result


@reports_mcp.tool(annotations=_READ_ONLY)
def get_search_terms_report(
    customer_id: str | int,
    date_range: DateRange = "LAST_30_DAYS",
    start_date: str | None = None,
    end_date: str | None = None,
    campaign_ids: List[str | int] = [],
    min_clicks_without_conversions: int = 3,
    irrelevant_words: List[str] = DEFAULT_IRRELEVANT_WORDS,
    limit: int = 200,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Lists the searches that triggered ads and suggests negative keywords.

    Returns the search terms of the period sorted by cost, with their
    campaign, ad group, status (ADDED: already a keyword, EXCLUDED: already
    a negative keyword, NONE: neither) and metrics in the account currency.

    negative_candidates lists terms with status NONE that either got at least
    min_clicks_without_conversions clicks without any conversion (suggested as
    an EXACT negative) or contain one of irrelevant_words (that word is
    suggested as a PHRASE negative). Review them with the user, then add the
    chosen ones with the add_negative_keywords tool.

    Args:
        customer_id: The id of the customer.
        date_range: A date range preset; ignored when start_date and end_date are given.
        start_date: Optional start date as YYYY-MM-DD.
        end_date: Optional end date as YYYY-MM-DD.
        campaign_ids: Optional campaign ids to limit the report to.
        min_clicks_without_conversions: Clicks without conversions that make a term a candidate.
        irrelevant_words: Words that make a term a candidate; defaults to common terms like "gratis", "empleo" or "pdf".
        limit: The maximum number of search terms, up to 500.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    if not 1 <= limit <= MAX_ROWS:
        raise ToolError(f"limit must be between 1 and {MAX_ROWS}.")
    campaign_ids = _ids(campaign_ids, "campaign_ids")
    currency, time_zone = _account(customer_id, login_customer_id)
    start, end = _period(
        date_range, start_date, end_date, _now(time_zone).date()
    )
    conditions = [_date_condition(start, end), "metrics.impressions > 0"]
    if campaign_ids:
        conditions.append(f"campaign.id IN ({', '.join(campaign_ids)})")
    rows = _rows(
        customer_id,
        "SELECT search_term_view.search_term, search_term_view.status, "
        "campaign.id, campaign.name, ad_group.id, ad_group.name, "
        + ", ".join(_METRIC_FIELDS)
        + " FROM search_term_view WHERE "
        + " AND ".join(conditions)
        + f" ORDER BY metrics.cost_micros DESC LIMIT {limit}",
        login_customer_id,
    )

    words = [word.lower().strip() for word in irrelevant_words if word.strip()]
    terms = []
    candidates = []
    for row in rows:
        term = row.search_term_view.search_term
        values = _with_ratios(_metrics(row))
        entry = {
            "search_term": term,
            "status": row.search_term_view.status.name,
            "campaign_id": str(row.campaign.id),
            "campaign": row.campaign.name,
            "ad_group_id": str(row.ad_group.id),
            "ad_group": row.ad_group.name,
            **values,
        }
        terms.append(entry)
        if entry["status"] != "NONE":
            continue
        location = {
            "campaign_id": entry["campaign_id"],
            "campaign": entry["campaign"],
            "ad_group_id": entry["ad_group_id"],
            "ad_group": entry["ad_group"],
            "cost": values["cost"],
            "clicks": values["clicks"],
        }
        found = [word for word in words if word in term.lower()]
        if found:
            candidates.append(
                {
                    "search_term": term,
                    "reason": f"contains {found[0]!r}",
                    "suggested_negative": found[0],
                    "match_type": "PHRASE",
                    **location,
                }
            )
        elif (
            values["conversions"] == 0
            and values["clicks"] >= min_clicks_without_conversions
        ):
            candidates.append(
                {
                    "search_term": term,
                    "reason": (f"{values['clicks']} clicks and no conversions"),
                    "suggested_negative": term,
                    "match_type": "EXACT",
                    **location,
                }
            )
    return {
        "currency": currency,
        "period": {"start": str(start), "end": str(end)},
        "search_terms": terms,
        "negative_candidates": candidates,
    }


@reports_mcp.tool(annotations=_READ_ONLY)
def get_budget_pacing(
    customer_id: str | int,
    campaign_ids: List[str | int] = [],
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Compares this month's spend with the budget of each enabled campaign.

    For every enabled campaign with a daily budget it returns the daily
    budget, the spend so far this month, the spend expected by now (daily
    budget times the elapsed days), the projected spend for the whole month,
    the most Google can charge in the month (30.4 times the daily budget) and
    a pacing status: UNDER (below 80% of expected), ON_TRACK or OVER (above
    110%). Amounts are in the account currency and days in its time zone.

    Args:
        customer_id: The id of the customer.
        campaign_ids: Optional campaign ids to limit the report to.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    campaign_ids = _ids(campaign_ids, "campaign_ids")
    currency, time_zone = _account(customer_id, login_customer_id)
    now = _now(time_zone)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elapsed_days = max((now - month_start).total_seconds() / 86_400, 1 / 24)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    filters = (
        [f"campaign.id IN ({', '.join(campaign_ids)})"] if campaign_ids else []
    )

    campaigns = _rows(
        customer_id,
        "SELECT campaign.id, campaign.name, campaign_budget.amount_micros, "
        "campaign_budget.period, campaign_budget.explicitly_shared "
        "FROM campaign WHERE "
        + " AND ".join(["campaign.status = 'ENABLED'"] + filters),
        login_customer_id,
    )
    spend = {
        row.campaign.id: row.metrics.cost_micros / 1_000_000
        for row in _rows(
            customer_id,
            "SELECT campaign.id, metrics.cost_micros FROM campaign WHERE "
            + " AND ".join(
                [
                    "campaign.status = 'ENABLED'",
                    _date_condition(month_start.date(), now.date()),
                ]
                + filters
            ),
            login_customer_id,
        )
    }

    result_rows = []
    totals = {"daily_budget": 0.0, "spend_to_date": 0.0, "projected_spend": 0.0}
    for row in campaigns:
        budget = row.campaign_budget
        if budget.period.name != "DAILY":
            continue
        daily = budget.amount_micros / 1_000_000
        spent = spend.get(row.campaign.id, 0.0)
        expected = daily * elapsed_days
        projected = spent / elapsed_days * days_in_month
        pacing = spent / expected * 100 if expected else None
        if pacing is None:
            status = None
        elif pacing < UNDER_PACING_PERCENT:
            status = "UNDER"
        elif pacing > OVER_PACING_PERCENT:
            status = "OVER"
        else:
            status = "ON_TRACK"
        result_rows.append(
            {
                "campaign_id": str(row.campaign.id),
                "campaign": row.campaign.name,
                "shared_budget": budget.explicitly_shared,
                "daily_budget": round(daily, 2),
                "spend_to_date": round(spent, 2),
                "expected_to_date": round(expected, 2),
                "projected_month_spend": round(projected, 2),
                "max_month_charge": round(daily * DAYS_PER_BILLING_MONTH, 2),
                "pacing_percent": (
                    round(pacing, 1) if pacing is not None else None
                ),
                "status": status,
            }
        )
        totals["daily_budget"] += daily
        totals["spend_to_date"] += spent
        totals["projected_spend"] += projected
    result_rows.sort(key=lambda entry: entry["spend_to_date"], reverse=True)
    return {
        "currency": currency,
        "month": f"{now:%Y-%m}",
        "elapsed_days": round(elapsed_days, 2),
        "days_in_month": days_in_month,
        "campaigns": result_rows,
        "totals": {key: round(value, 2) for key, value in totals.items()},
    }


@reports_mcp.tool(annotations=_READ_ONLY)
def get_asset_performance(
    customer_id: str | int,
    campaign_ids: List[str | int] = [],
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Shows which ad texts and assets perform well and which to replace.

    For responsive search ads it returns each headline and description with
    Google's performance label (BEST, GOOD, LOW, LEARNING or PENDING) and
    lists the LOW ones as candidates to replace. For Performance Max asset
    groups, where the API no longer returns performance labels, it lists the
    assets that are not fully eligible to serve, with Google's reasons.

    Args:
        customer_id: The id of the customer.
        campaign_ids: Optional campaign ids to limit the report to.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    campaign_ids = _ids(campaign_ids, "campaign_ids")
    filters = (
        [f"campaign.id IN ({', '.join(campaign_ids)})"] if campaign_ids else []
    )

    search_rows = _rows(
        customer_id,
        "SELECT campaign.name, ad_group.name, ad_group_ad.ad.id, "
        "ad_group_ad_asset_view.field_type, "
        "ad_group_ad_asset_view.performance_label, "
        "ad_group_ad_asset_view.pinned_field, asset.text_asset.text "
        "FROM ad_group_ad_asset_view WHERE "
        + " AND ".join(
            [
                "ad_group_ad_asset_view.enabled = TRUE",
                "ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'",
                "ad_group_ad.status != 'REMOVED'",
                "campaign.status != 'REMOVED'",
            ]
            + filters
        ),
        login_customer_id,
    )
    responsive_search_ads = []
    label_counts: Dict[str, int] = {}
    for row in search_rows:
        label = row.ad_group_ad_asset_view.performance_label.name
        label_counts[label] = label_counts.get(label, 0) + 1
        responsive_search_ads.append(
            {
                "campaign": row.campaign.name,
                "ad_group": row.ad_group.name,
                "ad_id": str(row.ad_group_ad.ad.id),
                "field": row.ad_group_ad_asset_view.field_type.name,
                "text": row.asset.text_asset.text,
                "performance_label": label,
                "pinned": row.ad_group_ad_asset_view.pinned_field.name
                not in ("UNSPECIFIED", "UNKNOWN"),
            }
        )

    pmax_rows = _rows(
        customer_id,
        "SELECT campaign.name, asset_group.name, "
        "asset_group_asset.field_type, asset_group_asset.primary_status, "
        "asset_group_asset.primary_status_reasons, asset.type, asset.name, "
        "asset.text_asset.text FROM asset_group_asset WHERE "
        + " AND ".join(
            [
                "asset_group_asset.status != 'REMOVED'",
                "campaign.status != 'REMOVED'",
            ]
            + filters
        ),
        login_customer_id,
    )
    pmax_not_eligible = []
    for row in pmax_rows:
        status = row.asset_group_asset.primary_status.name
        if status == "ELIGIBLE":
            continue
        pmax_not_eligible.append(
            {
                "campaign": row.campaign.name,
                "asset_group": row.asset_group.name,
                "field": row.asset_group_asset.field_type.name,
                "asset": row.asset.text_asset.text or row.asset.name,
                "asset_type": row.asset.type_.name,
                "status": status,
                "reasons": [
                    reason.name
                    for reason in row.asset_group_asset.primary_status_reasons
                ],
            }
        )
    return {
        "responsive_search_ads": {
            "label_counts": label_counts,
            "to_replace": [
                entry
                for entry in responsive_search_ads
                if entry["performance_label"] == "LOW"
            ],
            "assets": responsive_search_ads,
        },
        "performance_max": {
            "assets_checked": len(pmax_rows),
            "not_fully_eligible": pmax_not_eligible,
        },
    }


@reports_mcp.tool(annotations=_READ_ONLY)
def list_recommendations(
    customer_id: str | int,
    types: List[str] = [],
    campaign_ids: List[str | int] = [],
    limit: int = 50,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Lists Google's active recommendations for the account.

    Each recommendation has an id, its type (for example KEYWORD,
    CAMPAIGN_BUDGET, SITELINK_ASSET or RESPONSIVE_SEARCH_AD), its campaign
    and Google's estimate of the change in impressions, clicks, cost and
    conversions per week if applied. Budget and keyword recommendations also
    include their details. Apply or dismiss them with apply_recommendations
    and dismiss_recommendations after reviewing them with the user.

    Args:
        customer_id: The id of the customer.
        types: Optional recommendation types to include, for example ["KEYWORD"].
        campaign_ids: Optional campaign ids to limit the list to.
        limit: The maximum number of recommendations, up to 500.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    if not 1 <= limit <= MAX_ROWS:
        raise ToolError(f"limit must be between 1 and {MAX_ROWS}.")
    campaign_ids = _ids(campaign_ids, "campaign_ids")
    types = [t.strip().upper() for t in types if t.strip()]
    if not all(t.replace("_", "").isalnum() for t in types):
        raise ToolError(
            f"types must be recommendation type names, got {types}."
        )
    currency, _ = _account(customer_id, login_customer_id)

    conditions = ["recommendation.dismissed = FALSE"]
    if types:
        conditions.append(
            "recommendation.type IN ("
            + ", ".join(f"'{t}'" for t in types)
            + ")"
        )
    if campaign_ids:
        conditions.append(
            "recommendation.campaign IN ("
            + ", ".join(
                f"'customers/{customer_id}/campaigns/{campaign_id}'"
                for campaign_id in campaign_ids
            )
            + ")"
        )
    # Nested recommendation fields can only be selected as whole messages.
    rows = _rows(
        customer_id,
        "SELECT recommendation.resource_name, recommendation.type, "
        "recommendation.campaign, recommendation.impact, "
        "recommendation.campaign_budget_recommendation, "
        "recommendation.keyword_recommendation FROM recommendation WHERE "
        + " AND ".join(conditions)
        + f" LIMIT {limit}",
        login_customer_id,
    )
    campaign_names = {}
    campaign_resource_names = sorted(
        {
            row.recommendation.campaign
            for row in rows
            if row.recommendation.campaign
        }
    )
    if campaign_resource_names:
        campaign_names = {
            row.campaign.resource_name: row.campaign.name
            for row in _rows(
                customer_id,
                "SELECT campaign.resource_name, campaign.name FROM campaign "
                "WHERE campaign.resource_name IN ("
                + ", ".join(f"'{name}'" for name in campaign_resource_names)
                + ")",
                login_customer_id,
            )
        }

    recommendations = []
    for row in rows:
        recommendation = row.recommendation
        base = recommendation.impact.base_metrics
        potential = recommendation.impact.potential_metrics
        entry = {
            "recommendation_id": recommendation.resource_name.rsplit("/", 1)[1],
            "type": recommendation.type_.name,
            "campaign": campaign_names.get(
                recommendation.campaign, recommendation.campaign or None
            ),
            "weekly_impact": {
                "impressions": round(potential.impressions - base.impressions),
                "clicks": round(potential.clicks - base.clicks),
                "cost": round(
                    (potential.cost_micros - base.cost_micros) / 1_000_000, 2
                ),
                "conversions": round(
                    potential.conversions - base.conversions, 2
                ),
            },
        }
        budget = recommendation.campaign_budget_recommendation
        if budget.recommended_budget_amount_micros:
            entry["budget"] = {
                "current": budget.current_budget_amount_micros / 1_000_000,
                "recommended": budget.recommended_budget_amount_micros
                / 1_000_000,
            }
        keyword = recommendation.keyword_recommendation.keyword
        if keyword.text:
            entry["keyword"] = {
                "text": keyword.text,
                "match_type": keyword.match_type.name,
            }
        recommendations.append(entry)
    return {"currency": currency, "recommendations": recommendations}
