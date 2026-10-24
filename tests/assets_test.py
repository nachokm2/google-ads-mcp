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

"""Test cases for loading image and video assets."""

import struct
import unittest
from unittest.mock import MagicMock, patch

from ads_mcp import assets


def png(width, height):
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def gif(width, height):
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def jpeg(width, height):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof0 = (
        b"\xff\xc0"
        + struct.pack(">HBHH", 17, 8, height, width)
        + b"\x03"
        + b"\x00" * 9
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def _addresses(ip):
    return [(2, 1, 6, "", (ip, 443))]


class TestImageSize(unittest.TestCase):

    def test_reads_png_gif_and_jpeg_sizes(self):
        self.assertEqual(assets.image_size(png(1200, 628)), (1200, 628))
        self.assertEqual(assets.image_size(gif(300, 300)), (300, 300))
        self.assertEqual(assets.image_size(jpeg(960, 1200)), (960, 1200))

    def test_rejects_other_formats(self):
        for data in (b"RIFF....WEBPVP8 ", b"", b"\xff\xd8\xff\xe0"):
            with self.subTest(data=data):
                with self.assertRaises(assets.AssetError):
                    assets.image_size(data)

    def test_check_image_size(self):
        assets.check_image_size(1200, 628, "MARKETING_IMAGE")
        assets.check_image_size(600, 314, "MARKETING_IMAGE")
        assets.check_image_size(1200, 1200, "SQUARE_MARKETING_IMAGE")
        assets.check_image_size(960, 1200, "PORTRAIT_MARKETING_IMAGE")
        assets.check_image_size(128, 128, "LOGO")
        assets.check_image_size(1200, 300, "LANDSCAPE_LOGO")

        with self.assertRaisesRegex(assets.AssetError, "1.91:1"):
            assets.check_image_size(1200, 1200, "MARKETING_IMAGE")
        with self.assertRaisesRegex(assets.AssetError, "at least 128x128"):
            assets.check_image_size(100, 100, "LOGO")
        with self.assertRaisesRegex(assets.AssetError, "4:5"):
            assets.check_image_size(1200, 628, "PORTRAIT_MARKETING_IMAGE")


class TestYoutubeVideoId(unittest.TestCase):

    def test_accepts_ids_and_urls(self):
        for value in (
            "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10",
            "https://youtu.be/dQw4w9WgXcQ?si=abc",
            "https://youtube.com/shorts/dQw4w9WgXcQ",
            "https://m.youtube.com/embed/dQw4w9WgXcQ",
        ):
            with self.subTest(value=value):
                self.assertEqual(assets.youtube_video_id(value), "dQw4w9WgXcQ")

    def test_rejects_other_values(self):
        for value in (
            "https://vimeo.com/123",
            "https://www.youtube.com/watch?v=short",
            "https://www.youtube.com/@canal",
            "no es un video",
        ):
            with self.subTest(value=value):
                with self.assertRaises(assets.AssetError):
                    assets.youtube_video_id(value)


class TestDownloadImage(unittest.TestCase):

    def fake_opener(self, data):
        response = MagicMock()
        response.__enter__.return_value.read.side_effect = lambda size: data[
            :size
        ]
        opener = MagicMock()
        opener.open.return_value = response
        return opener

    @patch("ads_mcp.assets.socket.getaddrinfo")
    @patch("ads_mcp.assets.urllib.request.build_opener")
    def test_downloads_public_https_images(self, build_opener, getaddrinfo):
        getaddrinfo.return_value = _addresses("142.250.0.1")
        build_opener.return_value = self.fake_opener(png(1200, 628))

        data = assets.download_image(" https://www.uautonoma.cl/img.png ")

        self.assertEqual(assets.image_size(data), (1200, 628))
        request = build_opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://www.uautonoma.cl/img.png")

    @patch("ads_mcp.assets.socket.getaddrinfo")
    def test_rejects_non_https_and_private_urls(self, getaddrinfo):
        with self.assertRaisesRegex(assets.AssetError, "https://"):
            assets.download_image("http://www.uautonoma.cl/img.png")
        getaddrinfo.assert_not_called()

        for ip in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "::1"):
            with self.subTest(ip=ip):
                getaddrinfo.return_value = _addresses(ip)
                with self.assertRaisesRegex(assets.AssetError, "non-public"):
                    assets.download_image("https://interno.example/img.png")

    @patch("ads_mcp.assets.socket.getaddrinfo")
    def test_redirects_are_checked_too(self, getaddrinfo):
        getaddrinfo.return_value = _addresses("10.0.0.5")
        handler = assets._PublicRedirectHandler()

        with self.assertRaises(assets.AssetError):
            handler.redirect_request(
                MagicMock(), None, 302, "Found", {}, "https://interno/img.png"
            )

    @patch("ads_mcp.assets.socket.getaddrinfo")
    @patch("ads_mcp.assets.urllib.request.build_opener")
    def test_rejects_images_over_5_mb(self, build_opener, getaddrinfo):
        getaddrinfo.return_value = _addresses("142.250.0.1")
        build_opener.return_value = self.fake_opener(
            b"x" * (assets.MAX_IMAGE_BYTES + 1)
        )

        with self.assertRaisesRegex(assets.AssetError, "5 MB"):
            assets.download_image("https://www.uautonoma.cl/grande.png")


if __name__ == "__main__":
    unittest.main()
