"""Offline regression tests: no Telegram/Ollama/xAI calls or real credentials."""
import contextlib
import csv
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("ginger_business_bot", Path(__file__).with_name("business_bot.py"))
bot = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bot
with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "XAI_API_KEY": "", "SYSTEM_PROMPT_FILE": "/nonexistent-ginger-test-prompt"}):
    SPEC.loader.exec_module(bot)


def response(text):
    return {"choices": [{"message": {"content": text}}]}


def message(text="Hello", number=1, chat_id=42, business=False):
    value = {"message_id": number, "chat": {"id": chat_id, "type": "private"},
             "from": {"id": chat_id, "username": "alex"}, "text": text}
    if business:
        value["business_connection_id"] = "biz"
    return {"business_message" if business else "message": value}


def qualified():
    return bot.merge_profile_update(bot.empty_profile(), {
        "age": 25, "name": "Alex", "general_location": "Athens", "interests": ["music"],
        "employment_status": "employed", "employment_evidence": "I work as a chef"})


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patches = [patch.object(bot, name, value) for name, value in {
            "PROFILE_DB_PATH": str(Path(self.tmp.name) / "profiles.sqlite3"),
            "ENABLE_DIRECT_TEST_MODE": True, "PROFILE_EXTRACTION_ENABLED": False,
            "BOT_ADMIN_USER_ID": 99, "VA_CHAT_ID": 99, "MIN_TYPING_SECONDS": 0,
            "MAX_TYPING_SECONDS": 0, "TYPING_JITTER_SECONDS": 0,
            "POST_GENERATION_PAUSE_MIN_SECONDS": 0, "POST_GENERATION_PAUSE_MAX_SECONDS": 0,
            "REPLY_DELAY_MODE": "sequence", "REPLY_DELAY_SEQUENCE": (0, 0, 0),
            "HANDOFF_INCLUDE_TRANSCRIPT": False,
            "MAX_PENDING_JOBS": 100, "MAX_PENDING_UPDATES": 100,
        }.items()]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        bot.scheduler = bot.ReplyScheduler()
        bot.histories.clear()
        bot.business_owner_ids.clear()
        bot.business_reply_allowed.clear()
        bot.reply_queue = queue.Queue(100)
        bot.initialize_storage()
        self.send = patch.object(bot, "send_chat_message").start()
        self.addCleanup(patch.stopall)
        patch.object(bot, "send_typing_action").start()
        self.model = patch.object(bot, "model_request", return_value=response("I’m Ginger. Nice to meet you!")).start()

    def rows(self, sql, args=()):
        with bot.profile_db() as db:
            return db.execute(sql, args).fetchall()

    def execute(self, sql, args=()):
        with bot.profile_db() as db:
            db.execute(sql, args)

    def schedule(self, text="Hello", number=1):
        update = message(text, number)
        bot.enqueue_update(update)
        row = self.rows("SELECT receipt_id,payload FROM incoming_updates WHERE status='queued' ORDER BY rowid LIMIT 1")[0]
        bot.process_message(json.loads(row[1]), row[0])
        return bot.scheduler.pending["direct-test:42"][-1]

    def test_import_has_no_database_or_credential_requirement(self):
        result = subprocess.run([sys.executable, "-c", f"import runpy; runpy.run_path({str(Path(bot.__file__))!r}, run_name='import_test')"],
                                cwd=self.tmp.name, env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((Path(self.tmp.name) / "client_profiles.sqlite3").exists())

    def test_stop_start_resumes_persisted_phase_and_clears_context(self):
        bot.chat_state("direct-test:42")
        self.execute("UPDATE conversation_state SET transcript=? WHERE conversation_key=?",
                     (json.dumps([{"role": "user", "content": "old context"}]), "direct-test:42"))
        bot.process_message(message("/stop"))
        self.assertEqual(bot.chat_state("direct-test:42")["transcript"], "[]")
        bot.scheduler = bot.ReplyScheduler()  # Restart loses all in-memory state.
        bot.process_message(message("/start", 2))
        self.assertFalse(bot.paused("direct-test:42"))
        bot.process_message(message("Hi again", 3))
        self.assertEqual(bot.scheduler.pending["direct-test:42"][0].text, "Hi again")

    def test_start_does_not_bypass_human_review(self):
        bot.chat_state("direct-test:42")
        self.execute("UPDATE conversation_state SET phase='manual_review' WHERE conversation_key='direct-test:42'")
        bot.process_message(message("/start"))
        self.assertTrue(bot.paused("direct-test:42"))

    def test_forget_removes_profile_identity_jobs_inbox_and_history(self):
        job = self.schedule("Personal fact")
        bot.save_client_profile(job.key, qualified())
        bot.cache_history(job.key, [{"role": "user", "content": "old"}])
        bot.enqueue_update(message("queued data", 2))
        bot.process_message(message("/forget", 3))
        self.assertTrue(job.cancelled.is_set())
        self.assertFalse(self.rows("SELECT * FROM client_profiles"))
        self.assertFalse(self.rows("SELECT * FROM client_identities"))
        self.assertFalse(self.rows("SELECT * FROM reply_jobs"))
        self.assertEqual(self.rows("SELECT DISTINCT payload FROM incoming_updates"), [("",)])
        self.assertNotIn(job.key, bot.histories)
        self.assertTrue(bot.paused(job.key))
        bot.process_message(message("/start", 4))
        self.assertFalse(bot.paused(job.key))

    def test_admin_resume_resets_limits_and_pause(self):
        key = "direct-test:42"
        bot.chat_state(key)
        self.execute("UPDATE conversation_state SET phase='manual_review',grok_count=5 WHERE conversation_key=?", (key,))
        bot.process_message(message("/resume_profile " + key, chat_id=99))
        self.assertFalse(bot.paused(key))
        self.assertEqual(bot.chat_state(key)["grok_count"], 0)

    def test_nonadmin_cannot_export_or_resume(self):
        bot.process_message(message("/export_profiles"))
        bot.process_message(message("/resume_profile direct-test:42", 2))
        self.send.assert_not_called()

    def test_nidum_identity_is_retried_as_ginger(self):
        self.model.side_effect = [response("Hi, I'm Nidum AI."), response("I'm Ginger!")]
        answer = bot.ai_reply([], "What's your name?", bot.empty_profile())
        self.assertEqual(answer, "I'm Ginger!")
        self.assertEqual(self.model.call_count, 2)
        self.assertEqual(self.model.call_args.kwargs["provider"], "local")
        self.assertIn("Your only character name is Ginger", self.model.call_args.args[0]["messages"][-1]["content"])

    def test_repeated_identity_leak_uses_safe_ginger_fallback(self):
        self.model.return_value = response("I'm NidumAI, your assistant.")
        answer = bot.ai_reply([], "Who are you?", bot.empty_profile())
        self.assertIn("Ginger", answer)
        self.assertIn("fictional AI", answer)
        self.assertNotIn("Nidum", answer)
        self.assertEqual(self.model.call_count, 2)

    def test_bad_assistant_name_removed_from_model_history(self):
        history = [{"role": "assistant", "content": "My name is Nidum AI"}, {"role": "user", "content": "Hi"}]
        bot.ai_reply(history, "Your name?", bot.empty_profile())
        sent = self.model.call_args.args[0]["messages"]
        self.assertFalse(any(m["role"] == "assistant" and "Nidum" in m["content"] for m in sent))
        self.assertIn("FINAL IDENTITY RULE", sent[0]["content"])

    def test_prompt_override_cannot_drop_final_ginger_instruction(self):
        with patch.dict(os.environ, {"SYSTEM_PROMPT": "You are Nidum AI."}):
            prompt = bot.load_system_prompt()
        self.assertTrue(prompt.endswith("dialogue."))
        self.assertGreater(prompt.index("FINAL IDENTITY RULE"), prompt.index("You are Nidum AI."))

    def test_qualified_chat_uses_xai_otherwise_local(self):
        bot.ai_reply([], "Hi", qualified())
        self.assertEqual(self.model.call_args.kwargs["provider"], "xai")
        p = qualified(); p["age"] = None
        bot.ai_reply([], "Hi", p)
        self.assertEqual(self.model.call_args.kwargs["provider"], "local")

    def test_profile_validation_handles_untrusted_types_and_underage(self):
        p = bot.merge_profile_update(bot.empty_profile(), {"employment_status": ["employed"], "age": True, "interests": {"add": "not a list"}})
        self.assertIsNone(p["employment_status"])
        self.assertIsNone(p["age"])
        p = bot.merge_profile_update(qualified(), {"age": 16, "age_confirmed_adult": True})
        self.assertFalse(p["age_confirmed_adult"])
        self.assertFalse(bot.profile_ready(p))

    def test_employment_extraction_requires_matching_user_evidence(self):
        self.model.return_value = response(json.dumps({"profile_update": {"employment_status": "employed", "employment_evidence": "I work"}}))
        self.assertNotIn("employment_status", bot.extract_profile_update("I study", bot.empty_profile()))
        self.assertEqual(bot.extract_profile_update("I work in a cafe", bot.empty_profile())["employment_status"], "employed")

    def test_invalid_extraction_json_retains_profile(self):
        self.model.return_value = response("not json")
        self.assertEqual(bot.extract_profile_update("Hi", bot.empty_profile()), {})

    def test_batched_messages_are_durable_and_receipt_committed_atomically(self):
        job = self.schedule("one", 1)
        self.schedule("two", 2)
        self.assertEqual(job.text, "one\ntwo")
        self.assertEqual(self.rows("SELECT status,payload FROM incoming_updates"), [("done", ""), ("done", "")])
        bot.scheduler = bot.ReplyScheduler()
        bot.scheduler.restore()
        self.assertEqual(bot.scheduler.pending[job.key][0].text, "one\ntwo")

    def test_duplicate_receipt_after_restart_does_not_schedule_again(self):
        job = self.schedule("once")
        bot.scheduler = bot.ReplyScheduler(); bot.scheduler.restore()
        bot.enqueue_update(message("once"))
        self.assertEqual(len(bot.scheduler.pending[job.key]), 1)
        self.assertEqual(len(self.rows("SELECT * FROM incoming_updates")), 1)

    def test_unscheduled_inbox_message_survives_restart(self):
        bot.enqueue_update(message("not processed yet"))
        with sqlite3.connect(bot.PROFILE_DB_PATH) as db:
            payload = db.execute("SELECT payload FROM incoming_updates WHERE status='queued'").fetchone()[0]
        self.assertEqual(json.loads(payload)["message"]["text"], "not processed yet")

    def test_restored_ready_reply_does_not_merge_away_new_message(self):
        job = self.schedule()
        bot.prepare_answer(job)
        bot.scheduler = bot.ReplyScheduler(); bot.scheduler.restore()
        bot.process_message(message("A new question", 2))
        self.assertEqual(len(bot.scheduler.pending[job.key]), 2)
        self.assertEqual(bot.scheduler.pending[job.key][1].text, "A new question")

    def test_ready_reply_recovery_does_not_regenerate_or_double_count(self):
        job = self.schedule()
        bot.prepare_answer(job)
        old_answer = job.answer
        bot.scheduler = bot.ReplyScheduler(); bot.scheduler.restore()
        recovered = bot.scheduler.pending[job.key][0]
        self.model.reset_mock()
        bot.perform_reply(recovered)
        self.model.assert_not_called()
        self.send.assert_called_with(None, 42, old_answer)
        self.assertEqual(bot.chat_state(job.key)["local_count"], 1)
        self.assertEqual(self.rows("SELECT status FROM reply_jobs"), [("sent",)])

    def test_sending_job_after_crash_is_not_replayed(self):
        job = self.schedule(); bot.prepare_answer(job)
        job.status = "sending"
        with bot.profile_db() as db: bot.persist_job(db, job)
        bot.scheduler = bot.ReplyScheduler(); bot.scheduler.restore()
        self.assertNotIn(job.key, bot.scheduler.pending)
        self.assertEqual(bot.chat_state(job.key)["phase"], "manual_review")
        self.assertEqual(self.rows("SELECT status FROM reply_jobs"), [("uncertain",)])

    def closing_job(self):
        job = self.schedule()
        bot.save_client_profile(job.key, qualified())
        self.execute("UPDATE conversation_state SET grok_count=? WHERE conversation_key=?", (bot.GROK_REPLY_LIMIT, job.key))
        return job

    def test_closing_phase_only_committed_after_successful_delivery(self):
        job = self.closing_job()
        self.send.side_effect = bot.APIError("HTTP 403")
        bot.perform_reply(job)
        self.assertFalse(bot.paused(job.key))
        self.assertEqual(self.rows("SELECT handoff FROM conversation_state WHERE conversation_key=?", (job.key,)), [(None,)])
        self.assertEqual(self.rows("SELECT status FROM reply_jobs"), [("failed",)])

    def test_successful_closing_records_transcript_and_handoff(self):
        job = self.closing_job(); bot.perform_reply(job)
        self.assertEqual(bot.chat_state(job.key)["phase"], "awaiting_va")
        self.assertIn(bot.CLOSING_MESSAGE, bot.chat_state(job.key)["transcript"])
        report = self.rows("SELECT handoff FROM conversation_state")[0][0]
        self.assertIn("Closing delivered successfully", report)
        self.assertIn("Transcript sharing disabled", report)
        self.assertNotIn("Recent conversation:", report)

    def test_ambiguous_send_is_not_retried_and_requests_review(self):
        job = self.schedule()
        self.send.side_effect = bot.APIError("timeout", uncertain=True)
        bot.perform_reply(job)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(bot.chat_state(job.key)["phase"], "manual_review")
        self.assertEqual(self.rows("SELECT status FROM reply_jobs"), [("uncertain",)])

    def test_telegram_rate_limit_is_retried_without_regenerating(self):
        job = self.schedule()
        self.send.side_effect = [bot.APIError("rate limited", retry_after=0), None]
        bot.perform_reply(job)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(bot.chat_state(job.key)["local_count"], 1)

    def test_profile_extraction_cannot_recreate_forgotten_data(self):
        job = self.schedule()
        with patch.object(bot, "PROFILE_EXTRACTION_ENABLED", True), patch.object(bot, "extract_profile_update") as extract:
            def forget_while_extracting(*args):
                bot.process_message(message("/forget", 2))
                return {"name": "should not be saved"}
            extract.side_effect = forget_while_extracting
            bot.perform_reply(job)
        self.assertFalse(self.rows("SELECT * FROM client_profiles"))
        self.assertFalse(self.rows("SELECT * FROM reply_jobs"))
        self.assertEqual(self.send.call_count, 1)  # Only the /forget confirmation.

    def test_pending_job_and_inbox_backlog_limits(self):
        with patch.object(bot, "MAX_PENDING_UPDATES", 1):
            bot.enqueue_update(message())
            with self.assertRaises(bot.BacklogFull): bot.enqueue_update(message("second", 2))
        with patch.object(bot, "MAX_PENDING_JOBS", 1):
            bot.scheduler.add("other:1", None, 1, "one")
            with self.assertRaises(bot.BacklogFull): bot.scheduler.add("other:2", None, 2, "two")

    def test_oversized_burst_splits_instead_of_discarding_messages(self):
        with patch.object(bot, "MAX_TURN_CHARS", 10):
            a = bot.scheduler.add("direct-test:42", None, 42, "12345678")
            b = bot.scheduler.add("direct-test:42", None, 42, "abcdefgh")
        self.assertIsNot(a, b)
        self.assertEqual(a.text, "12345678")
        self.assertEqual(b.text, "abcdefgh")

    def test_cancelled_jobs_not_restored(self):
        job = self.schedule(); bot.scheduler.cancel(job.key)
        bot.scheduler = bot.ReplyScheduler(); bot.scheduler.restore()
        self.assertFalse(bot.scheduler.pending)

    def test_sequential_delays_are_per_burst(self):
        with patch.object(bot, "REPLY_DELAY_SEQUENCE", (1, 2, 3)):
            a = bot.scheduler.add("key", None, 1, "a")
            a.generation_started = True
            bot.scheduler.active["key"] = bot.scheduler.pending.pop("key")[0]
            b = bot.scheduler.add("key", None, 1, "b")
        self.assertEqual((a.delay, b.delay), (1, 2))
        self.assertIsNone(b.due_at)

    def test_privacy_notice_and_redaction(self):
        job = self.schedule("my email is person@example.com and password=secret123")
        self.assertNotIn("person@example.com", self.rows("SELECT data FROM reply_jobs")[0][0])
        bot.perform_reply(job)
        self.assertTrue(self.send.call_args.args[2].startswith(bot.PRIVACY_NOTICE))
        self.assertEqual(bot.chat_state(job.key)["privacy_notified"], 1)

    def test_retention_removes_stale_profiles_and_work(self):
        job = self.schedule(); bot.save_client_profile(job.key, qualified())
        self.execute("UPDATE conversation_state SET last_activity=0")
        bot.cleanup_expired_data()
        self.assertFalse(self.rows("SELECT * FROM client_profiles"))
        self.assertFalse(self.rows("SELECT * FROM conversation_state"))
        self.assertNotIn(job.key, bot.scheduler.pending)

    def test_csv_formula_injection_is_escaped(self):
        bot.chat_state("key")
        bot.save_client_profile("key", bot.merge_profile_update(bot.empty_profile(), {"name": "=HYPERLINK(1)"}))
        rows = list(csv.DictReader(io.StringIO(bot.profiles_csv().decode("utf-8-sig"))))
        self.assertEqual(rows[0]["name"], "'=HYPERLINK(1)")

    def test_corrupt_transcript_is_ignored(self):
        self.assertEqual(bot.read_transcript("not json"), [])
        self.assertEqual(bot.read_transcript('{"unexpected":1}'), [])
        self.assertEqual(bot.read_transcript('[null,1,{"role":"user","content":"hi"}]'), [{"role": "user", "content": "hi"}])

    def test_revoked_business_connection_cancels_pending_work(self):
        job = bot.scheduler.add("biz:42", "biz", 42, "hello")
        bot.handle_business_connection({"business_connection": {"id": "biz", "user": {"id": 99}, "is_enabled": False}})
        self.assertTrue(job.cancelled.is_set())
        self.assertFalse(bot.business_reply_allowed["biz"])

    def test_missing_credentials_do_not_block_local_model_test(self):
        with patch.object(sys, "argv", ["business_bot.py", "--test-local-model"]), contextlib.redirect_stdout(io.StringIO()) as output:
            bot.main()
        self.assertIn("Ginger", output.getvalue())
        self.assertEqual(self.model.call_args.kwargs["provider"], "local")

    def test_http_errors_hide_token_and_ambiguous_flag(self):
        import urllib.error
        with patch.object(bot.urllib.request, "urlopen", side_effect=urllib.error.URLError("https://api.telegram.org/botsecret-token/sendMessage")):
            with self.assertRaises(bot.APIError) as caught:
                bot.post_json("https://example.com", {})
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_separate_provider_concurrency(self):
        self.assertIsNot(bot.model_slots["local"], bot.model_slots["xai"])

    def test_webhook_persists_before_ack_and_rejects_overload(self):
        with patch.object(bot, "WEBHOOK_SECRET", "test-secret"), patch.object(bot, "MAX_PENDING_UPDATES", 1):
            server = bot.LimitedHTTPServer(("127.0.0.1", 0), bot.WebhookHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                conn = http.client.HTTPConnection(*server.server_address, timeout=5)
                conn.request("POST", "/telegram/test-secret", json.dumps(message()), {"X-Telegram-Bot-Api-Secret-Token": "wrong"})
                reply = conn.getresponse(); self.assertEqual(reply.status, 403); reply.read()
                conn.request("POST", "/telegram/test-secret", json.dumps(message()), {"X-Telegram-Bot-Api-Secret-Token": "test-secret"})
                reply = conn.getresponse(); self.assertEqual(reply.status, 200); reply.read()
                self.assertEqual(self.rows("SELECT status FROM incoming_updates"), [("queued",)])
                conn.request("POST", "/telegram/test-secret", json.dumps(message("overflow", 2)), {"X-Telegram-Bot-Api-Secret-Token": "test-secret"})
                reply = conn.getresponse(); self.assertEqual(reply.status, 503); reply.read()
                conn.close()
            finally:
                server.shutdown(); server.server_close(); thread.join(2)

    def test_stop_preserves_a_later_queued_start(self):
        bot.enqueue_update(message("/stop", 1))
        bot.enqueue_update(message("/start", 2))
        receipt, payload = self.rows("SELECT receipt_id,payload FROM incoming_updates ORDER BY rowid LIMIT 1")[0]
        bot.process_message(json.loads(payload), receipt)
        rows = self.rows("SELECT receipt_id,payload FROM incoming_updates WHERE status='queued'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][1])["message"]["text"], "/start")
        bot.process_message(json.loads(rows[0][1]), rows[0][0])
        self.assertFalse(bot.paused("direct-test:42"))

    def test_legacy_database_migration_preserves_profiles(self):
        legacy = str(Path(self.tmp.name) / "legacy.sqlite3")
        with sqlite3.connect(legacy) as db:
            db.execute("CREATE TABLE conversation_state (conversation_key TEXT PRIMARY KEY, phase TEXT NOT NULL DEFAULT 'profiling', grok_count INTEGER NOT NULL DEFAULT 0, local_count INTEGER NOT NULL DEFAULT 0, transcript TEXT NOT NULL DEFAULT '[]', handoff TEXT, notified INTEGER NOT NULL DEFAULT 0)")
            db.execute("INSERT INTO conversation_state(conversation_key,phase,grok_count) VALUES ('old','stopped',4)")
        with patch.object(bot, "PROFILE_DB_PATH", legacy):
            bot.initialize_storage()
            bot.initialize_storage()  # Migration is repeatable.
            self.assertEqual(bot.chat_state("old")["phase"], "stopped")
            self.assertEqual(bot.chat_state("old")["grok_count"], 4)
            bot.scheduler.add("new", None, 1, "hello")

    def test_forget_waits_for_inflight_send_then_removes_delivery_records(self):
        job = self.schedule()
        entered, release = threading.Event(), threading.Event()
        errors = []
        def blocked_send(*args):
            entered.set()
            if not release.wait(2): raise AssertionError("send release timed out")
        self.send.side_effect = blocked_send
        def run(action):
            try: action()
            except BaseException as exc: errors.append(exc)
        reply = threading.Thread(target=lambda: run(lambda: bot.perform_reply(job)))
        reply.start()
        self.assertTrue(entered.wait(2))
        forget = threading.Thread(target=lambda: run(lambda: bot.process_message(message("/forget", 2))))
        forget.start()
        release.set()
        reply.join(2); forget.join(2)
        self.assertFalse(reply.is_alive()); self.assertFalse(forget.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(self.rows("SELECT * FROM reply_jobs"))
        self.assertEqual(bot.chat_state(job.key)["transcript"], "[]")
        self.assertNotIn(job.key, bot.histories)

    def test_polling_acknowledges_only_after_persistence(self):
        calls = []
        def telegram(method, payload, **kwargs):
            calls.append((method, dict(payload)))
            if method == "deleteWebhook": return True
            if len(calls) == 2: return [{"update_id": 7, **message()}]
            if len(calls) == 3:
                self.assertEqual(payload["offset"], 8)
                self.assertEqual(self.rows("SELECT status FROM incoming_updates"), [("queued",)])
                raise KeyboardInterrupt
        with patch.object(bot, "telegram", side_effect=telegram):
            with self.assertRaises(KeyboardInterrupt): bot.run_polling()
        self.assertEqual(calls[0], ("deleteWebhook", {"drop_pending_updates": False}))

    def test_full_http_worker_flow_repairs_nidum_before_telegram_send(self):
        # Isolate daemon workers in a subprocess. Both APIs are local mock servers.
        script = r'''
import http.server, json, os, tempfile, threading, time
import business_bot as b
class API(http.server.BaseHTTPRequestHandler):
    model_calls = 0
    sent = []
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/v1/chat/completions':
            API.model_calls += 1
            assert payload['model'] == b.LOCAL_MODEL_NAME
            assert 'FINAL IDENTITY RULE' in payload['messages'][0]['content']
            text = "I'm Nidum AI" if API.model_calls == 1 else "I'm Ginger! Nice to meet you."
            result = {'choices':[{'message': {'content': text}}]}
        else:
            if self.path.endswith('/sendMessage'): API.sent.append(payload['text'])
            result = {'ok': True, 'result': True}
        data = json.dumps(result).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *args): pass
with tempfile.TemporaryDirectory() as temp:
    b.PROFILE_DB_PATH = os.path.join(temp, 'state.sqlite3')
    b.ENABLE_DIRECT_TEST_MODE = True; b.PROFILE_EXTRACTION_ENABLED = False
    b.REPLY_DELAY_MODE = 'sequence'; b.REPLY_DELAY_SEQUENCE = (0,)
    b.MIN_TYPING_SECONDS = b.MAX_TYPING_SECONDS = b.TYPING_JITTER_SECONDS = 0
    b.POST_GENERATION_PAUSE_MIN_SECONDS = b.POST_GENERATION_PAUSE_MAX_SECONDS = 0
    b.VA_CHAT_ID = 0
    api = http.server.ThreadingHTTPServer(('127.0.0.1', 0), API)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:' + str(api.server_port)
    b.TELEGRAM_API = base + '/botTEST'
    b.LOCAL_CHAT_COMPLETIONS_API = base + '/v1/chat/completions'
    b.initialize_storage(); b.start_workers()
    b.enqueue_update({'message': {'message_id': 1, 'chat': {'id':42, 'type':'private'}, 'from':{'id':42}, 'text': "What's your name?"}})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with b.profile_db() as db:
            sent = db.execute("SELECT COUNT(*) FROM reply_jobs WHERE status='sent'").fetchone()[0]
        if sent: break
        time.sleep(.01)
    assert sent == 1
    assert API.model_calls == 2
    assert len(API.sent) == 1 and 'Ginger' in API.sent[0] and 'Nidum' not in API.sent[0]
    assert b.chat_state('direct-test:42')['local_count'] == 1
    assert all(t.is_alive() for t in b.runtime_threads)
    b.scheduler.shutdown(); api.shutdown(); api.server_close()
print('HTTP worker integration passed')
'''
        env = dict(os.environ, TELEGRAM_BOT_TOKEN="", XAI_API_KEY="", SYSTEM_PROMPT_FILE="/no-test-prompt")
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(bot.__file__).parent, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("HTTP worker integration passed", result.stdout)


    def test_extraction_model_is_independent_of_ginger(self):
        with patch.object(bot, "LOCAL_MODEL_NAME", "ginger"), patch.object(bot, "PROFILE_MODEL_NAME", "qwen2.5:3b"):
            self.model.return_value = response('{"profile_update":{"age":25}}')
            update = bot.extract_profile_update("I am 25", bot.empty_profile())
            self.assertEqual(update["age"], 25)
            self.assertEqual(self.model.call_args.args[0]["model"], "qwen2.5:3b")
            self.assertEqual(self.model.call_args.kwargs["provider"], "local")
            self.model.return_value = response("I'm Ginger!")
            bot.ai_reply([], "Hello", bot.empty_profile())
            self.assertEqual(self.model.call_args.args[0]["model"], "ginger")

    def test_age_alone_allows_grok_with_personal_details_optional(self):
        profile = bot.merge_profile_update(bot.empty_profile(), {"age": 21})
        self.assertTrue(bot.profile_ready(profile))
        bot.ai_reply([], "Hi", profile)
        self.assertEqual(self.model.call_args.kwargs["provider"], "xai")

    def test_adult_flag_alone_does_not_replace_numeric_age(self):
        for age in (None, True, "21", 17, 121):
            profile = dict(bot.empty_profile(), age=age, age_confirmed_adult=True)
            self.assertFalse(bot.profile_ready(profile))
        self.assertFalse(bot.profile_ready(dict(bot.empty_profile(), age=21, age_confirmed_adult=False)))

    def test_unemployed_and_declined_employment_do_not_close_adult_chats(self):
        for status in ("unemployed", "declined", "unknown"):
            with self.subTest(status=status):
                key = "direct-test:" + str(100 + len(status))
                profile = dict(bot.empty_profile(), age=25, employment_status=status)
                bot.save_client_profile(key, profile)
                bot.chat_state(key)
                job = bot.scheduler.add(key, None, 100, "hello")
                bot.perform_reply(job)
                self.assertIsNone(job.closing_phase)
                self.assertFalse(bot.paused(key))
                self.assertEqual(self.model.call_args.kwargs["provider"], "xai")

    def test_extracted_adult_age_routes_directly_to_grok(self):
        job = self.schedule("I am 25 years old")
        self.model.side_effect = [response('{"profile_update":{"age":25}}'), response("I'm Ginger. Lovely to meet you!")]
        with patch.object(bot, "PROFILE_EXTRACTION_ENABLED", True):
            bot.perform_reply(job)
        calls = self.model.call_args_list
        self.assertEqual(calls[0].args[0]["model"], bot.PROFILE_MODEL_NAME)
        self.assertEqual(calls[1].kwargs["provider"], "xai")
        self.assertEqual(bot.chat_state(job.key)["grok_count"], 1)
        self.assertEqual(bot.chat_state(job.key)["local_count"], 0)

    def test_underage_chat_does_not_call_conversational_model(self):
        job = self.schedule()
        bot.save_client_profile(job.key, dict(bot.empty_profile(), age=17))
        bot.perform_reply(job)
        self.model.assert_not_called()
        self.assertEqual(bot.chat_state(job.key)["phase"], "manual_review")

    def test_unknown_age_keeps_sfw_guidance_and_optional_details(self):
        bot.ai_reply([], "Hi", bot.empty_profile())
        system = self.model.call_args.args[0]["messages"][0]["content"]
        self.assertIn("Keep this phase SFW", system)
        self.assertIn("Name, location, interests and employment are optional", system)
        self.assertIn("Respect a refusal", system)
        self.assertNotIn("Learn these details gradually:", system)

    def test_same_ginger_identity_is_sent_to_both_providers(self):
        for profile in (bot.empty_profile(), dict(bot.empty_profile(), age=25)):
            bot.ai_reply([], "Who are you?", profile)
            system = self.model.call_args.args[0]["messages"][0]["content"]
            self.assertIn("Ginger", system)
            self.assertIn("21-year-old British woman from Manchester", system)
            self.assertIn("UK spelling", system)

    def test_preferences_and_boundaries_survive_restart_and_correct(self):
        key = "direct-test:42"
        profile = bot.merge_profile_update(bot.empty_profile(), {"favorite_music":"techno", "boundaries":["No pet names"], "interests":["gaming"]})
        bot.save_client_profile(key, profile)
        corrected = bot.merge_profile_update(bot.load_client_profile(key), {"favorite_music":"R&B", "interests":{"remove":["gaming"],"add":["cooking"]}})
        bot.save_client_profile(key, corrected)
        bot.histories.clear()
        loaded = bot.load_client_profile(key)
        self.assertEqual(loaded["favorite_music"], "R&B")
        self.assertEqual(loaded["interests"], ["cooking"])
        bot.ai_reply([], "Hello again", loaded)
        system = self.model.call_args.args[0]["messages"][0]["content"]
        self.assertIn("No pet names", system)
        self.assertIn("Respect boundaries", system)
        self.assertIn("latest user correction takes precedence", system)

    def test_old_plans_expire_without_erasing_stable_preferences(self):
        key = "direct-test:42"
        with patch.object(bot.time, "time", return_value=1000000):
            bot.save_client_profile(key, dict(bot.empty_profile(), upcoming_plans=["Trip tomorrow"], favorite_music="techno"))
        with patch.object(bot.time, "time", return_value=1000000 + bot.PLAN_MEMORY_DAYS*86400 + 1):
            profile = bot.load_client_profile(key)
        self.assertEqual(profile["upcoming_plans"], [])
        self.assertEqual(profile["favorite_music"], "techno")

    def test_unrelated_profile_update_does_not_refresh_a_plan(self):
        key = "direct-test:42"
        with patch.object(bot.time, "time", return_value=1000000):
            bot.save_client_profile(key, dict(bot.empty_profile(), upcoming_events=["Concert on Friday"]))
        with patch.object(bot.time, "time", return_value=1000000 + 86400):
            updated = bot.merge_profile_update(bot.load_client_profile(key), {"name":"Alex"})
            bot.save_client_profile(key, updated, confirmed_update={"name":"Alex"})
        timestamp = self.rows("SELECT confirmed_at FROM profile_memory_timestamps")[0][0]
        self.assertEqual(timestamp, 1000000)

    def test_reconfirmed_plan_refreshes_only_that_plan_and_removal_clears_metadata(self):
        key = "direct-test:42"
        with patch.object(bot.time, "time", return_value=1000000):
            bot.save_client_profile(key, dict(bot.empty_profile(), upcoming_plans=["Trip", "Dinner"]))
        with patch.object(bot.time, "time", return_value=1000000+86400):
            delta = {"upcoming_plans":{"add":["Trip"],"remove":["Dinner"]}}
            merged = bot.merge_profile_update(bot.load_client_profile(key), delta)
            bot.save_client_profile(key, merged, confirmed_update=delta)
        self.assertEqual(self.rows("SELECT item,confirmed_at FROM profile_memory_timestamps"), [("trip",1000000+86400)])
        bot.delete_client_profile(key)
        self.assertFalse(self.rows("SELECT * FROM profile_memory_timestamps"))

    def test_legacy_undated_plans_are_not_assumed_current(self):
        key = "direct-test:42"
        self.execute("INSERT INTO client_profiles(conversation_key,profile_json) VALUES (?,?)", (key,json.dumps({"upcoming_plans":["yesterday"],"interests":["music"]})))
        profile = bot.load_client_profile(key)
        self.assertEqual(profile["upcoming_plans"], [])
        self.assertEqual(profile["interests"], ["music"])

    def test_csv_excludes_expired_plans(self):
        key = "direct-test:42"
        bot.save_client_profile(key, dict(bot.empty_profile(), upcoming_plans=["Expired trip"]))
        self.execute("UPDATE profile_memory_timestamps SET confirmed_at=0")
        self.assertNotIn("Expired trip", bot.profiles_csv().decode("utf-8-sig"))

    def test_profile_model_diagnostic_validates_json_semantics(self):
        self.model.return_value = response('{"profile_update":{"name":"Alex","age":25}}')
        with patch.object(sys,"argv",["business_bot.py","--test-profile-model"]), contextlib.redirect_stdout(io.StringIO()) as output:
            bot.main()
        self.assertEqual(json.loads(output.getvalue()), {"name":"Alex","age":25})
        self.model.return_value = response("not JSON")
        with patch.object(sys,"argv",["business_bot.py","--test-profile-model"]):
            with self.assertRaises(SystemExit): bot.main()

    def test_shorter_defaults_can_still_be_overridden(self):
        script = 'import business_bot as b; import json; print(json.dumps([b.REPLY_DELAY_MIN_SECONDS,b.REPLY_DELAY_MAX_SECONDS,b.REPLY_DELAY_SEQUENCE]))'
        environment = {"PATH": os.environ["PATH"]}
        default = subprocess.run([sys.executable,"-c",script],cwd=Path(bot.__file__).parent,env=environment,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(default.stdout),[10.0,25.0,[10.0,15.0,25.0]])
        environment.update(REPLY_DELAY_MIN_SECONDS="60",REPLY_DELAY_MAX_SECONDS="180")
        override = subprocess.run([sys.executable,"-c",script],cwd=Path(bot.__file__).parent,env=environment,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(override.stdout)[:2],[60.0,180.0])


if __name__ == "__main__":
    unittest.main()
