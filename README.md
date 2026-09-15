# claude-ntfy

*Claude Code finishes. Your phone tells you what happened.*

You give Claude a long task, switch to something else, and then keep checking
back to see if it's done. Or worse — it hit a permission prompt thirty seconds
in and has been sitting there ever since.

This sends a push to your phone at the two moments that actually matter, with a
short summary so you can tell from the lock screen whether it's worth going back.

```
✅  Claude finished · my-api        All three migrations applied to staging
🤚  Claude needs you · my-api       Claude needs your permission to use…
```

The first arrives when a turn ends. The second when Claude is stuck waiting on
you — and it comes in at high priority, because that one is costing you time.

## Getting started

You'll need [Claude Code](https://claude.com/claude-code), Python 3.8+ available
as `python3`, and a ntfy topic — either on [ntfy.sh](https://ntfy.sh) or your own
server. Install the [ntfy app](https://ntfy.sh/docs/subscribe/phone/) and
subscribe to your topic.

Then, in Claude Code:

```
/plugin marketplace add AvAl4nch/claude-ntfy
/plugin install claude-ntfy
```

And tell Claude where to send things:

> use https://ntfy.sh/my-topic as the ntfy server

That's the whole setup. The plugin wires up the hooks itself, so there's no
config file to hand-edit and no paths to get right.

**One thing to know:** nothing gets sent until you set a topic. There's no
default, deliberately — a built-in URL would mean every unconfigured install
publishes to a topic somebody else owns, and ntfy topics are public by default.
That's a quiet way to leak your work to strangers. Until you pick a topic, this
does nothing and notes why in its log.

## Just ask for what you want

The plugin includes a skill, so you don't have to remember any of the settings
below. Say what you want in plain English:

> use https://ntfy.example.com/alerts as the ntfy server

> switch the summaries to the llm one, the truncated ones read badly

> stop notifying me when you just need permission

> my ntfy alerts stopped working

Claude makes the change and dry-runs it before anything real gets sent. The rest
of this README is for when you'd rather do it yourself.

## Settings

Everything lives in `~/.claude/ntfy-notify.json`:

```json
{
  "url": "https://ntfy.sh/your-topic-here",
  "summarizer": "heuristic"
}
```

| What | Config key | Environment variable | Default |
|---|---|---|---|
| Where to send | `url` | `NTFY_CLAUDE_URL` | none — you must set this |
| Access token | `token` | `NTFY_CLAUDE_TOKEN` | none |
| Username / password | `username`, `password` | `NTFY_CLAUDE_USERNAME`, `NTFY_CLAUDE_PASSWORD` | none |
| How credentials travel | `auth_mode` | `NTFY_CLAUDE_AUTH_MODE` | `header` |
| How summaries are written | `summarizer` | `NTFY_CLAUDE_SUMMARIZER` | `heuristic` |

A command-line flag beats an environment variable, which beats the config file.

### If your server needs a password

All of ntfy's auth styles work. Pick whichever your server uses.

An access token:

```json
{ "url": "https://ntfy.example.com/alerts", "token": "tk_AgQdq7mVBoFD37zQ" }
```

A username and password:

```json
{ "url": "https://ntfy.example.com/alerts", "username": "alice", "password": "hunter2" }
```

Both go out as a normal `Authorization` header — `Bearer` for a token, `Basic`
for a username. That also covers the case where the auth isn't ntfy's at all but
a reverse proxy in front of it, since that's the same header.

If you paste a `user:pass` string into the `token` field by mistake, it does the
right thing and sends Basic rather than failing cryptically.

**When credentials seem right but nothing arrives**, some proxies, CDNs and
corporate gateways strip the `Authorization` header before it reaches ntfy. ntfy
has a fallback that carries it in the URL instead:

```json
{ "auth_mode": "query" }
```

You don't have to figure this out yourself. When `--doctor` sees a 401 it retries
the other way and tells you which one worked:

```
publish    HTTP 401 Unauthorized
RETRY OK with auth_mode='query' -- your current mode is not reaching the
server (a proxy is likely stripping it). Set "auth_mode": "query" in ...
```

If both fail, it says that too — which means the credentials themselves are
wrong, not the delivery.

Worth knowing before you reach for it: a credential in the query string can end
up in server access logs and proxy logs, where a header wouldn't. It's still sent
over TLS, so it isn't exposed in transit, but `header` is the better default and
`query` is the fallback for when the header genuinely can't get through.

One thing this can't do for you: if you lock down a topic, **the ntfy app on your
phone needs the same credentials**. Add the server login there as well, or the
app goes quiet in exactly the same undramatic way.

### Picking a summarizer

**`heuristic`** is the default. It strips the markdown, grabs the first real
sentence, throws away filler openers, and cuts to six words. Instant and free.

**`llm`** asks Haiku to write the summary instead. The phrasing is better — you
get whole thoughts rather than sentences cut off mid-stride — but it adds five to
ten seconds and a small token cost to *every single turn*. If the call fails it
quietly falls back to the heuristic.

Same input, both ways:

| Claude said | `heuristic` | `llm` |
|---|---|---|
| "Perfect, I have fixed the failing authentication tests in the login module." | Fixed the failing authentication tests in… | Fixed failing authentication login tests |
| "\| Check \| Result \|… All three migrations applied cleanly against staging." | All three migrations applied cleanly against… | All three migrations applied to staging |

Start with `heuristic`. Switch if the truncation starts bothering you.

The "needs you" notifications always use the heuristic — that text is short and
fixed already, so there's nothing for a model to improve.

## When something isn't working

Start here:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ntfy_notify.py" --doctor
```

It'll tell you which Python it found, what topic you're configured for, which
hooks are actually live, and whether it can reach your server. It publishes at
minimum priority, so running it won't buzz your phone.

This matters more than it sounds, because **the notifier never reports failures
out loud**. If it crashed or exited non-zero it would interrupt your Claude
session, and a notifier that breaks your work is worse than one that misses a
message. So when something goes wrong it exits quietly and writes to
`~/.claude/ntfy-notify.log`. That silence is on purpose — `--doctor` and that log
are how you see behind it.

A few things it commonly catches:

| What you see | What's going on |
|---|---|
| Nothing arrives at all | No topic configured, or the plugin didn't install. `--doctor` says which. |
| `HTTP 403` in the log | A firewall or WAF — Cloudflare especially — rejecting the request. |
| `HTTP 401` on a private topic | Missing, wrong, or stripped credentials. `--doctor` distinguishes the three. |
| Publish says 200, phone stays quiet | It reached the server fine. Check the app is subscribed to that exact topic and allowed to notify in the background. |
| Two pushes for everything | The hooks are registered twice, usually plugin *and* manual. `--doctor` warns about this. |

## How it behaves, and why

**You get one notification per thing that happens.** Claude Code raises an "idle"
event about a minute after a turn ends, which would otherwise mean a "finished"
push immediately followed by a "waiting for your input" push about the same
standstill. The second one is dropped. It still comes through when it's genuinely
the first thing worth telling you, and any real event resets that.

**It ignores the boring events.** Login confirmations and quota messages fire on
their own schedule and aren't worth a buzz, so only the ones needing a human get
through.

**It sends its own User-Agent.** Cloudflare and most WAFs reject Python's default
one, which shows up as a mysterious silent 403. Learned that the hard way.

**It posts via ntfy's JSON API** instead of the header-based one, because
summaries contain em-dashes and other non-ASCII, and HTTP headers can't carry
that cleanly.

No dependencies — standard library only. Read
[`scripts/ntfy_notify.py`](scripts/ntfy_notify.py) if you want the details; it's
one file.

## Installing without the plugin system

<details>
<summary>If you'd rather just run the script</summary>

Clone it:

```bash
git clone https://github.com/AvAl4nch/claude-ntfy.git
```

Add both hooks to `~/.claude/settings.json`, merging into whatever's already
there:

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

Three details worth getting right:

- Use the `args` form rather than one long shell string — it runs the interpreter
  directly, so Windows paths with spaces or backslashes never hit a shell parser.
- Merge carefully. A malformed `settings.json` silently disables *everything* in
  that file, not just this hook, so the damage from a bad edit is wider than you'd
  expect.
- Keep `timeout` above 20 seconds. The `llm` summarizer can take that long, and a
  shorter timeout kills it mid-call — the notification just vanishes.

Set your topic in `~/.claude/ntfy-notify.json` as described above.

To let Claude manage the settings for you, link the skill in too:

```bash
ln -s /abs/path/to/claude-ntfy/skills/ntfy-notify ~/.claude/skills/ntfy-notify
```

On Windows, a junction instead:

```powershell
New-Item -ItemType Junction -Path "$HOME\.claude\skills\ntfy-notify" -Target "<repo>\skills\ntfy-notify"
```

Don't do this *and* install the plugin — both sets of hooks fire, and you'll get
everything twice.

</details>

## Testing changes

Dry-run prints what would be sent without sending it:

```bash
echo '{"hook_event_name":"Stop","cwd":"/p/demo","last_assistant_message":"Fixed the flaky login test."}' \
  | python3 scripts/ntfy_notify.py --dry-run
```

Send a real one:

```bash
python3 scripts/ntfy_notify.py --test
```

`evals/payloads.jsonl` has sample payloads covering prose, markdown tables, code
fences, terse one-word replies, and the cases that should stay silent.

## License

MIT — see [LICENSE](LICENSE).
