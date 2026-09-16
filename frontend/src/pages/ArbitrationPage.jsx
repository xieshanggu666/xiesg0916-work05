import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'

const STATUS_LABEL = {
  open: '双盲标注中', arbitrating: '待裁定', completed: '已完成', void: '已失效',
}

function loadImage(url) {
  return new Promise((res, rej) => {
    const img = new Image(); img.onload = () => res(img); img.onerror = rej; img.src = url
  })
}

function tint(mask, color) {
  const c = document.createElement('canvas')
  c.width = mask.width; c.height = mask.height
  const ctx = c.getContext('2d')
  ctx.drawImage(mask, 0, 0)
  ctx.globalCompositeOperation = 'source-in'
  ctx.fillStyle = color; ctx.fillRect(0, 0, c.width, c.height)
  return c
}

export default function ArbitrationPage() {
  const { id } = useParams()
  const [arb, setArb] = useState(null)
  const [picks, setPicks] = useState({}) // region_index -> 'a' | 'b'
  const [arbitrator, setArbitrator] = useState(localStorage.getItem('arbitrator') || '')
  const [msg, setMsg] = useState('')
  const canvasRef = useRef()
  const layersRef = useRef({})

  const redraw = useCallback((a = arb, regions = a?.diff_regions || []) => {
    const canvas = canvasRef.current
    const { img, maskA, maskB } = layersRef.current
    if (!canvas || !img) return
    const ctx = canvas.getContext('2d')
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    ctx.drawImage(img, 0, 0)
    if (maskA) { ctx.globalAlpha = 0.45; ctx.drawImage(tint(maskA, '#ef4444'), 0, 0); ctx.globalAlpha = 1 }
    if (maskB) { ctx.globalAlpha = 0.45; ctx.drawImage(tint(maskB, '#3b82f6'), 0, 0); ctx.globalAlpha = 1 }
    // difference regions in yellow, numbered
    ctx.font = '12px sans-serif'
    for (const r of regions) {
      ctx.strokeStyle = '#eab308'; ctx.lineWidth = 2
      ctx.strokeRect(r.x, r.y, r.w, r.h)
      ctx.fillStyle = '#eab308'
      ctx.fillText(`#${r.index}`, r.x + 2, r.y + 12)
    }
  }, [arb])

  const loadAll = useCallback(async () => {
    const a = await api.getArbitration(id)
    setArb(a)
    setArbitrator(prev => prev || a.arbitrator)
    const showDiff = a.status === 'arbitrating' || a.status === 'completed' ||
      (a.status === 'void' && a.diff_regions)
    if (showDiff) {
      const [img, maskA, maskB] = await Promise.all([
        loadImage(api.imageUrl(a.image_id, a.image_revision)),
        loadImage(api.arbitrationMaskUrl(a.id, 'a')),
        loadImage(api.arbitrationMaskUrl(a.id, 'b')),
      ])
      layersRef.current = { img, maskA, maskB }
      canvasRef.current.width = img.width
      canvasRef.current.height = img.height
      redraw(a)
    }
  }, [id, redraw])

  useEffect(() => { loadAll().catch(e => setMsg(e.message)) }, [loadAll])
  useEffect(() => { redraw() }, [arb, redraw])

  const submitSide = async (assignee) => {
    setMsg('')
    try { await api.submitArbitrationSide(id, assignee); await loadAll() }
    catch (e) { setMsg(e.message) }
  }

  const submitAdjudication = async () => {
    setMsg('')
    localStorage.setItem('arbitrator', arbitrator)
    const decisions = (arb.diff_regions || []).map(r => ({ region_index: r.index, pick: picks[r.index] }))
    try {
      await api.adjudicate(id, arbitrator, decisions)
      setMsg('裁定完成，正式掩膜版本已进入复核')
      await loadAll()
    } catch (e) { setMsg(e.message) }
  }

  if (!arb) return <p>加载中… {msg && <span className="error">{msg}</span>}</p>

  const regions = arb.diff_regions || []
  const allPicked = regions.every(r => picks[r.index])

  return (
    <div>
      <h2>双盲仲裁 #{arb.id}（{arb.label}）<span className={`badge ${arb.status}`}>{STATUS_LABEL[arb.status]}</span></h2>
      <div className="card">
        <p>
          负责人 <b>{arb.initiator}</b> · 仲裁者 <b>{arb.arbitrator}</b> · 基于原图 r{arb.image_revision}
          {arb.voided_at && <> · 失效于 {new Date(arb.voided_at).toLocaleString()}</>}
        </p>
        <table>
          <thead><tr><th>侧</th><th>标注者</th><th>标注</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            {[['A', arb.side_a], ['B', arb.side_b]].map(([name, s]) => (
              <tr key={name}>
                <td>{name}</td>
                <td>{s.assignee}</td>
                <td>#{s.annotation_id}</td>
                <td>{s.submitted ? `已提交（v${s.submitted_version}）` : '标注中'}</td>
                <td>
                  {arb.status === 'open' && !s.submitted && (
                    <>
                      <Link to={`/annotate/${s.annotation_id}`}>进入标注</Link>{' '}
                      <button onClick={() => submitSide(s.assignee)}>提交该侧</button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {arb.status === 'open' && (
          <p className="region-list">双盲隔离中：双方掩膜互不可见，双方均提交后才会展示差异并开放裁定。</p>
        )}
      </div>

      {(arb.status === 'arbitrating' || arb.status === 'completed' || (arb.status === 'void' && arb.diff_regions)) && (
        <div className="card">
          <p>差异区域 {regions.length} 处（黄色框），共 {arb.diff_pixels} 像素。红色 = A 方（{arb.side_a.assignee}），蓝色 = B 方（{arb.side_b.assignee}）。</p>
          <div className="canvas-wrap"><canvas ref={canvasRef} /></div>
        </div>
      )}

      {arb.status === 'arbitrating' && (
        <div className="card">
          <h3>裁定</h3>
          {regions.length === 0 && <p>双方结果完全一致，无差异区域，可直接确认生成正式版本。</p>}
          {regions.map(r => (
            <div key={r.index} className="toolbar" style={{ borderBottom: '1px solid #333' }}>
              <span>#{r.index}（{r.w}×{r.h} @ {r.x},{r.y}，{r.pixels} 像素）</span>
              <label><input type="radio" name={`r${r.index}`} checked={picks[r.index] === 'a'}
                onChange={() => setPicks(p => ({ ...p, [r.index]: 'a' }))} /> 选 A（{arb.side_a.assignee}）</label>
              <label><input type="radio" name={`r${r.index}`} checked={picks[r.index] === 'b'}
                onChange={() => setPicks(p => ({ ...p, [r.index]: 'b' }))} /> 选 B（{arb.side_b.assignee}）</label>
            </div>
          ))}
          <div className="toolbar">
            <label>仲裁者 <input value={arbitrator} onChange={e => setArbitrator(e.target.value)} size={8} /></label>
            <button className="primary" disabled={!allPicked || !arbitrator} onClick={submitAdjudication}>
              提交裁定，生成正式掩膜版本
            </button>
          </div>
        </div>
      )}

      {arb.status === 'completed' && (
        <div className="card">
          <h3>裁定记录</h3>
          <table>
            <thead><tr><th>区域</th><th>范围</th><th>裁定</th><th>采纳方</th><th>仲裁者</th><th>时间</th></tr></thead>
            <tbody>
              {arb.decisions.map(d => (
                <tr key={d.region_index}>
                  <td>#{d.region_index}</td>
                  <td>{d.region.w}×{d.region.h} @ {d.region.x},{d.region.y}（{d.region.pixels} 像素）</td>
                  <td>{d.pick.toUpperCase()}</td>
                  <td>{d.picked_author}</td>
                  <td>{d.actor}</td>
                  <td>{new Date(d.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p>
            正式标注 #{arb.result_annotation_id} 已进入复核。
            <Link to={`/review/${arb.result_annotation_id}`}>前往复核</Link>
          </p>
        </div>
      )}

      {arb.status === 'void' && (
        <div className="card" style={{ borderColor: '#eab308' }}>
          ⚠️ 原图已替换，本仲裁在完成前失效。已有提交与裁定记录保留可查，两侧标注按普通 stale 规则处理。
        </div>
      )}
      {msg && <p className={msg.includes('完成') ? 'ok' : 'error'}>{msg}</p>}
    </div>
  )
}
