import { BriefcaseBusiness, Database, ListChecks } from "lucide-react";
import { useEffect, useRef } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useUi } from "../app/useUi";
import { EvidenceDrawer } from "./EvidenceDrawer";
import { useActiveJobCount } from "../hooks/useActiveJobCount";
import { JobDrawer } from "./JobDrawer";
import { QueryRunDrawer } from "./QueryRunDrawer";

export function AppShell() {
  const { setJobsOpen } = useUi();
  const activeJobCount = useActiveJobCount();
  const location = useLocation();
  const mainRef = useRef<HTMLElement>(null);

  useEffect(() => {
    mainRef.current?.focus({ preventScroll: true });
  }, [location.pathname]);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <header className="app-header">
        <div className="brand" aria-label="财析可信财报助手">
          <span className="brand__mark" aria-hidden="true">财</span>
          <div><strong>财析</strong><small>可信财报助手</small></div>
        </div>
        <nav className="primary-nav" aria-label="主导航">
          <NavLink to="/workspace" className={({ isActive }) => isActive ? "nav-link is-active" : "nav-link"}>
            <BriefcaseBusiness size={19} aria-hidden="true" />
            <span>问数工作台</span>
          </NavLink>
          <NavLink to="/data" className={({ isActive }) => isActive ? "nav-link is-active" : "nav-link"}>
            <Database size={19} aria-hidden="true" />
            <span>数据管理</span>
          </NavLink>
        </nav>
        <button className="task-button" type="button" onClick={() => setJobsOpen(true)} aria-label={`打开任务中心，${activeJobCount} 个进行中任务`}>
          <ListChecks size={19} aria-hidden="true" />
          <span>任务</span>
          {activeJobCount > 0 ? <b aria-hidden="true">{activeJobCount}</b> : null}
        </button>
      </header>
      <main id="main-content" ref={mainRef} tabIndex={-1} className="app-main">
        <Outlet />
      </main>
      <JobDrawer />
      <EvidenceDrawer />
      <QueryRunDrawer />
    </div>
  );
}
