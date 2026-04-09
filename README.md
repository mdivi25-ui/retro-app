# RetroBoard — Agile Retrospective App

A real-time, hide-and-reveal retrospective tool for distributed Scrum teams.

## Features

| Feature | Status |
|---|---|
| Session creation with custom sections & timeboxes | ✅ |
| Shareable session links (no login required) | ✅ |
| Private card input (hidden from others) | ✅ |
| Simultaneous reveal with animation | ✅ |
| Real-time sync via WebSocket | ✅ |
| Column board layout with drag-and-drop grouping | ✅ |
| Dot voting (3 votes/participant) | ✅ |
| Facilitator controls (timer, reveal, lock, next section) | ✅ |
| Card highlighting by facilitator | ✅ |
| Comments on cards after reveal | ✅ |
| Anonymous mode | ✅ |
| ROTI (Return on Time Invested) rating | ✅ |
| Text export / summary | ✅ |
| Mobile-friendly responsive layout | ✅ |

---

## Quickstart

### 1. Clone / copy the project

```
retro_app/
├── server.py
├── requirements.txt
├── static/
│   └── index.html
└── README.md
```

### 2. Install dependencies

```bash
cd retro_app
pip install -r requirements.txt
```

### 3. Run the server

```bash
python server.py
```

Or with uvicorn directly:

```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

### 4. Open your browser

```
http://localhost:8000
```

---

## How to Run a Session

### As Scrum Master (Facilitator)

1. Go to `http://localhost:8000`
2. Click **Create Session**
3. Set session name, sections, and timeboxes
4. Click **Create Retrospective**
5. Enter your name and check **I'm the facilitator**
6. Copy the **Share Link** from the banner and send to your team
7. Use the **Facilitator Controls** panel to:
   - ▶ **Start Timer** — begins the input phase countdown
   - ✨ **Reveal Now** — instantly reveals all cards
   - ⏸ **Pause / Resume** timer
   - 🔒 **Lock** — prevent further edits
   - → **Next Section** — move to the next section
   - 🗳 **Open Voting** — start dot voting across all sections
   - ✅ **End Session** — show ROTI and wrap up
   - ↺ **Reset** — clear board and restart

### As Participant

1. Open the shared link
2. Enter your name
3. Write your thoughts in the input cards (private until revealed!)
4. Watch for the countdown timer
5. When cards are revealed — discuss, vote, and comment
6. Rate the session with ROTI at the end

---

## Architecture

```
Browser (React)  ←→  WebSocket (/ws/{session_id}/{client_id})
                 ←→  REST API (/api/sessions/*)
                          ↓
                   FastAPI (server.py)
                          ↓
                   In-memory dict (sessions{})
```

- **Backend**: Python FastAPI with native WebSocket support
- **Frontend**: React 18 (CDN, no build step) + Babel Standalone
- **State**: Single in-memory Python dict per session — broadcast on every change
- **Real-time**: WebSocket fan-out to all connected clients

> **Note**: In-memory store is reset on server restart. For production, swap with Redis or a lightweight DB.

---

## Session Lifecycle

```
waiting → input → revealed → [next section...] → voting → done
              ↑ timer auto-reveals at 0
```

- `waiting` — facilitator hasn't started yet
- `input`   — timer running, participants writing (cards hidden)
- `revealed`— all cards visible simultaneously
- `voting`  — dot voting open across all sections
- `done`    — session over, ROTI collected, export available

---

## API Reference

| Method | Path | Description |
|---|---|---|
| POST | `/api/sessions` | Create a new session |
| GET  | `/api/sessions/{id}` | Get session state |
| POST | `/api/sessions/{id}/export` | Export session as text |
| WS   | `/ws/{session_id}/{client_id}` | Real-time WebSocket |

---

## Optional: Deploy to Production

### With Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
docker build -t retroboard .
docker run -p 8000:8000 retroboard
```

### Persistent Storage (upgrade path)

Replace the `sessions` dict in `server.py` with:
- **Redis** via `aioredis` for in-memory with persistence
- **SQLite** via `aiosqlite` for lightweight local DB
- **Firebase/Supabase** for cloud-hosted with auth

---

## Supports 5–15 participants with no authentication required.
