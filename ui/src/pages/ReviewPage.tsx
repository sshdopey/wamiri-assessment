import { useEffect, useState, useCallback } from "react";
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
} from "lucide-react";
import { Button } from "@/components/ui/button";
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
import { queueApi, documentApi } from "@/lib/api";
import type { ReviewItem, ExtractedField, ReviewAction } from "@/lib/types";
import { FIELD_META, getDocumentDisplayName, getDocumentSubtitle } from "@/lib/types";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { PdfViewer } from "@/components/PdfViewer";

export function ReviewPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const backTo = (location.state as { from?: string })?.from || "/queue";
  const [item, setItem] = useState<ReviewItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [corrections, setCorrections] = useState<Record<string, string>>({});
  const [rejectDialogOpen, setRejectDialogOpen] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [activeTab, setActiveTab] = useState("fields");

  const fetchItem = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    try {
      const data = await queueApi.getItem(id);
      setItem(data);
      // Initialize corrections from current values
      const corr: Record<string, string> = {};
      data.fields?.forEach((f) => {
        if (f.value) corr[f.field_name] = f.value;
      });
      setCorrections(corr);
    } catch {
      toast.error("Failed to load document");
      navigate(backTo);
    } finally {
      setLoading(false);
    }
  }, [id, navigate]);

  useEffect(() => {
    fetchItem();
  }, [fetchItem]);

  const handleClaim = async () => {
    if (!item) return;
    try {
      const claimed = await queueApi.claimItem(item.id, "reviewer-1");
      setItem(claimed);
      toast.success("Document claimed for review");
    } catch {
      toast.error("Failed to claim document");
    }
  };

  const handleSubmit = async (action: ReviewAction, reason?: string) => {
    if (!item) return;
    setSubmitting(true);
    try {
      // Build corrections: only send fields that actually changed
      const changedCorrections: Record<string, string> = {};
      if (action === "correct") {
        item.fields?.forEach((f) => {
          const newVal = corrections[f.field_name];
          if (newVal !== undefined && newVal !== f.value) {
            changedCorrections[f.field_name] = newVal;
          }
        });
      }

      await queueApi.submitReview(item.id, {
        action,
        corrections: changedCorrections,
        reason,
      });

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

  // Separate header fields from line items
  const headerFields =
    item?.fields?.filter((f) => !f.field_name.startsWith("line_item")) ?? [];

  const lineItemFields =
    item?.fields?.filter((f) => f.field_name.startsWith("line_item")) ?? [];

  // Group line items by index
  const lineItems: Record<string, ExtractedField[]> = {};
  lineItemFields.forEach((f) => {
    const match = f.field_name.match(/line_item_(\d+)/);
    if (match) {
      const key = match[1];
      if (!lineItems[key]) lineItems[key] = [];
      lineItems[key].push(f);
    }
  });

  const canReview =
    item?.status === "pending" || item?.status === "in_review";

  if (loading) {
    return (
      <div className="p-6 lg:p-8 max-w-7xl mx-auto space-y-6">
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
      {/* ── Top bar ───────────────────────────────────────────────── */}
      <div className="flex items-center gap-3 px-6 py-3 border-b bg-card shrink-0">
        <Button variant="ghost" size="icon" onClick={() => navigate(backTo)}>
          <ArrowLeft className="h-4 w-4" />
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
              {item.status.replace("_", " ")}
            </Badge>
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
            {displayName !== item.filename && (
              <>
                <span>•</span>
                <span className="italic truncate max-w-[140px]">
                  {item.filename}
                </span>
              </>
            )}
          </div>
        </div>

        {/* Actions */}
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
          >
            <Download className="h-4 w-4 mr-1.5" />
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
          >
            <ExternalLink className="h-4 w-4 mr-1.5" />
            Open File
          </Button>
        </div>
      </div>

      {/* ── Split pane ────────────────────────────────────────────── */}
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
              </TabsList>
            </div>

            {/* ── Fields tab ──────────────────────────────────────── */}
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
                          {/* Confidence bar */}
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

                      {canReview && !field.locked ? (
                        <Input
                          value={
                            corrections[field.field_name] ?? field.value ?? ""
                          }
                          onChange={(e) =>
                            setCorrections((prev) => ({
                              ...prev,
                              [field.field_name]: e.target.value,
                            }))
                          }
                          className={cn(
                            "h-9 text-sm",
                            corrections[field.field_name] !== undefined &&
                              corrections[field.field_name] !== field.value &&
                              "border-amber-500 bg-amber-500/5"
                          )}
                        />
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
            </TabsContent>

            {/* ── Line items tab ──────────────────────────────────── */}
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
                              {canReview && !f.locked ? (
                                <Input
                                  value={
                                    corrections[f.field_name] ??
                                    f.value ??
                                    ""
                                  }
                                  onChange={(e) =>
                                    setCorrections((prev) => ({
                                      ...prev,
                                      [f.field_name]: e.target.value,
                                    }))
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
          </Tabs>

          {/* ── Action bar ────────────────────────────────────────── */}
          {canReview && (
            <div className="border-t bg-card px-4 py-3 shrink-0">
              {item.status === "pending" ? (
                <Button className="w-full" onClick={handleClaim}>
                  Claim for Review
                </Button>
              ) : (
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    className="flex-1 border-red-200 text-red-600 hover:bg-red-50 hover:text-red-700"
                    onClick={() => setRejectDialogOpen(true)}
                    disabled={submitting}
                  >
                    <XCircle className="h-4 w-4 mr-1.5" />
                    Reject
                  </Button>
                  <Button
                    variant="outline"
                    className="flex-1 border-amber-200 text-amber-600 hover:bg-amber-50 hover:text-amber-700"
                    onClick={() => handleSubmit("correct")}
                    disabled={submitting}
                  >
                    {submitting ? (
                      <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                    ) : (
                      <Edit3 className="h-4 w-4 mr-1.5" />
                    )}
                    Submit Corrections
                  </Button>
                  <Button
                    className="flex-1 bg-green-600 hover:bg-green-700"
                    onClick={() => handleSubmit("approve")}
                    disabled={submitting}
                  >
                    {submitting ? (
                      <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                    ) : (
                      <CheckCircle2 className="h-4 w-4 mr-1.5" />
                    )}
                    Approve
                  </Button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* ── Reject dialog ─────────────────────────────────────────── */}
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
    </div>
  );
}
