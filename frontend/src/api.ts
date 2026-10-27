import type { AppStatus, Control, Run } from './types';

async function request<T>(path: string, options: RequestInit = {}, timeoutMs = 20000): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`/api${path}`, { ...options, signal: controller.signal, headers: { 'Content-Type': 'application/json', ...options.headers } });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = data?.detail ?? data?.error;
      throw new Error(typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `服务请求失败（${response.status}）`);
    }
    if (data == null) throw new Error('服务返回了空结果，请检查后端日志。');
    return data as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw new Error(path === '/browser/open' ? '等待 Edge 连接授权超时。旧授权弹窗可能已失效；请关闭旧弹窗后重新连接，并在新弹窗中点击允许。' : '服务响应超时，请检查本地服务后重试。');
    if (error instanceof TypeError) throw new Error('无法连接本地服务，请通过启动脚本运行应用。');
    throw error;
  } finally { window.clearTimeout(timeout); }
}
export const api = {
  status: async () => { const result = await request<AppStatus>('/status'); return { ...result, active_run: result.active_run ? normalizeRun(result.active_run) : null }; },
  history: async () => { const result = await request<Run[] | { runs: Run[] }>('/history'); return (Array.isArray(result) ? result : result.runs ?? []).map(normalizeRun); },
  run: async (id: string) => normalizeRun(await request<Run>(`/runs/${encodeURIComponent(id)}`)),
  start: async (prompt: string) => normalizeRun(await request<Run>('/runs', { method: 'POST', body: JSON.stringify({ prompt }) })),
  control: async (id: string, action: Control, selection?: string[]) => normalizeRun(await request<Run>(`/runs/${encodeURIComponent(id)}/control`, { method: 'POST', body: JSON.stringify({ action, ...(selection ? { selection } : {}) }) })),
  validate: async (id: string, selection: string[]) => normalizeRun(await request<Run>(`/runs/${encodeURIComponent(id)}/validate`, { method: 'POST', body: JSON.stringify({ selection }) })),
  openBrowser: (pageId?: string) => request<Record<string, unknown>>('/browser/open', { method: 'POST', body: JSON.stringify(pageId ? { page_id: pageId } : {}) }, 210000),
  enableLocalModel: () => request<Record<string, unknown>>('/local-model/enable', { method: 'POST' }, 10000),
};

function normalizeRun(run: Run): Run {
  const plan = run.plan;
  const proposals = Array.isArray(run.proposals) ? run.proposals.map(item => ({
    ...item,
    id: String(item.id || ''),
    label: String(item.label || '购物方案'),
    selected_ids: Array.isArray(item.selected_ids) ? item.selected_ids.map(String) : [],
    tradeoffs: Array.isArray(item.tradeoffs) ? item.tradeoffs.map(String) : [],
  })).filter(item => item.id) : [];
  return { ...run, candidates: run.candidates ?? [], events: run.events ?? [], cart_items: run.cart_items ?? [], proposals, plan: plan ? {
    ...plan,
    summary: plan.summary || plan.purpose || '',
    includes_monitor: plan.includes_monitor ?? plan.needs_monitor,
    assumptions: plan.assumptions ?? [],
    components: plan.categories?.length ? plan.categories.map(category => ({
      category: category.id,
      name: category.name,
      quantity: 1,
      requirements: category.required === false ? '可选项目' : '必需项目',
      search_query: category.query,
      budget_hint: typeof category.budget_allocation === 'number' && Number.isFinite(category.budget_allocation) && category.budget_allocation >= 0 ? category.budget_allocation : typeof category.budget_share === 'number' && Number.isFinite(category.budget_share) && category.budget_share >= 0 && category.budget_share <= 1 && typeof plan.budget === 'number' ? category.budget_share * plan.budget : null,
      target_models: Array.isArray(category.target_models) ? category.target_models.filter(target => target && typeof target.model === 'string' && target.model.trim()) : [],
      needs_page_model_lookup: category.needs_page_model_lookup,
    })) : plan.components ?? [],
  } : null };
}
