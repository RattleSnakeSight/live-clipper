"""Publishing subsystem: delivery worker, provider adapters, destination OAuth and the /api/destinations|publish API."""
from . import store  # noqa: F401  (registers the `publish` migrations at import time)
