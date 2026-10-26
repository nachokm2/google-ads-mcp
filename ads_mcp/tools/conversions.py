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
offline conversion uploads.

The tools are registered in the `mutate` namespace, so they are enabled
together with the other write tools.
"""

from typing import Any, Dict, Literal

from fastmcp.exceptions import ToolError

import ads_mcp.utils as utils
from ads_mcp.tools.create import _send_operations
from ads_mcp.tools.mutate import (
    _CREATE_ANNOTATIONS,
    _clean_id,
    _find_one,
    _result,
    mutate_mcp,
)

MAX_LOOKBACK_DAYS = 90

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
