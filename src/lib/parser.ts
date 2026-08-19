import { PdfWord } from './pdf'
import { StatementInfo, Transaction } from '../types'

export type EngineResult = { bank: string; info: Record<string, any>; transactions: Transaction[] }

let worker: Worker | null = null
let requestId = 0
const pending = new Map<number, { resolve: (v: any) => void; reject: (e: any) => void }>()

function getWorker() {
  if (worker) return worker
  worker = new Worker('/parserWorker.js')
  worker.onmessage = (event: MessageEvent) => {
    const id = event.data?.__id
    const p = id ? pending.get(id) : undefined
    if (!p) return
    pending.delete(id)
    if (event.data?.error) p.reject(new Error(event.data.error))
    else p.resolve(event.data)
  }
  return worker
}

function callEngine(payload: any): Promise<any> {
  const id = ++requestId
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject })
    getWorker().postMessage({ ...payload, __id: id })
  })
}

// A simpler one-at-a-time worker call. The parser engine is CPU-heavy, so
// serializing calls avoids competing Pyodide runtimes for multiple PDFs.
let chain = Promise.resolve()
function queued(payload: any) {
  const result = chain.then(() => callEngine(payload))
  chain = result.then(() => undefined, () => undefined)
  return result
}

export async function parseStatement(pagesText: string[], pagesWords: PdfWord[][]): Promise<{ info: StatementInfo; transactions: Transaction[]; bankCode: string }> {
  const pageWords = pagesWords
  const result: EngineResult = await queued({ action: 'parse', pages_text: pagesText, pages_words: pageWords })
  const raw = result.info || {}
  const bankCode = result.bank || 'UNIVERSAL'
  const labels: Record<string, string> = {
    QONTO:'Qonto', LCL:'LCL (Crédit Lyonnais)', CA:'Crédit Agricole', CE:"Caisse d'Épargne", BP:'Banque Populaire', CIC:'CIC', CM:'Crédit Mutuel', CMB:'Crédit Mutuel de Bretagne', CGD:'Caixa Geral de Depósitos', LBP:'La Banque Postale', SG:'Société Générale', BNP:'BNP Paribas', MYPOS:'myPOS', SHINE:'Shine', CBAO:'CBAO (Sénégal)', ECOBANK:'Ecobank', BCI:'BCI', CORIS:'Coris Bank', UBA:'UBA', ORABANK:'Orabank', BOA:'Bank of Africa', ATB:'Arab Tunisian Bank', SG_AFRIQUE:'Société Générale Afrique', BSIC:'BSIC', BIS:'Banque Islamique du Sénégal', BNDE:'BNDE', NSIA:'NSIA Banque', WISE:'Wise', UNIVERSAL:'Autre banque'
  }
  const transactions = (result.transactions || []).filter(Boolean).map((t: any) => ({
    date: String(t.date || ''), type: t.type === 'CREDIT' ? 'CREDIT' : 'DEBIT', amount: Number(t.amount || 0), name: String(t.name || ''), memo: String(t.memo || ''), fitid: String(t.fitid || '')
  })) as Transaction[]
  const info: StatementInfo = {
    bank: labels[bankCode] || bankCode,
    bankCode,
    iban: raw.iban || '', bic: raw.bic || '',
    currency: raw.currency || ({ QONTO:'EUR',LCL:'EUR',CA:'EUR',CE:'EUR',BP:'EUR',CIC:'EUR',CM:'EUR',CMB:'EUR',CGD:'EUR',LBP:'EUR',SG:'EUR',BNP:'EUR',MYPOS:'EUR',SHINE:'EUR',WISE:'EUR',CBAO:'XOF',ECOBANK:'XOF',BCI:'XOF',CORIS:'XOF',UBA:'XOF',ORABANK:'XOF',BOA:'XOF',SG_AFRIQUE:'XOF',BSIC:'XOF',BIS:'XOF',BNDE:'XOF',NSIA:'XOF',ATB:'TND' } as any)[bankCode] || 'EUR',
    periodStart: raw.period_start ? String(raw.period_start).split('/').reverse().join('') : '',
    periodEnd: raw.period_end ? String(raw.period_end).split('/').reverse().join('') : '',
    balance: raw.balance_close == null ? null : Number(raw.balance_close),
    confidence: transactions.length ? 98 : 45
  }
  return { info, transactions, bankCode }
}

export async function generateOfxFromEngine(info: StatementInfo, transactions: Transaction[], target='quadra') {
  const result = await queued({ action: 'ofx', info: {
    iban: info.iban, period_start: info.periodStart ? `${info.periodStart.slice(6,8)}/${info.periodStart.slice(4,6)}/${info.periodStart.slice(0,4)}` : '', period_end: info.periodEnd ? `${info.periodEnd.slice(6,8)}/${info.periodEnd.slice(4,6)}/${info.periodEnd.slice(0,4)}` : '', balance_close: info.balance || 0, currency: info.currency
  }, transactions, target, currency: info.currency })
  return result.ofx as string
}
