import os
from datetime import datetime, timedelta, timezone
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    get_jwt_identity,
    jwt_required,
)
from supabase import Client, create_client


load_dotenv()


def env(name, default=None):
    value = os.getenv(name, default)

    if value is None or value == "":
        raise RuntimeError(
            f"Missing environment variable: {name}"
        )

    return value


app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static",
)

app.config["JWT_SECRET_KEY"] = env(
    "JWT_SECRET_KEY"
)

app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(
    hours=12
)

jwt = JWTManager(app)

supabase: Client = create_client(
    env("SUPABASE_URL"),
    env("SUPABASE_SERVICE_ROLE_KEY"),
)

ADMIN_EMAIL = env(
    "ADMIN_EMAIL"
).strip().lower()

FRONTEND_ORIGIN = os.getenv(
    "FRONTEND_ORIGIN",
    "*",
)

CORS(
    app,
    resources={
        r"/api/*": {
            "origins": FRONTEND_ORIGIN,
        }
    },
    allow_headers=[
        "Content-Type",
        "Authorization",
    ],
    methods=[
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
    ],
)


PLAN_AMOUNTS = {
    "day": 2500,
    "monthly": 15000,
    "quarterly": 40000,
}

PLAN_DAYS = {
    "day": 1,
    "monthly": 30,
    "quarterly": 90,
}


def now_utc():
    return datetime.now(timezone.utc)


def get_identity_email():
    identity = get_jwt_identity()

    if isinstance(identity, dict):
        email = identity.get("email", "")
    else:
        email = str(identity or "")

    return email.strip().lower()


def admin_only(view_function):
    @wraps(view_function)
    @jwt_required()
    def secured_view(*args, **kwargs):
        email = get_identity_email()

        if email != ADMIN_EMAIL:
            return jsonify({
                "error": "Admin access required"
            }), 403

        return view_function(*args, **kwargs)

    return secured_view


def get_profile(email):
    result = (
        supabase
        .table("profiles")
        .select("*")
        .eq("email", email)
        .limit(1)
        .execute()
    )

    rows = result.data or []

    return rows[0] if rows else None


def subscription_expiry(plan):
    if plan not in PLAN_DAYS:
        raise ValueError(
            "Invalid subscription plan"
        )

    return (
        now_utc()
        + timedelta(days=PLAN_DAYS[plan])
    ).isoformat()


def json_body():
    return request.get_json(
        silent=True
    ) or {}


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/admin")
def admin():
    return render_template("admin.html")


# ============================================================
# Authentication
# ============================================================

@app.post("/api/signup")
def signup():
    body = json_body()

    email = (
        body.get("email") or ""
    ).strip().lower()

    password = body.get("password") or ""

    if not email or not password:
        return jsonify({
            "error":
                "Email and password are required"
        }), 400

    if len(password) < 6:
        return jsonify({
            "error":
                "Password must contain at least 6 characters"
        }), 400

    try:
        result = supabase.auth.sign_up({
            "email": email,
            "password": password,
        })

        user = result.user
        session = result.session

        if not user:
            return jsonify({
                "error": "Account creation failed"
            }), 400

        profile = get_profile(email)

        if not profile:
            (
                supabase
                .table("profiles")
                .insert({
                    "id": str(user.id),
                    "email": email,
                    "subscription_status": "free",
                })
                .execute()
            )

        if not session:
            return jsonify({
                "message":
                    "Account created. Check your email to confirm it."
            }), 201

        token = create_access_token(
            identity=email
        )

        return jsonify({
            "token": token,
            "user": {
                "email": email,
                "is_admin": email == ADMIN_EMAIL,
            },
        }), 201

    except Exception:
        app.logger.exception(
            "Signup error"
        )

        return jsonify({
            "error":
                "Could not create account"
        }), 400


@app.post("/api/login")
def login():
    body = json_body()

    email = (
        body.get("email") or ""
    ).strip().lower()

    password = body.get("password") or ""

    if not email or not password:
        return jsonify({
            "error":
                "Email and password are required"
        }), 400

    try:
        result = (
            supabase.auth.sign_in_with_password({
                "email": email,
                "password": password,
            })
        )

        if not result.user:
            return jsonify({
                "error":
                    "Invalid email or password"
            }), 401

        profile = get_profile(email)

        if not profile:
            (
                supabase
                .table("profiles")
                .insert({
                    "id": str(result.user.id),
                    "email": email,
                    "subscription_status": "free",
                })
                .execute()
            )

        token = create_access_token(
            identity=email
        )

        return jsonify({
            "token": token,
            "user": {
                "email": email,
                "is_admin": email == ADMIN_EMAIL,
            },
        }), 200

    except Exception:
        app.logger.exception(
            "Login error"
        )

        return jsonify({
            "error":
                "Invalid email or password"
        }), 401


@app.get("/api/me")
@jwt_required()
def me():
    email = get_identity_email()
    profile = get_profile(email)

    tier = "free"

    if profile:
        tier = profile.get(
            "subscription_status",
            "free",
        )

    return jsonify({
        "user": {
            "email": email,
            "is_admin": email == ADMIN_EMAIL,
            "tier": tier,
            "subscription_status": tier,
        },
    }), 200


# ============================================================
# Customer payment submission
# ============================================================

@app.post("/api/payments")
@jwt_required()
def submit_payment():
    email = get_identity_email()
    body = json_body()

    plan = (
        body.get("plan") or ""
    ).strip().lower()

    manual_transaction_id = (
        body.get("transaction_id") or
        body.get("manual_transaction_id") or
        ""
    ).strip()

    if plan not in PLAN_AMOUNTS:
        return jsonify({
            "error":
                "Invalid payment plan"
        }), 400

    if not manual_transaction_id:
        return jsonify({
            "error":
                "Transaction ID is required"
        }), 400

    try:
        duplicate = (
            supabase
            .table("payments")
            .select("id")
            .eq(
                "manual_transaction_id",
                manual_transaction_id,
            )
            .limit(1)
            .execute()
        )

        if duplicate.data:
            return jsonify({
                "error":
                    "This transaction ID has already been submitted"
            }), 409

        profile = get_profile(email)

        payment = {
            "email": email,
            "plan": plan,
            "amount": PLAN_AMOUNTS[plan],
            "manual_transaction_id":
                manual_transaction_id,
            "status": "pending",
        }

        if profile and profile.get("id"):
            payment["user_id"] = profile["id"]

        result = (
            supabase
            .table("payments")
            .insert(payment)
            .execute()
        )

        return jsonify({
            "message":
                "Payment submitted for review",
            "payment": (
                result.data[0]
                if result.data
                else None
            ),
        }), 201

    except Exception:
        app.logger.exception(
            "Payment submission error"
        )

        return jsonify({
            "error":
                "Could not submit payment"
        }), 500


# ============================================================
# Admin endpoint 1:
# List pending transactions
# ============================================================

@app.get("/api/admin/pending-transactions")
@admin_only
def pending_transactions():
    try:
        result = (
            supabase
            .table("payments")
            .select(
                "id,user_id,email,plan,amount,"
                "manual_transaction_id,status,created_at"
            )
            .eq("status", "pending")
            .order(
                "created_at",
                desc=True,
            )
            .execute()
        )

        return jsonify({
            "transactions": result.data or []
        }), 200

    except Exception:
        app.logger.exception(
            "Pending transaction error"
        )

        return jsonify({
            "error":
                "Could not load pending transactions"
        }), 500


# ============================================================
# Admin endpoint 2:
# Activate by transaction ID
# ============================================================

@app.post("/api/admin/activate")
@admin_only
def activate_transaction():
    body = json_body()

    transaction_id = (
        body.get("transaction_id") or ""
    ).strip()

    if not transaction_id:
        return jsonify({
            "error":
                "transaction_id is required"
        }), 400

    try:
        result = (
            supabase
            .table("payments")
            .select("*")
            .eq("id", transaction_id)
            .limit(1)
            .execute()
        )

        payments = result.data or []

        if not payments:
            return jsonify({
                "error":
                    "Transaction not found"
            }), 404

        payment = payments[0]

        if payment.get("status") == "approved":
            return jsonify({
                "error":
                    "Transaction already activated"
            }), 409

        email = (
            payment.get("email") or ""
        ).strip().lower()

        plan = (
            payment.get("plan") or ""
        ).strip().lower()

        if not email:
            return jsonify({
                "error":
                    "Transaction has no email"
            }), 400

        if plan not in PLAN_DAYS:
            return jsonify({
                "error":
                    "Transaction has an invalid plan"
            }), 400

        profile = get_profile(email)

        if not profile:
            return jsonify({
                "error":
                    "User profile not found"
            }), 404

        expiry = subscription_expiry(plan)

        (
            supabase
            .table("profiles")
            .update({
                "subscription_status": plan,
                "subscription_expires_at": expiry,
            })
            .eq("email", email)
            .execute()
        )

        (
            supabase
            .table("payments")
            .update({
                "status": "approved",
                "approved_at":
                    now_utc().isoformat(),
            })
            .eq("id", transaction_id)
            .execute()
        )

        return jsonify({
            "message":
                "Subscription activated successfully"
        }), 200

    except ValueError as error:
        return jsonify({
            "error": str(error)
        }), 400

    except Exception:
        app.logger.exception(
            "Transaction activation error"
        )

        return jsonify({
            "error":
                "Could not activate transaction"
        }), 500


# ============================================================
# Admin endpoint 3:
# Activate directly by email
# ============================================================

@app.post("/api/admin/activate-by-email")
@admin_only
def activate_by_email():
    body = json_body()

    email = (
        body.get("email") or ""
    ).strip().lower()

    plan = (
        body.get("plan") or "monthly"
    ).strip().lower()

    if not email:
        return jsonify({
            "error":
                "Email is required"
        }), 400

    if plan not in PLAN_DAYS:
        return jsonify({
            "error":
                "Invalid subscription plan"
        }), 400

    try:
        profile = get_profile(email)

        if not profile:
            return jsonify({
                "error":
                    "User not found"
            }), 404

        expiry = subscription_expiry(plan)

        (
            supabase
            .table("profiles")
            .update({
                "subscription_status": plan,
                "subscription_expires_at": expiry,
            })
            .eq("email", email)
            .execute()
        )

        return jsonify({
            "message":
                f"{email} activated on the {plan} plan"
        }), 200

    except Exception:
        app.logger.exception(
            "Email activation error"
        )

        return jsonify({
            "error":
                "Could not activate user"
        }), 500


# ============================================================
# Existing scraper endpoints
# ============================================================

@app.get("/api/arbs")
@jwt_required()
def get_arbs():
    """
    Replace the empty list with your existing scraper logic.
    """
    return jsonify({
        "arbs": [],
        "tier": "free",
    }), 200


@app.get("/api/history")
@jwt_required()
def get_history():
    """
    Replace this with your existing history logic.
    """
    return jsonify({
        "history": {},
    }), 200


@app.post("/api/scan")
@jwt_required()
def run_scan():
    """
    Replace this with your existing scan logic.
    """
    return jsonify({
        "message":
            "Scan completed",
        "arbs": [],
    }), 200


# ============================================================
# Optional route debugging
# ============================================================

@app.get("/debug/routes")
def debug_routes():
    routes = []

    for rule in app.url_map.iter_rules():
        methods = sorted(
            rule.methods - {
                "HEAD",
                "OPTIONS",
            }
        )

        routes.append({
            "path": rule.rule,
            "methods": methods,
            "endpoint": rule.endpoint,
        })

    return jsonify({
        "routes": routes
    }), 200


if __name__ == "__main__":
    port = int(
        os.getenv("PORT", "5000")
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
)
