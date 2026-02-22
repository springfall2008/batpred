# fmt: off
# pylint: disable=line-too-long
"""Prometheus metrics for PredBat.

Defines all metrics emitted by the OSS PredBat codebase. Each component
imports ``metrics()`` and emits its own data at the point of origin.
``prometheus_client`` is optional -- when absent every metric operation
silently no-ops so the core engine runs unchanged.
"""

# ---------------------------------------------------------------------------
# Conditional import -- zero overhead when prometheus_client is absent
# ---------------------------------------------------------------------------
try:
    from prometheus_client import Counter, Gauge, Histogram, generate_latest
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False


class _NoOpMetric:
    """Swallows all metric operations silently.

    Handles chained calls like ``m.labels(service='x').inc()`` by returning
    *self* from every method that would normally return a child metric.
    """
    def inc(self, amount=1): pass
    def dec(self, amount=1): pass
    def set(self, value): pass
    def observe(self, value): pass
    def set_to_current_time(self): pass
    def labels(self, **kw): return self


_NOOP = _NoOpMetric()


def _gauge(name, doc, labels=None):
    if not PROMETHEUS_AVAILABLE:
        return _NOOP
    return Gauge(name, doc, labels or [])


def _counter(name, doc, labels=None):
    if not PROMETHEUS_AVAILABLE:
        return _NOOP
    return Counter(name, doc, labels or [])


def _histogram(name, doc, buckets=None):
    if not PROMETHEUS_AVAILABLE:
        return _NOOP
    kw = {}
    if buckets is not None:
        kw["buckets"] = buckets
    return Histogram(name, doc, **kw)


# ---------------------------------------------------------------------------
# Singleton metrics registry
# ---------------------------------------------------------------------------
class PredbatMetrics:
    """All PredBat Prometheus metrics in one place.

    Instantiated once via :func:`metrics`.  Each attribute is either a real
    ``prometheus_client`` metric or :class:`_NoOpMetric`.
    """

    def __init__(self):
        # -- Application health ------------------------------------------------
        self.up = _gauge("predbat_up", "Application is running", ["version"])
        self.errors_total = _counter("predbat_errors_total", "Total errors", ["type"])
        self.last_update_timestamp = _gauge("predbat_last_update_timestamp", "Timestamp of last update")
        self.config_valid = _gauge("predbat_config_valid", "Configuration is valid (1) or has errors (0)")
        self.config_warnings = _gauge("predbat_config_warnings", "Number of configuration warnings")

        # -- Plan --------------------------------------------------------------
        self.plan_valid = _gauge("predbat_plan_valid", "Whether the current plan is valid")
        self.plan_age_minutes = _gauge("predbat_plan_age_minutes", "Age of current plan in minutes")
        self.planning_duration_seconds = _histogram(
            "predbat_planning_duration_seconds",
            "Time taken to complete a planning cycle in seconds",
            buckets=[30, 60, 120, 180, 240, 300, 600, 900],
        )

        # -- External API metrics (per-service labels) -------------------------
        self.api_requests_total = _counter("predbat_api_requests_total", "Total API requests", ["service"])
        self.api_failures_total = _counter("predbat_api_failures_total", "Total API failures", ["service", "reason"])
        self.api_last_success_timestamp = _gauge("predbat_api_last_success_timestamp", "Last successful API call timestamp", ["service"])

        # -- Battery & inverter ------------------------------------------------
        self.battery_soc_percent = _gauge("predbat_battery_soc_percent", "Battery state of charge percentage")
        self.battery_soc_kwh = _gauge("predbat_battery_soc_kwh", "Battery state of charge in kWh")
        self.battery_max_kwh = _gauge("predbat_battery_max_kwh", "Battery maximum capacity in kWh")
        self.charge_rate_kw = _gauge("predbat_charge_rate_kw", "Current charge rate in kW")
        self.discharge_rate_kw = _gauge("predbat_discharge_rate_kw", "Current discharge rate in kW")
        self.inverter_register_writes_total = _counter("predbat_inverter_register_writes_total", "Total inverter register writes")

        # -- Energy today ------------------------------------------------------
        self.load_today_kwh = _gauge("predbat_load_today_kwh", "Load energy today in kWh")
        self.import_today_kwh = _gauge("predbat_import_today_kwh", "Import energy today in kWh")
        self.export_today_kwh = _gauge("predbat_export_today_kwh", "Export energy today in kWh")
        self.pv_today_kwh = _gauge("predbat_pv_today_kwh", "PV energy today in kWh")
        self.data_age_days = _gauge("predbat_data_age_days", "Age of load data in days")

        # -- Cost & savings ----------------------------------------------------
        self.cost_today = _gauge("predbat_cost_today", "Cost today in currency units")
        self.savings_today_pvbat = _gauge("predbat_savings_today_pvbat", "PV/Battery system savings today")
        self.savings_today_actual = _gauge("predbat_savings_today_actual", "Actual savings today")

        # -- IOG (Intelligent Octopus Go) --------------------------------------
        self.iog_action_latency_seconds = _histogram(
            "predbat_iog_action_latency_seconds",
            "Time between IOG slot start and charge command sent in seconds",
            buckets=[10, 30, 60, 120, 180, 240, 300, 600],
        )
        self.iog_actions_total = _counter("predbat_iog_actions_total", "Total IOG charge actions attempted", ["status"])

        # -- Solcast / solar ---------------------------------------------------
        self.solcast_api_limit = _gauge("predbat_solcast_api_limit", "Solcast API quota limit")
        self.solcast_api_used = _gauge("predbat_solcast_api_used", "Solcast API usage count")
        self.solcast_api_remaining = _gauge("predbat_solcast_api_remaining", "Solcast API calls remaining")
        self.pv_scaling_worst = _gauge("predbat_pv_scaling_worst", "PV calibration worst-day scaling factor")
        self.pv_scaling_best = _gauge("predbat_pv_scaling_best", "PV calibration best-day scaling factor")
        self.pv_scaling_total = _gauge("predbat_pv_scaling_total", "PV calibration total adjustment factor")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_instance = None


def metrics():
    """Return the singleton :class:`PredbatMetrics` instance."""
    global _instance
    if _instance is None:
        _instance = PredbatMetrics()
    return _instance


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def record_api_call(service, success=True, reason=None):
    """One-liner for API client instrumentation.

    Args:
        service: Service name (e.g. 'givenergy', 'octopus', 'solcast').
        success: Whether the call succeeded.
        reason: Failure reason -- one of ``auth_error``, ``rate_limit``,
                ``connection_error``, ``server_error``, ``decode_error``,
                ``client_error``.
    """
    m = metrics()
    m.api_requests_total.labels(service=service).inc()
    if success:
        m.api_last_success_timestamp.labels(service=service).set_to_current_time()
    else:
        m.api_failures_total.labels(service=service, reason=reason or "unknown").inc()


# ---------------------------------------------------------------------------
# /metrics HTTP handler (for aiohttp web component)
# ---------------------------------------------------------------------------

async def metrics_handler(request):
    """Serve Prometheus exposition format at ``/metrics``."""
    from aiohttp import web

    if not PROMETHEUS_AVAILABLE:
        return web.Response(status=503, text="prometheus_client not installed")
    return web.Response(
        text=generate_latest().decode("utf-8"),
        content_type="text/plain",
    )
