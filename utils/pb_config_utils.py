import os
from .s3_utils import (
    delete_folder_from_s3,
    upload_any_file,
    read_json_from_s3,
    delete_file_from_s3,
)
import json
import uuid
from .normal import ensure_dir


def create_empty_playbook_config(user_id):
    # Step 1: Prepare config data
    config_data = {user_id: {"playbooklist": []}}

    # Step 2: Generate unique config filename
    config_id = uuid.uuid4().hex[:8]
    filename = f"config_playbook_{config_id}.json"
    local_path = f"/tmp/{filename}"
    s3_key = f"{user_id}/workflow/{filename}"

    # Step 3: Write locally
    with open(local_path, "w") as f:
        json.dump(config_data, f, indent=2)

    # Step 4: Upload to S3
    upload_any_file(
        file_path=local_path, user_id=user_id, file_name=filename, type="workflow"
    )

    # Step 5: Delete local file
    try:
        os.remove(local_path)
    except Exception as e:
        print(f"⚠️ Failed to delete temp config: {e}")

    return s3_key


def update_playbook_config(
    configpath, user_id, name, filepath, title, description, num_steps, status="stopped"
):
    config_filename = "playbooksconfig.json"
    local_config_path = f"data/tmp_json/{config_filename}"
    user_id = str(user_id)
    ensure_dir(os.path.dirname(local_config_path))

    # Step 1: Read existing config from S3
    config_data = read_json_from_s3(configpath)
    if not config_data:
        config_data = {}

    # Step 2: Prepare new entry
    new_entry = {
        "name": name,
        "filepath": filepath,
        "title": title,
        "description": description,
        "num_steps": num_steps,
        "status": status,
        "schedule": {},
        "runtime": {},
    }

    # Step 3: Insert or update entry in playbooklist
    if user_id not in config_data:
        config_data[user_id] = {"playbooklist": []}

    playbook_list = config_data[user_id]["playbooklist"]

    # Replace if exists, else append
    replaced = False
    for i, entry in enumerate(playbook_list):
        if entry["name"] == name:
            playbook_list[i] = new_entry
            replaced = True
            break
    if not replaced:
        playbook_list.append(new_entry)

    # Step 4: Save locally
    with open(local_config_path, "w") as f:
        json.dump(config_data, f, indent=2)

    # Step 5: Upload updated config to S3
    upload_any_file(
        file_path=local_config_path,
        user_id=user_id,
        file_name=configpath,
        type="workflow",
    )

    try:
        os.remove(local_config_path)
        # print(f"🧹 Deleted local temp file: {local_config_path}")
        return True
    except Exception as e:
        # print(f"⚠️ Failed to delete temp file: {e}")
        return False


def update_playbook_clarifications(configpath, user_id, name, clarifications_required):
    """
    Updates the 'clarifications_required' field for a specific playbook entry
    in the user's playbooksconfig.json file.
    """
    config_filename = "playbooksconfig.json"
    local_config_path = f"data/tmp_json/{config_filename}"
    user_id = str(user_id)
    ensure_dir(os.path.dirname(local_config_path))

    # Step 1: Read existing config from S3
    config_data = read_json_from_s3(configpath)
    if not config_data or user_id not in config_data:
        return False  # Cannot update if user or config doesn't exist

    playbook_list = config_data[user_id].get("playbooklist", [])
    updated = False

    # Step 2: Find and update the matching playbook entry
    for entry in playbook_list:
        if entry.get("name") == name:
            entry["clarifications_required"] = clarifications_required
            updated = True
            break

    if not updated:
        return False  # No matching playbook found

    # Step 3: Save locally
    with open(local_config_path, "w") as f:
        json.dump(config_data, f, indent=2)

    # Step 4: Upload updated config to S3
    upload_any_file(
        file_path=local_config_path,
        user_id=user_id,
        file_name=configpath,
        type="workflow",
    )

    try:
        os.remove(local_config_path)
        # print(f"🧹 Deleted local temp file: {local_config_path}")
        return True
    except Exception as e:
        # print(f"⚠️ Failed to delete temp file: {e}")
        return False


def deleteConfigdata(configpath, user_id, name):
    config_filename = "playbooksconfig.json"
    local_config_path = f"data/tmp_json/{config_filename}"
    user_id = str(user_id)

    ensure_dir(os.path.dirname(local_config_path))

    # Normalize name: handle abc / abc.json / KAKA / kaka.JSON
    name = name.strip()
    base_name = name.replace(".json", "").strip()
    json_name = f"{base_name}.json"

    # Step 1: Load config
    config_data = read_json_from_s3(configpath)
    if not config_data:
        # print("⚠️ No config data found.")
        return False

    user_config = config_data.get(user_id, {})
    playbooklist = user_config.get("playbooklist", [])

    # Delete entries matching base name or base_name.json
    allowed_names = {base_name, json_name}

    updated_playbooklist = [
        pb for pb in playbooklist if pb.get("name") not in allowed_names
    ]

    if len(updated_playbooklist) == len(playbooklist):
        # print(f"⚠️ No entry found with name: {name}")
        return False

    config_data[user_id]["playbooklist"] = updated_playbooklist

    # Step 3: Save locally
    with open(local_config_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2)

    # Step 4: Upload updated config to S3
    upload_any_file(file_path=local_config_path, user_id=user_id, file_name=configpath)

    # Step 5: Delete both possible files from S3
    s3_keys_to_delete = [
        f"{user_id}/workflow/{base_name}",
        f"{user_id}/workflow/{json_name}",
    ]

    for s3_key in s3_keys_to_delete:
        delete_folder_from_s3(s3_key)

    # Step 6: Clean local temp file
    try:
        os.remove(local_config_path)
    except:
        pass

    return True


def readconfigstatus(user_id, name):
    # Normalize filename
    filename = name if name.endswith(".json") else f"{name}.json"
    configpath = f"{user_id}/workflow/{filename}"

    # Load workflow JSON from S3
    config_data = read_json_from_s3(configpath)
    if not config_data:
        return None  # File missing or unreadable

    # Get playbook list
    playbook_list = config_data.get("playbooklist", [])
    if not playbook_list:
        return None  # No playbooklist key or empty

    # Find matching entry
    for entry in playbook_list:
        entry_name = entry.get("name")
        if entry_name == name or entry_name == filename:
            return entry.get("status")  # returns None if status missing

    # Not found
    return None


def updateconfigstatus(user_id, name, new_status):
    # Normalize file name
    filename = name if name.endswith(".json") else f"{name}.json"
    configpath = f"{user_id}/workflow/{filename}"

    # Load config data
    config_data = read_json_from_s3(configpath)
    if not config_data:
        return None  # File missing

    playbook_list = config_data.get("playbooklist")
    if not playbook_list:
        return None  # No playbooklist

    # Find the entry
    for entry in playbook_list:
        entry_name = entry.get("name")
        if entry_name == name or entry_name == filename:

            # --- NEW BEHAVIOR: do not update if already same ---
            old_status = entry.get("status")
            if old_status == new_status:
                return False  # nothing to update

            # Update only if status is different
            entry["status"] = new_status

            # Save locally
            local_config_path = f"/tmp/{filename}"
            with open(local_config_path, "w") as f:
                json.dump(config_data, f, indent=2)

            # Upload to S3
            try:
                upload_any_file(
                    file_path=local_config_path,
                    user_id=user_id,
                    file_name=configpath,
                    type="workflow",
                )
                return True
            except Exception as e:
                # print("❌ Upload failed:", e)
                return None

    # Entry not found
    return None
