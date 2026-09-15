"""Runtime hooks for the data_management plugin.

The framework calls exported functions in this module by name. Currently:

  - install()       — after the plugin is placed in usr/plugins/
  - pre_update()    — immediately before pulling new plugin code
  - uninstall()     — before deleting the plugin directory

Hooks run inside the Agent Zero framework runtime, not the separate
agent execution environment. Use this module for plugin-owned setup
that the framework should drive automatically.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def install() -> None:
    """Verify the runtime has the Postgres driver we depend on.

    `psycopg` is the only third-party dependency. If it's not present in
    the framework runtime, log a clear warning. The plugin stays
    enabled so settings UI works, but tool calls will fail with a
    friendly message until the dependency is installed.
    """
    try:
        import psycopg  # noqa: F401
    except ImportError:
        log.warning(
            "data_management: psycopg is not installed. Tool calls "
            "will fail until you run: /opt/venv-a0/bin/pip install "
            "'psycopg[binary]>=3.1'"
        )


def pre_update() -> None:
    """No-op. Reserved for future migration steps."""
    return None


def uninstall() -> None:
    """No state to clean up — plugin keeps no system-side resources."""
    return None