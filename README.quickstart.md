# 微助教签到助手快速开始

这份文档面向第一次在本机部署项目的用户。

如果你只关心“怎么尽快跑起来”，先看这里；如果遇到问题，再看 [README.troubleshooting.md](README.troubleshooting.md)；如果需要完整说明，再回到 [README.md](README.md)。

## 1. 部署前先确认

- 系统是 Windows 10 / 11
- 已安装 Python 3.9 或更高版本
- 已登录微信桌面版
- 当前网络能正常打开微助教网页

## 2. 先安装主项目依赖

在项目目录下运行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

第一次启动主服务、命令行采集器或 `mitmproxy` 插件时，如果根目录还没有 `local_config.json`，程序会自动按 [local_config.example.json](local_config.example.json) 生成一份可编辑的本机配置模板。

如果你需要二维码推送，可以稍后再配置 `PushPlus`；不配置也不影响本地运行。

## 3. 先判断你应该用哪种微信模式

- 旧版微信：优先用 `uiautomation`
- 新版微信：优先用 `cv`

如果你不确定，通常可以这样判断：

- 旧版微信更适合 UIAutomation 直接找控件
- 新版微信更推荐 `cv + mitmproxy`

## 4. 旧版微信最短启动路径

适用条件：

- 微信窗口能正常显示
- 左侧聊天列表里能看到 `微助教服务号`
- 最好把 `微助教服务号` 置顶到靠前位置

启动服务：

```powershell
.\.venv\Scripts\python.exe web.py --openid-method uiautomation
```

或者：

```powershell
powershell -ExecutionPolicy Bypass -File .\start_web_app.ps1 -OpenIdMethod uiautomation
```

启动后优先打开：

- [http://127.0.0.1:5000/](http://127.0.0.1:5000/)
- [http://127.0.0.1:5000/health](http://127.0.0.1:5000/health)

如果首页显示“现在可以直接用”，或者体检页里微信窗口和运行状态都通过，说明旧版链路基本正常。

## 5. 新版微信最短启动路径

新版微信需要额外准备 `mitmproxy` 环境。

### 5.1 创建独立的 mitmproxy 环境

```powershell
python -m venv .venv-mitm
.\.venv-mitm\Scripts\pip.exe install mitmproxy
```

### 5.2 第一次运行 mitmproxy，生成证书

```powershell
.\.venv-mitm\Scripts\mitmproxy.exe
```

出现界面后可以直接关闭。

### 5.3 导入根证书

```powershell
.\install_mitmproxy_cert.ps1
```

### 5.4 准备模板图

默认模板目录：

- [cv_templates](cv_templates)

如果你的微信样式和仓库默认模板不一致，把本机版本放到：

- [cv_templates_local](cv_templates_local)

常用模板文件：

- `session.png`
- `student_button.png`
- `all_item.png`
- `close_button.png`

注意：

- 如果当前已经停留在 `微助教服务号` 聊天页，不要再手动点一次左侧会话
- 如果底部 `学生(S)` 按钮已经可见，保持当前状态即可
- 如果你不想手工裁图，后面启动完主服务后，可以直接去体检页底部使用“点击确认式模板采集向导”

### 5.5 启动 mitmproxy

```powershell
.\start_mitmproxy_openid.ps1
```

### 5.6 启动主服务

```powershell
powershell -ExecutionPolicy Bypass -File .\start_web_app.ps1 -OpenIdMethod cv
```

或者：

```powershell
.\.venv\Scripts\python.exe web.py --openid-method cv
```

启动后同样优先打开：

- [http://127.0.0.1:5000/](http://127.0.0.1:5000/)
- [http://127.0.0.1:5000/health](http://127.0.0.1:5000/health)

## 6. 怎样算启动成功

首页建议关注这几项：

- 当前是否显示“现在可以直接用”
- `openid` 是否已有来源
- 是否提示正在使用缓存回退
- `mitm` 状态是否正常

体检页建议关注这几项：

- 微信窗口
- 系统代理
- `mitmproxy` 监听
- `mitmproxy` 证书
- `cv` 模板
- `OpenID` 缓存

## 7. 推荐的第一次验证动作

### 旧版微信

1. 打开微信并确保能看到 `微助教服务号`
2. 启动主服务
3. 打开首页确认状态
4. 打开体检页确认没有关键阻塞项

### 新版微信

1. 先启动 `mitmproxy`
2. 再启动主服务
3. 让微信停留在 `微助教服务号` 聊天页
4. 如果默认模板不匹配，直接在体检页底部运行“点击确认式模板采集向导”
5. 打开体检页确认代理、证书和模板都正常
6. 回首页确认已经拿到可用 `openid`

## 8. 遇到问题时怎么做

先看 [README.troubleshooting.md](README.troubleshooting.md)。

如果需要把问题交给开发者排查：

1. 打开 [http://127.0.0.1:5000/health](http://127.0.0.1:5000/health)
2. 点击“导出诊断包”
3. 把导出的 ZIP 发给开发者

## 9. 常用入口

- 完整文档：[README.md](README.md)
- 排查文档：[README.troubleshooting.md](README.troubleshooting.md)
- 体检页：[http://127.0.0.1:5000/health](http://127.0.0.1:5000/health)
- 首页：[http://127.0.0.1:5000/](http://127.0.0.1:5000/)
