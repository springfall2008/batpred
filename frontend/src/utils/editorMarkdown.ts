import MarkdownIt from 'markdown-it'

const renderer = new MarkdownIt({ html: false, linkify: true, typographer: true })

/** Render schema help without bundling a second syntax highlighter. */
export function renderMarkdown(markdown: string, inline = true): string {
  return inline ? renderer.renderInline(markdown) : renderer.render(markdown)
}
