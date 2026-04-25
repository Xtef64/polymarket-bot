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
REFRESH_INTERVAL_H = 1    # toutes les heures

# Parametres de selection
N_WALLETS          = 5    # nombre de wallets a suivre
MIN_PNL            = 0    # PnL minimum ($) pour etre eligible
LEADERBOARD_POOL   = 80   # combien de wallets on scrute en haut du classement
MIN_RECENT_TRADES  = 3    # trades recents minimum pour etre considere "actif"
RECENT_HOURS       = 72   # fenetre d'activite recente (heures)

# Criteres de remplacement automatique (evalue chaque heure)
INACTIVE_MAX_TRADES_1H = 2   # < 3 trades dans la derniere heure → inactif (si PnL=0)
INACTIVE_MAX_PNL       = 0.0 # PnL mensuel <= 0 → combiné avec trades/1h pour décider


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


# -- PnL individuel -----------------------------------------------------------

def _get_wallets_pnl(addresses: list, time_period: str = "MONTH") -> dict:
    """Retourne {address_lower: pnl} pour les wallets donnes.
    Fetche le top 200 du leaderboard et mappe les adresses.
    Si une adresse est absente du classement, son PnL est 0."""
    candidates = _fetch_leaderboard(limit=200, time_period=time_period)
    result = {a.lower(): 0.0 for a in addresses}
    for entry in candidates:
        addr = (entry.get("proxyWallet") or "").lower()
        if addr in result:
            result[addr] = float(entry.get("pnl", 0) or 0)
    return result


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
        ts_start = datetime.now(timezone.utc)
        try:
            changed, n_replaced, n_kept = _run_selection(tracker, perf, tg_send)
        except Exception as e:
            print(f"  [Leaderboard] Erreur inattendue : {e}")
            changed, n_replaced, n_kept = False, 0, len(tracker.wallets)

        # Log horaire de confirmation (visible dans les logs Railway)
        ts_end    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        tag       = f"{n_replaced} remplace(s)" if changed else "aucun changement"
        print(
            f"\n  [Leaderboard] ✓ Scan {ts_end} UTC — "
            f"{n_kept} wallet(s) actif(s), {tag} | "
            f"prochain scan dans {interval_h}h"
        )

        # Attendre interval_h heures avant le prochain rafraichissement
        stop_event.wait(timeout=interval_h * 3600)


def _run_selection(tracker, perf: dict, tg_send=None) -> tuple[bool, int, int]:
    """Evalue chaque wallet suivi. Remplace ceux qui sont inactifs
    (PnL=0 ET 0 trade dans la derniere heure) par de nouveaux candidats.
    Retourne (changed, n_replaced, n_kept)."""
    now = datetime.now(timezone.utc).isoformat()
    with tracker._wallets_lock:
        current = list(tracker.wallets)

    print(f"\n  [Leaderboard] Evaluation des {len(current)} wallets suivis...")

    # 1. PnL mensuel de chaque wallet suivi (1 appel leaderboard)
    pnl_map = _get_wallets_pnl(current)

    # 2. Identifie les wallets inactifs
    inactive = []
    for wallet in current:
        pnl = pnl_map.get(wallet.lower(), 0.0)
        if pnl > INACTIVE_MAX_PNL:
            print(f"    {wallet[:12]}... PnL=${pnl:,.0f} — actif (PnL positif)")
            continue
        trades_1h = _count_recent_trades(wallet, hours=1)
        if trades_1h <= INACTIVE_MAX_TRADES_1H:
            print(f"    {wallet[:12]}... PnL=${pnl:,.0f} trades/1h={trades_1h} — INACTIF → remplacement")
            inactive.append(wallet)
        else:
            print(f"    {wallet[:12]}... PnL=${pnl:,.0f} trades/1h={trades_1h} — PnL=0 mais actif, conserve")
        time.sleep(0.2)

    if not inactive:
        print(f"  [Leaderboard] Tous les wallets actifs — aucun remplacement")
        _log_selection(perf, [], now, changed=False)
        return False, 0, len(current)

    print(f"  [Leaderboard] {len(inactive)} wallet(s) inactif(s) a remplacer")

    # 3. Cherche des remplacants dans le leaderboard (non deja suivis)
    keep_set      = {w.lower() for w in current if w not in inactive}
    candidates    = _fetch_leaderboard(limit=LEADERBOARD_POOL)
    inactive_q    = list(inactive)
    replacements  = []   # [{old_wallet, new_wallet, pnl, username, rank, trades_1h}]

    for entry in candidates:
        if not inactive_q:
            break
        addr     = (entry.get("proxyWallet") or "").lower()
        pnl      = float(entry.get("pnl", 0) or 0)
        username = entry.get("userName", addr[:10])
        rank     = entry.get("rank", "?")

        if not addr or len(addr) != 42:
            continue
        if addr in keep_set or pnl <= INACTIVE_MAX_PNL:
            continue

        trades_1h = _count_recent_trades(addr, hours=1)
        if trades_1h <= INACTIVE_MAX_TRADES_1H:
            print(f"    #{rank} {username}: PnL=${pnl:,.0f} mais 0 trade/1h — ignore")
            time.sleep(0.2)
            continue

        old = inactive_q.pop(0)
        replacements.append({
            "old_wallet": old,
            "new_wallet": addr,
            "pnl":        pnl,
            "username":   username,
            "rank":       rank,
            "trades_1h":  trades_1h,
        })
        keep_set.add(addr)
        print(f"    #{rank} {username} ({addr[:12]}...): {trades_1h} trade(s)/1h "
              f"→ remplace {old[:12]}...")
        time.sleep(0.2)

    if not replacements:
        print("  [Leaderboard] Aucun remplacant eligible trouve — liste inchangee")
        _log_selection(perf, [], now, changed=False)
        return False, 0, len(current)

    # 4. Applique les remplacements (preserves l'ordre, remplace en place)
    replace_map = {r["old_wallet"].lower(): r["new_wallet"] for r in replacements}
    new_wallets = [replace_map.get(w.lower(), w) for w in current]

    removed_set = {r["old_wallet"] for r in replacements}
    # Verrou : empêche snapshot() de lire wallets pendant la mise à jour
    with tracker._wallets_lock:
        tracker.wallets         = new_wallets
        tracker._last_trades    = {w: v for w, v in tracker._last_trades.items()    if w not in removed_set}
        tracker._last_positions = {w: v for w, v in tracker._last_positions.items() if w not in removed_set}

    # Synchronise perf["meta"]["wallets"] → persisté par save_perf + lu par le dashboard
    perf.setdefault("meta", {})["wallets"] = new_wallets

    print(f"\n  [Leaderboard] {len(replacements)} wallet(s) remplace(s) :")
    for r in replacements:
        print(f"    - {r['old_wallet'][:12]}... → #{r['rank']} {r['username']} "
              f"({r['new_wallet'][:12]}...) PnL=${r['pnl']:,.0f}")

    _log_selection(perf, replacements, now, changed=True)
    n_replaced = len(replacements)
    n_kept     = len(current) - n_replaced

    # 5. Notification Telegram
    if tg_send:
        lines = [f"🔄 <b>Wallets mis à jour automatiquement</b>"]
        for r in replacements:
            lines.append(
                f"\n❌ Retiré  : <code>{r['old_wallet'][:14]}…</code>"
                f" (PnL=$0, inactif)\n"
                f"✅ Ajouté  : #{r['rank']} <b>{r['username']}</b>"
                f" <code>{r['new_wallet'][:14]}…</code>\n"
                f"   PnL ${r['pnl']:,.0f}/mois · {r['trades_1h']} trade(s)/1h"
            )
        try:
            tg_send("\n".join(lines))
        except Exception:
            pass

    return True, n_replaced, n_kept


def _log_selection(perf: dict, replacements: list[dict], ts: str,
                   changed: bool) -> None:
    """Enregistre le resultat de l'evaluation dans perf["leaderboard_history"] (max 30)."""
    history = perf.setdefault("leaderboard_history", [])
    entry = {"timestamp": ts, "changed": changed, "replacements": []}
    if changed:
        entry["replacements"] = [
            {
                "old": r["old_wallet"],
                "new": r["new_wallet"],
                "username": r["username"],
                "pnl": r["pnl"],
                "rank": r["rank"],
            }
            for r in replacements
        ]
        # Champs compatibles avec le dashboard existant
        entry["added"]   = [r["new_wallet"] for r in replacements]
        entry["removed"] = [r["old_wallet"] for r in replacements]
        entry["wallets"] = [
            {"address": r["new_wallet"], "username": r["username"],
             "pnl": r["pnl"], "rank": r["rank"]}
            for r in replacements
        ]
    else:
        entry["added"] = []
        entry["removed"] = []
        entry["wallets"] = []
    history.append(entry)
    if len(history) > 30:
        perf["leaderboard_history"] = history[-30:]
