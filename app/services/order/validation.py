import logging
from decimal import Decimal, ROUND_DOWN
from sqlalchemy import select, func, case as sa_case
from sqlalchemy.orm import Session
from app.models.order import Order, OrderStatus
from app.models.user import User, UserStatus, KYCStatus, UserRole
from app.models.instruments import Instrument, InstrumentType
from app.models.funding import WalletLedger, LedgerEntryType
from app.schemas.order import OrderPlaceRequest

logger = logging.getLogger(__name__)

MARGIN_RATES = {
    InstrumentType.EQUITY  : Decimal("0.20"),
    InstrumentType.ETF     : Decimal("0.25"),
    InstrumentType.INDEX   : Decimal("0.15"),
    InstrumentType.FUTURES : Decimal("0.15"),
    InstrumentType.OPTIONS : Decimal("1.00"),
    InstrumentType.CRYPTO  : Decimal("1.00"),
}

def _get_current_price(symbol):
    try:
        from app.services.marketdata.price_cache import get_ltp
        ltp_data = get_ltp(symbol)
        if ltp_data and ltp_data.get("ltp"):
            return Decimal(str(ltp_data["ltp"]))
    except Exception as exc:
        logger.warning("Price cache read failed for %s: %s", symbol, exc)
    return None

def _get_free_balance(db, user_id):
    result = db.scalar(
        select(
            func.coalesce(
                func.sum(
                    sa_case(
                        (WalletLedger.entry_type == LedgerEntryType.CREDIT,  WalletLedger.amount),
                        (WalletLedger.entry_type == LedgerEntryType.DEBIT,  -WalletLedger.amount),
                        else_=Decimal("0"),
                    )
                ),
                Decimal("0"),
            )
        ).where(WalletLedger.user_id == user_id)
    )
    return Decimal(str(result or 0))

def _get_blocked_margin(db, user_id):
    result = db.scalar(
        select(
            func.coalesce(
                func.sum(Order.margin_blocked),
                Decimal("0"),
            )
        ).where(
            Order.user_id == user_id,
            Order.status.in_([
                OrderStatus.ACCEPTED,
                OrderStatus.QUEUED,
                OrderStatus.PENDING_TRIGGER,
                OrderStatus.PARTIALLY_FILLED,
            ])
        )
    )
    return Decimal(str(result or 0))

class ValidationResult:
    def __init__(self):
        self.passed           = True
        self.rejection_reason = None
        self.instrument       = None
        self.current_price    = None
        self.margin_required  = Decimal("0")

def run_all_validations(db, user, payload):
    result = ValidationResult()
    try:
        if user.status != UserStatus.ACTIVE:
            result.passed = False
            result.rejection_reason = f"Account not active: {user.status.value}"
            return result

        if user.role == UserRole.USER and user.kyc_status != KYCStatus.APPROVED:
            result.passed = False
            result.rejection_reason = f"KYC required. Status: {user.kyc_status.value}"
            return result

        pt = payload.product_type.value
        if user.role not in (UserRole.SUPER_ADMIN, UserRole.BROKER):
            perms = {"intraday": user.can_trade_equity, "delivery": user.can_trade_equity,
                     "futures": user.can_trade_fno, "options": user.can_trade_fno}
            if not perms.get(pt, False):
                result.passed = False
                result.rejection_reason = f"No permission to trade {pt.upper()}"
                return result

        instrument = db.query(Instrument).filter(
            Instrument.symbol == payload.symbol.upper()
        ).first()
        if not instrument:
            result.passed = False
            result.rejection_reason = f"Instrument not found: {payload.symbol}"
            return result
        if not instrument.is_active:
            result.passed = False
            result.rejection_reason = f"{payload.symbol} is not active"
            return result
        if not instrument.trading_allowed:
            result.passed = False
            result.rejection_reason = f"Trading suspended for {payload.symbol}"
            return result
        result.instrument = instrument

        if payload.quantity <= 0:
            result.passed = False
            result.rejection_reason = "Quantity must be > 0"
            return result

        if instrument.instrument_type in (InstrumentType.FUTURES, InstrumentType.OPTIONS):
            lot_size = Decimal(str(instrument.lot_size or 1))
            if lot_size > 0 and payload.quantity % lot_size != 0:
                result.passed = False
                result.rejection_reason = f"Quantity must be multiple of lot size {lot_size}"
                return result

        if payload.price and instrument.tick_size:
            tick = Decimal(str(instrument.tick_size))
            if tick > 0 and payload.price % tick != 0:
                result.passed = False
                result.rejection_reason = f"Price must be multiple of tick size {tick}"
                return result

        current_price = _get_current_price(payload.symbol.upper())
        if payload.order_type.value == "market" and current_price is None:
            result.passed = False
            result.rejection_reason = f"No live price for {payload.symbol}. Start simulator first."
            return result

        effective_price = payload.price or current_price or Decimal("0")
        result.current_price = current_price

        if effective_price > 0:
            rate          = MARGIN_RATES.get(instrument.instrument_type, Decimal("1.00"))
            margin_needed = (payload.quantity * effective_price * rate).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            free_balance  = _get_free_balance(db, user.id)
            blocked       = _get_blocked_margin(db, user.id)
            available     = free_balance - blocked
            if available < margin_needed:
                result.passed = False
                result.rejection_reason = (
                    f"Insufficient margin. Required: {margin_needed:.2f}, "
                    f"Available: {available:.2f} (Balance: {free_balance:.2f}, Blocked: {blocked:.2f}). "
                    f"Ask broker to credit your account."
                )
                return result
            result.margin_required = margin_needed

    except Exception as exc:
        logger.exception("Validation error: %s", exc)
        result.passed = False
        result.rejection_reason = f"Validation error: {exc}"
    return result
