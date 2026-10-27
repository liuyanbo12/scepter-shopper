import { Icon } from './Icon';
import { isTerminal, STATUS_LABELS } from '../types';
import type { Control, InputMode, Run } from '../types';

const phases = [ ['planning', '理解需求'], ['searching', '搜索商品'], ['awaiting_approval', '确认方案'], ['adding', '加入购物车'], ['verifying', '核验结果'] ];
const stages: Record<string, string> = { plan: 'planning', search: 'searching', recommend: 'awaiting_approval', approval: 'awaiting_approval', cart: 'adding', done: 'verifying' };
interface Props { run: Run | null; activeRun: Run | null; keyConfigured: boolean; connected: boolean; busy: string; readOnly: boolean; inputMode?: InputMode; prompt: string; setPrompt: (value: string) => void; onStart: (prompt: string) => Promise<void>; onControl: (action: Control) => Promise<void>; onReturn: () => void }
export function TaskPanel({ run, activeRun, keyConfigured, connected, busy, readOnly, inputMode, prompt, setPrompt, onStart, onControl, onReturn }: Props) {
  const active = !isTerminal(activeRun?.status);
  // Cart reads also happen before and between additions; only the final read
  // advances the rail to the final verification step.
  const phaseName = run?.status === 'completed' ? 'verifying' : run?.status === 'awaiting_choice' ? 'awaiting_approval' : run?.status === 'verifying' && !run.phase.includes('最终') ? 'adding' : phases.some(([id]) => id === run?.status) ? run?.status : stages[run?.stage || ''] || run?.phase;
  const phaseIndex = phases.findIndex(([id]) => id === phaseName);
  const canPause = run && !readOnly && ['planning', 'searching', 'adding', 'verifying', 'recovering'].includes(run.status);
  const canResume = run && !readOnly && ['paused', 'needs_user'].includes(run.status);
  return <aside className="task-panel">
    <form className="composer" onSubmit={event => { event.preventDefault(); if (prompt.trim().length >= 4 && !active && keyConfigured && connected) void onStart(prompt.trim()); }}>
      <label className="section-label" htmlFor="shopping-prompt">购物需求</label>
      <textarea id="shopping-prompt" value={prompt} onChange={event => setPrompt(event.target.value)} placeholder="比如：预算 15000 元，组装一台游戏电脑，包含显示器。" rows={5} maxLength={5000} disabled={!!busy || active} />
      <button className="button button-primary start-button" type="submit" disabled={prompt.trim().length < 4 || !keyConfigured || !connected || !!busy || active}>{busy === 'start' ? '正在创建任务…' : active ? '任务进行中' : '开始任务'}<Icon name="arrow" /></button>
      <p className="helper">{inputMode === 'native' ? '原生键鼠模式：请在京东浏览器登录，并保持窗口在前台、无遮挡。' : inputMode === 'browser_mouse' ? '首次使用先在专用京东窗口登录一次，后续沿用登录状态。Agent 可保持打开，搜索不会占用系统鼠标。' : '连接你已打开的 Edge，沿用京东登录状态。Agent 可保持打开，搜索不会占用系统鼠标；不会自动另开购物窗口。'}<br/>加购前由你确认 · 不会关闭你的其他标签页</p>
    </form>
    <section className="progress-section" aria-label="任务进度">
      <h2>执行流程</h2>
      <ol className="progress-rail">{phases.map(([id, label], index) => {
        const done = !!run && (run.status === 'completed' || (phaseIndex > index));
        const current = !!run && phaseIndex === index && !isTerminal(run.status);
        return <li key={id} className={`${done ? 'done' : ''} ${current ? 'current' : ''}`}><span className="step-number">{done ? <Icon name="check" size={15} /> : index + 1}</span><span>{label}</span>{current ? <span className="step-current">当前</span> : null}</li>;
      })}</ol>
      <div className="run-status"><span className={`status-dot ${run && !isTerminal(run.status) ? 'live' : ''} ${run?.status === 'failed' ? 'error' : ''}`} /><span>{run ? STATUS_LABELS[run.status] || run.status : '等待开始'}</span>{readOnly ? <span className="read-only">历史记录</span> : null}</div>
      {run?.phase && !isTerminal(run.status) ? <p className="run-message">{run.phase}</p> : null}
      {run && (run.summary || run.error) ? <p className={`run-message ${run.error ? 'error-text' : ''}`}>{run.error || run.summary}</p> : null}
      {run?.usage || run?.local_usage ? <p className="helper">DeepSeek 已用 {run.usage?.prompt_tokens || 0} token{run.local_usage ? ` · 本地 Qwen ${run.local_usage.prompt_tokens || 0} token（不产生费用）` : ''}</p> : null}
      {readOnly ? <button className="button button-secondary wide" onClick={onReturn}>返回当前任务</button> : run && !isTerminal(run.status) ? <div className="run-controls">
        {canPause ? <button className="button button-secondary" onClick={() => void onControl('pause')} disabled={!!busy}><Icon name="pause" size={15} />暂停</button> : null}
        {canResume ? <button className="button button-secondary" onClick={() => void onControl('resume')} disabled={!!busy}><Icon name="play" size={15} />{run.status === 'needs_user' ? '已处理，继续' : '继续'}</button> : null}
        <button className="button button-quiet" onClick={() => void onControl('stop')} disabled={!!busy}><Icon name="stop" size={14} />停止</button>
      </div> : null}
    </section>
  </aside>;
}
