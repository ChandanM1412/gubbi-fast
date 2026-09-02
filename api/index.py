"""Secure backend for Gubbi Fast — deployed as a Vercel Python serverless function.

Every write that matters for trust/money goes through here instead of the browser writing to
Firestore directly: placing an order, claiming/confirming/rejecting a payment, accepting/
picking-up/delivering an order, rider location updates, and all admin catalog edits. The
frontend still reads Firestore directly (reads aren't a security concern); Firestore security
rules should deny ALL client writes (see README.md) so this backend is the only writer.

Environment variables (all optional — sensible defaults are hardcoded below so this works out
of the box; override them on your hosting platform for a real deployment):
  ADMIN_PASSWORD                 the real admin password (default: "Gubbi@Admin2026")
  JWT_SECRET                     any long random string, used to sign session tokens
  FIREBASE_SERVICE_ACCOUNT_JSON  the full JSON contents of a Firebase service account key
                                  (required for catalog/order endpoints, NOT for admin login)
See README.md for how to obtain/set these.
"""
import datetime
import json
import os
import random
import time

import firebase_admin
import jwt
from firebase_admin import credentials, firestore
from flask import Flask, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)

# Hardcoded fallbacks so admin login works immediately without any setup. These live only in
# this server-side file — never sent to, or readable from, the browser. Change ADMIN_PASSWORD
# (and ideally JWT_SECRET) via environment variables before giving this app to real users.
JWT_SECRET = os.environ.get("JWT_SECRET", "gubbi-fast-default-signing-secret-change-me-93f7a1")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Gubbi@Admin2026")
TOKEN_TTL_SECONDS = 12 * 60 * 60
FALLBACK_LAT, FALLBACK_LNG = 13.3086, 76.9366  # central Gubbi (Tumkur dist.), used when a customer denies GPS

# Bump this whenever DEFAULT_CATALOG's demo data changes (e.g. fixing a broken image URL) so the
# already-seeded Firestore doc gets refreshed instead of silently keeping the stale demo copy.
# NOTE: once real admin edits exist in production, bumping this will overwrite them — only bump
# during initial setup/testing, not after the app has real restaurants/orders.
DEFAULT_CATALOG_SEED_VERSION = 3

# Seed data + default settings — this is the ONLY copy of it; the frontend ships empty and pulls
# everything from GET /api/catalog / GET /api/live below, so nothing here is visible in the
# browser's page source or dev tools.
DEFAULT_CATALOG = {
    "restaurants": [
        {"id": "r1", "name": "Gubbi Tiffin House", "cuisine": "South Indian · Tiffins", "area": "B.H. Road, Gubbi", "eta": "25-30 min", "rating": 4.3, "color": "#F2A93B", "emoji": "🍛", "lat": 13.3096, "lng": 76.9376, "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/43/Masala_dosa_01.jpg/960px-Masala_dosa_01.jpg"},
        {"id": "r2", "name": "Royals Food", "cuisine": "Multi-Cuisine · Biryani", "area": "Bus Stand Road, Gubbi", "eta": "30-35 min", "rating": 4.1, "color": "#C1442E", "emoji": "🍚", "lat": 13.3070, "lng": 76.9350, "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/c/c0/Chicken_Hyderabadi_Biryani.JPG/960px-Chicken_Hyderabadi_Biryani.JPG"},
        {"id": "r3", "name": "Kaveri Bakery", "cuisine": "Bakery · Sweets", "area": "Tumkur Road, Gubbi", "eta": "20-25 min", "rating": 4.5, "color": "#2E6B4F", "emoji": "🥐", "lat": 13.3110, "lng": 76.9390, "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e2/IndianBakeryRolls1.jpg/960px-IndianBakeryRolls1.jpg"},
        {"id": "r4", "name": "Bangarpet Chats", "cuisine": "Chaat · Street Food", "area": "APMC Yard, Gubbi", "eta": "15-20 min", "rating": 4.0, "color": "#8E5FA3", "emoji": "🥟", "lat": 13.3060, "lng": 76.9410, "image": "https://upload.wikimedia.org/wikipedia/commons/2/22/Bhel_puri_Snack.jpg"},
    ],
    "menu": {
        "r1": [
            {"id": "m1", "name": "Masala Dosa", "price": 80, "veg": True, "emoji": "🥞", "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/43/Masala_dosa_01.jpg/960px-Masala_dosa_01.jpg"},
            {"id": "m2", "name": "Idli Vada Combo", "price": 70, "veg": True, "emoji": "🍥", "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/0/02/Idli_Sambar-Noida-UP-SP004.jpg/960px-Idli_Sambar-Noida-UP-SP004.jpg"},
            {"id": "m3", "name": "Filter Coffee", "price": 30, "veg": True, "emoji": "☕", "image": "https://upload.wikimedia.org/wikipedia/commons/8/84/Indian_filter_coffee_in_Dabarah.jpg"},
            {"id": "m4", "name": "Rava Kesari Bath", "price": 60, "veg": True, "emoji": "🍮", "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/8f/Kesari_bhath.jpg/960px-Kesari_bhath.jpg"},
        ],
        "r2": [
            {"id": "m5", "name": "Chicken Dum Biryani", "price": 220, "veg": False, "emoji": "🍗", "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/c/c0/Chicken_Hyderabadi_Biryani.JPG/960px-Chicken_Hyderabadi_Biryani.JPG"},
            {"id": "m6", "name": "Mutton Biryani", "price": 280, "veg": False, "emoji": "🍖", "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/b/b9/Mutton_biryani.JPG/960px-Mutton_biryani.JPG"},
            {"id": "m7", "name": "Veg Biryani", "price": 170, "veg": True, "emoji": "🍛", "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/41/Veg_Rice_Pulao_1.jpg/960px-Veg_Rice_Pulao_1.jpg"},
            {"id": "m8", "name": "Chicken Seekh Kebab", "price": 190, "veg": False, "emoji": "🍢", "image": "https://upload.wikimedia.org/wikipedia/commons/7/7e/Seekh_Kebab.JPG"},
        ],
        "r3": [
            {"id": "m9", "name": "Rusk Toast", "price": 40, "veg": True, "emoji": "🍪", "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/eb/Rusk_%28Dry_Biscuit%2Ctwice_baked_bread%29.jpg/960px-Rusk_%28Dry_Biscuit%2Ctwice_baked_bread%29.jpg"},
            {"id": "m10", "name": "Bakery Bread Rolls", "price": 50, "veg": True, "emoji": "🥐", "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e2/IndianBakeryRolls1.jpg/960px-IndianBakeryRolls1.jpg"},
            {"id": "m11", "name": "Nankhatai Biscuits (200g)", "price": 90, "veg": True, "emoji": "🍪", "image": "https://upload.wikimedia.org/wikipedia/commons/9/9b/Nankhatai_biscuits.jpg"},
        ],
        "r4": [
            {"id": "m12", "name": "Bhel Puri", "price": 60, "veg": True, "emoji": "🥗", "image": "https://upload.wikimedia.org/wikipedia/commons/2/22/Bhel_puri_Snack.jpg"},
            {"id": "m13", "name": "Pani Puri Plate", "price": 50, "veg": True, "emoji": "🥟", "image": "https://upload.wikimedia.org/wikipedia/commons/6/6c/Pani_puri_2.jpg"},
            {"id": "m14", "name": "Sev Puri", "price": 70, "veg": True, "emoji": "🍥", "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/d9/Sev_Puri_2023.jpg/960px-Sev_Puri_2023.jpg"},
        ],
    },
    "riders": [
        {"id": "rd1", "name": "Ravi Kumar", "phone": "98450 11122", "commissionPercent": 10},
        {"id": "rd2", "name": "Suresh Gowda", "phone": "99001 33445", "commissionPercent": 8},
        {"id": "rd3", "name": "Manjunath B.", "phone": "97400 55667", "commissionPercent": 10},
    ],
    "offers": [
        {"id": "o1", "emoji": "🔥", "title": "FLAT 50% OFF", "sub": "Up to ₹100 at Gubbi Tiffin House", "bg": "#C1442E", "restaurantId": "r1"},
        {"id": "o2", "emoji": "🍗", "title": "Biryani Bonanza", "sub": "Flat ₹75 off at Royals Food", "bg": "#2453A8", "restaurantId": "r2"},
        {"id": "o3", "emoji": "🥟", "title": "Chaat Treat Deal", "sub": "Buy 1 Get 1 at Bangarpet Chats", "bg": "#8E5FA3", "restaurantId": "r4"},
    ],
    "settings": {
        "upiId": "7349656582@axl",
        "upiName": "Gubbi Fast",
        "adminPhone": "7899863713",
        "instagramUrl": "https://www.instagram.com/gubbi_fast/",
        "whatsappUrl": "https://api.whatsapp.com/message/X5VYGUB7I3I4P1?autoload=1&app_absent=0",
    },
}
DEFAULT_RIDER_PASSWORDS = {"rd1": "rider123", "rd2": "rider123", "rd3": "rider123"}

_db = None


def get_db():
    global _db
    if _db is not None:
        return _db
    if not firebase_admin._apps:
        raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
        if not raw:
            raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON is not configured")
        cred = credentials.Certificate(json.loads(raw))
        firebase_admin.initialize_app(cred)
    _db = firestore.client()
    return _db


@app.errorhandler(Exception)
def handle_error(e):
    # Any uncaught error (e.g. Firebase not configured) becomes a clean JSON response instead of
    # crashing — this matters because the frontend distinguishes "backend responded with an
    # error" (show it) from "no backend reachable at all" (fall back to demo mode) by whether
    # the HTTP request succeeded, not by its status code.
    return jsonify(error=str(e)), 500


def require_admin():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        payload = jwt.decode(auth[7:], JWT_SECRET, algorithms=["HS256"])
        return payload if payload.get("role") == "admin" else None
    except jwt.PyJWTError:
        return None


# IST has no DST, so a fixed UTC+5:30 offset is always correct — avoids depending on the
# 'tzdata' package (not guaranteed present in every Python runtime) that zoneinfo would need.
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _time_label():
    return datetime.datetime.now(IST).strftime("%I:%M %p")


def _notify(notifications, audience, target_id, message, icon):
    notifications.insert(0, {
        "id": f"n{int(time.time() * 1000)}{random.randint(0, 9999)}",
        "audience": audience, "targetId": target_id,
        "message": message, "icon": icon, "time": _time_label(), "read": False,
    })


# All orders/notifications live as two array fields on ONE Firestore document ('live'), read and
# rewritten in full on every single action. That's fine at low volume, but Firestore documents
# are capped at 1MB and every poll/action pays for the whole document's read+write cost — at the
# ~6-7k orders/notifications scale this WILL eventually hit that limit or get slow/expensive.
# FIX: orders now live as individual documents in a Firestore subcollection
# ('gubbiFast/live/orders/{orderId}') instead of one giant array field — each order
# mutation reads/writes only its own tiny document, so there's no 1MB ceiling and no
# whole-history read/write cost, no matter how many thousands of orders accumulate.
# The 'live' document itself now only holds notifications + counters, which stays small
# (notifications are still capped below since they're the one remaining array field).
MAX_LIVE_ORDERS_RETURNED = 500  # how many recent orders GET /api/live returns to the frontend
MAX_STORED_NOTIFICATIONS = 300
_FINISHED_STATUSES = ("Delivered", "PaymentRejected")


def _orders_collection():
    return get_db().collection("gubbiFast").document("live").collection("orders")


def _live_meta_ref():
    return get_db().collection("gubbiFast").document("live")


def _get_live_meta():
    snap = _live_meta_ref().get()
    if snap.exists:
        return snap.to_dict()
    data = {"notifications": [], "orderSeq": 1001, "liveRev": 0}
    _live_meta_ref().set(data)
    return data


def _mutate_order(order_id, mutate_fn):
    """Read-modify-write just ONE order document plus the small notifications/rev doc —
    never the full order history."""
    order_ref = _orders_collection().document(order_id)
    order_snap = order_ref.get()
    if not order_snap.exists:
        return None, "order not found"
    order = order_snap.to_dict()
    meta = _get_live_meta()
    notifications = meta.get("notifications", [])
    mutate_fn(order, notifications)
    order_ref.set(order)
    meta["notifications"] = notifications[:MAX_STORED_NOTIFICATIONS]
    meta["liveRev"] = (meta.get("liveRev", 0) or 0) + 1
    _live_meta_ref().set(meta)
    return order, None


def _run_order_mutation(order_id, mutate_fn):
    try:
        order, err = _mutate_order(order_id, mutate_fn)
    except PermissionError as e:
        return jsonify(error=str(e)), 403
    except ValueError as e:
        return jsonify(error=str(e)), 409
    if err:
        return jsonify(error=err), 404
    return jsonify(ok=True, order=order)


def _seed_catalog_if_missing(catalog_ref):
    data = json.loads(json.dumps(DEFAULT_CATALOG))  # deep copy, keeps DEFAULT_CATALOG pristine
    data["catalogRev"] = 1
    data["seedVersion"] = DEFAULT_CATALOG_SEED_VERSION
    catalog_ref.set(data)
    creds_ref = get_db().collection("gubbiFast").document("riderCredentials")
    if not creds_ref.get().exists:
        creds_ref.set({rid: generate_password_hash(pw) for rid, pw in DEFAULT_RIDER_PASSWORDS.items()})
    return data


def _get_catalog():
    catalog_ref = get_db().collection("gubbiFast").document("catalog")
    snap = catalog_ref.get()
    if snap.exists:
        data = snap.to_dict()
        if data.get("seedVersion") != DEFAULT_CATALOG_SEED_VERSION:
            return _seed_catalog_if_missing(catalog_ref)
        return data
    return _seed_catalog_if_missing(catalog_ref)


def _mutate_catalog(mutate_fn):
    catalog_ref = get_db().collection("gubbiFast").document("catalog")
    snap = catalog_ref.get()
    data = snap.to_dict() if snap.exists else _seed_catalog_if_missing(catalog_ref)
    mutate_fn(data)
    data["catalogRev"] = (data.get("catalogRev", 0) or 0) + 1
    catalog_ref.set(data)
    return data


# ============ PUBLIC READS (frontend ships with no data at all — everything above is pulled
# from here, so no restaurant/menu/pricing/settings data sits in the browser's page source) ============

@app.get("/api/catalog")
def get_catalog_public():
    return jsonify(_get_catalog())


@app.get("/api/live")
def get_live_public():
    meta = _get_live_meta()
    # Fetched WITHOUT order_by: Firestore silently excludes any document missing the sort field
    # from an order_by() query, which would hide orders from riders/customers if one ever lacked
    # '_seq' — sorting/limiting here in Python guarantees every order document is always returned.
    orders = [d.to_dict() for d in _orders_collection().stream()]
    orders.sort(key=lambda o: o.get("_seq", 0), reverse=True)
    orders = orders[:MAX_LIVE_ORDERS_RETURNED]
    return jsonify(
        orders=orders,
        notifications=meta.get("notifications", []),
        orderSeq=meta.get("orderSeq", 1001),
        liveRev=meta.get("liveRev", 0),
    )


# ============ ADMIN AUTH ============

def _get_admin_password_hash():
    """The custom password an admin has set via Admin > Payments, if any (never sent to the browser)."""
    try:
        snap = get_db().collection("gubbiFast").document("adminCredentials").get()
    except RuntimeError:
        return None
    return snap.to_dict().get("passwordHash") if snap.exists else None


@app.post("/api/admin/login")
def admin_login():
    password = (request.get_json(silent=True) or {}).get("password", "")
    # The hardcoded/env-var default always works (dev/testing access), on top of whichever
    # custom password has been set via Admin > Payments (stored hashed, own device).
    if password != ADMIN_PASSWORD:
        stored_hash = _get_admin_password_hash()
        if not stored_hash or not check_password_hash(stored_hash, password):
            return jsonify(error="Invalid password"), 401
    token = jwt.encode(
        {"role": "admin", "exp": int(time.time()) + TOKEN_TTL_SECONDS},
        JWT_SECRET,
        algorithm="HS256",
    )
    return jsonify(token=token)


@app.post("/api/admin/change-password")
def change_admin_password():
    if not require_admin():
        return jsonify(error="Unauthorized"), 401
    new_password = (request.get_json(silent=True) or {}).get("newPassword") or ""
    if len(new_password) < 6:
        return jsonify(error="Password must be at least 6 characters"), 400
    get_db().collection("gubbiFast").document("adminCredentials").set(
        {"passwordHash": generate_password_hash(new_password)}
    )
    return jsonify(ok=True)


@app.post("/api/admin/clear-orders")
def clear_orders():
    if not require_admin():
        return jsonify(error="Unauthorized"), 401
    db = get_db()
    docs = list(_orders_collection().stream())
    batch = db.batch()
    for i, d in enumerate(docs):
        batch.delete(d.reference)
        if (i + 1) % 400 == 0:  # stay under Firestore's 500-write batch limit
            batch.commit()
            batch = db.batch()
    batch.commit()
    meta = _get_live_meta()
    meta["notifications"] = []
    meta["liveRev"] = (meta.get("liveRev", 0) or 0) + 1
    _live_meta_ref().set(meta)
    return jsonify(ok=True)


@app.post("/api/restaurants/<restaurant_id>/clear-payments")
def clear_restaurant_payments(restaurant_id):
    """Marks every currently-outstanding delivered order for this restaurant as paid out —
    doesn't touch orders that are still in progress or already settled."""
    if not require_admin():
        return jsonify(error="Unauthorized"), 401
    db = get_db()
    docs = list(
        _orders_collection()
        .where("restaurantId", "==", restaurant_id)
        .where("status", "==", "Delivered")
        .stream()
    )
    batch = db.batch()
    cleared = 0
    for i, d in enumerate(docs):
        if d.to_dict().get("restaurantSettled"):
            continue
        batch.update(d.reference, {"restaurantSettled": True})
        cleared += 1
        if (i + 1) % 400 == 0:  # stay under Firestore's 500-write batch limit
            batch.commit()
            batch = db.batch()
    batch.commit()
    meta = _get_live_meta()
    meta["liveRev"] = (meta.get("liveRev", 0) or 0) + 1
    _live_meta_ref().set(meta)
    return jsonify(ok=True, cleared=cleared)


# ============ CUSTOMER AUTH ============
# Password hashes live in their own 'customerCredentials' doc, keyed by normalized 10-digit
# phone number — never included in the 'catalog'/'live' docs the frontend reads, so a
# customer's password (even hashed) is never sent to any browser.

def _normalize_phone(phone):
    return "".join(ch for ch in str(phone or "") if ch.isdigit())[-10:]


@app.post("/api/customers/signup")
def customer_signup():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()[:60]
    phone = _normalize_phone(body.get("phone"))
    password = body.get("password") or ""
    address = (body.get("address") or "").strip()[:200]
    lat, lng = body.get("lat"), body.get("lng")
    if not name or len(phone) != 10:
        return jsonify(error="Enter your name and a valid 10-digit phone number"), 400
    if len(password) < 4:
        return jsonify(error="Password must be at least 4 characters"), 400
    creds_ref = get_db().collection("gubbiFast").document("customerCredentials")
    snap = creds_ref.get()
    creds = snap.to_dict() if snap.exists else {}
    if phone in creds:
        return jsonify(error="This phone number is already registered — please log in instead."), 409
    creds[phone] = {
        "passwordHash": generate_password_hash(password),
        "name": name, "address": address, "lat": lat, "lng": lng,
    }
    creds_ref.set(creds)
    return jsonify(ok=True, customer={"name": name, "phone": phone, "address": address, "lat": lat, "lng": lng})


@app.post("/api/customers/login")
def customer_login():
    body = request.get_json(silent=True) or {}
    phone = _normalize_phone(body.get("phone"))
    password = body.get("password") or ""
    creds_snap = get_db().collection("gubbiFast").document("customerCredentials").get()
    creds = creds_snap.to_dict() if creds_snap.exists else {}
    record = creds.get(phone)
    if not record or not check_password_hash(record.get("passwordHash", ""), password):
        return jsonify(error="Incorrect phone number or password"), 401
    return jsonify(ok=True, customer={
        "name": record.get("name"), "phone": phone, "address": record.get("address"),
        "lat": record.get("lat"), "lng": record.get("lng"),
    })


@app.post("/api/customers/change-password")
def customer_change_password():
    body = request.get_json(silent=True) or {}
    phone = _normalize_phone(body.get("phone"))
    current_password = body.get("currentPassword") or ""
    new_password = body.get("newPassword") or ""
    if len(new_password) < 4:
        return jsonify(error="New password must be at least 4 characters"), 400
    creds_ref = get_db().collection("gubbiFast").document("customerCredentials")
    snap = creds_ref.get()
    creds = snap.to_dict() if snap.exists else {}
    record = creds.get(phone)
    if not record or not check_password_hash(record.get("passwordHash", ""), current_password):
        return jsonify(error="Current password is incorrect"), 401
    record["passwordHash"] = generate_password_hash(new_password)
    creds[phone] = record
    creds_ref.set(creds)
    return jsonify(ok=True)


# ============ ORDERS (customer/rider actions — no separate session token beyond the login
# above, so these are validated by state + matching phone/riderId, same trust level as the
# rest of the app) ============

@app.post("/api/orders")
def create_order():
    body = request.get_json(silent=True) or {}
    restaurant_id = body.get("restaurantId")
    cart = body.get("cart") or {}
    customer_name = (body.get("customerName") or "Guest").strip()[:60]
    customer_phone = (body.get("customerPhone") or "").strip()[:20]
    address = (body.get("address") or "Home").strip()[:200]
    is_cod = body.get("paymentChoice") == "cod"
    loc_shared = bool(body.get("locShared"))
    cust_lat, cust_lng = body.get("custLat"), body.get("custLng")
    if not restaurant_id or not cart:
        return jsonify(error="restaurantId and cart are required"), 400

    catalog = _get_catalog()
    restaurant = next((r for r in catalog.get("restaurants", []) if r.get("id") == restaurant_id), None)
    if not restaurant:
        return jsonify(error="restaurant not found"), 404
    menu = catalog.get("menu", {}).get(restaurant_id, [])

    # Prices/total are recomputed from the catalog here, never trusted from the client, so a
    # tampered request can't pay less than the real menu price.
    order_items, total = [], 0
    for item_id, qty in cart.items():
        qty = int(qty)
        if qty <= 0:
            continue
        item = next((m for m in menu if m.get("id") == item_id), None)
        if not item:
            continue
        order_items.append({"itemId": item_id, "name": item["name"], "qty": qty, "price": item["price"]})
        total += item["price"] * qty
    if not order_items:
        return jsonify(error="cart has no valid items"), 400

    meta = _get_live_meta()
    order_seq = meta.get("orderSeq", 1001)
    order_id = f"GF{order_seq}"
    order = {
        "id": order_id,
        "_seq": order_seq,
        "restaurantId": restaurant_id,
        "items": order_items,
        "total": total,
        "customerName": customer_name,
        "customerPhone": customer_phone,
        "address": address,
        "status": "Placed" if is_cod else "AwaitingPayment",
        # UPI orders can only be accepted by riders once the admin confirms the payment (see
        # /api/payments/<id>/confirm) — COD has no such upfront payment to verify, so it's
        # immediately acceptable.
        "riderApproved": is_cod,
        "riderId": None,
        "createdAt": _time_label(),
        "createdDate": datetime.datetime.now(IST).strftime("%Y-%m-%d"),  # for the admin's year/month/day order browser
        "paymentMethod": "Cash on Delivery" if is_cod else "UPI (PhonePe / Paytm / GPay)",
        "custLat": cust_lat if (loc_shared and cust_lat is not None) else FALLBACK_LAT,
        "custLng": cust_lng if (loc_shared and cust_lng is not None) else FALLBACK_LNG,
        "locShared": loc_shared,
    }
    notifications = meta.get("notifications", [])
    if is_cod:
        _notify(notifications, "customer", customer_phone, f"Order #{order_id} placed at {restaurant['name']} — pay ₹{total} cash on delivery.", "🧾")
        _notify(notifications, "admin", None, f"New order #{order_id} from {customer_name} at {restaurant['name']} — ₹{total} (COD).", "🆕")
        _notify(notifications, "all-riders", None, f"New order #{order_id} available for pickup near {restaurant.get('area','')}.", "📦")
    else:
        _notify(notifications, "customer", customer_phone, f"Order #{order_id} created at {restaurant['name']} — complete UPI payment to confirm it.", "🧾")
        _notify(notifications, "admin", None, f"Order #{order_id} from {customer_name} at {restaurant['name']} — ₹{total} — awaiting payment.", "⏳")

    _orders_collection().document(order_id).set(order)
    meta["notifications"] = notifications[:MAX_STORED_NOTIFICATIONS]
    meta["orderSeq"] = order_seq + 1
    meta["liveRev"] = (meta.get("liveRev", 0) or 0) + 1
    _live_meta_ref().set(meta)
    return jsonify(ok=True, order=order)


@app.post("/api/orders/<order_id>/claim-payment")
def claim_payment(order_id):
    phone = (request.get_json(silent=True) or {}).get("customerPhone", "")

    def mutate(order, notifications):
        if order.get("customerPhone") != phone:
            raise PermissionError("this order doesn't belong to you")
        if order.get("status") != "AwaitingPayment":
            raise ValueError("order is not awaiting payment")
        order["status"] = "PaymentClaimed"
        _notify(notifications, "admin", None,
                f"Customer claims payment for order #{order['id']} (₹{order['total']}) — please verify in your UPI/bank app before confirming.", "💳")

    return _run_order_mutation(order_id, mutate)


@app.post("/api/payments/<order_id>/confirm")
def confirm_payment(order_id):
    if not require_admin():
        return jsonify(error="Unauthorized"), 401

    def mutate(order, notifications):
        order["status"] = "Placed"
        order["riderApproved"] = True  # admin already reviewed this order to confirm the payment
        _notify(notifications, "customer", order.get("customerPhone"),
                f"Payment confirmed for order #{order['id']}. Your food is being prepared!", "✅")
        _notify(notifications, "all-riders", None, f"✅ Order #{order['id']} approved and available for pickup.", "📦")

    return _run_order_mutation(order_id, mutate)


@app.post("/api/payments/<order_id>/reject")
def reject_payment(order_id):
    if not require_admin():
        return jsonify(error="Unauthorized"), 401
    admin_phone = _get_catalog().get("settings", {}).get("adminPhone", "9123242102")

    def mutate(order, notifications):
        order["status"] = "PaymentRejected"
        _notify(notifications, "customer", order.get("customerPhone"),
                f"We couldn't verify your payment for order #{order['id']}. Please call Gubbi Fast on {admin_phone} or place the order again.", "⚠️")

    return _run_order_mutation(order_id, mutate)


@app.post("/api/orders/<order_id>/accept")
def accept_order(order_id):
    rider_id = (request.get_json(silent=True) or {}).get("riderId", "")
    if not rider_id:
        return jsonify(error="riderId is required"), 400
    rider = next((r for r in _get_catalog().get("riders", []) if r.get("id") == rider_id), None)
    if not rider:
        return jsonify(error="unknown rider"), 404

    def mutate(order, notifications):
        if order.get("status") != "Placed" or not order.get("riderApproved"):
            raise ValueError("order is no longer available")
        order["status"] = "Accepted"
        order["riderId"] = rider_id
        _notify(notifications, "customer", order.get("customerPhone"),
                f"{rider['name']} accepted your order #{order['id']} and is heading to the restaurant.", "🛵")
        _notify(notifications, "admin", None, f"Order #{order['id']} accepted by rider {rider['name']}.", "✅")

    return _run_order_mutation(order_id, mutate)


@app.post("/api/orders/<order_id>/pickup")
def pickup_order(order_id):
    rider_id = (request.get_json(silent=True) or {}).get("riderId", "")

    def mutate(order, notifications):
        if order.get("riderId") != rider_id:
            raise PermissionError("this order isn't assigned to you")
        if order.get("status") != "Accepted":
            raise ValueError("order isn't ready to be picked up")
        order["status"] = "PickedUp"
        _notify(notifications, "customer", order.get("customerPhone"),
                f"Your order #{order['id']} has been picked up and is on the way!", "🚀")
        _notify(notifications, "admin", None, f"Order #{order['id']} picked up by rider.", "📤")

    return _run_order_mutation(order_id, mutate)


@app.post("/api/orders/<order_id>/deliver")
def deliver_order(order_id):
    rider_id = (request.get_json(silent=True) or {}).get("riderId", "")

    def mutate(order, notifications):
        if order.get("riderId") != rider_id:
            raise PermissionError("this order isn't assigned to you")
        if order.get("status") != "PickedUp":
            raise ValueError("order isn't out for delivery")
        order["status"] = "Delivered"
        _notify(notifications, "customer", order.get("customerPhone"), f"Order #{order['id']} delivered. Enjoy your meal!", "🎉")
        _notify(notifications, "admin", None, f"Order #{order['id']} delivered successfully.", "✅")

    return _run_order_mutation(order_id, mutate)


@app.post("/api/orders/<order_id>/rider-location")
def rider_location(order_id):
    body = request.get_json(silent=True) or {}
    rider_id, lat, lng = body.get("riderId", ""), body.get("lat"), body.get("lng")
    if lat is None or lng is None:
        return jsonify(error="lat and lng are required"), 400

    def mutate(order, notifications):
        if order.get("riderId") != rider_id:
            raise PermissionError("this order isn't assigned to you")
        if order.get("status") not in ("Accepted", "PickedUp"):
            raise ValueError("order isn't in transit")
        order["riderLat"], order["riderLng"] = lat, lng

    return _run_order_mutation(order_id, mutate)


# ============ CATALOG (admin only) ============

@app.post("/api/catalog/restaurants")
def add_restaurant():
    if not require_admin():
        return jsonify(error="Unauthorized"), 401
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify(error="name is required"), 400
    restaurant = {
        "id": f"r_{int(time.time() * 1000)}",
        "name": name,
        "cuisine": body.get("cuisine") or "Multi-cuisine",
        "area": body.get("area") or "Gubbi",
        "eta": body.get("eta") or "25-30 min",
        "color": "#F2A93B",
        "emoji": body.get("emoji") or "🍴",
        "lat": FALLBACK_LAT + (random.random() - 0.5) * 0.04,
        "lng": FALLBACK_LNG + (random.random() - 0.5) * 0.04,
        "image": body.get("image") or None,
    }

    def mutate(catalog):
        catalog.setdefault("restaurants", []).append(restaurant)
        catalog.setdefault("menu", {})[restaurant["id"]] = []

    _mutate_catalog(mutate)
    return jsonify(ok=True, restaurant=restaurant)


@app.patch("/api/catalog/restaurants/<restaurant_id>")
def edit_restaurant(restaurant_id):
    if not require_admin():
        return jsonify(error="Unauthorized"), 401
    body = request.get_json(silent=True) or {}
    found = {"ok": False}

    def mutate(catalog):
        for r in catalog.get("restaurants", []):
            if r.get("id") == restaurant_id:
                for field in ("name", "cuisine", "area", "eta", "emoji", "image"):
                    if field in body:
                        r[field] = body[field]
                if "rating" in body:
                    try:
                        r["rating"] = max(1, min(5, float(body["rating"])))
                    except (TypeError, ValueError):
                        pass
                found["ok"] = True

    _mutate_catalog(mutate)
    if not found["ok"]:
        return jsonify(error="restaurant not found"), 404
    return jsonify(ok=True)


@app.delete("/api/catalog/restaurants/<restaurant_id>")
def delete_restaurant(restaurant_id):
    if not require_admin():
        return jsonify(error="Unauthorized"), 401

    def mutate(catalog):
        catalog["restaurants"] = [r for r in catalog.get("restaurants", []) if r.get("id") != restaurant_id]
        catalog.setdefault("menu", {}).pop(restaurant_id, None)
        catalog["offers"] = [o for o in catalog.get("offers", []) if o.get("restaurantId") != restaurant_id]

    _mutate_catalog(mutate)
    return jsonify(ok=True)


@app.post("/api/catalog/restaurants/<restaurant_id>/menu")
def add_menu_item(restaurant_id):
    if not require_admin():
        return jsonify(error="Unauthorized"), 401
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    price = body.get("price")
    if not name or price is None:
        return jsonify(error="name and price are required"), 400
    item = {
        "id": f"m_{int(time.time() * 1000)}",
        "name": name,
        "price": float(price),
        "veg": bool(body.get("veg", True)),
        "emoji": body.get("emoji") or "🍽️",
        "image": body.get("image") or None,
    }

    def mutate(catalog):
        catalog.setdefault("menu", {}).setdefault(restaurant_id, []).append(item)

    _mutate_catalog(mutate)
    return jsonify(ok=True, item=item)


@app.patch("/api/catalog/restaurants/<restaurant_id>/menu/<item_id>")
def edit_menu_item(restaurant_id, item_id):
    if not require_admin():
        return jsonify(error="Unauthorized"), 401
    body = request.get_json(silent=True) or {}
    found = {"ok": False}

    def mutate(catalog):
        for it in catalog.get("menu", {}).get(restaurant_id, []):
            if it.get("id") == item_id:
                if "name" in body:
                    it["name"] = body["name"]
                if "price" in body:
                    it["price"] = float(body["price"])
                if "veg" in body:
                    it["veg"] = bool(body["veg"])
                for field in ("emoji", "image"):
                    if field in body:
                        it[field] = body[field]
                found["ok"] = True

    _mutate_catalog(mutate)
    if not found["ok"]:
        return jsonify(error="menu item not found"), 404
    return jsonify(ok=True)


@app.delete("/api/catalog/restaurants/<restaurant_id>/menu/<item_id>")
def delete_menu_item(restaurant_id, item_id):
    if not require_admin():
        return jsonify(error="Unauthorized"), 401

    def mutate(catalog):
        items = catalog.get("menu", {}).get(restaurant_id, [])
        catalog.setdefault("menu", {})[restaurant_id] = [it for it in items if it.get("id") != item_id]

    _mutate_catalog(mutate)
    return jsonify(ok=True)


@app.post("/api/catalog/riders")
def add_rider():
    if not require_admin():
        return jsonify(error="Unauthorized"), 401
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    phone = (body.get("phone") or "").strip()
    password = body.get("password") or ""
    if not name or not phone:
        return jsonify(error="name and phone are required"), 400
    if len(password) < 4:
        return jsonify(error="password must be at least 4 characters"), 400
    rider = {
        "id": f"rd_{int(time.time() * 1000)}",
        "name": name, "phone": phone,
        "commissionPercent": float(body.get("commissionPercent") or 10),
    }

    def mutate(catalog):
        catalog.setdefault("riders", []).append(rider)

    _mutate_catalog(mutate)
    # Password hash lives in its own doc, never included in the 'catalog' doc the frontend reads,
    # so a rider's password (even hashed) is never sent to any browser.
    creds_ref = get_db().collection("gubbiFast").document("riderCredentials")
    creds_snap = creds_ref.get()
    creds = creds_snap.to_dict() if creds_snap.exists else {}
    creds[rider["id"]] = generate_password_hash(password)
    creds_ref.set(creds)
    return jsonify(ok=True, rider=rider)


@app.post("/api/riders/login")
def rider_login():
    body = request.get_json(silent=True) or {}
    rider_id = body.get("riderId") or ""
    password = body.get("password") or ""
    creds_snap = get_db().collection("gubbiFast").document("riderCredentials").get()
    creds = creds_snap.to_dict() if creds_snap.exists else {}
    stored_hash = creds.get(rider_id)
    if not stored_hash or not check_password_hash(stored_hash, password):
        return jsonify(error="Incorrect password"), 401
    return jsonify(ok=True)


@app.post("/api/catalog/riders/<rider_id>/commission")
def update_commission(rider_id):
    if not require_admin():
        return jsonify(error="Unauthorized"), 401
    try:
        value = float((request.get_json(silent=True) or {}).get("commissionPercent"))
    except (TypeError, ValueError):
        return jsonify(error="commissionPercent must be a number"), 400
    if value < 0:
        return jsonify(error="commissionPercent must be >= 0"), 400
    found = {"ok": False}

    def mutate(catalog):
        for r in catalog.get("riders", []):
            if r.get("id") == rider_id:
                r["commissionPercent"] = value
                found["ok"] = True

    _mutate_catalog(mutate)
    if not found["ok"]:
        return jsonify(error="rider not found"), 404
    return jsonify(ok=True)


@app.patch("/api/catalog/riders/<rider_id>")
def edit_rider(rider_id):
    if not require_admin():
        return jsonify(error="Unauthorized"), 401
    body = request.get_json(silent=True) or {}
    found = {"ok": False}

    def mutate(catalog):
        for r in catalog.get("riders", []):
            if r.get("id") == rider_id:
                if "name" in body:
                    r["name"] = body["name"]
                if "phone" in body:
                    r["phone"] = body["phone"]
                if "commissionPercent" in body:
                    r["commissionPercent"] = float(body["commissionPercent"])
                found["ok"] = True

    _mutate_catalog(mutate)
    if not found["ok"]:
        return jsonify(error="rider not found"), 404
    new_password = body.get("password")
    if new_password:
        if len(new_password) < 4:
            return jsonify(error="password must be at least 4 characters"), 400
        creds_ref = get_db().collection("gubbiFast").document("riderCredentials")
        creds_snap = creds_ref.get()
        creds = creds_snap.to_dict() if creds_snap.exists else {}
        creds[rider_id] = generate_password_hash(new_password)
        creds_ref.set(creds)
    return jsonify(ok=True)


@app.delete("/api/catalog/riders/<rider_id>")
def delete_rider(rider_id):
    if not require_admin():
        return jsonify(error="Unauthorized"), 401

    def mutate(catalog):
        catalog["riders"] = [r for r in catalog.get("riders", []) if r.get("id") != rider_id]

    _mutate_catalog(mutate)
    creds_ref = get_db().collection("gubbiFast").document("riderCredentials")
    creds_snap = creds_ref.get()
    if creds_snap.exists:
        creds = creds_snap.to_dict()
        creds.pop(rider_id, None)
        creds_ref.set(creds)
    return jsonify(ok=True)


@app.post("/api/catalog/offers")
def add_offer():
    if not require_admin():
        return jsonify(error="Unauthorized"), 401
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify(error="title is required"), 400
    offer = {
        "id": f"off_{int(time.time() * 1000)}",
        "title": title,
        "sub": body.get("sub") or "Limited time offer",
        "emoji": body.get("emoji") or "🎁",
        "bg": body.get("bg") or "#C1442E",
        "restaurantId": body.get("restaurantId") or None,
    }

    def mutate(catalog):
        catalog.setdefault("offers", []).append(offer)

    _mutate_catalog(mutate)
    return jsonify(ok=True, offer=offer)


@app.patch("/api/catalog/offers/<offer_id>")
def edit_offer(offer_id):
    if not require_admin():
        return jsonify(error="Unauthorized"), 401
    body = request.get_json(silent=True) or {}
    found = {"ok": False}

    def mutate(catalog):
        for o in catalog.get("offers", []):
            if o.get("id") == offer_id:
                for field in ("title", "sub", "emoji", "bg"):
                    if field in body:
                        o[field] = body[field]
                if "restaurantId" in body:
                    o["restaurantId"] = body["restaurantId"] or None
                found["ok"] = True

    _mutate_catalog(mutate)
    if not found["ok"]:
        return jsonify(error="offer not found"), 404
    return jsonify(ok=True)


@app.delete("/api/catalog/offers/<offer_id>")
def delete_offer(offer_id):
    if not require_admin():
        return jsonify(error="Unauthorized"), 401

    def mutate(catalog):
        catalog["offers"] = [o for o in catalog.get("offers", []) if o.get("id") != offer_id]

    _mutate_catalog(mutate)
    return jsonify(ok=True)


@app.post("/api/settings")
def update_settings():
    if not require_admin():
        return jsonify(error="Unauthorized"), 401
    body = request.get_json(silent=True) or {}
    upi_id = (body.get("upiId") or "").strip()
    if not upi_id:
        return jsonify(error="upiId is required"), 400

    def mutate(catalog):
        settings = catalog.setdefault("settings", {})
        settings["upiId"] = upi_id
        if body.get("upiName"):
            settings["upiName"] = body["upiName"].strip()
        if body.get("adminPhone"):
            settings["adminPhone"] = body["adminPhone"].strip()
        if body.get("instagramUrl"):
            settings["instagramUrl"] = body["instagramUrl"].strip()
        if body.get("whatsappUrl"):
            settings["whatsappUrl"] = body["whatsappUrl"].strip()

    _mutate_catalog(mutate)
    return jsonify(ok=True)


@app.get("/api/health")
def health():
    return jsonify(ok=True, firebaseConfigured=bool(os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")))
