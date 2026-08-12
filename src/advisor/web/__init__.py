"""Local web UI, served from 127.0.0.1 only.

The module is `server`, not `app`, so that `advisor.web.app` unambiguously means
the FastAPI instance below. When both existed, patching "advisor.web.app._backend"
resolved to the instance and failed with a confusing AttributeError.
"""

from advisor.web.server import app

__all__ = ["app"]
