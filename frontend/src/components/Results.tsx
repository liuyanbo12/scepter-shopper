import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import type { BrowserPreview, Candidate, Run } from '../types';
import { money, plain, safeLink } from '../types';
import { Icon } from './Icon';

type Tab = 'plan' | 'candidates' | 'events' | 'cart';
const tabs: { id: Tab; title: string }[] = [ { id: 'plan', title: '需求清单' }, { id: 'candidates', title: '商品候选' }, { id: 'events', title: '执行日志' }, { id: 'cart', title: '购物车结果' } ];

function Empty({ title = '从一句需求开始', description = '在左侧输入新的购物需求。已结束的任务在下方历史记录里，不会自动接着跑。' }: { title?: string; description?: string }) {
  return <div className="empty-state"><span className="empty-icon"><Icon name="list" size={38} /></span><h2>{title}</h2><p>{description}</p></div>;
}

function PlanView({ run, preview }: { run: Run | null; preview?: BrowserPreview | null }) {
  if (!run && preview) {
    const ready = preview.status === 'ready';
    const windowName = preview.input_mode === 'browser_mouse' ? '专用京东窗口' : preview.input_mode === 'native' ? '京东浏览器' : '当前 Edge 中的京东';
    return <div className="empty-state"><span className="empty-icon"><Icon name="list" size={38} /></span>
      <h2>{preview.error === 'browser_tab_selection_required' ? '请选择要连接的京东标签页' : ready ? `${windowName}已就绪` : preview.status === 'needs_user' ? '请先处理京东窗口中的提示' : `${windowName}暂不可用`}</h2>
      {preview.title ? <p>{preview.title}</p> : null}
      {preview.url ? <p>{preview.url}</p> : null}
      {preview.text ? <p>{preview.text.slice(0, 220)}</p> : null}
      <p>{preview.message || (ready ? preview.elements ? `已读到 ${preview.elements} 个页面控件，可以开始任务。` : `开始任务后，Agent 会在${windowName}里阅读页面和操作。` : '尚未确认页面可用，请检查京东窗口后重试。')}</p>
    </div>;
  }
  if (!run) return <Empty />;
  const plan = run.plan;
  if (!plan) return <Empty title={run.status === 'failed' ? '需求尚未生成' : '正在整理你的需求'} description={run.error || '需求解析完成后，将在这里展示预算和购物清单。'} />;
  const books = plan.scenario === 'books';
  return <div className="plan-content"><div className="plan-heading"><h2>{plan.summary || '已整理购物需求'}</h2><p>{run.prompt}</p></div>
    <div className="plan-facts"><div><span>总预算</span><strong>{typeof plan.budget === 'number' ? money(plan.budget) : '未指定'}</strong></div>{plan.scenario === 'computer' && typeof plan.includes_monitor === 'boolean' ? <div><span>显示器</span><strong>{plan.includes_monitor ? '包含在清单内' : '不包含'}</strong></div> : null}<div><span>{books ? '书目类别' : '购物项目'}</span><strong>{plan.components.length} 类</strong></div></div>
    <section className="model-plan" aria-labelledby="model-plan-title">
      <div className="model-plan-intro"><h3 id="model-plan-title">{books ? '书目建议（待页面核实）' : '型号建议（待页面核实）'}</h3><p>{books ? '下面是准备搜索的书名与版本，作者、出版社和版次仍需核对京东页面。售价、库存和店铺查明后会单独列入「商品候选」。' : '下面是准备搜索的具体型号与规格，尚未核实京东售价、库存和店铺。查到的商品会单独列入「商品候选」。'}</p></div>
      <div className="requirements-table"><div className="requirement-table-head"><span>{books ? '书目类别' : '购物项目'}</span><span>{books ? '书名与版本 / 搜索词' : '建议型号与搜索词'}</span><span>类别预算分配</span></div>{plan.components.map((component, index) => {
        const models = component.target_models ?? [];
        return <div className="requirement-row model-requirement-row" key={`${component.category}-${index}`}>
          <div><strong>{component.name || component.category}</strong><span>数量 {component.quantity || 1}</span><span>{plain(component.requirements)}</span></div>
          <div>{models.length ? <ol className="target-models">{models.map((target, modelIndex) => <li key={`${target.model}-${modelIndex}`}>
            <strong className="target-model-name">{books ? target.model : [target.brand, target.model].filter(Boolean).join(' ')}</strong>
            <p className="target-model-variant">{books ? '作者 / 出版社 / 版次：' : '具体规格：'}{books ? [target.brand, target.variant].filter(Boolean).join(' · ') || '尚待页面核实作者、出版社与版次' : target.variant || '尚待页面核实具体规格'}</p>
            <p className="target-model-query"><span>京东搜索词</span>{target.search_query || '尚未指定搜索词'}</p>
            {target.reason ? <p className="target-model-reason">建议理由：{target.reason}</p> : null}
          </li>)}</ol> : <div className="unspecified-model"><strong>{books ? '尚未指定具体书名与版本' : '尚未指定具体型号'}</strong>{component.search_query ? <p>{books ? '书目要求：' : '规格要求：'}{component.search_query}</p> : null}<p>{component.needs_page_model_lookup ? books ? '待浏览京东页面确认书名与版本，未查明前不会填入商品候选。' : '待浏览京东页面确认准确型号，未查明前不会填入商品候选。' : books ? '此清单尚未细化书目。请继续任务或用相同需求新建任务，进一步细化。' : '此清单尚未细化型号。请继续任务或用相同需求新建任务，进一步细化。'}</p></div>}</div>
          <div className="category-budget"><span className="mobile-budget-label">类别预算分配</span><strong>{typeof component.budget_hint === 'number' ? money(component.budget_hint) : component.budget_hint || '尚未分配'}</strong><span>不是商品报价</span></div>
        </div>;
      })}</div>
      <p className="budget-explanation">{books ? '类别预算分配只是总预算的规划份额，不是京东查价结果，也不代表这些书籍的实际售价。' : '类别预算分配只是总预算的规划份额，不是京东查价结果，也不代表这些型号的实际售价。'}</p>
    </section>
    {plan.assumptions.length ? <section className="assumptions"><h3>当前采用的假设</h3><ul>{plan.assumptions.map((item, index) => <li key={index}>{plain(item)}</li>)}</ul></section> : null}
    {plan.compatibility_notes ? <div className="inline-note">{plain(plan.compatibility_notes)}</div> : null}
  </div>;
}

function CandidateRow({ candidate, selected, selectable, onToggle }: { candidate: Candidate; selected: boolean; selectable: boolean; onToggle: () => void }) {
  const link = safeLink(candidate.url);
  return <article className={`candidate-row ${selected ? 'is-selected' : ''}`}>
    <label className="candidate-check"><input type="checkbox" checked={selected} onChange={onToggle} disabled={!selectable} aria-label={`选择 ${candidate.title}`}/></label>
    <div className="candidate-information"><div className="candidate-title">{link ? <a href={link} target="_blank" rel="noreferrer">{candidate.title}<Icon name="external" size={13}/></a> : <strong>{candidate.title}</strong>}</div>
      <div className="candidate-meta"><span>{candidate.store || '店铺未展示'}</span><span>{candidate.self_operated === true ? '京东自营' : candidate.self_operated === false ? '非自营' : '自营状态未核实'}</span><span>评分 {candidate.rating == null || candidate.rating === '' ? '未展示' : plain(candidate.rating)}</span></div>
      <p className="candidate-spec">规格：{candidate.spec || '待页面核实'}{candidate.quantity ? ` · 数量 ${candidate.quantity}` : ''}</p>
      <p className="candidate-promotion">优惠：{plain(candidate.promotions ?? candidate.promotion) || '页面未展示'}</p>
      {candidate.compatibility_notes ? <p className="compatibility-note">{plain(candidate.compatibility_notes)}</p> : null}
      {candidate.evidence || candidate.compatibility ? <details className="evidence"><summary>查看页面依据与兼容信息</summary>{candidate.evidence ? <p>{plain(candidate.evidence)}</p> : null}{candidate.compatibility ? <p>{plain(candidate.compatibility)}</p> : null}</details> : null}
    </div>
    <div className="candidate-price"><span className="verified-price-label">京东页面核实价</span><strong className={candidate.price == null ? 'unknown-price' : ''}>{money(candidate.price)}</strong>{candidate.recommended ? <span>推荐候选</span> : null}</div>
  </article>;
}

function CandidateView({ run, readOnly, busy, onApprove, onReviseSearch }: { run: Run | null; readOnly: boolean; busy: boolean; onApprove: (ids: string[]) => Promise<void>; onReviseSearch?: () => Promise<void> }) {
  const [selection, setSelection] = useState<{ runId: string; ids: string[] } | null>(null);
  const [checked, setChecked] = useState<{ signature: string; validation?: Run['validation']; error?: string } | null>(null);
  const [validationAttempt, setValidationAttempt] = useState(0);
  const [activeProposalId, setActiveProposalId] = useState<string | null>(null);
  const [remaining, setRemaining] = useState<number | null>(null);
  const requestSequence = useRef(0);
  useEffect(() => { setSelection(null); setChecked(null); setActiveProposalId(null); }, [run?.id]);
  const proposals = run?.proposals ?? [];
  const defaultProposal = proposals.find(proposal => proposal.id === run?.default_proposal_id) || proposals.find(proposal => proposal.recommended);
  const defaults = run?.selected_ids?.length ? run.selected_ids : defaultProposal?.selected_ids?.length ? defaultProposal.selected_ids : (run?.candidates ?? []).filter(candidate => candidate.selected || candidate.recommended).map(candidate => candidate.id);
  const selectedIds = selection?.runId === run?.id ? selection?.ids ?? [] : defaults.filter((id, index, values) => {
    const category = run?.candidates.find(candidate => candidate.id === id)?.category;
    return values.findIndex(value => run?.candidates.find(candidate => candidate.id === value)?.category === category) === index;
  });
  const signature = JSON.stringify([run?.id, [...selectedIds].sort()]);
  const shouldValidate = !!run && ['awaiting_choice', 'awaiting_approval'].includes(run.status) && !readOnly;
  useEffect(() => {
    if (run?.status !== 'awaiting_choice' || !run.choice_deadline) { setRemaining(null); return; }
    const update = () => setRemaining(Math.max(0, Math.ceil((new Date(run.choice_deadline as string).getTime() - Date.now()) / 1000)));
    update();
    const timer = window.setInterval(update, 250);
    return () => window.clearInterval(timer);
  }, [run?.id, run?.status, run?.choice_deadline]);
  useEffect(() => {
    if (!shouldValidate) return;
    const sequence = ++requestSequence.current;
    const [id, ids] = JSON.parse(signature) as [string, string[]];
    let cancelled = false;
    const timer = setTimeout(() => {
      void api.validate(id, ids).then(result => { if (!cancelled && sequence === requestSequence.current) setChecked({ signature, validation: result.validation }); }).catch(error => { if (!cancelled && sequence === requestSequence.current) setChecked({ signature, error: error instanceof Error ? error.message : '方案核验失败，请重试。' }); });
    }, 300);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [signature, shouldValidate, validationAttempt]);
  if (!run?.candidates.length) return <Empty title="还没有候选商品" description={run?.status === 'searching' ? '正在真实浏览京东，找到的商品会逐步出现在这里。' : 'Agent 搜索后，会在这里列出真实商品、规格和价格。'} />;
  const selected = run.candidates.filter(candidate => selectedIds.includes(candidate.id));
  const total = selected.reduce((sum, candidate) => sum + (candidate.price ?? 0) * (candidate.quantity || 1), 0);
  const unknown = selected.filter(candidate => candidate.price == null).length;
  const overBudget = typeof run.plan?.budget === 'number' && total > run.plan.budget;
  const selectable = !readOnly && ['awaiting_choice', 'awaiting_approval'].includes(run.status) && !busy;
  const validating = shouldValidate && checked?.signature !== signature;
  const validation = shouldValidate ? checked?.signature === signature ? checked.validation : undefined : run.validation;
  const validationError = shouldValidate && checked?.signature === signature ? checked.error : undefined;
  const errors = validation?.errors ?? [];
  const grouped = new Map<string, Candidate[]>();
  for (const candidate of run.candidates) grouped.set(candidate.category, [...(grouped.get(candidate.category) ?? []), candidate]);
  const toggle = (candidate: Candidate) => {
    const remaining = selectedIds.filter(id => run.candidates.find(value => value.id === id)?.category !== candidate.category);
    setSelection({ runId: run.id, ids: selectedIds.includes(candidate.id) ? remaining : [...remaining, candidate.id] });
  };
  const chooseProposal = (proposal: NonNullable<Run['proposals']>[number]) => {
    setActiveProposalId(proposal.id);
    setSelection({ runId: run.id, ids: proposal.selected_ids });
    setChecked(null);
  };
  return <div className="candidates-content"><div className="results-intro"><h2>{['awaiting_choice', 'awaiting_approval'].includes(run.status) ? '选择你的购物方案' : '已找到的商品'}</h2><p>{readOnly ? '历史任务仅供查看。' : run.status === 'awaiting_choice' && remaining != null ? `推荐方案已预选；${remaining} 秒无操作将自动采用“${defaultProposal?.label || '均衡推荐'}”。` : '每类选择一项，检查规格、数量和预算后确认加购。'}</p></div>
    {proposals.length ? <section className="proposal-grid" aria-label="购物方案">{proposals.map(proposal => {
      const proposalSelected = (activeProposalId === proposal.id) || (!activeProposalId && proposal.id === (run.default_proposal_id || defaultProposal?.id));
      const proposalErrors = proposal.validation?.errors ?? [];
      return <button type="button" key={proposal.id} className={`proposal-card ${proposalSelected ? 'is-active' : ''} ${proposal.recommended ? 'is-recommended' : ''}`} onClick={() => chooseProposal(proposal)} disabled={readOnly || busy}>
        <span className="proposal-card-top"><strong>{proposal.label}</strong>{proposal.recommended ? <em>推荐</em> : null}</span>
        <span className="proposal-total">{money(proposal.total)}</span>
        <span className="proposal-reason">{proposal.reason || '根据已核实商品生成。'}</span>
        {proposal.tradeoffs?.length ? <span className="proposal-tradeoffs">取舍：{proposal.tradeoffs.join('；')}</span> : null}
        {proposalErrors.length ? <span className="proposal-warning">{proposalErrors[0]}</span> : null}
        {proposalSelected && run.status === 'awaiting_choice' && remaining != null ? <span className="proposal-countdown">{remaining} 秒后自动采用</span> : null}
      </button>;
    })}</section> : null}
    {(run.plan?.components || []).filter(component => !grouped.has(component.category)).map(component => <section className="candidate-group is-empty" key={component.category}><div className="candidate-group-heading"><h3>{component.name || component.category}</h3><span>0 个候选</span></div><p className="empty-category">京东页面尚未核实到该类商品。{component.requirements === '可选项目' ? '此类已改为可选。' : '将更换备选后再搜索。'}</p></section>)}
    {[...grouped.entries()].map(([category, candidates]) => <section className="candidate-group" key={category}><div className="candidate-group-heading"><h3>{run.plan?.components.find(component => component.category === category)?.name || category}</h3><span>{candidates.length} 个候选</span></div>{candidates.map(candidate => <CandidateRow key={candidate.id} candidate={candidate} selected={selectedIds.includes(candidate.id)} selectable={selectable} onToggle={() => toggle(candidate)}/>)}</section>)}
    {errors.length ? <div className="validation-block error-text"><strong>方案需要调整</strong><ul>{errors.map((error, index) => <li key={index}>{plain(error)}</li>)}</ul>{!readOnly && run.status === 'awaiting_approval' && onReviseSearch ? <button className="button button-secondary" disabled={busy} onClick={() => void onReviseSearch()}>{busy ? '正在更换备选…' : '更换备选并继续搜索'}</button> : null}</div> : null}
    {validation?.warnings?.length ? <div className="validation-block"><strong>需要留意</strong><ul>{validation.warnings.map((warning, index) => <li key={index}>{plain(warning)}</li>)}</ul></div> : null}
    {validationError ? <div className="validation-block error-text">{validationError}<button className="text-button" onClick={() => { setChecked(null); setValidationAttempt(previous => previous + 1); }}>重新核验</button></div> : null}
    <div className="selection-footer"><div><span>已选 {selected.length} 项{unknown ? ` · ${unknown} 项价格未核实` : ''}</span><strong className={overBudget ? 'error-text' : ''}>{unknown ? '已知金额 ' : '合计 '}{money(total)}</strong>{overBudget ? <small className="error-text">超出预算 {money(total - (run.plan?.budget || 0))}</small> : null}</div>{!readOnly && ['awaiting_choice', 'awaiting_approval'].includes(run.status) ? <button className="button button-primary" disabled={!selected.length || busy || overBudget || errors.length > 0 || validating || !!validationError} onClick={() => void onApprove(selectedIds)}>{validating ? '正在核验方案…' : '立即采用并加入购物车'}<Icon name="arrow" size={17}/></button> : null}</div>
  </div>;
}

function EventView({ run }: { run: Run | null }) {
  if (!run?.events.length) return <Empty title="操作将在这里留下记录" description="任务开始后，你可以查看 Agent 的判断、页面操作与异常提示。"/>;
  return <div className="events-content"><div className="results-intro"><h2>执行日志</h2><p>只展示实际发生的操作与返回结果。</p></div><ol className="event-list">{run.events.map((event, index) => <li key={`${event.time}-${index}`} className={`event-${event.level}`}><time>{event.time ? new Date(event.time).toLocaleTimeString('zh-CN', { hour12: false }) : '—'}</time><span className="event-dot"/><p>{event.message}</p></li>)}</ol><div className="run-metadata"><span>执行步数 {run.step_count || 0}</span>{run.usage ? <span>模型用量 {(run.usage.prompt_tokens || 0) + (run.usage.completion_tokens || 0)} tokens</span> : null}</div></div>;
}

function CartView({ run }: { run: Run | null }) {
  if (!run?.cart_items.length) return <Empty title="等待购物车核验" description="你确认方案后，Agent 会加购并记录实际核验结果。"/>;
  return <div className="cart-content"><div className="results-intro"><h2>购物车核验结果</h2><p>{run.summary || '以下结果来自本次任务的购物车检查。'}</p></div><div className="cart-list">{run.cart_items.map((item, index) => { const delta = item.candidate_id ? run.cart_deltas?.[String(item.candidate_id)] : undefined; return <div className="cart-row" key={index}><div><strong>{item.title || item.name || item.category || `商品 ${index + 1}`}</strong><p>{item.spec || '规格未提供'} · 数量 {item.quantity ?? '未核实'}</p><span>{item.verified === true ? '已核验' : item.status || '请核对页面结果'}{delta && typeof delta.before_quantity === 'number' && typeof delta.after_quantity === 'number' ? ` · 数量 ${delta.before_quantity} → ${delta.after_quantity}` : ''}</span></div><strong>{money(item.price)}</strong></div>; })}</div></div>;
}

export function Results({ run, readOnly, busy, onApprove, onReviseSearch, preview }: { run: Run | null; readOnly: boolean; busy: boolean; onApprove: (ids: string[]) => Promise<void>; onReviseSearch?: () => Promise<void>; preview?: BrowserPreview | null }) {
  const [choice, setChoice] = useState<{ runId: string | null; tab: Tab } | null>(null);
  const runId = run?.id ?? null;
  const autoTab: Tab = ['awaiting_choice', 'awaiting_approval'].includes(run?.status || '') ? 'candidates' : run?.status === 'completed' ? 'cart' : 'plan';
  const tab = choice?.runId === runId ? choice.tab : autoTab;
  return <section className="results-panel"><div className="results-bar"><div className="tabs" role="tablist" aria-label="任务信息">{tabs.filter(item => item.id !== 'cart' || !!run?.cart_items.length || ['adding', 'verifying', 'completed'].includes(run?.status || '')).map(item => <button key={item.id} role="tab" id={`tab-${item.id}`} aria-selected={tab === item.id} aria-controls="result-panel" className={tab === item.id ? 'active' : ''} onClick={() => setChoice({ runId, tab: item.id })}>{item.title}{item.id === 'candidates' && run?.candidates.length ? <span className="tab-count">{run.candidates.length}</span> : null}</button>)}</div><div className="header-budget">总预算 <strong>{typeof run?.plan?.budget === 'number' ? money(run.plan.budget) : '—'}</strong></div></div>
    <div className="result-body" id="result-panel" role="tabpanel" aria-labelledby={`tab-${tab}`}><div className="tab-view" hidden={tab !== 'plan'}><PlanView run={run} preview={preview}/></div><div className="tab-view" hidden={tab !== 'candidates'}><CandidateView run={run} readOnly={readOnly} busy={busy} onApprove={onApprove} onReviseSearch={onReviseSearch}/></div><div className="tab-view" hidden={tab !== 'events'}><EventView run={run}/></div><div className="tab-view" hidden={tab !== 'cart'}><CartView run={run}/></div></div>
    <div className="result-footnote">{run?.plan?.scenario === 'books' ? '书目建议' : '型号建议'}和预算分配属于购物规划；候选商品与售价以京东实际页面核实结果为准。</div>
  </section>;
}
