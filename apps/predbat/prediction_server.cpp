#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <float.h>
#include <math.h>

#define min(a, b) ((a) < (b) ? (a) : (b))
#define max(a, b) ((a) > (b) ? (a) : (b))

#define FIXED_POINT_SCALE 1000000

// The struct below mirrors the attributes of the Python class above
typedef struct {
    long minutes_now;
    long forecast_minutes;
    long midnight_utc;
    double soc_kw;
    double soc_max;
    double export_today_now;
    double import_today_now;
    double load_minutes_now;
    double pv_today_now;
    double iboost_today;
    double charge_rate_now;
    double discharge_rate_now;
    double cost_today_sofar;
    double carbon_today_sofar;
    bool debug_enable;
    long num_cars;
    double *car_charging_soc;
    double *car_charging_soc_next;
    double car_charging_loss;
    double reserve;
    double metric_standing_charge;
    bool set_charge_freeze;
    bool set_reserve_enable;
    bool set_export_freeze;
    bool set_export_freeze_only;
    bool set_discharge_during_charge;
    bool set_read_only;
    bool set_charge_low_power;
    bool set_export_low_power;
    bool set_charge_window;
    bool set_export_window;
    double charge_low_power_margin;
    double **car_charging_slots_fold;
    double *car_charging_limit;
    bool car_charging_from_battery;
    bool iboost_enable;
    bool iboost_on_export;
    bool iboost_prevent_discharge;
    bool carbon_enable;
    double iboost_next;
    double iboost_max_energy;
    double iboost_max_power;
    double iboost_min_power;
    double iboost_min_soc;
    bool iboost_solar; 
    bool iboost_solar_excess; 
    bool iboost_charging; 
    double *iboost_plan;
    bool iboost_gas;
    bool iboost_gas_export;
    double iboost_gas_scale; // scale factor for gas
    double iboost_rate_threshold; // threshold for rate
    double iboost_rate_threshold_export; // threshold for export rate
    double *rate_gas; // gas rate
    double inverter_loss; // inverter loss percentage
    bool inverter_hybrid; // hybrid inverter flag
    double inverter_limit; // inverter limit in kW
    double export_limit; // export limit in kW
    double battery_rate_min; // minimum battery rate
    double battery_rate_max_charge; // maximum charge rate
    double battery_rate_max_discharge; // maximum discharge rate
    double battery_rate_max_charge_scaled; // scaled maximum charge rate
    double battery_rate_max_discharge_scaled; // scaled maximum discharge rate
    double *battery_charge_power_curve; // charge power curve for battery
    double *battery_discharge_power_curve; // discharge power curve for battery
    double battery_temperature; // current battery temperature
    double *battery_temperature_charge_curve; // charge temperature curve for battery
    double *battery_temperature_discharge_curve; // discharge temperature curve for battery
    double *battery_temperature_prediction; // predicted battery temperature
    double battery_rate_max_scaling; // maximum scaling for charge rate
    double battery_rate_max_scaling_discharge; // maximum scaling for discharge rate
    double battery_loss; // battery loss percentage
    double battery_loss_discharge; // battery loss during discharge
    double best_soc_keep; // best state of charge to keep
    double best_soc_keep_weight; // weight for best state of charge
    double best_soc_min; // minimum state of charge
    double *car_charging_battery_size; // battery size for each car
    double *rate_import; // import rate
    double *rate_export; // export rate
    double *pv_forecast_minute_step; // step for PV forecast in minutes
    double *pv_forecast_minute10_step; // step for 10-minute PV forecast
    double *load_minutes_step; // step for load minutes in minutes
    double *load_minutes_step10; // step for 10-minute load minutes
    double *carbon_intensity; // carbon intensity value
    double *alert_active_keep; // flag to keep alert active
    bool iboost_running; // flag to indicate if iboost is running
    bool iboost_running_solar; // flag for solar iboost running
    bool iboost_running_full; // flag for full iboost running
    bool inverter_can_charge_during_export; // flag to indicate if inverter can charge during export
} params;

struct prediction_result {
    double final_metric;
    double import_kwh_battery;
    double import_kwh_house;
    double export_kwh;
    double soc_min;
    double final_soc;
    double soc_min_minute;
    double final_battery_cycle;
    double final_metric_keep;
    double final_iboost_kwh;
    double final_carbon_g;
    double *predict_soc; // Array of predicted SOC values
    double *car_charging_soc_next; // Next SOC for each car
    double iboost_next; // Next iboost value
    bool iboost_running; // Is iboost running?
    bool iboost_running_solar; // Is solar iboost running?
    bool iboost_running_full; // Is full iboost running?
};

long read_int(FILE *file) {
    long value;
    // Check file is not at end
    if (fread(&value, sizeof(value), 1, file) != 1) {
        perror("Failed to read integer value");
        return 0;
    }
    return value;
}
bool read_bool(FILE *file) {
    long value = read_int(file);
    return value != 0;
}
double read_double(FILE *file) {
    long value = read_int(file);
    double flt;
    // Convert the long value to double
    flt = (double)value / FIXED_POINT_SCALE;
    return flt;
}
int write_fixed_array(FILE *file, long size, double *array) {
    for (long i = 0; i < size; i++) {
        long scaled_value = (long)(array[i] * FIXED_POINT_SCALE); // Scale the double value
        if (fwrite(&scaled_value, sizeof(scaled_value), 1, file) != 1) {
            perror("Failed to write fixed array value");
            return -1;
        }
    }
    return 0;
}

int write_int(FILE *file, long value) {
    if (fwrite(&value, sizeof(value), 1, file) != 1) {
        perror("Failed to write integer value");
        return -1;
    }
    return 0;
}
int write_double(FILE *file, double value) {
    long scaled_value = (long)(value * FIXED_POINT_SCALE); // Scale the double value
    return write_int(file, scaled_value);
}
int write_bool(FILE *file, bool value) {
    return write_int(file, value ? 1 : 0);
}
double dp6(double value) {
    return round(value * 1000000.0) / 1000000.0; // Round to 6 decimal places
}
double dp4(double value) {
    return round(value * 10000.0) / 10000.0; // Round to 4 decimal places
}
double dp3(double value) {
    return round(value * 1000.0) / 1000.0; // Round to 3 decimal places
}
double dp2(double value) {
    return round(value * 100.0) / 100.0; // Round to 2 decimal places
}

double *read_fixed_array(FILE *file, long size) {
    double *array = (double*)malloc(size * sizeof(double));
    if (!array) {
        perror("Failed to allocate memory for fixed array");
        return NULL;
    }
    for (long i = 0; i < size; i++) {
        array[i] = read_double(file);
    }
    return array;
}

double *read_minute_array(FILE *file, long forecast_minutes) {
    long total_elements = forecast_minutes / 5;
    return read_fixed_array(file, total_elements);
}

double **read_minute_array_cars(FILE *file, long forecast_minutes, long num_cars) {
    double **array = (double**)malloc(num_cars * sizeof(double *));
    if (!array) {
        perror("Failed to allocate memory for minute array cars");
        return NULL;
    }
    for (long i = 0; i < num_cars; i++) {
        array[i] = read_minute_array(file, forecast_minutes);
    }
    return array;
}

// Read from a file into the params struct
int read_params_from_file(FILE *file, params *p) {
    if (!file) {
        perror("Trying to read from a NULL file pointer");
        return -1;
    }
    p->minutes_now = read_int(file);
    p->forecast_minutes = read_int(file);
    long max_minutes = p->forecast_minutes + p->minutes_now;
    p->midnight_utc = read_int(file);
    p->soc_kw = read_double(file);
    p->soc_max = read_double(file);
    p->export_today_now = read_double(file);
    p->import_today_now = read_double(file);
    p->load_minutes_now = read_double(file);
    p->pv_today_now = read_double(file);
    p->iboost_today = read_double(file);
    p->charge_rate_now = read_double(file);
    p->discharge_rate_now = read_double(file);
    p->cost_today_sofar = read_double(file);
    p->carbon_today_sofar = read_double(file);
    p->debug_enable = read_bool(file);
    p->num_cars = read_int(file);
    p->car_charging_soc = read_fixed_array(file, p->num_cars);
    p->car_charging_soc_next = read_fixed_array(file, p->num_cars);
    p->car_charging_loss = read_double(file);
    p->reserve = read_double(file);
    p->metric_standing_charge = read_double(file);
    p->set_charge_freeze = read_bool(file);
    p->set_reserve_enable = read_bool(file);
    p->set_export_freeze = read_bool(file);
    p->set_export_freeze_only = read_bool(file);
    p->set_discharge_during_charge = read_bool(file);
    p->set_read_only = read_bool(file);
    p->set_charge_low_power = read_bool(file);
    p->set_export_low_power = read_bool(file);
    p->set_charge_window = read_bool(file);
    p->set_export_window = read_bool(file);
    p->charge_low_power_margin = read_double(file);
    p->car_charging_slots_fold = read_minute_array_cars(file, max_minutes, p->num_cars);
    p->car_charging_limit = read_fixed_array(file, p->num_cars);
    p->car_charging_from_battery = read_bool(file);
    p->iboost_enable = read_bool(file);
    p->iboost_on_export = read_bool(file);
    p->iboost_prevent_discharge = read_bool(file);
    p->carbon_enable = read_bool(file);
    p->iboost_next = read_double(file);
    p->iboost_max_energy = read_double(file);
    p->iboost_max_power = read_double(file);
    p->iboost_min_power = read_double(file);
    p->iboost_min_soc = read_double(file);
    p->iboost_solar = read_bool(file);
    p->iboost_solar_excess = read_bool(file);
    p->iboost_charging = read_bool(file);
    p->iboost_plan = read_minute_array(file, max_minutes);
    p->iboost_gas = read_bool(file);
    p->iboost_gas_export = read_bool(file);
    p->iboost_gas_scale = read_double(file);
    p->iboost_rate_threshold = read_double(file);
    p->iboost_rate_threshold_export = read_double(file);
    p->rate_gas = read_minute_array(file, max_minutes);
    p->inverter_loss = read_double(file);
    p->inverter_hybrid = read_bool(file);
    p->inverter_limit = read_double(file);
    p->export_limit = read_double(file);
    p->battery_rate_min = read_double(file);
    p->battery_rate_max_charge = read_double(file);
    p->battery_rate_max_discharge = read_double(file);
    p->battery_rate_max_charge_scaled = read_double(file);
    p->battery_rate_max_discharge_scaled = read_double(file);
    p->battery_charge_power_curve = read_fixed_array(file, 100);
    p->battery_discharge_power_curve = read_fixed_array(file, 100);
    p->battery_temperature = read_double(file);
    p->battery_temperature_charge_curve = read_fixed_array(file, 40);
    p->battery_temperature_discharge_curve = read_fixed_array(file, 40);
    p->battery_temperature_prediction = read_minute_array(file, max_minutes);
    p->battery_rate_max_scaling = read_double(file);
    p->battery_rate_max_scaling_discharge = read_double(file);
    p->battery_loss = read_double(file);
    p->battery_loss_discharge = read_double(file);
    p->best_soc_keep = read_double(file);
    p->best_soc_keep_weight = read_double(file);
    p->best_soc_min = read_double(file);
    p->car_charging_battery_size = read_fixed_array(file, p->num_cars);
    p->rate_import = read_minute_array(file, max_minutes);
    p->rate_export = read_minute_array(file, max_minutes);
    p->pv_forecast_minute_step = read_minute_array(file, max_minutes);
    p->pv_forecast_minute10_step = read_minute_array(file, max_minutes);
    p->load_minutes_step = read_minute_array(file, max_minutes);
    p->load_minutes_step10 = read_minute_array(file, max_minutes);
    p->carbon_intensity = read_minute_array(file, max_minutes);
    p->alert_active_keep = read_minute_array(file, max_minutes);
    p->iboost_running = read_bool(file);
    p->iboost_running_solar = read_bool(file);
    p->iboost_running_full = read_bool(file);
    p->inverter_can_charge_during_export = read_bool(file);
    return 0;
}


char* read_command(FILE *file) {
    char *command = (char*)malloc(5 * sizeof(char));
    if (!command) {
        perror("Failed to allocate memory for command");
        return NULL;
    }
    if (fread(command, sizeof(char), 4, file) != 4) {
        free(command);
        return NULL;
    }
    command[4] = '\0'; // Null-terminate the string
    return command;
}

int write_command(FILE *file, const char *command) {
    if (fwrite(command, sizeof(char), 4, file) != 4) {
        perror("Failed to write command to file");
        return -1;
    }
    return 0;
}
// Calculate a charge limit in percent
double calc_percent_limit(double charge_limit, double soc_max) {
    if (soc_max <= 0) {
        return 0.0;
    } else {
        return fmin((charge_limit / soc_max * 100.0) + 0.5, 100.0);
    }
}

// Calculate charge limits in percent for an array
void calc_percent_limit_array(double *charge_limit, long length, double soc_max, double *result) {
    for (long i = 0; i < length; i++) {
        if (soc_max <= 0) {
            result[i] = 0.0;
        } else {
            result[i] = fmin((charge_limit[i] / soc_max * 100.0) + 0.5, 100.0);
        }
    }
}

// Helper function to calculate difference
double get_diff(double battery_draw, double pv_dc, double pv_ac, double load_yesterday, double inverter_loss, double inverter_loss_recp) {
    double battery_balance = battery_draw + pv_dc;
    battery_balance = (battery_balance > 0) ? (battery_balance * inverter_loss) : (battery_balance * inverter_loss_recp);
    double diff = load_yesterday - battery_balance - pv_ac;
    return diff;
}

// Helper function to get total inverter power
double get_total_inverted(double battery_draw, double pv_dc, double pv_ac, double inverter_loss, bool inverter_hybrid) 
{
    double battery_balance = battery_draw + pv_dc;
    double total_inverted;
    
    if (battery_balance > 0) {
        total_inverted = battery_balance;
    } else {
        total_inverted = fabs(battery_balance) / inverter_loss;
    }
    
    if (inverter_hybrid) {
        total_inverted = total_inverted + pv_ac / inverter_loss;
    }
    
    return total_inverted;
}

// Find the battery temperature cap
double find_battery_temperature_cap(double battery_temperature, double *battery_temperature_curve, double soc_max, double max_rate)
{
    // Find the battery temperature cap
    long battery_temperature_idx = min(long(battery_temperature), 20);
    battery_temperature_idx = max(battery_temperature_idx, -20);

    double battery_temperature_adjust = battery_temperature_curve[battery_temperature_idx + 20];
    if (battery_temperature_adjust == 0)
    {
        if (battery_temperature_idx > 0) 
        {
            battery_temperature_adjust = battery_temperature_curve[39];
        }
        else {
            battery_temperature_adjust = battery_temperature_curve[0 + 20];
        }
    }
    double battery_temperature_rate_cap = soc_max * battery_temperature_adjust / 60.0;
    return min(battery_temperature_rate_cap, max_rate);
}
// Helper function to calculate charge rate based on charge curve
double get_charge_rate_curve(double soc, double charge_rate_setting, double soc_max, double battery_rate_max_charge, 
                           double *battery_charge_power_curve, double battery_rate_min, double battery_temperature, double *battery_temperature_curve)
{
    double soc_percent = calc_percent_limit(soc, soc_max);
    int soc_percent_int = (int)soc_percent;
    if (soc_percent_int < 0) soc_percent_int = 0;
    if (soc_percent_int >= 100) soc_percent_int = 99;
    
    double max_charge_rate = battery_rate_max_charge * battery_charge_power_curve[soc_percent_int];

    double max_rate_cap = find_battery_temperature_cap(battery_temperature, battery_temperature_curve, soc_max, battery_rate_max_charge);
    max_charge_rate = fmin(max_charge_rate, max_rate_cap);

    return fmax(fmin(charge_rate_setting, max_charge_rate), battery_rate_min);
}

// Helper function to calculate discharge rate based on discharge curve
double get_discharge_rate_curve(double soc, double discharge_rate_setting, double soc_max, double battery_rate_max_discharge,
                               double *battery_discharge_power_curve, double battery_rate_min, double battery_temperature, double *battery_temperature_curve) {

    double soc_percent = calc_percent_limit(soc, soc_max);
    int soc_percent_int = (int)soc_percent;
    if (soc_percent_int < 0) soc_percent_int = 0;
    if (soc_percent_int >= 100) soc_percent_int = 99;

    double max_discharge_rate = battery_rate_max_discharge * battery_discharge_power_curve[soc_percent_int];
    double max_rate_cap = find_battery_temperature_cap(battery_temperature, battery_temperature_curve, soc_max, battery_rate_max_discharge);
    max_discharge_rate = min(max_discharge_rate, max_rate_cap);

    return fmax(fmin(discharge_rate_setting, max_discharge_rate), battery_rate_min);
}

// Helper function to check if minute is in charge window
int in_charge_window(double *charge_window, double *charge_limit, long num_windows, long minute) {
    for (long i = 0; i < num_windows; i++) {
        long start = (long)charge_window[i * 2];     // start minute
        long end = (long)charge_window[i * 2 + 1];   // end minute
        double limit = charge_limit[i];
        
        if (minute >= start && minute < end && limit > 0) {
            return i;
        }
    }
    return -1;
}

// C++ version of find_charge_rate function
typedef struct {
    double rate;
    double rate_real;
} charge_rate_result;

charge_rate_result find_charge_rate(
    long minutes_now,
    double soc,
    double window_start,
    double window_end,
    double target_soc,
    double max_rate,
    double soc_max,
    double *battery_charge_power_curve,
    bool set_charge_low_power,
    double charge_low_power_margin,
    double battery_rate_min,
    double battery_rate_max_scaling,
    double battery_loss,
    double battery_temperature,
    double *battery_temperature_curve,
    double current_charge_rate
) {
    charge_rate_result result;
    const double MINUTE_WATT = 1000.0;
    const long PREDICT_STEP = 5;
    
    double margin = charge_low_power_margin;
    target_soc = dp2(target_soc);
        
    // Real achieved max rate
    double max_rate_real = get_charge_rate_curve(soc, max_rate, soc_max, max_rate, battery_charge_power_curve, battery_rate_min, battery_temperature, battery_temperature_curve) * battery_rate_max_scaling;
    
    if (set_charge_low_power) {
        long minutes_left = (long)(window_end - minutes_now - margin);
        long abs_minutes_left = (long)(window_end - minutes_now);
        
        // If we don't have enough minutes left go to max
        if (abs_minutes_left < 0) {
            result.rate = max_rate;
            result.rate_real = max_rate_real;
            return result;
        }
        
        // If we already have reached target go back to max
        if (dp2(soc) >= target_soc) {
            result.rate = max_rate;
            result.rate_real = max_rate_real;
            return result;
        }
        
        // Work out the charge left in kw
        double charge_left = dp2(target_soc - soc);
        
        // If we can never hit the target then go to max
        if (dp2(max_rate_real * abs_minutes_left) <= charge_left) {
            result.rate = max_rate;
            result.rate_real = max_rate_real;
            return result;
        }
        
        // What's the lowest we could go?
        double min_rate = charge_left / abs_minutes_left;
        long min_rate_w = (long)(min_rate * MINUTE_WATT);
        
        // Apply the curve at each rate to pick one that works
        long rate_w = (long)(max_rate * MINUTE_WATT);
        double best_rate = max_rate;
        double best_rate_real = max_rate_real;
        double highest_achievable_rate = 0;
        
        while (rate_w >= 400) {
            double rate = rate_w / MINUTE_WATT;
            if (rate_w >= min_rate_w) {
                double charge_now = soc;
                long minute = 0;
                double rate_scale_max = 0;
                
                // Compute over the time period, include the completion time
                for (minute = 0; minute < minutes_left; minute += PREDICT_STEP) {
                    double rate_scale = get_charge_rate_curve(charge_now, rate, soc_max, max_rate, battery_charge_power_curve, battery_rate_min, battery_temperature, battery_temperature_curve);
                    highest_achievable_rate = fmax(highest_achievable_rate, rate_scale);
                    rate_scale *= battery_rate_max_scaling;
                    rate_scale_max = fmax(rate_scale_max, rate_scale);
                    double charge_amount = rate_scale * PREDICT_STEP * battery_loss;
                    charge_now += charge_amount;
                    
                    if ((dp2(charge_now) >= target_soc) && (rate_scale_max < best_rate_real)) {
                        best_rate = rate;
                        best_rate_real = rate_scale_max;
                        break;
                    }
                }
            } else {
                break;
            }
            rate_w -= 100;
        }
        
        // Stick with current rate if it doesn't matter
        if (best_rate >= highest_achievable_rate && current_charge_rate >= highest_achievable_rate) {
            best_rate = current_charge_rate;
        }
        
        best_rate_real = get_charge_rate_curve(soc, best_rate, soc_max, max_rate, battery_charge_power_curve, battery_rate_min, battery_temperature, battery_temperature_curve) * battery_rate_max_scaling;
        
        result.rate = best_rate;
        result.rate_real = best_rate_real;
        return result;
    } else {
        result.rate = max_rate;
        result.rate_real = max_rate_real;
        return result;
    }
}

prediction_result* run_prediction(params *p, double *charge_limit, double *charge_window, long charge_window_len, double *export_window, long export_window_len, double *export_limits, bool pv10, long end_record, long step, FILE *handle, FILE *out_handle) {
    prediction_result *result = (prediction_result*)malloc(sizeof(prediction_result));
    if (!result) {
        perror("Failed to allocate memory for prediction result");
        return NULL;
    }
    
    // Initialize variables
    double soc = p->soc_kw;
    double soc_min = p->soc_max;
    double soc_min_minute = p->minutes_now;
    double export_kwh = p->export_today_now;
    double export_kwh_h0 = export_kwh;
    double import_kwh = p->import_today_now;
    double import_kwh_h0 = import_kwh;
    double load_kwh = p->load_minutes_now;
    double load_kwh_h0 = load_kwh;
    double pv_kwh = p->pv_today_now;
    double pv_kwh_h0 = pv_kwh;
    double iboost_today_kwh = p->iboost_today;
    double import_kwh_house = 0.0;
    double import_kwh_battery = 0.0;
    double carbon_g = 0.0;
    double battery_cycle = 0.0;
    double metric_keep = 0.0;
    bool four_hour_rule = true;
    double final_export_kwh = export_kwh;
    double final_import_kwh = import_kwh;
    double final_load_kwh = load_kwh;
    double final_pv_kwh = pv_kwh;
    double final_iboost_kwh = iboost_today_kwh;
    double final_import_kwh_house = import_kwh_house;
    double final_import_kwh_battery = import_kwh_battery;
    double final_battery_cycle = battery_cycle;
    double final_metric_keep = metric_keep;
    double final_carbon_g = carbon_g;
    double metric = p->cost_today_sofar;
    double final_soc = soc;
    double first_charge_soc = soc;
    double prev_soc = soc;
    double final_metric;
    long forecast_minutes = p->forecast_minutes;
    long forecast_minutes_step = forecast_minutes / 5;
    long minute_left = forecast_minutes;

    // Car charging state
    double *car_soc = (double*)malloc(p->num_cars * sizeof(double));
    for (long i = 0; i < p->num_cars; i++) {
        car_soc[i] = p->car_charging_soc[i];
    }
    double *final_car_soc = (double*)malloc(p->num_cars * sizeof(double));
    for (long i = 0; i < p->num_cars; i++) {
        final_car_soc[i] = car_soc[i];
    }
    double charge_rate_now = p->charge_rate_now;
    double discharge_rate_now = p->discharge_rate_now;
    double first_charge = end_record;
    double export_to_first_charge = 0;
    double clipped_today = 0;

    result->predict_soc = (double*)malloc(forecast_minutes_step * sizeof(double));
    // Initialize prediction arrays
    for (long i = 0; i < forecast_minutes_step; i++) {
        result->predict_soc[i] = 0.0;
    }

    result->car_charging_soc_next = (double*)malloc(p->num_cars * sizeof(double));
    for (long i = 0; i < p->num_cars; i++) {
        result->car_charging_soc_next[i] = car_soc[i];
    }
    double iboost_next = p->iboost_next;
    bool iboost_running = p->iboost_running;
    bool iboost_running_solar = p->iboost_running_solar;
    bool iboost_running_full = p->iboost_running_full;

    /// XXX: Remove interesecting window

    bool record = true;
    double inverter_loss_ac = 1.0;
    if (p->inverter_hybrid) {
        inverter_loss_ac = p->inverter_loss; 
    }
    double inverter_loss = p->inverter_loss;
    bool inverter_hybrid = p->inverter_hybrid;
    double inverter_loss_recp = 1.0 / inverter_loss;

    bool enable_standing_charge = false;
    bool enable_save_stats = false;
    bool car_enable = p->num_cars > 0;
    double inverter_limit = p->inverter_limit * step;
    double export_limit = p->export_limit * step;
    bool set_charge_low_power = p->set_charge_window && p->set_charge_low_power && false;
    bool carbon_enable = p->carbon_enable;
    double reserve = p->reserve;
    double soc_max = p->soc_max;
    double battery_loss = p->battery_loss;
    double battery_loss_discharge = p->battery_loss_discharge;
    double* battery_temperature_prediction = p->battery_temperature_prediction;
    double* active_alert_keep = p->alert_active_keep;
    double best_soc_keep_weight = p->best_soc_keep_weight;
    double best_soc_keep_orig = p->best_soc_keep;
    bool debug_enable = p->debug_enable;
    bool set_reserve_enable = p->set_reserve_enable;
    bool set_export_freeze = p->set_export_freeze;
    bool set_export_freeze_only = p->set_export_freeze_only;
    bool set_charge_window = p->set_charge_window;
    bool set_export_window = p->set_export_window;
    double battery_rate_max_charge = p->battery_rate_max_charge;
    double battery_rate_max_discharge = p->battery_rate_max_discharge;
    double* battery_temperature_charge_curve = p->battery_temperature_charge_curve;
    double battery_rate_min = p->battery_rate_min;
    double* carbon_intensity = p->carbon_intensity;
    bool set_discharge_during_charge = p->set_discharge_during_charge;
    double keep_minute_scaling = 1.0;
    
    // Main simulation loop
    long minute = 0;
    while (minute < forecast_minutes) {
        long minute_absolute = p->minutes_now + minute;
        long minute_index = minute / 5;
        long minute_absolute_index = minute_absolute / 5;
        prev_soc = soc; 
        double reserve_expected = reserve;
        double import_rate = p->rate_import[minute_absolute_index];
        double export_rate = p->rate_export[minute_absolute_index];

        // Alert?
        double alert_keep = active_alert_keep[minute_absolute_index];

        // Project battery temperature
        double battery_temperature = battery_temperature_prediction[minute_index];

        if (four_hour_rule) {
            keep_minute_scaling = fmin((minute / 256), 1.0) * best_soc_keep_weight;
        }
        else {
            keep_minute_scaling = best_soc_keep_weight;
        }

        // Get soc keep value
        double best_soc_keep = best_soc_keep_orig;

        if (alert_keep > 0) {
            keep_minute_scaling = fmax(keep_minute_scaling, 2.0);
            best_soc_keep = fmax(best_soc_keep, fmin(alert_keep / 100.0 * soc_max, soc_max));
        }

        // Find charge and discharge winddows
        int charge_window_n = in_charge_window(charge_window, charge_limit, charge_window_len, minute);
        int export_window_n = in_charge_window(export_window, export_limits, export_window_len, minute);
        bool charge_window_active = (charge_window_n >= 0);
        bool export_window_active = (export_window_n >= 0);
        double export_limit_now = export_window_active ? export_limits[export_window_n] : 100.0;

        // Find charge limit
        double charge_limit_n = 0;
        if (charge_window_active) {
            charge_limit_n = charge_limit[charge_window_n];
            if (p->set_charge_freeze && (charge_limit_n == reserve)) {
                charge_limit_n = fmax(soc, reserve);
            }
            if (set_reserve_enable && (soc >= charge_limit_n)) {
                reserve_expected = fmax(charge_limit_n, reserve);
            }
        }

        if (record && (minute >= end_record)) {
            record = false;
        }
        result->predict_soc[minute_index] = soc;

        // Ignore standing charge

        double load_yesterday = 0.0;
        double pv_now = 0.0;
        for (int offset = 0; offset < step; offset += 5) {
            load_yesterday += p->load_minutes_step[minute_index + offset];
            pv_now += p->pv_forecast_minute_step[minute_index + offset];
        }
        double pv_ac = 0.0;
        double pv_dc = 0.0;
        double diff = 0.0;

        // Count PV Kwh
        pv_kwh += pv_now;

        // Modelling reset of charge/discharge rate
        if (set_charge_window || set_export_window) {
            charge_rate_now = p->battery_rate_max_charge;
            discharge_rate_now = p->battery_rate_max_discharge;
        }
        // Simulate car charging
        bool car_freeze = false;

        double car_load = 0;
        if (car_enable) {
            // XXX:
        }
        else {
            car_load = 0;
        }

        // IBoost
        bool iboost_rate_okay = true;
        double iboost_amount = 0;
        bool iboost_freeze= false;

        if (p->iboost_enable) {
            // XXXX:
        }

        // Count load
        load_kwh += load_yesterday;

        //  discharge freeze, reset charge rate by default
        if (set_export_freeze) {
            // Freeze mode
            if (export_window_active && (export_limit_now < 100.0) && (set_export_freeze && (export_limit_now == 99.0 || set_export_freeze_only))) {
                charge_rate_now = battery_rate_min;  // 0
            }
        }

        // Set discharge during charge?
        if (charge_window_active) {
            if (!set_discharge_during_charge) {
                discharge_rate_now = battery_rate_min;
            } else if (set_charge_window && soc >= charge_limit_n && (fabs(calc_percent_limit(soc, soc_max) - calc_percent_limit(charge_limit_n, soc_max)) <= 1.0)) {
                discharge_rate_now = battery_rate_min;
            }
        }


        // Current real charge rate
        double charge_rate_now_curve = get_charge_rate_curve(soc, charge_rate_now, soc_max, battery_rate_max_charge, p->battery_charge_power_curve, battery_rate_min, battery_temperature, p->battery_temperature_charge_curve) * p->battery_rate_max_scaling;
        double charge_rate_now_curve_step = charge_rate_now_curve * step;
        double discharge_rate_now_curve = get_discharge_rate_curve(soc, discharge_rate_now, soc_max, battery_rate_max_discharge, p->battery_discharge_power_curve, battery_rate_min, battery_temperature, p->battery_temperature_discharge_curve) * p->battery_rate_max_scaling_discharge;
        double discharge_rate_now_curve_step = discharge_rate_now_curve * step;

        double battery_to_min = fmax(soc - reserve_expected, 0) * battery_loss_discharge;
        double battery_to_max = fmax(soc_max - soc, 0) * battery_loss;

        double discharge_min = reserve;
        if (export_window_active) {
            discharge_min = fmax(fmax(soc_max * export_limit_now / 100.0, reserve), p->best_soc_min);
        }
        double battery_draw = 0.0;

        if (!set_export_freeze_only && export_window_active && export_limit_now < 99.0 && (soc > discharge_min)) 
        {
            // Discharge enable
            discharge_rate_now = battery_rate_max_discharge;
            double export_rate_adjust;
            if (set_charge_low_power) {
                export_rate_adjust = 1.0 - (export_limit_now - floor(export_limit_now));
            } else {
                export_rate_adjust = 1.0;
            }
            discharge_rate_now = battery_rate_max_discharge * export_rate_adjust;
            discharge_rate_now_curve = get_discharge_rate_curve(soc, discharge_rate_now, soc_max, battery_rate_max_discharge, p->battery_discharge_power_curve, battery_rate_min, battery_temperature, p->battery_temperature_discharge_curve) * p->battery_rate_max_scaling_discharge;
            discharge_rate_now_curve_step = discharge_rate_now_curve * step;

            battery_draw = fmin(discharge_rate_now_curve_step, battery_to_min);

            pv_ac = pv_now * inverter_loss_ac;
            pv_dc = 0.0;

            // Exceed export limit?
            diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss, inverter_loss_recp);
            if (diff < 0 && fabs(diff) > export_limit) {
                double over_limit = fabs(diff) - export_limit;
                double reduce_by = over_limit;

                if (reduce_by > battery_draw) {
                    if (p->inverter_can_charge_during_export) {
                        reduce_by = reduce_by - battery_draw;
                        battery_draw = fmax(fmax(-reduce_by * inverter_loss, -battery_to_min), -charge_rate_now_curve_step);
                    } else {
                        battery_draw = 0.0;
                    }
                } else {
                    battery_draw = battery_draw - reduce_by;
                }

                if (inverter_hybrid && battery_draw < 0) {
                    pv_dc = fmin(fabs(battery_draw), pv_now);
                    pv_ac = (pv_now - pv_dc) * inverter_loss_ac;
                }
            }

            // Exceeds inverter limit, scale back discharge?
            double total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid);
            if (inverter_hybrid) {
                double over_limit = total_inverted - inverter_limit;
                if (total_inverted > inverter_limit) {
                    double reduce_by = over_limit;
                    if (reduce_by > battery_draw) {
                        reduce_by = reduce_by - battery_draw;
                        battery_draw = 0.0;
                        if (p->inverter_can_charge_during_export) {
                            battery_draw = fmax(fmax(-reduce_by * inverter_loss, -battery_to_min), -charge_rate_now_curve_step);
                        }
                    } else {
                        battery_draw = battery_draw - reduce_by;
                    }

                    if (battery_draw < 0) {
                        pv_dc = fmin(fabs(battery_draw), pv_now);
                    }
                    pv_ac = (pv_now - pv_dc) * inverter_loss_ac;
                }
            } else {
                if (total_inverted > inverter_limit) {
                    double over_limit = total_inverted - inverter_limit;
                    battery_draw = fmax(battery_draw - over_limit * inverter_loss, 0.0);
                }
            }

            // Once force discharge starts the four hour rule is disabled
            four_hour_rule = false;
        }
        else if (charge_window_active && (soc < charge_limit_n))
        {
                // Charge enable
                // Only tune charge rate on final plan not every simulation
                charge_rate_result charge_result = find_charge_rate(
                    minute_absolute,
                    soc,
                    charge_window[charge_window_n * 2],  // start
                    charge_window[charge_window_n * 2 + 1],  // end
                    charge_limit_n,
                    battery_rate_max_charge,
                    soc_max,
                    p->battery_charge_power_curve,
                    set_charge_low_power,
                    p->charge_low_power_margin,
                    battery_rate_min,
                    p->battery_rate_max_scaling,
                    battery_loss,
                    battery_temperature,
                    battery_temperature_charge_curve,
                    charge_rate_now
                );
                charge_rate_now = charge_result.rate;
                charge_rate_now_curve = charge_result.rate_real;
                charge_rate_now_curve_step = charge_rate_now_curve * step;

                battery_draw = -fmax(fmax(fmin(charge_rate_now_curve_step, fmax(charge_limit_n - soc, pv_now)), 0), -battery_to_max);
                first_charge = fmin(first_charge, minute);

                if (inverter_hybrid) {
                    pv_dc = fmin(fabs(battery_draw), pv_now);
                } else {
                    pv_dc = 0;
                }
                pv_ac = (pv_now - pv_dc) * inverter_loss_ac;

                if ((charge_limit_n - soc) < charge_rate_now_curve_step) {
                    // The battery will hit the charge limit in this period, so if the charge was spread over the period
                    // it could be done from solar, but in reality it will be full rate and then stop meaning the solar
                    // won't cover it and it will likely create an import.
                    double pv_compare = pv_dc + pv_ac;
                    if (pv_dc >= (charge_limit_n - soc) && (pv_compare < charge_rate_now_curve_step)) {
                        double charge_time_remains = (charge_limit_n - soc) / charge_rate_now_curve;  // Time in minute periods left
                        double pv_in_period = pv_compare / step * charge_time_remains;
                        double potential_import = fmin((charge_rate_now_curve * charge_time_remains) - pv_in_period, (charge_limit_n - soc));
                        metric_keep += fmax(potential_import * import_rate, 0);
                    }
                }
        }
        else {
            // ECO Mode
            pv_ac = pv_now * inverter_loss_ac;
            pv_dc = 0;
            diff = get_diff(0, pv_dc, pv_ac, load_yesterday, inverter_loss, inverter_loss_recp);

            double required_for_load = load_yesterday * inverter_loss_recp;
            double potential_to_charge;
            if (inverter_hybrid) {
                potential_to_charge = pv_now;
            } else {
                potential_to_charge = pv_ac;
            }

            diff = required_for_load - potential_to_charge;

            if (diff > 0) {
                battery_draw = fmin(fmin(fmin(diff, discharge_rate_now_curve_step), inverter_limit), battery_to_min);
                // battery_state = "e-";
            } else {
                battery_draw = fmax(fmax(fmax(diff, -charge_rate_now_curve_step), -inverter_limit), -battery_to_max);
                if (battery_draw < 0) {
                    // battery_state = "e+";
                } else {
                    // battery_state = "e~";
                }

                if (inverter_hybrid) {
                    pv_dc = fmin(fabs(battery_draw), pv_now);
                } else {
                    pv_dc = 0;
                }
                pv_ac = (pv_now - pv_dc) * inverter_loss_ac;
            }
        }
        
        // Clamp at inverter limit
        if (inverter_hybrid) {
            double battery_inverted = get_total_inverted(battery_draw, pv_dc, 0, inverter_loss, inverter_hybrid);
            if (battery_inverted > inverter_limit) {
                double over_limit = battery_inverted - inverter_limit;

                if (battery_draw + pv_dc > 0) {
                    battery_draw = fmax(battery_draw - over_limit, 0);
                } else {
                    battery_draw = fmin(battery_draw + over_limit * inverter_loss, 0);
                }

                // Adjustment to charging from solar case
                if (battery_draw < 0) {
                    pv_dc = fmin(fabs(battery_draw), pv_now);
                    pv_ac = (pv_now - pv_dc) * inverter_loss_ac;
                }
            }

            // Clip battery discharge back
            double total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid);
            if ((total_inverted > inverter_limit) && ((battery_draw + pv_dc) > 0)) {
                double over_limit = total_inverted - inverter_limit;
                if ((battery_draw + pv_dc) > 0) {
                    battery_draw = fmax(battery_draw - over_limit, 0);
                }

                if (battery_draw == 0) {
                    total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid);
                    over_limit = 0;
                    if (total_inverted > inverter_limit) {
                        over_limit = total_inverted - inverter_limit;
                    }
                    battery_draw = fmax(fmax(fmax(-over_limit * inverter_loss, -charge_rate_now_curve_step), -battery_to_max), -pv_ac);
                }

                if (battery_draw < 0) {
                    pv_dc = fmin(fabs(battery_draw), pv_now);
                    pv_ac = (pv_now - pv_dc) * inverter_loss_ac;
                }
            }
            // Clip solar
            total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid);
            if (total_inverted > inverter_limit) {
                double over_limit = total_inverted - inverter_limit;
                clipped_today += over_limit;
                pv_ac = fmax(pv_ac - over_limit * inverter_loss, 0);
            }
        } else {
            double total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid);
            if (total_inverted > inverter_limit) {
                double over_limit = total_inverted - inverter_limit;
                if (battery_draw > 0) {
                    battery_draw = fmax(battery_draw - over_limit, 0);
                } else {
                    battery_draw = fmin(battery_draw + over_limit * inverter_loss, 0);
                }
            }
        }

        // Export limit, clip PV output
        diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss, inverter_loss_recp);
        if (diff < 0 && fabs(diff) > export_limit) {
            double over_limit = fabs(diff) - export_limit;
            clipped_today += over_limit;
            pv_ac = fmax(pv_ac - over_limit, 0);
        }

        // Adjust battery soc
        if (battery_draw > 0) {
            soc = fmax(soc - battery_draw / battery_loss_discharge, reserve_expected);
        } else {
            soc = fmin(soc - battery_draw * battery_loss, soc_max);
        }
        soc = dp6(soc);    

        // XXX: IBOOOST Finally count
        
        // Count battery cycles
        battery_cycle = battery_cycle + fabs(battery_draw);
        
        // Calculate energy flows
        diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss, inverter_loss_recp);
        
        // Metric keep calculation
        if (best_soc_keep > 0 && soc <= best_soc_keep) {
            metric_keep += (best_soc_keep - soc) * import_rate * keep_minute_scaling * step / 60.0;
        }
        
        if (diff > 0) {
            // Import energy
            import_kwh += diff;

            if (carbon_enable) {
                carbon_g += diff * carbon_intensity[minute_absolute_index];
            }

            if (charge_window_active) {
                import_kwh_battery += diff;
            } else {
                import_kwh_house += diff;
            }
            metric += import_rate * diff;
            // Grid_state "<"
        } else {
            // Export energy
            double energy = -diff;
            export_kwh += energy;

            if (carbon_enable) {
                carbon_g -= energy * carbon_intensity[minute_absolute_index];
            }
            metric -= export_rate * energy;           
        }

        if (record) {
            // Store the number of minutes until the battery runs out
            if (soc <= reserve) {
                minute_left = fmin(minute, minute_left);
            }
            final_soc = soc;

            if (car_enable) {
                for (long car_n = 0; car_n < p->num_cars; car_n++) {
                    final_car_soc[car_n] = round(car_soc[car_n] * 1000.0) / 1000.0;
                    if (minute == 0) {
                        // Next car SOC
                        result->car_charging_soc_next[car_n] = round(car_soc[car_n] * 1000.0) / 1000.0;
                    }
                }
            }

            final_metric = metric;
            final_import_kwh = import_kwh;
            final_import_kwh_battery = import_kwh_battery;
            final_import_kwh_house = import_kwh_house;
            final_export_kwh = export_kwh;
            final_iboost_kwh += iboost_amount;
            final_battery_cycle = battery_cycle;
            final_metric_keep = metric_keep;
            final_carbon_g = carbon_g;
            final_load_kwh = load_kwh;
            final_pv_kwh = pv_kwh;

            // Store export data
            if (diff < 0) {
                double energy = -diff;
                // predict_export[minute] = energy;
                if (minute <= first_charge) {
                    export_to_first_charge += energy;
                }
            } else {
                // predict_export[minute] = 0;
            }

            // Soc at next charge start
            if (minute <= first_charge) {
                first_charge_soc = prev_soc;
            }

            // Record soc min
            if (soc < soc_min) {
                soc_min_minute = minute_absolute;
            }
            soc_min = fmin(soc_min, soc);
        }

        // Record state: XXX

        minute += step;
    }
    
    // Store final results
    result->final_metric = (final_metric);
    result->import_kwh_battery = (import_kwh_battery);
    result->import_kwh_house = (import_kwh_house);
    result->export_kwh = (export_kwh);
    result->soc_min = (soc_min);
    result->final_soc = (final_soc);
    result->soc_min_minute = soc_min_minute;
    result->final_battery_cycle = (final_battery_cycle);
    result->final_metric_keep = (final_metric_keep);
    result->final_iboost_kwh = (final_iboost_kwh);
    result->final_carbon_g = (final_carbon_g);
    
    // Store car charging SOC next
    for (long i = 0; i < p->num_cars; i++) {
        result->car_charging_soc_next[i] = car_soc[i];
    }
    
    // Store iBoost state
    result->iboost_next = iboost_next;
    result->iboost_running = iboost_running;
    result->iboost_running_solar = iboost_running_solar;
    result->iboost_running_full = iboost_running_full;
   
    // Clean up
    free(car_soc);
    
    return result;
}

int start_prediction(params *p, FILE *handle, FILE *out_handle) {

    // First we must read the prediction parameters from the input
    // charge_limit, charge_window, export_window, export_limits, pv10, end_record, save=None, step=PREDICT_STEP
    long charge_limit_len = read_int(handle);
    double* charge_limit = read_fixed_array(handle, charge_limit_len);
    long charge_window_len = read_int(handle);
    double* charge_window = read_fixed_array(handle, charge_window_len * 2);
    long export_window_len = read_int(handle);
    double* export_window = read_fixed_array(handle, export_window_len * 2);
    long export_limits_len = read_int(handle);
    double* export_limits = read_fixed_array(handle, export_limits_len);
    bool pv10 = read_bool(handle);
    long end_record = read_int(handle);
    long step = read_int(handle);

    // Call prediction command
    prediction_result* result = run_prediction(p, charge_limit, charge_window, charge_window_len, export_window, export_window_len, export_limits, pv10, end_record, step, handle, out_handle);

    /*
    fprintf(stderr, "Prediction completed: final_metric=%.2f, import_kwh_battery=%.2f, import_kwh_house=%.2f, export_kwh=%.2f, soc_min=%.2f, final_soc=%.2f, soc_min_minute=%.2f, final_battery_cycle=%.2f, final_metric_keep=%.2f, final_iboost_kwh=%.2f, final_carbon_g=%.2f\n",
           result->final_metric, result->import_kwh_battery, result->import_kwh_house,
           result->export_kwh, result->soc_min, result->final_soc,
           result->soc_min_minute, result->final_battery_cycle,
           result->final_metric_keep, result->final_iboost_kwh,
           result->final_carbon_g);
    */

    // Send results
    write_command(out_handle, "PRED");
    write_double(out_handle, result->final_metric);
    write_double(out_handle, result->import_kwh_battery);
    write_double(out_handle, result->import_kwh_house);
    write_double(out_handle, result->export_kwh);
    write_double(out_handle, result->soc_min);
    write_double(out_handle, result->final_soc);
    write_int(out_handle, result->soc_min_minute);
    write_double(out_handle, result->final_battery_cycle);
    write_double(out_handle, result->final_metric_keep);
    write_double(out_handle, result->final_iboost_kwh);
    write_double(out_handle, result->final_carbon_g);
    write_fixed_array(out_handle, p->forecast_minutes / 5, result->predict_soc);
    write_fixed_array(out_handle, p->num_cars, result->car_charging_soc_next);
    write_double(out_handle, result->iboost_next);
    write_bool(out_handle, result->iboost_running);
    write_bool(out_handle, result->iboost_running_solar);
    write_bool(out_handle, result->iboost_running_full);
    write_command(out_handle, "DONE");
    fflush(out_handle);

    // Free allocated memory
    free(result->predict_soc);
    free(result->car_charging_soc_next);
    free(result);
    free(charge_limit);
    free(charge_window);
    free(export_window);
    free(export_limits);

    return 0;
}

int main()
{
    params p;
    FILE* handle = stdin;
    FILE* out_handle = stdout;

    char* command = read_command(handle);
    if (strcmp(command, "INIT") != 0) {
        fprintf(stderr, "Invalid command: %s, expecting INIT\n", command);
        return EXIT_FAILURE;
    }
    free(command);
    // Read parameters from file
    if (read_params_from_file(handle, &p) != 0) {
        fprintf(stderr, "Error reading parameters from file\n");
        return EXIT_FAILURE;
    }
    command = read_command(handle);
    if (strcmp(command, "DONE") != 0) {
        fprintf(stderr, "Invalid command: %s, expecting DONE\n", command);
        return EXIT_FAILURE;
    }
    free(command);
    write_command(out_handle, "OKAY");
    fflush(out_handle);
    
    while (1) {
        command = read_command(handle);
        if (strcmp(command, "QUIT") == 0) {
            break; // Exit the loop on QUIT command
        } else if (strcmp(command, "PING") == 0) {
            write_command(out_handle, "PONG");
        } else if (strcmp(command, "PRED") == 0) {
            start_prediction(&p, handle, out_handle);
        } else {
            free(command);
            return EXIT_FAILURE;
        }
        free(command);
    }


    return EXIT_SUCCESS;
}