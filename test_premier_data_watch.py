import json
import unittest

from premier_data_watch import diff_rows, rows_from_html, snapshot_hash


PAGE = """
<html><body>
<table><tr><td>Home</td></tr></table>
<table>
  <tr><th>Name</th><th>Phone</th><th>When</th></tr>
  <tr><td>Ada</td><td>555-0100</td><td>9:00</td></tr>
  <tr><td>Bea</td><td>555-0101</td><td>9:30</td></tr>
</table>
</body></html>
"""


class WatchTests(unittest.TestCase):
    def test_largest_table_becomes_rows(self) -> None:
        rows = rows_from_html(PAGE, 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Name"], "Ada")
        self.assertEqual(rows[1]["Phone"], "555-0101")

    def test_same_rows_have_the_same_hash(self) -> None:
        rows = rows_from_html(PAGE, 2)
        self.assertEqual(snapshot_hash(rows), snapshot_hash(list(rows)))

    def test_diff_reports_added_and_removed(self) -> None:
        previous = [{"Name": "Ada", "Phone": "555-0100"}]
        current = [{"Name": "Bea", "Phone": "555-0101"}]
        added, removed = diff_rows(previous, current)
        self.assertEqual(added[0]["Name"], "Bea")
        self.assertEqual(removed[0]["Name"], "Ada")

    def test_unchanged_diff_is_empty(self) -> None:
        rows = [{"Name": "Ada"}]
        added, removed = diff_rows(rows, json.loads(json.dumps(rows)))
        self.assertEqual(added, [])
        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main()
