"""Guard rail: model names live in config.toml and nowhere else.

CLAUDE.md rule 5. A model name compiled into ``src/`` is a change that has to be
shipped as code instead of edited in config, and it is the single easiest way for
a job to silently stop using the model it is configured to use.
"""

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

BANNED = ("opus", "sonnet", "haiku", "claude-3", "claude-4", "claude-5")

SCANNED_SUFFIXES = {".py", ".sql", ".toml", ".md", ".html", ".j2", ".jinja", ".txt", ".json"}


def test_no_source_file_hardcodes_a_model_name():
    offenders = []
    for path in sorted(SRC.rglob("*")):
        # Templates count: Jinja under src/hal_mary/web/templates/ could otherwise
        # name a model in a form default or a status page and escape the guard.
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            lowered = line.lower()
            for needle in BANNED:
                if needle in lowered:
                    offenders.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")
    assert offenders == [], "model names must come from config.toml:\n" + "\n".join(offenders)
