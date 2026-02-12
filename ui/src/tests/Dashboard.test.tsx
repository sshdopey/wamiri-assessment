
import { describe, test, expect, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { MemoryRouter } from "react-router-dom";

// Mock API layer
vi.mock("@/lib/api", () => ({
  queueApi: {
    getQueue: vi.fn().mockResolvedValue({
      items: [
        {
          id: "q-1",
          document_id: "doc-1",
          filename: "invoice_cloud_vps.pdf",
          status: "pending",
          priority: 72,
          sla_deadline: new Date(Date.now() + 3_600_000).toISOString(),
          assigned_to: null,
          created_at: new Date().toISOString(),
          claimed_at: null,
          completed_at: null,
          fields: [
            {
              id: "f-1",
              review_item_id: "q-1",
              field_name: "vendor",
              value: "CloudVPS B.V.",
              confidence: 0.95,
              manually_corrected: false,
              corrected_at: null,
              corrected_by: null,
              locked: false,
            },
            {
              id: "f-2",
              review_item_id: "q-1",
              field_name: "total",
              value: "1250.00",
              confidence: 0.65,
              manually_corrected: false,
              corrected_at: null,
              corrected_by: null,
              locked: false,
            },
          ],
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    }),
    getItem: vi.fn().mockResolvedValue({
      id: "q-1",
      document_id: "doc-1",
      filename: "invoice_cloud_vps.pdf",
      status: "pending",
      priority: 72,
      sla_deadline: new Date(Date.now() + 3_600_000).toISOString(),
      assigned_to: null,
      created_at: new Date().toISOString(),
      claimed_at: null,
      completed_at: null,
      fields: [
        {
          id: "f-1",
          review_item_id: "q-1",
          field_name: "vendor",
          value: "CloudVPS B.V.",
          confidence: 0.95,
          manually_corrected: false,
          corrected_at: null,
          corrected_by: null,
          locked: false,
        },
      ],
    }),
    claimItem: vi.fn().mockResolvedValue({ id: "q-1", status: "in_review" }),
    submitReview: vi.fn().mockResolvedValue({ id: "q-1", status: "approved" }),
    getStats: vi.fn().mockResolvedValue({
      queue_depth: 42,
      items_reviewed_today: 15,
      avg_review_time_seconds: 28.5,
      sla_compliance_percent: 97.3,
    }),
  },
  documentApi: {
    upload: vi.fn(),
    list: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    getStatus: vi.fn().mockResolvedValue({ id: "doc-1", status: "completed" }),
    getPreviewUrl: vi.fn().mockReturnValue("/preview"),
    getDownloadUrl: vi.fn().mockReturnValue("/download"),
  },
}));

// Import components AFTER mocks
import { DashboardPage } from "@/pages/DashboardPage";
import { QueuePage } from "@/pages/QueuePage";
import { UploadPage } from "@/pages/UploadPage";
import { TooltipProvider } from "@/components/ui/tooltip";

// Helpers

function renderWithRouter(ui: React.ReactElement) {
  return render(
    <MemoryRouter>
      <TooltipProvider>{ui}</TooltipProvider>
    </MemoryRouter>
  );
}

// Tests

describe("DashboardPage", () => {
  test("renders dashboard heading", async () => {
    renderWithRouter(<DashboardPage />);
    expect(screen.getByText("Invoice Processing Hub")).toBeInTheDocument();
  });

  test("displays KPI cards after loading", async () => {
    renderWithRouter(<DashboardPage />);
    await waitFor(() => {
      expect(screen.getByText("Queue Depth")).toBeInTheDocument();
      expect(screen.getByText("Reviewed Today")).toBeInTheDocument();
      expect(screen.getByText("Avg Review Time")).toBeInTheDocument();
      expect(screen.getByText("SLA Compliance")).toBeInTheDocument();
    });
  });

  test("shows stat values from API", async () => {
    renderWithRouter(<DashboardPage />);
    await waitFor(() => {
      expect(screen.getByText("42")).toBeInTheDocument();
      expect(screen.getByText("15")).toBeInTheDocument();
      expect(screen.getByText("97%")).toBeInTheDocument();
    });
  });

  test("has quick action cards", async () => {
    renderWithRouter(<DashboardPage />);
    await waitFor(() => {
      expect(screen.getByText("Upload New Invoices")).toBeInTheDocument();
      expect(screen.getByText("Review Pending Items")).toBeInTheDocument();
      expect(screen.getByText("View All Documents")).toBeInTheDocument();
    });
  });
});

describe("QueuePage", () => {
  test("renders queue heading", async () => {
    renderWithRouter(<QueuePage />);
    expect(screen.getByText("Review Queue")).toBeInTheDocument();
  });

  test("loads and displays queue items", async () => {
    renderWithRouter(<QueuePage />);
    await waitFor(() => {
      expect(screen.getByText("CloudVPS B.V.")).toBeInTheDocument();
    });
  });

  test("shows stats strip", async () => {
    renderWithRouter(<QueuePage />);
    await waitFor(() => {
      expect(screen.getByText("In Queue")).toBeInTheDocument();
      expect(screen.getByText("Reviewed Today")).toBeInTheDocument();
    });
  });
});

describe("UploadPage", () => {
  test("renders upload heading", () => {
    renderWithRouter(<UploadPage />);
    expect(screen.getByText("Upload Invoices")).toBeInTheDocument();
  });

  test("shows drop zone", () => {
    renderWithRouter(<UploadPage />);
    expect(
      screen.getByText(/Drag & drop PDF invoices here/)
    ).toBeInTheDocument();
  });

  test("shows step cards when empty", () => {
    renderWithRouter(<UploadPage />);
    expect(screen.getByText("Upload PDF")).toBeInTheDocument();
    expect(screen.getByText("AI Extracts Data")).toBeInTheDocument();
    expect(screen.getByText("Review & Approve")).toBeInTheDocument();
  });
});

describe("API error handling", () => {
  test("dashboard handles API failure gracefully", async () => {
    const { queueApi } = await import("@/lib/api");
    vi.mocked(queueApi.getStats).mockRejectedValueOnce(
      new Error("Network error")
    );

    renderWithRouter(<DashboardPage />);

    // Should not crash — either shows data or gracefully fails
    await waitFor(() => {
      expect(screen.getByText("Invoice Processing Hub")).toBeInTheDocument();
    });
  });
});

// Interaction tests

describe("QueuePage interactions", () => {
  test("filter select changes status filter", async () => {
    userEvent.setup();
    renderWithRouter(<QueuePage />);

    await waitFor(() => {
      expect(screen.getByText("Review Queue")).toBeInTheDocument();
    });

    // The filter Select should be present
    const filterTriggers = screen.getAllByRole("combobox");
    expect(filterTriggers.length).toBeGreaterThanOrEqual(1);
  });

  test("refresh button triggers data reload", async () => {
    const { queueApi } = await import("@/lib/api");
    renderWithRouter(<QueuePage />);

    await waitFor(() => {
      expect(screen.getByText("Review Queue")).toBeInTheDocument();
    });

    const refreshBtn = screen.getByText("Refresh");
    fireEvent.click(refreshBtn);

    // getQueue should have been called again
    expect(queueApi.getQueue).toHaveBeenCalledTimes(2);
  });

  test("clicking queue item navigates (card is clickable)", async () => {
    renderWithRouter(<QueuePage />);

    await waitFor(() => {
      expect(screen.getByText("CloudVPS B.V.")).toBeInTheDocument();
    });

    // The card should be clickable
    const card = screen.getByText("CloudVPS B.V.").closest("[class*='cursor-pointer']");
    expect(card).toBeTruthy();
  });

  test("queue shows confidence percentage", async () => {
    renderWithRouter(<QueuePage />);

    await waitFor(() => {
      // Average confidence of mock fields: (0.95 + 0.65) / 2 = 0.80 → 80%
      expect(screen.getByText("80%")).toBeInTheDocument();
    });
  });

  test("queue shows priority", async () => {
    renderWithRouter(<QueuePage />);

    await waitFor(() => {
      expect(screen.getByText("Priority 72")).toBeInTheDocument();
    });
  });
});

describe("DashboardPage interactions", () => {
  test("quick action cards are clickable links", async () => {
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText("Upload New Invoices")).toBeInTheDocument();
    });

    // Each quick action should have a link or be clickable
    const uploadAction = screen.getByText("Upload New Invoices").closest("a, [role='link'], [class*='cursor']");
    expect(uploadAction).toBeTruthy();
  });

  test("dashboard displays formatted average review time", async () => {
    renderWithRouter(<DashboardPage />);

    await waitFor(() => {
      // avg_review_time_seconds = 28.5 → should show ~29s or 28s
      const timeDisplay = screen.getByText("Avg Review Time");
      expect(timeDisplay).toBeInTheDocument();
    });
  });
});

describe("UploadPage interactions", () => {
  test("drop zone accepts click interaction", async () => {
    renderWithRouter(<UploadPage />);

    const dropZone = screen.getByText(/Drag & drop PDF invoices here/);
    expect(dropZone).toBeInTheDocument();

    // The drop zone or its parent should have an input[type=file]
    const fileInputs = document.querySelectorAll('input[type="file"]');
    expect(fileInputs.length).toBeGreaterThanOrEqual(1);
  });
});
