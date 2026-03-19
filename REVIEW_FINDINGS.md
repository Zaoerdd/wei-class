# Review Findings Summary

最后更新：2026-03-20

本文档汇总两类信息：

- 当前仍待处理的 review findings
- 之前已经发现、但后续已修复的 findings

## 当前待处理

### 1. [P2] 诊断包日志截断仍会先把整个文件读进内存

- 来源：P0 review
- 状态：Open
- 风险：
  `read_text_tail()` 对大日志文件仍可能造成不必要的内存占用或导出卡顿。
- 关键位置：
  - `web.py:220`
  - `web.py:221`
- 说明：
  目前实现先执行 `path.read_bytes()`，再做尾部截断；行为上仍不是“从文件尾部按需读取”。

### 2. [P1] `local_config.json` 里的相对路径没有锚定到项目根目录

- 来源：P1 review
- 状态：Open
- 风险：
  如果用户不是在项目根目录启动脚本，而是从其他目录通过绝对路径调用，`cv_templates_local`、`logs/mitm_openid_result.txt` 这类相对路径会解析到错误位置，导致模板找不到或 mitm 输出写错目录。
- 关键位置：
  - `runtime_config.py:78`
  - `wechat_openid_strategy.py:136`
  - `wechat_openid_strategy.py:152`
  - `mitmproxy_openid_addon.py:18`
  - `mitmproxy_openid_addon.py:20`
- 说明：
  当前 `_parse_path()` 直接返回 `Path(text).expanduser()`，没有把相对路径解析为“相对于项目根目录”或“相对于 local_config.json 所在目录”。

### 3. [P2] 自动生成 `local_config.json` 的行为发生在模块导入阶段

- 来源：P1 review
- 状态：Open
- 风险：
  只读操作也会产生写文件副作用，例如单纯 `import web`、加载 mitm 插件、或者查看 CLI 默认值时，就可能在仓库根目录生成 `local_config.json`。
- 关键位置：
  - `runtime_config.py:135`
  - `runtime_config.py:446`
  - `web.py:1517`
  - `wechat_openid_collector.py:425`
  - `mitmproxy_openid_addon.py:18`
- 说明：
  当前 `reload()` 会调用 `ensure_local_config_exists()`；而多个入口都会在 import 或参数解析阶段执行 `get_runtime_settings(reload=True)`。

### 4. [P3] `RuntimeSettings.get(..., default=...)` 会缓存调用方传入的临时 fallback

- 来源：P1 review
- 状态：Open
- 风险：
  同一个 key 的返回值会受调用顺序影响；先用带 fallback 的读取，再做普通读取，后者可能继续拿到前者临时传入的值。
- 关键位置：
  - `runtime_config.py:451`
  - `runtime_config.py:457`
  - `wechat_openid_strategy.py:144`
- 说明：
  当前 `get()` 会在 `value is None and default is not _UNSET` 时，把 `default` 写入 `_cache`。这会把“调用点级 fallback”提升成“全局缓存结果”。

## 之前发现、现在已修复

### 1. [P1] 诊断包会泄露带认证信息的代理地址

- 来源：P0 review
- 状态：Fixed
- 修复说明：
  已增加代理地址脱敏逻辑，不再导出 `user:pass@host` 这种明文凭证。

### 2. [P1] `/api/health` 和 `/api/support_bundle` 会触发运行时启动

- 来源：P0 review
- 状态：Fixed
- 修复说明：
  诊断接口现在只要求 runtime 已配置，不再因为访问诊断页就启动自动采集。

### 3. [P1] mitm 证书检查出现误报阻塞

- 来源：真实运行验证
- 状态：Fixed
- 修复说明：
  证书检测改为正确携带 PowerShell 执行策略与参数，不再把“已信任证书”误判为阻塞。

### 4. [P2] 体检页把后端字符串直接拼进 HTML，存在注入风险

- 来源：P0 review
- 状态：Mitigated
- 修复说明：
  `health.js` 已增加 `escapeHtml()`，并对体检卡片、模板状态区、错误态文案等动态内容做了转义。当前没有把它继续作为 open finding 跟踪。
- 关键位置：
  - `static/health.js:27`
  - `static/health.js:210`
  - `static/health.js:249`
  - `static/health.js:293`

## 建议的后续处理顺序

1. 先修相对路径锚定问题
2. 再修 `local_config.json` 的导入副作用
3. 再修 `RuntimeSettings.get()` 的 fallback 缓存问题
4. 最后优化诊断包日志尾读实现
