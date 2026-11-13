"""Rover-scale terrain descriptors (vehicle-independent).

The descriptors follow the hazard evaluation used by JPL rover autonomy (GESTALT,
Goldberg, Maimone & Matthies 2002): a least-squares **plane is fitted to a
rover-sized patch** centred on every cell, and

* **tilt** = inclination of the fitted plane (the attitude the chassis would take);
* **roughness** = maximum absolute residual of the patch elevations from that plane
  (the largest bump/step under the vehicle once the overall slope is removed).

Both are computed on a regular metric grid (the region's Transverse Mercator grid,
scale error < 0.03 %, see processing.derive_terrain). Definitions, assumptions and
validation: docs/terrain-analysis.md.

Missing data semantics: if *any* elevation in the patch is missing, the descriptor is
NaN ("unknown"), never computed from a partial patch. A partially observed patch could
hide exactly the obstacle the descriptor is meant to find.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import ndimage


def window_cells(window_m: float, res: float) -> int:
    """Patch size in cells for a patch of ``window_m`` metres. Must be odd and >= 3."""
    n = window_m / res
    if abs(n - round(n)) > 1e-6:
        raise ValueError(f"window {window_m} m is not a whole number of {res} m cells")
    n = round(n)
    if n < 3 or n % 2 == 0:
        raise ValueError(f"window must be an odd number >= 3 of cells, got {n}")
    return n


def _offsets(n: int, res: float) -> tuple[np.ndarray, np.ndarray, float]:
    """East offsets per column, north offsets per row, and sum of squared offsets.

    For a complete, symmetric n x n patch the design matrix [1, u, v] is orthogonal, so
    the least-squares coefficients decouple: a = mean(z), b_x = sum(u z)/S, b_y = sum(v z)/S
    with S = n * sum_j u_j^2 (identical for u and v).
    """
    k = n // 2
    u = (np.arange(n) - k) * res  # +east along columns
    v = -(np.arange(n) - k) * res  # +north: rows increase southward
    s = n * float(np.sum(u**2))
    return u, v, s


def complete_windows(z: np.ndarray, n: int) -> np.ndarray:
    """True where the n x n patch centred on the cell lies inside ``z`` and has no NaN."""
    finite = np.isfinite(z).astype(np.uint8)
    return ndimage.minimum_filter(finite, size=n, mode="constant", cval=0).astype(bool)


def plane_fit(z: np.ndarray, res: float, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Plane fitted to the n x n patch around every cell.

    Returns ``(tilt_deg, max_abs_residual_m)`` as float64 arrays, NaN where the patch is
    incomplete. Cost: 3 correlations + n^2 vectorised passes (exact sums, no running-sum
    accumulation, so no catastrophic cancellation on large absolute elevations).
    """
    z = np.asarray(z, dtype=np.float64)
    u, v, s = _offsets(n, res)
    ok = complete_windows(z, n)
    z0 = np.where(np.isfinite(z), z, 0.0)
    kx = np.tile(u / s, (n, 1))  # kx[i, j] = u_j / S
    ky = np.tile((v / s)[:, None], (1, n))  # ky[i, j] = v_i / S
    km = np.full((n, n), 1.0 / n**2)
    bx = ndimage.correlate(z0, kx, mode="constant", cval=0.0)
    by = ndimage.correlate(z0, ky, mode="constant", cval=0.0)
    a = ndimage.correlate(z0, km, mode="constant", cval=0.0)

    k = n // 2
    zp = np.pad(z0, k, mode="constant", constant_values=0.0)
    h, w = z.shape
    max_res = np.zeros_like(z0)
    for i in range(n):
        for j in range(n):
            patch = zp[i : i + h, j : j + w]  # z[r + i - k, c + j - k]
            r = np.abs(patch - (a + bx * u[j] + by * v[i]))
            np.maximum(max_res, r, out=max_res)

    tilt = np.degrees(np.arctan(np.hypot(bx, by)))
    tilt[~ok] = np.nan
    max_res[~ok] = np.nan
    return tilt, max_res


def tilt_sigma_deg(sigma_z: float, n: int, res: float) -> float:
    """1-sigma uncertainty of the fitted tilt from independent height errors ``sigma_z``.

    Var(b_x) = Var(b_y) = sigma_z^2 / S (least squares, orthogonal design). Reported in
    degrees using d(atan s)/ds <= 1 (exact at zero slope, conservative elsewhere).
    Assumes uncorrelated errors; stereo DTM errors are spatially correlated, so this is
    an approximation (docs/terrain-analysis.md section 'Uncertainty').
    """
    _, _, s = _offsets(n, res)
    return math.degrees(sigma_z / math.sqrt(s))


def residual_noise_floor(sigma_z: float, n: int) -> float:
    """Expected max |residual| of a perfectly planar patch observed with N(0, sigma_z) noise.

    Monte-Carlo-free approximation: E[max of m half-normal] ~ sigma * sqrt(2 ln m) for the
    m = n^2 residuals. Used only for documentation/diagnostics, not for classification.
    """
    m = n * n
    return sigma_z * math.sqrt(2.0 * math.log(m))
