#!/usr/bin/env python3
"""Small authenticated control panel for the Goodstack browser batch worker."""
from __future__ import annotations

import base64
import csv
import hmac
import json
import os
import signal
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("DATA_DIR", "/data"))
OUT = DATA / "results"
INPUT = Path(os.getenv("INPUT_FILE", "/data/input.json"))
WEB = ROOT / "web" / "index.html"
USER = os.getenv("UI_USERNAME", "").strip()
PASSWORD = os.getenv("UI_PASSWORD", "")
PORT = int(os.getenv("UI_INTERNAL_PORT", "8765"))
worker: subprocess.Popen | None = None
worker_lock = threading.Lock()


def read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def checkpoint_snapshot() -> tuple[list[dict], int]:
    path = OUT / "checkpoint.jsonl"
    latest: dict[str, dict] = {}
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    key = row.get("inputKey")
                    if key:
                        latest[key] = row
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return list(latest.values()), len(latest)


class Handler(BaseHTTPRequestHandler):
    server_version = "GoodstackDashboard/1.0"

    def log_message(self, fmt, *args):
        # Avoid logging Basic Auth headers or query strings.
        print("dashboard", self.command, self.path.split("?", 1)[0], args[1] if len(args) > 1 else "")

    def authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(username, USER) and hmac.compare_digest(password, PASSWORD)

    def require_auth(self) -> bool:
        if USER and PASSWORD and self.authorized():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Goodstack Batch Checker", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def send_bytes(self, body: bytes, content_type: str, status: int = 200, filename: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_bytes(body, "application/json; charset=utf-8", status)

    def do_GET(self):
        if not self.require_auth():
            return
        path = urlparse(self.path).path
        if path == "/":
            try:
                body = WEB.read_bytes()
            except OSError:
                self.send_json({"error": "UI 文件不存在"}, 500)
                return
            self.send_bytes(body, "text/html; charset=utf-8")
        elif path == "/api/status":
            self.status()
        elif path == "/api/results":
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = min(max(int(query.get("limit", ["50"])[0]), 1), 500)
            except ValueError:
                limit = 50
            result = read_json(OUT / "both_dates.json", {"summary": {}, "results": []})
            result["results"] = result.get("results", [])[:limit]
            self.send_json(result)
        elif path == "/api/log":
            try:
                lines = (OUT / "worker.log").read_text(encoding="utf-8", errors="replace").splitlines()[-100:]
            except OSError:
                lines = []
            self.send_json({"lines": lines})
        elif path in {"/download/both_dates.csv", "/download/both_dates.json"}:
            file_name = "both_dates.csv" if path.endswith(".csv") else "both_dates.json"
            try:
                body = (OUT / file_name).read_bytes()
            except OSError:
                body = b"" if file_name.endswith(".csv") else b'{"summary":{},"results":[]}'
            mime = "text/csv; charset=utf-8" if file_name.endswith(".csv") else "application/json; charset=utf-8"
            self.send_bytes(body, mime, filename=file_name)
        else:
            self.send_json({"error": "Not found"}, 404)

    def status(self):
        with worker_lock:
            running = worker is not None and worker.poll() is None
            exit_code = None if running or worker is None else worker.returncode
        rows, processed = checkpoint_snapshot()
        summary = read_json(OUT / "summary.json", {})
        results = read_json(OUT / "both_dates.json", {"results": []})
        all_count = summary.get("inputRecordCount", 0)
        if not all_count:
            try:
                payload = json.loads(INPUT.read_text(encoding="utf-8-sig"))
                records = payload if isinstance(payload, list) else payload.get("机构记录") or payload.get("records") or payload.get("data") or []
                if isinstance(records, dict):
                    records = records.get("data", [])
                all_count = len(records) if isinstance(records, list) else 0
            except (OSError, json.JSONDecodeError, TypeError):
                all_count = 0
        progress = min(100, round(processed * 100 / all_count)) if all_count else 0
        recent = sorted(rows, key=lambda row: row.get("checkedAt", ""), reverse=True)[:8]
        self.send_json({
            "running": running,
            "exitCode": exit_code,
            "processed": processed,
            "total": all_count,
            "progress": progress,
            "bothDates": len(results.get("results", [])),
            "summary": summary,
            "recent": recent,
        })

    def do_POST(self):
        if not self.require_auth():
            return
        path = urlparse(self.path).path
        if path == "/api/start":
            self.start_worker()
        elif path == "/api/stop":
            self.stop_worker()
        else:
            self.send_json({"error": "Not found"}, 404)

    def start_worker(self):
        global worker
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 4096:
                self.send_json({"error": "Request too large"}, 413)
                return
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid request"}, 400)
            return
        mode = payload.get("mode", "full")
        if mode not in {"pilot", "full", "recheck"}:
            self.send_json({"error": "Invalid run mode"}, 400)
            return
        command = ["python", str(ROOT / "goodstack_batch_verify.py"), "--input", str(INPUT), "--output-dir", str(OUT)]
        if mode == "pilot":
            command.extend(["--max-records", "20"])
        elif mode == "recheck":
            command.append("--no-resume")
        OUT.mkdir(parents=True, exist_ok=True)
        log = (OUT / "worker.log").open("a", encoding="utf-8")
        with worker_lock:
            if worker is not None and worker.poll() is None:
                log.close()
                self.send_json({"error": "任务正在运行"}, 409)
                return
            worker = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            self.send_json({"started": True, "pid": worker.pid, "mode": mode})

    def stop_worker(self):
        with worker_lock:
            if worker is None or worker.poll() is not None:
                self.send_json({"stopped": False, "message": "当前没有运行中的任务"})
                return
            try:
                worker.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
        self.send_json({"stopped": True, "message": "已发送停止信号；已完成机构会保留断点"})


def main() -> None:
    if not USER or not PASSWORD:
        raise SystemExit("UI_USERNAME 和 UI_PASSWORD 必须配置；请通过 deploy.sh 生成登录凭据。")
    OUT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Goodstack dashboard listening on 0.0.0.0:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with worker_lock:
            if worker is not None and worker.poll() is None:
                worker.send_signal(signal.SIGINT)
        server.server_close()


if __name__ == "__main__":
    main()
