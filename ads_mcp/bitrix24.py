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

"""Minimal read client for the Bitrix24 CRM REST API.

The server reads won deals and their contacts from Bitrix24 through an
inbound webhook, so emails and phones go straight from the CRM to Google
(hashed) without passing through the chat. The webhook URL is a secret: it is
read from GOOGLE_ADS_MCP_BITRIX24_WEBHOOK_URL and never logged or returned.
"""

import os
import time
from typing import Any, Dict, List

from fastmcp.exceptions import ToolError

WEBHOOK_ENV_VAR = "GOOGLE_ADS_MCP_BITRIX24_WEBHOOK_URL"
TIMEOUT_SECONDS = 60
PAGE_SIZE = 50
MAX_ITEMS = 10000
_RETRIES = 3


def configured() -> bool:
    return bool(os.environ.get(WEBHOOK_ENV_VAR, "").strip())


def _webhook_url() -> str:
    url = os.environ.get(WEBHOOK_ENV_VAR, "").strip()
    if not url:
        raise ToolError(
            "Bitrix24 is not connected to this server. The server "
            f"administrator must create an inbound webhook with the CRM "
            f"permission in Bitrix24 and set {WEBHOOK_ENV_VAR} to its URL."
        )
    if not url.startswith("https://"):
        raise ToolError(f"{WEBHOOK_ENV_VAR} must be an https:// URL.")
    return url if url.endswith("/") else url + "/"


def call(method: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Calls a Bitrix24 REST method and returns its JSON response.

    Retries when Bitrix24 asks the client to slow down.
    """
    import requests

    url = f"{_webhook_url()}{method}.json"
    for attempt in range(_RETRIES):
        try:
            response = requests.post(
                url, json=params or {}, timeout=TIMEOUT_SECONDS
            )
        except requests.RequestException as ex:
            # The exception text includes the URL, which holds the secret.
            raise ToolError(
                f"Could not reach Bitrix24 ({type(ex).__name__})."
            ) from None
        try:
            body = response.json()
        except ValueError:
            raise ToolError(
                f"Bitrix24 returned HTTP {response.status_code} without JSON."
            )
        error = body.get("error") if isinstance(body, dict) else None
        if error == "QUERY_LIMIT_EXCEEDED" and attempt < _RETRIES - 1:
            time.sleep(2**attempt)
            continue
        if error or response.status_code >= 400:
            raise ToolError(
                f"Bitrix24 error in {method}: {error or response.status_code}"
                f" {body.get('error_description', '')}".rstrip()
            )
        return body
    raise ToolError(f"Bitrix24 kept rejecting {method} for too many requests.")


def list_all(
    method: str, params: Dict[str, Any], max_items: int = MAX_ITEMS
) -> List[Dict[str, Any]]:
    """Returns every item of a Bitrix24 list method, following its pages."""
    items: List[Dict[str, Any]] = []
    start = 0
    while True:
        body = call(method, {**params, "start": start})
        result = body.get("result") or []
        items.extend(result)
        if len(items) > max_items:
            raise ToolError(
                f"{method} returned more than {max_items} items. Use a "
                "shorter period or fewer pipelines."
            )
        if "next" not in body or not result:
            return items
        start = body["next"]
