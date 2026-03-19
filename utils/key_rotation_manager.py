import os
import base64
import boto3
from datetime import datetime, timedelta, timezone
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecureKMSService:

    def __init__(self, region="ca-central-1", kms_master_key="alias/bytoid-aes256-key"):

        # initialize KMS client
        self.kms = boto3.client("kms", region_name=region)

        # master key alias
        self.master_key = kms_master_key

        # store user keys
        self.user_keys = {}

    # ---------------------------------------------------
    # Generate data key for user
    # ---------------------------------------------------
    def generate_user_key(self, user_id):

        response = self.kms.generate_data_key(KeyId=self.master_key, KeySpec="AES_256")

        plaintext_key = response["Plaintext"]
        encrypted_key = response["CiphertextBlob"]

        self.user_keys[user_id] = {
            "encrypted_key": encrypted_key,
            "last_rotation": datetime.now(timezone.utc),
        }

        print(f"[USER KEY GENERATED] user={user_id}")

        return plaintext_key, encrypted_key

    # ---------------------------------------------------
    # Get user key
    # ---------------------------------------------------
    def get_user_key(self, user_id):

        if user_id not in self.user_keys:
            return self.generate_user_key(user_id)

        encrypted_key = self.user_keys[user_id]["encrypted_key"]

        response = self.kms.decrypt(CiphertextBlob=encrypted_key)

        plaintext_key = response["Plaintext"]

        return plaintext_key, encrypted_key

    # ---------------------------------------------------
    # Check rotation
    # ---------------------------------------------------
    def needs_rotation(self, user_id):

        last_rotation = self.user_keys[user_id]["last_rotation"]

        next_rotation = last_rotation + timedelta(days=180)

        if datetime.now(timezone.utc) > next_rotation:
            return True

        return False

    # ---------------------------------------------------
    # Rotate key for one user
    # ---------------------------------------------------
    def rotate_user_key(self, user_id, admin=False):

        if not admin:
            raise PermissionError("Only admin can rotate keys")

        plaintext_key, encrypted_key = self.generate_user_key(user_id)

        self.user_keys[user_id]["encrypted_key"] = encrypted_key
        self.user_keys[user_id]["last_rotation"] = datetime.now(timezone.utc)

        print(f"[KEY ROTATED] user={user_id}")

        return {
            "user_id": user_id,
            "encrypted_key": base64.b64encode(encrypted_key).decode(),
            "last_rotation": self.user_keys[user_id]["last_rotation"].isoformat(),
        }

    # ---------------------------------------------------
    # Rotate all users
    # ---------------------------------------------------
    def rotate_all_keys(self, admin=False):

        if not admin:
            raise PermissionError("Only admin can rotate keys")
        results = {}

        for user_id in self.user_keys:
            rotated = self.rotate_user_key(user_id, admin=True)
            results[user_id] = rotated

        print("[ALL KEYS ROTATED]")

    # ---------------------------------------------------
    # Admin view all user keys (encrypted format only)
    # ---------------------------------------------------
    def admin_view_all_keys(self, admin=False):

        if not admin:
            raise PermissionError("Only admin can view keys")
        keys = {}
        for user_id, data in self.user_keys.items():
            keys[user_id] = {
                "encrypted_key": base64.b64encode(data["encrypted_key"]).decode(),
                "last_rotation": data["last_rotation"].isoformat(),
            }
        return keys

    # ---------------------------------------------------
    # Encryption
    # ---------------------------------------------------
    def encrypt(self, user_id, plaintext):

        if not user_id:
            raise ValueError("user_id required")

        plaintext_key, encrypted_key = self.get_user_key(user_id)

        aesgcm = AESGCM(plaintext_key)

        iv = os.urandom(12)

        aad = str(user_id).encode()

        ciphertext = aesgcm.encrypt(iv, plaintext.encode(), aad)

        return {
            "user_id": user_id,
            "ciphertext": base64.b64encode(ciphertext).decode(),
            "iv": base64.b64encode(iv).decode(),
            "encrypted_key": base64.b64encode(encrypted_key).decode(),
        }

    # ---------------------------------------------------
    # Decryption
    # ---------------------------------------------------
    def decrypt(self, user_id, encrypted_key, iv, ciphertext):

        if not user_id:
            raise ValueError("user_id required")

        encrypted_key = base64.b64decode(encrypted_key)
        iv = base64.b64decode(iv)
        ciphertext = base64.b64decode(ciphertext)

        response = self.kms.decrypt(CiphertextBlob=encrypted_key)

        data_key = response["Plaintext"]

        aesgcm = AESGCM(data_key)

        aad = str(user_id).encode()

        plaintext = aesgcm.decrypt(iv, ciphertext, aad)

        return plaintext.decode()
