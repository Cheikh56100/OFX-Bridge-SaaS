import * as XLSX from 'xlsx'
import {StatementInfo,Transaction} from '../types'

const CODE_JOURNAL = 'BQ'
const COMPTE_CONTREPARTIE = 471000 // compte d'attente (tiers)
const COMPTE_BANQUE = 512000        // compte banque

// Les dates transaction sont au format OFX AAAAMMJJ -> on les remet en JJ/MM/AAAA
function toDDMMYYYY(ofxDate: string): string {
  if (/^\d{8}$/.test(ofxDate)) {
    return `${ofxDate.slice(6, 8)}/${ofxDate.slice(4, 6)}/${ofxDate.slice(0, 4)}`
  }
  return ofxDate
}

export function downloadExcel(info: StatementInfo, txs: Transaction[], filename: string) {
  const header = ['CODE JOURNAL', 'DATE', 'COMPTE', 'INTITULE', 'PIECE1', 'PIECE2', 'DEBIT', 'CREDIT']
  const rows: any[][] = [header]

  txs.forEach(t => {
    const date = toDDMMYYYY(t.date)
    const montant = Math.round(Math.abs(t.amount) * 100) / 100
    const isDebit = t.type === 'DEBIT'

    // Ligne 1 : contrepartie (471000)
    rows.push([
      CODE_JOURNAL, date, COMPTE_CONTREPARTIE, t.name, '', '',
      isDebit ? montant : '', isDebit ? '' : montant
    ])
    // Ligne 2 : banque (512000), sens inverse de la contrepartie
    rows.push([
      CODE_JOURNAL, date, COMPTE_BANQUE, t.name, '', '',
      isDebit ? '' : montant, isDebit ? montant : ''
    ])
  })

  const firstDataRow = 2
  const lastDataRow = rows.length

  // Ligne de total avec formules SUM
  rows.push(['', '', '', 'TOTAL', '', '', '', ''])
  const totalRow = rows.length

  const wb = XLSX.utils.book_new()
  const ws = XLSX.utils.aoa_to_sheet(rows)
  ws[`G${totalRow}`] = { t: 'n', f: `SUM(G${firstDataRow}:G${lastDataRow})` }
  ws[`H${totalRow}`] = { t: 'n', f: `SUM(H${firstDataRow}:H${lastDataRow})` }
  ws['!cols'] = [
    { wch: 12 }, { wch: 12 }, { wch: 10 }, { wch: 32 },
    { wch: 10 }, { wch: 10 }, { wch: 12 }, { wch: 12 }
  ]

  XLSX.utils.book_append_sheet(wb, ws, 'Journal BQ')
  XLSX.writeFile(wb, filename)
}
