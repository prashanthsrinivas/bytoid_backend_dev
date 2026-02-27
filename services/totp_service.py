import pyotp

class TOTPService:
    @staticmethod
    def generate_secret():
        return pyotp.random_base32()

    @staticmethod
    def provisioning_uri(secret, user_id, email, issuer="Bytoid"):
        label = f"{email}"
        return pyotp.TOTP(secret).provisioning_uri(
            name=label,
            issuer_name=issuer
        )
    @staticmethod
    def verify_totp(secret, code):
        if not secret or not code:
            return False
        code = str(code).strip()
        if not code.isdigit() or len(code) != 6:
            return False

        totp = pyotp.TOTP(secret)
        return totp.verify(code, valid_window=1)

