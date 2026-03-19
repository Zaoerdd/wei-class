import logging
import tempfile
import unittest
from pathlib import Path

from wechat_openid_collector import CollectorConfig
from wechat_openid_strategy import CVWeChatOpenIdCollector


class CVTemplateCandidateTests(unittest.TestCase):
    def build_collector(self, template_dir: Path, override_dir: Path) -> CVWeChatOpenIdCollector:
        config = CollectorConfig(
            session_name="微助教服务号",
            menu_button_prefix="学生",
            menu_item_prefix="全部",
            interval_hours=2.0,
            control_timeout_seconds=10.0,
            browser_timeout_seconds=15.0,
            output_path=template_dir / "collector_output.json",
            log_path=template_dir / "collector.log",
        )
        collector = CVWeChatOpenIdCollector(config, logging.getLogger("cv-template-test"))
        collector.template_dir = template_dir
        collector.template_override_dir = override_dir
        collector.template_scales = [1.0]
        collector.template_names = {
            "menu_item": "all_item.png",
        }
        return collector

    def test_template_candidates_fall_back_to_default_when_override_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            template_dir = root / "cv_templates"
            override_dir = root / "cv_templates_local"
            template_dir.mkdir()
            override_dir.mkdir()

            default_path = template_dir / "all_item.png"
            override_path = override_dir / "all_item.png"
            default_path.write_bytes(b"default-template")
            override_path.write_bytes(b"override-template")

            collector = self.build_collector(template_dir, override_dir)

            candidates = list(collector._iter_template_candidates("menu_item"))

            self.assertEqual(candidates, [override_path, default_path])

    def test_template_candidates_use_default_when_override_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            template_dir = root / "cv_templates"
            override_dir = root / "cv_templates_local"
            template_dir.mkdir()
            override_dir.mkdir()

            default_path = template_dir / "all_item.png"
            default_path.write_bytes(b"default-template")

            collector = self.build_collector(template_dir, override_dir)

            candidates = list(collector._iter_template_candidates("menu_item"))

            self.assertEqual(candidates, [default_path])


if __name__ == "__main__":
    unittest.main()
