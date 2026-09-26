# Bright Data Russian Web Access Implementation Plan

> **For Hermes:** Execute task-by-task with TDD and real network verification.

**Goal:** Give Hermes resilient read-only discovery and extraction for Russian marketplaces, classifieds, hotel aggregators, railway sites, airlines, and airfare aggregators without automating purchases or bypassing account/payment boundaries.

**Architecture:** Keep Bright Data SERP as discovery. Add a standalone `brightdata-unlocker` extraction provider, then make `resilient-extract` route protected domains to it before Firecrawl and use Browser Use only as the last fallback. Add site-specific structured adapters only where live probes prove generic extraction insufficient; prefer public JSON endpoints or Bright Data Scraper Studio over brittle browser selectors.

**Tech stack:** Hermes `WebSearchProvider`, Python 3.11, `httpx`, BeautifulSoup, pytest, Bright Data Web Unlocker API, optional Bright Data Scraper Studio.

---

### Task 1: Create the standalone Bright Data Web Unlocker provider

**Files:**
- Create: `~/.hermes/plugins/web/brightdata_unlocker/provider.py`
- Create: `~/.hermes/plugins/web/brightdata_unlocker/__init__.py`
- Create: `~/.hermes/plugins/web/brightdata_unlocker/plugin.yaml`
- Test: `~/.hermes/plugins/web/brightdata_unlocker/tests/test_provider.py`

**Steps:**
1. Write a failing provider-contract test for extraction-only registration and config loading.
2. Run the focused test and confirm it fails because the provider does not exist.
3. Implement the minimal plugin registration and config loader.
4. Run focused and full tests.
5. Commit.

### Task 2: Implement typed Web Unlocker HTTP extraction

**Files:**
- Modify: `brightdata_unlocker/provider.py`
- Test: `brightdata_unlocker/tests/test_provider.py`

**Steps:**
1. Write a failing test for a successful external HTTP 200 plus internal `x-brd-status-code: 200` response.
2. Implement async requests to `https://api.brightdata.com/request` using `BRIGHTDATA_API_KEY`, configured zone, timeout, bounded concurrency, `format=raw`, and optional render.
3. Write and verify failing tests for internal BRD errors carried inside external HTTP 200, network timeout, empty body, and CAPTCHA body.
4. Implement typed per-URL errors without logging credentials.
5. Write a failing test for HTML cleanup and implement script/style removal plus visible-text extraction.
6. Run full tests and secret scan.
7. Commit.

### Task 3: Reproduce and fix successful CAPTCHA-page handling

**Files:**
- Modify: `~/.hermes/plugins/web/resilient_extract/provider.py`
- Test: `~/.hermes/plugins/web/resilient_extract/tests/test_provider.py`

**Steps:**
1. Add a failing regression test where Firecrawl returns non-empty `Вы не робот?` content with no `error`.
2. Confirm Browser Use is not currently called.
3. Add content/title anti-bot detection shared across primary and fallback results.
4. Verify the focused test, then all resilient-extract tests.
5. Commit.

### Task 4: Add ordered secondary-backend routing

**Files:**
- Modify: `resilient_extract/provider.py`
- Modify: `resilient_extract/tests/test_provider.py`
- Modify: `resilient_extract/README.md`
- Modify: `resilient_extract/plugin.yaml`

**Behavior:**
- Default domains: `firecrawl -> brightdata-unlocker -> browser-use`.
- Protected configured domains: `brightdata-unlocker -> firecrawl -> browser-use`.
- Permanent/auth/config errors do not trigger wasteful retries.
- Per-call limits bound secondary and browser fallbacks.
- Every result records `backend_used`, attempted backends, and fallback reason.

**Steps:**
1. Add one failing routing test at a time: normal success, protected-domain preference, primary CAPTCHA to secondary success, secondary failure to browser, limit exhaustion, permanent error stop, result ordering.
2. Implement only enough behavior to pass each test before adding the next.
3. Run full tests and commit.

### Task 5: Configure Hermes and verify in a fresh process

**Files:**
- Modify: `~/.hermes/config.yaml`

**Steps:**
1. Add `web.brightdata_unlocker` settings for zone `web_unlocker1`, render, timeout, and bounded concurrency.
2. Add secondary backend and protected domains to `web.resilient_extract`.
3. Load plugins in a fresh Hermes process and verify provider availability, order, and secret resolution.
4. Run a real `web_extract` against the known Yandex Market card and verify no CAPTCHA plus Bright Data telemetry.
5. Do not restart the gateway without separate restart approval.

### Task 6: Build and maintain a capability matrix

**Files:**
- Create: `docs/russian-site-capability-matrix.md`

**Steps:**
1. Probe one public read-only URL per target with bounded concurrency and no retries.
2. Record external/internal status, latency, useful content, CAPTCHA/block page, and required next layer.
3. Test specific product/listing pages separately from search pages.
4. Classify each target as generic extraction, public JSON adapter, Scraper Studio custom scraper, or interactive browser-only.
5. Commit evidence without response bodies or credentials.

### Task 7: Add only evidence-backed structured adapters

**Candidate adapters:**
- Wildberries product/search via stable public catalog JSON if live tests pass.
- Avito listing/search parser from unlocked HTML/embedded state.
- Yandex Market card parser from unlocked HTML/embedded state.
- Ozon only after a stable allowed source is identified.
- Travel and ticket sites through Scraper Studio or Browser API only when routes, dates, and passenger counts require interaction.

**Steps for each adapter:**
1. Capture a sanitized fixture without secrets or personal data.
2. Write a failing schema test for title, URL, price/fare, availability, seller/carrier, dates, and source timestamp as applicable.
3. Implement the minimum parser.
4. Add malformed/blocked-page tests.
5. Verify with a live read-only query.
6. Commit separately.

### Task 8: Final verification

1. Run all plugin tests with strict warnings.
2. Run `py_compile`, `git diff --check`, repository integrity checks, and exact-secret scan.
3. Verify clean plugin discovery in a temporary `HERMES_HOME`.
4. Run representative discovery plus extraction workflows.
5. Report supported, partially supported, and blocked capabilities honestly; do not call interactive search or checkout production-ready without runtime evidence.
