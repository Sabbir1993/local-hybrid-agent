"""Project builder hardening: every item of the security / robustness audit of core/agent_tools/project_files.py, as a regression test.

Covers: entry path traversal, ReDoS, valid pages left byte-identical, deferred scripts in the preview, concurrency, size limit on
appends, nested paths, finish record, per-request scope, wrapper tags, strict download lookup, card numbers in project writes,
generic tool errors, Windows names and deliverable cleanup.

Run: python -X utf8 -m unittest tests.test_project_files_hardening -v
"""
import re
import sys
import tempfile
import threading
import contextvars
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.agent_tools  # noqa: F401
from core.agent_tools import project_files as pf

HTML = "<!doctype html><html><head><title>t</title></head><body><p>hi</p></body></html>"
VISA = "4111 1111 1111 1111"               # the public test number, never a real one


class Base(unittest.TestCase):
    uid = 1

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.common = Path(self.td.name)
        for patcher in (mock.patch.object(core.agent_tools, "COMMON_ROOT", self.common),
                        mock.patch.object(sys.modules["core.agent_tools.workspace"], "get_current_user_id", lambda: self.uid)):
            patcher.start()
            self.addCleanup(patcher.stop)
        pf.set_scope(5)
        self.addCleanup(pf.reset_scope)

    def user_dir(self, uid=None):
        return self.common / f"user_{uid or self.uid}"

    def plan(self, project, *paths, min_lines=1):
        return pf.tool_project_plan({"project": project, "files": [{"path": p, "purpose": "x", "min_lines": min_lines} for p in paths]})

    def write(self, project, path, content, **kw):
        return pf.tool_project_write({"project": project, "path": path, "content": content, **kw})

    def append(self, project, path, content):
        return pf.tool_project_append({"project": project, "path": path, "content": content})

    def page(self, project="p", html=HTML, plan_extra=()):
        self.plan(project, "index.html", *plan_extra)
        return self.write(project, "index.html", html)

    def root(self, project="p"):
        return pf._root(project)


# 1 ----------------------------------------------------------------------------------------------------------------------------------
class EntryCannotEscape(Base):
    def test_entry_of_another_users_file_or_a_server_file_is_refused(self):
        victim = self.user_dir(2)
        victim.mkdir(parents=True)
        (victim / "payroll.csv").write_text("alice,100000\n")
        (victim / "notes.html").write_text("<html><body>USER2 PRIVATE</body></html>")
        self.page()
        for entry in ("../../../../user_2/payroll.csv", "../../../../user_2/notes.html", "/etc/passwd",
                      "../../../../../../../../etc/hostname", "..\\..\\x.html"):
            r = pf.tool_project_finish({"project": "p", "entry": entry})
            self.assertTrue(r.startswith("error"), (entry, r))
            self.assertNotIn("[DOWNLOAD", r)
        self.assertFalse(any("PRIVATE" in f.read_text(errors="ignore") for f in self.user_dir().glob("*.html")))

    def test_entry_must_be_an_html_page_of_the_project(self):
        self.page(plan_extra=("data.json",))
        self.write("p", "data.json", '{"a": 1}')
        self.assertTrue(pf.tool_project_finish({"project": "p", "entry": "data.json"}).startswith("error"))

    def test_asset_references_that_escape_the_folder_are_not_read(self):
        self.page(html='<!doctype html><html><head><link rel="stylesheet" href="../../../../secret.css">'
                       '<link rel="stylesheet" href="/abs.css"><script src="../../../secret.js"></script></head>'
                       '<body><p>x</p></body></html>')
        for name, body in (("secret.css", "body{content:'SECRETCSS'}"), ("secret.js", "var SECRETJS=1")):
            (self.root().parents[2] / name).write_text(body)        # user_1/secret.css: what ../../.. reaches
            (self.root().parents[1] / name).write_text(body)
            (self.root().parent / name).write_text(body)
        out = pf.inline_entry(self.root(), "index.html")
        self.assertNotIn("SECRET", out)
        self.assertIn('href="../../../../secret.css"', out)                    # left as it was, not inlined
        self.assertEqual(pf._missing_links(self.root(), "index.html"), [])     # not a project file: not reported either

    def test_a_link_inside_the_project_is_not_followed(self):
        self.page()
        outside = self.user_dir() / "outside.txt"
        outside.write_text("OUT")
        import os
        os.symlink(outside, self.root() / "l.txt")
        self.assertIsNone(pf._local(self.root(), "l.txt"))
        self.assertTrue(self.write("p", "l.txt", "new text here").startswith("error"))
        self.assertEqual(outside.read_text(), "OUT")


# 2 ----------------------------------------------------------------------------------------------------------------------------------
class NoReDoS(Base):
    def test_cut_off_tag_and_script_inputs_are_fast(self):
        old = "<!doctype html><html><body><main>\n<p>hi</p>\n"
        t = time.time()
        for n in (18, 24, 40):
            pf._smart_html_append(old + "<div " + "a " * n + ">x</div>\n", "<section>next</section>")
            pf._smart_html_append(old + "<p>if x <y then " + "word " * n + "> done</p>\n", "<section>next</section>")
        js = ("<script>\nfunction step(n, arr){ let total = 0;\n for (let i = 0; i<n && total < 1e9; i++) {\n total += arr[i] * 2 + "
              + " + ".join(str(i) for i in range(60)) + ";\n }\n return total; }\n</script>")
        pf._smart_html_append(old + js + "\n<div>more</div>\n", "<section>next</section>")
        pf._smart_html_append(old + "<script>" + "var a=1;if(i<n && x>1){}\n" * 4000, "more();")
        self.assertLess(time.time() - t, 1.0)

    def test_many_scripts_stay_linear(self):
        body = "".join(f"<div id=c{i}></div><script>var q{i}={i};" + "x" * 300 + "</script>\n" for i in range(1500))
        html = "<!doctype html><html><body>" + body + "</body></html>"
        t = time.time()
        pf._close_document(html)
        pf._smart_html_append(html, "<section>z</section>")
        self.assertLess(time.time() - t, 1.0)


# 3 ----------------------------------------------------------------------------------------------------------------------------------
CORPUS = {}
def _page(name, body, head="", extra_end=""):
    CORPUS[name] = f"<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\"><title>{name}</title>\n{head}\n</head>\n<body>\n{body}\n</body>\n</html>\n"

_page("three_module", '<div id="c"></div><script type="importmap">{"imports":{"three":"https://unpkg.com/three@0.160.0/build/three.module.js"}}</script>\n<script type="module">\nimport * as THREE from "three";\nconst s=new THREE.Scene();\nfor(let i=0;i<10 && s;i++){ s.add(new THREE.Object3D()); }\ndocument.getElementById("c").textContent="n="+s.children.length;\n</script>')
_page("canvas_game", '<canvas id="g" width="200" height="100"></canvas>\n<script>\nconst c=document.getElementById("g"),x=c.getContext("2d");let a=1,b=2;\nif(a<b && b>a){ document.title="ok "+(a<b); }\nx.fillStyle="#fa0";x.fillRect(0,0,10,10);\n</script>')
_page("jsonld", '<h1>Shop</h1><p>Hello</p>', head='<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"A<B","url":"https://e.com"}</script>')
_page("escaped_script", '<div id="o"></div><script>\ndocument.getElementById("o").innerHTML = \'<script>1<\\/script>\' + "<\\/body>";\n</script>')
_page("export_html_string", '<button id="b">Export</button><div id="o"></div>\n<script>\nfunction exportHTML(){ return "<html><body><h1>x</h1></body></html>"; }\ndocument.getElementById("o").textContent = exportHTML().length;\n</script>')
_page("svg_inline", '<svg width="50" height="50" viewBox="0 0 50 50"><style>.a{fill:red}</style><circle class="a" cx="25" cy="25" r="20"/><path d="M0 0L10 10"></path></svg><p>after</p>')
_page("style_in_js", '<div id="o"></div><script>\ndocument.getElementById("o").innerHTML="<style>.z{color:red}</style><span class=z>hi</span>";\n</script>')
_page("comments", '<!-- <script> unclosed in comment -->\n<p>a</p>\n<!-- </body> also --><p>b</p>')
_page("textarea", '<textarea id="t">&lt;/body&gt; text <b>x</b></textarea><p>after</p>')
_page("omitted_tags", '<ul><li>one<li>two<li>three</ul><p>para one<p>para two<table><tr><td>a<td>b</table>')
_page("template", '<template id="t"><div class="x"><span>hi</span></div></template><div id="o"></div><script>document.getElementById("o").appendChild(document.getElementById("t").content.cloneNode(true));</script>')
_page("srcdoc", '<iframe id="f" srcdoc="<html><body><p>inner</p></body></html>"></iframe>')
_page("attr_gt", '<div data-x="a>b" title=\'1 < 2\'>x</div><p>y</p>')
_page("noscript", '<noscript><style>.n{display:none}</style><p>no js</p></noscript><p>yes</p>')
_page("custom_elem", '<style-guide><my-card>hi</my-card></style-guide><script-loader></script-loader><p>z</p>')
_page("regex_script", '<script>\nconst r=/<\\/?script[^>]*>/gi; const s="a<b"; document.title=String(r.test("<script>"));\n</script><p>t</p>')
_page("long_script_over3000", '<div id="o"></div>\n<script>\n' + "\n".join(f"var v{i} = {i}*2; if(v{i}<{i+5}) {{ window.q{i}=v{i}; }}" for i in range(120)) + "\ndocument.getElementById('o').textContent='done';\n</script>")
_page("template_literal", '<div id="o"></div><script>\nconst name="x"; const html = `<div class="card"><h2>${name}</h2></div>`;\ndocument.getElementById("o").innerHTML = html;\n</script>')
_page("pre_code", '<pre><code>&lt;script&gt;alert(1)&lt;/script&gt;\n&lt;/body&gt;</code></pre><p>x</p>')
_page("css_in_style", '<style>a>b{color:red} .x::after{content:"</body>"} /* <script> */ </style><p>x</p>')
_page("title_textarea_script_text", '<p>use the &lt;script&gt; tag</p><script>document.title="t"</script>')
_page("body_on_events", '<div onclick="if(1<2){this.textContent=\'</body>\'}">click</div>')


class CompletePagesAreNeverRewritten(Base):
    def test_corpus_of_valid_pages_is_byte_identical_after_finish(self):
        self.assertEqual(len(CORPUS), 22)
        for name, html in CORPUS.items():
            proj = "c-" + name.replace("_", "-")
            self.plan(proj, "index.html")
            self.write(proj, "index.html", html)
            stored = (self.root(proj) / "index.html").read_text()
            self.assertEqual(stored.strip(), html.strip(), name)
            out = pf.tool_project_finish({"project": proj})
            self.assertTrue(pf.finish_result_ok(out), (name, out))
            self.assertEqual((self.root(proj) / "index.html").read_text(), stored, name)
            self.assertEqual(pf._close_document(html), html, name)
            self.assertEqual(pf._balance_html_document(html), html, name)

    def test_cut_off_page_is_closed_outside_strings_and_comments(self):
        cut = '<!doctype html><html><body><div><script>var s = "</body></html>";\nvar q = 1;'
        out = pf._close_document(cut)
        self.assertTrue(out.rstrip().endswith("</html>"))
        self.assertEqual(out.count("</script>"), 1)
        self.assertLess(out.index("</script>"), out.index("</div>"))           # script closed first, then the open div, then body
        self.assertEqual(out.count("</body>"), 2)                             # the one in the string and the real one
        self.assertTrue(out.rstrip().endswith("</body>\n</html>"))
        css = pf._close_document('<html><head><style>.a::after{content:"</body>"}</style></head><body><p>x')
        self.assertEqual(css.count("</style>"), 1)
        self.assertIn("</p>\n</body>\n</html>", css)

    def test_style_guide_is_not_a_style_tag_and_omitted_end_tags_get_no_stray_closers(self):
        out = pf._close_document("<html><body><style-guide><p>a<p>b<ul><li>x<li>y")
        self.assertNotIn("</style>", out)
        self.assertEqual(out.count("</p>"), 0)                 # <p> is ended by the next <p> / <ul> by itself: no stray </p>
        self.assertEqual(out.count("</li>"), 1)
        self.assertEqual(out.count("</ul>"), 1)
        self.assertEqual(out.count("</style-guide>"), 1)
        self.assertEqual(pf._get_open_tags("<ul><li>one<li>two</ul><p>a<p>b<table><tr><td>a<td>b</table>"), [])
        self.assertEqual(pf._get_open_tags("<ul><li>one<li>two</ul><p>a<p>b"), ["p"])


# 4 ----------------------------------------------------------------------------------------------------------------------------------
class PreviewScripts(Base):
    def make(self, head):
        self.plan("d", "index.html", "js/main.js", "css/s.css")
        self.write("d", "index.html", f'<!doctype html><html><head><meta charset="utf-8">{head}</head><body><h1 id="t">x</h1>'
                                      '<script>var inl=1;</script></body></html>')
        self.write("d", "js/main.js", 'document.getElementById("t").textContent="ran"; // </script> and </SCRIPT>')
        self.write("d", "css/s.css", 'a::after{content:"</style>"} h1{color:red}')

    def test_defer_and_async_scripts_move_to_the_end_of_the_body_in_order(self):
        self.make('<link rel="stylesheet" href="css/s.css"><script src="js/main.js" defer></script>')
        out = pf.inline_entry(self.root("d"), "index.html")
        self.assertLess(out.index('id="t"'), out.index('textContent="ran"'))
        self.assertGreater(out.index('textContent="ran"'), out.index("var inl=1"))
        self.assertLess(out.index('textContent="ran"'), out.index("</body>"))
        self.assertNotIn("defer", out)
        self.assertNotIn("</style>\"", out)                                    # escaped inside the inlined css
        self.assertIn('<\\/style>', out)
        self.assertIn("<\\/script> and <\\/SCRIPT>", out)
        self.assertEqual(out.lower().count("</script>"), 2)

    def test_plain_and_module_scripts_stay_in_place(self):
        self.make('<script src="js/main.js"></script>')
        out = pf.inline_entry(self.root("d"), "index.html")
        self.assertLess(out.index('textContent="ran"'), out.index('id="t"'))
        self.make('<script type="module" src="js/main.js" defer></script>')
        out = pf.inline_entry(self.root("d"), "index.html")
        self.assertLess(out.index('textContent="ran"'), out.index('id="t"'))
        self.assertIn('type="module"', out)

    def test_script_tag_text_inside_a_script_string_is_not_replaced(self):
        self.plan("d", "index.html", "js/main.js")
        self.write("d", "index.html", '<!doctype html><html><body><script>var s=\'<script src="js/main.js"></script>\';</script></body></html>')
        self.write("d", "js/main.js", "var REAL=1;")
        self.assertNotIn("REAL", pf.inline_entry(self.root("d"), "index.html"))


# 5 / 6 ------------------------------------------------------------------------------------------------------------------------------
class Concurrency(Base):
    def test_30_parallel_appends_keep_every_piece_and_leave_no_temp_files(self):
        self.plan("r", "index.html", "main.js")
        self.write("r", "main.js", "// start\n" + "var x=0;\n" * 12)
        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, old)
        ths = [threading.Thread(target=contextvars.copy_context().run, args=(self.append, "r", "main.js", f"var part{i}={i};"))
               for i in range(30)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        txt = (self.root("r") / "main.js").read_text()
        self.assertEqual(sum(f"var part{i}=" in txt for i in range(30)), 30)
        self.assertEqual([p.name for p in self.root("r").rglob("*.tmp")], [])


class AppendSizeLimit(Base):
    def test_appends_count_and_stop_at_the_limit(self):
        self.plan("big", "index.html")
        self.write("big", "data.js", "var a=1;\nvar b=2;")
        chunk = ("var x = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';\n" * 13000)[:500000]
        msg = None
        for _ in range(40):
            r = self.append("big", "data.js", chunk)
            if r.startswith("error"):
                msg = r
                break
        self.assertIsNotNone(msg)
        self.assertIn("limit", msg)
        size = (self.root("big") / "data.js").stat().st_size
        self.assertLessEqual(size, pf.MAX_PROJECT_BYTES)
        self.assertGreater(size, pf.MAX_PROJECT_BYTES - 2 * 500000)

    def test_backups_count_against_the_limit(self):
        self.plan("big", "index.html")
        self.write("big", "a.js", "var a=1;\n" * 10)
        self.write("big", "a.js", "var b=1;\n" * 11, replace=True)               # leaves a.js.prev
        prev = self.root("big") / "a.js.prev"
        self.assertTrue(prev.is_file())
        self.assertEqual(pf._total_bytes(self.root("big")), (self.root("big") / "a.js").stat().st_size + prev.stat().st_size)
        with mock.patch.object(pf, "MAX_PROJECT_BYTES", prev.stat().st_size + (self.root("big") / "a.js").stat().st_size + 10):
            self.assertTrue(self.write("big", "a.js", "var c=1;\n" * 12, replace=True).startswith("error"))


# 7 ----------------------------------------------------------------------------------------------------------------------------------
class RealFoldersAreKept(Base):
    def test_blog_index_does_not_collapse_into_the_root_index(self):
        r = self.plan("blog", "index.html", "blog/index.html")
        self.assertFalse(r.startswith("error"), r)
        self.write("blog", "index.html", HTML.replace("hi", "HOME"))
        self.write("blog", "blog/index.html", HTML.replace("hi", "LIST"))
        self.assertIn("HOME", (self.root("blog") / "index.html").read_text())
        self.assertIn("LIST", (self.root("blog") / "blog" / "index.html").read_text())

    def test_a_wrapper_folder_is_still_dropped_when_it_is_clearly_a_wrapper(self):
        self.plan("shop", "index.html", "css/s.css")
        self.assertIn("Created css/s.css", self.write("shop", "shop/css/s.css", "a{}"))
        self.assertIn("Created index.html", self.write("shop", "projects/shop/index.html", HTML))
        self.assertFalse((self.root("shop") / "shop").exists())

    def test_ambiguous_wrapper_is_an_error_not_a_silent_overwrite(self):
        self.plan("blog", "index.html")
        self.write("blog", "index.html", HTML)
        r = self.write("blog", "blog/index.html", HTML.replace("hi", "OTHER"))
        self.assertTrue(r.startswith("error"), r)
        self.assertIn("hi", (self.root("blog") / "index.html").read_text())

    def test_two_plan_paths_that_collapse_are_an_error(self):
        r = self.plan("blog", "index.html", "Index.HTML")
        self.assertTrue(r.startswith("error"), r)
        r = pf.tool_project_plan({"project": "docs", "files": [{"path": "css/a.css"}, {"path": "docs/css/a.css"}]})
        self.assertTrue(r.startswith("error"), r)


# 8 / 9 ------------------------------------------------------------------------------------------------------------------------------
class FinishedRecord(Base):
    def test_a_finished_single_page_is_not_called_interrupted(self):
        self.page("solo")
        self.assertIn("solo", pf.open_project_note())
        out = pf.tool_project_finish({"project": "solo"})
        self.assertTrue(pf.finish_result_ok(out))
        self.assertEqual(pf.open_project_note(), "")
        self.assertIsNone(pf.finish_if_possible("solo"))
        self.assertTrue((self.root("solo") / ".finished.json").is_file())
        self.write("solo", "index.html", HTML.replace("hi", "edited"), replace=True)
        self.assertIn("solo", pf.open_project_note())

    def test_slug_prefix_collision(self):
        self.page("site")
        time.sleep(0.02)
        self.page("site-v2")
        pf.tool_project_finish({"project": "site-v2"})
        self.assertIn("`site`", pf.open_project_note())
        self.assertIsNotNone(pf.finish_if_possible("site"))
        self.assertIsNone(pf.finish_if_possible("site"))

    def test_same_project_name_in_another_chat_does_not_hide_this_one(self):
        pf.set_scope(2)
        self.page("blog")
        time.sleep(0.02)
        pf.set_scope(3)
        self.page("blog")
        pf.tool_project_finish({"project": "blog"})
        pf.set_scope(2)
        self.assertIn("blog", pf.open_project_note())
        out = pf.finish_if_possible("blog")
        self.assertIsNotNone(out)
        self.assertTrue(pf.finish_result_ok(out))

    def test_no_name_means_the_newest_unfinished_project_of_this_chat(self):
        self.page("one")
        time.sleep(0.02)
        self.page("two")
        pf.tool_project_finish({"project": "two"})
        out = pf.finish_if_possible()
        self.assertIn("Page one", out)
        self.assertIsNone(pf.finish_if_possible())

    def test_edit_with_a_future_or_equal_mtime_is_still_seen(self):
        import os
        self.page("skew")
        pf.tool_project_finish({"project": "skew"})
        f = self.root("skew") / "index.html"
        st = f.stat()
        self.write("skew", "index.html", HTML.replace("hi", "EDITED"), replace=True)
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))                         # same mtime as at the finish
        self.assertIsNotNone(pf.finish_if_possible("skew"))

    def test_projects_finished_before_the_record_use_the_strict_name_check(self):
        self.page("old")
        pf.tool_project_finish({"project": "old"})
        (self.root("old") / ".finished.json").unlink()
        self.assertEqual(pf.open_project_note(), "")                             # old-style `old-<8hex>.html` is newer
        (self.user_dir() / "old-v2-12345678.html").write_text("x")
        self.page("old-b")
        (self.root("old-b") / ".finished.json").unlink()
        (self.user_dir() / "old-b-v2-12345678.zip").write_text("x")              # not <slug>-<8 hex>: does not count
        self.assertIn("old-b", pf.open_project_note())


class ScopeWithoutSession(Base):
    def test_unsaved_chats_do_not_share_a_project(self):
        pf.set_scope(None)
        a = pf._scope()
        self.page("anon-a")
        pf.set_scope(None)
        self.assertNotEqual(pf._scope(), a)
        self.assertNotEqual(pf._scope(), "anon")
        self.assertEqual(pf.open_project_note(), "")
        self.assertEqual(pf.plan_items("anon-a"), [])


# 10 ---------------------------------------------------------------------------------------------------------------------------------
class SmartAppendKeepsWrappers(Base):
    def test_text_with_a_lt_sign_is_kept(self):
        out = pf._smart_html_append("<!doctype html><html><body><main><p>score <5", "<section>x</section>")
        self.assertIn("score <5", out)

    def test_cut_off_tag_is_removed(self):
        out = pf._smart_html_append('<!doctype html><html><body><main><p>a</p><div class="', "<p>x</p>")
        self.assertNotIn('<div class="', out)
        self.assertIn("<p>a</p>", out)

    def test_sections_land_inside_the_container(self):
        old = '<!doctype html><html><body><main><div class="container">\n<section id="a"><h2>A</h2></section>\n'
        out = pf._smart_html_append(old, '<section id="b"><h2>B</h2></section>')
        self.assertNotIn("</main>", out.split('id="b"')[0])
        self.assertNotIn("</div>", out.split('id="b"')[0])

    def test_a_card_link_is_not_closed_early(self):
        out = pf._smart_html_append('<!doctype html><html><body><main><a href="#" class="card">', '<div class="p">card body</div></a>')
        self.assertLess(out.index('class="card"'), out.index("card body"))
        self.assertNotIn("</a>", out.split("card body")[0])

    def test_text_cut_in_the_middle_is_still_closed(self):
        out = pf._smart_html_append("<body><div><p>Some text <em>emphasis", "<h3>Next</h3>")
        self.assertIn("</em>\n</p>", out)
        self.assertNotIn("</div>", out)

    def test_appended_script_text_continues_an_open_script(self):
        out = pf._smart_html_append("<html><body><script>var a=1;", "var b=2;")
        self.assertEqual(out.count("<script>"), 1)
        self.assertNotIn("</script>", out)


# 11 ---------------------------------------------------------------------------------------------------------------------------------
class StrictDownloadLookup(Base):
    def setUp(self):
        super().setUp()
        from routes.agent.download import _resolve_requested_file
        self.R = _resolve_requested_file
        u = self.user_dir()
        u.mkdir(parents=True, exist_ok=True)
        (u / "index-aaaaaaaa.html").write_text("<html>OLD index</html>")
        (u / "private-plan.pdf").write_text("x")
        (u / "report-1a2b3c4d.html").write_text("<html>R</html>")
        (u / "site-11111111.zip").write_text("z")
        (u / "plain.txt").write_text("t")

    def test_strict_allows_only_the_exact_name_or_a_versioned_one(self):
        R = self.R
        self.assertEqual(R("index.html", "common", False, strict=True).name, "index-aaaaaaaa.html")      # <stem>-<8hex><suffix>
        self.assertEqual(R("plain.txt", "common", False, strict=True).name, "plain.txt")
        self.assertEqual(R("site-11111111.zip", "common", False, strict=True).name, "site-11111111.zip")
        self.assertIsNone(R("site-22222222.zip", "common", False, strict=True))                     # a specific version: exactly that
        self.assertIsNone(R("*.pdf", "common", False, strict=True))
        self.assertIsNone(R("pri.pdf", "common", False, strict=True))
        self.assertIsNone(R("report.pdf", "common", False, strict=True))
        self.assertIsNone(R("private-plan.csv", "common", False, strict=True))

    def test_strict_never_matches_a_wildcard_or_prefix(self):
        self.assertIsNone(self.R("rep.html", "common", False, strict=True))
        self.assertIsNone(self.R("[a-z]*.html", "common", False, strict=True))

    def test_loose_lookup_keeps_its_behaviour_but_never_treats_the_name_as_a_pattern(self):
        self.assertEqual(self.R("pri.pdf", None, False).name, "private-plan.pdf")
        self.assertIsNone(self.R("*.pdf", None, False))
        self.assertIsNone(self.R("[a-z]*.pdf", None, False))

    def test_another_users_files_are_never_reached(self):
        v = self.user_dir(2)
        (v / "generated").mkdir(parents=True)
        (v / "generated" / "secret.png").write_bytes(b"x")
        (v / "payroll.csv").write_text("a")
        for req in ("generated/../../user_2/generated/secret.png", "../user_2/payroll.csv", "/etc/hostname"):
            self.assertIsNone(self.R(req, "common", False, strict=True))
            self.assertIsNone(self.R(req, "common", False))


# 12 ---------------------------------------------------------------------------------------------------------------------------------
class CardNumbersInProjectWrites(Base):
    def refused(self, content, rel):
        return (pf._pan_check(content, rel) or "").startswith("error: write refused")

    def test_a_number_split_over_two_appends_is_refused(self):
        self.plan("c", "index.html")
        self.write("c", "a.md", "line1\nline2\nMy visa number is 4111 1111")
        r = self.append("c", "a.md", "1111 1111 and more")
        self.assertTrue(r.startswith("error: write refused"), r)
        self.assertNotIn("1111 1111 and more", (self.root("c") / "a.md").read_text())
        self.write("c", "b.js", "var a = 1;\nvar s = '4111 1111 ")
        self.assertTrue(self.append("c", "b.js", "1111 1111';").startswith("error: write refused"))

    def test_file_names_and_plan_purposes_are_scanned(self):
        self.plan("c", "index.html")
        self.assertTrue(self.write("c", "4111111111111111.html", HTML).startswith("error: write refused"))
        self.assertTrue(self.write("c", "x/4111-1111-1111-1111.css", "a{}").startswith("error: write refused"))
        r = pf.tool_project_plan({"project": "c", "files": [{"path": "index.html", "purpose": f"show card {VISA}"}]})
        self.assertTrue(r.startswith("error: write refused"), r)
        self.assertTrue(pf.tool_project_plan({"project": VISA, "files": [{"path": "index.html", "purpose": "x"}]}).startswith("error"))

    def test_html_text_gets_the_contextual_check_but_code_does_not(self):
        self.assertTrue(self.refused("<p>Credit card number: 4111 1111 1111 1112, expiry 12/27</p>", "p.html"))
        self.assertTrue(self.refused("<p>Card 5105 1051 0510 5100</p>", "p.html"))
        self.assertTrue(self.refused("<p>Card number 4111 1111 1111 111</p>", "p.html"))
        code = "<script>const cardinal = 1; const card = [0xdeadbeefcafebabe, 4000000000000002001]; const id = abc4111111111111111;</script>"
        self.assertFalse(self.refused(code, "p.html"))
        self.assertFalse(self.refused("// card seed\nconst seed = 1234567890123456;\n", "a.js"))
        self.assertFalse(self.refused("<style>.card{width:1234567890123456px}</style><p>ok</p>", "p.html"))

    def test_identifier_tail_is_exempt_only_when_glued_in_code(self):
        self.assertTrue(self.refused("x4111 1111 1111 1111", "a.js"))
        self.assertTrue(self.refused("x-4111-1111-1111-1111", "a.js"))
        self.assertTrue(self.refused("card4111111111111111", "a.js"))
        self.assertTrue(self.refused("const visaNumber4111111111111111 = 1;", "a.js"))
        self.assertFalse(self.refused("const id = abc4111111111111111;", "a.js"))
        self.assertFalse(self.refused("const m = 0x4111111111111111; const y = 1.4111111111111111;", "a.js"))
        self.assertFalse(self.refused("<script>const id = abc4111111111111111;</script>", "p.html"))
        self.assertTrue(self.refused("<p>abc4111111111111111</p>", "p.html"))

    def test_markup_and_entity_split_numbers_are_found(self):
        for s in ("<p><span>4111</span><span>1111</span><span>1111</span><span>1111</span></p>",
                  "<p>4111&nbsp;1111&nbsp;1111&nbsp;1111</p>",
                  "<p>&#52;&#49;&#49;&#49; 1111 1111 1111</p>",
                  "<p><b>4111</b> <b>1111</b> <b>1111</b> <b>1111</b></p>",
                  "<p>4111<br>1111<br>1111<br>1111</p>"):
            self.assertTrue(self.refused(s, "p.html"), s)
            self.assertTrue(self.refused(s, "p.md"), s)

    def test_refusal_names_the_line_and_never_the_digits(self):
        r = pf._pan_check("var a = 1;\nvar b = 2;\nvar n = 4111111111111111;\n", "a.js")
        self.assertIn("line 3", r)
        self.assertNotIn("4111", r)
        r = pf._pan_check("<p>x</p>\n<p>y</p>\n<p>Card: 4111 1111 1111 1111</p>", "a.html")
        self.assertIn("line 3", r)
        self.assertNotIn("4111", r)

    def test_code_refusal_says_where_and_what_to_do_without_the_digits(self):
        r = pf._pan_check("var a = 1;\nvar b = 2;\nconst seed = 4111111111111111;\n", "js/game.js")
        self.assertTrue(r.startswith("error: write refused"), r)
        self.assertIn("line 3", r)
        self.assertIn("js/game.js", r)
        self.assertIn("payment card number", r)
        self.assertIn("shorter constant (under 13 digits)", r)
        self.assertIn("Math.random() / Date.now()", r)
        self.assertIn("If it is a real card number, remove it", r)
        self.assertNotIn("split", r.lower())                        # no recipe that would hide a real number
        self.assertNotRegex(r, r"\d{6,}")                            # never the digits (or any long run of them)

    def test_a_refused_write_leaves_no_file(self):
        self.plan("c", "index.html")
        r = self.write("c", "index.html", f"<p>pay with {VISA} now</p>")
        self.assertTrue(r.startswith("error: write refused"), r)
        self.assertFalse((self.root("c") / "index.html").exists())


# 13 ---------------------------------------------------------------------------------------------------------------------------------
class GenericErrors(Base):
    def test_file_and_folder_collisions_are_refused_up_front(self):
        self.plan("p", "index.html")
        self.assertTrue(self.write("p", "a.js", "var a=1;\nvar b=2;").startswith("Created"))
        for path in ("a.js/b.js", "a.js/b/c.js", "d.css/x.css", "x.css/"):
            r = self.write("p", path, "x=1;")
            self.assertTrue(r.startswith("error:"), (path, r))
            self.assertNotIn(self.td.name, r)
        self.write("p", "sub/x.js", "var q = 1;")
        r = self.write("p", "sub", "var q = 1;")                                     # no extension: refused as a type
        self.assertTrue(r.startswith("error:"))

    def test_os_errors_become_a_generic_message_without_a_server_path(self):
        self.plan("p", "index.html")
        with mock.patch.object(pf.os, "replace", side_effect=OSError(28, "No space left on device: '/srv/secret/path'")):
            for fn in (lambda: self.write("p", "a.js", "var a=1;"),
                       lambda: self.plan("p", "index.html", "b.js")):
                r = fn()
                self.assertTrue(r.startswith("error:"), r)
                self.assertNotIn("/srv", r)
                self.assertNotIn("Errno", r)
        self.write("p", "index.html", HTML)
        with mock.patch.object(pf, "_save_record", side_effect=OSError(13, "Permission denied: '/srv/x'")):
            r = pf.tool_project_finish({"project": "p"})
        self.assertTrue(r.startswith("error:"))
        self.assertNotIn("/srv", r)

    def test_odd_argument_types_do_not_raise(self):
        self.plan("p", "index.html")
        for c in (123, ["a"], {"x": 1}):
            self.assertIsInstance(self.write("p", "n.js", c), str)
        for pr in (None, 5, ["a"], {"a": 1}):
            self.assertIsInstance(self.write(pr, "n2.js", "var q=1"), str)
        self.assertIsInstance(pf.tool_project_write({"project": "p", "path": ["a"], "content": "var q=1"}), str)


# 14 ---------------------------------------------------------------------------------------------------------------------------------
class FinishMarkersAndCleanup(Base):
    def test_a_page_linking_to_missing_files_is_marked_not_finished(self):
        self.plan("m", "index.html")
        self.write("m", "index.html", '<!doctype html><html><head><link rel="stylesheet" href="css/missing.css"></head><body>x</body></html>')
        out = pf.tool_project_finish({"project": "m"})
        self.assertIn("[DOWNLOAD:", out)
        self.assertIn("(not finished:", out)
        self.assertIn("css/missing.css", out)
        self.assertFalse(pf.finish_result_ok(out))
        self.write("m", "css/missing.css", "body{}", replace=True)
        self.assertTrue(pf.finish_result_ok(pf.tool_project_finish({"project": "m"})))

    def test_forced_finish_with_planned_files_missing_is_marked(self):
        self.plan("m", "index.html", "a.js")
        self.write("m", "index.html", HTML)
        out = pf.finish_if_possible("m")
        self.assertFalse(pf.finish_result_ok(out), out)
        self.assertIn("a.js", out)
        self.assertIn("m", pf.open_project_note())                                  # still open for a "continue"

    def test_finish_result_ok(self):
        self.assertFalse(pf.finish_result_ok(""))
        self.assertFalse(pf.finish_result_ok(None))
        self.assertFalse(pf.finish_result_ok("error: nothing"))
        self.assertTrue(pf.finish_result_ok("Page x: 1 file. [DOWNLOAD: x-12345678.html]"))

    def test_only_the_newest_three_deliverable_sets_are_kept(self):
        self.plan("k", "index.html", "a.js")
        self.write("k", "index.html", HTML)
        other = self.user_dir() / "k-v2-12345678.zip"
        self.write("k", "a.js", "var a = 1;")
        self.user_dir().mkdir(parents=True, exist_ok=True)
        other.write_text("another project's")
        names = []
        for i in range(5):
            self.write("k", "a.js", f"var a = {i + 2};\n" * 3, replace=True)
            names.append(re.findall(r"\[DOWNLOAD: ([^\]]+)\]", pf.tool_project_finish({"project": "k"})))
        left = sorted(p.name for p in self.user_dir().iterdir() if re.fullmatch(r"k-[0-9a-f]{8}\.(zip|html)", p.name))
        self.assertEqual(len(left), 6)
        for older in names[:2]:
            for n in older:
                self.assertNotIn(n, left)
        for newer in names[2:]:
            for n in newer:
                self.assertIn(n, left)
        self.assertTrue(other.exists())

    def test_windows_unsafe_names_are_refused(self):
        self.plan("w", "index.html")
        for path in ("a:b.js", 'a"b.js', "a<b.js", "a|b.js", "a?b.js", "a*b.js", "CON.html", "con", "nul.txt", "Aux.css", "COM1.js", "lpt9.md",
                     "css/PRN.css", "a.js.", "dir./x.js", "bad\x01.js"):
            r = self.write("w", path, "var a=1;")
            self.assertTrue(r.startswith("error:"), (path, r))
        self.assertTrue(self.write("w", "console.js", "var a=1;").startswith("Created"))        # starts like CON, is not CON

    def test_names_that_differ_only_by_case_collide(self):
        self.plan("w", "index.html")
        self.write("w", "index.html", HTML)
        r = self.write("w", "INDEX.HTML", HTML.replace("hi", "other"))
        self.assertTrue(r.startswith("error:"), r)
        self.assertIn("index.html", r)
        self.write("w", "css/a.css", "a{}")
        self.assertTrue(self.write("w", "CSS/b.css", "a{}").startswith("error:"))


if __name__ == "__main__":
    unittest.main()
