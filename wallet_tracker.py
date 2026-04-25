"""
wallet_tracker.py - Suit les positions et trades d'un wallet Polymarket
Robuste : retry automatique, backoff exponentiel, jamais de crash.
Session HTTP persistante + cache positions (fetch tous les 5 cycles).
"""

import requests
import time
import threading
from datetime import datetime

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

HEADERS = {"Accept": "application/json", "User-Agent": "polymarket-bot/1.0"}

# Session persistante — réutilise les connexions TCP (réduit trafic réseau)
_session = requests.Session()
_session.headers.update(HEADERS)


def _safe_get(url: str, params: dict = None, timeout: int = 6,
              retries: int = 3, label: str = "") -> list | dict | None:
    """GET avec retry + backoff exponentiel. Ne lève jamais d'exception."""
    for attempt in range(retries):
        try:
            r = _session.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"  [API] Rate-limit 429{' ' + label if label else ''} — attente {wait}s")
                time.sleep(wait)
                continue
            if r.status_code == 200:
                data = r.json()
                r.close()
                return data
            r.close()
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
    """Détecte les trades par deux méthodes complémentaires :
    1. Historique de trades (detect_new_trades) — marchés récents, souvent résolus
    2. Changements de positions (detect_position_changes) — FIABLE : positions actives uniquement
    """

    def __init__(self, wallets: list[str]):
        self.wallets = wallets
        self._wallets_lock = threading.Lock()  # protège wallets contre les accès concurrents
        self._last_trades: dict[str, list] = {}
        self._last_positions: dict[str, dict] = {}  # wallet → {market_key: position}
        self._snapshot_count = 0

    @staticmethod
    def _pos_key(pos: dict) -> str:
        """Clé unique pour une position : conditionId + outcome."""
        cid = pos.get("conditionId") or pos.get("market") or pos.get("asset_id", "")
        out = (pos.get("outcome") or "").upper()
        return f"{cid}|{out}"

    def snapshot(self) -> dict:
        """Prend un snapshot COMPLET à chaque cycle.
        Les positions sont fetchées à chaque cycle pour détecter les changements en temps réel."""
        self._snapshot_count += 1
        # Copie thread-safe de la liste avant d'itérer (le leaderboard thread peut modifier wallets)
        with self._wallets_lock:
            wallets_snapshot = list(self.wallets)
        data = {}
        for wallet in wallets_snapshot:
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
        """Détecte les trades apparus depuis le dernier snapshot (méthode historique)."""
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

    def detect_position_changes(self, current_snapshot: dict) -> list[dict]:
        """Detecte les BUY/SELL en comparant les positions ouvertes actuelles vs precedentes.
        METHODE FIABLE : les positions ouvertes sont par definition sur des marches actifs.
        - Nouvelle position apparue       -> signal BUY
        - Position disparue               -> signal SELL
        - Position dont la taille a augmente -> signal BUY supplementaire

        Gestion des nouveaux wallets (ajoutes via leaderboard) :
        Le premier cycle pour un wallet donne initialise silencieusement son baseline
        sans emettre de signaux (evite un flood de BUY sur toutes ses positions existantes).
        """
        signals = []
        is_global_first_run = not self._last_positions  # aucun wallet initialise
        try:
            for wallet, data in current_snapshot.items():
                cur_positions = data.get("positions", [])
                # Index par cle unique
                cur_map: dict[str, dict] = {}
                for p in cur_positions:
                    k = self._pos_key(p)
                    if k:
                        cur_map[k] = p

                # Nouveau wallet : initialiser le baseline silencieusement
                if wallet not in self._last_positions:
                    self._last_positions[wallet] = cur_map
                    print(f"  [PositionChange] Init baseline : {len(cur_map)} positions pour {wallet[:10]}...")
                    continue

                prev_map = self._last_positions[wallet]

                # Nouvelles positions ou positions augmentees -> BUY
                for k, pos in cur_map.items():
                    cid = pos.get("conditionId") or pos.get("market") or ""
                    outcome = (pos.get("outcome") or "YES").upper()
                    # Prix : on essaie plusieurs champs
                    price = float(pos.get("curPrice") or pos.get("avgPrice") or 0.5)
                    size_cur = float(pos.get("size") or pos.get("cashBalance") or 0)

                    if k not in prev_map:
                        # Nouvelle position
                        signals.append({
                            "side":        "BUY",
                            "conditionId": cid,
                            "outcome":     outcome,
                            "price":       price,
                            "size":        size_cur,
                            "tokenId":     pos.get("asset_id") or pos.get("tokenId") or "",
                            "market":      cid,
                            "wallet":      wallet,
                            "timestamp":   int(time.time()),
                            "_source":     "position_change",
                        })
                    else:
                        # Position existante : a-t-elle augmente de taille ?
                        size_prev = float(prev_map[k].get("size") or prev_map[k].get("cashBalance") or 0)
                        if size_cur > size_prev * 1.05:  # +5% de tolerance
                            signals.append({
                                "side":        "BUY",
                                "conditionId": cid,
                                "outcome":     outcome,
                                "price":       price,
                                "size":        size_cur - size_prev,
                                "tokenId":     pos.get("asset_id") or pos.get("tokenId") or "",
                                "market":      cid,
                                "wallet":      wallet,
                                "timestamp":   int(time.time()),
                                "_source":     "position_increase",
                            })

                # Positions disparues -> SELL
                for k, pos in prev_map.items():
                    if k not in cur_map:
                        cid = pos.get("conditionId") or pos.get("market") or ""
                        outcome = (pos.get("outcome") or "YES").upper()
                        price = float(pos.get("curPrice") or pos.get("avgPrice") or 0.5)
                        signals.append({
                            "side":        "SELL",
                            "conditionId": cid,
                            "outcome":     outcome,
                            "price":       price,
                            "size":        0,
                            "tokenId":     pos.get("asset_id") or pos.get("tokenId") or "",
                            "market":      cid,
                            "wallet":      wallet,
                            "timestamp":   int(time.time()),
                            "_source":     "position_close",
                        })

                self._last_positions[wallet] = cur_map

            if signals:
                buys  = sum(1 for s in signals if s["side"] == "BUY")
                sells = sum(1 for s in signals if s["side"] == "SELL")
                print(f"  [PositionChange] {buys} BUY + {sells} SELL detectes")
        except Exception as e:
            print(f"  [WalletTracker] Erreur detect_position_changes : {e}")

        return signals

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
