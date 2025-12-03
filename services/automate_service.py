import json
from create_db import connect_to_rds
from cust_helpers import pathconfig
from db.db_checkers import get_business_info
import uuid, re
from pathlib import Path
from utils.base_logger import get_logger
from utils.fireworkzz import get_fireworks_response2
from utils.normal import (
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

        # FIX — ensure workflow is always a dict
        self.workflow = workflow or {}

        # Safe get
        self.inputdata = self.workflow.get("input_data", {})

        self.current_wf_id = wf_id

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
        if args:
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
    You are a professional email designer and marketer as per User Request.

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
        email_html = get_fireworks_response2(prompt, "system", temp=0.5).strip()

        # Enforce strict HTML-only output
        # if not email_html.lower().startswith("<html"):
        #     raise ValueError("Model did not return valid HTML email content.")

        return {"email_body_html": email_html}

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
        ai_content = get_fireworks_response2(
            f"{creator_template['instructions']}\nUser Input: {user_input}",
            role="system",
            temp=0.3,
        )
        # 🔹 Extract only the JSON-like part using regex
        json_match = re.search(r"\{[\s\S]*\}", ai_content)
        if json_match:
            ai_content = json_match.group(0).strip()
        # print("ai content received", ai_content)

        # 2. Ask AI to suggest filename and file type
        filename_and_type_prompt = (
            f"Based on this content description: '{user_input}', "
            "suggest a short, descriptive filename (underscores, no spaces) "
            "and a suitable file type/extension (txt, md, docx, pptx, css, html). "
            "Return only as 'filename.extension'."
        )
        suggested_file = get_fireworks_response2(
            filename_and_type_prompt, role="system", temp=0.7
        ).strip()

        # 3. Fallback if AI returns invalid
        if not suggested_file or "." not in suggested_file:
            suggested_file = f"{uuid.uuid4()}.txt"
        ##print("filename created", suggested_file)

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
                structured_doc = get_fireworks_response2(
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
                    temp=0.6,
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
            structured_ppt = get_fireworks_response2(
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
                temp=0.6,
            )

            # 🔹 Extract JSON from any extra text first
            json_match = re.search(r"\{[\s\S]*\}", structured_ppt)
            if json_match:
                structured_ppt = json_match.group(0).strip()

            # 🔹 Parse JSON
            slide_data = parse_ai_json(structured_ppt)

            # 🔁 Retry with manual fallback if JSON is invalid or no slides
            if slide_data is None or not slide_data.get("slides"):
                # print("Attempting to parse raw AI content for PPTX...")
                slide_data = parse_pptx_content_to_json(ai_content)
            # print("the pptx structured final", slide_data)

            if slide_data is None or not slide_data.get("slides"):
                return {"error": "Cannot create the file"}

            # 🔹 Save the structured PPT
            save_pptx_from_json(slide_data, file_path)

        else:
            # Default to plain text
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(ai_content)

        return str(file_path)

    def generate_email_reply(self, previous_msg):
        """
        Generate an AI-based email reply using Fireworks.
        """
        prompt = f"""
            You are an AI email assistant. Based on the previous email conversation below, 
            draft a polite, professional, and relevant reply.

            Previous Email:
            {previous_msg}

            Your Reply:
            """
        try:
            response = get_fireworks_response2(prompt, role="system", temp=0.5).strip()
            return response
        except Exception as e:
            print(f"Error generating email reply: {e}")
            return "Sorry, I couldn't generate an email reply."

    def generate_chat_reply(self, previous_msg, **args):
        """
        Generates a friendly and conversational reply based on a previous chat message.
        Accepts any dynamic context via **args such as intent, workflow data, chats, or AI instructions.
        """
        # Serialize args for visibility in prompt
        args_pretty = json.dumps(args, indent=2, ensure_ascii=False)
        if self.workflow:
            input_data = json.dumps(self.inputdata or {}, indent=2, ensure_ascii=False)
            allchats = self.workflow.get("chat", [])
            chat_log = self.workflow.get("chat_log", {})
            basechats = allchats
            last_summarization = None
            if chat_log:
                last_chat_check = chat_log.get("last_chat_summarized")
                last_summarization = chat_log.get("chat_summarization") or ""
                if last_chat_check:
                    basechats = allchats[-10:]

            prompt = f"""
                    You are a friendly, intelligent conversational AI assistant.
                    Your goal is to continue the chat naturally and contextually.

                    ==========================
                    CONTEXT DATA
                    ==========================
                    🗨️ Previous Message:
                    {previous_msg}

                    💡 Workflow Input Data:
                    {input_data}

                    💬 Previous Chats:
                    {basechats}

                    🧠 Last Chat Summary:
                    {last_summarization}

                    ⚙️ Additional Dynamic Inputs:
                    {args_pretty}

                    ==========================
                    INSTRUCTIONS
                    ==========================
                    - Respond naturally, conversationally, and helpfully.
                    - Use any relevant details from workflow data, chat history, or dynamic inputs.
                    - Stay concise — 2 to 4 sentences is ideal unless elaboration is required.
                    - Maintain context; connect your reply to the ongoing conversation.
                    - Never invent or assume information not present in the given context.
                    - Avoid repeating the user’s message verbatim.
                    - If the user asks for something not found in the provided data, acknowledge it gracefully (e.g., “I’m not sure about that, could you clarify?”).
                    - Do not output explanations, reasoning, or system text — only the assistant’s message.

                    ==========================
                    Assistant Reply:
                    """

        else:
            prompt = f"""
            You are an intelligent and friendly AI assistant.
            Continue the chat naturally based on the previous message and dynamic context.

            --------------------------
            PREVIOUS MESSAGE
            --------------------------
            {previous_msg}
            
            --------------------------
            ADDITIONAL CONTEXT (**args)
            --------------------------
            {args_pretty}

            --------------------------
            YOUR TASK
            --------------------------
            - Respond naturally and contextually.
            - Use any helpful information from the arguments (intent, workflow, chat history, etc.).
            - Keep tone conversational, concise, and relevant.
            - Do not repeat the user’s message verbatim.
            - If workflow data or dynamic_inputs are present, use them to maintain context.

            Assistant Reply:
            """

        try:
            chat_reply = get_fireworks_response2(
                prompt, role="system", temp=0.4
            ).strip()
            return {"return_str": chat_reply}
        except Exception as e:
            print(f"Error generating chat reply: {e}")
            return {"error": "encountering a problem please try again"}

    def generate_ai_content(self, user_input, **args):
        """
        Generates creative or informational content based on user instructions.
        Prioritization:
        1️⃣ User input (main instruction)
        2️⃣ Workflow input data (self.inputdata)
        3️⃣ Additional args (**args)
        """
        # JSON representations of inputs
        input_data = json.dumps(self.inputdata or {}, indent=2, ensure_ascii=False)
        args_pretty = json.dumps(args, indent=2, ensure_ascii=False)

        # Construct a prompt emphasizing prioritization
        prompt = f"""
            You are a versatile and creative AI content generator.

            PRIORITIZATION RULES:
            1. Follow the USER REQUEST first.
            2. Use workflow input data (below) only if it adds context or detail.
            3. Use ADDITIONAL CONTEXT (**args) only as supporting information.

            --------------------------
            USER REQUEST (Highest Priority)
            --------------------------
            {user_input}

            --------------------------
            Workflow Input Data (Secondary)
            --------------------------
            {input_data}

            --------------------------
            Additional Context (**args, Lowest Priority)
            --------------------------
            {args_pretty}

            --------------------------
            YOUR TASK
            --------------------------
            - Generate content that strictly follows the USER REQUEST first.
            - Ensure output respects any constraints in **args** (tone, length, etc.).
            - Output must be clear, self-contained, and final.
            - Do not add unnecessary extra questions or content unless requested.
            - If the user specifies a number (like max 10 questions), obey it exactly.

            Generated Content:
        """

        try:
            generated_content = get_fireworks_response2(
                prompt, role="system", temp=0.5
            ).strip()
            return {"return_str": generated_content}
        except Exception as e:
            print(f"Error generating AI content: {e}")
            return {"error": "Sorry, I couldn't generate content right now."}
