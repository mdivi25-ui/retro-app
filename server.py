"""
Agile Retrospective App - FastAPI Backend
Real-time WebSocket server with in-memory session store
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, List, Optional, Any
import uuid
import json
import asyncio
import time
import os

app = FastAPI(title="Retro App")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── In-Memory Store ──────────────────────────────────────────────────────────

sessions: Dict[str, dict] = {}
# session_id -> { client_id -> WebSocket }
connections: Dict[str, Dict[str, WebSocket]] = {}
# session_id -> asyncio.Task (timer countdown)
timer_tasks: Dict[str, asyncio.Task] = {}


def make_session(name: str, sections: List[dict], anonymous: bool) -> dict:
    return {
        "id": str(uuid.uuid4())[:8],
        "name": name,
        "facilitatorId": None,
        "anonymous": anonymous,
        "sections": sections,
        "currentSectionIndex": 0,
        "phase": "waiting",          # waiting | input | revealed | voting | done
        "timerRunning": False,
        "timerEnd": None,            # unix ms timestamp
        "timerPausedRemaining": None,
        "participants": {},          # id -> name
        "cards": {},                 # cardId -> card dict
        "locked": False,
        "roti": {},                  # participantId -> 1-5
    }


def make_card(section_id: str, participant_id: str, text: str) -> dict:
    return {
        "id": str(uuid.uuid4())[:8],
        "sectionId": section_id,
        "participantId": participant_id,
        "text": text,
        "votes": [],       # list of participant_ids
        "highlighted": False,
        "comments": [],    # list of {id, authorId, text, ts}
        "order": int(time.time() * 1000),
    }


# ─── Broadcast Helpers ────────────────────────────────────────────────────────

async def broadcast(session_id: str, message: dict):
    """Send message to all connected clients in a session."""
    dead = []
    for client_id, ws in list(connections.get(session_id, {}).items()):
        try:
            await ws.send_text(json.dumps(message))
        except Exception:
            dead.append(client_id)
    for cid in dead:
        connections[session_id].pop(cid, None)


def session_state(session_id: str) -> dict:
    s = sessions[session_id]
    return {"type": "session_update", "session": s}


async def push_state(session_id: str):
    await broadcast(session_id, session_state(session_id))


# ─── Timer Logic ──────────────────────────────────────────────────────────────

async def run_timer(session_id: str):
    """Background task: count down and auto-reveal when timer hits zero."""
    try:
        while True:
            await asyncio.sleep(0.5)
            s = sessions.get(session_id)
            if not s or not s["timerRunning"]:
                break
            remaining = s["timerEnd"] - int(time.time() * 1000)
            if remaining <= 0:
                s["timerRunning"] = False
                s["timerEnd"] = None
                # Auto-reveal
                if s["phase"] == "input":
                    s["phase"] = "revealed"
                await push_state(session_id)
                break
    except asyncio.CancelledError:
        pass


def cancel_timer(session_id: str):
    task = timer_tasks.pop(session_id, None)
    if task and not task.done():
        task.cancel()


# ─── REST Endpoints ───────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    name: str
    sections: List[dict]
    anonymous: bool = False


class JoinRequest(BaseModel):
    participantName: str


@app.post("/api/sessions")
async def create_session(req: CreateSessionRequest):
    s = make_session(req.name, req.sections, req.anonymous)
    sessions[s["id"]] = s
    connections[s["id"]] = {}
    return {"sessionId": s["id"]}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    if session_id not in sessions:
        raise HTTPException(404, "Session not found")
    return sessions[session_id]


@app.post("/api/sessions/{session_id}/export")
async def export_session(session_id: str):
    if session_id not in sessions:
        raise HTTPException(404, "Session not found")
    s = sessions[session_id]
    lines = [f"# Retrospective: {s['name']}", ""]
    for sec in s["sections"]:
        lines.append(f"## {sec['name']}")
        sec_cards = [c for c in s["cards"].values() if c["sectionId"] == sec["id"]]
        sec_cards.sort(key=lambda c: -len(c["votes"]))
        for card in sec_cards:
            author = "Anonymous" if s["anonymous"] else s["participants"].get(card["participantId"], "Unknown")
            votes = len(card["votes"])
            star = " ⭐" if card["highlighted"] else ""
            lines.append(f"- [{votes}🗳] {card['text']}  (by {author}){star}")
            for comment in card.get("comments", []):
                lines.append(f"  💬 {comment['text']}")
        lines.append("")
    # ROTI
    if s["roti"]:
        avg = sum(s["roti"].values()) / len(s["roti"])
        lines.append(f"## ROTI (Return on Time Invested)")
        lines.append(f"Average rating: {avg:.1f}/5 from {len(s['roti'])} participants")
    return {"text": "\n".join(lines)}


# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws/{session_id}/{client_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str, client_id: str):
    if session_id not in sessions:
        await websocket.close(code=4004)
        return

    await websocket.accept()
    connections.setdefault(session_id, {})[client_id] = websocket

    # Send current state immediately
    await websocket.send_text(json.dumps(session_state(session_id)))

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            await handle_message(session_id, client_id, msg)
    except WebSocketDisconnect:
        connections[session_id].pop(client_id, None)
        # Remove participant if they disconnect (optional: keep ghost)
        s = sessions.get(session_id)
        if s and client_id in s["participants"]:
            pass  # keep them in participants list so cards remain
        await push_state(session_id)


async def handle_message(session_id: str, client_id: str, msg: dict):
    s = sessions[session_id]
    t = msg.get("type")

    # ── Participant join ──────────────────────────────────────────────────────
    if t == "join":
        name = msg.get("name", "Anonymous")
        is_fac = msg.get("isFacilitator", False)
        s["participants"][client_id] = name
        if is_fac and s["facilitatorId"] is None:
            s["facilitatorId"] = client_id
        await push_state(session_id)

    # ── Add / update card ─────────────────────────────────────────────────────
    elif t == "add_card":
        if s["locked"]:
            return
        section_id = msg["sectionId"]
        text = msg.get("text", "").strip()
        if not text:
            return
        card = make_card(section_id, client_id, text)
        s["cards"][card["id"]] = card
        await push_state(session_id)

    elif t == "update_card":
        if s["locked"]:
            return
        card_id = msg["cardId"]
        if card_id in s["cards"] and s["cards"][card_id]["participantId"] == client_id:
            s["cards"][card_id]["text"] = msg.get("text", "").strip()
            await push_state(session_id)

    elif t == "delete_card":
        card_id = msg["cardId"]
        card = s["cards"].get(card_id)
        if card and (card["participantId"] == client_id or client_id == s["facilitatorId"]):
            del s["cards"][card_id]
            await push_state(session_id)

    # ── Voting ────────────────────────────────────────────────────────────────
    elif t == "vote":
        card_id = msg["cardId"]
        if card_id not in s["cards"]:
            return
        max_votes = 3
        # Count current votes by this participant
        current_votes = sum(1 for c in s["cards"].values() if client_id in c["votes"])
        card = s["cards"][card_id]
        if client_id in card["votes"]:
            card["votes"].remove(client_id)   # toggle off
        elif current_votes < max_votes:
            card["votes"].append(client_id)   # toggle on
        await push_state(session_id)

    # ── Comments ──────────────────────────────────────────────────────────────
    elif t == "add_comment":
        card_id = msg["cardId"]
        text = msg.get("text", "").strip()
        if card_id in s["cards"] and text:
            comment = {
                "id": str(uuid.uuid4())[:8],
                "authorId": client_id,
                "text": text,
                "ts": int(time.time() * 1000),
            }
            s["cards"][card_id]["comments"].append(comment)
            await push_state(session_id)

    # ── ROTI ──────────────────────────────────────────────────────────────────
    elif t == "roti":
        rating = int(msg.get("rating", 3))
        s["roti"][client_id] = max(1, min(5, rating))
        await push_state(session_id)

    # ── Facilitator controls ──────────────────────────────────────────────────
    elif t == "fac_start_timer":
        if client_id != s["facilitatorId"]:
            return
        section = s["sections"][s["currentSectionIndex"]]
        duration_ms = section["timeboxMinutes"] * 60 * 1000
        s["timerRunning"] = True
        s["timerEnd"] = int(time.time() * 1000) + duration_ms
        s["timerPausedRemaining"] = None
        s["phase"] = "input"
        s["locked"] = False
        cancel_timer(session_id)
        task = asyncio.create_task(run_timer(session_id))
        timer_tasks[session_id] = task
        await push_state(session_id)

    elif t == "fac_pause_timer":
        if client_id != s["facilitatorId"]:
            return
        if s["timerRunning"] and s["timerEnd"]:
            remaining = s["timerEnd"] - int(time.time() * 1000)
            s["timerPausedRemaining"] = max(0, remaining)
        s["timerRunning"] = False
        s["timerEnd"] = None
        cancel_timer(session_id)
        await push_state(session_id)

    elif t == "fac_resume_timer":
        if client_id != s["facilitatorId"]:
            return
        remaining = s["timerPausedRemaining"] or 0
        s["timerRunning"] = True
        s["timerEnd"] = int(time.time() * 1000) + remaining
        s["timerPausedRemaining"] = None
        cancel_timer(session_id)
        task = asyncio.create_task(run_timer(session_id))
        timer_tasks[session_id] = task
        await push_state(session_id)

    elif t == "fac_reveal":
        if client_id != s["facilitatorId"]:
            return
        s["phase"] = "revealed"
        s["timerRunning"] = False
        cancel_timer(session_id)
        await push_state(session_id)

    elif t == "fac_next_section":
        if client_id != s["facilitatorId"]:
            return
        s["phase"] = "revealed"
        cancel_timer(session_id)
        s["timerRunning"] = False
        s["timerEnd"] = None
        next_idx = s["currentSectionIndex"] + 1
        if next_idx < len(s["sections"]):
            s["currentSectionIndex"] = next_idx
            s["phase"] = "waiting"
        else:
            s["phase"] = "voting"
        await push_state(session_id)

    elif t == "fac_start_voting":
        if client_id != s["facilitatorId"]:
            return
        s["phase"] = "voting"
        await push_state(session_id)

    elif t == "fac_end_session":
        if client_id != s["facilitatorId"]:
            return
        s["phase"] = "done"
        await push_state(session_id)

    elif t == "fac_toggle_lock":
        if client_id != s["facilitatorId"]:
            return
        s["locked"] = not s["locked"]
        await push_state(session_id)

    elif t == "fac_toggle_highlight":
        if client_id != s["facilitatorId"]:
            return
        card_id = msg["cardId"]
        if card_id in s["cards"]:
            s["cards"][card_id]["highlighted"] = not s["cards"][card_id]["highlighted"]
            await push_state(session_id)

    elif t == "fac_reset":
        if client_id != s["facilitatorId"]:
            return
        cancel_timer(session_id)
        s["cards"] = {}
        s["currentSectionIndex"] = 0
        s["phase"] = "waiting"
        s["timerRunning"] = False
        s["timerEnd"] = None
        s["roti"] = {}
        await push_state(session_id)


# ─── Static files & SPA catch-all ─────────────────────────────────────────────

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    index = os.path.join(static_dir, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return HTMLResponse("<h1>Static files not found. Place index.html in ./static/</h1>", 404)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
