# 京东购物助手 · Scepter Shopper

独立运行的 Windows 购物 Agent。DeepSeek 只负责理解需求和制定购物计划，以及必要时的复杂取舍。本地 Qwen 3B 判断商品是否符合要求。搜索、翻页、提取价格/SKU、去重和加购由本机程序与 Playwright 执行。确认候选后回读购物车核对。默认连接你已经打开的 Edge，不另开购物浏览器，也不接入 Jev。运行不依赖 Codex。

## 阅读代码

| 入口 | 职责 |
|---|---|
| `frontend/src/` | 需求输入、任务进度、商品候选和用户确认界面 |
| `shopper/server.py` → `shopper/engine.py` | 本地 API 和 Agent 任务流程，建议先从这里看 |
| `shopper/llm.py`、`shopper/local_qwen.py`、`shopper/prompts.py` | DeepSeek 规划、本地 Qwen 筛选和模型指令 |
| `shopper/existing_browser.py` → `shopper/browser_bridge.py` | Python 发出连接、读取和操作命令 |
| `browser-runtime/existing_browser.cjs`、`virtual_browser.cjs` | 绑定用户 Edge 标签，执行浏览器内鼠标与键盘动作 |
| `shopper/dom_snapshot.js`、`shopper/rules.py` | 读取页面信息，校验商品证据、预算和加购条件 |
| `launcher.cjs`、`service_host.cjs` | 启动与管理本地服务 |

`tests/` 是有效的自动化回归测试。`.venv/`、`node_modules/`、`frontend/dist/` 是运行依赖和构建产物；`data/`、`logs/` 是运行数据。

## 直接使用

这台电脑的依赖和前端会随交付准备好，无须重新安装。

1. 检查本文件同目录的 `.env`。已有 `DEEPSEEK_API_KEY` 时保留原值，无须重填；首次配置时只填入自己的 Key。不要把 Key 发到聊天或提交到代码仓库。
2. 双击 `启动.vbs`（系统禁用 VBScript 时使用 `启动.cmd`）。程序启动本地服务并打开购物助手窗口。
3. 在你平常使用的 Edge 中保留已登录的京东网页。首次连接时，由你手动打开 `edge://inspect/#remote-debugging`，允许当前实例接受远程调试；随后在 Edge 的连接确认提示中授权。本机当前为 Edge 155，项目使用 Playwright 1.62.1。无需关闭 Edge 后重新以调试参数启动，也无需复制浏览器资料。
4. 回到助手点击一次连接，等待期间在 Edge 新出现的弹窗中点击“允许”。连接最多等待 3 分钟，重复点击会等待同一次请求，刷新助手不会立即取消该请求。若超时，旧弹窗已不能授权下一次请求，请关闭旧弹窗后重新连接。若已有多个京东标签，在助手中选择本次使用的标签。没有京东标签时，先由你手动打开一个。程序不会为默认模式新建购物浏览器或切换个人资料。
5. 输入需求，例如「预算15000元，组装一台玩游戏和剪视频的电脑，需要显示器，没有已有配件」，开始任务。
6. 需要省 DeepSeek 筛选费用时，在助手中点击一次「下载并启用本地模型」。需本机已安装 [Ollama](https://ollama.com/download)。Qwen 3B 的模型文件不在本仓库里，部署后由 Ollama 下载到本机 `data/ollama-models`；之后由 Agent 自动启动。
7. 在候选列表中检查每类商品、规格、价格和待核实参数，调整选择后确认加购。
8. 查看核验结果，并到真实京东购物车检查。结算和付款由你完成。

购物助手窗口可以一直保持打开，也无需关闭其他浏览器标签。默认模式不移动 Windows 鼠标，不要求 Edge 占用操作系统前台，操作范围是你选定的京东主标签和本次操作打开的详情标签。手动修改正在由 Agent 操作的购物页面前，先点「暂停」；处理完后点「继续」。Key 会在开始新任务时重新读取，修改后不必重新安装。

## 操作方式

默认配置为 `INPUT_MODE=existing_browser`。开源 [Playwright](https://playwright.dev/) 读取默认 Edge 的本机 `DevToolsActivePort` 元数据，通过 `chromium.connectOverCDP` 与 `noDefaults:true` 连接用户授权的现有 Edge 实例，绑定已有京东标签。它不启动另一个购物浏览器，不复制 Cookie，不切换个人资料，也不关闭其他标签。固定的只读 DOM 提取器读取页面文本、控件名称、输入框值、选中状态与位置；模型只能提出有限的操作命令，不能运行任意代码或脚本。默认不截图、不做 OCR，也不把截图传给模型。

独立运行时只依赖项目本地服务、DeepSeek 接口与开源 Playwright，不导入或调用 Codex、ChatGPT Computer Use 或 `@oai/sky`。开发排查中使用的外部工具不是产品运行依赖。

搜索复用一个主结果页，比较商品时保留一个任务详情页。相同关键词直接使用已有结果；看完一个版本后返回原结果位置，再比较其他 SKU。京东点击产生的新标签会由执行器归并，只有能确认属于本次动作且未被用户改址的临时标签才会关闭。连接前已经存在的标签、先前运行遗留而本次无法确认归属的标签均保留；不会为了清理页面而关闭用户的其他标签。页面验证、登录和商品证据检查仍然适用。

点击和滚动使用 Playwright 的 `page.mouse`，输入使用该页面的键盘接口。这些操作发生在真实浏览器页面内，会正常触发商品搜索、规格选择及加购交互，但不会移动 Windows 的系统鼠标，也不依赖 Windows 窗口前台状态。定位依据当前 DOM 观察；页面改变、目标被页面弹层遮挡或信息不足时会暂停，不猜坐标继续点击。连接保留 `noDefaults:true`；执行输入时，仅对选定的京东标签临时启用页面焦点模拟，完成或失败后释放。搜索输入与回车在同一短操作内完成，先核对输入框及回读值再提交。导航中的读取最多重试三次、总计八秒，不重放点击或按键。

双击入口先检查运行服务与磁盘代码的版本，再启动项目自带的 Node 浏览器服务及 Python 任务服务。两者通过带随机令牌的本机连接通信。Python 负责模型规划、证据和操作校验，Node 负责浏览器操作。请使用提供的启动入口，单独运行 `app.py` 不会启动浏览器服务。

旧模式仅在显式配置时启用：`INPUT_MODE=browser_mouse` 会打开项目专用的可见购物浏览器，使用独立的 `data/browser-profile/virtual-edge`（Chrome 为 `virtual-chrome`）并需要单独登录；它不是连接当前已登录 Edge 的替代授权步骤。

`INPUT_MODE=native` 使用 Koffi 调用 Windows 原生鼠标与 Unicode 键盘输入，并结合 UIA/DOM 读取与窗口定位。它会移动 Windows 鼠标，要求浏览器和助手权限一致、桌面可交互、目标浏览器处于前台且未被遮挡。不要在执行过程中移动鼠标或切换窗口。此模式遇失焦会暂停，用户点击继续才重新尝试聚焦。

各模式都可用界面的「暂停」「继续」「停止」。主屏幕左上角鼠标急停仅适用于 `native`，不适用于浏览器内虚拟鼠标。停止任务不会撤销已经完成的加购，请查看任务记录及购物车后再决定是否重跑。默认模式关闭或重启服务时只断开连接，不关闭你自己的 Edge 窗口或标签。

京东的部分内容可能只在图片中、被动态组件隐藏，或者位于无法读取的跨域内嵌页面。此版本遇到读不到的价格/规格/数量会明确暂停，不能保证所有京东页面都能无截图操作。

## 功能和边界

- 支持电脑配置、学习买书及一般购物需求拆解。电脑覆盖 CPU、主板、显卡、内存、SSD、电源、机箱、散热器，按需求加入显示器。
- 搜索计划区分品牌、具体型号、版本/容量等规格与搜索词；未知的准确型号标记为待页面查明。型号备选是未核实搜索建议，类别预算分配不是商品报价。
- 默认每类搜索 3 个候选，可在配置中改为 1–5。名称、规格、价格须有当前京东详情页文字证据；评分、店铺或促销未展示时明确标记未知。
- 检查总预算及已取得的插槽、内存代际、功率、尺寸参数。未知参数会列为待核实，不能当作兼容性已通过。
- 用户确认之后才能加购；核对批准的 SKU、规格、价格与数量。不自动下单、付款、清空购物车或发送客服消息。
- 加购操作留本地账本；点击后若无法确定结果，暂停核实，避免盲目重试造成重复购买。
- 登录、扫码、验证码由用户完成。网页文字只作为资料，不能直接执行其中的指令或代码。
- 任务和日志保存在本目录；界面能查看历史任务。服务重启后旧任务标为停止，不自动重放鼠标操作。

分工如下：DeepSeek 理解需求、制定计划和复杂取舍（按次 API 费用）；本地 Qwen 3B 判断商品是否符合要求、区分版本（占用本机资源，无按次费用）；本地规则提取价格和 SKU、去重、计算预算、检查数量；Playwright 操作你已打开的 Edge。搜索框和关键词已确定时，程序直接输入并点击，不再为此询问模型。Qwen 只看到精简商品记录，不能补写读不到的价格或版本。未启用本地模型时，筛选仍走 DeepSeek。不接入 Jev。

## 页面读取与用量控制

模型读取的是当前视口的 DOM 文字和控件，默认不截图。完整 DOM 路径、坐标、CSS 类及祖先关系留在执行器本地；模型只收到控件编号、名称、值、状态及必要的页面原文。重复的父级/商品分组文字只保留一次。搜索每轮只发送当前类别的完整要求，以及总预算和其他类别摘要；已核实候选不重复发送长证据。执行前仍根据本地完整观察重新检查地址、编号、控件和遮挡。

购物车优先在本地核对：固定提取器只将包含同一 SKU 和一个明确数量输入框的容器标为商品行。数量、单价与该商品行对应后可直接核验，不必请求模型。多行不一致、缺单价、缺数量或价格冲突会暂停；不使用批准时的价格填补当前购物车单价。旧布局没有可靠行信息时仍可用精简模型读取并校验。

`MAX_INPUT_TOKENS=2000000` 默认限制单任务累计输入用量：达到后，在下一次模型请求前暂停并保留结果。供应商在请求结束后才返回用量，所以最后一次请求可能使总量超过阈值；它不是精确的人民币限额。任务 JSON 的 `usage` 记录输入、输出和接口实际返回的缓存命中/未命中量，`model_usage` 记录各次调用的阶段、请求字符数和 token 数。旧记录没有缓存量时，不能按总输入量精确还原账单。

2026-10-22 对保存的真实商品页观察做离线比较，原观察 105,521 字符，精简后 16,008 字符，减少约 84.8%。这是单个页面观察的序列化字符数，不能视为完整任务 token、费用或成功率的同等降幅。页面精简采用与 [agent-browser 的 compact/scoped snapshot](https://github.com/vercel-labs/agent-browser#snapshot-options) 类似的分层思路，未添加该项目运行依赖。

DeepSeek 的 `MAX_INPUT_TOKENS` / `MAX_MODEL_CALLS` 只约束付费调用。本地 Qwen 使用 `MAX_LOCAL_CALLS` 和 `MAX_LOCAL_INPUT_CHARS` 限制次数和单次上下文。[DeepSeek 缓存](https://api-docs.deepseek.com/zh-cn/guides/kv_cache/) 默认开启，实际是否命中以响应字段为准。

## 已写好的配置

| 配置 | 默认值 | 含义 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 用户自行配置 | 沿用已有 Key；不在文档或日志展示 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 官方 API |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 官方文档截至2026-10-19对应 DeepSeek V4.1 Flash |
| `APP_PORT` | `8766` | 只监听本机的界面端口 |
| `BROWSER` | `edge` | 默认连接现有 Edge；旧的专用浏览器模式可选 `chrome` |
| `INPUT_MODE` | `existing_browser` | 连接已授权的现有 Edge 京东标签；旧 `browser_mouse` / `native` 仅显式启用 |
| `CANDIDATES_PER_CATEGORY` | `3` | 每类候选目标数量 |
| `SCREENSHOT_EVIDENCE` | `false` | 开启后仅保存本地购物车证据截图，不用于模型识别 |
| `MAX_STEPS` | `400` | 单任务操作上限 |
| `MAX_MODEL_CALLS` | `240` | 单任务模型调用上限 |
| `MAX_INPUT_TOKENS` | `2000000` | 单任务累计输入达到阈值后，暂停下一次模型请求；不是人民币限额 |
| `MAX_RUN_SECONDS` | `3600` | 单任务时限 |
| `MAX_NO_PROGRESS` | `5` | 连续无进展后暂停 |
| `QWEN_MODEL` | `qwen2.5:3b` | 本机筛选用的 Ollama 模型 |
| `QWEN_BASE_URL` | `http://127.0.0.1:11434` | 本机 Ollama 地址 |
| `MAX_LOCAL_CALLS` | `80` | 单任务本地筛选次数上限 |
| `MAX_LOCAL_INPUT_CHARS` | `8000` | 单次发给 Qwen 的最大字符数 |

模型名依据：[DeepSeek 官方 API 文档](https://api-docs.deepseek.com/)。官方后续可能更新别名对应版本。

API Key 仅供本地后端调用 DeepSeek。用户需求及相关页面文字会发送给 DeepSeek；页面中的价格、标题等必要证据会保存到本地任务记录。默认模式使用现有 Edge 标签中的登录状态，登录资料仍由你原来的 Edge 个人资料管理，程序不复制 Cookie。不要把 `data`、`.env` 或完整日志发到公开仓库。

## 文件结构

```text
.env                         本地配置和用户的 API Key（不公开）
启动.vbs                     日常双击入口
启动调试.cmd                 显示后端错误的调试入口
安装依赖.cmd                 换电脑或重建环境时使用
launcher.cjs / app.py        Node 独立启动器与 Python 本地服务入口
service_host.cjs             后端监督、退出记录及有限重启；不重放购物动作
runtime_build.cjs            启动器与 broker 的源码版本指纹
shopper/runtime_build.py     后端的同格式版本指纹
shopper/config.py            配置与公开状态
shopper/llm.py               DeepSeek API 客户端
shopper/engine.py            任务状态、选品、确认及加购核验
shopper/prompts.py           模型指令和工具定义
shopper/rules.py             商品证据、预算及兼容性规则
shopper/existing_browser.py  默认现有 Edge 标签连接与虚拟鼠标执行器
shopper/browser_mouse.py     可选专用浏览器的 Playwright 虚拟鼠标执行器
shopper/desktop.py           可选 native：已有浏览器绑定、UIA及系统输入
shopper/windows_access.py    native 输入进程权限检查
shopper/dom_reader.py        native 模式的只读 DOM 观察接口
shopper/browser_bridge.py    Python 与本地浏览器进程的有限命令接口
shopper/dom_snapshot.js      固定页面结构读取脚本
browser-runtime/bridge.cjs   有限命令、本机认证、版本状态及会话释放
browser-runtime/existing_browser.cjs  用户授权的现有 Edge 连接、标签选择与操作
browser-runtime/virtual_browser.cjs  可选专用可见浏览器与 page.mouse 操作
browser-runtime/native_input.cjs  可选 Windows 原生鼠标与键盘操作
browser-runtime/windows_access.cjs  native Node 输入进程权限检查
重启助手.vbs / restart-services.ps1  精准重启本项目服务；支持只停止模式
shopper/server.py            本机 API 与前端服务
shopper/storage.py           本地任务存储
frontend/                    React + Vite 界面及构建产物
tests/                       自动化测试（不请求真实模型、不真实购买）
qa/                          可复用专项验证脚本与验收记录
design/                      界面设计参考
data/                        运行时生成的任务及旧模式的专用浏览器资料
data/browser-profile/virtual-edge/  仅 browser_mouse 模式使用的独立登录资料
logs/                        本地服务日志
```

## 重建与开发

Windows 10/11、Python 3.11、Node.js 22.12+ 或 24。默认模式使用支持当前实例调试授权的 Edge，本机版本为 155，Playwright 锁定为 1.62.1；旧模式可使用 Edge/Chrome。双击 `安装依赖.cmd` 安装锁定的 Python 依赖、浏览器运行依赖与前端 npm 依赖并构建。使用 uv 时缓存仍在本目录。

```powershell
.\.venv\Scripts\python.exe -m pytest
cd frontend
npm run build
```

开发时前端可用 `npm run dev`；生产入口由 Python 本地服务直接提供 `frontend/dist`。不需要启动云服务器。源码或前端构建更新后，再次通过启动入口打开助手，启动器会比对后端、监督进程和浏览器服务的已加载版本；旧版本会被替换，不会仅因端口仍有响应就继续复用。自动重启保留历史，并将中断任务停止，绝不自动重放加购。

已经打开的旧助手页面需要按一次 F5 加载新版。新版提供界面更新提示，并以 `no-store` 响应避免继续缓存旧入口；刷新助手不会关闭 Edge 中的京东标签。

## 验证范围

自动化测试中的模型和商品只用于测试，正式界面不会载入这些数据。单元测试验证的是规则、状态及执行器契约，不能等同于真实京东加购成功。已通过用户现有 Edge 实际搜索、滚动、点击并读取到商品详情文字和带条件的报价，但完整自动筛选、候选保存和加购仍需实机验收。

搜索阶段现在会等待导航及商品组件加载，遇到旧网址或变化的控件时有限重新观察，再让模型重新决策；旧动作不会重放，购物车阶段不使用此恢复机制。多行标题按真实可见文字行定位。检索记忆记录已提交关键词和已访问商品，同词重复提交及同商品循环有明确上限，避免无进展地反复搜索。书籍证据按出版社/书名参数校验，电脑仍按品牌/型号校验。

`qa/验收记录.md` 保留各版本的历史结论，旧生成结果已移入上述归档目录。京东页面改版、库存变化、登录及安全验证都可能需要进一步处理。

## 故障处理

- 提示未填 Key：编辑本目录 `.env`，确认保存后再新建任务。
- 401/鉴权失败：核对 Key；余额或服务限制由 DeepSeek 返回，程序会显示原因类型。
- 尚未允许连接：在现有 Edge 中手动打开 `edge://inspect/#remote-debugging`，允许当前实例调试，并确认 Edge 随后显示的连接请求，再回助手继续。无需复制资料、关闭 Edge 或重启为调试浏览器。
- 授权后仍提示超时：Edge 的“允许”仅对应当次连接，旧版本 25 秒的等待可能在你操作前已经结束。新版等待 3 分钟，不会自动循环重连；若超时，关闭旧授权弹窗，再从助手发起一次新连接并允许新提示。`/json/version` 在这种授权模式返回 404 属正常现象，不能用它判断是否开启调试。
- 浏览器需要登录：在选定的现有京东标签中手动登录，再回界面继续；若列出多个京东标签，选择你已登录且希望操作的那个。
- 界面仍显示旧文案或书籍任务的显示器字段：在助手页面按一次 F5。新版出现更新提示时，在任务暂停或停止后刷新页面。
- 旧版本出现 Edge“现有实例正在以提升的权限运行”弹窗：原因涉及重新启动或复用 Edge 时的权限冲突；改代码后仍出现，还可能是后台进程一直使用旧代码。当前默认模式改为经用户授权连接现有 Edge，启动器也增加已加载版本校验。先关闭旧弹窗，再通过启动入口加载新版，无需关闭现有 Edge。
- 更新代码后：启动器会自动检查并替换旧项目服务，也可双击 `重启助手.vbs`。默认模式只断开现有 Edge 的连接，再精准重启本项目的 Node/Python 服务，不关闭用户 Edge 窗口或其他标签。历史任务保留并停止，不自动续购；不要在加购动作执行中主动重启。
- 仅 `native` 模式提示“受限权限环境”：从文件资源管理器启动入口，并保持助手与浏览器权限一致。默认 `existing_browser` 不依赖 Windows 原生输入权限或前台焦点。
- 控件读不到：等待页面加载或手动关闭遮挡提示后继续；无法读取的页面不会猜坐标继续点击。
- 启动失败：查看 `logs/server.log` 或运行 `启动调试.cmd`。本机端口8766需要空闲。
- 关闭界面只关闭界面窗口，本地服务仍可供下次打开使用；离开前先停止正在执行的任务。

开源依赖及许可证见 `THIRD_PARTY_NOTICES.md`。
