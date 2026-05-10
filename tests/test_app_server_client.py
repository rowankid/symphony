import json
import sys
import time
from pathlib import Path

from symphony_gitlab.app_server import CodexAppServerClient


def test_app_server_client_runs_thread_turn_and_collects_usage(tmp_path: Path):
    fake_server = tmp_path / "fake_server.py"
    fake_server.write_text(
        """
import json
import sys

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {}}), flush=True)
    elif method == "thread/start":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"thread": {"id": "thread-1"}, "approvalPolicy": "never", "approvalsReviewer": "user", "cwd": msg["params"]["cwd"], "model": "test", "modelProvider": "test", "sandbox": "workspace-write"}}), flush=True)
    elif method == "turn/start":
        thread_id = msg["params"]["threadId"]
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"turn": {"id": "turn-1"}}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "method": "thread/tokenUsage/updated", "params": {"threadId": thread_id, "turnId": "turn-1", "tokenUsage": {"last": {"inputTokens": 3, "outputTokens": 4, "totalTokens": 7, "cachedInputTokens": 0, "reasoningOutputTokens": 0}, "total": {"inputTokens": 3, "outputTokens": 4, "totalTokens": 7, "cachedInputTokens": 0, "reasoningOutputTokens": 0}}}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": "turn-1"}}}), flush=True)
    elif method == "thread/unsubscribe":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {}}), flush=True)
""",
        encoding="utf-8",
    )

    client = CodexAppServerClient(f"{sys.executable} {fake_server}", cwd=tmp_path, read_timeout_ms=5000, turn_timeout_ms=5000)

    result = client.run_turn("hello")

    assert result.thread_id == "thread-1"
    assert result.turn_id == "turn-1"
    assert result.session_id == "thread-1-turn-1"
    assert result.input_tokens == 3
    assert result.output_tokens == 4
    assert result.total_tokens == 7


def test_app_server_client_rejects_unsupported_server_requests(tmp_path: Path):
    fake_server = tmp_path / "fake_server.py"
    seen = tmp_path / "seen.json"
    fake_server.write_text(
        f"""
import json
import sys

print(json.dumps({{"jsonrpc": "2.0", "id": 99, "method": "dynamicTool/call", "params": {{}}}}), flush=True)
line = sys.stdin.readline()
open({str(seen)!r}, "w").write(line)
""",
        encoding="utf-8",
    )
    client = CodexAppServerClient(f"{sys.executable} {fake_server}", cwd=tmp_path, read_timeout_ms=5000, turn_timeout_ms=5000)

    client.start()
    client.read_next_message()
    deadline = time.time() + 2
    while not seen.exists() and time.time() < deadline:
        time.sleep(0.01)
    client.stop()

    response = json.loads(seen.read_text(encoding="utf-8"))
    assert response["id"] == 99
    assert response["error"]["code"] == -32601
