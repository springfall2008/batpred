import { FontAwesomeIcon } from '@fortawesome/react-fontawesome'
import { faArrowUpRightFromSquare } from '@fortawesome/free-solid-svg-icons'

import './DocsPage.css'

const DOCS_URL = 'https://springfall2008.github.io/batpred/'

/** Show the Predbat documentation without leaving the dashboard. */
export default function DocsPage() {
  return (
    <section className="docs-page">
      <header className="docs-page-header">
        <div>
          <h1>Documentation</h1>
          <p>Predbat setup, configuration and usage guides.</p>
        </div>
        <a href={DOCS_URL} target="_blank" rel="noreferrer">
          Open in new tab
          <FontAwesomeIcon icon={faArrowUpRightFromSquare} />
        </a>
      </header>

      <iframe
        className="docs-frame"
        src={DOCS_URL}
        title="Predbat documentation"
      />
    </section>
  )
}
