import React, { useState, useEffect } from 'react'
import { supabase } from './lib/supabase'
import { LandingPage } from './components/LandingPage'
import { AuthModal } from './components/AuthModal'
import { Converter } from './components/Converter'
import { ErrorBoundary } from './components/ErrorBoundary'
import { createRoot } from 'react-dom/client'
import './styles.css'

export function App() {
  const [session, setSession] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [showAuthModal, setShowAuthModal] = useState(false)
  const [authMode, setAuthMode] = useState<'login' | 'signup'>('login')

  useEffect(() => {
    if (!supabase) {
      console.warn(
        "Supabase n'est pas configuré (VITE_SUPABASE_URL / VITE_SUPABASE_PUBLISHABLE_KEY manquantes). " +
        "L'authentification est désactivée."
      )
      setLoading(false)
      return
    }
    
    supabase.auth.getSession().then(({ data: { session } }) => {
      setSession(session)
      setLoading(false)
    })

    const { data: { subscription } } = supabase.auth.onAuthStateChange((_event, session) => {
      setSession(session)
      setLoading(false)
    })

    return () => subscription.unsubscribe()
  }, [])

  const handleOpenAuth = (mode: 'login' | 'signup') => {
    setAuthMode(mode)
    setShowAuthModal(true)
  }

  if (loading) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center text-slate-400">
        Chargement...
      </div>
    )
  }

  // Utilisateur NON connecté -> Landing Page
  if (!session) {
    return (
      <>
        <LandingPage 
          onLogin={() => handleOpenAuth('login')}
          onGetStarted={() => handleOpenAuth('signup')}
        />
        {showAuthModal && (
          <AuthModal 
            initialMode={authMode}
            onSuccess={() => setShowAuthModal(false)}
            onCancel={() => setShowAuthModal(false)}
          />
        )}
      </>
    )
  }

  // Utilisateur CONNECTÉ -> Outil de conversion + bouton déconnexion
  return (
    <div>
      <div className="bg-slate-900 border-b border-slate-800 px-6 py-3 flex items-center justify-between text-xs text-slate-300">
        <span>Connecté en tant que : <b>{session.user.email}</b></span>
        <button 
          onClick={() => supabase?.auth.signOut()}
          className="bg-slate-800 hover:bg-slate-700 text-white px-3 py-1.5 rounded transition-colors"
        >
          Se déconnecter
        </button>
      </div>
      <Converter />
    </div>
  )
}

export default App

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary>
    <App />
    </ErrorBoundary>
  </React.StrictMode>
)
