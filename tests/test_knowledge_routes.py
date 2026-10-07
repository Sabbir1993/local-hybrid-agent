"""tests/test_knowledge_routes.py - E2: knowledge CRUD/ACL/validation/error paths.

The routes sit at 20-26% coverage. These tests pin the validation and error
behaviour that guards the surface: permission gating, upload type/size caps,
traversal-safe filenames, 404s, and the chunked-upload aggregate cap (chunked
uploads exist to bypass proxy size limits per static/js/knowledge.js, not to
exceed the 50MB file ceiling -- an unbounded assembly is a disk-fill).

Run: python -m unittest tests.test_knowledge_routes -v
"""

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth_db, deps, knowledge_ingest
from core.auth import Principal
from routes import knowledge as knowledge_pkg
from routes.knowledge import endpoints as kb_endpoints
from routes.knowledge import chunk_upload as kb_chunks


def _principal(perms):
    return Principal(id=7, username="u7", display_name="u7", is_super_admin=False,
                     must_change_password=False, role_names=[], permission_keys=set(perms))


class _KbApp(unittest.TestCase):
    perms = {"knowledge.manage"}

    async def _fake_ingest(self, sid, text, user):
        return {"ok": True, "chunks": 3, "pans_masked": 0}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = auth_db._auth_db
        with mock.patch.object(auth_db, "AUTH_DB_FILE", Path(self.tmp.name) / "auth.db"):
            auth_db._auth_db = auth_db._init_auth_db()
        self.uid = auth_db.create_user(username="u7", password_hash=None)
        self.me = _principal(self.perms)
        self.me.id = self.uid
        app = FastAPI()
        app.include_router(knowledge_pkg.router)
        app.dependency_overrides[deps.get_current_user] = lambda: self.me
        self.patches = [
            mock.patch.object(kb_endpoints, "audit_log", lambda *a, **k: None),
            mock.patch.object(kb_endpoints, "_finish_ingest", self._fake_ingest),
            mock.patch.object(kb_chunks, "_finish_ingest", self._fake_ingest),
            mock.patch.object(knowledge_ingest, "extract_file", lambda dest: "extracted text"),
            mock.patch.object(knowledge_ingest, "extract_url", self._fake_url),
            mock.patch.object(kb_endpoints, "KNOWLEDGE_UPLOADS_DIR", Path(self.tmp.name) / "uploads"),
            mock.patch.object(kb_chunks, "CHUNKS_TEMP_DIR", Path(self.tmp.name) / "chunks"),
        ]
        # endpoints.py does `from core.config import KNOWLEDGE_UPLOADS_DIR`; chunk_upload
        # reads CHUNKS_TEMP_DIR from .models at call time, so patch models too
        from routes.knowledge import models as kb_models
        self.patches.append(mock.patch.object(kb_models, "CHUNKS_TEMP_DIR", Path(self.tmp.name) / "chunks"))
        for p in self.patches:
            p.start()
        # endpoint modules bound the real dirs at import; redirect both
        kb_endpoints.KNOWLEDGE_UPLOADS_DIR = Path(self.tmp.name) / "uploads"
        self.client = TestClient(app, raise_server_exceptions=False)

    async def _fake_url(self, url):
        if "boom" in url:
            raise RuntimeError("fetch failed")
        return "remote text"

    def tearDown(self):
        self.client.close()
        for p in self.patches:
            p.stop()
        auth_db._auth_db.close()
        auth_db._auth_db = self._saved
        self.tmp.cleanup()


class CrudTests(_KbApp):
    def test_list_starts_empty(self):
        r = self.client.get("/knowledge")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["sources"], [])

    def test_add_text_round_trip(self):
        r = self.client.post("/knowledge/text", json={"title": "t", "text": "hello"})
        self.assertEqual(r.status_code, 200, r.text[:200])
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["source"]["title"], "t")
        self.assertEqual(len(self.client.get("/knowledge").json()["sources"]), 1)

    def test_add_url_error_marks_source(self):
        r = self.client.post("/knowledge/url", json={"title": "u", "url": "http://x/boom"})
        self.assertEqual(r.status_code, 400)
        srcs = self.client.get("/knowledge").json()["sources"]
        self.assertEqual(srcs[0]["status"], "error")

    def test_add_url_ok(self):
        r = self.client.post("/knowledge/url", json={"title": "u", "url": "http://x/ok"})
        self.assertEqual(r.status_code, 200, r.text[:200])

    def test_access_404_then_ok(self):
        r = self.client.put("/knowledge/4242/access", json={"roles": ["user"]})
        self.assertEqual(r.status_code, 404)
        sid = auth_db.create_knowledge_source(title="t", kind="text", created_by=self.uid)
        r = self.client.put(f"/knowledge/{sid}/access", json={"roles": ["user"]})
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertIn("user", r.json()["source"]["roles"])

    def test_delete_404_then_ok(self):
        self.assertEqual(self.client.delete("/knowledge/4242").status_code, 404)
        sid = auth_db.create_knowledge_source(title="t", kind="text", created_by=self.uid)
        with mock.patch.object(kb_endpoints, "delete_knowledge_chunks") as dc:
            r = self.client.delete(f"/knowledge/{sid}")
        self.assertEqual(r.status_code, 200)
        dc.assert_called_once_with(sid)
        self.assertEqual(self.client.get("/knowledge").json()["sources"], [])

    def test_bulk_delete(self):
        s1 = auth_db.create_knowledge_source(title="t1", kind="text", created_by=self.uid)
        s2 = auth_db.create_knowledge_source(title="t2", kind="text", created_by=self.uid)
        s3 = auth_db.create_knowledge_source(title="t3", kind="text", created_by=self.uid)
        with mock.patch.object(kb_endpoints, "delete_knowledge_chunks"):
            r = self.client.post("/knowledge/bulk-delete", json={"ids": [s1, s2, 99999]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "deleted": 2})
        remaining = self.client.get("/knowledge").json()["sources"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], s3)

    def test_reindex_paths(self):
        self.assertEqual(self.client.post("/knowledge/4242/reindex").status_code, 404)
        sid = auth_db.create_knowledge_source(title="t", kind="text", created_by=self.uid)
        r = self.client.post(f"/knowledge/{sid}/reindex")
        self.assertEqual(r.status_code, 400)  # text sources re-add instead
        self.assertIn("cannot be reindexed", r.json()["error"])
        fid = auth_db.create_knowledge_source(title="f.txt", kind="file", origin="f.txt",
                                              stored_path=str(Path(self.tmp.name) / "uploads" / "f.txt"),
                                              created_by=self.uid)
        Path(self.tmp.name, "uploads").mkdir(parents=True, exist_ok=True)
        Path(self.tmp.name, "uploads", "f.txt").write_text("hi")
        r = self.client.post(f"/knowledge/{fid}/reindex")
        self.assertEqual(r.status_code, 200, r.text[:200])


class UploadTests(_KbApp):
    def _file(self, name, data: bytes):
        return {"file": (name, io.BytesIO(data), "application/octet-stream")}

    def test_unsupported_type_400(self):
        r = self.client.post("/knowledge/upload", files=self._file("x.exe", b"data"))
        self.assertEqual(r.status_code, 400)

    def test_oversize_413(self):
        with mock.patch.object(kb_endpoints, "MAX_KB_FILE_BYTES", 10):
            r = self.client.post("/knowledge/upload", files=self._file("a.csv", b"x" * 11))
        self.assertEqual(r.status_code, 413)

    def test_ok_and_traversal_name_sandboxed(self):
        r = self.client.post("/knowledge/upload", files=self._file("../../evil.csv", b"hi"),
                             data={"title": "t"})
        self.assertEqual(r.status_code, 200, r.text[:200])
        ups = list((Path(self.tmp.name) / "uploads").glob("*"))
        self.assertEqual(len(ups), 1)
        self.assertTrue(ups[0].is_file())  # basename'd + uuid name inside uploads dir

    def test_get_file_404s(self):
        sid = auth_db.create_knowledge_source(title="t", kind="text", created_by=self.uid)
        self.assertEqual(self.client.get(f"/knowledge/{sid}/file").status_code, 404)
        fid = auth_db.create_knowledge_source(title="f", kind="file", origin="f.txt",
                                              stored_path=str(Path(self.tmp.name) / "nope.txt"),
                                              created_by=self.uid)
        self.assertEqual(self.client.get(f"/knowledge/{fid}/file").status_code, 404)

    def test_get_file_ok(self):
        d = Path(self.tmp.name) / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        (d / "real.txt").write_text("content")
        fid = auth_db.create_knowledge_source(title="f", kind="file", origin="f.txt",
                                              stored_path=str(d / "real.txt"), created_by=self.uid)
        r = self.client.get(f"/knowledge/{fid}/file")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"content")

    def test_chunk_validation(self):
        bad = {"upload_id": "nope!!", "chunk_index": 0, "total_chunks": 1}
        r = self.client.post("/knowledge/upload/chunk", data=bad, files=self._file("a.csv", b"x"))
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/knowledge/upload/chunk",
                             data={"upload_id": "ok1", "chunk_index": 5, "total_chunks": 2},
                             files=self._file("a.csv", b"x"))
        self.assertEqual(r.status_code, 400)
        with mock.patch.object(kb_chunks, "MAX_KB_CHUNK_BYTES", 4):
            r = self.client.post("/knowledge/upload/chunk",
                                 data={"upload_id": "ok1", "chunk_index": 0, "total_chunks": 1},
                                 files=self._file("a.csv", b"12345"))
        self.assertEqual(r.status_code, 413)

    def test_chunk_round_trip(self):
        for i in range(3):
            r = self.client.post("/knowledge/upload/chunk",
                                 data={"upload_id": "abc", "chunk_index": i, "total_chunks": 3},
                                 files=self._file("a.csv", f"part{i}-".encode()))
            self.assertEqual(r.status_code, 200, r.text[:200])
        r = self.client.post("/knowledge/upload/complete",
                             json={"upload_id": "abc", "filename": "a.csv", "total_chunks": 3,
                                   "title": "t"})
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertTrue(r.json()["ok"])

    def test_complete_missing_chunk_400(self):
        r = self.client.post("/knowledge/upload/chunk",
                             data={"upload_id": "m1", "chunk_index": 0, "total_chunks": 2},
                             files=self._file("a.csv", b"x"))
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/knowledge/upload/complete",
                             json={"upload_id": "m1", "filename": "a.csv", "total_chunks": 2})
        self.assertEqual(r.status_code, 400)
        self.assertIn("missing chunk 1", r.json()["error"])

    def test_complete_unsupported_type_cleans_up(self):
        self.client.post("/knowledge/upload/chunk",
                         data={"upload_id": "e1", "chunk_index": 0, "total_chunks": 1},
                         files=self._file("a.csv", b"x"))
        r = self.client.post("/knowledge/upload/complete",
                             json={"upload_id": "e1", "filename": "a.exe", "total_chunks": 1})
        self.assertEqual(r.status_code, 400)
        self.assertFalse((Path(self.tmp.name) / "chunks" / "e1").exists())

    def test_complete_unknown_session_404(self):
        r = self.client.post("/knowledge/upload/complete",
                             json={"upload_id": "ghost", "filename": "a.csv", "total_chunks": 1})
        self.assertEqual(r.status_code, 404)

    def test_assembled_total_capped(self):
        """Chunked upload bypasses proxy size limits, not the file ceiling:
        parts summing past MAX_KB_FILE_BYTES are refused with 413."""
        with mock.patch.object(kb_chunks, "MAX_KB_FILE_BYTES", 10):
            for i in range(2):
                r = self.client.post("/knowledge/upload/chunk",
                                     data={"upload_id": "big", "chunk_index": i, "total_chunks": 2},
                                     files=self._file("a.csv", b"x" * 6))
                self.assertEqual(r.status_code, 200)
            r = self.client.post("/knowledge/upload/complete",
                                 json={"upload_id": "big", "filename": "a.csv", "total_chunks": 2})
        self.assertEqual(r.status_code, 413, r.text[:200])
        self.assertFalse((Path(self.tmp.name) / "chunks" / "big").exists())


class PermissionTests(_KbApp):
    perms = {"chat.use"}

    def test_every_route_403s_without_knowledge_manage(self):
        self.assertEqual(self.client.get("/knowledge").status_code, 403)
        self.assertEqual(self.client.post("/knowledge/text",
                                          json={"title": "t", "text": "x"}).status_code, 403)
        self.assertEqual(self.client.post("/knowledge/upload/chunk",
                                          data={"upload_id": "a", "chunk_index": 0,
                                                "total_chunks": 1},
                                          files={"file": ("a.csv", io.BytesIO(b"x"),
                                                          "application/octet-stream")}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
