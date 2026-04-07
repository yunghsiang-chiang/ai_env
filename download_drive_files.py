from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

# === 設定 ===
SERVICE_ACCOUNT_FILE = 'service_account.json'  # 你的服務帳號金鑰路徑
FOLDER_ID = '0ACuINb2Qs58kUk9PVA'              # Google Drive 資料夾 ID
DOWNLOAD_DIR = Path('after')                   # 下載檔案儲存資料夾
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

# === 建立下載資料夾 ===
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# === 建立 Google Drive API 服務 ===
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=credentials)

# === 查詢資料夾中的所有檔案 ===
results = drive_service.files().list(
    q=f"'{FOLDER_ID}' in parents and trashed = false",
    fields="files(id, name)").execute()
files = results.get('files', [])

# === 執行下載 ===
for file in files:
    file_path = DOWNLOAD_DIR / file['name']
    if file_path.exists():
        print(f"✅ 已存在，略過：{file['name']}")
        continue

    print(f"⬇️ 下載中：{file['name']}")
    request = drive_service.files().get_media(fileId=file['id'])
    with io.FileIO(file_path, 'wb') as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
    print(f"✔️ 完成下載：{file['name']}")

print("📁 所有檔案處理完成。")
