"""
Clipboard handling for Signal TUI

Provides functionality to grab images from the clipboard
and stage them as attachments for sending.
"""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Try to import Pillow for clipboard image support
try:
    from PIL import Image, ImageGrab
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


@dataclass
class StagedAttachment:
    """Represents a staged attachment ready to be sent."""
    path: str
    filename: str
    size: int  # in bytes
    width: int = 0
    height: int = 0
    preview: str = ""  # ASCII art preview

    @property
    def size_human(self) -> str:
        """Return human-readable file size."""
        if self.size < 1024:
            return f"{self.size} B"
        elif self.size < 1024 * 1024:
            return f"{self.size / 1024:.1f} KB"
        else:
            return f"{self.size / (1024 * 1024):.1f} MB"

    @property
    def dimensions(self) -> str:
        """Return image dimensions string."""
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return ""


def generate_image_preview(image: "Image.Image", width: int = 40, height: int = 8) -> str:
    """
    Generate a small ASCII art preview of an image using shade characters.

    Args:
        image: PIL Image to preview
        width: Target width in characters
        height: Target height in characters

    Returns:
        String containing the ASCII art preview
    """
    if not HAS_PILLOW:
        return ""

    try:
        # Convert to RGB if necessary
        if image.mode != 'RGB':
            image = image.convert('RGB')

        # Resize to target dimensions
        thumb = image.resize((width, height), Image.Resampling.LANCZOS)
        pixels = thumb.load()

        # Characters from brightest to darkest (inverted for dark terminal backgrounds)
        # This makes dark images more visible on dark terminals
        chars = "█▓▒░ "

        lines = []
        for y in range(height):
            line = ""
            for x in range(width):
                r, g, b = pixels[x, y]
                # Convert to grayscale (0-255)
                gray = int(0.299 * r + 0.587 * g + 0.114 * b)
                # Map to character index (0-4)
                idx = min(len(chars) - 1, gray * len(chars) // 256)
                line += chars[idx]
            lines.append(line)

        return "\n".join(lines)
    except Exception:
        return ""


def is_clipboard_supported() -> bool:
    """Check if clipboard image grabbing is supported."""
    return HAS_PILLOW


def grab_clipboard_image() -> Optional[StagedAttachment]:
    """
    Grab an image from the system clipboard.

    Returns:
        StagedAttachment if an image was found, None otherwise.
    """
    if not HAS_PILLOW:
        return None

    try:
        # Try to grab image from clipboard
        image = ImageGrab.grabclipboard()

        if image is None:
            return None

        # Handle case where clipboard contains file paths (macOS/Windows)
        if isinstance(image, list):
            # List of file paths - check if first is an image
            if image and os.path.isfile(image[0]):
                file_path = image[0]
                # Check if it's an image file
                try:
                    with Image.open(file_path) as img:
                        # It's a valid image file, use it directly
                        filename = os.path.basename(file_path)
                        size = os.path.getsize(file_path)
                        return StagedAttachment(
                            path=file_path,
                            filename=filename,
                            size=size
                        )
                except Exception:
                    return None
            return None

        # It's an actual image in the clipboard
        if isinstance(image, Image.Image):
            # Save to temp file
            temp_dir = tempfile.gettempdir()
            temp_path = os.path.join(temp_dir, "signal_tui_clipboard.png")

            # Get dimensions before any conversion
            img_width, img_height = image.size

            # Generate preview before saving
            preview = generate_image_preview(image)

            # Convert to RGB if necessary (for RGBA images)
            if image.mode == 'RGBA':
                # Keep as PNG to preserve transparency
                image.save(temp_path, 'PNG')
            else:
                image.save(temp_path, 'PNG')

            size = os.path.getsize(temp_path)

            return StagedAttachment(
                path=temp_path,
                filename="clipboard_image.png",
                size=size,
                width=img_width,
                height=img_height,
                preview=preview
            )

        return None

    except Exception:
        # Clipboard access can fail for various reasons
        return None


def cleanup_temp_attachment(attachment: StagedAttachment) -> None:
    """
    Clean up a temporary attachment file if it's in the temp directory.

    Args:
        attachment: The attachment to clean up.
    """
    if attachment is None:
        return

    try:
        temp_dir = tempfile.gettempdir()
        if attachment.path.startswith(temp_dir):
            if os.path.exists(attachment.path):
                os.remove(attachment.path)
    except Exception:
        # Ignore cleanup errors
        pass
