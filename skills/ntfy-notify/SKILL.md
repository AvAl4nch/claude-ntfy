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

The plugin is the normal path. It registers the hooks itself, so there is no
`settings.json` editing and no absolute paths to get wrong:

```
/plugin marketplace add AvAl4nch/claude-ntfy
/plugin install claude-ntfy
```

Then set the topic (see Configure). Nothing is sent until one is set.

### Manual install, without the plugin system

Only for someone who wants the script alone. Register both hooks in
`~/.claude/settings.json`, merging into whatever is already there:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ {
          "type": "command", "command": "python3",
          "args": ["<abs-path>/scripts/ntfy_notify.py"],
          "async": true, "timeout": 30
      } ] }
    ],
    "Notification": [
      { "hooks": [ {
          "type": "command", "command": "python3",
          "args": ["<abs-path>/scripts/ntfy_notify.py"],
          "async": true, "timeout": 30
      } ] }
    ]
  }
}
```

Three things to get right, each of which has bitten this setup:

- **Use the `args` exec form**, not one shell string. It spawns the interpreter
  directly, so Windows paths with spaces or backslashes never reach a parser.
- **Merge, never overwrite.** A malformed `settings.json` silently disables every
  setting in that file, not just the hook -- so the blast radius of a bad edit is
  much larger than it looks.
- **`timeout` must clear the summarizer.** The `llm` summarizer can take 20s; a
  shorter timeout kills it mid-call and the notification is lost silently.

Settings are watched live, but a session that started before the file existed may
not pick them up. If nothing fires, have the user open `/hooks` once or restart.
You cannot open `/hooks` yourself -- it ends the turn.

Registering both ways is redundant but no longer harmful: the two processes race
for an atomic lock and only one sends. `--doctor` points it out.

## Configure

Resolution order: CLI flag → env var → `~/.claude/ntfy-notify.json` → built-in default.

| Setting | Env var | Config key | Default |
|---|---|---|---|
| Topic URL | `NTFY_CLAUDE_URL` | `url` | *(none -- must be set)* |
| Access token | `NTFY_CLAUDE_TOKEN` | `token` | none (anonymous) |
| Username / password | `NTFY_CLAUDE_USERNAME` / `NTFY_CLAUDE_PASSWORD` | `username` / `password` | none |
| Credential transport | `NTFY_CLAUDE_AUTH_MODE` | `auth_mode` | `header` (or `query`) |
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

Start with the self-check -- it reports interpreter, config, which hooks are
actually live, and whether the server is reachable, and it publishes at minimum
priority so it will not buzz the user's phone:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ntfy_notify.py" --doctor
```

Always dry-run before sending, so testing never spams the user's phone:

```bash
echo '{"hook_event_name":"Stop","cwd":"/p/demo","last_assistant_message":"Fixed the flaky login test."}' \
  | python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ntfy_notify.py" --dry-run
```

Then one real send:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ntfy_notify.py" --test
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

## Authentication

ntfy accepts a Bearer access token or Basic username/password, and the script
supports both, plus either transport:

- `token` -> `Authorization: Bearer <token>`
- `username` + `password` -> `Authorization: Basic <base64>`
- a `user:pass` string mistakenly put in `token` is detected and sent as Basic
- `auth_mode: "query"` moves the credential into ntfy's `?auth=` parameter, for
  proxies that strip the `Authorization` header. Prefer `header`: a query-string
  credential can land in server and proxy access logs, where a header would not.
  Suggest `query` only once `--doctor` shows the header is not getting through.

Do not guess which is wrong when a user reports silence -- `--doctor` retries the
opposite transport and distinguishes "credentials rejected" from "credentials
never arrived". Remind the user that a locked-down topic also needs the
credentials entered in their phone's ntfy app.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Config is right but nothing arrives | Hooks load at session START. A session already running when the plugin was installed has none of them -- the user must restart. Check `--doctor`'s "last real send": if it predates the install, this is it. |
| Nothing fires at all | Hook not registered, or session predates the settings file. Check `jq '.hooks.Stop' ~/.claude/settings.json`, then `/hooks` or restart. |
| Log shows `HTTP 403` | A WAF (commonly Cloudflare) rejecting the client. The script sends its own `User-Agent` because the `Python-urllib/3.x` default gets blocked. |
| Log shows `HTTP 401/403` on a private topic | Run `--doctor`: it retries the other `auth_mode` and reports whether the credentials are wrong, absent, or being stripped in transit by a proxy. |
| Publish returns 200, no push on phone | Server-side delivery: check the topic name matches the phone's subscription, and that the app has background notifications enabled. |
| Two pushes for one standstill | The idle-after-completion dedup relies on `~/.claude/ntfy-notify.state.json`. If that path is unwritable the fallback is to notify, so check permissions on it. |
| Too many notifications | Drop the offending `notification_type` from `WAITING_TYPES`, or remove the `Notification` hook entirely to keep only completions. |
| Summary is a fragment | Expected at six words. Raise `SUMMARY_WORDS`, or switch to the `llm` summarizer. |

## Files

- `scripts/ntfy_notify.py` — the notifier. Reads hook JSON on stdin, POSTs to ntfy.
- `evals/payloads.jsonl` — sample Stop/Notification payloads for dry-run testing.
