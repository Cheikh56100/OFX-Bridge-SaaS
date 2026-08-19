# OFX Bridge — SaaS Netlify + Supabase

Cette version reprend l’interface SaaS React/Vite et embarque le moteur bancaire complet de la première application. Pour garantir la parité avec les parseurs bancaires existants, le code métier Python d’origine est exécuté **localement dans le navigateur via Pyodide**, piloté par TypeScript. Il n’y a pas de backend Python ni d’envoi du PDF.

## Moteur bancaire

Le fichier `public/engine.py` contient les utilitaires, la détection bancaire, les parseurs Qonto, LCL, Crédit Agricole, Caisse d’Épargne, Banque Populaire, CIC, Crédit Mutuel, CMB, CGD, Banque Postale, Société Générale, BNP, myPOS, Shine, CBAO, Ecobank, BCI, Coris, UBA, Orabank, BOA, ATB, SG Afrique, BSIC, BIS, BNDE, NSIA, Wise et le parseur universel, ainsi que le générateur OFX.

Le traitement PDF natif utilise PDF.js. Pour les scans, Tesseract.js fournit le texte **et les coordonnées des mots**, ce qui permet de conserver les parseurs basés sur les positions de colonnes.

## Installation

```bash
npm install
npm run dev
```

Copier `.env.example` vers `.env.local` et renseigner Supabase.

## Déploiement Netlify

- Build command: `npm run build`
- Publish directory: `dist`
- Variables (optionnelles) : `VITE_SUPABASE_URL`, `VITE_SUPABASE_PUBLISHABLE_KEY`

`netlify.toml` configure également le fallback SPA et fixe `NODE_VERSION = 20`.

**Important — dossier de base** : les fichiers de ce zip (`package.json`, `src/`, `public/`...) doivent être à la **racine** du dépôt/dossier déployé sur Netlify. Si vous les mettez dans un sous-dossier (ex. `ofxbridge_final/`), configurez alors "Base directory" = `ofxbridge_final` dans Netlify, sinon le build échoue car `package.json` est introuvable.

Si vous déployez sans passer par Git (glisser-déposer sur Netlify), déposez le **dossier `dist` généré par `npm run build`**, pas le code source — Netlify ne fait pas `npm install` dans ce mode.

## Supabase

Exécuter `supabase/migrations/001_initial.sql` dans le SQL Editor. Les RLS sont activées.

## Confidentialité

Les PDF et les images OCR restent dans le navigateur.

## Important

Pyodide est chargé depuis le CDN officiel au premier traitement et mis en cache par le navigateur. Cela augmente le premier temps de démarrage du moteur, mais évite de réécrire/réinterpréter les règles bancaires et conserve le comportement de la première version.

### Premier lancement OCR/parser
Le premier PDF peut prendre plus de temps car Pyodide (~runtime Python WebAssembly) est téléchargé puis initialisé. Les traitements suivants bénéficient du cache navigateur.
