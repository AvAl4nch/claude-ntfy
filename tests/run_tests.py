#!/usr/bin/env python3
"""Test suite for ntfy-notify. No dependencies, no network, no real pushes.

    python3 tests/run_tests.py

Covers the behaviours that actually broke in practice: duplicate suppression
under real concurrency, auth encoding, summariser grounding, and which events
stay silent. Network tests run against a throwaway local HTTP server, so the
suite never touches a real ntfy topic.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ntfy_notify.py"
sys.path.insert(0, str(ROOT / "scripts"))

from ntfy_notify import (  # noqa: E402
    LOCK_DIR, auth_query_param, condense, grounded, resolve_auth, should_notify,
)

FAILURES = []


def check(name, condition):
    print("  {0:<48} {1}".format(name, "ok" if condition else "FAIL"))
    if not condition:
        FAILURES.append(name)


def run(payload, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *extra],
        input=json.dumps(payload), capture_output=True, text=True,
    )


def test_payloads():
    print("payload suite")
    path = ROOT / "evals" / "payloads.jsonl"
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    bad = 0
    for payload in rows:
        case = payload.pop("_case")
        r = run(payload, "--dry-run")
        silent = r.stdout.strip() == ""
        if r.returncode != 0 or silent != case.endswith("SILENT"):
            bad += 1
            print("    FAIL {0}".format(case))
    check("{0} payload cases".format(len(rows)), bad == 0)


def test_auth():
    print("\nauth")
    check("access token -> Bearer", resolve_auth(token="tk_x") == "Bearer tk_x")
    check("username/password -> Basic",
          resolve_auth(username="a", password="b")
          == "Basic " + base64.b64encode(b"a:b").decode())
    check("user:pass in the token field -> Basic",
          resolve_auth(token="a:b").startswith("Basic "))
    check("no credentials -> anonymous", resolve_auth() is None)
    q = auth_query_param("Basic abc")
    check("?auth= is url-safe and unpadded", not any(c in q for c in "+/="))
    check("?auth= round-trips",
          base64.urlsafe_b64decode(q + "=" * (-len(q) % 4)).decode() == "Basic abc")


def test_summariser():
    print("\nsummariser")
    check("a terse reply survives intact", condense("Done.") == "Done")
    check("a long reply is capped",
          len(condense("alpha bravo charlie delta echo foxtrot golf hotel").split()) <= 7)
    check("markdown tables are skipped",
          condense("| a | b |\n|---|---|\n\nMigrations applied cleanly.").startswith("Migrations"))
    check("an ungrounded summary is rejected",
          not grounded("Nothing accomplished yet", "Refactored the payment module"))
    check("a grounded summary is accepted",
          grounded("Refactored payment module", "Refactored the payment module"))


def test_suppression():
    print("\nsuppression")
    check("re-entrant Stop stays silent",
          not should_notify({"hook_event_name": "Stop", "stop_hook_active": True}))
    check("auth_success stays silent",
          not should_notify({"hook_event_name": "Notification",
                             "notification_type": "auth_success"}))
    check("idle after a completion stays silent",
          not should_notify({"hook_event_name": "Notification",
                             "notification_type": "idle_prompt"}, "stop"))
    check("idle on its own fires",
          should_notify({"hook_event_name": "Notification",
                         "notification_type": "idle_prompt"}, None))
    check("a permission prompt always fires",
          should_notify({"hook_event_name": "Notification",
                         "notification_type": "permission_prompt"}, "stop"))
    check("an unknown event stays silent",
          not should_notify({"hook_event_name": "PreToolUse"}))


def test_concurrency_and_delivery():
    print("\nconcurrency and delivery")
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append(json.loads(raw))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:{0}/t".format(port)

    def fire(payload):
        run(payload, "--url", url, "--summarizer", "heuristic")

    def burst(payload, n):
        shutil.rmtree(LOCK_DIR, ignore_errors=True)
        received.clear()
        threads = [threading.Thread(target=fire, args=(payload,)) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return len(received)

    stop = {"hook_event_name": "Stop", "cwd": "/p/demo", "session_id": "cc",
            "last_assistant_message": "Fixed the flaky login test."}
    check("2 registrations firing at once -> 1 push", burst(stop, 2) == 1)
    check("4 registrations firing at once -> 1 push", burst(stop, 4) == 1)

    shutil.rmtree(LOCK_DIR, ignore_errors=True)
    received.clear()
    perm = {"hook_event_name": "Notification", "notification_type": "permission_prompt",
            "cwd": "/p/d", "session_id": "p"}
    fire(dict(perm, message="Claude needs your permission to use Bash"))
    fire(dict(perm, message="Claude needs your permission to use Write"))
    # Both truncate to the same six words, so a summary-based fingerprint would
    # wrongly swallow the second one.
    check("two different permission prompts both send", len(received) == 2)

    shutil.rmtree(LOCK_DIR, ignore_errors=True)
    received.clear()
    fire(dict(stop, session_id="x"))
    fire(dict(stop, session_id="y"))
    check("same text in different sessions both send", len(received) == 2)

    received.clear()
    r = run(stop, "--url", url, "--summarizer", "heuristic")
    check("a hook never exits non-zero", r.returncode == 0)
    server.shutdown()


def test_never_crashes():
    print("\nrobustness")
    for label, raw in [("empty stdin", ""), ("garbage", "not json"),
                       ("a bare list", "[1,2,3]"), ("an empty object", "{}")]:
        r = subprocess.run([sys.executable, str(SCRIPT), "--dry-run"],
                           input=raw, capture_output=True, text=True)
        check("{0} exits 0 and stays quiet".format(label),
              r.returncode == 0 and r.stdout.strip() == "")

    env = dict(os.environ, HOME=str(ROOT / ".nonexistent-home"),
               USERPROFILE=str(ROOT / ".nonexistent-home"))
    r = subprocess.run([sys.executable, str(SCRIPT)],
                       input=json.dumps({"hook_event_name": "Stop", "cwd": "/p/d",
                                         "last_assistant_message": "Hello."}),
                       capture_output=True, text=True, env=env)
    check("an unconfigured install sends nothing",
          r.returncode == 0 and r.stdout.strip() == "")


def main():
    test_payloads()
    test_auth()
    test_summariser()
    test_suppression()
    test_concurrency_and_delivery()
    test_never_crashes()
    print("\n" + ("ALL PASS" if not FAILURES else "FAILED: " + ", ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
