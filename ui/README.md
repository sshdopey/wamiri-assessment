# Frontend — Wamiri Invoices Dashboard

> A polished React 19 single-page application for uploading invoices, tracking AI extraction, reviewing results, and monitoring system health. Built with TypeScript, Vite, TailwindCSS v4, and shadcn/ui.

---

## Pages at a Glance

| Page | Route | What It Does |
|------|-------|-------------|
| **Dashboard** | `/` | Hero section with KPI cards (queue depth, reviewed today, SLA compliance), quick-action buttons |
| **Upload** | `/upload` | Drag-and-drop zone for PDFs and images, real-time processing status via polling |
| **Documents** | `/documents` | **Dual-tab view:** "Documents" (review queue with priority/SLA) + "Upload History" (all uploads with lifecycle status) |
| **Queue** | `/queue` | Priority-ranked review cards with confidence scores, SLA countdown timers, and reviewer filter |
| **Review** | `/review/:id` | Split-pane: pdf.js canvas preview on the left, editable extracted fields on the right |

---

## UX Decisions

### pdf.js Canvas Rendering (No Browser Chrome)

The Review page uses **pdfjs-dist** to render PDFs onto HTML `<canvas>` elements instead of embedding via `<object>` or `<iframe>`. This eliminates browser toolbars, download buttons, and print prompts — giving reviewers a clean, distraction-free preview. The renderer is retina-aware (`devicePixelRatio`) and auto-fits the container width.

### Cross-Tab State Sync

The Zustand store uses `zustand-sync-tabs` middleware so multiple browser tabs share the same state (reviewer identity, filters, polling). Opening a second tab inherits the current reviewer and queue filter settings.

### Reviewer Filter

The Queue page includes a dropdown to filter by assigned reviewer (`reviewer-1`, `reviewer-2`, `reviewer-3`, or All). Combined with the status filter, this gives full visibility into workload distribution. The default view shows all statuses and all reviewers.

### Dual-Tab Documents Page

The Documents page has two tabs:
- **Documents tab** — Shows items in the review queue with review statuses (pending, in review, approved, corrected, rejected). Sortable by date, SLA, or priority.
- **Upload History tab** — Shows every upload with lifecycle statuses (queued, processing, completed, failed, duplicate). Includes the "duplicate" status for re-uploaded files.

Both tabs load on mount so badge counts in the sidebar are always accurate.

### Vendor-First Display

Throughout the UI, documents display as **"Vendor — Invoice #"** instead of cryptic filenames. The backend queries extracted vendor and invoice number from the `extracted_fields` table and sets the `Content-Disposition` header accordingly.

### Real-Time Upload Tracking

A Zustand store polls the backend every 5 seconds while uploads are processing. The sidebar shows a live count badge on the Documents link, and the Upload page displays processing progress for each file.

---

## Tech Stack

| Technology | Version | Why |
|------------|---------|-----|
| **React** | 19 | Latest stable, concurrent features |
| **Vite** | 7 | Sub-second HMR, fast builds |
| **TypeScript** | 5.9 | Type safety across all components |
| **TailwindCSS** | v4 | CSS-based configuration (no `tailwind.config.js`) |
| **shadcn/ui** | new-york style | Accessible Radix UI primitives, easily customizable |
| **Zustand** | 5 | Lightweight state management for upload polling |
| **Axios** | 1.13 | HTTP client with interceptors |
| **React Router** | 7.13 | Client-side routing with nested layouts |
| **pdfjs-dist** | latest | Canvas-based PDF rendering (no browser chrome) |
| **Lucide React** | — | Consistent icon library |
| **Sonner** | — | Toast notifications (top-right positioning) |

---

## Project Structure

```
ui/src/
├── App.tsx                     # Route definitions (/, /upload, /documents, /queue, /review/:id)
├── main.tsx                    # App entrypoint with BrowserRouter
├── index.css                   # TailwindCSS v4 imports
├── pages/
│   ├── DashboardPage.tsx       # KPI cards, hero section, quick actions
│   ├── UploadPage.tsx          # Drag-drop + real-time polling tracker
│   ├── DocumentsPage.tsx       # Dual-tab: review queue + upload history
│   ├── QueuePage.tsx           # Priority-ranked review cards
│   └── ReviewPage.tsx          # Split-pane: pdf.js preview + field editor
├── components/
│   ├── PdfViewer.tsx           # pdfjs-dist canvas renderer (retina-aware)
│   ├── layout/
│   │   └── AppLayout.tsx       # Sidebar with "Wamiri Invoices" branding + badge
│   └── ui/                     # 16 shadcn/ui components (button, card, table, etc.)
├── lib/
│   ├── api.ts                  # Axios client: documentApi + queueApi
│   ├── store.ts                # Application state
│   ├── types.ts                # TypeScript types, DocumentTrackingStatus, display helpers
│   ├── upload-tracking.ts      # Zustand store for upload polling
│   └── utils.ts                # Tailwind merge utility (cn)
└── tests/
    ├── setup.ts                # Testing Library + jsdom config
    └── Dashboard.test.tsx      # 11 tests across 4 suites
```

---

## Testing

```bash
npm test
```

**11 tests** across 4 describe blocks:

| Suite | # Tests | What It Covers |
|-------|---------|----------------|
| **DashboardPage** | 4 | Heading renders, KPI cards present, stat values display, quick actions work |
| **QueuePage** | 3 | Heading renders, vendor display names shown, stats strip present |
| **UploadPage** | 3 | Heading renders, drop zone visible, step cards displayed |
| **API error handling** | 1 | Dashboard handles API failures gracefully |

All tests use `MemoryRouter` for routing and `vi.mock` for API mocking.

---

## Commands

| Command | Description |
|---------|-------------|
| `npm run dev` | Vite dev server with HMR (http://localhost:5173) |
| `npm run build` | TypeScript check + production build |
| `npm run lint` | ESLint |
| `npm test` | Vitest (11 tests) |
| `npm run test:watch` | Vitest in watch mode |

---

## Docker

The `Dockerfile` runs a two-stage build: `npm run build` produces static assets, then nginx serves them on port 80 (mapped to 5173 by `docker-compose.yml`). The nginx config proxies `/api` requests to the backend container.
