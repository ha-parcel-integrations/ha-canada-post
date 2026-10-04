# Canada Post Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-canada-post.svg)](https://github.com/ha-parcel-integrations/ha-canada-post/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-canada-post/total.svg)](https://github.com/ha-parcel-integrations/ha-canada-post/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [Canada Post](https://www.canadapost-postescanada.ca/track-reperage/en) parcels. Choose either tracking codes you enter yourself, or your Canada Post account's saved tracking list.

> **Unrecognised data is reported, not hidden.** Whenever the integration meets a status, event or field shape it does not know yet, it logs a one-time warning with a copy-paste issue link: please report it.

Part of the [ha-parcel-integrations](https://ha-parcel-integrations.github.io/) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Two sources, chosen at setup: **Tracking codes** (no account; you add PINs, international tracking numbers or delivery notice card numbers) or **Account** (parcels saved in your Canada Post account are imported automatically)
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …), Canada Post's own status text, the expected delivery date and a tracking deep-link, plus the pickup point while a parcel waits at one
- Summary sensors: incoming parcels, next delivery, awaiting pickup, recently delivered parcels
- Read-only **Deliveries** calendar with the expected delivery dates (and times, when Canada Post gives a delivery window)
- `canada_post.track_parcel` / `canada_post.untrack_parcel` services for the tracking-codes source, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2024.12 or newer
- **Tracking codes:** a Canada Post PIN (11, 12 or 16 digits), an international tracking number (such as `RN123456789CA`) or the number on a delivery notice card. No account needed.
- **Account:** your Canada Post username and password. Accounts with two-step verification switched on are not supported. Your password is used once to sign in and is never stored.

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-canada-post` as an **Integration**.
3. Install **Canada Post** and restart Home Assistant.

### Manual

Copy `custom_components/canada_post` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → Canada Post** and pick a source:

- **Tracking codes** — nothing to fill in; the hub is created immediately. Only one tracking hub can exist. Add parcels via the integration's **Configure** dialog, the [`canada_post.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). A delivery notice card number is looked up once and replaced by the parcel's PIN.
- **Account** — enter your Canada Post username and password. The integration reads the tracking list saved in your account (it only reads: it never adds, renames or deletes anything) and keeps it up to date. Add one entry per Canada Post login. Parcels you have marked "not mine" in Canada Post are left out. Every account parcel is treated as incoming.

If Canada Post stops accepting the stored sign-in, Home Assistant asks for your password again.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels (tracking codes only) | Add / remove | — | Manage the tracked codes. Changes apply immediately, no restart. An account entry has no parcel list: Canada Post's saved list is the inbox. |
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. |

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule with nothing to configure.

## Dynamic polling

Polling isn't a setting here — the integration adjusts its own cadence to
what your tracked parcels are actually doing:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM), so an overnight update is never missed.
- **Hot (every 15 minutes)** — while any tracked parcel is out for delivery
  today, starting an hour before its delivery window opens (or immediately if
  no window is known yet).
- **Normal (every 45 minutes)** — for anything else still on its way.
- **Fully paused** (tracking codes only) — once every tracked parcel has been delivered, or nothing
  is tracked at all, polling stops until you add a parcel back (adding one
  always triggers an immediate check, regardless of the pause). An account
  entry never pauses, because that is also how new parcels are discovered.
- A small, fixed per-hub offset is added on top, so not every Canada Post
  hub out there polls at exactly the same second.

Canada Post gives an expected delivery *date*. Until a delivery window is seen, a parcel that is out for delivery counts as "hot" from the start of that day. An account entry makes one list call and then one lookup per parcel that is not yet delivered.

## Removal

Standard HA removal applies: **Settings → Devices & Services → Canada Post → ⋮ → Delete**. Nothing is stored on Canada Post's side. For an account entry, the tokens stored in Home Assistant are removed with it.

## Sensors

| Entity | Description |
|---|---|
| `sensor.canada_post_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.canada_post_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel. An account parcel with a label in Canada Post is named after that label |
| `sensor.canada_post_next_delivery` | Earliest expected delivery moment across all active parcels |
| `sensor.canada_post_awaiting_pickup` | Parcels ready to collect at a pickup point |
| `sensor.canada_post_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.canada_post_last_successful_update` | Diagnostic: when Canada Post was last polled successfully |

An account entry's device is named after the login (`Canada Post (<username>)`), so its entity IDs carry the username, e.g. `sensor.canada_post_<username>_incoming_parcels`.

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning |
|---|---|
| `registered` | Announced to Canada Post |
| `in_transit` | In Canada Post's network |
| `out_for_delivery` | With the courier today |
| `at_pickup_point` | Waiting for you at a pickup location |
| `delivered` | Delivered |
| `returning` | Being returned to the sender |
| `problem` | Canada Post reports an alert, a failed delivery attempt or a customs hold |
| `unknown` | No history yet, or a status and history we cannot map yet |

Canada Post's own status string is always available as `raw_status`. Variants such as `InTransit-Item-Delay` are matched to their family and logged once. A status string that is not recognised at all takes the status of the newest recognised tracking event; only when that fails too is it `unknown`, with a one-time warning. With history enabled, every history entry carries a canonical status as well.

## Events

The integration fires these on the event bus (also available as device triggers on the Canada Post device):

| Event | When |
|---|---|
| `canada_post_parcel_registered` | A new parcel appears in the active list (an account parcel is announced when it shows up in your list) |
| `canada_post_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `canada_post_parcel_delivered` | A parcel is delivered |
| `canada_post_parcel_delivery_time_changed` | The expected delivery date changes |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

## Services

| Service | Fields | Description |
|---|---|---|
| `canada_post.track_parcel` | `tracking_code` | Start tracking a parcel (PIN, international number or notice card number) |
| `canada_post.untrack_parcel` | `tracking_code` | Stop tracking a parcel |

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.canada_post: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — Canada Post has no history for it yet (an unknown or expired PIN looks the same), or the code is wrong. It will pick up automatically once scanned.
- **"Two-step verification is not supported"** — the account has two-step verification switched on. Turn it off for the account, or use the tracking-codes source. Support is tracked in [#4](https://github.com/ha-parcel-integrations/ha-canada-post/issues/4).
- **A notice card number is not found** — Canada Post found no single matching parcel for it. Enter the parcel's PIN instead.
- **A status logs "Unrecognised Canada Post status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-canada-post/issues/new) with the logged line so the mapping can be extended.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by Canada Post, Home Assistant, or any other third party referenced in this project. Please don't contact Canada Post for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration may rely on public, unofficial, or undocumented carrier interfaces, accessed with your own account or API key where required. These may change or be withdrawn without notice and may be subject to Canada Post's terms. Data is sent only to Canada Post's own services or those of its group; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

The tracking-codes source uses the same public tracking service as the Canada Post consumer website. The account source signs in with your own Canada Post account and only reads your saved tracking list.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
