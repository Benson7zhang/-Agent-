import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "../components/AppShell";
import { LoadingState } from "../components/Feedback";

const DataManagementPage = lazy(() =>
  import("../pages/DataManagementPage").then((module) => ({ default: module.DataManagementPage })),
);
const WorkspacePage = lazy(() =>
  import("../pages/WorkspacePage").then((module) => ({ default: module.WorkspacePage })),
);

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Navigate to="/workspace" replace />} />
        <Route path="workspace" element={<Suspense fallback={<LoadingState label="正在打开问数工作台" />}><WorkspacePage /></Suspense>} />
        <Route path="data" element={<Suspense fallback={<LoadingState label="正在打开数据管理" />}><DataManagementPage /></Suspense>} />
        <Route path="*" element={<Navigate to="/workspace" replace />} />
      </Route>
    </Routes>
  );
}
