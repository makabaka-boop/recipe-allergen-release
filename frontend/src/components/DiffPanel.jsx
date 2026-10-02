export function DiffPanel({ diff, versions }) {
  const labelOf = (id) => {
    const v = versions.find((x) => x.id === id)
    return v ? `v${v.version_no}` : `#${id}`
  }
  return (
    <div className="panel">
      <h3>版本差异</h3>
      <p className="muted">
        比较 {labelOf(diff.base_version_id)} → {labelOf(diff.target_version_id)}
        ；差异与标签清单来自同一服务端版本。
      </p>
      <table className="grid">
        <thead>
          <tr><th>变化</th><th>组件</th><th>变更前</th><th>变更后</th></tr>
        </thead>
        <tbody>
          {diff.entries.map((e, i) => (
            <tr key={i} className={`diff-${e.change}`}>
              <td>{e.change === 'added' ? '新增' : e.change === 'removed' ? '移除' : '替换/修改'}</td>
              <td>{e.name}</td>
              <td>{e.before || '—'}{e.labels_before?.length > 0 && <em className="tagline">（{e.labels_before.join('、')}）</em>}</td>
              <td>{e.after || '—'}{e.labels_after?.length > 0 && <em className="tagline">（{e.labels_after.join('、')}）</em>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="diff-labels">
        {diff.labels_added.length > 0 &&
          <span className="tag tag-add">新增标签：{diff.labels_added.join('、')}</span>}
        {diff.labels_removed.length > 0 &&
          <span className="tag tag-remove">消失标签：{diff.labels_removed.join('、')}</span>}
        {diff.entries.length === 0 && <span className="muted">两个版本无差异</span>}
      </div>
    </div>
  )
}
