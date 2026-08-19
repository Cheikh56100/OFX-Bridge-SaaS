export function parseAmount(input:string):number|null{
 let s=String(input).replace(/\s+/g,'').replace(/\u00a0/g,'').replace(/\*/g,'').trim()
 if(/^\d{1,3}(\.\d{3})*,\d{2}$/.test(s)) return Number(s.replace(/\./g,'').replace(',','.'))
 if(/^\d+,\d{2}$/.test(s)) return Number(s.replace(',','.'))
 if(/^\d+\.\d{2}$/.test(s)) return Number(s)
 s=s.replace(/[^\d,.-]/g,'')
 if(s.includes(',') && s.includes('.')) s=s.lastIndexOf(',')>s.lastIndexOf('.')?s.replace(/\./g,'').replace(',','.'):s.replace(/,/g,'')
 else if(s.includes(',')) s=s.replace(',','.')
 const n=Number(s); return Number.isFinite(n)?n:null
}
export function clean(s:string){return s.replace(/\s+/g,' ').trim()}
export function fitId(date:string,label:string,amount:number){
 const input=`${date}${label}${amount.toFixed(2)}`
 let h=0; for(let i=0;i<input.length;i++){h=((h<<5)-h)+input.charCodeAt(i);h|=0}
 return `${Math.abs(h)}-${date}-${Math.round(Math.abs(amount)*100)}`
}
export function dateToOfx(s:string):string{
 const m=s.match(/(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?/); if(!m) return ''
 const y=(m[3]?m[3].length===2?`20${m[3]}`:m[3]:String(new Date().getFullYear()))
 return `${y}${m[2].padStart(2,'0')}${m[1].padStart(2,'0')}`
}
export function fmtMoney(n:number,currency='EUR'){return new Intl.NumberFormat('fr-FR',{style:'currency',currency}).format(n)}
