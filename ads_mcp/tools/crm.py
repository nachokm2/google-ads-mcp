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

"""Tools that send Bitrix24 CRM data to Google Ads.

sync_crm_conversions uploads the won deals (for example enrollments) of the
chosen pipelines as offline conversions, identified by the contact's email
and phone. The server reads the CRM through its own webhook and hashes the
contact data before it leaves, so emails and phones never pass through the
chat and are never logged or returned.

The tools are registered in the `mutate` namespace, so they are enabled
together with the other write tools.
"""

import datetime
import re
from typing import Any, Dict, List

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp import bitrix24, data_manager
from ads_mcp.tools.conversions import (
    MAX_CONVERSIONS_PER_CALL,
    MAX_LOOKBACK_DAYS,
    OfflineConversion,
    _hash,
    _normalize_email,
    _normalize_phone,
    _require_data_manager,
    _upload,
)
from ads_mcp.tools.create import _check_date, _send_operations
from ads_mcp.tools.mutate import (
    _CREATE_ANNOTATIONS,
    _clean_id,
    _find_one,
    _result,
    mutate_mcp,
)

MAX_PIPELINES = 20
MAX_MEMBERSHIP_DAYS = 540
MAX_MEMBERS_PER_REQUEST = 10000
_CONTACTS_PER_REQUEST = 50
_MAX_SKIPPED_IDS = 20
_DIGITS = re.compile(r"\d+")


def _check_pipeline_ids(pipeline_ids: List[int]) -> List[int]:
    cleaned = list(dict.fromkeys(pipeline_ids))
    if not 1 <= len(cleaned) <= MAX_PIPELINES:
        raise ToolError(
            f"pipeline_ids needs between 1 and {MAX_PIPELINES} pipelines."
        )
    if any(pipeline_id < 0 for pipeline_id in cleaned):
        raise ToolError("pipeline_ids cannot be negative.")
    return cleaned


def _check_country_code(code: str | None) -> str | None:
    if code is None:
        return None
    digits = code.strip().lstrip("+")
    if not re.fullmatch(r"[1-9]\d{0,2}", digits):
        raise ToolError(
            "default_phone_country_code must be a calling code such as 56, "
            f"got {code!r}."
        )
    return digits


def _period(start_date: str, end_date: str | None):
    """Validates the period and returns its first and last day."""
    start = _check_date(start_date, "start_date")
    end = _check_date(end_date, "end_date") or datetime.date.today()
    if end < start:
        raise ToolError("end_date cannot be before start_date.")
    oldest = datetime.date.today() - datetime.timedelta(days=MAX_LOOKBACK_DAYS)
    if start < oldest:
        raise ToolError(
            f"start_date cannot be more than {MAX_LOOKBACK_DAYS} days ago "
            f"({oldest}); Google ignores conversions of older clicks."
        )
    return start, end


def _crm_values(field: Any) -> List[str]:
    """Returns the values of a Bitrix24 multi-field such as EMAIL or PHONE."""
    if not isinstance(field, list):
        return []
    return [
        str(item.get("VALUE", "")).strip()
        for item in field
        if isinstance(item, dict) and str(item.get("VALUE", "")).strip()
    ]


def _crm_email(contact: Dict[str, Any]) -> str | None:
    for value in _crm_values(contact.get("EMAIL")):
        try:
            return _normalize_email(value)
        except ToolError:
            continue
    return None


def _crm_phone(
    contact: Dict[str, Any], default_country_code: str | None
) -> str | None:
    """Returns the first phone that can be written in E.164 format.

    Phones without a leading + or 00 get default_country_code, if given.
    """
    for value in _crm_values(contact.get("PHONE")):
        digits = "".join(_DIGITS.findall(value))
        if value.startswith("+"):
            candidate = f"+{digits}"
        elif digits.startswith("00"):
            candidate = f"+{digits[2:]}"
        elif default_country_code:
            if not digits.startswith(default_country_code):
                digits = default_country_code + digits.lstrip("0")
            candidate = f"+{digits}"
        else:
            continue
        try:
            return _normalize_phone(candidate)
        except ToolError:
            continue
    return None


def _deals(
    pipeline_ids: List[int],
    only_won: bool,
    start: datetime.date | None = None,
    end: datetime.date | None = None,
) -> List[Dict[str, Any]]:
    """Returns the deals of the pipelines, optionally only the won ones.

    For won deals the period applies to when they reached the won stage;
    for all deals, to when they were created.
    """
    date_field = "MOVED_TIME" if only_won else "DATE_CREATE"
    deal_filter: Dict[str, Any] = {"CATEGORY_ID": pipeline_ids}
    if only_won:
        deal_filter["STAGE_SEMANTIC_ID"] = "S"
    if start:
        deal_filter[f">={date_field}"] = start.isoformat()
    if end:
        deal_filter[f"<{date_field}"] = (
            end + datetime.timedelta(days=1)
        ).isoformat()
    return bitrix24.list_all(
        "crm.deal.list",
        {
            "filter": deal_filter,
            "select": [
                "ID",
                "CATEGORY_ID",
                "CONTACT_ID",
                "MOVED_TIME",
                "OPPORTUNITY",
                "CURRENCY_ID",
            ],
            "order": {"ID": "ASC"},
        },
    )


def _deal_contacts(
    deals: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Returns the email and phone fields of the deals' contacts by id."""
    contact_ids = sorted(
        {
            str(deal["CONTACT_ID"])
            for deal in deals
            if deal.get("CONTACT_ID") not in (None, "", "0", 0)
        }
    )
    contacts: Dict[str, Dict[str, Any]] = {}
    for index in range(0, len(contact_ids), _CONTACTS_PER_REQUEST):
        batch = contact_ids[index : index + _CONTACTS_PER_REQUEST]
        for contact in bitrix24.list_all(
            "crm.contact.list",
            {
                "filter": {"ID": batch},
                "select": ["ID", "EMAIL", "PHONE"],
            },
        ):
            contacts[str(contact.get("ID"))] = contact
    return contacts


def _deal_value(deal: Dict[str, Any]) -> tuple[float | None, str | None]:
    try:
        value = float(deal.get("OPPORTUNITY") or 0)
    except ValueError:
        return None, None
    if value <= 0:
        return None, None
    return value, (deal.get("CURRENCY_ID") or None)


@mutate_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_crm_pipelines() -> Dict[str, Any]:
    """Lists the Bitrix24 deal pipelines and the stage that counts as won.

    Use the ids with sync_crm_conversions. A deal counts as a conversion
    when it reaches the won stage of its pipeline (for example
    "Matriculado").
    """
    # Deal stages are statuses whose ENTITY_ID is DEAL_STAGE for the default
    # pipeline and DEAL_STAGE_<id> for the others.
    won_stages = {}
    for status in bitrix24.list_all("crm.status.list", {}):
        entity = str(status.get("ENTITY_ID", ""))
        if not entity.startswith("DEAL_STAGE"):
            continue
        if status.get("SEMANTICS") == "S" or str(
            status.get("STATUS_ID", "")
        ).endswith("WON"):
            pipeline_id = entity.removeprefix("DEAL_STAGE").lstrip("_") or "0"
            won_stages[int(pipeline_id)] = status.get("NAME")

    pipelines = []
    for category in (
        bitrix24.call("crm.category.list", {"entityTypeId": 2})
        .get("result", {})
        .get("categories", [])
    ):
        pipeline_id = int(category.get("id", 0))
        pipelines.append(
            {
                "pipeline_id": pipeline_id,
                "name": category.get("name"),
                "won_stage": won_stages.get(pipeline_id),
            }
        )
    return {"pipelines": pipelines}


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def sync_crm_conversions(
    customer_id: str | int,
    conversion_action_id: str | int,
    pipeline_ids: List[int],
    start_date: str,
    end_date: str | None = None,
    include_value: bool = True,
    default_phone_country_code: str | None = None,
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Uploads the won deals of Bitrix24 pipelines as offline conversions.

    Reads the deals that reached the won stage (for example "Matriculado")
    between start_date and end_date in the given pipelines, takes the email
    and phone of each deal's contact and uploads them as enhanced
    conversions for leads, at the moment the deal was won. Google matches
    them with the ad clicks of those people. Each deal is sent with its id
    as order_id, so running the sync again over the same days does not
    count anything twice.

    The server reads Bitrix24 and hashes the contact data itself: emails and
    phones never pass through the chat and are not returned. The account
    must have enhanced conversions for leads turned on and the customer data
    terms accepted (Google Ads > Goals > Settings). Find pipeline ids with
    list_crm_pipelines and the conversion action with
    create_offline_conversion_action.

    By default the upload is only validated, not applied. Show the returned
    summary to the user and call again with validate_only=false only after
    they confirm. Then check the result with get_conversion_upload_status.

    Args:
        customer_id: The id of the Google Ads customer.
        conversion_action_id: The id of an UPLOAD_CLICKS conversion action.
        pipeline_ids: The Bitrix24 deal pipelines (CATEGORY_ID) to read.
        start_date: First day, as YYYY-MM-DD, at most 90 days ago.
        end_date: Last day, as YYYY-MM-DD; defaults to today.
        include_value: Whether to send the deal amount as the conversion value.
        default_phone_country_code: Calling code, for example 56, for CRM phones saved without one.
        validate_only: If true, validates the upload without applying it.
        login_customer_id: Optional manager customer ID that gives access to the customer.
    """
    pipeline_ids = _check_pipeline_ids(pipeline_ids)
    country_code = _check_country_code(default_phone_country_code)
    start, end = _period(start_date, end_date)

    deals = _deals(pipeline_ids, True, start, end)
    contacts = _deal_contacts(deals)

    conversions = []
    with_email = with_phone = 0
    skipped = []
    for deal in deals:
        contact = contacts.get(str(deal.get("CONTACT_ID")), {})
        email = _crm_email(contact)
        phone = _crm_phone(contact, country_code)
        if not email and not phone:
            skipped.append(str(deal.get("ID")))
            continue
        with_email += bool(email)
        with_phone += bool(phone)
        value, currency = _deal_value(deal) if include_value else (None, None)
        conversions.append(
            OfflineConversion(
                email=email,
                phone=phone,
                conversion_date_time=deal["MOVED_TIME"],
                value=value,
                currency=currency,
                order_id=f"bitrix24-deal-{deal['ID']}",
            )
        )

    crm_summary = {
        "pipelines": pipeline_ids,
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "won_deals": len(deals),
        "conversions": len(conversions),
        "with_email": with_email,
        "with_phone": with_phone,
        "skipped_without_contact_data": len(skipped),
    }
    if skipped:
        crm_summary["skipped_deal_ids"] = skipped[:_MAX_SKIPPED_IDS]
    if not conversions:
        return {
            "applied": False,
            "message": "There are no won deals with contact data to upload.",
            "crm": crm_summary,
        }

    results = [
        _upload(
            "sync_crm_conversions",
            customer_id,
            conversion_action_id,
            conversions[index : index + MAX_CONVERSIONS_PER_CALL],
            validate_only,
            login_customer_id,
        )
        for index in range(0, len(conversions), MAX_CONVERSIONS_PER_CALL)
    ]
    result = results[0]
    if len(results) > 1:
        result["request_ids"] = [item.get("request_id") for item in results]
        result.pop("request_id", None)
    result["crm"] = crm_summary
    return result


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def create_customer_match_list(
    customer_id: str | int,
    name: str,
    description: str | None = None,
    membership_days: int = MAX_MEMBERSHIP_DAYS,
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Creates a Customer Match audience to fill with contacts from the CRM.

    Use it, for example, for "Matriculados" to exclude current students from
    campaigns, or as a seed for similar audiences. Fill it with
    sync_crm_audience. Google needs at least 100 matched members before the
    list can be used for targeting, and the account must meet the Customer
    Match policy requirements.

    By default the list is only validated, not created. Show the returned
    summary to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the customer.
        name: The name of the audience, unique in the account.
        description: Optional description.
        membership_days: How many days a member stays in the list, 1 to 540.
        validate_only: If true, validates the list without creating it.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    name = " ".join(name.split())
    if not 1 <= len(name) <= 255:
        raise ToolError("name must have between 1 and 255 characters.")
    if not 1 <= membership_days <= MAX_MEMBERSHIP_DAYS:
        raise ToolError(
            f"membership_days must be between 1 and {MAX_MEMBERSHIP_DAYS}."
        )

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    operation = client.get_type("MutateOperation")
    user_list = operation.user_list_operation.create
    user_list.name = name
    if description:
        user_list.description = " ".join(description.split())
    user_list.membership_status = client.enums.UserListMembershipStatusEnum.OPEN
    user_list.membership_life_span = membership_days
    user_list.crm_based_user_list.upload_key_type = (
        client.enums.CustomerMatchUploadKeyTypeEnum.CONTACT_INFO
    )
    user_list.crm_based_user_list.data_source_type = (
        client.enums.UserListCrmDataSourceTypeEnum.FIRST_PARTY
    )

    resource_names = _send_operations(
        client, customer_id, [operation], validate_only
    )
    result = _result(
        "create_customer_match_list",
        customer_id,
        validate_only,
        [
            {
                "name": name,
                "type": "CRM_BASED (contact info)",
                "membership_days": membership_days,
            }
        ],
        resource_names,
    )
    if resource_names:
        result["user_list_id"] = resource_names[0].rsplit("/", 1)[1]
    return result


@mutate_mcp.tool(annotations=_CREATE_ANNOTATIONS)
def sync_crm_audience(
    customer_id: str | int,
    user_list_id: str | int,
    pipeline_ids: List[int],
    only_won_deals: bool = True,
    since_date: str | None = None,
    accept_customer_match_terms: bool = False,
    default_phone_country_code: str | None = None,
    validate_only: bool = True,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Adds the contacts of Bitrix24 deals to a Customer Match audience.

    Reads the deals of the pipelines (only the won ones by default, for
    example enrolled students), takes the email and phone of each contact
    and adds them to the audience created with create_customer_match_list.
    Adding someone who is already in the list does nothing. The server reads
    Bitrix24 and hashes the contact data itself: emails and phones never
    pass through the chat and are not returned.

    Customer Match requires accepting Google's Customer Match terms on
    behalf of the advertiser: ask the user and pass
    accept_customer_match_terms=true only after they agree. Google takes up
    to 48 hours to match the members.

    By default the upload is only validated, not applied. Show the returned
    summary to the user and call again with validate_only=false only after
    they confirm.

    Args:
        customer_id: The id of the Google Ads customer.
        user_list_id: The id of a Customer Match audience (user_list.id).
        pipeline_ids: The Bitrix24 deal pipelines (CATEGORY_ID) to read.
        only_won_deals: If true, only contacts of won deals; otherwise of every deal.
        since_date: Optional YYYY-MM-DD; only deals won (or created, with only_won_deals=false) since then.
        accept_customer_match_terms: Whether the user accepted Google's Customer Match terms.
        default_phone_country_code: Calling code, for example 56, for CRM phones saved without one.
        validate_only: If true, validates the upload without applying it.
        login_customer_id: Optional manager customer ID that gives access to the customer.
    """
    customer_id = _clean_id(customer_id, "customer_id")
    user_list_id = _clean_id(user_list_id, "user_list_id")
    login_account_id = (
        _clean_id(login_customer_id, "login_customer_id")
        if login_customer_id
        else customer_id
    )
    pipeline_ids = _check_pipeline_ids(pipeline_ids)
    country_code = _check_country_code(default_phone_country_code)
    since = _check_date(since_date, "since_date")
    if not accept_customer_match_terms:
        raise ToolError(
            "Uploading to a Customer Match audience requires accepting "
            "Google's Customer Match terms "
            "(https://support.google.com/adspolicy/answer/6299717). Ask the "
            "user and call again with accept_customer_match_terms=true only "
            "if they accept them."
        )
    _require_data_manager()

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    row = _find_one(
        client,
        customer_id,
        "SELECT user_list.name, user_list.type FROM user_list WHERE "
        f"user_list.id = {user_list_id}",
        f"Audience {user_list_id} was not found in customer {customer_id}.",
    )
    if row.user_list.type_.name != "CRM_BASED":
        raise ToolError(
            f"Audience {user_list_id} has type {row.user_list.type_.name}; "
            "contacts can only be added to a Customer Match audience, which "
            "create_customer_match_list creates."
        )

    deals = _deals(pipeline_ids, only_won_deals, since)
    contacts = _deal_contacts(deals)
    members = []
    seen = set()
    with_email = with_phone = without_data = 0
    for contact in contacts.values():
        email = _crm_email(contact)
        phone = _crm_phone(contact, country_code)
        if not email and not phone:
            without_data += 1
            continue
        if (email, phone) in seen:
            continue
        seen.add((email, phone))
        identifiers = []
        if email:
            identifiers.append({"emailAddress": _hash(email)})
            with_email += 1
        if phone:
            identifiers.append({"phoneNumber": _hash(phone)})
            with_phone += 1
        members.append({"userData": {"userIdentifiers": identifiers}})

    crm_summary = {
        "pipelines": pipeline_ids,
        "only_won_deals": only_won_deals,
        "since": since.isoformat() if since else None,
        "deals": len(deals),
        "contacts": len(contacts),
        "members": len(members),
        "with_email": with_email,
        "with_phone": with_phone,
        "contacts_without_data": without_data,
    }
    if not members:
        return {
            "applied": False,
            "message": "There are no contacts with an email or phone to add.",
            "crm": crm_summary,
        }

    request_ids = []
    for index in range(0, len(members), MAX_MEMBERS_PER_REQUEST):
        response = data_manager.call(
            "POST",
            "audienceMembers:ingest",
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
                        "productDestinationId": user_list_id,
                    }
                ],
                "audienceMembers": members[
                    index : index + MAX_MEMBERS_PER_REQUEST
                ],
                "encoding": "HEX",
                "termsOfService": {
                    "customerMatchTermsOfServiceStatus": "ACCEPTED"
                },
                "validateOnly": validate_only,
            },
        )
        request_ids.append(response.get("requestId"))

    result = _result(
        "sync_crm_audience",
        customer_id,
        validate_only,
        [
            {
                "audience": row.user_list.name,
                "user_list_id": user_list_id,
                "members": len(members),
            }
        ],
        [],
    )
    result["request_ids"] = request_ids
    result["crm"] = crm_summary
    if not validate_only:
        result["next_step"] = (
            "Google matches the members within 48 hours. The audience can "
            "be used for targeting once it has at least 100 matched members. "
            "Check each request_id with get_conversion_upload_status."
        )
    return result
