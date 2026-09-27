"""The codes of a test written in the system, read from the code (tasks 07b, 07c).

Every lock that holds documentation to these codes reads them here: the README
of the homework package (``tests/integration/test_reference_key_e2e_db.py``)
and the pages of the documentation site (``tests/unit/test_documentation_site.py``).
One set for both, so a code that a new vocabulary or route brings reaches each
lock at once, not only the one someone remembered to update.

The format's codes and an unfinished draft's have vocabularies. The routes
spell theirs out as string constants, so those are read from the modules'
source. Of the document routes' codes only the ``TEST_`` ones are a written
test's: the others there are other tasks' and are documented with them.
"""

from __future__ import annotations

import ast
import inspect
import re
from types import ModuleType

from course_supporter.api.routes import documents as documents_routes
from course_supporter.api.routes import test_objects as test_objects_routes
from course_supporter.homework.test_completeness import (
    DraftIncompleteError,
    IncompleteCode,
)
from course_supporter.homework.test_yaml import DraftRefusalCode


def codes_in(module: ModuleType) -> set[str]:
    """The refusal codes a module spells out: its string constants shaped like one."""
    return {
        node.value
        for node in ast.walk(ast.parse(inspect.getsource(module)))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.fullmatch(r"[A-Z]+(?:_[A-Z0-9]+)+", node.value)
    }


def written_test_codes() -> frozenset[str]:
    """Every code a test written in the system is refused or left unfinished with."""
    authors = codes_in(test_objects_routes)
    documents = {
        code for code in codes_in(documents_routes) if code.startswith("TEST_")
    }
    assert authors, "the author's routes refuse with some codes"
    assert documents, "the document routes refuse a written test with some codes"
    return frozenset(
        {code.value for code in DraftRefusalCode}
        | {code.value for code in IncompleteCode}
        | {DraftIncompleteError.code}
        | authors
        | documents
    )
