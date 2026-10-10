export const CONFIG_GROUPS = ['System', 'Solar', 'Load', 'Battery', 'Plan', 'Inverter', 'Car', 'Tariffs', 'Heating', 'iBoost', 'Other'] as const

/** Put each live setting in the first relevant, user-facing group. */
export function configGroup(name: string): typeof CONFIG_GROUPS[number] {
  if (/^(module$|class$|dependencies$|prefix$|timezone$|template$|web_ui$|version|expert_|performance_|active$|compare_|update$|auto_update|debug_|chat_|ai_|set_.*notify|set_read_only|carbon_|saverestore|overview_)/.test(name)) return 'System'
  if (/^(pv_|.*solar)/.test(name)) return 'Solar'
  if (/^(load_|holiday_)/.test(name)) return 'Load'
  if (/^(inverter_|balance_inverters)/.test(name)) return 'Inverter'
  if (/^(car_|octopus_intelligent)/.test(name)) return 'Car'
  if (/^(battery_|.*soc|set_reserve|charge_scaling)/.test(name)) return 'Battery'
  if (/^(rate_|combine_rate|.*tariff|octopus_saving|manual_(import|export)_rates)/.test(name)) return 'Tariffs'
  if (/^(predheat_|next_volume_temp|ashp_)/.test(name)) return 'Heating'
  if (/^iboost_/.test(name)) return 'iBoost'
  if (/^(metric_|calculate_|plan_|forecast_plan|mode$|manual_|combine_(charge|export)|set_(charge|export|discharge|freeze)|export_)/.test(name)) return 'Plan'
  return 'Other'
}
