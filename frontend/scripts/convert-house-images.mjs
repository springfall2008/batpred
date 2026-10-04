import { readdir, stat } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import sharp from 'sharp'

/** Generate WebP siblings while retaining the PNG masters and their alpha channel. */
export async function convertHouseImages(directory) {
  const converter = await stat(fileURLToPath(import.meta.url))
  const results = []
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (!entry.isFile() || !/\.png$/i.test(entry.name)) continue
    const source = path.join(directory, entry.name)
    const destination = source.replace(/\.png$/i, '.webp')
    const original = await stat(source)
    const existing = await stat(destination).catch(error => {
      if (error.code === 'ENOENT') return null
      throw error
    })
    if (existing && existing.mtimeMs >= Math.max(original.mtimeMs, converter.mtimeMs)) {
      results.push({ name: entry.name, skipped: true })
      continue
    }
    const output = await sharp(source).webp({ quality: 88, alphaQuality: 100, effort: 6 }).toFile(destination)
    results.push({ name: entry.name, skipped: false, before: original.size, after: output.size })
  }
  return results
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  const directory = fileURLToPath(new URL('../src/assets/house/', import.meta.url))
  const results = await convertHouseImages(directory)
  for (const result of results.filter(result => !result.skipped)) {
    console.log(`${result.name}: ${(result.before / 1024).toFixed(0)} KB → ${(result.after / 1024).toFixed(0)} KB`)
  }
  console.log(`House images: ${results.filter(result => !result.skipped).length} converted, ${results.filter(result => result.skipped).length} unchanged.`)
}
