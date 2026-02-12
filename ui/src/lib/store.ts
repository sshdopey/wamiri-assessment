/**
 * Global state (Zustand) — single source of truth for all pages.
 * Cross-tab sync via zustand-sync-tabs.
 * Always-on 5s polling keeps every tab current without manual refresh.
 */

import { create } from "zustand";
import { syncTabs } from "zustand-sync-tabs";
import type {
  ReviewItem,
  QueueStats,
  ReviewAction,
  AuditEntry,
  TrackedDocument,
} from "./types";
import { queueApi, documentApi } from "./api";

// Polling interval (ms)
const POLL_INTERVAL = 5_000;

// Store shape

interface ReviewStore {
  /* queue list */
  queueItems: ReviewItem[];
  total: number;
  page: number;
  pageSize: number;
  filters: { status: string | undefined; sort_by: "priority" | "sla" | "date"; assigned_to: string | undefined };

  /* current review item (shared across tabs) */
  currentItem: ReviewItem | null;
  corrections: Record<string, string>;

  /* dashboard stats */
  stats: QueueStats;

  /* audit trail for current item */
  auditTrail: AuditEntry[];

  /* document tracking (upload history) */
  documents: TrackedDocument[];
  documentsTotal: number;
  documentsFilter: string | undefined;

  /* ui flags */
  loading: boolean;
  error: string | null;

  /* actions */
  fetchQueue: () => Promise<void>;
  fetchStats: () => Promise<void>;
  fetchItem: (id: string) => Promise<void>;
  fetchAuditTrail: (id: string) => Promise<void>;
  fetchDocuments: () => Promise<void>;
  setDocumentsFilter: (status: string | undefined) => void;
  setFilter: (key: string, value: unknown) => void;
  setPage: (page: number) => void;
  setCorrection: (field: string, value: string) => void;
  resetCorrections: () => void;
  submitReview: (itemId: string, action: ReviewAction, corrections: Record<string, string>, reason?: string) => Promise<void>;

  /* polling control */
  _pollTimer: ReturnType<typeof setInterval> | null;
  startPolling: () => void;
  stopPolling: () => void;
}

export const useReviewStore = create<ReviewStore>()(
  syncTabs(
    (set, get) => ({
      // Initial state
      queueItems: [],
      total: 0,
      page: 0,
      pageSize: 20,
      filters: { status: undefined, sort_by: "priority", assigned_to: undefined },
      currentItem: null,
      corrections: {},
      stats: {
        queue_depth: 0,
        items_reviewed_today: 0,
        avg_review_time_seconds: 0,
        sla_compliance_percent: 100,
      },
      auditTrail: [],
      documents: [],
      documentsTotal: 0,
      documentsFilter: undefined,
      loading: false,
      error: null,
      _pollTimer: null,

      // Fetch queue
      fetchQueue: async () => {
        try {
          const { filters, page, pageSize } = get();
          const res = await queueApi.getQueue({
            ...filters,
            limit: pageSize,
            offset: page * pageSize,
          });
          set({ queueItems: res.items, total: res.total, error: null });
        } catch (err: unknown) {
          const msg = err instanceof Error ? err.message : "Failed to fetch queue";
          set({ error: msg });
        }
      },

      // Fetch stats
      fetchStats: async () => {
        try {
          const stats = await queueApi.getStats();
          set({ stats });
        } catch {
          /* non-critical */
        }
      },

      // Fetch single item
      fetchItem: async (id: string) => {
        set({ loading: true, error: null });
        try {
          const item = await queueApi.getItem(id);
          // Initialize corrections from current field values
          const corr: Record<string, string> = {};
          item.fields?.forEach((f) => {
            if (f.value != null) corr[f.field_name] = f.value;
          });
          set({ currentItem: item, corrections: corr, loading: false });
        } catch (err: unknown) {
          const msg = err instanceof Error ? err.message : "Failed to load item";
          set({ error: msg, loading: false });
        }
      },

      // Audit trail
      fetchAuditTrail: async (id: string) => {
        try {
          const res = await queueApi.getAuditTrail(id);
          set({ auditTrail: res.trail });
        } catch {
          /* silent */
        }
      },

      // Document tracking (upload history)
      fetchDocuments: async () => {
        try {
          const { documentsFilter } = get();
          const res = await documentApi.list({
            status: documentsFilter,
            limit: 200,
            offset: 0,
          });
          set({ documents: res.items, documentsTotal: res.total });
        } catch {
          /* non-critical */
        }
      },

      setDocumentsFilter: (status) => set({ documentsFilter: status }),

      // Filters / pagination
      setFilter: (key, value) =>
        set((s) => ({ filters: { ...s.filters, [key]: value }, page: 0 })),

      setPage: (page) => set({ page }),

      // Corrections
      setCorrection: (field, value) =>
        set((s) => ({ corrections: { ...s.corrections, [field]: value } })),

      resetCorrections: () => set({ corrections: {} }),

      // Submit review
      submitReview: async (itemId, action, corrections, reason) => {
        set({ loading: true });
        try {
          await queueApi.submitReview(itemId, { action, corrections, reason });
          // Refresh everything so every tab sees the change
          set({ currentItem: null, corrections: {}, loading: false });
          get().fetchQueue();
          get().fetchStats();
          get().fetchDocuments();
        } catch (err: unknown) {
          const msg = err instanceof Error ? err.message : "Review submission failed";
          set({ error: msg, loading: false });
          throw new Error(msg);
        }
      },

      // Polling
      startPolling: () => {
        const existing = get()._pollTimer;
        if (existing) return; // already running
        // Fetch immediately
        get().fetchQueue();
        get().fetchStats();
        get().fetchDocuments();
        const timer = setInterval(() => {
          get().fetchQueue();
          get().fetchStats();
          get().fetchDocuments();
        }, POLL_INTERVAL);
        set({ _pollTimer: timer });
      },

      stopPolling: () => {
        const timer = get()._pollTimer;
        if (timer) {
          clearInterval(timer);
          set({ _pollTimer: null });
        }
      },
    }),
    { name: "wamiri-review-store" },
  ),
);
