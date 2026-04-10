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

# Sessions persistantes — réutilise les connexions TCP
_tg_session   = requests.Session()
_clob_session = requests.Session()
_clob_session.headers.update({"Accept": "application/json", "User-Agent": "polymarket-bot/1.0"})

GAMMA_API          = "https://gamma-api.polymarket.com"
POLYMARKET_BASE    = "https://polymarket.com/market"
_market_cache: dict = {}   # conditionId → {"question": str, "end_date": str|None, "slug": str|None}


def _fetch_market_info(condition_id: str) -> dict:
    """Appelle Gamma API et retourne {"question": ..., "end_date": ..., "slug": ...}.
    Met le résultat en cache. Retourne des valeurs vides si indisponible."""
    if condition_id in _market_cache:
        return _market_cache[condition_id]
    info = {"question": "", "end_date": None, "slug": None}
    try:
        r = _clob_session.get(
            f"{GAMMA_API}/markets",
            params={"condition_id": condition_id},
            timeout=5,
        )
        if r.status_code == 200:
            data = r.json()
            r.close()
            if isinstance(data, list) and data:
                m = data[0]
                # Vérifie que le marché retourné correspond bien à l'ID demandé.
                # Si la Gamma API reçoit un ID invalide (ex: token_id décimal),
                # elle retourne tous les marchés par défaut — data[0] serait alors
                # un marché aléatoire (bug "Russia-Ukraine pour un trade tennis").
                returned_cid = (m.get("conditionId") or "").lower()
                if returned_cid != condition_id.lower():
                    print(f"  [market_cache] ID non concordant — demandé={condition_id[:14]}… retourné={returned_cid[:14]}… (skipped)")
                else:
                    info["question"] = m.get("question", "") or ""
                    info["end_date"]  = m.get("endDate") or None
                    info["slug"]      = m.get("slug") or None
                    if info["question"]:          # ne cache que les succès complets
                        _market_cache[condition_id] = info
        else:
            r.close()
    except Exception:
        pass
    return info


def _get_market_name(condition_id: str, max_len: int = 55) -> str:
    """Retourne le nom du marché (tronqué si besoin). Fallback sur ID court."""
    if not condition_id:
        return "?"
    name = _fetch_market_info(condition_id).get("question", "")
    if not name:
        return f"{condition_id[:10]}…"
    return name[:max_len] + "…" if len(name) > max_len else name


def _get_market_link(condition_id: str, max_len: int = 50) -> str:
    """Retourne un lien HTML cliquable vers la page Polymarket du marché.
    Fallback sur le nom seul si le slug est indisponible."""
    if not condition_id:
        return "?"
    info  = _fetch_market_info(condition_id)
    name  = info.get("question", "") or f"{condition_id[:10]}…"
    label = (name[:max_len] + "…") if len(name) > max_len else name
    slug  = info.get("slug")
    if slug:
        return f'<a href="{POLYMARKET_BASE}/{slug}">{label}</a>'
    return label


def _fmt_resolution(condition_id: str) -> str:
    """Retourne une chaîne 'Résolution dans Xh Ym (HH:MM UTC)' ou '' si inconnue."""
    end_date_str = _fetch_market_info(condition_id).get("end_date")
    if not end_date_str:
        return ""
    try:
        end_dt  = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        now     = datetime.now(timezone.utc)
        delta   = end_dt - now
        total_s = int(delta.total_seconds())
        if total_s <= 0:
            return "⚠️ Marché déjà résolu"
        h, rem = divmod(total_s, 3600)
        m      = rem // 60
        hhmm   = end_dt.strftime("%H:%M")
        date_s = end_dt.strftime("%Y-%m-%d")
        if h >= 24:
            jours = h // 24
            return f"⏳ Résolution dans {jours}j {h % 24}h ({date_s} {hhmm} UTC)"
        return f"⏳ Résolution dans {h}h {m:02d}m ({hhmm} UTC)"
    except Exception:
        return ""

COMMANDS_HELP = """🤖 <b>Polymarket Bot — Commandes</b>

📊 /status     – Résumé général du portfolio
📋 /positions  – Toutes les positions numérotées avec PnL temps réel
🏆 /top        – Top 5 meilleures positions (gain potentiel)
💰 /pnl        – PnL réalisé vs non-réalisé détaillé
❌ /close1 /close2 … – Ferme la position numérotée
🔥 /closeall   – Ferme toutes les positions ouvertes
🛑 /stop       – Arrêter le bot proprement
▶️ /start      – Confirmer que le bot tourne
❓ /help       – Cette aide"""


def _send(text: str) -> None:
    """Envoie un message Telegram (silencieux en cas d'erreur)."""
    try:
        _tg_session.post(
            f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception:
        pass


def _flush_pending_updates() -> int:
    """Vide la queue Telegram des anciens updates (évite de rejouer /stop d'une session précédente).
    Retourne le prochain offset à utiliser."""
    try:
        r = _tg_session.get(
            f"{TELEGRAM_API}/bot{BOT_TOKEN}/getUpdates",
            params={"offset": -1, "timeout": 0},  # offset=-1 = dernier update seulement
            timeout=10,
        )
        if r.status_code == 200:
            results = r.json().get("result", [])
            if results:
                latest_id = results[-1]["update_id"]
                print(f"  [Telegram] Flush {latest_id + 1} (skip updates anciens)")
                return latest_id + 1
    except Exception:
        pass
    return 0


def notify_trade(order) -> None:
    icon        = "🟢" if order.side == "BUY" else "🔴"
    market_name = _get_market_name(order.market_id)
    resolution  = _fmt_resolution(order.market_id)
    resol_line  = f"\n  {resolution}" if resolution else ""
    _send(
        f"{icon} <b>[DRY RUN] Nouveau trade</b>\n"
        f"  {order.side} {order.outcome} @ ${order.price:.3f}\n"
        f"  {order.shares:.2f} shares · ${order.size_usdc:.2f} USDC\n"
        f"  📌 {market_name}{resol_line}\n"
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

    def __init__(self, trader, stop_event: threading.Event, started_at: datetime = None,
                 price_cache: dict = None):
        self._trader          = trader
        self._stop_event      = stop_event
        self._started_at      = started_at or datetime.now(timezone.utc)
        self._price_cache     = price_cache if price_cache is not None else {}
        self._offset          = 0
        self._thread          = None
        self._running         = False
        self._position_index: dict[int, str] = {}  # numéro → token_id

    def start(self) -> None:
        self._running = True
        # Flush les anciens updates pour éviter de rejouer /stop d'une session précédente
        self._offset = _flush_pending_updates()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    # ── Boucle de polling ─────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        import re
        DISPATCH = {
            "/status":    self._cmd_status,
            "/positions": self._cmd_positions,
            "/top":       self._cmd_top,
            "/pnl":       self._cmd_pnl,
            "/stop":      self._cmd_stop,
            "/start":     self._cmd_start,
            "/help":      self._cmd_help,
            "/ping":      self._cmd_ping,
            "/closeall":  self._cmd_closeall,
        }
        print("  [Telegram] Poll loop demarre (intervalle 2s, pas de long-polling)")
        while self._running and not self._stop_event.is_set():
            try:
                r = _tg_session.get(
                    f"{TELEGRAM_API}/bot{BOT_TOKEN}/getUpdates",
                    params={"offset": self._offset, "timeout": 0},  # pas de long-polling
                    timeout=5,  # coupe net si pas de réponse en 5s
                )
                if r.status_code != 200:
                    print(f"  [Telegram] getUpdates HTTP {r.status_code}")
                    r.close()
                    time.sleep(5)
                    continue
                updates = r.json().get("result", [])
                r.close()
                for upd in updates:
                    self._offset = upd["update_id"] + 1
                    msg  = upd.get("message") or upd.get("edited_message") or {}
                    raw  = (msg.get("text") or "").strip()
                    if not raw:
                        continue
                    # Accepte "/cmd" et "/cmd@botname"
                    cmd = raw.lower().split()[0].split("@")[0]
                    print(f"  [Telegram] >>> commande : '{cmd}' (update_id={upd['update_id']})")
                    fn = DISPATCH.get(cmd)
                    if fn:
                        self._safe_run(fn)
                    else:
                        # Commandes dynamiques /close1, /close2, ...
                        m = re.fullmatch(r"/close(\d+)", cmd)
                        if m:
                            n = int(m.group(1))
                            self._safe_run(lambda n=n: self._cmd_close_position(n))
                        else:
                            print(f"  [Telegram] commande inconnue ignoree : {cmd}")
            except requests.exceptions.Timeout:
                print("  [Telegram] Timeout getUpdates — retry")
            except Exception as e:
                print(f"  [Telegram] Erreur poll : {type(e).__name__}: {e}")
                time.sleep(5)
            # Pause fixe de 2s entre chaque poll
            time.sleep(2)

    def _safe_run(self, fn) -> None:
        """Exécute une commande en isolant les erreurs pour ne pas crasher le thread."""
        try:
            fn()
        except Exception as e:
            print(f"  [Telegram] Erreur commande : {e}")
            try:
                _send(f"Erreur interne : {e}")
            except Exception:
                pass

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fetch_current_prices(self, token_ids: list) -> dict:
        """Retourne les prix courants. Utilise _price_cache en priorité (déjà rafraîchi
        toutes les 5 min par le price-refresher) — appel CLOB seulement si manquant."""
        prices = {}
        missing = []
        for tid in token_ids:
            cached = self._price_cache.get(tid)
            if cached is not None:
                prices[tid] = cached
            else:
                missing.append(tid)

        for tid in missing:
            try:
                r = _clob_session.get(f"{CLOB_API}/midpoint", params={"token_id": tid}, timeout=5)
                if r.status_code == 200:
                    mid = r.json().get("mid")
                    r.close()
                    if mid is not None:
                        prices[tid] = float(mid)
                else:
                    r.close()
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
            rows.append({"pos": pos, "token_id": tid, "cur": cur, "val": val, "pnl": pnl, "pct": pct})
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
            self._position_index = {}
            return
        rows, total_cost, total_value = self._positions_with_pnl()
        rows.sort(key=lambda x: x["pnl"], reverse=True)

        # Rebuild index numéroté (utilisé par /closeN)
        self._position_index = {}
        for i, r in enumerate(rows, start=1):
            self._position_index[i] = r["token_id"]

        lines = [f"📋 <b>Positions ouvertes ({len(p.positions)} / 60)</b>\n"]
        for i, r in enumerate(rows, start=1):
            pos   = r["pos"]
            icon  = "🟢" if r["pnl"] >= 0 else "🔴"
            mlink = _get_market_link(pos.get("market_id", ""), max_len=45)
            resol = _fmt_resolution(pos.get("market_id", ""))
            pnl_str = f"{'+' if r['pnl']>=0 else ''}{r['pnl']:.2f}$ ({r['pct']:+.1f}%)"
            parts = [
                f"{icon} <b>Position {i} : BUY {pos['outcome']}</b> — {mlink}",
                f"Entrée ${pos['avg_cost']:.3f}",
                f"PnL latent <b>{pnl_str}</b>",
            ]
            if resol:
                parts.append(resol)
            parts.append(f"/close{i}")
            lines.append(" | ".join(parts))

        total_pnl = total_value - total_cost
        total_pct = total_pnl / total_cost * 100 if total_cost > 0 else 0
        lines.append(
            f"\n💼 <b>TOTAL</b>  coût ${total_cost:.2f} | mtm ${total_value:.2f}\n"
            f"   PnL latent : <b>{'+' if total_pnl>=0 else ''}{total_pnl:.2f}$ ({total_pct:+.1f}%)</b>\n"
            f"\n🔥 /closeall — fermer toutes les positions"
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
            pos   = r["pos"]
            mname = _get_market_name(pos.get("market_id", ""), max_len=45)
            lines.append(
                f"{medals[i]} <b>{pos['outcome']}</b>  {pos['shares']:.1f} sh\n"
                f"   📌 {mname}\n"
                f"   ${pos['avg_cost']:.3f} → ${r['cur']:.3f}\n"
                f"   Gain : <b>+{r['pnl']:.2f}$ (+{r['pct']:.1f}%)</b>"
            )

        # Pires positions aussi
        worst3 = sorted(rows, key=lambda x: x["pnl"])[:3]
        lines.append("\n⚠️ <b>3 pires positions</b>")
        for r in worst3:
            pos   = r["pos"]
            mname = _get_market_name(pos.get("market_id", ""), max_len=45)
            lines.append(
                f"🔴 <b>{pos['outcome']}</b>  {pos['shares']:.1f} sh\n"
                f"   📌 {mname}\n"
                f"   {r['pnl']:.2f}$ ({r['pct']:+.1f}%)"
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

    def _close_position_by_token_id(self, token_id: str) -> None:
        """Ferme une position via SELL au prix midpoint actuel."""
        from copytrader import SimulatedOrder
        p   = self._trader.portfolio
        pos = p.positions.get(token_id)
        if not pos:
            _send("⚠️ Position introuvable (déjà fermée ?).")
            return
        price = self._trader._fetch_midpoint(token_id)
        if price is None:
            price = pos["avg_cost"]
        order = SimulatedOrder(
            market_id=pos.get("market_id", ""),
            token_id=token_id,
            outcome=pos["outcome"],
            price=price,
            size_usdc=pos["total_cost"],
            side="SELL",
            wallet_source="telegram-close",
        )
        order.shares = pos["shares"]
        success = p.apply_order(order)
        if success:
            pnl = (price - pos["avg_cost"]) * order.shares
            mname = _get_market_name(pos.get("market_id", ""), max_len=45)
            icon = "🟢" if pnl >= 0 else "🔴"
            _send(
                f"{icon} <b>Position fermée</b>\n"
                f"  {pos['outcome']} — {mname}\n"
                f"  SELL @ ${price:.3f} | {order.shares:.2f} shares\n"
                f"  PnL réalisé : <b>{'+' if pnl>=0 else ''}{pnl:.2f}$</b>"
            )
        else:
            _send("❌ Échec de la fermeture.")

    def _cmd_close_position(self, n: int) -> None:
        token_id = self._position_index.get(n)
        if not token_id:
            _send(
                f"⚠️ Position {n} introuvable.\n"
                f"Utilisez /positions pour voir la liste à jour."
            )
            return
        self._close_position_by_token_id(token_id)

    def _cmd_closeall(self) -> None:
        p = self._trader.portfolio
        if not p.positions:
            _send("📭 Aucune position ouverte.")
            return
        token_ids = list(p.positions.keys())
        _send(f"🔥 Fermeture de {len(token_ids)} position(s)…")
        closed = 0
        for token_id in token_ids:
            pos = p.positions.get(token_id)
            if not pos:
                continue
            self._close_position_by_token_id(token_id)
            closed += 1
        _send(f"✅ {closed} position(s) fermée(s).")

    def _cmd_ping(self) -> None:
        _send("pong — bot actif")

    def _cmd_stop(self) -> None:
        _send("🛑 <b>Arrêt demandé via Telegram…</b>\nLe bot s'arrête après le cycle en cours.")
        self._stop_event.set()
