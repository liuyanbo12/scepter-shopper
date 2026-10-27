export type RunStatus = 'planning' | 'searching' | 'awaiting_choice' | 'awaiting_approval' | 'auto_approving' | 'recovering' | 'adding' | 'verifying' | 'paused' | 'needs_user' | 'completed' | 'failed' | 'stopped';
export type Control = 'pause' | 'resume' | 'stop' | 'approve' | 'revise_search';
export type PlanScenario = 'computer' | 'books' | 'general';
export type InputMode = 'existing_browser' | 'browser_mouse' | 'native';
export interface BrowserTab { id: string; title: string; url: string }
export interface BrowserPreview { title?: string; url?: string; text?: string; status: 'ready' | 'needs_user' | 'error' | 'unknown'; message?: string; error?: string; elements?: number; input_mode?: InputMode }
export interface TargetModel { brand: string; model: string; variant: string; search_query: string; reason: string }
export interface PlanCategory { id: string; name: string; query: string; budget_share?: number | null; budget_allocation?: number | null; required?: boolean; target_models?: TargetModel[]; needs_page_model_lookup?: boolean }
export interface ComponentRequirement { category: string; name?: string; quantity: number; requirements: string | string[]; search_query: string; budget_hint?: number | string | null; target_models?: TargetModel[]; needs_page_model_lookup?: boolean }
export interface Plan { version?: number; summary: string; budget: number | null; includes_monitor?: boolean; assumptions: string[]; components: ComponentRequirement[]; categories?: PlanCategory[]; scenario?: PlanScenario; purpose?: string; needs_monitor?: boolean; compatibility_notes?: string | string[]; [key: string]: unknown }
export interface Candidate {
  id: string; category: string; title: string; url: string; sku?: string; spec?: string; price: number | null;
  quantity: number; store?: string; self_operated?: boolean | null; rating?: string | number | null;
  promotion?: string; promotions?: string | string[]; compatibility_notes?: string | string[]; compatibility?: Record<string, unknown>; evidence?: string | Record<string, unknown>; selected?: boolean; recommended?: boolean;
}
export interface RunEvent { time: string; level: string; message: string }
export interface CartItem { title?: string; name?: string; category?: string; spec?: string; quantity?: number; price?: number | null; url?: string; status?: string; verified?: boolean; [key: string]: unknown }
export interface Proposal { id: string; label: string; selected_ids: string[]; total?: number; reason?: string; tradeoffs?: string[]; recommended?: boolean; validation?: { total?: number; errors?: string[]; warnings?: string[]; compatible?: boolean } }
export interface Run { id: string; prompt: string; status: RunStatus; phase: string; stage?: string; created_at: string; updated_at: string; plan: Plan | null; candidates: Candidate[]; events: RunEvent[]; summary?: string; error?: string; step_count: number; usage?: { prompt_tokens: number; completion_tokens: number }; local_usage?: { prompt_tokens: number; completion_tokens: number }; cart_items: CartItem[]; cart_deltas?: Record<string, { before_quantity?: number; after_quantity?: number; price?: number | null; sku?: string; title?: string }>; selected_ids?: string[]; validation?: { total?: number; errors?: string[]; warnings?: string[]; compatible?: boolean }; proposals?: Proposal[]; default_proposal_id?: string | null; choice_deadline?: string | null; choice_policy?: string; recovery?: Record<string, unknown>; browser_tabs?: BrowserTab[] }
export interface LocalModelStatus {
  state?: string; model?: string; label?: string; enabled?: boolean; ready?: boolean;
  runtime_found?: boolean; progress?: string; message?: string; setup_url?: string; models_dir?: string;
}
export interface PublicConfig { model: string; base_url: string; key_configured: boolean; observation_mode: string; input_mode?: InputMode; input_mode_label?: string; local_model?: LocalModelStatus; [key: string]: unknown }
export interface AppStatus { config: PublicConfig; desktop: { available: boolean; detail: string; connection_method?: string; [key: string]: unknown }; active_run: Run | null; runtime_build?: string; frontend_entry?: string | null }
export const STATUS_LABELS: Record<RunStatus, string> = { planning: '正在理解需求', searching: '正在搜索商品', awaiting_choice: '等待方案选择', awaiting_approval: '等待确认方案', auto_approving: '正在采用推荐方案', recovering: '正在自动恢复', adding: '正在加入购物车', verifying: '正在核验结果', paused: '任务已暂停', needs_user: '等待你处理', completed: '任务已完成', failed: '任务未完成', stopped: '任务已停止' };
export const isTerminal = (status?: RunStatus) => !status || ['completed', 'failed', 'stopped'].includes(status);
export const money = (value?: number | null) => typeof value === 'number' && Number.isFinite(value) ? `¥${value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}` : '未核实';
export const plain = (value: unknown): string => value == null ? '' : typeof value === 'string' ? value : Array.isArray(value) ? value.map(plain).join('；') : typeof value === 'object' ? Object.entries(value).map(([k, v]) => `${k}：${plain(v)}`).join('；') : String(value);
export const safeLink = (value?: string) => { try { const u = new URL(value || ''); return ['https:', 'http:'].includes(u.protocol) ? u.href : undefined; } catch { return undefined; } };
