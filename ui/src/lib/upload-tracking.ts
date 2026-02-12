/** Upload tracking store — polls backend /api/documents for real status. */

import { create } from "zustand";
import type { TrackedUpload, UploadTrackingStatus } from "./types";
import { documentApi } from "./api";

interface UploadTrackingStore {
  uploads: TrackedUpload[];

  /** Add a new tracked upload after successful API upload */
  addUpload: (doc_id: string, filename: string, task_id?: string) => void;

  /** Update status for a tracked upload */
  updateStatus: (doc_id: string, status: UploadTrackingStatus) => void;

  /** Remove an upload from tracking */
  removeUpload: (doc_id: string) => void;

  /** Poll all "processing" uploads against backend /api/documents/{id}/status */
  pollProcessing: () => Promise<void>;

  /** Clear completed/ready uploads */
  clearReady: () => void;

  /** Get count of actively processing uploads */
  processingCount: () => number;
}

export const useUploadTracking = create<UploadTrackingStore>((set, get) => ({
  uploads: [],

  addUpload: (document_id, filename, task_id) =>
    set((s) => ({
      uploads: [
        ...s.uploads,
        {
          document_id,
          filename,
          status: "processing" as const,
          uploadedAt: new Date().toISOString(),
          task_id,
        },
      ],
    })),

  updateStatus: (doc_id, status) =>
    set((s) => ({
      uploads: s.uploads.map((u) =>
        u.document_id === doc_id ? { ...u, status } : u
      ),
    })),

  removeUpload: (doc_id) =>
    set((s) => ({
      uploads: s.uploads.filter((u) => u.document_id !== doc_id),
    })),

  pollProcessing: async () => {
    const { uploads, updateStatus } = get();
    const processing = uploads.filter((u) => u.status === "processing");
    if (processing.length === 0) return;

    // Poll each processing upload against the backend documents API
    for (const u of processing) {
      try {
        const doc = await documentApi.getStatus(u.document_id);
        if (doc.status === "completed") {
          updateStatus(u.document_id, "ready");
        } else if (doc.status === "failed") {
          updateStatus(u.document_id, "failed");
        }
        // "queued" and "processing" stay as "processing" in the UI
      } catch {
        // Silently ignore individual polling errors
      }
    }
  },

  clearReady: () =>
    set((s) => ({
      uploads: s.uploads.filter((u) => u.status !== "ready"),
    })),

  processingCount: () =>
    get().uploads.filter((u) => u.status === "processing").length,
}));
