import React from 'react'

interface Props {
  children: React.ReactNode
}

interface State {
  error: Error | null
}

/**
 * Filet de sécurité : si un composant plante au rendu (erreur JS non gérée),
 * on affiche un message clair au lieu de laisser la page blanche.
 */
export class ErrorBoundary extends React.Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('Erreur applicative interceptée :', error, info)
  }

  render() {
    if (this.state.error) {
      return (
        <div className="min-h-screen bg-slate-950 flex items-center justify-center p-6">
          <div className="max-w-lg w-full bg-slate-900 border border-slate-800 rounded-2xl p-8 text-center">
            <h1 className="text-xl font-bold text-white mb-3">
              Une erreur est survenue
            </h1>
            <p className="text-slate-400 text-sm mb-4">
              L'application n'a pas pu démarrer correctement. Essayez de recharger la page.
            </p>
            <pre className="text-left text-xs text-red-400 bg-slate-950 border border-slate-800 rounded-lg p-3 overflow-auto mb-4">
              {this.state.error.message}
            </pre>
            <button
              onClick={() => window.location.reload()}
              className="bg-amber-500 text-slate-950 font-bold px-4 py-2 rounded-lg"
            >
              Recharger la page
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
