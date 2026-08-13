#!/usr/bin/env bash
# Установка koroche-blyat для Codex CLI:
#   curl -fsSL https://raw.githubusercontent.com/maksimryzhov614/koroche-blyat/main/install-codex.sh | bash
#
# Ставит:
#   $CODEX_HOME/AGENTS.md            — политика в маркерном блоке, остальное не трогается
#   $CODEX_HOME/koroche-blyat.sh     — подкрепление стиля на каждом промпте
#
# Хук требует ручного доверия: codex features enable hooks, затем /hooks.
set -euo pipefail

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
RAW="${RAW:-https://raw.githubusercontent.com/maksimryzhov614/koroche-blyat/main}"
BEGIN="<!-- BEGIN KOROCHE-BLYAT -->"
END="<!-- END KOROCHE-BLYAT -->"

mkdir -p "$CODEX_HOME"
curl -fsSL "$RAW/hooks/style-reminder.sh" -o "$CODEX_HOME/koroche-blyat.sh"
chmod +x "$CODEX_HOME/koroche-blyat.sh"

POLICY=$(curl -fsSL "$RAW/codex/AGENTS-koroche-blyat.md")
AGENTS="$CODEX_HOME/AGENTS.md"
touch "$AGENTS"

python3 - "$AGENTS" "$BEGIN" "$END" <<PY
import sys
path, begin, end = sys.argv[1], sys.argv[2], sys.argv[3]
policy = """$POLICY"""
with open(path, encoding="utf-8") as handle:
    text = handle.read()
block = begin + "\n" + policy.strip() + "\n" + end
if begin in text and end in text:
    head, rest = text.split(begin, 1)
    _, tail = rest.split(end, 1)
    text = head + block + tail
else:
    text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block + "\n"
with open(path, "w", encoding="utf-8") as handle:
    handle.write(text)
PY

echo "Готово. Политика в $AGENTS между маркерами; всё остальное в файле не тронуто."
echo "Для подкрепления на каждом промпте: codex features enable hooks, затем /hooks."
