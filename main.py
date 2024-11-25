from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import Dict, Set, List
import json
import uuid

app = FastAPI()

memos: Dict[str, Dict] = {}
cursors: Dict[str, Dict] = {}

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Dict[str, WebSocket]] = {}
        self.user_names: Dict[str, str] = {}

    async def connect(self, websocket: WebSocket, memo_id: str, user_id: str, user_name: str):
        await websocket.accept()
        if memo_id not in self.active_connections:
            self.active_connections[memo_id] = {}
        self.active_connections[memo_id][user_id] = websocket
        self.user_names[user_id] = user_name
        await self.broadcast_users(memo_id)

    def disconnect(self, memo_id: str, user_id: str):
        if memo_id in self.active_connections:
            self.active_connections[memo_id].pop(user_id, None)
            self.user_names.pop(user_id, None)
            if not self.active_connections[memo_id]:
                del self.active_connections[memo_id]

    async def broadcast_users(self, memo_id: str):
        if memo_id in self.active_connections:
            users = {
                uid: self.user_names[uid]
                for uid in self.active_connections[memo_id].keys()
            }
            message = json.dumps({"type": "users", "users": users})
            await self.broadcast(message, memo_id)

    async def broadcast(self, message: str, memo_id: str, exclude_user: str = None):
        if memo_id in self.active_connections:
            for user_id, connection in self.active_connections[memo_id].items():
                if exclude_user != user_id:
                    await connection.send_text(message)

manager = ConnectionManager()

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def get():
    return FileResponse("static/index.html")

@app.get("/new")
async def create_memo():
    memo_id = str(uuid.uuid4())
    memos[memo_id] = {
        "content": "",
        "cursors": {}
    }
    return {"memo_id": memo_id}

@app.websocket("/ws/{memo_id}")
async def websocket_endpoint(websocket: WebSocket, memo_id: str):
    user_id = str(uuid.uuid4())
    user_name = f"User-{user_id[:6]}"
    
    await manager.connect(websocket, memo_id, user_id, user_name)
    
    try:
        if memo_id in memos:
            await websocket.send_json({
                "type": "init",
                "content": memos[memo_id]["content"]
            })
        
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)

            if message["type"] == "content":
                if memo_id in memos:
                    memos[memo_id]["content"] = message["content"]
                    await manager.broadcast(data, memo_id, user_id)
            
            elif message["type"] == "cursor":
                if memo_id in memos:
                    cursors = memos[memo_id].get("cursors", {})
                    cursors[user_id] = {
                        "position": message["position"],
                        "name": user_name
                    }
                    memos[memo_id]["cursors"] = cursors
                    await manager.broadcast(
                        json.dumps({
                            "type": "cursor",
                            "user_id": user_id,
                            "position": message["position"],
                            "name": user_name
                        }),
                        memo_id,
                        user_id
                    )
            
    except WebSocketDisconnect:
        manager.disconnect(memo_id, user_id)
        if memo_id in memos and "cursors" in memos[memo_id]:
            memos[memo_id]["cursors"].pop(user_id, None)
        await manager.broadcast_users(memo_id)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="localhost", port=8080)