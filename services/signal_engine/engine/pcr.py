"""
Put-Call Ratio calculation from chain data.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PCRResult:
    pcr_oi: float
    pcr_volume: float
    total_call_oi: int
    total_put_oi: int
    total_call_volume: int
    total_put_volume: int


def calculate_pcr(strikes: list[dict]) -> PCRResult:
    """Calculate Put-Call Ratio by OI and by volume from chain strike data.

    Parameters
    ----------
    strikes : list of dict
        Each dict must have keys: call_oi, put_oi, call_volume, put_volume.

    Returns
    -------
    PCRResult with pcr_oi and pcr_volume.
    """
    total_call_oi = 0
    total_put_oi = 0
    total_call_volume = 0
    total_put_volume = 0

    for s in strikes:
        total_call_oi += s.get("call_oi", 0)
        total_put_oi += s.get("put_oi", 0)
        total_call_volume += s.get("call_volume", 0)
        total_put_volume += s.get("put_volume", 0)

    pcr_oi = total_put_oi / total_call_oi if total_call_oi > 0 else 0.0
    pcr_volume = total_put_volume / total_call_volume if total_call_volume > 0 else 0.0

    return PCRResult(
        pcr_oi=round(pcr_oi, 4),
        pcr_volume=round(pcr_volume, 4),
        total_call_oi=total_call_oi,
        total_put_oi=total_put_oi,
        total_call_volume=total_call_volume,
        total_put_volume=total_put_volume,
    )
