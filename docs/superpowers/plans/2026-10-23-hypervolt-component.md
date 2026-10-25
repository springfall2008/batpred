# Hypervolt Component Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `hypervolt` cloud component to Predbat that monitors every Hypervolt charger on an account, wires them into car charging, exposes manual controls, and can stop and start each charger from Predbat's car plan.

**Architecture:** One new file, `apps/predbat/hypervolt.py`, in the shape of `wallbox.py`. Pure `apply_*` functions fold JSON-RPC message bodies into a `HypervoltCharger` dataclass. `HypervoltTransport` owns the Keycloak tokens, the one REST call, and one long-lived sync websocket per charger (plus a session websocket for API version 2), with a reader task per socket and request/reply correlation so commands are awaitable. `HypervoltAPI(ComponentBase)` publishes, handles events, auto-configures and runs plan-led control from `run()` every 60 seconds; it never touches `aiohttp`.

**Tech Stack:** Python 3, `aiohttp` (`ClientSession.post`, `.get`, `.ws_connect`; already a dependency), `ComponentBase`, `MockBase`, `predbat_metrics.record_api_call`, `utils.parse_car_plan_windows` / `in_car_plan_window`, the Storage component.

**Spec:** `docs/superpowers/specs/2026-10-23-hypervolt-component-design.md`. Read it before starting; section numbers below refer to it. `apps/predbat/wallbox.py` and `apps/predbat/tests/test_wallbox.py` are the model for structure, naming, log style and test style: copy their shape wherever this plan does not say otherwise.

## Global Constraints

- No new dependency. Do not import `websockets`, `requests` or the Home Assistant integration.
- Token endpoint `https://kc.prod.hypervolt.co.uk/realms/retail-customers/protocol/openid-connect/token`; API host `https://api.hypervolt.co.uk/`; sockets `wss://api.hypervolt.co.uk/ws/charger/{id}/sync` and `…/session/in-progress`. `HYPERVOLT_CLIENT_ID = "home-assistant"` is a module constant.
- Socket headers: `Origin: https://hypervolt.co.uk`, `User-Agent: Predbat`.
- Config keys are exactly `hypervolt_username`, `hypervolt_password`, `hypervolt_automatic` (default `True`), `hypervolt_control` (default `False`). No poll interval key.
- Entity names are `{prefix}_hypervolt_{charger_id}_…`; the global switch is `switch.{prefix}_hypervolt_control`. Every entity is published with `dashboard_item(..., app="hypervolt")`.
- Constants: `COMMAND_TIMEOUT_SECONDS = 10`, `TOKEN_MARGIN_SECONDS = 600`, `STALE_SECONDS = 240` (probe), `2 * STALE_SECONDS` (reconnect), back-off random 3–12 s × 1.7 capped at 300 s, `CHARGER_LIST_SECONDS = 30 * 60`, current 6–32 A.
- No state-of-charge sensor and no `car_charging_soc` wiring.
- The access token never appears in a log line or in `--raw` output.
- Log lines start `Info: hypervolt:`, `Warn: hypervolt:` or `Error: hypervolt:`.
- Every function and class needs a docstring (`interrogate` 100%). Line length up to 250. British English. `lower_case_with_underscores`.
- Before editing an existing symbol (`COMPONENT_LIST`, `APPS_SCHEMA`, `TEST_REGISTRY`) run GitNexus `impact({target: "<symbol>", direction: "upstream"})` and report the blast radius; before each commit run GitNexus `detect_changes()` (repository rule in `CLAUDE.md`).
- Tests are slow: write test output to a file, then read the file. Never pipe a test run into `grep`.
- New test files are untracked until added, and pre-commit skips untracked files: `git add` the test file before running `./run_pre_commit`.
- Work on a branch from `main`: `git checkout main && git pull && git checkout -b feat/hypervolt-component` before Task 1.

Test command used throughout (from `coverage/`): `./run_all --test hypervolt > /tmp/hv_test.log 2>&1; tail -40 /tmp/hv_test.log`. Pass means the log ends with the suite's success line and no `AssertionError` or `Traceback`.

## Review Focus

Inputs the spec implies but no rule spells out. Each has a test in the task named.

1. Predbat restarts while the car sits plugged in, and Hypervolt pushes no pilot status until the cable moves — `_connected` and `_plugged` are `unavailable`, not `off`, and control neither releases nor forgets the charger. → Task 4 (`test_connected_state_unknown_until_pilot`), Task 9 (`test_control_waits_for_pilot_status`).
2. A reply arrives for a request id nobody is waiting on (late after a timeout, or from a previous connection) — ignored, no exception, the reader keeps reading. → Task 2 (`test_late_reply_is_ignored`).
3. The socket drops while a command is in flight — the waiting caller gets `HypervoltApiError` promptly, not a 10 second timeout, and the link reconnects. → Task 5 (`test_drop_fails_pending_commands`).
4. A message body that is not valid JSON, or a snapshot item with an unknown enum value (`"release_state": "paused"`) — skipped, the rest of the message still applied. → Task 4 (`test_apply_snapshot_ignores_bad_items`), Task 2 (`test_reader_skips_bad_json`).
5. The account's password changes while Predbat runs, so refresh and sign in both fail — errors are logged once per `run()`, the component returns False, and the sockets are not left reconnecting with a dead token in a tight loop. → Task 5 (`test_reconnect_stops_on_auth_failure`), Task 6 (`test_run_auth_failure_after_start`).

## File Structure

| File | Responsibility |
|---|---|
| `apps/predbat/hypervolt.py` (create) | Constants, `HypervoltCharger`, `apply_*`, state helpers, errors, `charger_api_version`, `ChargerLink`, `HypervoltTransport`, `HypervoltAPI`, CLI `main()` |
| `apps/predbat/tests/test_hypervolt.py` (create) | All tests; `FakeSocket`, `_StubTransport`, captured `MOCK_*` fixtures inline |
| `apps/predbat/components.py` (modify) | `COMPONENT_LIST["hypervolt"]` (spec 4.1), after `"wallbox"` |
| `apps/predbat/config.py` (modify) | `APPS_SCHEMA` keys (spec 4.2), after the `wallbox_*` keys |
| `apps/predbat/unit_test.py` (modify) | import and `TEST_REGISTRY` entry `("hypervolt", test_hypervolt, "Hypervolt EV charger tests (auth, sockets, state, publishing, auto-config, controls, plan-led charging)", False)` after `wallbox` |
| `docs/components.md`, `docs/car-charging.md`, `docs/apps-yaml.md`, `docs/devices.md`, `apps/predbat/config/apps.yaml`, `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md` (modify) | Task 10 |
| `.cspell/custom-dictionary-workspace.txt` (modify) | `Hypervolt`, `plugncharge`, `Keycloak` and any other words cspell rejects |

---

### Task 1: Transport — authentication, charger list and API version

**Files:**
- Create: `apps/predbat/hypervolt.py`, `apps/predbat/tests/test_hypervolt.py`
- Modify: `apps/predbat/unit_test.py` (registry entry, so the tests run from Task 1)

**Interfaces:**
- Produces: errors `HypervoltError` > `HypervoltAuthError`, `HypervoltApiError` > `HypervoltRefusedError`, `HypervoltTimeoutError`. `charger_api_version(charger_id: str, firmware_version: str | None) -> int`. `charger_major_version(charger_id: str) -> int`. `HypervoltTransport(log, username, password)` with `async authenticate() -> str` (valid access token, refreshing or signing in as needed), `async list_chargers() -> list[dict]` (the `chargers` list), attributes `access_token`, `refresh_token`, `token_expiry` (epoch seconds), and `token_due() -> bool` (less than `TOKEN_MARGIN_SECONDS` left).

- [ ] **Step 1: Write the failing tests**

`test_hypervolt(my_predbat=None)` calls each test and returns a failure count, as `test_wallbox` does. Tests, all REST under `patch("aiohttp.ClientSession")` with Wallbox's `_response` / `_session` helpers adapted to form bodies:
- `test_api_version_from_id`: a 12-byte id, e.g. `str(int("aa" * 12, 16))` → major 2; `str(int("aa" * 16, 16))` → major 3; `"junk"` → 3. `charger_api_version(v2_id, None) == 2`, `(v2_id, "1.0") == 3`, `(v2_id, "202111231810.abcdef") == 2`, `(v3_id, None) == 3`.
- `test_sign_in`: posts to the token URL with `client_id=home-assistant`, `grant_type=password`, `scope=openid profile email offline_access`, username and password; stores both tokens; `token_expiry` ≈ now + `expires_in`.
- `test_token_reused_then_refreshed`: a second `authenticate()` with time to spare makes no call; with `token_expiry` inside the margin it posts `grant_type=refresh_token`.
- `test_refresh_rejected_then_sign_in`: refresh answers 400 → one password sign in follows.
- `test_bad_credentials`: sign in answers 401 → `HypervoltAuthError`; the password is not in the exception text.
- `test_list_chargers`: GET `users/me?includes=chargers` with `Authorization: Bearer <token>` returns the `chargers` list; a 500 raises `HypervoltApiError`; a 401 clears the access token and retries once after `authenticate()`.

- [ ] **Step 2: Run the tests and confirm they fail** (import error).

- [ ] **Step 3: Implement** the errors, the two version functions (spec 2.7; constant `UPDATED_V2_FIRMWARE_MAX_LEN = 7`), and the transport's token handling and `list_chargers()`. A new `aiohttp.ClientSession` per REST call with a 30 second total timeout, as Wallbox does; `record_api_call("hypervolt", …)` on each.

- [ ] **Step 4: Run the tests and confirm they pass.**

- [ ] **Step 5: Commit** — `feat(hypervolt): transport authentication and charger list`.

---

### Task 2: Transport — the sync socket and request/reply correlation

**Files:** Modify `apps/predbat/hypervolt.py`, `apps/predbat/tests/test_hypervolt.py`

**Interfaces:**
- Consumes: Task 1's `authenticate()`.
- Produces: `ChargerLink` (one charger's sockets, reader tasks, `pending: dict[str, asyncio.Future]`, `api_version`). On the transport: `async connect(charger_id: str) -> None` (starts the sync reader task; returns immediately), `async request(charger_id: str, method: str, params: dict | None = None) -> Any` (sends, awaits the reply's `result`; raises `HypervoltRefusedError` with the server's `code` and `error`, `HypervoltTimeoutError` after `COMMAND_TIMEOUT_SECONDS`, `HypervoltApiError` when the charger has no open sync socket), `async close() -> None`, and the hook `on_message(charger_id: str, method: str, body: Any) -> None` that Task 4 fills in (a no-op here; tests replace it). `next_message_id() -> str` unique per call. `raw_log: Optional[Callable[[str, dict], None]]` called with every received message, token redacted, for the CLI.

Test double, in the test file: `FakeSocket` with `queue(msg_dict_or_str)`, `async receive()` returning an `aiohttp.WSMessage(WSMsgType.TEXT, json, None)` from the queue (blocking on an `asyncio.Event` when empty; a queued `None` yields `WSMsgType.CLOSED`), `async send_str(s)` recording to `sent`, `async close()`. Patch `aiohttp.ClientSession.ws_connect` to return it, as an async context manager.

- [ ] **Step 1: Write the failing tests**
- `test_sync_login_and_state_requests`: after `connect()`, the first sent message is `login` with `params == {"token": <token>, "version": 3}`; on the reply `{"authenticated": true}` the link sends `sync.snapshot`, `firmware.version`, `get.name`, and `plugncharge.get` only when the link's `api_version` is 3. Every message has `"jsonrpc": "2.0"` and an `id`.
- `test_login_refused`: reply `{"authenticated": false}` closes the socket and the next `request()` raises `HypervoltApiError`.
- `test_request_reply`: `request(id, "sync.apply", {"release": True})` sends that method and params and returns the reply's `result` matched by `id`, while an unrelated push arriving first goes to `on_message` and not to the caller.
- `test_request_error_reply`: an `error` reply `{"code": 409, "error": "Concurrent modifications…"}` raises `HypervoltRefusedError` whose text contains `409` and the message.
- `test_request_timeout`: no reply → `HypervoltTimeoutError` (patch `COMMAND_TIMEOUT_SECONDS` to 0.05) and the pending entry is removed.
- `test_late_reply_is_ignored`: a reply for an unknown id is dropped and the next queued push still reaches `on_message`.
- `test_reader_skips_bad_json`: a non-JSON frame is skipped; the following frame is processed.
- `test_replies_and_pushes_reach_on_message`: replies to `sync.snapshot`, `plugncharge.get`, `firmware.version`, `get.name` reach `on_message` with the method of the request they answer; pushes reach it with their own `method`.
- `test_raw_log_redacts_token`: with `raw_log` set, the login message's token appears as `********`.
- `test_close_cancels_readers`: after `close()` every reader task is done and every socket closed.

- [ ] **Step 2: Run and confirm failure.**
- [ ] **Step 3: Implement.** The reader loops on `receive()`; a reply resolves its future (popping the pending entry) and is also passed to `on_message` with the request's method; `CLOSED` / `ERROR` ends the loop. Send with `send_str(json.dumps(...))`. Each socket connect is recorded with `record_api_call("hypervolt", …)`. No reconnect yet (Task 5).
- [ ] **Step 4: Run and confirm pass.**
- [ ] **Step 5: Commit** — `feat(hypervolt): sync websocket with request/reply correlation`.

---

### Task 3: Command line mode and payload capture (needs a real charger)

**Files:** Modify `apps/predbat/hypervolt.py` (`main()`, `print_charger_table()`, both `# pragma: no cover`), `apps/predbat/tests/test_hypervolt.py` (fixtures only)

**Interfaces:** Consumes Tasks 1–2. Produces the `MOCK_*` fixtures Task 4 tests against.

- [ ] **Step 1: Implement `main()`** per spec section 9 with `--raw` wired to `transport.raw_log`; `--start` / `--stop` send `sync.apply {"release": false|true}`, `--max-current` sends `{"max_current": amps * 1000}`, `--mode` sends `plugncharge.set` or `sync.apply {"solar_mode"}` by API version. Without Task 4, the default output is the raw messages.
- [ ] **Step 2: Capture**, with the maintainer, `python3 apps/predbat/hypervolt.py --username … --raw --wait 60` in each of: unplugged; plugged and not charging; charging; after `--stop`; after `--start`; after unplugging and replugging while connected. Record per message whether `get.pilot_status` arrives at connect with no cable change (spec 13).
- [ ] **Step 3: Save fixtures** in the test file as `MOCK_SNAPSHOT`, `MOCK_SESSION_CHARGING`, `MOCK_SESSION_IDLE`, `MOCK_PILOT_A`, `MOCK_PILOT_B`, `MOCK_PILOT_C`, `MOCK_PLUGNCHARGE_GET`, `MOCK_PLUGNCHARGE_SET`, `MOCK_FIRMWARE`, `MOCK_ERROR_REPLY`, with the charger id, name and serial replaced. Add a comment above them stating what the capture established about each spec-13 risk; if pilot status is not pushed at connect, add `get.pilot_status` to the post-login requests in Task 2's code and its test.
- [ ] **Step 4: Run the suite and confirm it still passes.**
- [ ] **Step 5: Commit** — `feat(hypervolt): command line test mode and captured payloads`.

---

### Task 4: Charger state — dataclass, `apply_*` and derived states

**Files:** Modify `apps/predbat/hypervolt.py`, `apps/predbat/tests/test_hypervolt.py`

**Interfaces:**
- Consumes: Task 2's `on_message` hook; Task 3's fixtures.
- Produces:
  - `HypervoltCharger` dataclass fields: `charger_id: str`, `model: str = ""`, `name: str = ""`, `firmware_version: Optional[str] = None`, `api_version: int = 2`, `release_state: Optional[str] = None` (`"default"` / `"released"`), `charging: bool = False`, `pilot_status: Optional[str] = None`, `power_w: float = 0.0`, `session_id: Optional[int] = None`, `session_energy_kwh: float = 0.0`, `max_current_a: Optional[int] = None`, `charge_mode: Optional[str] = None`, `features: list = field(default_factory=list)`, `lock_state: Optional[str] = None`, `last_message: float = 0.0`.
  - `apply_snapshot(charger, items) -> None`, `apply_session(charger, result) -> None`, `apply_pilot(charger, result) -> None`, `apply_plugncharge(charger, result) -> None`.
  - `connected_state(charger) -> str` (`"on"` / `"off"` / `"unavailable"`, spec 5), `plugged_state(charger) -> str`, `status_text(charger) -> str`, `charge_mode_options(charger) -> list[str]`, `is_stale(charger, now: float) -> bool` (`now - last_message > STALE_SECONDS`).
  - Transport: `chargers: dict[str, HypervoltCharger]`, filled by `connect()` (from the `list_chargers()` entry, so `model` and `api_version` are known) and updated by `on_message`, which stamps `last_message`. On `firmware.version` it recomputes `api_version` with `charger_api_version`; Task 5 acts on a promotion.

- [ ] **Step 1: Write the failing tests**
- `test_apply_snapshot`: `MOCK_SNAPSHOT` sets `release_state`, `max_current_a` (mA ÷ 1000, an int), `charge_mode` from `solar_mode`, `features`, `lock_state`.
- `test_apply_snapshot_ignores_bad_items`: `[{"release_state": "paused"}, {"max_current": "x"}, {"max_current": 16000}]` leaves `release_state` unchanged, sets `max_current_a == 16`.
- `test_apply_session_power`: with `ev_power` → `power_w == ev_power`; without, `true_milli_amps=16000, voltage=240` → `3840.0`; `charging` false → `power_w == 0.0`.
- `test_session_energy_holds_and_resets`: session 7 at 2371 Wh → 2.371 kWh; a later lower reading in session 7 keeps 2.371; `charging` false keeps 2.371 (never unknown); session 8 at 150 Wh → 0.15.
- `test_apply_pilot`: A → `"off"` from `connected_state` and `plugged_state`; B and C → `"on"`; `"E"` is ignored.
- `test_connected_state_unknown_until_pilot`: no pilot and not charging → both `"unavailable"`; no pilot and charging → `connected_state == "on"`, `plugged_state == "unavailable"`.
- `test_apply_plugncharge`: the get form `{"applied": "boost"}` and the set form `{"mode": "eco", "applied": true}` set the mode; `{"mode": "eco", "applied": false}` does not; an unknown mode is ignored.
- `test_status_text`: charging → `charging`; released → `stopped`; pilot B → `connected`; pilot A → `disconnected`; nothing known → `unknown`.
- `test_charge_mode_options`: base three; with `home-battery-drain-prevention` in `features` adds `super_eco_battery_safe`.
- `test_captured_payloads`: each Task 3 fixture applied in capture order yields the state it was captured in.
- `test_on_message_updates_charger`: a push through the transport's `on_message` updates `transport.chargers[id]` and stamps `last_message`; `firmware.version` `"1.0"` on a V2 id sets `api_version == 3`.

- [ ] **Step 2: Run and confirm failure.**
- [ ] **Step 3: Implement.** Each `apply_*` wraps each key in its own guard, so one bad value does not stop the rest.
- [ ] **Step 4: Run and confirm pass.**
- [ ] **Step 5: Commit** — `feat(hypervolt): charger state from sync and session messages`.

---

### Task 5: Transport — link lifecycle

**Files:** Modify `apps/predbat/hypervolt.py`, `apps/predbat/tests/test_hypervolt.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 4.
- Produces: `async reconnect(charger_id: str) -> None`, `async reconnect_all() -> None`, `async probe(charger_id: str) -> None` (sends `sync.snapshot` without awaiting a reply), `backoff_seconds: Callable[[], float]` attribute on each link (tests set it to return 0), and `auth_failed: bool` (set when a reconnect's `authenticate()` raises `HypervoltAuthError`; reader loops stop reconnecting while it is set).

- [ ] **Step 1: Write the failing tests**
- `test_reconnects_with_backoff`: a `CLOSED` frame makes the reader wait the back-off and open a new socket, which logs in again. Back-off sequence for a seeded `random`: first value in 3–12; each next ×1.7; never above 300; reset to the initial range after four messages on one connection.
- `test_drop_fails_pending_commands`: a `request()` in flight when the socket closes raises `HypervoltApiError` before `COMMAND_TIMEOUT_SECONDS`.
- `test_reconnect_stops_on_auth_failure`: `authenticate()` raising `HypervoltAuthError` during a reconnect sets `auth_failed` and the reader stops trying; a later successful `authenticate()` (called by the component) clears it and `reconnect_all()` restarts the readers.
- `test_session_socket_only_for_api_2`: `connect()` for an API 2 charger opens both sockets, an API 3 charger only the sync socket; session socket frames go to `on_message` with method `get.session`.
- `test_promotion_to_api_3`: a `firmware.version` reply `"1.0"` on a V2 id sends `plugncharge.get` and closes the session socket.
- `test_reconnect_all_uses_new_token`: after `access_token` changes, `reconnect_all()` closes every socket and the new login carries the new token.

- [ ] **Step 2: Run and confirm failure.**
- [ ] **Step 3: Implement.** One reader loop per socket kind; back-off function per spec 3.1; on close, fail every pending future with `HypervoltApiError("connection closed")`.
- [ ] **Step 4: Run and confirm pass.**
- [ ] **Step 5: Commit** — `feat(hypervolt): reconnect, back-off, token refresh and API promotion`.

---

### Task 6: Component — registration, run loop and publishing

**Files:**
- Modify: `apps/predbat/hypervolt.py`, `apps/predbat/tests/test_hypervolt.py`, `apps/predbat/components.py` (spec 4.1), `apps/predbat/config.py` (spec 4.2)

**Interfaces:**
- Consumes: the transport (Tasks 1–5). Tests use `_StubTransport` holding `chargers: dict[str, HypervoltCharger]`, a settable `token_due()` and `auth_failed`, and recording `authenticate`, `list_chargers`, `connect`, `reconnect_all`, `probe`, `reconnect`, `request`, `close` calls, in Wallbox's `_StubTransport` style.
- Produces: `HypervoltAPI.initialize(self, username, password, automatic=True, hypervolt_control=False)`; `async run(seconds, first) -> bool`; `async publish_data()`; `async final()`; `entity_prefix(charger) -> str`; `car_order: list[str]` (append-only, numeric id order, Wallbox `update_car_order`); `ordered_chargers() -> list[HypervoltCharger]`; `fresh_chargers(now) -> list[HypervoltCharger]`; `queued_events: list`; `async process_events() -> bool` (copied from Wallbox, with `HypervoltError` types).

- [ ] **Step 1: Write the failing tests**
- `test_component_registration`: `COMPONENT_LIST["hypervolt"]` matches spec 4.1 exactly; the four `APPS_SCHEMA` keys exist with the spec's types.
- `test_component_missing_credentials`: no transport, error logged, `run()` returns False.
- `test_run_first_signs_in_lists_and_connects`: first run calls `authenticate`, `list_chargers`, `connect` per charger in id order; returns True only once a charger has a fresh message (stub chargers with `last_message = now` → True; all zero → False).
- `test_run_refreshes_token`: `token_due()` True → `authenticate` then `reconnect_all`.
- `test_run_probes_then_reconnects_stale`: a charger 300 s stale gets `probe`; 600 s stale gets `reconnect`; a fresh one gets neither.
- `test_run_lists_chargers_every_30_minutes`: at `seconds % CHARGER_LIST_SECONDS == 0` the list is re-read and a new charger is connected and appended to `car_order`.
- `test_run_auth_failure_after_start`: `authenticate` raising `HypervoltAuthError` logs one `Error: hypervolt:` line per run and returns False.
- `test_publishes_one_charger`: exactly the spec-5 entity set, with states from the charger, `app="hypervolt"`, and the charger name in each friendly name; no control switch while control is unavailable.
- `test_publishes_two_chargers`: both sets, in car order.
- `test_update_success_only_when_fresh`: `update_success_timestamp` called only when at least one charger is fresh.
- `test_final_closes_transport`: `final()` calls `transport.close()`.

- [ ] **Step 2: Run and confirm failure.**
- [ ] **Step 3: Implement**, following Wallbox `run()` / `publish_data()` / `charger_attributes()`. The `hypervolt_attribute_table` mirrors Wallbox's with the spec-5 keys; the `max_charging_current` entry has `min` 6, `max` 32, `step` 1; the `charge_mode` entry's `options` come from `charge_mode_options(charger)`. Run GitNexus `impact` on `COMPONENT_LIST` and `APPS_SCHEMA` before editing them.
- [ ] **Step 4: Run** `./run_all --test hypervolt --test components > /tmp/hv_test.log 2>&1` **and confirm pass** (`components` imports every registered class).
- [ ] **Step 5: Commit** — `feat(hypervolt): component registration, run loop and publishing`.

---

### Task 7: Manual controls

**Files:** Modify `apps/predbat/hypervolt.py`, `apps/predbat/tests/test_hypervolt.py`

**Interfaces:**
- Consumes: Task 6's queue and `process_events()`; `transport.request()`.
- Produces: `switch_event`, `number_event`, `select_event` stubs that queue, and their `_handler` methods; `charger_for_entity(entity_id) -> Optional[HypervoltCharger]` (whole-id match, as Wallbox's).

- [ ] **Step 1: Write the failing tests**
- `test_controls_queue_rather_than_call`: an event makes no transport call until the next `run()`.
- `test_switch_charging`: `turn_on` → `request(id, "sync.apply", {"release": False})`; `turn_off` → `{"release": True}`.
- `test_number_current_clamps_and_ignores_junk`: `"16"` → `{"max_current": 16000}`; `40` → `32000`; `3` → `6000`; `"abc"` → no request.
- `test_select_charge_mode_by_api_version`: API 3 → `plugncharge.set {"mode": "eco"}`; API 2 → `sync.apply {"solar_mode": "eco"}`; `"turbo"` → no request; `super_eco_battery_safe` without the feature → no request.
- `test_control_refused_logged_with_reason`: `HypervoltRefusedError("409 Concurrent…")` → a `Warn: hypervolt:` line containing it; the run still returns True.
- `test_charger_for_entity_requires_whole_id`: charger `12` does not match an entity for charger `123`.

- [ ] **Step 2: Run and confirm failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run and confirm pass.**
- [ ] **Step 5: Commit** — `feat(hypervolt): manual charge, current and mode controls`.

---

### Task 8: Automatic configuration

**Files:** Modify `apps/predbat/hypervolt.py`, `apps/predbat/tests/test_hypervolt.py`

**Interfaces:**
- Consumes: `car_order`.
- Produces: `automatic_config() -> None`, called from `run()` once per car order (Wallbox's `_auto_configured_order` pattern).

- [ ] **Step 1: Write the failing tests**
- `test_automatic_config_two_chargers`: `set_arg_auto` receives the five spec-6 lists in car order; `car_charging_now` with `overwrite=False`; `num_cars` raised to 2.
- `test_automatic_config_keeps_user_values`: a user `car_charging_now` survives; a user `num_cars` of 3 is not lowered.
- `test_automatic_config_disabled`: `automatic=False` sets nothing.
- `test_automatic_config_runs_once_per_order`: a second run sets nothing; a charger added later triggers one more call.
- `test_automatic_config_api_2_info`: an API 2 charger logs one `Info: hypervolt:` line naming `docs/car-charging.md`, once, and is still wired.

- [ ] **Step 2: Run and confirm failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run and confirm pass.**
- [ ] **Step 5: Commit** — `feat(hypervolt): automatic car charging configuration`.

---

### Task 9: Predbat-led charging

**Files:** Modify `apps/predbat/hypervolt.py`, `apps/predbat/tests/test_hypervolt.py`

**Interfaces:**
- Consumes: Tasks 4, 6, 8; `parse_car_plan_windows`, `in_car_plan_window`; `self.storage`.
- Produces: `enable_control()`, `async load_control_state()`, `async save_control_state() -> bool`, `control_read_only_now() -> bool`, `refresh_car_windows(now) -> bool`, `async control_tick(now)`, `async release_chargers()`, `async control_charge(now)`, `async control_one(car_n, charger, wanted)`; state `control_active`, `control_enabled`, `released_by_predbat: set[str]`, `control_state_dirty`. Storage module `"hypervolt"`, key `"control_state"`, value `{"control_enabled": bool, "released": [ids]}`. Port each from the Wallbox method of the same name; the differences are spec 7.2's rules (release state instead of status ids, no lock or OCPP handling, no schedule hand-back). `control_tick` runs on every `run()`, not on a poll cadence.

- [ ] **Step 1: Write the failing tests** (Wallbox's `_plan_window` and `_control_component` helpers, adapted)
- `test_control_needs_automatic`: `hypervolt_control=True, automatic=False` → warning, `control_active` False.
- `test_control_starts_inside_window`: released, pilot B, inside → `{"release": False}`; also with no pilot status (API 2) → sent; pilot A → not sent.
- `test_control_stops_outside_window`: default, pilot B, outside → id saved to storage before `{"release": True}` is requested (assert storage write precedes the request in a shared call log).
- `test_control_stops_waiting_car`: default, pilot B, not charging, outside → stopped (the difference from Wallbox).
- `test_control_waits_for_pilot_status`: default, no pilot, not charging, outside → nothing; a released charger with no pilot is not forgotten.
- `test_control_forgets_unplugged`: a recorded charger at pilot A is dropped from `released_by_predbat` and nothing is sent.
- `test_control_skips_stale_and_unplanned`: a stale charger and a car with no slot sensor are left alone.
- `test_control_is_per_car`: car 1 reads `binary_sensor.predbat_car_charging_slot_1`.
- `test_control_one_command_per_run`: a charger whose state has not changed after a command is not sent it again in the same run.
- `test_control_releases_on_read_only_and_switch_off`: each sends `{"release": False}` to recorded chargers only, then clears and saves.
- `test_control_state_survives_restart`: a new component with the same storage releases a charger an earlier one recorded, with control now off.
- `test_control_switch_published_only_when_available`, `test_control_switch_state_survives_restart`, `test_control_without_storage_still_works`, `test_control_state_load_is_retried`: as their Wallbox namesakes.
- `test_control_failure_does_not_fail_run`: `HypervoltRefusedError` from a control command → one warning, run returns True.

- [ ] **Step 2: Run and confirm failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run and confirm pass.**
- [ ] **Step 5: Commit** — `feat(hypervolt): Predbat-led charging with persisted release`.

---

### Task 10: Documentation

**Files:** those listed in spec section 11.

- [ ] **Step 1: Write the docs**, using the Wallbox sections as the template in each file. `docs/car-charging.md` "Hypervolt direct integration" must state: the four keys; that Predbat-led charging needs Boost mode and the Hypervolt schedule off; that a V2 charger cannot report a plugged-in car that is not charging (link the existing V2 guidance section); that the charger cannot report the car's state of charge. `docs/devices.md` keeps the Home Assistant route and adds one line pointing to the direct component. Bump the component count in `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md` by one and add `Hypervolt` to the name lists.
- [ ] **Step 2: Run** `./run_pre_commit > /tmp/hv_pc.log 2>&1` (after `git add` of every new file) **and confirm it passes**; add rejected words to `.cspell/custom-dictionary-workspace.txt` and re-stage.
- [ ] **Step 3: Commit** — `docs(hypervolt): component, car charging and apps.yaml documentation`.

---

### Task 11: Full verification and live check (needs a real charger)

- [ ] **Step 1:** `./run_all --quick > /tmp/hv_quick.log 2>&1`; confirm no failures. Run `./run_shuffle --hunt --quick > /tmp/hv_shuffle.log 2>&1` once to check the new tests do not leak state into others.
- [ ] **Step 2:** GitNexus `detect_changes({scope: "compare", base_ref: "main"})`; confirm only `hypervolt.py`, the test file, the three registry symbols and docs changed.
- [ ] **Step 3: Live**, with `hypervolt_control: True` on the maintainer's system, record in the PR description: entities appear with sensible values; `switch.<p>_charging` off then on stops and restarts a charge; plug in outside a planned window → stopped within one minute; inside a window → starts; turning the control switch off releases a charger Predbat stopped; restart Predbat with the car plugged in and note what `_connected` shows before the cable moves.
- [ ] **Step 4:** Update spec section 13 with what the live check settled, and add a `tools/debug-journal.md` entry for Hypervolt (API quirks, pilot status behaviour, release across unplug).
- [ ] **Step 5: Commit**, push and open a draft PR referencing the spec.
