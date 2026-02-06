
import { useState, useCallback, useEffect } from 'react'
import { DiffViewer } from './components/DiffViewer'
import { ChatPanel } from './components/ChatPanel'
import { FlagsPanel } from './components/FlagsPanel'
import { BugsPanel } from './components/BugsPanel'
import { PRSummary } from './components/PRSummary'
import { PRSummarySkeleton } from './components/PRSummarySkeleton'
import type { PRInfo, DiffSelection, DiffCitation } from './types'

function App() {
  const [activeTab, setActiveTab] = useState<'chat' | 'flags' | 'bugs'>('chat')
  const [prUrl, setPrUrl] = useState('')
  const [prInfo, setPrInfo] = useState<PRInfo | null>(null)
  const [selectedFile, setSelectedFile] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [currentSelection, setCurrentSelection] = useState<DiffSelection | null>(null)
  const [highlightedLines, setHighlightedLines] = useState<{
    start: number
    end: number
    side: 'additions' | 'deletions'
  } | null>(null)

  // Resize logic
  const [chatWidth, setChatWidth] = useState(450)
  const [isResizing, setIsResizing] = useState(false)

  // PR Summary Resize logic
  const [prSummaryHeight, setPrSummaryHeight] = useState(300) // Default height
  const [isResizingSummary, setIsResizingSummary] = useState(false)

  const startResizing = useCallback(() => {
    setIsResizing(true)
  }, [])

  const stopResizing = useCallback(() => {
    setIsResizing(false)
  }, [])

  const resize = useCallback((e: MouseEvent) => {
    if (isResizing) {
      const newWidth = window.innerWidth - e.clientX
      // Constraints: Min 300px, Max 800px (or 60%)
      if (newWidth > 300 && newWidth < Math.max(800, window.innerWidth * 0.6)) {
        setChatWidth(newWidth)
      }
    }
  }, [isResizing])

  // PR Summary Resize Handlers
  const startResizingSummary = useCallback(() => {
    setIsResizingSummary(true)
  }, [])

  const stopResizingSummary = useCallback(() => {
    setIsResizingSummary(false)
  }, [])

  const resizeSummary = useCallback((e: MouseEvent) => {
    if (isResizingSummary) {
      // Direct clientY works because diff panel starts at top when PR is loaded
      // We might want to clamp it
      const newHeight = e.clientY
      const maxHeight = window.innerHeight * 0.8
      if (newHeight > 100 && newHeight < maxHeight) {
        setPrSummaryHeight(newHeight)
      }
    }
  }, [isResizingSummary])

  // Global event listeners for resizing
  useEffect(() => {
    if (isResizing) {
      window.addEventListener('mousemove', resize as any)
      window.addEventListener('mouseup', stopResizing)
    }
    if (isResizingSummary) {
      window.addEventListener('mousemove', resizeSummary as any)
      window.addEventListener('mouseup', stopResizingSummary)
    }
    return () => {
      window.removeEventListener('mousemove', resize as any)
      window.removeEventListener('mouseup', stopResizing)
      window.removeEventListener('mousemove', resizeSummary as any)
      window.removeEventListener('mouseup', stopResizingSummary)
    }
  }, [isResizing, resize, stopResizing, isResizingSummary, resizeSummary, stopResizingSummary])

  const handleSelectionChange = useCallback((selection: DiffSelection | null) => {
    setCurrentSelection(selection)
  }, [])

  const handleCitationClick = useCallback((citation: DiffCitation) => {
    // Switch to the cited file if needed
    if (citation.path !== selectedFile) {
      setSelectedFile(citation.path)
    }
    // Highlight the cited lines
    setHighlightedLines({
      start: citation.startLine,
      end: citation.endLine,
      side: citation.side,
    })
  }, [selectedFile])

  const loadPR = async () => {
    if (!prUrl.trim()) return

    setLoading(true)
    setError(null)

    try {
      const response = await fetch('/api/pr/load', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prUrl }),
      })

      if (!response.ok) {
        const data = await response.json()
        throw new Error(data.detail || 'Failed to load PR')
      }

      const data: PRInfo = await response.json()
      setPrInfo(data)
      setSelectedFile(data.files[0]?.path || null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }

  // Auto-review state
  const [issues, setIssues] = useState<import('./types').ReviewIssue[]>([])
  const [reviewing, setReviewing] = useState(false)

  // Trigger auto-review when PR is loaded
  useEffect(() => {
    if (prInfo?.reviewId) {
      const runReview = async () => {
        setReviewing(true)
        try {
          // In a real app we might cache this or check if it's already done
          const response = await fetch(`/api/diff/review?reviewId=${prInfo.reviewId}`, {
            method: 'POST'
          })
          if (response.ok) {
            const data = await response.json()
            setIssues(data.issues)
          }
        } catch (e) {
          console.error("Failed to run auto-review", e)
        } finally {
          setReviewing(false)
        }
      }

      runReview()
    } else {
      setIssues([])
    }
  }, [prInfo?.reviewId])

  // Focused issue state for auto-scrolling
  const [focusedIssue, setFocusedIssue] = useState<import('./types').ReviewIssue | null>(null)

  const handleIssueClick = useCallback((issue: import('./types').ReviewIssue) => {
    const citation = issue.citations[0]
    if (citation) {
      handleCitationClick(citation)
      // Clone to force update if clicking same issue
      setFocusedIssue({ ...issue })
    }
  }, [handleCitationClick])

  // Filter issues
  const bugIssues = issues.filter(i => i.category === 'bug')
  const flagIssues = issues.filter(i => i.category === 'investigation' || i.category === 'informational' || !i.category)

  return (
    <div className="app-container">
      {/* File List Panel */}
      <div className="panel file-list-panel">
        <div className="panel-header">
          <img src="/asyncfunc.png" alt="Logo" className="header-logo" />
          <span className="shimmer-text">ASYNCREVIEW</span>
        </div>
        <div className="url-input-container">
          <input
            type="text"
            className="url-input"
            placeholder="Enter GitHub PR or GitLab MR URL..."
            value={prUrl}
            onChange={e => setPrUrl(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && loadPR()}
          />
          <button className="btn btn-primary" onClick={loadPR} disabled={loading}
            style={{ marginTop: 8, width: '100%' }}>
            {loading ? 'Loading...' : 'Load PR'}
          </button>
        </div>
        <div className="panel-content">
          {error && <div style={{ color: 'var(--error)', padding: 8 }}>{error}</div>}
          {prInfo?.files.map(file => (
            <div
              key={file.path}
              className={`file-item ${selectedFile === file.path ? 'selected' : ''}`}
              onClick={() => setSelectedFile(file.path)}
            >
              <span className={`file-status ${file.status}`}>{file.status[0]}</span>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{file.path}</span>
            </div>
          ))}
        </div>
        <div className="panel-footer">
          Built by Sheing Ng • All rights reserved.
        </div>
      </div>

      {/* Diff Viewer Panel */}
      <div className="panel diff-panel">
        {!prInfo && (
          <div className="panel-header">
            Diff Viewer
          </div>
        )}
        <div className="panel-content" style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
          <div style={{ flex: `0 0 ${prSummaryHeight}px`, maxHeight: '80vh', overflow: 'auto' }}>
            {loading && !prInfo ? <PRSummarySkeleton /> : <PRSummary prInfo={prInfo} />}
          </div>
          {/* Vertical Resizer */}
          <div
            className={`resizer-vertical ${isResizingSummary ? 'resizing' : ''}`}
            onMouseDown={startResizingSummary}
          />
          <div style={{ flex: 1, minHeight: 0 }}>
            <DiffViewer
              prInfo={prInfo}
              selectedFilePath={selectedFile}
              onSelectionChange={handleSelectionChange}
              highlightedLines={highlightedLines}
              issues={issues}
              focusedIssue={focusedIssue}
            />
            {reviewing && (
              <div style={{
                position: 'absolute',
                bottom: 20,
                right: 20,
                backgroundColor: 'var(--bg-tertiary)',
                padding: '8px 12px',
                borderRadius: '6px',
                boxShadow: 'var(--shadow-md)',
                fontSize: '12px',
                color: 'var(--text-secondary)',
                border: '1px solid var(--border)',
                zIndex: 100
              }}>
                Running automated review...
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Resizer Handle */}
      <div
        className={`resizer ${isResizing ? 'resizing' : ''}`}
        onMouseDown={startResizing}
      />

      {/* Chat Panel */}
      <div
        className="panel chat-panel"
        style={{ width: chatWidth, minWidth: chatWidth, maxWidth: chatWidth }}
      >
        <div className="panel-header" style={{ padding: '0 8px' }}>
          <div style={{ display: 'flex', gap: '4px' }}>
            <button
              onClick={() => setActiveTab('chat')}
              style={{
                background: activeTab === 'chat' ? 'var(--bg-secondary)' : 'transparent',
                border: 'none',
                padding: '8px 12px',
                borderRadius: '6px',
                color: activeTab === 'chat' ? 'var(--text-primary)' : 'var(--text-secondary)',
                fontSize: '13px',
                fontWeight: 500,
                cursor: 'pointer'
              }}
            >
              Code Review
            </button>
            <button
              onClick={() => setActiveTab('flags')}
              style={{
                background: activeTab === 'flags' ? 'var(--bg-secondary)' : 'transparent',
                border: 'none',
                padding: '8px 12px',
                borderRadius: '6px',
                color: activeTab === 'flags' ? 'var(--text-primary)' : 'var(--text-secondary)',
                fontSize: '13px',
                fontWeight: 500,
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: '6px'
              }}
            >
              Flags
              {(flagIssues.length > 0 || (reviewing && activeTab !== 'flags')) && (
                <span style={{
                  fontSize: '10px',
                  background: 'var(--bg-tertiary)',
                  padding: '1px 5px',
                  borderRadius: '10px',
                  color: 'var(--text-secondary)',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '4px'
                }}>
                  {reviewing && (
                    <div style={{
                      width: '8px',
                      height: '8px',
                      border: '1.5px solid currentColor',
                      borderRightColor: 'transparent',
                      borderRadius: '50%',
                      animation: 'spin 1s linear infinite',
                      display: 'inline-block'
                    }} />
                  )}
                  {flagIssues.length}
                </span>
              )}
            </button>
            <button
              onClick={() => setActiveTab('bugs')}
              style={{
                background: activeTab === 'bugs' ? 'var(--bg-secondary)' : 'transparent',
                border: 'none',
                padding: '8px 12px',
                borderRadius: '6px',
                color: activeTab === 'bugs' ? 'var(--text-primary)' : 'var(--text-secondary)',
                fontSize: '13px',
                fontWeight: 500,
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: '6px'
              }}
            >
              Bugs
              {(bugIssues.length > 0) && (
                <span style={{
                  fontSize: '10px',
                  background: '#ef4444',
                  color: 'white',
                  padding: '1px 5px',
                  borderRadius: '10px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '4px'
                }}>
                  {bugIssues.length}
                </span>
              )}
            </button>
          </div>
        </div>

        {activeTab === 'chat' ? (
          <ChatPanel
            reviewId={prInfo?.reviewId || null}
            currentSelection={currentSelection}
            onCitationClick={handleCitationClick}
            disabled={loading}
          />
        ) : activeTab === 'bugs' ? (
          <BugsPanel
            issues={issues}
            onIssueClick={handleIssueClick}
            isLoading={reviewing}
          />
        ) : (
          <FlagsPanel
            issues={flagIssues}
            onIssueClick={handleIssueClick}
            isLoading={reviewing}
          />
        )}

      </div>
    </div>
  )
}

export default App
