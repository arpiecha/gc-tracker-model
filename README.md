# GC Tracker Template (`gc-tracker-template`)

One receipt tracker for every job. This is the template repo the live tracker is deployed from. Snap a receipt on your phone, Claude reads
it, it lands on the right job's dashboard with the photo attached.

Adding a new job is a name and an address on a form. No API keys, no Google
Sheet, no Dropbox, no Telegram bot, no new deployment.

## The pages

| Page | What it's for |
| --- | --- |
| `/` | Dashboard: pick a job, see totals and the receipts table, add a receipt, export to Excel |
| `/c/<job-slug>` | The dashboard opened straight to one job, e.g. `/c/1041-arbor-ln` |

The page asks for the password once and remembers it on that device.

### Put it on your phone

Open the site in Safari → Share → **Add to Home Screen**. It gets its own icon,
opens full screen like an app and goes straight to the dashboard. **Upload
Receipt** there lets you take a photo or pick one from the library.

## Adding a job

A fresh install shows **+ New Project** — name (and optionally address and
notes) → Add. That is the whole setup.

Extra jobs are a paid add-on, so once a job exists the button is gone from the
page. To add another, `POST /clients` with the passcode, or put the button back
by moving it out of the `#no-clients` block in `static/index.html`.

## Switching jobs

With more than one job, the name at the top of the page becomes the switcher:
tap it for the list, with each job's receipt count beside it. One job and it is
just the heading.

## Project tab

Rename the project (the name at the top of the page), set the start date the
counter at the top runs from, change the passcode, turn construction draws on
or off, edit the receipt categories, and pick light, dark or auto for the
theme. The theme choice is per device.

**Categories** are what the receipt form offers and what Claude is told to
file receipts under. They start as Materials, Labor, Mortgage and MISC and
live in the database once edited. Removing one leaves receipts already filed
under it alone — they keep their label, the category just leaves the picker.
There is always at least one.

The passcode lives in the database once it has been changed here;
`ADMIN_PASSWORD` is only the starting value. Changing it signs out every
other device.

To run a job with no passcode at all — everyone on site opens the link and is
straight in — set `REQUIRE_PASSWORD=false` on the service. The lock screen and
this box disappear, and anyone with the link can add and delete receipts, so
it is on unless you turn it off.

## Construction draws

Off unless a job is on a construction loan. The switch is in the Project tab;
with it on the dashboard gains **Draws received**, **Out of pocket** (total
spent minus the draws) and, once an approved loan amount is entered,
**Loan remaining** — plus a **Construction Draws** panel beside the chart to
log each draw with its date and note, see the running total, and delete one.
The Excel export gains a Draws sheet.

A draw is money **in**, never an expense: it is never subtracted from a
category or from Total spent, so what the job cost reads the same either way.
Turning the switch off only hides it — the draws stay and come back with it.

## Editing a receipt

Every row in the receipt log has a pencil beside its ✕. It opens the same form
the upload flow confirms into, filled in with what is stored, and saving it
updates that receipt rather than adding another. The photo is left alone.

Switching a receipt between Purchase and Return flips the sign for you, so the
totals stay right without retyping the amount.

On a phone the log drops to two lines per receipt — store and amount, then the
type, date and category — because seven columns cannot fit a phone without
cutting the amount or the word "purchase" short.

## Bills due

Each job has a **Bills due** list: a name, the day of the month it's due, and
optionally an amount. The dashboard shows what's coming and how many days
away. Nothing is sent anywhere — it's a list to look at.

## Running it

Everything is one Railway project:

- a **Flask** service (this repo)
- a **Postgres** database
- a **volume** mounted at `/data` for receipt photos

### Environment variables

| Variable | What it is |
| --- | --- |
| `DATABASE_URL` | Postgres connection string |
| `ADMIN_PASSWORD` | Starting passcode. Once changed in the Project tab, the stored one wins |
| `REQUIRE_PASSWORD` | `false` opens the tracker to anyone with the link. Defaults to on |
| `ANTHROPIC_API_KEY` | Used to read receipts |
| `RECEIPT_STORAGE_DIR` | Where photos are written. `/data/receipts` |

`PORT` is provided by Railway.

If `ANTHROPIC_API_KEY` is missing the site still runs — only receipt reading
fails, and it says so. You can still enter receipts by hand.

## How the pieces fit

```
app.py               Flask: API + serves the dashboard
db.py                Tables: clients, receipts, bills, draws
storage.py           Receipt photos on the volume
claude_receipts.py   The prompt and the Claude call
static/index.html    Dashboard
static/manifest.json Home-screen app details
```

Tables are created on startup, so a fresh database needs no migration step.

## API

Everything except `/`, `/c/<slug>`, `/static/*` and `/health` needs the
password, sent as an `X-Admin-Password` header. `/receipts/<id>/image` also
accepts `?key=`, which is what lets the **View** link be a plain link.

```
GET    /health
GET    /auth-check                 is this password right?

GET    /clients                    with receipt counts
POST   /clients                    {name, address?, notes?}
GET    /clients/<id>
PATCH  /clients/<id>
DELETE /clients/<id>               also deletes its receipts and bills

GET    /clients/<id>/receipts      newest first
POST   /analyze                    photo -> Claude -> receipt fields
POST   /save                       {client_id, date, store, category, type,
                                    amount, items?, notes?, image_base64?,
                                    source, force?}
PATCH  /receipts/<id>              correct a saved one; the photo is left as is
DELETE /receipts/<id>
GET    /receipts/<id>/image        the photo behind View

POST   /settings/password          {current_password, new_password}

GET    /categories                 with a receipt count each
POST   /categories                 {name}
DELETE /categories/<name>

GET    /clients/<id>/bills         soonest first
POST   /clients/<id>/bills         {name, due_day, amount?, notes?}
DELETE /bills/<id>

GET    /clients/<id>/draws         oldest first
POST   /clients/<id>/draws         {date, amount, note?}
DELETE /draws/<id>
```

The draws switch and the approved loan amount are per job, so they ride on
the client: `PATCH /clients/<id>` with `{draws_enabled: true}` or
`{loan_amount: 300000}` (`null` clears it).

## Notes

- **Returns** are stored as negative amounts, so the totals are net spend.
- **Duplicates** are caught per job by matching the date and the amount. Store
  names are not part of it — receipts write them inconsistently — so the same
  total on the same day is what counts as already logged. Amounts compare as
  whole cents and keep their sign, so a one-cent difference is a different
  receipt and a return never collides with a purchase. You always get the
  choice to save anyway.
- **Photos** are shrunk to 1600px in the browser before upload. Phone photos
  are 5-10MB and the API rejects images over 5MB.
- Photos live on the volume, not in the database. Keep the volume when moving
  the service or the **View** links stop working.
