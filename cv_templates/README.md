# CV Templates

`cv` mode is for the newer WeChat client:

1. Click `微助教服务号` in the left session list.
2. Click the bottom `学生(S)` button.
3. Click `全部(A)` from the popup menu.
4. Wait for the built-in browser page to load.
5. Read `openid` from the mitmproxy capture result file.
6. Click the browser `关闭` button to restore the original state.

Required template files:

- `session.png`
  - Crop a stable area from the left sidebar entry for `微助教服务号`.
- `student_button.png`
  - Crop the bottom `学生(S)` button.
- `all_item.png`
  - Crop the popup menu item `全部(A)`.
- `close_button.png`
  - Crop the WeChat built-in browser close button.

Template tips:

- Use small and stable crops.
- Keep display scaling consistent between capture and runtime.
- If the defaults do not fit, override them with:
  - `WECHAT_CV_SESSION_TEMPLATE`
  - `WECHAT_CV_MENU_BUTTON_TEMPLATE`
  - `WECHAT_CV_MENU_ITEM_TEMPLATE`
  - `WECHAT_CV_CLOSE_TEMPLATE`

mitmproxy wiring:

- Run mitmproxy or mitmdump with [mitmproxy_openid_addon.py](/D:/Zaoer/OneDrive/Tools/wei-class/mitmproxy_openid_addon.py).
- Recommended: run mitmproxy in a separate virtual environment from this Flask app.
- Trust the mitmproxy root certificate first, for example with [install_mitmproxy_cert.ps1](/D:/Zaoer/OneDrive/Tools/wei-class/install_mitmproxy_cert.ps1).
- Prefer [start_mitmproxy_openid.ps1](/D:/Zaoer/OneDrive/Tools/wei-class/start_mitmproxy_openid.ps1) so existing system proxies can be chained as upstream automatically.
- `cv` mode now switches the Windows system proxy to `127.0.0.1:8080` automatically during collection and restores the previous proxy afterward.
- Set `WECHAT_CV_MITM_RESULT_PATH` to the file written by the addon if you do not use the default path.
- The default output file is `logs/mitm_openid_result.txt`.
- Automatic proxy switching is enabled by default. Set `WECHAT_CV_AUTO_SWITCH_SYSTEM_PROXY=0` if you want to keep manual control.

Example:

```powershell
$env:WECHAT_CV_MITM_RESULT_PATH = "D:\Zaoer\OneDrive\Tools\wei-class\logs\mitm_openid_result.txt"
.\install_mitmproxy_cert.ps1
.\start_mitmproxy_openid.ps1
```

Then start the app:

```powershell
$env:WECHAT_CV_MITM_RESULT_PATH = "D:\Zaoer\OneDrive\Tools\wei-class\logs\mitm_openid_result.txt"
.\.venv\Scripts\python.exe web.py --openid-method cv
```
