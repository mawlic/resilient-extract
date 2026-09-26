from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

if "agent.web_search_provider" not in sys.modules:
    try:
        import agent.web_search_provider  # noqa: F401
    except ModuleNotFoundError:
        agent_module = types.ModuleType("agent")
        web_module = types.ModuleType("agent.web_search_provider")
        setattr(web_module, "WebSearchProvider", type("WebSearchProvider", (), {}))
        sys.modules.setdefault("agent", agent_module)
        sys.modules["agent.web_search_provider"] = web_module

try:
    import hermes_cli.config  # noqa: F401
except (ImportError, ModuleNotFoundError):
    hermes_cli_module = sys.modules.setdefault("hermes_cli", types.ModuleType("hermes_cli"))
    config_module = types.ModuleType("hermes_cli.config")
    setattr(config_module, "load_config", lambda: {})
    setattr(hermes_cli_module, "config", config_module)
    sys.modules["hermes_cli.config"] = config_module

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from provider import (
    BrowserFallbackAdapter,
    ResilientExtractProvider,
    load_plugin_settings,
)


class StubPrimary:
    name = "firecrawl"

    def is_available(self) -> bool:
        return True

    async def extract(self, urls, **kwargs):
        return [
            {
                "url": urls[0],
                "title": "",
                "content": "",
                "error": "SCRAPE_RETRY_LIMIT: document_antibot",
            }
        ]


class StubBrowser:
    def __init__(self):
        self.calls = []

    def extract_many(self, urls):
        self.calls.extend(urls)
        return {
            urls[0]: {
                "url": urls[0],
                "title": "Recovered",
                "content": "Recovered browser content",
            }
        }


def test_antibot_failure_falls_back_to_browser(caplog):
    browser = StubBrowser()
    provider = ResilientExtractProvider(
        primary_provider=StubPrimary(),
        browser_adapter=browser,
        max_browser_fallbacks_per_call=10,
    )

    with caplog.at_level(logging.WARNING):
        results = asyncio.run(provider.extract(["https://example.com/protected"]))

    assert browser.calls == ["https://example.com/protected"]
    assert results == [
        {
            "url": "https://example.com/protected",
            "title": "Recovered",
            "content": "Recovered browser content",
            "backend_used": "browser-use",
            "fallback_from": "firecrawl",
            "fallback_reason": "document_antibot",
            "attempted_backends": ["firecrawl", "browser-use"],
        }
    ]
    assert "outcome=success" in caplog.text
    assert "fallback_reason=document_antibot" in caplog.text


def test_typed_brd_retriable_error_is_fallback_candidate():
    result = {
        "url": "https://www.ozon.ru/search/",
        "title": "",
        "content": "",
        "error": "BRD_RETRIABLE 502: request failed",
    }

    assert ResilientExtractProvider._fallback_reason(result) == "brd_retriable"


def test_legitimate_content_that_mentions_captcha_is_not_rejected():
    result = {
        "url": "https://market.yandex.ru/card/product",
        "title": "Mini PC GMKtec",
        "content": "Product description and internal captcha compatibility note",
    }

    assert ResilientExtractProvider._fallback_reason(result) is None


def test_browser_adapter_reuses_one_session_and_cleans_it_up():
    navigations = []
    evaluations = []
    cleanups = []

    def navigate(url, task_id=None):
        navigations.append((url, task_id))
        return '{"success": true, "title": "Navigation title"}'

    def evaluate(*, expression=None, task_id=None, **_kwargs):
        evaluations.append((expression, task_id))
        url = navigations[-1][0]
        return (
            '{"success": true, "result": '
            '"{\\"title\\": \\"Browser title\\", '
            '\\"url\\": \\"' + url + '\\", '
            '\\"content\\": \\"Useful body text\\"}"}'
        )

    def cleanup(task_id=None):
        cleanups.append(task_id)

    adapter = BrowserFallbackAdapter(
        navigate=navigate,
        evaluate=evaluate,
        cleanup=cleanup,
    )

    results = adapter.extract_many([
        "https://example.com/one",
        "https://example.com/two",
    ])

    assert set(results) == {
        "https://example.com/one",
        "https://example.com/two",
    }
    assert all(item["content"] == "Useful body text" for item in results.values())
    task_ids = {task_id for _, task_id in navigations}
    assert len(task_ids) == 1
    assert {task_id for _, task_id in evaluations} == task_ids
    assert cleanups == [next(iter(task_ids))]


def test_browser_fallback_is_limited_to_ten_urls_and_marks_the_rest():
    urls = [f"https://example.com/protected-{index}" for index in range(12)]

    class BulkPrimary:
        name = "firecrawl"

        def is_available(self):
            return True

        async def extract(self, requested_urls, **_kwargs):
            return [
                {
                    "url": url,
                    "title": "",
                    "content": "",
                    "error": "document_antibot",
                }
                for url in requested_urls
            ]

    class BulkBrowser:
        def __init__(self):
            self.calls = []

        def extract_many(self, requested_urls):
            self.calls.extend(requested_urls)
            return {
                url: {"url": url, "title": "Recovered", "content": "Body"}
                for url in requested_urls
            }

    browser = BulkBrowser()
    provider = ResilientExtractProvider(
        primary_provider=BulkPrimary(),
        browser_adapter=browser,
        max_browser_fallbacks_per_call=10,
    )

    results = asyncio.run(provider.extract(urls))

    assert browser.calls == urls[:10]
    assert all("error" not in item for item in results[:10])
    assert all("fallback limit 10" in item["error"].lower() for item in results[10:])


def test_default_provider_resolves_firecrawl_from_registry(monkeypatch):
    primary = StubPrimary()
    browser = StubBrowser()

    monkeypatch.setattr(
        "agent.web_search_registry.get_provider",
        lambda name: primary if name == "firecrawl" else None,
    )
    provider = ResilientExtractProvider(
        browser_adapter=browser,
        max_browser_fallbacks_per_call=10,
    )

    results = asyncio.run(provider.extract(["https://example.com/protected"]))

    assert results[0]["title"] == "Recovered"
    assert browser.calls == ["https://example.com/protected"]


def test_browser_adapter_rejects_http_status_title():
    adapter = BrowserFallbackAdapter(
        navigate=lambda url, task_id=None: '{"success": true, "title": "403"}',
        evaluate=lambda **_kwargs: (
            '{"success": true, "result": '
            '"{\\"title\\": \\"403\\", '
            '\\"url\\": \\"https://example.com/blocked\\", '
            '\\"content\\": \\"Request could not be completed\\"}"}'
        ),
        cleanup=lambda task_id=None: None,
    )

    result = adapter.extract_many(["https://example.com/blocked"])[
        "https://example.com/blocked"
    ]

    assert "error" in result
    assert "http status" in result["error"].lower()


def test_browser_adapter_rejects_a_challenge_page():
    def navigate(url, task_id=None):
        return '{"success": true, "title": "Just a moment", "bot_detection_warning": "blocked"}'

    def evaluate(*, expression=None, task_id=None, **_kwargs):
        return (
            '{"success": true, "result": '
            '"{\\"title\\": \\"Just a moment\\", '
            '\\"url\\": \\"https://example.com/protected\\", '
            '\\"content\\": \\"Verify you are human\\"}"}'
        )

    adapter = BrowserFallbackAdapter(
        navigate=navigate,
        evaluate=evaluate,
        cleanup=lambda task_id=None: None,
    )

    result = adapter.extract_many(["https://example.com/protected"])

    assert "error" in result["https://example.com/protected"]
    assert "bot detection" in result["https://example.com/protected"]["error"].lower()


def test_plugin_settings_read_primary_and_limit_from_config(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "web": {
                "resilient_extract": {
                    "primary_backend": "firecrawl",
                    "max_browser_fallbacks_per_call": 10,
                }
            }
        },
    )

    assert load_plugin_settings() == {
        "primary_backend": "firecrawl",
        "secondary_backend": "",
        "prefer_secondary_domains": [],
        "max_browser_fallbacks_per_call": 10,
    }


def test_plugin_settings_read_secondary_routing_from_config(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "web": {
                "resilient_extract": {
                    "primary_backend": "firecrawl",
                    "secondary_backend": "brightdata-unlocker",
                    "prefer_secondary_domains": [
                        "market.yandex.ru",
                        "avito.ru",
                    ],
                    "max_browser_fallbacks_per_call": 7,
                }
            }
        },
    )

    settings = load_plugin_settings()

    assert settings["secondary_backend"] == "brightdata-unlocker"
    assert settings["prefer_secondary_domains"] == [
        "market.yandex.ru",
        "avito.ru",
    ]
    assert settings["max_browser_fallbacks_per_call"] == 7


def test_plugin_registers_resilient_extract_provider():
    entry_path = PLUGIN_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "resilient_extract_plugin",
        entry_path,
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    class Context:
        provider = None

        def register_web_search_provider(self, provider):
            self.provider = provider

    context = Context()
    module.register(context)

    assert context.provider is not None
    assert context.provider.name == "resilient-extract"
    assert context.provider.supports_extract()


def test_reddit_atom_uses_direct_free_backend_before_external_extractors():
    class Primary:
        def __init__(self):
            self.calls = []

        def is_available(self):
            return True

        async def extract(self, urls, **_kwargs):
            self.calls.extend(urls)
            return [{"url": url, "title": "Primary", "content": "page"} for url in urls]

    class DirectAtom:
        def __init__(self):
            self.calls = []

        def extract_many(self, urls):
            self.calls.extend(urls)
            return {
                url: {
                    "url": url,
                    "title": "Reddit Atom",
                    "content": "<feed><entry><title>fresh</title></entry></feed>",
                }
                for url in urls
            }

    primary = Primary()
    direct = DirectAtom()
    rss_url = "https://www.reddit.com/r/defi+CryptoCurrency/new/.rss?limit=30"
    normal_url = "https://example.com/page"
    provider = ResilientExtractProvider(
        primary_provider=primary,
        direct_atom_adapter=direct,
        browser_adapter=StubBrowser(),
    )

    results = asyncio.run(provider.extract([rss_url, normal_url]))

    assert direct.calls == [rss_url]
    assert primary.calls == [normal_url]
    assert results[0]["content"].startswith("<feed>")
    assert results[0]["backend_used"] == "direct-atom"
    assert results[0]["attempted_backends"] == ["direct-atom"]
    assert results[1]["backend_used"] == "firecrawl"


def test_browser_adapter_uses_current_lifecycle_cleanup_module(monkeypatch):
    browser_module = types.ModuleType("tools.browser_tool")
    setattr(browser_module, "browser_console", lambda **_kwargs: None)
    setattr(browser_module, "browser_navigate", lambda *_args, **_kwargs: None)
    lifecycle_module = types.ModuleType("tools.browser_tool_lifecycle")
    cleanup = lambda **_kwargs: None
    setattr(lifecycle_module, "cleanup_browser", cleanup)
    monkeypatch.setitem(sys.modules, "tools.browser_tool", browser_module)
    monkeypatch.setitem(sys.modules, "tools.browser_tool_lifecycle", lifecycle_module)

    adapter = BrowserFallbackAdapter()

    assert adapter._cleanup is cleanup


def test_browser_failure_is_reported_with_primary_error():
    class FailedBrowser:
        def extract_many(self, urls):
            return {
                url: {
                    "url": url,
                    "title": "",
                    "content": "",
                    "error": "Browser fallback failed: navigation timeout",
                }
                for url in urls
            }

    provider = ResilientExtractProvider(
        primary_provider=StubPrimary(),
        browser_adapter=FailedBrowser(),
        max_browser_fallbacks_per_call=10,
    )

    result = asyncio.run(provider.extract(["https://example.com/protected"]))[0]

    assert "document_antibot" in result["error"]
    assert "browser fallback failed" in result["error"].lower()


def test_browser_adapter_rejects_empty_document():
    adapter = BrowserFallbackAdapter(
        navigate=lambda url, task_id=None: '{"success": true, "title": ""}',
        evaluate=lambda **_kwargs: (
            '{"success": true, "result": '
            '"{\\"title\\": \\"\\", '
            '\\"url\\": \\"https://example.com/empty\\", '
            '\\"content\\": \\"   \\"}"}'
        ),
        cleanup=lambda task_id=None: None,
    )

    result = adapter.extract_many(["https://example.com/empty"])[
        "https://example.com/empty"
    ]

    assert "error" in result
    assert "empty" in result["error"].lower()


def test_protected_domain_prefers_secondary_backend():
    class RecordingProvider:
        def __init__(self, name, title):
            self.name = name
            self.title = title
            self.calls = []

        def is_available(self):
            return True

        def supports_extract(self):
            return True

        async def extract(self, urls, **_kwargs):
            self.calls.extend(urls)
            return [
                {"url": url, "title": self.title, "content": f"{self.title} body"}
                for url in urls
            ]

    primary = RecordingProvider("firecrawl", "Primary")
    secondary = RecordingProvider("brightdata-unlocker", "Unlocker")
    browser = StubBrowser()
    provider = ResilientExtractProvider(
        primary_provider=primary,
        secondary_provider=secondary,
        secondary_backend="brightdata-unlocker",
        prefer_secondary_domains=["market.yandex.ru"],
        browser_adapter=browser,
    )

    result = asyncio.run(
        provider.extract(["https://market.yandex.ru/card/product"])
    )[0]

    assert primary.calls == []
    assert secondary.calls == ["https://market.yandex.ru/card/product"]
    assert browser.calls == []
    assert result["backend_used"] == "brightdata-unlocker"
    assert result["attempted_backends"] == ["brightdata-unlocker"]


def test_protected_domain_unsupported_by_secondary_uses_primary():
    class UnsupportedSecondary:
        name = "brightdata-unlocker"

        def is_available(self):
            return True

        def supports_extract(self):
            return True

        async def extract(self, urls, **_kwargs):
            return [
                {
                    "url": url,
                    "title": "",
                    "content": "",
                    "error": "BRD_PERMANENT 400: this endpoint is not supported",
                }
                for url in urls
            ]

    class SuccessfulPrimary:
        name = "firecrawl"

        def __init__(self):
            self.calls = []

        def is_available(self):
            return True

        def supports_extract(self):
            return True

        async def extract(self, urls, **_kwargs):
            self.calls.extend(urls)
            return [
                {"url": url, "title": "Primary", "content": "Primary content"}
                for url in urls
            ]

    primary = SuccessfulPrimary()
    provider = ResilientExtractProvider(
        primary_provider=primary,
        secondary_provider=UnsupportedSecondary(),
        secondary_backend="brightdata-unlocker",
        prefer_secondary_domains=["travel.yandex.ru"],
    )

    result = asyncio.run(
        provider.extract(["https://travel.yandex.ru/hotels/moscow/"])
    )[0]

    assert primary.calls == ["https://travel.yandex.ru/hotels/moscow/"]
    assert result["backend_used"] == "firecrawl"
    assert result["fallback_from"] == "brightdata-unlocker"
    assert result["fallback_reason"] == "provider_unsupported"


def test_primary_captcha_falls_back_to_secondary_before_browser():
    class SuccessfulSecondary:
        name = "brightdata-unlocker"

        def __init__(self):
            self.calls = []

        def is_available(self):
            return True

        def supports_extract(self):
            return True

        async def extract(self, urls, **_kwargs):
            self.calls.extend(urls)
            return [
                {"url": url, "title": "Unlocked", "content": "Useful unlocked page"}
                for url in urls
            ]

    secondary = SuccessfulSecondary()
    browser = StubBrowser()
    provider = ResilientExtractProvider(
        primary_provider=StubPrimary(),
        secondary_provider=secondary,
        secondary_backend="brightdata-unlocker",
        browser_adapter=browser,
    )

    result = asyncio.run(provider.extract(["https://example.com/protected"]))[0]

    assert secondary.calls == ["https://example.com/protected"]
    assert browser.calls == []
    assert result["backend_used"] == "brightdata-unlocker"
    assert result["fallback_from"] == "firecrawl"
    assert result["fallback_reason"] == "document_antibot"
    assert result["attempted_backends"] == ["firecrawl", "brightdata-unlocker"]


def test_permanent_secondary_error_stops_before_browser():
    class PermanentSecondary:
        name = "brightdata-unlocker"

        def is_available(self):
            return True

        def supports_extract(self):
            return True

        async def extract(self, urls, **_kwargs):
            return [
                {
                    "url": url,
                    "title": "",
                    "content": "",
                    "error": "BRD_PERMANENT 400: endpoint is not supported",
                }
                for url in urls
            ]

    browser = StubBrowser()
    provider = ResilientExtractProvider(
        primary_provider=StubPrimary(),
        secondary_provider=PermanentSecondary(),
        secondary_backend="brightdata-unlocker",
        browser_adapter=browser,
    )

    result = asyncio.run(provider.extract(["https://example.com/protected"]))[0]

    assert browser.calls == []
    assert result["backend_used"] == "brightdata-unlocker"
    assert "BRD_PERMANENT 400" in result["error"]
    assert result["attempted_backends"] == ["firecrawl", "brightdata-unlocker"]


def test_primary_successful_captcha_page_falls_back_to_browser():
    class CaptchaPrimary:
        name = "firecrawl"

        def is_available(self):
            return True

        async def extract(self, urls, **_kwargs):
            return [
                {
                    "url": urls[0],
                    "title": "Вы не робот?",
                    "content": "Подтвердите, что запросы отправляли вы, а не робот",
                }
            ]

    browser = StubBrowser()
    provider = ResilientExtractProvider(
        primary_provider=CaptchaPrimary(),
        browser_adapter=browser,
    )

    result = asyncio.run(provider.extract(["https://market.yandex.ru/card/1"]))[0]

    assert browser.calls == ["https://market.yandex.ru/card/1"]
    assert result["backend_used"] == "browser-use"
    assert result["fallback_reason"] == "bot_challenge_content"


def test_primary_success_has_backend_telemetry():
    class SuccessfulPrimary:
        def is_available(self):
            return True

        def supports_extract(self):
            return True

        async def extract(self, urls, **_kwargs):
            return [
                {"url": urls[0], "title": "Primary", "content": "Primary body"}
            ]

    provider = ResilientExtractProvider(primary_provider=SuccessfulPrimary())
    result = asyncio.run(provider.extract(["https://example.com/primary"]))[0]

    assert result["backend_used"] == "firecrawl"
    assert result["fallback_from"] is None
    assert result["fallback_reason"] is None
