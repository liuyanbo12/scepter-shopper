import { useShopper } from './hooks/useShopper';
import { Icon } from './components/Icon';
import { TaskPanel } from './components/TaskPanel';
import { Results } from './components/Results';
import { History } from './components/History';
import type { LocalModelStatus } from './types';
import './connection.css';

function footerModel(config?: { model?: string; model_label?: string; key_configured?: boolean; local_model?: LocalModelStatus }) {
  if (!config) return '模型待连接';
  const paid = String(config.model_label || config.model || 'DeepSeek');
  const local = config.local_model;
  if (local?.ready) return `${paid} + 本地 Qwen 3B`;
  if (local?.state === 'downloading') return `${paid} · 本地 Qwen 下载中`;
  if (local?.state === 'starting' || (local?.enabled && !local?.ready)) return `${paid} · 本地 Qwen 启动中`;
  return config.key_configured ? paid : '模型待连接';
}

function LocalModelCard({ local, busy, onEnable }: { local: LocalModelStatus; busy: string; onEnable: () => void }) {
  const downloading = local.state === 'downloading' || busy === 'local-model';
  const starting = !local.ready && (local.state === 'starting' || (!!local.enabled && !downloading && local.state !== 'missing_runtime'));
  const missing = local.state === 'missing_runtime';
  const ready = !!local.ready;
  const title = ready ? '本地 Qwen 3B 已就绪' : downloading ? '正在下载本地 Qwen 3B' : starting ? '正在启动本地 Qwen 3B' : missing ? '其他电脑使用前：先安装 Ollama' : '下载并启用本地 Qwen 3B';
  const detail = ready
    ? `商品是否符合需求由本机判断，不为此调用 DeepSeek。连接 Edge 不会重新启动模型。保存在 ${local.models_dir || '本机'}。`
    : starting
      ? '服务已开始拉起，但还不能筛选。底栏出现「本地 Qwen 3B」才表示就绪；连接 Edge 不会重新启动或下载。'
      : missing
      ? '任意电脑两步即可：先安装免费的 Ollama（不必登录账号），再回到这里下载千问 3B。约 1.9GB，保存在本机项目目录。'
      : `${local.message || '启用后，筛选在本机完成，不再为此调用 DeepSeek。'} 约 1.9GB。${local.progress ? ` 进度 ${local.progress}` : ''}`;
  return <div className="notification notification-setup" role="status">
    <div><strong>{title}</strong><p>{detail}</p></div>
    <div className="local-model-actions">
      {missing ? <a className="button button-small button-secondary" href={local.setup_url || 'https://ollama.com/download'} target="_blank" rel="noreferrer">打开 Ollama 安装页</a> : null}
      {ready ? <span className="setup-label">本机已启用</span> : starting ? <span className="setup-label">启动中</span> : <button className="button button-small button-secondary" onClick={onEnable} disabled={missing || !!busy || downloading}>{downloading ? (local.progress || '处理中…') : '下载并启用本地模型'}</button>}
    </div>
  </div>;
}

export default function App() {
  const shopper = useShopper();
  const connected = !!shopper.status && !shopper.networkError;
  const config = connected ? shopper.status?.config : undefined;
  const existingBrowser = !config?.input_mode || config.input_mode === 'existing_browser';
  return <div className="app-shell">
    <header className="app-header"><a href="/" className="brand" aria-label="京东购物 Agent 首页"><span className="brand-mark"><Icon name="cursor" size={27}/></span><span><strong>京东购物 Agent</strong><small>理解需求，真实操作。</small></span></a><button className={`button button-outline ${existingBrowser ? 'connection-button' : ''}`} onClick={() => void shopper.openBrowser()} disabled={!connected || !!shopper.busy}><Icon name="external" size={17}/>{shopper.busy === 'browser' ? existingBrowser ? '正在连接…' : '正在打开…' : existingBrowser ? '连接当前Edge中的京东' : '打开京东'}</button></header>
    <main className="main"><div className="intro"><h1>把购物清单，交给 Agent。</h1><p>描述你的需求，Agent 将理解预算、浏览京东并整理商品方案。</p></div>
      {shopper.updateAvailable ? <div className="notification notification-setup" role="status"><div><strong>发现新版本，可以更新界面</strong><p>点击后会先保存未提交的购物需求，再刷新界面。不会自动刷新或丢弃输入。</p></div><button className="button button-small button-secondary" onClick={shopper.updateInterface} disabled={!!shopper.busy}>更新界面</button></div> : null}
      {existingBrowser ? <details className="browser-connection-help" open={shopper.browserPreview?.status !== 'ready'}><summary>首次连接：在 Edge 手动允许调试</summary><ol><li>在你已经打开的 Edge 地址栏输入 <code>edge://inspect/#remote-debugging</code>。</li><li>手动勾选允许此浏览器实例接受调试；每次连接出现授权弹窗时，请在本次弹窗点击允许。</li><li>在这个 Edge 中打开京东，再点击「连接当前Edge中的京东」。点完后立刻用 Alt+Tab 切到京东那个 Edge——授权弹窗在那边，会被这个 Agent 挡住。</li></ol><p>Agent 不会修改这些设置，也不会自动另开购物窗口；沿用你当前的京东登录状态。旧授权弹窗无效，请先关掉再连。</p></details> : null}
      {existingBrowser && shopper.busy === 'browser' ? <div className="notification notification-setup" role="status"><div><strong>正在等待 Edge 连接授权（最多 3 分钟）</strong><p>请马上用 Alt+Tab 切到京东那个 Edge，在本次 Edge 弹窗点“允许”。不要留在这个 Agent 窗口里等，授权弹窗会被挡住。等待期间不要重复连接。</p></div></div> : null}
      {shopper.networkError ? <div className="notification notification-error" role="alert"><div><strong>本地服务暂未连接</strong><p>{shopper.networkError}</p></div><button className="button button-small button-secondary" onClick={() => void shopper.refresh()}><Icon name="refresh" size={15}/>重试连接</button></div> : null}
      {config && !config.key_configured ? <div className="notification notification-setup"><div><strong>还差一步：填写你的 DeepSeek API Key</strong><p>打开项目目录的 <code>.env</code>，填写 <code>DEEPSEEK_API_KEY</code>，保存后即可开始。模型和其余配置已准备好。</p></div><span className="setup-label">等待配置</span></div> : null}
      {connected && config?.local_model ? <LocalModelCard local={config.local_model} busy={shopper.busy} onEnable={() => void shopper.enableLocalModel()}/> : null}
      {shopper.actionError ? <div className="notification notification-error" role="alert"><p>{shopper.actionError}</p><button className="text-button" onClick={shopper.dismissError}>关闭</button></div> : null}
      {shopper.notice ? <p className="action-notice" role="status">{shopper.notice}</p> : null}
      {shopper.browserTabs.length ? <section className="browser-tab-selection" aria-labelledby="browser-tabs-title"><h2 id="browser-tabs-title">选择要操作的京东标签页</h2><p>只连接你点击的标签页，不会关闭其他标签页。</p><div>{shopper.browserTabs.map(tab => <button className="browser-tab-option" key={tab.id} disabled={!connected || !!shopper.busy} onClick={() => void shopper.openBrowser(tab.id)}><span><strong>{tab.title || '京东标签页'}</strong><small>{tab.url}</small></span><span>连接此标签页<Icon name="arrow" size={16}/></span></button>)}</div></section> : null}
      {connected && shopper.status?.desktop && !shopper.status.desktop.available ? <div className="notification notification-setup"><p>{shopper.status.desktop.detail || '浏览器执行环境暂不可用，请检查浏览器与本地依赖。'}</p></div> : null}
      <div className="workspace"><div className="left-column"><TaskPanel run={shopper.run} activeRun={shopper.activeRun} keyConfigured={!!config?.key_configured} connected={connected} busy={shopper.busy || (!connected ? 'offline' : '')} readOnly={shopper.readOnly} inputMode={config?.input_mode} prompt={shopper.draft} setPrompt={shopper.setDraft} onStart={shopper.start} onControl={shopper.control} onReturn={shopper.returnToActive}/><History runs={shopper.history} currentId={shopper.run?.id} onSelect={shopper.selectHistory} busy={!!shopper.busy || !connected}/></div><Results run={shopper.run} readOnly={shopper.readOnly} busy={!!shopper.busy || !connected} preview={connected ? shopper.browserPreview : null} onApprove={ids => shopper.control('approve', ids)} onReviseSearch={() => shopper.control('revise_search')}/></div>
    </main>
    <footer className="app-footer"><span><i className={`status-dot ${config?.local_model?.ready ? 'live' : config?.key_configured ? 'live' : ''}`}/>{shopper.networkError ? '本地服务未连接' : footerModel(config)}</span><span>{config?.input_mode_label || (config?.input_mode === 'native' ? '本地运行 · 原生键鼠' : existingBrowser ? '本地运行 · 连接当前 Edge' : '本地运行 · 浏览器内虚拟鼠标')}</span><span>{config?.observation_mode || '页面文字优先 · 默认不截图'}</span></footer>
  </div>;
}
