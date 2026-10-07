"""badcat-host: a read-only Chrome Native Messaging host reporting BadCat `+1` review states.

A separate program shipped in the `misscat` distribution. It imports only BadCat's GitHub client
and review protocol (never the BadCat CLI, watcher or merge code) and keeps its own state.
"""
from misscat import __version__  # one repository, one distribution, one version

__all__ = ["__version__"]
