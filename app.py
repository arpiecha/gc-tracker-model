"""GC Tracker — multi-client receipt tracker.

One Flask service: JSON API plus the static dashboard and mobile upload page.
Adding a client is a form, not a deployment.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import date, datetime

from flask import Flask, Response, jsonify, redirect, request, send_from_directory
from flask_cors import CORS
from sqlalchemy import func, select

import storage
from claude_receipts import CATEGORIES, analyze_receipt
from db import (Bill, Client, Draw, Receipt, SessionLocal, get_setting, init_db,
                set_setting, unique_slug)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
# A job everyone on site should be able to open without being asked for a
# passcode every time can set REQUIRE_PASSWORD=false. Anyone with the link
# then has the run of that tracker, so it is off by default.
REQUIRE_PASSWORD = os.environ.get("REQUIRE_PASSWORD", "true").strip().lower() not in ("0", "false", "no", "off")
PORT = int(os.environ.get("PORT", "8080"))

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = Flask(__name__, static_folder=None)
CORS(app, resources={r"/*": {"origins": "*"}}, allow_headers=["Content-Type", "X-Admin-Password"])


# --- categories ---------------------------------------------------------

CATEGORIES_KEY = "categories"


def get_categories() -> list[str]:
    """The editable category list, falling back to the ones the bot shipped with."""
    stored = get_setting(CATEGORIES_KEY)
    if stored:
        try:
            cats = json.loads(stored)
            if isinstance(cats, list) and cats:
                return [str(c) for c in cats]
        except ValueError:
            logger.warning("Stored categories are not valid JSON; using the defaults")
    return list(CATEGORIES)


# --- auth ---------------------------------------------------------------

PASSWORD_KEY = "admin_password"

# Verifying a PBKDF2 hash costs ~100ms, which is far too slow to repeat on
# every request. The passcode is a single shared value, so remember the one
# that last verified and against which stored hash, and only re-derive when
# either changes.
_verified = {"raw": None, "against": None}


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    iterations = 200_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${digest.hex()}"


def _check_hash(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, digest_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def password_ok() -> bool:
    """Accept the password from the header, or from ?key= for <img>/<a> links.

    The passcode lives in the database once it has been changed from the UI;
    until then the ADMIN_PASSWORD environment variable is the passcode.
    """
    if not REQUIRE_PASSWORD:
        return True

    supplied = request.headers.get("X-Admin-Password") or request.args.get("key") or ""
    if not supplied:
        return False

    try:
        stored = get_setting(PASSWORD_KEY)
    except Exception:
        logger.exception("Could not read the stored passcode; falling back to the env var")
        stored = None

    if not stored:
        return hmac.compare_digest(supplied, ADMIN_PASSWORD)

    if (_verified["against"] == stored and _verified["raw"] is not None
            and hmac.compare_digest(supplied, _verified["raw"])):
        return True

    if _check_hash(supplied, stored):
        _verified["raw"], _verified["against"] = supplied, stored
        return True
    return False


def require_auth():
    """Return an error response if the request is not authorised, else None."""
    if not password_ok():
        return jsonify({"error": "Unauthorized"}), 401
    return None


# --- static pages -------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/add")
def add_page():
    """The phone page is gone; old home-screen shortcuts land on the dashboard."""
    return redirect("/", code=302)


@app.route("/c/<slug>")
def client_page(slug: str):
    """Per-job dashboard URL. The page reads the slug out of the path."""
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/auth-check")
def auth_check():
    """Lets a page verify a password without fetching any data.

    `required` is how the dashboard knows whether to show the lock screen at
    all: with REQUIRE_PASSWORD off it goes straight to the job.
    """
    return jsonify({"ok": password_ok(), "required": REQUIRE_PASSWORD})


# --- clients ------------------------------------------------------------

@app.route("/clients", methods=["GET"])
def list_clients():
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        counts = dict(
            session.execute(
                select(Receipt.client_id, func.count(Receipt.id)).group_by(Receipt.client_id)
            ).all()
        )
        clients = session.scalars(select(Client).order_by(Client.name)).all()
        return jsonify([c.to_dict(receipt_count=counts.get(c.id, 0)) for c in clients])


@app.route("/clients", methods=["POST"])
def create_client():
    if (err := require_auth()):
        return err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400

    with SessionLocal() as session:
        client = Client(
            name=name,
            slug=unique_slug(session, name),
            address=(data.get("address") or "").strip() or None,
            notes=(data.get("notes") or "").strip() or None,
        )
        session.add(client)
        session.commit()
        logger.info("Created client %s (%s)", client.id, client.slug)
        return jsonify(client.to_dict(receipt_count=0)), 201


@app.route("/clients/<int:client_id>", methods=["GET"])
def get_client(client_id: int):
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        client = session.get(Client, client_id)
        if client is None:
            return jsonify({"error": "Client not found"}), 404
        count = session.scalar(
            select(func.count(Receipt.id)).where(Receipt.client_id == client_id)
        )
        return jsonify(client.to_dict(receipt_count=count or 0))


@app.route("/clients/<int:client_id>", methods=["PATCH"])
def update_client(client_id: int):
    if (err := require_auth()):
        return err
    data = request.get_json(silent=True) or {}
    with SessionLocal() as session:
        client = session.get(Client, client_id)
        if client is None:
            return jsonify({"error": "Client not found"}), 404

        if "name" in data:
            name = (data.get("name") or "").strip()
            if not name:
                return jsonify({"error": "Name cannot be empty"}), 400
            if name != client.name:
                client.name = name
                client.slug = unique_slug(session, name, exclude_id=client.id)
        if "address" in data:
            client.address = (data.get("address") or "").strip() or None
        if "notes" in data:
            client.notes = (data.get("notes") or "").strip() or None
        if "start_date" in data:
            raw = (data.get("start_date") or "").strip()
            if not raw:
                client.start_date = None
            else:
                try:
                    client.start_date = datetime.strptime(raw, "%Y-%m-%d").date()
                except ValueError:
                    return jsonify({"error": "Start date must be YYYY-MM-DD"}), 400
        if "draws_enabled" in data:
            client.draws_enabled = bool(data.get("draws_enabled"))
        if "loan_amount" in data:
            raw = data.get("loan_amount")
            if raw in (None, ""):
                client.loan_amount = None
            else:
                try:
                    client.loan_amount = round(float(raw), 2)
                except (TypeError, ValueError):
                    return jsonify({"error": "Loan amount must be a number"}), 400

        session.commit()
        return jsonify(client.to_dict())


@app.route("/clients/<int:client_id>", methods=["DELETE"])
def delete_client(client_id: int):
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        client = session.get(Client, client_id)
        if client is None:
            return jsonify({"error": "Client not found"}), 404
        session.delete(client)  # receipts cascade
        session.commit()
        logger.info("Deleted client %s", client_id)
        return jsonify({"success": True})


# --- receipts -----------------------------------------------------------

def find_duplicate(session, client_id: int, rdate: date, amount: float) -> Receipt | None:
    """Same client, same date, same amount — whatever the store is called.

    Receipts render store names inconsistently, so the name is not part of the
    test: the same total on the same day is what marks a receipt as already
    logged. The amount is compared signed, so a return never collides with a
    purchase, and in whole cents, because subtracting floats puts 66.54 and
    66.55 a hair under a cent apart.
    """
    cents = round(amount * 100)
    same_day = session.scalars(
        select(Receipt).where(Receipt.client_id == client_id, Receipt.date == rdate)
    ).all()
    for r in same_day:
        if round(float(r.amount) * 100) == cents:
            return r
    return None


@app.route("/analyze", methods=["POST"])
def analyze_endpoint():
    if (err := require_auth()):
        return err
    if "photo" not in request.files:
        return jsonify({"error": "No photo provided"}), 400
    file = request.files["photo"]
    image_bytes = file.read()
    if not image_bytes:
        return jsonify({"error": "Empty photo"}), 400
    mime_type = file.content_type or "image/jpeg"
    try:
        receipt = analyze_receipt(image_bytes, mime_type, get_categories())
        return jsonify({"success": True, "receipt": receipt})
    except Exception as e:
        logger.exception("Analyze failed")
        return jsonify({"error": f"Could not read receipt: {e}"}), 500


@app.route("/save", methods=["POST"])
def save_endpoint():
    if (err := require_auth()):
        return err
    data = request.get_json(silent=True) or {}

    try:
        client_id = int(data.get("client_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "client_id is required"}), 400

    store = (data.get("store") or "").strip()
    if not store:
        return jsonify({"error": "Store is required"}), 400

    try:
        amount = round(float(data.get("amount")), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "Amount must be a number"}), 400

    rtype = (data.get("type") or "purchase").strip().lower()
    if rtype not in ("purchase", "return"):
        rtype = "purchase"
    # Returns are stored negative so summing the column gives net spend.
    amount = -abs(amount) if rtype == "return" else abs(amount)

    # Anything off the current list is kept as sent: a receipt filed under a
    # category that has since been removed keeps its label.
    cats = get_categories()
    category = (data.get("category") or "").strip() or (cats[-1] if cats else "MISC")
    if len(category) > 40:
        category = category[:40]

    raw_date = (data.get("date") or "").strip()
    try:
        rdate = datetime.strptime(raw_date, "%Y-%m-%d").date() if raw_date else date.today()
    except ValueError:
        rdate = date.today()

    with SessionLocal() as session:
        if session.get(Client, client_id) is None:
            return jsonify({"error": "Client not found"}), 404

        if not data.get("force"):
            dup = find_duplicate(session, client_id, rdate, amount)
            if dup is not None:
                return jsonify({
                    "success": False,
                    "duplicate": True,
                    "error": f"Looks like a duplicate — ${abs(float(dup.amount)):.2f} on "
                             f"{dup.date.isoformat()} is already logged"
                             f"{f' ({dup.store})' if dup.store else ''}.",
                })

        receipt = Receipt(
            client_id=client_id,
            date=rdate,
            store=store,
            category=category,
            type=rtype,
            amount=amount,
            items=(data.get("items") or "").strip() or None,
            notes=(data.get("notes") or "").strip() or None,
            source=(data.get("source") or "dashboard").strip()[:32],
        )
        session.add(receipt)
        session.flush()  # need the id before naming the image file

        image_b64 = data.get("image_base64")
        if image_b64:
            try:
                receipt.image_path = storage.save_image(
                    client_id, receipt.id, base64.b64decode(image_b64)
                )
            except Exception:
                logger.exception("Could not save receipt image; saving the row anyway")

        session.commit()
        logger.info("Saved receipt %s for client %s", receipt.id, client_id)
        return jsonify({"success": True, "id": receipt.id, "receipt": receipt.to_dict()})


@app.route("/clients/<int:client_id>/receipts", methods=["GET"])
def list_receipts(client_id: int):
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        if session.get(Client, client_id) is None:
            return jsonify({"error": "Client not found"}), 404
        receipts = session.scalars(
            select(Receipt)
            .where(Receipt.client_id == client_id)
            .order_by(Receipt.date.desc(), Receipt.id.desc())
        ).all()
        return jsonify([r.to_dict() for r in receipts])


@app.route("/receipts/<int:receipt_id>/image", methods=["GET"])
def receipt_image(receipt_id: int):
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        receipt = session.get(Receipt, receipt_id)
        if receipt is None:
            return jsonify({"error": "Receipt not found"}), 404
        if not receipt.image_path:
            return jsonify({"error": "No photo was saved for this receipt"}), 404
        image = storage.read_image(receipt.image_path)
        if image is None:
            return jsonify({"error": "Photo is missing from storage"}), 404
        return Response(image, mimetype="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


@app.route("/receipts/<int:receipt_id>", methods=["PATCH"])
def update_receipt(receipt_id: int):
    """Correct a receipt after the fact. The photo is left alone — this edits
    what was typed or what Claude read off it, not the picture."""
    if (err := require_auth()):
        return err
    data = request.get_json(silent=True) or {}

    with SessionLocal() as session:
        receipt = session.get(Receipt, receipt_id)
        if receipt is None:
            return jsonify({"error": "Receipt not found"}), 404

        if "store" in data:
            store = (data.get("store") or "").strip()
            if not store:
                return jsonify({"error": "Store is required"}), 400
            receipt.store = store

        # Type and amount travel together: returns are stored negative so that
        # summing the column still gives net spend.
        rtype = receipt.type
        if "type" in data:
            rtype = (data.get("type") or "purchase").strip().lower()
            if rtype not in ("purchase", "return"):
                rtype = "purchase"
            receipt.type = rtype
        if "amount" in data:
            try:
                amount = round(float(data.get("amount")), 2)
            except (TypeError, ValueError):
                return jsonify({"error": "Amount must be a number"}), 400
            receipt.amount = -abs(amount) if rtype == "return" else abs(amount)
        elif "type" in data:
            current = abs(float(receipt.amount))
            receipt.amount = -current if rtype == "return" else current

        if "category" in data:
            cats = get_categories()
            category = (data.get("category") or "").strip() or (cats[-1] if cats else "MISC")
            receipt.category = category[:40]

        if "date" in data:
            raw_date = (data.get("date") or "").strip()
            try:
                receipt.date = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                return jsonify({"error": "Date must be YYYY-MM-DD"}), 400

        if "items" in data:
            receipt.items = (data.get("items") or "").strip() or None
        if "notes" in data:
            receipt.notes = (data.get("notes") or "").strip() or None

        session.commit()
        logger.info("Updated receipt %s", receipt_id)
        return jsonify({"success": True, "receipt": receipt.to_dict()})


@app.route("/receipts/<int:receipt_id>", methods=["DELETE"])
def delete_receipt(receipt_id: int):
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        receipt = session.get(Receipt, receipt_id)
        if receipt is None:
            return jsonify({"error": "Receipt not found"}), 404
        storage.delete_image(receipt.image_path)
        session.delete(receipt)
        session.commit()
        logger.info("Deleted receipt %s", receipt_id)
        return jsonify({"success": True})


# --- bills --------------------------------------------------------------

@app.route("/clients/<int:client_id>/bills", methods=["GET"])
def list_bills(client_id: int):
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        if session.get(Client, client_id) is None:
            return jsonify({"error": "Client not found"}), 404
        bills = session.scalars(
            select(Bill).where(Bill.client_id == client_id)
        ).all()
        return jsonify(sorted((b.to_dict() for b in bills), key=lambda b: b["days_away"]))


@app.route("/clients/<int:client_id>/bills", methods=["POST"])
def create_bill(client_id: int):
    if (err := require_auth()):
        return err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    try:
        due_day = int(data.get("due_day"))
    except (TypeError, ValueError):
        return jsonify({"error": "Due day must be a number"}), 400
    if not 1 <= due_day <= 31:
        return jsonify({"error": "Due day must be between 1 and 31"}), 400

    amount = data.get("amount")
    try:
        amount = round(float(amount), 2) if amount not in (None, "") else None
    except (TypeError, ValueError):
        amount = None

    with SessionLocal() as session:
        if session.get(Client, client_id) is None:
            return jsonify({"error": "Client not found"}), 404
        bill = Bill(client_id=client_id, name=name, due_day=due_day, amount=amount,
                    notes=(data.get("notes") or "").strip() or None)
        session.add(bill)
        session.commit()
        return jsonify(bill.to_dict()), 201


@app.route("/bills/<int:bill_id>", methods=["DELETE"])
def delete_bill(bill_id: int):
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        bill = session.get(Bill, bill_id)
        if bill is None:
            return jsonify({"error": "Bill not found"}), 404
        session.delete(bill)
        session.commit()
        return jsonify({"success": True})


# --- draws --------------------------------------------------------------
#
# Money IN, kept apart from the receipts. Total spent and the category totals
# are what the job cost and never move because of a draw; the draws only say
# how much of it the bank has covered and how much came out of pocket.

@app.route("/clients/<int:client_id>/draws", methods=["GET"])
def list_draws(client_id: int):
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        if session.get(Client, client_id) is None:
            return jsonify({"error": "Client not found"}), 404
        draws = session.scalars(
            select(Draw)
            .where(Draw.client_id == client_id)
            .order_by(Draw.date, Draw.id)     # oldest first: Draw 1 is the first one taken
        ).all()
        return jsonify([d.to_dict() for d in draws])


@app.route("/clients/<int:client_id>/draws", methods=["POST"])
def create_draw(client_id: int):
    if (err := require_auth()):
        return err
    data = request.get_json(silent=True) or {}

    try:
        amount = round(float(data.get("amount")), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "Amount must be a number"}), 400
    if amount <= 0:
        return jsonify({"error": "A draw needs an amount"}), 400

    raw_date = (data.get("date") or "").strip()
    try:
        ddate = datetime.strptime(raw_date, "%Y-%m-%d").date() if raw_date else date.today()
    except ValueError:
        return jsonify({"error": "Date must be YYYY-MM-DD"}), 400

    with SessionLocal() as session:
        if session.get(Client, client_id) is None:
            return jsonify({"error": "Client not found"}), 404
        draw = Draw(
            client_id=client_id,
            date=ddate,
            amount=amount,
            note=(data.get("note") or "").strip() or None,
        )
        session.add(draw)
        session.commit()
        logger.info("Saved draw %s for client %s", draw.id, client_id)
        return jsonify(draw.to_dict()), 201


@app.route("/draws/<int:draw_id>", methods=["DELETE"])
def delete_draw(draw_id: int):
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        draw = session.get(Draw, draw_id)
        if draw is None:
            return jsonify({"error": "Draw not found"}), 404
        session.delete(draw)
        session.commit()
        logger.info("Deleted draw %s", draw_id)
        return jsonify({"success": True})


# --- settings -----------------------------------------------------------

@app.route("/categories", methods=["GET"])
def list_categories():
    """The categories the pickers offer and Claude chooses from.

    Receipts keep their category as plain text, so deleting a category never
    touches receipts already filed under it — it only leaves the pickers.
    """
    if (err := require_auth()):
        return err
    cats = get_categories()
    with SessionLocal() as session:
        rows = (session.query(Receipt.category, func.count(Receipt.id))
                .group_by(Receipt.category).all())
    used = {name: count for name, count in rows}
    return jsonify({"categories": [{"name": c, "receipts": used.get(c, 0)} for c in cats]})


@app.route("/categories", methods=["POST"])
def add_category():
    if (err := require_auth()):
        return err
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    if len(name) > 40:
        return jsonify({"error": "Keep the name under 40 characters"}), 400

    cats = get_categories()
    if any(c.lower() == name.lower() for c in cats):
        return jsonify({"error": f"{name} is already a category"}), 409
    cats.append(name)
    set_setting(CATEGORIES_KEY, json.dumps(cats))
    logger.info("Category added: %s", name)
    return jsonify({"success": True, "categories": cats})


@app.route("/categories/<path:name>", methods=["DELETE"])
def delete_category(name: str):
    if (err := require_auth()):
        return err
    cats = get_categories()
    match = next((c for c in cats if c.lower() == name.strip().lower()), None)
    if match is None:
        return jsonify({"error": "No such category"}), 404
    if len(cats) == 1:
        return jsonify({"error": "Keep at least one category"}), 400

    cats.remove(match)
    set_setting(CATEGORIES_KEY, json.dumps(cats))
    logger.info("Category removed: %s", match)
    return jsonify({"success": True, "categories": cats})


@app.route("/settings/password", methods=["POST"])
def change_password():
    if (err := require_auth()):
        return err
    data = request.get_json(silent=True) or {}
    current = (data.get("current_password") or "").strip()
    new = (data.get("new_password") or "").strip()

    if len(new) < 4:
        return jsonify({"success": False, "error": "New passcode must be at least 4 characters"}), 400

    stored = get_setting(PASSWORD_KEY)
    ok = _check_hash(current, stored) if stored else hmac.compare_digest(current, ADMIN_PASSWORD)
    if not ok:
        return jsonify({"success": False, "error": "Current passcode is incorrect"}), 403

    set_setting(PASSWORD_KEY, _hash_password(new))
    _verified["raw"], _verified["against"] = None, None
    logger.info("Passcode changed")
    return jsonify({"success": True})


if __name__ == "__main__":
    init_db()
    storage.ensure_storage()
    logger.info("Tables and storage ready; listening on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
