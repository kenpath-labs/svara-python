# Agent skills: where they are published and how

`skills/` holds seven [Agent Skills](https://agentskills.io). The repository is
laid out so that one public GitHub repo serves every channel:

```
.claude-plugin/marketplace.json   # Claude Code marketplace "kenpath-labs" → plugin "svara" (source ./)
.claude-plugin/plugin.json        # plugin metadata; skills load from the default skills/ folder
skills/<name>/SKILL.md            # what `npx skills`, SkillsMP, claude-plugins.dev and others index
```

`skills/` is not in the PyPI sdist or wheel; see `[tool.hatch.build]` in
`pyproject.toml`.

Checked 2026-10-03:
- `npx skills add ./ --list` finds all seven skills.
- `skills-ref validate` passes for each skill.
- `claude plugin validate .` passes.
- A local `/plugin marketplace add` and `/plugin install svara@kenpath-labs` loads all seven.

## Where to publish

| Platform | How a skill is listed | What to do | Priority |
|---|---|---|---|
| [skills.sh](https://skills.sh) (Vercel, `npx skills`) | Automatically: the leaderboard counts anonymous installs from `npx skills add owner/repo`. Works with dozens of agents. | Nothing to submit. Put `npx skills add kenpath-labs/svara-python` in the docs, the README and the console so installs happen. | **Must** |
| [Claude plugin directory](https://claude.ai/directory/manage) (Anthropic, built into Claude Code's `/plugin`) | Reviewed submission of a GitHub repo + branch/tag; automated validation and security scan, then review | claude.ai → Directory → Submit new → Plugin bundle. Use repo `kenpath-labs/svara-python`, path `/`, and track `main` or a release tag. Answer the data-handling questions: audio text is sent to api.kenpathlabs.com. | **Must** |
| Self-hosted marketplace (this repo) | Push `.claude-plugin/marketplace.json` | Done. Users run `/plugin marketplace add kenpath-labs/svara-python`. | **Must** (done) |
| [OpenAgentSkill](https://www.openagentskill.com/submit) | Auto-indexes GitHub, plus a one-URL submit form; reviewed asynchronously, no star minimum | Submit `https://github.com/kenpath-labs/svara-python/tree/main/skills`, or each skill folder. Category: audio/voice. Tags: tts, voice, multilingual, hindi, telephony. | Worth it |
| [SkillsMP](https://skillsmp.com) | Automatically aggregates public GitHub SKILL.md files and ranks them by repo stars | Nothing to submit; listing follows from the public repo. Stars raise the ranking. | Passive |
| [claude-plugins.dev](https://claude-plugins.dev) | Automatically discovers public `marketplace.json` files and skills | Nothing to submit. Users install with `npx claude-plugins install @kenpath-labs/kenpath-labs/svara`. | Passive |
| [Smithery skills](https://smithery.ai/skills) | Indexed from GitHub; installed with `smithery skill add` | Check the listing after indexing. Matters more if we ship an MCP server later. | Low |
| [VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills) | Pull request, but only for skills with real community usage | Once installs show on skills.sh, open a PR titled `Add skill: kenpath-labs/svara-tts` for the official-teams section. | Later |
| [anthropics/skills](https://github.com/anthropics/skills) | Anthropic's own examples, not a third-party registry | Skip. Use it as the format reference. | Skip |
| [ClawHub](https://clawhub.ai) (OpenClaw) | CLI publish (`clawhub skill publish`) with versioned releases | Skip for now: it needs its own publish step and the registry has a malware track record. Revisit if OpenClaw users ask. | Skip |

## Making the skills easy to find

Agents choose a skill by matching the user's request against its
`description`, so keep the descriptions concrete. Each one names:
- the task ("IVR prompts", "audiobook", "voice agent")
- the languages ("Hindi, Tamil…", "Hinglish")
- the integrations ("Twilio", "LiveKit", "Pipecat")
- the providers it replaces ("OpenAI TTS or ElevenLabs alternative")

When adding a skill, write the description in the words a user would type.

Install counts drive the skills.sh and SkillsMP rankings. Put the install
commands where developers already are:
- the docs site's quickstart and SDKs page
- https://kenpathlabs.com/developers
- the console's API-keys page
- docs/llms.txt and https://docs.kenpathlabs.com/llms.txt

Starring the GitHub repo also helps SkillsMP.

## Releasing a skills update

1. Edit the skills and regenerate the snapshots (see `skills/README.md`).
2. Bump `version` in `.claude-plugin/plugin.json` and in the plugin entry of `marketplace.json`.
3. Run `skills-ref validate skills/<name>` and `claude plugin validate .`.
4. Merge to `main`.
   - skills.sh and the indexers pick up the change on their next crawl.
   - The Claude directory re-reviews the tracked branch or tag. After the first approval, new commits can publish automatically.
