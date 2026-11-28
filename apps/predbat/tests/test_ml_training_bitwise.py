# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
"""
ML Training Bit-identity Tests

LoadML's initial training is chaotic: a change of one part in a million to the arithmetic of
one step gives a visibly different model and forecast. So the training step may only be made
faster in ways that leave every bit of its output unchanged. These tests keep frozen copies of
the reference implementations of the optimiser step and the forward and backward passes, and
check the live ones against them byte for byte (so a -0.0 or a NaN payload counts), and check
that the optimiser step no longer allocates weight-sized temporaries.
"""

import copy
import tracemalloc

import numpy as np

import load_predictor
from load_predictor import LOOKBACK_STEPS, NUM_TIME_FEATURES, LoadPredictor, TOTAL_FEATURES, WindowedFeatures


def reference_relu(x):
    """ReLU as the reference implementation computed it."""
    return np.maximum(0, x)


def reference_relu_derivative(x):
    """ReLU's derivative as the reference implementation computed it."""
    return (x > 0).astype(np.float32)


def reference_huber_loss_derivative(y_true, y_pred, delta=1.0):
    """Huber loss derivative as the reference implementation computed it."""
    error = y_pred - y_true
    abs_error = np.abs(error)
    return np.where(abs_error <= delta, error, delta * np.sign(error)) / y_true.shape[0]


def reference_forward(predictor, X, training=False):
    """The forward pass before the in-place rewrite (load_predictor.py, Nov 2026)."""
    activations = [X]
    pre_activations = []
    dropout_masks = []
    current = X
    for i, (w, b) in enumerate(zip(predictor.weights, predictor.biases)):
        z = np.dot(current, w) + b
        pre_activations.append(z)
        if i < len(predictor.weights) - 1:
            current = reference_relu(z)
            if training and predictor.dropout_rate > 0.0:
                mask = (np.random.rand(*current.shape) > predictor.dropout_rate).astype(np.float32)
                mask /= 1.0 - predictor.dropout_rate
                current = current * mask
                dropout_masks.append(mask)
            else:
                dropout_masks.append(None)
        else:
            current = z
        activations.append(current)
    return current, activations, pre_activations, dropout_masks


def reference_backward(predictor, y_true, activations, pre_activations, sample_weights=None, dropout_masks=None, huber_delta=1.35):
    """The backward pass before the in-place rewrite (load_predictor.py, Nov 2026)."""
    delta = reference_huber_loss_derivative(y_true, activations[-1], delta=huber_delta)
    if sample_weights is not None:
        delta = delta * sample_weights.reshape(-1, 1)
    weight_grads = []
    bias_grads = []
    for i in range(len(predictor.weights) - 1, -1, -1):
        weight_grads.insert(0, np.dot(activations[i].T, delta))
        bias_grads.insert(0, np.sum(delta, axis=0))
        if i > 0:
            delta = np.dot(delta, predictor.weights[i].T) * reference_relu_derivative(pre_activations[i - 1])
            if dropout_masks is not None and (i - 1) < len(dropout_masks) and dropout_masks[i - 1] is not None:
                delta = delta * dropout_masks[i - 1]
    weight_grads = [np.clip(g, -10.0, 10.0) for g in weight_grads]
    bias_grads = [np.clip(g, -10.0, 10.0) for g in bias_grads]
    return weight_grads, bias_grads


def reference_adam_update(predictor, weight_grads, bias_grads, beta1=0.9, beta2=0.999, epsilon=1e-8, lr=None):
    """The optimiser step before the in-place rewrite (load_predictor.py, Nov 2026)."""
    predictor.adam_t += 1
    effective_lr = lr if lr is not None else predictor.learning_rate
    for i in range(len(predictor.weights)):
        predictor.m_weights[i] = beta1 * predictor.m_weights[i] + (1 - beta1) * weight_grads[i]
        predictor.v_weights[i] = beta2 * predictor.v_weights[i] + (1 - beta2) * (weight_grads[i] ** 2)
        m_hat = predictor.m_weights[i] / (1 - beta1**predictor.adam_t)
        v_hat = predictor.v_weights[i] / (1 - beta2**predictor.adam_t)
        predictor.weights[i] -= effective_lr * m_hat / (np.sqrt(v_hat) + epsilon)
        if predictor.weight_decay > 0:
            predictor.weights[i] *= 1 - effective_lr * predictor.weight_decay
        predictor.m_biases[i] = beta1 * predictor.m_biases[i] + (1 - beta1) * bias_grads[i]
        predictor.v_biases[i] = beta2 * predictor.v_biases[i] + (1 - beta2) * (bias_grads[i] ** 2)
        m_hat = predictor.m_biases[i] / (1 - beta1**predictor.adam_t)
        v_hat = predictor.v_biases[i] / (1 - beta2**predictor.adam_t)
        predictor.biases[i] -= effective_lr * m_hat / (np.sqrt(v_hat) + epsilon)
        predictor.weights[i] = np.nan_to_num(predictor.weights[i], copy=False, nan=0.0, posinf=0.0, neginf=0.0)
        predictor.biases[i] = np.nan_to_num(predictor.biases[i], copy=False, nan=0.0, posinf=0.0, neginf=0.0)


def _same_bytes(a, b):
    """Whether two arrays hold exactly the same bits in the same shape and dtype."""
    a, b = np.asarray(a), np.asarray(b)
    return a.dtype == b.dtype and a.shape == b.shape and np.ascontiguousarray(a).tobytes() == np.ascontiguousarray(b).tobytes()


def _all_same(first, second):
    """Whether two lists of arrays (None allowed) are bit-identical element by element."""
    if len(first) != len(second):
        return False
    for a, b in zip(first, second):
        if (a is None) != (b is None):
            return False
        if a is not None and not _same_bytes(a, b):
            return False
    return True


def _predictor(seed=3):
    """A predictor with initialised weights and some non-zero optimiser state."""
    predictor = LoadPredictor(log_func=lambda message: None, learning_rate=0.001, weight_decay=0.01, dropout_rate=0.1)
    predictor._initialize_weights()
    rng = np.random.default_rng(seed)
    for group in (predictor.m_weights, predictor.v_weights, predictor.m_biases, predictor.v_biases):
        for i, array in enumerate(group):
            group[i] = (np.abs(rng.standard_normal(array.shape)) * 1e-3).astype(np.float32)
    predictor.adam_t = 7
    return predictor


def _grads(predictor, seed, scale=1.0):
    """Gradients shaped like the predictor's weights and biases, float32, as _backward returns them."""
    rng = np.random.default_rng(seed)
    weight_grads = [(rng.standard_normal(w.shape) * scale).astype(np.float32) for w in predictor.weights]
    bias_grads = [(rng.standard_normal(b.shape) * scale).astype(np.float32) for b in predictor.biases]
    return weight_grads, bias_grads


def _state(predictor):
    """Every array the optimiser step writes, in a fixed order."""
    return predictor.weights + predictor.biases + predictor.m_weights + predictor.v_weights + predictor.m_biases + predictor.v_biases


def test_adam_update_matches_the_reference_bitwise(my_predbat=None):
    """Several optimiser steps, with and without a learning-rate override, give the reference's bits exactly"""
    print("\n=== Testing _adam_update() against the reference, bit for bit ===")
    failed = 0
    live = _predictor()
    reference = copy.deepcopy(live)
    for step in range(5):
        weight_grads, bias_grads = _grads(live, seed=100 + step, scale=3.0)
        # train() passes the cosine schedule's np.float64, which makes NumPy compute the step in float64; also a Python float, and None
        lr = (None, 0.0007 + step * 1e-5, np.float64(0.0009) - step * np.float64(1e-5))[step % 3]
        reference_adam_update(reference, [g.copy() for g in weight_grads], [g.copy() for g in bias_grads], lr=lr)
        live._adam_update([g.copy() for g in weight_grads], [g.copy() for g in bias_grads], lr=lr)
        if not _all_same(_state(live), _state(reference)) or live.adam_t != reference.adam_t:
            print("ERROR: optimiser state differs from the reference after step {}".format(step + 1))
            failed += 1
            break
    if not failed:
        print("✓ weights, biases and both moments match the reference bit for bit over 5 steps (np.float64, float and default rates)")
    return failed


def test_adam_update_matches_the_reference_on_non_finite_values(my_predbat=None):
    """Infinite and NaN gradients and weights are cleared exactly as the reference clears them"""
    print("\n=== Testing _adam_update() against the reference with non-finite values ===")
    failed = 0
    live = _predictor()
    live.weights[0][0, :4] = [np.inf, -np.inf, np.nan, -0.0]
    live.biases[1][:2] = [np.nan, np.inf]
    reference = copy.deepcopy(live)
    weight_grads, bias_grads = _grads(live, seed=7)
    weight_grads[2][0, 0] = np.inf
    weight_grads[0][1, 1] = np.nan
    bias_grads[0][0] = -np.inf
    reference_adam_update(reference, [g.copy() for g in weight_grads], [g.copy() for g in bias_grads])
    live._adam_update([g.copy() for g in weight_grads], [g.copy() for g in bias_grads])
    if not _all_same(_state(live), _state(reference)):
        print("ERROR: non-finite handling differs from the reference")
        failed += 1
    elif not all(np.isfinite(array).all() for array in live.weights + live.biases):
        print("ERROR: weights or biases left non-finite")
        failed += 1
    else:
        print("✓ non-finite weights, biases and gradients handled exactly as the reference does")
    return failed


def _training_dtype_grads(predictor, seed):
    """Gradients in the dtypes train() really passes: float64, since the He-initialised weights are float64."""
    weight_grads, bias_grads = _grads(predictor, seed, scale=2.0)
    return [g.astype(np.float64) for g in weight_grads], [g.astype(np.float64) for g in bias_grads]


def test_adam_update_matches_the_reference_with_training_dtypes(my_predbat=None):
    """From a fresh initialisation, with float64 gradients and the schedule's np.float64 rate (float32 biases whose moments
    the first step promotes to float64), several steps give the reference's bits and dtypes exactly"""
    print("\n=== Testing _adam_update() against the reference with training's dtypes ===")
    failed = 0
    live = LoadPredictor(log_func=lambda message: None, learning_rate=0.001, weight_decay=0.01, dropout_rate=0.1)
    live._initialize_weights()
    reference = copy.deepcopy(live)
    for step in range(4):
        weight_grads, bias_grads = _training_dtype_grads(live, seed=300 + step)
        lr = np.float64(0.001) * np.float64(0.9 + step / 100.0)
        reference_adam_update(reference, [g.copy() for g in weight_grads], [g.copy() for g in bias_grads], lr=lr)
        live._adam_update([g.copy() for g in weight_grads], [g.copy() for g in bias_grads], lr=lr)
        if not _all_same(_state(live), _state(reference)):
            print("ERROR: optimiser state differs from the reference after step {} with training's dtypes".format(step + 1))
            failed += 1
            break
    if not failed:
        print("✓ 4 steps with training's dtypes match the reference bit for bit, dtypes included")
    return failed


def test_adam_update_with_training_dtypes_allocates_no_weight_sized_temporaries(my_predbat=None):
    """With training's float64 state, a warm step still allocates nothing the size of a weight matrix"""
    print("\n=== Testing _adam_update() allocations with training's dtypes ===")
    failed = 0
    predictor = LoadPredictor(log_func=lambda message: None, learning_rate=0.001, weight_decay=0.01, dropout_rate=0.1)
    predictor._initialize_weights()
    weight_grads, bias_grads = _training_dtype_grads(predictor, seed=12)
    for _ in range(2):  # the first step promotes the bias moments and makes the buffers
        predictor._adam_update([g.copy() for g in weight_grads], [g.copy() for g in bias_grads], lr=np.float64(0.001))
    largest = max(w.nbytes for w in predictor.weights)
    fresh_w, fresh_b = [g.copy() for g in weight_grads], [g.copy() for g in bias_grads]
    tracemalloc.start()
    try:
        predictor._adam_update(fresh_w, fresh_b, lr=np.float64(0.001))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    if peak >= largest // 2:
        print("ERROR: a warm step with training's dtypes allocated {:.2f} MB at its peak (largest weight matrix {:.2f} MB)".format(peak / 1048576.0, largest / 1048576.0))
        failed += 1
    else:
        print("✓ a warm step with training's dtypes peaks at {:.3f} MB of new allocations".format(peak / 1048576.0))
    return failed


def test_adam_update_allocates_no_weight_sized_temporaries(my_predbat=None):
    """Once warm, an optimiser step reuses its buffers: it never allocates an array the size of the largest weight matrix"""
    print("\n=== Testing _adam_update() allocations ===")
    failed = 0
    predictor = _predictor()
    weight_grads, bias_grads = _grads(predictor, seed=11)
    predictor._adam_update([g.copy() for g in weight_grads], [g.copy() for g in bias_grads])  # warm: buffers made here
    largest = max(w.nbytes for w in predictor.weights)
    fresh_w, fresh_b = [g.copy() for g in weight_grads], [g.copy() for g in bias_grads]
    tracemalloc.start()
    try:
        predictor._adam_update(fresh_w, fresh_b)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    if peak >= largest // 2:
        print("ERROR: a warm optimiser step allocated {:.2f} MB at its peak, against a {:.2f} MB largest weight matrix".format(peak / 1048576.0, largest / 1048576.0))
        failed += 1
    else:
        print("✓ a warm optimiser step peaks at {:.3f} MB of new allocations (largest weight matrix {:.2f} MB)".format(peak / 1048576.0, largest / 1048576.0))
    return failed


def test_forward_and_backward_match_the_reference_bitwise(my_predbat=None):
    """Training and inference forward passes, and the backward pass, give the reference's bits exactly, dropout included"""
    print("\n=== Testing _forward() and _backward() against the reference, bit for bit ===")
    failed = 0
    predictor = _predictor()
    rng = np.random.default_rng(5)
    X = rng.standard_normal((128, TOTAL_FEATURES)).astype(np.float32)
    y = rng.standard_normal((128, 1)).astype(np.float32)
    for training in (True, False):
        np.random.seed(1234)
        ref = reference_forward(predictor, X, training=training)
        np.random.seed(1234)
        live = predictor._forward(X, training=training)
        if not (_same_bytes(live[0], ref[0]) and _all_same(live[1], ref[1]) and _all_same(live[2], ref[2]) and _all_same(live[3], ref[3])):
            print("ERROR: _forward(training={}) differs from the reference".format(training))
            failed += 1
            continue
        ref_grads = reference_backward(predictor, y, ref[1], ref[2], dropout_masks=ref[3])
        live_grads = predictor._backward(y, live[1], live[2], dropout_masks=live[3])
        if not (_all_same(live_grads[0], ref_grads[0]) and _all_same(live_grads[1], ref_grads[1])):
            print("ERROR: _backward after training={} differs from the reference".format(training))
            failed += 1
    weights = np.abs(rng.standard_normal(128)).astype(np.float32)
    np.random.seed(99)
    ref = reference_forward(predictor, X, training=True)
    ref_grads = reference_backward(predictor, y, ref[1], ref[2], sample_weights=weights, dropout_masks=ref[3])
    np.random.seed(99)
    live = predictor._forward(X, training=True)
    live_grads = predictor._backward(y, live[1], live[2], sample_weights=weights, dropout_masks=live[3])
    if not (_all_same(live_grads[0], ref_grads[0]) and _all_same(live_grads[1], ref_grads[1])):
        print("ERROR: _backward with sample weights differs from the reference")
        failed += 1
    if not failed:
        print("✓ forward (training and inference) and backward match the reference bit for bit")
    return failed


def test_forward_matches_the_reference_when_the_bias_would_promote(my_predbat=None):
    """float32 weights with float64 biases: the expression form promotes the layer's output, and so must the live pass"""
    print("\n=== Testing _forward() against the reference with a promoting bias ===")
    failed = 0
    predictor = _predictor()
    predictor.weights = [w.astype(np.float32) for w in predictor.weights]
    predictor.biases = [(b + 0.25).astype(np.float64) for b in predictor.biases]
    X = np.random.default_rng(8).standard_normal((16, TOTAL_FEATURES)).astype(np.float32)
    ref = reference_forward(predictor, X)
    live = predictor._forward(X)
    if not (_same_bytes(live[0], ref[0]) and _all_same(live[2], ref[2])):
        print("ERROR: _forward with float32 weights and float64 biases differs from the reference")
        failed += 1
    else:
        print("✓ a promoting bias gives the reference's dtype and bits")
    return failed


def test_optimiser_buffers_follow_reset_and_load(my_predbat=None):
    """After a reset or a fresh initialisation the step still matches the reference: no stale buffer carries state across"""
    print("\n=== Testing _adam_update() after reset and re-initialisation ===")
    failed = 0
    live = _predictor()
    weight_grads, bias_grads = _grads(live, seed=21)
    live._adam_update([g.copy() for g in weight_grads], [g.copy() for g in bias_grads])
    live._reset_adam_optimizer()
    reference = copy.deepcopy(live)
    reference_adam_update(reference, [g.copy() for g in weight_grads], [g.copy() for g in bias_grads])
    live._adam_update([g.copy() for g in weight_grads], [g.copy() for g in bias_grads])
    if not _all_same(_state(live), _state(reference)):
        print("ERROR: the step after a reset differs from the reference")
        failed += 1
    live._initialize_weights()
    reference = copy.deepcopy(live)
    reference_adam_update(reference, [g.copy() for g in weight_grads], [g.copy() for g in bias_grads])
    live._adam_update([g.copy() for g in weight_grads], [g.copy() for g in bias_grads])
    if not _all_same(_state(live), _state(reference)):
        print("ERROR: the step after re-initialisation differs from the reference")
        failed += 1
    if not failed:
        print("✓ the step matches the reference after a reset and after re-initialisation")
    return failed


def test_early_stopping_restores_the_best_epoch(my_predbat=None):
    """When training stops early, the weights and biases it ends with are exactly the best epoch's, not the last epoch's"""
    print("\n=== Testing early stopping restores the best epoch's weights ===")
    import re
    from datetime import datetime, timezone

    from tests.test_load_ml import _create_synthetic_load_data

    failed = 0
    now_utc = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    np.random.seed(42)
    load_data = _create_synthetic_load_data(n_days=5, now_utc=now_utc)
    logs, snapshots = [], []
    predictor = LoadPredictor(log_func=logs.append, learning_rate=0.01, weight_decay=0.01, dropout_rate=0.1)
    # The heartbeat runs after each epoch's validation and before its checkpoint, so each snapshot is that epoch's weights
    record = lambda: snapshots.append([array.copy() for array in predictor.weights + predictor.biases])
    predictor.train(load_data, now_utc, is_initial=True, epochs=40, time_decay_days=7, patience=2, progress_callback=record)
    ema = [float(match) for message in logs for match in re.findall(r"ema_combined=([0-9.]+)", message)]
    if load_predictor._ADAM_POOL is not None:
        print("ERROR: train() left the optimiser's thread pool running")
        failed += 1
    if not any("Early stopping" in message for message in logs):
        print("ERROR: training did not stop early, so this case is not exercised")
        return 1
    final = predictor.weights + predictor.biases
    matches = [epoch for epoch, snapshot in enumerate(snapshots) if _all_same(snapshot, final)]
    if len(matches) != 1:
        print("ERROR: the final weights match {} epochs' snapshots, expected exactly one".format(len(matches)))
        failed += 1
    elif matches[0] == len(snapshots) - 1:
        print("ERROR: the final weights are the last epoch's, not the best epoch's")
        failed += 1
    elif ema[matches[0]] != min(ema):
        print("ERROR: restored epoch {} has ema_combined {}, the best was {}".format(matches[0] + 1, ema[matches[0]], min(ema)))
        failed += 1
    if not failed:
        print("✓ stopped after epoch {} and restored epoch {}, the best, bit for bit".format(len(snapshots), matches[0] + 1))
    return failed


def test_threaded_adam_update_matches_the_reference(my_predbat=None):
    """Split over 1, 2 or 3 threads, several steps (one with NaN and +/-inf gradients in different slices) give the reference's bits"""
    print("\n=== Testing the threaded optimiser step against the reference ===")
    failed = 0
    saved = load_predictor.ADAM_THREADS
    try:
        for threads in (1, 2, 3):
            load_predictor.ADAM_THREADS = threads
            load_predictor.release_adam_pool()
            live = LoadPredictor(log_func=lambda message: None, learning_rate=0.001, weight_decay=0.01, dropout_rate=0.1)
            live._initialize_weights()
            reference = copy.deepcopy(live)
            for step in range(4):
                weight_grads, bias_grads = _training_dtype_grads(live, seed=500 + step)
                if step == 2:
                    size = weight_grads[0].size
                    weight_grads[0].flat[[5, size // 2, size - 3]] = [np.nan, np.inf, -np.inf]
                    weight_grads[1].flat[[7, weight_grads[1].size - 1]] = [np.inf, np.nan]
                lr = np.float64(0.001) * np.float64(1.0 - step / 50.0)
                reference_adam_update(reference, [g.copy() for g in weight_grads], [g.copy() for g in bias_grads], lr=lr)
                live._adam_update([g.copy() for g in weight_grads], [g.copy() for g in bias_grads], lr=lr)
                if not _all_same(_state(live), _state(reference)):
                    print("ERROR: {} threads: optimiser state differs from the reference after step {}".format(threads, step + 1))
                    failed += 1
                    break
            if (load_predictor._ADAM_POOL is not None) != (threads > 1):
                print("ERROR: {} threads: thread pool {}".format(threads, "made" if load_predictor._ADAM_POOL is not None else "never made"))
                failed += 1
            load_predictor.release_adam_pool()
            if load_predictor._ADAM_POOL is not None:
                print("ERROR: release_adam_pool() left the pool in place")
                failed += 1
    finally:
        load_predictor.ADAM_THREADS = saved
        load_predictor.release_adam_pool()
    if not failed:
        print("✓ 1, 2 and 3 threads match the reference bit for bit, non-finite values included, and the pool is released")
    return failed


def _reference_batch(windows, indices):
    """Batch rows as __getitem__ built them before the finiteness gate: nan_to_num on every batch."""
    batch = windows._assemble(np.atleast_1d(windows.targets[indices]), np.empty((len(np.atleast_1d(windows.targets[indices])), TOTAL_FEATURES), dtype=np.float32))
    if windows.mean is not None and windows.std is not None:
        batch -= windows.mean
        batch /= windows.std
        np.nan_to_num(batch, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
        load_predictor.bound_normalised(batch)
    else:
        np.nan_to_num(batch, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    return batch


def test_batch_rows_match_the_reference(my_predbat=None):
    """Training batches are bit-identical to always running nan_to_num, with and without non-finite inputs, and a finite batch skips it"""
    print("\n=== Testing training batch rows against the reference ===")
    failed = 0
    steps = LOOKBACK_STEPS + 40
    rng = np.random.default_rng(8)
    indices = np.arange(30)
    for label, poison in (("finite", False), ("non-finite", True)):
        channels = [rng.standard_normal(steps).astype(np.float32) for _ in range(5)]
        if poison:
            channels[0][10], channels[2][20], channels[4][LOOKBACK_STEPS + 5] = np.nan, np.inf, -np.inf
        windows = WindowedFeatures(channels, rng.standard_normal((steps, NUM_TIME_FEATURES)).astype(np.float32), np.arange(steps - LOOKBACK_STEPS, dtype=np.int64))
        for normalised in (False, True):
            windows.mean = (rng.standard_normal(TOTAL_FEATURES) * 0.1).astype(np.float32) if normalised else None
            windows.std = (np.abs(rng.standard_normal(TOTAL_FEATURES)) + 0.5).astype(np.float32) if normalised else None
            calls = []
            real = np.nan_to_num
            np.nan_to_num = lambda *args, **kwargs: (calls.append(1), real(*args, **kwargs))[1]
            try:
                live = windows[indices].copy()
            finally:
                np.nan_to_num = real
            expected = _reference_batch(windows, indices)
            if not _same_bytes(live, expected):
                print("ERROR: {} {} batch differs from the reference".format(label, "normalised" if normalised else "raw"))
                failed += 1
            np.nan_to_num = lambda *args, **kwargs: (calls.append(1), real(*args, **kwargs))[1]
            try:
                matrix = np.asarray(windows)
            finally:
                np.nan_to_num = real
            if not _same_bytes(matrix, _reference_batch(windows, np.arange(len(windows.targets)))):
                print("ERROR: {} {} materialised matrix differs from the reference".format(label, "normalised" if normalised else "raw"))
                failed += 1
            if not poison and calls:
                print("ERROR: a finite {} batch still ran nan_to_num".format("normalised" if normalised else "raw"))
                failed += 1
    if not failed:
        print("✓ batch rows and the materialised matrix match the reference with and without non-finite inputs, and finite ones skip nan_to_num")
    return failed


def test_adam_threads_follow_the_usable_cpus(my_predbat=None):
    """The optimiser step uses at most 3 threads, and no more than the CPUs this process may use (a container's quota, not the host's cores)"""
    print("\n=== Testing the optimiser's thread count follows the usable CPUs ===")
    failed = 0
    real = load_predictor.available_cpu_count
    try:
        for usable, expected in ((1, 1), (2, 2), (3, 3), (12, 3)):
            load_predictor.available_cpu_count = lambda usable=usable: usable
            got = load_predictor.adam_thread_count()
            if got != expected:
                print("ERROR: with {} usable CPUs the optimiser would use {} threads, expected {}".format(usable, got, expected))
                failed += 1
    finally:
        load_predictor.available_cpu_count = real
    if not failed:
        print("✓ 1, 2, 3 and 12 usable CPUs give 1, 2, 3 and 3 optimiser threads")
    return failed


def test_training_that_raises_releases_the_pool(my_predbat=None):
    """A training that raises after its first optimiser step still shuts the worker pool down and drops the step's buffers"""
    print("\n=== Testing a training that raises releases the optimiser's pool and buffers ===")
    from datetime import datetime, timezone

    from tests.test_load_ml import _create_synthetic_load_data

    failed = 0
    now_utc = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    np.random.seed(42)
    load_data = _create_synthetic_load_data(n_days=5, now_utc=now_utc)
    predictor = LoadPredictor(log_func=lambda message: None, learning_rate=0.01, weight_decay=0.01, dropout_rate=0.1)
    real_backward = predictor._backward
    calls = []

    def failing_backward(*args, **kwargs):
        """Fail the second backward pass, after the first optimiser step has made the pool."""
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("injected failure")
        return real_backward(*args, **kwargs)

    predictor._backward = failing_backward
    saved = load_predictor.ADAM_THREADS
    try:
        load_predictor.ADAM_THREADS = 3
        try:
            predictor.train(load_data, now_utc, is_initial=True, epochs=3, time_decay_days=7)
            print("ERROR: the injected failure did not propagate")
            failed += 1
        except RuntimeError:
            pass
        if load_predictor._ADAM_POOL is not None:
            print("ERROR: the optimiser's thread pool outlived a training that raised")
            failed += 1
        if predictor.adam_buffers:
            print("ERROR: {} optimiser buffers outlived a training that raised".format(len(predictor.adam_buffers)))
            failed += 1
    finally:
        load_predictor.ADAM_THREADS = saved
        load_predictor.release_adam_pool()
    if not failed:
        print("✓ a training that raises after its first step leaves no pool and no buffers")
    return failed


def run_ml_training_bitwise_tests(my_predbat=None):
    """Run the ML training bit-identity tests; return the number of failures."""
    failed = 0
    failed += test_adam_update_matches_the_reference_bitwise(my_predbat)
    failed += test_adam_update_matches_the_reference_on_non_finite_values(my_predbat)
    failed += test_adam_update_allocates_no_weight_sized_temporaries(my_predbat)
    failed += test_adam_update_matches_the_reference_with_training_dtypes(my_predbat)
    failed += test_adam_update_with_training_dtypes_allocates_no_weight_sized_temporaries(my_predbat)
    failed += test_forward_and_backward_match_the_reference_bitwise(my_predbat)
    failed += test_forward_matches_the_reference_when_the_bias_would_promote(my_predbat)
    failed += test_optimiser_buffers_follow_reset_and_load(my_predbat)
    failed += test_early_stopping_restores_the_best_epoch(my_predbat)
    failed += test_threaded_adam_update_matches_the_reference(my_predbat)
    failed += test_batch_rows_match_the_reference(my_predbat)
    failed += test_adam_threads_follow_the_usable_cpus(my_predbat)
    failed += test_training_that_raises_releases_the_pool(my_predbat)
    return failed
