import os
from pathlib import Path
import yaml
import re
import json

from docx import Document
from pptx import Presentation


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


def can_reply_to_email(email: str) -> bool:
    """
    Returns True if it is safe to reply to this email.
    Rules:
    1. Skip system/no-reply/admin/bounce/automation emails.
    2. Skip emails from blocked domains (both .com and .in).
    3. Only allow .com and .in emails; reject others.
    """
    if not email:
        return False

    email = email.lower().strip()

    # 1️⃣ Blocked keywords in local part
    blocked_keywords = [
        "no-reply",
        "noreply",
        "donotreply",
        "do-not-reply",
        "system",
        "postmaster",
        "bounce",
        "mailer-daemon",
        "undeliverable",
        "return",
        "notifications",
        "alerts",
        "updates",
        "robot",
        "automation",
    ]
    if any(keyword in email for keyword in blocked_keywords):
        return False

    # 2️⃣ Blocked domain names (without TLD)
    blocked_domains = [
        "naukri",
        "google",
        "amazon",
        "twitter",
        "x",
        "indeed",
        "train",
        "zomato",
        "swiggy",
        "ola",
        "rapido",
        "linkedin",
        "microsoft",
        "facebook",
        "instagram",
        "youtube",
        "flipkart",
        "ubereats",
        "booking",
        "airbnb",
        "phonepe",
        "upstox",
        "irctc",
        "aubank",
        "kotak",
        "railone",
    ]

    # Extract domain name and TLD
    match = re.match(r"^[\w\.-]+@([\w-]+)\.(\w+)$", email)
    if not match:
        return False

    domain_name, tld = match.groups()

    # 3️⃣ Only allow .com and .in emails
    if tld not in {"com", "in"}:
        return False

    # 2️⃣ Block specific domains
    if domain_name in blocked_domains:
        return False

    return True


def read_function_jsons():
    """
    Reads all function JSON files and returns a string formatted for the prompt,
    including function name, description, and arguments.
    """
    paths = [
        "playbook/fn_configs/autopilot_functions.json",
        "playbook/fn_configs/gmail_functions.json",
        "playbook/fn_configs/google_meet_functions.json",
        "playbook/fn_configs/twillo_fucntion.json",
    ]
    all_functions_details = []

    for path in paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except json.JSONDecodeError:
                continue

            for fn in data.get("functions", []):
                fn_name = fn.get("name", "Unknown Function")
                fn_description = fn.get("description", "No description provided.")
                fn_args = fn.get("args", {})

                args_list = [
                    f"    - {arg_name}: {arg_desc}"
                    for arg_name, arg_desc in fn_args.items()
                ]
                args_str = "\n".join(args_list) or "    - None"

                function_detail = (
                    f"### Function: **{fn_name}**\n"
                    f"Description: {fn_description}\n"
                    f"Arguments:\n"
                    f"{args_str}"
                )
                all_functions_details.append(function_detail)

    return "\n\n".join(all_functions_details)


from typing import Union


def parse_pptx_content_to_json(content: str) -> dict:
    slides_data = {"slides": []}
    current_slide = None

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        # Detect slide heading: Roman numeral or numbered heading
        if re.match(r"^[IVXLCDM]+\. ", line):  # I., II., III., etc.
            # Save previous slide
            if current_slide:
                slides_data["slides"].append(current_slide)
            current_slide = {"title": "", "bulletPoints": []}
            continue

        # Detect title line
        title_match = re.match(r"^\* Title:\s*(.*)", line)
        if title_match:
            if current_slide is None:
                current_slide = {"title": "", "bulletPoints": []}
            current_slide["title"] = title_match.group(1)
            continue

        # Detect bullet lines
        bullet_match = re.match(r"^\* (?!Title:)(.*)", line)
        if bullet_match and current_slide:
            current_slide["bulletPoints"].append(bullet_match.group(1))
            continue

    if current_slide:
        slides_data["slides"].append(current_slide)

    return slides_data


def prepare_docx_data_from_ai(ai_content: str) -> dict:
    """
    Convert AI content (raw string or JSON) into doc_data format for .docx:
    {
        "title": "...",
        "sections": [{"heading": "...", "paragraphs": ["..."]}]
    }
    """
    # Try parsing ai_content as JSON first
    try:
        parsed = json.loads(ai_content)
        if "content" in parsed:
            content_text = parsed["content"].strip()
        else:
            content_text = ai_content.strip()
    except (json.JSONDecodeError, TypeError):
        content_text = ai_content.strip()

    # Split into lines
    lines = [line.strip() for line in content_text.splitlines() if line.strip()]

    # First line as title
    title = lines[0] if lines else "Document"

    # Split rest into sections
    sections = []
    current_heading = ""
    current_paragraphs = []

    for line in lines[1:]:
        if (
            line.endswith(":")
            or line.startswith("###")
            or line.endswith("------------")
        ):
            # Treat as a heading
            if current_heading or current_paragraphs:
                sections.append(
                    {
                        "heading": current_heading or "Section",
                        "paragraphs": current_paragraphs,
                    }
                )
            current_heading = line.replace("###", "").replace("-", "").strip()
            current_paragraphs = []
        else:
            current_paragraphs.append(line)
    # Append last section
    if current_heading or current_paragraphs:
        sections.append(
            {
                "heading": current_heading or "Section",
                "paragraphs": current_paragraphs or ["Content unavailable"],
            }
        )

    return {
        "title": title,
        "sections": sections
        or [{"heading": "Section 1", "paragraphs": ["Content unavailable"]}],
    }


def save_docx_from_json(doc_data, file_path):
    """Save structured Word content as a .docx file, replacing if exists"""
    file_path = Path(file_path)
    if file_path.exists():
        file_path.unlink()  # Delete existing file
    print("doc data", doc_data)

    doc = Document()
    doc.add_heading(doc_data.get("title", ""), level=0)
    for section in doc_data.get("sections", []):
        doc.add_heading(section.get("heading", ""), level=1)
        for para in section.get("paragraphs", []):
            doc.add_paragraph(para)
    doc.save(file_path)
    # os.remove(file_path)


def save_pptx_from_json(slide_data, file_path):
    """Save structured PowerPoint content as a .pptx file, replacing if exists"""
    file_path = Path(file_path)
    if file_path.exists():
        file_path.unlink()  # Delete existing file
    print("slide data", slide_data)

    prs = Presentation()
    for slide_info in slide_data.get("slides", []):
        slide_layout = prs.slide_layouts[1]  # Title and Content
        slide = prs.slides.add_slide(slide_layout)
        slide.shapes.title.text = slide_info.get("title", "")
        content = "\n".join(slide_info.get("bulletPoints", []))
        slide.placeholders[1].text = content
    prs.save(file_path)
    # os.remove(file_path)
