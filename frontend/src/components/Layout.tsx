import { createContext, ReactNode, useContext, useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { nav as navApi, NavCounts, User } from "../api/client";

// Live sidebar counts (incl. `downloading`), shared so any page can react to
// downloads finishing without opening its own stream.
const NavCountsContext = createContext<NavCounts>({ requests: 0, downloads: 0, downloading: 0 });
export const useNavCounts = () => useContext(NavCountsContext);
import {
  IActivity, ICalendar, IDownload, IFolder, IGearNav, IInbox, IPlay, ISearch, IUsers,
} from "./icons";

interface Props {
  user: User;
  onLogout: () => void;
  children: ReactNode;
}

interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  adminOnly?: boolean;
  badge?: keyof NavCounts;
}

const NAV: NavItem[] = [
  { to: "/search",    label: "Search",    icon: <ISearch size={17} /> },
  { to: "/watch",     label: "Watch",     icon: <IPlay size={17} /> },
  { to: "/requests",  label: "Requests",  icon: <IInbox size={17} />, badge: "requests" },
  { to: "/downloads", label: "Downloads", icon: <IDownload size={17} />, adminOnly: true, badge: "downloads" },
  { to: "/schedule",  label: "Schedule",  icon: <ICalendar size={17} />, adminOnly: true },
  { to: "/library",   label: "Library",   icon: <IFolder size={17} /> },
  { to: "/users",     label: "Users",     icon: <IUsers size={17} />, adminOnly: true },
  { to: "/activity",  label: "Activity",  icon: <IActivity size={17} />, adminOnly: true },
  { to: "/settings",  label: "Settings",  icon: <IGearNav size={17} /> },
];

export default function Layout({ user, onLogout, children }: Props) {
  const isAdmin = user.role === "admin";
  const items = NAV.filter((n) => !n.adminOnly || isAdmin);

  const [counts, setCounts] = useState<NavCounts>({ requests: 0, downloads: 0, downloading: 0 });
  const loc = useLocation();

  // Refresh badge counts on first paint + each route change (no idle connection).
  useEffect(() => { navApi.counts().then(setCounts).catch(() => {}); }, [loc.pathname]);

  // Only stream while something is downloading; otherwise the connection closes.
  const streaming = (counts.downloading ?? 0) > 0;
  useEffect(() => {
    if (!streaming) return;
    const es = new EventSource("/api/nav/stream", { withCredentials: true });
    es.onmessage = (e) => { try { setCounts(JSON.parse(e.data)); } catch { /* ignore */ } };
    es.onerror = () => { es.close(); navApi.counts().then(setCounts).catch(() => {}); };
    return () => es.close();
  }, [streaming]);

  return (
    <div className="min-h-screen flex">
      {/* Sidebar */}
      <aside className="w-56 bg-gray-900 border-r border-gray-800 flex flex-col fixed h-full">
        <div className="h-16 flex items-center px-5 border-b border-gray-800">
          <span className="font-bold text-sky-400 text-xl tracking-tight">RipRate</span>
        </div>
        <nav className="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
          {items.map((item) => {
            const badge = (item.badge ? counts[item.badge] : 0) ?? 0;
            return (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  `flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                    isActive
                      ? "bg-sky-600/15 text-sky-300"
                      : "text-gray-400 hover:text-gray-100 hover:bg-gray-800"
                  }`
                }
              >
                <span className="w-5 flex items-center justify-center">{item.icon}</span>
                <span className="flex-1">{item.label}</span>
                {badge > 0 && (
                  <span className={`text-xs font-semibold px-1.5 py-0.5 rounded-full min-w-[1.25rem] text-center ${
                    item.badge === "downloads"
                      ? "bg-sky-600 text-white"
                      : "bg-amber-500 text-gray-900"
                  }`}>
                    {badge}
                  </span>
                )}
              </NavLink>
            );
          })}
        </nav>
        <div className="border-t border-gray-800 p-3">
          <div className="w-full flex items-center gap-3 px-3 py-2">
            <span className="w-8 h-8 rounded-full bg-sky-700 flex items-center justify-center text-sm font-semibold uppercase">
              {user.username.slice(0, 1)}
            </span>
            <span className="min-w-0">
              <span className="block text-sm font-medium text-gray-100 truncate">{user.username}</span>
              <span className={`block text-xs ${isAdmin ? "text-amber-400" : "text-gray-500"}`}>
                {isAdmin ? "Administrator" : "User"}
              </span>
            </span>
          </div>
          <button onClick={onLogout} className="btn-ghost btn-sm w-full mt-1 justify-start text-gray-400">
            Sign out
          </button>
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 ml-56 min-w-0">
        <div className="max-w-6xl mx-auto px-6 py-8">
          <NavCountsContext.Provider value={counts}>{children}</NavCountsContext.Provider>
        </div>
      </main>
    </div>
  );
}
