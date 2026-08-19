import React, { useState } from 'react';
import { supabase } from '../lib/supabase';

interface AuthProps {
  initialMode?: 'login' | 'signup';
  onSuccess: () => void;
  onCancel: () => void;
}

export function AuthModal({ initialMode = 'login', onSuccess, onCancel }: AuthProps) {
  const [isLogin, setIsLogin] = useState(initialMode === 'login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
            if (!supabase) {
        throw new Error(
          "La connexion n'est pas disponible : Supabase n'est pas configuré sur ce déploiement (variables VITE_SUPABASE_URL / VITE_SUPABASE_PUBLISHABLE_KEY manquantes)."
        );
      }
      if (isLogin) {
        const { error } = await supabase.auth.signInWithPassword({ email, password });
        if (error) throw error;
      } else {
        const { error } = await supabase.auth.signUp({ email, password });
        if (error) throw error;
      }
      onSuccess();
    } catch (err: any) {
      setError(err.message || 'Une erreur est survenue');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4 z-50">
      <div className="bg-slate-900 border border-slate-800 rounded-2xl p-8 max-w-md w-full">
        <h2 className="text-2xl font-bold text-white mb-2">{isLogin ? 'Connexion' : 'Créer un compte'}</h2>
        {error && <div className="bg-red-500/10 border border-red-500/20 text-red-400 p-3 rounded-lg text-sm mb-4">{error}</div>}
        <form onSubmit={handleSubmit} className="space-y-4">
          <input type="email" required value={email} onChange={e => setEmail(e.target.value)} placeholder="Email" className="w-full bg-slate-950 border border-slate-800 rounded-lg p-3 text-white"/>
          <input type="password" required value={password} onChange={e => setPassword(e.target.value)} placeholder="Mot de passe" className="w-full bg-slate-950 border border-slate-800 rounded-lg p-3 text-white"/>
          <button type="submit" disabled={loading} className="w-full bg-amber-500 text-slate-950 font-bold py-3 rounded-lg">
            {loading ? 'Chargement...' : isLogin ? 'Se connecter' : "S'inscrire"}
          </button>
        </form>
        <button onClick={onCancel} className="mt-4 text-xs text-slate-500 w-full">Fermer</button>
      </div>
    </div>
  );
}
