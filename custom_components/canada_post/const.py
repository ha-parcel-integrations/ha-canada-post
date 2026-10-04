"""Constants for the Canada Post parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "canada_post"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Ready to collect at a pickup location
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping a carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Both sources read the same detail payload, so the two variants are identical;
# the dict keeps a future divergence a one-line change. ``CAPABILITIES`` stays
# aliased to ``Tracking`` for the docs-site comparison table.
CAPABILITIES_BY_VARIANT = {
    "Tracking": frozenset({"delivery_window", "pickup_point", "url", "history"}),
    "Account": frozenset({"delivery_window", "pickup_point", "url", "history"}),
}
PENDING_CAPABILITIES_BY_VARIANT: dict[str, frozenset[str]] = {}
CAPABILITIES = CAPABILITIES_BY_VARIANT["Tracking"]
PENDING_CAPABILITIES: frozenset[str] = frozenset()

# Detail transport shared by both sources. All three headers are required: the
# host answers a 403 HTML page without them.
DETAIL_URL = (
    "https://www.canadapost-postescanada.ca/track-reperage/rs/track/json/"
    "package/{pin}/detail"
)
ALIAS_URL = (
    "https://www.canadapost-postescanada.ca/track-reperage/rs/track/json/package"
)
DETAIL_HEADERS = {
    "Authorization": "Basic Og==",
    "Referer": "https://www.canadapost-postescanada.ca/track-reperage/en",
    "X-Requested-With": "XMLHttpRequest",
    # The route answers 406 to a plain application/json Accept.
    "Accept": "application/vnd.cpc.trackweb-v1+json",
}
TRACKING_URL = (
    "https://www.canadapost-postescanada.ca/track-reperage/{lang}#/details/{pin}"
)

ACCOUNT_TOKEN_URL = "https://sso-osu.canadapost-postescanada.ca/mga/sps/oauth/oauth20/token"
ACCOUNT_CLIENT_ID = "cpc-nativeapp-2020"
ACCOUNT_SCOPE = "openid profile"
ACCOUNT_GRAPHQL_URL = (
    "https://b3n4knmt6famlj575e5pfn74lm.appsync-api.ca-central-1.amazonaws.com/graphql"
)
ACCOUNT_TOKEN_REFRESH_MARGIN_SECONDS = 60

CONF_SOURCE = "source"
SOURCE_ACCOUNT = "account"
SOURCE_TRACKING = "tracking"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ID_TOKEN = "id_token"
CONF_EXPIRES_AT = "expires_at"

# Tracked parcels of a tracking hub live in the entry options as a list of
# ``{tracking_code}`` dicts. Kept as dicts so future per-parcel fields slot in
# without an options migration. An account entry has no such list.
CONF_PARCELS = "parcels"
CONF_TRACKING_CODE = "tracking_code"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Dynamic, status-driven polling — unconditional across the suite, no
# user-facing interval option (see scaffold/CLAUDE.md's "Dynamic polling"
# section for the full algorithm and the reasoning behind it).
#
# Quiet window: no polling between these local hours except the two anchors
# below, for overnight / end-of-day catch-up.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6

# Cadence while polling is active (minutes). Hot = at least one tracked,
# not-yet-delivered parcel is out_for_delivery within HOT_LOOKAHEAD_HOURS of
# its planned_from (or has no planned_from at all); mid = anything else still
# in flight (registered, in_transit, at_pickup_point, unknown, problem,
# returning).
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1

# Small, stable per-install offset added to every computed interval so
# different installs don't all hit an anchor or tier boundary at the same
# second. Deterministic (hash of the config entry id), not random.
STAGGER_MINUTES = 7

# Per-parcel status history is opt-in and off by default, identical across the
# suite. Keep it off by default even when — as here — the timeline arrives in
# the same response and costs no extra request: it is a large attribute, and on
# carriers that need a second call per parcel the cost is real.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20
DETAIL_FETCH_CONCURRENCY = 5

# Help-wanted issues a one-shot warning points at, so a report lands on the
# question it answers.
_ISSUES = "https://github.com/ha-parcel-integrations/ha-canada-post/issues"
DELIVERY_WINDOW_ISSUE_URL = f"{_ISSUES}/1"
PICKUP_STATUS_ISSUE_URL = f"{_ISSUES}/2"
RETURN_EVENT_ISSUE_URL = f"{_ISSUES}/3"
