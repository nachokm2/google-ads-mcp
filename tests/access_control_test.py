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

"""Test cases for restricting the server to allowed emails."""

import os
import unittest
from unittest.mock import MagicMock, patch

from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken
from mcp.server.auth.provider import AccessToken as SDKAccessToken

from ads_mcp import access_control


def _token(email, verified=True):
    claims = {"email": email, "email_verified": verified} if email else {}
    return AccessToken(
        token="fake", client_id="client", scopes=[], claims=claims
    )


def _ctx(token):
    return MagicMock(token=token)


class TestAllowedEmails(unittest.TestCase):

    def test_parse_allowed_emails(self):
        self.assertEqual(
            access_control.parse_allowed_emails(
                " Ana@Gmail.com, @uautonoma.cl  bob@x.org,,"
            ),
            frozenset({"ana@gmail.com", "@uautonoma.cl", "bob@x.org"}),
        )
        self.assertEqual(access_control.parse_allowed_emails(None), frozenset())
        self.assertEqual(access_control.parse_allowed_emails(" "), frozenset())

    def test_is_email_allowed(self):
        allowed = frozenset({"ana@gmail.com", "@uautonoma.cl"})

        self.assertTrue(
            access_control.is_email_allowed("ANA@gmail.com", allowed)
        )
        self.assertTrue(
            access_control.is_email_allowed(
                "rodrigo.palma@uautonoma.cl", allowed
            )
        )
        self.assertFalse(
            access_control.is_email_allowed("bob@gmail.com", allowed)
        )
        self.assertFalse(
            access_control.is_email_allowed(
                "eve@uautonoma.cl.evil.com", allowed
            )
        )

    def test_email_check(self):
        check = access_control.make_email_check(frozenset({"ana@gmail.com"}))

        self.assertTrue(check(_ctx(_token("ana@gmail.com"))))
        self.assertTrue(check(_ctx(_token("ana@gmail.com", verified="true"))))
        self.assertFalse(check(_ctx(_token("ana@gmail.com", verified=False))))
        self.assertFalse(check(_ctx(_token("ana@gmail.com", verified=None))))
        self.assertFalse(check(_ctx(_token("bob@gmail.com"))))
        self.assertFalse(check(_ctx(_token(None))))
        self.assertFalse(check(_ctx(None)))
        # MCP SDK tokens may carry no claims at all.
        self.assertFalse(
            check(
                _ctx(
                    SDKAccessToken(token="fake", client_id="client", scopes=[])
                )
            )
        )

    def test_denied_email_is_logged_once(self):
        check = access_control.make_email_check(frozenset({"ana@gmail.com"}))

        with self.assertLogs(access_control.logger, "WARNING") as logs:
            check(_ctx(_token("bob@gmail.com")))
            check(_ctx(_token("bob@gmail.com")))

        self.assertEqual(len(logs.records), 1)
        self.assertIn("bob@gmail.com", logs.output[0])


class TestAddAccessControl(unittest.IsolatedAsyncioTestCase):

    def make_server(self):
        server = FastMCP("Test Server")

        @server.tool
        def ping() -> str:
            return "pong"

        return server

    async def call_as(self, server, token):
        with patch(
            "fastmcp.server.middleware.authorization.get_access_token",
            return_value=token,
        ):
            async with Client(server) as client:
                tools = [t.name for t in await client.list_tools()]
                result = await client.call_tool("ping")
        return tools, result.data

    async def test_no_restriction_without_env_var(self):
        server = self.make_server()

        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(access_control.add_access_control(server))

        tools, result = await self.call_as(server, None)
        self.assertEqual(tools, ["ping"])
        self.assertEqual(result, "pong")

    async def test_allowed_email_can_use_tools(self):
        server = self.make_server()

        with patch.dict(
            os.environ,
            {access_control.ALLOWED_EMAILS_ENV_VAR: "@uautonoma.cl"},
        ):
            self.assertTrue(access_control.add_access_control(server))

        tools, result = await self.call_as(
            server, _token("rodrigo.palma@uautonoma.cl")
        )
        self.assertEqual(tools, ["ping"])
        self.assertEqual(result, "pong")

    async def test_other_email_cannot_see_or_call_tools(self):
        server = self.make_server()

        with patch.dict(
            os.environ,
            {access_control.ALLOWED_EMAILS_ENV_VAR: "ana@gmail.com"},
        ):
            access_control.add_access_control(server)

        with self.assertRaises(ToolError):
            await self.call_as(server, _token("bob@gmail.com"))

        with patch(
            "fastmcp.server.middleware.authorization.get_access_token",
            return_value=_token("bob@gmail.com"),
        ):
            async with Client(server) as client:
                self.assertEqual(await client.list_tools(), [])


if __name__ == "__main__":
    unittest.main()
