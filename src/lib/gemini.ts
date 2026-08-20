import * as pdfjsLib from 'pdfjs-dist'
// @ts-ignore
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

// Moteur de secours (fallback) cloud. N'est appelé QUE si PDF.js et
// Tesseract.js ont tous les deux échoué à produire un texte exploitable
// (voir assessPagesText). Contrairement aux deux étapes locales, ceci envoie
// une image de chaque page à notre fonction serverless Netlify, qui la
// relaie à Gemini 1.5 Flash. La clé API reste côté serveur (voir
// netlify/functions/gemini-extract.mts) — le navigateur ne la voit jamais.
const GEMINI_ENDPOINT = '/.netlify/functions/gemini-extract'

async function renderPageToBase64Png(page: any, scale = 2.0): Promise<string> {
  const viewport = page.getViewport({ scale })
  const canvas = document.createElement('canvas')
  canvas.width = Math.ceil(viewport.width)
  canvas.height = Math.ceil(viewport.height)
  const ctx = canvas.getContext('2d')!
  await page.render({ canvasContext: ctx, canvas, viewport }).promise
  const dataUrl = canvas.toDataURL('image/png')
  return dataUrl.split(',')[1] || ''
}

export async function extractWithGemini(file: File, onProgress?: (p: number) => void) {
  const data = await file.arrayBuffer()
  const pdf = await pdfjsLib.getDocument({ data }).promise
  const pagesText: string[] = []

  for (let i = 1; i <= pdf.numPages; i++) {
    const page = await pdf.getPage(i)
    const image = await renderPageToBase64Png(page)

    const res = await fetch(GEMINI_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image, mimeType: 'image/png' }),
    })

    if (!res.ok) {
      const errBody = await res.json().catch(() => ({}) as any)
      throw new Error(errBody?.error || `Le moteur de secours Gemini a échoué (page ${i})`)
    }

    const { text } = await res.json()
    pagesText.push(String(text || ''))
    onProgress?.(Math.round((i / pdf.numPages) * 100))
  }

  // Gemini renvoie du texte transcrit, pas des coordonnées de mots : on ne
  // peut pas reconstruire x0/top comme le fait PDF.js/Tesseract. pages_words
  // reste donc vide pour ces pages. detect_bank() et le parseur UNIVERSAL
  // (engine.py) fonctionnent uniquement à partir de pages_text, donc le
  // parsing reste opérationnel ; seuls les parseurs bancaires spécifiques
  // qui s'appuient sur la position des colonnes perdent en précision sur les
  // pages issues de Gemini — un cas déjà rare puisqu'on n'arrive ici que si
  // les deux étapes locales ont échoué.
  const pagesWords = pagesText.map(() => [])
  return { pagesText, pagesWords }
}
