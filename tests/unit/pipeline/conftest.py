"""Pipeline test configuration.

Sets Prefect to use an ephemeral in-process server so unit tests don't
require a running Prefect API at localhost:4200.
"""
import os

os.environ.setdefault("PREFECT_API_URL", "")
os.environ.setdefault("PREFECT_SERVER_ALLOW_EPHEMERAL_MODE", "true")
