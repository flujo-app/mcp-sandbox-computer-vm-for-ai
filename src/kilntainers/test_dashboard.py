"""MCP Apps dashboard registration and static auth tests."""

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from kilntainers.auth import BearerTokenMiddleware
from kilntainers.backends.test_utils import MockBackend
from kilntainers.config import BackendConfig, ServerConfig
from kilntainers.dashboard import DASHBOARD_MIME_TYPE, DASHBOARD_URI
from kilntainers.server import create_server


async def test_dashboard_tool_resource_and_extension_are_registered() -> None:
    server = create_server(
        MockBackend(BackendConfig()), ServerConfig(enable_lifecycle_tools=True)
    )
    tools = {tool.name: tool for tool in await server.list_tools()}
    resources = await server.list_resources()

    assert set(tools) == {
        "terminal_execute",
        "computer_release",
        "computer_dashboard",
        "computer_list",
        "computer_create",
        "computer_restart",
        "computer_factory_reset",
        "computer_delete",
    }
    assert tools["computer_dashboard"].meta == {
        "ui": {
            "visibility": ["model", "app"],
            "resourceUri": DASHBOARD_URI,
        }
    }
    assert len(resources) == 1
    assert str(resources[0].uri) == DASHBOARD_URI
    assert resources[0].mime_type == DASHBOARD_MIME_TYPE


async def test_dashboard_html_is_self_contained_and_calls_management_tools() -> None:
    server = create_server(
        MockBackend(BackendConfig()), ServerConfig(enable_lifecycle_tools=True)
    )
    contents = await server.read_resource(DASHBOARD_URI)
    html = next(iter(contents)).content

    assert isinstance(html, str)
    assert "2026-01-26" in html
    assert 'callTool("computer_list"' in html
    assert 'callTool("terminal_execute"' in html
    assert "<script src=" not in html
    assert "<link rel=" not in html


async def test_lifecycle_tools_and_dashboard_are_disabled_by_default() -> None:
    server = create_server(MockBackend(BackendConfig()), ServerConfig())

    tools = {tool.name for tool in await server.list_tools()}
    resources = await server.list_resources()
    assert tools == {"terminal_execute", "computer_release"}
    assert resources == []


async def _ok(request):
    return JSONResponse({"ok": True})


def test_bearer_middleware_protects_all_routes() -> None:
    app = Starlette(
        routes=[
            Route("/", _ok),
            Route("/healthz", _ok),
            Route("/mcp", _ok, methods=["GET", "POST"]),
        ]
    )
    app.add_middleware(
        BearerTokenMiddleware,  # ty: ignore[invalid-argument-type]
        token="test-secret",
        allowed_hosts=["testserver"],
    )

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 401
        assert client.get("/").status_code == 401
        assert client.get("/mcp").status_code == 401
        assert (
            client.get("/mcp", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        assert (
            client.get(
                "/mcp", headers={"Authorization": "Bearer test-secret"}
            ).status_code
            == 200
        )
