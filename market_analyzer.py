"""
market_analyzer.py - Analyse les marchés Polymarket et détecte les opportunités
"""

import requests
from datetime import datetime, timezone

GAMMA_API = "https://gamma-api.polymarket.com"
HEADERS   = {"Accept": "application/json", "User-Agent": "polymarket-bot/1.0"}


def get_markets(limit: int = 100, active: bool = True) -> list[dict]:
    """Récupère les marchés depuis l'API Gamma."""
    url = f"{GAMMA_API}/markets"
    params = {
        "limit": limit,
        "active": str(active).lower(),
        "closed": "false",
        "order": "volume24hr",
        "ascending": "false",
    }
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"[MarketAnalyzer] Erreur marchés: {e}")
        return []


def get_market_by_id(market_id: str) -> dict:
    """Récupère les détails d'un marché spécifique."""
    url = f"{GAMMA_API}/markets/{market_id}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"[MarketAnalyzer] Erreur marché {market_id}: {e}")
        return {}


def get_order_book(token_id: str) -> dict:
    """Récupère l'order book d'un token (YES ou NO)."""
    url = "https://clob.polymarket.com/book"
    params = {"token_id": token_id}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"[MarketAnalyzer] Erreur order book {token_id[:12]}...: {e}")
        return {}


def parse_price(market: dict) -> tuple[float, float]:
    """
    Extrait le prix YES et NO d'un marché.
    Retourne (yes_price, no_price) entre 0 et 1.
    """
    tokens = market.get("tokens", [])
    yes_price, no_price = 0.5, 0.5
    for token in tokens:
        outcome = token.get("outcome", "").upper()
        price   = float(token.get("price", 0.5))
        if outcome == "YES":
            yes_price = price
        elif outcome == "NO":
            no_price = price
    return yes_price, no_price


def score_market(market: dict) -> float:
    """
    Score de 0 à 10 basé sur :
    - Volume sur 24h (liquidité)
    - Spread (efficacité du marché)
    - Valeur extrême du prix (conviction du marché)
    """
    score = 0.0
    volume_24h = float(market.get("volume24hr", 0) or 0)
    liquidity  = float(market.get("liquidity", 0) or 0)
    yes_price, no_price = parse_price(market)

    # Liquidité (max 4 pts)
    if volume_24h > 50_000:
        score += 4.0
    elif volume_24h > 10_000:
        score += 2.5
    elif volume_24h > 1_000:
        score += 1.0

    # Liquidity pool (max 2 pts)
    if liquidity > 20_000:
        score += 2.0
    elif liquidity > 5_000:
        score += 1.0

    # Prix loin de 0.5 = marché tranché (max 4 pts)
    deviation = abs(yes_price - 0.5)
    score += deviation * 8  # 0→0, 0.5→4

    return round(min(score, 10.0), 2)


class MarketAnalyzer:
    def __init__(self, min_volume_24h: float = 5_000, min_score: float = 4.0):
        self.min_volume_24h = min_volume_24h
        self.min_score      = min_score

    def get_top_markets(self, limit: int = 50) -> list[dict]:
        """Filtre et classe les meilleurs marchés."""
        markets = get_markets(limit=limit)
        result  = []
        for m in markets:
            vol = float(m.get("volume24hr", 0) or 0)
            if vol < self.min_volume_24h:
                continue
            score = score_market(m)
            if score < self.min_score:
                continue
            yes_p, no_p = parse_price(m)
            result.append({
                "id":          m.get("id"),
                "conditionId": m.get("conditionId"),
                "question":    m.get("question", "")[:80],
                "yes_price":   yes_p,
                "no_price":    no_p,
                "volume_24h":  vol,
                "liquidity":   float(m.get("liquidity", 0) or 0),
                "score":       score,
                "end_date":    m.get("endDate"),
                "tokens":      m.get("tokens", []),
            })
        result.sort(key=lambda x: x["score"], reverse=True)
        return result

    def display_top(self, markets: list[dict], top_n: int = 10) -> None:
        """Affiche les meilleurs marchés dans la console."""
        print("\n" + "=" * 70)
        print(f"  TOP {top_n} MARCHÉS — {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
        print("=" * 70)
        for i, m in enumerate(markets[:top_n], 1):
            print(
                f"{i:2}. [{m['score']:4.1f}] "
                f"YES={m['yes_price']:.2f} "
                f"Vol=${m['volume_24h']:>10,.0f} "
                f"| {m['question']}"
            )

    def find_mispriced(self, markets: list[dict], threshold: float = 0.05) -> list[dict]:
        """
        Détecte les marchés potentiellement mal pricés en comparant
        YES + NO par rapport à 1.0 (somme théorique).
        """
        mispriced = []
        for m in markets:
            total = m["yes_price"] + m["no_price"]
            gap   = abs(total - 1.0)
            if gap > threshold:
                mispriced.append({**m, "price_gap": round(gap, 4)})
        return sorted(mispriced, key=lambda x: x["price_gap"], reverse=True)
