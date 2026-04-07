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

from wallet_tracker     import WalletTracker
from market_analyzer    import MarketAnalyzer
from copytrader         import CopyTrader
from serve              import start_server_thread
from telegram_notifier  import (
    TelegramCommandHandler, notify_start, notify_stop,
    notify_trade, notify_cycle, _send as tg_send,
)

# ── Configuration ─────────────────────────────────────────────────────────────

# Top 3 du leaderboard Polymarket par profit (30j) — limité à 3 pour réduire l'empreinte mémoire
WALLETS_TO_TRACK = [
    "0x492442eab586f242b53bda933fd5de859c8a3782",  # #1  +$5,734,027
    "0x02227b8f5a9636e895607edd3185ed6ee5598ff7",  # #2  +$4,016,108  HorizonSplendidView
    "0xefbc5fec8d7b0acdc8911bdd9a98d6964308f9a2",  # #3  +$3,742,635  reachingthesky
]

BOT_CONFIG = {
    "poll_interval_sec":  60,
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
MAX_CYCLES_IN_MEMORY  = 200        # cap anti-OOM : garde uniquement les N derniers cycles
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
            # Toujours synchroniser wallets et initial_balance avec la config actuelle
            data.setdefault("meta", {})["wallets"] = WALLETS_TO_TRACK
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
              top_markets: list) -> None:
    """Construit et sauvegarde l'entrée de performance du cycle courant."""
    now = datetime.now(timezone.utc).isoformat()
    portfolio = trader.portfolio

    # Snapshot wallets
    wallets_state = {}
    for wallet, wdata in snapshot.items():
        pnl = wdata.get("pnl", {})
        wallets_state[wallet] = {
            "open_positions": len(wdata.get("positions", [])),
            "pnl":    pnl.get("profit", None),
            "volume": pnl.get("volume", None),
        }

    # Top 10 marchés
    top5 = [
        {
            "question":   m["question"],
            "yes_price":  m["yes_price"],
            "volume_24h": m["volume_24h"],
            "score":      m["score"],
        }
        for m in top_markets[:5]  # réduit de 10 → 5 pour économiser mémoire
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

    # Derniers ordres exécutés
    last_orders = [
        {
            "order_id":  o.order_id,
            "side":      o.side,
            "outcome":   o.outcome,
            "price":     o.price,
            "size_usdc": o.size_usdc,
            "shares":    o.shares,
            "market_id": o.market_id,
            "source":    o.wallet_source[:20] if o.wallet_source else "",
            "timestamp": o.timestamp,
        }
        for o in portfolio.order_log[-executed:] if executed > 0
    ]

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

    # Résumé global mis à jour
    data["summary"] = {
        "last_update":      now,
        "total_cycles":     cycle,
        "total_orders":     portfolio.total_orders_count,
        "net_worth":        portfolio.net_worth(),
        "cash_usdc":        round(portfolio.balance_usdc, 4),
        "realized_pnl":     round(portfolio.realized_pnl, 4),
        "return_pct":       round(
            (portfolio.net_worth() - BOT_CONFIG["initial_balance"])
            / BOT_CONFIG["initial_balance"] * 100, 4
        ),
        "open_positions":   len(portfolio.positions),
        "best_cycle_orders": max(
            (c["orders_executed"] for c in data["cycles"]), default=0
        ),
    }

    # Écriture atomique avec protection totale contre les erreurs I/O
    try:
        with _PERF_LOCK:
            tmp = PERF_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, PERF_FILE)
        print(f"  >> performance.json mis a jour (cycle #{cycle}, net_worth=${portfolio.net_worth():.2f})")
    except Exception as e:
        print(f"  [WARN] Impossible d'ecrire performance.json : {e}")


def _restore_portfolio(trader: "CopyTrader", perf: dict) -> None:
    """Restaure les positions et le cash depuis le dernier cycle sauvegardé."""
    cycles = perf.get("cycles", [])
    if not cycles:
        return
    last = cycles[-1]
    port = last.get("portfolio", {})
    positions = last.get("open_positions", [])

    if not positions:
        return

    # Restaure le cash et le compteur d'ordres
    saved_cash = port.get("cash_usdc", BOT_CONFIG["initial_balance"])
    trader.portfolio.balance_usdc = saved_cash
    trader.portfolio.realized_pnl = port.get("realized_pnl", 0.0)
    trader.portfolio.total_orders_count = perf.get("summary", {}).get("total_orders", 0)

    # Restaure les positions (avec opened_at si présent)
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

        # 3. Sauvegarde atomique
        tmp = PERF_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(perf, f, indent=2, ensure_ascii=False)
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

    # 0. Fermeture automatique des positions périmées (>24h)
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

    # 2. Nouveaux trades
    try:
        new_trades = tracker.detect_new_trades(snapshot)
        print(f"  >> {len(new_trades)} nouveau(x) trade(s) detecte(s)")
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
        save_perf(perf, trader, cycle, snapshot,
                  new_trades=len(new_trades),
                  executed=len(executed_orders),
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
    parser.add_argument("--live", action="store_true", help="Active les ordres reels (DANGEREUX)")
    parser.add_argument("--cycles", type=int, default=0, help="Nombre de cycles (0 = infini)")
    args = parser.parse_args()

    dry_run = not args.live

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

    # Restaure les positions depuis le dernier cycle sauvegardé
    _restore_portfolio(trader, perf)

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

    # ── Gestionnaire SIGTERM (Railway arrête proprement avant de tuer) ──────────
    def _handle_sigterm(signum, frame):
        print("\n  [SIGTERM] Signal reçu — arrêt propre demandé par Railway")
        stop_event.set()

    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except Exception:
        pass  # Windows ne supporte pas tous les signaux

    tg_handler = TelegramCommandHandler(trader, stop_event, started_at)
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
