import { NavLink } from "react-router-dom";

type Props = {
  theme: "light" | "dark";
  onToggleTheme: () => void;
  serverOk: boolean | null;
};

export function TopBar({ theme, onToggleTheme, serverOk }: Props) {
  const pillClass = serverOk === true ? "ok" : serverOk === false ? "down" : "";
  return (
    <header className="topbar">
      <NavLink to="/" className="brand" end>
        <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
          <circle cx="9" cy="9" r="6.2" fill="none" stroke="currentColor" strokeWidth="1.7" />
          <path d="M13.6 13.6 L17.4 17.4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
          <circle className="mark-dot" cx="9" cy="9" r="2.3" />
        </svg>
        Prospector
      </NavLink>
      <nav className="topnav" aria-label="主导航">
        <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
          发起新研究
        </NavLink>
        <NavLink to="/jobs" className={({ isActive }) => (isActive ? "active" : "")}>
          任务列表
        </NavLink>
      </nav>
      <div className="top-right">
        <span className={`server-pill ${pillClass}`}>
          <i />
          {serverOk === false ? "服务不可用" : "127.0.0.1:7620"}
        </span>
        <button
          className="theme-btn"
          type="button"
          onClick={onToggleTheme}
          title="切换浅色 / 深色外观"
          aria-label="切换外观"
        >
          {theme === "dark" ? (
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <circle cx="8" cy="8" r="3.2" stroke="currentColor" strokeWidth="1.3" />
              <path
                d="M8 1.6v1.4M8 13v1.4M1.6 8h1.4M13 8h1.4M3.2 3.2l1 1M11.8 11.8l1 1M3.2 12.8l1-1M11.8 4.2l1-1"
                stroke="currentColor"
                strokeWidth="1.3"
                strokeLinecap="round"
              />
            </svg>
          ) : (
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path
                d="M13.4 9.5A5.5 5.5 0 0 1 6.5 2.6a.45.45 0 0 0-.62-.56 6.5 6.5 0 1 0 8.08 8.08.45.45 0 0 0-.56-.62Z"
                stroke="currentColor"
                strokeWidth="1.3"
              />
            </svg>
          )}
        </button>
      </div>
    </header>
  );
}
