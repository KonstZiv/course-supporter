"""Per-language letter and word data, plus the two signals built on them.

Home for facts about languages themselves -- which letters an alphabet
has, which words carry no meaning of their own. Two very different
callers need the same facts:

* ``ingestion`` filters stop words out of identifier matching;
* ``security`` verifies a recovered character encoding by asking whether
  the recovered text is actually written in the language of the course.

That second caller is why this module exists rather than living next to
the stop words in ``ingestion``. Today ``security`` imports nothing from
``ingestion`` while eight ``ingestion`` modules import from ``security``;
reaching the other way would invert an established one-way edge for the
sake of a word list. This module sits below both and imports neither.

Keys are ISO 639-3 throughout -- the same alphabet of codes the database
and the language whitelist use, so no caller has to translate. Note that
one language here, Montenegrin, has no ISO 639-1 code at all, which is
the second reason not to key on the two-letter form.
"""

from __future__ import annotations

import re
import string
import unicodedata
from collections.abc import Iterable, Sequence

# ---------------------------------------------------------------------------
# Natural language stop words
# ---------------------------------------------------------------------------
# Moved verbatim from ``ingestion/stop_words.py``; ``build_stop_words``
# still serves them to the identifier matcher, unchanged.

_ENGLISH: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "must",
        "not",
        "no",
        "and",
        "or",
        "but",
        "if",
        "then",
        "else",
        "for",
        "in",
        "on",
        "at",
        "to",
        "from",
        "with",
        "by",
        "of",
        "that",
        "this",
        "it",
        "its",
        "we",
        "you",
        "he",
        "she",
        "they",
        "them",
        "our",
        "your",
        "my",
        "his",
        "her",
        "their",
        "what",
        "which",
        "who",
        "how",
        "when",
        "where",
        "why",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "than",
        "too",
        "very",
        "just",
        "about",
        "above",
        "after",
        "before",
        "between",
        "into",
        "through",
        "during",
        "without",
        "again",
        "further",
        "once",
        "here",
        "there",
        "so",
        "up",
        "out",
        "only",
        "also",
        "now",
        "new",
        "one",
        "two",
        "three",
        "first",
        "function",
        "method",
        "variable",
        "value",
        "used",
        "called",
        "using",
    }
)

_UKRAINIAN: frozenset[str] = frozenset(
    {
        "це",
        "та",
        "і",
        "в",
        "у",
        "на",
        "з",
        "до",
        "від",
        "за",
        "як",
        "що",
        "але",
        "або",
        "не",
        "так",
        "ми",
        "ви",
        "він",
        "вона",
        "вони",
        "воно",
        "мій",
        "наш",
        "ваш",
        "його",
        "її",
        "їх",
        "той",
        "цей",
        "ця",
        "ці",
        "тут",
        "там",
        "коли",
        "де",
        "чому",
        "який",
        "яка",
        "яке",
        "які",
        "все",
        "вже",
        "ще",
        "тільки",
        "також",
        "дуже",
        "треба",
        "можна",
        "потрібно",
        "зараз",
        "потім",
        "після",
        "перед",
        "між",
        "через",
        "без",
        "над",
        "під",
        "про",
        "функція",
        "метод",
        "змінна",
        "значення",
        "використовується",
    }
)

_GERMAN: frozenset[str] = frozenset(
    {
        "der",
        "die",
        "das",
        "ein",
        "eine",
        "ist",
        "sind",
        "war",
        "waren",
        "hat",
        "haben",
        "wird",
        "werden",
        "kann",
        "können",
        "muss",
        "müssen",
        "und",
        "oder",
        "aber",
        "wenn",
        "dann",
        "weil",
        "dass",
        "nicht",
        "mit",
        "von",
        "zu",
        "auf",
        "in",
        "an",
        "für",
        "aus",
        "bei",
        "ich",
        "du",
        "er",
        "sie",
        "es",
        "wir",
        "ihr",
        "man",
        "was",
        "wer",
        "wie",
        "wo",
        "wann",
        "warum",
        "welch",
        "hier",
        "dort",
        "jetzt",
        "noch",
        "schon",
        "auch",
        "nur",
        "sehr",
        "alle",
        "viel",
        "mehr",
        "kein",
        "jede",
        "diese",
        "neue",
        "funktion",
        "methode",
        "variable",
        "wert",
    }
)

_SERBIAN: frozenset[str] = frozenset(
    {
        "је",
        "су",
        "био",
        "била",
        "били",
        "има",
        "ће",
        "би",
        "и",
        "или",
        "али",
        "ако",
        "да",
        "не",
        "ни",
        "већ",
        "у",
        "на",
        "за",
        "од",
        "до",
        "из",
        "са",
        "по",
        "ка",
        "ја",
        "ти",
        "он",
        "она",
        "оно",
        "ми",
        "ви",
        "они",
        "мој",
        "твој",
        "наш",
        "ваш",
        "његов",
        "њен",
        "њихов",
        "тај",
        "овај",
        "онај",
        "ова",
        "ово",
        "ови",
        "шта",
        "ко",
        "како",
        "где",
        "кад",
        "зашто",
        "који",
        "све",
        "још",
        "само",
        "такође",
        "врло",
        "сада",
        "после",
        "функција",
        "метод",
        "променљива",
        "вредност",
    }
)

_CROATIAN: frozenset[str] = frozenset(
    {
        "je",
        "su",
        "bio",
        "bila",
        "bili",
        "ima",
        "biti",
        "i",
        "ili",
        "ali",
        "ako",
        "da",
        "ne",
        "ni",
        "već",
        "u",
        "na",
        "za",
        "od",
        "do",
        "iz",
        "sa",
        "po",
        "ka",
        "ja",
        "ti",
        "on",
        "ona",
        "ono",
        "mi",
        "vi",
        "oni",
        "moj",
        "tvoj",
        "naš",
        "vaš",
        "njegov",
        "njen",
        "njihov",
        "taj",
        "ovaj",
        "onaj",
        "ova",
        "ovo",
        "ovi",
        "što",
        "tko",
        "kako",
        "gdje",
        "kad",
        "zašto",
        "koji",
        "sve",
        "još",
        "samo",
        "također",
        "vrlo",
        "sada",
        "poslije",
        "funkcija",
        "metoda",
        "varijabla",
        "vrijednost",
    }
)

# Montenegrin is very close to Serbian/Croatian
_MONTENEGRIN: frozenset[str] = _SERBIAN | frozenset(
    {
        "đe",
        "nijesam",
        "sjutra",
        "śutra",
    }
)

_SPANISH: frozenset[str] = frozenset(
    {
        "el",
        "la",
        "los",
        "las",
        "un",
        "una",
        "es",
        "son",
        "fue",
        "era",
        "tiene",
        "hay",
        "ser",
        "estar",
        "haber",
        "hacer",
        "no",
        "sí",
        "pero",
        "sino",
        "que",
        "como",
        "cuando",
        "donde",
        "por",
        "para",
        "con",
        "sin",
        "sobre",
        "entre",
        "hasta",
        "desde",
        "yo",
        "tú",
        "él",
        "ella",
        "nosotros",
        "ellos",
        "ellas",
        "este",
        "esta",
        "esto",
        "ese",
        "eso",
        "aquel",
        "aquella",
        "qué",
        "quién",
        "cómo",
        "dónde",
        "cuándo",
        "por qué",
        "todo",
        "cada",
        "muy",
        "más",
        "menos",
        "bien",
        "mal",
        "también",
        "solo",
        "ahora",
        "después",
        "antes",
        "aquí",
        "allí",
        "función",
        "método",
        "variable",
        "valor",
    }
)

_TURKISH: frozenset[str] = frozenset(
    {
        "bir",
        "bu",
        "şu",
        "var",
        "yok",
        "ile",
        "için",
        "gibi",
        "ama",
        "fakat",
        "veya",
        "hem",
        "çünkü",
        "eğer",
        "ise",
        "ben",
        "sen",
        "biz",
        "siz",
        "onlar",
        "ne",
        "kim",
        "nasıl",
        "nerede",
        "neden",
        "hangi",
        "çok",
        "daha",
        "en",
        "sadece",
        "bile",
        "hala",
        "şimdi",
        "sonra",
        "önce",
        "burada",
        "orada",
        "arasında",
        "fonksiyon",
        "metot",
        "değişken",
        "değer",
    }
)

_ROMANIAN: frozenset[str] = frozenset(
    {
        "este",
        "sunt",
        "era",
        "fost",
        "are",
        "avea",
        "face",
        "și",
        "sau",
        "dar",
        "dacă",
        "când",
        "unde",
        "cum",
        "nu",
        "mai",
        "foarte",
        "doar",
        "încă",
        "deja",
        "de",
        "la",
        "în",
        "pe",
        "cu",
        "din",
        "prin",
        "între",
        "eu",
        "tu",
        "el",
        "ea",
        "noi",
        "voi",
        "lor",
        "ce",
        "cine",
        "care",
        "tot",
        "fiecare",
        "alt",
        "acest",
        "acel",
        "aici",
        "acolo",
        "funcție",
        "metodă",
        "variabilă",
        "valoare",
    }
)

_BULGARIAN: frozenset[str] = frozenset(
    {
        "е",
        "са",
        "бе",
        "беше",
        "бяха",
        "има",
        "ще",
        "би",
        "и",
        "или",
        "но",
        "ако",
        "да",
        "не",
        "нито",
        "вече",
        "в",
        "на",
        "за",
        "от",
        "до",
        "из",
        "със",
        "по",
        "към",
        "аз",
        "ти",
        "той",
        "тя",
        "то",
        "ние",
        "вие",
        "те",
        "мой",
        "твой",
        "наш",
        "ваш",
        "негов",
        "нейн",
        "техен",
        "този",
        "тази",
        "това",
        "тези",
        "онзи",
        "оня",
        "какво",
        "кой",
        "как",
        "къде",
        "кога",
        "защо",
        "който",
        "всичко",
        "още",
        "само",
        "също",
        "много",
        "сега",
        "после",
        "функция",
        "метод",
        "променлива",
        "стойност",
    }
)

_POLISH: frozenset[str] = frozenset(
    {
        "jest",
        "są",
        "był",
        "była",
        "było",
        "byli",
        "ma",
        "mieć",
        "i",
        "lub",
        "ale",
        "jeśli",
        "że",
        "bo",
        "gdy",
        "nie",
        "tak",
        "już",
        "jeszcze",
        "tylko",
        "też",
        "bardzo",
        "w",
        "na",
        "za",
        "od",
        "do",
        "z",
        "ze",
        "po",
        "przez",
        "ja",
        "ty",
        "on",
        "ona",
        "ono",
        "my",
        "wy",
        "oni",
        "mój",
        "twój",
        "nasz",
        "wasz",
        "jego",
        "jej",
        "ich",
        "ten",
        "ta",
        "to",
        "te",
        "tamten",
        "tutaj",
        "tam",
        "co",
        "kto",
        "jak",
        "gdzie",
        "kiedy",
        "dlaczego",
        "który",
        "wszystko",
        "każdy",
        "inny",
        "nowy",
        "teraz",
        "potem",
        "funkcja",
        "metoda",
        "zmienna",
        "wartość",
    }
)

_LITHUANIAN: frozenset[str] = frozenset(
    {
        "yra",
        "buvo",
        "bus",
        "turi",
        "gali",
        "reikia",
        "ir",
        "arba",
        "bet",
        "jei",
        "kai",
        "nes",
        "kad",
        "ne",
        "taip",
        "dar",
        "jau",
        "tik",
        "labai",
        "dabar",
        "aš",
        "tu",
        "jis",
        "ji",
        "mes",
        "jūs",
        "jie",
        "mano",
        "tavo",
        "mūsų",
        "jūsų",
        "jo",
        "jos",
        "jų",
        "tas",
        "ta",
        "tie",
        "čia",
        "ten",
        "kas",
        "kaip",
        "kur",
        "kada",
        "kodėl",
        "kuris",
        "viskas",
        "kiekvienas",
        "kitas",
        "naujas",
        "paskui",
        "funkcija",
        "metodas",
        "kintamasis",
        "reikšmė",
    }
)

_SWEDISH: frozenset[str] = frozenset(
    {
        "är",
        "var",
        "har",
        "hade",
        "kan",
        "ska",
        "vill",
        "måste",
        "och",
        "eller",
        "men",
        "om",
        "när",
        "där",
        "hur",
        "inte",
        "också",
        "bara",
        "redan",
        "mycket",
        "mer",
        "en",
        "ett",
        "den",
        "det",
        "de",
        "denna",
        "detta",
        "jag",
        "du",
        "han",
        "hon",
        "vi",
        "ni",
        "min",
        "din",
        "vår",
        "er",
        "hans",
        "hennes",
        "deras",
        "vad",
        "vem",
        "varför",
        "vilken",
        "allt",
        "varje",
        "annan",
        "här",
        "sedan",
        "före",
        "funktion",
        "metod",
        "variabel",
        "värde",
    }
)

_FRENCH: frozenset[str] = frozenset(
    {
        "est",
        "sont",
        "était",
        "été",
        "avoir",
        "faire",
        "le",
        "la",
        "les",
        "un",
        "une",
        "des",
        "du",
        "et",
        "ou",
        "mais",
        "donc",
        "car",
        "que",
        "qui",
        "ne",
        "pas",
        "plus",
        "très",
        "aussi",
        "encore",
        "de",
        "en",
        "dans",
        "sur",
        "pour",
        "par",
        "avec",
        "sans",
        "je",
        "tu",
        "il",
        "elle",
        "nous",
        "vous",
        "ils",
        "elles",
        "mon",
        "ton",
        "son",
        "notre",
        "votre",
        "leur",
        "ce",
        "cette",
        "ces",
        "ici",
        "là",
        "quoi",
        "comment",
        "où",
        "quand",
        "pourquoi",
        "quel",
        "tout",
        "chaque",
        "autre",
        "nouveau",
        "maintenant",
        "après",
        "fonction",
        "méthode",
        "variable",
        "valeur",
    }
)


# Russian is the one set written for this module rather than moved into
# it. The whitelist calls it mandatory ("many Ukrainian-authored courses
# are recorded in Russian"), so a Russian course is a live case, and
# without a set here every non-UTF-8 file on such a course would be
# refused for lack of a way to verify the recovery. Same shape as
# ``_UKRAINIAN``: function words plus the handful of course-domain nouns
# that carry no meaning for identifier matching either.
_RUSSIAN: frozenset[str] = frozenset(
    {
        "это",
        "и",
        "в",
        "на",
        "с",
        "к",
        "до",
        "от",
        "за",
        "как",
        "что",
        "но",
        "или",
        "не",
        "так",
        "же",
        "бы",
        "мы",
        "вы",
        "он",
        "она",
        "они",
        "оно",
        "мой",
        "наш",
        "ваш",
        "его",
        "её",
        "их",
        "тот",
        "этот",
        "эта",
        "эти",
        "здесь",
        "там",
        "когда",
        "где",
        "почему",
        "который",
        "которая",
        "которое",
        "которые",
        "всё",
        "уже",
        "ещё",
        "только",
        "также",
        "очень",
        "надо",
        "можно",
        "нужно",
        "сейчас",
        "потом",
        "после",
        "перед",
        "между",
        "через",
        "без",
        "над",
        "под",
        "про",
        "функция",
        "метод",
        "переменная",
        "значение",
        "используется",
    }
)

NATURAL_STOP_WORDS: dict[str, frozenset[str]] = {
    "eng": _ENGLISH,
    "ukr": _UKRAINIAN,
    "rus": _RUSSIAN,
    "deu": _GERMAN,
    "srp": _SERBIAN,
    "hrv": _CROATIAN,
    "cnr": _MONTENEGRIN,
    "spa": _SPANISH,
    "tur": _TURKISH,
    "ron": _ROMANIAN,
    "bul": _BULGARIAN,
    "pol": _POLISH,
    "lit": _LITHUANIAN,
    "swe": _SWEDISH,
    "fra": _FRENCH,
}


# ---------------------------------------------------------------------------
# Alphabets
# ---------------------------------------------------------------------------
# One row per language that has a stop-word set above, so the two signals
# always cover the same languages: a language we can score by words we can
# also score by letters, and vice versa.
#
# What an alphabet is for here: deciding whether a decoded text is written
# in a language at all. A wrong single-byte decoding of Cyrillic text is
# still Cyrillic -- it just spells the words with letters that language
# never uses. Measured on the live cp1251 submission: read as ``kz1048``
# or ``ptcp154`` the text is 99.4% identical to the correct reading and
# differs in exactly thirteen characters, all of them Kazakh or Azeri
# letters standing where Ukrainian ``ї`` and ``є`` belong. Letters are what
# separates those readings; nothing else does (see ``charset_recovery``).
#
# Both cases are stored. Case folding is not a safe shortcut across these
# languages: ``'İ'.lower()`` is two characters, ``'ß'.upper()`` is ``'SS'``,
# and Turkish dotted/dotless ``i`` does not round-trip through Python's
# locale-independent mapping at all -- hence ``İ`` is listed explicitly.


def _letters(lower: str, *extra: str) -> frozenset[str]:
    """Both cases of every character in ``lower``, plus ``extra`` verbatim.

    A single-character uppercase form is added when one exists; ``ß``
    uppercases to the two-character ``SS``, which is not a letter of the
    alphabet, so multi-character results are dropped rather than split.
    """
    out: set[str] = set()
    for ch in lower:
        out.add(ch)
        upper = ch.upper()
        if len(upper) == 1:
            out.add(upper)
    out.update(extra)
    return frozenset(out)


# Serbian and Montenegrin are digraphic: both scripts are official and a
# course file may arrive in either, so both belong to the one alphabet.
_SERBIAN_CYRILLIC = "абвгдђежзијклљмнњопрстћуфхцчџш"
_SERBIAN_LATIN = "abcčćdđefghijklmnoprsštuvzž"

ALPHABETS: dict[str, frozenset[str]] = {
    "eng": _letters("abcdefghijklmnopqrstuvwxyz"),
    "ukr": _letters("абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"),
    "rus": _letters("абвгдеёжзийклмнопрстуфхцчшщъыьэюя"),
    "deu": _letters("abcdefghijklmnopqrstuvwxyzäöüß"),
    "srp": _letters(_SERBIAN_CYRILLIC + _SERBIAN_LATIN),
    "hrv": _letters("abcčćdđefghijklmnoprsštuvzž"),
    # Montenegrin adds ``ś``/``ź`` to the Latin alphabet. Their Cyrillic
    # counterparts ``с́``/``з́`` are written as the base letter plus
    # COMBINING ACUTE ACCENT (U+0301), which is a mark and not a letter --
    # it is skipped by the letter check rather than listed here.
    "cnr": _letters(_SERBIAN_CYRILLIC + _SERBIAN_LATIN + "śź"),
    "spa": _letters("abcdefghijklmnñopqrstuvwxyzáéíóúü"),
    # ``İ`` explicitly: see ``_letters``.
    "tur": _letters("abcçdefgğhıijklmnoöprsştuüvyz", "İ"),
    # Both the standard comma-below ``ș``/``ț`` and the cedilla ``ş``/``ţ``
    # they are still routinely typed as: a legacy Romanian file uses the
    # latter, and calling those letters foreign would refuse a correct
    # recovery for a spelling convention.
    "ron": _letters("aăâbcdefghiîjklmnopqrsștțuvwxyz", "ş", "Ş", "ţ", "Ţ"),
    "bul": _letters("абвгдежзийклмнопрстуфхцчшщъьюя"),
    "pol": _letters("aąbcćdeęfghijklłmnńoóprsśtuwyzźż"),
    "lit": _letters("aąbcčdeęėfghiįyjklmnoprsštuųūvzž"),
    "swe": _letters("abcdefghijklmnopqrstuvwxyzåäö"),
    "fra": _letters("abcdefghijklmnopqrstuvwxyzàâäçéèêëîïôöùûüÿæœ"),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Basic Latin letters are never foreign, whatever the language. They carry
# no evidence about the encoding: every single-byte encoding in play here
# agrees on bytes 0x41-0x5A and 0x61-0x7A, so those characters read the
# same under the correct decoding and under every wrong one. Counting them
# would only punish the truth -- a Croatian, Serbian, Montenegrin, Turkish,
# Polish or Lithuanian alphabet has no ``q``/``w``/``x``/``y``, so a
# library name or a line of Python in an otherwise correct file would score
# as a bad decoding. Same reasoning as the programming keywords excluded
# from ``stop_word_share``: what every candidate shares cannot separate
# them. The alphabets themselves stay truthful about their languages; the
# exemption belongs to the check.
_ASCII_LETTERS = frozenset(string.ascii_letters)


def supported_languages() -> list[str]:
    """ISO 639-3 codes carrying both a stop-word set and an alphabet."""
    return sorted(NATURAL_STOP_WORDS.keys() & ALPHABETS.keys())


def has_language_data(code: str) -> bool:
    """``True`` when ``code`` has both signals and can therefore be verified."""
    return code in NATURAL_STOP_WORDS and code in ALPHABETS


def script_of(char: str) -> str | None:
    """Writing system of ``char``, or ``None`` when it is not a letter.

    Derived from the character's Unicode name rather than a range table:
    ``а`` is ``CYRILLIC SMALL LETTER A`` and ``a`` is ``LATIN SMALL LETTER
    A``, so the first word names the script. Marks, digits, punctuation
    and symbols return ``None`` -- including COMBINING ACUTE ACCENT, which
    is how Montenegrin ``с́`` stays a Cyrillic ``с`` for this purpose.
    """
    if not unicodedata.category(char).startswith("L"):
        return None
    try:
        name = unicodedata.name(char)
    except ValueError:  # unnamed character; no script to speak of
        return None
    return name.split(" ", 1)[0]


def alphabet_for(languages: Iterable[str]) -> frozenset[str]:
    """Union of the alphabets of ``languages``; unknown codes contribute none."""
    out: set[str] = set()
    for code in languages:
        out |= ALPHABETS.get(code, frozenset())
    return frozenset(out)


def count_foreign_letters(text: str, languages: Sequence[str]) -> int:
    """Letters of the languages' own scripts that their alphabets lack.

    Only the scripts those alphabets use are judged, and basic Latin
    letters are never judged at all (see ``_ASCII_LETTERS``). A Latin word
    inside Ukrainian prose -- a library name, a URL -- is not evidence of a
    bad decoding and is not counted; neither is the ``w`` in that same name
    inside Croatian prose, whose alphabet has no ``w``. A Cyrillic letter
    Ukrainian does not have is exactly that evidence, and is.
    """
    alphabet = alphabet_for(languages)
    if not alphabet:
        return 0
    scripts = {script_of(ch) for ch in alphabet} - {None}
    return sum(
        1
        for ch in text
        if ch not in alphabet and ch not in _ASCII_LETTERS and script_of(ch) in scripts
    )


def stop_word_share(text: str, languages: Sequence[str]) -> float:
    """Fraction of word tokens that are stop words of ``languages``.

    Natural-language sets only. Programming keywords are ASCII and read
    the same under every single-byte decoding, so they would score every
    candidate identically and separate nothing.
    """
    tokens = _WORD_RE.findall(text.lower())
    if not tokens:
        return 0.0
    words: set[str] = set()
    for code in languages:
        words |= NATURAL_STOP_WORDS.get(code, frozenset())
    if not words:
        return 0.0
    return sum(1 for token in tokens if token in words) / len(tokens)
