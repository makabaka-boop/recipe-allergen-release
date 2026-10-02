export function PathChain({ hops }) {
  return (
    <div className="path-chain">
      {hops.map((h, i) => (
        <span key={i} className="hop-wrap">
          {i > 0 && <span className="arrow">›</span>}
          <span className={`hop hop-${h.kind}`} title={h.kind === 'material' ? '原料' : '子配方'}>
            <span className="hop-name">{h.name}</span>
            {h.batch_code && <span className="hop-batch">[{h.batch_code}]</span>}
            {h.qty && <span className="hop-qty"> {h.qty}</span>}
          </span>
        </span>
      ))}
    </div>
  )
}
