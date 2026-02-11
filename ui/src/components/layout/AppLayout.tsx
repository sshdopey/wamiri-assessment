import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  LayoutDashboard,
  Upload,
  FileText,
  ClipboardList,
  Menu,
  X,
  FileSearch,
  Loader2,
} from "lucide-react";
import { useState, useEffect } from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { useUploadTracking } from "@/lib/upload-tracking";

const navigation = [
  { name: "Dashboard", href: "/dashboard", icon: LayoutDashboard },
  { name: "Upload", href: "/upload", icon: Upload },
  { name: "Documents", href: "/documents", icon: FileText },
  { name: "Review Queue", href: "/queue", icon: ClipboardList },
];

export function AppLayout() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const location = useLocation();
  const { uploads, pollProcessing } = useUploadTracking();
  const processingCount = uploads.filter(
    (u) => u.status === "processing"
  ).length;

  // Global polling for processing uploads
  useEffect(() => {
    if (processingCount === 0) return;
    const interval = setInterval(pollProcessing, 5000);
    return () => clearInterval(interval);
  }, [processingCount, pollProcessing]);

  return (
    <div className="flex h-screen bg-background overflow-hidden">
      {/* ── Mobile overlay ──────────────────────────────────────────── */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/40 lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* ── Sidebar ─────────────────────────────────────────────────── */}
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-50 flex w-64 flex-col bg-card border-r border-border transition-transform duration-200 lg:static lg:translate-x-0",
          sidebarOpen ? "translate-x-0" : "-translate-x-full"
        )}
      >
        {/* Brand */}
        <div className="flex h-16 items-center gap-2.5 border-b border-border px-6">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary">
            <FileSearch className="h-4 w-4 text-primary-foreground" />
          </div>
          <div className="flex flex-col">
            <span className="text-sm font-semibold tracking-tight">
              Wamiri Invoices
            </span>
            <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
              AI Document Engine
            </span>
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="ml-auto lg:hidden"
            onClick={() => setSidebarOpen(false)}
          >
            <X className="h-5 w-5" />
          </Button>
        </div>

        {/* Nav links */}
        <nav className="flex-1 space-y-1 px-3 py-4">
          {navigation.map((item) => (
            <NavLink
              key={item.href}
              to={item.href}
              onClick={() => setSidebarOpen(false)}
              className={({ isActive }) =>
                cn(
                  "group flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                )
              }
            >
              <item.icon className="h-4 w-4 shrink-0" />
              {item.name}
              {item.href === "/documents" && processingCount > 0 && (
                <span className="ml-auto flex items-center gap-1 rounded-full bg-blue-500/15 px-2 py-0.5 text-[10px] font-medium text-blue-600">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  {processingCount}
                </span>
              )}
            </NavLink>
          ))}
        </nav>

        {/* Footer */}
        <div className="border-t border-border px-4 py-3">
          <p className="text-[11px] text-muted-foreground text-center">
            Wamiri Assessment v1.0
          </p>
        </div>
      </aside>

      {/* ── Main content ────────────────────────────────────────────── */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Top bar */}
        <header className="flex h-16 items-center gap-4 border-b border-border bg-card px-4 lg:px-6">
          <Button
            variant="ghost"
            size="icon"
            className="lg:hidden"
            onClick={() => setSidebarOpen(true)}
          >
            <Menu className="h-5 w-5" />
          </Button>
          <div className="flex-1">
            <h1 className="text-lg font-semibold tracking-tight">
              {navigation.find((n) =>
                location.pathname.startsWith(n.href)
              )?.name ??
                (location.pathname.startsWith("/review")
                  ? "Document Review"
                  : "Wamiri Invoices")}
            </h1>
          </div>
        </header>

        {/* Page content */}
        <main className="flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
