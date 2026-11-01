import json
import os
import tempfile
import unittest

from server import read_events


class ReadEventsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "events.jsonl")

    def write(self, *lines):
        with open(self.path, "w", encoding="utf-8") as events:
            events.write("".join(line + "\n" for line in lines))

    def test_missing_file(self):
        self.assertEqual(read_events(self.path, 0), ([], 0))

    def test_returns_only_new_events(self):
        self.write(json.dumps({"n": 1}), json.dumps({"n": 2}))
        self.assertEqual(read_events(self.path, 1), ([{"n": 2}], 2))

    def test_restarted_file_is_read_from_the_beginning(self):
        self.write(json.dumps({"n": 1}))
        self.assertEqual(read_events(self.path, 9), ([{"n": 1}], 1))

    def test_partial_line_is_read_later(self):
        self.write(json.dumps({"n": 1}), '{"n":')
        self.assertEqual(read_events(self.path, 0), ([{"n": 1}], 1))


if __name__ == "__main__":
    unittest.main()
