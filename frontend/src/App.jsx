import { useEffect, useRef, useState } from 'react'

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'
const POLL_MS = 3000

const COLUMNS = [
  { key: 'pending', title: 'Pending' },
  { key: 'in_progress', title: 'In Progress' },
  { key: 'completed', title: 'Completed' },
]

function taskText(task) {
  if (task.transcription_status === 'processing') return '🎙 Transcribing…'
  if (task.transcription_status === 'failed') return '⚠️ Transcription failed'
  return task.text
}

function formatTime(iso) {
  // the API returns UTC without a timezone marker, so add "Z"
  return new Date(iso + 'Z').toLocaleString()
}

export default function App() {
  const [tasks, setTasks] = useState([])
  const [error, setError] = useState(null)
  const [fresh, setFresh] = useState(new Set())
  const seen = useRef(null)

  async function load() {
    try {
      const res = await fetch(`${API_URL}/tasks`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()

      if (seen.current) {
        const added = data.filter((t) => !seen.current.has(t.id)).map((t) => t.id)
        if (added.length) {
          setFresh(new Set(added))
          setTimeout(() => setFresh(new Set()), 3000)
        }
      }
      seen.current = new Set(data.map((t) => t.id))

      setTasks(data)
      setError(null)
    } catch {
      setError('Cannot reach the API. Retrying…')
    }
  }

  useEffect(() => {
    load()
    const id = setInterval(load, POLL_MS)
    return () => clearInterval(id)
  }, [])

  async function changeStatus(id, status) {
    setTasks((prev) => prev.map((t) => (t.id === id ? { ...t, status } : t)))
    try {
      const res = await fetch(`${API_URL}/tasks/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      })
      if (!res.ok) throw new Error()
    } catch {
      setError('Could not update the task.')
      load()
    }
  }

  return (
    <div className="app">
      <header>
        <h1>Task Dashboard</h1>
        <span className="live">● Live · {tasks.length} tasks</span>
      </header>

      {error && <div className="error">{error}</div>}

      <div className="board">
        {COLUMNS.map((col) => {
          const items = tasks.filter((t) => t.status === col.key)
          return (
            <section key={col.key} className="column">
              <h2>
                {col.title} <span className="count">{items.length}</span>
              </h2>
              {items.length === 0 && <p className="empty">No tasks</p>}
              {items.map((task) => (
                <article key={task.id} className={`card ${fresh.has(task.id) ? 'fresh' : ''}`}>
                  <p className="text">{taskText(task)}</p>
                  <div className="meta">
                    <span>
                      #{task.id} · {task.source === 'voice' ? '🎤 voice' : '💬 text'}
                    </span>
                    <span>{formatTime(task.created_at)}</span>
                  </div>
                  <select value={task.status} onChange={(e) => changeStatus(task.id, e.target.value)}>
                    {COLUMNS.map((c) => (
                      <option key={c.key} value={c.key}>
                        {c.title}
                      </option>
                    ))}
                  </select>
                </article>
              ))}
            </section>
          )
        })}
      </div>
    </div>
  )
}