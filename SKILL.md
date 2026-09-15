---
name: ntfy-notify
description: Push a ntfy notification to your phone when Claude Code finishes a turn or is blocked waiting on you, with a ~6-word summary of the result. Use this skill whenever the user wants to install, configure, test, retune, silence, or troubleshoot ntfy/push/mobile notifications for Claude Code sessions — including phrasings like "notify me when Claude is done", "ping my phone when it needs input", "my ntfy alerts stopped working", "change the notification topic", or "make the summaries better". Also use it when hooks fire but no notification arrives, or when notifications are too noisy.
---

# ntfy-notify

Sends a push to a [ntfy](https://ntfy.sh) topic at two moments that matter:

| Moment | Hook event | Title | Priority |
|---|---|---|---|
| Claude finished its turn | `Stop` | `Claude finished · <project>` | 3 (default) |
| Claude is blocked on you | `Notification` | `Claude needs you · <project>` | 4 (high) |

The body is a ~6-word summary of what actually happened, so the notification is
readable on a lock screen without opening anything.

## The thing to understand first

**This is hook-driven, not model-driven.** Claude cannot reliably notify you that
it stopped, because by the time it stops it is no longer running. The harness
fires the hooks; `scripts/ntfy_notify.py` turns the hook payload into a push.

So "install this skill" really means "register two hooks in settings.json". If
notifications are not arriving, the hook registration is the first thing to check,
not the script.

## Install

1. Point the hooks at the script in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ {
          "type": "command", "command": "python",
          "args": ["<abs-path>/scripts/ntfy_notify.py"],
          "async": true, "timeout": 30
      } ] }
    ],
    "Notification": [
      { "hooks": [ {
          "type": "command", "command": "python",
          "args": ["<abs-path>/scripts/ntfy_notify.py"],
          "async": true, "timeout": 30
      } ] }
    ]
  }
}
```

   Use the `args` exec form rather than a single shell string: it spawns the
   interpreter directly, so Windows paths with spaces or backslashes never reach
   a shell parser. `async: true` keeps the push off the critical path — the turn
   ends immediately and the notification follows.

2. Merge, never overwrite. If `hooks.Stop` already exists, append to the array.

3. Settings are watched live, but a session that started before the file existed
   may not pick it up. If the hook does not fire, have the user open `/hooks`
   once or restart. You cannot open `/hooks` yourself — it ends the turn.

## Configure

Resolution order: CLI flag → env var → `~/.claude/ntfy-notify.json` → built-in default.

| Setting | Env var | Config key | Default |
|---|---|---|---|
| Topic URL | `NTFY_CLAUDE_URL` | `url` | *(none -- must be set)* |
| Bearer token | `NTFY_CLAUDE_TOKEN` | `token` | none (public topic) |
| Summarizer | `NTFY_CLAUDE_SUMMARIZER` | `summarizer` | `heuristic` |

```json
{ "url": "https://ntfy.example.com/my-topic", "token": "tk_...", "summarizer": "heuristic" }
```

### Choosing a summarizer

`heuristic` (default) strips markdown, takes the first prose sentence, drops
filler openers, and truncates to six words. Instant, free, no network call.

`llm` shells out to `claude -p` with Haiku for a better-phrased summary. It reads
more naturally but adds a few seconds and a token cost to **every** turn, and
falls back to the heuristic if the call fails. Suggest it only if the user says
the summaries read awkwardly — the cost is per-turn and recurring, so it should
be a deliberate choice.

## Verify

Always dry-run before sending, so testing never spams the user's phone:

```bash
echo '{"hook_event_name":"Stop","cwd":"/p/demo","last_assistant_message":"Fixed the flaky login test."}' \
  | python scripts/ntfy_notify.py --dry-run
```

Then one real send:

```bash
python scripts/ntfy_notify.py --test
```

Confirm it landed by polling the topic — a 200 on publish only proves the server
accepted it:

```bash
curl -s "$NTFY_CLAUDE_URL/json?poll=1&since=2"
```

The last leg, phone delivery, is the one leg no command can verify. Say so rather
than claiming the notification was delivered.

## What is deliberately silent

The script exits 0 and sends nothing when:

- `stop_hook_active` is true — a Stop triggered by a stop hook would double-send.
- `notification_type` is not a waiting-on-human type. `auth_success` and the
  `quota_*` events fire routinely and are not worth a buzz; the allowlist is
  `WAITING_TYPES` in the script.
- The event is `idle_prompt` and this session has already notified. Claude Code
  raises it a minute after a turn ends, so without this you get "finished"
  followed by a redundant "waiting for your input" for the same standstill. It
  still fires when it is the first thing worth saying, and a completion or a
  permission request re-arms it. State: `~/.claude/ntfy-notify.state.json`,
  keyed by `session_id`, pruned after a day.
- stdin is empty or malformed.

**The script never exits non-zero.** A notifier that breaks a session is worse
than one that misses a message. Failures are appended to
`~/.claude/ntfy-notify.log` instead — that log is the first place to look when a
push goes missing, because the silence is by design.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Nothing fires at all | Hook not registered, or session predates the settings file. Check `jq '.hooks.Stop' ~/.claude/settings.json`, then `/hooks` or restart. |
| Log shows `HTTP 403` | A WAF (commonly Cloudflare) rejecting the client. The script sends its own `User-Agent` because the `Python-urllib/3.x` default gets blocked. |
| Log shows `HTTP 401/403` on a private topic | Missing or expired `token`. |
| Publish returns 200, no push on phone | Server-side delivery: check the topic name matches the phone's subscription, and that the app has background notifications enabled. |
| Two pushes for one standstill | The idle-after-completion dedup relies on `~/.claude/ntfy-notify.state.json`. If that path is unwritable the fallback is to notify, so check permissions on it. |
| Too many notifications | Drop the offending `notification_type` from `WAITING_TYPES`, or remove the `Notification` hook entirely to keep only completions. |
| Summary is a fragment | Expected at six words. Raise `SUMMARY_WORDS`, or switch to the `llm` summarizer. |

## Files

- `scripts/ntfy_notify.py` — the notifier. Reads hook JSON on stdin, POSTs to ntfy.
- `evals/payloads.jsonl` — sample Stop/Notification payloads for dry-run testing.
