"""ColdSnake - a Python Icedrive client.

Icedrive killed WebDAV (new users blocked 2026-04-15, existing users being
sunset) and has no public API, so this drives the v3 desktop API that Icedrive's
own desktop and CLI clients use. See README.md for the protocol notes.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("coldsnake")
except PackageNotFoundError:                      # running from a source tree
    __version__ = "0.0.0"
