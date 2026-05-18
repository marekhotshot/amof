from __future__ import annotations

import builtins
import importlib
import sys
import tomllib
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


class PackagingMetadataTests(unittest.TestCase):
    def test_pyproject_declares_requests_runtime_dependency(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = pyproject["project"]["dependencies"]

        self.assertIn("requests>=2.28.0", dependencies)


class ProfileCatalogImportTests(unittest.TestCase):
    def test_profile_catalog_import_does_not_eagerly_import_runpod(self) -> None:
        real_import = builtins.__import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "amof.api.services.runpod_heavy_lane":
                raise AssertionError("profile_catalog imported runpod eagerly")
            return real_import(name, globals, locals, fromlist, level)

        sys.modules.pop("amof.orchestrator.llm.profile_catalog", None)
        try:
            with patch("builtins.__import__", side_effect=guarded_import):
                module = importlib.import_module("amof.orchestrator.llm.profile_catalog")
        finally:
            sys.modules.pop("amof.orchestrator.llm.profile_catalog", None)

        self.assertTrue(hasattr(module, "get_profile_catalog"))


class BedrockAnthropicClientTests(unittest.TestCase):
    def test_get_client_uses_aws_ca_bundle_when_other_tls_env_vars_are_unset(self) -> None:
        from amof.orchestrator.llm.bedrock_anthropic import BedrockAnthropicClient

        captured_httpx_verify: list[str] = []
        captured_kwargs: dict[str, object] = {}

        class FakeHTTPXClient:
            def __init__(self, *, verify):
                captured_httpx_verify.append(verify)

        class FakeAnthropicBedrock:
            def __init__(self, **kwargs):
                captured_kwargs.update(kwargs)

        fake_anthropic = types.SimpleNamespace(AnthropicBedrock=FakeAnthropicBedrock)
        fake_httpx = types.SimpleNamespace(Client=FakeHTTPXClient)

        with patch.dict(
            sys.modules,
            {"anthropic": fake_anthropic, "httpx": fake_httpx},
            clear=False,
        ):
            with patch.dict(
                "os.environ",
                {
                    "AWS_REGION": "eu-central-1",
                    "AWS_CA_BUNDLE": "/tmp/corp-ca.pem",
                },
                clear=False,
            ):
                client = BedrockAnthropicClient(model="eu.anthropic.claude-haiku-4-5-20251001-v1:0")
                sdk_client = client._get_client()

        self.assertIsInstance(sdk_client, FakeAnthropicBedrock)
        self.assertEqual(captured_httpx_verify, ["/tmp/corp-ca.pem"])
        self.assertIsInstance(captured_kwargs.get("http_client"), FakeHTTPXClient)

    def test_wrap_provider_error_mentions_tls_env_guidance_for_network_failures(self) -> None:
        from amof.orchestrator.llm.base import PROVIDER_FAILURE_NETWORK
        from amof.orchestrator.llm.bedrock_anthropic import BedrockAnthropicClient

        class ConnectError(Exception):
            pass

        client = BedrockAnthropicClient(
            model="eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            aws_region="eu-central-1",
        )
        wrapped = client._wrap_provider_error(ConnectError("TLS handshake failed"))

        self.assertEqual(wrapped.failure_class, PROVIDER_FAILURE_NETWORK)
        self.assertIn("SSL_CERT_FILE", str(wrapped))
        self.assertIn("REQUESTS_CA_BUNDLE", str(wrapped))
        self.assertIn("AWS_CA_BUNDLE", str(wrapped))


if __name__ == "__main__":
    unittest.main()
