"""Constants for the imhd.sk integration."""

DOMAIN = "imhd_sk"

BASE_URL = "https://imhd.sk"
SOCKETIO_PATH = "rt/sio2"

SERVICE_GET_DEPARTURES = "get_departures"

ATTR_STOP_ID = "stop_id"
ATTR_LINES = "lines"
ATTR_PLATFORMS = "platforms"
ATTR_LIMIT = "limit"
ATTR_TIMEOUT = "timeout"
ATTR_SETTLE = "settle"

DEFAULT_LIMIT = 10
DEFAULT_TIMEOUT = 10
# One-shot fetch: board is complete after this many seconds without a new `tabs` message.
DEFAULT_SETTLE = 2.0
# Departure stays listed this long after its time (vehicle may still be at the stop).
DEPARTED_GRACE_SECONDS = 30

# Stop (sensor) entries
CONF_STOP_ID = "stop_id"
CONF_STOP = "stop"  # config-flow field: selected stop / URL / number
CONF_PLATFORM_LABELS = "platform_labels"
CONF_LINES = "lines"
CONF_PLATFORM_SENSORS = "platform_sensors"
CONF_LINE_SENSORS = "line_sensors"
CONF_MAX_DEPARTURES = "max_departures"
CONF_MODE = "mode"
CONF_SCAN_INTERVAL = "scan_interval"  # minutes, polling mode
CONF_ENTRY_TYPE = "entry_type"

ENTRY_TYPE_ACTION = "action"
ENTRY_TYPE_STOP = "stop"

MODE_PUSH = "push"
MODE_POLL = "poll"
MODE_MANUAL = "manual"  # fetch only on request (button / update_entity)

DEFAULT_MAX_DEPARTURES = 10
DEFAULT_SCAN_INTERVAL = 2

# Sensors re-evaluate (drop departed connections) this often between data updates.
REFRESH_SECONDS = 30

# Push mode: reconnect when no `tabs` arrive for this long (normally ~every minute).
WATCHDOG_SECONDS = 300
