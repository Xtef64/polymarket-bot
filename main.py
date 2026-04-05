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
from datetime import datetime, timezone

# Force UTF-8 sur stdout/stderr (Windows cp1252 ne supporte pas les box-drawing chars)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
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
    # ── Top traders Polymarket leaderboard — PnL positif 30j (source: polymarket.com/leaderboard)
    "0x492442eab586f242b53bda933fd5de859c8a3782",  # anonyme              | PnL30j=+$5 920 735  [#1]
    "0x02227b8f5a9636e895607edd3185ed6ee5598ff7",  # HorizonSplendidView  | PnL30j=+$4 016 108  [#2]
    "0xefbc5fec8d7b0acdc8911bdd9a98d6964308f9a2",  # reachingthesky       | PnL30j=+$3 742 635  [#3]
    "0xc2e7800b5af46e6093872b177b7a5e7f0563be51",  # beachboy4            | PnL30j=+$3 179 491  [#4]
    "0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1",  # anonyme              | PnL30j=+$2 572 124  [#5]
    "0x019782cab5d844f02bafb71f512758be78579f3c",  # majorexploiter       | PnL30j=+$2 416 975  [#6]
    "0xbddf61af533ff524d27154e589d2d7a81510c684",  # Countryside          | PnL30j=+$2 203 283  [#7]
    "0xb45a797faa52b0fd8adc56d30382022b7b12192c",  # bcda                 | PnL30j=+$1 780 507  [#8]
    "0x2005d16a84ceefa912d4e380cd32e7ff827875ea",  # RN1                  | PnL30j=+$1 746 768  [#9]
    "0xee613b3fc183ee44f9da9c05f53e2da107e3debf",  # sovereign2013        | PnL30j=+$1 728 688  [#10]
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

PERF_FILE = os.path.join(os.path.dirname(__file__), "performance.json")

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

    # Démarre le serveur dashboard en arrière-plan
    dashboard_port = int(os.environ.get("PORT", 8765))
    start_server_thread(dashboard_port)
    public_url = os.environ.get("PUBLIC_URL", f"http://localhost:{dashboard_port}")
    print(f"  >> Dashboard : {public_url}/dashboard.html")

    stop_event = threading.Event()
    started_at = datetime.now(timezone.utc)
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
