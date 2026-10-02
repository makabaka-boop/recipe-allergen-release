import { useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import { ComponentsPanel } from './components/ComponentsPanel.jsx'
import { LabelsPanel } from './components/LabelsPanel.jsx'
import { DiffPanel } from './components/DiffPanel.jsx'
import { ApprovalPanel } from './components/ApprovalPanel.jsx'
import { SubstitutionPanel } from './components/SubstitutionPanel.jsx'
import { BatchesPanel } from './components/BatchesPanel.jsx'
import { StatusPill } from './components/StatusPill.jsx'
const TABS = [
  ['labels', '标签与传播'],
  ['components', '组件快照'],
  ['diff', '版本差异'],
  ['approval', '双人放行'],
  ['substitution', '替代原料'],
  ['batches', '批次召回'],
]

export default function App() {
  const [meta, setMeta] = useState({ allergens: [], reviewers: [] })
  const [recipes, setRecipes] = useState([])
  const [materials, setMaterials] = useState([])
  const [batches, setBatches] = useState([])
  const [selectedRecipe, setSelectedRecipe] = useState(null)
  const [selectedVersionId, setSelectedVersionId] = useState(null)
  const [diffBaseId, setDiffBaseId] = useState(null)
  const [graph, setGraph] = useState(null)
  const [diff, setDiff] = useState(null)
  const [tab, setTab] = useState('labels')
  const [toast, setToast] = useState(null)
  const [busy, setBusy] = useState(false)

  const notify = (text, ok = false) => setToast({ text, ok })
  useEffect(() => { if (toast) { const t = setTimeout(() => setToast(null), 5000); return () => clearTimeout(t) } },
    [toast])

  const loadAll = async () => {
    const [m, rs, ms, bs] = await Promise.all([api.meta(), api.recipes(), api.materials(), api.batches()])
    setMeta(m); setRecipes(rs); setMaterials(ms); setBatches(bs)
    return rs
  }

  useEffect(() => {
    loadAll().then((rs) => {
      const sign = rs.find((r) => r.code === 'RC-SIGN') || rs[0]
      if (sign) setSelectedRecipe(sign.id)
    }).catch((e) => notify(e.message))
  }, [])

  const [recipeDetail, setRecipeDetail] = useState(null)
  useEffect(() => {
    if (selectedRecipe == null) return
    api.recipe(selectedRecipe).then((d) => {
      setRecipeDetail(d)
      const cur = d.versions.find((v) => v.is_current) || d.versions[d.versions.length - 1]
      setSelectedVersionId((prev) =>
        prev && d.versions.some((v) => v.id === prev) ? prev : cur.id)
      setDiffBaseId(d.versions.length > 1 ? d.versions[d.versions.length - 2].id : cur.id)
    }).catch((e) => notify(e.message))
  }, [selectedRecipe, recipes])

  const version = useMemo(
    () => recipeDetail?.versions.find((v) => v.id === selectedVersionId) || null,
    [recipeDetail, selectedVersionId])

  // 页面标签清单 / 差异 / 导出均以所选版本 id 重新从服务端取同一视图
  useEffect(() => {
    if (selectedVersionId == null) return
    let cancelled = false
    api.graph(selectedVersionId).then((g) => { if (!cancelled) setGraph(g) })
    return () => { cancelled = true }
  }, [selectedVersionId, recipeDetail])

  useEffect(() => {
    if (selectedVersionId == null || diffBaseId == null) return
    api.diff(diffBaseId, selectedVersionId).then(setDiff).catch(() => {})
  }, [selectedVersionId, diffBaseId])

  const refresh = async (silent = false) => {
    const rs = await api.recipes()
    setRecipes(rs)
    if (selectedRecipe != null) {
      const d = await api.recipe(selectedRecipe)
      setRecipeDetail(d)
    }
    setBatches(await api.batches())
    if (selectedVersionId != null) setGraph(await api.graph(selectedVersionId))
    if (!silent) notify('已从服务端刷新最新版本', true)
  }

  const batchStatus = (batchId) => batches.find((b) => b.id === batchId)?.status || 'active'

  const run = async (fn, okMsg) => {
    setBusy(true)
    try {
      const result = await fn()
      await refresh(true)
      if (okMsg) notify(okMsg, true)
      return result
    } catch (e) {
      notify(e.message)
      return null
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="app">
      <header>
        <h1>过敏原标签复核台</h1>
        <div className="header-meta">
          <span>业务日期：{meta.today}</span>
          <button className="btn-secondary" onClick={() => refresh()}>刷新</button>
        </div>
      </header>

      <div className="toolbar">
        <label>配方：
          <select value={selectedRecipe ?? ''} onChange={(e) => setSelectedRecipe(Number(e.target.value))}>
            {recipes.map((r) => <option key={r.id} value={r.id}>{r.code} {r.name}</option>)}
          </select>
        </label>
        <label>版本：
          <select value={selectedVersionId ?? ''} onChange={(e) => setSelectedVersionId(Number(e.target.value))}>
            {recipeDetail?.versions.map((v) => (
              <option key={v.id} value={v.id}>
                v{v.version_no}（{v.status === 'released' ? '已放行' : '待放行'}{v.needs_review ? '·待复核' : ''}{v.is_current ? '·当前' : ''}）
              </option>
            ))}
          </select>
        </label>
        {version && <StatusPill status={version.status} needsReview={version.needs_review} />}
        {version?.needs_review && <span className="tag tag-stale">召回命中：需新版本重新放行</span>}
        <span className="spacer" />
        <a className="btn-link" href={api.exportUrl(selectedVersionId, 'json')} target="_blank" rel="noreferrer">导出 JSON</a>
        <a className="btn-link" href={api.exportUrl(selectedVersionId, 'csv')}>导出标签 CSV</a>
      </div>

      <nav className="tabs">
        {TABS.map(([key, label]) => (
          <button key={key} className={tab === key ? 'tab active' : 'tab'} onClick={() => setTab(key)}>
            {label}
          </button>
        ))}
      </nav>

      <main>
        {version && tab === 'components' && <ComponentsPanel version={version} batchStatus={batchStatus} />}
        {graph && tab === 'labels' && <LabelsPanel graph={graph} />}
        {diff && recipeDetail && tab === 'diff' && (
          <>
            <div className="panel">
              <label>基准版本：
                <select value={diffBaseId ?? ''} onChange={(e) => setDiffBaseId(Number(e.target.value))}>
                  {recipeDetail.versions.map((v) => <option key={v.id} value={v.id}>v{v.version_no}</option>)}
                </select>
              </label>
            </div>
            <DiffPanel diff={diff} versions={recipeDetail.versions} />
          </>
        )}
        {version && tab === 'approval' && (
          <ApprovalPanel
            version={version}
            reviewers={meta.reviewers}
            busy={busy}
            onApprove={(rid) => run(() => api.approve(version.id, rid), '确认已记录')}
          />
        )}
        {recipeDetail && version && tab === 'substitution' && (
          <SubstitutionPanel
            detail={recipeDetail}
            version={version}
            materials={materials}
            batches={batches}
            reviewers={meta.reviewers}
            busy={busy}
            onSubmit={(p) => run(async () => { await api.createProposal(p) }, '替代提案已提出')}
            onApply={(id) => run(async () => {
              const r = await api.applyProposal(id)
              setSelectedVersionId(r.resulting_version_id)
            }, '已采纳并生成新的不可变版本（需重新双人放行）')}
            onReject={(id) => run(() => api.rejectProposal(id), '提案已驳回')}
          />
        )}
        {tab === 'batches' && (
          <BatchesPanel
            batches={batches}
            materials={materials}
            busy={busy}
            onRecall={(id, reason) => run(() => api.recall(id, reason), '批次已召回，受影响版本已标待复核')}
          />
        )}
      </main>

      {toast && (
        <div className={`toast ${toast.ok ? 'toast-ok' : 'toast-err'}`}>
          {toast.text}
          <button onClick={() => setToast(null)}>×</button>
        </div>
      )}
    </div>
  )
}
