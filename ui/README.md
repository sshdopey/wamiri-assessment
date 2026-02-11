# Frontend — Invoice Processing Dashboard

React 19 single-page application for uploading, reviewing, and managing AI-extracted invoice data.

## Tech Stack

| Technology | Version | Purpose |
|------------|---------|---------|
| React | 19 | UI framework |
| Vite | 7 | Build tool & dev server |
| TypeScript | 5.9 | Type safety |
| TailwindCSS | v4 | Utility-first CSS (CSS-based config) |
| shadcn/ui | new-york style | Component library (Radix UI primitives) |
| Zustand | 5 | State management |
| Axios | 1.13 | HTTP client |
| React Router | 7.13 | Client-side routing |
| Vitest | 4 | Test runner |
| Testing Library | 16 | Component testing |
| Lucide React | — | Icon library |

## Pages

| Page | Route | Description |
|------|-------|-------------|
| Dashboard | `/` | Hero section, KPI cards (queue depth, reviewed today, SLA %), quick actions |
| Upload | `/upload` | Drag-and-drop PDF upload with real-time processing status tracking |
| Documents | `/documents` | Searchable table of all documents with vendor/invoice display names |
| Queue | `/queue` | Priority-ranked review cards with confidence scores and SLA countdowns |
| Review | `/review/:id` | Split-pane: inline PDF preview (left) + editable extracted fields (right) |

## Project Structure

```
ui/src/
├── App.tsx                    # React Router route definitions
├── main.tsx                   # App entrypoint
├── index.css                  # TailwindCSS v4 imports
├── pages/
│   ├── DashboardPage.tsx      # Invoice Processing Hub with KPIs
│   ├── UploadPage.tsx         # Drag-drop upload + Zustand tracking
│   ├── DocumentsPage.tsx      # Document table with search & status
│   ├── QueuePage.tsx          # Review queue cards
│   └── ReviewPage.tsx         # Split-pane document review
├── components/
│   ├── layout/
│   │   └── AppLayout.tsx      # Sidebar navigation with processing badge
│   └── ui/                    # shadcn/ui components (button, card, table, etc.)
├── lib/
│   ├── api.ts                 # Axios client (queueApi, documentApi)
│   ├── store.ts               # Application state
│   ├── types.ts               # TypeScript types + display helpers
│   ├── upload-tracking.ts     # Zustand store for upload polling
│   └── utils.ts               # Tailwind merge utilities
└── tests/
    ├── setup.ts               # Testing Library + jsdom setup
    └── Dashboard.test.tsx     # 11 tests (Dashboard, Queue, Upload, error handling)
```

## Key Features

- **Vendor-first display**: Documents show "Vendor — INV#" instead of cryptic filenames throughout the UI
- **Upload tracking**: Zustand store polls the API every 5 seconds while documents are being processed by Celery
- **Inline PDF preview**: Review page embeds the PDF via iframe for side-by-side comparison
- **Processing badge**: Sidebar shows a count badge on Documents while uploads are being processed

## Setup

### Development

```bash
npm install
npm run dev              # http://localhost:5173 with HMR
```

### Production (Docker)

The `Dockerfile` builds the React app and serves it via nginx on port 80, mapped to `5173` by `docker-compose.yml`.

### Commands

| Command | Description |
|---------|-------------|
| `npm run dev` | Start Vite dev server with HMR |
| `npm run build` | TypeScript check + production build |
| `npm run lint` | Run ESLint |
| `npm test` | Run Vitest (11 tests) |
| `npm run test:watch` | Run Vitest in watch mode |

## Testing

```bash
npm test
```

11 tests across 4 describe blocks:

| Suite | Tests | Coverage |
|-------|-------|----------|
| DashboardPage | 4 | Heading, KPI cards, stat values, quick actions |
| QueuePage | 3 | Heading, vendor display names, stats strip |
| UploadPage | 3 | Heading, drop zone, step cards |
| API error handling | 1 | Dashboard graceful failure |

All tests use `MemoryRouter` for routing and mock the API layer via `vi.mock`.
