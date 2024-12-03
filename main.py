from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import Dict, Set, List
import json
import uuid
import google.generativeai as genai
from dotenv import load_dotenv
import os

load_dotenv()

genai.configure(api_key=os.getenv("GEMINI_APIKEY")) 

app = FastAPI()

memos: Dict[str, Dict] = {}
cursors: Dict[str, Dict] = {}


COLORLIST = [   "#E69AAF", "#EEB598", "#E6D38C", "#BFE095", "#93D6CE", "#94BBE6", "#B297CF", "#E0A1B1", "#EBC297", "#E6DBB1",
                "#C3DE0B", "#95CCBF", "#A6C2E6", "#BFA1E6", "#E2B4C7", "#E6CCAC", "#DED9A6", "#B4CFA2", "#A2CBBE", "#E6D9C2",
                "#AFC2E0", "#C3AEE0", "#E0B7C7", "#E6C7B4", "#E6DEB7", "#CCDBBE", "#B7D1CA", "#C3CCE6", "#CEBEE0", "#E0BECF"
            ]

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Dict[str, WebSocket]] = {}
        self.user_names: Dict[str, str] = {}
        self.user_colors: Dict[str, str] = {}

    async def connect(self, websocket: WebSocket, memo_id: str, user_id: str, user_name: str):
        await websocket.accept()
        if memo_id not in self.active_connections:
            self.active_connections[memo_id] = {}
        self.active_connections[memo_id][user_id] = websocket
        self.user_names[user_id] = user_name
        self.user_colors[user_id] = COLORLIST[len(self.user_colors) % len(COLORLIST)]
        await self.broadcast_users(memo_id)

    def disconnect(self, memo_id: str, user_id: str):
        if memo_id in self.active_connections:
            self.active_connections[memo_id].pop(user_id, None)
            self.user_names.pop(user_id, None)
            self.user_colors.pop(user_id, None)
            if not self.active_connections[memo_id]:
                del self.active_connections[memo_id]

    async def broadcast_users(self, memo_id: str):
        if memo_id in self.active_connections:
            users = {
                uid: {
                    "name": self.user_names[uid],
                    "color": self.user_colors[uid]
                }
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
        else:
            await websocket.send_json({
                "type": "error",
                "status":"notfound"
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
                        "name": user_name,
                        "color": manager.user_colors[user_id]
                    }
                    memos[memo_id]["cursors"] = cursors
                    await manager.broadcast(
                        json.dumps({
                            "type": "cursor",
                            "user_id": user_id,
                            "position": message["position"],
                            "name": user_name,
                            "color": manager.user_colors[user_id]
                        }),
                        memo_id,
                        user_id
                    )
            
            elif message["type"] == "rewrite":
                if memo_id in memos:
                    if message["status"] == "gen":
                        memo = memos[memo_id]["content"]
                        await manager.broadcast(json.dumps({
                                "type": "rewrite",
                                "status": "stop",
                                "name":user_id
                            }), memo_id, user_id)

                        input_text = f'''
                        #命令
                            文章を校正してください。
                            誤字脱字や不自然な表現も修正してください。
                        #制約条件
                            元の文章の意図や構造を保ったまま修正してください。
                            改行の位置関係を変更しないでください。
                            出力形式は、添削後の文章のみとしてください。
                            修正文章に含まれるHTMLタグは、基本的にそのまま残してください。ただし、明確に不要な場合のみ削除してください。
                            改行は必ず <br> タグを使用してください。
                        #修正対象文章
                            {memo}
                        '''

                        try:
                            model = genai.GenerativeModel("gemini-1.5-flash")
                            response = model.generate_content(input_text)
                            print(response.text)
                            await manager.broadcast(json.dumps({
                                "type": "rewrite",
                                "status": "success",
                                "content":response.text
                            }), memo_id, None)
                        except:
                            await manager.broadcast(json.dumps({
                                "type": "rewrite",
                                "status": "failure",
                            }), memo_id, None)
                    elif message["status"] == "accept":
                        await manager.broadcast(json.dumps({
                            "type": "rewrite",
                            "status": "accept",
                        }), memo_id, user_id)
                    elif message["status"] == "cancel":
                        await manager.broadcast(json.dumps({
                            "type": "rewrite",
                            "status": "cancel",
                        }), memo_id, user_id)

    except WebSocketDisconnect:
        manager.disconnect(memo_id, user_id)
        if memo_id in memos and "cursors" in memos[memo_id]:
            memos[memo_id]["cursors"].pop(user_id, None)
        await manager.broadcast_users(memo_id)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="localhost", port=8080)