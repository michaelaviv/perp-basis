"""Round-trip tests for the BS lognormal IV inversion helpers.

The forward/backward identity is the only thing worth testing here:
given any (sigma, F, K, T) inside reasonable ranges, computing the
probability and then inverting it back must recover sigma to within
brentq's tolerance.
"""

from __future__ import annotations

import math

import pytest

from perp_basis import iv_math


SIGMAS = [0.10, 0.20, 0.30, 0.50, 0.80]
TENORS = [7 / 365, 30 / 365, 90 / 365]
F0 = 100.0
ITM_ATM_STRIKES = [80.0, 95.0, 100.0]   # K <= F → prob_above is monotone in sigma
OTM_STRIKES = [105.0, 120.0]            # K > F → prob_above is unimodal


# ITM/ATM: monotone, full round-trip works for all sigmas.
@pytest.mark.parametrize("sigma", SIGMAS)
@pytest.mark.parametrize("T", TENORS)
@pytest.mark.parametrize("K", ITM_ATM_STRIKES)
def test_above_itm_atm_round_trip(sigma: float, T: float, K: float) -> None:
    p = iv_math.prob_above(sigma, F0, K, T)
    if not (0.001 < p < 0.999):
        pytest.skip(f"saturated prob {p:.4f} at sigma={sigma}, K={K}")
    recovered = iv_math.iv_from_above_prob(p, F0, K, T)
    assert not math.isnan(recovered)
    assert recovered == pytest.approx(sigma, abs=1e-5)


# OTM: prob_above is unimodal, two roots possible. Inversion returns the
# SMALLER root, so only test sigmas that are on the left side of the peak.
# These ranges are chosen so the listed sigmas are clearly pre-peak.
OTM_LEFT_BRANCH_CASES = [
    (105.0, 0.05),
    (105.0, 0.10),
    (105.0, 0.20),
    (120.0, 0.10),
    (120.0, 0.20),
    (120.0, 0.30),
]


@pytest.mark.parametrize("K,sigma", OTM_LEFT_BRANCH_CASES)
@pytest.mark.parametrize("T", TENORS)
def test_above_otm_left_branch_round_trip(K: float, sigma: float, T: float) -> None:
    p = iv_math.prob_above(sigma, F0, K, T)
    if not (0.001 < p < 0.999):
        pytest.skip(f"saturated prob {p:.4f}")
    recovered = iv_math.iv_from_above_prob(p, F0, K, T)
    assert not math.isnan(recovered)
    assert recovered == pytest.approx(sigma, abs=1e-3)


# Body case (F inside [lo, hi]): prob is monotone decreasing in sigma — single
# unambiguous root, full round-trip works.
BODY_RANGES = [(90.0, 110.0), (85.0, 115.0), (95.0, 105.0), (80.0, 120.0)]


@pytest.mark.parametrize("sigma", SIGMAS)
@pytest.mark.parametrize("T", TENORS)
@pytest.mark.parametrize("lo,hi", BODY_RANGES)
def test_range_body_round_trip(sigma: float, T: float, lo: float, hi: float) -> None:
    F = 100.0
    p = iv_math.prob_in_range(sigma, F, lo, hi, T)
    if not (0.001 < p < 0.999):
        pytest.skip(f"saturated prob {p:.4f} at sigma={sigma}, range=[{lo},{hi}]")
    recovered = iv_math.iv_from_range_prob(p, F, lo, hi, T)
    assert not math.isnan(recovered)
    assert recovered == pytest.approx(sigma, abs=1e-5)


# Wing case (F outside [lo, hi]): prob is unimodal — TWO roots for any prob
# below the peak. We document and pin down the "small-sigma" root choice; only
# inputs whose sigma is on the LEFT side of the peak round-trip cleanly.
# Realistic prediction-market wing markets sit comfortably on the left branch
# (typical underlying IVs are 20–80%, well below the wing-peak sigma).
WING_CASES_LEFT_BRANCH = [
    # (lo, hi, sigma_in) — sigma_in chosen to be on the left side of the peak.
    (110.0, 120.0, 0.10),
    (110.0, 120.0, 0.20),
    (130.0, 150.0, 0.30),
    (130.0, 150.0, 0.50),
    (60.0, 70.0, 0.30),
    (60.0, 70.0, 0.50),
]


@pytest.mark.parametrize("lo,hi,sigma", WING_CASES_LEFT_BRANCH)
@pytest.mark.parametrize("T", TENORS)
def test_range_wing_left_branch_round_trip(lo: float, hi: float, sigma: float, T: float) -> None:
    F = 100.0
    p = iv_math.prob_in_range(sigma, F, lo, hi, T)
    if not (0.001 < p < 0.999):
        pytest.skip(f"saturated prob {p:.4f}")
    recovered = iv_math.iv_from_range_prob(p, F, lo, hi, T)
    assert not math.isnan(recovered)
    # Tolerate slight peak-scan discretization on the wing path.
    assert recovered == pytest.approx(sigma, abs=1e-3)


def test_range_wing_picks_smaller_root() -> None:
    """When a wing prob has two roots, return the SMALLER one."""
    F, T = 100.0, 30 / 365
    lo, hi = 110.0, 120.0
    # The peak for [110, 120] @ T=30d sits around sigma=0.50 (prob~0.14).
    # Pick a small sigma well left of the peak; recovery should match it,
    # not jump to the much larger right-branch root.
    sigma_small = 0.20
    p = iv_math.prob_in_range(sigma_small, F, lo, hi, T)
    recovered = iv_math.iv_from_range_prob(p, F, lo, hi, T)
    assert recovered == pytest.approx(sigma_small, abs=1e-3)


def test_above_otm_picks_smaller_root() -> None:
    """When an OTM-call prob has two roots, return the SMALLER one."""
    F, K, T = 100.0, 110.0, 30 / 365
    sigma_small = 0.20
    p = iv_math.prob_above(sigma_small, F, K, T)
    recovered = iv_math.iv_from_above_prob(p, F, K, T)
    assert recovered == pytest.approx(sigma_small, abs=1e-3)


def test_above_unreachable_returns_nan() -> None:
    # A strike at $1 with F=$100 has P(S>K) ≈ 1 for any sigma — invertible
    # only at p = 1. Asking for p = 0.5 is unreachable in the bracket.
    # Use a more extreme example: very deep OTM.
    F, K, T = 100.0, 1000.0, 30 / 365
    # P(S > 1000) at typical sigmas is essentially 0; asking for 0.99 is unreachable.
    nan = iv_math.iv_from_above_prob(0.99, F, K, T)
    assert math.isnan(nan)


def test_invalid_inputs_return_nan() -> None:
    assert math.isnan(iv_math.iv_from_above_prob(0.5, F=-1, K=100, T=0.1))
    assert math.isnan(iv_math.iv_from_above_prob(0.5, F=100, K=100, T=0))
    assert math.isnan(iv_math.iv_from_above_prob(0.0, F=100, K=100, T=0.1))
    assert math.isnan(iv_math.iv_from_above_prob(1.0, F=100, K=100, T=0.1))
    assert math.isnan(iv_math.iv_from_range_prob(0.5, F=100, lo=110, hi=100, T=0.1))


def test_prob_above_atm_at_zero_drift_is_below_half() -> None:
    # At F=K and r=0, P(S_T > K) under lognormal is N(-sigma*sqrt(T)/2),
    # which is slightly LESS than 0.5 (drift adjustment for lognormal mean).
    F, K, T, sigma = 100.0, 100.0, 30 / 365, 0.30
    p = iv_math.prob_above(sigma, F, K, T)
    assert 0.45 < p < 0.50
