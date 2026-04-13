from cryptography.fernet import Fernet, InvalidToken

from net_grading.config import get_settings


def _cipher() -> Fernet:
    return Fernet(get_settings().site2_enc_key.encode())


def encrypt(plaintext: str) -> bytes:
    return _cipher().encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes) -> str:
    try:
        return _cipher().decrypt(ciphertext).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("invalid ciphertext") from exc
