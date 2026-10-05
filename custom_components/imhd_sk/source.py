"""Data sources for stop sensors: persistent push or periodic one-shot polling."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

import socketio

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .api import Board, Departure, ImhdError, clean_info, fetch_board, filter_departures
from .const import BASE_URL, SOCKETIO_PATH, WATCHDOG_SECONDS

_LOGGER = logging.getLogger(__name__)


class StopSource:
    """Common interface used by sensors."""

    mode = "?"

    def __init__(self, stop_id: int) -> None:
        self.stop_id = stop_id
        self.connected = False
        self.last_update: datetime | None = None
        self.last_error: str | None = None
        self.reconnects = 0
        self.info: list[Any] = []
        self._departures: list[Departure] = []
        self._listeners: list[Callable[[], None]] = []
        # Data older than this means "not healthy" even if a connection claims to be up.
        self.stale_after = timedelta(minutes=5)

    @property
    def healthy(self) -> bool | None:
        """Connected and receiving fresh data."""
        if not self.connected or self.last_update is None:
            return False
        return datetime.now(timezone.utc) - self.last_update <= self.stale_after

    @property
    def available(self) -> bool:
        """Whether sensors should be available."""
        return self.connected or bool(self._departures)

    def departures(
        self, lines: set[str] | None = None, platforms: set[str] | None = None
    ) -> list[Departure]:
        return filter_departures(self._departures, lines, platforms=platforms)

    @property
    def line_platforms(self) -> set[tuple[str, str]]:
        """(line, platform) pairs in the current board."""
        return {(d.line, d.platform) for d in self._departures if d.platform}

    @property
    def platforms(self) -> list[str]:
        """Platforms seen in the current board."""
        return sorted({d.platform for d in self._departures if d.platform})

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    async def start(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def stop(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class PushSource(StopSource):
    """Keeps a socket.io connection subscribed to the stop, supervised.

    python-socketio does not reconnect after a server-initiated disconnect, and
    a live connection can silently stop delivering `tabs`. So reconnection is
    handled here instead of by the library:

    * any disconnect (network, ping timeout, server kick) -> reconnect with
      exponential backoff, forever;
    * no `tabs` for `watchdog` seconds while connected -> drop and reconnect;
    * a (re)connection counts only once the first `tabs` arrives.
    """

    mode = "push"

    def __init__(
        self,
        hass: HomeAssistant,
        stop_id: int,
        http_session=None,
        *,
        timeout: float = 15,
        watchdog: float = WATCHDOG_SECONDS,
        check_interval: float = 30,
        backoff_min: float = 2,
        backoff_max: float = 60,
    ) -> None:
        super().__init__(stop_id)
        self._hass = hass
        self._session = http_session
        self._timeout = timeout
        self._watchdog = watchdog
        self._check_interval = check_interval
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        self.stale_after = timedelta(seconds=watchdog)
        self._board = Board()  # kept across reconnects; platforms get replaced
        self._sio: socketio.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        self._last_tabs = 0.0
        self._stopping = False

    async def start(self) -> None:
        """First connection must succeed (else ImhdError -> ConfigEntryNotReady)."""
        gone = await self._connect()
        self._task = self._hass.async_create_background_task(
            self._supervise(gone), f"imhd_sk push {self.stop_id}"
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self._drop()

    # -- internals --------------------------------------------------------

    async def _connect(self) -> asyncio.Event:
        """Open a connection, subscribe, wait for the first board.

        Returns an event that is set when this connection goes away.
        """
        kwargs: dict[str, Any] = {"reconnection": False}
        if self._session is not None:
            kwargs["http_session"] = self._session
        sio = socketio.AsyncClient(**kwargs)
        gone = asyncio.Event()
        first = asyncio.Event()

        @sio.event
        async def connect() -> None:
            # Subscription lives only within a connection: send it on every connect.
            await sio.emit("tabStart", [self.stop_id, "*"])
            await sio.emit("infoStart")

        @sio.event
        async def disconnect(*_args) -> None:
            gone.set()

        @sio.on("tabs")
        async def on_tabs(data) -> None:
            self._board.update(data)
            self._departures = self._board.departures
            self.last_update = datetime.now(timezone.utc)
            self._last_tabs = self._hass.loop.time()
            if not first.is_set():
                first.set()
                self.connected = True
            self._notify()

        @sio.on("iText")
        async def on_itext(data) -> None:
            self.info = clean_info(data)
            self._notify()

        self._sio = sio
        try:
            await sio.connect(
                BASE_URL,
                socketio_path=SOCKETIO_PATH,
                transports=["websocket", "polling"],
                wait_timeout=self._timeout,
            )
            await asyncio.wait_for(first.wait(), self._timeout)
        except Exception as err:  # noqa: BLE001 - any failure means "try again later"
            await self._drop()
            msg = str(err) or type(err).__name__
            if isinstance(err, TimeoutError):
                msg = f"žiadne dáta do {self._timeout:g} s"
            self.last_error = msg
            raise ImhdError(msg) from err
        self.last_error = None
        return gone

    async def _drop(self) -> None:
        sio, self._sio = self._sio, None
        if sio is not None:
            try:
                await sio.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if self.connected:
            self.connected = False
            self._notify()

    async def _watch(self, gone: asyncio.Event) -> str:
        """Block while the connection is alive and delivering data."""
        while True:
            try:
                await asyncio.wait_for(gone.wait(), self._check_interval)
                return "odpojené"
            except TimeoutError:
                silent = self._hass.loop.time() - self._last_tabs
                if silent > self._watchdog:
                    return f"žiadne dáta {silent:.0f} s"

    async def _supervise(self, gone: asyncio.Event) -> None:
        while not self._stopping:
            reason = await self._watch(gone)
            self.last_error = reason
            _LOGGER.warning(
                "imhd.sk zastávka %s: %s, pripájam sa znova", self.stop_id, reason
            )
            await self._drop()
            delay = self._backoff_min
            while not self._stopping:
                await asyncio.sleep(delay)
                try:
                    gone = await self._connect()
                except ImhdError as err:
                    _LOGGER.debug(
                        "imhd.sk zastávka %s: pripojenie zlyhalo (%s), ďalší pokus o %.0f s",
                        self.stop_id, err, min(delay * 2, self._backoff_max),
                    )
                    delay = min(delay * 2, self._backoff_max)
                    self._notify()
                    continue
                self.reconnects += 1
                _LOGGER.info("imhd.sk zastávka %s: spojenie obnovené", self.stop_id)
                break


class PollSource(StopSource):
    """One-shot fetch every `interval`; no connection kept in between.

    With `interval=None` (manual mode) nothing is fetched automatically, not even
    at startup; only `refresh()` (button / homeassistant.update_entity) fetches.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        stop_id: int,
        interval: timedelta | None,
        http_session=None,
    ) -> None:
        super().__init__(stop_id)
        self._hass = hass
        self._interval = interval
        self._session = http_session
        self._unsub: Callable[[], None] | None = None
        self._lock = asyncio.Lock()
        self.mode = "poll" if interval else "manual"
        if interval:
            # Two missed polls in a row = not healthy.
            self.stale_after = interval * 2 + timedelta(seconds=30)
        self._refreshed = False

    async def refresh(self) -> None:
        """Fetch now (also used by homeassistant.update_entity)."""
        if self._lock.locked():
            return
        async with self._lock:
            try:
                deps, info = await fetch_board(self.stop_id, http_session=self._session)
            except ImhdError as err:
                if self.connected:
                    _LOGGER.warning("imhd.sk zastávka %s: %s", self.stop_id, err)
                self.connected = False
                self.last_error = str(err)
            else:
                if not self.connected and self.last_update is not None:
                    self.reconnects += 1
                self._departures, self.info = deps, info
                self.connected = True
                self.last_error = None
                self.last_update = datetime.now(timezone.utc)
            self._refreshed = True
            self._notify()

    @property
    def healthy(self) -> bool | None:
        if self._interval is None:
            # Manual: "did the last requested refresh work?"; unknown before the first.
            return self.connected if self._refreshed else None
        return super().healthy

    @property
    def available(self) -> bool:
        # Manual sensors stay available; their data is as old as the last refresh.
        return True if self._interval is None else super().available

    async def start(self) -> None:
        if self._interval is None:
            return
        await self.refresh()
        if not self.connected:
            raise ImhdError("imhd.sk neposlalo dáta")

        @callback
        def _tick(_now) -> None:
            self._hass.async_create_task(self.refresh())

        self._unsub = async_track_time_interval(self._hass, _tick, self._interval)

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
