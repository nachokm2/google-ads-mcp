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

"""Helpers to load image and YouTube video assets for Google Ads.

Images are downloaded from public https:// URLs only. The server may run next
to private services (for example on Railway), so URLs that resolve to
private, loopback or link-local addresses are rejected, including after
redirects.
"""

import ipaddress
import re
import socket
import struct
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

MAX_IMAGE_BYTES = 5120 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 15
# Google Ads accepts images whose aspect ratio is within 1% of the required one.
ASPECT_RATIO_TOLERANCE = 0.01


class AssetError(ValueError):
    """Raised when an image or a video cannot be used as an asset."""


@dataclass(frozen=True)
class ImageSpec:
    aspect_ratio: float
    ratio_label: str
    min_width: int
    min_height: int


IMAGE_SPECS = {
    "MARKETING_IMAGE": ImageSpec(1.91, "1.91:1", 600, 314),
    "SQUARE_MARKETING_IMAGE": ImageSpec(1.0, "1:1", 300, 300),
    "PORTRAIT_MARKETING_IMAGE": ImageSpec(0.8, "4:5", 480, 600),
    "LOGO": ImageSpec(1.0, "1:1", 128, 128),
    "LANDSCAPE_LOGO": ImageSpec(4.0, "4:1", 512, 128),
}

# JPEG start-of-frame markers, which carry the image size.
_JPEG_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}  # fmt: skip


def image_size(data: bytes) -> tuple[int, int]:
    """Returns the (width, height) of a PNG, GIF or JPEG image."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data.startswith(b"\xff\xd8"):
        position = 2
        while position + 9 <= len(data):
            if data[position] != 0xFF:
                position += 1
                continue
            marker = data[position + 1]
            if marker == 0xFF:
                position += 1
                continue
            if marker == 0x01 or 0xD0 <= marker <= 0xD9:
                position += 2
                continue
            if marker in _JPEG_SOF_MARKERS:
                height, width = struct.unpack(
                    ">HH", data[position + 5 : position + 9]
                )
                return width, height
            (length,) = struct.unpack(">H", data[position + 2 : position + 4])
            position += 2 + length
    raise AssetError("Only PNG, JPEG and GIF images are supported.")


def check_image_size(width: int, height: int, field_type: str) -> None:
    """Checks an image against the aspect ratio and minimum size of its use."""
    spec = IMAGE_SPECS[field_type]
    if not width or not height:
        raise AssetError("The image has no size.")
    ratio = width / height
    if (
        abs(ratio - spec.aspect_ratio) / spec.aspect_ratio
        > ASPECT_RATIO_TOLERANCE
    ):
        raise AssetError(
            f"{field_type} images must have a {spec.ratio_label} aspect "
            f"ratio, but this one is {width}x{height}."
        )
    if width < spec.min_width or height < spec.min_height:
        raise AssetError(
            f"{field_type} images must be at least "
            f"{spec.min_width}x{spec.min_height}, but this one is "
            f"{width}x{height}."
        )


def _check_public_https_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise AssetError(f"Image URLs must start with https://, got {url!r}.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror as ex:
        raise AssetError(f"Could not resolve {parsed.hostname}: {ex}")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise AssetError(
                f"{parsed.hostname} resolves to the non-public address {ip}; "
                "only public images can be downloaded."
            )


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Applies the same URL checks to every redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_image(url: str) -> bytes:
    """Downloads an image from a public https:// URL."""
    url = url.strip()
    _check_public_https_url(url)
    opener = urllib.request.build_opener(_PublicRedirectHandler)
    request = urllib.request.Request(
        url, headers={"User-Agent": "google-ads-mcp"}
    )
    try:
        with opener.open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            data = response.read(MAX_IMAGE_BYTES + 1)
    except (urllib.error.URLError, OSError) as ex:
        raise AssetError(f"Could not download {url}: {ex}")
    if len(data) > MAX_IMAGE_BYTES:
        raise AssetError(f"{url} is larger than 5 MB.")
    return data


_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def youtube_video_id(value: str) -> str:
    """Returns the video id of a YouTube URL, or the value if it is an id."""
    value = value.strip()
    if _YOUTUBE_ID.match(value):
        return value
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").removeprefix("www.").removeprefix("m.")
    candidate = ""
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
    elif host == "youtube.com":
        parts = parsed.path.strip("/").split("/")
        if parts[0] == "watch":
            candidate = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) > 1 and parts[0] in ("shorts", "embed", "live", "v"):
            candidate = parts[1]
    if not _YOUTUBE_ID.match(candidate):
        raise AssetError(f"{value!r} is not a YouTube video id or URL.")
    return candidate
