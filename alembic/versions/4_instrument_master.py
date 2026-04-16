"""
All Modules 1-4 — Full schema: users, brokers, KYC, funding, instruments
Revision ID: 0001_all_modules
Revises:
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision      = "0001_all_modules"
down_revision = None
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── Extension ──────────────────────────────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ── Enums ──────────────────────────────────────────────────────────────
    op.execute("CREATE TYPE userrole     AS ENUM ('super_admin','broker','user')")
    op.execute("CREATE TYPE userstatus   AS ENUM ('active','suspended','pending_activation','pending_kyc')")
    op.execute("CREATE TYPE kycstatus    AS ENUM ('not_submitted','pending','approved','rejected')")
    op.execute("CREATE TYPE kycreqstatus AS ENUM ('submitted','under_review','approved','rejected','resubmit_required')")
    op.execute("CREATE TYPE documenttype AS ENUM ('pan_card','aadhaar_front','aadhaar_back','passport','driving_license','bank_statement','selfie')")
    op.execute("CREATE TYPE reviewaction AS ENUM ('approved','rejected','requested_more_info')")
    op.execute("CREATE TYPE fundingtype  AS ENUM ('deposit','withdrawal')")
    op.execute("CREATE TYPE fundingstatus AS ENUM ('pending','approved','rejected','processing')")
    op.execute("CREATE TYPE ledgertype   AS ENUM ('credit','debit','fee','adjustment')")
    op.execute("CREATE TYPE instrumenttype AS ENUM ('equity','etf','futures','options','index','crypto')")
    op.execute("CREATE TYPE segment      AS ENUM ('cash','fno','crypto')")
    op.execute("CREATE TYPE optiontype   AS ENUM ('CE','PE')")
    op.execute("CREATE TYPE pricesource  AS ENUM ('kite','binance','manual')")

    # ── Module 1: Auth — brokers, users, refresh_tokens, login_audit ──────
    op.create_table("brokers",
        sa.Column("id",            sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("email",         sa.String(255), unique=True, nullable=False),
        sa.Column("full_name",     sa.String(255), nullable=False),
        sa.Column("company_name",  sa.String(255), nullable=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("is_active",     sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at",    sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at",    sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table("users",
        sa.Column("id",              sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("email",           sa.String(255), unique=True, nullable=False, index=True),
        sa.Column("full_name",       sa.String(255), nullable=False),
        sa.Column("phone",           sa.String(20),  nullable=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("role",            sa.Enum("super_admin","broker","user", name="userrole"),
                  nullable=False, server_default="user"),
        sa.Column("status",          sa.Enum("active","suspended","pending_activation","pending_kyc",
                  name="userstatus"), nullable=False, server_default="pending_activation"),
        sa.Column("kyc_status",      sa.Enum("not_submitted","pending","approved","rejected",
                  name="kycstatus"), nullable=False, server_default="not_submitted"),
        sa.Column("broker_id",       sa.Integer, sa.ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_login_at",   sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at",      sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at",      sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table("refresh_tokens",
        sa.Column("id",         sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id",    sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("token",      sa.Text, nullable=False, unique=True),
        sa.Column("is_revoked", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table("login_audit",
        sa.Column("id",         sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id",    sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column("success",    sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table("user_settings",
        sa.Column("id",             sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id",        sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("leverage_equity", sa.Numeric(5,2), nullable=False, server_default="5.0"),
        sa.Column("leverage_fno",    sa.Numeric(5,2), nullable=False, server_default="2.0"),
        sa.Column("leverage_crypto", sa.Numeric(5,2), nullable=False, server_default="1.0"),
        sa.Column("leverage_etf",    sa.Numeric(5,2), nullable=False, server_default="3.0"),
        sa.Column("leverage_index",  sa.Numeric(5,2), nullable=False, server_default="2.0"),
        sa.Column("created_at",      sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at",      sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── Module 3: KYC ─────────────────────────────────────────────────────
    op.create_table("kyc_requests",
        sa.Column("id",           sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id",      sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("status",       sa.Enum("submitted","under_review","approved","rejected",
                  "resubmit_required", name="kycreqstatus"), nullable=False, server_default="submitted"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("reviewed_at",  sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewer_id",  sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("notes",        sa.Text, nullable=True),
        sa.Column("created_at",   sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at",   sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table("kyc_documents",
        sa.Column("id",             sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("kyc_request_id", sa.Integer, sa.ForeignKey("kyc_requests.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("document_type",  sa.Enum("pan_card","aadhaar_front","aadhaar_back","passport",
                  "driving_license","bank_statement","selfie", name="documenttype"), nullable=False),
        sa.Column("file_path",      sa.String(500), nullable=False),
        sa.Column("file_name",      sa.String(255), nullable=True),
        sa.Column("uploaded_at",    sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table("kyc_review_logs",
        sa.Column("id",             sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("kyc_request_id", sa.Integer, sa.ForeignKey("kyc_requests.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("reviewer_id",    sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action",         sa.Enum("approved","rejected","requested_more_info",
                  name="reviewaction"), nullable=False),
        sa.Column("notes",          sa.Text, nullable=True),
        sa.Column("created_at",     sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── Module 2: Funding ─────────────────────────────────────────────────
    op.create_table("funding_logs",
        sa.Column("id",              sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id",         sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("type",            sa.Enum("deposit","withdrawal", name="fundingtype"), nullable=False),
        sa.Column("amount",          sa.Numeric(18,2), nullable=False),
        sa.Column("status",          sa.Enum("pending","approved","rejected","processing",
                  name="fundingstatus"), nullable=False, server_default="pending"),
        sa.Column("reference",       sa.String(100), nullable=True),
        sa.Column("notes",           sa.Text, nullable=True),
        sa.Column("approved_by_id",  sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("approved_at",     sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at",      sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at",      sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table("wallet_ledger",
        sa.Column("id",           sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id",      sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("entry_type",   sa.Enum("credit","debit","fee","adjustment", name="ledgertype"),
                  nullable=False),
        sa.Column("amount",       sa.Numeric(18,2), nullable=False),
        sa.Column("balance_after", sa.Numeric(18,2), nullable=False),
        sa.Column("reference_id", sa.Integer, nullable=True),
        sa.Column("description",  sa.Text, nullable=True),
        sa.Column("created_at",   sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table("balance_snapshots",
        sa.Column("id",              sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id",         sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("available_balance", sa.Numeric(18,2), nullable=False, server_default="0"),
        sa.Column("used_margin",       sa.Numeric(18,2), nullable=False, server_default="0"),
        sa.Column("total_balance",     sa.Numeric(18,2), nullable=False, server_default="0"),
        sa.Column("snapshot_at",       sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── Module 4: Instruments ─────────────────────────────────────────────
    op.create_table("instruments",
        sa.Column("id",               postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("symbol",           sa.String(100), unique=True, nullable=False, index=True),
        sa.Column("external_symbol",  sa.String(100), nullable=False, index=True),
        sa.Column("external_token",   sa.String(100), nullable=True),
        sa.Column("exchange",         sa.String(20),  nullable=False, index=True),
        sa.Column("display_name",     sa.String(200), nullable=False),
        sa.Column("instrument_type",  sa.Enum("equity","etf","futures","options","index","crypto",
                  name="instrumenttype"), nullable=False, index=True),
        sa.Column("segment",          sa.Enum("cash","fno","crypto", name="segment"),
                  nullable=False, index=True),
        sa.Column("tick_size",        sa.Numeric(18,8), nullable=False, server_default="0.05"),
        sa.Column("lot_size",         sa.Numeric(18,8), nullable=False, server_default="1"),
        sa.Column("expiry",           sa.Date, nullable=True),
        sa.Column("strike",           sa.Numeric(18,4), nullable=True),
        sa.Column("option_type",      sa.Enum("CE","PE", name="optiontype"), nullable=True),
        sa.Column("underlying_symbol", sa.String(100), nullable=True),
        sa.Column("price_source",     sa.Enum("kite","binance","manual", name="pricesource"),
                  nullable=False, server_default="kite"),
        sa.Column("is_active",        sa.Boolean, nullable=False, server_default="true", index=True),
        sa.Column("trading_allowed",  sa.Boolean, nullable=False, server_default="true"),
        sa.Column("is_index",         sa.Boolean, nullable=False, server_default="false"),
        sa.Column("isin",             sa.String(20),  nullable=True),
        sa.Column("series",           sa.String(10),  nullable=True),
        sa.Column("metadata_json",    postgresql.JSONB, nullable=True),
        sa.Column("created_at",       sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at",       sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.execute("CREATE INDEX ix_instruments_symbol_trgm ON instruments USING GIN (symbol gin_trgm_ops)")
    op.execute("CREATE INDEX ix_instruments_name_trgm ON instruments USING GIN (display_name gin_trgm_ops)")

    op.create_table("instrument_sync_logs",
        sa.Column("id",                     sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source",                 sa.Enum("kite","binance","manual", name="pricesource"),
                  nullable=False),
        sa.Column("instruments_upserted",   sa.Integer, nullable=False, server_default="0"),
        sa.Column("instruments_deactivated", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error",                  sa.Text, nullable=True),
        sa.Column("synced_at",              sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("instrument_sync_logs")
    op.drop_table("instruments")
    op.drop_table("balance_snapshots")
    op.drop_table("wallet_ledger")
    op.drop_table("funding_logs")
    op.drop_table("kyc_review_logs")
    op.drop_table("kyc_documents")
    op.drop_table("kyc_requests")
    op.drop_table("user_settings")
    op.drop_table("login_audit")
    op.drop_table("refresh_tokens")
    op.drop_table("users")
    op.drop_table("brokers")
    for t in ["pricesource","optiontype","segment","instrumenttype",
              "ledgertype","fundingstatus","fundingtype","reviewaction",
              "documenttype","kycreqstatus","kycstatus","userstatus","userrole"]:
        op.execute(f"DROP TYPE IF EXISTS {t}")
