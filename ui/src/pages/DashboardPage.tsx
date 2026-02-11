import { useEffect, useState } from "react";
import {
  BarChart3,
  Clock,
  CheckCircle2,
  AlertTriangle,
  Layers,
  TrendingUp,
  ArrowRight,
  Upload,
  Sparkles,
  FileSearch,
  ShieldCheck,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { queueApi } from "@/lib/api";
import type { QueueStats } from "@/lib/types";
import { useUploadTracking } from "@/lib/upload-tracking";
import { useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";

export function DashboardPage() {
  const [stats, setStats] = useState<QueueStats | null>(null);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();
  const { uploads } = useUploadTracking();
  const processingCount = uploads.filter(
    (u) => u.status === "processing"
  ).length;

  useEffect(() => {
    let mounted = true;
    queueApi
      .getStats()
      .then((data) => mounted && setStats(data))
      .catch(() => mounted && setStats(null))
      .finally(() => mounted && setLoading(false));
    return () => {
      mounted = false;
    };
  }, []);

  const metrics = stats
    ? [
        {
          title: "Queue Depth",
          value: stats.queue_depth,
          icon: Layers,
          color: "text-blue-600",
          bg: "bg-blue-500/10",
          desc: "Invoices awaiting review",
        },
        {
          title: "Reviewed Today",
          value: stats.items_reviewed_today,
          icon: CheckCircle2,
          color: "text-green-600",
          bg: "bg-green-500/10",
          desc: "Completed reviews",
        },
        {
          title: "Avg Review Time",
          value:
            stats.avg_review_time_seconds > 0
              ? `${Math.round(stats.avg_review_time_seconds)}s`
              : "—",
          icon: Clock,
          color: "text-amber-600",
          bg: "bg-amber-500/10",
          desc: "Per document",
        },
        {
          title: "SLA Compliance",
          value: `${Math.round(stats.sla_compliance_percent)}%`,
          icon:
            stats.sla_compliance_percent >= 90 ? TrendingUp : AlertTriangle,
          color:
            stats.sla_compliance_percent >= 90
              ? "text-green-600"
              : "text-red-600",
          bg:
            stats.sla_compliance_percent >= 90
              ? "bg-green-500/10"
              : "bg-red-500/10",
          desc: "Within deadline",
        },
      ]
    : [];

  return (
    <div className="p-6 lg:p-8 space-y-8 max-w-7xl mx-auto">
      {/* ── Hero / App purpose ────────────────────────────────────── */}
      <div className="rounded-xl border bg-gradient-to-br from-primary/5 via-background to-blue-500/5 p-6 lg:p-8">
        <h2 className="text-2xl font-bold tracking-tight mb-2">
          Invoice Processing Hub
        </h2>
        <p className="text-muted-foreground max-w-2xl leading-relaxed">
          Upload PDF invoices and let AI extract vendor details, amounts, dates,
          and line items automatically. Then review the results, make corrections
          if needed, and approve — all in one place.
        </p>

        {/* Workflow steps */}
        <div className="grid grid-cols-1 sm:grid-cols-4 gap-4 mt-6">
          {[
            {
              icon: Upload,
              title: "1. Upload",
              desc: "Drop PDF invoices",
              color: "text-blue-600",
              bg: "bg-blue-500/10",
            },
            {
              icon: Sparkles,
              title: "2. AI Extracts",
              desc: "Gemini reads the PDF",
              color: "text-purple-600",
              bg: "bg-purple-500/10",
            },
            {
              icon: FileSearch,
              title: "3. Review",
              desc: "Verify extracted data",
              color: "text-amber-600",
              bg: "bg-amber-500/10",
            },
            {
              icon: ShieldCheck,
              title: "4. Approve",
              desc: "Finalize & export",
              color: "text-green-600",
              bg: "bg-green-500/10",
            },
          ].map((step, i) => (
            <div key={i} className="flex items-start gap-3">
              <div className={cn("rounded-lg p-2 shrink-0", step.bg)}>
                <step.icon className={cn("h-4 w-4", step.color)} />
              </div>
              <div>
                <p className="text-sm font-semibold">{step.title}</p>
                <p className="text-xs text-muted-foreground">{step.desc}</p>
              </div>
            </div>
          ))}
        </div>

        <div className="flex gap-3 mt-6">
          <Button onClick={() => navigate("/upload")}>
            <Upload className="h-4 w-4 mr-1.5" />
            Upload Invoices
          </Button>
          {stats && stats.queue_depth > 0 && (
            <Button variant="outline" onClick={() => navigate("/queue")}>
              Review Queue ({stats.queue_depth}){" "}
              <ArrowRight className="ml-1.5 h-4 w-4" />
            </Button>
          )}
        </div>
      </div>

      {/* ── Processing banner ─────────────────────────────────────── */}
      {processingCount > 0 && (
        <Card className="border-blue-200 bg-blue-50/60 dark:bg-blue-950/20">
          <CardContent className="py-3 px-5 flex items-center gap-3">
            <div className="rounded-full bg-blue-500/10 p-2">
              <Sparkles className="h-4 w-4 text-blue-600 animate-pulse" />
            </div>
            <div className="flex-1">
              <p className="text-sm font-medium text-blue-900 dark:text-blue-200">
                AI is processing {processingCount} document
                {processingCount !== 1 ? "s" : ""}…
              </p>
              <p className="text-xs text-blue-700/70 dark:text-blue-300/70">
                Results will appear in the review queue automatically.
              </p>
            </div>
            <Button
              size="sm"
              variant="outline"
              onClick={() => navigate("/documents")}
            >
              View Progress
            </Button>
          </CardContent>
        </Card>
      )}

      {/* ── KPI cards ─────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {loading
          ? Array.from({ length: 4 }).map((_, i) => (
              <Card key={i}>
                <CardContent className="pt-6">
                  <Skeleton className="h-4 w-24 mb-3" />
                  <Skeleton className="h-8 w-16 mb-2" />
                  <Skeleton className="h-3 w-32" />
                </CardContent>
              </Card>
            ))
          : metrics.map((m) => (
              <Card
                key={m.title}
                className="hover:shadow-md transition-shadow"
              >
                <CardContent className="pt-6">
                  <div className="flex items-center justify-between mb-3">
                    <p className="text-sm font-medium text-muted-foreground">
                      {m.title}
                    </p>
                    <div className={cn("rounded-lg p-2", m.bg)}>
                      <m.icon className={cn("h-4 w-4", m.color)} />
                    </div>
                  </div>
                  <p className="text-3xl font-bold tracking-tight">{m.value}</p>
                  <p className="text-xs text-muted-foreground mt-1">
                    {m.desc}
                  </p>
                </CardContent>
              </Card>
            ))}
      </div>

      {/* ── Quick actions ─────────────────────────────────────────── */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card
          className="cursor-pointer hover:shadow-md transition-all hover:border-primary/30 group"
          onClick={() => navigate("/upload")}
        >
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center gap-2">
              <div className="rounded-lg bg-blue-500/10 p-2 group-hover:bg-blue-500/20 transition-colors">
                <BarChart3 className="h-4 w-4 text-blue-600" />
              </div>
              Upload New Invoices
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-muted-foreground">
              Drag & drop PDF invoices for AI-powered data extraction.
            </p>
          </CardContent>
        </Card>

        <Card
          className="cursor-pointer hover:shadow-md transition-all hover:border-primary/30 group"
          onClick={() => navigate("/queue")}
        >
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center gap-2">
              <div className="rounded-lg bg-amber-500/10 p-2 group-hover:bg-amber-500/20 transition-colors">
                <Clock className="h-4 w-4 text-amber-600" />
              </div>
              Review Pending Items
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-muted-foreground">
              {stats?.queue_depth
                ? `${stats.queue_depth} invoice${stats.queue_depth !== 1 ? "s" : ""} waiting for your review.`
                : "No items in the queue right now."}
            </p>
          </CardContent>
        </Card>

        <Card
          className="cursor-pointer hover:shadow-md transition-all hover:border-primary/30 group"
          onClick={() => navigate("/documents")}
        >
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center gap-2">
              <div className="rounded-lg bg-green-500/10 p-2 group-hover:bg-green-500/20 transition-colors">
                <CheckCircle2 className="h-4 w-4 text-green-600" />
              </div>
              View All Documents
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-muted-foreground">
              Browse all processed invoices and download extracted data.
            </p>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
