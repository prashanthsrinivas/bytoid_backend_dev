from flask import Blueprint, request, jsonify,session
import asyncio
from microsoft_route.routes import microsoft_list_drafts
from gmail_route.routes import list_drafts


unified_bp = Blueprint('unified', __name__)


@unified_bp.route('/unified_drafts')
def unified_drafts():
    emails = asyncio.run(get_all_drafts())
    return jsonify(emails)


async def get_all_drafts():

    gmail_task = list_drafts()
    outlook_task = microsoft_list_drafts()
    gmail_emails, outlook_emails = await asyncio.gather(gmail_task, outlook_task)
    return {
        'gmail': gmail_emails,
        'outlook': outlook_emails
    }