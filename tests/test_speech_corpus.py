"""Voice collection (/voice, /control/speech/*): switch, link code, strict WAV checks, prompt pick, speaker folders.

Run: python -m pytest tests/test_speech_corpus.py -q
The corpus goes to a temp folder; app.json is never written (update_app_config is faked).
"""
import io
import json
import shutil
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import limits
from core.auth import Principal
from core.config import BASE_DIR
from core.speech_corpus import prompts as corpus_prompts
from core.speech_corpus import settings as corpus_settings
from core.speech_corpus import store as corpus_store
import routes.speech_corpus as speech_routes

RATE = 16000


def wav(seconds=2.0, rate=RATE, channels=1, amp=0.2, bits=16) -> bytes:
    n = int(seconds * rate)
    t = np.arange(n) / rate
    x = (amp * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
    if channels == 2:
        x = np.repeat(x, 2)
    data = x.tobytes()
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * channels * bits // 8, channels * bits // 8, bits)
    return hdr + b"data" + struct.pack("<I", len(data)) + data


def admin() -> Principal:
    return Principal(id=1, username="admin", display_name="Admin", is_super_admin=True, must_change_password=False,
                     role_names=["admin"], permission_keys={"speech.manage"}, mfa_verified=True)


LINES = "\n".join(
    [json.dumps({"text": f"প্রমিত বাক্য নম্বর {i}", "category": "standard"}, ensure_ascii=False) for i in range(20)]
    + [json.dumps({"text": f"ডোমেইন বাক্য {i}", "category": "domain"}, ensure_ascii=False) for i in range(12)]
    + [json.dumps({"text": f"আমার payment {i} fail হয়েছে", "category": "banglish"}, ensure_ascii=False) for i in range(16)]
    + [json.dumps({"text": f"মোট {i} হাজার টাকা", "category": "numbers"}, ensure_ascii=False) for i in range(12)])


class SpeechCorpusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="voice-corpus-test-"))
        self.cfg = {"speech_corpus": {"enabled": False, "dir": str(self.tmp / "corpus"), "access_code_sha256": "",
                                      "lines_per_session": 15, "max_clip_seconds": 20, "sessions_per_day": 5}}
        self.file_cfg = json.loads(json.dumps(self.cfg))

        def fake_update(mutator, path=None):
            mutator(self.file_cfg)
            return self.file_cfg

        self.keychain = {}          # stands in for the OS keychain: the real one is never touched
        self.patches = [
            mock.patch.dict("core.small_model.APP_CONFIG", self.cfg),
            mock.patch("core.credentials.set_token", side_effect=lambda k, v: self.keychain.__setitem__(k, v)),
            mock.patch("core.credentials.get_token", side_effect=lambda k: self.keychain.get(k)),
            mock.patch("core.config.update_app_config", side_effect=fake_update),
            mock.patch.object(speech_routes, "audit_log", lambda *a, **k: None),
        ]
        for p in self.patches:
            p.start()
        limits.buckets.reset()
        corpus_store.reset_stores()
        app = FastAPI()
        app.include_router(speech_routes.router)
        app.dependency_overrides[speech_routes._manage] = admin
        app.dependency_overrides[speech_routes._manage_verified] = admin
        self.app = app
        self.c = TestClient(app)

    def tearDown(self):
        corpus_store.reset_stores()
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def set_cfg(self, key, value):
        """Change a setting in both the live config and the faked app.json (a save copies the file over the live one)."""
        from core.small_model import APP_CONFIG
        APP_CONFIG["speech_corpus"][key] = value
        self.file_cfg["speech_corpus"][key] = value

    def turn_on(self) -> str:
        code = self.c.post("/control/speech/code").json()["code"]
        r = self.c.post("/control/speech/enabled", json={"enabled": True})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.c.post("/control/speech/prompts", json={"jsonl": LINES})
        self.assertEqual(r.json()["added"], 60, r.text)
        return code

    def start(self, code, emp="E1001", name="Sabbir Hosain", region="standard", **kw):
        body = {"code": code, "name": name, "employee_id": emp, "region": region, "consent": True, **kw}
        return self.c.post("/media/voice-collect/start", json=body)

    def upload(self, token, prompt_id, data):
        return self.c.post("/media/voice-collect/recording", data={"token": token, "prompt_id": str(prompt_id)},
                           files={"file": ("line.wav", io.BytesIO(data), "audio/wav")})

    # ---- switch and code ----
    def test_disabled_is_404_everywhere(self):
        for path in ("/voice", "/media/voice-collect/options"):
            self.assertEqual(self.c.get(path).status_code, 404)
        self.assertEqual(self.c.post("/media/voice-collect/start", json={}).status_code, 404)
        self.assertEqual(self.c.post("/media/voice-collect/finish", json={}).status_code, 404)

    def test_turning_on_needs_a_code(self):
        r = self.c.post("/control/speech/enabled", json={"enabled": True})
        self.assertEqual(r.status_code, 409)
        self.assertIn("access code", r.json()["error"])

    def test_folder_inside_the_app_is_refused(self):
        self.set_cfg("dir", str(BASE_DIR / "voice-corpus-test-should-not-exist"))
        self.c.post("/control/speech/code")
        r = self.c.post("/control/speech/enabled", json={"enabled": True})
        self.assertEqual(r.status_code, 409)
        self.assertIn("outside the application", r.json()["error"])
        self.assertFalse((BASE_DIR / "voice-corpus-test-should-not-exist").exists())

    def test_relative_or_missing_folder_is_refused(self):
        self.set_cfg("dir", "relative/path")
        with self.assertRaises(corpus_settings.CorpusConfigError):
            corpus_settings.corpus_dir()
        self.set_cfg("dir", "")
        with self.assertRaises(corpus_settings.CorpusConfigError):
            corpus_settings.corpus_dir()

    def test_enabled_is_saved_and_switch_off_is_immediate(self):
        code = self.turn_on()
        self.assertTrue(self.file_cfg["speech_corpus"]["enabled"])
        self.assertNotIn(code, json.dumps(self.file_cfg))            # only the hash is stored
        tok = self.start(code).json()["token"]
        self.assertEqual(self.c.post("/control/speech/enabled", json={"enabled": False}).status_code, 200)
        self.assertEqual(self.c.get("/voice").status_code, 404)
        self.assertEqual(self.upload(tok, 1, wav()).status_code, 404)
        self.c.post("/control/speech/enabled", json={"enabled": True})
        self.assertEqual(self.c.get("/media/voice-collect/options").status_code, 200)

    def test_current_link_is_shown_again(self):
        self.assertIsNone(self.c.get("/control/speech/status").json()["link_path"])
        first = self.c.post("/control/speech/code").json()
        self.assertTrue(first["link_saved"])
        self.assertEqual(self.c.get("/control/speech/status").json()["link_path"], f"/voice?k={first['code']}")
        second = self.c.post("/control/speech/code").json()
        self.assertEqual(self.c.get("/control/speech/status").json()["link_path"], f"/voice?k={second['code']}")
        self.assertNotIn(second["code"], json.dumps(self.file_cfg))       # app.json still holds only the hash

    def test_no_keychain_still_works_but_cannot_show_the_link(self):
        with mock.patch("core.credentials.set_token", side_effect=RuntimeError("no keyring")):
            d = self.c.post("/control/speech/code").json()
        self.assertFalse(d["link_saved"])
        self.assertIsNone(self.c.get("/control/speech/status").json()["link_path"])
        self.assertTrue(corpus_settings.code_ok(d["code"]))

    def test_stale_keychain_code_is_not_shown(self):
        self.c.post("/control/speech/code")
        self.keychain["speech-corpus-link-code"] = "some-other-code"
        self.assertIsNone(self.c.get("/control/speech/status").json()["link_path"])

    # ---- open window ----
    def test_window_not_yet_and_expired_block_new_sessions(self):
        import time
        code = self.turn_on()
        now = time.time()
        self.set_cfg("valid_from", now + 3600)
        o = self.c.get("/media/voice-collect/options").json()
        self.assertEqual(o["state"], "not_yet")
        r = self.start(code)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["state"], "not_yet")
        self.set_cfg("valid_from", 0)
        self.set_cfg("valid_until", now - 60)
        self.assertEqual(self.c.get("/media/voice-collect/options").json()["state"], "expired")
        self.assertEqual(self.start(code).json()["state"], "expired")

    def test_extend_reopens_with_the_same_link(self):
        import time
        code = self.turn_on()
        self.set_cfg("valid_until", time.time() - 60)
        self.assertEqual(self.start(code).status_code, 403)
        r = self.c.post("/control/speech/window", json={"valid_from": None, "valid_until": time.time() + 7 * 86400})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["state"], "open")
        self.assertEqual(self.start(code).status_code, 200)           # same code still works
        self.assertGreater(self.file_cfg["speech_corpus"]["valid_until"], time.time())

    def test_window_validation(self):
        import time
        now = time.time()
        bad = [
            {"valid_from": now + 7200, "valid_until": now + 3600},    # end before start
            {"valid_from": None, "valid_until": now - 10},            # already over
            {"valid_from": None, "valid_until": now + 400 * 86400},   # longer than a year
        ]
        for body in bad:
            self.assertEqual(self.c.post("/control/speech/window", json=body).status_code, 400, body)
        ok = self.c.post("/control/speech/window", json={"valid_from": None, "valid_until": None}).json()
        self.assertEqual((ok["valid_from"], ok["valid_until"], ok["state"]), (None, None, "open"))

    def test_session_started_in_window_can_finish_after_it_closes(self):
        import time
        code = self.turn_on()
        d = self.start(code).json()
        self.set_cfg("valid_until", time.time() - 1)
        self.assertEqual(self.upload(d["token"], d["lines"][0]["id"], wav()).status_code, 200)
        self.assertEqual(self.start(code).status_code, 403)

    def test_wrong_and_rotated_code(self):
        code = self.turn_on()
        self.assertEqual(self.start("nope").status_code, 403)
        self.c.post("/control/speech/code")
        self.assertEqual(self.start(code).status_code, 403)          # the old link stopped working

    def test_consent_and_required_fields(self):
        code = self.turn_on()
        self.assertEqual(self.c.post("/media/voice-collect/start", json={"code": code, "name": "A B", "employee_id": "E1",
                                                                "region": "standard"}).status_code, 400)
        self.assertEqual(self.start(code, emp="").status_code, 400)
        self.assertEqual(self.start(code, emp="../x").status_code, 400)
        self.assertEqual(self.start(code, region="mars").status_code, 400)

    def test_admin_routes_need_the_permission(self):
        app = FastAPI()
        app.include_router(speech_routes.router)
        c = TestClient(app)
        self.assertIn(c.get("/control/speech/status").status_code, (401, 403))
        self.assertIn(c.post("/control/speech/enabled", json={"enabled": True}).status_code, (401, 403))

    # ---- sessions, prompts, folders ----
    def test_start_gives_a_mixed_session_without_repeats(self):
        code = self.turn_on()
        r = self.start(code)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(len(d["lines"]), 15)
        self.assertEqual(d["instruction"], "read")
        st = corpus_store.get_store(corpus_settings.corpus_dir())
        cats = [st.db.execute("SELECT category FROM prompts WHERE id=?", (l["id"],)).fetchone()[0] for l in d["lines"]]
        self.assertEqual({c: cats.count(c) for c in set(cats)}, {"standard": 5, "domain": 3, "banglish": 4, "numbers": 3})
        second = self.start(code, name="sabbir  hosain").json()
        self.assertFalse({l["id"] for l in d["lines"]} & {l["id"] for l in second["lines"]})

    def test_least_recorded_lines_come_first(self):
        code = self.turn_on()
        st = corpus_store.get_store(corpus_settings.corpus_dir())
        a = self.start(code, emp="E1").json()
        for line in a["lines"]:
            self.assertEqual(self.upload(a["token"], line["id"], wav()).status_code, 200)
        recorded = {l["id"] for l in a["lines"]}
        b = self.start(code, emp="E2", name="Other Person").json()
        self.assertFalse(recorded & {l["id"] for l in b["lines"]})
        self.assertEqual(st.stats()["clips"], 15)

    def test_dialect_speaker_gets_the_dialect_instruction(self):
        code = self.turn_on()
        d = self.start(code, region="sylhet").json()
        self.assertEqual(d["instruction"], "dialect")
        self.assertEqual(self.upload(d["token"], d["lines"][0]["id"], wav()).status_code, 200)
        st = corpus_store.get_store(corpus_settings.corpus_dir())
        self.assertEqual(st.db.execute("SELECT instruction FROM recordings").fetchone()[0], "dialect")

    def test_speaker_folder_name(self):
        f = corpus_store.speaker_folder
        self.assertEqual(f("Sabbir Hosain", "E1234"), "Sabbir-Hosain_E1234")
        self.assertEqual(f("সাব্বির হোসেন", "E1"), "সাব্বির-হোসেন_E1")
        self.assertEqual(f("../../etc/passwd", "E1"), "etc-passwd_E1")
        self.assertNotIn("/", f("a/b\\c", "X_1"))
        self.assertLessEqual(len(f("x" * 300, "E" * 32)), 60)
        self.assertEqual(f("!!!", "E9"), "speaker_E9")

    def test_same_employee_keeps_the_first_folder(self):
        code = self.turn_on()
        a = self.start(code, emp="e77", name="Rahim Uddin").json()
        self.upload(a["token"], a["lines"][0]["id"], wav())
        b = self.start(code, emp="E77", name="Md. Rahim").json()
        self.upload(b["token"], b["lines"][0]["id"], wav())
        audio = corpus_settings.corpus_dir() / "audio"
        self.assertEqual([p.name for p in audio.iterdir()], ["Rahim-Uddin_E77"])
        self.assertEqual(len(list((audio / "Rahim-Uddin_E77").glob("*.wav"))), 2)

    def test_sessions_per_day_limit(self):
        code = self.turn_on()
        self.set_cfg("sessions_per_day", 2)
        self.assertEqual(self.start(code).status_code, 200)
        self.assertEqual(self.start(code).status_code, 200)
        self.assertEqual(self.start(code).status_code, 429)

    # ---- uploads ----
    def test_good_clip_is_stored_with_its_row(self):
        code = self.turn_on()
        d = self.start(code).json()
        line = d["lines"][0]
        r = self.upload(d["token"], line["id"], wav(2.5))
        self.assertEqual(r.status_code, 200, r.text)
        root = corpus_settings.corpus_dir()
        con = sqlite3.connect(root / "corpus.db")
        row = con.execute("SELECT path, prompt_text, seconds, status FROM recordings").fetchone()
        con.close()
        self.assertEqual(row[1], line["text"])
        self.assertAlmostEqual(row[2], 2.5, places=2)
        self.assertEqual(row[3], "ok")
        self.assertTrue((root / row[0]).is_file())
        self.assertTrue(row[0].startswith("audio/Sabbir-Hosain_E1001/"))

    def test_bad_clips_are_rejected(self):
        code = self.turn_on()
        d = self.start(code).json()
        pid = d["lines"][0]["id"]
        cases = {
            "44.1 kHz": wav(rate=44100),
            "stereo": wav(channels=2),
            "silent": wav(amp=0.0),
            "too short": wav(0.4),
            "too long": wav(21),
            "clipped": wav(amp=1.5),
            "not wav": b"hello" * 100,
        }
        for name, data in cases.items():
            r = self.upload(d["token"], pid, data)
            self.assertEqual(r.status_code, 422, f"{name}: {r.text}")
        self.assertEqual(self.upload(d["token"], pid, b"\0" * (speech_routes.MAX_UPLOAD_BYTES + 10)).status_code, 413)
        self.assertEqual(corpus_store.get_store(corpus_settings.corpus_dir()).stats()["clips"], 0)

    def test_line_outside_the_session_and_dead_token(self):
        code = self.turn_on()
        d = self.start(code).json()
        other = max(l["id"] for l in d["lines"]) + 1000
        self.assertEqual(self.upload(d["token"], other, wav()).status_code, 400)
        self.assertEqual(self.upload("not-a-token", d["lines"][0]["id"], wav()).status_code, 401)
        self.c.post("/media/voice-collect/finish", json={"token": d["token"]})
        self.assertEqual(self.upload(d["token"], d["lines"][0]["id"], wav()).status_code, 401)

    def test_rerecord_replaces_and_attempts_are_capped(self):
        code = self.turn_on()
        d = self.start(code).json()
        pid = d["lines"][0]["id"]
        for _ in range(3):
            self.assertEqual(self.upload(d["token"], pid, wav()).status_code, 200)
        st = corpus_store.get_store(corpus_settings.corpus_dir())
        self.assertEqual(st.stats()["clips"], 1)
        st.db.execute("UPDATE sessions SET uploads = ?", (15 * corpus_store.UPLOADS_PER_LINE,))
        st.db.commit()
        self.assertEqual(self.upload(d["token"], pid, wav()).status_code, 429)

    # ---- admin ----
    def test_import_refuses_card_numbers_and_bad_lines(self):
        self.turn_on()
        bad = "\n".join([
            json.dumps({"text": "আমার কার্ড 4111 1111 1111 1111", "category": "numbers"}, ensure_ascii=False),
            json.dumps({"text": "x", "category": "standard"}),
            json.dumps({"text": "ঠিক আছে একটা লাইন", "category": "poetry"}, ensure_ascii=False),
            "not json",
            json.dumps({"text": "প্রমিত বাক্য নম্বর 1", "category": "standard"}, ensure_ascii=False),   # duplicate
        ])
        d = self.c.post("/control/speech/prompts", json={"jsonl": bad}).json()
        self.assertEqual((d["added"], d["duplicates"], d["rejected_count"]), (0, 1, 4))

    def test_starter_set_imports_cleanly(self):
        self.turn_on()
        text = (BASE_DIR / "scripts" / "stt_corpus_prompts" / "starter.jsonl").read_text(encoding="utf-8")
        d = self.c.post("/control/speech/prompts", json={"jsonl": text}).json()
        self.assertEqual(d["rejected_count"], 0, d["rejected"])
        self.assertGreater(d["added"], 50)

    def test_delete_contributor_removes_rows_and_files(self):
        code = self.turn_on()
        d = self.start(code, emp="E5").json()
        self.upload(d["token"], d["lines"][0]["id"], wav())
        folder = corpus_settings.corpus_dir() / "audio" / "Sabbir-Hosain_E5"
        self.assertTrue(folder.is_dir())
        r = self.c.post("/control/speech/contributor/delete", json={"employee_id": "e5"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["recordings_deleted"], 1)
        self.assertFalse(folder.exists())
        st = corpus_store.get_store(corpus_settings.corpus_dir())
        self.assertEqual(st.stats()["speakers"], 0)
        self.assertEqual(self.c.post("/control/speech/contributor/delete", json={"employee_id": "E5"}).status_code, 404)

    def test_sample_file_downloads_and_imports(self):
        r = self.c.get("/control/speech/prompts/sample")
        self.assertEqual(r.status_code, 200)
        self.assertIn("voice-lines-sample.jsonl", r.headers.get("content-disposition", ""))
        first = json.loads(r.text.splitlines()[0])
        self.assertIn(first["category"], corpus_prompts.CATEGORIES)
        self.turn_on()
        d = self.c.post("/control/speech/prompts", json={"jsonl": r.text}).json()
        self.assertEqual(d["rejected_count"], 0)

    # ---- review report ----
    def test_review_lists_plays_and_flags(self):
        code = self.turn_on()
        a = self.start(code, emp="E10", name="Rahim Uddin", region="sylhet").json()
        b = self.start(code, emp="E11", name="Karim Mia").json()
        for line in a["lines"][:3]:
            self.upload(a["token"], line["id"], wav())
        self.upload(b["token"], b["lines"][0]["id"], wav(1.5))

        sp = self.c.get("/control/speech/contributors").json()
        self.assertEqual(sp["total"], 2)
        rahim = next(x for x in sp["items"] if x["employee_id"] == "E10")
        self.assertEqual((rahim["clips"], rahim["region"]), (3, "sylhet"))
        self.assertEqual(self.c.get("/control/speech/contributors", params={"q": "karim"}).json()["total"], 1)

        recs = self.c.get("/control/speech/recordings").json()
        self.assertEqual(recs["total"], 4)
        mine = self.c.get("/control/speech/recordings", params={"contributor": rahim["id"]}).json()
        self.assertEqual(mine["total"], 3)
        self.assertEqual(self.c.get("/control/speech/recordings", params={"region": "sylhet"}).json()["total"], 3)
        self.assertEqual(self.c.get("/control/speech/recordings", params={"q": "100%_"}).json()["total"], 0)   # LIKE escaped

        rid = mine["items"][0]["id"]
        audio = self.c.get(f"/control/speech/recordings/{rid}/audio")
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.headers["content-type"], "audio/wav")
        self.assertIn("no-store", audio.headers["cache-control"])
        self.assertEqual(audio.content[:4], b"RIFF")
        self.assertEqual(self.c.get("/control/speech/recordings/999999/audio").status_code, 404)

        self.assertEqual(self.c.post(f"/control/speech/recordings/{rid}/status", json={"status": "flagged"}).status_code, 200)
        self.assertEqual(self.c.get("/control/speech/recordings", params={"status": "flagged"}).json()["total"], 1)
        st = corpus_store.get_store(corpus_settings.corpus_dir()).stats()
        self.assertEqual((st["clips"], st["flagged"]), (3, 1))
        self.assertEqual(self.c.post(f"/control/speech/recordings/{rid}/status", json={"status": "deleted"}).status_code, 400)
        self.assertEqual(self.c.post("/control/speech/recordings/999999/status", json={"status": "ok"}).status_code, 404)

    def test_review_needs_the_permission(self):
        app = FastAPI()
        app.include_router(speech_routes.router)
        c = TestClient(app)
        for path in ("/control/speech/contributors", "/control/speech/recordings", "/control/speech/recordings/1/audio"):
            self.assertIn(c.get(path).status_code, (401, 403), path)

    def test_speaker_codes_are_never_reused(self):
        code = self.turn_on()
        a = self.start(code, emp="E1").json()
        self.c.post("/control/speech/contributor/delete", json={"employee_id": "E1"})
        b = self.start(code, emp="E2").json()
        self.assertEqual(a["speaker_code"], "S0001")
        self.assertEqual(b["speaker_code"], "S0002")


class BodyCapTests(unittest.TestCase):
    def test_voice_cap_holds_even_with_an_admin_map(self):
        with mock.patch.dict("core.small_model.APP_CONFIG", {"serving": {"max_body_bytes": 99 * 1024 * 1024,
                                                                           "body_limit_paths": {}}}):
            self.assertEqual(limits.body_limit_for("/media/voice-collect/recording"), 2 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
