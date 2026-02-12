import { useEffect, useState } from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import {
  ArrowLeft,
  CheckCircle2,
  XCircle,
  Edit3,
  Loader2,
  Download,
  ExternalLink,
  Lock,
  Unlock,
  FileText,
  AlertTriangle,
  Keyboard,
  RotateCcw,
  History,
  User,
  Clock,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { documentApi, getCurrentReviewer, queueApi } from "@/lib/api";
import type { ReviewAction } from "@/lib/types";
import { FIELD_META, getDocumentDisplayName, getDocumentSubtitle } from "@/lib/types";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { PdfViewer } from "@/components/PdfViewer";
import { useReviewStore } from "@/lib/store";

export function ReviewPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const backTo = (location.state as { from?: string })?.from || "/queue";

// Store state (synced across tabs)
  const {
    currentItem: item,
    corrections,
    auditTrail,
    loading,
    fetchItem,
    fetchAuditTrail,
    setCorrection,
    submitReview,
    startPolling,
    stopPolling,
  } = useReviewStore();

  const [submitting, setSubmitting] = useState(false);
  const [startingReview, setStartingReview] = useState(false);
  const [rejectDialogOpen, setRejectDialogOpen] = useState(false);
  const [approveDialogOpen, setApproveDialogOpen] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [activeTab, setActiveTab] = useState("fields");
  const [auditLoading, setAuditLoading] = useState(false);

// Load item on mount
  useEffect(() => {
    if (id) fetchItem(id);
    startPolling();
    return () => stopPolling();
  }, [id, fetchItem, startPolling, stopPolling]);

// Keyboard shortcuts
  const isMac = navigator.platform.toUpperCase().includes("MAC");
  const isMyReview = item?.assigned_to === getCurrentReviewer();

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (!isMyReview || item?.status !== "in_review" || submitting || rejectDialogOpen || approveDialogOpen) return;

      const mod = isMac ? e.metaKey : e.ctrlKey;
      if (!mod || !e.shiftKey) return;

      switch (e.key.toUpperCase()) {
        case "A":
          e.preventDefault();
          setApproveDialogOpen(true);
          break;
        case "E":
          e.preventDefault();
          if (hasEdits) handleSubmit("correct");
          break;
        case "X":
          e.preventDefault();
          setRejectDialogOpen(true);
          break;
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [item?.status, submitting, rejectDialogOpen, approveDialogOpen, isMyReview]);

  const modKey = isMac ? "⌘" : "Ctrl";

// Audit trail
  useEffect(() => {
    if (activeTab === "history" && id) {
      setAuditLoading(true);
      fetchAuditTrail(id).finally(() => setAuditLoading(false));
    }
  }, [activeTab, id, fetchAuditTrail]);

// Submit
  const handleSubmit = async (action: ReviewAction, reason?: string) => {
    if (!item) return;
    setSubmitting(true);
    try {
      const changedCorrections: Record<string, string> = {};
      if (action === "correct") {
        item.fields?.forEach((f) => {
          const newVal = corrections[f.field_name];
          if (newVal !== undefined && newVal !== f.value) {
            changedCorrections[f.field_name] = newVal;
          }
        });
      }

      await submitReview(item.id, action, changedCorrections, reason);

      toast.success(
        action === "approve"
          ? "Document approved!"
          : action === "correct"
            ? "Corrections submitted!"
            : "Document rejected."
      );
      navigate(backTo);
    } catch {
      toast.error("Failed to submit review");
    } finally {
      setSubmitting(false);
    }
  };

// Field helpers
  const getFieldMeta = (fieldName: string) =>
    FIELD_META.find((m) => m.name === fieldName);

  const getConfidenceColor = (conf: number) => {
    if (conf >= 0.8) return "text-green-600";
    if (conf >= 0.6) return "text-amber-600";
    return "text-red-600";
  };

  const getConfidenceBg = (conf: number) => {
    if (conf >= 0.8) return "bg-green-500";
    if (conf >= 0.6) return "bg-amber-500";
    return "bg-red-500";
  };

  const getInputProps = (fieldName: string) => {
    const meta = getFieldMeta(fieldName);
    if (!meta) return { type: "text" as const };
    switch (meta.type) {
      case "date":
        return { type: "date" as const };
      case "number":
        return { type: "number" as const, step: "0.01", min: "0" };
      case "currency":
        return { type: "number" as const, step: "0.01", min: "0" };
      default:
        return { type: "text" as const };
    }
  };

  const isFieldModified = (fieldName: string, originalValue: string | null) => {
    const current = corrections[fieldName];
    return current !== undefined && current !== (originalValue ?? "");
  };

  const resetField = (fieldName: string, originalValue: string | null) => {
    setCorrection(fieldName, originalValue ?? "");
  };

// Derived data
  const headerFields =
    item?.fields?.filter((f) => !f.field_name.startsWith("line_item")) ?? [];

  const lineItemFields =
    item?.fields?.filter((f) => f.field_name.startsWith("line_item")) ?? [];

  const lineItems: Record<string, typeof lineItemFields> = {};
  lineItemFields.forEach((f) => {
    const match = f.field_name.match(/line_item_(\d+)/);
    if (match) {
      const key = match[1];
      if (!lineItems[key]) lineItems[key] = [];
      lineItems[key].push(f);
    }
  });

  /** Are there any edits to submit? */
  const hasEdits = item?.fields?.some((f) => {
    const newVal = corrections[f.field_name];
    return newVal !== undefined && newVal !== (f.value ?? "");
  }) ?? false;

  if (loading && !item) {
    return (
      <div className="p-6 lg:p-8 max-w-7xl mx-auto space-y-6" aria-busy="true" aria-label="Loading document review">
        <div className="flex gap-4 items-center">
          <Skeleton className="h-8 w-8" />
          <Skeleton className="h-6 w-64" />
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <Skeleton className="h-[600px] rounded-xl" />
          <Skeleton className="h-[600px] rounded-xl" />
        </div>
      </div>
    );
  }

  if (!item) return null;

  const displayName = getDocumentDisplayName(item);
  const subtitle = getDocumentSubtitle(item);

  const avgConfidence = item.fields?.length
    ? item.fields.reduce((s, f) => s + f.confidence, 0) / item.fields.length
    : 0;

  return (
    <div className="flex flex-col h-[calc(100vh-4rem)]">
      <div className="flex items-center gap-3 px-6 py-3 border-b bg-card shrink-0" role="banner" aria-label="Document review toolbar">
        <Button variant="ghost" size="icon" onClick={() => navigate(backTo)} aria-label="Back to queue">
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
        </Button>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <FileText className="h-4 w-4 text-muted-foreground shrink-0" />
            <h2 className="text-sm font-semibold truncate">{displayName}</h2>
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
            >
              {item.status === "pending"
                ? "pending review"
                : item.status.replace("_", " ")}
            </Badge>
            {item.assigned_to && (
              <Badge variant="outline" className="text-xs gap-1">
                <User className="h-3 w-3" aria-hidden="true" />
                {item.assigned_to}
              </Badge>
            )}
          </div>
          <div className="flex items-center gap-3 text-xs text-muted-foreground mt-0.5">
            {subtitle && (
              <>
                <span className="font-medium text-foreground/70">
                  {subtitle}
                </span>
                <span>•</span>
              </>
            )}
            <span>
              Confidence:{" "}
              <span
                className={cn(
                  "font-semibold",
                  getConfidenceColor(avgConfidence)
                )}
              >
                {Math.round(avgConfidence * 100)}%
              </span>
            </span>
            <span>•</span>
            <span>Priority {item.priority}</span>
            <span>•</span>
            <span>{item.fields?.length ?? 0} fields</span>
          </div>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <Button
            variant="ghost"
            size="sm"
            onClick={() =>
              window.open(
                documentApi.getDownloadUrl(item.document_id, "json"),
                "_blank"
              )
            }
            aria-label="Download extracted data as JSON"
          >
            <Download className="h-4 w-4 mr-1.5" aria-hidden="true" />
            JSON
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() =>
              window.open(
                documentApi.getPreviewUrl(item.document_id),
                "_blank"
              )
            }
            aria-label="Open original file in new tab"
          >
            <ExternalLink className="h-4 w-4 mr-1.5" aria-hidden="true" />
            Open File
          </Button>
        </div>
      </div>

      <div className="flex-1 flex flex-col lg:flex-row overflow-hidden">
        {/* Left: Document preview */}
        <div className="lg:w-1/2 h-1/2 lg:h-full border-b lg:border-b-0 lg:border-r bg-muted/30 relative overflow-hidden">
          {(() => {
            const previewUrl = documentApi.getPreviewUrl(item.document_id);
            const isImage = item.filename && /\.(png|jpe?g|webp|gif|tiff?|bmp)$/i.test(item.filename);

            if (isImage) {
              return (
                <div className="flex items-center justify-center w-full h-full p-4 overflow-auto">
                  <img
                    src={previewUrl}
                    alt="Invoice preview"
                    className="max-w-full max-h-full object-contain rounded-lg shadow-sm"
                  />
                </div>
              );
            }

            return <PdfViewer url={previewUrl} />;
          })()}
        </div>

        {/* Right: Extracted data */}
        <div className="lg:w-1/2 h-1/2 lg:h-full flex flex-col overflow-hidden">
          <Tabs
            value={activeTab}
            onValueChange={setActiveTab}
            className="flex flex-col h-full"
          >
            <div className="border-b px-4 shrink-0">
              <TabsList className="bg-transparent h-10 p-0 gap-4">
                <TabsTrigger
                  value="fields"
                  className="data-[state=active]:bg-transparent data-[state=active]:shadow-none data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none px-1 pb-2.5"
                >
                  Extracted Fields
                </TabsTrigger>
                {Object.keys(lineItems).length > 0 && (
                  <TabsTrigger
                    value="lines"
                    className="data-[state=active]:bg-transparent data-[state=active]:shadow-none data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none px-1 pb-2.5"
                  >
                    Line Items ({Object.keys(lineItems).length})
                  </TabsTrigger>
                )}
                <TabsTrigger
                  value="history"
                  className="data-[state=active]:bg-transparent data-[state=active]:shadow-none data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none px-1 pb-2.5"
                >
                  <History className="h-3.5 w-3.5 mr-1" />
                  History
                </TabsTrigger>
              </TabsList>
            </div>

            <TabsContent
              value="fields"
              className="flex-1 overflow-y-auto m-0 p-4 space-y-3"
            >
              {headerFields.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-12 text-center">
                  <AlertTriangle className="h-8 w-8 text-muted-foreground mb-3" />
                  <p className="text-sm text-muted-foreground">
                    No extracted fields available.
                  </p>
                </div>
              ) : (
                headerFields.map((field) => {
                  const meta = getFieldMeta(field.field_name);
                  const label =
                    meta?.label ??
                    field.field_name
                      .replace(/_/g, " ")
                      .replace(/\b\w/g, (c) => c.toUpperCase());

                  return (
                    <div
                      key={field.id}
                      className="group rounded-lg border p-3 hover:border-primary/30 transition-colors"
                    >
                      <div className="flex items-center justify-between mb-2">
                        <Label className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                          {label}
                        </Label>
                        <div className="flex items-center gap-2">
                          <div className="flex items-center gap-1.5">
                            <div className="h-1 w-8 bg-muted rounded-full overflow-hidden">
                              <div
                                className={cn(
                                  "h-full rounded-full transition-all",
                                  getConfidenceBg(field.confidence)
                                )}
                                style={{
                                  width: `${Math.round(field.confidence * 100)}%`,
                                }}
                              />
                            </div>
                            <span
                              className={cn(
                                "text-[10px] font-medium tabular-nums",
                                getConfidenceColor(field.confidence)
                              )}
                            >
                              {Math.round(field.confidence * 100)}%
                            </span>
                          </div>
                          {field.locked ? (
                            <Lock className="h-3 w-3 text-muted-foreground" />
                          ) : (
                            <Unlock className="h-3 w-3 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity" />
                          )}
                        </div>
                      </div>

                      {isMyReview && item.status === "in_review" && !field.locked ? (
                        <div className="flex gap-1.5 items-center">
                          <Input
                            {...getInputProps(field.field_name)}
                            value={
                              corrections[field.field_name] ?? field.value ?? ""
                            }
                            onChange={(e) =>
                              setCorrection(field.field_name, e.target.value)
                            }
                            className={cn(
                              "h-9 text-sm flex-1",
                              isFieldModified(field.field_name, field.value) &&
                                "border-amber-500 bg-amber-500/5"
                            )}
                          />
                          {isFieldModified(field.field_name, field.value) && (
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-9 w-9 shrink-0 text-muted-foreground hover:text-foreground"
                              onClick={() => resetField(field.field_name, field.value)}
                              title="Reset to AI value"
                            >
                              <RotateCcw className="h-3.5 w-3.5" />
                            </Button>
                          )}
                        </div>
                      ) : (
                        <p className="text-sm font-medium py-1">
                          {field.value || (
                            <span className="text-muted-foreground italic">
                              Empty
                            </span>
                          )}
                        </p>
                      )}

                      {field.manually_corrected && (
                        <p className="text-[10px] text-amber-600 mt-1">
                          Manually corrected
                          {field.corrected_by && ` by ${field.corrected_by}`}
                        </p>
                      )}
                    </div>
                  );
                })
              )}

              {item.status === "in_review" && isMyReview && (
                <div className="pt-4 space-y-3">
                  {/* Shortcut hint */}
                  <div className="flex items-center justify-center gap-1.5 px-4 py-1.5 bg-muted/50 rounded-lg text-[11px] text-muted-foreground">
                    <Keyboard className="h-3 w-3" />
                    <span>Shortcuts:</span>
                    <kbd className="px-1.5 py-0.5 rounded bg-background border text-[10px] font-mono font-medium shadow-sm">{modKey}+Shift+A</kbd>
                    <span>Approve</span>
                    <span className="mx-1 text-border">│</span>
                    <kbd className="px-1.5 py-0.5 rounded bg-background border text-[10px] font-mono font-medium shadow-sm">{modKey}+Shift+E</kbd>
                    <span>Correct</span>
                    <span className="mx-1 text-border">│</span>
                    <kbd className="px-1.5 py-0.5 rounded bg-background border text-[10px] font-mono font-medium shadow-sm">{modKey}+Shift+X</kbd>
                    <span>Reject</span>
                  </div>

                  <div className="flex gap-2">
                    <TooltipProvider>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            variant="outline"
                            className="flex-1 border-red-200 text-red-600 hover:bg-red-50 hover:text-red-700"
                            onClick={() => setRejectDialogOpen(true)}
                            disabled={submitting}
                            aria-label="Reject document"
                          >
                            <XCircle className="h-4 w-4 mr-1.5" aria-hidden="true" />
                            Reject
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent><p>{modKey}+Shift+X</p></TooltipContent>
                      </Tooltip>
                    </TooltipProvider>
                    <TooltipProvider>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            variant="outline"
                            className={cn(
                              "flex-1 border-amber-200 text-amber-600 hover:bg-amber-50 hover:text-amber-700",
                              !hasEdits && "opacity-40 cursor-not-allowed"
                            )}
                            onClick={() => handleSubmit("correct")}
                            disabled={submitting || !hasEdits}
                            aria-label="Submit corrections"
                          >
                            {submitting ? (
                              <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                            ) : (
                              <Edit3 className="h-4 w-4 mr-1.5" />
                            )}
                            Submit Corrections
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent>
                          <p>{hasEdits ? `${modKey}+Shift+E` : "Edit a field first"}</p>
                        </TooltipContent>
                      </Tooltip>
                    </TooltipProvider>
                    <TooltipProvider>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            className="flex-1 bg-green-600 hover:bg-green-700"
                            onClick={() => setApproveDialogOpen(true)}
                            disabled={submitting}
                            aria-label="Approve document"
                          >
                            {submitting ? (
                              <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                            ) : (
                              <CheckCircle2 className="h-4 w-4 mr-1.5" />
                            )}
                            Approve
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent><p>{modKey}+Shift+A</p></TooltipContent>
                      </Tooltip>
                    </TooltipProvider>
                  </div>
                </div>
              )}

              {item.status === "in_review" && !isMyReview && (
                <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
                  <div className="flex items-center gap-2">
                    <User className="h-4 w-4" />
                    <span>
                      Assigned to <strong>{item.assigned_to}</strong> — only the assigned reviewer can take action.
                    </span>
                  </div>
                </div>
              )}

              {item.status === "pending" && (
                <div className="mt-4 space-y-3">
                  {item.assigned_to && isMyReview ? (
                    /* Assigned to current reviewer — show Start Review button */
                    <div className="rounded-lg border border-blue-200 bg-blue-50 dark:border-blue-800 dark:bg-blue-950/30 px-4 py-4 text-center space-y-3">
                      <div className="flex items-center justify-center gap-2 text-sm text-blue-700 dark:text-blue-300">
                        <Clock className="h-4 w-4" />
                        <span>This document is assigned to you and awaiting review.</span>
                      </div>
                      <Button
                        className="bg-blue-600 hover:bg-blue-700"
                        disabled={startingReview}
                        onClick={async () => {
                          setStartingReview(true);
                          try {
                            await queueApi.startReview(item.id, getCurrentReviewer());
                            toast.success("Review started — SLA is now active.");
                            if (id) fetchItem(id);
                          } catch {
                            toast.error("Failed to start review.");
                          } finally {
                            setStartingReview(false);
                          }
                        }}
                      >
                        {startingReview ? (
                          <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                        ) : (
                          <ArrowLeft className="h-4 w-4 mr-1.5 rotate-180" />
                        )}
                        Start Review
                      </Button>
                    </div>
                  ) : item.assigned_to ? (
                    /* Assigned to a different reviewer */
                    <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
                      <div className="flex items-center gap-2">
                        <User className="h-4 w-4" />
                        <span>
                          Assigned to <strong>{item.assigned_to}</strong> — waiting for them to start review.
                        </span>
                      </div>
                    </div>
                  ) : (
                    /* Not yet assigned */
                    <div className="rounded-lg border bg-muted/40 px-4 py-3 text-sm text-muted-foreground text-center">
                      Waiting for auto-assignment…
                    </div>
                  )}
                </div>
              )}
            </TabsContent>

            <TabsContent
              value="lines"
              className="flex-1 overflow-y-auto m-0 p-4"
            >
              {Object.keys(lineItems).length > 0 ? (
                <Card>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>#</TableHead>
                        <TableHead>Field</TableHead>
                        <TableHead>Value</TableHead>
                        <TableHead className="text-right">
                          Confidence
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {Object.entries(lineItems).map(([idx, fields]) =>
                        fields.map((f) => (
                          <TableRow key={f.id}>
                            <TableCell className="text-muted-foreground">
                              {idx}
                            </TableCell>
                            <TableCell className="text-xs text-muted-foreground">
                              {f.field_name
                                .replace(`line_item_${idx}_`, "")
                                .replace(/_/g, " ")}
                            </TableCell>
                            <TableCell className="font-medium text-sm">
                              {isMyReview && item.status === "in_review" && !f.locked ? (
                                <Input
                                  value={
                                    corrections[f.field_name] ??
                                    f.value ??
                                    ""
                                  }
                                  onChange={(e) =>
                                    setCorrection(f.field_name, e.target.value)
                                  }
                                  className="h-7 text-xs"
                                />
                              ) : (
                                f.value ?? "—"
                              )}
                            </TableCell>
                            <TableCell className="text-right">
                              <span
                                className={cn(
                                  "text-xs font-medium",
                                  getConfidenceColor(f.confidence)
                                )}
                              >
                                {Math.round(f.confidence * 100)}%
                              </span>
                            </TableCell>
                          </TableRow>
                        ))
                      )}
                    </TableBody>
                  </Table>
                </Card>
              ) : (
                <p className="text-sm text-muted-foreground text-center py-8">
                  No line items extracted.
                </p>
              )}
            </TabsContent>

            <TabsContent
              value="history"
              className="flex-1 overflow-y-auto m-0 p-4 space-y-3"
              role="region"
              aria-label="Audit trail history"
            >
              {auditLoading ? (
                <div className="flex items-center justify-center py-12">
                  <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
                </div>
              ) : auditTrail.length === 0 ? (
                <p className="text-sm text-muted-foreground text-center py-8">
                  No audit history yet.
                </p>
              ) : (
                <div className="relative pl-6 space-y-0">
                  <div className="absolute left-2.5 top-2 bottom-2 w-px bg-border" />

                  {auditTrail.map((entry, idx) => (
                    <div key={entry.id ?? idx} className="relative pb-4 last:pb-0">
                      <div
                        className={cn(
                          "absolute -left-3.5 top-1.5 h-3 w-3 rounded-full border-2 border-background",
                          entry.action === "approve"
                            ? "bg-green-500"
                            : entry.action === "reject"
                              ? "bg-red-500"
                              : entry.action === "correct"
                                ? "bg-amber-500"
                                : entry.action === "auto_assign" || entry.action === "claim"
                                  ? "bg-blue-500"
                                  : "bg-muted-foreground"
                        )}
                      />

                      <div className="bg-muted/40 rounded-lg px-3 py-2.5 ml-2">
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-2">
                            <Badge
                              variant={
                                entry.action === "approve"
                                  ? "default"
                                  : entry.action === "reject"
                                    ? "destructive"
                                    : "secondary"
                              }
                              className="text-[10px] uppercase tracking-wide"
                            >
                              {entry.action.replace("_", " ")}
                            </Badge>
                            {entry.actor && (
                              <span className="text-xs text-muted-foreground">
                                by {entry.actor}
                              </span>
                            )}
                          </div>
                          <span className="text-[11px] text-muted-foreground tabular-nums">
                            {entry.created_at
                              ? new Date(entry.created_at).toLocaleString()
                              : "—"}
                          </span>
                        </div>

                        {entry.field_name && (
                          <div className="mt-1.5 text-xs space-y-0.5">
                            <p className="font-medium text-foreground">
                              {entry.field_name.replace(/_/g, " ")}
                            </p>
                            {entry.old_value && (
                              <p className="text-muted-foreground line-through">
                                {entry.old_value}
                              </p>
                            )}
                            {entry.new_value && (
                              <p className="text-foreground">{entry.new_value}</p>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </TabsContent>
          </Tabs>
        </div>
      </div>

      <Dialog open={rejectDialogOpen} onOpenChange={setRejectDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Reject Document</DialogTitle>
          </DialogHeader>
          <div className="space-y-3 py-2">
            <Label>Reason for rejection</Label>
            <Input
              placeholder="Enter reason…"
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
              aria-label="Rejection reason"
            />
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setRejectDialogOpen(false)}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                handleSubmit("reject", rejectReason);
                setRejectDialogOpen(false);
              }}
              disabled={!rejectReason.trim()}
            >
              Reject
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={approveDialogOpen} onOpenChange={setApproveDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Approve Document</DialogTitle>
          </DialogHeader>
          <div className="space-y-3 py-2">
            <p className="text-sm text-muted-foreground">
              Are you sure you want to approve this invoice? This action will
              finalize the extracted data for downstream processing.
            </p>
            {item && (
              <div className="rounded-lg border bg-muted/30 p-3 space-y-1.5 text-sm">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Document</span>
                  <span className="font-medium">{displayName}</span>
                </div>
                {subtitle && (
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Vendor</span>
                    <span className="font-medium">{subtitle}</span>
                  </div>
                )}
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Confidence</span>
                  <span className={cn("font-medium", getConfidenceColor(avgConfidence))}>
                    {Math.round(avgConfidence * 100)}%
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Fields</span>
                  <span className="font-medium">{item.fields?.length ?? 0}</span>
                </div>
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setApproveDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              className="bg-green-600 hover:bg-green-700"
              onClick={() => {
                handleSubmit("approve");
                setApproveDialogOpen(false);
              }}
              disabled={submitting}
            >
              {submitting ? (
                <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
              ) : (
                <CheckCircle2 className="h-4 w-4 mr-1.5" />
              )}
              Confirm Approval
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
