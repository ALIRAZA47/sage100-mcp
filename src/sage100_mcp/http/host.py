"""The running HTTP server (SPEC.md §10.2): the runtime behind the app, under uvicorn.

``sage100-mcp serve`` runs it in the foreground and the Windows service (service.py) runs it for the
service control manager; both stop it the same way, by asking uvicorn to exit and then closing the
runtime's connections.
"""

from __future__ import annotations

import logging

import uvicorn

from sage100_mcp import logs
from sage100_mcp.config import LoadedConfig
from sage100_mcp.http.app import build_http_app, token_store
from sage100_mcp.runtime import Runtime

logger = logging.getLogger(__name__)
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class Host:
    def __init__(self, loaded: LoadedConfig, runtime: Runtime | None = None) -> None:
        http = loaded.config.http
        self.loaded = loaded
        self.runtime = runtime or Runtime(loaded)
        for error in self.runtime.errors:
            logger.error("Serving nothing: %s", error)
        if http.auth == "bearer" and not token_store(self.runtime.home).records():
            logger.warning("No bearer tokens yet, so every request gets 401: run sage100-mcp token create")
        settings = uvicorn.Config(
            build_http_app(loaded, self.runtime),
            host=http.host,
            port=http.port,
            ssl_certfile=str(http.tls_cert) if http.tls_cert else None,
            ssl_keyfile=str(http.tls_key) if http.tls_key else None,
            log_level=loaded.config.server.log_level,
            server_header=False,
            timeout_graceful_shutdown=5,
        )
        logs.guard(*(logging.getLogger(name) for name in UVICORN_LOGGERS))  # uvicorn set up its own handlers
        self.server = uvicorn.Server(settings)

    def run(self) -> bool:
        """Serve until stopped; True when the server started at all."""
        http = self.loaded.config.http
        self.runtime.start()
        logger.info("MCP endpoint %s (%s auth), listening on %s", http.endpoint, http.auth, http.bind)
        try:
            self.server.run()
        finally:
            self.runtime.close()
        return bool(self.server.started)

    def stop(self) -> None:
        """Ask the server to finish its requests and exit; run() then returns. Safe from any thread."""
        self.server.should_exit = True
