from datetime import datetime
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
from request_context import current_user_id

logger = get_logger(__name__)


class AutoMateService:
    def __init__(self, userid, testing=False, credits=None, workflow=None, wf_id=None):
        self.userid = userid
        self.connection = connect_to_rds()

        self.autopilot_data = None
        self.testing = testing

        # FIX — ensure workflow is always a dict
        self.workflow = workflow or {}

        # Safe get
        self.inputdata = self.workflow.get("input_data", {})

        self.current_step_id = wf_id
        self.current_step_data = None
        self.credits = credits
        if self.current_step_id:
            self.get_current_step_data()

    def get_current_step_data(self):
        if not self.current_step_id:
            self.current_step_data = None
            return

        workflow_steps = self.workflow.get("workflow", {}).get("steps", [])

        steps = {
            step.get("id"): step
            for step in workflow_steps
            if isinstance(step, dict) and step.get("id")
        }

        self.current_step_data = steps.get(self.current_step_id)

        if not self.current_step_data:
            # Optional: structured error for runner / logs
            raise ValueError(f"Step '{self.current_step_id}' not found in workflow")

    async def create_custom_email_body(self, user_input: str, **args):
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
        email_html = await get_fireworks_response2(
            user_id=self.userid,
            user_message=prompt,
            role="system",
            temp=0.5,
            credits=self.credits,
        )

        # Enforce strict HTML-only output
        # if not email_html.lower().startswith("<html"):
        #     raise ValueError("Model did not return valid HTML email content.")

        return {"email_body_html": email_html.strip()}

    async def generate_file_from_ai(self, user_input: str, **args):
        """
        Generate a real file based on user input using AI.
        Supports txt, md, css, html, docx, pptx.
        Retries AI once if structured JSON parsing fails.
        """
        template_data = load_yaml_file(path=pathconfig.play_template)
        creator_template = template_data.get("generate_file_content")
        output_dir = "data"

        # 1. Generate AI content
        ai_content = await get_fireworks_response2(
            user_message=f"{creator_template['instructions']}\nUser Input: {user_input}",
            role="system",
            temp=0.3,
            user_id=self.userid,
            credits=self.credits,
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
        suggested_file = await get_fireworks_response2(
            user_message=filename_and_type_prompt,
            role="system",
            temp=0.7,
            user_id=self.userid,
            credits=self.credits,
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
                structured_doc = await get_fireworks_response2(
                    user_message=f"""
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
                    user_id=self.userid,
                    credits=self.credits,
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
            structured_ppt = await get_fireworks_response2(
                user_message=f"""
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
                user_id=self.userid,
                credits=self.credits,
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

    async def generate_email_reply(self, previous_msg, **args):
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
            response = await get_fireworks_response2(
                user_message=prompt,
                role="system",
                temp=0.5,
                user_id=self.userid,
                credits=self.credits,
            )
            return response.strip()
        except Exception as e:
            print(f"Error generating email reply: {e}")
            return "Sorry, I couldn't generate an email reply."

    async def generate_chat_reply(self, previous_msg, **args):
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
            response = await get_fireworks_response2(
                user_message=prompt,
                role="system",
                temp=0.4,
                user_id=self.userid,
                credits=self.credits,
            )
            chat_reply = response.strip()
            return {"return_str": chat_reply}
        except Exception as e:
            print(f"Error generating chat reply: {e}")
            return {"error": "encountering a problem please try again"}

    async def generate_ai_content(self, user_input, **args):
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
        step_context = self.current_step_data

        prompt = f"""
        You are a precise, instruction-following AI for text-based tasks.
        
        ===========================
        CURRENT WORKFLOW STEP (REFERENCE)
        ===========================
        {step_context}
        
        You must adapt your behavior strictly based on the USER REQUEST intent.

        ===========================
        INTENT AWARENESS (CRITICAL)
        ===========================
        Determine intent from the USER REQUEST:

        1️⃣ GENERATION MODE
        Triggered by words like:
        "generate", "create", "write", "draft", "compose", "prepare"

        → Create new content as requested.

        2️⃣ QUESTION MODE
        Triggered by:
        "questions", "questionnaire", "list questions", "ask"

        → Generate only questions.
        → Obey any count or format exactly.

        3️⃣ REVIEW MODE
        Triggered by:
        "review", "evaluate", "assess", "validate", "check", "analyze"

        → Do NOT rewrite, summarize, or restate the input.
        → Do NOT polish or reformat unless explicitly asked.
        → Only:
        - Confirm completeness OR
        - Identify gaps, issues, or risks OR
        - State readiness clearly.

        4️⃣ MIXED MODE
        If the request explicitly asks for both review AND generation,
        perform them in the order stated.

        ===========================
        PRIORITIZATION RULES
        ===========================
        1. Follow the USER REQUEST exactly.
        2. Use workflow input data ONLY as reference context.
        3. Use ADDITIONAL CONTEXT (**args) only to apply constraints
        (tone, length, style, count).
        4. If step_context use the step_context ai instruction on how and what kind of details.

        ===========================
        USER REQUEST (HIGHEST PRIORITY)
        ===========================
        {user_input}

        ===========================
        WORKFLOW INPUT DATA (REFERENCE ONLY)
        ===========================
        {input_data}

        ===========================
        ADDITIONAL CONTEXT (**args)
        ===========================
        {args_pretty}

        ===========================
        GLOBAL OUTPUT RULES
        ===========================
        - Never invent facts, emails, names, or data.
        - Never change the meaning of provided content.
        - Never add extra sections or questions unless explicitly requested.
        - If a numeric limit is specified, obey it exactly.
        - Output must be final, clear, and self-contained.
        - Do NOT explain your reasoning.
        - Do NOT mention modes or rules.

        ===========================
        FINAL RESPONSE
        ===========================
        """

        try:
            response = await get_fireworks_response2(
                user_message=prompt,
                role="system",
                temp=0.5,
                user_id=self.userid,
                credits=self.credits,
            )

            generated_content = response.strip()
            return {"return_str": generated_content}
        except Exception as e:
            print(f"Error generating AI content: {e}")
            return {"error": "Sorry, I couldn't generate content right now."}

    async def generate_questions(self, user_input, **args):

        # 🔒 Backend-controlled timestamp prefix
        current_time_prefix = datetime.now().strftime("qid_%Y%m%d_%H%M%S")

        # Safe serialization
        step_dict = self.current_step_data or {}

        step_context = json.dumps(step_dict, indent=2, ensure_ascii=False)

        # print("step data", step_context)

        ai_instruction = step_dict.get("ai_instructions", "")
        # print("AI INSTRUCTION", ai_instruction)

        # print("INP DATA", user_input)

        args_pretty = json.dumps(args, indent=2, ensure_ascii=False)

        prompt = f"""
            You are a STRICT workflow-bound question generation engine.
            You are operating inside a LOCKED workflow step.

            ===========================
            WORKFLOW STEP AUTHORITY (PRIMARY)
            ===========================
            {step_context}

            - The ABOVE step definition is the ABSOLUTE authority.
            - It determines:
            • question type (normal / MCQ / quiz)
            • output structure
            • constraints
            - You MUST follow it even if the user input is vague or incomplete.

            ===========================
            AI INSTRUCTIONS (HIGHEST PRIORITY)
            ===========================
            The following instructions DEFINE the type of questions to generate.
            They OVERRIDE user phrasing.

            {ai_instruction}

            ===========================
            DEFAULT BEHAVIOR (ONLY IF NOT OVERRIDDEN ABOVE)
            ===========================
            - Generate NORMAL (open-ended) questions by default.
            - Generate MCQs ONLY if explicitly required by AI INSTRUCTIONS.
            - NEVER infer MCQs from user wording alone.

            ===========================
            QUESTION QUALITY RULES (MANDATORY)
            ===========================
            - Assessment-grade questions only.
            - Focus on reasoning, implications, trade-offs, systems thinking.
            - Avoid definitions, recall, or introductory questions.

            ===========================
            ID GENERATION (CRITICAL)
            ===========================
            - Every question MUST include an "id".
            - IDs MUST start with EXACT prefix:

            {current_time_prefix}

            - Append a unique suffix per question (_001, _002, _a1).
            - IDs must be strings and never repeat.

            ===========================
            OUTPUT FORMAT (MANDATORY)
            ===========================
            Return ONLY a JSON ARRAY.

            NORMAL QUESTION:
            {{
            "id": "{current_time_prefix}_<unique_suffix>",
            "question": "<question text>",
            "answer": null
            }}

            MCQ QUESTION (ONLY IF REQUIRED BY AI INSTRUCTIONS):
            {{
            "id": "{current_time_prefix}_<unique_suffix>",
            "question": "<question text>",
            "options": {{
                "A": "<option>",
                "B": "<option>",
                "C": "<option>",
                "D": "<option>"
            }},
            "answer": null
            }}

            ===========================
            USER INPUT (TOPIC ONLY — NOT AUTHORITY)
            ===========================
            Use this ONLY as the subject matter or theme.
            Do NOT infer format or structure from it.

            {user_input}

            ===========================
            ADDITIONAL CONSTRAINTS (**args)
            ===========================
            {args_pretty}

            ===========================
            GLOBAL OUTPUT RULES
            ===========================
            - Output ONLY the JSON array
            - No markdown
            - No comments
            - No explanations
            - Must be execution-ready

            FINAL RESPONSE:
            """

        try:
            response = await get_fireworks_response2(
                user_message=prompt,
                role="system",
                temp=0.3,
                user_id=self.userid,
                credits=self.credits,
            )

            questions = json.loads(response.strip())
            return {"questions": questions}

        except json.JSONDecodeError:
            return {
                "error": "Invalid response format from AI",
                "raw_response": response.strip(),
            }
        except Exception as e:
            print(f"Error generating questions: {e}")
            return {"error": "Sorry, I couldn't generate questions right now."}

    async def review_content(self, user_input, **args):
        """
        Professionally reviews user-provided content against workflow AI instructions.
        The reviewer explains what is OK, what is NOT OK, and how to improve.
        It NEVER rewrites or generates content.
        """

        try:
            # 🔒 Backend-controlled timestamp prefix
            current_time_prefix = datetime.now().strftime("qid_%Y%m%d_%H%M%S")

            # 🧠 Step context
            step_dict = self.current_step_data or {}
            step_context = json.dumps(step_dict, indent=2, ensure_ascii=False)

            # 📌 AI instruction
            ai_instruction = step_dict.get("ai_instructions", "")

            # 📦 Runtime arguments
            args_pretty = json.dumps(args, indent=2, ensure_ascii=False)

            # 📝 Review prompt
            prompt = f"""
            You are a PROFESSIONAL CONTENT REVIEWER operating inside a workflow execution system.

            ============================
            YOUR ROLE
            ============================
            You must REVIEW the provided content in a professional, constructive manner.

            ❌ Do NOT rewrite the content  
            ❌ Do NOT generate new content  
            ❌ Do NOT add examples or alternatives  
            ❌ Do NOT ask questions  

            Your job is to evaluate and give feedback only.

            ============================
            STEP CONTEXT
            ============================
            Workflow Step:
            {step_context}

            AI Instruction:
            "{ai_instruction}"

            ============================
            CONTENT TO REVIEW
            ============================
            {user_input}

            ============================
            RUNTIME DATA
            ============================
            Arguments:
            {args_pretty}

            Timestamp Reference (DO NOT MODIFY):
            {current_time_prefix}

            ============================
            REVIEW CRITERIA
            ============================
            Evaluate the content based on:

            1. Alignment with the AI instruction
            2. Accuracy and relevance
            3. Completeness
            4. Clarity and professional tone

            ============================
            RESPONSE FORMAT (MANDATORY)
            ============================
            Use EXACTLY the following structure:

            Overall Status:
            - OK | NEEDS IMPROVEMENT | NOT OK

            What Is Good:
            - <clearly state what works well>
            - <be specific and professional>

            What Is Not Good:
            - <clearly state issues or gaps>
            - <mention only real problems>

            How to Improve:
            - <actionable guidance>
            - <do NOT rewrite the content>
            - <do NOT add examples>

            ============================
            STRICT RULES
            ============================
            - Be professional and concise
            - No emojis
            - No markdown
            - No policy or system mentions
            - Plain text only
            - Output will be consumed directly by a workflow runner
            """

            # 🤖 LLM call
            response = await get_fireworks_response2(
                user_message=prompt,
                role="system",
                temp=0.4,
                user_id=self.userid,
                credits=self.credits,
            )

            review_output = response.strip()

            if not review_output:
                return {"error": "Review failed: empty response from reviewer."}

            return {"return_str": review_output}

        except Exception as e:
            print(f"Error reviewing content: {e}")
            return {"error": "Sorry, I couldn't review the content right now."}
