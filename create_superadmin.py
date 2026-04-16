import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv()
from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.user import User, UserRole, UserStatus, KYCStatus

def create_superadmin():
    db = SessionLocal()
    try:
        email    = os.getenv("SUPERADMIN_EMAIL", "admin@arc.com")
        password = os.getenv("SUPERADMIN_PASSWORD", "Admin@1234")
        existing = db.query(User).filter(User.email == email).first()
        if existing:
            print(f"Super Admin already exists: {email}")
            return
        admin = User(
            email           = email,
            full_name       = "Super Admin",
            hashed_password = hash_password(password),
            role            = UserRole.SUPER_ADMIN,
            status          = UserStatus.ACTIVE,
            kyc_status      = KYCStatus.APPROVED,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        print("Super Admin created!")
        print(f"  Email:    {email}")
        print(f"  Password: {password}")
    except Exception as e:
        print(f"Error: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    create_superadmin()
