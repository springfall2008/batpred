declare module '@cubone/react-file-manager' {
  import type { ComponentType, CSSProperties, ReactNode } from 'react'

  export type FileManagerFile = {
    name: string
    isDirectory: boolean
    path: string
    updatedAt?: string
    size?: number
  }

  type FileManagerProps = {
    files: FileManagerFile[]
    isLoading?: boolean
    layout?: 'list' | 'grid'
    height?: string | number
    width?: string | number
    initialPath?: string
    primaryColor?: string
    fontFamily?: string
    className?: string
    style?: CSSProperties
    collapsibleNav?: boolean
    defaultNavExpanded?: boolean
    enableFilePreview?: boolean
    filePreviewComponent?: (file: FileManagerFile) => ReactNode
    formatDate?: (date: string | Date) => string
    onDownload?: (files: FileManagerFile[]) => void
    onFileOpen?: (file: FileManagerFile) => void
    onFolderChange?: (path: string) => void
    onRefresh?: () => void
    onError?: (error: { type: string; message: string }, file: FileManagerFile) => void
    permissions?: Partial<Record<'create' | 'upload' | 'move' | 'copy' | 'rename' | 'download' | 'delete', boolean>>
  }

  export const FileManager: ComponentType<FileManagerProps>
}
