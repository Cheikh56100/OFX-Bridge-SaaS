import {StatementInfo,Transaction} from '../types'
export function generateOfx(info:StatementInfo,txs:Transaction[],target='QUADRA'){
 const start=info.periodStart||new Date().toISOString().slice(0,10).replaceAll('-',''); const end=info.periodEnd||start
 const balance=(info.balance??txs.reduce((s,t)=>s+(t.type==='CREDIT'?t.amount:-t.amount),0)).toFixed(2)
 const bankid=info.bankCode||'OFXBRIDGE'
 const rows=txs.map(t=>`<STMTTRN><TRNTYPE>${t.type}</TRNTYPE><DTPOSTED>${t.date}000000</DTPOSTED><TRNAMT>${t.type==='DEBIT'?'-':''}${t.amount.toFixed(2)}</TRNAMT><FITID>${t.fitid}</FITID><NAME>${esc(t.name)}</NAME>${t.memo?`<MEMO>${esc(t.memo)}</MEMO>`:''}</STMTTRN>`).join('')
 return `OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:UTF-8\nCHARSET:1252\nCOMPRESSION:NONE\nOLDFILEUID:NONE\nNEWFILEUID:NONE\n\n<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS><DTSERVER>${new Date().toISOString().replace(/[-:]/g,'').replace(/\.\d{3}Z/,'')}</DTSERVER><LANGUAGE>FRA</LANGUAGE></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>${info.currency}</CURDEF><BANKACCTFROM><BANKID>${bankid}</BANKID><ACCTID>${info.iban||'000000'}</ACCTID><ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTFROM><BANKTRANLIST><DTSTART>${start}000000</DTSTART><DTEND>${end}000000</DTEND>${rows}</BANKTRANLIST><LEDGERBAL><BALAMT>${balance}</BALAMT><DTASOF>${end}000000</DTASOF></LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>`
}
function esc(s:string){return s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')}
