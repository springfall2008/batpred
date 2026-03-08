# fmt: off
# pylint: disable=line-too-long
"""Prometheus metrics for PredBat.

Defines all metrics emitted by the PredBat codebase. Each component
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
        # -- Currency (plain string, not a Prometheus metric) ------------------
        self.currency_symbol = "\u00A3"  # default £, overridden by PredBat at runtime

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
        self.charge_rate_kw = _gauge("predbat_charge_rate_kw", "Current max charge rate in kW")
        self.discharge_rate_kw = _gauge("predbat_discharge_rate_kw", "Current max discharge rate in kW")
        self.inverter_register_writes_total = _counter("predbat_inverter_register_writes_total", "Total inverter register writes")
        self.grid_power = _gauge("predbat_grid_power", "Current grid power in kW (positive for import, negative for export)")
        self.load_power = _gauge("predbat_load_power", "Current load power in kW")
        self.pv_power = _gauge("predbat_pv_power", "Current PV power in kW")
        self.battery_power = _gauge("predbat_battery_power", "Current battery power in kW (positive for discharge, negative for charge)")

        # -- Energy today ------------------------------------------------------
        self.load_today_kwh = _gauge("predbat_load_today_kwh", "Load energy today in kWh")
        self.import_today_kwh = _gauge("predbat_import_today_kwh", "Import energy today in kWh")
        self.export_today_kwh = _gauge("predbat_export_today_kwh", "Export energy today in kWh")
        self.pv_today_kwh = _gauge("predbat_pv_today_kwh", "PV energy today in kWh")
        self.data_age_days = _gauge("predbat_data_age_days", "Age of load data in days")

        # -- Cost & savings ----------------------------------------------------
        self.cost_today = _gauge("predbat_cost_today", "Cost today in currency units")
        self.savings_today_pvbat = _gauge("predbat_savings_today_pvbat", "PV/Battery system savings yesterday")
        self.savings_today_actual = _gauge("predbat_savings_today_actual", "Actual cost yesterday")
        self.savings_today_predbat = _gauge("predbat_savings_today_predbat", "PredBat savings yesterday")

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


    def to_dict(self):
        """Return current metric values as a plain dict for the dashboard."""
        def _val(metric):
            """Read current value from a prometheus Gauge/Counter."""
            if isinstance(metric, _NoOpMetric):
                return 0
            try:
                return metric._value.get()
            except AttributeError:
                return 0

        def _labeled(metric):
            """Read all label combinations from a labeled metric."""
            if isinstance(metric, _NoOpMetric):
                return {}
            try:
                label_names = metric._labelnames
                return {
                    str(dict(zip(label_names, label_values))): child._value.get()
                    for label_values, child in metric._metrics.items()
                }
            except AttributeError:
                return {}

        def _api_services(m):
            """Build a pre-aggregated {service: {requests, failures, last_success}} dict."""
            out = {}
            if not isinstance(m.api_requests_total, _NoOpMetric):
                try:
                    for label_values, child in m.api_requests_total._metrics.items():
                        svc = label_values[0]
                        out.setdefault(svc, {"requests": 0, "failures": 0, "last_success": 0})
                        out[svc]["requests"] += child._value.get()
                except AttributeError:
                    pass
            if not isinstance(m.api_last_success_timestamp, _NoOpMetric):
                try:
                    for label_values, child in m.api_last_success_timestamp._metrics.items():
                        svc = label_values[0]
                        out.setdefault(svc, {"requests": 0, "failures": 0, "last_success": 0})
                        out[svc]["last_success"] = child._value.get()
                except AttributeError:
                    pass
            if not isinstance(m.api_failures_total, _NoOpMetric):
                try:
                    for label_values, child in m.api_failures_total._metrics.items():
                        svc = label_values[0]
                        out.setdefault(svc, {"requests": 0, "failures": 0, "last_success": 0})
                        out[svc]["failures"] += child._value.get()
                except AttributeError:
                    pass
            return out

        return {
            # Health
            "up": _labeled(self.up),
            "errors_total": _labeled(self.errors_total),
            "last_update_timestamp": _val(self.last_update_timestamp),
            "config_valid": _val(self.config_valid),
            "config_warnings": _val(self.config_warnings),
            # Plan
            "plan_valid": _val(self.plan_valid),
            "plan_age_minutes": _val(self.plan_age_minutes),
            # Battery
            "battery_soc_percent": _val(self.battery_soc_percent),
            "battery_soc_kwh": _val(self.battery_soc_kwh),
            "battery_max_kwh": _val(self.battery_max_kwh),
            "charge_rate_kw": _val(self.charge_rate_kw),
            "discharge_rate_kw": _val(self.discharge_rate_kw),
            "battery_power": _val(self.battery_power),
            "grid_power": _val(self.grid_power),
            "load_power": _val(self.load_power),
            "pv_power": _val(self.pv_power),
            # Energy
            "load_today_kwh": _val(self.load_today_kwh),
            "import_today_kwh": _val(self.import_today_kwh),
            "export_today_kwh": _val(self.export_today_kwh),
            "pv_today_kwh": _val(self.pv_today_kwh),
            "data_age_days": _val(self.data_age_days),
            # Currency symbol
            "currency_symbol": self.currency_symbol,
            # Cost
            "cost_today": _val(self.cost_today),
            "savings_today_pvbat": _val(self.savings_today_pvbat),
            "savings_today_actual": _val(self.savings_today_actual),
            "savings_today_predbat": _val(self.savings_today_predbat),
            # API (pre-aggregated per service - avoids JS key-parsing complexity)
            "api_services": _api_services(self),
            # Solar
            "solcast_api_limit": _val(self.solcast_api_limit),
            "solcast_api_used": _val(self.solcast_api_used),
            "solcast_api_remaining": _val(self.solcast_api_remaining),
            "pv_scaling_worst": _val(self.pv_scaling_worst),
            "pv_scaling_best": _val(self.pv_scaling_best),
            "pv_scaling_total": _val(self.pv_scaling_total),
        }


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


async def metrics_json_handler(request):
    """Serve metrics as JSON at ``/metrics/json``."""
    import json
    from aiohttp import web

    if not PROMETHEUS_AVAILABLE:
        return web.Response(
            status=503,
            text='{"error":"prometheus_client not installed"}',
            content_type="application/json",
        )
    return web.Response(
        text=json.dumps(metrics().to_dict()),
        content_type="application/json",
    )
