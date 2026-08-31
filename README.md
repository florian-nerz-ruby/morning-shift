# Alfred

A morning-shift task assistant for the reservations team. Proof of concept —
two checks so far: **Cancellation Check** and **In-House Check**.

## Data flow

Alfred reads encrypted PMS observations from the separate `diggies_pms`
database. It uses `PMS_DATABASE_URL` with a PostgreSQL role that has only
`SELECT` access, and decrypts an envelope only in process memory while
building a page. Its own `DATABASE_URL` remains the writable database for
rules, property settings, and staff workflow state.

The PMS importer in `diggies-api` deduplicates the raw Opera CSV, enriches
in-house rows with HAPI `bookedUnits.unitCount`, encrypts the normalized
payload, and deletes a source CSV only after a committed import. Alfred never
parses or stores a raw CSV. Configure the matching private key only as a
protected deployment secret:

```dotenv
PMS_DATABASE_URL=postgresql+psycopg://morning_shift_reader:...@127.0.0.1:5432/diggies_pms
PMS_DECRYPTION_PRIVATE_KEY='-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----'
PMS_DECRYPTION_KEY_ID=morning-shift-2026-01
```

The private key must never be committed, put in either database, or sent to a
browser. Prefer a systemd credential or secret manager over a general-purpose
environment file.

“Done” is stored only in Alfred's own `handled_findings` table, keyed by the
opaque PMS lookup and the payload fingerprint. A changed encrypted PMS record
therefore automatically reopens the task without placing a raw reservation ID
in Alfred's database.

Everything else Alfred *does* own - cancellation policy rules, hotel
names/currencies, the in-house balance/room thresholds, and which
reservations are ticked off - lives in its own tables, read and written by
Alfred itself through the Settings page and the check-off checkboxes.

Alfred's own database is picked by the `DATABASE_URL` environment variable
(SQLAlchemy under the hood) - unset, it defaults to a local SQLite file
(`data/alfred.db`), so nothing extra needs installing for local development.
Point it at Postgres in production
(`postgresql+psycopg2://user:pass@host/dbname`); the store/loader code is
identical either way.

`RuleStore`, `PropertyStore`, and `ThresholdRuleStore` each keep an
in-memory cache loaded once at startup and kept in sync on writes, rather
than querying on every read. That's not an optimization to skip - `get()` /
`get_currency()` / `get_threshold()` are each called once per reservation on
every check page (hundreds to thousands of times for a handful of distinct
values), so without the cache every page load turns into that many separate
database round-trips.

## Cancellation Check

For every reservation, Alfred compares the cancellation date/time against the
arrival date using the reservation's cancellation policy text
(`CANCELLATION_RULE`). Multiple rate codes can share the same policy, so rules
are keyed by the policy text, not the rate code. Anything cancelled too late
is flagged so staff know a fee needs to be booked, and what the fee should be
(1st night vs. full stay).

Every rule is one of:
- **X days before arrival** — free until then, then charged (1st night or full stay)
- **Non-refundable** — always charged, no free window at all

("Flexible 6PM DOA" is modeled as "0 days before arrival" with an optional
same-day time cutoff.)

Known policies are pre-seeded from patterns like `1st N <X>D Prior` and
`NRF Full`. Anything that doesn't match is flagged **"Needs setup"** rather
than guessed at — configure it on the **Settings** page, where every policy
text seen in the data can be reviewed and edited.

Other things Alfred does per reservation:
- Shows nights, booking source, and cancellation policy at a glance
- Shows the hotel by name, not just its property code. The `properties`
  table is the source of truth, not the raw export directly — the first
  time a code is seen, a name is auto-detected from `PROPERTY_NAME`
  (e.g. `"Ruby Emma Hotel Amsterdam"` → `"Emma"`) if that column is present.
  If it isn't, or a code has no name yet, it just falls back to showing the
  code until someone fills it in on the **Settings** page (or a later import
  happens to include a name for it, which upgrades it automatically). A name
  set by hand is never overwritten by auto-detection.
- Tags reservations that have a `SHIJI` external reference as **migrated
  from the legacy Shiji system**, with its reference number
  (`EXTERNAL_REF_NUMBER`, e.g. `ZRH01-0209405`) one click to copy — also
  optional, hidden if not present in the export
- Same treatment for `TA_RECORD_LOCATOR` - the booking channel's own
  reference (Booking.com's number, Expedia's itinerary ID, etc.), labelled
  with the source name (`SOURCE_CODE_DESCRIPTION`) and one click to copy, for
  looking a reservation up directly in that channel's own portal
- Links directly to the reservation in Opera Cloud (`RESERVATION_ID` +
  `PROPERTY` build the deeplink; the ID itself is never shown, only the
  confirmation number)
- Lets staff tick a reservation off once its fee is booked — persisted to
  the `handled_reservations` table, survives restarts

## In-House Check

Reads the newest successfully completed encrypted in-house snapshot. Same
dedup and Shiji/travel-agent-locator tagging as above, but the raw export
doesn't carry a `PROPERTY_NAME` column - hotel names come
entirely from the shared property mapping (see below), which is exactly
the "a source might only ever have the code" case that mapping was built
for.

A folio's `BALANCE` is normally negative while the guest is in credit (a
deposit/prepayment held against the stay), and positive once they owe money.
The balance side of the flag is a severity ladder, not independent checks -
each reservation shows whichever *one* of these is most specific, since a €0
or positive balance always technically also clears the "buffer thin" bar,
and repeating that fact doesn't help anyone decide what to do:
1. **Balance is positive** (worst - guest currently owes money)
2. **Balance is exactly zero** (no cushion left at all)
3. **Balance is above the property's threshold** (credit cushion wearing
   thin - default -70, in the property's own currency; see below)

Independently of that, **more than one room** on a reservation is its own
separate flag, and can show up alongside whichever balance reason applies.

A row's color is graded by the worst reason it was flagged for, not a flat
"flagged = red" - full red is reserved for a positive balance (money actually
owed), zero/thin-buffer gets a lighter amber, and a reservation flagged only
for its room count gets no background tint at all (just the pill and a blue
dot), since that's not a financial risk worth the same visual alarm.

Since balances come out in each property's own currency (`CURRENCY`, a
3-letter code) rather than one shared currency, both thresholds - the
balance buffer and the room count - are driven by a small rule engine on the
**Settings** page rather than a single hardcoded number:
- **One default** that applies to any hotel not covered by a rule below
- **Any number of named rules**, each with its own threshold and the set of
  hotels it applies to (picked from the same chip-picker used for the
  dashboard's hotel scope, with "+ all EUR" / "+ all CHF" etc. quick-select
  buttons per currency actually in use)
- A hotel belongs to **at most one rule at a time** - assigning it to a new
  one silently removes it from wherever it was, so there's never an
  ambiguous "which rule wins"
- A read-only resolved table shows, per hotel, its currency, effective
  threshold, and which rule (or "Default") it's coming from - and flags any
  hotel still on the untouched EUR-sized default despite not being in EUR

The room-count rule is deliberately simpler: one flat number (a row in
`app_settings`), the same for every hotel. Unlike balance, "how many rooms
counts as a lot" isn't a currency or per-property concern, so it doesn't get
the rule-engine treatment - just a single Settings field.

Handled state is tracked separately from the Cancellation Check (a
`check_type` column on the same `handled_reservations` table), so ticking
something off here has no effect on the other check, even if the exact same
reservation happens to appear in both exports.

## Shared across both checks

- **Hotel names and currencies** aren't parsed per-export - they live in one
  `properties` table, the source of truth regardless of which export a code
  first showed up in. The first time a code is seen, a
  name is auto-detected from `PROPERTY_NAME` (e.g. `"Ruby Emma Hotel
  Amsterdam"` → `"Emma"`) and a currency from `CURRENCY`, wherever those
  columns happen to be present. If either isn't, or a code has no value yet,
  it falls back (name → the code itself, currency → EUR) until someone fills
  it in on the **Settings** page - or a later import happens to include it,
  which upgrades it automatically. Name and currency are tracked
  independently, and anything set by hand is never overwritten by
  auto-detection.
- The dashboard opens with a hotel picker as the first thing you see - pick
  which hotels you're working with and everything else (counts, both check
  lists) is scoped to just those. It's remembered per browser session (a
  cookie, good for ~16 hours), not shared across everyone using the tool, so
  different people at different desks can have different hotel selections at
  the same time.
- Both checks use the same "Flagged / All / Done" view switch and the same
  green-flash-then-fade animation when something is ticked off.

## Running it

```bash
pip install -r requirements.txt

# First time only - creates the local SQLite database and pulls in whatever
# real config is still sitting in data/*.json from before this migration:
python migrate_config_data.py

python app.py
```

Then open http://127.0.0.1:5000

No `DATABASE_URL` set → a local SQLite file at `data/alfred.db`. Set it to a
Postgres URL to point at a real database instead - nothing else changes.

There is no upload button and `seed_from_csv.py` is deliberately disabled:
reservation data arrives only via the encrypted `diggies-api` PMS importer.
