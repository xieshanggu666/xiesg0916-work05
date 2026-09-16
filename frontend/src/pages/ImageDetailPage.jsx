import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'

const STATUS_LABEL = {
  draft: '草稿', in_review: '待复核', approved: '已通过',
  changes_requested: '被退回', stale: '待迁移', invalidated: '已作废',
}

const ARB_STATUS_LABEL = {
  open: '双盲标注中', arbitrating: '待裁定', completed: '已完成', void: '已失效',
}

export default function ImageDetailPage() {
  const { id } = useParams()
  const [anns, setAnns] = useState([])
  const [arbs, setArbs] = useState([])
  const [label, setLabel] = useState('')
  const [arbForm, setArbForm] = useState({ label: '', annotator_a: '', annotator_b: '', arbitrator: '' })
  const [error, setError] = useState('')

  const load = () => {
    api.listAnnotations({ image_id: id }).then(setAnns).catch(e => setError(e.message))
    api.listArbitrations(id).then(setArbs).catch(e => setError(e.message))
  }
  useEffect(() => { load() }, [id])

  const create = async () => {
    if (!label.trim()) return
    try { await api.createAnnotation(id, label.trim()); setLabel(''); load() }
    catch (e) { setError(e.message) }
  }
  const initiate = async () => {
    setError('')
    try {
      await api.createArbitration({
        image_id: +id, label: arbForm.label.trim(), initiator: 'boss',
        annotator_a: arbForm.annotator_a.trim(), annotator_b: arbForm.annotator_b.trim(),
        arbitrator: arbForm.arbitrator.trim(),
      })
      setArbForm({ label: '', annotator_a: '', annotator_b: '', arbitrator: '' })
      load()
    } catch (e) { setError(e.message) }
  }
  const migrate = async (annId) => {
    try { await api.migrate(annId, 'annotator'); load() } catch (e) { setError(e.message) }
  }
  const invalidate = async (annId) => {
    if (!confirm('确认作废该标注？此操作不可恢复。')) return
    try { await api.invalidate(annId); load() } catch (e) { setError(e.message) }
  }

  const stale = anns.filter(a => a.status === 'stale')

  return (
    <div>
      <h2>图片 #{id} 的标注</h2>
      {error && <p className="error">{error}</p>}
      {stale.length > 0 && (
        <div className="card" style={{ borderColor: '#eab308' }}>
          ⚠️ 原图已替换，{stale.length} 条标注基于旧版本。必须显式「迁移」到新原图或「作废」，系统不会自动处理。
        </div>
      )}
      <div className="card toolbar">
        <input value={label} onChange={e => setLabel(e.target.value)} placeholder="新标注类别名" />
        <button className="primary" onClick={create}>新建标注</button>
      </div>
      <div className="card">
        <table>
          <thead><tr><th>ID</th><th>类别</th><th>状态</th><th>版本</th><th>基于原图</th><th>待处理区域</th><th>操作</th></tr></thead>
          <tbody>
            {anns.map(a => (
              <tr key={a.id}>
                <td>{a.id}</td>
                <td>{a.label}{a.arbitration && <span className="badge">双盲{a.arbitration.side.toUpperCase()}</span>}</td>
                <td><span className={`badge ${a.status}`}>{STATUS_LABEL[a.status]}</span></td>
                <td>v{a.current_version}</td>
                <td>r{a.image_revision}</td>
                <td>{a.open_regions.length > 0 ? `${a.open_regions.length} 处被退回` : '—'}</td>
                <td>
                  {a.status === 'stale' ? (
                    <>
                      {!a.arbitration && <button onClick={() => migrate(a.id)}>迁移到新原图</button>}{' '}
                      <button className="danger" onClick={() => invalidate(a.id)}>作废</button>
                    </>
                  ) : a.status !== 'invalidated' && (
                    <>
                      <Link to={`/annotate/${a.id}`}>修边</Link>{' '}
                      {!a.arbitration && <Link to={`/review/${a.id}`}>复核</Link>}
                      {a.arbitration && <Link to={`/arbitrations/${a.arbitration.id}`}>仲裁</Link>}
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2>双盲仲裁</h2>
      <div className="card toolbar">
        <input value={arbForm.label} placeholder="类别名"
          onChange={e => setArbForm(f => ({ ...f, label: e.target.value }))} />
        <input value={arbForm.annotator_a} placeholder="标注者甲"
          onChange={e => setArbForm(f => ({ ...f, annotator_a: e.target.value }))} />
        <input value={arbForm.annotator_b} placeholder="标注者乙"
          onChange={e => setArbForm(f => ({ ...f, annotator_b: e.target.value }))} />
        <input value={arbForm.arbitrator} placeholder="仲裁者"
          onChange={e => setArbForm(f => ({ ...f, arbitrator: e.target.value }))} />
        <button className="primary" onClick={initiate}
          disabled={!arbForm.label.trim() || !arbForm.annotator_a.trim() || !arbForm.annotator_b.trim() || !arbForm.arbitrator.trim()}>
          发起双盲仲裁
        </button>
      </div>
      <div className="card">
        <table>
          <thead><tr><th>ID</th><th>类别</th><th>状态</th><th>甲方</th><th>乙方</th><th>仲裁者</th><th>差异区域</th><th>操作</th></tr></thead>
          <tbody>
            {arbs.map(a => (
              <tr key={a.id}>
                <td>{a.id}</td>
                <td>{a.label}</td>
                <td><span className={`badge ${a.status}`}>{ARB_STATUS_LABEL[a.status]}</span></td>
                <td>{a.side_a.assignee}{a.side_a.submitted ? ' ✓' : ''}</td>
                <td>{a.side_b.assignee}{a.side_b.submitted ? ' ✓' : ''}</td>
                <td>{a.arbitrator}</td>
                <td>{a.diff_regions ? `${a.diff_regions.length} 处` : '—'}</td>
                <td><Link to={`/arbitrations/${a.id}`}>详情</Link></td>
              </tr>
            ))}
            {arbs.length === 0 && <tr><td colSpan="8">暂无仲裁任务</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}
