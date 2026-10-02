import { useState } from 'react'
import { PathChain } from './PathChain.jsx'

export function BatchesPanel({ batches, materials, onRecall, busy }) {
  const [reasons, setReasons] = useState({})
  const [result, setResult] = useState(null)

  const recall = async (b) => {
    const reason = reasons[b.id]?.trim()
    if (!reason) { alert('请填写召回原因'); return }
    const r = await onRecall(b.id, reason)
    setResult(r)
    setReasons((s) => ({ ...s, [b.id]: '' }))
  }

  return (
    <div className="panel">
      <h3>供应批次与召回</h3>
      {result && (
        <div className="alert alert-warn recall-result">
          <div>
            批次 <b>{result.batch_code}</b> 已召回。
            命中 {result.affected_versions.length} 个配方版本（含多级引用），已标记为待复核；
            既有放行记录保持不变，新版本须重新双人放行。
          </div>
          <div className="recall-paths">
            {result.propagation.map((pv) => (
              <div key={pv.version_id}>
                <b>配方版本 #{pv.version_id}</b>
                {pv.paths.map((p, i) => <PathChain key={i} hops={p} />)}
              </div>
            ))}
          </div>
          <button className="btn-secondary" onClick={() => setResult(null)}>关闭</button>
        </div>
      )}
      <table className="grid">
        <thead>
          <tr><th>批次号</th><th>原料</th><th>收货</th><th>保质期至</th><th>状态</th><th>召回操作</th></tr>
        </thead>
        <tbody>
          {batches.map((b) => {
            const m = materials.find((x) => x.id === b.material_id)
            return (
              <tr key={b.id} className={b.status === 'active' ? '' : 'row-disabled'}>
                <td>{b.code}</td>
                <td>{m?.code} {m?.name}</td>
                <td>{b.received_on}</td>
                <td>{b.expires_on}</td>
                <td>
                  <span className={`batch-pill batch-${b.status}`}>
                    {{ active: '生效中', recalled: `已召回${b.recalled_reason ? '：' + b.recalled_reason : ''}`, expired: '已过期' }[b.status]}
                  </span>
                </td>
                <td>
                  {b.status === 'active' ? (
                    <span className="recall-row">
                      <input
                        placeholder="召回原因"
                        value={reasons[b.id] || ''}
                        onChange={(e) => setReasons((s) => ({ ...s, [b.id]: e.target.value }))}
                      />
                      <button disabled={busy} onClick={() => recall(b)}>召回</button>
                    </span>
                  ) : '—'}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
