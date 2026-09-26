import { useEffect, useRef } from 'react'

import './AppsPage.css'

/** Host Predbat's structured apps.yaml settings editor inside the modern shell. */
export default function AppsPage() {
  const frameRef = useRef<HTMLIFrameElement>(null)

  function styleEmbeddedPage() {
    const document = frameRef.current?.contentDocument
    if (!document) return

    document.querySelector('.menu-bar')?.remove()
    document.documentElement.classList.toggle('dark-mode', window.document.documentElement.dataset.theme === 'dark')
    document.body.classList.toggle('dark-mode', window.document.documentElement.dataset.theme === 'dark')

    if (!document.getElementById('modern-apps-frame-style')) {
      const style = document.createElement('style')
      style.id = 'modern-apps-frame-style'
      style.textContent = 'body { height: auto !important; margin: 0.75rem !important; padding-top: 0 !important; border: 0 !important; }'
      document.head.append(style)
    }
  }

  useEffect(() => {
    const observer = new MutationObserver(styleEmbeddedPage)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])

  return (
    <section className="apps-page">
      <header className="apps-page-header">
        <h1>Apps</h1>
        <p>Review and update the active values loaded from apps.yaml.</p>
      </header>
      <iframe ref={frameRef} className="apps-frame" src="./legacy_apps" title="Predbat apps.yaml settings" onLoad={styleEmbeddedPage} />
    </section>
  )
}
