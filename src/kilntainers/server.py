"""MCP server implementation."""

import asyncio
import json
import os
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Annotated, Any, AsyncContextManager

import anyio
from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from pydantic import Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from kilntainers.backends.base import Backend, ExecRequest, Sandbox
from kilntainers.computers import ComputerRegistry, random_computer_id
from kilntainers.config import ServerConfig
from kilntainers.dashboard import (
    DASHBOARD_MIME_TYPE,
    DASHBOARD_RESOURCE_META,
    DASHBOARD_URI,
    dashboard_html,
)
from kilntainers.errors import BackendError, SandboxDiedError
from kilntainers.leases import SandboxLeases

# Constants
STDIN_LIMIT = 2 * 1024 * 1024  # 2 MiB (D32)


# --- Session Context ---


class SessionContext:
    """Per-session state, available to tool handlers via Context.

    Supports lazy sandbox creation — the sandbox is only created on
    the first call to get_or_create_sandbox(). This allows the MCP
    server to respond to non-exec requests (tools/list, etc.) without
    waiting for container startup.
    """

    def __init__(
        self,
        backend: Backend,
        transport: str,
        death_callback: Callable[[], None] | None = None,
        registry: ComputerRegistry | None = None,
    ) -> None:
        """Initialize the session context.

        Args:
            backend: The backend to use for sandbox creation.
            transport: The transport mode ("stdio" or "http").
            death_callback: Optional callback for sandbox death in stdio mode.
        """
        self._backend = backend
        self._registry = registry or ComputerRegistry(backend)
        self._transport = transport
        self._death_callback = death_callback
        self._default_computer_id: str | None = None
        self._current_computer_id: str | None = None
        self._owned_computers: dict[str, bool] = {}
        self._death_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    @property
    def sandbox(self) -> Sandbox | None:
        """The most recently selected sandbox, or None before first use."""
        if self._current_computer_id is None:
            return None
        return self._registry.peek(self._current_computer_id)

    @property
    def death_task(self) -> asyncio.Task[None] | None:
        """The current computer's death monitor, if one has been created."""
        if self._current_computer_id is None:
            return None
        return self._death_tasks.get(self._current_computer_id)

    @property
    def current_computer_id(self) -> str | None:
        """Stable ID of the computer most recently used by this session."""
        return self._current_computer_id

    @property
    def registry(self) -> ComputerRegistry:
        """Shared computer registry used by management tool handlers."""
        return self._registry

    async def get_or_create_sandbox(
        self,
        computer_id: str | None = None,
        *,
        temporary: bool = True,
    ) -> Sandbox:
        """Get the sandbox, creating it lazily on first call.

        Concurrency-safe: uses asyncio.Lock to ensure only one sandbox
        is created even if multiple calls arrive simultaneously.

        Returns:
            The sandbox instance.

        Raises:
            BackendError: If sandbox creation fails. The next call
                will retry creation.
        """
        async with self._lock:
            target_id = computer_id
            if target_id is None and self._default_computer_id is not None:
                target_id = self._default_computer_id
                temporary = self._owned_computers.get(target_id, temporary)

            if target_id is not None and target_id in self._owned_computers:
                existing = await self._registry.get_owned(target_id)
                if existing is not None:
                    if self._owned_computers[target_id] != temporary:
                        raise BackendError(
                            f"Computer '{target_id}' is already attached with "
                            f"temporary={str(self._owned_computers[target_id]).lower()}."
                        )
                    self._current_computer_id = target_id
                    return existing

            assigned_id, sandbox = await self._registry.acquire(
                target_id,
                temporary=temporary,
                add_owner=True,
            )
            self._owned_computers[assigned_id] = sandbox.temporary
            self._current_computer_id = assigned_id
            if self._default_computer_id is None:
                self._default_computer_id = assigned_id
            self._start_death_monitor(assigned_id, sandbox)
            return sandbox

    def _start_death_monitor(self, computer_id: str, sandbox: Sandbox) -> None:
        """Start monitoring sandbox for unexpected death."""

        async def _monitor_death() -> None:
            try:
                await sandbox.wait_for_death()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Unexpected error monitoring sandbox — treat as death
                pass

            # Sandbox died (or monitoring failed)
            if self._transport == "stdio":
                if self._death_callback is not None:
                    self._death_callback()
                else:
                    os.kill(os.getpid(), signal.SIGTERM)

        self._death_tasks[computer_id] = asyncio.create_task(_monitor_death())

    async def _cancel_death_monitor(self, computer_id: str) -> None:
        task = self._death_tasks.pop(computer_id, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def restart_computer(self, computer_id: str) -> Sandbox:
        """Restart through the registry and refresh any session death monitor."""
        await self._cancel_death_monitor(computer_id)
        sandbox = await self._registry.restart(computer_id)
        if computer_id in self._owned_computers:
            self._start_death_monitor(computer_id, sandbox)
        self._current_computer_id = computer_id
        return sandbox

    async def factory_reset_computer(self, computer_id: str) -> Sandbox:
        """Factory-reset through the registry and refresh monitoring."""
        await self._cancel_death_monitor(computer_id)
        sandbox = await self._registry.factory_reset(computer_id)
        if computer_id in self._owned_computers:
            self._start_death_monitor(computer_id, sandbox)
        self._current_computer_id = computer_id
        return sandbox

    async def delete_computer(self, computer_id: str) -> None:
        """Delete through the registry and detach it from this session."""
        await self._cancel_death_monitor(computer_id)
        await self._registry.delete(computer_id)
        self._owned_computers.pop(computer_id, None)
        if self._current_computer_id == computer_id:
            self._current_computer_id = None
        if self._default_computer_id == computer_id:
            self._default_computer_id = None

    async def cleanup(self) -> None:
        """Clean up resources. Called by lifespan on exit.

        Safe to call even if no sandbox was ever created (no-op).
        """
        for task in self._death_tasks.values():
            task.cancel()
        for task in self._death_tasks.values():
            try:
                await task
            except asyncio.CancelledError:
                pass
        for computer_id in list(self._owned_computers):
            await self._registry.release(computer_id)
            self._owned_computers.pop(computer_id, None)


def _result(
    payload: dict[str, Any],
    *,
    is_error: bool = False,
) -> CallToolResult:
    """Create an MCP result with JSON fallback and structured app data."""
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        is_error=is_error,
        structured_content=payload,
    )


def _session_from_context(
    ctx: Context[Any, Any] | None,
) -> SessionContext | None:
    if ctx is None:
        return None
    return ctx.request_context.lifespan_context


# --- Tool Description Assembly ---


def assemble_tool_description(
    backend: Backend,
    override: str | None,
    extended: str | None,
) -> str:
    """Assemble the terminal_execute tool description.

    Raises BackendError if the result would be empty.

    Args:
        backend: The backend instance to query for tool instructions.
        override: User-provided description that replaces everything.
        extended: User-provided text to append to backend instructions.

    Returns:
        The assembled tool description text.

    Raises:
        BackendError: If both override and extended are provided, or if
            the result would be empty.
    """
    # Rule 4: Both override and extended is an error
    if override is not None and extended is not None:
        raise BackendError(
            "Cannot use both --tool-instruction-override and "
            "--extended-tool-instruction. Use override to replace "
            "the description entirely, or extended to append to "
            "the backend default."
        )

    # Rule 1: Override replaces everything
    if override is not None:
        return override

    # Rule 2: Backend instructions, optionally extended
    backend_instructions = backend.tool_instructions()

    if not backend_instructions:
        # Rule 3: No backend instructions and no override
        raise BackendError(
            "Backend does not provide tool instructions describing "
            "the sandbox. Supply --tool-instruction-override to "
            "describe the capabilities of this sandbox (example "
            "'a Debian Linux bash shell' or 'A minimal BusyBox "
            "shell with the following commands: ...')."
        )

    if extended is not None:
        return f"{backend_instructions}\n\n{extended}"

    return backend_instructions


# --- Lifespan Factory ---


def create_lifespan(
    backend: Backend,
    transport: str,
    *,
    death_callback: Callable[[], None] | None = None,
    registry: ComputerRegistry | None = None,
) -> Callable[[MCPServer], AsyncContextManager[SessionContext]]:
    """Create a lifespan context manager for the given transport.

    The returned context manager creates a SessionContext that supports
    lazy sandbox creation. The sandbox is not created until the first
    terminal_execute call.

    Args:
        backend: The backend to use for creating sandboxes.
        transport: The transport mode ("stdio" or "http").
        death_callback: Optional callback for sandbox death in stdio mode.
            If None, sends SIGTERM to current process. For testing, pass
            a custom callback to capture death notifications.

    Returns:
        An async context manager function compatible with MCPServer.
    """

    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[SessionContext]:
        """Create a SessionContext for this session and clean up on exit."""
        ctx = SessionContext(
            backend=backend,
            transport=transport,
            death_callback=death_callback,
            registry=registry,
        )
        try:
            yield ctx
        finally:
            await ctx.cleanup()

    return lifespan


# --- Input Validation ---


def _validate_inputs(
    command: str | None,
    args: list[str] | None,
    stdin: str | None,
    working_directory: str | None,
    timeout: int | None,
) -> str | None:
    """Validate tool inputs.

    Returns error message or None if valid.

    Args:
        command: The shell command string, if using command mode.
        args: The list of arguments, if using args mode.
        stdin: The stdin content to pipe to the command.
        working_directory: The working directory for the command.
        timeout: The timeout in seconds.

    Returns:
        An error message string if validation fails, None otherwise.
    """
    # Exactly one of command or args
    if command is not None and args is not None:
        return "Cannot provide both 'command' and 'args'. Use 'command' for shell commands or 'args' for direct execution."
    if command is None and args is None:
        return "Must provide either 'command' or 'args'."

    # working_directory must be absolute
    if working_directory is not None and not working_directory.startswith("/"):
        return f"working_directory must be an absolute path, got: {working_directory}"

    # timeout must be positive
    if timeout is not None and timeout < 1:
        return "timeout must be at least 1 second."

    # stdin size limit (D32)
    if stdin is not None and len(stdin.encode("utf-8")) > STDIN_LIMIT:
        return (
            f"stdin content exceeds the 2 MiB limit "
            f"({len(stdin.encode('utf-8'))} bytes). "
            f"Split into smaller chunks or use a different approach."
        )

    return None


# --- Tool Handler ---


def _state(ctx: Context[Any, Any] | None) -> SandboxLeases | SessionContext:
    if ctx is None:
        raise BackendError("Internal error: no context provided")
    return ctx.request_context.lifespan_context


@asynccontextmanager
async def _use(
    ctx, sandbox_handle=None, *, create=False, computer_id=None, temporary=True
):
    state = _state(ctx)
    if isinstance(state, SessionContext):
        yield state, None
        return
    async with state.use(
        sandbox_handle, create=create, computer_id=computer_id, temporary=temporary
    ) as lease:
        yield lease.session, lease.handle


async def _stop_cancelled_sandbox(sandbox):
    # AnyIO cancellation can repeat at each await. Keep the actual provider stop
    # awaited instead of detaching it when the HTTP connection disappears.
    with anyio.CancelScope(shield=True):
        await asyncio.wait_for(sandbox.stop(), 20)


def _create_handler(config: ServerConfig):
    async def handler(
        command=None,
        args=None,
        stdin=None,
        working_directory=None,
        timeout=None,
        computer_id=None,
        temporary=True,
        ctx=None,
        sandbox_handle=None,
    ):
        command = command or None
        args = args or None
        stdin = stdin or None
        working_directory = working_directory or None
        error = _validate_inputs(command, args, stdin, working_directory, timeout)
        if error:
            return _result({"error": error}, is_error=True)
        if timeout is not None and timeout > 3600:
            return _result(
                {"error": "timeout must not exceed 3600 seconds"}, is_error=True
            )
        if len((command or "").encode()) > 65536 or (
            args and (len(args) > 256 or sum(len(arg.encode()) for arg in args) > 65536)
        ):
            return _result(
                {"error": "command/args exceed the 64 KiB or 256 argument limit"},
                is_error=True,
            )
        try:
            async with _use(
                ctx,
                sandbox_handle,
                create=True,
                computer_id=computer_id,
                temporary=temporary,
            ) as (session, handle):
                async with asyncio.timeout(120):
                    try:
                        sandbox = await session.get_or_create_sandbox(
                            computer_id, temporary=temporary
                        )
                    except BackendError:
                        raise BackendError(
                            "Sandbox could not be created or attached"
                        ) from None
                selected_id = sandbox.computer_id
                request = ExecRequest(
                    command=command,
                    args=args,
                    stdin=stdin,
                    working_directory=working_directory,
                    timeout=timeout if timeout is not None else config.default_timeout,
                    output_limit=config.output_limit,
                )
                try:
                    async with asyncio.timeout(request.timeout + 15):
                        try:
                            result = await sandbox.exec(request)
                        except BackendError:
                            raise BackendError("Sandbox execution failed") from None
                except (asyncio.CancelledError, TimeoutError):
                    # Killing a client-side request does not cancel provider-side work.
                    # Stop the affected computer. Permanent Docker/Fly writable state remains.
                    await _stop_cancelled_sandbox(sandbox)
                    raise
                payload = {
                    "computer_id": selected_id,
                    "temporary": sandbox.temporary,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "exit_code": result.exit_code,
                    "exec_duration_ms": result.exec_duration_ms,
                }
                if handle:
                    payload["sandbox_handle"] = handle
                return _result(payload)
        except SandboxDiedError:
            return _result(
                {"error": "Sandbox died; create or restart its computer"}, is_error=True
            )
        except BackendError as error:
            return _result({"error": str(error)}, is_error=True)
        except TimeoutError:
            return _result(
                {"error": "Sandbox operation exceeded its deadline"}, is_error=True
            )
        except Exception:
            # Provider exceptions may embed authorization headers, URLs, or command data.
            return _result(
                {"error": "Sandbox provider operation failed"}, is_error=True
            )

    return handler


def _computer_ui_meta(*, launcher=False):
    ui = {"visibility": ["model", "app"]}
    if launcher:
        ui["resourceUri"] = DASHBOARD_URI
    return {"ui": ui}


def _register_computer_tools(mcp, config):
    async def inventory(ctx):
        state = _state(ctx)
        registry = state.registry
        async with asyncio.timeout(30):
            computers = await registry.list()
        return {
            "computers": [computer.to_dict() for computer in computers],
            "count": len(computers),
        }

    async def computer_dashboard(
        sandbox_handle: str | None = None,
        ctx: Context[Any, Any] | None = None,
    ) -> CallToolResult:
        """Open the sandbox computer dashboard for this authenticated administrative owner."""
        try:
            payload = await inventory(ctx)
            if sandbox_handle:
                async with _use(ctx, sandbox_handle) as (session, handle):
                    payload.update(
                        sandbox_handle=handle,
                        active_computer_id=session.current_computer_id,
                    )
            return _result(payload)
        except Exception:
            return _result({"error": "Computer inventory unavailable"}, is_error=True)

    async def computer_list(ctx: Context[Any, Any] | None = None) -> CallToolResult:
        """List this owner's computers. Capability handles are never included in inventory."""
        return await computer_dashboard(ctx=ctx)

    async def computer_create(
        computer_id: str | None = None,
        temporary: bool = True,
        ctx: Context[Any, Any] | None = None,
    ) -> CallToolResult:
        """Create a computer, or deliberately attach a permanent named computer.
        Save sandbox_handle and pass it to subsequent commands and lifecycle operations."""
        try:
            async with _use(
                ctx, create=True, computer_id=computer_id, temporary=temporary
            ) as (session, handle):
                async with asyncio.timeout(120):
                    sandbox = await session.get_or_create_sandbox(
                        computer_id
                        or session._default_computer_id
                        or random_computer_id(),
                        temporary=temporary,
                    )
                return _result(
                    {
                        "ok": True,
                        "computer_id": sandbox.computer_id,
                        "sandbox_id": sandbox.sandbox_id,
                        "temporary": sandbox.temporary,
                        "sandbox_handle": handle,
                    }
                )
        except BackendError as error:
            return _result({"error": str(error)}, is_error=True)
        except Exception:
            return _result({"error": "Computer creation failed"}, is_error=True)

    async def lifecycle(ctx, computer_id, handle, action):
        try:
            async with _use(ctx, handle) as (session, actual_handle):
                if computer_id not in session._owned_computers:
                    raise BackendError("Computer is not owned by this sandbox handle")
                async with asyncio.timeout(60):
                    if action == "delete":
                        await session.delete_computer(computer_id)
                        return _result(
                            {"ok": True, "computer_id": computer_id, "deleted": True}
                        )
                    method = (
                        session.restart_computer
                        if action == "restart"
                        else session.factory_reset_computer
                    )
                    sandbox = await method(computer_id)
                return _result(
                    {
                        "ok": True,
                        "computer_id": computer_id,
                        "sandbox_id": sandbox.sandbox_id,
                        "temporary": sandbox.temporary,
                        "sandbox_handle": actual_handle,
                    }
                )
        except BackendError as error:
            return _result({"error": str(error)}, is_error=True)
        except Exception:
            return _result(
                {"error": "Computer lifecycle operation failed"}, is_error=True
            )

    async def computer_restart(
        computer_id: str,
        sandbox_handle: str | None = None,
        ctx: Context[Any, Any] | None = None,
    ) -> CallToolResult:
        """Restart the handle's computer while preserving its writable filesystem."""
        return await lifecycle(ctx, computer_id, sandbox_handle, "restart")

    async def computer_factory_reset(
        computer_id: str,
        sandbox_handle: str | None = None,
        ctx: Context[Any, Any] | None = None,
    ) -> CallToolResult:
        """Permanently erase the handle's computer state and recreate it from its image."""
        return await lifecycle(ctx, computer_id, sandbox_handle, "reset")

    async def computer_delete(
        computer_id: str,
        sandbox_handle: str | None = None,
        ctx: Context[Any, Any] | None = None,
    ) -> CallToolResult:
        """Permanently delete the handle's computer and its writable state."""
        return await lifecycle(ctx, computer_id, sandbox_handle, "delete")

    for fn in [
        computer_dashboard,
        computer_list,
        computer_create,
        computer_restart,
        computer_factory_reset,
        computer_delete,
    ]:
        mcp.add_tool(
            fn,
            name=fn.__name__,
            meta=_computer_ui_meta(launcher=fn is computer_dashboard),
        )

    @mcp.resource(
        DASHBOARD_URI,
        name="Sandbox Computer Dashboard",
        title="Sandbox Computer Dashboard",
        mime_type=DASHBOARD_MIME_TYPE,
        meta=DASHBOARD_RESOURCE_META,
    )
    def computer_dashboard_resource() -> str:
        return dashboard_html()


def create_http_app(mcp: MCPServer, config: ServerConfig) -> Starlette:
    from kilntainers.auth import BearerTokenMiddleware, http_allowlists

    hosts, origins = http_allowlists(config)
    app = mcp.streamable_http_app(
        host=config.host,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        ),
        max_request_body_size=4 * 1024 * 1024,
    )

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def service_info(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "name": "mcp-sandbox-computer-vm-for-ai",
                "mcp_endpoint": "/mcp",
                "health": "/healthz",
            }
        )

    app.router.routes.extend([Route("/healthz", healthz), Route("/", service_info)])
    app.add_middleware(
        BearerTokenMiddleware,  # ty: ignore[invalid-argument-type]
        token=config.auth_token,
        allowed_hosts=hosts,
        allowed_origins=origins,
        allow_unauthenticated=config.allow_unauthenticated_http,
    )
    return app


class SandboxServer(MCPServer[SandboxLeases]):
    cleanup_failed: bool = False


def create_server(backend: Backend, config: ServerConfig) -> SandboxServer:
    description = assemble_tool_description(
        backend,
        override=config.tool_instruction_override,
        extended=config.extended_tool_instruction,
    )

    @asynccontextmanager
    async def lifespan(server):
        try:
            async with SandboxLeases(backend, config) as state:
                yield state
        except BackendError:
            server.cleanup_failed = True
            raise

    mcp = SandboxServer(
        name="Kilntainers",
        version=version("mcp-sandbox-computer-vm-for-ai"),
        lifespan=lifespan,
        extensions=[Apps()] if config.enable_lifecycle_tools else [],
        log_level="WARNING",
    )
    handler = _create_handler(config)

    async def terminal_execute(
        command: Annotated[
            str | None, Field(description="Shell command, exclusive with args")
        ] = None,
        args: Annotated[
            list[str] | None,
            Field(description="Direct execution argv, exclusive with command"),
        ] = None,
        stdin: str | None = None,
        working_directory: str | None = None,
        timeout: Annotated[int | None, Field(ge=1, le=3600)] = None,
        computer_id: str | None = None,
        temporary: bool = True,
        sandbox_handle: Annotated[
            str | None,
            Field(
                description="Opaque handle returned by a prior call. Required to reuse HTTP sandbox state; "
                "omitting it creates an independent sandbox. Stdio retains its process default."
            ),
        ] = None,
        ctx: Context[Any, Any] | None = None,
    ) -> CallToolResult:
        return await handler(
            command,
            args,
            stdin,
            working_directory,
            timeout,
            computer_id,
            temporary,
            ctx,
            sandbox_handle,
        )

    async def computer_release(
        sandbox_handle: str | None = None, ctx: Context[Any, Any] | None = None
    ) -> CallToolResult:
        """Release a handle immediately. Temporary computers are removed; permanent state remains."""
        try:
            state = _state(ctx)
            if isinstance(state, SessionContext):
                await state.cleanup()
            else:
                complete = await state.release(sandbox_handle)
                if not complete:
                    return _result(
                        {
                            "released": True,
                            "cleanup_complete": False,
                            "error": "Cleanup is pending; retry in background or use provider console",
                        },
                        is_error=True,
                    )
            return _result({"released": True, "cleanup_complete": True})
        except BackendError as error:
            return _result({"error": str(error)}, is_error=True)

    mcp.add_tool(
        terminal_execute,
        name="terminal_execute",
        description=description
        + "\n\nHTTP: save sandbox_handle from the first response and pass it on every subsequent "
        "call to reuse state. Computer IDs are readable names, not authorization capabilities.",
        meta=_computer_ui_meta(),
    )
    mcp.add_tool(computer_release, name="computer_release", meta=_computer_ui_meta())
    if config.enable_lifecycle_tools:
        _register_computer_tools(mcp, config)
    return mcp
