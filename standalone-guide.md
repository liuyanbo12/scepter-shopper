# 独立安装与交付说明

这是 Windows 本地源码交付包，包含已构建的助手界面。运行不需要安装 Codex、ChatGPT 或 Computer Use 插件，也不需要登录这些产品。它仍需本机 Python、Node.js、Microsoft Edge，以及你自己的 DeepSeek API Key；它不是内含所有运行环境的免安装 EXE。

## 在另一台电脑使用

1. 安装 Python 3.11 或更新版本，安装时启用 Python launcher 或加入 PATH；推荐 Python 3.11。安装 Node.js 22.12 或更新版本，推荐 Node.js 24 LTS，并保留 npm 与 PATH 选项。关闭旧终端后重新打开文件夹。
2. 使用 Windows 10/11，安装正式版 Microsoft Edge。默认连接方式要求 Edge 的 `edge://inspect/#remote-debugging` 页面支持允许当前浏览器实例接受调试连接。需要同一个 Windows 用户运行 Edge 和助手。自定义 `--user-data-dir` 或 Beta/Dev 浏览器目录暂不支持默认连接方式。
3. 把 ZIP 完整解压到自己的可写文件夹，再双击 `安装依赖.cmd`。安装过程需要访问 Python 和 npm 软件源。它会检查 Python 与 Node 版本、创建本机 `.venv`、安装锁定依赖并构建界面。不会打开购物页面或发送模型请求。
4. 编辑程序目录新建的 `.env`，只填入自己的 `DEEPSEEK_API_KEY`。其余配置已有默认值。重新安装不会覆盖现有 `.env`，不要将 Key 发给其他人。
5. 双击 `启动.vbs`。如果系统禁用 VBScript，使用 `启动.cmd`。助手窗口和本地 Node/Python 服务会在本机运行；不要单独运行 `app.py`，那样不会启动浏览器执行服务。
6. 在平常使用的 Edge 中打开并登录京东，按助手指引在 `edge://inspect/#remote-debugging` 手动允许当前实例接受调试。点击“连接当前 Edge 中的京东”，并在本次 Edge 弹窗中点击“允许”。每次连接可能需要授权，最多等待 3 分钟；旧超时弹窗不能授权下一次请求。多个京东标签时由你选择操作哪一个。

助手使用页面文字和控件结构，默认不截图。虚拟鼠标只操作授权的京东标签，不需要操作系统前台焦点。确认候选之后才能加购；付款由你自己完成。

## 虚拟环境失效或文件夹被移动

安装器会检查 `.venv` 能否运行、Python 版本、解释器归属，以及创建时保存的路径。缺失时创建；损坏或迁移时，将原 `.venv` 改名为同一项目目录内的 `.venv.backup-时间-随机编号`，然后重新创建。备份保留，不会递归删除。若 `.venv` 是符号链接或目录联接，安装器会拒绝处理；请先自行确认路径。安装失败时旧环境仍留在备份目录。

只检查、不安装，可以在此目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-dependencies.ps1 -CheckOnly
```

不要直接把本机 `.venv`、`node_modules` 或整个工作目录复制给别人。虚拟环境与本机 Python 路径相关，运行资料可能含登录状态、任务内容和本地认证信息。

## 重新生成交付包

确认源码和 `frontend/dist` 均为准备交付的版本，然后在项目目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\package-standalone.ps1
```

ZIP 生成在 `release/`，旁边包含清单与 SHA-256 校验文件。打包器仅收录明确白名单中的源码、锁文件、启动/安装脚本、文档、空 Key 的 `.env.example`、前端源码和构建产物。它会检查归档中没有实际 `.env`、`data`、浏览器资料、日志、QA、虚拟环境或 `node_modules`，也拒绝符号链接以及源码中的开发者绝对目录引用。打包本身不会安装依赖、重建前端或启动任何服务。

这是可复现安装的源码包；是否能在特定电脑完成安装，还取决于其网络、系统权限、浏览器版本和软件源可用性。项目中的单元与隔离页面测试，不代表已经在新电脑或真实京东完成加购验收。
