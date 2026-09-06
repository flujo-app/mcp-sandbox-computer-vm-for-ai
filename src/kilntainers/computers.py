"""Named computer registry shared by MCP sessions and dashboard tools."""

import asyncio
import re
import secrets
from dataclasses import dataclass

from kilntainers.backends.base import Backend, ComputerInfo, Sandbox
from kilntainers.errors import BackendError

_COMPUTER_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_ADJECTIVES = (
    "amber",
    "brisk",
    "calm",
    "clever",
    "coral",
    "crisp",
    "gentle",
    "lucky",
    "quiet",
    "rapid",
    "silver",
    "steady",
)
_NOUNS = (
    "badger",
    "comet",
    "falcon",
    "gecko",
    "heron",
    "lynx",
    "otter",
    "panda",
    "raven",
    "tiger",
    "whale",
    "wolf",
)


def random_computer_id() -> str:
    """Generate a readable, collision-resistant provider-safe slug."""
    return (
        f"{secrets.choice(_ADJECTIVES)}-{secrets.choice(_NOUNS)}-{secrets.token_hex(2)}"
    )


def validate_computer_id(computer_id: str) -> str:
    """Validate and return a stable computer ID.

    IDs intentionally follow the common Docker/Fly lowercase slug subset, so
    the same value works across both providers and can safely become a resource
    name without provider-specific rewriting.
    """
    value = computer_id.strip()
    if not _COMPUTER_ID_RE.fullmatch(value):
        raise BackendError(
            "computer_id must be 1-63 lowercase letters, numbers, or hyphens; "
            "it must start and end with a letter or number."
        )
    return value


@dataclass(slots=True)
class _ComputerRecord:
    sandbox: Sandbox
    temporary: bool
    owners: int = 0


class ComputerRegistry:
    """Coordinate named sandboxes across MCP sessions in one server process.

    Docker and Fly backends additionally discover computers provider-side, so
    permanent records survive a server restart and can be reattached by ID.
    """

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self._records: dict[str, _ComputerRecord] = {}
        self._lock = asyncio.Lock()
        self._creating_ids: set[str] = set()
        self.abandoned: dict[str, asyncio.Task[Sandbox]] = {}

    @staticmethod
    def _tag(sandbox: Sandbox, computer_id: str, temporary: bool) -> Sandbox:
        """Apply registry metadata used by legacy Sandbox base properties."""
        setattr(sandbox, "_managed_computer_id", computer_id)
        setattr(sandbox, "_managed_temporary", temporary)
        return sandbox

    async def acquire(
        self,
        computer_id: str | None,
        *,
        temporary: bool,
        add_owner: bool,
    ) -> tuple[str, Sandbox]:
        """Attach to or create a computer and optionally add an owner ref."""
        requested_id = (
            random_computer_id()
            if computer_id is None
            else validate_computer_id(computer_id)
        )

        if (
            not temporary
            and type(self.backend).attach_sandbox is Backend.attach_sandbox
        ):
            raise BackendError(
                "Permanent computers require a backend with named reattachment (Docker or Fly)"
            )
        async with self._lock:
            record = self._records.get(requested_id)
            if record is not None:
                if record.temporary != temporary:
                    raise BackendError(
                        f"Computer '{requested_id}' already exists with "
                        f"temporary={str(record.temporary).lower()}; lifecycle "
                        "mode cannot be changed without a factory reset or delete."
                    )
                if add_owner:
                    record.owners += 1
                return requested_id, record.sandbox

            if requested_id in self._creating_ids:
                raise BackendError("Computer creation or rollback is still in progress")
            sandbox = await self.backend.attach_sandbox(requested_id)
            if sandbox is not None:
                actual_temporary = sandbox.temporary
                if actual_temporary != temporary:
                    raise BackendError(
                        f"Computer '{requested_id}' already exists with "
                        f"temporary={str(actual_temporary).lower()}; requested "
                        f"temporary={str(temporary).lower()}."
                    )
            else:
                if requested_id in self._creating_ids:
                    raise BackendError(
                        "Computer creation or rollback is still in progress"
                    )
                self._creating_ids.add(requested_id)
                creation = asyncio.create_task(
                    asyncio.wait_for(
                        self.backend.create_sandbox(
                            computer_id=requested_id, temporary=temporary
                        ),
                        timeout=150,
                    )
                )
                try:
                    sandbox = await asyncio.shield(creation)
                except asyncio.CancelledError:
                    # Provisioning can outlive a cancelled MCP request. Retain ownership
                    # until the returned resource can be removed, including a newly
                    # requested permanent resource which the caller never received.
                    self.abandoned[requested_id] = creation
                    raise
                finally:
                    if requested_id not in self.abandoned:
                        self._creating_ids.discard(requested_id)
                # Legacy backends keep their original Sandbox interface. These
                # attributes let the base properties expose registry semantics
                # without wrapping the instance or breaking backend-specific APIs.
                self._tag(sandbox, requested_id, temporary)
                actual_temporary = temporary

            self._records[requested_id] = _ComputerRecord(
                sandbox=sandbox,
                temporary=actual_temporary,
                owners=1 if add_owner else 0,
            )
            return requested_id, sandbox

    async def cleanup_pending(self, *, wait: bool = False) -> bool:
        """Rollback completed abandoned creations; bounded shutdown reports failures."""

        async def cleanup(computer_id, task):
            if not task.done() and not wait:
                return True
            try:
                sandbox = await asyncio.wait_for(asyncio.shield(task), timeout=25)
                async with asyncio.timeout(20):
                    if not await self.backend.delete_computer(computer_id):
                        await sandbox.stop()
            except Exception:
                return False
            self.abandoned.pop(computer_id, None)
            self._creating_ids.discard(computer_id)
            return True

        results = await asyncio.gather(
            *(
                cleanup(computer_id, task)
                for computer_id, task in list(self.abandoned.items())
            )
        )
        return all(results)

    async def get_owned(self, computer_id: str) -> Sandbox | None:
        """Return a locally attached sandbox without changing owner refs."""
        async with self._lock:
            record = self._records.get(computer_id)
            return record.sandbox if record is not None else None

    def peek(self, computer_id: str) -> Sandbox | None:
        """Return a local sandbox for synchronous status properties."""
        record = self._records.get(computer_id)
        return record.sandbox if record is not None else None

    async def release(self, computer_id: str) -> None:
        """Release one owner; retain a failed cleanup record for retry."""
        async with self._lock:
            record = self._records.get(computer_id)
            if record is None:
                return
            record.owners = max(0, record.owners - 1)
            if record.temporary and record.owners == 0:
                await record.sandbox.stop()
                del self._records[computer_id]

    async def list(self) -> list[ComputerInfo]:
        """Return a de-duplicated provider and in-process inventory."""
        provider_items = await self.backend.list_computers()
        by_id = {item.computer_id: item for item in provider_items}

        async with self._lock:
            for computer_id, record in self._records.items():
                by_id.setdefault(
                    computer_id,
                    ComputerInfo(
                        computer_id=computer_id,
                        sandbox_id=record.sandbox.sandbox_id,
                        backend=self.backend.__class__.__name__,
                        state="running",
                        temporary=record.temporary,
                    ),
                )
        return sorted(by_id.values(), key=lambda item: item.computer_id)

    async def restart(self, computer_id: str) -> Sandbox:
        """Restart a computer while preserving its filesystem."""
        computer_id = validate_computer_id(computer_id)
        async with self._lock:
            record = self._records.get(computer_id)
            replacement = await self.backend.restart_computer(computer_id)
            if replacement is None:
                raise BackendError(
                    "This backend cannot restart while preserving files; use factory reset explicitly"
                )
            if record is None:
                record = _ComputerRecord(
                    replacement,
                    replacement.temporary,
                    owners=0,
                )
                self._records[computer_id] = record
            else:
                record.sandbox = replacement
            return replacement

    async def factory_reset(self, computer_id: str) -> Sandbox:
        """Delete a computer's writable state and recreate its base image."""
        computer_id = validate_computer_id(computer_id)
        async with self._lock:
            record = self._records.get(computer_id)
            replacement = await self.backend.factory_reset_computer(computer_id)
            if replacement is None:
                if record is None:
                    raise BackendError(f"Computer '{computer_id}' was not found.")
                await record.sandbox.stop()
                replacement = await self.backend.create_sandbox(
                    computer_id=computer_id,
                    temporary=record.temporary,
                )
                self._tag(replacement, computer_id, record.temporary)
            if record is None:
                self._records[computer_id] = _ComputerRecord(
                    replacement,
                    replacement.temporary,
                    owners=0,
                )
            else:
                record.sandbox = replacement
            return replacement

    async def delete(self, computer_id: str) -> None:
        """Permanently delete a managed computer."""
        computer_id = validate_computer_id(computer_id)
        async with self._lock:
            record = self._records.get(computer_id)
            deleted = await self.backend.delete_computer(computer_id)
            if deleted:
                self._records.pop(computer_id, None)
                return
            if record is None:
                raise BackendError(f"Computer '{computer_id}' was not found.")
            await record.sandbox.stop()
            self._records.pop(computer_id, None)
