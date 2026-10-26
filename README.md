# Ginger Telegram Business bot

Python 3.10+; application and tests use only the Python standard library.
Ollama handles introductions and profile extraction; xAI handles qualified chats.
Ginger is a fictional AI character. Introductory chats remain SFW; the existing
adult age/self-report and employment qualification rules are retained.

## Run

Run these commands from this directory. Keep one bot process per SQLite database;
this is a single-instance worker design, not a distributed queue.

1. Keep your existing `.env`, `system_prompt.txt` (if used), and SQLite database.
   Stop the old bot and back up the SQLite database using SQLite's backup API
   before replacing `business_bot.py`. Schema additions happen automatically.
2. Put required settings in `.env` or the cloud environment's secure settings:
   `TELEGRAM_BOT_TOKEN`, `XAI_API_KEY`, `BOT_ADMIN_USER_ID`, and optionally
   `VA_CHAT_ID` (defaults to the admin). Never put credentials in version control.
   The cloud workspace does not currently have these credentials.
3. Start an accessible Ollama server and pull the model, for example:

   ```sh
   ollama serve
   # In another terminal, once the server is listening:
   ollama pull qwen2.5:3b
   ```

   Ollama is a separate runtime prerequisite, not a Python package. If it runs on
   another machine, set `LOCAL_MODEL_BASE_URL` to its accessible OpenAI-compatible
   `/v1` endpoint. Loopback always refers to the machine running the bot.
   Do not expose an unauthenticated Ollama endpoint publicly.
4. Check the models before connecting Telegram:

   ```sh
   python business_bot.py --test-local-model
   python business_bot.py --test-model
   ```

   The local check needs no Telegram or xAI credentials. The xAI check requires
   an API key and an available `XAI_MODEL`; the original default is retained.
   A successful check prints an answer. Review that it identifies as Ginger.
5. Start polling:

   ```sh
   python business_bot.py
   ```

   Enable reply permissions in Telegram Business and test from another account.
   Set `ENABLE_DIRECT_TEST_MODE=true` only when you want private direct bot chats
   processed too. Direct test mode is off by default. Starting polling removes
   any existing webhook without dropping pending Telegram updates.

For webhook deployment, set an HTTPS `WEBHOOK_URL` and `WEBHOOK_SECRET` (1–256
ASCII letters, digits, `_`, `-`). `PORT` defaults to 10000. Terminate TLS at your
hosting platform and forward to the bot. `/healthz` checks worker-thread liveness;
it does not prove that Telegram or either model is reachable.

## What changed

- Ginger identity is reinforced at the end of every conversational system prompt,
  including custom prompt files and `.env` overrides. Known wrong identities
  such as Nidum AI are removed from prior assistant context. Outgoing replies
  containing a known identity leak receive one corrective model request; if the
  model still gets it wrong, a fixed, honest Ginger introduction is used.
  Profile extraction remains a separate JSON-only task.
- `/start` resumes a persisted stopped chat. `/stop` clears both in-memory and
  stored recent context and cancels queued work. Human-review pauses still need
  `/resume_profile <connection_id:chat_id>` from the configured admin.
- `/forget` clears the live profile, identity, transcript and pending job payloads
  and leaves the bot stopped. A later `/start` resumes a clean chat. It cannot
  retract Telegram history, backup files, or copies already delivered externally.
- Incoming messages and pending/generated replies are persisted before receipt
  acknowledgement. Ordinary waiting/ready replies recover on restart without
  charging a second turn for an already generated reply. Duplicate receipt IDs
  are retained without contents for the most recent 10,000 processed updates.
- Successful delivery, transcript updates and closing handoff are committed
  together. A definite send rejection leaves the chat available. Telegram 429s
  receive at most two retries. Ambiguous sends (timeouts, 5xx, crash during send)
  request human review and are not automatically replayed: Telegram provides no
  exactly-once delivery key. An operator must inspect the chat before resuming.
- Local and xAI requests have separate concurrency allowances. Inbox/job limits,
  bounded message bursts and webhook connection limits apply backpressure.
- Untrusted profile types and malformed extraction JSON are handled. Employment
  updates require a quote actually present in the current user turn.
- A first-reply privacy notice and `/privacy` explain storage and handoffs.
  Recent transcripts are excluded from human handoffs by default. Common tokens,
  emails, passwords and long contact/account numbers are redacted best-effort.
  This is not a comprehensive sensitive-data detector or consent/age verifier.

## Settings added

| Setting | Default | Meaning |
| --- | --- | --- |
| `MAX_PENDING_UPDATES` | `10000` | Maximum unprocessed inbox entries; webhooks return 503 at capacity. |
| `MAX_PENDING_JOBS` | `1000` | Maximum outstanding reply bursts and cached chat histories. |
| `MAX_TURN_CHARS` | `16000` | Maximum characters in one burst; overflowing messages form another burst. |
| `PROFILE_RETENTION_DAYS` | `30` | Clear live chat records after this many inactive days; checked hourly. |
| `HANDOFF_INCLUDE_TRANSCRIPT` | `false` | Opt in to sharing recent conversation text with the handler. |

Existing delay, history, typing, profiling and reply-limit settings remain
available. Default delays are still 60–180 seconds, so a reply is not immediate.
A local SQLite database holds pending payloads and recent context in plaintext;
protect filesystem access and backups. Model-side retention is governed by the
providers. xAI requests retain the existing `store: false` setting.

## Offline validation

```sh
python -m unittest discover -s . -v
```

43 regression tests cover identity retries/fallback, routing, stop/start/forget,
admin access, migration, restart recovery, delivery failures/rate limits,
privacy/retention, webhook overload, a concurrent forget/send race, and a complete
worker flow against local mock HTTP APIs. They do not prove compatibility with
your installed model, account permissions, xAI model availability or Telegram.
No real credentials, live chat messages or external API calls are used by tests.
