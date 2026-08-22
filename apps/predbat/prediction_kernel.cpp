// -----------------------------------------------------------------------------
// Predbat Home Battery System
// Copyright Trefor Southwell 2026 - All Rights Reserved
// This application maybe used for personal use only and not for commercial use
// -----------------------------------------------------------------------------
//
// C++ prediction kernel - a fast mirror of Prediction.run_prediction() in prediction.py.
//
// PARITY RULE: This file must produce bit-identical results to the Python engine
// for the scenarios it supports (see prediction_kernel.py capability check).
// Any behavioural change to the hot loop in prediction.py MUST be mirrored here
// and PK_PARITY_REVISION below AND KERNEL_PARITY_REVISION in prediction_kernel.py must
// both be bumped. A mismatch at load time disables the kernel (Python fallback).
// Section comments reference the prediction.py line anchors they mirror.
//
// Build: bash apps/predbat/build_kernel.sh (g++/clang, C++17, no dependencies)

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <limits>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <map>
#include <memory>
#include <mutex>
// std::system_error, thrown when the system refuses a thread. libc++ pulls this in through <thread>,
// libstdc++ does not, so leaving it implicit builds on macOS and fails on GCC.
#include <system_error>
#include <thread>
#include <vector>

#if defined(__unix__) || defined(__APPLE__)
#include <pthread.h>
#endif

// ABI 4: PkScenario::soc_out may be null, and pk_run_batch was added. The null is the reason this had
// to move - Python now passes no SoC buffer for every cached run, and an ABI 3 binary writes to it
// unconditionally, so loading one against this Python segfaults on the first prediction rather than
// falling back. Bumping makes the loader reject it and use the Python engine, which is the whole
// point of the check.
#define PK_ABI_VERSION 5
#define PK_PARITY_REVISION 9
#define PK_MAX_CARS 8
#define PK_RUN_EVERY 5 // const.py RUN_EVERY

namespace {

// Mirror of CPython round(x, n): correctly-rounded decimal rounding (ties to even).
// snprintf performs a correctly-rounded binary->decimal conversion and strtod a
// correctly-rounded decimal->binary conversion, matching CPython's _Py_dg_dtoa path.
double round_py(double value, int ndigits)
{
    if (!std::isfinite(value)) {
        return value;
    }
    char buf[64];
    snprintf(buf, sizeof(buf), "%.*f", ndigits, value);
    return strtod(buf, nullptr);
}

// Mirror of utils.py calc_percent_limit() for a scalar value
int32_t calc_percent_limit(double charge_limit, double soc_max)
{
    if (soc_max <= 0) {
        return 0;
    }
    return std::min(static_cast<int32_t>((charge_limit / soc_max * 100.0) + 0.5), 100);
}

// The SoC percent that Python's round(soc, 1) -> calc_percent_limit() pair produces.
//
// The 1dp rounding is not a modelling decision - it is there so utils.get_charge_rate_curve_cached's
// lru_cache hits - but rounding before the percent is taken occasionally moves the integer, so the
// kernel has to reproduce it exactly.
int32_t percent_via_round(double soc, double soc_max)
{
    return calc_percent_limit(round_py(soc, 1), soc_max);
}

// Smallest SoC whose percent_via_round() reaches p, found by bisecting the double's bit pattern.
//
// Non-negative doubles compare in the same order as their bit patterns read as integers, so this is
// an exact bisection over every representable value in the bracket rather than an approximation:
// the boundary it returns is the true one, to the last bit.
double smallest_soc_for_percent(double soc_max, int32_t p, double hi_d)
{
    if (percent_via_round(0.0, soc_max) >= p) {
        return 0.0;
    }
    if (percent_via_round(hi_d, soc_max) < p) {
        return std::numeric_limits<double>::infinity();
    }
    uint64_t lo_bits, hi_bits;
    double lo_d = 0.0;
    std::memcpy(&lo_bits, &lo_d, sizeof(lo_bits));
    std::memcpy(&hi_bits, &hi_d, sizeof(hi_bits));
    while (hi_bits - lo_bits > 1) {
        const uint64_t mid_bits = lo_bits + (hi_bits - lo_bits) / 2;
        double mid_d;
        std::memcpy(&mid_d, &mid_bits, sizeof(mid_d));
        if (percent_via_round(mid_d, soc_max) >= p) {
            hi_bits = mid_bits;
        } else {
            lo_bits = mid_bits;
        }
    }
    double result;
    std::memcpy(&result, &hi_bits, sizeof(result));
    return result;
}

// Boundaries of the 101 SoC percent buckets, so the hot loop can map SoC to percent with a binary
// search over doubles instead of a round_py (snprintf + strtod, ~96ns and the most expensive thing
// in the loop - and unusable under threading, since it serialises on the C library's global locale).
// out[p] is the smallest SoC that reads as p percent; out[0] is unused.
void build_soc_percent_thresholds(std::vector<double> &out, double soc_max)
{
    out.assign(101, 0.0);
    if (soc_max <= 0) {
        return;
    }
    const double hi = soc_max * 2.0 + 1.0;
    for (int32_t p = 1; p <= 100; p++) {
        out[p] = smallest_soc_for_percent(soc_max, p, hi);
    }
    // upper_bound needs a non-decreasing sequence; the map is monotone so this only guards against
    // an unreachable percent leaving an infinity in front of a later finite entry
    for (int32_t p = 2; p <= 100; p++) {
        if (out[p] < out[p - 1]) {
            out[p] = out[p - 1];
        }
    }
}

// Per-plan static context passed from Python, arrays are all n_steps long
// (index k covers relative minute k*5) unless stated otherwise.
// Field order MUST match the ctypes Structure in prediction_kernel.py exactly.
struct PkContext {
    const double *rate_import;        // import rate per step
    const double *rate_export;        // export rate per step
    const double *alert_keep;         // alert keep value per step
    const double *pv;                 // PV forecast kWh per step (central)
    const double *load;               // load kWh per step (central)
    const double *pv10;               // PV forecast kWh per step (PV10)
    const double *load10;             // load kWh per step (PV10)
    const double *pv90;               // PV forecast kWh per step (PV90)
    const double *load90;             // load kWh per step (PV90)
    const double *temp_charge_cap;    // temperature rate cap base (soc_max*adjust/60) per step, charge curve
    const double *temp_discharge_cap; // temperature rate cap base per step, discharge curve
    const int32_t *io_flag;           // io_adjusted flag per step
    const double *charge_curve;       // 101 entries, raw charge power curve multiplier by SoC percent
    const double *discharge_curve;    // 101 entries, raw discharge power curve multiplier by SoC percent
    const double *carbon;             // carbon intensity per step
    const double *gas_rate;           // gas rate per step, pre-scaled by iboost_gas_scale
    const double *iboost_plan_load;   // iBoost plan load kW per step (in_iboost_slot)
    const double *car_load_flat;      // car charging load kW, num_cars * n_steps (in_car_slot)
    const double *car_rate_flat;      // car premium rate, num_cars * n_steps (in_car_slot)

    double soc_kw;
    double soc_max;
    double reserve;
    double best_soc_min;
    double best_soc_keep;
    double best_soc_keep_weight;
    double battery_loss;
    double battery_loss_discharge;
    double inverter_loss;
    double inverter_freeze_export_discharge_rate; // per-minute rate (multiplied by step in the kernel), residual battery-side discharge entering the AC balance during Freeze Export
    double inverter_limit;    // per-minute rate (multiplied by step in the kernel)
    double export_limit;      // per-minute rate
    double pv_ac_limit;       // per-minute rate
    double battery_rate_min;
    double battery_rate_max_charge;
    double battery_rate_max_charge_dc;
    double battery_rate_max_discharge;
    double battery_rate_max_export;
    double battery_rate_max_scaling;
    double battery_rate_max_scaling10;
    double battery_rate_max_scaling_discharge;
    double charge_rate_now;
    double discharge_rate_now;
    double rate_max;
    double cost_today_sofar;
    double carbon_today_sofar;
    double export_today_now;
    double iboost_today;
    double car_charging_loss;
    double car_charging_limit[PK_MAX_CARS];
    double car_charging_soc[PK_MAX_CARS];
    double iboost_max_energy;
    double iboost_max_power;
    double iboost_min_power;
    double iboost_min_soc;
    double iboost_rate_threshold;
    double iboost_rate_threshold_export;

    int32_t n_steps;
    int32_t minutes_now;
    int32_t forecast_minutes;
    int32_t inverter_hybrid;
    int32_t set_charge_freeze;
    int32_t set_reserve_enable;
    int32_t set_export_freeze;
    int32_t set_export_freeze_only;
    int32_t set_charge_window;
    int32_t set_export_window;
    int32_t set_discharge_during_charge;
    int32_t set_export_low_power;
    int32_t calculate_export_on_pv;
    int32_t inverter_can_charge_during_export;
    int32_t num_cars;
    int32_t car_energy_reported_load;
    int32_t car_charging_from_battery;
    int32_t carbon_enable;
    int32_t iboost_enable;
    int32_t iboost_solar;
    int32_t iboost_solar_excess;
    int32_t iboost_gas;
    int32_t iboost_gas_export;
    int32_t iboost_charging;
    int32_t iboost_prevent_discharge;
    int32_t iboost_on_export;
    int32_t has_rate_gas;
    int32_t has_iboost_plan;
};

// Per-scenario inputs; field order MUST match the ctypes Structure in prediction_kernel.py.
struct PkScenario {
    const double *charge_limit;   // kWh target per charge window
    const int32_t *charge_start;  // absolute minutes
    const int32_t *charge_end;
    const double *export_limits;  // percent per export window (99=freeze, 100=off - see EXPORT_LIMIT_FREEZE/EXPORT_LIMIT_IDLE in const.py)
    const int32_t *export_start;
    const int32_t *export_end;
    double *soc_out;              // caller-allocated, n_steps entries, filled with round(soc, 3)

    int32_t n_charge;
    int32_t n_export;
    int32_t pv_scenario;          // 0 = nominal, 1 = pv10, 2 = pv90
    int32_t end_record;
    int32_t step;
};

// Scalar results; field order MUST match the ctypes Structure in prediction_kernel.py.
struct PkResult {
    double final_metric;
    double import_kwh_battery;
    double import_kwh_house;
    double export_kwh;
    double soc_min;
    double final_soc;
    double battery_cycle;
    double metric_keep;
    double final_iboost;
    double final_carbon_g;
    double car_soc_next[PK_MAX_CARS]; // rounded to 3dp, valid when car_soc_next_valid
    double iboost_next;               // valid when iboost_enable
    int32_t soc_min_minute;
    int32_t car_soc_next_valid;
    int32_t iboost_running;
    int32_t iboost_running_solar;
    int32_t iboost_running_full;
};

// One scenario in a pk_run_batch() call; field order MUST match the ctypes Structure in
// prediction_kernel.py.
//
// This is deliberately a separate type from PkScenario rather than extra fields on it: pk_run keeps
// its existing layout, so a kernel binary predating the batch entry point still loads and runs, and
// Python falls back to looping pk_run when pk_run_batch is absent.
//
// soc_out may be null. A batched scenario is always a cached (non-saving) run, whose caller discards
// the per-minute SoC series, and materialising one buffer per job would cost ~84MB on a large batch.
// The one thing callers do want from the series is the SoC range across a charge window, so the
// kernel tracks that inline over [soc_range_start_step, soc_range_end_step] instead - set
// soc_range_start_step < 0 to skip it.
struct PkBatchJob {
    const double *charge_limit;
    const int32_t *charge_start;
    const int32_t *charge_end;
    const double *export_limits;
    const int32_t *export_start;
    const int32_t *export_end;
    double *soc_out; // optional, null to skip

    int32_t n_charge;
    int32_t n_export;
    int32_t pv_scenario;
    int32_t end_record;
    int32_t step;
    int32_t soc_range_start_step; // <0 to skip the SoC range scan
    int32_t soc_range_end_step;   // inclusive
};

// Result for one batched scenario; field order MUST match the ctypes Structure in
// prediction_kernel.py. soc_range_min/max mirror thread_run_prediction_charge_min_max's scan over
// predict_soc and are only meaningful when the job asked for a range.
struct PkBatchResult {
    PkResult result;
    double soc_range_min;
    double soc_range_max;
    int32_t status; // 0 = ok, matching pk_run's return codes otherwise
    int32_t pad;
};

// Reusable per-simulation scratch buffers.
//
// build_window_membership assigns n_steps entries and the clip helpers clear-then-push, so all of
// these can be reused across scenarios without reallocating - which matters a lot under threading:
// each scenario would otherwise heap-allocate ~70KB, and 1200 of those per batch put every worker
// thread in contention on the allocator. Measured 4 threads at 0.6x of serial before this, because
// the simulation is cheap enough that malloc, not arithmetic, was the bottleneck.
//
// See thread_scratch() below: one of these now serves every batch a thread runs, not one per call.

// Counts PkScratch constructions for the life of the library, for pk_scratch_construct_count(). The
// suite asserts this stops growing with the number of batches rather than trusting the reuse.
std::atomic<int64_t> g_scratch_constructions{0};

// Counts worker threads started for the life of the library, for pk_worker_thread_count(). Thread
// creation measured 7.5us each and a benchmark plan used to start 201,545 of them - 1.5s of a 26s
// plan, which is what the pool exists to stop paying. The suite asserts this stops growing.
std::atomic<int64_t> g_worker_threads_started{0};

struct PkScratch {
    std::vector<int32_t> charge_member, export_member;
    std::vector<int32_t> clipped_start, clipped_end;
    std::vector<double> clipped_limit;

    PkScratch()
    {
        g_scratch_constructions.fetch_add(1, std::memory_order_relaxed);
    }
};

// One scratch per thread, reused for that thread's lifetime.
//
// Every field is fully rewritten before it is read on each run - build_window_membership assigns
// exactly n_steps entries and the clip helpers clear-then-push - which is the same invariant that
// already let one scratch serve a whole stride within a batch. Extending it across calls means a
// thread grows these buffers once instead of once per call: a benchmark plan issues ~25,000 batch
// calls, and at a median of 6 jobs each the ~70KB allocation was the same order as the simulation it
// wrapped.
//
// Thread-local rather than per-context because nothing here survives a simulation, so it never needs
// to belong to a particular context. A thread that later runs a context with a different n_steps
// simply has the vectors reassigned to the new length.
static PkScratch &thread_scratch()
{
    static thread_local PkScratch scratch;
    return scratch;
}

// Deep-copied context storage so Python-side buffers can be freed after create
struct ContextStore {
    std::vector<double> rate_import, rate_export, alert_keep;
    std::vector<double> pv, load, pv10, load10, pv90, load90;
    std::vector<double> temp_charge_cap, temp_discharge_cap;
    std::vector<int32_t> io_flag;
    std::vector<double> charge_curve, discharge_curve;
    std::vector<double> carbon, gas_rate, iboost_plan_load;
    std::vector<double> car_load_flat, car_rate_flat;
    std::vector<double> soc_percent_threshold; // see build_soc_percent_thresholds
    PkContext ctx;
};

std::mutex g_context_mutex;
std::map<int64_t, std::unique_ptr<ContextStore>> g_contexts;
int64_t g_next_handle = 1;

// Mirror of prediction.py get_diff()
inline double get_diff(double battery_draw, double pv_dc, double pv_ac, double load_yesterday, double inverter_loss, double inverter_loss_recp)
{
    double battery_balance = battery_draw + pv_dc;
    battery_balance = (battery_balance > 0) ? battery_balance * inverter_loss : battery_balance * inverter_loss_recp;
    return load_yesterday - battery_balance - pv_ac;
}

// Mirror of prediction.py get_total_inverted()
inline double get_total_inverted(double battery_draw, double pv_dc, double pv_ac, double inverter_loss, bool inverter_hybrid)
{
    double battery_balance = battery_draw + pv_dc;
    double total_inverted = (battery_balance > 0) ? battery_balance : std::fabs(battery_balance) / inverter_loss;
    if (inverter_hybrid) {
        total_inverted += pv_ac / inverter_loss;
    }
    return total_inverted;
}

// Mirror of utils.py get_charge_rate_curve_cached()/get_discharge_rate_curve_cached().
// soc_key is passed exactly as Python does (usually round(soc, 1), but raw soc for the DC-rate lookups).
// temp_cap_base is the pre-computed find_battery_temperature_cap() value before the min with max_rate.
inline double rate_curve_pct(int32_t soc_percent, double rate_setting, double rate_max, double temp_cap_base, const double *curve, double rate_min)
{
    double max_rate = rate_max * curve[soc_percent];
    double max_rate_cap = std::min(temp_cap_base, rate_max);
    max_rate = std::min(max_rate, max_rate_cap);
    return std::max(std::min(rate_setting, max_rate), rate_min);
}

inline double rate_curve(double soc_key, double rate_setting, double rate_max, double temp_cap_base, const double *curve, double soc_max, double rate_min)
{
    int32_t soc_percent = calc_percent_limit(soc_key, soc_max);
    double max_rate = rate_max * curve[soc_percent];
    double max_rate_cap = std::min(temp_cap_base, rate_max);
    max_rate = std::min(max_rate, max_rate_cap);
    return std::max(std::min(rate_setting, max_rate), rate_min);
}

// Build per-step window membership, mirroring Prediction.find_charge_window_optimised():
// dict keyed by absolute minute stepping 5 from each window start, last window wins,
// looked up at minute_absolute = minutes_now + k*5 (so misaligned windows never match).
// Mirrors remove_intersecting_windows() in utils.py, which the Python engine applies before
// simulating. Charge windows that collide with an enabled export window are trimmed, and a window
// with an export landing inside it is split in two. Two rules are easy to get wrong and are pinned
// by tests: a window that was never clipped survives whatever its length, while a remnant clipping
// itself created is dropped below 5 minutes; and windows that merely touch at a boundary overlap
// arithmetically but remove nothing, so they must not count as clipped.
//
// PARITY: any change here must be mirrored in utils.remove_intersecting_windows and vice versa.
static void clip_intersecting_charge_windows(std::vector<int32_t> &out_start, std::vector<int32_t> &out_end, std::vector<double> &out_limit, int32_t n_charge, const int32_t *charge_start, const int32_t *charge_end, const double *charge_limit, int32_t n_export, const int32_t *export_start,
                                             const int32_t *export_end, const double *export_limits)
{
    // Enabled export windows only - the sole candidates for clipping anything - in start order
    std::vector<std::pair<int32_t, int32_t>> export_active;
    export_active.reserve(n_export);
    for (int32_t n = 0; n < n_export; n++) {
        if (export_limits[n] < 100.0) {
            export_active.emplace_back(export_start[n], export_end[n]);
        }
    }
    std::sort(export_active.begin(), export_active.end());

    out_start.clear();
    out_end.clear();
    out_limit.clear();
    out_start.reserve(n_charge);
    out_end.reserve(n_charge);
    out_limit.reserve(n_charge);

    if (export_active.empty()) {
        for (int32_t n = 0; n < n_charge; n++) {
            out_start.push_back(charge_start[n]);
            out_end.push_back(charge_end[n]);
            out_limit.push_back(charge_limit[n]);
        }
        return;
    }

    for (int32_t n = 0; n < n_charge; n++) {
        int32_t start = charge_start[n];
        int32_t end = charge_end[n];
        const double limit = charge_limit[n];

        if (!(limit > 0.0)) {
            // A disabled charge window can never be clipped
            out_start.push_back(start);
            out_end.push_back(end);
            out_limit.push_back(limit);
            continue;
        }

        bool clipped = false;
        for (const auto &dw : export_active) {
            const int32_t dstart = dw.first;
            const int32_t dend = dw.second;
            if ((dstart < end) && (dend >= start)) {
                if (dstart <= start) {
                    if (start != dend) {
                        start = dend;
                        clipped = true;
                    }
                } else if (dend >= end) {
                    if (end != dstart) {
                        end = dstart;
                        clipped = true;
                    }
                } else {
                    // Two segments - emit the head now, carry on clipping the tail
                    if ((dstart - start) >= 5) {
                        out_start.push_back(start);
                        out_end.push_back(dstart);
                        out_limit.push_back(limit);
                    }
                    start = dend;
                    clipped = true;
                }
            }
        }

        if (!clipped || ((end - start) >= 5)) {
            out_start.push_back(start);
            out_end.push_back(end);
            out_limit.push_back(limit);
        }
    }
}

void build_window_membership(std::vector<int32_t> &member, int32_t n_windows, const int32_t *starts, const int32_t *ends, const double *limits, bool is_export, int32_t minutes_now, int32_t n_steps)
{
    member.assign(n_steps, -1);
    for (int32_t window_n = 0; window_n < n_windows; window_n++) {
        if (is_export ? !(limits[window_n] < 100.0) : !(limits[window_n] > 0.0)) {
            continue;
        }
        for (int32_t m = starts[window_n]; m < ends[window_n]; m += 5) {
            int32_t rel = m - minutes_now;
            if (rel >= 0 && (rel % 5) == 0) {
                int32_t k = rel / 5;
                if (k < n_steps) {
                    member[k] = window_n;
                }
            }
        }
    }
}

} // namespace

extern "C" {

// ABI version of the shared library, checked by the Python loader
int32_t pk_abi_version(void)
{
    return PK_ABI_VERSION;
}

// Parity revision, must match KERNEL_PARITY_REVISION in prediction_kernel.py
int32_t pk_parity_revision(void)
{
    return PK_PARITY_REVISION;
}

// Create a per-plan context; deep-copies all arrays. Returns a handle (>0) or 0 on error.
int64_t pk_context_create(const PkContext *in)
{
    if (!in || in->n_steps <= 0 || in->num_cars < 0 || in->num_cars > PK_MAX_CARS) {
        return 0;
    }
    auto store = std::make_unique<ContextStore>();
    build_soc_percent_thresholds(store->soc_percent_threshold, in->soc_max);
    size_t n = static_cast<size_t>(in->n_steps);
    store->rate_import.assign(in->rate_import, in->rate_import + n);
    store->rate_export.assign(in->rate_export, in->rate_export + n);
    store->alert_keep.assign(in->alert_keep, in->alert_keep + n);
    store->pv.assign(in->pv, in->pv + n);
    store->load.assign(in->load, in->load + n);
    store->pv10.assign(in->pv10, in->pv10 + n);
    store->load10.assign(in->load10, in->load10 + n);
    store->pv90.assign(in->pv90, in->pv90 + n);
    store->load90.assign(in->load90, in->load90 + n);
    store->temp_charge_cap.assign(in->temp_charge_cap, in->temp_charge_cap + n);
    store->temp_discharge_cap.assign(in->temp_discharge_cap, in->temp_discharge_cap + n);
    store->io_flag.assign(in->io_flag, in->io_flag + n);
    store->charge_curve.assign(in->charge_curve, in->charge_curve + 101);
    store->discharge_curve.assign(in->discharge_curve, in->discharge_curve + 101);
    store->carbon.assign(in->carbon, in->carbon + n);
    store->gas_rate.assign(in->gas_rate, in->gas_rate + n);
    store->iboost_plan_load.assign(in->iboost_plan_load, in->iboost_plan_load + n);
    size_t n_car = static_cast<size_t>(in->num_cars) * n;
    if (n_car > 0) {
        store->car_load_flat.assign(in->car_load_flat, in->car_load_flat + n_car);
        store->car_rate_flat.assign(in->car_rate_flat, in->car_rate_flat + n_car);
    }
    store->ctx = *in;
    store->ctx.rate_import = store->rate_import.data();
    store->ctx.rate_export = store->rate_export.data();
    store->ctx.alert_keep = store->alert_keep.data();
    store->ctx.pv = store->pv.data();
    store->ctx.load = store->load.data();
    store->ctx.pv10 = store->pv10.data();
    store->ctx.load10 = store->load10.data();
    store->ctx.pv90 = store->pv90.data();
    store->ctx.load90 = store->load90.data();
    store->ctx.temp_charge_cap = store->temp_charge_cap.data();
    store->ctx.temp_discharge_cap = store->temp_discharge_cap.data();
    store->ctx.io_flag = store->io_flag.data();
    store->ctx.charge_curve = store->charge_curve.data();
    store->ctx.discharge_curve = store->discharge_curve.data();
    store->ctx.carbon = store->carbon.data();
    store->ctx.gas_rate = store->gas_rate.data();
    store->ctx.iboost_plan_load = store->iboost_plan_load.data();
    store->ctx.car_load_flat = store->car_load_flat.data();
    store->ctx.car_rate_flat = store->car_rate_flat.data();

    std::lock_guard<std::mutex> lock(g_context_mutex);
    int64_t handle = g_next_handle++;
    g_contexts[handle] = std::move(store);
    return handle;
}

// Free a context created by pk_context_create
void pk_context_free(int64_t handle)
{
    std::lock_guard<std::mutex> lock(g_context_mutex);
    g_contexts.erase(handle);
}

// Run one prediction scenario. Returns 0 on success, non-zero on error.
// Mirrors the hot loop of Prediction.run_prediction() (prediction.py:385-1200) for the
// supported configuration: save=None, debug off, step=5 (cars, iBoost and carbon included).
// Look up a context by handle, or null when the handle is unknown. The returned pointer stays valid
// as long as the caller does not free the context concurrently, which no caller does: contexts are
// created once per plan and released by a weakref finaliser once the Prediction is gone.
static const ContextStore *lookup_context(int64_t handle)
{
    std::lock_guard<std::mutex> lock(g_context_mutex);
    auto it = g_contexts.find(handle);
    if (it == g_contexts.end()) {
        return nullptr;
    }
    return it->second.get();
}

// Simulate one scenario against an already-resolved context.
//
// Shared by pk_run and pk_run_batch, and safe to call concurrently on one context: c is const, every
// working value below is a local, and results go only to the caller's own out/soc_range slots.
//
// soc_out may be null (see PkBatchJob). soc_range_min/max are optional out-params: when non-null and
// soc_range_start_step >= 0, they receive the min/max of the rounded SoC over that inclusive step
// range, mirroring the predict_soc scan in Prediction.thread_run_prediction_charge_min_max.
static int32_t pk_run_one(const ContextStore *store, const PkScenario *s, PkResult *out, int32_t soc_range_start_step, int32_t soc_range_end_step, double *soc_range_min_out, double *soc_range_max_out, PkScratch &scratch)
{
    const PkContext *c = &store->ctx;
    const std::vector<double> &soc_pct_threshold = store->soc_percent_threshold;
    if (!s || !out || s->step != 5) {
        return 2;
    }
    // Seeded to match Prediction.thread_run_prediction_charge_min_max, which starts min at soc_max
    // and max at 0 so a range covering no recorded step leaves them in that state
    double soc_range_min = c->soc_max;
    double soc_range_max = 0.0;
    const bool want_soc_range = soc_range_start_step >= 0;

    const int32_t pv_scenario = s->pv_scenario;
    const bool is_pv10 = pv_scenario == 1;
    const bool is_pv90 = pv_scenario == 2;
    const int32_t step = s->step;
    const int32_t n_steps = c->n_steps;
    const bool inverter_hybrid = c->inverter_hybrid != 0;

    // Window membership - prediction.py:494-495 / find_charge_window_optimised
    std::vector<int32_t> &charge_window_optimised = scratch.charge_member;
    std::vector<int32_t> &export_window_optimised = scratch.export_member;

    // The caller hands over the raw charge windows; clipping them against the export windows used to
    // be done in Python on every simulation, which cost more than the simulation itself
    std::vector<int32_t> &clipped_start = scratch.clipped_start;
    std::vector<int32_t> &clipped_end = scratch.clipped_end;
    std::vector<double> &clipped_limit = scratch.clipped_limit;
    clip_intersecting_charge_windows(clipped_start, clipped_end, clipped_limit, s->n_charge, s->charge_start, s->charge_end, s->charge_limit, s->n_export, s->export_start, s->export_end, s->export_limits);
    const int32_t n_charge_clipped = static_cast<int32_t>(clipped_start.size());

    build_window_membership(charge_window_optimised, n_charge_clipped, clipped_start.data(), clipped_end.data(), clipped_limit.data(), false, c->minutes_now, n_steps);
    build_window_membership(export_window_optimised, s->n_export, s->export_start, s->export_end, s->export_limits, true, c->minutes_now, n_steps);

    // Initial state - prediction.py:435-490
    double soc = c->soc_kw;
    double soc_min = c->soc_max;
    int32_t soc_min_minute = c->minutes_now;
    double export_kwh = c->export_today_now;
    double import_kwh_battery = 0;
    double import_kwh_house = 0;
    double battery_cycle = 0;
    double metric_keep = 0;
    double metric = c->cost_today_sofar;
    double carbon_g = c->carbon_today_sofar;
    double iboost_today_kwh = c->iboost_today;
    bool four_hour_rule = true;
    bool record = true;
    double final_soc = soc;
    double final_metric = metric;
    double final_battery_cycle = battery_cycle;
    double final_metric_keep = metric_keep;
    double final_iboost_kwh = iboost_today_kwh;
    double final_carbon_g = carbon_g;
    double final_import_kwh_battery = import_kwh_battery;
    double final_import_kwh_house = import_kwh_house;
    double final_export_kwh = export_kwh;
    double charge_rate_now = c->charge_rate_now;
    double discharge_rate_now = c->discharge_rate_now;
    const bool car_enable = c->num_cars > 0;
    double car_soc[PK_MAX_CARS] = {0};
    for (int32_t car_n = 0; car_n < c->num_cars; car_n++) {
        car_soc[car_n] = c->car_charging_soc[car_n];
    }
    double car_soc_next[PK_MAX_CARS] = {0};
    int32_t car_soc_next_valid = 0;
    double iboost_next = 0;
    int32_t iboost_running = 0;
    int32_t iboost_running_solar = 0;
    int32_t iboost_running_full = 0;

    // Battery behaviour - prediction.py:501-521
    const double inverter_loss = c->inverter_loss;
    const double inverter_loss_ac = inverter_hybrid ? inverter_loss : 1.0;
    const double inverter_loss_recp = 1.0 / inverter_loss;
    const double inverter_limit = c->inverter_limit * step;
    const double export_limit = c->export_limit * step;
    const double pv_ac_limit = c->pv_ac_limit * step;
    const double reserve = c->reserve;
    const double soc_max = c->soc_max;
    const int32_t reserve_percent = calc_percent_limit(reserve, soc_max);
    const double battery_loss = c->battery_loss;
    const double battery_loss_discharge = c->battery_loss_discharge;
    const double best_soc_keep_weight = c->best_soc_keep_weight;
    const double best_soc_keep_orig = c->best_soc_keep;
    const double battery_rate_max_charge = c->battery_rate_max_charge;
    const double battery_rate_max_charge_dc = c->battery_rate_max_charge_dc;
    const double battery_rate_max_discharge = c->battery_rate_max_discharge;
    const double battery_rate_max_export = c->battery_rate_max_export;
    const double battery_rate_min = c->battery_rate_min;
    const double inverter_freeze_export_discharge_rate = c->inverter_freeze_export_discharge_rate;
    // PV10 de-rating of the charge rate - prediction.py:587-592. PV90 is the upside case, no de-rate.
    const double battery_rate_max_scaling = is_pv10 ? c->battery_rate_max_scaling10 : c->battery_rate_max_scaling;
    const double battery_rate_max_scaling_discharge = c->battery_rate_max_scaling_discharge;
    const double *pv_step = is_pv10 ? c->pv10 : (is_pv90 ? c->pv90 : c->pv);
    const double *load_step = is_pv10 ? c->load10 : (is_pv90 ? c->load90 : c->load);

    // Simulate each forward step - prediction.py:570-1200
    for (int32_t k = 0; k < n_steps; k++) {
        const int32_t minute = k * step;
        const int32_t minute_absolute = minute + c->minutes_now;
        double reserve_expected = reserve;

        // Rates - prediction.py:577-580
        double import_rate = c->rate_import[k];
        if (c->io_flag[k] && is_pv10 && minute > 30) {
            import_rate = c->rate_max; // Assume in worst case that slot goes away and max rate applies
        }
        const double export_rate = c->rate_export[k];

        // Alert - prediction.py:583
        const double alert_keep = c->alert_keep[k];

        // Four hour rule scaling - prediction.py:589-592
        double keep_minute_scaling = four_hour_rule ? std::min(minute / 240.0, 1.0) * best_soc_keep_weight : best_soc_keep_weight;

        // Alert keep - prediction.py:595-600
        double best_soc_keep = best_soc_keep_orig;
        if (alert_keep > 0) {
            keep_minute_scaling = std::max(keep_minute_scaling, 10.0);
            best_soc_keep = std::max(best_soc_keep, std::min(alert_keep / 100.0 * soc_max, soc_max));
        }

        // Find charge & discharge windows - prediction.py:602-607
        const int32_t charge_window_n = charge_window_optimised[k];
        const int32_t export_window_n = export_window_optimised[k];
        const bool charge_window_active = charge_window_n >= 0;
        const bool export_window_active = export_window_n >= 0;
        const double export_limit_now = export_window_active ? s->export_limits[export_window_n] : 100.0;

        // Find charge limit - prediction.py:609-620
        double charge_limit_n = 0;
        if (charge_window_active) {
            charge_limit_n = clipped_limit[charge_window_n];
            if (c->set_charge_freeze && (calc_percent_limit(charge_limit_n, soc_max) == reserve_percent)) {
                // Charge freeze via reserve
                charge_limit_n = std::max(soc, reserve);
            }
            if (c->set_reserve_enable && (soc >= charge_limit_n)) {
                reserve_expected = std::max(charge_limit_n, reserve);
            }
        }

        // Outside the recording window - prediction.py:622-624
        if (record && minute >= s->end_record) {
            record = false;
        }

        // Save SoC prediction data - prediction.py:627-628. Batched scenarios pass a null buffer
        // (their caller discards the series); the rounded value is still what feeds the SoC range,
        // so the range matches a Python scan over predict_soc exactly.
        // round_py is snprintf+strtod, ~96ns and by far the most expensive thing in this loop, so it
        // is only paid when something actually consumes the value. soc itself is never rounded - the
        // result feeds soc_out and the SoC range and nothing else - so skipping it when a batched
        // scenario wants neither cannot change the simulation.
        const bool in_soc_range = want_soc_range && k >= soc_range_start_step && k <= soc_range_end_step;
        if (s->soc_out || in_soc_range) {
            const double soc_rounded = round_py(soc, 3);
            if (s->soc_out) {
                s->soc_out[k] = soc_rounded;
            }
            if (in_soc_range) {
                if (soc_rounded < soc_range_min) {
                    soc_range_min = soc_rounded;
                }
                if (soc_rounded > soc_range_max) {
                    soc_range_max = soc_rounded;
                }
            }
        }

        // Get load and pv forecast - prediction.py:657-659
        double pv_now = pv_step[k];
        double load_yesterday = load_step[k];

        // Clip PV for AC-coupled inverters with a PV AC limit - prediction.py:664-668
        if (!inverter_hybrid && pv_ac_limit > 0 && pv_now > pv_ac_limit) {
            pv_now = pv_ac_limit;
        }

        // Modelling reset of charge/discharge rate - prediction.py:670-673
        if (c->set_charge_window || c->set_export_window) {
            charge_rate_now = battery_rate_max_charge;
            discharge_rate_now = battery_rate_max_discharge;
        }

        // Simulate car charging - prediction.py:675-702
        double car_rate_premium = 0;
        double car_amount_premium = 0;
        double car_load_energy_bypass = 0;
        if (car_enable) {
            for (int32_t car_n = 0; car_n < c->num_cars; car_n++) {
                const double car_load_now = c->car_load_flat[car_n * n_steps + k];
                if (car_load_now > 0.0) {
                    double car_load_scale = car_load_now * step / 60.0;
                    car_load_scale = car_load_scale * c->car_charging_loss;
                    car_load_scale = std::max(std::min(car_load_scale, c->car_charging_limit[car_n] - car_soc[car_n]), 0.0);
                    car_soc[car_n] = car_soc[car_n] + car_load_scale;

                    // Work out the premium rate for car charging
                    car_rate_premium = std::max(car_rate_premium, std::max(0.0, c->car_rate_flat[car_n * n_steps + k] - import_rate));

                    if (c->car_energy_reported_load) {
                        // Note: mirrors the Python engine exactly - the cumulative premium amount is added per car
                        car_amount_premium += car_load_scale / c->car_charging_loss;
                        load_yesterday += car_amount_premium;
                    } else {
                        car_load_energy_bypass += car_load_scale / c->car_charging_loss;
                    }

                    // Model not allowing the car to charge from the battery - applies regardless of
                    // car_energy_reported_load, which only controls CT-clamp house-load inclusion
                    if ((car_load_scale > 0) && (!c->car_charging_from_battery) && c->set_charge_window) {
                        discharge_rate_now = battery_rate_min; // 0
                    }
                }
            }
        }

        // iBoost - prediction.py:704-760
        bool iboost_rate_okay = true;
        double iboost_amount = 0;
        if (c->iboost_enable) {
            // Boost on energy rates
            if (import_rate > c->iboost_rate_threshold) {
                iboost_rate_okay = false;
            }
            if (export_rate > c->iboost_rate_threshold_export) {
                iboost_rate_okay = false;
            }

            // Boost on gas vs import/export rate
            if (c->iboost_gas && c->has_rate_gas) {
                if (import_rate > c->gas_rate[k]) {
                    iboost_rate_okay = false;
                }
            }
            if (c->iboost_gas_export && c->has_rate_gas) {
                if (export_rate > c->gas_rate[k]) {
                    iboost_rate_okay = false;
                }
            }

            // iBoost based on plan for given rates - prediction.py:731-733
            if (c->has_iboost_plan && (c->iboost_on_export || (export_window_n < 0))) {
                const double iboost_load = c->iboost_plan_load[k] * step / 60.0;
                iboost_amount = std::min({iboost_load, c->iboost_max_power * step, std::max(c->iboost_max_energy - iboost_today_kwh, 0.0)});
            }

            // iBoost based on Predbat charging - prediction.py:735-738
            if (c->iboost_charging && iboost_rate_okay && iboost_today_kwh < c->iboost_max_energy) {
                if (charge_window_active) {
                    iboost_amount = std::min(c->iboost_max_power * step, std::max(c->iboost_max_energy - iboost_today_kwh, 0.0));
                }
            }

            // Freeze discharge on iboost - prediction.py:740-743
            if (iboost_amount > 0 && c->iboost_prevent_discharge && c->set_charge_window) {
                discharge_rate_now = battery_rate_min; // 0
            }

            // iBoost running - prediction.py:745-747
            if (iboost_amount > 0 && minute == 0) {
                iboost_running_full = 1;
            }

            // iBoost load added - prediction.py:749-750
            load_yesterday += iboost_amount;

            // iBoost solar diversion model - prediction.py:752-759
            if (c->iboost_solar && !c->iboost_solar_excess) {
                if (iboost_rate_okay && iboost_today_kwh < c->iboost_max_energy && (pv_now > (c->iboost_min_power * step) && ((soc * 100.0 / soc_max) >= c->iboost_min_soc)) && (c->iboost_on_export || (export_window_n < 0))) {
                    const double iboost_pv_amount = std::min({pv_now, std::max(c->iboost_max_power * step - iboost_amount, 0.0), std::max(c->iboost_max_energy - iboost_today_kwh - iboost_amount, 0.0)});
                    pv_now -= iboost_pv_amount;
                    iboost_amount += iboost_pv_amount;
                    if (iboost_pv_amount > 0 && minute == 0) {
                        iboost_running_solar = 1;
                    }
                }
            }
        }

        // Set discharge during charge - prediction.py:770-775
        if (charge_window_active) {
            if (!c->set_discharge_during_charge) {
                discharge_rate_now = battery_rate_min;
            } else if (c->set_charge_window && soc >= charge_limit_n && (std::fabs(static_cast<double>(calc_percent_limit(soc, soc_max) - calc_percent_limit(charge_limit_n, soc_max))) <= 1.0)) {
                discharge_rate_now = battery_rate_min;
            }
        }

        // Current real charge rate - prediction.py:777-786.
        // Python rounds SoC to 1dp here purely to quantise an lru_cache key, but the rounding moves
        // the percent at bucket edges so it is observable. The precomputed bucket boundaries give
        // the identical percent from a binary search, with no round_py in the loop at all.
        const int32_t soc_percent_round1 = soc >= 0.0 ? static_cast<int32_t>(std::upper_bound(soc_pct_threshold.begin() + 1, soc_pct_threshold.end(), soc) - (soc_pct_threshold.begin() + 1)) : percent_via_round(soc, soc_max);
        double charge_rate_now_curve = rate_curve_pct(soc_percent_round1, charge_rate_now, battery_rate_max_charge, c->temp_charge_cap[k], c->charge_curve, battery_rate_min) * battery_rate_max_scaling;
        double charge_rate_now_curve_step = charge_rate_now_curve * step;
        double discharge_rate_now_curve = rate_curve_pct(soc_percent_round1, discharge_rate_now, battery_rate_max_discharge, c->temp_discharge_cap[k], c->discharge_curve, battery_rate_min) * battery_rate_max_scaling_discharge;
        double discharge_rate_now_curve_step = discharge_rate_now_curve * step;

        const double battery_to_min = std::max(soc - reserve_expected, 0.0) * battery_loss_discharge;
        const double battery_to_max = std::max(soc_max - soc, 0.0) * battery_loss;

        // prediction.py:791-793
        double discharge_min = reserve;
        if (export_window_active) {
            discharge_min = std::max({soc_max * export_limit_now / 100.0, reserve, c->best_soc_min});
        }

        double battery_draw = 0;
        double pv_dc = 0;
        double pv_ac = 0;

        if (!c->set_export_freeze_only && export_window_active && export_limit_now < 99.0 && (soc > discharge_min)) {
            // Force export - prediction.py:795-902
            double export_rate_adjust = 1.0;
            if (c->set_export_low_power) {
                export_rate_adjust = 1 - (export_limit_now - static_cast<double>(static_cast<int64_t>(export_limit_now)));
            }
            discharge_rate_now = battery_rate_max_export * export_rate_adjust;
            discharge_rate_now_curve = rate_curve_pct(soc_percent_round1, discharge_rate_now, battery_rate_max_export, c->temp_discharge_cap[k], c->discharge_curve, battery_rate_min) * battery_rate_max_scaling_discharge;
            discharge_rate_now_curve_step = discharge_rate_now_curve * step;

            battery_draw = std::min(discharge_rate_now_curve_step, battery_to_min);
            pv_ac = pv_now * inverter_loss_ac;
            pv_dc = 0;

            // Exceed export limit? - prediction.py:813-855
            double diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss, inverter_loss_recp);
            if (diff < 0 && std::fabs(diff) > export_limit) {
                const double over_limit = std::fabs(diff) - export_limit;
                double reduce_by = over_limit;

                if (reduce_by > battery_draw * inverter_loss) {
                    if (c->inverter_can_charge_during_export) {
                        reduce_by = reduce_by - battery_draw * inverter_loss;
                        if (inverter_hybrid) {
                            // Note: Python passes the un-rounded soc for the DC-rate lookup here
                            const double charge_rate_now_curve_dc = rate_curve(soc, battery_rate_max_charge_dc, battery_rate_max_charge_dc, c->temp_charge_cap[k], c->charge_curve, soc_max, battery_rate_min) * battery_rate_max_scaling;
                            const double charge_rate_now_curve_dc_step = charge_rate_now_curve_dc * step;
                            battery_draw = std::max({-reduce_by * inverter_loss_recp, -battery_to_max, -charge_rate_now_curve_dc_step});
                        } else {
                            battery_draw = std::max({-reduce_by * inverter_loss, -battery_to_max, -charge_rate_now_curve_step});
                        }
                    } else {
                        battery_draw = 0;
                    }
                } else {
                    battery_draw = std::max(battery_draw - reduce_by * inverter_loss_recp, 0.0);
                }

                if (inverter_hybrid && battery_draw < 0) {
                    pv_dc = std::min(std::fabs(battery_draw), pv_now);
                    pv_ac = (pv_now - pv_dc) * inverter_loss_ac;
                }
            }

            // Exceeds inverter limit, scale back discharge - prediction.py:857-889
            double total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid);
            if (inverter_hybrid) {
                const double over_limit = total_inverted - inverter_limit;
                if (total_inverted > inverter_limit) {
                    double reduce_by = over_limit;
                    if (reduce_by > battery_draw) {
                        reduce_by = reduce_by - battery_draw;
                        battery_draw = 0;
                        if (c->inverter_can_charge_during_export) {
                            const double charge_rate_now_curve_dc = rate_curve(soc, battery_rate_max_charge_dc, battery_rate_max_charge_dc, c->temp_charge_cap[k], c->charge_curve, soc_max, battery_rate_min) * battery_rate_max_scaling;
                            const double charge_rate_now_curve_dc_step = charge_rate_now_curve_dc * step;
                            battery_draw = std::max({-reduce_by, -battery_to_max, -charge_rate_now_curve_dc_step});
                        }
                    } else {
                        battery_draw = battery_draw - reduce_by;
                    }

                    if (battery_draw < 0) {
                        pv_dc = std::min(std::fabs(battery_draw), pv_now);
                    }
                    pv_ac = (pv_now - pv_dc) * inverter_loss_ac;
                }
            } else {
                if (total_inverted > inverter_limit) {
                    const double over_limit = total_inverted - inverter_limit;
                    battery_draw = std::max(battery_draw - over_limit * inverter_loss, 0.0);
                }
            }

            // Score against forced export from PV - prediction.py:891-894
            if (!c->calculate_export_on_pv && battery_draw > 0) {
                metric_keep += pv_ac * export_rate * 5;
            }

            // Once force discharge starts the four hour rule is disabled - prediction.py:901-902
            four_hour_rule = false;
        } else if (charge_window_active && soc < charge_limit_n) {
            // Force charge - prediction.py:903-950
            double battery_rate_max_charge_combined;
            if (inverter_hybrid && (battery_rate_max_charge_dc > battery_rate_max_charge)) {
                const double pv_above = std::max((pv_now / step) - battery_rate_max_charge, 0.0);
                battery_rate_max_charge_combined = battery_rate_max_charge + std::min(battery_rate_max_charge_dc - battery_rate_max_charge, pv_above);
            } else {
                battery_rate_max_charge_combined = battery_rate_max_charge;
            }
            // find_charge_rate with set_charge_low_power off (always the case for scenario runs)
            // reduces to the max rate and its curve value - utils.py:1145,1237-1238
            charge_rate_now = battery_rate_max_charge_combined;
            charge_rate_now_curve = rate_curve_pct(soc_percent_round1, battery_rate_max_charge_combined, battery_rate_max_charge_combined, c->temp_charge_cap[k], c->charge_curve, battery_rate_min) * battery_rate_max_scaling;
            charge_rate_now_curve_step = charge_rate_now_curve * step;

            battery_draw = -std::max({std::min(charge_rate_now_curve_step, std::max(charge_limit_n - soc, pv_now)), 0.0, -battery_to_max});

            if (inverter_hybrid) {
                pv_dc = std::min(std::fabs(battery_draw), pv_now);
            } else {
                pv_dc = 0;
            }
            pv_ac = (pv_now - pv_dc) * inverter_loss_ac;

            // Charge hits limit mid-period, model the potential import - prediction.py:941-950
            if ((charge_limit_n - soc) < charge_rate_now_curve_step) {
                const double pv_compare = pv_dc + pv_ac;
                if (pv_dc >= (charge_limit_n - soc) && (pv_compare < charge_rate_now_curve_step)) {
                    const double charge_time_remains = (charge_limit_n - soc) / charge_rate_now_curve;
                    const double pv_in_period = pv_compare / step * charge_time_remains;
                    const double potential_import = std::min((charge_rate_now_curve * charge_time_remains) - pv_in_period, (charge_limit_n - soc));
                    metric_keep += std::max(potential_import * import_rate, 0.0);
                }
            }
        } else if (c->set_export_freeze && export_window_active && export_limit_now < 100.0 && (export_limit_now == 99.0 || c->set_export_freeze_only)) {
            // Freeze - not an active discharge, but genuine PV surplus beyond what
            // load+export_limit can absorb still charges the battery on some inverters rather
            // than being clipped (#4207) - mirrors the recapture logic in the force export
            // branch above, without any active discharge. prediction.py's matching elif.
            battery_draw = 0;
            pv_ac = pv_now * inverter_loss_ac;
            pv_dc = 0;

            const double diff_freeze = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss, inverter_loss_recp);
            if (diff_freeze < 0 && std::fabs(diff_freeze) > export_limit && c->inverter_can_charge_during_export) {
                const double over_limit = std::fabs(diff_freeze) - export_limit;
                if (inverter_hybrid) {
                    const double charge_rate_now_curve_dc = rate_curve(soc, battery_rate_max_charge_dc, battery_rate_max_charge_dc, c->temp_charge_cap[k], c->charge_curve, soc_max, battery_rate_min) * battery_rate_max_scaling;
                    const double charge_rate_now_curve_dc_step = charge_rate_now_curve_dc * step;
                    battery_draw = std::max({-over_limit * inverter_loss_recp, -battery_to_max, -charge_rate_now_curve_dc_step});
                } else {
                    battery_draw = std::max({-over_limit * inverter_loss, -battery_to_max, -charge_rate_now_curve_step});
                }

                if (battery_draw < 0) {
                    pv_dc = std::min(std::fabs(battery_draw), pv_now);
                    pv_ac = (pv_now - pv_dc) * inverter_loss_ac;
                }
            }

            // Some inverters (observed on AlphaESS) continue a small residual battery
            // discharge during Freeze Export. Feed the battery-side rate into the normal AC
            // balance so load consumes it first and any surplus may export, while respecting
            // the reserve and the physical grid export limit.
            if (inverter_freeze_export_discharge_rate > 0 && battery_draw >= 0) {
                double freeze_draw = std::min(inverter_freeze_export_discharge_rate * step * battery_loss_discharge, battery_to_min);
                const double freeze_diff = get_diff(freeze_draw, pv_dc, pv_ac, load_yesterday, inverter_loss, inverter_loss_recp);
                if (freeze_diff < 0 && std::abs(freeze_diff) > export_limit) {
                    freeze_draw = std::max(freeze_draw - (std::abs(freeze_diff) - export_limit) * inverter_loss_recp, 0.0);
                }
                battery_draw = freeze_draw;
            }
        } else {
            // ECO Mode - prediction.py:951-997
            pv_ac = pv_now * inverter_loss_ac;
            pv_dc = 0;

            const double potential_to_charge = pv_ac;
            double required_for_load = load_yesterday;
            if (required_for_load > potential_to_charge) {
                required_for_load += (required_for_load - potential_to_charge) * inverter_loss_recp - (required_for_load - potential_to_charge);
            }
            const double diff = required_for_load - potential_to_charge;

            if (diff > 0) {
                battery_draw = std::min({diff, discharge_rate_now_curve_step, inverter_limit, battery_to_min});
            } else {
                if (inverter_hybrid) {
                    const double charge_rate_now_dc = battery_rate_max_charge_dc;
                    // Freeze windows are handled by their own else-if branch above and never
                    // reach here - no need to zero the charge rate for them in this branch.
                    // Note: Python passes the un-rounded soc for the DC-rate lookup here
                    const double charge_rate_now_curve_dc = rate_curve(soc, charge_rate_now_dc, battery_rate_max_charge_dc, c->temp_charge_cap[k], c->charge_curve, soc_max, battery_rate_min) * battery_rate_max_scaling;
                    const double charge_rate_now_curve_dc_step = charge_rate_now_curve_dc * step;

                    const double virtual_inverter_limit = inverter_limit + pv_now;
                    battery_draw = std::max({diff, -charge_rate_now_curve_dc_step, -virtual_inverter_limit, -battery_to_max});
                } else {
                    battery_draw = std::max({diff, -charge_rate_now_curve_step, -inverter_limit, -battery_to_max});
                }

                if (inverter_hybrid) {
                    pv_dc = std::min(std::fabs(battery_draw), pv_now);
                } else {
                    pv_dc = 0;
                }
                pv_ac = (pv_now - pv_dc) * inverter_loss_ac;
            }
        }

        // Clamp at inverter limit - prediction.py:999-1049
        if (inverter_hybrid) {
            const double battery_inverted = get_total_inverted(battery_draw, pv_dc, 0, inverter_loss, inverter_hybrid);
            if (battery_inverted > inverter_limit) {
                const double over_limit = battery_inverted - inverter_limit;

                if (battery_draw + pv_dc > 0) {
                    battery_draw = std::max(battery_draw - over_limit, 0.0);
                } else {
                    battery_draw = std::min(battery_draw + over_limit * inverter_loss, 0.0);
                }

                if (battery_draw < 0) {
                    pv_dc = std::min(std::fabs(battery_draw), pv_now);
                    pv_ac = (pv_now - pv_dc) * inverter_loss_ac;
                }
            }

            // Clip battery discharge back - prediction.py:1015-1031
            double total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid);
            if (total_inverted > inverter_limit && (battery_draw + pv_dc) > 0) {
                double over_limit = total_inverted - inverter_limit;
                if (battery_draw + pv_dc > 0) {
                    battery_draw = std::max(battery_draw - over_limit, 0.0);
                }

                if (battery_draw == 0) {
                    total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid);
                    over_limit = 0;
                    if (total_inverted > inverter_limit) {
                        over_limit = total_inverted - inverter_limit;
                    }
                    battery_draw = std::max({-over_limit * inverter_loss, -charge_rate_now_curve_step, -battery_to_max, -pv_ac});
                }

                if (battery_draw < 0) {
                    pv_dc = std::min(std::fabs(battery_draw), pv_now);
                    pv_ac = (pv_now - pv_dc) * inverter_loss_ac;
                }
            }

            // Clip solar - prediction.py:1033-1041
            total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid);
            if (total_inverted > inverter_limit) {
                const double over_limit = total_inverted - inverter_limit;
                pv_ac = std::max(pv_ac - over_limit * inverter_loss, 0.0);
            }
        } else {
            const double total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid);
            if (total_inverted > inverter_limit) {
                const double over_limit = total_inverted - inverter_limit;
                if (battery_draw > 0) {
                    battery_draw = std::max(battery_draw - over_limit, 0.0);
                } else {
                    battery_draw = std::min(battery_draw + over_limit * inverter_loss, 0.0);
                }
            }
        }

        // Export limit, clip PV output - prediction.py:1051-1058
        double diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss, inverter_loss_recp);
        if (diff < 0 && std::fabs(diff) > export_limit) {
            const double over_limit = std::fabs(diff) - export_limit;
            pv_ac = std::max(pv_ac - over_limit, 0.0);
        }

        // Adjust battery soc - prediction.py:1060-1064
        if (battery_draw > 0) {
            soc = std::max(soc - battery_draw / battery_loss_discharge, reserve_expected);
        } else {
            soc = std::min(soc - battery_draw * battery_loss, soc_max);
        }

        // iBoost final count - prediction.py:1066-1092
        if (c->iboost_enable) {
            // iBoost solar excess diversion model - prediction.py:1068-1078 (uses the pre-clip diff)
            if (c->iboost_solar && c->iboost_solar_excess) {
                double excess = 0;
                if (diff < 0) {
                    excess = -diff;
                }
                if (iboost_rate_okay && iboost_today_kwh < c->iboost_max_energy && (excess > (c->iboost_min_power * step) && ((soc * 100.0 / soc_max) >= c->iboost_min_soc)) && (c->iboost_on_export || (export_window_n < 0))) {
                    const double iboost_pv_amount = std::min({excess, std::max(c->iboost_max_power * step - iboost_amount, 0.0), std::max(c->iboost_max_energy - iboost_today_kwh - iboost_amount, 0.0)});
                    load_yesterday += iboost_pv_amount;
                    iboost_amount += iboost_pv_amount;
                    if (iboost_pv_amount > 0 && minute == 0) {
                        iboost_running_solar = 1;
                    }
                }
            }

            // Cumulative iBoost energy - prediction.py:1080-1081
            iboost_today_kwh += iboost_amount;

            // Model iboost reset - prediction.py:1083-1085
            if ((minute_absolute % (24 * 60)) == ((24 * 60) - step)) {
                iboost_today_kwh = 0;
            }

            // Save iBoost next prediction - prediction.py:1087-1092
            if (minute == 0) {
                const double scaled_boost = (iboost_amount / step) * PK_RUN_EVERY;
                iboost_next = round_py(c->iboost_today + scaled_boost, 6);
                if (iboost_next > c->iboost_today) {
                    iboost_running = 1;
                }
            }
        }

        // Count battery cycles - prediction.py:1094-1095
        battery_cycle = battery_cycle + std::fabs(battery_draw);

        // Work out left over energy after battery adjustment - prediction.py:1097-1098
        diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss, inverter_loss_recp);

        // Metric keep - prediction.py:1100-1102
        if (best_soc_keep > 0 && soc <= best_soc_keep) {
            metric_keep += (best_soc_keep - soc) * import_rate * keep_minute_scaling * step / 60.0;
        }

        // Import/export accounting - prediction.py:1104-1143
        if (diff > 0) {
            // Import
            if (c->carbon_enable) {
                carbon_g += diff * c->carbon[k];
            }
            if (charge_window_active) {
                import_kwh_battery += diff;
            } else {
                import_kwh_house += diff;
            }
            // Premium for car charging capped at the actual grid import - prediction.py:1119-1122
            car_amount_premium = std::min(diff, car_amount_premium);
            metric += import_rate * diff + car_rate_premium * car_amount_premium;
        } else {
            // Export
            const double energy = -diff;
            export_kwh += energy;
            if (c->carbon_enable) {
                carbon_g -= energy * c->carbon[k];
            }
            if (!c->car_energy_reported_load) {
                // Export can end up in a car outside the CT clamp, value that amount at 0 - prediction.py:1131-1135
                metric -= export_rate * std::max(0.0, energy - car_load_energy_bypass);
            } else {
                metric -= export_rate * energy;
            }
        }

        // Record final soc & metric - prediction.py:1145-1186
        if (record) {
            final_soc = soc;

            if (car_enable) {
                for (int32_t car_n = 0; car_n < c->num_cars; car_n++) {
                    if (minute == 0) {
                        // Next car SoC - prediction.py:1155-1157
                        car_soc_next[car_n] = round_py(car_soc[car_n], 3);
                        car_soc_next_valid = 1;
                    }
                }
            }

            final_metric = metric;
            final_battery_cycle = battery_cycle;
            final_metric_keep = metric_keep;
            final_iboost_kwh += iboost_amount;
            final_carbon_g = carbon_g;
            final_import_kwh_battery = import_kwh_battery;
            final_import_kwh_house = import_kwh_house;
            final_export_kwh = export_kwh;

            // Record soc min - prediction.py:1183-1186
            if (soc < soc_min) {
                soc_min_minute = minute_absolute;
            }
            soc_min = std::min(soc_min, soc);
        }
    }

    out->final_metric = final_metric;
    out->import_kwh_battery = final_import_kwh_battery;
    out->import_kwh_house = final_import_kwh_house;
    out->export_kwh = final_export_kwh;
    out->soc_min = soc_min;
    out->final_soc = final_soc;
    out->battery_cycle = final_battery_cycle;
    out->metric_keep = final_metric_keep;
    out->final_iboost = final_iboost_kwh;
    out->final_carbon_g = final_carbon_g;
    for (int32_t car_n = 0; car_n < PK_MAX_CARS; car_n++) {
        out->car_soc_next[car_n] = car_soc_next[car_n];
    }
    out->iboost_next = iboost_next;
    out->soc_min_minute = soc_min_minute;
    out->car_soc_next_valid = car_soc_next_valid;
    out->iboost_running = iboost_running;
    out->iboost_running_solar = iboost_running_solar;
    out->iboost_running_full = iboost_running_full;
    if (soc_range_min_out) {
        if (want_soc_range) {
            // Mirrors the two clamping lines that follow the scan in
            // thread_run_prediction_charge_min_max, so an empty range collapses to a single value
            // rather than leaving min above max
            const double clamped_max = soc_range_max > soc_range_min ? soc_range_max : soc_range_min;
            const double clamped_min = soc_range_min < clamped_max ? soc_range_min : clamped_max;
            *soc_range_min_out = clamped_min;
            *soc_range_max_out = clamped_max;
        } else {
            // No range asked for. Python's all_n path skips the scan AND the clamping, returning
            // (soc_max, 0) untouched, so clamping here would hand back (soc_max, soc_max) instead.
            *soc_range_min_out = c->soc_max;
            *soc_range_max_out = 0.0;
        }
    }
    return 0;
}

// Run one scenario. Kept with its original signature and struct so a binary built before the batch
// entry point existed still satisfies the loader and every single-scenario caller.
int32_t pk_run(int64_t handle, const PkScenario *s, PkResult *out)
{
    const ContextStore *c = lookup_context(handle);
    if (!c) {
        return 1;
    }
    return pk_run_one(c, s, out, -1, -1, nullptr, nullptr, thread_scratch());  // c is the store
}

// Run n_jobs scenarios against one context in a single call.
//
// The point is to pay the Python/C boundary and the context lookup once for a whole fan-out of
// scenarios instead of once each: the per-call Python around pk_run measured ~31us against ~55us of
// simulation, and neither Python threads nor a process pool can recover it (the GIL serialises the
// former, pickling the latter).
//
// Per-job failures are reported in that job's status field rather than aborting the batch, so one
// malformed scenario cannot discard the rest of the fan-out.
// Simulate one batched scenario into its result slot. Split out so the serial and threaded paths
// below share exactly one copy of the marshalling.
static void run_batch_job(const ContextStore *c, const PkBatchJob &job, PkBatchResult &out, PkScratch &scratch)
{
    PkScenario scenario;
    scenario.charge_limit = job.charge_limit;
    scenario.charge_start = job.charge_start;
    scenario.charge_end = job.charge_end;
    scenario.export_limits = job.export_limits;
    scenario.export_start = job.export_start;
    scenario.export_end = job.export_end;
    scenario.soc_out = job.soc_out;
    scenario.n_charge = job.n_charge;
    scenario.n_export = job.n_export;
    scenario.pv_scenario = job.pv_scenario;
    scenario.end_record = job.end_record;
    scenario.step = job.step;
    out.soc_range_min = 0.0;
    out.soc_range_max = 0.0;
    out.pad = 0;
    out.status = pk_run_one(c, &scenario, &out.result, job.soc_range_start_step, job.soc_range_end_step, &out.soc_range_min, &out.soc_range_max, scratch);
}


// A pool of worker threads that outlives every batch and every context.
//
// pk_run_batch used to build and join a fresh std::vector<std::thread> per call. Measured over one
// benchmark plan that was 201,545 thread creations costing 1.5s - against 3.1s of simulation, so the
// creation cost was cancelling most of what the parallelism won and threading measured 1.6% overall.
// Parking workers on a condition variable replaces each creation with a wake.
//
// Workers park until `generation` changes, then take lane `index` of a stride partition. The calling
// thread takes lane 0 itself, so a six-job batch wakes five workers rather than six and the caller
// does useful work instead of blocking. Nobody holds the mutex while simulating; run_batch_job only
// reads the shared const context and writes its own result slot, which is what made the previous
// per-call threading safe and is unchanged here.
class PkThreadPool {
  public:
    // Start workers until at least `want` exist. Grows, never shrinks. Returns how many there are.
    int32_t reserve_workers(int32_t want)
    {
        std::lock_guard<std::mutex> lock(mutex);
        while (static_cast<int32_t>(workers.size()) < want) {
            const int32_t lane = static_cast<int32_t>(workers.size()) + 1;  // lane 0 is the caller
            std::unique_ptr<Worker> worker(new Worker());
            try {
                worker->thread = std::thread(&PkThreadPool::worker_loop, this, lane, worker.get());
            } catch (const std::system_error &) {
                // Out of thread resources - keep what we have and let the caller use fewer lanes.
                break;
            }
            workers.push_back(std::move(worker));
            g_worker_threads_started.fetch_add(1, std::memory_order_relaxed);
        }
        return static_cast<int32_t>(workers.size());
    }

    // Run jobs[0..n_jobs) across `use` lanes, the calling thread taking lane 0. `use` must be at
    // least 1 and no more than worker_count() + 1.
    void run(const ContextStore *c, const PkBatchJob *jobs, int32_t n_jobs, PkBatchResult *results, int32_t use)
    {
        {
            std::lock_guard<std::mutex> lock(mutex);
            cur_ctx = c;
            cur_jobs = jobs;
            cur_results = results;
            cur_n_jobs = n_jobs;
            cur_use = use;
            outstanding = use - 1;
            // Each worker has its own flag and its own condition variable, so exactly the lanes this
            // batch needs are woken. Broadcasting instead would wake every worker in the pool - with
            // a median batch of six jobs against sixteen workers that was ~250,000 wakeups per plan
            // spent entirely on threads that immediately went back to sleep.
            for (int32_t lane = 1; lane < use; lane++) {
                workers[lane - 1]->has_work = true;
            }
        }
        for (int32_t lane = 1; lane < use; lane++) {
            workers[lane - 1]->ready.notify_one();
        }

        PkScratch &scratch = thread_scratch();
        for (int32_t i = 0; i < n_jobs; i += use) {
            run_batch_job(c, jobs[i], results[i], scratch);
        }

        std::unique_lock<std::mutex> lock(mutex);
        work_done.wait(lock, [this] { return outstanding == 0; });
    }

  private:
    // One parked worker. The flag is written under the pool mutex before its condition variable is
    // signalled, so a wake cannot be lost even if the worker has not re-parked yet.
    struct Worker {
        std::condition_variable ready;
        bool has_work = false;
        std::thread thread;
    };

    void worker_loop(int32_t lane, Worker *self)
    {
        std::unique_lock<std::mutex> lock(mutex);
        for (;;) {
            self->ready.wait(lock, [self] { return self->has_work; });
            self->has_work = false;
            const ContextStore *c = cur_ctx;
            const PkBatchJob *jobs = cur_jobs;
            PkBatchResult *results = cur_results;
            const int32_t n_jobs = cur_n_jobs;
            const int32_t use = cur_use;
            lock.unlock();

            PkScratch &scratch = thread_scratch();
            for (int32_t i = lane; i < n_jobs; i += use) {
                run_batch_job(c, jobs[i], results[i], scratch);
            }

            lock.lock();
            if (--outstanding == 0) {
                work_done.notify_one();
            }
        }
    }

    std::mutex mutex;
    std::condition_variable work_done;  // the caller waits here
    const ContextStore *cur_ctx = nullptr;
    const PkBatchJob *cur_jobs = nullptr;
    PkBatchResult *cur_results = nullptr;
    int32_t cur_n_jobs = 0;
    int32_t cur_use = 1;
    int32_t outstanding = 0;
    std::vector<std::unique_ptr<Worker>> workers;
};

// The pool is created on first threaded use and deliberately never destroyed: a static destructor
// running while CPython tears the process down risks a deadlock for no benefit, and the OS reclaims
// the threads at exit.
std::atomic<PkThreadPool *> g_pool{nullptr};
std::mutex g_pool_create_mutex;
// Held for a whole batch. Python can enter pk_run_batch from two threads at once because ctypes
// releases the GIL, and one shared pool cannot serve two batches concurrently - its published job
// state would race. Nothing in predbat does this today; serialising is the safe answer if it ever does.
std::mutex g_dispatch_mutex;

#if defined(__unix__) || defined(__APPLE__)
// Threads do not survive fork(), so a child would inherit a pool whose workers do not exist and hang
// waiting for them. The prepare/parent pair keep the dispatch mutex consistent across the fork, and
// the child drops the pool so its next batch builds a fresh one. Nothing here allocates or takes a
// new lock, which is what the child handler is allowed to do; the dead pool leaks in the child.
void pk_atfork_prepare()
{
    g_dispatch_mutex.lock();
}

void pk_atfork_parent()
{
    g_dispatch_mutex.unlock();
}

void pk_atfork_child()
{
    g_dispatch_mutex.unlock();
    g_pool.store(nullptr, std::memory_order_relaxed);
}
#endif

// Fetch the pool, creating it on first use.
static PkThreadPool *pool_singleton()
{
    PkThreadPool *pool = g_pool.load(std::memory_order_acquire);
    if (pool) {
        return pool;
    }
    std::lock_guard<std::mutex> lock(g_pool_create_mutex);
    pool = g_pool.load(std::memory_order_relaxed);
    if (!pool) {
#if defined(__unix__) || defined(__APPLE__)
        static std::once_flag atfork_once;
        std::call_once(atfork_once, [] { pthread_atfork(pk_atfork_prepare, pk_atfork_parent, pk_atfork_child); });
#endif
        pool = new PkThreadPool();
        g_pool.store(pool, std::memory_order_release);
    }
    return pool;
}

// Run n_jobs scenarios against one context in a single call.
//
// The context lookup and the Python/C boundary are paid once for a whole fan-out rather than once
// per scenario, but the real point is n_threads: 19209 separate ctypes calls cannot be parallelised
// usefully from Python (the GIL serialises threads, and a process pool pays pickling), whereas one
// batch can split the work across cores with no GIL involved at all.
//
// n_threads <= 1 runs serially. Per-job failures land in that job's status field rather than
// aborting the batch, so one malformed scenario cannot discard the rest of the fan-out.
int32_t pk_run_batch(int64_t handle, const PkBatchJob *jobs, int32_t n_jobs, PkBatchResult *results, int32_t n_threads)
{
    const ContextStore *c = lookup_context(handle);
    if (!c) {
        return 1;
    }
    if (!jobs || !results || n_jobs < 0) {
        return 2;
    }
    // Scenarios are independent - each reads the shared const context and writes only its own result
    // slot - so they are split across threads by stride.
    if (n_threads > 1 && n_jobs > 1) {
        std::lock_guard<std::mutex> dispatch(g_dispatch_mutex);
        int32_t use = n_threads < n_jobs ? n_threads : n_jobs;
        PkThreadPool *pool = pool_singleton();
        // Clamp to the lanes that actually exist: reserve_workers stops early if the system refuses a
        // thread, and a batch must never wait on a worker that was never started.
        const int32_t lanes = pool->reserve_workers(use - 1) + 1;
        if (use > lanes) {
            use = lanes;
        }
        if (use > 1) {
            pool->run(c, jobs, n_jobs, results, use);
            return 0;
        }
        // No workers available - fall through and run the batch inline.
    }
    for (int32_t i = 0; i < n_jobs; i++) {
        run_batch_job(c, jobs[i], results[i], thread_scratch());
    }
    return 0;
}


// Test hook: how many PkScratch instances have been constructed since the library was loaded.
//
// The scratch buffers are the only heap allocation a simulation makes, and the kernel's whole
// batching win rests on not paying them per call - so the suite asserts this counter stops growing
// with the number of batches rather than trusting the code to be reusing them.
int64_t pk_scratch_construct_count(void)
{
    return g_scratch_constructions.load(std::memory_order_relaxed);
}


// Test hook: how many worker threads have been started since the library was loaded.
//
// Batches used to build and join a fresh set of threads on every call. The suite asserts this count
// stops growing once the pool is warm, because a thread creation costs several microseconds against
// a median batch of six jobs - it was cancelling the parallelism it was there to provide.
int64_t pk_worker_thread_count(void)
{
    return g_worker_threads_started.load(std::memory_order_relaxed);
}


// Test hook: sweep SoC densely and confirm the precomputed bucket boundaries give exactly the same
// percent as the round_py path they replace. Returns the number of disagreements (0 = equivalent).
int32_t pk_verify_soc_percent_table(double soc_max, int32_t samples)
{
    std::vector<double> table;
    build_soc_percent_thresholds(table, soc_max);
    int32_t bad = 0;
    const double hi = soc_max * 1.2 + 0.5;
    for (int32_t i = 0; i <= samples; i++) {
        const double soc = hi * static_cast<double>(i) / static_cast<double>(samples);
        const int32_t want = percent_via_round(soc, soc_max);
        const int32_t got = static_cast<int32_t>(std::upper_bound(table.begin() + 1, table.end(), soc) - (table.begin() + 1));
        if (want != got) {
            bad++;
        }
    }
    // Also probe either side of every boundary, where a disagreement would actually hide
    for (int32_t p = 1; p <= 100; p++) {
        const double edge = table[p];
        if (!std::isfinite(edge)) {
            continue;
        }
        for (int32_t d = -2; d <= 2; d++) {
            double probe = edge;
            for (int32_t n = 0; n < std::abs(d); n++) {
                probe = d < 0 ? std::nextafter(probe, -1.0) : std::nextafter(probe, 1e18);
            }
            if (probe < 0) {
                continue;
            }
            const int32_t want = percent_via_round(probe, soc_max);
            const int32_t got = static_cast<int32_t>(std::upper_bound(table.begin() + 1, table.end(), probe) - (table.begin() + 1));
            if (want != got) {
                bad++;
            }
        }
    }
    return bad;
}

} // extern "C"
