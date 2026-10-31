"""Test-wide setup: the bundled synthetic fixtures are used unless a test says otherwise."""

import os

# Must be set before `willay.api.app` is imported (it builds a module-level app).
os.environ["WILLAY_DATA_MODE"] = "synthetic"
