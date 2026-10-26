# Ginger Telegram Business bot

Python 3.10+; application and tests use only the Python standard library.
Ollama uses the custom `ginger` model for introductions and a separate
`qwen2.5:3b` model for JSON profile extraction. xAI handles chats after an exact
adult age is self-reported. Ginger is a fictional 21-year-old British AI character
from Manchester. Introductory chats remain SFW. Name, location, interests and
employment are optional and never determine eligibility.

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
   ollama pull dolphin-mistral
   ollama pull qwen2.5:3b
   ollama create ginger -f Modelfile
   ```

   Ollama is a separate runtime prerequisite, not a Python package. If it runs on
   another machine, set `LOCAL_MODEL_BASE_URL` to its accessible OpenAI-compatible
   `/v1` endpoint. Loopback always refers to the machine running the bot.
   Do not expose an unauthenticated Ollama endpoint publicly. Use
   `LOCAL_MODEL_NAME=ginger` and `PROFILE_MODEL_NAME=qwen2.5:3b`; both models
   must be present in that Ollama instance.
4. Check the models before connecting Telegram:

   ```sh
   python business_bot.py --test-local-model
   python business_bot.py --test-profile-model
   python business_bot.py --test-model
   ```

   The two local checks need no Telegram or xAI credentials. The profile check
   verifies that the extraction model returns the expected name and numeric age. The xAI check requires
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
  model still gets it wrong, a configured Ginger fallback is used.
  Profile extraction remains a separate JSON-only task.
- Only the configured admin may use commands, and only in a private bot chat.
  Client commands are silently ignored. Private admin controls work even when
  direct test mode is disabled. `/start` is silent and resumes a persisted stopped chat. `/stop` clears both in-memory and
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
- Untrusted profile types and malformed extraction JSON are handled by a
  separately configured JSON model. Optional employment notes require a quote
  actually present in the current user turn; they do not qualify or exclude users.
- An exact self-reported age from 18 through 120 is the only profile requirement
  for routing to Grok. A boolean adult flag alone does not qualify. Underage
  reports and the existing unknown-age reply limit still pause for review.
- Stable preferences and boundaries survive restarts and support corrections.
  Upcoming plans/events have individual confirmation timestamps and expire from
  usable profile memory after seven days unless explicitly reconfirmed. Unrelated
  updates do not refresh them. Undated legacy plans are not assumed current.
- Privacy details are sent only when someone requests `/privacy`; normal replies
  and `/start` do not include an automatic notice. Ready replies saved by older
  versions should be checked before upgrading if they still contain notices.
  Recent transcripts are excluded from human handoffs by default. Common tokens,
  emails, passwords and long contact/account numbers are redacted best-effort.
  This is not a comprehensive sensitive-data detector or consent/age verifier.

## Settings added

| Setting | Default | Meaning |
| --- | --- | --- |
| `PROFILE_MODEL_NAME` | `qwen2.5:3b` | Local model used only for JSON extraction. |
| `PLAN_MEMORY_DAYS` | `7` | Maximum time since each plan/event was explicitly confirmed. |
| `MAX_PENDING_UPDATES` | `10000` | Maximum unprocessed inbox entries; webhooks return 503 at capacity. |
| `MAX_PENDING_JOBS` | `1000` | Maximum outstanding reply bursts and cached chat histories. |
| `MAX_TURN_CHARS` | `16000` | Maximum characters in one burst; overflowing messages form another burst. |
| `PROFILE_RETENTION_DAYS` | `30` | Clear live chat records after this many inactive days; checked hourly. |
| `HANDOFF_INCLUDE_TRANSCRIPT` | `false` | Opt in to sharing recent conversation text with the handler. |

Existing delay, history, typing, profiling and reply-limit settings remain
available. Default delays are now 10–25 seconds (sequence mode: 10, 15, 25).
Existing `.env` values still override the new defaults; update them explicitly.
The Grok reply limit and human handoff remain unchanged. Previously paused
chats are not automatically resumed; the configured admin can resume them.
A local SQLite database holds pending payloads and recent context in plaintext;
protect filesystem access and backups. Model-side retention is governed by the
providers. xAI requests retain the existing `store: false` setting.

## Offline validation

```sh
python -m unittest discover -s . -v
```

93 regression tests cover identity retries/fallback, routing, stop/start/forget,
admin access, migration, restart recovery, delivery failures/rate limits,
privacy/retention, webhook overload, a concurrent forget/send race, and a complete
worker flow against local mock HTTP APIs. They do not prove compatibility with
your installed model, account permissions, xAI model availability or Telegram.
No real credentials, live chat messages or external API calls are used by tests.
The suite also covers separate model routing, optional personal fields, underage
handling, temporary-memory expiry/reconfirmation, and the profile diagnostic.

## Shared personality prompt

`system_prompt.txt` contains the conversational prompt used by both Ollama and
Grok. `ROLEPLAY_CHARACTER` supplies the same Ginger identity to both providers;
profile extraction never receives the character prompt.

If your current `.env` contains `SYSTEM_PROMPT` with employment qualification or
instructions to chase missing details, remove that line to use the supplied
`system_prompt.txt`, or replace its contents with the updated prompt. A nonempty
`SYSTEM_PROMPT` always overrides the file. The `Modelfile` changes Ollama only;
rebuild it with `ollama create ginger -f Modelfile` after changing its contents.

For Windows PowerShell, run from the extracted bot folder:

```powershell
ollama pull dolphin-mistral
ollama pull qwen2.5:3b
ollama create ginger -f Modelfile
# For a new installation only; keep existing credentials when upgrading:
# Copy-Item .env.example .env
python business_bot.py --test-local-model
python business_bot.py --test-profile-model
python business_bot.py --test-model
python business_bot.py
```

Ollama is installed separately. Keep the application directory on a persistent
volume when deploying. None of the offline tests substitute for live model
quality evaluation: try introductions, corrections, refusals, British phrasing
and provider switches against your installed models before activating Telegram.

## Correction of the uploaded Python file

The Python prompt sections are preserved exactly from the supplied pasted file;
`prompt_checksums.json` records their source hashes and a regression test checks
those hashes. Existing external `.env`, `system_prompt.txt` and `Modelfile`
contents were not edited in this correction. Keep your own versions when upgrading.

The missing generated-answer persistence block is restored, including the
cancellation check, turn reservation and durable ready status. Private admin
controls are accepted with direct test mode disabled; `/start` remains silent.
Free-form employment notes requested by the supplied extraction prompt are
accepted and still require evidence from the current user message. Prompt text
and extraction sampling settings are unchanged. Validation uses mocked APIs;
live model/Telegram integration has not been tested with your credentials.

## Llama profiling → profile-only Grok handoff

For a llama.cpp server started with `--alias ginger --port 8080`, set:

```dotenv
LOCAL_MODEL_BASE_URL=http://127.0.0.1:8080/v1
LOCAL_MODEL_NAME=ginger
PROFILE_MODEL_NAME=ginger
GROK_SYSTEM_PROMPT_FILE=grok_prompt.txt
```

The earlier Ollama instructions remain an alternative; a single llama.cpp server
uses the same loaded Llama model for local replies and JSON extraction. Set an
available `XAI_MODEL` and your `XAI_API_KEY` for Grok.

The data flow is:

1. Llama handles introductions and extracts the profile locally.
2. When an exact adult age is established, Grok's first request contains the
   validated profile plus shared character instructions and `grok_prompt.txt`.
   It does not contain the profiling transcript, Llama's prior replies, or even
   the final raw message that completed the profile.
3. Subsequent Grok requests contain the profile, new client messages, and Grok's
   own post-handoff chat history only. Grok needs those new messages to respond.

Grok history is stored separately in SQLite (`grok_history`). Migration never
copies the old combined transcript into it. `/stop`, `/forget` and admin resume
clear this separate context as appropriate. Pending handoffs retain their context
boundary on restart. Changes cannot retract any requests made by older versions.

Edit `grok_prompt.txt` for additional instructions that only Grok receives.
`GROK_SYSTEM_PROMPT` can override that file via `.env`, using `\n` for newlines.
The existing shared character prompt and the Llama extraction prompt are kept
unchanged. The separate file adds profile-handoff and conversation instructions
without forwarding Llama chat content. Profile fields themselves may contain
user-supplied facts or short employment evidence quotes; those are profile data.

Tests verify the actual constructed xAI request, identity-retry requests,
separate post-handoff memory, restart recovery and deletion of Grok context.
No live xAI or Telegram calls were made during validation.

## Identity warning

The identity check retries known model self-introductions such as "I'm Nidum AI"
once, then uses the configured Ginger fallback if needed. Ordinary discussion
of model names no longer triggers that retry or removes assistant history.
Warnings now identify the selected provider and model without logging chat text.
A warning alone does not establish which underlying model behavior caused it;
check whether the final delivered reply uses Ginger. All existing prompts remain
unchanged by this identity-check correction.

## Profile-extraction failures on local servers

`Profile extraction unavailable` means the local request failed and the stored
profile was retained. It does not by itself identify the cause. Warnings now
include the requested profile model, timeout and a safe upstream error category;
arbitrary response bodies and chat text are not logged. Recognized context
errors include numeric `n_ctx`/`n_prompt_tokens` when supplied by the server.

Run `python business_bot.py --test-profile-model` from the bot folder to isolate
the extraction request without Telegram. For the single llama.cpp server with
`--alias ginger --port 8080`, verify:

```dotenv
LOCAL_MODEL_BASE_URL=http://127.0.0.1:8080/v1
LOCAL_MODEL_NAME=ginger
PROFILE_MODEL_NAME=ginger
```

- Connection refused: start the server and check the configured port.
- Model unavailable: match `PROFILE_MODEL_NAME` to the loaded server's alias.
- Timeout: a partially CPU-offloaded 7B model may need more time. Set
  `LOCAL_MODEL_TIMEOUT_SECONDS=600` if the log confirms timeouts, then restart
  the bot. This increases waiting time, not generation speed.
- Context overflow: extraction instructions, profile, history and generated
  output share the model context. 2048 tokens may be insufficient. Previous
  messages are now bounded; an explicit context overflow triggers one retry
  without old dialogue. The current profile, latest input and instructions are
  preserved. If that still exceeds the limit, increase the server context (for
  example to 4096) and reduce GPU-offloaded layers if needed for available VRAM.

A simple diagnostic can pass while long live conversations still exceed the
context limit. Check the actual warning and server error before choosing a fix.
Prompt text and extraction sampling settings are unchanged.
