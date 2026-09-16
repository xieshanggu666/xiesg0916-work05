import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'

const STATUS_LABEL = {
  draft: '草稿', in_review: '待复核', approved: '已通过',
  changes_requested: '被退回', stale: '待迁移', invalidated: '已作废',
}

export default function ImageDetailPage() {
  const { id } = useParams()
  const [anns, setAnns] = useState([])
  const [label, setLabel] = useState('')
  const [error, setError] = useState('')

  const load = () => api.listAnnotations({ image_id: id }).then(setAnns).catch(e => setError(e.message))
  useEffect(() => { load() }, [id])

  const create = async () => {
    if (!label.trim()) return
    try { await api.createAnnotation(id, label.trim()); setLabel(''); load() }
    catch (e) { setError(e.message) }
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
                <td>{a.label}</td>
                <td><span className={`badge ${a.status}`}>{STATUS_LABEL[a.status]}</span></td>
                <td>v{a.current_version}</td>
                <td>r{a.image_revision}</td>
                <td>{a.open_regions.length > 0 ? `${a.open_regions.length} 处被退回` : '—'}</td>
                <td>
                  {a.status === 'stale' ? (
                    <>
                      <button onClick={() => migrate(a.id)}>迁移到新原图</button>{' '}
                      <button className="danger" onClick={() => invalidate(a.id)}>作废</button>
                    </>
                  ) : a.status !== 'invalidated' && (
                    <>
                      <Link to={`/annotate/${a.id}`}>修边</Link>{' '}
                      <Link to={`/review/${a.id}`}>复核</Link>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
