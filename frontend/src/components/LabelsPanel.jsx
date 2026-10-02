import { useState } from 'react'
import { PathChain } from './PathChain.jsx'

export function LabelsPanel({ graph }) {
  const [openCode, setOpenCode] = useState(null)
  return (
    <div className="panel">
      <h3>过敏原标签清单与传播路径（服务端版本视图）</h3>
      {graph.violations.length > 0 && (
        <div className="alert alert-block">
          <b>活体检查未通过：</b>
          <ul>{graph.violations.map((v, i) => <li key={i}>{v}</li>)}</ul>
        </div>
      )}
      {graph.labels.length === 0 && <p className="muted">该版本不含任何过敏原标签。</p>}
      <div className="label-list">
        {graph.labels.map((lab) => (
          <div key={lab.code} className="label-item">
            <button className="label-head" onClick={() => setOpenCode(openCode === lab.code ? null : lab.code)}>
              <span className="label-code">{lab.code}</span>
              <span className="label-name">{lab.name}</span>
              <span className="label-count">{lab.paths.length} 条引用路径</span>
            </button>
            {openCode === lab.code && (
              <div className="label-paths">
                {lab.paths.map((p, i) => <PathChain key={i} hops={p} />)}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
