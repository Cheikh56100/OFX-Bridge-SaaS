import { createClient } from '@supabase/supabase-js'

const rawUrl = import.meta.env.VITE_SUPABASE_URL
const rawKey = import.meta.env.VITE_SUPABASE_ANON_KEY || import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY

// URL et clé de secours pour éviter le crash au chargement
const supabaseUrl = (rawUrl && rawUrl.startsWith('http')) 
  ? rawUrl 
  : 'https://rwfwqnxxjyydaoagpkik.supabase.co'

const supabaseAnonKey = rawKey || 'sb_publishable_cCeYmbuMo85UHaEAZB8Blw_Y6grd8Ja'

export const supabase = createClient(supabaseUrl, supabaseAnonKey)
