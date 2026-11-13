"""Vehicle-specific traversability: terrain features -> classes, traverse time, planning cost.

Layering (docs/traversability.md):

1. raw observations          (DTM, as delivered)
2. terrain features          (terrain.py: tilt, roughness - vehicle-independent)
3. terrain classification    (this module: vehicle limits applied to features)
4. traverse time             (this module: speed model, physical units s/m)
5. planning cost             (this module: time x explicit risk weight, s/m)

The model structure follows JPL's Mars 2020 landing-site traversability analysis
(Ono et al. 2016): binary hazard thresholds on slope/rock hazards, a speed model that maps
terrain to expected driving speed, and cost = 1 / speed (time-optimal planning).
The thresholds come from config/vehicles/<id>.yaml where each value states whether it
is published, derived or an assumption.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

import numpy as np

from marsgis.config import VehicleConfig


class TClass(IntEnum):
    """Traversability classes (uint8). 0 is reserved for 'outside the raster' (nodata)."""

    NOMINAL = 1  # within all nominal limits: full speed
    SLOW = 2  # traversable with reduced speed (tilt above nominal or moderate roughness)
    MARGINAL = 3  # within k-sigma of a hard limit: classification uncertain, risk-weighted
    HAZARD = 4  # exceeds a hard limit (tilt or step): impassable
    UNKNOWN = 5  # no/insufficient elevation data in the vehicle footprint: impassable


CLASS_INFO = {
    TClass.NOMINAL: ("nominal", "#1a9850"),
    TClass.SLOW: ("slow", "#fee08b"),
    TClass.MARGINAL: ("marginal (uncertain)", "#f46d43"),
    TClass.HAZARD: ("hazard (impassable)", "#a50026"),
    TClass.UNKNOWN: ("unknown (no data)", "#5e4fa2"),
}


@dataclass(frozen=True)
class Uncertainty:
    """1-sigma uncertainties of the features, in feature units."""

    tilt_deg: float
    roughness_m: float


@dataclass
class Evaluation:
    classes: np.ndarray  # uint8 TClass
    time_s_per_m: np.ndarray  # float32, NaN where not traversable
    cost_s_per_m: np.ndarray  # float32, NaN where not traversable


def evaluate(tilt_deg: np.ndarray, roughness_m: np.ndarray, vehicle: VehicleConfig,
             sigma: Uncertainty) -> Evaluation:
    """Apply the vehicle model cell by cell.

    * UNKNOWN if a feature is missing (incomplete footprint) - never assumed safe.
    * HAZARD if tilt > max_tilt or roughness > max_step.
    * MARGINAL if not HAZARD but tilt > max_tilt - k*sigma_tilt or
      roughness > max_step - k*sigma_roughness (the measurement cannot exclude a hazard).
    * SLOW if tilt > nominal_tilt or roughness > slow_step.
    * NOMINAL otherwise.

    Speed (m/h): base = speed_nominal if roughness <= slow_step else speed_slow, times a
    tilt factor that is 1 up to nominal_tilt and decreases linearly to
    speed_factor_at_max_tilt at max_tilt. time = 3600 / speed (s/m).
    cost = time * marginal_cost_multiplier on MARGINAL cells, = time elsewhere.
    """
    t = np.asarray(tilt_deg, dtype=np.float64)
    r = np.asarray(roughness_m, dtype=np.float64)
    v = vehicle
    k = v.uncertainty_k.value
    unknown = ~(np.isfinite(t) & np.isfinite(r))
    tt = np.where(unknown, 0.0, t)
    rr = np.where(unknown, 0.0, r)

    hazard = ~unknown & ((tt > v.max_tilt.value) | (rr > v.max_step.value))
    marginal = (~unknown & ~hazard
                & ((tt > v.max_tilt.value - k * sigma.tilt_deg)
                   | (rr > v.max_step.value - k * sigma.roughness_m)))
    slow = (~unknown & ~hazard & ~marginal
            & ((tt > v.nominal_tilt.value) | (rr > v.slow_step.value)))

    classes = np.full(t.shape, TClass.NOMINAL, dtype=np.uint8)
    classes[slow] = TClass.SLOW
    classes[marginal] = TClass.MARGINAL
    classes[hazard] = TClass.HAZARD
    classes[unknown] = TClass.UNKNOWN

    base = np.where(rr > v.slow_step.value, v.speed_slow.value, v.speed_nominal.value)
    span = v.max_tilt.value - v.nominal_tilt.value
    frac = np.clip((tt - v.nominal_tilt.value) / span, 0.0, 1.0)
    factor = 1.0 - frac * (1.0 - v.speed_factor_at_max_tilt.value)
    speed = base * factor  # m/h
    time = 3600.0 / speed
    passable = ~(hazard | unknown)
    time = np.where(passable, time, np.nan)
    cost = np.where(marginal, time * v.marginal_cost_multiplier.value, time)
    return Evaluation(classes=classes, time_s_per_m=time.astype(np.float32),
                      cost_s_per_m=cost.astype(np.float32))


def class_legend() -> list[dict[str, object]]:
    return [{"value": int(c), "label": label, "color": color}
            for c, (label, color) in CLASS_INFO.items()]
