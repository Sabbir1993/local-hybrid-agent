import unittest
from core.agent_tools.project_files import (
    _smart_html_append,
    _balance_html_document,
    _get_open_tags,
    _calculate_auto_closing_tags,
)


class TestSmartHtmlAppend(unittest.TestCase):
    def test_user_dhaka_snippet_auto_closure(self):
        old_html = """<!DOCTYPE html>
<html lang="en">
<head><title>Dhaka Guide</title></head>
<body>
  <div class="min-h-screen bg-slate-900">
    <section id="climate" class="py-12">
      <div class="max-w-4xl mx-auto">
        <p class="text-slate-400 leading-relaxed">
          Located on the banks of the <strong>Buriganga River</strong>, Dhaka has a tropical monsoon climate with hot, humid summers and heavy rainfall during the monsoon season. The city experiences temperatures ranging from 13°C in winter to 35°C+ in summer [2](https://facts.net/world/cities/40-facts-about-dhaka/
</body>
</html>"""

        new_chunk = """  <!-- Hero Section -->
  <section id="hero" class="relative min-h-screen flex items-center justify-center overflow-hidden">
    <div class="container">
      <h1>Welcome to Dhaka</h1>
    </div>
  </section>"""

        result = _smart_html_append(old_html, new_chunk)

        # Verify that </p>, </div>, and </section> were injected before <!-- Hero Section -->
        expected_close = "</p>\n</div>\n</section>"
        self.assertIn(expected_close, result)
        self.assertIn("[2](https://facts.net/world/cities/40-facts-about-dhaka/)", result)
        hero_pos = result.find("<!-- Hero Section -->")
        close_pos = result.find(expected_close)
        self.assertTrue(close_pos != -1 and hero_pos != -1)
        self.assertLess(close_pos, hero_pos, "Auto-closing tags must appear before the new section")

    def test_unclosed_paragraph_before_heading(self):
        old = "<body><div><p>Some text <em>emphasis"
        new = "<h3>Next section</h3>"
        res = _smart_html_append(old, new)
        self.assertIn("</em>\n</p>", res)

    def test_container_children_not_prematurely_closed(self):
        old = "<body><div class='grid'><div class='col'>1</div>"
        new = "<div class='col'>2</div>\n</div>"
        res = _smart_html_append(old, new)
        # Should not close the grid prematurely before appending the next column
        self.assertNotIn("</div>\n</div>\n\n<div class='col'>2</div>", res)

    def test_balance_html_document(self):
        raw = "<body><div class='outer'><section><p>Unclosed text</body></html>"
        balanced = _balance_html_document(raw)
        self.assertIn("</p>\n</section>\n</div>\n</body>", balanced)


if __name__ == "__main__":
    unittest.main()
