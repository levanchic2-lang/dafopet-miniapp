import json
import unittest
from types import SimpleNamespace

from app.services.cbc_pdf import _build_html
from app.services.cbc_report import CBC_CODES, CBC_NAMES_ZH, normalize_results


class CbcChineseLabelsTests(unittest.TestCase):
    def test_every_cbc_code_has_a_chinese_label(self):
        self.assertEqual(set(CBC_CODES), set(CBC_NAMES_ZH))

    def test_normalized_rows_and_pdf_include_chinese_labels(self):
        rows = normalize_results([{"code": "WBC", "value": 8.2}], "dog")
        self.assertEqual(rows[0]["name_zh"], "白细胞总数")
        report = SimpleNamespace(
            results_json=json.dumps(rows, ensure_ascii=False), species="dog", tested_at="2026-10-18 14:38",
            created_at=None, id=6, device_model="BC-60R", sample_no="1095", vet_name="梁天兵",
            ai_analysis="—", doctor_conclusion="—",
        )
        pet = SimpleNamespace(name="嘟嘟", store="横岗店", breed="犬")
        customer = SimpleNamespace(name="罗先生", phone="13800000000")
        rendered = _build_html(report, customer, pet, None)
        self.assertIn("WBC", rendered)
        self.assertIn("白细胞总数", rendered)
        self.assertIn("网织红细胞血红蛋白含量", rendered)


if __name__ == "__main__":
    unittest.main()
