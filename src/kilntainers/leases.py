"""Application-owned sandbox capabilities, independent of MCP transport sessions."""

import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio

from kilntainers.computers import ComputerRegistry, random_computer_id
from kilntainers.errors import BackendError

if TYPE_CHECKING:
    from kilntainers.server import SessionContext

log = logging.getLogger(__name__)


@dataclass
class SandboxLease:
    handle: str
    session: "SessionContext"
    last_used: float = field(default_factory=time.monotonic)
    pending: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set[asyncio.Task] = field(default_factory=set)


class SandboxLeases:
    """A single authenticated owner's bounded, independently expiring handles.

    Authentication happens before MCP dispatch. A handle additionally selects an
    isolated context; it is never derived from a readable name or transport ID.
    Bearer sharing is intentional owner sharing, not a multi-tenant identity model.
    """

    def __init__(self, backend, config):
        self.backend = backend
        self.config = config
        self.registry = ComputerRegistry(backend)
        self.leases: dict[str, SandboxLease] = {}
        self.default_handle: str | None = None
        self.pending = 0
        self.closed = False
        self.reaper: asyncio.Task | None = None
        self.retired: list[SandboxLease] = []

    async def __aenter__(self):
        self.reaper = asyncio.create_task(self._reap())
        return self

    async def __aexit__(self, *exc):
        with anyio.CancelScope(shield=True):
            self.closed = True
            if self.reaper:
                self.reaper.cancel()
                await asyncio.gather(self.reaper, return_exceptions=True)
            leases = [*self.leases.values(), *self.retired]
            self.leases.clear()
            self.retired.clear()
            outcomes = await asyncio.gather(*(self._close(lease) for lease in leases))
            pending_complete = await self.registry.cleanup_pending(wait=True)
            if not all(outcomes) or not pending_complete:
                raise BackendError("Sandbox shutdown cleanup is incomplete")

    async def _close(self, lease):
        for task in tuple(lease.tasks):
            if task is not asyncio.current_task():
                task.cancel()
        tasks = [task for task in lease.tasks if task is not asyncio.current_task()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=22)
            for task in pending:
                task.cancel()
        try:
            await asyncio.wait_for(lease.session.cleanup(), timeout=25)
        except Exception:
            # Keep enough state to retry idle cleanup; do not log provider secrets.
            if not self.closed and lease not in self.retired:
                self.retired.append(lease)
            log.error(
                "Sandbox cleanup did not complete; operator cleanup may be required"
            )
            return False
        return True

    async def expire_idle(self, *, now=None):
        now = time.monotonic() if now is None else now
        stale = [
            lease
            for lease in self.leases.values()
            if lease.pending == 0
            and now - lease.last_used >= self.config.session_timeout
        ]
        for lease in stale:
            self.leases.pop(lease.handle, None)
        retry = self.retired
        self.retired = []
        await asyncio.gather(*(self._close(lease) for lease in [*stale, *retry]))

    async def _reap(self):
        while True:
            await asyncio.sleep(min(1, self.config.session_timeout / 2))
            if self.config.transport == "http":
                await self.expire_idle()
            if not await self.registry.cleanup_pending():
                log.error("Abandoned sandbox creation cleanup remains pending")

    async def release(self, handle=None):
        if self.config.transport == "stdio" and handle is None:
            handle = self.default_handle
        if not handle or handle not in self.leases:
            raise BackendError("Unknown or expired sandbox_handle")
        lease = self.leases.pop(handle)
        if handle == self.default_handle:
            self.default_handle = None
        return await self._close(lease)

    @asynccontextmanager
    async def use(self, handle=None, *, create=False, computer_id=None, temporary=True):
        from kilntainers.server import SessionContext

        if self.closed:
            raise BackendError("Server is shutting down")
        if self.pending >= self.config.max_pending_requests:
            raise BackendError("Server request queue is full")
        if self.config.transport == "stdio" and handle is None:
            handle = self.default_handle
        fresh = False
        if handle is not None:
            if (
                not isinstance(handle, str)
                or len(handle) > 128
                or handle not in self.leases
            ):
                raise BackendError("Unknown or expired sandbox_handle")
            lease = self.leases[handle]
        else:
            if not create:
                raise BackendError("sandbox_handle is required")
            if (
                len(self.leases) + len(self.retired) + len(self.registry.abandoned)
                >= self.config.max_sandboxes
            ):
                raise BackendError("Sandbox limit reached; release an existing handle")
            if self.config.transport == "http":
                computer_id = computer_id or random_computer_id()
                if temporary and any(
                    item.session._default_computer_id == computer_id
                    for item in self.leases.values()
                ):
                    raise BackendError(
                        "Temporary computer already has an active sandbox_handle"
                    )
            handle = secrets.token_urlsafe(32)
            lease = SandboxLease(
                handle,
                SessionContext(
                    self.backend, self.config.transport, registry=self.registry
                ),
            )
            if self.config.transport == "http":
                lease.session._default_computer_id = computer_id
            self.leases[handle] = lease
            if self.config.transport == "stdio":
                self.default_handle = handle
            fresh = True
        if lease.pending >= 8:
            raise BackendError("Sandbox request queue is full")
        task = asyncio.current_task()
        assert task is not None
        lease.pending += 1
        self.pending += 1
        lease.tasks.add(task)
        try:
            async with lease.lock:
                if lease.handle not in self.leases:
                    raise BackendError("Unknown or expired sandbox_handle")
                # A HTTP capability addresses exactly one computer. Permanent names
                # may be deliberately attached by the same administrator.
                selected = lease.session.current_computer_id
                if (
                    self.config.transport == "http"
                    and selected
                    and computer_id
                    and computer_id != selected
                ):
                    raise BackendError("computer_id does not match sandbox_handle")
                if (
                    fresh
                    and temporary
                    and computer_id
                    and self.registry.peek(computer_id)
                ):
                    raise BackendError(
                        "Temporary computer already has an active sandbox_handle"
                    )
                yield lease
        except BaseException:
            if fresh and lease.session.sandbox is None:
                self.leases.pop(handle, None)
                if self.default_handle == handle:
                    self.default_handle = None
                await self._close(lease)
            raise
        finally:
            lease.pending -= 1
            self.pending -= 1
            lease.tasks.discard(task)
            lease.last_used = time.monotonic()
