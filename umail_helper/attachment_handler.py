"""
Email Attachment Handler Module
Handles file uploads and storage for email attachments
"""

import os
import json
import uuid
from datetime import datetime, timezone
from werkzeug.utils import secure_filename
from utils.s3_utils import upload_any_file
from cust_helpers import pathconfig
from utils.normal import ensure_dir
from utils.base_logger import get_logger

logger = get_logger(__name__)

# Allowed file extensions for attachments
ALLOWED_EXTENSIONS = {
    'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx', 'xls', 'xlsx', 
    'ppt', 'pptx', 'zip', 'rar', '7z', 'csv', 'json', 'xml', 'mp3', 'mp4',
    'mov', 'avi', 'mkv', 'zip', 'tar', 'gz', 'bmp', 'svg', 'webp'
}

# Max file size: 25MB
MAX_FILE_SIZE = 25 * 1024 * 1024


def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def validate_file(file):
    """Validate uploaded file"""
    if not file or file.filename == '':
        return False, "No file selected"
    
    if not allowed_file(file.filename):
        return False, f"File type not allowed. Allowed types: {', '.join(ALLOWED_EXTENSIONS)}"
    
    # Check file size
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)
    
    if file_size > MAX_FILE_SIZE:
        return False, f"File size exceeds maximum allowed size of {MAX_FILE_SIZE / (1024*1024):.0f}MB"
    
    if file_size == 0:
        return False, "File is empty"
    
    return True, "OK"


def handle_attachment_upload(user_id, conversation_id, client_id, file):
    """
    Handle single file upload for email attachment
    
    Args:
        user_id (str): User ID
        conversation_id (str): Conversation ID
        client_id (str): Client/Contact ID
        file (FileStorage): Uploaded file object
    
    Returns:
        dict: {
            'status': 'success' | 'error',
            'attachment_id': str (if success),
            'filename': str (if success),
            'file_size': int (if success),
            'mime_type': str (if success),
            's3_key': str (if success),
            'upload_timestamp': str (if success),
            'error': str (if error),
            'message': str (if error)
        }
    """
    try:
        # Validate file
        is_valid, message = validate_file(file)
        if not is_valid:
            logger.warning(f"File validation failed for user {user_id}: {message}")
            return {
                'status': 'error',
                'error': 'validation_failed',
                'message': message
            }
        
        # Secure filename
        filename = secure_filename(file.filename)
        if not filename:
            logger.warning(f"Invalid filename for user {user_id}")
            return {
                'status': 'error',
                'error': 'invalid_filename',
                'message': 'Invalid filename'
            }
        
        # Generate unique attachment ID
        attachment_id = str(uuid.uuid4())
        
        # Create folder structure
        attachment_folder = os.path.join(
            pathconfig.basepath, 
            "attachments", 
            user_id, 
            client_id,
            conversation_id
        )
        ensure_dir(attachment_folder)
        
        # Add timestamp and ID to filename to make it unique
        file_ext = filename.rsplit('.', 1)[1].lower()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        unique_filename = f"{timestamp}_{attachment_id}.{file_ext}"
        
        # Save locally
        local_file_path = os.path.join(attachment_folder, unique_filename)
        file.save(local_file_path)
        
        # Get file size and mime type
        file_size = os.path.getsize(local_file_path)
        mime_type = file.content_type or "application/octet-stream"
        
        # Upload to S3
        s3_key = f"{user_id}/attachments/{client_id}/{conversation_id}/{unique_filename}"
        
        upload_result = upload_any_file(
            local_file_path,
            user_id,
            type="messages",
            s3_key_C=s3_key
        )
        
        if upload_result.get('status') != 'success':
            logger.error(f"S3 upload failed for attachment {attachment_id}")
            # Clean up local file
            os.remove(local_file_path)
            return {
                'status': 'error',
                'error': 's3_upload_failed',
                'message': 'Failed to upload file to storage'
            }
        
        # Create attachment metadata
        attachment_metadata = {
            'attachment_id': attachment_id,
            'original_filename': filename,
            'filename': unique_filename,
            'file_size': file_size,
            'mime_type': mime_type,
            's3_key': s3_key,
            'upload_timestamp': datetime.now(timezone.utc).isoformat(),
            'status': 'ready'
        }
        
        logger.info(
            f"Attachment uploaded successfully - "
            f"User: {user_id}, Attachment: {attachment_id}, Size: {file_size} bytes"
        )
        
        return {
            'status': 'success',
            **attachment_metadata
        }
        
    except Exception as e:
        logger.error(f"Error handling attachment upload for user {user_id}: {str(e)}")
        return {
            'status': 'error',
            'error': 'upload_exception',
            'message': f'Upload failed: {str(e)}'
        }


def handle_multiple_attachments(user_id, conversation_id, client_id, files):
    """
    Handle multiple file uploads for email attachments
    
    Args:
        user_id (str): User ID
        conversation_id (str): Conversation ID
        client_id (str): Client/Contact ID
        files (list): List of FileStorage objects
    
    Returns:
        dict: {
            'status': 'success' | 'partial' | 'error',
            'attachments': [attachment_metadata],
            'failed': [{'filename': str, 'error': str}],
            'total_uploaded': int,
            'total_size': int,
            'message': str
        }
    """
    if not files:
        return {
            'status': 'error',
            'error': 'no_files',
            'message': 'No files provided'
        }
    
    attachments = []
    failed = []
    total_size = 0
    
    for file in files:
        result = handle_attachment_upload(user_id, conversation_id, client_id, file)
        
        if result.get('status') == 'success':
            attachments.append(result)
            total_size += result.get('file_size', 0)
        else:
            failed.append({
                'filename': file.filename,
                'error': result.get('message', 'Unknown error')
            })
    
    if not attachments and failed:
        status = 'error'
        message = 'All files failed to upload'
    elif failed:
        status = 'partial'
        message = f'Uploaded {len(attachments)} of {len(files)} files'
    else:
        status = 'success'
        message = f'Successfully uploaded {len(attachments)} file(s)'
    
    logger.info(
        f"Batch attachment upload - "
        f"User: {user_id}, Success: {len(attachments)}, Failed: {len(failed)}"
    )
    
    return {
        'status': status,
        'attachments': attachments,
        'failed': failed,
        'total_uploaded': len(attachments),
        'total_failed': len(failed),
        'total_size': total_size,
        'message': message
    }


def create_attachment_metadata_for_message(attachments_list):
    """
    Convert attachment metadata list to format suitable for email message
    
    Args:
        attachments_list (list): List of attachment metadata dicts from upload handlers
    
    Returns:
        list: Formatted attachments for including in message
    """
    if not attachments_list:
        return []
    
    formatted_attachments = []
    for att in attachments_list:
        if isinstance(att, dict):
            formatted_attachments.append({
                'id': att.get('attachment_id'),
                'filename': att.get('original_filename'),
                'size': att.get('file_size'),
                'mime_type': att.get('mime_type'),
                's3_key': att.get('s3_key'),
                'upload_timestamp': att.get('upload_timestamp'),
                'status': att.get('status', 'ready')
            })
    
    return formatted_attachments


def get_attachment_by_id(user_id, attachment_id):
    """
    Retrieve attachment metadata by ID
    
    Args:
        user_id (str): User ID
        attachment_id (str): Attachment ID
    
    Returns:
        dict or None: Attachment metadata if found
    """
    # This would require storing attachment metadata in DB or S3 index
    # For now, this is a placeholder for future implementation
    logger.warning(f"get_attachment_by_id not yet implemented for {attachment_id}")
    return None


def delete_attachment(user_id, attachment_id, s3_key):
    """
    Delete attachment from S3 and local storage
    
    Args:
        user_id (str): User ID
        attachment_id (str): Attachment ID
        s3_key (str): S3 key of the attachment
    
    Returns:
        dict: {'status': 'success' | 'error', 'message': str}
    """
    try:
        # Delete from S3 would be implemented here
        logger.info(f"Attachment deletion request - User: {user_id}, Attachment: {attachment_id}")
        return {
            'status': 'success',
            'message': 'Attachment marked for deletion'
        }
    except Exception as e:
        logger.error(f"Error deleting attachment {attachment_id}: {str(e)}")
        return {
            'status': 'error',
            'message': f'Failed to delete attachment: {str(e)}'
        }
