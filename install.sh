#!/usr/bin/env bash
# Установка koroche-blyat для Claude Code одной командой:
#   curl -fsSL https://raw.githubusercontent.com/maksimryzhov614/koroche-blyat/main/install.sh | bash
#
# Ставит:
#   ~/.claude/skills/koroche-blyat/          — скилл со словарём, сценами и онтологией
#   ~/.claude/output-styles/koroche-blyat.md — постоянный тон
#   ~/.claude/hooks/koroche-blyat.sh         — подкрепление стиля на каждом промпте
#   ~/.claude/settings.json                  — outputStyle и регистрация хука
set -euo pipefail

CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
RAW="${RAW:-https://raw.githubusercontent.com/maksimryzhov614/koroche-blyat/main}"

mkdir -p "$CLAUDE_DIR/skills/koroche-blyat/references" \
         "$CLAUDE_DIR/output-styles" "$CLAUDE_DIR/hooks"

curl -fsSL "$RAW/skills/koroche-blyat/SKILL.md" -o "$CLAUDE_DIR/skills/koroche-blyat/SKILL.md"
for f in slovar.md sceny.md ontologia.md compression.md; do
  curl -fsSL "$RAW/skills/koroche-blyat/references/$f" -o "$CLAUDE_DIR/skills/koroche-blyat/references/$f"
done
curl -fsSL "$RAW/output-styles/koroche-blyat.md" -o "$CLAUDE_DIR/output-styles/koroche-blyat.md"
curl -fsSL "$RAW/hooks/style-reminder.sh" -o "$CLAUDE_DIR/hooks/koroche-blyat.sh"
chmod +x "$CLAUDE_DIR/hooks/koroche-blyat.sh"

SETTINGS="$CLAUDE_DIR/settings.json"
if command -v python3 >/dev/null 2>&1; then
  python3 - "$SETTINGS" "$CLAUDE_DIR/hooks/koroche-blyat.sh" <<'PY'
import json, os, sys
path, cmd = sys.argv[1], sys.argv[2]
data = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
data["outputStyle"] = "koroche-blyat"
groups = data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
if not any(h.get("command") == cmd for g in groups for h in g.get("hooks", [])):
    groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
with open(path, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2, ensure_ascii=False)
    handle.write("\n")
PY
  echo "Готово. outputStyle и хук прописаны в $SETTINGS"
else
  echo "python3 не найден — включи стиль вручную: /config -> Output style -> koroche-blyat"
fi

echo "Перезапусти claude: стиль и список скиллов читаются при старте процесса."
