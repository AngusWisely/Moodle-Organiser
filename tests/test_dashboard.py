import tempfile
import unittest
from pathlib import Path

from dashboard import index_pdfs, load_rows, render, safe_file, search, sync_state, versions_for
from demo import create_demo, pdf_bytes


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.materials = create_demo(Path(self.temp.name) / "demo") / "materials"

    def test_reads_sync_history_failures_and_versions_from_real_schema(self):
        self.assertEqual(len(load_rows(self.materials)), 5)
        self.assertEqual(sync_state(self.materials)["status"], "completed_with_errors")
        self.assertEqual(len(versions_for(self.materials)), 1)
        page = render(self.materials, {}, {"running": False})
        self.assertIn("Materials worksheet", page)
        self.assertIn("Previous versions (1)", page)
        self.assertIn("TimeoutError", page)

    def test_pdf_search_updates_one_changed_document(self):
        self.assertEqual(index_pdfs(self.materials)["indexed"], 3)
        self.assertEqual(index_pdfs(self.materials)["unchanged"], 3)
        self.assertEqual(search(self.materials, "heat transfer coefficient")[0][3], "1")
        path = self.materials / "Building Physics" / "Week 1" / "example-1.pdf"
        path.write_bytes(pdf_bytes(["Updated lecture", "Thermal bridge coefficient revision."]))
        self.assertEqual(index_pdfs(self.materials)["indexed"], 1)
        self.assertEqual(search(self.materials, "heat transfer coefficient"), [])
        self.assertIn("Thermal bridge", search(self.materials, "thermal bridge")[0][4])

    def test_cannot_open_unindexed_or_outside_file(self):
        self.assertIsNone(safe_file(self.materials, "../../etc/passwd"))
        version = versions_for(self.materials)[0]
        self.assertTrue(safe_file(self.materials, version["local_file"]).is_file())

    def test_replaced_unreadable_pdf_drops_stale_search_results(self):
        index_pdfs(self.materials)
        path = self.materials / "Building Physics" / "Week 1" / "example-1.pdf"
        path.write_bytes(b"not a PDF")
        self.assertEqual(index_pdfs(self.materials)["errors"], 1)
        self.assertEqual(search(self.materials, "heat transfer coefficient"), [])
        self.assertEqual(index_pdfs(self.materials)["errors"], 1)


if __name__ == "__main__":
    unittest.main()
