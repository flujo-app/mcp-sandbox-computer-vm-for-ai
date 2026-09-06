"""Real TCP MCP calls and application lifecycle boundaries; no provider credentials."""

import asyncio
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx2
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from sse_starlette.sse import AppStatus

from kilntainers.backends.base import ExecResult
from kilntainers.backends.test_utils import MockBackend, MockSandbox
from kilntainers.config import BackendConfig, ServerConfig
from kilntainers.errors import BackendError
from kilntainers.leases import SandboxLeases
from kilntainers.server import _create_handler, create_http_app, create_server

TOKEN = "test-owner-" * 4


class MemorySandbox(MockSandbox):
    def __init__(self):
        super().__init__()
        self.data = ""
        self.executing = asyncio.Event()

    async def exec(self, request):
        if self._stopped:
            raise BackendError("Sandbox stopped")
        if request.command == "wait":
            self.executing.set()
            await asyncio.Future()
        elif request.command.startswith("write "):
            self.data = request.command[6:]
        return ExecResult(stdout=self.data, stderr="", exit_code=0, exec_duration_ms=1)


class MemoryBackend(MockBackend):
    def __init__(self):
        super().__init__(BackendConfig())
        self.created = []

    async def _create_sandbox(self, **kwargs):
        if self.fail_next_create:
            self.fail_next_create = False
            raise BackendError("mock creation failure")
        sandbox = MemorySandbox()
        self.created.append(sandbox)
        return sandbox


@asynccontextmanager
async def serve(backend, **kwargs):
    # Each fixture represents a fresh process. sse-starlette's Uvicorn watcher
    # records previous server shutdown in this public process-global flag.
    # Sharing that flag across independent fixture servers can truncate initialize.
    AppStatus.should_exit = False
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = ServerConfig(
        transport="http",
        port=port,
        auth_token=TOKEN,
        enable_lifecycle_tools=True,
        **kwargs,
    )
    app = create_http_app(create_server(backend, config), config)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 35)
        sock.close()


@asynccontextmanager
async def connect(url, mode):
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http:
        transport = streamable_http_client(url + "/mcp", http_client=http)
        # Allow CI scheduling/Windows startup latency; cancellation cleanup below
        # still has its own strict five-second assertion.
        async with Client(transport, mode=mode, read_timeout_seconds=15) as client:
            yield client


async def call(client, tool, **arguments):
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_real_http_explicit_handles_survive_disconnect_and_expire_independently(
    mode,
):
    backend = MemoryBackend()
    async with serve(backend, session_timeout=2) as url:
        async with connect(url, mode) as client:
            assert client.protocol_version == (
                "2026-07-28" if mode == "auto" else "2025-11-25"
            )
            names = {t.name for t in (await client.list_tools()).tools}
            assert {
                "terminal_execute",
                "computer_release",
                "computer_factory_reset",
            } <= names
            assert backend.created == []  # discovery stays lazy
            first, second = await asyncio.gather(
                call(client, "terminal_execute", command="write first"),
                call(client, "terminal_execute", command="write second"),
            )
            assert first["sandbox_handle"] != second["sandbox_handle"]
            assert first["computer_id"] != second["computer_id"]
            a, b = await asyncio.gather(
                call(
                    client,
                    "terminal_execute",
                    command="read",
                    sandbox_handle=first["sandbox_handle"],
                ),
                call(
                    client,
                    "terminal_execute",
                    command="read",
                    sandbox_handle=second["sandbox_handle"],
                ),
            )
            assert a["stdout"] == "first" and b["stdout"] == "second"
            wrong = await client.call_tool(
                "terminal_execute",
                {
                    "command": "read",
                    "sandbox_handle": first["sandbox_handle"],
                    "computer_id": second["computer_id"],
                },
            )
            assert wrong.is_error
            inventory = await call(client, "computer_list")
            assert first["sandbox_handle"] not in str(inventory)
        # Both protocol eras use explicit application lifetime, not transport teardown.
        assert not backend.created[0].is_stopped()
        async with connect(url, mode) as client:
            assert (
                await call(
                    client,
                    "terminal_execute",
                    command="read",
                    sandbox_handle=first["sandbox_handle"],
                )
            )["stdout"] == "first"
            await call(
                client, "computer_release", sandbox_handle=first["sandbox_handle"]
            )
            assert backend.created[0].is_stopped()
            invalid = await client.call_tool(
                "terminal_execute",
                {"command": "read", "sandbox_handle": first["sandbox_handle"]},
            )
            assert invalid.is_error
            async with asyncio.timeout(5):
                while not backend.created[1].is_stopped():
                    await asyncio.sleep(0.1)
            expired = await client.call_tool(
                "terminal_execute",
                {"command": "read", "sandbox_handle": second["sandbox_handle"]},
            )
            assert expired.is_error


async def test_capability_quota_failed_creation_cancellation_and_retryable_cleanup():
    backend = MemoryBackend()
    config = ServerConfig(transport="http", max_sandboxes=2, session_timeout=10)
    async with SandboxLeases(backend, config) as state:
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state))
        handler = _create_handler(config)
        backend.fail_next_create = True
        failed = await handler(command="read", ctx=ctx)
        assert failed.is_error and not state.leases
        first = (await handler(command="write private", ctx=ctx)).structured_content
        handle = first["sandbox_handle"]
        lease = state.leases[handle]

        async def work():
            return await handler(command="wait", sandbox_handle=handle, ctx=ctx)

        task = asyncio.create_task(work())
        await backend.created[0].executing.wait()
        await state.expire_idle(now=lease.last_used + 100)
        assert handle in state.leases  # idle expiry cannot terminate active execution
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.created[0].is_stopped()
        await handler(command="read", ctx=ctx)
        full = await handler(command="read", ctx=ctx)
        assert full.is_error and "limit" in full.structured_content["error"].lower()
        await state.release(handle)
        assert len(state.leases) == 1
    assert all(sandbox.is_stopped() for sandbox in backend.created)


async def test_fixed_http_auth_host_origin_and_body_boundaries():
    async with serve(MemoryBackend()) as url:
        async with httpx2.AsyncClient() as http:
            for path in ["/", "/healthz", "/mcp", "/mcp/", "/unknown"]:
                assert (await http.get(url + path)).status_code == 401
            headers = {"Authorization": f"Bearer {TOKEN}"}
            assert (
                await http.get(url + "/healthz", headers=headers)
            ).status_code == 200
            for malicious in [
                {"Origin": "null"},
                {"Origin": "https://attacker.invalid"},
                {"Host": "attacker.invalid"},
                {"Origin": url + "/path"},
            ]:
                response = await http.get(
                    url + "/healthz", headers={**headers, **malicious}
                )
                assert response.status_code == 403
                assert "access-control-allow-origin" not in response.headers
            response = await http.post(
                url + "/mcp",
                headers={
                    **headers,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                content=b"x" * (4 * 1024 * 1024 + 1),
            )
            assert response.status_code == 413


async def test_http_named_temporary_creation_race_cannot_share_context():
    backend = MemoryBackend()
    config = ServerConfig(transport="http")
    async with SandboxLeases(backend, config) as state:
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state))
        handler = _create_handler(config)
        a, b = await asyncio.gather(
            handler(command="write first", computer_id="shared-test", ctx=ctx),
            handler(command="write second", computer_id="shared-test", ctx=ctx),
        )
        assert sorted([a.is_error, b.is_error]) == [False, True]
        assert len(backend.created) == 1


async def test_cleanup_failure_is_retained_and_retried():
    backend = MemoryBackend()
    async with SandboxLeases(backend, ServerConfig(transport="http")) as state:
        async with state.use(create=True) as lease:
            sandbox = await lease.session.get_or_create_sandbox()
        original = sandbox.stop
        count = 0

        async def flaky():
            nonlocal count
            count += 1
            if count == 1:
                raise BackendError("simulated cleanup failure")
            await original()

        sandbox.stop = flaky
        await state.release(lease.handle)
        assert state.retired and not sandbox.is_stopped()
        await state.expire_idle()
        assert not state.retired and sandbox.is_stopped()


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_actual_http_client_cancellation_stops_provider_work(mode):
    backend = MemoryBackend()
    async with serve(backend) as url:
        async with connect(url, mode) as client:
            created = await call(client, "terminal_execute", command="read")
            task = asyncio.create_task(
                client.call_tool(
                    "terminal_execute",
                    {"command": "wait", "sandbox_handle": created["sandbox_handle"]},
                )
            )
            await backend.created[0].executing.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with asyncio.timeout(5):
                while not backend.created[0].is_stopped():
                    await asyncio.sleep(0.02)


async def test_provider_error_details_are_not_returned_or_logged(caplog, monkeypatch):
    backend = MemoryBackend()

    async def fail(**kwargs):
        raise BackendError("Authorization: Bearer provider-private-secret")

    monkeypatch.setattr(backend, "_create_sandbox", fail)
    async with serve(backend) as url:
        async with connect(url, "auto") as client:
            result = await client.call_tool("terminal_execute", {"command": "read"})
            assert result.is_error
            assert "provider-private-secret" not in str(result)
            assert "provider-private-secret" not in caplog.text


async def test_dashboard_requires_explicit_capability_handoff_for_existing_temporary():
    async with serve(MemoryBackend()) as url:
        async with connect(url, "auto") as client:
            created = await call(client, "terminal_execute", command="read")
            payload = await call(
                client, "computer_dashboard", sandbox_handle=created["sandbox_handle"]
            )
            assert payload["sandbox_handle"] == created["sandbox_handle"]
            assert payload["active_computer_id"] == created["computer_id"]
            assert "sandbox_handle" not in await call(client, "computer_list")


async def test_shutdown_reports_unresolved_provider_cleanup():
    backend = MemoryBackend()
    with pytest.raises(BackendError, match="shutdown cleanup"):
        async with SandboxLeases(backend, ServerConfig(transport="http")) as state:
            async with state.use(create=True) as lease:
                sandbox = await lease.session.get_or_create_sandbox()

            async def fail_cleanup():
                raise BackendError("provider offline")

            sandbox.stop = fail_cleanup


async def test_cancelled_creation_rolls_back_the_resource_returned_later(monkeypatch):
    backend = MemoryBackend()
    started, finish = asyncio.Event(), asyncio.Event()
    original = backend._create_sandbox

    async def delayed(**kwargs):
        started.set()
        await finish.wait()
        return await original(**kwargs)

    monkeypatch.setattr(backend, "_create_sandbox", delayed)
    config = ServerConfig(transport="http")
    async with SandboxLeases(backend, config) as state:
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state))
        task = asyncio.create_task(_create_handler(config)(command="read", ctx=ctx))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state.registry.abandoned
        finish.set()
        assert await state.registry.cleanup_pending(wait=True)
        assert not state.registry.abandoned
        assert len(backend.created) == 1 and backend.created[0].is_stopped()


async def test_fresh_http_fixture_does_not_inherit_previous_server_shutdown():
    async with serve(MemoryBackend()) as url:
        async with connect(url, "legacy") as client:
            assert await call(client, "computer_list") is not None
    # The SSE watcher polls shutdown asynchronously, so make the old server's
    # final state deterministic before creating the next independent fixture.
    AppStatus.should_exit = True
    async with serve(MemoryBackend()) as url:
        async with connect(url, "legacy") as client:
            assert await call(client, "computer_list") is not None


async def test_monitor_cancellation_is_not_unexpected_provider_death(monkeypatch):
    from kilntainers.server import SessionContext

    backend = MemoryBackend()
    deaths = []
    session = SessionContext(
        backend, "stdio", death_callback=lambda: deaths.append(True)
    )
    sandbox = await session.get_or_create_sandbox()
    started = asyncio.Event()

    async def swallow_cancel():
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            return  # WASM's no-process watcher has this contract.

    assert isinstance(sandbox, MemorySandbox)
    monkeypatch.setattr(sandbox, "wait_for_death", swallow_cancel)
    async with asyncio.timeout(5):
        await started.wait()
        await session.cleanup()
    assert sandbox.is_stopped()
    assert deaths == [], "Normal cleanup must not emit another termination signal"
