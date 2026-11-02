"""Tests for mermaid-viewer.py.  Run:  python -m unittest -v test_mermaid_viewer

Pure-function tests run anywhere. GUI tests drive the real window offscreen and
need the Mermaid bundles, which are downloaded once into the temp folder (the
GUI tests are skipped when that is impossible, e.g. offline).
"""
import importlib.util
import os
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

HERE = Path(__file__).resolve().parent
BUNDLE_CACHE = Path(tempfile.gettempdir()) / "mermaid-studio-test-bundles"
DATA_DIR = Path(tempfile.mkdtemp(prefix="mstudio-data-"))
os.environ["MERMAID_STUDIO_DATA"] = str(DATA_DIR)

spec = importlib.util.spec_from_file_location("mermaid_viewer", HERE / "mermaid-viewer.py")
mv = importlib.util.module_from_spec(spec)
sys.modules["mermaid_viewer"] = mv
spec.loader.exec_module(mv)

from PySide6.QtCore import QSettings, QUrl
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])


def pump(ms: int = 0, until=None, timeout: int = 20000):
    """Process events for `ms`, or until `until()` is truthy (up to `timeout`)."""
    end = time.monotonic() + (timeout if until else ms) / 1000
    while time.monotonic() < end:
        app.processEvents()
        if until and until():
            return True
        time.sleep(0.01)
    return bool(until and until())


def ensure_bundles() -> bool:
    BUNDLE_CACHE.mkdir(exist_ok=True)
    for ver in mv.MERMAID_VERSIONS:
        dest = BUNDLE_CACHE / f"mermaid-{ver}.min.js"
        if not dest.exists():
            try:
                with urllib.request.urlopen(mv.mermaid_cdn_url(ver), timeout=60) as r:
                    dest.write_bytes(r.read())
            except OSError:
                return False
        (DATA_DIR / dest.name).write_bytes(dest.read_bytes())
    return True


class PureFunctionTests(unittest.TestCase):
    def test_version_helpers(self):
        self.assertEqual(mv.version_major("11.17.2"), 11)
        self.assertEqual(mv.version_major("bogus"), 10)
        self.assertIn("mermaid@10.9.8", mv.mermaid_cdn_url("10.9.8"))

    def test_valid_colors_filters_bad_values(self):
        out = mv.valid_colors({"background": "#123456", "primaryColor": "red", "x": "#000000"})
        self.assertEqual(out["background"], "#123456")
        self.assertEqual(out["primaryColor"], mv.DEFAULT_COLORS["primaryColor"])
        self.assertNotIn("x", out)

    def test_lint_diagram_finds_unresolved_style_references(self):
        source = ("flowchart TD\n A[One] --> B[Two]\n"
                  " style Missing fill:#fff\n class A absent\n")
        self.assertEqual(mv.lint_diagram(source), [
            (3, "Style target 'Missing' is not defined in this diagram."),
            (4, "Class 'absent' has no matching classDef."),
        ])
        self.assertEqual(mv.lint_diagram("sequenceDiagram\n A->>B: hi\n"), [])

    def test_standalone_html_embeds_runtime_and_escapes_script_terminators(self):
        doc = mv.build_standalone_html(
            "flowchart TD\n A[</script>]\n", "window.mermaid={};", "sample",
            mv.DEFAULT_COLORS)
        self.assertIn("<title>sample</title>", doc)
        self.assertIn("window.mermaid={};", doc)
        self.assertIn(r"\u003c/script>", doc)
        self.assertIn("themeVariables", doc)

    def test_parse_outline(self):
        text = "flowchart TD\n    subgraph One\n        A --> B\n    end\n"
        items = mv.parse_outline(text)
        self.assertEqual(items[0][2:], ("diagram", "flowchart TD"))
        self.assertEqual(items[1][2:], ("subgraph", "One"))

    def test_generator_flowchart(self):
        out = mv.outline_to_mermaid("Start -> Check input? -> Save -> End")
        self.assertTrue(out.startswith("flowchart TD"))
        self.assertIn('{"Check input?"}', out)

    def test_generator_sequence(self):
        out = mv.outline_to_mermaid("User -> Server: login\nServer --> User: token")
        self.assertTrue(out.startswith("sequenceDiagram"))
        self.assertIn("User->>Server: login", out)
        self.assertIn("Server-->>User: token", out)

    def test_generator_mindmap(self):
        out = mv.outline_to_mermaid("Plan\n  Build\n    Code\n  Ship", "mindmap")
        lines = out.splitlines()
        self.assertEqual(lines[0], "mindmap")
        self.assertIn("root((Plan))", lines[1])

    def test_read_text_file_limits_and_encoding(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.mmd"
            p.write_bytes(b"\xef\xbb\xbfflowchart TD\n")
            self.assertEqual(mv.read_text_file(p), ("flowchart TD\n", False))
            p.write_bytes(b"caf\xe9")
            text, lossy = mv.read_text_file(p)
            self.assertTrue(lossy)
            self.assertIn("caf", text)
            p.write_bytes(b"x" * (mv.MAX_OPEN_BYTES + 1))
            with self.assertRaises(ValueError):
                mv.read_text_file(p)


class StyleFunctionTests(unittest.TestCase):
    BASE = "flowchart TD\n A[One] --> B[Two]\n"

    def test_kind_and_elements(self):
        self.assertEqual(mv.diagram_kind("---\ntitle: x\n---\n%% c\ngraph LR\n A-->B"), "flowchart")
        text = ('flowchart LR\n subgraph S1[Group]\n  A[Start] -- yes --> B{Pick}\n end\n'
                ' B -->|no| C(("Round"))\n classDef good fill:#fff\n class A good\n style C fill:#eee\n')
        self.assertEqual(mv.list_elements(text), ["S1", "A", "B", "C", "classDef:good"])
        self.assertEqual(mv.list_elements("pie\n \"a\": 1"), [])

    def test_chart_roundtrip_and_clear(self):
        out = mv.apply_chart_style(self.BASE, {"font_size": 20, "padding": 12,
                                               "fill": "#abcdef", "stroke_width": 4})
        self.assertTrue(out.startswith("%%{init:"))
        cfg = mv.read_init(out)
        self.assertEqual(cfg["themeVariables"]["fontSize"], "20px")
        self.assertEqual(cfg["flowchart"]["padding"], 12)
        got = mv.chart_style_options(out)
        self.assertEqual((got["font_size"], got["padding"], got["stroke_width"]), (20, 12, 4))
        again = mv.apply_chart_style(out, {"font_size": 14})
        self.assertEqual(again.count("%%{init"), 1)
        self.assertEqual(mv.chart_style_options(again)["padding"], 12)
        self.assertEqual(mv.apply_chart_style(again, {}, clear=True), self.BASE)

    def test_chart_keeps_front_matter_and_existing_init(self):
        fm = "---\ntitle: T\n---\nflowchart TD\n A-->B\n"
        out = mv.apply_chart_style(fm, {"font_size": 10})
        self.assertTrue(out.startswith("---\ntitle: T\n---\n%%{init:"))
        existing = "%%{init: {'theme':'dark','flowchart':{'curve':'basis'}}}%%\nflowchart TD\n A-->B\n"
        cfg = mv.read_init(mv.apply_chart_style(existing, {"padding": 9}))
        self.assertEqual(cfg["theme"], "dark")
        self.assertEqual(cfg["flowchart"], {"curve": "basis", "padding": 9})

    def test_element_style_flow_and_class_kinds(self):
        out = mv.apply_element_style(self.BASE, "B", {"fill": "#f00", "font_size": 18})
        out = mv.apply_element_style(out, "B", {"stroke": "#000"})
        self.assertEqual(out.count("style B "), 1)
        opts = mv.element_style_options(out, "B")
        self.assertEqual((opts["fill"], opts["stroke"], opts["font_size"]), ("#f00", "#000", 18))
        self.assertEqual(mv.apply_element_style(out, "B", {}, clear=True), self.BASE)
        st = "stateDiagram-v2\n S1 --> S2\n"
        out = mv.apply_element_style(st, "S2", {"fill": "#0f0"})
        self.assertIn("classDef ms_S2 fill:#0f0\n", out)
        self.assertEqual(out.count("class S2 ms_S2"), 1)
        out = mv.apply_element_style(out, "S2", {"stroke": "#111"})
        self.assertEqual(out.count("class S2 ms_S2"), 1)
        self.assertEqual(mv.apply_element_style(out, "S2", {}, clear=True), st)

    def test_preview_opacity_style_round_trip(self):
        out = mv.apply_element_style(self.BASE, "B", {"opacity": 0.6})
        self.assertIn("style B opacity:0.6", out)
        self.assertEqual(mv.element_style_options(out, "B")["opacity"], "0.6")
        self.assertEqual(mv.apply_element_style(out, "B", {}, clear=True), self.BASE)

    def test_font_family_is_sanitised(self):
        p = mv.element_props({"font_family": "Courier New, monospace;x"})
        self.assertEqual(p["font-family"], "Courier New")


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.ed = mv.EditorEdit()

    def test_toggle_comment_round_trip(self):
        self.ed.setPlainText("a\nb")
        self.ed.selectAll()
        self.ed.toggle_comment()
        self.assertEqual(self.ed.toPlainText(), "%% a\n%% b")
        self.ed.selectAll()
        self.ed.toggle_comment()
        self.assertEqual(self.ed.toPlainText(), "a\nb")

    def test_indent_unindent(self):
        self.ed.setPlainText("a\nb")
        self.ed.selectAll()
        self.ed.indent_selection()
        self.assertEqual(self.ed.toPlainText(), "    a\n    b")
        self.ed.selectAll()
        self.ed.unindent_selection()
        self.assertEqual(self.ed.toPlainText(), "a\nb")

    def test_auto_indent_keeps_and_adds_indent(self):
        self.ed.setPlainText("    a")
        self.ed.moveCursor(QTextCursor.MoveOperation.End)
        self.ed._auto_indent_newline()
        self.assertEqual(self.ed.toPlainText(), "    a\n    ")
        self.ed.setPlainText("subgraph X")
        self.ed.moveCursor(QTextCursor.MoveOperation.End)
        self.ed._auto_indent_newline()
        self.assertEqual(self.ed.toPlainText(), "subgraph X\n    ")

    def test_snippet_places_cursor(self):
        self.ed.insert_snippet("Note over A,B: $0")
        self.assertEqual(self.ed.toPlainText(), "Note over A,B: ")
        self.assertEqual(self.ed.textCursor().position(), len("Note over A,B: "))

    def test_error_line_selection_and_goto(self):
        self.ed.setPlainText("a\nb\nc")
        self.ed.set_error_line(2)
        self.assertEqual(self.ed.error_line, 2)
        self.ed.goto_line(3)
        self.assertEqual(self.ed.textCursor().blockNumber(), 2)

    def test_find_and_replace_all(self):
        self.ed.setPlainText("foo Foo foo")
        bar = mv.FindBar(lambda: self.ed)
        bar._find.setText("foo")
        bar._repl.setText("x")
        bar.replace_all()
        self.assertEqual(self.ed.toPlainText(), "x x x")
        bar._case.setChecked(True)
        self.ed.setPlainText("foo Foo foo")
        bar.replace_all()
        self.assertEqual(self.ed.toPlainText(), "x Foo x")


@unittest.skipUnless(ensure_bundles(), "Mermaid bundles unavailable (offline?)")
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ini = str(DATA_DIR / "settings.ini")
        cls.w = mv.MermaidStudio(QSettings(cls.ini, QSettings.Format.IniFormat), restore=False)
        cls.w.show()
        assert pump(until=lambda: cls.w._loaded), "preview page never became ready"

    @classmethod
    def tearDownClass(cls):
        cls.w._session_dirty = False
        cls.w.close()
        pump(200)

    def render(self, text: str | None = None, timeout: int = 20000):
        w = self.w
        if text is not None:
            w._cur.editor.setPlainText(text)
        done: list = []
        w._after_render(lambda *a: done.append(a), timeout)
        w.render_now()
        self.assertTrue(pump(until=lambda: done, timeout=timeout + 1000))
        return done[0]

    def js(self, code: str):
        box: list = []
        self.w._view.run_js(code, lambda r: box.append(r))
        self.assertTrue(pump(until=lambda: box, timeout=5000))
        return box[0]

    def test_01_default_renders_and_has_svg(self):
        ok, msg, line = self.render()
        self.assertTrue(ok, msg)
        self.assertGreater(self.js("document.querySelectorAll('#wrap svg').length"), 0)

    def test_02_error_keeps_last_diagram_and_marks_line(self):
        self.assertTrue(self.render("flowchart TD\n A-->B")[0])
        ok, msg, line = self.render("flowchart TD\n A-->B\n B-->>>>C[")
        self.assertFalse(ok)
        self.assertIsNotNone(self.w._cur.error)
        self.assertEqual(self.js("document.querySelectorAll('#wrap svg').length"), 1)
        self.assertEqual(self.js("document.querySelectorAll('body > svg').length"), 0)
        self.assertTrue(self.w._err_label.isVisibleTo(self.w))
        if line:
            self.assertEqual(self.w._cur.editor.error_line, line)
        self.assertTrue(self.render("flowchart TD\n A-->B")[0])
        self.assertIsNone(self.w._cur.error)
        self.assertEqual(self.w._cur.editor.error_line, 0)

    def test_03_pan_zoom_state(self):
        self.render("flowchart TD\n A-->B")
        self.js("actual_size(true); view.zoom")
        pump(300)
        self.assertAlmostEqual(self.w._cur.view["zoom"], 1.0, places=2)
        self.assertFalse(self.w._cur.view["fit"])
        self.w._push_view(self.w._cur)
        self.js("zoom_by(2); 1")
        pump(300)
        self.assertAlmostEqual(self.w._cur.view["zoom"], 2.0, places=2)
        self.js("fit_view(true); 1")
        pump(300)
        self.assertTrue(self.w._cur.view["fit"])

    def test_04_svg_export_contains_labels(self):
        self.render("flowchart TD\n A[Hello world] --> B{Pick one}")
        svg = self.js("export_svg()")
        self.assertIn("Hello world", svg)
        self.assertIn("Pick one", svg)

    def test_05_png_pdf_svg_md_export(self):
        self.render("flowchart TD\n A[Hello] --> B[World]")
        with tempfile.TemporaryDirectory() as d:
            for fmt in ("png", "svg", "pdf", "html"):
                result: list = []
                p = Path(d) / f"out.{fmt}"
                self.w._write_export(fmt, p, lambda ok, err: result.append((ok, err)))
                self.assertTrue(pump(until=lambda: result, timeout=15000), fmt)
                self.assertEqual(result[0], (True, ""), fmt)
                self.assertGreater(p.stat().st_size, 500, fmt)
            self.assertEqual(Path(d, "out.png").read_bytes()[:4], b"\x89PNG")
            self.assertEqual(Path(d, "out.pdf").read_bytes()[:4], b"%PDF")
            html_export = Path(d, "out.html").read_text(encoding="utf-8")
            self.assertIn("Hello", html_export)
            self.assertIn("mermaid.initialize", html_export)
            self.assertNotIn("cdn.jsdelivr.net", html_export)
            self.w._loaded = False
            self.w._view.load(QUrl.fromLocalFile(str(Path(d, "out.html"))))
            self.assertTrue(pump(until=lambda:
                                 self.js("!!document.querySelector('#diagram svg')"),
                                 timeout=15000), "standalone HTML did not render")
            self.w._load_preview()
            self.assertTrue(pump(until=lambda: self.w._loaded, timeout=30000),
                            "preview was not restored after standalone export test")

    def test_06_png_scale_matches_setting(self):
        self.render("flowchart TD\n A-->B")
        got: list = []
        self.w._request_png(1, False, lambda img, err: got.append((img, err)))
        self.assertTrue(pump(until=lambda: got, timeout=15000))
        img1 = got[0][0]
        self.assertIsNotNone(img1, got[0][1])
        got.clear()
        self.w._request_png(2, False, lambda img, err: got.append((img, err)))
        self.assertTrue(pump(until=lambda: got, timeout=15000))
        self.assertAlmostEqual(got[0][0].width() / img1.width(), 2.0, delta=0.05)

    def test_07_single_render_per_new_tab_and_switch(self):
        count = []
        orig = self.w._view.run_js

        def spy(code, cb=None):
            if code.startswith("mermaid_render"):
                count.append(1)
            return orig(code, cb)
        self.w._view.run_js = spy
        try:
            self.w._add_tab(mv.DEFAULT_TEXT)
            pump(300)
            self.assertEqual(len(count), 1)
            count.clear()
            self.w._tabs.setCurrentIndex(0)
            pump(300)
            self.assertEqual(len(count), 1)
        finally:
            self.w._view.run_js = orig
            while len(self.w._models) > 1:
                self.w._models[-1].dirty = False
                self.w._remove_tab(len(self.w._models) - 1)

    def test_08_session_round_trip(self):
        self.render("flowchart TD\n S-->T")
        tab = self.w._cur
        tab.dirty = True
        tab.theme_name = ""
        tab.colors = dict(mv.DEFAULT_COLORS, background="#101010")
        tab.view = {"zoom": 1.5, "x": 10.0, "y": 20.0, "fit": False}
        self.w._save_session()
        other = mv.MermaidStudio(
            QSettings(str(DATA_DIR / "other.ini"), QSettings.Format.IniFormat), restore=True)
        try:
            t = other._models[0]
            self.assertIn("S-->T", t.editor.toPlainText())
            self.assertTrue(t.dirty)
            self.assertEqual(t.colors["background"], "#101010")
            self.assertEqual(t.view["zoom"], 1.5)
        finally:
            other._session_dirty = False
            other._save_session = lambda: None
            other.close()
        tab.dirty = False

    def test_09_open_save_and_live_reload(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.mmd"
            p.write_text("flowchart TD\n A-->B\n", encoding="utf-8")
            self.w._open_path(p)
            tab = self.w._cur
            self.assertEqual(tab.path, p)
            self.assertFalse(tab.dirty)
            p.write_text("flowchart TD\n A-->B\n B-->C\n", encoding="utf-8")
            os.utime(p, ns=(time.time_ns() + 5_000_000_000,) * 2)
            self.assertTrue(pump(until=lambda: "B-->C" in tab.editor.toPlainText(), timeout=5000))
            tab.editor.setPlainText("flowchart TD\n Q-->R\n")
            self.w.save_file()
            self.assertIn("Q-->R", p.read_text(encoding="utf-8"))
            self.assertFalse(tab.dirty)
            tab.dirty = False
            self.w._remove_tab(self.w._tabs.indexOf(tab.editor))

    def test_10_project_workspace_opens_diagram_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            diagram = root / "workspace.mmd"
            diagram.write_text("flowchart TD\n A-->B\n", encoding="utf-8")
            (root / "other.mmd").write_text("flowchart LR\n X-->Y\n", encoding="utf-8")
            self.w._set_workspace_root(root, announce=False)
            index = self.w._workspace_model.index(str(diagram))
            self.assertTrue(index.isValid())
            self.assertEqual(Path(self.w._workspace_model.filePath(index)), diagram)
            self.w._workspace_search.setText("workspace")
            proxy_index = self.w._workspace_proxy.mapFromSource(index)
            self.assertTrue(proxy_index.isValid())
            self.w._open_workspace_index(proxy_index)
            self.assertEqual(self.w._cur.path, diagram)
            self.w.close_workspace()
            self.w._remove_tab(self.w._tabs.currentIndex())

    def test_11_all_samples_render(self):
        failures = []
        for name, text in mv.PRESETS.items():
            ok, msg, _ = self.render(text)
            if not ok:
                failures.append(f"{name}: {msg.splitlines()[0]}")
        self.assertEqual(failures, [])

    def test_12_mermaid11_samples_and_all_samples(self):
        w = self.w
        w._set_mermaid_version("11.17.2")
        self.assertTrue(pump(until=lambda: w._loaded, timeout=30000))
        failures = []
        for name, text in {**mv.PRESETS_V11, **mv.PRESETS}.items():
            ok, msg, _ = self.render(text)
            if not ok:
                failures.append(f"{name}: {msg.splitlines()[0]}")
        w._set_mermaid_version(mv.DEFAULT_MERMAID_VERSION)
        self.assertTrue(pump(until=lambda: w._loaded, timeout=30000))
        self.assertEqual(failures, [])

    def test_13_batch_export(self):
        w = self.w
        w._add_tab("flowchart TD\n P-->Q")
        w._add_tab("sequenceDiagram\n A->>B: hi")
        pump(300)
        with tempfile.TemporaryDirectory() as d:
            w._batch = {"i": 0, "orig": 0, "folder": Path(d), "fmt": "png", "ok": 0,
                        "failed": [], "used": set()}
            w._batch_step()
            self.assertTrue(pump(until=lambda: w._batch is None, timeout=60000))
            self.assertEqual(len(list(Path(d).glob("*.png"))), len(w._models))
        while len(w._models) > 1:
            w._models[-1].dirty = False
            w._remove_tab(len(w._models) - 1)

    def test_14_style_editing_renders(self):
        w = self.w
        base = "flowchart TD\n A[One] --> B[Two]\n"
        text = mv.apply_chart_style(base, {"font_family": "Courier New", "font_size": 22,
                                           "stroke_width": 5, "radius": 12,
                                           "padding": 30, "node_spacing": 90})
        text = mv.apply_element_style(text, "B", {"fill": "#ff0000", "font_weight": "bold"})
        ok, msg, _ = self.render(text)
        self.assertTrue(ok, msg)
        self.assertEqual(self.js(
            "getComputedStyle(document.querySelector('#wrap svg .node rect')).strokeWidth"), "5px")
        self.assertEqual(self.js(
            "getComputedStyle(document.querySelector('#wrap svg .node rect')).rx"), "12px")
        self.assertIn("Courier New", self.js(
            "getComputedStyle(document.querySelector('#wrap svg .nodeLabel')).fontFamily"))
        self.assertEqual(self.js(
            "[...document.querySelectorAll('#wrap svg .node')]"
            ".filter(n => n.textContent.includes('Two'))"
            ".map(n => getComputedStyle(n.querySelector('rect')).fill)[0]"), "rgb(255, 0, 0)")
        for kind, src in (("state", "stateDiagram-v2\n S1 --> S2\n"),
                          ("class", "classDiagram\n class Cat\n Cat <|-- Dog\n")):
            t = mv.apply_element_style(src, "S2" if kind == "state" else "Dog",
                                       {"fill": "#00ff00", "stroke_width": 3})
            t = mv.apply_chart_style(t, {"font_size": 18})
            ok, msg, _ = self.render(t)
            self.assertTrue(ok, f"{kind}: {msg}")

    def test_15_preview_styles_are_written_to_mermaid_source(self):
        base = "flowchart TD\n A[One] --> B[Two]\n"
        self.assertTrue(self.render(base)[0])
        self.assertEqual(self.js(
            "source_id_of(document.querySelector('#wrap svg g.node'))"), "A")
        self.js("bridge_call('previewStyleChanged', 'B', 'fill', '#ff0000'); 'ok'")
        self.assertTrue(pump(until=lambda:
                             "style B fill:#ff0000" in self.w._cur.editor.toPlainText()))
        self.assertIn("style B fill:#ff0000", self.w._cur.editor.toPlainText())
        self.assertTrue(self.w._cur.dirty)
        self.js("bridge_call('previewStyleChanged', 'B', 'opacity', '0.5'); 'ok'")
        self.assertTrue(pump(until=lambda:
                             "opacity:0.5" in self.w._cur.editor.toPlainText()))
        self.assertIn("opacity:0.5", self.w._cur.editor.toPlainText())
        self.js("bridge_call('previewStyleChanged', 'B', 'reset', ''); 'ok'")
        self.assertTrue(pump(until=lambda:
                             "style B " not in self.w._cur.editor.toPlainText()))
        self.assertNotIn("style B ", self.w._cur.editor.toPlainText())


if __name__ == "__main__":
    unittest.main()
