"""The door for the review-language form field.

``response_language`` used to be an unvalidated string on both submission
routes: ``xx`` travelled into the Mentor's instruction verbatim and the
model was left to decide what it meant. It now goes through the same
validator the rest of the project uses, so a typo is answered at the door.
"""

from __future__ import annotations

from typing import Annotated, get_args, get_type_hints

import pytest
from fastapi import FastAPI, Form
from fastapi.testclient import TestClient
from pydantic import AfterValidator

from course_supporter.api.routes import homework, portal_submissions
from course_supporter.api.schemas import (
    OptionalLanguageForm,
    _validate_optional_language,
)


def expected_validator() -> object:
    return _validate_optional_language


@pytest.fixture
def client() -> TestClient:
    """A minimal app carrying the exact annotation the routes carry."""
    app = FastAPI()

    @app.post("/echo")
    async def echo(
        response_language: Annotated[OptionalLanguageForm, Form()] = None,
    ) -> dict[str, str | None]:
        return {"language": response_language}

    return TestClient(app)


class TestTheDoor:
    def test_639_1_is_normalized_to_639_3(self, client: TestClient) -> None:
        resp = client.post("/echo", data={"response_language": "uk"})
        assert resp.status_code == 200
        assert resp.json()["language"] == "ukr"

    @pytest.mark.parametrize("value", ["ukr", "Ukrainian"])
    def test_the_other_accepted_forms(self, client: TestClient, value: str) -> None:
        assert client.post("/echo", data={"response_language": value}).json() == {
            "language": "ukr"
        }

    def test_nonsense_is_refused_with_422(self, client: TestClient) -> None:
        resp = client.post("/echo", data={"response_language": "xx"})
        assert resp.status_code == 422

    def test_a_real_language_outside_the_whitelist_is_refused(
        self, client: TestClient
    ) -> None:
        resp = client.post("/echo", data={"response_language": "lat"})
        assert resp.status_code == 422

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_means_not_given_not_invalid(
        self, client: TestClient, value: str
    ) -> None:
        # A form sends an empty string when the field is left alone; an
        # empty string is not a bad language, it is no language.
        resp = client.post("/echo", data={"response_language": value})
        assert resp.status_code == 200
        assert resp.json()["language"] is None

    def test_omitted_is_none(self, client: TestClient) -> None:
        assert client.post("/echo", data={}).json()["language"] is None


class TestBothRoutesUseIt:
    """Wiring lock: the validation is worth nothing on only one door."""

    @pytest.mark.parametrize(
        ("module", "func_name"),
        [
            (homework, "submit_homework"),
            (portal_submissions, "submit_portal_homework"),
        ],
    )
    def test_route_declares_the_validated_type(
        self, module: object, func_name: str
    ) -> None:
        # ``from __future__ import annotations`` makes signatures strings, so
        # resolve them; and ``Annotated`` flattens when nested, so the thing
        # to look for is the validator itself rather than the alias that
        # carries it.
        func = getattr(module, func_name)
        hints = get_type_hints(func, include_extras=True)
        validators = [
            arg
            for arg in get_args(hints["response_language"])
            if isinstance(arg, AfterValidator)
        ]
        assert [v.func for v in validators] == [expected_validator()]
