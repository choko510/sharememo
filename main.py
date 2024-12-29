from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import Dict, Set, List
import json
import uuid
import google.generativeai as genai
from dotenv import load_dotenv
import os
from sqlalchemy import create_engine, Column, String, Text
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

genai.configure(api_key=os.getenv("GEMINI_APIKEY"))

DATABASE_URL = "sqlite:///./memo.db"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Memo(Base):
    __tablename__ = "memos"
    id = Column(String, primary_key=True)
    content = Column(Text, default="")

Base.metadata.create_all(bind=engine)

app = FastAPI()

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
        await self.broadcast_users(memo_id, user_id, "join")

    async def disconnect(self, memo_id: str, user_id: str):
        if memo_id in self.active_connections:
            self.active_connections[memo_id].pop(user_id, None)
            self.user_names.pop(user_id, None)
            self.user_colors.pop(user_id, None)
            if self.active_connections[memo_id]:
                await self.broadcast_users(memo_id, user_id, "leave")
            else:
                del self.active_connections[memo_id]

    async def broadcast_users(self, memo_id: str, user_id:str , status:str):
        if memo_id in self.active_connections:
            users = {
                uid: {
                    "name": self.user_names[uid],
                    "color": self.user_colors[uid]
                }
                for uid in self.active_connections[memo_id].keys()
            }
            message = json.dumps({"type": "users", "users": users, "status": status, "user_id": user_id})
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
    db = SessionLocal()
    try:
        new_memo = Memo(id=memo_id)
        db.add(new_memo)
        db.commit()
    finally:
        db.close()
    return {"memo_id": memo_id}

@app.websocket("/ws/{memo_id}")
async def websocket_endpoint(websocket: WebSocket, memo_id: str):
    user_id = str(uuid.uuid4())
    user_name = f"User-{user_id[:6]}"
    
    await manager.connect(websocket, memo_id, user_id, user_name)
    
    try:
        db = SessionLocal()
        memo = db.query(Memo).filter(Memo.id == memo_id).first()
        if memo:
            await websocket.send_json({
                "type": "init",
                "content": memo.content,
                "myid": user_id,
            })
        else:
            await websocket.send_json({
                "type": "error",
                "status":"notfound"
            })
        db.close()
        
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)

            if message["type"] == "content":
                db = SessionLocal()
                memo = db.query(Memo).filter(Memo.id == memo_id).first()
                if memo:
                    memo.content = message["content"]
                    db.commit()
                    await manager.broadcast(data, memo_id, user_id)
                db.close()
            
            elif message["type"] == "cursor":
                db = SessionLocal()
                memo = db.query(Memo).filter(Memo.id == memo_id).first()
                if memo:
                    if not hasattr(memo, "cursors"):
                        memo.cursors = {}
                    memo.cursors[user_id] = {
                        "position": message["position"],
                        "name": user_name,
                        "color": manager.user_colors[user_id]
                    }
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
                db.close()
            
            elif message["type"] == "ai":
                db = SessionLocal()
                memo = db.query(Memo).filter(Memo.id == memo_id).first()
                if memo:
                    if message["status"] == "gen":
                        memo_content = memo.content
                        await manager.broadcast(json.dumps({
                                "type": "ai",
                                "status": "stop",
                                "name":user_id
                            }), memo_id, user_id)
                        if message["req"] == "rewrite":
                            input_text = f'''
                            #命令
                                文章を校正してください。
                                誤字脱字や不自然な表現も修正してください。
                            #制約条件
                                元の文章の意図や構造を保ったまま修正してください。
                                大きく改行の位置関係を変更しないでください。
                                出力形式は、添削後の文章のみとしてください。
                                修正文章に含まれるHTMLタグは、基本的にそのまま残してください。ただし、明確に不要な場合のみ削除してください。
                                改行は必ず<br>タグを使用してください。
                            #修正対象文章
                                {memo_content}
                            '''
                        elif message["req"] == "makeword":
                            input_text = f'''
                            #命令
                                文章の量を増やして下さい。
                            #制約条件
                                元の文章の意図や構造を保ったまま増やしてください。
                                大きく改行の位置関係を変更しないでください。
                                出力形式は、増やした後の文章のみとしてください。
                                修正文章に含まれるHTMLタグは、基本的にそのまま残してください。ただし、明確に不要な場合のみ削除してください。
                                改行は必ず<br>タグを使用してください。
                            #対象文章 
                                {memo_content}
                            '''
                        elif message["req"] == "continued":
                            input_text = f'''
                            #命令
                                文章が途切れている所から、自然な風に続きを書いて下さい。
                            #制約条件
                                元の文章の意図や構造を保ったまま続きを書いてください。
                                大きく改行の位置関係を変更しないでください。
                                出力形式は、続きの文章のみを出力してください。
                                修正文章に含まれるHTMLタグは、基本的にそのまま残してください。ただし、明確に不要な場合のみ削除してください。
                                改行は必ず<br>タグを使用してください。
                            #対象文章
                                {memo_content}
                            '''
                        else:
                            await manager.broadcast(json.dumps({
                                "type": "ai",
                                "status": "failure",
                            }), memo_id, None)

                        try:
                            model = genai.GenerativeModel("gemini-1.5-flash")
                            response = model.generate_content(input_text)
                            if message["req"] == "continued":
                                await manager.broadcast(json.dumps({
                                    "type": "ai",
                                    "status": "success",
                                    "content":memo_content+response.text
                                }), memo_id, None)
                            else:
                                await manager.broadcast(json.dumps({
                                    "type": "ai",
                                    "status": "success",
                                    "content":response.text
                                }), memo_id, None)
                        except:
                            await manager.broadcast(json.dumps({
                                "type": "ai",
                                "status": "failure",
                            }), memo_id, None)
                    elif message["status"] == "accept":
                        await manager.broadcast(json.dumps({
                            "type": "ai",
                            "status": "accept",
                        }), memo_id, user_id)
                    elif message["status"] == "cancel":
                        await manager.broadcast(json.dumps({
                            "type": "ai",
                            "status": "cancel",
                        }), memo_id, user_id)
                db.close()

    except WebSocketDisconnect:
        await manager.disconnect(memo_id, user_id)
        db = SessionLocal()
        memo = db.query(Memo).filter(Memo.id == memo_id).first()
        if memo and hasattr(memo, "cursors"):
            memo.cursors.pop(user_id, None)
        db.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="localhost", port=8080)