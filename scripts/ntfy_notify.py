#!/usr/bin/env python3
"""Send a ntfy push when Claude Code finishes a turn or needs the user.

Reads Claude Code hook JSON on stdin and POSTs a short notification to a ntfy
topic. Designed to be wired to the Stop and Notification hook events.

Contract with the harness: this script must NEVER break a session. Every failure
path exits 0 and stays quiet, because a notifier that blocks the agent is worse
than a notifier that misses a message.

Config resolution (first hit wins):
  1. CLI flags
  2. environment: NTFY_CLAUDE_URL, NTFY_CLAUDE_TOKEN, NTFY_CLAUDE_SUMMARIZER
  3. config file: ~/.claude/ntfy-notify.json
  4. built-in defaults below
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# No default topic on purpose. A baked-in URL would mean an unconfigured install
# silently publishes to a topic someone else owns -- and ntfy topics are public
# by default, so that leaks work summaries to strangers. Unconfigured = silent.
DEFAULT_URL = None
DEFAULT_SUMMARIZER = "heuristic"  # or "llm"
SUMMARY_WORDS = 6
HTTP_TIMEOUT = 8
# The llm summarizer shells out to `claude -p`, which is CLI-startup dominated
# (~5-10s observed). Keep this comfortably under the hook's own timeout, or the
# harness kills us mid-call and the notification is lost silently.
LLM_TIMEOUT = 20
CONFIG_PATH = Path.home() / ".claude" / "ntfy-notify.json"
LOG_PATH = Path.home() / ".claude" / "ntfy-notify.log"
STATE_PATH = Path.home() / ".claude" / "ntfy-notify.state.json"
STATE_TTL = 24 * 3600  # forget sessions after a day so the file cannot grow forever

# Notification types that mean "a human needs to do something".
# Anything else (auth_success, quota_* chatter) is just noise on a phone.
WAITING_TYPES = {
    "permission_prompt",
    "idle_prompt",
    "agent_needs_input",
    "elicitation_dialog",
    "elicitation_url_dialog",
}


# ---------------------------------------------------------------------------
# summarising
# ---------------------------------------------------------------------------

# Openers that carry no information. Stripped only when enough words survive,
# so a terse "Done." stays "Done." instead of becoming empty.
FILLER = re.compile(
    r"^(?:ok(?:ay)?|sure|alright|great|perfect|got it|understood|yes|no)\b[,:—-]*\s*",
    re.I,
)
LEAD_PRONOUN = re.compile(r"^(?:i(?:'ve|'ll| have| will)?|we(?:'ve| have)?|let me)\s+", re.I)


def strip_markdown(text):
    """Reduce markdown to the prose a phone notification can actually show."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)        # fenced code
    text = re.sub(r"^\s*\|.*$", " ", text, flags=re.M)         # table rows
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)      # headings
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)       # bullets
    text = re.sub(r"^\s*\d+[.)]\s+", "", text, flags=re.M)     # numbered lists
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)       # links -> label
    text = re.sub(r"[`*_~]+", "", text)                         # emphasis / ticks
    text = re.sub(r"<[^>]+>", " ", text)                        # stray html
    return text


def first_sentence(text):
    """First prose sentence, skipping lines that are pure structure."""
    for line in (raw.strip() for raw in text.splitlines()):
        if len(line.split()) < 2:
            continue
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\"'])", line)
        return parts[0].strip() if parts else line
    return text.strip()


def condense(text, limit=SUMMARY_WORDS):
    """Squeeze an assistant message down to ~`limit` words of signal."""
    if not text:
        return ""
    sentence = first_sentence(strip_markdown(text))
    sentence = re.sub(r"\s+", " ", sentence).strip(" .,:;—-")
    sentence = FILLER.sub("", sentence)
    stripped = LEAD_PRONOUN.sub("", sentence)
    if len(stripped.split()) >= 4:  # keep the pronoun if the rest is too thin
        sentence = stripped
    # Bare dashes are punctuation, not words -- they should not eat a slot.
    words = [w for w in sentence.split() if w not in {"-", "—", "–", "--"}]
    if not words:
        return ""
    truncated = len(words) > limit
    out = " ".join(words[:limit]).rstrip(",;:—-")
    out = out[:1].upper() + out[1:]  # stripping "I have" can leave a lowercase start
    return out + "…" if truncated else out.rstrip(".")


def llm_condense(text, limit=SUMMARY_WORDS):
    """Optional: ask Haiku for the summary. Better phrasing, costs a call.

    Falls back to the heuristic on any failure so the hook still fires.
    """
    import subprocess

    prompt = (
        "Summarize what was just accomplished in at most {n} words. "
        "It must be a complete phrase, not a truncated one. "
        "Reply with only the summary: no preamble, no quotes, no trailing period.\n\n"
        "---\n{body}"
    ).format(n=limit, body=text[:4000])
    try:
        out = subprocess.run(
            ["claude", "-p", prompt, "--model", "claude-haiku-4-5-20251001"],
            capture_output=True, text=True, timeout=LLM_TIMEOUT,
        )
        lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        if out.returncode == 0 and lines:
            words = lines[0].strip().strip("\"'").rstrip(".").split()
            # Allow a little slack: a whole thought one word over budget beats a
            # fragment. A big overrun means the model ignored the brief, and the
            # heuristic's truncation is at least predictable.
            if words and len(words) <= limit + 2:
                return " ".join(words)
    except Exception:
        pass
    return condense(text, limit)


# ---------------------------------------------------------------------------
# message construction
# ---------------------------------------------------------------------------

def read_last_assistant_message(transcript_path):
    """Fallback for payloads that lack last_assistant_message."""
    try:
        rows = []
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        for row in reversed(rows):
            if row.get("type") != "assistant" or row.get("isSidechain"):
                continue
            for block in row.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    if block.get("text", "").strip():
                        return block["text"]
    except Exception:
        pass
    return ""


def project_name(payload):
    cwd = payload.get("cwd") or ""
    return Path(cwd).name or "claude"


def build(payload, summarizer, last_kind=None):
    """Turn a hook payload into ntfy fields, or None if it should stay silent.

    `last_kind` is what this session last notified about ("stop" / "waiting"),
    used to drop the idle nag that trails every completion.
    """
    event = payload.get("hook_event_name") or ""
    where = project_name(payload)

    if event == "Stop":
        # A Stop triggered by a stop hook would double-send.
        if payload.get("stop_hook_active"):
            return None
        raw = payload.get("last_assistant_message") or ""
        if not raw and payload.get("transcript_path"):
            raw = read_last_assistant_message(payload["transcript_path"])
        summary = (llm_condense(raw) if summarizer == "llm" else condense(raw))
        return {
            "title": "Claude finished · {0}".format(where),
            "body": summary or "Turn finished",
            "tags": "white_check_mark",
            "priority": "3",
            "_kind": "stop",
        }

    if event == "Notification":
        kind = payload.get("notification_type") or ""
        if kind and kind not in WAITING_TYPES:
            return None  # auth / quota chatter is not worth a buzz
        # The idle nag never adds information once this session has already
        # buzzed: you were told the turn ended (or that Claude wants permission),
        # and "waiting for your input" only restates that you have not replied
        # yet. It still fires when it is the FIRST thing worth saying. Any real
        # event -- a completion, a permission request -- re-arms it.
        if kind == "idle_prompt" and last_kind in ("stop", "waiting"):
            return None
        raw = payload.get("message") or ""
        return {
            "title": "Claude needs you · {0}".format(where),
            "body": condense(raw) or "Waiting for your input",
            "tags": "raised_hand",
            "priority": "4",  # actionable, so louder than a completion
            "_kind": "waiting",
        }

    return None


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------

def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_state():
    """Per-session record of what we last notified about. Best-effort only."""
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def save_state(state, session_id, kind):
    """Record `kind` for this session, pruning entries older than STATE_TTL."""
    if not session_id:
        return
    import time

    now = time.time()
    try:
        pruned = {k: v for k, v in state.items()
                  if isinstance(v, dict) and now - v.get("ts", 0) < STATE_TTL}
        pruned[session_id] = {"kind": kind, "ts": now}
        STATE_PATH.write_text(json.dumps(pruned), encoding="utf-8")
    except Exception:
        pass  # dedup is a nicety; never let it break the notification


def log(line):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
    except Exception:
        pass


def split_topic_url(url):
    """Split "https://host/topic" into ("https://host", "topic").

    ntfy's JSON API posts to the server root with the topic in the body. We take
    that route rather than the header API because titles carry non-ASCII (the
    separator, em-dashes out of assistant prose) and HTTP headers are latin-1.
    """
    base, _, topic = url.rstrip("/").rpartition("/")
    return base, topic


def publish(url, token, fields):
    base, topic = split_topic_url(url)
    if not topic:
        return False, "cannot parse topic from URL {0!r}".format(url)
    body = {
        "topic": topic,
        "title": fields["title"],
        "message": fields["body"],
        "tags": [t for t in fields["tags"].split(",") if t],
        "priority": int(fields["priority"]),
    }
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        # Cloudflare (and most WAFs) 403 the default "Python-urllib/3.x" agent.
        "User-Agent": "ntfy-notify/1.0 (+claude-code-hook)",
    }
    if token:
        headers["Authorization"] = "Bearer {0}".format(token)
    req = urllib.request.Request(
        base, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return True, "HTTP {0}".format(resp.status)
    except urllib.error.HTTPError as exc:
        return False, "HTTP {0} {1}".format(exc.code, exc.reason)
    except Exception as exc:
        return False, "{0}: {1}".format(type(exc).__name__, exc)


def doctor(url, token, summarizer):
    """Explain why notifications are or aren't arriving.

    Worth having because this script is deliberately silent on failure: without
    a self-check, "it stopped working" means reading a log you don't know exists.
    """
    import shutil

    ok = True
    print("interpreter")
    for name in ("python3", "python"):
        path = shutil.which(name)
        print("  {0:<8} {1}".format(name, path or "not found"))
    print("  running  {0}".format(sys.executable))

    print("\nconfig")
    print("  file       {0}{1}".format(CONFIG_PATH, "" if CONFIG_PATH.exists() else "  (absent)"))
    print("  topic URL  {0}".format(url or "NOT SET -- nothing will be sent"))
    print("  token      {0}".format("set" if token else "none (public topic)"))
    print("  summarizer {0}".format(summarizer))
    if not url:
        ok = False
    if summarizer == "llm" and not shutil.which("claude"):
        print("  WARNING: summarizer is 'llm' but the claude CLI is not on PATH;"
              " every summary will fall back to the heuristic")

    print("\nhook registration")
    found = []
    settings = Path.home() / ".claude" / "settings.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
        for event, groups in (data.get("hooks") or {}).items():
            for group in groups:
                for h in group.get("hooks", []):
                    blob = "{0} {1}".format(h.get("command", ""), " ".join(h.get("args") or []))
                    if "ntfy_notify" in blob:
                        found.append("{0} (settings.json)".format(event))
    except Exception:
        pass
    # A hooks.json in the checkout is not the same as an installed plugin -- it is
    # only live when this copy is the one Claude Code installed under ~/.claude/plugins.
    here = Path(__file__).resolve()
    plugin_root = here.parent.parent
    plugins_dir = (Path.home() / ".claude" / "plugins").resolve()
    installed = plugins_dir in here.parents
    plugin_events = []
    plugin_hooks = plugin_root / "hooks" / "hooks.json"
    if plugin_hooks.exists():
        try:
            plugin_events = list((json.loads(plugin_hooks.read_text(encoding="utf-8"))
                                  .get("hooks") or {}))
        except Exception:
            pass
    if plugin_events:
        if installed:
            found += ["{0} (plugin)".format(e) for e in plugin_events]
        else:
            print("  note: this checkout ships plugin hooks for {0}, but it is not"
                  " installed via /plugin, so they are inactive"
                  .format(", ".join(sorted(plugin_events))))

    if found:
        for f in sorted(set(found)):
            print("  {0}".format(f))
        if any("settings.json" in f for f in found) and any("(plugin)" in f for f in found):
            print("  WARNING: registered BOTH ways -- you will get duplicate pushes."
                  " Remove the settings.json entries, or uninstall the plugin.")
    else:
        print("  none active -- install the plugin, or register the hooks manually")
        ok = False

    print("\nconnectivity")
    if url:
        sent, detail = publish(url, token, {
            "title": "ntfy-notify doctor",
            "body": "Self-check reached the server",
            "tags": "stethoscope", "priority": "1",  # min priority: no buzz
        })
        print("  publish    {0}".format(detail))
        ok = ok and sent
    else:
        print("  publish    skipped (no URL)")

    print("\nrecent errors ({0})".format(LOG_PATH))
    try:
        tail = LOG_PATH.read_text(encoding="utf-8").strip().splitlines()[-3:]
        print("\n".join("  " + t for t in tail) if tail else "  (none)")
    except Exception:
        print("  (no log file -- nothing has failed yet)")

    print("\n{0}".format("OK" if ok else "PROBLEMS FOUND (see above)"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="ntfy notifier for Claude Code hooks")
    ap.add_argument("--url", help="ntfy topic URL")
    ap.add_argument("--token", help="bearer token for protected topics")
    ap.add_argument("--summarizer", choices=["heuristic", "llm"],
                    help="how to shorten the result (default: heuristic)")
    ap.add_argument("--event", help="override hook_event_name (Stop | Notification)")
    ap.add_argument("--dry-run", action="store_true", help="print, do not send")
    ap.add_argument("--test", action="store_true", help="send a synthetic test push")
    ap.add_argument("--doctor", action="store_true",
                    help="diagnose config, hook registration and connectivity")
    args = ap.parse_args()

    cfg = load_config()
    url = args.url or os.environ.get("NTFY_CLAUDE_URL") or cfg.get("url") or DEFAULT_URL
    token = args.token or os.environ.get("NTFY_CLAUDE_TOKEN") or cfg.get("token")
    summarizer = (args.summarizer or os.environ.get("NTFY_CLAUDE_SUMMARIZER")
                  or cfg.get("summarizer") or DEFAULT_SUMMARIZER)

    if args.doctor:
        return doctor(url, token, summarizer)

    session_id = None
    state = {}

    if args.test:
        fields = {
            "title": "ntfy-notify self-test",
            "body": "Hook wiring verified end to end",
            "tags": "wrench",
            "priority": "3",
            "_kind": "test",
        }
    else:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except (json.JSONDecodeError, OSError, ValueError):
            return 0  # malformed stdin is not the session's problem
        if not isinstance(payload, dict):
            return 0
        if args.event:
            payload["hook_event_name"] = args.event
        session_id = payload.get("session_id")
        state = load_state()
        last_kind = (state.get(session_id) or {}).get("kind") if session_id else None
        fields = build(payload, summarizer, last_kind)
        if fields is None:
            return 0

    if args.dry_run:
        print(json.dumps(fields, ensure_ascii=False, indent=2))
        return 0

    if not url:
        log("[ntfy-notify] no topic URL configured -- set 'url' in {0}, "
            "or NTFY_CLAUDE_URL. Nothing sent.".format(CONFIG_PATH))
        return 0

    ok, detail = publish(url, token, fields)
    if ok:
        save_state(state, session_id, fields.get("_kind"))
    else:
        log("[ntfy-notify] send failed ({0}) title={1!r}".format(detail, fields["title"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # last-resort guard: never fail a turn
        log("[ntfy-notify] crashed: {0}: {1}".format(type(exc).__name__, exc))
        sys.exit(0)
