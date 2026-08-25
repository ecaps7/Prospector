import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import App from "./App.tsx";
import { AskPage } from "./pages/AskPage.tsx";
import { JobsPage } from "./pages/JobsPage.tsx";
import { MonitorPage } from "./pages/MonitorPage.tsx";
import { ReportPage } from "./pages/ReportPage.tsx";
import "./styles/app.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route element={<App />}>
          <Route path="/" element={<AskPage />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/jobs/:jobId" element={<MonitorPage />} />
          <Route path="/jobs/:jobId/report" element={<ReportPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
