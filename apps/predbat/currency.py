"""Extended monetary heuristic and precision calibration for upstream Predbat currency_scale.

Upstream v9.7.11 PR #6156 owns parsing of currency_scale and the ranges of
monetary HA settings. This module only rounds numerical optimisation outputs in
calibrated minor units. It NEVER converts tariff inputs or user-selected values.
"""


def round_currency(value, scale=1.0, digits=4):
    """Round a monetary result at a scale-invariant algorithmic precision.

    Preserves the original built-in round() exactly at scale=1.
    """
    if scale == 1.0:
        return round(value, digits)
    return round(value / scale, digits) * scale
