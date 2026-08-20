# --- Client (préfixe VITE_ obligatoire, ces valeurs finissent dans le bundle JS public) ---
VITE_SUPABASE_URL=
VITE_SUPABASE_PUBLISHABLE_KEY=

# --- Serveur UNIQUEMENT (Netlify Function) ---
# NE JAMAIS préfixer par VITE_ : une variable VITE_ est intégrée en clair
# dans le JS envoyé au navigateur, ce qui exposerait la clé à tous les
# visiteurs du site. GEMINI_API_KEY doit être définie dans Netlify sous
# Site settings -> Environment variables (pas dans un fichier .env commité),
# et reste uniquement lue côté serveur par netlify/functions/gemini-extract.mts.
GEMINI_API_KEY=
