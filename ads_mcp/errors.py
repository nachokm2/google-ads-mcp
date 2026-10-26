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

"""Formats Google Ads API errors for tool responses.

Some API errors come without a message, so the error code and the field path
are included to tell the agent what failed.
"""

from typing import Dict, List

from google.ads.googleads.errors import GoogleAdsException


def _pb(message):
    """Returns the protobuf message behind a proto-plus message."""
    return type(message).pb(message) if hasattr(message, "_pb") else message


def _error_code(error) -> str | None:
    """Returns the error code as 'category: NAME', if it can be read."""
    try:
        error_code = _pb(error.error_code)
        field = error_code.WhichOneof("error_code")
        if not field:
            return None
        enum_type = error_code.DESCRIPTOR.fields_by_name[field].enum_type
        name = enum_type.values_by_number[getattr(error_code, field)].name
        return f"{field}: {name}"
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _field_path(error) -> str:
    try:
        return ".".join(
            element.field_name
            + (f"[{element.index}]" if _pb(element).HasField("index") else "")
            for element in error.location.field_path_elements
        )
    except (AttributeError, TypeError, ValueError):
        return ""


def describe_error(error) -> str:
    """Describes one GoogleAdsError as 'message (code) at field path'."""
    parts = []
    if error.message:
        parts.append(str(error.message))
    code = _error_code(error)
    if code:
        parts.append(f"({code})")
    path = _field_path(error)
    if path:
        parts.append(f"at {path}")
    return " ".join(parts) or "Unknown error"


def partial_failure_errors(client, status) -> Dict[int, List[str]]:
    """Maps the index of each failed operation to its error descriptions.

    Requests sent with partial_failure=True return the failures of single
    operations in a google.rpc.Status instead of raising an exception.
    """
    failures: Dict[int, List[str]] = {}
    if not status or not status.code:
        return failures
    failure_type = type(client.get_type("GoogleAdsFailure"))
    for detail in status.details:
        failure = failure_type.deserialize(detail.value)
        for error in failure.errors:
            elements = error.location.field_path_elements
            index = elements[0].index if elements else -1
            failures.setdefault(index, []).append(describe_error(error))
    return failures


def _transport_error(ex: GoogleAdsException) -> str | None:
    """Returns the gRPC status and details, used when no failure is parsed."""
    try:
        details = ex.error.details()
        status = ex.error.code().name
    except (AttributeError, TypeError, ValueError):
        return None
    if not details and not status:
        return None
    return f"{details or 'No details'} (grpc status: {status})"


def format_google_ads_exception(ex: GoogleAdsException) -> str:
    lines = [f"Request ID: {ex.request_id}"]
    errors = list(ex.failure.errors) if ex.failure else []
    lines.extend(
        f"Google Ads API Error: {describe_error(error)}" for error in errors
    )
    if not errors:
        transport_error = _transport_error(ex)
        if transport_error:
            lines.append(f"Google Ads API Error: {transport_error}")
    return "\n".join(lines)
