# 微助教签到助手

一个面向 Windows 的本地签到辅助工具。

它会持续监听微助教的签到任务，在检测到二维码签到链接后推送到 PushPlus，并且支持两种 `openid` 获取方式：

- `uiautomation`
  适合旧版微信。直接通过 UIAutomation 操作微信窗口并从内置浏览器读取链接。
- `cv`
  适合新版微信。通过模板图点击微信界面，打开微助教页面后使用 `mitmproxy` 抓包提取 `openid`。

当前默认运行方式是启动 `web.py`，服务启动后会立即尝试采集一次 `openid`，之后每 2 小时自动刷新一次。

## 功能概览

- 自动刷新 `openid`
  启动时优先采集新 `openid`，采集失败时才回退到 `logs/latest_openid.json`
- 自动监听签到任务
  通过 WebSocket 接收签到事件，自动更新前端状态
- 二维码链接推送
  获取到 `qr_url` 后，实时推送到 PushPlus，并附带课程名、签到名、教师等信息
- 新老微信双方案兼容
  启动时可选择 `uiautomation` 或 `cv`
- 新版微信自动切代理
  `cv` 模式会在采集时临时把 Windows 系统代理切到 `127.0.0.1:8080`，采集完成后自动恢复原设置

## 工作流程

### 旧版微信 `uiautomation`

1. 唤醒并置顶微信窗口
2. 定位左侧会话 `微助教服务号`
3. 点击底部 `学生 -> 全部`
4. 等待微信内置浏览器打开
5. 直接从浏览器页面链接中提取 `openid`
6. 点击关闭按钮，恢复到初始聊天界面

### 新版微信 `cv`

1. 唤醒并置顶微信窗口
2. 根据模板图点击 `微助教服务号`
3. 点击 `学生 -> 全部`
4. 临时把系统代理切到本机 `mitmproxy`
5. 等待微信内置浏览器访问微助教页面
6. 从 `mitmproxy` 抓到的请求/响应中提取 `openid`
7. 关闭内置浏览器
8. 恢复原系统代理

如果你的机器原本已经有系统代理，比如 `127.0.0.1:7890`，项目提供的 `start_mitmproxy_openid.ps1` 会自动把 `mitmproxy` 挂在这个代理前面，形成：

`微信 -> 127.0.0.1:8080(mitmproxy) -> 127.0.0.1:7890(原代理) -> 外网`

## 环境要求

- Windows 10 / 11
- Python 3.9 及以上，推荐 Python 3.10+
- 已登录的微信桌面版
- 能正常访问微助教网页
- 如果使用 `cv` 模式：
  - 需要安装 `mitmproxy`
  - 需要导入 `mitmproxy` 根证书
  - 需要准备模板图

## 目录说明

- [web.py](web.py)
  主服务入口，启动后监听签到并定时刷新 `openid`
- [wechat_openid_collector.py](wechat_openid_collector.py)
  独立 `openid` 采集脚本，适合调试
- [wechat_openid_strategy.py](wechat_openid_strategy.py)
  `uiautomation` / `cv` 两套采集策略的封装
- [mitmproxy_openid_addon.py](mitmproxy_openid_addon.py)
  `mitmproxy` 抓包插件，从流量中提取 `openid`
- [start_mitmproxy_openid.ps1](start_mitmproxy_openid.ps1)
  启动 `mitmproxy`，支持自动串接当前系统代理
- [install_mitmproxy_cert.ps1](install_mitmproxy_cert.ps1)
  将 `mitmproxy` 根证书导入当前用户证书库
- [cv_templates](cv_templates)
  新版微信 `cv` 模式使用的模板图目录
- [logs/latest_openid.json](logs/latest_openid.json)
  最近一次有效 `openid` 结果
- [logs/mitm_openid_result.txt](logs/mitm_openid_result.txt)
  `mitmproxy` 抓到的原始 `openid` 结果
- [logs/wechat_openid_collector.log](logs/wechat_openid_collector.log)
  `openid` 采集日志
- [logs/faye_history.log](logs/faye_history.log)
  签到监听事件日志

## 一、安装项目依赖

在项目目录下创建并使用项目虚拟环境：

```powershell
cd D:\Zaoer\OneDrive\Tools\wei-class
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

[requirements.txt](requirements.txt) 里包含主服务所需依赖：

- `Flask`
- `requests`
- `websockets`
- `uiautomation`
- `opencv-python`
- `pyautogui`

注意：

- 不要把 `mitmproxy` 安装到这个项目的 `.venv` 里
- `mitmproxy` 会引入自己的依赖版本，和当前 Flask 运行环境冲突的概率较高
- `mitmproxy` 建议单独放到 `.venv-mitm`

## 二、可选配置 PushPlus

二维码推送支持两种配置方式：

- 环境变量
- 本地配置文件 `local_config.json`

优先级：

- 环境变量优先
- 如果环境变量没配，再读取 [local_config.json](local_config.json)

示例：

```json
{
  "pushplus": {
    "token": "你的PushPlusToken",
    "topic": "可选的群组编码"
  }
}
```

等价的环境变量：

```powershell
$env:PUSHPLUS_TOKEN = "你的PushPlusToken"
$env:PUSHPLUS_TOPIC = "可选的群组编码"
```

如果没有配置 PushPlus，服务照常运行，只是不做二维码推送。

## 三、旧版微信运行方式

旧版微信推荐使用 `uiautomation`。

前提：

- 微信窗口能正常显示
- 左侧聊天列表中能看到 `微助教服务号`
- 最好把 `微助教服务号` 置顶
- 微信内置浏览器没有残留的手动页面

启动主服务：

```powershell
cd D:\Zaoer\OneDrive\Tools\wei-class
.\.venv\Scripts\python.exe web.py --openid-method uiautomation
```

调试单次采集：

```powershell
.\.venv\Scripts\python.exe wechat_openid_collector.py --method uiautomation --once
```

## 四、新版微信运行方式

新版微信推荐使用 `cv`。

### 1. 安装独立的 mitmproxy 环境

```powershell
cd D:\Zaoer\OneDrive\Tools\wei-class
python -m venv .venv-mitm
.\.venv-mitm\Scripts\pip.exe install mitmproxy
```

### 2. 生成并导入 mitmproxy 根证书

第一次使用时，先让 `mitmproxy` 生成证书：

```powershell
cd D:\Zaoer\OneDrive\Tools\wei-class
.\.venv-mitm\Scripts\mitmproxy.exe
```

出现界面后可以直接关闭。

这一步会在当前用户目录下生成证书文件：

- `%USERPROFILE%\.mitmproxy\mitmproxy-ca-cert.cer`

然后导入证书：

```powershell
cd D:\Zaoer\OneDrive\Tools\wei-class
.\install_mitmproxy_cert.ps1
```

这个脚本会把证书导入：

- `Cert:\CurrentUser\Root`

如果已经导入过，脚本会直接提示已存在。

### 3. 准备模板图

把新版微信界面的模板图放到 [cv_templates](cv_templates)：

- `session.png`
  左侧会话里的 `微助教服务号`
- `student_button.png`
  底部 `学生(S)` 按钮
- `all_item.png`
  弹出菜单中的 `全部(A)`
- `close_button.png`
  微信内置浏览器的关闭按钮

模板图要求：

- 截图尽量小，只保留稳定区域
- 运行时缩放比例尽量和截图时一致
- 微信主题、字体和分辨率不要频繁变动

模板图裁剪说明可参考 [cv_templates/README.md](cv_templates/README.md)

### 4. 启动 mitmproxy

推荐使用项目提供的脚本：

```powershell
cd D:\Zaoer\OneDrive\Tools\wei-class
.\start_mitmproxy_openid.ps1
```

这个脚本会：

- 启动 [mitmproxy_openid_addon.py](mitmproxy_openid_addon.py)
- 默认监听 `127.0.0.1:8080`
- 自动把当前已有的系统代理作为 upstream
- 把抓到的 `openid` 写入 `logs/mitm_openid_result.txt`

如果你当前系统代理是 `127.0.0.1:7890`，脚本会自动打印：

```text
Using upstream proxy: 127.0.0.1:7890
```

### 5. 启动主服务

```powershell
cd D:\Zaoer\OneDrive\Tools\wei-class
$env:WECHAT_CV_MITM_RESULT_PATH = "D:\Zaoer\OneDrive\Tools\wei-class\logs\mitm_openid_result.txt"
.\.venv\Scripts\python.exe web.py --openid-method cv
```

现在不需要手动修改 Windows 系统代理。

`cv` 采集器会在真正采集时：

1. 检查 `127.0.0.1:8080` 是否有可用的 `mitmproxy`
2. 临时切换系统代理到 `127.0.0.1:8080`
3. 点击微信界面并抓取 `openid`
4. 自动恢复原来的系统代理

如果 `mitmproxy` 没启动，采集器会直接报错，不会修改系统代理。

### 6. 调试单次采集

```powershell
cd D:\Zaoer\OneDrive\Tools\wei-class
$env:WECHAT_CV_MITM_RESULT_PATH = "D:\Zaoer\OneDrive\Tools\wei-class\logs\mitm_openid_result.txt"
.\.venv\Scripts\python.exe wechat_openid_collector.py --method cv --once
```

## 五、服务启动后的行为

启动 [web.py](web.py) 后，服务会：

1. 立即尝试采集一次新的 `openid`
2. 采集成功后验证 `openid` 是否可用
3. 根据当前 `openid` 自动建立签到监听管道
4. 每 2 小时自动刷新一次 `openid`
5. 当发现新的 `openid` 时，自动切换到新的监听管道
6. 获取到二维码签到链接后，实时推送到 PushPlus

启动时如果没有采集到新的 `openid`，会尝试回退读取：

- [logs/latest_openid.json](logs/latest_openid.json)

## 六、常用命令

启动主服务，使用旧版微信方案：

```powershell
.\.venv\Scripts\python.exe web.py --openid-method uiautomation
```

启动主服务，使用新版微信方案：

```powershell
.\start_mitmproxy_openid.ps1
.\.venv\Scripts\python.exe web.py --openid-method cv
```

查看主服务帮助：

```powershell
.\.venv\Scripts\python.exe web.py --help
```

查看采集器帮助：

```powershell
.\.venv\Scripts\python.exe wechat_openid_collector.py --help
```

## 七、可配置项

### 主服务相关

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `WECHAT_OPENID_METHOD` | `uiautomation` | 默认 `openid` 采集方式 |
| `PUSHPLUS_TOKEN` | 空 | PushPlus token |
| `PUSHPLUS_TOPIC` | 空 | PushPlus topic，可选 |

### `cv` 采集相关

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `WECHAT_CV_TEMPLATE_DIR` | `cv_templates` | 模板图目录 |
| `WECHAT_CV_MATCH_THRESHOLD` | `0.82` | 模板匹配阈值 |
| `WECHAT_CV_CLICK_DELAY` | `0.8` | 每次点击后的等待时间 |
| `WECHAT_CV_BROWSER_DELAY` | `3.0` | 打开微信浏览器后的等待时间 |
| `WECHAT_CV_MITM_TIMEOUT` | `max(15, browser_timeout)` | 等待 mitm 结果的超时时间 |
| `WECHAT_CV_MITM_POLL_INTERVAL` | `0.4` | 轮询 mitm 结果文件的间隔 |
| `WECHAT_CV_CLOSE_TIMEOUT` | `6.0` | 等待关闭按钮出现/确认关闭的超时时间 |
| `WECHAT_CV_CLOSE_POLL_INTERVAL` | `0.4` | 关闭按钮重试间隔 |
| `WECHAT_CV_PROXY_HOST` | `127.0.0.1` | 本地抓包代理地址 |
| `WECHAT_CV_PROXY_PORT` | `8080` | 本地抓包代理端口 |
| `WECHAT_CV_PROXY_CONNECT_TIMEOUT` | `2.0` | 检测 mitm 可用性的连接超时 |
| `WECHAT_CV_AUTO_SWITCH_SYSTEM_PROXY` | `1` | 是否自动切换/恢复系统代理 |
| `WECHAT_CV_MITM_RESULT_PATH` | `logs/mitm_openid_result.txt` | mitm 输出文件 |
| `WECHAT_CV_WINDOW_TITLE` | `微信` | 查找微信窗口时使用的标题关键字 |
| `WECHAT_CV_WINDOW_CLASSES` | `WeChatMainWndForPC` | 微信窗口类名列表 |
| `WECHAT_CV_SESSION_TEMPLATE` | `session.png` | 会话模板文件 |
| `WECHAT_CV_MENU_BUTTON_TEMPLATE` | `student_button.png` | 底部按钮模板文件 |
| `WECHAT_CV_MENU_ITEM_TEMPLATE` | `all_item.png` | 菜单项模板文件 |
| `WECHAT_CV_CLOSE_TEMPLATE` | `close_button.png` | 关闭按钮模板文件 |

### mitmproxy 相关

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `WECHAT_MITM_OUTPUT_PATH` | `logs/mitm_openid_result.txt` | mitm 插件输出文件 |
| `WECHAT_MITM_TARGET_DOMAIN` | `v18.teachermate.cn` | 抓包目标域名 |

## 八、前端与接口

默认启动地址：

- [http://127.0.0.1:5000/](http://127.0.0.1:5000/)

常用接口：

- [http://127.0.0.1:5000/api/check_session](http://127.0.0.1:5000/api/check_session)
  查看当前自动会话状态
- [http://127.0.0.1:5000/api/openid_status](http://127.0.0.1:5000/api/openid_status)
  查看 `openid` 刷新状态
- [http://127.0.0.1:5000/qr_code](http://127.0.0.1:5000/qr_code)
  获取当前二维码状态
- [http://127.0.0.1:5000/usage](http://127.0.0.1:5000/usage)
  使用说明页面

兼容保留接口：

- `POST /api/login`
  手动指定 `openid` 作为兜底

## 九、日志与结果文件

### `logs/latest_openid.json`

保存当前有效 `openid`，示例：

```json
{
  "openid": "88993df9f4494576c8a28609f55608d5",
  "url": null,
  "captured_at": "2026-03-19T13:38:25+0800",
  "source": "collector"
}
```

### `logs/mitm_openid_result.txt`

保存 `mitmproxy` 抓包结果，每行一个 JSON：

```json
{"captured_at":"2026-03-19T13:28:59.104450+08:00","openid":"543bb0fe287b32e7b5225170e3a46247","source":"url","url":"https://v18.teachermate.cn/wechat-pro-ssr/?openid=543bb0fe287b32e7b5225170e3a46247&from=wzj","host":"v18.teachermate.cn"}
```

### `logs/wechat_openid_collector.log`

`openid` 采集器日志，包括：

- 模板点击
- mitm 等待
- 关闭浏览器
- 自动切代理与恢复

### `logs/faye_history.log`

签到监听历史日志，包括：

- 签到任务
- 二维码事件
- Faye 相关消息

## 十、常见问题

### 1. 新版微信模式提示 `capture proxy ... is not reachable`

原因：

- `mitmproxy` 没有启动
- 监听地址不是 `127.0.0.1:8080`
- 端口被占用

解决：

```powershell
.\start_mitmproxy_openid.ps1
```

然后再启动服务。

### 2. 微信页面打不开或加载失败

优先检查：

- 是否已导入 mitm 根证书
- [install_mitmproxy_cert.ps1](install_mitmproxy_cert.ps1) 是否执行成功
- `mitmproxy` 是否已启动
- 当前网络是否原本就依赖另一个系统代理

如果你的机器本来就使用 `127.0.0.1:7890` 等代理，优先使用 [start_mitmproxy_openid.ps1](start_mitmproxy_openid.ps1)，它会自动串接 upstream。

### 3. `cv` 模式找不到按钮

检查：

- 模板图是否与当前微信界面一致
- Windows 缩放比例是否变化
- 微信是否被遮挡
- 微信是否停留在正确聊天界面
- `微助教服务号` 是否在左侧会话列表可见

### 4. `uiautomation` 模式失败

原因通常是：

- 新版微信控件结构变化
- UIAutomation 已无法稳定识别对应控件

这种情况请改用 `cv` 模式。

### 5. PushPlus 没收到推送

检查：

- `PUSHPLUS_TOKEN` 是否正确
- `local_config.json` 是否格式正确
- 网络是否允许访问 PushPlus

## 十一、PowerShell 脚本执行被阻止

如果 PowerShell 拦截脚本，可以临时这样运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start_mitmproxy_openid.ps1
```

导入证书同理：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_mitmproxy_cert.ps1
```

## 十二、建议的日常使用流程

### 旧版微信

1. 打开微信并确认 `微助教服务号` 可见
2. 运行：

```powershell
.\.venv\Scripts\python.exe web.py --openid-method uiautomation
```

### 新版微信

1. 启动 `mitmproxy`

```powershell
.\start_mitmproxy_openid.ps1
```

2. 启动主服务

```powershell
.\.venv\Scripts\python.exe web.py --openid-method cv
```

3. 保持微信已登录，且 `微助教服务号` 在左侧列表可见

---

如果你只想验证 `openid` 获取是否正常，先用：

```powershell
.\.venv\Scripts\python.exe wechat_openid_collector.py --method cv --once
```

或：

```powershell
.\.venv\Scripts\python.exe wechat_openid_collector.py --method uiautomation --once
```

确认采集正常后，再启动 [web.py](web.py) 做持续监听。
