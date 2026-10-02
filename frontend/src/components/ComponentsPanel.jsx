import { BatchStatusPill } from './StatusPill.jsx'

export function ComponentsPanel({ version, batchStatus }) {
  return (
    <div className="panel">
      <h3>组件清单（不可变快照 #{version.id} · 指纹 <code>{version.content_hash}</code>）</h3>
      <table className="grid">
        <thead>
          <tr>
            <th>#</th><th>类型</th><th>原料 / 子配方</th><th>供应批次</th><th>用量</th><th>快照标签</th>
          </tr>
        </thead>
        <tbody>
          {version.components.map((c) => (
            <tr key={c.id}>
              <td>{c.position + 1}</td>
              <td>{c.kind === 'material' ? '原料' : '子配方'}</td>
              <td>
                {c.kind === 'material'
                  ? `${c.material_code ?? ''} ${c.material_name ?? ''}`
                  : c.child_version_label}
              </td>
              <td>
                {c.batch_code
                  ? <span>{c.batch_code} <BatchStatusPill status={batchStatus(c.batch_id)} /></span>
                  : '—'}
              </td>
              <td>{c.qty}</td>
              <td>{(c.material_labels || []).join('、') || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
