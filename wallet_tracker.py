"""
wallet_tracker.py - Suit les positions et trades d'un wallet Polymarket
Robuste : retry automatique, backoff exponentiel, jamais de crash.
"""

import requests
import time
from datetime import datetime

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

HEADERS = {"Accept": "application/json", "User-Agent": "polymarket-bot/1.0"}


def _safe_get(url: str, params: dict = None, timeout: int = 6,
              retries: int = 3, label: str = "") -> list | dict | None:
    """GET avec retry + backoff exponentiel. Ne lève jamais d'exception."""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"  [API] Rate-limit 429{' ' + label if label else ''} — attente {wait}s")
                time.sleep(wait)
                continue
            if r.status_code == 200:
                return r.json()
            # 5xx ou autre : on retente
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
        except requests.exceptions.Timeout:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"  [API] Timeout{' ' + label if label else ''} après {retries} tentatives")
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"  [API] Erreur{' ' + label if label else ''} : {e}")
    return None


def get_positions(wallet: str) -> list[dict]:
    """Retourne les positions ouvertes d'un wallet."""
    result = _safe_get(
        f"{DATA_API}/positions",
        params={"user": wallet.lower(), "sizeThreshold": "0"},
        label=f"positions {wallet[:10]}"
    )
    return result if isinstance(result, list) else []


def get_trade_history(wallet: str, limit: int = 20) -> list[dict]:
    """Retourne l'historique des trades d'un wallet (max 50 pour économiser mémoire)."""
    result = _safe_get(
        f"{DATA_API}/trades",
        params={"user": wallet.lower(), "limit": min(limit, 50)},
        label=f"trades {wallet[:10]}"
    )
    if not isinstance(result, list):
        return []
    for t in result:
        t.setdefault("wallet", wallet)
    # Garde uniquement les champs utiles pour réduire l'empreinte mémoire
    return [
        {k: t.get(k) for k in (
            "conditionId", "timestamp", "side", "outcome", "price",
            "size", "asset", "asset_id", "tokenId", "market",
            "proxyWallet", "wallet"
        )}
        for t in result[:50]
    ]


def compute_pnl(positions: list[dict]) -> dict:
    """Calcule le PnL réalisé à partir d'une liste de positions."""
    if not positions:
        return {}
    try:
        realized    = sum(float(p.get("realizedPnl",  0) or 0) for p in positions)
        unrealized  = sum(float(p.get("cashPnl",      0) or 0) for p in positions)
        total_value = sum(float(p.get("currentValue", 0) or 0) for p in positions)
        return {
            "profit":     round(realized,   2),
            "unrealized": round(unrealized, 2),
            "volume":     round(total_value, 2),
        }
    except Exception as e:
        print(f"  [WalletTracker] Erreur compute_pnl : {e}")
        return {}


class WalletTracker:
    def __init__(self, wallets: list[str]):
        self.wallets = wallets
        self._last_trades: dict[str, list] = {}

    def snapshot(self) -> dict:
        """Prend un snapshot de tous les wallets suivis. Ne lève jamais d'exception."""
        data = {}
        for wallet in self.wallets:
            try:
                positions = get_positions(wallet)
                trades    = get_trade_history(wallet, limit=20)
                pnl       = compute_pnl(positions)
                data[wallet] = {
                    "positions":     positions,
                    "recent_trades": trades,
                    "pnl":           pnl,
                    "timestamp":     datetime.utcnow().isoformat(),
                }
            except Exception as e:
                print(f"  [WalletTracker] Erreur snapshot {wallet[:10]}... : {e}")
                data[wallet] = {
                    "positions": [], "recent_trades": [], "pnl": {},
                    "timestamp": datetime.utcnow().isoformat(),
                }
            time.sleep(0.3)
        return data

    @staticmethod
    def _trade_key(trade: dict) -> str:
        return (
            f"{trade.get('conditionId','')}|{trade.get('timestamp','')}|"
            f"{trade.get('proxyWallet', trade.get('wallet',''))}|{trade.get('side','')}"
        )

    def detect_new_trades(self, current_snapshot: dict) -> list[dict]:
        """Détecte les trades apparus depuis le dernier snapshot."""
        new_trades = []
        try:
            for wallet, data in current_snapshot.items():
                prev_keys = {
                    self._trade_key(t) for t in self._last_trades.get(wallet, [])
                }
                for trade in data.get("recent_trades", []):
                    key = self._trade_key(trade)
                    if key not in prev_keys:
                        new_trades.append({**trade, "wallet": wallet})
                self._last_trades[wallet] = data.get("recent_trades", [])[:50]
        except Exception as e:
            print(f"  [WalletTracker] Erreur detect_new_trades : {e}")
        return new_trades

    def display_summary(self, snapshot: dict) -> None:
        try:
            print("\n" + "=" * 60)
            print(f"  WALLET TRACKER — {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
            print("=" * 60)
            for wallet, data in snapshot.items():
                pnl   = data.get("pnl", {})
                pos   = data.get("positions", [])
                print(f"\nWallet : {wallet[:12]}...{wallet[-6:]}")
                print(f"  Positions ouvertes : {len(pos)}")
                profit = pnl.get("profit", "N/A")
                volume = pnl.get("volume", "N/A")
                print(f"  PnL total  : ${profit}")
                print(f"  Volume     : ${volume}")
        except Exception as e:
            print(f"  [WalletTracker] Erreur display : {e}")
