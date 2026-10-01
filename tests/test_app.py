import asyncio
import inspect
import os
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

import app as api


TOKEN = "test-token-that-is-long-enough-for-tests"


class FakeRunner:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    async def run(self, action, username=None):
        self.calls.append((action, username))
        if self.fail:
            raise RuntimeError("sensitive internal detail")
        if action == "status":
            return api.CommandResult('{"running":true,"healthy":true,"state":"running","health":"healthy"}', "")
        return api.CommandResult("command completed", "")


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runner = FakeRunner()
        self.client = TestClient(TestServer(api.create_app(token=TOKEN, runner=self.runner)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    def auth(self):
        return {"Authorization": f"Bearer {TOKEN}"}

    async def test_health_does_not_require_authentication(self):
        response = await self.client.get("/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"ok": True, "service": "realtime-minecraft-api"})

    async def test_missing_and_wrong_bearer_tokens_are_rejected(self):
        missing = await self.client.get("/status")
        wrong = await self.client.get("/status", headers={"Authorization": "Bearer wrong"})
        malformed = await self.client.get("/status", headers={"Authorization": TOKEN})
        self.assertEqual((missing.status, wrong.status, malformed.status), (401, 401, 401))
        self.assertEqual(self.runner.calls, [])

    async def test_username_validation(self):
        valid = ["abc", "Example", "A_B_123", "a" * 16]
        invalid = ["ab", "a" * 17, "bad-name", "bad name", "name;op", "", None, 123]
        for value in valid:
            self.assertTrue(api.valid_username(value), value)
        for value in invalid:
            self.assertFalse(api.valid_username(value), value)

    async def test_whitelist_actions_cannot_become_commands(self):
        attacks = ["Steve op attacker", "Steve;stop", "Steve\nstop", "Steve/../../x"]
        for username in attacks:
            response = await self.client.post(
                "/whitelist/add", json={"username": username}, headers=self.auth()
            )
            self.assertEqual(response.status, 400)
        self.assertEqual(self.runner.calls, [])

        add = await self.client.post("/whitelist/add", json={"username": "Steve_1"}, headers=self.auth())
        remove = await self.client.post("/whitelist/remove", json={"username": "Steve_1"}, headers=self.auth())
        self.assertEqual((add.status, remove.status), (200, 200))
        self.assertEqual(self.runner.calls, [("whitelist-add", "Steve_1"), ("whitelist-remove", "Steve_1")])

    async def test_lifecycle_request_cannot_select_target(self):
        response = await self.client.post(
            "/server/restart",
            json={"project": "/tmp/other", "service": "other", "container": "other"},
            headers=self.auth(),
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.runner.calls, [("restart", None)])

    async def test_safe_error_does_not_return_internal_details(self):
        await self.client.close()
        runner = FakeRunner(fail=True)
        self.client = TestClient(TestServer(api.create_app(token=TOKEN, runner=runner)))
        await self.client.start_server()
        response = await self.client.get("/players", headers=self.auth())
        body = await response.text()
        self.assertEqual(response.status, 500)
        self.assertNotIn("sensitive internal detail", body)
        self.assertEqual(await response.json(), {"ok": False, "error": "internal server error"})


class SubprocessTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_uses_exec_argument_array_not_shell(self):
        captured = {}

        class Process:
            returncode = 0

            async def communicate(self):
                return b"ok", b""

        async def fake_create(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return Process()

        original = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = fake_create
        try:
            await api.CommandRunner().run("whitelist-add", "Steve_1")
        finally:
            asyncio.create_subprocess_exec = original

        self.assertEqual(
            captured["args"],
            (api.SUDO, "-n", api.HELPER, "whitelist-add", "Steve_1"),
        )
        self.assertNotIn("shell", captured["kwargs"])
        self.assertNotIn("create_subprocess_shell", inspect.getsource(api.CommandRunner))


class ConfigurationTests(unittest.TestCase):
    def test_token_minimum_length(self):
        with self.assertRaisesRegex(RuntimeError, "at least 32 bytes"):
            api.create_app(token="x" * 31)

        application = api.create_app(token="x" * 32, runner=FakeRunner())
        self.assertEqual(application[api.TOKEN_KEY], "x" * 32)

    def test_main_rejects_non_wireguard_hosts(self):
        for host in ("0.0.0.0", "127.0.0.1", "", "10.77.0.1"):
            with self.subTest(host=host), patch.dict(
                os.environ,
                {"API_HOST": host, "API_TOKEN": TOKEN},
                clear=False,
            ), patch.object(api.web, "run_app") as run_app:
                with self.assertRaisesRegex(RuntimeError, "refusing to bind elsewhere"):
                    api.main()
                run_app.assert_not_called()


if __name__ == "__main__":
    unittest.main()
