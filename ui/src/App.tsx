import { Routes, Route, Navigate } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AppLayout } from "@/components/layout/AppLayout";
import { DashboardPage } from "@/pages/DashboardPage";
import { UploadPage } from "@/pages/UploadPage";
import { QueuePage } from "@/pages/QueuePage";
import { ReviewPage } from "@/pages/ReviewPage";
import { DocumentsPage } from "@/pages/DocumentsPage";

export default function App() {
  return (
    <TooltipProvider delayDuration={200}>
      <Routes>
        <Route element={<AppLayout />}>
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="dashboard" element={<DashboardPage />} />
          <Route path="upload" element={<UploadPage />} />
          <Route path="documents" element={<DocumentsPage />} />
          <Route path="queue" element={<QueuePage />} />
          <Route path="review/:id" element={<ReviewPage />} />
        </Route>
      </Routes>
      <Toaster richColors position="top-right" />
    </TooltipProvider>
  );
}
