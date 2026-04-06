"""
market_analyzer.py - Analyse les marchés Polymarket et détecte les opportunités
Robuste : retry automatique, jamais de crash.
"""

import time
import requests
from datetime import datetime, timezone

GAMMA_API = "https://gamma-api.polymarket.com"
HEADERS   = {"Accept": "application/json", "User-Agent": "polymarket-bot/1.0"}


def _safe_get(url: str, params: dict = None, timeout: int = 8,
              retries: int = 3) -> list | dict | None:
    """GET avec retry + backoff. Ne lève jamais d'exception."""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"  [MarketAnalyzer] Rate-limit 429 — attente {wait}s")
                time.sleep(wait)
                continue
            if r.status_code == 200:
                return r.json()
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
        except requests.exceptions.Timeout:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"  [MarketAnalyzer] Timeout après {retries} tentatives")
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"  [MarketAnalyzer] Erreur : {e}")
    return None


def get_markets(limit: int = 100, active: bool = True) -> list[dict]:
    """Récupère les marchés depuis l'API Gamma."""
    result = _safe_get(
        f"{GAMMA_API}/markets",
        params={
            "limit": limit,
            "active": str(active).lower(),
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
        }
    )
    return result if isinstance(result, list) else []


def get_market_by_id(market_id: str) -> dict:
    """Récupère les détails d'un marché spécifique."""
    result = _safe_get(f"{GAMMA_API}/markets/{market_id}")
    return result if isinstance(result, dict) else {}


def parse_price(market: dict) -> tuple[float, float]:
    """Extrait le prix YES et NO d'un marché."""
    try:
        tokens = market.get("tokens", [])
        yes_price, no_price = 0.5, 0.5
        for token in tokens:
            outcome = token.get("outcome", "").upper()
            price   = float(token.get("price", 0.5) or 0.5)
            if outcome == "YES":
                yes_price = price
            elif outcome == "NO":
                no_price = price
        return yes_price, no_price
    except Exception:
        return 0.5, 0.5


def score_market(market: dict) -> float:
    """Score de 0 à 10 basé sur liquidité, spread et conviction."""
    try:
        score = 0.0
        volume_24h = float(market.get("volume24hr", 0) or 0)
        liquidity  = float(market.get("liquidity",  0) or 0)
        yes_price, _ = parse_price(market)

        if volume_24h > 50_000:
            score += 4.0
        elif volume_24h > 10_000:
            score += 2.5
        elif volume_24h > 1_000:
            score += 1.0

        if liquidity > 20_000:
            score += 2.0
        elif liquidity > 5_000:
            score += 1.0

        deviation = abs(yes_price - 0.5)
        score += deviation * 8

        return round(min(score, 10.0), 2)
    except Exception:
        return 0.0


class MarketAnalyzer:
    def __init__(self, min_volume_24h: float = 5_000, min_score: float = 4.0):
        self.min_volume_24h = min_volume_24h
        self.min_score      = min_score

    def get_top_markets(self, limit: int = 50) -> list[dict]:
        """Filtre et classe les meilleurs marchés. Retourne [] en cas d'erreur."""
        try:
            markets = get_markets(limit=limit)
            if not markets:
                print("  [MarketAnalyzer] Aucun marché reçu (API indisponible ?)")
                return []
            result = []
            for m in markets:
                try:
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
                except Exception:
                    continue
            result.sort(key=lambda x: x["score"], reverse=True)
            return result
        except Exception as e:
            print(f"  [MarketAnalyzer] Erreur get_top_markets : {e}")
            return []

    def display_top(self, markets: list[dict], top_n: int = 10) -> None:
        try:
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
        except Exception as e:
            print(f"  [MarketAnalyzer] Erreur display : {e}")

    def find_mispriced(self, markets: list[dict], threshold: float = 0.05) -> list[dict]:
        try:
            mispriced = []
            for m in markets:
                total = m["yes_price"] + m["no_price"]
                gap   = abs(total - 1.0)
                if gap > threshold:
                    mispriced.append({**m, "price_gap": round(gap, 4)})
            return sorted(mispriced, key=lambda x: x["price_gap"], reverse=True)
        except Exception:
            return []
