import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import {
  ClipboardList,
  Clock,
  AlertTriangle,
  ArrowRight,
  RefreshCw,
  Inbox,
  Filter,
  ArrowUpDown,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { queueApi } from "@/lib/api";
import type { ReviewItem, QueueStats } from "@/lib/types";
import { getDocumentDisplayName, getDocumentSubtitle } from "@/lib/types";
import { cn } from "@/lib/utils";

export function QueuePage() {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [stats, setStats] = useState<QueueStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>("pending");
  const [sortBy, setSortBy] = useState<"priority" | "sla" | "date">(
    "priority"
  );
  const navigate = useNavigate();

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const [queueRes, statsRes] = await Promise.all([
        queueApi.getQueue({
          status: statusFilter === "all" ? undefined : statusFilter,
          sort_by: sortBy,
          limit: 50,
          offset: 0,
        }),
        queueApi.getStats(),
      ]);
      setItems(queueRes.items);
      setStats(statsRes);
    } catch {
      /* empty state */
    } finally {
      setLoading(false);
    }
  }, [statusFilter, sortBy]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const getConfidence = (item: ReviewItem) => {
    if (!item.fields?.length) return null;
    const avg =
      item.fields.reduce((s, f) => s + f.confidence, 0) / item.fields.length;
    return Math.round(avg * 100);
  };

  const getTimeAgo = (dateStr: string) => {
    const diff = Date.now() - new Date(dateStr).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  };

  const isOverdue = (item: ReviewItem) => {
    if (!item.sla_deadline) return false;
    return new Date(item.sla_deadline) < new Date();
  };

  return (
    <div className="p-6 lg:p-8 space-y-6 max-w-7xl mx-auto">
      {/* ── Header ────────────────────────────────────────────────── */}
      <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">Review Queue</h2>
          <p className="text-muted-foreground mt-1">
            Invoices extracted by AI, awaiting your review and approval.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={fetchData}
          disabled={loading}
        >
          <RefreshCw
            className={cn("h-4 w-4 mr-1.5", loading && "animate-spin")}
          />
          Refresh
        </Button>
      </div>

      {/* ── Stats strip ───────────────────────────────────────────── */}
      {stats && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          {[
            {
              label: "In Queue",
              value: stats.queue_depth,
              icon: ClipboardList,
              color: "text-blue-600",
            },
            {
              label: "Reviewed Today",
              value: stats.items_reviewed_today,
              icon: ArrowRight,
              color: "text-green-600",
            },
            {
              label: "Avg Time",
              value:
                stats.avg_review_time_seconds > 0
                  ? `${Math.round(stats.avg_review_time_seconds)}s`
                  : "—",
              icon: Clock,
              color: "text-amber-600",
            },
            {
              label: "SLA",
              value: `${Math.round(stats.sla_compliance_percent)}%`,
              icon: AlertTriangle,
              color:
                stats.sla_compliance_percent >= 90
                  ? "text-green-600"
                  : "text-red-600",
            },
          ].map((s) => (
            <Card key={s.label}>
              <CardContent className="py-3 px-4 flex items-center gap-3">
                <s.icon className={cn("h-5 w-5 shrink-0", s.color)} />
                <div>
                  <p className="text-xs text-muted-foreground">{s.label}</p>
                  <p className="text-lg font-bold">{s.value}</p>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* ── Filters ───────────────────────────────────────────────── */}
      <div className="flex gap-2">
        <Select value={statusFilter} onValueChange={setStatusFilter}>
          <SelectTrigger className="w-40">
            <Filter className="h-3.5 w-3.5 mr-1.5 text-muted-foreground" />
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All</SelectItem>
            <SelectItem value="pending">Pending</SelectItem>
            <SelectItem value="in_review">In Review</SelectItem>
            <SelectItem value="approved">Approved</SelectItem>
            <SelectItem value="corrected">Corrected</SelectItem>
            <SelectItem value="rejected">Rejected</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={sortBy}
          onValueChange={(v) => setSortBy(v as typeof sortBy)}
        >
          <SelectTrigger className="w-36">
            <ArrowUpDown className="h-3.5 w-3.5 mr-1.5 text-muted-foreground" />
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="priority">Priority</SelectItem>
            <SelectItem value="sla">SLA Deadline</SelectItem>
            <SelectItem value="date">Upload Date</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {/* ── Queue items ───────────────────────────────────────────── */}
      {loading ? (
        <div className="space-y-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-24 w-full rounded-xl" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-center">
          <div className="rounded-full bg-muted p-4 mb-4">
            <Inbox className="h-8 w-8 text-muted-foreground" />
          </div>
          <h3 className="text-lg font-semibold">Queue is empty</h3>
          <p className="text-sm text-muted-foreground mt-1 max-w-sm">
            No invoices match your current filters. Upload new documents or
            change the filter.
          </p>
          <Button className="mt-4" onClick={() => navigate("/upload")}>
            Upload Invoices
          </Button>
        </div>
      ) : (
        <div className="space-y-3">
          {items.map((item) => {
            const conf = getConfidence(item);
            const overdue = isOverdue(item);
            const displayName = getDocumentDisplayName(item);
            const subtitle = getDocumentSubtitle(item);
            return (
              <Card
                key={item.id}
                className={cn(
                  "cursor-pointer transition-all hover:shadow-md hover:border-primary/30 group",
                  overdue && "border-red-500/30"
                )}
                onClick={() => navigate(`/review/${item.id}`, { state: { from: '/queue' } })}
              >
                <CardContent className="py-4 px-5 flex items-center gap-4">
                  {/* Priority indicator */}
                  <div
                    className={cn(
                      "w-1.5 h-14 rounded-full shrink-0",
                      item.priority >= 8
                        ? "bg-red-500"
                        : item.priority >= 5
                          ? "bg-amber-500"
                          : "bg-blue-500"
                    )}
                  />

                  {/* Info */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <h4 className="text-sm font-semibold truncate">
                        {displayName}
                      </h4>
                      {overdue && (
                        <Badge
                          variant="destructive"
                          className="text-[10px] px-1.5 py-0"
                        >
                          OVERDUE
                        </Badge>
                      )}
                    </div>
                    <div className="flex items-center gap-3 text-xs text-muted-foreground">
                      {subtitle && (
                        <>
                          <span className="font-medium text-foreground/70">
                            {subtitle}
                          </span>
                          <span>•</span>
                        </>
                      )}
                      <span>Priority {item.priority}</span>
                      <span>•</span>
                      <span>{item.fields?.length ?? 0} fields</span>
                      <span>•</span>
                      <span>{getTimeAgo(item.created_at)}</span>
                    </div>
                  </div>

                  {/* Confidence */}
                  {conf !== null && (
                    <div className="hidden sm:flex flex-col items-center gap-1">
                      <span
                        className={cn(
                          "text-xl font-bold tabular-nums",
                          conf >= 80
                            ? "text-green-600"
                            : conf >= 60
                              ? "text-amber-600"
                              : "text-red-600"
                        )}
                      >
                        {conf}%
                      </span>
                      <span className="text-[10px] text-muted-foreground">
                        confidence
                      </span>
                    </div>
                  )}

                  {/* Status badge */}
                  <Badge
                    variant={
                      item.status === "pending"
                        ? "secondary"
                        : item.status === "in_review"
                          ? "default"
                          : item.status === "rejected"
                            ? "destructive"
                            : "outline"
                    }
                    className="shrink-0"
                  >
                    {item.status.replace("_", " ")}
                  </Badge>

                  {/* Arrow */}
                  <ArrowRight className="h-4 w-4 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity shrink-0" />
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
