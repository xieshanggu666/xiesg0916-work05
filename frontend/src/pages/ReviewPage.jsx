import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../api'

function loadImage(url) {
  return new Promise((res, rej) => {
    const img = new Image(); img.onload = () => res(img); img.onerror = rej; img.src = url
  })
}

export default function ReviewPage() {
  const { id } = useParams()
  const [ann, setAnn] = useState(null)
  const [reviewer, setReviewer] = useState(localStorage.getItem('reviewer') || 'reviewer')
  const [polygon, setPolygon] = useState([])
  const [comment, setComment] = useState('')
  const [msg, setMsg] = useState('')
  const canvasRef = useRef()
  const layersRef = useRef({})

  const redraw = useCallback((a = ann, poly = polygon) => {
    const canvas = canvasRef.current
    const { img, mask } = layersRef.current
    if (!canvas || !img) return
    const ctx = canvas.getContext('2d')
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    ctx.drawImage(img, 0, 0)
    if (mask) {
      const tint = document.createElement('canvas')
      tint.width = canvas.width; tint.height = canvas.height
      const tctx = tint.getContext('2d')
      tctx.drawImage(mask, 0, 0)
      tctx.globalCompositeOperation = 'source-in'
      tctx.fillStyle = '#ef4444'; tctx.fillRect(0, 0, tint.width, tint.height)
      ctx.globalAlpha = 0.5; ctx.drawImage(tint, 0, 0); ctx.globalAlpha = 1
    }
    for (const r of a?.open_regions || []) {
      ctx.strokeStyle = '#f97316'; ctx.lineWidth = 2
      ctx.beginPath()
      r.polygon.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)))
      ctx.closePath(); ctx.stroke()
    }
    if (poly.length) {
      ctx.strokeStyle = '#a855f7'; ctx.lineWidth = 2
      ctx.beginPath()
      poly.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)))
      ctx.stroke()
      poly.forEach(([x, y]) => { ctx.fillStyle = '#a855f7'; ctx.fillRect(x - 2, y - 2, 4, 4) })
    }
  }, [ann, polygon])

  const loadAll = useCallback(async () => {
    const a = await api.getAnnotation(id)
    setAnn(a)
    const img = await loadImage(api.imageUrl(a.image_id, a.image_revision))
    const mask = await loadImage(api.maskUrl(id, a.current_version))
    layersRef.current = { img, mask }
    canvasRef.current.width = img.width
    canvasRef.current.height = img.height
    setPolygon([])
    redraw(a, [])
  }, [id, redraw])

  useEffect(() => { loadAll().catch(e => setMsg(e.message)) }, [loadAll])
  useEffect(() => { redraw() }, [polygon, redraw])

  const click = (e) => {
    if (ann?.status !== 'in_review') return
    setPolygon(p => [...p, [e.nativeEvent.offsetX, e.nativeEvent.offsetY]])
  }

  const act = async (fn, okMsg) => {
    setMsg('')
    localStorage.setItem('reviewer', reviewer)
    try { await fn(); setMsg(okMsg); await loadAll() }
    catch (e) {
      if (e.status === 409) setMsg(`冲突：${e.message}（他人已先操作，已为你刷新）`)
      else setMsg(e.message)
      loadAll()
    }
  }

  if (!ann) return <p>加载中…</p>
  const canReview = ann.status === 'in_review'

  return (
    <div>
      <h2>复核 · 标注 #{ann.id}（{ann.label}）v{ann.current_version}</h2>
      <div className="toolbar">
        <label>复核人 <input value={reviewer} onChange={e => setReviewer(e.target.value)} size={8} /></label>
        <span className="badge">{ann.status}</span>
        <button className="primary" disabled={!canReview}
          onClick={() => act(() => api.approve(id, reviewer, ann.current_version), '已通过')}>
          通过
        </button>
        <input value={comment} onChange={e => setComment(e.target.value)} placeholder="退回备注" />
        <button className="danger" disabled={!canReview || polygon.length < 3}
          onClick={() => act(
            () => api.reject(id, reviewer, ann.current_version, [{ polygon, comment }]),
            '已退回指定区域'
          )}>
          退回圈选区域（{polygon.length} 点）
        </button>
        <button onClick={() => setPolygon([])}>清除圈选</button>
        {msg && <span className={msg.startsWith('冲突') ? 'error' : 'ok'}>{msg}</span>}
      </div>
      {!canReview && <p className="region-list">当前状态不可复核（需处于「待复核」）。</p>}
      {canReview && <p className="region-list">在画布上点击圈选要退回的区域（≥3 点），然后点「退回圈选区域」。</p>}
      <div className="canvas-wrap"><canvas ref={canvasRef} onClick={click} /></div>
    </div>
  )
}
