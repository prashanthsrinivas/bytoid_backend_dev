import os
import requests
from dotenv import load_dotenv

load_dotenv()

FACEBOOK_CLIENT_ID = os.getenv('FACEBOOK_CLIENT_ID')
FACEBOOK_CLIENT_SECRET = os.getenv('FACEBOOK_CLIENT_SECRET')
FACEBOOK_REDIRECT_URI = os.getenv('FACEBOOK_REDIRECT_URI')
FACEBOOK_CONFIG_ID = '1147527330391577'

class FacebookOAuthHandler:
    def __init__(self):
        self.client_id = FACEBOOK_CLIENT_ID
        self.client_secret = FACEBOOK_CLIENT_SECRET
        self.redirect_uri = FACEBOOK_REDIRECT_URI
    def exchange_code_for_token(self, code):
        token_url = 'https://graph.facebook.com/v13.0/oauth/access_token'
        payload = {
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'client_secret': self.client_secret,
            'code': code
        }
        response = requests.get(token_url, params=payload)
        return response.json()

    def get_user_info(self, access_token):
        user_info_url = f'https://graph.facebook.com/me?fields=id,name,email,accounts&access_token={access_token}'
        response = requests.get(user_info_url)
        return response.json()

