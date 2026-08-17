"""Sector -> ETF resolution, so a candidate's move can be compared against
its sector's move.

Two lookups, deliberately separate:

1. The provider's own industry label -> a GICS-11 sector. The label is
   whatever `profile()["sector"]` returned, which today is Finnhub's
   `finnhubIndustry` — a finer, non-GICS vocabulary ("Semiconductors",
   "Insurance", "Hotels, Restaurants & Leisure"). Several of its labels
   collapse into one GICS sector, which is exactly the point.
2. GICS-11 sector -> the SPDR Select Sector ETF that tracks it. These
   eleven are the liquid, universally-quoted proxies for "the sector", and
   their history comes from the same Yahoo OHLCV path as any other ticker.

An unrecognized label resolves to None rather than to a nearest guess.
A wrong ETF would hand the tribunal a comparison that looks measured and
is not; an absent one is read as unverified, which is what it is
(invariant 6 — missing data is never a silent pass).

The industry table below covers every label the system has actually
received (see tests/test_sector_etf.py, measured over the 19 live
recommendations) plus the rest of Finnhub's published vocabulary that maps
to a GICS sector unambiguously. Labels whose GICS home genuinely depends
on the company — "Internet" splits between Communication Services and
Consumer Discretionary — are left out on purpose.
"""
from __future__ import annotations

from typing import Optional

# GICS 11 sectors -> SPDR Select Sector ETF.
SECTOR_ETFS: dict[str, str] = {
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Information Technology": "XLK",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
}

# Provider industry label (normalized) -> GICS sector. Keys are lowercase
# with collapsed whitespace; use _normalize() to build one.
_INDUSTRY_TO_SECTOR: dict[str, str] = {
    # --- Information Technology -------------------------------------------
    "technology": "Information Technology",
    "information technology": "Information Technology",
    "semiconductors": "Information Technology",
    "software": "Information Technology",
    "it services": "Information Technology",
    "technology hardware": "Information Technology",
    "electronic equipment": "Information Technology",

    # --- Health Care -------------------------------------------------------
    "health care": "Health Care",
    "healthcare": "Health Care",
    "biotechnology": "Health Care",
    "pharmaceuticals": "Health Care",
    "life sciences tools & services": "Health Care",
    "health care providers & services": "Health Care",
    "health care equipment & supplies": "Health Care",

    # --- Financials --------------------------------------------------------
    "financial services": "Financials",
    "financials": "Financials",
    "banking": "Financials",
    "banks": "Financials",
    "insurance": "Financials",
    "capital markets": "Financials",
    "consumer finance": "Financials",
    "diversified financial services": "Financials",
    "thrifts & mortgage finance": "Financials",

    # --- Real Estate -------------------------------------------------------
    "real estate": "Real Estate",
    "equity real estate investment trusts": "Real Estate",
    "reits": "Real Estate",

    # --- Consumer Discretionary -------------------------------------------
    # "Retail" is Finnhub's catch-all for retailers; XLY is the better single
    # home for it even though a grocer would sit in Consumer Staples under
    # strict GICS. Accepted as the lesser error — the alternative is no
    # comparison material at all for every retailer.
    "retail": "Consumer Discretionary",
    "consumer discretionary": "Consumer Discretionary",
    "hotels, restaurants & leisure": "Consumer Discretionary",
    "restaurants": "Consumer Discretionary",
    "textiles, apparel & luxury goods": "Consumer Discretionary",
    "automobiles": "Consumer Discretionary",
    "auto components": "Consumer Discretionary",
    "household durables": "Consumer Discretionary",
    "leisure products": "Consumer Discretionary",
    "multiline retail": "Consumer Discretionary",
    "specialty retail": "Consumer Discretionary",
    "diversified consumer services": "Consumer Discretionary",

    # --- Consumer Staples --------------------------------------------------
    # "Consumer products" is the one observed label whose GICS home is
    # arguable; Finnhub applies it mostly to household/personal goods, so it
    # sits in Staples. Revisit if live cases say otherwise.
    "consumer products": "Consumer Staples",
    "consumer staples": "Consumer Staples",
    "food products": "Consumer Staples",
    "beverages": "Consumer Staples",
    "tobacco": "Consumer Staples",
    "household products": "Consumer Staples",
    "personal products": "Consumer Staples",
    "food and staples retailing": "Consumer Staples",

    # --- Industrials -------------------------------------------------------
    "machinery": "Industrials",
    "industrials": "Industrials",
    "aerospace & defense": "Industrials",
    "airlines": "Industrials",
    "building": "Industrials",
    "construction": "Industrials",
    "commercial services & supplies": "Industrials",
    "professional services": "Industrials",
    "industrial conglomerates": "Industrials",
    "electrical equipment": "Industrials",
    "trading companies & distributors": "Industrials",
    "transportation": "Industrials",
    "transportation infrastructure": "Industrials",
    "logistics & transportation": "Industrials",
    "road & rail": "Industrials",
    "marine": "Industrials",

    # --- Materials ---------------------------------------------------------
    "materials": "Materials",
    "chemicals": "Materials",
    "metals & mining": "Materials",
    "packaging": "Materials",
    "paper & forest": "Materials",
    "construction materials": "Materials",

    # --- Energy ------------------------------------------------------------
    "energy": "Energy",
    "oil & gas": "Energy",
    "oil, gas & consumable fuels": "Energy",
    "energy equipment & services": "Energy",

    # --- Utilities ---------------------------------------------------------
    "utilities": "Utilities",
    "electric utilities": "Utilities",
    "gas utilities": "Utilities",
    "water utilities": "Utilities",
    "multi-utilities": "Utilities",
    "independent power producers": "Utilities",

    # --- Communication Services -------------------------------------------
    "communication services": "Communication Services",
    "communications": "Communication Services",
    "telecommunication": "Communication Services",
    "diversified telecommunication services": "Communication Services",
    "wireless telecommunication services": "Communication Services",
    "media": "Communication Services",
    "entertainment": "Communication Services",
    "interactive media & services": "Communication Services",
}


def _normalize(label: str) -> str:
    return " ".join(label.split()).lower()


def sector_for_industry(label: str) -> Optional[str]:
    """The GICS-11 sector a provider's industry label belongs to, or None if
    the label is empty or not one we map."""
    if not label:
        return None
    return _INDUSTRY_TO_SECTOR.get(_normalize(label))


def etf_for_industry(label: str) -> Optional[tuple[str, str]]:
    """(gics_sector, etf_ticker) for a provider's industry label, or None."""
    sector = sector_for_industry(label)
    if sector is None:
        return None
    return sector, SECTOR_ETFS[sector]
