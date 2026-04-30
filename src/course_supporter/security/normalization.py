"""Unicode and filename normalization helpers (vision §KD14, KD-F).

Two-pass normalization design:

* :func:`nfc_for_storage` -- canonical-composed NFC form. Used for
  the version we keep downstream (LLM input, persisted text).
  Preserves visual identity for human readers across platforms.

* :func:`nfkc_for_security` -- compatibility-composed NFKC form.
  Used ONLY for security checks (regex pre-screen, unicode safety
  check, filename whitelist resolution). Discarded after the
  check; never stored. NFKC collapses compatibility variants
  (full-width / circled / superscript Latin, presentation forms)
  to ASCII so obfuscated injection attempts converge to the same
  canonical form as plain ASCII payloads.

* :func:`normalize_filename` -- NFKC over filename, then unchanged
  for whitelist lookup. Filename rendering attacks like
  ``homework‮.txt`` (RTL override) become deterministic
  strings before whitelist comparison; the ``‮`` codepoint
  survives NFKC -- detection is the unicode_check module's job.

Two distinct callables (rather than ``normalize(text, *, form=...)``)
force every callsite to declare its intent explicitly. Past sprints
saw silent NFC/NFKC mix-ups when the form was a parameter; an
explicit verb in the function name removes that failure mode.
"""

from __future__ import annotations

import unicodedata


def nfc_for_storage(text: str) -> str:
    """Return NFC-normalized text for downstream storage and LLM input.

    NFC is the W3C-recommended on-the-wire form. Composition is
    canonical -- visually identical strings produce identical byte
    sequences -- without the lossy compatibility folding of NFKC.
    """
    return unicodedata.normalize("NFC", text)


def nfkc_for_security(text: str) -> str:
    """Return NFKC-normalized text for security pre-screen ONLY.

    NFKC collapses compatibility variants (full-width / circled /
    superscript / presentation forms) to canonical bases. Use
    exclusively for security checks: regex pattern matching,
    unicode hard-reject, filename whitelist resolution. NEVER
    persist the NFKC result -- it is lossy with respect to the
    original visual form and would corrupt content semantics.
    """
    return unicodedata.normalize("NFKC", text)


def normalize_filename(filename: str) -> str:
    """Normalize a filename for whitelist comparison.

    Applies NFKC to defuse rendering attacks (compatibility-encoded
    Latin / Cyrillic look-alikes, full-width characters used to
    bypass extension whitelists). The whitelist comparison then
    operates on a deterministic canonical string.

    Suspicious characters that survive NFKC (e.g. RTL override
    U+202E) are NOT stripped here -- detection is the responsibility
    of :func:`course_supporter.security.unicode_check.check_text_unicode_safety`
    invoked on the post-NFKC filename. The original filename should
    still be surfaced verbatim in user-facing error messages.
    """
    return unicodedata.normalize("NFKC", filename)
