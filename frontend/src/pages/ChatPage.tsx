import { useEffect, useRef } from 'react'

import './ChatPage.css'

/** Host Predbat's complete streaming Chat client inside the modern dashboard shell. */
export default function ChatPage() {
  const frameRef = useRef<HTMLIFrameElement>(null)

  function styleEmbeddedPage() {
    const document = frameRef.current?.contentDocument
    if (!document) return

    document.querySelector('.menu-bar')?.remove()
    document.querySelectorAll<HTMLAnchorElement>('a[href="./log"], a[href="./components"]').forEach((link) => {
      link.target = '_parent'
    })
    document.documentElement.classList.toggle('dark-mode', window.document.documentElement.dataset.theme === 'dark')
    document.body.classList.toggle('dark-mode', window.document.documentElement.dataset.theme === 'dark')

    if (!document.getElementById('modern-chat-frame-style')) {
      const style = document.createElement('style')
      style.id = 'modern-chat-frame-style'
      style.textContent = 'body { height: 100vh !important; margin: 0 !important; padding-top: 0 !important; border: 0 !important; }'
      document.head.append(style)
    }
  }

  useEffect(() => {
    const observer = new MutationObserver(styleEmbeddedPage)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])

  return (
    <section className="chat-page">
      <header className="chat-page-header">
        <h1>Chat</h1>
        <p>Ask Predbat about your system, configuration and plan.</p>
      </header>
      <iframe ref={frameRef} className="chat-frame" src="./legacy_chat" title="Predbat Chat" onLoad={styleEmbeddedPage} />
    </section>
  )
}
