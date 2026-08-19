import * as pdfjsLib from 'pdfjs-dist'
import { createWorker } from 'tesseract.js'
// @ts-ignore
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

export type PdfWord = { text: string; x0: number; x1: number; top: number; bottom: number }

// A pdf.js text-content "item" is a whole text run (e.g. "Transaction de carte
// de 117,18 EUR émise par Boucherie Pl Fra MASSY" can be ONE item), not a single
// word, and items are emitted in content-stream order which does not always
// match visual top-to-bottom order (a right-hand amount column can appear in
// the stream after the detail line below it). We split each item into
// individual word tokens (approximating x-position from character offset)
// and later regroup everything by real vertical position, mirroring what
// pdfplumber gives the original Python engine (page.extract_words()).
function itemToWords(item: any, pageHeight: number): PdfWord[] {
  const str = String(item?.str ?? '')
  if (!str.trim()) return []
  const tr = item.transform || [1, 0, 0, 1, 0, 0]
  const startX = Number(tr[4] || 0)
  const height = Math.abs(Number(item.height || Math.abs(tr[3]) || 10))
  const top = pageHeight - Number(tr[5] || 0) - height
  const totalWidth = Number(item.width || 0)
  const charWidth = str.length ? totalWidth / str.length : 0
  const words: PdfWord[] = []
  const re = /\S+/g
  let m: RegExpExecArray | null
  while ((m = re.exec(str))) {
    const x0 = startX + m.index * charWidth
    const x1 = x0 + m[0].length * charWidth
    words.push({ text: m[0], x0, x1, top, bottom: top + height })
  }
  return words
}

function wordsToLines(words: PdfWord[]): string {
  if (!words.length) return ''
  // Reconstruct lines from real word positions (row = words with close top
  // values, ordered left to right), mirroring the original pdfplumber-based
  // engine's group_words_by_row. This is what pages_text depends on: every
  // parser that splits pages_text on '\n' (Wise, myPOS, Shine, the universal
  // fallback, etc.) needs correctly separated, visually-ordered lines.
  const sorted = [...words].sort((a, b) => a.top - b.top || a.x0 - b.x0)
  const rows: PdfWord[][] = []
  const tol = 3.0
  let cur: PdfWord[] = [sorted[0]]
  let curTop = sorted[0].top
  for (let i = 1; i < sorted.length; i++) {
    const w = sorted[i]
    if (Math.abs(w.top - curTop) <= tol) {
      cur.push(w)
    } else {
      rows.push(cur.sort((a, b) => a.x0 - b.x0))
      cur = [w]
      curTop = w.top
    }
  }
  rows.push(cur.sort((a, b) => a.x0 - b.x0))

  return rows.map(row => row.map(w => w.text).join(' ')).join('\n')
}

export async function extractPdf(file: File, onProgress?: (p: number) => void) {
  const data = await file.arrayBuffer()
  const pdf = await pdfjsLib.getDocument({ data }).promise
  const pagesText: string[] = []
  const pagesWords: PdfWord[][] = []

  for (let i = 1; i <= pdf.numPages; i++) {
    const page = await pdf.getPage(i)
    const viewport = page.getViewport({ scale: 1 })
    const content = await page.getTextContent()
    const words: PdfWord[] = []
    for (const item of content.items as any[]) {
      words.push(...itemToWords(item, viewport.height))
    }
    // pdf.js emits text items in PDF content-stream order, which does not
    // always match visual top-to-bottom, left-to-right reading order (e.g. a
    // right-hand amount column can be drawn after the detail line below it).
    // The Python engine's row grouping (group_words_by_row) assumes words
    // already arrive in reading order, matching pdfplumber's extract_words().
    // Without sorting here, that assumption breaks and whole rows -
    // including their amounts - get dropped or merged into the wrong row.
    words.sort((a, b) => a.top - b.top || a.x0 - b.x0)
    pagesWords.push(words)
    pagesText.push(wordsToLines(words))
    onProgress?.(Math.round((i / pdf.numPages) * 100))
  }
  return { pagesText, pagesWords, pdf }
}

export async function ocrPdf(file: File, onProgress?: (p: number) => void) {
  const data = await file.arrayBuffer()
  const pdf = await pdfjsLib.getDocument({ data }).promise
  const worker = await createWorker('fra+eng')
  const pagesText: string[] = []
  const pagesWords: PdfWord[][] = []

  for (let i = 1; i <= pdf.numPages; i++) {
    const page = await pdf.getPage(i)
    const scale = 2.2
    const viewport = page.getViewport({ scale })
    const canvas = document.createElement('canvas')
    canvas.width = Math.ceil(viewport.width)
    canvas.height = Math.ceil(viewport.height)
    const ctx = canvas.getContext('2d')!
    await page.render({ canvasContext: ctx, canvas, viewport }).promise

    const result = await worker.recognize(canvas, {}, { blocks: true })
    const text = result.data.text || ''
    const words: PdfWord[] = []
    // Tesseract.js v7 nests recognition results as blocks -> paragraphs -> lines -> words
    // instead of exposing a flat `words` array on the page.
    for (const block of result.data.blocks || []) {
      for (const paragraph of block.paragraphs || []) {
        for (const line of paragraph.lines || []) {
          for (const w of line.words || []) {
            const value = String(w.text || '').trim()
            if (!value || !w.bbox) continue
            // Convert Tesseract pixel coordinates back to PDF points. This keeps
            // the same x0/top semantics expected by the original bank parsers.
            words.push({
              text: value,
              x0: w.bbox.x0 / scale,
              x1: w.bbox.x1 / scale,
              top: w.bbox.y0 / scale,
              bottom: w.bbox.y1 / scale,
            })
          }
        }
      }
    }
    pagesText.push(text)
    // Sort defensively too: Tesseract emits blocks in the order its layout
    // analysis finds them, not guaranteed to be strict top-to-bottom, and
    // the Python engine's row grouping needs reading order.
    words.sort((a, b) => a.top - b.top || a.x0 - b.x0)
    pagesWords.push(words)
    onProgress?.(Math.round((i / pdf.numPages) * 100))
  }
  await worker.terminate()
  return { pagesText, pagesWords }
}
