"""Exercise the installed wheel over both protocol eras; Docker fixtures are optional."""

import argparse
import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import httpx2
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextResourceContents

import kilntainers

RUN = uuid.uuid4().hex
LABEL = "kilntainers.artifact-run"
TOKEN = uuid.uuid4().hex + uuid.uuid4().hex
ROOT = Path(__file__).resolve().parents[1]


def docker(*args, check=True):
    result = subprocess.run(
        ["docker", *args], text=True, capture_output=True, timeout=30, check=check
    )
    return result


def owned_containers():
    ids = docker(
        "ps", "-aq", "--no-trunc", "--filter", f"label={LABEL}={RUN}"
    ).stdout.split()
    if not ids:
        return []
    if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in ids):
        raise RuntimeError("Unexpected fixture ID")
    data = json.loads(docker("inspect", *ids).stdout)
    for item in data:
        labels = item.get("Config", {}).get("Labels", {})
        if (
            item["Id"] not in ids
            or labels.get(LABEL) != RUN
            or labels.get("kilntainers") != "true"
        ):
            raise RuntimeError(
                "Refusing cleanup of a container without fixture ownership"
            )
    return data


def assert_absent(computer_id):
    assert not any(
        item["Config"]["Labels"].get("kilntainers.computer-id") == computer_id
        for item in owned_containers()
    ), "Temporary container remains after awaited cleanup"


async def call(client, name, arguments):
    result = await client.call_tool(name, arguments, read_timeout_seconds=90)
    assert not result.is_error, result.content
    assert isinstance(result.structured_content, dict)
    return result.structured_content


def cli_args(backend, *extra):
    args = ["-m", "kilntainers", "--backend", backend, "--timeout", "20"]
    if backend == "docker":
        args += ["--no-network", f"--docker-run-flag=--label={LABEL}={RUN}"]
    return [*args, *extra]


async def stdio(backend, directory):
    environment = {**os.environ, "ENABLE_LIFECYCLE_TOOLS": "true"}
    environment.pop("PYTHONPATH", None)
    for mode in ["auto", "legacy"]:
        name = f"artifact-{RUN[:12]}-{mode}"
        params = StdioServerParameters(
            command=sys.executable,
            args=cli_args(backend),
            cwd=directory,
            env=environment,
        )
        async with Client(params, mode=mode, read_timeout_seconds=90) as client:
            assert client.protocol_version == (
                "2026-07-28" if mode == "auto" else "2025-11-25"
            )
            names = {item.name for item in (await client.list_tools()).tools}
            assert {
                "terminal_execute",
                "computer_release",
                "computer_dashboard",
            } <= names
            resources = await client.list_resources()
            assert str(resources.resources[0].uri) == "ui://kilntainers/computers"
            resource = await client.read_resource("ui://kilntainers/computers")
            assert resource.contents[0].mime_type == "text/html;profile=mcp-app"
            assert isinstance(resource.contents[0], TextResourceContents)
            assert "sandbox_handle" in resource.contents[0].text
            result = await call(
                client,
                "terminal_execute",
                {"args": ["echo", "artifact-café"], "computer_id": name},
            )
            assert result["stdout"] == "artifact-café\n" and result["exit_code"] == 0
            again = await call(client, "terminal_execute", {"args": ["echo", "second"]})
            assert again["computer_id"] == result["computer_id"]  # stdio default reuse
        if backend == "docker":
            assert_absent(name)  # EOF must await confirmed Docker removal
        print(
            json.dumps(
                {"backend": backend, "protocol": mode, "installed_cli": "passed"}
            ),
            flush=True,
        )


async def persistent_docker(directory):
    name = f"artifact-{RUN[:12]}-persistent"
    immutable_id = None
    for mode in ["auto", "legacy"]:
        params = StdioServerParameters(
            command=sys.executable,
            args=cli_args("docker"),
            cwd=directory,
            env={**os.environ, "ENABLE_LIFECYCLE_TOOLS": "true"},
        )
        async with Client(params, mode=mode, read_timeout_seconds=90) as client:
            result = await call(
                client,
                "terminal_execute",
                {
                    "computer_id": name,
                    "temporary": False,
                    "command": "echo permanent-proof > /tmp/value"
                    if mode == "auto"
                    else "cat /tmp/value",
                },
            )
            record = next(
                item
                for item in owned_containers()
                if item["Config"]["Labels"]["kilntainers.computer-id"] == name
            )
            if immutable_id is None:
                immutable_id = record["Id"]
            else:
                assert record["Id"] == immutable_id
                assert result["stdout"] == "permanent-proof\n"
                await call(
                    client,
                    "computer_delete",
                    {"computer_id": name, "sandbox_handle": result["sandbox_handle"]},
                )
        if mode == "auto":
            assert any(item["Id"] == immutable_id for item in owned_containers())
        else:
            assert_absent(name)
    print(
        json.dumps(
            {"persistent_reconnect_both_eras": "passed", "container_id": immutable_id}
        ),
        flush=True,
    )


async def http_docker(directory):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "KILNTAINERS_AUTH_TOKEN": TOKEN,
        "ENABLE_LIFECYCLE_TOOLS": "true",
    }
    env.pop("PYTHONPATH", None)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *cli_args(
            "docker",
            "--transport",
            "http",
            "--port",
            str(port),
            "--session-timeout",
            "5",
        ),
        env=env,
        cwd=directory,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {TOKEN}"}
        ) as http:
            async with asyncio.timeout(20):
                while True:
                    if process.returncode is not None:
                        raise RuntimeError("Installed HTTP CLI exited before readiness")
                    try:
                        if (
                            await http.get(f"http://127.0.0.1:{port}/healthz")
                        ).status_code == 200:
                            break
                    except httpx2.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
            for mode in ["auto", "legacy"]:
                transport = streamable_http_client(url, http_client=http)
                async with Client(
                    transport, mode=mode, read_timeout_seconds=90
                ) as client:
                    a, b = await asyncio.gather(
                        call(
                            client,
                            "terminal_execute",
                            {
                                "command": "echo alpha > /tmp/value",
                                "computer_id": f"artifact-{RUN[:12]}-{mode}-a",
                            },
                        ),
                        call(
                            client,
                            "terminal_execute",
                            {
                                "command": "echo beta > /tmp/value",
                                "computer_id": f"artifact-{RUN[:12]}-{mode}-b",
                            },
                        ),
                    )
                    assert a["sandbox_handle"] != b["sandbox_handle"]
                    for first, text in [(a, "alpha\n"), (b, "beta\n")]:
                        result = await call(
                            client,
                            "terminal_execute",
                            {
                                "command": "cat /tmp/value",
                                "sandbox_handle": first["sandbox_handle"],
                            },
                        )
                        assert result["stdout"] == text
                    await call(
                        client,
                        "computer_release",
                        {"sandbox_handle": a["sandbox_handle"]},
                    )
                    assert_absent(a["computer_id"])
                    async with asyncio.timeout(12):
                        while any(
                            item["Config"]["Labels"].get("kilntainers.computer-id")
                            == b["computer_id"]
                            for item in owned_containers()
                        ):
                            await asyncio.sleep(0.1)
                print(
                    json.dumps(
                        {
                            "backend": "docker",
                            "http_protocol": mode,
                            "isolation_release_idle_removal": "passed",
                        }
                    ),
                    flush=True,
                )
            # Create a final fixture and terminate the server with work in progress.
            async with Client(
                streamable_http_client(url, http_client=http), mode="auto"
            ) as client:
                final = await call(
                    client,
                    "terminal_execute",
                    {
                        "command": "echo ready",
                        "computer_id": f"artifact-{RUN[:12]}-sigterm",
                    },
                )
                assert final["exit_code"] == 0
                process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(process.wait(), 40)
            assert process.returncode == 0
            assert_absent(final["computer_id"])
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 40)
            except TimeoutError:
                process.kill()
                await process.wait()
        stdout, stderr = await process.communicate()
        assert TOKEN.encode() not in stdout + stderr, "Bearer token leaked into logs"


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--docker", action="store_true")
    args = parser.parse_args()
    module = Path(kilntainers.__file__).resolve()
    assert not module.is_relative_to(ROOT / "src"), (
        "Acceptance requires the installed wheel"
    )
    print(json.dumps({"installed_module": str(module), "run_id": RUN}), flush=True)
    with tempfile.TemporaryDirectory(prefix="sandbox-wheel-") as directory:
        async with asyncio.timeout(240):
            if args.docker:
                docker("info")
                docker("image", "inspect", "debian:bookworm-slim")
                try:
                    await stdio("docker", directory)
                    await persistent_docker(directory)
                    await http_docker(directory)
                    assert owned_containers() == []
                finally:
                    for item in owned_containers():
                        docker("rm", "--force", item["Id"])
            else:
                await stdio("go_busybox", directory)


if __name__ == "__main__":
    asyncio.run(main())
