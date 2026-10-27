# 开源组成

本应用是本地开发的任务编排与界面，默认以 `existing_browser` 模式，通过 Playwright 1.62.1 连接用户已打开并授权的 Edge，读取选定京东标签的真实 DOM 并操作浏览器内虚拟鼠标。它不移动 Windows 系统鼠标、不另开购物浏览器、不复制 Cookie，也不切换个人资料或关闭其他标签。旧的 `browser_mouse` 专用浏览器模式及 `native` Windows 原生输入模式仅显式配置时启用。没有嵌入或分发 Codex Computer Use 插件。

| 项目 | 用途 | 许可证 | 上游 |
|---|---|---|---|
| Playwright | 默认通过 `chromium.connectOverCDP('msedge', { noDefaults: true })` 连接现有 Edge；真实 DOM 观察、`page.mouse` 点击/滚动及页面键盘输入；显式可选专用浏览器与截图 | Apache-2.0 | https://github.com/microsoft/playwright |
| pywinauto | 可选 native 模式的 Windows 窗口识别与 UIA 观察 | BSD-3-Clause | https://github.com/pywinauto/pywinauto |
| PyAutoGUI | 可选 native 模式的桌面兼容辅助、急停与截图 | BSD-3-Clause | https://github.com/asweigart/pyautogui |
| Koffi | 可选 native 模式，在 Node 进程中调用 Windows 原生鼠标和键盘 API | MIT | https://github.com/Koromix/koffi |
| FastAPI | 本地 HTTP 服务 | MIT | https://github.com/fastapi/fastapi |
| Uvicorn | 本地 ASGI 服务 | BSD-3-Clause | https://github.com/encode/uvicorn |
| OpenAI Python SDK | 调用 DeepSeek 兼容 API；并不调用 OpenAI 模型 | Apache-2.0 | https://github.com/openai/openai-python |
| React | 界面 | MIT | https://github.com/facebook/react |
| Vite | 前端构建 | MIT | https://github.com/vitejs/vite |
| Lucide | 界面图标 | ISC | https://github.com/lucide-icons/lucide |
| python-dotenv | .env 读取 | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| psutil | 可选 native 模式的浏览器进程识别 | BSD-3-Clause | https://github.com/giampaolo/psutil |
| websocket-client | 可选 native 模式的本机 CDP 观察兼容通道 | Apache-2.0 | https://github.com/websocket-client/websocket-client |
| Pillow | 可选 native 模式的本地截图保存 | HPND / PIL | https://github.com/python-pillow/Pillow |

Python 实际安装版本锁定于 `requirements.lock.txt`，前端版本锁定于 `frontend/package-lock.json`，浏览器执行器版本锁定于 `browser-runtime/package-lock.json`。各包的完整许可证、作者和版权声明保留在 `.venv/Lib/site-packages` 对应包及 `.dist-info`、两个 `node_modules` 中。上表为主要依赖说明，不替代依赖自带许可证。重新分发完整程序时应一并保留各依赖的原始许可证。

默认模式保留现有 Edge 的登录资料。首次连接需要用户在 `edge://inspect/#remote-debugging` 允许当前实例调试并确认连接；本机 Edge 为 155。多个京东标签需用户在助手中选择。关闭或重启项目服务只断开现有连接，不关闭用户 Edge。仅旧的 `browser_mouse` 模式使用项目自己的 `data/browser-profile/virtual-edge` 或 `virtual-chrome`；不推荐通过复制个人资料或关闭 Edge 后以调试参数重启来接入已有登录。

原生窗口定位、前台检查与左上角急停仅属于 `native` 模式。启动时的源码指纹校验覆盖 Python 服务、浏览器执行器及构建后的界面，用于避免源码更新后继续复用旧后台；它不包含 `.env`、登录资料或任务历史。旧助手页面需按一次 F5 加载新版；新版使用更新提示及 `no-store` 响应减少旧界面缓存。

依赖能力及单元测试不等同于真实京东购物验收。现有已登录京东标签的输入与回读仍待用户允许连接后实测，验收结果由带模式和时间的 QA 记录说明。

设计概念图由图像生成工具创作，位于 `design/`；真实应用界面由 React/CSS 绘制。
