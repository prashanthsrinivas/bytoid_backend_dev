import json
import os
import boto3
import requests
import uuid
import hashlib
import tempfile  # For test endpoints only
import asyncio  # For async Whisper transcription
import time
import threading
from io import BytesIO
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, session
from utils.base_logger import get_logger
from utils.s3_utils import s3bucket, attach_CLDFRNT_url
from utils.content_filter import (
    is_inappropriate_content,
    get_filtered_response,
    quick_content_check,
    should_allow_question,
    get_bytoid_focused_response,
)
from dotenv import load_dotenv
from botocore.exceptions import ClientError
from agent_route.s_t_s import Speech2TextService
import re

# Initialize blueprint and logger
onboarding_bps = Blueprint("onboarding", __name__)
logger = get_logger(__name__)
load_dotenv()

# Get S3 configuration from environment
S3_BUCKET = os.getenv("S3_BUCKET")
if not S3_BUCKET:
    logger.error("❌ S3_BUCKET environment variable not set")

# Required environment variables
REQUIRED_ENV_VARS = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "S3_BUCKET"]
missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
if missing_vars:
    logger.warning(f"⚠️ Missing environment variables: {missing_vars}")

# FAQ Cache for performance optimization
FAQ_CACHE = {
    "all_questions": None,
    "last_updated": None,
    "cache_duration": 300,  # 5 minutes cache
}

# Thread lock for cache updates
cache_lock = threading.Lock()

# Request deduplication for voice conversation to prevent duplicate generation
voice_request_cache = {}
voice_request_lock = threading.Lock()
VOICE_CACHE_DURATION = 5  # seconds

# FAQ JSON files directory
FAQ_JSON_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
ALL_FAQ_JSON = os.path.join(FAQ_JSON_DIR, "all_faq_questions.json")


def save_faq_to_json(qa_pairs, filename=None):
    """Save FAQ data to JSON file for faster loading"""
    try:
        # Remove duplicate questions before saving to keep JSON compact and consistent
        try:
            deduped = dedupe_faqs(qa_pairs, keep="latest")
            if len(deduped) != len(qa_pairs):
                logger.info(
                    f"🧹 Removed {len(qa_pairs) - len(deduped)} duplicate FAQ entries before saving"
                )
            qa_pairs = deduped
        except Exception:
            # If dedupe fails for any reason, continue with original list
            logger.debug("⚠️ dedupe_faqs failed, proceeding without dedupe")
        if filename is None:
            filename = ALL_FAQ_JSON

        faq_data = {
            "timestamp": time.time(),
            "created_at": datetime.now().isoformat(),
            "total_questions": len(qa_pairs),
            "questions": qa_pairs,
        }

        os.makedirs(os.path.dirname(filename), exist_ok=True)

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(faq_data, f, indent=2, ensure_ascii=False)

        logger.info(f"💾 Saved {len(qa_pairs)} FAQ questions to: {filename}")
        return True

    except Exception as e:
        logger.error(f"❌ Error saving FAQ to JSON: {e}")
        return False


def save_user_faq_to_json(user_id, qa_pairs):
    """Save user-specific FAQ data to JSON file for faster loading"""
    try:
        user_faq_file = os.path.join(FAQ_JSON_DIR, f"user_faq_{user_id}.json")

        faq_data = {
            "timestamp": time.time(),
            "created_at": datetime.now().isoformat(),
            "user_id": user_id,
            "total_questions": len(qa_pairs),
            "questions": qa_pairs,
        }

        os.makedirs(os.path.dirname(user_faq_file), exist_ok=True)

        with open(user_faq_file, "w", encoding="utf-8") as f:
            json.dump(faq_data, f, indent=2, ensure_ascii=False)

        logger.info(
            f"💾 Saved {len(qa_pairs)} user FAQ questions for {user_id} to: user_faq_{user_id}.json"
        )
        return True

    except Exception as e:
        logger.error(f"❌ Error saving user FAQ to JSON: {e}")
        return False


def load_faq_from_json(filename=None, max_age_seconds=600):
    """Load FAQ data from JSON file if it exists and is recent"""
    try:
        if filename is None:
            filename = ALL_FAQ_JSON

        if not os.path.exists(filename):
            logger.debug(f"FAQ JSON file not found: {filename}")
            return None

        # Check file age - 600 seconds (10 minutes) for fast response times
        # New Q&A submissions will trigger immediate cache clear anyway
        file_age = time.time() - os.path.getmtime(filename)
        if file_age > max_age_seconds:
            logger.debug(f"FAQ JSON file expired (age: {file_age:.1f}s): {filename}")
            return None

        with open(filename, "r", encoding="utf-8") as f:
            faq_data = json.load(f)

        questions = faq_data.get("questions", [])
        logger.info(
            f"📋 Loaded {len(questions)} FAQ questions from JSON (age: {file_age:.1f}s)"
        )
        return questions

    except Exception as e:
        logger.error(f"❌ Error loading FAQ from JSON: {e}")
        return None


def dedupe_faqs(qa_pairs, keep="latest", similarity_threshold=0.75):
    """Remove duplicate FAQ entries using fuzzy string matching.

    Two questions are considered duplicates if they are >= 75% similar.
    Uses difflib.SequenceMatcher for fuzzy matching.

    Parameters:
    - qa_pairs: list of dicts containing at least a 'question' and 'timestamp' key
    - keep: 'latest' to keep the most recent entry per question, 'first' to keep the first seen
    - similarity_threshold: similarity score (0-1) to consider as duplicates (default 0.75)

    Returns: list of deduplicated entries
    """
    if not qa_pairs or not isinstance(qa_pairs, list):
        return []

    from difflib import SequenceMatcher

    def normalize(text):
        if not text:
            return ""
        s = str(text).strip().lower()
        s = re.sub(r"\s+", " ", s)  # Normalize whitespace
        s = re.sub(r"[^\w\s]", "", s)  # Remove punctuation
        return s

    def similarity(a, b):
        """Calculate similarity between two strings (0-1)"""
        return SequenceMatcher(None, a, b).ratio()

    def ts_key(entry):
        ts = entry.get("timestamp")
        try:
            return float(ts)
        except Exception:
            return str(ts or "")

    # Sort items chronologically
    try:
        items = sorted(qa_pairs, key=lambda x: ts_key(x))
    except Exception:
        items = list(qa_pairs)

    # Track which entries to keep
    kept_indices = []
    seen_normalized = {}
    removed_count = 0

    for i, entry in enumerate(items):
        entry_id = entry.get("id", "")
        question_text = normalize(entry.get("question", ""))

        # Skip if we already have this exact ID
        if entry_id in [items[j].get("id", "") for j in kept_indices]:
            removed_count += 1
            continue

        # Check if this is similar to any already kept entry
        is_duplicate = False
        for kept_idx in kept_indices:
            kept_text = normalize(items[kept_idx].get("question", ""))
            sim_score = similarity(question_text, kept_text)

            if sim_score >= similarity_threshold:
                logger.debug(
                    f"Found duplicate: '{question_text[:40]}...' ({sim_score:.1%} similar to existing)"
                )
                is_duplicate = True

                # If keep='latest', replace older entry with newer one
                if keep == "latest" and ts_key(entry) > ts_key(items[kept_idx]):
                    kept_indices.remove(kept_idx)
                    kept_indices.append(i)
                    removed_count += 1
                    is_duplicate = False  # We're keeping this one
                else:
                    removed_count += 1

                break

        if not is_duplicate:
            kept_indices.append(i)

    # Build result maintaining order
    deduped = [items[i] for i in kept_indices]
    logger.info(
        f"🧹 Fuzzy Dedupe (>{similarity_threshold*100:.0f}% match): {len(qa_pairs)} → {len(deduped)} ({removed_count} removed)"
    )
    return deduped


def save_user_faq_to_json(user_id, qa_pairs):
    """Save user-specific FAQ data to JSON file"""
    user_faq_file = os.path.join(FAQ_JSON_DIR, f"user_faq_{user_id}.json")
    return save_faq_to_json(qa_pairs, user_faq_file)


def load_user_faq_from_json(user_id, max_age_seconds=300):
    """Load user-specific FAQ data from JSON file"""
    user_faq_file = os.path.join(FAQ_JSON_DIR, f"user_faq_{user_id}.json")
    return load_faq_from_json(user_faq_file, max_age_seconds)


# Voice conversation configuration
FIREWORKS_API_KEY = os.getenv("FIREWORKS_API_KEY")
FIREWORKS_KEY = os.getenv(
    "FIREWORKS_KEY"
)  # Alternative key name used in voice training
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
CLOUDFRONT_DOMAIN = os.getenv("CLOUDFRONT_DOMAIN")
AUDIO_MODEL = os.getenv("AUDIO_MODEL", "whisper-v3-turbo")

# Check for Fireworks API key
if not (FIREWORKS_API_KEY or FIREWORKS_KEY):
    logger.warning(
        "⚠️ FIREWORKS_API_KEY or FIREWORKS_KEY not set - Whisper transcription will not work"
    )

# Voice conversation S3 configuration
VOICE_CONFIG = {
    "bucket_name": S3_BUCKET,
    "audio_path": "voice-conversations/",
    "recordings_path": "voice-recordings/",
}

# Fireworks AI configuration
FIREWORKS_CONFIG = {
    "url": "https://api.fireworks.ai/inference/v1/chat/completions",
    "model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
    "temperature": 0.6,
    "top_p": 1,
    "top_k": 40,
}

# Tour page order configuration
TOUR_PAGE_ORDER = [
    "terms_and_conditions",
    "onboarding_1",
    "home",
    "train_dev",
    "unified_mailbox",
    "my_notes",
    "tickets",
    "contacts",
    "ai_reporting",
    "bytoid_playbook",
    "agents_hub",
    "bytoid_agent",
]


# Initialize AWS services using boto3.Session pattern
def get_polly_instance():
    try:
        # Check for required environment variables
        if missing_vars:
            logger.warning(
                f"❌ Cannot initialize AWS services. Missing: {missing_vars}"
            )
            polly_client = None
        else:
            aws_session = boto3.Session(
                aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
                region_name=os.getenv("AWS_REGION", "us-east-1"),
            )

            polly_client = aws_session.client("polly")
            logger.info("✅ AWS Polly initialized")
            return polly_client

    except Exception as e:
        logger.warning(f"❌ AWS services not initialized: {e}")
        return None


# Tour scripts for multiple pages with complete tour experience
TOUR_SCRIPTS = {
    "terms_and_conditions": {
        "components": [
            {
                "id": "eva_introduction",
                "name": "Eva Introduction",
                "order": 1,
                "script": """Hi! I'm Eva, your AI assistant. I'm here to guide you through Bytoid and help you discover everything the platform offers.

Before we proceed, please review our Terms and Conditions - they cover data usage, features, and how we work together. Once you understand them, accept to continue.

Any questions about the terms? Just let me know!""",
                "target_element": "terms-container",
                "position": "center",
            },
            {
                "id": "terms_acceptance",
                "name": "Accept Terms",
                "order": 2,
                "script": """Click 'Accept & Continue' once you've reviewed the terms. You're ready to set up your company profile and unlock Bytoid's AI features!

Do you have any questions before proceeding?""",
                "target_element": "accept-button",
                "position": "bottom",
            },
        ],
        "total_components": 2,
        "estimated_duration": 20,
        "next_page": "onboarding_1",
        "navigation_instruction": "Great! Now let's set up your company details!",
    },
    "onboarding_1": {
        "components": [
            {
                "id": "company_setup_complete",
                "name": "Company Setup",
                "order": 1,
                "script": """Welcome to Company Setup! Fill in your Full Name, Company Name, Line of Business, Years in Business, and Registration Status.

**Quick Tips:**
- Right-click for 'Ask Bytoid' (tour help) or 'Ask Eva' (automation help)
- Quick actions: Search Email, Build Playbook, Add Contact, etc.
- These details personalize your Bytoid experience

Do you have any questions about the company setup fields?""",
                "target_element": "company-setup-container",
                "position": "center",
            },
        ],
        "total_components": 1,
        "estimated_duration": 25,
        "next_page": "home",
        "navigation_instruction": "Perfect! Your company is set. Now let's explore your AI Hub!",
    },
    "home": {
        "components": [
            {
                "id": "hub-intro",
                "name": "Welcome to Your Hub",
                "order": 1,
                "script": """Welcome to Bytoid! I'm Eva, your AI assistant. This is your operations hub - your command center for running your business.

You're looking at the Standard View, which gives you a real-time snapshot of everything happening right now. All your key metrics, customer communications, and business activities in one place.

Let's explore what makes this hub so powerful!

Any questions before we dive in?""",
                "target_element": "body",
                "position": "center",
            },
            {
                "id": "metrics-cards",
                "name": "Real-Time Business Metrics",
                "order": 2,
                "script": """Here's your pulse at a glance - four essential metrics that show you exactly what needs attention right now:

**Tickets Unassigned** - Customer issues waiting for attention
**Playbooks Defined** - Automations you've created
**Web Chats** - Active customer conversations
**Response Time** - How fast you're responding to customers

Each metric updates in real-time. One glance tells you if everything is running smoothly or if action is needed. This is your business dashboard at its simplest!

Do you have questions about these metrics?""",
                "target_element": "[data-tour-id='metrics-cards']",
                "position": "top",
            },
            {
                "id": "dashboard-analytics",
                "name": "Ticket Trends & Analytics",
                "order": 3,
                "script": """Below your metrics, see your Ticket Trends - a 6-month view of your customer support workload.

This bar chart shows:
- **Ticket volume over time** - How many tickets you're handling each month
- **Status breakdown** - Open (red), Pending (yellow), Solved (green)
- **Trends and patterns** - Notice if volume is increasing or decreasing

This helps you spot seasonal patterns and plan your team capacity. Data-driven decisions start here!

Do you have questions about ticket trends?""",
                "target_element": "[data-tour-id='dashboard-analytics']",
                "position": "top",
            },
            {
                "id": "quick-notes",
                "name": "Quick Notes Hub",
                "order": 4,
                "script": """Your Quick Notes section keeps important reminders and team messages front and center.

Here you can:
- See recent notes and reminders
- View important updates from your team
- Click "View All" to access your complete notes library
- Stay connected to what matters most

Quick notes keep everyone aligned and nothing falls through the cracks!

Do you have questions about Quick Notes?""",
                "target_element": "[data-tour-id='quick-notes']",
                "position": "top",
            },
            {
                "id": "recent-connects-panel",
                "name": "Right Sidebar: Quick Setup & Guides",
                "order": 5,
                "script": """On the right sidebar, you'll find your Setup Guide and quick action buttons:

**Setup Guide** shows your onboarding progress:
- Dashboard setup
- Communication configuration  
- Training & AI setup

**Quick Actions** let you instantly:
- **Connect Your Website** - Integrate your business data
- **Update Knowledge Base** - Feed your AI current information
- **Define Workflows** - Create automations
- **Generate AI Report** - Get insights on demand

These tools help you unlock Bytoid's full power quickly!

Do you have questions about the sidebar?""",
                "target_element": "[data-tour-id='recent-connects-panel']",
                "position": "left",
            },
            {
                "id": "my-apps-section",
                "name": "My Apps & Integrations",
                "order": 6,
                "script": """At the bottom, you have My Apps - your integration marketplace:

Available integrations include:
- **App Store** - Browse 100+ business apps
- **Bytoid Live** - Live chat and customer engagement
- **Facebook Inbox** - Connect customer messages
- **Instagram Inbox** - Social media conversations

Connect the tools your business uses every day. Bytoid becomes your central hub, pulling in data from everywhere!

Do you have questions about integrations?""",
                "target_element": "[data-tour-id='my-apps-section']",
                "position": "top",
            },
            {
                "id": "reporting-tab-button",
                "name": "Switch to Reporting View",
                "order": 7,
                "script": """Now here's the power move - see that "Reporting" button at the top next to "Standard"?

**Reporting View** is your analytics powerhouse where you can:
- View AI-generated reports with automatic insights
- Build custom dashboards with drag-and-drop widgets
- Create visualizations and charts for data analysis
- Access 20+ pre-built reports (Business Analytics, Tickets, Customers, Revenue, Communication, etc.)
- Your configurations auto-save automatically

The Reporting View transforms your data into actionable intelligence. Same data, completely different perspective!

Ready to explore the Reporting dashboard?

Do you have any questions about switching to Reporting?""",
                "target_element": "[data-tour-id='reporting-tab-button']",
                "position": "top",
            },
        ],
        "total_components": 7,
        "estimated_duration": 120,
        "next_page": "train_dev",
        "navigation_instruction": "Excellent! Now let's create your AI assistant!",
    },
    "train_dev": {
        "components": [
            {
                "id": "assistant-profile-card",
                "name": "Meet Your AI Assistant",
                "order": 1,
                "script": """Welcome to Rachel's training center! You're about to create your own intelligent business assistant.

First, let's give her personality. This is where you'll:
- Choose a name that reflects your brand
- Select personality traits - is she professional? Friendly? Creative?
- Pick her voice - male or female

Think of this as bringing your AI assistant to life. She's getting her identity, her voice, her unique character.

Once she has her personality, she'll be ready to learn about your world!

Do you have questions about personality setup?""",
                "target_element": "[data-tour-id='assistant-profile-card']",
                "position": "bottom",
                "tab": "profile",
            },
            {
                "id": "web-training-card",
                "name": "Teaching Her About You",
                "order": 2,
                "script": """Now that Rachel has her personality, it's time to teach her about your business!

**Web Scraping** - This is where the learning begins:
- Paste your website URL or YouTube video link
- Click "Analyze" button and watch the magic happen
- Rachel automatically extracts and processes all your content
- She learns from public websites and captioned videos

In seconds, Rachel becomes an expert on your online presence, your products, your services, and how you communicate with customers.

But websites only show your public face. Your real operations, policies, and procedures live in your internal documents. Ready to share those secrets with Rachel?

Do you have questions about web scraping?""",
                "target_element": "[data-tour-id='web-training-card']",
                "position": "bottom",
                "tab": "web",
            },
            {
                "id": "voice-training-card",
                "name": "Hearing Your Voice",
                "order": 3,
                "script": """Perfect! Now comes something special - teaching Rachel to sound like YOU.

**Voice Training** - Click "Record" to capture your essence:
- Speak naturally about your business and values
- Share your communication style and how you respond to customers
- Record multiple examples so Rachel learns your unique voice
- The AI absorbs your tone, personality, and communication patterns

This is powerful! Rachel now has a voice trained on YOUR voice. She'll communicate with customers not just with your knowledge, but with YOUR personality. She's becoming YOU in digital form!

But knowledge and voice aren't enough. Your business also has rules, standards, and procedures that make it unique. Time to feed those to Rachel!

Do you have questions about voice training?""",
                "target_element": "[data-tour-id='voice-training-card']",
                "position": "bottom",
                "tab": "voice",
            },
            {
                "id": "documents-card",
                "name": "Deep Dive into Your Playbook",
                "order": 4,
                "script": """Great! Now let's give Rachel access to your complete business playbook.

**Document Upload** - Your business in black and white:
- Upload PDFs, Word documents, Excel files
- Add company policies, procedures, and guidelines
- Share product catalogs, pricing guides, FAQs
- Include any industry knowledge or best practices

Rachel reads every document, connects the dots between information, and builds a comprehensive map of how your business actually operates day-to-day.

She now knows your website, has learned your voice, and understands your documented procedures. But business is complex - there are always edge cases and special scenarios. That's where the final piece comes in!

Do you have questions about document upload?""",
                "target_element": "[data-tour-id='documents-card']",
                "position": "bottom",
                "tab": "docs",
            },
            {
                "id": "clarifications-content",
                "name": "Learning Your Unique Rules",
                "order": 5,
                "script": """Here's the final, most important piece - teaching Rachel YOUR specific rules and judgment calls.

**Clarifications & Fine-Tuning** - Rachel gets smart:
- She analyzes everything you've trained her on
- She asks intelligent questions about edge cases and policies
- "How do you handle customer refunds?"
- "What's your policy on rush orders?"
- "How do you handle difficult customers?"

Every answer you give becomes a business rule that guides Rachel's decisions. These aren't just policies - they're YOUR values, YOUR judgment, YOUR way of doing business.

Rachel now understands your website, sounds like you, knows your procedures, AND follows your unique rules. She's fully trained and ready to work! In the next step, watch her handle real customer emails in the Unified Mailbox. She'll apply everything she's learned, thinking like your best team member!

Let's see Rachel in action!

Do you have any final questions?""",
                "target_element": "[data-tour-id='clarifications-content']",
                "position": "bottom",
                "tab": "faq",
            },
        ],
        "total_components": 5,
        "estimated_duration": 85,
        "next_page": "unified_mailbox",
        "navigation_instruction": "Perfect! Rachel is fully trained. Now let's watch her handle real customer emails!",
    },
    "unified_mailbox": {
        "components": [
            {
                "id": "unified_mailbox_introduction",
                "name": "Unified Mailbox Hub",
                "order": 1,
                "script": """Welcome to your AI-powered Unified Mailbox! All customer communications in one intelligent workspace.

Your AI automatically:
- Organizes conversations
- Analyzes customer sentiment
- Categorizes by priority
- Suggests perfect responses

Your email command center is ready!

Questions about Unified Mailbox?""",
                "target_element": "unified",
                "position": "center",
            },
            {
                "id": "conversation_list",
                "name": "Layout & Organization",
                "order": 2,
                "script": """Three-part layout:
1. **Left Panel** - Smart filters sorting by priority and type
2. **Center** - Conversations with AI-generated summaries and insights
3. **Right Panel** - Powerful reply options with full AI automation

Every element designed for effortless, intelligent email management.

Questions about the layout?""",
                "target_element": "inbox-sidebar",
                "position": "center",
            },
            {
                "id": "conversation_list_1",
                "name": "Smart Conversation Management",
                "order": 3,
                "script": """Each conversation shows:
- Customer priority level
- Sentiment indicators
- AI-generated summaries
- Complete customer journey
- Past interactions and preferences

AI remembers everything - like a personal assistant who never forgets!

Questions about conversation tracking?""",
                "target_element": "conversation-view",
                "position": "right",
            },
            {
                "id": "reply_options_1",
                "name": "AI-Powered Responses",
                "order": 4,
                "script": """Three powerful response modes:
1. **Manual** - Full control for personal touches
2. **AI Suggestions** - Smart assistance, you review before sending
3. **Full Autopilot** - AI handles routine emails automatically

Your AI crafts responses exactly like you, using your knowledge and style. It's cloning your best customer service!

Questions about responses?""",
                "target_element": "reply-options",
                "position": "top",
            },
        ],
        "total_components": 4,
        "estimated_duration": 45,
        "next_page": "my_notes",
        "navigation_instruction": "Great! Now let's explore My Notes!",
    },
    "my_notes": {
        "components": [
            {
                "id": "notes_introduction",
                "name": "My Notes Hub",
                "order": 1,
                "script": """Your personal knowledge hub! Manage personal and shared notes for quick reference and team collaboration.

**Features:**
- Organize by categories
- Mark as shared or private
- Search instantly
- View creation dates and sharing status

Use for: Customer details, process docs, team reminders, important info.

Questions about My Notes?""",
                "target_element": "notes-container",
                "position": "center",
            },
            {
                "id": "notes_features",
                "name": "Notes Management",
                "order": 2,
                "script": """**Create & Organize:**
- Click button to create new notes
- Filter: "All Notes", "Shared", "Private"
- Search bar for instant access

**Sharing:**
- Lock icon = Private (only you)
- People icon = Shared (with team)

Each note shows title, date, and status. Click to view/edit.

Questions about note management?""",
                "target_element": "notes-actions",
                "position": "bottom",
            },
        ],
        "total_components": 2,
        "estimated_duration": 15,
        "next_page": "tickets",
        "navigation_instruction": "Perfect! Now let's manage customer support tickets!",
    },
    "tickets": {
        "components": [
            {
                "id": "ticket_search_1",
                "name": "Intelligent Ticket Search",
                "order": 1,
                "script": """Your AI-powered Tickets command center for customer support!

**Search & Filter:**
- Natural language queries: "urgent tickets from last week"
- Filters: Status, Priority, Channel, SLA Status, Date ranges
- AI suggests optimal filter combinations

Find exactly what you need instantly!

Do you have any questions about ticket search?""",
                "target_element": "ticket-search",
                "position": "bottom",
            },
            {
                "id": "tickets_table_1",
                "name": "Ticket Dashboard",
                "order": 2,
                "script": """AI-enhanced ticket overview showing:
- **Contact Details** - Customer history
- **Ticket Numbers** - Auto-categorized
- **Assignment** - Who's handling it
- **Priority** - AI-suggested urgency
- **SLA Status** - Compliance monitoring
- **Subjects** - AI summaries

**AI Analysis:**
- Predicts resolution times
- Suggests optimal team assignments
- Identifies trends and patterns

Do you have questions about the ticket dashboard?""",
                "target_element": "tickets-table",
                "position": "top",
            },
        ],
        "total_components": 2,
        "estimated_duration": 30,
        "next_page": "contacts",
        "navigation_instruction": "Excellent! Now let's explore Contacts for intelligent customer relationship management!",
    },
    "contacts": {
        "components": [
            {
                "id": "contact_tabs_1",
                "name": "Contacts & Add Contact",
                "order": 1,
                "script": """Welcome to your intelligent Contacts hub - where you manage your business network with AI-powered insights!

**Tabs:**
- **Contacts View** - Shows your network with AI-enhanced profiles and engagement history
- **Add Contact** - Creates new contacts with automatic data enrichment from LinkedIn and company databases

The AI automatically imports contacts from Gmail and Outlook, detects duplicates, and keeps everything synchronized. It tracks interaction history and suggests optimal contact times!

Do you have questions about the Contacts section?""",
                "target_element": "contact-tabs",
                "position": "top",
            },
            {
                "id": "contact_search_1",
                "name": "Search & Filter",
                "order": 2,
                "script": """Powerful AI-enhanced search and filtering! Use natural language queries like:
- "Find VIP customers from last month"
- "Show prospects in tech industry"
- "Companies with 50+ employees"

The AI searches names, companies, titles, locations, interaction history, and conversation content. It learns your patterns and proactively suggests relevant contacts!

**Filter by Contact Type:**
- Customers - Existing clients and partners
- Prospects - Potential business opportunities

Do you have questions about contact search?""",
                "target_element": "contact-search",
                "position": "bottom",
            },
            {
                "id": "contacts_table_1",
                "name": "Contacts Table",
                "order": 3,
                "script": """Your relationship intelligence dashboard! The table shows:
- **Contact Type** - Customer, Prospect, or Partner
- **Email/Status** - Contact info and engagement level
- **Last Interaction** - When you last communicated
- **Workflow Tags** - AI auto-categorizes based on behavior

AI-powered insights: predicts customer lifetime value, identifies churn risks, recommends communication strategies, and suggests next best actions!

Do you have questions about contact management?""",
                "target_element": "contacts-table",
                "position": "top",
            },
        ],
        "total_components": 3,
        "estimated_duration": 35,
        "next_page": "ai_reporting",
        "navigation_instruction": "Perfect! Now let's explore AI Reporting for powerful business analytics!",
    },
    "ai_reporting": {
        "components": [
            {
                "id": "report_assistant_1",
                "name": "AI Report Assistant",
                "order": 1,
                "script": """Welcome to AI Reporting - your business intelligence command center!

The **AI Report Assistant** on the right is like having a data analyst working 24/7! Ask natural language questions like:
- "Analyze my top campaigns last quarter"
- "Show customer churn trends"
- "What's our sales forecast for next month?"

The AI performs predictive analytics, identifies patterns, detects anomalies, provides recommendations, and creates executive summaries automatically!

Do you have questions about the AI Report Assistant?""",
                "target_element": "report-assistant",
                "position": "right",
            },
            {
                "id": "report_sidebar_",
                "name": "Report Tools",
                "order": 2,
                "script": """Powerful professional-grade tools on the left:

**Generate Reports** - Machine learning automatically selects relevant data, applies statistical analysis, and creates publication-ready reports with professional formatting.

**Visualization** - Advanced engine recommends optimal chart types, color schemes, and interactive elements based on your data.

**Report Library** - Your organizational knowledge base storing historical analyses, enabling versioning, team collaboration, and audit trails for compliance.

These transform complex data analysis into intuitive, minutes-long processes!

Do you have questions about report tools?""",
                "target_element": "report-sidebar",
                "position": "right",
            },
            {
                "id": "popular_reports_",
                "name": "Popular Report Types",
                "order": 3,
                "script": """Essential business intelligence reports:

**Sales Analytics** - Revenue analysis, conversion funnel optimization, customer acquisition costs, sales forecasting.

**Lead Analysis** - Lead scoring, source attribution, conversion probability.

**Performance Reports** - Operational efficiency, team productivity, resource utilization.

**Customer Insights** - Behavioral segmentation, lifetime value predictions, churn risk, personalization.

**Integration Status** - Data pipeline health, API performance, system reliability.

Each includes industry benchmarking, AI-generated insights, and action items ranked by impact!

Do you have questions about report types?""",
                "target_element": "popular-reports",
                "position": "top",
            },
        ],
        "total_components": 3,
        "estimated_duration": 40,
        "next_page": "bytoid_playbook",
        "navigation_instruction": "Excellent! Now let's explore Bytoid Playbook for business automation!",
    },
    "bytoid_playbook": {
        "components": [
            {
                "id": "playbook_overview",
                "name": "Playbook Introduction",
                "order": 1,
                "script": """Welcome to Bytoid Playbook - your automation powerhouse! This is where you create digital workflows that run your business 24/7.

Think of Playbooks as your business instruction manual. You describe what you want in plain English, and the AI transforms it into powerful automation that handles:
- Email responses and routing
- Customer journey workflows
- Multi-step approval processes
- Lead qualification
- And much more!

Let's explore how easy it is to create intelligent workflows!""",
                "target_element": "playbook-container",
                "position": "center",
            },
            {
                "id": "create_button_2",
                "name": "Your Automation Library",
                "order": 2,
                "script": """This is where all your playbooks are saved and organized! Each automation you create appears here for easy access, modification, and management.

You can:
- Duplicate existing playbooks
- Edit and refine workflows
- View performance metrics
- Turn on/off automations
- Monitor execution status

Ready to create your first automation? Click 'Create New Instructions' to build intelligent workflows!

Do you have questions about Playbook features?""",
                "target_element": "first-instructions",
                "position": "top",
            },
        ],
        "total_components": 2,
        "estimated_duration": 30,
        "next_page": "agents_hub",
        "navigation_instruction": "Great! Now let's explore the Agents Hub for managing multiple AI assistants!",
    },
    "agents_hub": {
        "components": [
            {
                "id": "agents_overview",
                "name": "Agents Hub Overview",
                "order": 1,
                "script": """Welcome to the Agents Hub - your command center for managing multiple AI agents!

Think of this as your AI workforce management system. You can create specialized agents for different business functions:
- Customer service agent
- Sales inquiry handler
- Product support specialist
- And more!

**Dashboard shows:**
- Total Agents - Your entire AI team
- Active Agents - Currently running
- Agents With Concerns - Needing attention

Search bar and filters help you quickly find specific agents. Ready to add your first agent?""",
                "target_element": "agents-hub-container",
                "position": "center",
            },
            {
                "id": "add_agent_navigation",
                "name": "Add Agent Navigation",
                "order": 2,
                "script": """Perfect! Click any 'Add Agent' button to start creating your specialized AI assistants.

Notice how the interface shows slots for multiple agents? You can build an entire team:
- Each agent has unique personality and knowledge
- All agents work simultaneously
- Each can handle specific business functions
- Share knowledge base or have specialized training

Let's navigate to agent creation!

Do you have questions about agent creation?""",
                "target_element": "add-agent-buttons",
                "position": "center",
            },
            {
                "id": "agent_creation_process",
                "name": "Creating Your Agent",
                "order": 3,
                "script": """Building your specialized AI assistant is easy!

**Configuration Steps:**
1. **General Settings** - Agent name, personality, role
2. **Onboarding** - How your agent learns about your business
3. **Assistant Tab** - Knowledge base and communication style
4. **Account Section** - Permissions and access levels

Similar to training your main assistant, but now you create multiple specialized agents for different tasks!

You've mastered building your AI workforce!

Do you have questions about agent creation?""",
                "target_element": "agent-creation-process",
                "position": "center",
            },
        ],
        "total_components": 3,
        "estimated_duration": 40,
        "next_page": "bytoid_agent",
        "navigation_instruction": "Excellent! You've mastered agent management. Now let's visit Support where Eva can help!",
    },
    "bytoid_agent": {
        "components": [
            {
                "id": "support_chat",
                "name": "Support Chat with Eva",
                "order": 1,
                "script": """🎉 Congratulations! You've completed the comprehensive Bytoid tour!

Welcome to Support - Eva, your AI assistant, is here 24/7 to help with:
- Questions about any feature
- Technical issues or troubleshooting
- Account and setup help
- Best practice guidance
- Anything else you need!

**Just type your question below** and Eva will provide instant, personalized assistance. She has access to all documentation and can escalate complex issues to specialists.

**You're now ready to transform your business with AI-powered automation!**

Do you have any final questions before we finish?""",
                "target_element": "support-chat-container",
                "position": "center",
            },
        ],
        "total_components": 1,
        "estimated_duration": 15,
        "next_page": None,
        "navigation_instruction": "🎉 Incredible! You've mastered the complete Bytoid platform. You're ready to revolutionize your business!",
    },
}
# Map tour page names to actual frontend routes
TOUR_PAGE_ROUTES = {
    "terms_and_conditions": "/beta-agreement",
    "onboarding_1": "/onboarding",
    "home": "/dashboard",
    "train_dev": "/train-rachel",
    "unified_mailbox": "/umail",
    "my_notes": "/notes",
    "tickets": "/tickets",
    "contacts": "/contacts",
    "ai_reporting": "/ai-reporting",
    "bytoid_playbook": "/self-service",
    "agents_hub": "/agents-hub",
    "bytoid_agent": "/bytoid-agent",
}

# Map frontend route names to internal page names for alias support
TOUR_PAGE_NAME_ALIASES = {
    "notes": "my_notes",  # Frontend calls "notes", internally it's "my_notes"
}

# Audio configuration
AUDIO_CONFIG = {
    "bucket_name": S3_BUCKET,
    "base_path": "tour-audio/",
    "voice_id": "Danielle",
    "engine": "neural",
}


def generate_audio_filename(page_name, component_id):
    """Generate audio filename for component"""
    return f"{page_name}_{component_id}.mp3"


def get_audio_path(page_name, component_id):
    """Get S3 path for audio file"""
    filename = generate_audio_filename(page_name, component_id)
    return f"{AUDIO_CONFIG['base_path']}{filename}"


def text_to_speech_polly(text, filename):
    """Convert text to speech using AWS Polly - only if audio doesn't exist"""
    try:
        logger.info(f"🎵 Checking audio for: {filename}")
        polly_client = get_polly_instance()

        if not polly_client:
            logger.error("❌ Polly client not available")
            return None

        # Extract page and component from filename properly
        parts = filename.replace(".mp3", "").split(
            "_", 1
        )  # Split only on first underscore
        if len(parts) >= 2:
            page_name = parts[0]
            component_id = parts[1]  # Keep the rest as component_id
            audio_key = get_audio_path(page_name, component_id)
        else:
            logger.error(f"❌ Invalid filename format: {filename}")
            return None

        # Check if audio already exists in S3
        try:
            s3_client = s3bucket()
            s3_client.head_object(Bucket=AUDIO_CONFIG["bucket_name"], Key=audio_key)
            # If no exception, file exists - return existing URL
            logger.info(f"✅ Using existing audio: {audio_key}")
            return attach_CLDFRNT_url(audio_key)
        except ClientError as e:
            if e.response["Error"]["Code"] != "404":
                # Some other error occurred
                logger.error(f"❌ Error checking S3: {e}")
                return None
            # File doesn't exist, continue to generate
            logger.info(f"🎵 Audio not found, generating: {audio_key}")

        # Clean text
        text_to_speak = text.strip()
        if len(text_to_speak) > 3000:
            text_to_speak = text_to_speak[:2900] + "..."

        # Generate speech
        response = polly_client.synthesize_speech(
            Text=text_to_speak, OutputFormat="mp3", VoiceId="Danielle", Engine="neural"
        )

        # Upload to S3
        audio_stream = response["AudioStream"]

        s3_client.upload_fileobj(
            BytesIO(audio_stream.read()),
            AUDIO_CONFIG["bucket_name"],
            audio_key,
            ExtraArgs={"ContentType": "audio/mpeg"},
        )

        logger.info(f"✅ Audio uploaded: {audio_key}")

        # Return presigned URL
        return attach_CLDFRNT_url(audio_key)

    except Exception as e:
        logger.error(f"❌ Error generating audio: {e}")
        logger.error(f"❌ Failed filename: {filename}")
        import traceback

        logger.error(f"❌ Traceback: {traceback.format_exc()}")
        return None


def get_full_conversation_context(messages):
    """Return all conversation messages without any token limits or summarization"""
    try:
        logger.info(f"� Using full conversation context: {len(messages)} messages")
        return messages  # Return all messages without limits

    except Exception as e:
        logger.error(f"❌ Error getting conversation context: {e}")
        return messages


def get_llama_response(messages, user_context=""):
    """Get response from Fireworks AI Llama 3.3 70B model"""
    try:
        if not FIREWORKS_API_KEY:
            logger.error("❌ Fireworks API key not available")
            return None

        # Prepare system context about the application
        system_context = f"""You are Eva, Bytoid's helpful AI assistant. Give direct, specific answers to user questions about Bytoid.

Answer exactly what users ask about:
- HOW to use features → Give step-by-step instructions
- WHAT features do → Explain clearly and practically  
- WHY use Bytoid → Share specific benefits
- WHICH feature to use → Recommend based on their need

Bytoid's Hubstack Components (when users ask about "Hubstack" or components):
- Agents Hub: AI agent management and multi-agent system
- AI Assistant Chat: Interactive AI conversations
- Unified Mailbox: AI-powered email management and automation
- Playbook: Workflow automation and business process management
- Tickets: Customer support and issue tracking system
- Contacts: CRM and contact management
- AI Reporting: Business analytics and insights
- Search Email: Advanced email search and discovery
- Session Management: User session handling
- Credits: Usage tracking and billing system
- Integrations: Google, Microsoft, Facebook, Zoho connections
- Webhooks: External system integrations

Main capabilities:
- Email automation & AI-powered unified mailbox
- AI assistant training (Train Dev)
- Workflow automation (Playbooks)
- Contact management & CRM
- Customer support tickets
- Business reporting & analytics
- Voice conversations & integrations

If users ask about specific components like "Hubstack", explain the integrated platform components listed above.

Be conversational and natural. Don't just list features - answer their specific question directly. Keep responses under 200 words for voice interactions.

{user_context}"""

        # Prepare messages for the API
        api_messages = [{"role": "system", "content": system_context}]

        for msg in messages:
            if msg["type"] == "user":
                api_messages.append({"role": "user", "content": msg["content"]})
            elif msg["type"] == "assistant":
                api_messages.append({"role": "assistant", "content": msg["content"]})

        payload = {
            "model": FIREWORKS_CONFIG["model"],
            "temperature": FIREWORKS_CONFIG["temperature"],
            "top_p": FIREWORKS_CONFIG["top_p"],
            "top_k": FIREWORKS_CONFIG["top_k"],
            "messages": api_messages,
        }

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {FIREWORKS_API_KEY}",
        }

        logger.info(
            f"🤖 Sending request to Llama 3.3 70B with {len(api_messages)} messages"
        )

        response = requests.post(
            FIREWORKS_CONFIG["url"],
            headers=headers,
            data=json.dumps(payload),
            timeout=30,
        )

        if response.status_code == 200:
            result = response.json()
            assistant_response = (
                result.get("choices", [{}])[0].get("message", {}).get("content", "")
            )

            if assistant_response:
                logger.info(
                    f"✅ Got Llama response: {len(assistant_response)} characters"
                )
                return assistant_response.strip()
            else:
                logger.error("❌ Empty response from Llama model")
                return None
        else:
            logger.error(
                f"❌ Fireworks API error: {response.status_code} - {response.text}"
            )
            return None

    except Exception as e:
        logger.error(f"❌ Error getting Llama response: {e}")
        return None


def convert_response_to_speech(text, user_id):
    """Convert text response to speech using existing Polly integration"""
    try:
        # Generate unique filename for this response
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"voice_response_{user_id}_{timestamp}.mp3"

        # Use existing text_to_speech_polly function
        audio_url = text_to_speech_polly(text, filename)

        if audio_url:
            logger.info(f"🔊 Generated speech response for user {user_id}")
            return audio_url
        else:
            logger.error(f"❌ Failed to generate speech for user {user_id}")
            return None

    except Exception as e:
        logger.error(f"❌ Error converting response to speech: {e}")
        return None


@onboarding_bps.route("/tour/admin/generate-audio", methods=["POST"])
def admin_generate_audio():
    """Generate all tour audio files for all components"""
    try:
        # Check if force regeneration is requested
        data = request.get_json() or {}
        force_regenerate = data.get("force", False)

        logger.info(f"🎵 Starting audio generation... (force={force_regenerate})")
        logger.info(f"📝 TOUR_SCRIPTS keys: {list(TOUR_SCRIPTS.keys())}")

        generated_urls = {}
        skipped_count = 0
        generated_count = 0

        for page_name, tour_data in TOUR_SCRIPTS.items():
            logger.info(f"📄 Processing page: {page_name}")

            generated_urls[page_name] = {}

            if "components" in tour_data:
                logger.info(f"🧩 Found {len(tour_data['components'])} components")

                for component in tour_data["components"]:
                    component_id = component["id"]
                    script_text = component["script"]
                    audio_filename = f"{page_name}_{component_id}.mp3"
                    audio_key = get_audio_path(page_name, component_id)

                    # If not forcing regeneration, check if audio exists
                    if not force_regenerate:
                        try:
                            s3_client = s3bucket()
                            s3_client.head_object(
                                Bucket=AUDIO_CONFIG["bucket_name"], Key=audio_key
                            )
                            # File exists, use existing
                            generated_urls[page_name][component_id] = {
                                "url": attach_CLDFRNT_url(audio_key),
                                "name": component["name"],
                                "status": "existing",
                            }
                            skipped_count += 1
                            logger.info(
                                f"✅ Using existing audio for {page_name}/{component_id}"
                            )
                            continue
                        except ClientError as e:
                            if e.response["Error"]["Code"] != "404":
                                continue
                            # File doesn't exist, proceed with generation

                    logger.info(
                        f"🎯 Generating audio for {component_id} (text length: {len(script_text)})"
                    )

                    audio_url = text_to_speech_polly(script_text, audio_filename)

                    if audio_url:
                        generated_urls[page_name][component_id] = {
                            "url": audio_url,
                            "name": component["name"],
                            "status": "generated",
                        }
                        generated_count += 1
                        logger.info(
                            f"✅ Generated audio for {page_name}/{component_id}"
                        )
                    else:
                        logger.error(
                            f"❌ Failed to generate audio for {page_name}/{component_id}"
                        )
            else:
                logger.warning(f"⚠️ No 'components' key found in {page_name}")

        total_processed = skipped_count + generated_count

        logger.info(
            f"🎉 Total: {total_processed} audio files ({generated_count} new, {skipped_count} existing)"
        )

        return jsonify(
            {
                "status": "success",
                "message": f"Generated {generated_count} audio files successfully",
                "audio_urls": generated_urls,
                "bucket": AUDIO_CONFIG["bucket_name"],
                "voice_id": AUDIO_CONFIG["voice_id"],
                "total_processed": total_processed,
                "new_generated": generated_count,
                "existing_used": skipped_count,
            }
        )

    except Exception as e:
        logger.error(f"❌ Error generating audio: {e}")
        import traceback

        logger.error(f"❌ Traceback: {traceback.format_exc()}")
        return jsonify({"error": "Audio generation failed", "details": str(e)}), 500


def cleanup_old_audio_files(current_audio_keys):
    """Remove old audio files that are no longer needed"""
    try:
        logger.info(f"🧹 Starting cleanup of old audio files...")
        s3_client = s3bucket()

        # List all audio files in S3
        paginator = s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(
            Bucket=AUDIO_CONFIG["bucket_name"], Prefix=AUDIO_CONFIG["base_path"]
        )

        deleted_count = 0
        for page in pages:
            if "Contents" in page:
                for obj in page["Contents"]:
                    s3_key = obj["Key"]

                    # Skip if this is a current audio file
                    if s3_key in current_audio_keys:
                        logger.info(f"✅ Keeping current file: {s3_key}")
                        continue

                    # Delete old file
                    try:
                        s3_client.delete_object(
                            Bucket=AUDIO_CONFIG["bucket_name"], Key=s3_key
                        )
                        deleted_count += 1
                        logger.info(f"🗑️ Deleted old file: {s3_key}")
                    except Exception as e:
                        logger.error(f"❌ Failed to delete {s3_key}: {e}")

        logger.info(f"🎉 Cleanup complete: {deleted_count} old files deleted")
        return deleted_count

    except Exception as e:
        logger.error(f"❌ Error during cleanup: {e}")
        return 0


# Simple status endpoint
@onboarding_bps.route("/tour/status", methods=["GET"])
def get_tour_status():
    """Get tour system status"""
    try:
        polly_client = get_polly_instance()
        # Check Polly
        polly_available = polly_client is not None

        # Check S3
        s3_available = False
        try:
            s3_client = s3bucket()
            s3_client.head_bucket(Bucket=AUDIO_CONFIG["bucket_name"])
            s3_available = True
        except:
            pass

        # Check scripts
        scripts_loaded = len(TOUR_SCRIPTS) > 0

        status = (
            "ready"
            if (polly_available and s3_available and scripts_loaded)
            else "needs_setup"
        )

        return jsonify(
            {
                "status": status,
                "polly_available": polly_available,
                "s3_available": s3_available,
                "scripts_loaded": scripts_loaded,
                "total_scripts": len(TOUR_SCRIPTS),
                "bucket": AUDIO_CONFIG["bucket_name"],
                "available_pages": list(TOUR_SCRIPTS.keys()),
                "tour_order": TOUR_PAGE_ORDER,
            }
        )

    except Exception as e:
        logger.error(f"❌ Error getting status: {e}")
        return jsonify({"error": "Failed to get status"}), 500


@onboarding_bps.route("/tour/<page_name>/components", methods=["GET"])
def get_tour_components(page_name):
    """Get all components for a tour page"""
    try:
        logger.info(f"🔍 Getting tour components for page: {page_name}")  # Debug log

        # Handle page name aliases (e.g., "notes" -> "my_notes")
        actual_page_name = TOUR_PAGE_NAME_ALIASES.get(page_name, page_name)

        if actual_page_name not in TOUR_SCRIPTS:
            logger.warning(
                f"❌ Page not found: {page_name} (resolved to: {actual_page_name})"
            )  # Debug log
            return jsonify({"error": "Page not found"}), 404

        tour_data = TOUR_SCRIPTS[actual_page_name]
        components = tour_data["components"]

        # Sort by order
        components_sorted = sorted(components, key=lambda x: x["order"])

        return jsonify(
            {
                "page_name": page_name,
                "total_components": tour_data["total_components"],
                "estimated_duration": tour_data["estimated_duration"],
                "components": components_sorted,
                "next_page": tour_data.get("next_page"),
                "navigation_instruction": tour_data.get("navigation_instruction"),
            }
        )

    except Exception as e:
        logger.error(f"❌ Error getting tour components: {e}")
        return jsonify({"error": str(e)}), 500


@onboarding_bps.route(
    "/tour/<page_name>/component/<component_id>/audio", methods=["GET"]
)
def get_component_audio(page_name, component_id):
    """Get audio URL for a specific component in any tour page"""
    try:
        logger.info(f"🎵 Getting audio for {page_name}/{component_id}")  # Debug log

        # Handle page name aliases (e.g., "notes" -> "my_notes")
        actual_page_name = TOUR_PAGE_NAME_ALIASES.get(page_name, page_name)

        # Check if page exists
        if actual_page_name not in TOUR_SCRIPTS:
            logger.warning(
                f"❌ Page not found: {page_name} (resolved to: {actual_page_name})"
            )
            return jsonify({"error": f"Page '{page_name}' not found"}), 404

        # Check if component exists
        tour_data = TOUR_SCRIPTS.get(actual_page_name, {})
        components = tour_data.get("components", [])

        component = next(
            (comp for comp in components if comp["id"] == component_id), None
        )
        if not component:
            logger.warning(f"❌ Component not found: {component_id}")  # Debug log
            return (
                jsonify(
                    {
                        "error": f"Component '{component_id}' not found in page '{page_name}'"
                    }
                ),
                404,
            )

        # Check if audio file exists in S3
        # Use actual_page_name since audio files are generated with internal names
        audio_filename = f"{actual_page_name}_{component_id}.mp3"
        audio_key = f"{AUDIO_CONFIG['base_path']}{audio_filename}"

        try:
            s3_client = s3bucket()
            s3_client.head_object(Bucket=AUDIO_CONFIG["bucket_name"], Key=audio_key)
            # If no exception, file exists
            audio_url = attach_CLDFRNT_url(audio_key)
            return jsonify(
                {
                    "page_name": page_name,
                    "component_id": component_id,
                    "component_name": component.get("name", component_id),
                    "audio_url": audio_url,
                    "script": component.get("script", ""),
                    "target_element": component.get("target_element", ""),
                    "position": component.get("position", "bottom"),
                }
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return (
                    jsonify(
                        {
                            "error": "Audio file not found",
                            "page_name": page_name,
                            "component_id": component_id,
                            "expected_file": audio_filename,
                            "message": "Audio may not be generated yet. Try /tour/admin/generate-audio first.",
                        }
                    ),
                    404,
                )
            else:
                return (
                    jsonify(
                        {"error": "An error occurred checking S3", "details": str(e)}
                    ),
                    500,
                )

    except Exception as e:
        logger.error(f"❌ Error getting component audio: {e}")
        return jsonify({"error": "An error occurred", "details": str(e)}), 500


@onboarding_bps.route("/tour/<page_name>/progress", methods=["POST"])
def save_tour_progress(page_name):
    """Save user's tour progress"""
    try:
        data = request.json
        user_id = data.get("user_id")
        completed_components = data.get("completed_components", [])
        current_component = data.get("current_component")
        status = data.get("status", "in_progress")  # in_progress, completed, skipped

        logger.info(f"💾 Saving tour progress for user {user_id} on page {page_name}")

        # TODO: Implement database storage
        # For now, just return success

        return jsonify(
            {
                "status": "success",
                "message": "Tour progress saved",
                "user_id": user_id,
                "page_name": page_name,
                "completed_components": completed_components,
                "current_component": current_component,
            }
        )

    except Exception as e:
        logger.error(f"❌ Error saving tour progress: {e}")
        return jsonify({"error": str(e)}), 500


@onboarding_bps.route("/tour/<page_name>/progress/<user_id>", methods=["GET"])
def get_tour_progress(page_name, user_id):
    """Get user's tour progress"""
    try:
        logger.info(f"📊 Getting tour progress for user {user_id} on page {page_name}")

        # TODO: Fetch from database
        # For now, return empty progress

        return jsonify(
            {
                "user_id": user_id,
                "page_name": page_name,
                "completed_components": [],
                "current_component": None,
                "status": "not_started",
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
        )

    except Exception as e:
        logger.error(f"❌ Error getting tour progress: {e}")
        return jsonify({"error": str(e)}), 500


@onboarding_bps.route("/tour/next-page/<current_page>", methods=["GET"])
def get_next_tour_page(current_page):
    """Get the next page in the tour sequence with navigation route"""
    try:
        logger.info(f"🔍 Getting next page after: {current_page}")

        # Handle page name aliases (e.g., "notes" -> "my_notes")
        actual_current_page = TOUR_PAGE_NAME_ALIASES.get(current_page, current_page)

        if actual_current_page not in TOUR_PAGE_ORDER:
            return jsonify({"error": "Invalid page"}), 404

        current_index = TOUR_PAGE_ORDER.index(actual_current_page)

        # Check if there's a next page
        if current_index < len(TOUR_PAGE_ORDER) - 1:
            next_page = TOUR_PAGE_ORDER[current_index + 1]
            next_page_data = TOUR_SCRIPTS.get(next_page, {})
            next_route = TOUR_PAGE_ROUTES.get(next_page, f"/{next_page}")

            return jsonify(
                {
                    "status": "success",
                    "current_page": actual_current_page,
                    "next_page": next_page,
                    "next_route": next_route,
                    "has_next": True,
                    "navigation_instruction": next_page_data.get(
                        "navigation_instruction", ""
                    ),
                    "next_page_components": len(next_page_data.get("components", [])),
                }
            )
        else:
            return jsonify(
                {
                    "status": "success",
                    "current_page": actual_current_page,
                    "next_page": None,
                    "next_route": None,
                    "has_next": False,
                    "message": "Tour completed! 🎉",
                }
            )

    except Exception as e:
        logger.error(f"❌ Error getting next page: {e}")
        return jsonify({"error": str(e)}), 500


@onboarding_bps.route("/tour/navigation/next/<current_page>", methods=["GET"])
def get_next_tour_page_navigation(current_page):
    """Alternative endpoint for frontend navigation - same functionality"""
    return get_next_tour_page(current_page)


# ==================== SIMPLIFIED S3-ONLY VOICE FUNCTIONS ====================


def transcribe_audio_from_s3(s3_key):
    """Transcribe audio file from S3 using Fireworks Whisper (same as voice training)"""
    try:
        import asyncio
        import tempfile

        # Download audio from S3 to temporary file
        s3_client = s3bucket()
        response = s3_client.get_object(Bucket=S3_BUCKET, Key=s3_key)
        audio_data = response["Body"].read()

        # Create temporary file for audio processing
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
            temp_file.write(audio_data)
            temp_audio_path = temp_file.name

        try:
            # Use the same Speech2TextService as in voice training
            speech_service = Speech2TextService(userid="voice_conversation")

            # Run async transcription
            transcript = asyncio.run(speech_service.transcribe_audio(temp_audio_path))

            if transcript:
                logger.info(
                    f"✅ Fireworks Whisper transcription: {transcript[:100]}..."
                )
                return transcript.strip()
            else:
                logger.error("❌ Fireworks Whisper returned empty transcript")
                return None

        finally:
            # Clean up temporary file
            if os.path.exists(temp_audio_path):
                os.remove(temp_audio_path)

    except Exception as e:
        logger.error(f"❌ Error transcribing audio with Fireworks Whisper: {e}")
        return None


def store_conversation_to_s3(user_id, role, message):
    """Store conversation message directly to S3"""
    try:
        s3_client = s3bucket()
        timestamp = datetime.now(timezone.utc).isoformat()

        # Create conversation entry
        conversation_entry = {
            "timestamp": timestamp,
            "role": role,
            "message": message,
            "user_id": user_id,
        }

        # Store in S3 with timestamp-based key
        s3_key = f"conversations/{user_id}/{timestamp}_{role}.json"

        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=s3_key,
            Body=json.dumps(conversation_entry),
            ContentType="application/json",
        )

        logger.info(f"✅ Stored conversation message to S3: {s3_key}")
        return True

    except Exception as e:
        logger.error(f"❌ Error storing conversation to S3: {e}")
        return False


def get_conversation_from_s3(user_id, limit=10, source_folder=None):
    """Get conversation history from S3 (from conversations/ or qa-conversations/ folder)"""
    try:
        s3_client = s3bucket()

        # Try conversations/ first if no specific source folder specified
        if source_folder is None:
            source_folder = "conversations"

        # List conversation files for user
        prefix = f"{source_folder}/{user_id}/"
        response = s3_client.list_objects_v2(
            Bucket=S3_BUCKET,
            Prefix=prefix,
            MaxKeys=limit * 2,  # Get more to account for user/assistant pairs
        )

        conversations = []
        if "Contents" in response:
            # Sort by timestamp (newest first)
            files = sorted(
                response["Contents"], key=lambda x: x["LastModified"], reverse=True
            )

            for file_obj in files[:limit]:
                # Skip faq_list.json and other non-qa files
                if "faq_list" in file_obj["Key"] or not file_obj["Key"].endswith(
                    ".json"
                ):
                    continue

                try:
                    obj_response = s3_client.get_object(
                        Bucket=S3_BUCKET, Key=file_obj["Key"]
                    )
                    conversation_data = json.loads(
                        obj_response["Body"].read().decode("utf-8")
                    )
                    # Only process items that have 'timestamp' (actual Q&A data, not metadata files)
                    if (
                        isinstance(conversation_data, dict)
                        and "timestamp" in conversation_data
                    ):
                        conversations.append(conversation_data)
                except Exception as e:
                    logger.error(
                        f"Error reading conversation file {file_obj['Key']}: {e}"
                    )
                    continue

        # Sort by timestamp (oldest first for proper conversation flow)
        if conversations:
            conversations.sort(key=lambda x: x.get("timestamp", ""))

        return conversations

    except Exception as e:
        logger.error(f"❌ Error getting conversation from S3: {e}")
        return []


def cleanup_old_conversations(user_id, keep_qa_pairs):
    """
    DISABLED: Delete old conversation files from S3, keeping only the latest 10 Q&A pairs
    This function is no longer used as we keep ALL conversations permanently
    """
    # Function disabled - keeping all conversations in S3
    logger.info(f"📝 Cleanup disabled - keeping all conversations for user {user_id}")
    return 0


def get_fireworks_llama_response(conversation_history, current_message):
    """Get short, crisp response from Fireworks AI Llama 3.3 70B only"""
    try:
        import requests

        # Prepare conversation context with Bytoid-specific response prompt
        messages = [
            {
                "role": "system",
                "content": "You are Eva, Bytoid's helpful AI assistant. Give direct, specific answers to user questions about Bytoid and its components. When users ask about 'Hubstack' or 'Hub components', they're referring to Bytoid's integrated platform components: Agents Hub (AI agent management), AI Assistant Chat, Email management, Playbook workflows, Tickets system, Contacts CRM, AI Reporting, Search Email, Session Management, Credits system, Google/Microsoft/Facebook integrations, Webhooks, and Unified Mailbox. Explain these components clearly when asked. If you're not sure about a specific term, acknowledge that and suggest related Bytoid features. Answer exactly what the user asks: HOW questions get step-by-step instructions, WHAT questions get clear explanations, WHY questions get benefits. Only redirect to general Bytoid features if the question is completely unrelated to business software. Keep answers helpful and conversational.",
            }
        ]

        # Add conversation history (limit to last 5 for context)
        for conv in conversation_history[-5:]:
            messages.append({"role": conv["role"], "content": conv["message"]})

        # Add current message if not already in history
        if (
            not conversation_history
            or conversation_history[-1]["message"] != current_message
        ):
            messages.append({"role": "user", "content": current_message})

        # Use Fireworks AI only - enhanced error handling
        fireworks_api_key = os.getenv("FIREWORKS_KEY")
        if not fireworks_api_key:
            # Note: Update this with a valid API key
            fireworks_api_key = "fw_3IRjUMnSb8IofJ8iD1zA4V4O"
            logger.warning("⚠️ Using fallback Fireworks API key - may be invalid")

        # Try different API endpoint format
        url = "https://api.fireworks.ai/inference/v1/chat/completions"

        # Use original model and add parameters back
        payload = {
            "model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
            "max_tokens": 300,
            "temperature": 0.3,
            "top_p": 0.9,
            "messages": messages,
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {fireworks_api_key}",
        }

        response = requests.post(url, headers=headers, json=payload, timeout=30)

        # Log response details for debugging
        logger.info(f"🔍 Fireworks API Response Status: {response.status_code}")
        if response.status_code != 200:
            logger.error(f"❌ Fireworks API Error Response: {response.text}")
            if response.status_code == 403:
                logger.error(
                    "❌ Fireworks API: Unauthorized - API key may be invalid or expired"
                )
                logger.error(
                    "💡 Solution: Get a valid Fireworks API key and set FIREWORKS_API_KEY environment variable"
                )
            elif response.status_code == 429:
                logger.error("❌ Fireworks API: Rate limit exceeded")
            elif response.status_code == 500:
                logger.error("❌ Fireworks API: Server error")

        response.raise_for_status()

        result = response.json()
        assistant_message = result["choices"][0]["message"]["content"].strip()

        # Allow longer responses but keep reasonable for voice
        if len(assistant_message) > 500:
            assistant_message = assistant_message[:497] + "..."

        logger.info(f"✅ Got Fireworks response: {assistant_message}")
        return assistant_message

    except Exception as e:
        logger.error(f"❌ Fireworks AI error: {e}")
        # Try OpenAI as fallback
        try:
            logger.info("🔄 Attempting OpenAI fallback...")
            return get_openai_fallback_response(conversation_history, current_message)
        except Exception as fallback_error:
            logger.error(f"❌ OpenAI fallback also failed: {fallback_error}")
            return "Both Fireworks and OpenAI are currently unavailable. Please check your API keys."


def get_openai_fallback_response(conversation_history, current_message):
    """Fallback to OpenAI if Fireworks fails"""
    try:
        import openai

        # Get OpenAI API key from environment
        openai_api_key = os.getenv("OPENAI_API_KEY")
        if not openai_api_key:
            raise Exception(
                "OpenAI API key not available. Set OPENAI_API_KEY environment variable."
            )

        # Prepare messages for OpenAI with Bytoid-specific prompt
        messages = [
            {
                "role": "system",
                "content": "You are Eva, Bytoid's helpful AI assistant. Give direct, specific answers to user questions about Bytoid and its components. When users ask about 'Hubstack' or 'Hub components', they're referring to Bytoid's integrated platform components: Agents Hub (AI agent management), AI Assistant Chat, Email management, Playbook workflows, Tickets system, Contacts CRM, AI Reporting, Search Email, Session Management, Credits system, Google/Microsoft/Facebook integrations, Webhooks, and Unified Mailbox. Explain these components clearly when asked. If you're not sure about a specific term, acknowledge that and suggest related Bytoid features. Answer exactly what the user asks: HOW questions get step-by-step instructions, WHAT questions get clear explanations, WHY questions get benefits. Only redirect to general Bytoid features if the question is completely unrelated to business software. Keep answers helpful and conversational.",
            }
        ]

        # Add conversation history (limit to last 5 for context)
        for conv in conversation_history[-5:]:
            messages.append({"role": conv["role"], "content": conv["message"]})

        # Add current message
        if (
            not conversation_history
            or conversation_history[-1]["message"] != current_message
        ):
            messages.append({"role": "user", "content": current_message})

        # Call OpenAI API
        client = openai.OpenAI(api_key=openai_api_key)
        response = client.chat.completions.create(
            model="gpt-3.5-turbo", messages=messages, max_tokens=300, temperature=0.3
        )

        assistant_message = response.choices[0].message.content.strip()

        # Allow longer responses but keep reasonable for voice
        if len(assistant_message) > 500:
            assistant_message = assistant_message[:497] + "..."

        logger.info(f"✅ Got OpenAI fallback response: {assistant_message}")
        return assistant_message

    except Exception as e:
        logger.error(f"❌ OpenAI fallback error: {e}")
        raise e


def correct_spelling_in_text(text):
    """Correct spelling using Fireworks Llama 3.3 70B - context-aware to nearest meaningful word."""
    if not text or len(text.strip()) < 3 or len(text) > 2000:
        return text

    # Try both API key names (FIREWORKS_API_KEY or FIREWORKS_KEY)
    api_key = os.getenv("FIREWORKS_KEY")
    if not api_key:
        return text

    try:
        payload = {
            "model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
            "max_tokens": min(len(text) + 50, 500),
            "temperature": 0.1,
            "messages": [
                {
                    "role": "user",
                    "content": f'Fix ONLY spelling/grammar errors. Keep meaning, style, and tech terms unchanged. Text: "{text}"\n\nCorrected text:',
                }
            ],
        }

        resp = requests.post(
            "https://api.fireworks.ai/inference/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=8,
        )

        if resp.status_code == 200:
            corrected = resp.json()["choices"][0]["message"]["content"].strip()
            # Verify it's not too different (>70% word overlap)
            orig_words = set(text.lower().split())
            corr_words = set(corrected.lower().split())
            if (
                orig_words
                and len(orig_words & corr_words) / len(orig_words | corr_words) >= 0.7
            ):
                logger.debug(f"✓ Corrected: {text[:40]}... → {corrected[:40]}...")
                return corrected
        return text
    except Exception as e:
        logger.debug(f"⚠️ Spell correction failed: {e}")
        return text


def correct_spelling_in_qa_pairs(qa_pairs):
    """Correct spelling in Q&A list before saving to Lance DB."""
    if not qa_pairs or not isinstance(qa_pairs, list):
        return qa_pairs

    corrected = []
    for qa in qa_pairs:
        if isinstance(qa, dict) and "question" in qa and "answer" in qa:
            qa_copy = qa.copy()
            qa_copy["question"] = correct_spelling_in_text(qa.get("question", ""))
            qa_copy["answer"] = correct_spelling_in_text(qa.get("answer", ""))
            corrected.append(qa_copy)
        else:
            corrected.append(qa)

    logger.info(f"✓ Spell corrected {len(corrected)} Q&A pairs")
    return corrected


def store_qa_conversation_as_json(user_id, transcript, answer):
    """Store Q&A conversation as JSON file for frontend FAQ display with AI filtering and spelling correction"""
    try:
        # Use AI-based filtering instead of strict rules
        try:
            from utils.content_filter import is_question_bytoid_related_ai
        except ImportError:
            from utils.content_filter import is_bytoid_related_question

            def is_question_bytoid_related_ai(text):
                result, _, _ = is_bytoid_related_question(text)
                return result

        # Check if question is Bytoid-related using AI
        if not is_question_bytoid_related_ai(transcript):
            logger.warning(
                f"🚫 Question blocked from user {user_id}: Not Bytoid-related"
            )
            # Don't store non-Bytoid content
            return False

        logger.info(f"✅ Bytoid-related question approved from user {user_id}")

        # ✏️ Apply spelling correction to question and answer
        logger.info(f"📝 Correcting spelling in question from user {user_id}...")
        corrected_question = correct_spelling_in_text(transcript.strip())

        logger.info(f"📝 Correcting spelling in answer for user {user_id}...")
        corrected_answer = correct_spelling_in_text(answer.strip())

        timestamp = datetime.now(timezone.utc)
        timestamp_str = timestamp.strftime("%Y%m%d_%H%M%S")

        # Create Q&A entry in FAQ format with corrected text
        qa_entry = {
            "id": f"qa_{timestamp_str}_{user_id}",
            "timestamp": timestamp.isoformat(),
            "user_id": user_id,
            "question": corrected_question,  # Store corrected version
            "answer": corrected_answer,  # Store corrected version
            "original_question": transcript.strip(),  # Keep original for reference
            "original_answer": answer.strip(),  # Keep original for reference
            "created_at": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "type": "voice_conversation",
            "bytoid_related": True,  # Only Bytoid questions are stored
            "spelling_corrected": True,  # Flag that spelling was corrected
        }

        # Store individual Q&A file
        s3_client = s3bucket()
        qa_filename = f"qa_{timestamp_str}.json"
        s3_key = f"qa-conversations/{user_id}/{qa_filename}"

        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=s3_key,
            Body=json.dumps(qa_entry, indent=2),
            ContentType="application/json",
        )

        # Clear in-memory cache when new content is added (fast)
        with cache_lock:
            FAQ_CACHE["all_questions"] = None
            FAQ_CACHE["last_updated"] = None

        # Also clear JSON file cache (fast disk operation, not S3 rebuild)
        # This way the next FAQ view will rebuild from S3 in ~7 seconds
        json_cache_file = ALL_FAQ_JSON
        if os.path.exists(json_cache_file):
            try:
                os.remove(json_cache_file)
                logger.info(
                    f"🗑️ Cleared FAQ JSON cache to show new questions immediately"
                )
            except Exception as e:
                logger.debug(f"⚠️ Could not clear JSON cache: {e}")

        logger.info(
            f"✅ Stored Bytoid Q&A conversation with spelling correction to S3: {s3_key}"
        )
        logger.info(
            f"✅ In-memory cache cleared - will rebuild from S3 when JSON cache expires (600s)"
        )

        # Also update the master FAQ list for this user
        update_user_faq_list(user_id, qa_entry)

        return qa_entry

    except Exception as e:
        logger.error(f"❌ Error storing Q&A conversation: {e}")
        return None


def update_user_faq_list(user_id, qa_entry):
    """Update the master FAQ list for the user"""
    try:
        s3_client = s3bucket()
        faq_list_key = f"qa-conversations/{user_id}/faq_list.json"

        # Try to get existing FAQ list
        try:
            response = s3_client.get_object(Bucket=S3_BUCKET, Key=faq_list_key)
            faq_data = json.loads(response["Body"].read().decode("utf-8"))
        except s3_client.exceptions.NoSuchKey:
            # Create new FAQ list if doesn't exist
            faq_data = {
                "user_id": user_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "total_conversations": 0,
                "conversations": [],
            }

        # Add new Q&A to the list
        faq_data["conversations"].append(qa_entry)
        faq_data["total_conversations"] = len(faq_data["conversations"])
        faq_data["updated_at"] = datetime.now(timezone.utc).isoformat()

        # Keep only the latest 50 conversations
        if len(faq_data["conversations"]) > 50:
            faq_data["conversations"] = faq_data["conversations"][-50:]
            faq_data["total_conversations"] = 50

        # Upload updated FAQ list
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=faq_list_key,
            Body=json.dumps(faq_data, indent=2),
            ContentType="application/json",
        )

        logger.info(
            f"✅ Updated FAQ list for user {user_id}: {len(faq_data['conversations'])} conversations"
        )

    except Exception as e:
        logger.error(f"❌ Error updating FAQ list: {e}")


# ==================== Q&A VOICE CONVERSATION ENDPOINTS ====================


@onboarding_bps.route("/qa/voice-conversation", methods=["POST", "OPTIONS"])
def qa_voice_conversation():
    """Handle Q&A voice conversation - simple voice-only version"""
    polly_client = get_polly_instance()
    # Handle CORS preflight request
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )
        response.headers.add(
            "Access-Control-Allow-Methods", "GET,PUT,POST,DELETE,OPTIONS"
        )
        return response

    try:
        # Handle both JSON and form data requests
        if request.is_json:
            data = request.json or {}
            user_id = data.get("user_id")
        else:
            data = request.form.to_dict()
            user_id = data.get("user_id")

        if not user_id:
            return jsonify({"error": "user_id is required"}), 400

        # EARLY DEDUP: Create a fingerprint of the incoming request BEFORE processing
        # This prevents duplicate audio submissions from being processed simultaneously
        request_fingerprint = None

        # Read transcript early to create better dedup key
        transcript = None
        audio_file = None
        audio_field_names = [
            "audio",
            "question",
            "voice",
            "recording",
            "file",
            "audioData",
            "uploadedFile",
        ]

        for field_name in audio_field_names:
            if field_name in request.files:
                file_obj = request.files[field_name]
                if file_obj.filename:
                    audio_file = file_obj
                    break

        if "text_message" in data:
            transcript = data["text_message"]

        # If we have either audio or text, create a fingerprint for early dedup
        if audio_file or transcript:
            if transcript:
                # For text, hash the text directly
                request_fingerprint = hashlib.md5(transcript.encode()).hexdigest()[:8]
            elif audio_file:
                # For audio, read the file and hash its content
                try:
                    audio_file.seek(0)
                    audio_content = audio_file.read()
                    audio_file.seek(0)
                    request_fingerprint = hashlib.md5(audio_content).hexdigest()[:8]
                except Exception:
                    # If we can't hash, generate a time-based fingerprint
                    request_fingerprint = str(int(time.time() * 1000))

            # Check for duplicate BEFORE processing
            early_request_id = f"{user_id}_early_{request_fingerprint}"
            with voice_request_lock:
                if early_request_id in voice_request_cache:
                    cached = voice_request_cache[early_request_id]
                    if (
                        isinstance(cached, dict)
                        and cached.get("status") == "processing"
                    ):
                        logger.warning(
                            f"⚠️ DUPLICATE DETECTED - Request already being processed: {early_request_id}"
                        )
                        # Wait a bit and return error to prevent double processing
                        return (
                            jsonify(
                                {
                                    "status": "duplicate",
                                    "error": "This question is already being processed",
                                    "message": "Please wait for the previous request to complete",
                                }
                            ),
                            429,
                        )  # Too Many Requests
                    elif isinstance(cached, dict) and cached.get("status") == "success":
                        logger.info(
                            f"✅ EARLY CACHE HIT - Returning previously computed response"
                        )
                        response = jsonify(cached)
                        response.headers.add("Access-Control-Allow-Origin", "*")
                        return response

                # Mark as being processed
                voice_request_cache[early_request_id] = {"status": "processing"}

        if audio_file:
            # Process audio file
            try:
                import tempfile
                import os

                # Get file extension
                file_ext = ".webm"
                if audio_file.filename:
                    file_ext = os.path.splitext(audio_file.filename)[1] or ".webm"

                # Save to temporary file
                with tempfile.NamedTemporaryFile(
                    suffix=file_ext, delete=False
                ) as temp_file:
                    audio_file.save(temp_file.name)
                    temp_audio_path = temp_file.name

                # Transcribe using Speech2TextService
                speech_service = Speech2TextService(userid=user_id)
                import asyncio

                # Run transcription
                transcript = asyncio.run(
                    speech_service.transcribe_audio(temp_audio_path)
                )

                # Clean up
                if os.path.exists(temp_audio_path):
                    os.remove(temp_audio_path)

                if not transcript:
                    return jsonify({"error": "Failed to transcribe audio"}), 500

            except Exception as e:
                return jsonify({"error": f"Audio processing failed: {str(e)}"}), 500

        elif "text_message" in data:
            # Text message provided directly
            transcript = data["text_message"]
        else:
            return (
                jsonify({"error": "Either audio file or text_message is required"}),
                400,
            )

        # NOW that we have the transcript, check for duplicate requests (STRONG DEDUP)
        # This prevents the same question from being processed twice
        transcript_hash = hashlib.md5(transcript.encode()).hexdigest()[:8]
        request_id = f"{user_id}_{transcript_hash}"

        with voice_request_lock:
            if request_id in voice_request_cache:
                logger.info(
                    f"📋 DUPLICATE REQUEST BLOCKED - Returning cached response for {user_id}"
                )
                cached_response = voice_request_cache[request_id]
                response = jsonify(cached_response)
                response.headers.add("Access-Control-Allow-Origin", "*")
                return response

            # Mark this request as being processed (prevent concurrent processing)
            voice_request_cache[request_id] = {"status": "processing"}

        # Get conversation history for context
        conversation_history = get_conversation_from_s3(user_id, limit=5)

        # Get AI response using Fireworks Llama
        ai_response = get_fireworks_llama_response(conversation_history, transcript)

        # Store conversation messages for future use
        store_conversation_to_s3(user_id, "user", transcript)
        store_conversation_to_s3(user_id, "assistant", ai_response)

        # ✅ Store Q&A to FAQ list for community display (with Bytoid filtering)
        qa_entry = store_qa_conversation_as_json(user_id, transcript, ai_response)
        if qa_entry:
            logger.info(f"✅ Q&A stored to FAQ list for community display")
        else:
            logger.warning(f"⚠️ Q&A not stored to FAQ (filtered out or error)")

        # Initialize response variables
        speech_url = None

        # Generate speech from AI response (with caching to avoid duplicate generation)
        import tempfile
        import uuid
        from io import BytesIO

        try:
            # Create a hash of the response to check if we already generated speech for it
            response_hash = hashlib.md5(ai_response.encode()).hexdigest()[:8]
            cached_speech_key = f"qa-audio/response_{response_hash}.mp3"

            # First, check if speech already exists in S3 for this response
            s3_client = s3bucket()
            try:
                s3_client.head_object(Bucket=S3_BUCKET, Key=cached_speech_key)
                # Speech already exists, use it
                speech_url = attach_CLDFRNT_url(cached_speech_key)
                logger.info(f"✅ Using cached speech URL: {speech_url}")
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    # Speech doesn't exist, generate it
                    # Check if polly_client is available (defined at module level)
                    if "polly_client" not in globals() or not polly_client:
                        logger.error("❌ Polly client not available")
                        speech_url = None
                    else:
                        # Generate speech using AWS Polly - ONLY ONCE
                        logger.info(
                            f"🎵 Generating new speech for response hash: {response_hash}"
                        )
                        polly_response = polly_client.synthesize_speech(
                            Text=ai_response,
                            OutputFormat="mp3",
                            VoiceId="Danielle",
                            Engine="neural",
                        )

                        # Upload to S3 with cached filename
                        audio_stream = polly_response["AudioStream"]

                        s3_client.upload_fileobj(
                            BytesIO(audio_stream.read()),
                            S3_BUCKET,
                            cached_speech_key,
                            ExtraArgs={"ContentType": "audio/mpeg"},
                        )

                        # Generate CloudFront URL
                        speech_url = attach_CLDFRNT_url(cached_speech_key)
                        logger.info(f"✅ Generated and cached speech URL: {speech_url}")
                else:
                    raise

        except Exception as e:
            logger.error(f"❌ Speech generation error: {e}")
            speech_url = None  # Set to None instead of returning error

        # Always return consistent response format
        # NOTE: speech_url is for the ANSWER ONLY - frontend should NOT generate additional voice for question
        response_data = {
            "status": "success",
            "transcript": transcript,
            "ai_response": ai_response,
            "speech_url": speech_url,  # ANSWER voice only
            "has_speech": speech_url is not None,
            "question_voice": None,  # Explicitly NO voice generation for question
            "note": "Only answer voice is provided - do not generate voice for question",
        }

        # Cache this response to prevent duplicate requests
        with voice_request_lock:
            # Store final response with both request IDs for dedup
            response_data["status"] = "success"  # Ensure status is success
            voice_request_cache[request_id] = response_data

            # Also mark the early request as successful
            if early_request_id:
                voice_request_cache[early_request_id] = response_data

            # Clean up old processing markers (entries that are still "processing" should be cleaned up)
            # This prevents stale "processing" markers from blocking legitimate requests
            keys_to_remove = []
            for k, v in voice_request_cache.items():
                if isinstance(v, dict) and v.get("status") == "processing":
                    keys_to_remove.append(k)
            for k in keys_to_remove:
                del voice_request_cache[k]

        logger.info(f"📤 Sending response (cached for dedup)")
        logger.info(
            f"✅ Response contains: speech_url={response_data.get('speech_url') is not None}, answer={len(response_data.get('ai_response', '')) > 0}"
        )

        response = jsonify(response_data)
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )
        return response

    except Exception as e:
        logger.error(f"❌ Error in Q&A voice conversation: {e}")
        # Always return consistent structure even on error
        error_response = jsonify(
            {
                "status": "error",
                "error": str(e),
                "transcript": None,
                "ai_response": None,
                "speech_url": None,
                "has_speech": False,
            }
        )
        error_response.headers.add("Access-Control-Allow-Origin", "*")
        error_response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )
        return error_response, 500


@onboarding_bps.route("/qa/conversations/<user_id>", methods=["GET"])
def get_qa_conversations(user_id):
    """Get conversation history for a user - frontend ready format"""
    try:
        # Get conversation history using existing function
        conversations = get_conversation_from_s3(user_id, limit=50)

        # Format for frontend display
        formatted_conversations = []
        current_pair = {}

        for conv in conversations:
            if conv["role"] == "user":
                current_pair = {
                    "id": conv["timestamp"],
                    "timestamp": conv["timestamp"],
                    "question": conv["message"],
                    "answer": None,
                }
            elif conv["role"] == "assistant" and current_pair:
                current_pair["answer"] = conv["message"]
                formatted_conversations.append(current_pair)
                current_pair = {}

        # Add any incomplete pairs (questions without answers)
        if current_pair and current_pair.get("question"):
            current_pair["answer"] = "Processing..."
            formatted_conversations.append(current_pair)

        # Sort by timestamp (newest first for frontend display)
        formatted_conversations.sort(key=lambda x: x["timestamp"], reverse=True)

        response_data = {
            "user_id": user_id,
            "total_conversations": len(formatted_conversations),
            "conversations": formatted_conversations,
        }

        response = jsonify(response_data)
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )

        logger.info(
            f"✅ Retrieved {len(formatted_conversations)} Q&A conversations for user {user_id}"
        )
        return response

    except Exception as e:
        logger.error(f"❌ Error retrieving Q&A conversations: {e}")
        error_response = jsonify({"error": str(e)})
        error_response.headers.add("Access-Control-Allow-Origin", "*")
        return error_response, 500


@onboarding_bps.route("/qa/user-questions/<user_id>", methods=["GET", "OPTIONS"])
def get_user_questions_faq(user_id):
    """Get ALL user's questions in FAQ format for frontend display (no deletion - keeps all conversations)"""
    # Handle CORS preflight
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )
        response.headers.add("Access-Control-Allow-Methods", "GET,OPTIONS")
        return response

    try:
        # 🚀 ULTRA-FAST: Try JSON file first (0.01s vs 6+ seconds!)
        user_faq_file = os.path.join(FAQ_JSON_DIR, f"user_faq_{user_id}.json")

        if os.path.exists(user_faq_file):
            # Load directly from JSON file
            with open(user_faq_file, "r", encoding="utf-8") as f:
                user_faq_data = json.load(f)

            cached_questions = user_faq_data.get("questions", [])
            file_age = time.time() - os.path.getmtime(user_faq_file)
            file_size_kb = round(os.path.getsize(user_faq_file) / 1024, 2)

            logger.info(
                f"� ULTRA-FAST: Using JSON file for user {user_id}: {len(cached_questions)} questions ({file_size_kb}KB)"
            )

            response_data = {
                "status": "success",
                "user_id": user_id,
                "heading": "Your Personal Questions",
                "description": "Questions you have asked",
                "total_questions": len(cached_questions),
                "questions": cached_questions,
                "cached": True,
                "performance": {
                    "source": "json_file",
                    "file_size_kb": file_size_kb,
                    "file_age_seconds": round(file_age, 2),
                    "load_method": "ultra_fast_json",
                },
            }

            response = jsonify(response_data)
            response.headers.add("Access-Control-Allow-Origin", "*")
            response.headers.add(
                "Access-Control-Allow-Headers", "Content-Type,Authorization"
            )
            return response

        # Fallback: Create JSON file from S3 (first time only)
        logger.info(f"📋 Creating JSON file for user {user_id} from S3...")

        # Get conversation history (limit to more for cleanup, then filter)
        conversations = get_conversation_from_s3(user_id, limit=200)

        # Group conversations into Q&A pairs
        qa_pairs = []
        current_question = None

        for conv in sorted(conversations, key=lambda x: x["timestamp"]):
            if conv["role"] == "user":
                # Comprehensive filtering for questions
                should_allow, reason, details = should_allow_question(conv["message"])
                if not should_allow:
                    logger.debug(f"🚫 Filtering question from user {user_id}: {reason}")
                    continue
                # Save previous Q&A if exists
                if current_question:
                    qa_pairs.append(
                        {
                            "id": current_question["timestamp"],
                            "timestamp": current_question["timestamp"],
                            "question": current_question["message"],
                            "answer": "No response recorded",
                            "status": "incomplete",
                        }
                    )

                current_question = conv

            elif conv["role"] == "assistant" and current_question:
                # Content filtering for answers
                answer_text = conv["message"]
                if not quick_content_check(answer_text):
                    logger.debug(
                        f"🚫 Filtering inappropriate answer for user {user_id}"
                    )
                    answer_text = get_bytoid_focused_response()

                # Complete the Q&A pair
                qa_pairs.append(
                    {
                        "id": current_question["timestamp"],
                        "timestamp": current_question["timestamp"],
                        "question": current_question["message"],
                        "answer": answer_text,
                        "status": "complete",
                    }
                )
                current_question = None

        # Add any remaining incomplete question (with comprehensive filtering)
        if current_question:
            should_allow, reason, details = should_allow_question(
                current_question["message"]
            )
            if should_allow:
                qa_pairs.append(
                    {
                        "id": current_question["timestamp"],
                        "timestamp": current_question["timestamp"],
                        "question": current_question["message"],
                        "answer": "Processing...",
                        "status": "processing",
                    }
                )

        # Sort by timestamp (newest first) - keep ALL questions
        qa_pairs.sort(key=lambda x: x["timestamp"], reverse=True)

        # Save to JSON file for faster loading next time
        save_user_faq_to_json(user_id, qa_pairs)

        # Format response for frontend FAQ display
        response_data = {
            "status": "success",
            "user_id": user_id,
            "heading": "Your Personal Questions",
            "description": "Questions you have asked",
            "total_questions": len(qa_pairs),
            "questions": qa_pairs,
        }

        response = jsonify(response_data)
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )

        logger.info(f"✅ Retrieved {len(qa_pairs)} FAQ questions for user {user_id}")
        return response

    except Exception as e:
        logger.error(f"❌ Error retrieving user questions: {e}")
        error_response = jsonify(
            {
                "status": "error",
                "error": str(e),
                "message": "Failed to retrieve your questions",
            }
        )
        error_response.headers.add("Access-Control-Allow-Origin", "*")
        return error_response, 500


@onboarding_bps.route(
    "/qa/question/<user_id>/<question_id>", methods=["GET", "OPTIONS"]
)
def get_single_question_answer(user_id, question_id):
    """Get a specific question and answer by ID for expandable view"""
    # Handle CORS preflight
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )
        response.headers.add("Access-Control-Allow-Methods", "GET,OPTIONS")
        return response

    try:
        # Get all conversations for user
        conversations = get_conversation_from_s3(user_id, limit=200)

        # Find the specific question and its answer
        question_data = None
        answer_data = None

        for conv in conversations:
            if conv["timestamp"] == question_id and conv["role"] == "user":
                question_data = conv
            elif conv["role"] == "assistant" and question_data:
                # Find the corresponding answer (next assistant message after the question)
                if conv["timestamp"] > question_data["timestamp"]:
                    answer_data = conv
                    break

        if not question_data:
            return jsonify({"status": "error", "error": "Question not found"}), 404

        # Format the response
        response_data = {
            "status": "success",
            "user_id": user_id,
            "question_id": question_id,
            "question": question_data["message"],
            "answer": answer_data["message"] if answer_data else "No response recorded",
            "question_timestamp": question_data["timestamp"],
            "answer_timestamp": answer_data["timestamp"] if answer_data else None,
            "has_answer": answer_data is not None,
        }

        response = jsonify(response_data)
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )

        return response

    except Exception as e:
        logger.error(f"❌ Error retrieving question {question_id}: {e}")
        error_response = jsonify({"status": "error", "error": str(e)})
        error_response.headers.add("Access-Control-Allow-Origin", "*")
        return error_response, 500


@onboarding_bps.route("/qa/recent-questions/<user_id>", methods=["GET", "OPTIONS"])
def get_recent_questions(user_id):
    """Get all user questions for FAQ display (no limit - keeps all conversations)"""
    # Handle CORS preflight
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )
        response.headers.add("Access-Control-Allow-Methods", "GET,OPTIONS")
        return response

    try:
        # Get recent conversations (increased limit since we're keeping all)
        conversations = get_conversation_from_s3(user_id, limit=50)

        # Get ALL user questions (no limit)
        questions = []
        for conv in sorted(conversations, key=lambda x: x["timestamp"], reverse=True):
            if conv["role"] == "user":
                # Get preview of question (first 100 chars)
                question_preview = conv["message"][:100] + (
                    "..." if len(conv["message"]) > 100 else ""
                )

                questions.append(
                    {
                        "id": conv["timestamp"],
                        "question_preview": question_preview,
                        "full_question": conv["message"],
                        "timestamp": conv["timestamp"],
                    }
                )

        response_data = {
            "status": "success",
            "user_id": user_id,
            "recent_questions": questions,
        }

        response = jsonify(response_data)
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )

        return response

    except Exception as e:
        logger.error(f"❌ Error retrieving recent questions: {e}")
        error_response = jsonify({"status": "error", "error": str(e)})
        error_response.headers.add("Access-Control-Allow-Origin", "*")
        return error_response, 500


@onboarding_bps.route("/qa/conversation/<conversation_id>", methods=["GET"])
def get_single_qa_conversation(conversation_id):
    """Get a single Q&A conversation by ID"""
    try:
        if not conversation_id.startswith("qa_"):
            return jsonify({"error": "Invalid conversation ID format"}), 400

        # Parse conversation_id format: qa_YYYYMMDD_HHMMSS_user_id
        parts = conversation_id.split("_")
        if len(parts) < 4:
            return jsonify({"error": "Invalid conversation ID format"}), 400

        timestamp_str = f"{parts[1]}_{parts[2]}"
        user_id = "_".join(parts[3:])

        # Construct the exact S3 key instead of listing files
        s3_key = f"qa-conversations/{user_id}/qa_{timestamp_str}.json"

        s3_client = s3bucket()

        try:
            obj_response = s3_client.get_object(Bucket=S3_BUCKET, Key=s3_key)
            qa_data = json.loads(obj_response["Body"].read().decode("utf-8"))
            logger.info(f"✅ Retrieved Q&A conversation: {conversation_id}")
            return jsonify(qa_data)

        except s3_client.exceptions.NoSuchKey:
            logger.warning(f"❌ Q&A file not found with key: {s3_key}")
            return jsonify({"error": "Q&A conversation not found"}), 404

    except Exception as e:
        logger.error(f"❌ Error retrieving single Q&A conversation: {e}")
        return jsonify({"error": str(e)}), 500


def get_qa_conversations_from_s3_optimized():
    """OPTIMIZED: Get ONLY Q&A conversations from qa-conversations/ folder (much faster than full fetch)"""
    try:
        s3_client = s3bucket()
        all_qa_pairs = []

        # List all user folders under qa-conversations/
        qa_prefix = "qa-conversations/"
        response = s3_client.list_objects_v2(
            Bucket=S3_BUCKET, Prefix=qa_prefix, Delimiter="/"
        )

        # Get user directories
        user_folders = []
        if "CommonPrefixes" in response:
            for prefix_info in response["CommonPrefixes"]:
                folder_path = prefix_info["Prefix"]
                user_id = folder_path.replace("qa-conversations/", "").rstrip("/")
                if user_id:
                    user_folders.append(user_id)

        logger.info(f"📂 Fetching Q&A from {len(user_folders)} users")

        # Get Q&A files from each user
        for user_id in user_folders:
            try:
                user_prefix = f"qa-conversations/{user_id}/"
                user_response = s3_client.list_objects_v2(
                    Bucket=S3_BUCKET, Prefix=user_prefix
                )

                if "Contents" in user_response:
                    files = sorted(
                        user_response["Contents"],
                        key=lambda x: x["LastModified"],
                        reverse=True,
                    )

                    for file_obj in files:
                        # Skip faq_list.json and other metadata
                        if "faq_list" in file_obj["Key"] or not file_obj[
                            "Key"
                        ].endswith(".json"):
                            continue

                        try:
                            obj_response = s3_client.get_object(
                                Bucket=S3_BUCKET, Key=file_obj["Key"]
                            )
                            qa_data = json.loads(
                                obj_response["Body"].read().decode("utf-8")
                            )

                            # Only process actual Q&A data (has both question and answer)
                            if (
                                isinstance(qa_data, dict)
                                and "question" in qa_data
                                and "answer" in qa_data
                            ):
                                qa_data["source_user_id"] = user_id
                                all_qa_pairs.append(qa_data)
                        except Exception as e:
                            logger.debug(
                                f"Error reading Q&A file {file_obj['Key']}: {e}"
                            )
                            continue
            except Exception as e:
                logger.debug(f"Error reading Q&A for user {user_id}: {e}")
                continue

        logger.info(
            f"✅ Retrieved {len(all_qa_pairs)} Q&A pairs from qa-conversations/"
        )
        return all_qa_pairs

    except Exception as e:
        logger.error(f"❌ Error getting Q&A conversations from S3: {e}")
        return []


def get_all_users_conversations_from_s3(limit_per_user=50):
    """Get conversation history from ALL users' S3 folders (both conversations/ and qa-conversations/)"""
    try:
        s3_client = s3bucket()
        all_conversations = []

        # List all user folders under conversations/
        prefix = "conversations/"
        response = s3_client.list_objects_v2(
            Bucket=S3_BUCKET, Prefix=prefix, Delimiter="/"
        )

        # Get user directories from conversations/
        user_folders_conv = []
        if "CommonPrefixes" in response:
            for prefix_info in response["CommonPrefixes"]:
                folder_path = prefix_info["Prefix"]
                user_id = folder_path.replace("conversations/", "").rstrip("/")
                if user_id:  # Skip empty user IDs
                    user_folders_conv.append(user_id)

        # Also get user directories from qa-conversations/
        user_folders_qa = []
        qa_prefix = "qa-conversations/"
        try:
            qa_response = s3_client.list_objects_v2(
                Bucket=S3_BUCKET, Prefix=qa_prefix, Delimiter="/"
            )
            if "CommonPrefixes" in qa_response:
                for prefix_info in qa_response["CommonPrefixes"]:
                    folder_path = prefix_info["Prefix"]
                    user_id = folder_path.replace("qa-conversations/", "").rstrip("/")
                    if user_id:
                        user_folders_qa.append(user_id)
        except Exception as e:
            logger.debug(f"⚠️ Could not retrieve qa-conversations folders: {e}")

        logger.info(
            f"📂 Found {len(user_folders_conv)} users in conversations/ and {len(user_folders_qa)} users in qa-conversations/"
        )

        # Get conversations from conversations/ folder
        for user_id in user_folders_conv:
            try:
                user_conversations = get_conversation_from_s3(
                    user_id, limit=limit_per_user, source_folder="conversations"
                )

                # Add user_id to each conversation for identification
                for conv in user_conversations:
                    conv["source_user_id"] = user_id
                    all_conversations.append(conv)

            except Exception as e:
                logger.debug(f"⚠️ Error getting conversations for user {user_id}: {e}")
                continue

        # Get Q&A conversations from qa-conversations/ folder (from ALL users, including those in conversations/)
        for user_id in user_folders_qa:
            try:
                user_conversations = get_conversation_from_s3(
                    user_id, limit=limit_per_user, source_folder="qa-conversations"
                )

                # Add user_id to each conversation for identification
                for conv in user_conversations:
                    conv["source_user_id"] = user_id
                    all_conversations.append(conv)

            except Exception as e:
                logger.debug(
                    f"⚠️ Error getting Q&A conversations for user {user_id}: {e}"
                )
                continue

        # Sort all conversations by timestamp (newest first)
        all_conversations.sort(key=lambda x: x["timestamp"], reverse=True)

        logger.info(
            f"✅ Retrieved {len(all_conversations)} total conversations from {len(user_folders_conv) + len(user_folders_qa)} users"
        )
        return all_conversations

    except Exception as e:
        logger.error(f"❌ Error getting all users' conversations from S3: {e}")
        return []


def is_cache_valid():
    """Check if FAQ cache is still valid"""
    if FAQ_CACHE["last_updated"] is None or FAQ_CACHE["all_questions"] is None:
        return False

    cache_age = time.time() - FAQ_CACHE["last_updated"]
    return cache_age < FAQ_CACHE["cache_duration"]


def get_cached_faq_data():
    """Get FAQ data from JSON cache (fastest) - use AI filtering for Bytoid relevance"""
    # FIRST - Try local JSON file cache (persistent, fast)
    # NOTE: Cache max age is now 60 seconds to ensure new questions appear quickly
    cached_questions = load_faq_from_json()
    if cached_questions and len(cached_questions) > 0:
        logger.info(
            f"✅ Using fresh JSON file cache: {len(cached_questions)} questions (age < 60s)"
        )
        return cached_questions

    # Cache file is missing or expired - rebuild from S3
    logger.info("🔄 JSON cache missing or expired (> 60s) - rebuilding from S3...")

    # SECOND - Try in-memory cache
    with cache_lock:
        if is_cache_valid() and FAQ_CACHE["all_questions"]:
            logger.info("✅ Using in-memory cache")
            save_faq_to_json(FAQ_CACHE["all_questions"])  # Persist to disk
            return FAQ_CACHE["all_questions"]

        logger.info("🔄 Building fresh FAQ data with AI filtering from S3...")

        try:
            from utils.content_filter import is_question_bytoid_related_ai
        except ImportError:
            logger.warning("Could not import AI filter, using fallback")
            from utils.content_filter import is_bytoid_related_question

            def is_question_bytoid_related_ai(text):
                result, _, _ = is_bytoid_related_question(text)
                return result

        # OPTIMIZED: Fetch ONLY Q&A conversations (much faster than fetching all conversations)
        conversations = get_qa_conversations_from_s3_optimized()
        logger.info(f"📥 Processing {len(conversations)} Q&A conversations for FAQ...")

        qa_pairs = []
        rejected_count = 0

        # Process Q&A pairs directly (no role-based processing needed)
        for conv in sorted(conversations, key=lambda x: x["timestamp"], reverse=True):
            user_id = conv.get("source_user_id", "unknown")

            # Q&A data is already formatted
            if "question" in conv and "answer" in conv:
                # Q&A pair already formatted - just add it directly
                question_text = conv.get("question", "")

                # Use AI to filter - only keep Bytoid-related questions
                if not is_question_bytoid_related_ai(question_text):
                    logger.debug(f"🚫 Filtered out: '{question_text[:50]}'")
                    rejected_count += 1
                    continue

                # Add the Q&A pair as-is
                qa_pairs.append(
                    {
                        "id": conv.get("id", f"{user_id}_{conv['timestamp']}"),
                        "timestamp": conv["timestamp"],
                        "question": question_text,
                        "answer": conv.get("answer", ""),
                        "status": "complete",
                        "user_id": user_id,
                        "source_user_id": user_id,
                    }
                )

        # Sort by newest first and limit
        qa_pairs.sort(key=lambda x: x["timestamp"], reverse=True)
        qa_pairs = qa_pairs[:50]  # Keep only 50 most recent

        # Remove duplicates before caching
        qa_pairs = dedupe_faqs(qa_pairs, keep="latest")

        # Update caches
        FAQ_CACHE["all_questions"] = qa_pairs
        FAQ_CACHE["last_updated"] = time.time()
        save_faq_to_json(qa_pairs)

        logger.info(
            f"✅ Built FAQ: {len(qa_pairs)} accepted, {rejected_count} rejected"
        )
        return qa_pairs


@onboarding_bps.route("/qa/all-questions", methods=["GET", "OPTIONS"])
def get_all_users_questions_faq():
    """Get ALL users' questions in FAQ format as JSON (no caching, no pagination)"""
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )
        response.headers.add("Access-Control-Allow-Methods", "GET,OPTIONS")
        return response

    try:
        logger.info("Fetching all FAQ questions from S3...")
        qa_pairs = get_cached_faq_data()
        qa_pairs = dedupe_faqs(qa_pairs, keep="latest")
        logger.info(f"Loaded {len(qa_pairs)} unique questions from S3")

        response_data = {
            "status": "success",
            "questions": qa_pairs,
            "total_questions": len(qa_pairs),
            "timestamp": time.time(),
        }

        response = jsonify(response_data)
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )
        logger.info(f"Served {len(qa_pairs)} FAQ questions from S3")
        return response

    except Exception as e:
        logger.error(f"Error retrieving questions: {e}")
        error_response = jsonify(
            {
                "status": "error",
                "error": str(e),
                "message": "Failed to retrieve community questions",
            }
        )
        error_response.headers.add("Access-Control-Allow-Origin", "*")
        return error_response, 500


@onboarding_bps.route("/qa/admin/clear-cache", methods=["POST", "OPTIONS"])
def clear_faq_cache_endpoint():
    """Clear FAQ cache and JSON files for administrators"""
    # Handle CORS preflight
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )
        response.headers.add("Access-Control-Allow-Methods", "POST,OPTIONS")
        return response

    try:
        # Clear in-memory cache
        with cache_lock:
            FAQ_CACHE["all_questions"] = None
            FAQ_CACHE["last_updated"] = None

        # Clear JSON files
        removed_files = []
        try:
            # Remove all FAQ JSON file
            if os.path.exists(ALL_FAQ_JSON):
                os.remove(ALL_FAQ_JSON)
                removed_files.append("all_faq_questions.json")

            # Remove user FAQ JSON files
            for filename in os.listdir(FAQ_JSON_DIR):
                if filename.startswith("user_faq_") and filename.endswith(".json"):
                    file_path = os.path.join(FAQ_JSON_DIR, filename)
                    os.remove(file_path)
                    removed_files.append(filename)

        except Exception as e:
            logger.warning(f"⚠️ Error removing some JSON files: {e}")

        logger.info(
            f"🗑️ FAQ cache cleared by admin, removed {len(removed_files)} JSON files"
        )

        response = jsonify(
            {
                "status": "success",
                "message": "FAQ cache cleared successfully",
                "removed_files": removed_files,
            }
        )
        response.headers.add("Access-Control-Allow-Origin", "*")
        return response

    except Exception as e:
        logger.error(f"❌ Error clearing FAQ cache: {e}")
        error_response = jsonify({"status": "error", "error": str(e)})
        error_response.headers.add("Access-Control-Allow-Origin", "*")
        return error_response, 500


@onboarding_bps.route("/qa/admin/cache-status", methods=["GET", "OPTIONS"])
def get_cache_status():
    """Get FAQ cache status including JSON files for administrators"""
    # Handle CORS preflight
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )
        response.headers.add("Access-Control-Allow-Methods", "GET,OPTIONS")
        return response

    try:
        # In-memory cache status
        cache_valid = is_cache_valid()
        cache_age = 0
        if FAQ_CACHE["last_updated"]:
            cache_age = time.time() - FAQ_CACHE["last_updated"]

        cached_questions = (
            len(FAQ_CACHE["all_questions"]) if FAQ_CACHE["all_questions"] else 0
        )

        # JSON file cache status
        json_files = []
        total_json_questions = 0

        try:
            # Check all FAQ JSON file
            if os.path.exists(ALL_FAQ_JSON):
                file_stat = os.stat(ALL_FAQ_JSON)
                file_age = time.time() - file_stat.st_mtime
                json_files.append(
                    {
                        "filename": "all_faq_questions.json",
                        "age_seconds": file_age,
                        "size_kb": round(file_stat.st_size / 1024, 2),
                        "valid": file_age < 300,  # 5 minutes
                    }
                )

                # Try to read question count
                try:
                    with open(ALL_FAQ_JSON, "r") as f:
                        data = json.load(f)
                    total_json_questions += data.get("total_questions", 0)
                except:
                    pass

            # Check user FAQ JSON files
            for filename in os.listdir(FAQ_JSON_DIR):
                if filename.startswith("user_faq_") and filename.endswith(".json"):
                    file_path = os.path.join(FAQ_JSON_DIR, filename)
                    file_stat = os.stat(file_path)
                    file_age = time.time() - file_stat.st_mtime
                    json_files.append(
                        {
                            "filename": filename,
                            "age_seconds": file_age,
                            "size_kb": round(file_stat.st_size / 1024, 2),
                            "valid": file_age < 300,  # 5 minutes
                        }
                    )

        except Exception as e:
            logger.warning(f"⚠️ Error checking JSON files: {e}")

        response_data = {
            "status": "success",
            "in_memory_cache": {
                "cache_valid": cache_valid,
                "cache_age_seconds": cache_age,
                "cached_questions": cached_questions,
                "cache_duration": FAQ_CACHE["cache_duration"],
                "last_updated": FAQ_CACHE["last_updated"],
            },
            "json_files": {
                "total_files": len(json_files),
                "total_questions": total_json_questions,
                "files": json_files,
            },
        }

        response = jsonify(response_data)
        response.headers.add("Access-Control-Allow-Origin", "*")
        return response

    except Exception as e:
        logger.error(f"❌ Error getting cache status: {e}")
        error_response = jsonify({"status": "error", "error": str(e)})
        error_response.headers.add("Access-Control-Allow-Origin", "*")
        return error_response, 500


@onboarding_bps.route("/qa/admin/save-json", methods=["POST", "OPTIONS"])
def save_faq_json():
    """Manually save current FAQ data to JSON file"""
    # Handle CORS preflight
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )
        response.headers.add("Access-Control-Allow-Methods", "POST,OPTIONS")
        return response

    try:
        # Get current FAQ data
        qa_pairs = get_cached_faq_data()

        # Save to JSON file
        success = save_faq_to_json(qa_pairs)

        if success:
            response = jsonify(
                {
                    "status": "success",
                    "message": f"Saved {len(qa_pairs)} FAQ questions to JSON file",
                    "questions_saved": len(qa_pairs),
                    "json_file": "all_faq_questions.json",
                }
            )
        else:
            response = jsonify(
                {"status": "error", "message": "Failed to save FAQ to JSON file"}
            )

        response.headers.add("Access-Control-Allow-Origin", "*")
        return response

    except Exception as e:
        logger.error(f"❌ Error saving FAQ to JSON: {e}")
        error_response = jsonify({"status": "error", "error": str(e)})
        error_response.headers.add("Access-Control-Allow-Origin", "*")
        return error_response, 500


@onboarding_bps.route("/qa/json/all-questions", methods=["GET", "OPTIONS"])
def get_faq_json_file():
    """Serve the FAQ JSON file directly for ultra-fast frontend loading"""
    # Handle CORS preflight
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )
        response.headers.add("Access-Control-Allow-Methods", "GET,OPTIONS")
        return response

    try:
        # Check if JSON file exists
        if not os.path.exists(ALL_FAQ_JSON):
            # Create JSON file first time
            logger.info("📋 JSON file not found, creating it...")
            qa_pairs = get_cached_faq_data()
            save_faq_to_json(qa_pairs)

        # Serve the JSON file directly
        with open(ALL_FAQ_JSON, "r", encoding="utf-8") as f:
            faq_data = json.load(f)

        # Add metadata for frontend
        file_age = time.time() - os.path.getmtime(ALL_FAQ_JSON)
        faq_data["file_age_seconds"] = file_age
        faq_data["served_from"] = "json_file"
        faq_data["file_size_kb"] = round(os.path.getsize(ALL_FAQ_JSON) / 1024, 2)

        response = jsonify(faq_data)
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )

        logger.info(
            f"📄 Served JSON file: {len(faq_data.get('questions', []))} questions, {faq_data['file_size_kb']}KB"
        )
        return response

    except Exception as e:
        logger.error(f"❌ Error serving FAQ JSON file: {e}")
        error_response = jsonify(
            {
                "status": "error",
                "error": str(e),
                "message": "Failed to load FAQ JSON file",
            }
        )
        error_response.headers.add("Access-Control-Allow-Origin", "*")
        return error_response, 500


@onboarding_bps.route("/qa/json/user-questions/<user_id>", methods=["GET", "OPTIONS"])
def get_user_faq_json_file(user_id):
    """Serve user-specific FAQ JSON file directly for ultra-fast loading"""
    # Handle CORS preflight
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )
        response.headers.add("Access-Control-Allow-Methods", "GET,OPTIONS")
        return response

    try:
        user_faq_file = os.path.join(FAQ_JSON_DIR, f"user_faq_{user_id}.json")

        # Check if user JSON file exists
        if not os.path.exists(user_faq_file):
            # Create user JSON file first time
            logger.info(f"📋 User JSON file not found for {user_id}, creating it...")
            # Get fresh user data (this will be slow first time only)
            conversations = get_conversation_from_s3(user_id, limit=200)

            qa_pairs = []
            current_question = None

            for conv in sorted(conversations, key=lambda x: x["timestamp"]):
                if conv["role"] == "user":
                    should_allow, reason, details = should_allow_question(
                        conv["message"]
                    )
                    if not should_allow:
                        continue

                    if current_question:
                        qa_pairs.append(
                            {
                                "id": current_question["timestamp"],
                                "timestamp": current_question["timestamp"],
                                "question": current_question["message"],
                                "answer": "No response recorded",
                                "status": "incomplete",
                            }
                        )
                    current_question = conv

                elif conv["role"] == "assistant" and current_question:
                    answer_text = conv["message"]
                    if not quick_content_check(answer_text):
                        answer_text = get_bytoid_focused_response()

                    qa_pairs.append(
                        {
                            "id": current_question["timestamp"],
                            "timestamp": current_question["timestamp"],
                            "question": current_question["message"],
                            "answer": answer_text,
                            "status": "complete",
                        }
                    )
                    current_question = None

            if current_question:
                should_allow, reason, details = should_allow_question(
                    current_question["message"]
                )
                if should_allow:
                    qa_pairs.append(
                        {
                            "id": current_question["timestamp"],
                            "timestamp": current_question["timestamp"],
                            "question": current_question["message"],
                            "answer": "Processing...",
                            "status": "processing",
                        }
                    )

            qa_pairs.sort(key=lambda x: x["timestamp"], reverse=True)
            save_user_faq_to_json(user_id, qa_pairs)

        # Serve the user JSON file directly
        with open(user_faq_file, "r", encoding="utf-8") as f:
            user_faq_data = json.load(f)

        # Add metadata
        file_age = time.time() - os.path.getmtime(user_faq_file)
        user_faq_data["file_age_seconds"] = file_age
        user_faq_data["served_from"] = "json_file"
        user_faq_data["file_size_kb"] = round(os.path.getsize(user_faq_file) / 1024, 2)
        user_faq_data["user_id"] = user_id

        response = jsonify(user_faq_data)
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )

        logger.info(
            f"📄 Served user JSON file for {user_id}: {len(user_faq_data.get('questions', []))} questions"
        )
        return response

    except Exception as e:
        logger.error(f"❌ Error serving user FAQ JSON file: {e}")
        error_response = jsonify(
            {
                "status": "error",
                "error": str(e),
                "message": f"Failed to load FAQ JSON file for user {user_id}",
            }
        )
        error_response.headers.add("Access-Control-Allow-Origin", "*")
        return error_response, 500


@onboarding_bps.route("/qa/json/refresh", methods=["POST", "OPTIONS"])
def refresh_faq_json():
    """Force refresh of FAQ JSON files for admin use"""
    # Handle CORS preflight
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )
        response.headers.add("Access-Control-Allow-Methods", "POST,OPTIONS")
        return response

    try:
        # Get fresh FAQ data
        qa_pairs = get_cached_faq_data()

        # Force save to JSON
        success = save_faq_to_json(qa_pairs)

        if success:
            file_size = round(os.path.getsize(ALL_FAQ_JSON) / 1024, 2)
            response = jsonify(
                {
                    "status": "success",
                    "message": "FAQ JSON file refreshed successfully",
                    "questions_count": len(qa_pairs),
                    "file_size_kb": file_size,
                    "json_file": "all_faq_questions.json",
                }
            )
        else:
            response = jsonify(
                {"status": "error", "message": "Failed to refresh FAQ JSON file"}
            )

        response.headers.add("Access-Control-Allow-Origin", "*")
        return response

    except Exception as e:
        logger.error(f"❌ Error refreshing FAQ JSON: {e}")
        error_response = jsonify({"status": "error", "error": str(e)})
        error_response.headers.add("Access-Control-Allow-Origin", "*")
        return error_response, 500
