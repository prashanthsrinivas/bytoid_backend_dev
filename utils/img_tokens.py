from PIL import Image
import base64
import io, math


PATCH_SIZE = 16  # industry-safe default
MIN_IMAGE_TOKENS = 300
MAX_IMAGE_TOKENS = 2000
MAX_PIXELS = 4096 * 4096  # hard safety limit


def get_image_resolution(data_url):
    """
    Extract image resolution from inline base64 data URL
    """
    if not data_url.startswith("data:image/"):
        raise ValueError("Only inline base64 images are supported")

    _, encoded = data_url.split(",", 1)
    image_bytes = base64.b64decode(encoded)

    with Image.open(io.BytesIO(image_bytes)) as img:
        width, height = img.width, img.height

    if width * height > MAX_PIXELS:
        raise ValueError("Image resolution too large")

    return width, height


def image_tokens_from_patches(
    width,
    height,
    patch_size=PATCH_SIZE,
):
    """
    image_tokens ≈ ceil(H/P) × ceil(W/P)
    """
    patches_w = math.ceil(width / patch_size)
    patches_h = math.ceil(height / patch_size)
    return patches_w * patches_h


def normalized_image_tokens(width: int, height: int) -> int:
    raw_tokens = image_tokens_from_patches(width, height)

    # normalize + cap
    return max(
        MIN_IMAGE_TOKENS,
        min(raw_tokens, MAX_IMAGE_TOKENS),
    )


def image_credit_cost(data_url: str) -> int:
    width, height = get_image_resolution(data_url)
    return normalized_image_tokens(width, height)
