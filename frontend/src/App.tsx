import { createContext, useContext, useEffect, useState } from "react";
import { Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { auth, setup, User } from "./api/client";
import { ToastProvider } from "./components/ui";
import Layout from "./components/Layout";
import Login from "./pages/Login";
import Setup from "./pages/Setup";
import Search from "./pages/Search";
import Requests from "./pages/Requests";
import Library from "./pages/Library";
import Watch from "./pages/Watch";
import Downloads from "./pages/admin/Downloads";
import Users from "./pages/admin/Users";
import Activity from "./pages/admin/Activity";
import Schedule from "./pages/admin/Schedule";
import SettingsPage from "./pages/admin/Settings";

const AuthCtx = createContext<User | null>(null);
export const useAuth = () => useContext(AuthCtx);

function App() {
  const [needsSetup, setNeedsSetup] = useState<boolean | undefined>(undefined); // undefined = loading
  const [user, setUser] = useState<User | null | undefined>(undefined); // undefined = loading
  const navigate = useNavigate();

  useEffect(() => {
    let cancelled = false;
    const loadUser = async () => {
      try {
        const data = await auth.me();
        if (cancelled) return;
        if (data.authenticated && data.username && data.role) {
          setUser({ username: data.username, role: data.role });
        } else setUser(null);
      } catch { if (!cancelled) setUser(null); }
    };
    // First-run gate: a fresh install shows the setup wizard instead of login.
    setup.status()
      .then((s) => {
        if (cancelled) return;
        if (s.needs_setup) {
          // Fresh install → wipe leftover browser-side search history (recents
          // live in localStorage, not the DB, so a DB reset doesn't clear them)
          // so onboarding really starts from scratch.
          try {
            Object.keys(localStorage)
              .filter((k) => k.startsWith("riprate.recent."))
              .forEach((k) => localStorage.removeItem(k));
          } catch { /* ignore */ }
          setNeedsSetup(true);
          return;
        }
        setNeedsSetup(false);
        return loadUser();
      })
      .catch(() => {          // status unreachable → assume configured, try auth
        if (cancelled) return;
        setNeedsSetup(false);
        return loadUser();
      });
    return () => { cancelled = true; };
  }, []);

  const handleLogin = (u: User) => {
    setUser(u);
    navigate("/search");
  };

  const handleLogout = async () => {
    try { await auth.logout(); } catch { /* ignore */ }
    setUser(null);
    navigate("/login");
  };

  if (needsSetup === undefined || (!needsSetup && user === undefined)) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-gray-500 text-lg animate-pulse">Loading…</div>
      </div>
    );
  }

  // Fresh install → the setup wizard fully replaces login until it's done. It
  // owns its own clean URL (/setup) and every other path redirects to it, so
  // the address bar never shows /login while onboarding. Once setup_completed
  // the app falls through to login and /setup is no longer reachable.
  if (needsSetup) {
    return (
      <ToastProvider>
        <Routes>
          <Route path="/setup" element={<Setup onComplete={() => window.location.reload()} />} />
          <Route path="*" element={<Navigate to="/setup" replace />} />
        </Routes>
      </ToastProvider>
    );
  }

  if (!user) {
    return (
      <ToastProvider>
        <Routes>
          <Route path="/login" element={<Login onLogin={handleLogin} />} />
          <Route path="*" element={<Navigate to="/login" replace />} />
        </Routes>
      </ToastProvider>
    );
  }

  const isAdmin = user.role === "admin";
  const adminOnly = (el: JSX.Element) => (isAdmin ? el : <Navigate to="/search" replace />);

  return (
    <ToastProvider>
      <AuthCtx.Provider value={user}>
        <Layout user={user} onLogout={handleLogout}>
          <Routes>
            <Route path="/" element={<Navigate to="/search" replace />} />
            <Route path="/login" element={<Navigate to="/search" replace />} />
            <Route path="/search" element={<Search />} />
            <Route path="/requests" element={<Requests />} />
            <Route path="/library" element={<Library />} />
            <Route path="/watch" element={<Watch />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/downloads" element={adminOnly(<Downloads />)} />
            <Route path="/schedule" element={adminOnly(<Schedule />)} />
            <Route path="/users" element={adminOnly(<Users />)} />
            <Route path="/activity" element={adminOnly(<Activity />)} />
            <Route path="*" element={<Navigate to="/search" replace />} />
          </Routes>
        </Layout>
      </AuthCtx.Provider>
    </ToastProvider>
  );
}

export default App;
