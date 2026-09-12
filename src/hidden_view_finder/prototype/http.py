"""Bound HTTP header waits without timing out application work.

Uvicorn's keep-alive timer starts after a response and is cleared by the first
byte of the next request. This additional, absolute deadline covers initial
idle connections and incomplete headers, including requests after keep-alive.
Once H11 parses a request, body and application deadlines belong to the API.
"""
from __future__ import annotations

import asyncio

import h11
from uvicorn.protocols.http.h11_impl import H11Protocol


class HeaderTimeoutH11Protocol(H11Protocol):
    header_timeout_seconds = 5.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._header_timeout_handle: asyncio.TimerHandle | None = None

    def _cancel_header_timeout(self) -> None:
        if self._header_timeout_handle is not None:
            self._header_timeout_handle.cancel()
            self._header_timeout_handle = None

    def _waiting_for_headers(self) -> bool:
        return (
            self.transport is not None
            and not self.transport.is_closing()
            and self.conn.their_state is h11.IDLE
            and (self.cycle is None or self.cycle.response_complete)
        )

    def _sync_header_timeout(self) -> None:
        if not self._waiting_for_headers():
            self._cancel_header_timeout()
        elif self._header_timeout_handle is None:
            # Do not renew this deadline for individual bytes: slow header
            # streams must not occupy the bounded connection pool indefinitely.
            self._header_timeout_handle = self.loop.call_later(
                self.header_timeout_seconds, self._header_timeout
            )

    def _header_timeout(self) -> None:
        self._header_timeout_handle = None
        if self._waiting_for_headers():
            # An incomplete request need not be parsed or receive an HTTP
            # response. Closing releases its slot via connection_lost().
            self.transport.close()

    def connection_made(self, transport: asyncio.Transport) -> None:
        super().connection_made(transport)
        self._sync_header_timeout()

    def handle_events(self) -> None:
        super().handle_events()
        self._sync_header_timeout()

    def on_response_complete(self) -> None:
        super().on_response_complete()
        # The superclass may already have parsed a pipelined request; inspect
        # the resulting state rather than arming over that active request.
        self._sync_header_timeout()

    def connection_lost(self, exc: Exception | None) -> None:
        self._cancel_header_timeout()
        super().connection_lost(exc)

    def shutdown(self) -> None:
        self._cancel_header_timeout()
        super().shutdown()
