import React, { useState, useEffect, useMemo, useCallback } from 'react'
import { MultiFileDiff } from '@pierre/diffs/react'
import { IssuePopover } from './IssuePopover'
import type { FileContents as PierreFileContents } from '@pierre/diffs/react'
import type { SelectedLineRange, DiffLineAnnotation } from '@pierre/diffs'
import type { FileContents, DiffSelection, PRInfo, ReviewIssue } from '../types'

interface DiffViewerProps {
  prInfo: PRInfo | null
  selectedFilePath: string | null
  onSelectionChange?: (selection: DiffSelection | null) => void
  highlightedLines?: { start: number; end: number; side: 'additions' | 'deletions' } | null
  issues?: ReviewIssue[]
  focusedIssue?: ReviewIssue | null
}

export function DiffViewerComponent({
  prInfo,
  selectedFilePath,
  onSelectionChange,
  highlightedLines,
  issues = [],
  focusedIssue,
}: DiffViewerProps) {
  const [oldFile, setOldFile] = useState<FileContents | null>(null)
  const [newFile, setNewFile] = useState<FileContents | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [diffStyle, setDiffStyle] = useState<'split' | 'unified'>('split')

  // Helper to generate unique key for issue element finding
  const getIssueKey = (issue: ReviewIssue) => {
    const citation = issue.citations[0]
    return `${issue.title}-${citation?.startLine || 0}`
  }

  // Auto-scroll and focus effect
  useEffect(() => {
    let checkTimer: any = null
    let attempts = 0
    let lastTop = 0

    if (focusedIssue && !loading && !error) {
      // Hide active popover immediately to verify visual cleanness during scroll
      setActiveIssue(null)

      // Small timeout to allow render to complete
      const timer = setTimeout(() => {
        const key = getIssueKey(focusedIssue)
        const element = document.querySelector(`[data-issue-key="${key}"]`)

        if (element) {
          element.scrollIntoView({ block: 'center', behavior: 'smooth' })

          // Poll for scroll stability
          const checkScroll = () => {
            const rect = element.getBoundingClientRect()
            // Check if position is stable (within 1px) compared to last check
            // We give it a few frames to start scrolling
            if (attempts > 0 && Math.abs(rect.top - lastTop) < 1) {
              // Stable!
              setPopoverPosition({
                top: rect.top,
                left: rect.right + 10,
              })
              setActiveIssue(focusedIssue)
            } else {
              // Not stable yet, or just started
              lastTop = rect.top
              attempts++
              if (attempts < 20) { // Max 2 seconds (20 * 100ms)
                checkTimer = setTimeout(checkScroll, 100)
              } else {
                // Component gave up waiting, show it anyway
                setPopoverPosition({
                  top: rect.top,
                  left: rect.right + 10,
                })
                setActiveIssue(focusedIssue)
              }
            }
          }

          // Start checking
          checkTimer = setTimeout(checkScroll, 100)
        }
      }, 100)

      return () => {
        clearTimeout(timer)
        if (checkTimer) clearTimeout(checkTimer)
      }
    }
  }, [focusedIssue, loading, error])

  // Fetch file contents when selection changes
  useEffect(() => {
    if (!prInfo || !selectedFilePath) {
      setOldFile(null)
      setNewFile(null)
      return
    }

    const fetchFile = async () => {
      setLoading(true)
      setError(null)
      // Clear active issue when switching files to prevent ghost popovers
      setActiveIssue(null)

      try {
        const response = await fetch(
          `/api/file?reviewId=${encodeURIComponent(prInfo.reviewId)}&path=${encodeURIComponent(selectedFilePath)}`
        )

        if (!response.ok) {
          const data = await response.json()
          throw new Error(data.detail || 'Failed to load file')
        }

        const data = await response.json()
        setOldFile(data.oldFile)
        setNewFile(data.newFile)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error')
        setOldFile(null)
        setNewFile(null)
      } finally {
        setLoading(false)
      }
    }

    fetchFile()
  }, [prInfo?.reviewId, selectedFilePath])

  // Convert to @pierre/diffs FileContents format (memoized for reference stability)
  const pierreOldFile = useMemo<PierreFileContents | null>(() => {
    if (!oldFile) return null
    return {
      name: oldFile.name,
      contents: oldFile.contents,
      cacheKey: oldFile.cacheKey,
    }
  }, [oldFile?.name, oldFile?.contents, oldFile?.cacheKey])

  const pierreNewFile = useMemo<PierreFileContents | null>(() => {
    if (!newFile) return null
    return {
      name: newFile.name,
      contents: newFile.contents,
      cacheKey: newFile.cacheKey,
    }
  }, [newFile?.name, newFile?.contents, newFile?.cacheKey])

  // For added files (no old file), create an empty old file
  const effectiveOldFile = useMemo(() =>
    pierreOldFile || { name: selectedFilePath || '', contents: '' },
    [pierreOldFile, selectedFilePath])

  // For deleted files (no new file), create an empty new file
  const effectiveNewFile = useMemo(() =>
    pierreNewFile || { name: selectedFilePath || '', contents: '' },
    [pierreNewFile, selectedFilePath])

  // Handle line selection
  const handleLineSelected = useCallback((range: SelectedLineRange | null) => {
    if (!range || !selectedFilePath) {
      onSelectionChange?.(null)
      return
    }

    onSelectionChange?.({
      path: selectedFilePath,
      side: range.side || 'unified',
      startLine: range.start,
      endLine: range.end,
      mode: range.start === range.end ? 'single-line' : 'range',
    })
  }, [selectedFilePath, onSelectionChange])

  // Convert highlighted lines to @pierre/diffs selectedLines format with validation
  const selectedLines = useMemo<SelectedLineRange | null>(() => {
    if (!highlightedLines) return null

    // Safety check: Get line counts
    const oldLineCount = oldFile ? oldFile.contents.split('\n').length : 0
    const newLineCount = newFile ? newFile.contents.split('\n').length : 0

    const { start, end, side: rawSide } = highlightedLines

    // Normalize range
    const startLine = Math.max(1, Math.min(start, end))
    const endLine = Math.max(1, Math.max(start, end))

    let side: 'additions' | 'deletions' = 'additions' // Default
    let isValid = false

    // Normalize side
    const sideLower = String(rawSide).toLowerCase()

    if (sideLower === 'unified' || sideLower === 'null' || sideLower === 'undefined') {
      // Guess side based on line validity
      if (startLine <= newLineCount) {
        side = 'additions'
        isValid = true
      } else if (startLine <= oldLineCount) {
        side = 'deletions'
        isValid = true
      }
    } else if (sideLower === 'deletions' || sideLower === 'left' || sideLower.includes('del')) {
      side = 'deletions'
      if (startLine <= oldLineCount) isValid = true
    } else {
      // Default to additions (right)
      side = 'additions'
      if (startLine <= newLineCount) isValid = true
    }

    if (!isValid) return null

    return {
      start: startLine,
      end: endLine,
      side: side,
    }
  }, [highlightedLines, oldFile, newFile])

  // Process issues into annotations with validation
  const lineAnnotations = useMemo<DiffLineAnnotation<ReviewIssue>[]>(() => {
    if (!selectedFilePath || !issues.length) return []

    const annotations: DiffLineAnnotation<ReviewIssue>[] = []

    // Safety check: Get line counts
    const oldLineCount = oldFile ? oldFile.contents.split('\n').length : 0
    const newLineCount = newFile ? newFile.contents.split('\n').length : 0

    issues.forEach(issue => {
      issue.citations.forEach(citation => {
        if (citation.path === selectedFilePath) {
          let valid = false
          // Validate line number based on side with robust type checking
          const s = citation.side as any
          if (s === 'additions' || s === 'right') {
            if (citation.startLine > 0 && citation.startLine <= newLineCount) valid = true
          } else if (s === 'deletions' || s === 'left') {
            if (citation.startLine > 0 && citation.startLine <= oldLineCount) valid = true
          } else {
            // Unified/context handling
            // Unified/context - usually maps to right side for additions or left for deletions.
            // If uncertain, we might default to right side if it exists there, or safer: check both?
            // Pierre expects 'additions' or 'deletions' for side usually, but 'unified' might be passed as generic.
            // Let's assume if side is missing or 'unified', we try to guess or just validate existence.
            // For now, let's map 'unified' to 'additions' if line exists there, else 'deletions'.
            if (citation.startLine > 0 && citation.startLine <= newLineCount) {
              citation.side = 'additions'
              valid = true
            } else if (citation.startLine > 0 && citation.startLine <= oldLineCount) {
              citation.side = 'deletions'
              valid = true
            }
          }

          if (valid) {
            annotations.push({
              lineNumber: citation.startLine,
              side: (citation.side as any) === 'unified' ? 'additions' : citation.side,
              metadata: issue
            })
          }
        }
      })
    })

    return annotations
  }, [selectedFilePath, issues, oldFile, newFile])

  // Popover state
  const [activeIssue, setActiveIssue] = useState<ReviewIssue | null>(null)
  const [popoverPosition, setPopoverPosition] = useState<{ top: number; left: number } | null>(null)

  const diffOptions = useMemo(() => ({
    theme: { dark: 'pierre-dark', light: 'pierre-light' },
    diffStyle,
    enableLineSelection: true,
    onLineSelected: handleLineSelected,
  }), [diffStyle, handleLineSelected])

  const handleAnnotationClick = useCallback((event: React.MouseEvent, issue: ReviewIssue) => {
    event.stopPropagation()
    const rect = event.currentTarget.getBoundingClientRect()
    // Position popover to the right of the flag, or below if space is tight
    setPopoverPosition({
      top: rect.top,
      left: rect.right + 10,
    })
    setActiveIssue(issue)
  }, [])

  const renderAnnotation = useCallback((annotation: DiffLineAnnotation<ReviewIssue>) => {
    const issue = annotation.metadata
    const issueKey = `${issue.title}-${issue.citations[0]?.startLine || 0}`

    return (
      <div
        style={{ marginLeft: '0.5rem', display: 'flex', alignItems: 'center', cursor: 'pointer' }}
        title={issue.title}
        onClick={(e) => handleAnnotationClick(e, issue)}
        data-issue-key={issueKey}
      >
        {issue.category === 'bug' ? (
          // Ladybug icon for bugs
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <ellipse cx="12" cy="14" rx="7" ry="8" fill={
              (issue.severity === 'high' || issue.severity === 'critical') ? '#ef4444' : '#eab308'
            } />
            <circle cx="12" cy="6" r="3" fill={
              (issue.severity === 'high' || issue.severity === 'critical') ? '#ef4444' : '#eab308'
            } />
            <line x1="12" y1="6" x2="12" y2="22" stroke="#18181b" strokeWidth="1.5" />
            <circle cx="9" cy="11" r="1.5" fill="#18181b" />
            <circle cx="15" cy="11" r="1.5" fill="#18181b" />
            <circle cx="8" cy="16" r="1.5" fill="#18181b" />
            <circle cx="16" cy="16" r="1.5" fill="#18181b" />
            <circle cx="10" cy="19" r="1.2" fill="#18181b" />
            <circle cx="14" cy="19" r="1.2" fill="#18181b" />
          </svg>
        ) : (
          // Flag icon for flags
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M2 2v12h2V9h8V2H2z" fill={
              (issue.category === 'investigation' || issue.severity === 'high' || issue.severity === 'critical')
                ? '#f59e0b' // Amber/Yellow for Investigate
                : '#a1a1aa' // Gray for Informational
            } />
          </svg>
        )}
      </div>
    )
  }, [handleAnnotationClick])

  if (!prInfo || !selectedFilePath) {
    return (
      <div className="diff-placeholder">
        <img src="/asyncfunc.png" alt="AsyncFunc Logo" className="diff-placeholder-logo" />
        Enter a GitHub PR or GitLab MR URL and start instant Code Review.
      </div>
    )
  }

  if (loading) {
    return <div className="diff-loading">Loading diff...</div>
  }

  if (error) {
    return <div className="diff-error">Error: {error}</div>
  }



  return (
    <div className="diff-viewer" onClick={() => setActiveIssue(null)}>
      <div className="diff-toolbar">
        <button
          className={`btn btn-secondary ${diffStyle === 'split' ? 'active' : ''}`}
          onClick={() => setDiffStyle('split')}
        >
          Split
        </button>
        <button
          className={`btn btn-secondary ${diffStyle === 'unified' ? 'active' : ''}`}
          onClick={() => setDiffStyle('unified')}
        >
          Unified
        </button>
      </div>
      <div className="diff-content">
        <InnerDiffViewer
          oldFile={effectiveOldFile}
          newFile={effectiveNewFile}
          selectedLines={selectedLines}
          lineAnnotations={lineAnnotations}
          renderAnnotation={renderAnnotation}
          diffOptions={diffOptions}
        />
      </div>
      {activeIssue && (
        <IssuePopover
          issue={activeIssue}
          position={popoverPosition}
          onClose={() => setActiveIssue(null)}
        />
      )}
    </div>
  )
}

// Separate component to prevent re-renders of the heavy diff when popover state changes
interface InnerDiffViewerProps {
  oldFile: PierreFileContents
  newFile: PierreFileContents
  selectedLines: SelectedLineRange | null
  lineAnnotations: DiffLineAnnotation<ReviewIssue>[]
  renderAnnotation: (annotation: DiffLineAnnotation<ReviewIssue>) => React.ReactNode
  diffOptions: any
}

const InnerDiffViewer = React.memo(function InnerDiffViewer({
  oldFile,
  newFile,
  selectedLines,
  lineAnnotations,
  renderAnnotation,
  diffOptions
}: InnerDiffViewerProps) {

  // Basic Error Boundary to catch selection crashes
  class SelectionErrorBoundary extends React.Component<
    { children: React.ReactNode; fallback: React.ReactNode; resetKey: any },
    { hasError: boolean }
  > {
    constructor(props: any) {
      super(props)
      this.state = { hasError: false }
    }

    static getDerivedStateFromError(_error: any) {
      return { hasError: true }
    }

    componentDidUpdate(prevProps: any) {
      // Reset error state if the selection (resetKey) changes
      if (prevProps.resetKey !== this.props.resetKey) {
        this.setState({ hasError: false })
      }
    }

    componentDidCatch(_error: any, _errorInfo: any) {
      // console.warn("Caught selection error in DiffViewer:", _error)
    }

    render() {
      if (this.state.hasError) {
        return this.props.fallback
      }
      return this.props.children
    }
  }

  return (
    <SelectionErrorBoundary
      resetKey={selectedLines}
      fallback={
        <MultiFileDiff
          oldFile={oldFile}
          newFile={newFile}
          selectedLines={null} // Render without selection if crashed
          lineAnnotations={lineAnnotations}
          renderAnnotation={renderAnnotation}
          options={diffOptions}
        />
      }
    >
      <MultiFileDiff
        oldFile={oldFile}
        newFile={newFile}
        selectedLines={selectedLines}
        lineAnnotations={lineAnnotations}
        renderAnnotation={renderAnnotation}
        options={diffOptions}
      />
    </SelectionErrorBoundary>
  )
})

export const DiffViewer = React.memo(DiffViewerComponent)
