/** TypeScript types mirroring backend Pydantic models. */

export type ReviewStatus =
  | "pending"
  | "in_review"
  | "approved"
  | "corrected"
  | "rejected";

export type ReviewAction = "approve" | "correct" | "reject";

export interface ExtractedField {
  id: string;
  review_item_id: string;
  field_name: string;
  value: string | null;
  confidence: number;
  manually_corrected: boolean;
  corrected_at: string | null;
  corrected_by: string | null;
  locked: boolean;
}

export interface ReviewItem {
  id: string;
  document_id: string;
  filename: string;
  status: ReviewStatus;
  priority: number;
  sla_deadline: string | null;
  assigned_to: string | null;
  created_at: string;
  claimed_at: string | null;
  completed_at: string | null;
  fields: ExtractedField[];
}

export interface PaginatedResponse {
  items: ReviewItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface QueueStats {
  queue_depth: number;
  items_reviewed_today: number;
  avg_review_time_seconds: number;
  sla_compliance_percent: number;
}

export interface ReviewSubmission {
  action: ReviewAction;
  corrections: Record<string, string>;
  reason?: string;
}

export interface UploadResponse {
  document_id: string;
  task_id: string;
  filename: string;
  mime_type: string;
  status: string;
}

// Document tracking (backend-synced)

export type DocumentTrackingStatus = "queued" | "processing" | "completed" | "failed" | "duplicate";

export interface TrackedDocument {
  id: string;
  filename: string;
  original_filename: string;
  mime_type: string;
  status: DocumentTrackingStatus;
  task_id: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface DocumentListResponse {
  items: TrackedDocument[];
  total: number;
}

// Tracked upload (local-only, before Celery finishes)

export type UploadTrackingStatus = "uploading" | "processing" | "ready" | "failed";

export interface TrackedUpload {
  document_id: string;
  filename: string;
  status: UploadTrackingStatus;
  uploadedAt: string;
  task_id?: string;
}

// Audit trail

export interface AuditEntry {
  id: number;
  item_id: string;
  action: string;
  field_name: string | null;
  old_value: string | null;
  new_value: string | null;
  actor: string | null;
  created_at: string | null;
}

export interface AuditTrailResponse {
  item_id: string;
  trail: AuditEntry[];
}

/* Field metadata for smart inputs */
export interface FieldMeta {
  name: string;
  label: string;
  type: "text" | "number" | "date" | "currency";
  required: boolean;
}

export const FIELD_META: FieldMeta[] = [
  { name: "vendor", label: "Vendor", type: "text", required: true },
  { name: "invoice_number", label: "Invoice #", type: "text", required: true },
  { name: "date", label: "Date", type: "date", required: true },
  { name: "due_date", label: "Due Date", type: "date", required: false },
  { name: "subtotal", label: "Subtotal", type: "currency", required: false },
  { name: "tax_rate", label: "Tax Rate (%)", type: "number", required: false },
  { name: "tax_amount", label: "Tax Amount", type: "currency", required: false },
  { name: "total", label: "Total", type: "currency", required: true },
  { name: "currency", label: "Currency", type: "text", required: false },
];

// Helpers

/** Extract a human-friendly display name from a ReviewItem's fields.
 *  Returns "Vendor — INV#" or falls back to filename. */
export function getDocumentDisplayName(item: ReviewItem): string {
  const vendor = item.fields?.find((f) => f.field_name === "vendor")?.value;
  const invNum = item.fields?.find((f) => f.field_name === "invoice_number")?.value;

  if (vendor && invNum) return `${vendor} — ${invNum}`;
  if (vendor) return vendor;
  if (invNum) return `Invoice ${invNum}`;
  return item.filename;
}

/** Get subtitle info: total amount + currency (if available) */
export function getDocumentSubtitle(item: ReviewItem): string | null {
  const total = item.fields?.find((f) => f.field_name === "total")?.value;
  const currency = item.fields?.find((f) => f.field_name === "currency")?.value;
  const date = item.fields?.find((f) => f.field_name === "date")?.value;

  const parts: string[] = [];
  if (total) parts.push(`${currency ?? "$"}${total}`);
  if (date) parts.push(date);
  return parts.length > 0 ? parts.join(" • ") : null;
}
