# wdgwars-api-tester

Systematic probe of the **[WDGoWars](https://wdgwars.pl/)** HTTP API surface.

Built 2026-05-29 during the mass `/api/*` 404 outage. The point of this tool is
to answer, in one command, the questions that took an hour of curl that day:

- Which endpoints are alive vs returning the styled 404 page?
- Does an unauthenticated `/api/me` return 401 (the expected behavior) or 404
  (route-not-bound)?
- Is `/api/stats` exposing the LiteSpeed admin telemetry leak?
- Did anything change since the last snapshot?

Stdlib-only Python 3. No `pip install`. Single file.

## Quick start

```bash
# Probe apex with all three auth variants (none, garbage, valid)
python3 wdgwars_api_tester.py

# Add www. and api. subdomains
python3 wdgwars_api_tester.py --hosts all

# Machine-readable
python3 wdgwars_api_tester.py --json > snapshot.json

# Just the overall verdict word + exit code (good for shell / CI)
python3 wdgwars_api_tester.py --quiet --variants none,garbage
# → prints `HEALTHY` / `DEGRADED` / `OUTAGE` / `UNREACHABLE`
#   plus optional `+LEAK` or `+SENTINEL-DIVERGED` suffix.

# Poll every 60s, print compact deltas on state change.
# Full table is printed on the recovery moment (first transition into HEALTHY).
python3 wdgwars_api_tester.py --watch 60

# Snapshot once, then diff future runs against it
python3 wdgwars_api_tester.py --baseline baseline.json

# Watch + Telegram self-page on state change (no bridge needed)
export TELEGRAM_BOT_TOKEN=123456:ABC...
export TELEGRAM_CHAT_ID=-1001234567890
python3 wdgwars_api_tester.py --watch 60 --alert-telegram

# Watch + Discord / Slack / n8n / PagerDuty (any webhook URL)
python3 wdgwars_api_tester.py --watch 60 \
   --alert-webhook https://discord.com/api/webhooks/.../...

# Watch + arbitrary shell command on state change
python3 wdgwars_api_tester.py --watch 60 \
   --exec-on-change 'echo "$WDGWARS_PREV_OVERALL → $WDGWARS_OVERALL" | mail -s "wdgwars alert" me@example.com'
```

## API key

Same precedence as [wigle-to-wdgwars](https://github.com/HiroAlleyCat/wigle-to-wdgwars):

1. `--key` CLI flag
2. `$WDGWARS_API_KEY`
3. `~/.config/wigle-to-wdgwars/wdgwars.key`

If no key is found, the `valid` variant is dropped automatically and only the
`none` and `garbage` variants run.

## What it probes

| Probe | Method | Path | Auth | Notes |
|---|---|---|---|---|
| `api-root` | GET | `/api/` | no | Baseline shape of the /api/ subtree. |
| `me` | GET | `/api/me` | yes | Identity. Unauth → 401, not 404. |
| `upload-history` | GET | `/api/upload-history?limit=5` | yes | Added 2026-04-27. |
| `upload-csv` | POST | `/api/upload-csv` | yes | Multipart WiGLE-1.6, mixed Types. |
| `signed-upload` | GET | `/api/upload/` | yes | HMAC JSON endpoint. GET → 405 if healthy. |
| `health-asked-for` | GET | `/api/health` | no | Doesn't exist yet. Asked for in bug #1. |
| `stats-leak-check` | GET | `/api/stats` | no | 200 here = LiteSpeed admin leak. |
| `api-sentinel-404-a/b/c` | GET | `/api/<random>` × 3 | no | Quorum fingerprint of the /api/ 404 page (2-of-3 majority required). |
| `non-api-sentinel-404` | GET | `/<random>` | no | Fingerprints the non-/api/ 404 page. |
| `changelog-control` | GET | `/changelog` | no | Public-page reachability control. |

## Verdicts

| Verdict | Meaning |
|---|---|
| `OK` | 2xx response, body distinct from any 404 sentinel. |
| `AUTH-REQUIRED` | 401. Endpoint is alive and rejecting the key. |
| `DEAD` | Body hash matches the /api/ 404 quorum sentinel. Route not bound. |
| `DEAD-NONAPI` | Body matches the non-/api/ 404 sentinel. |
| `LEAK` | `/api/stats` returned 200 → LiteSpeed admin telemetry exposed. |
| `BLOCKED` | `/api/stats` returned non-200. Desired state for that endpoint, regardless of which 404 handler served it. |
| `404` | 404 response but body distinct from sentinels. |
| `METHOD` | 405. Healthy endpoint, wrong verb. |
| `ERROR` | Network/timeout/URL error. |
| `SENTINEL` | One of the 3 /api/ quorum sentinels, in agreement with the majority. |
| `SENTINEL-OUTLIER` | The 1 of 3 sentinels that disagreed with the other 2 (CDN cache slip, e.g.). DEAD detection still works via the 2-vote majority. |
| `SENTINEL-DIVERGED` | All 3 sentinels returned distinct bodies. DEAD detection disabled for that host. Investigate the diagnostic before trusting results. |
| `SENTINEL-NONAPI` | The non-/api/ 404 fingerprint probe. |

The overall summary is one of:

- `HEALTHY` — no DEAD, no ERROR, no LEAK.
- `UNREACHABLE` — everything errored. DNS, no internet, host down.
- `DEGRADED` — at least one probe DEAD.
- `OUTAGE` — `/api/me` with a valid key is DEAD. Whole API surface is down.
- `…+LEAK` — appended to any of the above when `/api/stats` is exposed.
- `…+SENTINEL-DIVERGED` — appended when the 3 quorum sentinels couldn't agree on a fingerprint. DEAD detection is disabled for affected hosts; investigate before trusting results.

Exit code is `1` for DEGRADED/OUTAGE/UNREACHABLE/LEAK/SENTINEL-DIVERGED and `0` for HEALTHY.

## Running on a schedule

Drop it in cron, a systemd timer, or Windows Task Scheduler. Pair `--baseline`
with `--json` to log every snapshot for later trend analysis, or use `--watch`
on a long-running host to get a single state-change notification when the API
comes back up.

Cron example:

```cron
*/5 * * * * cd /opt/wdgwars-api-tester && \
  python3 wdgwars_api_tester.py --baseline /var/log/wdgwars/baseline.json \
                                --json >> /var/log/wdgwars/snapshots.jsonl
```

## Notification channels

`--watch` mode supports three independent notification paths. Use one, two, or all three at once — they don't conflict.

| Flag | Use when |
|---|---|
| `--alert-telegram` | You have a Telegram bot + chat. Easiest setup. |
| `--alert-webhook URL` | You're on Discord, Slack, n8n, PagerDuty, or any service that takes a JSON POST. |
| `--exec-on-change CMD` | None of the above fit — email, SMS, a Lambda, write to a database, pipe to logger. |

Failure in any one path logs a warning to stderr but never crashes the watch loop or blocks the others.

### Telegram self-paging

In `--watch` mode the tool can post directly to a Telegram chat on every state change. No external broker, webhook service, or alerting infrastructure required — stdlib `urllib` to the Bot API and a chat id.

### Setup

1. Talk to [@BotFather](https://t.me/BotFather) on Telegram and create a bot. Copy the token.
2. Add the bot to the chat where you want alerts (DM, group, or channel).
3. Send any message in that chat, then `GET https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[0].message.chat.id` (or `result[0].channel_post.chat.id` for channels).
4. Export both values and pass `--alert-telegram`:

```bash
export TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
export TELEGRAM_CHAT_ID=-1001234567890
python3 wdgwars_api_tester.py --watch 60 --alert-telegram
```

Or pass them inline: `--telegram-bot-token <token> --telegram-chat-id <id>`.

### Message format

| Transition | Header |
|---|---|
| Recovery (`* → HEALTHY`) | `✅ wdgwars API recovered` |
| Diagnostic broken (`+SENTINEL-DIVERGED` appears) | `🔧 wdgwars-api-tester diagnostic broken` |
| Regression (anything else worse) | `🚨 wdgwars API <new-overall>` |

Body includes the `prev_overall → curr_overall` transition, per-probe deltas (capped at 30 lines for Telegram's 4096-char message limit), and a verdict count rollup. Uses HTML parse mode so `<b>` / `<code>` render correctly.

### Generic webhook (`--alert-webhook URL`)

POSTs a JSON payload to any HTTP endpoint on state change. The payload carries multiple top-level keys so the same URL works for several services without per-service flags:

```json
{
  "text": "🚨 wdgwars-api-tester: HEALTHY → OUTAGE+LEAK\n\n<deltas>\n\nverdicts: DEAD=10, LEAK=1",
  "content": "<same as text — Discord reads this>",
  "title": "🚨 wdgwars-api-tester: HEALTHY → OUTAGE+LEAK",
  "kind": "regression",
  "overall": "OUTAGE+LEAK",
  "prev_overall": "HEALTHY",
  "deltas": ["wdgwars.pl me/valid  OK/200 -> DEAD/404", "..."],
  "by_verdict": {"DEAD": 10, "LEAK": 1, "OK": 1},
  "tool": "wdgwars-api-tester",
  "version": "0.4.0"
}
```

- **Discord** reads `content`. Drop in any channel webhook URL.
- **Slack incoming webhooks** read `text`. Same drop-in.
- **n8n / Zapier / Make** can pick the structured fields directly.
- **PagerDuty Events v2** — wrap with `--exec-on-change` (it expects a different envelope).
- **Custom HTTP handlers** — read whatever they need from the structured fields.

### Arbitrary command (`--exec-on-change CMD`)

Runs any shell command on state change. The following env vars are exported into the subprocess:

| Env var | Value |
|---|---|
| `WDGWARS_OVERALL` | New overall verdict, e.g. `DEGRADED+LEAK` |
| `WDGWARS_PREV_OVERALL` | Previous overall verdict |
| `WDGWARS_KIND` | `recovery` / `regression` / `diagnostic-broken` |
| `WDGWARS_RECOVERY` | `1` if transitioning into HEALTHY, else `0` |
| `WDGWARS_DELTAS` | Newline-joined per-probe delta lines |
| `WDGWARS_VERDICTS` | JSON-encoded `{verdict: count}` dict |

Examples:

```bash
# Email on every transition
--exec-on-change 'echo "$WDGWARS_DELTAS" | mail -s "wdgwars: $WDGWARS_OVERALL" me@example.com'

# Only page on regression (not recovery, not diagnostic)
--exec-on-change '[ "$WDGWARS_KIND" = "regression" ] && /usr/local/bin/page-me.sh "$WDGWARS_OVERALL"'

# Forward to an existing internal alerting service
--exec-on-change 'curl -X POST -H "Authorization: Bearer $MY_TOKEN" \
                  -d "{\"summary\":\"$WDGWARS_OVERALL\",\"verdicts\":$WDGWARS_VERDICTS}" \
                  https://internal.example.com/alert'
```

The command runs with `shell=True` and a 15-second timeout. Non-zero exit codes log a warning but don't crash the watch loop.

## Adapting the tool for your own service

Single-file, MIT, stdlib only — fork is encouraged. The structure is designed to make these changes easy:

- **Probe a different API.** Edit `build_probes()` to swap the endpoints, methods, and expected statuses. `DEFAULT_HOSTS` / `ALL_HOSTS` at the top change which hosts get probed.
- **Add new probes.** Append `Probe(...)` entries to `build_probes()`. Each gets the same auth-variant matrix and verdict annotation automatically.
- **Add a new verdict.** Edit `annotate_verdicts()` to add a branch, then add the verdict to `VERDICT_PRIORITY` so the table sort works, and `summary()` so it rolls up into the overall verdict if relevant.
- **Customize the sentinel mechanism.** `SENTINEL_PROBES` and `_canonical_sentinel()` define the quorum logic. Change `SENTINEL_PROBES` to use more sentinels, or rewrite `_canonical_sentinel()` to use a different agreement rule.
- **Different notification format.** Edit `_format_telegram_text()` or `_format_webhook_payload()` directly. Both are pure functions, easy to unit-test.

If you ship a fork, MIT means clone-and-rename is fine — no need to credit upstream.

## Tests

Two suites, both stdlib only.

### Unit tests (offline, fast)

```
python3 -m unittest test_wdgwars_api_tester
```

32 tests, no network. Covers verdict annotation, quorum sentinel logic, state signature stability, summary rollup, probe delta detection, Telegram message formatting, and webhook payload shape. Runs in under a second.

### Integration tests (live API + local mock HTTP)

```
python3 integration_test.py
```

16 end-to-end scenarios. Hits the real `wdgwars.pl` host (same as the tool itself) and spins up a local HTTP server on a random port to capture webhook POSTs. Covers:

- `--version`, `--help`, default one-shot, `--quiet`, `--json`, `--no-table`
- Invalid `--variants` rejection
- `--valid` variant drop on missing key (with HOME / USERPROFILE override so the config-file fallback can't pollute the test)
- `--baseline` first-run file creation + second-run diff detection
- All three notification guard rails (`--alert-telegram` / `--alert-webhook` / `--exec-on-change` without `--watch` warn and disable)
- Watch-mode credential check (`--alert-telegram` + `--watch` without env vars warns at startup)
- **End-to-end notification dispatch:** `_format_webhook_payload()` + `_post_webhook()` against a local mock receiver, with payload assertions (Slack `text`, Discord `content`, structured fields). `_exec_on_change()` against a cross-platform Python helper that captures all `WDGWARS_*` env vars to a sink file.
- Live JSON schema check: every probe documented in this README must appear in the snapshot, the 3-sentinel quorum produces ≤2 distinct hashes in non-diverged states.

Takes about 80 seconds (most of it real HTTP latency to wdgwars.pl). Exit 0 = all green.

## Related

- [wigle-to-wdgwars](https://github.com/HiroAlleyCat/wigle-to-wdgwars) — WiFi/BLE CSV uploader.
- [adsb-to-wdgwars (Muninn)](https://github.com/HiroAlleyCat/adsb-to-wdgwars) — ADS-B uploader.

## License

MIT.
