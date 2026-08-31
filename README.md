# Gubbi Fast — deployment guide

The app is a static frontend (`gubbi-fast.html`) plus a Python backend (`api/index.py`) that owns
**everything**: all restaurant/menu/pricing/settings data, and every write that matters —
placing an order, claiming/confirming/rejecting a payment, accepting/picking-up/delivering an
order, rider location updates, admin login, and all catalog edits. `gubbi-fast.html` ships with
no data and no config baked in at all — it only ever talks to the backend over `fetch()`, so
there's nothing sensitive (or even just the demo menu) sitting in the page source/dev tools.

## 1. Deploy to Vercel

1. Push this whole `gubbi-fast/` folder to a GitHub repo → **Vercel → New Project → Import**.
   No build command needed: `vercel.json` serves `gubbi-fast.html` at `/`, and Vercel
   auto-detects `api/index.py` (Flask) as a serverless function using `requirements.txt`.
2. That's it for admin login — it works immediately with the hardcoded default password
   **`Gubbi@Admin2026`** (set in `api/index.py`, never sent to the browser). Change it before
   giving this to real users: in **Vercel → Project → Settings → Environment Variables**, add:
   | Name | Value |
   |---|---|
   | `ADMIN_PASSWORD` | your real admin password (overrides the hardcoded default) |
   | `JWT_SECRET` | any long random string (e.g. output of `openssl rand -hex 32`) |
   | `FIREBASE_SERVICE_ACCOUNT_JSON` | full JSON of a Firebase service account key (step 2 below) |
3. Redeploy after adding the variables (Vercel → Deployments → ⋯ → Redeploy).

Admin login works even without step 2 (it doesn't touch Firestore). Everything else — the
restaurant list, placing orders, catalog edits, payment confirmation — needs
`FIREBASE_SERVICE_ACCOUNT_JSON` set; without it the site falls back to **single-device demo
mode** (browser `localStorage`, starting empty until you add restaurants via Admin).

## 2. Set up Firebase (required for real data + cross-device sync)

Free, no credit card needed:

1. https://console.firebase.google.com → **Add project**.
2. **Build → Firestore Database → Create database → Start in test mode**.
3. **Project settings → Service accounts → Generate new private key** → downloads a JSON file →
   paste its *entire contents* as the `FIREBASE_SERVICE_ACCOUNT_JSON` environment variable in
   Vercel (step 1.2 above). This key lets the Python backend read/write Firestore directly — it
   must never be put in `gubbi-fast.html` or any other browser-visible file. (No web app / no
   `firebaseConfig` needs registering — the frontend never talks to Firebase directly at all.)
4. **Firestore → Rules**, replace the default with:
   ```
   rules_version = '2';
   service cloud.firestore {
     match /databases/{database}/documents {
       match /gubbiFast/{doc} {
         allow read, write: if false; // everything goes through the Python backend
       }
     }
   }
   ```
   The frontend no longer reads Firestore directly (it reads through `GET /api/catalog` /
   `GET /api/live` instead), so this can now deny read *and* write for the browser's SDK
   entirely — the Admin SDK the backend uses **bypasses these rules by design**, so the backend
   keeps working regardless.

If you skip this, the frontend falls back to the browser's `localStorage`, which only works
for one device/browser — fine to try the app solo, not for real customers/riders/admin on
separate phones. In that fallback mode every action is client-only again (no backend to enforce
anything), same trade-off as before, and the app starts with zero restaurants until you add some
through Admin.

## 3. Set your UPI ID

Defaults to a placeholder (`yourname@upi`) seeded by the backend on first run — change it from
**Admin → Payments tab** once deployed (saved to the same backend as everything else), or edit
`DEFAULT_CATALOG["settings"]` in `api/index.py` before the first deploy.

## 4. Rider logins now require a password

Admin sets a password when adding a rider (**Admin → Riders → Add a rider**); the rider needs it
to log in. Passwords are hashed server-side and stored in a Firestore doc the browser never
reads — the frontend only ever sees rider names/phone/commission.

The 3 seeded demo riders (Ravi Kumar, Suresh Gowda, Manjunath B.) all use `rider123` — this works
both in single-device demo mode and once deployed with Firebase configured (the backend seeds
their password hashes the first time `GET /api/catalog` runs). Add real riders through Admin for
production use.

## 5. Admin can now edit and delete everything

**Admin → Restaurants/Riders/Offers** now has ✏️ Edit and 🗑️ Delete/Remove next to every
restaurant, menu item, rider and offer (prices, rider name/phone/commission/password, offer
copy — all editable). Deletes ask for confirmation first. On desktop-sized screens the
customer's "☰" menu is also replaced by the menu items shown directly in the top bar.

## 6. Rejected payments

If admin marks a payment as not received, the customer sees a "call Gubbi Fast" button (number
comes from **Admin → Payments → Admin contact phone**, defaults to 9123242102) plus a
"place order again" button that takes them straight back to that restaurant's menu.

---

## Answers to your questions

**"Is it responsive?"** Mobile-first (full-width, single column) below ~700px, widening into a
multi-column layout above that. Also fixed a subtle mobile bug: form inputs were 14px, which
makes Safari/Chrome on phones auto-zoom on tap (a common cause of "layout looks broken" on
mobile) — now 16px so that no longer happens. If it still looks wrong somewhere, the exact
device/width + what's overflowing or cut off would help me target the fix precisely.

**"Will the real food images show once deployed?"** Yes for every seeded restaurant/menu item —
they're already real photos (Wikimedia Commons/Wikipedia), not emojis, and load the same
whether run locally or deployed. Emoji only appears as a small fallback icon if *you* add a new
restaurant/dish through Admin without uploading a photo or URL for it.

**"Guest location always shows Basavanagudi — will deploying fix it?"** Very likely yes. Browsers
block `navigator.geolocation` on `file://` pages (which is how you were opening it locally) —
GPS access requires a "secure context", meaning `https://` (or `localhost`). Vercel serves over
HTTPS automatically, so once deployed, the browser will properly prompt for real location. The
Basavanagudi-ish coordinates you saw are the hardcoded fallback (`FALLBACK_LOC`) used only when
location access fails or is denied.

**"Cash on Delivery?"** Added — customers now pick UPI or COD at checkout. COD orders skip the
payment-verification step entirely and go straight to the kitchen/riders, same as before UPI was
added.

**"Can you make it so no one can debug/inspect it?"** No, and I want to be straight with you
about why, instead of adding fake protection that doesn't work: anything sent to a browser
(HTML/CSS/JS) is by definition downloaded and run on the visitor's own computer, so it can
always be read and modified there — this is true for every website, including banks' and
Google's. "Disable right-click" or "detect DevTools" tricks some sites use don't actually stop
anyone (they're trivially bypassed, e.g. by disabling JavaScript or using the browser's remote
debugging protocol) and mainly annoy legitimate users. Real security isn't about hiding
code — it's about never trusting the browser to enforce anything that matters. That's why
*every* write in this app — placing an order, claiming/confirming/rejecting payment,
accept/pickup/deliver, rider location, and all catalog edits — now goes through `api/index.py`
with server-side validation (e.g. order totals are recomputed from the real menu prices, not
trusted from the browser; a rider can't mark someone else's order delivered; only a valid admin
session can edit the catalog), and Firestore rules (step 5 above) reject any write that doesn't
come from the backend. Inspecting the frontend source no longer gives an attacker a way to
bypass any of that — the worst they can do is see the same HTML/CSS/JS everyone's browser
already downloaded, which was never secret to begin with.

**Remaining known trade-off:** there's still no real *user accounts* for customers/riders (a
phone number/rider selection is trusted as identity, not password-protected) — that's true to
how the app was designed from the start (frictionless demo login) rather than a security bug
introduced by this change. Add real authentication (e.g. Firebase Auth with OTP) if you want
customers/riders to have protected accounts too.

