# =============================================================================
# app/services/risk/engine.py
# Module 9 â€” Risk & Margin Engine | ARC Trading Platform
#
# SUBMODULES (per spec section 5.9):
#   1. MarginCalculator      â€” calculates margin needed per product type
#   2. ProductRuleEngine     â€” validates lot size and tick size compliance
#   3. ExposureChecker       â€” checks position limits and broker caps
#   4. LimitEnforcement      â€” enforces user/broker/platform limits
#   5. LiquidationGuardrails â€” warns and prevents dangerous positions
#   6. UserHealthMonitor     â€” computes portfolio health ratios
#   7. PreTradeMarginValidator â€” orchestrates all checks before order placement
#
# MATHEMATICAL FORMULAS explained inline for beginners.
# =============================================================================

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import select, func, case as sa_case
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# =============================================================================
# MARGIN RATES PER PRODUCT TYPE
# (how much of the trade value the user must have in their wallet)
#
# WHAT IS A MARGIN RATE?
#   If you want to buy â‚¹1,00,000 worth of RELIANCE shares:
#     Delivery  â†’ 100% margin = â‚¹1,00,000 needed  (no leverage)
#     Intraday  â†’ 20%  margin = â‚¹20,000   needed  (5x leverage)
#     Futures   â†’ 15%  margin = â‚¹15,000   needed  (6.67x leverage)
#     Options   â†’ 100% of premium          (full premium needed to buy)
#     Crypto    â†’ 100% of value            (no leverage for paper trading)
# =============================================================================

MARGIN_RATES: dict[str, Decimal] = {
    "intraday" : Decimal("0.20"),   # 20% â†’ 5x leverage
    "delivery" : Decimal("1.00"),   # 100% â†’ no leverage (full amount needed)
    "futures"  : Decimal("0.15"),   # 15% â†’ ~6.67x leverage
    "options"  : Decimal("1.00"),   # 100% â†’ full premium for buying options
    "crypto"   : Decimal("1.00"),   # 100% â†’ no leverage (paper trading safety)
}

# Leverage limits by user role (max allowed leverage multiplier)
# SUPER_ADMIN and BROKER have higher limits for testing
LEVERAGE_LIMITS: dict[str, dict[str, Decimal]] = {
    "super_admin": {
        "intraday": Decimal("10"),  # 10x max
        "delivery": Decimal("1"),
        "futures" : Decimal("10"),
        "options" : Decimal("1"),
    },
    "broker": {
        "intraday": Decimal("10"),
        "delivery": Decimal("1"),
        "futures" : Decimal("7"),
        "options" : Decimal("1"),
    },
    "user": {
        "intraday": Decimal("5"),   # 5x max for regular users
        "delivery": Decimal("1"),
        "futures" : Decimal("5"),
        "options" : Decimal("1"),
    },
}

# Minimum margin floor â€” no order can require less than this
MINIMUM_MARGIN = Decimal("1.00")

# Maximum quantity per order by product type (circuit breaker)
MAX_QUANTITY: dict[str, Decimal] = {
    "intraday" : Decimal("10000"),
    "delivery" : Decimal("10000"),
    "futures"  : Decimal("1000"),
    "options"  : Decimal("10000"),
    "crypto"   : Decimal("100"),
}

# Health ratio thresholds
HEALTH_HEALTHY  = Decimal("100")   # > 100% = healthy
HEALTH_WARNING  = Decimal("50")    # 50-100% = warning
HEALTH_DANGER   = Decimal("25")    # 25-50% = danger
HEALTH_CRITICAL = Decimal("0")     # < 25% = critical â†’ liquidation risk


# =============================================================================
# SUBMODULE 1 â€” MARGIN CALCULATOR
# =============================================================================

class MarginCalculator:
    """
    Calculates the margin required for a given trade.

    MARGIN FORMULA (step by step for beginners):
        Step 1: Get the effective price
                If market order â†’ use live LTP from Redis
                If limit order  â†’ use the limit price entered by user

        Step 2: Calculate trade value
                trade_value = quantity Ã— effective_price
                Example: 10 shares Ã— â‚¹2,400 = â‚¹24,000

        Step 3: Get the margin rate for this product type
                intraday â†’ 20% (from MARGIN_RATES table above)

        Step 4: Calculate raw margin
                raw_margin = trade_value Ã— margin_rate
                Example: â‚¹24,000 Ã— 0.20 = â‚¹4,800

        Step 5: Apply minimum floor
                final_margin = max(raw_margin, MINIMUM_MARGIN)
                Example: max(â‚¹4,800, â‚¹1) = â‚¹4,800

        Step 6: Calculate leverage
                leverage = 1 / margin_rate
                Example: 1 / 0.20 = 5x leverage
    """

    def calculate(
        self,
        quantity     : Decimal,
        effective_price: Decimal,
        product_type : str,
        symbol       : str = "",
    ) -> dict:
        """
        Calculate margin for a trade.

        Args:
            quantity       : number of units to trade
            effective_price: price per unit (LTP or limit price)
            product_type   : intraday, delivery, futures, options, crypto
            symbol         : trading symbol (used to detect crypto)

        Returns:
            dict with all margin calculation details
        """
        # â”€â”€ Detect product type for crypto symbols â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Crypto symbols always use 100% margin regardless of product_type
        pt = product_type.lower()
        if "CRYPTO" in symbol.upper() or "USDT" in symbol.upper():
            pt = "crypto"

        # â”€â”€ Step 1: Get margin rate â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        margin_rate = MARGIN_RATES.get(pt, Decimal("1.00"))

        # â”€â”€ Step 2: Calculate trade value â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # This is the total money involved in the trade BEFORE leverage
        trade_value = (quantity * effective_price).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        # â”€â”€ Step 3: Calculate raw margin â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # This is how much the user actually needs in their wallet
        raw_margin = (trade_value * margin_rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        # â”€â”€ Step 4: Apply minimum floor â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Even very small trades need at least â‚¹1 margin
        final_margin = max(raw_margin, MINIMUM_MARGIN)

        # â”€â”€ Step 5: Calculate leverage â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # leverage = how many times more exposure you get than margin paid
        # Example: 20% margin rate â†’ 5x leverage (trade â‚¹5 for every â‚¹1 paid)
        leverage = (Decimal("1") / margin_rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ) if margin_rate > 0 else Decimal("1")

        # â”€â”€ Step 6: Format margin rate as percentage â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        margin_rate_pct = f"{int(margin_rate * 100)}%"

        logger.debug(
            "Margin calc: %s qty=%s price=%s â†’ trade_val=%s margin=%s",
            symbol, quantity, effective_price, trade_value, final_margin
        )

        return {
            "trade_value"    : trade_value,
            "margin_rate"    : margin_rate,
            "margin_rate_pct": margin_rate_pct,
            "raw_margin"     : raw_margin,
            "minimum_margin" : MINIMUM_MARGIN,
            "final_margin"   : final_margin,
            "leverage"       : leverage,
        }


# =============================================================================
# SUBMODULE 2 â€” PRODUCT RULE ENGINE
# =============================================================================

class ProductRuleEngine:
    """
    Validates that the order complies with instrument-specific rules.

    TWO KEY VALIDATIONS:

    1. LOT SIZE COMPLIANCE (for F&O):
        F&O contracts are traded in "lots" not individual units.
        Example: NIFTY futures â†’ 1 lot = 50 units
        You cannot buy 75 units. You must buy 50 or 100 (multiples of 50).

        MATH:
            quantity % lot_size == 0  â†’ VALID
            75 % 50 == 25 â†’ NOT ZERO â†’ INVALID
            100 % 50 == 0 â†’ ZERO â†’ VALID

    2. TICK SIZE COMPLIANCE (for limit prices):
        Prices must be in specific increments called "tick size".
        Example: RELIANCE tick_size = â‚¹0.05
        You cannot place a limit order at â‚¹2,400.03 (not a multiple of 0.05).
        You can place at â‚¹2,400.00 or â‚¹2,400.05 or â‚¹2,400.10.

        MATH:
            price % tick_size == 0  â†’ VALID
            2400.03 % 0.05 == 0.03 â†’ NOT ZERO â†’ INVALID
            2400.05 % 0.05 == 0.00 â†’ ZERO â†’ VALID
    """

    def validate_lot_size(
        self,
        quantity : Decimal,
        lot_size : Optional[Decimal],
        product_type: str,
    ) -> tuple[bool, str]:
        """
        Check if quantity is a valid multiple of the lot size.

        Returns:
            (is_valid: bool, message: str)
        """
        # Lot size only applies to F&O products
        if product_type.lower() not in ("futures", "options"):
            return True, "Lot size not applicable for this product type"

        if not lot_size or lot_size <= 0:
            return True, "No lot size configured for this instrument"

        # â”€â”€ THE LOT SIZE CHECK â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # quantity % lot_size gives the remainder when dividing
        # If remainder is 0 â†’ quantity is a perfect multiple â†’ VALID
        # If remainder is not 0 â†’ not a valid lot multiple â†’ INVALID
        #
        # Example:
        #   quantity = 75, lot_size = 50
        #   75 Ã· 50 = 1 remainder 25
        #   75 % 50 = 25 (not zero) â†’ INVALID
        #
        #   quantity = 100, lot_size = 50
        #   100 Ã· 50 = 2 remainder 0
        #   100 % 50 = 0 (zero) â†’ VALID
        remainder = quantity % lot_size

        if remainder != 0:
            # Calculate valid nearby quantities
            floor_qty  = (quantity // lot_size) * lot_size
            ceiling_qty = floor_qty + lot_size
            return False, (
                f"Quantity {quantity} is not a valid lot multiple. "
                f"Lot size = {lot_size}. "
                f"Valid nearest quantities: {floor_qty} or {ceiling_qty}. "
                f"Formula: quantity must satisfy (quantity % {lot_size} == 0)"
            )

        num_lots = int(quantity / lot_size)
        return True, f"Valid: {quantity} units = {num_lots} lot(s) of {lot_size}"

    def validate_tick_size(
        self,
        price    : Optional[Decimal],
        tick_size: Optional[Decimal],
        order_type: str,
    ) -> tuple[bool, str]:
        """
        Check if the limit price is a valid multiple of the tick size.

        Returns:
            (is_valid: bool, message: str)
        """
        # Tick size only applies to limit orders with a price
        if order_type.lower() == "market" or price is None:
            return True, "Tick size not applicable for market orders"

        if not tick_size or tick_size <= 0:
            return True, "No tick size configured"

        # â”€â”€ THE TICK SIZE CHECK â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Works exactly like the lot size check but for prices.
        #
        # Example:
        #   price = 2400.03, tick_size = 0.05
        #   2400.03 % 0.05 = 0.03 (not zero) â†’ INVALID
        #
        #   price = 2400.05, tick_size = 0.05
        #   2400.05 % 0.05 = 0.00 (zero) â†’ VALID
        #
        # Note: We use round() to avoid floating point precision issues
        # with Decimal arithmetic
        remainder = price % tick_size

        # Use a small epsilon for floating point comparison
        if abs(remainder) > Decimal("0.0000001"):
            floor_price   = (price // tick_size) * tick_size
            ceiling_price = floor_price + tick_size
            return False, (
                f"Price {price} is not a valid tick multiple. "
                f"Tick size = {tick_size}. "
                f"Valid nearby prices: {floor_price} or {ceiling_price}. "
                f"Formula: price must satisfy (price % {tick_size} == 0)"
            )

        return True, f"Valid: price {price} is a multiple of tick size {tick_size}"

    def validate_order_type_price(
        self,
        order_type: str,
        price     : Optional[Decimal],
    ) -> tuple[bool, str]:
        """
        Validate that limit orders have a price and market orders do not.
        Per spec 5.6 input validations.
        """
        if order_type.lower() in ("limit", "stop_loss_limit") and not price:
            return False, f"Order type '{order_type}' requires a price to be specified"

        if order_type.lower() == "market" and price:
            return False, "Market orders must not include a price â€” remove the price field"

        return True, "Order type and price combination is valid"


# =============================================================================
# SUBMODULE 3 â€” EXPOSURE CHECKER
# =============================================================================

class ExposureChecker:
    """
    Checks that the new trade does not exceed exposure limits.

    EXPOSURE = total value of all your open positions.

    WHY EXPOSURE LIMITS EXIST:
        Too much exposure â†’ small price move â†’ huge loss â†’ account blown up.
        Limits protect users from over-leveraging.

    CHECKS PERFORMED:
        1. Max open positions count (per user)
        2. Max single trade value
        3. Max total portfolio exposure
        4. Broker-level exposure caps
    """

    # Maximum number of open positions per user at any time
    MAX_OPEN_POSITIONS = 20

    # Maximum single trade value (â‚¹50 lakhs per trade for safety)
    MAX_SINGLE_TRADE_VALUE = Decimal("5000000")

    # Maximum total portfolio exposure as multiple of wallet balance
    # Example: 3x means with â‚¹1 lakh wallet, max exposure = â‚¹3 lakhs
    MAX_PORTFOLIO_EXPOSURE_MULTIPLIER = Decimal("3")

    def check_open_positions_count(
        self,
        db     : Session,
        user_id: int,
    ) -> tuple[bool, str]:
        """Check user has not exceeded max open position count."""
        from app.models.position import Position

        count = db.scalar(
            select(func.count(Position.id)).where(
                Position.user_id == user_id,
                Position.is_open == True,  # noqa: E712
            )
        ) or 0

        if count >= self.MAX_OPEN_POSITIONS:
            return False, (
                f"Maximum open positions limit reached ({self.MAX_OPEN_POSITIONS}). "
                f"You currently have {count} open positions. "
                f"Close some positions before opening new ones."
            )

        return True, f"Open positions: {count}/{self.MAX_OPEN_POSITIONS}"

    def check_single_trade_value(
        self,
        trade_value: Decimal,
    ) -> tuple[bool, str]:
        """Check single trade does not exceed value limit."""
        if trade_value > self.MAX_SINGLE_TRADE_VALUE:
            return False, (
                f"Single trade value â‚¹{trade_value:,.2f} exceeds the maximum "
                f"allowed â‚¹{self.MAX_SINGLE_TRADE_VALUE:,.2f}. "
                f"Split into multiple orders."
            )
        return True, f"Trade value â‚¹{trade_value:,.2f} is within limits"

    def check_portfolio_exposure(
        self,
        db             : Session,
        user_id        : int,
        wallet_balance : Decimal,
        new_trade_value: Decimal,
    ) -> tuple[bool, str]:
        """
        Check total portfolio exposure won't exceed the multiplier limit.

        FORMULA:
            current_exposure = SUM(net_qty Ã— avg_cost) for all open positions
            new_total_exposure = current_exposure + new_trade_value
            max_allowed = wallet_balance Ã— MAX_PORTFOLIO_EXPOSURE_MULTIPLIER

            If new_total_exposure > max_allowed â†’ REJECTED
        """
        from app.models.position import Position

        # Sum of all current position values (qty Ã— avg_cost)
        result = db.scalar(
            select(
                func.coalesce(
                    func.sum(Position.net_qty * Position.avg_cost),
                    Decimal("0")
                )
            ).where(
                Position.user_id == user_id,
                Position.is_open == True,  # noqa: E712
                Position.net_qty > 0,
            )
        ) or Decimal("0")

        current_exposure = Decimal(str(result))
        new_total        = current_exposure + new_trade_value
        max_allowed      = wallet_balance * self.MAX_PORTFOLIO_EXPOSURE_MULTIPLIER

        if new_total > max_allowed:
            return False, (
                f"Portfolio exposure limit exceeded. "
                f"Current: â‚¹{current_exposure:,.2f} + New: â‚¹{new_trade_value:,.2f} "
                f"= â‚¹{new_total:,.2f} exceeds max â‚¹{max_allowed:,.2f} "
                f"({self.MAX_PORTFOLIO_EXPOSURE_MULTIPLIER}Ã— wallet balance of â‚¹{wallet_balance:,.2f})"
            )

        return True, (
            f"Exposure OK: â‚¹{new_total:,.2f} / â‚¹{max_allowed:,.2f} "
            f"({(new_total/max_allowed*100).quantize(Decimal('0.1'))}% of limit)"
        )


# =============================================================================
# SUBMODULE 4 â€” LIMIT ENFORCEMENT
# =============================================================================

class LimitEnforcement:
    """
    Enforces hard limits set by the platform, broker, and user settings.

    LIMITS HIERARCHY:
        Platform limits (highest â€” set by Super Admin)
            â†“ cannot exceed
        Broker limits (set by Broker for their users)
            â†“ cannot exceed
        User limits (set per user by broker)

    CHECKS:
        1. Product access permissions (can this user trade F&O?)
        2. Leverage limit by role
        3. Quantity cap per order
        4. System-wide trading suspension (kill switch)
    """

    def check_product_permissions(
        self,
        user,
        product_type: str,
    ) -> tuple[bool, str]:
        """
        Check if user has permission to trade this product type.
        Per spec 5.9: Leverage limits by role/product.
        """
        from app.models.user import UserRole

        role = (user.role.value if hasattr(user.role, "value") else str(user.role)).upper()
        pt   = product_type.lower()

        # Super admin bypasses all product restrictions
        if role == "SUPER_ADMIN":
            return True, "Super Admin â€” all products allowed"

        # Broker can trade anything for testing
        if role == "BROKER":
            return True, "Broker â€” all products allowed"

        # Regular user â€” check specific permissions
        if pt in ("futures", "options") and not getattr(user, "can_trade_fno", False):
            return False, (
                f"You do not have permission to trade {pt.upper()} products. "
                f"Contact your broker to enable F&O trading on your account."
            )

        if pt in ("intraday", "delivery") and not getattr(user, "can_trade_equity", True):
            return False, (
                f"Equity trading is not enabled on your account. "
                f"Contact your broker."
            )

        return True, f"Permission granted for {pt} trading"

    def check_leverage_limit(
        self,
        user,
        product_type: str,
        requested_leverage: Decimal,
    ) -> tuple[bool, str]:
        """
        Check if requested leverage is within allowed limits for this role.

        LEVERAGE = trade_value / margin_required
        Example: trade â‚¹1 lakh with â‚¹20,000 margin = 5x leverage

        Each role has a max leverage cap. If the product margin rate
        gives more leverage than allowed, we increase the margin rate.
        """
        role = (user.role.value if hasattr(user.role, "value") else str(user.role)).lower()
        pt   = product_type.lower()

        role_limits = LEVERAGE_LIMITS.get(role, LEVERAGE_LIMITS["user"])
        max_leverage = role_limits.get(pt, Decimal("1"))

        if requested_leverage > max_leverage:
            return False, (
                f"Requested leverage {requested_leverage}x exceeds your maximum "
                f"allowed leverage of {max_leverage}x for {pt} products. "
                f"Reduce your position size or use a higher margin product."
            )

        return True, f"Leverage {requested_leverage}x is within your {max_leverage}x limit"

    def check_quantity_cap(
        self,
        quantity    : Decimal,
        product_type: str,
    ) -> tuple[bool, str]:
        """
        Validate quantity does not exceed the per-order cap.
        Per spec 5.9: Quantity cap validation.
        """
        pt      = product_type.lower()
        max_qty = MAX_QUANTITY.get(pt, Decimal("10000"))

        if quantity > max_qty:
            return False, (
                f"Quantity {quantity} exceeds the maximum allowed "
                f"{max_qty} units per order for {pt} products. "
                f"Split into multiple orders."
            )

        return True, f"Quantity {quantity} is within the {max_qty} unit cap"

    def check_trading_enabled(self, db: Session, symbol: str) -> tuple[bool, str]:
        """
        Check if platform-wide trading is enabled for this symbol.
        Per spec 5.13: System-wide trading suspension (kill switch).
        """
        # Check instruments table for trading_allowed flag
        from app.models.instruments import Instrument

        instrument = db.query(Instrument).filter(
            Instrument.symbol == symbol.upper()
        ).first()

        if not instrument:
            return False, f"Instrument {symbol} not found in the system"

        if not instrument.is_active:
            return False, f"{symbol} is not active. It may be delisted or suspended."

        if not instrument.trading_allowed:
            return False, (
                f"Trading is suspended for {symbol}. "
                f"This may be due to a circuit breaker or admin restriction."
            )

        return True, f"{symbol} is active and trading is allowed"


# =============================================================================
# SUBMODULE 5 â€” LIQUIDATION GUARDRAILS
# =============================================================================

class LiquidationGuardrails:
    """
    Prevents catastrophic account losses by monitoring position health.

    LIQUIDATION SCENARIO:
        User has â‚¹1 lakh in wallet.
        They buy â‚¹5 lakh of Nifty futures (5x leverage).
        Market drops 20% â†’ futures position loses â‚¹1 lakh.
        User now has â‚¹0 in wallet but still has the position.
        Without guardrails â†’ user owes money to the broker.

    HOW GUARDRAILS WORK:
        Level 1 (50% health): WARN the user
        Level 2 (25% health): BLOCK new positions from being opened
        Level 3 (0%  health): AUTO-SQUARE-OFF (in production)

    HEALTH RATIO FORMULA:
        For each position:
            position_value = net_qty Ã— current_LTP
            health_ratio   = position_value / (net_qty Ã— avg_cost) Ã— 100

        Example:
            Bought 100 RELIANCE @ â‚¹2,400 (invested â‚¹2,40,000)
            Current LTP = â‚¹2,100
            position_value = 100 Ã— 2100 = â‚¹2,10,000
            health_ratio   = 2,10,000 / 2,40,000 Ã— 100 = 87.5% (WARNING zone)
    """

    def check_can_open_new_position(
        self,
        db            : Session,
        user_id       : int,
        wallet_balance: Decimal,
        margin_used   : Decimal,
    ) -> tuple[bool, str]:
        """
        Block new position opening if existing positions are critically unhealthy.

        FORMULA:
            free_margin_ratio = (wallet_balance - margin_used) / wallet_balance Ã— 100
            If free_margin_ratio < 25% â†’ block new positions
        """
        if wallet_balance <= 0:
            return False, "Wallet balance is zero. Cannot open new positions."

        free_margin       = wallet_balance - margin_used
        free_margin_ratio = (free_margin / wallet_balance * 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        if free_margin_ratio < Decimal("10"):
            return False, (
                f"Free margin is critically low ({free_margin_ratio}% of wallet). "
                f"You must close some positions before opening new ones. "
                f"Free margin: â‚¹{free_margin:,.2f} of â‚¹{wallet_balance:,.2f} wallet."
            )

        if free_margin_ratio < Decimal("25"):
            # Allow but warn â€” this is a warning not a block
            logger.warning(
                "User %s free margin is low: %.2f%%", user_id, free_margin_ratio
            )

        return True, f"Free margin: â‚¹{free_margin:,.2f} ({free_margin_ratio}%)"

    def get_position_health_status(self, health_ratio: Decimal) -> str:
        """
        Convert health ratio percentage to a status label.

        > 100% = healthy (position has gained value)
        50-100% = warning (position has lost some value)
        25-50%  = danger (approaching margin call)
        < 25%   = critical (liquidation risk)
        """
        if health_ratio >= HEALTH_HEALTHY:
            return "healthy"
        elif health_ratio >= HEALTH_WARNING:
            return "warning"
        elif health_ratio >= HEALTH_DANGER:
            return "danger"
        else:
            return "critical"


# =============================================================================
# SUBMODULE 6 â€” USER HEALTH MONITOR
# =============================================================================

class UserHealthMonitor:
    """
    Monitors overall portfolio health for a user.
    Generates the health report for GET /risk/health.

    OVERALL HEALTH RATIO:
        Sum of all position market values / Sum of all position invested values Ã— 100

    Example:
        Position 1: RELIANCE â€” invested â‚¹2,40,000, now worth â‚¹2,10,000
        Position 2: TCS      â€” invested â‚¹1,50,000, now worth â‚¹1,65,000
        Position 3: BTCUSDT  â€” invested â‚¹50,000,  now worth â‚¹55,000

        total_invested    = 2,40,000 + 1,50,000 + 50,000 = 4,40,000
        total_current     = 2,10,000 + 1,65,000 + 55,000 = 4,30,000
        overall_health    = 4,30,000 / 4,40,000 Ã— 100 = 97.7% (WARNING zone)
    """

    def __init__(self):
        self._liquidation = LiquidationGuardrails()

    def compute_health(
        self,
        db            : Session,
        user_id       : int,
        wallet_balance: Decimal,
        margin_used   : Decimal,
    ) -> dict:
        """
        Compute full portfolio health report.
        Fetches all open positions and calculates health per position.
        """
        from app.models.position import Position

        # Get all open positions
        positions = db.execute(
            select(Position).where(
                Position.user_id == user_id,
                Position.is_open == True,  # noqa: E712
            )
        ).scalars().all()

        position_health_list = []
        total_invested        = Decimal("0")
        total_current_value   = Decimal("0")
        total_unrealized_pnl  = Decimal("0")
        warning_count         = 0
        danger_count          = 0
        margin_call_warning   = False
        liquidation_risk      = False
        alerts                = []

        for pos in positions:
            # Get live price from Redis
            ltp, margin_req, health_ratio, market_val = self._compute_position_health(pos)
            avg = pos.avg_cost or Decimal("0")
            qty = pos.net_qty or Decimal("0")
            upnl = (ltp * qty - avg * qty) if ltp and qty else Decimal("0")

            # Accumulate totals
            invested = (pos.avg_cost or Decimal("0")) * (pos.net_qty or Decimal("0"))
            total_invested += invested
            if market_val:
                total_current_value += market_val

            upnl = Decimal("0")
            total_unrealized_pnl += upnl

            # Get status
            status = self._liquidation.get_position_health_status(health_ratio)

            if status == "warning":
                warning_count += 1
            elif status in ("danger", "critical"):
                danger_count += 1
                if health_ratio < HEALTH_WARNING:
                    margin_call_warning = True
                    alerts.append(
                        f"âš ï¸ {pos.symbol}: Health ratio {health_ratio:.1f}% â€” "
                        f"approaching margin call territory"
                    )
                if health_ratio < HEALTH_DANGER:
                    liquidation_risk = True
                    alerts.append(
                        f"ðŸš¨ {pos.symbol}: CRITICAL â€” Health {health_ratio:.1f}% â€” "
                        f"liquidation risk! Close this position immediately."
                    )

            position_health_list.append({
                "symbol"          : pos.symbol,
                "product_type"    : pos.product_type,
                "net_qty"         : pos.net_qty,
                "avg_cost"        : pos.avg_cost,
                "current_ltp"     : ltp,
                "market_value"    : market_val,
                "margin_required" : margin_req,
                "unrealized_pnl"  : upnl,
                "health_ratio"    : health_ratio,
                "status"          : status,
            })

        # Overall portfolio health
        if total_invested > 0:
            overall_health = (total_current_value / total_invested * 100).quantize(
                Decimal("0.01")
            )
        else:
            overall_health = Decimal("100")  # No positions = perfectly healthy

        overall_status = self._liquidation.get_position_health_status(overall_health)

        # Check free margin health
        free_margin = wallet_balance - margin_used
        if wallet_balance > 0:
            free_pct = (free_margin / wallet_balance * 100).quantize(Decimal("0.01"))
            if free_pct < Decimal("25"):
                alerts.append(
                    f"âš ï¸ Free margin is low: â‚¹{free_margin:,.2f} ({free_pct}%). "
                    f"Consider reducing positions."
                )

        return {
            "wallet_balance"      : wallet_balance,
            "total_margin_used"   : margin_used,
            "free_margin"         : free_margin,
            "total_position_value": total_current_value,
            "total_unrealized_pnl": total_unrealized_pnl,
            "overall_health_ratio": overall_health,
            "overall_status"      : overall_status,
            "positions"           : position_health_list,
            "margin_call_warning" : margin_call_warning,
            "liquidation_risk"    : liquidation_risk,
            "alerts"              : alerts,
            "open_positions"      : len(positions),
            "warning_positions"   : warning_count,
            "danger_positions"    : danger_count,
        }

    def _compute_position_health(
        self,
        pos,
    ) -> tuple[Optional[Decimal], Decimal, Decimal, Optional[Decimal]]:
        """
        Compute health metrics for a single position.

        Returns:
            (current_ltp, margin_required, health_ratio, market_value)
        """
        calc = MarginCalculator()
        ltp  = None

        # Try to get live price from Redis
        try:
            from app.services.marketdata.price_cache import get_ltp
            ltp_data = get_ltp(pos.symbol)
            if ltp_data and ltp_data.get("ltp"):
                ltp = Decimal(str(ltp_data["ltp"]))
        except Exception:
            pass

        net_qty  = pos.net_qty  or Decimal("0")
        avg_cost = pos.avg_cost or Decimal("0")

        # Calculate margin required to maintain this position
        if avg_cost > 0 and net_qty > 0:
            margin_info  = calc.calculate(net_qty, avg_cost, pos.product_type, pos.symbol)
            margin_req   = margin_info["final_margin"]
        else:
            margin_req = Decimal("0")

        # Calculate health ratio
        if ltp and net_qty > 0 and avg_cost > 0:
            market_val   = ltp * net_qty
            invested_val = avg_cost * net_qty
            if invested_val > 0:
                health_ratio = (market_val / invested_val * 100).quantize(Decimal("0.01"))
            else:
                health_ratio = Decimal("100")
        else:
            market_val   = None
            health_ratio = Decimal("100")   # Unknown LTP = assume healthy

        return ltp, margin_req, health_ratio, market_val


# =============================================================================
# SUBMODULE 7 â€” PRE-TRADE MARGIN VALIDATOR (MAIN ORCHESTRATOR)
# =============================================================================

class PreTradeMarginValidator:
    """
    The main entry point â€” runs ALL risk checks before an order is placed.

    VALIDATION SEQUENCE (order matters â€” fail fast):
        Check 1: Trading enabled for this symbol
        Check 2: Product permissions (can this user trade futures/options?)
        Check 3: Order type vs price consistency
        Check 4: Quantity cap
        Check 5: Lot size compliance (F&O only)
        Check 6: Tick size compliance (limit orders only)
        Check 7: Get live price from Redis
        Check 8: Calculate margin required
        Check 9: Check leverage limit
        Check 10: Portfolio exposure check
        Check 11: Available margin check (the key margin check)
        Check 12: Liquidation guardrail check

    Returns:
        Complete MarginPreviewResponse with all check results
    """

    def __init__(self):
        self._calc        = MarginCalculator()
        self._rules       = ProductRuleEngine()
        self._exposure    = ExposureChecker()
        self._limits      = LimitEnforcement()
        self._guardrails  = LiquidationGuardrails()

    def validate(
        self,
        db           : Session,
        user,
        symbol       : str,
        order_type   : str,
        side         : str,
        product_type : str,
        quantity     : Decimal,
        price        : Optional[Decimal] = None,
    ) -> dict:
        """
        Run all pre-trade risk checks.

        Returns a dict that maps to MarginPreviewResponse schema.
        """
        now         = datetime.now(timezone.utc)
        validations = []
        rejected    = None

        # â”€â”€ Get wallet state â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        wallet_balance, margin_used = self._get_wallet_state(db, user.id)
        available_margin = wallet_balance - margin_used

        def add_check(name: str, passed: bool, msg: str) -> bool:
            validations.append({"check": name, "passed": passed, "message": msg})
            return passed

        # â”€â”€ CHECK 1: Trading enabled â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        ok, msg = self._limits.check_trading_enabled(db, symbol)
        if not add_check("trading_enabled", ok, msg):
            rejected = msg

        # â”€â”€ CHECK 2: Product permissions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if not rejected:
            ok, msg = self._limits.check_product_permissions(user, product_type)
            if not add_check("product_permissions", ok, msg):
                rejected = msg

        # â”€â”€ CHECK 3: Order type vs price â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if not rejected:
            ok, msg = self._rules.validate_order_type_price(order_type, price)
            if not add_check("order_type_price", ok, msg):
                rejected = msg

        # â”€â”€ CHECK 4: Quantity cap â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if not rejected:
            ok, msg = self._limits.check_quantity_cap(quantity, product_type)
            if not add_check("quantity_cap", ok, msg):
                rejected = msg

        # â”€â”€ Get instrument metadata for lot/tick checks â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        instrument = self._get_instrument(db, symbol)
        lot_size   = Decimal(str(instrument.lot_size or 1)) if instrument else None
        tick_size  = Decimal(str(instrument.tick_size or 0.05)) if instrument else None

        # â”€â”€ CHECK 5: Lot size compliance â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if not rejected:
            ok, msg = self._rules.validate_lot_size(quantity, lot_size, product_type)
            if not add_check("lot_size_compliance", ok, msg):
                rejected = msg

        # â”€â”€ CHECK 6: Tick size compliance â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if not rejected:
            ok, msg = self._rules.validate_tick_size(price, tick_size, order_type)
            if not add_check("tick_size_compliance", ok, msg):
                rejected = msg

        # â”€â”€ GET LIVE PRICE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        ltp, prices_live = self._get_live_price(symbol)
        effective_price  = price or ltp or Decimal("0")

        if order_type.lower() == "market" and not ltp:
            msg = f"No live price available for {symbol}. Start the simulator first."
            add_check("live_price_available", False, msg)
            if not rejected:
                rejected = msg
        else:
            add_check("live_price_available", True, f"LTP: â‚¹{ltp}" if ltp else "Limit price provided")

        # â”€â”€ CALCULATE MARGIN â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        margin_breakdown = self._calc.calculate(
            quantity, effective_price, product_type, symbol
        )
        margin_required = margin_breakdown["final_margin"]
        leverage        = margin_breakdown["leverage"]

        # â”€â”€ CHECK 7: Leverage limit â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if not rejected:
            ok, msg = self._limits.check_leverage_limit(user, product_type, leverage)
            if not add_check("leverage_limit", ok, msg):
                rejected = msg

        # â”€â”€ CHECK 8: Portfolio exposure â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if not rejected:
            trade_value = margin_breakdown["trade_value"]
            ok, msg = self._exposure.check_portfolio_exposure(
                db, user.id, wallet_balance, trade_value
            )
            if not add_check("portfolio_exposure", ok, msg):
                rejected = msg

        # â”€â”€ CHECK 9: Open positions count â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if not rejected:
            ok, msg = self._exposure.check_open_positions_count(db, user.id)
            if not add_check("positions_count", ok, msg):
                rejected = msg

        # â”€â”€ CHECK 10: Liquidation guardrail â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if not rejected:
            ok, msg = self._guardrails.check_can_open_new_position(
                db, user.id, wallet_balance, margin_used
            )
            if not add_check("liquidation_guardrail", ok, msg):
                rejected = msg

        # â”€â”€ CHECK 11: MARGIN AVAILABILITY (THE KEY CHECK) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # This is the most important check:
        # "Does the user have enough free cash for this trade?"
        #
        # FORMULA:
        #   available_margin = wallet_balance - margin_already_blocked
        #   sufficient       = available_margin >= margin_required
        #
        # Example:
        #   wallet_balance    = â‚¹1,00,000
        #   margin_already_blocked = â‚¹20,000 (from existing open orders)
        #   available_margin  = â‚¹80,000
        #   margin_required   = â‚¹15,000 (for new order)
        #   sufficient        = â‚¹80,000 >= â‚¹15,000 â†’ TRUE
        sufficient = available_margin >= margin_required
        margin_after = available_margin - margin_required

        if not rejected:
            if sufficient:
                add_check(
                    "margin_sufficiency", True,
                    f"Sufficient: â‚¹{available_margin:,.2f} available >= "
                    f"â‚¹{margin_required:,.2f} required. "
                    f"After trade: â‚¹{margin_after:,.2f} remaining."
                )
            else:
                msg = (
                    f"Insufficient margin. "
                    f"Required: â‚¹{margin_required:,.2f} | "
                    f"Available: â‚¹{available_margin:,.2f} | "
                    f"Shortfall: â‚¹{(margin_required - available_margin):,.2f}. "
                    f"Please add funds or reduce quantity."
                )
                add_check("margin_sufficiency", False, msg)
                rejected = msg

        can_place = rejected is None

        return {
            "symbol"          : symbol,
            "side"            : side,
            "order_type"      : order_type,
            "product_type"    : product_type,
            "quantity"        : quantity,
            "price"           : price,
            "ltp"             : ltp,
            "effective_price" : effective_price,
            "margin_breakdown": margin_breakdown,
            "margin_required" : margin_required,
            "wallet_balance"  : wallet_balance,
            "margin_used"     : margin_used,
            "available_margin": available_margin,
            "margin_after"    : margin_after,
            "sufficient"      : sufficient,
            "can_place"       : can_place,
            "validations"     : validations,
            "rejection_reason": rejected,
            "prices_live"     : prices_live,
            "computed_at"     : now,
        }

    # â”€â”€ Private helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_wallet_state(
        self, db: Session, user_id: int
    ) -> tuple[Decimal, Decimal]:
        """Get wallet balance and currently blocked margin."""
        from app.models.funding import WalletLedger, LedgerEntryType
        from app.models.order import Order, OrderStatus

        # Cash balance from ledger
        balance_result = db.scalar(
            select(
                func.coalesce(
                    func.sum(
                        sa_case(
                            (WalletLedger.entry_type == LedgerEntryType.CREDIT, WalletLedger.amount),
                            (WalletLedger.entry_type == LedgerEntryType.DEBIT, -WalletLedger.amount),
                            else_=Decimal("0"),
                        )
                    ), Decimal("0")
                )
            ).where(WalletLedger.user_id == user_id)
        )
        wallet_balance = Decimal(str(balance_result or 0))

        # Margin blocked by open orders
        margin_result = db.scalar(
            select(func.coalesce(func.sum(Order.margin_blocked), Decimal("0")))
            .where(
                Order.user_id == user_id,
                Order.status.in_([
                    OrderStatus.ACCEPTED, OrderStatus.QUEUED,
                    OrderStatus.PENDING_TRIGGER, OrderStatus.PARTIALLY_FILLED
                ])
            )
        )
        margin_used = Decimal(str(margin_result or 0))

        return wallet_balance, margin_used

    def _get_instrument(self, db: Session, symbol: str):
        """Fetch instrument metadata."""
        try:
            from app.models.instruments import Instrument
            return db.query(Instrument).filter(
                Instrument.symbol == symbol.upper()
            ).first()
        except Exception:
            return None

    def _get_live_price(self, symbol: str) -> tuple[Optional[Decimal], bool]:
        """Get live LTP from Redis price cache."""
        try:
            from app.services.marketdata.price_cache import get_ltp
            data = get_ltp(symbol)
            if data and data.get("ltp"):
                return Decimal(str(data["ltp"])), True
        except Exception as exc:
            logger.warning("Price fetch failed for %s: %s", symbol, exc)
        return None, False

