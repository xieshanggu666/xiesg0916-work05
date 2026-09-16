import { useEffect, useState } from 'react'
import { api } from '../api'

export default function ExportsPage() {
  const [jobs, setJobs] = useState([])
  const [name, setName] = useState('')
  const [error, setError] = useState('')

  const load = () => api.listExports().then(setJobs).catch(e => setError(e.message))
  useEffect(() => {
    load()
    const t = setInterval(() => {
      setJobs(prev => {
        if (prev.some(j => j.status === 'running' || j.status === 'pending')) load()
        return prev
      })
    }, 2000)
    return () => clearInterval(t)
  }, [])

  const create = async () => {
    if (!name.trim()) return
    try { await api.createExport(name.trim()); setName(''); load() }
    catch (e) { setError(e.message) }
  }
  const retry = async (id) => {
    try { await api.retryExport(id); load() } catch (e) { setError(e.message) }
  }

  return (
    <div>
      <h2>训练集导出（按冻结版本）</h2>
      {error && <p className="error">{error}</p>}
      <div className="card toolbar">
        <input value={name} onChange={e => setName(e.target.value)} placeholder="导出任务名" />
        <button className="primary" onClick={create}>冻结当前已通过标注并导出</button>
      </div>
      <div className="card">
        <table>
          <thead><tr><th>ID</th><th>名称</th><th>状态</th><th>进度</th><th>尝试次数</th><th>错误</th><th>操作</th></tr></thead>
          <tbody>
            {jobs.map(j => (
              <tr key={j.id}>
                <td>{j.id}</td>
                <td>{j.name}</td>
                <td><span className={`badge ${j.status}`}>{j.status}</span></td>
                <td>{j.done_items}/{j.total_items}</td>
                <td>{j.attempt}</td>
                <td className="error">{j.error}</td>
                <td>
                  {j.status === 'failed' && <button onClick={() => retry(j.id)}>重试（断点续跑）</button>}
                  {j.status === 'completed' && <span className="ok">{j.output_dir}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
