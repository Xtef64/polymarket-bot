"""
telegram_notifier.py - Alertes Telegram + commandes pour le bot Polymarket
"""

import os
import threading
import requests
import time
from datetime import datetime, timezone

CLOB_API     = "https://clob.polymarket.com"
TELEGRAM_API = "https://api.telegram.org"
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "8743436885:AAGVQ3OOGl_rJeyEoHyRVeIuAvFoB9qXi88")
CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID",   "6741061312")

COMMANDS_HELP = """🤖 <b>Polymarket Bot — Commandes</b>

📊 /status     – Résumé général du portfolio
📋 /positions  – Toutes les positions ouvertes avec PnL temps réel
🏆 /top        – Top 5 meilleures positions (gain potentiel)
💰 /pnl        – PnL réalisé vs non-réalisé détaillé
🛑 /stop       – Arrêter le bot proprement
▶️ /start      – Confirmer que le bot tourne
❓ /help       – Cette aide"""


def _send(text: str) -> None:
    """Envoie un message Telegram (silencieux en cas d'erreur)."""
    try:
        requests.post(
            f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception:
        pass


def notify_trade(order) -> None:
    icon = "🟢" if order.side == "BUY" else "🔴"
    _send(
        f"{icon} <b>[DRY RUN] Nouveau trade</b>\n"
        f"  {order.side} {order.outcome} @ ${order.price:.3f}\n"
        f"  {order.shares:.2f} shares · ${order.size_usdc:.2f} USDC\n"
        f"  Market: <code>{order.market_id[:16]}...</code>\n"
        f"  Source: <code>{order.wallet_source[:20] if order.wallet_source else '?'}</code>\n"
        f"  ID: {order.order_id}"
    )


def notify_cycle(cycle: int, new_trades: int, executed: int, net_worth: float) -> None:
    _send(
        f"🔄 <b>Cycle #{cycle}</b> terminé\n"
        f"  Trades détectés : {new_trades}\n"
        f"  Ordres simulés  : {executed}\n"
        f"  Net worth       : <b>${net_worth:,.2f}</b>"
    )


def notify_start(dry_run: bool, n_wallets: int) -> None:
    mode = "DRY RUN" if dry_run else "⚠️ LIVE"
    _send(
        f"🚀 <b>Bot démarré — {mode}</b>\n"
        f"  Wallets suivis : {n_wallets}\n"
        f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        + COMMANDS_HELP
    )


def notify_stop(total_orders: int, net_worth: float) -> None:
    _send(
        f"🛑 <b>Bot arrêté</b>\n"
        f"  Total ordres : {total_orders}\n"
        f"  Net worth    : <b>${net_worth:,.2f}</b>"
    )


# ── Commandes entrantes ───────────────────────────────────────────────────────

class TelegramCommandHandler:

    def __init__(self, trader, stop_event: threading.Event, started_at: datetime = None):
        self._trader     = trader
        self._stop_event = stop_event
        self._started_at = started_at or datetime.now(timezone.utc)
        self._offset     = 0
        self._thread     = None
        self._running    = False

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    # ── Boucle de polling ─────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        DISPATCH = {
            "/status":    self._cmd_status,
            "/positions": self._cmd_positions,
            "/top":       self._cmd_top,
            "/pnl":       self._cmd_pnl,
            "/stop":      self._cmd_stop,
            "/start":     self._cmd_start,
            "/help":      self._cmd_help,
        }
        while self._running and not self._stop_event.is_set():
            try:
                r = requests.get(
                    f"{TELEGRAM_API}/bot{BOT_TOKEN}/getUpdates",
                    params={"offset": self._offset, "timeout": 20},
                    timeout=25,
                )
                if r.status_code == 200:
                    for upd in r.json().get("result", []):
                        self._offset = upd["update_id"] + 1
                        text = upd.get("message", {}).get("text", "").strip().lower().split()[0]
                        fn = DISPATCH.get(text)
                        if fn:
                            fn()
            except Exception:
                time.sleep(5)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fetch_current_prices(self, token_ids: list) -> dict:
        prices = {}
        for tid in token_ids:
            try:
                r = requests.get(f"{CLOB_API}/midpoint", params={"token_id": tid}, timeout=5)
                if r.status_code == 200:
                    mid = r.json().get("mid")
                    if mid is not None:
                        prices[tid] = float(mid)
            except Exception:
                pass
        return prices

    def _positions_with_pnl(self) -> tuple[list, float, float]:
        """Retourne (rows, total_cost, total_value) avec prix temps réel."""
        p = self._trader.portfolio
        token_ids     = list(p.positions.keys())
        cur_prices    = self._fetch_current_prices(token_ids)
        rows          = []
        total_cost    = 0.0
        total_value   = 0.0
        for tid, pos in p.positions.items():
            avg  = pos["avg_cost"]
            cost = pos["total_cost"]
            sh   = pos["shares"]
            cur  = cur_prices.get(tid, avg)
            val  = sh * cur
            pnl  = val - cost
            pct  = pnl / cost * 100 if cost > 0 else 0
            rows.append({"pos": pos, "cur": cur, "val": val, "pnl": pnl, "pct": pct})
            total_cost  += cost
            total_value += val
        return rows, total_cost, total_value

    # ── Commandes ─────────────────────────────────────────────────────────────

    def _cmd_help(self) -> None:
        _send(COMMANDS_HELP)

    def _cmd_start(self) -> None:
        p   = self._trader.portfolio
        sec = int((datetime.now(timezone.utc) - self._started_at).total_seconds())
        h, m = divmod(sec // 60, 60)
        _send(
            f"▶️ <b>Bot en cours d'exécution</b>\n"
            f"  Démarré il y a : {h}h {m}m\n"
            f"  Positions ouvertes : {len(p.positions)}\n"
            f"  Net worth : <b>${p.net_worth():,.2f}</b>\n"
            f"  Mode : DRY RUN\n\n"
            + COMMANDS_HELP
        )

    def _cmd_status(self) -> None:
        p   = self._trader.portfolio
        sec = int((datetime.now(timezone.utc) - self._started_at).total_seconds())
        h, rem = divmod(sec, 3600)
        m, s   = divmod(rem, 60)
        rows, total_cost, total_value = self._positions_with_pnl()
        unrealized = total_value - total_cost
        unr_pct    = unrealized / total_cost * 100 if total_cost > 0 else 0
        _send(
            f"📊 <b>Status du portfolio</b>\n"
            f"  ⏱ Runtime      : {h}h {m}m {s}s\n"
            f"  💵 Cash         : <b>${p.balance_usdc:,.2f}</b> USDC\n"
            f"  📂 Positions    : {len(p.positions)} / 60\n"
            f"  📈 PnL réalisé  : <b>${p.realized_pnl:+,.2f}</b>\n"
            f"  📉 PnL latent   : <b>${unrealized:+,.2f} ({unr_pct:+.1f}%)</b>\n"
            f"  💼 Net worth    : <b>${p.net_worth():,.2f}</b>\n"
            f"  🔢 Total ordres : {len(p.order_log)}"
        )

    def _cmd_positions(self) -> None:
        p = self._trader.portfolio
        if not p.positions:
            _send("📭 Aucune position ouverte.")
            return
        rows, total_cost, total_value = self._positions_with_pnl()
        rows.sort(key=lambda x: x["pnl"], reverse=True)

        lines = [f"📋 <b>Positions ouvertes ({len(p.positions)} / 60)</b>\n"]
        for r in rows:
            pos  = r["pos"]
            icon = "🟢" if r["pnl"] >= 0 else "🔴"
            lines.append(
                f"{icon} <b>{pos['outcome']}</b>  {pos['shares']:.1f} sh\n"
                f"   ${pos['avg_cost']:.3f} → ${r['cur']:.3f}  "
                f"| coût ${pos['total_cost']:.2f}\n"
                f"   <b>{'+' if r['pnl']>=0 else ''}{r['pnl']:.2f}$ ({r['pct']:+.1f}%)</b>"
            )

        total_pnl = total_value - total_cost
        total_pct = total_pnl / total_cost * 100 if total_cost > 0 else 0
        lines.append(
            f"\n💼 <b>TOTAL</b>  coût ${total_cost:.2f} | mtm ${total_value:.2f}\n"
            f"   PnL latent : <b>{'+' if total_pnl>=0 else ''}{total_pnl:.2f}$ ({total_pct:+.1f}%)</b>"
        )

        msg = "\n".join(lines)
        for i in range(0, len(msg), 4000):
            _send(msg[i:i+4000])

    def _cmd_top(self) -> None:
        p = self._trader.portfolio
        if not p.positions:
            _send("📭 Aucune position ouverte.")
            return
        rows, _, _ = self._positions_with_pnl()
        rows.sort(key=lambda x: x["pnl"], reverse=True)
        top5 = rows[:5]

        lines = ["🏆 <b>Top 5 meilleures positions</b>\n"]
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, r in enumerate(top5):
            pos = r["pos"]
            lines.append(
                f"{medals[i]} <b>{pos['outcome']}</b>  {pos['shares']:.1f} sh\n"
                f"   ${pos['avg_cost']:.3f} → ${r['cur']:.3f}\n"
                f"   Gain : <b>+{r['pnl']:.2f}$ (+{r['pct']:.1f}%)</b>"
            )

        # Pires positions aussi
        worst3 = sorted(rows, key=lambda x: x["pnl"])[:3]
        lines.append("\n⚠️ <b>3 pires positions</b>")
        for r in worst3:
            pos = r["pos"]
            lines.append(
                f"🔴 <b>{pos['outcome']}</b>  {pos['shares']:.1f} sh  "
                f"{r['pnl']:.2f}$ ({r['pct']:+.1f}%)"
            )

        _send("\n".join(lines))

    def _cmd_pnl(self) -> None:
        p = self._trader.portfolio
        rows, total_cost, total_value = self._positions_with_pnl()

        unrealized     = total_value - total_cost
        unr_pct        = unrealized / total_cost * 100 if total_cost > 0 else 0
        realized       = p.realized_pnl
        total_pnl      = realized + unrealized
        initial        = total_cost + p.balance_usdc  # approximation
        winners        = sum(1 for r in rows if r["pnl"] >= 0)
        losers         = len(rows) - winners
        win_rate       = winners / len(rows) * 100 if rows else 0

        _send(
            f"💰 <b>PnL détaillé</b>\n\n"
            f"  ✅ PnL réalisé   : <b>${realized:+,.2f}</b>\n"
            f"  📉 PnL latent    : <b>${unrealized:+,.2f} ({unr_pct:+.1f}%)</b>\n"
            f"  💼 PnL total     : <b>${total_pnl:+,.2f}</b>\n\n"
            f"  💵 Cash restant  : ${p.balance_usdc:,.2f} USDC\n"
            f"  📦 Capital investi: ${total_cost:,.2f}\n"
            f"  📊 Valeur mtm    : ${total_value:,.2f}\n\n"
            f"  🟢 Positions +   : {winners}\n"
            f"  🔴 Positions −   : {losers}\n"
            f"  🎯 Win rate latent: {win_rate:.1f}%\n"
            f"  🔢 Total ordres  : {len(p.order_log)}"
        )

    def _cmd_stop(self) -> None:
        _send("🛑 <b>Arrêt demandé via Telegram…</b>\nLe bot s'arrête après le cycle en cours.")
        self._stop_event.set()
