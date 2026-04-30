"""
portfolio.py — Moteur de calcul financier du bot Polymarket

Règles strictes :
  1. Net Worth   = cash + Σ(shares × min(prix_entrée, prix_actuel))
  2. PnL réalisé = Σ(prix_sortie − prix_entrée) × shares  [trades FERMÉS seulement]
  3. Filtre BUY  : 0.10 ≤ prix_entrée ≤ 0.90
  4. Capital     : $50
"""

from datetime import datetime, timezone
from typing import Optional

ENTRY_MIN       = 0.10    # prix minimum pour ouvrir une position
ENTRY_MAX       = 0.90    # prix maximum pour ouvrir une position
PRICE_VALID_MIN = 0.02    # prix actif (en-dessous = marché résolu)
PRICE_VALID_MAX = 0.98    # prix actif (au-dessus = marché résolu)
INITIAL_BALANCE = 50.0
_MAX_ORDER_LOG  = 200


class Portfolio:
    """
    Portefeuille virtuel (dry run).

    Attributs :
      cash           — liquidités USDC disponibles
      positions      — positions ouvertes {token_id: {...}}
      closed_trades  — source de vérité pour PnL et win rate (tous les SELL)
      order_log      — derniers ordres BUY+SELL (cap 200, pour le dashboard)
    """

    def __init__(self, initial_usdc: float = INITIAL_BALANCE):
        self.initial_usdc        = float(initial_usdc)
        self.cash                = float(initial_usdc)
        self.positions: dict     = {}
        self.closed_trades: list = []
        self.order_log: list     = []
        self.total_orders_count  = 0

    # ── Alias de compatibilité (ancien code utilise balance_usdc) ─────────────
    @property
    def balance_usdc(self) -> float:
        return self.cash

    @balance_usdc.setter
    def balance_usdc(self, v: float) -> None:
        self.cash = float(v)

    # ── Opérations ────────────────────────────────────────────────────────────

    def open_position(
        self,
        token_id: str,
        market_id: str,
        outcome: str,
        price: float,
        size_usdc: float,
        wallet_source: str = "",
    ) -> Optional[dict]:
        """Ouvre ou complète une position BUY. Retourne le dict ordre ou None."""
        if size_usdc > self.cash or price <= 0:
            return None
        shares = round(size_usdc / price, 4)
        self.total_orders_count += 1
        ts = datetime.now(timezone.utc).isoformat()
        self.cash = round(self.cash - size_usdc, 4)

        pos = self.positions.setdefault(token_id, {
            "market_id":  market_id,
            "outcome":    outcome,
            "shares":     0.0,
            "avg_cost":   price,
            "total_cost": 0.0,
            "opened_at":  ts,
        })
        new_total = pos["total_cost"] + size_usdc
        new_sh    = pos["shares"] + shares
        pos["avg_cost"]   = round(new_total / new_sh, 6) if new_sh > 0 else price
        pos["shares"]     = round(new_sh, 4)
        pos["total_cost"] = round(new_total, 4)

        order = {
            "order_id":         f"SIM-{self.total_orders_count:05d}",
            "side":             "BUY",
            "token_id":         token_id,
            "market_id":        market_id,
            "outcome":          outcome,
            "price":            price,
            "shares":           shares,
            "size_usdc":        size_usdc,
            "wallet_source":    wallet_source,
            "timestamp":        ts,
            "status":           "FILLED_SIM",
            "entry_price":      None,
            "realized_pnl":     None,
            "realized_pnl_pct": None,
            "duration_sec":     None,
        }
        self._log(order)
        return order

    def close_position(
        self,
        token_id: str,
        exit_price: float,
        wallet_source: str = "",
    ) -> Optional[dict]:
        """Ferme une position en intégralité. Retourne le dict ordre ou None."""
        pos = self.positions.get(token_id)
        if not pos:
            return None

        self.total_orders_count += 1
        ts     = datetime.now(timezone.utc).isoformat()
        shares = pos["shares"]
        entry  = pos["avg_cost"]
        proc   = round(shares * exit_price, 4)
        cost   = round(shares * entry, 4)
        pnl    = round(proc - cost, 4)
        pct    = round(pnl / cost * 100, 2) if cost > 0 else 0.0
        self.cash = round(self.cash + proc, 4)
        del self.positions[token_id]

        dur = None
        oa  = pos.get("opened_at")
        if oa:
            try:
                dur = int((datetime.now(timezone.utc) - datetime.fromisoformat(oa)).total_seconds())
            except (ValueError, TypeError):
                pass

        order = {
            "order_id":         f"SIM-{self.total_orders_count:05d}",
            "side":             "SELL",
            "token_id":         token_id,
            "market_id":        pos.get("market_id", ""),
            "outcome":          pos.get("outcome", ""),
            "price":            exit_price,
            "shares":           shares,
            "size_usdc":        proc,
            "wallet_source":    wallet_source,
            "timestamp":        ts,
            "status":           "FILLED_SIM",
            "entry_price":      round(entry, 6),
            "realized_pnl":     pnl,
            "realized_pnl_pct": pct,
            "duration_sec":     dur,
            "opened_at":        oa,
        }
        self.closed_trades.append(order)
        self._log(order)
        return order

    def _log(self, order: dict) -> None:
        self.order_log.append(order)
        if len(self.order_log) > _MAX_ORDER_LOG:
            self.order_log = self.order_log[-_MAX_ORDER_LOG:]

    # ── Indicateurs financiers ─────────────────────────────────────────────────

    def net_worth(self, current_prices: Optional[dict] = None) -> float:
        """
        Net Worth = cash + Σ(shares × min(prix_entrée, prix_actuel))

        Conservateur : si le prix baisse, on valorise à la baisse (réaliste).
                       si le prix monte, on garde le prix d'entrée (pas de gain fictif).
        Prix hors [0.02, 0.98] → marché résolu, fallback sur prix d'entrée.
        """
        total = self.cash
        cp    = current_prices or {}
        for tid, pos in self.positions.items():
            entry = pos["avg_cost"]
            cur   = cp.get(tid)
            if cur is None or not (PRICE_VALID_MIN <= cur <= PRICE_VALID_MAX):
                cur = entry
            total += pos["shares"] * min(entry, cur)
        return round(total, 4)

    @property
    def realized_pnl(self) -> float:
        """Somme des PnL des trades FERMÉS uniquement (source de vérité)."""
        return round(sum(t["realized_pnl"] for t in self.closed_trades), 4)

    @property
    def win_rate(self) -> tuple:
        """(win_rate_pct | None, gagnants, perdants) — basé sur trades FERMÉS uniquement."""
        if not self.closed_trades:
            return None, 0, 0
        w = sum(1 for t in self.closed_trades if t["realized_pnl"] > 0)
        l = len(self.closed_trades) - w
        return round(w / len(self.closed_trades) * 100, 1), w, l

    def unrealized_pnl(self, current_prices: Optional[dict] = None) -> float:
        """PnL latent = Σ((prix_actuel − prix_entrée) × shares) pour positions ouvertes."""
        cp  = current_prices or {}
        tot = 0.0
        for tid, pos in self.positions.items():
            entry = pos["avg_cost"]
            cur   = cp.get(tid)
            if cur is None or not (PRICE_VALID_MIN <= cur <= PRICE_VALID_MAX):
                cur = entry
            tot += (cur - entry) * pos["shares"]
        return round(tot, 4)

    def return_pct(self, current_prices: Optional[dict] = None) -> float:
        """Rendement en % par rapport au capital initial."""
        if self.initial_usdc <= 0:
            return 0.0
        return round((self.net_worth(current_prices) - self.initial_usdc) / self.initial_usdc * 100, 2)

    # ── Restauration depuis historique ────────────────────────────────────────

    def restore_closed_trades(self, trade_history: list) -> int:
        """
        Peuple closed_trades depuis trade_history (persistence JSON).
        Retourne le nombre de trades restaurés.
        """
        self.closed_trades = []
        for t in trade_history:
            if t.get("side") != "SELL":
                continue
            rpnl = t.get("realized_pnl")
            if rpnl is None:
                continue
            self.closed_trades.append({
                "order_id":         t.get("order_id", ""),
                "side":             "SELL",
                "token_id":         t.get("token_id", ""),
                "market_id":        t.get("market_id", ""),
                "outcome":          t.get("outcome", ""),
                "price":            t.get("price", 0.0),
                "shares":           t.get("shares", 0.0),
                "size_usdc":        t.get("size_usdc", 0.0),
                "wallet_source":    t.get("source", ""),
                "timestamp":        t.get("ts", t.get("timestamp", "")),
                "entry_price":      t.get("entry_price"),
                "realized_pnl":     float(rpnl),
                "realized_pnl_pct": t.get("realized_pnl_pct"),
                "duration_sec":     t.get("duration_sec"),
                "opened_at":        None,
            })
        return len(self.closed_trades)

    # ── Affichage ─────────────────────────────────────────────────────────────

    def display(self, current_prices: Optional[dict] = None) -> None:
        nw  = self.net_worth(current_prices)
        unr = self.unrealized_pnl(current_prices)
        wr, ww, wl = self.win_rate
        wrs = f"{wr:.1f}% ({ww}W/{wl}L)" if wr is not None else "N/A"
        print("\n── Portfolio ─────────────────────────────────────────────")
        print(f"  Cash        : ${self.cash:>10.2f} USDC")
        print(f"  Positions   : {len(self.positions)}")
        print(f"  PnL réalisé : ${self.realized_pnl:>+.2f}")
        print(f"  PnL latent  : ${unr:>+.2f}")
        print(f"  Net worth   : ${nw:>10.2f} USDC")
        print(f"  Win rate    : {wrs}")
        if self.positions:
            print("  ── Positions ouvertes ──")
            cp = current_prices or {}
            for tid, p in self.positions.items():
                cur = cp.get(tid, p["avg_cost"])
                if not (PRICE_VALID_MIN <= cur <= PRICE_VALID_MAX):
                    cur = p["avg_cost"]
                val  = p["shares"] * cur
                upnl = (cur - p["avg_cost"]) * p["shares"]
                print(
                    f"    {p['outcome']:3s} | {p['shares']:.4f} sh"
                    f" @ avg ${p['avg_cost']:.3f} | mtm ${val:.2f} | PnL {upnl:+.2f}$"
                )
