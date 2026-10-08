import { useEffect, useMemo, useRef, useState } from 'react'

const API_URL = import.meta.env.VITE_API_URL || '/api'
const SAFETY_POLL_MS = 20000

const COLUMNS = [
  { key: 'pending', title: 'Pending', hint: 'Waiting to start', empty: 'New tasks land here' },
  { key: 'in_progress', title: 'In Progress', hint: 'Being worked on', empty: 'Nothing in progress' },
  { key: 'completed', title: 'Completed', hint: 'All wrapped up', empty: 'None finished yet' },
]

const FILTERS = [
  { key: 'all', label: 'All' },
  { key: 'text', label: 'Text' },
  { key: 'voice', label: 'Voice' },
]

function readView() {
  try {
    return localStorage.getItem('task-view') === 'list' ? 'list' : 'board'
  } catch {
    return 'board'
  }
}

function formatTime(iso) {
  if (!iso) return ''
  // API datetimes are UTC and have no timezone suffix.
  const normalized = /Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`
  const date = new Date(normalized)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function person(task) {
  const user = task.user
  if (!user) return null
  if (user.username) return `@${user.username}`
  if (user.first_name) return user.first_name
  return null
}

function Flower({ className }) {
  return (
    <svg className={className} viewBox="0 0 64 64" aria-hidden="true">
      <circle cx="32" cy="32" r="30" fill="#ffe4ef" />
      <circle cx="32" cy="26" r="8" fill="#ff8fb3" />
      <circle cx="20" cy="36" r="8" fill="#ffb3cc" />
      <circle cx="44" cy="36" r="8" fill="#ffc2d6" />
      <circle cx="26" cy="46" r="8" fill="#ff9fbe" />
      <circle cx="40" cy="46" r="8" fill="#f7a8c4" />
      <circle cx="32" cy="36" r="5.5" fill="#fff4b8" />
    </svg>
  )
}

function IconText() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="icon">
      <path
        fill="currentColor"
        d="M5 6.5A2.5 2.5 0 0 1 7.5 4h9A2.5 2.5 0 0 1 19 6.5v7A2.5 2.5 0 0 1 16.5 16H9l-3.6 2.4c-.4.3-.9 0-.9-.5V16A2.5 2.5 0 0 1 5 13.5v-7Z"
      />
    </svg>
  )
}

function IconMic() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="icon">
      <path
        fill="currentColor"
        d="M12 14a3 3 0 0 0 3-3V7a3 3 0 1 0-6 0v4a3 3 0 0 0 3 3Zm-5-3a1 1 0 1 1 2 0 5 5 0 0 0 10 0 1 1 0 1 1 2 0 7 7 0 0 1-6 6.9V20h2a1 1 0 1 1 0 2H9a1 1 0 1 1 0-2h2v-2.1A7 7 0 0 1 7 11Z"
      />
    </svg>
  )
}

function TaskBody({ task }) {
  if (task.transcription_status === 'processing') {
    return (
      <p className="text">
        Transcribing voice
        <span className="dots" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
      </p>
    )
  }
  if (task.transcription_status === 'failed') {
    return <p className="text">Could not transcribe this voice note</p>
  }
  return <p className="text">{task.text}</p>
}

function TaskCard({ task, fresh, saving, onStatus }) {
  const who = person(task)
  const classes = [
    'card',
    `status-${task.status}`,
    task.transcription_status === 'failed' ? 'bad' : '',
    task.transcription_status === 'processing' ? 'working' : '',
    fresh ? 'fresh' : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <article className={classes} aria-busy={task.transcription_status === 'processing'}>
      <TaskBody task={task} />
      <div className="meta">
        <span className="chip">#{task.id}</span>
        <span className="chip">
          {task.source === 'voice' ? <IconMic /> : <IconText />}
          {task.source === 'voice' ? 'Voice' : 'Text'}
        </span>
        {who && <span className="chip">{who}</span>}
        <time className="time" dateTime={task.created_at}>
          {formatTime(task.created_at)}
        </time>
      </div>
      <div className="toggles" role="group" aria-label={`Status for task ${task.id}`}>
        {COLUMNS.map((column) => (
          <button
            key={column.key}
            type="button"
            aria-pressed={task.status === column.key}
            disabled={saving}
            onClick={() => onStatus(task.id, column.key)}
          >
            {column.title}
          </button>
        ))}
      </div>
    </article>
  )
}

export default function App() {
  const [tasks, setTasks] = useState([])
  const [error, setError] = useState(null)
  const [fresh, setFresh] = useState(() => new Set())
  const [connected, setConnected] = useState(false)
  const [loading, setLoading] = useState(true)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [view, setView] = useState(readView)
  const [saving, setSaving] = useState(() => new Set())

  const tasksRef = useRef([])
  const seen = useRef(null)
  const savingRef = useRef(new Set())

  useEffect(() => {
    try {
      localStorage.setItem('task-view', view)
    } catch {
      // Private mode can block storage. The choice still applies for this visit.
    }
  }, [view])

  useEffect(() => {
    let stopped = false
    let source = null
    let retry = null
    let flashTimer = null

    function flash(ids) {
      if (!ids.length) return
      setFresh(new Set(ids))
      clearTimeout(flashTimer)
      flashTimer = setTimeout(() => {
        if (!stopped) setFresh(new Set())
      }, 2800)
    }

    function remember(list) {
      seen.current = new Set(list.map((task) => task.id))
    }

    async function load() {
      try {
        const res = await fetch(`${API_URL}/tasks`, { cache: 'no-store' })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const data = await res.json()
        if (stopped) return

        if (seen.current) {
          const added = data.filter((task) => !seen.current.has(task.id)).map((task) => task.id)
          if (added.length) flash(added)
        }

        const serverIds = new Set(data.map((task) => task.id))
        const arrivedDuringLoad = tasksRef.current.filter((task) => !serverIds.has(task.id))
        const pendingSaves = savingRef.current
        const merged = [...arrivedDuringLoad, ...data].map((task) => {
          if (!pendingSaves.has(task.id)) return task
          return tasksRef.current.find((item) => item.id === task.id) || task
        })

        remember(merged)
        tasksRef.current = merged
        setTasks(merged)
        setError(null)
      } catch {
        if (!stopped) setError('Cannot reach the API. Retrying…')
      } finally {
        if (!stopped) setLoading(false)
      }
    }

    function applyEvent(task, highlight) {
      const prev = tasksRef.current
      const index = prev.findIndex((item) => item.id === task.id)
      let nextTask = task
      if (index !== -1 && savingRef.current.has(task.id)) {
        nextTask = { ...task, status: prev[index].status }
      }
      const next = index === -1 ? [nextTask, ...prev] : prev.map((item) => (item.id === task.id ? nextTask : item))
      tasksRef.current = next
      if (seen.current) seen.current.add(task.id)
      setTasks(next)
      if (highlight) flash([task.id])
    }

    function connect() {
      source = new EventSource(`${API_URL}/events`)
      source.onopen = () => {
        if (!stopped) setConnected(true)
      }
      source.onmessage = (event) => {
        let payload
        try {
          payload = JSON.parse(event.data)
        } catch {
          return
        }
        const task = payload?.task
        if (!task || stopped) return
        const previous = tasksRef.current.find((item) => item.id === task.id)
        const arrived = !previous
        const transcribed = previous && previous.transcription_status !== 'done' && task.transcription_status === 'done'
        applyEvent(task, arrived || transcribed)
        setError(null)
      }
      source.onerror = () => {
        setConnected(false)
        source.close()
        if (!stopped) retry = setTimeout(connect, 2000)
      }
    }

    load()
    connect()
    const poll = setInterval(load, SAFETY_POLL_MS)
    const onFocus = () => load()
    window.addEventListener('focus', onFocus)

    return () => {
      stopped = true
      clearInterval(poll)
      clearTimeout(retry)
      clearTimeout(flashTimer)
      source?.close()
      window.removeEventListener('focus', onFocus)
    }
  }, [])

  async function changeStatus(id, status) {
    const current = tasksRef.current.find((task) => task.id === id)
    if (!current || current.status === status || savingRef.current.has(id)) return

    savingRef.current.add(id)
    setSaving(new Set(savingRef.current))

    const optimistic = tasksRef.current.map((task) => (task.id === id ? { ...task, status } : task))
    tasksRef.current = optimistic
    setTasks(optimistic)

    try {
      const res = await fetch(`${API_URL}/tasks/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const updated = await res.json()
      const merged = tasksRef.current.map((task) => (task.id === id ? updated : task))
      tasksRef.current = merged
      setTasks(merged)
      setError(null)
    } catch {
      const rolled = tasksRef.current.map((task) => (task.id === id ? { ...task, status: current.status } : task))
      tasksRef.current = rolled
      setTasks(rolled)
      setError('Could not update the task. Please try again.')
    } finally {
      savingRef.current.delete(id)
      setSaving(new Set(savingRef.current))
    }
  }

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return tasks.filter((task) => {
      if (filter !== 'all' && task.source !== filter) return false
      if (!needle) return true
      const who = person(task) || ''
      return `${task.text || ''} ${who} ${task.id}`.toLowerCase().includes(needle)
    })
  }, [tasks, query, filter])

  const counts = useMemo(
    () => ({
      all: tasks.length,
      pending: tasks.filter((task) => task.status === 'pending').length,
      in_progress: tasks.filter((task) => task.status === 'in_progress').length,
      completed: tasks.filter((task) => task.status === 'completed').length,
    }),
    [tasks],
  )

  const filtering = query.trim() !== '' || filter !== 'all'

  return (
    <div className="app">
      <header className="hero">
        <div className="hero-top">
          <div className="brand">
            <Flower className="mark" />
            <div>
              <h1>Task Dashboard</h1>
              <p>A soft board for tasks you send by text or voice.</p>
            </div>
          </div>
          <div className={`live ${connected ? '' : 'off'}`}>
            <span className="dot" />
            {connected ? 'Live' : 'Reconnecting'}
          </div>
        </div>

        <div className="stats">
          <div className="stat">
            <b>{counts.all}</b>
            <span>Tasks</span>
          </div>
          <div className="stat">
            <b>{counts.pending}</b>
            <span>Pending</span>
          </div>
          <div className="stat">
            <b>{counts.in_progress}</b>
            <span>In progress</span>
          </div>
          <div className="stat">
            <b>{counts.completed}</b>
            <span>Completed</span>
          </div>
        </div>
      </header>

      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}

      <div className="toolbar">
        <label className="search">
          <span className="sr-only">Search tasks</span>
          <input
            type="search"
            value={query}
            placeholder="Search tasks"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <div className="pills" role="group" aria-label="Filter by source">
          {FILTERS.map((item) => (
            <button
              key={item.key}
              type="button"
              aria-pressed={filter === item.key}
              onClick={() => setFilter(item.key)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="view" role="group" aria-label="Layout">
          <button type="button" aria-pressed={view === 'board'} onClick={() => setView('board')}>
            Board
          </button>
          <button type="button" aria-pressed={view === 'list'} onClick={() => setView('list')}>
            List
          </button>
        </div>
        {filtering && (
          <p className="showing">
            Showing {visible.length} of {tasks.length}
          </p>
        )}
      </div>

      {loading && <p className="loading">Gathering your tasks…</p>}

      {!loading && tasks.length === 0 && !error && (
        <p className="welcome-note">Send a text or voice message to the bot. It will show up here.</p>
      )}

      {!loading && view === 'board' && (
        <div className="board">
          {COLUMNS.map((column) => {
            const items = visible.filter((task) => task.status === column.key)
            return (
              <section key={column.key} className="column" aria-label={column.title}>
                <div className="column-head">
                  <div>
                    <h2>
                      <span className={`swatch swatch-${column.key}`} />
                      {column.title}
                    </h2>
                    <p className="hint">{column.hint}</p>
                  </div>
                  <span className="count">{items.length}</span>
                </div>
                <div className="column-body">
                  {items.length === 0 && <p className="empty">{column.empty}</p>}
                  {items.map((task) => (
                    <TaskCard
                      key={task.id}
                      task={task}
                      fresh={fresh.has(task.id)}
                      saving={saving.has(task.id)}
                      onStatus={changeStatus}
                    />
                  ))}
                </div>
              </section>
            )
          })}
        </div>
      )}

      {!loading && view === 'list' && (
        <div className="list">
          {visible.length === 0 && <p className="empty-board">No tasks to show.</p>}
          {visible.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              fresh={fresh.has(task.id)}
              saving={saving.has(task.id)}
              onStatus={changeStatus}
            />
          ))}
        </div>
      )}
    </div>
  )
}
