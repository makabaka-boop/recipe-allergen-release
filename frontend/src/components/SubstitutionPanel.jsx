import { useState } from 'react'

export function SubstitutionPanel({ detail, version, materials, batches, reviewers, onSubmit, onApply, onReject, busy }) {
  const [componentId, setComponentId] = useState('')
  const [materialId, setMaterialId] = useState('')
  const [batchId, setBatchId] = useState('')
  const [proposer, setProposer] = useState('')
  const [reason, setReason] = useState('')

  const materialComps = version.components.filter((c) => c.kind === 'material')
  const candidateBatches = batches
    .filter((b) => b.material_id === Number(materialId) && b.status === 'active')

  const submit = async () => {
    await onSubmit({
      recipe_id: detail.id,
      base_version_id: version.id,
      component_id: Number(componentId),
      new_material_id: Number(materialId),
      new_batch_id: Number(batchId),
      proposed_by: Number(proposer),
      reason,
    })
    setComponentId(''); setMaterialId(''); setBatchId(''); setReason('')
  }

  return (
    <div className="panel">
      <h3>替代原料提案</h3>
      {!version.is_current && (
        <div className="alert alert-warn">当前查看的不是最新版本，提案只能基于当前版本提出。</div>
      )}
      {version.is_current && (
        <div className="proposal-form">
          <select value={componentId} onChange={(e) => setComponentId(e.target.value)}>
            <option value="">选择要替换的组件…</option>
            {materialComps.map((c) => (
              <option key={c.id} value={c.id}>
                {c.material_name}（现批次 {c.batch_code}）
              </option>
            ))}
          </select>
          <select value={materialId} onChange={(e) => { setMaterialId(e.target.value); setBatchId('') }}>
            <option value="">替代原料…</option>
            {materials.map((m) => <option key={m.id} value={m.id}>{m.code} {m.name}</option>)}
          </select>
          <select value={batchId} onChange={(e) => setBatchId(e.target.value)} disabled={!materialId}>
            <option value="">生效批次…</option>
            {candidateBatches.map((b) => <option key={b.id} value={b.id}>{b.code}（保质期至 {b.expires_on}）</option>)}
          </select>
          <select value={proposer} onChange={(e) => setProposer(e.target.value)}>
            <option value="">提案人…</option>
            {reviewers.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
          <input placeholder="理由（可选）" value={reason} onChange={(e) => setReason(e.target.value)} />
          <button
            disabled={busy || !componentId || !materialId || !batchId || !proposer}
            onClick={submit}
          >
            提出替代
          </button>
        </div>
      )}

      <h4>本配方提案记录</h4>
      <table className="grid">
        <thead>
          <tr><th>#</th><th>替代为</th><th>批次</th><th>提案人</th><th>理由</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody>
          {detail.proposals.map((p) => (
            <tr key={p.id}>
              <td>{p.id}</td>
              <td>{p.new_material_name}</td>
              <td>{p.new_batch_code}</td>
              <td>{p.proposer_name}</td>
              <td>{p.reason || '—'}</td>
              <td>
                <span className={`proposal-status proposal-${p.status}`}>
                  {{ open: '待处理', applied: '已采纳', rejected: '已驳回', superseded: '已失效（并发）' }[p.status]}
                </span>
                {p.resulting_version_id && <span className="muted"> → v? #{p.resulting_version_id}</span>}
              </td>
              <td>
                {p.status === 'open' && version.is_current && p.base_version_id === version.id && (
                  <>
                    <button disabled={busy} onClick={() => onApply(p.id)}>采纳（生成新版本）</button>
                    <button className="btn-secondary" disabled={busy} onClick={() => onReject(p.id)}>驳回</button>
                  </>
                )}
              </td>
            </tr>
          ))}
          {detail.proposals.length === 0 && <tr><td colSpan="7" className="muted">暂无提案</td></tr>}
        </tbody>
      </table>
    </div>
  )
}
