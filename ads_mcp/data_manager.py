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

"""Minimal client for the Google Data Manager API.

Google Ads only accepts offline conversion uploads from new integrations
through the Data Manager API. It needs its own OAuth scope, which the server
requests when GOOGLE_ADS_MCP_DATA_MANAGER is true, and the API must be
enabled in the Google Cloud project of the OAuth client.
"""

import os
from typing import Any, Dict

from fastmcp.exceptions import ToolError

DATA_MANAGER_SCOPE = "https://www.googleapis.com/auth/datamanager"
ENABLE_ENV_VAR = "GOOGLE_ADS_MCP_DATA_MANAGER"
BASE_URL = "https://datamanager.googleapis.com/v1"
TIMEOUT_SECONDS = 30


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV_VAR, "").lower() in ("true", "1", "yes")


def _credentials():
    """Returns the signed-in user's credentials, or Application Default ones."""
    from fastmcp.server.dependencies import get_access_token
    from google.oauth2.credentials import Credentials

    token = get_access_token()
    if token and token.token:
        return Credentials(token=token.token)

    import google.auth

    from ads_mcp.utils import prevent_stdio_inheritance

    with prevent_stdio_inheritance():
        credentials, _ = google.auth.default(scopes=[DATA_MANAGER_SCOPE])
    return credentials


def _describe_error(response) -> str:
    try:
        error = response.json().get("error", {})
    except ValueError:
        return (
            f"Data Manager API error {response.status_code}: "
            f"{response.text[:500]}"
        )
    lines = [
        f"Data Manager API error {response.status_code} "
        f"({error.get('status', 'UNKNOWN')}): {error.get('message', '')}"
    ]
    for detail in error.get("details", []):
        for violation in detail.get("fieldViolations", []):
            lines.append(
                f"- {violation.get('field', '?')}: "
                f"{violation.get('description', '')}"
            )
        if detail.get("reason"):
            lines.append(f"- reason: {detail['reason']}")
    if response.status_code in (401, 403):
        lines.append(
            "Check that the Data Manager API is enabled in the Google Cloud "
            "project and that the user signed in granting the Data Manager "
            "permission (sign in again if it was added recently)."
        )
    return "\n".join(lines)


def call(
    method: str,
    path: str,
    *,
    json: Dict[str, Any] | None = None,
    params: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    """Calls a Data Manager API method and returns its JSON response."""
    import requests
    from google.auth.transport.requests import AuthorizedSession

    session = AuthorizedSession(_credentials())
    try:
        response = session.request(
            method,
            f"{BASE_URL}/{path}",
            json=json,
            params=params,
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as ex:
        raise ToolError(f"Could not reach the Data Manager API: {ex}")
    if response.status_code >= 400:
        raise ToolError(_describe_error(response))
    return response.json() if response.content else {}
