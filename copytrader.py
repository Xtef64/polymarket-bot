"""
copytrader.py - Reproduit les trades de wallets cibles sur Polymarket
Mode DRY RUN : simule les ordres sans les envoyer réellement.
"""

import time
import requests
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional

# Session persistante — réutilise les connexions TCP
_session = requests.Session()
_session.headers.update({"Accept": "application/json", "User-Agent": "polymarket-bot/1.0"})


# ── Constantes de risque ──────────────────────────────────────────────────────
DEFAULT_TRADE_SIZE_USDC = 10.0   # montant par trade en USDC (dry run)
MAX_TRADE_SIZE_USDC     = 100.0
MAX_OPEN_POSITIONS      = 20
MIN_PRICE               = 0.05   # évite les moonshots à 2¢
MAX_PRICE               = 0.95   # évite les marchés quasi-résolus
MIN_MARKET_VOLUME       = 5_000  # liquidité minimale 24h
CLOB_API                = "https://clob.polymarket.com"
GAMMA_API               = "https://gamma-api.polymarket.com"
STALE_POSITION_HOURS    = 24     # ferme les positions ouvertes depuis plus de X heures
MAX_RESOLUTION_HOURS    = 24     # ignore les marchés qui se résolvent dans plus de 24h


class SimulatedOrder:
    """Représente un ordre simulé (dry run)."""
    _counter = 0

    def __init__(
        self,
        market_id: str,
        token_id: str,
        outcome: str,
        price: float,
        size_usdc: float,
        side: str = "BUY",
        wallet_source: str = "",
    ):
        SimulatedOrder._counter += 1
        self.order_id     = f"SIM-{SimulatedOrder._counter:05d}"
        self.market_id    = market_id
        self.token_id     = token_id
        self.outcome      = outcome
        self.price        = price
        self.size_usdc    = size_usdc
        self.shares       = round(size_usdc / price, 4) if price > 0 else 0
        self.side         = side
        self.wallet_source = wallet_source
        self.timestamp    = datetime.now(timezone.utc).isoformat()
        self.status       = "FILLED_SIM"

    def __repr__(self) -> str:
        return (
            f"[{self.order_id}] {self.side} {self.outcome} "
            f"@ ${self.price:.3f} × {self.shares:.2f} shares "
            f"(${self.size_usdc:.2f} USDC) | market={self.market_id[:12]}..."
        )


class PortfolioSimulator:
    """Suit le portefeuille en mode dry run."""

    _MAX_ORDER_LOG = 200  # garde uniquement les N derniers ordres en mémoire

    def __init__(self, initial_usdc: float = 300.0):
        self.balance_usdc  = initial_usdc
        self.positions: dict[str, dict] = {}  # token_id → position
        self.order_log: list[SimulatedOrder] = []
        self.realized_pnl = 0.0
        self.total_orders_count = 0  # compteur cumulatif (non affecté par le cap)

    def apply_order(self, order: SimulatedOrder) -> bool:
        """Applique un ordre simulé au portefeuille virtuel."""
        if order.side == "BUY":
            if order.size_usdc > self.balance_usdc:
                print(f"  [Portfolio] Solde insuffisant (${self.balance_usdc:.2f})")
                return False
            self.balance_usdc -= order.size_usdc
            pos = self.positions.setdefault(order.token_id, {
                "market_id":  order.market_id,
                "outcome":    order.outcome,
                "shares":     0.0,
                "avg_cost":   0.0,
                "total_cost": 0.0,
                "opened_at":  datetime.now(timezone.utc).isoformat(),
            })
            new_total   = pos["total_cost"] + order.size_usdc
            new_shares  = pos["shares"] + order.shares
            pos["avg_cost"]   = new_total / new_shares if new_shares else 0
            pos["shares"]     = round(new_shares, 4)
            pos["total_cost"] = round(new_total, 4)

        elif order.side == "SELL":
            pos = self.positions.get(order.token_id)
            if not pos or pos["shares"] < order.shares:
                print(f"  [Portfolio] Pas assez de shares pour vendre")
                return False
            proceeds = order.shares * order.price
            self.balance_usdc += proceeds
            self.realized_pnl += proceeds - (order.shares * pos["avg_cost"])
            pos["shares"] = round(pos["shares"] - order.shares, 4)
            if pos["shares"] <= 0:
                del self.positions[order.token_id]

        self.order_log.append(order)
        self.total_orders_count += 1
        # Cap pour éviter la croissance mémoire infinie
        if len(self.order_log) > self._MAX_ORDER_LOG:
            self.order_log = self.order_log[-self._MAX_ORDER_LOG:]
        return True

    def net_worth(self, current_prices: Optional[dict] = None) -> float:
        """Calcule la valeur nette du portefeuille (USDC + positions mark-to-market)."""
        mtm = 0.0
        if current_prices:
            for token_id, pos in self.positions.items():
                price = current_prices.get(token_id, pos["avg_cost"])
                mtm  += pos["shares"] * price
        else:
            for pos in self.positions.values():
                mtm += pos["total_cost"]  # au prix d'achat
        return round(self.balance_usdc + mtm, 4)

    def display(self) -> None:
        print("\n── Portfolio Simulé ──────────────────────────────────────")
        print(f"  Cash      : ${self.balance_usdc:>10.2f} USDC")
        print(f"  Positions : {len(self.positions)}")
        print(f"  PnL réalisé : ${self.realized_pnl:>+.2f}")
        print(f"  Net worth : ${self.net_worth():>10.2f} USDC")
        if self.positions:
            print("  ── Positions ouvertes ──")
            for tid, p in self.positions.items():
                print(
                    f"    {p['outcome']:3s} | {p['shares']:.4f} shares "
                    f"@ avg ${p['avg_cost']:.3f} | cost ${p['total_cost']:.2f}"
                )


class CopyTrader:
    def __init__(
        self,
        dry_run: bool = True,
        trade_size_usdc: float = DEFAULT_TRADE_SIZE_USDC,
        max_positions: int = MAX_OPEN_POSITIONS,
        initial_balance: float = 300.0,
    ):
        self.dry_run         = dry_run
        self.trade_size_usdc = min(trade_size_usdc, MAX_TRADE_SIZE_USDC)
        self.max_positions   = max_positions
        self.portfolio       = PortfolioSimulator(initial_balance)
        self._processed_ids: set[str] = set()
        # deque(maxlen) : popleft O(1) au lieu de list.pop(0) O(n)
        self._processed_ids_order: deque = deque(maxlen=5_000)
        self._MAX_PROCESSED_IDS = 5_000
        # Cache dates de résolution (ne changent jamais) — évite 1 appel API / trade
        self._end_date_cache: dict[str, str | None] = {}

    # ── Validation ────────────────────────────────────────────────────────────

    def _fetch_market_end_date(self, condition_id: str) -> Optional[str]:
        """Récupère la date de résolution d'un marché via l'API Gamma.
        Résultat mis en cache indéfiniment (les dates ne changent pas)."""
        if condition_id in self._end_date_cache:
            return self._end_date_cache[condition_id]
        try:
            r = _session.get(
                f"{GAMMA_API}/markets",
                params={"condition_id": condition_id},
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                r.close()
                end_date = data[0].get("endDate") if isinstance(data, list) and data else None
                self._end_date_cache[condition_id] = end_date
                # Cap cache à 2000 entrées pour éviter OOM sur longue durée
                if len(self._end_date_cache) > 2_000:
                    oldest = next(iter(self._end_date_cache))
                    del self._end_date_cache[oldest]
                return end_date
            r.close()
        except Exception:
            pass
        self._end_date_cache[condition_id] = None
        return None

    def _is_valid_trade(self, trade: dict, market_info: Optional[dict] = None) -> tuple[bool, str]:
        """Retourne (valide, raison) pour un trade candidat."""
        price = float(trade.get("price", 0) or 0)
        if not (MIN_PRICE <= price <= MAX_PRICE):
            return False, f"prix hors plage ({price:.3f})"
        if len(self.portfolio.positions) >= self.max_positions:
            return False, "trop de positions ouvertes"
        if market_info:
            vol = float(market_info.get("volume_24h", 0) or 0)
            if vol < MIN_MARKET_VOLUME:
                return False, f"volume trop faible (${vol:,.0f})"

        # Filtre résolution : marché doit se résoudre dans les 3 prochaines heures
        end_date_str = market_info.get("end_date") if market_info else None
        if end_date_str is None:
            condition_id = trade.get("conditionId") or trade.get("market", "")
            if condition_id:
                end_date_str = self._fetch_market_end_date(condition_id)
        if end_date_str is not None:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                hours_left = (end_date - now).total_seconds() / 3600
                if hours_left < 0:
                    return False, f"marché déjà résolu"
                if hours_left > MAX_RESOLUTION_HOURS:
                    return False, f"résolution trop loin ({hours_left:.1f}h > {MAX_RESOLUTION_HOURS}h)"
            except (ValueError, TypeError):
                pass
        else:
            return False, "date de résolution inconnue"

        return True, "OK"

    # ── Copie de trade ────────────────────────────────────────────────────────

    def copy_trade(self, trade: dict, market_info: Optional[dict] = None) -> Optional[SimulatedOrder]:
        """
        Copie un trade détecté depuis un wallet suivi.
        En dry run : crée un SimulatedOrder et l'applique au portefeuille virtuel.
        En live    : appellerait l'API CLOB (non implémenté ici).
        """
        trade_id = (
            f"{trade.get('conditionId','')}|{trade.get('timestamp','')}|"
            f"{trade.get('proxyWallet', trade.get('wallet',''))}|{trade.get('side','')}"
        )
        if trade_id in self._processed_ids:
            return None
        # Éviction FIFO via deque(maxlen) — popleft O(1)
        if len(self._processed_ids_order) >= self._MAX_PROCESSED_IDS:
            evicted = self._processed_ids_order[0]  # peek sans pop (deque auto-évince)
            self._processed_ids.discard(evicted)
        self._processed_ids.add(trade_id)
        self._processed_ids_order.append(trade_id)  # auto-évince le plus ancien si maxlen atteint

        side    = (trade.get("side") or "BUY").upper()
        outcome = (trade.get("outcome") or "YES").upper()
        price   = float(trade.get("price", 0.5) or 0.5)
        token_id  = trade.get("asset") or trade.get("asset_id") or trade.get("tokenId", "")
        market_id = trade.get("conditionId") or trade.get("market", "")
        wallet    = trade.get("wallet", "unknown")

        valid, reason = self._is_valid_trade(trade, market_info)
        if not valid:
            print(f"  [CopyTrader] Trade ignoré ({reason}): {trade_id[:12]}...")
            return None

        order = SimulatedOrder(
            market_id=market_id,
            token_id=token_id,
            outcome=outcome,
            price=price,
            size_usdc=self.trade_size_usdc,
            side=side,
            wallet_source=wallet,
        )

        mode_label = "DRY RUN" if self.dry_run else "LIVE"
        print(f"  [{mode_label}] {order}")

        if self.dry_run:
            success = self.portfolio.apply_order(order)
            if not success:
                return None

        return order

    def process_new_trades(
        self,
        new_trades: list[dict],
        market_lookup: Optional[dict] = None,
    ) -> list[SimulatedOrder]:
        """Traite une liste de nouveaux trades détectés par le WalletTracker."""
        executed = []
        for trade in new_trades:
            market_id   = trade.get("market") or trade.get("conditionId", "")
            market_info = (market_lookup or {}).get(market_id)
            order = self.copy_trade(trade, market_info)
            if order:
                executed.append(order)
            time.sleep(0.1)
        return executed

    def _fetch_midpoint(self, token_id: str) -> Optional[float]:
        """Récupère le prix midpoint actuel depuis l'API CLOB."""
        try:
            r = _session.get(
                f"{CLOB_API}/midpoint",
                params={"token_id": token_id},
                timeout=5,
            )
            if r.status_code == 200:
                mid = r.json().get("mid")
                r.close()
                if mid is not None:
                    return float(mid)
            r.close()
        except Exception:
            pass
        return None

    def auto_close_stale_positions(self, max_age_hours: int = STALE_POSITION_HOURS) -> list[SimulatedOrder]:
        """
        Ferme automatiquement les positions ouvertes depuis plus de max_age_hours.
        En dry run : simule un SELL au prix midpoint actuel.
        Retourne la liste des ordres de fermeture exécutés.
        """
        now       = datetime.now(timezone.utc)
        cutoff    = now - timedelta(hours=max_age_hours)
        closed    = []
        to_close  = []

        for token_id, pos in list(self.portfolio.positions.items()):
            opened_at_str = pos.get("opened_at")
            if not opened_at_str:
                # Position sans timestamp (ancienne session) → considérée comme périmée
                to_close.append(token_id)
                continue
            try:
                opened_at = datetime.fromisoformat(opened_at_str)
                if opened_at < cutoff:
                    to_close.append(token_id)
            except ValueError:
                to_close.append(token_id)

        if to_close:
            print(f"\n  [AutoClose] {len(to_close)} position(s) perimee(s) (>{max_age_hours}h) a fermer")

        for token_id in to_close:
            pos   = self.portfolio.positions.get(token_id)
            if not pos:
                continue
            price = self._fetch_midpoint(token_id)
            if price is None:
                price = pos["avg_cost"]  # fallback au prix d'achat
                print(f"  [AutoClose] Midpoint indisponible pour {token_id[:12]}..., utilise avg_cost ${price:.3f}")

            order = SimulatedOrder(
                market_id=pos.get("market_id", ""),
                token_id=token_id,
                outcome=pos["outcome"],
                price=price,
                size_usdc=pos["total_cost"],
                side="SELL",
                wallet_source="auto-close",
            )
            order.shares = pos["shares"]  # on vend toutes les shares

            success = self.portfolio.apply_order(order)
            if success:
                pnl = (price - pos["avg_cost"]) * pos["shares"]
                print(
                    f"  [AutoClose] SELL {pos['outcome']} {pos['shares']:.2f}sh "
                    f"@ ${price:.3f} | PnL={pnl:+.2f}$ | {token_id[:12]}..."
                )
                closed.append(order)
            time.sleep(0.1)

        return closed

    def display_log(self, last_n: int = 10) -> None:
        print(f"\n── Derniers {last_n} ordres simulés ──────────────────────────────")
        for o in self.portfolio.order_log[-last_n:]:
            src = o.wallet_source[:10] + "..." if o.wallet_source else "?"
            print(f"  {o.timestamp[11:19]} | {o} | src={src}")
