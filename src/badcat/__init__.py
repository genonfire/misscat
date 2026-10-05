"""BadCat: a deterministic GitHub PR review-state watcher with opt-in squash merge.

Shipped by the `misscat` distribution; it shares only small generic helpers with MissCat and
keeps its own state, lock and (nonexistent) workspace.
"""
from misscat import __version__  # one repository, one distribution, one version

__all__ = ["__version__"]
