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

The bundled skill (`skills/ntfy-notify/`) exists so Claude can install, configure, and troubleshoot
the thing on request — but the hooks are what actually do the work.

## Requirements

- Claude Code
- Python 3.8+ on PATH as `python3` (standard library only — no dependencies)
- A ntfy topic, either on [ntfy.sh](https://ntfy.sh) or a self-hosted server

## Install

```
/plugin marketplace add AvAl4nch/claude-ntfy
/plugin install claude-ntfy
```

Then tell Claude your topic:

> use https://ntfy.sh/my-topic as the ntfy server

That's it. The plugin registers the `Stop` and `Notification` hooks itself, so
there is no `settings.json` to edit and no absolute path to get wrong.

**Nothing is sent until you set a topic.** There is no default on purpose: a
baked-in URL would mean an unconfigured install publishes to a topic someone else
owns, and ntfy topics are public by default, so that would leak your work
summaries to strangers. Unconfigured, the script sends nothing and says so in its
log.

Check it worked at any time:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ntfy_notify.py" --doctor
```

That reports your interpreter, your config, which hooks are actually live, and
whether the server is reachable. It publishes at minimum priority, so it won't
buzz your phone.

<details>
<summary>Manual install, without the plugin system</summary>

For using the script on its own. Clone it, then merge both hooks into
`~/.claude/settings.json`:

```bash
git clone https://github.com/AvAl4nch/claude-ntfy.git
```

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ {
          "type": "command", "command": "python3",
          "args": ["/abs/path/to/claude-ntfy/scripts/ntfy_notify.py"],
          "async": true, "timeout": 30
      } ] }
    ],
    "Notification": [
      { "hooks": [ {
          "type": "command", "command": "python3",
          "args": ["/abs/path/to/claude-ntfy/scripts/ntfy_notify.py"],
          "async": true, "timeout": 30
      } ] }
    ]
  }
}
```

The `args` exec form spawns the interpreter directly, so Windows paths with
spaces or backslashes never reach a shell parser. `async: true` keeps the push
off the critical path. Merge carefully - a malformed `settings.json` silently
disables every setting in that file, not just the hook.

Set your topic in `~/.claude/ntfy-notify.json`:

```json
{ "url": "https://ntfy.sh/your-topic-here", "summarizer": "heuristic" }
```

To let Claude manage the thing for you, link the skill in as well:

```bash
ln -s /abs/path/to/claude-ntfy/skills/ntfy-notify ~/.claude/skills/ntfy-notify
```

On Windows use a junction instead:

```powershell
New-Item -ItemType Junction -Path "$HOME\.claude\skills\ntfy-notify" -Target "<repo>\skills\ntfy-notify"
```

Don't do both - plugin hooks and manual hooks both fire, so every event pushes
twice. `--doctor` detects that.

</details>

## Just tell Claude

Installing the plugin also installs the skill, so you don't have to edit any of
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
  | python3 scripts/ntfy_notify.py --dry-run
```

One real send:

```bash
python3 scripts/ntfy_notify.py --test
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
