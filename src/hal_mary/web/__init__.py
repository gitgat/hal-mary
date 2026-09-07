"""The web app Caroline actually looks at.

:mod:`hal_mary.web.app` builds the FastAPI application; :mod:`hal_mary.web.serve`
runs it and works out the URL to hand to a phone. Nothing here is imported by
``hal-mary --help``: FastAPI and uvicorn cost real import time, so the CLI pulls
them in only inside ``serve``.
"""

__all__ = ["app", "serve"]
