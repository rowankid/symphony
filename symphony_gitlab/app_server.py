from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AppServerError(Exception):
    pass


@dataclass(slots=True)
class AppServerTurnResult:
    thread_id: str
    turn_id: str
    session_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class CodexAppServerClient:
    def __init__(self, command: str, cwd: Path, read_timeout_ms: int, turn_timeout_ms: int, approval_policy: Any = None, thread_sandbox: Any = None, turn_sandbox_policy: Any = None):
        self.command = command
        self.cwd = cwd
        self.read_timeout_ms = read_timeout_ms
        self.turn_timeout_ms = turn_timeout_ms
        self.approval_policy = approval_policy
        self.thread_sandbox = thread_sandbox
        self.turn_sandbox_policy = turn_sandbox_policy
        self.process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._next_id = 1
        self._usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def start(self) -> None:
        if self.process is not None:
            return
        self.process = subprocess.Popen(
            ["bash", "-lc", self.command],
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def run_turn(self, prompt: str) -> AppServerTurnResult:
        self.start()
        try:
            self._request("initialize", {})
            thread_response = self._request(
                "thread/start",
                {
                    "cwd": str(self.cwd),
                    "approvalPolicy": self.approval_policy,
                    "sandbox": self.thread_sandbox,
                    "ephemeral": True,
                    "threadSource": "symphony-gitlab",
                    "sessionStartSource": "symphony-gitlab",
                },
            )
            thread_id = thread_response["thread"]["id"]
            turn_response = self._request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "cwd": str(self.cwd),
                    "sandboxPolicy": self.turn_sandbox_policy,
                    "input": [{"type": "text", "text": prompt}],
                },
            )
            turn_id = turn_response["turn"]["id"]
            self._wait_turn_completed(thread_id, turn_id)
            self._request("thread/unsubscribe", {"threadId": thread_id})
            return AppServerTurnResult(
                thread_id=thread_id,
                turn_id=turn_id,
                session_id=f"{thread_id}-{turn_id}",
                input_tokens=self._usage["input_tokens"],
                output_tokens=self._usage["output_tokens"],
                total_tokens=self._usage["total_tokens"],
            )
        finally:
            self.stop()

    def read_next_message(self) -> dict[str, Any]:
        self.start()
        try:
            message = self._messages.get(timeout=self.read_timeout_ms / 1000)
        except queue.Empty as exc:
            stderr = ""
            if self.process is not None and self.process.stderr is not None and self.process.poll() is not None:
                stderr = self.process.stderr.read()
            detail = f": {stderr.strip()}" if stderr.strip() else ""
            raise AppServerError(f"app-server read timeout{detail}") from exc
        if "id" in message and "method" in message and "result" not in message and "error" not in message:
            self._send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": f"Unsupported server request: {message['method']}"}})
        return message

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = self.read_next_message()
            if message.get("id") == request_id:
                if "error" in message:
                    raise AppServerError(str(message["error"]))
                return message.get("result", {})
            self._handle_notification(message)

    def _wait_turn_completed(self, thread_id: str, turn_id: str) -> None:
        deadline = time.monotonic() + self.turn_timeout_ms / 1000
        while time.monotonic() < deadline:
            message = self.read_next_message()
            if self._handle_notification(message, thread_id=thread_id, turn_id=turn_id):
                return
        raise AppServerError("turn timeout")

    def _handle_notification(self, message: dict[str, Any], thread_id: str | None = None, turn_id: str | None = None) -> bool:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "thread/tokenUsage/updated":
            total = ((params.get("tokenUsage") or {}).get("total") or {})
            self._usage = {
                "input_tokens": int(total.get("inputTokens", 0)),
                "output_tokens": int(total.get("outputTokens", 0)),
                "total_tokens": int(total.get("totalTokens", 0)),
            }
        if method == "turn/completed":
            completed_thread_id = params.get("threadId")
            completed_turn_id = (params.get("turn") or {}).get("id")
            return (thread_id is None or completed_thread_id == thread_id) and (turn_id is None or completed_turn_id == turn_id)
        if method == "error":
            raise AppServerError(str(params))
        return False

    def _send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise AppServerError("app-server is not running")
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            try:
                self._messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue
