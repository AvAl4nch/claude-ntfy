# ntfy-notify

Push a [ntfy](https://ntfy.sh) notification to your phone when Claude Code
finishes a turn or gets blocked waiting on you — with a short summary of what
actually happened, so it's readable on a lock screen.

```
✅  Claude finished · my-api          All three migrations applied to staging
🤚  Claude needs you · my-api         Claude needs your permission to use…
```

| Moment | Hook event | Priority |
|---|---|---|
| Claude finished its turn | `Stop` | 3 (default) |
| Claude is blocked on you | `Notification` | 4 (high) |

## Why hooks, not a skill

Claude can't reliably notify you that it stopped, because by the time it stops
it is no longer running. Claude Code's harness fires `Stop` and `Notification`
hooks, and `scripts/ntfy_notify.py` turns each hook payload into a push.

The bundled `SKILL.md` exists so Claude can install, configure, and troubleshoot
the thing on request — but the hooks are what actually do the work.

## Requirements

- Claude Code
- Python 3.8+ (standard library only — no dependencies)
- A ntfy topic, either on [ntfy.sh](https://ntfy.sh) or a self-hosted server

## Install

```bash
git clone https://github.com/<you>/claude-ntfy.git
```

Point the two hooks at the script in `~/.claude/settings.json`, merging with
whatever is already there:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ {
          "type": "command", "command": "python",
          "args": ["/abs/path/to/ntfy-notify/scripts/ntfy_notify.py"],
          "async": true, "timeout": 30
      } ] }
    ],
    "Notification": [
      { "hooks": [ {
          "type": "command", "command": "python",
          "args": ["/abs/path/to/ntfy-notify/scripts/ntfy_notify.py"],
          "async": true, "timeout": 30
      } ] }
    ]
  }
}
```

The `args` exec form spawns the interpreter directly, so Windows paths with
spaces or backslashes never reach a shell parser. `async: true` keeps the push
off the critical path — your turn ends immediately and the notification follows.

Then set your topic in `~/.claude/ntfy-notify.json`:

```json
{ "url": "https://ntfy.sh/your-topic-here", "summarizer": "heuristic" }
```

**There is no default topic on purpose.** A baked-in URL would mean an
unconfigured install silently publishes to a topic someone else owns — and ntfy
topics are public by default, so that would leak your work summaries to
strangers. Unconfigured, the script sends nothing and says so in its log.

To drop the skill into Claude Code so it can manage itself, symlink it:

```bash
ln -s /abs/path/to/ntfy-notify ~/.claude/skills/ntfy-notify
# Windows: New-Item -ItemType Junction -Path "$HOME\.claude\skills\ntfy-notify" -Target <repo>
```

## Just tell Claude

Once the skill is linked into `~/.claude/skills/`, you don't have to edit any of
this by hand. Claude reads `SKILL.md` and makes the change for you — say what you
want in plain language:

> use https://ntfy.example.com/alerts as the ntfy server

> switch the summaries to the llm one, the truncated ones read badly

> my ntfy alerts stopped working

> stop notifying me when you just need permission

> the summaries are too short, make them longer

Claude edits `~/.claude/ntfy-notify.json`, or the hook registration in
`~/.claude/settings.json`, and verifies the change with a dry-run before sending
anything real. The sections below document what it is actually changing, for when
you'd rather do it yourself.

## Configuration

Resolution order: CLI flag → environment variable → config file → built-in default.

| Setting | Env var | Config key | Default |
|---|---|---|---|
| Topic URL | `NTFY_CLAUDE_URL` | `url` | *(none — must be set)* |
| Bearer token | `NTFY_CLAUDE_TOKEN` | `token` | none (public topic) |
| Summarizer | `NTFY_CLAUDE_SUMMARIZER` | `summarizer` | `heuristic` |

### Summarizers

**`heuristic`** (default) strips markdown, takes the first prose sentence, drops
filler openers, and truncates to six words. Instant, free, no network call.

**`llm`** shells out to `claude -p` with Haiku. Phrasing is noticeably better —
complete thoughts instead of truncations — but it adds ~5–10s and a token cost
to **every turn**, and falls back to the heuristic if the call fails.

| Input | `heuristic` | `llm` |
|---|---|---|
| "Perfect, I have fixed the failing authentication tests in the login module." | Fixed the failing authentication tests in… | Fixed failing authentication login tests |
| "\| Check \| Result \|…All three migrations applied cleanly against staging." | All three migrations applied cleanly against… | All three migrations applied to staging |

`Notification` always uses the heuristic — its text is already short and fixed,
so there's nothing for a model to improve.

## Testing

Dry-run, so testing never spams your phone:

```bash
echo '{"hook_event_name":"Stop","cwd":"/p/demo","last_assistant_message":"Fixed the flaky login test."}' \
  | python scripts/ntfy_notify.py --dry-run
```

One real send:

```bash
python scripts/ntfy_notify.py --test
```

`evals/payloads.jsonl` holds sample payloads covering prose, markdown tables,
code fences, terse replies, and the cases that should stay silent.

## Design notes

**It never exits non-zero.** A notifier that breaks your session is worse than
one that misses a message, so every failure path exits 0 and appends to
`~/.claude/ntfy-notify.log`. That log is the first place to look when a push goes
missing — the silence is deliberate.

**One buzz per event.** Claude Code fires an `idle_prompt` notification a minute
after a turn ends, which would mean a "finished" push followed by a redundant
"waiting for your input" push for the same standstill. The idle nag is dropped
whenever the session has already notified you; it still fires when it's the first
thing worth saying, and any real event — a completion, a permission request —
re-arms it. Per-session state lives in `~/.claude/ntfy-notify.state.json` and is
pruned after a day.

**It stays quiet** when `stop_hook_active` is set (a stop hook triggering a Stop
would double-send), when the notification type isn't one a human needs to act on
(`auth_success`, `quota_*`), and when stdin is empty or malformed.

**It sends its own `User-Agent`.** Cloudflare and most WAFs reject the default
`Python-urllib/3.x`, which shows up as a silent 403.

**It posts via ntfy's JSON API** rather than the header API, because titles carry
non-ASCII and HTTP headers are latin-1.

## License

MIT — see [LICENSE](LICENSE).
