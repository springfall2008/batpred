# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Domestic load profile data tables for the annual prediction tool.

Data only, no behaviour, so the shapes can be recalibrated against real
consumption data without touching the code that consumes them.
"""

# Relative electricity consumption by hour of day for a typical UK domestic
# property, index 0 = 00:00. Unnormalised; half_hour_shape() normalises to 1.0.
# Shape: overnight trough, a modest morning peak, a midday plateau, and a
# pronounced evening peak from about 17:00 to 21:00.
HOURLY_SHAPE = [
    2.6,  # 00:00
    2.4,  # 01:00
    2.3,  # 02:00
    2.2,  # 03:00
    2.2,  # 04:00
    2.4,  # 05:00
    3.0,  # 06:00
    3.8,  # 07:00
    4.2,  # 08:00
    4.1,  # 09:00
    3.9,  # 10:00
    3.8,  # 11:00
    3.9,  # 12:00
    3.8,  # 13:00
    3.7,  # 14:00
    3.9,  # 15:00
    4.6,  # 16:00
    5.8,  # 17:00
    6.5,  # 18:00
    6.3,  # 19:00
    5.6,  # 20:00
    5.0,  # 21:00
    4.3,  # 22:00
    3.4,  # 23:00
]

# Relative daily consumption by month, index 0 = January. Captures the UK
# winter/summer split, which drives much of the annual answer. These are daily
# rates, so consumers must normalise by days-in-month to preserve the annual total.
MONTH_WEIGHTS = [
    1.20,  # January
    1.15,  # February
    1.05,  # March
    0.95,  # April
    0.88,  # May
    0.83,  # June
    0.82,  # July
    0.83,  # August
    0.90,  # September
    1.00,  # October
    1.12,  # November
    1.22,  # December
]

# Proportion of the SOURCE band's own energy moved to the destination band when
# the user selects a "night" or "day" biased profile. Expressed relative to the
# source band rather than to the whole day so the transfer can never exceed the
# energy available to move. Tunable against real data.
SHAPE_TILT_FRACTION = 0.30

# Half-hour slot indices, 0 = 00:00-00:30, 47 = 23:30-00:00.
NIGHT_BAND_SLOTS = list(range(0, 14))  # 00:00 - 07:00
DAY_BAND_SLOTS = list(range(14, 40))  # 07:00 - 20:00


def half_hour_shape():
    """Return the 48-slot half-hourly domestic shape, normalised to sum to exactly 1.0.

    Each hourly weight is split evenly across its two half-hour slots. The final
    slot absorbs any floating-point residue so the total is exactly 1.0.
    """
    total = float(sum(HOURLY_SHAPE))
    shape = []
    for weight in HOURLY_SHAPE:
        half = (weight / total) / 2.0
        shape.append(half)
        shape.append(half)
    residue = 1.0 - sum(shape)
    shape[-1] += residue
    return shape
