# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
"""
ML Lean Inference Tests

Between trainings LoadML only predicts, and prediction needs neither the float64 weights nor the
Adam moments (every fine-tune resets the moments before its first step). Once the full state is
safely in the model file, release_training_state() keeps float16 copies of the weights and biases
for prediction and frees the rest. The next train() or save() reloads the full state from that
file, so training is bit-identical to never having released it.
"""

import copy
import os
import tempfile
from datetime import datetime, timezone

import numpy as np

from load_predictor import INFERENCE_DTYPE, LoadPredictor, TOTAL_FEATURES
from tests.test_load_ml import _create_synthetic_load_data


def _same_bytes(a, b):
    """Whether two arrays hold exactly the same bits in the same shape and dtype."""
    a, b = np.asarray(a), np.asarray(b)
    return a.dtype == b.dtype and a.shape == b.shape and np.ascontiguousarray(a).tobytes() == np.ascontiguousarray(b).tobytes()


def _training_state(predictor):
    """Every array of network and optimiser state, in a fixed order."""
    return predictor.weights + predictor.biases + predictor.m_weights + predictor.v_weights + predictor.m_biases + predictor.v_biases


def _all_same(first, second):
    """Whether two lists of arrays are bit-identical element by element."""
    return len(first) == len(second) and all(_same_bytes(a, b) for a, b in zip(first, second))


def _trained(now_utc, logs=None):
    """A predictor briefly trained from scratch on synthetic data, and that data."""
    np.random.seed(42)
    load_data = _create_synthetic_load_data(n_days=5, now_utc=now_utc)
    predictor = LoadPredictor(log_func=(logs.append if logs is not None else lambda message: None), learning_rate=0.005)
    predictor.train(load_data, now_utc, is_initial=True, epochs=3, time_decay_days=7)
    return predictor, load_data


def test_release_keeps_inference_weights_and_frees_training_state(my_predbat=None):
    """After a save, release keeps float16 weights and biases, drops the moments, and predicts within float16 rounding"""
    print("\n=== Testing release_training_state() after a save ===")
    failed = 0
    now_utc = datetime.now(timezone.utc)
    predictor, _ = _trained(now_utc)
    probe = np.random.default_rng(1).standard_normal((32, TOTAL_FEATURES)).astype(np.float32)
    before, _, _, _ = predictor._forward(probe)
    with tempfile.TemporaryDirectory() as folder:
        if not predictor.save(os.path.join(folder, "model.npz")):
            print("ERROR: save failed")
            return 1
        if not predictor.release_training_state():
            print("ERROR: release_training_state() refused after a successful save")
            return 1
    if not predictor.training_state_released:
        print("ERROR: training_state_released not set")
        failed += 1
    if any(array.dtype != INFERENCE_DTYPE for array in predictor.weights + predictor.biases):
        print("ERROR: released weights are {}, expected {}".format(sorted({str(a.dtype) for a in predictor.weights + predictor.biases}), np.dtype(INFERENCE_DTYPE)))
        failed += 1
    if any(group is not None for group in (predictor.m_weights, predictor.v_weights, predictor.m_biases, predictor.v_biases)) or predictor.adam_buffers:
        print("ERROR: the moments or optimiser buffers were kept")
        failed += 1
    after, _, _, _ = predictor._forward(probe)
    if after.dtype != np.float32 or not np.allclose(after, before, rtol=1e-2, atol=1e-3):
        print("ERROR: released prediction differs beyond float16 rounding (max diff {:.5f})".format(float(np.max(np.abs(after - before)))))
        failed += 1
    if not failed:
        print("✓ released state keeps float16 weights, frees the moments and predicts within float16 rounding")
    return failed


def test_release_needs_a_model_file(my_predbat=None):
    """Without a successful save or load there is no file to restore from, so release keeps everything"""
    print("\n=== Testing release_training_state() refuses without a model file ===")
    failed = 0
    predictor, _ = _trained(datetime.now(timezone.utc))
    state = copy.deepcopy(_training_state(predictor))
    if predictor.release_training_state():
        print("ERROR: release_training_state() released state that was never saved")
        failed += 1
    elif predictor.training_state_released or not _all_same(_training_state(predictor), state):
        print("ERROR: a refused release still changed the state")
        failed += 1
    else:
        print("✓ release refused, and the state untouched, without a model file")
    return failed


def test_restore_is_bit_identical(my_predbat=None):
    """Restoring after a release brings back the saved weights, biases, moments and step count exactly"""
    print("\n=== Testing the training state restores bit for bit ===")
    failed = 0
    predictor, _ = _trained(datetime.now(timezone.utc))
    with tempfile.TemporaryDirectory() as folder:
        predictor.save(os.path.join(folder, "model.npz"))
        state, adam_t = copy.deepcopy(_training_state(predictor)), predictor.adam_t
        predictor.release_training_state()
        predictor._restore_training_state()
    if predictor.training_state_released:
        print("ERROR: still marked released after restoring")
        failed += 1
    if not _all_same(_training_state(predictor), state) or predictor.adam_t != adam_t:
        print("ERROR: restored state differs from the state that was saved")
        failed += 1
    if not failed:
        print("✓ weights, biases, moments and adam_t restore bit for bit, dtypes included")
    return failed


def test_fine_tune_after_release_matches_bitwise(my_predbat=None):
    """A fine-tune that starts from released state gives exactly the model a never-released one gives"""
    print("\n=== Testing a fine-tune after release is bit-identical ===")
    failed = 0
    now_utc = datetime.now(timezone.utc)
    released, load_data = _trained(now_utc)
    with tempfile.TemporaryDirectory() as folder:
        released.save(os.path.join(folder, "model.npz"))
        kept = copy.deepcopy(released)
        released.release_training_state()
        results = []
        for predictor in (kept, released):
            np.random.seed(7)
            results.append(predictor.train(load_data, now_utc, is_initial=False, epochs=2, time_decay_days=7))
    if results[0] != results[1]:
        print("ERROR: val_mae {} after release, {} without".format(results[1], results[0]))
        failed += 1
    if not _all_same(_training_state(released), _training_state(kept)) or released.adam_t != kept.adam_t:
        print("ERROR: fine-tuned state after release differs from the never-released one")
        failed += 1
    if not failed:
        print("✓ a fine-tune after release matches the never-released fine-tune bit for bit")
    return failed


def test_save_after_release_writes_full_precision(my_predbat=None):
    """Saving released state writes the full float64 state, not the float16 copies"""
    print("\n=== Testing save() after release writes full precision ===")
    failed = 0
    predictor, _ = _trained(datetime.now(timezone.utc))
    with tempfile.TemporaryDirectory() as folder:
        predictor.save(os.path.join(folder, "model.npz"))
        state = copy.deepcopy(_training_state(predictor))
        predictor.release_training_state()
        second = os.path.join(folder, "second.npz")
        if not predictor.save(second):
            print("ERROR: save after release failed")
            return 1
        reloaded = LoadPredictor(log_func=lambda message: None)
        if not reloaded.load(second):
            print("ERROR: the model saved after release does not load")
            return 1
    if not _all_same(_training_state(reloaded), state):
        print("ERROR: the model saved after release is not the full-precision state")
        failed += 1
    else:
        print("✓ save after release writes the full-precision state")
    return failed


def test_restore_falls_back_when_the_file_changed(my_predbat=None):
    """If the model file has gone, training still runs from the float16 weights upcast, with fresh moments and a warning"""
    print("\n=== Testing restore falls back when the model file is gone ===")
    failed = 0
    now_utc = datetime.now(timezone.utc)
    logs = []
    predictor, load_data = _trained(now_utc, logs)
    dtypes = [array.dtype for array in predictor.weights + predictor.biases]
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "model.npz")
        predictor.save(path)
        predictor.release_training_state()
        os.unlink(path)
        predictor._restore_training_state()
    if predictor.training_state_released or [array.dtype for array in predictor.weights + predictor.biases] != dtypes:
        print("ERROR: fallback did not restore the training dtypes {}".format(sorted({str(d) for d in dtypes})))
        failed += 1
    if predictor.m_weights is None or any(np.any(m) for m in predictor.m_weights + predictor.v_weights + predictor.m_biases + predictor.v_biases) or predictor.adam_t != 0:
        print("ERROR: fallback did not start the moments from zero")
        failed += 1
    if not any("Warn:" in message and "float16" in message for message in logs):
        print("ERROR: no warning logged for the fallback")
        failed += 1
    np.random.seed(7)
    if predictor.train(load_data, now_utc, is_initial=False, epochs=1, time_decay_days=7) is None:
        print("ERROR: fine-tune after the fallback failed")
        failed += 1
    if not failed:
        print("✓ with the file gone, the float16 weights upcast, moments start at zero, a warning is logged and training runs")
    return failed


def test_release_refuses_once_the_state_moves_on_from_the_file(my_predbat=None):
    """After training or re-initialising without a save, the file no longer holds the state, so release refuses rather than roll it back"""
    print("\n=== Testing release refuses once the state differs from the model file ===")
    failed = 0
    now_utc = datetime.now(timezone.utc)
    for change in ("fine-tune", "re-initialise"):
        predictor, load_data = _trained(now_utc)
        with tempfile.TemporaryDirectory() as folder:
            predictor.save(os.path.join(folder, "model.npz"))
            if change == "fine-tune":
                np.random.seed(7)
                predictor.train(load_data, now_utc, is_initial=False, epochs=1, time_decay_days=7)
            else:
                predictor._initialize_weights()
            state = copy.deepcopy(_training_state(predictor))
            if predictor.release_training_state():
                print("ERROR: after a {} with no save, release went ahead and would restore the file's older model".format(change))
                failed += 1
            elif not _all_same(_training_state(predictor), state):
                print("ERROR: a refused release after a {} still changed the state".format(change))
                failed += 1
    if not failed:
        print("✓ release refuses after a fine-tune or a re-initialisation that was not saved")
    return failed


class _MockBase:
    """Minimal base for constructing a LoadMLComponent against a config folder."""

    def __init__(self, config_root):
        """Expose what ComponentBase and LoadMLComponent.initialize read."""
        self.prefix = "predbat"
        self.config_root = config_root
        self.now_utc = datetime.now(timezone.utc)
        self.midnight_utc = self.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = (self.now_utc - self.midnight_utc).seconds // 60
        self.local_tz = timezone.utc
        self.args = {}
        self.prediction_started = False

    def log(self, msg, **kwargs):
        """Swallow component logging."""
        return None

    def get_arg(self, key, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
        """Return the few settings the component asks for."""
        return {"load_today": ["sensor.load_today"], "load_power": None, "car_charging_energy": None, "load_scaling": 1.0, "car_charging_energy_scale": 1.0}.get(key, default)


def test_component_releases_after_loading_a_model(my_predbat=None):
    """A component that loads a saved model at start-up holds only the float16 prediction copy until it next trains"""
    print("\n=== Testing the component releases the training state after loading a model ===")
    from load_ml_component import LoadMLComponent

    failed = 0
    predictor, _ = _trained(datetime.now(timezone.utc))
    predictor.validation_mae = 0.1
    with tempfile.TemporaryDirectory() as folder:
        predictor.save(os.path.join(folder, "predbat_ml_model.npz"))
        component = LoadMLComponent(_MockBase(config_root=folder), load_ml_enable=True)
        if not component.predictor.model_initialized:
            print("ERROR: the component did not load the saved model")
            return 1
        if not component.predictor.training_state_released:
            print("ERROR: the loaded model kept its full training state")
            failed += 1
    if not failed:
        print("✓ after loading a model the component predicts from the released float16 copy")
    return failed


def test_component_releases_after_saving_a_training(my_predbat=None):
    """After a training that saves the model, the component releases the training state; without a model file it keeps it"""
    print("\n=== Testing the component releases the training state after saving a training ===")
    import asyncio

    from load_ml_component import LoadMLComponent

    failed = 0
    now_utc = datetime.now(timezone.utc)
    for with_file in (True, False):
        predictor, load_data = _trained(now_utc)
        predictor.train_curriculum = lambda *args, **kwargs: 0.05  # the training itself is covered elsewhere
        with tempfile.TemporaryDirectory() as folder:
            component = LoadMLComponent.__new__(LoadMLComponent)
            component.base = _MockBase(config_root=folder)
            component.log = component.base.log
            component.last_success_timestamp = None
            component.predictor = predictor
            component.data_lock = asyncio.Lock()
            component.load_data = load_data
            component.load_data_age_days = 5
            component.pv_data = component.temperature_data = component.import_rates_data = component.export_rates_data = None
            component.ml_epochs_initial = component.ml_epochs_update = 1
            component.ml_time_decay_days = 7
            component.ml_validation_holdout_hours = 24
            component.ml_patience_initial = component.ml_patience_update = 1
            component.ml_curriculum_max_passes = 1
            component.ml_curriculum_window_days = 7
            component.ml_curriculum_step_days = 1
            component.ml_validation_threshold = 2.0
            if not with_file:
                # A predictor whose state is in a file it could release to, so only the component's own guard keeps it
                predictor.save(os.path.join(folder, "held.npz"))
            component.model_filepath = os.path.join(folder, "predbat_ml_model.npz") if with_file else None
            component.model_valid = False
            component.model_status = "not_initialized"
            component.last_train_time = None
            component.initial_training_done = True
            asyncio.run(component._do_training(is_initial=False))
            if predictor.training_state_released != with_file:
                print("ERROR: with{} a model file the training state was {}released".format("" if with_file else "out", "" if predictor.training_state_released else "not "))
                failed += 1
    if not failed:
        print("✓ the training state is released after a saved training, and kept when there is no model file")
    return failed


def run_ml_lean_inference_tests(my_predbat=None):
    """Run the ML lean inference tests; return the number of failures."""
    failed = 0
    failed += test_release_keeps_inference_weights_and_frees_training_state(my_predbat)
    failed += test_release_needs_a_model_file(my_predbat)
    failed += test_restore_is_bit_identical(my_predbat)
    failed += test_fine_tune_after_release_matches_bitwise(my_predbat)
    failed += test_save_after_release_writes_full_precision(my_predbat)
    failed += test_restore_falls_back_when_the_file_changed(my_predbat)
    failed += test_release_refuses_once_the_state_moves_on_from_the_file(my_predbat)
    failed += test_component_releases_after_loading_a_model(my_predbat)
    failed += test_component_releases_after_saving_a_training(my_predbat)
    return failed
