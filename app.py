import os, json, base64, threading, datetime, uuid  
from flask import Flask, request, render_template, redirect, url_for, session, flash, jsonify  
from flask_session import Session  
from azure.search.documents import SearchClient  
from azure.core.credentials import AzureKeyCredential  
from azure.core.pipeline.transport import RequestsTransport  
from azure.cosmos import CosmosClient  
from openai import AzureOpenAI  
import certifi  
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions  
from werkzeug.utils import secure_filename  
import markdown2  
import PyPDF2  
import pandas as pd  
  
# === プロキシ環境が必要な場合（なければ以下2行は削除推奨） ===  
os.environ['HTTP_PROXY'] = 'http://g3.konicaminolta.jp:8080'  
os.environ['HTTPS_PROXY'] = 'http://g3.konicaminolta.jp:8080'  
  
app = Flask(__name__)  
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'your-default-secret-key')  
app.config['SESSION_TYPE'] = 'filesystem'  
app.config['SESSION_FILE_DIR'] = os.path.join(os.getcwd(), 'flask_session')  
app.config['SESSION_PERMANENT'] = False  
Session(app)  
  
client = AzureOpenAI(  
    api_key=os.getenv("AZURE_OPENAI_KEY"),  
    api_version=os.getenv("AZURE_OPENAI_API_VERSION"),  
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT")  
)  
search_service_endpoint = os.getenv("AZURE_SEARCH_ENDPOINT")  
search_service_key = os.getenv("AZURE_SEARCH_KEY")  
transport = RequestsTransport(verify=certifi.where())  
  
cosmos_endpoint = os.getenv("AZURE_COSMOS_ENDPOINT")  
cosmos_key = os.getenv("AZURE_COSMOS_KEY")  
database_name = 'chatdb'  
container_name = 'Drawing_management'  
cosmos_client = CosmosClient(cosmos_endpoint, credential=cosmos_key)  
database = cosmos_client.get_database_client(database_name)  
container = database.get_container_client(container_name)  
  
blob_connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")  
blob_service_client = BlobServiceClient.from_connection_string(blob_connection_string)  
file_container_name = 'drawing-management'  
file_container_client = blob_service_client.get_container_client(file_container_name)  
  
lock = threading.Lock()  
  
SYSTEM_MESSAGE = "図面や機器リスト（PDF, DWG, Excel形式等）のファイル管理を、専門的ルールや煩雑な作業なしに、「誰でも簡単に」「正確で迅速に」できるようにしたい。"  
ALLOWED_EXTENSIONS = {'jpg','jpeg','png','gif','pdf','dwg','xls','xlsx'}  
  
def allowed_file(filename):  
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS  
  
def generate_sas_url(blob_client, blob_name):  
    storage_account_key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")  
    if not storage_account_key:  
        raise Exception("AZURE_STORAGE_ACCOUNT_KEY が設定されていません。")  
    expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)  
    sas_token = generate_blob_sas(  
        account_name=blob_client.account_name,  
        container_name=blob_client.container_name,  
        blob_name=blob_name,  
        account_key=storage_account_key,  
        permission=BlobSasPermissions(read=True),  
        expiry=expiry  
    )  
    return f"{blob_client.url}?{sas_token}"  
  
def get_authenticated_user():  
    if "user_id" in session and "user_name" in session:  
        return session["user_id"]  
    client_principal = request.headers.get("X-MS-CLIENT-PRINCIPAL")  
    if client_principal:  
        try:  
            decoded = base64.b64decode(client_principal).decode("utf-8")  
            user_data = json.loads(decoded)  
            user_id, user_name = None, None  
            if "claims" in user_data:  
                for claim in user_data["claims"]:  
                    if claim.get("typ") == "http://schemas.microsoft.com/identity/claims/objectidentifier":  
                        user_id = claim.get("val")  
                    if claim.get("typ") == "name":  
                        user_name = claim.get("val")  
            if user_id:  
                session["user_id"] = user_id  
            if user_name:  
                session["user_name"] = user_name  
            return user_id  
        except Exception as e:  
            print("Easy Auth ユーザー情報の取得エラー:", e)  
    session["user_id"] = "anonymous@example.com"  
    session["user_name"] = "anonymous"  
    return session["user_id"]  
  
def save_chat_history():  
    with lock:  
        try:  
            sidebar = session.get("sidebar_messages", [])  
            idx = session.get("current_chat_index", 0)  
            if idx < len(sidebar):  
                current = sidebar[idx]  
                user_id = get_authenticated_user()  
                user_name = session.get("user_name", "anonymous")  
                session_id = current.get("session_id")  
                item = {  
                    'id': session_id,  
                    'user_id': user_id,  
                    'user_name': user_name,  
                    'session_id': session_id,  
                    'messages': current.get("messages", []),  
                    'system_message': SYSTEM_MESSAGE,  
                    'first_assistant_message': current.get("first_assistant_message", ""),  
                    'timestamp': datetime.datetime.utcnow().isoformat()  
                }  
                container.upsert_item(item)  
        except Exception as e:  
            print(f"チャット履歴保存エラー: {e}")  
  
def load_chat_history():  
    with lock:  
        user_id = get_authenticated_user()  
        sidebar_messages = []  
        try:  
            query = "SELECT * FROM c WHERE c.user_id = @user_id ORDER BY c.timestamp DESC"  
            parameters = [{"name": "@user_id", "value": user_id}]  
            items = container.query_items(query=query, parameters=parameters, enable_cross_partition_query=True)  
            for item in items:  
                if 'session_id' in item:  
                    chat = {  
                        "session_id": item['session_id'],  
                        "messages": item.get("messages", []),  
                        "system_message": SYSTEM_MESSAGE,  
                        "first_assistant_message": item.get("first_assistant_message", ""),  
                    }  
                    sidebar_messages.append(chat)  
        except Exception as e:  
            print(f"チャット履歴読み込みエラー: {e}")  
        return sidebar_messages  
  
def start_new_chat():  
    session["file_filenames"] = []  
    new_session_id = str(uuid.uuid4())  
    new_chat = {  
        "session_id": new_session_id,  
        "messages": [],  
        "first_assistant_message": "",  
        "system_message": SYSTEM_MESSAGE  
    }  
    sidebar = session.get("sidebar_messages", [])  
    sidebar.insert(0, new_chat)  
    session["sidebar_messages"] = sidebar  
    session["current_chat_index"] = 0  
    session["main_chat_messages"] = []  
    session.modified = True  
  
def get_indexed_files():  
    index_name = "index_drawing_management"  
    search_client = SearchClient(  
        endpoint=search_service_endpoint,  
        index_name=index_name,  
        credential=AzureKeyCredential(search_service_key),  
        transport=transport  
    )  
    results = search_client.search("*", select="id,filepath,title,category,url")  
    files = []  
    for r in results:  
        files.append({  
            "id": r["id"],  
            "filepath": r.get("filepath", ""),  
            "title": r.get("title", r.get("filepath", "")),  
            "category": r.get("category", ""),  
            "url": r.get("url", "")  
        })  
    return files  
  
# ===== ここだけ追記 =====  
def extract_ocr_text_from_gpt4o(image_bytes, ext):  
    ext = ext.lower()  
    mime_type = "jpeg" if ext == "jpg" else ext  
    encoded = base64.b64encode(image_bytes).decode('utf-8')  
    data_url = f"data:image/{mime_type};base64,{encoded}"  
    prompt = (  
        "この画像内の日本語・英語のテキストをできるだけ正確にすべて抜き出してください。"  
        "説明や要約は不要です。"  
    )  
    result = client.chat.completions.create(  
        model="gpt-4o",  
        messages=[  
            {  
                "role": "user",  
                "content": [  
                    {"type": "text", "text": prompt},  
                    {"type": "image_url", "image_url": {"url": data_url}}  
                ]  
            }  
        ],  
        max_tokens=4000  
    )  
    return result.choices[0].message.content.strip()  
# ========================  
  
@app.route('/', methods=['GET', 'POST'])  
def index():  
    get_authenticated_user()  
    if "sidebar_messages" not in session:  
        session["sidebar_messages"] = load_chat_history() or []  
        session.modified = True  
    if "current_chat_index" not in session:  
        start_new_chat()  
        session["show_all_history"] = False  
        session.modified = True  
    if "main_chat_messages" not in session:  
        idx = session.get("current_chat_index", 0)  
        sidebar = session.get("sidebar_messages", [])  
        if sidebar and idx < len(sidebar):  
            session["main_chat_messages"] = sidebar[idx].get("messages", [])  
        else:  
            session["main_chat_messages"] = []  
        session.modified = True  
    if "file_filenames" not in session:  
        session["file_filenames"] = []  
        session.modified = True  
    if "show_all_history" not in session:  
        session["show_all_history"] = False  
        session.modified = True  
  
    if request.method == 'POST':  
        if 'new_chat' in request.form:  
            start_new_chat()  
            session["show_all_history"] = False  
            session.modified = True  
            return redirect(url_for('index'))  
        if 'select_chat' in request.form:  
            selected_session = request.form.get("select_chat")  
            sidebar = session.get("sidebar_messages", [])  
            for idx, chat in enumerate(sidebar):  
                if chat.get("session_id") == selected_session:  
                    session["current_chat_index"] = idx  
                    session["main_chat_messages"] = chat.get("messages", [])  
                    break  
            session.modified = True  
            return redirect(url_for('index'))  
        if 'toggle_history' in request.form:  
            session["show_all_history"] = not session.get("show_all_history", False)  
            session.modified = True  
            return redirect(url_for('index'))  
        if 'upload_files' in request.form:  
            if 'files' in request.files:  
                files = request.files.getlist("files")  
                file_filenames = session.get("file_filenames", [])  
                for file in files:  
                    if file and allowed_file(file.filename):  
                        try:  
                            filename = secure_filename(file.filename)  
                            blob_client = file_container_client.get_blob_client(filename)  
                            if blob_client.exists():  
                                flash(f"「{filename}」は既に存在します。ファイル名を変更してアップロードしてください。", "error")  
                                continue  
                            ext = filename.rsplit('.', 1)[1].lower()  
                            file.stream.seek(0)  
                            blob_client.upload_blob(file.stream, overwrite=False)  
                            blob_url = generate_sas_url(blob_client, filename)  
                            index_file_content_to_search(filename, blob_url, ext)  
                            if filename not in file_filenames:  
                                file_filenames.append(filename)  
                        except Exception as e:  
                            print("ファイルアップロードエラー:", e)  
                            flash(f"ファイルアップロードエラー: {e}", "error")  
                session["file_filenames"] = file_filenames  
                session.modified = True  
            return redirect(url_for('index'))  
  
    chat_history = session.get("main_chat_messages", [])  
    sidebar_messages = session.get("sidebar_messages", [])  
    file_filenames = session.get("file_filenames", [])  
    files = [{"name": filename} for filename in file_filenames]  
    max_displayed_history = 6  
    max_total_history = 50  
    show_all_history = session.get("show_all_history", False)  
    indexed_files = get_indexed_files()  
    return render_template(  
        'index.html',  
        chat_history=chat_history,  
        chat_sessions=sidebar_messages,  
        files=files,  
        show_all_history=show_all_history,  
        max_displayed_history=max_displayed_history,  
        max_total_history=max_total_history,  
        session=session,  
        indexed_files=indexed_files  
    )  
  
# ===== 画像OCR機能を含んだインデックス作成関数 ================  
def index_file_content_to_search(filename, file_url, ext):  
    try:  
        extracted_text = ""  
        title = ""  
        summary = ""  
        category = ""  
        blob_client = file_container_client.get_blob_client(filename)  
        ext = ext.lower()  
        if ext == 'pdf':  
            from io import BytesIO  
            blob_bytes = blob_client.download_blob().readall()  
            pdf_stream = BytesIO(blob_bytes)  
            reader = PyPDF2.PdfReader(pdf_stream)  
            for page in reader.pages:  
                text = page.extract_text()  
                if text:  
                    extracted_text += text  
            prompt = (  
                "これは工業用の図面・成績書等のPDFです。\n"  
                f"本文抜粋：\n{extracted_text[:2000]}"  
                "\n内容の要約（サマリ）とタイトル（ファイル名を含めない実質的な中身名）、分類（例：図面/検査成績書/部品表/機器リスト等）を簡潔な日本語一文で出力:\n"  
                "フォーマット: タイトル:[…] サマリ:[…] カテゴリ:[…]"  
            )  
            ai_out = client.chat.completions.create(  
                model="gpt-4.1",    # gpt-4.1を使用  
                messages=[{"role":"system","content":prompt}]  
            ).choices[0].message.content  
            try:  
                title = ai_out.split("タイトル:")[1].split("サマリ:")[0].strip()  
                summary = ai_out.split("サマリ:")[1].split("カテゴリ:")[0].strip()  
                category = ai_out.split("カテゴリ:")[1].strip()  
            except Exception:  
                title = filename  
                summary = extracted_text[:100]  
                category = "不明"  
        elif ext in ['xls', 'xlsx']:  
            from io import BytesIO  
            blob_bytes = blob_client.download_blob().readall()  
            excel_stream = BytesIO(blob_bytes)  
            dfs = pd.read_excel(excel_stream, sheet_name=None)  
            txt = ""  
            for s, df in dfs.items():  
                txt += f"[{s}]\n" + df.to_string() + "\n"  
            extracted_text = txt  
            prompt = (  
                "これは工業用設備の機器リスト等のエクセルファイルです。\n"  
                f"冒頭抜粋：\n{txt[:2000]}"  
                "\n内容の要約（サマリ）とタイトル、分類を簡潔な日本語で:\n"  
                "フォーマット: タイトル:[…] サマリ:[…] カテゴリ:[…]"  
            )  
            ai_out = client.chat.completions.create(  
                model="gpt-4.1",    # gpt-4.1を使用  
                messages=[{"role":"system","content":prompt}]  
            ).choices[0].message.content  
            try:  
                title = ai_out.split("タイトル:")[1].split("サマリ:")[0].strip()  
                summary = ai_out.split("サマリ:")[1].split("カテゴリ:")[0].strip()  
                category = ai_out.split("カテゴリ:")[1].strip()  
            except Exception:  
                title = filename  
                summary = extracted_text[:100]  
                category = "不明"  
        elif ext in ['jpg','jpeg','png','gif']:  
            blob_bytes = blob_client.download_blob().readall()  
            extracted_text = extract_ocr_text_from_gpt4o(blob_bytes, ext)  
            title = filename  
            summary = extracted_text[:100]  
            category = "画像"  
        else:  
            extracted_text = ""  
            title, summary, category = filename, "", "その他"  
  
        index_name = "index_drawing_management"  
        search_client = SearchClient(  
            endpoint=search_service_endpoint,  
            index_name=index_name,  
            credential=AzureKeyCredential(search_service_key),  
            transport=transport  
        )  
        doc = {  
            "id": str(uuid.uuid4()),  
            "title": title or filename,  
            "content": extracted_text or summary or "",  
            "category": category,  
            "filepath": filename,  
            "url": file_url  
        }  
        search_client.upload_documents([doc])  
    except Exception as e:  
        print(f"Search登録エラー: {e}")  
  
@app.route("/delete_index_file", methods=["POST"])  
def delete_index_file():  
    index_id = request.form.get("index_id")  
    filepath = request.form.get("filepath")  
    try:  
        search_client = SearchClient(  
            endpoint=search_service_endpoint,  
            index_name="index_drawing_management",  
            credential=AzureKeyCredential(search_service_key),  
            transport=transport  
        )  
        search_client.delete_documents(documents=[{"id": index_id}])  
    except Exception as e:  
        flash(f"インデックス削除エラー: {e}", "error")  
    # Blobも削除  
    try:  
        blob_client = file_container_client.get_blob_client(filepath)  
        blob_client.delete_blob()  
    except Exception as e:  
        flash(f"Blobファイル削除エラー（インデックスは削除済）: {e}", "error")  
    flash(f"インデックス・ファイルの削除が完了しました。", "info")  
    return redirect(url_for("index"))  
  
@app.route('/indexed_files', methods=['GET'])  
def ajax_list_indexed_files():  
    files = get_indexed_files()  
    return jsonify(files)  
  
@app.route('/send_message', methods=['POST'])  
def send_message():  
    data = request.get_json()  
    prompt = data.get('prompt', '').strip()  
    if not prompt:  
        return json.dumps({'response': ''}), 400, {'Content-Type': 'application/json'}  
  
    messages = session.get("main_chat_messages", [])  
    messages.append({"role": "user", "content": prompt})  
    session["main_chat_messages"] = messages  
    session.modified = True  
  
    save_chat_history()  
  
    try:  
        last2_user = [m["content"] for m in messages if m["role"] == "user"][-2:]  
        last2_ai = [m["content"] for m in messages if m["role"] == "assistant"][-2:]  
        search_chunks = last2_user + last2_ai + [prompt]  
        search_query = "\n".join(search_chunks)  
        index_name = "index_drawing_management"  
        search_client = SearchClient(  
            endpoint=search_service_endpoint,  
            index_name=index_name,  
            credential=AzureKeyCredential(search_service_key),  
            transport=transport  
        )  
        topNDocuments = 20  
        strictness = 0.1  
        search_results = search_client.search(  
            search_text=search_query,  
            search_fields=["content", "title"],  
            select="content,filepath,title,url,category",  
            query_type="semantic",  
            semantic_configuration_name="default",  
            query_caption="extractive",  
            query_answer="extractive",  
            top=topNDocuments  
        )  
        results_list = [result for result in search_results if result['@search.score'] >= strictness]  
        results_list.sort(key=lambda x: x['@search.score'], reverse=True)  
        context = "\n".join([  
            f"ファイル:{result.get('title','?')} 種別:{result.get('category')} 内容:{result['content']} 保存先:{result.get('url', '')}"  
            for result in results_list  
        ])  
        rule_message = (  
            "回答の際は必ず根拠となる図面・機器リスト等の「ファイル名」「種別」「保存先リンク（URL）」を示してください。\n"  
            "ファイル名や中身だけでなく、保存先のURLも記載することでユーザーが内容をチェックできるようにしてください。\n"  
            "シンプルな回答と、参照元（保存先）もセットで答えてください。"  
        )  
        messages_list = []  
        messages_list.append({"role": "system", "content": SYSTEM_MESSAGE})  
        messages_list.append({"role": "user", "content": rule_message})  
        messages_list.append({"role": "user", "content": f"以下のファイルリストを参考にしてください: {context[:50000]}"})  
        past_message_count = 20  
        messages_list.extend(session.get("main_chat_messages", [])[-(past_message_count * 2):])  
        model_name = "gpt-4.1"  # gpt-4.1 を使用  
        extra_args = {}  
  
        response_obj = client.chat.completions.create(  
            model=model_name,  
            messages=messages_list,  
            **extra_args  
        )  
        assistant_response = response_obj.choices[0].message.content  
  
        assistant_response_html = markdown2.markdown(  
            assistant_response,  
            extras=["tables", "fenced-code-blocks", "code-friendly", "break-on-newline", "cuddled-lists"]  
        )  
  
        messages.append({"role": "assistant", "content": assistant_response_html, "type": "html"})  
        session["main_chat_messages"] = messages  
        session.modified = True  
  
        idx = session.get("current_chat_index", 0)  
        sidebar = session.get("sidebar_messages", [])  
        if idx < len(sidebar):  
            sidebar[idx]["messages"] = messages  
            if not sidebar[idx].get("first_assistant_message"):  
                sidebar[idx]["first_assistant_message"] = assistant_response  
            session["sidebar_messages"] = sidebar  
            session.modified = True  
  
        save_chat_history()  
        session["assistant_responded"] = True  
        session.modified = True  
  
        return json.dumps({'response': assistant_response_html}), 200, {'Content-Type': 'application/json'}  
    except Exception as e:  
        print("チャット応答エラー:", e)  
        flash(f"エラーが発生しました: {e}", "error")  
        session["assistant_responded"] = True  
        session.modified = True  
        return json.dumps({'response': f"エラーが発生しました: {e}"}), 500, {'Content-Type': 'application/json'}  
  
if __name__ == '__main__':  
    app.run(debug=True, host='0.0.0.0')  