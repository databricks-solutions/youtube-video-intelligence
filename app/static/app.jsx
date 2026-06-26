const { useState, useEffect, useRef } = React;

// YouTube logo SVG
const YTIcon = () => (
  <svg viewBox="0 0 90 63" style={{width: 40, height: 28, flexShrink: 0}}>
    <path d="M88.1,9.5c-1-3.8-4-6.8-7.8-7.8C73.3,0,45,0,45,0S16.7,0,9.7,1.7c-3.8,1-6.8,4-7.8,7.8C0,16.5,0,31.3,0,31.3s0,14.8,1.9,21.8c1,3.8,4,6.8,7.8,7.8C16.7,62.5,45,62.5,45,62.5s28.3,0,35.3-1.7c3.8-1,6.8-4,7.8-7.8C90,46.1,90,31.3,90,31.3S90,16.5,88.1,9.5z" fill="#FF0000"/>
    <polygon points="36,17.5 36,45 59,31.3" fill="white"/>
  </svg>
);

const Spinner = ({text}) => (
  <div className="spinner-wrap"><div className="spinner"></div>{text}</div>
);

const ProgressBar = ({pct, message}) => (
  <div>
    <div className="progress-bar"><div className="progress-fill" style={{width: `${pct}%`}}></div></div>
    <div className="progress-text">{message}</div>
  </div>
);

const Markdown = ({text}) => {
  if (!text) return null;
  return <div className="result" dangerouslySetInnerHTML={{__html: marked.parse(text)}} />;
};

const ModelTag = ({name}) => {
  if (!name) return null;
  return <div className="model-tag">Analyzed with <code>{name}</code></div>;
};

const VideoCard = ({title, channel, thumbnail, url}) => (
  <div className="video-card">
    {thumbnail && <img src={thumbnail} alt={title} />}
    <div>
      <h3><a href={url} target="_blank" rel="noopener">{title}</a></h3>
      <div className="meta">{channel}</div>
    </div>
  </div>
);

const Mosaic = ({items}) => {
  if (!items || items.length === 0) return null;
  return (
    <div className="mosaic">
      {items.map((v, i) => (
        <a key={i} href={v.url} target="_blank" rel="noopener" title={v.title}>
          <img src={v.thumbnail} alt={v.title} />
        </a>
      ))}
    </div>
  );
};

// --- History Panel ---
const HistoryPanel = ({items, onSelect, selectedId}) => {
  const [open, setOpen] = useState(false);
  const count = items ? items.length : 0;
  return (
    <div>
      <div className="history-header" onClick={() => setOpen(!open)}>
        History {count > 0 && `(${count})`} <span>{open ? '\u25BC' : '\u25B6'}</span>
      </div>
      {open && (
        <div className="history-list">
          {count === 0 && <div style={{color: '#555', fontSize: 13, padding: '10px 12px'}}>No history yet.</div>}
          {(items || []).map(item => (
            <div
              key={item.id}
              className={`history-item ${selectedId === item.id ? 'selected' : ''}`}
              onClick={() => onSelect(item.id)}
            >
              <div className="title">{item.label}</div>
              <div className="mode">{item.mode}</div>
              <div className="time">{item.time}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

// --- Single Video Tab ---
const SingleVideoTab = () => {
  const [url, setUrl] = useState('');
  const [question, setQuestion] = useState('');
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState('');
  const [meta, setMeta] = useState(null);
  const [result, setResult] = useState('');
  const [model, setModel] = useState('');
  const [error, setError] = useState('');
  const [history, setHistory] = useState([]);
  const [selectedId, setSelectedId] = useState(null);

  const loadHistory = async () => {
    try {
      const res = await fetch('/api/history/videos');
      const data = await res.json();
      setHistory(data.map(mapVideoHistoryItem));
    } catch(e) {}
  };

  useEffect(() => { loadHistory(); }, []);

  const analyze = (e) => {
    if (e) e.preventDefault();
    if (!url.trim()) return;
    setLoading(true); setStatus(''); setMeta(null); setResult(''); setModel(''); setError(''); setSelectedId(null);

    fetch('/api/analyze-video', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(buildAnalyzeVideoBody(url, question)),
    }).then(response => consumeSSEStream(response, event => {
      const patch = videoEventPatch(event);
      applyPatch({
        status: setStatus,
        meta: setMeta,
        result: setResult,
        model: setModel,
        error: setError,
        loading: setLoading,
      }, patch);
      if (patch && patch.refreshHistory) loadHistory();
    })).then(() => setLoading(false)).catch(e => {
      if (e.name !== 'AbortError') setError('Connection lost. Try again.');
      setLoading(false);
    });
  };

  const loadHistoryItem = async (id) => {
    setSelectedId(id);
    try {
      const res = await fetch(`/api/history/videos/${id}`);
      const view = historyDetailToVideoState(await res.json());
      setMeta(view.meta);
      setResult(view.result);
      setModel('');
      setError('');
    } catch(e) { setError('Failed to load history item.'); }
  };

  return (
    <div className="tab-layout">
      <div className="tab-main">
        <form onSubmit={analyze}>
          <div className="field">
            <label>YouTube URL</label>
            <input value={url} onChange={e => setUrl(e.target.value)} placeholder="https://www.youtube.com/watch?v=..." />
          </div>
          <div className="field">
            <label>Question (optional)</label>
            <textarea value={question} onChange={e => setQuestion(e.target.value)} placeholder="Ask a specific question, or leave blank for a summary" rows={2} />
          </div>
          <button type="submit" className="btn btn-primary" disabled={loading} style={{marginBottom: 16}}>
            {loading ? 'Analyzing...' : 'Analyze'}
          </button>
        </form>

        {status && <Spinner text={status} />}
        {error && <div className="error-msg">{error}</div>}
        {meta && <VideoCard {...meta} />}
        {result && <ModelTag name={model} />}
        {result && <Markdown text={result} />}
      </div>
      <div className="tab-sidebar">
        <HistoryPanel items={history} onSelect={loadHistoryItem} selectedId={selectedId} />
      </div>
    </div>
  );
};

// --- Theme Explorer Tab ---
const ThemeExplorerTab = () => {
  const [theme, setTheme] = useState('');
  const [question, setQuestion] = useState('');
  const [dateStart, setDateStart] = useState('');
  const [dateEnd, setDateEnd] = useState('');
  const [maxDur, setMaxDur] = useState('30');
  const [blacklist, setBlacklist] = useState('');
  const [allowlist, setAllowlist] = useState('');
  const [showFilters, setShowFilters] = useState(false);
  const [loading, setLoading] = useState(false);
  const [progress, setProgress] = useState(null);
  const [foundVideos, setFoundVideos] = useState([]);
  const [mosaic, setMosaic] = useState([]);
  const [synthesis, setSynthesis] = useState('');
  const [model, setModel] = useState('');
  const [progressText, setProgressText] = useState('');
  const [error, setError] = useState('');
  const [history, setHistory] = useState([]);
  const [selectedId, setSelectedId] = useState(null);

  const loadHistory = async () => {
    try {
      const res = await fetch('/api/history/themes');
      const data = await res.json();
      setHistory(data.map(mapThemeHistoryItem));
    } catch(e) {}
  };

  useEffect(() => { loadHistory(); }, []);

  const eventCountRef = useRef(0);
  const taskIdRef = useRef(null);
  const finishedRef = useRef(false);

  const handleThemeEvent = (event) => {
    const patch = themeEventPatch(event);
    applyPatch({
      progress: setProgress,
      foundVideos: setFoundVideos,
      mosaic: setMosaic,
      synthesis: setSynthesis,
      model: setModel,
      progressText: setProgressText,
      error: setError,
      loading: setLoading,
    }, patch);
    if (patch && patch.finished) finishedRef.current = true;
    if (patch && patch.refreshHistory) loadHistory();
  };

  const streamEvents = (taskId, after) => {
    fetch(`/api/analyze-theme/${taskId}/events?after=${after}`)
    .then(response => consumeSSEStream(response, event => {
      if (event.type === 'keepalive') return;
      eventCountRef.current++;
      handleThemeEvent(event);
    })).then(() => {
      if (!finishedRef.current) {
        setTimeout(() => streamEvents(taskId, eventCountRef.current), 1000);
      }
    }).catch(() => {
      if (!finishedRef.current) {
        setTimeout(() => streamEvents(taskId, eventCountRef.current), 2000);
      }
    });
  };

  const analyze = (e) => {
    if (e) e.preventDefault();
    if (!theme.trim()) return;
    const durCheck = validateMaxDuration(maxDur);
    if (!durCheck.ok) {
      setError(durCheck.error);
      return;
    }
    setLoading(true); setProgress(null); setFoundVideos([]); setMosaic([]); setSynthesis(''); setModel('');
    setProgressText(''); setError(''); setSelectedId(null);
    eventCountRef.current = 0;
    finishedRef.current = false;

    fetch('/api/analyze-theme', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(buildAnalyzeThemeBody(theme, dateStart, dateEnd, durCheck.value, blacklist, allowlist, question)),
    }).then(r => r.json()).then(data => {
      taskIdRef.current = data.task_id;
      streamEvents(data.task_id, 0);
    }).catch(() => {
      setError('Failed to start analysis.');
      setLoading(false);
    });
  };

  const loadHistoryItem = async (id) => {
    setSelectedId(id);
    try {
      const res = await fetch(`/api/history/themes/${id}`);
      const view = historyDetailToThemeState(await res.json());
      setMosaic(view.mosaic);
      setSynthesis(view.synthesis);
      setModel('');
      setFoundVideos(view.foundVideos);
      setProgressText(view.progressText);
      setProgress(view.progress);
      setError('');
    } catch(e) { setError('Failed to load history item.'); }
  };

  return (
    <div className="tab-layout">
      <div className="tab-main">
        <form onSubmit={analyze}>
          <div className="field">
            <label>Theme</label>
            <input value={theme} onChange={e => setTheme(e.target.value)} placeholder="e.g., audience reaction to our latest show launch" />
          </div>

          <div className="field">
            <label>Question (optional)</label>
            <textarea value={question} onChange={e => setQuestion(e.target.value)} placeholder="Ask a question to answer across the videos, or leave blank for a synthesis" rows={2} />
          </div>

          <div className="accordion-header" onClick={() => setShowFilters(!showFilters)}>
            {showFilters ? '\u25BC' : '\u25B6'} Filters
          </div>
          {showFilters && (
            <div style={{marginBottom: 12, padding: '8px 0'}}>
              <div style={{display: 'flex', gap: 10, marginBottom: 8}}>
                <div className="field" style={{flex: 1}}>
                  <label>Published after</label>
                  <input value={dateStart} onChange={e => setDateStart(e.target.value)} placeholder="YYYY-MM-DD" />
                </div>
                <div className="field" style={{flex: 1}}>
                  <label>Published before</label>
                  <input value={dateEnd} onChange={e => setDateEnd(e.target.value)} placeholder="YYYY-MM-DD" />
                </div>
                <div className="field" style={{flex: 1}}>
                  <label>Max video length (min)</label>
                  <input type="number" value={maxDur} onChange={e => setMaxDur(e.target.value)} min={1} max={180} />
                </div>
              </div>
              <div className="field">
                <label>Exclude channels (one per line)</label>
                <textarea value={blacklist} onChange={e => setBlacklist(e.target.value)} rows={2} />
              </div>
              <div className="field">
                <label>Only these channels (one per line, leave blank for all)</label>
                <textarea value={allowlist} onChange={e => setAllowlist(e.target.value)} rows={2} />
              </div>
            </div>
          )}

          <div className="btn-row">
            <button type="submit" className="btn btn-primary" disabled={loading}>
              {loading ? 'Analyzing...' : 'Search & Analyze'}
            </button>
          </div>
        </form>

        {progress && <ProgressBar pct={progress.pct} message={progress.message} />}
        {error && <div className="error-msg">{error}</div>}

        <Mosaic items={mosaic} />

        {foundVideos.length > 0 && !mosaic.length && (
          <details style={{marginBottom: 12}}>
            <summary style={{cursor: 'pointer', color: '#58a6ff', fontWeight: 600, fontSize: 13}}>
              Found {foundVideos.length} videos (analyzing top {foundVideos.length})
            </summary>
            <div style={{marginTop: 8}}>
              {foundVideos.map((v, i) => (
                <div key={i} style={{display: 'flex', gap: 10, padding: '6px 0', alignItems: 'center'}}>
                  <img src={v.thumbnail} style={{width: 100, borderRadius: 4}} />
                  <div>
                    <div style={{fontWeight: 500, fontSize: 13}}><a href={v.url} target="_blank" style={{color: '#58a6ff', textDecoration: 'none'}}>{v.title}</a></div>
                    <div style={{color: '#888', fontSize: 12}}>{buildVideoMeta(v.channel, v.views, v.duration)}</div>
                  </div>
                </div>
              ))}
            </div>
          </details>
        )}

        {progressText && <div style={{fontSize: 13, color: '#aaa', marginBottom: 8}}>{progressText}</div>}
        {synthesis && <ModelTag name={model} />}
        {synthesis && <Markdown text={synthesis} />}
      </div>
      <div className="tab-sidebar">
        <HistoryPanel items={history} onSelect={loadHistoryItem} selectedId={selectedId} />
      </div>
    </div>
  );
};

// --- App ---
const App = () => {
  const [tab, setTab] = useState('video');
  return (
    <div className="container">
      <div className="header">
        <a href="/">
          <YTIcon />
          <div>
            <h1>Video Intelligence</h1>
            <div className="subtitle">Powered by Gemini on Databricks</div>
          </div>
        </a>
      </div>
      <div className="tabs">
        <button className={`tab ${tab === 'video' ? 'active' : ''}`} onClick={() => setTab('video')}>Single Video</button>
        <button className={`tab ${tab === 'theme' ? 'active' : ''}`} onClick={() => setTab('theme')}>Theme Explorer</button>
      </div>
      <div style={{display: tab === 'video' ? 'block' : 'none'}}><SingleVideoTab /></div>
      <div style={{display: tab === 'theme' ? 'block' : 'none'}}><ThemeExplorerTab /></div>
    </div>
  );
};

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
