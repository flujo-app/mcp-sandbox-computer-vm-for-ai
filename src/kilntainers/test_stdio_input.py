"""Cross-platform stdin boundaries for real pipes."""

import json
import subprocess
import sys


def test_stdio_input_preserves_utf8_and_diverts_child_stdin():
    """Real pipes retain split UTF-8 input while children receive EOF."""
    source = r"""
import json, subprocess, sys
from kilntainers.stdio_input import interruptible_stdin
with interruptible_stdin(True):
    child = subprocess.check_output([sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"])
    data = sys.stdin.read()
sys.stdout.write(json.dumps({"data": data, "child": child.decode()}))
"""
    payload = "split UTF-8: café 漢字\n" * 8000
    result = subprocess.run(
        [sys.executable, "-c", source],
        input=payload,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
        encoding="utf-8",
    )
    assert json.loads(result.stdout) == {"data": payload, "child": ""}
