# ruff: noqa: E501, PTH118, PTH120
"""Build the other languages of the documentation into <site>/<code>/ after the main build."""

import os

from mkdocs.commands.build import build
from mkdocs.config import load_config

OTHER_LANGUAGES = {"en": "mkdocs.en.yml"}


def on_post_build(config, **kwargs):
    if config.extra.get("language_build"):
        return
    root = os.path.dirname(config.config_file_path)
    for code, file in OTHER_LANGUAGES.items():
        build(
            load_config(
                os.path.join(root, file),
                site_dir=os.path.join(config.site_dir, code),
                strict=config.strict,
            )
        )
