"""AI helpers kept in the plugin build: deterministic tool functions only.

Upstream's model client and prompts are not part of the plugin. Claude does
that work itself, guided by the skill references. ``tools`` keeps the value
normalisers and the evidence lookups the skills expose through the CLI.
"""
