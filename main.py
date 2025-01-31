from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import Dict, List, Optional
import json
import uuid
import time
import google.generativeai as genai
from dotenv import load_dotenv
import os
from sqlalchemy import create_engine, Column, String, Text, Integer
from sqlalchemy.orm import declarative_base, sessionmaker
from pydantic import BaseModel, field_validator

load_dotenv()

if not os.getenv("GEMINI_APIKEY") or os.getenv("GEMINI_APIKEY") == "":
    if not os.path.exists(".env"):
        raise ValueError("Please create .env file")
    else:
        raise ValueError("Please set GEMINI_APIKEY in .env file")

genai.configure(api_key=os.getenv("GEMINI_APIKEY"))

DATABASE_URL = "sqlite:///./memo.db"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Memo(Base):
    __tablename__ = "memos"
    id = Column(String, primary_key=True)
    content = Column(Text, default="")
    updated_at = Column(Integer, default=0)
    created_at = Column(Integer, default=0)
    created_user = Column(String, default="")

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
async def get(request: Request):
    response = FileResponse("static/panel.html")
    userid = request.cookies.get("userid")
    if not userid:
        response.set_cookie(
            key="userid",
            value=str(uuid.uuid4())[:6],
            max_age=60*60*24*365,
        )
    return response

@app.get("/edit")
async def get(request: Request):
    response = FileResponse("static/editor/main.html")
    userid = request.cookies.get("userid")
    if not userid:
        response.set_cookie(
            key="userid",
            value=str(uuid.uuid4())[:6],
            max_age=60*60*24*365,
        )
    return response

@app.get("/edit/new")
async def create_memo(request: Request, response: Response):
    memo_id = str(uuid.uuid4())
    current_time = int(time.time())

    userid = request.cookies.get("userid")
    if not userid:
        response.set_cookie(
            key="userid",
            value= str(uuid.uuid4())[:6],
            max_age=60*60*24*365,
        )

    db = SessionLocal()
    try:
        new_memo = Memo(
            id=memo_id,
            created_at=current_time,
            updated_at=current_time,
            created_user=userid
        )
        db.add(new_memo)
        db.commit()
    finally:
        db.close()
    return {"memo_id": memo_id}

class SuggestItem(BaseModel):
    count: int # 文字位置（UTF-16コードユニット)
    before: str
    after: str

@app.websocket("/ws/{memo_id}")
async def websocket_endpoint(websocket: WebSocket, memo_id: str):
    cookies = websocket.cookies
    user_id = cookies.get("userid")

    if not user_id:
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "status":"nocookie"
        })
        await websocket.close()
        return

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
                    memo.updated_at = int(time.time())
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
                                変更点がない場合は対象の文章を含まずに、"nochange"とだけ出力して下さい。
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
                                変更点がない場合は対象の文章を含まずに、"nochange"とだけ出力して下さい。
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
                                元の文章は絶対に変更しないで下さい。
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
                            if response.text == "nochange" or input_text == response.text:
                                await manager.broadcast(json.dumps({
                                    "type": "ai",
                                    "status": "nochange",
                                }), memo_id, None)
                            if message["req"] == "continued":
                                new_content = memo_content + response.text
                                memo.content = new_content
                                memo.updated_at = int(time.time())
                                db.commit()
                                await manager.broadcast(json.dumps({
                                    "type": "ai",
                                    "status": "success",
                                    "content": new_content
                                }), memo_id, None)
                            else:
                                memo.content = response.text
                                memo.updated_at = int(time.time())
                                db.commit()
                                await manager.broadcast(json.dumps({
                                    "type": "ai",
                                    "status": "success",
                                    "content": response.text
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

            elif message["type"] == "spellcheck":
                    memo_content = message["content"]
                    suggestions = await check_spelling(memo_content) # Gemini API を使用したスペルチェック関数を使用
                    await websocket.send_json({
                        "type": "suggest",
                        "suggestions": suggestions
                    })


    except WebSocketDisconnect:
        await manager.disconnect(memo_id, user_id)
        db = SessionLocal()
        memo = db.query(Memo).filter(Memo.id == memo_id).first()
        if memo and hasattr(memo, "cursors"):
            memo.cursors.pop(user_id, None)
        db.close()

@app.post("/api/memo/{memo_id}/delete")
async def delete_memo(memo_id: str):
    db = SessionLocal()
    try:
        memo = db.query(Memo).filter(Memo.id == memo_id).first()
        if memo is None:
            raise HTTPException(status_code=404, detail="Memo not found")

        if memo_id in manager.active_connections:
            connections = manager.active_connections[memo_id].copy()
            for user_id in connections:
                await manager.disconnect(memo_id, user_id)

        db.delete(memo)
        db.commit()

        return JSONResponse(
            content={"status": "success", "message": "ok"},
            status_code=200
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

class MemoIdsRequest(BaseModel):
    memo_ids: List[str]

    @field_validator('memo_ids')
    @classmethod
    def validate_memo_ids(cls, v: List[str]) -> List[str]:
        if len(v) > 150:
            raise ValueError('Max request size is 150')
        if len(v) == 0:
            raise ValueError('Min request size is 1')
        return v

@app.post("/api/memo/info")
async def get_memos_info(request: MemoIdsRequest,cookie: Request):
    db = SessionLocal()
    try:
        results: Dict[str, dict] = {}
        memos = db.query(Memo).filter(Memo.id.in_(request.memo_ids)).all()
        userid = cookie.cookies.get("userid")
        if not userid:
            userid = None
        for memo in memos:
            owner = False
            if memo.created_user == userid:
                owner == True
            else:
                owner == False
            results[memo.id] = {
                "memo_id": memo.id,
                "created_at": memo.created_at,
                "updated_at": memo.updated_at,
                "owner":owner,
                "status": "found"
            }

        for memo_id in request.memo_ids:
            if memo_id not in results:
                results[memo_id] = {
                    "memo_id": memo_id,
                    "status": "not_found"
                }

        return {
            "memos": results
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

async def check_spelling(content: str) -> List[SuggestItem]:
    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = f"""
    # 命令
    与えられた文章の誤字脱字を検出し、修正候補と共にJSON形式で出力してください。
    もし誤字脱字がない場合は、空のJSON配列を返してください。

    # JSON出力形式
    [
        {{"count": 誤字脱字があった1文字目の文字数, "before": 誤字脱字の元の文章, "after": 修正後の文章}},
        ...
    ]

    # 制約条件
    - 文字数のカウントは、UTF-16コードユニットを基準とします。
    - 出力JSONは、SuggestItemモデルのリスト形式に準拠してください。
    - 修正候補は、文脈に最も適したものを1つ提示してください。
    - HTMLタグは無視して、テキスト内容のみを対象にスペルチェックを行ってください。
    - 同じ文字位置で複数の誤字脱字がある場合は、それぞれ別のSuggestItemとして出力してください。
    - count は、文章全体を0から数えたUTF-16コードユニットでの位置です。

    # 入力文章
    {content}
    """

    try:
        response = await model.generate_content_async(prompt)
        json_str = response.text.strip()

        if not json_str or json_str == "[]":
            return []

        if json_str == '"nochange"' or json_str == "'nochange'":
            return []

        try:
            suggestions_json = json.loads(json_str)
            suggestions = [SuggestItem(**item) for item in suggestions_json]
            return suggestions
        except json.JSONDecodeError as e:
            return []

    except Exception as e:
        print(f"Error: {e}")
        return []

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="localhost", port=8081)