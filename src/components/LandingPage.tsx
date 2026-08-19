import React from 'react';

interface LandingPageProps {
  onGetStarted: () => void;
  onLogin: () => void;
}

export function LandingPage({ onGetStarted, onLogin }: LandingPageProps) {
  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 font-sans">
      <nav className="flex items-center justify-between px-8 py-6 max-w-7xl mx-auto border-b border-slate-800/80">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-amber-500 flex items-center justify-center font-bold text-slate-950">OFX</div>
          <span className="font-bold text-xl text-white">OFX Bridge</span>
        </div>
        <div className="flex items-center gap-4">
          <button onClick={onLogin} className="text-sm font-medium text-slate-300 hover:text-white px-4 py-2">Se connecter</button>
          <button onClick={onGetStarted} className="text-sm font-semibold bg-amber-500 text-slate-950 px-5 py-2.5 rounded-lg">Créer un compte</button>
        </div>
      </nav>
      <section className="max-w-5xl mx-auto text-center px-6 pt-20 pb-16">
        <h1 className="text-4xl sm:text-6xl font-extrabold text-white mb-6">
          Vos relevés PDF, transformés en <span className="text-amber-400">données financières</span>
        </h1>
        <p className="text-lg text-slate-400 max-w-2xl mx-auto mb-10">
          Importez vos fichiers bancaires PDF, vérifiez vos transactions et obtenez un export OFX ou Excel propre.
        </p>
        <button onClick={onGetStarted} className="px-8 py-4 bg-amber-500 text-slate-950 font-bold rounded-xl">Commencer gratuitement</button>
      </section>
    </div>
  );
}
