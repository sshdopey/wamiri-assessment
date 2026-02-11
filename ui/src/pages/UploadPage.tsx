import { useCallback, useState, useRef, useEffect } from "react";
import {
  Upload,
  FileText,
  CheckCircle2,
  Loader2,
  AlertCircle,
  X,
  Sparkles,
  ArrowRight,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { documentApi } from "@/lib/api";
import type { UploadResponse } from "@/lib/types";
import { useUploadTracking } from "@/lib/upload-tracking";
import { toast } from "sonner";
import { useNavigate } from "react-router-dom";

interface UploadedFile {
  file: File;
  status: "pending" | "uploading" | "success" | "error";
  progress: number;
  response?: UploadResponse;
  error?: string;
}

export function UploadPage() {
  const [files, setFiles] = useState<UploadedFile[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();
  const { uploads, addUpload, pollProcessing, clearReady } =
    useUploadTracking();

  // Poll for processing completion every 5s
  useEffect(() => {
    const interval = setInterval(pollProcessing, 5000);
    return () => clearInterval(interval);
  }, [pollProcessing]);

  const addFiles = useCallback((newFiles: FileList | File[]) => {
    const ACCEPTED_TYPES = [
      "application/pdf",
      "image/png",
      "image/jpeg",
      "image/webp",
      "image/gif",
      "image/tiff",
      "image/bmp",
    ];
    const validFiles = Array.from(newFiles).filter((f) =>
      ACCEPTED_TYPES.includes(f.type) ||
      /\.(pdf|png|jpe?g|webp|gif|tiff?|bmp)$/i.test(f.name)
    );
    if (validFiles.length === 0) {
      toast.error("Only PDF and image files (PNG, JPG, WebP, GIF, TIFF, BMP) are supported");
      return;
    }
    const entries: UploadedFile[] = validFiles.map((f) => ({
      file: f,
      status: "pending" as const,
      progress: 0,
    }));
    setFiles((prev) => [...prev, ...entries]);
  }, []);

  const uploadFile = async (index: number) => {
    setFiles((prev) =>
      prev.map((f, i) =>
        i === index ? { ...f, status: "uploading" as const, progress: 30 } : f
      )
    );
    try {
      const entry = files[index];
      setFiles((prev) =>
        prev.map((f, i) => (i === index ? { ...f, progress: 60 } : f))
      );
      const response = await documentApi.upload(entry.file);
      setFiles((prev) =>
        prev.map((f, i) =>
          i === index
            ? { ...f, status: "success" as const, progress: 100, response }
            : f
        )
      );
      // Track this upload globally so other pages can see it
      addUpload(response.document_id, entry.file.name, response.task_id);
      toast.success(`${entry.file.name} uploaded — AI extraction started`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Upload failed";
      setFiles((prev) =>
        prev.map((f, i) =>
          i === index ? { ...f, status: "error" as const, error: msg } : f
        )
      );
      toast.error("Failed to upload file");
    }
  };

  const uploadAll = async () => {
    const pendingIndices = files
      .map((f, i) => (f.status === "pending" ? i : -1))
      .filter((i) => i >= 0);
    for (const i of pendingIndices) {
      await uploadFile(i);
    }
  };

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  };

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setIsDragging(false);
      addFiles(e.dataTransfer.files);
    },
    [addFiles]
  );

  const pendingCount = files.filter((f) => f.status === "pending").length;
  const successCount = files.filter((f) => f.status === "success").length;
  const processingUploads = uploads.filter((u) => u.status === "processing");
  const readyUploads = uploads.filter((u) => u.status === "ready");

  return (
    <div className="p-6 lg:p-8 max-w-4xl mx-auto space-y-8">
      {/* ── Header ────────────────────────────────────────────────── */}
      <div>
        <h2 className="text-2xl font-bold tracking-tight">Upload Invoices</h2>
        <p className="text-muted-foreground mt-1">
          Upload invoice PDFs or images and our AI will automatically extract vendor info,
          amounts, dates, and line items. You'll review the results before
          they're finalized.
        </p>
      </div>

      {/* ── Active processing banner ──────────────────────────────── */}
      {(processingUploads.length > 0 || readyUploads.length > 0) && (
        <Card className="border-blue-200 bg-blue-50/60 dark:bg-blue-950/20">
          <CardContent className="py-3 px-4 space-y-2">
            {processingUploads.length > 0 && (
              <div className="flex items-center gap-3">
                <Loader2 className="h-4 w-4 text-blue-600 animate-spin shrink-0" />
                <div className="flex-1">
                  <p className="text-sm font-medium text-blue-900 dark:text-blue-200">
                    AI is extracting data from{" "}
                    {processingUploads.length} document
                    {processingUploads.length !== 1 ? "s" : ""}…
                  </p>
                  <p className="text-xs text-blue-700/70 dark:text-blue-300/70">
                    {processingUploads.map((u) => u.filename).join(", ")}
                  </p>
                </div>
              </div>
            )}
            {readyUploads.length > 0 && (
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <CheckCircle2 className="h-4 w-4 text-green-600 shrink-0" />
                  <span className="text-sm font-medium text-green-800 dark:text-green-300">
                    {readyUploads.length} document
                    {readyUploads.length !== 1 ? "s" : ""} ready for review!
                  </span>
                </div>
                <Button
                  size="sm"
                  onClick={() => {
                    clearReady();
                    navigate("/queue");
                  }}
                >
                  Review Now <ArrowRight className="h-3.5 w-3.5 ml-1" />
                </Button>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* ── Drop zone ─────────────────────────────────────────────── */}
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setIsDragging(true);
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={handleDrop}
        onClick={() => fileInputRef.current?.click()}
        className={cn(
          "relative cursor-pointer rounded-xl border-2 border-dashed p-12 text-center transition-all duration-200",
          isDragging
            ? "border-primary bg-primary/5 scale-[1.01]"
            : "border-muted-foreground/25 hover:border-primary/50 hover:bg-muted/50"
        )}
      >
        <input
          ref={fileInputRef}
          type="file"
          accept=".pdf,.png,.jpg,.jpeg,.webp,.gif,.tiff,.tif,.bmp,application/pdf,image/png,image/jpeg,image/webp,image/gif,image/tiff,image/bmp"
          multiple
          className="hidden"
          onChange={(e) => e.target.files && addFiles(e.target.files)}
        />
        <div className="flex flex-col items-center gap-4">
          <div
            className={cn(
              "rounded-full p-4 transition-colors",
              isDragging ? "bg-primary/10" : "bg-muted"
            )}
          >
            <Upload
              className={cn(
                "h-8 w-8 transition-colors",
                isDragging ? "text-primary" : "text-muted-foreground"
              )}
            />
          </div>
          <div>
            <p className="text-base font-medium">
              {isDragging
                ? "Drop files here"
                : "Drag & drop invoices here"}
            </p>
            <p className="text-sm text-muted-foreground mt-1">
              or click to browse • PDF and image files (PNG, JPG, WebP, etc.)
            </p>
          </div>
        </div>
      </div>

      {/* ── File list ─────────────────────────────────────────────── */}
      {files.length > 0 && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider">
              {files.length} file{files.length !== 1 && "s"} selected
            </h3>
            <div className="flex gap-2">
              {successCount > 0 && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => navigate("/documents")}
                >
                  View Documents →
                </Button>
              )}
              {pendingCount > 0 && (
                <Button size="sm" onClick={uploadAll}>
                  <Upload className="h-4 w-4 mr-1.5" />
                  Upload{" "}
                  {pendingCount === files.length
                    ? "All"
                    : `${pendingCount} Remaining`}
                </Button>
              )}
            </div>
          </div>

          <div className="space-y-2">
            {files.map((entry, index) => (
              <Card
                key={`${entry.file.name}-${index}`}
                className={cn(
                  "transition-colors",
                  entry.status === "success" &&
                    "border-green-500/30 bg-green-500/5",
                  entry.status === "error" &&
                    "border-destructive/30 bg-destructive/5"
                )}
              >
                <CardContent className="flex items-center gap-4 py-3 px-4">
                  {/* Icon */}
                  <div
                    className={cn(
                      "flex h-10 w-10 shrink-0 items-center justify-center rounded-lg",
                      entry.status === "success"
                        ? "bg-green-500/10 text-green-600"
                        : entry.status === "error"
                          ? "bg-destructive/10 text-destructive"
                          : "bg-muted text-muted-foreground"
                    )}
                  >
                    {entry.status === "uploading" ? (
                      <Loader2 className="h-5 w-5 animate-spin" />
                    ) : entry.status === "success" ? (
                      <CheckCircle2 className="h-5 w-5" />
                    ) : entry.status === "error" ? (
                      <AlertCircle className="h-5 w-5" />
                    ) : (
                      <FileText className="h-5 w-5" />
                    )}
                  </div>

                  {/* Info */}
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium truncate">
                      {entry.file.name}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      {(entry.file.size / 1024).toFixed(0)} KB
                      {entry.status === "success" && entry.response && (
                        <span className="text-blue-600 ml-2">
                          <Sparkles className="h-3 w-3 inline mr-0.5" />
                          AI is extracting data…
                        </span>
                      )}
                      {entry.status === "error" && (
                        <span className="text-destructive ml-2">
                          • {entry.error}
                        </span>
                      )}
                    </p>
                    {entry.status === "uploading" && (
                      <Progress value={entry.progress} className="h-1 mt-2" />
                    )}
                  </div>

                  {/* Status badge */}
                  <div className="shrink-0">
                    {entry.status === "success" && (
                      <Badge
                        variant="secondary"
                        className="bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300"
                      >
                        <Loader2 className="h-3 w-3 mr-1 animate-spin" />
                        Extracting
                      </Badge>
                    )}
                  </div>

                  {/* Actions */}
                  <div className="shrink-0">
                    {entry.status === "pending" && (
                      <div className="flex gap-1">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => uploadFile(index)}
                        >
                          Upload
                        </Button>
                        <Button
                          size="icon"
                          variant="ghost"
                          className="h-8 w-8 text-muted-foreground"
                          onClick={() => removeFile(index)}
                        >
                          <X className="h-4 w-4" />
                        </Button>
                      </div>
                    )}
                    {entry.status === "error" && (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => uploadFile(index)}
                      >
                        Retry
                      </Button>
                    )}
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      )}

      {/* ── How it works ──────────────────────────────────────────── */}
      {files.length === 0 && (
        <div>
          <h3 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider mb-3">
            How it works
          </h3>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            {[
              {
                num: 1,
                title: "Upload Invoice",
                desc: "Drop your invoice files here. Supports PDFs and images (PNG, JPG, WebP, etc.) in any language.",
                icon: Upload,
              },
              {
                num: 2,
                title: "AI Extracts Data",
                desc: "Google Gemini reads the document and extracts vendor, amounts, dates, and line items.",
                icon: Sparkles,
              },
              {
                num: 3,
                title: "Review & Approve",
                desc: "See the document side-by-side with extracted data. Correct anything, then approve.",
                icon: CheckCircle2,
              },
            ].map((step) => (
              <Card key={step.num} className="relative overflow-hidden">
                <CardContent className="pt-6 pb-5 px-5">
                  <div className="text-4xl font-bold text-muted-foreground/10 absolute top-2 right-4">
                    {step.num}
                  </div>
                  <div className="rounded-lg bg-primary/5 p-2 w-fit mb-3">
                    <step.icon className="h-4 w-4 text-primary" />
                  </div>
                  <h4 className="font-semibold text-sm">{step.title}</h4>
                  <p className="text-xs text-muted-foreground mt-1 leading-relaxed">
                    {step.desc}
                  </p>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
