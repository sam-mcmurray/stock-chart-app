// App.jsx
import { useState, useEffect } from 'react';
import { Routes, Route, Navigate, useLocation } from 'react-router-dom';
import useIsMobile from './hooks/useIsMobile.jsx';
import Sidebar from './components/Sidebar.jsx';
import Topbar from './components/Topbar.jsx';
import Dashboard from './pages/Dashboard.jsx';
import Calendar from './pages/Calendar.jsx';
import NewsFeed from './pages/NewsFeed.jsx';
import Scanner from './pages/Scanner.jsx';
import Settings from './pages/Settings.jsx';
import Profile from './pages/Profile.jsx';
import ChartWorkspace from './pages/ChartWorkspace.jsx';
import SchwabReauthNotice from './components/SchwabReauthNotice.jsx';
import NewsLlm from './pages/NewsLlm.jsx';
import Research from './pages/Research.jsx';

export default function App(){
  const isMobile = useIsMobile(900);
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('sbx') === '1');
  const [drawerOpen, setDrawerOpen] = useState(false);
  const loc = useLocation();

  useEffect(()=>{ if (!isMobile) localStorage.setItem('sbx', collapsed ? '1':'0'); }, [collapsed, isMobile]);
  useEffect(()=>{ setDrawerOpen(false); }, [loc.pathname]);
  useEffect(()=>{ if (!isMobile) setDrawerOpen(false); }, [isMobile]);

  return (
    <>
      <Topbar onMenu={() => setDrawerOpen(true)} />

      <div className={`app ${!isMobile && collapsed ? 'collapsed' : ''}`}>
        <Sidebar
          collapsed={collapsed}
          onToggle={()=>setCollapsed(c => !c)}
          isMobile={isMobile}
          drawerOpen={drawerOpen}
          onClose={()=>setDrawerOpen(false)}
        />

        {isMobile && (
          <div className={`backdrop ${drawerOpen ? 'show' : ''}`} onClick={()=>setDrawerOpen(false)} />
        )}

        <main className="content">
          <SchwabReauthNotice />
          <Routes>
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/calendar"  element={<Calendar />} />
            <Route path="/news"      element={<NewsFeed />} />
            <Route path="/news-llm"  element={<NewsLlm />} />
            <Route path="/scanner"   element={<Scanner />} />
            <Route path="/research"  element={<Research />} />
            <Route path="/charts/:symbol" element={<ChartWorkspace />} />
            <Route path="/settings"  element={<Settings />} />
            <Route path="/profile"   element={<Profile />} />
            <Route path="*"          element={<div className="panel">Not found</div>} />
          </Routes>
        </main>
      </div>
    </>
  );
}
