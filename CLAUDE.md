# Working in this repository

Home Assistant custom integration for **Canada Post** parcel tracking.
Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo.
No DTO layer.

API mechanics — endpoints, parameters, status vocabularies — live in the
private `carrier-research/canada-post/api/` and are **never** copied here.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| change which optional field this carrier populates vs. always returns `None` | Update `const.py`'s `CAPABILITIES` in the same commit — it feeds the comparison table on the docs site, so a field that starts (or stops) coming back non-null and isn't reflected there is a wrong claim on the website, not just a stale comment. If this carrier has more than one backend (a country-specific transport, not just a config option) with genuinely different field support, `CAPABILITIES` should be a `CAPABILITIES_BY_VARIANT` dict instead — one frozenset per backend, so a field only some backends populate doesn't get silently intersected away or overclaimed for the rest. A field you could not verify (no real parcel yet) goes in `PENDING_CAPABILITIES` instead of being left out — the site then says "awaiting data" rather than claiming the API never exposes it; move it into `CAPABILITIES` once confirmed |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).
- **`awaiting_pickup` is required here**: `ParcelStatus.AT_PICKUP_POINT` is
  reachable. Say "pickup point", not
  "post office"/"counter", for the generic concept. `en_route_to_pickup_point`
  is deliberately **not** built: `pickup` is only ever `status is
  AT_PICKUP_POINT`, so that sensor would be permanently empty.

## Carrier-specific notes

**API mechanics live in `carrier-research/canada-post/api/` (private research
repo)** — endpoints, headers, the token flow, the response model and the status
vocabulary. Do not duplicate them here; this section is integration-level
decisions only.

**Structure, options flow, dynamic polling and module layout are suite-wide**
and identical in every carrier — the authoritative spec is
[`ha-carrier-template/scaffold/CLAUDE.md`](https://github.com/ha-parcel-integrations/ha-carrier-template/blob/main/scaffold/CLAUDE.md).
This repo follows it, with the two-source layout of `ha-bpost`.

**Confirmed on real data; the WARNING net stays for what is still unseen.**
The tracking payload is confirmed on real parcels (in transit, returned,
customs-refused, waiting at a pickup point) and the account source on a real
login. Still unseen, each logging a one-shot WARNING when it first appears: an
expected-delivery window, an event time or zone-offset format the parser does
not understand, an unknown status or event type that history cannot resolve,
and an unexpected token or list-item shape.

**Two sources in one domain — `tracking/` and `account/` packages.**
`entry.data[CONF_SOURCE]` (`tracking` / `account`) is read at every dispatch
site (`__init__.py`, `sensor.py` via the coordinator, `services.py`,
`config_flow.py`); there is no default, because no entry predates the key.
- **Unlike bpost, the sources share one normaliser.** The account list carries
  identifiers only, so the account coordinator fetches each parcel through
  `tracking/api.py` and normalises through `tracking/parcels.normalize_parcel`.
  Sign-in, tokens and the list call stay in `account/`; nothing else crosses.
  `status.py` (the maps) and `events.py` (the bus contract) are shared one
  level up.
- **One tracking hub** (`unique_id = "tracking"`), enforced by unique id, not
  the manifest: `single_config_entry` is deliberately absent because account
  entries are one per login (`account:<lowercased username>`). An account
  entry's device is named `Canada Post (<username>)` so two logins stay apart.
- **An account entry has no parcel list**, so its options menu is `settings`
  only and `track_parcel` / `untrack_parcel` only ever target the tracking hub.
  The services go away when the tracking hub unloads, never for an account.
- **Every account parcel is incoming.** The list has no direction field, so
  the outgoing event pair does not exist here.
- **Account polling never suspends**, but reuses the tracking tiers and quiet
  window. Detail fetches run at most `DETAIL_FETCH_CONCURRENCY` at a time.
  Delivered parcels are not re-fetched; one failing parcel keeps its previous
  value; every fetch failing with nothing cached is `UpdateFailed`; a 429 backs
  off like the tracking source.
- **The update listener reacts to options only.** Token rotation writes
  `entry.data` mid-poll; refreshing on that would poll twice.
- **The account's own label names the per-parcel sensor and calendar entry**
  when set. A pickup-type list source is a hint only and never sets `pickup`.
  `raw["account"]` carries the list item's source, type and label; the cached
  detail is copied, not mutated.

**Account credentials: tokens are stored, the password never is.** Sign-in
exchanges username and password for tokens written to `entry.data` through a
token callback; the client renews them on its own and persists every new set.
A rejected renewal raises `CanadaPostAccountReauthRequired`, which the
**coordinator** must translate into `ConfigEntryAuthFailed` — a bare client
exception retries setup forever without a prompt (`tests/test_init.py` guards
the setup and the running path). A rate-limited or failed renewal is
transient, never a reauth. Reauth re-asks only the password and keeps the
unique id. A second-factor challenge aborts with `two_step_not_supported`
instead of looping. Transport constants live only in `const.py` and never
reach the UI, diagnostics or a log line; client errors never carry a response
body. `username` and all tokens are in `diagnostics.TO_REDACT`.

**Read-only against the account.** Never write to the saved list. Items the
user flagged "not mine" and deleted items are skipped. Mail notifications are
out of scope for now.

**Transport is a closed set of routes.** The tracking client calls only the
detail and notice-card routes; `tests/tracking/test_api.py` pins the request
URL and headers. A transport failure (non-JSON, a wall page) is
`CanadaPostApiError`, never "not found". `manifest.json` keeps
`"requirements": []`.

**Tracking codes are validated strictly** (PIN, international S10 number, or
a delivery notice card number). A notice card is resolved to its PIN **once,
at add time** (options flow and `track_parcel`), so the PIN is what is stored
and polled. This deliberately departs from the suite's "accept every non-empty
code" convention; the formats are settled.

**Status: map, else history, else warn** (suite agreement). The status string
is mapped exactly, then by family prefix (its own one-shot WARNING — a
mitigation, not evidence); failing that, the newest history event that maps
decides; only then `unknown` with a one-shot WARNING. A delivered flag forces
`delivered`; a return flag makes the parcel `returning` unless delivered.
History entries carry a status too: event code first (one event type covers
several outcomes), then event type, then `unknown` with a one-shot WARNING.
`Attempted` maps to `problem` on purpose, matching the failed-attempt package
status, although the carrier groups it with in-transit.

**`delivered` never comes from event text**, only from the delivered flag or
the mapped status.

**The ETA is usually a date.** A date-only `planned_from` sits at local
midnight with `planned_to = None`, and the calendar shows it as an all-day
event. `delivery_window` claims an ETA signal only (the suite's 2026-09-05
ruling).

**`pickup_point` is populated only while the status is `at_pickup_point`**,
as "office name, city" from the newest event naming an office. The office has
no street line on this route.

**`sender` comes only from a shipper name**, never an address; `receiver` is
always `None`. Weight and dimensions do not exist on this route. `raw` is the
untouched payload, never trimmed. Diagnostics also redact the pickup office
and the origin/destination towns.

**History is sorted on the parsed tz-aware timestamp**, because the wire order
is not reliable; an unreadable time falls back to Home Assistant's local zone
and warns once.

**Translations:** English, French (Canada Post serves both) and Dutch. The URL
language follows `hass.config.language`.

**Do not build:** any route other than the two above, writes to the user's
account, reference-number lookups, code enumeration or bulk lookups, outgoing
events from the account source.

## Running tests

```
python -m pytest tests/ --cov=custom_components.canada_post
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file + `docs/` in the same
commit; the API reference lives in your own private research notes, never in
this repo.
