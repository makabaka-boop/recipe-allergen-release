export function StatusPill({ status, needsReview }) {
  if (status === 'released' && !needsReview) return <span className="pill pill-released">已放行</span>
  if (status === 'released' && needsReview) return <span className="pill pill-review">已放行 · 待复核</span>
  return <span className="pill pill-pending">待双人放行</span>
}

export function BatchStatusPill({ status }) {
  const map = {
    active: ['生效中', 'batch-active'],
    recalled: ['已召回', 'batch-recalled'],
    expired: ['已过期', 'batch-expired'],
  }
  const [text, cls] = map[status] || [status, '']
  return <span className={`pill ${cls}`}>{text}</span>
}
