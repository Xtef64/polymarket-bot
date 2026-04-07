"""
leaderboard_selector.py - Selection automatique des meilleurs wallets Polymarket

Toutes les 24h, interroge le leaderboard officiel Polymarket, filtre les wallets
actifs (trades recents sur marches actifs) avec PnL positif, et met a jour la
liste suivie par WalletTracker sans redemarrer le bot.
"""

import time
import threading
import requests
from datetime import datetime, timezone

LEADERBOARD_API = "https://data-api.polymarket.com/v1/leaderboard"
DATA_API        = "https://data-api.polymarket.com"

_session = requests.Session()
_session.headers.update({"Accept": "application/json", "User-Agent": "polymarket-bot/1.0"})

# Intervalle de rafraichissement du leaderboard
REFRESH_INTERVAL_H = 24

# Parametres de selection
N_WALLETS          = 5    # nombre de wallets a suivre
MIN_PNL            = 0    # PnL minimum ($) pour etre eligible
LEADERBOARD_POOL   = 50   # combien de wallets on scrute en haut du classement
MIN_RECENT_TRADES  = 3    # trades recents minimum pour etre considere "actif"
RECENT_HOURS       = 72   # fenetre d'activite recente (heures)


# -- Helpers ------------------------------------------------------------------

def _fetch_leaderboard(limit: int = LEADERBOARD_POOL,
                       time_period: str = "MONTH") -> list[dict]:
    """Recupere le classement Polymarket (PnL mensuel par defaut)."""
    try:
        r = _session.get(
            LEADERBOARD_API,
            params={"limit": limit, "orderBy": "PNL", "timePeriod": time_period},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            r.close()
            return data if isinstance(data, list) else []
        r.close()
    except Exception as e:
        print(f"  [Leaderboard] Erreur fetch : {e}")
    return []


def _count_recent_trades(wallet: str, hours: int = RECENT_HOURS) -> int:
    """Compte les trades recents d'un wallet sur des marches encore actifs."""
    try:
        r = _session.get(
            f"{DATA_API}/trades",
            params={"user": wallet.lower(), "limit": 20},
            timeout=8,
        )
        if r.status_code != 200:
            r.close()
            return 0
        trades = r.json()
        r.close()
        if not isinstance(trades, list):
            return 0

        now = datetime.now(timezone.utc).timestamp()
        cutoff = now - hours * 3600
        return sum(
            1 for t in trades
            if float(t.get("timestamp", 0) or 0) >= cutoff
        )
    except Exception:
        return 0


def _has_open_positions(wallet: str) -> bool:
    """Verifie qu'un wallet a des positions ouvertes (marches actifs)."""
    try:
        r = _session.get(
            f"{DATA_API}/positions",
            params={"user": wallet.lower(), "sizeThreshold": "0"},
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            r.close()
            return isinstance(data, list) and len(data) > 0
        r.close()
    except Exception:
        pass
    return False


# -- Selection principale -----------------------------------------------------

def select_best_wallets(n: int = N_WALLETS,
                        time_period: str = "MONTH") -> list[dict]:
    """
    Retourne les N meilleurs wallets actifs avec PnL positif.

    Criteres :
    1. PnL > MIN_PNL sur la periode
    2. Au moins MIN_RECENT_TRADES trades dans les dernieres RECENT_HOURS
    3. Au moins 1 position ouverte (actif sur marches actifs)

    Retourne une liste de dicts :
      { "address": str, "pnl": float, "vol": float,
        "username": str, "rank": str, "recent_trades": int }
    """
    print(f"\n  [Leaderboard] Analyse du classement (top {LEADERBOARD_POOL}, periode={time_period})...")
    candidates = _fetch_leaderboard(limit=LEADERBOARD_POOL, time_period=time_period)
    if not candidates:
        print("  [Leaderboard] Impossible de recuperer le classement")
        return []

    selected = []
    checked  = 0
    for entry in candidates:
        if len(selected) >= n:
            break

        wallet  = entry.get("proxyWallet", "")
        pnl     = float(entry.get("pnl", 0) or 0)
        vol     = float(entry.get("vol", 0) or 0)
        username = entry.get("userName", wallet[:10])
        rank    = entry.get("rank", "?")

        if not wallet or len(wallet) != 42:
            continue
        if pnl <= MIN_PNL:
            continue

        checked += 1
        # Verifie l'activite recente (coute 1-2 API calls par wallet)
        recent = _count_recent_trades(wallet, RECENT_HOURS)
        if recent < MIN_RECENT_TRADES:
            print(f"    #{rank} {username}: PnL=${pnl:,.0f} mais seulement {recent} trades recents - ignore")
            time.sleep(0.2)
            continue

        has_pos = _has_open_positions(wallet)
        if not has_pos:
            print(f"    #{rank} {username}: actif mais 0 positions ouvertes - ignore")
            time.sleep(0.2)
            continue

        selected.append({
            "address":      wallet,
            "pnl":          pnl,
            "vol":          vol,
            "username":     username,
            "rank":         rank,
            "recent_trades": recent,
        })
        print(f"    #{rank} {username} ({wallet[:10]}...): PnL=${pnl:,.0f} | {recent} trades recents - SELECTIONNE")
        time.sleep(0.2)

    print(f"  [Leaderboard] {len(selected)}/{checked} wallets selectionnes")
    return selected


# -- Thread daemon ------------------------------------------------------------

def leaderboard_refresh_loop(
    tracker,                        # WalletTracker dont on met a jour .wallets
    perf: dict,                     # perf dict pour logger la selection
    stop_event: threading.Event,
    interval_h: int = REFRESH_INTERVAL_H,
    tg_send=None,                   # fonction optionnelle pour notifier Telegram
) -> None:
    """
    Thread daemon : rafraichit la selection des wallets toutes les interval_h heures.
    Premier run des le demarrage (apres 30s pour laisser le bot s'initialiser).
    """
    stop_event.wait(timeout=30)  # laisse le 1er cycle de trading s'executer

    while not stop_event.is_set():
        try:
            _run_selection(tracker, perf, tg_send)
        except Exception as e:
            print(f"  [Leaderboard] Erreur inattendue : {e}")

        # Attendre interval_h heures avant le prochain rafraichissement
        stop_event.wait(timeout=interval_h * 3600)


def _run_selection(tracker, perf: dict, tg_send=None) -> None:
    """Execute une selection et met a jour tracker.wallets si des candidats sont trouves."""
    now = datetime.now(timezone.utc).isoformat()
    best = select_best_wallets()

    if not best:
        print("  [Leaderboard] Aucun wallet eligible - conservation de la liste actuelle")
        return

    new_addresses = [w["address"] for w in best]
    old_addresses = list(tracker.wallets)

    added   = [a for a in new_addresses if a not in old_addresses]
    removed = [a for a in old_addresses if a not in new_addresses]

    if not added and not removed:
        print(f"  [Leaderboard] Wallets inchanges ({len(new_addresses)} suivis)")
        _log_selection(perf, best, now, changed=False)
        return

    # Mise a jour thread-safe de la liste
    tracker.wallets = new_addresses
    # Reinitialise les caches position/trade pour les wallets retires
    # Les nouveaux wallets seront initialises silencieusement au 1er cycle
    # (detect_position_changes gere le cas wallet non present dans _last_positions)
    tracker._last_trades    = {w: v for w, v in tracker._last_trades.items()    if w in new_addresses}
    tracker._last_positions = {w: v for w, v in tracker._last_positions.items() if w in new_addresses}

    print(f"\n  [Leaderboard] Wallets MIS A JOUR")
    print(f"    Ajoutes  : {added}")
    print(f"    Retires  : {removed}")
    for w in best:
        print(f"    #{w['rank']:>3} {w['username']:<20} PnL=${w['pnl']:>12,.0f}  trades_recents={w['recent_trades']}")

    _log_selection(perf, best, now, changed=True, added=added, removed=removed)

    if tg_send:
        lines = [f"<b>Wallets mis a jour automatiquement</b> ({now[:10]})"]
        for w in best:
            lines.append(f"  #{w['rank']} {w['username']} - PnL ${w['pnl']:,.0f}")
        if added:
            lines.append(f"\n  + Ajoutes   : {len(added)}")
        if removed:
            lines.append(f"  - Retires   : {len(removed)}")
        try:
            tg_send("\n".join(lines))
        except Exception:
            pass


def _log_selection(perf: dict, wallets: list[dict], ts: str,
                   changed: bool, added: list = None, removed: list = None) -> None:
    """Enregistre la selection dans perf["leaderboard_history"] (max 30 entrees)."""
    history = perf.setdefault("leaderboard_history", [])
    history.append({
        "timestamp": ts,
        "changed":   changed,
        "wallets":   [
            {"address": w["address"], "username": w["username"],
             "pnl": w["pnl"], "rank": w["rank"]}
            for w in wallets
        ],
        "added":   added or [],
        "removed": removed or [],
    })
    if len(history) > 30:
        perf["leaderboard_history"] = history[-30:]
