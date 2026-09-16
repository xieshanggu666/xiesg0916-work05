import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../api'

function loadImage(url) {
  return new Promise((res, rej) => {
    const img = new Image()
    img.onload = () => res(img)
    img.onerror = rej
    img.src = url
  })
}

export default function AnnotatePage() {
  const { id } = useParams()
  const [ann, setAnn] = useState(null)
  const [baseVersion, setBaseVersion] = useState(0)
  const [author, setAuthor] = useState(localStorage.getItem('author') || 'annotator')
  const [brush, setBrush] = useState(12)
  const [mode, setMode] = useState('draw') // draw | erase
  const [conflict, setConflict] = useState(null)
  const [msg, setMsg] = useState('')
  const [loadError, setLoadError] = useState('')

  const canvasRef = useRef()
  const maskRef = useRef()   // offscreen canvas holding the editable mask
  const imgRef = useRef()    // offscreen image
  const drawing = useRef(false)
  const authorRef = useRef(author)
  authorRef.current = author

  const redraw = useCallback((conflictRegions) => {
    const canvas = canvasRef.current
    if (!canvas || !imgRef.current) return
    const ctx = canvas.getContext('2d')
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    ctx.drawImage(imgRef.current, 0, 0)
    // tint mask red at 50%
    if (maskRef.current) {
      const tint = document.createElement('canvas')
      tint.width = canvas.width; tint.height = canvas.height
      const tctx = tint.getContext('2d')
      tctx.drawImage(maskRef.current, 0, 0)
      tctx.globalCompositeOperation = 'source-in'
      tctx.fillStyle = 'rgba(239,68,68,1)'
      tctx.fillRect(0, 0, tint.width, tint.height)
      ctx.globalAlpha = 0.5
      ctx.drawImage(tint, 0, 0)
      ctx.globalAlpha = 1
    }
    // open rejection regions in orange
    for (const r of ann?.open_regions || []) {
      ctx.strokeStyle = '#f97316'; ctx.lineWidth = 2
      ctx.beginPath()
      r.polygon.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)))
      ctx.closePath(); ctx.stroke()
    }
    // conflict regions in yellow
    for (const b of conflictRegions || []) {
      ctx.strokeStyle = '#eab308'; ctx.lineWidth = 3
      ctx.strokeRect(b.x, b.y, b.w, b.h)
    }
  }, [ann])

  const loadAll = useCallback(async () => {
    const a = await api.getAnnotation(id, authorRef.current)
    setAnn(a)
    setLoadError('')
    // a double-blind side belongs to its assignee; lock the identity to it
    if (a.arbitration) setAuthor(a.assignee)
    setBaseVersion(a.current_version)
    const img = await loadImage(api.imageUrl(a.image_id, a.image_revision))
    imgRef.current = img
    const maskImg = await loadImage(api.maskUrl(id, a.current_version, a.arbitration ? a.assignee : undefined))
    const mc = document.createElement('canvas')
    mc.width = img.width; mc.height = img.height
    mc.getContext('2d').drawImage(maskImg, 0, 0)
    maskRef.current = mc
    canvasRef.current.width = img.width
    canvasRef.current.height = img.height
    setConflict(null)
    redraw()
  }, [id, redraw])

  useEffect(() => {
    loadAll().catch(e => { setLoadError(e.message); setMsg(e.message) })
  }, [loadAll])
  useEffect(() => { redraw(conflict?.conflicts?.flatMap(c => c.regions)) }, [ann, conflict, redraw])

  const paint = (e) => {
    const ctx = maskRef.current.getContext('2d')
    ctx.globalCompositeOperation = mode === 'erase' ? 'destination-out' : 'source-over'
    ctx.fillStyle = '#fff'
    ctx.beginPath()
    ctx.arc(e.nativeEvent.offsetX, e.nativeEvent.offsetY, brush, 0, Math.PI * 2)
    ctx.fill()
    redraw(conflict?.conflicts?.flatMap(c => c.regions))
  }

  const save = async (resolution) => {
    setMsg('')
    const blob = await new Promise(res => maskRef.current.toBlob(res, 'image/png'))
    localStorage.setItem('author', author)
    try {
      const r = await api.saveMask(id, blob, author, baseVersion, resolution)
      setBaseVersion(r.version)
      setConflict(null)
      setMsg(`已保存为 v${r.version}`)
      loadAll()
    } catch (e) {
      if (e.status === 409 && e.detail?.conflicts) {
        setConflict(e.detail)   // 展示冲突，绝不静默覆盖
      } else if (e.status === 409) {
        setMsg(typeof e.detail === 'string' ? e.detail : e.detail.message)
      } else setMsg(e.message)
    }
  }

  const submit = async () => {
    try {
      await api.submit(id, author, baseVersion)
      setMsg('已提交复核'); loadAll()
    } catch (e) { setMsg(e.message) }
  }

  const submitArbitration = async () => {
    try {
      await api.submitArbitrationSide(ann.arbitration.id, author)
      setMsg('已提交仲裁，结果已冻结等待对方与裁定'); loadAll()
    } catch (e) { setMsg(e.message) }
  }

  if (loadError && !ann) {
    return (
      <div>
        <h2>修边 · 标注 #{id}</h2>
        <p className="error">{loadError}</p>
        <div className="toolbar">
          <label>作者 <input value={author} onChange={e => setAuthor(e.target.value)} size={8} /></label>
          <button onClick={() => loadAll().catch(e => setLoadError(e.message))}>以该身份重试</button>
        </div>
      </div>
    )
  }
  if (!ann) return <p>加载中…</p>
  const conflictRegions = conflict?.conflicts?.flatMap(c => c.regions)
  const blind = ann.arbitration

  return (
    <div>
      <h2>
        修边 · 标注 #{ann.id}（{ann.label}）
        {blind && <span className="badge">双盲 {blind.side.toUpperCase()} 侧 · 仲裁 #{blind.id}</span>}
      </h2>
      <div className="toolbar">
        <label>作者 <input value={author} onChange={e => setAuthor(e.target.value)} size={8} disabled={!!blind} /></label>
        <label>笔刷 <input type="range" min="2" max="60" value={brush}
          onChange={e => setBrush(+e.target.value)} /> {brush}px</label>
        <button onClick={() => setMode('draw')} disabled={mode === 'draw'}>涂抹</button>
        <button onClick={() => setMode('erase')} disabled={mode === 'erase'}>擦除</button>
        <button className="primary" onClick={() => save()}>保存（基于 v{baseVersion}）</button>
        {blind ? (
          <button onClick={submitArbitration} disabled={blind.submitted}>
            {blind.submitted ? '已提交仲裁' : '提交仲裁（提交后冻结）'}
          </button>
        ) : (
          <button onClick={submit} disabled={ann.status !== 'draft' && ann.status !== 'changes_requested'}>
            提交复核
          </button>
        )}
        <span className="badge">{ann.status}</span>
        {msg && <span className={msg.startsWith('已') ? 'ok' : 'error'}>{msg}</span>}
      </div>
      {blind && (
        <p className="region-list">双盲隔离中：你看不到对方的标注，对方也看不到你的。提交后该侧冻结，等待双方提交后由仲裁者裁定。</p>
      )}
      {ann.open_regions.length > 0 && (
        <p className="region-list">复核退回 {ann.open_regions.length} 处区域（橙色框），修改覆盖这些区域后重新提交才会解除。</p>
      )}
      <div className="canvas-wrap">
        <canvas ref={canvasRef}
          onMouseDown={e => { drawing.current = true; paint(e) }}
          onMouseMove={e => drawing.current && paint(e)}
          onMouseUp={() => (drawing.current = false)}
          onMouseLeave={() => (drawing.current = false)} />
      </div>

      {conflict && (
        <div className="dialog-mask">
          <div className="dialog">
            <h3>⚠️ 检测到并发修改冲突</h3>
            <p>你基于 v{baseVersion} 编辑，但当前已推进到 v{conflict.current_version}。
              以下版本与你修改了<strong>同一批像素</strong>，系统未保存你的版本（不会静默覆盖）：</p>
            <ul>
              {conflict.conflicts.map((c, i) => (
                <li key={i}>
                  <b>{c.rival_author}</b> 的 v{c.rival_version}：重叠 {c.overlap_pixels} 像素，
                  {c.regions.length} 处区域（画布上黄色框）
                </li>
              ))}
            </ul>
            <p>请选择处理方式：</p>
            <div className="toolbar">
              <button onClick={loadAll}>放弃我的修改，载入最新版本</button>
              <button className="danger" onClick={() => {
                if (confirm('确认以你的版本覆盖对方的修改？此操作会记录在历史中。')) save('overwrite')
              }}>强制以我的为准覆盖</button>
              <button onClick={() => setConflict(null)}>继续查看冲突区域</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
