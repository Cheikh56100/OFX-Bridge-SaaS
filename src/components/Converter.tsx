import React, { useMemo, useState } from 'react'
import {
  ArrowDownToLine,
  BarChart3,
  CheckCircle2,
  FileSpreadsheet,
  FileText,
  Lock,
  Plus,
  ScanLine,
  Search,
  ShieldCheck,
  Sparkles,
  Trash2,
  UploadCloud,
  X
} from 'lucide-react'
import { extractPdf, ocrPdf } from '../lib/pdf'
import { extractWithGemini } from '../lib/gemini'
import { assessPagesText } from '../lib/textQuality'
import { parseStatement, generateOfxFromEngine } from '../lib/parser'
import { downloadExcel } from '../lib/excel'
import { fmtMoney } from '../lib/utils'
import { StatementInfo, Transaction } from '../types'

const SUPPORTED_BANKS = [
  { name: 'Qonto' },
  { name: 'Banque Populaire' },
  { name: 'Société Générale' },
  { name: "Caisse d'Épargne" },
  { name: 'Crédit Agricole' },
  { name: 'CIC' },
  { name: 'Crédit Mutuel' },
  { name: 'LCL' },
  { name: 'La Banque Postale' },
  { name: 'CGD (Caixa)' },
  { name: 'MyPOS' },
  { name: 'Shine' },
  { name: 'Ecobank' },
  { name: 'SG Sénégal' },
  { name: 'UBA' },
  { name: 'BNDE' },
  { name: 'Banque Islamique' },
  { name: 'BSIC' },
  { name: 'Bank of Africa' },
  { name: 'NSIA Banque' },
  { name: 'Orabank' },
  { name: 'CBAO' },
  { name: 'Coris Bank' },
]

export function Converter() {
  const [files, setFiles] = useState<File[]>([]); 
  const [active, setActive] = useState(0); 
  const [showBanksModal, setShowBanksModal] = useState(false)
  const [bankSearch, setBankSearch] = useState('')
  const [info, setInfo] = useState<StatementInfo|null>(null); 
  const [txs, setTxs] = useState<Transaction[]>([]); 
  const [status, setStatus] = useState('Prêt'); 
  const [progress, setProgress] = useState(0)
  const [cutoffDate, setCutoffDate] = useState('')

  async function process(file: File) {
    setStatus(`Lecture de ${file.name}`);
    setProgress(5); 
    try {
      const r = await extractPdf(file, p => setProgress(Math.max(5, Math.round(p * .25))));
      let pagesText = r.pagesText;
      let pagesWords = r.pagesWords;

      if (!assessPagesText(pagesText).ok) {
        setStatus('PDF scanné — OCR local en cours…');
        const ocr = await ocrPdf(file, p => setProgress(25 + Math.round(p * .35)));
        pagesText = ocr.pagesText;
        pagesWords = ocr.pagesWords;
      }

      if (!assessPagesText(pagesText).ok) {
        setStatus('Lecture locale insuffisante — moteur de secours IA (Gemini)…');
        try {
          const gem = await extractWithGemini(file, p => setProgress(60 + Math.round(p * .15)));
          if (assessPagesText(gem.pagesText).ok) {
            pagesText = gem.pagesText;
            pagesWords = gem.pagesWords;
          }
        } catch (gemErr: any) {
          console.warn('Moteur de secours Gemini indisponible:', gemErr?.message || gemErr);
          setStatus("Moteur de secours indisponible — poursuite avec le texte local…");
        }
      }

      setStatus('Analyse bancaire — moteur complet…'); 
      setProgress(78); 
      const parsed = await parseStatement(pagesText, pagesWords); 
      setInfo(parsed.info); 
      setTxs(parsed.transactions); 
      setProgress(100); 
      setStatus(`${parsed.transactions.length} transactions détectées — ${parsed.info.bank}`);
    } catch(e: any) {
      setStatus(e?.message || 'Erreur de traitement');
      setProgress(0)
    }
  }

  function onFiles(list: FileList | null) {
    if (!list?.length) return; 
    const arr = Array.from(list).filter(f => f.type === 'application/pdf' || f.name.toLowerCase().endsWith('.pdf')); 
    setFiles(arr);
    setActive(0);
    if (arr[0]) process(arr[0])
  }

  const totals = useMemo(() => ({
    debit: txs.filter(t => t.type === 'DEBIT').reduce((s, t) => s + t.amount, 0),
    credit: txs.filter(t => t.type === 'CREDIT').reduce((s, t) => s + t.amount, 0),
  }), [txs])

  const filteredBanks = useMemo(() => {
    const query = bankSearch.trim().toLowerCase()
    if (!query) return SUPPORTED_BANKS
    return SUPPORTED_BANKS.filter(bank =>
      bank.name.toLowerCase().includes(query)
    )
  }, [bankSearch])

  function update(i: number, k: keyof Transaction, v: string) {
    setTxs(a => a.map((t, idx) => idx === i ? { ...t, [k]: k === 'amount' ? Number(v) : v } : t))
  }

  function removeRow(i: number) {
    setTxs(a => a.filter((_, idx) => idx !== i))
  }

  function removeBefore() {
    if (!cutoffDate) return;
    const cutoff = cutoffDate.replace(/-/g, '');
    setTxs(a => a.filter(t => t.date >= cutoff))
  }

  function removeFrom() {
    if (!cutoffDate) return;
    const cutoff = cutoffDate.replace(/-/g, '');
    setTxs(a => a.filter(t => t.date < cutoff))
  }

  function updateIban(v: string) {
    setInfo(i => i ? { ...i, iban: v } : i)
  }

  async function exportOfx() {
    if (!info) return;
    const ofx = await generateOfxFromEngine(info, txs);
    const blob = new Blob([ofx], { type: 'application/x-ofx' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${files[active]?.name.replace(/\.pdf$/i, '') || 'releve'}.ofx`;
    a.click();
    URL.revokeObjectURL(url)
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="logo"><Lock size={19}/></div>
          <div><b>OFX Bridge</b><span>Banking workspace</span></div>
        </div>
        <nav>
          <button className="nav active"><BarChart3 size={18}/>Vue d’ensemble</button>
        </nav>
        <div className="sideBottom">
          <button
            className="banksTrigger"
            onClick={() => setShowBanksModal(true)}
            type="button"
          >
            <div className="banksTriggerIcon">
              <ShieldCheck size={16} />
            </div>

            <div className="banksTriggerText">
              <strong>{SUPPORTED_BANKS.length} banques prises en charge</strong>
              <span>Voir la liste</span>
            </div>
          </button>

          <div className="secure">
            <ShieldCheck size={17}/>
            <span>
              Traitement local d'abord
              <br />
              <small>
                Envoi cloud (Gemini) seulement en dernier recours
              </small>
            </span>
          </div>
        </div>
      </aside>

      <main>
        <header>
          <div>
            <div className="eyebrow"><Sparkles size={14}/>Workspace financier</div>
            <h1>Vos relevés, transformés en données prêtes à l’emploi.</h1>
            <p>Importez vos PDF, vérifiez les transactions et exportez en OFX ou Excel.</p>
          </div>
          <div className="headerActions">
            <div className="privacyPill"><ShieldCheck size={15}/>Local d'abord</div>
          </div>
        </header>

        <section className="heroGrid">
          <div className="drop" onClick={() => document.getElementById('pdfInput')?.click()} onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); onFiles(e.dataTransfer.files) }}>
            <input id="pdfInput" hidden type="file" accept="application/pdf" multiple onChange={e => onFiles(e.target.files)}/>
            <div className="dropIcon"><UploadCloud size={28}/></div>
            <h2>Déposez vos relevés PDF</h2>
            <p>PDF natifs ou scans. OCR gratuit dans votre navigateur, secours IA cloud si besoin.</p>
            <button className="primary"><Plus size={17}/>Importer des PDF</button>
            <div className="dropMeta">
              <span><CheckCircle2 size={14}/>PDF texte</span>
              <span><ScanLine size={14}/>OCR scan</span>
              <span><ShieldCheck size={14}/>Local</span>
            </div>
          </div>

          <div className="pipeline">
            <div className="cardHead"><span>Pipeline</span><span className="live"><i/>EN DIRECT</span></div>
            <Step n="01" title="Lecture PDF" desc="PDF.js détecte le texte existant" done={progress > 20}/>
            <Step n="02" title="OCR local" desc="Tesseract.js pour les scans" done={progress > 55}/>
            <Step n="03" title="IA de secours" desc="Gemini 1.5 Flash si besoin" done={progress > 75}/>
            <Step n="04" title="Parsing bancaire" desc="Détection + normalisation" done={progress === 100}/>
            <Step n="05" title="Export" desc="OFX ou journal Excel" done={false}/>
            <div className="progress"><span style={{ width: `${progress}%` }}/></div>
            <small className="status">{status}</small>
          </div>
        </section>

        {files.length > 0 && (
          <section className="workspace">
            <div className="tabs">
              {files.map((f, i) => (
                <button className={i === active ? 'tab active' : 'tab'} key={f.name} onClick={() => { setActive(i); process(f) }}>
                  <FileText size={16}/>{f.name}<X size={14}/>
                </button>
              ))}
            </div>
            {info && (
              <>
                <div className="summary">
                  <div className="bankCard">
                    <div className="bankDetectedName">
  <small>BANQUE DÉTECTÉE</small>
  <strong>{info.bank}</strong>
</div>
                    <div>
                      <small>BANQUE DÉTECTÉE</small>
                      <strong>{info.bank}</strong>
                      <input
                        className="ibanInput"
                        value={info.iban || ''}
                        placeholder="IBAN non détecté"
                        onChange={e => updateIban(e.target.value)}
                      />
                    </div>
                    <div className="confidence">{info.confidence}%<small>confiance</small></div>
                  </div>
                  <Metric label="Débits" value={fmtMoney(totals.debit, info.currency)}/>
                  <Metric label="Crédits" value={fmtMoney(totals.credit, info.currency)}/>
                  <Metric label="Transactions" value={String(txs.length)}/>
                </div>

                <div className="editor">
                  <div className="editorHead">
                    <div>
                      <h2>Transactions</h2>
                      <p>Vérifiez les lignes avant de générer le fichier comptable.</p>
                    </div>
                    <div className="cutoffTools">
                      <input
                        type="date"
                        value={cutoffDate}
                        onChange={e => setCutoffDate(e.target.value)}
                      />
                      <button className="ghost" onClick={removeBefore}>Garder à partir de cette date</button>
                      <button className="ghost" onClick={removeFrom}>Garder avant cette date</button>
                    </div>
                    <div className="actions">
                      <button className="secondary" onClick={() => info && downloadExcel(info, txs, `${files[active]?.name.replace(/\.pdf$/i, '') || 'journal'}.xlsx`)}>
                        <FileSpreadsheet size={16}/>Excel
                      </button>
                      <button className="primary" onClick={exportOfx}>
                        <ArrowDownToLine size={16}/>Télécharger OFX
                      </button>
                    </div>
                  </div>

                  <div className="tableWrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Date</th>
                          <th>Type</th>
                          <th>Libellé</th>
                          <th>Mémo</th>
                          <th className="right">Montant</th>
                          <th></th>
                        </tr>
                      </thead>
                      <tbody>
                        {txs.map((t, i) => (
                          <tr key={t.fitid}>
                            <td>
                              <input value={`${t.date.slice(6,8)}/${t.date.slice(4,6)}/${t.date.slice(0,4)}`} onChange={e => {
                                const p = e.target.value.split('/');
                                if (p.length === 3) update(i, 'date', `${p[2]}${p[1].padStart(2,'0')}${p[0].padStart(2,'0')}`);
                              }}/>
                            </td>
                            <td>
                              <select value={t.type} onChange={e => update(i, 'type', e.target.value)}>
                                <option>DEBIT</option>
                                <option>CREDIT</option>
                              </select>
                            </td>
                            <td><input value={t.name} onChange={e => update(i, 'name', e.target.value)}/></td>
                            <td><input value={t.memo} onChange={e => update(i, 'memo', e.target.value)}/></td>
                            <td className="right">
                              <input className={`amount ${t.amount >= 0 ? 'positive' : 'negative'}`} value={t.amount} onChange={e => update(i, 'amount', e.target.value)}/>
                            </td>
                            <td className="right">
                              <button className="iconBtn" title="Supprimer cette ligne" onClick={() => removeRow(i)}>
                                <Trash2 size={14}/>
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {!txs.length && <div className="empty">Aucune transaction fiable détectée. Essayez un PDF plus net ou vérifiez le texte OCR.</div>}
                  </div>
                </div>
              </>
            )}
          </section>
        )}

        {showBanksModal && (
          <div
            className="modalBack banksModalBack"
            onMouseDown={() => setShowBanksModal(false)}
          >
            <div
              className="modal banksModal"
              onMouseDown={e => e.stopPropagation()}
              role="dialog"
              aria-modal="true"
              aria-labelledby="banks-modal-title"
            >
              <button
                className="close"
                onClick={() => setShowBanksModal(false)}
                aria-label="Fermer"
                type="button"
              >
                <X size={20} />
              </button>

              <div className="banksModalHeader">
                <div className="banksModalIcon">
                  <ShieldCheck size={20} />
                </div>

                <div>
                  <h2 id="banks-modal-title">
                    Banques prises en charge
                  </h2>

                  <p>
                    OFX Bridge est compatible avec {SUPPORTED_BANKS.length} banques.
                  </p>
                </div>
              </div>

              <p className="banksModalIntro">
                Retrouvez les établissements actuellement reconnus par le moteur
                de parsing bancaire.
              </p>

              <div className="bankSearch">
                <Search size={17} />

                <input
                  type="search"
                  value={bankSearch}
                  onChange={e => setBankSearch(e.target.value)}
                  placeholder="Rechercher une banque..."
                  aria-label="Rechercher une banque"
                />

                {bankSearch && (
                  <button
                    type="button"
                    className="bankSearchClear"
                    onClick={() => setBankSearch('')}
                    aria-label="Effacer la recherche"
                  >
                    <X size={14} />
                  </button>
                )}
              </div>

              <div className="banksList">
                {filteredBanks.map((bank, index) => (
                  <div className="bankItem">
  <div className="bankItemContent">
    <span className="bankItemNumber">
      {String(index + 1).padStart(2, '0')}
    </span>

    <strong>{bank.name}</strong>
  </div>
</div>
                ))}
              </div>

              {!filteredBanks.length && (
                <div className="banksEmpty">
                  <Search size={20} />
                  <strong>Aucune banque trouvée</strong>
                  <span>
                    Essayez avec un autre nom.
                  </span>
                </div>
              )}

              <div className="banksModalFooter">
                <span>
                  {filteredBanks.length} banque
                  {filteredBanks.length > 1 ? 's' : ''} affichée
                  {filteredBanks.length > 1 ? 's' : ''}
                </span>

                <button
                  type="button"
                  className="secondary"
                  onClick={() => setShowBanksModal(false)}
                >
                  Fermer
                </button>
              </div>
            </div>
          </div>
        )}

        <footer>
          OFX Bridge · React + Vite · OCR local en priorité (<a href="https://tesseract.projectnaptha.com/" target="_blank" rel="noreferrer">Tesseract.js</a>), secours cloud Gemini si le PDF reste illisible
        </footer>
      </main>
    </div>
  )
}

function Step({ n, title, desc, done }: { n: string; title: string; desc: string; done: boolean }) {
  return (
    <div className="step">
      <div className={done ? 'stepNum done' : 'stepNum'}>{done ? <CheckCircle2 size={15}/> : n}</div>
      <div><b>{title}</b><span>{desc}</span></div>
    </div>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <small>{label}</small>
      <strong>{value}</strong>
    </div>
  )
}
