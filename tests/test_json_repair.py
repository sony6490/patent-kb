import unittest
from patentkb.analyzer import _extract_json, _repair_truncated_json

class TestJsonRepair(unittest.TestCase):
    def test_clean_json(self):
        s = '{"keywords": [{"text": "RNA"}], "classifications": [], "patent_query": {"query": "RNA"}}'
        self.assertIsNotNone(_extract_json(s))

    def test_markdown_wrapped_json(self):
        s = '```json\n{"keywords": [{"text": "RNA"}], "classifications": [], "patent_query": {"query": "RNA"}}\n```'
        self.assertIsNotNone(_extract_json(s))

    def test_truncated_json_repair(self):
        # 模拟因为达到 2000 token 被腰斩截断的 JSON
        truncated = '{"keywords": [{"text": "RNA", "source_refs": ["C1"]}], "classifications": [{"code": "C12N", "explanation": "test'
        repaired = _extract_json(truncated)
        self.assertIsNotNone(repaired)
        self.assertIn("keywords", repaired)

if __name__ == "__main__":
    unittest.main()
