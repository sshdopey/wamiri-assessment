import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import {
  FileText,
  Search,
  Filter,
  ArrowUpDown,
  Eye,
  Download,
  RefreshCw,
  Inbox,
  Loader2,
  Sparkles,
  AlertCircle,
  CheckCircle2,
  Clock,
  Image,
  Copy,
  History,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { documentApi, queueApi } from "@/lib/api";
import type {
  TrackedDocument,
  DocumentTrackingStatus,
  ReviewItem,
  ReviewStatus,
} from "@/lib/types";
import { getDocumentDisplayName, getDocumentSubtitle } from "@/lib/types";
import { cn } from "@/lib/utils";

/* ── Review status badge config ───────────────────────────────────────── */

const reviewStatusConfig: Record<
  ReviewStatus,
  {
    label: string;
    variant: "default" | "secondary" | "destructive" | "outline";
  }
> = {
  pending: { label: "Pending Review", variant: "secondary" },
  in_review: { label: "In Review", variant: "default" },
  approved: { label: "Approved", variant: "outline" },
  corrected: { label: "Corrected", variant: "outline" },
  rejected: { label: "Rejected", variant: "destructive" },
};

/* ── Upload lifecycle status badge config ─────────────────────────────── */

const uploadStatusConfig: Record<
  DocumentTrackingStatus,
  {
    label: string;
    variant: "default" | "secondary" | "destructive" | "outline";
    icon: React.ReactNode;
    className?: string;
  }
> = {
  queued: {
    label: "Queued",
    variant: "secondary",
    icon: <Clock className="h-3 w-3" />,
  },
  processing: {
    label: "Processing",
    variant: "default",
    icon: <Loader2 className="h-3 w-3 animate-spin" />,
    className:
      "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300",
  },
  completed: {
    label: "Completed",
    variant: "outline",
    icon: <CheckCircle2 className="h-3 w-3 text-green-600" />,
    className:
      "border-green-200 text-green-700 dark:border-green-800 dark:text-green-400",
  },
  failed: {
    label: "Failed",
    variant: "destructive",
    icon: <AlertCircle className="h-3 w-3" />,
  },
  duplicate: {
    label: "Duplicate",
    variant: "secondary",
    icon: <Copy className="h-3 w-3" />,
    className:
      "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300",
  },
};

/* ── Helpers ───────────────────────────────────────────────────────────── */

function formatDate(dateStr: string) {
  const d = new Date(dateStr);
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatRelative(dateStr: string) {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "Just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function isImageMime(mime: string) {
  return mime.startsWith("image/");
}

/* ═══════════════════════════════════════════════════════════════════════
   DocumentsPage — dual-view: "Documents" (review queue) + "Upload History"
   ═══════════════════════════════════════════════════════════════════════ */

export function DocumentsPage() {
  const navigate = useNavigate();

  /* ── Review Queue state ───────────────────────────────────────────── */
  const [reviewItems, setReviewItems] = useState<ReviewItem[]>([]);
  const [reviewTotal, setReviewTotal] = useState(0);
  const [reviewLoading, setReviewLoading] = useState(true);
  const [reviewSearch, setReviewSearch] = useState("");
  const [reviewFilter, setReviewFilter] = useState<string>("all");
  const [reviewSortBy, setReviewSortBy] = useState<"priority" | "sla" | "date">("date");

  /* ── Upload History state ─────────────────────────────────────────── */
  const [uploads, setUploads] = useState<TrackedDocument[]>([]);
  const [uploadsTotal, setUploadsTotal] = useState(0);
  const [uploadsLoading, setUploadsLoading] = useState(true);
  const [uploadsSearch, setUploadsSearch] = useState("");
  const [uploadsFilter, setUploadsFilter] = useState<string>("all");

  const [activeTab, setActiveTab] = useState("documents");

  /* ── Fetch review queue ───────────────────────────────────────────── */

  const fetchReviewItems = useCallback(async () => {
    setReviewLoading(true);
    try {
      const res = await queueApi.getQueue({
        status: reviewFilter === "all" ? undefined : reviewFilter,
        sort_by: reviewSortBy,
        limit: 200,
        offset: 0,
      });
      setReviewItems(res.items);
      setReviewTotal(res.total);
    } catch {
      /* empty state */
    } finally {
      setReviewLoading(false);
    }
  }, [reviewFilter, reviewSortBy]);

  /* ── Fetch upload history ─────────────────────────────────────────── */

  const fetchUploads = useCallback(async () => {
    setUploadsLoading(true);
    try {
      const res = await documentApi.list({
        status: uploadsFilter === "all" ? undefined : uploadsFilter,
        limit: 200,
        offset: 0,
      });
      setUploads(res.items);
      setUploadsTotal(res.total);
    } catch {
      /* empty state */
    } finally {
      setUploadsLoading(false);
    }
  }, [uploadsFilter]);

  /* ── Always load both on mount so badge counts are available ─────── */

  useEffect(() => {
    fetchReviewItems();
  }, [fetchReviewItems]);

  useEffect(() => {
    fetchUploads();
  }, [fetchUploads]);

  /* ── Auto-refresh uploads while active jobs exist ─────────────────── */
  useEffect(() => {
    if (activeTab !== "history") return;
    const hasActive = uploads.some(
      (d) => d.status === "processing" || d.status === "queued"
    );
    if (!hasActive) return;
    const interval = setInterval(fetchUploads, 5000);
    return () => clearInterval(interval);
  }, [activeTab, uploads, fetchUploads]);

  /* ── Filter helpers ───────────────────────────────────────────────── */

  const filteredReview = reviewSearch
    ? reviewItems.filter((item) => {
        const q = reviewSearch.toLowerCase();
        const dn = getDocumentDisplayName(item).toLowerCase();
        const sub = getDocumentSubtitle(item)?.toLowerCase() ?? "";
        return (
          dn.includes(q) ||
          item.filename.toLowerCase().includes(q) ||
          sub.includes(q)
        );
      })
    : reviewItems;

  const filteredUploads = uploadsSearch
    ? uploads.filter((doc) => {
        const q = uploadsSearch.toLowerCase();
        return (
          doc.original_filename.toLowerCase().includes(q) ||
          doc.filename.toLowerCase().includes(q) ||
          doc.id.toLowerCase().includes(q)
        );
      })
    : uploads;

  /* ── Confidence helper ────────────────────────────────────────────── */
  const getConfidence = (item: ReviewItem) => {
    if (!item.fields?.length) return null;
    const avg =
      item.fields.reduce((s, f) => s + f.confidence, 0) / item.fields.length;
    return Math.round(avg * 100);
  };

  /* ══════════════════════════════════════════════════════════════════════
     RENDER
     ══════════════════════════════════════════════════════════════════════ */

  return (
    <div className="p-6 lg:p-8 space-y-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">Documents</h2>
          <p className="text-muted-foreground mt-1">
            Review extracted invoices and track upload history.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={
            activeTab === "documents" ? fetchReviewItems : fetchUploads
          }
          disabled={
            activeTab === "documents" ? reviewLoading : uploadsLoading
          }
        >
          <RefreshCw
            className={cn(
              "h-4 w-4 mr-1.5",
              (activeTab === "documents" ? reviewLoading : uploadsLoading) &&
                "animate-spin"
            )}
          />
          Refresh
        </Button>
      </div>

      {/* Tabs */}
      <Tabs
        value={activeTab}
        onValueChange={setActiveTab}
        className="space-y-5"
      >
        <TabsList className="grid w-full max-w-md grid-cols-2">
          <TabsTrigger value="documents" className="gap-1.5">
            <FileText className="h-3.5 w-3.5" />
            Documents
            {reviewTotal > 0 && (
              <Badge
                variant="secondary"
                className="ml-1 h-5 min-w-5 px-1.5 text-[10px]"
              >
                {reviewTotal}
              </Badge>
            )}
          </TabsTrigger>
          <TabsTrigger value="history" className="gap-1.5">
            <History className="h-3.5 w-3.5" />
            Upload History
            {uploadsTotal > 0 && (
              <Badge
                variant="secondary"
                className="ml-1 h-5 min-w-5 px-1.5 text-[10px]"
              >
                {uploadsTotal}
              </Badge>
            )}
          </TabsTrigger>
        </TabsList>

        {/* TAB 1 — Documents (Review Queue items) */}
        <TabsContent value="documents" className="space-y-4 mt-0">
          <Card>
            <CardContent className="py-3 px-4 flex flex-col sm:flex-row gap-3">
              <div className="relative flex-1">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                <Input
                  placeholder="Search by vendor, invoice #, or filename..."
                  value={reviewSearch}
                  onChange={(e) => setReviewSearch(e.target.value)}
                  className="pl-9"
                />
              </div>
              <Select value={reviewFilter} onValueChange={setReviewFilter}>
                <SelectTrigger className="w-44">
                  <Filter className="h-3.5 w-3.5 mr-1.5 text-muted-foreground" />
                  <SelectValue placeholder="Status" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Status</SelectItem>
                  <SelectItem value="pending">Pending Review</SelectItem>
                  <SelectItem value="in_review">In Review</SelectItem>
                  <SelectItem value="approved">Approved</SelectItem>
                  <SelectItem value="corrected">Corrected</SelectItem>
                  <SelectItem value="rejected">Rejected</SelectItem>
                </SelectContent>
              </Select>
              <Select value={reviewSortBy} onValueChange={(v) => setReviewSortBy(v as typeof reviewSortBy)}>
                <SelectTrigger className="w-40">
                  <ArrowUpDown className="h-3.5 w-3.5 mr-1.5 text-muted-foreground" />
                  <SelectValue placeholder="Sort" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="date">Upload Date</SelectItem>
                  <SelectItem value="sla">SLA Deadline</SelectItem>
                  <SelectItem value="priority">Priority</SelectItem>
                </SelectContent>
              </Select>
            </CardContent>
          </Card>

          {reviewLoading ? (
            <LoadingSkeleton />
          ) : filteredReview.length === 0 ? (
            <EmptyState
              search={reviewSearch}
              message="No documents in the review queue yet."
              onUpload={() => navigate("/upload")}
            />
          ) : (
            <Card>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-10" />
                    <TableHead>Document</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead className="hidden md:table-cell">Confidence</TableHead>
                    <TableHead className="hidden md:table-cell">Priority</TableHead>
                    <TableHead className="hidden lg:table-cell">Created</TableHead>
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredReview.map((item) => {
                    const conf = getConfidence(item);
                    const sc = reviewStatusConfig[item.status];
                    const displayName = getDocumentDisplayName(item);
                    const subtitle = getDocumentSubtitle(item);
                    return (
                      <TableRow
                        key={item.id}
                        className="cursor-pointer hover:bg-muted/50"
                        onClick={() =>
                          navigate(`/review/${item.id}`, {
                            state: { from: "/documents" },
                          })
                        }
                      >
                        <TableCell>
                          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-muted">
                            <FileText className="h-4 w-4 text-muted-foreground" />
                          </div>
                        </TableCell>
                        <TableCell>
                          <p className="font-medium text-sm truncate max-w-[260px]">
                            {displayName}
                          </p>
                          {subtitle && (
                            <p className="text-xs text-muted-foreground mt-0.5">
                              {subtitle}
                            </p>
                          )}
                        </TableCell>
                        <TableCell>
                          <Badge variant={sc.variant}>{sc.label}</Badge>
                        </TableCell>
                        <TableCell className="hidden md:table-cell">
                          {conf !== null ? (
                            <div className="flex items-center gap-2">
                              <div className="h-1.5 w-16 bg-muted rounded-full overflow-hidden">
                                <div
                                  className={cn(
                                    "h-full rounded-full",
                                    conf >= 80
                                      ? "bg-green-500"
                                      : conf >= 60
                                        ? "bg-amber-500"
                                        : "bg-red-500"
                                  )}
                                  style={{ width: `${conf}%` }}
                                />
                              </div>
                              <span className="text-xs text-muted-foreground">
                                {conf}%
                              </span>
                            </div>
                          ) : (
                            <span className="text-xs text-muted-foreground">
                              ---
                            </span>
                          )}
                        </TableCell>
                        <TableCell className="hidden md:table-cell">
                          <span className="text-sm tabular-nums">
                            {item.priority}
                          </span>
                        </TableCell>
                        <TableCell className="hidden lg:table-cell text-xs text-muted-foreground">
                          {formatDate(item.created_at)}
                        </TableCell>
                        <TableCell className="text-right">
                          <div
                            className="flex justify-end gap-1"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-8 w-8"
                              onClick={() =>
                                navigate(`/review/${item.id}`, {
                                  state: { from: "/documents" },
                                })
                              }
                              title="Review"
                            >
                              <Eye className="h-4 w-4" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-8 w-8"
                              onClick={() =>
                                window.open(
                                  documentApi.getDownloadUrl(
                                    item.document_id,
                                    "json"
                                  ),
                                  "_blank"
                                )
                              }
                              title="Download JSON"
                            >
                              <Download className="h-4 w-4" />
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
              <div className="border-t px-4 py-3 text-xs text-muted-foreground">
                Showing {filteredReview.length} of {reviewTotal} documents
              </div>
            </Card>
          )}
        </TabsContent>

        {/* TAB 2 — Upload History (all uploads from documents table) */}
        <TabsContent value="history" className="space-y-4 mt-0">
          <UploadStatusSummary uploads={uploads} />

          <Card>
            <CardContent className="py-3 px-4 flex flex-col sm:flex-row gap-3">
              <div className="relative flex-1">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                <Input
                  placeholder="Search by filename or document ID..."
                  value={uploadsSearch}
                  onChange={(e) => setUploadsSearch(e.target.value)}
                  className="pl-9"
                />
              </div>
              <Select value={uploadsFilter} onValueChange={setUploadsFilter}>
                <SelectTrigger className="w-44">
                  <Filter className="h-3.5 w-3.5 mr-1.5 text-muted-foreground" />
                  <SelectValue placeholder="Status" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Status</SelectItem>
                  <SelectItem value="queued">Queued</SelectItem>
                  <SelectItem value="processing">Processing</SelectItem>
                  <SelectItem value="completed">Completed</SelectItem>
                  <SelectItem value="failed">Failed</SelectItem>
                  <SelectItem value="duplicate">Duplicate</SelectItem>
                </SelectContent>
              </Select>
            </CardContent>
          </Card>

          {uploadsLoading ? (
            <LoadingSkeleton />
          ) : filteredUploads.length === 0 ? (
            <EmptyState
              search={uploadsSearch}
              message="No uploads recorded yet."
              onUpload={() => navigate("/upload")}
            />
          ) : (
            <Card>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-10" />
                    <TableHead>File</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead className="hidden md:table-cell">Type</TableHead>
                    <TableHead className="hidden lg:table-cell">Uploaded</TableHead>
                    <TableHead className="hidden lg:table-cell">Updated</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredUploads.map((doc) => {
                    const sc = uploadStatusConfig[doc.status];
                    const isActive =
                      doc.status === "processing" || doc.status === "queued";
                    const isFailed = doc.status === "failed";
                    const isDuplicate = doc.status === "duplicate";

                    return (
                      <TableRow
                        key={doc.id}
                        className={cn(isActive && "opacity-80")}
                      >
                        <TableCell>
                          <div
                            className={cn(
                              "flex h-8 w-8 items-center justify-center rounded-lg",
                              isActive
                                ? "bg-blue-500/10"
                                : isFailed
                                  ? "bg-red-500/10"
                                  : isDuplicate
                                    ? "bg-amber-500/10"
                                    : "bg-muted"
                            )}
                          >
                            {isActive ? (
                              <Loader2 className="h-4 w-4 text-blue-500 animate-spin" />
                            ) : isDuplicate ? (
                              <Copy className="h-4 w-4 text-amber-500" />
                            ) : isImageMime(doc.mime_type) ? (
                              <Image className="h-4 w-4 text-muted-foreground" />
                            ) : (
                              <FileText className="h-4 w-4 text-muted-foreground" />
                            )}
                          </div>
                        </TableCell>
                        <TableCell>
                          <p className="font-medium text-sm truncate max-w-[280px]">
                            {doc.original_filename}
                          </p>
                          {isActive ? (
                            <p className="text-xs text-muted-foreground flex items-center gap-1 mt-0.5">
                              <Sparkles className="h-3 w-3" />
                              {doc.status === "queued"
                                ? "Waiting in queue..."
                                : "AI is extracting data..."}
                            </p>
                          ) : isFailed ? (
                            <p
                              className="text-xs text-red-500 mt-0.5 truncate max-w-[280px]"
                              title={doc.error_message ?? ""}
                            >
                              {doc.error_message || "Processing failed"}
                            </p>
                          ) : isDuplicate ? (
                            <p className="text-xs text-amber-600 mt-0.5">
                              This file was already processed before
                            </p>
                          ) : null}
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant={sc.variant}
                            className={cn("gap-1", sc.className)}
                          >
                            {sc.icon}
                            {sc.label}
                          </Badge>
                        </TableCell>
                        <TableCell className="hidden md:table-cell">
                          <span className="text-xs text-muted-foreground">
                            {doc.mime_type.split("/")[1]?.toUpperCase() ??
                              doc.mime_type}
                          </span>
                        </TableCell>
                        <TableCell className="hidden lg:table-cell text-xs text-muted-foreground">
                          {formatDate(doc.created_at)}
                        </TableCell>
                        <TableCell className="hidden lg:table-cell text-xs text-muted-foreground">
                          {formatRelative(doc.updated_at)}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
              <div className="border-t px-4 py-3 text-xs text-muted-foreground">
                Showing {filteredUploads.length} of {uploadsTotal} uploads
              </div>
            </Card>
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}

/* ══════════════════════════════════════════════════════════════════════
   Sub-components
   ══════════════════════════════════════════════════════════════════════ */

function UploadStatusSummary({ uploads }: { uploads: TrackedDocument[] }) {
  if (uploads.length === 0) return null;

  const counts: Record<
    string,
    { count: number; icon: React.ReactNode; color: string }
  > = {
    completed: {
      count: uploads.filter((u) => u.status === "completed").length,
      icon: <CheckCircle2 className="h-3.5 w-3.5" />,
      color: "text-green-600",
    },
    processing: {
      count: uploads.filter((u) => u.status === "processing").length,
      icon: <Loader2 className="h-3.5 w-3.5 animate-spin" />,
      color: "text-blue-600",
    },
    queued: {
      count: uploads.filter((u) => u.status === "queued").length,
      icon: <Clock className="h-3.5 w-3.5" />,
      color: "text-muted-foreground",
    },
    failed: {
      count: uploads.filter((u) => u.status === "failed").length,
      icon: <AlertCircle className="h-3.5 w-3.5" />,
      color: "text-red-600",
    },
    duplicate: {
      count: uploads.filter((u) => u.status === "duplicate").length,
      icon: <Copy className="h-3.5 w-3.5" />,
      color: "text-amber-600",
    },
  };

  const nonZero = Object.entries(counts).filter(([, v]) => v.count > 0);
  if (nonZero.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-2">
      {nonZero.map(([key, { count, icon, color }]) => (
        <div
          key={key}
          className="flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-medium"
        >
          <span className={color}>{icon}</span>
          <span className="capitalize">{key}</span>
          <span className="text-muted-foreground">{count}</span>
        </div>
      ))}
      <div className="flex items-center gap-1.5 rounded-full bg-muted px-3 py-1.5 text-xs font-medium text-muted-foreground">
        Total {uploads.length}
      </div>
    </div>
  );
}

function LoadingSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 5 }).map((_, i) => (
        <Skeleton key={i} className="h-14 w-full rounded-lg" />
      ))}
    </div>
  );
}

function EmptyState({
  search,
  message,
  onUpload,
}: {
  search: string;
  message: string;
  onUpload: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-20 text-center">
      <div className="rounded-full bg-muted p-4 mb-4">
        <Inbox className="h-8 w-8 text-muted-foreground" />
      </div>
      <h3 className="text-lg font-semibold">
        {search ? "No results" : "Nothing here yet"}
      </h3>
      <p className="text-sm text-muted-foreground mt-1 max-w-md">
        {search ? "Try adjusting your search or filters." : message}
      </p>
      {!search && (
        <Button className="mt-4" onClick={onUpload}>
          Upload Invoices
        </Button>
      )}
    </div>
  );
}
