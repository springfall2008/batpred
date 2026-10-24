import { useCallback, useEffect, useState } from 'react'

import { FileManager, type FileManagerFile } from '@cubone/react-file-manager'
import '@cubone/react-file-manager/dist/style.css'

import './BrowsePage.css'

type BrowseResponse = {
  files: FileManagerFile[]
  error?: string
}

function fileUrl(file: FileManagerFile) {
  const separator = file.path.lastIndexOf('/')
  const directory = file.path.slice(1, separator) || '.'
  return `./download?path=${encodeURIComponent(directory)}&file=${encodeURIComponent(file.name)}`
}

function FilePreview({ file }: { file: FileManagerFile }) {
  const [content, setContent] = useState('Loading preview…')

  useEffect(() => {
    let cancelled = false
    fetch(fileUrl(file))
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return response.text()
      })
      .then((text) => { if (!cancelled) setContent(text) })
      .catch((reason) => { if (!cancelled) setContent(`Preview unavailable: ${reason instanceof Error ? reason.message : String(reason)}`) })
    return () => { cancelled = true }
  }, [file])

  return <pre className="browse-file-preview">{content}</pre>
}

/** Browse and download files from Predbat's working directory. */
export default function BrowsePage() {
  const [files, setFiles] = useState<FileManagerFile[]>([])
  const [currentFolder, setCurrentFolder] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const loadFolder = useCallback(async (path = '') => {
    setLoading(true)
    try {
      const response = await fetch(`./api/browse?path=${encodeURIComponent(path.replace(/^\//, ''))}`, { cache: 'no-store' })
      const data = await response.json() as BrowseResponse
      if (!response.ok) throw new Error(data.error ?? `HTTP ${response.status}`)
      setFiles((current) => {
        const next = new Map(current.map((file) => [file.path, file]))
        data.files.forEach((file) => next.set(file.path, file))
        return [...next.values()]
      })
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to browse files')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect -- initial API load
    loadFolder()
  }, [loadFolder])

  function download(selectedFiles: FileManagerFile[]) {
    selectedFiles.filter((file) => !file.isDirectory).forEach((file) => {
      const link = document.createElement('a')
      link.href = fileUrl(file)
      link.download = file.name
      link.click()
    })
  }

  function changeFolder(path: string) {
    setCurrentFolder(path)
    loadFolder(path)
  }

  return (
    <section className="browse-page">
      <header className="browse-page-header">
        <div>
          <h1>Browse</h1>
          <p>View and download files from Predbat's working directory.</p>
        </div>
      </header>

      {error && <div className="browse-page-error" role="alert">Unable to load files: {error}</div>}

      <FileManager
        files={files}
        isLoading={loading}
        layout="list"
        height="calc(100dvh - 8rem)"
        primaryColor="#3b82f6"
        fontFamily="inherit"
        className="browse-file-manager"
        collapsibleNav
        enableFilePreview
        filePreviewComponent={(file) => <FilePreview file={file} />}
        onFolderChange={changeFolder}
        onRefresh={() => loadFolder(currentFolder)}
        onDownload={download}
        permissions={{ create: false, upload: false, move: false, copy: false, rename: false, delete: false, download: true }}
      />
    </section>
  )
}
