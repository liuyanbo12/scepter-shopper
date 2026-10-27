import type { Run } from '../types';
import { STATUS_LABELS } from '../types';
import { Icon } from './Icon';

export function History({ runs, onSelect, busy, currentId }: { runs: Run[]; onSelect: (id: string) => Promise<void>; busy: boolean; currentId?: string }) {
  if (!runs.length) return null;
  return <details className="history"><summary><Icon name="clock" size={16}/><span>历史任务</span><span className="history-count">{runs.length}</span></summary><div className="history-list">{runs.map(run => <button className={`history-row ${currentId === run.id ? 'selected' : ''}`} key={run.id} onClick={() => void onSelect(run.id)} disabled={busy}><span className="history-prompt">{run.prompt}</span><span>{STATUS_LABELS[run.status] || run.status}</span><time>{new Date(run.created_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</time></button>)}</div></details>;
}
