import os  
import json  
import base64  
import threading  
import datetime  
import uuid  
import io  
  
from flask import Flask, request, render_template, redirect, url_for, session, flash, jsonify, copy_current_request_context  
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
from pdf2image import convert_from_bytes  
  
POPLER_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "poppler", "bin"))  
os.environ["PATH"] = POPLER_PATH + os.pathsep + os.environ.get("PATH", "")  
  
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
  
blob_service_client = BlobServiceClient.from_connection_string(os.getenv("AZURE_STORAGE_CONNECTION_STRING"))  
file_container_client = blob_service_client.get_container_client('drawing-management')  
  
SYSTEM_MESSAGE = "図面や機器リスト（PDF, DWG, 画像等）のファイル管理を、専門的ルールや煩雑な作業なしに、「誰でも簡単に」「正確で迅速に」できるようにアシスタントしてください。"  
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'pdf', 'dwg'}  
  
def allowed_file(filename):  
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS  
  
def generate_sas_url(blob_client, blob_name):  
    storage_account_key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")  
    now_utc = datetime.datetime.now(datetime.timezone.utc)  
    start = now_utc - datetime.timedelta(minutes=5)  
    expiry = now_utc + datetime.timedelta(days=30)  
    sas_token = generate_blob_sas(  
        account_name=blob_client.account_name,  
        container_name=blob_client.container_name,  
        blob_name=blob_name,  
        account_key=storage_account_key,  
        permission=BlobSasPermissions(read=True),  
        expiry=expiry,  
        start=start  
    )  
    return f"{blob_client.url}?{sas_token}"  
  
def get_indexed_files():  
    index_name = "index_drawing_management"  
    search_client = SearchClient(  
        endpoint=search_service_endpoint,  
        index_name=index_name,  
        credential=AzureKeyCredential(search_service_key),  
        transport=transport  
    )  
    results = search_client.search("*", select="id,filepath,title,category,factory_name,machine_number")  
    files = []  
    for r in results:  
        filepath = r.get("filepath", "")  
        blob_client = file_container_client.get_blob_client(filepath)  
        url = generate_sas_url(blob_client, filepath)  
        files.append({  
            "id": r["id"],  
            "filepath": filepath,  
            "title": r.get("title", filepath),  
            "category": r.get("category", ""),  
            "url": url,  
            "factory_name": r.get("factory_name", ""),  
            "machine_number": r.get("machine_number", "")  
        })  
    return files  
  
def extract_ocr_text_from_gpt4o(image_bytes, ext):  
    ext = ext.lower()  
    mime_type = "jpeg" if ext == "jpg" else ext  
    encoded = base64.b64encode(image_bytes).decode('utf-8')  
    data_url = f"data:image/{mime_type};base64,{encoded}"  
    prompt = "この画像内の日本語・英語のテキストをできるだけ正確にすべて抜き出してください。説明や要約は不要です。"  
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
  
def index_file_content_to_search(filename, ext, factory_name=None, machine_number=None):  
    try:  
        extracted_text, title, summary, category = "", "", "", ""  
        blob_client = file_container_client.get_blob_client(filename)  
        ext = ext.lower()  
        if ext == 'pdf':  
            blob_bytes = blob_client.download_blob().readall()  
            pdf_images = convert_from_bytes(blob_bytes)  
            for i, img in enumerate(pdf_images):  
                img_byte_arr = io.BytesIO()  
                img.save(img_byte_arr, format='JPEG')  
                img_bytes = img_byte_arr.getvalue()  
                page_text = extract_ocr_text_from_gpt4o(img_bytes, "jpeg")  
                extracted_text += f"\n--- Page {i+1} ---\n{page_text}\n"  
            prompt = (  
                "これは工業用の図面・成績書等のPDFをOCRして抽出したテキストです。\n"  
                f"本文抜粋：\n{extracted_text[:2000]}"  
                "\n内容の要約（サマリ）とタイトル（ファイル名を含めない実質的な中身名）、分類（例：図面/検査成績書/部品表/機器リスト等）を簡潔な日本語一文で出力:\n"  
                "フォーマット: タイトル:[…] サマリ:[…] カテゴリ:[…]"  
            )  
            ai_out = client.chat.completions.create(  
                model="gpt-4.1",  
                messages=[{"role":"system","content":prompt}]  
            ).choices[0].message.content  
            try:  
                title = ai_out.split("タイトル:")[1].split("サマリ:")[0].strip()  
                summary = ai_out.split("サマリ:")[1].split("カテゴリ:")[0].strip()  
                category = ai_out.split("カテゴリ:")[1].strip()  
            except Exception:  
                title, summary, category = filename, extracted_text[:100], "不明"  
        elif ext in ['jpg','jpeg','png','gif']:  
            blob_bytes = blob_client.download_blob().readall()  
            extracted_text = extract_ocr_text_from_gpt4o(blob_bytes, ext)  
            title, summary, category = filename, extracted_text[:100], "画像"  
        else:  
            extracted_text, title, summary, category = "", filename, "", "その他"  
  
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
            "factory_name": factory_name or "",  
            "machine_number": machine_number or ""  
        }  
        search_client.upload_documents([doc])  
        return True, None  
    except Exception as e:  
        print(f"Search登録エラー: {e}")  
        return False, str(e)  
  
@app.route('/', methods=['GET', 'POST'])  
def index():  
    if 'main_chat_messages' not in session:  
        session['main_chat_messages'] = []  
    if 'indexing_status' not in session:  
        session['indexing_status'] = []  
  
    if request.method == 'POST':  
        # ファイルアップロード  
        if 'upload_files' in request.form:  
            factory_name = request.form.get("factory_name")  
            machine_number = request.form.get("machine_number")  
            if not factory_name or not machine_number:  
                flash("工場名・機番は必須です", "error")  
                return redirect(url_for('index'))  
            if 'files' in request.files:  
                files = request.files.getlist("files")  
                for file in files:  
                    if file and allowed_file(file.filename):  
                        filename = secure_filename(file.filename)  
                        blob_client = file_container_client.get_blob_client(filename)  
                        if blob_client.exists():  
                            flash(f"「{filename}」は既に存在します。ファイル名を変更してアップロードしてください。", "error")  
                            continue  
                        ext = filename.rsplit('.', 1)[1].lower()  
                        # 進行状況を"indexing"追加  
                        status_entry = {'filename': filename, 'status': 'indexing'}  
                        session['indexing_status'] = [x for x in session['indexing_status'] if x['filename']!=filename]  
                        session['indexing_status'].append(status_entry)  
                        session.modified = True  
  
                        # ファイル保存  
                        file.stream.seek(0)  
                        blob_client.upload_blob(file.stream, overwrite=False)  
  
                        # 登録処理をバックグラウンドで実行  
                        @copy_current_request_context  
                        def background_indexing():  
                            ok, err = index_file_content_to_search(filename, ext, factory_name, machine_number)  
                            # ステータス更新  
                            idx_list = session.get('indexing_status', [])  
                            for entry in idx_list:  
                                if entry['filename'] == filename:  
                                    if ok:  
                                        entry['status'] = 'done'  
                                    else:  
                                        entry['status'] = 'error'  
                                        entry['error_msg'] = err  
                            session['indexing_status'] = idx_list  
                            session.modified = True  
  
                        t = threading.Thread(target=background_indexing)  
                        t.start()  
                return redirect(url_for('index'))  
  
    indexed_files = get_indexed_files()  
    indexing_status = session.get('indexing_status', [])  
    chat_history = session.get("main_chat_messages", [])  
    return render_template(  
        'index.html',  
        indexing_status=indexing_status,  
        indexed_files=indexed_files,  
        chat_history=chat_history  
    )  
  
@app.route('/indexing_status')  
def api_indexing_status():  
    return jsonify(session.get('indexing_status', []))  
  
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
    try:  
        blob_client = file_container_client.get_blob_client(filepath)  
        blob_client.delete_blob()  
    except Exception as e:  
        flash(f"Blobファイル削除エラー（インデックスは削除済）: {e}", "error")  
    flash(f"インデックス・ファイルの削除が完了しました。", "info")  
    return redirect(url_for("index"))  
  
@app.route('/indexed_files', methods=['GET'])  
def ajax_list_indexed_files():  
    return jsonify(get_indexed_files())  
  
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
    try:  
        last2_user = [m["content"] for m in messages if m["role"] == "user"][-2:]  
        last2_ai = [m["content"] for m in messages if m["role"] == "assistant"][-2:]  
        search_query = "\n".join(last2_user + last2_ai + [prompt])  
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
            search_fields=["title", "content", "category", "factory_name", "machine_number"],  
            select="content,filepath,title,category,factory_name,machine_number",  
            query_type="semantic",  
            semantic_configuration_name="default",  
            query_caption="extractive",  
            query_answer="extractive",  
            top=topNDocuments  
        )  
        results_list = [result for result in search_results if result['@search.score'] >= strictness]  
        results_list.sort(key=lambda x: x['@search.score'], reverse=True)  
        context_lines = []  
        for result in results_list:  
            filepath = result.get('filepath', '')  
            blob_client = file_container_client.get_blob_client(filepath)  
            url = generate_sas_url(blob_client, filepath)  
            context_lines.append(  
                f"ファイル:{result.get('title','?')} 種別:{result.get('category','?')} 工場名:{result.get('factory_name','?')} 機番:{result.get('machine_number','?')} 内容:{result.get('content','')} 保存先:{url}"  
            )  
        context = "\n".join(context_lines)  
        rule_message = (  
            "回答の際は必ず根拠となる図面・機器リスト等の「ファイル名」「種別」「保存先リンク（URL）」を示してください。\n"  
            "ファイル名や中身だけでなく、保存先のURLも記載することでユーザーが内容をチェックできるようにしてください。\n"  
            "参照元（保存先）もセットで答えてください。\n"  
            "なお、URLや保存先リンクは必ず [ファイル名や説明](https://...) のようなMarkdownリンク形式で記載してください。"  
            "例: [回路図のPDF](https://xxx.blob.core.windows.net/xxx/yyy.pdf) のような形式で書くこと。"  
            "「保存先: https://xxx...」だけの生URLは使用しないでください。"  
        )  
        messages_list = []  
        messages_list.append({"role": "system", "content": SYSTEM_MESSAGE})  
        messages_list.append({"role": "user", "content": rule_message})  
        messages_list.append({  
            "role": "user",  
            "content": (  
                f"以下のファイルリストを参考にしてください:\n"  
                f"{context[:50000]}\n"  
                f"--- ここから過去のチャット履歴 ---"  
            )  
        })  
        past_message_count = 20  
        messages_list.extend(session.get("main_chat_messages", [])[-(past_message_count * 2):])  
        response_obj = client.chat.completions.create(  
            model="gpt-4.1-mini",  
            messages=messages_list  
        )  
        assistant_response = response_obj.choices[0].message.content  
        assistant_response_html = markdown2.markdown(  
            assistant_response,  
            extras=["tables", "fenced-code-blocks", "code-friendly", "break-on-newline", "cuddled-lists"]  
        )  
        messages.append({"role": "assistant", "content": assistant_response_html, "type": "html"})  
        session["main_chat_messages"] = messages  
        session.modified = True  
        return json.dumps({'response': assistant_response_html}), 200, {'Content-Type': 'application/json'}  
    except Exception as e:  
        print("チャット応答エラー:", e)  
        flash(f"エラーが発生しました: {e}", "error")  
        session.modified = True  
        return json.dumps({'response': f"エラーが発生しました: {e}"}), 500, {'Content-Type': 'application/json'}  
  
if __name__ == '__main__':  
    app.run(debug=True, host='0.0.0.0')  