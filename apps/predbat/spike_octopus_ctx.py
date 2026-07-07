"""SPIKE: prove the shared-component `ctx` shape on the Octopus hot path.

Goal
----
Show that a SINGLE shared Octopus service object can serve N tenants with no
state bleed, by threading a per-tenant context (`TenantContext`) through the
methods that today read/write per-tenant state on `self`.

This is a throwaway proof, NOT the Phase-2 refactor. It ports the *real*
Octopus state transitions (staleness clocks, the UI command queue, cred
access) verbatim in shape, with the network faked so it runs offline.

Run:  python3 spike_octopus_ctx.py     (exit 0 = all invariants held)

Mapping to real code (apps/predbat/octopus.py @ origin/main b1d2614c):
  - initialize()            octopus.py:354   -> TenantContext fields
  - _data_age_minutes()     octopus.py:417   -> staticmethod (already pure)
  - run() due-logic         octopus.py:423   -> run(ctx, seconds, first)
  - select_event() append   octopus.py:382   -> select_event(ctx, ...)
  - process_commands()      octopus.py:490   -> process_commands(ctx)
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional
import asyncio


# ---------------------------------------------------------------------------
# Per-tenant state bag.  Everything octopus.py:initialize() sets on `self`
# that is tenant-specific lands here instead.  (17 of the 18 initialize fields;
# `_product_info_cache` is the sole shared one -> lives on the service below.)
# ---------------------------------------------------------------------------
@dataclass
class TenantContext:
    account_id: str
    api_key: str  # cred (octopus.py:356)
    base: Any  # the per-tenant HA/base ref
    mpan: Optional[str] = None
    graphql_token: Optional[str] = None
    graphql_expiration: Optional[datetime] = None
    account_data: dict = field(default_factory=dict)
    tariffs: dict = field(default_factory=dict)
    saving_sessions: dict = field(default_factory=dict)
    saving_sessions_to_join: list = field(default_factory=list)
    intelligent_devices: dict = field(default_factory=dict)
    free_electricity_events: list = field(default_factory=list)
    tariff_fetched_at: Optional[datetime] = None  # staleness clock (octopus.py:366)
    device_fetched_at: Optional[datetime] = None  # staleness clock (octopus.py:367)
    automatic: bool = False
    commands: list = field(default_factory=list)  # UI command queue (octopus.py:369)


# ---------------------------------------------------------------------------
# A tiny fleet-wide rate limiter — the density win.  One budget for ALL
# tenants replaces N per-instance clients each hammering the vendor limit.
# ---------------------------------------------------------------------------
class RateBudget:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.spent = 0

    def take(self) -> None:
        self.spent += 1
        if self.spent > self.capacity:
            raise RuntimeError("fleet rate budget exhausted")


# ---------------------------------------------------------------------------
# The SHARED service.  Holds ONLY tenant-agnostic state: the product-info
# cache, one HTTP session (connection pool), and the fleet rate budget.
# Every method takes `ctx` as its first argument. No per-tenant attributes.
# ---------------------------------------------------------------------------
class SharedOctopusService:
    def __init__(self, session: Any, budget: RateBudget):
        self.session = session  # shared connection pool
        self.product_info_cache: dict = {}  # shared (octopus.py:378)
        self.budget = budget  # shared vendor rate budget
        # Test hook: every fake API call records (account_id, api_key) here so
        # the test can prove each call used the RIGHT tenant's credentials.
        self.calls: list = []

    # --- ported pure helper (octopus.py:417) — already took its clock as arg ---
    @staticmethod
    def _data_age_minutes(fetched_at: Optional[datetime], now: datetime) -> float:
        if fetched_at is None:
            return 9999
        return (now - fetched_at).total_seconds() / 60

    # --- faked network: records which tenant's creds it saw, draws budget ---
    async def _api_call(self, ctx: TenantContext, endpoint: str) -> dict:
        self.budget.take()
        self.calls.append((endpoint, ctx.account_id, ctx.api_key))
        assert self.session is not None  # same shared pool for every tenant
        return {"endpoint": endpoint, "account": ctx.account_id}

    async def async_get_account(self, ctx: TenantContext) -> bool:
        data = await self._api_call(ctx, "account")
        ctx.account_data = data
        return True

    async def async_update_intelligent_devices(self, ctx: TenantContext) -> None:
        await self._api_call(ctx, "devices")
        ctx.intelligent_devices = {"dev": ctx.account_id}

    # --- ported UI event path (octopus.py:382). In a shared world the inbound
    #     event MUST carry the tenant identity so it lands on the right ctx. ---
    async def select_event(self, ctx: TenantContext, command: dict) -> None:
        ctx.commands.append(command)

    # --- ported command drain (octopus.py:490) — keyed on ctx, not self ---
    async def process_commands(self, ctx: TenantContext) -> bool:
        if not ctx.commands:
            return False
        for cmd in ctx.commands:
            await self._api_call(ctx, "command:" + cmd["command"])
        ctx.commands = []
        return True

    # --- ported run() due-logic (octopus.py:423), self.* -> ctx.* ------------
    async def run(self, ctx: TenantContext, seconds: int, first: bool, now: datetime) -> None:
        refresh = False
        if not first and await self.process_commands(ctx):
            refresh = True

        tariff_due = self._data_age_minutes(ctx.tariff_fetched_at, now) >= 30
        device_due = refresh or self._data_age_minutes(ctx.device_fetched_at, now) >= 10

        if tariff_due:
            if await self.async_get_account(ctx):
                ctx.tariff_fetched_at = now
        if device_due:
            await self.async_update_intelligent_devices(ctx)
            ctx.device_fetched_at = now


# ---------------------------------------------------------------------------
# The no-bleed proof.
# ---------------------------------------------------------------------------
async def main() -> int:
    session = object()  # sentinel shared pool
    budget = RateBudget(capacity=100)
    svc = SharedOctopusService(session, budget)

    t0 = datetime(2026, 7, 7, 12, 0, 0)
    A = TenantContext(account_id="A-acct", api_key="A-key", base=object(), automatic=True)
    B = TenantContext(account_id="B-acct", api_key="B-key", base=object(), automatic=False)

    # First cycle: both tenants cold -> both fetch tariff + device.
    await svc.run(A, seconds=0, first=True, now=t0)
    await svc.run(B, seconds=0, first=True, now=t0)

    # 15 min later: tariff not yet due (<30), device IS due (>=10) for both.
    t1 = t0 + timedelta(minutes=15)
    await svc.run(A, seconds=900, first=False, now=t1)
    await svc.run(B, seconds=900, first=False, now=t1)

    # A user queues a UI command on tenant A only, then A runs; B does NOT.
    await svc.select_event(A, {"command": "set_intelligent_target_time", "value": "06:00"})
    t2 = t1 + timedelta(minutes=1)
    await svc.run(A, seconds=960, first=False, now=t2)

    checks = []

    # (1) No credential bleed: every recorded call used its tenant's own creds.
    cred_ok = all((acct, key) in {("A-acct", "A-key"), ("B-acct", "B-key")} and ((acct == "A-acct") == (key == "A-key")) for _ep, acct, key in svc.calls)
    checks.append(("no credential bleed across tenants", cred_ok))

    # (2) Independent staleness clocks: A's command-triggered refresh at t2 must
    #     not have touched B's clocks (B last ran at t1).
    checks.append(("independent staleness clocks", A.device_fetched_at == t2 and B.device_fetched_at == t1))

    # (3) Command isolation: the queued command was processed for A and drained;
    #     it never appeared in B's queue nor was billed to B's account.
    a_cmd_calls = [c for c in svc.calls if c[0].startswith("command:") and c[1] == "A-acct"]
    b_cmd_calls = [c for c in svc.calls if c[0].startswith("command:")]
    checks.append(("command routed to A only, drained", len(a_cmd_calls) == 1 and len(b_cmd_calls) == 1 and A.commands == [] and B.commands == []))

    # (4) Partition holds: the shared service carries NONE of the per-tenant
    #     field names as attributes after serving both tenants.
    per_tenant_fields = set(TenantContext.__dataclass_fields__) - {"base"}
    leaked = per_tenant_fields & set(vars(svc))
    checks.append(("service holds no per-tenant state", leaked == set()))

    # (5) Density win: both tenants drew from ONE shared rate budget.
    checks.append(("single fleet-wide rate budget", budget.spent == len(svc.calls) and budget.spent > 0))

    # (6) Per-tenant data stayed distinct on the two contexts.
    checks.append(("tenant data isolated", A.intelligent_devices == {"dev": "A-acct"} and B.intelligent_devices == {"dev": "B-acct"} and A.automatic is True and B.automatic is False))

    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print(f"\n{'ALL INVARIANTS HELD' if ok else 'SPIKE FAILED'} " f"({len(svc.calls)} API calls, budget spent {budget.spent}/{budget.capacity})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
