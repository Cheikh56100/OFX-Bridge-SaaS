export type TextQuality = { ok: boolean; reason?: 'empty' | 'short' | 'garbled' }

// Décide si le texte extrait d'un relevé est exploitable, ou si l'app doit
// escalader vers l'étape suivante du pipeline (OCR local, puis en dernier
// recours le moteur cloud Gemini). Utilisée après CHAQUE étape :
//   1. extractPdf (PDF.js)      — gratuit, instantané
//   2. ocrPdf (Tesseract.js)    — gratuit, local, plus lent
//   3. extractWithGemini        — payant, cloud, uniquement si 1 et 2 échouent
export function assessPagesText(pagesText: string[]): TextQuality {
  const joined = pagesText.join('\n')
  const stripped = joined.replace(/\s/g, '')

  if (!stripped.length) return { ok: false, reason: 'empty' }
  if (stripped.length < 250) return { ok: false, reason: 'short' }

  // "Illisible" : un OCR cassé produit souvent beaucoup de caractères mais
  // surtout du bruit (glyphes mal mappés, ponctuation aléatoire). On vérifie
  // qu'une proportion raisonnable du texte est composée de lettres/chiffres.
  const letters = (joined.match(/[A-Za-zÀ-ÿ0-9]/g) || []).length
  if (letters / stripped.length < 0.45) return { ok: false, reason: 'garbled' }

  return { ok: true }
}
