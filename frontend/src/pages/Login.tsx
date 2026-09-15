import { FormEvent, useState } from "react";
import { auth, User } from "../api/client";

interface Props {
  onLogin: (user: User) => void;
}

export default function Login({ onLogin }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const res = await auth.login(username, password);
      onLogin({ username: res.username, role: res.role });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex items-center justify-center min-h-screen bg-gray-950 px-4">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <h1 className="text-3xl font-bold text-sky-400 tracking-tight">RipRate</h1>
          <p className="text-gray-500 text-sm mt-1">Media acquisition &amp; library manager</p>
        </div>
        <div className="card">
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="label">Username</label>
              <input
                className="input"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoFocus
                autoComplete="username"
                required
              />
            </div>
            <div>
              <label className="label">Password</label>
              <div className="relative">
                <input
                  className="input pr-11"
                  type={showPassword ? "text" : "password"}
                  value={password}
                  autoComplete="current-password"
                  onChange={(e) => setPassword(e.target.value)}
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((s) => !s)}
                  className="absolute inset-y-0 right-0 px-3 flex items-center text-gray-500 hover:text-gray-300"
                  title={showPassword ? "Hide password" : "Show password"}
                  aria-label={showPassword ? "Hide password" : "Show password"}
                >
                  {showPassword ? "🙈" : "👁"}
                </button>
              </div>
            </div>
            {error && (
              <div className="bg-red-900/40 border border-red-700 rounded-lg px-3 py-2 text-red-300 text-sm">
                {error}
              </div>
            )}
            <button className="btn-primary w-full" type="submit" disabled={loading}>
              {loading ? "Signing in…" : "Sign in"}
            </button>
          </form>
        </div>
        <p className="text-center text-gray-600 text-xs mt-4">
          Access is restricted. Contact your administrator for an account.
        </p>
      </div>
    </div>
  );
}
