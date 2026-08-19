export type TxType = 'CREDIT' | 'DEBIT'
export interface Transaction { date:string; type:TxType; amount:number; name:string; memo:string; fitid:string }
export interface StatementInfo { bank:string; bankCode:string; iban:string; bic:string; currency:string; periodStart:string; periodEnd:string; balance:number|null; confidence:number }
export interface Conversion { id:string; filename:string; bank:string; transactionCount:number; createdAt:string; }
