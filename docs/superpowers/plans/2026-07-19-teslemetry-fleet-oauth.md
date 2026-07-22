# Direct Tesla Fleet API via OAuthMixin — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. Steps use `- [ ]`.

**Goal:** Let the Teslemetry component talk to the Tesla **Fleet API directly** (not just the Teslemetry proxy) using OAuth, with the OAuth flow + token refresh **driven externally by predbat.com** via the existing `OAuthMixin` — exactly the pattern Fox/Kraken/Solis already use. The Teslemetry static-token path stays as the default; OAuth is a second `auth_method`.

**Key insight (why this is small):** `OAuthMixin` does NOT call Tesla's OAuth endpoints. It calls predbat.com's Supabase `oauth-refresh` edge function with `{instance_id (user_id), provider, token_hash}`; predbat.com's backend holds the client id/secret, the refresh token, partner registration and the public-key hosting, and returns a fresh `access_token` + `expires_at`. The component only: holds `access_token`/`token_expires_at`, refreshes-before-call, swaps the bearer token, and retries once on 401. Tesla **energy-site** commands (`operation`/`backup`/`grid_import_export`/`time_of_use_settings`) use plain REST — the vehicle command-signing protocol does NOT apply — so nothing in the data/control layer changes.

**Tech Stack:** Python 3, `aiohttp`; `OAuthMixin` (`apps/predbat/oauth_mixin.py`); component registry (`components.py`); config (`config.py`); tests (`tests/test_teslemetry.py`).

## Global Constraints
- Line ≤ 250 (Flake8 250 / Black 256); 100% docstrings (interrogate); British spelling; `lower_case_with_underscores`.
- Tests run from `coverage/` (`./run_all -k teslemetry > /tmp/x.log 2>&1`, then grep the file). New tests registered in the `def test_teslemetry()` runner at the bottom of the test file.
- **Do not change** the tariff/quantise/boost/sync/evaluate/assert logic — this feature is auth-only.

## Reference pattern (mirror Fox exactly)
- `class FoxAPI(ComponentBase, OAuthMixin)`; `initialize(... auth_method=None, token_expires_at=None, token_hash=None)` calls `self._init_oauth(auth_method, key, token_expires_at, "fox_ess")` (`fox.py:335,377`).
- Request path (`fox.py:1633-1691`): if `auth_method=="oauth"` and not a retry → `await self.check_and_refresh_oauth_token()`; skip the call if it returns False. Build the bearer from `self.access_token` when oauth else the static key. On HTTP 401 (oauth, not already retried) → `await self.handle_oauth_401()`; if it returns True, retry the request ONCE with a retry flag.
- Config keys `fox_auth_method` / `fox_token_expires_at` / `fox_token_hash`; in oauth mode `fox_key` holds the access token. Registered in `components.py:206-241` as `args` entries mapping config → `initialize` kwargs.

---

### Task 1: Mix `OAuthMixin` into `TeslemetryAPI` and initialise it

**Files:** Modify `apps/predbat/teslemetry.py`; Test `apps/predbat/tests/test_teslemetry.py`.

**Changes:**
- Import: `from oauth_mixin import OAuthMixin`.
- Class: `class TeslemetryAPI(ComponentBase, OAuthMixin):`.
- `initialize(...)`: make the OAuth kwargs explicit and initialise the mixin. Replace the `**kwargs` swallow with named params:
  ```python
  def initialize(self, key="", site_id="", base_url=TESLEMETRY_DEFAULT_URL, automatic=False,
                 auth_method=None, token_expires_at=None, token_hash=None, **kwargs):
      ...
      self.api_key = key
      ...
      self._init_oauth(auth_method, key, token_expires_at, "tesla")   # provider_name -> see Decision D1
      if token_hash:
          self.token_hash = token_hash
      ...
  ```
  (`_init_oauth` sets `self.auth_method`, `self.access_token` (=key in oauth mode), `self.token_expires_at`, `self.provider_name`, `self.oauth_failed`, `self.token_hash`.)
- Add a bearer helper so one place decides which token to send:
  ```python
  def _bearer_token(self):
      """Return the bearer token: the refreshable OAuth access token in oauth mode, else the static key."""
      return self.access_token if self.auth_method == "oauth" else self.api_key
  ```

**Tests:**
- `test_teslemetry_oauth_init_sets_access_token`: construct a `MockTeslemetryAPI`-style instance (or call `_init_oauth`) with `auth_method="oauth"`, `key="ACCESS"`, and assert `api.auth_method == "oauth"`, `api._bearer_token() == "ACCESS"`. Default (`auth_method=None`) → `_bearer_token()` returns the static key and `auth_method == "api_key"`.

- [ ] Write the failing test; run (FAIL: no `_bearer_token`/mixin); implement; run (PASS); commit `feat(teslemetry): mix in OAuthMixin, resolve bearer per auth_method`.

> Note for the mock: `MockTeslemetryAPI` bypasses `initialize`. Add a tiny helper in the test that calls `TeslemetryAPI._init_oauth(api, ...)` (the mixin methods only need `self.log`, `self.base`), or set `api.auth_method`/`api.access_token` directly for the api_key-default assertions.

---

### Task 2: OAuth in `_request` (refresh-before-call, bearer swap, 401→refresh→retry)

**Files:** Modify `apps/predbat/teslemetry.py` (`_request`); Test `tests/test_teslemetry.py`.

**Changes — mirror Fox's `request_get_func`:**
```python
async def _request(self, method, path, json_body=None, _retry_after_refresh=False):
    # Refresh the OAuth token before the call (no-op in api_key mode).
    if self.auth_method == "oauth" and not _retry_after_refresh:
        if not await self.check_and_refresh_oauth_token():
            self.api_auth_failed = True
            self.log("Warn: Teslemetry OAuth token refresh failed, skipping API call")
            return None
    url = "{}{}".format(self.base_url, path)
    headers = {"Authorization": "Bearer {}".format(self._bearer_token()), "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=TESLEMETRY_TIMEOUT)
    for attempt in range(TESLEMETRY_RETRIES):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, headers=headers, json=json_body) as resp:
                    if resp.status == 401 and self.auth_method == "oauth" and not _retry_after_refresh:
                        if await self.handle_oauth_401():
                            return await self._request(method, path, json_body=json_body, _retry_after_refresh=True)
                    if resp.status in (401, 403):
                        self.api_auth_failed = True
                        self.log("Warn: Teslemetry auth failed ({}) on {}".format(resp.status, path))
                        return None
                    # ... existing 429 / >=400 / success handling unchanged ...
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            ...
```
- The 401→refresh→retry must sit BEFORE the existing `401/403 → api_auth_failed` branch, and only fire once (`_retry_after_refresh` guard).
- Everything else in `_request` (429 backoff, `_summarize_for_log`, success parse) is unchanged.

**Tests (mock the mixin, not the network):**
- `test_teslemetry_request_refreshes_before_call_oauth`: `auth_method="oauth"`, monkeypatch `check_and_refresh_oauth_token` to a coroutine that flips `access_token` to `"NEW"`; assert the request went out with `Authorization: Bearer NEW`. (The `MockTeslemetryAPI._request` is overridden for canned responses, so for this test drive the REAL `_request` with an `aiohttp` mock from `test_infra`, or unit-test the header assembly via a seam — see note.)
- `test_teslemetry_request_401_triggers_single_refresh_retry`: first response 401, `handle_oauth_401` returns True and sets a new token; assert exactly one retry and the second attempt used the new bearer; a still-401 retry sets `api_auth_failed`.
- `test_teslemetry_request_api_key_mode_unchanged`: `auth_method="api_key"` → no refresh call, bearer is the static key (guards against regressing the Teslemetry path).

> Note: `MockTeslemetryAPI` overrides `_request` to return canned `mock_responses`, so these tests exercise the mixin seams (`check_and_refresh_oauth_token`/`handle_oauth_401`/`_bearer_token`) rather than the overridden `_request`. Prefer testing those seams directly plus one header-assembly test against the real `_request` using `create_aiohttp_mock_session` from `tests/test_infra.py` (as other real-HTTP teslemetry tests do).

- [ ] Write failing tests; run (FAIL); implement; run (PASS); commit `feat(teslemetry): OAuth refresh-before-call and 401 retry in _request`.

---

### Task 3: Config schema + component registration

**Files:** Modify `apps/predbat/config.py` (`APPS_SCHEMA`), `apps/predbat/components.py` (teslemetry `args`); Test `tests/test_teslemetry.py`.

**`config.py` APPS_SCHEMA** — add beside the existing `teslemetry_*` keys (mirror the `fox_*` oauth keys):
```python
    "teslemetry_auth_method": {"type": "string", "empty": False},
    "teslemetry_token_expires_at": {"type": "string", "empty": False},
    "teslemetry_token_hash": {"type": "string", "empty": False},
```

**`components.py`** — add to the `teslemetry` `args` block (mirror `fox`):
```python
    "auth_method": {"required": False, "config": "teslemetry_auth_method", "default": "api_key"},
    "token_expires_at": {"required": False, "config": "teslemetry_token_expires_at"},
    "token_hash": {"required": False, "config": "teslemetry_token_hash"},
```
(`teslemetry_key` continues to hold the access token in oauth mode — no new key.)

**Test:** `test_teslemetry_inverter_def_tesla`/an APPS_SCHEMA presence test — assert the three new schema keys parse and are optional; a components-wiring test that the `auth_method` arg reaches `initialize` (follow any existing components arg-mapping test).

- [ ] Write failing test; run (FAIL); implement; run (PASS); pre-commit (cspell: add `teslemetry`/oauth words if flagged); commit `feat(teslemetry): oauth auth_method/token config wiring`.

---

### Task 4: Docs, template, and the CLI harness

**Files:** `docs/components.md`, `docs/inverter-setup.md`, `templates/teslemetry.yaml`, `apps/predbat/teslemetry.py` (CLI `main()`).

- **components.md** Teslemetry section: document `auth_method` (`api_key` default vs `oauth`), that oauth is provisioned by predbat.com (like Fox), and that in oauth mode `base_url` must be the **regional Fleet endpoint** (`https://fleet-api.prd.<region>.vn.cloud.tesla.com`) and `key` holds the access token. Note token refresh is handled by predbat.com.
- **inverter-setup.md / template**: add an oauth example block (commented) alongside the Teslemetry-token one.
- **CLI harness** (`test_teslemetry_api`/`main`): optionally accept `--auth-method oauth` + `--base-url <fleet>` for manual testing (low priority; the static-token path already works for local testing).

- [ ] Update docs + template; run markdownlint/cspell hooks; commit `docs(teslemetry): document direct Fleet API via OAuth`.

---

## Backend prerequisites (predbat.com — NOT in this repo)
These gate whether oauth mode actually works at runtime; the component code is inert without them:
1. **`oauth-refresh` edge function must support a `tesla` provider** — a Tesla Fleet OAuth app registered by predbat.com (client id/secret, **partner registration**, hosted public key), holding each user's refresh token and returning `{success, access_token, expires_at, token_hash}` for `provider="tesla"`.
2. Token **scopes**: `energy_device_data` + `energy_cmds` (read + control energy sites).
3. **Region**: predbat.com provisions the correct regional `base_url` into the user's `teslemetry_base_url` (or a region → base_url mapping is added). The auth/refresh endpoint is global; only the API base is regional.
4. The predbat.com connect UI runs the Tesla authorization-code flow and writes `teslemetry_auth_method: oauth`, `teslemetry_key` (access token), `teslemetry_token_expires_at`, `teslemetry_token_hash`, `teslemetry_base_url` into the user's config — same as the Fox connect flow.

## Decisions to confirm
- **D1 — `provider_name` string.** Plan assumes `"tesla"`. Must match the identifier the `oauth-refresh` edge function registers for the Tesla handler. (Fox uses `"fox_ess"`.)
- **D2 — Region/base_url.** Reuse the existing `teslemetry_base_url` (predbat.com sets it) vs add a `teslemetry_region` enum that maps to a base_url. Plan assumes reuse `base_url`.
- **D3 — Both modes coexist.** `auth_method` defaults to `api_key` (Teslemetry static token) — unchanged for existing users; `oauth` opts into direct Fleet. (Assumed yes, mirrors Fox/Solis.)

## Effort & risk
- **Component code:** small — ~Task 1–3 are a few dozen lines mirroring Fox; Task 4 docs. Days, not weeks. No change to tariff/control logic or its tests.
- **Real effort is backend + onboarding** (predbat.com Tesla Fleet app, partner registration, region provisioning) — outside this repo, and the actual reason to do it this way (self-hosted users can't easily do Fleet OAuth themselves; predbat.com does it for them, exactly like Fox).
- **Self-hosted-without-predbat.com** users keep using `api_key` (Teslemetry) — oauth mode requires the predbat.com edge function, same limitation Fox has.
