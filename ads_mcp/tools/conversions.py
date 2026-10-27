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

"""Tools for offline conversions, for example enrollments from a CRM.

create_offline_conversion_action creates the conversion action that receives
the uploads. upload_offline_conversions sends conversions identified by the
Google click id (gclid) or, for enhanced conversions for leads, by the email
or phone of the lead, through the Data Manager API, which Google Ads requires
for new integrations. Emails and phones are normalized and hashed with
SHA-256 before they leave the server, and they are never logged or returned.

The tools are registered in the `mutate` namespace, so they are enabled
together with the other write tools.
"""

import datetime
import hashlib
import re
from typing import Any, Dict, List, Literal
from zoneinfo import ZoneInfo

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

import ads_mcp.utils as utils
from ads_mcp import data_manager
from ads_mcp.tools.create import _send_operations
from ads_mcp.tools.mutate import (
    _CREATE_ANNOTATIONS,
    _clean_id,
    _find_one,
    _result,
    mutate_mcp,
)

MAX_LOOKBACK_DAYS = 90
MAX_CONVERSIONS_PER_CALL = 2000

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_DATE_TIME = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)\s*([+-]\d{2}:?\d{2}|Z)?$"
)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_\-]+$")

ConversionCategory = Literal[
    "CONVERTED_LEAD",
    "QUALIFIED_LEAD",
    "SUBMIT_LEAD_FORM",
    "IMPORTED_LEAD",
    "SIGNUP",
    "BOOK_APPOINTMENT",
    "CONTACT",
    "PURCHASE",
    "DEFAULT",
]


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def create_offline_conversion_action(
    customer_id: str | int,
    name: str,
    category: ConversionCategory = "CONVERTED_LEAD",
    default_value: float | None = None,
    always_use_default_value: bool = False,
    click_through_lookback_window_days: int = MAX_LOOKBACK_DAYS,
    primary_for_goal: bool = True,
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Creates a conversion action that receives offline conversion uploads.

    Use one action per CRM milestone, for example "Matrícula" with category
    CONVERTED_LEAD or "Lead calificado" with QUALIFIED_LEAD. Primary actions
    are the ones bidding strategies optimize for. Each click counts at most
    one conversion. The returned conversion_action_id is the one
    upload_offline_conversions needs.

    To upload by email or phone (enhanced conversions for leads), the account
    must also turn on enhanced conversions for leads and accept the customer
    data terms in Google Ads (Goals > Settings).

    By default the action is only validated, not created. Show the returned
    summary to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer.
        name: The name of the conversion action, unique in the account.
        category: What the conversion represents.
        default_value: Optional value in the account currency used when an upload has none.
        always_use_default_value: Whether to ignore the values of the uploads and always use default_value.
        click_through_lookback_window_days: How many days after a click a conversion can be attributed to it, 1 to 90.
        primary_for_goal: Whether bidding strategies optimize for this conversion.
        validate_only: If true, validates the action without creating it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    name = " ".join(name.split())
    if not 1 <= len(name) <= 100:
        raise ToolError("name must have between 1 and 100 characters.")
    if not 1 <= click_through_lookback_window_days <= MAX_LOOKBACK_DAYS:
        raise ToolError(
            "click_through_lookback_window_days must be between 1 and "
            f"{MAX_LOOKBACK_DAYS}."
        )
    if default_value is not None and default_value < 0:
        raise ToolError("default_value cannot be negative.")
    if always_use_default_value and default_value is None:
        raise ToolError("always_use_default_value needs default_value.")

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    currency = _find_one(
        client,
        customer_id,
        "SELECT customer.currency_code FROM customer",
        f"Customer {customer_id} was not found.",
    ).customer.currency_code

    operation = client.get_type("MutateOperation")
    action = operation.conversion_action_operation.create
    action.name = name
    action.type_ = client.enums.ConversionActionTypeEnum.UPLOAD_CLICKS
    action.category = client.enums.ConversionActionCategoryEnum[category]
    action.status = client.enums.ConversionActionStatusEnum.ENABLED
    action.counting_type = (
        client.enums.ConversionActionCountingTypeEnum.ONE_PER_CLICK
    )
    action.click_through_lookback_window_days = (
        click_through_lookback_window_days
    )
    action.primary_for_goal = primary_for_goal
    action.value_settings.default_currency_code = currency
    action.value_settings.default_value = default_value or 0
    action.value_settings.always_use_default_value = always_use_default_value

    resource_names = _send_operations(
        client, customer_id, [operation], validate_only
    )
    result = _result(
        "create_offline_conversion_action",
        customer_id,
        validate_only,
        [
            {
                "name": name,
                "type": "UPLOAD_CLICKS",
                "category": category,
                "currency": currency,
                "default_value": default_value,
                "always_use_default_value": always_use_default_value,
                "click_through_lookback_window_days": (
                    click_through_lookback_window_days
                ),
                "primary_for_goal": primary_for_goal,
            }
        ],
        resource_names,
    )
    if resource_names:
        result["conversion_action_id"] = resource_names[0].rsplit("/", 1)[1]
    return result


class OfflineConversion(BaseModel):
    """One conversion to upload."""

    model_config = ConfigDict(extra="forbid")

    gclid: str | None = Field(
        None, description="The Google click id saved with the lead, if any."
    )
    gbraid: str | None = Field(
        None, description="The iOS app click id (gbraid), if any."
    )
    wbraid: str | None = Field(
        None, description="The iOS web click id (wbraid), if any."
    )
    email: str | None = Field(
        None,
        description="The lead's email, for enhanced conversions for leads; it is hashed before upload.",
    )
    phone: str | None = Field(
        None,
        description="The lead's phone in international format, for example +56912345678; it is hashed before upload.",
    )
    conversion_date_time: str = Field(
        description="When the conversion happened, as YYYY-MM-DD HH:MM:SS in the account time zone, or with an offset such as -03:00."
    )
    value: float | None = Field(None, description="Optional conversion value.")
    currency: str | None = Field(
        None,
        description="Optional ISO 4217 currency of the value; defaults to the account currency.",
    )
    order_id: str | None = Field(
        None,
        description="Optional unique id, for example the CRM deal id; Google ignores repeated uploads with the same id.",
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_email(email: str) -> str:
    """Normalizes an email following the Data Manager formatting rules."""
    email = "".join(email.split()).lower()
    local, separator, domain = email.partition("@")
    if not separator or not local or "." not in domain:
        raise ToolError("email is not a valid email address.")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
    return f"{local}@{domain}"


def _normalize_phone(phone: str) -> str:
    phone = re.sub(r"[\s\-().]", "", phone)
    if not _E164.match(phone):
        raise ToolError(
            "phone must be in international format, for example +56912345678."
        )
    return phone


def _event_timestamp(value: str, time_zone: str) -> str:
    """Returns the date time in ISO 8601 with its UTC offset."""
    match = _DATE_TIME.match(value.strip())
    if not match:
        raise ToolError(
            "conversion_date_time must look like 2026-10-24 15:30:00, "
            f"got {value!r}."
        )
    date, time, offset = match.groups()
    if len(time) == 5:
        time += ":00"
    try:
        moment = datetime.datetime.fromisoformat(f"{date}T{time}")
    except ValueError:
        raise ToolError(f"conversion_date_time is not a valid date: {value!r}.")
    if offset is None:
        moment = moment.replace(tzinfo=ZoneInfo(time_zone))
    elif offset == "Z":
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    else:
        offset = offset.replace(":", "")
        delta = datetime.timedelta(
            hours=int(offset[1:3]), minutes=int(offset[3:5])
        )
        moment = moment.replace(
            tzinfo=datetime.timezone(delta if offset[0] == "+" else -delta)
        )
    if moment > datetime.datetime.now(datetime.timezone.utc):
        raise ToolError(f"conversion_date_time is in the future: {value!r}.")
    return moment.isoformat(timespec="seconds")


def _event(conversion: OfflineConversion, time_zone: str, currency: str):
    """Builds a Data Manager event and lists what identifies it."""
    click_ids = {
        name: (getattr(conversion, name) or "").strip()
        for name in ("gclid", "gbraid", "wbraid")
    }
    click_ids = {name: value for name, value in click_ids.items() if value}
    email = (conversion.email or "").strip()
    phone = (conversion.phone or "").strip()
    if not click_ids and not email and not phone:
        raise ToolError(
            "needs a gclid, gbraid or wbraid, or an email or phone."
        )
    if conversion.value is not None and conversion.value < 0:
        raise ToolError("value cannot be negative.")

    event: Dict[str, Any] = {
        "eventTimestamp": _event_timestamp(
            conversion.conversion_date_time, time_zone
        ),
        "eventSource": "WEB",
    }
    if click_ids:
        event["adIdentifiers"] = click_ids
    identifiers = []
    if email:
        identifiers.append({"emailAddress": _hash(_normalize_email(email))})
    if phone:
        identifiers.append({"phoneNumber": _hash(_normalize_phone(phone))})
    if identifiers:
        event["userData"] = {"userIdentifiers": identifiers}
    if conversion.value is not None:
        event["conversionValue"] = conversion.value
        event["currency"] = (conversion.currency or currency).upper()
    if conversion.order_id and conversion.order_id.strip():
        event["transactionId"] = conversion.order_id.strip()
    identified_by = (
        list(click_ids)
        + (["email"] if email else [])
        + (["phone"] if phone else [])
    )
    return event, identified_by


def _require_data_manager() -> None:
    if not data_manager.enabled():
        raise ToolError(
            "Offline conversion uploads use the Data Manager API, which is "
            "not enabled on this server. The server administrator must set "
            f"{data_manager.ENABLE_ENV_VAR}=true, enable the Data Manager API "
            "in the Google Cloud project and add its OAuth scope to the "
            "consent screen; users then sign in again."
        )


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def upload_offline_conversions(
    customer_id: str | int,
    conversion_action_id: str | int,
    conversions: List[OfflineConversion],
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Uploads offline conversions, for example enrollments from a CRM.

    Each conversion needs the Google click id (gclid, or gbraid/wbraid) or,
    for enhanced conversions for leads, the lead's email and/or phone, plus
    when it happened. Emails and phones are normalized and hashed with
    SHA-256 on the server; they are not logged or returned. Pass order_id
    (for example the CRM deal id) so the same conversion is not counted
    twice.

    The upload goes through the Data Manager API, which Google Ads requires
    for new integrations. Google processes it in the background: call
    get_conversion_upload_status with the returned request_id a few minutes
    later to see how many conversions were accepted and why any failed. A
    click can only receive conversions within the action's lookback window
    (90 days at most).

    Find conversion_action_id with create_offline_conversion_action or the
    search tool (conversion_action resource, type UPLOAD_CLICKS).

    By default the upload is only validated, not applied. Show the returned
    summary to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer.
        conversion_action_id: The id of the conversion action (conversion_action.id).
        conversions: 1 to 2000 conversions.
        validate_only: If true, validates the upload without applying it.
        login_customer_id: Optional manager customer ID that gives access to the customer.
    """
    return _upload(
        "upload_offline_conversions",
        customer_id,
        conversion_action_id,
        conversions,
        validate_only,
        login_customer_id,
    )


def _upload(
    tool_name: str,
    customer_id: str | int,
    conversion_action_id: str | int,
    conversions: List[OfflineConversion],
    validate_only: bool,
    login_customer_id: str | int | None,
) -> Dict[str, Any]:
    """Validates the conversions and sends them in one Data Manager request."""
    customer_id = _clean_id(customer_id, "customer_id")
    conversion_action_id = _clean_id(
        conversion_action_id, "conversion_action_id"
    )
    login_account_id = (
        _clean_id(login_customer_id, "login_customer_id")
        if login_customer_id
        else customer_id
    )
    if not 1 <= len(conversions) <= MAX_CONVERSIONS_PER_CALL:
        raise ToolError(
            f"conversions needs between 1 and {MAX_CONVERSIONS_PER_CALL} "
            f"conversions, got {len(conversions)}."
        )
    _require_data_manager()

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    row = _find_one(
        client,
        customer_id,
        "SELECT customer.currency_code, customer.time_zone, "
        "conversion_action.name, conversion_action.type FROM "
        "conversion_action WHERE conversion_action.id = "
        f"{conversion_action_id}",
        f"Conversion action {conversion_action_id} was not found in "
        f"customer {customer_id}.",
    )
    if row.conversion_action.type_.name != "UPLOAD_CLICKS":
        raise ToolError(
            f"Conversion action {conversion_action_id} has type "
            f"{row.conversion_action.type_.name}; uploads need an "
            "UPLOAD_CLICKS action, which create_offline_conversion_action "
            "creates."
        )

    events = []
    identified_by: Dict[str, int] = {}
    problems = []
    for index, conversion in enumerate(conversions):
        try:
            event, identifiers = _event(
                conversion,
                row.customer.time_zone,
                row.customer.currency_code,
            )
        except ToolError as ex:
            problems.append(f"conversion {index}: {ex}")
            continue
        events.append(event)
        for identifier in identifiers:
            identified_by[identifier] = identified_by.get(identifier, 0) + 1
    if problems:
        raise ToolError(
            f"Found {len(problems)} problems, nothing was sent to Google "
            "Ads:\n- " + "\n- ".join(problems[:30])
        )

    response = data_manager.call(
        "POST",
        "events:ingest",
        json={
            "destinations": [
                {
                    "operatingAccount": {
                        "accountType": "GOOGLE_ADS",
                        "accountId": customer_id,
                    },
                    "loginAccount": {
                        "accountType": "GOOGLE_ADS",
                        "accountId": login_account_id,
                    },
                    "productDestinationId": conversion_action_id,
                }
            ],
            "encoding": "HEX",
            "events": events,
            "validateOnly": validate_only,
        },
    )
    result = _result(
        tool_name,
        customer_id,
        validate_only,
        [
            {
                "conversion_action": row.conversion_action.name,
                "conversion_action_id": conversion_action_id,
                "conversions": len(events),
                "identified_by": identified_by,
            }
        ],
        [],
    )
    result["request_id"] = response.get("requestId")
    if response.get("fieldWarnings"):
        result["field_warnings"] = response["fieldWarnings"]
    if not validate_only:
        result["next_step"] = (
            "Google processes the upload in the background. Call "
            "get_conversion_upload_status with this request_id in a few "
            "minutes to see how many conversions were accepted."
        )
    return result


@mutate_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_conversion_upload_status(request_id: str) -> Dict[str, Any]:
    """Shows how Google processed an upload_offline_conversions request.

    Returns the status (PROCESSING, SUCCESS, PARTIAL_SUCCESS or FAILED), how
    many conversions were sent and, when some failed, how many failed for
    each reason (for example a click older than the lookback window or a
    click id from another account).

    Args:
        request_id: The request_id returned by upload_offline_conversions.
    """
    request_id = request_id.strip()
    if not _REQUEST_ID.match(request_id):
        raise ToolError(f"{request_id!r} is not a request id.")
    _require_data_manager()
    response = data_manager.call(
        "GET", "requestStatus:retrieve", params={"requestId": request_id}
    )
    statuses = []
    for entry in response.get("requestStatusPerDestination", []):
        destination = entry.get("destination", {})
        statuses.append(
            {
                "conversion_action_id": destination.get("productDestinationId"),
                "status": entry.get("requestStatus"),
                "conversions_sent": int(
                    entry.get("eventsIngestionStatus", {}).get("recordCount", 0)
                ),
                "errors": {
                    count.get("reason"): int(count.get("recordCount", 0))
                    for count in entry.get("errorInfo", {}).get(
                        "errorCounts", []
                    )
                },
                "warnings": {
                    count.get("reason"): int(count.get("recordCount", 0))
                    for count in entry.get("warningInfo", {}).get(
                        "warningCounts", []
                    )
                },
            }
        )
    return {"request_id": request_id, "destinations": statuses}
