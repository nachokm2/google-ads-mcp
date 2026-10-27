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

"""Read-only diagnostics: account alerts, policy issues and change history.

They are registered in the `reports` namespace. Amounts are returned in the
account currency and dates use the account time zone.
"""

import datetime
import enum
from typing import Any, Dict, List

from fastmcp.exceptions import ToolError

import ads_mcp.utils as utils
from ads_mcp.tools.reports import (
    _READ_ONLY,
    _account,
    _ids,
    _now,
    _rows,
    reports_mcp,
)

# Yesterday's spend is flagged when it is this many times the average of the
# previous 7 days, or this fraction of it.
SPEND_SPIKE_RATIO = 2.0
SPEND_DROP_RATIO = 0.5
# Conversions are flagged when they fall to zero after at least this many.
MIN_PREVIOUS_CONVERSIONS = 3
ENDING_SOON_DAYS = 7
MAX_POLICY_ROWS = 200
MAX_CHANGES = 1000
CHANGE_HISTORY_DAYS = 30
_MAX_VALUE_LENGTH = 200

_SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
_PROBLEM_STATUSES = ("NOT_ELIGIBLE", "MISCONFIGURED")
_POLICY_STATUSES = ("DISAPPROVED", "APPROVED_LIMITED", "AREA_OF_INTEREST_ONLY")


def _money(micros: int) -> float:
    return round(micros / 1_000_000, 2)


def _alert(severity, kind, campaign, message, **details) -> Dict[str, Any]:
    alert = {
        "severity": severity,
        "type": kind,
        "campaign_id": str(campaign.id) if campaign else None,
        "campaign": campaign.name if campaign else None,
        "message": message,
    }
    if details:
        alert["details"] = details
    return alert


def _campaign_metrics(customer_id, start, end, login_customer_id):
    """Returns cost, impressions and conversions per campaign id."""
    totals: Dict[int, Dict[str, float]] = {}
    for row in _rows(
        customer_id,
        "SELECT campaign.id, metrics.cost_micros, metrics.impressions, "
        "metrics.conversions FROM campaign WHERE campaign.status = "
        f"'ENABLED' AND segments.date BETWEEN '{start}' AND '{end}'",
        login_customer_id,
    ):
        total = totals.setdefault(
            row.campaign.id, {"cost": 0, "impressions": 0, "conversions": 0}
        )
        total["cost"] += row.metrics.cost_micros
        total["impressions"] += row.metrics.impressions
        total["conversions"] += row.metrics.conversions
    return totals


def _topics(entries) -> List[Dict[str, Any]]:
    topics = []
    for entry in entries:
        evidence = []
        for item in entry.evidences:
            evidence.extend(item.text_list.texts)
            evidence.extend(item.website_list.websites)
            evidence.extend(item.destination_text_list.destination_texts)
        topic = {"topic": entry.topic, "type": entry.type_.name}
        if evidence:
            topic["evidence"] = [str(text) for text in evidence[:5]]
        topics.append(topic)
    return topics


@reports_mcp.tool(annotations=_READ_ONLY)
def get_account_alerts(
    customer_id: str | int,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Checks an account for problems that need attention today.

    Looks at the enabled campaigns and flags, by severity:
    - HIGH: campaigns that cannot serve (not eligible or misconfigured),
      campaigns that stopped getting impressions yesterday after serving the
      week before, and ads disapproved by Google.
    - MEDIUM: campaigns limited by budget or by other reasons, yesterday's
      spend far above or below the previous 7 days, and conversions that fell
      to zero in the last 7 days.
    - LOW: campaigns that end within 7 days.

    Use it every morning, or before a review, and then the other tools to
    look into each alert (for example get_policy_issues for disapproved ads).

    Args:
        customer_id: The id of the customer.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    currency, time_zone = _account(customer_id, login_customer_id)
    today = _now(time_zone).date()
    yesterday = today - datetime.timedelta(days=1)
    week_start = yesterday - datetime.timedelta(days=7)
    week_end = yesterday - datetime.timedelta(days=1)
    last_7_start = yesterday - datetime.timedelta(days=6)
    previous_7_start = last_7_start - datetime.timedelta(days=7)
    previous_7_end = last_7_start - datetime.timedelta(days=1)

    campaigns = {
        row.campaign.id: row
        for row in _rows(
            customer_id,
            "SELECT campaign.id, campaign.name, campaign.primary_status, "
            "campaign.primary_status_reasons, campaign.end_date_time, "
            "campaign_budget.amount_micros FROM campaign WHERE "
            "campaign.status = 'ENABLED'",
            login_customer_id,
        )
    }
    day = _campaign_metrics(
        customer_id, yesterday, yesterday, login_customer_id
    )
    week = _campaign_metrics(
        customer_id, week_start, week_end, login_customer_id
    )
    last_7 = _campaign_metrics(
        customer_id, last_7_start, yesterday, login_customer_id
    )
    previous_7 = _campaign_metrics(
        customer_id, previous_7_start, previous_7_end, login_customer_id
    )
    empty = {"cost": 0, "impressions": 0, "conversions": 0}

    alerts = []
    for campaign_id, row in campaigns.items():
        campaign = row.campaign
        status = campaign.primary_status.name
        reasons = [reason.name for reason in campaign.primary_status_reasons]
        yesterday_stats = day.get(campaign_id, empty)
        week_stats = week.get(campaign_id, empty)
        if status in _PROBLEM_STATUSES:
            alerts.append(
                _alert(
                    "HIGH",
                    "CANNOT_SERVE",
                    campaign,
                    f"The campaign is enabled but {status.lower()}.",
                    reasons=reasons,
                )
            )
        elif not yesterday_stats["impressions"] and week_stats["impressions"]:
            alerts.append(
                _alert(
                    "HIGH",
                    "STOPPED_SERVING",
                    campaign,
                    "No impressions yesterday after serving the previous "
                    "week.",
                    impressions_previous_7_days=week_stats["impressions"],
                    reasons=reasons,
                )
            )
        elif status == "LIMITED":
            budget_limited = "BUDGET_CONSTRAINED" in reasons
            alerts.append(
                _alert(
                    "MEDIUM",
                    "LIMITED_BY_BUDGET" if budget_limited else "LIMITED",
                    campaign,
                    (
                        "The budget limits how often ads show."
                        if budget_limited
                        else "The campaign serves with limitations."
                    ),
                    reasons=reasons,
                    daily_budget=_money(row.campaign_budget.amount_micros),
                )
            )

        average = week_stats["cost"] / 7
        if average and yesterday_stats["cost"] >= average * SPEND_SPIKE_RATIO:
            kind, text = "SPEND_SPIKE", "far above"
        elif average and yesterday_stats["cost"] <= average * SPEND_DROP_RATIO:
            kind, text = "SPEND_DROP", "far below"
        else:
            kind = None
        if kind:
            alerts.append(
                _alert(
                    "MEDIUM",
                    kind,
                    campaign,
                    f"Yesterday's spend was {text} the average of the "
                    "previous 7 days.",
                    spend_yesterday=_money(yesterday_stats["cost"]),
                    average_previous_7_days=_money(average),
                )
            )

        previous_conversions = previous_7.get(campaign_id, empty)["conversions"]
        if (
            not last_7.get(campaign_id, empty)["conversions"]
            and previous_conversions >= MIN_PREVIOUS_CONVERSIONS
        ):
            alerts.append(
                _alert(
                    "MEDIUM",
                    "CONVERSIONS_STOPPED",
                    campaign,
                    "No conversions in the last 7 days after "
                    f"{previous_conversions:g} the 7 days before.",
                )
            )

        if campaign.end_date_time:
            end = datetime.date.fromisoformat(campaign.end_date_time[:10])
            if (
                today
                <= end
                <= today + datetime.timedelta(days=ENDING_SOON_DAYS)
            ):
                alerts.append(
                    _alert(
                        "LOW",
                        "ENDING_SOON",
                        campaign,
                        f"The campaign ends on {end}.",
                    )
                )

    disapproved: Dict[int, int] = {}
    for row in _rows(
        customer_id,
        "SELECT campaign.id, campaign.status, ad_group.status, "
        "ad_group_ad.status, ad_group_ad.ad.id FROM ad_group_ad WHERE "
        "campaign.status = 'ENABLED' AND ad_group.status = 'ENABLED' AND "
        "ad_group_ad.status = 'ENABLED' AND "
        "ad_group_ad.policy_summary.approval_status = 'DISAPPROVED'",
        login_customer_id,
    ):
        disapproved[row.campaign.id] = disapproved.get(row.campaign.id, 0) + 1
    for campaign_id, count in disapproved.items():
        row = campaigns.get(campaign_id)
        alerts.append(
            _alert(
                "HIGH",
                "ADS_DISAPPROVED",
                row.campaign if row else None,
                f"{count} enabled ads are disapproved; see get_policy_issues.",
                disapproved_ads=count,
            )
        )

    alerts.sort(key=lambda alert: _SEVERITY_ORDER[alert["severity"]])
    total_yesterday = sum(stats["cost"] for stats in day.values())
    total_week = sum(stats["cost"] for stats in week.values())
    return {
        "customer_id": customer_id,
        "currency": currency,
        "date": today.isoformat(),
        "enabled_campaigns": len(campaigns),
        "spend": {
            "yesterday": _money(total_yesterday),
            "average_previous_7_days": _money(total_week / 7),
        },
        "summary": {
            severity: sum(alert["severity"] == severity for alert in alerts)
            for severity in _SEVERITY_ORDER
        },
        "alerts": alerts,
    }


@reports_mcp.tool(annotations=_READ_ONLY)
def get_policy_issues(
    customer_id: str | int,
    campaign_ids: List[str | int] = [],
    include_limited: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Lists ads and assets that Google disapproved or limited, and why.

    For each ad or campaign asset it returns the approval status, the
    policies involved (for example "Trademarks" or "Destination not
    working"), whether each is prohibited or only limits where the ad shows,
    and the text or URL Google flagged. Removed and paused items are left
    out.

    Args:
        customer_id: The id of the customer.
        campaign_ids: Optional campaign ids to check; all campaigns by default.
        include_limited: Whether to include approved with limitations, not only disapproved.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    statuses = _POLICY_STATUSES if include_limited else ("DISAPPROVED",)
    status_list = ", ".join(f"'{status}'" for status in statuses)
    campaign_filter = ""
    if campaign_ids:
        ids = ", ".join(_ids(campaign_ids, "campaign_ids"))
        campaign_filter = f" AND campaign.id IN ({ids})"

    ads = []
    for row in _rows(
        customer_id,
        "SELECT campaign.id, campaign.name, campaign.status, ad_group.name, "
        "ad_group.status, ad_group_ad.status, ad_group_ad.ad.id, "
        "ad_group_ad.ad.type, ad_group_ad.ad.final_urls, "
        "ad_group_ad.policy_summary.approval_status, "
        "ad_group_ad.policy_summary.review_status, "
        "ad_group_ad.policy_summary.policy_topic_entries FROM ad_group_ad "
        "WHERE campaign.status != 'REMOVED' AND ad_group.status != 'REMOVED' "
        "AND ad_group_ad.status = 'ENABLED' AND "
        f"ad_group_ad.policy_summary.approval_status IN ({status_list})"
        f"{campaign_filter} LIMIT {MAX_POLICY_ROWS}",
        login_customer_id,
    ):
        summary = row.ad_group_ad.policy_summary
        ads.append(
            {
                "campaign_id": str(row.campaign.id),
                "campaign": row.campaign.name,
                "ad_group": row.ad_group.name,
                "ad_id": str(row.ad_group_ad.ad.id),
                "ad_type": row.ad_group_ad.ad.type_.name,
                "final_urls": list(row.ad_group_ad.ad.final_urls),
                "approval_status": summary.approval_status.name,
                "review_status": summary.review_status.name,
                "policies": _topics(summary.policy_topic_entries),
            }
        )

    assets = []
    for row in _rows(
        customer_id,
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign_asset.status, campaign_asset.field_type, "
        "asset.id, asset.name, asset.type, "
        "asset.policy_summary.approval_status, "
        "asset.policy_summary.review_status, "
        "asset.policy_summary.policy_topic_entries FROM campaign_asset WHERE "
        "campaign.status != 'REMOVED' AND campaign_asset.status = 'ENABLED' "
        f"AND asset.policy_summary.approval_status IN ({status_list})"
        f"{campaign_filter} LIMIT {MAX_POLICY_ROWS}",
        login_customer_id,
    ):
        summary = row.asset.policy_summary
        assets.append(
            {
                "campaign_id": str(row.campaign.id),
                "campaign": row.campaign.name,
                "asset_id": str(row.asset.id),
                "asset": row.asset.name or None,
                "asset_type": row.asset.type_.name,
                "used_as": row.campaign_asset.field_type.name,
                "approval_status": summary.approval_status.name,
                "review_status": summary.review_status.name,
                "policies": _topics(summary.policy_topic_entries),
            }
        )

    return {
        "customer_id": customer_id,
        "ads": ads,
        "campaign_assets": assets,
        "summary": {
            "ads": len(ads),
            "ads_disapproved": sum(
                ad["approval_status"] == "DISAPPROVED" for ad in ads
            ),
            "campaign_assets": len(assets),
        },
    }


def _value(message, path: str) -> Any:
    """Returns a field of a changed resource, readable and short."""
    value = message
    for part in path.split("."):
        if not hasattr(value, part) and hasattr(value, f"{part}_"):
            part = f"{part}_"
        if not hasattr(value, part):
            return None
        value = getattr(value, part)
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, int) and path.endswith("_micros"):
        return _money(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = value
    else:
        text = str(value).replace("\n", " ")
    if isinstance(text, str) and len(text) > _MAX_VALUE_LENGTH:
        text = text[:_MAX_VALUE_LENGTH] + "…"
    return text


@reports_mcp.tool(annotations=_READ_ONLY)
def get_change_history(
    customer_id: str | int,
    days: int = 7,
    campaign_ids: List[str | int] = [],
    user_email: str | None = None,
    limit: int = 200,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Lists the recent changes made to the account, by whom and from where.

    Each change shows when it happened, who made it, from where (the Google
    Ads website, the API, which includes this MCP server, or Google's
    automatic changes), what resource changed and, for updates, the
    changed fields with their old and new values. Google keeps the last 30
    days.

    Args:
        customer_id: The id of the customer.
        days: How many days back to look, 1 to 30.
        campaign_ids: Optional campaign ids to filter by.
        user_email: Optional email of the person who made the changes.
        limit: Maximum number of changes, newest first, 1 to 1000.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    if not 1 <= days <= CHANGE_HISTORY_DAYS:
        raise ToolError(f"days must be between 1 and {CHANGE_HISTORY_DAYS}.")
    if not 1 <= limit <= MAX_CHANGES:
        raise ToolError(f"limit must be between 1 and {MAX_CHANGES}.")
    _, time_zone = _account(customer_id, login_customer_id)
    now = _now(time_zone)
    # A few minutes of margin keep the period inside Google's 30 days.
    start = (
        now - datetime.timedelta(days=days) + datetime.timedelta(minutes=5)
    ).strftime("%Y-%m-%d %H:%M:%S")
    end = (now + datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    conditions = [
        f"change_event.change_date_time >= '{start}'",
        f"change_event.change_date_time <= '{end}'",
    ]
    if campaign_ids:
        names = ", ".join(
            f"'customers/{customer_id}/campaigns/{campaign_id}'"
            for campaign_id in _ids(campaign_ids, "campaign_ids")
        )
        conditions.append(f"change_event.campaign IN ({names})")
    if user_email:
        email = user_email.strip().replace("'", "")
        conditions.append(f"change_event.user_email = '{email}'")

    changes = []
    for row in _rows(
        customer_id,
        "SELECT change_event.change_date_time, "
        "change_event.change_resource_type, "
        "change_event.change_resource_name, change_event.client_type, "
        "change_event.user_email, change_event.resource_change_operation, "
        "change_event.changed_fields, change_event.old_resource, "
        "change_event.new_resource, campaign.name FROM change_event WHERE "
        + " AND ".join(conditions)
        + f" ORDER BY change_event.change_date_time DESC LIMIT {limit}",
        login_customer_id,
    ):
        event = row.change_event
        resource_type = event.change_resource_type.name
        operation = event.resource_change_operation.name
        change = {
            "date_time": event.change_date_time,
            "user": event.user_email or None,
            "source": event.client_type.name,
            "operation": operation,
            "resource_type": resource_type,
            "resource": event.change_resource_name,
            "campaign": row.campaign.name or None,
        }
        fields = list(event.changed_fields.paths)
        if fields:
            change["changed_fields"] = fields
        if operation == "UPDATE" and fields:
            attribute = resource_type.lower()
            old = getattr(event.old_resource, attribute, None)
            new = getattr(event.new_resource, attribute, None)
            if old is not None and new is not None:
                change["values"] = {
                    field: {
                        "from": _value(old, field),
                        "to": _value(new, field),
                    }
                    for field in fields
                }
        changes.append(change)

    return {
        "customer_id": customer_id,
        "period": {"from": start, "to": now.strftime("%Y-%m-%d %H:%M:%S")},
        "changes": changes,
        "summary": {
            "changes": len(changes),
            "by_source": {
                source: sum(change["source"] == source for change in changes)
                for source in sorted({change["source"] for change in changes})
            },
        },
    }
