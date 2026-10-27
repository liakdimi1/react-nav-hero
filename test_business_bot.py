"""Offline regression tests: synthetic data and local mock APIs only."""
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
with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "SYSTEM_PROMPT_FILE": "/nonexistent-ginger-test-prompt"}):
    SPEC.loader.exec_module(bot)
REAL_MODEL_REQUEST = bot.model_request


def response(text):
    return {"choices": [{"message": {"content": text}}]}


def message(text="Hello", number=1, chat_id=42, business=False):
    value = {"message_id": number, "chat": {"id": chat_id, "type": "private"},
             "from": {"id": chat_id, "username": "alex"}, "text": text}
    if business:
        value["business_connection_id"] = "biz"
    return {"business_message" if business else "message": value}


def admin_control(update, receipt_id=None):
    """Exercise control behavior with a configured admin, not client privileges."""
    with patch.object(bot, "BOT_ADMIN_USER_ID", update["message"]["from"]["id"]):
        return bot.process_message(update, receipt_id)


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
            "ENABLE_DIRECT_TEST_MODE": True,
            "BOT_ADMIN_USER_ID": 99, "CHARACTER_OCCUPATION": "", "MIN_TYPING_SECONDS": 0,
            "MAX_TYPING_SECONDS": 0, "TYPING_JITTER_SECONDS": 0,
            "POST_GENERATION_PAUSE_MIN_SECONDS": 0, "POST_GENERATION_PAUSE_MAX_SECONDS": 0,
            "REPLY_DELAY_MODE": "sequence", "REPLY_DELAY_SEQUENCE": (0, 0, 0),

            "MAX_PENDING_JOBS": 100, "MAX_PENDING_UPDATES": 100,
        }.items()]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        bot.scheduler = bot.ReplyScheduler()
        bot.histories.clear()
        bot.admin_tokens.clear()
        bot.admin_sessions.clear()
        bot.business_owner_ids.clear()
        bot.business_reply_allowed.clear()
        bot.reply_queue = queue.Queue(100)
        bot.initialize_storage()
        self.send = patch.object(bot, "send_chat_message").start()
        self.addCleanup(patch.stopall)
        patch.object(bot, "send_typing_action").start()
        self.telegram = patch.object(bot, "telegram", return_value=True).start()
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
        admin_control(message("/stop"))
        self.assertEqual(bot.chat_state("direct-test:42")["transcript"], "[]")
        bot.scheduler = bot.ReplyScheduler()  # Restart loses all in-memory state.
        admin_control(message("/start", 2))
        self.assertFalse(bot.paused("direct-test:42"))
        bot.process_message(message("Hi again", 3))
        self.assertEqual(bot.scheduler.pending["direct-test:42"][0].text, "Hi again")

    def test_start_does_not_bypass_human_review(self):
        bot.chat_state("direct-test:42")
        self.execute("UPDATE conversation_state SET phase='manual_review' WHERE conversation_key='direct-test:42'")
        admin_control(message("/start"))
        self.assertTrue(bot.paused("direct-test:42"))

    def test_forget_removes_profile_identity_jobs_inbox_and_history(self):
        job = self.schedule("Personal fact")
        bot.save_client_profile(job.key, qualified())
        bot.cache_history(job.key, [{"role": "user", "content": "old"}])
        bot.enqueue_update(message("queued data", 2))
        admin_control(message("/forget", 3))
        self.assertTrue(job.cancelled.is_set())
        self.assertFalse(self.rows("SELECT * FROM client_profiles"))
        self.assertFalse(self.rows("SELECT * FROM client_identities"))
        self.assertFalse(self.rows("SELECT * FROM reply_jobs"))
        self.assertEqual(self.rows("SELECT DISTINCT payload FROM incoming_updates"), [("",)])
        self.assertNotIn(job.key, bot.histories)
        self.assertTrue(bot.paused(job.key))
        admin_control(message("/start", 4))
        self.assertFalse(bot.paused(job.key))

    def test_admin_resume_resets_limits_and_pause(self):
        key = "direct-test:42"
        bot.chat_state(key)
        self.execute("UPDATE conversation_state SET phase='manual_review',local_count=5 WHERE conversation_key=?", (key,))
        bot.process_message(message("/resume_profile " + key, chat_id=99))
        self.assertFalse(bot.paused(key))
        self.assertEqual(bot.chat_state(key)["local_count"], 0)

    def test_nonadmin_cannot_export_or_resume(self):
        bot.process_message(message("/export_profiles"))
        bot.process_message(message("/resume_profile direct-test:42", 2))
        self.send.assert_not_called()

    def test_nidum_identity_is_retried_as_ginger(self):
        self.model.side_effect = [response("Hi, I'm Nidum AI."), response("I'm Ginger!")]
        answer = bot.ai_reply([], "What's your name?", bot.empty_profile())
        self.assertEqual(answer, "I'm Ginger!")
        self.assertEqual(self.model.call_count, 2)
        self.assertIn("Your only character name is Ginger", self.model.call_args.args[0]["messages"][0]["content"])

    def test_repeated_identity_leak_uses_safe_ginger_fallback(self):
        self.model.return_value = response("I'm NidumAI, your assistant.")
        answer = bot.ai_reply([], "Who are you?", bot.empty_profile())
        self.assertIn("Ginger", answer)
        self.assertEqual(answer, "You've got my attention 😌, i am Ginger")
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


    def test_profile_validation_handles_untrusted_types_and_underage(self):
        p = bot.merge_profile_update(bot.empty_profile(), {"employment_status": ["employed"], "age": True, "interests": {"add": "not a list"}})
        self.assertIsNone(p["employment_status"])
        self.assertIsNone(p["age"])
        p = bot.merge_profile_update(qualified(), {"age": 16, "age_confirmed_adult": True})
        self.assertFalse(p["age_confirmed_adult"])
        self.assertFalse(bot.profile_ready(p))



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

    def test_first_reply_has_no_automatic_notice_and_keeps_redaction(self):
        job = self.schedule("my email is person@example.com and password=secret123")
        self.assertNotIn("person@example.com", self.rows("SELECT data FROM reply_jobs")[0][0])
        bot.perform_reply(job)
        self.assertEqual(self.send.call_args.args[2], "I’m Ginger. Nice to meet you!")
        self.assertNotIn("AI services process these messages", bot.chat_state(job.key)["transcript"])

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

    def test_http_errors_hide_token_and_ambiguous_flag(self):
        import urllib.error
        with patch.object(bot.urllib.request, "urlopen", side_effect=urllib.error.URLError("https://api.telegram.org/botsecret-token/sendMessage")):
            with self.assertRaises(bot.APIError) as caught:
                bot.post_json("https://example.com", {})
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)


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
        admin_control(json.loads(payload), receipt)
        rows = self.rows("SELECT receipt_id,payload FROM incoming_updates WHERE status='queued'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][1])["message"]["text"], "/start")
        admin_control(json.loads(rows[0][1]), rows[0][0])
        self.assertFalse(bot.paused("direct-test:42"))

    def test_legacy_database_migration_preserves_profiles(self):
        legacy = str(Path(self.tmp.name) / "legacy.sqlite3")
        with sqlite3.connect(legacy) as db:
            db.execute("CREATE TABLE conversation_state (conversation_key TEXT PRIMARY KEY, phase TEXT NOT NULL DEFAULT 'profiling', retired_count INTEGER NOT NULL DEFAULT 0, local_count INTEGER NOT NULL DEFAULT 0, transcript TEXT NOT NULL DEFAULT '[]', handoff TEXT, notified INTEGER NOT NULL DEFAULT 0)")
            db.execute("INSERT INTO conversation_state(conversation_key,phase,retired_count) VALUES ('old','stopped',4)")
        with patch.object(bot, "PROFILE_DB_PATH", legacy):
            bot.initialize_storage()
            bot.initialize_storage()  # Migration is repeatable.
            self.assertEqual(bot.chat_state("old")["phase"], "stopped")
            self.assertNotIn("retired_count", {row[1] for row in self.rows("PRAGMA table_info(conversation_state)")})
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
        forget = threading.Thread(target=lambda: run(lambda: admin_control(message("/forget", 2))))
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
    b.ENABLE_DIRECT_TEST_MODE = True
    b.REPLY_DELAY_MODE = 'sequence'; b.REPLY_DELAY_SEQUENCE = (0,)
    b.MIN_TYPING_SECONDS = b.MAX_TYPING_SECONDS = b.TYPING_JITTER_SECONDS = 0
    b.POST_GENERATION_PAUSE_MIN_SECONDS = b.POST_GENERATION_PAUSE_MAX_SECONDS = 0
    b.BOT_ADMIN_USER_ID = 99
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
        env = dict(os.environ, TELEGRAM_BOT_TOKEN="", SYSTEM_PROMPT_FILE="/no-test-prompt")
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(bot.__file__).parent, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("HTTP worker integration passed", result.stdout)




    def test_adult_flag_alone_does_not_replace_numeric_age(self):
        for age in (None, True, "21", 17, 121):
            profile = dict(bot.empty_profile(), age=age, age_confirmed_adult=True)
            self.assertFalse(bot.profile_ready(profile))
        self.assertFalse(bot.profile_ready(dict(bot.empty_profile(), age=21, age_confirmed_adult=False)))



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


    def test_shorter_defaults_can_still_be_overridden(self):
        script = 'import business_bot as b; import json; print(json.dumps([b.REPLY_DELAY_MIN_SECONDS,b.REPLY_DELAY_MAX_SECONDS,b.REPLY_DELAY_SEQUENCE]))'
        environment = {"PATH": os.environ["PATH"]}
        default = subprocess.run([sys.executable,"-c",script],cwd=Path(bot.__file__).parent,env=environment,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(default.stdout),[10.0,25.0,[10.0,15.0,25.0]])
        environment.update(REPLY_DELAY_MIN_SECONDS="60",REPLY_DELAY_MAX_SECONDS="180")
        override = subprocess.run([sys.executable,"-c",script],cwd=Path(bot.__file__).parent,env=environment,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(override.stdout)[:2],[60.0,180.0])



    def test_start_has_no_automatic_privacy_notice(self):
        admin_control(message("/start"))
        self.send.assert_not_called()

    def test_privacy_details_are_available_only_on_request(self):
        admin_control(message("/privacy"))
        self.assertIn("Profiles expire after", self.send.call_args.args[2])
        self.assertIn("backups", self.send.call_args.args[2].lower())

    def test_ready_reply_recovery_preserves_answer_without_automatic_notice(self):
        job = self.schedule()
        bot.prepare_answer(job)
        job.answer = "Hello again!"
        with bot.profile_db() as db:
            bot.persist_job(db, job)

        bot.initialize_storage()
        bot.scheduler = bot.ReplyScheduler()
        bot.scheduler.restore()
        recovered = bot.scheduler.pending[job.key][0]
        self.model.reset_mock()
        bot.perform_reply(recovered)
        self.model.assert_not_called()
        self.send.assert_called_once_with(None, 42, "Hello again!")
        self.assertNotIn("AI services process these messages", bot.chat_state(job.key)["transcript"])


    def test_generated_answer_and_turn_are_persisted_before_send(self):
        job = self.schedule()
        bot.prepare_answer(job)
        self.assertEqual(job.answer, "I’m Ginger. Nice to meet you!")
        self.assertEqual(job.status, "ready")
        status, data = self.rows("SELECT status,data FROM reply_jobs WHERE job_id=?", (job.job_id,))[0]
        self.assertEqual(status, "ready")
        self.assertEqual(json.loads(data)["answer"], job.answer)
        self.assertEqual(bot.chat_state(job.key)["local_count"], 1)
        self.send.assert_not_called()

    def test_admin_start_is_silent_with_direct_test_mode_disabled(self):
        key = "direct-test:99"
        bot.chat_state(key)
        self.execute("UPDATE conversation_state SET phase='stopped' WHERE conversation_key=?", (key,))
        with patch.object(bot, "ENABLE_DIRECT_TEST_MODE", False):
            bot.process_message(message("/start", chat_id=99))
        self.assertEqual(bot.chat_state(key)["phase"], "profiling")
        self.send.assert_not_called()

    def test_admin_controls_work_with_direct_test_mode_disabled(self):
        with patch.object(bot, "ENABLE_DIRECT_TEST_MODE", False):
            bot.process_message(message("/stop", chat_id=99))
            self.assertTrue(bot.paused("direct-test:99"))
            bot.process_message(message("/privacy", 2, chat_id=99))
            self.assertIn("Profiles expire", self.send.call_args.args[2])
            bot.process_message(message("/forget", 3, chat_id=99))
            self.assertTrue(bot.paused("direct-test:99"))
        self.assertEqual(self.send.call_count, 3)

    def test_clients_cannot_execute_any_commands(self):
        for command in ("/start", "/stop", "/forget", "/privacy", "/export_profiles", "/resume_profile biz:42", "/profile @alex", "profile:alex"):
            for business in (False, True):
                with self.subTest(command=command, business=business):
                    bot.process_message(message(command, business=business))
        self.send.assert_not_called()
        self.model.assert_not_called()
        self.assertFalse(self.rows("SELECT * FROM conversation_state"))
        self.assertFalse(bot.scheduler.pending)

    def test_admin_commands_are_restricted_to_private_bot_chat(self):
        private = message("/stop", chat_id=99)
        private["message"]["chat"]["type"] = "group"
        bot.process_message(private)
        bot.process_message(message("/stop", chat_id=99, business=True))
        self.assertFalse(self.rows("SELECT * FROM conversation_state"))
        self.send.assert_not_called()

    def test_normal_business_client_message_still_schedules_with_direct_mode_off(self):
        connection = {"user":{"id":99},"is_enabled":True,"rights":{"can_reply":True}}
        with patch.object(bot,"ENABLE_DIRECT_TEST_MODE",False), patch.object(bot,"telegram",return_value=connection):
            bot.process_message(message("Hello Ginger", business=True))
        self.assertEqual(bot.scheduler.pending["biz:42"][0].text,"Hello Ginger")
        self.send.assert_not_called()


    def test_character_prompt_source_is_preserved(self):
        import ast,hashlib
        source = Path(bot.__file__).read_text()
        expected = json.loads(Path(bot.__file__).with_name("prompt_checksums.json").read_text())
        sections = {}
        for node in ast.parse(source).body:
            if isinstance(node,ast.FunctionDef) and node.name == "load_system_prompt":
                sections[node.name] = ast.get_source_segment(source,node)
            if isinstance(node,ast.FunctionDef) and node.name == "ai_reply":
                parts = []
                for item in ast.walk(node):
                    if isinstance(item,(ast.Assign,ast.AugAssign)):
                        targets = item.targets if isinstance(item,ast.Assign) else [item.target]
                        if any(isinstance(target,ast.Name) and target.id == "guidance" for target in targets):
                            parts.append((item.lineno,ast.get_source_segment(source,item.value)))
                    if isinstance(item,ast.Constant) and isinstance(item.value,str) and (item.value.startswith("Rewrite the answer") or item.value.startswith("You've got my attention")):
                        parts.append((item.lineno,item.value))
                sections["ai_reply_prompt_parts"] = json.dumps([value for _,value in sorted(parts)],ensure_ascii=False)
            if isinstance(node,ast.FunctionDef) and node.name == "extract_profile_update":
                assignment = next(item for item in node.body if isinstance(item,ast.Assign) and any(isinstance(target,ast.Name) and target.id == "instructions" for target in item.targets))
                sections["extraction_instructions"] = ast.get_source_segment(source,assignment.value)
            if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id in {"ROLEPLAY_CHARACTER","CLOSING_MESSAGE"} for target in node.targets):
                sections[node.targets[0].id] = ast.get_source_segment(source,node.value)
        self.assertEqual({name:hashlib.sha256(text.encode()).hexdigest() for name,text in sections.items()},expected)




    def test_identity_retry_keeps_local_conversation_context(self):
        self.model.side_effect=[response("I'm Nidum AI"),response("I'm Ginger")]
        bot.ai_reply([{"role":"user","content":"PRIVATE_LOCAL_TURN"},{"role":"assistant","content":"Thanks for telling me."}],"FINAL_LOCAL_ANSWER",dict(bot.empty_profile(),age=25))
        for call in self.model.call_args_list:
            self.assertIn("PRIVATE_LOCAL_TURN",json.dumps(call.args[0]))
            self.assertIn("FINAL_LOCAL_ANSWER",json.dumps(call.args[0]))









    def test_harmless_model_mentions_do_not_trigger_identity_retry(self):
        for text in ("I'm Ginger. You can run Qwen locally.", "Nidum AI is a model name, but call me Ginger.", "Have you tried Llama?", "I'm not Qwen, I'm Ginger.", "I don't have a name for that song."):
            with self.subTest(text=text):
                self.assertFalse(bot.identity_leak(text))
                self.model.reset_mock()
                self.model.return_value=response(text)
                self.assertEqual(bot.ai_reply([],"Hi",bot.empty_profile()),text)
                self.assertEqual(self.model.call_count,1)

    def test_explicit_model_introductions_still_trigger_identity_check(self):
        for text in ("I'm Nidum AI.", "I’m NidumAI", "My name is Qwen2.5", "Call me Claude.", "I am an AI assistant.", "I'm Dolphin-Mistral.", "Nidum AI", "I have no name."):
            with self.subTest(text=text):
                self.assertTrue(bot.identity_leak(text))

    def test_drift_warning_identifies_provider_without_logging_chat(self):
        self.model.side_effect=[response("I'm Nidum AI."),response("I'm Ginger.")]
        with self.assertLogs(bot.log,level="WARNING") as logs:
            bot.ai_reply([],"PRIVATE_USER_MESSAGE",bot.empty_profile())
        output="\n".join(logs.output)
        self.assertIn("model="+bot.LOCAL_MODEL_NAME,output)
        self.assertNotIn("PRIVATE_USER_MESSAGE",output)
        self.assertNotIn("I'm Nidum AI",output)



    def test_llama_context_error_is_classified_without_echoing_body(self):
        import urllib.error
        body={"error":{"type":"exceed_context_size_error","message":"request exceeds the available context size PRIVATE_CHAT_TEXT", "n_ctx":2048,"n_prompt_tokens":2800}}
        error=urllib.error.HTTPError("http://127.0.0.1:8080/v1/chat/completions",400,"Bad request",{},io.BytesIO(json.dumps(body).encode()))
        with patch.object(bot.urllib.request,"urlopen",side_effect=error):
            with self.assertRaises(bot.APIError) as caught:
                bot.post_json(bot.LOCAL_CHAT_COMPLETIONS_API,{})
        self.assertTrue(caught.exception.context_exceeded)
        self.assertIn("n_ctx=2048",str(caught.exception))
        self.assertIn("n_prompt_tokens=2800",str(caught.exception))
        self.assertNotIn("PRIVATE_CHAT_TEXT",str(caught.exception))






    def test_connection_refusal_and_timeout_have_actionable_safe_errors(self):
        import urllib.error
        for reason,expected in ((ConnectionRefusedError(111,"PRIVATE_URL"),"Connection refused"),(TimeoutError("PRIVATE_URL"),"timed out")):
            with self.subTest(reason=reason):
                with patch.object(bot.urllib.request,"urlopen",side_effect=urllib.error.URLError(reason)):
                    with self.assertRaises(bot.APIError) as caught:bot.post_json(bot.LOCAL_CHAT_COMPLETIONS_API,{})
                self.assertIn(expected,str(caught.exception))
                self.assertNotIn("PRIVATE_URL",str(caught.exception))

    def test_unknown_upstream_error_body_is_not_logged(self):
        import urllib.error
        error=urllib.error.HTTPError("https://example.com/SECRET",400,"Bad request",{},io.BytesIO(b'{"error":{"message":"PRIVATE_CHAT_TEXT and SECRET"}}'))
        with patch.object(bot.urllib.request,"urlopen",side_effect=error):
            with self.assertRaises(bot.APIError) as caught:bot.post_json(bot.LOCAL_CHAT_COMPLETIONS_API,{})
        self.assertEqual(str(caught.exception),"Upstream HTTP 400")
        self.assertFalse(caught.exception.context_exceeded)

    def test_requested_model_unavailable_is_classified_without_echo(self):
        import urllib.error
        error=urllib.error.HTTPError("https://example.com",404,"Not found",{},io.BytesIO(b'{"error":{"message":"model PRIVATE_MODEL not found"}}'))
        with patch.object(bot.urllib.request,"urlopen",side_effect=error):
            with self.assertRaises(bot.APIError) as caught:bot.post_json(bot.LOCAL_CHAT_COMPLETIONS_API,{})
        self.assertIn("requested model is unavailable",str(caught.exception))
        self.assertNotIn("PRIVATE_MODEL",str(caught.exception))


    def seed_client(self, number=42, *, age=25, username=None, name=None):
        key=f"biz:{number}"
        bot.chat_state(key)
        bot.save_client_identity(key,{"id":number,"username":username or f"alex_{number}"})
        bot.save_client_profile(key,dict(bot.empty_profile(),age=age,name=name or f"Client {number}"))
        return key

    def callback(self, data, *, sender=99, chat=99, chat_type="private", query_id="query1"):
        query={"id":query_id,"data":data,"from":{"id":sender},"message":{"message_id":7,"chat":{"id":chat,"type":chat_type}}}
        bot.handle_admin_callback(query)
        return query

    def menu_payload(self):
        return next(call.args[1] for call in reversed(self.telegram.call_args_list) if call.args[0] in {"sendMessage","editMessageText"})

    def menu_button(self, label):
        return next(button["callback_data"] for row in self.menu_payload()["reply_markup"]["inline_keyboard"] for button in row if button["text"]==label)

    def test_single_model_transport_overrides_other_model_names(self):
        with patch.object(bot,"model_request",REAL_MODEL_REQUEST),patch.object(bot,"post_json",return_value=response("I'm Ginger")) as http:
            bot.model_request({"model":"different-model","messages":[{"role":"user","content":"Hello"}]})
        self.assertEqual(http.call_args.args[0],bot.LOCAL_CHAT_COMPLETIONS_API)
        self.assertEqual(http.call_args.args[1]["model"],bot.LOCAL_MODEL_NAME)
        self.assertFalse(hasattr(bot,"PROFILE_MODEL_NAME"))

    def test_same_ginger_model_is_used_for_chat_at_all_stages(self):
        self.model.return_value=response("I'm Ginger")
        for profile in (bot.empty_profile(),dict(bot.empty_profile(),age=25)):
            bot.ai_reply([],"Hello",profile)
            self.assertEqual(self.model.call_args.args[0]["model"],bot.LOCAL_MODEL_NAME)
            self.assertNotIn("provider",self.model.call_args.kwargs)

    def test_adult_chat_has_no_removed_provider_reply_limit(self):
        job=self.schedule()
        bot.save_client_profile(job.key,dict(bot.empty_profile(),age=25,employment_status="unemployed"))
        self.execute("UPDATE conversation_state SET local_count=99 WHERE conversation_key=?",(job.key,))
        bot.perform_reply(job)
        self.assertEqual(bot.chat_state(job.key)["local_count"],100)
        self.assertEqual(bot.chat_state(job.key)["phase"],"active")
        self.assertIsNone(job.closing_phase)
        self.send.assert_called_once()

    def test_local_cli_runs_without_old_credentials(self):
        with patch.object(bot,"TELEGRAM_BOT_TOKEN","test-only"),patch.object(bot,"WEBHOOK_URL",""),patch.object(bot,"start_workers"),patch.object(bot,"run_polling"),patch.object(sys,"argv",["business_bot.py"]):
            bot.main()

    def test_admin_dashboard_displays_collected_clients_and_never_calls_model(self):
        self.seed_client()
        with patch.object(bot,"ENABLE_DIRECT_TEST_MODE",False):bot.process_message(message("/admin",chat_id=99))
        payload=self.menu_payload()
        self.assertEqual(payload["chat_id"],99)
        self.assertIn("Collected clients: 1",payload["text"])
        self.assertIn("All clients",str(payload["reply_markup"]))
        self.model.assert_not_called()
        self.assertNotIn("direct-test:99",bot.scheduler.pending)

    def test_nonadmin_cannot_open_dashboard_or_burn_callback_token(self):
        key=self.seed_client()
        data=bot.admin_button("Delete","delete_execute",key=key)["callback_data"]
        bot.process_message(message("/admin"))
        self.callback(data,sender=42,chat=42)
        self.assertTrue(bot.client_exists(key))
        self.assertIn(data[4:],bot.admin_tokens)
        self.assertEqual(self.telegram.call_args.args[0],"answerCallbackQuery")
        self.assertIn("Not authorized",self.telegram.call_args.args[1]["text"])

    def test_callbacks_require_both_private_chat_and_matching_admin_id(self):
        key=self.seed_client()
        data=bot.admin_button("Pause","pause",key=key)["callback_data"]
        for sender,chat,kind in ((99,42,"private"),(99,99,"group"),(42,99,"private")):
            self.callback(data,sender=sender,chat=chat,chat_type=kind)
            self.assertFalse(bot.paused(key))
        self.assertIn(data[4:],bot.admin_tokens)

    def test_menu_tokens_expire_and_are_single_use(self):
        key=self.seed_client()
        data=bot.admin_button("Pause","pause",key=key)["callback_data"]
        self.callback(data)
        self.assertTrue(bot.paused(key))
        self.callback(data)
        self.assertIn("Menu expired",self.telegram.call_args.args[1]["text"])
        data=bot.admin_button("Resume","resume",key=key)["callback_data"]
        with patch.object(bot.time,"monotonic",return_value=time.monotonic()+bot.ADMIN_STATE_SECONDS+1):
            self.callback(data)
        self.assertTrue(bot.paused(key))

    def test_long_keys_use_short_opaque_callback_data(self):
        key="connection-"+"x"*180+":42"
        bot.save_client_identity(key,{"id":42,"username":"alex"});bot.chat_state(key)
        bot.admin_list()
        payload=self.menu_payload()
        for row in payload["reply_markup"]["inline_keyboard"]:
            for button in row:
                if "callback_data" in button:
                    self.assertLessEqual(len(button["callback_data"].encode()),64)
                    self.assertNotIn(key,button["callback_data"])

    def test_client_list_paginates_and_filters(self):
        for number in range(19):self.seed_client(number,age=None if number==0 else 25)
        bot.admin_set_paused("biz:1",True)
        rows,total,page=bot.client_rows(page=2)
        self.assertEqual((len(rows),total,page),(3,19,2))
        self.assertEqual(bot.client_rows(scope="paused")[1],1)
        self.assertEqual(bot.client_rows(scope="active")[1],18)
        self.assertEqual(bot.client_rows(scope="age")[1],1)
        self.assertEqual(bot.client_rows(page=999)[2],2)

    def test_search_matches_name_username_id_and_literal_wildcards(self):
        self.seed_client(42,name="Alice Example",username="alex_42")
        self.seed_client(43,name="Bob",username="alexX42")
        for query in ("Alice","@alex_42","biz:42"):
            self.assertEqual(bot.client_rows(query=query)[1],1)
        self.assertEqual(bot.client_rows(query="%")[1],0)
        self.assertEqual(bot.client_rows(query="' OR 1=1 --")[1],0)

    def test_search_text_is_consumed_by_admin_menu_with_direct_mode_off(self):
        self.seed_client(42,name="Alice")
        with patch.object(bot,"ENABLE_DIRECT_TEST_MODE",False):
            bot.process_message(message("/search",chat_id=99))
            bot.process_message(message("Alice",2,chat_id=99))
        self.assertIn("1 matches",self.menu_payload()["text"])
        self.model.assert_not_called()
        self.assertFalse(bot.scheduler.pending)

    def test_client_details_have_profile_navigation_and_telegram_link(self):
        key=self.seed_client()
        profile=bot.load_client_profile(key)
        profile.update(favorite_music="techno",work_or_studies="work",occupation="chef",field_of_study="math",interests=["music"])
        bot.save_client_profile(key,profile)
        bot.admin_client(key)
        payload=self.menu_payload()
        self.assertIn("self-reported",payload["text"])
        self.assertIn("More profile",str(payload["reply_markup"]))
        self.assertIn("https://t.me/alex_42",str(payload["reply_markup"]))
        self.assertLessEqual(len(payload["text"]),3000)

    def test_pause_and_resume_cancel_backlog_without_sending_to_client(self):
        key=self.seed_client()
        job=bot.scheduler.add(key,"biz",42,"pending")
        bot.enqueue_update(message("queued",business=True))
        self.callback(bot.admin_button("Pause","pause",key=key)["callback_data"])
        self.assertTrue(job.cancelled.is_set())
        self.assertTrue(bot.paused(key))
        self.assertFalse(self.rows("SELECT 1 FROM incoming_updates WHERE conversation_key=? AND status='queued'",(key,)))
        self.callback(bot.admin_button("Resume","resume",key=key)["callback_data"],query_id="q2")
        self.assertFalse(bot.paused(key))
        self.assertEqual(bot.load_client_profile(key)["name"],"Client 42")
        self.send.assert_not_called()

    def test_deletion_requires_confirmation_then_clears_live_data(self):
        key=self.seed_client()
        self.execute("INSERT INTO admin_notes(conversation_key,note,created_at) VALUES (?,?,?)",(key,"note",time.time()))
        bot.scheduler.add(key,"biz",42,"pending")
        self.callback(bot.admin_button("Delete","delete_confirm",key=key)["callback_data"])
        self.assertTrue(bot.client_exists(key))
        confirm=self.menu_button("Confirm deletion")
        self.callback(confirm,query_id="q2")
        self.assertFalse(bot.client_exists(key))
        self.assertFalse(bot.get_admin_notes(key))
        self.assertFalse(self.rows("SELECT 1 FROM reply_jobs WHERE conversation_key=?",(key,)))
        self.assertTrue(bot.paused(key))
        self.send.assert_not_called()
        with patch.object(bot,"ENABLE_DIRECT_TEST_MODE",False):bot.process_message(message("/resume_profile "+key,chat_id=99))
        self.assertFalse(bot.paused(key))

    def test_cancel_deletion_keeps_profile(self):
        key=self.seed_client()
        self.callback(bot.admin_button("Delete","delete_confirm",key=key)["callback_data"])
        self.callback(self.menu_button("Cancel"),query_id="q2")
        self.assertTrue(bot.client_exists(key))

    def test_notes_are_admin_only_bounded_and_not_sent_to_model(self):
        key=self.seed_client()
        self.callback(bot.admin_button("Note","note",key=key)["callback_data"])
        with patch.object(bot,"ENABLE_DIRECT_TEST_MODE",False):bot.process_message(message("INTERNAL_ADMIN_NOTE password=secret123",chat_id=99))
        notes=bot.get_admin_notes(key)
        self.assertEqual(len(notes),1)
        self.assertNotIn("secret123",notes[0])
        bot.ai_reply([],"Hello",bot.load_client_profile(key))
        self.assertNotIn("INTERNAL_ADMIN_NOTE",json.dumps(self.model.call_args.args[0]))
        self.assertEqual(bot.client_rows(query="INTERNAL_ADMIN_NOTE")[1],1)

    def test_csv_includes_identity_only_clients_and_admin_notes(self):
        key="biz:42";bot.save_client_identity(key,{"id":42,"username":"alex"})
        self.execute("INSERT INTO admin_notes(conversation_key,note,created_at) VALUES (?,?,?)",(key,"=formula",time.time()))
        rows=list(csv.DictReader(io.StringIO(bot.profiles_csv().decode("utf-8-sig"))))
        self.assertEqual(rows[0]["telegram_user_id"],"42")
        self.assertEqual(rows[0]["admin_notes"],"'=formula")

    def test_export_button_sends_csv_only_to_configured_admin(self):
        self.seed_client()
        with patch.object(bot,"send_profile_csv") as export:
            self.callback(bot.admin_button("Export","export")["callback_data"])
        export.assert_called_once_with()
        self.assertEqual(self.menu_payload()["chat_id"],99)

    def test_callback_is_durable_deduplicated_and_does_not_collect_admin(self):
        key=self.seed_client()
        data=bot.admin_button("Pause","pause",key=key)["callback_data"]
        query={"id":"cb1","data":data,"from":{"id":99},"message":{"message_id":1,"chat":{"id":99,"type":"private"}}}
        bot.enqueue_update({"callback_query":query});bot.enqueue_update({"callback_query":query})
        records=self.rows("SELECT receipt_id,payload FROM incoming_updates")
        self.assertEqual(len(records),1)
        self.assertEqual(records[0][0],"callback:cb1")
        self.assertFalse(self.rows("SELECT 1 FROM conversation_state WHERE conversation_key='admin:99'"))

    def test_invalid_callback_metadata_is_rejected(self):
        query={"id":"cb1","data":"x"*65,"from":{"id":99},"message":{"message_id":1,"chat":{"id":99,"type":"private"}}}
        with self.assertRaises(ValueError):bot.enqueue_update({"callback_query":query})

    def test_menu_and_sessions_are_bounded_and_cancelled(self):
        with patch.object(bot,"ADMIN_MAX_TOKENS",4):
            for _ in range(8):bot.admin_button("Menu","dashboard")
        self.assertLessEqual(len(bot.admin_tokens),4)
        bot.admin_begin_input("search")
        bot.process_message(message("/cancel",chat_id=99))
        self.assertNotIn(99,bot.admin_sessions)

    def test_upgrade_creates_one_recoverable_backup_and_preserves_old_rows(self):
        legacy=Path(self.tmp.name)/"old.sqlite3"
        with sqlite3.connect(legacy) as db:
            db.execute("CREATE TABLE conversation_state(conversation_key TEXT PRIMARY KEY,phase TEXT NOT NULL DEFAULT 'profiling',local_count INTEGER NOT NULL DEFAULT 0,transcript TEXT NOT NULL DEFAULT '[]')")
            db.execute("INSERT INTO conversation_state(conversation_key,phase) VALUES ('biz:42','stopped')")
        with patch.object(bot,"PROFILE_DB_PATH",str(legacy)):
            bot.initialize_storage();bot.initialize_storage()
        backups=list(Path(self.tmp.name).glob("old.sqlite3.pre-local-menu-*.sqlite3"))
        self.assertEqual(len(backups),1)
        with sqlite3.connect(backups[0]) as db:self.assertEqual(db.execute("SELECT phase FROM conversation_state WHERE conversation_key='biz:42'").fetchone()[0],"stopped")

    def test_restart_cancels_prepared_replies_from_removed_provider(self):
        job=self.schedule()
        bot.prepare_answer(job)
        status,data=self.rows("SELECT status,data FROM reply_jobs")[0]
        values=json.loads(data);values.update(provider="retired_remote",profile_handoff=True)
        self.execute("UPDATE reply_jobs SET data=?",(json.dumps(values),))
        bot.scheduler=bot.ReplyScheduler();bot.scheduler.restore()
        self.assertFalse(bot.scheduler.pending)
        self.assertEqual(self.rows("SELECT status FROM reply_jobs"),[("cancelled",)])
        self.send.assert_not_called()

    def test_restart_keeps_legacy_local_replies_without_provider_fields(self):
        job=self.schedule();bot.prepare_answer(job)
        values=json.loads(self.rows("SELECT data FROM reply_jobs")[0][0]);values.update(provider="local",profile_handoff=False)
        self.execute("UPDATE reply_jobs SET data=?",(json.dumps(values),))
        bot.scheduler=bot.ReplyScheduler();bot.scheduler.restore()
        restored=bot.scheduler.pending[job.key][0]
        self.assertEqual(restored.answer,job.answer)
        self.assertFalse(hasattr(restored,"provider"))


    def test_callback_navigation_cancels_pending_note_input(self):
        key=self.seed_client()
        bot.admin_begin_input("note",key)
        self.callback(bot.admin_button("Clients","list")["callback_data"])
        self.assertNotIn(99,bot.admin_sessions)
        with patch.object(bot,"ENABLE_DIRECT_TEST_MODE",False):bot.process_message(message("This is not a client note",chat_id=99))
        self.assertFalse(bot.get_admin_notes(key))

    def test_empty_admin_message_does_not_consume_note_session(self):
        key=self.seed_client();bot.admin_begin_input("note",key)
        update=message("",chat_id=99)
        self.assertFalse(bot.handle_admin_message(update["message"]))
        self.assertIn(99,bot.admin_sessions)
        self.assertFalse(bot.get_admin_notes(key))

    def test_callback_acknowledgement_failure_does_not_drop_admin_action(self):
        key=self.seed_client()
        def telegram(method,payload,**kwargs):
            if method=="answerCallbackQuery":raise bot.APIError("temporary acknowledgement failure")
            return True
        self.telegram.side_effect=telegram
        self.callback(bot.admin_button("Pause","pause",key=key)["callback_data"])
        self.assertTrue(bot.paused(key))

    def test_webhook_accepts_authenticated_callback_and_persists_it(self):
        with patch.object(bot,"WEBHOOK_SECRET","test-secret"):
            server=bot.LimitedHTTPServer(("127.0.0.1",0),bot.WebhookHandler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            query={"id":"cb1","data":"adm:token","from":{"id":99},"message":{"message_id":1,"chat":{"id":99,"type":"private"}}}
            try:
                conn=http.client.HTTPConnection(*server.server_address,timeout=5)
                conn.request("POST","/telegram/test-secret",json.dumps({"callback_query":query}),{"X-Telegram-Bot-Api-Secret-Token":"test-secret"})
                reply=conn.getresponse();self.assertEqual(reply.status,200);reply.read();conn.close()
                self.assertEqual(self.rows("SELECT receipt_id,status FROM incoming_updates"),[("callback:cb1","queued")])
            finally:
                server.shutdown();server.server_close();thread.join(2)

    def test_full_http_worker_admin_menu_pause_resume_and_local_adult_chat(self):
        script=r'''
import http.server,json,os,tempfile,threading,time
import business_bot as b
class API(http.server.BaseHTTPRequestHandler):
    calls=[]
    models=[]
    def do_POST(self):
        payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        API.calls.append((self.path,payload))
        if self.path=='/v1/chat/completions':
            API.models.append(payload['model'])
            if payload['messages'][0]['content'].startswith('You extract client profile changes'):
                assert payload['max_tokens']==400
                text=json.dumps({'profile_update':{'name':'Alex','age':25}})
            else:text="I'm Ginger. Lovely to chat with you!"
            result={'choices':[{'message':{'content':text}}]}
        else:result={'ok':True,'result':{'message_id':10}}
        data=json.dumps(result).encode()
        self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def log_message(self,*args):pass

def wait_for(predicate):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        value=predicate()
        if value:return value
        time.sleep(.01)
    raise AssertionError('Timed out waiting for worker operation')
def latest_menu(prefix):
    return next((payload for path,payload in reversed(API.calls) if path.endswith(('/sendMessage','/editMessageText')) and payload.get('chat_id')==99 and payload.get('text','').startswith(prefix)),None)
def button(payload,label):
    return next(item['callback_data'] for row in payload['reply_markup']['inline_keyboard'] for item in row if item['text']==label)
def cb(data,number):
    b.enqueue_update({'callback_query':{'id':'cb'+str(number),'data':data,'from':{'id':99},'message':{'message_id':10,'chat':{'id':99,'type':'private'}}}})
def client(text,number):
    b.enqueue_update({'message':{'message_id':number,'chat':{'id':42,'type':'private'},'from':{'id':42,'username':'alex'},'text':text}})
with tempfile.TemporaryDirectory() as tmp:
    b.PROFILE_DB_PATH=os.path.join(tmp,'state.sqlite3');b.BOT_ADMIN_USER_ID=99
    b.ENABLE_DIRECT_TEST_MODE=True
    b.REPLY_DELAY_MODE='sequence';b.REPLY_DELAY_SEQUENCE=(0,)
    b.MIN_TYPING_SECONDS=b.MAX_TYPING_SECONDS=b.TYPING_JITTER_SECONDS=0
    b.POST_GENERATION_PAUSE_MIN_SECONDS=b.POST_GENERATION_PAUSE_MAX_SECONDS=0
    api=http.server.ThreadingHTTPServer(('127.0.0.1',0),API)
    threading.Thread(target=api.serve_forever,daemon=True).start()
    base='http://127.0.0.1:'+str(api.server_port)
    b.TELEGRAM_API=base+'/botTEST';b.LOCAL_CHAT_COMPLETIONS_API=base+'/v1/chat/completions'
    b.initialize_storage();b.start_workers();client('I am Alex and I am 25',1)
    wait_for(lambda:b.chat_state('direct-test:42')['local_count']==1)
    b.enqueue_update({'message':{'message_id':11,'chat':{'id':99,'type':'private'},'from':{'id':99},'text':'/admin'}})
    dashboard=wait_for(lambda:latest_menu('Ginger · Client manager'))
    cb(button(dashboard,'All clients'),1)
    listing=wait_for(lambda:latest_menu('Clients ·'))
    cb(listing['reply_markup']['inline_keyboard'][0][0]['callback_data'],2)
    detail=wait_for(lambda:latest_menu('Client:'))
    cb(button(detail,'Set reported age'),22)
    wait_for(lambda:b.admin_sessions.get(99,{}).get('action')=='age')
    b.enqueue_update({'message':{'message_id':12,'chat':{'id':99,'type':'private'},'from':{'id':99},'text':'25'}})
    wait_for(lambda:b.load_client_profile('direct-test:42')['age']==25)
    detail=wait_for(lambda:latest_menu('Client:') if 'Age: 25' in (latest_menu('Client:') or {}).get('text','') else None)
    cb(button(detail,'Pause'),3)
    wait_for(lambda:b.paused('direct-test:42'))
    wait_for(lambda:'Paused / needs review' in (latest_menu('Client:') or {}).get('text',''))
    cb(button(latest_menu('Client:'),'Resume'),4)
    wait_for(lambda:not b.paused('direct-test:42'))
    client_sends=[payload for path,payload in API.calls if path.endswith('/sendMessage') and payload.get('chat_id')==42]
    assert len(client_sends)==1,client_sends
    client('Hello again',2)
    wait_for(lambda:len([p for path,p in API.calls if path.endswith('/sendMessage') and p.get('chat_id')==42])==2)
    assert API.models and set(API.models)=={b.LOCAL_MODEL_NAME},API.models
    assert len(API.models)==2,API.models
    assert all(thread.is_alive() for thread in b.runtime_threads)
    b.scheduler.shutdown();api.shutdown();api.server_close()
print('Local-only profile/chat and admin callback integration passed')
'''
        environment=dict(os.environ,TELEGRAM_BOT_TOKEN="",SYSTEM_PROMPT_FILE="/no-test-prompt")
        result=subprocess.run([sys.executable,"-c",script],cwd=Path(bot.__file__).parent,env=environment,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn("admin callback integration passed",result.stdout)


    def test_navigation_commands_cancel_pending_note_for_other_client(self):
        first=self.seed_client(42);second=self.seed_client(43)
        commands=("/profile @alex_43","/search Client","/resume_profile "+second,"/export_profiles","/privacy","/unknown")
        with patch.object(bot,"send_profile_csv"):
            for command in commands:
                with self.subTest(command=command):
                    bot.admin_begin_input("note",first)
                    bot.handle_admin_message(message(command,chat_id=99)["message"])
                    self.assertNotIn(99,bot.admin_sessions)
                    with patch.object(bot,"ENABLE_DIRECT_TEST_MODE",False):
                        bot.process_message(message("Not a note for client 42",chat_id=99))
                    self.assertFalse(bot.get_admin_notes(first))

    def test_upgrade_removes_retired_live_payload_columns_after_backup(self):
        legacy=Path(self.tmp.name)/"retired.sqlite3"
        with sqlite3.connect(legacy) as db:
            db.execute("CREATE TABLE conversation_state(conversation_key TEXT PRIMARY KEY,phase TEXT NOT NULL DEFAULT 'profiling',local_count INTEGER NOT NULL DEFAULT 0,transcript TEXT NOT NULL DEFAULT '[]',retired_payload TEXT)")
            db.execute("INSERT INTO conversation_state(conversation_key,phase,transcript,retired_payload) VALUES (?,?,?,?)",("biz:42","stopped",'[{"role":"user","content":"Keep supported context"}]',"RETIRED_PRIVATE_CHAT"))
        with patch.object(bot,"PROFILE_DB_PATH",str(legacy)):
            bot.initialize_storage()
            self.assertEqual({row[1] for row in self.rows("PRAGMA table_info(conversation_state)")},set(bot.CONVERSATION_DEFINITIONS))
            self.assertIn("Keep supported context",bot.chat_state("biz:42")["transcript"])
            bot.delete_client_profile("biz:42")
            self.assertEqual(bot.chat_state("biz:42")["transcript"],"[]")
        backup=next(Path(self.tmp.name).glob("retired.sqlite3.pre-local-menu-*.sqlite3"))
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute("SELECT retired_payload FROM conversation_state").fetchone()[0],"RETIRED_PRIVATE_CHAT")

    def test_callback_retries_keep_token_when_database_action_fails(self):
        key=self.seed_client()
        data=bot.admin_button("Pause","pause",key=key)["callback_data"]
        original=bot.admin_set_paused
        count=0
        def transient(*args):
            nonlocal count
            count+=1
            if count==1:raise sqlite3.OperationalError("database locked")
            return original(*args)
        with patch.object(bot,"admin_set_paused",side_effect=transient):
            with self.assertRaises(sqlite3.OperationalError):self.callback(data)
            self.assertIn(data[4:],bot.admin_tokens)
            self.assertFalse(bot.paused(key))
            self.callback(data)
        self.assertTrue(bot.paused(key))
        self.assertNotIn(data[4:],bot.admin_tokens)

    def test_callback_render_retry_does_not_repeat_committed_resume_or_drop_new_input(self):
        key=self.seed_client();bot.admin_set_paused(key,True)
        data=bot.admin_button("Resume","resume",key=key)["callback_data"]
        original_client=bot.admin_client
        with patch.object(bot,"admin_client",side_effect=[sqlite3.OperationalError("database locked"),None]),patch.object(bot,"admin_set_paused",wraps=bot.admin_set_paused) as resume:
            with self.assertRaises(sqlite3.OperationalError):self.callback(data)
            self.assertFalse(bot.paused(key))
            bot.enqueue_update(message("New message after resume",business=True))
            self.callback(data)
            self.assertEqual(resume.call_count,1)
        self.assertTrue(self.rows("SELECT 1 FROM incoming_updates WHERE conversation_key=? AND status='queued'",(key,)))


    def test_screenshot_punctuation_loop_is_rejected_and_rewritten(self):
        self.model.side_effect=[response('/'*160),response("That’s sweet of you 😌 How has your day been?")]
        answer=bot.ai_reply([],"Your lovely smile",dict(bot.empty_profile(),name="Dimi",age=25))
        self.assertNotIn('/'*20,answer)
        self.assertEqual(self.model.call_count,2)
        self.assertIn("meaningful words",self.model.call_args.args[0]["messages"][0]["content"])

    def test_repeated_invalid_output_uses_clean_fallback(self):
        self.model.return_value=response('/'*160)
        answer=bot.ai_reply([],"Hello",bot.empty_profile())
        self.assertIsNone(bot.reply_problem(answer,bot.empty_profile()))
        self.assertEqual(self.model.call_count,2)

    def test_normal_urls_emojis_and_markdown_are_not_repetition(self):
        for text in ("https://example.com/path/to/music", "Aww 😌💕", "I like R&B / techno.", "**That sounds lovely!**", "Yes! "*3):
            with self.subTest(text=text):self.assertFalse(bot.repeated_output(text))
        self.assertTrue(bot.repeated_output("yes "*30))

    def test_screenshot_wrong_vocative_is_corrected_without_blocking_self_intro(self):
        profile=dict(bot.empty_profile(),name="Dimi",age=25)
        bad="okay so ginger, what keeps you busy over there?"
        self.assertEqual(bot.reply_problem(bad,profile),"role_confusion")
        for text in ("I'm Ginger, lovely to meet you!","You can call me Ginger.","Ginger is a spice."):
            self.assertIsNone(bot.reply_problem(text,profile))
        self.model.side_effect=[response(bad),response("Dimi, what music do you enjoy playing?")]
        answer=bot.ai_reply([],"I'm a musician",profile)
        self.assertNotIn("so ginger",answer.lower())
        self.assertIn('"client_name": "Dimi"',self.model.call_args.args[0]["messages"][0]["content"])

    def test_clients_who_explicitly_share_the_name_ginger_are_allowed(self):
        self.assertTrue(bot.client_explicitly_named_ginger("My name is Ginger"))
        self.assertFalse(bot.client_explicitly_named_ginger("Hi Ginger"))
        self.assertTrue(bot.client_explicitly_named_ginger("Ginger",[{"role":"assistant","content":"What's your name?"}]))
        self.assertFalse(bot.client_explicitly_named_ginger("Ginger",[{"role":"assistant","content":"How are you?"}]))
        self.model.return_value=response("Hi Ginger, lovely to meet you!")
        bot.ai_reply([],"My name is Ginger",dict(bot.empty_profile(),name="Ginger",age=25))
        self.assertEqual(self.model.call_count,1)

    def test_screenshot_freelance_writer_job_is_not_invented(self):
        bad="ohh hello dimi, lovely to meet you! i'm a freelance writer by day and a bit of a night owl when it comes to music 😌"
        profile=dict(bot.empty_profile(),name="Dimi",age=25)
        self.assertEqual(bot.reply_problem(bad,profile),"unsupported_occupation")
        self.model.side_effect=[response(bad),response("Music is more my thing 😌 What do you enjoy?")]
        answer=bot.ai_reply([],"What do you do for work?",profile)
        self.assertNotIn("freelance writer",answer)
        self.assertEqual(self.model.call_count,2)

    def test_explicitly_configured_character_job_is_allowed(self):
        with patch.object(bot,"CHARACTER_OCCUPATION","freelance writer"):
            self.assertIsNone(bot.reply_problem("I'm a freelance writer by day.",bot.empty_profile()))
            self.assertEqual(bot.reply_problem("I'm a nurse.",bot.empty_profile()),"unsupported_occupation")
            self.assertIn('"character_occupation": "freelance writer"',bot.character_boundaries(bot.empty_profile(),False))

    def test_client_occupation_does_not_become_gingers_job(self):
        profile=dict(bot.empty_profile(),age=25,occupation="musician")
        self.assertIsNone(bot.reply_problem("You're a musician? How did you get into that?",profile))
        self.assertEqual(bot.reply_problem("I'm a musician too!",profile),"unsupported_occupation")
        self.assertIn("client profile describes the client, never you",bot.character_boundaries(profile,False))


    def test_ambiguous_saved_ginger_name_is_omitted_from_model_context_only(self):
        profile=dict(bot.empty_profile(),name="Ginger",age=25,interests=["music"])
        bot.ai_reply([],"Hi",profile)
        system=self.model.call_args.args[0]["messages"][0]["content"]
        self.assertNotIn('"name": "Ginger"',system)
        self.assertIn('"client_name": null',system)
        self.assertEqual(profile["name"],"Ginger")
        self.assertIn("music",system)

    def test_bad_saved_turns_are_not_fed_back_and_roles_remain_alternating(self):
        history=[{"role":"user","content":"What do you do?"},{"role":"assistant","content":"I'm a freelance writer by day."},
                 {"role":"user","content":"Hello"},{"role":"assistant","content":"/"*160},
                 {"role":"user","content":"I enjoy music"},{"role":"assistant","content":"What kind of music?"}]
        bot.ai_reply(history,"Techno",dict(bot.empty_profile(),age=25,name="Dimi"))
        payload=self.model.call_args.args[0]
        encoded=json.dumps(payload)
        self.assertNotIn("freelance writer by day",encoded)
        self.assertNotIn('/'*20,encoded)
        self.assertIn("What kind of music?",encoded)
        self.assertEqual([message["role"] for message in payload["messages"]],["system","user","assistant","user"])

    def test_retry_uses_one_leading_system_message_for_strict_chat_templates(self):
        self.model.side_effect=[response('/'*160),response("Hi there!")]
        bot.ai_reply([],"Hello",bot.empty_profile())
        messages=self.model.call_args.args[0]["messages"]
        self.assertEqual([item["role"] for item in messages],["system","user"])
        self.assertEqual(messages[-1]["content"],"Hello")
        self.assertEqual(self.model.call_args.args[0]["frequency_penalty"],bot.CHAT_FREQUENCY_PENALTY)

    def test_prompt_markers_and_multiple_generated_speakers_are_rejected(self):
        for text in ("<|im_start|>assistant Hello","[INST]Hello[/INST]","User: Hello\nAssistant: Hi!"):
            self.assertIn(bot.reply_problem(text,bot.empty_profile()),{"template_tokens","multiple_speakers"})

    def test_old_ready_garbage_is_repaired_without_charging_another_turn(self):
        job=self.schedule();bot.prepare_answer(job)
        count=bot.chat_state(job.key)["local_count"]
        job.answer='/'*160
        with bot.profile_db() as db:bot.persist_job(db,job)
        bot.scheduler=bot.ReplyScheduler();bot.scheduler.restore()
        recovered=bot.scheduler.pending[job.key][0]
        self.model.return_value=response("Hi, lovely to chat with you.")
        bot.perform_reply(recovered)
        self.assertEqual(bot.chat_state(job.key)["local_count"],count)
        self.assertNotIn('/'*20,self.send.call_args.args[2])
        self.assertNotIn('/'*20,bot.chat_state(job.key)["transcript"])

    def test_only_accepted_reply_is_saved_to_conversation(self):
        job=self.schedule()
        self.model.side_effect=[response('/'*160),response("Hi there!")]
        bot.perform_reply(job)
        transcript=bot.chat_state(job.key)["transcript"]
        self.assertNotIn('/'*20,transcript)
        self.assertIn("Hi there!",transcript)


    def test_normal_chat_has_one_generation_and_no_automatic_profile_write(self):
        job=self.schedule("My name is Alex and I am 25")
        bot.perform_reply(job)
        self.assertEqual(self.model.call_count,1)
        self.assertFalse(self.rows("SELECT * FROM client_profiles"))
        self.assertTrue(self.rows("SELECT * FROM client_identities"))
        self.assertIn("My name is Alex",bot.chat_state(job.key)["transcript"])

    def test_existing_saved_details_remain_unchanged_during_chat(self):
        job=self.schedule("Actually I'm 30 and my favourite music changed")
        bot.save_client_profile(job.key,dict(bot.empty_profile(),name="Alex",age=25,favorite_music="techno"))
        before=bot.load_client_profile(job.key)
        bot.perform_reply(job)
        self.assertEqual(bot.load_client_profile(job.key),before)
        self.assertEqual(self.model.call_count,1)

    def test_profile_extraction_function_settings_and_cli_are_removed(self):
        for name in ("extract_profile_update","PROFILE_EXTRACTION_ENABLED","PROFILING_REPLY_LIMIT"):
            self.assertFalse(hasattr(bot,name))
        with patch.object(sys,"argv",["business_bot.py","--test-profile-model"]),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:bot.main()
        self.assertEqual(error.exception.code,2)

    def test_unknown_age_chat_has_no_automatic_profiling_reply_limit(self):
        job=self.schedule()
        self.execute("UPDATE conversation_state SET local_count=99 WHERE conversation_key=?",(job.key,))
        bot.perform_reply(job)
        self.assertFalse(bot.paused(job.key))
        self.assertIsNone(job.closing_phase)
        self.assertEqual(bot.chat_state(job.key)["local_count"],100)
        self.assertEqual(self.model.call_count,1)

    def test_admin_can_record_reported_age_without_any_model_call(self):
        first=self.seed_client(42,age=None);second=self.seed_client(43,age=None)
        self.callback(bot.admin_button("Age","age",key=first)["callback_data"])
        bot.process_message(message("25",chat_id=99))
        self.assertEqual(bot.load_client_profile(first)["age"],25)
        self.assertTrue(bot.load_client_profile(first)["age_confirmed_adult"])
        self.assertIsNone(bot.load_client_profile(second)["age"])
        self.model.assert_not_called()

    def test_invalid_manual_age_is_rejected_and_clear_removes_it(self):
        key=self.seed_client()
        self.callback(bot.admin_button("Age","age",key=key)["callback_data"])
        bot.process_message(message("400",chat_id=99))
        self.assertEqual(bot.load_client_profile(key)["age"],25)
        self.assertIn(99,bot.admin_sessions)
        bot.process_message(message("clear",2,chat_id=99))
        self.assertIsNone(bot.load_client_profile(key)["age"])
        self.assertIsNone(bot.load_client_profile(key)["age_confirmed_adult"])
        self.model.assert_not_called()

    def test_no_intake_question_instructions_are_added_to_chat(self):
        bot.ai_reply([],"Hello",bot.empty_profile())
        text=self.model.call_args.args[0]["messages"][0]["content"]
        self.assertNotIn("ask once naturally for their age",text)
        self.assertIn("Do not start a profiling questionnaire",text)
        self.assertIn("Keep this phase SFW",text)


    def test_manual_underage_update_cancels_old_prepared_reply_immediately(self):
        job=self.schedule()
        bot.save_client_profile(job.key,dict(bot.empty_profile(),age=25))
        bot.prepare_answer(job)
        self.assertIsNotNone(job.answer)
        bot.admin_set_reported_age(job.key,{"age":17})
        self.assertTrue(job.cancelled.is_set())
        self.assertEqual(bot.chat_state(job.key)["phase"],"manual_review")
        self.assertEqual(bot.chat_state(job.key)["transcript"],"[]")
        bot.perform_reply(job)
        self.send.assert_not_called()

    def test_clearing_age_cancels_old_context_but_keeps_future_chat_available_sfw(self):
        job=self.schedule()
        bot.save_client_profile(job.key,dict(bot.empty_profile(),age=25))
        bot.prepare_answer(job)
        bot.admin_set_reported_age(job.key,{"age":None,"age_confirmed_adult":None})
        self.assertTrue(job.cancelled.is_set())
        self.assertFalse(bot.paused(job.key))
        self.assertEqual(bot.chat_state(job.key)["transcript"],"[]")
        bot.perform_reply(job)
        self.send.assert_not_called()

    def test_adult_age_update_does_not_automatically_resume_a_paused_chat(self):
        key=self.seed_client(42,age=17)
        self.execute("UPDATE conversation_state SET phase='manual_review',review_reason='Reported age is under 18' WHERE conversation_key=?",(key,))
        bot.admin_set_reported_age(key,{"age":25})
        self.assertTrue(bot.paused(key))
        self.assertTrue(bot.profile_ready(bot.load_client_profile(key)))
        self.assertIn("Age updated",bot.chat_state(key)["review_reason"])

    def test_self_introductions_and_occupation_idioms_remain_valid(self):
        for text in ("Hi! Ginger here.","Hey, Ginger here!","Hello, Ginger speaking.","I'm a model citizen, most days.","I'm a student of life."):
            with self.subTest(text=text):self.assertIsNone(bot.reply_problem(text,bot.empty_profile()))
        self.assertEqual(bot.reply_problem("Hi Ginger, here is a question.",bot.empty_profile()),"role_confusion")

    def test_meaningful_formatted_text_with_separator_is_not_a_punctuation_loop(self):
        text="Here is the short divider you asked for:\n----------------------\nWe can carry on chatting now."
        self.assertFalse(bot.repeated_output(text))
        self.assertTrue(bot.repeated_output('/'*160))


    def test_retained_ready_reply_cannot_bypass_an_existing_underage_record(self):
        job=self.schedule()
        bot.save_client_profile(job.key,dict(bot.empty_profile(),age=17))
        job.answer="An old prepared answer";job.status="ready";job.generated=True
        with bot.profile_db() as db:bot.persist_job(db,job)
        bot.perform_reply(job)
        self.send.assert_not_called()
        self.model.assert_not_called()
        self.assertEqual(bot.chat_state(job.key)["phase"],"manual_review")
        self.assertEqual(self.rows("SELECT status FROM reply_jobs WHERE job_id=?",(job.job_id,)),[("cancelled",)])

    def test_old_unknown_age_quota_closing_is_regenerated_without_pausing(self):
        job=self.schedule()
        job.answer="Thanks for chatting 💖 I’m wrapping up here for now."
        job.closing_phase="manual_review";job.generated=True;job.status="ready"
        with bot.profile_db() as db:bot.persist_job(db,job)
        bot.scheduler=bot.ReplyScheduler();bot.scheduler.restore()
        recovered=bot.scheduler.pending[job.key][0]
        self.assertIsNone(recovered.answer)
        self.assertIsNone(recovered.closing_phase)
        bot.perform_reply(recovered)
        self.assertFalse(bot.paused(job.key))
        self.assertNotIn("wrapping up",self.send.call_args.args[2])
        self.assertEqual(self.model.call_count,1)


if __name__ == "__main__":
    unittest.main()
