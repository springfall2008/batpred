import { useEffect, useRef } from 'react'

import './AnnualPage.css'

/** Host Predbat's complete What If tool inside the modern dashboard shell. */
export default function AnnualPage() {
  const frameRef = useRef<HTMLIFrameElement>(null)

  function styleEmbeddedPage() {
    const document = frameRef.current?.contentDocument
    if (!document) return

    document.querySelector('.menu-bar')?.remove()
    document.querySelectorAll<HTMLAnchorElement>('a[href="./annual"]').forEach((link) => {
      link.href = './legacy_annual'
    })
    document.documentElement.classList.toggle('dark-mode', window.document.documentElement.dataset.theme === 'dark')
    document.body.classList.toggle('dark-mode', window.document.documentElement.dataset.theme === 'dark')

    if (!document.getElementById('modern-annual-frame-style')) {
      const style = document.createElement('style')
      style.id = 'modern-annual-frame-style'
      style.textContent = 'body { padding-top: 0 !important; margin: 0.75rem !important; }'
      document.head.append(style)
    }
  }

  useEffect(() => {
    const observer = new MutationObserver(styleEmbeddedPage)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])

  return (
    <section className="annual-page">
      <header className="annual-page-header">
        <h1>What If</h1>
        <p>Model the annual cost, savings and payback for different solar, battery and tariff choices.</p>
      </header>
      <iframe ref={frameRef} className="annual-frame" src="./legacy_annual" title="Predbat What If" onLoad={styleEmbeddedPage} />
    </section>
  )
}
