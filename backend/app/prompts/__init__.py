"""Versioned prompt loader and untrusted-data framing helpers for prompt-injection defense."""

from dataclasses import dataclass
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class PromptTemplate:
    """Loaded versioned system prompt template."""

    name: str
    version: str
    content: str


def load_prompt(name: str, version: str = "v1") -> PromptTemplate:
    """Load a versioned Markdown prompt file from the prompts directory."""
    safe_name = name.strip().lower()
    safe_version = version.strip().lower()
    filename = f"{safe_name}_{safe_version}.md"
    prompt_path = (_PROMPTS_DIR / filename).resolve()
    if not prompt_path.is_relative_to(_PROMPTS_DIR) or not prompt_path.is_file():
        raise FileNotFoundError(f"Versioned prompt '{filename}' not found.")

    return PromptTemplate(
        name=safe_name,
        version=f"{safe_name}:{safe_version}",
        content=prompt_path.read_text(encoding="utf-8").strip(),
    )


def wrap_untrusted_data(label: str, content: str) -> str:
    """Wrap repository, issue, or retrieved text inside explicit untrusted data delimiters."""
    safe_label = label.replace('"', "'").strip()
    return (
        f'<UNTRUSTED_DATA label="{safe_label}">\n'
        f"{content}\n"
        f"</UNTRUSTED_DATA>"
    )