// Moteur de secours (fallback) — appelé UNIQUEMENT quand PDF.js + Tesseract.js
// (locaux, gratuits) n'ont pas réussi à extraire un texte exploitable.
//
// SÉCURITÉ : cette fonction tourne côté serveur Netlify. La clé Gemini vit
// dans la variable d'environnement GEMINI_API_KEY (à définir dans Netlify ->
// Site settings -> Environment variables). Elle ne doit JAMAIS être préfixée
// par VITE_ (sinon Vite l'intègre en clair dans le bundle JS envoyé au
// navigateur) et ne doit jamais être renvoyée dans une réponse HTTP.
//
// Le navigateur envoie une image de page (PNG en base64) ; cette fonction la
// relaie à Gemini et ne renvoie que le texte transcrit.

const GEMINI_MODEL = 'gemini-1.5-flash'
const GEMINI_URL = `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent`

const PROMPT = `Tu es un moteur de transcription (OCR) pour des relevés bancaires français et africains.
Transcris FIDÈLEMENT tout le texte visible sur cette image, ligne par ligne, dans l'ordre de lecture naturel (haut en bas, gauche à droite).
Règles strictes :
- Une ligne visuelle du document = une ligne de ta réponse.
- Conserve les dates, montants, IBAN, libellés exactement comme écrits. Ne corrige rien, ne traduis rien, n'invente rien, n'ajoute aucune donnée absente de l'image.
- N'ajoute aucun commentaire, aucun titre, aucune explication, aucun markdown : réponds UNIQUEMENT avec le texte transcrit brut.
- Si une zone est vraiment illisible, ignore-la plutôt que d'inventer du texte.`

export default async (req: Request) => {
  if (req.method !== 'POST') {
    return new Response(JSON.stringify({ error: 'Method not allowed' }), { status: 405 })
  }

  const apiKey = process.env.GEMINI_API_KEY
  if (!apiKey) {
    return new Response(
      JSON.stringify({ error: "GEMINI_API_KEY manquante côté serveur (variable d'environnement Netlify)." }),
      { status: 500, headers: { 'Content-Type': 'application/json' } }
    )
  }

  let body: { image?: string; mimeType?: string }
  try {
    body = await req.json()
  } catch {
    return new Response(JSON.stringify({ error: 'Corps de requête JSON invalide' }), { status: 400 })
  }

  const { image, mimeType = 'image/png' } = body
  if (!image) {
    return new Response(JSON.stringify({ error: "Champ 'image' (base64) manquant" }), { status: 400 })
  }

  try {
    const geminiRes = await fetch(`${GEMINI_URL}?key=${apiKey}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        contents: [
          {
            parts: [{ text: PROMPT }, { inline_data: { mime_type: mimeType, data: image } }],
          },
        ],
        generationConfig: { temperature: 0, maxOutputTokens: 8192 },
      }),
    })

    if (!geminiRes.ok) {
      const errText = await geminiRes.text()
      return new Response(
        JSON.stringify({ error: `Gemini API error (${geminiRes.status}): ${errText.slice(0, 500)}` }),
        { status: 502, headers: { 'Content-Type': 'application/json' } }
      )
    }

    const data: any = await geminiRes.json()
    const text =
      data?.candidates?.[0]?.content?.parts?.map((p: any) => p.text || '').join('\n') || ''

    return new Response(JSON.stringify({ text }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  } catch (err: any) {
    return new Response(JSON.stringify({ error: err?.message || 'Erreur réseau vers Gemini' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    })
  }
}
