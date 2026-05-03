"""Black-Scholes lognormal probability helpers and implied-volatility inversion.

Used to convert prediction-market binary contract prices into the implied
volatility that an option-pricing model would assign to the underlying.

Conventions
-----------
- Risk-free rate r = 0 in v1 (small effect at 30d, simplifies the math).
  Forward F is used directly; for live data, pass spot as F.
- T is time-to-expiry in years (e.g. 30 days = 30/365).
- All probabilities are returned in [0, 1]; sigmas in [0.01, 5.0] are typical.
- Inversion uses scipy.optimize.brentq inside a generous bracket.
  If no root exists in the bracket, returns float('nan') and the caller
  decides whether to log/skip.

Two market shapes are supported:
- ABOVE: a binary "Will S_T > K?" → directly invertible (monotone in sigma).
- RANGE: a binary "Will lo < S_T < hi?" → invertible with one caveat
  (when F is INSIDE the range the prob is non-monotone in sigma; we always
  bracket on the right side of the peak, which is the regime real markets
  trade in for typical IVs).
"""

from __future__ import annotations

import math

from scipy.optimize import brentq
from scipy.stats import norm

_SIGMA_LO = 0.01   # 1% IV — practical lower bound for any liquid market
_SIGMA_HI = 5.00   # 500% IV — extreme upper bound; anything above is noise


def prob_above(sigma: float, F: float, K: float, T: float) -> float:
    """P(S_T > K) under lognormal with vol `sigma` (annualized) over `T` years."""
    if sigma <= 0 or T <= 0 or F <= 0 or K <= 0:
        return float("nan")
    d2 = (math.log(F / K) - 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    return float(norm.cdf(d2))


def prob_in_range(sigma: float, F: float, lo: float, hi: float, T: float) -> float:
    """P(lo < S_T < hi) under lognormal."""
    if hi <= lo:
        return float("nan")
    return prob_above(sigma, F, lo, T) - prob_above(sigma, F, hi, T)


def iv_from_above_prob(prob: float, F: float, K: float, T: float) -> float:
    """Solve for sigma such that prob_above(sigma, F, K, T) == prob.

    ITM / ATM (K <= F): prob is monotone decreasing in sigma — single root.

    OTM (K > F): prob is UNIMODAL in sigma. As sigma rises from 0, the
    distribution spreads enough to put mass above K (prob rises). Past a
    peak, the lognormal becomes so skewed that mass piles up near zero
    and P(S>K) declines back to 0. We return the SMALLER root, matching
    real option-market regimes (10–80% IV, well below the wing peak).
    """
    if not (0.0 < prob < 1.0) or F <= 0 or K <= 0 or T <= 0:
        return float("nan")
    f = lambda s: prob_above(s, F, K, T) - prob

    if K <= F:
        # Monotone decreasing.
        try:
            if f(_SIGMA_LO) * f(_SIGMA_HI) > 0:
                return float("nan")
            return float(brentq(f, _SIGMA_LO, _SIGMA_HI, xtol=1e-7, rtol=1e-7))
        except (ValueError, RuntimeError):
            return float("nan")

    # OTM: unimodal — same handling as wing range, return smaller root.
    sigmas = [10 ** (math.log10(_SIGMA_LO) + i * (math.log10(_SIGMA_HI) - math.log10(_SIGMA_LO)) / 49)
              for i in range(50)]
    probs = [prob_above(s, F, K, T) for s in sigmas]
    peak_i = max(range(len(probs)), key=lambda i: probs[i])
    if probs[peak_i] < prob:
        return float("nan")
    sigma_peak = sigmas[peak_i]
    try:
        if f(_SIGMA_LO) * f(sigma_peak) <= 0:
            return float(brentq(f, _SIGMA_LO, sigma_peak, xtol=1e-7, rtol=1e-7))
    except (ValueError, RuntimeError):
        pass
    try:
        if f(sigma_peak) * f(_SIGMA_HI) <= 0:
            return float(brentq(f, sigma_peak, _SIGMA_HI, xtol=1e-7, rtol=1e-7))
    except (ValueError, RuntimeError):
        pass
    return float("nan")


def iv_from_range_prob(prob: float, F: float, lo: float, hi: float, T: float) -> float:
    """Solve for sigma such that prob_in_range(sigma, F, lo, hi, T) == prob.

    Body case (F inside [lo, hi]): prob is monotone decreasing in sigma —
    one root.

    Wing case (F outside [lo, hi]): prob is unimodal in sigma. From sigma=0
    where prob=0, it rises to a peak (where the spread of mass first reaches
    the range) then falls again as mass spreads beyond. There are TWO sigmas
    for any prob below the peak — a "small-sigma" root and a "large-sigma"
    root. We return the SMALLER root; this is the regime real option markets
    trade in for typical IVs (10–80%). The large-sigma root corresponds to
    extreme IVs (>200%) that aren't physical for these underlyings.
    """
    if not (0.0 < prob < 1.0) or F <= 0 or lo <= 0 or hi <= lo or T <= 0:
        return float("nan")
    f = lambda s: prob_in_range(s, F, lo, hi, T) - prob

    in_body = lo < F < hi
    if in_body:
        # Monotone decreasing. Single root.
        try:
            if f(_SIGMA_LO) * f(_SIGMA_HI) > 0:
                return float("nan")
            return float(brentq(f, _SIGMA_LO, _SIGMA_HI, xtol=1e-7, rtol=1e-7))
        except (ValueError, RuntimeError):
            return float("nan")

    # Wing case: scan for peak, then bracket the SMALLER (left) root.
    sigmas = [10 ** (math.log10(_SIGMA_LO) + i * (math.log10(_SIGMA_HI) - math.log10(_SIGMA_LO)) / 49)
              for i in range(50)]
    probs = [prob_in_range(s, F, lo, hi, T) for s in sigmas]
    peak_i = max(range(len(probs)), key=lambda i: probs[i])
    if probs[peak_i] < prob:
        return float("nan")  # requested prob is unreachable (above the peak)
    sigma_peak = sigmas[peak_i]
    try:
        # Left branch: SIGMA_LO → peak. Prob rises from 0 to peak.
        if f(_SIGMA_LO) * f(sigma_peak) <= 0:
            return float(brentq(f, _SIGMA_LO, sigma_peak, xtol=1e-7, rtol=1e-7))
    except (ValueError, RuntimeError):
        pass
    # Fallback: right branch (only reachable if no left root and prob is past peak;
    # implies very large sigma — log a warning at the call site if you care).
    try:
        if f(sigma_peak) * f(_SIGMA_HI) <= 0:
            return float(brentq(f, sigma_peak, _SIGMA_HI, xtol=1e-7, rtol=1e-7))
    except (ValueError, RuntimeError):
        pass
    return float("nan")
