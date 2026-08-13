from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Dict, Mapping


@dataclass(frozen=True)
class SourceAsset:
    path: str
    content: bytes
    sha256: str
    mode: int


_EXPECTED = {'skills/koroche-blyat/LICENSE.txt': 'cf733a4af531facbdb2818cc4a06e8e5baf325b252501150614eb87b8d61501e', 'skills/koroche-blyat/NOTICE.md': 'f2f177dba9599ff7397bfffdfa708a2a80bed491b59bab341d39bc78fd2adcb4', 'skills/koroche-blyat/SKILL.md': '43cf5b31ec6c0167ecd1be94833d2f3e1faed53a415e0058948f604ff32f2fc3', 'skills/koroche-blyat/licenses/caveman-MIT.txt': '1cd9aa70ec104afb3b0d2dc2e5343230f74737dc01fdc8dad585c9da6449d5a5', 'skills/koroche-blyat/licenses/pohuy-MIT.txt': '27cd410525efac04b5fc0706333cbf92fcc7cefc246d5be33a3e1c77ace71205', 'skills/koroche-blyat/references/compression.md': '97c3189f5b4c09a80750dda2ecb60a565bd4aec08722837e1e08a47c15659147', 'skills/koroche-blyat/references/ontologia.md': '91e3e6f89ad22e75c011de97be22df855bfd743089551d93171fb81ad0d9a0e0', 'skills/koroche-blyat/references/sceny.md': '849be63a7e1def2310cabd2424021edae1c908cf3b5a99ce427e95e77e5813c4', 'skills/koroche-blyat/references/slovar.md': '5924e9436bdc75d25109876daefc32a0202da303feca328b21381ace7a554380', 'adapters/generated/always-on.md': '3ef284230c19b0bc6ceb9c0e7454483c1ac017aa11c34ad0ddc6ea7a672c5d42', 'adapters/generated/claude-output-style.md': '2675d3698c4111d6def20aee88494b632f4e8e667e9eacecb08ea2d03bc37887', 'adapters/generated/reminder.txt': '0e4805953875340289b5846684dc336f99175ccc2e9ce6ff9951a9109a86c452', 'adapters/prime/extension.ts': 'a44d58f73fb2d953e8020131ef9dada0ee153d304d63e52eb9707bd35eb7b92f', 'adapters/codex/user-prompt-reminder.sh': '2fd43a35f4bde3b307c0dd129da7ec3e5563c66c6a0b0548a073ae51ed8d7db5', 'adapters/claude/user-prompt-reminder.sh': '2fd43a35f4bde3b307c0dd129da7ec3e5563c66c6a0b0548a073ae51ed8d7db5'}


def load_sources(repo_root: Path) -> Mapping[str, SourceAsset]:
    root = Path(repo_root).resolve()
    result: Dict[str, SourceAsset] = {}
    for relative, expected_hash in sorted(_EXPECTED.items()):
        path = root / relative
        current = root
        unsafe = False
        for component in Path(relative).parts:
            current = current / component
            if current.is_symlink():
                unsafe = True
                break
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError):
            unsafe = True
        if unsafe or not path.is_file() or path.is_symlink():
            raise ValueError("required source is missing or unsafe: %s" % relative)
        content = path.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected_hash:
            raise ValueError("source SHA-256 mismatch: %s" % relative)
        mode = 0o755 if relative.endswith(".sh") else 0o644
        result[relative] = SourceAsset(relative, content, actual, mode)
    allowed = set(_EXPECTED)
    discovered = set()
    for directory in ("skills/koroche-blyat", "adapters/generated", "adapters/prime", "adapters/codex", "adapters/claude"):
        base = root / directory
        if base.exists():
            for path in base.rglob("*"):
                relative = path.relative_to(root).as_posix()
                if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                    raise ValueError("source tree contains an unsafe entry: %s" % relative)
                if path.is_file():
                    discovered.add(relative)
    unexpected = sorted(discovered - allowed)
    if unexpected:
        raise ValueError("source allowlist contains unexpected files: %s" % ", ".join(unexpected))
    return result
