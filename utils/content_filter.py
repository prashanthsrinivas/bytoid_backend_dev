"""
Content filtering utility for inappropriate content detection
"""
import re
import logging

logger = logging.getLogger(__name__)

# List of inappropriate keywords and patterns
INAPPROPRIATE_KEYWORDS = [
    # Sexual content
    'sex', 'sexual', 'sexy', 'erotic', 'porn', 'pornography', 'nude', 'naked', 'intercourse',
    'orgasm', 'masturbat', 'arousal', 'arousing', 'seductive', 'seduce', 'intimate', 'intimacy',
    'bedroom', 'foreplay', 'climax', 'penetrat', 'stimulat', 'passion', 'lust', 'horny',
    'sensual', 'sexuality', 'virginity', 'virgin', 'tempting', 'temptation',
    
    # Abuse and violence
    'abuse', 'abusing', 'abused', 'abusive', 'violence', 'violent', 'rape', 'assault',
    'molest', 'harassment', 'harass', 'stalking', 'stalker', 'threat', 'threaten',
    'intimidat', 'bully', 'bullying', 'torture', 'beating', 'hit', 'punch', 'kick',
    'slap', 'choke', 'strangle', 'murder', 'kill', 'death', 'suicide', 'harm',
    'hurt', 'pain', 'suffer', 'victim', 'predator', 'exploit', 'exploitation',
    
    # Hate speech and discrimination
    'hate', 'racist', 'racism', 'discrimination', 'prejudice', 'bigot', 'bigotry',
    'homophob', 'transphob', 'xenophob', 'misogyn', 'sexist', 'stereotype',
    
    # Profanity (common ones)
    'fuck', 'shit', 'damn', 'hell', 'bitch', 'bastard', 'asshole', 'cunt', 'whore',
    'slut', 'piss', 'crap', 'goddamn', 'motherfucker', 'dickhead', 'pussy',
    
    # Drug-related
    'drug', 'cocaine', 'heroin', 'marijuana', 'cannabis', 'methamphetamine', 'ecstasy',
    'addiction', 'overdose', 'dealer', 'trafficking', 'substance abuse',
    
    # Other inappropriate content
    'suicide', 'self-harm', 'cutting', 'depression', 'mental illness', 'eating disorder',
    'anorexia', 'bulimia', 'gambling', 'addiction', 'alcoholism', 'drunk', 'intoxicated'
]

# Patterns for more sophisticated detection
INAPPROPRIATE_PATTERNS = [
    # Sexual patterns
    r'\b(make love|have sex|sleep with|hook up)\b',
    r'\b(sexual (activity|behavior|conduct|assault))\b',
    r'\b(inappropriate touch|unwanted advances)\b',
    
    # Violence patterns
    r'\b(physical (abuse|violence|assault))\b',
    r'\b(domestic violence|child abuse)\b',
    r'\b(want to (die|kill|hurt))\b',
    
    # Self-harm patterns
    r'\b(kill myself|hurt myself|end my life)\b',
    r'\b(suicidal thoughts|self harm)\b',
]

def is_inappropriate_content(text):
    """
    Check if text contains inappropriate content - STRICT filtering
    Blocks: abuse, sexual, violence, profanity, self-harm
    
    Args:
        text (str): Text to analyze
        
    Returns:
        tuple: (is_inappropriate: bool, detected_keywords: list, confidence_score: float)
    """
    if not text or not isinstance(text, str):
        return False, [], 0.0
    
    text_lower = text.lower()
    detected_keywords = []
    
    # Check for inappropriate keywords - STRICT VERSION
    inappropriate_keywords = [
        # Sexual content
        'sex', 'sexual', 'sexy', 'porn', 'nude', 'naked', 'intercourse',
        'orgasm', 'arousal', 'seductive', 'intimate', 'intimacy', 'foreplay',
        'technique', 'relationship', 'lust', 'horny',
        
        # Abuse and violence
        'abuse', 'abusing', 'abused', 'violence', 'violent', 'rape', 'assault',
        'molest', 'harassment', 'stalking', 'threat', 'bully', 'torture',
        'beating', 'hit', 'punch', 'kick', 'slap', 'murder', 'kill',
        'suicide', 'self-harm', 'self harm', 'hurt myself', 'kill myself',
        
        # Hate speech
        'hate', 'racist', 'racism', 'discrimination', 'prejudice',
        'homophob', 'transphob', 'misogyn', 'sexist',
        
        # Profanity
        'fuck', 'shit', 'damn', 'bitch', 'bastard', 'asshole',
        'goddamn', 'motherfucker', 'dickhead',
        
        # Drug-related
        'drug', 'cocaine', 'heroin', 'marijuana', 'cannabis',
        'addiction', 'overdose', 'dealer',
    ]
    
    # Check for inappropriate keywords with word boundaries
    for keyword in inappropriate_keywords:
        pattern = r'\b' + re.escape(keyword.lower()) + r'\b'
        if re.search(pattern, text_lower):
            detected_keywords.append(keyword)
    
    # Calculate confidence score
    total_matches = len(detected_keywords)
    confidence_score = min(total_matches * 0.3, 1.0)
    
    is_inappropriate = total_matches > 0
    
    if is_inappropriate:
        logger.warning(f"Inappropriate content detected: {detected_keywords[:3]}... (confidence: {confidence_score:.2f})")
    
    return is_inappropriate, detected_keywords, confidence_score

def sanitize_content(text):
    """
    Sanitize content by removing or replacing inappropriate parts
    
    Args:
        text (str): Text to sanitize
        
    Returns:
        str: Sanitized text
    """
    if not text or not isinstance(text, str):
        return text
    
    sanitized_text = text
    
    # Replace inappropriate keywords with asterisks
    for keyword in INAPPROPRIATE_KEYWORDS:
        pattern = re.compile(re.escape(keyword), re.IGNORECASE)
        sanitized_text = pattern.sub('*' * len(keyword), sanitized_text)
    
    # Replace inappropriate patterns
    for pattern in INAPPROPRIATE_PATTERNS:
        sanitized_text = re.sub(pattern, '[CONTENT FILTERED]', sanitized_text, flags=re.IGNORECASE)
    
    return sanitized_text

def get_filtered_response():
    """
    Get a standard response for inappropriate content
    
    Returns:
        str: Standard filtered response
    """
    return """I understand you may have questions, but I'm designed to provide helpful information about professional and appropriate topics. 

I can assist you with:
- Business and work-related questions
- Technology and software help
- General knowledge and learning
- Professional communication
- Product information and support

Please feel free to ask about any of these topics, and I'll be happy to help!"""

# Quick validation function for real-time filtering
def quick_content_check(text):
    """
    Quick content check for real-time validation
    
    Args:
        text (str): Text to check
        
    Returns:
        bool: True if content appears appropriate, False otherwise
    """
    if not text or not isinstance(text, str):
        return True
    
    text_lower = text.lower()
    
    # Check for most common inappropriate keywords with word boundaries
    high_priority_keywords = [
        'sex', 'sexual', 'abuse', 'abusing', 'rape', 'assault', 'violence', 'violent',
        'fuck', 'shit', 'bitch', 'suicide', 'kill myself', 'hurt myself'
    ]
    
    import re
    for keyword in high_priority_keywords:
        pattern = r'\b' + re.escape(keyword) + r'\b'
        if re.search(pattern, text_lower):
            return False
    
    return True

# Bytoid-specific keywords and phrases
BYTOID_KEYWORDS = [
    # Core product
    'bytoid', 'byteroid', 'bytedroid', 'byte', 'eva', 'ai assistant',
    
    # Features and components
    'agents hub', 'agent hub', 'ai assistant chat', 'email management', 'unified mailbox',
    'playbook', 'workflow', 'tickets system', 'ticket system', 'contacts crm', 'crm',
    'ai reporting', 'reporting', 'search email', 'session management', 'credits system',
    'webhooks', 'integration', 'google integration', 'microsoft integration', 'facebook integration',
    'gmail', 'outlook', 'automation', 'automate', 'business software', 'platform',
    
    # Functionality terms
    'dashboard', 'onboarding', 'tour', 'setup', 'configuration', 'settings',
    'sync', 'connect', 'api', 'database', 'storage', 'cloud', 's3',
    'user management', 'permissions', 'roles', 'admin', 'administrator',
    'notifications', 'alerts', 'templates', 'custom', 'customize',
    
    # Business terms
    'business', 'enterprise', 'organization', 'team', 'collaboration',
    'productivity', 'efficiency', 'streamline', 'optimize', 'manage',
    'customer', 'client', 'lead', 'sales', 'support', 'service'
]

# Common general questions that should be blocked (math, general knowledge, etc.)
COMMON_GENERAL_QUESTIONS = [
    # Math questions - BLOCK ALL
    'what is', 'how much', 'calculate', 'plus', 'minus', 'multiply', 'divide',
    '1 + 1', '2 + 2', '3 + 3', 'math', 'mathematics', 'equation', 'formula',
    'answer', 'solve', 'problem',
    
    # General knowledge - BLOCK ALL
    'who is', 'who was', 'when did', 'where is', 'why do', 
    'capital of', 'president', 'history', 'geography', 'science', 'physics',
    'chemistry', 'biology', 'weather', 'temperature', 'time', 'date',
    
    # General tech - BLOCK
    'artificial intelligence', 'machine learning', 'programming', 'coding',
    'python', 'javascript', 'html', 'css', 'database', 'sql',
    
    # Greetings only - these are allowed sometimes
    # 'hello', 'hi', 'hey' - NOT blocking these as they might precede real questions
]

def is_bytoid_related_question(text):
    """
    Check if the question is related to Bytoid features and functionality
    
    Args:
        text (str): Question text to analyze
        
    Returns:
        tuple: (is_bytoid_related: bool, confidence_score: float, detected_keywords: list)
    """
    if not text or not isinstance(text, str):
        return False, 0.0, []
    
    text_lower = text.lower()
    detected_keywords = []
    
    # Check for Bytoid-specific keywords
    bytoid_matches = 0
    for keyword in BYTOID_KEYWORDS:
        if keyword.lower() in text_lower:
            detected_keywords.append(keyword)
            bytoid_matches += 1
    
    # Check for common general questions (negative indicators)
    general_matches = 0
    for common_phrase in COMMON_GENERAL_QUESTIONS:
        if common_phrase.lower() in text_lower:
            general_matches += 1
    
    # Calculate confidence score
    if bytoid_matches > 0:
        confidence_score = min(bytoid_matches * 0.3, 1.0)  # Higher score for more keywords
        confidence_score -= general_matches * 0.15  # Reduce for general terms (but not heavily)
        confidence_score = max(confidence_score, 0.0)  # Don't go below 0
    else:
        confidence_score = 0.0
    
    # BALANCED Decision logic - Accept Bytoid questions but reject bad ones
    is_bytoid_related = (
        bytoid_matches > 0 and  # MUST have at least one Bytoid keyword
        confidence_score >= 0.15 and  # Lower threshold (was 0.3, more lenient)
        general_matches <= 1  # Allow up to 1 general term (was 0)
    )
    
    return is_bytoid_related, confidence_score, detected_keywords

def should_allow_question(text):
    """
    Comprehensive check if a question should be allowed in the FAQ system
    
    Args:
        text (str): Question text to check
        
    Returns:
        tuple: (should_allow: bool, reason: str, details: dict)
    """
    if not text or not isinstance(text, str):
        return False, "Empty or invalid question", {}
    
    # First check for inappropriate content
    is_inappropriate, inappropriate_keywords, inappropriate_confidence = is_inappropriate_content(text)
    if is_inappropriate:
        return False, "Inappropriate content detected", {
            "type": "inappropriate",
            "keywords": inappropriate_keywords[:3],
            "confidence": inappropriate_confidence
        }
    
    # Then check if it's Bytoid-related
    is_bytoid, bytoid_confidence, bytoid_keywords = is_bytoid_related_question(text)
    if not is_bytoid:
        return False, "Question not related to Bytoid", {
            "type": "off_topic",
            "bytoid_confidence": bytoid_confidence,
            "bytoid_keywords": bytoid_keywords
        }
    
    return True, "Question approved", {
        "type": "approved",
        "bytoid_confidence": bytoid_confidence,
        "bytoid_keywords": bytoid_keywords[:5]  # Top 5 keywords
    }

def get_bytoid_focused_response():
    """
    Get a standard response for non-Bytoid questions
    
    Returns:
        str: Standard response directing users to Bytoid topics
    """
    return """I'm Eva, Bytoid's AI assistant, and I specialize in helping with Bytoid platform features and functionality.

I can assist you with:
• Agents Hub - AI agent management and automation
• Email Management - Gmail, Outlook integration and unified mailbox
• Playbook Workflows - Custom business process automation
• Tickets System - Customer support and issue tracking
• Contacts CRM - Customer relationship management
• AI Reporting - Dynamic reports and analytics
• Search Email - Advanced email search and filtering
• Integrations - Google, Microsoft, Facebook connections
• Webhooks - Custom app integrations
• Credits System - Usage and billing management

Please ask me about any Bytoid features, setup, configuration, or how to use specific components of the platform!"""


def is_question_bytoid_related_ai(text: str) -> bool:
    """
    Filter for Bytoid-related questions.
    Rejects inappropriate, rude, argumentative, or non-Bytoid questions.
    """
    if not text or len(text.strip()) < 3:
        return False
    
    text_lower = text.lower().strip()
    
    # REJECT non-English questions (has non-ASCII characters)
    if any(ord(c) > 127 for c in text):
        logger.debug(f"🚫 Non-English rejected: {text[:40]}")
        return False
    
    # REJECT: Inappropriate content
    inappropriate_patterns = [
        # Profanity and offensive language
        'fuck', 'shit', 'bitch', 'asshole', 'asshole', 'damn', 'hell',
        # Sexual content
        'sex', 'sexual', 'porn', 'nude', 'rape', 'assault',
        # Violence and harm
        'suicide', 'kill', 'murder', 'abuse', 'abuse', 'evil',
        # Rude/argumentative/dismissive tone
        r"don't.*care", r"i don't.*care", r"stop.*ask.*me", r"stop asking",
        r"you're.*wrong", r"wrong information", r"telling.*wrong",
        r"not.*interested.*asking", r"not interested", r"not correct",
        r"i guess.*accent", r"don't really understand",  # Sarcastic/dismissive
    ]
    
    import re
    for pattern in inappropriate_patterns:
        if re.search(pattern, text_lower):
            logger.debug(f"🚫 Inappropriate rejected: {pattern} - '{text[:50]}'")
            return False
    
    # ACCEPT: Help-related questions about Bytoid
    # Questions should be asking for help/information
    help_indicators = [
        'how', 'what', 'where', 'when', 'why', 'can you', 'could you',
        'tell me', 'explain', 'help', 'assist', 'feature', 'integrate',
        'setup', 'configure', 'guide', 'tutorial', 'information',
    ]
    
    is_help_question = any(indicator in text_lower for indicator in help_indicators)
    
    if not is_help_question:
        logger.debug(f"🚫 Not a help question - rejected: '{text[:50]}'")
        return False
    
    logger.debug(f"✅ Accepted: {text[:50]}")
    return True