"""How a name is compared. Standard library only, so any module can import it at its top."""

import re
import unicodedata


def name_key(text, *, split_apostrophes: bool = False) -> str:
    """A name as compared: case, accents and punctuation ignored, a hyphen the same as
    a space — 'Térraba-Sierpe', 'terraba sierpe' and 'TERRABA SIERPE.' are one key.
    An apostrophe is dropped ("Cox's" -> 'coxs') or, with split_apostrophes, a space
    ('cox s')."""
    t = unicodedata.normalize("NFKD", str(text if text is not None else ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).casefold()
    if not split_apostrophes:
        t = re.sub(r"['’`]", "", t)
    return " ".join(re.sub(r"[\W_]+", " ", t).split())
