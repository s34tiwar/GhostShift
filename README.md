# GhostShift

## Frontend activity view

The Next.js control center is in `frontend/`. Set `NEXT_PUBLIC_API_BASE_URL` in `frontend/.env.local` to the FastAPI address (see `frontend/.env.local.example`), then run `npm run dev` from `frontend/`. The API needs `FACTORY_BASE_URL`, `BACKEND_MONGODB_URI`, `MONGODB_DB`, and `CORS_ORIGINS` with the frontend origin. The factory and worker run as separate services. Production code also accepts the legacy `MONGODB_URI` name for local compatibility.

With the API URL set, the page polls `/api/machines` and the selected run's `/api/runs/{id}`, `/audit`, and `/actions` once per second. The highlighted backend step follows those responses, and a machine is shown as reconciled only after the run is complete and its ledger effect is verified. Without the API URL, the page remains a clearly labeled browser preview.
