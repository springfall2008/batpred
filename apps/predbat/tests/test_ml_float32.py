# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
"""
ML Float32 Training Tests

LoadML's network trains in float32: its weights, biases and Adam moments are float32 from the
He initialisation onward, so the forward and backward passes, the gradients and the optimiser
state are all half the size they would be in float64. Before this, multiplying the float32
initial weights by NumPy's float64 He scale silently promoted them to float64, and training
ran in double precision. A model saved in float64 by an earlier version is cast to float32
when it is loaded, so its next fine-tune runs in float32 too.
"""

import os
import tempfile

import numpy as np

from load_predictor import LoadPredictor, TOTAL_FEATURES


def _predictor():
    """A freshly initialised predictor."""
    predictor = LoadPredictor(log_func=lambda message: None, learning_rate=0.001, weight_decay=0.01, dropout_rate=0.1)
    predictor._initialize_weights()
    return predictor


def _state(predictor):
    """Every array of network and optimiser state, in a fixed order."""
    return predictor.weights + predictor.biases + predictor.m_weights + predictor.v_weights + predictor.m_biases + predictor.v_biases


def _non_float32(predictor):
    """The dtypes of any state arrays that are not float32."""
    return sorted({str(array.dtype) for array in _state(predictor) if array.dtype != np.float32})


def test_initialised_state_is_float32(my_predbat=None):
    """He initialisation leaves every weight, bias and moment float32, with the He scale applied"""
    print("\n=== Testing the initialised network is float32 ===")
    failed = 0
    predictor = _predictor()
    wrong = _non_float32(predictor)
    if wrong:
        print("ERROR: initialised state holds {} arrays, expected float32 only".format(", ".join(wrong)))
        failed += 1
    first = predictor.weights[0]
    expected_std = np.sqrt(2.0 / TOTAL_FEATURES)
    if abs(float(first.std()) - expected_std) > 0.05 * expected_std:
        print("ERROR: first layer std {:.5f}, expected about the He scale {:.5f}".format(float(first.std()), expected_std))
        failed += 1
    if not failed:
        print("✓ weights, biases and moments initialise as float32 at the He scale")
    return failed


def test_training_steps_keep_float32(my_predbat=None):
    """Forward, backward and several optimiser steps at the cosine schedule's np.float64 rate keep everything float32"""
    print("\n=== Testing training steps stay in float32 ===")
    failed = 0
    predictor = _predictor()
    rng = np.random.default_rng(5)
    X = rng.standard_normal((64, TOTAL_FEATURES)).astype(np.float32)
    y = rng.standard_normal((64, 1)).astype(np.float32)
    for step in range(3):
        output, activations, pre_activations, dropout_masks = predictor._forward(X, training=True)
        if output.dtype != np.float32:
            print("ERROR: forward pass output is {}, expected float32".format(output.dtype))
            failed += 1
            break
        weight_grads, bias_grads = predictor._backward(y, activations, pre_activations, dropout_masks=dropout_masks)
        wrong = sorted({str(g.dtype) for g in weight_grads + bias_grads if g.dtype != np.float32})
        if wrong:
            print("ERROR: gradients hold {} arrays, expected float32 only".format(", ".join(wrong)))
            failed += 1
            break
        predictor._adam_update(weight_grads, bias_grads, lr=np.float64(0.001) * np.float64(1.0 - step / 10.0))
        wrong = _non_float32(predictor)
        if wrong:
            print("ERROR: after optimiser step {} the state holds {} arrays, expected float32 only".format(step + 1, ", ".join(wrong)))
            failed += 1
            break
    if not failed:
        print("✓ 3 training steps keep activations, gradients, weights and moments in float32")
    return failed


def test_load_casts_a_float64_model_to_float32(my_predbat=None):
    """A model saved with float64 weights and moments, as earlier versions saved them, loads as float32 with the same values rounded"""
    print("\n=== Testing a float64 saved model loads as float32 ===")
    failed = 0
    saved = _predictor()
    rng = np.random.default_rng(9)
    for group in (saved.weights, saved.biases, saved.m_weights, saved.v_weights, saved.m_biases, saved.v_biases):
        for i, array in enumerate(group):
            group[i] = array.astype(np.float64) + rng.standard_normal(array.shape) * 1e-3
    saved.adam_t = 42
    saved.feature_mean = np.zeros(TOTAL_FEATURES, dtype=np.float32)
    saved.feature_std = np.ones(TOTAL_FEATURES, dtype=np.float32)
    saved.target_mean, saved.target_std = 0.5, 0.25
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "model.npz")
        if not saved.save(path):
            print("ERROR: could not save the float64 model")
            return 1
        loaded = LoadPredictor(log_func=lambda message: None, learning_rate=0.001, weight_decay=0.01, dropout_rate=0.1)
        if not loaded.load(path):
            print("ERROR: the float64 model did not load")
            return 1
    wrong = _non_float32(loaded)
    if wrong:
        print("ERROR: the loaded model holds {} arrays, expected float32 only".format(", ".join(wrong)))
        failed += 1
    elif not all(np.array_equal(live, original.astype(np.float32)) for live, original in zip(_state(loaded), _state(saved))):
        print("ERROR: the loaded values are not the saved ones rounded to float32")
        failed += 1
    if loaded.adam_t != 42:
        print("ERROR: adam_t {} after load, expected 42".format(loaded.adam_t))
        failed += 1
    if not failed:
        print("✓ a float64 model loads as float32, values rounded, optimiser step count kept")
    return failed


def run_ml_float32_tests(my_predbat=None):
    """Run the ML float32 training tests; return the number of failures."""
    failed = 0
    failed += test_initialised_state_is_float32(my_predbat)
    failed += test_training_steps_keep_float32(my_predbat)
    failed += test_load_casts_a_float64_model_to_float32(my_predbat)
    return failed
