import { useEffect, useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import {
  ClipboardList,
  Clock,
  AlertTriangle,
  ArrowRight,
  Inbox,
  Filter,
  ArrowUpDown,
  ChevronLeft,
  ChevronRight,
  Timer,
  User,
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
import type { ReviewItem } from "@/lib/types";
import { getDocumentDisplayName, getDocumentSubtitle } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useReviewStore } from "@/lib/store";

export function QueuePage() {
  const navigate = useNavigate();

  const {
    queueItems: items,
    total,
    page,
    pageSize,
    stats,
    loading,
    filters,
    fetchQueue,
    setFilter,
    setPage,
    startPolling,
    stopPolling,
  } = useReviewStore();

  // SLA countdown timer — ticks every second
  const [, setTick] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | undefined>(undefined);

  useEffect(() => {
    timerRef.current = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(timerRef.current);
  }, []);

  // Start always-on polling on mount, stop on unmount
  useEffect(() => {
    startPolling();
    return () => stopPolling();
  }, [startPolling, stopPolling]);

  // Re-fetch when filters or page change
  useEffect(() => {
    fetchQueue();
  }, [filters.status, filters.sort_by, filters.assigned_to, page, fetchQueue]);

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

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

  /** Live SLA countdown: returns formatted time remaining or "OVERDUE" */
  const getSlaCountdown = (item: ReviewItem) => {
    if (!item.sla_deadline) return null;
    const deadline = new Date(item.sla_deadline).getTime();
    const now = Date.now();
    const diff = deadline - now;

    if (diff <= 0) return "OVERDUE";

    const hours = Math.floor(diff / 3600000);
    const mins = Math.floor((diff % 3600000) / 60000);
    const secs = Math.floor((diff % 60000) / 1000);

    if (hours > 0) return `${hours}h ${mins}m`;
    if (mins > 0) return `${mins}m ${secs}s`;
    return `${secs}s`;
  };

  return (
    <div className="p-6 lg:p-8 space-y-6 max-w-7xl mx-auto" role="main" aria-label="Review Queue">
      <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">Review Queue</h2>
          <p className="text-muted-foreground mt-1">
            Invoices extracted by AI, awaiting your review and approval.
          </p>
        </div>
        <div className="flex gap-2">
          <Badge variant="outline" className="text-xs text-muted-foreground">
            <span className="relative flex h-2 w-2 mr-1.5">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-green-500" />
            </span>
            Live
          </Badge>
        </div>
      </div>

      {stats && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3" role="region" aria-label="Queue statistics">
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
                <s.icon className={cn("h-5 w-5 shrink-0", s.color)} aria-hidden="true" />
                <div>
                  <p className="text-xs text-muted-foreground">{s.label}</p>
                  <p className="text-lg font-bold" aria-label={`${s.label}: ${s.value}`}>{s.value}</p>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <div className="flex flex-wrap gap-2" role="search" aria-label="Queue filters">
        <Select
          value={filters.status ?? "all"}
          onValueChange={(v) => setFilter("status", v === "all" ? undefined : v)}
        >
          <SelectTrigger className="w-40" aria-label="Filter by status">
            <Filter className="h-3.5 w-3.5 mr-1.5 text-muted-foreground" aria-hidden="true" />
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Statuses</SelectItem>
            <SelectItem value="pending">Pending Review</SelectItem>
            <SelectItem value="in_review">In Review</SelectItem>
            <SelectItem value="approved">Approved</SelectItem>
            <SelectItem value="corrected">Corrected</SelectItem>
            <SelectItem value="rejected">Rejected</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={filters.assigned_to ?? "all"}
          onValueChange={(v) => setFilter("assigned_to", v === "all" ? undefined : v)}
        >
          <SelectTrigger className="w-40" aria-label="Filter by reviewer">
            <User className="h-3.5 w-3.5 mr-1.5 text-muted-foreground" aria-hidden="true" />
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Reviewers</SelectItem>
            <SelectItem value="reviewer-1">Reviewer 1</SelectItem>
            <SelectItem value="reviewer-2">Reviewer 2</SelectItem>
            <SelectItem value="reviewer-3">Reviewer 3</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={filters.sort_by}
          onValueChange={(v) => setFilter("sort_by", v)}
        >
          <SelectTrigger className="w-36" aria-label="Sort by">
            <ArrowUpDown className="h-3.5 w-3.5 mr-1.5 text-muted-foreground" aria-hidden="true" />
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="priority">Priority</SelectItem>
            <SelectItem value="sla">SLA Deadline</SelectItem>
            <SelectItem value="date">Upload Date</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {loading ? (
        <div className="space-y-3" aria-busy="true" aria-label="Loading queue items">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-24 w-full rounded-xl" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-center" role="status">
          <div className="rounded-full bg-muted p-4 mb-4">
            <Inbox className="h-8 w-8 text-muted-foreground" aria-hidden="true" />
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
        <div className="space-y-3" role="list" aria-label="Queue items">
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
                role="listitem"
                tabIndex={0}
                aria-label={`${displayName}, priority ${item.priority}, status ${item.status}`}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    navigate(`/review/${item.id}`, { state: { from: '/queue' } });
                  }
                }}
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
                    aria-hidden="true"
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
                          <span aria-hidden="true">•</span>
                        </>
                      )}
                      <span>Priority {item.priority}</span>
                      <span aria-hidden="true">•</span>
                      <span>{item.fields?.length ?? 0} fields</span>
                      <span aria-hidden="true">•</span>
                      <span>{getTimeAgo(item.created_at)}</span>
                      {item.assigned_to && (
                        <>
                          <span aria-hidden="true">•</span>
                          <span className="flex items-center gap-0.5">
                            <User className="h-3 w-3" aria-hidden="true" />
                            {item.assigned_to}
                          </span>
                        </>
                      )}
                      {/* SLA Countdown */}
                      {item.sla_deadline && !item.completed_at && (() => {
                        const countdown = getSlaCountdown(item);
                        if (!countdown) return null;
                        const isOver = countdown === "OVERDUE";
                        return (
                          <>
                            <span aria-hidden="true">•</span>
                            <span
                              className={cn(
                                "flex items-center gap-0.5 font-medium",
                                isOver ? "text-red-600" : "text-amber-600"
                              )}
                              aria-label={isOver ? "SLA overdue" : `SLA remaining: ${countdown}`}
                            >
                              <Timer className="h-3 w-3" aria-hidden="true" />
                              {isOver ? "Overdue" : countdown}
                            </span>
                          </>
                        );
                      })()}
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
                        aria-label={`Confidence: ${conf}%`}
                      >
                        {conf}%
                      </span>
                      <span className="text-[10px] text-muted-foreground">
                        confidence
                      </span>
                    </div>
                  )}

                  {/* Reviewer badge */}
                  {item.assigned_to && (
                    <div className="hidden sm:flex flex-col items-center gap-1">
                      <div className="flex items-center gap-1 text-xs font-medium text-muted-foreground bg-muted px-2 py-1 rounded-full">
                        <User className="h-3 w-3" aria-hidden="true" />
                        <span>{item.assigned_to.replace("reviewer-", "R")}</span>
                      </div>
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
                    {item.status === "pending"
                      ? "pending review"
                      : item.status.replace("_", " ")}
                  </Badge>

                  {/* Arrow */}
                  <ArrowRight className="h-4 w-4 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity shrink-0" aria-hidden="true" />
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {!loading && items.length > 0 && totalPages > 1 && (
        <nav className="flex items-center justify-between pt-2" aria-label="Queue pagination">
          <p className="text-sm text-muted-foreground" aria-live="polite">
            Showing {page * pageSize + 1}–{Math.min((page + 1) * pageSize, total)} of {total}
          </p>
          <div className="flex items-center gap-1">
            <Button
              variant="outline"
              size="sm"
              disabled={page === 0}
              onClick={() => setPage(Math.max(0, page - 1))}
              aria-label="Previous page"
            >
              <ChevronLeft className="h-4 w-4" aria-hidden="true" />
              Prev
            </Button>
            {Array.from({ length: Math.min(totalPages, 5) }, (_, i) => {
              // Show pages around current page
              let pageNum: number;
              if (totalPages <= 5) {
                pageNum = i;
              } else if (page < 3) {
                pageNum = i;
              } else if (page > totalPages - 4) {
                pageNum = totalPages - 5 + i;
              } else {
                pageNum = page - 2 + i;
              }
              return (
                <Button
                  key={pageNum}
                  variant={pageNum === page ? "default" : "outline"}
                  size="sm"
                  className="w-9"
                  onClick={() => setPage(pageNum)}
                  aria-label={`Page ${pageNum + 1}`}
                  aria-current={pageNum === page ? "page" : undefined}
                >
                  {pageNum + 1}
                </Button>
              );
            })}
            <Button
              variant="outline"
              size="sm"
              disabled={page >= totalPages - 1}
              onClick={() => setPage(Math.min(totalPages - 1, page + 1))}
              aria-label="Next page"
            >
              Next
              <ChevronRight className="h-4 w-4" aria-hidden="true" />
            </Button>
          </div>
        </nav>
      )}
    </div>
  );
}
