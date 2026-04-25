"""
main.py - Orchestrateur du bot Polymarket
Usage : python main.py [--live]  (dry run par défaut)
"""

import sys
import time
import json
import argparse
import os
import gc
import signal
import threading
import traceback
import requests
from datetime import datetime, timezone

# Force UTF-8 sur stdout/stderr (Windows cp1252 ne supporte pas les box-drawing chars)
# Actif seulement sur Windows ; sur Linux (Railway), stdout est déjà UTF-8
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.platform == "win32" and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from wallet_tracker        import WalletTracker
from market_analyzer       import MarketAnalyzer
from copytrader            import CopyTrader
from serve                 import start_server_thread
from leaderboard_selector  import leaderboard_refresh_loop
from telegram_notifier     import (
    TelegramCommandHandler, notify_start, notify_stop,
    notify_trade, notify_cycle, _send as tg_send,
)

# ── Configuration ─────────────────────────────────────────────────────────────

# Top 3 du leaderboard Polymarket par profit (30j) — limité à 3 pour réduire l'empreinte mémoire
WALLETS_TO_TRACK = [
    "0xea9b517a08ccf962b85db123b36e775a87d02be5",  # conservé — actif et performant
    "0xc2e7800b5af46e6093872b177b7a5e7f0563be51",  # #4 beachboy4    — 9 trades/24h, PnL $3.7M
    "0x2005d16a84ceefa912d4e380cd32e7ff827875ea",  # #7 RN1           — 20 trades/24h, PnL $1.9M
    "0xee613b3fc183ee44f9da9c05f53e2da107e3debf",  # #9 sovereign2013 — 20 trades/24h, PnL $1.75M
    "0xb45a797faa52b0fd8adc56d30382022b7b12192c",  # #12 bcda         — 7 trades/24h, PnL $1.3M
    "0xc8049876db52053426dfb71875a879fbe25ec12b",  # ajouté manuellement
    "0xd1042bcea4113d0d831bf2aad893e820bd2bc79c",  # ajouté manuellement
    "0x37ccc65cbae317faafb10802a9da1825fe0cc53a",  # ajouté manuellement
    "0x1fa9b794a9191a05f66ceb34da1f7c6aa6571fa9",  # ajouté manuellement
    "0xa4ed238c0ba0957b5835f47eed15ec3974a2d010",  # ajouté manuellement
    "0x0006a661f1a09e9e0670943bcd4fc3b830238c63",  # ajouté manuellement
]

# Wallets explicitement exclus du suivi (mauvaises performances en copie)
# Le leaderboard_selector ne les sélectionnera jamais non plus.
EXCLUDED_WALLETS: set[str] = {
    "0x204f72f35326db932158cba6adff0b9a1da95e14",  # swisstony — PnL copie négatif
}

BOT_CONFIG = {
    "poll_interval_sec":  30,   # réduit 60→30s : capture les trades sports avant résolution
    "top_markets_limit":  30,   # réduit de 50 → 30 pour économiser mémoire
    "top_markets_display": 10,
    "trade_size_usdc":    10.0,
    "initial_balance":  300.0,
    "max_positions":      20,   # réduit de 80 → 20 max
    "min_volume_24h":  5_000.0,
    "min_score":          4.0,
}

PERF_FILE    = os.path.join(os.path.dirname(__file__), "performance.json")
_PERF_LOCK   = threading.Lock()   # protège perf dict + écriture fichier
_price_cache: dict = {}           # token_id → prix courant (mis à jour par le refresher)
MAX_CYCLES_IN_MEMORY  = 50         # cap anti-OOM : réduit de 200→50 (positions × cycles = JSON géant)
MAX_CONSECUTIVE_ERRORS = 10       # backoff max après erreurs répétées
_consecutive_errors    = 0        # compteur d'erreurs consécutives (reset à chaque succès)

# Session HTTP persistante pour le price refresher — réutilise les connexions TCP
_price_session = requests.Session()
_price_session.headers.update({"Accept": "application/json", "User-Agent": "polymarket-bot/1.0"})

# ──────────────────────────────────────────────────────────────────────────────


def load_perf() -> dict:
    """Charge le fichier performance.json existant, ou retourne une structure vide."""
    if os.path.exists(PERF_FILE):
        try:
            with open(PERF_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Conserve la liste sauvegardée telle quelle (inclut les remplacements leaderboard)
            # Les wallets manuels de WALLETS_TO_TRACK sont fusionnés plus bas,
            # après la création du tracker (main → _sync_tracker_wallets)
            data.setdefault("meta", {}).setdefault("wallets", list(WALLETS_TO_TRACK))
            data["meta"]["initial_balance"] = BOT_CONFIG["initial_balance"]
            data.setdefault("summary", {})
            data.setdefault("cycles", [])
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "meta": {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "initial_balance": BOT_CONFIG["initial_balance"],
            "wallets": WALLETS_TO_TRACK,
        },
        "cycles": [],
        "summary": {},
    }


def save_perf(data: dict, trader: "CopyTrader", cycle: int,
              snapshot: dict, new_trades: int, executed: int,
              all_executed: int, top_markets: list) -> None:
    """Construit et sauvegarde l'entrée de performance du cycle courant."""
    now = datetime.now(timezone.utc).isoformat()
    portfolio = trader.portfolio

    # Snapshot wallets
    # PnL affiché : PnL Polymarket officiel (mensuel, depuis leaderboard_selector)
    # en priorité sur le PnL calculé localement (qui ne couvre que les positions ouvertes).
    wallets_pnl = data.get("meta", {}).get("wallets_pnl", {})
    wallets_state = {}
    for wallet, wdata in snapshot.items():
        pnl_local = wdata.get("pnl", {})
        pnl_real  = wallets_pnl.get(wallet.lower())   # None si pas encore fetchée
        wallets_state[wallet] = {
            "open_positions": len(wdata.get("positions", [])),
            "pnl":    pnl_real if pnl_real is not None else pnl_local.get("profit", None),
            "volume": pnl_local.get("volume", None),
        }

    # Top 5 marchés (inclut conditionId pour le lookup dashboard)
    top5 = [
        {
            "conditionId": m.get("conditionId", ""),
            "question":    m["question"],
            "yes_price":   m["yes_price"],
            "volume_24h":  m["volume_24h"],
            "score":       m["score"],
        }
        for m in top_markets[:5]
    ]

    # Positions ouvertes — inclut le prix courant depuis _price_cache si disponible
    open_pos = []
    for tid, p in portfolio.positions.items():
        cost      = p["total_cost"]
        shares    = p["shares"]
        cur_price = _price_cache.get(tid)          # None si pas encore rafraîchi
        cur_value = round(shares * cur_price, 4)   if cur_price is not None else None
        unr_pnl   = round(cur_value - cost, 4)     if cur_value is not None else None
        pnl_pct   = round(unr_pnl / cost * 100, 2) if unr_pnl is not None and cost > 0 else None
        open_pos.append({
            "token_id":      tid,
            "market_id":     p.get("market_id", ""),
            "outcome":       p["outcome"],
            "shares":        shares,
            "avg_cost":      p["avg_cost"],
            "total_cost":    cost,
            "opened_at":     p.get("opened_at", ""),
            "current_price": cur_price,
            "current_value": cur_value,
            "unrealized_pnl": unr_pnl,
            "pnl_pct":       pnl_pct,
        })

    # Derniers ordres exécutés (tous : copie + stop-loss + auto-close)
    last_orders = [
        {
            "order_id":         o.order_id,
            "side":             o.side,
            "outcome":          o.outcome,
            "price":            o.price,
            "size_usdc":        o.size_usdc,
            "shares":           o.shares,
            "market_id":        o.market_id,
            "source":           o.wallet_source[:20] if o.wallet_source else "",
            "timestamp":        o.timestamp,
            "entry_price":      getattr(o, "entry_price",      None),
            "realized_pnl":     getattr(o, "realized_pnl",     None),
            "realized_pnl_pct": getattr(o, "realized_pnl_pct", None),
            "duration_sec":     getattr(o, "duration_sec",     None),
        }
        for o in portfolio.order_log[-all_executed:] if all_executed > 0
    ]

    # Accumule les noms / slugs de marchés pour le dashboard (lookup persistant)
    market_names = data.setdefault("market_names", {})
    for m in top_markets:
        cid = m.get("conditionId", "")
        if cid and m.get("question"):
            market_names[cid] = {
                "question":   m["question"],
                "slug":       m.get("slug", ""),
                "group_slug": m.get("group_slug", ""),
            }
    if len(market_names) > 2000:
        excess = len(market_names) - 2000
        for k in list(market_names.keys())[:excess]:
            del market_names[k]

    # Sauvegarde les derniers trades connus par wallet — empêche le re-traitement au redémarrage
    data["_last_wallet_trades"] = {
        wallet: wdata.get("recent_trades", [])[:20]
        for wallet, wdata in snapshot.items()
    }

    # Historique cumulatif des trades (capped 500) — alimente le graphique PnL
    trade_history = data.setdefault("trade_history", [])
    existing_ids  = {t.get("order_id") for t in trade_history}
    for o in (portfolio.order_log[-all_executed:] if all_executed > 0 else []):
        if o.order_id in existing_ids:
            continue
        minfo = market_names.get(o.market_id, {})
        trade_history.append({
            "order_id":          o.order_id,
            "ts":                o.timestamp,
            "market_id":         o.market_id,  # ID complet (plus de troncature)
            "market_question":   minfo.get("question", ""),
            "market_slug":       minfo.get("slug", ""),
            "market_group_slug": minfo.get("group_slug", ""),
            "outcome":           o.outcome,
            "source":            o.wallet_source[:20] if o.wallet_source else "",
            "side":              o.side,
            "price":             o.price,
            "shares":            round(o.shares, 4),
            "entry_price":       getattr(o, "entry_price",      None),
            "realized_pnl":      getattr(o, "realized_pnl",     None),
            "realized_pnl_pct":  getattr(o, "realized_pnl_pct", None),
            "duration_sec":      getattr(o, "duration_sec",      None),
        })
        existing_ids.add(o.order_id)
    if len(trade_history) > 500:
        data["trade_history"] = trade_history[-500:]

    cycle_entry = {
        "cycle":          cycle,
        "timestamp":      now,
        "new_trades_detected": new_trades,
        "orders_executed":     executed,
        "portfolio": {
            "cash_usdc":      round(portfolio.balance_usdc, 4),
            "net_worth":      portfolio.net_worth(),
            "realized_pnl":   round(portfolio.realized_pnl, 4),
            "open_positions": len(portfolio.positions),
            "total_orders":   portfolio.total_orders_count,
            "return_pct":     round(
                (portfolio.net_worth() - BOT_CONFIG["initial_balance"])
                / BOT_CONFIG["initial_balance"] * 100, 4
            ),
        },
        "wallets":       wallets_state,
        "top_markets":   top5,
        "open_positions": open_pos,
        "last_orders":   last_orders,
    }

    data["cycles"].append(cycle_entry)
    # Cap anti-OOM : ne garde que les derniers cycles en mémoire/JSON
    if len(data["cycles"]) > MAX_CYCLES_IN_MEMORY:
        data["cycles"] = data["cycles"][-MAX_CYCLES_IN_MEMORY:]

    # CRITIQUE anti-OOM : open_positions et last_orders sont lourds (N positions × M cycles).
    # On les garde uniquement dans le dernier cycle — les anciens n'en ont pas besoin.
    for old_c in data["cycles"][:-1]:
        old_c.pop("open_positions", None)
        old_c.pop("last_orders", None)
        old_c.pop("top_markets", None)

    # Net worth maximum atteint — mis à jour si dépassé, jamais réduit, préservé au reset
    current_nw  = portfolio.net_worth()
    saved_max   = data.get("meta", {}).get("net_worth_max", BOT_CONFIG["initial_balance"])
    new_max     = max(saved_max, current_nw)
    data.setdefault("meta", {})["net_worth_max"] = round(new_max, 4)

    # Résumé global mis à jour
    data["summary"] = {
        "last_update":      now,
        "total_cycles":     cycle,
        "total_orders":     portfolio.total_orders_count,
        "net_worth":        current_nw,
        "net_worth_max":    round(new_max, 4),
        "cash_usdc":        round(portfolio.balance_usdc, 4),
        "realized_pnl":     round(portfolio.realized_pnl, 4),
        "return_pct":       round(
            (current_nw - BOT_CONFIG["initial_balance"])
            / BOT_CONFIG["initial_balance"] * 100, 4
        ),
        "open_positions":   len(portfolio.positions),
        "best_cycle_orders": max(
            (c["orders_executed"] for c in data["cycles"]), default=0
        ),
    }

    # Écriture atomique avec protection totale contre les erreurs I/O
    # separators compacts : réduit la taille du JSON de ~40% vs indent=2
    try:
        with _PERF_LOCK:
            tmp = PERF_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
            os.replace(tmp, PERF_FILE)
        print(f"  >> performance.json mis a jour (cycle #{cycle}, net_worth=${portfolio.net_worth():.2f})")
    except Exception as e:
        print(f"  [WARN] Impossible d'ecrire performance.json : {e}")


def _restore_portfolio(trader: "CopyTrader", perf: dict) -> None:
    """Restaure les positions et le cash depuis le dernier cycle sauvegardé.
    Cherche open_positions dans les cycles du plus récent au plus ancien
    (les anciens cycles sont purgés de open_positions pour économiser la RAM)."""
    cycles = perf.get("cycles", [])
    summary = perf.get("summary", {})

    # Restaure les compteurs depuis le summary (toujours présent)
    saved_cash = summary.get("cash_usdc", BOT_CONFIG["initial_balance"])
    trader.portfolio.balance_usdc = saved_cash

    # Restaure le PnL réalisé depuis le summary (valeur cumulée, toujours à jour)
    # Note : _last_wallet_trades évite désormais les trades re-traités au restart,
    # donc la valeur du summary reste fiable entre les sessions.
    trader.portfolio.realized_pnl = summary.get("realized_pnl", 0.0)

    # Garde-fou anti-bug : si le PnL réalisé dépasse 10× le capital initial, c'est aberrant
    max_pnl = BOT_CONFIG["initial_balance"] * 10
    if abs(trader.portfolio.realized_pnl) > max_pnl:
        print(
            f"  [SANITY] PnL realise aberrant (${trader.portfolio.realized_pnl:.2f}) "
            f"— depasse 10x le capital (${max_pnl:.0f}) — remis a zero"
        )
        trader.portfolio.realized_pnl = 0.0

    trader.portfolio.total_orders_count = summary.get("total_orders", 0)

    # Cherche open_positions dans les cycles (du plus récent au plus ancien)
    positions = []
    for c in reversed(cycles):
        if c.get("open_positions"):
            positions = c["open_positions"]
            port = c.get("portfolio", {})
            # Priorité au cash du cycle qui contient les positions
            saved_cash = port.get("cash_usdc", saved_cash)
            trader.portfolio.balance_usdc = saved_cash
            break

    if not positions:
        print(f"  >> Portfolio restaure : 0 position(s), cash=${saved_cash:.2f}")
        return

    # Limite à max_positions pour éviter l'accumulation entre restarts
    max_p = trader.max_positions
    if len(positions) > max_p:
        print(f"  [WARN] {len(positions)} positions sauvegardées > max {max_p} — troncature")
        positions = positions[-max_p:]  # garde les plus récentes

    restored = 0
    for p in positions:
        tid = p.get("token_id", "")
        if not tid:
            continue
        trader.portfolio.positions[tid] = {
            "market_id":  p.get("market_id", ""),
            "outcome":    p.get("outcome", ""),
            "shares":     p.get("shares", 0.0),
            "avg_cost":   p.get("avg_cost", 0.0),
            "total_cost": p.get("total_cost", 0.0),
            "opened_at":  p.get("opened_at", ""),  # vide = sera considéré périmé
        }
        restored += 1

    print(f"  >> Portfolio restaure : {restored} position(s), cash=${saved_cash:.2f}")

    # Restaure le compteur d'ordres pour éviter les collisions d'ID (SIM-xxxxx) après redémarrage
    from copytrader import SimulatedOrder
    max_counter = 0
    for t in perf.get("trade_history", []):
        oid = t.get("order_id", "")
        if oid.startswith("SIM-"):
            try:
                n = int(oid[4:])
                if n > max_counter:
                    max_counter = n
            except ValueError:
                pass
    if max_counter > 0:
        SimulatedOrder._counter = max_counter
        print(f"  >> Compteur d'ordres restaure : SIM-{max_counter:05d} (evite collisions post-restart)")


def _do_price_refresh(trader: "CopyTrader", perf: dict) -> None:
    """Récupère les prix CLOB, met à jour _price_cache et performance.json."""
    global _price_cache
    CLOB_API = "https://clob.polymarket.com"

    # Snapshot thread-safe des token_ids en portefeuille
    with _PERF_LOCK:
        token_ids = [tid for tid in trader.portfolio.positions.keys() if tid]

    if not token_ids:
        # Purge complète du cache si plus aucune position ouverte
        if _price_cache:
            print(f"  [price_refresh] Aucune position — purge _price_cache ({len(_price_cache)} entrées)")
            _price_cache.clear()
        else:
            print("  [price_refresh] Aucune position ouverte — rien à rafraîchir")
        return

    # Appels réseau hors verrou (session persistante)
    fetched: dict = {}
    for tid in token_ids:
        try:
            r = _price_session.get(
                f"{CLOB_API}/midpoint",
                params={"token_id": tid},
                timeout=5,
            )
            if r.status_code == 200:
                mid = r.json().get("mid")
                r.close()
                if mid is not None:
                    fetched[tid] = float(mid)
            else:
                r.close()
        except Exception as exc:
            print(f"  [price_refresh] Erreur midpoint {tid[:16]}... : {exc}")

    if not fetched:
        print(f"  [price_refresh] 0/{len(token_ids)} prix reçus — vérifiez les token IDs")
        return

    now          = datetime.now(timezone.utc).isoformat()
    total_cost   = 0.0
    total_value  = 0.0

    with _PERF_LOCK:
        # 1. Mettre à jour le cache + purger les entrées obsolètes (positions fermées)
        current_token_ids = set(token_ids)
        stale = [k for k in _price_cache if k not in current_token_ids]
        for k in stale:
            del _price_cache[k]
        if stale:
            print(f"  [price_refresh] _price_cache : {len(stale)} entrée(s) obsolète(s) purgée(s)")
        _price_cache.update(fetched)

        # 2. Mettre à jour immédiatement le dernier cycle dans perf (dashboard temps réel)
        if perf.get("cycles"):
            last_cycle = perf["cycles"][-1]
            updated = []
            for p in last_cycle.get("open_positions", []):
                tid      = p.get("token_id", "")
                # Priorité : prix fraîchement reçu, sinon valeur précédente dans le cache
                cur_price = fetched.get(tid) or _price_cache.get(tid)
                if cur_price is None:
                    cur_price = p.get("avg_cost", 0.0)
                shares    = p.get("shares",     0.0)
                cost      = p.get("total_cost", 0.0)
                cur_value = shares * cur_price
                unr_pnl   = cur_value - cost
                pnl_pct   = unr_pnl / cost * 100 if cost > 0 else 0.0
                entry = dict(p)
                entry["current_price"]   = round(cur_price, 4)
                entry["current_value"]   = round(cur_value, 4)
                entry["unrealized_pnl"]  = round(unr_pnl,  4)
                entry["pnl_pct"]         = round(pnl_pct,  2)
                updated.append(entry)
                total_cost  += cost
                total_value += cur_value
            last_cycle["open_positions"]    = updated
            last_cycle["prices_updated_at"] = now

        unrealized_total = total_value - total_cost
        perf.setdefault("summary", {})
        perf["summary"]["unrealized_pnl"]    = round(unrealized_total, 4)
        perf["summary"]["prices_updated_at"] = now

        # 3. Sauvegarde atomique (séparateurs compacts = ~40% plus petit)
        tmp = PERF_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(perf, f, separators=(",", ":"), ensure_ascii=False)
        os.replace(tmp, PERF_FILE)

    print(
        f"  >> [price_refresh] {len(fetched)}/{len(token_ids)} prix mis a jour "
        f"| PnL latent : ${unrealized_total:+.2f}"
    )


def refresh_position_prices(trader: "CopyTrader", perf: dict,
                            stop_event: threading.Event,
                            interval: int = 300) -> None:
    """Thread daemon : rafraîchit les prix toutes les `interval` secondes.
    Premier refresh dès que le 1er cycle a eu le temps de sauvegarder (~90s)."""

    # Attendre que le 1er cycle ait sauvegardé des positions
    stop_event.wait(timeout=90)

    while not stop_event.is_set():
        try:
            _do_price_refresh(trader, perf)
        except Exception as e:
            print(f"  [price_refresh] Erreur inattendue : {e}")
        # Attendre avant le prochain refresh
        stop_event.wait(timeout=interval)


def banner(dry_run: bool) -> None:
    mode = "DRY RUN (simulation)" if dry_run else "LIVE -- ordres REELS"
    print("=" * 60)
    print("  POLYMARKET BOT")
    print(f"  Mode    : {mode}")
    print(f"  Wallets : {len(WALLETS_TO_TRACK)} suivis")
    print(f"  Cycle   : toutes les {BOT_CONFIG['poll_interval_sec']}s")
    print(f"  Perf    : {PERF_FILE}")
    print("=" * 60)


def build_market_lookup(markets: list[dict]) -> dict:
    return {m["conditionId"]: m for m in markets if m.get("conditionId")}


def run_cycle(
    tracker:  WalletTracker,
    analyzer: MarketAnalyzer,
    trader:   CopyTrader,
    cycle:    int,
    perf:     dict,
    tg_handler=None,
) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[Cycle #{cycle}] {ts} UTC")

    # Compteur avant le cycle pour capturer TOUS les ordres (SL + autoclose + copie)
    orders_before = trader.portfolio.total_orders_count

    # 0a. Stop-loss : ferme toute position avec PnL latent <= -20%
    try:
        print(f"  [StopLoss] _price_cache : {len(_price_cache)} token(s) — "
              f"positions ouvertes : {len(trader.portfolio.positions)}")
        sl_orders = trader.auto_stop_loss(_price_cache, max_loss_pct=-20.0)
        if sl_orders:
            print(f"  >> [StopLoss] {len(sl_orders)} position(s) fermee(s)")
            if tg_handler:
                lines = [f"🛑 Stop-loss : {len(sl_orders)} position(s) fermee(s)"]
                for o in sl_orders:
                    avg = trader.portfolio.realized_pnl  # juste pour référence
                    lines.append(f"  SELL {o.outcome} {o.shares:.1f}sh @ ${o.price:.4f}")
                tg_send("\n".join(lines))
    except Exception as e:
        print(f"  [WARN] auto_stop_loss : {e}")

    # 0b. Fermeture automatique des positions périmées (>72h)
    try:
        closed_orders = trader.auto_close_stale_positions()
        if closed_orders:
            print(f"  >> {len(closed_orders)} position(s) fermee(s) automatiquement")
            if tg_handler:
                freed = sum(o.price * o.shares for o in closed_orders)
                lines = [f"Auto-close {len(closed_orders)} position(s) perimee(s)"]
                for o in closed_orders:
                    lines.append(f"  SELL {o.outcome} {o.shares:.1f}sh @ ${o.price:.3f}")
                lines.append(f"  Cash libere : ${freed:.2f} USDC")
                tg_send("\n".join(lines))
    except Exception as e:
        print(f"  [WARN] auto_close_stale_positions : {e}")

    # 1. Snapshot wallets
    print("  >> Snapshot wallets...")
    try:
        snapshot = tracker.snapshot()
    except Exception as e:
        print(f"  [WARN] snapshot wallets impossible : {e} — cycle skipped")
        snapshot = {}

    # 2. Nouveaux trades — double méthode :
    #    a) Historique de trades  (marchés récents, souvent déjà résolus)
    #    b) Changements positions (marchés actifs, FIABLE)
    try:
        trades_from_history  = tracker.detect_new_trades(snapshot)
        trades_from_positions = tracker.detect_position_changes(snapshot)
        # Déduplique par conditionId+side+outcome
        seen_keys = set()
        new_trades = []
        for t in trades_from_history + trades_from_positions:
            k = f"{t.get('conditionId','')}|{t.get('side','')}|{t.get('outcome','')}"
            if k not in seen_keys:
                seen_keys.add(k)
                new_trades.append(t)
        src_h = len(trades_from_history)
        src_p = len(trades_from_positions)
        print(f"  >> {len(new_trades)} trade(s) detecte(s) (historique:{src_h} positions:{src_p})")
    except Exception as e:
        print(f"  [WARN] detect_new_trades : {e}")
        new_trades = []

    # 3. Marchés
    print("  >> Analyse des marches...")
    try:
        top_markets   = analyzer.get_top_markets(limit=BOT_CONFIG["top_markets_limit"])
        market_lookup = build_market_lookup(top_markets)
        print(f"  >> {len(top_markets)} marches qualifies")
    except Exception as e:
        print(f"  [WARN] get_top_markets : {e}")
        top_markets, market_lookup = [], {}

    # 4. Affichage (non-critique)
    try:
        tracker.display_summary(snapshot)
        analyzer.display_top(top_markets, top_n=BOT_CONFIG["top_markets_display"])
        mispriced = analyzer.find_mispriced(top_markets)
        if mispriced:
            print(f"\n  Marches potentiellement mal prices : {len(mispriced)}")
            for m in mispriced[:3]:
                print(f"    gap={m['price_gap']:.4f} | {m['question'][:60]}")
    except Exception as e:
        print(f"  [WARN] affichage : {e}")

    # 5. Copie
    executed_orders = []
    if new_trades:
        try:
            print(f"\n  >> Copie de {len(new_trades)} trade(s)...")
            executed_orders = trader.process_new_trades(new_trades, market_lookup)
            print(f"  >> {len(executed_orders)} ordre(s) simule(s)")
            if tg_handler:
                for order in executed_orders:
                    try:
                        notify_trade(order)
                    except Exception:
                        pass
        except Exception as e:
            print(f"  [WARN] process_new_trades : {e}")

    # 6. Portfolio (non-critique)
    try:
        trader.portfolio.display()
        trader.display_log(last_n=5)
    except Exception as e:
        print(f"  [WARN] display portfolio : {e}")

    # 7. Sauvegarde performance
    try:
        all_executed = trader.portfolio.total_orders_count - orders_before
        save_perf(perf, trader, cycle, snapshot,
                  new_trades=len(new_trades),
                  executed=len(executed_orders),
                  all_executed=all_executed,
                  top_markets=top_markets)
    except Exception as e:
        print(f"  [WARN] save_perf : {e}")

    # Résumé Telegram (non-critique)
    if tg_handler and executed_orders:
        try:
            notify_cycle(cycle, len(new_trades), len(executed_orders),
                         trader.portfolio.net_worth())
        except Exception:
            pass

    # 8. Nettoyage mémoire explicite après chaque cycle
    snapshot.clear()
    new_trades.clear()
    top_markets.clear()
    market_lookup.clear()
    executed_orders.clear()
    gc.collect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Copy Trading Bot")
    parser.add_argument("--live",   action="store_true", help="Active les ordres reels (DANGEREUX)")
    parser.add_argument("--cycles", type=int, default=0, help="Nombre de cycles (0 = infini)")
    args = parser.parse_args()

    dry_run = not args.live

    # Reset du state de trading — UNIQUEMENT via variable d'environnement Railway (jamais par le code).
    # Préserve net_worth_max et les métadonnées pour ne jamais perdre l'historique de performance.
    if os.environ.get("BOT_RESET", "").lower() in ("1", "true", "yes"):
        preserved_max = 0.0
        if os.path.exists(PERF_FILE):
            try:
                with open(PERF_FILE, "r", encoding="utf-8") as f:
                    old_perf = json.load(f)
                preserved_max = old_perf.get("meta", {}).get("net_worth_max", 0.0)
            except Exception:
                pass
        fresh = {
            "meta": {
                "started_at":    datetime.now(timezone.utc).isoformat(),
                "initial_balance": BOT_CONFIG["initial_balance"],
                "wallets":       list(WALLETS_TO_TRACK),
                "net_worth_max": preserved_max,   # jamais perdu
            },
            "cycles":  [],
            "summary": {},
        }
        with open(PERF_FILE, "w", encoding="utf-8") as f:
            json.dump(fresh, f, separators=(",", ":"), ensure_ascii=False)
        print(f"  >> [RESET] Portfolio remis a zero — net_worth_max=${preserved_max:.2f} preserve")

    # ── Serveur dashboard démarré EN PREMIER pour passer le health check Railway ──
    dashboard_port = int(os.environ.get("PORT", 8765))
    start_server_thread(dashboard_port)
    public_url = os.environ.get("PUBLIC_URL", f"http://localhost:{dashboard_port}")
    print(f"  >> Dashboard : {public_url}/dashboard.html")

    banner(dry_run)

    if not dry_run:
        confirm = input("\n  ATTENTION : mode LIVE active. Confirmer ? (yes/no) : ")
        if confirm.strip().lower() != "yes":
            print("  Annule.")
            sys.exit(0)

    tracker = WalletTracker(wallets=WALLETS_TO_TRACK)
    analyzer = MarketAnalyzer(
        min_volume_24h=BOT_CONFIG["min_volume_24h"],
        min_score=BOT_CONFIG["min_score"],
    )
    trader = CopyTrader(
        dry_run=dry_run,
        trade_size_usdc=BOT_CONFIG["trade_size_usdc"],
        max_positions=BOT_CONFIG["max_positions"],
        initial_balance=BOT_CONFIG["initial_balance"],
    )

    perf  = load_perf()
    cycle = perf.get("summary", {}).get("total_cycles", 0)

    # Wallets actifs : si performance.json en a sauvegardé → utilise UNIQUEMENT ceux-là.
    # Sinon → WALLETS_TO_TRACK comme fallback (premier démarrage ou après BOT_RESET).
    # Dans tous les cas, filtre EXCLUDED_WALLETS.
    excluded_lower = {w.lower() for w in EXCLUDED_WALLETS}
    saved_wallets  = perf["meta"].get("wallets")
    base_wallets   = saved_wallets if saved_wallets else list(WALLETS_TO_TRACK)
    active_wallets = [w for w in base_wallets if w.lower() not in excluded_lower]
    with tracker._wallets_lock:
        tracker.wallets = active_wallets
    perf["meta"]["wallets"] = active_wallets
    src_label = "sauvegardes" if saved_wallets else "WALLETS_TO_TRACK (fallback)"
    excl = len(base_wallets) - len(active_wallets)
    print(f"  >> Wallets actifs : {len(active_wallets)} ({src_label}"
          f"{f', {excl} exclu(s)' if excl else ''})")

    # Restaure les positions depuis le dernier cycle sauvegardé
    _restore_portfolio(trader, perf)

    # Restaure _last_trades du WalletTracker — empêche les faux "nouveaux trades"
    # causés par la réinitialisation de _last_trades à chaque restart
    saved_wallet_trades = perf.get("_last_wallet_trades", {})
    if saved_wallet_trades:
        tracker._last_trades = {w: list(trades) for w, trades in saved_wallet_trades.items()}
        n = sum(len(v) for v in tracker._last_trades.values())
        print(f"  >> Trades wallets restaures : {n} entrees ({len(tracker._last_trades)} wallets)")

    stop_event = threading.Event()
    started_at = datetime.now(timezone.utc)

    # Thread de rafraîchissement des prix (toutes les 5 min)
    price_thread = threading.Thread(
        target=refresh_position_prices,
        args=(trader, perf, stop_event, 300),
        daemon=True,
        name="price-refresher",
    )
    price_thread.start()

    # Thread de sélection automatique des wallets (toutes les heures)
    leaderboard_thread = threading.Thread(
        target=leaderboard_refresh_loop,
        args=(tracker, perf, stop_event),
        kwargs={"tg_send": tg_send, "excluded_wallets": EXCLUDED_WALLETS},
        daemon=True,
        name="leaderboard-selector",
    )
    leaderboard_thread.start()
    print("  >> Thread leaderboard-selector demarre (scan toutes les 1h)")

    # ── Gestionnaire SIGTERM (Railway arrête proprement avant de tuer) ──────────
    def _handle_sigterm(signum, frame):
        print("\n  [SIGTERM] Signal reçu — arrêt propre demandé par Railway")
        stop_event.set()

    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except Exception:
        pass  # Windows ne supporte pas tous les signaux

    tg_handler = TelegramCommandHandler(trader, stop_event, started_at, price_cache=_price_cache)
    tg_handler.start()
    notify_start(dry_run, len(WALLETS_TO_TRACK))

    global _consecutive_errors

    try:
        while not stop_event.is_set():
            cycle += 1
            cycle_ok = False
            try:
                run_cycle(tracker, analyzer, trader, cycle, perf, tg_handler)
                cycle_ok = True
                _consecutive_errors = 0
            except Exception as e:
                _consecutive_errors += 1
                print(f"  [ERREUR cycle #{cycle}] ({_consecutive_errors} consécutives) {e}")
                traceback.print_exc()

            if args.cycles and cycle >= args.cycles:
                print(f"\nNombre de cycles atteint ({args.cycles}). Arret.")
                break

            if stop_event.is_set():
                break

            # Backoff exponentiel si erreurs répétées (max 5 min)
            if not cycle_ok and _consecutive_errors > 0:
                backoff = min(30 * (2 ** (_consecutive_errors - 1)), 300)
                print(f"  >> Backoff {backoff}s avant prochain cycle (erreur #{_consecutive_errors})")
                stop_event.wait(timeout=backoff)
            else:
                print(f"\n  Prochain cycle dans {BOT_CONFIG['poll_interval_sec']}s...")
                stop_event.wait(timeout=BOT_CONFIG["poll_interval_sec"])

    except KeyboardInterrupt:
        pass
    except Exception as e:
        # Filet de sécurité final — ne devrait jamais arriver
        print(f"  [FATAL] Erreur hors boucle : {e}")
        traceback.print_exc()
    finally:
        tg_handler.stop()
        print("\n\n  Bot arrete.")
        try:
            trader.portfolio.display()
        except Exception:
            pass
        print(f"\n  Total ordres simules : {trader.portfolio.total_orders_count}")
        print(f"  Performances sauvegardees dans : {PERF_FILE}")
        try:
            notify_stop(trader.portfolio.total_orders_count, trader.portfolio.net_worth())
        except Exception:
            pass


if __name__ == "__main__":
    main()
