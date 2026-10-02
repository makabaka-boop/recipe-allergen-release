export function ApprovalPanel({ version, reviewers, onApprove, busy }) {
  return (
    <div className="panel">
      <h3>双人放行（同一不可变快照，两名不同审核者）</h3>
      <div className="approval-summary">
        <span>有效确认：<b>{version.active_approval_count}</b> / 2</span>
        <span className="muted">不同审核者：{version.distinct_approver_count} 人</span>
      </div>
      <ol className="approval-list">
        {version.approvals.map((a) => (
          <li key={a.id} className={a.stale ? 'approval-stale' : ''}>
            <b>{a.reviewer_name}</b>
            <span className="muted"> {a.decided_on}</span>
            {a.stale
              ? <span className="tag tag-stale">已失效：{a.stale_reason}</span>
              : <span className="tag tag-ok">有效</span>}
          </li>
        ))}
        {version.approvals.length === 0 && <li className="muted">暂无确认</li>}
      </ol>
      {version.status === 'released' ? (
        <div className="alert alert-ok">
          已于 {version.released_on} 放行{version.needs_review ? '；后因批次召回标记为待复核，放行记录未改写' : ''}。
        </div>
      ) : (
        <div className="approve-row">
          <select id="approver-select">
            <option value="">选择审核者…</option>
            {reviewers.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
          <button
            disabled={busy || !version.is_current}
            onClick={() => {
              const sel = document.getElementById('approver-select')
              if (sel.value) onApprove(Number(sel.value))
            }}
          >
            确认此快照
          </button>
          {!version.is_current && <span className="tag tag-stale">该快照已不是当前版本，审批失效</span>}
        </div>
      )}
      {version.release_blockers.length > 0 && version.status !== 'released' && (
        <ul className="blockers">
          {version.release_blockers.map((b, i) => <li key={i}>{b}</li>)}
        </ul>
      )}
    </div>
  )
}
