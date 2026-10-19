"""nastycat-host: a read-only Chrome Native Messaging host answering "is this PR merged?" for NastyCat.

A separate program shipped in the `misscat` distribution. It reuses BadCat's `gh` client and the
Native Messaging framing of `badcat_host`; nothing in MissCat, BadCat or badcat-host depends on it.
"""
from misscat import __version__  # one repository, one distribution, one version

__all__ = ["__version__"]
