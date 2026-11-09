"""DocForge toolkit: the command line the DocForge.live skills run, on top of the engine."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("docforge-live")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0+source"
