# app/models/__init__.py
# Registers ALL models with SQLAlchemy Base.
# Every model imported here gets its table auto-created on server startup.

# Module 1 — Auth
from app.models.user          import User, UserRole, UserStatus, KYCStatus
from app.models.refresh_token import RefreshToken
from app.models.login_audit   import LoginAudit

# Module 2 — Users & Brokers
from app.models.broker        import Broker
from app.models.user_settings import UserSettings

# Module 3 — KYC
from app.models.kyc           import KYCRequest, KYCDocument, KYCReviewLog

# Module 4 — Instruments
from app.models.instruments  import Instrument, InstrumentSyncLog

# Module 10 — Funding
from app.models.funding       import FundingLog, WalletLedger, BalanceSnapshot

# Module 6 — Orders
from app.models.order         import Order, OrderEvent, OrderTrigger

# Module 7 — Execution Engine
from app.models.fill          import OrderFill, TradeLog, ExecutionEvent
from app.models.position      import Position
