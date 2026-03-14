"""
signal_engine.engine — Greeks, IV, PCR, regime classification, and chain processing.
"""

from .greeks import black_scholes_vectorised
from .iv_calculator import newton_raphson_iv, calculate_iv_rank
from .pcr import calculate_pcr
from .regime import RegimeClassifier, MarketRegime
from .chain_processor import ChainProcessor

__all__ = [
    "black_scholes_vectorised",
    "newton_raphson_iv",
    "calculate_iv_rank",
    "calculate_pcr",
    "RegimeClassifier",
    "MarketRegime",
    "ChainProcessor",
]
