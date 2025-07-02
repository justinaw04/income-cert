from flask import Flask, request, render_template_string
import os
import json
import tempfile
import io
from pathlib import Path
from urllib.parse import urlparse
import re
import docx
import fitz
import pytesseract
from PIL import Image
from airtable import Airtable
from boxsdk import Client, JWTAuth
from openai import OpenAI

app = Flask(__name__)

# --- Initialize Clients from Environment Variables ---
AIRTABLE_API_KEY = os.getenv('AIRTABLE_API_KEY')
AIRTABLE_BASE_ID = os.getenv('AIRTABLE_BASE_ID')
AIRTABLE_TABLE_NAME = os.getenv('AIRTABLE_TABLE_NAME')
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
BOX_JWT_CONFIG = os.getenv("BOX_JWT_CONFIG")

# --- Client Initialization ---
airtable_client = None
box_client = None
openai_client = None

if AIRTABLE_API_KEY and AIRTABLE_BASE_ID and AIRTABLE_TABLE_NAME:
    airtable_client = Airtable(AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, AIRTABLE_API_KEY)

if OPENAI_API_KEY:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)

if BOX_JWT_CONFIG:
    try:
        jwt_config = json.loads(BOX_JWT_CONFIG)
        private_key_data = jwt_config['boxAppSettings']['appAuth']['privateKey']
        public_key_id = jwt_config['boxAppSettings']['appAuth']['publicKeyID']
        passphrase_data = jwt_config['boxAppSettings']['appAuth'].get('passphrase')

        auth = JWTAuth(
            client_id=jwt_config['boxAppSettings']['clientID'],
            client_secret=jwt_config['boxAppSettings']['clientSecret'],
            enterprise_id=jwt_config['enterpriseID'],
            jwt_key_id=public_key_id,
            rsa_private_key_data=private_key_data.encode('utf-8'),
            rsa_private_key_passphrase=passphrase_data.encode('utf-8') if passphrase_data else None
        )
        box_client = Client(auth)
    except Exception as e:
        print(f"Box client initialization failed: {e}")

def get_box_folder_id_from_link(link: str) -> str | None:
    parsed_url = urlparse(link)
    match = re.search(r'/folder/(\d+)', parsed_url.path)
    if match:
        return match.group(1)
    if re.match(r'^\d+$', link):
        return link
    return None

def extract_text_from_file(file_path: Path) -> str:
    text_content = ""
    file_extension = file_path.suffix.lower()
    try:
        if file_extension == ".txt":
            with open(file_path, 'r', encoding='utf-8') as f:
                text_content = f.read()
        elif file_extension == ".docx":
            document = docx.Document(file_path)
            for paragraph in document.paragraphs:
                text_content += paragraph.text + "\n"
        elif file_extension == ".pdf":
            doc = fitz.open(file_path)
            for page_num in range(doc.page_count):
                page = doc.load_page(page_num)
                page_text = page.get_text("text")
                if page_text.strip():
                    text_content += page_text + "\n"
                else:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                    img = Image.open(io.BytesIO(pix.tobytes("png")))
                    try:
                        ocr_text = pytesseract.image_to_string(img)
                        text_content += ocr_text + "\n"
                    except Exception as e:
                        return f"Error during OCR: {e}"
            doc.close()
        elif file_extension in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff"]:
            try:
                img = Image.open(file_path)
                text_content = pytesseract.image_to_string(img)
            except Exception as e:
                return f"Error processing image file: {e}"
        else:
            return f"Unsupported file type: '{file_extension}'"
    except Exception as e:
        return f"Error extracting text: {e}"
    return text_content.strip()

def summarize_text_with_openai(text: str) -> str | None:
    if not text:
        return None
    if not openai_client:
        return "OpenAI client not initialized."

    system_prompt = (
        "You are an affordable housing compliance specialist with expertise in reviewing "
        "program guidelines, extracting key data from earnings reports and financial statements, "
        "and preparing income certifications. Your primary tasks involve processing income "
        "certifications for CWH staff, producing reports, and filling out different program type "
        "forms including LIHTC, HUD, CalHFA, and Section 8."
    )

    user_content = f"""
Here is the aggregated text from the applicant's financial documents:

{text}

Please provide the Compliance Memo that summarizes income sources, calculation steps, applicable allowances, and whether the household appears income-eligible. Use bullet points where helpful. End with a clear compliance determination.
"""

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]

        response = openai_client.chat.completions.create(
            model="gpt-4o", messages=messages, max_tokens=1500, temperature=0.2
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Error calling OpenAI API: {e}"

def run_summarization_process() -> str:
    log_messages = []
    if not all([airtable_client, box_client, openai_client]):
        return "Error: One or more API clients failed to initialize."

    log_messages.append("--- Starting Box Folder Summarization Process ---")
    try:
        records = airtable_client.get_all(view='Grid view')
        log_messages.append(f"Found {len(records)} records in Airtable.")
    except Exception as e:
        return f"Error reading records from Airtable: {e}"

    for record in records:
        record_id = record['id']
        box_link = record['fields'].get("Link to Box")
        if not box_link:
            continue
        folder_id = get_box_folder_id_from_link(box_link)
        if not folder_id:
            continue
        try:
            box_folder = box_client.folder(folder_id).get()
            items = list(box_folder.get_items())
            if any(item.name == "Summary.txt" for item in items):
                continue

            extracted_texts = []
            for item in items:
                if item.type == 'file':
                    with tempfile.TemporaryDirectory() as tmpdir:
                        temp_file_path = Path(tmpdir) / item.name
                        with open(temp_file_path, 'wb') as f:
                            item.download_to(f)
                        text = extract_text_from_file(temp_file_path)
                        extracted_texts.append(f"--- File: {item.name} ---\n{text}\n")

            aggregated_text = "\n\n".join(extracted_texts)
            summary = summarize_text_with_openai(aggregated_text)
            summary_stream = io.BytesIO(summary.encode('utf-8'))
            box_client.folder(folder_id).upload_stream(summary_stream, "Summary.txt")

        except Exception as e:
            log_messages.append(f"Error processing record {record_id}: {e}")

    log_messages.append("--- Summarization Process Completed ---")
    return "\n".join(log_messages)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Income Certification Wizard</title>
    <style>
        body { font-family: sans-serif; margin: 20px; background-color: #f4f7f6; color: #333; }
        .container { max-width: 800px; margin: 0 auto; background-color: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h1 { color: #007bff; text-align: center; margin-bottom: 30px; }
        form { text-align: center; }
        button {
            padding: 12px 25px;
            font-size: 1.1em;
            background-color: #28a745;
            color: white;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            transition: background-color 0.3s ease;
        }
        button:hover { background-color: #218838; }
        .result-box {
            margin-top: 30px;
            padding: 15px;
            border: 1px solid #ccc;
            border-radius: 5px;
            background-color: #e9ecef;
            font-family: monospace;
            white-space: pre-wrap;
            max-height: 400px;
            overflow-y: auto;
        }
        .running-message {
            text-align: center;
            margin-top: 20px;
            font-size: 1.1em;
            color: #007bff;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Income Certification Wizard</h1>
        <form id="summarizeForm" method="POST" action="/">
            <button type="submit" id="startButton">Click to Run</button>
        </form>

        <div id="loadingMessage" class="running-message" style="display:none;">
            Processing... This might take a few minutes. Please wait.
        </div>

        {% if result %}
            <div class="result-box">
                <pre>{{ result }}</pre>
            </div>
        {% endif %}
    </div>

    <script>
        document.getElementById('summarizeForm').addEventListener('submit', function() {
            document.getElementById('startButton').style.display = 'none';
            document.getElementById('loadingMessage').style.display = 'block';
        });
    </script>
</body>
</html>
"""

@app.route('/', methods=['GET', 'POST'])
def index():
    result_message = None
    if request.method == 'POST':
        result_message = run_summarization_process()
    return render_template_string(HTML_TEMPLATE, result=result_message)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
