"""Vehicle model: class boundaries, speed model, uncertainty bands, unknown handling."""

import numpy as np
import pytest

from marsgis.config import load_vehicle
from marsgis.traversability import TClass, Uncertainty, evaluate


@pytest.fixture(scope="module")
def rover():
    return load_vehicle("perseverance")


ZERO = Uncertainty(tilt_deg=0.0, roughness_m=0.0)


def test_vehicle_parameters_are_documented(rover):
    for name in ("max_tilt", "nominal_tilt", "max_step", "slow_step", "speed_nominal",
                 "speed_slow", "uncertainty_k", "marginal_cost_multiplier"):
        p = getattr(rover, name)
        assert p.basis in {"published", "derived", "assumption"} and len(p.source) > 20
    assert rover.status == "experimental"


def test_classes_without_uncertainty(rover):
    t = np.array([0.0, 12.0, 19.9, 20.1, 5.0, 5.0, np.nan, 5.0])
    r = np.array([0.0, 0.0, 0.0, 0.0, 0.25, 0.36, 0.0, np.nan])
    ev = evaluate(t, r, rover, ZERO)
    assert ev.classes.tolist() == [TClass.NOMINAL, TClass.SLOW, TClass.SLOW, TClass.HAZARD,
                                   TClass.SLOW, TClass.HAZARD, TClass.UNKNOWN, TClass.UNKNOWN]


def test_unknown_and_hazard_are_never_traversable(rover):
    ev = evaluate(np.array([np.nan, 45.0]), np.array([0.0, 0.0]), rover, ZERO)
    assert np.isnan(ev.time_s_per_m).all() and np.isnan(ev.cost_s_per_m).all()


def test_speed_model(rover):
    v_nom, v_slow = rover.speed_nominal.value, rover.speed_slow.value
    t = np.array([0.0, rover.nominal_tilt.value, 15.0, rover.max_tilt.value, 0.0])
    r = np.array([0.0, 0.0, 0.0, 0.0, 0.25])
    ev = evaluate(t, r, rover, ZERO)
    f15 = 1 - (15 - 10) / (20 - 10) * (1 - rover.speed_factor_at_max_tilt.value)
    expected = 3600 / np.array([v_nom, v_nom, v_nom * f15,
                                v_nom * rover.speed_factor_at_max_tilt.value, v_slow])
    np.testing.assert_allclose(ev.time_s_per_m, expected, rtol=1e-6)
    np.testing.assert_allclose(ev.cost_s_per_m, ev.time_s_per_m)  # no marginal cells here


def test_uncertainty_band_marks_marginal_and_weights_cost(rover):
    sigma = Uncertainty(tilt_deg=2.0, roughness_m=0.25)
    k = rover.uncertainty_k.value
    t = np.array([rover.max_tilt.value - 0.5 * k * 2.0, 5.0, 5.0])
    r = np.array([0.0, rover.max_step.value - 0.5 * k * 0.25, 0.0])
    ev = evaluate(t, r, rover, sigma)
    assert ev.classes.tolist() == [TClass.MARGINAL, TClass.MARGINAL, TClass.NOMINAL]
    m = rover.marginal_cost_multiplier.value
    np.testing.assert_allclose(ev.cost_s_per_m[:2], ev.time_s_per_m[:2] * m, rtol=1e-6)


def test_monotonic_in_tilt(rover):
    t = np.linspace(0, 19.9, 200)
    ev = evaluate(t, np.zeros_like(t), rover, ZERO)
    assert np.all(np.diff(ev.time_s_per_m) >= -1e-6)
