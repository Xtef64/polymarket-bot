"""
main.py - Orchestrateur du bot Polymarket
Usage : python main.py [--live]  (dry run par défaut)
"""

import sys
import time
import json
import argparse
import os
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

WALLETS_TO_TRACK = [
    # ── Top traders Polymarket leaderboard — PnL réalisé POSITIF 30j vérifié
    # Source : polymarket.com/leaderboard — tous > +$500 000 sur 30 jours
    "0xdc876e6873772d38716fda7f2452a78d426d7ab6",  # anon-dc87            | PnL30j=+$1 495 977  [#11]
    "0xf195721ad850377c96cd634457c70cd9e8308057",  # anon-f195            | PnL30j=+$1 459 819  [#12]
    "0x93abbc022ce98d6f45d4444b594791cc4b7a9723",  # anon-93ab            | PnL30j=+$1 295 513  [#13]
    "0x59a0744db1f39ff3afccd175f80e6e8dfc239a09",  # anon-59a0            | PnL30j=+$1 202 926  [#14]
    "0x8f037a2e4fd49d11267f4ab874ab7ba745ac64d6",  # anon-8f03            | PnL30j=+$1 185 569  [#15]
    "0x50b1db131a24a9d9450bbd0372a95d32ea88f076",  # anon-50b1            | PnL30j=+$1 126 032  [#16]
    "0x204f72f35326db932158cba6adff0b9a1da95e14",  # swisstony            | PnL30j=+$1 089 278  [#17]
    "0x8c80d213c0cbad777d06ee3f58f6ca4bc03102c3",  # anon-8c80            | PnL30j=+$  951 431  [#18]
    "0xb6d6e99d3bfe055874a04279f659f009fd57be17",  # anon-b6d6            | PnL30j=+$  887 474  [#19]
    "0x07921379f7b31ef93da634b688b2fe36897db778",  # anon-0792            | PnL30j=+$  879 527  [#20]
    "0x492442eab586f242b53bda933fd5de859c8a3782",  # anon-top1            | PnL30j=+$5 920 735  [#1]
    "0x02227b8f5a9636e895607edd3185ed6ee5598ff7",  # HorizonSplendidView  | PnL30j=+$4 016 108  [#2]
    "0xefbc5fec8d7b0acdc8911bdd9a98d6964308f9a2",  # reachingthesky       | PnL30j=+$3 742 635  [#3]
    "0xc2e7800b5af46e6093872b177b7a5e7f0563be51",  # beachboy4            | PnL30j=+$3 179 491  [#4]
    "0xbddf61af533ff524d27154e589d2d7a81510c684",  # Countryside          | PnL30j=+$2 203 283  [#7]
]

BOT_CONFIG = {
    "poll_interval_sec":  60,
    "top_markets_limit":  50,
    "top_markets_display": 10,
    "trade_size_usdc":    10.0,
    "initial_balance":  1_000.0,
    "max_positions":      80,
    "min_volume_24h":  5_000.0,
    "min_score":          4.0,
}

PERF_FILE  = os.path.join(os.path.dirname(__file__), "performance.json")
_PERF_LOCK = threading.Lock()   # protège perf dict + écriture fichier

# ──────────────────────────────────────────────────────────────────────────────


def load_perf() -> dict:
    """Charge le fichier performance.json existant, ou retourne une structure vide."""
    if os.path.exists(PERF_FILE):
        try:
            with open(PERF_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Toujours synchroniser la liste des wallets avec la config actuelle
            data.setdefault("meta", {})["wallets"] = WALLETS_TO_TRACK
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
        for m in top_markets[:10]
    ]

    # Positions ouvertes (avec market_id et opened_at pour auto-close)
    open_pos = [
        {
            "token_id":   tid,
            "market_id":  p.get("market_id", ""),
            "outcome":    p["outcome"],
            "shares":     p["shares"],
            "avg_cost":   p["avg_cost"],
            "total_cost": p["total_cost"],
            "opened_at":  p.get("opened_at", ""),
        }
        for tid, p in portfolio.positions.items()
    ]

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
            "total_orders":   len(portfolio.order_log),
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

    # Résumé global mis à jour
    data["summary"] = {
        "last_update":      now,
        "total_cycles":     cycle,
        "total_orders":     len(portfolio.order_log),
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

    # Écriture atomique : écrit dans un fichier temporaire puis renomme
    # pour éviter la corruption en cas d'exécutions concurrentes
    with _PERF_LOCK:
        tmp = PERF_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, PERF_FILE)

    print(f"  >> performance.json mis a jour (cycle #{cycle}, net_worth=${portfolio.net_worth():.2f})")


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

    # Restaure le cash
    saved_cash = port.get("cash_usdc", BOT_CONFIG["initial_balance"])
    trader.portfolio.balance_usdc = saved_cash
    trader.portfolio.realized_pnl = port.get("realized_pnl", 0.0)

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


def refresh_position_prices(trader: "CopyTrader", perf: dict,
                            stop_event: threading.Event,
                            interval: int = 300) -> None:
    """Thread : rafraîchit les prix courants des positions toutes les 5 min
    depuis l'API CLOB Polymarket et recalcule le PnL latent réel dans performance.json."""
    CLOB_API = "https://clob.polymarket.com"

    # Attendre que le premier cycle ait sauvegardé des positions (~90s)
    stop_event.wait(timeout=90)

    while not stop_event.is_set():
        try:
            # Copie thread-safe des token_ids courants
            with _PERF_LOCK:
                token_ids = list(trader.portfolio.positions.keys())

            if token_ids:
                # Récupération des prix hors verrou (appels réseau)
                current_prices: dict = {}
                for tid in token_ids:
                    try:
                        r = requests.get(
                            f"{CLOB_API}/midpoint",
                            params={"token_id": tid},
                            timeout=5,
                        )
                        if r.status_code == 200:
                            mid = r.json().get("mid")
                            if mid is not None:
                                current_prices[tid] = float(mid)
                    except Exception:
                        pass

                if current_prices:
                    now = datetime.now(timezone.utc).isoformat()
                    total_cost  = 0.0
                    total_value = 0.0

                    with _PERF_LOCK:
                        if perf.get("cycles"):
                            last_cycle = perf["cycles"][-1]
                            updated_positions = []
                            for p in last_cycle.get("open_positions", []):
                                tid       = p.get("token_id", "")
                                avg_cost  = p.get("avg_cost", 0.0)
                                # Utilise le prix précédent si l'API n'a pas répondu pour ce token
                                cur_price = current_prices.get(
                                    tid, p.get("current_price", avg_cost)
                                )
                                shares    = p.get("shares", 0.0)
                                cost      = p.get("total_cost", 0.0)
                                cur_value = shares * cur_price
                                unr_pnl   = cur_value - cost
                                pnl_pct   = unr_pnl / cost * 100 if cost > 0 else 0.0

                                entry = dict(p)
                                entry["current_price"]  = round(cur_price,  4)
                                entry["current_value"]  = round(cur_value,  4)
                                entry["unrealized_pnl"] = round(unr_pnl,    4)
                                entry["pnl_pct"]        = round(pnl_pct,    2)
                                updated_positions.append(entry)
                                total_cost  += cost
                                total_value += cur_value

                            last_cycle["open_positions"]    = updated_positions
                            last_cycle["prices_updated_at"] = now

                        unrealized_total = total_value - total_cost
                        perf["summary"]["unrealized_pnl"]    = round(unrealized_total, 4)
                        perf["summary"]["prices_updated_at"] = now

                        # Sauvegarde atomique
                        tmp = PERF_FILE + ".tmp"
                        with open(tmp, "w", encoding="utf-8") as f:
                            json.dump(perf, f, indent=2, ensure_ascii=False)
                        os.replace(tmp, PERF_FILE)

                    print(
                        f"  >> [price_refresh] {len(current_prices)}/{len(token_ids)} prix mis a jour "
                        f"| PnL latent total : ${unrealized_total:+.2f}"
                    )
                else:
                    print("  [price_refresh] Aucun prix reçu depuis l'API CLOB")

        except Exception as e:
            print(f"  [price_refresh] Erreur : {e}")

        # Attendre avant le prochain refresh (interruptible)
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
    closed_orders = trader.auto_close_stale_positions()
    if closed_orders:
        print(f"  >> {len(closed_orders)} position(s) fermee(s) automatiquement")
        if tg_handler:
            freed = sum(o.price * o.shares for o in closed_orders)
            lines = [f"♻️ <b>Auto-close {len(closed_orders)} position(s) périmée(s)</b>"]
            for o in closed_orders:
                lines.append(f"  {'🟢' if o.price >= 0 else '🔴'} SELL {o.outcome} {o.shares:.1f}sh @ ${o.price:.3f}")
            lines.append(f"  Cash libéré : <b>${freed:.2f} USDC</b>")
            lines.append(f"  Cash total  : <b>${trader.portfolio.balance_usdc:.2f} USDC</b>")
            tg_send("\n".join(lines))

    # 1. Snapshot wallets
    print("  >> Snapshot wallets...")
    snapshot = tracker.snapshot()

    # 2. Nouveaux trades
    new_trades = tracker.detect_new_trades(snapshot)
    print(f"  >> {len(new_trades)} nouveau(x) trade(s) detecte(s)")

    # 3. Marchés
    print("  >> Analyse des marches...")
    top_markets   = analyzer.get_top_markets(limit=BOT_CONFIG["top_markets_limit"])
    market_lookup = build_market_lookup(top_markets)
    print(f"  >> {len(top_markets)} marches qualifies")

    # 4. Affichage
    tracker.display_summary(snapshot)
    analyzer.display_top(top_markets, top_n=BOT_CONFIG["top_markets_display"])

    mispriced = analyzer.find_mispriced(top_markets)
    if mispriced:
        print(f"\n  Marches potentiellement mal prices : {len(mispriced)}")
        for m in mispriced[:5]:
            print(
                f"    gap={m['price_gap']:.4f} | YES={m['yes_price']:.3f} "
                f"NO={m['no_price']:.3f} | {m['question']}"
            )

    # 5. Copie
    executed_orders = []
    if new_trades:
        print(f"\n  >> Copie de {len(new_trades)} trade(s)...")
        executed_orders = trader.process_new_trades(new_trades, market_lookup)
        print(f"  >> {len(executed_orders)} ordre(s) simule(s)")
        # Alertes Telegram pour chaque ordre simulé
        if tg_handler:
            for order in executed_orders:
                notify_trade(order)

    # 6. Portfolio
    trader.portfolio.display()
    trader.display_log(last_n=5)

    # 7. Sauvegarde performance
    save_perf(perf, trader, cycle, snapshot,
              new_trades=len(new_trades),
              executed=len(executed_orders),
              top_markets=top_markets)

    # Résumé Telegram de fin de cycle (seulement si des trades ont eu lieu)
    if tg_handler and executed_orders:
        notify_cycle(cycle, len(new_trades), len(executed_orders),
                     trader.portfolio.net_worth())


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

    tg_handler = TelegramCommandHandler(trader, stop_event, started_at)
    tg_handler.start()
    notify_start(dry_run, len(WALLETS_TO_TRACK))

    try:
        while not stop_event.is_set():
            cycle += 1
            try:
                run_cycle(tracker, analyzer, trader, cycle, perf, tg_handler)
            except Exception as e:
                print(f"  [ERREUR cycle #{cycle}] {e}")
                traceback.print_exc()

            if args.cycles and cycle >= args.cycles:
                print(f"\nNombre de cycles atteint ({args.cycles}). Arret.")
                break

            if stop_event.is_set():
                break

            print(f"\n  Prochain cycle dans {BOT_CONFIG['poll_interval_sec']}s... (Ctrl+C pour arreter)")
            stop_event.wait(timeout=BOT_CONFIG["poll_interval_sec"])

    except KeyboardInterrupt:
        pass
    finally:
        tg_handler.stop()
        print("\n\n  Bot arrete par l'utilisateur.")
        trader.portfolio.display()
        print(f"\n  Total ordres simules : {len(trader.portfolio.order_log)}")
        print(f"  Performances sauvegardees dans : {PERF_FILE}")
        notify_stop(len(trader.portfolio.order_log), trader.portfolio.net_worth())


if __name__ == "__main__":
    main()
