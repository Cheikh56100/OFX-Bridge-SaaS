# OFX Bridge browser parser engine.
# Portability layer: original bank parsing logic is retained from the production
# Python engine and executed client-side through Pyodide. No PDF is uploaded.
import re, hashlib, json, logging, urllib, urllib.request, os
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ofxbridge-browser")
_PYDANTIC_OK = False

class _DummyPDFPlumber:
    pass

try:
    import pdfplumber
except Exception:
    pdfplumber = None
def parse_amount(s):
    s = re.sub(r'\s+', ' ', str(s))  # normalise \n \t \r en espace
    s = s.replace('\xa0','').replace(' ','').replace('*','').strip()
    if re.match(r'^\d{1,3}(\.\d{3})*,\d{2}$', s):
        return float(s.replace('.','').replace(',','.'))
    if re.match(r'^\d+,\d{2}$', s):
        return float(s.replace(',','.'))
    if re.match(r'^\d+\.\d{2}$', s):
        return float(s)
    cleaned = re.sub(r'[^\d,.]', '', s)
    cleaned = cleaned.replace(',', '.')
    try:
        return float(cleaned)
    except ValueError:
        return None

def group_words_by_row(words, tol=3.0):
    if not words:
        return []
    rows, cur, top = [], [words[0]], words[0]['top']
    for w in words[1:]:
        if abs(w['top'] - top) <= tol:
            cur.append(w)
        else:
            rows.append(sorted(cur, key=lambda x: x['x0']))
            cur, top = [w], w['top']
    if cur:
        rows.append(sorted(cur, key=lambda x: x['x0']))
    return sorted(rows, key=lambda r: r[0]['top'])

def clean_label(s):
    return re.sub(r'\s+', ' ', s).strip()

def join_words_with_spaces(words, gap_threshold=4.0):
    """
    Reconstruit un libellé depuis une liste de mots pdfplumber en insérant
    un espace entre deux mots dès que l'écart horizontal (x0_suivant - x1_précédent)
    dépasse gap_threshold points. Cela corrige le problème où pdfplumber
    fusionne des tokens contigus sans espace (ex: SAROUXELAUBER → SA ROUXEL AUBER).
    """
    if not words:
        return ''
    parts = [words[0]['text']]
    for prev, cur in zip(words, words[1:]):
        gap = cur['x0'] - prev['x1']
        sep = ' ' if gap >= gap_threshold else ''
        parts.append(sep + cur['text'])
    return ''.join(parts)

def _is_technical_label(label):
    if not label:
        return True
    if re.match(r'^\d{6}\s+CB\*+\d+\s+\w+\s*$', label):
        return True
    if not re.search(r'[A-Za-zÀ-ÿ]{3,}', label):
        return True
    return False

def _is_human_readable(label):
    if not label:
        return False
    if re.search(r'[A-Z0-9]{15,}', label):
        return False
    if re.match(r'^[\d\s\-\/.,]+$', label):
        return False
    readable_words = [w for w in label.split() if re.search(r'[A-Za-zÀ-ÿ]{2,}', w)
                      and not re.match(r'^\d', w)]
    return len(readable_words) >= 2

def smart_label(main_label, memo_lines):
    label = clean_label(main_label)
    memos = [clean_label(m) for m in memo_lines if clean_label(m)]
    if _is_technical_label(label) and memos:
        for candidate in memos:
            if _is_human_readable(candidate):
                remaining = ' | '.join(m for m in memos if m != candidate and m)
                return candidate, (label + (' | ' + remaining if remaining else ''))
        return label, ' | '.join(memos)
    return label, ' | '.join(memos)

def make_fitid(date, label, amount, memo=''):
    # Inclure le mémo dans le hash : deux transactions peuvent partager la même
    # date, le même libellé et le même montant (ex : deux retraits DAB
    # identiques le même jour, distingués uniquement par l'heure dans le
    # mémo). Sans le mémo, elles produisent le même FITID et la plupart des
    # logiciels d'import OFX considèrent la seconde comme un doublon et
    # l'ignorent silencieusement.
    return hashlib.md5(f"{date}{label}{amount:.2f}{memo}".encode()).hexdigest()

def date_jjmm_to_ofx(jjmm, year):
    p = jjmm.replace('.', '/').split('/')
    if len(p) == 2:
        return f"{year}{p[1].zfill(2)}{p[0].zfill(2)}"
    return f"{year}0101"

def date_full_to_ofx(date_str):
    date_str = date_str.replace('.', '/')
    p = date_str.split('/')
    if len(p) == 3:
        return f"{p[2]}{p[1].zfill(2)}{p[0].zfill(2)}"
    return datetime.now().strftime('%Y%m%d')

def extract_iban(text):
    """
    Extrait le premier IBAN valide trouvé dans le texte.
    Stratégies (ordre de priorité) :
      1. Mot-clé IBAN + lecture ligne par ligne (évite la fusion avec le BIC)
      2. IBAN nu — format groupé par 4 sans mot-clé
      3. Fallback UEMOA/BCEAO sans mot-clé
    Tronque à la longueur IBAN réglementaire du pays (ex: FR=27, SN=28…).
    """
    # Longueurs IBAN officielles par code pays
    _IBAN_LEN = {
        'FR':27,'BE':16,'DE':22,'ES':24,'IT':27,'NL':18,'PT':25,'GB':22,
        'CH':21,'IE':22,'LU':20,'AT':20,'DK':18,'FI':18,'NO':15,'SE':24,
        'SN':28,'CI':28,'BJ':28,'TG':28,'ML':28,'BF':28,'NE':28,
        'GW':25,'GN':26,'MR':27,'CM':27,'MA':28,'TN':24,'DZ':24,
    }

    def _clean_and_truncate(raw):
        """Nettoie et tronque à la bonne longueur selon le code pays."""
        raw = re.sub(r'\s+', '', raw).upper()
        raw = re.sub(r'[^A-Z0-9]', '', raw)
        if len(raw) < 14 or not re.match(r'^[A-Z]{2}\d{2}', raw):
            return ''
        max_len = _IBAN_LEN.get(raw[:2], 34)
        return raw[:max_len]

    # Normaliser les espaces insécables
    text_clean = text.replace('\xa0', ' ').replace('\u202f', ' ')

    # ── 1. Avec mot-clé : traiter ligne par ligne pour éviter la fusion avec BIC ─
    # Le BIC est souvent sur la ligne suivante : "IBAN: FR76 ...\nBIC: QNTO..."
    for line in text_clean.split('\n'):
        line = line.strip()
        m = re.search(
            r'(?:IBAN|I\.?B\.?A\.?N\.?)\s*[:\-]?\s*'
            r'([A-Z]{2}[\s]?\d{2}[\s\dA-Z]{10,38})',
            line, re.IGNORECASE
        )
        if m:
            result = _clean_and_truncate(m.group(1))
            if result:
                return result

    # ── 2. IBAN nu sans mot-clé — format groupé par 4 (ex: FR76 3000 4028…) ───
    # On cherche ligne par ligne pour ne pas déborder sur la ligne suivante
    for line in text_clean.split('\n'):
        m2 = re.search(
            r'\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,4})?)\b',
            line.upper()
        )
        if m2:
            result = _clean_and_truncate(m2.group(1))
            if result:
                return result

    # ── 3. IBAN UEMOA/BCEAO avec mot-clé (peut contenir lettres dans le BBAN) ──
    # Ex : "IBAN : SN08 SN035 01010 0022015022 03" ou compact
    uemoa_cc = r'(?:SN|CI|BJ|TG|ML|BF|NE|GW|GN|MR|CM|MA|TN|DZ)'
    for line in text_clean.split('\n'):
        m3a = re.search(
            r'(?:IBAN|I\.?B\.?A\.?N\.?)\s*[:\-]?\s*'
            r'(' + uemoa_cc + r'[\s\dA-Z]{14,36})',
            line, re.IGNORECASE
        )
        if m3a:
            result = _clean_and_truncate(m3a.group(1))
            if result:
                return result

    # ── 4. Fallback UEMOA/BCEAO sans mot-clé (scan global) ───────────────────
    m4 = re.search(
        r'\b(' + uemoa_cc + r'\d{2}[A-Z0-9\s]{15,35})\b',
        text_clean.upper()
    )
    if m4:
        result = _clean_and_truncate(m4.group(1))
        if result:
            return result

    # ── 5. Numéro de compte RIB brut BCEAO (si aucun IBAN trouvé) ────────────
    # Format BCEAO numérique pur : ex "SN011 01005 005000458982 90"
    # Certains PDF africains écrivent le RIB sans mentionner "IBAN"
    m5 = re.search(
        r'\b([A-Z]{2}\d{3,5})\s+(\d{4,6})\s+(\d{8,14})\s+(\d{2})\b',
        text_clean.upper()
    )
    if m5:
        raw = ''.join(m5.groups())
        if len(raw) >= 14 and re.match(r'^[A-Z]{2}', raw):
            result = _clean_and_truncate(raw)
            if result:
                return result

    return ''

# Codes pays UEMOA → (longueur IBAN, longueur compte)
_UEMOA_IBAN = {
    # Format BCEAO : CC KK BBB AAAAA NNNNNNNNNNNN CC
    # CC=pays(2) KK=clé(2) BBB=banque(3-5) AAAAA=agence(4-5) N=compte(11-12) CC=clé RIB(2)
    'SN': 28,  # Sénégal
    'CI': 28,  # Côte d'Ivoire
    'BJ': 28,  # Bénin
    'TG': 28,  # Togo
    'ML': 28,  # Mali
    'BF': 28,  # Burkina Faso
    'NE': 28,  # Niger
    'GW': 25,  # Guinée-Bissau
    'GN': 26,  # Guinée
    'MR': 27,  # Mauritanie
    'CM': 27,  # Cameroun
    'MA': 28,  # Maroc
    'TN': 24,  # Tunisie
    'DZ': 24,  # Algérie
}

def iban_to_rib(iban, info=None):
    """
    Décompose un IBAN en (banque, agence, compte) pour l'OFX.
    Priorité : champs _rib_* extraits directement du PDF (via _afr_header).
    Sinon : France (FR27), UEMOA/BCEAO (SN28…), fallback numérique.
    """
    # ── Priorité : RIB directement extrait du PDF ────────────────────────────
    if info and info.get('_rib_bank') and info.get('_rib_account'):
        return (info['_rib_bank'],
                info.get('_rib_agency', '00000'),
                info['_rib_account'])

    c = iban.replace(' ', '').upper()
    c = re.sub(r'[^A-Z0-9]', '', c)   # purge tout caractère invalide

    # ── France ──────────────────────────────────────────────────────────────
    if c.startswith('FR') and len(c) == 27:
        r = c[4:]
        return r[0:5], r[5:10], r[10:21]

    # ── UEMOA/BCEAO ─────────────────────────────────────────────────────────
    country = c[:2]
    if country in _UEMOA_IBAN and len(c) >= 20:
        bban = c[4:]   # tout après CC+KK
        # Format BCEAO : BB(5 alphan.) + Agence(5 num.) + Compte(11-12 num.) + [Cle(2)]
        # Certains IBAN BSIC font 26 chars sans cle RIB -> ne pas tronquer avec [-2]
        code_banque = bban[0:5]
        agence      = bban[5:10]
        expected_len = _UEMOA_IBAN.get(country, 28)
        iban_has_key = len(c) >= expected_len
        compte_raw = bban[10:-2] if iban_has_key else bban[10:]
        compte     = re.sub(r'[A-Z]', '', compte_raw)
        return code_banque, agence, compte

    # ── Fallback numérique ───────────────────────────────────────────────────
    # Si on a un code pays UEMOA mais que l'IBAN était trop court pour le bloc
    # ci-dessus, on retente avec le BBAN brut (lettres conservées pour code banque)
    if country in _UEMOA_IBAN and len(c) >= 12:
        bban = c[4:]
        code_banque = bban[0:5]
        agence      = re.sub(r'[^0-9]', '', bban[5:10]) or '00000'
        expected_len = _UEMOA_IBAN.get(country, 28)
        iban_has_key = len(c) >= expected_len
        compte_raw  = bban[10:-2] if iban_has_key else bban[10:]
        compte      = re.sub(r'[A-Z]', '', compte_raw)
        if code_banque:
            return code_banque, agence, compte
    digits = re.sub(r'[^0-9]', '', c)
    if len(digits) >= 15:
        return digits[0:5], digits[5:10], digits[10:22]
    if len(digits) >= 5:
        return digits[0:5], '00000', digits[5:]
    return '00000', '00000', c[:20] if c else '00000'

def _year_from_text(text):
    m = re.search(r'\b(20\d{2})\b', text)
    return int(m.group(1)) if m else datetime.now().year

def _parse_col_amount(words):
    if not words:
        return None
    full = ' '.join(w['text'] for w in words).replace('\xa0', ' ').strip()
    if full in ('.', ',', ''):
        return None
    m = re.search(r'(\d{1,3}(?:[.\s]\d{3})+,\d{2})', full)
    if m:
        val = parse_amount(m.group(1).replace(' ', '.'))
        if val is not None and val > 0:
            return val
    m2 = re.search(r'(\d+,\d{2})', full)
    if m2:
        val = parse_amount(m2.group(1))
        if val is not None and val > 0:
            return val
    return None

def _parse_signed_amount(words):
    if not words:
        return None
    full = ' '.join(w['text'] for w in words).replace('\xa0', ' ').strip()
    m = re.search(r'([+\-])\s*([\d\s]+[,.][\d]{2})', full)
    if m:
        sign = 1.0 if m.group(1) == '+' else -1.0
        val = parse_amount(m.group(2))
        if val is not None:
            return sign * val
    m2 = re.search(r'([\d\s]+[,.][\d]{2})', full)
    if m2:
        val = parse_amount(m2.group(1))
        if val is not None:
            return val
    return None

def _make_txn(date_ofx, amount, label, memo=''):
    txn_dict = {
        'date':   date_ofx,
        'type':   'CREDIT' if amount >= 0 else 'DEBIT',
        'amount': amount,
        'name':   clean_label(label)[:64],
        'memo':   clean_label(memo)[:128],
        'fitid':  make_fitid(date_ofx, label, amount, memo)
    }
    if _PYDANTIC_OK:
        try:
            Transaction(**txn_dict)
        except Exception as exc:
            logger.warning("Transaction ignorée [%s | %s | %.2f] : %s",
                           date_ofx, label[:40], amount, exc)
            return None
    return txn_dict

def _pdf_has_text(pages_text, min_chars=300):
    total = sum(len(p.strip()) for p in pages_text)
    return total >= min_chars

def _ocr_pdf(pdf_path):
    if not _OCR_AVAILABLE:
        raise RuntimeError(
            "Ce PDF semble scanné (aucun texte extractible). "
            "Les outils OCR (pytesseract, pdf2image, Tesseract) ne sont pas installés sur ce serveur."
        )
    images = convert_from_path(pdf_path, dpi=300)
    return [pytesseract.image_to_string(img, lang='fra+eng') for img in images]


# ════════════════════════════════════════════════════════════════════════════
# DÉTECTION DE LA BANQUE
# ════════════════════════════════════════════════════════════════════════════

def detect_bank(pages_text):
    text = pages_text[0][:3000].upper()
    if 'QONTO' in text or 'QNTOFRP' in text:
        return 'QONTO'
    if 'CREDIT LYONNAIS' in text or ('LCL' in text and 'RELEVE DE COMPTE COURANT' in text):
        return 'LCL'
    text_nospace = text.replace(' ', '')
    if ('SOCIETE GENERALE' in text or 'SOCIÉTÉ GÉNÉRALE' in text
            or 'SOCIETEGENERALE' in text_nospace) and (
            'SENEGAL' in text or 'SÉNÉGAL' in text or 'COTE D' in text
            or "CÔTE D'" in text or 'CAMEROUN' in text or 'DAKAR' in text
            or 'ABIDJAN' in text or 'DOUALA' in text or 'LOME' in text
            or 'BAMAKO' in text):
        return 'SG_AFRIQUE'
    if ('SOCIETE GENERALE' in text or 'SOCIÉTÉ GÉNÉRALE' in text
            or '552 120 222' in text or 'SOCIETEGENERALE' in text_nospace
            or 'SG.FR' in text or 'PROFESSIONNELS.SG.FR' in text):
        return 'SG'
    if 'CREDIT AGRICOLE' in text or 'AGRIFRPP' in text:
        return 'CA'
    if 'CAIXA GERAL' in text or 'CGDIFRPP' in text or 'CGD' in text[:500]:
        return 'CGD'
    if "CAISSE D'EPARGNE" in text or "CAISSE D.EPARGNE" in text or 'CEPAFRPP' in text:
        return 'CE'
    if 'BANQUE POPULAIRE' in text or 'CCBPFRPP' in text:
        return 'BP'
    if 'BANQUE POSTALE' in text or 'PSSTFRPP' in text or 'LABANQUEPOSTALE' in text:
        return 'LBP'
    if 'CREDIT INDUSTRIEL' in text or 'CMCIFRPP' in text or ('CIC' in text and 'RELEVE' in text):
        return 'CIC'
    # Crédit Mutuel de Bretagne (CMB) — caisse régionale distincte du Crédit
    # Mutuel "générique" (CIC/CM classique) : gabarit PDF différent (le nom
    # "Crédit Mutuel de Bretagne" est un logo/image, absent du texte extrait ;
    # on détecte donc via le BIC CMBRFRxx ou le domaine cmb.fr, présents en
    # texte clair sur la 1ère page). Placé AVANT la détection CM générique
    # pour ne pas passer par ce dernier parseur, qui reste inchangé.
    if 'CMBRFR2' in text_nospace or 'CMB.FR' in text_nospace:
        return 'CMB'
    if ('CREDIT MUTUEL' in text or 'CRÉDIT MUTUEL' in text
            or 'CMCIFR2A' in text or 'CREDITMUTUEL' in text_nospace
            or 'CAISSE DE CREDIT MUTUEL' in text):
        return 'CM'
    if ('BNP PARIBAS' in text or 'BNPAFRPP' in text or 'BNP' in text[:500]
            or 'BANQUE NATIONALE DE PARIS' in text):
        return 'BNP'
    if 'MYPOS' in text or 'MYPOS LTD' in text or 'MY POS' in text:
        return 'MYPOS'
    if ('SNNNFR22XXX' in text or 'SHINE.FR' in text or 'SHINE SAS' in text
            or ('SHINE' in text and ('RELEVE' in text or 'SNNN' in text or '1741' in text))):
        return 'SHINE'
    if 'NSIA BANQUE' in text or ('NSIA' in text and ('RELEVE DE COMPTE' in text or 'SOLDE DEBUT' in text)):
        return 'NSIA'
    # Détection NSIA par structure du relevé (le logo NSIA est souvent une image)
    if ('SOLDE DEBUT' in text or 'SOLDE DÉBUT' in text) and 'MOUV' in text and (
            'MOUV. DÉBIT' in text or 'MOUV. DEBIT' in text or
            'MOV. DEBIT' in text or 'NOMBRE DEBIT' in text or 'NOMBRE CRÉDIT' in text):
        return 'NSIA'
    if 'CBAO' in text or 'COMPAGNIE BANCAIRE DE L' in text:
        return 'CBAO'
    if 'ECOBANK' in text or 'ECOBANK SENEGAL' in text or 'PAN AFRICAN BANK' in text:
        return 'ECOBANK'
    # Détection Ecobank quand le logo est une image (pas de texte "ECOBANK")
    # Le relevé anglais Ecobank contient "Account Statement" + "Payments" + "Deposits"
    # et le format de date DD-Mon-YYYY (ex: "30-May-2025")
    if ('ACCOUNT STATEMENT' in text and 'PAYMENTS' in text and 'DEPOSITS' in text
            and re.search(r'\d{2}-[A-Z][A-Z][A-Z]-\d{4}', text)):
        return 'ECOBANK'
    # Détection par en-tête anglais Ecobank : "Statement From Date" + "Statement To Date"
    if 'STATEMENT FROM DATE' in text and 'STATEMENT TO DATE' in text:
        return 'ECOBANK'
    if 'BANQUE POUR LE COMMERCE' in text and 'INDUSTRIE' in text:
        return 'BCI'
    if 'CORIS BANK' in text or 'CORISBANK' in text_nospace:
        return 'CORIS'
    if 'UNITED BANK FOR AFRICA' in text or 'UNAFSNDA' in text or ('UBA' in text[:400] and 'BANK' in text):
        return 'UBA'
    if 'ORABANK' in text:
        return 'ORABANK'
    if ('BANK OF AFRICA' in text or 'AFRISNDA' in text
            or 'boasenegal' in text.lower() or 'bank-of-africa' in text.lower()
            or 'CIB-SN100Y' in text or 'CIB-SN' in text):
        return 'BOA'
    if 'ARAB TUNISIAN BANK' in text:
        return 'ATB'
    if ('BSIC' in text or 'BANQUE SAHELO' in text or 'SN08SN111' in text_nospace):
        return 'BSIC'
    if ('BANQUE ISLAMIQUE DU SENEGAL' in text or 'ISLAMIQUE' in text and 'SENEGAL' in text):
        return 'BIS'
    if 'BNDE' in text or 'BANQUE NATIONALE POUR LE DEVELOPPEMENT' in text:
        return 'BNDE'
    # Détection BNDE par code banque IBAN (SN08SN169...) ou structure colonnes
    if ('SN08SN169' in text_nospace or 'SN169' in text_nospace) and 'EXTRAIT DE COMPTE' in text:
        return 'BNDE'
    if ('DÉBIT (XOF)' in text or 'DEBIT (XOF)' in text) and ('CRÉDIT (XOF)' in text or 'CREDIT (XOF)' in text) and 'EXTRAIT DE COMPTE' in text:
        return 'BNDE'
    # Détection Wise (Wise Europe SA — néo-banque internationale)
    if ('WISE' in text or 'TRWIBEB' in text or 'WISE EUROPE' in text
            or 'WISE.COM' in text):
        return 'WISE'
    return 'UNIVERSAL'


# ════════════════════════════════════════════════════════════════════════════
# PARSEURS BANCAIRES (identiques à la version Tkinter — non modifiés)
# ════════════════════════════════════════════════════════════════════════════

def parse_qonto(pages_words, pages_text):
    info = _extract_qonto_header(pages_text[0])
    year = int(info['period_start'].split('/')[2]) if info.get('period_start') else _year_from_text(pages_text[0])
    txns = []
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            date_str = _qonto_date(row)
            if not date_str:
                i += 1; continue
            label_words = [w for w in row if 130 <= w['x0'] < 410]
            label = join_words_with_spaces(label_words)
            amount = _qonto_amount(row)
            memo = ''
            j = i + 1
            while j < len(rows) and not _qonto_date(rows[j]):
                memo_words = [w for w in rows[j] if 130 <= w['x0'] < 410]
                nl = join_words_with_spaces(memo_words)
                na = _qonto_amount(rows[j])
                if na is not None and amount is None:
                    amount = na; memo = nl; j += 1; break
                elif na is None and nl:
                    memo = nl; j += 1; break
                else:
                    break
            i = j
            if amount is None or not label or label in ('Transactions', 'Date de valeur'):
                continue
            memo_clean = memo if memo.strip() not in ('', '-', '+') else ''
            name, memo_out = smart_label(label, [memo_clean] if memo_clean else [])
            txns.append(_make_txn(date_jjmm_to_ofx(date_str, year), amount, name, memo_out))
    return info, [t for t in txns if t is not None]

def _qonto_date(row):
    for w in row:
        if w['x0'] < 120 and re.match(r'^\d{2}/\d{2}$', w['text']):
            return w['text']
    return ''

def _qonto_amount(row):
    aw = [w for w in row if w['x0'] >= 400]
    if not aw: return None
    full = ' '.join(w['text'] for w in aw).replace('EUR','').replace('\xa0',' ').strip()
    m = re.search(r'([+\-])\s*([\d\s]+[.,]\d{2})', full)
    if m:
        sign = 1.0 if m.group(1)=='+' else -1.0
        try: return sign * float(m.group(2).replace(' ','').replace(',','.'))
        except: pass
    m2 = re.search(r'([\d\s]+[.,]\d{2})', full)
    if m2:
        sign = 1.0
        for w in aw:
            if w['text'] in ('+','-'): sign = 1.0 if w['text']=='+' else -1.0; break
            sm = re.match(r'^([+\-])([\d,.]+)$', w['text'])
            if sm: sign = 1.0 if sm.group(1)=='+' else -1.0; break
        try: return sign * float(m2.group(1).replace(' ','').replace(',','.'))
        except: pass
    return None

def _extract_qonto_header(text):
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    m = re.search(r'Du\s+(\d{2}/\d{2}/\d{4})\s+au\s+(\d{2}/\d{2}/\d{4})', text)
    if m: info['period_start'], info['period_end'] = m.group(1), m.group(2)
    bals = re.findall(r'Solde au \d{2}/\d{2}\s*[+\-]\s*([\d]+\.[\d]{2})\s*EUR', text)
    if len(bals) >= 1: info['balance_open']  = float(bals[0])
    if len(bals) >= 2: info['balance_close'] = float(bals[-1])
    return info

def parse_lcl(pages_words, pages_text):
    info = _extract_lcl_header(pages_text)
    year = _year_from_text(pages_text[0])
    txns = []

    # Labels à ignorer absolument (lignes de solde, totaux, en-têtes)
    SKIP_LABELS = {
        'DEBIT', 'CREDIT', 'VALEUR', 'DATE', 'LIBELLE', 'ANCIEN SOLDE',
        'SOLDE EN EUROS', 'TOTAUX', 'NOUVEAU SOLDE', 'SOLDE INITIAL',
        'SOLDE FINAL', 'TOTAL', 'REPORT', 'A REPORTER',
    }
    # Préfixes de labels de solde ou total (comparaison startswith)
    SKIP_PREFIXES = ('SOLDE', 'TOTAUX', 'TOTAL', 'ANCIEN', 'NOUVEAU')

    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            date_str = _lcl_date(row)
            if not date_str:
                i += 1; continue
            label = ' '.join(w['text'] for w in row if 70 <= w['x0'] < 360).strip()

            # Ignorer les lignes de solde/totaux même si elles ont une date
            label_up = label.upper().strip()
            if (label_up in SKIP_LABELS
                    or any(label_up.startswith(p) for p in SKIP_PREFIXES)
                    or not label_up):
                i += 1; continue

            debit_words = [w for w in row if 360 <= w['x0'] < 490
                           and not re.match(r'^\d{2}\.\d{2}(\.\d{2,4})?$', w['text'])]
            debit_amt  = _parse_col_amount(debit_words)
            credit_amt = _parse_col_amount([w for w in row if w['x0'] >= 490])

            memo = ''
            j = i + 1
            while j < len(rows) and not _lcl_date(rows[j]):
                nl = ' '.join(w['text'] for w in rows[j] if 70 <= w['x0'] < 360).strip()
                nl_up = nl.upper().strip()
                if nl and nl_up not in SKIP_LABELS and not any(nl_up.startswith(p) for p in SKIP_PREFIXES):
                    memo = (memo + ' ' + nl).strip()
                j += 1
            i = j

            date_ofx = date_jjmm_to_ofx(date_str, year)
            name, memo_out = smart_label(label, [memo] if memo else [])
            if debit_amt is not None:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo_out))
            elif credit_amt is not None:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo_out))
    return info, [t for t in txns if t is not None]

def _lcl_date(row):
    for w in row:
        if w['x0'] < 100 and re.match(r'^\d{2}\.\d{2}$', w['text']):
            return w['text']
    return ''

def _extract_lcl_header(pages_text):
    text = ' '.join(pages_text)
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)

    # Période : "du 03.10.2025 au 31.10.2025"
    m = re.search(r'du\s+(\d{2}\.\d{2}\.\d{4})\s+au\s+(\d{2}\.\d{2}\.\d{4})', text, re.IGNORECASE)
    if m:
        info['period_start'] = m.group(1).replace('.','/')
        info['period_end']   = m.group(2).replace('.','/')

    # Solde d'ouverture : "ANCIEN SOLDE  40 978,70" ou "ANCIEN SOLDE 40978,70"
    m_open = re.search(r'ANCIEN SOLDE\s+([\d\s]+[,\.]\d{2})', text)
    if m_open:
        v = parse_amount(m_open.group(1).replace(' ', ''))
        if v: info['balance_open'] = v

    # Solde de clôture : "SOLDE EN EUROS  46 862,54" (dernière ligne du relevé)
    for m_close in re.finditer(r'SOLDE EN EUROS\s+([\d\s]+[,\.]\d{2})', text):
        v = parse_amount(m_close.group(1).replace(' ', ''))
        if v: info['balance_close'] = v

    return info

def _ca_parse_zone(row, x_min, x_max):
    col = [w for w in row if x_min <= w['x0'] < x_max and re.match(r'^\d', w['text'])]
    if not col: return None
    last = col[-1]['text']
    if not re.match(r'^\d+,\d{2}$', last): return None
    if len(col) == 1: return parse_amount(last)
    prefix_tokens = [w['text'] for w in col[:-1]]
    if all(re.match(r'^\d+$', p) for p in prefix_tokens):
        try:
            return float(''.join(prefix_tokens) + last.replace(',', '.'))
        except ValueError:
            pass
    return None

def parse_ca(pages_words, pages_text):
    info = _extract_ca_header(pages_text)
    year = _year_from_text(pages_text[0])
    txns = []
    SKIP = {'Débit','Crédit','Date','Libellé','Total des opérations','Nouveau solde',
            'opé.','valeur','Libellé des opérations','Ancien solde débiteur','Nouveau solde débiteur'}
    # Page footers/legal notices sit right below the last transaction of a page,
    # with no date column, so the memo-continuation scan below would otherwise
    # swallow them into that transaction's memo.
    STOP_PAT = re.compile(
        r'^(Page\s+\d|Total des opérations|Nouveau solde|Ancien solde|'
        r'Crédit Agricole|CAISSE REGI|Société coopérative|L.absence de réclamation|'
        r'Les sommes figurant|IDU CITEO|FR\d{2}\d+.Membre|\*Appel non surtax)',
        re.IGNORECASE)
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            date_str = _ca_date(row)
            if not date_str:
                i += 1; continue
            label = ' '.join(w['text'] for w in row if 70 <= w['x0'] < 420).strip()
            debit_amt  = _ca_parse_zone(row, 415, 490)
            credit_amt = _ca_parse_zone(row, 490, 560)
            memo_parts = []
            j = i + 1
            while j < len(rows) and not _ca_date(rows[j]):
                nl = ' '.join(w['text'] for w in rows[j] if 70 <= w['x0'] < 420).strip()
                if STOP_PAT.match(nl):
                    break
                if nl and nl not in SKIP and len(nl) > 1:
                    memo_parts.append(nl)
                j += 1
            i = j
            if not label or label in SKIP: continue
            date_ofx = date_jjmm_to_ofx(date_str, year)
            name, memo = smart_label(label, memo_parts)
            if debit_amt is not None:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt is not None:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))
    return info, [t for t in txns if t is not None]

def _ca_date(row):
    for w in row:
        if w['x0'] < 50 and re.match(r'^\d{2}\.\d{2}$', w['text']):
            return w['text']
    return ''

def _extract_ca_header(pages_text):
    text = ' '.join(pages_text[:2])
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    mois_map = {'janvier':'01','février':'02','mars':'03','avril':'04','mai':'05','juin':'06',
                'juillet':'07','août':'08','septembre':'09','octobre':'10','novembre':'11','décembre':'12'}
    m = re.search(r'Date d.arrêté\s*:\s*(\d+)\s+(\w+)\s+(\d{4})', text)
    if m:
        mn = mois_map.get(m.group(2).lower(), '01')
        info['period_end']   = f"{m.group(1).zfill(2)}/{mn}/{m.group(3)}"
        info['period_start'] = f"01/{mn}/{m.group(3)}"
    # Solde d'ouverture / clôture : "Ancien solde débiteur au 31.05.2024  99,00"
    # ou "Ancien/Nouveau solde créditeur au JJ.MM.AAAA  X,XX" (débiteur => négatif).
    mo = re.search(r'Ancien\s+solde\s+(débiteur|créditeur)\s+au\s+\d{2}\.\d{2}\.\d{4}\s+([\d\s]+,\d{2})', text, re.IGNORECASE)
    if mo:
        val = parse_amount(mo.group(2)) or 0.0
        info['balance_open'] = -val if mo.group(1).lower() == 'débiteur' else val
    mc = re.search(r'Nouveau\s+solde\s+(débiteur|créditeur)\s+au\s+\d{2}\.\d{2}\.\d{4}\s+([\d\s]+,\d{2})', text, re.IGNORECASE)
    if mc:
        val = parse_amount(mc.group(2)) or 0.0
        info['balance_close'] = -val if mc.group(1).lower() == 'débiteur' else val
    return info

def parse_ce(pages_words, pages_text):
    info = _extract_ce_header(pages_text)
    txns = []
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            date_str = _ce_date(row)
            if not date_str:
                i += 1; continue
            label = ' '.join(w['text'] for w in row if 155 <= w['x0'] < 500).strip()
            amount = _parse_signed_amount([w for w in row if w['x0'] >= 500])
            memo = ''
            j = i + 1
            while j < len(rows) and not _ce_date(rows[j]):
                nl = ' '.join(w['text'] for w in rows[j] if 155 <= w['x0'] < 500).strip()
                if nl and len(nl) > 2:
                    memo = (memo + ' ' + nl).strip()
                j += 1
            i = j
            if not label or amount is None: continue
            skip_kw = {'DATE','VALEUR','MONTANT','OPERATIONS','SOLDE','TOTAL','DETAIL'}
            if any(s in label.upper() for s in skip_kw): continue
            date_ofx = date_full_to_ofx(date_str)
            name, memo_out = smart_label(label, [memo] if memo else [])
            txns.append(_make_txn(date_ofx, amount, name, memo_out))
    return info, [t for t in txns if t is not None]

def _ce_date(row):
    for w in row:
        if w['x0'] < 100 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text']):
            return w['text']
    return ''

def _extract_ce_header(pages_text):
    text = ' '.join(pages_text[:2])
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    return info

def parse_bp(pages_words, pages_text):
    info = _extract_bp_header(pages_text)
    year = _year_from_text(pages_text[0])
    txns = []
    SKIP_KW = {'DATE','LIBELLE','REFERENCE','COMPTA','VALEUR','MONTANT','SOLDE','TOTAL','DETAIL','OPERATION'}
    for pw in pages_words:
        rows = group_words_by_row(pw)
        skip_from = None
        for idx, row in enumerate(rows):
            row_text = ' '.join(w['text'] for w in row).upper()
            if 'DETAIL DE VOS MOUVEMENTS SEPA' in row_text or 'DETAIL DE VOS PRELEVEMENTS SEPA' in row_text:
                skip_from = idx; break
        i = 0
        while i < len(rows):
            if skip_from is not None and i >= skip_from: break
            row = rows[i]
            date_str = _bp_date(row)
            if not date_str:
                i += 1; continue
            label = ' '.join(w['text'] for w in row if 80 <= w['x0'] < 355).strip()
            amount = _bp_amount([w for w in row if w['x0'] >= 490])
            memo_parts = []
            j = i + 1
            while j < len(rows) and not _bp_date(rows[j]):
                if skip_from is not None and j >= skip_from: break
                nl = ' '.join(w['text'] for w in rows[j] if 80 <= w['x0'] < 355).strip()
                if nl and len(nl) > 2 and not re.match(r'^[\d\s.,€%=\-EUR]+$', nl):
                    memo_parts.append(nl)
                j += 1
            i = j
            if not label or amount is None: continue
            if any(s in label.upper() for s in SKIP_KW): continue
            date_ofx = date_jjmm_to_ofx(date_str, year)
            name, memo = smart_label(label, memo_parts)
            txns.append(_make_txn(date_ofx, amount, name, memo))
    return info, [t for t in txns if t is not None]

def _bp_date(row):
    for w in row:
        if w['x0'] < 80 and re.match(r'^\d{2}/\d{2}$', w['text']):
            return w['text']
    return ''

def _bp_amount(words):
    if not words: return None
    full = ' '.join(w['text'] for w in words).replace('€','').replace('\xa0',' ').strip()
    m = re.search(r'-\s*([\d\s]+[,.][\d]{2})', full)
    if m:
        try: return -abs(float(m.group(1).replace(' ','').replace(',','.')))
        except: pass
    m2 = re.search(r'\+\s*([\d\s]+[,.][\d]{2})', full)
    if m2:
        try: return abs(float(m2.group(1).replace(' ','').replace(',','.')))
        except: pass
    m3 = re.search(r'([\d\s]+[,.][\d]{2})', full)
    if m3:
        try:
            val = float(m3.group(1).replace(' ','').replace(',','.'))
            return val if val > 0 else None
        except: pass
    return None

def _extract_bp_header(pages_text):
    text = pages_text[0] if pages_text else ''
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    return info

def parse_cic(pages_words, pages_text):
    info = _extract_cic_header(pages_text)
    txns = []
    for pw in pages_words:
        rows = group_words_by_row(pw)
        # Détecter dynamiquement les limites des colonnes débit/crédit sur cette page
        # En analysant la ligne "Total des mouvements" ou les colonnes d'en-tête
        # Par défaut : débit ~420-490, crédit ~490+
        # On ajuste dynamiquement selon le x0 des montants trouvés dans les lignes de transaction
        i = 0
        while i < len(rows):
            row = rows[i]
            date_str = _cic_date(row)
            if not date_str:
                i += 1; continue

            # Le libellé commence après les deux colonnes de date (x0 ≈ 100-145)
            # et s'étend jusqu'à la zone montant
            # On détecte dynamiquement le x_label_start (après la 2e date)
            date_words = [w for w in row if re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
            x_label_start = max((w['x1'] for w in date_words), default=140) + 2
            # Zone montant : tous les mots numériques en fin de ligne (x0 > 400)
            amount_words = [w for w in row if w['x0'] > 400 and re.match(r'^[\d.,]+$', w['text'])]
            # Libellé = mots entre fin des dates et début des montants
            x_label_end = min((w['x0'] for w in amount_words), default=420) - 2
            label_words = [w for w in row if x_label_start <= w['x0'] < x_label_end]
            label = ' '.join(w['text'] for w in label_words).strip()

            # Détermination débit / crédit selon la position du montant dans la ligne
            # Sur ce relevé CIC : débit ≈ col 420-490, crédit ≈ col 490+
            # On utilise le fait que débit est AVANT crédit (x0 plus petit)
            debit_amt = None
            credit_amt = None
            if len(amount_words) >= 2:
                # Deux montants : le plus à gauche = débit, le plus à droite = crédit
                sorted_amts = sorted(amount_words, key=lambda w: w['x0'])
                debit_amt  = _parse_col_amount([sorted_amts[0]])
                credit_amt = _parse_col_amount([sorted_amts[-1]])
            elif len(amount_words) == 1:
                # Un seul montant : débit si x0 < milieu (~490), crédit sinon
                w = amount_words[0]
                # Trouver x_mid dynamiquement : milieu entre x_label_end et bord droit
                # Par défaut on utilise 490 comme seuil
                if w['x0'] < 490:
                    debit_amt = _parse_col_amount([w])
                else:
                    credit_amt = _parse_col_amount([w])

            # Lignes de continuation (mémo) : pas de date, dans la zone libellé
            memo_parts = []
            j = i + 1
            while j < len(rows) and not _cic_date(rows[j]):
                nl_words = [w for w in rows[j] if x_label_start <= w['x0'] < x_label_end + 100]
                nl = ' '.join(w['text'] for w in nl_words).strip()
                if nl and len(nl) > 2 and not re.match(r'^[\d.,\s]+$', nl):
                    memo_parts.append(nl)
                j += 1
            i = j

            if not label: continue
            skip_kw = {'DATE','DÉBIT','CRÉDIT','EUROS','SOLDE CREDITEUR','CREDIT INDUSTRIEL',
                       'TOTAL DES MOUVEMENTS','TOTAL DES','SOLDE DEBITEUR'}
            if any(s in label.upper() for s in skip_kw): continue

            date_ofx = date_full_to_ofx(date_str)
            name, memo_out = smart_label(label, memo_parts)
            if debit_amt is not None:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo_out))
            elif credit_amt is not None:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo_out))
    return info, [t for t in txns if t is not None]

def _cic_date(row):
    """Retourne la première date JJ/MM/AAAA trouvée dans les 150 premiers points de la ligne."""
    for w in row:
        if w['x0'] < 150 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text']):
            return w['text']
    return ''

def _extract_cic_header(pages_text):
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    for pt in reversed(pages_text):
        iban = extract_iban(pt)
        if iban:
            info['iban'] = iban; break
    return info

def parse_cgd(pages_words, pages_text):
    info = _extract_cgd_header(pages_text)
    year = _year_from_text(pages_text[0])
    txns = []
    SKIP = {'A REPORTER','REPORT','TOTAL','NOUVEAU','ANCIEN','SARL','CPT ORD'}
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            if not (len(row) >= 2
                    and re.match(r'^\d{2}$', row[0]['text']) and row[0]['x0'] < 50
                    and re.match(r'^\d{2}$', row[1]['text']) and row[1]['x0'] < 55):
                i += 1; continue
            dd, mm = row[0]['text'], row[1]['text']
            label = ' '.join(w['text'] for w in row if 70 <= w['x0'] < 310).strip()
            if not label or any(s in label.upper() for s in SKIP):
                i += 1; continue
            debit_amt  = _cgd_amount_in_zone(row, 395, 500)
            credit_amt = _cgd_amount_in_zone(row, 500, 570)
            memo_parts = []
            j = i + 1
            while j < len(rows):
                r2 = rows[j]
                if len(r2) >= 2 and re.match(r'^\d{2}$', r2[0]['text']) and r2[0]['x0'] < 50: break
                nl = ' '.join(w['text'] for w in r2 if 70 <= w['x0'] < 310).strip()
                if nl: memo_parts.append(nl)
                j += 1
            i = j
            date_ofx = f"{year}{mm.zfill(2)}{dd.zfill(2)}"
            name, memo = smart_label(label, memo_parts)
            if debit_amt:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))
    return info, [t for t in txns if t is not None]

def _cgd_amount_in_zone(row, x_min, x_max):
    col = [w for w in row if x_min <= w['x0'] < x_max and re.match(r'^\d', w['text'])]
    if not col: return None
    return parse_amount(col[-1]['text'])

def _extract_cgd_header(pages_text):
    text = ' '.join(pages_text[:2])
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    return info

def parse_lbp(pages_words, pages_text):
    info = _extract_lbp_header(pages_text)
    year = _year_from_text(pages_text[0])
    txns = []
    SKIP = {'TOTAL DES','NOUVEAU SOLDE','ANCIEN SOLDE','VOS OPERATIONS','DATE OPERATION','SITUATION DU','PAGE'}
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            if not (row[0]['x0'] < 60 and re.match(r'^\d{2}/\d{2}$', row[0]['text'])):
                i += 1; continue
            label = ' '.join(w['text'] for w in row if 85 <= w['x0'] < 430).strip()
            label = re.sub(r'\(cid:\d+\)', '', label).strip()
            if not label or any(s in label.upper() for s in SKIP):
                i += 1; continue
            debit_amt  = _lbp_amount_in_zone(row, 430, 500)
            credit_amt = _lbp_amount_in_zone(row, 500, 560)
            j = i + 1
            while j < len(rows):
                r2 = rows[j]
                if r2[0]['x0'] < 60 and re.match(r'^\d{2}/\d{2}$', r2[0]['text']): break
                j += 1
            i = j
            date_ofx = f"{year}{row[0]['text'][3:5]}{row[0]['text'][:2]}"
            name, memo = smart_label(label, [])
            if debit_amt:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))
    return info, [t for t in txns if t is not None]

def _lbp_amount_in_zone(row, x_min, x_max):
    col = [w for w in row if x_min <= w['x0'] < x_max and re.match(r'^\d', w['text'])]
    if not col: return None
    last = col[-1]['text']
    if not re.match(r'^\d+,\d{2}$', last): return None
    if len(col) == 1: return parse_amount(last)
    prefix_tokens = [w['text'] for w in col[:-1]]
    if all(re.match(r'^\d+$', p) for p in prefix_tokens):
        try: return float(''.join(prefix_tokens) + last.replace(',', '.'))
        except: pass
    return parse_amount(last)

def _extract_lbp_header(pages_text):
    text = re.sub(r'\(cid:\d+\)', ' ', ' '.join(pages_text[:2]))
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    return info

def parse_sg(pages_words, pages_text):
    info = _extract_sg_header(pages_text)
    txns = []
    SKIP = {'TOTAUX DES','NOUVEAU SOLDE','SOLDE PRECEDENT','PROGRAMME DE','RAPPEL DES','MONTANT CUMULE'}
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            if not (row[0]['x0'] < 45 and re.match(r'^\d{2}/\d{2}/\d{4}$', row[0]['text'])):
                i += 1; continue
            label = ' '.join(w['text'] for w in row if 120 <= w['x0'] < 430).strip()
            if not label or any(s in label.upper() for s in SKIP):
                i += 1; continue
            debit_amt  = _sg_amount_in_zone(row, 430, 510)
            credit_amt = _sg_amount_in_zone(row, 510, 570)
            memo_parts = []
            j = i + 1
            while j < len(rows):
                r2 = rows[j]
                if r2[0]['x0'] < 45 and re.match(r'^\d{2}/\d{2}/\d{4}$', r2[0]['text']): break
                nl = ' '.join(w['text'] for w in r2 if 120 <= w['x0'] < 430).strip()
                if nl and not any(s in nl.upper() for s in ('TOTAUX','NOUVEAU','PROGRAMME','RAPPEL')):
                    memo_parts.append(nl)
                j += 1
            i = j
            date_ofx = date_full_to_ofx(row[0]['text'])
            name, memo = smart_label(label, memo_parts)
            if debit_amt:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))
    return info, [t for t in txns if t is not None]

def _uba_join_amount(words):
    """
    Reconstitue un montant XOF/EUR fragmente en plusieurs tokens pdfplumber.
    Gere les montants avec decimales : ['41', '295,00'] → 41295.0
    ET les montants entiers XOF     : ['15', '400']     → 15400.0
    ET les grands entiers           : ['2', '316', '032,00'] → 2316032.0
    Strategie :
      1. Cherche d'abord un pattern avec decimales (ex: '15 400,00')
      2. Sinon, si tous les tokens sont numeriques purs, les concatene
         en verifiant que c'est coherent (pas un solde, pas une date)
    """
    if not words:
        return None
    full = ' '.join(w['text'] for w in words).replace('\xa0', ' ').strip()
    if not full or full in ('.', ','):
        return None

    # --- Cas 0 : format BIS iMAL (virgule séparateur milliers, pas de décimales) ---
    # Ex: "2,362,500"  "660,000"  "1,350,412"
    m0 = re.match(r'^(\d{1,3}(?:,\d{3})+)$', full.replace(' ', ''))
    if m0:
        try:
            val = float(m0.group(1).replace(',', ''))
            if val > 0:
                return val
        except ValueError:
            pass

    # --- Cas 1 : montant avec decimales (EUR ou XOF) ---
    # Ex: "15 400,00"  "2 316 032,00"  "41,95"
    m = re.search(r'(\d[\d\s]*[\.,]\d{2})\b', full)
    if m:
        raw = m.group(1).strip()
        normalized = re.sub(r'\s+', '', raw).replace(',', '.')
        try:
            val = float(normalized)
            if val > 0:
                return val
        except ValueError:
            pass

    # --- Cas 2 : montant entier XOF (pas de decimales) ---
    # Tous les tokens doivent etre purement numeriques
    # Ex: ['15', '400']  ['369', '630']  ['5', '850']
    texts = [w['text'] for w in words]
    if all(re.match(r'^\d+$', t) for t in texts) and len(texts) >= 2:
        # Verifier que la concatenation donne un nombre raisonnable
        # Le 2e token et suivants doivent avoir exactement 3 chiffres
        # (separateur milliers) pour eviter de concatener une date valeur
        valid = True
        for t in texts[1:]:
            if len(t) != 3:
                valid = False
                break
        if valid:
            try:
                val = float(''.join(texts))
                if val > 0:
                    return val
            except ValueError:
                pass
    elif len(texts) == 1 and re.match(r'^\d+$', texts[0]):
        # Montant entier sur un seul token
        try:
            val = float(texts[0])
            if val > 0:
                return val
        except ValueError:
            pass

    return None

def _sg_amount_in_zone(row, x_min, x_max):
    col = [w for w in row if x_min <= w['x0'] < x_max]
    if not col: return None
    # Enlever le '*' (opérations exonérées de commission) avant de parser
    col_clean = [w for w in col if w['text'].strip() not in ('*', '')]
    if not col_clean: return None

    # Reconstituer la chaîne complète et tenter parse_amount directement
    # (gère correctement "1.082,92", "29.117,17", etc.)
    full = ' '.join(w['text'] for w in col_clean).replace('*', '').replace('\xa0', ' ').strip()
    # Format français groupé par 3 avec point comme séparateur de milliers : 1.082,92
    m_fr = re.search(r'(\d{1,3}(?:\.\d{3})+,\d{2})', full)
    if m_fr:
        v = parse_amount(m_fr.group(1))
        if v is not None and v > 0:
            return v
    # Format simple : 100,75  ou  312,48
    m_simple = re.search(r'(\d+,\d{2})', full)
    if m_simple:
        v = parse_amount(m_simple.group(1))
        if v is not None and v > 0:
            return v
    # Fallback : montants fragmentés en plusieurs tokens (ex: "5 850" → 5850)
    v = _uba_join_amount(col_clean)
    if v is not None:
        return v
    return None

def _extract_sg_header(pages_text):
    text = ' '.join(pages_text[:2])
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}

    # ── IBAN (présent sur certains relevés SG) ────────────────────────────────
    info['iban'] = extract_iban(text)

    # ── RIB SG : "n° 30003 03320 00020641644 69" ─────────────────────────────
    # Format : n° BBBBB GGGGG CCCCCCCCCCC KK  (Banque 5 + Guichet 5 + Compte 11 + Clé 2)
    # pdfplumber peut lire avec espaces OU en un seul token compact "n°30003033200002064164469"
    if not info['iban']:
        # Cas 1 : avec espaces
        rib_m = re.search(
            r'n[°o]\s*(\d{5})\s+(\d{5})\s+(\d{10,12})\s+(\d{2})\b', text)
        # Cas 2 : compact sans espaces (23 chiffres : 5+5+11+2)
        if not rib_m:
            rib_m = re.search(r'n[°o]\s*(\d{5})(\d{5})(\d{11})(\d{2})\b', text)
        if rib_m:
            banque  = rib_m.group(1)
            guichet = rib_m.group(2)
            compte  = rib_m.group(3)
            cle     = rib_m.group(4)
            info['_rib_bank']    = banque
            info['_rib_agency']  = guichet
            info['_rib_account'] = compte
            info['_rib_key']     = cle
            info['iban'] = f"{banque} {guichet} {compte} {cle}"

    # ── Période : "du 01/03/2026 au 31/03/2026" ──────────────────────────────
    # Tolérance aux espaces multiples et aux retours à la ligne (layout multi-col SG)
    m = re.search(
        r'du\s+(\d{2}/\d{2}/\d{4})\s+au\s+(\d{2}/\d{2}/\d{4})',
        text, re.IGNORECASE
    )
    if m:
        info['period_start'] = m.group(1)
        info['period_end']   = m.group(2)

    # ── Solde de clôture : "NOUVEAU SOLDE AU 31/03/2026 + 85.536,72" ─────────
    m_bal = re.search(
        r'NOUVEAU SOLDE\s+AU\s+\d{2}/\d{2}/\d{4}\s+[+\-]?\s*([\d\s]+[,\.]\d{2})',
        text, re.IGNORECASE
    )
    if m_bal:
        v = parse_amount(m_bal.group(1).replace(' ', '').replace('\xa0', ''))
        if v:
            info['balance_close'] = v

    return info

def parse_bnp(pages_words, pages_text):
    info = _extract_bnp_header(pages_text)
    year = _year_from_text(' '.join(pages_text[:2]))
    txns = []
    SKIP = {'DATE','LIBELLE','VALEUR','DEBIT','CREDIT','EUROS','SOLDE','TOTAL','OPERATIONS',
            'ANCIEN SOLDE','NOUVEAU SOLDE','VIREMENT RECU','RELEVE DE COMPTE'}
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            date_str = _bnp_date(row)
            if not date_str:
                i += 1; continue
            label = ' '.join(w['text'] for w in row if 85 <= w['x0'] < 430).strip()
            if not label or any(s in label.upper() for s in SKIP):
                i += 1; continue
            debit_amt  = _parse_col_amount([w for w in row if 480 <= w['x0'] < 560])
            credit_amt = _parse_col_amount([w for w in row if w['x0'] >= 560])
            memo_parts = []
            j = i + 1
            while j < len(rows) and not _bnp_date(rows[j]):
                nl = ' '.join(w['text'] for w in rows[j] if 85 <= w['x0'] < 430).strip()
                if nl and len(nl) > 2:
                    memo_parts.append(nl)
                j += 1
            i = j
            date_ofx = _bnp_date_to_ofx(date_str, year)
            name, memo = smart_label(label, memo_parts)
            if debit_amt is not None:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt is not None:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))
    return info, [t for t in txns if t is not None]

def _bnp_date(row):
    for w in row:
        if w['x0'] < 80:
            if re.match(r'^\d{2}/\d{2}/\d{2}$', w['text']): return w['text']
            if re.match(r'^\d{2}/\d{2}/\d{4}$', w['text']): return w['text']
    return ''

def _bnp_date_to_ofx(date_str, year_hint):
    parts = date_str.split('/')
    if len(parts) == 3:
        dd, mm, yy = parts[0].zfill(2), parts[1].zfill(2), parts[2]
        if len(yy) == 2:
            full_year = (2000 + int(yy)) if int(yy) <= 30 else (1900 + int(yy))
        else:
            full_year = int(yy)
        return f"{full_year}{mm}{dd}"
    return str(year_hint) + '0101'

def _extract_bnp_header(pages_text):
    text = ' '.join(pages_text[:2])
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    return info

def parse_mypos(pages_words, pages_text):
    info = _extract_mypos_header(pages_text)
    txns = []
    full_text = '\n'.join(pages_text)
    lines = [l.strip() for l in full_text.splitlines()]
    txn_re = re.compile(
        r'^(\d{2}\.\d{2}\.\d{4})\s+\d{2}:\d{2}\s+'
        r'(System Fee|myPOS Payment|Glass Payment|Outgoing bank transfer|POS Payment|Mobile)\s*'
        r'.*?1\.0000\s+([\d,]+\.?\d*)\s+([\d,]+\.?\d*)\s*$'
    )
    for idx, line in enumerate(lines):
        m = txn_re.match(line)
        if not m: continue
        date_raw = m.group(1)
        txn_type = m.group(2).strip()
        try:
            debit_val  = float(m.group(3).replace(',', ''))
            credit_val = float(m.group(4).replace(',', ''))
        except ValueError:
            continue
        date_ofx = f"{date_raw[6:10]}{date_raw[3:5]}{date_raw[0:2]}"
        description = ''
        for back in (1, 2):
            if idx >= back:
                prev = lines[idx - back].strip()
                if prev and not re.match(r'^\d{2}\.\d{2}\.\d{4}', prev):
                    description = prev; break
        if txn_type == 'System Fee':
            name, memo = 'myPOS Fee', description
        else:
            name, memo = description or txn_type, description
        amount = -debit_val if debit_val > 0 else (credit_val if credit_val > 0 else None)
        if amount is None: continue
        txns.append(_make_txn(date_ofx, amount, name[:64], memo[:128]))
    return info, [t for t in txns if t is not None]

def _extract_mypos_header(pages_text):
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    text = pages_text[0] if pages_text else ''
    m = re.search(r'IBAN\s*:?\s*(IE\d{2}[A-Z0-9]+)', text)
    if m: info['iban'] = m.group(1).replace(' ','')
    m2 = re.search(r'Monthly statement\s*[-–]\s*(\d{2})\.(\d{4})', text, re.IGNORECASE)
    if m2:
        import calendar
        month, year = m2.group(1), m2.group(2)
        last_day = calendar.monthrange(int(year), int(month))[1]
        info['period_start'] = f"01/{month}/{year}"
        info['period_end']   = f"{last_day:02d}/{month}/{year}"
    return info

def parse_shine(pages_words, pages_text):
    info = _extract_shine_header(pages_text)
    txns = []
    # NB: 'TOTAL' n'est volontairement PAS dans cette liste : les lignes
    # récapitulatives ("Total des mouvements", "Total des commissions") n'ont
    # jamais de date en colonne 1 et sont donc déjà exclues plus haut par le
    # test `if not date_str`. En revanche "TOTAL" est un nom de commerçant très
    # courant (carte carburant TOTAL/TotalEnergies) : le garder ici supprimait
    # à tort toutes les transactions "Carte TOTAL".
    SKIP = {'DATE','TYPE','OPÉRATION','OPERATION','DÉBIT','DEBIT','CRÉDIT','CREDIT',
            '(EURO)','SOLDE','NOUVEAU','COMMISSIONS','MOUVEMENTS','PAGE','LES','RELEVÉ'}
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            date_str = _shine_date(row)
            if not date_str:
                i += 1; continue
            txn_type = ' '.join(w['text'] for w in row if 95 <= w['x0'] < 160).strip()
            label    = ' '.join(w['text'] for w in row if 160 <= w['x0'] < 453).strip()
            debit_amt  = _parse_col_amount([w for w in row if 453 <= w['x0'] < 513])
            credit_amt = _parse_col_amount([w for w in row if w['x0'] >= 513])
            memo_parts = []
            j = i + 1
            while j < len(rows) and not _shine_date(rows[j]):
                nl = ' '.join(w['text'] for w in rows[j] if 95 <= w['x0'] < 453).strip()
                if nl and len(nl) > 2:
                    memo_parts.append(nl)
                j += 1
            i = j
            full_label = (txn_type + ' ' + label).strip() if txn_type else label
            if any(s in full_label.upper() for s in SKIP) or len(full_label) < 2: continue
            date_ofx = date_full_to_ofx(date_str)
            name, memo = smart_label(full_label, memo_parts)
            if debit_amt is not None:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt is not None:
                txns.append(_make_txn(date_ofx,  credit_amt, name, memo))
    return info, [t for t in txns if t is not None]

def _shine_date(row):
    for w in row:
        if w['x0'] < 60 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text']):
            return w['text']
    return ''

def _extract_shine_header(pages_text):
    text = ' '.join(pages_text[:3])
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)

    # Période : "De 01/01/2026 à 31/01/2026"
    m = re.search(r'De\s+(\d{2}/\d{2}/\d{4})\s+[àa]\s+(\d{2}/\d{2}/\d{4})', text, re.IGNORECASE)
    if m:
        info['period_start'] = m.group(1)
        info['period_end']   = m.group(2)

    # Solde d'ouverture : "Solde au JJ/MM/AAAA X,XX €"
    m_open = re.search(r'Solde\s+au\s+\d{2}/\d{2}/\d{4}\s+([\d\s]+[,\.]\d{2})', text, re.IGNORECASE)
    if m_open:
        v = parse_amount(m_open.group(1))
        if v is not None: info['balance_open'] = v

    # Nouveau solde (clôture). Shine peut écrire :
    #   Nouveau solde 1 234,56
    #   Nouveau solde créditeur au 31/01/2026 1 234,56
    #   Nouveau solde débiteur au 31/01/2026 1 234,56
    # Dans le dernier cas, un solde débiteur est négatif.
    close_patterns = [
        (r'Nouveau\s+solde\s+(?:cr[ée]diteur\s+|d[ée]biteur\s+)?(?:au\s+\d{2}[./-]\d{2}[./-]\d{4}\s+)?([\d\s]+[,\.]\d{2})', False),
        (r'Nouveau\s+solde\s+(?:cr[ée]diteur\s+|d[ée]biteur\s+)?(?:au\s+\d{2}[./-]\d{2}[./-]\d{2}\s+)?([\d\s]+[,\.]\d{2})', False),
    ]
    m_close = None
    for pat, _ in close_patterns:
        m_close = re.search(pat, text, re.IGNORECASE)
        if m_close:
            v = parse_amount(m_close.group(1))
            if v is not None:
                # Regarder le libellé situé juste avant le montant pour le signe.
                prefix = m_close.group(0).upper()
                info['balance_close'] = -v if 'DÉBITEUR' in prefix or 'DEBITEUR' in prefix else v
                break
    if not m_close and m_open:
        # Si pas de "Nouveau solde", solde de clôture = solde d'ouverture (mois vide).
        info['balance_close'] = info['balance_open']

    # Détecter explicitement un mois sans mouvement pour produire un OFX valide
    if re.search(r'Total\s+des\s+mouvements\s+0[,\.]00\s+0[,\.]00', text, re.IGNORECASE):
        info['_empty_period'] = True

    return info


# ════════════════════════════════════════════════════════════════════════════
# PARSEUR UNIVERSEL
# ════════════════════════════════════════════════════════════════════════════

_COL_SYNONYMS = {
    'date':   ['date','date opé','date opé.','date opération','date val','date valeur','date comptable','valeur','jour','date op'],
    'label':  ['libellé','libelle','opération','operation','description','motif','désignation','nature','détail','detail','mouvement','intitulé','label','wording','particulars','narration'],
    'debit':  ['débit','debit','débit (euro)','debit (euro)','sorties','sortie','retrait','retraits','paiements','débit fcfa','débit xof','withdrawals','withdrawal','payments','dr','déb','deb'],
    'credit': ['crédit','credit','crédit (euro)','credit (euro)','entrées','entrée','versement','versements','encaissements','crédit fcfa','crédit xof','deposits','deposit','receipts','cr','cré','cred'],
    'amount': ['montant','amount','somme','mouvement','débit/crédit','debit/credit','montant net','net'],
    'balance':['solde','balance','solde après','running balance'],
}

_DATE_PATTERNS = [
    (r'^(\d{2})[/\-\.](\d{2})[/\-\.](\d{4})$', 'dmy4'),
    (r'^(\d{4})[/\-\.](\d{2})[/\-\.](\d{2})$', 'ymd4'),
    (r'^(\d{2})[/\-\.](\d{2})[/\-\.](\d{2})$', 'dmy2'),
    (r'^(\d{2})[/\-\.](\d{2})$',                'dm'),
    (r'^(\d{8})$',                               'Ymd8'),
]

def _match_col(cell_text, col_type):
    if not cell_text: return False
    t = str(cell_text).strip().lower()
    for syn in _COL_SYNONYMS[col_type]:
        if t == syn or t.startswith(syn + ' ') or t.startswith(syn + '('): return True
    return False

def _detect_header_row(table):
    for row_idx, row in enumerate(table[:20]):
        col_map = {}
        for col_idx, cell in enumerate(row):
            if not cell: continue
            for ctype in ('date','label','debit','credit','amount','balance'):
                if ctype not in col_map and _match_col(str(cell), ctype):
                    col_map[ctype] = col_idx
        has_date  = 'date' in col_map
        has_money = ('debit' in col_map and 'credit' in col_map) or 'amount' in col_map
        if has_date and has_money:
            return row_idx, col_map
    return None, {}

def _parse_date_universal(raw, year_hint=None):
    if not raw: return None
    raw = str(raw).strip()
    raw = re.sub(r'^[A-Za-zÀ-ÿ]+\.?\s*', '', raw).strip()
    raw = raw.split('\n')[0].strip()
    for pattern, fmt in _DATE_PATTERNS:
        m = re.match(pattern, raw)
        if not m: continue
        if fmt == 'dmy4': return f"{m.group(3)}{m.group(2).zfill(2)}{m.group(1).zfill(2)}"
        elif fmt == 'ymd4': return f"{m.group(1)}{m.group(2).zfill(2)}{m.group(3).zfill(2)}"
        elif fmt == 'dmy2':
            yy = int(m.group(3))
            return f"{2000+yy if yy<=30 else 1900+yy}{m.group(2).zfill(2)}{m.group(1).zfill(2)}"
        elif fmt == 'dm':
            yr = str(year_hint) if year_hint else str(datetime.now().year)
            return f"{yr}{m.group(2).zfill(2)}{m.group(1).zfill(2)}"
        elif fmt == 'Ymd8':
            s = m.group(0); return f"{s[:4]}{s[4:6]}{s[6:8]}"
    return None

def _parse_amount_cell(cell_text):
    if not cell_text: return None
    s = str(cell_text).strip().replace('\xa0',' ').replace('\u202f',' ').replace('\n',' ').strip()
    s = re.sub(r'[€$£FCFAXOF]','',s,flags=re.IGNORECASE).strip().replace('*','').strip()
    if not s or s in ('.', ',', '-', '—', '–', ''): return None
    negative = False
    if s.startswith('(') and s.endswith(')'): s = s[1:-1].strip(); negative = True
    if s.startswith('-'): negative = True; s = s[1:].strip()
    elif s.startswith('+'): s = s[1:].strip()
    s_nospace = s.replace(' ','')
    m = re.match(r'^(\d{1,3}(?:[.,]\d{3})+)[,.](\d{2})$', s_nospace)
    if m:
        integer_part = re.sub(r'[,.]','',m.group(1))
        val = float(f"{integer_part}.{m.group(2)}")
        return -val if negative else val
    m2 = re.match(r'^(\d+)[,.](\d{1,2})$', s_nospace)
    if m2:
        val = float(f"{m2.group(1)}.{m2.group(2)}")
        return -val if negative else val
    m3 = re.match(r'^\d[\d\s]*\d$|^\d$', s)
    if m3:
        val = float(s.replace(' ',''))
        return -val if negative else val
    return None

def _extract_universal_header(pages_text):
    text = ' '.join(pages_text[:3])
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    # Utilise extract_iban() centralisé pour cohérence avec tous les autres parsers
    info['iban'] = extract_iban(text)
    m1 = re.search(
        r'(?:du|from|de|period[e]?\s*:?)\s*(\d{1,2}[/\-.]\d{2}[/\-.]\d{2,4})'
        r'\s*(?:au|to|[àa]|\-)\s*(\d{1,2}[/\-.]\d{2}[/\-.]\d{2,4})',
        text, re.IGNORECASE)
    if m1:
        info['period_start'] = m1.group(1).replace('-','/').replace('.','/') 
        info['period_end']   = m1.group(2).replace('-','/').replace('.','/')
    return info

def _universal_parse_path(pdf_path, pages_text):
    info = _extract_universal_header(pages_text)
    year_hint = _year_from_text(' '.join(pages_text[:2]))
    txns = []
    SKIP_LABELS = {'TOTAL','TOTAUX','SOLDE','SOUS-TOTAL','REPORT','A REPORTER',
                   'NOUVEAU SOLDE','ANCIEN SOLDE','SOLDE INITIAL','SOLDE FINAL'}
    TABLE_SETTINGS_LIST = [
        {"vertical_strategy":"text","horizontal_strategy":"text","snap_tolerance":4,"join_tolerance":4},
        {"vertical_strategy":"lines","horizontal_strategy":"lines","snap_tolerance":3},
        {"vertical_strategy":"lines","horizontal_strategy":"text","snap_tolerance":4},
    ]
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            table = None
            for settings in TABLE_SETTINGS_LIST:
                t = page.extract_table(settings)
                if t and len(t) >= 3:
                    table = t; break
            if not table: continue
            table_clean = [[str(c).replace('\n',' ').strip() if c else '' for c in row] for row in table]
            header_idx, col_map = _detect_header_row(table_clean)
            if header_idx is None: continue
            for row in table_clean[header_idx + 1:]:
                if not any(row): continue
                date_col = col_map.get('date')
                if date_col is None or date_col >= len(row): continue
                date_ofx = _parse_date_universal(row[date_col], year_hint)
                if not date_ofx: continue
                label_col = col_map.get('label')
                label = row[label_col].strip() if (label_col is not None and label_col < len(row)) else row[date_col]
                label_up = label.upper().strip()
                if not label or len(label) < 2: continue
                if any(skip in label_up for skip in SKIP_LABELS): continue
                if re.match(r'^[\d\s.,\-]+$', label): continue
                amount = None
                if 'debit' in col_map and 'credit' in col_map:
                    d_col, c_col = col_map['debit'], col_map['credit']
                    dv = _parse_amount_cell(row[d_col] if d_col < len(row) else '')
                    cv = _parse_amount_cell(row[c_col] if c_col < len(row) else '')
                    if dv and dv > 0: amount = -dv
                    elif cv and cv > 0: amount = cv
                elif 'amount' in col_map:
                    a_col = col_map['amount']
                    amount = _parse_amount_cell(row[a_col] if a_col < len(row) else '')
                if amount is None or amount == 0.0: continue
                name, memo = smart_label(label, [])
                txn = _make_txn(date_ofx, amount, name, memo)
                if txn: txns.append(txn)

    # ── Fallback texte ligne par ligne (banques non reconnues ou sans table) ──
    # Analyse chaque ligne à la recherche du pattern : DATE ... MONTANT
    # Fonctionne pour tout relevé avec des montants en fin de ligne.
    if not txns:
        full = '\n'.join(pages_text)
        full = full.replace('\xa0', ' ').replace('\u202f', ' ')
        SKIP_TEXT = ('SOLDE', 'TOTAL', 'TOTAUX', 'REPORT', 'DATE', 'VALEUR',
                     'LIBELLÉ', 'LIBELLE', 'DÉBIT', 'DEBIT', 'CRÉDIT', 'CREDIT',
                     'EUROS', 'MONTANT', 'PAGE', 'SUITE', 'VERSO', 'REF :',
                     'IBAN', 'BIC', 'AGENCE', 'COMPTE', 'TITULAIRE', 'ADRESSE',)
        # Regex générique : DD/MM/YYYY (optionnellement suivi d'une 2e date) + libellé + montant(s)
        DATE_RE = re.compile(r'^(\d{2}[/\-.]\d{2}[/\-.]\d{2,4})')
        AMT_RE  = re.compile(r'([\d]{1,3}(?:[.\s]\d{3})*[,]\d{2}|[\d]+[,]\d{2}|[\d]+[.]\d{2})')

        prev_solde = None
        lines = full.splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            m_date = DATE_RE.match(line)
            if not m_date:
                continue
            date_raw = m_date.group(1).replace('.', '/').replace('-', '/')
            date_ofx = _parse_date_universal(date_raw, year_hint)
            if not date_ofx:
                continue

            line_up = line.upper()
            if any(kw in line_up for kw in SKIP_TEXT):
                continue

            # Trouver tous les montants dans la ligne
            amounts_found = AMT_RE.findall(line)
            if not amounts_found:
                continue
            amounts_vals = [v for v in (parse_amount(a) for a in amounts_found) if v and v > 0.5]
            if not amounts_vals:
                continue

            # Extraire le libellé : retirer date(s) en début + montants en fin
            label_part = line
            # Supprimer jusqu'à 2 dates en début de ligne
            label_part = DATE_RE.sub('', label_part, count=1).strip()
            label_part = DATE_RE.sub('', label_part, count=1).strip()
            # Supprimer les montants numériques de fin
            label_part = re.sub(r'[\d\s.,]+$', '', label_part).strip()
            # Supprimer les caractères parasites restants
            label_part = re.sub(r'^[^\w]+', '', label_part).strip()

            if not label_part or len(label_part) < 3:
                continue
            if not re.search(r'[A-Za-zÀ-ÿ]{2,}', label_part):
                continue
            label_up2 = label_part.upper()
            if any(kw in label_up2 for kw in SKIP_TEXT):
                continue
            if re.match(r'^[\d\s/\-.,]+$', label_part):
                continue

            # Déterminer sens (débit/crédit) par variation de solde ou mots-clés
            is_credit = None
            if len(amounts_vals) >= 2:
                # Dernier montant = solde courant, avant-dernier = opération
                solde_courant = amounts_vals[-1]
                montant_op    = amounts_vals[-2]
                if prev_solde is not None:
                    diff = solde_courant - prev_solde
                    if diff > 0.5:
                        is_credit = True
                    elif diff < -0.5:
                        is_credit = False
                prev_solde = solde_courant
                amt = montant_op
            else:
                amt = amounts_vals[0]

            # Fallback mots-clés si sens non déterminé par solde
            if is_credit is None:
                CREDIT_KW = ('VIR ', 'VIREMENT', 'REGLEMENT', 'REMBOURSEMENT',
                             'VERSEMENT', 'AVOIR', 'RETOUR', 'REMISE', 'CREDIT',
                             'EDENRED', 'DELIVEROO', 'PLUXEE', 'BIMPLI', 'UBER',
                             'QUATRA', 'SCI ', 'RECETTE')
                DEBIT_KW  = ('PRLV', 'PRELEVEMENT', 'PAIEMENT CB', 'PAIEMENT PSC',
                             'PREL ', 'FACT ', 'COTISATION', 'ABONNEMENT',
                             'COMMISSION', 'FRAIS', 'AGIOS', 'RETRAIT',
                             'CHEQUE', 'LOYER', 'EDF', 'ORANGE', 'DGFIP',
                             'GENERALI', 'MAXANCE', 'SURAVENIR')
                if any(k in label_up2 for k in CREDIT_KW):
                    is_credit = True
                elif any(k in label_up2 for k in DEBIT_KW):
                    is_credit = False
                else:
                    is_credit = False  # défaut conservateur

            signed = amt if is_credit else -amt
            name, memo = smart_label(label_part, [])
            txn = _make_txn(date_ofx, signed, name, memo)
            if txn:
                txns.append(txn)

    return info, [t for t in txns if t is not None]

# ════════════════════════════════════════════════════════════════════════════
# CRÉDIT MUTUEL
# Format : Date | Date valeur | Opération | Débit EUROS | Crédit EUROS
# Relevé « RELEVE ET INFORMATIONS BANCAIRES » — Eurocompte Pro / Compte courant
# Particularités :
#   • La date opération et la date valeur sont toutes deux au format DD/MM/YYYY
#   • Le libellé principal est sur la ligne de date ; les lignes suivantes
#     (sans date) sont des lignes de continuation (mémo : référence, ICS, RUM…)
#   • Deux colonnes montant distinctes (Débit / Crédit) en fin de ligne
# Positions mesurées sur relevés CM réels :
#   Date op    : x0 < 70   (DD/MM/YYYY)
#   Date val   : x0 ≈ 70–130 (DD/MM/YYYY)
#   Libellé    : x0 ≈ 130–430
#   Débit      : x0 ≈ 430–530
#   Crédit     : x0 ≈ 530+
# ════════════════════════════════════════════════════════════════════════════

def _extract_cm_header(pages_text):
    text = ' '.join(pages_text[:3])
    info = {'iban': '', 'period_start': '', 'period_end': '',
            'balance_open': 0.0, 'balance_close': 0.0}

    # IBAN
    info['iban'] = extract_iban(text)

    # Période — "31 octobre 2025" → on déduit fin de mois, ou cherche dates explicites
    m_per = re.search(
        r'(?:du|Du)\s+(\d{2}/\d{2}/\d{4})\s+(?:au|Au)\s+(\d{2}/\d{2}/\d{4})',
        text, re.IGNORECASE
    )
    if m_per:
        info['period_start'] = m_per.group(1)
        info['period_end']   = m_per.group(2)
    else:
        # Chercher la date du relevé (ex: "31 octobre 2025")
        MOIS = {'janvier':'01','février':'02','fevrier':'02','mars':'03','avril':'04',
                'mai':'05','juin':'06','juillet':'07','août':'08','aout':'08',
                'septembre':'09','octobre':'10','novembre':'11','décembre':'12','decembre':'12'}
        m_date = re.search(
            r'(\d{1,2})\s+(' + '|'.join(MOIS.keys()) + r')\s+(20\d{2})',
            text, re.IGNORECASE
        )
        if m_date:
            day = m_date.group(1).zfill(2)
            month = MOIS.get(m_date.group(2).lower(), '01')
            year  = m_date.group(3)
            info['period_end'] = f"{day}/{month}/{year}"
            info['period_start'] = f"01/{month}/{year}"

    # Soldes — "SOLDE CREDITEUR AU 30/09/2025  4.286,81"
    for pat in [
        r'SOLDE\s+(?:CREDITEUR|DEBITEUR)\s+AU\s+\d{2}/\d{2}/\d{4}\s+([\d\s.,]+)',
        r'SOLDE\s+(?:INITIAL|D\'OUVERTURE)\s+([\d\s.,]+)',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            v = parse_amount(m.group(1).strip().split()[0])
            if v: info['balance_open'] = v; break

    # Solde final
    m_close = re.search(
        r'SOLDE\s+CREDITEUR\s+AU\s+\d{2}/\d{2}/\d{4}\s+([\d\s.,]+)',
        text, re.IGNORECASE
    )
    if m_close:
        vals = re.findall(r'[\d]+[.,][\d]{2}', text)
        # Prendre la dernière occurrence de "SOLDE CREDITEUR"
        all_solde = re.findall(
            r'SOLDE\s+CREDITEUR\s+AU\s+\d{2}/\d{2}/\d{4}\s+([\d.,\s]+)',
            text, re.IGNORECASE
        )
        if all_solde:
            v = parse_amount(all_solde[-1].strip().split()[0])
            if v: info['balance_close'] = v

    # "Total des mouvements  12.525,90  14.417,56" + "SOLDE CREDITEUR AU 31/10/2025  6.178,47"
    m_final = re.search(
        r'R[eé]f\s*:\s*\d+\s+SOLDE\s+CREDITEUR\s+AU\s+\d{2}/\d{2}/\d{4}\s+([\d.,\s]+)',
        text, re.IGNORECASE
    )
    if m_final:
        v = parse_amount(m_final.group(1).strip().split()[0])
        if v: info['balance_close'] = v

    return info


def parse_cm(pages_words, pages_text):
    """Parseur Crédit Mutuel — format RELEVE ET INFORMATIONS BANCAIRES.

    Stratégie :
    1. Parsing mot-par-mot (pdfplumber words) avec distinction colonne débit/crédit
       par mots-clés d'opération (plus fiable que la position x qui varie).
    2. Fallback : parsing texte brut ligne par ligne.
    """
    info = _extract_cm_header(pages_text)
    year = _year_from_text(' '.join(pages_text[:2]))
    txns = []

    SKIP_UP = {
        'SOLDE', 'TOTAL', 'TOTAUX', 'DATE', 'VALEUR', 'OPERATION', 'OPÉRATION',
        'DÉBIT', 'DEBIT', 'CRÉDIT', 'CREDIT', 'EUROS', 'LIBELLÉ', 'LIBELLE',
        'REPORT', 'A REPORTER', 'SUITE', 'VERSO', 'PAGE', 'RELEVE',
        'RELEVÉ', 'INFORMATIONS', 'BANCAIRES',
    }
    # Seuil de séparation débit/crédit mesuré sur relevés CM réels :
    # Débit  : x0 ≈ 428–499  (colonne gauche)
    # Crédit : x0 ≈ 500–535  (colonne droite)
    CM_CREDIT_X_MIN = 500  # si x0 du montant >= cette valeur → crédit

    # Mots-clés de secours quand la position est ambiguë ou le montant absent
    CREDIT_KW = (
        'REGLEMENT AFFILIES', 'STICHTING CUSTODIAN', 'EDENRED FRANCE',
        'DELIVEROO FRANCE', 'PLUXEE FRANCE', 'QUATRA FRANCE',
    )
    DEBIT_KW = (
        'PRLV SEPA', 'PRLV ', 'PRELEVEMENT', 'PAIEMENT CB', 'PAIEMENT PSC',
        'PREL EURO', 'FACT SGT', 'LOYER LOCAL', 'VIR SEPA LOYER',
    )

    def _cm_is_skip(label):
        up = label.upper().strip()
        if up in SKIP_UP: return True
        for kw in ('SOLDE ', 'TOTAL ', 'SUITE AU', '<<SUITE', 'SOUS RESERVE',
                   'DONT TVA', 'INFORMATION SUR'):
            if up.startswith(kw): return True
        return False

    def _cm_date(row):
        for w in row:
            if w['x0'] < 95 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text']):
                return w['text']
        return ''

    for pw in pages_words:
        rows = group_words_by_row(pw, tol=3.0)
        i = 0
        while i < len(rows):
            row = rows[i]
            date_str = _cm_date(row)
            if not date_str:
                i += 1; continue

            # Libellé : mots entre fin des dates et début colonnes montants
            # Dates : x0 < 145, montants : x0 >= 415
            label_words = [w for w in row
                           if 145 <= w['x0'] < 415
                           and not re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
            label = ' '.join(w['text'] for w in label_words).strip()

            if _cm_is_skip(label):
                i += 1; continue

            # Montant : tous les tokens à x0 >= 415
            amount_words = [w for w in row if w['x0'] >= 415]
            raw_amount_text = ' '.join(w['text'] for w in amount_words).strip()
            amt = parse_amount(raw_amount_text) if raw_amount_text else None
            # Position du premier token montant pour déduire la colonne
            amount_x0 = amount_words[0]['x0'] if amount_words else None

            # Lignes de continuation (mémo) : pas de date op
            memo_parts = []
            j = i + 1
            while j < len(rows):
                nr = rows[j]
                if _cm_date(nr):
                    break
                cont_words = [w for w in nr
                              if 145 <= w['x0'] < 415
                              and not re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
                cont = ' '.join(w['text'] for w in cont_words).strip()
                if cont and not _cm_is_skip(cont):
                    memo_parts.append(cont)
                # Si montant absent de la ligne principale, chercher ici
                if amt is None:
                    alt_amount_words = [w for w in nr if w['x0'] >= 415]
                    alt_raw = ' '.join(w['text'] for w in alt_amount_words).strip()
                    if alt_raw:
                        amt = parse_amount(alt_raw)
                        if alt_amount_words:
                            amount_x0 = alt_amount_words[0]['x0']
                j += 1
            i = j

            if not label or not re.search(r'[A-Za-zÀ-ÿ]{2,}', label):
                continue
            if amt is None or amt == 0:
                continue

            date_ofx = date_full_to_ofx(date_str)
            if not re.match(r'^\d{8}$', date_ofx):
                continue

            # Déterminer débit / crédit — CRITÈRE PRINCIPAL : position x du montant
            # Crédit si x0 >= CM_CREDIT_X_MIN (colonne droite), sinon débit
            label_up = label.upper()
            if amount_x0 is not None:
                is_credit = (amount_x0 >= CM_CREDIT_X_MIN)
            else:
                # Fallback mots-clés si pas de position
                if any(k in label_up for k in CREDIT_KW):
                    is_credit = True
                elif any(k in label_up for k in DEBIT_KW):
                    is_credit = False
                else:
                    is_credit = False

            signed = amt if is_credit else -amt
            name, memo = smart_label(label, memo_parts)
            txns.append(_make_txn(date_ofx, signed, name, memo))

    # Fallback texte brut si pdfplumber n'a pas donné de words utilisables
    if not txns:
        full = '\n'.join(pages_text)
        full = full.replace('\xa0', ' ').replace('\u202f', ' ')
        SKIP_TEXT = ('SOLDE', 'TOTAL', 'TOTAUX', 'RELEVE', 'RELEVÉ', 'DATE',
                     'VALEUR', 'DÉBIT', 'DEBIT', 'CRÉDIT', 'CREDIT', 'EUROS',
                     'SUITE', 'VERSO', 'PAGE', 'INFORMATIONS', 'BANCAIRES', 'REF :',
                     'DONT TVA', 'SOUS RESERVE', 'INFORMATION SUR')
        CREDIT_KW2 = ('VIR ', 'REGLEMENT', 'EDENRED', 'DELIVEROO', 'PLUXEE',
                      'BIMPLI', 'UBER', 'M&M', 'QUATRA', 'SCI ')
        DEBIT_KW2  = ('PRLV', 'PAIEMENT', 'PREL ', 'FACT SGT', 'LOYER')
        for line in full.splitlines():
            line = line.strip()
            m = re.match(r'^(\d{2}/\d{2}/\d{4})\s+\d{2}/\d{2}/\d{4}\s+(.+?)\s+([\d.]+[,]\d{2})\s*$', line)
            if not m:
                m = re.match(r'^(\d{2}/\d{2}/\d{4})\s+(.+?)\s+([\d.]+[,]\d{2})\s*$', line)
            if not m: continue
            date_str, label, amt_str = m.group(1), m.group(2).strip(), m.group(3)
            label_up = label.upper()
            if any(kw in label_up for kw in SKIP_TEXT): continue
            if not re.search(r'[A-Za-zÀ-ÿ]{2,}', label): continue
            amt = parse_amount(amt_str)
            if not amt: continue
            date_ofx = date_full_to_ofx(date_str)
            if not re.match(r'^\d{8}$', date_ofx): continue
            is_credit = (any(k in label_up for k in CREDIT_KW2) and
                         not any(k in label_up for k in DEBIT_KW2))
            signed = amt if is_credit else -amt
            txns.append(_make_txn(date_ofx, signed, label))

    return info, [t for t in txns if t is not None]


# ════════════════════════════════════════════════════════════════════════════
# PARSEUR CRÉDIT MUTUEL DE BRETAGNE (CMB) — DÉDIÉ, DISTINCT DE parse_cm()
# ════════════════════════════════════════════════════════════════════════════
# CMB (caisse régionale, gabarit "Relevé de Compte") est un format PDF
# différent du Crédit Mutuel "générique" traité par parse_cm() ci-dessus :
#   - Colonnes Date / Date de Valeur / Opération / Débit / Crédit
#   - Le libellé démarre à x0 ≈ 136-140 pt (juste après la date de valeur,
#     qui se termine à x1 ≈ 128-133 pt)
#   - Les mots sont fusionnés sans espace à la tolérance x par défaut de
#     pdfplumber (ex: "VIRCAISSEDESREGLEMENTSPECUNI"), on réextrait donc
#     avec une tolérance plus fine quand le chemin du PDF est disponible.
# Ce parseur ne touche à AUCUN comportement de parse_cm() (Crédit Mutuel
# classique / CIC), qui reste inchangé.

def _extract_cmb_header(pages_text, _pdf_path=''):
    # Réextraction du texte avec tolérance x fine pour restaurer les espaces
    # entre mots (le gabarit CMB fusionne les mots à tolérance par défaut).
    if _pdf_path and _PDFPLUMBER_OK:
        try:
            with pdfplumber.open(_pdf_path) as _pdf:
                pages_text = [p.extract_text(x_tolerance=1.3) or '' for p in _pdf.pages]
        except Exception:
            pass

    text = ' '.join(pages_text[:3])
    text_all = ' '.join(pages_text)  # le "NOUVEAU SOLDE" est sur la dernière page
    info = {'iban': '', 'period_start': '', 'period_end': '',
            'balance_open': 0.0, 'balance_close': 0.0}

    info['iban'] = extract_iban(text)

    # Soldes CMB — format toutes lettres :
    #   "ANCIEN SOLDE CRÉDITEUR AU 07 FÉVRIER 2026 8 990,23 €"
    #   "NOUVEAU SOLDE CRÉDITEUR AU 07 MARS 2026 21 463,40 €"
    MOIS_CMB = ('JANVIER','FÉVRIER','FEVRIER','MARS','AVRIL','MAI','JUIN','JUILLET',
                'AOÛT','AOUT','SEPTEMBRE','OCTOBRE','NOVEMBRE','DÉCEMBRE','DECEMBRE')
    _MOIS_NUM = {'JANVIER':'01','FÉVRIER':'02','FEVRIER':'02','MARS':'03','AVRIL':'04',
                 'MAI':'05','JUIN':'06','JUILLET':'07','AOÛT':'08','AOUT':'08',
                 'SEPTEMBRE':'09','OCTOBRE':'10','NOVEMBRE':'11','DÉCEMBRE':'12','DECEMBRE':'12'}

    m_open = re.search(
        r'ANCIEN\s+SOLDE\s+(?:CR[ÉE]DITEUR|D[ÉE]BITEUR)\s+AU\s+\d{1,2}\s+(?:'
        + '|'.join(MOIS_CMB) + r')\s+20\d{2}\s+([\d\s.,]+?)\s*€',
        text, re.IGNORECASE)
    if m_open:
        v = parse_amount(m_open.group(1))
        if v is not None: info['balance_open'] = v

    m_close = re.search(
        r'NOUVEAU\s+SOLDE\s+(?:CR[ÉE]DITEUR|D[ÉE]BITEUR)\s+AU\s+\d{1,2}\s+(?:'
        + '|'.join(MOIS_CMB) + r')\s+20\d{2}\s+([\d\s.,]+?)\s*€',
        text_all, re.IGNORECASE)
    if m_close:
        v = parse_amount(m_close.group(1))
        if v is not None: info['balance_close'] = v

    m_start_date = re.search(
        r'ANCIEN\s+SOLDE\s+(?:CR[ÉE]DITEUR|D[ÉE]BITEUR)\s+AU\s+(\d{1,2})\s+('
        + '|'.join(MOIS_CMB) + r')\s+(20\d{2})', text, re.IGNORECASE)
    if m_start_date:
        d, mo, y = m_start_date.group(1).zfill(2), m_start_date.group(2).upper(), m_start_date.group(3)
        info['period_start'] = f"{d}/{_MOIS_NUM.get(mo,'01')}/{y}"

    m_end_date = re.search(
        r'NOUVEAU\s+SOLDE\s+(?:CR[ÉE]DITEUR|D[ÉE]BITEUR)\s+AU\s+(\d{1,2})\s+('
        + '|'.join(MOIS_CMB) + r')\s+(20\d{2})', text_all, re.IGNORECASE)
    if m_end_date:
        d, mo, y = m_end_date.group(1).zfill(2), m_end_date.group(2).upper(), m_end_date.group(3)
        info['period_end'] = f"{d}/{_MOIS_NUM.get(mo,'01')}/{y}"

    return info


def parse_cmb(pages_words, pages_text, _pdf_path=''):
    """Parseur dédié Crédit Mutuel de Bretagne (CMB) — gabarit "Relevé de
    Compte" (Date / Date de Valeur / Opération / Débit / Crédit).

    Indépendant de parse_cm() : n'affecte aucun autre format Crédit Mutuel.
    """
    info = _extract_cmb_header(pages_text, _pdf_path=_pdf_path)
    txns = []

    # Réextraction avec tolérance x réduite pour restaurer les espaces entre
    # mots (le gabarit CMB fusionne les mots à tolérance par défaut, ex:
    # "VIRCAISSEDESREGLEMENTSPECUNI").
    if _pdf_path and _PDFPLUMBER_OK:
        try:
            with pdfplumber.open(_pdf_path) as _pdf:
                pages_words = [p.extract_words(keep_blank_chars=False, x_tolerance=1.3)
                               for p in _pdf.pages]
        except Exception:
            pass

    SKIP_UP = {
        'SOLDE', 'TOTAL', 'TOTAUX', 'DATE', 'VALEUR', 'OPERATION', 'OPÉRATION',
        'DÉBIT', 'DEBIT', 'CRÉDIT', 'CREDIT', 'EUROS', 'LIBELLÉ', 'LIBELLE',
        'REPORT', 'A REPORTER', 'SUITE', 'VERSO', 'PAGE', 'RELEVE',
        'RELEVÉ', 'INFORMATIONS', 'BANCAIRES',
    }
    # Seuil de séparation débit/crédit mesuré sur relevés CMB réels :
    # Débit  : x0 ≈ 428-499  (colonne gauche)
    # Crédit : x0 ≈ 500-540  (colonne droite)
    CMB_CREDIT_X_MIN = 500

    def _cmb_is_skip(label):
        up = label.upper().strip()
        if up in SKIP_UP: return True
        for kw in ('SOLDE ', 'TOTAL ', 'SUITE AU', '<<SUITE', 'SOUS RESERVE',
                   'DONT TVA', 'INFORMATION SUR'):
            if up.startswith(kw): return True
        return False

    def _cmb_date(row):
        for w in row:
            if w['x0'] < 100 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text']):
                return w['text']
        return ''

    for pw in pages_words:
        rows = group_words_by_row(pw, tol=3.0)
        i = 0
        while i < len(rows):
            row = rows[i]
            date_str = _cmb_date(row)
            if not date_str:
                i += 1; continue

            # Libellé : mots entre fin de la date de valeur (x1 ≈ 128-133)
            # et début des colonnes montants (x0 ≈ 415). Le libellé démarre
            # dès x0 ≈ 136-140 sur ce gabarit — seuil bas fixé à 128.
            label_words = [w for w in row
                           if 128 <= w['x0'] < 415
                           and not re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
            label = ' '.join(w['text'] for w in label_words).strip()

            if _cmb_is_skip(label):
                i += 1; continue

            amount_words = [w for w in row if w['x0'] >= 415]
            raw_amount_text = ' '.join(w['text'] for w in amount_words).strip()
            amt = parse_amount(raw_amount_text) if raw_amount_text else None
            amount_x0 = amount_words[0]['x0'] if amount_words else None

            memo_parts = []
            j = i + 1
            while j < len(rows):
                nr = rows[j]
                if _cmb_date(nr):
                    break
                cont_words = [w for w in nr
                              if 128 <= w['x0'] < 415
                              and not re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
                cont = ' '.join(w['text'] for w in cont_words).strip()
                if cont and not _cmb_is_skip(cont):
                    memo_parts.append(cont)
                if amt is None:
                    alt_amount_words = [w for w in nr if w['x0'] >= 415]
                    alt_raw = ' '.join(w['text'] for w in alt_amount_words).strip()
                    if alt_raw:
                        amt = parse_amount(alt_raw)
                        if alt_amount_words:
                            amount_x0 = alt_amount_words[0]['x0']
                j += 1
            i = j

            if not label or not re.search(r'[A-Za-zÀ-ÿ]{2,}', label):
                continue
            if amt is None or amt == 0:
                continue

            date_ofx = date_full_to_ofx(date_str)
            if not re.match(r'^\d{8}$', date_ofx):
                continue

            is_credit = (amount_x0 >= CMB_CREDIT_X_MIN) if amount_x0 is not None else False
            signed = amt if is_credit else -amt
            name, memo = smart_label(label, memo_parts)
            txns.append(_make_txn(date_ofx, signed, name, memo))

    return info, [t for t in txns if t is not None]


# ════════════════════════════════════════════════════════════════════════════
# PARSEURS AFRICAINS DÉDIÉS
# ════════════════════════════════════════════════════════════════════════════

def _afr_header(pages_text):
    """Header commun pour les banques africaines."""
    text = ' '.join(pages_text[:3])
    info = {'iban': '', 'period_start': '', 'period_end': '',
            'balance_open': 0.0, 'balance_close': 0.0}

    # ── 1. RIB explicitement labellisé : Code Banque / Agence / Compte / Clé ──
    # Ex tableau : "Code Banque  Agence   Compte         Clé RIB"
    #              "SN213        01001    02341624101    33"
    rib_table = re.search(
        r'(?:Code\s*Banque|Banque)\s*[:\|]?\s*([A-Z]{0,2}\d{3,5})'
        r'.{0,40?}(?:Agence|Guichet)\s*[:\|]?\s*(\d{4,6})'
        r'.{0,40?}(?:N[°o]?\s*(?:de\s*)?[Cc]ompte|Compte)\s*[:\|]?\s*(\d{8,14})'
        r'.{0,40?}(?:Cl[eé]\s*(?:RIB)?|RIB)\s*[:\|]?\s*(\d{2})\b',
        text, re.IGNORECASE | re.DOTALL
    )
    if rib_table:
        info['_rib_bank']    = rib_table.group(1)
        info['_rib_agency']  = rib_table.group(2)
        info['_rib_account'] = rib_table.group(3)
        info['_rib_key']     = rib_table.group(4)

    # ── 2. RIB compact sur une ligne : "SN213 01001 02341624101 33" ───────────
    _SEP = r'[\s\t\-]+'
    if not info.get('_rib_bank'):
        rib_match = re.search(
            r'\b([A-Z]{0,2}\d{3,5})' + _SEP +
            r'(\d{4,6})'             + _SEP +
            r'(\d{8,14})'            + _SEP +
            r'(\d{2})\b',
            text
        )
        if rib_match:
            info['_rib_bank']    = rib_match.group(1)
            info['_rib_agency']  = rib_match.group(2)
            info['_rib_account'] = rib_match.group(3)
            info['_rib_key']     = rib_match.group(4)

    # ── 2b. RIB format tiret BSIC/ECOBANK : "01001-00100029193-76" ─────────────
    if not info.get('_rib_bank'):
        rib_tiret = re.search(
            r'(?:Num[\xe9e]ro\s+de\s+compte|N[o\xb0]\s*compte)\s*[:\-]?\s*'
            r'(\d{5})-(\d{8,14})-(\d{2})',
            text, re.IGNORECASE
        )
        if rib_tiret:
            info['_rib_agency']  = rib_tiret.group(1)
            info['_rib_account'] = rib_tiret.group(2)
            info['_rib_key']     = rib_tiret.group(3)

    # ── 3. Extraction IBAN centralisée ───────────────────────────────────────
    if not info['iban']:
        info['iban'] = extract_iban(text)

    # ── 4. Si IBAN trouvé mais pas de code banque, dériver depuis l'IBAN ──────
    # Ne pas ecraser _rib_account si deja extrait par regex tiret (plus fiable)
    if info['iban'] and not info.get('_rib_bank'):
        try:
            b, ag, ac = iban_to_rib(info['iban'])
            if b and b != '00000':
                info['_rib_bank']   = b
                info['_rib_agency'] = info.get('_rib_agency') or ag
                # Ne pas ecraser le compte si deja capture par la regex tiret
                if not info.get('_rib_account'):
                    info['_rib_account'] = ac
                info['_rib_key']    = info.get('_rib_key') or ''
        except Exception:
            pass

    # ── 5. Numéro de compte brut (dernier recours) ───────────────────────────
    if not info['iban']:
        m4 = re.search(
            r'(?:N[°o°]\.?\s*(?:de\s*)?compte|Compte|COMPTE)\s*[:\-]?\s*'
            r'([\d]{5,20}(?:[\s\-]?\d{1,6})*)',
            text, re.IGNORECASE
        )
        if m4:
            info['iban'] = re.sub(r'[\s\-]', '', m4.group(1))

    # ── 6. Période ───────────────────────────────────────────────────────────
    m5 = re.search(r'(?:du|[Pp]ériode du?|[Pp]our la p[ée]riode du?|[Dd]u)\s+'
                   r'(\d{1,2}[/\-\.]\d{2}[/\-\.]\d{2,4})'
                   r'\s*(?:au|[àa]|[Aa]u)\s*'
                   r'(\d{1,2}[/\-\.]\d{2}[/\-\.]\d{2,4})',
                   text, re.IGNORECASE)
    if m5:
        info['period_start'] = m5.group(1).replace('-', '/').replace('.', '/')
        info['period_end']   = m5.group(2).replace('-', '/').replace('.', '/')

    # ── 7. Solde d'ouverture / clôture — format « Solde au DD/MM/YYYY  MONTANT »
    # (ex : relevés UBA). Recherché sur tout le texte (pas seulement les 3 premières pages).
    full_text_all = ' '.join(pages_text)
    solde_au = re.findall(
        r'Solde\s+au\s+\d{1,2}[/\-\.]\d{2}[/\-\.]\d{2,4}\s+([\d\s.,]+?)(?=\n|$)',
        full_text_all, re.IGNORECASE
    )
    if solde_au:
        v_first = parse_amount(solde_au[0])
        v_last = parse_amount(solde_au[-1])
        if v_first is not None and not info.get('balance_open'):
            info['balance_open'] = v_first
        if v_last is not None and not info.get('balance_close'):
            info['balance_close'] = v_last
    return info


# ── BSIC : format « Date Op | Date Val | Libellé | Débit | Crédit | Solde »
# Positions mesurées sur PDF réel :
#   Date opération : x0 ≈ 32    (dd/mm/yyyy)
#   Date valeur    : x0 ≈ 80    (dd/mm/yyyy) — à exclure du libellé
#   Libellé        : x0 ≈ 128-320
#   Débit          : x0 ≈ 355-410
#   Crédit         : x0 ≈ 440-500
#   Solde          : x0 ≈ 510+  (ignoré)
def parse_bsic(pages_words, pages_text, _pdf_path=''):
    info = _afr_header(pages_text)
    year = _year_from_text(' '.join(pages_text[:2]))
    txns = []
    SKIP = {'TOTAL','SOLDE','A REPORTER','REPORT','DATE','VALEUR','LIBELLÉ','LIBELLE',
            'DÉBIT','DEBIT','CRÉDIT','CREDIT','EXTRAIT','PÉRIODE','CODE','NOM',
            'PAGE SUR'}  # "PAGE" et "SUR" uniquement en mots entiers via regex ci-dessous

    def _is_skip_label(lbl):
        """Retourne True si le libellé est une ligne d'en-tête/pied à ignorer."""
        lbl_up = lbl.upper().strip()
        # Mots-clés exacts (libellé = entièrement ce mot)
        if lbl_up in SKIP:
            return True
        # Mots-clés en début de libellé (solde, total…)
        for kw in ('SOLDE', 'TOTAL', 'A REPORTER', 'REPORT', 'DÉBIT', 'DEBIT',
                   'CRÉDIT', 'CREDIT', 'DATE', 'VALEUR', 'LIBEL', 'EXTRAIT'):
            if lbl_up.startswith(kw):
                return True
        # "Page N sur N" — mot isolé SUR/PAGE uniquement s'il est le seul mot significatif
        if re.match(r'^PAGE\s+\d+\s+SUR\s+\d+$', lbl_up):
            return True
        return False

    # ── Extraction solde final depuis le texte ────────────────────────────────
    # "Solde (XOF) au 31/01/2024 : 8 728 070"
    full_text = ' '.join(pages_text)
    m_bal = re.search(
        r'Solde\s+\([A-Z]+\)\s+au\s+\d{2}/\d{2}/\d{4}\s*:\s*([\d\s]+)',
        full_text, re.IGNORECASE
    )
    if m_bal:
        raw_bal = re.sub(r'\s+', '', m_bal.group(1))
        try:
            info['balance_close'] = float(raw_bal)
        except ValueError:
            pass

    # "Solde initial (XOF) : 279 462"
    m_bal_open = re.search(
        r'Solde\s+initial\s+\([A-Z]+\)\s*:\s*([\d\s]+)',
        full_text, re.IGNORECASE
    )
    if m_bal_open:
        raw_bal_open = re.sub(r'\s+', '', m_bal_open.group(1))
        try:
            info['balance_open'] = float(raw_bal_open)
        except ValueError:
            pass

    # ── Extraction RIB/IBAN BSIC ─────────────────────────────────────────────
    # Format numéro de compte : "01001-00100029193-76 XOF"
    #   → Guichet=01001, Compte=00100029193, Clé=76
    # IBAN collé possible : "Code Iban : SN08SN11101001000100029193"

    m_compte = re.search(
        r'Num[eé]ro\s+de\s+compte\s*:\s*(\d{5})-(\d{8,14})-(\d{2})',
        full_text, re.IGNORECASE
    )
    if m_compte:
        info['_rib_agency']  = m_compte.group(1)   # ex: 01001
        info['_rib_account'] = m_compte.group(2)   # ex: 00100029193
        info['_rib_key']     = m_compte.group(3)   # ex: 76
        if not info.get('_rib_bank'):
            iban_raw = re.sub(r'\s+', '', info.get('iban', '')).upper()
            if re.match(r'^[A-Z]{2}\d{2}', iban_raw) and len(iban_raw) >= 9:
                info['_rib_bank'] = iban_raw[4:9]
            else:
                m_ib = re.search(r'Code\s+[Ii]ban\s*:\s*([A-Z]{2}\d{2}[A-Z0-9]{3,5})',
                                  full_text, re.IGNORECASE)
                if m_ib:
                    info['_rib_bank'] = m_ib.group(1)[4:9]

    # Valider/compléter l'IBAN depuis "Code Iban : SN08SN111..."
    if not info.get('iban') or not re.match(r'^[A-Z]{2}\d{2}', re.sub(r'\s+','',info.get('iban','')).upper()):
        m_ci = re.search(r'Code\s+[Ii]ban\s*:\s*([A-Z]{2}\d{2}[A-Z0-9\s]{14,32})',
                          full_text, re.IGNORECASE)
        if m_ci:
            raw = re.sub(r'\s+', '', m_ci.group(1)).upper()
            raw = re.sub(r'[^A-Z0-9]', '', raw)
            if len(raw) >= 15:
                info['iban'] = raw[:28]

    for pw in pages_words:
        rows = group_words_by_row(pw, tol=2.0)  # réduit pour BSIC (lignes serrées)
        i = 0
        while i < len(rows):
            row = rows[i]
            # Date opération : dd/mm/yyyy à x0 ≈ 32 (tolérance 70 pts)
            date_w = [w for w in row if w['x0'] < 70
                      and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
            if not date_w:
                i += 1; continue

            date_str = date_w[0]['text']
            date_ofx = date_full_to_ofx(date_str)

            # Libellé : x0 entre 120 et 335 sur la ligne courante
            label_words = [w for w in row if 120 <= w['x0'] < 335]
            label = ' '.join(w['text'] for w in label_words).strip()

            # Si le libellé est vide, chercher dans la ou les lignes précédentes
            # (cas BSIC : "Virmt fav. ALIOS FINANCE" sur la ligne juste avant la date)
            if not label and i > 0:
                # Chercher jusqu'à 3 lignes en arrière (sans date op)
                for k in range(i - 1, max(i - 4, -1), -1):
                    prev_row = rows[k]
                    prev_date = [w for w in prev_row if w['x0'] < 70
                                 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
                    if prev_date:
                        break  # une autre date op → stop
                    prev_label_words = [w for w in prev_row if 120 <= w['x0'] < 335]
                    prev_label = ' '.join(w['text'] for w in prev_label_words).strip()
                    if prev_label and not _is_skip_label(prev_label):
                        label = prev_label
                        break

            # Récupérer les lignes de continuation qui suivent (sans date op)
            j = i + 1
            memo_parts = []
            while j < len(rows):
                next_row = rows[j]
                next_date = [w for w in next_row if w['x0'] < 70
                             and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
                if next_date:
                    break
                cont_words = [w for w in next_row if 120 <= w['x0'] < 335]
                cont = ' '.join(w['text'] for w in cont_words).strip()
                if cont and not _is_skip_label(cont):
                    memo_parts.append(cont)
                j += 1
            i = j

            if not label or _is_skip_label(label):
                continue
            if re.match(r'^[\d\s/\-]+$', label):
                continue

            # Débit (x0 ≈ 355-415) / Crédit (x0 ≈ 440-510)
            debit_words  = [w for w in row if 340 <= w['x0'] < 430]
            credit_words = [w for w in row if 430 <= w['x0'] < 515]

            debit_amt  = _uba_join_amount(debit_words)
            credit_amt = _uba_join_amount(credit_words)

            name, memo = smart_label(label, memo_parts)
            if debit_amt and debit_amt > 0:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt and credit_amt > 0:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))

    # ── Fallback texte brut (PDF scanné via OCR ou pages_words vides) ──────────
    # Quand pages_words est vide (scan OCR), on parse le texte ligne par ligne.
    # Format BSIC OCR attendu par ligne :
    #   "01/10/2025 30/09/2025 RET ESP CP 2149951 MESSIN DRA   80 000   9 262 621"
    #   "07/10/2025 07/10/2025 V/V FAC FV2025 09 000260        867 300   948 931"
    # → date_op  date_val  libellé  [débit ou crédit]  solde
    if not txns:
        full_text_ocr = '\n'.join(pages_text)
        # Pré-traitement : normaliser espaces insécables
        full_text_ocr = full_text_ocr.replace('\xa0', ' ').replace('\u202f', ' ')

        # Extraire toutes les lignes de transaction + la suivante (solde précédent
        # pour déduire le sens débit/crédit)
        lines = full_text_ocr.split('\n')
        prev_solde = None

        for idx, line in enumerate(lines):
            line = line.strip()
            # Doit commencer par date_op dd/mm/yyyy
            m_date = re.match(r'^(\d{2}/\d{2}/\d{4})', line)
            if not m_date:
                continue
            date_str = m_date.group(1)
            date_ofx = date_full_to_ofx(date_str)

            line_up = line.upper()
            if any(kw in line_up for kw in ('SOLDE', 'TOTAL', 'LIBELLÉ', 'LIBELLE',
                                             'DÉBIT', 'DEBIT', 'CRÉDIT', 'CREDIT',
                                             'EXTRAIT', 'PAGE', 'PÉRIODE', 'CODE CLIENT')):
                continue

            # Extraire tous les montants de la ligne (≥ 100 pour ignorer numéros)
            # On cherche des nombres avec éventuels espaces comme séparateurs de milliers
            raw_amounts = re.findall(
                r'\b(\d{1,3}(?:\s\d{3})*(?:,\d{2})?)\b', line)
            amounts_vals = []
            for a in raw_amounts:
                v = parse_amount(a.replace(' ', '.'))
                if v is not None and v >= 100:
                    amounts_vals.append(v)

            if not amounts_vals:
                continue

            # Libellé : retirer date_op + date_val, puis enlever les montants de fin
            label_part = re.sub(r'^\d{2}/\d{2}/\d{4}\s*', '', line)    # date op
            label_part = re.sub(r'^\d{2}/\d{2}/\d{4}\s*', '', label_part)  # date val
            # Retirer les montants numériques de fin de chaîne
            label_part = re.sub(r'[\d\s,]+$', '', label_part).strip()
            label_part = clean_label(label_part)

            if not label_part or len(label_part) < 3 or _is_skip_label(label_part):
                continue

            # ── Déduire débit/crédit ─────────────────────────────────────────
            # Méthode 1 : variation de solde
            # Format : libellé  [montant_op]  solde_courant
            # Si on a ≥ 2 montants : le dernier est le solde, l'avant-dernier est l'opération
            # Si solde_courant < solde_précédent → débit, sinon crédit
            is_credit = None
            if len(amounts_vals) >= 2:
                solde_courant = amounts_vals[-1]
                montant_op    = amounts_vals[-2]
                if prev_solde is not None:
                    diff = solde_courant - prev_solde
                    # Tolérance de 10 XOF pour arrondi OCR
                    if diff > 10:
                        is_credit = True
                    elif diff < -10:
                        is_credit = False
                prev_solde = solde_courant
                amt = montant_op
            else:
                amt = amounts_vals[0]

            # Méthode 2 : mots-clés du libellé (si méthode 1 insuffisante)
            if is_credit is None:
                credit_kw = ('V/V', 'FAC FV', 'VIREMENT ENTRANT', 'REMISE',
                             'VERSEMENT', 'AVOIR', 'RETOUR', 'REMBOURSEMENT',
                             'VIREMENT REÇU', 'CREDIT')
                debit_kw  = ('VIREMENT W', 'FACTURE ARC', 'CHQ', 'PREST',
                             'RET ESP', 'FRAIS', 'RETRAIT', 'COMMISSION',
                             'COTISATION', 'ABONNEMENT', 'TENUE DE COMPTE',
                             'SOUSCRIPTION', 'SMS')
                lbl_up2 = label_part.upper()
                if any(kw in lbl_up2 for kw in credit_kw):
                    is_credit = True
                elif any(kw in lbl_up2 for kw in debit_kw):
                    is_credit = False
                else:
                    is_credit = False  # défaut : débit

            signed_amt = amt if is_credit else -amt
            txns.append(_make_txn(date_ofx, signed_amt, label_part))

    # Fallback universel si toujours rien trouvé
    if not txns and _pdf_path and Path(_pdf_path).exists():
        return _universal_parse_path(_pdf_path, pages_text)
    return info, [t for t in txns if t is not None]


# ── BIS (Banque Islamique du Sénégal) : format iMAL*CSM
# Positions mesurées sur PDF réel :
#   Date+Valeur : x0 ≈ 19-103 (format "dd/mm/yyyydd/mm/yyyy" concaténé ou simple)
#   No Trs      : x0 ≈ 111-152
#   Description : x0 ≈ 157-400
#   Mnt Crédit  : x0 ≈ 320-415  ('0' = colonne vide)
#   Mnt Débit   : x0 ≈ 415-500  ('0' = colonne vide)
#   Solde       : x0 ≈ 509+
def parse_bis(pages_words, pages_text, _pdf_path=''):
    info = _afr_header(pages_text)
    txns = []
    SKIP = {'TOTAL','SOLDE','DATE','VALEUR','NO TRS','DESCRIPTION','MNT','DÉBIT',
            'RÉSUMÉ','BANQUE','ISLAMIQUE','ECOLE','CIF','NO. CPTE','DISPONIBLE',
            'Solde','Bénéficiaire','Page','**'}

    # Solde d'ouverture ("** Solde du Bénéficiaire") et de clôture ("Total des Mnts ... Solde final")
    full_text = ' '.join(pages_text)
    # Ce relevé BIS utilise la virgule comme séparateur de milliers (pas de décimales) :
    # "25,343,570" = 25 343 570 XOF. Ne pas utiliser parse_amount (attend virgule = décimales).
    def _bis_int(s):
        try:
            return float(s.replace(',', ''))
        except ValueError:
            return None
    m_open_bis = re.search(r'Solde\s+du\s+B[ée]n[ée]ficiaire\s+([\d,\.]+)\s*Cr[ée]dit', full_text, re.IGNORECASE)
    if m_open_bis:
        v = _bis_int(m_open_bis.group(1))
        if v is not None:
            info['balance_open'] = v
    m_close_bis = re.search(r'Total\s+des\s+Mnts\s+[\d,\.]+\s+[\d,\.]+\s+([\d,\.]+)\s*Cr[ée]dit', full_text, re.IGNORECASE)
    if m_close_bis:
        v = _bis_int(m_close_bis.group(1))
        if v is not None:
            info['balance_close'] = v

    for pw in pages_words:
        rows = group_words_by_row(pw, tol=5.0)
        for row in rows:
            # Date : token en x0 < 110 commençant par dd/mm/yyyy
            # Peut être concaténé "02/05/202402/05/2024" (date trs + date valeur)
            date_w = None
            for w in row:
                if w['x0'] < 110:
                    m = re.match(r'^(\d{2}/\d{2}/\d{4})', w['text'])
                    if m:
                        date_w = w; break
            if not date_w:
                continue
            date_str = date_w['text'][:10]  # prendre dd/mm/yyyy

            # Ignorer les lignes de pied de page (contenant heure HH:MM:SS)
            if any(re.match(r'^\d{2}:\d{2}:\d{2}$', w['text']) for w in row):
                continue

            # Description : x0 ~157-400
            desc_words = [w for w in row if 150 <= w['x0'] < 400]
            label = ' '.join(w['text'] for w in desc_words).strip()
            if not label:
                continue
            if re.match(r'^[0-9#,\s]+$', label):
                continue
            if any(s in label for s in SKIP):
                continue

            # Positions mesurées sur PDF BIS réel (ECOLE BILINGUE AVENI, mai 2024) :
            #   En-tête "Mnt. de Cr"    x0=319 → montants crédit à x0 ≈ 344–400
            #   En-tête "Mnt. de débit" x0=397 → montants débit  à x0 ≈ 427–470
            #   Solde (fusionné "XXXXX Crédit") x0 ≈ 509 → à ignorer strictement
            # Note : le '0' fictif (colonne vide) est à x0=375 (crédit) ou x0=459 (débit)
            credit_words = [w for w in row if 320 <= w['x0'] < 410]
            debit_words  = [w for w in row if 415 <= w['x0'] < 490]

            credit_raw = ' '.join(w['text'] for w in credit_words).strip()
            debit_raw  = ' '.join(w['text'] for w in debit_words).strip()

            # '0' seul = colonne vide dans ce format BIS — ne pas le traiter comme un montant
            credit_amt = _uba_join_amount(credit_words) if credit_raw and credit_raw not in ('0', '') else None
            debit_amt  = _uba_join_amount(debit_words)  if debit_raw  and debit_raw  not in ('0', '') else None

            date_ofx = date_full_to_ofx(date_str)
            name, memo = smart_label(label, [])
            if credit_amt and credit_amt > 0:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))
            elif debit_amt and debit_amt > 0:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))

    if not txns and _pdf_path and Path(_pdf_path).exists():
        return _universal_parse_path(_pdf_path, pages_text)
    return info, [t for t in txns if t is not None]


# ── BNDE : format tableau Date | Libellé | Valeur | Débit | Crédit | Solde
# PDF natif pdfplumber → parsing mot-par-mot d'abord, texte brut en fallback
def parse_bnde(pages_words, pages_text, _pdf_path=''):
    info = _afr_header(pages_text)
    year = _year_from_text(' '.join(pages_text[:2]))
    txns = []

    SKIP = {'TOTAL','SOLDE','DATE','LIBELLÉ','LIBELLE','VALEUR','DÉBIT','DEBIT',
            'CRÉDIT','CREDIT','A REPORTER','SOLDE À REPORTER','TITULAIRE','RELEVE',
            'VEUILLEZ','PAGE','BNDE','AGENCE','COMPTE','DEVISE','DOMICILIATION',
            'SIÈGE','SOCIAL','RCCM','NINEA'}
    CREDIT_KW = {'VERSEMENT','VIREMENT','RECU','REMISE','CREDIT','DÉBLOCAGE',
                 'DEBLOCAGE','ANNUL CHQ','RECOUVREMENT','SWIFT','PAR :',
                 'CNCA THIES','REMISE DE CHEQUES','TIRE :'}
    DEBIT_KW  = {'RETRAIT','CHEQ','CHQ','AGIOS','FRAIS','COMMISSION',
                 'ABONNEMENT','ROUTAGE','FAV :','BENEF :','VIREMENT AUTRE BANQUE',
                 '000090','000110','000104','000103','000101','000106',
                 '000115','000111','000112','000114','000113','000123',
                 '000131','000130','000132','000124','000128','000905',
                 '000906'}

    # ── Essai 1 : parsing pdfplumber words ───────────────────────────────────
    for pw in pages_words:
        rows = group_words_by_row(pw, tol=4.0)
        i = 0
        while i < len(rows):
            row = rows[i]
            # Date : DD/MM/YYYY en x0 < 100
            date_words = [w for w in row if w['x0'] < 100
                          and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
            if not date_words:
                i += 1; continue
            date_str = date_words[0]['text']

            label_words = [w for w in row if 70 <= w['x0'] < 360
                           and not re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
            label = ' '.join(w['text'] for w in label_words).strip()
            label_up = label.upper()

            # Si libellé vide/numérique/trop court, chercher dans lignes adjacentes sans date
            if not label or len(label) < 3 or re.match(r'^[\d\s/\-.,]+$', label):
                for k in list(range(i - 1, max(i - 4, -1), -1)) + list(range(i + 1, min(i + 4, len(rows)))):
                    r2 = rows[k]
                    has_date2 = any(w['x0'] < 100 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])
                                    for w in r2)
                    if has_date2:
                        continue
                    adj_words = [w for w in r2 if 70 <= w['x0'] < 360
                                 and not re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
                    adj = ' '.join(w['text'] for w in adj_words).strip()
                    if adj and len(adj) >= 3 and re.search(r'[A-Za-zÀ-ÿ]{2,}', adj):
                        label = adj
                        break
                label_up = label.upper()

            if not label or any(s == label_up for s in SKIP):
                i += 1; continue
            if re.match(r'^[\d\s/\-.,]+$', label):
                i += 1; continue

            # Montants : débit (x0 342-421) et crédit (x0 421-503)
            # Positions mesurées sur PDF BNDE réel (SUP DECO THIES) :
            #   Débit  header x0=342, Crédit header x0=421, Solde header x0=503
            debit_words  = [w for w in row if 335 <= w['x0'] < 420]
            credit_words = [w for w in row if 420 <= w['x0'] < 503]
            debit_amt  = _uba_join_amount(debit_words)
            credit_amt = _uba_join_amount(credit_words)

            memo_parts = []
            j = i + 1
            while j < len(rows):
                r2 = rows[j]
                if r2 and any(w['x0'] < 100 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])
                              for w in r2):
                    break
                nl = ' '.join(w['text'] for w in r2 if 70 <= w['x0'] < 360).strip()
                if nl and not any(s in nl.upper() for s in SKIP) and len(nl) > 2:
                    memo_parts.append(nl)
                j += 1
            i = j

            date_ofx = date_full_to_ofx(date_str)
            name, memo = smart_label(label, memo_parts)
            if debit_amt and debit_amt > 0:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt and credit_amt > 0:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))
            else:
                # Fallback sémantique si colonnes non distinctes
                all_right = [w for w in row if w['x0'] >= 360]
                amt = _uba_join_amount(all_right)
                if amt and amt > 100:
                    is_credit = any(k in label_up for k in CREDIT_KW)
                    is_debit  = any(k in label_up for k in DEBIT_KW)
                    if is_credit and not is_debit:
                        txns.append(_make_txn(date_ofx, amt, name, memo))
                    elif is_debit and not is_credit:
                        txns.append(_make_txn(date_ofx, -amt, name, memo))

    # ── Essai 2 : parseur universel table ─────────────────────────────────────
    if not txns and _pdf_path and Path(_pdf_path).exists():
        result_info, txns_u = _universal_parse_path(_pdf_path, pages_text)
        if txns_u:
            result_info.update({k: v for k, v in info.items() if v})
            return result_info, txns_u

    # ── Essai 3 : fallback texte brut DD/MM/YYYY ──────────────────────────────
    if not txns:
        full_text = '\n'.join(pages_text)
        date_re = re.compile(r'^(\d{2}/\d{2}/\d{4})\s+')
        for line in full_text.splitlines():
            line = line.strip()
            if not line: continue
            m = date_re.match(line)
            if not m: continue
            date_str = m.group(1)
            rest = line[m.end():]
            # Sauter la date valeur si présente
            rest = re.sub(r'^\d{2}/\d{2}/\d{4}\s*', '', rest).strip()
            rest_up = rest.upper()
            if any(s in rest_up for s in SKIP): continue

            amounts_raw = re.findall(r'\d[\d\s]*[,.]\d{2}|\d+', rest)
            amounts_parsed = [v for a in amounts_raw
                              for v in [parse_amount(a.strip().replace(' ', '').replace(',', '.'))]
                              if v and v >= 100]
            if not amounts_parsed: continue

            label_m = re.match(r'^([A-Za-zÀ-ÿ\s\-\'\./,:&°0-9]+?)(?=\s{2,}\d|\s+\d{3,})', rest)
            label = label_m.group(1).strip() if label_m else rest[:60].strip()
            if not label or len(label) < 3: continue
            if any(s in label.upper() for s in SKIP): continue
            if not re.search(r'[A-Za-zÀ-ÿ]{2,}', label): continue

            is_credit = any(k in label.upper() for k in CREDIT_KW)
            is_debit  = any(k in label.upper() for k in DEBIT_KW)
            amt = amounts_parsed[0]
            date_ofx = date_full_to_ofx(date_str)
            name, memo_out = smart_label(label, [])
            if is_credit and not is_debit:
                txns.append(_make_txn(date_ofx, amt, name, memo_out))
            elif is_debit and not is_credit:
                txns.append(_make_txn(date_ofx, -amt, name, memo_out))

    # ── Extraction solde depuis le texte (BNDE) ───────────────────────────────
    full_text_bnde = ' '.join(pages_text)
    m_close = re.search(
        r'Solde\s+\([A-Z]+\)\s+au\s+\d{2}/\d{2}/\d{4}\s*:\s*([\d\s]+)',
        full_text_bnde, re.IGNORECASE)
    if m_close:
        v = parse_amount(re.sub(r'\s+', '', m_close.group(1).strip()))
        if v and v > 0:
            info['balance_close'] = v
    m_open = re.search(
        r'Solde\s+initial\s+\([A-Z]+\)\s*:\s*([\d\s]+)',
        full_text_bnde, re.IGNORECASE)
    if m_open:
        v = parse_amount(re.sub(r'\s+', '', m_open.group(1).strip()))
        if v and v > 0:
            info['balance_open'] = v

    if not txns and _pdf_path and Path(_pdf_path).exists():
        return _universal_parse_path(_pdf_path, pages_text)
    return info, [t for t in txns if t is not None]


# ── UBA : format Extrait de compte
# Structure réelle : le libellé est sur les lignes PRÉCÉDANT la ligne de date.
# Ligne de date: date (x0<90), date valeur (x0≈269), débit (x0≈330-430), crédit (x0≈430-520), solde (x0≈520+)
def parse_uba(pages_words, pages_text, _pdf_path=''):
    info = _afr_header(pages_text)
    txns = []
    CREDIT_KW = {'MAINLEVEE','BLOCAGE','RECU','SWIFT','DECAISSEMENT','VERSEMENT','REMISE','CREDIT'}
    DEBIT_KW  = {'VISA','FRAIS','COMMISSION','CHEQUE','TOB','ECHEANCE','PRET',
                 'FACTURATION','REDEVANCE','RETRAIT','AGIOS'}
    HEADER_SKIP = {'SOLDE','DÉBIT','DEBIT','CRÉDIT','CREDIT','VALEUR','DATE',
                   'OPÉRATION','OPERATION','INSTR','AGENCE','COMPTE','PÉRIODE',
                   'POUR','RELEVÉ','EXTRAIT','TITULAIRE','RIB','BIC'}

    def _group_num_tokens(words):
        blocks, cur, prev_x1 = [], [], None
        for w in words:
            is_num = bool(re.match(r'^[\d\s,\.]+$', w['text']) and re.search(r'\d', w['text']))
            if not is_num:
                if cur: blocks.append(cur); cur = []
                prev_x1 = None; continue
            if prev_x1 is not None and (w['x0'] - prev_x1) > 35:
                if cur: blocks.append(cur)
                cur = [w]
            else:
                cur.append(w)
            prev_x1 = w.get('x1', w['x0'] + 20)
        if cur: blocks.append(cur)
        return blocks

    for pw in pages_words:
        rows = group_words_by_row(pw, tol=5.0)

        # Identifier les indices des lignes de date (dd/mm/yyyy à x0 < 90)
        date_row_indices = []
        for idx, row in enumerate(rows):
            dw = [w for w in row if w['x0'] < 90
                  and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
            if dw:
                date_row_indices.append(idx)

        for i, date_idx in enumerate(date_row_indices):
            row = rows[date_idx]
            date_str = [w for w in row if w['x0'] < 90
                        and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])][0]['text']

            # Libellé = lignes entre date row précédente et celle-ci
            prev_date_idx = date_row_indices[i - 1] if i > 0 else -1
            label_parts = []
            for j in range(prev_date_idx + 1, date_idx):
                r = rows[j]
                lw = [w for w in r if 115 <= w['x0'] < 410]
                text = ' '.join(w['text'] for w in lw).strip()
                if not text or re.match(r'^[\d/,\.\s]+$', text):
                    continue
                text_up = text.upper()
                if any(s in text_up for s in HEADER_SKIP):
                    continue
                if re.match(r'^[au\s\d/]+$', text, re.IGNORECASE):
                    continue
                label_parts.append(text)
            label = ' '.join(label_parts).strip()

            if not label:
                row_label = ' '.join(w['text'] for w in row if 80 <= w['x0'] < 260).strip()
                label = row_label

            if not label:
                continue
            label_up = label.upper()
            has_useful = bool(re.search(r'[A-Za-zÀ-ÿ]{3,}', label))
            if not has_useful:
                continue

            # Montants sur la ligne de date (à droite de x0≥250)
            right_words = sorted([w for w in row if w['x0'] >= 250], key=lambda w: w['x0'])
            blocks = _group_num_tokens(right_words)
            numeric_blocks = []
            for b in blocks:
                joined = ''.join(w['text'] for w in b)
                if re.match(r'^\d{2}/\d{2}/\d{4}$', joined):
                    continue
                v = _uba_join_amount(b)
                if v is not None:
                    numeric_blocks.append((v, b[0]['x0']))

            if not numeric_blocks:
                continue

            # Débit x0≈330-430, Crédit x0≈430-520, Solde x0≈520+
            debit_cands  = [(v, x) for v, x in numeric_blocks if 330 <= x < 430]
            credit_cands = [(v, x) for v, x in numeric_blocks if 430 <= x < 520]

            debit_amt  = debit_cands[0][0]  if debit_cands  else None
            credit_amt = credit_cands[0][0] if credit_cands else None

            if debit_amt is None and credit_amt is None and numeric_blocks:
                val, x0 = numeric_blocks[0]
                is_credit = any(k in label_up for k in CREDIT_KW)
                is_debit  = any(k in label_up for k in DEBIT_KW)
                if is_credit and not is_debit:
                    credit_amt = val
                elif is_debit and not is_credit:
                    debit_amt = val

            date_ofx = date_full_to_ofx(date_str)
            name, memo = smart_label(label, [])
            if debit_amt and debit_amt > 0:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt and credit_amt > 0:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))

    if not txns and _pdf_path and Path(_pdf_path).exists():
        return _universal_parse_path(_pdf_path, pages_text)
    return info, [t for t in txns if t is not None]


# ── SG Afrique (SGBS Sénégal) : Date | Libellé | Débit | Crédit
# Particularités : montants XOF entiers sans décimales ('15 400', '369 630'),
# fragmentés en plusieurs tokens par pdfplumber.
def parse_sg_afrique(pages_words, pages_text, _pdf_path=''):
    info = _afr_header(pages_text)
    txns = []
    SKIP = {'TOTAUX DES','NOUVEAU SOLDE','SOLDE PRECEDENT','PROGRAMME DE',
            'RAPPEL DES','MONTANT CUMULE','SOLDE AU','DATE D','DATE DE','LIBELLÉ',
            'TOTAL DES','DÉBIT','CRÉDIT','SOLDE','TOTAL','LIBELLE'}

    # Mots-clés sémantiques pour déduire le sens quand la position seule est ambiguë
    CREDIT_KW = {'VIREMENT','VERSEMENT','REMISE','CREDIT','RECU','SWIFT',
                 'DECAISSEMENT','MAINLEVEE','DEBLOCAGE','TRF-RECU','TRF RECU',
                 'REM.CHQ','REMISE CHQ','TRF-REÇU','VIRT RECU','CAPI SENEGAL',
                 'SOCOCIM','SENEGALAISE','ASS MEDIA','EDWIGE','DIOP EDWIGE',
                 'C LINES INTER', 'S.A.S. C', 'VERSEMENT DIOP', 'VERSEMENT EDWIGE',
                 'TIRE :', 'BENEF :', 'SORT CHEQUES', 'CNCA'}
    DEBIT_KW  = {'REDEVANCE','CHEQUE','COMMISSION','FRAIS','ECHEANCE','PRET',
                 'FACTURATION','RETRAIT','AGIOS','PRELEVEMENT','PRLV',
                 'CHQ COMP','RETRAIT ESPECES','IMPAYE','IBE','ABONNEMENT',
                 'FRAIS TELECOMP','FRAIS VIRTREG','VIREMENT REG PRO ENERGY',
                 'REG PRO ENERGY','LUFTHANSA'}

    for pw in pages_words:
        rows = group_words_by_row(pw, tol=4.0)
        i = 0
        while i < len(rows):
            row = rows[i]
            # Date : dd/mm/yyyy en x0 < 75
            if not (row and row[0]['x0'] < 75
                    and re.match(r'^\d{2}/\d{2}/\d{4}$', row[0]['text'])):
                i += 1; continue

            label = ' '.join(w['text'] for w in row if 115 <= w['x0'] < 370).strip()
            label_up = label.upper()
            if not label or any(s in label_up for s in SKIP):
                i += 1; continue

            # --- Détection robuste des montants ---
            # SG Sénégal : Date | Date_valeur (79–114) | Libellé (115–370)
            # Débit  ≈ x0 370–420, Crédit ≈ x0 445–490, Solde ≈ x0 520+
            # Zones mesurées sur PDF réel SGBS Sénégal
            right_words = sorted([w for w in row if w['x0'] >= 360],
                                 key=lambda w: w['x0'])

            # Regrouper en blocs contigus (gap > 30px = nouveau bloc)
            def _make_blocks(words, gap=30):
                blocks, cur, prev_x1 = [], [], None
                for w in words:
                    is_num = bool(re.match(r'^[\d,\.]+$', w['text'])
                                  and re.search(r'\d', w['text']))
                    if not is_num:
                        if cur: blocks.append(cur); cur = []
                        prev_x1 = None; continue
                    if prev_x1 is not None and (w['x0'] - prev_x1) > gap:
                        if cur: blocks.append(cur)
                        cur = [w]
                    else:
                        cur.append(w)
                    prev_x1 = w.get('x1', w['x0'] + len(w['text']) * 7)
                if cur: blocks.append(cur)
                return blocks

            blocks = _make_blocks(right_words)

            # Résoudre chaque bloc en valeur numérique
            resolved = []  # liste de (valeur, x0_du_bloc)
            for b in blocks:
                v = _uba_join_amount(b)
                if v is not None:
                    resolved.append((v, b[0]['x0']))

            if not resolved:
                i += 1; continue

            debit_amt = credit_amt = None
            nb = len(resolved)

            # SG Sénégal (SGBS) — positions mesurées sur PDF réel SGBS Jan-2026 (TRC) :
            #   Débit  : x0 ≈ 355–435   (colonne "Débit" à x0=358)
            #   Crédit : x0 ≈ 435–510   (colonne "Crédit" à x0=437)
            #   Solde  : x0 ≥ 510       (colonne "Solde" à x0=507)
            DEBIT_X_MAX  = 435   # colonne débit : [360, 435)
            CREDIT_X_MIN = 435   # colonne crédit : [435, 510)
            SOLDE_X_MIN  = 505   # colonne solde : ≥ 505 (ignorée)

            is_credit_kw = any(k in label_up for k in CREDIT_KW)
            is_debit_kw  = any(k in label_up for k in DEBIT_KW)

            # Filtrer le solde (bloc le plus à droite si x0 ≥ SOLDE_X_MIN)
            # mais seulement quand on a plus de 2 blocs
            candidates = resolved if nb <= 2 else [r for r in resolved if r[1] < SOLDE_X_MIN]
            if not candidates:
                candidates = resolved[:-1] if nb >= 2 else resolved

            if len(candidates) == 1:
                val, x0 = candidates[0]
                if x0 >= CREDIT_X_MIN:
                    credit_amt = val
                elif x0 < DEBIT_X_MAX:
                    # Position clairement dans la colonne débit
                    debit_amt = val
                else:
                    # Zone ambiguë : fallback sémantique
                    if is_credit_kw and not is_debit_kw:
                        credit_amt = val
                    else:
                        debit_amt = val

            elif len(candidates) == 2:
                val_l, x0_l = min(candidates, key=lambda x: x[1])
                val_r, x0_r = max(candidates, key=lambda x: x[1])
                if x0_l < DEBIT_X_MAX and x0_r >= CREDIT_X_MIN:
                    # Colonnes bien séparées : débit gauche, crédit droite
                    debit_amt  = val_l
                    credit_amt = val_r
                elif x0_l >= CREDIT_X_MIN:
                    # Les deux en zone crédit (montant fragmenté)
                    credit_amt = val_l  # le plus à gauche est le 1er token
                else:
                    # Les deux en zone débit
                    debit_amt = val_l

            else:
                # ≥ 3 candidats après filtrage solde : prendre les deux plus distincts
                x_min_c = min(candidates, key=lambda x: x[1])
                x_max_c = max(candidates, key=lambda x: x[1])
                if x_min_c[1] < DEBIT_X_MAX and x_max_c[1] >= CREDIT_X_MIN:
                    debit_amt  = x_min_c[0]
                    credit_amt = x_max_c[0]
                elif x_max_c[1] >= CREDIT_X_MIN:
                    credit_amt = x_max_c[0]
                else:
                    if is_credit_kw and not is_debit_kw:
                        credit_amt = x_max_c[0]
                    else:
                        debit_amt = x_min_c[0]

            # Mémo lignes suivantes
            memo_parts = []
            j = i + 1
            while j < len(rows):
                r2 = rows[j]
                if r2 and r2[0]['x0'] < 75 and re.match(r'^\d{2}/\d{2}/\d{4}$', r2[0]['text']):
                    break
                nl = ' '.join(w['text'] for w in r2 if 115 <= w['x0'] < 370).strip()
                if nl and not any(s in nl.upper() for s in SKIP):
                    memo_parts.append(nl)
                j += 1
            i = j

            date_ofx = date_full_to_ofx(row[0]['text'])
            name, memo = smart_label(label, memo_parts)
            if debit_amt and debit_amt > 0:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt and credit_amt > 0:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))

    if not txns and _pdf_path and Path(_pdf_path).exists():
        return _universal_parse_path(_pdf_path, pages_text)
    # ── Extraction du solde de clôture ─────────────────────────────────────
    full_text_sg = ' '.join(pages_text)
    for pat in [
        r'Nouveau\s+solde\s+en\s+FRANC[^0-9]+([\d][\d\s]{5,})',
        r'Nouveau\s+solde\b[^0-9]+([\d][\d\s]{5,})',
        r'NOUVEAU\s+SOLDE\b[^0-9]+([\d][\d\s]{5,})',
        r'Solde\s+au\s+\d{2}/\d{2}/\d{4}\s+([\d][\d\s]{5,})',
    ]:
        matches = list(re.finditer(pat, full_text_sg, re.IGNORECASE))
        if matches:
            # Prendre le dernier match (solde final en bas du relevé)
            raw = matches[-1].group(1).strip().split()
            candidate = ''.join(raw[:3])
            v = parse_amount(candidate)
            if v and v > 0:
                info['balance_close'] = v
                break

    return info, [t for t in txns if t is not None]
def _make_african_parser(bank_name):
    def _parser(pages_words, pages_text, _pdf_path=''):
        if _pdf_path and Path(_pdf_path).exists():
            return _universal_parse_path(_pdf_path, pages_text)
        return _afr_header(pages_text), []
    return _parser

parse_bci       = _make_african_parser('BCI')
parse_atb       = _make_african_parser('ATB')
parse_universal = _make_african_parser('Universal')


# ════════════════════════════════════════════════════════════════════════════
# CBAO — Compagnie Bancaire de l'Afrique Occidentale
# Format : Date | Valeur | Libellé | Débit (XOF) | Crédit (XOF) | Solde (XOF)
# En-tête   : "EXTRAIT DE COMPTE" + "Nom du client", "Numéro de compte"
#
# Positions réelles mesurées sur relevé CBAO CB MOTORS (mars 2026) via pdfplumber :
#   Date op    : x0 < 75    (DD/MM/YYYY)
#   Date val   : x0 ≈ 75–140 (DD/MM/YYYY) — ignoré
#   Libellé    : x0 ≈ 140–400  (texte sur 1 à 3 lignes)
#   Débit      : x0 ≈ 400–510  (montant entier XOF, sans décimale)
#   Crédit     : x0 ≈ 510–620  (montant entier XOF, sans décimale)
#   Solde      : x0 ≥ 620     (ignoré)
#
# Particularités CBAO :
#   • Montants XOF sans décimale (ex: "920 400" = 920400)
#   • Certains libellés s'étalent sur 2–3 lignes sans date répétée (mémo)
#   • La ligne (*) "Les évènements du jour sont sujets à modification" doit être ignorée
#   • "VIREMENT W Factures m" puis "BOA NGAPAROU KOF-EXPERTS 356 950" sur la ligne suivante
#     → la 2e ligne porte un montant ET un label → nouvelle transaction distincte
#   • "5 850" dans le libellé est un numéro de compte (référence), PAS un montant
# ════════════════════════════════════════════════════════════════════════════
def parse_cbao(pages_words, pages_text, _pdf_path=''):
    info = _afr_header(pages_text)
    year = _year_from_text(' '.join(pages_text[:2]))
    txns = []

    SKIP = {'TOTAL', 'SOLDE', 'DATE', 'VALEUR', 'LIBELLÉ', 'LIBELLE', 'DÉBIT', 'DEBIT',
            'CRÉDIT', 'CREDIT', 'SOLDE (XOF)', 'DÉBIT (XOF)', 'CRÉDIT (XOF)',
            'EXTRAIT', 'PÉRIODE', 'CODE', 'NOM', 'PAGE', 'NUMÉRO', 'NUMERO'}
    SKIP_START = ('SOLDE', 'TOTAL', '(*)')
    # Ignorer la ligne de note légale CBAO
    SKIP_CONTAINS = ('SUJETS À MODIFICATION', 'SUJETS A MODIFICATION',
                     'ÉVÈNEMENTS DU JOUR', 'EVENEMENTS DU JOUR')

    # Seuils de colonnes CBAO — recalibrés sur relevé CB MOTORS mars 2026
    # En-têtes réels : "Débit (XOF)" x0≈363, "Crédit (XOF)" x0≈421, "Solde (XOF)" x0≈503
    # Montants débit  : x0 ≈ 355–440  (FRA/COM=371, WOLF=360, W Cartes 1454350=365, W Fac 356950=371)
    # Montants crédit : x0 ≈ 440–510  (SUNSTEEL 920400=452, RTRACTAFRIC 1121000=446)
    # Montants solde  : x0 ≥ 510      (ignorés)
    CBAO_DATE_MAX    = 75    # x0 < 75  → colonne Date
    CBAO_LABEL_MIN   = 140   # x0 ≥ 140 → début libellé
    CBAO_LABEL_MAX   = 355   # x0 < 355 → fin libellé
    CBAO_DEBIT_MIN   = 355   # x0 ≥ 355 → zone montants droite
    CBAO_DEBIT_MAX   = 440   # x0 < 440 → débit
    CBAO_CREDIT_MIN  = 440   # x0 ≥ 440 → crédit
    CBAO_SOLDE_MIN   = 510   # x0 ≥ 510 → solde (ignoré)

    # Mots-clés sémantiques pour lever l'ambiguïté débit/crédit quand
    # la position X seule ne suffit pas (montant unique sans colonne claire)
    # NB CB MOTORS mars 2026 — position x0 suffit pour la majorité :
    #   Débit  x0 ≈ 355–440 : FRA/COM, SAISIE TRF WOLF OIL, W Cartes 1454350, W Fac 356950
    #   Crédit x0 ≈ 440–510 : SUNSTEEL 920400, RTRACTAFRIC 1121000
    #   Seuls cas à lever par sémantique : montant unique sans position claire
    CREDIT_KW = {'VIREMENT RECU', 'VIRMT ORD', 'RTRACTAFRIC',
                 'CREDIT', 'VERSEMENT', 'REMISE'}
    DEBIT_KW  = {'FRAIS', 'COMMISSION', 'SAISIE TRF', 'RETRAIT', 'CHEQUE',
                 'FRA/COM', 'FRAIS/', 'FAV.'}

    # Lecture du solde final et initial
    full_text = ' '.join(pages_text)
    m_bal = re.search(r'Solde\s+\([A-Z]+\)\s+au\s+[\d/]+\s*:\s*([\d\s]+)', full_text, re.IGNORECASE)
    if not m_bal:
        m_bal = re.search(r'Solde\s+initial\s+\([A-Z]+\)\s*:\s*([\d\s]+)', full_text, re.IGNORECASE)
    if m_bal:
        v = parse_amount(m_bal.group(1).strip().replace(' ', ''))
        if v:
            info['balance_close'] = v

    # Solde initial (balance_open) — utilisé pour initialiser prev_solde
    m_open = re.search(r'Solde\s+initial\s+\([A-Z]+\)\s*:\s*([\d\s]+)', full_text, re.IGNORECASE)
    if m_open:
        v = parse_amount(m_open.group(1).strip().replace(' ', ''))
        if v:
            info['balance_open'] = v

    def _cbao_resolve_amounts(row_words):
        """
        Extrait les montants débit/crédit d'une ligne CBAO.
        Retourne (debit_amt, credit_amt) — l'un des deux est None.
        Les montants CBAO sont des entiers XOF sans décimale (ex: 920 400).
        La colonne solde (x0 ≥ CBAO_SOLDE_MIN) est ignorée.

        Cas particulier CB MOTORS : quand le libellé contient "W Cartes ess 5 850"
        ou "W Factures m 5 850", le "5 850" est un numéro de compte (référence),
        pas un montant débit. On détecte cela : si le seul bloc numérique en zone
        débit est entièrement composé de tokens qui apparaissent aussi dans le libellé
        (même texte, même position proche), on le considère comme référence et on
        retourne (None, None) — le caller utilisera la sémantique / le montant sera
        éventuellement fourni par une ligne de continuation.
        """
        # Tokens du libellé (pour détecter les numéros de compte)
        label_texts = {w['text'] for w in row_words if CBAO_LABEL_MIN <= w['x0'] < CBAO_LABEL_MAX}

        # Récupérer uniquement les mots numériques dans la zone montants
        right_words = sorted(
            [w for w in row_words if w['x0'] >= CBAO_DEBIT_MIN and re.search(r'\d', w['text'])],
            key=lambda w: w['x0']
        )
        if not right_words:
            return None, None

        # Regrouper en blocs contigus (gap > 30px entre tokens du même montant)
        blocs, cur, prev_x1 = [], [], None
        for w in right_words:
            is_num = bool(re.match(r'^[\d\s,\.]+$', w['text']) and re.search(r'\d', w['text']))
            if not is_num:
                if cur: blocs.append(cur); cur = []
                prev_x1 = None
                continue
            if prev_x1 is not None and (w['x0'] - prev_x1) > 30:
                if cur: blocs.append(cur)
                cur = [w]
            else:
                cur.append(w)
            prev_x1 = w.get('x1', w['x0'] + len(w['text']) * 7)
        if cur:
            blocs.append(cur)

        # Résoudre chaque bloc en valeur + position x0
        resolved = []
        for b in blocs:
            x0_bloc = b[0]['x0']
            if x0_bloc >= CBAO_SOLDE_MIN:
                continue   # ignorer la colonne Solde
            # Filtre référence-compte : si tous les tokens de ce bloc figurent aussi
            # dans le libellé (numéro de compte glissé dans le label), ignorer ce bloc
            if all(w['text'] in label_texts for w in b):
                continue
            v = _uba_join_amount(b)
            if v is not None and v > 0:
                resolved.append((v, x0_bloc))

        if not resolved:
            return None, None

        if len(resolved) == 1:
            return resolved[0][0], None  # caller assignera débit ou crédit via sémantique
        else:
            # 2+ blocs hors solde → le gauche est débit, le droit est crédit
            c_left  = min(resolved, key=lambda x: x[1])
            c_right = max(resolved, key=lambda x: x[1])
            # Mais si les deux sont dans la même colonne (ex: solde mal calibré), ne garder qu'un
            if c_left[1] < CBAO_DEBIT_MAX and c_right[1] >= CBAO_CREDIT_MIN:
                return c_left[0], c_right[0]
            elif c_right[1] >= CBAO_CREDIT_MIN:
                return None, c_right[0]
            else:
                return c_left[0], None

    def _cbao_assign_side(val, x0, label_up, row_words=None):
        """Assigne un montant unique au débit ou crédit selon sa position X et le libellé."""
        is_credit = any(k in label_up for k in CREDIT_KW)
        is_debit  = any(k in label_up for k in DEBIT_KW)
        if is_credit and not is_debit:
            return None, val
        if is_debit and not is_credit:
            return val, None
        # Fallback position : ≥ CBAO_CREDIT_MIN → crédit, sinon débit
        if x0 >= CBAO_CREDIT_MIN:
            return None, val
        return val, None

    def _cbao_has_amount_right(row_words):
        """Vrai si la ligne contient au moins un token numérique dans la zone montants."""
        return any(w['x0'] >= CBAO_DEBIT_MIN and re.search(r'\d', w['text']) for w in row_words)

    for pw in pages_words:
        rows = group_words_by_row(pw, tol=5.0)
        i = 0
        # Initialiser prev_solde depuis le solde initial du relevé
        prev_solde = info.get('balance_open') or info.get('balance_close') or None
        while i < len(rows):
            row = rows[i]

            # ── Date opération : DD/MM/YYYY en x0 < CBAO_DATE_MAX ──────────────
            date_words = [w for w in row
                          if w['x0'] < CBAO_DATE_MAX
                          and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
            if not date_words:
                i += 1; continue
            date_str = date_words[0]['text']

            # ── Libellé principal : x0 dans [CBAO_LABEL_MIN, CBAO_LABEL_MAX) ──
            label_words = [w for w in row if CBAO_LABEL_MIN <= w['x0'] < CBAO_LABEL_MAX]
            label = ' '.join(w['text'] for w in label_words).strip()
            label_up = label.upper()

            # Ignorer les lignes d'en-tête ou de note légale
            if not label or any(s in label_up for s in SKIP):
                i += 1; continue
            if any(label_up.startswith(s) for s in SKIP_START):
                i += 1; continue
            if any(s in label_up for s in SKIP_CONTAINS):
                i += 1; continue

            # ── Montants de la ligne principale ─────────────────────────────────
            d_val, c_val = _cbao_resolve_amounts(row)

            # Un seul montant résolu → assigner via sémantique + position
            if d_val is not None and c_val is None:
                # Lire le solde courant (colonne Solde x0 ≥ CBAO_SOLDE_MIN)
                solde_toks = sorted([w for w in row if w['x0'] >= CBAO_SOLDE_MIN
                                     and re.search(r'\d', w['text'])], key=lambda w: w['x0'])
                curr_solde = None
                if solde_toks:
                    try:
                        curr_solde = float(''.join(w['text'] for w in solde_toks).replace(' ',''))
                    except Exception:
                        pass

                # Si prev_solde connu → le sens est donné par la variation du solde
                if curr_solde is not None and prev_solde is not None:
                    if curr_solde > prev_solde:
                        d_val, c_val = None, d_val   # solde augmente → crédit
                    else:
                        d_val, c_val = d_val, None   # solde diminue → débit
                else:
                    # Fallback sémantique + position
                    right_w = sorted([w for w in row if w['x0'] >= CBAO_DEBIT_MIN and re.search(r'\d', w['text'])],
                                      key=lambda w: w['x0'])
                    x0_val = right_w[0]['x0'] if right_w else CBAO_DEBIT_MIN
                    d_val, c_val = _cbao_assign_side(d_val, x0_val, label_up, row_words=row)

                # Mettre à jour prev_solde pour la prochaine transaction
                if curr_solde is not None:
                    prev_solde = curr_solde
            elif d_val is None and c_val is not None:
                # Crédit direct (2 blocs résolus) : lire solde aussi
                solde_toks = sorted([w for w in row if w['x0'] >= CBAO_SOLDE_MIN
                                     and re.search(r'\d', w['text'])], key=lambda w: w['x0'])
                if solde_toks:
                    try:
                        prev_solde = float(''.join(w['text'] for w in solde_toks).replace(' ',''))
                    except Exception:
                        pass
            elif d_val is not None and c_val is not None:
                # 2 blocs (débit + crédit simultanés) : cas rare, lire solde
                solde_toks = sorted([w for w in row if w['x0'] >= CBAO_SOLDE_MIN
                                     and re.search(r'\d', w['text'])], key=lambda w: w['x0'])
                if solde_toks:
                    try:
                        prev_solde = float(''.join(w['text'] for w in solde_toks).replace(' ',''))
                    except Exception:
                        pass
            else:
                # Lire solde même si pas de montant (pour initialiser prev_solde)
                solde_toks = sorted([w for w in row if w['x0'] >= CBAO_SOLDE_MIN
                                     and re.search(r'\d', w['text'])], key=lambda w: w['x0'])
                if solde_toks:
                    try:
                        prev_solde = float(''.join(w['text'] for w in solde_toks).replace(' ',''))
                    except Exception:
                        pass

            debit_amt  = d_val
            credit_amt = c_val

            # Si (None, None) : le montant a peut-être été filtré comme numéro de référence
            # (ex: "W Cartes ess 5 850" où 5 850 est un compte, le vrai montant est sur la
            # ligne de continuation). On ne skippe PAS — on continue pour ramasser la suite.
            has_ref_only = (debit_amt is None and credit_amt is None and
                            any(w['x0'] >= CBAO_DEBIT_MIN and re.search(r'\d', w['text'])
                                for w in row))

            # Cas spécial : "VIREMENT W … 5 850" — le petit nombre (≤ 9 999) affiché dans
            # la colonne débit est un numéro de compte CBAO, pas un montant débit réel.
            # Ce cas est géré par le tracking du solde ci-dessous (prev_solde).

            # ── Lignes de continuation (mémo ou transaction séparée) ───────────
            memo_parts = []
            continuation_txns = []
            j = i + 1
            while j < len(rows):
                r2 = rows[j]
                # Arrêt si nouvelle date
                if any(w['x0'] < CBAO_DATE_MAX
                       and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text']) for w in r2):
                    break

                nl_label = ' '.join(
                    w['text'] for w in r2 if CBAO_LABEL_MIN <= w['x0'] < CBAO_LABEL_MAX
                ).strip()
                nl_up = nl_label.upper()

                # Ignorer les lignes de note légale dans les continuations aussi
                if any(s in nl_up for s in SKIP_CONTAINS):
                    j += 1; continue

                has_right = _cbao_has_amount_right(r2)

                if has_right and nl_label:
                    # Ligne avec montant ET libellé → transaction distincte sur la même date
                    r2_d, r2_c = _cbao_resolve_amounts(r2)
                    if r2_d is not None and r2_c is None:
                        right_w2 = sorted([w for w in r2 if w['x0'] >= CBAO_DEBIT_MIN
                                           and re.search(r'\d', w['text'])], key=lambda w: w['x0'])
                        x0_v2 = right_w2[0]['x0'] if right_w2 else CBAO_DEBIT_MIN
                        r2_d, r2_c = _cbao_assign_side(r2_d, x0_v2, nl_up, row_words=r2)
                    date_ofx_r2 = date_full_to_ofx(date_str)

                    if has_ref_only:
                        # La ligne principale avait uniquement un numéro de compte en guise
                        # de montant (ex: "W Cartes ess 5 850") — la vraie transaction est ici.
                        # On fusionne : libellé principal + libellé continuation
                        full_label = label + (' ' + nl_label if nl_label else '')
                        fn, fm = smart_label(full_label, memo_parts)
                        if r2_d and r2_d > 0:
                            txns.append(_make_txn(date_ofx_r2, -r2_d, fn, fm))
                        elif r2_c and r2_c > 0:
                            txns.append(_make_txn(date_ofx_r2, r2_c, fn, fm))
                        # Marquer qu'on a consommé la continuation
                        has_ref_only = False
                        debit_amt = credit_amt = None  # empêche l'émission ci-dessous
                    else:
                        r2_name, r2_memo = smart_label(nl_label, [])
                        if r2_d and r2_d > 0:
                            continuation_txns.append(_make_txn(date_ofx_r2, -r2_d, r2_name, r2_memo))
                        elif r2_c and r2_c > 0:
                            continuation_txns.append(_make_txn(date_ofx_r2, r2_c, r2_name, r2_memo))
                elif nl_label and not any(s in nl_up for s in SKIP):
                    # Pas de montant → mémo de la ligne courante
                    memo_parts.append(nl_label)
                j += 1
            i = j

            # ── Émettre la transaction principale ────────────────────────────────
            date_ofx = date_full_to_ofx(date_str)
            name, memo = smart_label(label, memo_parts)
            if debit_amt and debit_amt > 0:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt and credit_amt > 0:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))
            # Émettre les transactions de continuation
            for ct in continuation_txns:
                if ct is not None:
                    txns.append(ct)

    if not txns and _pdf_path and Path(_pdf_path).exists():
        return _universal_parse_path(_pdf_path, pages_text)
    return info, [t for t in txns if t is not None]


# ════════════════════════════════════════════════════════════════════════════
# BOA — Bank of Africa Sénégal
# Format : Date op | Description | Référence | Date valeur | Débit | Crédit | Solde courant
# En-tête  : "BANK OF AFRICA" / "BANK OF AFRICA - SENEGAL"
#
# Positions mesurées sur deux relevés réels :
#   BOA ATS AZIMUT SERVICES (nov 2025) :
#     Date op   : x0 ≈ 29–58   (DD/MM/YY)
#     Libellé   : x0 ≈ 100–340
#     Référence : x0 ≈ 258–284 (alphanumérique court)
#     Date val  : x0 ≈ 312–341
#     Débit     : x0 ≈ 390–440  ← seuil précédent (trop haut)
#     Crédit    : x0 ≈ 440–495
#     Solde     : x0 ≈ 495–540
#
#   BOA VILLA YEMAYA (jan 2026) :
#     Date op   : x0 ≈ 41–70   (DD/MM/YY)
#     Libellé   : x0 ≈ 91–242
#     Référence : x0 ≈ 258–284
#     Date val  : x0 ≈ 312–341
#     Débit     : x0 ≈ 365–412  (montants négatifs : "-200", "000,00")
#     Crédit    : x0 ≈ 442–485  (montants positifs sans signe)
#     Solde     : x0 ≈ 517–561
#
# → Seuils unifiés couvrant les deux formats :
#     DEBIT_X_MIN  = 355   (assez bas pour capturer x0=365)
#     DEBIT_X_MAX  = 440   (limite avant crédit)
#     CREDIT_X_MIN = 440
#     CREDIT_X_MAX = 510
#     SOLDE_X_MIN  = 510
#
# NOTE : Les débits BOA sont encodés avec un signe '-' DANS la colonne débit
#        (ex : "-200" + "000,00" = -200 000 XOF).  Le signe négatif identifie
#        le débit ; l'absence de signe dans la zone crédit identifie le crédit.
# ════════════════════════════════════════════════════════════════════════════
def parse_boa(pages_words, pages_text, _pdf_path=''):
    info = _afr_header(pages_text)
    txns = []

    SKIP = {'TOTAL', 'SOLDE', 'DATE', 'VALEUR', 'DESCRIPTION', 'RÉFÉRENCE', 'REFERENCE',
            'DÉBIT', 'DEBIT', 'CRÉDIT', 'CREDIT', 'SOLDE COURANT', 'BANK OF AFRICA',
            'BMCE GROUP', 'BMCE', 'TOTAUX', 'LIBELLÉ', 'LIBELLE'}
    SKIP_START = ('SOLDE', 'TOTAL', 'ANCIEN SOLDE', 'NOUVEAU SOLDE', 'SAUF ERREUR')

    # Mots-clés sémantiques pour déduire le sens (Crédit = entrée d'argent)
    CREDIT_KW = {'VERSEMENT', 'VIR RECU', 'VIREMENT RECU', 'VIR.RECU', 'CREDIT',
                 'REMISE', 'SWIFT', 'DEPOT', 'RECOUVREMENT', 'REGLEMENT APP', 'CC REMISE'}
    DEBIT_KW  = {'RETRAIT', 'ACHAT', 'CHEQUE', 'FRAIS', 'TAXE', 'COMMISSION',
                 'DROIT TIMBRE', 'PRELEVEMENT', 'PRELEV', 'VIR BOAWEB',
                 'ABONNEMENT', 'PRIME ASSURANCE', 'PAIEMENT'}

    # Solde de clôture / d'ouverture depuis le texte
    full_text = ' '.join(pages_text)
    m_close = re.search(r'Solde\s+de\s+cl[ôo]ture\s*[:\-]?\s*([\d\s,\.]+)\s*XOF', full_text, re.IGNORECASE)
    if m_close:
        v = parse_amount(m_close.group(1).replace(' ', ''))
        if v:
            info['balance_close'] = v
    m_open = re.search(r"Solde\s+d[’'\u2019]ouverture\s*[:\-]?\s*([\d\s,\.]+)\s*XOF", full_text, re.IGNORECASE)
    if m_open:
        v = parse_amount(m_open.group(1).replace(' ', ''))
        if v is not None:
            info['balance_open'] = v

    # Certains relevés BOA n'emploient pas les libellés "Solde de clôture" /
    # "Solde d'ouverture" mais affichent le solde courant uniquement dans la
    # colonne finale. Ces valeurs sont plus fiables que le parseur d'en-tête
    # générique : on les récupère également ligne par ligne ci-dessous.
    boa_last_running_balance = None

    def _boa_parse_col(words):
        """Parse un montant BOA dans une colonne précise.
        Retourne (valeur_absolue, est_négatif) ou (None, False)."""
        if not words:
            return None, False
        # Reconstituer le texte de la colonne
        full = ' '.join(w['text'] for w in sorted(words, key=lambda w: w['x0']))
        full = full.replace('\xa0', ' ').strip()
        neg = full.startswith('-')
        full_clean = full.lstrip('-').strip()
        # Essayer de parser en XOF (entier ou décimal, espaces comme séparateurs de milliers)
        # Formats : "200 000,00" ou "200000,00" ou "200 000" ou "200000"
        m = re.search(r'([\d][\d\s]*[\d](?:,\d{2})?)', full_clean)
        if m:
            v = parse_amount(m.group(1).replace(' ', ''))
            if v is not None and v > 0:
                return v, neg
        return None, False

    for pw in pages_words:
        rows = group_words_by_row(pw, tol=4.0)
        i = 0
        while i < len(rows):
            row = rows[i]
            # Date op : x0 ≈ 29–58, format DD/MM/YY (2 chiffres) ou DD/MM/YYYY
            date_words = [w for w in row if w['x0'] < 65
                          and re.match(r'^\d{2}/\d{2}/\d{2,4}$', w['text'])]
            if not date_words:
                i += 1; continue
            raw_date = date_words[0]['text']
            # Normaliser DD/MM/YY → DD/MM/YYYY
            parts = raw_date.split('/')
            if len(parts) == 3 and len(parts[2]) == 2:
                raw_date = f"{parts[0]}/{parts[1]}/20{parts[2]}"

            # Libellé : x0 ≈ 91–228 (exclut Date op à <70 et Référence à ~259–340)
            # Mesuré sur le relevé BOA VILLA YEMAYA : le mot le plus à gauche du
            # libellé ("RETRAIT", "VERSEMENT"...) démarre à x0=91.0. L'ancien seuil
            # bas (98) l'excluait, tronquant systématiquement le premier mot du
            # libellé ; l'ancien seuil haut (342) débordait sur les colonnes
            # Référence (x0≈259–284) et Date valeur (x0≈312–341), les injectant
            # dans le libellé.
            label_words = [w for w in row if 80 <= w['x0'] < 250]
            label = ' '.join(w['text'] for w in label_words).strip()
            label_up = label.upper()
            if not label or len(label) < 3:
                i += 1; continue
            if any(label_up.startswith(s) for s in SKIP_START):
                i += 1; continue
            if any(s in label_up for s in SKIP) and len(label) < 20:
                i += 1; continue

            # ── Montants BOA ─────────────────────────────────────────────────
            # Seuils unifiés couvrant BOA ATS (nov 2025) et BOA VILLA YEMAYA (jan 2026) :
            #   Débit  x0 ≈ 355–440  (ATS: 390–440 | VILLA: 365–412)
            #   Crédit x0 ≈ 440–510  (ATS: 440–495 | VILLA: 442–485)
            #   Solde  x0 ≥ 510      (ignoré)
            # Les débits ont un signe '-' explicite (ex: "-200" + "000,00").
            DEBIT_X_MIN  = 355
            DEBIT_X_MAX  = 440
            CREDIT_X_MIN = 440
            CREDIT_X_MAX = 510
            SOLDE_X_MIN  = 510

            right_words = sorted(
                [w for w in row if w['x0'] >= DEBIT_X_MIN
                 and re.search(r'[\d\-]', w['text'])
                 and not re.match(r'^\d{2}/\d{2}/\d{2,4}$', w['text'])],
                key=lambda w: w['x0']
            )

            # Collecter les tokens par zone (débit / crédit / solde)
            # Un token purement '-' ou '-NNN' est rattaché à la zone où il se trouve
            debit_tokens, credit_tokens = [], []
            for w in right_words:
                txt = w['text']
                if w['x0'] < DEBIT_X_MAX:
                    # Inclure les signes '-' isolés et les montants négatifs
                    if re.search(r'\d', txt) or txt.strip() == '-':
                        debit_tokens.append(w)
                elif w['x0'] < CREDIT_X_MAX:
                    if re.search(r'\d', txt) or txt.strip() == '-':
                        credit_tokens.append(w)
                # else : solde → ignorer

            def _parse_boa_amount(tokens):
                """Reconstitue un montant XOF à partir de tokens espacés.
                Gère les montants avec signe '-' (ex: -200 000,00) dans la colonne débit."""
                if not tokens:
                    return None
                # Tri par x0, puis concaténation pour gérer "3 000 000" → "3000000"
                t = sorted(tokens, key=lambda w: w['x0'])
                raw = ''.join(w['text'].replace('\xa0', '').replace(' ', '')
                              for w in t)
                # Détecter et mémoriser le signe négatif (présent dans col débit BOA)
                has_minus = raw.startswith('-')
                raw_clean = raw.lstrip('-')
                # Format attendu : entier (200000, 3000000) ou avec virgule (200000,00)
                m = re.match(r'^(\d+)(?:,\d{2})?$', raw_clean)
                if m:
                    v = float(m.group(1))
                    # Retourner None si zéro, sinon la valeur absolue
                    # (le signe '-' confirme que c'est bien un débit — sera géré à l'émission)
                    return v if v > 0 else None
                return None

            def _parse_boa_amount_signed(tokens):
                """Comme _parse_boa_amount mais retourne (valeur, is_negative).
                Utilisé pour détecter les débits signés '-200 000,00'."""
                if not tokens:
                    return None, False
                t = sorted(tokens, key=lambda w: w['x0'])
                raw = ''.join(w['text'].replace('\xa0', '').replace(' ', '')
                              for w in t)
                has_minus = raw.startswith('-')
                raw_clean = raw.lstrip('-')
                m = re.match(r'^(\d+)(?:,\d{2})?$', raw_clean)
                if m:
                    v = float(m.group(1))
                    return (v if v > 0 else None), has_minus
                return None, False

            # Mémo lignes suivantes (description multi-ligne)
            memo_parts = []
            j = i + 1
            while j < len(rows):
                r2 = rows[j]
                if r2 and any(w['x0'] < 65 and re.match(r'^\d{2}/\d{2}/\d{2,4}$', w['text'])
                               for w in r2):
                    break
                nl = ' '.join(w['text'] for w in r2 if 80 <= w['x0'] < 250).strip()
                if nl and not any(s == nl.upper() for s in SKIP):
                    memo_parts.append(nl)
                j += 1
            i = j

            date_ofx = date_full_to_ofx(raw_date)
            name, memo = smart_label(label, memo_parts)
            label_up = label.upper()

            debit_amt,  debit_neg  = _parse_boa_amount_signed(debit_tokens)
            credit_amt, credit_neg = _parse_boa_amount_signed(credit_tokens)

            # Le solde courant est la colonne la plus à droite. Il doit être lu
            # indépendamment des colonnes débit/crédit : certains PDF BOA décalent
            # légèrement les chiffres et le précédent parseur pouvait prendre le
            # solde pour un montant de transaction.
            balance_tokens = [w for w in row if w['x0'] >= SOLDE_X_MIN and re.search(r'\d', w['text'])]
            if balance_tokens:
                bal_raw = ''.join(w['text'].replace('\xa0','').replace(' ','') for w in sorted(balance_tokens, key=lambda w:w['x0']))
                bal_neg = bal_raw.startswith('-')
                bal_clean = bal_raw.lstrip('-')
                bal_m = re.match(r'^(\d+)(?:[,\.]\d{2})?$', bal_clean)
                if bal_m:
                    bal_val = parse_amount(bal_clean.replace('.', '') if ',' in bal_clean else bal_clean)
                    if bal_val is not None:
                        boa_last_running_balance = -bal_val if bal_neg else bal_val

            # Cas particulier BOA : le montant apparaît avec un signe '-' dans la
            # colonne débit (ex: "-200 000,00") — certains relevés encodent ainsi.
            # Si la colonne crédit contient un montant négatif, c'est en réalité un débit.
            if credit_amt and credit_neg and debit_amt is None:
                debit_amt  = credit_amt
                credit_amt = None

            # Fallback sémantique si aucune colonne détectée (montant unique ambigu)
            if debit_amt is None and credit_amt is None:
                # Tenter de lire n'importe quel montant numérique à droite du libellé
                all_right = [w for w in row if w['x0'] >= DEBIT_X_MIN
                             and w['x0'] < SOLDE_X_MIN
                             and re.search(r'\d', w['text'])]
                fallback, fallback_neg = _parse_boa_amount_signed(all_right)
                if fallback:
                    is_credit_kw = any(k in label_up for k in CREDIT_KW)
                    is_debit_kw  = any(k in label_up for k in DEBIT_KW)
                    # Un signe '-' explicite force le débit quelle que soit la sémantique
                    if fallback_neg or (is_debit_kw and not is_credit_kw):
                        debit_amt = fallback
                    elif is_credit_kw and not is_debit_kw:
                        credit_amt = fallback
                    else:
                        debit_amt = fallback

            # Émettre la transaction
            if debit_amt and debit_amt > 0:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt and credit_amt > 0:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))

    # Priorité au solde de clôture explicite de l'en-tête (m_close plus haut).
    # On ne se rabat sur le solde courant de la dernière ligne du tableau que si
    # l'en-tête n'a fourni aucune valeur exploitable : sur les relevés triés du
    # plus récent au plus ancien (ex. BOA Villa Yemaya), la dernière ligne du
    # tableau est la transaction la plus ancienne, donc son solde courant est
    # proche du solde d'OUVERTURE, pas du solde de clôture.
    if info.get('balance_close') is None and boa_last_running_balance is not None:
        info['balance_close'] = boa_last_running_balance

    # Si le relevé ne donne pas de solde d'ouverture exploitable, reconstruire
    # celui-ci à partir du solde de clôture et des mouvements extraits. Cela permet
    # de conserver un OFX cohérent même lorsque l'en-tête BOA varie.
    clean_txns = [t for t in txns if t is not None]
    if not info.get('balance_open') and info.get('balance_close') is not None and clean_txns:
        net = sum(float(t.get('amount', 0) or 0) for t in clean_txns)
        info['balance_open'] = float(info['balance_close']) - net

    if not clean_txns and _pdf_path and Path(_pdf_path).exists():
        return _universal_parse_path(_pdf_path, pages_text)
    return info, clean_txns


# ════════════════════════════════════════════════════════════════════════════
# ORABANK Sénégal
# Format : Date | Libellé opération | Valeur | Débit | Crédit | Solde
# En-tête : "Orabank", "EXTRAIT DE COMPTE"
# Positions mesurées sur relevés Orabank AHC (déc 2025) et SUNBEAM (jan 2026) :
#   Date       : x0 < 75   (DD/MM/YYYY)
#   Libellé    : x0 ≈ 75–370
#   Valeur     : x0 ≈ 370–430 (date valeur — ignoré)
#   Débit      : x0 ≈ 430–510
#   Crédit     : x0 ≈ 510–590
#   Solde      : x0 ≥ 590  (ignoré)
# ════════════════════════════════════════════════════════════════════════════
def parse_orabank(pages_words, pages_text, _pdf_path=''):
    info = _afr_header(pages_text)
    year = _year_from_text(' '.join(pages_text[:2]))
    txns = []

    SKIP = {'TOTAL', 'SOLDE', 'DATE', 'VALEUR', 'LIBELLÉ', 'LIBELLE', 'DÉBIT', 'DEBIT',
            'CRÉDIT', 'CREDIT', 'EXTRAIT', 'ORABANK', 'PAGE', 'RELEVÉ', 'RELEVE',
            'TITULAIRE', 'CODE', 'IBAN', 'BIC', 'AGENCE', 'DESTINATAIRE', 'COMPTE',
            'TOTAL GÉNÉRAL', 'TOTAL GENERAL'}
    SKIP_START = ('SOLDE', 'TOTAL', 'ANCIEN SOLDE', 'NOUVEAU SOLDE')

    CREDIT_KW = {'VIREMENT', 'TRF RECU', 'TRF-RECU', 'TRF RECU AUTO', 'SORT CHEQUES',
                 'VERSEMENT', 'REMISE', 'CREDIT', 'SWIFT', 'RECOUVREMENT',
                 'W FREIGHT', 'W FREIGHT PA', 'AU426', 'VIREMENT INTER AGENCE',
                 'VIREMENT W', 'CHQ RECU COMPENSE', 'REMISE CHQ INTERNE',
                 'REMISE CHQ'}
    DEBIT_KW  = {'LUFTHANSA', 'RETRAIT', 'FRAIS', 'COMMISSION', 'AGIOS',
                 'ABONNEMENT', 'PACK PRO', 'CHEQUE', 'RETRAIT ESPECES',
                 'RETRAIT DAB', 'RETRAIT GAB', 'CHQ N.', 'RET. GAB',
                 'PAIEMENT VISA', 'PAIEMENT HORS', 'VIRMT FAV.'}

    full_text = ' '.join(pages_text)
    # "Solde (XOF) au 31/01/2026 : 5 272 384"  ou  "Solde au 31/01/2026 : ..."
    for bal_pat in [
        r'Solde\s+\([A-Z]+\)\s+au\s+[\d/]+\s*:\s*([\d\s]+)',
        r'Solde\s+au\s*[:\-]?\s*[\d/]+\s+([\d\s]+)',
    ]:
        m_bal = re.search(bal_pat, full_text, re.IGNORECASE)
        if m_bal:
            v = parse_amount(re.sub(r'\s+', '', m_bal.group(1).strip()))
            if v and v > 0:
                info['balance_close'] = v
                break

    # "Solde initial (XOF) : 863 532"
    m_bal_open = re.search(r'Solde\s+initial\s+\([A-Z]+\)\s*:\s*([\d\s]+)', full_text, re.IGNORECASE)
    if m_bal_open:
        v = parse_amount(re.sub(r'\s+', '', m_bal_open.group(1).strip()))
        if v is not None:
            info['balance_open'] = v

    for pw in pages_words:
        rows = group_words_by_row(pw, tol=4.0)
        i = 0
        while i < len(rows):
            row = rows[i]
            # Date : x0 < 80, DD/MM/YYYY
            date_words = [w for w in row if w['x0'] < 80 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
            if not date_words:
                i += 1; continue
            date_str = date_words[0]['text']

            # Libellé : x0 ≈ 75–400 (élargi pour capturer les deux formats)
            label_words = [w for w in row if 75 <= w['x0'] < 400
                           and not re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
            label = ' '.join(w['text'] for w in label_words).strip()
            label_up = label.upper()

            # Si libellé vide ou trop court sur la ligne de date,
            # chercher dans les lignes précédentes sans date (pattern Orabank SUNBEAM/BNDE)
            if not label or len(label) < 3:
                for k in range(i - 1, max(i - 6, -1), -1):
                    r2 = rows[k]
                    has_date2 = any(w['x0'] < 80 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])
                                    for w in r2)
                    if has_date2:
                        break
                    adj_words = [w for w in r2 if 75 <= w['x0'] < 400
                                 and not re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
                    adj = ' '.join(w['text'] for w in adj_words).strip()
                    if adj and len(adj) >= 3 and re.search(r'[A-Za-zÀ-ÿ]{2,}', adj):
                        label = adj
                        break
                label_up = label.upper()

            if not label or len(label) < 3:
                i += 1; continue
            if any(label_up.startswith(s) for s in SKIP_START):
                i += 1; continue
            if any(s == label_up for s in SKIP):
                i += 1; continue

            # ── Montants : stratégie adaptive ────────────────────────────────
            # Positions mesurées sur PDF Orabank réel (SUNBEAM Jan-2026) :
            #   Débit  header x0=342, tokens à x0 ≈ 330–420
            #   Crédit header x0=421, tokens à x0 ≈ 420–500
            #   Solde  header x0=503, tokens à x0 ≥ 500 (ignorer)
            DEBIT_X_MAX  = 420
            CREDIT_X_MIN = 420
            CREDIT_X_MAX = 500
            SOLDE_X_MIN  = 500

            right_words = sorted(
                [w for w in row if w['x0'] >= 330
                 and re.search(r'\d', w['text'])
                 and not re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])],
                key=lambda w: w['x0']
            )

            # Regrouper en blocs contigus (gap > 20px)
            blocs, cur, prev_x1 = [], [], None
            for w in right_words:
                if prev_x1 is not None and (w['x0'] - prev_x1) > 20:
                    if cur: blocs.append(cur)
                    cur = [w]
                else:
                    cur.append(w)
                prev_x1 = w.get('x1', w['x0'] + max(len(w['text']) * 6, 15))
            if cur: blocs.append(cur)

            resolved = []
            for b in blocs:
                v = _uba_join_amount(b)
                if v is not None:
                    resolved.append((v, b[0]['x0']))

            debit_amt = credit_amt = None
            non_solde = [(v, x) for v, x in resolved if x < SOLDE_X_MIN]

            if len(non_solde) == 1:
                val, x0 = non_solde[0]
                if x0 < DEBIT_X_MAX:
                    debit_amt = val
                elif CREDIT_X_MIN <= x0 < CREDIT_X_MAX:
                    credit_amt = val
                else:
                    # Ambiguïté positionnelle → sémantique
                    is_cr = any(k in label_up for k in CREDIT_KW)
                    is_db = any(k in label_up for k in DEBIT_KW)
                    if is_cr and not is_db:
                        credit_amt = val
                    elif is_db and not is_cr:
                        debit_amt = val
                    else:
                        # Défaut : débit si dans la moitié gauche, crédit sinon
                        if x0 < (DEBIT_X_MAX + CREDIT_X_MAX) / 2:
                            debit_amt = val
                        else:
                            credit_amt = val
            elif len(non_solde) >= 2:
                c_left  = min(non_solde, key=lambda x: x[1])
                c_right = max(non_solde, key=lambda x: x[1])
                if c_left[1] < DEBIT_X_MAX and c_right[1] >= CREDIT_X_MIN:
                    debit_amt  = c_left[0]
                    credit_amt = c_right[0]
                elif c_right[1] >= CREDIT_X_MIN:
                    credit_amt = c_right[0]
                elif c_left[1] < DEBIT_X_MAX:
                    debit_amt = c_left[0]
                else:
                    is_cr = any(k in label_up for k in CREDIT_KW)
                    if is_cr:
                        credit_amt = c_right[0]
                    else:
                        debit_amt = c_left[0]
            elif resolved:
                val, x0 = resolved[0]
                is_cr = any(k in label_up for k in CREDIT_KW)
                is_db = any(k in label_up for k in DEBIT_KW)
                if is_cr and not is_db:
                    credit_amt = val
                elif is_db and not is_cr:
                    debit_amt = val

            # Mémo lignes suivantes
            memo_parts = []
            j = i + 1
            while j < len(rows):
                r2 = rows[j]
                if r2 and any(w['x0'] < 80 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])
                               for w in r2):
                    break
                nl = ' '.join(w['text'] for w in r2 if 75 <= w['x0'] < 400).strip()
                if nl and not any(s in nl.upper() for s in SKIP):
                    memo_parts.append(nl)
                j += 1
            i = j

            date_ofx = date_full_to_ofx(date_str)
            name, memo = smart_label(label, memo_parts)
            if debit_amt and debit_amt > 0:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt and credit_amt > 0:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))

    if not txns and _pdf_path and Path(_pdf_path).exists():
        return _universal_parse_path(_pdf_path, pages_text)
    return info, [t for t in txns if t is not None]


# ════════════════════════════════════════════════════════════════════════════
# NSIA Banque (Sénégal)
# Format : Date Transact | Détails Transaction | Cheque N° | Agence |
#          Date Valeur | Mouv. Débit | Mouv. Crédit | Solde
# En-tête : "NSIA BANQUE", "RELEVE DE COMPTE"
# Positions mesurées sur relevés NSIA IROKO BEACH et AAG SENEGAL (mars 2026) :
#   Date Transact : x0 < 80   (DD/MM/YYYY)
#   Détails       : x0 ≈ 80–380
#   Cheque N°     : x0 ≈ 200–280 (optionnel)
#   Agence        : x0 ≈ 280–320 (ignoré)
#   Date Valeur   : x0 ≈ 320–390 (ignoré)
#   Mouv. Débit   : x0 ≈ 390–475
#   Mouv. Crédit  : x0 ≈ 475–560
#   Solde         : x0 ≥ 560  (ignoré)
# ════════════════════════════════════════════════════════════════════════════
def parse_nsia(pages_words, pages_text, _pdf_path=''):
    info = _afr_header(pages_text)
    year = _year_from_text(' '.join(pages_text[:2]))
    txns = []

    SKIP = {'TOTAL', 'SOLDE', 'DATE', 'VALEUR', 'DÉTAILS', 'DETAILS', 'DÉBIT', 'DEBIT',
            'CRÉDIT', 'CREDIT', 'CHEQUE', 'AGENCE', 'TRANSACTION', 'MOUV',
            'RELEVE', 'RELEVÉ', 'NSIA', 'PAGE', 'TITULAIRE', 'IBAN', 'BIC',
            'COMPTE', 'NUMÉRO', 'NUMERO', 'DEVISE', 'TOTAL DEBIT', 'TOTAL CREDIT'}
    SKIP_START = ('SOLDE', 'TOTAL DEBIT', 'TOTAL CREDIT', 'CHERS', 'VEUILLEZ',
                  'SEN/BANM', 'PAGE ')

    # Mots-clés sémantiques NSIA
    CREDIT_KW = {'VIREMENT PERMANENT', 'VIREMENT', 'REMISE', 'CREDIT', 'VERSEMENT',
                 'SWIFT', 'RECOUVREMENT', 'SV CLEARING', '-PURCHASE'}
    DEBIT_KW  = {'PAIEMENT DES CHQ', 'RETRAIT PAR CHQ', 'RETRAIT', 'FRAIS',
                 'COMMISSION MANUELLE', 'TAF SUR', 'TAX SUR', 'FRAIS DE TENUE',
                 'FRAIS DE GESTION', 'MAINTENANCE', 'PRELEV', 'PRELEVEMENT'}

    # Solde final depuis le texte
    full_text = ' '.join(pages_text)
    # Format NSIA : "SOLDE  1 136 108" en fin de relevé
    for pat in [
        r'SOLDE\s+(\d[\d\s]{3,})\s*$',
        r'SOLDE\s*\n\s*(\d[\d\s]{3,})',
        r'Solde\s+Final\s*[:\-]?\s*([\d\s]+)',
    ]:
        m = re.search(pat, full_text, re.IGNORECASE | re.MULTILINE)
        if m:
            v = parse_amount(m.group(1).strip().replace(' ', ''))
            if v:
                info['balance_close'] = v
                break

    # Solde d'ouverture : "Solde Début Période  26 484 486"
    m_open_nsia = re.search(r'Solde\s+D[ée]but\s+P[ée]riode\s+([\d\s]+)', full_text, re.IGNORECASE)
    if m_open_nsia:
        v = parse_amount(m_open_nsia.group(1).strip().replace(' ', ''))
        if v is not None:
            info['balance_open'] = v

    # Récupérer aussi le solde depuis la dernière ligne "SOLDE XXXX" du tableau
    solde_lines = re.findall(r'SOLDE\s+([\d][\d\s]{2,})', full_text, re.IGNORECASE)
    if solde_lines:
        last_v = parse_amount(solde_lines[-1].strip().replace(' ', ''))
        if last_v and last_v > 0:
            info['balance_close'] = last_v

    for pw in pages_words:
        rows = group_words_by_row(pw, tol=4.0)
        i = 0
        while i < len(rows):
            row = rows[i]
            # Date Transact : x0 < 80, DD/MM/YYYY
            date_words = [w for w in row if w['x0'] < 80 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
            if not date_words:
                i += 1; continue
            date_str = date_words[0]['text']

            # Détails Transaction : x0 ≈ 80–380 (élargi pour capturer descriptions longues)
            # Le libellé NSIA est souvent sur PLUSIEURS lignes précédant la ligne de date.
            # La date est sur une ligne qui contient aussi le n° agence et les montants.
            # Format IROKO/NSIA : la ligne de date contient "OPID: xxx TERM: xxx" comme
            # continuation du libellé, tandis que "VIREMENT. SV CLEARING :-PURCHASE"
            # se trouve sur la ligne précédente (sans date).
            # On exclut les tokens purement numériques longs (n° chèque ≥ 9 chiffres)
            # ainsi que les n° agence courts (2-3 chiffres à x0 ≈ 280-320).
            label_words = [w for w in row if 80 <= w['x0'] < 245
                           and not re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])
                           and not re.match(r'^\d{9,}$', w['text'])    # exclure n° chèque long
                           and not re.match(r'^\d{2,4}$', w['text'])]  # exclure n° agence (2-4 chiffres)
            label_same = ' '.join(w['text'] for w in label_words).strip()

            # Toujours chercher dans les lignes précédentes (jusqu'à 4 lignes en arrière)
            # même si le label courant n'est pas vide, car le type de transaction
            # (SV CLEARING :PURCHASE vs :-PURCHASE) est souvent sur la ligne précédente.
            parts_prev = []
            if i > 0:
                for k in range(i - 1, max(i - 5, -1), -1):
                    prev_row = rows[k]
                    prev_date = [w for w in prev_row if w['x0'] < 82
                                 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
                    if prev_date:
                        break  # autre transaction → stop
                    prev_label_words = [w for w in prev_row if 80 <= w['x0'] < 300
                                        and not re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])
                                        and not re.match(r'^\d{9,}$', w['text'])
                                        and not re.match(r'^\d{2,4}$', w['text'])]
                    pl = ' '.join(w['text'] for w in prev_label_words).strip()
                    if pl and not any(s in pl.upper() for s in SKIP):
                        parts_prev.insert(0, pl)

            # Construire le label complet : lignes précédentes + ligne courante
            label = ' '.join(parts_prev + ([label_same] if label_same else [])).strip()
            # Fallback : si toujours vide, prendre tout le texte de la ligne courante (zone élargie)
            if not label or len(label) < 3:
                label_words_wide = [w for w in row if 80 <= w['x0'] < 380
                                    and not re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])
                                    and not re.match(r'^\d{2,3}$', w['text'])]
                label = ' '.join(w['text'] for w in label_words_wide).strip()

            label_up = label.upper()
            if not label or len(label) < 3:
                i += 1; continue
            if any(label_up.startswith(s) for s in SKIP_START):
                i += 1; continue
            if label_up in SKIP:
                i += 1; continue

            # Mouv. Débit  : x0 ≈ 390–460  (tokens numériques dans cette zone)
            # Mouv. Crédit : x0 ≈ 460–515  (tokens numériques dans cette zone)
            # Solde        : x0 ≥ 515 → exclure absolument pour éviter toute confusion
            # IMPORTANT : ne pas filtrer les tokens à 3 chiffres comme '000' car
            # ils font partie des montants (ex: '17' + '500' = 17 500)
            right_words = sorted(
                [w for w in row if 380 <= w['x0'] < 515
                 and re.search(r'^\d+$', w['text'])  # uniquement les tokens purement numériques
                 and not re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])],
                key=lambda w: w['x0']
            )

            # Regrouper en blocs contigus (gap > 18px = nouveau bloc)
            nsia_blocs, cur_b, prev_x1_b = [], [], None
            for w in right_words:
                if prev_x1_b is not None and (w['x0'] - prev_x1_b) > 18:
                    if cur_b: nsia_blocs.append(cur_b)
                    cur_b = [w]
                else:
                    cur_b.append(w)
                prev_x1_b = w.get('x1', w['x0'] + max(len(w['text']) * 6, 15))
            if cur_b: nsia_blocs.append(cur_b)

            # Résoudre chaque bloc en (valeur, x0_debut)
            nsia_resolved = []
            for b in nsia_blocs:
                x0_b = b[0]['x0']
                v = _uba_join_amount(b)
                if v is not None and v > 0:
                    nsia_resolved.append((v, x0_b))

            # Positions mesurées sur relevé NSIA IROKO BEACH (avril 2026) :
            #   En-tête : "Mouv. Débit" x0=394, "Mouv. Crédit" x0=463, "Solde" x0=541
            #   Tokens débit  : x0 ≈ 419–437  (ex: '1','250' → '1 250')
            #   Tokens crédit : x0 ≈ 484–510  (ex: '17','500' → '17 500')
            #   Tokens solde  : x0 ≈ 524–548  → ignorer strictement
            NSIA_DEBIT_MIN   = 390
            NSIA_DEBIT_MAX   = 460
            NSIA_CREDIT_MIN  = 460
            NSIA_CREDIT_MAX  = 520
            NSIA_SOLDE_MIN   = 515   # ignorer tout ce qui est ≥ 515

            debit_amt  = None
            credit_amt = None

            # Stratégie NSIA : séparer par position absolue (pas par gap)
            # car les tokens crédit et solde peuvent être très proches
            debit_words_nsia  = [w for w in right_words if NSIA_DEBIT_MIN <= w['x0'] < NSIA_DEBIT_MAX]
            credit_words_nsia = [w for w in right_words if NSIA_CREDIT_MIN <= w['x0'] < NSIA_CREDIT_MAX]

            debit_amt  = _uba_join_amount(debit_words_nsia)  if debit_words_nsia  else None
            credit_amt = _uba_join_amount(credit_words_nsia) if credit_words_nsia else None

            # Filtrer les montants nuls
            if debit_amt is not None and debit_amt <= 0:
                debit_amt = None
            if credit_amt is not None and credit_amt <= 0:
                credit_amt = None

            # Reconstruire nsia_resolved pour le fallback
            nsia_resolved_all = []
            for b_words, x0_b in [(debit_words_nsia, NSIA_DEBIT_MIN),
                                   (credit_words_nsia, NSIA_CREDIT_MIN)]:
                if b_words:
                    v = _uba_join_amount(b_words)
                    if v and v > 0:
                        nsia_resolved_all.append((v, b_words[0]['x0']))
            nsia_resolved = nsia_resolved_all
            non_solde = nsia_resolved_all

            # Mémo lignes suivantes (continuation de la description)
            memo_parts = []
            j = i + 1
            while j < len(rows):
                r2 = rows[j]
                if r2 and any(w['x0'] < 80 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])
                               for w in r2):
                    break
                nl_words = [w for w in r2 if 80 <= w['x0'] < 380
                            and not re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])
                            and not re.match(r'^\d{3,4}$', w['text'])]
                nl = ' '.join(w['text'] for w in nl_words).strip()
                if nl and not any(s in nl.upper() for s in SKIP) and len(nl) > 2:
                    memo_parts.append(nl)
                j += 1
            i = j

            date_ofx = date_full_to_ofx(date_str)
            name, memo = smart_label(label, memo_parts)

            # ── Règle SV CLEARING NSIA ────────────────────────────────────────
            # Le terminal TPE NSIA génère pour chaque paiement carte 3 lignes :
            #   :PURCHASE  OPID=xxxx → frais réseau (petit montant) → IGNORER
            #   :PURCHASE  OPID=xxxx → TVA frais réseau             → IGNORER
            #   :-PURCHASE OPID=xxxx → montant principal            → GARDER (crédit)
            # La distinction est uniquement dans le libellé : ":-PURCHASE" vs ":PURCHASE"
            # NE PAS filtrer sur le montant car cela cause des erreurs.
            # Enrichir avec les lignes de mémo pour capturer le type quand il est
            # sur la ligne APRÈS la date (ex: ligne 51=VIREMENT, 52=DATE, 53=CLEARING).
            full_context_up = (label_up + ' ' + ' '.join(memo_parts)).upper()

            if debit_amt and debit_amt > 0:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt and credit_amt > 0:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))
            else:
                # Fallback sémantique : si aucun bloc dans les zones exactes,
                # prendre le premier bloc non-solde et utiliser les mots-clés
                if non_solde:
                    amt = non_solde[0][0]
                    is_credit = any(k in full_context_up for k in CREDIT_KW)
                    is_debit  = any(k in full_context_up for k in DEBIT_KW)
                    # SV CLEARING :-PURCHASE = entrée (crédit)
                    if ':-PURCHASE' in full_context_up or '-PURCHASE' in full_context_up:
                        txns.append(_make_txn(date_ofx, amt, name, memo))
                    elif ':PURCHASE' in full_context_up and ':-PURCHASE' not in full_context_up:
                        txns.append(_make_txn(date_ofx, -amt, name, memo))
                    elif is_credit and not is_debit:
                        txns.append(_make_txn(date_ofx, amt, name, memo))
                    elif is_debit and not is_credit:
                        txns.append(_make_txn(date_ofx, -amt, name, memo))
                else:
                    # Dernier recours : blocs résolus complets (incluant solde)
                    # Prendre le premier uniquement et déduire via sémantique
                    if nsia_resolved:
                        amt = nsia_resolved[0][0]
                        is_credit = any(k in full_context_up for k in CREDIT_KW)
                        is_debit  = any(k in full_context_up for k in DEBIT_KW)
                        if ':-PURCHASE' in full_context_up or '-PURCHASE' in full_context_up:
                            txns.append(_make_txn(date_ofx, amt, name, memo))
                        elif ':PURCHASE' in full_context_up and ':-PURCHASE' not in full_context_up:
                            txns.append(_make_txn(date_ofx, -amt, name, memo))
                        elif is_credit and not is_debit:
                            txns.append(_make_txn(date_ofx, amt, name, memo))
                        elif is_debit and not is_credit:
                            txns.append(_make_txn(date_ofx, -amt, name, memo))

    # ── Fallback texte brut désactivé pour NSIA IROKO ──────────────────────
    # Le parseur par positions (words) capture déjà toutes les SV CLEARING.
    # Le fallback texte brut causait des doublons car le label capturé
    # pour les :-PURCHASE est "OPID: xxxx" (sans le mot CLEARING),
    # ce qui rendait la condition de déclenchement du fallback toujours vraie.
    # On désactive ce fallback ; si le parseur words échoue totalement,
    # on tombe sur _universal_parse_path ci-dessous.

    if not txns and _pdf_path and Path(_pdf_path).exists():
        return _universal_parse_path(_pdf_path, pages_text)
    return info, [t for t in txns if t is not None]


# ── CORIS BANK : format Date | Libellé | Valeur | Débit | Crédit | Solde
# Montants XOF entiers (sans décimales), dates DD/MM/YYYY.
# Positions mesurées sur relevé Coris Bank Sénégal réel :
#   Date      : x0 < 75
#   Libellé   : x0 ≈ 75–310
#   Valeur    : x0 ≈ 310–390  (date valeur — on l'ignore)
#   Débit     : x0 ≈ 390–470
#   Crédit    : x0 ≈ 470–555
#   Solde     : x0 ≥ 555
def parse_coris(pages_words, pages_text, _pdf_path=''):
    info = _afr_header(pages_text)
    txns = []

    SKIP = {'SOLDE', 'TOTAL', 'TOTAUX', 'DATE', 'LIBELLÉ', 'LIBELLE', 'VALEUR',
            'DÉBIT', 'DEBIT', 'CRÉDIT', 'CREDIT', 'REPORT', 'A REPORTER',
            'NOMBRE', 'MOUVEMENTS', 'ANCIEN', 'NOUVEAU', 'EXTRAIT', 'COMPTE',
            'COMPTES COURANTS', 'RELEVE', 'RELEVÉ', 'PRÉCÉDENT', 'PRECEDENT'}

    CREDIT_KW = {'VERSEMENT', 'VERS.', 'VRT RECU', 'VIREMENT RECU', 'CREDIT',
                 'REMISE', 'SWIFT', 'DÉBLOCAGE', 'DEBLOCAGE', 'RECOUVREMENT',
                 'ANNULATION', 'RETROCESSION'}
    DEBIT_KW  = {'RET ', 'RETRAIT', 'CHEQUE', 'CHEQ', 'FRAIS', 'COMMISSION',
                 'VRT EMIS', 'VIREMENT EMIS', 'PRELEVEMENT', 'AGIOS',
                 'ABONNEMENT', 'IMPAYE', 'PACKAGE', 'CAUTION'}

    for pw in pages_words:
        rows = group_words_by_row(pw, tol=4.0)
        i = 0
        while i < len(rows):
            row = rows[i]

            # Date : DD/MM/YYYY en x0 < 75
            date_w = [w for w in row
                      if w['x0'] < 75 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
            if not date_w:
                i += 1; continue

            date_str = date_w[0]['text']

            # Libellé : x0 entre 75 et 310
            label_words = [w for w in row if 75 <= w['x0'] < 310]
            label = ' '.join(w['text'] for w in label_words).strip()
            label_up = label.upper()

            if not label or any(s in label_up for s in SKIP):
                i += 1; continue

            # Collecter les lignes de suite (mémo, libellé multi-lignes)
            memo_parts = []
            j = i + 1
            while j < len(rows):
                r2 = rows[j]
                # Stopper si nouvelle ligne de date
                if r2 and r2[0]['x0'] < 75 and re.match(r'^\d{2}/\d{2}/\d{4}$', r2[0]['text']):
                    break
                nl = ' '.join(w['text'] for w in r2 if 75 <= w['x0'] < 310).strip()
                if nl and not any(s in nl.upper() for s in SKIP):
                    memo_parts.append(nl)
                j += 1
            i = j

            # --- Montants ---
            # Positions mesurées sur PDF réel Coris Bank Sénégal :
            #   Débit  : x0 ≈ 363–420  (ex: '2','100','000' → 2 100 000)
            #   Crédit : x0 ≈ 445–495  (ex: '12','940','000' → 12 940 000)
            #   Solde  : x0 ≈ 520–575  (ex: '27','010','116' → ignoré)
            debit_words  = [w for w in row if 355 <= w['x0'] < 430]
            credit_words = [w for w in row if 440 <= w['x0'] < 510]

            debit_amt  = _uba_join_amount(debit_words)
            credit_amt = _uba_join_amount(credit_words)

            # Fallback sémantique si les colonnes se chevauchent ou sont absentes
            if debit_amt is None and credit_amt is None:
                # Tous les mots numériques à droite
                right_words = sorted([w for w in row if w['x0'] >= 380],
                                     key=lambda w: w['x0'])
                # Exclure les dates (valeur)
                right_words = [w for w in right_words
                               if not re.match(r'^\d{2}/\d{2}/\d{4}$', w['text'])]
                if right_words:
                    # Regrouper en blocs (gap > 25px)
                    blocs, cur, prev_x1 = [], [], None
                    for w in right_words:
                        is_num = bool(re.match(r'^[\d\s,\.]+$', w['text'])
                                      and re.search(r'\d', w['text']))
                        if not is_num:
                            if cur: blocs.append(cur); cur = []
                            prev_x1 = None; continue
                        if prev_x1 is not None and (w['x0'] - prev_x1) > 25:
                            if cur: blocs.append(cur)
                            cur = [w]
                        else:
                            cur.append(w)
                        prev_x1 = w.get('x1', w['x0'] + len(w['text']) * 7)
                    if cur: blocs.append(cur)

                    resolved = [(v, b[0]['x0']) for b in blocs
                                for v in [_uba_join_amount(b)] if v is not None]

                    if resolved:
                        is_credit = any(k in label_up for k in CREDIT_KW)
                        is_debit  = any(k in label_up for k in DEBIT_KW)

                        if len(resolved) == 1:
                            val, x0 = resolved[0]
                            if is_credit and not is_debit:
                                credit_amt = val
                            elif is_debit and not is_credit:
                                debit_amt = val
                            # sinon ambigu sans position fiable
                        elif len(resolved) >= 2:
                            # Exclure le solde (dernier bloc, le plus à droite)
                            candidates = resolved[:-1]
                            if len(candidates) == 1:
                                val, x0 = candidates[0]
                                if x0 >= 440:
                                    credit_amt = val
                                else:
                                    debit_amt = val
                            else:
                                c_left  = min(candidates, key=lambda x: x[1])
                                c_right = max(candidates, key=lambda x: x[1])
                                if is_credit and not is_debit:
                                    credit_amt = c_right[0]
                                elif is_debit and not is_credit:
                                    debit_amt = c_left[0]
                                else:
                                    if c_right[1] >= 440:
                                        credit_amt = c_right[0]
                                    else:
                                        debit_amt = c_left[0]

            date_ofx = date_full_to_ofx(date_str)
            name, memo = smart_label(label, memo_parts)
            if debit_amt and debit_amt > 0:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt and credit_amt > 0:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))

    # Fallback universel si rien trouvé
    if not txns and _pdf_path and Path(_pdf_path).exists():
        # Essayer d'abord le parseur universel
        info2, txns2 = _universal_parse_path(_pdf_path, pages_text)
        if txns2:
            return info2, txns2
        # Si toujours rien, utiliser Claude Vision (tableau dans image)
        try:
            info3, txns3 = _coris_vision_fallback(_pdf_path, info)
            if txns3:
                return info3, txns3
        except Exception as exc:
            logger.warning("Coris Vision fallback échoué : %s", exc)
    return info, [t for t in txns if t is not None]


def _coris_vision_fallback(pdf_path, info):
    """
    Fallback pour les relevés Coris Bank dont les données (dates, montants)
    sont intégrées dans une image. Envoie chaque page à l'API Claude Vision
    et parse la réponse JSON structurée.
    Requiert ANTHROPIC_API_KEY dans l'environnement et PyMuPDF ou pdf2image.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY absent — fallback vision impossible")

    # Convertir les pages PDF en images PNG
    images_b64 = _pdf_to_images_base64(pdf_path)
    if not images_b64:
        raise RuntimeError("Impossible de convertir le PDF en images")

    PROMPT_TEXT = (
        "Tu es un extracteur de relevé bancaire Coris Bank Sénégal. "
        "Ce relevé a les colonnes : Date | Libellé | Valeur | Débit | Crédit | Solde. "
        "Les montants sont en FCFA entiers (ex: 2 064 000, 65 000 000). "
        "Extrais TOUTES les lignes de transaction du tableau. "
        "Réponds UNIQUEMENT avec un JSON valide sans texte autour, format strict :\n"
        '{"transactions": ['
        '{"date": "DD/MM/YYYY", "libelle": "...", "debit": 0, "credit": 0}'
        ', ...]}\n'
        "Règles :\n"
        "- date : utilise la colonne Date (pas Valeur)\n"
        "- libelle : texte complet (concatène les lignes de suite si besoin)\n"
        "- debit / credit : montant numérique pur (entier, sans espaces ni virgule)\n"
        "- Ignore : 'Solde précédent', 'Report', 'Nombre de transactions', 'Total des mouvements', 'Solde au'\n"
        "- Les lignes FRAIS PACKAGE, VRT PERMANENT AUTRE BANQUE sont des vraies transactions à inclure"
    )

    txns = []
    for img_b64 in images_b64:
        payload = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 4096,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                    {"type": "text", "text": PROMPT_TEXT}
                ]
            }]
        }
        try:
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            logger.warning("Coris Vision API call failed: %s", exc)
            continue

        raw_text = ''.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text').strip()

        # Nettoyer les éventuels marqueurs de code
        if raw_text.startswith('```'):
            raw_text = re.sub(r'^```[a-z]*\n?', '', raw_text)
            raw_text = re.sub(r'\n?```$', '', raw_text.strip())
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            m = re.search(r'\{.*\}', raw_text, re.DOTALL)
            if not m:
                logger.warning("Coris Vision: réponse non-JSON : %s", raw_text[:200])
                continue
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                continue

        for t in parsed.get('transactions', []):
            date_str = str(t.get('date', '')).strip()
            label    = str(t.get('libelle', '')).strip()
            debit    = float(t.get('debit', 0) or 0)
            credit   = float(t.get('credit', 0) or 0)

            if not date_str or not label:
                continue
            date_ofx = date_full_to_ofx(date_str)
            if not re.match(r'^\d{8}$', date_ofx):
                continue

            if debit > 0:
                txn = _make_txn(date_ofx, -debit, label)
                if txn:
                    txns.append(txn)
            elif credit > 0:
                txn = _make_txn(date_ofx, credit, label)
                if txn:
                    txns.append(txn)

    return info, txns


def parse_ecobank(pages_words, pages_text, _pdf_path=''):
    """
    Parser dédié Ecobank Sénégal.
    Supporte deux formats :
      - Format anglais (Mega Max) : Date "30-May-2025", "Account Number", colonnes en pts
      - Format français            : Date DD/MM/YY, "Numéro de compte"
    Montants XOF entiers fragmentés. Pas d'IBAN : numéro de compte brut.
    """
    info = _afr_header(pages_text)
    full_text = ' '.join(pages_text)

    # ── Numéro de compte : supporte "Account Number" (anglais) et "Numéro de compte" ──
    if not info.get('iban') and not info.get('_rib_account'):
        m_cpte = re.search(
            r'(?:Account\s+Number|Num[eé]ro\s+de\s+compte)\s*[:\-]?\s*([\d]{6,20})',
            full_text, re.IGNORECASE
        )
        if m_cpte:
            num = m_cpte.group(1).strip()
            info['iban']         = num
            info['_rib_bank']    = '00000'
            info['_rib_agency']  = '00000'
            info['_rib_account'] = num
            info['_rib_key']     = ''

    # ── Période : formats français "Du 01/01/2025 Au 31/01/2025"
    #             et anglais "Statement From Date 01-05-2025 Statement To Date 31-05-2025" ──
    if not info.get('period_start'):
        # Format français
        m_per = re.search(
            r'(?:Du|du)\s+(\d{2}/\d{2}/\d{4})\s+(?:Au|au)\s+(\d{2}/\d{2}/\d{4})',
            full_text
        )
        if m_per:
            info['period_start'] = m_per.group(1)
            info['period_end']   = m_per.group(2)
        else:
            # Format anglais Ecobank : "01-05-2025"
            m_per2 = re.search(
                r'Statement\s+From\s+Date\s+(\d{2}-\d{2}-\d{4}).*?Statement\s+To\s+Date\s+(\d{2}-\d{2}-\d{4})',
                full_text, re.IGNORECASE
            )
            if m_per2:
                def _eco_reformat(s):
                    p = s.split('-')
                    return f"{p[0]}/{p[1]}/{p[2]}" if len(p)==3 else s
                info['period_start'] = _eco_reformat(m_per2.group(1))
                info['period_end']   = _eco_reformat(m_per2.group(2))

    # ── Solde de clôture : "Closing Balance XOF6,095,325.00" ou "Solde de clôture …" ──
    m_bal_en = re.search(r'Closing\s+Balance\s+(?:XOF)?([\d,\.]+)', full_text, re.IGNORECASE)
    if m_bal_en:
        raw = m_bal_en.group(1).replace(',', '')
        try: info['balance_close'] = float(raw)
        except ValueError: pass
    if not info.get('balance_close'):
        m_bal = re.search(r'Solde\s+de\s+cl[oô]ture\s+([\d\s]+)', full_text, re.IGNORECASE)
        if m_bal:
            raw = re.sub(r'\s+', '', m_bal.group(1))
            try: info['balance_close'] = float(raw)
            except ValueError: pass

    # ── Solde d'ouverture : "Opening Balance XOF8,383,905.00" ──
    m_open_en = re.search(r'Opening\s+Balance\s+(?:XOF)?([\d,\.]+)', full_text, re.IGNORECASE)
    if m_open_en:
        raw = m_open_en.group(1).replace(',', '')
        try: info['balance_open'] = float(raw)
        except ValueError: pass

    txns = []
    year = _year_from_text(full_text)

    # Mois anglais → numéro
    _MONTH_EN = {'Jan':'01','Feb':'02','Mar':'03','Apr':'04','May':'05','Jun':'06',
                 'Jul':'07','Aug':'08','Sep':'09','Oct':'10','Nov':'11','Dec':'12'}

    SKIP_UP = {'TOTAL','SOLDE','DATE','DATEVAL','TRANSACTION',
               'DÉBIT','DEBIT','CRÉDIT','CREDIT','PERIODE','PÉRIODE',
               'PAYMENTS','DEPOSITS','BALANCE','OPENING','CLOSING',
               'ACCOUNT','STATEMENT','DESCRIPTION','VALUE','REFERENCE'}
    # Page footer (page number + timestamp, and the legal disclaimer line)
    # sits right below the last transaction of a page with no date column,
    # so without this the continuation scan below swallows it into that
    # transaction's memo (e.g. "STANDING ORDER | 11:00").
    STOP_PAT_ECO = re.compile(
        r'^(\d{1,3}\s+\d{2}\s+[A-Za-z]{3}\s+\d{4},|Please\s+examine\s+this\s+statement|'
        r'information\s+below|\d{1,2}:\d{2}$)',
        re.IGNORECASE)

    def _eco_date(row):
        """Détecte une date en colonne gauche.
        Supporte DD/MM/YY, DD/MM/YYYY (format français)
        et DD-Mon-YYYY comme '30-May-2025' (format anglais Ecobank)."""
        for w in row:
            if w['x0'] < 90:
                # Format français
                if re.match(r'^\d{2}/\d{2}/(\d{2}|\d{4})$', w['text']):
                    return ('fr', w['text'])
                # Format anglais : "30-May-2025"
                m = re.match(r'^(\d{2})-([A-Za-z]{3})-(\d{4})$', w['text'])
                if m:
                    return ('en', w['text'])
        return None

    def _eco_date_ofx(date_info):
        mode, ds = date_info
        if mode == 'fr':
            p = ds.split('/')
            if len(p) == 3:
                dd, mm, yy = p
                yr = (2000+int(yy)) if len(yy)==2 and int(yy)<=30 else (1900+int(yy)) if len(yy)==2 else int(yy)
                return f"{yr}{mm.zfill(2)}{dd.zfill(2)}"
        elif mode == 'en':
            p = ds.split('-')
            if len(p) == 3:
                dd, mon, yyyy = p
                mm = _MONTH_EN.get(mon[:3].capitalize(), '01')
                return f"{yyyy}{mm}{dd.zfill(2)}"
        return str(year)+'0101'

    # Solde précédent (partagé entre toutes les pages)
    _eco_prev_solde = None
    # Flag pour ignorer la ligne complémentaire d'une paire B/O annulée
    _eco_skip_next_bo = False

    for pw in pages_words:
        rows = group_words_by_row(pw, tol=4.0)
        i = 0
        while i < len(rows):
            row = rows[i]
            date_info = _eco_date(row)
            if not date_info:
                i += 1; continue
            label_words = [w for w in row if 90 <= w['x0'] < 350]
            label = ' '.join(w['text'] for w in label_words).strip()
            label_up = label.upper()
            if not label or any(s in label_up for s in SKIP_UP) or re.match(r'^[\d\s/\-]+$', label):
                i += 1; continue
            memo_parts = []
            txn_type_suffix = ''   # COMM / TAX / PRIN récupéré depuis les lignes de continuation
            j = i + 1
            while j < len(rows):
                r2 = rows[j]
                if _eco_date(r2): break
                cont = ' '.join(w['text'] for w in r2 if 90 <= w['x0'] < 350).strip()
                if STOP_PAT_ECO.match(cont):
                    break
                if cont and not any(s in cont.upper() for s in SKIP_UP):
                    # La dernière ligne de continuation Ecobank est souvent COMM / TAX / PRIN
                    if re.match(r'^(COMM|TAX|PRIN)$', cont.upper()):
                        txn_type_suffix = cont.upper()
                    else:
                        memo_parts.append(cont)
                j += 1
            i = j

            # Enrichir le label avec le type d'opération Ecobank
            if txn_type_suffix:
                _type_labels = {'COMM': 'Commission', 'TAX': 'Taxe', 'PRIN': 'Principal'}
                label = label + ' — ' + _type_labels.get(txn_type_suffix, txn_type_suffix)

            # ── Colonnes montants ────────────────────────────────────────────
            def _eco_parse_en(words):
                """Parse un montant XOF anglais : 'XOF6,500.00' → 6500.0"""
                full = ' '.join(w['text'] for w in words).strip()
                full = re.sub(r'^XOF', '', full, flags=re.IGNORECASE).strip()
                full = full.replace(',', '')
                try:
                    v = float(full)
                    return v if v > 0 else None
                except ValueError:
                    return None

            if date_info[0] == 'en':
                # ── Format anglais Ecobank ──────────────────────────────────
                # Colonnes exactes (mesurées sur le PDF) :
                #   Payments  x0 ≈ 357  (<410) → DÉBIT
                #   Deposits  x0 ≈ 417  (≥410, <476) → CRÉDIT
                #   Balance   x0 ≈ 476  (≥476) → solde, à exclure
                #
                # Cas spécial : paire d'écritures B/O annulée
                #   Ligne 1 : tiret "-" dans Payments + solde → écriture d'annulation
                #   Ligne 2 : montant XOF dans Payments → écriture complémentaire
                #   Ces deux lignes se compensent (net = 0) → à ignorer toutes les deux.
                #
                # Tokens fusionnés (ex. "XOF3,000,000.00XOF8,224,583.00") :
                #   → 1er montant = opération, 2ème = solde

                def _eco_split_xof(text):
                    """Renvoie (montant_op, solde_opt) depuis un token XOF éventuellement fusionné."""
                    hits = re.findall(r'XOF([\d,]+\.?\d*)', text, re.IGNORECASE)
                    if not hits:
                        return None, None
                    vals = [float(h.replace(',', '')) for h in hits]
                    return (vals[0], vals[1]) if len(vals) >= 2 else (vals[0], None)

                # Détecter ligne d'annulation : tiret "-" dans zone Payments, aucun XOF op
                dash_in_pay = any(
                    w['text'] == '-' and 330 <= w['x0'] < 476 for w in row
                )

                xof_tokens = sorted(
                    [w for w in row if 'XOF' in w['text'] and w['x0'] >= 335],
                    key=lambda w: w['x0']
                )
                op_amt    = None
                cur_solde = None
                col_x0    = 0

                for w in xof_tokens:
                    v1, v2 = _eco_split_xof(w['text'])
                    if v2 is not None:          # token fusionné
                        op_amt    = v1
                        cur_solde = v2
                        col_x0    = w['x0']
                    elif w['x0'] >= 476:        # colonne Balance
                        cur_solde = v1
                    else:
                        if v1 and v1 > 0:
                            op_amt = v1
                            col_x0 = w['x0']

                if cur_solde is not None:
                    _eco_prev_solde = cur_solde

                # Ligne d'annulation (tiret) → ignorer + marquer la suivante
                if dash_in_pay and op_amt is None:
                    _eco_skip_next_bo = True
                    continue

                # Ligne complémentaire d'une paire annulée → ignorer
                if _eco_skip_next_bo and 'B/O' in label.upper():
                    _eco_skip_next_bo = False
                    continue
                _eco_skip_next_bo = False

                date_ofx = _eco_date_ofx(date_info)
                name, memo = smart_label(label, memo_parts)

                if op_amt and op_amt > 0:
                    if col_x0 >= 410:
                        txns.append(_make_txn(date_ofx, op_amt, name, memo))   # CRÉDIT
                    else:
                        txns.append(_make_txn(date_ofx, -op_amt, name, memo))  # DÉBIT

                continue  # passer le bloc format français ci-dessous

            else:
                # ── Format français Ecobank ─────────────────────────────────
                debit_words  = [w for w in row if 390 <= w['x0'] < 465]
                credit_words = [w for w in row if 465 <= w['x0'] < 535]
                debit_amt  = _uba_join_amount(debit_words)
                credit_amt = _uba_join_amount(credit_words)
                if debit_amt is None:
                    raw_d = ' '.join(w['text'] for w in debit_words)
                    if '- ' in raw_d or raw_d.strip().startswith('-'):
                        nums = re.sub(r'[^\d]', '', raw_d)
                        try: credit_amt = float(nums); debit_amt = None
                        except: pass

                date_ofx = _eco_date_ofx(date_info)
                name, memo = smart_label(label, memo_parts)
                if debit_amt and debit_amt > 0:
                    txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
                elif credit_amt and credit_amt > 0:
                    txns.append(_make_txn(date_ofx, credit_amt, name, memo))

    # Fallback universel si rien extrait
    if not txns and _pdf_path and Path(_pdf_path).exists():
        _, txns2 = _universal_parse_path(_pdf_path, pages_text)
        if txns2:
            return info, txns2

    return info, [t for t in txns if t is not None]


# ════════════════════════════════════════════════════════════════════════════
# PARSEUR WISE (Wise Europe SA — relevés EUR)
# ════════════════════════════════════════════════════════════════════════════

def _extract_wise_header(pages_text):
    """Extrait les métadonnées d'un relevé Wise (IBAN BE, période, solde)."""
    text = pages_text[0]
    info = {
        'iban': '',
        'period_start': '',
        'period_end': '',
        'balance_open': 0.0,
        'balance_close': 0.0,
    }

    # IBAN
    info['iban'] = extract_iban(text)

    # Période : "1 mai 2026 [GMT+02:00] - 31 mai 2026 [GMT+02:00]"
    # ou       "3 avril 2026 [GMT+02:00] - 2 mai 2026 [GMT+02:00]"
    MONTHS_FR = {
        'janvier':1,'février':2,'mars':3,'avril':4,'mai':5,'juin':6,
        'juillet':7,'août':8,'septembre':9,'octobre':10,'novembre':11,'décembre':12,
    }
    m = re.search(
        r'(\d{1,2})\s+([a-zéûô]+)\s+(\d{4}).*?-\s*(\d{1,2})\s+([a-zéûô]+)\s+(\d{4})',
        text, re.IGNORECASE
    )
    if m:
        def _ofx_date(d, mo_str, y):
            mo = MONTHS_FR.get(mo_str.lower(), 1)
            return f"{y}{mo:02d}{int(d):02d}"
        info['period_start'] = _ofx_date(m.group(1), m.group(2), m.group(3))
        info['period_end']   = _ofx_date(m.group(4), m.group(5), m.group(6))

    # Solde de clôture : "EUR du 31 mai 2026 [GMT+02:00]  240,98 EUR"
    mc = re.search(r'EUR\s+du\s+\d{1,2}\s+\w+\s+\d{4}[^\n]*?([\d\s]+,\d{2})\s*EUR', text)
    if mc:
        info['balance_close'] = parse_amount(mc.group(1)) or 0.0

    return info


def parse_wise(pages_words, pages_text, _pdf_path=None):
    """
    Parseur dédié aux relevés Wise (Wise Europe SA — relevés EUR).
    Structure texte :
      Ligne A : "Transaction de carte de XX,XX EUR émise par MARCHAND  [-XX,XX]  solde"
                "Argent reçu de NWA avec la référence REF  [XX,XX]  solde"
      Ligne B : "JJ mois AAAA  Carte se terminant par XXXX  ...  Transaction : CARD-XXX"
      (Parfois une ligne intermédiaire : suite du libellé ex "MASSY")
    """
    info = _extract_wise_header(pages_text)

    MONTHS_FR = {
        'janvier':1,'f\u00e9vrier':2,'mars':3,'avril':4,'mai':5,'juin':6,
        'juillet':7,'ao\u00fbt':8,'septembre':9,'octobre':10,'novembre':11,'d\u00e9cembre':12,
        'fevrier':2,'aout':8,
    }

    def _wise_date_ofx(date_str):
        """Convertit 'JJ mois AAAA' en AAAAMMJJ."""
        m = re.match(r'(\d{1,2})\s+([a-z\u00e9\u00fb\u00f4]+)\s+(\d{4})', date_str.strip(), re.IGNORECASE)
        if m:
            mo = MONTHS_FR.get(m.group(2).lower(), 1)
            return f"{m.group(3)}{mo:02d}{int(m.group(1)):02d}"
        return ''

    # Pattern : montant signé en fin de ligne de description
    # Ex: "-4,80 240,98"  → sortant 4.80
    #     "100,00 289,60" → entrant 100.00
    #     "-150,00 139,60"→ sortant 150.00
    pat_amounts = re.compile(r'(?<!\d)(-?\d{1,7},\d{2})\s+(\d{1,10},\d{2})\s*$')
    
    # Ligne de détail (date + carte/virement)
    pat_detail = re.compile(r'^(\d{1,2}\s+[a-z\u00e9\u00fb\u00f4]+\s+\d{4})\b', re.IGNORECASE)

    txns = []
    full_text = '\n'.join(pages_text)
    lines = [l.strip() for l in full_text.split('\n') if l.strip()]

    i = 0
    while i < len(lines):
        line = lines[i]

        # Cherche un montant signé ou positif en fin de ligne
        ma = pat_amounts.search(line)
        if not ma:
            i += 1
            continue

        # Vérifier qu'une ligne de date suit (éventuellement après une ligne de suite libellé)
        # Chercher la date parmi les 3 prochaines lignes
        date_ofx = ''
        detail_idx = -1
        desc_continuation = ''

        for offset in range(1, 4):
            if i + offset >= len(lines):
                break
            candidate = lines[i + offset]
            md = pat_detail.match(candidate)
            if md:
                date_ofx = _wise_date_ofx(md.group(1))
                detail_idx = i + offset
                # Si offset == 2, la ligne i+1 est une continuation du libellé
                if offset == 2:
                    desc_continuation = lines[i + 1]
                break
            # Si la ligne intermédiaire ressemble à une continuation (ville, suite marchand)
            # on continue à chercher, sinon on abandonne
            if re.match(r'^[A-Z][A-Z0-9\s\-\.]+$', candidate) or len(candidate) < 30:
                continue  # continuation probable
            else:
                break  # ce n'est pas une txn Wise

        if not date_ofx or detail_idx < 0:
            i += 1
            continue

        # Extraire le montant (colonne 1 = flux, colonne 2 = solde)
        col1_str = ma.group(1).strip()
        amount_val = parse_amount(col1_str)
        if amount_val is None:
            i = detail_idx + 1
            continue

        # Si col1 commence par "-" → sortant (débit)
        if col1_str.startswith('-'):
            amount = -abs(amount_val)
        else:
            # Montant positif → entrant ou remboursement
            amount = abs(amount_val)

        # Construire le libellé : retirer les montants en fin de ligne
        desc = pat_amounts.sub('', line).strip()
        if desc_continuation:
            desc = desc + ' ' + desc_continuation

        # Extraire le nom du marchand/émetteur
        mc = re.match(r'transaction\s+de\s+carte\s+de\s+[\d,\s]+EUR\s+[e\u00e9]mise\s+par\s+(.+)', desc, re.IGNORECASE)
        mt = re.match(r'argent\s+re[\u00e7c]u\s+de\s+(.+?)\s+avec\s+la\s+r[e\u00e9]f[e\u00e9]rence\s+(.+)', desc, re.IGNORECASE)

        if mc:
            label = mc.group(1).strip()
        elif mt:
            label = f"Virement reçu de {mt.group(1).strip()}"
        else:
            label = desc

        # Mémo depuis la ligne de détail
        detail_line = lines[detail_idx]
        memo_parts = []
        ref_m = re.search(r'Transaction\s*:\s*(\S+)', detail_line, re.IGNORECASE)
        if ref_m:
            memo_parts.append(ref_m.group(1))
        ref_m2 = re.search(r'R[e\u00e9]f[e\u00e9]rence\s*:\s*(\S+)', detail_line, re.IGNORECASE)
        if ref_m2:
            memo_parts.append(ref_m2.group(1))
        memo_str = ' | '.join(memo_parts)

        if date_ofx and label:
            txns.append(_make_txn(date_ofx, amount, label[:64], memo_str[:128]))

        i = detail_idx + 1

    # Fallback universel si rien extrait
    if not txns and _pdf_path and Path(_pdf_path).exists():
        _, txns2 = _universal_parse_path(_pdf_path, pages_text)
        if txns2:
            return info, txns2

    return info, [t for t in txns if t is not None]


# ════════════════════════════════════════════════════════════════════════════
# DEVISE & LABELS
# ════════════════════════════════════════════════════════════════════════════

BANK_CURRENCY = {
    'QONTO':'EUR','LCL':'EUR','CA':'EUR','CE':'EUR','BP':'EUR','CIC':'EUR',
    'CM':'EUR','CMB':'EUR','CGD':'EUR','LBP':'EUR','SG':'EUR','BNP':'EUR','MYPOS':'EUR','SHINE':'EUR',
    'CBAO':'XOF','ECOBANK':'XOF','BCI':'XOF','CORIS':'XOF','UBA':'XOF',
    'ORABANK':'XOF','BOA':'XOF','ATB':'TND','SG_AFRIQUE':'XOF','BSIC':'XOF',
    'BIS':'XOF','BNDE':'XOF','UNIVERSAL':'XOF','NSIA':'XOF','WISE':'EUR',
}

BANK_LABELS = {
    'QONTO':'Qonto','LCL':'LCL (Crédit Lyonnais)','CA':'Crédit Agricole',
    'CE':"Caisse d'Épargne",'BP':'Banque Populaire','CIC':'CIC',
    'CM':'Crédit Mutuel','CMB':'Crédit Mutuel de Bretagne',
    'CGD':'Caixa Geral de Depositos','LBP':'La Banque Postale',
    'SG':'Société Générale','BNP':'BNP Paribas','MYPOS':'myPOS',
    'SHINE':'Shine (néo-banque pro)','CBAO':'CBAO (Sénégal)',
    'ECOBANK':'Ecobank','BCI':'BCI','CORIS':'Coris Bank','UBA':'UBA',
    'ORABANK':'Orabank','BOA':'Bank of Africa','ATB':'Arab Tunisian Bank',
    'SG_AFRIQUE':'Société Générale Afrique','BSIC':'BSIC (Sénégal)',
    'BIS':'Banque Islamique du Sénégal','BNDE':'BNDE','UNIVERSAL':'Format universel',
    'NSIA':'NSIA Banque','WISE':'Wise (néo-banque internationale)',
}

AFRICAN_BANKS = {'CBAO','ECOBANK','BCI','CORIS','UBA','ORABANK','BOA','ATB',
                 'SG_AFRIQUE','UNIVERSAL','BSIC','BIS','BNDE','NSIA'}

# Banques (non-africaines) dont le parseur a besoin du chemin PDF, ex. pour
# réextraire avec une tolérance x différente. N'inclut PAS 'CM' : le
# parseur CM générique reste inchangé et n'a pas besoin de _pdf_path.
NEEDS_PDF_PATH = {'CMB'}

PARSERS = {
    'QONTO':parse_qonto,'LCL':parse_lcl,'CA':parse_ca,'CE':parse_ce,
    'BP':parse_bp,'CIC':parse_cic,'CM':parse_cm,'CMB':parse_cmb,'CGD':parse_cgd,'LBP':parse_lbp,
    'SG':parse_sg,'BNP':parse_bnp,'MYPOS':parse_mypos,'SHINE':parse_shine,
    'CBAO':parse_cbao,'ECOBANK':parse_ecobank,'BCI':parse_bci,'CORIS':parse_coris,
    'UBA':parse_uba,'ORABANK':parse_orabank,'BOA':parse_boa,'ATB':parse_atb,
    'SG_AFRIQUE':parse_sg_afrique,'UNIVERSAL':parse_universal,
    'BSIC':parse_bsic,'BIS':parse_bis,'BNDE':parse_bnde,'NSIA':parse_nsia,'WISE':parse_wise,
}


# ════════════════════════════════════════════════════════════════════════════
# GÉNÉRATION OFX
# ════════════════════════════════════════════════════════════════════════════

def period_to_ofx(date_str):
    try:
        p = date_str.split('/')
        return f"{p[2]}{p[1].zfill(2)}{p[0].zfill(2)}"
    except:
        return datetime.now().strftime('%Y%m%d')

def _clamp_balance_for_ofx(bal: float) -> tuple:
    """
    Quadra/Cegid limite le champ BALAMT à 11 caractères total
    (signe éventuel + chiffres + point + 2 décimales).
    - Avec décimales : max 9 999 999.99  (format "9999999.99"  = 10 chars)
    - Sans décimales : max 999 999 999   (format "999999999"   = 9 chars)
    En pratique Quadra bloque à partir de 10 chiffres entiers (ex: 1 000 000 000).
    On tronque à 9 999 999.99 pour laisser une marge.
    Retourne (valeur_clampée, True si tronquée).
    """
    QUADRA_MAX = 9_999_999_999.99   # ~10 milliards — limite sûre Quadra
    if abs(bal) > QUADRA_MAX:
        sign = -1 if bal < 0 else 1
        return sign * QUADRA_MAX, True
    return bal, False


def _format_balamt(bal: float, currency: str) -> str:
    """
    Formate le solde pour le champ BALAMT OFX.
    - XOF / devises sans centimes : entier sans décimales
    - EUR et autres : 2 décimales
    Quadra accepte mieux les entiers pour les devises africaines.
    """
    if currency in ('XOF', 'XAF', 'GNF', 'MGA'):
        return f"{int(round(bal))}"
    return f"{bal:.2f}"


def generate_ofx(info, txns, target='quadra', currency='EUR'):
    iban_full = info.get('iban', '') or ''
    bid, brid, aid = iban_to_rib(iban_full, info=info)

    # ── ACCTID pour Quadra / Money ────────────────────────────────────────────
    # Priorite :
    #   1. RIB extrait directement du PDF :
    #      - Banques africaines (IBAN >= 26 chars ou IBAN alphanum) : ACCTID =
    #        numéro de compte seul (ce que Money utilise pour identifier le client)
    #      - Banques FR (IBAN=27 chars, tout numérique) : ACCTID = Banque+Guichet+Compte+Cle
    #   2. IBAN court (<= 22 chars) -> utiliser l'IBAN compact directement.
    #   3. IBAN long (> 22 chars, UEMOA SN=28...) sans _rib_account -> BBAN compte
    #   4. Fallback fragment iban_to_rib().
    iban_is_real  = bool(re.match(r'^[A-Z]{2}\d{2}', iban_full.replace(' ','')))
    iban_compact  = re.sub(r'\s+', '', iban_full)
    # Detecter si c'est une banque africaine : IBAN > 25 chars OU BBAN contient des lettres
    bban_part = iban_compact[4:] if len(iban_compact) > 4 else ''
    is_african_iban = len(iban_compact) > 25 or bool(re.search(r'[A-Z]', bban_part))

    if info.get('_rib_account'):
        bank = info.get('_rib_bank', '') or ''
        if is_african_iban or not iban_is_real:
            # Banque africaine OU num compte brut (pas d'IBAN) :
            # Quadra reconnait le compte via le numero seul
            acctid = info['_rib_account']
        elif bank and bank != '00000':
            # Banque francaise avec RIB complet : Banque+Guichet+Compte+Cle
            acctid = (bank +
                      info.get('_rib_agency','') +
                      info.get('_rib_account','') +
                      info.get('_rib_key',''))
        else:
            acctid = info['_rib_account']
    elif iban_is_real and len(iban_compact) <= 22:
        # IBAN court (FR, BE, NL...) -> IBAN compact
        acctid = iban_compact
    elif iban_is_real and len(iban_compact) > 22:
        # IBAN long (UEMOA SN=28...) -> extraire le numero de compte du BBAN
        # Format BCEAO : CC(2)+KK(2)+Banque(5)+Agence(5)+Compte(11-12)+Cle(2)
        bban = iban_compact[4:]
        compte_bceao = bban[10:-2] if len(bban) >= 13 else bban[10:]
        acctid = compte_bceao if compte_bceao else iban_compact[:22]
    else:
        # Fallback fragment RIB
        acctid = aid

    # ── ACCTID : limiter a 22 caracteres (limite OFX SGML) ───────────────────
    acctid = acctid[:22] if acctid else '0000000000'

    # ── BANKID / BRANCHID : purger les caractères non-alphanumériques ─────────
    bid  = re.sub(r'[^A-Z0-9]', '', bid.upper())[:11]  if bid  else '00000'
    brid = re.sub(r'[^A-Z0-9]', '', brid.upper())[:11] if brid else '00000'

    ds  = period_to_ofx(info.get('period_start',''))
    de  = period_to_ofx(info.get('period_end',''))
    dn  = datetime.now().strftime('%Y%m%d%H')
    # DTASOF doit correspondre à la date de fin du relevé, pas à la date de génération.
    balance_date = de or dn[:8]

    # ── Solde : limité à 13 chiffres pour compatibilité Quadra/Cegid ──────────
    bal_raw = info.get('balance_close', 0.0)
    bal, _bal_truncated = _clamp_balance_for_ofx(bal_raw)

    memo_carries_label = target in ('myunisoft','sage','ebp')
    lines = [
        'OFXHEADER:100','DATA:OFXSGML','VERSION:102','SECURITY:NONE',
        'ENCODING:USASCII','CHARSET:1252','COMPRESSION:NONE',
        'OLDFILEUID:NONE','NEWFILEUID:NONE',
        '<OFX>','<SIGNONMSGSRSV1>','<SONRS>','<STATUS>',
        '<CODE>0','<SEVERITY>INFO','</STATUS>',
        f'<DTSERVER>{dn}','<LANGUAGE>FRA',
        '</SONRS>','</SIGNONMSGSRSV1>',
        '<BANKMSGSRSV1>','<STMTTRNRS>','<TRNUID>00',
        '<STATUS>','<CODE>0','<SEVERITY>INFO','</STATUS>',
        '<STMTRS>',f'<CURDEF>{currency}','<BANKACCTFROM>',
        f'<BANKID>{bid}',f'<BRANCHID>{brid}',
        f'<ACCTID>{acctid}','<ACCTTYPE>CHECKING','</BANKACCTFROM>',
        '<BANKTRANLIST>',f'<DTSTART>{ds}',f'<DTEND>{de}',
    ]
    for t in txns:
        name = t['name']
        memo = t.get('memo', '') or ''
        if memo_carries_label:
            name_tag = name
            memo_tag = (name + ' | ' + memo) if memo else name
        else:
            name_tag = name
            memo_tag = memo
        # Échapper les champs texte pour ne jamais produire un OFX invalide
        # lorsqu'un libellé contient &, < ou >.
        def _ofx_escape(value):
            return (str(value).replace('&', '&amp;')
                    .replace('<', '&lt;')
                    .replace('>', '&gt;'))
        lines += [
            '<STMTTRN>',
            f"<TRNTYPE>{t['type']}",
            f"<DTPOSTED>{t['date']}",
            f"<TRNAMT>{_format_balamt(t['amount'], currency)}",
            f"<FITID>{t['fitid']}",
            '<NAME>' + _ofx_escape(name_tag),
            '<MEMO>' + _ofx_escape(memo_tag),
            '</STMTTRN>',
        ]
    bal_fmt = _format_balamt(bal, currency)
    # TRNAMT : aussi entier pour devises sans centimes
    lines += [
        '</BANKTRANLIST>',
        f'<LEDGERBAL>',f'<BALAMT>{bal_fmt}',f'<DTASOF>{balance_date}','</LEDGERBAL>',
        f'<AVAILBAL>',f'<BALAMT>{bal_fmt}',f'<DTASOF>{balance_date}','</AVAILBAL>',
        '</STMTRS>','</STMTTRNRS>','</BANKMSGSRSV1>','</OFX>',
    ]
    return '\n'.join(lines) + '\n'


# ════════════════════════════════════════════════════════════════════════════
# FONCTION PRINCIPALE DE CONVERSION (avec cache Streamlit)
# ════════════════════════════════════════════════════════════════════════════

# Browser-specific replacements. The native app used pdfplumber paths and
# optional Claude Vision as fallbacks. In the browser we already provide
# structured PDF.js/Tesseract words, so the universal fallback is text-based.
def _browser_universal(pages_text):
    info = _extract_universal_header(pages_text)
    year_hint = _year_from_text(' '.join(pages_text[:2]))
    txns = []
    SKIP_TEXT = ('SOLDE', 'TOTAL', 'TOTAUX', 'REPORT', 'DATE', 'VALEUR',
                 'LIBELLÉ', 'LIBELLE', 'DÉBIT', 'DEBIT', 'CRÉDIT', 'CREDIT',
                 'EUROS', 'MONTANT', 'PAGE', 'SUITE', 'VERSO', 'REF :',
                 'IBAN', 'BIC', 'AGENCE', 'COMPTE', 'TITULAIRE', 'ADRESSE',)
    DATE_RE = re.compile(r'^(\d{2}[/\-.]\d{2}[/\-.]\d{2,4})')
    AMT_RE  = re.compile(r'([\d]{1,3}(?:[.\s]\d{3})*[,]\d{2}|[\d]+[,]\d{2}|[\d]+[.]\d{2})')
    prev_solde = None
    full = '\n'.join(pages_text).replace('\xa0', ' ').replace('\u202f', ' ')
    for line in full.splitlines():
        line = line.strip()
        if not line:
            continue
        m_date = DATE_RE.match(line)
        if not m_date:
            continue
        date_raw = m_date.group(1).replace('.', '/').replace('-', '/')
        date_ofx = _parse_date_universal(date_raw, year_hint)
        if not date_ofx:
            continue
        line_up = line.upper()
        if any(kw in line_up for kw in SKIP_TEXT):
            continue
        amounts_found = AMT_RE.findall(line)
        amounts_vals = [v for v in (parse_amount(a) for a in amounts_found) if v and v > 0.5]
        if not amounts_vals:
            continue
        label_part = DATE_RE.sub('', line, count=1).strip()
        label_part = DATE_RE.sub('', label_part, count=1).strip()
        label_part = re.sub(r'[\d\s.,]+$', '', label_part).strip()
        label_part = re.sub(r'^[^\w]+', '', label_part).strip()
        if not label_part or len(label_part) < 3 or not re.search(r'[A-Za-zÀ-ÿ]{2,}', label_part):
            continue
        label_up2 = label_part.upper()
        if any(kw in label_up2 for kw in SKIP_TEXT):
            continue
        is_credit = None
        if len(amounts_vals) >= 2:
            solde_courant = amounts_vals[-1]
            montant_op = amounts_vals[-2]
            if prev_solde is not None:
                diff = solde_courant - prev_solde
                if diff > 0.5: is_credit = True
                elif diff < -0.5: is_credit = False
            prev_solde = solde_courant
            amt = montant_op
        else:
            amt = amounts_vals[0]
        if is_credit is None:
            CREDIT_KW = ('VIR ', 'VIREMENT', 'REGLEMENT', 'REMBOURSEMENT', 'VERSEMENT', 'AVOIR', 'RETOUR', 'REMISE', 'CREDIT', 'EDENRED', 'DELIVEROO', 'PLUXEE', 'BIMPLI', 'UBER', 'QUATRA', 'SCI ', 'RECETTE')
            DEBIT_KW  = ('PRLV', 'PRELEVEMENT', 'PAIEMENT CB', 'PAIEMENT PSC', 'PREL ', 'FACT ', 'COTISATION', 'ABONNEMENT', 'COMMISSION', 'FRAIS', 'AGIOS', 'RETRAIT', 'CHEQUE', 'LOYER', 'EDF', 'ORANGE', 'DGFIP', 'GENERALI', 'MAXANCE', 'SURAVENIR')
            if any(k in label_up2 for k in CREDIT_KW): is_credit = True
            elif any(k in label_up2 for k in DEBIT_KW): is_credit = False
            else: is_credit = False
        signed = amt if is_credit else -amt
        name, memo = smart_label(label_part, [])
        txn = _make_txn(date_ofx, signed, name, memo)
        if txn: txns.append(txn)
    return info, [t for t in txns if t is not None]

def _universal_parse_path(pdf_path, pages_text):
    return _browser_universal(pages_text)

def _coris_vision_fallback(pdf_path, info):
    return info, []

def parse_bci(pages_words, pages_text, _pdf_path=''):
    return _browser_universal(pages_text)

def parse_atb(pages_words, pages_text, _pdf_path=''):
    return _browser_universal(pages_text)

def parse_universal(pages_words, pages_text, _pdf_path=''):
    # Prefer a bank-independent text parser if no dedicated parser matches.
    info, txns = _browser_universal(pages_text)
    return info, txns

PARSERS.update({
    'BCI': parse_bci,
    'ATB': parse_atb,
    'UNIVERSAL': parse_universal,
})

def browser_convert(bank, pages_words, pages_text):
    bank = bank or detect_bank(pages_text)
    parser = PARSERS.get(bank, parse_universal)
    try:
        info, txns = parser(pages_words, pages_text, _pdf_path='')
    except TypeError:
        info, txns = parser(pages_words, pages_text)
    if not txns:
        info_u, txns_u = _browser_universal(pages_text)
        if txns_u:
            info, txns = info_u, txns_u
    return bank, info, txns

def browser_generate_ofx(info, txns, target='quadra', currency='EUR'):
    return generate_ofx(info, txns, target=target, currency=currency)

def handle_json(payload):
    action = payload.get('action')
    if action == 'parse':
        bank, info, txns = browser_convert(payload.get('bank',''), payload.get('pages_words',[]), payload.get('pages_text',[]))
        return {'bank': bank, 'info': info, 'transactions': txns}
    if action == 'detect':
        return {'bank': detect_bank(payload.get('pages_text',[]))}
    if action == 'ofx':
        return {'ofx': browser_generate_ofx(payload.get('info',{}), payload.get('transactions',[]), payload.get('target','quadra'), payload.get('currency','EUR'))}
    raise ValueError('Unknown action')

def run_json(s):
    try:
        return json.dumps(handle_json(json.loads(s)), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({'error': f'{type(exc).__name__}: {exc}'}, ensure_ascii=False)
