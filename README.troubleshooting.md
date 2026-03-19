# 微助教签到助手排查指南

这份文档面向已经完成基本部署、但运行过程中遇到问题的用户。

如果你还没开始部署，建议先看 [README.quickstart.md](README.quickstart.md)。如果你需要完整配置、接口和日志说明，再看 [README.md](README.md)。

## 1. 排查时先做这三步

1. 打开 [http://127.0.0.1:5000/health](http://127.0.0.1:5000/health)
2. 记录当前 `collector_method`、`current_source`、最近错误和阻塞项
3. 如果准备把问题交给开发者，直接点击“导出诊断包”

体检页优先看这些检查项：

- 自动化运行状态
- 微信窗口
- 系统代理
- `mitmproxy` 监听
- `mitmproxy` 证书
- `cv` 模板
- `OpenID` 缓存

## 2. 服务本身起不来，或者 `/health` 打不开

优先检查：

- 主服务是否已经启动
- 当前是否在项目目录下执行命令
- `.venv` 是否已创建并安装依赖

建议命令：

```powershell
.\.venv\Scripts\python.exe web.py --help
.\.venv\Scripts\python.exe web.py --openid-method uiautomation
```

如果连 `web.py --help` 都执行不了，先回到 [README.quickstart.md](README.quickstart.md) 重新检查 Python 和依赖安装。

## 3. 新版微信提示 `capture proxy ... is not reachable`

常见原因：

- `mitmproxy` 没有启动
- 当前监听地址不是 `127.0.0.1:8080`
- 端口被别的程序占用

建议动作：

```powershell
.\start_mitmproxy_openid.ps1
```

然后刷新体检页，确认：

- `mitmproxy` 监听已通过
- 系统代理没有卡死在不可用的本地地址

## 4. 微信页面打不开，或者打开后加载失败

优先检查：

- `mitmproxy` 根证书是否已导入
- [install_mitmproxy_cert.ps1](install_mitmproxy_cert.ps1) 是否执行成功
- `mitmproxy` 是否已经启动
- 当前网络是否本来就依赖其他系统代理

如果你原本就使用 `127.0.0.1:7890` 之类的代理，优先使用 [start_mitmproxy_openid.ps1](start_mitmproxy_openid.ps1)，不要手动乱改系统代理。

## 5. `cv` 模式找不到按钮或模板

优先检查：

- 微信是否被遮挡、最小化或切到别的页面
- 当前是否真的停留在 `微助教服务号` 聊天页
- `微助教服务号` 是否仍在左侧会话列表可见
- Windows 缩放比例是否和截图时差异过大
- 当前微信样式是否和仓库默认模板一致

处理建议：

- 如果 `微助教服务号` 已经处于选中状态，不要再手动点击一次左侧会话
- 如果底部 `学生(S)` 按钮已经可见，保持当前状态即可
- 先去体检页底部运行“点击确认式模板采集向导”，优先生成这台机器自己的 `cv_templates_local`
- 如果默认模板不匹配，把本机模板放到 [cv_templates_local](cv_templates_local)
- 如果上一次采集后残留在微信内置浏览器页，先手动退回聊天页再重试

## 6. `uiautomation` 模式失败

常见原因：

- 新版微信控件结构变化
- UIAutomation 无法稳定识别当前微信控件

处理建议：

- 如果你用的是新版微信，优先切换到 `cv`
- 如果你是旧版微信，先确认微信窗口没有最小化，且左侧会话里能看到 `微助教服务号`

## 7. 首页一直拿不到 OpenID

先看首页和体检页里的这些字段：

- `current_source`
- 最近错误
- 是否正在刷新
- `OpenID` 缓存检查项

常见情况：

- `current_source=collector`
  说明本轮刚成功采集到新的 `openid`
- `current_source=file`
  说明服务当前依赖缓存回退，能用但不是刚采到的新结果
- 没有可用 `OpenID`
  说明当前自动采集还没成功，继续看微信窗口、代理、证书、模板这些检查项

## 8. 系统代理看起来不对

体检页里如果提示“系统代理仍然指向本机 mitmproxy”或“系统代理指向本机 mitmproxy，但监听端口不可用”，通常表示：

- 上一次 `cv` 采集没有完整收尾
- `mitmproxy` 已经停了，但 Windows 代理还没恢复

处理建议：

- 先重新启动 `mitmproxy`
- 或者先手动关闭 Windows 系统代理
- 然后重新打开体检页确认状态恢复正常

## 9. PushPlus 没收到推送

优先检查：

- `PUSHPLUS_TOKEN` 是否正确
- `local_config.json` 是否格式正确
- 网络是否允许访问 PushPlus

注意：

- 不配置 PushPlus 不影响本地运行
- 只是不会自动推送二维码链接

## 10. PowerShell 脚本执行被阻止

可以临时这样运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start_mitmproxy_openid.ps1
```

导入证书同理：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_mitmproxy_cert.ps1
```

## 11. 什么情况下应该导出诊断包

下面这些情况，建议直接导出：

- 你已经看过体检页，但还是不知道卡在哪
- 问题能复现，但日志太多，不想手工截图和复制
- 需要把问题交给开发者排查

导出路径：

- 页面入口：[http://127.0.0.1:5000/health](http://127.0.0.1:5000/health)
- 接口入口：[http://127.0.0.1:5000/api/support_bundle](http://127.0.0.1:5000/api/support_bundle)

诊断包默认包含：

- 脱敏后的运行状态和健康检查 JSON
- 系统代理、`mitmproxy`、证书和模板状态快照
- 脱敏后的关键日志文件和缓存文件

诊断包默认不包含：

- 本机模板图片原文件
- 虚拟环境目录
- 未脱敏的 `openid`、token 或密码

## 12. 还需要更多上下文时看哪里

- 快速开始：[README.quickstart.md](README.quickstart.md)
- 完整说明：[README.md](README.md)
