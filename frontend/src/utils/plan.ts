export function isCarCharging(
    row: PlanRow
) {
    return (
        (row.car_charging ?? 0) >
        0.001
    )
}


export function isCarBatteryProtected(
    row: PlanRow,
    plan: Plan
) {
    return (
        isCarCharging(row) &&
        !plan.car_charging_from_battery &&
        plan.car_energy_reported_load
    )
}