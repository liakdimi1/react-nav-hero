# Local Ginger Telegram Business bot

One local **Ginger** model handles every chat reply and profile extraction.
The same OpenAI-compatible local endpoint and model alias are used for both
jobs. Python 3.10+ standard library only; no pip dependencies.

The private Telegram admin menu manages collected client profiles and observed
Telegram identities. Existing personality and extraction prompt text is retained
from the supplied Python file. Admin notes are kept separate from model input.

## Upgrade an existing bot

1. Stop the old Python bot. Keep the local model server running.
2. Keep your `.env`, SQLite database (including any WAL files), and your own
   `system_prompt.txt` / `Modelfile`. Replace `business_bot.py` with this version.
3. Update `.env` with the local settings below. Old cloud-model credentials,
   provider prompt files, separate profile-model settings and reply-limit settings
   are unused and can be removed. The old provider prompt file is not shipped.
4. On the first schema upgrade, the bot creates a SQLite backup beside the
   original database named `*.pre-local-menu-*.sqlite3`. Migration retains profiles,
   identities, supported conversation state and context. Retired conversation
   columns are removed from the live table. Backups retain the original data.
5. Prepared replies produced by a removed provider are cancelled rather than
   delivered. Local prepared replies can recover without regenerating. Replies
   interrupted during sending are marked for review to avoid duplicates.
6. Run the local model tests, then start the bot. Send `/admin` privately to it.

```dotenv
TELEGRAM_BOT_TOKEN=your_existing_bot_token
BOT_ADMIN_USER_ID=your_numeric_telegram_user_id
LOCAL_MODEL_BASE_URL=http://127.0.0.1:8080/v1
LOCAL_MODEL_NAME=ginger
LOCAL_MODEL_TIMEOUT_SECONDS=600
```

`BOT_ADMIN_USER_ID` must be a positive numeric **user** ID. Your private Telegram
chat with the bot must use that same account. This ID is not your bot ID or a
group chat ID. CLI model tests and local CSV export do not require bot credentials.

## Start on Windows

Keep your existing llama.cpp server running on port 8080 with `--alias ginger`.
For a 4 GB GPU, a 3B Q4 model generally fits better than a 7B model with CPU
offloading. The local API must expose `/v1/chat/completions`.

From the bot folder in PowerShell:

```powershell
python business_bot.py --test-model
python business_bot.py --test-profile-model
python business_bot.py
```

`--test-local-model` is an alias for `--test-model`. Both use Ginger locally.
The profile test uses the synthetic name Alex and age 25. A successful result is
JSON containing those values. Profile extraction retains the supplied 400-token
output setting. Chat and profiling remain separate calls to the same local model.

An Ollama endpoint also works: set the base URL to `http://127.0.0.1:11434/v1`
and load your model as `ginger`. The optional `Modelfile` is for Ollama only;
llama.cpp uses the Python/file character prompts instead.

Enable reply access in Telegram Business and test from another Telegram account.
`ENABLE_DIRECT_TEST_MODE=true` additionally allows private direct bot chats to be
processed. It is off by default. Run one bot process per SQLite database.

## Admin menu

Send **`/admin`** privately to the bot. The menu provides:

- Dashboard with client, active, paused/review, age-check and pending-reply counts.
- Paginated client lists and filters, with eight clients per page.
- Search by username, numeric ID, name, full chat key, profile contents or notes.
- Profile details split into pages, including self-reported age and review reason.
- Open Telegram profile links when a valid observed username is available.
- Pause and resume per client. These cancel pending replies and wait for a new
  client message after resume; no greeting is sent to that client.
- Admin notes, up to 1,000 characters each and 50 notes per client. Notes are
  searchable and included in exports, and are never sent to the model.
- Delete confirmation before clearing live profile, identity, notes, context and
  pending replies. The chat remains paused. Backups/exported copies are retained.
- CSV export to the configured admin's private chat.

Buttons and free-text input sessions expire after 15 minutes or a restart;
send `/admin` again. Every command and callback checks both the configured user
ID and private chat ID. Other users' commands are silently ignored. Normal client
messages continue to trigger Ginger replies.

Shortcuts:

```text
/admin
/menu
/accounts
/stats
/search Alex
/profile @username
/profile <full_chat_key>
/pause_profile <full_chat_key>
/resume_profile <full_chat_key>
/delete_profile <full_chat_key>
/export_profiles
/cancel
```

`/delete_profile` opens confirmation; it does not immediately delete data. If a
client was deleted, `/resume_profile <full_chat_key>` can re-enable the known chat;
its next message will record identity/profile information again. Unknown keys are
rejected. `/start` is silent and only controls the admin's own direct chat. Use
client menu buttons or the full-key shortcuts to manage other chats.

No bulk outreach is sent by the menu. Opening a Telegram profile lets the admin
choose their next action in Telegram.

## Profiles, context and delivery

Personal details beyond age are optional. Employment does not determine whether
a client can chat. A numeric adult age is a self-report, not verified identity.
Unknown-age introductions stay SFW; underage reports or the existing 12-reply
unknown-age limit pause the conversation for admin review. Eligible adult chats
continue on local Ginger without a separate provider's five-reply cap.

Stable preferences and boundaries survive restarts, with corrections/removals
supported. Plans/events expire from usable memory after `PLAN_MEMORY_DAYS`
(default 7) unless explicitly reconfirmed. Other profile changes do not refresh
those timestamps. Client records and notes expire after `PROFILE_RETENTION_DAYS`
(default 30) of client inactivity. Common credentials/contact strings are redacted
best-effort; this is not a comprehensive sensitive-data filter.

The bot sends no automatic startup greeting or privacy notice. Clients initiate
conversations. Admin resume also waits for a new client message. `/privacy` is an
on-request admin command describing local storage.

Incoming updates and pending/generated replies use a durable SQLite inbox and
queue. Deduplication retains the most recent 10,000 processed receipt IDs without
message bodies. Telegram has no exactly-once send key: ambiguous timeouts or a
crash during delivery pause the affected chat for review instead of replaying a
possibly delivered reply. Check the Telegram chat, then resume from the menu.

Schema migration normalizes legacy paused states to manual review; resume those
clients explicitly. Legacy active phases become active. Supported context is
retained, while retired payload columns are kept only in the pre-upgrade backup.

Local profiles/recent context, admin notes and backups are plaintext files; keep
filesystem access and backups private. Admin deletion clears live records, not
backup archives or Telegram history. Old events cannot recreate deleted live data
through a retained duplicate receipt.

## Settings

`.env.example` contains local settings and fast delivery values. Keep existing
credentials when upgrading instead of overwriting your `.env` wholesale.

| Setting | Default in code | Purpose |
| --- | --- | --- |
| `LOCAL_MODEL_NAME` | `ginger` | Single model for chat and extraction. |
| `LOCAL_MODEL_BASE_URL` | `http://127.0.0.1:8080/v1` | Local OpenAI-compatible server. |
| `LOCAL_MODEL_TIMEOUT_SECONDS` | `180` | Increase to 600 for slow CPU offloading. |
| `MODEL_CONCURRENCY` | `1` | Concurrent requests to the one local model. |
| `WORKER_COUNT` | `2` | Reply workers; waiting jobs occupy no model slot. |
| `MAX_PENDING_UPDATES` | `10000` | Incoming queue capacity. |
| `MAX_PENDING_JOBS` | `1000` | Outstanding bursts/cache limit. |
| `MAX_TURN_CHARS` | `16000` | Maximum characters in one message burst. |
| `PROFILING_REPLY_LIMIT` | `12` | Unknown-age review threshold. |
| `PROFILE_RETENTION_DAYS` | `30` | Client inactivity retention. |
| `PLAN_MEMORY_DAYS` | `7` | Per-plan/event confirmation retention. |

Artificial delay/typing settings remain available. `.env.example` sets them to
zero for fast delivery; old `.env` values still override the code defaults.
`SYSTEM_PROMPT` overrides `SYSTEM_PROMPT_FILE`; keep your own chosen prompt.

For webhook deployments, configure an HTTPS `WEBHOOK_URL` and `WEBHOOK_SECRET`
(ASCII letters/digits/underscores/hyphens, 1–256 characters). `PORT` defaults to
10000. Polling removes a pre-existing webhook without dropping pending updates.
Restarting this version subscribes to button callback updates automatically.
`/healthz` checks worker-thread liveness, not model or Telegram connectivity.

## Troubleshooting extraction

The detailed warning reports model, timeout and a safe upstream error category.
For explicit context overflow, the bot retries once without old dialogue while
preserving the extraction instructions, current profile and latest input. It does
not automatically retry connection failures. A 2048-token server context can be
too small for full profiles; increase context and reduce GPU layers if necessary.

Run `python business_bot.py --test-profile-model` from the same folder and `.env`
as the live bot. Match `LOCAL_MODEL_NAME` to the server alias, check its port and
inspect the actual warning before changing settings. Slow inference can require
more timeout; changing timeout does not make generation faster.

## Offline validation

```powershell
python -m unittest discover -s . -v
```

108 tests cover local-only routing for all chat stages, exact preservation of the
existing prompt strings, profile extraction/error handling, queues/restarts,
legacy schema backups and payload removal, private admin authorization, opaque
buttons, search/pagination, notes/export, confirmation and deletion, pause/resume,
transient DB retries and complete local HTTP model/Telegram menu flows.

Live Telegram and your installed model were not contacted during tests. Verify
the CLI model tests and admin menu on your installation before normal operation.
