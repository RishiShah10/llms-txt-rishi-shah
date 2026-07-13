# Frontend — llms.txt Generator

Next.js frontend with WebSocket integration for real-time crawl monitoring.

## Setup

```bash
cd frontend
cp .env.local.example .env.local
npm install
```

`.env.local` sets `NEXT_PUBLIC_API_URL` — the base URL of the backend API
(defaults to `http://localhost:8000`). The WebSocket URL is derived from it.

## Development

```bash
npm run dev
```

Open http://localhost:3000

## Build

```bash
npm run build
npm start
```

## Features

- Real-time WebSocket log streaming of the crawl pipeline
- Configurable crawl parameters (URL, max pages, crawl linked pages, AI-enhanced
  descriptions, unblock protected sites, honor robots.txt, auto-update)
- Download generated llms.txt
- Copy to clipboard
- View hosted URL (if storage configured)
- Re-check a generated site for changes
- Dark theme UI
- Responsive design
