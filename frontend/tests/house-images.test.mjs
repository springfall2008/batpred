import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, writeFile, utimes } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import sharp from 'sharp'
import { convertHouseImages } from '../scripts/convert-house-images.mjs'

test('house conversion preserves dimensions, alpha and originals; skips unchanged inputs and refreshes replacements', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'house-images-'))
  try {
    const source = path.join(directory, 'day-house.png')
    const pixels = Buffer.from([200, 120, 60, 0, 80, 100, 150, 128, 255, 255, 255, 255])
    await sharp(pixels, { raw: { width: 3, height: 1, channels: 4 } }).png().toFile(source)
    await writeFile(path.join(directory, 'ignore.txt'), 'not an image')
    const original = await readFile(source)
    const results = await convertHouseImages(directory)
    assert.equal(results.length, 1)
    assert.equal(results[0].skipped, false)
    const output = sharp(path.join(directory, 'day-house.webp'))
    const metadata = await output.metadata()
    assert.equal(metadata.format, 'webp')
    assert.equal(metadata.width, 3)
    assert.equal(metadata.height, 1)
    const decoded = await output.raw().toBuffer()
    assert.deepEqual([decoded[3], decoded[7], decoded[11]], [0, 128, 255])
    assert.deepEqual(await readFile(source), original)
    assert.equal((await convertHouseImages(directory))[0].skipped, true)
    const future = new Date(Date.now() + 2000)
    await utimes(source, future, future)
    assert.equal((await convertHouseImages(directory))[0].skipped, false)
  } finally {
    await rm(directory, { recursive: true, force: true })
  }
})
