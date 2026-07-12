"""Hawkeye — adversarial-verification investment decision system.

Package layout mirrors the intended service decomposition (see
docs/ARCHITECTURE.md). Sub-packages communicate only through
``hawkeye.contracts`` so each one can later be extracted into a
standalone microservice without touching the others.
"""

__version__ = "0.1.0"
