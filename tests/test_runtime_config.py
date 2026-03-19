import json
import tempfile
import unittest
from pathlib import Path

from runtime_config import RuntimeSettings


class RuntimeSettingsTests(unittest.TestCase):
    def test_missing_local_config_is_created_from_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            config_path = temp_root / "local_config.json"
            template_path = temp_root / "local_config.example.json"
            template_text = '{\n  "pushplus": {\n    "token": "from-template"\n  }\n}\n'
            template_path.write_text(template_text, encoding="utf-8")

            settings = RuntimeSettings(
                config_path=config_path,
                template_path=template_path,
                env={},
            )

            self.assertTrue(config_path.exists())
            self.assertTrue(settings.generated_local_config)
            self.assertEqual(config_path.read_text(encoding="utf-8"), template_text)
            self.assertEqual(settings.get("pushplus.token"), "from-template")

    def test_existing_local_config_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            config_path = temp_root / "local_config.json"
            template_path = temp_root / "local_config.example.json"
            config_path.write_text('{"pushplus":{"token":"existing"}}\n', encoding="utf-8")
            template_path.write_text('{"pushplus":{"token":"template"}}\n', encoding="utf-8")

            settings = RuntimeSettings(
                config_path=config_path,
                template_path=template_path,
                env={},
            )

            self.assertFalse(settings.generated_local_config)
            self.assertEqual(settings.get("pushplus.token"), "existing")
            self.assertIn("existing", config_path.read_text(encoding="utf-8"))

    def test_invalid_template_falls_back_to_builtin_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            config_path = temp_root / "local_config.json"
            template_path = temp_root / "local_config.example.json"
            template_path.write_text("{ invalid json }\n", encoding="utf-8")

            settings = RuntimeSettings(
                config_path=config_path,
                template_path=template_path,
                env={},
            )

            self.assertTrue(settings.generated_local_config)
            generated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(generated["wechat"]["openid_method"], "uiautomation")
            self.assertIn("cv", generated)

    def test_environment_overrides_local_config_and_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "local_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "pushplus": {"token": "local-token"},
                        "wechat": {"session_name": "本地微助教"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            settings = RuntimeSettings(
                config_path=config_path,
                env={
                    "PUSHPLUS_TOKEN": "env-token",
                },
            )

            self.assertEqual(settings.get("pushplus.token"), "env-token")
            self.assertEqual(settings.get("wechat.session_name"), "本地微助教")
            self.assertEqual(settings.get("openid.method"), "uiautomation")

    def test_nested_local_config_values_are_parsed_consistently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "local_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "cv": {
                            "template_scales": [1, 1.5, "2.0"],
                            "auto_switch_system_proxy": False,
                            "proxy_port": "9090",
                            "mitm_result_path": "logs/custom_result.txt",
                            "regions": {
                                "menu_button": [100, 200, 300, 400],
                            },
                        },
                        "mitmproxy": {
                            "target_domain": "demo.teachermate.test",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            settings = RuntimeSettings(config_path=config_path, env={})

            self.assertEqual(settings.get("cv.template_scales"), [1.0, 1.5, 2.0])
            self.assertFalse(settings.get("cv.auto_switch_system_proxy"))
            self.assertEqual(settings.get("cv.proxy_port"), 9090)
            self.assertEqual(settings.get("mitm.output_path"), Path("logs/custom_result.txt"))
            self.assertEqual(settings.get("mitm.target_domain"), "demo.teachermate.test")
            self.assertEqual(
                settings.resolve_value(
                    env_names=["WECHAT_CV_MENU_BUTTON_REGION"],
                    config_paths=[["cv", "regions", "menu_button"]],
                ),
                [100, 200, 300, 400],
            )

    def test_reload_refreshes_local_config_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "local_config.json"
            config_path.write_text(
                json.dumps({"pushplus": {"topic": "first"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            settings = RuntimeSettings(config_path=config_path, env={})

            self.assertEqual(settings.get("pushplus.topic"), "first")

            config_path.write_text(
                json.dumps({"pushplus": {"topic": "second"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            settings.reload()

            self.assertEqual(settings.get("pushplus.topic"), "second")


if __name__ == "__main__":
    unittest.main()
