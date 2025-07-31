import os
import yaml
def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
def load_yaml_file(path):
    if not os.path.exists(path):
        return None  # Important: use None to differentiate from empty list
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or []
    except yaml.YAMLError as e:
        print(f"❌ Error reading YAML file at {path}: {e}")
        return []
