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
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <map>
#include <memory>
#include <mutex>
#include <vector>

#define PK_ABI_VERSION 2
#define PK_PARITY_REVISION 2
#define PK_MAX_CARS 4
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
    const double *export_limits;  // percent per export window (99=freeze, 100=off)
    const int32_t *export_start;
    const int32_t *export_end;
    double *soc_out;              // caller-allocated, n_steps entries, filled with round(soc, 3)

    int32_t n_charge;
    int32_t n_export;
    int32_t pv10;
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

// Deep-copied context storage so Python-side buffers can be freed after create
struct ContextStore {
    std::vector<double> rate_import, rate_export, alert_keep;
    std::vector<double> pv, load, pv10, load10;
    std::vector<double> temp_charge_cap, temp_discharge_cap;
    std::vector<int32_t> io_flag;
    std::vector<double> charge_curve, discharge_curve;
    std::vector<double> carbon, gas_rate, iboost_plan_load;
    std::vector<double> car_load_flat, car_rate_flat;
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
    size_t n = static_cast<size_t>(in->n_steps);
    store->rate_import.assign(in->rate_import, in->rate_import + n);
    store->rate_export.assign(in->rate_export, in->rate_export + n);
    store->alert_keep.assign(in->alert_keep, in->alert_keep + n);
    store->pv.assign(in->pv, in->pv + n);
    store->load.assign(in->load, in->load + n);
    store->pv10.assign(in->pv10, in->pv10 + n);
    store->load10.assign(in->load10, in->load10 + n);
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
int32_t pk_run(int64_t handle, const PkScenario *s, PkResult *out)
{
    const PkContext *c;
    {
        std::lock_guard<std::mutex> lock(g_context_mutex);
        auto it = g_contexts.find(handle);
        if (it == g_contexts.end()) {
            return 1;
        }
        c = &it->second->ctx;
    }
    if (!s || !out || s->step != 5) {
        return 2;
    }

    const bool pv10 = s->pv10 != 0;
    const int32_t step = s->step;
    const int32_t n_steps = c->n_steps;
    const bool inverter_hybrid = c->inverter_hybrid != 0;

    // Window membership - prediction.py:494-495 / find_charge_window_optimised
    std::vector<int32_t> charge_window_optimised, export_window_optimised;
    build_window_membership(charge_window_optimised, s->n_charge, s->charge_start, s->charge_end, s->charge_limit, false, c->minutes_now, n_steps);
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
    // PV10 de-rating of the charge rate - prediction.py:547-551
    const double battery_rate_max_scaling = pv10 ? c->battery_rate_max_scaling10 : c->battery_rate_max_scaling;
    const double battery_rate_max_scaling_discharge = c->battery_rate_max_scaling_discharge;
    const double *pv_step = pv10 ? c->pv10 : c->pv;
    const double *load_step = pv10 ? c->load10 : c->load;

    // Simulate each forward step - prediction.py:570-1200
    for (int32_t k = 0; k < n_steps; k++) {
        const int32_t minute = k * step;
        const int32_t minute_absolute = minute + c->minutes_now;
        double reserve_expected = reserve;

        // Rates - prediction.py:577-580
        double import_rate = c->rate_import[k];
        if (c->io_flag[k] && pv10 && minute > 30) {
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
            charge_limit_n = s->charge_limit[charge_window_n];
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

        // Save SoC prediction data - prediction.py:627-628 (kernel always fills the buffer)
        s->soc_out[k] = round_py(soc, 3);

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
                        // Model not allowing the car to charge from the battery
                        if ((car_load_scale > 0) && (!c->car_charging_from_battery) && c->set_charge_window) {
                            discharge_rate_now = battery_rate_min; // 0
                        }
                    } else {
                        car_load_energy_bypass += car_load_scale / c->car_charging_loss;
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

        // Discharge freeze - prediction.py:764-768
        if (c->set_export_freeze) {
            if (export_window_active && export_limit_now < 100.0 && (c->set_export_freeze && (export_limit_now == 99.0 || c->set_export_freeze_only))) {
                charge_rate_now = battery_rate_min; // 0
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

        // Current real charge rate - prediction.py:777-786
        const double soc_round1 = round_py(soc, 1);
        double charge_rate_now_curve = rate_curve(soc_round1, charge_rate_now, battery_rate_max_charge, c->temp_charge_cap[k], c->charge_curve, soc_max, battery_rate_min) * battery_rate_max_scaling;
        double charge_rate_now_curve_step = charge_rate_now_curve * step;
        double discharge_rate_now_curve = rate_curve(soc_round1, discharge_rate_now, battery_rate_max_discharge, c->temp_discharge_cap[k], c->discharge_curve, soc_max, battery_rate_min) * battery_rate_max_scaling_discharge;
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
            discharge_rate_now_curve = rate_curve(soc_round1, discharge_rate_now, battery_rate_max_export, c->temp_discharge_cap[k], c->discharge_curve, soc_max, battery_rate_min) * battery_rate_max_scaling_discharge;
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
            charge_rate_now_curve = rate_curve(soc_round1, battery_rate_max_charge_combined, battery_rate_max_charge_combined, c->temp_charge_cap[k], c->charge_curve, soc_max, battery_rate_min) * battery_rate_max_scaling;
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
                    double charge_rate_now_dc = battery_rate_max_charge_dc;
                    // Freeze mode - prediction.py:973-975
                    if (c->set_export_freeze && export_window_active && export_limit_now < 100.0 && (export_limit_now == 99.0 || c->set_export_freeze_only)) {
                        charge_rate_now_dc = battery_rate_min; // 0
                    }
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

            // Record soc min - prediction.py:1183-1186
            if (soc < soc_min) {
                soc_min_minute = minute_absolute;
            }
            soc_min = std::min(soc_min, soc);
        }
    }

    out->final_metric = final_metric;
    out->import_kwh_battery = import_kwh_battery;
    out->import_kwh_house = import_kwh_house;
    out->export_kwh = export_kwh;
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
    return 0;
}

} // extern "C"
