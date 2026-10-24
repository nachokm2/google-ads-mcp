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

"""Restricts which Google accounts can use the server over HTTP.

With the OAuth proxy enabled, any Google account allowed by the OAuth consent
screen can sign in. Setting GOOGLE_ADS_MCP_ALLOWED_EMAILS limits the tools,
resources and prompts to the listed emails and @domains. STDIO is not
affected, since it has no signed-in user.
"""

import logging
import os

from fastmcp import FastMCP
from fastmcp.server.middleware.authorization import AuthMiddleware
from fastmcp.utilities.authorization import AuthCheck, AuthContext

logger = logging.getLogger(__name__)

ALLOWED_EMAILS_ENV_VAR = "GOOGLE_ADS_MCP_ALLOWED_EMAILS"


def parse_allowed_emails(value: str | None) -> frozenset[str]:
    """Parses a comma or whitespace separated list of emails and @domains."""
    if not value:
        return frozenset()
    return frozenset(entry.lower() for entry in value.replace(",", " ").split())


def is_email_allowed(email: str, allowed: frozenset[str]) -> bool:
    """Returns whether the email, or its @domain, is in the allowed set."""
    email = email.lower()
    domain = "@" + email.rpartition("@")[2]
    return email in allowed or domain in allowed


def make_email_check(allowed: frozenset[str]) -> AuthCheck:
    """Builds an auth check that only accepts verified, allowed emails."""
    denied_logged: set[str] = set()

    def check(ctx: AuthContext) -> bool:
        claims = (ctx.token.claims if ctx.token else None) or {}
        email = claims.get("email")
        # Google reports the flag as a bool or as the string "true".
        verified = str(claims.get("email_verified")).lower() == "true"
        if email and verified and is_email_allowed(email, allowed):
            return True

        user = email or "an unauthenticated user"
        if user not in denied_logged:
            denied_logged.add(user)
            logger.warning(
                f"Denied access for {user} (email verified: {verified}); "
                f"add it to {ALLOWED_EMAILS_ENV_VAR} to allow it."
            )
        return False

    return check


def add_access_control(server: FastMCP) -> bool:
    """Restricts the server to GOOGLE_ADS_MCP_ALLOWED_EMAILS, if it is set.

    Returns:
        True when the restriction was added, otherwise False.
    """
    allowed = parse_allowed_emails(os.environ.get(ALLOWED_EMAILS_ENV_VAR))
    if not allowed:
        logger.warning(
            f"{ALLOWED_EMAILS_ENV_VAR} is not set: any Google account that "
            "can sign in may use this server."
        )
        return False

    server.add_middleware(AuthMiddleware(auth=make_email_check(allowed)))
    logger.info(
        f"Access restricted to {len(allowed)} allowed emails or domains."
    )
    return True
