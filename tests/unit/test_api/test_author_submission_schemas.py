"""The schemas of the author's submissions screen: the contract the panel reads.

The panel is built from the OpenAPI document, so every field has to say what it
is (M-3, 24.11), and the three routes have to be there under their own names.
"""

from __future__ import annotations

import pytest

from course_supporter.api import schemas
from course_supporter.api.app import app

AUTHOR_SCHEMAS = (
    schemas.AuthorStudentSubmissions,
    schemas.AuthorStudentHeader,
    schemas.AuthorStudentEnrollment,
    schemas.AuthorSubmissionListItem,
    schemas.AuthorSubmissionDetail,
    schemas.AuthorSubmissionStudent,
    schemas.AuthorCriterionVerdict,
    schemas.AuthorPassRule,
    schemas.AuthorSubmissionFileLink,
)


@pytest.mark.parametrize("model", AUTHOR_SCHEMAS, ids=lambda m: m.__name__)
def test_every_field_has_a_description(model: type[schemas.BaseModel]) -> None:
    undescribed = [
        name for name, field in model.model_fields.items() if not field.description
    ]
    assert undescribed == []


def test_the_four_routes_answer_with_their_schemas() -> None:
    spec = app.openapi()["paths"]
    base = "/api/v1/students/{student_id}/submissions"
    expected = {
        base: "AuthorStudentSubmissions",
        f"{base}/{{submission_id}}": "AuthorSubmissionDetail",
        f"{base}/{{submission_id}}/file": "AuthorSubmissionFileLink",
        f"{base}/{{submission_id}}/comments": "RemarkThreads",
    }
    for path, name in expected.items():
        ref = spec[path]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        assert ref.endswith(f"/{name}")


def test_nothing_of_the_trace_or_the_cost_is_a_field_of_the_author_schemas() -> None:
    forbidden = {
        "safety_result",
        "sanity_result",
        "review_result",
        "job_id",
        "file_url",
        "snapshot_key",
        "billing_note",
        "cost_units",
        "charged_units",
        "error_message",
    }
    for model in AUTHOR_SCHEMAS:
        assert forbidden & set(model.model_fields) == set(), model.__name__
