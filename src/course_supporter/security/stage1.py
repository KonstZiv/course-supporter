"""Stage 1 synchronous orchestrator (vision §KD14).

Single entry point for the security layer's pre-LLM gate. Composes
the per-concern modules (size cap, magic detection, whitelist,
archive extraction, charset, unicode hard-reject, regex pre-screen)
behind one call:

    >>> result = run_stage1(filename="hw.txt", content=b"...", context="homework")

The orchestrator is **synchronous and pre-ESC**: every rejection
raises before any LLM is invoked, so a malformed upload never
incurs a tenant-billed token. Stage 2 (LLM safety check, commit i)
runs only when ``run_stage1`` returns successfully.

## Pipeline order (cheap-fail-fast)

1. **Extension extraction** -- NFKC-normalized, lowest-suffix only.
2. **Whitelist** -- ``policy.allowed_extensions`` membership.
3. **Size cap** -- ``len(content)`` against the extension-specific
   limit (video override applies inside ``get_max_size_for_extension``).
4. **Magic / extension match** -- ``verify_extension_matches_content``
   (also rejects empty content).
5. **Archive recursion** -- if extension dispatches to an archive
   kind, drain the iterator eagerly (all-or-nothing); each text
   entry inside the archive is independently routed through the
   text-content checks.
6. **Text content checks** -- charset (when strict), three-tier
   decode, NFKC, unicode hard-reject, regex pre-screen.
7. **Build result** -- NFC text for storage on text inputs.

## Acceptance trade-off (vision-blocking)

Stage 2 retry is **not** modeled here. The 0.5 ``StageRouter``
contract is sealed; structural retry belongs to a deferred Phase 1+
extension. Per the 0.6 acceptance, terminal :class:`SafetyValidationError`
replaces the original retry hook -- see
:class:`course_supporter.security.exceptions.SafetyValidationError`.

## Defensive invariant

The orchestrator raises ``FORBIDDEN_TYPE`` if a policy whitelisted
an archive extension but failed to configure
``max_archive_unzipped_bytes`` / ``max_archive_nesting_depth``.
Every shipped policy is consistent (regression-tested in
:class:`tests.unit.security.test_policies.TestPolicyConsistency`),
so this branch is unreachable under normal config. It exists to
short-circuit a runtime KeyError that would otherwise reach the
caller as a generic 500.

## Logging

Each rejection emits a single ``stage1.rejected`` WARNING via
structlog with ``category``, ``filename``, ``context``, and
``detail`` keys. The exception is re-raised after logging; callers
in the HTTP layer translate it to a 400 response. Successful
validation is intentionally silent at this layer -- callers
(Stage 2 / ingestion) emit their own success records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

import structlog

from course_supporter.security.archive import (
    ClassifiedEntry,
    EntryVerdict,
    ExtractedFile,
    SkipMatcher,
    extract_archive_safely,
)
from course_supporter.security.exceptions import (
    ErrorCategory,
    SecurityRejectedError,
)
from course_supporter.security.file_type import (
    detect_charset,
    detect_mime_type,
    extension_of,
    verify_extension_matches_content,
)
from course_supporter.security.normalization import (
    nfc_for_storage,
    nfkc_for_security,
    normalize_filename,
)
from course_supporter.security.policies import (
    _PROSE,
    CODE_EXTENSIONS,
    ContextPolicy,
    get_max_size_for_extension,
    policy_for,
)
from course_supporter.security.regex_patterns import match_text
from course_supporter.security.schemas import NotOpenedEntry
from course_supporter.security.unicode_check import check_text_unicode_safety

# Extensions decoded and run through the text content pipeline (charset,
# unicode hard-reject, prompt-injection pre-screen).
#
# DERIVED, not listed. The 0.6 hand-written set ({txt, md, html, py, ipynb})
# was the third, quietest member of the drift family DD-19-B names: it decides
# not admission but whether an admitted file is SCREENED, so its incompleteness
# failed open -- a .json / .yaml / .ts payload reached the model past the
# unicode reject and past the injection pre-screen, silently. Deriving it from
# the same CODE_EXTENSIONS the policy admits closes that by construction:
# a format the student may send is a format that gets screened.
#
# The union is monotonic in the same one-directional sense as
# ``normalizer/classify.py``'s ``_TEXT_EXTS``: widening CODE_EXTENSIONS widens
# screening; nothing here narrows it. Locked by
# ``TestTextExtensionsDerived`` (empty set difference), which guards the
# CONSTRUCTION rather than the values.
_TEXT_EXTENSIONS: frozenset[str] = _PROSE | CODE_EXTENSIONS

# Charsets accepted when policy.enable_charset_strict is True. The
# strict gate is the homework baseline -- modern submissions ship
# UTF-8; legacy encodings (Windows-1251, KOI8-R, ISO-8859-*) reach
# CHARSET_VIOLATION. ASCII is a UTF-8 subset; libmagic reports it
# as ``us-ascii`` so both spellings are honored.
_STRICT_ALLOWED_CHARSETS: frozenset[str] = frozenset({"utf-8", "us-ascii", "ascii"})


logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Stage1Result:
    """Successful Stage 1 outcome handed to Stage 2 / ingestion.

    Built only when every check passes; rejection is communicated
    via :class:`SecurityRejectedError`. Therefore this class never
    represents partial state -- every field is meaningful.

    Attributes:
        filename: NFC-normalized filename safe for downstream
            storage. The original (pre-NFC) filename is not
            preserved on the result; logs/error messages use the
            input verbatim.
        extension: Lower-cased extension without leading dot, as
            extracted via :func:`extension_of`. Available for
            policy-aware downstream branching without re-parsing.
        detected_mime: libmagic MIME for the input bytes. Echoed
            from :func:`detect_mime_type`; matches the extension
            family per :func:`verify_extension_matches_content`.
        detected_charset: libmagic charset label for text inputs;
            ``None`` for binary content, empty input, or archive
            inputs (where charset is meaningful only per entry,
            not at the archive level).
        nfc_text: NFC-normalized text body suitable for storage
            and downstream LLM ingestion. Set only for non-archive
            inputs whose extension is in :data:`_TEXT_EXTENSIONS`;
            ``None`` for binary or archive inputs.
        archive_entries: The archive members that were READ, in
            archive-iteration order. ``None`` for non-archive inputs.
            A strict context yields :class:`ExtractedFile`; a context
            with ``archive_soft_exclude`` yields
            :class:`ClassifiedEntry`. In both cases only members that
            passed every check are here, so a caller may decode the
            content of anything in this tuple without filtering
            further. What was set aside is in :attr:`not_opened`.
        not_opened: Members the checker did not read, each with its
            reason. Always empty in a strict context (there an
            unreadable member raises instead) and for non-archive
            inputs.
        context: Active context discriminator -- echoed for
            downstream callers that compose Stage 1 with later
            stages and want a single object to pass through.
    """

    filename: str
    extension: str
    detected_mime: str
    detected_charset: str | None
    nfc_text: str | None
    archive_entries: tuple[ExtractedFile | ClassifiedEntry, ...] | None
    context: Literal["authored", "homework"]
    not_opened: tuple[NotOpenedEntry, ...] = field(default=())


def run_stage1(
    *,
    filename: str,
    content: bytes,
    context: Literal["authored", "homework"],
    archive_skip_matcher: SkipMatcher | None = None,
) -> Stage1Result:
    """Run the synchronous Stage 1 pipeline; raise on first violation.

    Args:
        filename: User-supplied filename. NFKC normalization is
            applied internally for extension parsing and whitelist
            comparison; the original is surfaced in error detail.
        content: Full upload bytes. Size is bounded by the policy
            cap; libmagic only inspects the first ~2KB.
        context: ``"authored"`` (course author uploads) or
            ``"homework"`` (student submissions). Resolves the
            active :class:`ContextPolicy`.
        archive_skip_matcher: Optional denylist skip gate for the
            archive branch (№14): matched entries are silently
            dropped before resource accounting instead of
            fail-closing the whole upload on junk (``__MACOSX/``,
            ``node_modules/`` …). Callers inject the canonical
            normalizer ``denylist_prefix`` — the security layer
            keeps zero upward imports. Ignored for non-archive
            uploads.

    Returns:
        :class:`Stage1Result` populated only when every check
        passes. Stage 2 dispatch (commit i) decides whether to run
        the LLM safety classifier based on
        ``policy.enable_llm_safety_check``.

    Raises:
        SecurityRejectedError: with a category indicating the
            failed check. Each rejection is logged at WARNING
            level before re-raising; see module docstring for the
            log schema.
        ValueError: if ``context`` is not a valid policy
            discriminator (propagated from :func:`policy_for`).
    """
    try:
        policy = policy_for(context)

        ext = extension_of(filename)
        if not ext:
            raise SecurityRejectedError(
                ErrorCategory.FORBIDDEN_TYPE,
                f"missing or unrecognised extension in {filename!r}",
            )

        if ext not in policy.allowed_extensions:
            raise SecurityRejectedError(
                ErrorCategory.FORBIDDEN_TYPE,
                (f"extension {ext!r} not allowed for context {policy.name!r}"),
            )

        max_size = get_max_size_for_extension(ext, policy)
        if len(content) > max_size:
            raise SecurityRejectedError(
                ErrorCategory.SIZE_LIMIT,
                (
                    f"file size {len(content)} bytes exceeds context "
                    f"{policy.name!r} cap {max_size} bytes for extension "
                    f"{ext!r}"
                ),
            )

        verify_extension_matches_content(filename, content)
        detected_mime = detect_mime_type(content)

        archive_kind = archive_kind_for_filename(filename)
        if archive_kind is not None:
            return _handle_archive_input(
                filename=filename,
                content=content,
                extension=ext,
                detected_mime=detected_mime,
                archive_kind=archive_kind,
                policy=policy,
                context=context,
                skip_matcher=archive_skip_matcher,
            )

        nfc_text: str | None = None
        if _is_text_extension(ext):
            nfc_text = _run_text_content_checks(
                content=content,
                filename=filename,
                enable_charset_strict=policy.enable_charset_strict,
            )

        return Stage1Result(
            filename=nfc_for_storage(filename),
            extension=ext,
            detected_mime=detected_mime,
            detected_charset=detect_charset(content),
            nfc_text=nfc_text,
            archive_entries=None,
            context=context,
        )
    except SecurityRejectedError as exc:
        logger.warning(
            "stage1.rejected",
            category=exc.category.value,
            filename=filename,
            context=context,
            detail=exc.detail,
        )
        raise


# ── Archive handling ───────────────────────────────────────────────


# Extractor verdicts that become a "not opened" record, mapped onto the one
# code vocabulary the rest of the surface already uses. DENYLIST_SKIP is
# absent on purpose (see the loop); INCLUDED never reaches here.
_VERDICT_REASON: Final[dict[EntryVerdict, ErrorCategory]] = {
    EntryVerdict.FORBIDDEN_TYPE: ErrorCategory.FORBIDDEN_TYPE,
    EntryVerdict.MAGIC_MISMATCH: ErrorCategory.MAGIC_MISMATCH,
    EntryVerdict.NESTED_ARCHIVE: ErrorCategory.NESTED_ARCHIVE,
}


def _handle_archive_input(
    *,
    filename: str,
    content: bytes,
    extension: str,
    detected_mime: str,
    archive_kind: Literal["zip", "tar.gz"],
    policy: ContextPolicy,
    context: Literal["authored", "homework"],
    skip_matcher: SkipMatcher | None,
) -> Stage1Result:
    """Drain the archive iterator eagerly; raise on first violation.

    Iterating eagerly is intentional: the all-or-nothing contract
    means a partial yield must never reach Stage 2. ``tuple(...)``
    forces every entry through the same byte budget and per-entry
    structural checks before the result is built. With a
    ``skip_matcher`` the strict extractor silently drops denylist
    junk before accounting (№14) — ``archive_entries`` carries only
    the surviving files.
    """
    if (
        policy.max_archive_unzipped_bytes is None
        or policy.max_archive_nesting_depth is None
    ):
        # Defensive invariant: policy whitelisted an archive
        # extension but left the caps unset. Guards against silent
        # KeyError-equivalents from passing None to archive layer.
        raise SecurityRejectedError(
            ErrorCategory.FORBIDDEN_TYPE,
            (
                f"archive uploads not configured for context "
                f"{policy.name!r}; internal policy invariant violation"
            ),
        )

    soft = policy.archive_soft_exclude
    # ``classify`` is typed as a Literal on the overloads (it selects the
    # yielded type), so the branch is explicit rather than a passed-through
    # bool -- that is what keeps the two return types apart under --strict.
    entries: tuple[ExtractedFile | ClassifiedEntry, ...]
    if soft:
        entries = tuple(
            extract_archive_safely(
                content,
                archive_kind=archive_kind,
                max_unzipped_size=policy.max_archive_unzipped_bytes,
                max_nesting_depth=policy.max_archive_nesting_depth,
                allowed_extensions=policy.allowed_extensions,
                classify=True,
                skip_matcher=skip_matcher,
            )
        )
    else:
        entries = tuple(
            extract_archive_safely(
                content,
                archive_kind=archive_kind,
                max_unzipped_size=policy.max_archive_unzipped_bytes,
                max_nesting_depth=policy.max_archive_nesting_depth,
                allowed_extensions=policy.allowed_extensions,
                skip_matcher=skip_matcher,
            )
        )

    read: list[ExtractedFile | ClassifiedEntry] = []
    not_opened: list[NotOpenedEntry] = []

    for entry in entries:
        if (
            isinstance(entry, ClassifiedEntry)
            and entry.verdict is not EntryVerdict.INCLUDED
        ):
            if entry.verdict is EntryVerdict.DENYLIST_SKIP:
                # Packaging noise (__MACOSX/, node_modules/ ...). Dropped
                # rather than reported: naming it would bury the entries the
                # student can actually act on.
                continue
            not_opened.append(
                NotOpenedEntry(
                    arcname=entry.arcname,
                    reason=_VERDICT_REASON[entry.verdict],
                    size=entry.declared_size,
                )
            )
            continue

        entry_ext = extension_of(entry.arcname)
        if _is_text_extension(entry_ext):
            try:
                # Return value discarded -- inside-archive entries are handed
                # to downstream callers as raw bytes via ``archive_entries``;
                # the storage-side NFC pass happens there. Here we only
                # enforce the validation gates.
                _run_text_content_checks(
                    content=entry.content,
                    filename=entry.arcname,
                    enable_charset_strict=policy.enable_charset_strict,
                )
            except SecurityRejectedError as exc:
                # The split that defines the soft mode: a file saved in the
                # wrong encoding is a formatting mistake, so it is set aside
                # and the rest of the work is still reviewed. A unicode
                # hard-reject or an injection hit is not a mistake, so it
                # propagates and takes the whole submission with it --
                # "name it and skip it" there would be a ready-made bypass.
                if not (soft and exc.category is ErrorCategory.CHARSET_VIOLATION):
                    raise
                not_opened.append(
                    NotOpenedEntry(
                        arcname=entry.arcname,
                        reason=ErrorCategory.CHARSET_VIOLATION,
                        size=len(entry.content),
                    )
                )
                continue

        read.append(entry)

    if soft and not read:
        # Nothing survived. An empty body would buy a confident review of
        # nothing, at full price; the student is told instead.
        raise SecurityRejectedError(
            ErrorCategory.EMPTY_DOCUMENT,
            (
                f"archive {filename!r} contains no readable file "
                f"({len(not_opened)} set aside)"
            ),
        )

    return Stage1Result(
        filename=nfc_for_storage(filename),
        extension=extension,
        detected_mime=detected_mime,
        detected_charset=None,
        nfc_text=None,
        archive_entries=tuple(read),
        not_opened=tuple(not_opened),
        context=context,
    )


def archive_kind_for_filename(
    filename: str,
) -> Literal["zip", "tar.gz"] | None:
    """Return the archive kind for ``filename`` or ``None``.

    Matches the suffix taxonomy used by
    :mod:`course_supporter.security.archive`. ``.tar.gz`` /
    ``.tgz`` map to ``"tar.gz"``; bare ``.gz`` is also routed to
    ``"tar.gz"`` -- single-file gzip is not a tar and the archive
    layer rejects it as malformed. This is the narrowest behavior
    consistent with the homework allow-list (``.gz`` / ``.tgz``);
    the alternative (silently accept bare gzip) would skip path
    validation and member discovery entirely.

    Public (KD18 P2): reused by the base-attach route and the
    base-normalize worker to resolve ``archive_kind`` from the upload
    filename / S3 key extension, keeping route↔worker consistent.
    """
    norm = normalize_filename(filename).lower()
    if norm.endswith(".tar.gz") or norm.endswith(".tgz"):
        return "tar.gz"
    if norm.endswith(".zip"):
        return "zip"
    if norm.endswith(".gz"):
        return "tar.gz"
    return None


# ── Text content pipeline ──────────────────────────────────────────


def _is_text_extension(extension: str) -> bool:
    """``True`` if ``extension`` requires the text-content pipeline."""
    return extension.lower() in _TEXT_EXTENSIONS


def _run_text_content_checks(
    *,
    content: bytes,
    filename: str,
    enable_charset_strict: bool,
) -> str:
    """Decode, NFKC-normalize, run unicode + regex; return NFC for storage.

    Pipeline order (cheap-fail-fast):

    1. Charset gate (when strict). Library-detected charset must
       be UTF-8 / ASCII; legacy encodings reach
       :attr:`ErrorCategory.CHARSET_VIOLATION` here so the upload
       fails before any decode work.
    2. Three-tier decode -- see :func:`_decode_text`.
    3. NFKC normalization for security (compatibility-folding so
       full-width / circled / presentation forms collapse to ASCII
       before the regex layer scans).
    4. Unicode hard-reject (zero-width / bidi / tag / control).
    5. Regex pre-screen for known prompt-injection phrases.
    6. NFC normalization for storage.

    Args:
        content: Bytes to decode and validate.
        filename: Used in error / log detail to identify which
            entry failed (especially inside archives).
        enable_charset_strict: When ``True``, non-UTF-8 / non-ASCII
            charsets raise :attr:`ErrorCategory.CHARSET_VIOLATION`
            and decoding is forced to UTF-8.

    Returns:
        NFC-normalized text body suitable for downstream storage
        and LLM ingestion.

    Raises:
        SecurityRejectedError: with one of
            ``CHARSET_VIOLATION`` / ``SUSPICIOUS_UNICODE`` /
            ``PROMPT_INJECTION`` depending on which gate failed.
    """
    detected = detect_charset(content)

    if enable_charset_strict and (
        detected is None or detected.lower() not in _STRICT_ALLOWED_CHARSETS
    ):
        raise SecurityRejectedError(
            ErrorCategory.CHARSET_VIOLATION,
            (
                f"strict charset enforcement: {filename!r} reports charset "
                f"{detected!r}; expected one of {sorted(_STRICT_ALLOWED_CHARSETS)}"
            ),
        )

    if enable_charset_strict:
        # Strict: charset gate above guaranteed UTF-8 / ASCII.
        # A naked UnicodeDecodeError here would mean mid-file
        # corruption that libmagic's head-only inspection missed;
        # it propagates unchanged (rare bug-not-policy concern).
        text = _decode_text(content, detected, strict=True)
    else:
        try:
            text = _decode_text(content, detected, strict=False)
        except (UnicodeDecodeError, LookupError) as exc:
            # Non-strict tier-2 failure: libmagic detected a charset
            # but the bytes do not decode cleanly OR the label is
            # not a registered Python codec. Lossy UTF-8-with-
            # replacement fallback preserves the upload but emits a
            # structured warning so production telemetry can surface
            # encoding anomalies without blocking authored content.
            logger.warning(
                "stage1_charset_decode_fallback",
                filename=filename,
                detected_charset=detected,
                error=str(exc),
            )
            text = content.decode("utf-8", errors="replace")

    nfkc_text = nfkc_for_security(text)
    # DD-SP-E: a single leading U+FEFF is a legitimate UTF-8 byte-order mark
    # (the common source is a Google Docs "export as plain text", which
    # prepends one), not an in-text zero-width obfuscation attempt. Strip
    # exactly one leading BOM at the input to the unicode hard-reject so a
    # benign encoding mark does not falsely reject the upload; in-text
    # zero-width characters are still rejected by check_text_unicode_safety.
    if nfkc_text.startswith("\ufeff"):
        nfkc_text = nfkc_text[1:]
    check_text_unicode_safety(nfkc_text)

    matched = match_text(nfkc_text)
    if matched is not None:
        raise SecurityRejectedError(
            ErrorCategory.PROMPT_INJECTION,
            (f"matched pattern category {matched.category!r} in {filename!r}"),
        )

    return nfc_for_storage(text)


def _decode_text(content: bytes, charset: str | None, *, strict: bool) -> str:
    """Decode ``content`` per the three-tier policy.

    Tier 1 -- strict: UTF-8 only. The charset gate has already
    confirmed the libmagic label is UTF-8 / ASCII, so this decode
    is expected to succeed; a stray ``UnicodeDecodeError`` would
    indicate a bug or a corrupted middle-of-file (libmagic only
    inspects the head). Propagated unchanged.

    Tier 2 -- non-strict, library-detected charset: try the label
    libmagic reported. Authored content frequently ships in
    Windows-1251 / KOI8-R / ISO-8859 series; honoring the detected
    label lets the orchestrator preserve content semantics. May
    raise :class:`UnicodeDecodeError` (label valid but bytes do
    not decode) or :class:`LookupError` (label is not a registered
    Python codec). The caller in :func:`_run_text_content_checks`
    catches these and falls through to a UTF-8-with-replacement
    decode, emitting a structured ``stage1_charset_decode_fallback``
    warning so production telemetry can detect anomalies.

    Tier 3 -- non-strict fallback (charset is ``None`` here): UTF-8
    with replacement. Lossy by design; never raises. Reached only
    when libmagic cannot determine a charset at all.
    """
    if strict:
        return content.decode("utf-8")

    if charset is not None:
        return content.decode(charset)

    return content.decode("utf-8", errors="replace")
