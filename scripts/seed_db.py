#!/usr/bin/env python3
"""
seed_db.py — Initialize SOMNI-Guard gateway database with default admin user and demo patient.

Run after the gateway venv is created but before the service starts.
Safe to run multiple times — only creates records if they don't already exist.

Usage:
    python3 scripts/seed_db.py
"""

import sys
import os
import secrets
import bcrypt
from datetime import datetime, timedelta

# Add somniguard_gateway to path so we can import modules
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GATEWAY_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "somniguard_gateway")
sys.path.insert(0, GATEWAY_DIR)

import database as db
import config as cfg
from security import validate_password_complexity

# Sample patient names (realistic for medical monitoring context)
PATIENT_NAMES = [
    "James Johnson", "Mary Smith", "Robert Brown", "Patricia Williams",
    "Michael Davis", "Linda Miller", "William Wilson", "Barbara Moore",
    "David Taylor", "Elizabeth Anderson", "Richard Thomas", "Jennifer Taylor",
    "Joseph Jackson", "Susan White", "Thomas Harris", "Jessica Martin",
    "Charles Thompson", "Sarah Garcia", "Christopher Martinez", "Karen Robinson",
]


def generate_secure_password():
    """Generate a password that satisfies validate_password_complexity().

    Requirements:
    - 14-128 chars
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character
    """
    import string

    # Compose password from required character classes
    special_chars = "!@#$%^&*"

    # Get 8 random lowercase, 4 uppercase, 3 digits, 2 special
    lowercase = ''.join(secrets.choice(string.ascii_lowercase) for _ in range(8))
    uppercase = ''.join(secrets.choice(string.ascii_uppercase) for _ in range(4))
    digits = ''.join(secrets.choice(string.digits) for _ in range(3))
    special = ''.join(secrets.choice(special_chars) for _ in range(2))

    # Combine and shuffle
    pwd_chars = list(lowercase + uppercase + digits + special)
    secrets.SystemRandom().shuffle(pwd_chars)

    return ''.join(pwd_chars)


def seed_admin_user():
    """Create an admin user if none exists. Returns (created, username, password)."""
    existing = db.list_users()
    if existing:
        print("[SEED] Admin user already exists. Skipping user creation.")
        return False, None, None

    username = "admin"
    email = "admin@somniguard.local"
    password = generate_secure_password()

    # Validate password complexity before hashing
    valid, errors = validate_password_complexity(password)
    if not valid:
        print("[SEED] ERROR: Generated password failed validation:")
        for err in errors:
            print(f"  - {err}")
        return False, None, None

    pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")

    try:
        db.create_user(username=username, email=email, password_hash=pwd_hash, role="admin")
        print(f"[SEED] ✓ Created admin user: {username}")
        return True, username, password
    except Exception as e:
        print(f"[SEED] ERROR: Could not create admin user: {e}")
        return False, None, None


def seed_demo_patient(created_by_user_id=1):
    """Create a demo patient if none exist. Returns (created, name, dob)."""
    existing = db.list_patients()
    if existing:
        print("[SEED] Patients already exist. Skipping patient creation.")
        return False, None, None

    name = secrets.choice(PATIENT_NAMES)
    # Random DOB between 1940 and 1990
    year = secrets.randbelow(50) + 1940
    month = secrets.randbelow(12) + 1
    day = secrets.randbelow(28) + 1  # Safe day range (1-28)
    dob = f"{year:04d}-{month:02d}-{day:02d}"

    try:
        db.create_patient(
            name=name,
            dob=dob,
            notes="Demo patient for testing SOMNI-Guard sleep monitoring.",
            created_by=created_by_user_id,
        )
        print(f"[SEED] ✓ Created demo patient: {name} (DOB: {dob})")
        return True, name, dob
    except Exception as e:
        print(f"[SEED] ERROR: Could not create demo patient: {e}")
        return False, None, None


def main():
    """Initialize the database and seed default records."""
    print("")
    print("=" * 60)
    print("  SOMNI-Guard Database Seeding")
    print("=" * 60)
    print("")

    # Ensure the database directory exists
    db_dir = os.path.dirname(cfg.DB_PATH)
    if db_dir:
        try:
            os.makedirs(db_dir, exist_ok=True)
            print(f"[SEED] Database directory ready: {db_dir}")
        except Exception as e:
            print(f"[SEED] ERROR: Could not create database directory: {e}")
            return 1

    # Initialize the database (idempotent)
    print("[SEED] Initializing database schema…")
    try:
        db.init_db()
        print("[SEED] ✓ Database schema initialized")
    except Exception as e:
        print(f"[SEED] ERROR: Database init failed: {e}")
        import traceback
        traceback.print_exc()
        return 1

    # Seed admin user
    print("")
    try:
        admin_created, admin_user, admin_password = seed_admin_user()
    except Exception as e:
        print(f"[SEED] ERROR: Admin seeding failed: {e}")
        import traceback
        traceback.print_exc()
        return 1

    # Seed demo patient (only if admin user was created)
    print("")
    patient_created = False
    patient_name = None
    patient_dob = None
    if admin_created:
        try:
            # Get the admin user ID (should be 1 if just created)
            users = db.list_users()
            admin_id = users[0]["id"] if users else 1
            patient_created, patient_name, patient_dob = seed_demo_patient(created_by_user_id=admin_id)
        except Exception as e:
            print(f"[SEED] ERROR: Patient seeding failed: {e}")
            import traceback
            traceback.print_exc()
            # Don't fail if patient creation fails; the admin user is created
            patient_created = False
    else:
        print("[SEED] Skipping patient creation (admin user not created)")

    # Print a clear summary
    print("")
    print("=" * 60)
    print("  ✓ Seeding Complete")
    print("=" * 60)
    print("")

    if admin_created:
        print("  🔐 ADMIN CREDENTIALS (save these!):")
        print(f"     Username: {admin_user}")
        print(f"     Password: {admin_password}")
        print(f"     Email: admin@somniguard.local")
        print("")

    if patient_created:
        print("  👤 DEMO PATIENT:")
        print(f"     Name: {patient_name}")
        print(f"     DOB: {patient_dob}")
        print("")

    print("  Next: Log in to the gateway dashboard at https://10.42.0.1:5443/")
    print("=" * 60)
    print("")

    return 0


if __name__ == "__main__":
    sys.exit(main())
