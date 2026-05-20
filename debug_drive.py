"""
Діагностика Google Drive підключення.
Запускай: python3 debug_drive.py [назва папки]
"""
from __future__ import annotations

import sys
import os
from dotenv import load_dotenv

load_dotenv()


def main():
    search_name = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Agentic Notion"

    print("=" * 55)
    print("  Google Drive Diagnostic")
    print("=" * 55)

    # 1. Check credentials file
    creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH", "google_credentials.json")
    print(f"\n1. Credentials path: {creds_path}")
    if os.path.exists(creds_path):
        print("   ✅ File exists")
    else:
        print("   ❌ File NOT found — place google_credentials.json in the project folder")
        print("      (or set GOOGLE_SERVICE_ACCOUNT_PATH in .env)")
        return

    # 2. Build service
    print("\n2. Building Drive service...")
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            creds_path,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        print("   ✅ Service built OK")
        print(f"   Service account email: {creds.service_account_email}")
    except Exception as e:
        print(f"   ❌ Failed: {e}")
        return

    # 3. List all folders accessible to service account
    print("\n3. All folders accessible to this service account:")
    try:
        results = service.files().list(
            q="mimeType = 'application/vnd.google-apps.folder' and trashed = false",
            fields="files(id, name, parents, modifiedTime)",
            pageSize=30,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        folders = results.get("files", [])
        if not folders:
            print("   ⚠️  No folders found — share the KSE GBS Drive folder with:")
            print(f"      {creds.service_account_email}")
        else:
            for f in folders:
                print(f"   📁 {f['name']}")
                print(f"      id: {f['id']}")
                print(f"      parents: {f.get('parents', [])}")
    except Exception as e:
        print(f"   ❌ Failed to list folders: {e}")
        return

    # 4. Search for the specified folder name
    print(f"\n4. Searching for folder: '{search_name}'")
    safe_name = search_name.replace("'", "\\'")
    query = (
        f"mimeType = 'application/vnd.google-apps.folder' "
        f"and name contains '{safe_name}' "
        f"and trashed = false"
    )
    try:
        results = service.files().list(
            q=query,
            fields="files(id, name, parents, modifiedTime)",
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        found = results.get("files", [])
        if found:
            print(f"   ✅ Found {len(found)} match(es):")
            for f in found:
                print(f"   📁 {f['name']}")
                print(f"      https://drive.google.com/drive/folders/{f['id']}")
        else:
            print("   ❌ Not found")
            # Try each word separately
            for word in search_name.split():
                if len(word) < 3:
                    continue
                safe_word = word.replace("'", "\\'")
                q2 = (
                    f"mimeType = 'application/vnd.google-apps.folder' "
                    f"and name contains '{safe_word}' "
                    f"and trashed = false"
                )
                r2 = service.files().list(
                    q=q2,
                    fields="files(id, name)",
                    pageSize=5,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()
                w_found = r2.get("files", [])
                if w_found:
                    print(f"   Word '{word}' matched: {[f['name'] for f in w_found]}")
    except Exception as e:
        print(f"   ❌ Search failed: {e}")

    print("\n" + "=" * 55)
    print("Done.")


if __name__ == "__main__":
    main()
