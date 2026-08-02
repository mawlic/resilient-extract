from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from agent.web_search_provider import WebSearchProvider


logger = logging.getLogger(__name__)


_RETRYABLE_ERROR_MARKERS = (
    "brd_retriable",
    "document_antibot",
    "scrape_retry_limit",
    "captcha",
    "cloudflare",
    "challenge",
    "access denied",
    "bot detected",
    "verification required",
    "checking your browser",
    "all scraping engines failed",
    "timeout",
    "timed out",
    "http 403",
    "status 403",
    "http 429",
    "status 429",
)


_BOT_PAGE_MARKERS = (
    "just a moment",
    "verify you are human",
    "verification required",
    "are you a robot",
    "checking your browser",
    "attention required",
    "access denied",
    "bot detected",
    "вы не робот",
    "подтвердите, что запросы отправляли вы",
    "yandex smartcaptcha",
    "доступ к сайту временно ограничен владельцем веб-ресурса",
)


def load_plugin_settings() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        raw = ((load_config().get("web") or {}).get("resilient_extract") or {})
    except Exception:
        raw = {}
    primary = str(raw.get("primary_backend") or "firecrawl").strip().lower()
    secondary = str(raw.get("secondary_backend") or "").strip().lower()
    domains_raw = raw.get("prefer_secondary_domains") or []
    if isinstance(domains_raw, str):
        domains_raw = domains_raw.split(",")
    domains = [
        str(domain).strip().lower().lstrip(".")
        for domain in domains_raw
        if str(domain).strip()
    ]
    try:
        limit = int(raw.get("max_browser_fallbacks_per_call", 10))
    except (TypeError, ValueError):
        limit = 10
    return {
        "primary_backend": primary,
        "secondary_backend": secondary,
        "prefer_secondary_domains": domains,
        "max_browser_fallbacks_per_call": max(0, min(limit, 50)),
    }


class BrowserFallbackAdapter:
    _EXPRESSION = "JSON.stringify({title:document.title||'',url:location.href,content:(document.body&&document.body.innerText)||''})"

    def __init__(self, *, navigate=None, evaluate=None, cleanup=None) -> None:
        if navigate is None or evaluate is None or cleanup is None:
            from tools.browser_tool import browser_console, browser_navigate, cleanup_browser

            navigate = navigate or browser_navigate
            evaluate = evaluate or browser_console
            cleanup = cleanup or cleanup_browser
        self._navigate = navigate
        self._evaluate = evaluate
        self._cleanup = cleanup

    @staticmethod
    def _decode_payload(raw: str) -> Dict[str, Any]:
        outer = json.loads(raw)
        if not outer.get("success"):
            raise RuntimeError(str(outer.get("error") or "Browser operation failed"))
        result = outer.get("result")
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            raise RuntimeError("Browser evaluation returned no document payload")
        return result

    def extract_many(self, urls: List[str]) -> Dict[str, Dict[str, Any]]:
        task_id = f"resilient-extract-{uuid.uuid4().hex}"
        recovered: Dict[str, Dict[str, Any]] = {}
        try:
            for url in urls:
                try:
                    navigation = json.loads(self._navigate(url, task_id=task_id))
                    if not navigation.get("success"):
                        raise RuntimeError(str(navigation.get("error") or "Navigation failed"))
                    if navigation.get("bot_detection_warning"):
                        raise RuntimeError("Browser fallback still hit bot detection")
                    payload = self._decode_payload(
                        self._evaluate(expression=self._EXPRESSION, task_id=task_id)
                    )
                    page_text = " ".join(
                        (str(payload.get("title") or ""), str(payload.get("content") or ""))
                    ).lower()
                    page_title = str(payload.get("title") or "").strip().lower()
                    if not str(payload.get("content") or "").strip():
                        raise RuntimeError("Browser fallback returned empty content")
                    if page_title in {"401", "403", "407", "429", "502", "503"}:
                        raise RuntimeError(
                            f"Browser fallback returned HTTP status page {page_title}"
                        )
                    if any(marker in page_text for marker in _BOT_PAGE_MARKERS):
                        raise RuntimeError("Browser fallback still hit bot detection")
                    recovered[url] = {
                        "url": str(payload.get("url") or url),
                        "title": str(payload.get("title") or navigation.get("title") or ""),
                        "content": str(payload.get("content") or ""),
                    }
                except Exception as exc:
                    recovered[url] = {
                        "url": url,
                        "title": "",
                        "content": "",
                        "error": f"Browser fallback failed: {exc}",
                    }
        finally:
            self._cleanup(task_id=task_id)
        return recovered


class ResilientExtractProvider(WebSearchProvider):
    def __init__(
        self,
        *,
        primary_provider=None,
        primary_backend: str = "firecrawl",
        secondary_provider=None,
        secondary_backend: str = "",
        prefer_secondary_domains: Optional[List[str]] = None,
        browser_adapter=None,
        max_browser_fallbacks_per_call: int = 10,
    ) -> None:
        self._primary_provider = primary_provider
        self._primary_backend = primary_backend
        self._secondary_provider = secondary_provider
        self._secondary_backend = secondary_backend
        self._prefer_secondary_domains = tuple(
            domain.strip().lower().lstrip(".")
            for domain in (prefer_secondary_domains or [])
            if domain.strip()
        )
        self._browser_adapter = browser_adapter
        self._max_browser_fallbacks_per_call = max(0, int(max_browser_fallbacks_per_call))

    @property
    def name(self) -> str:
        return "resilient-extract"

    @property
    def display_name(self) -> str:
        return "Resilient Extract (Firecrawl → Browser Use)"

    def _resolve_primary_provider(self):
        if self._primary_provider is not None:
            return self._primary_provider
        if self._primary_backend == self.name:
            return None
        from agent.web_search_registry import get_provider

        return get_provider(self._primary_backend)

    def _resolve_secondary_provider(self):
        if self._secondary_provider is not None:
            return self._secondary_provider
        if not self._secondary_backend or self._secondary_backend == self.name:
            return None
        from agent.web_search_registry import get_provider

        return get_provider(self._secondary_backend)

    def _prefers_secondary(self, url: str) -> bool:
        hostname = (urlparse(url).hostname or "").lower()
        return any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in self._prefer_secondary_domains
        )

    @staticmethod
    async def _call_provider(provider, urls: List[str], **kwargs):
        method = provider.extract
        if inspect.iscoroutinefunction(method):
            return await method(urls, **kwargs)
        return await asyncio.to_thread(method, urls, **kwargs)

    def is_available(self) -> bool:
        primary = self._resolve_primary_provider()
        if primary is None:
            return False
        try:
            return bool(primary.is_available() and primary.supports_extract())
        except AttributeError:
            return bool(primary.is_available())

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    @staticmethod
    def _fallback_reason(result: Dict[str, Any]) -> Optional[str]:
        page_text = " ".join(
            (
                str(result.get("title") or ""),
                str(result.get("content") or ""),
            )
        ).lower()
        if any(marker in page_text for marker in _BOT_PAGE_MARKERS):
            return "bot_challenge_content"
        error = str(result.get("error") or "").lower()
        for marker in _RETRYABLE_ERROR_MARKERS:
            if marker in error:
                return marker
        if not error and not str(result.get("content") or "").strip():
            return "empty_content"
        return None

    @classmethod
    def _needs_browser_fallback(cls, result: Dict[str, Any]) -> bool:
        return cls._fallback_reason(result) is not None

    @classmethod
    def _backend_fallback_reason(cls, result: Dict[str, Any]) -> Optional[str]:
        reason = cls._fallback_reason(result)
        if reason is not None:
            return reason
        error = str(result.get("error") or "").lower()
        if error.startswith("brd_permanent") and "endpoint is not supported" in error:
            return "provider_unsupported"
        return None

    async def extract(self, urls: List[str], **kwargs) -> List[Dict[str, Any]]:
        primary_provider = self._resolve_primary_provider()
        if primary_provider is None:
            return [
                {
                    "url": url,
                    "title": "",
                    "content": "",
                    "error": f"Primary extract backend '{self._primary_backend}' is unavailable",
                }
                for url in urls
            ]
        secondary_provider = self._resolve_secondary_provider()
        primary_indexes = []
        secondary_indexes = []
        for index, url in enumerate(urls):
            if secondary_provider is not None and self._prefers_secondary(url):
                secondary_indexes.append(index)
            else:
                primary_indexes.append(index)

        pending_results: List[Optional[Dict[str, Any]]] = [None] * len(urls)
        if primary_indexes:
            primary_results = await self._call_provider(
                primary_provider,
                [urls[index] for index in primary_indexes],
                **kwargs,
            )
            for index, item in zip(primary_indexes, primary_results):
                item.setdefault("backend_used", self._primary_backend)
                pending_results[index] = item
        if secondary_indexes:
            secondary_results = await self._call_provider(
                secondary_provider,
                [urls[index] for index in secondary_indexes],
                **kwargs,
            )
            for index, item in zip(secondary_indexes, secondary_results):
                item.setdefault("backend_used", self._secondary_backend)
                pending_results[index] = item

        completed_results: List[Dict[str, Any]] = []
        for index, item in enumerate(pending_results):
            result = item or {
                "url": urls[index],
                "title": "",
                "content": "",
                "error": "Extraction backend returned no result",
                "backend_used": self._primary_backend,
            }
            result.setdefault("attempted_backends", [result["backend_used"]])
            completed_results.append(result)
        results = completed_results

        for item in results:
            item.setdefault("fallback_from", None)
            item.setdefault("fallback_reason", None)

        backend_fallback_groups = []
        primary_fallbacks = []
        secondary_fallbacks = []
        for index, item in enumerate(results):
            reason = self._backend_fallback_reason(item)
            if reason is None:
                continue
            used = str(item.get("backend_used") or "")
            if used == self._primary_backend and secondary_provider is not None:
                primary_fallbacks.append((index, reason))
            elif used == self._secondary_backend and primary_provider is not None:
                secondary_fallbacks.append((index, reason))
        if primary_fallbacks:
            backend_fallback_groups.append(
                (secondary_provider, self._secondary_backend, primary_fallbacks)
            )
        if secondary_fallbacks:
            backend_fallback_groups.append(
                (primary_provider, self._primary_backend, secondary_fallbacks)
            )

        for fallback_provider, fallback_backend, indexed_reasons in backend_fallback_groups:
            fallback_urls = [urls[index] for index, _reason in indexed_reasons]
            fallback_results = await self._call_provider(
                fallback_provider, fallback_urls, **kwargs
            )
            for (index, original_reason), fallback_result in zip(
                indexed_reasons, fallback_results
            ):
                original = results[index]
                original_backend = str(original.get("backend_used") or "")
                original_error = str(original.get("error") or "").strip()
                fallback_result.setdefault("backend_used", fallback_backend)
                fallback_result["fallback_from"] = original_backend
                fallback_result["fallback_reason"] = original_reason
                fallback_result["attempted_backends"] = [
                    *original.get("attempted_backends", [original_backend]),
                    fallback_backend,
                ]
                if fallback_result.get("error") and original_error:
                    current_error = str(fallback_result["error"])
                    if current_error.startswith(("BRD_PERMANENT", "BRD_AUTH")):
                        metadata = fallback_result.setdefault("metadata", {})
                        metadata["previous_error"] = original_error
                    else:
                        fallback_result["error"] = (
                            f"{original_error}; {fallback_backend}: {current_error}"
                        )
                results[index] = fallback_result

        all_candidates = [
            (
                index,
                item.get("url") or urls[index],
                self._fallback_reason(item),
            )
            for index, item in enumerate(results)
            if self._needs_browser_fallback(item)
        ]
        candidates = all_candidates[: self._max_browser_fallbacks_per_call]
        skipped = all_candidates[self._max_browser_fallbacks_per_call :]
        logger.info(
            "Resilient extract primary=%s urls=%d retryable=%d browser_attempts=%d skipped=%d",
            self._primary_backend,
            len(urls),
            len(all_candidates),
            len(candidates),
            len(skipped),
        )
        for index, _url, reason in skipped:
            original = str(results[index].get("error") or "Extraction returned empty content")
            results[index]["error"] = (
                f"{original}; browser fallback skipped: per-call fallback limit "
                f"{self._max_browser_fallbacks_per_call} reached"
            )
            results[index]["fallback_reason"] = reason
        if not candidates:
            return results

        browser_adapter = self._browser_adapter or BrowserFallbackAdapter()
        recovered = await asyncio.to_thread(
            browser_adapter.extract_many,
            [url for _, url, _reason in candidates],
        )
        for index, url, reason in candidates:
            browser_result = recovered.get(url)
            if browser_result and not browser_result.get("error"):
                browser_result.update(
                    {
                        "backend_used": "browser-use",
                        "fallback_from": results[index].get("backend_used"),
                        "fallback_reason": reason,
                        "attempted_backends": [
                            *results[index].get("attempted_backends", []),
                            "browser-use",
                        ],
                    }
                )
                results[index] = browser_result
                logger.warning(
                    "Resilient extract fallback outcome=success primary=%s "
                    "fallback=browser-use fallback_reason=%s url=%s",
                    self._primary_backend,
                    reason,
                    url,
                )
            elif browser_result:
                primary_error = str(results[index].get("error") or "Extraction returned empty content")
                browser_error = str(browser_result.get("error") or "Browser fallback failed")
                results[index]["error"] = f"{primary_error}; {browser_error}"
                results[index]["fallback_from"] = self._primary_backend
                results[index]["fallback_reason"] = reason
                logger.warning(
                    "Resilient extract fallback outcome=failed primary=%s "
                    "fallback=browser-use fallback_reason=%s url=%s error=%s",
                    self._primary_backend,
                    reason,
                    url,
                    browser_error,
                )
        return results
