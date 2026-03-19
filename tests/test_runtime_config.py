import json
import tempfile
import unittest
from pathlib import Path

from runtime_config import RuntimeSettings


class RuntimeSettingsTests(unittest.TestCase):
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
