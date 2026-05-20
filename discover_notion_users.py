"""
Helper script: scan existing Projects database and extract all Notion user IDs
that appear in "Відповідальна особа", "Coordinator", "Project Manager" fields.

Run this ONCE to bootstrap your user_mapping.json:
    python discover_notion_users.py

It prints a table of discovered users and lets you match them to emails.
Then saves the result to data/user_mapping.json.
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv
from notion_client import Client

load_dotenv()

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = "173122fe-7695-80c6-96b3-000b08f81c95"
OUTPUT_FILE = Path(__file__).parent / "data" / "user_mapping.json"

PERSON_FIELDS = ["Відповідальна особа", "Coordinator", "Project Manager", "Marketer"]

notion = Client(auth=NOTION_TOKEN)


def scan_projects() -> dict[str, dict]:
    """
    Scan all projects and collect unique Notion users from person fields.
    Returns: {notion_user_id: {"name": ..., "email": ..., "avatar": ...}}
    """
    discovered: dict[str, dict] = {}
    cursor = None

    print("Scanning Projects database...")
    while True:
        kwargs = {"database_id": DATABASE_ID, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor

        response = notion.databases.query(**kwargs)
        pages = response.get("results", [])

        for page in pages:
            props = page.get("properties", {})
            for field in PERSON_FIELDS:
                prop = props.get(field, {})
                for person in prop.get("people", []):
                    uid = person.get("id")
                    if uid and uid not in discovered:
                        discovered[uid] = {
                            "name": person.get("name", ""),
                            "email": person.get("person", {}).get("email", ""),
                            "avatar": person.get("avatar_url", ""),
                            "type": person.get("type", ""),
                        }

        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")

    return discovered


def main():
    discovered = scan_projects()

    if not discovered:
        print("❌ No users found in Projects database. Make sure NOTION_TOKEN is correct and the integration has access.")
        return

    print(f"\n✅ Found {len(discovered)} unique users:\n")
    print(f"{'#':<4} {'Name':<30} {'Email':<35} {'Notion ID'}")
    print("-" * 100)

    users_list = sorted(discovered.items(), key=lambda x: x[1].get("name", ""))
    for i, (uid, info) in enumerate(users_list, 1):
        name = info.get("name", "—")
        email = info.get("email", "—")
        print(f"{i:<4} {name:<30} {email:<35} {uid}")

    print("\n" + "=" * 100)
    print("\n📋 Now match each person to their email (or press Enter to skip).")
    print("   If email is already shown above, just confirm it.\n")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Load existing mapping if any
    existing: dict[str, str] = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            existing = json.load(f)
        print(f"Found existing mapping with {len(existing)} entries. Adding/updating...\n")

    # Reverse existing mapping: notion_id → email
    notion_to_email = {v: k for k, v in existing.items()}

    new_mapping: dict[str, str] = dict(existing)  # start with existing

    for uid, info in users_list:
        name = info.get("name", "—")
        auto_email = info.get("email", "")
        existing_email = notion_to_email.get(uid, "")

        # If already mapped, skip
        if existing_email:
            print(f"✓ {name} → already mapped to {existing_email}")
            continue

        # If Notion returned an email, use it
        if auto_email and "@" in auto_email:
            new_mapping[auto_email] = uid
            print(f"✓ {name} → auto-mapped from Notion: {auto_email}")
            continue

        # Ask user to input email
        prompt = f"  {name} (Notion ID: {uid})\n  Email: "
        try:
            email_input = input(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            break

        if email_input and "@" in email_input:
            new_mapping[email_input] = uid
            print(f"  ✓ Mapped {name} → {email_input}\n")
        else:
            print(f"  ⚠️  Skipped {name}\n")

    # Save
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(new_mapping, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Saved {len(new_mapping)} mappings to {OUTPUT_FILE}")
    print("\nContents:")
    for email, uid in sorted(new_mapping.items()):
        name = discovered.get(uid, {}).get("name", "—")
        print(f"  {email:<35} → {uid}  ({name})")

    print(f"\n🎉 Done! user_mapping.json is ready. Bot will use it automatically.")
    print("   Run this script again any time to add new team members.")


if __name__ == "__main__":
    main()
