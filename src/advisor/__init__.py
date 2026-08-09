"""Fantasy football advisor — a conversational companion for in-season decisions."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("advisor")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
