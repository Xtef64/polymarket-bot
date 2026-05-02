"""
copytrader.py - Reproduit les trades de wallets cibles sur Polymarket
Mode DRY RUN : simule les ordres sans les envoyer réellement.
"""

import time
import requests
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional

from portfolio import Portfolio, ENTRY_MIN, ENTRY_MAX
from market_analyzer import is_allowed_market

# Session persistante — réutilise les connexions TCP
_session = requests.Session()
_session.headers.update({"Accept": "application/json", "User-Agent": "polymarket-bot/1.0"})


# ── Constantes ────────────────────────────────────────────────────────────────
DEFAULT_TRADE_SIZE_USDC = 10.0
MAX_TRADE_SIZE_USDC     = 100.0
MAX_OPEN_POSITIONS      = 20
MIN_MARKET_VOLUME       = 1_000
CLOB_API                = "https://clob.polymarket.com"
GAMMA_API               = "https://gamma-api.polymarket.com"
STALE_POSITION_HOURS    = 72
MIN_RESOLUTION_HOURS    = 24   # résolution doit être > 24h dans le futur


class CopyTrader:
    def __init__(
        self,
        dry_run: bool = True,
        trade_size_usdc: float = DEFAULT_TRADE_SIZE_USDC,
        max_positions: int = MAX_OPEN_POSITIONS,
        initial_balance: float = 50.0,
    ):
        self.dry_run         = dry_run
        self.trade_size_usdc = min(trade_size_usdc, MAX_TRADE_SIZE_USDC)
        self.max_positions   = max_positions
        self.portfolio       = Portfolio(initial_balance)
        self._processed_ids: set[str] = set()
        self._processed_ids_order: deque = deque(maxlen=5_000)
        self._MAX_PROCESSED_IDS = 5_000
        self._market_meta_cache: dict[str, dict] = {}  # condition_id → {end_date, question, slug, group_slug}

    # ── Validation ────────────────────────────────────────────────────────────

    def _fetch_market_meta(self, condition_id: str) -> dict:
        """Récupère question, slug, groupSlug et endDate via l'API Gamma (avec cache)."""
        if condition_id in self._market_meta_cache:
            return self._market_meta_cache[condition_id]
        try:
            r = _session.get(
                f"{GAMMA_API}/markets",
                params={"condition_id": condition_id},
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                r.close()
                m0 = data[0] if isinstance(data, list) and data else None
                if m0 and (m0.get("conditionId") or "").lower() == condition_id.lower():
                    meta = {
                        "end_date":   m0.get("endDate"),
                        "question":   m0.get("question", ""),
                        "slug":       m0.get("slug", ""),
                        "group_slug": m0.get("groupSlug", ""),
                    }
                    self._market_meta_cache[condition_id] = meta
                    if len(self._market_meta_cache) > 2_000:
                        del self._market_meta_cache[next(iter(self._market_meta_cache))]
                    return meta
            else:
                r.close()
        except Exception:
            pass
        return {}

    def _is_valid_trade(self, trade: dict, market_info: Optional[dict] = None) -> tuple[bool, str]:
        """Retourne (valide, raison) pour un trade candidat."""
        price = float(trade.get("price", 0) or 0)
        side  = (trade.get("side") or "BUY").upper()

        # Prix : 0.60 ≤ prix ≤ 0.90 (haute conviction, marchés politiques/économiques)
        if side == "BUY":
            if not (ENTRY_MIN <= price <= ENTRY_MAX):
                return False, f"prix entrée hors plage [{ENTRY_MIN},{ENTRY_MAX}] ({price:.3f})"
        else:
            if price <= 0:
                return False, f"prix nul ({price:.3f})"

        if len(self.portfolio.positions) >= self.max_positions:
            return False, "trop de positions ouvertes"

        if market_info:
            vol = float(market_info.get("volume_24h", 0) or 0)
            if vol < MIN_MARKET_VOLUME:
                return False, f"volume trop faible (${vol:,.0f})"

        # Si market_info absent, on récupère les métadonnées via l'API
        condition_id = trade.get("conditionId") or trade.get("market", "")
        if not market_info and condition_id:
            fetched = self._fetch_market_meta(condition_id)
            if fetched:
                market_info = fetched

        # Catégorie : marché politique ou économique obligatoire
        question   = (market_info.get("question",   "") if market_info else "")
        slug       = (market_info.get("slug",        "") if market_info else "")
        group_slug = (market_info.get("group_slug",  "") if market_info else "")

        if not question and not slug:
            return False, "catégorie marché inconnue (métadonnées absentes)"
        if not is_allowed_market(question, slug, group_slug):
            return False, f"marché non politique/économique"

        # Résolution : doit être > MIN_RESOLUTION_HOURS dans le futur
        end_date_str = market_info.get("end_date") if market_info else None
        if end_date_str is not None:
            try:
                end_date   = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                now        = datetime.now(timezone.utc)
                hours_left = (end_date - now).total_seconds() / 3600
                if hours_left < 0:
                    return False, "marché déjà résolu"
                if hours_left < MIN_RESOLUTION_HOURS:
                    return False, f"résolution trop proche ({hours_left:.1f}h < {MIN_RESOLUTION_HOURS}h)"
            except (ValueError, TypeError):
                pass

        return True, "OK"

    # ── Copie de trade ────────────────────────────────────────────────────────

    def copy_trade(self, trade: dict, market_info: Optional[dict] = None) -> Optional[dict]:
        """
        Copie un trade détecté depuis un wallet suivi.
        Retourne le dict ordre ou None si ignoré.
        """
        trade_id = (
            f"{trade.get('conditionId','')}|{trade.get('timestamp','')}|"
            f"{trade.get('proxyWallet', trade.get('wallet',''))}|{trade.get('side','')}"
        )
        if trade_id in self._processed_ids:
            return None
        if len(self._processed_ids_order) >= self._MAX_PROCESSED_IDS:
            evicted = self._processed_ids_order[0]
            self._processed_ids.discard(evicted)
        self._processed_ids.add(trade_id)
        self._processed_ids_order.append(trade_id)

        side     = (trade.get("side") or "BUY").upper()
        outcome  = (trade.get("outcome") or "YES").upper()
        price    = float(trade.get("price", 0.5) or 0.5)
        token_id  = trade.get("asset") or trade.get("asset_id") or trade.get("tokenId", "")
        market_id = trade.get("conditionId") or trade.get("market", "")
        wallet    = trade.get("wallet", "unknown")

        valid, reason = self._is_valid_trade(trade, market_info)
        if not valid:
            print(f"  [CopyTrader] Trade ignoré ({reason}): {trade_id[:12]}...")
            return None

        mode_label = "DRY RUN" if self.dry_run else "LIVE"

        if self.dry_run:
            if side == "BUY":
                order = self.portfolio.open_position(
                    token_id=token_id,
                    market_id=market_id,
                    outcome=outcome,
                    price=price,
                    size_usdc=self.trade_size_usdc,
                    wallet_source=wallet,
                )
            elif side == "SELL":
                order = self.portfolio.close_position(
                    token_id=token_id,
                    exit_price=price,
                    wallet_source=wallet,
                )
            else:
                order = None

            if not order:
                print(f"  [{mode_label}] Ordre refusé (solde insuffisant ou position inexistante)")
                return None
            print(
                f"  [{mode_label}] [{order['order_id']}] {side} {outcome}"
                f" @ ${price:.3f} × {order['shares']:.2f} sh"
                f" (${order['size_usdc']:.2f} USDC)"
            )

        return order

    def process_new_trades(
        self,
        new_trades: list[dict],
        market_lookup: Optional[dict] = None,
    ) -> list[dict]:
        """Traite une liste de nouveaux trades détectés par le WalletTracker."""
        executed   = []
        rejections: dict[str, int] = {}
        for trade in new_trades:
            market_id   = trade.get("market") or trade.get("conditionId", "")
            market_info = (market_lookup or {}).get(market_id)

            valid, reason = self._is_valid_trade(trade, market_info)
            if not valid:
                key = reason.split(" (")[0]
                rejections[key] = rejections.get(key, 0) + 1
                price = float(trade.get("price", 0) or 0)
                src   = trade.get("_source", "?")
                print(f"  [Filtre] {reason} | prix=${price:.3f} src={src} market={market_id[:14]}...")
            else:
                order = self.copy_trade(trade, market_info)
                if order:
                    executed.append(order)
            time.sleep(0.1)

        if rejections:
            summary = " | ".join(f"{k}: {v}" for k, v in sorted(rejections.items(), key=lambda x: -x[1]))
            print(f"  [Filtres] Résumé rejets : {summary}")

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

    def auto_close_stale_positions(self, max_age_hours: int = STALE_POSITION_HOURS) -> list[dict]:
        """
        Ferme automatiquement les positions ouvertes depuis plus de max_age_hours.
        Retourne la liste des ordres de fermeture (dicts).
        """
        now    = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=max_age_hours)
        closed: list[dict] = []
        to_close: list[str] = []

        for token_id, pos in list(self.portfolio.positions.items()):
            opened_at_str = pos.get("opened_at")
            if not opened_at_str:
                to_close.append(token_id)
                continue
            try:
                opened_at = datetime.fromisoformat(opened_at_str)
                if opened_at < cutoff:
                    to_close.append(token_id)
            except ValueError:
                to_close.append(token_id)

        if to_close:
            print(f"\n  [AutoClose] {len(to_close)} position(s) périmée(s) (>{max_age_hours}h) à fermer")

        for token_id in to_close:
            pos = self.portfolio.positions.get(token_id)
            if not pos:
                continue
            price = self._fetch_midpoint(token_id)
            if price is None:
                price = pos["avg_cost"]
                print(f"  [AutoClose] Midpoint indisponible pour {token_id[:12]}…, utilise avg_cost ${price:.3f}")

            order = self.portfolio.close_position(token_id, price, wallet_source="auto-close")
            if order:
                print(
                    f"  [AutoClose] SELL {order['outcome']} {order['shares']:.2f}sh"
                    f" @ ${price:.3f} | PnL={order['realized_pnl']:+.2f}$ | {token_id[:12]}…"
                )
                closed.append(order)
            time.sleep(0.1)

        return closed

    def auto_stop_loss(self, price_cache: dict, max_loss_pct: float = -20.0) -> list[dict]:
        """
        Ferme toute position dont le PnL latent dépasse max_loss_pct (ex: -20%).
        Retourne la liste des ordres de fermeture (dicts).
        """
        closed: list[dict] = []
        to_close: list[tuple[str, float, float]] = []

        n_pos   = len(self.portfolio.positions)
        n_cache = sum(1 for tid in self.portfolio.positions if tid in price_cache)
        print(f"  [StopLoss] {n_pos} position(s) — {n_cache}/{n_pos} prix en cache "
              f"(seuil {max_loss_pct:.0f}%)")

        for token_id, pos in list(self.portfolio.positions.items()):
            avg_cost = pos.get("avg_cost", 0.0)
            if avg_cost <= 0:
                continue

            cur_price = price_cache.get(token_id)
            source    = "cache"
            if cur_price is None:
                cur_price = self._fetch_midpoint(token_id)
                source    = "CLOB" if cur_price is not None else "indisponible"

            if cur_price is None:
                print(f"    {token_id[:14]}… avg=${avg_cost:.4f} | prix INDISPONIBLE — ignoré")
                continue

            pnl_pct = (cur_price - avg_cost) / avg_cost * 100
            flag    = " [STOP-LOSS]" if pnl_pct <= max_loss_pct else ""
            print(f"    {token_id[:14]}… avg=${avg_cost:.4f} cur=${cur_price:.4f} "
                  f"[{source}] PnL={pnl_pct:+.1f}%{flag}")

            if pnl_pct <= max_loss_pct:
                to_close.append((token_id, cur_price, pnl_pct))

        if to_close:
            print(f"  [StopLoss] {len(to_close)} position(s) sous {max_loss_pct:.0f}% à fermer")
        else:
            print(f"  [StopLoss] Aucune position sous le seuil")

        for token_id, price, pnl_pct in to_close:
            order = self.portfolio.close_position(token_id, price, wallet_source="stop-loss")
            if order:
                print(
                    f"  [StopLoss] SELL {order['outcome']} {order['shares']:.2f}sh"
                    f" @ ${price:.4f} | PnL={order['realized_pnl']:+.2f}$ ({pnl_pct:+.1f}%) | {token_id[:12]}…"
                )
                closed.append(order)
            time.sleep(0.1)

        return closed

    def display_log(self, last_n: int = 10) -> None:
        print(f"\n── Derniers {last_n} ordres simulés ──────────────────────────────")
        for o in self.portfolio.order_log[-last_n:]:
            src    = (o.get("wallet_source") or "?")[:10] + "…"
            ts     = (o.get("timestamp") or "")
            ts_s   = ts[11:19] if len(ts) >= 19 else ts
            side   = o.get("side", "?")
            out    = o.get("outcome", "?")
            price  = o.get("price", 0)
            shares = o.get("shares", 0)
            oid    = o.get("order_id", "?")
            print(f"  {ts_s} | [{oid}] {side} {out} @ ${price:.3f} × {shares:.2f} sh | src={src}")
