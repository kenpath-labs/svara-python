# Svara agent skills

[Agent Skills](https://agentskills.io) that teach coding agents (Claude Code,
Codex, Cursor, Gemini CLI, GitHub Copilot, Windsurf, OpenCode and other
SKILL.md-compatible tools) to build with **Svara TTS Turbo**, Kenpath Labs'
text-to-speech API: 320 voices, 82 languages, realtime streaming and telephony
formats.

| Skill | An agent loads it when the user wants to… |
|---|---|
| [`svara-tts`](svara-tts/SKILL.md) | turn text into speech (Python, curl, JavaScript), choose formats, handle errors |
| [`svara-voices`](svara-voices/SKILL.md) | find, filter and preview voices. No API key needed. Includes `find_voices.py` |
| [`svara-voice-agent`](svara-voice-agent/SKILL.md) | build a realtime voice agent: stream LLM tokens into speech, LiveKit, Pipecat, raw WebSocket |
| [`svara-telephony`](svara-telephony/SKILL.md) | put speech on a phone line: 8 kHz µ-law/A-law, IVR prompts, Twilio/Plivo/Vobiz, SIP |
| [`svara-multilingual`](svara-multilingual/SKILL.md) | speak Indian and other languages, mixed-script text, normalisation, pronunciation dictionaries |
| [`svara-narration`](svara-narration/SKILL.md) | narrate long text (audiobooks, voiceovers), with SRT subtitles. Includes `narrate.py` |
| [`svara-migrate`](svara-migrate/SKILL.md) | move OpenAI or ElevenLabs TTS code to Svara, or compare providers fairly |

## Install

Any SKILL.md-compatible agent, via [skills.sh](https://skills.sh):

```bash
npx skills add kenpath-labs/svara-python                 # choose skills and agents interactively
npx skills add kenpath-labs/svara-python --skill '*' -y  # all of them
```

Claude Code, as a plugin:

```
/plugin marketplace add kenpath-labs/svara-python
/plugin install svara@kenpath-labs
```

Manual install: copy a skill's folder into your agent's skills directory, for
example `~/.claude/skills/` or `.agents/skills/`.

Synthesis needs an API key in `SVARA_API_KEY`. Create one at
https://platform.kenpathlabs.com/dashboard/keys. Browsing voices and languages
needs no key.

## Maintaining

- These skills describe the SDK and the API as they are. When either changes,
  update the affected skill in the same change, as with the docs.
- Regenerate the snapshots after the catalogue changes:
  ```bash
  python skills/svara-voices/scripts/find_voices.py --markdown > skills/svara-voices/references/voice-catalogue.md
  ```
  `skills/svara-multilingual/references/languages.md` comes from `GET /v1/languages`.
- Validate before you push:
  `python -m skills_ref.cli validate skills/<name>` (`pip install skills-ref`) and
  `claude plugin validate .`.
- Bump `version` in `.claude-plugin/plugin.json` and `marketplace.json` when the
  skills change. Claude Code uses it to offer updates.
- Where the skills are listed, and how: [docs/agent-skills.md](../docs/agent-skills.md).
