from typing import Union

def verify_domain(email: str) -> bool:
    """
    Verify if the domain part of an email address is valid.
    
    Args:
        email (str): Email address to verify
        
    Returns:
        bool: True if domain is valid, False otherwise
    """
    try:
        # Extract domain part from email
        domain = email.split('@')[1]
        
        # Try to resolve the domain using DNS lookup
        socket.gethostbyname(domain)
        
        return True
    except (IndexError, socket.gaierror):
        # IndexError: Invalid email format (no @ symbol)
        # socket.gaierror: Domain does not exist
        return False
