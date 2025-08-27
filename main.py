from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import Dict, List, Optional, Any
import json
import uuid
import asyncio
import time
import google.generativeai as genai
from dotenv import load_dotenv
import os
from sqlalchemy import create_engine, Column, String, Text, Integer
from sqlalchemy.orm import declarative_base, sessionmaker
from pydantic import BaseModel, field_validator
from openai import OpenAI
import loro
import base64

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
                "#AFC2E0", "#C3AEE0", "#CEBEE0", "#E0BECF"
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
            snapshot = b''
            if memo.content:
                # In the new architecture, memo.content is a binary snapshot
                snapshot = bytes(memo.content)

            await websocket.send_json({
                "type": "init",
                "snapshot": base64.b64encode(snapshot).decode('utf-8'),
                "comments": [], # TODO: Integrate comments with Loro or handle separately
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

            if message["type"] == "op":
                # The server just needs to relay the op and persist the new state.
                await manager.broadcast(data, memo_id, user_id)

                db = SessionLocal()
                try:
                    memo = db.query(Memo).filter(Memo.id == memo_id).first()
                    if memo:
                        server_doc = loro.LoroDoc()
                        if memo.content and len(memo.content) > 0:
                            server_doc.import_snapshot(bytes(memo.content))

                        op = base64.b64decode(message["op"])
                        server_doc.import_updates(op)

                        memo.content = server_doc.export_snapshot()
                        memo.updated_at = int(time.time())
                        db.commit()
                finally:
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
                try:
                    memo = db.query(Memo).filter(Memo.id == memo_id).first()
                    if memo:
                        if message["status"] == "gen":
                            memo_content = memo.content
                            char_count = message.get("char_count")
                            cursor_pos = message.get("cursor_pos", -1)

                            await manager.broadcast(json.dumps({
                                    "type": "ai",
                                    "status": "stop",
                                    "name":user_id
                                }), memo_id, user_id)

                            char_constraint = ""
                            if char_count:
                                char_constraint = f"生成する文字数は{char_count}文字程度にしてください。"

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
                                    "nochange"と出力する場合は、何も他のいかなる文字も含めないで下さい。
                                #修正対象文章
                                    {memo_content}
                                '''
                            elif message["req"] == "makeword":
                                input_text = f'''
                                #命令
                                    文章の量を増やして下さい。{char_constraint}
                                #制約条件
                                    元の文章の意図や構造を保ったまま増やしてください。
                                    できるかぎり自然な風に増やしてください。
                                    大きく改行の位置関係を変更しないでください。
                                    出力形式は、増やした後の文章のみとしてください。
                                    修正文章に含まれるHTMLタグは、基本的にそのまま残してください。ただし、明確に不要な場合のみ削除してください。
                                    改行は必ず<br>タグを使用してください。
                                    変更点がない場合は対象の文章を含まずに、"nochange"とだけ出力して下さい。
                                    "nochange"と出力する場合は、何も他のいかなる文字も含めないで下さい。
                                #対象文章
                                    {memo_content}
                                '''
                            elif message["req"] == "continued":
                                before_cursor = memo_content
                                after_cursor = ""
                                instruction = "以下の文章の続きを自然な形で生成してください。"
                                if cursor_pos != -1 and cursor_pos <= len(memo_content):
                                    before_cursor = memo_content[:cursor_pos]
                                    after_cursor = memo_content[cursor_pos:]
                                    instruction = "以下の「文章1」と「文章2」の間に入る自然な文章を生成してください。"

                                input_text = f'''
                                # 命令
                                {instruction} {char_constraint}

                                # 制約条件
                                - 元の文章の意図や構造を完全に維持してください。
                                - 「文章1」と「文章2」のテキストは絶対に変更しないでください。
                                - 出力は、生成された文章のみにしてください。余計な説明や前置きは不要です。
                                - 生成する文章には、HTMLタグを適切に使用してください（例: 改行には`<br>`）。

                                # 文章1
                                ```html
                                {before_cursor}
                                ```

                                # 文章2
                                ```html
                                {after_cursor}
                                ```
                                '''

                            elif message["req"] == "summarize":
                                input_text = f'''
                                #命令
                                    以下の文章を要約してください。
                                #制約条件
                                    出力形式は、要約後の文章のみとしてください。
                                    HTMLタグは無視してください。
                                #対象文章
                                    {memo_content}
                                '''
                            else:
                                await manager.broadcast(json.dumps({
                                    "type": "ai",
                                    "status": "failure",
                                }), memo_id, None)
                                return

                            try:
                                model = genai.GenerativeModel("gemini-1.5-flash")
                                response = await asyncio.to_thread(
                                    model.generate_content,
                                    contents=input_text
                                )

                                response_text = ""
                                try:
                                    response_text = response.text
                                except ValueError:
                                    response_text = "nochange"

                                if response_text == "nochange" or response_text == "":
                                    await manager.broadcast(json.dumps({
                                        "type": "ai",
                                        "status": "nochange",
                                    }), memo_id, None)
                                    return

                                if message["req"] == "summarize":
                                    await websocket.send_json({
                                        "type": "ai",
                                        "status": "success",
                                        "content": response_text
                                    })
                                    await manager.broadcast(json.dumps({ "type": "ai", "status": "accept" }), memo_id, None)
                                    return

                                if message["req"] == "continued":
                                    if cursor_pos != -1 and cursor_pos <= len(memo_content):
                                        before_cursor = memo_content[:cursor_pos]
                                        after_cursor = memo_content[cursor_pos:]
                                        new_content = before_cursor + response_text + after_cursor
                                    else:
                                        new_content = memo_content + response_text
                                else:
                                    new_content = response_text

                                memo.content = new_content
                                memo.updated_at = int(time.time())
                                db.commit()
                                await manager.broadcast(json.dumps({
                                    "type": "ai",
                                    "status": "success",
                                    "content": new_content
                                }), memo_id, None)
                            except Exception as e:
                                print(f"AI generation failed: {e}")
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
                finally:
                    db.close()

            elif message["type"] == "spellcheck":

                memo_content = message["content"]

                

                await websocket.send_json({
                    "type": "spellcheck",
                })

            elif message["type"] == "suggest":
                memo_content = message["content"]
                
                input_text = f'''
                    #命令
                        文章が途切れている所から、書くのをアシストして、5文字から10文字程度続きを書いてください、場合によっては長くても構いません。
                    #制約条件
                        元の文章の意図や形式を保ったまま続きを書いてください。
                        出力形式は、続きの文章のみを出力してください。
                        改行は必ず<br>タグを使用してください。
                        極力改行は避けてください。
                        文章を書くときにあなたがアシストする役割です。
            
                    #対象文章
                        {memo_content}
                    '''
                
                model = genai.GenerativeModel("gemini-1.5-flash")
                response = await asyncio.to_thread(
                    model.generate_content,
                    contents=input_text
                )
                
                response_text = ""
                try:
                    response_text = response.text
                except ValueError:
                    response_text = ""

                print(response_text)
                await websocket.send_json({
                    "type": "suggest",
                    "suggestions": response_text
                })
            
            elif message["type"] == "correction":
                memo_content = message["content"]
                
                prompt = '''
                # 命令
                文章を校正し、問題点を指摘してください。
                以下の点を重点的にチェックしてください。
                - 不自然な表現
                - 曖昧な言葉遣い
                - 論理の飛躍
                - 冗長な部分

                # 制約条件
                - 出力は必ずJSONオブジェクトのストリームとしてください。各JSONオブジェクトは独立してパース可能である必要があります。
                - 各JSONオブジェクトは以下の構造に従ってください。
                {
                    "text": "指摘箇所のテキスト",
                    "reason": "指摘理由"
                }
                - 指摘箇所ごとに、上記のJSONオブジェクトを一つずつ出力してください。
                - 指摘箇所がない場合は、何も出力しないでください。
                - HTMLタグは無視してください。

                # 対象文章
                '''
                input_text = prompt + memo_content
                
                try:
                    # ストリーミング対応のモデルをインスタンス化
                    model = genai.GenerativeModel("gemini-1.5-flash")
                    # ストリーミングモードでAPIを呼び出す
                    response_stream = await asyncio.to_thread(
                        model.generate_content,
                        contents=input_text,
                        stream=True
                    )
                    
                    # ストリームで受け取ったデータを順次クライアントに送信
                    buffer = ""
                    for chunk in response_stream:
                        buffer += chunk.text
                        # JSONオブジェクトが完成した可能性のある部分を処理
                        while '{' in buffer and '}' in buffer:
                            start_index = buffer.find('{')
                            end_index = buffer.find('}') + 1
                            json_str = buffer[start_index:end_index]
                            try:
                                correction_data = json.loads(json_str)
                                await websocket.send_json({
                                    "type": "correction_stream",
                                    "correction": correction_data
                                })
                                buffer = buffer[end_index:]
                            except json.JSONDecodeError:
                                # JSONとして不完全な場合は次のチャンクを待つ
                                break
                    
                    # ストリーム終了を通知
                    await websocket.send_json({"type": "correction_stream_end"})

                except Exception as e:
                    print(f"Error during correction streaming: {e}")
                    await websocket.send_json({
                        "type": "correction",
                        "corrections": [] # エラー時は空のリストを返す
                    })


            elif message["type"] == "verycheck":
                memo_content = message["content"]

                if not os.getenv("OPENROUTER_APIKEY"):
                    await websocket.send_json({
                        "type": "error",
                        "status": "verycheck_failed",
                        "message": "OPENROUTER_APIKEY is not set in .env file."
                    })
                    return
                
                input_text = f'''
                # 命令  
                志望理由書を厳格に校正してください。  
                誤字脱字、不自然な表現、曖昧な言葉遣い、論理の飛躍、冗長な部分を修正してください。  
                また、全体的な問題点・修正の優先順位・部分的な修正例・カテゴリ別スコア・完成文章を必ず提示してください。  

                # 制約条件  
                - 元の文章の意図や構造を保ちながら、明確かつ論理的で説得力のある表現に整えてください。  
                - 改行位置を大きく変更しないでください。  
                - 出力形式は以下の順序で必ず記載してください。  

                ---

                ## 出力フォーマット  

                ### 1. 全体的な問題点（3点以上）  
                文章全体を読んで気づいた改善点を列挙してください。  

                ### 2. 修正の優先順位リスト（重要度 高→低）  
                最も重要な修正点から順に番号をつけてください。  

                ### 3. 部分的な修正例（原文と修正文の対比）  
                - 原文：〜〜〜  
                - 修正文：〜〜〜  

                ### 4. カテゴリ別スコア（100点満点）  
                各観点を採点し、合計も示してください。  
                - 論理性：◯点 / 20  
                - 明確さ・具体性：◯点 / 20  
                - 語彙・表現力：◯点 / 20  
                - 志望理由の説得力：◯点 / 20  
                - 構成力・流れ：◯点 / 20  
                - **合計：◯点 / 100**  

                ---

                # 修正対象文章  
                {memo_content}  

                ---

                ## 出力例（サンプル）

                ### 1. 全体的な問題点  
                - 志望理由が抽象的で「なぜこの大学でなければならないか」が弱い。  
                - 将来の目標と大学での学びが十分に結びついていない。  
                - 表現が冗長で、同じ内容を繰り返している箇所がある。  

                ### 2. 修正の優先順位リスト  
                1. 志望理由の具体性を高める（最重要）  
                2. 将来の目標と大学での学びを論理的に接続する  
                3. 冗長な部分を削除し、文章を簡潔にする  

                ### 3. 部分的な修正例  
                - 原文：「私は将来、人の役に立つ仕事に就きたいと考えています。」  
                - 修正文：「私は将来、地域医療に貢献できる医師として、患者一人ひとりに寄り添う仕事に就きたいと考えています。」  

                ### 4. カテゴリ別スコア  
                - 論理性：14 / 20  
                - 明確さ・具体性：12 / 20  
                - 語彙・表現力：15 / 20  
                - 志望理由の説得力：13 / 20  
                - 構成力・流れ：16 / 20  
                - **合計：70 / 100**  
                '''
                
                # 3つのAIからのレスポンスを非同期で取得
                async def get_responses(websocket: WebSocket):
                    await websocket.send_json({"type": "verycheck_progress", "message": "3つのAIに同時にリクエストを送信中...", "progress": 25})

                    # gemini
                    task1 = asyncio.to_thread(
                        genai.GenerativeModel("gemini-2.5-flash").generate_content,
                        contents=input_text
                    )
                    
                    # openrouter client
                    openrouterclient = OpenAI(
                        base_url="https://openrouter.ai/api/v1",
                        api_key=os.getenv("OPENROUTER_APIKEY"),
                    )

                    # deepseek
                    task2 = asyncio.to_thread(
                        openrouterclient.chat.completions.create,
                        extra_headers={
                            "HTTP-Referer": "choko.cc",
                            "X-Title": "choko",
                        },
                        model="deepseek/deepseek-chat-v3.1",
                        messages=[{"role": "user", "content": input_text}]
                    )
                    
                    # qwen
                    task3 = asyncio.to_thread(
                        openrouterclient.chat.completions.create,
                        extra_headers={
                            "HTTP-Referer": "choko.cc",
                            "X-Title": "choko",
                        },
                        model="qwen/qwen3-235b-a22b-thinking-2507",
                        messages=[{"role": "user", "content": input_text}]
                    )
                    
                    responses = await asyncio.gather(task1, task2, task3)
                    return responses

                try:
                    response1, response2, response3 = await get_responses(websocket)
                    
                    await websocket.send_json({"type": "verycheck_progress", "message": "AIからのレスポンスを統合中...", "progress": 90})

                    gemini_response_text = ""
                    try:
                        gemini_response_text = response1.text
                    except ValueError:
                        gemini_response_text = "（回答がありませんでした）"

                    response_texts = {
                        "gemini": gemini_response_text,
                        "deepseek": response2.choices[0].message.content,
                        "qwen": response3.choices[0].message.content
                    }

                    integration_prompt = f"""
                    # 命令
                    以下の3つのAIによる志望理由書の評価と修正案を統合し、最も優れた最終的な成果物を作成してください。

                    # 入力
                    - AI1 (Gemini) の回答:
                    {response_texts['gemini']}

                    - AI2 (DeepSeek) の回答:
                    {response_texts['deepseek']}

                    - AI3 (Qwen) の回答:
                    {response_texts['qwen']}

                    - AI4 (あなたの回答):
                    ここにあなたの回答

                    # タスク
                    1.  **各AIの長所と短所を分析**: それぞれのAIの評価の鋭さ、提案の質、表現の自然さなどを比較してください。
                    2.  **評価の統合**: 4つのAIの「全体的な問題点」「修正の優先順位」を参考に、より網羅的で的確な評価を生成してください。重複する指摘はまとめ、より重要な点を強調してください。
                    3.  **修正案の統合**: 4つのAIの「部分的な修正例」「完成文章」を比較検討し、それぞれの良い点を取り入れた最高の「完成文章」を作成してください。元の文章の意図を尊重しつつ、論理的で説得力のある文章に仕上げてください。
                    4.  **スコアの再評価**: 統合された評価と完成文章に基づき、「カテゴリ別スコア」を再評価してください。

                    # 出力フォーマット
                    以下の形式で、最終的な成果物のみを出力してください。

                    ## 1. 統合された全体的な問題点
                    （4つのAIの指摘を統合し、3〜5点にまとめる）

                    ## 2. 統合された修正の優先順位リスト
                    （最も重要な修正点から順にリストアップ）

                    ## 3. 最終的な完成文章
                    （4つのAIの提案の良いところを取り入れた、最も優れた文章）

                    ## 4. 最終的なカテゴリ別スコア
                    - 論理性：◯点 / 20
                    - 明確さ・具体性：◯点 / 20
                    - 語彙・表現力：◯点 / 20
                    - 志望理由の説得力：◯点 / 20
                    - 構成力・流れ：◯点 / 20
                    - **合計：◯点 / 100**
                    """

                    integration_model = genai.GenerativeModel("gemini-2.5-pro")
                    integrated_response = await asyncio.to_thread(
                        integration_model.generate_content,
                        contents=integration_prompt
                    )

                    integrated_response_text = ""
                    try:
                        integrated_response_text = integrated_response.text
                    except ValueError:
                        integrated_response_text = "（回答がありませんでした）"

                    await websocket.send_json({
                        "type": "verycheck_result",
                        "result": integrated_response_text
                    })

                except Exception as e:
                    print(f"Error during verycheck: {e}")
                    await websocket.send_json({
                        "type": "error",
                        "status": "verycheck_failed",
                        "message": str(e)
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="localhost", port=8081)