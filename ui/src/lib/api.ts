/** API client — Axios wrapper for the FastAPI backend. */

import axios from "axios";
import type {
  DocumentListResponse,
  PaginatedResponse,
  QueueStats,
  ReviewItem,
  ReviewSubmission,
  TrackedDocument,
  UploadResponse,
  AuditTrailResponse,
} from "./types";

const api = axios.create({
  baseURL: "/api",
  timeout: 30_000,
  headers: { "Content-Type": "application/json" },
});

/** Currently active reviewer ID (simulated multi-reviewer). */
let _currentReviewerId = localStorage.getItem("reviewer_id") || "reviewer-1";

export function setCurrentReviewer(id: string) {
  _currentReviewerId = id;
  localStorage.setItem("reviewer_id", id);
}

export function getCurrentReviewer(): string {
  return _currentReviewerId;
}

// Queue

export interface QueueFilters {
  status?: string;
  assigned_to?: string;
  priority_min?: number;
  sort_by?: "priority" | "sla" | "date";
  limit?: number;
  offset?: number;
}

export const queueApi = {
  getQueue: async (filters: QueueFilters = {}): Promise<PaginatedResponse> => {
    const { data } = await api.get<PaginatedResponse>("/queue", {
      params: filters,
    });
    return data;
  },

  getItem: async (id: string): Promise<ReviewItem> => {
    const { data } = await api.get<ReviewItem>(`/queue/${id}`);
    return data;
  },

  claimItem: async (
    id: string,
    reviewerId: string
  ): Promise<ReviewItem> => {
    const { data } = await api.post<ReviewItem>(`/queue/${id}/claim`, {
      reviewer_id: reviewerId,
    });
    return data;
  },

  submitReview: async (
    id: string,
    submission: ReviewSubmission
  ): Promise<ReviewItem> => {
    const { data } = await api.put<ReviewItem>(
      `/queue/${id}/submit`,
      submission,
      { params: { reviewer_id: _currentReviewerId } }
    );
    return data;
  },

  /** Auto-assign item to least-loaded reviewer. */
  autoAssign: async (id: string): Promise<ReviewItem> => {
    const { data } = await api.post<ReviewItem>(`/queue/${id}/auto-assign`);
    return data;
  },

  /** Start reviewing an item (transitions pending → in_review, starts SLA). */
  startReview: async (id: string, reviewerId: string): Promise<ReviewItem> => {
    const { data } = await api.post<ReviewItem>(`/queue/${id}/claim`, {
      reviewer_id: reviewerId,
    });
    return data;
  },

  /** Fetch the full audit trail for a review item. */
  getAuditTrail: async (id: string): Promise<AuditTrailResponse> => {
    const { data } = await api.get<AuditTrailResponse>(`/queue/${id}/audit`);
    return data;
  },

  getStats: async (): Promise<QueueStats> => {
    const { data } = await api.get<QueueStats>("/stats");
    return data;
  },
};

// Documents

export const documentApi = {
  upload: async (file: File): Promise<UploadResponse> => {
    const form = new FormData();
    form.append("file", file);
    const { data } = await api.post<UploadResponse>("/documents/upload", form, {
      headers: { "Content-Type": "multipart/form-data" },
    });
    return data;
  },

  /** List all tracked documents (with processing status from backend). */
  list: async (params?: {
    status?: string;
    limit?: number;
    offset?: number;
  }): Promise<DocumentListResponse> => {
    const { data } = await api.get<DocumentListResponse>("/documents", {
      params,
    });
    return data;
  },

  /** Get a single document's processing status. */
  getStatus: async (docId: string): Promise<TrackedDocument> => {
    const { data } = await api.get<TrackedDocument>(
      `/documents/${docId}/status`
    );
    return data;
  },

  getPreviewUrl: (docId: string): string =>
    `${api.defaults.baseURL}/documents/${docId}/preview`,

  getDownloadUrl: (docId: string, format: "parquet" | "json"): string =>
    `${api.defaults.baseURL}/documents/${docId}/download/${format}`,
};

export default api;
