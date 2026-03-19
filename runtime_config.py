from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple


logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
LOCAL_CONFIG_PATH = BASE_DIR / "local_config.json"
LOCAL_CONFIG_TEMPLATE_PATH = BASE_DIR / "local_config.example.json"
_UNSET = object()

DEFAULT_LOCAL_CONFIG_TEMPLATE = {
    "wechat": {
        "openid_method": "uiautomation",
        "session_name": "微助教服务号",
        "menu_button": "学生",
        "menu_item": "全部",
        "control_timeout_seconds": 10,
        "browser_timeout_seconds": 15,
    },
    "pushplus": {
        "token": "",
        "topic": "",
    },
    "cv": {
        "template_override_dir": "cv_templates_local",
        "match_threshold": 0.82,
        "template_scales": [1.0, 1.25, 1.5, 1.75, 2.0],
        "auto_switch_system_proxy": True,
        "templates": {
            "session": "session.png",
            "menu_button": "student_button.png",
            "menu_item": "all_item.png",
            "close": "close_button.png",
        },
    },
    "mitmproxy": {
        "output_path": "logs/mitm_openid_result.txt",
        "target_domain": "v18.teachermate.cn",
    },
}


def _parse_text(value: Any) -> Optional[str]:
    text = str(value).strip()
    return text or None


def _parse_float(value: Any) -> Optional[float]:
    text = _parse_text(value)
    if text is None:
        return None
    return float(text)


def _parse_int(value: Any) -> Optional[int]:
    text = _parse_text(value)
    if text is None:
        return None
    return int(text)


def _parse_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    text = _parse_text(value)
    if text is None:
        return None
    return text.lower() not in {"0", "false", "no", "off"}


def _parse_path(value: Any) -> Optional[Path]:
    if isinstance(value, Path):
        return value
    text = _parse_text(value)
    if text is None:
        return None
    return Path(text).expanduser()


def _parse_string_list(value: Any) -> Optional[list[str]]:
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value]
    else:
        text = _parse_text(value)
        if text is None:
            return None
        items = [part.strip() for part in text.split(",")]
    return [item for item in items if item]


def _parse_float_list(value: Any) -> Optional[list[float]]:
    items = _parse_string_list(value)
    if items is None:
        return None
    return [float(item) for item in items]


@dataclass(frozen=True)
class RuntimeSettingDefinition:
    key: str
    env_names: Tuple[str, ...]
    config_paths: Tuple[Tuple[str, ...], ...]
    default: Any = None
    parser: Optional[Callable[[Any], Any]] = None


def load_local_config(config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        return {}

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("加载本地配置文件失败 %s: %s", config_path, exc)
        return {}

    if not isinstance(payload, dict):
        logger.warning("本地配置文件格式无效，应为 JSON 对象: %s", config_path)
        return {}

    return payload


def render_default_local_config_template() -> str:
    return json.dumps(DEFAULT_LOCAL_CONFIG_TEMPLATE, ensure_ascii=False, indent=2) + "\n"


def ensure_local_config_exists(
    config_path: Path,
    template_path: Path = LOCAL_CONFIG_TEMPLATE_PATH,
) -> bool:
    if config_path.exists():
        return False

    config_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        template_text = template_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        template_text = render_default_local_config_template()
    except Exception as exc:
        logger.warning("读取本地配置模板失败 %s: %s", template_path, exc)
        template_text = render_default_local_config_template()

    if not template_text.strip():
        template_text = render_default_local_config_template()
    else:
        try:
            payload = json.loads(template_text)
        except Exception as exc:
            logger.warning("本地配置模板格式无效，改用内置模板 %s: %s", template_path, exc)
            template_text = render_default_local_config_template()
        else:
            if not isinstance(payload, dict):
                logger.warning("本地配置模板不是 JSON 对象，改用内置模板: %s", template_path)
                template_text = render_default_local_config_template()

    if not template_text.endswith("\n"):
        template_text += "\n"

    config_path.write_text(template_text, encoding="utf-8")
    logger.info("已自动生成本地配置文件: %s", config_path)
    return True


def _setting(
    key: str,
    env_names: Sequence[str],
    config_paths: Sequence[Sequence[str]],
    default: Any,
    parser: Optional[Callable[[Any], Any]] = None,
) -> RuntimeSettingDefinition:
    return RuntimeSettingDefinition(
        key=key,
        env_names=tuple(env_names),
        config_paths=tuple(tuple(path) for path in config_paths),
        default=default,
        parser=parser,
    )


RUNTIME_SETTING_DEFINITIONS: Dict[str, RuntimeSettingDefinition] = {
    "openid.method": _setting(
        "openid.method",
        ["WECHAT_OPENID_METHOD"],
        [["wechat", "openid_method"], ["openid_method"]],
        "uiautomation",
        _parse_text,
    ),
    "wechat.session_name": _setting(
        "wechat.session_name",
        ["WECHAT_SESSION_NAME"],
        [["wechat", "session_name"], ["session_name"]],
        "微助教服务号",
        _parse_text,
    ),
    "wechat.menu_button": _setting(
        "wechat.menu_button",
        ["WECHAT_MENU_BUTTON"],
        [["wechat", "menu_button"], ["menu_button"]],
        "学生",
        _parse_text,
    ),
    "wechat.menu_item": _setting(
        "wechat.menu_item",
        ["WECHAT_MENU_ITEM"],
        [["wechat", "menu_item"], ["menu_item"]],
        "全部",
        _parse_text,
    ),
    "wechat.control_timeout_seconds": _setting(
        "wechat.control_timeout_seconds",
        ["WECHAT_CONTROL_TIMEOUT"],
        [["wechat", "control_timeout_seconds"]],
        10.0,
        _parse_float,
    ),
    "wechat.browser_timeout_seconds": _setting(
        "wechat.browser_timeout_seconds",
        ["WECHAT_BROWSER_TIMEOUT"],
        [["wechat", "browser_timeout_seconds"]],
        15.0,
        _parse_float,
    ),
    "pushplus.token": _setting(
        "pushplus.token",
        ["PUSHPLUS_TOKEN"],
        [["pushplus", "token"], ["pushplus_token"]],
        "",
        _parse_text,
    ),
    "pushplus.topic": _setting(
        "pushplus.topic",
        ["PUSHPLUS_TOPIC"],
        [["pushplus", "topic"], ["pushplus_topic"]],
        "",
        _parse_text,
    ),
    "cv.template_dir": _setting(
        "cv.template_dir",
        ["WECHAT_CV_TEMPLATE_DIR"],
        [["cv", "template_dir"]],
        BASE_DIR / "cv_templates",
        _parse_path,
    ),
    "cv.template_override_dir": _setting(
        "cv.template_override_dir",
        ["WECHAT_CV_TEMPLATE_OVERRIDE_DIR"],
        [["cv", "template_override_dir"]],
        BASE_DIR / "cv_templates_local",
        _parse_path,
    ),
    "cv.match_threshold": _setting(
        "cv.match_threshold",
        ["WECHAT_CV_MATCH_THRESHOLD"],
        [["cv", "match_threshold"]],
        0.82,
        _parse_float,
    ),
    "cv.template_scales": _setting(
        "cv.template_scales",
        ["WECHAT_CV_TEMPLATE_SCALES"],
        [["cv", "template_scales"]],
        [1.0, 1.25, 1.5, 1.75, 2.0],
        _parse_float_list,
    ),
    "cv.click_delay": _setting(
        "cv.click_delay",
        ["WECHAT_CV_CLICK_DELAY"],
        [["cv", "click_delay"]],
        0.8,
        _parse_float,
    ),
    "cv.session_ready_timeout": _setting(
        "cv.session_ready_timeout",
        ["WECHAT_CV_SESSION_READY_TIMEOUT"],
        [["cv", "session_ready_timeout"]],
        6.0,
        _parse_float,
    ),
    "cv.menu_popup_timeout": _setting(
        "cv.menu_popup_timeout",
        ["WECHAT_CV_MENU_POPUP_TIMEOUT"],
        [["cv", "menu_popup_timeout"]],
        3.0,
        _parse_float,
    ),
    "cv.browser_delay": _setting(
        "cv.browser_delay",
        ["WECHAT_CV_BROWSER_DELAY"],
        [["cv", "browser_delay"]],
        3.0,
        _parse_float,
    ),
    "cv.mitm_timeout": _setting(
        "cv.mitm_timeout",
        ["WECHAT_CV_MITM_TIMEOUT"],
        [["cv", "mitm_timeout"]],
        None,
        _parse_float,
    ),
    "cv.mitm_poll_interval": _setting(
        "cv.mitm_poll_interval",
        ["WECHAT_CV_MITM_POLL_INTERVAL"],
        [["cv", "mitm_poll_interval"]],
        0.4,
        _parse_float,
    ),
    "cv.close_timeout": _setting(
        "cv.close_timeout",
        ["WECHAT_CV_CLOSE_TIMEOUT"],
        [["cv", "close_timeout"]],
        6.0,
        _parse_float,
    ),
    "cv.close_poll_interval": _setting(
        "cv.close_poll_interval",
        ["WECHAT_CV_CLOSE_POLL_INTERVAL"],
        [["cv", "close_poll_interval"]],
        0.4,
        _parse_float,
    ),
    "cv.proxy_host": _setting(
        "cv.proxy_host",
        ["WECHAT_CV_PROXY_HOST"],
        [["cv", "proxy_host"]],
        "127.0.0.1",
        _parse_text,
    ),
    "cv.proxy_port": _setting(
        "cv.proxy_port",
        ["WECHAT_CV_PROXY_PORT"],
        [["cv", "proxy_port"]],
        8080,
        _parse_int,
    ),
    "cv.proxy_connect_timeout": _setting(
        "cv.proxy_connect_timeout",
        ["WECHAT_CV_PROXY_CONNECT_TIMEOUT"],
        [["cv", "proxy_connect_timeout"]],
        2.0,
        _parse_float,
    ),
    "cv.auto_switch_system_proxy": _setting(
        "cv.auto_switch_system_proxy",
        ["WECHAT_CV_AUTO_SWITCH_SYSTEM_PROXY"],
        [["cv", "auto_switch_system_proxy"]],
        True,
        _parse_bool,
    ),
    "mitm.output_path": _setting(
        "mitm.output_path",
        ["WECHAT_CV_MITM_RESULT_PATH", "WECHAT_MITM_OUTPUT_PATH"],
        [["mitmproxy", "output_path"], ["cv", "mitm_result_path"]],
        BASE_DIR / "logs" / "mitm_openid_result.txt",
        _parse_path,
    ),
    "cv.window_title": _setting(
        "cv.window_title",
        ["WECHAT_CV_WINDOW_TITLE"],
        [["cv", "window_title"]],
        "微信",
        _parse_text,
    ),
    "cv.window_classes": _setting(
        "cv.window_classes",
        ["WECHAT_CV_WINDOW_CLASSES"],
        [["cv", "window_classes"]],
        ["WeChatMainWndForPC", "Qt51514QWindowIcon"],
        _parse_string_list,
    ),
    "cv.templates.session": _setting(
        "cv.templates.session",
        ["WECHAT_CV_SESSION_TEMPLATE"],
        [["cv", "templates", "session"]],
        "session.png",
        _parse_text,
    ),
    "cv.templates.menu_button": _setting(
        "cv.templates.menu_button",
        ["WECHAT_CV_MENU_BUTTON_TEMPLATE"],
        [["cv", "templates", "menu_button"]],
        "student_button.png",
        _parse_text,
    ),
    "cv.templates.menu_item": _setting(
        "cv.templates.menu_item",
        ["WECHAT_CV_MENU_ITEM_TEMPLATE"],
        [["cv", "templates", "menu_item"]],
        "all_item.png",
        _parse_text,
    ),
    "cv.templates.close": _setting(
        "cv.templates.close",
        ["WECHAT_CV_CLOSE_TEMPLATE"],
        [["cv", "templates", "close"]],
        "close_button.png",
        _parse_text,
    ),
    "mitm.target_domain": _setting(
        "mitm.target_domain",
        ["WECHAT_MITM_TARGET_DOMAIN"],
        [["mitmproxy", "target_domain"]],
        "v18.teachermate.cn",
        _parse_text,
    ),
    "mitm.cert_path": _setting(
        "mitm.cert_path",
        ["WECHAT_MITM_CERT_PATH"],
        [["mitmproxy", "cert_path"]],
        Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.cer",
        _parse_path,
    ),
    "mitm.dump_path": _setting(
        "mitm.dump_path",
        ["WECHAT_MITM_DUMP_PATH"],
        [["mitmproxy", "dump_path"]],
        BASE_DIR / ".venv-mitm" / "Scripts" / "mitmdump.exe",
        _parse_path,
    ),
}


class RuntimeSettings:
    def __init__(
        self,
        config_path: Path = LOCAL_CONFIG_PATH,
        template_path: Path = LOCAL_CONFIG_TEMPLATE_PATH,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.template_path = Path(template_path)
        self._env = env if env is not None else os.environ
        self.local_config: Dict[str, Any] = {}
        self._cache: Dict[str, Any] = {}
        self.generated_local_config = False
        self.reload()

    def reload(self) -> None:
        self.generated_local_config = ensure_local_config_exists(self.config_path, self.template_path)
        self.local_config = load_local_config(self.config_path)
        self._cache.clear()

    def get(self, key: str, default: Any = _UNSET) -> Any:
        if key in self._cache:
            return self._cache[key]

        definition = RUNTIME_SETTING_DEFINITIONS[key]
        value = self._resolve(definition)
        if value is None and default is not _UNSET:
            value = default
        self._cache[key] = value
        return value

    def get_local_config_value(self, *keys: str, default: Any = None) -> Any:
        current: Any = self.local_config
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return default
            current = current[key]
        return current

    def tracked_env_names(self) -> set[str]:
        names = set()
        for definition in RUNTIME_SETTING_DEFINITIONS.values():
            names.update(definition.env_names)
        return names

    def resolve_value(
        self,
        *,
        env_names: Sequence[str],
        config_paths: Sequence[Sequence[str]],
        default: Any = None,
        parser: Optional[Callable[[Any], Any]] = None,
    ) -> Any:
        definition = RuntimeSettingDefinition(
            key="__dynamic__",
            env_names=tuple(env_names),
            config_paths=tuple(tuple(path) for path in config_paths),
            default=default,
            parser=parser,
        )
        return self._resolve(definition)

    def _resolve(self, definition: RuntimeSettingDefinition) -> Any:
        env_value = self._resolve_from_environment(definition)
        if env_value is not _UNSET:
            return env_value

        config_value = self._resolve_from_local_config(definition)
        if config_value is not _UNSET:
            return config_value

        return self._resolve_default(definition)

    def _resolve_from_environment(self, definition: RuntimeSettingDefinition) -> Any:
        for env_name in definition.env_names:
            if env_name not in self._env:
                continue
            raw_value = self._env.get(env_name)
            parsed = self._parse_value(raw_value, definition.parser)
            if parsed is not None:
                return parsed
        return _UNSET

    def _resolve_from_local_config(self, definition: RuntimeSettingDefinition) -> Any:
        for config_path in definition.config_paths:
            raw_value = self.get_local_config_value(*config_path, default=_UNSET)
            if raw_value is _UNSET:
                continue
            parsed = self._parse_value(raw_value, definition.parser)
            if parsed is not None:
                return parsed
        return _UNSET

    def _resolve_default(self, definition: RuntimeSettingDefinition) -> Any:
        default_value = definition.default(self) if callable(definition.default) else definition.default
        if default_value is None:
            return None
        return self._parse_value(default_value, definition.parser)

    @staticmethod
    def _parse_value(raw_value: Any, parser: Optional[Callable[[Any], Any]]) -> Any:
        if raw_value is None:
            return None
        if parser is None:
            return raw_value
        return parser(raw_value)

DEFAULT_RUNTIME_SETTINGS: Optional[RuntimeSettings] = None


def get_runtime_settings(*, reload: bool = False) -> RuntimeSettings:
    global DEFAULT_RUNTIME_SETTINGS
    if DEFAULT_RUNTIME_SETTINGS is None:
        DEFAULT_RUNTIME_SETTINGS = RuntimeSettings()
    elif reload:
        DEFAULT_RUNTIME_SETTINGS.reload()
    return DEFAULT_RUNTIME_SETTINGS
