import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'

export default function ImagesPage() {
  const [images, setImages] = useState([])
  const [error, setError] = useState('')
  const fileRef = useRef()
  const replaceRef = useRef()
  const [replaceTarget, setReplaceTarget] = useState(null)

  const load = () => api.listImages().then(setImages).catch(e => setError(e.message))
  useEffect(() => { load() }, [])

  const upload = async () => {
    const f = fileRef.current.files[0]
    if (!f) return
    try { await api.uploadImage(f.name.replace(/\.\w+$/, ''), f); load() }
    catch (e) { setError(e.message) }
  }

  const replace = async () => {
    const f = replaceRef.current.files[0]
    if (!f || !replaceTarget) return
    try {
      const r = await api.replaceImage(replaceTarget, f)
      alert(`已替换为第 ${r.image.current_revision} 版。${r.stale_annotation_ids.length} 条标注变为「待迁移」，需逐条显式迁移或作废。`)
      setReplaceTarget(null); load()
    } catch (e) { setError(e.message) }
  }

  return (
    <div>
      <h2>图片库</h2>
      {error && <p className="error">{error}</p>}
      <div className="card toolbar">
        <input type="file" accept="image/*" ref={fileRef} />
        <button className="primary" onClick={upload}>上传图片</button>
        <input type="file" accept="image/*" ref={replaceRef} />
        <button onClick={replace} disabled={!replaceTarget}>替换选中图片的原图</button>
      </div>
      <div className="card">
        <table>
          <thead><tr><th>ID</th><th>名称</th><th>版本</th><th>尺寸</th><th>待迁移标注</th><th></th></tr></thead>
          <tbody>
            {images.map(im => (
              <tr key={im.id}>
                <td>{im.id}</td>
                <td>{im.name}</td>
                <td>r{im.current_revision}</td>
                <td>{im.width}×{im.height}</td>
                <td>{im.stale_annotations > 0
                  ? <span className="badge stale">{im.stale_annotations} 条待处理</span> : '—'}</td>
                <td>
                  <Link to={`/images/${im.id}`}>进入</Link>{' '}
                  <label>
                    <input type="radio" name="replace" onChange={() => setReplaceTarget(im.id)} /> 替换
                  </label>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
