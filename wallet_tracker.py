"""
wallet_tracker.py - Suit les positions et trades d'un wallet Polymarket
"""

import requests
import time
from datetime import datetime

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

HEADERS = {"Accept": "application/json", "User-Agent": "polymarket-bot/1.0"}


def get_positions(wallet: str) -> list[dict]:
    """Retourne les positions ouvertes d'un wallet."""
    url = f"{DATA_API}/positions"
    params = {"user": wallet.lower(), "sizeThreshold": "0"}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"[WalletTracker] Erreur positions {wallet[:10]}...: {e}")
        return []


def get_trade_history(wallet: str, limit: int = 50) -> list[dict]:
    """Retourne l'historique des trades d'un wallet via /trades?user=."""
    url = f"{DATA_API}/trades"
    params = {"user": wallet.lower(), "limit": limit}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        # Injecte le wallet dans chaque trade pour le tracking
        for t in data:
            t.setdefault("wallet", wallet)
        return data
    except requests.RequestException as e:
        print(f"[WalletTracker] Erreur historique {wallet[:10]}...: {e}")
        return []


def compute_pnl(positions: list[dict]) -> dict:
    """Calcule le PnL réalisé à partir d'une liste de positions.

    Utilise le champ 'realizedPnl' de l'API (gains déjà encaissés sur positions
    fermées ou partiellement fermées) pour éviter d'afficher uniquement les
    pertes latentes sur positions ouvertes, qui biaisait le PnL en négatif.
    """
    if not positions:
        return {}
    realized    = sum(float(p.get("realizedPnl",  0) or 0) for p in positions)
    unrealized  = sum(float(p.get("cashPnl",      0) or 0) for p in positions)
    total_value = sum(float(p.get("currentValue", 0) or 0) for p in positions)
    return {
        "profit":     round(realized,   2),   # PnL réalisé (gains encaissés)
        "unrealized": round(unrealized, 2),   # PnL latent positions ouvertes
        "volume":     round(total_value, 2),
    }


class WalletTracker:
    def __init__(self, wallets: list[str]):
        self.wallets = wallets
        # snapshot précédent pour détecter les nouveaux trades
        self._last_trades: dict[str, list] = {}

    def snapshot(self) -> dict:
        """Prend un snapshot de tous les wallets suivis."""
        data = {}
        for wallet in self.wallets:
            positions = get_positions(wallet)
            trades    = get_trade_history(wallet, limit=20)
            pnl       = compute_pnl(positions)   # réutilise positions déjà chargées
            data[wallet] = {
                "positions": positions,
                "recent_trades": trades,
                "pnl": pnl,
                "timestamp": datetime.utcnow().isoformat(),
            }
            time.sleep(0.3)  # rate-limit poli
        return data

    @staticmethod
    def _trade_key(trade: dict) -> str:
        """Clé unique d'un trade : conditionId + timestamp + wallet + side."""
        return f"{trade.get('conditionId','')}|{trade.get('timestamp','')}|{trade.get('proxyWallet', trade.get('wallet',''))}|{trade.get('side','')}"

    def detect_new_trades(self, current_snapshot: dict) -> list[dict]:
        """Détecte les trades apparus depuis le dernier snapshot."""
        new_trades = []
        for wallet, data in current_snapshot.items():
            prev_keys = {
                self._trade_key(t) for t in self._last_trades.get(wallet, [])
            }
            for trade in data.get("recent_trades", []):
                key = self._trade_key(trade)
                if key not in prev_keys:
                    new_trades.append({**trade, "wallet": wallet})
            # mise à jour du cache
            self._last_trades[wallet] = data.get("recent_trades", [])
        return new_trades

    def display_summary(self, snapshot: dict) -> None:
        """Affiche un résumé des wallets dans la console."""
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
