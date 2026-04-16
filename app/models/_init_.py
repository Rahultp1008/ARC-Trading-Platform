# app/models/__init__.py — register all models so Alembic sees every table
from app.models.broker        import Broker                                    # noqa: F401
from app.models.user          import User, UserRole, UserStatus, KYCStatus     # noqa: F401
from app.models.user_settings import UserSettings                              # noqa: F401
from app.models.kyc           import KYCRequest, KYCDocument, KYCReviewLog    # noqa: F401
from app.models.funding       import FundingLog, WalletLedger, BalanceSnapshot # noqa: F401
from app.models.login_audit   import LoginAudit                                # noqa: F401
from app.models.refresh_token import RefreshToken                              # noqa: F401
from app.models.instruments   import Instrument, InstrumentSyncLog             # noqa: F401
