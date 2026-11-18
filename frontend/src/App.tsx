import { useEffect, type ReactNode } from 'react';
import { BrowserRouter, HashRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { Home } from './pages/Home';
import { Examples } from './pages/Examples';
import { AdminConsole, AdminLogin } from './pages/Admin';
import { VisitorNotice } from './components/shared/VisitorNotice';
import { About, ManagedSettingsNotice, ProductHelp } from './pages/About';
import { History } from './pages/History';
import { OutlineEditor } from './pages/OutlineEditor';
import { DetailEditor } from './pages/DetailEditor';
import { TemplateSetupPage } from './pages/TemplateSetupPage';
import { SlidePreview } from './pages/SlidePreview';
import { useProjectStore } from './store/useProjectStore';
import { useToast, AccessCodeGuard, DesktopTitleBar, UpdateChecker } from './components/shared';
import { getDesktopTopInset } from './components/shared/UpdateChecker';
import { isDesktop } from '@/utils';

function CreativeSessionNotice() {
  const location = useLocation();
  return location.pathname.startsWith('/admin') ? null : <VisitorNotice />;
}

function CreativeAccessBoundary({children}: {children: ReactNode}) {
  const location = useLocation();
  // Admin authentication must remain usable even when a visitor session expired.
  return location.pathname.startsWith('/admin') ? children : <AccessCodeGuard>{children}</AccessCodeGuard>;
}

function App() {
  const { currentProject, syncProject, error, setError } = useProjectStore();
  const { show, ToastContainer } = useToast();

  // 恢复项目状态
  useEffect(() => {
    if (window.location.pathname.startsWith('/admin')) return;
    const savedProjectId = localStorage.getItem('currentProjectId');
    if (savedProjectId && !currentProject) {
      syncProject();
    }
  }, [currentProject, syncProject]);

  // 显示全局错误
  useEffect(() => {
    if (error) {
      show({ message: error, type: 'error' });
      setError(null);
    }
  }, [error, setError, show]);


  return (
    <>
      {isDesktop && <UpdateChecker />}
      <div style={isDesktop ? { paddingTop: `${getDesktopTopInset()}px` } : undefined}>
          {(() => {
            const Router = isDesktop ? HashRouter : BrowserRouter;
            return (
              <Router>
                <DesktopTitleBar />
                <CreativeAccessBoundary>
                <CreativeSessionNotice />
                <Routes>
                  <Route path="/admin/login" element={<AdminLogin />} />
                  <Route path="/admin" element={<AdminConsole />} />
                  <Route path="/" element={<Home />} />
                  <Route path="/examples" element={<Examples />} />
                  <Route path="/landing" element={<Navigate to="/" replace />} />
                  <Route path="/about" element={<About />} />
                  <Route path="/help" element={<ProductHelp />} />
                  <Route path="/history" element={<History />} />
                  <Route path="/settings" element={<ManagedSettingsNotice />} />
                  <Route path="/project/:projectId/outline" element={<OutlineEditor />} />
                  <Route path="/project/:projectId/detail" element={<DetailEditor />} />
                  <Route path="/project/:projectId/template-setup" element={<TemplateSetupPage />} />
                  <Route path="/project/:projectId/preview" element={<SlidePreview />} />
                  <Route path="*" element={<Navigate to="/" replace />} />
                </Routes>
                <ToastContainer />
                </CreativeAccessBoundary>
              </Router>
            );
          })()}
      </div>
    </>
  );
}

export default App;
