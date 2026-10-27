# Local Ginger chat bot and private admin client manager

Ginger handles chat replies through one local OpenAI-compatible model server.
**Automatic profile extraction is removed:** there is no second JSON extraction
request, no automatic personal-fact update, no extraction prompt/setting/CLI, and
no profiling reply quota. A normal reply uses one generation. Invalid output can
receive one bounded rewrite before a clean fallback is used.

Collected Telegram user IDs/usernames, local conversation context, admin notes,
manual reported ages and existing saved details remain available in the private
admin menu. Existing details are not automatically updated from client messages.
All generation uses the configured local model. Python 3.10+ standard library
only; no pip dependencies or cloud-model key.

## Upgrade

1. Stop the old Python process and preserve your `.env`, SQLite database and your
   own prompt files. Keep the model server running.
2. Replace `business_bot.py`. Do not overwrite existing credentials with example
   placeholders. Old extraction/provider settings are unused and can be removed.
3. Keep these local settings:

   ```dotenv
   TELEGRAM_BOT_TOKEN=your_existing_bot_token
   BOT_ADMIN_USER_ID=your_numeric_telegram_user_id
   LOCAL_MODEL_BASE_URL=http://127.0.0.1:8080/v1
   LOCAL_MODEL_NAME=ginger
   LOCAL_MODEL_TIMEOUT_SECONDS=600
   ```

4. Test and start from the bot folder:

   ```powershell
   python business_bot.py --test-model
   python business_bot.py
   ```

`--test-local-model` is an alias for `--test-model`. The old extraction diagnostic
flag is removed. Live startup requires the Telegram token and a positive numeric
admin **user** ID. Model tests and local CSV export do not require Telegram
credentials. Run one bot process per database.

Keep your existing llama.cpp server using `--alias ginger --port 8080`. The GGUF
file selects the underlying model; the alias does not define its personality.
The model's embedded chat template must match its training. A custom filename
alone is not evidence that ChatML or another template should be forced. An Ollama
endpoint also works with base URL `http://127.0.0.1:11434/v1` and model `ginger`.
The optional `Modelfile` is for Ollama only.

## Reply-quality controls

The changes address the observed invented writing job, reversed speaker name and
repeated slash replies:

- Separate character and client facts are added to the model context. Ginger is
  the speaker. Client facts describe the client, not Ginger.
- No occupation is supplied by default. Set `CHARACTER_OCCUPATION` only if that
  specific job belongs in your script. Common first-person job claims outside
  that setting are rejected. This is a pattern check, not a complete semantic
  fact verifier.
- Clear vocatives addressing the client as Ginger are rejected unless the client
  explicitly introduced themselves with that name. Self-introductions as Ginger
  remain allowed. An ambiguous old saved client name of Ginger is omitted from
  model context without deleting the saved record or other details.
- Long punctuation runs, repeated-word loops, template-token leakage and multiple
  generated speaker turns are rejected. Short emoji, ordinary URLs and normal
  slash/Markdown usage are accepted.
- Bad historical assistant turns and their paired user turns are excluded from
  future model context. Complete alternating exchanges are retained, avoiding
  orphan turns that strict chat templates can reject.
- A bad reply gets at most one rewrite. The rewrite uses one leading system
  message instead of appending a system role after the user message. Rejected
  output is not sent or stored as the assistant's reply.
- Old prepared replies from earlier versions are checked and repaired before
  delivery, without charging a second turn.
- `CHAT_FREQUENCY_PENALTY` defaults to `0.25` (range -2 to 2) for chat requests.
  The style/character prompt text remains; an added grounding layer provides
  factual boundaries. The old automatic intake-question rule is removed.

These controls prevent the tested output patterns. They do not establish the raw
model's conversational quality; test your installed GGUF and its chat template.

## Admin menu

Send **`/admin`** privately to the bot from the configured admin account.

The menu supports dashboard counts, paginated client lists, name/username/ID/note
search, saved-detail pages, Open Telegram profile links, notes, manual reported
age, pause/resume, confirmed deletion and CSV export. Every command and button
checks both the configured admin ID and its private chat ID. Other users' commands
are silently ignored. Normal client messages still receive replies.

- **Set reported age:** enter an age explicitly reported by the client (1–120),
  or `clear`. No model is called and no age is extracted from messages. It records
  a self-report, not verified identity. Age changes cancel old queued/generated
  replies; clearing or lowering age also clears old context. Underage ages pause
  immediately, and changing an age does not auto-resume a paused client.
  Unknown-age chat stays SFW. The old
  unknown-age reply quota is removed. Underage records still trigger review.
- **Pause / Resume:** cancel pending replies and wait for a fresh client message.
  Resuming does not send a greeting. Existing client data is retained.
- **Add note:** up to 1,000 characters and 50 notes per client. Notes are searchable
  and exported, but never sent to Ginger. Command or button navigation cancels
  pending input so notes cannot accidentally be added to the wrong client.
- **Delete data:** requires confirmation. It clears live identity, saved details,
  notes, context and queued replies. The chat remains paused. Backups and already
  exported copies are separate.
- **Export CSV:** delivered to the admin's private chat. Identity-only clients and
  notes are included. `/export_profiles` and the local CLI export still work.

Buttons and input sessions expire after 15 minutes or a restart; send `/admin`
again. Database retries preserve their button capability and do not repeat an
already committed action merely because rendering its result failed.

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

`/delete_profile` opens confirmation. A known deleted chat can be re-enabled with
`/resume_profile <full_chat_key>`; its next message records Telegram identity again,
without extracting personal facts. `/start` is silent and controls only the admin's
own direct test chat. Use client controls for other chats.

## Testing Telegram

Business reply access must be enabled. Test Business conversations from a
separate account; owner-authored messages are ignored to prevent self-replies.
For normal messages sent directly to the bot, set:

```dotenv
ENABLE_DIRECT_TEST_MODE=true
```

This is off by default. Admin commands work with it off. If testing from the
admin account, send `/cancel` first to leave a pending menu search/note/age input.
The bot sends no automatic startup greeting or privacy notice; clients initiate.

## Storage, migration and recovery

Before the first legacy schema upgrade, SQLite's backup API creates
`*.pre-local-menu-*.sqlite3` beside the database. The supported conversation fields
are migrated transactionally; retired payload columns are removed from the live
conversation table and retained only in the backup. Existing identity/profile
records and supported context are preserved. Previously paused clients remain
paused for admin review. Prepared replies from a removed provider are cancelled.

The durable inbox and reply queue recover normal waiting/ready work on restart.
Recent processed receipt IDs are retained without message bodies for duplicate
protection. A timeout or crash during Telegram sending can leave delivery
uncertain. Those chats pause for review instead of replaying a possibly sent
message. Inspect the Telegram chat, then resume through the admin menu.

`PROFILE_RETENTION_DAYS` defaults to 30 days of client inactivity and covers live
client records and notes. Existing temporary plans/events in saved data retain
`PLAN_MEMORY_DAYS` (default 7). No new plans/events are extracted. Files and backups
are plaintext; keep access private. Deleting live data does not delete backups or
Telegram history. Common credential/contact redaction is best-effort.

## Configuration and troubleshooting

`.env.example` contains fast-delivery values. Existing `.env` values override
code defaults. Keep your own nonempty `SYSTEM_PROMPT` if desired; it overrides
`SYSTEM_PROMPT_FILE`. If your custom prompt still asks personal intake questions,
that wording can influence replies even though extraction code is removed.

Model calls use `LOCAL_MODEL_BASE_URL` and `LOCAL_MODEL_NAME`. Concurrency defaults
to one; request timeout defaults to 180 seconds and can be set to 600 for CPU
offloading. Increasing timeout changes waiting time, not generation speed.

The warning reports safe HTTP error categories rather than arbitrary response
bodies. Check server port/model alias for connection or model errors. Prompts,
chat history and generated output share the context window; increase server
context or reduce history if a large request exceeds it, balancing GPU memory.

For webhook hosting, set HTTPS `WEBHOOK_URL`, `WEBHOOK_SECRET` (1–256 ASCII
letters/digits/underscores/hyphens) and optional `PORT` (default 10000). Restarting
subscribes to callbacks. Polling removes an old webhook without dropping updates.
`/healthz` checks worker liveness, not model/Telegram connectivity.

## Offline validation

```powershell
python -m unittest discover -s . -v
```

126 tests cover the single chat generation/no automatic profile writes, removed
extraction entrypoints, manual age controls, screenshot failure patterns, character
prompt preservation, queue recovery, legacy backups and payload removal, private
admin authorization, menu search/notes/deletion/export, transient DB retries and
complete local HTTP model/Telegram menu flows. Only synthetic data and mock local
APIs are used; live model quality and Telegram operation require your local tests.
