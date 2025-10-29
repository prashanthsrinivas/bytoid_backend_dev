import json
from datetime import datetime
from create_db import connect_to_rds
from cust_helpers import pathconfig
from db.db_checkers import get_business_info, get_users_clients_id
from flask import jsonify, request
import pymysql
import uuid, re
from pathlib import Path
from suggest_assist.suggest_helper import (
    getselectedconv,
    helper_make_reply_email,
    send_pilot_messages,
    suggest_helper_base,
    umail_get_sorted_lance_emails,
)
from utils.base_logger import get_logger
from utils.fireworkzz import get_fireworks_response
from utils.normal import (
    can_reply_to_email,
    load_yaml_file,
    parse_pptx_content_to_json,
    prepare_docx_data_from_ai,
    save_docx_from_json,
    save_pptx_from_json,
)

logger = get_logger(__name__)


class AutoMateService:
    def __init__(self, userid, testing=False, workflow=None, wf_id=None):
        self.userid = userid
        self.connection = connect_to_rds()
        self.autopilot_data = None
        self.testing = testing
        self.workflow = workflow
        self.current_wf_id = wf_id

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.connection:
            self.connection.close()

    def create_custom_email_body(self, user_input: str, **args):
        """
        Generates a modern, professionally designed HTML email using user_input and dynamic data.
        The AI must return only a fully designed HTML string (no fallbacks, no text, no markdown).
        """

        # Fetch business info
        business_info = (
            get_business_info(userid=self.userid, connection=self.connection) or {}
        )

        # Format dynamic args for prompt readability
        dynamic_info = ""
        for key, value in args.items():
            if isinstance(value, list):
                value_str = ", ".join(map(str, value))
            else:
                value_str = str(value)
            dynamic_info += f"{key.replace('_', ' ').title()}: {value_str}\n"

        business_name = business_info.get("BusinessName", "")
        billing_address = business_info.get("BillingAddress", "")
        website = business_info.get("WebsiteUrl", "")
        logo_url = business_info.get("LogoUrl", "")

        # Strictly enforce modern HTML email design
        prompt = f"""
    You are a professional email designer and marketer.

    Create a **modern, elegant, mobile-responsive HTML email** with inline CSS.

    ### Output Rules
    - Return only valid HTML (starts with `<html>` and ends with `</html>`).  
    - Use `<table>` layout with inline CSS for maximum email client compatibility.  
    - Use **modern visual design**:
    - White background, soft shadows, subtle color palette (light blue / gray / accent color).
    - Rounded corners for main container and buttons.
    - Include padding and spacing between sections.
    - Include:
    1. A header section with the business name and logo (if available)
    2. A warm greeting line
    3. A clear and concise main body text relevant to the user’s request
    4. A **CTA button** (only if relevant) styled with a primary color (#007BFF or similar)
    5. A footer with business name, address, and website
    - The tone should be friendly, confident, and professional.
    - The layout should be **max-width: 600px** and center-aligned.
    - Do **not** include any explanation or text outside the HTML.
    - Make sure the output is well-formatted HTML.
    - if timings or dates present make them into natural like 23 november 2025 or 12 am or 12:30 am

    ---

    **User Request:**
    "{user_input}"

    **Dynamic Info (if any):**
    {dynamic_info}

    **Business Details:**
    Business Name: {business_name}
    Address: {billing_address}
    Website: {website}
    Logo URL: {logo_url}

    ---

    Return only the final HTML email — no extra commentary, no markdown.
    """

        # Get designed HTML from Fireworks model
        email_html = get_fireworks_response(prompt, "system").strip()

        # Enforce strict HTML-only output
        # if not email_html.lower().startswith("<html"):
        #     raise ValueError("Model did not return valid HTML email content.")

        return {"email_body_html": email_html}

    # -------------------- Autopilot Data Fetch/Save --------------------
    def fetch_autopilot(self):
        with self.connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT autopilot FROM users WHERE user_id = %s LIMIT 1", (self.userid,)
            )
            row = cursor.fetchone()
            if not row:
                return None, {"error": "user not found"}, 404

            autopilot_data = row.get("autopilot") or {}
            if isinstance(autopilot_data, str):
                try:
                    autopilot_data = json.loads(autopilot_data)
                except json.JSONDecodeError:
                    autopilot_data = {}

            # Ensure structure
            autopilot_data.setdefault("mode", "dynamic")
            autopilot_data.setdefault("logs", [])

            self.autopilot_data = autopilot_data
            return autopilot_data, None, None

    def persist_autopilot(self):
        with self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE users SET autopilot = %s WHERE user_id = %s",
                (json.dumps(self.autopilot_data), self.userid),
            )
        self.connection.commit()

    def suggest_reply(self, email_msg, conv_id):
        try:
            umail_conversations = getselectedconv(conv_id=conv_id, userid=self.userid)
            umail_bodies = [msg.get("body", "") for msg in umail_conversations]
            print("umail conversations", umail_bodies)
            ai_reply = suggest_helper_base(
                userid=self.userid,
                email_msg=email_msg,
                umail_conversations=umail_conversations,
                umail_bodies=umail_bodies,
            )
            if ai_reply:
                return jsonify({"message": ai_reply.strip()}), 200
            return jsonify({"error": "Cannot generate AI suggestion"}), 400
        except Exception as e:
            logger.error("AI suggest error: %s", e)
            return jsonify({"error": "Cannot generate AI suggestion"}), 500

    # -------------------- AI Suggestion & Auto-reply --------------------
    def auto_reply_email(self, from_email):
        try:
            clientid = get_users_clients_id(email=from_email, user_id=self.userid)
            if not clientid:
                return jsonify({"error": "No email communication found"}), 404

            sorted_conversations = umail_get_sorted_lance_emails(
                connection=self.connection, user_id=self.userid, client_id=clientid
            )
            all_messages = [
                msg for conv in sorted_conversations for msg in conv.get("messages", [])
            ]
            if not all_messages:
                return jsonify({"error": "No messages found"}), 404

            latest_msg = all_messages[-1]
            if not can_reply_to_email(latest_msg.get("from")):
                return jsonify({"status": "cannot_reply"}), 200

            if latest_msg.get("direction") == "inbound":
                ai_reply = suggest_helper_base(
                    userid=self.userid,
                    email_msg=latest_msg["body"],
                    umail_conversations=all_messages,
                    umail_bodies=[msg.get("body") for msg in all_messages],
                )
                if ai_reply:
                    send_pilot_messages(
                        user_id=self.userid,
                        channel="gmail",
                        text=ai_reply,
                        conversation_id=latest_msg["conversation_id"],
                        b_connection=self.connection,
                        client_id=clientid,
                        user_email=latest_msg["to"],
                        client_email=latest_msg["from"],
                        subject=latest_msg["subject"],
                        thread_id=latest_msg["thread_id"],
                        ticket_id=latest_msg["ticket_id"],
                        ticket_name=latest_msg["ticket_name"],
                        is_reply=True,
                    )
                    return True
                return False
            return False
        except Exception as e:
            logger.error("Auto-reply error: %s", e)
            return None

    # -------------------- Autopilot Operations --------------------
    def activate_autopilot(self, from_email, selected_agents=None):
        autopilot_data, err, code = self.fetch_autopilot()
        if err:
            return jsonify(err), code

        mode = "all" if from_email == "ALL" else "dynamic"
        self.autopilot_data["mode"] = mode

        emails = [from_email] if isinstance(from_email, str) else from_email
        already_active = True

        for email in emails:
            existing_entry = next(
                (e for e in autopilot_data["logs"] if e["email"] == email), None
            )
            if (
                not existing_entry
                or existing_entry.get("status") != "active"
                or existing_entry.get("selected_agent") != selected_agents
            ):
                now = datetime.utcnow().isoformat()
                update_data = {
                    "email": email,
                    "status": "active",
                    "last-conv": None,
                    "last-msg": None,
                    "updated_at": now,
                    "selected_agent": selected_agents or self.userid,
                }
                if existing_entry:
                    idx = autopilot_data["logs"].index(existing_entry)
                    autopilot_data["logs"][idx] = update_data
                else:
                    autopilot_data["logs"].append(update_data)
                already_active = False

            helper_make_reply_email(
                userid=self.userid, from_email=email, n_connection=self.connection
            )

        if not already_active:
            self.persist_autopilot()

        msg = "autopilot already active" if already_active else "autopilot activated"
        return jsonify({"message": msg, "autopilot": autopilot_data}), 200

    def revoke_autopilot(self, target_email, pilot_override=False):
        autopilot_data, err, code = self.fetch_autopilot()
        if err:
            return jsonify(err), code

        emails = [target_email] if isinstance(target_email, str) else target_email
        logs = autopilot_data.get("logs", [])
        now = datetime.utcnow().isoformat()
        revoked_any = False

        for email in emails:
            if email == "ALL":
                for log in logs:
                    log["status"] = "revoked"
                    log["updated_at"] = now
                revoked_any = True
                if pilot_override:
                    autopilot_data["mode"] = "dynamic"
            else:
                entry = next((e for e in logs if e["email"] == email), None)
                if entry:
                    entry["status"] = "revoked"
                    entry["updated_at"] = now
                    revoked_any = True
                    if pilot_override and autopilot_data.get("mode") == "all":
                        autopilot_data["mode"] = "dynamic"

        if not revoked_any:
            return jsonify({"error": "email(s) not found in autopilot logs"}), 404

        autopilot_data["logs"] = logs
        self.autopilot_data = autopilot_data
        self.persist_autopilot()
        return (
            jsonify({"message": "Autopilot revoked", "autopilot": autopilot_data}),
            200,
        )

    def change_mode(self, new_mode):
        autopilot_data, err, code = self.fetch_autopilot()
        if err:
            return jsonify(err), code

        current_mode = autopilot_data.get("mode")
        if current_mode == new_mode:
            return (
                jsonify({"message": f"Autopilot mode already set to '{new_mode}'"}),
                200,
            )

        autopilot_data["mode"] = new_mode
        self.autopilot_data = autopilot_data
        self.persist_autopilot()
        msg = f"Autopilot mode changed from '{current_mode}' to '{new_mode}'"
        return jsonify({"message": msg, "autopilot": autopilot_data}), 200

    def reset_autopilot(self):
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT user_id FROM users WHERE user_id = %s LIMIT 1", (self.userid,)
            )
            if not cursor.fetchone():
                return jsonify({"error": "User not found"}), 404

            cursor.execute(
                "UPDATE users SET autopilot = NULL WHERE user_id = %s", (self.userid,)
            )
            self.connection.commit()
        return jsonify({"message": f"Autopilot data reset for user {self.userid}"}), 200

    def generate_file_from_ai(self, user_input: str):
        """
        Generate a real file based on user input using AI.
        Supports txt, md, css, html, docx, pptx.
        Retries AI once if structured JSON parsing fails.
        """
        template_data = load_yaml_file(path=pathconfig.play_template)
        creator_template = template_data.get("generate_file_content")
        output_dir = "data"

        # 1. Generate AI content
        ai_content = get_fireworks_response(
            f"{creator_template['instructions']}\nUser Input: {user_input}",
            role="system",
        )
        # 🔹 Extract only the JSON-like part using regex
        json_match = re.search(r"\{[\s\S]*\}", ai_content)
        if json_match:
            ai_content = json_match.group(0).strip()
        print("ai content received", ai_content)

        # 2. Ask AI to suggest filename and file type
        filename_and_type_prompt = (
            f"Based on this content description: '{user_input}', "
            "suggest a short, descriptive filename (underscores, no spaces) "
            "and a suitable file type/extension (txt, md, docx, pptx, css, html). "
            "Return only as 'filename.extension'."
        )
        suggested_file = get_fireworks_response(
            filename_and_type_prompt, role="system"
        ).strip()

        # 3. Fallback if AI returns invalid
        if not suggested_file or "." not in suggested_file:
            suggested_file = f"{uuid.uuid4()}.txt"
        # print("filename created", suggested_file)

        # 4. Ensure output directory exists
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        file_path = Path(output_dir) / suggested_file
        ext = file_path.suffix.lower()

        # Helper for safe JSON parsing
        def parse_ai_json(ai_response):
            try:
                return json.loads(ai_response)
            except json.JSONDecodeError:
                return None

        # 5. Save content based on file type
        if ext in [".txt", ".md", ".css", ".html"]:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(ai_content)

        elif ext == ".docx":
            if not ai_content:
                structured_doc = get_fireworks_response(
                    f"""
                    You are an expert content writer and Word document designer.

                    Generate a structured Word document based on this input:
                    '{user_input}'

                    Return **valid JSON only** with the following keys:
                    - "title": the main document title
                    - "sections": a list of sections, each with:
                        - "heading": section heading
                        - "paragraphs": list of paragraphs as strings
                    Do not include any extra text or markdown outside JSON.
                    """,
                    role="system",
                )
            else:
                structured_doc = ai_content
            # 2️⃣ Extract JSON from any extra text (AI may return extra characters)
            json_match = re.search(r"\{[\s\S]*\}", structured_doc)
            if json_match:
                structured_doc = json_match.group(0).strip()

            # 3️⃣ Parse JSON
            try:
                doc_data = json.loads(structured_doc)
            except json.JSONDecodeError:
                doc_data = None

            doc_data = prepare_docx_data_from_ai(structured_doc)

            # 5️⃣ Save the .docx using your existing helper
            save_docx_from_json(doc_data, file_path)

        elif ext == ".pptx":
            # Generate structured PPT JSON using the full AI content
            structured_ppt = get_fireworks_response(
                f"""
                You are an expert PowerPoint presentation designer.

                Based on the following detailed content, generate a complete, visually engaging slideshow structure:
                ---
                {ai_content}
                ---

                ## OUTPUT REQUIREMENTS:
                - Return valid JSON only, with a top-level key "slides".
                - Each slide must include:
                - "title": short and clear (<= 12 words)
                - "bulletPoints": list of 2–5 concise points
                - "visuals" (optional): background/image ideas
                - "animation" (optional): animation/transition suggestion
                - "style" (optional): slide style like "corporate", "modern", "minimal", etc.
                - Avoid markdown or formatting outside JSON.
                - Focus on stunning visual storytelling — transitions, layouts, imagery.
                """,
                role="system",
            )

            # 🔹 Extract JSON from any extra text first
            json_match = re.search(r"\{[\s\S]*\}", structured_ppt)
            if json_match:
                structured_ppt = json_match.group(0).strip()

            # 🔹 Parse JSON
            slide_data = parse_ai_json(structured_ppt)

            # 🔁 Retry with manual fallback if JSON is invalid or no slides
            if slide_data is None or not slide_data.get("slides"):
                print("Attempting to parse raw AI content for PPTX...")
                slide_data = parse_pptx_content_to_json(ai_content)
                print("the pptx structured final", slide_data)

            if slide_data is None or not slide_data.get("slides"):
                return {"error": "Cannot create the file"}

            # 🔹 Save the structured PPT
            save_pptx_from_json(slide_data, file_path)

        else:
            # Default to plain text
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(ai_content)

        return str(file_path)
