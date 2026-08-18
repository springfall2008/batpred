# fmt: off
"""
ML Training Memory Tests

Training a curriculum pass over three weeks of history builds a feature matrix of
around 11,500 samples by 1,446 float32 features - 66.7MB per copy. These tests pin the
numerics of dataset construction and normalisation so the memory work cannot change
them, and assert the normalisation reuses its input buffer rather than copying it.
"""

import math
from datetime import datetime, timezone

import numpy as np

from load_predictor import LoadPredictor, TOTAL_FEATURES


def _predictor_with_stats(samples=64, seed=7):
    """Build a predictor with normalisation stats fitted to a fixed synthetic matrix."""
    np.random.seed(seed)
    features = (np.random.randn(samples, TOTAL_FEATURES) * 3.0 + 1.5).astype(np.float32)
    predictor = LoadPredictor(learning_rate=0.001)
    return predictor, features


def test_normalize_features_values_are_stable(my_predbat=None):
    """Normalisation produces the documented z-score, whatever the implementation"""
    print("\n=== Testing _normalize_features() numerics ===")
    failed = 0

    predictor, features = _predictor_with_stats()
    reference = features.copy()

    result = predictor._normalize_features(features.copy(), fit=True)

    expected = (reference - predictor.feature_mean) / predictor.feature_std
    if not np.allclose(result, expected, rtol=1e-5, atol=1e-6):
        worst = float(np.max(np.abs(result - expected)))
        print("ERROR: normalised values drifted from the z-score definition, max diff {}".format(worst))
        failed += 1
    else:
        print("✓ normalised output matches (X - mean) / std")

    # The fitted statistics themselves must stay accurate against a float64 reference
    ref_mean = reference.astype(np.float64).mean(axis=0)
    if not np.allclose(predictor.feature_mean, ref_mean, rtol=1e-4, atol=1e-5):
        worst = float(np.max(np.abs(predictor.feature_mean - ref_mean)))
        print("ERROR: fitted mean drifted from the float64 reference, max diff {}".format(worst))
        failed += 1
    else:
        print("✓ fitted mean matches a float64 reference")

    ref_std = reference.astype(np.float64).std(axis=0)
    if not np.allclose(predictor.feature_std, ref_std, rtol=1e-3, atol=1e-4):
        worst = float(np.max(np.abs(predictor.feature_std - ref_std)))
        print("ERROR: fitted std drifted from the float64 reference, max diff {}".format(worst))
        failed += 1
    else:
        print("✓ fitted std matches a float64 reference")

    return failed


def test_normalize_features_reuses_the_input_buffer(my_predbat=None):
    """in_place normalisation must not allocate a second copy of the feature matrix"""
    print("\n=== Testing _normalize_features() in-place behaviour ===")
    failed = 0

    predictor, features = _predictor_with_stats()
    predictor._normalize_features(features.copy(), fit=True)

    subject = features.copy()
    result = predictor._normalize_features(subject, fit=False, in_place=True)

    if result is not subject:
        print("ERROR: in_place normalisation returned a new array; at 11,500 samples that is a 66.7MB copy")
        return failed + 1
    print("✓ in_place normalisation writes into the array it was given")

    expected = (features - predictor.feature_mean) / predictor.feature_std
    if not np.allclose(result, expected, rtol=1e-5, atol=1e-6):
        print("ERROR: in_place normalisation produced different values to the copying path")
        failed += 1
    else:
        print("✓ in_place result matches the copying path")

    # The default must keep the caller's array untouched
    untouched = features.copy()
    original = untouched.copy()
    predictor._normalize_features(untouched, fit=False)
    if not np.array_equal(untouched, original):
        print("ERROR: default call mutated the caller's array")
        failed += 1
    else:
        print("✓ default call leaves the caller's array untouched")

    return failed


def test_normalisation_stays_float32(my_predbat=None):
    """Normalising must not promote the feature matrix to float64

    The network's weights, the dataset and the activations are all float32. A float64
    minimum-std array promotes the whole normalised matrix to float64, doubling a 66.7MB
    training matrix and running every matmul in double precision against float32 weights.
    """
    print("\n=== Testing normalisation dtype ===")
    failed = 0

    predictor, features = _predictor_with_stats()
    predictor._normalize_features(features.copy(), fit=True)

    if predictor._get_min_std_array(TOTAL_FEATURES).dtype != np.float32:
        print("ERROR: minimum-std array is {}, expected float32".format(predictor._get_min_std_array(TOTAL_FEATURES).dtype))
        failed += 1
    else:
        print("✓ minimum-std array is float32")

    if predictor.feature_std.dtype != np.float32:
        print("ERROR: fitted std is {}, expected float32".format(predictor.feature_std.dtype))
        failed += 1
    else:
        print("✓ fitted std stays float32")

    result = predictor._normalize_features(features.copy(), fit=False)
    if result.dtype != np.float32:
        print("ERROR: normalised matrix is {}, expected float32 - a 66.7MB matrix becomes 133.4MB".format(result.dtype))
        failed += 1
    else:
        print("✓ normalised matrix stays float32")

    return failed


def _synthetic_history(days=5):
    """Build deterministic load/pv/temp/rate dicts spanning the given number of days."""
    minutes = days * 24 * 60
    load, pv, temp, imp, exp = {}, {}, {}, {}, {}
    for minute in range(0, minutes, 5):
        phase = minute / 180.0
        load[minute] = 0.05 + 0.03 * math.sin(phase)
        pv[minute] = max(0.0, 0.09 * math.sin(phase / 4.0))
        temp[minute] = 12.0 + 5.0 * math.cos(phase / 6.0)
        imp[minute] = 20.0 + 4.0 * math.sin(phase / 3.0)
        exp[minute] = 8.0 + 2.0 * math.cos(phase / 5.0)
    return load, pv, temp, imp, exp


def test_create_dataset_output_is_stable(my_predbat=None):
    """Building the feature matrix must produce the same data whatever the construction

    This pins the dataset so the memory work - preallocating the matrix instead of
    building a Python list of per-sample rows - cannot silently change what is trained on.
    """
    print("\n=== Testing _create_dataset() output ===")
    failed = 0

    load, pv, temp, imp, exp = _synthetic_history(days=5)
    now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
    predictor = LoadPredictor(learning_rate=0.001)

    X_train, y_train, weights, X_val, y_val = predictor._create_dataset(load, now, pv_minutes=pv, temp_minutes=temp, import_rates=imp, export_rates=exp, validation_holdout_hours=24)

    if X_train is None:
        print("ERROR: no dataset produced from 5 days of synthetic history")
        return failed + 1

    for name, array, dtype in (("X_train", X_train, np.float32), ("y_train", y_train, np.float32), ("weights", weights, np.float32), ("X_val", X_val, np.float32)):
        if array.dtype != dtype:
            print("ERROR: {} dtype is {}, expected {}".format(name, array.dtype, dtype))
            failed += 1

    if X_train.shape[1] != TOTAL_FEATURES:
        print("ERROR: X_train has {} features, expected {}".format(X_train.shape[1], TOTAL_FEATURES))
        failed += 1
    elif X_train.shape[0] != y_train.shape[0] or X_train.shape[0] != weights.shape[0]:
        print("ERROR: ragged dataset: X {} y {} weights {}".format(X_train.shape, y_train.shape, weights.shape))
        failed += 1
    else:
        print("✓ {} samples x {} features, aligned with targets and weights".format(X_train.shape[0], X_train.shape[1]))

    if not np.all(np.isfinite(X_train)):
        print("ERROR: X_train contains non-finite values")
        failed += 1
    else:
        print("✓ all feature values finite")

    # Time-decay weights are normalised to sum to the sample count
    if abs(float(np.sum(weights)) - len(weights)) > 0.01 * len(weights):
        print("ERROR: weights sum to {}, expected about {}".format(float(np.sum(weights)), len(weights)))
        failed += 1
    else:
        print("✓ time-decay weights normalised to the sample count")

    # A checksum over the feature matrix catches any change in row content or ordering.
    # Recorded from the list-of-rows construction this replaced; if numpy ever changes its
    # summation this may need re-recording, but it must not move for a refactor.
    # X_train assembles rows on demand; materialise it explicitly for the checksum
    checksum = float(np.sum(np.asarray(X_train).astype(np.float64)))
    expected_checksum = 12977609.094096
    if abs(checksum - expected_checksum) > abs(expected_checksum) * 1e-9:
        print("ERROR: feature matrix changed - checksum {:.6f}, expected {:.6f}".format(checksum, expected_checksum))
        failed += 1
    else:
        print("✓ feature matrix checksum unchanged ({:.6f})".format(checksum))

    if X_train.shape[0] != 1151:
        print("ERROR: sample count changed: {} rows, expected 1151".format(X_train.shape[0]))
        failed += 1
    else:
        print("✓ sample count unchanged (1151)")

    return failed


def test_fitted_statistics_are_accurate_on_large_datasets(my_predbat=None):
    """Fitting normalisation stats must not lose precision to float32 accumulation

    np.mean/np.std accumulate in float32 for a float32 input, which drifts on the feature
    groups that sit far from zero - temperature around 12C, import rates around 20p. It also
    materialises the deviations array internally, another full copy of a 66.7MB matrix just
    to produce 1,446 numbers. Accumulating over row blocks in float64 fixes both.
    """
    print("\n=== Testing fitted statistics accuracy ===")
    failed = 0

    np.random.seed(3)
    # Large mean, small spread: the case where a float32 accumulator loses its low bits
    features = (np.random.randn(20000, TOTAL_FEATURES // 32).astype(np.float32) * 0.01 + 500.0).astype(np.float32)

    predictor = LoadPredictor(learning_rate=0.001)
    predictor._normalize_features(features.copy(), fit=True)

    reference = features.astype(np.float64)
    mean_error = float(np.max(np.abs(predictor.feature_mean - reference.mean(axis=0))))
    std_error = float(np.max(np.abs(predictor.feature_std - reference.std(axis=0))))

    # A float32 accumulator lands around 2.2e-04 on this data. float64 accumulation reduces
    # that to the float32 representation floor of the stored result - eps * 500 is about 6e-05
    # - so the threshold sits between the two rather than at zero.
    if mean_error > 5e-5:
        print("ERROR: fitted mean drifted {:.3e} from the float64 reference (float32 accumulation)".format(mean_error))
        failed += 1
    else:
        print("✓ fitted mean accurate to {:.3e}".format(mean_error))

    if std_error > 1e-6:
        print("ERROR: fitted std drifted {:.3e} from the float64 reference".format(std_error))
        failed += 1
    else:
        print("✓ fitted std accurate to {:.3e}".format(std_error))

    return failed


def test_create_dataset_skips_gaps_in_the_history(my_predbat=None):
    """Samples whose lookback window crosses a gap must be skipped, not half-written

    Real history has gaps where the sensor stopped reporting. A sample spanning one cannot
    be built, so the row is skipped - the dataset must stay dense and the targets must line
    up with it.
    """
    print("\n=== Testing _create_dataset() with gaps ===")
    failed = 0

    load, pv, temp, imp, exp = _synthetic_history(days=6)
    # Punch two holes in the load data, as a sensor dropout would
    for minute in range(2 * 24 * 60, 2 * 24 * 60 + 600, 5):
        load.pop(minute, None)
    for minute in range(4 * 24 * 60, 4 * 24 * 60 + 300, 5):
        load.pop(minute, None)

    now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
    predictor = LoadPredictor(learning_rate=0.001)

    result = predictor._create_dataset(load, now, pv_minutes=pv, temp_minutes=temp, import_rates=imp, export_rates=exp, validation_holdout_hours=24)
    X_train, y_train, weights, X_val, y_val = result

    if X_train is None:
        print("ERROR: gaps caused the whole dataset to be discarded")
        return failed + 1
    print("✓ dataset still built with gaps present")

    if X_train.shape[0] != y_train.shape[0]:
        print("ERROR: {} feature rows but {} targets - skipped rows were still counted".format(X_train.shape[0], y_train.shape[0]))
        failed += 1
    elif X_train.shape[0] != weights.shape[0]:
        print("ERROR: {} feature rows but {} weights".format(X_train.shape[0], weights.shape[0]))
        failed += 1
    else:
        print("✓ {} rows, targets and weights all aligned".format(X_train.shape[0]))

    if y_train.dtype != np.float32 or not np.all(np.isfinite(y_train)):
        print("ERROR: targets are {} and finite={}".format(y_train.dtype, bool(np.all(np.isfinite(y_train)))))
        failed += 1
    else:
        print("✓ targets are finite float32")

    if not np.all(np.isfinite(X_train)):
        print("ERROR: feature matrix has non-finite values - a skipped row was left unwritten")
        failed += 1
    else:
        print("✓ every feature row fully written")

    return failed


def test_windowed_features_indexes_like_an_array(my_predbat=None):
    """The dataset must answer the indexing an array would, not just the batch case

    Training only ever gathers with an index array, but the object stands in for the feature
    matrix everywhere else - diagnostics, tests, anything reaching for a single row - so a
    scalar index must give one row rather than raising.
    """
    print("\n=== Testing WindowedFeatures indexing ===")
    failed = 0

    load, pv, temp, imp, exp = _synthetic_history(days=5)
    now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
    predictor = LoadPredictor(learning_rate=0.001)
    X_train, y_train, weights, X_val, y_val = predictor._create_dataset(load, now, pv_minutes=pv, temp_minutes=temp, import_rates=imp, export_rates=exp, validation_holdout_hours=24)

    if X_train is None:
        print("ERROR: no dataset built")
        return failed + 1

    try:
        row = X_train[0]
    except Exception as exc:
        print("ERROR: scalar index raised {}: {}".format(type(exc).__name__, exc))
        return failed + 1

    if np.shape(row) != (TOTAL_FEATURES,):
        print("ERROR: scalar index gave shape {}, expected ({},)".format(np.shape(row), TOTAL_FEATURES))
        failed += 1
    else:
        print("✓ scalar index gives a single row")

    batch = X_train[np.array([0, 1])]
    if not np.array_equal(batch[0], row):
        print("ERROR: scalar and array indexing disagree on row 0")
        failed += 1
    else:
        print("✓ scalar and array indexing agree")

    if X_train.dtype != np.dtype(np.float32):
        print("ERROR: dtype is {}, expected a float32 dtype".format(X_train.dtype))
        failed += 1
    else:
        print("✓ dtype reports float32")

    if X_train.shape != (len(X_train), TOTAL_FEATURES):
        print("ERROR: shape {} disagrees with len {}".format(X_train.shape, len(X_train)))
        failed += 1
    else:
        print("✓ shape agrees with len")

    return failed


def run_ml_memory_tests(my_predbat=None):
    """Run all ML training memory tests"""
    print("\n" + "=" * 80)
    print("ML Training Memory Tests")
    print("=" * 80)

    failed = 0
    failed += test_normalize_features_values_are_stable(my_predbat)
    failed += test_normalize_features_reuses_the_input_buffer(my_predbat)
    failed += test_normalisation_stays_float32(my_predbat)
    failed += test_create_dataset_output_is_stable(my_predbat)
    failed += test_fitted_statistics_are_accurate_on_large_datasets(my_predbat)
    failed += test_create_dataset_skips_gaps_in_the_history(my_predbat)
    failed += test_windowed_features_indexes_like_an_array(my_predbat)

    print("\n" + "=" * 80)
    if failed == 0:
        print("✅ All ML training memory tests passed!")
    else:
        print(f"❌ {failed} ML training memory test(s) failed")
    print("=" * 80)

    return failed
