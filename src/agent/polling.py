import logging
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def parse_command(text: str) -> dict | None:
    text = text.strip()
    if "@renderpr" not in text:
        return None

    idx = text.find("@renderpr")
    after = text[idx + len("@renderpr"):].strip()

    if not after or after.startswith("review") or after.startswith("help"):
        return {"action": "review", "query": None}

    if after.startswith("code change"):
        query = after.removeprefix("code change").lstrip(": ").strip()
        if query:
            return {"action": "change", "query": query}

    if after.startswith("change "):
        return {"action": "change", "query": after.removeprefix("change ").strip()}

    if after == "apply":
        return {"action": "apply", "query": None}

    if after == "reject":
        return {"action": "reject", "query": None}

    return {"action": "review", "query": None}


def has_pending_edits(workdir: str) -> bool:
    result = subprocess.run(
        ["git", "diff", "--stat"],
        capture_output=True, text=True,
        cwd=workdir,
    )
    return bool(result.stdout.strip())


@dataclass
class ChangeSession:
    edited_files: list[str] = field(default_factory=list)

    def add_edit(self, file_path: str) -> None:
        if file_path not in self.edited_files:
            self.edited_files.append(file_path)

    def clear(self) -> None:
        self.edited_files.clear()
