# Resilient Extract

Hermes extraction orchestrator with an ordered provider chain and a bounded
Browser Use last resort. Designed for environments where a single extraction
backend is not enough: one site returns SmartCaptcha to Firecrawl, another
returns empty HTML to a generic unlocker, a third only loads under Browser
Use. This plugin keeps all of that visible in the response object so you can
debug what actually happened.

## What it does

Given a list of URLs, the plugin:

1. Routes each URL to its **primary** or **secondary** extraction backend
   based on the URL's hostname and your `prefer_secondary_domains` list.
2. Detects retriable failures both in the error string and in the response
   content itself (so a "successful" page that contains `Вы не робот` is
   correctly treated as a bot challenge).
3. Tries the other HTTP extractor (the one that was not the primary) once.
4. Falls back to a managed browser (Browser Use by default) for the URLs that
   still failed, with a hard per-call budget.
5. Stops on auth/config errors (`400/401/403/407`) instead of wasting credits.

Each result carries:

| Field | Meaning |
| --- | --- |
| `backend_used` | which backend produced the final answer |
| `fallback_from` | which backend failed first |
| `fallback_reason` | short classifier (`captcha`, `bot_challenge_content`, `wait_element_timeout`, …) |
| `attempted_backends` | ordered list of every backend tried |

## Configuration

```yaml
web:
  extract_backend: resilient-extract
  resilient_extract:
    primary_backend: firecrawl
    secondary_backend: brightdata-unlocker
    prefer_secondary_domains:
      - market.yandex.ru
      - ozon.ru
      - wildberries.ru
      - avito.ru
      - travel.yandex.ru
      - rzd.ru
      - aeroflot.ru
      - aviasales.ru
    max_browser_fallbacks_per_call: 10

browser:
  engine: auto
  cloud_provider: browser-use
```

Routing:

- ordinary domains: `firecrawl → brightdata-unlocker → browser-use`;
- domains listed in `prefer_secondary_domains`: `brightdata-unlocker →
  firecrawl → browser-use` (Firecrawl is skipped first because many Russian
  anti-bot sites return a SmartCaptcha page through it);
- provider-specific `endpoint is not supported` can move to the other HTTP
  extractor;
- auth/config errors stop rather than wasting more requests.

Fallback candidates include typed `BRD_RETRIABLE` failures, anti-bot /
CAPTCHA / challenge errors, HTTP 403/429, scrape timeouts, `SCRAPE_RETRY_LIMIT`,
`all scraping engines failed`, empty content, and anti-bot markers inside
nominally successful content (for example Yandex's `Вы не робот` page).

A single Browser Use session is reused for all last-resort URLs in one call
and closed afterward. At most `max_browser_fallbacks_per_call` URLs reach
Browser Use per call — additional failing URLs get a typed error explaining
the per-call limit was reached.

## Install

This is a standard Hermes plugin. Drop the directory into `~/.hermes/plugins/`
or install it from GitHub:

```bash
hermes plugins install mawlic/resilient-extract
```

Then enable it and configure the `web.extract_backend` setting as shown above.

The plugin assumes you have already configured the backends it routes to
(`firecrawl` and `brightdata-unlocker`), and that Hermes is configured with a
managed browser engine (`browser-use` is the default cloud provider).

## Tests

```bash
pytest -q tests/test_provider.py
```

The tests are pure-Python with `httpx.MockTransport`; no network is required.

## Status

Verified live in production (2026-08-01) for `wildberries.ru`, `ozon.ru`,
`avito.ru` via the `brightdata-unlocker` fallback path. See
[`hermes-web-access`](https://github.com/mawlic/hermes-web-access) for the
cross-plugin architecture document, verified-targets table, and known
restrictions.